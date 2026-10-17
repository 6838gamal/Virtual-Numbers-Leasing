# admin/storage.py
from __future__ import annotations

import json
import os
import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

# ══ مسار المشروع الجذري ══
# admin/storage.py → admin/ → project/
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
STORE_FILE = DATA_DIR / "store.json"
BACKUP_DIR = DATA_DIR / "backups"

_LOCK = threading.RLock()
_SAVE_TIMER: threading.Timer | None = None
_SAVE_DELAY = float(os.getenv("STORE_SAVE_DELAY", "0.8"))


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
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not STORE_FILE.exists():
        store = _default_store()
        _write_atomic(store)
        return store
    try:
        with STORE_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        print(f"[storage] ⚠️  store.json تالف ({e}) — أرشفة وإعادة إنشاء")
        try:
            bad = DATA_DIR / f"store.corrupt.{int(time.time())}.json"
            shutil.copy2(STORE_FILE, bad)
        except Exception:
            pass
        data = _default_store()
        _write_atomic(data)

    base = _default_store()
    for k, v in base.items():
        data.setdefault(k, v)
    if isinstance(data.get("owner_wallet"), dict):
        for k, v in base["owner_wallet"].items():
            data["owner_wallet"].setdefault(k, v)
    return data


def _write_atomic(store: dict) -> None:
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
    global _SAVE_TIMER
    with _LOCK:
        if _SAVE_TIMER is not None:
            _SAVE_TIMER.cancel()
        t = threading.Timer(_SAVE_DELAY, _do_save, args=(store,))
        t.daemon = True
        t.start()
        _SAVE_TIMER = t


def save_now(store: dict) -> None:
    global _SAVE_TIMER
    with _LOCK:
        if _SAVE_TIMER is not None:
            _SAVE_TIMER.cancel()
            _SAVE_TIMER = None
        _do_save(store)


def backup(store: dict) -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    path = BACKUP_DIR / f"store_{stamp}.json"
    with path.open("w", encoding="utf-8") as f:
        json.dump(store, f, ensure_ascii=False, indent=2, default=str)
    return path


# ══ لمزامنة التغييرات من الطرف الآخر ══
def reload_from_disk() -> dict:
    """يعيد قراءة store.json من القرص (يُستخدم قبل العمليات الحسّاسة)."""
    if not STORE_FILE.exists():
        return load_store()
    try:
        with STORE_FILE.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return load_store()
