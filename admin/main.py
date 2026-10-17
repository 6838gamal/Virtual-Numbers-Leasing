# admin/main.py
"""
Jamal Solutions — Admin Panel (standalone)

- GET  /                       → login.html   (نفس /login)
- GET  /login                  → singin.html
- GET  /admin-panel            → admin.html   (JS يحمي)
- POST /api/v1/admin/login     → {ok, token}
- POST /api/v1/admin/logout    → {ok}
- GET  /api/v1/admin/whoami    → {ok, email, auth}
- كل /api/v1/admin/*           → محمي بـ X-Admin-Secret = token أو ADMIN_SECRET

admin/ هو الجذر. المسارات مكتوبة صراحةً:
    admin/templates/  ← القوالب
    admin/static/     ← الستاتيك

يحاول استيراد STORE و_sim5_get من main.py (إن وُجد بجانب admin/) لتزامن owner_wallet.

التشغيل (من داخل admin/):
    uvicorn main:app --host 0.0.0.0 --port 8001

متغيرات البيئة:
    ADMIN_EMAIL           (افتراضي: admin@virtual-num.com)
    ADMIN_PASSWORD        (افتراضي: Virtual123) — للتطوير
    ADMIN_PASSWORD_HASH   (bcrypt) — الأفضل للإنتاج
    ADMIN_SECRET          (اختياري — توافق قديم)
    SIM5_API_KEY, FIVESIM_MARKUP_PERCENT
"""
from __future__ import annotations

import os
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field


# ══════════════════════════════════════════════════════════════
# 1) Paths — admin/ هو الجذر، والمسارات مكتوبة صراحةً
# ══════════════════════════════════════════════════════════════
ADMIN_DIR = Path(__file__).resolve().parent          # admin/
TEMPLATES_DIR = ADMIN_DIR / "templates"              # admin/templates/
STATIC_DIR = ADMIN_DIR / "static"                    # admin/static/


# ══════════════════════════════════════════════════════════════
# 2) استيراد من main.py الرئيسي (اختياري — فقط إن وُجد بجانب admin/)
# ══════════════════════════════════════════════════════════════
_IMPORT_MODE = "standalone"

STORE: dict = {
    "customers": {}, "numbers": [], "orders": [], "sms": [],
    "transactions": [], "deposits": [], "tickets": [], "sessions": [],
    "vouchers": [], "access_codes": [],
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


async def _sim5_get(path: str, auth: bool = False, timeout: float = 25.0):
    raise Sim5Error(503, "5SIM غير مفعّل (وضع مستقل)", path)


def _create_vouchers_batch(count, value, expires_days=30, note=""):
    prefix = os.getenv("VOUCHER_PREFIX", "JS").strip().upper()
    batch_id = f"batch_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
    now = datetime.now(timezone.utc)
    expires = now + timedelta(days=expires_days)
    created = []
    for _ in range(count):
        while True:
            code = f"{prefix}-{f'{value:g}'.replace('.', '')}-{secrets.token_hex(2).upper()}-{secrets.token_hex(2).upper()}"
            if not any(v["code"] == code for v in STORE["vouchers"]):
                break
        v = {
            "id": len(STORE["vouchers"]) + 1,
            "code": code,
            "value": float(value),
            "status": "unused",
            "batch_id": batch_id,
            "note": note,
            "created_at": now.isoformat(),
            "expires_at": expires.isoformat(),
            "used_by": None,
            "used_at": None,
        }
        STORE["vouchers"].append(v)
        created.append(v)
    return created


def _create_access_code(
    label="", expires_days=30, max_uses=1,
    preset_name="", preset_email="", preset_balance=0.0,
):
    prefix = os.getenv("ACCESS_CODE_PREFIX", "JS-LOGIN").strip().upper()
    while True:
        code = f"{prefix}-{secrets.token_hex(6).upper()}"
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


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# محاولة الاستيراد — دون تعديل sys.path
try:
    from main import (  # type: ignore
        STORE as _MAIN_STORE,
        SIM5_ENABLED as _MAIN_SIM5_ENABLED,
        FIVESIM_MARKUP_PERCENT as _MAIN_MARKUP,
        Sim5Error as _MainSim5Error,
        _sim5_get as _main_sim5_get,
        _create_vouchers_batch as _main_create_vouchers,
        _create_access_code as _main_create_access_code,
    )
    STORE = _MAIN_STORE
    SIM5_ENABLED = _MAIN_SIM5_ENABLED
    FIVESIM_MARKUP_PERCENT = _MAIN_MARKUP
    Sim5Error = _MainSim5Error
    _sim5_get = _main_sim5_get
    _create_vouchers_batch = _main_create_vouchers
    _create_access_code = _main_create_access_code
    _IMPORT_MODE = "main.py"
    print("[admin] ✅ تم استيراد STORE و_sim5_get من main.py")
except Exception as e:
    print(f"[admin] ⚠️  تعذّر استيراد main.py ({type(e).__name__}: {e})")
    print("[admin] ⚠️  سيعمل الأدمن بمخزن مستقل — owner_wallet لن يتزامن")


# ══════════════════════════════════════════════════════════════
# 3) App + Templates
# ══════════════════════════════════════════════════════════════
app = FastAPI(title="Jamal Solutions — Admin Panel", version="1.7.3")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ══════════════════════════════════════════════════════════════
# 4) إعدادات الأدمن
# ══════════════════════════════════════════════════════════════
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "admin@virtual-num.com").strip().lower()
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "Virtual123")
ADMIN_PASSWORD_HASH = os.getenv("ADMIN_PASSWORD_HASH", "").strip()
ADMIN_SECRET = os.getenv("ADMIN_SECRET", "").strip()

