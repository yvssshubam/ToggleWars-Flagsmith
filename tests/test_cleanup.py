"""Step 10, Phase C: released -> cleanup PR -> merge (approval #3) -> deploy ->
flag and segment deleted -> completed. Runs a real rollout to `released` first;
GitHub, Flagsmith and Groq are faked."""

from autopilot.cleanup import CLEANUP_PREFIX, RETRY_SECONDS, Cleanup
from autopilot.github_client import GitHubError
from autopilot.webhooks import parse_event
from tests.test_github import pr_payload
from tests.test_plan_flow import HEAD, comment
from tests.test_rollout_flow import FLAG, Env, FakeGitHub

BRANCH = f"{CLEANUP_PREFIX}{FLAG}"


class CleanupGitHub(FakeGitHub):
    """Adds branches and opening PRs to the plan-flow fake."""

    def __init__(self):
        super().__init__()
        self.branches, self.pulls_opened, self.fail_next = {}, [], set()

    def _maybe_fail(self, name):
        if name in self.fail_next:
            self.fail_next.discard(name)
            raise GitHubError(name, 502, "bad gateway")

    def branch_sha(self, branch):
        return f"{branch}-tip"

    def create_branch(self, branch, sha):
        self._maybe_fail("create_branch")
        if branch in self.branches:
            return False
        self.branches[branch] = self.sources["main"]
        return True

    def get_file(self, path, ref):
        if ref in self.branches and path == self.path:
            return self.branches[ref], f"blob-{ref}-{len(self.branches[ref])}"
        return super().get_file(path, ref)

    def update_file(self, path, text, blob_sha, branch, message):
        if branch in self.branches:
            self.branches[branch] = text
            self.commits.append({"path": path, "text": text, "branch": branch, "message": message})
            return f"cleanup{len(self.commits)}"
        return super().update_file(path, text, blob_sha, branch, message)

    def create_pull(self, title, branch, base, body):
        self._maybe_fail("create_pull")
        self.pulls_opened.append({"title": title, "branch": branch, "base": base, "body": body})
        return 40 + len(self.pulls_opened)


class CleanupEnv(Env):
    def __init__(self, deploy_ok=True):
        super().__init__()
        self.gh = CleanupGitHub()
        self.gh.current_sha = "sha1"
        self.engine.notify.gh = self.gh
        self.handler.gh = self.gh
        self.deploy_ok = deploy_ok

        def deploy():
            self.fs.calls.append("deploy")
            return self.deploy_ok, "Fast-forward" if self.deploy_ok else "error: local changes"
        self.handler.deploy = deploy
        self.cleanup = Cleanup(self.store, self.gh, self.fs, deploy, clock=self.clock, log=self.logs.append)
        self.handler.cleanup = self.cleanup

    def release_it(self):
        self.approve_and_merge()
        for _ in range(3):
            self.stage()
        self.send(comment("/approve"))
        self.engine.tick(self.release()["id"])
        assert self.stage(old=0) == "released"

    def merge_cleanup(self, merged=True):
        payload = pr_payload("closed", number=self.release()["cleanup_pr"], merged=merged)
        payload["pull_request"]["head"]["ref"] = BRANCH
        if merged:
            self.gh.sources["main"] = self.gh.branches[BRANCH]
        self.send(parse_event("pull_request", payload))


def released():
    env = CleanupEnv()
    env.release_it()
    return env


# --- opening the cleanup PR ---------------------------------------------------------------

def test_released_release_gets_a_cleanup_pr_with_the_released_code():
    env = released()
    env.cleanup.tick()
    assert env.gh.branches[BRANCH] == HEAD  # the released version, no flag code
    pr = env.gh.pulls_opened[0]
    assert pr["branch"] == BRANCH and pr["base"] == "main" and FLAG in pr["title"]
    assert "third approval" in pr["body"] and "(no AI)" in pr["body"]
    release = env.release()
    assert release["state"] == "cleanup_pr_open" and release["cleanup_pr"] == 41
    assert "Cleanup PR #41 opened" in env.bodies()[-1]
    assert "cleanup PR #41 is open" in env.gh.edits[-1][1]  # plan comment status follows
    assert "delete_feature" not in env.fs.calls  # nothing deleted before the human merges
    env.cleanup.tick()
    assert len(env.gh.pulls_opened) == 1  # no second PR


