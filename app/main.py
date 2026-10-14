"""
Jamal Solutions — Virtual Numbers Platform
Customer Portal (FastAPI + Jinja2 + SSE keepalive)
"""
from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sse_starlette.sse import EventSourceResponse

# ══════════════════════════════════════════════════════════════
# Paths
# ══════════════════════════════════════════════════════════════
BASE_DIR = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "app" / "static"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

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
    version="1.0.0",
    lifespan=lifespan,
)

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ══════════════════════════════════════════════════════════════
# Current customer (stub — replace with auth/session)
# ══════════════════════════════════════════════════════════════
async def get_current_customer() -> dict:
    return {
        "id": 1,
        "name": "أحمد محمد",
        "email": "ahmed@example.com",
        "initials": "أم",
        "balance": 0.0,
        "currency": "USD",
    }


# ══════════════════════════════════════════════════════════════
# Customer-scoped data stubs
# ══════════════════════════════════════════════════════════════
async def get_my_stats(customer_id: int) -> dict:
    """KPIs scoped to the current customer only."""
    return {
        # My numbers
        "my_numbers": 0,
        "active_numbers": 0,
        "expiring_soon": 0,
        # My orders
        "my_orders": 0,
        "pending_orders": 0,
        "completed_orders": 0,
        # My SMS
        "my_sms": 0,
        "sms_today": 0,
        "unread_sms": 0,
        # My spending
        "total_spent": 0.0,
        "spent_this_month": 0.0,
    }


async def get_my_orders_by_status(customer_id: int) -> dict:
    return {
        "pending": 0,
        "active": 0,
        "completed": 0,
        "expired": 0,
        "cancelled": 0,
    }


async def get_my_top_services(customer_id: int) -> list[dict]:
    """Services the customer uses most (WhatsApp, Telegram, etc)."""
    return [
        {"name": "واتساب",   "icon": "💬", "count": 0},
        {"name": "تليجرام",  "icon": "✈️", "count": 0},
        {"name": "جوجل",     "icon": "🔍", "count": 0},
        {"name": "فيسبوك",   "icon": "📘", "count": 0},
        {"name": "تويتر",    "icon": "🐦", "count": 0},
    ]


async def get_my_numbers(customer_id: int) -> list[dict]:
    """Customer's active numbers."""
    return []


async def get_my_recent_sms(customer_id: int) -> list[dict]:
    """Recent SMS received on customer's numbers."""
    return []


# ══════════════════════════════════════════════════════════════
# Routes
# ══════════════════════════════════════════════════════════════
@app.get("/health")
async def health():
    return {
        "status": "ok",
        "uptime": _fmt_uptime(time.time() - START_TIME),
        "timestamp": _now_iso(),
    }


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    customer = await get_current_customer()
    cid = customer["id"]

    stats = await get_my_stats(cid)
    orders_by_status = await get_my_orders_by_status(cid)
    top_services = await get_my_top_services(cid)
    my_numbers = await get_my_numbers(cid)
    recent_sms = await get_my_recent_sms(cid)

    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "customer": customer,
            "stats": stats,
            "orders_by_status": orders_by_status,
            "top_services": top_services,
            "my_numbers": my_numbers,
            "recent_sms": recent_sms,
        },
    )


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

    html = f"""<!doctype html>
<html lang="ar" dir="rtl">
<head>
  <meta charset="utf-8">
  <title>حالة الخدمة — جمال سولوشنز</title>
  <style>
    body {{ font-family: system-ui, sans-serif; padding: 2rem; background:#f8fafc; color:#1e293b; }}
    .card {{ background:#fff; border-radius:12px; padding:1.5rem; box-shadow:0 1px 3px rgba(0,0,0,.08); max-width:640px; margin:auto; }}
    h1 {{ font-size:1.25rem; margin:0 0 1rem; }}
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
