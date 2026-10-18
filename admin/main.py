# admin/main.py
"""
Jamal Solutions — Admin Panel (standalone + shared storage)

- GET  /                       → admin.html
- GET  /api/v1/admin/catalog   → دول + خدمات + توفر (منطق app/main.py)
- كل /api/v1/admin/*           → 🔓 مفتوحة (بدون تحقق)

التخزين المشترك (shared_storage.py في جذر المشروع):
    project/data/store.json     (أو DATA_DIR=/var/data على Render Disks)
    project/data/backups/

التشغيل (من داخل admin/):
    uvicorn main:app --host 127.0.0.1 --port 8001 --workers 1

⚠️  workers=1 لكلا التطبيقين.
⚠️  بدون تسجيل دخول — لا تنشرها على إنترنت عام.
"""
from __future__ import annotations

import asyncio
import os
import secrets
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field


# ══════════════════════════════════════════════════════════════
# 1) Paths
# ══════════════════════════════════════════════════════════════
ADMIN_DIR = Path(__file__).resolve().parent          # admin/
PROJECT_ROOT = ADMIN_DIR.parent                      # project/
TEMPLATES_DIR = ADMIN_DIR / "templates"
STATIC_DIR = ADMIN_DIR / "static"


# ══════════════════════════════════════════════════════════════
# 2) التخزين الدائم المشترك — shared_storage.py في جذر المشروع
# ══════════════════════════════════════════════════════════════
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from shared_storage import (  # type: ignore
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
    schedule_save(STORE)


def _persist_now() -> None:
    save_now(STORE)


def _refresh() -> None:
    fresh = reload_from_disk()
    STORE.clear()
    STORE.update(fresh)


# ══════════════════════════════════════════════════════════════
# 3) 5SIM — إعدادات
# ══════════════════════════════════════════════════════════════
SIM5_BASE = os.getenv("SIM5_BASE", "https://5sim.net/v1").rstrip("/")
SIM5_API_KEY = os.getenv("SIM5_API_KEY", "").strip()
SIM5_ENABLED = bool(SIM5_API_KEY)

FIVESIM_MARKUP_PERCENT = float(os.getenv("FIVESIM_MARKUP_PERCENT", "40"))


class Sim5Error(Exception):
    def __init__(self, status_code: int, message: str, endpoint: str = ""):
        self.status = status_code
        self.message = message
        self.endpoint = endpoint
        super().__init__(f"[{status_code}] {message} ({endpoint})")


def _sim5_explain_400(text: str) -> str:
    t = text.lower()
    if "no free phones" in t:
        return "لا توجد أرقام متاحة حاليًا لهذه الدولة/الخدمة"
    if "not enough user balance" in t:
        return "رصيد 5SIM غير كافٍ"
    if "bad country" in t:
        return "الدولة غير مدعومة في 5SIM"
    if "bad operator" in t:
        return "المشغل غير مدعوم في 5SIM"
    if "no product" in t:
        return "الخدمة غير مدعومة في 5SIM"
    if "server offline" in t:
        return "خادم 5SIM غير متصل"
    return f"خطأ من 5SIM: {text[:200]}"


async def _sim5_get(path: str, auth: bool = False, timeout: float = 25.0) -> Any:
    if not SIM5_ENABLED and auth:
        raise Sim5Error(503, "5SIM غير مفعّل — SIM5_API_KEY غير مضبوط", path)

    url = f"{SIM5_BASE}{path}"
    headers = {"Accept": "application/json"}
    if auth and SIM5_API_KEY:
        headers["Authorization"] = f"Bearer {SIM5_API_KEY}"

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.get(url, headers=headers)
    except httpx.TimeoutException:
        raise Sim5Error(0, "انتهت مهلة الاتصال بـ 5SIM", path)
    except httpx.RequestError as e:
        raise Sim5Error(0, f"فشل الاتصال بـ 5SIM: {type(e).__name__}", path)

    if r.status_code == 200:
        try:
            return r.json()
        except Exception:
            raise Sim5Error(200, "استجابة 5SIM ليست JSON صالحًا", path)

    if r.status_code == 400:
        raise Sim5Error(400, _sim5_explain_400(r.text), path)
    if r.status_code == 401:
        raise Sim5Error(401, "مفتاح 5SIM غير صالح أو منتهي", path)
    if r.status_code == 403:
        raise Sim5Error(403, "لا تملك صلاحية لهذا الإجراء في 5SIM", path)
    if r.status_code == 404:
        raise Sim5Error(404, "العنصر غير موجود في 5SIM", path)
    if r.status_code == 429:
        raise Sim5Error(429, "تجاوزت حد الطلبات (100/ثانية) — حاول بعد قليل", path)
    if r.status_code == 503:
        raise Sim5Error(503, "5SIM مشغول حاليًا أو تجاوزت حد الطلبات", path)
    raise Sim5Error(r.status_code, f"استجابة غير متوقعة من 5SIM: {r.text[:200]}", path)


# ══════════════════════════════════════════════════════════════
# 3b) أدوات مساعدة
# ══════════════════════════════════════════════════════════════
def _iso_to_flag(iso: str) -> str:
    if not iso or len(iso) != 2:
        return "🏳️"
    return "".join(chr(0x1F1E6 + ord(c) - ord("A")) for c in iso.upper())


_SERVICE_ICONS = {
    "whatsapp": "💬", "telegram": "✈️", "google": "🔍", "facebook": "📘",
    "twitter": "🐦", "instagram": "📷", "tiktok": "🎵", "amazon": "📦",
    "snapchat": "👻", "discord": "🎮", "uber": "🚕", "netflix": "🎬",
    "apple": "🍎", "microsoft": "🪟", "yahoo": "🟣", "steam": "🎮",
    "binance": "🟡", "coinbase": "🔵", "paypal": "🅿️", "alipay": "🅰️",
    "wechat": "💚", "line": "💚", "viber": "📞", "signal": "🔒",
    "tinder": "❤️", "bumble": "🐝", "clubhouse": "🎙️", "twitch": "🎮",
    "spotify": "🎵", "yandex": "🔴", "vk": "🔵", "ok": "🟠",
    "airbnb": "🏠", "aliexpress": "🛒", "alibaba": "🏭", "ebay": "🛍️",
    "walmart": "🛒", "target": "🎯", "bestbuy": "🛒", "costco": "🛒",
    "openai": "🤖", "chatgpt": "🤖", "claudeai": "🤖", "gemini": "✨",
    "deepseek": "🐋", "perplexity": "🔎", "poeai": "💬",
}


def _service_icon(slug: str) -> str:
    return _SERVICE_ICONS.get(slug.lower(), "📱")


def _calculate_sale_price(fivesim_cost: float) -> float:
    if fivesim_cost <= 0:
        return 0.0
    markup = fivesim_cost * (FIVESIM_MARKUP_PERCENT / 100.0)
    return round(fivesim_cost + markup, 2)


# ══════════════════════════════════════════════════════════════
# 3c) كتالوج 5SIM
# ══════════════════════════════════════════════════════════════
CATALOG_TTL_SECONDS = float(os.getenv("CATALOG_TTL_SECONDS", "300"))
_SIM5_CACHE: dict[str, dict] = {
    "prices": {"data": None, "at": 0.0},
    "countries": {"data": None, "at": 0.0},
}
_SIM5_LOCK = asyncio.Lock()


async def _fetch_sim5_countries(force: bool = False) -> dict:
    now = time.time()
    cached = _SIM5_CACHE["countries"]
    if not force and cached["data"] and (now - cached["at"]) < CATALOG_TTL_SECONDS:
        return cached["data"]
    async with _SIM5_LOCK:
        now = time.time()
        if not force and cached["data"] and (now - cached["at"]) < CATALOG_TTL_SECONDS:
            return cached["data"]
        data = await _sim5_get("/guest/countries", auth=False)
        _SIM5_CACHE["countries"] = {"data": data, "at": time.time()}
        return data


async def _fetch_sim5_prices(force: bool = False) -> dict:
    now = time.time()
    cached = _SIM5_CACHE["prices"]
    if not force and cached["data"] and (now - cached["at"]) < CATALOG_TTL_SECONDS:
        return cached["data"]
    async with _SIM5_LOCK:
        now = time.time()
        if not force and cached["data"] and (now - cached["at"]) < CATALOG_TTL_SECONDS:
            return cached["data"]
        data = await _sim5_get("/guest/prices", auth=False)
        _SIM5_CACHE["prices"] = {"data": data, "at": time.time()}
        return data


async def build_live_catalog(force: bool = False) -> dict:
    if not SIM5_ENABLED:
        return {
            "ok": False,
            "countries": [],
            "error": "SIM5_API_KEY غير مضبوط",
            "error_code": None,
            "fetched_at": None,
        }
    try:
        raw_countries = await _fetch_sim5_countries(force=force)
        raw_prices = await _fetch_sim5_prices(force=force)
    except Sim5Error as e:
        return {
            "ok": False,
            "countries": [],
            "error": e.message,
            "error_code": e.status,
            "fetched_at": None,
        }

    countries_out: list[dict] = []

    for country_slug, cmeta in raw_countries.items():
        iso_raw = cmeta.get("iso") or {}
        iso = next(iter(iso_raw.keys()), "").upper() if isinstance(iso_raw, dict) else ""
        prefix_raw = cmeta.get("prefix") or {}
        prefix = next(iter(prefix_raw.keys()), "") if isinstance(prefix_raw, dict) else ""
        name_en = cmeta.get("text_en") or country_slug

        country_prices = raw_prices.get(country_slug) or {}
        services: list[dict] = []

        for product_slug, operators in country_prices.items():
            best = None
            for op_name, info in (operators or {}).items():
                try:
                    cost = float(info.get("cost", 0))
                    count = int(info.get("count", 0))
                    rate = float(info["rate"]) if info.get("rate") else None
                except (TypeError, ValueError):
                    continue
                if count <= 0 or cost <= 0:
                    continue
                if best is None or cost < best["fivesim_cost"]:
                    best = {
                        "fivesim_cost": cost,
                        "cost": _calculate_sale_price(cost),
                        "count": count,
                        "rate": rate,
                        "operator": op_name,
                    }

            services.append({
                "id": product_slug,
                "name": product_slug,
                "icon": _service_icon(product_slug),
                "cost": best["cost"] if best else None,
                "fivesim_cost": best["fivesim_cost"] if best else None,
                "count": best["count"] if best else 0,
                "rate": best["rate"] if best else None,
                "operator": best["operator"] if best else None,
                "available": best is not None,
            })

        services.sort(key=lambda s: (not s["available"], s["cost"] or 999))
        available_services = [s for s in services if s["available"]]

        countries_out.append({
            "code": iso or country_slug,
            "slug": country_slug,
            "name": name_en,
            "flag": _iso_to_flag(iso),
            "prefix": prefix,
            "services": services,
            "available": sum(s["count"] for s in available_services),
            "min_price": min((s["cost"] for s in available_services), default=None),
            "services_count": len(available_services),
        })

    countries_out.sort(key=lambda c: (-c["services_count"], c["name"]))

    return {
        "ok": True,
        "countries": countries_out,
        "error": None,
        "error_code": None,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


# ══════════════════════════════════════════════════════════════
# 4) مولّدات الأكواد
# ══════════════════════════════════════════════════════════════
def _create_vouchers_batch(
    count: int,
    value: float,
    expires_days: int = 30,
    note: str = "",
    country: Optional[str] = None,
    service: Optional[str] = None,
) -> list[dict]:
    prefix = os.getenv("VOUCHER_PREFIX", "JS").strip().upper()
    batch_id = f"batch_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
    now = datetime.now(timezone.utc)
    expires = now + timedelta(days=expires_days)
    created: list[dict] = []
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
    label: str = "",
    expires_days: int = 30,
    max_uses: int = 1,
    preset_name: str = "",
    preset_email: str = "",
    preset_balance: float = 0.0,
) -> dict:
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
# 5) App
# ══════════════════════════════════════════════════════════════
app = FastAPI(title="Jamal Solutions — Admin Panel", version="2.2.0-open")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "admin@virtual-num.com").strip().lower()


