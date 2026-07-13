from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
import secrets
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, AsyncIterator
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app import __version__
from app.bot import BotManager
from app.config import Settings, SettingsStore, verify_password
from app.history import DownloadHistory, RETRYABLE_STATUSES
from app.logs import MemoryLogHandler
from app.previews import PREVIEWABLE_CATEGORIES, PreviewError, PreviewGenerator

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("media-downloader-web")
memory_log_handler = MemoryLogHandler()
memory_log_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
logging.getLogger().addHandler(memory_log_handler)

settings_store = SettingsStore(Settings.from_env())
settings = settings_store.settings
history = DownloadHistory(
    settings.config_dir / "downloads.json",
    flush_interval=settings.history_flush_interval_seconds,
)
bot_manager = BotManager(history)
preview_generator = PreviewGenerator(settings.config_dir / "previews")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
SESSION_COOKIE = "telethon_media_bot_session"
SESSION_MAX_AGE_SECONDS = 7 * 24 * 60 * 60
LOGIN_WINDOW_SECONDS = 5 * 60
LOGIN_MAX_ATTEMPTS = 5
BOOTSTRAP_TOKEN = os.getenv("BOOTSTRAP_TOKEN", "").strip() or secrets.token_urlsafe(18)
login_attempts: dict[str, deque[float]] = defaultdict(deque)


class LoginPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    password: str = Field(max_length=1024)


class SettingsPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    api_id: int | None = Field(default=None, gt=0)
    api_hash: str = Field(default="", max_length=256)
    bot_token: str = Field(default="", max_length=512)
    allowed_user_ids: str | list[int] = ""
    admin_user_ids: str | list[int] = ""
    admin_password: str = Field(default="", max_length=1024)
    download_dir: str = Field(default="/downloads", min_length=1, max_length=1024)
    image_download_dir: str = Field(default="/downloads/images", min_length=1, max_length=1024)
    video_download_dir: str = Field(default="/downloads/videos", min_length=1, max_length=1024)
    file_download_dir: str = Field(default="/downloads/files", min_length=1, max_length=1024)
    session_dir: str = Field(default="/sessions", min_length=1, max_length=1024)
    session_name: str = Field(default="media_downloader_bot", min_length=1, max_length=80)
    progress_interval_seconds: float = Field(default=3, ge=0.5, le=60)
    progress_percent_step: int = Field(default=5, ge=1, le=100)
    max_filename_stem_length: int = Field(default=120, ge=16, le=200)
    max_auto_retries: int = Field(default=3, ge=0, le=10)
    queue_maxsize: int = Field(default=100, ge=1, le=1000)
    history_flush_interval_seconds: float = Field(default=2, ge=0.5, le=60)

    @field_validator(
        "download_dir",
        "image_download_dir",
        "video_download_dir",
        "file_download_dir",
        "session_dir",
    )
    @classmethod
    def validate_absolute_path(cls, value: str) -> str:
        if not (PurePosixPath(value).is_absolute() or PureWindowsPath(value).is_absolute()):
            raise ValueError("目录必须使用绝对路径")
        return value


class LimitPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    megabytes_per_second: float | None = Field(default=None, ge=1, le=10_000)


