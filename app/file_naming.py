from __future__ import annotations

import mimetypes
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from telethon.tl.types import (
    DocumentAttributeFilename,
    Message,
    MessageMediaDocument,
    MessageMediaPhoto,
)

_SAFE_CHARS = re.compile(r"[^A-Za-z0-9._ -]+")
_SPACES = re.compile(r"\s+")


def sanitize_filename(name: str, fallback: str, max_stem_length: int) -> str:
    cleaned = _SAFE_CHARS.sub("_", name).strip(" ._-")
    cleaned = _SPACES.sub(" ", cleaned)
    if not cleaned:
        cleaned = fallback

    path = Path(cleaned)
    suffix = path.suffix[:20]
    stem = path.stem or fallback
    if len(stem) > max_stem_length:
        stem = stem[:max_stem_length].rstrip(" ._-") or fallback
    return f"{stem}{suffix}"


def media_extension(message: Message) -> str:
    media = message.media
    if isinstance(media, MessageMediaPhoto):
        return ".jpg"

    document = getattr(message, "document", None)
    mime_type = getattr(document, "mime_type", None)
    guessed = mimetypes.guess_extension(mime_type or "")
    if guessed == ".jpe":
        return ".jpg"
    return guessed or ".bin"


def original_media_name(message: Message) -> Optional[str]:
    document = getattr(message, "document", None)
    for attr in getattr(document, "attributes", []) or []:
        if isinstance(attr, DocumentAttributeFilename) and attr.file_name:
            return attr.file_name
    return None


def media_kind(message: Message) -> str:
    media = message.media
    if isinstance(media, MessageMediaPhoto):
        return "photo"
    if isinstance(media, MessageMediaDocument):
        mime_type = getattr(getattr(message, "document", None), "mime_type", "") or ""
        if mime_type.startswith("video/"):
            return "video"
        if mime_type.startswith("image/"):
            return "image"
        return "file"
    return "media"


def unique_media_path(message: Message, download_dir: Path, max_stem_length: int) -> Path:
    download_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    message_id = getattr(message, "id", "unknown")
    fallback_name = f"{media_kind(message)}_{timestamp}_{message_id}{media_extension(message)}"
    candidate_name = sanitize_filename(
        original_media_name(message) or fallback_name,
        fallback=fallback_name,
        max_stem_length=max_stem_length,
    )
    candidate = download_dir / candidate_name
    suffix = candidate.suffix
    stem = candidate.stem

    for attempt in range(101):
        target = candidate if attempt == 0 else (
            download_dir / f"{stem}_{timestamp}_{message_id}_{uuid.uuid4().hex[:8]}{suffix}"
        )
        try:
            descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            continue
        else:
            os.close(descriptor)
            return target

    target = download_dir / f"{stem}_{timestamp}_{message_id}_{uuid.uuid4().hex}{suffix}"
    descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
    return target