def _check_admin(request: Request) -> None:
    """🔓 بدون تسجيل دخول."""
    return


# ══════════════════════════════════════════════════════════════
# 6) Pydantic models
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
# 7) Pages
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
        "store_file": str(STORE_FILE),
        "store_file_exists": STORE_FILE.exists(),
        "sim5_enabled": SIM5_ENABLED,
        "sim5_base": SIM5_BASE,
        "catalog_cached": _SIM5_CACHE["prices"]["data"] is not None,
        "counts": {
            "vouchers": len(STORE["vouchers"]),
            "access_codes": len(STORE["access_codes"]),
            "deposits": len(STORE["deposits"]),
            "customers": len(STORE["customers"]),
            "orders": len(STORE["orders"]),
        },
    }


# ══════════════════════════════════════════════════════════════
# 8) Auth (توافقي)
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
# 8b) 5SIM Catalog
# ══════════════════════════════════════════════════════════════
@app.get("/api/v1/admin/catalog")
async def admin_catalog(request: Request, force: bool = False):
    _check_admin(request)
    result = await build_live_catalog(force=force)
    return {
        "ok": result["ok"],
        "source": "5sim",
        "markup_percent": FIVESIM_MARKUP_PERCENT,
        "fetched_at": result["fetched_at"],
        "error": result["error"],
        "error_code": result["error_code"],
        "countries": result["countries"],
    }


