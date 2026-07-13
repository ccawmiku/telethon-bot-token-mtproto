from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from telethon import TelegramClient, events, functions, types
from telethon.errors import FloodWaitError, MessageNotModifiedError, RPCError

from app import __version__
from app.config import Settings
from app.file_naming import media_kind, unique_media_path
from app.history import DownloadHistory, DownloadRecord, RETRYABLE_STATUSES

logger = logging.getLogger("media-downloader-bot")
LIMIT_RE = re.compile(r"^/limit(?:@\w+)?\s+([0-9]+(?:\.[0-9]+)?)(?:m|mb)?$", re.IGNORECASE)
DELAY_RE = re.compile(r"^/delay(?:@\w+)?\s+([0-9]+(?:\.[0-9]+)?)$", re.IGNORECASE)
DURATION_RE = re.compile(r"^([0-9]+(?:\.[0-9]+)?)(s|m|h)?$", re.IGNORECASE)


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


def format_duration(seconds: int | float | None) -> str:
    if seconds is None:
        return "无限期"
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


def parse_pause_seconds(value: str) -> float | None:
    match = DURATION_RE.fullmatch(value.strip())
    if not match:
        return None
    amount = float(match.group(1))
    if amount <= 0:
        return None
    unit = (match.group(2) or "m").lower()
    multiplier = {"s": 1, "m": 60, "h": 3600}[unit]
    return min(12 * 3600, amount * multiplier)


