"""
Jamal Solutions — Virtual Numbers Platform
Customer Portal (FastAPI + Jinja2 + SSE + Google OAuth + 5SIM)

✅ 1.1.1 — إصلاحات بعد النشر الأول:
- إعادة get_my_orders_by_status و get_my_top_services.
- تمريرهما في سياق /dashboard.
- إصلاح خطأ مطبعي في TicketCreate / PasswordChange.

✅ 1.1.0 — قراءة خالصة من 5SIM (لا بيانات وهمية إطلاقًا):
- كل الدول والخدمات والأسعار تُقرأ مباشرة من 5SIM عبر المفتاح.
- لا قوائم يدوية، لا أرقام وهمية، لا أسعار ثابتة، لا fallback.
- عند أي فشل (شبكة/صلاحيات/لا بيانات) → رسالة واضحة في الواجهة.
- الشراء الفعلي عبر /user/buy/activation.
- فحص الطلب عبر /user/check واستيراد SMS.
- إنهاء/إلغاء الطلب عبر /user/finish و /user/cancel.
- الرصيد والتقييم من /user/profile.
- مزامنة سجل الطلبات من /user/orders.
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
from urllib.parse import quote, urlparse

import httpx
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from authlib.integrations.starlette_client import OAuth
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
# OAuth / Session config
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

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
OAUTH_ENABLED = bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)

oauth = OAuth()
if OAUTH_ENABLED:
    oauth.register(
        name="google",
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile"},
    )


# ══════════════════════════════════════════════════════════════
# 5SIM — إعدادات
# ══════════════════════════════════════════════════════════════
SIM5_BASE = os.getenv("SIM5_BASE", "https://5sim.net/v1").rstrip("/")
SIM5_API_KEY = os.getenv("SIM5_API_KEY", "").strip()
SIM5_ENABLED = bool(SIM5_API_KEY)

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


async def _sim5_post(path: str, body: dict, timeout: float = 25.0) -> Any:
    """استدعاء POST إلى 5SIM."""
    if not SIM5_ENABLED:
        raise Sim5Error(503, "5SIM غير مفعّل", path)
    url = f"{SIM5_BASE}{path}"
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {SIM5_API_KEY}",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(url, headers=headers, json=body)
    except httpx.TimeoutException:
        raise Sim5Error(0, "انتهت مهلة الاتصال بـ 5SIM", path)
    except httpx.RequestError as e:
        raise Sim5Error(0, f"فشل الاتصال بـ 5SIM: {type(e).__name__}", path)

    if r.status_code == 200:
        try:
            return r.json()
        except Exception:
            return {}
    if r.status_code == 400:
        raise Sim5Error(400, _sim5_explain_400(r.text), path)
    if r.status_code == 401:
        raise Sim5Error(401, "مفتاح 5SIM غير صالح", path)
    raise Sim5Error(r.status_code, r.text[:200], path)


async def _sim5_delete(path: str, body: dict, timeout: float = 25.0) -> Any:
    """استدعاء DELETE إلى 5SIM."""
    if not SIM5_ENABLED:
        raise Sim5Error(503, "5SIM غير مفعّل", path)
    url = f"{SIM5_BASE}{path}"
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {SIM5_API_KEY}",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.request("DELETE", url, headers=headers, json=body)
    except httpx.RequestError as e:
        raise Sim5Error(0, f"فشل الاتصال بـ 5SIM: {type(e).__name__}", path)

    if r.status_code == 200:
        try:
            return r.json()
        except Exception:
            return {}
    if r.status_code == 400:
        raise Sim5Error(400, _sim5_explain_400(r.text), path)
    raise Sim5Error(r.status_code, r.text[:200], path)


# ── دوال مساعدة للعرض فقط (ليست بيانات) ──
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


async def fetch_sim5_countries(force: bool = False) -> dict:
    """GET /v1/guest/countries — قائمة الدول مع المشغلين."""
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
    """GET /v1/guest/prices — كل الأسعار الحية (بدون توثيق)."""
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
    يُرجع:
      {
        "ok": bool,
        "countries": [...],
        "error": str | None,
        "error_code": int | None,
        "fetched_at": ISO | None,
      }
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
                if best is None or cost < best["cost"]:
                    best = {
                        "cost": cost,
                        "count": count,
                        "rate": rate,
                        "operator": op_name,
                    }

            services.append({
                "id": product_slug,
                "name": product_slug,
                "icon": _service_icon(product_slug),
                "cost": best["cost"] if best else None,
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
    "/auth/google",
    "/auth/google/callback",
    "/auth/logout",
    "/static",
    "/favicon.ico",
    "/api/v1/system",
    "/api/v1/prices",
)

PROTECTED_PREFIXES = (
    "/dashboard",
    "/buy",
    "/catalog",
    "/my-numbers",
    "/orders",
    "/inbox",
    "/wallet",
    "/transactions",
    "/support",
    "/profile",
    "/security",
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
# Request stats (diagnostics)
# ══════════════════════════════════════════════════════════════
_REQUEST_STATS: dict[str, Any] = {
    "total": 0,
    "protected_401": 0,
    "protected_redirect": 0,
    "session_expired": 0,
    "last_401": None,
    "last_401_path": None,
    "last_401_ua": None,
    "last_401_referer": None,
    "last_expired": None,
    "last_expired_path": None,
    "top_401_paths": {},
    "top_redirect_paths": {},
}
_401_LOCK = asyncio.Lock()


async def _record_401(path: str, ua: str, referer: str) -> None:
    async with _401_LOCK:
        _REQUEST_STATS["protected_401"] += 1
        _REQUEST_STATS["last_401"] = _now_iso()
        _REQUEST_STATS["last_401_path"] = path
        _REQUEST_STATS["last_401_ua"] = ua[:120]
        _REQUEST_STATS["last_401_referer"] = referer[:120]
        top = _REQUEST_STATS["top_401_paths"]
        top[path] = top.get(path, 0) + 1


async def _record_redirect(path: str) -> None:
    async with _401_LOCK:
        _REQUEST_STATS["protected_redirect"] += 1
        top = _REQUEST_STATS["top_redirect_paths"]
        top[path] = top.get(path, 0) + 1


async def _record_expired(path: str) -> None:
    async with _401_LOCK:
        _REQUEST_STATS["session_expired"] += 1
        _REQUEST_STATS["last_expired"] = _now_iso()
        _REQUEST_STATS["last_expired_path"] = path


# ══════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════
def _fmt_uptime(seconds: float) -> str:
    h, r = divmod(int(seconds), 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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

    if not OAUTH_ENABLED:
        print("⚠️  OAuth معطّل (GOOGLE_CLIENT_ID/SECRET غير مضبوطة).")
    else:
        print(f"✅ OAuth مفعّل — Client ID: {GOOGLE_CLIENT_ID[:20]}...")

    if SIM5_ENABLED:
        print(f"✅ 5SIM مفعّل — Base: {SIM5_BASE}")
        try:
            profile = await _sim5_get("/user/profile", auth=True)
            print(f"✅ 5SIM profile: balance={profile.get('balance')} rating={profile.get('rating')}")
        except Sim5Error as e:
            print(f"⚠️  5SIM profile check failed: [{e.status}] {e.message}")
    else:
        print("🔴 5SIM معطّل — SIM5_API_KEY غير مضبوط. لن تُعرض أي بيانات.")

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
    version="1.1.1",
    lifespan=lifespan,
)

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ══════════════════════════════════════════════════════════════
# In-memory store — لا بيانات تجريبية، تُملأ من OAuth ومن 5SIM فقط
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
}


# ══════════════════════════════════════════════════════════════
# Customer helpers
# ══════════════════════════════════════════════════════════════
def _get_customer_from_session(request: Request) -> dict | None:
    cid = request.session.get("customer_id")
    if cid and cid in STORE["customers"]:
        return STORE["customers"][cid]
    return None


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
    """
    يعيد قاموسًا: {"pending": N, "active": N, "completed": N, "expired": N, "cancelled": N}
    متوافق مع statuses 5SIM الحقيقية (PENDING, RECEIVED, CANCELED, TIMEOUT, FINISHED, BANNED).
    """
    buckets = {
        "pending": 0,
        "active": 0,
        "completed": 0,
        "expired": 0,
        "cancelled": 0,
    }
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
    """
    يعيد أكثر 5 خدمات استخدامًا للعميل (من STORE["numbers"]).
    متوافق مع القالب الذي يتوقع: {"name": ..., "icon": ..., "count": ...}
    """
    counts: dict[str, int] = {}
    for n in STORE["numbers"]:
        if n["customer_id"] == customer_id:
            sid = n.get("service_id") or n.get("service") or ""
            counts[sid] = counts.get(sid, 0) + 1

    rows = []
    for sid, cnt in counts.items():
        rows.append({
            "name": sid,
            "icon": _service_icon(sid),
            "count": cnt,
        })
    rows.sort(key=lambda r: r["count"], reverse=True)
    return rows[:5]


async def get_my_numbers(customer_id: int) -> list[dict]:
    return [n for n in STORE["numbers"] if n["customer_id"] == customer_id]


async def get_my_recent_sms(customer_id: int, limit: int = 20) -> list[dict]:
    rows = [s for s in STORE["sms"] if s["customer_id"] == customer_id]
    rows.sort(key=lambda s: s["created_at"], reverse=True)
    return rows[:limit]


# ══════════════════════════════════════════════════════════════
# Pydantic models
# ══════════════════════════════════════════════════════════════
class OrderCreate(BaseModel):
    country: str
    service: str


class DepositCreate(BaseModel):
    amount: float = Field(..., ge=5)
    method: str


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
            sim5_info.update({
                "ok": False,
                "error": e.message,
                "error_code": e.status,
            })
    return {
        "version": app.version,
        "pure_sim5_read": True,
        "has_live_catalog": True,
        "has_real_purchase": True,
        "has_check_order": True,
        "has_cancel_finish": True,
        "has_orders_by_status": True,
        "has_top_services": True,
        "sim5": sim5_info,
        "debug": DEBUG,
        "oauth_enabled": OAUTH_ENABLED,
        "secret_key_from_env": _SECRET_KEY_FROM_ENV,
        "session_idle_timeout_seconds": SESSION_IDLE_TIMEOUT,
        "uptime": _fmt_uptime(time.time() - START_TIME),
    }


@app.get("/api/v1/system/sim5-profile")
async def sim5_profile():
    """الرصيد والتقييم الحقيقيان من 5SIM."""
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
        "default_country": p.get("default_country"),
        "default_operator": p.get("default_operator"),
    }


@app.get("/api/v1/system/whoami")
async def whoami(request: Request):
    idle_seconds = None
    try:
        ls = request.session.get("_last_seen")
        if ls:
            last_seen = datetime.fromisoformat(ls)
            if last_seen.tzinfo is None:
                last_seen = last_seen.replace(tzinfo=timezone.utc)
            idle_seconds = (datetime.now(timezone.utc) - last_seen).total_seconds()
    except Exception:
        pass

    return {
        "has_session": bool(request.session),
        "session_keys": sorted(request.session.keys()),
        "customer_id": request.session.get("customer_id"),
        "email": request.session.get("email"),
        "name": request.session.get("name"),
        "last_seen": request.session.get("_last_seen"),
        "idle_seconds": idle_seconds,
        "idle_timeout_seconds": SESSION_IDLE_TIMEOUT,
        "is_expired": _session_is_expired(request) if request.session else None,
        "cookies_received": sorted(request.cookies.keys()),
        "user_agent": request.headers.get("user-agent", "")[:120],
        "host": request.headers.get("host", ""),
        "client_host": request.client.host if request.client else None,
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
    sim5_detail = "SIM5_API_KEY غير مضبوط"
    if SIM5_ENABLED:
        try:
            p = await _sim5_get("/user/profile", auth=True)
            sim5_status = f"✅ متصل — رصيد: {p.get('balance')} — تقييم: {p.get('rating')}"
            sim5_detail = f"vendor: {p.get('vendor')}"
        except Sim5Error as e:
            sim5_status = f"⚠️ خطأ [{e.status}]"
            sim5_detail = e.message

    html = f"""<!doctype html>
