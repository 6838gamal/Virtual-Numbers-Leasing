"""الدعم الفني."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Request

from app.core.storage import STORE, persist_now, refresh
from app.deps import get_current_customer
from app.models.schemas import TicketCreate


router = APIRouter()


@router.post("/api/v1/support/tickets")
async def create_ticket(payload: TicketCreate, request: Request):
    refresh()
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
    persist_now()
    return {"id": ticket["id"], "status": "open"}
