"""المحفظة: استرداد كود + طلب إيداع."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request

from app.config import GUEST_MODE
from app.core.storage import STORE, persist_now, refresh
from app.core.state import now_iso
from app.deps import get_current_customer
from app.models.schemas import VoucherRedeem, DepositRequestCreate
from app.services.sim5 import is_country_blocked
from app.services.vouchers import validate_voucher, voucher_allows


router = APIRouter()


@router.post("/api/v1/wallet/redeem-voucher")
async def redeem_voucher(payload: VoucherRedeem, request: Request):
    refresh()
    if GUEST_MODE:
        raise HTTPException(403, "تفعيل أكواد الشحن غير متاح في وضع الزائر")
    customer = await get_current_customer(request)
    cid = customer["id"]

    voucher = validate_voucher(payload.code)

    if payload.country and payload.service:
        if is_country_blocked(payload.country):
            raise HTTPException(400, "هذه الدولة غير متاحة")
        if not voucher_allows(voucher, payload.country, payload.service):
            pairs_str = ", ".join(
                f"{p['country']}/{p['service']}"
                for p in voucher["allowed_pairs"]
            )
            raise HTTPException(
                403,
                f"هذا الكود لا يصلح لـ {payload.country}/{payload.service}. "
                f"يصلح لـ: {pairs_str}",
            )

    now = datetime.now(timezone.utc)
    voucher["status"] = "used"
    voucher["used_by"] = cid
    voucher["used_at"] = now.isoformat()
    if payload.country and payload.service:
        voucher["used_on"] = {
            "country": payload.country,
            "service": payload.service,
        }

    customer["balance"] += voucher["value"]

    STORE["transactions"].append({
        "id": len(STORE["transactions"]) + 1,
        "customer_id": cid,
        "type": "voucher",
        "description": f"تفعيل كود {voucher['code']}",
        "amount": voucher["value"],
        "balance_after": customer["balance"],
        "created_at": now.isoformat(),
        "date": now.strftime("%Y-%m-%d %H:%M"),
    })

    STORE["owner_wallet"]["total_revenue"] += voucher["value"]
    persist_now()

    return {
        "status": "success",
        "value": voucher["value"],
        "new_balance": customer["balance"],
        "allowed_pairs": voucher["allowed_pairs"],
    }


@router.post("/api/v1/wallet/deposit-request")
async def create_deposit_request(payload: DepositRequestCreate, request: Request):
    refresh()
    if GUEST_MODE:
        raise HTTPException(403, "طلبات الإيداع غير متاحة في وضع الزائر")
    customer = await get_current_customer(request)

    deposit = {
        "id": len(STORE["deposits"]) + 1,
        "customer_id": customer["id"],
        "amount": payload.amount,
        "method": payload.method,
        "reference": payload.reference,
        "status": "pending",
        "created_at": now_iso(),
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
        "reviewed_at": None,
    }
    STORE["deposits"].append(deposit)
    persist_now()

    return {
        "status": "pending",
        "request_id": deposit["id"],
        "message": "طلبك قيد المراجعة",
    }