<html lang="ar" dir="rtl">
<head>
  <meta charset="utf-8">
  <title>حالة الخدمة — جمال سولوشنز</title>
  <style>
    body {{ font-family: system-ui, sans-serif; padding: 2rem; background:#f8fafc; color:#1e293b; }}
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
    <h1>🔌 5SIM</h1>
    <div class="meta"><span>{sim5_status}</span></div>
    <div style="font-size:12px; color:#64748b;">{sim5_detail}</div>
    <h2 style="font-size:1rem;margin:1rem 0 .5rem;color:#475569;">آخر عمليات الفحص</h2>
    <table>
      <thead><tr><th align="right">الوقت</th><th align="right">الحالة</th></tr></thead>
      <tbody>{rows}</tbody>
    </table>
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
        KA_STATE["next_ping_in"] = max(0, KA_INTERVAL - (elapsed % KA_INTERVAL))
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
                    elapsed = int(time.time() - START_TIME)
                    KA_STATE["uptime"] = _fmt_uptime(elapsed)
                    KA_STATE["next_ping_in"] = max(0, KA_INTERVAL - (elapsed % KA_INTERVAL))
                    yield {"event": "status", "data": json.dumps(KA_STATE, default=str)}
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
            {"error": "session_expired", "detail": "انتهت الجلسة، يرجى تسجيل الدخول"},
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


@app.get("/api/v1/me/session")
async def me_session(request: Request):
    cid = request.session.get("customer_id")
    expired = _session_is_expired(request) if cid else False
    return {
        "authenticated": bool(cid) and not expired,
        "customer_id": cid,
        "expired": expired,
        "idle_timeout_seconds": SESSION_IDLE_TIMEOUT,
    }


# ══════════════════════════════════════════════════════════════
# API — Prices (5SIM فقط)
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
    return {
        "status": "refreshed",
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


# ══════════════════════════════════════════════════════════════
# Auth routes
# ══════════════════════════════════════════════════════════════
@app.get("/", response_class=HTMLResponse)
async def login_page(request: Request):
    if request.session.get("customer_id"):
        if _session_is_expired(request):
            request.session.clear()
        else:
            return RedirectResponse(url="/dashboard", status_code=303)

    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={
            "oauth_enabled": OAUTH_ENABLED,
            "next": request.query_params.get("next", "/dashboard"),
            "error": request.query_params.get("error"),
        },
    )


@app.get("/auth/google")
async def auth_google(request: Request):
    if not OAUTH_ENABLED:
        return RedirectResponse(url="/?error=oauth_disabled", status_code=303)

    redirect_uri = str(request.url_for("auth_google_callback"))
    next_url = request.query_params.get("next", "")
    if next_url and _is_safe_next(next_url):
        request.session["oauth_next"] = next_url
    else:
        request.session.pop("oauth_next", None)
    return await oauth.google.authorize_redirect(request, redirect_uri)


@app.get("/auth/google/callback", name="auth_google_callback")
async def auth_google_callback(request: Request):
    if not OAUTH_ENABLED:
        raise HTTPException(status_code=503, detail="OAuth غير مُفعّل")

    try:
        token = await oauth.google.authorize_access_token(request)
    except Exception as e:
        print(f"❌ OAuth error: {type(e).__name__}: {e}")
        return RedirectResponse(url="/?error=oauth_failed", status_code=303)

    userinfo = token.get("userinfo") or {}
    email = userinfo.get("email")
    name = userinfo.get("name") or (email.split("@")[0] if email else "مستخدم")
    picture = userinfo.get("picture")

    if not email:
        return RedirectResponse(url="/?error=no_email", status_code=303)

    customer = next(
        (c for c in STORE["customers"].values() if c["email"] == email),
        None,
    )
    if customer is None:
        new_id = max(STORE["customers"].keys(), default=0) + 1
        customer = {
            "id": new_id,
            "name": name,
            "email": email,
            "phone": "",
            "country": "",
            "initials": name[:2],
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
        STORE["customers"][new_id] = customer
    else:
        if picture:
            customer["picture"] = picture

    request.session["customer_id"] = customer["id"]
    request.session["email"] = email
    request.session["name"] = name
    request.session["picture"] = picture
    request.session["logged_in_at"] = _now_iso()
    request.session["_last_seen"] = _now_iso()

    nxt = request.session.pop("oauth_next", None)
    if not nxt or not _is_safe_next(nxt):
        nxt = "/dashboard"

    return RedirectResponse(url=nxt, status_code=303)


@app.api_route("/logout", methods=["GET", "POST"], name="logout")
async def logout(request: Request):
    next_url = request.query_params.get("next", "/")
    if not _is_safe_next(next_url):
        next_url = "/"
    also_google = request.query_params.get("google", "0") == "1"
    request.session.clear()
    if OAUTH_ENABLED and also_google:
        return RedirectResponse(url="https://accounts.google.com/Logout", status_code=303)
    return RedirectResponse(url=next_url, status_code=303)


@app.get("/auth/logout")
async def auth_logout(request: Request):
    next_url = request.query_params.get("next", "/")
    if not _is_safe_next(next_url):
        next_url = "/"
    return RedirectResponse(url=f"/logout?next={quote(next_url)}", status_code=303)


# ══════════════════════════════════════════════════════════════
# Page routes (protected)
# ══════════════════════════════════════════════════════════════
@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    customer = await get_current_customer(request)
    cid = customer["id"]
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "customer": customer,
            "stats": await get_my_stats(cid),
            "my_numbers": await get_my_numbers(cid),
            "recent_sms": await get_my_recent_sms(cid, limit=5),
            "orders_by_status": await get_my_orders_by_status(cid),
            "top_services": await get_my_top_services(cid),
        },
    )


@app.get("/buy", response_class=HTMLResponse)
async def buy_page(
    request: Request,
    country: str | None = None,
    service: str | None = None,
):
    """
    صفحة الشراء — كل البيانات من 5SIM.
    عند الفشل: لا بيانات وهمية، فقط رسالة خطأ.
    """
    customer = await get_current_customer(request)
    cid = customer["id"]

    result = await build_live_catalog()
    stats = await get_my_stats(cid)

    if not result["ok"]:
        return templates.TemplateResponse(
            request=request,
            name="buy.html",
            context={
                "customer": customer,
                "countries": [],
                "preselected_country": country,
                "preselected_service": service,
                "stats": stats,
                "prices_meta": {
                    "ok": False,
                    "source": "5sim",
                    "error": result["error"],
                    "error_code": result["error_code"],
                    "fetched_at": None,
                },
                "popular_services": [],
                "available_numbers": [],
            },
            status_code=200,
        )

    countries = result["countries"]

    popular: list[dict] = []
    for c in countries:
        for s in c["services"]:
            if s["available"]:
                popular.append({
                    "country_slug": c["slug"],
                    "country_code": c["code"],
                    "country_name": c["name"],
                    "country_flag": c["flag"],
                    "service_id": s["id"],
                    "service_name": s["name"],
                    "service_icon": s["icon"],
                    "price": s["cost"],
                    "count": s["count"],
                })
    popular.sort(key=lambda x: x["price"])
    popular = popular[:6]

    available: list[dict] = []
    for c in countries:
        for s in c["services"]:
            if s["available"]:
                available.append({
                    "country_slug": c["slug"],
                    "country_code": c["code"],
                    "country_name": c["name"],
                    "country_flag": c["flag"],
                    "service_id": s["id"],
                    "service_name": s["name"],
                    "service_icon": s["icon"],
                    "count": s["count"],
                    "rate": s["rate"],
                    "price": s["cost"],
                })
    available.sort(key=lambda x: x["price"])
    available = available[:12]

    stats["countries_count"] = len(countries)
    stats["services_count"] = sum(c["services_count"] for c in countries)

    return templates.TemplateResponse(
        request=request,
        name="buy.html",
        context={
            "customer": customer,
            "countries": countries,
            "preselected_country": country,
            "preselected_service": service,
            "stats": stats,
            "prices_meta": {
                "ok": True,
                "source": "5sim",
                "error": None,
                "error_code": None,
                "fetched_at": result["fetched_at"],
            },
            "popular_services": popular,
            "available_numbers": available,
        },
    )


@app.get("/catalog", response_class=HTMLResponse)
async def catalog_page(request: Request):
    customer = await get_current_customer(request)
    result = await build_live_catalog()
    return templates.TemplateResponse(
        request=request,
        name="catalog.html",
        context={
            "customer": customer,
            "countries": result["countries"],
            "catalog_ok": result["ok"],
            "catalog_error": result["error"],
            "stats": await get_my_stats(customer["id"]),
        },
    )


@app.get("/my-numbers", response_class=HTMLResponse)
async def my_numbers_page(request: Request):
    customer = await get_current_customer(request)
    cid = customer["id"]
    return templates.TemplateResponse(
        request=request,
        name="my_numbers.html",
        context={
            "customer": customer,
            "stats": await get_my_stats(cid),
            "my_numbers": await get_my_numbers(cid),
        },
    )


@app.get("/orders", response_class=HTMLResponse)
async def orders_page(request: Request):
    customer = await get_current_customer(request)
    cid = customer["id"]
    orders = [o for o in STORE["orders"] if o["customer_id"] == cid]
    orders.sort(key=lambda o: o["created_at"], reverse=True)
    return templates.TemplateResponse(
        request=request,
        name="orders.html",
        context={
            "customer": customer,
            "orders": orders,
            "stats": await get_my_stats(cid),
        },
    )


@app.get("/orders/{order_id}", response_class=HTMLResponse)
async def order_detail_page(request: Request, order_id: int):
    customer = await get_current_customer(request)
    order = next(
        (o for o in STORE["orders"]
         if o["id"] == order_id and o["customer_id"] == customer["id"]),
        None,
    )
    if not order:
        raise HTTPException(status_code=404, detail="الطلب غير موجود")
    return templates.TemplateResponse(
        request=request,
        name="orders.html",
        context={
            "customer": customer,
            "orders": [order],
            "stats": await get_my_stats(customer["id"]),
        },
    )


@app.get("/inbox", response_class=HTMLResponse)
async def inbox_page(request: Request, number: str | None = None):
    customer = await get_current_customer(request)
    cid = customer["id"]
    sms_list = await get_my_recent_sms(cid, limit=100)
    if number:
        sms_list = [s for s in sms_list if s["phone"] == number]
    return templates.TemplateResponse(
        request=request,
        name="inbox.html",
        context={
            "customer": customer,
            "my_numbers": await get_my_numbers(cid),
            "recent_sms": sms_list,
            "stats": await get_my_stats(cid),
        },
    )


@app.get("/wallet", response_class=HTMLResponse)
async def wallet_page(request: Request):
    customer = await get_current_customer(request)
    cid = customer["id"]
    deposits = [d for d in STORE["deposits"] if d["customer_id"] == cid]
    deposits.sort(key=lambda d: d["created_at"], reverse=True)
    return templates.TemplateResponse(
        request=request,
        name="wallet.html",
        context={
            "customer": customer,
            "payment_methods": [],
            "recent_deposits": deposits[:5],
            "stats": await get_my_stats(cid),
        },
    )


@app.get("/transactions", response_class=HTMLResponse)
async def transactions_page(request: Request):
    customer = await get_current_customer(request)
    cid = customer["id"]
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

    return templates.TemplateResponse(
        request=request,
        name="transactions.html",
        context={
            "customer": customer,
            "transactions": txs,
            "summary": {
                "total_deposits": total_deposits,
                "total_spent": total_spent,
                "count": len(txs),
                "this_month": this_month,
            },
            "stats": await get_my_stats(cid),
        },
    )


@app.get("/support", response_class=HTMLResponse)
async def support_page(request: Request):
    customer = await get_current_customer(request)
    cid = customer["id"]
    tickets = [t for t in STORE["tickets"] if t["customer_id"] == cid]
    tickets.sort(key=lambda t: t["created_at"], reverse=True)
    return templates.TemplateResponse(
        request=request,
        name="support.html",
        context={
            "customer": customer,
            "tickets": tickets,
            "faqs": [],
            "stats": await get_my_stats(cid),
        },
    )


@app.get("/profile", response_class=HTMLResponse)
async def profile_page(request: Request):
    customer = await get_current_customer(request)
    result = await build_live_catalog()
    return templates.TemplateResponse(
        request=request,
        name="profile.html",
        context={
            "customer": customer,
            "stats": await get_my_stats(customer["id"]),
            "countries": result["countries"],
        },
    )


@app.get("/security", response_class=HTMLResponse)
async def security_page(request: Request):
    customer = await get_current_customer(request)
    return templates.TemplateResponse(
        request=request,
        name="security.html",
        context={
            "customer": customer,
            "sessions": STORE["sessions"],
            "stats": await get_my_stats(customer["id"]),
        },
    )


# ══════════════════════════════════════════════════════════════
# API — Orders (شراء حقيقي من 5SIM)
# ══════════════════════════════════════════════════════════════
@app.post("/api/v1/orders")
async def create_order(payload: OrderCreate, request: Request):
    """
    يشتري رقمًا فعليًا من 5SIM.
    - لا رقم وهمي.
    - عند الفشل: خطأ 5SIM الأصلي مترجمًا للعربية.
    """
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
        "price": float(sim5_order.get("price", service["cost"] or 0)),
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
        "price": order["price"],
        "sms_count": 0,
        "expires": order["expires"],
        "created_at": order["created_at"],
    })

    customer["balance"] -= order["price"]
    STORE["transactions"].append({
        "id": len(STORE["transactions"]) + 1,
        "customer_id": cid,
        "type": "purchase",
        "description": f"شراء رقم {service['name']} — {country['name']}",
        "amount": -order["price"],
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
    }


@app.get("/api/v1/orders/sync")
async def sync_orders_from_sim5(request: Request, category: str = "activation"):
    """يجلب سجل الطلبات من 5SIM ويحدّث STORE."""
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
            "status": so.get("status", ""),
            "expires": so.get("expires"),
            "created_at": so.get("created_at"),
            "sms": so.get("sms") or [],
        })
        imported += 1

    return {"ok": True, "imported": imported, "total_from_sim5": len(sim5_orders)}


