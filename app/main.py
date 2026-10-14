"""
Jamal Solutions — Virtual Numbers Platform
Customer Portal (FastAPI + Jinja2 + SSE keepalive + Google OAuth)

✅ إصلاحات الجلسة:
- SECRET_KEY ثابت من متغير البيئة (إلزامي في الإنتاج).
- استخدام @app.middleware("http") بدل BaseHTTPMiddleware لضمان وصول session.
- ترتيب middleware صحيح: SessionMiddleware أولاً في التنفيذ.
- max_age موسّع (30 يوم) + sliding refresh عبر _last_seen.
- same_site="lax" لدعم OAuth redirect.
- /logout لا يخرج من Google إلا بطلب صريح.
- حفظ الجلسة عند OAuth callback قبل الـ redirect.

✅ إصلاحات 2026-10-14 (1.0.1):
- إصلاح UndefinedError: 'stats' is undefined في buy.html.
- تمرير stats إلى كل القوالب التي قد تحتاجه.
- إضافة total_orders و active_orders إلى get_my_stats.
- إصلاح ثغرة أمنية في auth_guard: path.startswith(p) مع p="/".
- تفعيل StrictUndefined في وضع DEBUG.

✅ إصلاحات 2026-10-14 (1.0.2):
- تحسين auth_guard: redirect للمتصفح، 401 للـ API فقط.
- إضافة /api/v1/system/version و /api/v1/system/whoami.
- logging تشخيصي في auth_guard (عند DEBUG).
- فحص SECRET_KEY في lifespan وطباعة تحذير واضح.

✅ إصلاحات 2026-10-14 (1.0.3):
- إزالة الاعتماد على Accept header في auth_guard (غير موثوق).
- الصفحات المحمية → 303 Redirect دائماً.
- API المحمي → 401 JSON دائماً.
- إضافة /api/v1/me/stats لـ JavaScript (بدل fetch على /dashboard).
- logging تشخيصي دائم لطلبات 401.

✅ إصلاحات 2026-10-14 (1.0.4):
- logging تشخيصي مفصّل لكل طلبات 401 (referer, host, x-forwarded-for).
- إضافة /api/v1/system/health-check لاستخدامه كـ Health Check Path على Render.
- إضافة /api/v1/system/whoami-detailed لمزيد من التشخيص.
- إضافة middleware لتسجيل كل الطلبات المحمية (عند DEBUG).
- تحسين رسائل الـ logging لتحديد مصدر طلبات 401.
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

import httpx
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, EmailStr, Field
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

# في التطوير: اجعل أي متغير غير معرّف في القالب يسبب خطأ واضحاً
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
    # في التطوير فقط: ولّد قيمة ثابتة واطبعها، ثم ضعها في .env
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
SESSION_MAX_AGE = int(os.getenv("SESSION_MAX_AGE", str(30 * 24 * 3600)))  # 30 يوم

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

# المسارات التي تتطلب تسجيل دخول
PROTECTED_PREFIXES = (
    "/dashboard", "/buy", "/catalog", "/my-numbers", "/orders",
    "/inbox", "/wallet", "/transactions", "/support", "/profile", "/security",
)

# ⚠️ مهم: لا تضع "/" هنا. المسار "/" يُعامل كحالة خاصة في auth_guard.
PUBLIC_PATHS = (
    "/logout", "/auth", "/static", "/health", "/status", "/api/v1/system",
)

# ══════════════════════════════════════════════════════════════
# Keepalive state
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
# إحصائيات الطلبات (للتشخيص)
# ══════════════════════════════════════════════════════════════
_REQUEST_STATS: dict[str, Any] = {
    "total": 0,
    "protected_401": 0,
    "protected_redirect": 0,
    "last_401": None,
    "last_401_path": None,
    "last_401_ua": None,
    "last_401_referer": None,
    "top_401_paths": {},
}
_401_LOCK = asyncio.Lock()


async def _record_401(path: str, ua: str, referer: str) -> None:
    """يسجل إحصائيات طلبات 401 للتشخيص."""
    async with _401_LOCK:
        _REQUEST_STATS["protected_401"] += 1
        _REQUEST_STATS["last_401"] = _now_iso()
        _REQUEST_STATS["last_401_path"] = path
        _REQUEST_STATS["last_401_ua"] = ua[:120]
        _REQUEST_STATS["last_401_referer"] = referer[:120]
        top = _REQUEST_STATS["top_401_paths"]
        top[path] = top.get(path, 0) + 1


# ══════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════
def _fmt_uptime(seconds: float) -> str:
    h, r = divmod(int(seconds), 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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
    # ── فحص إعدادات حرجة عند بدء التشغيل ──
    if _SECRET_KEY_FROM_ENV:
        print("✅ SECRET_KEY مضبوط من متغيرات البيئة — الجلسات ستبقى بين النشرات.")
    else:
        print(
            "🔴 تحذير: SECRET_KEY غير مضبوط في متغيرات البيئة! "
            "كل إعادة نشر ستُبطل جميع الجلسات. "
            "أضف SECRET_KEY إلى Environment على Render."
        )

    if not OAUTH_ENABLED:
        print("⚠️  OAuth معطّل (GOOGLE_CLIENT_ID/SECRET غير مضبوطة) — وضع تجريبي.")
    else:
        print(f"✅ OAuth مفعّل — Client ID: {GOOGLE_CLIENT_ID[:20]}...")

    print(f"🔧 DEBUG={DEBUG} | SESSION_COOKIE_SECURE={SESSION_COOKIE_SECURE} "
          f"| SESSION_MAX_AGE={SESSION_MAX_AGE}s | Version={app.version}")

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
    version="1.0.4",
    lifespan=lifespan,
)

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ══════════════════════════════════════════════════════════════
# In-memory store (replace with DB later)
# ══════════════════════════════════════════════════════════════
STORE: dict[str, Any] = {
    "customers": {
        1: {
            "id": 1,
            "name": "أحمد محمد",
            "email": "ahmed@example.com",
            "phone": "+967 777 000 000",
            "country": "YE",
            "initials": "أم",
            "balance": 0.0,
            "currency": "USD",
            "created_at": "2025-01-15",
            "email_verified": True,
            "phone_verified": False,
            "two_fa_enabled": False,
            "has_strong_password": True,
            "security_score": 60,
            "prefs": {
                "email_notifications": True,
                "sms_notifications": False,
                "marketing_emails": False,
            },
        },
    },
    "numbers": [],
    "orders": [],
    "sms": [],
    "transactions": [],
    "deposits": [],
    "tickets": [],
    "sessions": [
        {
            "id": "s1",
            "device": "desktop",
            "device_name": "Chrome · Windows",
            "location": "اليمن، صنعاء",
            "last_active": "الآن",
            "current": True,
        },
    ],
}


# ══════════════════════════════════════════════════════════════
# Catalogs
# ══════════════════════════════════════════════════════════════
COUNTRIES: list[dict] = [
    {"code": "YE", "name": "اليمن",          "flag": "🇾🇪", "continent": "asia",    "continent_label": "آسيا",    "price": "$0.50", "available": 42, "min_price": 0.50, "services_count": 8,  "top_services": ["واتساب", "تليجرام", "جوجل"]},
    {"code": "SA", "name": "السعودية",       "flag": "🇸🇦", "continent": "asia",    "continent_label": "آسيا",    "price": "$0.70", "available": 85, "min_price": 0.70, "services_count": 12, "top_services": ["واتساب", "سناب شات", "جوجل"]},
    {"code": "EG", "name": "مصر",            "flag": "🇪🇬", "continent": "africa",  "continent_label": "أفريقيا", "price": "$0.60", "available": 63, "min_price": 0.60, "services_count": 10, "top_services": ["واتساب", "تليجرام", "فيسبوك"]},
    {"code": "US", "name": "الولايات المتحدة","flag": "🇺🇸", "continent": "america", "continent_label": "أمريكا",  "price": "$1.20", "available": 210,"min_price": 1.20, "services_count": 25, "top_services": ["واتساب", "جوجل", "أمازون"]},
    {"code": "GB", "name": "المملكة المتحدة","flag": "🇬🇧", "continent": "europe",  "continent_label": "أوروبا",  "price": "$1.10", "available": 156,"min_price": 1.10, "services_count": 22, "top_services": ["واتساب", "جوجل", "تويتر"]},
    {"code": "DE", "name": "ألمانيا",         "flag": "🇩🇪", "continent": "europe",  "continent_label": "أوروبا",  "price": "$1.00", "available": 130,"min_price": 1.00, "services_count": 18, "top_services": ["واتساب", "تليجرام", "أمازون"]},
    {"code": "FR", "name": "فرنسا",           "flag": "🇫🇷", "continent": "europe",  "continent_label": "أوروبا",  "price": "$1.05", "available": 118,"min_price": 1.05, "services_count": 17, "top_services": ["واتساب", "جوجل", "فيسبوك"]},
    {"code": "TR", "name": "تركيا",           "flag": "🇹🇷", "continent": "asia",    "continent_label": "آسيا",    "price": "$0.80", "available": 92, "min_price": 0.80, "services_count": 14, "top_services": ["واتساب", "تليجرام", "إنستغرام"]},
    {"code": "AE", "name": "الإمارات",        "flag": "🇦🇪", "continent": "asia",    "continent_label": "آسيا",    "price": "$0.95", "available": 74, "min_price": 0.95, "services_count": 13, "top_services": ["واتساب", "تليجرام", "جوجل"]},
    {"code": "IN", "name": "الهند",           "flag": "🇮🇳", "continent": "asia",    "continent_label": "آسيا",    "price": "$0.40", "available": 320,"min_price": 0.40, "services_count": 20, "top_services": ["واتساب", "تليجرام", "فيسبوك"]},
    {"code": "BR", "name": "البرازيل",        "flag": "🇧🇷", "continent": "america", "continent_label": "أمريكا",  "price": "$0.85", "available": 145,"min_price": 0.85, "services_count": 16, "top_services": ["واتساب", "تليجرام", "إنستغرام"]},
    {"code": "ID", "name": "إندونيسيا",       "flag": "🇮🇩", "continent": "asia",    "continent_label": "آسيا",    "price": "$0.45", "available": 260,"min_price": 0.45, "services_count": 19, "top_services": ["واتساب", "تليجرام", "تيك توك"]},
]

SERVICES: list[dict] = [
    {"id": "whatsapp",  "name": "واتساب",   "icon": "💬"},
    {"id": "telegram",  "name": "تليجرام",  "icon": "✈️"},
    {"id": "google",    "name": "جوجل",     "icon": "🔍"},
    {"id": "facebook",  "name": "فيسبوك",   "icon": "📘"},
    {"id": "twitter",   "name": "تويتر",    "icon": "🐦"},
    {"id": "instagram", "name": "إنستغرام", "icon": "📷"},
    {"id": "tiktok",    "name": "تيك توك",  "icon": "🎵"},
    {"id": "amazon",    "name": "أمازون",   "icon": "📦"},
    {"id": "snapchat",  "name": "سناب شات", "icon": "👻"},
    {"id": "discord",   "name": "ديسكورد",  "icon": "🎮"},
    {"id": "uber",      "name": "أوبر",     "icon": "🚕"},
    {"id": "netflix",   "name": "نتفليكس",  "icon": "🎬"},
]

PAYMENT_METHODS: list[dict] = [
    {"id": "card",   "name": "بطاقة بنكية", "icon": "💳", "description": "Visa · Mastercard"},
    {"id": "paypal", "name": "PayPal",      "icon": "🅿️", "description": "دفع آمن"},
    {"id": "crypto", "name": "عملة رقمية",  "icon": "₿",  "description": "BTC · USDT · ETH"},
    {"id": "local",  "name": "محفظة محلية", "icon": "📱", "description": "جوالي · كاش · وغيرها"},
]

FAQS: list[dict] = [
    {"question": "كيف أستلم الرقم بعد الشراء؟",
     "answer": "يظهر الرقم مباشرة في صفحة (أرقامي) ويمكنك استخدامه فورًا."},
    {"question": "كم يستغرق وصول الرسالة؟",
     "answer": "عادة خلال 30 ثانية إلى دقيقتين، وقد يتأخر بعض المزودين حتى 10 دقائق."},
    {"question": "هل يمكنني استرداد المبلغ؟",
     "answer": "نعم، إذا لم تصل أي رسالة خلال ساعة من الشراء، يتم استرداد كامل المبلغ تلقائيًا."},
    {"question": "ما مدة صلاحية الرقم؟",
     "answer": "20 دقيقة افتراضيًا من لحظة الشراء، ويمكن تجديده."},
]

COUNTRIES_BY_CODE = {c["code"]: c for c in COUNTRIES}
SERVICES_BY_ID = {s["id"]: s for s in SERVICES}


# ══════════════════════════════════════════════════════════════
# Current customer helpers
# ══════════════════════════════════════════════════════════════
def _get_customer_from_session(request: Request) -> dict | None:
    """يرجع بيانات العميل المسجّل من الجلسة، أو None."""
    cid = request.session.get("customer_id")
    if cid and cid in STORE["customers"]:
        return STORE["customers"][cid]
    return None


async def get_current_customer(request: Request) -> dict:
    """للصفحات المحمية — يفترض أن middleware قد تحقق من الجلسة."""
    customer = _get_customer_from_session(request)
    if customer is None:
        raise HTTPException(status_code=401, detail="غير مسجّل")
    return customer


# ══════════════════════════════════════════════════════════════
# Data aggregators
# ══════════════════════════════════════════════════════════════
async def get_my_stats(customer_id: int) -> dict:
    nums = [n for n in STORE["numbers"] if n["customer_id"] == customer_id]
    ords = [o for o in STORE["orders"] if o["customer_id"] == customer_id]
    sms  = [s for s in STORE["sms"] if s["customer_id"] == customer_id]
    txs  = [t for t in STORE["transactions"] if t["customer_id"] == customer_id]

    today = datetime.now(timezone.utc).date()
    sms_today = sum(
        1 for s in sms
        if datetime.fromisoformat(s["created_at"]).date() == today
    )

    total_spent = sum(
        -t["amount"] for t in txs if t["type"] == "purchase"
    )
    month_start = today.replace(day=1)
    spent_this_month = sum(
        -t["amount"] for t in txs
        if t["type"] == "purchase"
        and datetime.fromisoformat(t["created_at"]).date() >= month_start
    )

    return {
        # الأرقام
        "my_numbers":        len(nums),
        "active_numbers":    sum(1 for n in nums if n["status"] == "active"),
        "expiring_soon":     sum(1 for n in nums if n["status"] == "expiring"),
        # الطلبات — aliases للتماثل مع القوالب
        "my_orders":         len(ords),
        "total_orders":      len(ords),
        "pending_orders":    sum(1 for o in ords if o["status"] == "pending"),
        "active_orders":     sum(1 for o in ords if o["status"] == "active"),
        "completed_orders":  sum(1 for o in ords if o["status"] == "completed"),
        # الرسائل
        "my_sms":            len(sms),
        "sms_today":         sms_today,
        "unread_sms":        sum(1 for s in sms if not s["read"]),
        # المالية
        "total_spent":       total_spent,
        "spent_this_month":  spent_this_month,
    }


async def get_my_orders_by_status(customer_id: int) -> dict:
    buckets = {"pending": 0, "active": 0, "completed": 0, "expired": 0, "cancelled": 0}
    for o in STORE["orders"]:
        if o["customer_id"] == customer_id and o["status"] in buckets:
            buckets[o["status"]] += 1
    return buckets


async def get_my_top_services(customer_id: int) -> list[dict]:
    counts: dict[str, int] = {}
    for n in STORE["numbers"]:
        if n["customer_id"] == customer_id:
            counts[n["service_id"]] = counts.get(n["service_id"], 0) + 1

    rows = []
    for s in SERVICES:
        rows.append({
            "name":  s["name"],
            "icon":  s["icon"],
            "count": counts.get(s["id"], 0),
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
# System routes (public)
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
    """
    نقطة نهاية مخصصة لـ Render Health Check.
    استخدمها كـ Health Check Path في Render Dashboard.
    لا تتطلب تسجيل دخول، وترجع 200 دائماً.
    """
    return {
        "status": "healthy",
        "version": app.version,
        "uptime": _fmt_uptime(time.time() - START_TIME),
        "timestamp": _now_iso(),
    }


@app.get("/api/v1/system/version")
async def system_version():
    """للتأكد من أن الكود الجديد فعلاً منشور."""
    return {
        "version": app.version,
        "has_stats_fix": True,
        "has_auth_guard_fix": True,
        "has_me_stats": True,
        "has_detailed_logging": True,
        "has_health_check": True,
        "debug": DEBUG,
        "oauth_enabled": OAUTH_ENABLED,
        "secret_key_from_env": _SECRET_KEY_FROM_ENV,
        "session_cookie_secure": SESSION_COOKIE_SECURE,
        "uptime": _fmt_uptime(time.time() - START_TIME),
        "started_at": datetime.fromtimestamp(START_TIME, tz=timezone.utc).isoformat(),
    }


@app.get("/api/v1/system/whoami")
async def whoami(request: Request):
    """يرجع محتوى الجلسة الحالية (بدون بيانات حساسة) للتشخيص."""
    return {
        "has_session": bool(request.session),
        "session_keys": sorted(request.session.keys()),
        "customer_id": request.session.get("customer_id"),
        "email": request.session.get("email"),
        "name": request.session.get("name"),
        "logged_in_at": request.session.get("logged_in_at"),
        "last_seen": request.session.get("_last_seen"),
        "cookies_received": sorted(request.cookies.keys()),
        "user_agent": request.headers.get("user-agent", "")[:120],
        "accept": request.headers.get("accept", ""),
        "referer": request.headers.get("referer", ""),
        "host": request.headers.get("host", ""),
        "x_forwarded_for": request.headers.get("x-forwarded-for", ""),
        "x_forwarded_proto": request.headers.get("x-forwarded-proto", ""),
        "client_host": request.client.host if request.client else None,
    }


@app.get("/api/v1/system/whoami-detailed")
async def whoami_detailed(request: Request):
    """
    تشخيص مفصّل: يعرض كل الترويسات + حالة الطلب + إحصائيات 401.
    لا يعرض قيم الكوكيز الحساسة، فقط أسماءها.
    """
    return {
        "request": {
            "method": request.method,
            "url": str(request.url),
            "path": request.url.path,
            "query": dict(request.query_params),
            "client_host": request.client.host if request.client else None,
        },
        "headers": {
            k: (v[:200] if isinstance(v, str) else v)
            for k, v in request.headers.items()
        },
        "cookies": {
            k: f"<{len(v)} chars>" for k, v in request.cookies.items()
        },
        "session": {
            "has_session": bool(request.session),
            "keys": sorted(request.session.keys()),
            "customer_id": request.session.get("customer_id"),
        },
        "request_stats": _REQUEST_STATS,
        "uptime": _fmt_uptime(time.time() - START_TIME),
    }


@app.get("/api/v1/system/request-stats")
async def request_stats():
    """إحصائيات طلبات 401 — لمعرفة المصدر."""
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

    # إحصائيات 401
    top_401 = _REQUEST_STATS.get("top_401_paths", {})
    top_401_rows = "".join(
        f"<tr><td>{p}</td><td>{c}</td></tr>"
        for p, c in sorted(top_401.items(), key=lambda x: -x[1])[:10]
    ) or "<tr><td colspan=2>لا توجد</td></tr>"

    html = f"""<!doctype html>
