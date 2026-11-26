"""Step 9c: merge -> rollout announced on the PR -> /approve -> released, and
the switch-off with its diagnosis. Real engine, health gates, policy, store,
transformer, PR handler and notifier; GitHub, Flagsmith and Groq are faked."""

import pytest

from autopilot.engine import Engine
from autopilot.github_client import GitHubError
from autopilot.llm import LLMError
from autopilot.pr_handler import PRHandler, git_pull
from autopilot.pr_notifier import PRNotifier
from autopilot.store import Store
from autopilot.webhooks import parse_event
from tests.test_engine import Clock, feed
from tests.test_engine import FakeFlagsmith as EngineFakeFlagsmith
from tests.test_github import pr_payload
from tests.test_plan_flow import PROSE, FakeGitHub, FakeLLM, comment, opened, send, triage_answer

FLAG = "ap_big_order_discount"


class FakeFlagsmith(EngineFakeFlagsmith):
    def list_features(self):
        return [{"name": n, "id": i} for n, i in self.features.items()]


class Env:
    def __init__(self, diagnosis=None, deploy_ok=True):
        self.clock = Clock()
        self.store = Store(":memory:", clock=self.clock)
        self.gh, self.fs, self.logs = FakeGitHub(), FakeFlagsmith(), []
        self.gh.current_sha = "sha1"
        self.llm = FakeLLM(triage_answer(), PROSE, diagnosis)
        self.engine = Engine(self.store, self.fs, clock=self.clock, log=self.logs.append,
                             notify=PRNotifier(self.store, self.gh, self.llm, log=self.logs.append))
        self.deploys = []

        def deploy():
            self.deploys.append(1)
            return deploy_ok, "Fast-forward" if deploy_ok else "error: Your local changes would be overwritten"
        self.handler = PRHandler(self.store, self.gh, ["yvssshubam"], llm=self.llm, fs=self.fs,
                                 engine=self.engine, deploy=deploy, log=self.logs.append)

    def send(self, *events):
        send(self.store, self.handler, *events)

    def approve_and_merge(self):
        self.send(opened(), comment("/approve"))
        assert self.release()["state"] == "pending_merge"
        self.gh.sources["main"] = self.gh.sources[self.gh.current_sha]  # the merge lands on main
        self.send(parse_event("pull_request", pr_payload("closed", merged=True)))

    def release(self):
        return self.store.release_for_pr(7)

    def stage(self, new=60, old=300, new_errors=0, latency=5.0):
        """Traffic for one stage, then past the soak, then one engine tick."""
        self.clock.now += 1
        feed(self.store, self.clock, FLAG, new=new, old=old, new_errors=new_errors, latency=latency)
        self.clock.now += 121  # past the high-risk soak
        self.engine.tick(self.release()["id"])
        return self.release()["state"]

    def bodies(self):
        return [b for _, b in self.gh.comments]


# --- the healthy path -------------------------------------------------------------------------

def test_merge_deploys_and_starts_stage_0_on_the_existing_flag():
    env = Env()
    env.approve_and_merge()
    release = env.release()
    assert release["state"] == "stage_0" and env.deploys == [1]
    assert release["feature_id"] == 42 and release["segment_id"] == 7
    assert env.fs.calls.count("create_feature") == 1  # created at /approve, not again on merge
    assert env.fs.override == 7 and env.fs.percent == 0 and env.fs.default_on is False
    bodies = env.bodies()
    assert "Merged. **Deploy:** `git pull` in the shop's folder succeeded." in bodies[-2]
    assert "**Rollout started** for `ap_big_order_discount` (high risk plan)" in bodies[-1]
    assert "internal only" in bodies[-1] and "120s" in bodies[-1] and "50 new-path requests" in bodies[-1]


def test_full_healthy_release_with_the_second_approval_on_the_pr():
    env = Env()
    env.approve_and_merge()
    assert env.stage() == "stage_1" and env.fs.percent == 5
    assert "**Stage 1:** new code on for **internal + 5%**" in env.bodies()[-1]
    assert "new path 60 requests, 0.0% errors, p95 5 ms" in env.bodies()[-1]
    env.send(comment("/approve"))  # too early
    assert "can only be approved once all stages are healthy" in env.bodies()[-1]
    assert env.stage() == "stage_2" and env.fs.percent == 25
    assert env.stage() == "awaiting_approval"
    assert "Release to everyone?" in env.bodies()[-1] and "`/approve`" in env.bodies()[-1]

    env.send(comment("/approve", author="stranger"))
    assert "isn't on the approver list" in env.bodies()[-1]
    env.send(comment("/approve"))
    assert "approved: the new code goes on for everyone" in env.bodies()[-1]
    env.engine.tick(env.release()["id"])
    assert env.release()["state"] == "full" and env.fs.default_on is True and env.fs.override is None
    assert "**Released to everyone**, approved by @yvssshubam" in env.bodies()[-1]
    assert env.stage(old=0) == "released"
    assert "🎉 **Released.**" in env.bodies()[-1]


def test_deploy_failure_is_reported_and_the_rollout_waits_for_traffic():
    env = Env(deploy_ok=False)
    env.approve_and_merge()
    assert "**failed**" in env.bodies()[-2] and "local changes would be overwritten" in env.bodies()[-2]
    assert env.release()["state"] == "stage_0"
    assert env.stage(new=0, old=0) == "stage_0"  # no new-path traffic yet: waits, never advances


# --- the switch-off and the diagnosis -----------------------------------------------------------

