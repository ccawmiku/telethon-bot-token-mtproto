from __future__ import annotations

import json
import os
import secrets
import time
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any


ACTIVE_STATUSES = {"queued", "downloading", "paused", "retrying", "verifying"}
RETRYABLE_STATUSES = {"failed", "interrupted", "cancelled"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class DownloadRecord:
    id: str
    message_id: int
    chat_id: int | None
    file_name: str
    path: str
    status: str = "queued"
    progress: int = 0
    downloaded_bytes: int = 0
    total_bytes: int = 0
    size_bytes: int = 0
    speed_bytes_per_second: float = 0.0
    eta_seconds: int | None = None
    speed_limit_bytes_per_second: int | None = None
    retry_count: int = 0
    max_retries: int = 3
    error: str = ""
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)


class DownloadHistory:
    def __init__(self, path: Path, limit: int = 200, flush_interval: float = 2.0):
        self.path = path
        self.limit = limit
        self.flush_interval = max(0.5, flush_interval)
        self._lock = Lock()
        self._records: list[DownloadRecord] = []
        self._dirty = False
        self._last_saved = 0.0
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, list):
                raise ValueError("history root must be a list")
            allowed_fields = {item.name for item in fields(DownloadRecord)}
            records: list[DownloadRecord] = []
            for item in data[-self.limit :]:
                if not isinstance(item, dict):
                    continue
                filtered = {key: value for key, value in item.items() if key in allowed_fields}
                records.append(DownloadRecord(**filtered))
            self._records = records
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            backup = self.path.with_suffix(".json.corrupt")
            try:
                os.replace(self.path, backup)
            except OSError:
                pass
            self._records = []
            self._dirty = True
            self._save_locked()
            self._records.append(
                DownloadRecord(
                    id=f"recovery-{secrets.token_hex(6)}",
                    message_id=0,
                    chat_id=None,
                    file_name=backup.name,
                    path=str(backup),
                    status="failed",
                    error=f"历史文件损坏，已备份：{type(exc).__name__}: {exc}",
                )
            )
            self._dirty = True
            self._save_locked()

    def _save_locked(self) -> None:
        data = [asdict(record) for record in self._records[-self.limit :]]
        temporary = self.path.with_name(f".{self.path.name}.{secrets.token_hex(6)}.tmp")
        try:
            temporary.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            try:
                temporary.chmod(0o600)
            except OSError:
                pass
            os.replace(temporary, self.path)
            try:
                self.path.chmod(0o600)
            except OSError:
                pass
        finally:
            temporary.unlink(missing_ok=True)
        self._dirty = False
        self._last_saved = time.monotonic()

    def _maybe_save_locked(self, force: bool) -> None:
        if not self._dirty:
            return
        if force or time.monotonic() - self._last_saved >= self.flush_interval:
            self._save_locked()

    def flush(self) -> None:
        with self._lock:
            self._maybe_save_locked(force=True)

    def add(self, record: DownloadRecord) -> None:
        with self._lock:
            self._records.append(record)
            self._records = self._records[-self.limit :]
            self._dirty = True
            self._maybe_save_locked(force=True)

    def update(self, record_id: str, *, persist: bool = True, **updates: Any) -> bool:
        with self._lock:
            found = False
            for record in self._records:
                if record.id != record_id:
                    continue
                for key, value in updates.items():
                    if hasattr(record, key):
                        setattr(record, key, value)
                record.updated_at = now_iso()
                found = True
                self._dirty = True
                break
            self._maybe_save_locked(force=persist)
            return found

    def find(self, record_id_or_prefix: str) -> dict[str, Any] | None:
        with self._lock:
            matches = [record for record in self._records if record.id.startswith(record_id_or_prefix)]
            if len(matches) != 1:
                return None
            return asdict(matches[0])

    def list(self, limit: int | None = None) -> list[dict[str, Any]]:
        with self._lock:
            records = list(reversed(self._records))
            if limit is not None:
                records = records[: max(0, limit)]
            return [asdict(record) for record in records]

    def list_statuses(self, statuses: set[str], limit: int | None = 10) -> list[dict[str, Any]]:
        with self._lock:
            records = [record for record in reversed(self._records) if record.status in statuses]
            if limit is not None:
                records = records[: max(0, limit)]
            return [asdict(record) for record in records]

    def remove_statuses(self, statuses: set[str]) -> int:
        with self._lock:
            before = len(self._records)
            self._records = [record for record in self._records if record.status not in statuses]
            removed = before - len(self._records)
            if removed:
                self._dirty = True
                self._maybe_save_locked(force=True)
            return removed

    def recover_incomplete(self) -> dict[str, int]:
        recovered = 0
        interrupted = 0
        with self._lock:
            for record in self._records:
                if record.status not in ACTIVE_STATUSES:
                    continue
                path = Path(record.path)
                try:
                    actual_size = path.stat().st_size if path.is_file() else 0
                except OSError:
                    actual_size = 0
                if record.total_bytes > 0 and actual_size == record.total_bytes:
                    record.status = "complete"
                    record.progress = 100
                    record.downloaded_bytes = actual_size
                    record.size_bytes = actual_size
                    record.error = ""
                    recovered += 1
                else:
                    record.status = "interrupted"
                    record.downloaded_bytes = actual_size
                    record.size_bytes = actual_size
                    record.error = "服务重启或任务中断，可使用 /retry 重试"
                    partial = path.with_name(f".{path.name}.{record.id[:8]}.part")
                    partial.unlink(missing_ok=True)
                    if actual_size == 0:
                        path.unlink(missing_ok=True)
                    interrupted += 1
                record.speed_bytes_per_second = 0
                record.eta_seconds = None
                record.updated_at = now_iso()
                self._dirty = True
            self._maybe_save_locked(force=True)
        return {"recovered": recovered, "interrupted": interrupted}
