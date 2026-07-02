from __future__ import annotations

import json
import os
import hashlib
import hmac
import secrets
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


def _optional_int(value: str | None) -> int | None:
    if value is None or not value.strip():
        return None
    try:
        return int(value)
    except ValueError as exc:
        raise RuntimeError("API_ID must be an integer") from exc


def parse_user_ids(value: Any) -> list[int]:
    if value is None:
        return []
    if isinstance(value, list):
        parts = value
    else:
        parts = str(value).replace("\n", ",").replace(" ", ",").split(",")

    user_ids: list[int] = []
    for part in parts:
        if part is None or str(part).strip() == "":
            continue
        try:
            user_id = int(str(part).strip())
        except ValueError as exc:
            raise RuntimeError("Allowed user IDs must be integers") from exc
        if user_id not in user_ids:
            user_ids.append(user_id)
    return user_ids


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    iterations = 260_000
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${digest.hex()}"


def verify_password(password: str, password_hash: str) -> bool:
    try:
        algorithm, iterations, salt_hex, digest_hex = password_hash.split("$", 3)
    except ValueError:
        return False
    if algorithm != "pbkdf2_sha256":
        return False
    expected = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        bytes.fromhex(salt_hex),
        int(iterations),
    )
    return hmac.compare_digest(expected.hex(), digest_hex)