def committed_line(env, text):
    return env.gh.sources["main"].split("\n").index(text) + 1


def test_switch_off_posts_numbers_then_a_checked_diagnosis():
    env = Env()
    env.approve_and_merge()
    line = committed_line(env, "        total *= 0.9")
    env.llm.answers["diagnosis"] = {
        "likely_cause": "The new discount branch in `_calculate_total__new` fails for some carts.",
        "evidence": [{"claim": "The discount line is only on the new path", "file": "demo_shop/checkout.py",
                      "line": line},
                     {"claim": "Made up", "file": "demo_shop/checkout.py", "line": 999}],
        "suggested_fix": {"description": "Guard the discount.", "file": "demo_shop/checkout.py",
                          "start_line": line, "end_line": line, "code": "        total = total * 0.9"},
        "confidence": "medium"}
    assert env.stage(new=60, old=300, new_errors=20) == "rolled_back"
    assert env.fs.default_on is False and env.fs.override is None
    comment_id, body = env.gh.edits[-1]
    original = [b for _, b in env.gh.comments if "Switched off" in b][0]
    assert "_Diagnosis: working on it…_" in original  # numbers went out before the AI call
    assert "🛑 **Switched off.**" in body and "off for everyone" in body
    assert "| new | 60 requests, 33.3% errors, p95 5 ms |" in body and "| old | 300 requests, 0.0% errors" in body
    assert "20 × `RuntimeError` at `demo_shop/checkout.py:30 in _calculate_total__new`" in body
    assert "### Diagnosis (AI, every cited line checked" in body
    assert f"checkout.py:{line}" in body and "Made up" not in body
    assert "**Confidence:** medium" in body and "total = total * 0.9" in body
    assert "working on it" not in body
    assert env.llm.calls.count("diagnosis") == 1


def test_diagnosis_unavailable_keeps_the_numbers():
    env = Env(diagnosis=LLMError("Groq rate limit"))
    env.approve_and_merge()
    assert env.stage(new=60, old=300, new_errors=20) == "rolled_back"
    body = env.gh.edits[-1][1]
    assert "| new | 60 requests, 33.3% errors" in body and "Diagnosis unavailable (Groq rate limit)" in body


def test_manual_rollback_from_the_pr_has_no_diagnosis():
    env = Env()
    env.approve_and_merge()
    env.stage()
    env.send(comment("/rollback"))
    assert "switch-off requested" in env.bodies()[-1]
    env.engine.tick(env.release()["id"])
    assert env.release()["state"] == "rolled_back" and env.fs.default_on is False
    assert "manual rollback by @yvssshubam" in env.bodies()[-1] and "Diagnosis" not in env.bodies()[-1]
    assert "diagnosis" not in env.llm.calls


def test_github_down_never_stops_a_switch_off():
    env = Env()
    env.approve_and_merge()

    def broken(*a, **k):
        raise GitHubError("comment", 502, "bad gateway")
    env.gh.post_comment = broken
    assert env.stage(new=60, old=300, new_errors=20) == "rolled_back"
    assert env.fs.default_on is False
    assert any("couldn't announce rolled_back" in line for line in env.logs)


# --- merges that must not start a rollout ---------------------------------------------------------

def test_merge_with_a_weakened_fallback_is_blocked():
    env = Env()
    env.send(opened(), comment("/approve"))
    merged = env.gh.sources[env.gh.current_sha].replace(
        "def _calculate_total__old(items):\n    total = 0.0", "def _calculate_total__old(items):\n    total = 1.0")
    env.gh.sources["main"] = merged
    env.send(parse_event("pull_request", pr_payload("closed", merged=True)))
    assert env.release()["state"] == "error" and env.fs.override is None and env.deploys == []
    assert "Rollout not started" in env.bodies()[-1] and "isn't the approved old version" in env.bodies()[-1]


def test_merge_with_the_switch_removed_is_blocked():
    env = Env()
    env.send(opened(), comment("/approve"))
    env.gh.sources["main"] = env.gh.sources["sha1"]  # someone force-pushed the unwrapped code
    env.send(parse_event("pull_request", pr_payload("closed", merged=True)))
    assert env.release()["state"] == "error"
    assert "wrapper isn't there any more" in env.bodies()[-1]


def test_change_to_the_new_version_after_approval_is_allowed():
    env = Env()
    env.send(opened(), comment("/approve"))
    env.gh.sources["main"] = env.gh.sources[env.gh.current_sha].replace("total *= 0.9", "total *= 0.95")
    env.send(parse_event("pull_request", pr_payload("closed", merged=True)))
    assert env.release()["state"] == "stage_0"


def test_merge_before_approval_finished_is_blocked():
    env = Env()
    env.gh.fail_commits = 5
    env.send(opened(), comment("/approve"))
    assert env.release()["state"] == "approving"
    env.send(parse_event("pull_request", pr_payload("closed", merged=True)))
    assert env.release()["state"] == "error" and env.fs.override is None


def test_git_pull_reports_failure_outside_a_repo(tmp_path):
    ok, output = git_pull(str(tmp_path))
    assert ok is False and output


@pytest.mark.parametrize("state", ["stage_0", "awaiting_approval"])
def test_rollback_command_in_any_rollout_state(state):
    env = Env()
    env.approve_and_merge()
    env.store.update_release(env.release()["id"], state=state)
    env.send(comment("/rollback"))
    assert env.release()["rollback_requested_by"] == "@yvssshubam"