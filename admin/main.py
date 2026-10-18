# admin/main.py
"""
Jamal Solutions — Admin Panel (standalone + shared storage)

- GET  /                       → admin.html
- GET  /api/v1/admin/catalog   → دول + خدمات + توفر (منطق app/main.py)
- كل /api/v1/admin/*           → 🔓 مفتوحة (بدون تحقق)

التخزين المشترك (shared_storage.py في جذر المشروع):
    project/data/store.json     (أو DATA_DIR=/var/data على Render Disks)
    project/data/backups/
    أو PostgreSQL عبر DATABASE_URL

التشغيل (من داخل admin/):
    uvicorn main:app --host 127.0.0.1 --port 8001 --workers 1

⚠️  workers=1 لكلا التطبيقين.
⚠️  بدون تسجيل دخول — لا تنشرها على إنترنت عام.

✨ v2.3.0 — دعم الكود الموحّد (Multi-Pair Voucher):
    - كود واحد يصلح لأكثر من زوج (دولة/خدمة)
    - allowed_pairs في كل voucher
    - endpoint /vouchers/check للتحقق المسبق
    - وضع merge في generate-multi
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

try:
    from shared_storage import store_status as _store_status  # type: ignore
    _HAS_STORE_STATUS = True
except ImportError:
    _store_status = None
    _HAS_STORE_STATUS = False


STORE: dict = load_store()
print(f"[admin] 💾 store loaded from {STORE_FILE}")


def _store_file_info() -> dict:
    if _HAS_STORE_STATUS:
        try:
            st = _store_status()
            return {
                "path": st.get("target", str(STORE_FILE)),
                "exists": st.get("exists", True),
                "kind": st.get("backend", "unknown"),
                "version": st.get("version"),
                "size_bytes": st.get("size_bytes"),
            }
        except Exception as e:
            print(f"[admin] ⚠️  store_status فشل: {type(e).__name__}: {e}")

    if isinstance(STORE_FILE, Path):
        return {
            "path": str(STORE_FILE),
            "exists": STORE_FILE.exists(),
            "kind": "file",
        }
    s = str(STORE_FILE)
    is_dsn = ("://" in s) or ("@/" in s)
    return {
        "path": s,
        "exists": True,
        "kind": "dsn" if is_dsn else "string",
    }


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
    allowed_pairs: Optional[list[dict]] = None,   # ✨ NEW
) -> list[dict]:
    """
    ينشئ دفعة أكواد.

    - allowed_pairs (✨ NEW): قائمة [{"country":..., "service":...}, ...]
      كود واحد يصلح لأي من هذه الأزواج. متوافق مع الأكواد القديمة.
    - country/service (قديم): زوج واحد فقط. يُستخدم إن كانت allowed_pairs فارغة.
    """
    prefix = os.getenv("VOUCHER_PREFIX", "JS").strip().upper()
    batch_id = f"batch_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
    now = datetime.now(timezone.utc)
    expires = now + timedelta(days=expires_days)

    # ✨ تطبيع allowed_pairs
    pairs: list[dict] = []
    if allowed_pairs:
        for p in allowed_pairs:
            c = (p.get("country") or "").strip()
            s = (p.get("service") or "").strip()
            if c and s:
                pairs.append({"country": c, "service": s})
    # 🔧 توافق عكسي: لو مفيش pairs وعندنا country+service، اعتبرهم زوج واحد
    if not pairs and country and service:
        pairs = [{"country": country, "service": service}]

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

        # ✨ للتوافق: خزّن country/service المفردين لو كان الزوج واحداً
        single = pairs[0] if len(pairs) == 1 else None

        v = {
            "id": len(STORE["vouchers"]) + 1,
            "code": code,
            "value": float(value),
            "status": "unused",
            "batch_id": batch_id,
            "note": note,
            # 🔧 حقول قديمة — تبقى للتوافق العكسي
            "country": single["country"] if single else (country or None),
            "service": single["service"] if single else (service or None),
            # ✨ جديد: قائمة الأزواج المسموحة
            "allowed_pairs": pairs,
            "created_at": now.isoformat(),
            "expires_at": expires.isoformat(),
            "used_by": None,
            "used_at": None,
            # ✨ جديد: أي زوج تم استخدام الكود عليه فعلاً
            "used_on": None,
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


def _normalize_pairs(v: dict) -> list[dict]:
    """
    ✨ NEW — يعيد قائمة الأزواج المسموحة لكود، مع التوافق العكسي:
    - إن وُجد allowed_pairs نستخدمها.
    - وإلا، نستخدم country/service المفردين (كود قديم).
    """
    pairs = v.get("allowed_pairs") or []
    if pairs:
        return pairs
    if v.get("country") and v.get("service"):
        return [{"country": v["country"], "service": v["service"]}]
    return []


# ══════════════════════════════════════════════════════════════
# 5) App
# ══════════════════════════════════════════════════════════════
app = FastAPI(title="Jamal Solutions — Admin Panel", version="2.3.0-open")
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


class AllowedPair(BaseModel):
    """✨ NEW — زوج مسموح به داخل كود موحّد."""
    country: str = Field(..., min_length=1)
    service: str = Field(..., min_length=1)


class VoucherBatchCreate(BaseModel):
    count: int = Field(..., ge=1, le=1000)
    value: float = Field(..., ge=1)
    expires_days: int = Field(30, ge=1, le=365)
    note: str = ""
    # 🔧 للتوافق القديم — تُستخدم إن كانت allowed_pairs فارغة
    country: str = ""
    service: str = ""
    # ✨ NEW — قائمة أزواج متعددة لكود واحد
    allowed_pairs: list[AllowedPair] = Field(default_factory=list)


class VoucherPair(BaseModel):
    country: str
    service: str
    count: int = Field(..., ge=1, le=1000)


class VoucherMultiCreate(BaseModel):
    """
    ✨ يدعم وضعين:
    - merge=False (افتراضي): كود منفصل لكل زوج في pairs.
    - merge=True: كود واحد لكل count، يصلح لأي من allowed_pairs.
    """
    value: float = Field(..., ge=1)
    expires_days: int = Field(30, ge=1, le=365)
    note: str = ""
    # الوضع القديم
    pairs: list[VoucherPair] = Field(default_factory=list)
    # ✨ وضع الدمج
    merge: bool = False
    count: int = Field(1, ge=1, le=1000)
    allowed_pairs: list[AllowedPair] = Field(default_factory=list)


class VoucherCheckIn(BaseModel):
    """✨ NEW — للتحقق من صلاحية كود لزوج معيّن."""
    code: str = Field(..., min_length=1)
    country: str = Field(..., min_length=1)
    service: str = Field(..., min_length=1)


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
    _info = _store_file_info()
    # ✨ إحصاء الأكواد الموحّدة
    merged_count = sum(
        1 for v in STORE["vouchers"]
        if len(_normalize_pairs(v)) > 1
    )
    out = {
        "status": "ok",
        "app": "admin",
        "version": app.version,
        "auth": "open",
        "store_file": _info["path"],
        "store_file_exists": _info["exists"],
        "store_kind": _info["kind"],
        "sim5_enabled": SIM5_ENABLED,
        "sim5_base": SIM5_BASE,
        "catalog_cached": _SIM5_CACHE["prices"]["data"] is not None,
        "counts": {
            "vouchers": len(STORE["vouchers"]),
            "merged_vouchers": merged_count,       # ✨ NEW
            "access_codes": len(STORE["access_codes"]),
            "deposits": len(STORE["deposits"]),
            "customers": len(STORE["customers"]),
            "orders": len(STORE["orders"]),
        },
        "features": {
            "multi_pair_vouchers": True,           # ✨ NEW
            "voucher_check_endpoint": True,        # ✨ NEW
        },
    }
    if _info.get("version") is not None:
        out["store_version"] = _info["version"]
    if _info.get("size_bytes") is not None:
        out["store_size_bytes"] = _info["size_bytes"]
    return out


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
        # ✨ NEW
        "merged": sum(1 for v in vouchers if len(_normalize_pairs(v)) > 1),
    }

    # ✨ NEW — أضف معلومات موحّدة لكل voucher للعرض
    out = []
    for v in filtered:
        pairs = _normalize_pairs(v)
        out.append({
            **v,
            # املأ حقول العرض للتوافق مع الواجهة الحالية
            "country": v.get("country") or (pairs[0]["country"] if len(pairs) == 1 else None),
            "service": v.get("service") or (pairs[0]["service"] if len(pairs) == 1 else None),
            "allowed_pairs": pairs,
            "is_merged": len(pairs) > 1,
        })

    return {"summary": summary, "vouchers": out}


# ══════════════════════════════════════════════════════════════
# 11) Vouchers — generate (دفعة واحدة، مع دعم allowed_pairs)
# ══════════════════════════════════════════════════════════════
@app.post("/api/v1/admin/vouchers/generate")
async def admin_generate_vouchers(payload: VoucherBatchCreate, request: Request):
    _check_admin(request)
    _refresh()

    # ✨ دعم allowed_pairs أو country/service المفردين
    created = _create_vouchers_batch(
        count=payload.count,
        value=payload.value,
        expires_days=payload.expires_days,
        note=payload.note,
        country=payload.country or None,
        service=payload.service or None,
        allowed_pairs=[p.model_dump() for p in payload.allowed_pairs] or None,
    )
    _persist_now()
    pairs_out = _normalize_pairs(created[0]) if created else []
    return {
        "ok": True,
        "count": len(created),
        "batch_id": created[0]["batch_id"] if created else None,
        "total_value": round(sum(v["value"] for v in created), 2),
        "codes": [v["code"] for v in created],
        "country": payload.country or None,
        "service": payload.service or None,
        "allowed_pairs": pairs_out,          # ✨ NEW
        "is_merged": len(pairs_out) > 1,     # ✨ NEW
    }


# ══════════════════════════════════════════════════════════════
# 12) Vouchers — generate multi (يدعم merge)
# ══════════════════════════════════════════════════════════════
@app.post("/api/v1/admin/vouchers/generate-multi")
async def admin_generate_vouchers_multi(payload: VoucherMultiCreate, request: Request):
    """
    ✨ يدعم وضعين:
    - merge=False (افتراضي): كود منفصل لكل زوج في pairs (السلوك القديم).
    - merge=True: كود واحد (× count) يصلح لأي من allowed_pairs.
    """
    _check_admin(request)
    _refresh()

    # ── وضع الدمج ✨ NEW ──
    if payload.merge:
        if not payload.allowed_pairs:
            raise HTTPException(400, "allowed_pairs مطلوبة في وضع الدمج")
        if not payload.count or payload.count < 1:
            raise HTTPException(400, "count مطلوبة في وضع الدمج (≥1)")

        created = _create_vouchers_batch(
            count=payload.count,
            value=payload.value,
            expires_days=payload.expires_days,
            note=payload.note,
            allowed_pairs=[p.model_dump() for p in payload.allowed_pairs],
        )
        _persist_now()

        pairs_desc = ", ".join(f"{p.country}/{p.service}" for p in payload.allowed_pairs)
        return {
            "ok": True,
            "merge": True,
            "total_count": len(created),
            "total_value": round(sum(v["value"] for v in created), 2),
            "batches": [{
                "country": "merged",
                "service": pairs_desc,
                "allowed_pairs": [p.model_dump() for p in payload.allowed_pairs],
                "count": len(created),
                "batch_id": created[0]["batch_id"] if created else None,
                "codes": [v["code"] for v in created],
                "total_value": round(sum(v["value"] for v in created), 2),
            }],
        }

    # ── الوضع القديم (كود منفصل لكل زوج) ──
    if not payload.pairs:
        raise HTTPException(400, "pairs مطلوبة (أو استخدم merge=true مع allowed_pairs)")

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
        "merge": False,
        "total_count": len(all_created),
        "total_value": round(sum(v["value"] for v in all_created), 2),
        "batches": batches,
    }


# ══════════════════════════════════════════════════════════════
# 12b) Vouchers — check (✨ NEW: تحقق مسبق من صلاحية الكود لزوج)
# ══════════════════════════════════════════════════════════════
@app.post("/api/v1/admin/vouchers/check")
async def admin_check_voucher(payload: VoucherCheckIn, request: Request):
    """
    يتحقق أن الكود:
      - موجود
      - غير مستخدم/معطّل
      - غير منتهي
      - يصلح للزوج (country, service) المطلوب

    لا يستهلك الكود — آمن للاستدعاء من واجهة الزبون.
    """
    _check_admin(request)
    _refresh()

    v = next((x for x in STORE["vouchers"] if x["code"] == payload.code), None)
    if not v:
        raise HTTPException(404, "الكود غير موجود")

    if v["status"] != "unused":
        status_ar = {
            "used": "مستخدم",
            "expired": "منتهي",
            "disabled": "معطّل",
        }.get(v["status"], v["status"])
        raise HTTPException(400, f"هذا الكود {status_ar} — لا يمكن استخدامه")

    try:
        exp = datetime.fromisoformat(v["expires_at"])
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) > exp:
            v["status"] = "expired"
            _persist()
            raise HTTPException(400, "هذا الكود منتهي الصلاحية")
    except (ValueError, KeyError, TypeError):
        pass

    pairs = _normalize_pairs(v)
    if pairs:
        ok = any(
            p["country"] == payload.country and p["service"] == payload.service
            for p in pairs
        )
        if not ok:
            pairs_str = ", ".join(f"{p['country']}/{p['service']}" for p in pairs)
            raise HTTPException(
                403,
                f"هذا الكود لا يصلح لـ {payload.country}/{payload.service}. "
                f"يصلح لـ: {pairs_str}",
            )

    return {
        "ok": True,
        "code": v["code"],
        "value": v["value"],
        "expires_at": v["expires_at"],
        "allowed_pairs": pairs,
        "is_merged": len(pairs) > 1,
        "note": v.get("note", ""),
        "selected_pair": {
            "country": payload.country,
            "service": payload.service,
        },
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
    result = backup(STORE)
    save_now(STORE)

    if isinstance(result, Path):
        try:
            size = result.stat().st_size
        except OSError:
            size = None
        return {
            "ok": True,
            "path": str(result),
            "size": size,
            "kind": "file",
        }
    if isinstance(result, dict):
        return {"ok": True, **result}
    if result is None:
        return {
            "ok": True,
            "path": None,
            "kind": "none",
            "note": "backup غير مدعوم في وضع PostgreSQL — استخدم pg_dump",
        }
    return {"ok": True, "path": str(result), "kind": "string"}


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
    _info = _store_file_info()
    merged_count = sum(1 for v in STORE["vouchers"] if len(_normalize_pairs(v)) > 1)
    print("═" * 60)
    print(f"[admin] 🚀 لوحة الأدمن v{app.version}  (🔓 بدون تسجيل دخول)")
    print(f"[admin] root          = {ADMIN_DIR}")
    print(f"[admin] project root  = {PROJECT_ROOT}")
    print(f"[admin] store file    = {_info['path']}")
    print(f"[admin] store kind    = {_info['kind']}")
    print(f"[admin] store ready   = {'✅' if _info['exists'] else '❌'}")
    if _info.get("version") is not None:
        print(f"[admin] store version = {_info['version']}")
    if _info.get("size_bytes") is not None:
        print(f"[admin] store size    = {_info['size_bytes']} bytes")
    print(f"[admin] store_status  = {'✅' if _HAS_STORE_STATUS else '⚠️  fallback'}")
    print(f"[admin] templates     = {TEMPLATES_DIR}")
    print(f"[admin] admin.html    = {'✅' if (TEMPLATES_DIR / 'admin.html').exists() else '❌ مفقود'}")
    print(f"[admin] static        = {STATIC_DIR} (exists={STATIC_DIR.exists()})")
    print(f"[admin] SIM5          = {SIM5_ENABLED}")
    print(f"[admin] SIM5 base     = {SIM5_BASE}")
    print(f"[admin] MARKUP        = {FIVESIM_MARKUP_PERCENT}%")
    print(f"[admin] CATALOG TTL   = {CATALOG_TTL_SECONDS}s")
    print(f"[admin] owner_wallet  = {STORE['owner_wallet']}")
    print(f"[admin] vouchers      = {len(STORE['vouchers'])}  (merged: {merged_count})")
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
        result = backup(STORE)
        if isinstance(result, Path):
            print(f"[admin] 💾 تم حفظ store ونسخ احتياطي في {result}")
        elif result is None:
            print("[admin] 💾 تم حفظ store (بدون نسخ احتياطي في وضع PG)")
        else:
            print(f"[admin] 💾 تم حفظ store ونسخ احتياطي: {result}")
    except Exception as e:
        print(f"[admin] ⚠️  فشل الحفظ عند الإغلاق: {type(e).__name__}: {e}")