def _client_key(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _prune_login_attempts(key: str) -> deque[float]:
    now = time.monotonic()
    attempts = login_attempts[key]
    while attempts and now - attempts[0] > LOGIN_WINDOW_SECONDS:
        attempts.popleft()
    return attempts


def configured_download_dirs(current: Settings) -> dict[str, Path]:
    return {
        "images": current.image_download_dir.resolve(),
        "videos": current.video_download_dir.resolve(),
        "files": current.file_download_dir.resolve(),
    }


def resolve_downloaded_file(current: Settings, category: str, file_name: str) -> Path:
    download_dirs = configured_download_dirs(current)
    if category not in download_dirs:
        raise HTTPException(status_code=404, detail="文件不存在")
    download_dir = download_dirs[category]
    target = (download_dir / file_name).resolve()
    if download_dir not in target.parents or not target.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    return target


def _media_urls(category: str, name: str, modified_ns: int) -> dict[str, str | None]:
    encoded_category = quote(category, safe="")
    encoded_name = quote(name, safe="")
    return {
        "url": f"/files/{encoded_category}/{encoded_name}",
        "preview_url": (
            f"/previews/{encoded_category}/{encoded_name}?v={modified_ns}"
            if category in PREVIEWABLE_CATEGORIES
            else None
        ),
    }


def list_downloaded_files(download_dirs: dict[str, Path]) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    for category, download_dir in download_dirs.items():
        download_dir.mkdir(parents=True, exist_ok=True)
        try:
            paths = list(download_dir.iterdir())
        except OSError:
            logger.exception("无法读取下载目录：%s", download_dir)
            continue
        for path in paths:
            try:
                if not path.is_file() or path.name.startswith("."):
                    continue
                stat = path.stat()
                if stat.st_size == 0:
                    continue
            except OSError:
                continue
            files.append(
                {
                    "category": category,
                    "name": path.name,
                    "size_bytes": stat.st_size,
                    "modified_at": stat.st_mtime,
                    **_media_urls(category, path.name, stat.st_mtime_ns),
                }
            )
    files.sort(key=lambda item: item["modified_at"], reverse=True)
    return files[:200]


def list_download_records(current: Settings) -> list[dict[str, Any]]:
    records = history.list()
    download_dirs = configured_download_dirs(current)
    for record in records:
        record["category"] = None
        record["url"] = None
        record["preview_url"] = None
        try:
            path = Path(record["path"]).resolve()
            stat = path.stat()
        except (KeyError, OSError, TypeError):
            continue
        if not path.is_file() or stat.st_size == 0:
            continue
        for category, download_dir in download_dirs.items():
            if path.parent != download_dir:
                continue
            record["category"] = category
            record.update(_media_urls(category, path.name, stat.st_mtime_ns))
            break
    return records


def _session_secret() -> str:
    password_hash = settings_store.settings.admin_password_hash
    if password_hash:
        return password_hash
    return hashlib.sha256(BOOTSTRAP_TOKEN.encode("utf-8")).hexdigest()


def _sign_session(timestamp: str) -> str:
    return hmac.new(_session_secret().encode("utf-8"), timestamp.encode("utf-8"), "sha256").hexdigest()


def _make_session_token() -> str:
    timestamp = str(int(time.time()))
    return f"{timestamp}.{_sign_session(timestamp)}"


def _valid_session(token: str | None) -> bool:
    if not token or "." not in token:
        return False
    timestamp, signature = token.split(".", 1)
    try:
        issued_at = int(timestamp)
    except ValueError:
        return False
    age = time.time() - issued_at
    if age < -60 or age > SESSION_MAX_AGE_SECONDS:
        return False
    return hmac.compare_digest(signature, _sign_session(timestamp))


def require_panel_auth(request: Request) -> None:
    if not _valid_session(request.cookies.get(SESSION_COOKIE)):
        raise HTTPException(status_code=401, detail="请先登录控制面板")


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    current = settings_store.settings
    current.ensure_dirs()
    recovery = history.recover_incomplete()
    if recovery["recovered"] or recovery["interrupted"]:
        logger.warning("启动时恢复完成 %s 条，标记中断 %s 条", recovery["recovered"], recovery["interrupted"])
    if not current.admin_password_hash:
        logger.warning("控制台尚未设置密码。一次性初始化口令：%s", BOOTSTRAP_TOKEN)
    auto_start = os.getenv("AUTO_START_BOT", "true").lower() not in {"0", "false", "no"}
    if current.ready and auto_start:
        try:
            await bot_manager.start(current)
        except Exception as exc:
            bot_manager.last_error = f"{type(exc).__name__}: {exc}"
            logger.exception("Bot 自动启动失败")
    try:
        yield
    finally:
        await bot_manager.stop()
        history.flush()


app = FastAPI(title="媒体下载 Bot 控制台", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
        "script-src 'self'; frame-ancestors 'none'; base-uri 'none'"
    )
    if request.url.scheme == "https":
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/healthz")
async def healthz():
    return {"status": "ok", "bot_running": bot_manager.running}


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={"version": __version__},
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/auth/status")
async def auth_status(request: Request):
    return {
        "password_enabled": bool(settings_store.settings.admin_password_hash),
        "bootstrap_required": not bool(settings_store.settings.admin_password_hash),
        "authenticated": _valid_session(request.cookies.get(SESSION_COOKIE)),
    }


@app.post("/api/auth/login")
async def login(request: Request, payload: LoginPayload):
    key = _client_key(request)
    attempts = _prune_login_attempts(key)
    if len(attempts) >= LOGIN_MAX_ATTEMPTS:
        raise HTTPException(status_code=429, detail="登录失败次数过多，请 5 分钟后重试")
    password_hash = settings_store.settings.admin_password_hash
    valid = verify_password(payload.password, password_hash) if password_hash else hmac.compare_digest(
        payload.password,
        BOOTSTRAP_TOKEN,
    )
    if not valid:
        attempts.append(time.monotonic())
        raise HTTPException(status_code=401, detail="密码或初始化口令错误")
    attempts.clear()
    response = JSONResponse({"authenticated": True})
    response.set_cookie(
        SESSION_COOKIE,
        _make_session_token(),
        max_age=SESSION_MAX_AGE_SECONDS,
        httponly=True,
        secure=settings_store.settings.cookie_secure,
        samesite="strict",
    )
    return response


@app.post("/api/auth/logout")
async def logout():
    response = JSONResponse({"authenticated": False})
    response.delete_cookie(SESSION_COOKIE)
    return response


@app.get("/api/state")
async def api_state(request: Request):
    require_panel_auth(request)
    current = settings_store.settings
    return {
        "version": __version__,
        "settings": current.public_dict(),
        "bot": bot_manager.state(),
        "downloads": list_download_records(current),
        "files": list_downloaded_files(configured_download_dirs(current)),
    }


@app.get("/api/logs")
async def api_logs(request: Request):
    require_panel_auth(request)
    return {"logs": memory_log_handler.list()}


@app.post("/api/settings")
async def save_settings(request: Request, payload: SettingsPayload):
    require_panel_auth(request)
    if payload.admin_password and len(payload.admin_password) < 10:
        raise HTTPException(status_code=422, detail="控制台新密码至少需要 10 个字符")
    old = settings_store.snapshot()
    was_running = bot_manager.running
    candidate = settings_store.build(payload.model_dump(exclude_unset=True))
    try:
        candidate.ensure_dirs()
        if candidate.ready:
            if was_running:
                await bot_manager.restart(candidate)
            else:
                await bot_manager.start(candidate)
        elif was_running:
            await bot_manager.stop()
        current = settings_store.commit(candidate)
    except Exception as exc:
        logger.exception("新设置验证或启动失败，正在回滚")
        try:
            if bot_manager.running:
                await bot_manager.stop()
            if was_running and old.ready:
                await bot_manager.start(old)
        except Exception:
            logger.exception("回滚后恢复旧 Bot 失败")
        raise HTTPException(status_code=400, detail=f"设置未保存，已回滚：{exc}") from exc
    return {"settings": current.public_dict(), "bot": bot_manager.state()}


@app.post("/api/bot/start")
async def start_bot(request: Request):
    require_panel_auth(request)
    try:
        await bot_manager.start(settings_store.settings)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"bot": bot_manager.state()}