@app.get("/api/v1/admin/catalog/refresh")
async def admin_catalog_refresh(request: Request):
    _check_admin(request)
    result = await build_live_catalog(force=True)
    return {
        "ok": result["ok"],
        "source": "5sim",
        "markup_percent": FIVESIM_MARKUP_PERCENT,
        "fetched_at": result["fetched_at"],
        "error": result["error"],
        "error_code": result["error_code"],
        "countries": result["countries"],
    }


@app.get("/api/v1/admin/sim5-profile")
async def admin_sim5_profile(request: Request):
    _check_admin(request)
    if not SIM5_ENABLED:
        raise HTTPException(503, "5SIM غير مفعّل — SIM5_API_KEY غير مضبوط")
    try:
        p = await _sim5_get("/user/profile", auth=True)
    except Sim5Error as e:
        raise HTTPException(e.status or 503, e.message)
    return {
        "ok": True,
        "balance": p.get("balance"),
        "frozen_balance": p.get("frozen_balance"),
        "rating": p.get("rating"),
        "email": p.get("email"),
        "vendor": p.get("vendor"),
    }


# ══════════════════════════════════════════════════════════════
# 9) Wallet
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
        "owner_wallet": ow,
        "markup_percent": FIVESIM_MARKUP_PERCENT,
    }


