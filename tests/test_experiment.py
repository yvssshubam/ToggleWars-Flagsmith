"""Step 12, stage 3.5 "prove it helps": conversion new vs old, from tagged
checkout/order events, in the approval request and on the dashboard."""

import sqlite3

import pytest
from fastapi.testclient import TestClient

from autopilot.experiment import MIN_PER_PATH, compare, summary_line
from autopilot.server import create_app
from autopilot.store import Store
from autopilot_sdk.flags import set_client, track
from demo_shop import app as shop_module
from demo_shop.app import create_app as create_shop
from tests.sample_checkout import wrapped_calculate_total
from tests.test_autopilot_sdk import FakeSDK
from tests.test_github import REPO
from tests.test_plan_flow import comment
from tests.test_rollout_flow import FLAG, Env


# --- the statistics (values cross-checked against statsmodels' proportions_ztest) ----------------

def test_significant_improvement():
    c = compare(720, 410, 2900, 1450)
    assert c.enough and c.verdict == "better"
    assert c.new_rate == pytest.approx(56.944, abs=1e-3) and c.old_rate == pytest.approx(50.0)
    assert c.p_value == pytest.approx(0.000847, abs=1e-6)
    assert (c.ci_low_pp, c.ci_high_pp) == (pytest.approx(2.896, abs=1e-3), pytest.approx(10.993, abs=1e-3))
    assert summary_line(c) == "conversion new 56.9% vs old 50.0%: +6.9 pp (95% CI +2.9 to +11.0), p < 0.001: better"


def test_significant_drop_and_no_difference():
    worse = compare(1000, 300, 5000, 1700)
    assert worse.verdict == "worse" and worse.p_value == pytest.approx(0.014306, abs=1e-6)
    same = compare(150, 90, 600, 330)
    assert same.verdict == "no_difference" and same.p_value == pytest.approx(0.269845, abs=1e-6)
    assert compare(400, 200, 400, 200).p_value == 1.0


def test_not_enough_data_and_edge_cases():
    c = compare(MIN_PER_PATH - 1, 50, 900, 400)
    assert not c.enough and c.verdict == "not_enough_data"
    assert summary_line(c) == f"not enough data yet ({MIN_PER_PATH - 1}/{MIN_PER_PATH} checkouts on the smaller path)"
    assert compare(200, 200, 300, 300).p_value == 1.0          # everyone converts: no variance
    capped = compare(100, 130, 100, 50)                          # orders can't exceed checkouts
    assert capped.new_orders == 100 and capped.new_rate == 100.0


# --- events: SDK, shop and store ---------------------------------------------------------------------

def test_track_tags_every_flag_event_of_the_request(monkeypatch):
    monkeypatch.setattr(shop_module, "calculate_total", wrapped_calculate_total())
    events = []
    app = create_shop(client=FakeSDK(), sink=events.extend, metrics_url=None)
    try:
        with TestClient(app) as client:
            cart = {"items": [{"sku": "beans", "qty": 2}]}
            assert client.post("/checkout", json=cart, headers={"X-User-Id": "u1"}).json() == {"total": 36.0}
            placed = client.post("/order", json=cart, headers={"X-User-Id": "u1", "X-Internal": "true"})
            assert placed.json() == {"ordered": True, "total": 32.4}  # staff get the new path
            assert client.post("/order", json={"items": [{"sku": "nope", "qty": 1}]},
                               headers={"X-User-Id": "u1"}).status_code == 400
            client.app.state.autopilot.reporter.flush()
    finally:
        set_client(None)
    assert [(e["event"], e["path"]) for e in events] == [("checkout", "old"), ("order", "new"), ("order", "old")]
    track("ignored")  # outside a request: does nothing, raises nothing


def test_store_counts_only_successful_orders_in_the_window():
    store = Store(":memory:")
    rows = [{"ts": 10, "flag": "f", "path": "new", "ok": True, "latency_ms": 1, "event": "checkout"},
            {"ts": 11, "flag": "f", "path": "new", "ok": True, "latency_ms": 1, "event": "order"},
            {"ts": 12, "flag": "f", "path": "new", "ok": False, "latency_ms": 1, "event": "order"},
            {"ts": 13, "flag": "f", "path": "old", "ok": True, "latency_ms": 1},
            {"ts": 50, "flag": "f", "path": "old", "ok": True, "latency_ms": 1, "event": "checkout"}]
    assert store.add_metrics(rows) == 5
    assert store.event_counts("f", 0) == {("new", "checkout"): 1, ("new", "order"): 1, ("old", "checkout"): 1}
    assert store.event_counts("f", 0, until=40) == {("new", "checkout"): 1, ("new", "order"): 1}


