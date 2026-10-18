# admin/main.py
"""
Jamal Solutions — Admin Panel (standalone + persistent storage مشترك)

- GET  /                       → admin.html  (بدون تسجيل دخول)
- GET  /admin-panel            → admin.html
- GET  /api/v1/admin/catalog   → الدول والخدمات + التوفر (من 5SIM)
- كل /api/v1/admin/*           → 🔓 مفتوحة (بدون تحقق)

التخزين (مشترك مع app/main.py):
    project/data/store.json     ← يُقرأ عند البدء + قبل كل عرض، يُحفظ بعد كل تعديل
    project/data/backups/       ← نسخة احتياطية عند الإغلاق الآمن

التشغيل (من داخل admin/):
    uvicorn main:app --host 127.0.0.1 --port 8001 --workers 1

⚠️  استخدم --workers 1 فقط لكلا التطبيقين (التخزين بالملف).
⚠️  هذه النسخة مفتوحة بالكامل — لا تنشرها على إنترنت عام.
"""
from __future__ import annotations

import asyncio
import json as _json
import os
import secrets
import time as _time
import urllib.request as _req
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field


# ══════════════════════════════════════════════════════════════
# 1) Paths
# ══════════════════════════════════════════════════════════════
ADMIN_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = ADMIN_DIR / "templates"
STATIC_DIR = ADMIN_DIR / "static"


# ══════════════════════════════════════════════════════════════
# 2) التخزين الدائم المشترك (JSON)
# ══════════════════════════════════════════════════════════════
from storage import (  # type: ignore
    load_store,
    schedule_save,
    save_now,
    backup,
    reload_from_disk,
    STORE_FILE,
)

STORE: dict = load_store()
print(f"[admin] 💾 store loaded from {STORE_FILE}")


def _persist() -> None:
    """حفظ مؤجل (debounced)."""
    schedule_save(STORE)


def _persist_now() -> None:
    """حفظ فوري — للعمليات الحسّاسة."""
    save_now(STORE)


def _refresh() -> None:
    """إعادة قراءة store.json من القرص."""
    fresh = reload_from_disk()
    STORE.clear()
    STORE.update(fresh)


# ══════════════════════════════════════════════════════════════
# 2b) إعدادات 5SIM
# ══════════════════════════════════════════════════════════════
SIM5_ENABLED = bool(os.getenv("SIM5_API_KEY", "").strip())
SIM5_BASE = os.getenv("SIM5_BASE", "https://5sim.net").rstrip("/")
FIVESIM_MARKUP_PERCENT = float(os.getenv("FIVESIM_MARKUP_PERCENT", "40"))


class Sim5Error(Exception):
    def __init__(self, status_code: int, message: str, endpoint: str = ""):
        self.status = status_code
        self.message = message
        self.endpoint = endpoint
        super().__init__(f"[{status_code}] {message} ({endpoint})")


def _sim5_http_get(path: str, timeout: float = 25.0):
    """GET متزامن إلى 5SIM مع API key."""
    api_key = os.getenv("SIM5_API_KEY", "").strip()
    if not api_key:
        raise Sim5Error(503, "SIM5_API_KEY غير مضبوط", path)
    req = _req.Request(
        f"{SIM5_BASE}{path}",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            "User-Agent": "jamal-solutions-admin/1.0",
        },
    )
    with _req.urlopen(req, timeout=timeout) as r:
        return _json.loads(r.read().decode("utf-8"))


async def _sim5_get(path: str, auth: bool = False, timeout: float = 25.0):
    """نسخة async — نغلّف المتزامن في thread."""
    if not SIM5_ENABLED:
        raise Sim5Error(503, "5SIM غير مفعّل", path)
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, lambda: _sim5_http_get(path, timeout))


