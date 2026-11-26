"""Acts on GitHub events queued by the webhook. Runs inside the bot loop, so
the webhook can answer GitHub immediately (GitHub gives up after 10 s).

Step 9a: a PR that changes demo_shop/ gets a release plan (Phase A, steps 1-4):
  1. code finds the top-level functions the PR really changed (transformer);
  2. the AI triages (llm.triage), and code checks its answer;
  3. code builds the wrapped file as a preview (transformer.wrap);
  4. the plan comment is posted (words from the AI, numbers from the policy),
     and the plan is stored as `proposed`. Nothing happens in Flagsmith.
New commits re-check the plan and edit the same comment. Approvers can
/reject it or /set the risk tier and thresholds. /approve does its work in 9b.
"""

import difflib
import json
from dataclasses import asdict

from autopilot import plan_comment
from autopilot.github_client import GitHubError
from autopilot.llm import Triage, plan_prose, triage
from autopilot.policy import Policy, for_tier, with_overrides
from autopilot.transformer import TransformError, changed_functions, wrap

MANAGED_PREFIX = "demo_shop/"
COMMANDS = {
    "/approve": "approve the plan, or the release to everyone",
    "/reject": "reject the proposed plan",
    "/rollback": "switch the flag off now",
    "/pause": "stop advancing stages",
    "/set": "change the plan before approval, e.g. /set risk high",
}
INT_FIELDS = ("soak_seconds", "min_new_requests")


def parse_command(body):
    """('/approve', 'rest of line') if the comment's first line is a command."""
    first = (body or "").strip().splitlines()[0].strip() if (body or "").strip() else ""
    if not first.startswith("/"):
        return None
    name, _, rest = first.partition(" ")
    return name.lower(), rest.strip()


def preview_diff(file, before, after):
    return "\n".join(difflib.unified_diff(before.split("\n"), after.split("\n"),
                                          f"{file} (this PR)", f"{file} (after /approve)", lineterm=""))


