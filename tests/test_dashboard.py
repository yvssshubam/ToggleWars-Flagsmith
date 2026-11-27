"""Step 11: the dashboard. Data and buttons against a real rollout (real engine,
store, health gates, notifier); GitHub, Flagsmith and Groq are faked."""

import pytest
from fastapi.testclient import TestClient

from autopilot.server import create_app
from tests.test_github import REPO
from tests.test_rollout_flow import FLAG, Env

TOKEN = "test-token"


def client_for(env, engine=True):
    app = create_app(env.store, github_repo=REPO, engine=env.engine if engine else None,
                     dashboard_token=TOKEN, clock=env.clock, log=env.logs.append)
    return TestClient(app, base_url="http://localhost:8000")


def press(client, release_id, action, token=TOKEN):
    return client.post(f"/api/releases/{release_id}/{action}", headers={"X-Dashboard-Token": token})


# --- the page and the empty state ----------------------------------------------------------

def test_page_is_served_locally_with_a_fresh_token():
    env = Env()
    client = client_for(env)
    page = client.get("/dashboard")
    assert page.status_code == 200 and "Release Autopilot" in page.text
    assert f'const TOKEN = "{TOKEN}"' in page.text and "__DASHBOARD_TOKEN__" not in page.text
    assert REPO in page.text and "No releases yet" in page.text  # the empty state is in the page
    assert client.get("/").status_code == 200
    assert client.get("/api/releases").json() == []


def test_random_token_when_none_given():
    env = Env()
    app = create_app(env.store, engine=env.engine)
    page = TestClient(app, base_url="http://127.0.0.1:8000").get("/dashboard").text
    token = page.split('const TOKEN = "')[1].split('"')[0]
    assert len(token) >= 24


@pytest.mark.parametrize("headers, base", [
    ({"X-Forwarded-For": "203.0.113.9"}, "http://localhost:8000"),          # through ngrok
    ({}, "https://fiftieth-overprice-follicle.ngrok-free.dev"),            # public host name
    ({}, "http://evil.example:8000"),                                      # DNS rebinding
    ({"Forwarded": "for=203.0.113.9"}, "http://127.0.0.1:8000"),
])
def test_dashboard_is_invisible_from_outside(headers, base):
    env = Env()
    env.approve_and_merge()
    app = create_app(env.store, github_repo=REPO, engine=env.engine, dashboard_token=TOKEN, clock=env.clock)
    client = TestClient(app, base_url=base)
    rid = env.release()["id"]
    assert client.get("/dashboard", headers=headers).status_code == 404
    assert client.get("/api/releases", headers=headers).status_code == 404
    assert client.get(f"/api/releases/{rid}", headers=headers).status_code == 404
    r = client.post(f"/api/releases/{rid}/switch-off", headers={**headers, "X-Dashboard-Token": TOKEN})
    assert r.status_code == 404 and env.release()["rollback_requested_by"] is None
    assert client.get("/health", headers=headers).status_code == 200  # the rest of the bot still answers


# --- what the page shows ---------------------------------------------------------------------

def test_live_stage_shows_the_engines_own_verdict():
    env = Env()
    env.approve_and_merge()
    env.clock.now += 1
    from tests.test_engine import feed
    feed(env.store, env.clock, FLAG, new=30, old=200)
    env.clock.now += 20
    client = client_for(env)
    [r] = client.get("/api/releases").json()
    assert r["flag"] == FLAG and r["state"] == "stage_0" and r["state_text"] == "rolling out: internal only"
    assert r["steps"][r["step"]] == "Internal" and r["tier"] == "high"
    assert r["pr_url"] == f"https://github.com/{REPO}/pull/7" and r["title"] == "Discount"
    gate = r["gate"]
    assert gate["status"] == "wait" and "30/50" in gate["reason"]  # same words the engine logs
    assert gate["new"]["n"] == 30 and gate["old"]["n"] == 200 and gate["soak"] == 120 and gate["elapsed"] == 21
    assert r["can_switch_off"] and not r["can_approve"]


def test_detail_has_series_markers_and_events():
    env = Env()
    env.approve_and_merge()
    env.stage()           # stage 0 -> stage 1
    env.stage()           # stage 1 -> stage 2
    detail = client_for(env).get(f"/api/releases/{env.release()['id']}").json()
    assert [m["label"] for m in detail["markers"]] == ["internal", "5%", "25%"]
    points = [p for p in detail["series"] if p["new"]]
    assert points and points[0]["new"]["error_rate"] == 0.0 and points[0]["old"]["n"] > 0
    kinds = [e["kind"] for e in detail["events"]]
    assert kinds[:3] == ["proposed", "plan_approved", "flag_created"] and kinds.count("stage") == 3


