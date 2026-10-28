"""GET /api/v1/me/stats."""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.config import HIDE_BALANCE_FROM_CUSTOMERS
from app.core.storage import refresh
from app.services.customers import get_customer_from_session, get_my_stats


router = APIRouter()


@router.get("/api/v1/me/stats")
async def me_stats(request: Request):
    refresh()
    customer = get_customer_from_session(request)
    if not customer:
        return JSONResponse(
            {"error": "session_expired", "detail": "انتهت الجلسة"},
            status_code=401,
        )
    return {
        "customer": {
            "id": customer["id"],
            "name": customer["name"],
            "email": customer["email"],
            "balance": None if HIDE_BALANCE_FROM_CUSTOMERS else customer["balance"],
        },
        "stats": await get_my_stats(customer["id"]),
    }
