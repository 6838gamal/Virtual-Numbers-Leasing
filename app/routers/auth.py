"""المصادقة: /, /api/v1/auth/login-with-code, /logout."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.config import (
    GUEST_MODE,
    TEMPLATES_DIR,
)
from app.core.storage import STORE, next_id, refresh, persist_now
from app.core.state import is_safe_next, now_iso
from app.services.access_codes import validate_access_code, consume_access_code
from app.services.customers import get_customer_from_session, session_is_expired
from app.models.schemas import AccessCodeLogin


router = APIRouter()
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


@router.get("/", response_class=HTMLResponse)
async def login_page(request: Request):
    refresh()
    if GUEST_MODE:
        return RedirectResponse(url="/buy", status_code=303)

    if request.session.get("customer_id"):
        if session_is_expired(request):
            request.session.clear()
        else:
            customer = get_customer_from_session(request)
            if customer is not None:
                return RedirectResponse(url="/buy", status_code=303)
            else:
                request.session.clear()

    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={
            "error": request.query_params.get("error"),
        },
    )


@router.post("/api/v1/auth/login-with-code")
async def login_with_code(payload: AccessCodeLogin, request: Request):
    refresh()
    if GUEST_MODE:
        return {"ok": True, "redirect": "/buy", "guest_mode": True}

    record = validate_access_code(payload.code)

    customer = None
    if record["used_by"]:
        prev_id = record["used_by"][-1]
        try:
            prev_id = int(prev_id)
        except (ValueError, TypeError):
            pass
        customer = STORE["customers"].get(prev_id)

    if customer is None:
        new_id = next_id(STORE["customers"])
        email = record.get("preset_email") or f"user{new_id}@jamal.local"
        name = record.get("preset_name") or record.get("label") or f"عميل {new_id}"
        customer = {
            "id": new_id,
            "name": name,
            "email": email,
            "phone": "",
            "country": "",
            "initials": name[:2],
            "picture": None,
            "balance": float(record.get("preset_balance") or 0.0),
            "currency": "USD",
            "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "email_verified": True,
            "phone_verified": False,
            "two_fa_enabled": False,
            "has_strong_password": False,
            "security_score": 40,
            "access_code_used": record["code"],
            "prefs": {
                "email_notifications": True,
                "sms_notifications": False,
                "marketing_emails": False,
            },
        }
        STORE["customers"][new_id] = customer

        if customer["balance"] > 0:
            STORE["transactions"].append({
                "id": len(STORE["transactions"]) + 1,
                "customer_id": new_id,
                "type": "deposit",
                "description": "رصيد ترحيبي (كود دخول)",
                "amount": customer["balance"],
                "balance_after": customer["balance"],
                "created_at": now_iso(),
                "date": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
            })

    request.session["customer_id"] = customer["id"]
    request.session["email"] = customer["email"]
    request.session["name"] = customer["name"]
    request.session["logged_in_at"] = now_iso()
    request.session["_last_seen"] = now_iso()

    consume_access_code(record, customer["id"])
    persist_now()

    return {
        "ok": True,
        "redirect": "/buy",
        "customer": {
            "id": customer["id"],
            "name": customer["name"],
            "email": customer["email"],
            "balance": customer["balance"],
        },
    }


@router.api_route("/logout", methods=["GET", "POST"], name="logout")
@router.api_route("/auth/logout", methods=["GET", "POST"], name="auth_logout")
async def logout(request: Request):
    next_url = request.query_params.get("next", "/")
    if not is_safe_next(next_url):
        next_url = "/"
    request.session.clear()
    return RedirectResponse(url=next_url, status_code=303)
