"""
Jamal Solutions — FastAPI Application
Virtual Numbers Platform
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
# Keepalive state (in-memory; swap for Redis in prod)
# ══════════════════════════════════════════════════════════════
KA_INTERVAL = 420  # 7 minutes — matches dashboard.html
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
    "history": [],           # list of {"ok": bool, "time": iso}
}

HISTORY_MAX = 20
_alert_flag = {"down": False}


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


# ══════════════════════════════════════════════════════════════
# Background task — server-side keepalive every 7 min
# ══════════════════════════════════════════════════════════════
async def _keepalive_loop(app: FastAPI) -> None:
    """Pings /health every KA_INTERVAL seconds and updates KA_STATE."""
    await asyncio.sleep(5)  # small initial delay

    async with httpx.AsyncClient(timeout=10.0) as client:
        while True:
            try:
                t0 = time.perf_counter()
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
                            "message": "✅ السيرفر عاد للعمل",
                            "time": _now_iso(),
                        })
                else:
                    KA_STATE["fail_count"] += 1
                    KA_STATE["server_ok"] = False
                    _push_history(False)
                    if not _alert_flag["down"]:
                        _alert_flag["down"] = True
                        await _broadcast("alert", {
                            "message": f"⚠️ السيرفر أرجع HTTP {r.status_code}",
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
                        "message": f"🔴 السيرفر لا يستجيب: {type(exc).__name__}",
                        "time": _now_iso(),
                    })

            await asyncio.sleep(KA_INTERVAL)


# ══════════════════════════════════════════════════════════════
# SSE broadcaster (simple in-process pub/sub)
# ══════════════════════════════════════════════════════════════
_subscribers: set[asyncio.Queue] = set()


async def _broadcast(event: str, data: dict) -> None:
    payload = {"event": event, "data": json.dumps(data, default=str)}
    for q in list(_subscribers):
        try:
            q.put_nowait(payload)
        except asyncio.QueueFull:
            pass


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
    title="Jamal Solutions",
    description="Virtual Numbers Platform",
    version="1.0.0",
    lifespan=lifespan,
)

# Static mount (optional — create app/static/ if needed)
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ══════════════════════════════════════════════════════════════
# Data stubs — replace with real DB queries
# ══════════════════════════════════════════════════════════════
async def get_stats() -> dict:
    """Return dashboard stats. Replace with real DB queries."""
    return {
        "total_agents": 0,
        "active_agents": 0,
        "total_customers": 0,
        "total_conversations": 0,
        "open_conversations": 0,
        "total_tasks": 0,
        "pending_tasks": 0,
        "total_documents": 0,
        "total_workflows": 0,
        "total_users": 0,
    }


async def get_customer_status_breakdown() -> dict:
    """Return {status: count}. Replace with real DB query."""
    return {
        "lead": 0,
        "prospect": 0,
        "active": 0,
        "inactive": 0,
        "churned": 0,
    }


# ══════════════════════════════════════════════════════════════
# Routes
# ══════════════════════════════════════════════════════════════
@app.get("/health")
async def health():
    """Used by Docker HEALTHCHECK + browser ping + server keepalive."""
    return {
        "status": "ok",
        "uptime": _fmt_uptime(time.time() - START_TIME),
        "timestamp": _now_iso(),
    }


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    stats = await get_stats()
    customer_by_status = await get_customer_status_breakdown()
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "stats": stats,
            "customer_by_status": customer_by_status,
        },
    )


@app.get("/status", response_class=HTMLResponse)
async def status_page(request: Request):
    """Simple status page linked from dashboard."""
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
  <title>حالة السيرفر — جمال سولوشنز</title>
  <style>
    body {{ font-family: system-ui, sans-serif; padding: 2rem; background:#f8fafc; color:#1e293b; }}
    .card {{ background:#fff; border-radius:12px; padding:1.5rem; box-shadow:0 1px 3px rgba(0,0,0,.08); max-width:640px; margin:auto; }}
    h1 {{ font-size:1.25rem; margin:0 0 1rem; }}
    table {{ width:100%; border-collapse:collapse; font-size:14px; }}
    td {{ padding:8px; border-bottom:1px solid #e2e8f0; }}
    .meta {{ display:flex; gap:1rem; margin-bottom:1rem; font-size:14px; color:#475569; }}
    .ok  {{ color:#16a34a; font-weight:600; }}
    .bad {{ color:#dc2626; font-weight:600; }}
  </style>
</head>
<body>
  <div class="card">
    <h1>🩺 حالة السيرفر</h1>
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
    """REST fallback used by dashboard when SSE is disconnected."""
    elapsed = int(time.time() - START_TIME)
    KA_STATE["uptime"] = _fmt_uptime(elapsed)
    KA_STATE["next_ping_in"] = max(0, KA_INTERVAL - (elapsed % KA_INTERVAL))
    return JSONResponse(KA_STATE)


@app.get("/api/v1/system/events")
async def sse_events(request: Request):
    """Server-Sent Events stream consumed by dashboard.html."""
    queue: asyncio.Queue = asyncio.Queue(maxsize=100)
    _subscribers.add(queue)

    async def event_generator():
        # Initial snapshot
        elapsed = int(time.time() - START_TIME)
        KA_STATE["uptime"] = _fmt_uptime(elapsed)
        KA_STATE["next_ping_in"] = max(0, KA_INTERVAL - (elapsed % KA_INTERVAL))
        yield {
            "event": "status",
            "data": json.dumps(KA_STATE, default=str),
        }

        try:
            while True:
                if await request.is_disconnected():
                    break

                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield payload
                except asyncio.TimeoutError:
                    # heartbeat keeps connection alive through proxies
                    yield {"event": "heartbeat", "data": str(int(time.time()))}

                    # also refresh status snapshot periodically
                    elapsed = int(time.time() - START_TIME)
                    KA_STATE["uptime"] = _fmt_uptime(elapsed)
                    KA_STATE["next_ping_in"] = max(
                        0, KA_INTERVAL - (elapsed % KA_INTERVAL)
                    )
                    yield {
                        "event": "status",
                        "data": json.dumps(KA_STATE, default=str),
                    }
        finally:
            _subscribers.discard(queue)

    return EventSourceResponse(event_generator())