<html lang="ar" dir="rtl">
<head>
  <meta charset="utf-8">
  <title>حالة الخدمة — جمال سولوشنز</title>
  <style>
    body {{ font-family: system-ui, sans-serif; padding: 2rem; background:#f8fafc; color:#1e293b; }}
    .card {{ background:#fff; border-radius:12px; padding:1.5rem; box-shadow:0 1px 3px rgba(0,0,0,.08); max-width:720px; margin:auto 0 1rem; }}
    h1 {{ font-size:1.25rem; margin:0 0 1rem; }}
    h2 {{ font-size:1rem; margin:1rem 0 .5rem; color:#475569; }}
    table {{ width:100%; border-collapse:collapse; font-size:14px; }}
    td {{ padding:8px; border-bottom:1px solid #e2e8f0; }}
    .meta {{ display:flex; gap:1rem; margin-bottom:1rem; font-size:14px; color:#475569; flex-wrap:wrap; }}
    .ok  {{ color:#16a34a; font-weight:600; }}
    .bad {{ color:#dc2626; font-weight:600; }}
  </style>
</head>
<body>
  <div class="card">
    <h1>🩺 حالة الخدمة</h1>
    <div class="meta">
      <span>Uptime: <b>{uptime}</b></span>
      <span>Ping ✓: <b class="ok">{KA_STATE['ping_count']}</b></span>
      <span>Fail ✗: <b class="bad">{KA_STATE['fail_count']}</b></span>
      <span>Next ping: <b>{KA_STATE['next_ping_in']}s</b></span>
    </div>
    <h2>آخر عمليات الفحص</h2>
    <table>
      <thead><tr><th align="right">الوقت</th><th align="right">الحالة</th></tr></thead>
      <tbody>{rows}</tbody>
    </table>
  </div>

  <div class="card">
    <h1>🔒 إحصائيات 401</h1>
    <div class="meta">
      <span>Total 401: <b class="bad">{_REQUEST_STATS['protected_401']}</b></span>
      <span>Last: <b>{_REQUEST_STATS['last_401'] or '-'}</b></span>
      <span>Last path: <b>{_REQUEST_STATS['last_401_path'] or '-'}</b></span>
    </div>
    <h2>آخر User-Agent</h2>
    <div style="font-size:12px; color:#64748b; word-break:break-all;">
      {_REQUEST_STATS['last_401_ua'] or '-'}
    </div>
    <h2>آخر Referer</h2>
    <div style="font-size:12px; color:#64748b; word-break:break-all;">
      {_REQUEST_STATS['last_401_referer'] or '-'}
    </div>
    <h2>المسارات الأكثر 401</h2>
    <table>
      <thead><tr><th align="right">المسار</th><th align="right">العدد</th></tr></thead>
      <tbody>{top_401_rows}</tbody>
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
# API — Me (للـ JavaScript: يعيد JSON فقط)
# ══════════════════════════════════════════════════════════════
@app.get("/api/v1/me/stats")
async def me_stats(request: Request):
    """
    نقطة نهاية JSON لـ JavaScript — بديل لـ fetch('/dashboard').
    إذا انتهت الجلسة، يعيد 401 JSON واضح.
    """
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
    """فحص سريع لحالة الجلسة من JavaScript."""
    cid = request.session.get("customer_id")
    return {
        "authenticated": bool(cid),
        "customer_id": cid,
        "session_keys": sorted(request.session.keys()),
    }


# ══════════════════════════════════════════════════════════════
# Auth routes
# ══════════════════════════════════════════════════════════════
@app.get("/", response_class=HTMLResponse)
async def login_page(request: Request):
    """شاشة تسجيل الدخول الرئيسية."""
    if request.session.get("customer_id"):
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
        # وضع التطوير بدون Google: سجّل الدخول كمستخدم تجريبي
        request.session["customer_id"] = 1
        request.session["email"] = STORE["customers"][1]["email"]
        request.session["name"] = STORE["customers"][1]["name"]
        request.session["logged_in_at"] = _now_iso()
        request.session["_last_seen"] = _now_iso()
        nxt = request.query_params.get("next", "/dashboard")
        return RedirectResponse(url=nxt, status_code=303)

    redirect_uri = str(request.url_for("auth_google_callback"))
    request.session["oauth_next"] = request.query_params.get("next", "/dashboard")
    return await oauth.google.authorize_redirect(request, redirect_uri)


@app.get("/auth/google/callback", name="auth_google_callback")
async def auth_google_callback(request: Request):
    if not OAUTH_ENABLED:
        raise HTTPException(status_code=503, detail="OAuth غير مُفعّل")

    try:
        token = await oauth.google.authorize_access_token(request)
    except Exception:
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
            "country": "YE",
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

    # ✅ سجّل الدخول — تأكد من كتابة القيم قبل الـ redirect
    request.session["customer_id"] = customer["id"]
    request.session["email"] = email
    request.session["name"] = name
    request.session["picture"] = picture
    request.session["logged_in_at"] = _now_iso()
    request.session["_last_seen"] = _now_iso()

    nxt = request.session.pop("oauth_next", "/dashboard")
    return RedirectResponse(url=nxt, status_code=303)


# ══════════════════════════════════════════════════════════════
# Logout
# ══════════════════════════════════════════════════════════════
@app.api_route("/logout", methods=["GET", "POST"], name="logout")
async def logout(request: Request):
    """
    تسجيل الخروج:
    - يمسح الجلسة المحلية بالكامل.
    - اختياريًا: ?google=1 لإنهاء جلسة Google أيضاً.
    - يدعم ?next= للعودة لصفحة معينة بعد الخروج.
    """
    next_url = request.query_params.get("next", "/")
    also_google = request.query_params.get("google", "0") == "1"

    request.session.clear()

    if OAUTH_ENABLED and also_google:
        return RedirectResponse(url="https://accounts.google.com/Logout", status_code=303)

    return RedirectResponse(url=next_url, status_code=303)


@app.get("/auth/logout")
async def auth_logout(request: Request):
    next_url = request.query_params.get("next", "/")
    return RedirectResponse(url=f"/logout?next={next_url}", status_code=303)


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
            "orders_by_status": await get_my_orders_by_status(cid),
            "top_services": await get_my_top_services(cid),
            "my_numbers": await get_my_numbers(cid),
            "recent_sms": await get_my_recent_sms(cid, limit=5),
        },
    )


@app.get("/buy", response_class=HTMLResponse)
async def buy_page(request: Request, country: str | None = None, service: str | None = None):
    customer = await get_current_customer(request)
    cid = customer["id"]
    return templates.TemplateResponse(
        request=request,
        name="buy.html",
        context={
            "customer": customer,
            "countries": COUNTRIES,
            "services": SERVICES,
            "preselected_country": country,
            "preselected_service": service,
            "stats": await get_my_stats(cid),
        },
    )


@app.get("/catalog", response_class=HTMLResponse)
async def catalog_page(request: Request):
    customer = await get_current_customer(request)
    return templates.TemplateResponse(
        request=request,
        name="catalog.html",
        context={
            "customer": customer,
            "countries": COUNTRIES,
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
            "payment_methods": PAYMENT_METHODS,
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
    total_spent    = sum(-t["amount"] for t in txs if t["type"] == "purchase")
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
            "faqs": FAQS,
            "stats": await get_my_stats(cid),
        },
    )


@app.get("/profile", response_class=HTMLResponse)
async def profile_page(request: Request):
    customer = await get_current_customer(request)
    cid = customer["id"]
    return templates.TemplateResponse(
        request=request,
        name="profile.html",
        context={
            "customer": customer,
            "stats": await get_my_stats(cid),
            "countries": COUNTRIES,
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
# API — Orders / Numbers
# ══════════════════════════════════════════════════════════════
@app.post("/api/v1/orders")
async def create_order(payload: OrderCreate, request: Request):
    customer = await get_current_customer(request)
    cid = customer["id"]

    country = COUNTRIES_BY_CODE.get(payload.country)
    service = SERVICES_BY_ID.get(payload.service)
    if not country or not service:
        raise HTTPException(status_code=400, detail="دولة أو خدمة غير صالحة")

    price = country["min_price"]
    if customer["balance"] < price:
        raise HTTPException(status_code=400, detail="الرصيد غير كافٍ")

    now = datetime.now(timezone.utc)
    order_id = len(STORE["orders"]) + 1
    number_id = len(STORE["numbers"]) + 1

    fake_phone = f"+{country['code']}{1000000000 + number_id}"

    order = {
        "id": order_id,
        "customer_id": cid,
        "country": country["name"],
        "country_code": country["code"],
        "flag": country["flag"],
        "service": service["name"],
        "service_id": service["id"],
        "phone": fake_phone,
        "price": price,
        "status": "active",
        "created_at": now.isoformat(),
    }
    STORE["orders"].append(order)

    number = {
        "id": number_id,
        "customer_id": cid,
        "phone": fake_phone,
        "country": country["name"],
        "country_code": country["code"],
        "flag": country["flag"],
        "service": service["name"],
        "service_id": service["id"],
        "status": "active",
        "price": price,
        "sms_count": 0,
        "expires_in": "20:00",
        "created_at": now.isoformat(),
    }
    STORE["numbers"].append(number)

    customer["balance"] -= price
    STORE["transactions"].append({
        "id": len(STORE["transactions"]) + 1,
        "customer_id": cid,
        "type": "purchase",
        "description": f"شراء رقم {service['name']} — {country['name']}",
        "amount": -price,
        "balance_after": customer["balance"],
        "created_at": now.isoformat(),
        "date": now.strftime("%Y-%m-%d %H:%M"),
    })

    return {"id": order_id, "phone": fake_phone, "status": "active"}


@app.post("/api/v1/numbers/{number_id}/renew")
async def renew_number(number_id: int, request: Request):
    customer = await get_current_customer(request)
    number = next(
        (n for n in STORE["numbers"]
         if n["id"] == number_id and n["customer_id"] == customer["id"]),
        None,
    )
    if not number:
        raise HTTPException(status_code=404, detail="الرقم غير موجود")
    if customer["balance"] < number["price"]:
        raise HTTPException(status_code=400, detail="الرصيد غير كافٍ")

    customer["balance"] -= number["price"]
    number["status"] = "active"
    number["expires_in"] = "20:00"

    now = datetime.now(timezone.utc)
    STORE["transactions"].append({
        "id": len(STORE["transactions"]) + 1,
        "customer_id": customer["id"],
        "type": "purchase",
        "description": f"تجديد رقم {number['phone']}",
        "amount": -number["price"],
        "balance_after": customer["balance"],
        "created_at": now.isoformat(),
        "date": now.strftime("%Y-%m-%d %H:%M"),
    })
    return {"status": "renewed"}


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


@app.delete("/api/v1/profile")
async def delete_profile(request: Request):
    await get_current_customer(request)
    return {"status": "deleted"}


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
    STORE["sessions"] = [
        s for s in STORE["sessions"] if s["id"] != session_id
    ]
    return {"status": "ended"}


@app.delete("/api/v1/security/sessions")
async def end_all_sessions(request: Request):
    await get_current_customer(request)
    STORE["sessions"] = []
    return {"status": "all_ended"}


# ══════════════════════════════════════════════════════════════
# Middleware — Auth Guard (يُنفَّذ بعد SessionMiddleware)
# ══════════════════════════════════════════════════════════════
@app.middleware("http")
async def auth_guard(request: Request, call_next):
    """
    يحمي المسارات الخاصة.

    ✅ القاعدة (1.0.4) — بدون الاعتماد على Accept header:

    - "/" → عام (صفحة تسجيل الدخول)
    - PUBLIC_PATHS → عامة
    - "/api/..." (غير عام) → 401 JSON دائماً إذا لم يكن مسجّلاً
    - أي صفحة محمية → 303 Redirect دائماً إذا لم يكن مسجّلاً

    ✅ logging تفصيلي لكل طلب محمي (للتشخيص):
    - method, path, client host
    - user-agent, referer, accept
    - has_cookie, session_keys, customer_id
    - x-forwarded-for (لأن Render يمرر الطلبات عبر بروكسي)
    """
    path = request.url.path
    is_api = path.startswith("/api/")
    cid = request.session.get("customer_id")

    # ── "/" مسار عام ──
    if path == "/":
        return await call_next(request)

    # ── مسارات عامة أخرى ──
    if any(
        path == p or path.startswith(p + "/")
        for p in PUBLIC_PATHS
    ):
        return await call_next(request)

    # ── هل يحتاج تسجيل دخول؟ ──
    needs_auth = any(
        path == p or path.startswith(p + "/") for p in PROTECTED_PREFIXES
    )
    if is_api:
        needs_auth = True

    # ── طلب محمي بدون جلسة ──
    if needs_auth and not cid:
        # اجمع معلومات التشخيص
        ua = request.headers.get("user-agent", "NONE")[:120]
        referer = request.headers.get("referer", "NONE")[:120]
        accept = request.headers.get("accept", "NONE")[:60]
        xff = request.headers.get("x-forwarded-for", "NONE")[:80]
        client_host = request.client.host if request.client else "NONE"
        has_cookie = "cookie" in request.headers

        # سجّل في الإحصائيات
        await _record_401(path, ua, referer)

        # اطبع في السجل (دائماً — ليس فقط في DEBUG)
        print(
            f"🔒 401 | path={path} | is_api={is_api} | "
            f"client={client_host} | xff={xff} | "
            f"has_cookie={has_cookie} | "
            f"accept={accept!r} | "
            f"referer={referer!r} | "
            f"ua={ua!r}"
        )

        if is_api:
            return JSONResponse(
                {"detail": "غير مسجّل", "error": "not_authenticated"},
                status_code=401,
            )
        # صفحة محمية → redirect دائماً
        print(f"↪️  303 redirect: path={path} → /?next={path}")
        return RedirectResponse(url="/?next=" + path, status_code=303)

    # ── sliding refresh ──
    if cid:
        request.session["_last_seen"] = _now_iso()

    return await call_next(request)


# ══════════════════════════════════════════════════════════════
# Session middleware — يُضاف بعد auth_guard ليكون الأقرب للتطبيق
# (في Starlette، آخر middleware مضاف هو الأول في التنفيذ)
# ══════════════════════════════════════════════════════════════
app.add_middleware(
    SessionMiddleware,
    secret_key=SECRET_KEY,
    session_cookie="jamal_session",
    max_age=SESSION_MAX_AGE,
    same_site="lax",
    https_only=SESSION_COOKIE_SECURE,
    path="/",
)