# ══════════════════════════════════════════════════════════════
# 10) Vouchers — list
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
# 11) Vouchers — generate (دفعة واحدة)
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
# 12) Vouchers — generate multi
# ══════════════════════════════════════════════════════════════
@app.post("/api/v1/admin/vouchers/generate-multi")
async def admin_generate_vouchers_multi(payload: VoucherMultiCreate, request: Request):
    _check_admin(request)
    _refresh()
    all_created: list[dict] = []
    batches: list[dict] = []
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
# 13) Vouchers — disable
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
# 14) Deposits
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
# 15) Access codes
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
# 16) Backup
# ══════════════════════════════════════════════════════════════
@app.post("/api/v1/admin/backup")
async def admin_backup(request: Request):
    _check_admin(request)
    _refresh()
    p = backup(STORE)
    save_now(STORE)
    return {"ok": True, "path": str(p), "size": p.stat().st_size}


# ══════════════════════════════════════════════════════════════
# 17) Error handler
# ══════════════════════════════════════════════════════════════
@app.exception_handler(HTTPException)
async def http_exc_handler(request: Request, exc: HTTPException):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


# ══════════════════════════════════════════════════════════════
# 18) Startup / Shutdown
# ══════════════════════════════════════════════════════════════
@app.on_event("startup")
async def _startup_log():
    _refresh()
    print("═" * 60)
    print(f"[admin] 🚀 لوحة الأدمن v{app.version}  (🔓 بدون تسجيل دخول)")
    print(f"[admin] root          = {ADMIN_DIR}")
    print(f"[admin] project root  = {PROJECT_ROOT}")
    print(f"[admin] store file    = {STORE_FILE}")
    print(f"[admin] store exists  = {STORE_FILE.exists()}")
    print(f"[admin] templates     = {TEMPLATES_DIR}")
    print(f"[admin] admin.html    = {'✅' if (TEMPLATES_DIR / 'admin.html').exists() else '❌ مفقود'}")
    print(f"[admin] static        = {STATIC_DIR} (exists={STATIC_DIR.exists()})")
    print(f"[admin] SIM5          = {SIM5_ENABLED}")
    print(f"[admin] SIM5 base     = {SIM5_BASE}")
    print(f"[admin] MARKUP        = {FIVESIM_MARKUP_PERCENT}%")
    print(f"[admin] CATALOG TTL   = {CATALOG_TTL_SECONDS}s")
    print(f"[admin] owner_wallet  = {STORE['owner_wallet']}")
    print(f"[admin] vouchers      = {len(STORE['vouchers'])}")
    print(f"[admin] access_codes  = {len(STORE['access_codes'])}")
    print(f"[admin] customers     = {len(STORE['customers'])}")
    print(f"[admin] orders        = {len(STORE['orders'])}")
    if SIM5_ENABLED:
        try:
            profile = await _sim5_get("/user/profile", auth=True)
            print(f"[admin] 5SIM balance  = {profile.get('balance')}")
            print(f"[admin] 5SIM rating   = {profile.get('rating')}")
        except Sim5Error as e:
            print(f"[admin] ⚠️ 5SIM profile: [{e.status}] {e.message}")
    print("═" * 60)


@app.on_event("shutdown")
async def _shutdown_save():
    try:
        save_now(STORE)
        p = backup(STORE)
        print(f"[admin] 💾 تم حفظ store ونسخ احتياطي في {p}")
    except Exception as e:
        print(f"[admin] ⚠️  فشل الحفظ عند الإغلاق: {type(e).__name__}: {e}")