@app.get("/api/v1/orders/{order_id}/check")
async def check_order(order_id: int, request: Request):
    """GET /v1/user/check/$id — يستورد SMS الجديدة ويحدّث الحالة."""
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

    existing_keys = {
        (s.get("order_id"), s.get("created_at"))
        for s in STORE["sms"]
    }
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


@app.post("/api/v1/orders/{order_id}/ban")
async def ban_order(order_id: int, request: Request):
    customer = await get_current_customer(request)
    order = next(
        (o for o in STORE["orders"]
         if o["id"] == order_id and o["customer_id"] == customer["id"]),
        None,
    )
    if not order or not order.get("sim5_id"):
        raise HTTPException(404, "الطلب غير موجود")
    try:
        data = await _sim5_get(f"/user/ban/{order['sim5_id']}", auth=True)
    except Sim5Error as e:
        raise HTTPException(e.status or 503, e.message)
    order["status"] = data.get("status", "BANNED")
    for n in STORE["numbers"]:
        if n.get("sim5_id") == order["sim5_id"]:
            n["status"] = "banned"
    return {"ok": True, "status": order["status"]}


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
# API — Wallet
# ══════════════════════════════════════════════════════════════
@app.post("/api/v1/wallet/deposit")
async def deposit(payload: DepositCreate, request: Request):
    customer = await get_current_customer(request)
    cid = customer["id"]
    now = datetime.now(timezone.utc)

    deposit_id = len(STORE["deposits"]) + 1
    STORE["deposits"].append({
        "id": deposit_id,
        "customer_id": cid,
        "amount": payload.amount,
        "method": payload.method,
        "status": "completed",
        "created_at": now.isoformat(),
        "date": now.strftime("%Y-%m-%d %H:%M"),
    })

    customer["balance"] += payload.amount
    STORE["transactions"].append({
        "id": len(STORE["transactions"]) + 1,
        "customer_id": cid,
        "type": "deposit",
        "description": f"إيداع رصيد ({payload.method})",
        "amount": payload.amount,
        "balance_after": customer["balance"],
        "created_at": now.isoformat(),
        "date": now.strftime("%Y-%m-%d %H:%M"),
    })

    return {"status": "completed", "checkout_url": "/wallet"}


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
# Middleware — Auth Guard (Sliding Session)
# ══════════════════════════════════════════════════════════════
@app.middleware("http")
async def auth_guard(request: Request, call_next):
    path = request.url.path
    method = request.method

    if path == "/":
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
            await _record_expired(path)
    except Exception:
        cid = None

    if cid:
        try:
            request.session["_last_seen"] = _now_iso()
        except Exception:
            pass
        return await call_next(request)

    ua = request.headers.get("user-agent", "NONE")[:120]
    referer = request.headers.get("referer", "NONE")[:120]

    if is_api:
        await _record_401(path, ua, referer)
        return JSONResponse(
            {
                "detail": "انتهت الجلسة" if expired else "غير مسجّل",
                "error": "session_expired" if expired else "not_authenticated",
            },
            status_code=401,
        )

    await _record_redirect(path)
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