_bcrypt = None
try:
    import bcrypt as _bcrypt  # type: ignore
except Exception:
    _bcrypt = None

_ADMIN_TOKENS: dict[str, dict] = {}


def _issue_admin_token() -> str:
    tok = secrets.token_urlsafe(32)
    _ADMIN_TOKENS[tok] = {"issued_at": _now_iso()}
    return tok


def _check_admin(request: Request) -> None:
    """يقبل X-Admin-Secret = ADMIN_SECRET أو token صادر من /login."""
    if not ADMIN_SECRET and not ADMIN_EMAIL:
        raise HTTPException(503, "ADMIN_SECRET أو ADMIN_EMAIL غير مضبوط")
    provided = request.headers.get("X-Admin-Secret", "")
    if ADMIN_SECRET and secrets.compare_digest(provided, ADMIN_SECRET):
        return
    if provided and provided in _ADMIN_TOKENS:
        return
    raise HTTPException(403, "غير مصرح")


# ══════════════════════════════════════════════════════════════
# 5) Pydantic models
# ══════════════════════════════════════════════════════════════
class AdminLoginIn(BaseModel):
    email: str
    password: str


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
# 6) Pages  —  `/` و `/login` يشيران إلى نفس الملف login.html
# ══════════════════════════════════════════════════════════════
@app.get("/", response_class=HTMLResponse)
@app.get("/signin", response_class=HTMLResponse)
async def login_page(request: Request):
    """`/` و `/login` → نفس القالب login.html (بدون redirect)."""
    return templates.TemplateResponse(request=request, name="signin.html", context={})


@app.get("/admin-panel", response_class=HTMLResponse)
async def admin_panel(request: Request):
    """لوحة التحكم — JS يتحقق من token ويعيد التوجيه إن لزم."""
    return templates.TemplateResponse(request=request, name="admin.html", context={})


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "app": "admin",
        "version": app.version,
        "import_mode": _IMPORT_MODE,
        "root": str(ADMIN_DIR),
        "templates_dir": str(TEMPLATES_DIR),
        "login_template": (TEMPLATES_DIR / "login.html").exists(),
        "admin_template": (TEMPLATES_DIR / "admin.html").exists(),
        "static_dir_exists": STATIC_DIR.exists(),
        "admin_email": ADMIN_EMAIL,
        "password_hash_set": bool(ADMIN_PASSWORD_HASH),
        "password_plain_set": bool(ADMIN_PASSWORD),
        "admin_secret_set": bool(ADMIN_SECRET),
        "store_keys": sorted(STORE.keys()),
    }


# ══════════════════════════════════════════════════════════════
# 7) Auth endpoints
# ══════════════════════════════════════════════════════════════
@app.post("/api/v1/admin/login")
async def admin_login(payload: AdminLoginIn):
    """
    - إن كان ADMIN_PASSWORD_HASH مضبوطاً → bcrypt
    - وإلا → مقارنة مع ADMIN_PASSWORD
    يُرجع {ok, token}
    """
    email_in = payload.email.strip().lower()
    email_ok = secrets.compare_digest(email_in, ADMIN_EMAIL)

    pass_ok = False
    if email_ok:
        if ADMIN_PASSWORD_HASH and _bcrypt is not None:
            try:
                pass_ok = _bcrypt.checkpw(
                    payload.password.encode("utf-8"),
                    ADMIN_PASSWORD_HASH.encode("utf-8"),
                )
            except Exception:
                pass_ok = False
        else:
            pass_ok = secrets.compare_digest(payload.password, ADMIN_PASSWORD)

    if not (email_ok and pass_ok):
        raise HTTPException(403, "البريد أو كلمة المرور غير صحيحة")

    return {"ok": True, "token": _issue_admin_token()}


