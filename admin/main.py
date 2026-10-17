# admin/main.py
"""
Jamal Solutions — Admin Panel (standalone app)

- يقدّم /admin-panel  → templates/admin.html
- يوفّر /api/v1/admin/* بنفس عقد التطبيق الرئيسي (Pasted text.txt)
- يستورد STORE / _sim5_get / Sim5Error / FIVESIM_MARKUP_PERCENT من main.py
  لتجنّب أي تعارض في owner_wallet أو sim5.

التشغيل:
    # من داخل admin/
    uvicorn main:app --host 0.0.0.0 --port 8001

متطلبات البيئة:
    ADMIN_SECRET      (إلزامي — وإلا كل /api/v1/admin/* → 503)
    SIM5_API_KEY      (اختياري — لتفعيل sim5 في /wallet)
    FIVESIM_MARKUP_PERCENT  (افتراضي 40)
"""
from __future__ import annotations

import os
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field


# ══════════════════════════════════════════════════════════════
# 1) استيراد من التطبيق الرئيسي — لا نعيد تعريف STORE أو _sim5_get
# ══════════════════════════════════════════════════════════════
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:
    # التطبيق الرئيسي: main.py في جذر المشروع
    from main import (  # type: ignore
        STORE,
        SIM5_ENABLED,
        FIVESIM_MARKUP_PERCENT,
        Sim5Error,
        _sim5_get,
        _create_vouchers_batch,
        _create_access_code,
        _now_iso,
        _iso_to_flag,  # noqa: F401  (متاح للاستخدام لو احتجته)
    )
    _IMPORT_MODE = "main.py"
except Exception as _imp_err:  # pragma: no cover
    # fallback: لم نجد main.py — نعمل مستقلاً (لا يجب أن يحدث في الإنتاج)
    print(f"[admin] ⚠️  تعذّر استيراد main.py: {_imp_err}")
    print("[admin] ⚠️  سيعمل الأدمن بمخزن مستقل — owner_wallet لن يتزامن!")
    STORE = {
        "customers": {},
        "numbers": [],
        "orders": [],
        "sms": [],
        "transactions": [],
        "deposits": [],
        "tickets": [],
        "sessions": [],
        "vouchers": [],
        "access_codes": [],
        "owner_wallet": {
            "total_profit": 0.0,
            "total_revenue": 0.0,
            "reserved_for_5sim": 0.0,
        },
    }
    SIM5_ENABLED = bool(os.getenv("SIM5_API_KEY", "").strip())
    FIVESIM_MARKUP_PERCENT = float(os.getenv("FIVESIM_MARKUP_PERCENT", "40"))

    class Sim5Error(Exception):
        def __init__(self, status_code: int, message: str, endpoint: str = ""):
            self.status = status_code
            self.message = message
            self.endpoint = endpoint
            super().__init__(f"[{status_code}] {message} ({endpoint})")

    async def _sim5_get(path: str, auth: bool = False, timeout: float = 25.0):  # type: ignore
        raise Sim5Error(503, "5SIM غير مفعّل (وضع مستقل)", path)

    def _create_vouchers_batch(count, value, expires_days=30, note=""):  # type: ignore
        raise RuntimeError("main.py غير متاح")

    def _create_access_code(**kwargs):  # type: ignore
        raise RuntimeError("main.py غير متاح")

    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()

    _IMPORT_MODE = "standalone"


# ══════════════════════════════════════════════════════════════
# 2) مسارات القوالب — نستخدم نفس templates/ في جذر المشروع
# ══════════════════════════════════════════════════════════════
TEMPLATES_DIR = PROJECT_ROOT / "templates"
STATIC_DIR = PROJECT_ROOT / "static"


# ══════════════════════════════════════════════════════════════
# 3) App
# ══════════════════════════════════════════════════════════════
app = FastAPI(title="Jamal Solutions — Admin Panel", version="1.5.0")

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ══════════════════════════════════════════════════════════════
# 4) Auth — نفس آلية _check_admin في main.py
# ══════════════════════════════════════════════════════════════
ADMIN_SECRET = os.getenv("ADMIN_SECRET", "").strip()


