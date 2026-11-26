"""Offline tests for the GitHub side: client, webhook signatures and parsing,
the webhook endpoint, and the PR handler. GitHub is faked; no network."""

import base64
import hashlib
import hmac
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from autopilot.github_client import BOT_MARKER, GitHubClient, GitHubError
from autopilot.pr_handler import PRHandler, parse_command
from autopilot.server import create_app
from autopilot.store import Store
from autopilot.webhooks import parse_event, verify_signature

SECRET = "test-secret-1234567890"
REPO = "yvssshubam/ToggleWars-Flagsmith"


def sign(body, secret=SECRET):
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def pr_payload(action="opened", number=7, repo=REPO, merged=False):
    return {"action": action, "repository": {"full_name": repo},
            "pull_request": {"number": number, "title": "Discount", "user": {"login": "yvssshubam"},
                             "head": {"ref": "feature", "sha": "abc"}, "base": {"ref": "main"},
                             "merged": merged}}


def comment_payload(body="/approve", author="yvssshubam", on_pr=True, number=7):
    issue = {"number": number, **({"pull_request": {"url": "x"}} if on_pr else {})}
    return {"action": "created", "repository": {"full_name": REPO}, "issue": issue,
            "comment": {"id": 99, "body": body, "user": {"login": author}}}


# --- signatures and parsing ----------------------------------------------------

def test_signature_checks():
    body = b'{"a": 1}'
    assert verify_signature(SECRET, body, sign(body))
    assert not verify_signature(SECRET, body, sign(body, "other-secret"))
    assert not verify_signature(SECRET, body + b" ", sign(body))  # body changed
    assert not verify_signature(SECRET, body, None)
    assert not verify_signature(SECRET, body, "sha1=abc")
    assert not verify_signature("", body, sign(body, ""))  # no secret configured: never valid


def test_parse_pr_and_comment_events():
    ev = parse_event("pull_request", pr_payload("opened"))
    assert ev["kind"] == "pr" and ev["number"] == 7 and ev["author"] == "yvssshubam"
    assert parse_event("pull_request", pr_payload("labeled")) is None
    c = parse_event("issue_comment", comment_payload("/approve"))
    assert c == {"kind": "comment", "number": 7, "author": "yvssshubam", "body": "/approve", "comment_id": 99}
    assert parse_event("issue_comment", comment_payload(on_pr=False)) is None  # plain issue
    assert parse_event("issue_comment", comment_payload(f"{BOT_MARKER}\nhello")) is None  # the bot itself
    assert parse_event("push", {}) is None


def test_parse_command():
    assert parse_command("/approve") == ("/approve", "")
    assert parse_command("  /SET risk high\nthanks") == ("/set", "risk high")
    assert parse_command("looks good! /approve") is None
    assert parse_command("") is None and parse_command(None) is None


# --- webhook endpoint ------------------------------------------------------------

@pytest.fixture
def server():
    store = Store(":memory:")
    logs = []
    client = TestClient(create_app(store, github_secret=SECRET, github_repo=REPO, log=logs.append))
    return store, client, logs


def post(client, event_type, payload, delivery="d1", secret=SECRET, raw=None):
    body = raw if raw is not None else json.dumps(payload).encode()
    return client.post("/webhooks/github", content=body, headers={
        "X-GitHub-Event": event_type, "X-GitHub-Delivery": delivery,
        "X-Hub-Signature-256": sign(body, secret), "Content-Type": "application/json"})


def test_webhook_rejects_bad_signature(server):
    store, client, logs = server
    r = post(client, "pull_request", pr_payload(), secret="wrong-secret")
    assert r.status_code == 401 and store.pending_github_events() == []
    assert "wrong signature" in logs[-1]


def test_webhook_ping_and_queueing(server):
    store, client, logs = server
    assert post(client, "ping", {"zen": "hi"}).json() == {"pong": True}
    assert "webhook connected" in logs[-1]
    r = post(client, "pull_request", pr_payload())
    assert r.status_code == 202 and r.json() == {"queued": True}
    assert post(client, "pull_request", pr_payload()).json() == {"queued": False}  # same delivery again
    assert len(store.pending_github_events()) == 1


def test_webhook_ignores_other_repos_and_events(server):
    store, client, logs = server
    assert "ignored" in post(client, "pull_request", pr_payload(repo="someone/else"), "d2").json()
    assert "ignored" in post(client, "issue_comment", comment_payload(on_pr=False), "d3").json()
    assert store.pending_github_events() == []


def test_webhook_not_configured_and_not_json():
    client = TestClient(create_app(Store(":memory:")))
    assert client.post("/webhooks/github", content=b"{}").status_code == 503
    store = Store(":memory:")
    client = TestClient(create_app(store, github_secret=SECRET, github_repo=REPO, log=lambda m: None))
    assert post(client, "pull_request", None, raw=b"payload=abc").status_code == 400


# --- client ----------------------------------------------------------------------

def make_gh(handler):
    calls = []

    def record(request):
        calls.append(request)
        return handler(request)

    return GitHubClient("github_pat_x", REPO, transport=httpx.MockTransport(record)), calls