# ══════════════════════════════════════════════════════════════
# 2c) كتالوج 5SIM (دول + خدمات + توفر) — مع كاش
# ══════════════════════════════════════════════════════════════
CATALOG_CACHE: dict = {"ts": 0.0, "data": None}
CATALOG_TTL_SECONDS = float(os.getenv("CATALOG_TTL_SECONDS", "300"))
CATALOG_MAX_PAIRS = int(os.getenv("CATALOG_MAX_PAIRS", "60"))


def _fetch_countries_sync() -> list:
    try:
        raw = _sim5_http_get("/guest/countries")
        if isinstance(raw, dict):
            return [
                {"code": k, "name": (v or {}).get("text") or (v or {}).get("name") or k}
                for k, v in raw.items()
                if isinstance(v, dict) and v.get("visible", True)
            ]
        if isinstance(raw, list):
            return [{"code": c, "name": c} for c in raw if isinstance(c, str)]
        return []
    except Exception as e:
        print(f"[admin] catalog countries error: {type(e).__name__}: {e}")
        return []


def _fetch_services_sync() -> list:
    try:
        raw = _sim5_http_get("/guest/products/any")
        if isinstance(raw, dict):
            return [
                {
                    "code": k,
                    "name": (v or {}).get("text") or (v or {}).get("name") or k,
                    "category": (v or {}).get("category", ""),
                }
                for k, v in raw.items()
            ]
        if isinstance(raw, list):
            return [{"code": s, "name": s, "category": ""} for s in raw if isinstance(s, str)]
        return []
    except Exception as e:
        print(f"[admin] catalog services error: {type(e).__name__}: {e}")
        return []


def _fetch_availability_sync(country: str, service: str) -> dict:
    try:
        raw = _sim5_http_get(f"/guest/products/{country}/{service}")
        if isinstance(raw, dict):
            total = 0
            prices = []
            for v in raw.values():
                if isinstance(v, list):
                    total += len(v)
                    for p in v:
                        if isinstance(p, dict) and p.get("price") is not None:
                            try:
                                prices.append(float(p["price"]))
                            except (TypeError, ValueError):
                                pass
            min_price = min(prices) if prices else None
            return {
                "available": total > 0,
                "count": total,
                "min_price": min_price,
                "in_use": None,
                "status": "ok" if total > 0 else "out_of_stock",
            }
        return {"available": False, "count": 0, "status": "no_data"}
    except Exception as e:
        return {
            "available": False,
            "count": 0,
            "status": f"error: {type(e).__name__}",
        }


async def _build_catalog() -> dict:
    if not SIM5_ENABLED:
        return {
            "countries": [],
            "services": [],
            "availability": {},
            "note": "SIM5_API_KEY غير مضبوط — لا يمكن جلب الدول/الخدمات",
        }

    loop = asyncio.get_event_loop()

    # جلب الدول والخدمات بالتوازي
    countries, services = await asyncio.gather(
        loop.run_in_executor(None, _fetch_countries_sync),
        loop.run_in_executor(None, _fetch_services_sync),
    )

    countries = sorted(countries, key=lambda x: x["name"])
    services = sorted(services, key=lambda x: x["name"])

    # بناء أزواج محدودة للتوفر
    pairs = []
    for c in countries:
        for s in services:
            pairs.append((c["code"], s["code"]))
            if len(pairs) >= CATALOG_MAX_PAIRS:
                break
        if len(pairs) >= CATALOG_MAX_PAIRS:
            break

    sem = asyncio.Semaphore(6)

    async def _one(c, s):
        async with sem:
            res = await loop.run_in_executor(None, lambda: _fetch_availability_sync(c, s))
            return f"{c}:{s}", res

    availability: dict = {}
    if pairs:
        results = await asyncio.gather(*[_one(c, s) for c, s in pairs])
        for key, res in results:
            availability[key] = res

    return {
        "countries": countries,
        "services": services,
        "availability": availability,
        "note": "",
    }