def _check_admin(request: Request) -> None:
    """تطابق حرفي مع main.py — 503 إذا لم يُضبط، 403 إذا خاطئ."""
    if not ADMIN_SECRET:
        raise HTTPException(503, "ADMIN_SECRET غير مضبوط")
    provided = request.headers.get("X-Admin-Secret", "")
    if not secrets.compare_digest(provided, ADMIN_SECRET):
        raise HTTPException(403, "غير مصرح")


# ══════════════════════════════════════════════════════════════
# 5) Pydantic models — مطابقة لـ main.py
# ══════════════════════════════════════════════════════════════
class VoucherBatchCreate(BaseModel):
    count: int = Field(..., ge=1, le=1000)
    value: float = Field(..., ge=1)
    expires_days: int = Field(30, ge=1, le=365)
    note: str = ""


class AccessCodeCreate(BaseModel):
    label: str = ""
    expires_days: int = Field(30, ge=1, le=365)
    max_uses: int = Field(1, ge=1, le=100)
    preset_name: str = ""
    preset_email: str = ""
    preset_balance: float = Field(0.0, ge=0)


# ══════════════════════════════════════════════════════════════
# 6) Pages
# ══════════════════════════════════════════════════════════════
@app.get("/admin-panel", response_class=HTMLResponse)
async def admin_panel(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="admin.html",
        context={},
    )


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "app": "admin",
        "version": app.version,
        "import_mode": _IMPORT_MODE,
        "admin_secret_configured": bool(ADMIN_SECRET),
        "templates_dir": str(TEMPLATES_DIR),
        "template_exists": (TEMPLATES_DIR / "admin.html").exists(),
        "store_keys": sorted(STORE.keys()),
    }


# ══════════════════════════════════════════════════════════════
# 7) /api/v1/admin/wallet
# ══════════════════════════════════════════════════════════════
@app.get("/api/v1/admin/wallet")
async def admin_owner_wallet(request: Request):
    _check_admin(request)
    ow = STORE["owner_wallet"]

    sim5_info: dict = {}
    if SIM5_ENABLED:
        try:
            p = await _sim5_get("/user/profile", auth=True)
            sim5_info = {
                "balance": p.get("balance"),
                "frozen": p.get("frozen_balance"),
            }
        except Sim5Error as e:
            sim5_info = {"error": e.message}
        except Exception as e:
            sim5_info = {"error": f"{type(e).__name__}: {e}"}

    return {
        "owner_wallet": ow,
        "sim5": sim5_info,
        "vouchers_summary": {
            "unused_value": sum(
                v["value"] for v in STORE["vouchers"] if v["status"] == "unused"
            ),
        },
        "markup_percent": FIVESIM_MARKUP_PERCENT,
    }


# ══════════════════════════════════════════════════════════════
# 8) /api/v1/admin/vouchers  (GET)
# ══════════════════════════════════════════════════════════════
@app.get("/api/v1/admin/vouchers")
async def admin_list_vouchers(
    request: Request,
    status_filter: Optional[str] = None,
    batch_id: Optional[str] = None,
):
    _check_admin(request)
    vouchers = STORE["vouchers"]

    # تحديث المنتهية ديناميكياً (مطابق لسلوك main.py عند الاستخدام)
    now = datetime.now(timezone.utc)
    for v in vouchers:
        if v.get("status") == "unused":
            try:
                exp = datetime.fromisoformat(v["expires_at"])
                if exp.tzinfo is None:
                    exp = exp.replace(tzinfo=timezone.utc)
                if now > exp:
                    v["status"] = "expired"
            except (ValueError, KeyError, TypeError):
                pass

    filtered = vouchers
    if status_filter:
        filtered = [v for v in filtered if v["status"] == status_filter]
    if batch_id:
        filtered = [v for v in filtered if v.get("batch_id") == batch_id]

    summary = {
        "total": len(vouchers),
        "unused": sum(1 for v in vouchers if v["status"] == "unused"),
        "used": sum(1 for v in vouchers if v["status"] == "used"),
        "expired": sum(1 for v in vouchers if v["status"] == "expired"),
        "disabled": sum(1 for v in vouchers if v["status"] == "disabled"),
        "total_value_unused": round(
            sum(v["value"] for v in vouchers if v["status"] == "unused"), 2
        ),
        "total_value_used": round(
            sum(v["value"] for v in vouchers if v["status"] == "used"), 2
        ),
    }

    return {"summary": summary, "vouchers": filtered}


