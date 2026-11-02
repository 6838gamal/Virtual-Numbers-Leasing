"""
test_orders_single.py
=====================
اختبارات سلامة قناة الطلبات — ملف واحد مكتفٍ بذاته.

ضمانات عدم إهدار الرصيد الحقيقي (ثلاثية):
  1) sim5_get و build_live_catalog مُستبدلان بـ AsyncMock.
  2) حجب الاتصالات الصادرة (AF_INET/AF_INET6) دون كسر TestClient.
  3) STORE و persist_now معزولان تماماً في الذاكرة.

التشغيل:
    pytest app/test_orders_single.py -v
"""
from __future__ import annotations

import importlib
import socket as _socket
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient


# ══════════════════════════════════════════════════════════════
# ⚙️ مسار وحدة الطلبات — يطابق ما أرسلته (app/routers/orders.py)
# ══════════════════════════════════════════════════════════════
ORDERS_MODULE = "app.routers.orders"

orders = importlib.import_module(ORDERS_MODULE)
from app.services.sim5 import Sim5Error  # noqa: E402


# ══════════════════════════════════════════════════════════════
# 🛡️ حماية الشبكة (autouse) — لا تكسر TestClient
# ══════════════════════════════════════════════════════════════
@pytest.fixture(autouse=True)
def _forbid_real_network(monkeypatch):
    """
    يحجب الاتصالات الصادرة الحقيقية (AF_INET/AF_INET6)
    دون المساس بـ socketpair (AF_UNIX) التي يحتاجها asyncio/anyio.
    """
    _real_connect = _socket.socket.connect
    _real_connect_ex = _socket.socket.connect_ex
    _real_create_conn = _socket.create_connection

    def _guarded_connect(self, address):
        if self.family in (_socket.AF_INET, _socket.AF_INET6):
            raise RuntimeError(
                "⛔ محاولة اتصال شبكي حقيقي (AF_INET) في اختبار — ممنوع!"
            )
        return _real_connect(self, address)

    def _guarded_connect_ex(self, address):
        if self.family in (_socket.AF_INET, _socket.AF_INET6):
            raise RuntimeError(
                "⛔ محاولة اتصال شبكي حقيقي (AF_INET) في اختبار — ممنوع!"
            )
        return _real_connect_ex(self, address)

    def _guarded_create_connection(address, *a, **k):
        host = address[0] if isinstance(address, tuple) else address
        if host not in ("127.0.0.1", "::1", "localhost", "testserver"):
            raise RuntimeError(
                f"⛔ create_connection إلى {host} في اختبار — ممنوع!"
            )
        return _real_create_conn(address, *a, **k)

    monkeypatch.setattr(_socket.socket, "connect", _guarded_connect)
    monkeypatch.setattr(_socket.socket, "connect_ex", _guarded_connect_ex)
    monkeypatch.setattr(_socket, "create_connection", _guarded_create_connection)


# ══════════════════════════════════════════════════════════════
# 🧱 مخزن وهمي + كتالوج + عميل
# ══════════════════════════════════════════════════════════════
def _fresh_store():
    return {
        "orders": [],
        "numbers": [],
        "sms": [],
        "transactions": [],
        "customers": [{"id": 1, "balance": 50.0}],
        "vouchers": [],
        "owner_wallet": {"total_profit": 0.0, "reserved_for_5sim": 0.0},
    }


def _catalog():
    return {
        "ok": True,
        "countries": [{
            "slug": "russia", "code": "RU", "name": "Russia",
            "flag": "🇷🇺", "prefix": "+7",
            "services": [{
                "id": "telegram", "name": "Telegram",
                "available": True, "operator": "any",
                "cost": 5.0, "fivesim_cost": 5.0,
            }],
        }],
    }


class Ctx:
    """حاوية حالة الاختبار الواحد."""
    def __init__(self):
        self.store = _fresh_store()
        self.customer = self.store["customers"][0]
        self.catalog = _catalog()
        self.sim5 = None
        self.client = None


