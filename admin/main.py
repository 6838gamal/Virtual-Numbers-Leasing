# admin/main.py
"""
FastAPI Admin Panel — يقدّم templates/admin.html ويوفّر واجهات /api/v1/admin/*
"""
from __future__ import annotations

import os
import secrets
import string
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Query
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

# ──────────────────────────────────────────────────────────────
# Paths
# ──────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

# ──────────────────────────────────────────────────────────────
# App
# ──────────────────────────────────────────────────────────────
app = FastAPI(title="Jamal Solutions — Admin Panel")

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ──────────────────────────────────────────────────────────────
# Auth dependency
# ──────────────────────────────────────────────────────────────
ADMIN_SECRET = os.getenv("ADMIN_SECRET", "")


def require_admin(x_admin_secret: Optional[str] = Header(default=None, alias="X-Admin-Secret")) -> None:
    """تحقّق من ترويسة X-Admin-Secret مقابل متغيّر البيئة ADMIN_SECRET."""
    if not ADMIN_SECRET:
        # متغيّر البيئة غير مضبوط — القالب يتوقع 503
        raise HTTPException(status_code=503, detail="ADMIN_SECRET is not configured on the server")
    if not x_admin_secret or not secrets.compare_digest(x_admin_secret, ADMIN_SECRET):
        raise HTTPException(status_code=403, detail="Invalid admin secret")


# ──────────────────────────────────────────────────────────────
# In-memory store (نسخة مبسطة — استبدلها بقاعدة بيانات)
# ──────────────────────────────────────────────────────────────
_CODE_ALPHABET = string.ascii_uppercase + string.digits


def _new_code(prefix: str = "JMS") -> str:
    body = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(12))
    return f"{prefix}-{body[:4]}-{body[4:8]}-{body[8:12]}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# المستخدم: {"email":..., "name":..., "balance":float}
USERS: dict[str, dict] = {
    "demo@jamal.dev": {"email": "demo@jamal.dev", "name": "Demo User", "balance": 0.0},
}

# الأكواد: dict[id, voucher]
VOUCHERS: dict[int, dict] = {}
_next_voucher_id = 1

# طلبات الإيداع
DEPOSITS: dict[int, dict] = {}
_next_deposit_id = 1

# محفظة المالك
OWNER_WALLET = {
    "total_profit": 0.0,
    "total_revenue": 0.0,
    "reserved_for_5sim": 0.0,
}
SIM5_STATE = {"balance": "—", "frozen": "—", "error": False}
MARKUP_PERCENT = 15  # نسبة هامش الربح المعروضة


# ──────────────────────────────────────────────────────────────
# Pydantic models
# ──────────────────────────────────────────────────────────────
class VoucherGenerateIn(BaseModel):
    count: int = Field(..., ge=1, le=1000)
    value: float = Field(..., ge=1)
    expires_days: int = Field(30, ge=1, le=365)
    note: str = ""


# ──────────────────────────────────────────────────────────────
# Pages
# ──────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    return templates.TemplateResponse(request=request, name="admin.html", context={})


@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    return templates.TemplateResponse(request=request, name="admin.html", context={})


@app.get("/health")
async def health():
    return {"status": "ok", "admin_secret_configured": bool(ADMIN_SECRET)}


# ──────────────────────────────────────────────────────────────
# /api/v1/admin/wallet
# ──────────────────────────────────────────────────────────────
@app.get("/api/v1/admin/wallet", dependencies=[Depends(require_admin)])
async def get_wallet():
    return {
        "owner_wallet": OWNER_WALLET,
        "sim5": SIM5_STATE,
        "markup_percent": MARKUP_PERCENT,
    }


# ──────────────────────────────────────────────────────────────
# /api/v1/admin/vouchers  (GET)
# ──────────────────────────────────────────────────────────────
@app.get("/api/v1/admin/vouchers", dependencies=[Depends(require_admin)])
async def list_vouchers(status_filter: Optional[str] = Query(default=None)):
    items = list(VOUCHERS.values())

    # حساب الملخّص قبل الفلترة (كما يتوقع القالب من الإجمالي الكلي)
    now = datetime.now(timezone.utc)
    for v in items:
        exp = datetime.fromisoformat(v["expires_at"])
        if v["status"] == "unused" and exp < now:
            v["status"] = "expired"

    summary = {
        "total": len(items),
        "unused": sum(1 for v in items if v["status"] == "unused"),
        "used": sum(1 for v in items if v["status"] == "used"),
        "expired": sum(1 for v in items if v["status"] == "expired"),
        "disabled": sum(1 for v in items if v["status"] == "disabled"),
        "total_value_unused": sum(v["value"] for v in items if v["status"] == "unused"),
    }

    if status_filter:
        items = [v for v in items if v["status"] == status_filter]

    return {"vouchers": items, "summary": summary}