# ══════════════════════════════════════════════════════════════
# 9) /api/v1/admin/vouchers/generate  (POST)
# ══════════════════════════════════════════════════════════════
@app.post("/api/v1/admin/vouchers/generate")
async def admin_generate_vouchers(payload: VoucherBatchCreate, request: Request):
    _check_admin(request)
    created = _create_vouchers_batch(
        count=payload.count,
        value=payload.value,
        expires_days=payload.expires_days,
        note=payload.note,
    )
    return {
        "ok": True,
        "count": len(created),
        "batch_id": created[0]["batch_id"] if created else None,
        "total_value": round(sum(v["value"] for v in created), 2),
        "codes": [v["code"] for v in created],
    }


# ══════════════════════════════════════════════════════════════
# 10) /api/v1/admin/vouchers/{id}/disable  (POST)
# ══════════════════════════════════════════════════════════════
@app.post("/api/v1/admin/vouchers/{voucher_id}/disable")
async def admin_disable_voucher(voucher_id: int, request: Request):
    _check_admin(request)
    v = next((v for v in STORE["vouchers"] if v["id"] == voucher_id), None)
    if not v:
        raise HTTPException(404, "الكود غير موجود")
    # مطابق main.py: يرفض used فقط
    if v["status"] == "used":
        raise HTTPException(400, "لا يمكن تعطيل كود مستخدم")
    v["status"] = "disabled"
    return {"ok": True, "status": "disabled"}


# ══════════════════════════════════════════════════════════════
# 11) /api/v1/admin/deposits  (GET)
# ══════════════════════════════════════════════════════════════
@app.get("/api/v1/admin/deposits")
async def admin_list_deposits(
    request: Request,
    status_filter: Optional[str] = None,
):
    _check_admin(request)
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


# ══════════════════════════════════════════════════════════════
# 12) /api/v1/admin/deposits/{id}/approve  (POST)
# ══════════════════════════════════════════════════════════════
@app.post("/api/v1/admin/deposits/{deposit_id}/approve")
async def admin_approve_deposit(deposit_id: int, request: Request):
    _check_admin(request)
    deposit = next((d for d in STORE["deposits"] if d["id"] == deposit_id), None)
    if not deposit:
        raise HTTPException(404, "الطلب غير موجود")
    if deposit["status"] != "pending":
        raise HTTPException(400, "تمت مراجعة هذا الطلب مسبقاً")

    customer = STORE["customers"].get(deposit["customer_id"])
    if not customer:
        raise HTTPException(404, "الزبون غير موجود")

    deposit["status"] = "completed"
    deposit["reviewed_at"] = _now_iso()
    customer["balance"] += deposit["amount"]

    STORE["transactions"].append({
        "id": len(STORE["transactions"]) + 1,
        "customer_id": customer["id"],
        "type": "deposit",
        "description": f"إيداع يدوي ({deposit['method']})",
        "amount": deposit["amount"],
        "balance_after": customer["balance"],
        "created_at": _now_iso(),
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
    })

    STORE["owner_wallet"]["total_revenue"] += deposit["amount"]

    return {"ok": True, "new_balance": customer["balance"]}


# ══════════════════════════════════════════════════════════════
# 13) /api/v1/admin/deposits/{id}/reject  (POST)
# ══════════════════════════════════════════════════════════════
@app.post("/api/v1/admin/deposits/{deposit_id}/reject")
async def admin_reject_deposit(deposit_id: int, request: Request, reason: str = ""):
    _check_admin(request)
    deposit = next((d for d in STORE["deposits"] if d["id"] == deposit_id), None)
    if not deposit:
        raise HTTPException(404, "الطلب غير موجود")
    deposit["status"] = "rejected"
    deposit["reviewed_at"] = _now_iso()
    deposit["reject_reason"] = reason
    return {"ok": True, "status": "rejected"}


