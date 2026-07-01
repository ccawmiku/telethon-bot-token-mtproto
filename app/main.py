from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.templating import Jinja2Templates

from app.bot import BotManager
from app.config import Settings, SettingsStore
from app.history import DownloadHistory

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("media-downloader-web")

settings_store = SettingsStore(Settings.from_env())
settings = settings_store.settings
history = DownloadHistory(settings.config_dir / "downloads.json")
bot_manager = BotManager(history)
templates = Jinja2Templates(directory="app/templates")
app = FastAPI(title="Telethon Media Downloader Bot")


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


@app.get("/api/state")
async def api_state():
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


@app.post("/api/settings")
async def save_settings(payload: dict[str, Any]):
    was_running = bot_manager.running
    current = settings_store.save(payload)
    current.ensure_dirs()
    if was_running:
        await bot_manager.restart(current)
    return {"settings": current.public_dict(), "bot": bot_manager.state()}


@app.post("/api/bot/start")
async def start_bot():
    try:
        await bot_manager.start(settings_store.settings)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"bot": bot_manager.state()}


@app.post("/api/bot/stop")
async def stop_bot():
    await bot_manager.stop()
    return {"bot": bot_manager.state()}


@app.post("/api/bot/restart")
async def restart_bot():
    try:
        await bot_manager.restart(settings_store.settings)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"bot": bot_manager.state()}


@app.get("/files/{category}/{file_name}")
async def get_file(category: str, file_name: str):
    current = settings_store.settings
    download_dirs = {
        "images": current.image_download_dir.resolve(),
        "videos": current.video_download_dir.resolve(),
        "files": current.file_download_dir.resolve(),
    }
    if category not in download_dirs:
        raise HTTPException(status_code=404, detail="File not found")
    download_dir = download_dirs[category]
    target = (download_dir / file_name).resolve()
    if download_dir not in target.parents and target != download_dir:
        raise HTTPException(status_code=404, detail="File not found")
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(target, filename=target.name)
