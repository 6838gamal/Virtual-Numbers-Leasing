"""مسارات النظام: /health, /status, /api/v1/system/*."""
from __future__ import annotations

import asyncio
import json
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sse_starlette.sse import EventSourceResponse

from app.config import (
    HIDE_PRICES_FROM_CUSTOMERS,
    GUEST_MODE,
    GUEST_REQUIRES_VOUCHER,
    HIDE_BALANCE_FROM_CUSTOMERS,
    BLOCKED_COUNTRY_SLUGS,
    BLOCKED_COUNTRY_CODES,
    SIM5_ENABLED,
    FIVESIM_MARKUP_PERCENT,
    ADMIN_SECRET,
)
from app.core.storage import STORE, refresh, store_file_info
from app.core.state import START_TIME, fmt_uptime, now_iso, _REQUEST_STATS
from app.core.keepalive import KA_STATE, add_subscriber, remove_subscriber
from app.services.sim5 import sim5_get, Sim5Error


router = APIRouter()


# ══════════════════════════════════════════════════════════════
# /health
# ══════════════════════════════════════════════════════════════
@router.get("/health")
async def health():
    info = store_file_info()
    out = {
        "status": "ok",
        "uptime": fmt_uptime(time.time() - START_TIME),
        "timestamp": now_iso(),
        "store_file": info["path"],
        "store_kind": info["kind"],
        "store_ready": info["exists"],
        "hide_prices": HIDE_PRICES_FROM_CUSTOMERS,
        "guest_mode": GUEST_MODE,
        "guest_requires_voucher": GUEST_REQUIRES_VOUCHER,
        "hide_balance": HIDE_BALANCE_FROM_CUSTOMERS,
        "popular_services_count": len(STORE.get("popular_services", [])),
        "blocked_countries": sorted(BLOCKED_COUNTRY_SLUGS),
    }
    if info.get("version") is not None:
        out["store_version"] = info["version"]
    if info.get("size_bytes") is not None:
        out["store_size_bytes"] = info["size_bytes"]
    return out


# ══════════════════════════════════════════════════════════════
# /api/v1/system/health-check
# ══════════════════════════════════════════════════════════════
@router.get("/api/v1/system/health-check")
async def health_check():
    from app.main import app
    return {
        "status": "healthy",
        "version": app.version,
        "uptime": fmt_uptime(time.time() - START_TIME),
        "timestamp": now_iso(),
    }


# ══════════════════════════════════════════════════════════════
# /api/v1/system/version
# ══════════════════════════════════════════════════════════════
@router.get("/api/v1/system/version")
async def system_version():
    from app.main import app
    refresh()
    sim5_info: dict = {"enabled": SIM5_ENABLED}
    if SIM5_ENABLED:
        try:
            p = await sim5_get("/user/profile", auth=True)
            sim5_info.update({
                "balance": p.get("balance"),
                "rating": p.get("rating"),
                "email": p.get("email"),
                "vendor": p.get("vendor"),
                "ok": True,
            })
        except Sim5Error as e:
            sim5_info.update({"ok": False, "error": e.message, "error_code": e.status})

    info = store_file_info()
    out = {
        "version": app.version,
        "features": {
            "vouchers": True,
            "owner_wallet": True,
            "markup_percent": FIVESIM_MARKUP_PERCENT,
            "deposit_requests": True,
            "admin_endpoints": bool(ADMIN_SECRET),
            "login_method": "none (guest)" if GUEST_MODE else "access_code",
            "access_codes": True,
            "single_screen": True,
            "shared_store": True,
            "multi_pair_vouchers": True,
            "voucher_check_endpoint": True,
            "voucher_check_optional_pair": True,
            "hide_prices_from_customers": HIDE_PRICES_FROM_CUSTOMERS,
            "hide_balance_from_customers": HIDE_BALANCE_FROM_CUSTOMERS,
            "guest_mode": GUEST_MODE,
            "guest_requires_voucher": GUEST_REQUIRES_VOUCHER,
            "popular_services_admin": True,
            "popular_services_tolerant_match": True,
            "blocked_countries": sorted(BLOCKED_COUNTRY_SLUGS),
            "blocked_country_codes": sorted(BLOCKED_COUNTRY_CODES),
        },
        "sim5": sim5_info,
        "customers_in_store": len(STORE["customers"]),
        "vouchers_in_store": len(STORE["vouchers"]),
        "access_codes_in_store": len(STORE["access_codes"]),
        "popular_services_count": len(STORE.get("popular_services", [])),
        "popular_services": STORE.get("popular_services", []),
        "uptime": fmt_uptime(time.time() - START_TIME),
        "store_file": info["path"],
        "store_kind": info["kind"],
    }
    if info.get("version") is not None:
        out["store_version"] = info["version"]
    return out


