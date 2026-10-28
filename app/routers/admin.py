"""كل endpoints الأدمن."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from app.config import (
    SIM5_ENABLED,
    FIVESIM_MARKUP_PERCENT,
)
from app.core.storage import STORE, persist_now, refresh
from app.core.state import now_iso
from app.deps import check_admin
from app.models.schemas import (
    VoucherBatchCreate,
    AccessCodeCreate,
    PopularServicesUpdate,
)
from app.services.access_codes import create_access_code
from app.services.sim5 import is_country_blocked, sim5_get, Sim5Error
from app.services.vouchers import create_vouchers_batch


router = APIRouter(prefix="/api/v1/admin")


# ══════════════════════════════════════════════════════════════
# Vouchers
# ══════════════════════════════════════════════════════════════
@router.post("/vouchers/generate")
async def admin_generate_vouchers(payload: VoucherBatchCreate, request: Request):
    check_admin(request)
    refresh()
    created = create_vouchers_batch(
        count=payload.count,
        value=payload.value,
        expires_days=payload.expires_days,
        note=payload.note,
    )
    persist_now()
    return {
        "ok": True,
        "count": len(created),
        "batch_id": created[0]["batch_id"] if created else None,
        "total_value": sum(v["value"] for v in created),
        "codes": [v["code"] for v in created],
    }


@router.get("/vouchers")
async def admin_list_vouchers(
    request: Request,
    status_filter: str | None = None,
    batch_id: str | None = None,
):
    check_admin(request)
    refresh()
    vouchers = STORE["vouchers"]
    if status_filter:
        vouchers = [v for v in vouchers if v["status"] == status_filter]
    if batch_id:
        vouchers = [v for v in vouchers if v["batch_id"] == batch_id]

    summary = {
        "total": len(STORE["vouchers"]),
        "unused": sum(1 for v in STORE["vouchers"] if v["status"] == "unused"),
        "used": sum(1 for v in STORE["vouchers"] if v["status"] == "used"),
        "expired": sum(1 for v in STORE["vouchers"] if v["status"] == "expired"),
        "disabled": sum(1 for v in STORE["vouchers"] if v["status"] == "disabled"),
        "total_value_unused": sum(v["value"] for v in STORE["vouchers"] if v["status"] == "unused"),
        "total_value_used": sum(v["value"] for v in STORE["vouchers"] if v["status"] == "used"),
    }

    return {"summary": summary, "vouchers": vouchers}


@router.post("/vouchers/{voucher_id}/disable")
async def admin_disable_voucher(voucher_id: int, request: Request):
    check_admin(request)
    refresh()
    v = next((v for v in STORE["vouchers"] if v["id"] == voucher_id), None)
    if not v:
        raise HTTPException(404, "الكود غير موجود")
    if v["status"] == "used":
        raise HTTPException(400, "لا يمكن تعطيل كود مستخدم")
    v["status"] = "disabled"
    persist_now()
    return {"ok": True, "status": "disabled"}


# ══════════════════════════════════════════════════════════════
# Wallet
# ══════════════════════════════════════════════════════════════
@router.get("/wallet")
async def admin_owner_wallet(request: Request):
    check_admin(request)
    refresh()
    ow = STORE["owner_wallet"]

    sim5_info: dict = {}
    if SIM5_ENABLED:
        try:
            p = await sim5_get("/user/profile", auth=True)
            sim5_info = {"balance": p.get("balance"), "frozen": p.get("frozen_balance")}
        except Sim5Error as e:
            sim5_info = {"error": e.message}

    return {
        "owner_wallet": ow,
        "sim5": sim5_info,
        "vouchers_summary": {
            "unused_value": sum(v["value"] for v in STORE["vouchers"] if v["status"] == "unused"),
        },
        "markup_percent": FIVESIM_MARKUP_PERCENT,
    }


# ══════════════════════════════════════════════════════════════
# Deposits
# ══════════════════════════════════════════════════════════════
@router.get("/deposits")
async def admin_list_deposits(
    request: Request,
    status_filter: str | None = None,
):
    check_admin(request)
    refresh()
    deposits = STORE["deposits"]
    if status_filter:
        deposits = [d for d in deposits if d["status"] == status_filter]
    deposits_sorted = sorted(deposits, key=lambda d: d["created_at"], reverse=True)

    out = []
    for d in deposits_sorted:
        cust = STORE["customers"].get(d["customer_id"])
        out.append({
            **d,
            "customer_email": cust["email"] if cust else None,
            "customer_name": cust["name"] if cust else None,
        })

    return {
        "summary": {
            "total": len(STORE["deposits"]),
            "pending": sum(1 for d in STORE["deposits"] if d["status"] == "pending"),
            "completed": sum(1 for d in STORE["deposits"] if d["status"] == "completed"),
            "rejected": sum(1 for d in STORE["deposits"] if d["status"] == "rejected"),
        },
        "deposits": out,
    }


@router.post("/deposits/{deposit_id}/approve")
async def admin_approve_deposit(deposit_id: int, request: Request):
    check_admin(request)
    refresh()
    deposit = next((d for d in STORE["deposits"] if d["id"] == deposit_id), None)
    if not deposit:
        raise HTTPException(404, "الطلب غير موجود")
    if deposit["status"] != "pending":
        raise HTTPException(400, "تمت مراجعة هذا الطلب مسبقاً")

    customer = STORE["customers"].get(deposit["customer_id"])
    if not customer:
        raise HTTPException(404, "الزبون غير موجود")

    deposit["status"] = "completed"
    deposit["reviewed_at"] = now_iso()
    customer["balance"] += deposit["amount"]

    STORE["transactions"].append({
        "id": len(STORE["transactions"]) + 1,
        "customer_id": customer["id"],
        "type": "deposit",
        "description": f"إيداع يدوي ({deposit['method']})",
        "amount": deposit["amount"],
        "balance_after": customer["balance"],
        "created_at": now_iso(),
        "date": now_iso()[:16].replace("T", " "),
    })

    STORE["owner_wallet"]["total_revenue"] += deposit["amount"]
    persist_now()

    return {"ok": True, "new_balance": customer["balance"]}


@router.post("/deposits/{deposit_id}/reject")
async def admin_reject_deposit(deposit_id: int, request: Request, reason: str = ""):
    check_admin(request)
    refresh()
    deposit = next((d for d in STORE["deposits"] if d["id"] == deposit_id), None)
    if not deposit:
        raise HTTPException(404, "الطلب غير موجود")
    deposit["status"] = "rejected"
    deposit["reviewed_at"] = now_iso()
    deposit["reject_reason"] = reason
    persist_now()
    return {"ok": True, "status": "rejected"}


# ══════════════════════════════════════════════════════════════
# Access codes
# ══════════════════════════════════════════════════════════════
@router.post("/access-codes/generate")
async def admin_generate_access_code(payload: AccessCodeCreate, request: Request):
    check_admin(request)
    refresh()
    record = create_access_code(
        label=payload.label,
        expires_days=payload.expires_days,
        max_uses=payload.max_uses,
        preset_name=payload.preset_name,
        preset_email=payload.preset_email,
        preset_balance=payload.preset_balance,
    )
    persist_now()
    return {
        "ok": True,
        "id": record["id"],
        "code": record["code"],
        "expires_at": record["expires_at"],
        "max_uses": record["max_uses"],
        "preset_balance": record["preset_balance"],
    }


@router.get("/access-codes")
async def admin_list_access_codes(
    request: Request,
    status_filter: str | None = None,
):
    check_admin(request)
    refresh()
    codes = STORE["access_codes"]
    if status_filter:
        codes = [c for c in codes if c["status"] == status_filter]

    summary = {
        "total": len(STORE["access_codes"]),
        "active": sum(1 for c in STORE["access_codes"] if c["status"] == "active"),
        "used": sum(1 for c in STORE["access_codes"] if c["status"] == "used"),
        "disabled": sum(1 for c in STORE["access_codes"] if c["status"] == "disabled"),
        "expired": sum(1 for c in STORE["access_codes"] if c["status"] == "expired"),
    }
    return {"summary": summary, "codes": list(reversed(codes))}


@router.post("/access-codes/{code_id}/disable")
async def admin_disable_access_code(code_id: int, request: Request):
    check_admin(request)
    refresh()
    record = next((c for c in STORE["access_codes"] if c["id"] == code_id), None)
    if not record:
        raise HTTPException(404, "الكود غير موجود")
    record["status"] = "disabled"
    persist_now()
    return {"ok": True, "status": "disabled"}


@router.delete("/access-codes/{code_id}")
async def admin_delete_access_code(code_id: int, request: Request):
    check_admin(request)
    refresh()
    before = len(STORE["access_codes"])
    STORE["access_codes"] = [c for c in STORE["access_codes"] if c["id"] != code_id]
    if len(STORE["access_codes"]) == before:
        raise HTTPException(404, "الكود غير موجود")
    persist_now()
    return {"ok": True, "deleted": True}


# ══════════════════════════════════════════════════════════════
# Popular services
# ══════════════════════════════════════════════════════════════
@router.get("/popular")
async def admin_list_popular(request: Request):
    check_admin(request)
    refresh()
    return {"ok": True, "popular": STORE.get("popular_services", [])}


@router.put("/popular")
async def admin_set_popular(payload: PopularServicesUpdate, request: Request):
    check_admin(request)
    refresh()
    items = []
    for it in payload.items:
        c = it.country_slug.strip().lower()
        s = it.service_id.strip().lower()
        if not c or not s:
            continue
        if is_country_blocked(c):
            print(f"[admin_set_popular] 🚫 رفض '{c}/{s}' — دولة محظورة")
            continue
        items.append({
            "country_slug": c,
            "service_id": s,
            "order": int(it.order),
        })
    items.sort(key=lambda x: x["order"])
    STORE["popular_services"] = items
    persist_now()
    return {"ok": True, "count": len(items), "popular": items}


@router.delete("/popular")
async def admin_clear_popular(request: Request):
    check_admin(request)
    refresh()
    STORE["popular_services"] = []
    persist_now()
    return {"ok": True, "count": 0}