async def _get_catalog(force: bool = False) -> dict:
    now = _time.time()
    if (
        not force
        and CATALOG_CACHE["data"] is not None
        and (now - CATALOG_CACHE["ts"]) < CATALOG_TTL_SECONDS
    ):
        return CATALOG_CACHE["data"]
    data = await _build_catalog()
    CATALOG_CACHE["ts"] = now
    CATALOG_CACHE["data"] = data
    return data


# ══════════════════════════════════════════════════════════════
# 2d) مولّدات الأكواد
# ══════════════════════════════════════════════════════════════
def _create_vouchers_batch(
    count,
    value,
    expires_days=30,
    note="",
    country: Optional[str] = None,
    service: Optional[str] = None,
):
    prefix = os.getenv("VOUCHER_PREFIX", "JS").strip().upper()
    batch_id = f"batch_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
    now = datetime.now(timezone.utc)
    expires = now + timedelta(days=expires_days)
    created = []
    for _ in range(count):
        while True:
            code = (
                f"{prefix}-{f'{value:g}'.replace('.', '')}"
                f"-{secrets.token_hex(2).upper()}"
                f"-{secrets.token_hex(2).upper()}"
            )
            if not any(v["code"] == code for v in STORE["vouchers"]):
                break
        v = {
            "id": len(STORE["vouchers"]) + 1,
            "code": code,
            "value": float(value),
            "status": "unused",
            "batch_id": batch_id,
            "note": note,
            "country": country or None,
            "service": service or None,
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


# ══════════════════════════════════════════════════════════════
# 3) App + Templates
# ══════════════════════════════════════════════════════════════
app = FastAPI(title="Jamal Solutions — Admin Panel", version="2.0.0-open")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ══════════════════════════════════════════════════════════════
# 4) إعدادات الأدمن — 🔓 مفتوح
# ══════════════════════════════════════════════════════════════
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "admin@virtual-num.com").strip().lower()


def _check_admin(request: Request) -> None:
    """🔓 بدون تسجيل دخول — لا تفعل شيئاً."""
    return


# ══════════════════════════════════════════════════════════════
# 5) Pydantic models
# ══════════════════════════════════════════════════════════════
class AdminLoginIn(BaseModel):
    email: str = ""
    password: str = ""


class VoucherBatchCreate(BaseModel):
    count: int = Field(..., ge=1, le=1000)
    value: float = Field(..., ge=1)
    expires_days: int = Field(30, ge=1, le=365)
    note: str = ""
    country: str = ""
    service: str = ""


class VoucherPair(BaseModel):
    country: str
    service: str
    count: int = Field(..., ge=1, le=1000)


class VoucherMultiCreate(BaseModel):
    value: float = Field(..., ge=1)
    expires_days: int = Field(30, ge=1, le=365)
    note: str = ""
    pairs: list[VoucherPair] = Field(..., min_length=1)


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
@app.get("/", response_class=HTMLResponse)
@app.get("/login", response_class=HTMLResponse)
@app.get("/admin-panel", response_class=HTMLResponse)
async def admin_panel(request: Request):
    return templates.TemplateResponse(request=request, name="admin.html", context={})


@app.get("/health")
async def health():
    _refresh()
    return {
        "status": "ok",
        "app": "admin",
        "version": app.version,
        "auth": "open",
        "root": str(ADMIN_DIR),
        "store_file": str(STORE_FILE),
        "store_file_exists": STORE_FILE.exists(),
        "templates_dir": str(TEMPLATES_DIR),
        "admin_template": (TEMPLATES_DIR / "admin.html").exists(),
        "static_dir_exists": STATIC_DIR.exists(),
        "sim5_enabled": SIM5_ENABLED,
        "sim5_base": SIM5_BASE,
        "catalog_cached": CATALOG_CACHE["data"] is not None,
        "catalog_cache_age_sec": (
            round(_time.time() - CATALOG_CACHE["ts"], 1)
            if CATALOG_CACHE["data"] is not None else None
        ),
        "store_keys": sorted(STORE.keys()),
        "counts": {
            "vouchers": len(STORE["vouchers"]),
            "access_codes": len(STORE["access_codes"]),
            "deposits": len(STORE["deposits"]),
            "customers": len(STORE["customers"]),
            "orders": len(STORE["orders"]),
        },
    }


