import asyncio
from pathlib import Path

import pytest

from app.bot import BotManager, DownloadJob, RuntimeControls, parse_pause_seconds
from app.config import Settings
from app.history import DownloadHistory, DownloadRecord


class FakeStatus:
    def __init__(self):
        self.edits = []

    async def edit(self, text):
        self.edits.append(text)


class FakeMessage:
    def __init__(self, message_id=1, failures=0, payload=b"payload"):
        self.id = message_id
        self.chat_id = 123
        self.media = object()
        self.document = None
        self.failures = failures
        self.payload = payload
        self.calls = 0

    async def download_media(self, file, progress_callback):
        self.calls += 1
        if self.calls <= self.failures:
            raise OSError("temporary network error")
        Path(file).write_bytes(self.payload)
        await progress_callback(len(self.payload), len(self.payload))
        return file


class FakeEvent:
    replies = 0

    def __init__(self, message):
        self.message = message
        self.chat_id = message.chat_id

    async def reply(self, _):
        type(self).replies += 1
        await asyncio.sleep(0.01)
        return FakeStatus()


def make_settings(tmp_path, retries=3, queue_size=10):
    return Settings.from_json_dict(
        {
            "download_dir": str(tmp_path / "downloads"),
            "image_download_dir": str(tmp_path / "downloads" / "images"),
            "video_download_dir": str(tmp_path / "downloads" / "videos"),
            "file_download_dir": str(tmp_path / "downloads" / "files"),
            "session_dir": str(tmp_path / "sessions"),
            "config_dir": str(tmp_path / "config"),
            "max_auto_retries": retries,
            "queue_maxsize": queue_size,
        }
    )


def make_manager(tmp_path, retries=3):
    settings = make_settings(tmp_path, retries)
    settings.ensure_dirs()
    history = DownloadHistory(settings.config_dir / "downloads.json")
    manager = BotManager(history)
    manager.settings = settings
    manager.retry_delay = lambda _: 0
    return manager, settings, history


def test_pause_duration_parser_and_runtime_controls():
    assert parse_pause_seconds("30m") == 1800
    assert parse_pause_seconds("2h") == 7200
    assert parse_pause_seconds("15s") == 15
    assert parse_pause_seconds("bad") is None

    controls = RuntimeControls()
    controls.set_limit_mb(2)
    assert controls.state()["speed_limit_bytes_per_second"] == 2 * 1024 * 1024
    controls.clear_limit()
    assert controls.state()["speed_limit_bytes_per_second"] is None
    controls.pause(None)
    assert controls.is_paused()
    controls.resume()
    assert not controls.is_paused()


@pytest.mark.asyncio
async def test_limit_off_interrupts_an_in_progress_throttle():
    controls = RuntimeControls()
    controls.set_limit_mb(1)
    throttling = asyncio.create_task(controls.throttle(5 * 1024 * 1024))
    await asyncio.sleep(0.01)

    controls.clear_limit()

    await asyncio.wait_for(throttling, timeout=0.5)


@pytest.mark.asyncio
async def test_download_retries_then_completes(tmp_path):
    manager, settings, history = make_manager(tmp_path)
    message = FakeMessage(failures=2)
    target = settings.file_download_dir / "result.bin"
    target.touch()
    record = DownloadRecord("retry-job", 1, 123, target.name, str(target), max_retries=3)
    history.add(record)
    job = DownloadJob(record.id, message, target, FakeStatus())

    await manager._process_job(job)

    saved = history.find("retry-job")
    assert message.calls == 3
    assert saved["status"] == "complete"
    assert saved["retry_count"] == 2
    assert target.read_bytes() == b"payload"


@pytest.mark.asyncio
async def test_download_fails_after_three_automatic_retries(tmp_path):
    manager, settings, history = make_manager(tmp_path)
    message = FakeMessage(failures=10)
    target = settings.file_download_dir / "failed.bin"
    target.touch()
    history.add(DownloadRecord("failed-job", 1, 123, target.name, str(target), max_retries=3))

    await manager._process_job(DownloadJob("failed-job", message, target, FakeStatus()))

    saved = history.find("failed-job")
    assert message.calls == 4
    assert saved["status"] == "failed"
    assert saved["retry_count"] == 3


@pytest.mark.asyncio
async def test_album_creation_is_serialized(tmp_path):
    manager, settings, _ = make_manager(tmp_path)
    manager.queue = asyncio.Queue(maxsize=10)
    FakeEvent.replies = 0

    await asyncio.gather(
        manager._enqueue_album_item(FakeEvent(FakeMessage(1)), settings, "album"),
        manager._enqueue_album_item(FakeEvent(FakeMessage(2)), settings, "album"),
    )

    batch = manager.albums["123:album"]
    assert FakeEvent.replies == 1
    assert batch.total == 2
    assert manager.queue.qsize() == 2


@pytest.mark.asyncio
async def test_cancelling_current_job_does_not_stop_worker(tmp_path):
    manager, _, history = make_manager(tmp_path)
    manager.queue = asyncio.Queue(maxsize=10)
    first_started = asyncio.Event()
    second_finished = asyncio.Event()

    async def process(job):
        if job.record_id == "first":
            first_started.set()
            await asyncio.Event().wait()
        second_finished.set()

    manager._process_job = process
    for record_id in ("first", "second"):
        target = tmp_path / f"{record_id}.bin"
        target.touch()
        history.add(DownloadRecord(record_id, 1, 123, target.name, str(target)))
        job = DownloadJob(record_id, FakeMessage(), target, FakeStatus())
        manager.jobs[record_id] = job
        manager.queue.put_nowait(job)

    worker = asyncio.create_task(manager._worker())
    await first_started.wait()
    assert await manager.cancel("current") == 1
    await asyncio.wait_for(second_finished.wait(), timeout=1)
    worker.cancel()
    await asyncio.gather(worker, return_exceptions=True)

    assert second_finished.is_set()