@pytest.fixture
def ctx():
    c = Ctx()
    with (
        patch.object(orders, "STORE", c.store),
        patch.object(orders, "refresh", lambda: None),
        patch.object(orders, "persist_now", lambda: None),
        patch.object(orders, "get_current_customer",
                     new=AsyncMock(return_value=c.customer)),
        patch.object(orders, "build_live_catalog",
                     new=AsyncMock(return_value=c.catalog)),
        patch.object(orders, "sim5_get", new=AsyncMock()) as sim5_mock,
        patch.object(orders, "SIM5_ENABLED", True),
        patch.object(orders, "GUEST_MODE", False),
        patch.object(orders, "GUEST_REQUIRES_VOUCHER", False),
        patch.object(orders, "HIDE_PRICES_FROM_CUSTOMERS", False),
        patch.object(orders, "HIDE_BALANCE_FROM_CUSTOMERS", False),
        patch.object(orders, "is_country_blocked", lambda *a, **k: False),
    ):
        c.sim5 = sim5_mock
        from fastapi import FastAPI
        app = FastAPI()
        app.include_router(orders.router)
        c.client = TestClient(app)
        yield c


# ══════════════════════════════════════════════════════════════
# 🔧 مساعدات
# ══════════════════════════════════════════════════════════════
PAYLOAD = {"country": "Russia", "service": "telegram"}


def _buy_ok(sim5_id="sim5-abc", price=5.0, phone="+79000000000"):
    return {
        "id": sim5_id, "phone": phone, "status": "PENDING",
        "expires": "2026-11-02T12:00:00Z",
        "created_at": "2026-11-02T11:00:00Z",
        "price": price, "operator": "any", "sms": [],
    }


def _seed_order(ctx, sim5_id="sim5-1", price=6.0, cost=5.0, profit=1.0):
    ctx.store["orders"].append({
        "id": 1, "customer_id": ctx.customer["id"],
        "sim5_id": sim5_id, "phone": "+7900", "status": "PENDING",
        "price": price, "fivesim_cost": cost, "profit": profit,
        "expires": None, "paid_by_voucher": None, "refunded": False,
    })
    ctx.store["numbers"].append({"sim5_id": sim5_id, "sms_count": 0, "status": "active"})
    ctx.customer["balance"] -= price
    ctx.store["owner_wallet"]["total_profit"] += profit
    ctx.store["owner_wallet"]["reserved_for_5sim"] += cost


def _with_sale_price(monkeypatch, fn):
    monkeypatch.setattr(orders, "calculate_sale_price", fn)


# ══════════════════════════════════════════════════════════════
# 🧪 مجموعة 1 — إنشاء الطلب
# ══════════════════════════════════════════════════════════════
def test_create_success_charges_correct_amount(ctx, monkeypatch):
    ctx.sim5.return_value = _buy_ok()
    _with_sale_price(monkeypatch, lambda cost: cost + 1.0)
    r = ctx.client.post("/api/v1/orders", json=PAYLOAD)
    assert r.status_code == 200, r.text
    assert r.json()["price"] == 6.0
    assert ctx.customer["balance"] == pytest.approx(44.0)
    assert len(ctx.store["orders"]) == 1
    assert ctx.store["transactions"][-1]["type"] == "purchase"
    assert ctx.store["transactions"][-1]["amount"] == -6.0
    assert ctx.store["owner_wallet"]["total_profit"] == pytest.approx(1.0)
    assert ctx.store["owner_wallet"]["reserved_for_5sim"] == pytest.approx(5.0)


def test_create_insufficient_balance_no_sim5_call(ctx, monkeypatch):
    ctx.customer["balance"] = 1.0
    ctx.sim5.return_value = _buy_ok()
    _with_sale_price(monkeypatch, lambda cost: cost + 1.0)
    r = ctx.client.post("/api/v1/orders", json=PAYLOAD)
    assert r.status_code == 400 and "غير كافٍ" in r.json()["detail"]
    ctx.sim5.assert_not_called()
    assert ctx.customer["balance"] == 1.0
    assert ctx.store["orders"] == []


def test_create_sale_price_above_balance_cancels(ctx, monkeypatch):
    """الرصيد يكفي cost لكن لا يكفي sale_price → يجب إلغاء الطلب في 5SIM."""
    ctx.customer["balance"] = 5.5
    ctx.sim5.side_effect = [_buy_ok(), {"status": "CANCELED"}]
    _with_sale_price(monkeypatch, lambda cost: cost + 2.0)
    r = ctx.client.post("/api/v1/orders", json=PAYLOAD)
    assert r.status_code == 400 and "رصيدك غير كافٍ" in r.json()["detail"]
    assert ctx.sim5.await_count == 2  # buy + cancel
    assert ctx.customer["balance"] == 5.5
    assert ctx.store["orders"] == []


def test_create_sim5_disabled(ctx):
    with patch.object(orders, "SIM5_ENABLED", False):
        r = ctx.client.post("/api/v1/orders", json=PAYLOAD)
    assert r.status_code == 503


