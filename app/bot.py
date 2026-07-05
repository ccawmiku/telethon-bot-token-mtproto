from __future__ import annotations

import asyncio
import logging
import re
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
LIMIT_RE = re.compile(r"^/limit(?:@\w+)?\s+([0-9]+(?:\.[0-9]+)?)(?:m|mb)?$", re.IGNORECASE)
DELAY_RE = re.compile(r"^/delay(?:@\w+)?\s+([0-9]+(?:\.[0-9]+)?)$", re.IGNORECASE)


def human_size(bytes_count: int | float | None) -> str:
    if not bytes_count:
        return "0 B"
    size = float(bytes_count)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} TB"


def progress_bar(percent: int | float, width: int = 18) -> str:
    value = max(0, min(100, float(percent)))
    filled = round(width * value / 100)
    return f"[{'#' * filled}{'-' * (width - filled)}]"


def format_duration(seconds: int | float) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}小时{minutes}分钟"
    if minutes:
        return f"{minutes}分钟{secs}秒"
    return f"{secs}秒"


def parse_limit_mb(text: str) -> float | None:
    match = LIMIT_RE.match(text.strip())
    if not match:
        return None
    return max(1.0, float(match.group(1)))


def parse_delay_hours(text: str) -> float | None:
    match = DELAY_RE.match(text.strip())
    if not match:
        return None
    hours = float(match.group(1))
    if hours <= 0:
        return None
    return min(12.0, hours)


class RuntimeControls:
    def __init__(self) -> None:
        self.speed_limit_bytes_per_second: int | None = None
        self.delay_until: float = 0.0

    def set_limit_mb(self, megabytes: float) -> float:
        value = max(1.0, megabytes)
        self.speed_limit_bytes_per_second = int(value * 1024 * 1024)
        return value

    def set_delay_hours(self, hours: float) -> float:
        value = min(12.0, max(0.0, hours))
        self.delay_until = time.time() + value * 3600
        return value

    def delay_remaining_seconds(self) -> int:
        return max(0, int(self.delay_until - time.time()))

    def limit_text(self) -> str:
        if not self.speed_limit_bytes_per_second:
            return "未设置"
        return f"{self.speed_limit_bytes_per_second / 1024 / 1024:.1f} MB/s"

    async def wait_if_delayed(self, edit_status=None, label: str = "下载已暂停") -> None:
        notified = False
        while self.delay_remaining_seconds() > 0:
            remaining = self.delay_remaining_seconds()
            if edit_status and not notified:
                await edit_status(f"{label}。\n剩余：{format_duration(remaining)}")
                notified = True
            await asyncio.sleep(min(remaining, 60))

    async def throttle(self, downloaded: int, started_at: float) -> None:
        if not self.speed_limit_bytes_per_second or downloaded <= 0:
            return
        elapsed = max(0.001, time.monotonic() - started_at)
        expected_elapsed = downloaded / self.speed_limit_bytes_per_second
        if expected_elapsed > elapsed:
            await asyncio.sleep(expected_elapsed - elapsed)