def test_switched_off_release_shows_where_it_stopped():
    env = Env()
    env.approve_and_merge()
    assert env.stage(new=60, old=300, new_errors=20) == "rolled_back"
    [r] = client_for(env).get("/api/releases").json()
    assert r["stopped"] and r["steps"][r["step"]] == "Internal" and r["gate"] is None
    assert "13.2%" not in (r["rollback_reason"] or "") and "error rate" in r["rollback_reason"]
    assert not r["can_switch_off"] and not r["can_approve"]


def test_cli_release_has_no_pr_link():
    env = Env()
    env.store.create_release("ap_cli", 1, 2, {"tier": "low", "soak_seconds": 30, "min_new_requests": 20,
                                              "max_error_increase_pp": 3, "max_error_rate_pct": 5,
                                              "max_p95_ratio": 2.0}, "released")
    [r] = client_for(env).get("/api/releases").json()
    assert r["pr_number"] is None and r["pr_url"] is None and r["state_text"] == "released"


# --- the two buttons ----------------------------------------------------------------------------

def to_awaiting_approval(env):
    env.approve_and_merge()
    for _ in range(3):
        env.stage()
    assert env.release()["state"] == "awaiting_approval"
    return env.release()["id"]


def test_approve_button_releases_to_everyone_and_is_recorded_on_the_pr():
    env = Env()
    rid = to_awaiting_approval(env)
    client = client_for(env)
    assert client.get(f"/api/releases/{rid}").json()["can_approve"]
    assert press(client, rid, "approve", token="").status_code == 403
    assert press(client, rid, "approve", token="wrong").status_code == 403
    assert env.release()["approved_by"] is None
    r = press(client, rid, "approve")
    assert r.status_code == 200 and r.json()["ok"] and "goes on for everyone" in r.json()["message"]
    env.engine.tick(rid)
    assert env.release()["state"] == "full" and env.fs.default_on is True
    assert "approved by the dashboard" in env.bodies()[-1]
    again = press(client, rid, "approve")
    assert again.status_code == 409 and "already approved by dashboard" in again.json()["message"]


def test_switch_off_button_switches_off_and_is_recorded_on_the_pr():
    env = Env()
    env.approve_and_merge()
    rid = env.release()["id"]
    r = press(client_for(env), rid, "switch-off")
    assert r.status_code == 200 and "Switch-off requested" in r.json()["message"]
    env.engine.tick(rid)
    assert env.release()["state"] == "rolled_back" and env.fs.default_on is False
    assert "manual rollback by the dashboard" in env.bodies()[-1]


def test_button_edge_cases():
    env = Env()
    env.approve_and_merge()
    rid = env.release()["id"]
    client = client_for(env)
    assert press(client, rid, "approve").status_code == 409          # not waiting for approval yet
    assert press(client, rid, "delete-everything").status_code == 404
    assert press(client, 999, "switch-off").status_code == 404
    assert press(client_for(env, engine=False), rid, "switch-off").status_code == 503
    env.store.create_release(FLAG, 1, 2, {"tier": "low", "soak_seconds": 30, "min_new_requests": 20,
                                          "max_error_increase_pp": 3, "max_error_rate_pct": 5,
                                          "max_p95_ratio": 2.0}, "stage_0")
    assert press(client, rid, "switch-off").status_code == 409        # a newer release of the flag exists


def test_switch_off_shows_the_ai_diagnosis_gist():
    from tests.test_rollout_flow import committed_line
    env = Env()
    env.approve_and_merge()
    line = committed_line(env, "        total *= 0.9")
    env.llm.answers["diagnosis"] = {
        "likely_cause": "The new branch divides by `(3 - units)`.",
        "evidence": [{"claim": "Division", "file": "demo_shop/checkout.py", "line": line}],
        "suggested_fix": {"description": "Guard it.", "file": None, "start_line": None, "end_line": None, "code": None},
        "confidence": "high"}
    env.stage(new=60, old=300, new_errors=20)
    detail = client_for(env).get(f"/api/releases/{env.release()['id']}").json()
    assert detail["diagnosis"] == {"cause": "The new branch divides by `(3 - units)`.", "confidence": "high"}
    [event] = [e for e in detail["events"] if e["kind"] == "diagnosis"]
    assert event["message"] == "AI diagnosis: The new branch divides by `(3 - units)`. (confidence high)"


def test_unavailable_diagnosis_is_shown_as_such():
    from autopilot.llm import LLMError
    env = Env(diagnosis=LLMError("Groq rate limit"))
    env.approve_and_merge()
    env.stage(new=60, old=300, new_errors=20)
    [r] = client_for(env).get("/api/releases").json()
    assert r["diagnosis"]["cause"].startswith("Diagnosis unavailable (Groq rate limit)")