def test_old_database_gets_the_event_column(tmp_path):
    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE metrics (id INTEGER PRIMARY KEY, ts REAL NOT NULL, flag TEXT NOT NULL, path TEXT"
               " NOT NULL, ok INTEGER NOT NULL, latency_ms REAL NOT NULL, error_type TEXT, error_message TEXT,"
               " error_where TEXT)")
    db.execute("INSERT INTO metrics (ts, flag, path, ok, latency_ms) VALUES (1, 'f', 'old', 1, 2)")
    db.commit()
    db.close()
    store = Store(str(path))
    store.add_metrics([{"ts": 2, "flag": "f", "path": "new", "ok": True, "latency_ms": 1, "event": "order"}])
    assert store.event_counts("f", 0) == {("new", "order"): 1} and len(store.metrics_since("f", 0)) == 2
    store.close()


# --- in the approval request and on the dashboard -------------------------------------------------

def shoppers(env, new_checkouts, new_orders, old_checkouts, old_orders):
    env.clock.now += 1
    rows = []
    for path, checkouts, orders in (("new", new_checkouts, new_orders), ("old", old_checkouts, old_orders)):
        rows += [{"ts": env.clock.now, "flag": FLAG, "path": path, "ok": True, "latency_ms": 5,
                  "event": "checkout"}] * checkouts
        rows += [{"ts": env.clock.now, "flag": FLAG, "path": path, "ok": True, "latency_ms": 5,
                  "event": "order"}] * orders
    env.store.add_metrics(rows)


def run_to_approval(env, conv):
    env.approve_and_merge()
    shoppers(env, *conv)
    for _ in range(3):
        env.stage()
    assert env.release()["state"] == "awaiting_approval"
    return [b for b in env.bodies() if "Release to everyone?" in b][-1]


def test_approval_request_shows_a_significant_improvement():
    env = Env()
    body = run_to_approval(env, (720, 410, 2900, 1450))
    assert "**Does it help?** Checkout conversion (orders ÷ checkouts) since the rollout started" in body
    assert "| new | 720 | 410 | 56.9% |" in body and "| old | 2900 | 1450 | 50.0% |" in body
    assert "**+6.9 percentage points** (95% CI +2.9 to +11.0), p < 0.001" in body
    assert "the new path converts better" in body and "not by Flagsmith Experimentation" in body
    assert "⚠️" not in body
    assert any("conversion new 56.9% vs old 50.0%" in m for m in env.logs)  # also in the bot's console


def test_high_risk_release_with_a_conversion_drop_gets_a_warning():
    env = Env()  # the plan in these tests is high risk
    body = run_to_approval(env, (1000, 300, 5000, 1700))
    assert "the new path converts worse" in body
    assert "high-risk release and conversion dropped significantly" in body
    env.send(comment("/approve"))  # the warning informs; it doesn't block
    env.engine.tick(env.release()["id"])
    assert env.release()["state"] == "full"


def test_approval_request_without_enough_data_says_so():
    env = Env()
    body = run_to_approval(env, (40, 20, 300, 150))
    assert "**not enough data yet** (40 checkouts on the new path, 300 on the old" in body
    assert "percentage points" not in body


def test_dashboard_shows_conversion_and_freezes_it_at_full():
    env = Env()
    run_to_approval(env, (720, 410, 2900, 1450))
    client = TestClient(create_app(env.store, github_repo=REPO, engine=env.engine, dashboard_token="t",
                                   clock=env.clock), base_url="http://localhost:8000")
    conv = client.get("/api/releases").json()[0]["conversion"]
    assert conv["verdict"] == "better" and conv["new_checkouts"] == 720 and conv["min_per_path"] == MIN_PER_PATH
    env.send(comment("/approve"))
    env.engine.tick(env.release()["id"])
    shoppers(env, 500, 100, 0, 0)  # after the release to everyone: not part of the comparison
    assert client.get("/api/releases").json()[0]["conversion"]["new_checkouts"] == 720
