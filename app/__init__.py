"""Telegram media downloader bot."""

import os

__version__ = os.getenv("APP_VERSION", "1.8.0").removeprefix("v")
