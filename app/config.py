from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
from copy import deepcopy
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


SESSION_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")


def _optional_int(value: str | None) -> int | None:
    if value is None or not value.strip():
        return None
    try:
        result = int(value)
    except ValueError as exc:
        raise RuntimeError("API_ID must be an integer") from exc
    if result <= 0:
        raise RuntimeError("API_ID must be greater than zero")
    return result


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


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
        if user_id <= 0:
            raise RuntimeError("User IDs must be greater than zero")
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
        algorithm, iterations_text, salt_hex, digest_hex = password_hash.split("$", 3)
        iterations = int(iterations_text)
        if algorithm != "pbkdf2_sha256" or not 100_000 <= iterations <= 2_000_000:
            return False
        expected = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            bytes.fromhex(salt_hex),
            iterations,
        )
    except (TypeError, ValueError):
        return False
    return hmac.compare_digest(expected.hex(), digest_hex)


def _atomic_json_write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    try:
        temporary.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        try:
            temporary.chmod(0o600)
        except OSError:
            pass
        os.replace(temporary, path)
        try:
            path.chmod(0o600)
        except OSError:
            pass
    finally:
        temporary.unlink(missing_ok=True)


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
    admin_user_ids: list[int] | None = None
    admin_password_hash: str = ""
    admin_password_from_env: bool = False
    session_name: str = "media_downloader_bot"
    progress_interval_seconds: float = 3.0
    progress_percent_step: int = 5
    max_filename_stem_length: int = 120
    max_auto_retries: int = 3
    queue_maxsize: int = 100
    history_flush_interval_seconds: float = 2.0
    log_level: str = "INFO"
    cookie_secure: bool = False

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
            "admin_user_ids": self.admin_user_ids or [],
            "admin_password_set": bool(self.admin_password_hash),
            "session_name": self.session_name,
            "progress_interval_seconds": self.progress_interval_seconds,
            "progress_percent_step": self.progress_percent_step,
            "max_filename_stem_length": self.max_filename_stem_length,
            "max_auto_retries": self.max_auto_retries,
            "queue_maxsize": self.queue_maxsize,
            "history_flush_interval_seconds": self.history_flush_interval_seconds,
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
        data.pop("admin_password_from_env", None)
        data.pop("cookie_secure", None)
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

    def validate(self) -> None:
        if self.api_id is not None and self.api_id <= 0:
            raise RuntimeError("API_ID 必须大于 0")
        if not SESSION_NAME_RE.fullmatch(self.session_name):
            raise RuntimeError("Session 名称只能包含字母、数字、点、下划线和连字符")
        if not 0.5 <= self.progress_interval_seconds <= 60:
            raise RuntimeError("进度更新间隔必须在 0.5 到 60 秒之间")
        if not 1 <= self.progress_percent_step <= 100:
            raise RuntimeError("进度百分比步长必须在 1 到 100 之间")
        if not 16 <= self.max_filename_stem_length <= 200:
            raise RuntimeError("文件名长度必须在 16 到 200 之间")
        if not 0 <= self.max_auto_retries <= 10:
            raise RuntimeError("自动重试次数必须在 0 到 10 之间")
        if not 1 <= self.queue_maxsize <= 1000:
            raise RuntimeError("队列长度必须在 1 到 1000 之间")
        if not 0.5 <= self.history_flush_interval_seconds <= 60:
            raise RuntimeError("历史写入间隔必须在 0.5 到 60 秒之间")
        for path in (
            self.download_dir,
            self.image_download_dir,
            self.video_download_dir,
            self.file_download_dir,
            self.session_dir,
            self.config_dir,
        ):
            if not path.is_absolute():
                raise RuntimeError(f"目录必须使用绝对路径：{path}")

    @classmethod
    def from_json_dict(cls, data: dict[str, Any]) -> "Settings":
        download_dir = Path(data.get("download_dir") or "/downloads").resolve()
        settings = cls(
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
            admin_user_ids=parse_user_ids(data.get("admin_user_ids")),
            admin_password_hash=str(data.get("admin_password_hash") or ""),
            admin_password_from_env=bool(data.get("admin_password_from_env")),
            session_name=str(data.get("session_name") or "media_downloader_bot"),
            progress_interval_seconds=float(data.get("progress_interval_seconds") or 3),
            progress_percent_step=int(data.get("progress_percent_step") or 5),
            max_filename_stem_length=int(data.get("max_filename_stem_length") or 120),
            max_auto_retries=int(data.get("max_auto_retries", 3)),
            queue_maxsize=int(data.get("queue_maxsize") or 100),
            history_flush_interval_seconds=float(data.get("history_flush_interval_seconds") or 2),
            log_level=str(data.get("log_level") or "INFO"),
        )
        settings.validate()
        return settings

    @classmethod
    def from_env(cls) -> "Settings":
        download_dir = Path(os.getenv("DOWNLOAD_DIR", "/downloads")).resolve()
        admin_password = os.getenv("ADMIN_PASSWORD", "")
        settings = cls(
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
            admin_user_ids=parse_user_ids(os.getenv("ADMIN_USER_IDS")),
            admin_password_hash=hash_password(admin_password) if admin_password else "",
            admin_password_from_env=bool(admin_password),
            session_name=os.getenv("SESSION_NAME", "media_downloader_bot").strip()
            or "media_downloader_bot",
            progress_interval_seconds=float(os.getenv("PROGRESS_INTERVAL_SECONDS", "3")),
            progress_percent_step=int(os.getenv("PROGRESS_PERCENT_STEP", "5")),
            max_filename_stem_length=int(os.getenv("MAX_FILENAME_STEM_LENGTH", "120")),
            max_auto_retries=int(os.getenv("MAX_AUTO_RETRIES", "3")),
            queue_maxsize=int(os.getenv("QUEUE_MAXSIZE", "100")),
            history_flush_interval_seconds=float(os.getenv("HISTORY_FLUSH_INTERVAL_SECONDS", "2")),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
            cookie_secure=_env_bool("COOKIE_SECURE", False),
        )
        settings.validate()
        return settings


