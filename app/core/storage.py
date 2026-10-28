"""الوصول للمخزن المشترك (shared_storage) + أدوات مساعدة."""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from app.config import BASE_DIR

if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from shared_storage import (  # type: ignore
    load_store, schedule_save, save_now, reload_from_disk, STORE_FILE,
)

try:
    from shared_storage import store_status as _store_status  # type: ignore
    _HAS_STORE_STATUS = True
except ImportError:
    _store_status = None
    _HAS_STORE_STATUS = False


STORE: dict[str, Any] = load_store()
STORE.setdefault("popular_services", [])
print(f"[app] 💾 store loaded from {STORE_FILE}")


def next_id(collection: dict) -> int:
    int_keys = []
    for k in collection.keys():
        try:
            int_keys.append(int(k))
        except (ValueError, TypeError):
            continue
    return (max(int_keys) if int_keys else 0) + 1


def store_file_info() -> dict:
    if _HAS_STORE_STATUS:
        try:
            st = _store_status()
            return {
                "path": st.get("target", str(STORE_FILE)),
                "exists": st.get("exists", True),
                "kind": st.get("backend", "unknown"),
                "version": st.get("version"),
                "size_bytes": st.get("size_bytes"),
            }
        except Exception as e:
            print(f"[app] ⚠️  store_status فشل: {type(e).__name__}: {e}")

    if isinstance(STORE_FILE, Path):
        return {
            "path": str(STORE_FILE),
            "exists": STORE_FILE.exists(),
            "kind": "file",
            "version": None,
            "size_bytes": None,
        }
    s = str(STORE_FILE)
    is_dsn = ("://" in s) or ("@/" in s)
    return {
        "path": s, "exists": True,
        "kind": "dsn" if is_dsn else "string",
        "version": None, "size_bytes": None,
    }


def persist() -> None:
    schedule_save(STORE)


def persist_now() -> None:
    save_now(STORE)


def refresh() -> None:
    fresh = reload_from_disk()
    STORE.clear()
    STORE.update(fresh)
    STORE.setdefault("popular_services", [])
