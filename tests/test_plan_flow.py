"""Step 9a: PR opened -> triage -> release plan comment, plus /set, /reject,
new commits, and closing. GitHub and Groq are faked; the transformer, the
AI-answer checks, the policy and the store are the real ones."""

import difflib
import json
import sqlite3
from pathlib import Path

from autopilot.pr_handler import PRHandler
from autopilot.store import Store
from autopilot.webhooks import parse_event
from tests.test_github import REPO, comment_payload, pr_payload

BASE = Path("demo_shop/checkout.py").read_text()
# Works whether or not the file ends with a newline.
assert BASE.count("    return round(total, 2)") == 1, "demo_shop/checkout.py isn't the plain version"
HEAD = BASE.replace("    return round(total, 2)",
                    "    # New: 10% off orders of 30.00 or more.\n"
                    "    if total >= 30:\n"
                    "        total *= 0.9\n"
                    "    return round(total, 2)")
DISCOUNT_LINE = HEAD.split("\n").index("        total *= 0.9") + 1


def patch_of(before, after):
    """A unified diff without file headers, the way GitHub's API returns `patch`."""
    lines = list(difflib.unified_diff(before.split("\n"), after.split("\n"), lineterm=""))
    return "\n".join(lines[2:])


class FakeGitHub:
    repo = REPO

    def __init__(self, base=BASE, head=HEAD, path="demo_shop/checkout.py", extra_files=()):
        self.path, self.sources = path, {"main": base, "sha1": head}
        self.extra = list(extra_files)
        self.comments, self.edits = [], []

    def set_head(self, sha, text):
        self.sources[sha] = text

    def list_pull_files(self, number):
        head = self.sources[self.current_sha]
        return [{"filename": self.path, "status": "modified",
                 "patch": patch_of(self.sources["main"], head)}] + self.extra

    def get_file(self, path, ref):
        if path != self.path or ref not in self.sources:
            return None
        return self.sources[ref], "blobsha"

    def post_comment(self, number, body):
        self.comments.append((number, body))
        return 1000 + len(self.comments)

    def edit_comment(self, comment_id, body):
        self.edits.append((comment_id, body))


class FakeLLM:
    """Answers by schema name; records what was asked."""

    def __init__(self, triage=None, prose=None):
        self.answers = {"triage": triage, "plan_prose": prose}
        self.calls = []

    def chat_json(self, system, user, name, schema):
        self.calls.append(name)
        answer = self.answers[name]
        if isinstance(answer, Exception):
            raise answer
        return answer


def triage_answer(**changes):
    answer = {
        "user_facing": True,
        "summary": "Adds a discount to large orders.",
        "functions": [{"file": "demo_shop/checkout.py", "name": "calculate_total"}],
        "flag_name": "ap_big_order_discount",
        "risk": {"tier": "high", "reasons": [
            {"claim": "Changes the price customers pay", "file": "demo_shop/checkout.py", "line": DISCOUNT_LINE}]},
        "success_metric": {"name": "checkout conversion", "why": "Cheaper big orders should convert better."},
    }
    answer.update(changes)
    return answer


PROSE = {"summary": "Big orders get a discount in `calculate_total`.",
         "risk_explanation": "It changes what customers pay.",
         "metric_explanation": "Conversion shows whether shoppers respond to the discount."}


def setup(llm="default", gh=None, approvers=("yvssshubam",)):
    store = Store(":memory:")
    gh = gh or FakeGitHub()
    gh.current_sha = "sha1"
    if llm == "default":
        llm = FakeLLM(triage_answer(), PROSE)
    handler = PRHandler(store, gh, approvers, llm=llm, log=lambda m: None)
    return store, gh, llm, handler


def send(store, handler, *events):
    for ev in events:
        store.add_github_event(f"d{len(store.db.execute('SELECT id FROM github_events').fetchall())}", ev)
    handler.process_pending()


