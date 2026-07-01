from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any


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
    error: str = ""
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)


class DownloadHistory:
    def __init__(self, path: Path, limit: int = 200):
        self.path = path
        self.limit = limit
        self._lock = Lock()
        self._records: list[DownloadRecord] = []
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return
        self._records = [DownloadRecord(**item) for item in data[-self.limit :]]

    def _save(self) -> None:
        data = [asdict(record) for record in self._records[-self.limit :]]
        self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def add(self, record: DownloadRecord) -> None:
        with self._lock:
            self._records.append(record)
            self._records = self._records[-self.limit :]
            self._save()

    def update(self, record_id: str, **updates: Any) -> None:
        with self._lock:
            for record in self._records:
                if record.id != record_id:
                    continue
                for key, value in updates.items():
                    setattr(record, key, value)
                record.updated_at = now_iso()
                break
            self._save()

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return [asdict(record) for record in reversed(self._records)]

