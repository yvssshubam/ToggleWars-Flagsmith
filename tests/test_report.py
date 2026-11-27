"""Step 13: the release report, built from the event log of real flows (engine,
handler, cleanup, notifier); GitHub, Flagsmith and Groq are faked."""

from autopilot.llm import LLMError
from autopilot.report import DIAGNOSIS_WAIT_SECONDS, ReleaseReporter, build, facts
from tests.test_cleanup import CleanupEnv
from tests.test_experiment import shoppers
from tests.test_plan_flow import comment
from tests.test_rollout_flow import Env, committed_line


def completed_env(dashboard=False):
    env = CleanupEnv()
    env.approve_and_merge()
    shoppers(env, 720, 410, 2900, 1450)
    for _ in range(3):
        env.stage()
    if dashboard:
        env.engine.approve(env.release()["flag"], by="dashboard")
    else:
        env.send(comment("/approve"))
    env.engine.tick(env.release()["id"])
    env.stage(old=0)
    env.cleanup.tick()
    env.merge_cleanup()
    assert env.release()["state"] == "completed"
    return env


def switched_off_env(diagnosis=True):
    env = Env()
    env.approve_and_merge()
    if diagnosis:
        line = committed_line(env, "        total *= 0.9")
        env.llm.answers["diagnosis"] = {
            "likely_cause": "Divides by zero.", "evidence": [{"claim": "x", "file": "demo_shop/checkout.py", "line": line}],
            "suggested_fix": {"description": "Guard.", "file": None, "start_line": None, "end_line": None, "code": None},
            "confidence": "high"}
    env.stage(new=60, old=300, new_errors=20)
    assert env.release()["state"] == "rolled_back"
    return env


# --- what the report says ------------------------------------------------------------------

def test_completed_release_report_covers_everything():
    env = completed_env()
    body = build(env.store, env.release())
    assert body.startswith("## 📋 Release report: `ap_big_order_discount`")
    assert "**Outcome:** ✅ released to everyone and cleaned up (high risk plan). Merge to everyone: **" in body
    assert "| 1 | Release plan | @yvssshubam |" in body and "| 2 | Release to everyone | @yvssshubam |" in body
    assert "| 3 | Cleanup PR #41 | merged on GitHub |" in body
    assert "| internal only |" in body and "| internal + 5% |" in body and "| internal + 25% |" in body
    assert "| everyone (100%) |" in body and "(old path off)" in body
    assert "conversion new 56.9% vs old 50.0%: +6.9 pp" in body and "not by Flagsmith Experimentation" in body
    assert "Widened it 2 times in place (internal + 5%, internal + 25%)" in body
    assert "Deployed the cleanup, then deleted the flag and its segment from Flagsmith" in body
    assert "Release plan proposed on the PR" in body and "Flag and segment deleted; done" in body


def test_dashboard_approval_is_named_as_such():
    env = completed_env(dashboard=True)
    body = build(env.store, env.release())
    assert "| 2 | Release to everyone | the dashboard |" in body and "dashboard button" in body


def test_switched_off_report():
    env = switched_off_env()
    f = facts(env.store, env.release())
    assert [row[3] for row in f["health"]] == ["switched off"]
    body = build(env.store, env.release())
    assert "**Outcome:** 🛑 switched off for everyone (high risk plan). Reason: new-path error rate 33.3%" in body
    assert "| internal only | 60 requests, 33.3% errors" in body
    assert "Not measured: the release stopped before the comparison at 25%." in body
    assert "Switched the flag off for everyone by the approved rule" in body
    assert "Posted an AI diagnosis" in body and "suggested fix are in the switch-off comment above" in body
    assert "Release to everyone" not in body


# --- the AI's two sentences -------------------------------------------------------------------

def test_ai_opening_is_used_when_it_has_no_numbers():
    env = completed_env()
    env.llm.answers["release_summary"] = {"summary": "The discount reached everyone. It went smoothly."}
    body = build(env.store, env.release(), env.llm)
    assert "The discount reached everyone. It went smoothly." in body and "AI-written, checked" in body


