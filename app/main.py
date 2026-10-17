"""
Jamal Solutions — Virtual Numbers Platform
Customer Portal (FastAPI + Jinja2 + SSE + 5SIM + Access-Code Login)

✅ 1.4.0 — شاشة واحدة موحّدة (/buy) + تسجيل دخول بالكود:
- كل شيء في /buy?tab=... (شراء، أرقامي، طلبات، وارد، محفظة، معاملات، دعم، حساب)
- إعادة توجيه كل المسارات القديمة إلى /buy تلقائياً.
- Access Codes: توليد، تفعيل، إدارة كاملة عبر endpoints إدارية.
- إزالة OAuth (Google) بالكامل.
"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from starlette.middleware.sessions import SessionMiddleware


# ══════════════════════════════════════════════════════════════
# Paths
# ══════════════════════════════════════════════════════════════
BASE_DIR = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "app" / "static"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

DEBUG = os.getenv("DEBUG", "false").lower() == "true"
if DEBUG:
    from jinja2 import StrictUndefined
    templates.env.undefined = StrictUndefined


# ══════════════════════════════════════════════════════════════
# Session config
# ══════════════════════════════════════════════════════════════
SECRET_KEY = os.getenv("SECRET_KEY", "").strip()
_SECRET_KEY_FROM_ENV = bool(SECRET_KEY)

if not SECRET_KEY:
    _dev_secret_file = Path(".dev_secret_key")
    if _dev_secret_file.exists():
        SECRET_KEY = _dev_secret_file.read_text().strip()
    else:
        SECRET_KEY = secrets.token_urlsafe(48)
        _dev_secret_file.write_text(SECRET_KEY)
        print(
            "⚠️  SECRET_KEY غير مضبوط في البيئة. تم توليد واحد ثابت في "
            ".dev_secret_key — لا تستخدم هذا في الإنتاج."
        )

SESSION_COOKIE_SECURE = os.getenv("SESSION_COOKIE_SECURE", "false").lower() == "true"
SESSION_MAX_AGE = int(os.getenv("SESSION_MAX_AGE", str(30 * 24 * 3600)))
SESSION_IDLE_TIMEOUT = int(os.getenv("SESSION_IDLE_TIMEOUT", str(7 * 24 * 3600)))


# ══════════════════════════════════════════════════════════════
# 5SIM — إعدادات
# ══════════════════════════════════════════════════════════════
SIM5_BASE = os.getenv("SIM5_BASE", "https://5sim.net/v1").rstrip("/")
SIM5_API_KEY = os.getenv("SIM5_API_KEY", "").strip()
SIM5_ENABLED = bool(SIM5_API_KEY)

FIVESIM_MARKUP_PERCENT = float(os.getenv("FIVESIM_MARKUP_PERCENT", "40"))

ADMIN_SECRET = os.getenv("ADMIN_SECRET", "").strip()
VOUCHER_PREFIX = os.getenv("VOUCHER_PREFIX", "JS").strip().upper()
ACCESS_CODE_PREFIX = os.getenv("ACCESS_CODE_PREFIX", "JS-LOGIN").strip().upper()

_SIM5_CACHE: dict[str, dict] = {
    "prices": {"data": None, "at": 0.0},
    "countries": {"data": None, "at": 0.0},
}
_SIM5_LOCK = asyncio.Lock()


class Sim5Error(Exception):
    """خطأ من 5SIM مع رمز الحالة ورسالة عربية واضحة."""
    def __init__(self, status_code: int, message: str, endpoint: str = ""):
        self.status = status_code
        self.message = message
        self.endpoint = endpoint
        super().__init__(f"[{status_code}] {message} ({endpoint})")


def _sim5_explain_400(text: str) -> str:
    """ترجمة رسائل 400 الشائعة من 5SIM إلى العربية."""
    t = text.lower()
    if "no free phones" in t:
        return "لا توجد أرقام متاحة حاليًا لهذه الدولة/الخدمة"
    if "not enough user balance" in t:
        return "رصيد 5SIM غير كافٍ"
    if "not enough rating" in t:
        return "تقييمك في 5SIM منخفض جدًا"
    if "bad country" in t:
        return "الدولة غير مدعومة في 5SIM"
    if "bad operator" in t:
        return "المشغل غير مدعوم في 5SIM"
    if "no product" in t:
        return "الخدمة غير مدعومة في 5SIM"
    if "server offline" in t:
        return "خادم 5SIM غير متصل"
    if "select country" in t:
        return "يجب تحديد الدولة"
    if "select operator" in t:
        return "يجب تحديد المشغل"
    if "reuse not possible" in t or "reuse false" in t:
        return "إعادة استخدام الرقم غير ممكنة"
    if "reuse expired" in t:
        return "انتهت صلاحية إعادة استخدام الرقم"
    return f"خطأ من 5SIM: {text[:200]}"


async def _sim5_get(path: str, auth: bool = False, timeout: float = 25.0) -> Any:
    """استدعاء GET واحد إلى 5SIM مع ترجمة كاملة للأخطاء."""
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


# ── دوال مساعدة للعرض فقط ──
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
    """سعر البيع = تكلفة 5SIM + هامش الربح."""
    if fivesim_cost <= 0:
        return 0.0
    markup = fivesim_cost * (FIVESIM_MARKUP_PERCENT / 100.0)
    return round(fivesim_cost + markup, 2)


async def fetch_sim5_countries(force: bool = False) -> dict:
    now = time.time()
    cached = _SIM5_CACHE["countries"]
    if not force and cached["data"] and (now - cached["at"]) < 300:
        return cached["data"]

    async with _SIM5_LOCK:
        now = time.time()
        if not force and cached["data"] and (now - cached["at"]) < 300:
            return cached["data"]
        data = await _sim5_get("/guest/countries", auth=False)
        _SIM5_CACHE["countries"] = {"data": data, "at": time.time()}
        return data


async def fetch_sim5_prices(force: bool = False) -> dict:
    now = time.time()
    cached = _SIM5_CACHE["prices"]
    if not force and cached["data"] and (now - cached["at"]) < 60:
        return cached["data"]

    async with _SIM5_LOCK:
        now = time.time()
        if not force and cached["data"] and (now - cached["at"]) < 60:
            return cached["data"]
        data = await _sim5_get("/guest/prices", auth=False)
        _SIM5_CACHE["prices"] = {"data": data, "at": time.time()}
        return data


async def build_live_catalog(force: bool = False) -> dict:
    """
    يبني كتالوج الدول والخدمات من 5SIM فقط.
    ✅ الأسعار المعروضة = سعر 5SIM + هامش الربح.
    """
    if not SIM5_ENABLED:
        return {
            "ok": False,
            "countries": [],
            "error": "SIM5_API_KEY غير مضبوط في متغيرات البيئة",
            "error_code": None,
            "fetched_at": None,
        }

    try:
        raw_countries = await fetch_sim5_countries(force=force)
        raw_prices = await fetch_sim5_prices(force=force)
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
# Public / Protected paths
# ══════════════════════════════════════════════════════════════
PUBLIC_PATHS = (
    "/health",
    "/status",
    "/logout",
    "/auth/logout",
    "/static",
    "/favicon.ico",
    "/api/v1/system",
    "/api/v1/prices",
    "/api/v1/auth/login-with-code",
)

PROTECTED_PREFIXES = (
    "/buy",
    "/dashboard",
    "/catalog",
    "/my-numbers",
    "/orders",
    "/inbox",
    "/wallet",
    "/transactions",
    "/support",
    "/profile",
    "/security",
    "/admin-panel",
)


# ══════════════════════════════════════════════════════════════
# Keepalive
# ══════════════════════════════════════════════════════════════
KA_INTERVAL = 420
START_TIME = time.time()

KA_STATE: dict[str, Any] = {
    "server_ok": True,
    "db_ok": True,
    "last_ping": None,
    "last_status": None,
    "ping_count": 0,
    "fail_count": 0,
    "uptime": "0:00:00",
    "next_ping_in": KA_INTERVAL,
    "history": [],
}

HISTORY_MAX = 20
_alert_flag = {"down": False}
_subscribers: set[asyncio.Queue] = set()


# ══════════════════════════════════════════════════════════════
# Request stats
# ══════════════════════════════════════════════════════════════
_REQUEST_STATS: dict[str, Any] = {
    "total": 0,
    "protected_401": 0,
    "protected_redirect": 0,
    "session_expired": 0,
    "session_recovered": 0,
    "last_401": None,
    "last_401_path": None,
    "top_401_paths": {},
    "top_redirect_paths": {},
}
_401_LOCK = asyncio.Lock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fmt_uptime(seconds: float) -> str:
    h, r = divmod(int(seconds), 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}"


def _is_safe_next(next_url: str) -> bool:
    if not next_url:
        return False
    if not next_url.startswith("/"):
        return False
    if next_url.startswith("//") or next_url.startswith("/\\"):
        return False
    parsed = urlparse(next_url)
    if parsed.scheme or parsed.netloc:
        return False
    return True


def _session_is_expired(request: Request) -> bool:
    try:
        last_seen_iso = request.session.get("_last_seen")
    except Exception:
        return True

    if not last_seen_iso:
        return False

    try:
        last_seen = datetime.fromisoformat(last_seen_iso)
        if last_seen.tzinfo is None:
            last_seen = last_seen.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return True

    now = datetime.now(timezone.utc)
    return (now - last_seen).total_seconds() > SESSION_IDLE_TIMEOUT


def _push_history(ok: bool) -> None:
    KA_STATE["history"].append({"ok": ok, "time": _now_iso()})
    if len(KA_STATE["history"]) > HISTORY_MAX:
        KA_STATE["history"] = KA_STATE["history"][-HISTORY_MAX:]


async def _broadcast(event: str, data: dict) -> None:
    payload = {"event": event, "data": json.dumps(data, default=str)}
    for q in list(_subscribers):
        try:
            q.put_nowait(payload)
        except asyncio.QueueFull:
            pass


# ══════════════════════════════════════════════════════════════
# Background keepalive
# ══════════════════════════════════════════════════════════════
async def _keepalive_loop(app: FastAPI) -> None:
    await asyncio.sleep(5)
    async with httpx.AsyncClient(timeout=10.0) as client:
        while True:
            try:
                r = await client.get("http://127.0.0.1:8000/health")
                KA_STATE["last_status"] = r.status_code
                KA_STATE["last_ping"] = _now_iso()
                ok = r.status_code == 200
                if ok:
                    KA_STATE["ping_count"] += 1
                    KA_STATE["server_ok"] = True
                    _push_history(True)
                    if _alert_flag["down"]:
                        _alert_flag["down"] = False
                        await _broadcast("recover", {
                            "message": "✅ الخدمة عادت للعمل",
                            "time": _now_iso(),
                        })
                else:
                    KA_STATE["fail_count"] += 1
                    KA_STATE["server_ok"] = False
                    _push_history(False)
                    if not _alert_flag["down"]:
                        _alert_flag["down"] = True
                        await _broadcast("alert", {
                            "message": f"⚠️ الخدمة أررجعت HTTP {r.status_code}",
                            "time": _now_iso(),
                        })
            except Exception as exc:
                KA_STATE["fail_count"] += 1
                KA_STATE["server_ok"] = False
                KA_STATE["last_ping"] = _now_iso()
                KA_STATE["last_status"] = None
                _push_history(False)
                if not _alert_flag["down"]:
                    _alert_flag["down"] = True
                    await _broadcast("alert", {
                        "message": f"🔴 الخدمة لا تستجيب: {type(exc).__name__}",
                        "time": _now_iso(),
                    })
            await asyncio.sleep(KA_INTERVAL)


# ══════════════════════════════════════════════════════════════
# Lifespan
# ══════════════════════════════════════════════════════════════
@asynccontextmanager
async def lifespan(app: FastAPI):
    if _SECRET_KEY_FROM_ENV:
        print("✅ SECRET_KEY مضبوط من متغيرات البيئة.")
    else:
        print("🔴 تحذير: SECRET_KEY غير مضبوط في متغيرات البيئة!")

    print("✅ تسجيل الدخول بالكود مفعّل (Access Codes).")
    print("✅ الشاشة الموحّدة /buy?tab=... مفعّلة.")

    if SIM5_ENABLED:
        print(f"✅ 5SIM مفعّل — Base: {SIM5_BASE}")
        print(f"✅ هامش الربح: {FIVESIM_MARKUP_PERCENT}%")
        try:
            profile = await _sim5_get("/user/profile", auth=True)
            print(f"✅ 5SIM profile: balance={profile.get('balance')} rating={profile.get('rating')}")
        except Sim5Error as e:
            print(f"⚠️  5SIM profile check failed: [{e.status}] {e.message}")
    else:
        print("🔴 5SIM معطّل — SIM5_API_KEY غير مضبوط.")

    if ADMIN_SECRET:
        print("✅ ADMIN_SECRET مضبوط — لوحة الأدمن مفعّلة.")
    else:
        print("🔴 تحذير: ADMIN_SECRET غير مضبوط — endpoints الأدمن معطّلة!")

    idle_days = SESSION_IDLE_TIMEOUT / 86400
    print(f"🔧 DEBUG={DEBUG} | Version={app.version} | Idle={idle_days:.1f}d")

    task = asyncio.create_task(_keepalive_loop(app))
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


# ══════════════════════════════════════════════════════════════
# App
# ══════════════════════════════════════════════════════════════
app = FastAPI(
    title="Jamal Solutions — Customer Portal",
    description="Virtual Numbers Platform",
    version="1.4.0",
    lifespan=lifespan,
)

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ══════════════════════════════════════════════════════════════
# In-memory store
# ══════════════════════════════════════════════════════════════
STORE: dict[str, Any] = {
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


# ══════════════════════════════════════════════════════════════
# Customer helpers
# ══════════════════════════════════════════════════════════════
def _get_customer_from_session(request: Request) -> dict | None:
    """يرجع العميل، أو يعيد إنشاؤه من الجلسة بعد restart."""
    try:
        cid = request.session.get("customer_id")
    except Exception:
        return None

    if not cid:
        return None

    if cid in STORE["customers"]:
        return STORE["customers"][cid]

    email = request.session.get("email")
    name = request.session.get("name")
    picture = request.session.get("picture")

    if not email:
        return None

    customer = {
        "id": cid,
        "name": name or email.split("@")[0],
        "email": email,
        "phone": "",
        "country": "",
        "initials": (name or email)[:2],
        "picture": picture,
        "balance": 0.0,
        "currency": "USD",
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "email_verified": True,
        "phone_verified": False,
        "two_fa_enabled": False,
        "has_strong_password": False,
        "security_score": 40,
        "prefs": {
            "email_notifications": True,
            "sms_notifications": False,
            "marketing_emails": False,
        },
    }
    STORE["customers"][cid] = customer
    print(f"♻️  أُعيد إنشاء العميل #{cid} ({email}) من الجلسة.")
    return customer


async def get_current_customer(request: Request) -> dict:
    customer = _get_customer_from_session(request)
    if customer is None:
        raise HTTPException(status_code=401, detail="غير مسجّل")
    return customer


async def get_my_stats(customer_id: int) -> dict:
    nums = [n for n in STORE["numbers"] if n["customer_id"] == customer_id]
    ords = [o for o in STORE["orders"] if o["customer_id"] == customer_id]
    sms = [s for s in STORE["sms"] if s["customer_id"] == customer_id]
    txs = [t for t in STORE["transactions"] if t["customer_id"] == customer_id]

    today = datetime.now(timezone.utc).date()
    sms_today = sum(
        1 for s in sms
        if datetime.fromisoformat(s["created_at"]).date() == today
    )

    total_spent = sum(-t["amount"] for t in txs if t["type"] == "purchase")
    month_start = today.replace(day=1)
    spent_this_month = sum(
        -t["amount"] for t in txs
        if t["type"] == "purchase"
        and datetime.fromisoformat(t["created_at"]).date() >= month_start
    )

    return {
        "my_numbers": len(nums),
        "active_numbers": sum(1 for n in nums if n["status"] == "active"),
        "expiring_soon": sum(1 for n in nums if n["status"] == "expiring"),
        "my_orders": len(ords),
        "total_orders": len(ords),
        "pending_orders": sum(1 for o in ords if o["status"] in ("PENDING", "pending")),
        "active_orders": sum(1 for o in ords if o["status"] in ("RECEIVED", "active")),
        "completed_orders": sum(1 for o in ords if o["status"] in ("FINISHED", "completed")),
        "my_sms": len(sms),
        "sms_today": sms_today,
        "unread_sms": sum(1 for s in sms if not s["read"]),
        "total_spent": total_spent,
        "spent_this_month": spent_this_month,
    }


async def get_my_orders_by_status(customer_id: int) -> dict:
    buckets = {"pending": 0, "active": 0, "completed": 0, "expired": 0, "cancelled": 0}
    for o in STORE["orders"]:
        if o["customer_id"] != customer_id:
            continue
        st = (o.get("status") or "").upper()
        if st == "PENDING":
            buckets["pending"] += 1
        elif st == "RECEIVED":
            buckets["active"] += 1
        elif st == "FINISHED":
            buckets["completed"] += 1
        elif st in ("TIMEOUT", "EXPIRED"):
            buckets["expired"] += 1
        elif st in ("CANCELED", "CANCELLED", "BANNED"):
            buckets["cancelled"] += 1
    return buckets


async def get_my_top_services(customer_id: int) -> list[dict]:
    counts: dict[str, int] = {}
    for n in STORE["numbers"]:
        if n["customer_id"] == customer_id:
            sid = n.get("service_id") or n.get("service") or ""
            counts[sid] = counts.get(sid, 0) + 1

    rows = [{"name": sid, "icon": _service_icon(sid), "count": cnt}
            for sid, cnt in counts.items()]
    rows.sort(key=lambda r: r["count"], reverse=True)
    return rows[:5]


async def get_my_numbers(customer_id: int) -> list[dict]:
    return [n for n in STORE["numbers"] if n["customer_id"] == customer_id]


async def get_my_recent_sms(customer_id: int, limit: int = 20) -> list[dict]:
    rows = [s for s in STORE["sms"] if s["customer_id"] == customer_id]
    rows.sort(key=lambda s: s["created_at"], reverse=True)
    return rows[:limit]


# ══════════════════════════════════════════════════════════════
# Vouchers helpers
# ══════════════════════════════════════════════════════════════
def _gen_voucher_code(value: float) -> str:
    """يولّد كوداً فريداً بصيغة JS-500-A1B2-C3D4"""
    part1 = secrets.token_hex(2).upper()
    part2 = secrets.token_hex(2).upper()
    val_str = f"{value:g}".replace(".", "")
    return f"{VOUCHER_PREFIX}-{val_str}-{part1}-{part2}"


def _create_vouchers_batch(
    count: int,
    value: float,
    expires_days: int = 30,
    note: str = "",
) -> list[dict]:
    """يولّد دفعة أكواد جديدة."""
    batch_id = f"batch_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
    now = datetime.now(timezone.utc)
    expires = now + timedelta(days=expires_days)
    created: list[dict] = []

    for _ in range(count):
        while True:
            code = _gen_voucher_code(value)
            if not any(v["code"] == code for v in STORE["vouchers"]):
                break

        voucher = {
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
        STORE["vouchers"].append(voucher)
        created.append(voucher)

    return created


def _validate_voucher(code: str) -> dict:
    """يتحقق من صلاحية الكود ويرجعه."""
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

    return v


# ══════════════════════════════════════════════════════════════
# Access Codes helpers (تسجيل دخول بالكود)
# ══════════════════════════════════════════════════════════════
def _gen_access_code() -> str:
    """يولّد كود دخول بصيغة JS-LOGIN-XXXXXXXXXXXX"""
    return f"{ACCESS_CODE_PREFIX}-{secrets.token_hex(6).upper()}"


def _create_access_code(
    label: str = "",
    expires_days: int = 30,
    max_uses: int = 1,
    preset_name: str = "",
    preset_email: str = "",
    preset_balance: float = 0.0,
) -> dict:
    """
    يولّد كود دخول جديد.
    - label: اسم/ملاحظة للتعرف على الكود
    - expires_days: صلاحية الكود بالأيام
    - max_uses: عدد مرات الاستخدام (1 = استخدام واحد)
    - preset_*: بيانات الحساب الذي سيُنشأ عند أول استخدام
    """
    while True:
        code = _gen_access_code()
        if not any(c["code"] == code for c in STORE["access_codes"]):
            break    now = datetime.now(timezone.utc)
    record = {
        "id": len(STORE["access_codes"]) + 1,
        "code": code,
        "label": label,
        "status": "active",  # active | used | expired | disabled
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


def _validate_access_code(code: str) -> dict:
    """يتحقق من صلاحية كود الدخول ويُرجعه."""
    code = (code or "").strip().upper()
    if not code:
        raise HTTPException(400, "الرجاء إدخال الكود")

    record = next((c for c in STORE["access_codes"] if c["code"] == code), None)
    if not record:
        raise HTTPException(404, "الكود غير صحيح")

    if record["status"] == "disabled":
        raise HTTPException(400, "الكود معطّل")
    if record["status"] == "expired":
        raise HTTPException(400, "انتهت صلاحية الكود")
    if record["status"] == "used" and record["uses_count"] >= record["max_uses"]:
        raise HTTPException(400, "تم استخدام هذا الكود بالكامل")

    try:
        exp = datetime.fromisoformat(record["expires_at"])
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) > exp:
            record["status"] = "expired"
            raise HTTPException(400, "انتهت صلاحية الكود")
    except (ValueError, TypeError):
        pass

    return record


def _consume_access_code(record: dict, customer_id: int) -> None:
    """يسجّل استخدام الكود ويربطه بالعميل."""
    record["uses_count"] += 1
    record["used_by"].append(customer_id)
    record["last_used_at"] = _now_iso()
    if record["uses_count"] >= record["max_uses"]:
        record["status"] = "used"


# ══════════════════════════════════════════════════════════════
# Admin helper
# ══════════════════════════════════════════════════════════════
def _check_admin(request: Request) -> None:
    """يتحقق من صلاحية الأدمن (كلمة سر في الهيدر)."""
    if not ADMIN_SECRET:
        raise HTTPException(503, "ADMIN_SECRET غير مضبوط")
    provided = request.headers.get("X-Admin-Secret", "")
    if not secrets.compare_digest(provided, ADMIN_SECRET):
        raise HTTPException(403, "غير مصرح")


# ══════════════════════════════════════════════════════════════
# Pydantic models
# ══════════════════════════════════════════════════════════════
class OrderCreate(BaseModel):
    country: str
    service: str


class DepositRequestCreate(BaseModel):
    amount: float = Field(..., ge=5)
    method: str
    reference: str


class VoucherRedeem(BaseModel):
    code: str


class VoucherBatchCreate(BaseModel):
    count: int = Field(..., ge=1, le=1000)
    value: float = Field(..., ge=1)
    expires_days: int = Field(30, ge=1, le=365)
    note: str = ""


class TicketCreate(BaseModel):
    subject: str
    message: str
    order_id: str | None = None


class PasswordChange(BaseModel):
    current: str
    new: str = Field(..., min_length=8)


class ProfileUpdate(BaseModel):
    name: str | None = None
    phone: str | None = None
    country: str | None = None


class PreferencesUpdate(BaseModel):
    email_notifications: bool | None = None
    sms_notifications: bool | None = None
    marketing_emails: bool | None = None


class AccessCodeLogin(BaseModel):
    code: str


class AccessCodeCreate(BaseModel):
    label: str = ""
    expires_days: int = Field(30, ge=1, le=365)
    max_uses: int = Field(1, ge=1, le=100)
    preset_name: str = ""
    preset_email: str = ""
    preset_balance: float = Field(0.0, ge=0)


# ══════════════════════════════════════════════════════════════
# System routes
# ══════════════════════════════════════════════════════════════
@app.get("/health")
async def health():
    return {
        "status": "ok",
        "uptime": _fmt_uptime(time.time() - START_TIME),
        "timestamp": _now_iso(),
    }


@app.get("/api/v1/system/health-check")
async def health_check():
    return {
        "status": "healthy",
        "version": app.version,
        "uptime": _fmt_uptime(time.time() - START_TIME),
        "timestamp": _now_iso(),
    }


@app.get("/api/v1/system/version")
async def system_version():
    sim5_info: dict[str, Any] = {"enabled": SIM5_ENABLED}
    if SIM5_ENABLED:
        try:
            p = await _sim5_get("/user/profile", auth=True)
            sim5_info.update({
                "balance": p.get("balance"),
                "rating": p.get("rating"),
                "email": p.get("email"),
                "vendor": p.get("vendor"),
                "ok": True,
            })
        except Sim5Error as e:
            sim5_info.update({"ok": False, "error": e.message, "error_code": e.status})
    return {
        "version": app.version,
        "features": {
            "vouchers": True,
            "owner_wallet": True,
            "markup_percent": FIVESIM_MARKUP_PERCENT,
            "deposit_requests": True,
            "admin_endpoints": bool(ADMIN_SECRET),
            "login_method": "access_code",
            "access_codes": True,
            "single_screen_buy": True,
        },
        "sim5": sim5_info,
        "customers_in_store": len(STORE["customers"]),
        "vouchers_in_store": len(STORE["vouchers"]),
        "access_codes_in_store": len(STORE["access_codes"]),
        "uptime": _fmt_uptime(time.time() - START_TIME),
    }


@app.get("/api/v1/system/sim5-profile")
async def sim5_profile():
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


@app.get("/api/v1/system/whoami")
async def whoami(request: Request):
    cid = request.session.get("customer_id")
    customer_exists = cid in STORE["customers"] if cid else False
    return {
        "has_session": bool(request.session),
        "customer_id": cid,
        "customer_in_store": customer_exists,
        "email": request.session.get("email"),
        "name": request.session.get("name"),
        "customers_count": len(STORE["customers"]),
    }


@app.get("/api/v1/system/request-stats")
async def request_stats():
    return _REQUEST_STATS


@app.get("/status", response_class=HTMLResponse)
async def status_page(request: Request):
    uptime = _fmt_uptime(time.time() - START_TIME)
    history = KA_STATE["history"][-20:]
    rows = "".join(
        f"<tr><td>{h['time']}</td>"
        f"<td style='color:{'green' if h['ok'] else 'red'}'>"
        f"{'✓ OK' if h['ok'] else '✗ FAIL'}</td></tr>"
        for h in reversed(history)
    ) or "<tr><td colspan=2>لا توجد بيانات بعد</td></tr>"

    sim5_status = "🔴 معطّل"
    if SIM5_ENABLED:
        try:
            p = await _sim5_get("/user/profile", auth=True)
            sim5_status = f"✅ متصل — رصيد: {p.get('balance')} — تقييم: {p.get('rating')}"
        except Sim5Error as e:
            sim5_status = f"⚠️ خطأ [{e.status}] {e.message}"

    html = f"""<!doctype html>
