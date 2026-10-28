"""الصفحات: /buy + redirects."""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.config import (
    TEMPLATES_DIR,
    GUEST_MODE,
    GUEST_REQUIRES_VOUCHER,
    HIDE_PRICES_FROM_CUSTOMERS,
    HIDE_BALANCE_FROM_CUSTOMERS,
)
from app.core.storage import STORE, refresh
from app.deps import get_current_customer
from app.services.catalog import strip_prices
from app.services.sim5 import build_live_catalog, is_country_blocked
from app.services.customers import (
    get_my_stats,
    get_my_numbers,
    get_my_recent_sms,
)


router = APIRouter()
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


@router.get("/buy", response_class=HTMLResponse)
async def buy_page(request: Request):
    refresh()
    customer = await get_current_customer(request)
    cid = customer["id"]
    stats = await get_my_stats(cid)

    result = await build_live_catalog()
    result = strip_prices(result)
    countries = result["countries"] if result["ok"] else []

    # ── الأكثر طلباً — من STORE (مطابقة مرنة + تجاهل الدول المحظورة)
    popular: list[dict] = []
    if result["ok"]:
        wanted = STORE.get("popular_services", [])
        print(f"[buy_page] popular_services in STORE: {len(wanted)}")

        by_key: dict[str, dict] = {}
        for c in countries:
            by_key[c["slug"].lower()] = c
            if c.get("code"):
                by_key[c["code"].lower()] = c
            if c.get("name"):
                by_key[c["name"].lower()] = c

        for item in wanted:
            c_slug = (item.get("country_slug") or "").strip().lower()
            s_id = (item.get("service_id") or "").strip().lower()
            if not c_slug or not s_id:
                continue

            if is_country_blocked(c_slug):
                print(f"[buy_page] 🚫 popular: country '{c_slug}' is blocked")
                continue

            c = by_key.get(c_slug)
            if not c:
                print(f"[buy_page] ⚠️ popular: country '{c_slug}' not in catalog")
                continue

            s = next(
                (x for x in c["services"] if x["id"].lower() == s_id),
                None,
            )
            if not s:
                print(f"[buy_page] ⚠️ popular: service '{s_id}' not in '{c_slug}'")
                continue
            if not s.get("available"):
                print(f"[buy_page] ⚠️ popular: '{c_slug}/{s_id}' not available")
                continue

            popular.append({
                "country_slug": c["slug"],
                "country_name": c["name"],
                "country_flag": c["flag"],
                "service_id": s["id"],
                "service_name": s["name"],
                "count": s["count"],
                "price": None,
            })

        print(f"[buy_page] ✅ popular resolved: {len(popular)}")

    stats["countries_count"] = len(countries)
    stats["services_count"] = sum(c["services_count"] for c in countries)
    stats["prices_hidden"] = HIDE_PRICES_FROM_CUSTOMERS
    stats["hide_balance"] = HIDE_BALANCE_FROM_CUSTOMERS

    my_numbers = await get_my_numbers(cid)
    sms_list = await get_my_recent_sms(cid, limit=100)

    deposits = [d for d in STORE["deposits"] if d["customer_id"] == cid]
    deposits.sort(key=lambda d: d["created_at"], reverse=True)

    txs = [t for t in STORE["transactions"] if t["customer_id"] == cid]
    txs.sort(key=lambda t: t["created_at"], reverse=True)
    total_deposits = sum(t["amount"] for t in txs if t["type"] == "deposit")
    total_spent = sum(-t["amount"] for t in txs if t["type"] == "purchase")

    tickets = [t for t in STORE["tickets"] if t["customer_id"] == cid]
    tickets.sort(key=lambda t: t["created_at"], reverse=True)

    return templates.TemplateResponse(
        request=request,
        name="buy.html",
        context={
            "customer": customer,
            "stats": stats,
            "countries": countries,
            "popular_services": popular,
            "hide_prices": HIDE_PRICES_FROM_CUSTOMERS,
            "hide_balance": HIDE_BALANCE_FROM_CUSTOMERS,
            "guest_mode": GUEST_MODE,
            "guest_requires_voucher": GUEST_REQUIRES_VOUCHER,
            "prices_meta": {
                "ok": result["ok"],
                "error": result["error"],
                "error_code": result["error_code"],
                "fetched_at": result["fetched_at"],
                "prices_hidden": HIDE_PRICES_FROM_CUSTOMERS,
            },
            "my_numbers": my_numbers,
            "recent_sms": sms_list,
            "recent_deposits": deposits[:10],
            "transactions": txs[:30],
            "summary": {
                "total_deposits": total_deposits,
                "total_spent": total_spent,
                "count": len(txs),
            },
            "tickets": tickets,
        },
    )


# ── Redirects ────────────────────────────────────────────────
@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page(request: Request):
    await get_current_customer(request)
    return RedirectResponse(url="/buy", status_code=303)


@router.get("/catalog", response_class=HTMLResponse)
async def catalog_page(request: Request):
    await get_current_customer(request)
    return RedirectResponse(url="/buy", status_code=303)


@router.get("/my-numbers", response_class=HTMLResponse)
async def my_numbers_page(request: Request):
    await get_current_customer(request)
    return RedirectResponse(url="/buy", status_code=303)


@router.get("/orders", response_class=HTMLResponse)
async def orders_page(request: Request):
    await get_current_customer(request)
    return RedirectResponse(url="/buy", status_code=303)


@router.get("/orders/{order_id}", response_class=HTMLResponse)
async def order_detail_page(request: Request, order_id: int):
    await get_current_customer(request)
    return RedirectResponse(url="/buy", status_code=303)


@router.get("/inbox", response_class=HTMLResponse)
async def inbox_page(request: Request):
    await get_current_customer(request)
    return RedirectResponse(url="/buy", status_code=303)


@router.get("/wallet", response_class=HTMLResponse)
async def wallet_page(request: Request):
    await get_current_customer(request)
    return RedirectResponse(url="/buy", status_code=303)


@router.get("/transactions", response_class=HTMLResponse)
async def transactions_page(request: Request):
    await get_current_customer(request)
    return RedirectResponse(url="/buy", status_code=303)


@router.get("/support", response_class=HTMLResponse)
async def support_page(request: Request):
    await get_current_customer(request)
    return RedirectResponse(url="/buy", status_code=303)


@router.get("/profile", response_class=HTMLResponse)
async def profile_page(request: Request):
    await get_current_customer(request)
    return RedirectResponse(url="/buy", status_code=303)


@router.get("/security", response_class=HTMLResponse)
async def security_page(request: Request):
    await get_current_customer(request)
    return RedirectResponse(url="/buy", status_code=303)


@router.get("/admin-panel", response_class=HTMLResponse)
async def admin_panel(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="admin.html",
        context={},
    )
