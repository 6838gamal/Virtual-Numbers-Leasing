"""Middleware المصادقة."""
from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse

from app.config import GUEST_MODE, PUBLIC_PATHS, PROTECTED_PREFIXES
from app.core.state import now_iso
from app.services.customers import get_customer_from_session, session_is_expired


async def auth_guard(request: Request, call_next):
    if GUEST_MODE:
        return await call_next(request)

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
        if cid and session_is_expired(request):
            request.session.clear()
            cid = None
            expired = True
    except Exception:
        cid = None

    if cid:
        customer = get_customer_from_session(request)
        if customer is None:
            try:
                request.session.clear()
            except Exception:
                pass
            cid = None
            expired = True
        else:
            try:
                request.session["_last_seen"] = now_iso()
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
