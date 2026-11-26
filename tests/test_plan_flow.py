"""Step 9a: PR opened -> triage -> release plan comment, plus /set, /reject,
new commits, and closing. GitHub and Groq are faked; the transformer, the
AI-answer checks, the policy and the store are the real ones."""

import difflib
import json
import sqlite3

from autopilot.flagsmith_client import FlagsmithError
from autopilot.github_client import GitHubError
from autopilot.pr_handler import PRHandler
from autopilot.store import Store
from autopilot.webhooks import parse_event
from tests.sample_checkout import PLAIN
from tests.test_github import REPO, comment_payload, pr_payload

BASE = PLAIN  # a fixed copy: demo_shop/checkout.py itself changes with every demo PR
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
        self.pr_base = base  # what the PR's diff is against (stays put when main changes on merge)
        self.blobs = {"main": "blob-main", "sha1": "blob-sha1"}
        self.extra = list(extra_files)
        self.comments, self.edits, self.commits = [], [], []
        self.pull = {"state": "open", "merged": False,
                     "head": {"ref": "patch-1", "sha": "sha1", "repo": {"full_name": REPO}}}
        self.fail_commits = 0

    def set_head(self, sha, text):
        self.sources[sha] = text
        self.blobs[sha] = f"blob-{sha}"

    def get_pull(self, number):
        return json.loads(json.dumps(self.pull))

    def update_file(self, path, text, blob_sha, branch, message):
        if self.fail_commits:
            self.fail_commits -= 1
            raise GitHubError(f"commit {path}", 502, "bad gateway")
        sha = f"bot{len(self.commits) + 1}"
        self.commits.append({"path": path, "text": text, "blob": blob_sha, "branch": branch,
                             "message": message, "sha": sha})
        self.set_head(sha, text)
        self.pull["head"]["sha"] = sha
        self.current_sha = sha
        return sha

    def list_pull_files(self, number):
        head = self.sources[self.current_sha]
        return [{"filename": self.path, "status": "modified",
                 "patch": patch_of(self.pr_base, head)}] + self.extra

    def get_file(self, path, ref):
        if path != self.path or ref not in self.sources:
            return None
        return self.sources[ref], self.blobs[ref]

    def post_comment(self, number, body):
        self.comments.append((number, body))
        return 1000 + len(self.comments)

    def edit_comment(self, comment_id, body):
        self.edits.append((comment_id, body))


class FakeFlagsmith:
    def __init__(self, existing=()):
        self.features = {name: 900 + i for i, name in enumerate(existing)}
        self.created, self.deleted, self.fail_create, self.delete_status = [], [], 0, None

    def list_features(self):
        return [{"name": n, "id": i} for n, i in self.features.items()]

    def get_feature_by_name(self, name):
        return next((f for f in self.list_features() if f["name"] == name), None)

    def create_feature(self, name, description=""):
        if self.fail_create:
            self.fail_create -= 1
            raise FlagsmithError(f"create feature {name}", 503, "unavailable")
        fid = 500 + len(self.created)
        self.features[name] = fid
        self.created.append((name, description))
        return fid

    def delete_feature(self, feature_id):
        if self.delete_status:
            raise FlagsmithError("delete feature", self.delete_status, "not found")
        self.deleted.append(feature_id)


class FakeLLM:
    """Answers by schema name; records what was asked."""

    def __init__(self, triage=None, prose=None, diagnosis=None):
        self.answers = {"triage": triage, "plan_prose": prose, "diagnosis": diagnosis}
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


def setup(llm="default", gh=None, approvers=("yvssshubam",), fs="default"):
    store = Store(":memory:")
    gh = gh or FakeGitHub()
    gh.current_sha = "sha1"
    if llm == "default":
        llm = FakeLLM(triage_answer(), PROSE)
    if fs == "default":
        fs = FakeFlagsmith()
    handler = PRHandler(store, gh, approvers, llm=llm, fs=fs, log=lambda m: None)
    handler.fake_fs = fs
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
    assert "The AI's reasoning for its suggestion: It changes what customers pay." in body
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


