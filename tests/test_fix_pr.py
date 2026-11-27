"""Step 14: the fix PR. A real switch-off with a located fix -> the bot opens a
PR changing only the new version -> /approve -> merge -> the same flag restarts
from the internal stage. Real engine, handler, notifier, transformer and fix
checks; GitHub, Flagsmith and Groq are faked."""

import json

import pytest

from autopilot.fix_pr import FIX_PREFIX, FixError, FixPR, apply_fix
from autopilot.transformer import wrap
from autopilot.webhooks import parse_event
from tests.test_cleanup import CleanupEnv, CleanupGitHub
from tests.test_github import pr_payload
from tests.test_plan_flow import BASE, HEAD, comment
from tests.test_rollout_flow import FLAG, committed_line

BRANCH = f"{FIX_PREFIX}{FLAG}-1"


class FixGitHub(CleanupGitHub):
    def update_file(self, path, text, blob_sha, branch, message):
        sha = super().update_file(path, text, blob_sha, branch, message)
        if branch in self.branches:  # a commit on a bot branch is readable by its sha too
            self.set_head(sha, text)
        return sha


class FixEnv(CleanupEnv):
    def __init__(self):
        super().__init__()
        self.gh = FixGitHub()
        self.gh.current_sha = "sha1"
        self.engine.notify.gh = self.handler.gh = self.cleanup.gh = self.gh
        self.fixes = FixPR(self.store, self.gh, clock=self.clock, log=self.logs.append,
                           approvers=["yvssshubam"], since=0)
        self.handler.fixes = self.fixes

    def switch_off(self, fix_code="        total *= 0.95", line_text="        total *= 0.9", located=True):
        self.approve_and_merge()
        line = committed_line(self, line_text)
        self.llm.answers["diagnosis"] = {
            "likely_cause": "The discount multiplies the wrong way.",
            "evidence": [{"claim": "the discount line", "file": "demo_shop/checkout.py", "line": line}],
            "suggested_fix": {"description": "Use the intended rate.",
                              "file": "demo_shop/checkout.py" if located else None,
                              "start_line": line if located else None, "end_line": line if located else None,
                              "code": fix_code},
            "confidence": "medium"}
        assert self.stage(new=60, old=300, new_errors=20) == "rolled_back"
        self.original = self.release()

    def fix_release(self):
        return self.store.release_for_pr(41)

    def fix_event(self, action, sha=None, **kw):
        payload = pr_payload(action, number=41, **kw)
        payload["pull_request"]["head"]["ref"] = BRANCH
        payload["pull_request"]["head"]["sha"] = sha or self.fix_release_plan()["head_sha"]
        return parse_event("pull_request", payload)

    def fix_release_plan(self):
        return json.loads(self.fix_release()["plan"])


def opened():
    env = FixEnv()
    env.switch_off()
    env.fixes.tick()
    return env


# --- opening the fix PR ------------------------------------------------------------------------

def test_switch_off_with_a_located_fix_opens_a_fix_pr_changing_only_the_new_version():
    env = opened()
    fixed = env.gh.branches[BRANCH]
    assert "        total *= 0.95" in fixed and fixed.count("total *= 0.9\n") == 0
    assert fixed.split("def _calculate_total__new")[0] == env.gh.sources["main"].split("def _calculate_total__new")[0]
    pr = env.gh.pulls_opened[0]
    assert pr["branch"] == BRANCH and pr["base"] == "main" and "switched off in #7" in pr["title"]
    assert "**suggested by AI**" in pr["body"] and "stays **off for everyone**" in pr["body"]

    fix = env.fix_release()
    assert fix["state"] == "proposed" and fix["flag"] == FLAG and fix["feature_id"] == env.original["feature_id"]
    assert json.loads(fix["policy"]) == json.loads(env.original["policy"])  # same risk plan
    [(number, body)] = [(n, b) for n, b in env.gh.comments if n == 41]
    assert "What this fix changes" in body and "Only the new version" in body and "approve this fix" in body
    assert "stays **off for everyone** until this fix is approved" in body
    assert any(n == 7 and "Fix PR #41 opened" in b for n, b in env.gh.comments)
    assert json.loads(env.store.get_release(env.original["id"])["plan"])["fix_pr_number"] == 41
    assert env.fs.calls.count("create_feature") == 1  # no new flag
    env.fixes.tick()
    assert len(env.gh.pulls_opened) == 1  # at most one fix PR per switch-off


def test_the_fix_prs_own_events_are_not_triaged():
    env = opened()
    calls = list(env.llm.calls)
    env.send(env.fix_event("opened"))
    assert env.llm.calls == calls and env.fix_release()["state"] == "proposed"


# --- approve, merge, restart ---------------------------------------------------------------------

def approved_fix():
    env = opened()
    env.gh.pull["head"]["sha"] = env.fix_release_plan()["head_sha"]
    env.send(comment("/approve", number=41))
    return env


def test_approving_a_fix_creates_and_commits_nothing():
    env = approved_fix()
    assert env.fix_release()["state"] == "pending_merge"
    assert env.fs.calls.count("create_feature") == 1
    assert [c["branch"] for c in env.gh.commits].count("patch-1") == 1  # only the original wrapper commit
    assert "Fix approved by @yvssshubam" in env.gh.edits[-1][1]
    assert any(n == 41 and "when this PR is merged" in b for n, b in env.gh.comments)