@app.get("/api/v1/admin/whoami")
async def admin_whoami(request: Request):
    """تتحقق اللوحة من صلاحية token عند الفتح."""
    _check_admin(request)
    provided = request.headers.get("X-Admin-Secret", "")
    auth_mode = "token" if provided in _ADMIN_TOKENS else "secret"
    return {"ok": True, "email": ADMIN_EMAIL, "auth": auth_mode}


@app.post("/api/v1/admin/logout")
async def admin_logout(request: Request):
    """يُبطل الـ token الحالي."""
    tok = request.headers.get("X-Admin-Secret", "")
    if tok and tok in _ADMIN_TOKENS:
        _ADMIN_TOKENS.pop(tok, None)
    return {"ok": True}


# ══════════════════════════════════════════════════════════════
# 8) /api/v1/admin/wallet
# ══════════════════════════════════════════════════════════════
@app.get("/api/v1/admin/wallet")
async def admin_owner_wallet(request: Request):
    _check_admin(request)
    ow = STORE["owner_wallet"]

    sim5_info: dict = {}
    if SIM5_ENABLED:
        try:
            p = await _sim5_get("/user/profile", auth=True)
            sim5_info = {"balance": p.get("balance"), "frozen": p.get("frozen_balance")}
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
# 9) /api/v1/admin/vouchers  (GET)
# ══════════════════════════════════════════════════════════════
@app.get("/api/v1/admin/vouchers")
async def admin_list_vouchers(
    request: Request,
    status_filter: Optional[str] = None,
    batch_id: Optional[str] = None,
):
    _check_admin(request)
    vouchers = STORE["vouchers"]

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
# 10) /api/v1/admin/vouchers/generate  (POST)
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
# 11) /api/v1/admin/vouchers/{id}/disable  (POST)
# ══════════════════════════════════════════════════════════════
@app.post("/api/v1/admin/vouchers/{voucher_id}/disable")
async def admin_disable_voucher(voucher_id: int, request: Request):
    _check_admin(request)
    v = next((v for v in STORE["vouchers"] if v["id"] == voucher_id), None)
    if not v:
        raise HTTPException(404, "الكود غير موجود")
    if v["status"] == "used":
        raise HTTPException(400, "لا يمكن تعطيل كود مستخدم")
    v["status"] = "disabled"
    return {"ok": True, "status": "disabled"}


# ══════════════════════════════════════════════════════════════
# 12) /api/v1/admin/deposits  (GET)
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
# 13) /api/v1/admin/deposits/{id}/approve  (POST)
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
# 14) /api/v1/admin/deposits/{id}/reject  (POST)
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
# 15) /api/v1/admin/access-codes/generate  (POST)
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
# 16) /api/v1/admin/access-codes  (GET)
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
# 17) /api/v1/admin/access-codes/{id}/disable  (POST)
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
# 18) /api/v1/admin/access-codes/{id}  (DELETE)
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
# 19) JSON error handler
# ══════════════════════════════════════════════════════════════
@app.exception_handler(HTTPException)
async def http_exc_handler(request: Request, exc: HTTPException):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


# ══════════════════════════════════════════════════════════════
# 20) Startup log
# ══════════════════════════════════════════════════════════════
@app.on_event("startup")
async def _startup_log():
    print("═" * 60)
    print(f"[admin] 🚀 لوحة الأدمن v{app.version}")
    print(f"[admin] import_mode   = {_IMPORT_MODE}")
    print(f"[admin] root          = {ADMIN_DIR}")
    print(f"[admin] templates     = {TEMPLATES_DIR}")
    print(f"[admin] signin.html    = {'✅' if (TEMPLATES_DIR / 'signin.html').exists() else '❌ مفقود'}")
    print(f"[admin] admin.html    = {'✅' if (TEMPLATES_DIR / 'admin.html').exists() else '❌ مفقود'}")
    print(f"[admin] static        = {STATIC_DIR} (exists={STATIC_DIR.exists()})")
    print(f"[admin] ADMIN_EMAIL   = {ADMIN_EMAIL}")
    print(f"[admin] hash set      = {bool(ADMIN_PASSWORD_HASH)}")
    print(f"[admin] secret set    = {bool(ADMIN_SECRET)}")
    print(f"[admin] SIM5          = {SIM5_ENABLED}")
    print(f"[admin] MARKUP        = {FIVESIM_MARKUP_PERCENT}%")
    print(f"[admin] owner_wallet  = {STORE['owner_wallet']}")
    print("═" * 60)
