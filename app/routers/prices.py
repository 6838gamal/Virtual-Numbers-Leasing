"""الأسعار والأكثر طلباً."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from app.config import (
    HIDE_PRICES_FROM_CUSTOMERS,
    FIVESIM_MARKUP_PERCENT,
    GUEST_MODE,
    SIM5_ENABLED,
    BLOCKED_COUNTRY_SLUGS,
)
from app.core.storage import STORE, refresh
from app.core.state import now_iso
from app.deps import get_current_customer
from app.services.sim5 import (
    build_live_catalog,
    fetch_sim5_countries,
    fetch_sim5_prices,
    Sim5Error,
)
from app.services.catalog import strip_prices


router = APIRouter()


@router.get("/api/v1/prices")
async def api_prices(country: str | None = None):
    result = await build_live_catalog()
    result = strip_prices(result)

    countries = result["countries"]
    if country and result["ok"]:
        c_lower = country.lower()
        countries = [
            c for c in countries
            if c["slug"] == c_lower or c["code"].lower() == c_lower
        ]
    return {
        "ok": result["ok"],
        "source": "5sim",
        "markup_percent": None if HIDE_PRICES_FROM_CUSTOMERS else FIVESIM_MARKUP_PERCENT,
        "prices_hidden": HIDE_PRICES_FROM_CUSTOMERS,
        "fetched_at": result["fetched_at"],
        "error": result["error"],
        "error_code": result["error_code"],
        "count": len(countries),
        "countries": countries,
        "blocked_countries": sorted(BLOCKED_COUNTRY_SLUGS),
    }


@router.post("/api/v1/prices/refresh")
async def api_prices_refresh(request: Request):
    if not GUEST_MODE:
        await get_current_customer(request)
    if not SIM5_ENABLED:
        raise HTTPException(503, "5SIM غير مفعّل")
    try:
        await fetch_sim5_countries(force=True)
        await fetch_sim5_prices(force=True)
    except Sim5Error as e:
        raise HTTPException(e.status or 503, e.message)
    return {"status": "refreshed", "fetched_at": now_iso()}


@router.get("/api/v1/popular")
async def api_popular():
    refresh()
    return {"ok": True, "popular": STORE.get("popular_services", [])}
