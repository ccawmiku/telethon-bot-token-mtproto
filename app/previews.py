from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import threading
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError


PREVIEW_SIZE = (320, 180)
PREVIEWABLE_CATEGORIES = {"images", "videos"}


class PreviewError(RuntimeError):
    """Raised when a safe preview cannot be generated."""


class PreviewGenerator:
    def __init__(self, cache_dir: Path, cache_limit: int = 500):
        self.cache_dir = cache_dir
        self.cache_limit = cache_limit
        self._lock = threading.Lock()

    def generate(self, source: Path, category: str) -> Path:
        if category not in PREVIEWABLE_CATEGORIES:
            raise PreviewError("该文件类型不支持缩略预览")
        stat = source.stat()
        cache_key = hashlib.sha256(
            f"{source.resolve()}\0{stat.st_size}\0{stat.st_mtime_ns}".encode("utf-8")
        ).hexdigest()
        destination = self.cache_dir / f"{cache_key}.jpg"
        if destination.is_file() and destination.stat().st_size:
            return destination

        with self._lock:
            if destination.is_file() and destination.stat().st_size:
                return destination
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            try:
                self.cache_dir.chmod(0o700)
            except OSError:
                pass
            temporary = destination.with_name(f".{destination.stem}.{os.getpid()}.tmp.jpg")
            temporary.unlink(missing_ok=True)
            try:
                if category == "images":
                    self._generate_image(source, temporary)
                else:
                    self._generate_video(source, temporary)
                if not temporary.is_file() or temporary.stat().st_size == 0:
                    raise PreviewError("缩略图生成失败")
                try:
                    temporary.chmod(0o600)
                except OSError:
                    pass
                os.replace(temporary, destination)
                self._prune_cache()
            finally:
                temporary.unlink(missing_ok=True)
        return destination

    @staticmethod
    def _generate_image(source: Path, destination: Path) -> None:
        try:
            with Image.open(source) as opened:
                image = ImageOps.exif_transpose(opened)
                image.seek(0)
                image.thumbnail(PREVIEW_SIZE, Image.Resampling.LANCZOS)
                if image.mode not in {"RGB", "RGBA"}:
                    image = image.convert("RGBA" if "transparency" in image.info else "RGB")
                canvas = Image.new("RGB", PREVIEW_SIZE, "#eef2f6")
                offset = ((PREVIEW_SIZE[0] - image.width) // 2, (PREVIEW_SIZE[1] - image.height) // 2)
                if image.mode == "RGBA":
                    canvas.paste(image, offset, image)
                else:
                    canvas.paste(image, offset)
                canvas.save(destination, "JPEG", quality=82, optimize=True)
        except (Image.DecompressionBombError, UnidentifiedImageError, OSError, ValueError) as exc:
            raise PreviewError(f"无法读取图片：{exc}") from exc

    @staticmethod
    def _generate_video(source: Path, destination: Path) -> None:
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise PreviewError("服务器未安装 FFmpeg，无法生成视频预览")
        video_filter = (
            "scale=320:180:force_original_aspect_ratio=decrease,"
            "pad=320:180:(ow-iw)/2:(oh-ih)/2:color=0xeef2f6"
        )
        for seek in ("1", "0"):
            command = [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-ss",
                seek,
                "-i",
                str(source),
                "-frames:v",
                "1",
                "-vf",
                video_filter,
                "-q:v",
                "4",
                "-y",
                str(destination),
            ]
            try:
                result = subprocess.run(command, capture_output=True, check=False, timeout=20)
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise PreviewError(f"视频预览生成失败：{exc}") from exc
            if result.returncode == 0 and destination.is_file() and destination.stat().st_size:
                return
            destination.unlink(missing_ok=True)
        raise PreviewError("FFmpeg 无法从该视频提取预览帧")

    def _prune_cache(self) -> None:
        try:
            previews = sorted(
                self.cache_dir.glob("*.jpg"),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )
        except OSError:
            return
        for stale in previews[self.cache_limit :]:
            stale.unlink(missing_ok=True)