def test_client_headers_comment_and_marker():
    gh, calls = make_gh(lambda r: httpx.Response(201, json={"id": 5}))
    assert gh.post_comment(7, "hello") == 5
    req = calls[0]
    assert str(req.url) == f"https://api.github.com/repos/{REPO}/issues/7/comments"
    assert req.headers["Authorization"] == "Bearer github_pat_x"
    assert json.loads(req.content)["body"] == f"{BOT_MARKER}\nhello"


def test_client_pr_files_pagination():
    def handler(r):
        page = int(r.url.params["page"])
        count = 100 if page == 1 else 3
        return httpx.Response(200, json=[{"filename": f"f{page}-{i}.py"} for i in range(count)])

    gh, calls = make_gh(handler)
    assert len(gh.list_pull_files(7)) == 103 and len(calls) == 2


def test_client_get_file_and_missing_file():
    content = base64.b64encode(b"print('hi')\n").decode()

    def handler(r):
        if "missing" in str(r.url):
            return httpx.Response(404, json={"message": "Not Found"})
        return httpx.Response(200, json={"content": content, "sha": "s1"})

    gh, _ = make_gh(handler)
    assert gh.get_file("demo_shop/checkout.py", "main") == ("print('hi')\n", "s1")
    assert gh.get_file("missing.py", "main") is None


def test_client_errors():
    gh, _ = make_gh(lambda r: httpx.Response(403, json={"message": "Resource not accessible"}))
    with pytest.raises(GitHubError) as e:
        gh.get_repo()
    assert e.value.status_code == 403
    with pytest.raises(ValueError):
        GitHubClient("t", "no-slash")


# --- PR handler --------------------------------------------------------------------

class FakeGitHub:
    def __init__(self, files=()):
        self.files, self.comments, self.fail = list(files), [], 0

    def list_pull_files(self, number):
        if self.fail:
            self.fail -= 1
            raise GitHubError("list files", 502, "bad gateway")
        return [{"filename": f} for f in self.files]

    def post_comment(self, number, body):
        self.comments.append((number, body))
        return len(self.comments)


def run_handler(files, *events, approvers=("yvssshubam",)):
    store, gh = Store(":memory:"), FakeGitHub(files)
    for i, ev in enumerate(events):
        store.add_github_event(f"d{i}", ev)
    handler = PRHandler(store, gh, approvers, log=lambda m: None)
    handler.process_pending()
    return store, gh, handler


def test_pr_outside_managed_app_gets_no_flag_needed():
    _, gh, _ = run_handler(["README.md", "autopilot/engine.py"], parse_event("pull_request", pr_payload()))
    assert len(gh.comments) == 1 and "no flag needed" in gh.comments[0][1]


def test_pr_in_managed_app_is_acknowledged_and_lists_files():
    _, gh, _ = run_handler(["demo_shop/checkout.py", "README.md"], parse_event("pull_request", pr_payload()))
    body = gh.comments[0][1]
    assert "reviewing this PR" in body and "`demo_shop/checkout.py`" in body and "README.md" not in body
    assert "Nothing changes in Flagsmith until someone approves it" in body


def test_new_commits_and_close_post_nothing():
    _, gh, _ = run_handler(["demo_shop/checkout.py"],
                           parse_event("pull_request", pr_payload("synchronize")),
                           parse_event("pull_request", pr_payload("closed", merged=True)))
    assert gh.comments == []


def test_commands_and_approver_list():
    _, gh, _ = run_handler([],
                           parse_event("issue_comment", comment_payload("Looks good to me")),
                           parse_event("issue_comment", comment_payload("/approve", author="stranger")),
                           parse_event("issue_comment", comment_payload("/approve", author="YVSSSHUBAM")),
                           parse_event("issue_comment", comment_payload("/deploy")))
    bodies = [b for _, b in gh.comments]
    assert len(bodies) == 3  # the plain comment gets no reply
    assert "isn't on the approver list" in bodies[0] and "@stranger" in bodies[0]
    assert "`/approve` received" in bodies[1]  # approver match ignores case
    assert "isn't a Release Autopilot command" in bodies[2] and "/rollback" in bodies[2]


def test_no_approvers_configured_means_nobody_can_approve():
    _, gh, _ = run_handler([], parse_event("issue_comment", comment_payload("/approve")), approvers=())
    assert "isn't on the approver list" in gh.comments[0][1]


def test_github_failure_is_retried_then_given_up():
    store, gh = Store(":memory:"), FakeGitHub(["demo_shop/checkout.py"])
    gh.fail = 1
    store.add_github_event("d1", parse_event("pull_request", pr_payload()))
    handler = PRHandler(store, gh, ["yvssshubam"], log=lambda m: None)
    handler.process_pending()
    assert gh.comments == [] and len(store.pending_github_events()) == 1  # will retry
    handler.process_pending()
    assert len(gh.comments) == 1 and store.pending_github_events() == []

    gh.fail = 10
    store.add_github_event("d2", parse_event("pull_request", pr_payload(number=8)))
    for _ in range(5):
        handler.process_pending()
    assert store.pending_github_events() == []  # gave up after 3 attempts, no endless retries