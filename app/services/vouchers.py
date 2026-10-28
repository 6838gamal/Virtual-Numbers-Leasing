"""إنشاء/تحقق/استهلاك أكواد الشحن."""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException

from app.config import VOUCHER_PREFIX
from app.core.storage import STORE, persist_now
from app.services.sim5 import is_country_blocked


def normalize_voucher_pairs(v: dict) -> list[dict]:
    pairs = v.get("allowed_pairs") or []
    if pairs:
        return pairs
    if v.get("country") and v.get("service"):
        return [{"country": v["country"], "service": v["service"]}]
    return []


def voucher_allows(v: dict, country: str, service: str) -> bool:
    pairs = normalize_voucher_pairs(v)
    if not pairs:
        return True
    return any(p["country"] == country and p["service"] == service for p in pairs)


def _gen_voucher_code(value: float) -> str:
    part1 = secrets.token_hex(2).upper()
    part2 = secrets.token_hex(2).upper()
    val_str = f"{value:g}".replace(".", "")
    return f"{VOUCHER_PREFIX}-{val_str}-{part1}-{part2}"


def create_vouchers_batch(
    count: int,
    value: float,
    expires_days: int = 30,
    note: str = "",
    country: str | None = None,
    service: str | None = None,
    allowed_pairs: list[dict] | None = None,
) -> list[dict]:
    batch_id = f"batch_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
    now = datetime.now(timezone.utc)
    expires = now + timedelta(days=expires_days)

    pairs: list[dict] = []
    if allowed_pairs:
        for p in allowed_pairs:
            c = (p.get("country") or "").strip()
            s = (p.get("service") or "").strip()
            if c and s:
                pairs.append({"country": c, "service": s})
    if not pairs and country and service:
        pairs = [{"country": country, "service": service}]

    created: list[dict] = []

    for _ in range(count):
        while True:
            code = _gen_voucher_code(value)
            if not any(v["code"] == code for v in STORE["vouchers"]):
                break

        single = pairs[0] if len(pairs) == 1 else None
        voucher = {
            "id": len(STORE["vouchers"]) + 1,
            "code": code,
            "value": float(value),
            "status": "unused",
            "batch_id": batch_id,
            "note": note,
            "country": single["country"] if single else (country or None),
            "service": single["service"] if single else (service or None),
            "allowed_pairs": pairs,
            "created_at": now.isoformat(),
            "expires_at": expires.isoformat(),
            "used_by": None,
            "used_at": None,
            "used_on": None,
        }
        STORE["vouchers"].append(voucher)
        created.append(voucher)

    return created


def validate_voucher(code: str) -> dict:
    code = code.strip().upper()
    v = next((v for v in STORE["vouchers"] if v["code"] == code), None)
    if not v:
        raise HTTPException(404, "كود غير موجود")

    if v["status"] == "used":
        raise HTTPException(400, "الكود مستخدم مسبقاً")
    if v["status"] == "disabled":
        raise HTTPException(400, "الكود معطّل")
    if v["status"] == "expired":
        raise HTTPException(400, "انتهت صلاحية الكود")

    try:
        exp = datetime.fromisoformat(v["expires_at"])
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) > exp:
            v["status"] = "expired"
            raise HTTPException(400, "انتهت صلاحية الكود")
    except (ValueError, TypeError):
        pass

    v["allowed_pairs"] = normalize_voucher_pairs(v)
    return v


def ensure_voucher_allows(v: dict, country: str, service: str) -> None:
    """يرفع HTTPException إن كانت الدولة محظورة أو الكود لا يصلح للزوج."""
    c = country.strip().lower()
    s = service.strip().lower()

    if is_country_blocked(c):
        raise HTTPException(400, "هذه الدولة غير متاحة")

    if not voucher_allows(v, c, s):
        pairs_str = ", ".join(
            f"{p['country']}/{p['service']}" for p in v["allowed_pairs"]
        ) or "بدون قيود"
        raise HTTPException(
            403,
            f"هذا الكود لا يصلح لـ {c}/{s}. يصلح لـ: {pairs_str}",
        )