class ProgressReporter:
    def __init__(
        self,
        status_message,
        target_path: Path,
        record_id: str,
        history: DownloadHistory,
        interval: float,
        percent_step: int,
        controls: RuntimeControls,
    ):
        self.status_message = status_message
        self.target_path = target_path
        self.record_id = record_id
        self.history = history
        self.interval = interval
        self.percent_step = max(1, percent_step)
        self.controls = controls
        self.last_edit_at = 0.0
        self.last_percent = -1
        self.started_at = time.monotonic()
        self.loop = asyncio.get_running_loop()
        self.lock = asyncio.Lock()

    async def callback(self, downloaded: int, total: int) -> None:
        await self._maybe_edit(downloaded, total)
        await self.controls.wait_if_delayed(self._edit, "下载已暂停")
        await self.controls.throttle(downloaded, self.started_at)

    async def _maybe_edit(self, downloaded: int, total: int) -> None:
        percent = min(100, int(downloaded * 100 / total)) if total and total > 0 else 0
        now = time.monotonic()
        should_edit = percent >= 100 or (
            percent - self.last_percent >= self.percent_step
            and now - self.last_edit_at >= self.interval
        ) or (not total and now - self.last_edit_at >= self.interval)
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
            if total and total > 0:
                progress_text = (
                    f"进度：{progress_bar(percent)} {percent}%\n"
                    f"大小：{human_size(downloaded)} / {human_size(total)}"
                )
            else:
                progress_text = f"已下载：{human_size(downloaded)}"
            text = (
                "正在下载...\n"
                f"文件：`{self.target_path.name}`\n"
                f"{progress_text}\n"
                f"限速：{self.controls.limit_text()}"
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
    item_progress: dict[str, tuple[int, int]] = field(default_factory=dict)
    last_edit_at: float = 0.0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
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
        self._download_lock = asyncio.Lock()
        self.controls = RuntimeControls()

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

    async def _handle_command(self, event, settings: Settings) -> bool:
        text = (event.message.raw_text or "").strip()
        if not text.startswith("/"):
            return False
        sender_id = getattr(event, "sender_id", None)
        if not self._is_allowed(sender_id, settings):
            await self._reply_unauthorized(event, settings)
            return True

        command = text.split()[0].split("@", 1)[0].lower()
        if command == "/help":
            await event.reply(
                "可用命令：\n"
                "/limit x - 设置下载限速为 x MB/s，最小 1 MB/s。例如：`/limit 2`\n"
                "/delay x - 暂停下载 x 小时，到时间自动继续，最长 12 小时。例如：`/delay 1.5`\n\n"
                f"当前限速：{self.controls.limit_text()}\n"
                f"暂停剩余：{format_duration(self.controls.delay_remaining_seconds())}"
            )
            return True

        if command == "/limit":
            value = parse_limit_mb(text)
            if value is None:
                await event.reply("用法：`/limit x`，x 为 MB/s，最小 1。例如：`/limit 2`")
                return True
            applied = self.controls.set_limit_mb(value)
            await event.reply(f"已设置下载限速：{applied:.1f} MB/s")
            return True

        if command == "/delay":
            value = parse_delay_hours(text)
            if value is None:
                await event.reply("用法：`/delay x`，x 为小时，最长 12。例如：`/delay 2`")
                return True
            applied = self.controls.set_delay_hours(value)
            await event.reply(f"已暂停下载：{applied:.2g} 小时，到时间会自动继续。")
            return True

        return False

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
        if await self._handle_command(event, settings):
            return

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
            self.controls,
        )

        try:
            logger.info("下载消息 %s 到 %s", message.id, target_path)
            await self.controls.wait_if_delayed(reporter._edit, "下载已暂停")
            async with self._download_lock:
                await self.controls.wait_if_delayed(reporter._edit, "下载已暂停")
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
                f"进度：{progress_bar(100)} 100%\n"
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
            status = await event.reply(f"收到一组媒体，正在下载...\n进度：{progress_bar(0)} 0%")
            batch = AlbumBatch(chat_id=getattr(event, "chat_id", None), status_message=status)
            self._albums[album_key] = batch

        if batch.finalize_task and not batch.finalize_task.done():
            batch.finalize_task.cancel()

        batch.total += 1
        batch.active += 1
        await self._update_album_status(batch)
        await self._download_album_item(message, settings, batch)
        batch.active -= 1
        await self._update_album_status(batch, force=True)
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
            started_at = time.monotonic()

            async def album_progress(downloaded: int, total: int) -> None:
                self.history.update(
                    record_id,
                    status="downloading",
                    progress=min(100, int(downloaded * 100 / total)) if total else 0,
                    downloaded_bytes=downloaded,
                    total_bytes=total or 0,
                )
                batch.item_progress[record_id] = (downloaded, total or 0)
                await self._update_album_status(batch)
                await self.controls.wait_if_delayed(
                    lambda text: self._safe_edit(batch.status_message, text),
                    "批量下载已暂停",
                )
                await self.controls.throttle(downloaded, started_at)

            await self.controls.wait_if_delayed(
                lambda text: self._safe_edit(batch.status_message, text),
                "批量下载已暂停",
            )
            async with self._download_lock:
                await self.controls.wait_if_delayed(
                    lambda text: self._safe_edit(batch.status_message, text),
                    "批量下载已暂停",
                )
                downloaded_path = await message.download_media(
                    file=str(target_path),
                    progress_callback=album_progress,
                )
            if downloaded_path is None:
                batch.failed += 1
                batch.errors.append(f"{target_path.name}: Telegram 没有返回文件")
                self.history.update(record_id, status="failed", error="Telegram 没有返回文件")
                return

            final_path = Path(downloaded_path)
            file_size = final_path.stat().st_size if final_path.exists() else 0
            batch.completed += 1
            batch.files.append(final_path.name)
            batch.item_progress[record_id] = (file_size, file_size)
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

    async def _safe_edit(self, status_message, text: str) -> None:
        try:
            await status_message.edit(text)
        except MessageNotModifiedError:
            return
        except FloodWaitError as exc:
            await asyncio.sleep(exc.seconds)
        except RPCError:
            logger.exception("无法编辑状态消息")

    async def _update_album_status(self, batch: AlbumBatch, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - batch.last_edit_at < 3:
            return
        async with batch.lock:
            total_bytes = sum(total for _, total in batch.item_progress.values())
            downloaded_bytes = sum(downloaded for downloaded, _ in batch.item_progress.values())
            if total_bytes > 0:
                percent = min(100, int(downloaded_bytes * 100 / total_bytes))
                progress_text = (
                    f"进度：{progress_bar(percent)} {percent}%\n"
                    f"大小：{human_size(downloaded_bytes)} / {human_size(total_bytes)}"
                )
            else:
                done = batch.completed + batch.failed
                percent = int(done * 100 / batch.total) if batch.total else 0
                progress_text = f"进度：{progress_bar(percent)} {percent}%"
            text = (
                "批量下载中...\n"
                f"{progress_text}\n"
                f"文件：{batch.completed + batch.failed}/{batch.total} 个\n"
                f"限速：{self.controls.limit_text()}"
            )
            await self._safe_edit(batch.status_message, text)
            batch.last_edit_at = now

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
                    f"总数：{batch.total} 个\n"
                    f"进度：{progress_bar(100)} 100%"
                )
            else:
                text = f"{batch.completed} 个文件已下载完成。\n进度：{progress_bar(100)} 100%"
            await batch.status_message.edit(text)
            self._albums.pop(album_key, None)
        except asyncio.CancelledError:
            return
