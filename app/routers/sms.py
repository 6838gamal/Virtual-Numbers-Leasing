"""الرسائل النصية."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from app.core.storage import STORE, persist_now, refresh
from app.deps import get_current_customer


router = APIRouter()


@router.delete("/api/v1/sms/{sms_id}")
async def delete_sms(sms_id: int, request: Request):
    refresh()
    customer = await get_current_customer(request)
    for i, s in enumerate(STORE["sms"]):
        if s["id"] == sms_id and s["customer_id"] == customer["id"]:
            STORE["sms"].pop(i)
            persist_now()
            return {"status": "deleted"}
    raise HTTPException(status_code=404, detail="الرسالة غير موجودة")