@dataclass
class Settings:
    api_id: int | None = None
    api_hash: str = ""
    bot_token: str = ""
    download_dir: Path = Path("/downloads")
    image_download_dir: Path = Path("/downloads/images")
    video_download_dir: Path = Path("/downloads/videos")
    file_download_dir: Path = Path("/downloads/files")
    session_dir: Path = Path("/sessions")
    config_dir: Path = Path("/config")
    allowed_user_ids: list[int] | None = None
    admin_password_hash: str = ""
    session_name: str = "media_downloader_bot"
    progress_interval_seconds: float = 3.0
    progress_percent_step: int = 10
    max_filename_stem_length: int = 120
    log_level: str = "INFO"

    @property
    def ready(self) -> bool:
        return bool(self.api_id and self.api_hash.strip() and self.bot_token.strip())

    @property
    def config_path(self) -> Path:
        return self.config_dir / "settings.json"

    def public_dict(self) -> dict[str, Any]:
        return {
            "api_id": self.api_id,
            "api_hash_set": bool(self.api_hash),
            "bot_token_set": bool(self.bot_token),
            "download_dir": str(self.download_dir),
            "image_download_dir": str(self.image_download_dir),
            "video_download_dir": str(self.video_download_dir),
            "file_download_dir": str(self.file_download_dir),
            "session_dir": str(self.session_dir),
            "allowed_user_ids": self.allowed_user_ids or [],
            "admin_password_set": bool(self.admin_password_hash),
            "session_name": self.session_name,
            "progress_interval_seconds": self.progress_interval_seconds,
            "progress_percent_step": self.progress_percent_step,
            "max_filename_stem_length": self.max_filename_stem_length,
            "ready": self.ready,
        }

    def to_json_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for key in (
            "download_dir",
            "image_download_dir",
            "video_download_dir",
            "file_download_dir",
            "session_dir",
            "config_dir",
        ):
            data[key] = str(data[key])
        return data

    def media_dir(self, kind: str) -> Path:
        if kind in {"photo", "image"}:
            return self.image_download_dir
        if kind == "video":
            return self.video_download_dir
        return self.file_download_dir

    def ensure_dirs(self) -> None:
        for path in (
            self.download_dir,
            self.image_download_dir,
            self.video_download_dir,
            self.file_download_dir,
            self.session_dir,
            self.config_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_json_dict(cls, data: dict[str, Any]) -> "Settings":
        download_dir = Path(data.get("download_dir") or "/downloads").resolve()
        return cls(
            api_id=_optional_int(str(data.get("api_id"))) if data.get("api_id") is not None else None,
            api_hash=str(data.get("api_hash") or ""),
            bot_token=str(data.get("bot_token") or ""),
            download_dir=download_dir,
            image_download_dir=Path(data.get("image_download_dir") or download_dir / "images").resolve(),
            video_download_dir=Path(data.get("video_download_dir") or download_dir / "videos").resolve(),
            file_download_dir=Path(data.get("file_download_dir") or download_dir / "files").resolve(),
            session_dir=Path(data.get("session_dir") or "/sessions").resolve(),
            config_dir=Path(data.get("config_dir") or "/config").resolve(),
            allowed_user_ids=parse_user_ids(data.get("allowed_user_ids")),
            admin_password_hash=str(data.get("admin_password_hash") or ""),
            session_name=str(data.get("session_name") or "media_downloader_bot"),
            progress_interval_seconds=float(data.get("progress_interval_seconds") or 3),
            progress_percent_step=int(data.get("progress_percent_step") or 10),
            max_filename_stem_length=int(data.get("max_filename_stem_length") or 120),
            log_level=str(data.get("log_level") or "INFO"),
        )

    @classmethod
    def from_env(cls) -> "Settings":
        download_dir = Path(os.getenv("DOWNLOAD_DIR", "/downloads")).resolve()
        return cls(
            api_id=_optional_int(os.getenv("API_ID")),
            api_hash=os.getenv("API_HASH", "").strip(),
            bot_token=os.getenv("BOT_TOKEN", "").strip(),
            download_dir=download_dir,
            image_download_dir=Path(os.getenv("IMAGE_DOWNLOAD_DIR", download_dir / "images")).resolve(),
            video_download_dir=Path(os.getenv("VIDEO_DOWNLOAD_DIR", download_dir / "videos")).resolve(),
            file_download_dir=Path(os.getenv("FILE_DOWNLOAD_DIR", download_dir / "files")).resolve(),
            session_dir=Path(os.getenv("SESSION_DIR", "/sessions")).resolve(),
            config_dir=Path(os.getenv("CONFIG_DIR", "/config")).resolve(),
            allowed_user_ids=parse_user_ids(os.getenv("ALLOWED_USER_IDS")),
            admin_password_hash=hash_password(os.getenv("ADMIN_PASSWORD", ""))
            if os.getenv("ADMIN_PASSWORD")
            else "",
            session_name=os.getenv("SESSION_NAME", "media_downloader_bot").strip()
            or "media_downloader_bot",
            progress_interval_seconds=float(os.getenv("PROGRESS_INTERVAL_SECONDS", "3")),
            progress_percent_step=int(os.getenv("PROGRESS_PERCENT_STEP", "10")),
            max_filename_stem_length=int(os.getenv("MAX_FILENAME_STEM_LENGTH", "120")),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        )


class SettingsStore:
    def __init__(self, settings: Settings):
        self._settings = settings
        self._settings.config_dir.mkdir(parents=True, exist_ok=True)
        self._load_saved()

    @property
    def settings(self) -> Settings:
        return self._settings

    def _load_saved(self) -> None:
        path = self._settings.config_path
        if not path.exists():
            return
        with path.open("r", encoding="utf-8") as handle:
            saved = Settings.from_json_dict(json.load(handle))

        env_seed = self._settings
        if not saved.api_id:
            saved.api_id = env_seed.api_id
        if not saved.api_hash:
            saved.api_hash = env_seed.api_hash
        if not saved.bot_token:
            saved.bot_token = env_seed.bot_token
        if not saved.allowed_user_ids:
            saved.allowed_user_ids = env_seed.allowed_user_ids
        if not saved.admin_password_hash:
            saved.admin_password_hash = env_seed.admin_password_hash
        saved.config_dir = env_seed.config_dir
        self._settings = saved

    def save(self, updates: dict[str, Any]) -> Settings:
        current = self._settings
        data = current.to_json_dict()

        for key in (
            "api_id",
            "api_hash",
            "bot_token",
            "download_dir",
            "image_download_dir",
            "video_download_dir",
            "file_download_dir",
            "session_dir",
            "allowed_user_ids",
            "session_name",
            "progress_interval_seconds",
            "progress_percent_step",
            "max_filename_stem_length",
        ):
            if key not in updates:
                continue
            value = updates[key]
            if key in {"api_hash", "bot_token"} and value == "":
                continue
            data[key] = value

        if updates.get("admin_password"):
            data["admin_password_hash"] = hash_password(str(updates["admin_password"]))

        data["config_dir"] = str(current.config_dir)
        self._settings = Settings.from_json_dict(data)
        self._settings.config_dir.mkdir(parents=True, exist_ok=True)
        with self._settings.config_path.open("w", encoding="utf-8") as handle:
            json.dump(self._settings.to_json_dict(), handle, indent=2)
        return self._settings