def test_create_blocked_country(ctx):
    with patch.object(orders, "is_country_blocked", lambda *a, **k: True):
        r = ctx.client.post("/api/v1/orders", json=PAYLOAD)
    assert r.status_code == 400 and "غير متاحة" in r.json()["detail"]
    ctx.sim5.assert_not_called()


def test_create_unknown_country(ctx):
    r = ctx.client.post("/api/v1/orders",
                        json={"country": "atlantis", "service": "telegram"})
    assert r.status_code == 400 and "غير مدعومة" in r.json()["detail"]
    ctx.sim5.assert_not_called()


def test_create_service_unavailable(ctx):
    r = ctx.client.post("/api/v1/orders",
                        json={"country": "Russia", "service": "whatsapp"})
    assert r.status_code == 400 and "غير متوفرة" in r.json()["detail"]
    ctx.sim5.assert_not_called()


def test_create_sim5_error_propagates(ctx):
    # توقيع Sim5Error: (status_code, message, endpoint="")
    ctx.sim5.side_effect = Sim5Error(402, "no funds")
    r = ctx.client.post("/api/v1/orders", json=PAYLOAD)
    assert r.status_code == 400 and "no funds" in r.json()["detail"]
    assert ctx.customer["balance"] == 50.0


# ══════════════════════════════════════════════════════════════
# 🧪 مجموعة 2 — المزامنة
# ══════════════════════════════════════════════════════════════
def test_sync_imports_only_new(ctx):
    ctx.store["orders"].append({"id": 1, "sim5_id": "sim5-1"})
    ctx.sim5.return_value = {"Data": [
        {"id": "sim5-1", "phone": "1", "status": "PENDING"},
        {"id": "sim5-2", "phone": "2", "status": "PENDING"},
    ]}
    r = ctx.client.get("/api/v1/orders/sync")
    assert r.status_code == 200 and r.json()["imported"] == 1
    assert len(ctx.store["orders"]) == 2


def test_sync_sim5_disabled(ctx):
    with patch.object(orders, "SIM5_ENABLED", False):
        r = ctx.client.get("/api/v1/orders/sync")
    assert r.status_code == 503


def test_sync_sim5_error(ctx):
    ctx.sim5.side_effect = Sim5Error(503, "down")
    r = ctx.client.get("/api/v1/orders/sync")
    assert r.status_code == 503


# ══════════════════════════════════════════════════════════════
# 🧪 مجموعة 3 — الفحص
# ══════════════════════════════════════════════════════════════
def test_check_imports_sms_once(ctx):
    _seed_order(ctx)
    ctx.sim5.return_value = {
        "status": "RECEIVED", "expires": "2026-11-02T12:00:00Z",
        "sms": [{"id": "s1", "sender": "TG", "text": "123",
                 "code": "123", "created_at": "2026-11-02T11:05:00Z"}],
    }
    r1 = ctx.client.get("/api/v1/orders/1/check")
    assert r1.status_code == 200 and r1.json()["new_sms"] == 1
    assert len(ctx.store["sms"]) == 1
    r2 = ctx.client.get("/api/v1/orders/1/check")
    assert r2.json()["new_sms"] == 0
    assert len(ctx.store["sms"]) == 1


def test_check_not_found(ctx):
    r = ctx.client.get("/api/v1/orders/999/check")
    assert r.status_code == 404


def test_check_other_customer_404(ctx):
    _seed_order(ctx)
    ctx.store["orders"][0]["customer_id"] = 99
    r = ctx.client.get("/api/v1/orders/1/check")
    assert r.status_code == 404
    ctx.sim5.assert_not_called()


# ══════════════════════════════════════════════════════════════
# 🧪 مجموعة 4 — الإنهاء والإلغاء
# ══════════════════════════════════════════════════════════════
def test_finish_sets_status_no_refund(ctx):
    _seed_order(ctx)
    bal = ctx.customer["balance"]
    ctx.sim5.return_value = {"status": "FINISHED"}
    r = ctx.client.post("/api/v1/orders/1/finish")
    assert r.status_code == 200
    assert ctx.store["orders"][0]["status"] == "FINISHED"
    assert ctx.customer["balance"] == bal


def test_cancel_refunds_and_reverses_wallet(ctx):
    _seed_order(ctx)
    bal = ctx.customer["balance"]
    ctx.sim5.return_value = {"status": "CANCELED"}
    r = ctx.client.post("/api/v1/orders/1/cancel")
    assert r.status_code == 200 and r.json()["refund"] == 6.0
    assert ctx.customer["balance"] == pytest.approx(bal + 6.0)
    assert ctx.store["owner_wallet"]["total_profit"] == pytest.approx(0.0)
    assert ctx.store["owner_wallet"]["reserved_for_5sim"] == pytest.approx(0.0)
    assert ctx.store["transactions"][-1]["type"] == "refund"
    assert ctx.store["numbers"][0]["status"] == "cancelled"


