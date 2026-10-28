"""تجهيز الكتالوج للعرض (إخفاء الأسعار...)."""
from __future__ import annotations

from app.config import HIDE_PRICES_FROM_CUSTOMERS


def strip_prices(catalog: dict) -> dict:
    if not HIDE_PRICES_FROM_CUSTOMERS or not catalog.get("ok"):
        return catalog

    out = {**catalog, "prices_hidden": True}
    new_countries = []
    for c in catalog["countries"]:
        new_c = {**c}
        new_c.pop("min_price", None)
        new_services = []
        for s in c["services"]:
            new_s = {**s}
            new_s.pop("cost", None)
            new_s.pop("fivesim_cost", None)
            new_s["price_hidden"] = True
            new_services.append(new_s)
        new_c["services"] = new_services
        new_countries.append(new_c)
    out["countries"] = new_countries
    return out
