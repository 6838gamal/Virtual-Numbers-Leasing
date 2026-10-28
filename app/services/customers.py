"""إدارة العملاء + استعلامات my_*."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import Request

from app.config import (
    GUEST_MODE, GUEST_CUSTOMER_ID, SESSION_IDLE_TIMEOUT,
)
from app.core.storage import STORE, next_id, persist_now
from app.services.sim5 import service_icon


# ══════════════════════════════════════════════════════════════
# Session helpers
# ══════════════════════════════════════════════════════════════
def session_is_expired(request: Request) -> bool:
    try:
        last_seen_iso = request.session.get("_last_seen")
    except Exception:
        return True
    if not last_seen_iso:
        return False
    try:
        last_seen = datetime.fromisoformat(last_seen_iso)
        if last_seen.tzinfo is None:
            last_seen = last_seen.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return True
    now = datetime.now(timezone.utc)
    return (now - last_seen).total_seconds() > SESSION_IDLE_TIMEOUT


# ══════════════════════════════════════════════════════════════
# Guest
# ══════════════════════════════════════════════════════════════
def get_or_create_guest() -> dict:
    if GUEST_CUSTOMER_ID and GUEST_CUSTOMER_ID in STORE["customers"]:
        return STORE["customers"][GUEST_CUSTOMER_ID]

    guest = next(
        (c for c in STORE["customers"].values() if c.get("is_guest")), None,
    )
    if guest:
        return guest

    new_id = next_id(STORE["customers"])
    now = datetime.now(timezone.utc)
    guest = {
        "id": new_id,
        "name": "زائر",
        "email": f"guest{new_id}@jamal.local",
        "phone": "",
        "country": "",
        "initials": "ز",
        "picture": None,
        "balance": 0.0,
        "currency": "USD",
        "created_at": now.strftime("%Y-%m-%d"),
        "email_verified": False,
        "phone_verified": False,
        "two_fa_enabled": False,
        "has_strong_password": False,
        "security_score": 0,
        "is_guest": True,
        "prefs": {
            "email_notifications": False,
            "sms_notifications": False,
            "marketing_emails": False,
        },
    }
    STORE["customers"][new_id] = guest
    persist_now()
    print(f"[guest] ✨ created guest customer #{new_id}")
    return guest


def get_customer_from_session(request: Request) -> dict | None:
    if GUEST_MODE:
        return get_or_create_guest()

    try:
        cid = request.session.get("customer_id")
    except Exception:
        return None

    if not cid:
        return None

    cid_int = cid
    try:
        cid_int = int(cid)
    except (ValueError, TypeError):
        pass

    if cid_int in STORE["customers"]:
        return STORE["customers"][cid_int]
    if cid in STORE["customers"]:
        return STORE["customers"][cid]

    email = request.session.get("email")
    name = request.session.get("name")
    picture = request.session.get("picture")

    if not email:
        return None

    customer = {
        "id": cid_int,
        "name": name or email.split("@")[0],
        "email": email,
        "phone": "",
        "country": "",
        "initials": (name or email)[:2],
        "picture": picture,
        "balance": 0.0,
        "currency": "USD",
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "email_verified": True,
        "phone_verified": False,
        "two_fa_enabled": False,
        "has_strong_password": False,
        "security_score": 40,
        "prefs": {
            "email_notifications": True,
            "sms_notifications": False,
            "marketing_emails": False,
        },
    }
    STORE["customers"][cid_int] = customer
    persist_now()
    print(f"♻️  أُعيد إنشاء العميل #{cid_int} ({email}) من الجلسة.")
    return customer


# ══════════════════════════════════════════════════════════════
# My * queries
# ══════════════════════════════════════════════════════════════
async def get_my_stats(customer_id: int) -> dict:
    nums = [n for n in STORE["numbers"] if n["customer_id"] == customer_id]
    ords = [o for o in STORE["orders"] if o["customer_id"] == customer_id]
    sms = [s for s in STORE["sms"] if s["customer_id"] == customer_id]
    txs = [t for t in STORE["transactions"] if t["customer_id"] == customer_id]

    today = datetime.now(timezone.utc).date()
    sms_today = sum(
        1 for s in sms
        if datetime.fromisoformat(s["created_at"]).date() == today
    )
    total_spent = sum(-t["amount"] for t in txs if t["type"] == "purchase")
    month_start = today.replace(day=1)
    spent_this_month = sum(
        -t["amount"] for t in txs
        if t["type"] == "purchase"
        and datetime.fromisoformat(t["created_at"]).date() >= month_start
    )

    return {
        "my_numbers": len(nums),
        "active_numbers": sum(1 for n in nums if n["status"] == "active"),
        "expiring_soon": sum(1 for n in nums if n["status"] == "expiring"),
        "my_orders": len(ords),
        "total_orders": len(ords),
        "pending_orders": sum(1 for o in ords if o["status"] in ("PENDING", "pending")),
        "active_orders": sum(1 for o in ords if o["status"] in ("RECEIVED", "active")),
        "completed_orders": sum(1 for o in ords if o["status"] in ("FINISHED", "completed")),
        "my_sms": len(sms),
        "sms_today": sms_today,
        "unread_sms": sum(1 for s in sms if not s["read"]),
        "total_spent": total_spent,
        "spent_this_month": spent_this_month,
    }


async def get_my_orders_by_status(customer_id: int) -> dict:
    buckets = {"pending": 0, "active": 0, "completed": 0, "expired": 0, "cancelled": 0}
    for o in STORE["orders"]:
        if o["customer_id"] != customer_id:
            continue
        st = (o.get("status") or "").upper()
        if st == "PENDING":
            buckets["pending"] += 1
        elif st == "RECEIVED":
            buckets["active"] += 1
        elif st == "FINISHED":
            buckets["completed"] += 1
        elif st in ("TIMEOUT", "EXPIRED"):
            buckets["expired"] += 1
        elif st in ("CANCELED", "CANCELLED", "BANNED"):
            buckets["cancelled"] += 1
    return buckets


async def get_my_top_services(customer_id: int) -> list[dict]:
    counts: dict[str, int] = {}
    for n in STORE["numbers"]:
        if n["customer_id"] == customer_id:
            sid = n.get("service_id") or n.get("service") or ""
            counts[sid] = counts.get(sid, 0) + 1
    rows = [{"name": sid, "icon": service_icon(sid), "count": cnt}
            for sid, cnt in counts.items()]
    rows.sort(key=lambda r: r["count"], reverse=True)
    return rows[:5]


async def get_my_numbers(customer_id: int) -> list[dict]:
    return [n for n in STORE["numbers"] if n["customer_id"] == customer_id]


async def get_my_recent_sms(customer_id: int, limit: int = 20) -> list[dict]:
    rows = [s for s in STORE["sms"] if s["customer_id"] == customer_id]
    rows.sort(key=lambda s: s["created_at"], reverse=True)
    return rows[:limit]
