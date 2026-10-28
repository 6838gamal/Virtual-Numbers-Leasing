"""الأمان: كلمة المرور، 2FA، الجلسات."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Request

from app.core.storage import STORE, persist_now, refresh
from app.deps import get_current_customer
from app.models.schemas import PasswordChange


router = APIRouter()


@router.put("/api/v1/security/password")
async def change_password(payload: PasswordChange, request: Request):
    await get_current_customer(request)
    if len(payload.new) < 8:
        raise HTTPException(status_code=400, detail="كلمة المرور قصيرة")
    return {"status": "updated"}


@router.post("/api/v1/security/2fa/enable")
async def enable_2fa(request: Request):
    refresh()
    customer = await get_current_customer(request)
    secret = uuid.uuid4().hex[:16].upper()
    customer["two_fa_enabled"] = True
    customer["security_score"] = min(100, customer["security_score"] + 20)
    persist_now()
    return {"secret": secret, "status": "enabled"}


@router.post("/api/v1/security/2fa/disable")
async def disable_2fa(request: Request):
    refresh()
    customer = await get_current_customer(request)
    customer["two_fa_enabled"] = False
    customer["security_score"] = max(0, customer["security_score"] - 20)
    persist_now()
    return {"status": "disabled"}


@router.delete("/api/v1/security/sessions/{session_id}")
async def end_session(session_id: str, request: Request):
    refresh()
    await get_current_customer(request)
    STORE["sessions"] = [s for s in STORE["sessions"] if s["id"] != session_id]
    persist_now()
    return {"status": "ended"}
