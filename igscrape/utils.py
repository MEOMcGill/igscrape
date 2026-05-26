"""Utility functions for Instagram scraping."""
import asyncio
import base64
import json
import os
import platform
import re
from datetime import datetime, timezone
from pathlib import Path

import requests


def get_device_os() -> str:
    """Detect current OS for Camoufox fingerprint."""
    system = platform.system().lower()
    if system == "darwin":
        return "macos"
    elif system == "windows":
        return "windows"
    return "linux"


class utc:
    @staticmethod
    def now() -> datetime:
        return datetime.now(timezone.utc)

    @staticmethod
    def from_iso(iso: str) -> datetime:
        return datetime.fromisoformat(iso).replace(tzinfo=timezone.utc)

    @staticmethod
    def ts() -> int:
        return int(utc.now().timestamp())


def parse_cookies(val: str) -> list[dict]:
    """Parse cookies from various formats into Playwright cookie list."""
    try:
        val = base64.b64decode(val).decode()
    except Exception:
        pass

    try:
        try:
            res = json.loads(val)
            if isinstance(res, dict) and "cookies" in res:
                res = res["cookies"]

            if isinstance(res, list):
                return res
            if isinstance(res, dict):
                return [
                    {
                        "name": name,
                        "value": value,
                        "domain": ".instagram.com",
                        "path": "/",
                        "secure": True,
                        "httpOnly": True,
                        "sameSite": "None",
                    }
                    for name, value in res.items()
                ]
        except json.JSONDecodeError:
            res = val.split("; ")
            res = [x.split("=", 1) for x in res]
            return [
                {
                    "name": x[0],
                    "value": x[1],
                    "domain": ".instagram.com",
                    "path": "/",
                    "secure": True,
                    "httpOnly": True,
                    "sameSite": "None",
                }
                for x in res
            ]
    except Exception:
        pass

    raise ValueError(f"Invalid cookie value: {val}")


def get_env_bool(key: str, default_val: bool = False) -> bool:
    val = os.getenv(key)
    if val is None:
        return default_val
    return val.lower() in ("1", "true", "yes")


def internet_good() -> bool:
    """Check if internet connection is working."""
    try:
        requests.get("https://8.8.8.8", timeout=10)
        return True
    except (ConnectionError, requests.exceptions.ConnectTimeout, requests.exceptions.Timeout):
        return False
    except Exception:
        return False


_POST_URL_RE = re.compile(r"/(?:p|reel|tv)/([A-Za-z0-9_-]+)/?")


def is_post_url(href: str | None) -> bool:
    """Return True if the URL is an Instagram post/reel permalink."""
    if href is None:
        return False
    return _POST_URL_RE.search(href) is not None


def extract_shortcode(url: str | None) -> str | None:
    """Extract shortcode from an Instagram post/reel URL."""
    if not url:
        return None
    m = _POST_URL_RE.search(url)
    return m.group(1) if m else None


def unix_to_datetime(unix_timestamp: int) -> datetime:
    return datetime.fromtimestamp(unix_timestamp, tz=timezone.utc)


def get_home_dir_path() -> str:
    return os.path.dirname(Path(os.path.abspath(__file__)).parent)


async def gather(coros):
    """Yield results from coroutines as they complete (unordered)."""
    for c in asyncio.as_completed(list(coros)):
        yield await c