<html lang="ar" dir="rtl">
<head>
  <meta charset="utf-8">
  <title>حالة الخدمة — جمال سولوشنز</title>
  <style>
    body {{ font-family: system-ui; padding: 2rem; background:#f8fafc; color:#1e293b; }}
    .card {{ background:#fff; border-radius:12px; padding:1.5rem; box-shadow:0 1px 3px rgba(0,0,0,.08); max-width:720px; margin:auto 0 1rem; }}
    h1 {{ font-size:1.25rem; margin:0 0 1rem; }}
    table {{ width:100%; border-collapse:collapse; font-size:14px; }}
    td {{ padding:8px; border-bottom:1px solid #e2e8f0; }}
    .meta {{ display:flex; gap:1rem; margin-bottom:1rem; font-size:14px; color:#475569; flex-wrap:wrap; }}
    .ok  {{ color:#16a34a; font-weight:600; }}
    .bad {{ color:#dc2626; font-weight:600; }}
    .badge {{ display:inline-block; background:#dbeafe; color:#1e40af; padding:2px 8px; border-radius:6px; font-size:12px; }}
  </style>
</head>
<body>
  <div class="card">
    <h1>🩺 حالة الخدمة <span class="badge">v{app.version}</span></h1>
    <div class="meta">
      <span>Uptime: <b>{uptime}</b></span>
      <span>Ping ✓: <b class="ok">{KA_STATE['ping_count']}</b></span>
      <span>Fail ✗: <b class="bad">{KA_STATE['fail_count']}</b></span>
    </div>
    <div class="meta">
      <span>عملاء: <b>{len(STORE['customers'])}</b></span>
      <span>أكواد شحن: <b>{len(STORE['vouchers'])}</b></span>
      <span>أكواد دخول: <b>{len(STORE['access_codes'])}</b></span>
      <span>هامش: <b>{FIVESIM_MARKUP_PERCENT}%</b></span>
    </div>
    <h1>🔌 5SIM</h1>
    <div class="meta"><span>{sim5_status}</span></div>
    <h2 style="font-size:1rem;margin:1rem 0 .5rem;color:#475569;">آخر عمليات الفحص</h2>
    <table><thead><tr><th align="right">الوقت</th><th align="right">الحالة</th></tr></thead>
    <tbody>{rows}</tbody></table>
  </div>
</body>
</html>"""
    return HTMLResponse(html)


@app.get("/api/v1/system/keepalive-status")
async def keepalive_status():
    elapsed = int(time.time() - START_TIME)
    KA_STATE["uptime"] = _fmt_uptime(elapsed)
    KA_STATE["next_ping_in"] = max(0, KA_INTERVAL - (elapsed % KA_INTERVAL))
    return JSONResponse(KA_STATE)


@app.get("/api/v1/system/events")
async def sse_events(request: Request):
    queue: asyncio.Queue = asyncio.Queue(maxsize=100)
    _subscribers.add(queue)

    async def event_generator():
        elapsed = int(time.time() - START_TIME)
        KA_STATE["uptime"] = _fmt_uptime(elapsed)
        yield {"event": "status", "data": json.dumps(KA_STATE, default=str)}

        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield payload
                except asyncio.TimeoutError:
                    yield {"event": "heartbeat", "data": str(int(time.time()))}
        finally:
            _subscribers.discard(queue)

    return EventSourceResponse(event_generator())


# ══════════════════════════════════════════════════════════════
# API — Me
# ══════════════════════════════════════════════════════════════
@app.get("/api/v1/me/stats")
async def me_stats(request: Request):
    customer = _get_customer_from_session(request)
    if not customer:
        return JSONResponse(
            {"error": "session_expired", "detail": "انتهت الجلسة"},
            status_code=401,
        )
    return {
        "customer": {
            "id": customer["id"],
            "name": customer["name"],
            "email": customer["email"],
            "balance": customer["balance"],
        },
        "stats": await get_my_stats(customer["id"]),
    }


# ══════════════════════════════════════════════════════════════
# API — Prices
# ══════════════════════════════════════════════════════════════
@app.get("/api/v1/prices")
async def api_prices(country: str | None = None):
    result = await build_live_catalog()
    countries = result["countries"]
    if country and result["ok"]:
        c_lower = country.lower()
        countries = [
            c for c in countries
            if c["slug"] == c_lower or c["code"].lower() == c_lower
        ]
    return {
        "ok": result["ok"],
        "source": "5sim",
        "markup_percent": FIVESIM_MARKUP_PERCENT,
        "fetched_at": result["fetched_at"],
        "error": result["error"],
        "error_code": result["error_code"],
        "count": len(countries),
        "countries": countries,
    }


@app.post("/api/v1/prices/refresh")
async def api_prices_refresh(request: Request):
    await get_current_customer(request)
    if not SIM5_ENABLED:
        raise HTTPException(503, "5SIM غير مفعّل")
    try:
        await fetch_sim5_countries(force=True)
        await fetch_sim5_prices(force=True)
    except Sim5Error as e:
        raise HTTPException(e.status or 503, e.message)
    return {"status": "refreshed", "fetched_at": _now_iso()}


# ══════════════════════════════════════════════════════════════
# Auth — Login page + Access Code
# ══════════════════════════════════════════════════════════════
@app.get("/", response_class=HTMLResponse)
async def login_page(request: Request):
    if request.session.get("customer_id"):
        if _session_is_expired(request):
            request.session.clear()
        else:
            customer = _get_customer_from_session(request)
            if customer is not None:
                return RedirectResponse(url="/buy", status_code=303)
            else:
                request.session.clear()

    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={
            "next": request.query_params.get("next", "/buy"),
            "error": request.query_params.get("error"),
        },
    )


@app.post("/api/v1/auth/login-with-code")
async def login_with_code(payload: AccessCodeLogin, request: Request):
    """
    تسجيل دخول العميل باستخدام كود صادر من المدير.
    - إن كان الكود مرتبطاً بعميل سابق → يفتح جلسته.
    - وإلا → ينشئ عميلاً جديداً بالبيانات preset_*.
    """
    record = _validate_access_code(payload.code)

    customer = None
    if record["used_by"]:
        prev_id = record["used_by"][-1]
        customer = STORE["customers"].get(prev_id)

    if customer is None:
        new_id = max(STORE["customers"].keys(), default=0) + 1
        email = record.get("preset_email") or f"user{new_id}@jamal.local"
        name = record.get("preset_name") or record.get("label") or f"عميل {new_id}"
        customer = {
            "id": new_id,
            "name": name,
            "email": email,
            "phone": "",
            "country": "",
            "initials": name[:2],
            "picture": None,
            "balance": float(record.get("preset_balance") or 0.0),
            "currency": "USD",
            "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "email_verified": True,
            "phone_verified": False,
            "two_fa_enabled": False,
            "has_strong_password": False,
            "security_score": 40,
            "access_code_used": record["code"],
            "prefs": {
                "email_notifications": True,
                "sms_notifications": False,
                "marketing_emails": False,
            },
        }
        STORE["customers"][new_id] = customer

        if customer["balance"] > 0:
            STORE["transactions"].append({
                "id": len(STORE["transactions"]) + 1,
                "customer_id": new_id,
                "type": "deposit",
                "description": "رصيد ترحيبي (كود دخول)",
                "amount": customer["balance"],
                "balance_after": customer["balance"],
                "created_at": _now_iso(),
                "date": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
            })

    request.session["customer_id"] = customer["id"]
    request.session["email"] = customer["email"]
    request.session["name"] = customer["name"]
    request.session["logged_in_at"] = _now_iso()
    request.session["_last_seen"] = _now_iso()

    _consume_access_code(record, customer["id"])

    return {
        "ok": True,
        "redirect": "/buy",
        "customer": {
            "id": customer["id"],
            "name": customer["name"],
            "email": customer["email"],
            "balance": customer["balance"],
        },
    }


@app.api_route("/logout", methods=["GET", "POST"], name="logout")
@app.api_route("/auth/logout", methods=["GET", "POST"], name="auth_logout")
async def logout(request: Request):
    next_url = request.query_params.get("next", "/")
    if not _is_safe_next(next_url):
        next_url = "/"
    request.session.clear()
    return RedirectResponse(url=next_url, status_code=303)


# ══════════════════════════════════════════════════════════════
# ⭐ الشاشة الوحيدة: /buy — كل التبويبات
# ══════════════════════════════════════════════════════════════
@app.get("/buy", response_class=HTMLResponse)
async def buy_page(
    request: Request,
    tab: str = "buy",
    country: str | None = None,
    service: str | None = None,
    number: str | None = None,
):
    """
    الشاشة الوحيدة للعميل. كل شيء يُدار هنا عبر تبويبات:
      tab = buy | numbers | orders | inbox | wallet | transactions | support | profile
    """
    customer = await get_current_customer(request)
    cid = customer["id"]
    stats = await get_my_stats(cid)

    valid_tabs = {"buy", "numbers", "orders", "inbox", "wallet",
                  "transactions", "support", "profile"}
    if tab not in valid_tabs:
        tab = "buy"

    # ── الكتالوج
    result = await build_live_catalog()
    countries = result["countries"] if result["ok"] else []

    popular: list[dict] = []
    available: list[dict] = []
    if result["ok"]:
        for c in countries:
            for s in c["services"]:
                if s["available"]:
                    row = {
                        "country_slug": c["slug"],
                        "country_code": c["code"],
                        "country_name": c["name"],
                        "country_flag": c["flag"],
                        "service_id": s["id"],
                        "service_name": s["name"],
                        "service_icon": s["icon"],
                        "price": s["cost"],
                        "count": s["count"],
                        "rate": s["rate"],
                    }
                    available.append(row)
                    popular.append(row)
        popular.sort(key=lambda x: x["price"])
        popular = popular[:6]
        available.sort(key=lambda x: x["price"])
        available = available[:24]

    stats["countries_count"] = len(countries)
    stats["services_count"] = sum(c["services_count"] for c in countries)

    # ── أرقامي
    my_numbers = await get_my_numbers(cid)

    # ── الطلبات
    orders = [o for o in STORE["orders"] if o["customer_id"] == cid]
    orders.sort(key=lambda o: o["created_at"], reverse=True)
    orders_by_status = await get_my_orders_by_status(cid)

    # ── الوارد
    sms_list = await get_my_recent_sms(cid, limit=100)
    if number:
        sms_list = [s for s in sms_list if s["phone"] == number]

    # ── المحفظة
    deposits = [d for d in STORE["deposits"] if d["customer_id"] == cid]
    deposits.sort(key=lambda d: d["created_at"], reverse=True)

    # ── المعاملات
    txs = [t for t in STORE["transactions"] if t["customer_id"] == cid]
    txs.sort(key=lambda t: t["created_at"], reverse=True)
    total_deposits = sum(t["amount"] for t in txs if t["type"] == "deposit")
    total_spent = sum(-t["amount"] for t in txs if t["type"] == "purchase")
    month_start = datetime.now(timezone.utc).date().replace(day=1)
    this_month = sum(
        -t["amount"] for t in txs
        if t["type"] == "purchase"
        and datetime.fromisoformat(t["created_at"]).date() >= month_start
    )

    # ── الدعم
    tickets = [t for t in STORE["tickets"] if t["customer_id"] == cid]
    tickets.sort(key=lambda t: t["created_at"], reverse=True)

    return templates.TemplateResponse(
        request=request,
        name="buy.html",
        context={
            "customer": customer,
            "stats": stats,
            "active_tab": tab,

            # شراء
            "countries": countries,
            "preselected_country": country,
            "preselected_service": service,
            "popular_services": popular,
            "available_numbers": available,
            "prices_meta": {
                "ok": result["ok"], "source": "5sim",
                "error": result["error"], "error_code": result["error_code"],
                "fetched_at": result["fetched_at"],
            },

            # أرقامي
            "my_numbers": my_numbers,

            # الطلبات
            "orders": orders,
            "orders_by_status": orders_by_status,

            # الوارد
            "recent_sms": sms_list,
            "sms_filter_number": number,

            # المحفظة
            "recent_deposits": deposits,

            # المعاملات
            "transactions": txs,
            "summary": {
                "total_deposits": total_deposits,
                "total_spent": total_spent,
                "count": len(txs),
                "this_month": this_month,
            },

            # الدعم
            "tickets": tickets,
            "faqs": [],
        },
    )


# ══════════════════════════════════════════════════════════════
# Redirect old page routes → /buy?tab=...
# ══════════════════════════════════════════════════════════════
def _redirect_to_buy(tab: str):
    return RedirectResponse(url=f"/buy?tab={tab}", status_code=303)


@app.get("/dashboard")
async def dashboard_redirect(request: Request):
    await get_current_customer(request)
    return _redirect_to_buy("buy")


@app.get("/catalog")
async def catalog_redirect(request: Request):
    await get_current_customer(request)
    return _redirect_to_buy("buy")


@app.get("/my-numbers")
async def my_numbers_redirect(request: Request):
    await get_current_customer(request)
    return _redirect_to_buy("numbers")


@app.get("/orders")
async def orders_redirect(request: Request):
    await get_current_customer(request)
    return _redirect_to_buy("orders")


@app.get("/orders/{order_id}")
async def order_detail_redirect(order_id: int, request: Request):
    await get_current_customer(request)
    return _redirect_to_buy("orders")


@app.get("/inbox")
async def inbox_redirect(request: Request):
    await get_current_customer(request)
    return _redirect_to_buy("inbox")


@app.get("/wallet")
async def wallet_redirect(request: Request):
    await get_current_customer(request)
    return _redirect_to_buy("wallet")


@app.get("/transactions")
async def transactions_redirect(request: Request):
    await get_current_customer(request)
    return _redirect_to_buy("transactions")


@app.get("/support")
async def support_redirect(request: Request):
    await get_current_customer(request)
    return _redirect_to_buy("support")


@app.get("/profile")
async def profile_redirect(request: Request):
    await get_current_customer(request)
    return _redirect_to_buy("profile")


@app.get("/security")
async def security_redirect(request: Request):
    await get_current_customer(request)
    return _redirect_to_buy("profile")


@app.get("/admin-panel", response_class=HTMLResponse)
async def admin_panel(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="admin.html",
        context={},
    )


# ══════════════════════════════════════════════════════════════
# API — Wallet: Voucher + Deposit Request
# ══════════════════════════════════════════════════════════════
@app.post("/api/v1/wallet/redeem-voucher")
async def redeem_voucher(payload: VoucherRedeem, request: Request):
    """الزبون يدخل الكود → يُضاف رصيد."""
    customer = await get_current_customer(request)
    cid = customer["id"]

    voucher = _validate_voucher(payload.code)

    now = datetime.now(timezone.utc)
    voucher["status"] = "used"
    voucher["used_by"] = cid
    voucher["used_at"] = now.isoformat()

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

    return {
        "status": "success",
        "value": voucher["value"],
        "new_balance": customer["balance"],
    }


@app.post("/api/v1/wallet/deposit-request")
async def create_deposit_request(
    payload: DepositRequestCreate,
    request: Request,
):
    """إنشاء طلب إيداع يدوي — ينتظر موافقة الأدمن."""
    customer = await get_current_customer(request)

    deposit = {
        "id": len(STORE["deposits"]) + 1,
        "customer_id": customer["id"],
        "amount": payload.amount,
        "method": payload.method,
        "reference": payload.reference,
        "status": "pending",
        "created_at": _now_iso(),
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
        "reviewed_at": None,
    }
    STORE["deposits"].append(deposit)

    return {
        "status": "pending",
        "request_id": deposit["id"],
        "message": "طلبك قيد المراجعة",
    }


# ══════════════════════════════════════════════════════════════
# API — Orders (شراء حقيقي من 5SIM مع هامش الربح)
# ══════════════════════════════════════════════════════════════
@app.post("/api/v1/orders")
async def create_order(payload: OrderCreate, request: Request):
    customer = await get_current_customer(request)
    cid = customer["id"]

    if not SIM5_ENABLED:
        raise HTTPException(503, "5SIM غير مفعّل — لا يمكن الشراء")

    result = await build_live_catalog()
    if not result["ok"]:
        raise HTTPException(503, f"تعذّر الاتصال بـ 5SIM: {result['error']}")

    c_slug = payload.country.lower()
    country = next(
        (c for c in result["countries"]
         if c["slug"] == c_slug or c["code"].lower() == c_slug),
        None,
    )
    if not country:
        raise HTTPException(400, "دولة غير مدعومة في 5SIM")

    service = next(
        (s for s in country["services"]
         if s["id"] == payload.service and s["available"]),
        None,
    )
    if not service:
        raise HTTPException(400, "خدمة غير متوفرة في هذه الدولة")

    if not service.get("operator"):
        raise HTTPException(400, "لم يُحدد مشغل متاح لهذه الخدمة")

    expected_price = float(service["cost"])
    if customer["balance"] < expected_price:
        raise HTTPException(
            400,
            f"رصيدك غير كافٍ. المطلوب: ${expected_price:.2f}، المتاح: ${customer['balance']:.2f}"
        )

    try:
        sim5_order = await _sim5_get(
            f"/user/buy/activation/{country['slug']}/{service['operator']}/{service['id']}",
            auth=True,
            timeout=30.0,
        )
    except Sim5Error as e:
        raise HTTPException(
            status_code=400 if 400 <= e.status < 500 else 503,
            detail=f"فشل الشراء من 5SIM: {e.message}",
        )

    fivesim_cost = float(sim5_order.get("price", service["fivesim_cost"] or 0))
    sale_price = _calculate_sale_price(fivesim_cost)
    profit = round(sale_price - fivesim_cost, 4)

    if customer["balance"] < sale_price:
        try:
            await _sim5_get(f"/user/cancel/{sim5_order['id']}", auth=True)
        except Exception:
            pass
        raise HTTPException(
            400,
            f"رصيدك غير كافٍ. المطلوب: ${sale_price:.2f}، المتاح: ${customer['balance']:.2f}"
        )

    now = datetime.now(timezone.utc)
    order_id = len(STORE["orders"]) + 1

    order = {
        "id": order_id,
        "customer_id": cid,
        "sim5_id": sim5_order.get("id"),
        "phone": sim5_order.get("phone", ""),
        "country": country["name"],
        "country_slug": country["slug"],
        "country_code": country["code"],
        "flag": country["flag"],
        "service": service["name"],
        "service_id": service["id"],
        "operator": sim5_order.get("operator", service["operator"]),
        "price": sale_price,
        "fivesim_cost": fivesim_cost,
        "profit": profit,
        "status": sim5_order.get("status", "PENDING"),
        "expires": sim5_order.get("expires"),
        "created_at": sim5_order.get("created_at", now.isoformat()),
        "sms": sim5_order.get("sms") or [],
    }
    STORE["orders"].append(order)

    STORE["numbers"].append({
        "id": len(STORE["numbers"]) + 1,
        "customer_id": cid,
        "sim5_id": sim5_order.get("id"),
        "phone": order["phone"],
        "country": country["name"],
        "country_code": country["code"],
        "flag": country["flag"],
        "service": service["name"],
        "service_id": service["id"],
        "status": "active" if order["status"] in ("PENDING", "RECEIVED") else order["status"],
        "price": sale_price,
        "sms_count": 0,
        "expires": order["expires"],
        "created_at": order["created_at"],
    })

    customer["balance"] -= sale_price

    ow = STORE["owner_wallet"]
    ow["total_profit"] += profit
    ow["reserved_for_5sim"] += fivesim_cost

    STORE["transactions"].append({
        "id": len(STORE["transactions"]) + 1,
        "customer_id": cid,
        "type": "purchase",
        "description": f"شراء رقم {service['name']} — {country['name']}",
        "amount": -sale_price,
        "balance_after": customer["balance"],
        "created_at": now.isoformat(),
        "date": now.strftime("%Y-%m-%d %H:%M"),
    })

    return {
        "id": order_id,
        "sim5_id": order["sim5_id"],
        "phone": order["phone"],
        "status": order["status"],
        "expires": order["expires"],
        "price": sale_price,
    }


@app.get("/api/v1/orders/sync")
async def sync_orders_from_sim5(request: Request, category: str = "activation"):
    customer = await get_current_customer(request)
    if not SIM5_ENABLED:
        raise HTTPException(503, "5SIM غير مفعّل")
    try:
        data = await _sim5_get(
            f"/user/orders?category={category}&limit=100&offset=0&order=id&reverse=true",
            auth=True,
        )
    except Sim5Error as e:
        raise HTTPException(e.status or 503, e.message)

    sim5_orders = data.get("Data") or []
    existing = {o.get("sim5_id") for o in STORE["orders"]}

    imported = 0
    for so in sim5_orders:
        if so["id"] in existing:
            continue
        STORE["orders"].append({
            "id": len(STORE["orders"]) + 1,
            "customer_id": customer["id"],
            "sim5_id": so["id"],
            "phone": so.get("phone", ""),
            "country": so.get("country", ""),
            "country_slug": so.get("country", ""),
            "country_code": "",
            "flag": "🏳️",
            "service": so.get("product", ""),
            "service_id": so.get("product", ""),
            "operator": so.get("operator"),
            "price": float(so.get("price", 0)),
            "fivesim_cost": float(so.get("price", 0)),
            "profit": 0.0,
            "status": so.get("status", ""),
            "expires": so.get("expires"),
            "created_at": so.get("created_at"),
            "sms": so.get("sms") or [],
        })
        imported += 1

    return {"ok": True, "imported": imported, "total_from_sim5": len(sim5_orders)}


@app.get("/api/v1/orders/{order_id}/check")
async def check_order(order_id: int, request: Request):
    customer = await get_current_customer(request)
    order = next(
        (o for o in STORE["orders"]
         if o["id"] == order_id and o["customer_id"] == customer["id"]),
        None,
    )
    if not order or not order.get("sim5_id"):
        raise HTTPException(404, "الطلب غير موجود")

    try:
        data = await _sim5_get(f"/user/check/{order['sim5_id']}", auth=True, timeout=15.0)
    except Sim5Error as e:
        raise HTTPException(e.status or 503, e.message)

    order["status"] = data.get("status", order["status"])
    order["expires"] = data.get("expires", order.get("expires"))

    existing_keys = {(s.get("order_id"), s.get("created_at")) for s in STORE["sms"]}
    imported = 0
    for sms in (data.get("sms") or []):
        key = (order_id, sms.get("created_at"))
        if key in existing_keys:
            continue
        STORE["sms"].append({
            "id": len(STORE["sms"]) + 1,
            "sim5_id": sms.get("id"),
            "customer_id": customer["id"],
            "order_id": order_id,
            "phone": order["phone"],
            "sender": sms.get("sender", ""),
            "text": sms.get("text", ""),
            "code": sms.get("code", ""),
            "created_at": sms.get("created_at") or _now_iso(),
            "read": False,
        })
        imported += 1

    for n in STORE["numbers"]:
        if n.get("sim5_id") == order["sim5_id"]:
            n["sms_count"] = len([s for s in STORE["sms"] if s.get("order_id") == order_id])
            if order["status"] == "RECEIVED":
                n["status"] = "active"

    return {
        "ok": True,
        "status": order["status"],
        "expires": order["expires"],
        "new_sms": imported,
        "sms": data.get("sms") or [],
    }


@app.post("/api/v1/orders/{order_id}/finish")
async def finish_order(order_id: int, request: Request):
    customer = await get_current_customer(request)
    order = next(
        (o for o in STORE["orders"]
         if o["id"] == order_id and o["customer_id"] == customer["id"]),
        None,
    )
    if not order or not order.get("sim5_id"):
        raise HTTPException(404, "الطلب غير موجود")
    try:
        data = await _sim5_get(f"/user/finish/{order['sim5_id']}", auth=True)
    except Sim5Error as e:
        raise HTTPException(e.status or 503, e.message)
    order["status"] = data.get("status", "FINISHED")
    return {"ok": True, "status": order["status"]}


@app.post("/api/v1/orders/{order_id}/cancel")
async def cancel_order(order_id: int, request: Request):
    customer = await get_current_customer(request)
    order = next(
        (o for o in STORE["orders"]
         if o["id"] == order_id and o["customer_id"] == customer["id"]),
        None,
    )
    if not order or not order.get("sim5_id"):
        raise HTTPException(404, "الطلب غير موجود")
    try:
        data = await _sim5_get(f"/user/cancel/{order['sim5_id']}", auth=True)
    except Sim5Error as e:
        raise HTTPException(e.status or 503, e.message)

    order["status"] = data.get("status", "CANCELED")

    refund = order["price"]
    customer["balance"] += refund

    ow = STORE["owner_wallet"]
    ow["total_profit"] -= order.get("profit", 0.0)
    ow["reserved_for_5sim"] -= order.get("fivesim_cost", 0.0)

    STORE["transactions"].append({
        "id": len(STORE["transactions"]) + 1,
        "customer_id": customer["id"],
        "type": "refund",
        "description": f"استرداد طلب ملغي #{order_id}",
        "amount": refund,
        "balance_after": customer["balance"],
        "created_at": _now_iso(),
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
    })

    for n in STORE["numbers"]:
        if n.get("sim5_id") == order["sim5_id"]:
            n["status"] = "cancelled"

    return {"ok": True, "status": order["status"], "refund": refund}


# ══════════════════════════════════════════════════════════════
# API — Admin (vouchers + deposits + wallet + access codes)
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
        "total_value": sum(v["value"] for v in created),
        "codes": [v["code"] for v in created],
    }


@app.get("/api/v1/admin/vouchers")
async def admin_list_vouchers(
    request: Request,
    status_filter: str | None = None,
    batch_id: str | None = None,
):
    _check_admin(request)
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


@app.get("/api/v1/admin/wallet")
async def admin_owner_wallet(request: Request):
    _check_admin(request)
    ow = STORE["owner_wallet"]

    sim5_info: dict[str, Any] = {}
    if SIM5_ENABLED:
        try:
            p = await _sim5_get("/user/profile", auth=True)
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


@app.get("/api/v1/admin/deposits")
async def admin_list_deposits(
    request: Request,
    status_filter: str | None = None,
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
# API — Admin: Access Codes
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


@app.get("/api/v1/admin/access-codes")
async def admin_list_access_codes(
    request: Request,
    status_filter: str | None = None,
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


@app.post("/api/v1/admin/access-codes/{code_id}/disable")
async def admin_disable_access_code(code_id: int, request: Request):
    _check_admin(request)
    record = next((c for c in STORE["access_codes"] if c["id"] == code_id), None)
    if not record:
        raise HTTPException(404, "الكود غير موجود")
    record["status"] = "disabled"
    return {"ok": True, "status": "disabled"}


@app.delete("/api/v1/admin/access-codes/{code_id}")
async def admin_delete_access_code(code_id: int, request: Request):
    _check_admin(request)
    before = len(STORE["access_codes"])
    STORE["access_codes"] = [c for c in STORE["access_codes"] if c["id"] != code_id]
    if len(STORE["access_codes"]) == before:
        raise HTTPException(404, "الكود غير موجود")
    return {"ok": True, "deleted": True}


# ══════════════════════════════════════════════════════════════
# API — SMS
# ══════════════════════════════════════════════════════════════
@app.delete("/api/v1/sms/{sms_id}")
async def delete_sms(sms_id: int, request: Request):
    customer = await get_current_customer(request)
    for i, s in enumerate(STORE["sms"]):
        if s["id"] == sms_id and s["customer_id"] == customer["id"]:
            STORE["sms"].pop(i)
            return {"status": "deleted"}
    raise HTTPException(status_code=404, detail="الرسالة غير موجودة")


# ══════════════════════════════════════════════════════════════
# API — Support
# ══════════════════════════════════════════════════════════════
@app.post("/api/v1/support/tickets")
async def create_ticket(payload: TicketCreate, request: Request):
    customer = await get_current_customer(request)
    cid = customer["id"]
    now = datetime.now(timezone.utc)
    ticket = {
        "id": len(STORE["tickets"]) + 1,
        "customer_id": cid,
        "subject": payload.subject,
        "preview": payload.message[:80],
        "message": payload.message,
        "order_id": payload.order_id,
        "status": "open",
        "created_at": now.isoformat(),
        "date": now.strftime("%Y-%m-%d %H:%M"),
    }
    STORE["tickets"].append(ticket)
    return {"id": ticket["id"], "status": "open"}


# ══════════════════════════════════════════════════════════════
# API — Profile
# ══════════════════════════════════════════════════════════════
@app.patch("/api/v1/profile")
async def update_profile(payload: ProfileUpdate, request: Request):
    customer = await get_current_customer(request)
    if payload.name is not None:
        customer["name"] = payload.name
        customer["initials"] = payload.name[:2]
    if payload.phone is not None:
        customer["phone"] = payload.phone
    if payload.country is not None:
        customer["country"] = payload.country
    return {"status": "updated"}


@app.patch("/api/v1/profile/preferences")
async def update_preferences(payload: PreferencesUpdate, request: Request):
    customer = await get_current_customer(request)
    prefs = customer.setdefault("prefs", {})
    if payload.email_notifications is not None:
        prefs["email_notifications"] = payload.email_notifications
    if payload.sms_notifications is not None:
        prefs["sms_notifications"] = payload.sms_notifications
    if payload.marketing_emails is not None:
        prefs["marketing_emails"] = payload.marketing_emails
    return {"status": "updated"}


# ══════════════════════════════════════════════════════════════
# API — Security
# ══════════════════════════════════════════════════════════════
@app.put("/api/v1/security/password")
async def change_password(payload: PasswordChange, request: Request):
    await get_current_customer(request)
    if len(payload.new) < 8:
        raise HTTPException(status_code=400, detail="كلمة المرور قصيرة")
    return {"status": "updated"}


@app.post("/api/v1/security/2fa/enable")
async def enable_2fa(request: Request):
    customer = await get_current_customer(request)
    secret = uuid.uuid4().hex[:16].upper()
    customer["two_fa_enabled"] = True
    customer["security_score"] = min(100, customer["security_score"] + 20)
    return {"secret": secret, "status": "enabled"}


@app.post("/api/v1/security/2fa/disable")
async def disable_2fa(request: Request):
    customer = await get_current_customer(request)
    customer["two_fa_enabled"] = False
    customer["security_score"] = max(0, customer["security_score"] - 20)
    return {"status": "disabled"}


@app.delete("/api/v1/security/sessions/{session_id}")
async def end_session(session_id: str, request: Request):
    await get_current_customer(request)
    STORE["sessions"] = [s for s in STORE["sessions"] if s["id"] != session_id]
    return {"status": "ended"}


# ══════════════════════════════════════════════════════════════
# Middleware — Auth Guard
# ══════════════════════════════════════════════════════════════
@app.middleware("http")
async def auth_guard(request: Request, call_next):
    path = request.url.path

    if path == "/":
        return await call_next(request)

    if path.startswith("/api/v1/admin/"):
        return await call_next(request)

    if any(
        path == p or path.startswith(p + "/") or path.startswith(p + "?")
        for p in PUBLIC_PATHS
    ):
        return await call_next(request)

    is_api = path.startswith("/api/")
    is_protected = is_api or any(
        path == p or path.startswith(p + "/") for p in PROTECTED_PREFIXES
    )

    if not is_protected:
        return await call_next(request)

    cid = None
    expired = False
    try:
        cid = request.session.get("customer_id")
        if cid and _session_is_expired(request):
            request.session.clear()
            cid = None
            expired = True
    except Exception:
        cid = None

    if cid:
        customer = _get_customer_from_session(request)
        if customer is None:
            try:
                request.session.clear()
            except Exception:
                pass
            cid = None
            expired = True
        else:
            try:
                request.session["_last_seen"] = _now_iso()
            except Exception:
                pass
            return await call_next(request)

    if is_api:
        return JSONResponse(
            {
                "detail": "انتهت الجلسة" if expired else "غير مسجّل",
                "error": "session_expired" if expired else "not_authenticated",
            },
            status_code=401,
        )

    return RedirectResponse(url="/", status_code=303)


app.add_middleware(
    SessionMiddleware,
    secret_key=SECRET_KEY,
    session_cookie="jamal_session",
    max_age=SESSION_MAX_AGE,
    same_site="lax",
    https_only=SESSION_COOKIE_SECURE,
    path="/",
)
