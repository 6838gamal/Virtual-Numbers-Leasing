# admin/storage.py
"""
تخزين دائم بسيط (JSON) لمخزن STORE.
- يقرأ من admin/data/store.json عند البدء.
- يحفظ تلقائياً عند كل تعديل (debounced).
- آمن للاستخدام مع uvicorn --workers 1 (لا تدعم multi-worker).
"""
from __future__ import annotations

import json
import os
import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ADMIN_DIR = Path(__file__).resolve().parent
DATA_DIR = ADMIN_DIR / "data"
STORE_FILE = DATA_DIR / "store.json"
BACKUP_DIR = DATA_DIR / "backups"

_LOCK = threading.RLock()
_SAVE_TIMER: threading.Timer | None = None
_SAVE_DELAY = float(os.getenv("STORE_SAVE_DELAY", "0.8"))  # ثواني


def _default_store() -> dict:
    return {
        "customers": {}, "numbers": [], "orders": [], "sms": [],
        "transactions": [], "deposits": [], "tickets": [], "sessions": [],
        "vouchers": [], "access_codes": [],
        "owner_wallet": {
            "total_profit": 0.0,
            "total_revenue": 0.0,
            "reserved_for_5sim": 0.0,
        },
    }


def load_store() -> dict:
    """يقرأ من الملف إن وُجد، وإلا يرجع الافتراضي."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not STORE_FILE.exists():
        store = _default_store()
        _write_atomic(store)
        return store

    try:
        with STORE_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        # ملف تالف → انسخه كنسخة احتياطية وابدأ من جديد
        print(f"[storage] ⚠️  ملف store.json تالف ({e}) — سيتم أرشفته")
        try:
            bad = DATA_DIR / f"store.corrupt.{int(time.time())}.json"
            shutil.copy2(STORE_FILE, bad)
            print(f"[storage] 📦 تم حفظ الملف التالف في {bad}")
        except Exception:
            pass
        data = _default_store()
        _write_atomic(data)

    # دمج مع الافتراضي لضمان وجود كل المفاتيح
    base = _default_store()
    for k, v in base.items():
        data.setdefault(k, v)
    if isinstance(data.get("owner_wallet"), dict):
        for k, v in base["owner_wallet"].items():
            data["owner_wallet"].setdefault(k, v)
    return data


def _write_atomic(store: dict) -> None:
    """كتابة ذرية عبر ملف مؤقت ثم rename."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STORE_FILE.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(store, f, ensure_ascii=False, indent=2, default=str)
    os.replace(tmp, STORE_FILE)


def _do_save(store: dict) -> None:
    global _SAVE_TIMER
    with _LOCK:
        try:
            _write_atomic(store)
        except Exception as e:
            print(f"[storage] ❌ فشل الحفظ: {type(e).__name__}: {e}")
        finally:
            _SAVE_TIMER = None


def schedule_save(store: dict) -> None:
    """يحفظ بعد تأخير بسيط (debounce) لتجنب كتابة متكررة."""
    global _SAVE_TIMER
    with _LOCK:
        if _SAVE_TIMER is not None:
            _SAVE_TIMER.cancel()
        t = threading.Timer(_SAVE_DELAY, _do_save, args=(store,))
        t.daemon = True
        t.start()
        _SAVE_TIMER = t


def save_now(store: dict) -> None:
    """حفظ فوري (يُستخدم عند الإغلاق)."""
    global _SAVE_TIMER
    with _LOCK:
        if _SAVE_TIMER is not None:
            _SAVE_TIMER.cancel()
            _SAVE_TIMER = None
        _do_save(store)


def backup(store: dict) -> Path:
    """نسخة احتياطية بوقت التنفيذ."""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    path = BACKUP_DIR / f"store_{stamp}.json"
    with path.open("w", encoding="utf-8") as f:
        json.dump(store, f, ensure_ascii=False, indent=2, default=str)
    return path
