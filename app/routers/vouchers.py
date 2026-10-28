"""التحقق من أكواد الشحن (endpoint عام)."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from app.config import GUEST_MODE
from app.core.storage import refresh
from app.deps import get_current_customer
from app.models.schemas import VoucherCheck
from app.services.sim5 import is_country_blocked
from app.services.vouchers import validate_voucher, voucher_allows


router = APIRouter()


@router.post("/api/v1/vouchers/check")
async def check_voucher_compat(payload: VoucherCheck, request: Request):
    """
    يتحقق أن الكود:
      - موجود
      - غير مستخدم/معطّل
      - غير منتهي
      - (اختياري) يصلح للزوج (country, service) المطلوب

    لا يستهلك الكود — آمن للاستدعاء من واجهة الزبون.
    """
    refresh()
    if not GUEST_MODE:
        await get_current_customer(request)

    v = validate_voucher(payload.code)
    pairs = v["allowed_pairs"]
    selected_pair = None

    if payload.country and payload.service:
        c = payload.country.strip().lower()
        s = payload.service.strip().lower()

        if is_country_blocked(c):
            raise HTTPException(400, "هذه الدولة غير متاحة")

        if not voucher_allows(v, c, s):
            pairs_str = ", ".join(f"{p['country']}/{p['service']}" for p in pairs) or "بدون قيود"
            raise HTTPException(
                403,
                f"هذا الكود لا يصلح لـ {c}/{s}. يصلح لـ: {pairs_str}",
            )
        selected_pair = {"country": c, "service": s}

    return {
        "ok": True,
        "code": v["code"],
        "value": v["value"],
        "label": v.get("label", ""),
        "expires_at": v["expires_at"],
        "allowed_pairs": pairs,
        "is_merged": len(pairs) > 1,
        "note": v.get("note", ""),
        "selected_pair": selected_pair,
    }