# --- 9b: /approve -----------------------------------------------------------------------------------

def approved():
    store, gh, llm, handler = setup()
    send(store, handler, opened(), comment("/approve"))
    return store, gh, llm, handler


def test_approve_creates_flag_off_and_commits_the_previewed_code():
    store, gh, _, handler = approved()
    fs = handler.fake_fs
    assert fs.created == [("ap_big_order_discount", "Release Autopilot, PR #7: Discount")]
    assert len(gh.commits) == 1
    c = gh.commits[0]
    assert c["path"] == "demo_shop/checkout.py" and c["branch"] == "patch-1" and c["blob"] == "blob-sha1"
    assert 'if flag_on("ap_big_order_discount"):' in c["text"] and "def _calculate_total__old(items):" in c["text"]
    assert "Approved by @yvssshubam in #7" in c["message"] and "(no AI)" in c["message"]
    compile(c["text"], "checkout.py", "exec")
    # What was committed is exactly what the preview showed.
    plan = json.loads(store.release_for_pr(7)["plan"])
    for line in plan["preview"].split("\n"):
        if line.startswith("+") and not line.startswith("+++"):
            assert line[1:] in c["text"].split("\n")

    release = store.release_for_pr(7)
    assert release["state"] == "pending_merge" and release["feature_id"] == 500
    assert plan["approved_by"] == "yvssshubam" and plan["bot_commits"] == ["bot1"]
    assert store.active_releases() == []  # no rollout before merge
    reply = gh.comments[-1][1]
    assert "Plan approved by @yvssshubam" in reply and "**off for everyone**" in reply
    assert f"https://github.com/{REPO}/commit/bot1" in reply and "starts when this PR is merged" in reply
    assert "Approved by @yvssshubam" in gh.edits[-1][1] and "Commands" not in gh.edits[-1][1]


def test_bots_own_commit_is_ignored_and_second_approve_is_a_no_op():
    store, gh, llm, handler = approved()
    comments_before = len(gh.comments)
    send(store, handler, opened(sha="bot1", action="synchronize"))
    assert len(gh.comments) == comments_before and llm.calls == ["triage", "plan_prose"]
    send(store, handler, comment("/approve"))
    assert "already approved by @yvssshubam" in gh.comments[-1][1]
    assert len(handler.fake_fs.created) == 1 and len(gh.commits) == 1


def test_human_commit_after_approval_is_warned_once():
    store, gh, _, handler = approved()
    send(store, handler, opened(sha="human1", action="synchronize"), opened(sha="human1", action="synchronize"))
    warnings = [b for _, b in gh.comments if "New commits after approval" in b]
    assert len(warnings) == 1 and "human1" in warnings[0]
    assert store.release_for_pr(7)["state"] == "pending_merge"


def test_closing_an_approved_pr_deletes_the_flag():
    store, gh, _, handler = approved()
    send(store, handler, parse_event("pull_request", pr_payload("closed")))
    assert handler.fake_fs.deleted == [500]
    assert store.release_for_pr(7)["state"] == "abandoned"
    assert "deleted from Flagsmith" in gh.comments[-1][1] and "flag was deleted" in gh.edits[-1][1]


def test_closing_when_flag_already_gone_is_fine():
    store, gh, _, handler = approved()
    handler.fake_fs.delete_status = 404
    send(store, handler, parse_event("pull_request", pr_payload("closed")))
    assert store.release_for_pr(7)["state"] == "abandoned"


def _refused(store, gh, handler, text):
    release = store.release_for_pr(7)
    assert release["state"] == "proposed" and release["feature_id"] is None
    assert handler.fake_fs.created == [] and gh.commits == []
    assert "Approval not carried out" in gh.comments[-1][1] and text in gh.comments[-1][1]
    assert "approved_by" not in json.loads(release["plan"])


