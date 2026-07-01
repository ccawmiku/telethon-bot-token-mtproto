from __future__ import annotations

import asyncio
import logging
import time
import uuid
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
                "Downloading...\n"
                f"File: `{self.target_path.name}`\n"
                f"Progress: {percent}% ({human_size(downloaded)} / {human_size(total)})"
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
            logger.warning("Telegram flood wait while editing progress: %ss", exc.seconds)
            await asyncio.sleep(exc.seconds)
        except RPCError:
            logger.exception("Could not edit progress message")


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

    async def start(self, settings: Settings) -> None:
        async with self._lock:
            if self.running:
                return
            if not settings.ready:
                raise RuntimeError("API_ID, API_HASH, and BOT_TOKEN are required before starting the bot")

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
            logger.info("Bot started as @%s", self.username)

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
            logger.exception("Bot disconnected with error")

    async def _handle_media(self, event, settings: Settings) -> None:
        message = event.message
        if not message.media:
            await event.reply("Please send me a photo, video, or file to download.")
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
        status = await event.reply(f"Queued download...\nFile: `{target_path.name}`")
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
            logger.info("Downloading message %s to %s", message.id, target_path)
            downloaded_path = await message.download_media(
                file=str(target_path),
                progress_callback=reporter.callback,
            )
            if downloaded_path is None:
                self.history.update(record_id, status="failed", error="Telegram did not return a file")
                await status.edit("Download failed: Telegram did not return a file.")
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
                "Download complete.\n"
                f"File: `{final_path.name}`\n"
                f"Size: {human_size(file_size)}\n"
                f"Path: `{final_path}`"
            )
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            logger.exception("Download failed")
            self.history.update(record_id, status="failed", error=error)
            await status.edit(f"Download failed: `{error}`")