class RuntimeControls:
    def __init__(self) -> None:
        self.speed_limit_bytes_per_second: int | None = None
        self.pause_until: float = 0.0
        self.pause_indefinitely = False
        self._changed = asyncio.Event()

    def set_limit_mb(self, megabytes: float) -> float:
        value = max(1.0, megabytes)
        self.speed_limit_bytes_per_second = int(value * 1024 * 1024)
        self._changed.set()
        return value

    def clear_limit(self) -> None:
        self.speed_limit_bytes_per_second = None
        self._changed.set()

    def pause(self, seconds: float | None = None) -> None:
        if seconds is None:
            self.pause_indefinitely = True
            self.pause_until = 0.0
        else:
            self.pause_indefinitely = False
            self.pause_until = time.time() + max(0.0, seconds)
        self._changed.set()

    def set_delay_hours(self, hours: float) -> float:
        value = min(12.0, max(0.0, hours))
        self.pause(value * 3600)
        return value

    def resume(self) -> None:
        self.pause_indefinitely = False
        self.pause_until = 0.0
        self._changed.set()

    def is_paused(self) -> bool:
        return self.pause_indefinitely or self.pause_until > time.time()

    def delay_remaining_seconds(self) -> int | None:
        if self.pause_indefinitely:
            return None
        return max(0, int(self.pause_until - time.time()))

    def limit_text(self) -> str:
        if not self.speed_limit_bytes_per_second:
            return "未设置"
        return f"{self.speed_limit_bytes_per_second / 1024 / 1024:.1f} MB/s"

    def state(self) -> dict[str, Any]:
        return {
            "paused": self.is_paused(),
            "pause_indefinitely": self.pause_indefinitely,
            "pause_remaining_seconds": self.delay_remaining_seconds(),
            "speed_limit_bytes_per_second": self.speed_limit_bytes_per_second,
            "speed_limit_text": self.limit_text(),
        }

    async def wait_if_paused(
        self,
        on_pause: Callable[[str], Awaitable[None]] | None = None,
        label: str = "下载已暂停",
    ) -> float:
        started = time.monotonic()
        notified = False
        while self.is_paused():
            remaining = self.delay_remaining_seconds()
            if on_pause and not notified:
                suffix = "等待 /resume" if remaining is None else f"剩余：{format_duration(remaining)}"
                await on_pause(f"{label}。\n{suffix}")
                notified = True
            self._changed.clear()
            timeout = 60.0 if remaining is None else max(0.1, min(float(remaining), 60.0))
            try:
                await asyncio.wait_for(self._changed.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                pass
        return time.monotonic() - started

    async def throttle(self, byte_count: int) -> None:
        if byte_count <= 0:
            return
        started = time.monotonic()
        while self.speed_limit_bytes_per_second:
            expected = byte_count / self.speed_limit_bytes_per_second
            remaining = expected - (time.monotonic() - started)
            if remaining <= 0:
                return
            self._changed.clear()
            try:
                await asyncio.wait_for(self._changed.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                return


@dataclass
class DownloadJob:
    record_id: str
    message: Any
    target_path: Path
    status_message: Any | None
    album_key: str | None = None


@dataclass
class AlbumBatch:
    chat_id: int | None
    status_message: Any
    total: int = 0
    completed: int = 0
    failed: int = 0
    cancelled: int = 0
    item_progress: dict[str, tuple[int, int]] = field(default_factory=dict)
    last_edit_at: float = 0.0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    finalize_task: asyncio.Task | None = None


class ProgressReporter:
    def __init__(self, manager: "BotManager", job: DownloadJob, settings: Settings):
        self.manager = manager
        self.job = job
        self.settings = settings
        self.started_at = time.monotonic()
        self.paused_seconds = 0.0
        self.last_sample_at = self.started_at
        self.last_sample_bytes = 0
        self.last_throttled_bytes = 0
        self.speed = 0.0
        self.last_edit_at = 0.0
        self.last_percent = -1
        self.lock = asyncio.Lock()

    async def callback(self, downloaded: int, total: int) -> None:
        now = time.monotonic()
        elapsed = now - self.last_sample_at
        if elapsed >= 0.2 and downloaded >= self.last_sample_bytes:
            current_speed = (downloaded - self.last_sample_bytes) / elapsed
            self.speed = current_speed if self.speed <= 0 else self.speed * 0.65 + current_speed * 0.35
            self.last_sample_at = now
            self.last_sample_bytes = downloaded
        elif self.speed <= 0:
            active_elapsed = max(0.001, now - self.started_at - self.paused_seconds)
            self.speed = downloaded / active_elapsed

        percent = min(100, int(downloaded * 100 / total)) if total else 0
        eta = int((total - downloaded) / self.speed) if total > downloaded and self.speed > 0 else 0
        paused = self.manager.controls.is_paused()
        self.manager.history.update(
            self.job.record_id,
            persist=False,
            status="paused" if paused else "downloading",
            progress=percent,
            downloaded_bytes=downloaded,
            total_bytes=total or 0,
            speed_bytes_per_second=round(self.speed, 2),
            eta_seconds=eta,
            speed_limit_bytes_per_second=self.manager.controls.speed_limit_bytes_per_second,
        )
        if self.job.album_key:
            await self.manager.update_album_progress(self.job.album_key, self.job.record_id, downloaded, total)
        else:
            await self._maybe_edit(downloaded, total, percent, eta)

        paused_for = await self.manager.controls.wait_if_paused(
            self._pause_message,
            "下载已暂停",
        )
        if paused_for:
            self.paused_seconds += paused_for
            self.last_sample_at = time.monotonic()
            self.last_sample_bytes = downloaded
            self.manager.history.update(self.job.record_id, status="downloading")
        delta = max(0, downloaded - self.last_throttled_bytes)
        self.last_throttled_bytes = downloaded
        await self.manager.controls.throttle(delta)

    async def _pause_message(self, text: str) -> None:
        self.manager.history.update(self.job.record_id, status="paused")
        if self.job.album_key:
            batch = self.manager.albums.get(self.job.album_key)
            if batch:
                await self.manager.safe_edit(batch.status_message, text)
        elif self.job.status_message:
            await self.manager.safe_edit(self.job.status_message, text)

    async def _maybe_edit(self, downloaded: int, total: int, percent: int, eta: int) -> None:
        now = time.monotonic()
        should_edit = percent >= 100 or (
            percent - self.last_percent >= max(1, self.settings.progress_percent_step)
            and now - self.last_edit_at >= self.settings.progress_interval_seconds
        ) or (not total and now - self.last_edit_at >= self.settings.progress_interval_seconds)
        if not should_edit or not self.job.status_message:
            return
        async with self.lock:
            limit_line = ""
            if self.manager.controls.speed_limit_bytes_per_second:
                limit_line = f"\n限速：{self.manager.controls.limit_text()}"
            text = (
                "正在下载...\n"
                f"文件：`{self.job.target_path.name}`\n"
                f"进度：{progress_bar(percent)} {percent}%\n"
                f"大小：{human_size(downloaded)} / {human_size(total)}\n"
                f"速度：{human_size(self.speed)}/s\n"
                f"预计剩余：{format_duration(eta)}{limit_line}"
            )
            await self.manager.safe_edit(self.job.status_message, text)
            self.last_percent = percent
            self.last_edit_at = time.monotonic()


class BotManager:
    def __init__(self, history: DownloadHistory):
        self.history = history
        self.client: TelegramClient | None = None
        self.task: asyncio.Task | None = None
        self.worker_task: asyncio.Task | None = None
        self.active_task: asyncio.Task | None = None
        self.active_job: DownloadJob | None = None
        self.queue: asyncio.Queue[DownloadJob] | None = None
        self.jobs: dict[str, DownloadJob] = {}
        self.cancelled_ids: set[str] = set()
        self.settings: Settings | None = None
        self.username: str | None = None
        self.last_error = ""
        self.started_at = ""
        self._lock = asyncio.Lock()
        self._album_lock = asyncio.Lock()
        self.albums: dict[str, AlbumBatch] = {}
        self.controls = RuntimeControls()
        self._stopping = False

    @property
    def running(self) -> bool:
        return bool(self.client and self.client.is_connected())

    def state(self) -> dict[str, Any]:
        active = self.history.find(self.active_job.record_id) if self.active_job else None
        return {
            "running": self.running,
            "username": self.username,
            "last_error": self.last_error,
            "started_at": self.started_at,
            "task_done": bool(self.task and self.task.done()),
            "queue_size": self.queue.qsize() if self.queue else 0,
            "queue_maxsize": self.queue.maxsize if self.queue else 0,
            "active": active,
            "controls": self.controls.state(),
        }

    def _is_allowed(self, sender_id: int | None, settings: Settings) -> bool:
        permitted = set(settings.allowed_user_ids or []) | set(settings.admin_user_ids or [])
        return bool(sender_id and sender_id in permitted)

    def _is_admin(self, sender_id: int | None, settings: Settings) -> bool:
        admin_ids = settings.admin_user_ids or settings.allowed_user_ids or []
        return bool(sender_id and sender_id in admin_ids)

    async def _reply_unauthorized(self, event, settings: Settings) -> None:
        sender_id = getattr(event, "sender_id", None)
        if not (settings.allowed_user_ids or []):
            await event.reply(
                "未配置允许使用的 Telegram 用户 ID。\n"
                f"你的用户 ID 是：`{sender_id}`\n"
                "请在网页控制面板添加后再使用。"
            )
            return
        await event.reply(f"你没有权限使用这个 bot。\n你的 Telegram 用户 ID：`{sender_id}`")
        logger.warning("已拒绝未授权 Telegram 用户：%s", sender_id)

    async def _require_admin(self, event, settings: Settings) -> bool:
        if self._is_admin(getattr(event, "sender_id", None), settings):
            return True
        await event.reply("该命令仅允许管理员使用。")
        return False

    async def _handle_command(self, event, settings: Settings) -> bool:
        text = (event.message.raw_text or "").strip()
        if not text.startswith("/"):
            return False
        sender_id = getattr(event, "sender_id", None)
        command = text.split()[0].split("@", 1)[0].lower()

        if command == "/whoami":
            await event.reply(f"你的 Telegram 用户 ID：`{sender_id}`")
            return True
        if not self._is_allowed(sender_id, settings):
            await self._reply_unauthorized(event, settings)
            return True

        if command == "/help":
            await event.reply(
                "可用命令：\n"
                "/status - 当前下载、速度、ETA、队列和控制状态\n"
                "/queue [n] - 查看下载队列\n"
                "/history [n] - 查看最近记录\n"
                "/failed - 查看失败和中断记录\n"
                "/storage - 查看磁盘空间\n"
                "/whoami - 查看你的 Telegram 用户 ID\n"
                "/version - 查看版本和运行时间\n"
                "/ping - 检查响应延迟\n\n"
                "管理员命令：\n"
                "/cancel current|all|任务ID\n"
                "/pause [30m|2h] - 不带时间则无限期暂停\n"
                "/resume - 恢复下载\n"
                "/limit x|off - 设置或取消 MB/s 限速\n"
                "/retry 任务ID|failed - 重试失败任务"
            )
            return True

        if command == "/status":
            await event.reply(self.status_text())
            return True
        if command == "/queue":
            parts = text.split()
            count = min(20, max(1, int(parts[1]))) if len(parts) > 1 and parts[1].isdigit() else 10
            await event.reply(self.queue_text(count))
            return True
        if command == "/history":
            parts = text.split()
            count = min(20, max(1, int(parts[1]))) if len(parts) > 1 and parts[1].isdigit() else 10
            await event.reply(self.history_text(count))
            return True
        if command == "/failed":
            await event.reply(self.failed_text())
            return True
        if command == "/storage":
            await event.reply(self.storage_text(settings))
            return True
        if command == "/version":
            uptime = "-"
            if self.started_at:
                uptime = self.started_at
            await event.reply(f"版本：`v{__version__}`\n启动时间：`{uptime}`")
            return True
        if command == "/ping":
            started = time.monotonic()
            message = await event.reply("Pong")
            elapsed = int((time.monotonic() - started) * 1000)
            await self.safe_edit(message, f"Pong · {elapsed} ms")
            return True

        if command in {"/limit", "/delay", "/pause", "/resume", "/cancel", "/retry"}:
            if not await self._require_admin(event, settings):
                return True

        if command == "/limit":
            if re.fullmatch(r"/limit(?:@\w+)?\s+off", text, re.IGNORECASE):
                self.controls.clear_limit()
                await event.reply("已取消下载限速。")
                return True
            value = parse_limit_mb(text)
            if value is None:
                await event.reply("用法：`/limit 2` 或 `/limit off`")
                return True
            applied = self.controls.set_limit_mb(value)
            await event.reply(f"已设置下载限速：{applied:.1f} MB/s；进度消息和面板会显示限速。")
            return True

        if command == "/delay":
            value = parse_delay_hours(text)
            if value is None:
                await event.reply("用法：`/delay 2`，最长 12 小时；推荐使用 `/pause 2h`。")
                return True
            self.controls.set_delay_hours(value)
            await event.reply(f"已暂停下载 {value:.2g} 小时，到时间自动继续。")
            return True

        if command == "/pause":
            parts = text.split(maxsplit=1)
            if len(parts) == 1:
                self.controls.pause(None)
                await event.reply("已无限期暂停下载，使用 /resume 恢复。")
                return True
            seconds = parse_pause_seconds(parts[1])
            if seconds is None:
                await event.reply("用法：`/pause`、`/pause 30m` 或 `/pause 2h`，最长 12 小时。")
                return True
            self.controls.pause(seconds)
            await event.reply(f"已暂停下载：{format_duration(seconds)}。")
            return True

        if command == "/resume":
            self.controls.resume()
            await event.reply("下载已恢复。")
            return True

        if command == "/cancel":
            parts = text.split(maxsplit=1)
            if len(parts) == 1:
                await event.reply("用法：`/cancel current`、`/cancel all` 或 `/cancel 任务ID`")
                return True
            cancelled = await self.cancel(parts[1].strip())
            await event.reply(f"已取消 {cancelled} 个任务。" if cancelled else "没有找到可取消的任务。")
            return True

        if command == "/retry":
            parts = text.split(maxsplit=1)
            if len(parts) == 1:
                await event.reply("用法：`/retry 任务ID` 或 `/retry failed`")
                return True
            count = await self.retry(parts[1].strip())
            await event.reply(f"已重新加入队列：{count} 个任务。" if count else "没有找到可重试的任务。")
            return True
        return False

    async def start(self, settings: Settings) -> None:
        async with self._lock:
            if self.running:
                return
            if not settings.ready:
                raise RuntimeError("启动 bot 前必须填写 API_ID、API_HASH 和 BOT_TOKEN")
            settings.validate()
            settings.ensure_dirs()
            self._stopping = False
            self.history.flush_interval = settings.history_flush_interval_seconds
            session_path = settings.session_dir / settings.session_name
            client = TelegramClient(str(session_path), settings.api_id, settings.api_hash)

            @client.on(events.NewMessage(incoming=True))
            async def on_message(event):
                await self._handle_media(event, settings)

            try:
                await client.start(bot_token=settings.bot_token)
                me = await client.get_me()
                await self._register_commands(client)
            except Exception:
                await client.disconnect()
                raise

            self.client = client
            self.settings = settings
            self.username = getattr(me, "username", None)
            self.last_error = ""
            self.started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            self.queue = asyncio.Queue(maxsize=settings.queue_maxsize)
            self.worker_task = asyncio.create_task(self._worker(), name="download-worker")
            self.task = asyncio.create_task(self._run_until_disconnected(client), name="telegram-client")
            logger.info("Bot 已启动：@%s，队列上限 %s", self.username, settings.queue_maxsize)

    async def _register_commands(self, client: TelegramClient) -> None:
        commands = [
            types.BotCommand("help", "显示命令帮助"),
            types.BotCommand("status", "当前下载、速度、ETA 和队列"),
            types.BotCommand("queue", "查看下载队列"),
            types.BotCommand("history", "查看最近下载记录"),
            types.BotCommand("failed", "查看失败和中断记录"),
            types.BotCommand("storage", "查看磁盘空间"),
            types.BotCommand("cancel", "取消当前或指定任务（管理员）"),
            types.BotCommand("pause", "暂停下载（管理员）"),
            types.BotCommand("resume", "恢复下载（管理员）"),
            types.BotCommand("limit", "设置或取消限速（管理员）"),
            types.BotCommand("retry", "重试失败任务（管理员）"),
            types.BotCommand("whoami", "查看 Telegram 用户 ID"),
            types.BotCommand("version", "查看版本"),
            types.BotCommand("ping", "检查 Bot 延迟"),
        ]
        try:
            await client(
                functions.bots.SetBotCommandsRequest(
                    scope=types.BotCommandScopeDefault(),
                    lang_code="",
                    commands=commands,
                )
            )
        except RPCError:
            logger.exception("无法注册 Telegram 命令菜单，Bot 将继续运行")

    async def stop(self) -> None:
        async with self._lock:
            self._stopping = True
            if self.active_task and not self.active_task.done():
                self.active_task.cancel()
                await asyncio.gather(self.active_task, return_exceptions=True)
            if self.worker_task:
                self.worker_task.cancel()
                await asyncio.gather(self.worker_task, return_exceptions=True)
            for record_id, job in list(self.jobs.items()):
                if self.active_job and record_id == self.active_job.record_id:
                    continue
                self.history.update(record_id, status="interrupted", error="Bot 停止，任务已中断")
                self._cleanup_job_files(job)
            self.jobs.clear()
            for batch in self.albums.values():
                if batch.finalize_task:
                    batch.finalize_task.cancel()
            self.albums.clear()
            if self.client:
                await self.client.disconnect()
            if self.task:
                try:
                    await asyncio.wait_for(self.task, timeout=5)
                except asyncio.TimeoutError:
                    self.task.cancel()
                    await asyncio.gather(self.task, return_exceptions=True)
            self.history.flush()
            self.client = None
            self.task = None
            self.worker_task = None
            self.active_task = None
            self.active_job = None
            self.queue = None
            self.username = None

    async def restart(self, settings: Settings) -> None:
        await self.stop()
        await self.start(settings)

    async def _run_until_disconnected(self, client: TelegramClient) -> None:
        try:
            await client.run_until_disconnected()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            logger.exception("Bot 断开连接")

    async def _handle_media(self, event, settings: Settings) -> None:
        if await self._handle_command(event, settings):
            return
        if not self._is_allowed(getattr(event, "sender_id", None), settings):
            await self._reply_unauthorized(event, settings)
            return
        message = event.message
        if not message.media:
            await event.reply("请发送图片、视频或文件，我会自动加入下载队列。")
            return
        grouped_id = getattr(message, "grouped_id", None)
        if grouped_id:
            await self._enqueue_album_item(event, settings, str(grouped_id))
        else:
            await self.enqueue(message, settings, await event.reply("正在加入下载队列..."))

    async def enqueue(
        self,
        message,
        settings: Settings,
        status_message,
        album_key: str | None = None,
    ) -> str | None:
        if not self.queue or self.queue.full():
            await self.safe_edit(status_message, "下载队列已满，请稍后重试。")
            return None
        kind = media_kind(message)
        target_path = unique_media_path(message, settings.media_dir(kind), settings.max_filename_stem_length)
        record_id = uuid.uuid4().hex
        self.history.add(
            DownloadRecord(
                id=record_id,
                message_id=message.id,
                chat_id=getattr(message, "chat_id", None),
                file_name=target_path.name,
                path=str(target_path),
                max_retries=settings.max_auto_retries,
            )
        )
        job = DownloadJob(record_id, message, target_path, status_message, album_key)
        self.jobs[record_id] = job
        self.queue.put_nowait(job)
        position = self.queue.qsize()
        if not album_key:
            await self.safe_edit(
                status_message,
                f"已加入下载队列。\n任务：`{record_id[:8]}`\n文件：`{target_path.name}`\n队列位置：{position}",
            )
        return record_id

    async def _enqueue_album_item(self, event, settings: Settings, grouped_id: str) -> None:
        album_key = f"{getattr(event, 'chat_id', None)}:{grouped_id}"
        async with self._album_lock:
            batch = self.albums.get(album_key)
            if batch is None:
                status = await event.reply(f"收到一组媒体，正在排队...\n进度：{progress_bar(0)} 0%")
                batch = AlbumBatch(getattr(event, "chat_id", None), status)
                self.albums[album_key] = batch
            if batch.finalize_task and not batch.finalize_task.done():
                batch.finalize_task.cancel()
            batch.total += 1
            record_id = await self.enqueue(event.message, settings, batch.status_message, album_key)
            if record_id is None:
                batch.failed += 1
            await self._update_album_status(album_key, force=True)

    async def _worker(self) -> None:
        assert self.queue is not None
        while True:
            job = await self.queue.get()
            try:
                if job.record_id in self.cancelled_ids:
                    self.cancelled_ids.discard(job.record_id)
                    continue
                self.active_job = job
                self.active_task = asyncio.create_task(self._process_job(job), name=f"download-{job.record_id[:8]}")
                await self.active_task
            except asyncio.CancelledError:
                if self._stopping or (asyncio.current_task() and asyncio.current_task().cancelling()):
                    raise
            finally:
                self.jobs.pop(job.record_id, None)
                self.active_task = None
                self.active_job = None
                self.queue.task_done()

    async def _process_job(self, job: DownloadJob) -> None:
        assert self.settings is not None
        partial = job.target_path.with_name(f".{job.target_path.name}.{job.record_id[:8]}.part")
        for retry_count in range(self.settings.max_auto_retries + 1):
            reporter = ProgressReporter(self, job, self.settings)
            try:
                partial.unlink(missing_ok=True)
                self.history.update(
                    job.record_id,
                    status="downloading",
                    progress=0,
                    downloaded_bytes=0,
                    speed_bytes_per_second=0,
                    eta_seconds=None,
                    retry_count=retry_count,
                    error="",
                    speed_limit_bytes_per_second=self.controls.speed_limit_bytes_per_second,
                )
                paused_for = await self.controls.wait_if_paused(reporter._pause_message)
                reporter.paused_seconds += paused_for
                reporter.last_sample_at = time.monotonic()
                logger.info("下载任务 %s（尝试 %s/%s）到 %s", job.record_id[:8], retry_count + 1, self.settings.max_auto_retries + 1, job.target_path)
                downloaded_path = await job.message.download_media(
                    file=str(partial),
                    progress_callback=reporter.callback,
                )
                if downloaded_path is None:
                    raise RuntimeError("Telegram 没有返回文件")
                result = Path(downloaded_path)
                if not result.is_file():
                    raise RuntimeError("下载结果文件不存在")
                self.history.update(job.record_id, status="verifying", progress=100, eta_seconds=0)
                file_size = result.stat().st_size
                os.replace(result, job.target_path)
                self.history.update(
                    job.record_id,
                    status="complete",
                    progress=100,
                    downloaded_bytes=file_size,
                    total_bytes=file_size,
                    size_bytes=file_size,
                    speed_bytes_per_second=round(reporter.speed, 2),
                    eta_seconds=0,
                    retry_count=retry_count,
                    error="",
                )
                if job.album_key:
                    await self._album_terminal(job.album_key, job.record_id, "complete")
                elif job.status_message:
                    limit_line = ""
                    if self.controls.speed_limit_bytes_per_second:
                        limit_line = f"\n限速：{self.controls.limit_text()}"
                    await self.safe_edit(
                        job.status_message,
                        "下载完成。\n"
                        f"任务：`{job.record_id[:8]}`\n"
                        f"文件：`{job.target_path.name}`\n"
                        f"大小：{human_size(file_size)}\n"
                        f"重试：{retry_count}/{self.settings.max_auto_retries}{limit_line}",
                    )
                return
            except asyncio.CancelledError:
                status = "cancelled" if job.record_id in self.cancelled_ids else "interrupted"
                error = "用户取消任务" if status == "cancelled" else "Bot 停止或任务被中断"
                self.history.update(job.record_id, status=status, error=error, speed_bytes_per_second=0, eta_seconds=None)
                partial.unlink(missing_ok=True)
                job.target_path.unlink(missing_ok=True)
                if job.album_key:
                    await self._album_terminal(job.album_key, job.record_id, status)
                elif job.status_message:
                    await self.safe_edit(job.status_message, "下载已取消。" if status == "cancelled" else "下载已中断。")
                self.cancelled_ids.discard(job.record_id)
                raise
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                logger.exception("下载任务 %s 失败", job.record_id[:8])
                if retry_count < self.settings.max_auto_retries:
                    delay = self.retry_delay(retry_count)
                    self.history.update(
                        job.record_id,
                        status="retrying",
                        retry_count=retry_count + 1,
                        error=error,
                        speed_bytes_per_second=0,
                        eta_seconds=delay,
                    )
                    if job.status_message and not job.album_key:
                        await self.safe_edit(
                            job.status_message,
                            f"下载失败，{delay} 秒后自动重试。\n"
                            f"重试：{retry_count + 1}/{self.settings.max_auto_retries}\n"
                            f"错误：`{error}`",
                        )
                    if delay:
                        await asyncio.sleep(delay)
                    continue
                self.history.update(
                    job.record_id,
                    status="failed",
                    error=error,
                    retry_count=retry_count,
                    speed_bytes_per_second=0,
                    eta_seconds=None,
                )
                partial.unlink(missing_ok=True)
                job.target_path.unlink(missing_ok=True)
                if job.album_key:
                    await self._album_terminal(job.album_key, job.record_id, "failed")
                elif job.status_message:
                    await self.safe_edit(
                        job.status_message,
                        f"下载失败，已自动重试 {self.settings.max_auto_retries} 次。\n"
                        f"任务：`{job.record_id[:8]}`\n错误：`{error}`\n"
                        f"可使用 `/retry {job.record_id[:8]}` 重试。",
                    )
                return

    def retry_delay(self, retry_count: int) -> int:
        return min(30, 2 ** (retry_count + 1))

    def _cleanup_job_files(self, job: DownloadJob) -> None:
        partial = job.target_path.with_name(f".{job.target_path.name}.{job.record_id[:8]}.part")
        partial.unlink(missing_ok=True)
        try:
            if job.target_path.exists() and job.target_path.stat().st_size == 0:
                job.target_path.unlink(missing_ok=True)
        except OSError:
            pass

    async def safe_edit(self, status_message, text: str) -> None:
        try:
            await status_message.edit(text)
        except MessageNotModifiedError:
            return
        except FloodWaitError as exc:
            logger.warning("编辑状态消息触发 Telegram 限流：%s 秒", exc.seconds)
            await asyncio.sleep(exc.seconds)
        except RPCError:
            logger.exception("无法编辑状态消息")

    async def update_album_progress(self, album_key: str, record_id: str, downloaded: int, total: int) -> None:
        batch = self.albums.get(album_key)
        if not batch:
            return
        batch.item_progress[record_id] = (downloaded, total or 0)
        await self._update_album_status(album_key)

    async def _update_album_status(self, album_key: str, force: bool = False) -> None:
        batch = self.albums.get(album_key)
        if not batch:
            return
        now = time.monotonic()
        if not force and now - batch.last_edit_at < 3:
            return
        async with batch.lock:
            total_bytes = sum(total for _, total in batch.item_progress.values())
            downloaded_bytes = sum(downloaded for downloaded, _ in batch.item_progress.values())
            done = batch.completed + batch.failed + batch.cancelled
            percent = min(100, int(downloaded_bytes * 100 / total_bytes)) if total_bytes else (
                int(done * 100 / batch.total) if batch.total else 0
            )
            limit_line = f"\n限速：{self.controls.limit_text()}" if self.controls.speed_limit_bytes_per_second else ""
            text = (
                "批量下载中...\n"
                f"进度：{progress_bar(percent)} {percent}%\n"
                f"大小：{human_size(downloaded_bytes)} / {human_size(total_bytes)}\n"
                f"文件：{done}/{batch.total} 个{limit_line}"
            )
            await self.safe_edit(batch.status_message, text)
            batch.last_edit_at = now

    async def _album_terminal(self, album_key: str, record_id: str, status: str) -> None:
        batch = self.albums.get(album_key)
        if not batch:
            return
        if status == "complete":
            batch.completed += 1
        elif status == "cancelled":
            batch.cancelled += 1
        else:
            batch.failed += 1
        await self._update_album_status(album_key, force=True)
        if batch.completed + batch.failed + batch.cancelled >= batch.total:
            if batch.finalize_task and not batch.finalize_task.done():
                batch.finalize_task.cancel()
            batch.finalize_task = asyncio.create_task(self._finalize_album_later(album_key))

    async def _finalize_album_later(self, album_key: str) -> None:
        try:
            await asyncio.sleep(2)
            batch = self.albums.get(album_key)
            if not batch or batch.completed + batch.failed + batch.cancelled < batch.total:
                return
            text = (
                "批量下载结束。\n"
                f"成功：{batch.completed}，失败：{batch.failed}，取消：{batch.cancelled}\n"
                f"总数：{batch.total} 个\n进度：{progress_bar(100)} 100%"
            )
            await self.safe_edit(batch.status_message, text)
            self.albums.pop(album_key, None)
        except asyncio.CancelledError:
            return

    async def cancel(self, target: str) -> int:
        target = target.lower()
        ids: list[str] = []
        if target == "current":
            if self.active_job:
                ids = [self.active_job.record_id]
        elif target == "all":
            ids = list(self.jobs)
        else:
            matches = [record_id for record_id in self.jobs if record_id.startswith(target)]
            if len(matches) == 1:
                ids = matches
        for record_id in ids:
            self.cancelled_ids.add(record_id)
            job = self.jobs.get(record_id)
            if self.active_job and record_id == self.active_job.record_id and self.active_task:
                self.active_task.cancel()
            elif job:
                self.history.update(record_id, status="cancelled", error="用户取消排队任务")
                self._cleanup_job_files(job)
                if job.album_key:
                    await self._album_terminal(job.album_key, record_id, "cancelled")
        return len(ids)

    async def retry(self, target: str) -> int:
        if not self.client or not self.queue:
            return 0
        if target.lower() == "failed":
            records = self.history.list_statuses(RETRYABLE_STATUSES, limit=10)
        else:
            record = self.history.find(target)
            records = [record] if record and record["status"] in RETRYABLE_STATUSES else []
        queued = 0
        for record in records:
            if not record or self.queue.full() or record["id"] in self.jobs:
                continue
            try:
                message = await self.client.get_messages(record["chat_id"], ids=record["message_id"])
            except RPCError:
                logger.exception("无法获取原 Telegram 消息：%s", record["id"][:8])
                continue
            if not message or not getattr(message, "media", None):
                self.history.update(record["id"], error="原 Telegram 消息已不可用，无法重试")
                continue
            status_message = await self.client.send_message(
                record["chat_id"],
                f"正在重新加入下载队列...\n任务：`{record['id'][:8]}`",
            )
            target_path = Path(record["path"])
            target_path.parent.mkdir(parents=True, exist_ok=True)
            if not target_path.exists():
                target_path.touch(mode=0o600)
            self.history.update(
                record["id"],
                status="queued",
                progress=0,
                downloaded_bytes=0,
                size_bytes=0,
                speed_bytes_per_second=0,
                eta_seconds=None,
                retry_count=0,
                error="",
            )
            job = DownloadJob(record["id"], message, target_path, status_message)
            self.jobs[record["id"]] = job
            self.queue.put_nowait(job)
            queued += 1
        return queued

    def status_text(self) -> str:
        controls = self.controls.state()
        lines = [
            f"Bot：{'运行中' if self.running else '已停止'}",
            f"队列：{self.queue.qsize() if self.queue else 0}/{self.queue.maxsize if self.queue else 0}",
            f"限速：{controls['speed_limit_text']}",
            f"暂停：{'是' if controls['paused'] else '否'}",
        ]
        if controls["paused"]:
            lines.append(f"暂停剩余：{format_duration(controls['pause_remaining_seconds'])}")
        if self.active_job:
            record = self.history.find(self.active_job.record_id)
            if record:
                lines.extend(
                    [
                        f"当前任务：`{record['id'][:8]}`",
                        f"文件：`{record['file_name']}`",
                        f"进度：{record['progress']}%",
                        f"速度：{human_size(record['speed_bytes_per_second'])}/s",
                        f"预计剩余：{format_duration(record['eta_seconds'])}",
                        f"自动重试：{record['retry_count']}/{record['max_retries']}",
                    ]
                )
        else:
            lines.append("当前任务：无")
        return "\n".join(lines)

    def queue_text(self, count: int = 10) -> str:
        if not self.jobs:
            return "下载队列为空。"
        lines = ["下载队列："]
        for index, job in enumerate(list(self.jobs.values())[:count], 1):
            record = self.history.find(job.record_id)
            if record:
                marker = "当前" if self.active_job and job.record_id == self.active_job.record_id else str(index)
                lines.append(f"{marker}. `{job.record_id[:8]}` {record['status']} {record['progress']}% · {record['file_name']}")
        return "\n".join(lines)

    def history_text(self, count: int = 10) -> str:
        records = self.history.list(count)
        if not records:
            return "暂无下载记录。"
        lines = ["最近下载："]
        for record in records:
            lines.append(f"`{record['id'][:8]}` {record['status']} {record['progress']}% · {record['file_name']}")
        return "\n".join(lines)

    def failed_text(self) -> str:
        records = self.history.list_statuses(RETRYABLE_STATUSES, 10)
        if not records:
            return "没有失败、中断或取消的记录。"
        lines = ["可重试记录："]
        for record in records:
            lines.append(f"`{record['id'][:8]}` {record['status']} · {record['file_name']}\n  {record['error'] or '-'}")
        return "\n".join(lines)

    def storage_text(self, settings: Settings) -> str:
        usage = shutil.disk_usage(settings.download_dir)
        total_files = 0
        total_size = 0
        for directory in (settings.image_download_dir, settings.video_download_dir, settings.file_download_dir):
            try:
                for path in directory.iterdir():
                    if path.is_file() and not path.name.startswith("."):
                        total_files += 1
                        total_size += path.stat().st_size
            except OSError:
                continue
        return (
            f"已保存：{total_files} 个文件，{human_size(total_size)}\n"
            f"磁盘可用：{human_size(usage.free)} / {human_size(usage.total)}"
        )