@app.post("/api/bot/stop")
async def stop_bot(request: Request):
    require_panel_auth(request)
    await bot_manager.stop()
    return {"bot": bot_manager.state()}


@app.post("/api/bot/restart")
async def restart_bot(request: Request):
    require_panel_auth(request)
    try:
        await bot_manager.restart(settings_store.settings)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"bot": bot_manager.state()}


@app.post("/api/controls/resume")
async def resume_downloads(request: Request):
    require_panel_auth(request)
    bot_manager.controls.resume()
    return {"controls": bot_manager.controls.state()}


@app.post("/api/controls/limit")
async def set_download_limit(request: Request, payload: LimitPayload):
    require_panel_auth(request)
    if payload.megabytes_per_second is None:
        bot_manager.controls.clear_limit()
    else:
        bot_manager.controls.set_limit_mb(payload.megabytes_per_second)
    return {"controls": bot_manager.controls.state()}


@app.post("/api/downloads/{record_id}/cancel")
async def cancel_download(request: Request, record_id: str):
    require_panel_auth(request)
    count = await bot_manager.cancel(record_id)
    if not count:
        raise HTTPException(status_code=404, detail="没有找到可取消的任务")
    return {"cancelled": count}


@app.post("/api/downloads/{record_id}/retry")
async def retry_download(request: Request, record_id: str):
    require_panel_auth(request)
    count = await bot_manager.retry(record_id)
    if not count:
        raise HTTPException(status_code=404, detail="没有找到可重试的任务或原消息已不可用")
    return {"queued": count}


@app.post("/api/downloads/retry-failed")
async def retry_all_failed_downloads(request: Request):
    require_panel_auth(request)
    if not bot_manager.running:
        raise HTTPException(status_code=409, detail="Bot 未运行，无法重新获取 Telegram 消息")
    total = len(history.list_statuses(RETRYABLE_STATUSES, limit=None))
    queued = await bot_manager.retry("failed", all_matches=True)
    remaining = len(history.list_statuses(RETRYABLE_STATUSES, limit=None))
    return {"total": total, "queued": queued, "remaining": remaining}


@app.post("/api/downloads/cleanup")
async def cleanup_downloads(request: Request):
    require_panel_auth(request)
    removed = history.remove_statuses(RETRYABLE_STATUSES)
    return {"removed": removed}


@app.get("/files/{category}/{file_name}")
async def get_file(request: Request, category: str, file_name: str):
    require_panel_auth(request)
    target = resolve_downloaded_file(settings_store.settings, category, file_name)
    return FileResponse(target, filename=target.name)


@app.get("/previews/{category}/{file_name}")
async def get_preview(request: Request, category: str, file_name: str):
    require_panel_auth(request)
    if category not in PREVIEWABLE_CATEGORIES:
        raise HTTPException(status_code=404, detail="该文件类型不支持预览")
    target = resolve_downloaded_file(settings_store.settings, category, file_name)
    try:
        preview = await asyncio.to_thread(preview_generator.generate, target, category)
    except PreviewError as exc:
        logger.warning("无法生成预览 %s：%s", target, exc)
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return FileResponse(
        preview,
        media_type="image/jpeg",
        headers={"Cache-Control": "private, max-age=86400"},
    )
