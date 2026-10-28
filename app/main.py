"""نقطة الدخول الرئيسية."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.config import (
    DEBUG,
    STATIC_DIR,
    SECRET_KEY,
    SESSION_MAX_AGE,
    SESSION_COOKIE_SECURE,
    SESSION_IDLE_TIMEOUT,
    ADMIN_SECRET,
    GUEST_MODE,
    GUEST_REQUIRES_VOUCHER,
    HIDE_PRICES_FROM_CUSTOMERS,
    HIDE_BALANCE_FROM_CUSTOMERS,
    BLOCKED_COUNTRY_SLUGS,
    BLOCKED_COUNTRY_CODES,
    SIM5_ENABLED,
    SIM5_BASE,
    FIVESIM_MARKUP_PERCENT,
)
from app.core.storage import (
    STORE,
    store_file_info,
    save_now,
    _HAS_STORE_STATUS,
)
from app.core.keepalive import keepalive_loop
from app.services.sim5 import sim5_get, Sim5Error
from app.middleware.auth_guard import auth_guard

# Routers
from app.routers import (
    system,
    auth,
    pages,
    prices,
    orders,
    wallet,
    vouchers as vouchers_router,
    sms,
    support,
    profile,
    security,
    admin,
    me,
)


# ══════════════════════════════════════════════════════════════
# Lifespan
# ══════════════════════════════════════════════════════════════
@asynccontextmanager
async def lifespan(app: FastAPI):
    # نكتشف إن كان SECRET_KEY جاء من البيئة أو من الملف
    import os
    secret_from_env = bool(os.getenv("SECRET_KEY", "").strip())

    if secret_from_env:
        print("✅ SECRET_KEY مضبوط من متغيرات البيئة.")
    else:
        print("🔴 تحذير: SECRET_KEY غير مضبوط في متغيرات البيئة!")

    if GUEST_MODE:
        print("✅ GUEST_MODE مفعّل — لا تسجيل دخول، الإقلاع مباشر على /buy")
        if GUEST_REQUIRES_VOUCHER:
            print("✅ GUEST_REQUIRES_VOUCHER=true — كود شراء إلزامي لكل عملية")
        else:
            print("⚠️  GUEST_REQUIRES_VOUCHER=false — الشراء مجاني")
    else:
        print("✅ تسجيل الدخول بالكود مفعّل.")

    print("✅ الشاشة الوحيدة: /buy")
    print(f"✅ إخفاء الأسعار عن الزبائن: {HIDE_PRICES_FROM_CUSTOMERS}")
    print(f"✅ إخفاء الرصيد عن الزبائن: {HIDE_BALANCE_FROM_CUSTOMERS}")
    print(f"✅ الدول المحظورة (slug): {sorted(BLOCKED_COUNTRY_SLUGS) or '—'}")
    print(f"✅ الدول المحظورة (ISO):  {sorted(BLOCKED_COUNTRY_CODES) or '—'}")

    info = store_file_info()
    print(f"✅ المخزن المشترك: {info['path']} (kind={info['kind']}, ready={'✅' if info['exists'] else '❌'})")
    if info.get("version") is not None:
        print(f"✅ store version: {info['version']}")
    if info.get("size_bytes") is not None:
        print(f"✅ store size: {info['size_bytes']} bytes")
    print(f"✅ store_status: {'✅' if _HAS_STORE_STATUS else '⚠️  fallback'}")

    if SIM5_ENABLED:
        print(f"✅ 5SIM مفعّل — Base: {SIM5_BASE}")
        print(f"✅ هامش الربح: {FIVESIM_MARKUP_PERCENT}%")
        try:
            p = await sim5_get("/user/profile", auth=True)
            print(f"✅ 5SIM profile: balance={p.get('balance')} rating={p.get('rating')}")
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

    task = asyncio.create_task(keepalive_loop())
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        try:
            save_now(STORE)
            print("[app] 💾 store saved on shutdown")
        except Exception as e:
            print(f"[app] ⚠️  shutdown save failed: {type(e).__name__}: {e}")


# ══════════════════════════════════════════════════════════════
# App
# ══════════════════════════════════════════════════════════════
app = FastAPI(
    title="Jamal Solutions — Customer Portal",
    description="Virtual Numbers Platform",
    version="1.9.3",
    lifespan=lifespan,
)

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# ══════════════════════════════════════════════════════════════
# Middleware
# ⚠️ ترتيب حرج: SessionMiddleware أولاً ثم auth_guard
#    (auth_guard يحتاج request.session جاهزاً)
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

app.middleware("http")(auth_guard)

# ══════════════════════════════════════════════════════════════
# Routers
# ══════════════════════════════════════════════════════════════
for r in (
    system.router,
    auth.router,
    pages.router,
    prices.router,
    orders.router,
    wallet.router,
    vouchers_router.router,
    sms.router,
    support.router,
    profile.router,
    security.router,
    admin.router,
    me.router,
):
    app.include_router(r)