def test_merging_the_cleanup_deploys_first_then_deletes_flag_and_segment():
    env = released()
    env.cleanup.tick()
    env.merge_cleanup()
    calls = env.fs.calls
    assert calls.index("deploy", calls.index("set_default")) < calls.index("delete_segment") < calls.index("delete_feature")
    assert env.fs.features == {}
    assert env.release()["state"] == "completed"
    assert "✅ **Completed.**" in env.bodies()[-1] and "✅ **Completed.**" in env.gh.edits[-1][1]


def test_deploy_failure_keeps_the_flag_on_and_retries():
    env = released()
    env.cleanup.tick()
    env.deploy_ok = False
    env.merge_cleanup()
    assert env.release()["state"] == "cleanup_merged" and FLAG in env.fs.features
    assert env.fs.default_on is True  # users keep the released code meanwhile
    assert "kept on for everyone" in env.bodies()[-1]
    warnings, deploys = len(env.bodies()), env.fs.calls.count("deploy")
    env.cleanup.tick()  # too soon: no retry, no new comment
    assert len(env.bodies()) == warnings and env.fs.calls.count("deploy") == deploys
    env.deploy_ok = True
    env.clock.now += RETRY_SECONDS + 1
    env.cleanup.tick()
    assert env.release()["state"] == "completed" and env.fs.features == {}


def test_flag_is_not_deleted_while_main_still_asks_for_it():
    env = released()
    env.cleanup.tick()
    payload = pr_payload("closed", number=41, merged=True)
    payload["pull_request"]["head"]["ref"] = BRANCH
    env.send(parse_event("pull_request", payload))  # "merged", but main didn't change
    assert env.release()["state"] == "cleanup_merged" and FLAG in env.fs.features


def test_closing_the_cleanup_pr_changes_nothing():
    env = released()
    env.cleanup.tick()
    env.merge_cleanup(merged=False)
    assert env.release()["state"] == "released" and FLAG in env.fs.features
    assert "stays on for everyone" in env.bodies()[-1]
    env.cleanup.tick()
    assert len(env.gh.pulls_opened) == 1  # not reopened over and over


def test_flag_already_deleted_by_hand_completes_without_a_pr():
    env = released()
    env.fs.features.clear()
    env.cleanup.tick()
    assert env.release()["state"] == "completed" and env.gh.pulls_opened == []


def test_unwrap_impossible_is_reported_once():
    env = released()
    env.gh.sources["main"] = HEAD  # someone removed the wrapper by hand, flag still exists
    env.cleanup.tick()
    env.cleanup.tick()
    failures = [b for b in env.bodies() if "Couldn't prepare the cleanup PR" in b]
    assert len(failures) == 1 and "stays **on for everyone**" in failures[0]
    assert env.gh.pulls_opened == [] and env.release()["state"] == "released"


def test_github_error_while_opening_is_retried_later_without_duplicates():
    env = released()
    env.gh.fail_next.add("create_pull")
    env.cleanup.tick()
    assert env.release()["state"] == "released" and any("cleanup step failed" in m for m in env.logs)
    env.cleanup.tick()  # within the retry wait: nothing
    assert env.gh.pulls_opened == []
    env.clock.now += RETRY_SECONDS + 1
    env.cleanup.tick()  # branch already exists and already has the commit
    assert env.release()["state"] == "cleanup_pr_open" and len(env.gh.pulls_opened) == 1
    assert sum(c["branch"] == BRANCH for c in env.gh.commits) == 1


def test_cleanup_pr_events_are_not_triaged_as_new_prs():
    env = released()
    env.cleanup.tick()
    calls = list(env.llm.calls)
    payload = pr_payload("opened", number=41)
    payload["pull_request"]["head"]["ref"] = BRANCH
    env.send(parse_event("pull_request", payload))
    assert env.llm.calls == calls and env.release()["state"] == "cleanup_pr_open"


def test_releases_without_a_pr_are_left_alone():
    env = CleanupEnv()
    release_id = env.store.create_release("ap_cli", 1, 2, {"tier": "low"}, "released")
    env.cleanup.tick()
    assert env.store.get_release(release_id)["state"] == "released" and env.gh.pulls_opened == []