def test_ai_opening_with_numbers_or_failure_falls_back_to_code():
    env = completed_env()
    env.llm.answers["release_summary"] = {"summary": "Conversion rose by 7 points."}
    body = build(env.store, env.release(), env.llm)
    assert "rose by 7" not in body and "was released to everyone in stages" in body and "written by code" in body
    env.llm.answers["release_summary"] = LLMError("rate limit")
    assert "written by code" in build(env.store, env.release(), env.llm)


# --- when it's posted ------------------------------------------------------------------------------

def test_posted_once_when_a_release_completes():
    env = completed_env()
    reporter = ReleaseReporter(env.store, env.gh, clock=env.clock, log=env.logs.append, since=0)
    reporter.tick()
    reporter.tick()
    reports = [b for _, b in env.gh.comments if b.startswith("## 📋 Release report")]
    assert len(reports) == 1
    assert any(e["kind"] == "report_posted" for e in env.store.events(env.release()["id"]))


def test_old_releases_get_no_automatic_report():
    env = completed_env()
    ReleaseReporter(env.store, env.gh, clock=env.clock, since=env.clock() + 1).tick()
    assert not any(b.startswith("## 📋 Release report") for _, b in env.gh.comments)


def test_switch_off_waits_for_the_diagnosis():
    env = Env()
    env.approve_and_merge()
    env.llm.answers["diagnosis"] = LLMError("slow")  # the diagnosis call fails -> "unavailable" is still recorded
    reporter = ReleaseReporter(env.store, env.gh, clock=env.clock, since=0)
    env.stage(new=60, old=300, new_errors=20)
    reporter.tick()
    assert any(b.startswith("## 📋 Release report") for _, b in env.gh.comments)  # diagnosis event exists


def test_switch_off_without_diagnosis_posts_after_the_wait():
    env = Env()
    env.approve_and_merge()
    reporter = ReleaseReporter(env.store, env.gh, clock=env.clock, since=0)
    env.engine.notify = None  # no notifier: no switch-off comment and no diagnosis
    env.stage(new=60, old=300, new_errors=20)
    reporter.tick()
    assert not any(b.startswith("## 📋 Release report") for _, b in env.gh.comments)
    env.clock.now += DIAGNOSIS_WAIT_SECONDS + 1
    reporter.tick()
    assert any(b.startswith("## 📋 Release report") for _, b in env.gh.comments)


def test_manual_rollback_report_is_immediate():
    env = Env()
    env.approve_and_merge()
    reporter = ReleaseReporter(env.store, env.gh, clock=env.clock, since=0)
    env.send(comment("/rollback"))
    env.engine.tick(env.release()["id"])
    reporter.tick()
    [body] = [b for _, b in env.gh.comments if b.startswith("## 📋 Release report")]
    assert "manual rollback by @yvssshubam" in body


def test_github_error_is_retried_later():
    from autopilot.github_client import GitHubError
    env = completed_env()
    reporter = ReleaseReporter(env.store, env.gh, clock=env.clock, log=env.logs.append, since=0)
    real = env.gh.post_comment
    env.gh.post_comment = lambda *a: (_ for _ in ()).throw(GitHubError("comment", 502, "x"))
    reporter.tick()
    env.gh.post_comment = real
    reporter.tick()  # too soon
    assert not any(b.startswith("## 📋 Release report") for _, b in env.gh.comments)
    env.clock.now += 61
    reporter.tick()
    assert sum(b.startswith("## 📋 Release report") for _, b in env.gh.comments) == 1


def test_request_count_stops_when_the_gates_stop():
    env = completed_env()
    counted = facts(env.store, env.release())["actions"]
    [line] = [a for a in counted if a.startswith("Checked the health gates")]
    released = next(e["ts"] for e in env.store.events(env.release()["id"]) if e["kind"] == "released")
    rows = [r for r in env.store.metrics_since(env.release()["flag"], 0) if r["ts"] <= released]
    assert f"over {len(rows)} requests" in line
    env.clock.now += 5  # traffic after the release isn't counted
    from tests.test_engine import feed
    feed(env.store, env.clock, env.release()["flag"], new=500, old=0)
    assert [a for a in facts(env.store, env.release())["actions"] if a.startswith("Checked")] == [line]