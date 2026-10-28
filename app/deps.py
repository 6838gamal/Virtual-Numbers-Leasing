"""Dependency + helper مشتركة."""
from __future__ import annotations

import secrets

from fastapi import HTTPException, Request

from app.config import ADMIN_SECRET
from app.services.customers import get_customer_from_session


async def get_current_customer(request: Request) -> dict:
    customer = get_customer_from_session(request)
    if customer is None:
        raise HTTPException(status_code=401, detail="غير مسجّل")
    return customer


def check_admin(request: Request) -> None:
    if not ADMIN_SECRET:
        raise HTTPException(503, "ADMIN_SECRET غير مضبوط")
    provided = request.headers.get("X-Admin-Secret", "")
    if not secrets.compare_digest(provided, ADMIN_SECRET):
        raise HTTPException(403, "غير مصرح")