# ══════════════════════════════════════════════════════════════
# 7) Auth endpoints — 🔓 توافقية فقط
# ══════════════════════════════════════════════════════════════
@app.post("/api/v1/admin/login")
async def admin_login(payload: AdminLoginIn = None):
    return {"ok": True, "token": "open"}


@app.get("/api/v1/admin/whoami")
async def admin_whoami(request: Request):
    return {"ok": True, "email": ADMIN_EMAIL, "auth": "open"}


@app.post("/api/v1/admin/logout")
async def admin_logout(request: Request):
    return {"ok": True}


# ══════════════════════════════════════════════════════════════
# 7b) 5SIM Catalog
# ══════════════════════════════════════════════════════════════
@app.get("/api/v1/admin/catalog")
async def admin_catalog(request: Request):
    _check_admin(request)
    data = await _get_catalog(force=False)
    return {"ok": True, **data}


@app.get("/api/v1/admin/catalog/refresh")
async def admin_catalog_refresh(request: Request):
    _check_admin(request)
    data = await _get_catalog(force=True)
    return {"ok": True, **data}


# ══════════════════════════════════════════════════════════════
# 8) Wallet — قيمة كلية واحدة
# ══════════════════════════════════════════════════════════════
@app.get("/api/v1/admin/wallet")
async def admin_owner_wallet(request: Request):
    _check_admin(request)
    _refresh()

    ow = STORE["owner_wallet"]
    total = float(ow.get("total_revenue", 0)) + float(ow.get("total_profit", 0))

    return {
        "ok": True,
        "total_balance": round(total, 2),
        # حقول توافقية (لا تُعرض في الواجهة)
        "owner_wallet": ow,
        "markup_percent": FIVESIM_MARKUP_PERCENT,
    }


# ══════════════════════════════════════════════════════════════
# 9) Vouchers — list
# ══════════════════════════════════════════════════════════════
@app.get("/api/v1/admin/vouchers")
async def admin_list_vouchers(
    request: Request,
    status_filter: Optional[str] = None,
    batch_id: Optional[str] = None,
):
    _check_admin(request)
    _refresh()

    vouchers = STORE["vouchers"]

    now = datetime.now(timezone.utc)
    changed = False
    for v in vouchers:
        if v.get("status") == "unused":
            try:
                exp = datetime.fromisoformat(v["expires_at"])
                if exp.tzinfo is None:
                    exp = exp.replace(tzinfo=timezone.utc)
                if now > exp:
                    v["status"] = "expired"
                    changed = True
            except (ValueError, KeyError, TypeError):
                pass
    if changed:
        _persist()

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
# 10) Vouchers — generate (دفعة واحدة لزوج واحد)
# ══════════════════════════════════════════════════════════════
@app.post("/api/v1/admin/vouchers/generate")
async def admin_generate_vouchers(payload: VoucherBatchCreate, request: Request):
    _check_admin(request)
    _refresh()

    created = _create_vouchers_batch(
        count=payload.count,
        value=payload.value,
        expires_days=payload.expires_days,
        note=payload.note,
        country=payload.country or None,
        service=payload.service or None,
    )
    _persist_now()
    return {
        "ok": True,
        "count": len(created),
        "batch_id": created[0]["batch_id"] if created else None,
        "total_value": round(sum(v["value"] for v in created), 2),
        "codes": [v["code"] for v in created],
        "country": payload.country or None,
        "service": payload.service or None,
    }