# ══════════════════════════════════════════════════════════════
# 14) /api/v1/admin/access-codes/generate  (POST)
# ══════════════════════════════════════════════════════════════
@app.post("/api/v1/admin/access-codes/generate")
async def admin_generate_access_code(payload: AccessCodeCreate, request: Request):
    _check_admin(request)
    record = _create_access_code(
        label=payload.label,
        expires_days=payload.expires_days,
        max_uses=payload.max_uses,
        preset_name=payload.preset_name,
        preset_email=payload.preset_email,
        preset_balance=payload.preset_balance,
    )
    return {
        "ok": True,
        "id": record["id"],
        "code": record["code"],
        "expires_at": record["expires_at"],
        "max_uses": record["max_uses"],
        "preset_balance": record["preset_balance"],
    }


# ══════════════════════════════════════════════════════════════
# 15) /api/v1/admin/access-codes  (GET)
# ══════════════════════════════════════════════════════════════
@app.get("/api/v1/admin/access-codes")
async def admin_list_access_codes(
    request: Request,
    status_filter: Optional[str] = None,
):
    _check_admin(request)
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


# ══════════════════════════════════════════════════════════════
# 16) /api/v1/admin/access-codes/{id}/disable  (POST)
# ══════════════════════════════════════════════════════════════
@app.post("/api/v1/admin/access-codes/{code_id}/disable")
async def admin_disable_access_code(code_id: int, request: Request):
    _check_admin(request)
    record = next((c for c in STORE["access_codes"] if c["id"] == code_id), None)
    if not record:
        raise HTTPException(404, "الكود غير موجود")
    record["status"] = "disabled"
    return {"ok": True, "status": "disabled"}


# ══════════════════════════════════════════════════════════════
# 17) /api/v1/admin/access-codes/{id}  (DELETE)
# ══════════════════════════════════════════════════════════════
@app.delete("/api/v1/admin/access-codes/{code_id}")
async def admin_delete_access_code(code_id: int, request: Request):
    _check_admin(request)
    before = len(STORE["access_codes"])
    STORE["access_codes"] = [c for c in STORE["access_codes"] if c["id"] != code_id]
    if len(STORE["access_codes"]) == before:
        raise HTTPException(404, "الكود غير موجود")
    return {"ok": True, "deleted": True}


# ══════════════════════════════════════════════════════════════
# 18) JSON error handler
# ══════════════════════════════════════════════════════════════
@app.exception_handler(HTTPException)
async def http_exc_handler(request: Request, exc: HTTPException):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


# ══════════════════════════════════════════════════════════════
# 19) Startup log — تشخيص سريع في Docker
# ══════════════════════════════════════════════════════════════
@app.on_event("startup")
async def _startup_log():
    print("═" * 60)
    print("[admin] 🚀 بدء لوحة الأدمن")
    print(f"[admin] import_mode       = {_IMPORT_MODE}")
    print(f"[admin] PROJECT_ROOT      = {PROJECT_ROOT}")
    print(f"[admin] TEMPLATES_DIR     = {TEMPLATES_DIR}")
    print(f"[admin] admin.html        = {(TEMPLATES_DIR / 'admin.html').exists()}")
    print(f"[admin] ADMIN_SECRET      = {'set ✅' if ADMIN_SECRET else 'NOT SET ❌'}")
    print(f"[admin] SIM5_ENABLED      = {SIM5_ENABLED}")
    print(f"[admin] MARKUP_PERCENT    = {FIVESIM_MARKUP_PERCENT}")
    print(f"[admin] STORE keys        = {sorted(STORE.keys())}")
    print(f"[admin] owner_wallet      = {STORE['owner_wallet']}")
    print("═" * 60)
