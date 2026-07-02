from __future__ import annotations

import logging
import os
import hmac
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from app.bot import BotManager
from app.config import Settings, SettingsStore, verify_password
from app.history import DownloadHistory
from app.logs import MemoryLogHandler

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
history = DownloadHistory(settings.config_dir / "downloads.json")
bot_manager = BotManager(history)
templates = Jinja2Templates(directory="app/templates")
app = FastAPI(title="媒体下载 Bot 控制台")
SESSION_COOKIE = "telethon_media_bot_session"
SESSION_MAX_AGE_SECONDS = 7 * 24 * 60 * 60


def list_downloaded_files(download_dirs: dict[str, Path]) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    for category, download_dir in download_dirs.items():
        download_dir.mkdir(parents=True, exist_ok=True)
        for path in download_dir.iterdir():
            if not path.is_file():
                continue
            stat = path.stat()
            files.append(
                {
                    "category": category,
                    "name": path.name,
                    "size_bytes": stat.st_size,
                    "modified_at": stat.st_mtime,
                    "url": f"/files/{category}/{path.name}",
                }
            )
    files.sort(key=lambda item: item["modified_at"], reverse=True)
    return files[:200]


def _session_secret() -> str:
    password_hash = settings_store.settings.admin_password_hash
    return password_hash or "no-password-configured"


def _sign_session(timestamp: str) -> str:
    return hmac.new(_session_secret().encode("utf-8"), timestamp.encode("utf-8"), "sha256").hexdigest()


def _make_session_token() -> str:
    timestamp = str(int(time.time()))
    return f"{timestamp}.{_sign_session(timestamp)}"


def _valid_session(token: str | None) -> bool:
    if not settings_store.settings.admin_password_hash:
        return True
    if not token or "." not in token:
        return False
    timestamp, signature = token.split(".", 1)
    try:
        issued_at = int(timestamp)
    except ValueError:
        return False
    if time.time() - issued_at > SESSION_MAX_AGE_SECONDS:
        return False
    return hmac.compare_digest(signature, _sign_session(timestamp))


def require_panel_auth(request: Request) -> None:
    if not _valid_session(request.cookies.get(SESSION_COOKIE)):
        raise HTTPException(status_code=401, detail="请先登录控制面板")


@app.on_event("startup")
async def startup() -> None:
    current = settings_store.settings
    current.ensure_dirs()
    if current.ready and os.getenv("AUTO_START_BOT", "false").lower() in {"1", "true", "yes"}:
        try:
            await bot_manager.start(current)
        except Exception as exc:
            bot_manager.last_error = f"{type(exc).__name__}: {exc}"
            logger.exception("Bot auto-start failed")


@app.on_event("shutdown")
async def shutdown() -> None:
    await bot_manager.stop()


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/api/auth/status")
async def auth_status(request: Request):
    return {
        "password_enabled": bool(settings_store.settings.admin_password_hash),
        "authenticated": _valid_session(request.cookies.get(SESSION_COOKIE)),
    }


@app.post("/api/auth/login")
async def login(payload: dict[str, Any]):
    password_hash = settings_store.settings.admin_password_hash
    if not password_hash:
        return {"authenticated": True}
    if not verify_password(str(payload.get("password") or ""), password_hash):
        raise HTTPException(status_code=401, detail="密码错误")
    response = JSONResponse({"authenticated": True})
    response.set_cookie(
        SESSION_COOKIE,
        _make_session_token(),
        max_age=SESSION_MAX_AGE_SECONDS,
        httponly=True,
        samesite="lax",
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
        "settings": current.public_dict(),
        "bot": bot_manager.state(),
        "downloads": history.list(),
        "files": list_downloaded_files(
            {
                "images": current.image_download_dir,
                "videos": current.video_download_dir,
                "files": current.file_download_dir,
            }
        ),
    }


@app.get("/api/logs")
async def api_logs(request: Request):
    require_panel_auth(request)
    return {"logs": memory_log_handler.list()}


@app.post("/api/settings")
async def save_settings(request: Request, payload: dict[str, Any]):
    require_panel_auth(request)
    was_running = bot_manager.running
    current = settings_store.save(payload)
    current.ensure_dirs()
    if was_running:
        await bot_manager.restart(current)
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


@app.get("/files/{category}/{file_name}")
async def get_file(request: Request, category: str, file_name: str):
    require_panel_auth(request)
    current = settings_store.settings
    download_dirs = {
        "images": current.image_download_dir.resolve(),
        "videos": current.video_download_dir.resolve(),
        "files": current.file_download_dir.resolve(),
    }
    if category not in download_dirs:
        raise HTTPException(status_code=404, detail="文件不存在")
    download_dir = download_dirs[category]
    target = (download_dir / file_name).resolve()
    if download_dir not in target.parents and target != download_dir:
        raise HTTPException(status_code=404, detail="文件不存在")
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    return FileResponse(target, filename=target.name)