def test_cancel_twice_does_not_double_refund(ctx):
    """يجب ألا يُسترد المبلغ مرتين عند تكرار الإلغاء."""
    _seed_order(ctx)
    ctx.sim5.return_value = {"status": "CANCELED"}
    r1 = ctx.client.post("/api/v1/orders/1/cancel")
    assert r1.status_code == 200
    bal_after_first = ctx.customer["balance"]

    r2 = ctx.client.post("/api/v1/orders/1/cancel")
    # الطلب مغلق → يجب رفض الطلب الثاني (400)
    assert r2.status_code == 400
    assert ctx.customer["balance"] == pytest.approx(bal_after_first)


# ══════════════════════════════════════════════════════════════
# 🧪 مجموعة 5 — الأكواد (vouchers) والأمان المالي
# ══════════════════════════════════════════════════════════════
def test_voucher_does_not_deduct_balance_and_returns_remainder(ctx, monkeypatch):
    voucher = {
        "code": "V-100", "value": 10.0, "status": "active",
        "allowed_pairs": [{"country": "russia", "service": "telegram"}],
    }
    ctx.store["vouchers"].append(voucher)
    monkeypatch.setattr(orders, "validate_voucher", lambda code: voucher)
    monkeypatch.setattr(orders, "voucher_allows", lambda v, c_, s: True)
    _with_sale_price(monkeypatch, lambda cost: cost + 1.0)
    ctx.sim5.return_value = _buy_ok()

    r = ctx.client.post("/api/v1/orders", json={
        "country": "Russia", "service": "telegram", "voucher_code": "V-100",
    })
    assert r.status_code == 200
    assert ctx.customer["balance"] == pytest.approx(54.0)  # 50 + (10-6)
    assert voucher["status"] == "used"
    types = [t["type"] for t in ctx.store["transactions"]]
    assert "voucher_remainder" in types


def test_voucher_insufficient_cancels_and_keeps_voucher(ctx, monkeypatch):
    voucher = {
        "code": "V-LOW", "value": 3.0, "status": "active",
        "allowed_pairs": [{"country": "russia", "service": "telegram"}],
    }
    ctx.store["vouchers"].append(voucher)
    monkeypatch.setattr(orders, "validate_voucher", lambda code: voucher)
    monkeypatch.setattr(orders, "voucher_allows", lambda v, c_, s: True)
    _with_sale_price(monkeypatch, lambda cost: cost + 2.0)
    ctx.sim5.side_effect = [_buy_ok(), {"status": "CANCELED"}]

    r = ctx.client.post("/api/v1/orders", json={
        "country": "Russia", "service": "telegram", "voucher_code": "V-LOW",
    })
    assert r.status_code == 400 and "قيمة الكود غير كافية" in r.json()["detail"]
    assert ctx.customer["balance"] == 50.0
    assert voucher["status"] == "active"
    assert ctx.sim5.await_count == 2


def test_voucher_wrong_pair_rejected(ctx, monkeypatch):
    voucher = {"code": "V-X", "value": 10.0, "status": "active",
               "allowed_pairs": [{"country": "usa", "service": "whatsapp"}]}
    monkeypatch.setattr(orders, "validate_voucher", lambda code: voucher)
    monkeypatch.setattr(orders, "voucher_allows", lambda v, c_, s: False)
    r = ctx.client.post("/api/v1/orders", json={
        "country": "Russia", "service": "telegram", "voucher_code": "V-X",
    })
    assert r.status_code == 403
    ctx.sim5.assert_not_called()


def test_wallet_invariants_after_create_and_cancel(ctx, monkeypatch):
    _with_sale_price(monkeypatch, lambda cost: cost + 1.5)
    ctx.sim5.return_value = _buy_ok()
    ctx.client.post("/api/v1/orders", json=PAYLOAD)
    ow = ctx.store["owner_wallet"]
    assert ow["total_profit"] == pytest.approx(1.5)
    assert ow["reserved_for_5sim"] == pytest.approx(5.0)

    ctx.sim5.return_value = {"status": "CANCELED"}
    ctx.client.post("/api/v1/orders/1/cancel")
    assert ow["total_profit"] == pytest.approx(0.0)
    assert ow["reserved_for_5sim"] == pytest.approx(0.0)
