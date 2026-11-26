"""Offline tests for autopilot_sdk and the demo shop, with a fake Flagsmith SDK."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import autopilot_sdk
from autopilot_sdk import flag_on
from autopilot_sdk.flags import set_client
from demo_shop import app as shop_module
from demo_shop.app import create_app
from tests.sample_checkout import plain_calculate_total, wrapped_calculate_total

# The shop runs with a fixed copy of the pricing code wrapped behind a flag
# (tests/sample_checkout.py), so these tests don't depend on demo_shop/checkout.py,
# which every demo PR changes.


@pytest.fixture(autouse=True)
def flagged_checkout(monkeypatch):
    monkeypatch.setattr(shop_module, "calculate_total", wrapped_calculate_total())


class FakeFlags:
    def __init__(self, enabled):
        self.enabled = enabled

    def is_feature_enabled(self, name):
        if name not in self.enabled:
            raise KeyError(name)  # the real SDK raises for a missing flag
        return self.enabled[name]


class FakeSDK:
    """New path for internal users and for user ids listed in `on_users`."""

    def __init__(self, on_users=(), flags=("ap_checkout_v2",), loaded=True):
        self.on_users, self.flags, self.calls = set(on_users), flags, []
        self._local_evaluation = object() if loaded else None

    def get_identity_flags(self, identifier, traits):
        self.calls.append((identifier, traits))
        on = traits["is_internal"] or identifier in self.on_users
        return FakeFlags({f: on for f in self.flags})


@pytest.fixture
def shop():
    events = []
    sdk = FakeSDK(on_users={"lucky"})
    app = create_app(client=sdk, sink=events.extend, metrics_url=None)
    with TestClient(app) as client:
        yield client, sdk, events
    set_client(None)


def checkout(client, user=None, internal=False, items=None):
    headers = {}
    if user:
        headers["X-User-Id"] = user
    if internal:
        headers["X-Internal"] = "true"
    return client.post("/checkout", json={"items": items or [{"sku": "beans", "qty": 2}]},
                       headers=headers)


def test_regular_user_gets_old_path(shop):
    client, sdk, _ = shop
    r = checkout(client, user="u1")
    assert r.status_code == 200 and r.json() == {"total": 36.0}  # no discount
    assert sdk.calls == [("u1", {"is_internal": False})]


def test_internal_header_sets_trait_and_new_path(shop):
    client, sdk, _ = shop
    r = checkout(client, user="staff-1", internal=True)
    assert r.json() == {"total": 32.4}  # 10% off 36.00
    assert sdk.calls == [("staff-1", {"is_internal": True})]


def test_x_internal_must_be_exactly_true(shop):
    client, sdk, _ = shop
    client.post("/checkout", json={"items": [{"sku": "tea", "qty": 1}]},
                headers={"X-User-Id": "u2", "X-Internal": "yes"})
    assert sdk.calls == [("u2", {"is_internal": False})]


def test_no_user_id_means_old_path_and_no_lookup(shop):
    client, sdk, _ = shop
    assert checkout(client).json() == {"total": 36.0}
    assert sdk.calls == []


def _reporter(client):
    return client.app.state.autopilot.reporter


def test_events_report_path_ok_and_latency(shop):
    client, _, events = shop
    checkout(client, user="lucky")
    checkout(client, user="u3")
    _reporter(client).flush()  # reporter sends on a timer; force it
    assert [(e["flag"], e["path"], e["ok"]) for e in events] == [
        ("ap_checkout_v2", "new", True), ("ap_checkout_v2", "old", True)]
    assert all(e["latency_ms"] > 0 and e["ts"] > 0 for e in events)


def test_client_error_is_still_ok(shop):
    client, _, events = shop
    r = checkout(client, user="u4", items=[{"sku": "unicorn", "qty": 1}])
    assert r.status_code == 400
    _reporter(client).flush()
    assert events[-1]["ok"] is True


def test_server_error_reports_not_ok():
    events = []
    app = FastAPI()
    autopilot = autopilot_sdk.setup(app, client=FakeSDK(on_users={"u"}, flags=("f",)),
                                    sink=events.extend)

    @app.get("/boom")
    def boom():
        if flag_on("f"):
            raise RuntimeError("new path is broken")
        return {"ok": True}

    with TestClient(app, raise_server_exceptions=False) as client:
        assert client.get("/boom", headers={"X-User-Id": "u"}).status_code == 500
        assert client.get("/boom", headers={"X-User-Id": "other"}).status_code == 200
    autopilot.close()
    assert [(e["path"], e["ok"]) for e in events] == [("new", False), ("old", True)]


def test_plain_shop_has_no_flag(monkeypatch):
    monkeypatch.setattr(shop_module, "calculate_total", plain_calculate_total())
    events, sdk = [], FakeSDK(on_users={"u"})
    app = create_app(client=sdk, sink=events.extend, metrics_url=None)
    with TestClient(app) as client:
        assert checkout(client, user="u").json() == {"total": 36.0}
        client.app.state.autopilot.reporter.flush()
    set_client(None)
    assert sdk.calls == [] and events == []  # no flag, no lookups, nothing to report


def test_flag_missing_or_sdk_not_loaded_falls_back_to_old():
    for sdk in (FakeSDK(on_users={"u"}, flags=()), FakeSDK(on_users={"u"}, loaded=False)):
        app = FastAPI()
        autopilot = autopilot_sdk.setup(app, client=sdk, sink=lambda b: None)

        @app.get("/x")
        def x():
            return {"on": flag_on("ap_checkout_v2")}

        with TestClient(app) as client:
            assert client.get("/x", headers={"X-User-Id": "u"}).json() == {"on": False}
        autopilot.close()


def test_one_answer_per_flag_per_request():
    sdk = FakeSDK(on_users={"u"})
    app = FastAPI()
    autopilot = autopilot_sdk.setup(app, client=sdk, sink=lambda b: None)

    @app.get("/twice")
    def twice():
        return [flag_on("ap_checkout_v2"), flag_on("ap_checkout_v2")]

    with TestClient(app) as client:
        assert client.get("/twice", headers={"X-User-Id": "u"}).json() == [True, True]
    autopilot.close()
    assert len(sdk.calls) == 1


def test_flag_on_outside_a_request_is_false():
    assert flag_on("ap_checkout_v2") is False


def test_setup_rejects_client_side_key():
    with pytest.raises(ValueError, match="ser."):
        autopilot_sdk.setup(FastAPI(), server_key="YVBbF2u3wfxhGLS8StCbhU")


def test_closing_an_old_app_keeps_the_newer_client():
    import autopilot_sdk.flags as flags
    first = autopilot_sdk.setup(FastAPI(), client=FakeSDK(), sink=lambda b: None)
    newer_sdk = FakeSDK()
    second = autopilot_sdk.setup(FastAPI(), client=newer_sdk, sink=lambda b: None)
    first.close()
    assert flags._client is newer_sdk
    second.close()
    assert flags._client is None