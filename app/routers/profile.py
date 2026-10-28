"""الملف الشخصي والتفضيلات."""
from __future__ import annotations

from fastapi import APIRouter, Request

from app.core.storage import persist_now, refresh
from app.deps import get_current_customer
from app.models.schemas import ProfileUpdate, PreferencesUpdate


router = APIRouter()


@router.patch("/api/v1/profile")
async def update_profile(payload: ProfileUpdate, request: Request):
    refresh()
    customer = await get_current_customer(request)
    if payload.name is not None:
        customer["name"] = payload.name
        customer["initials"] = payload.name[:2]
    if payload.phone is not None:
        customer["phone"] = payload.phone
    if payload.country is not None:
        customer["country"] = payload.country
    persist_now()
    return {"status": "updated"}


@router.patch("/api/v1/profile/preferences")
async def update_preferences(payload: PreferencesUpdate, request: Request):
    refresh()
    customer = await get_current_customer(request)
    prefs = customer.setdefault("prefs", {})
    if payload.email_notifications is not None:
        prefs["email_notifications"] = payload.email_notifications
    if payload.sms_notifications is not None:
        prefs["sms_notifications"] = payload.sms_notifications
    if payload.marketing_emails is not None:
        prefs["marketing_emails"] = payload.marketing_emails
    persist_now()
    return {"status": "updated"}