class PRHandler:
    def __init__(self, store, gh, approvers, llm=None, log=print):
        self.store, self.gh, self.llm, self.log = store, gh, llm, log
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
        release = self.store.release_for_pr(number)
        if action == "closed":
            return self._on_closed(ev, release)
        if release and release["state"] not in ("proposed", "rejected", "abandoned"):
            # Approved already: the bot's own wrapper commit lands here too (9b).
            self.log(f"[github] PR #{number}: {action} ignored (plan is {release['state']})")
            return "ignored_after_approval"
        if release and release["state"] == "rejected" and action != "reopened":
            self.log(f"[github] PR #{number}: new commits on a rejected plan; ignored")
            return "ignored_rejected"
        if release and release["state"] == "abandoned" and action != "reopened":
            return "ignored_closed"
        return self._propose(ev, release if release and release["state"] == "proposed" else None)

    def _on_closed(self, ev, release):
        number = ev["number"]
        if not release or release["state"] != "proposed":
            self.log(f"[github] PR #{number} closed ({'merged' if ev['merged'] else 'not merged'})")
            return "closed"
        self.store.update_release(release["id"], state="abandoned")
        if ev["merged"]:
            self.store.add_event(release["id"], "merged_unapproved", "PR merged without plan approval")
            self.gh.post_comment(number, (
                "⚠️ **Release Autopilot:** this PR was merged **without an approved plan**, so its change "
                "is in `main` **without a flag** and no rollout was started. Nothing was created in Flagsmith. "
                "Decide whether to revert it, or open a new PR so the change can be released safely."))
            self._rerender(self.store.get_release(release["id"]))
            self.log(f"[github] PR #{number}: merged without approval; warned")
            return "merged_unapproved"
        self.store.add_event(release["id"], "abandoned", "PR closed without merge")
        self._rerender(self.store.get_release(release["id"]))
        self.log(f"[github] PR #{number}: closed; plan dropped")
        return "abandoned"

    def _propose(self, ev, existing):
        number = ev["number"]
        files = self.gh.list_pull_files(number)
        managed = [f for f in files if f["filename"].startswith(MANAGED_PREFIX)]
        if not managed:
            if existing is None and ev["action"] != "synchronize":  # say it once, not on every push
                self.gh.post_comment(number, (
                    "**Release Autopilot:** no flag needed. This PR doesn't change the managed app "
                    f"(`{MANAGED_PREFIX}`), so it isn't released through Flagsmith."))
            self.log(f"[github] PR #{number}: outside {MANAGED_PREFIX}; no flag needed")
            return "outside_scope"

        # 1. Which top-level functions really changed (code, not AI).
        sources, changed, problems = {}, {}, []
        for f in managed:
            path = f["filename"]
            if not path.endswith(".py") or f.get("status") not in ("modified", "renamed"):
                continue
            base = self.gh.get_file(f.get("previous_filename") or path, ev["base_ref"])
            head = self.gh.get_file(path, ev["head_sha"])
            if base is None or head is None:
                continue
            try:
                names = changed_functions(base[0], head[0])
            except TransformError as e:
                problems.append(f"`{path}`: {e}")
                continue
            if names:
                sources[path], changed[path] = (base[0], head[0]), names
        if not changed:
            return self._cannot_flag(number, existing, (
                f"it changes `{MANAGED_PREFIX}` but no existing top-level function (for example only new "
                "files, constants, classes or imports)"), problems)

        # 2. AI triage, checked by code.
        if self.llm is None:
            tri = Triage(ok=False, error="GROQ_API_KEY isn't set, so the AI triage can't run")
        else:
            tri = triage(self.llm, managed, changed, pr_title=ev.get("title", ""))
        if not tri.ok or not tri.functions:
            found = ", ".join(f"`{n}` in `{p}`" for p, names in changed.items() for n in names)
            why = tri.error if not tri.ok else "the AI didn't name any function the PR really changed"
            return self._cannot_flag(number, existing, f"{why}. Changed functions found by code: {found}",
                                     problems + tri.notes)
        if not tri.user_facing:
            self.gh.post_comment(number, "**Release Autopilot:** no flag needed. " + tri.summary)
            return "not_user_facing"

        # 3. Flag name and the wrapped code (preview of what /approve commits).
        flag = existing["flag"] if existing else self._unique_flag(tri.flag_name, number)
        previews = []
        try:
            for path in sorted({p for p, _ in tri.functions}):
                base, head = sources[path]
                wrapped = head
                for _, name in sorted(fn for fn in tri.functions if fn[0] == path):
                    wrapped = wrap(base, wrapped, name, flag)
                previews.append(preview_diff(path, head, wrapped))
        except TransformError as e:
            return self._cannot_flag(number, existing, f"the code can't be wrapped automatically: {e}", problems)

        # 4. Wording, then the plan comment.
        prose = plan_prose(self.llm, tri)
        old_plan = json.loads(existing["plan"]) if existing else {}
        plan = {
            "head_sha": ev["head_sha"], "base_ref": ev["base_ref"], "title": ev.get("title", ""),
            "functions": [list(fn) for fn in tri.functions],
            "summary": tri.summary, "tier_ai": tri.tier, "tier_defaulted": tri.tier_defaulted,
            "reasons": [asdict(c) for c in tri.reasons], "notes": problems + tri.notes + prose.notes,
            "success_metric": tri.success_metric, "success_metric_why": tri.success_metric_why,
            "prose": asdict(prose), "preview": "\n".join(previews),
            "changes": old_plan.get("changes", []), "tier_set_by": old_plan.get("tier_set_by"),
            "updates": old_plan.get("updates", -1) + 1 if existing else 0,
        }
        # A reviewer's /set choices survive new commits; otherwise the AI's tier picks the template.
        policy = _policy_of(existing) if existing and plan["changes"] else for_tier(tri.tier)
        if existing:
            self.store.update_release(existing["id"], plan=json.dumps(plan), policy=json.dumps(asdict(policy)))
            release = self.store.get_release(existing["id"])
            self.store.add_event(release["id"], "plan_updated", f"plan re-checked at {ev['head_sha'][:7]}")
            self._rerender(release)
            self.log(f"[github] PR #{number}: new commits; plan for {flag} re-checked and comment updated")
            return "plan_updated"
        release_id = self.store.create_proposal(number, flag, asdict(policy), plan)
        release = self.store.get_release(release_id)
        body = plan_comment.render(plan, policy, flag, "proposed", self.gh.repo, sorted(self.approvers))
        comment_id = self.gh.post_comment(number, body)
        self.store.update_release(release_id, plan_comment_id=comment_id)
        self.store.add_event(release_id, "proposed",
                             f"plan proposed: {flag}, {policy.tier} risk"
                             + (" (defaulted)" if tri.tier_defaulted else ""),
                             {"functions": plan["functions"], "reasons": plan["reasons"]})
        self.log(f"[github] PR #{number}: plan proposed for {flag} ({policy.tier} risk; "
                 f"{len(tri.reasons)} verified reason(s)); waiting for /approve")
        return "proposed"

    def _cannot_flag(self, number, existing, why, notes=()):
        if existing:  # a plan that no longer works can't stay approvable
            self.store.update_release(existing["id"], state="abandoned")
            self.store.add_event(existing["id"], "abandoned", f"plan dropped after new commits: {why}")
            self._rerender(self.store.get_release(existing["id"]))
        extra = "".join(f"\n- {n}" for n in notes)
        self.gh.post_comment(number, (
            f"**Release Autopilot:** couldn't auto-flag this PR, please wrap it manually. Reason: {why}."
            + (f"\n\nDetails:{extra}" if extra else "")
            + "\n\nNothing was created in Flagsmith."))
        self.log(f"[github] PR #{number}: can't auto-flag ({why})")
        return "cannot_flag"

    def _unique_flag(self, name, number):
        base, candidate, n = name[:56], name, 2
        while self.store.flag_in_use(candidate, except_pr=number):
            candidate, n = f"{base}_{n}", n + 1
        return candidate

    def _rerender(self, release):
        if not release.get("plan_comment_id"):
            return
        plan = json.loads(release["plan"])
        body = plan_comment.render(plan, _policy_of(release), release["flag"], release["state"],
                                   self.gh.repo, sorted(self.approvers))
        self.gh.edit_comment(release["plan_comment_id"], body)

    # --- comments ----------------------------------------------------------------

    def _on_comment(self, ev):
        command = parse_command(ev["body"])
        if command is None:
            return "not_a_command"
        name, rest = command
        number, author = ev["number"], ev["author"]

        if name not in COMMANDS:
            options = "\n".join(f"- `{c}`: {d}" for c, d in COMMANDS.items())
            self.gh.post_comment(number, f"@{author} `{name}` isn't a Release Autopilot command. Available:\n{options}")
            return "unknown_command"
        if author.lower() not in self.approvers:
            self.gh.post_comment(number, f"@{author} isn't on the approver list, so `{name}` was ignored.")
            self.log(f"[github] PR #{number}: {name} from @{author} refused (not an approver)")
            return "not_authorised"

        release = self.store.release_for_pr(number)
        proposed = release if release and release["state"] == "proposed" else None
        if name == "/reject" and proposed:
            return self._reject(proposed, author)
        if name == "/set" and proposed:
            return self._set(proposed, author, rest)
        if name == "/approve" and proposed:
            self.gh.post_comment(number, (f"@{author} `/approve` received. Approving a plan (wrapping the code "
                                          "and creating the flag) is the next build step, 9b; nothing changed."))
            return "approve_not_built"

        self.gh.post_comment(number, f"@{author} `{name}` received, but nothing on this PR is waiting for it.")
        self.log(f"[github] PR #{number}: {name} from @{author} (nothing pending)")
        return f"command:{name}"

    def _reject(self, release, author):
        self.store.update_release(release["id"], state="rejected")
        self.store.add_event(release["id"], "rejected", f"plan rejected by @{author}", {"by": author})
        self._rerender(self.store.get_release(release["id"]))
        self.gh.post_comment(release["pr_number"], f"Plan rejected by @{author}. Nothing was created in Flagsmith.")
        self.log(f"[github] PR #{release['pr_number']}: plan rejected by @{author}")
        return "rejected"

    def _set(self, release, author, rest):
        number = release["pr_number"]
        parts = rest.split()
        policy, plan = _policy_of(release), json.loads(release["plan"])
        try:
            if len(parts) != 2:
                raise ValueError("use `/set risk high` or `/set <name> <value>`")
            key, value = parts[0].lower(), parts[1].lower()
            if key == "risk":
                new = for_tier(value)
                change = f"risk set to {value} by @{author} (thresholds reset to that template)"
                plan["tier_set_by"] = author
            else:
                if key not in plan_comment.SET_NAMES:
                    raise ValueError(f"`{key}` can't be set; use risk or one of: "
                                     + ", ".join(plan_comment.SET_NAMES))
                field = plan_comment.SET_NAMES[key]
                number_value = float(value.rstrip("%sx"))
                if field in INT_FIELDS:
                    number_value = int(number_value)
                new = with_overrides(policy, **{field: number_value})
                change = f"{key} set to {value} by @{author}"
        except ValueError as e:
            self.gh.post_comment(number, f"@{author} the plan wasn't changed: {e}")
            return "set_refused"
        plan["changes"] = plan.get("changes", []) + [change]
        self.store.update_release(release["id"], plan=json.dumps(plan), policy=json.dumps(asdict(new)))
        self.store.add_event(release["id"], "plan_changed", change, {"by": author})
        self._rerender(self.store.get_release(release["id"]))
        self.gh.post_comment(number, f"Plan updated: {change}. The plan comment above shows the new values.")
        self.log(f"[github] PR #{number}: {change}")
        return "set"


def _policy_of(release):
    return Policy(**json.loads(release["policy"]))