def test_merging_the_fix_restarts_the_same_flag_from_the_internal_stage():
    env = approved_fix()
    old_segment = env.original["segment_id"]
    env.gh.sources["main"] = env.gh.branches[BRANCH]
    env.send(env.fix_event("closed", merged=True))
    fix = env.fix_release()
    assert fix["state"] == "stage_0" and fix["feature_id"] == env.original["feature_id"]
    assert "delete_segment" in env.fs.calls and old_segment is not None
    assert env.fs.override == fix["segment_id"] and env.fs.percent == 0 and env.fs.default_on is False
    assert any(n == 41 and "Rollout started" in b for n, b in env.gh.comments)
    assert env.store.latest_release(FLAG)["id"] == fix["id"]  # /approve and /rollback now act on the fix
    # It goes through the stages and needs the second approval like any release.
    from tests.test_engine import feed
    for _ in range(3):
        env.clock.now += 1
        feed(env.store, env.clock, FLAG, new=60, old=300)
        env.clock.now += 121
        env.engine.tick(fix["id"])
    assert env.fix_release()["state"] == "awaiting_approval"
    env.send(comment("/approve", number=41))
    env.engine.tick(fix["id"])
    assert env.fix_release()["state"] == "full" and env.fs.default_on is True


def test_closing_the_fix_pr_keeps_the_flag_off():
    env = opened()
    env.send(env.fix_event("closed"))
    assert env.fix_release()["state"] == "abandoned" and FLAG in env.fs.features
    assert env.fs.default_on is False
    assert any(n == 41 and "stays off for everyone" in b for n, b in env.gh.comments)


def test_merging_the_fix_without_approval_starts_nothing():
    env = opened()
    env.send(env.fix_event("closed", merged=True))
    assert env.fix_release()["state"] == "abandoned" and env.fs.default_on is False
    assert any(n == 41 and "**without an approved plan**" in b for n, b in env.gh.comments)


def test_approval_refused_when_the_flag_is_gone():
    env = opened()
    env.fs.features.clear()
    env.gh.pull["head"]["sha"] = env.fix_release_plan()["head_sha"]
    env.send(comment("/approve", number=41))
    assert env.fix_release()["state"] == "proposed"
    assert "isn't the flag the switched-off release used" in env.gh.comments[-1][1]


# --- new commits on a fix PR --------------------------------------------------------------------

def test_commits_that_only_change_the_new_version_are_rechecked():
    env = opened()
    env.gh.set_head("human1", env.gh.branches[BRANCH].replace("total *= 0.95", "total *= 0.97"))
    env.send(env.fix_event("synchronize", sha="human1"))
    assert env.fix_release_plan()["head_sha"] == "human1" and env.fix_release()["state"] == "proposed"
    assert "total *= 0.97" in env.gh.edits[-1][1]


def test_commits_that_touch_the_old_version_drop_the_plan():
    env = opened()
    tampered = env.gh.branches[BRANCH].replace("def _calculate_total__old(items):\n    total = 0.0",
                                               "def _calculate_total__old(items):\n    total = 1.0")
    env.gh.set_head("human2", tampered)
    env.send(env.fix_event("synchronize", sha="human2"))
    assert env.fix_release()["state"] == "abandoned"
    assert "change the old version or the switch" in env.gh.comments[-1][1]


# --- fixes that must not become a PR -----------------------------------------------------------------

@pytest.mark.parametrize("fix_code, line_text, reason", [
    ("    total *= 2", "    total = 0.0", "aren't inside the body of `_calculate_total__new`"),   # in the old version
    ("        total *= 0.9", "        total *= 0.9", "doesn't change what the new version does"),
    ("        total *= (0.9", "        total *= 0.9", "isn't valid Python"),
])
def test_unsafe_fixes_are_refused_once(fix_code, line_text, reason):
    env = FixEnv()
    env.switch_off(fix_code=fix_code, line_text=line_text)
    env.fixes.tick()
    env.fixes.tick()
    assert env.gh.pulls_opened == [] and env.store.release_for_pr(41) is None
    skipped = [b for n, b in env.gh.comments if "No fix PR" in b]
    assert len(skipped) == 1 and reason in skipped[0]


def test_fix_without_a_location_is_skipped_quietly():
    env = FixEnv()
    env.switch_off(located=False)
    env.fixes.tick()
    assert env.gh.pulls_opened == [] and not any("No fix PR" in b for _, b in env.gh.comments)


def test_main_moved_since_the_diagnosis():
    env = FixEnv()
    env.switch_off()
    env.gh.sources["main"] = env.gh.sources["main"].replace("        total *= 0.9", "        total *= 0.8")
    env.fixes.tick()
    assert env.gh.pulls_opened == []
    assert "changed on main since the diagnosis" in [b for _, b in env.gh.comments if "No fix PR" in b][0]


def test_old_switch_offs_get_no_fix_pr():
    env = FixEnv()
    env.switch_off()
    env.fixes.since = env.clock() + 1
    env.fixes.tick()
    assert env.gh.pulls_opened == []


# --- apply_fix on its own ---------------------------------------------------------------------------

def test_apply_fix_checks():
    wrapped = wrap(BASE, HEAD, "calculate_total", FLAG)
    line = wrapped.split("\n").index("        total *= 0.9") + 1
    fix = {"start": line, "end": line, "code": "        total *= 0.95"}
    fixed = apply_fix(wrapped, wrapped, fix, "calculate_total", FLAG)
    assert fixed.split("\n")[line - 1] == "        total *= 0.95"
    with pytest.raises(FixError, match="aren't in the file"):
        apply_fix(wrapped, wrapped, dict(fix, start=0), "calculate_total", FLAG)
    with pytest.raises(FixError, match="isn't on main"):
        apply_fix(HEAD, HEAD, dict(fix, start=2, end=2), "calculate_total", FLAG)
    switch = wrapped.split("\n").index(f'    if flag_on("{FLAG}"):') + 1
    with pytest.raises(FixError, match="aren't inside the body"):
        apply_fix(wrapped, wrapped, {"start": switch, "end": switch, "code": "    if True:"}, "calculate_total", FLAG)