def opened(number=7, sha="sha1", action="opened"):
    payload = pr_payload(action, number=number)
    payload["pull_request"]["head"]["sha"] = sha
    return parse_event("pull_request", payload)


def comment(body, author="yvssshubam", number=7):
    return parse_event("issue_comment", comment_payload(body, author=author, number=number))


# --- the plan comment ------------------------------------------------------------------

def test_opened_pr_gets_a_plan_and_nothing_else_happens():
    store, gh, llm, handler = setup()
    send(store, handler, opened())
    assert llm.calls == ["triage", "plan_prose"]
    assert len(gh.comments) == 1
    body = gh.comments[0][1]
    assert "release plan for `ap_big_order_discount`" in body
    assert "Waiting for approval" in body and "Nothing has been created or changed in Flagsmith" in body
    assert "Risk: **HIGH**" in body
    link = f"https://github.com/{REPO}/blob/sha1/demo_shop/checkout.py#L{DISCOUNT_LINE}"
    assert link in body and "Changes the price customers pay" in body
    # The preview is the real transformer's output.
    assert "+def _calculate_total__new(items):" in body and '+    if flag_on("ap_big_order_discount"):' in body
    # Numbers come from the high-risk template, not the AI.
    assert "120s with no gate failing, and at least 50 new-path requests" in body
    assert "above **2%**" in body and "**1 percentage point**" in body and "**1.3×**" in body
    assert "internal staff + 5% of users" in body and "internal staff + 25% of users" in body
    assert "`/approve`" in body and "@yvssshubam" in body

    release = store.release_for_pr(7)
    assert release["state"] == "proposed" and release["flag"] == "ap_big_order_discount"
    assert release["feature_id"] is None and release["segment_id"] is None  # nothing in Flagsmith
    assert release["plan_comment_id"] == 1001
    assert json.loads(release["policy"])["tier"] == "high"
    assert store.active_releases() == []  # the engine doesn't touch proposals


def test_unverified_reasons_default_to_high_and_say_so():
    llm = FakeLLM(triage_answer(risk={"tier": "low", "reasons": [
        {"claim": "Tiny change", "file": "demo_shop/checkout.py", "line": 1}]}), PROSE)
    store, gh, _, handler = setup(llm=llm)
    send(store, handler, opened())
    body = gh.comments[0][1]
    assert "Risk: **HIGH** (default" in body and "defaults to high" in body
    assert json.loads(store.release_for_pr(7)["policy"])["tier"] == "high"


def test_ai_wording_with_numbers_is_replaced():
    llm = FakeLLM(triage_answer(), dict(PROSE, summary="Gives 10% off over 30."))
    store, gh, _, handler = setup(llm=llm)
    send(store, handler, opened())
    body = gh.comments[0][1]
    assert "Gives 10% off" not in body and "Adds a discount to large orders." in body


def test_ai_cannot_skip_the_flag_for_changed_code():
    store, gh, _, handler = setup(llm=FakeLLM(triage_answer(user_facing=False), PROSE))
    send(store, handler, opened())
    assert "release plan for" in gh.comments[0][1]
    assert store.release_for_pr(7)["state"] == "proposed"


# --- when it can't make a plan -------------------------------------------------------------

def test_ai_garbage_means_wrap_manually_and_no_plan():
    from autopilot.llm import LLMError
    store, gh, _, handler = setup(llm=FakeLLM(LLMError("the answer wasn't JSON"), PROSE))
    send(store, handler, opened())
    body = gh.comments[0][1]
    assert "couldn't auto-flag this PR, please wrap it manually" in body
    assert "`calculate_total` in `demo_shop/checkout.py`" in body  # what code found, for the human
    assert store.release_for_pr(7) is None


def test_no_groq_key_means_wrap_manually():
    store, gh, _, handler = setup(llm=None)
    send(store, handler, opened())
    assert "GROQ_API_KEY" in gh.comments[0][1] and store.release_for_pr(7) is None


