"""Keepalive loop + SSE broadcasting."""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import httpx

from app.config import KA_INTERVAL, HISTORY_MAX
from app.core.state import START_TIME, fmt_uptime, now_iso


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

_alert_flag = {"down": False}
_subscribers: set[asyncio.Queue] = set()


def push_history(ok: bool) -> None:
    KA_STATE["history"].append({"ok": ok, "time": now_iso()})
    if len(KA_STATE["history"]) > HISTORY_MAX:
        KA_STATE["history"] = KA_STATE["history"][-HISTORY_MAX:]


async def broadcast(event: str, data: dict) -> None:
    payload = {"event": event, "data": json.dumps(data, default=str)}
    for q in list(_subscribers):
        try:
            q.put_nowait(payload)
        except asyncio.QueueFull:
            pass


def add_subscriber(q: asyncio.Queue) -> None:
    _subscribers.add(q)


def remove_subscriber(q: asyncio.Queue) -> None:
    _subscribers.discard(q)


async def keepalive_loop() -> None:
    await asyncio.sleep(5)
    async with httpx.AsyncClient(timeout=10.0) as client:
        while True:
            try:
                r = await client.get("http://127.0.0.1:8000/health")
                KA_STATE["last_status"] = r.status_code
                KA_STATE["last_ping"] = now_iso()
                ok = r.status_code == 200
                if ok:
                    KA_STATE["ping_count"] += 1
                    KA_STATE["server_ok"] = True
                    push_history(True)
                    if _alert_flag["down"]:
                        _alert_flag["down"] = False
                        await broadcast("recover", {
                            "message": "✅ الخدمة عادت للعمل",
                            "time": now_iso(),
                        })
                else:
                    KA_STATE["fail_count"] += 1
                    KA_STATE["server_ok"] = False
                    push_history(False)
                    if not _alert_flag["down"]:
                        _alert_flag["down"] = True
                        await broadcast("alert", {
                            "message": f"⚠️ الخدمة أررجعت HTTP {r.status_code}",
                            "time": now_iso(),
                        })
            except Exception as exc:
                KA_STATE["fail_count"] += 1
                KA_STATE["server_ok"] = False
                KA_STATE["last_ping"] = now_iso()
                KA_STATE["last_status"] = None
                push_history(False)
                if not _alert_flag["down"]:
                    _alert_flag["down"] = True
                    await broadcast("alert", {
                        "message": f"🔴 الخدمة لا تستجيب: {type(exc).__name__}",
                        "time": now_iso(),
                    })
            await asyncio.sleep(KA_INTERVAL)