# ══════════════════════════════════════════════════════════════
# 10b) Vouchers — generate multi (عدة أزواج دفعة واحدة)
# ══════════════════════════════════════════════════════════════
@app.post("/api/v1/admin/vouchers/generate-multi")
async def admin_generate_vouchers_multi(payload: VoucherMultiCreate, request: Request):
    _check_admin(request)
    _refresh()

    all_created = []
    batches = []
    for p in payload.pairs:
        created = _create_vouchers_batch(
            count=p.count,
            value=payload.value,
            expires_days=payload.expires_days,
            note=payload.note,
            country=p.country,
            service=p.service,
        )
        all_created.extend(created)
        batches.append({
            "country": p.country,
            "service": p.service,
            "count": len(created),
            "batch_id": created[0]["batch_id"] if created else None,
            "codes": [v["code"] for v in created],
            "total_value": round(sum(v["value"] for v in created), 2),
        })

    _persist_now()
    return {
        "ok": True,
        "total_count": len(all_created),
        "total_value": round(sum(v["value"] for v in all_created), 2),
        "batches": batches,
    }


# ══════════════════════════════════════════════════════════════
# 11) Vouchers — disable
# ══════════════════════════════════════════════════════════════
@app.post("/api/v1/admin/vouchers/{voucher_id}/disable")
async def admin_disable_voucher(voucher_id: int, request: Request):
    _check_admin(request)
    _refresh()
    v = next((v for v in STORE["vouchers"] if v["id"] == voucher_id), None)
    if not v:
        raise HTTPException(404, "الكود غير موجود")
    if v["status"] == "used":
        raise HTTPException(400, "لا يمكن تعطيل كود مستخدم")
    v["status"] = "disabled"
    _persist_now()
    return {"ok": True, "status": "disabled"}


# ══════════════════════════════════════════════════════════════
# 12) Deposits — list
# ══════════════════════════════════════════════════════════════
@app.get("/api/v1/admin/deposits")
async def admin_list_deposits(
    request: Request,
    status_filter: Optional[str] = None,
):
    _check_admin(request)
    _refresh()

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
# 13) Deposits — approve
# ══════════════════════════════════════════════════════════════
@app.post("/api/v1/admin/deposits/{deposit_id}/approve")
async def admin_approve_deposit(deposit_id: int, request: Request):
    _check_admin(request)
    _refresh()

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
    _persist_now()
    return {"ok": True, "new_balance": customer["balance"]}


# ══════════════════════════════════════════════════════════════
# 14) Deposits — reject
# ══════════════════════════════════════════════════════════════
@app.post("/api/v1/admin/deposits/{deposit_id}/reject")
async def admin_reject_deposit(deposit_id: int, request: Request, reason: str = ""):
    _check_admin(request)
    _refresh()
    deposit = next((d for d in STORE["deposits"] if d["id"] == deposit_id), None)
    if not deposit:
        raise HTTPException(404, "الطلب غير موجود")
    deposit["status"] = "rejected"
    deposit["reviewed_at"] = _now_iso()
    deposit["reject_reason"] = reason
    _persist_now()
    return {"ok": True, "status": "rejected"}


# ══════════════════════════════════════════════════════════════
# 15) Access codes — generate
# ══════════════════════════════════════════════════════════════
@app.post("/api/v1/admin/access-codes/generate")
async def admin_generate_access_code(payload: AccessCodeCreate, request: Request):
    _check_admin(request)
    _refresh()
    record = _create_access_code(
        label=payload.label,
        expires_days=payload.expires_days,
        max_uses=payload.max_uses,
        preset_name=payload.preset_name,
        preset_email=payload.preset_email,
        preset_balance=payload.preset_balance,
    )
    _persist_now()
    return {
        "ok": True,
        "id": record["id"],
        "code": record["code"],
        "expires_at": record["expires_at"],
        "max_uses": record["max_uses"],
        "preset_balance": record["preset_balance"],
    }


