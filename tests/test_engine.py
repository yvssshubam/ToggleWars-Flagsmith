"""Offline tests for policy, health gates, store and the release engine.
Flagsmith is faked; time is a controllable clock."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import autopilot_sdk
from autopilot.engine import Engine
from autopilot.flagsmith_client import FlagsmithError
from autopilot.health import PathStats, evaluate, p95
from autopilot.policy import for_tier, with_overrides
from autopilot.server import create_app
from autopilot.store import Store
from autopilot_sdk import flag_on
from tests.test_autopilot_sdk import FakeSDK


# --- policy ------------------------------------------------------------------

def test_tiers_and_bounds():
    assert for_tier("high").max_error_rate_pct == 2
    with pytest.raises(ValueError):
        for_tier("extreme")
    tighter = with_overrides(for_tier("medium"), max_error_rate_pct=3)
    assert tighter.max_error_rate_pct == 3
    with pytest.raises(ValueError, match="safety bound"):
        with_overrides(for_tier("medium"), max_error_rate_pct=10)
    with pytest.raises(ValueError, match="safety bound"):
        with_overrides(for_tier("low"), min_new_requests=5)
    with pytest.raises(ValueError, match="can't be changed"):
        with_overrides(for_tier("low"), tier="none")


# --- health ------------------------------------------------------------------

def rows(n, errors=0, latency=5.0):
    return [{"ok": i >= errors, "latency_ms": latency} for i in range(n)]


def test_p95():
    assert p95([]) == 0
    assert p95(list(range(1, 101))) == 95
    assert p95([7]) == 7


def test_health_waits_for_traffic():
    medium = for_tier("medium")
    assert evaluate(rows(29), rows(100), medium).status == "wait"
    assert "29/30" in evaluate(rows(29), rows(100), medium).reason
    assert evaluate(rows(30), rows(10), medium).status == "wait"


def test_health_pass_and_error_gates():
    medium = for_tier("medium")
    assert evaluate(rows(100), rows(100), medium).status == "pass"
    v = evaluate(rows(100, errors=30), rows(100), medium)
    assert v.status == "fail" and v.gate == "error_rate_absolute"
    # 4% new vs 1% old: under the 5% ceiling but more than 2 points worse
    v = evaluate(rows(100, errors=4), rows(100, errors=1), medium)
    assert v.status == "fail" and v.gate == "error_rate_vs_old"
    # Limits are "more than": exactly at the limit passes, one step over fails.
    assert evaluate(rows(100, errors=3), rows(100, errors=1), medium).status == "pass"   # +2 pts
    assert evaluate(rows(100, errors=2), rows(100, errors=1), for_tier("high")).status == "pass"  # +1 pt
    v = evaluate(rows(200, errors=5), rows(200, errors=2), for_tier("high"))  # 2.5% vs 1%: over 2% ceiling
    assert v.status == "fail" and v.gate == "error_rate_absolute"
    v = evaluate(rows(200, errors=7), rows(200, errors=2), with_overrides(for_tier("high"), max_error_rate_pct=5))
    assert v.status == "fail" and v.gate == "error_rate_vs_old"  # 3.5% vs 1%: +2.5 pts > 1


def test_latency_gate_ignores_jitter():
    medium = for_tier("medium")
    # 9 ms vs 5 ms is 1.8x but only +4 ms: normal jitter, not a regression
    assert evaluate(rows(100, latency=9), rows(100, latency=5), medium).status == "pass"
    v = evaluate(rows(100, latency=260), rows(100, latency=5), medium)
    assert v.status == "fail" and v.gate == "latency"


def test_health_against_baseline_at_full():
    baseline = PathStats(n=200, errors=0, p95_ms=5)
    assert evaluate(rows(100), [], for_tier("medium"), baseline_old=baseline).status == "pass"
    assert evaluate(rows(100, errors=20), [], for_tier("medium"), baseline_old=baseline).status == "fail"


# --- engine ------------------------------------------------------------------

class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


class FakeFlagsmith:
    def __init__(self):
        self.features, self.calls, self.fail_next = {}, [], set()
        self.override = None
        self.default_on = False
        self.percent = None

    def _maybe_fail(self, name, status=500):
        self.calls.append(name)
        if name in self.fail_next:
            self.fail_next.discard(name)
            raise FlagsmithError(name, status, "boom")

    def get_feature_by_name(self, name):
        return {"id": self.features[name], "name": name} if name in self.features else None

    def create_feature(self, name, description=""):
        self._maybe_fail("create_feature")
        self.features[name] = 42
        return 42

    def delete_feature(self, feature_id):
        self._maybe_fail("delete_feature")
        self.features = {k: v for k, v in self.features.items() if v != feature_id}

    def create_percentage_segment(self, name, percent, include_internal=False):
        self._maybe_fail("create_segment")
        assert include_internal
        self.percent = percent
        return 7

    def set_segment_percentage(self, segment_id, name, percent, include_internal=False):
        self._maybe_fail("widen")
        assert include_internal
        self.percent = percent

    def delete_segment(self, segment_id):
        self._maybe_fail("delete_segment")

    def enable_for_segment_only(self, feature_id, segment_id):
        self._maybe_fail("enable_segment")
        self.override, self.default_on = segment_id, False

    def set_flag_for_everyone(self, feature_id, enabled):
        self._maybe_fail("set_default")
        self.default_on = enabled

    def remove_segment_override(self, feature_id, segment_id):
        self._maybe_fail("remove_override")
        if self.override is None:
            raise FlagsmithError("remove override", 404, '{"detail": "Segment override not found."}')
        self.override = None


@pytest.fixture
def env():
    clock = Clock()
    store = Store(":memory:", clock=clock)
    fs = FakeFlagsmith()
    logs = []
    engine = Engine(store, fs, clock=clock, log=logs.append)
    return clock, store, fs, engine, logs


def feed(store, clock, flag, new=0, old=0, new_errors=0, latency=5.0):
    events = [{"ts": clock.now, "flag": flag, "path": "new", "ok": i >= new_errors, "latency_ms": latency,
               **({} if i >= new_errors else {"error_type": "RuntimeError", "error_message": "x",
                                              "error_where": "demo_shop/checkout.py:30 in _calculate_total__new"})}
              for i in range(new)]
    events += [{"ts": clock.now, "flag": flag, "path": "old", "ok": True, "latency_ms": 5.0} for _ in range(old)]
    store.add_metrics(events)


def run_stage(clock, store, engine, rid, **traffic):
    clock.now += 1
    feed(store, clock, "ap_x", **traffic)
    clock.now += 61  # past the medium soak
    engine.tick(rid)
    return store.get_release(rid)


def test_happy_path_to_released(env):
    clock, store, fs, engine, logs = env
    rid = engine.start_release("ap_x", for_tier("medium"), started_by="tester")
    assert fs.features == {"ap_x": 42} and fs.percent == 0 and fs.override == 7
    assert store.get_release(rid)["state"] == "stage_0"

    assert run_stage(clock, store, engine, rid, new=40, old=300)["state"] == "stage_1"
    assert fs.percent == 5
    assert run_stage(clock, store, engine, rid, new=40, old=300)["state"] == "stage_2"
    assert fs.percent == 25
    assert run_stage(clock, store, engine, rid, new=60, old=200)["state"] == "awaiting_approval"

    engine.tick(rid)  # nothing happens without a human
    assert store.get_release(rid)["state"] == "awaiting_approval"
    assert "approved by tester" in engine.approve("ap_x", by="tester")
    assert "already approved" in engine.approve("ap_x", by="someone-else")
    engine.tick(rid)
    assert store.get_release(rid)["state"] == "full"
    assert fs.default_on is True and fs.override is None

    assert run_stage(clock, store, engine, rid, new=200)["state"] == "released"
    kinds = [e["kind"] for e in store.events(rid)]
    assert kinds[:3] == ["started", "flag_created", "stage"] and "full" in kinds and kinds[-1] == "released"


def test_does_not_advance_on_thin_data_or_before_soak(env):
    clock, store, fs, engine, logs = env
    rid = engine.start_release("ap_x", for_tier("medium"), started_by="t")
    feed(store, clock, "ap_x", new=10, old=300)
    clock.now += 120
    engine.tick(rid)
    assert store.get_release(rid)["state"] == "stage_0"
    assert "waiting for traffic (10/30" in logs[-1]

    feed(store, clock, "ap_x", new=40, old=300)
    clock.now += 1  # enough data, but the soak restarts? no: soak counts from stage start
    engine.tick(rid)
    assert store.get_release(rid)["state"] == "stage_1"


def test_broken_new_path_is_switched_off(env):
    clock, store, fs, engine, logs = env
    rid = engine.start_release("ap_x", for_tier("medium"), started_by="t")
    run_stage(clock, store, engine, rid, new=40, old=300)  # -> stage_1
    clock.now += 1
    feed(store, clock, "ap_x", new=40, old=300, new_errors=12)  # 30% errors
    clock.now += 5  # long before the soak ends: failures act immediately
    engine.tick(rid)
    r = store.get_release(rid)
    assert r["state"] == "rolled_back" and "30.0%" in r["rollback_reason"]
    assert fs.default_on is False and fs.override is None
    report = "\n".join(logs)
    assert "SWITCHED OFF: ap_x at stage_1" in report
    assert "12 x RuntimeError at demo_shop/checkout.py:30" in report
    rolled = [e for e in store.events(rid) if e["kind"] == "rolled_back"][0]
    assert '"gate": "error_rate_absolute"' in rolled["data"]


def test_flagsmith_error_while_widening_retries(env):
    clock, store, fs, engine, logs = env
    rid = engine.start_release("ap_x", for_tier("medium"), started_by="t")
    fs.fail_next.add("widen")
    assert run_stage(clock, store, engine, rid, new=40, old=300)["state"] == "stage_0"
    assert any("retrying" in line for line in logs)
    engine.tick(rid)
    assert store.get_release(rid)["state"] == "stage_1"


def test_failed_switch_off_is_retried_until_it_works(env):
    clock, store, fs, engine, logs = env
    rid = engine.start_release("ap_x", for_tier("medium"), started_by="t")
    fs.fail_next.add("set_default")
    clock.now += 1
    feed(store, clock, "ap_x", new=40, old=300, new_errors=20)
    engine.tick(rid)
    r = store.get_release(rid)
    assert r["state"] == "stage_0" and r["rollback_reason"]  # still pending
    assert any("SWITCH-OFF FAILED" in line for line in logs)
    # Even if traffic now looks healthy, the pending switch-off still happens.
    engine.tick(rid)
    assert store.get_release(rid)["state"] == "rolled_back"


def test_override_already_gone_is_not_a_failed_rollback(env):
    clock, store, fs, engine, logs = env
    rid = engine.start_release("ap_x", for_tier("medium"), started_by="t")
    fs.override = None  # someone removed it already
    assert "rollback requested" in engine.request_rollback("ap_x", by="tester")
    engine.tick(rid)
    assert store.get_release(rid)["state"] == "rolled_back"


def test_manual_rollback_and_teardown(env):
    clock, store, fs, engine, logs = env
    rid = engine.start_release("ap_x", for_tier("low"), started_by="t")
    engine.request_rollback("ap_x", by="tester")
    engine.tick(rid)
    r = store.get_release(rid)
    assert r["state"] == "rolled_back" and r["rollback_reason"] == "manual rollback by tester"
    assert "no active release" in engine.request_rollback("ap_x", by="tester")
    done = engine.teardown("ap_x")
    assert "deleted the flag ap_x" in done and fs.features == {}


def test_cannot_start_twice_and_existing_flag_is_reused(env):
    clock, store, fs, engine, logs = env
    fs.features["ap_x"] = 99
    rid = engine.start_release("ap_x", for_tier("medium"), started_by="t")
    assert store.get_release(rid)["feature_id"] == 99 and "create_feature" not in fs.calls
    with pytest.raises(ValueError, match="already has an active release"):
        engine.start_release("ap_x", for_tier("medium"), started_by="t")


def test_failed_enable_cleans_up_segment(env):
    clock, store, fs, engine, logs = env
    fs.fail_next.add("enable_segment")
    with pytest.raises(FlagsmithError):
        engine.start_release("ap_x", for_tier("medium"), started_by="t")
    assert "delete_segment" in fs.calls and store.latest_release("ap_x") is None


def test_approve_only_when_waiting(env):
    clock, store, fs, engine, logs = env
    engine.start_release("ap_x", for_tier("medium"), started_by="t")
    assert "isn't waiting for approval" in engine.approve("ap_x", by="tester")
    assert "no release" in engine.approve("ap_y", by="tester")


# --- metrics endpoint + SDK error details -------------------------------------

def test_metrics_endpoint_stores_good_events_and_skips_bad():
    store = Store(":memory:")
    client = TestClient(create_app(store))
    good = {"ts": 1.0, "flag": "f", "path": "new", "ok": True, "latency_ms": 3}
    r = client.post("/metrics", json={"events": [good, {"flag": "f"}, {**good, "path": "sideways"}]})
    assert r.json() == {"stored": 1}
    assert client.post("/metrics", content=b"not json").json() == {"stored": 0}
    assert len(store.metrics_since("f", 0)) == 1


def test_sdk_reports_error_type_and_location():
    events = []
    app = FastAPI()
    autopilot = autopilot_sdk.setup(app, client=FakeSDK(on_users={"u"}, flags=("f",)), sink=events.extend)

    @app.get("/boom")
    def boom():
        if flag_on("f"):
            raise ZeroDivisionError("division by zero")
        return {"ok": True}

    with TestClient(app, raise_server_exceptions=False) as client:
        client.get("/boom", headers={"X-User-Id": "u"})
        client.get("/boom", headers={"X-User-Id": "other"})
    autopilot.close()
    failed, fine = events
    assert failed["error_type"] == "ZeroDivisionError" and failed["error_message"] == "division by zero"
    assert failed["error_where"].endswith("in boom") and "test_engine.py:" in failed["error_where"]
    assert "error_type" not in fine