def test_ai_names_a_function_the_pr_did_not_change():
    llm = FakeLLM(triage_answer(functions=[{"file": "demo_shop/checkout.py", "name": "shipping"}]), PROSE)
    store, gh, _, handler = setup(llm=llm)
    send(store, handler, opened())
    assert "didn't name any function the PR really changed" in gh.comments[0][1]


def test_change_the_transformer_refuses_is_explained():
    head = BASE.replace("def calculate_total(items):", "def calculate_total(items, coupon=None):")
    store, gh, _, handler = setup(gh=FakeGitHub(head=head))
    send(store, handler, opened())
    body = gh.comments[0][1]
    assert "can't be wrapped automatically" in body and "parameters changed" in body
    assert store.release_for_pr(7) is None


def test_only_constants_changed():
    head = BASE.replace('"tea": 4.50', '"tea": 4.75')
    store, gh, llm, handler = setup(gh=FakeGitHub(head=head))
    send(store, handler, opened())
    assert "no existing top-level function" in gh.comments[0][1]
    assert llm.calls == []  # no AI call wasted


# --- new commits --------------------------------------------------------------------------

def test_new_commits_recheck_and_edit_the_same_comment():
    store, gh, llm, handler = setup()
    send(store, handler, opened())
    gh.set_head("sha2", HEAD.replace("0.9", "0.85"))
    gh.current_sha = "sha2"
    send(store, handler, opened(sha="sha2", action="synchronize"))
    assert len(gh.comments) == 1 and len(gh.edits) == 1
    comment_id, body = gh.edits[0]
    assert comment_id == 1001 and "total *= 0.85" in body and "now at `sha2`" in body
    release = store.release_for_pr(7)
    assert release["flag"] == "ap_big_order_discount"  # the name stays stable
    assert json.loads(release["plan"])["head_sha"] == "sha2"
    assert llm.calls == ["triage", "plan_prose"] * 2


def test_new_commits_that_break_wrapping_drop_the_plan():
    store, gh, _, handler = setup()
    send(store, handler, opened())
    gh.set_head("sha2", BASE.replace("def calculate_total(items):", "def calculate_total(items, x=1):"))
    gh.current_sha = "sha2"
    send(store, handler, opened(sha="sha2", action="synchronize"))
    assert store.release_for_pr(7)["state"] == "abandoned"
    assert "couldn't auto-flag" in gh.comments[-1][1]
    assert "Closed" in gh.edits[-1][1]


# --- commands ---------------------------------------------------------------------------------

def test_set_risk_switches_template_and_rerenders():
    store, gh, _, handler = setup()
    send(store, handler, opened(), comment("/set risk low"))
    policy = json.loads(store.release_for_pr(7)["policy"])
    assert policy["tier"] == "low" and policy["soak_seconds"] == 30
    body = gh.edits[-1][1]
    assert "Risk: **LOW** (set by @yvssshubam; the AI suggested high)" in body
    assert "30s with no gate failing" in body
    assert "Plan updated: risk set to low" in gh.comments[-1][1]


def test_set_threshold_within_bounds_and_refused_past_them():
    store, gh, _, handler = setup()
    send(store, handler, opened(), comment("/set max_error_rate 1.5"), comment("/set soak 200s"),
         comment("/set max_error_rate 9"), comment("/set colour blue"), comment("/set risk"))
    policy = json.loads(store.release_for_pr(7)["policy"])
    assert policy["max_error_rate_pct"] == 1.5 and policy["soak_seconds"] == 200
    replies = [b for _, b in gh.comments[1:]]
    assert "Plan updated" in replies[0] and "Plan updated" in replies[1]
    assert "wasn't changed" in replies[2] and "safety bound" in replies[2]
    assert "can't be set" in replies[3] and "wasn't changed" in replies[4]