# ──────────────────────────────────────────────────────────────
# /api/v1/admin/vouchers/generate  (POST)
# ──────────────────────────────────────────────────────────────
@app.post("/api/v1/admin/vouchers/generate", dependencies=[Depends(require_admin)])
async def generate_vouchers(payload: VoucherGenerateIn):
    global _next_voucher_id

    created_at = datetime.now(timezone.utc)
    expires_at = created_at + timedelta(days=payload.expires_days)

    codes: list[str] = []
    for _ in range(payload.count):
        code = _new_code()
        # ضمان عدم التكرار
        while any(v["code"] == code for v in VOUCHERS.values()):
            code = _new_code()

        VOUCHERS[_next_voucher_id] = {
            "id": _next_voucher_id,
            "code": code,
            "value": float(payload.value),
            "status": "unused",
            "used_by": None,
            "created_at": created_at.isoformat(),
            "expires_at": expires_at.isoformat(),
            "note": payload.note,
        }
        codes.append(code)
        _next_voucher_id += 1

    return {
        "count": payload.count,
        "value": payload.value,
        "total_value": round(payload.value * payload.count, 2),
        "codes": codes,
        "expires_at": expires_at.isoformat(),
    }


# ──────────────────────────────────────────────────────────────
# /api/v1/admin/vouchers/{id}/disable  (POST)
# ──────────────────────────────────────────────────────────────
@app.post("/api/v1/admin/vouchers/{voucher_id}/disable", dependencies=[Depends(require_admin)])
async def disable_voucher(voucher_id: int):
    v = VOUCHERS.get(voucher_id)
    if not v:
        raise HTTPException(404, "Voucher not found")
    if v["status"] != "unused":
        raise HTTPException(400, f"Cannot disable a voucher with status '{v['status']}'")
    v["status"] = "disabled"
    return {"ok": True, "id": voucher_id}


# ──────────────────────────────────────────────────────────────
# /api/v1/admin/deposits  (GET)
# ──────────────────────────────────────────────────────────────
@app.get("/api/v1/admin/deposits", dependencies=[Depends(require_admin)])
async def list_deposits(status_filter: Optional[str] = Query(default=None)):
    items = list(DEPOSITS.values())

    summary = {
        "total": len(items),
        "pending": sum(1 for d in items if d["status"] == "pending"),
        "completed": sum(1 for d in items if d["status"] == "completed"),
        "rejected": sum(1 for d in items if d["status"] == "rejected"),
    }

    if status_filter:
        items = [d for d in items if d["status"] == status_filter]

    return {"deposits": items, "summary": summary}


# ──────────────────────────────────────────────────────────────
# /api/v1/admin/deposits/{id}/approve  (POST)
# ──────────────────────────────────────────────────────────────
@app.post("/api/v1/admin/deposits/{deposit_id}/approve", dependencies=[Depends(require_admin)])
async def approve_deposit(deposit_id: int):
    d = DEPOSITS.get(deposit_id)
    if not d:
        raise HTTPException(404, "Deposit not found")
    if d["status"] != "pending":
        raise HTTPException(400, f"Deposit already '{d['status']}'")

    # إضافة الرصيد للزبون
    email = d["customer_email"]
    user = USERS.setdefault(email, {"email": email, "name": d.get("customer_name") or email, "balance": 0.0})
    user["balance"] = round(user["balance"] + d["amount"], 2)

    d["status"] = "completed"
    d["completed_at"] = _now_iso()

    # تحديث محفظة المالك
    OWNER_WALLET["total_revenue"] = round(OWNER_WALLET["total_revenue"] + d["amount"], 2)
    OWNER_WALLET["total_profit"] = round(OWNER_WALLET["total_profit"] + d["amount"], 2)

    return {"ok": True, "id": deposit_id, "new_balance": user["balance"]}


# ──────────────────────────────────────────────────────────────
# /api/v1/admin/deposits/{id}/reject  (POST)
# ──────────────────────────────────────────────────────────────
@app.post("/api/v1/admin/deposits/{deposit_id}/reject", dependencies=[Depends(require_admin)])
async def reject_deposit(deposit_id: int, reason: str = ""):
    d = DEPOSITS.get(deposit_id)
    if not d:
        raise HTTPException(404, "Deposit not found")
    if d["status"] != "pending":
        raise HTTPException(400, f"Deposit already '{d['status']}'")

    d["status"] = "rejected"
    d["rejected_at"] = _now_iso()
    d["reject_reason"] = reason
    return {"ok": True, "id": deposit_id}


# ──────────────────────────────────────────────────────────────
# Exception handler: تأكيد JSON موحّد للأخطاء (اختياري)
# ──────────────────────────────────────────────────────────────
@app.exception_handler(HTTPException)
async def http_exc_handler(request: Request, exc: HTTPException):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