# ══════════════════════════════════════════════════════════════
# 16) Access codes — list
# ══════════════════════════════════════════════════════════════
@app.get("/api/v1/admin/access-codes")
async def admin_list_access_codes(
    request: Request,
    status_filter: Optional[str] = None,
):
    _check_admin(request)
    _refresh()

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
# 17) Access codes — disable
# ══════════════════════════════════════════════════════════════
@app.post("/api/v1/admin/access-codes/{code_id}/disable")
async def admin_disable_access_code(code_id: int, request: Request):
    _check_admin(request)
    _refresh()
    record = next((c for c in STORE["access_codes"] if c["id"] == code_id), None)
    if not record:
        raise HTTPException(404, "الكود غير موجود")
    record["status"] = "disabled"
    _persist_now()
    return {"ok": True, "status": "disabled"}


# ══════════════════════════════════════════════════════════════
# 18) Access codes — delete
# ══════════════════════════════════════════════════════════════
@app.delete("/api/v1/admin/access-codes/{code_id}")
async def admin_delete_access_code(code_id: int, request: Request):
    _check_admin(request)
    _refresh()
    before = len(STORE["access_codes"])
    STORE["access_codes"] = [c for c in STORE["access_codes"] if c["id"] != code_id]
    if len(STORE["access_codes"]) == before:
        raise HTTPException(404, "الكود غير موجود")
    _persist_now()
    return {"ok": True, "deleted": True}


# ══════════════════════════════════════════════════════════════
# 19) Backup يدوي
# ══════════════════════════════════════════════════════════════
@app.post("/api/v1/admin/backup")
async def admin_backup(request: Request):
    _check_admin(request)
    _refresh()
    p = backup(STORE)
    save_now(STORE)
    return {"ok": True, "path": str(p), "size": p.stat().st_size}


# ══════════════════════════════════════════════════════════════
# 20) JSON error handler
# ══════════════════════════════════════════════════════════════
@app.exception_handler(HTTPException)
async def http_exc_handler(request: Request, exc: HTTPException):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


# ══════════════════════════════════════════════════════════════
# 21) Startup / Shutdown
# ══════════════════════════════════════════════════════════════
@app.on_event("startup")
async def _startup_log():
    _refresh()
    print("═" * 60)
    print(f"[admin] 🚀 لوحة الأدمن v{app.version}  (🔓 بدون تسجيل دخول)")
    print(f"[admin] root          = {ADMIN_DIR}")
    print(f"[admin] store file    = {STORE_FILE}")
    print(f"[admin] store exists  = {STORE_FILE.exists()}")
    print(f"[admin] templates     = {TEMPLATES_DIR}")
    print(f"[admin] admin.html    = {'✅' if (TEMPLATES_DIR / 'admin.html').exists() else '❌ مفقود'}")
    print(f"[admin] static        = {STATIC_DIR} (exists={STATIC_DIR.exists()})")
    print(f"[admin] SIM5          = {SIM5_ENABLED}")
    print(f"[admin] SIM5 base     = {SIM5_BASE}")
    print(f"[admin] MARKUP        = {FIVESIM_MARKUP_PERCENT}%")
    print(f"[admin] CATALOG TTL   = {CATALOG_TTL_SECONDS}s, MAX_PAIRS = {CATALOG_MAX_PAIRS}")
    print(f"[admin] owner_wallet  = {STORE['owner_wallet']}")
    print(f"[admin] vouchers      = {len(STORE['vouchers'])}")
    print(f"[admin] access_codes  = {len(STORE['access_codes'])}")
    print(f"[admin] customers     = {len(STORE['customers'])}")
    print(f"[admin] orders        = {len(STORE['orders'])}")
    print("═" * 60)


@app.on_event("shutdown")
async def _shutdown_save():
    try:
        save_now(STORE)
        p = backup(STORE)
        print(f"[admin] 💾 تم حفظ store ونسخ احتياطي في {p}")
    except Exception as e:
        print(f"[admin] ⚠️  فشل الحفظ عند الإغلاق: {type(e).__name__}: {e}")
