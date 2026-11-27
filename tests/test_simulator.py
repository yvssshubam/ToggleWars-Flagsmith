"""Offline tests for the traffic simulator, run against the real shop app
in-process (fake Flagsmith SDK, no network)."""

import asyncio
import random

import httpx

from autopilot_sdk.flags import set_client
from demo_shop.app import create_app
from simulator.traffic import SKUS, buy_probability, make_cart, pick_user, run
from demo_shop import app as shop_module
from tests.sample_checkout import wrapped_calculate_total
from tests.test_autopilot_sdk import FakeSDK


def test_cart_shape():
    rng = random.Random(1)
    for _ in range(200):
        items = make_cart(rng)["items"]
        assert 1 <= len(items) <= 3
        assert len({i["sku"] for i in items}) == len(items)
        assert all(i["sku"] in SKUS and 1 <= i["qty"] <= 3 for i in items)


def test_internal_share_and_ids():
    rng = random.Random(2)
    picks = [pick_user(rng, users=200, staff=10, internal_share=0.1) for _ in range(5000)]
    staff = [u for u, internal in picks if internal]
    assert 0.08 < len(staff) / len(picks) < 0.12
    assert all(u.startswith("staff-") for u in staff)
    assert all(u.startswith("user-") for u, internal in picks if not internal)


def test_no_staff_means_no_internal_requests():
    rng = random.Random(3)
    assert not any(pick_user(rng, 200, 0, 0.5)[1] for _ in range(500))


def test_run_against_shop_in_process(monkeypatch):
    monkeypatch.setattr(shop_module, "calculate_total", wrapped_calculate_total())
    events, lines = [], []
    sdk = FakeSDK()  # internal users get the new path
    app = create_app(client=sdk, sink=events.extend, metrics_url=None)
    transport = httpx.ASGITransport(app=app)
    try:
        totals = asyncio.run(run("http://shop", rps=50, duration=1.0, seed=4,
                                 report_every=0.5, transport=transport, out=lines.append))
    finally:
        app.state.autopilot.close()  # flushes the reporter
        set_client(None)

    assert 35 <= totals.sent <= 60
    assert totals.by_status == {"2xx": totals.sent}
    assert len(lines) >= 1 and "req/s" in lines[0]
    checkouts = [e for e in events if e.get("event") == "checkout"]
    orders = [e for e in events if e.get("event") == "order"]
    assert len(checkouts) == totals.sent and len(orders) == totals.orders > 0
    assert len(events) == totals.sent + totals.orders  # every request is tagged
    new_checkouts = sum(e["path"] == "new" for e in checkouts)
    assert new_checkouts == totals.internal  # only staff got the new path
    for path in ("new", "old"):  # an order always follows a priced cart on the same path
        assert sum(e["path"] == path for e in orders) <= sum(e["path"] == path for e in checkouts)
    assert "orders" in lines[0]
    assert {call[1]["is_internal"] for call in sdk.calls} <= {True, False}


def test_unreachable_shop_counts_errors():
    def refuse(request):
        raise httpx.ConnectError("refused", request=request)

    totals = asyncio.run(run("http://nowhere", rps=20, duration=0.3,
                             transport=httpx.MockTransport(refuse), out=lambda _: None))
    assert totals.sent > 0 and totals.by_status == {"error": totals.sent}


def test_buy_probability_falls_with_price():
    assert buy_probability(10) > 0.9 and abs(buy_probability(35) - 0.5) < 1e-9 and buy_probability(60) < 0.05
    assert buy_probability(32.40) > buy_probability(36.00)  # a 10% discount converts better


def test_no_orders_option(monkeypatch):
    monkeypatch.setattr(shop_module, "calculate_total", wrapped_calculate_total())
    events = []
    app = create_app(client=FakeSDK(), sink=events.extend, metrics_url=None)
    try:
        totals = asyncio.run(run("http://shop", rps=40, duration=0.5, seed=5, orders=False,
                                 transport=httpx.ASGITransport(app=app), out=lambda _: None))
    finally:
        app.state.autopilot.close()
        set_client(None)
    assert totals.orders == 0 and all(e["event"] == "checkout" for e in events)