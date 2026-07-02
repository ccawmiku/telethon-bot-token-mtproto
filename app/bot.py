from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from telethon import TelegramClient, events
from telethon.errors import FloodWaitError, MessageNotModifiedError, RPCError

from app.config import Settings
from app.file_naming import media_kind, unique_media_path
from app.history import DownloadHistory, DownloadRecord

logger = logging.getLogger("media-downloader-bot")


def human_size(bytes_count: int | float | None) -> str:
    if not bytes_count:
        return "0 B"
    size = float(bytes_count)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} TB"


class ProgressReporter:
    def __init__(
        self,
        status_message,
        target_path: Path,
        record_id: str,
        history: DownloadHistory,
        interval: float,
        percent_step: int,
    ):
        self.status_message = status_message
        self.target_path = target_path
        self.record_id = record_id
        self.history = history
        self.interval = interval
        self.percent_step = max(1, percent_step)
        self.last_edit_at = 0.0
        self.last_percent = -1
        self.loop = asyncio.get_running_loop()
        self.lock = asyncio.Lock()

    def callback(self, downloaded: int, total: int) -> None:
        self.loop.create_task(self._maybe_edit(downloaded, total))

    async def _maybe_edit(self, downloaded: int, total: int) -> None:
        if total <= 0:
            return
        percent = min(100, int(downloaded * 100 / total))
        now = time.monotonic()
        should_edit = percent >= 100 or (
            percent - self.last_percent >= self.percent_step
            and now - self.last_edit_at >= self.interval
        )
        self.history.update(
            self.record_id,
            status="downloading",
            progress=percent,
            downloaded_bytes=downloaded,
            total_bytes=total,
        )
        if not should_edit:
            return

        async with self.lock:
            text = (
                "正在下载...\n"
                f"文件：`{self.target_path.name}`\n"
                f"进度：{percent}% ({human_size(downloaded)} / {human_size(total)})"
            )
            await self._edit(text)
            self.last_percent = percent
            self.last_edit_at = time.monotonic()

    async def _edit(self, text: str) -> None:
        try:
            await self.status_message.edit(text)
        except MessageNotModifiedError:
            return
        except FloodWaitError as exc:
            logger.warning("编辑进度消息触发 Telegram 限流：%s 秒", exc.seconds)
            await asyncio.sleep(exc.seconds)
        except RPCError:
            logger.exception("无法编辑进度消息")


@dataclass
class AlbumBatch:
    chat_id: int | None
    status_message: Any
    total: int = 0
    completed: int = 0
    failed: int = 0
    active: int = 0
    files: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    finalize_task: asyncio.Task | None = None


