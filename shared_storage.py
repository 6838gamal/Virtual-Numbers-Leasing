# shared_storage.py
"""
تخزين دائم مشترك (PostgreSQL) بين app/main.py و admin/main.py.

- يستخدم DATABASE_URL (Postgres).
- يحفظ store كـ JSONB في جدول واحد (singleton row).
- نفس الواجهة العامة السابقة: load_store, schedule_save, save_now,
  reload_from_disk, backup, STORE_FILE (string وهمي للتوافق).

المتغيرات:
    DATABASE_URL   postgres://user:pass@host:port/db  (مطلوب)
    STORE_SAVE_DELAY  ثواني (افتراضي 0.8)

التبعيات:
    psycopg[binary,pool]>=3.2
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool


# ══════════════════════════════════════════════════════════════
# 1) إعدادات
# ══════════════════════════════════════════════════════════════
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
if not DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL غير مضبوط. مثال:\n"
        "  postgres://user:pass@host:5432/dbname\n"
        "أضفه إلى متغيرات البيئة."
    )

# psycopg 3 يحتاج postgresql:// بدل postgres:// (بعض المنصات تعطي postgres://)
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = "postgresql://" + DATABASE_URL[len("postgres://"):]

# للتوافق فقط — بعض الأكواد تطبع STORE_FILE في /health
STORE_FILE = DATABASE_URL.split("@")[-1]  # مضلّل قليلاً لكنه يعمل للعرض

_SAVE_DELAY = float(os.getenv("STORE_SAVE_DELAY", "0.8"))

# جدول واحد — صف واحد فقط (id=1)
_ROW_ID = 1

_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS store_state (
    id          INTEGER PRIMARY KEY,
    data        JSONB NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""

_UPSERT_SQL = """
INSERT INTO store_state (id, data, updated_at)
VALUES (%s, %s, NOW())
ON CONFLICT (id) DO UPDATE
SET data = EXCLUDED.data,
    updated_at = NOW();
"""

_SELECT_SQL = "SELECT data FROM store_state WHERE id = %s;"


# ══════════════════════════════════════════════════════════════
# 2) Pool + init
# ══════════════════════════════════════════════════════════════
_pool = ConnectionPool(
    conninfo=DATABASE_URL,
    min_size=1,
    max_size=int(os.getenv("PG_POOL_MAX", "5")),
    timeout=float(os.getenv("PG_POOL_TIMEOUT", "10")),
    kwargs={"autocommit": True},
    open=True,
)


def _ensure_schema() -> None:
    with _pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(_TABLE_SQL)


_ensure_schema()


# ══════════════════════════════════════════════════════════════
# 3) القاموس الافتراضي
# ══════════════════════════════════════════════════════════════
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


def _merge_defaults(data: dict) -> dict:
    base = _default_store()
    for k, v in base.items():
        data.setdefault(k, v)
    if isinstance(data.get("owner_wallet"), dict):
        for k, v in base["owner_wallet"].items():
            data["owner_wallet"].setdefault(k, v)
    return data


# ══════════════════════════════════════════════════════════════
# 4) CRUD
# ══════════════════════════════════════════════════════════════
def load_store() -> dict:
    """قراءة الحالة من DB (أو إنشاء الصف الافتراضي)."""
    with _pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(_SELECT_SQL, (_ROW_ID,))
            row = cur.fetchone()
            if row is None:
                store = _default_store()
                cur.execute(_UPSERT_SQL, (_ROW_ID, json.dumps(store)))
                return store
            data = row["data"]
            if isinstance(data, str):
                data = json.loads(data)
            return _merge_defaults(data)


def reload_from_disk() -> dict:
    """اسم متوافق — يقرأ من DB. يستخدمه _refresh() في الطرفين."""
    return load_store()


def _write_now(store: dict) -> None:
    with _pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(_UPSERT_SQL, (_ROW_ID, json.dumps(store, default=str)))


# ══════════════════════════════════════════════════════════════
# 5) Debounced save (نفس سلوك الملف)
# ══════════════════════════════════════════════════════════════
_LOCK = threading.RLock()
_SAVE_TIMER: threading.Timer | None = None


def _do_save(store: dict) -> None:
    global _SAVE_TIMER
    with _LOCK:
        try:
            _write_now(store)
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


# ══════════════════════════════════════════════════════════════
# 6) Backup (نسخة في DB + ملف محلي للطوارئ)
# ══════════════════════════════════════════════════════════════
_BACKUP_DIR = Path(os.getenv("BACKUP_DIR", "/tmp/jamal-backups"))
_BACKUP_DIR.mkdir(parents=True, exist_ok=True)


def backup(store: dict) -> Path:
    """نسخة احتياطية: يكتب نسخة في جدول backups + ملف محلي."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    payload = json.dumps(store, ensure_ascii=False, default=str)

    try:
        with _pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS store_backups (
                        id          BIGSERIAL PRIMARY KEY,
                        data        JSONB NOT NULL,
                        created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    );
                """)
                cur.execute(
                    "INSERT INTO store_backups (data) VALUES (%s);",
                    (payload,),
                )
    except Exception as e:
        print(f"[storage] ⚠️  backup DB فشل: {type(e).__name__}: {e}")

    path = _BACKUP_DIR / f"store_{stamp}.json"
    try:
        with path.open("w", encoding="utf-8") as f:
            f.write(payload)
    except Exception as e:
        print(f"[storage] ⚠️  backup ملف فشل: {type(e).__name__}: {e}")

    return path
