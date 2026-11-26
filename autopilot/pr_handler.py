"""Acts on GitHub events queued by the webhook. Runs inside the bot loop, so
the webhook can answer GitHub immediately (GitHub gives up after 10 s).

Step 6 scope: scope check (only demo_shop/ is managed), the approver
allow-list, and command parsing. Triage and the release plan replace the
"reviewing" reply in steps 8-9; commands start doing real work in step 9.
"""

from autopilot.github_client import GitHubError

MANAGED_PREFIX = "demo_shop/"
COMMANDS = {
    "/approve": "approve the plan, or the release to everyone",
    "/reject": "reject the proposed plan",
    "/rollback": "switch the flag off now",
    "/pause": "stop advancing stages",
    "/set": "change the plan before approval, e.g. /set risk high",
}


def parse_command(body):
    """('/approve', 'rest of line') if the comment's first line is a command."""
    first = (body or "").strip().splitlines()[0].strip() if (body or "").strip() else ""
    if not first.startswith("/"):
        return None
    name, _, rest = first.partition(" ")
    return name.lower(), rest.strip()


class PRHandler:
    def __init__(self, store, gh, approvers, log=print):
        self.store, self.gh, self.log = store, gh, log
        self.approvers = {a.strip().lower() for a in approvers if a.strip()}

    def process_pending(self):
        for row in self.store.pending_github_events():
            event = row["event"]
            try:
                result = self.handle(event)
            except GitHubError as e:
                retrying = self.store.fail_github_event(row["id"], str(e))
                self.log(f"[github] PR #{event.get('number')}: GitHub call failed ({e}); "
                         + ("will retry" if retrying else "GAVE UP after 3 attempts"))
                continue
            self.store.finish_github_event(row["id"], result)

    def handle(self, event):
        if event["kind"] == "pr":
            return self._on_pr(event)
        if event["kind"] == "comment":
            return self._on_comment(event)
        return "ignored"

    # --- pull requests ---------------------------------------------------------

    def _on_pr(self, ev):
        number, action = ev["number"], ev["action"]
        if action == "closed":
            self.log(f"[github] PR #{number} closed ({'merged' if ev['merged'] else 'not merged'})")
            return "closed"
        if action == "synchronize":
            self.log(f"[github] PR #{number}: new commits pushed")
            return "synchronize"  # re-triage before merge arrives in step 9

        files = self.gh.list_pull_files(number)
        managed = [f["filename"] for f in files if f["filename"].startswith(MANAGED_PREFIX)]
        if not managed:
            self.gh.post_comment(number, (
                "**Release Autopilot:** no flag needed. This PR doesn't change the managed app "
                f"(`{MANAGED_PREFIX}`), so it isn't released through Flagsmith."))
            self.log(f"[github] PR #{number}: outside {MANAGED_PREFIX}; replied 'no flag needed'")
            return "outside_scope"

        listed = "\n".join(f"- `{name}`" for name in managed[:20])
        more = f"\n- …and {len(managed) - 20} more" if len(managed) > 20 else ""
        self.gh.post_comment(number, (
            f"**Release Autopilot** is reviewing this PR. It changes {len(managed)} file(s) in the "
            f"managed app:\n{listed}{more}\n\nA release plan (risk, stages, switch-off rule) will be "
            "posted here for approval. Nothing changes in Flagsmith until someone approves it."))
        self.log(f"[github] PR #{number}: {len(managed)} managed file(s); acknowledged")
        return "in_scope"

    # --- comments ----------------------------------------------------------------

    def _on_comment(self, ev):
        command = parse_command(ev["body"])
        if command is None:
            return "not_a_command"
        name, _ = command
        number, author = ev["number"], ev["author"]

        if name not in COMMANDS:
            options = "\n".join(f"- `{c}`: {d}" for c, d in COMMANDS.items())
            self.gh.post_comment(number, f"@{author} `{name}` isn't a Release Autopilot command. Available:\n{options}")
            return "unknown_command"
        if author.lower() not in self.approvers:
            self.gh.post_comment(number, f"@{author} isn't on the approver list, so `{name}` was ignored.")
            self.log(f"[github] PR #{number}: {name} from @{author} refused (not an approver)")
            return "not_authorised"

        self.gh.post_comment(number, f"@{author} `{name}` received, but nothing on this PR is waiting for it yet.")
        self.log(f"[github] PR #{number}: {name} from @{author} (nothing pending)")
        return f"command:{name}"