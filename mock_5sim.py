"""محاكي 5SIM للاختبار المحلي — لا اتصال حقيقي، لا تكلفة."""
from fastapi import FastAPI, Header, HTTPException
from datetime import datetime, timezone, timedelta

app = FastAPI()
_orders: dict[str, dict] = {}
_next_id = 1000


def _auth(authorization: str | None):
    if not authorization or "Bearer" not in authorization:
        raise HTTPException(401, "unauthorized")


@app.get("/v1/user/profile")
async def profile(authorization: str | None = Header(None)):
    _auth(authorization)
    return {"id": 1, "email": "test@test", "balance": 100.0, "rating": 5.0}


@app.get("/v1/guest/countries")
async def countries():
    return {
        "russia": {
            "text_en": "Russia", "iso": {"RU": "Russia"},
            "prefix": {"+7": "Russia"},
            "prices": {"telegram": {"cost": 5.0, "count": 100}},
        }
    }


@app.get("/v1/guest/prices")
async def prices():
    return {
        "russia": {
            "telegram": {"any": {"cost": 5.0, "count": 100, "rate": 4.5}},
        }
    }


@app.get("/v1/user/buy/activation/{country}/{operator}/{product}")
async def buy(country: str, operator: str, product: str,
              authorization: str | None = Header(None)):
    global _next_id
    _auth(authorization)
    _next_id += 1
    oid = str(_next_id)
    expires = (datetime.now(timezone.utc) + timedelta(minutes=20)).isoformat()
    _orders[oid] = {
        "id": oid, "phone": f"+79000{_next_id}", "operator": operator,
        "product": product, "price": 5.0, "status": "PENDING",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "expires": expires, "sms": [],
    }
    return _orders[oid]


@app.get("/v1/user/check/{order_id}")
async def check(order_id: str, authorization: str | None = Header(None)):
    _auth(authorization)
    if order_id not in _orders:
        raise HTTPException(404, "order not found")
    return _orders[order_id]


@app.get("/v1/user/finish/{order_id}")
async def finish(order_id: str, authorization: str | None = Header(None)):
    _auth(authorization)
    if order_id not in _orders:
        raise HTTPException(404, "order not found")
    _orders[order_id]["status"] = "FINISHED"
    return _orders[order_id]


@app.get("/v1/user/cancel/{order_id}")
async def cancel(order_id: str, authorization: str | None = Header(None)):
    _auth(authorization)
    if order_id not in _orders:
        raise HTTPException(404, "order not found")
    _orders[order_id]["status"] = "CANCELED"
    return _orders[order_id]


@app.get("/v1/user/orders")
async def list_orders(category: str = "activation",
                      authorization: str | None = Header(None)):
    _auth(authorization)
    return {"Data": list(_orders.values())}