# ══════════════════════════════════════════════════════════════
# /api/v1/system/sim5-profile
# ══════════════════════════════════════════════════════════════
@router.get("/api/v1/system/sim5-profile")
async def sim5_profile():
    if not SIM5_ENABLED:
        raise HTTPException(503, "5SIM غير مفعّل — SIM5_API_KEY غير مضبوط")
    try:
        p = await sim5_get("/user/profile", auth=True)
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
# /api/v1/system/whoami
# ══════════════════════════════════════════════════════════════
@router.get("/api/v1/system/whoami")
async def whoami(request: Request):
    refresh()
    if GUEST_MODE:
        return {
            "has_session": False,
            "customer_id": None,
            "customer_in_store": False,
            "email": None,
            "name": "زائر",
            "customers_count": len(STORE["customers"]),
            "guest_mode": True,
        }
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


# ══════════════════════════════════════════════════════════════
# /api/v1/system/request-stats
# ══════════════════════════════════════════════════════════════
@router.get("/api/v1/system/request-stats")
async def request_stats():
    return _REQUEST_STATS


# ══════════════════════════════════════════════════════════════
# /status
# ══════════════════════════════════════════════════════════════
@router.get("/status", response_class=HTMLResponse)
async def status_page(request: Request):
    from app.main import app
    refresh()
    uptime = fmt_uptime(time.time() - START_TIME)
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
            p = await sim5_get("/user/profile", auth=True)
            sim5_status = f"✅ متصل — رصيد: {p.get('balance')} — تقييم: {p.get('rating')}"
        except Sim5Error as e:
            sim5_status = f"⚠️ خطأ [{e.status}] {e.message}"

    info = store_file_info()
    store_kind_str = f"{info['kind']} — {'✅' if info['exists'] else '❌'}"
    blocked_str = ", ".join(sorted(BLOCKED_COUNTRY_SLUGS)) or "—"

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
      <span>الأكثر طلباً: <b>{len(STORE.get('popular_services', []))}</b></span>
    </div>
    <div class="meta">
      <span>المخزن: <b>{store_kind_str}</b></span>
      <span>الأسعار مخفية: <b>{'✅ نعم' if HIDE_PRICES_FROM_CUSTOMERS else '❌ لا'}</b></span>
      <span>وضع الضيف: <b>{'✅ نعم' if GUEST_MODE else '❌ لا'}</b></span>
      <span>دول محظورة: <b>{blocked_str}</b></span>
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


# ══════════════════════════════════════════════════════════════
# /api/v1/system/keepalive-status
# ══════════════════════════════════════════════════════════════
@router.get("/api/v1/system/keepalive-status")
async def keepalive_status():
    from app.config import KA_INTERVAL
    elapsed = int(time.time() - START_TIME)
    KA_STATE["uptime"] = fmt_uptime(elapsed)
    KA_STATE["next_ping_in"] = max(0, KA_INTERVAL - (elapsed % KA_INTERVAL))
    return JSONResponse(KA_STATE)


# ══════════════════════════════════════════════════════════════
# /api/v1/system/events (SSE)
# ══════════════════════════════════════════════════════════════
@router.get("/api/v1/system/events")
async def sse_events(request: Request):
    queue: asyncio.Queue = asyncio.Queue(maxsize=100)
    add_subscriber(queue)

    async def event_generator():
        elapsed = int(time.time() - START_TIME)
        KA_STATE["uptime"] = fmt_uptime(elapsed)
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
            remove_subscriber(queue)

    return EventSourceResponse(event_generator())
