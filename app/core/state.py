"""حالة التطبيق العامة (uptime, request stats, ...)."""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse


START_TIME = time.time()


_REQUEST_STATS: dict[str, Any] = {
    "total": 0,
    "protected_401": 0,
    "protected_redirect": 0,
    "session_expired": 0,
    "session_recovered": 0,
    "last_401": None,
    "last_401_path": None,
    "top_401_paths": {},
    "top_redirect_paths": {},
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def fmt_uptime(seconds: float) -> str:
    h, r = divmod(int(seconds), 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}"


def is_safe_next(next_url: str) -> bool:
    if not next_url or not next_url.startswith("/"):
        return False
    if next_url.startswith("//") or next_url.startswith("\\"):
        return False
    parsed = urlparse(next_url)
    if parsed.scheme or parsed.netloc:
        return False
    return True