class SettingsStore:
    def __init__(self, settings: Settings):
        self._settings = settings
        self._settings.config_dir.mkdir(parents=True, exist_ok=True)
        self._load_saved()

    @property
    def settings(self) -> Settings:
        return self._settings

    def snapshot(self) -> Settings:
        return deepcopy(self._settings)

    def _load_saved(self) -> None:
        path = self._settings.config_path
        if not path.exists():
            return
        try:
            saved_data = json.loads(path.read_text(encoding="utf-8"))
            saved = Settings.from_json_dict(saved_data)
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            backup = path.with_suffix(".json.corrupt")
            try:
                os.replace(path, backup)
            except OSError:
                pass
            # Keep the environment seed so the panel can still start and repair the
            # configuration. The corrupt file is preserved for manual recovery.
            _ = exc
            return

        env_seed = self._settings
        if not saved.api_id:
            saved.api_id = env_seed.api_id
        if not saved.api_hash:
            saved.api_hash = env_seed.api_hash
        if not saved.bot_token:
            saved.bot_token = env_seed.bot_token
        if not saved.allowed_user_ids:
            saved.allowed_user_ids = env_seed.allowed_user_ids
        if not saved.admin_user_ids:
            saved.admin_user_ids = env_seed.admin_user_ids
        if env_seed.admin_password_from_env:
            saved.admin_password_hash = env_seed.admin_password_hash
        elif not saved.admin_password_hash:
            saved.admin_password_hash = env_seed.admin_password_hash
        saved.admin_password_from_env = False
        saved.config_dir = env_seed.config_dir
        saved.cookie_secure = env_seed.cookie_secure
        saved.validate()
        self._settings = saved

    def build(self, updates: dict[str, Any]) -> Settings:
        data = self._settings.to_json_dict()
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
            "admin_user_ids",
            "session_name",
            "progress_interval_seconds",
            "progress_percent_step",
            "max_filename_stem_length",
            "max_auto_retries",
            "queue_maxsize",
            "history_flush_interval_seconds",
        ):
            if key not in updates:
                continue
            value = updates[key]
            if key in {"api_hash", "bot_token"} and value == "":
                continue
            data[key] = value

        if updates.get("admin_password"):
            data["admin_password_hash"] = hash_password(str(updates["admin_password"]))
        data["config_dir"] = str(self._settings.config_dir)
        candidate = Settings.from_json_dict(data)
        candidate.cookie_secure = self._settings.cookie_secure
        return candidate

    def commit(self, settings: Settings) -> Settings:
        settings.validate()
        settings.config_dir.mkdir(parents=True, exist_ok=True)
        _atomic_json_write(settings.config_path, settings.to_json_dict())
        self._settings = settings
        return settings

    def save(self, updates: dict[str, Any]) -> Settings:
        return self.commit(self.build(updates))

    def restore(self, settings: Settings) -> Settings:
        return self.commit(deepcopy(settings))