def test_new_commits_keep_a_reviewers_settings():
    store, gh, _, handler = setup()
    send(store, handler, opened(), comment("/set risk medium"))
    gh.set_head("sha2", HEAD.replace("0.9", "0.85"))
    gh.current_sha = "sha2"
    send(store, handler, opened(sha="sha2", action="synchronize"))
    assert json.loads(store.release_for_pr(7)["policy"])["tier"] == "medium"


def test_non_approver_cannot_change_or_reject():
    store, gh, _, handler = setup()
    send(store, handler, opened(), comment("/set risk low", author="stranger"),
         comment("/reject", author="stranger"))
    assert store.release_for_pr(7)["state"] == "proposed"
    assert json.loads(store.release_for_pr(7)["policy"])["tier"] == "high"


def test_reject_closes_the_plan_and_later_commits_are_ignored():
    store, gh, llm, handler = setup()
    send(store, handler, opened(), comment("/reject"))
    assert store.release_for_pr(7)["state"] == "rejected"
    assert "Rejected" in gh.edits[-1][1] and "Plan rejected by @yvssshubam" in gh.comments[-1][1]
    send(store, handler, opened(action="synchronize"))
    assert llm.calls == ["triage", "plan_prose"]  # no new triage
    send(store, handler, comment("/set risk low"))
    assert "nothing on this PR is waiting" in gh.comments[-1][1]


def test_approve_is_acknowledged_until_9b():
    store, gh, _, handler = setup()
    send(store, handler, opened(), comment("/approve"))
    assert "next build step, 9b" in gh.comments[-1][1]
    assert store.release_for_pr(7)["state"] == "proposed"


# --- closing ------------------------------------------------------------------------------------

def test_closed_without_merge_drops_the_plan():
    store, gh, _, handler = setup()
    send(store, handler, opened(), parse_event("pull_request", pr_payload("closed")))
    assert store.release_for_pr(7)["state"] == "abandoned"
    assert "Closed" in gh.edits[-1][1] and len(gh.comments) == 1


def test_merged_without_approval_warns():
    store, gh, _, handler = setup()
    send(store, handler, opened(), parse_event("pull_request", pr_payload("closed", merged=True)))
    assert store.release_for_pr(7)["state"] == "abandoned"
    assert "merged **without an approved plan**" in gh.comments[-1][1]


def test_reopened_after_close_gets_a_new_plan():
    store, gh, _, handler = setup()
    send(store, handler, opened(), parse_event("pull_request", pr_payload("closed")), opened(action="reopened"))
    assert store.release_for_pr(7)["state"] == "proposed"
    assert sum("release plan for" in b for _, b in gh.comments) == 2


# --- flag names and storage -----------------------------------------------------------------------

def test_flag_name_in_use_by_another_pr_gets_a_suffix():
    store, gh, _, handler = setup()
    send(store, handler, opened(number=7), opened(number=8))
    assert store.release_for_pr(7)["flag"] == "ap_big_order_discount"
    assert store.release_for_pr(8)["flag"] == "ap_big_order_discount_2"


def test_existing_database_gets_the_new_columns(tmp_path):
    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE releases (id INTEGER PRIMARY KEY, flag TEXT NOT NULL, feature_id INTEGER,"
               " segment_id INTEGER, policy TEXT NOT NULL, state TEXT NOT NULL, stage_started_at REAL,"
               " approved_by TEXT, rollback_requested_by TEXT, rollback_reason TEXT, baseline_old TEXT,"
               " created_at REAL NOT NULL, updated_at REAL NOT NULL)")
    db.execute("INSERT INTO releases (flag, policy, state, created_at, updated_at)"
               " VALUES ('ap_checkout_v2', '{}', 'released', 1, 1)")
    db.commit()
    db.close()
    store = Store(str(path))
    assert store.all_releases()[0]["pr_number"] is None  # old row kept
    assert store.create_proposal(5, "ap_x", {"tier": "low"}, {"a": 1})
    assert store.release_for_pr(5)["state"] == "proposed"
    store.close()