def test_approve_refused_for_a_fork():
    store, gh, _, handler = setup()
    gh.pull["head"]["repo"] = {"full_name": "someone/fork"}
    send(store, handler, opened(), comment("/approve"))
    _refused(store, gh, handler, "comes from a fork")


def test_approve_refused_when_pr_moved_since_the_plan():
    store, gh, _, handler = setup()
    send(store, handler, opened())
    gh.pull["head"]["sha"] = "sha9"
    send(store, handler, comment("/approve"))
    _refused(store, gh, handler, "new commits since this plan")


def test_approve_refused_when_main_changed_since_the_plan():
    store, gh, _, handler = setup()
    send(store, handler, opened())
    gh.blobs["main"] = "blob-main-2"
    send(store, handler, comment("/approve"))
    _refused(store, gh, handler, "changed on `main` since this plan")


def test_approve_refused_when_pr_closed():
    store, gh, _, handler = setup()
    send(store, handler, opened())
    gh.pull["state"] = "closed"
    send(store, handler, comment("/approve"))
    _refused(store, gh, handler, "isn't open")


def test_approve_refused_when_flag_exists_in_flagsmith():
    store, gh, _, handler = setup()
    send(store, handler, opened())
    handler.fake_fs.features["ap_big_order_discount"] = 77  # made by hand after the plan
    send(store, handler, comment("/approve"))
    _refused(store, gh, handler, "already exists in Flagsmith")


def test_plan_avoids_names_already_in_flagsmith():
    store, gh, _, handler = setup(fs=FakeFlagsmith(existing=["ap_big_order_discount"]))
    send(store, handler, opened())
    assert store.release_for_pr(7)["flag"] == "ap_big_order_discount_2"


def test_flagsmith_error_stops_before_commit_and_approve_again_resumes():
    store, gh, _, handler = setup()
    handler.fake_fs.fail_create = 1
    send(store, handler, opened(), comment("/approve"))
    assert gh.commits == [] and store.release_for_pr(7)["state"] == "approving"
    assert "Flagsmith error" in gh.comments[-1][1] and "Comment `/approve` to try again" in gh.comments[-1][1]
    send(store, handler, comment("/approve"))
    assert store.release_for_pr(7)["state"] == "pending_merge"
    assert len(handler.fake_fs.created) == 1 and len(gh.commits) == 1


def test_github_error_while_committing_is_retried_without_a_second_flag():
    store, gh, _, handler = setup()
    gh.fail_commits = 1
    send(store, handler, opened(), comment("/approve"))
    assert store.release_for_pr(7)["state"] == "approving" and gh.commits == []
    handler.process_pending()  # the queued /approve is retried
    assert store.release_for_pr(7)["state"] == "pending_merge"
    assert len(handler.fake_fs.created) == 1 and len(gh.commits) == 1


def test_approve_needs_an_approver_and_flagsmith():
    store, gh, _, handler = setup()
    send(store, handler, opened(), comment("/approve", author="stranger"))
    assert store.release_for_pr(7)["state"] == "proposed" and handler.fake_fs.created == []
    store, gh, _, handler = setup(fs=None)
    send(store, handler, opened(), comment("/approve"))
    assert "isn't connected to Flagsmith" in gh.comments[-1][1]
    assert store.release_for_pr(7)["state"] == "proposed"


def test_reject_and_set_do_nothing_after_approval():
    store, gh, _, handler = approved()
    send(store, handler, comment("/reject"), comment("/set risk low"))
    assert store.release_for_pr(7)["state"] == "pending_merge"
    assert all("nothing on this PR is waiting" in b for _, b in gh.comments[-2:])


def test_invalid_python_is_reported_as_such():
    head = BASE.replace("    return round(total, 2)", "      return round(total, 2)")  # 6 spaces: invalid
    store, gh, llm, handler = setup(gh=FakeGitHub(head=head))
    send(store, handler, opened())
    assert "isn't valid Python" in gh.comments[0][1].split("Details")[0]
    assert llm.calls == []