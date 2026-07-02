from __future__ import annotations

import logging
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from threading import Lock
from typing import Any


@dataclass
class LogEntry:
    time: str
    level: str
    logger: str
    message: str


class MemoryLogHandler(logging.Handler):
    def __init__(self, limit: int = 300):
        super().__init__()
        self.records: deque[LogEntry] = deque(maxlen=limit)
        self._records_lock = Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
        except Exception:
            message = record.getMessage()
        entry = LogEntry(
            time=datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="seconds"),
            level=record.levelname,
            logger=record.name,
            message=message,
        )
        with self._records_lock:
            self.records.append(entry)

    def list(self) -> list[dict[str, Any]]:
        with self._records_lock:
            return [asdict(record) for record in reversed(self.records)]