class BotManager:
    def __init__(self, history: DownloadHistory):
        self.history = history
        self.client: TelegramClient | None = None
        self.task: asyncio.Task | None = None
        self.settings: Settings | None = None
        self.username: str | None = None
        self.last_error: str = ""
        self.started_at: str = ""
        self._lock = asyncio.Lock()
        self._albums: dict[str, AlbumBatch] = {}

    @property
    def running(self) -> bool:
        return bool(self.client and self.client.is_connected())

    def state(self) -> dict[str, Any]:
        task_done = bool(self.task and self.task.done())
        return {
            "running": self.running,
            "username": self.username,
            "last_error": self.last_error,
            "started_at": self.started_at,
            "task_done": task_done,
        }

    def _is_allowed(self, sender_id: int | None, settings: Settings) -> bool:
        allowed_ids = settings.allowed_user_ids or []
        return bool(sender_id and sender_id in allowed_ids)

    async def _reply_unauthorized(self, event, settings: Settings) -> None:
        sender_id = getattr(event, "sender_id", None)
        if not (settings.allowed_user_ids or []):
            await event.reply(
                "未配置允许使用的 Telegram 用户 ID。\n"
                f"你的用户 ID 是：`{sender_id}`\n"
                "请在网页控制面板的“允许用户 ID”里添加这个 ID，然后保存配置。"
            )
            return
        await event.reply(
            "你没有权限使用这个 bot。\n"
            f"你的 Telegram 用户 ID 是：`{sender_id}`"
        )
        logger.warning("已拒绝未授权 Telegram 用户：%s", sender_id)

    async def start(self, settings: Settings) -> None:
        async with self._lock:
            if self.running:
                return
            if not settings.ready:
                raise RuntimeError("启动 bot 前必须填写 API_ID、API_HASH 和 BOT_TOKEN")

            settings.ensure_dirs()
            session_path = settings.session_dir / settings.session_name
            client = TelegramClient(str(session_path), settings.api_id, settings.api_hash)

            @client.on(events.NewMessage(incoming=True))
            async def on_message(event):
                await self._handle_media(event, settings)

            await client.start(bot_token=settings.bot_token)
            me = await client.get_me()
            self.client = client
            self.settings = settings
            self.username = getattr(me, "username", None)
            self.last_error = ""
            self.started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            self.task = asyncio.create_task(self._run_until_disconnected(client))
            logger.info("Bot 已启动：@%s", self.username)

    async def stop(self) -> None:
        async with self._lock:
            if self.client:
                await self.client.disconnect()
            if self.task:
                try:
                    await asyncio.wait_for(self.task, timeout=5)
                except asyncio.TimeoutError:
                    self.task.cancel()
            self.client = None
            self.task = None
            self.username = None

    async def restart(self, settings: Settings) -> None:
        await self.stop()
        await self.start(settings)

    async def _run_until_disconnected(self, client: TelegramClient) -> None:
        try:
            await client.run_until_disconnected()
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            logger.exception("Bot 断开连接")

    async def _handle_media(self, event, settings: Settings) -> None:
        message = event.message
        if not self._is_allowed(getattr(event, "sender_id", None), settings):
            await self._reply_unauthorized(event, settings)
            return

        if not message.media:
            await event.reply("请发送图片、视频或文件，我会自动下载。")
            return

        grouped_id = getattr(message, "grouped_id", None)
        if grouped_id:
            await self._handle_album_media(event, settings, str(grouped_id))
            return

        kind = media_kind(message)
        target_path = unique_media_path(
            message,
            settings.media_dir(kind),
            settings.max_filename_stem_length,
        )
        record_id = uuid.uuid4().hex
        chat_id = getattr(event, "chat_id", None)
        self.history.add(
            DownloadRecord(
                id=record_id,
                message_id=message.id,
                chat_id=chat_id,
                file_name=target_path.name,
                path=str(target_path),
            )
        )
        status = await event.reply(f"已加入下载队列...\n文件：`{target_path.name}`")
        self.history.update(record_id, status="queued")

        reporter = ProgressReporter(
            status,
            target_path,
            record_id,
            self.history,
            settings.progress_interval_seconds,
            settings.progress_percent_step,
        )

        try:
            logger.info("下载消息 %s 到 %s", message.id, target_path)
            downloaded_path = await message.download_media(
                file=str(target_path),
                progress_callback=reporter.callback,
            )
            if downloaded_path is None:
                self.history.update(record_id, status="failed", error="Telegram 没有返回文件")
                await status.edit("下载失败：Telegram 没有返回文件。")
                return

            final_path = Path(downloaded_path)
            file_size = final_path.stat().st_size if final_path.exists() else 0
            self.history.update(
                record_id,
                status="complete",
                progress=100,
                downloaded_bytes=file_size,
                total_bytes=file_size,
                size_bytes=file_size,
                path=str(final_path),
                file_name=final_path.name,
            )
            await status.edit(
                "下载完成。\n"
                f"文件：`{final_path.name}`\n"
                f"大小：{human_size(file_size)}\n"
                f"路径：`{final_path}`"
            )
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            logger.exception("下载失败")
            self.history.update(record_id, status="failed", error=error)
            await status.edit(f"下载失败：`{error}`")

    async def _handle_album_media(self, event, settings: Settings, grouped_id: str) -> None:
        message = event.message
        album_key = f"{getattr(event, 'chat_id', None)}:{grouped_id}"
        batch = self._albums.get(album_key)
        if batch is None:
            status = await event.reply("收到一组媒体，正在下载...")
            batch = AlbumBatch(chat_id=getattr(event, "chat_id", None), status_message=status)
            self._albums[album_key] = batch

        if batch.finalize_task and not batch.finalize_task.done():
            batch.finalize_task.cancel()

        batch.total += 1
        batch.active += 1
        await self._download_album_item(message, settings, batch)
        batch.active -= 1
        batch.finalize_task = asyncio.create_task(self._finalize_album_later(album_key))

    async def _download_album_item(self, message, settings: Settings, batch: AlbumBatch) -> None:
        kind = media_kind(message)
        target_path = unique_media_path(
            message,
            settings.media_dir(kind),
            settings.max_filename_stem_length,
        )
        record_id = uuid.uuid4().hex
        self.history.add(
            DownloadRecord(
                id=record_id,
                message_id=message.id,
                chat_id=batch.chat_id,
                file_name=target_path.name,
                path=str(target_path),
            )
        )
        try:
            logger.info("下载相册文件 %s 到 %s", message.id, target_path)
            downloaded_path = await message.download_media(file=str(target_path))
            if downloaded_path is None:
                batch.failed += 1
                batch.errors.append(f"{target_path.name}: Telegram 没有返回文件")
                self.history.update(record_id, status="failed", error="Telegram 没有返回文件")
                return

            final_path = Path(downloaded_path)
            file_size = final_path.stat().st_size if final_path.exists() else 0
            batch.completed += 1
            batch.files.append(final_path.name)
            self.history.update(
                record_id,
                status="complete",
                progress=100,
                downloaded_bytes=file_size,
                total_bytes=file_size,
                size_bytes=file_size,
                path=str(final_path),
                file_name=final_path.name,
            )
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            batch.failed += 1
            batch.errors.append(error)
            logger.exception("相册文件下载失败")
            self.history.update(record_id, status="failed", error=error)

    async def _finalize_album_later(self, album_key: str) -> None:
        try:
            await asyncio.sleep(2)
            batch = self._albums.get(album_key)
            if batch is None:
                return
            while batch.active > 0:
                await asyncio.sleep(0.5)
            if batch.failed:
                text = (
                    f"批量下载完成：{batch.completed} 个成功，{batch.failed} 个失败。\n"
                    f"总数：{batch.total} 个"
                )
            else:
                text = f"{batch.completed} 个文件已下载完成。"
            await batch.status_message.edit(text)
            self._albums.pop(album_key, None)
        except asyncio.CancelledError:
            return
