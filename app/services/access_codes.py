"""إنشاء/تحقق أكواد الدخول."""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException

from app.config import ACCESS_CODE_PREFIX
from app.core.storage import STORE


def _gen_access_code() -> str:
    return f"{ACCESS_CODE_PREFIX}-{secrets.token_hex(6).upper()}"


def create_access_code(
    label: str = "",
    expires_days: int = 30,
    max_uses: int = 1,
    preset_name: str = "",
    preset_email: str = "",
    preset_balance: float = 0.0,
) -> dict:
    while True:
        code = _gen_access_code()
        if not any(c["code"] == code for c in STORE["access_codes"]):
            break

    now = datetime.now(timezone.utc)
    record = {
        "id": len(STORE["access_codes"]) + 1,
        "code": code,
        "label": label,
        "status": "active",
        "max_uses": max_uses,
        "uses_count": 0,
        "preset_name": preset_name,
        "preset_email": preset_email,
        "preset_balance": float(preset_balance),
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(days=expires_days)).isoformat(),
        "used_by": [],
        "last_used_at": None,
    }
    STORE["access_codes"].append(record)
    return record


def validate_access_code(code: str) -> dict:
    code = (code or "").strip().upper()
    if not code:
        raise HTTPException(400, "الرجاء إدخال الكود")

    record = next((c for c in STORE["access_codes"] if c["code"] == code), None)
    if not record:
        raise HTTPException(404, "الكود غير صحيح")

    if record["status"] == "disabled":
        raise HTTPException(400, "الكود معطّل")
    if record["status"] == "expired":
        raise HTTPException(400, "انتهت صلاحية الكود")
    if record["status"] == "used" and record["uses_count"] >= record["max_uses"]:
        raise HTTPException(400, "تم استخدام هذا الكود بالكامل")

    try:
        exp = datetime.fromisoformat(record["expires_at"])
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) > exp:
            record["status"] = "expired"
            raise HTTPException(400, "انتهت صلاحية الكود")
    except (ValueError, TypeError):
        pass

    return record


def consume_access_code(record: dict, customer_id: int) -> None:
    record["uses_count"] += 1
    record["used_by"].append(customer_id)
    record["last_used_at"] = datetime.now(timezone.utc).isoformat()
    if record["uses_count"] >= record["max_uses"]:
        record["status"] = "used"
