"""Acts on GitHub events queued by the webhook. Runs inside the bot loop, so
the webhook can answer GitHub immediately (GitHub gives up after 10 s).

Phase A (steps 9a-9b):
  1. code finds the top-level functions the PR really changed (transformer);
  2. the AI triages (llm.triage), and code checks its answer;
  3. code builds the wrapped file as a preview (transformer.wrap);
  4. the plan comment is posted (words from the AI, numbers from the policy),
     and the plan is stored as `proposed`. Nothing happens in Flagsmith.
  5. /approve from an approver: re-check that the PR and main still match the
     plan, create the flag (off for everyone), and commit the wrapped code to
     the PR branch. State `pending_merge`; the rollout starts on merge (9c).
Phase B start (step 9c):
  6. merged: check on main that the old version and the switch are exactly as
     approved (commits after approval may only change the new version), "deploy"
     with `git pull --ff-only` in the shop's folder, and start stage 0. The
     engine announces every stage on the PR (pr_notifier.py).
  7. `/approve` while the release waits at 25%: release to everyone.
     `/rollback` at any stage: switch the flag off now.
New commits re-check a proposed plan and edit the same comment. Approvers can
/reject it or /set the risk tier and thresholds. Closing an approved PR
without merging deletes its flag.

/approve is safe to repeat: each part (flag, each file's commit) is recorded
as it's done, so a retry after an error carries on where it stopped.

The PR's code is never run by the bot (this process holds the Flagsmith and
GitHub keys); it's only parsed and compiled.
"""

import difflib
import json
import subprocess
from dataclasses import asdict

from autopilot import plan_comment
from autopilot.flagsmith_client import FlagsmithError
from autopilot.github_client import GitHubError
from autopilot.llm import Triage, plan_prose, triage
from autopilot.policy import Policy, for_tier, with_overrides
from autopilot.transformer import TransformError, changed_functions, wrap, wrapper_parts

MANAGED_PREFIX = "demo_shop/"
COMMANDS = {
    "/approve": "approve the plan, or the release to everyone",
    "/reject": "reject the proposed plan",
    "/rollback": "switch the flag off now",
    "/pause": "stop advancing stages",
    "/set": "change the plan before approval, e.g. /set risk high",
}
INT_FIELDS = ("soak_seconds", "min_new_requests")
ROLLOUT_STATES = ("stage_0", "stage_1", "stage_2", "awaiting_approval", "full")


def git_pull(repo_dir="."):
    """'Deploy' for the demo: fast-forward the local clone the shop runs from
    (uvicorn --reload picks up the change). Returns (ok, output)."""
    try:
        r = subprocess.run(["git", "pull", "--ff-only"], cwd=repo_dir, capture_output=True,
                           text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)
    return r.returncode == 0, (r.stdout + r.stderr).strip()[-500:]


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
    def __init__(self, store, gh, approvers, llm=None, fs=None, engine=None, deploy=git_pull, log=print):
        self.store, self.gh, self.llm, self.fs, self.log = store, gh, llm, fs, log
        self.engine, self.deploy = engine, deploy
        self.approvers = {a.strip().lower() for a in approvers if a.strip()}

    def process_pending(self):
        for row in self.store.pending_github_events():
            event = row["event"]
            try:
                result = self.handle(event)
            except (GitHubError, FlagsmithError) as e:
                retrying = self.store.fail_github_event(row["id"], str(e))
                where = "GitHub" if isinstance(e, GitHubError) else "Flagsmith"
                self.log(f"[github] PR #{event.get('number')}: {where} call failed ({e}); "
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
        if release and release["state"] in ("approving", "pending_merge"):
            return self._commits_after_approval(ev, release)
        if release and release["state"] not in ("proposed", "rejected", "abandoned"):
            self.log(f"[github] PR #{number}: {action} ignored (release is {release['state']})")
            return "ignored_after_approval"
        if release and release["state"] == "rejected" and action != "reopened":
            self.log(f"[github] PR #{number}: new commits on a rejected plan; ignored")
            return "ignored_rejected"
        if release and release["state"] == "abandoned" and action != "reopened":
            return "ignored_closed"
        return self._propose(ev, release if release and release["state"] == "proposed" else None)

    def _commits_after_approval(self, ev, release):
        number, plan = ev["number"], json.loads(release["plan"])
        if ev["action"] != "synchronize" or ev["head_sha"] in plan.get("bot_commits", []):
            return "own_commit"  # the bot's wrapper commit
        if plan.get("changed_after_approval") == ev["head_sha"]:
            return "already_warned"
        plan["changed_after_approval"] = ev["head_sha"]
        self.store.update_release(release["id"], plan=json.dumps(plan))
        self.store.add_event(release["id"], "changed_after_approval",
                             f"new commit {ev['head_sha'][:7]} after the plan was approved")
        self.gh.post_comment(number, (
            f"⚠️ **New commits after approval** (now at `{ev['head_sha'][:7]}`). They weren't part of "
            "the approved plan. Before the rollout starts, the bot checks that the old version, the "
            "switch and the flag are still exactly as approved; if not, it won't start the rollout."))
        self.log(f"[github] PR #{number}: human commit after approval; warned")
        return "changed_after_approval"

    def _on_closed(self, ev, release):
        number = ev["number"]
        if release and release["state"] in ("approving", "pending_merge"):
            return self._closed_after_approval(ev, release)
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

    def _closed_after_approval(self, ev, release):
        number, plan = ev["number"], json.loads(release["plan"])
        if ev["merged"]:
            return self._merged(ev, release, plan)
        if plan.get("feature_id"):
            if self.fs is None:
                raise FlagsmithError("delete flag", None, "Flagsmith isn't configured in the bot")
            try:
                self.fs.delete_feature(plan["feature_id"])
            except FlagsmithError as e:
                if e.status_code != 404:  # 404: already gone
                    raise
            plan["flag_deleted"] = True
        self.store.update_release(release["id"], state="abandoned", plan=json.dumps(plan))
        self.store.add_event(release["id"], "abandoned", "approved PR closed without merge; flag deleted")
        self._rerender(self.store.get_release(release["id"]))
        self.gh.post_comment(number, (f"PR closed without merging, so the flag `{release['flag']}` was "
                                      "deleted from Flagsmith. Nothing reached users."))
        self.log(f"[github] PR #{number}: closed after approval; flag {release['flag']} deleted")
        return "abandoned_after_approval"

    def _merged(self, ev, release, plan):
        number, flag = ev["number"], release["flag"]
        if release["state"] == "approving":
            return self._block(release, "the PR was merged before the approval finished (the wrapper "
                                        "may not have been committed)")
        if self.engine is None:
            return self._block(release, "the bot's release engine isn't running")
        # 1. main must hold the wrapper exactly as approved (the new version may have changed).
        problems = []
        approved_sha = plan["bot_commits"][-1]
        for path in sorted({p for p, _ in plan["functions"]}):
            merged, approved = self.gh.get_file(path, plan["base_ref"]), self.gh.get_file(path, approved_sha)
            for _, name in sorted(fn for fn in plan["functions"] if fn[0] == path):
                try:
                    now = wrapper_parts(merged[0] if merged else "", name, flag)
                    then = wrapper_parts(approved[0], name, flag)
                except TransformError as e:
                    problems.append(f"`{path}`: {e}")
                    continue
                if now["old"] != then["old"]:
                    problems.append(f"`_{name}__old` in `{path}` isn't the approved old version any more")
                if now["dispatcher"] != then["dispatcher"]:
                    problems.append(f"the switch `{name}` in `{path}` isn't the approved one any more")
        if problems:
            return self._block(release, "the merged code doesn't match the approved wrapper: "
                                        + "; ".join(problems))
        # 2. "Deploy": the shop runs from a local clone and reloads on change.
        ok, output = self.deploy()
        self.store.add_event(release["id"], "deployed" if ok else "deploy_failed", output[:300])
        self.gh.post_comment(number, (
            "Merged. **Deploy:** `git pull` in the shop's folder "
            + ("succeeded." if ok else f"**failed**:\n```\n{output}\n```\nRun `git pull` there yourself. "
               "Until the shop runs the new code, the rollout simply waits for traffic.")))
        # 3. Stage 0. The engine announces it on the PR.
        self.engine.start_release(flag, None, started_by=f"merge of PR #{number}", release_id=release["id"])
        self.log(f"[github] PR #{number}: merged; deploy {'ok' if ok else 'FAILED'}; rollout of {flag} started")
        return "rollout_started"

    def _block(self, release, why):
        """Don't start a rollout that doesn't match what was approved. The flag stays off."""
        self.store.update_release(release["id"], state="error")
        self.store.add_event(release["id"], "blocked", why)
        self.gh.post_comment(release["pr_number"], (
            f"⛔ **Rollout not started:** {why}. The flag `{release['flag']}` stays **off for everyone**, "
            "so everyone keeps getting the old version. Fix it in a new PR."))
        self.log(f"[github] PR #{release['pr_number']}: rollout blocked ({why})")
        return "blocked"

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
        sources, changed, problems, base_blobs, invalid = {}, {}, [], {}, []
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
                invalid.append(path)
                continue
            if names:
                sources[path], changed[path], base_blobs[path] = (base[0], head[0]), names, base[1]
        if not changed and invalid:
            return self._cannot_flag(number, existing, (
                "a changed file isn't valid Python, so there's nothing safe to wrap: "
                + ", ".join(f"`{p}`" for p in invalid)), problems)
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
            "base_blobs": base_blobs,
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
        """A name no other release uses and that doesn't exist in Flagsmith yet."""
        taken = set()
        if self.fs is not None:
            taken = {f["name"] for f in self.fs.list_features()}
        base, candidate, n = name[:56], name, 2
        while candidate in taken or self.store.flag_in_use(candidate, except_pr=number):
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
        state = release["state"] if release else None
        if name == "/reject" and state == "proposed":
            return self._reject(release, author)
        if name == "/set" and state == "proposed":
            return self._set(release, author, rest)
        if name == "/approve" and state in ("proposed", "approving"):
            return self._approve(release, author)
        if state in ROLLOUT_STATES and name in ("/approve", "/rollback"):
            return self._rollout_command(release, name, author)
        if name == "/approve" and state == "pending_merge":
            by = json.loads(release["plan"]).get("approved_by")
            self.gh.post_comment(number, f"@{author} the plan was already approved by @{by}. "
                                         "The rollout starts when this PR is merged.")
            return "already_approved"

        self.gh.post_comment(number, f"@{author} `{name}` received, but nothing on this PR is waiting for it.")
        self.log(f"[github] PR #{number}: {name} from @{author} (nothing pending)")
        return f"command:{name}"

    def _approve(self, release, author):
        number, flag = release["pr_number"], release["flag"]
        if self.fs is None:
            self.gh.post_comment(number, f"@{author} the bot isn't connected to Flagsmith, so it can't "
                                         "create the flag. Nothing changed.")
            return "no_flagsmith"
        if release["state"] == "proposed":
            if not self.store.transition(release["id"], ("proposed",), "approving"):
                return "approve_race"  # another /approve won
            plan = json.loads(release["plan"])
            plan["approved_by"] = author
            self.store.update_release(release["id"], plan=json.dumps(plan))
            self.store.add_event(release["id"], "plan_approved", f"plan approved by @{author}", {"by": author})
            release = self.store.get_release(release["id"])
            self._rerender(release)
        plan = json.loads(release["plan"])
        done = plan.setdefault("committed", {})    # path -> commit sha, as each one lands

        # 1. The PR and main must still be what the plan was made from.
        pr = self.gh.get_pull(number)
        if pr.get("state") != "open" or pr.get("merged"):
            return self._back_to_proposed(release, "the PR isn't open any more")
        head_repo = ((pr.get("head") or {}).get("repo") or {}).get("full_name")
        if head_repo != self.gh.repo:
            return self._back_to_proposed(release, "the PR comes from a fork, and the bot can only commit "
                                                   f"to branches in {self.gh.repo}")
        branch = pr["head"]["ref"]
        if not done and pr["head"]["sha"] != plan["head_sha"]:
            return self._back_to_proposed(release, (
                f"the PR has new commits since this plan was made (now at `{pr['head']['sha'][:7]}`). "
                "The plan is being re-checked; approve the updated plan"))

        # 2. Build every wrapped file before changing anything (parse + compile only, never run).
        to_commit = []
        for path in sorted({p for p, _ in plan["functions"]}):
            if path in done:
                continue
            base, head = self.gh.get_file(path, plan["base_ref"]), self.gh.get_file(path, plan["head_sha"])
            if base is None or head is None:
                return self._back_to_proposed(release, f"`{path}` is missing on `{plan['base_ref']}` or the PR")
            if plan.get("base_blobs", {}).get(path, base[1]) != base[1]:
                return self._back_to_proposed(release, (
                    f"`{path}` changed on `{plan['base_ref']}` since this plan was made, so the old version "
                    "in the preview is out of date. Update the PR branch from main, and approve the new plan"))
            try:
                wrapped = head[0]
                for _, name in sorted(fn for fn in plan["functions"] if fn[0] == path):
                    wrapped = wrap(base[0], wrapped, name, flag)
            except TransformError as e:
                return self._back_to_proposed(release, f"the code can't be wrapped any more: {e}")
            to_commit.append((path, wrapped, head[1]))

        # 3. The flag, off for everyone.
        try:
            if not plan.get("feature_id"):
                if self.fs.get_feature_by_name(flag) is not None:
                    return self._back_to_proposed(release, (
                        f"a flag named `{flag}` already exists in Flagsmith, and the bot won't take over a "
                        "flag it didn't create. Delete it in Flagsmith or close and reopen this PR"))
                plan["feature_id"] = self.fs.create_feature(
                    flag, f"Release Autopilot, PR #{number}: {plan.get('title', '')[:120]}")
                self.store.update_release(release["id"], feature_id=plan["feature_id"], plan=json.dumps(plan))
                self.store.add_event(release["id"], "flag_created", f"created flag {flag} (off for everyone)")
        except FlagsmithError as e:
            self.gh.post_comment(number, (f"@{author} Flagsmith error while creating the flag ({e}). "
                                          "Nothing was committed. Comment `/approve` to try again."))
            self.log(f"[github] PR #{number}: approve stopped, Flagsmith error: {e}")
            return "approve_flagsmith_error"

        # 4. Commit the wrapped code to the PR branch.
        for path, text, blob in to_commit:
            names = ", ".join(f"`{n}`" for p, n in plan["functions"] if p == path)
            sha = self.gh.update_file(path, text, blob, branch, (
                f"Release Autopilot: put {names.replace('`', '')} behind flag {flag}\n\n"
                f"Approved by @{author} in #{number}. The old and new versions both ship; the flag "
                "picks one per user. Generated by autopilot/transformer.py (no AI)."))
            done[path] = sha
            plan.setdefault("bot_commits", []).append(sha)
            self.store.update_release(release["id"], plan=json.dumps(plan))
            self.store.add_event(release["id"], "committed", f"committed wrapped {path} as {sha[:7]}")

        # 5. Waiting for the merge.
        self.store.transition(release["id"], ("approving",), "pending_merge")
        release = self.store.get_release(release["id"])
        self._rerender(release)
        links = ", ".join(f"[`{s[:7]}`](https://github.com/{self.gh.repo}/commit/{s})" for s in plan["bot_commits"])
        self.gh.post_comment(number, (
            f"Plan approved by @{plan['approved_by']}.\n"
            f"- Flag `{flag}` created in Flagsmith, **off for everyone**.\n"
            f"- Wrapped code committed to `{branch}`: {links}.\n\n"
            "**The rollout starts when this PR is merged.** Until then, nothing reaches users."))
        self.log(f"[github] PR #{number}: approved by @{plan['approved_by']}; flag {flag} created (off); "
                 f"wrapper committed ({len(plan['bot_commits'])} commit(s)); waiting for merge")
        return "approved"

    def _rollout_command(self, release, name, author):
        number, flag = release["pr_number"], release["flag"]
        if self.engine is None:
            self.gh.post_comment(number, f"@{author} the bot's release engine isn't running; nothing changed.")
            return "no_engine"
        if name == "/rollback":
            result = self.engine.request_rollback(flag, by=f"@{author}")
            reply = (f"@{author} switch-off requested: the flag goes off for everyone within a few seconds."
                     if result.startswith("rollback requested") else f"@{author} {result}.")
        elif release["state"] != "awaiting_approval":
            reply = (f"@{author} the rollout is at {release['state'].replace('_', ' ')}; the release to everyone "
                     "can only be approved once all stages are healthy. The bot will ask here.")
        else:
            result = self.engine.approve(flag, by=author)
            reply = (f"@{author} approved: the new code goes on for everyone within a few seconds."
                     if result.startswith("approved by") else f"@{author} {result}.")
        self.gh.post_comment(number, reply)
        self.log(f"[github] PR #{number}: {name} from @{author}: {reply}")
        return f"rollout:{name}"

    def _back_to_proposed(self, release, why):
        """Undo the start of an approval that couldn't go ahead. Only used before
        anything was created or committed."""
        plan = json.loads(release["plan"])
        if plan.get("feature_id") or plan.get("committed"):
            self.gh.post_comment(release["pr_number"], f"Approval stopped: {why}. Comment `/approve` to retry.")
            return "approve_stopped"
        plan.pop("approved_by", None)
        self.store.update_release(release["id"], plan=json.dumps(plan))
        self.store.transition(release["id"], ("approving",), "proposed")
        self.store.add_event(release["id"], "approval_undone", why)
        self._rerender(self.store.get_release(release["id"]))
        self.gh.post_comment(release["pr_number"], f"Approval not carried out: {why}. Nothing was created or committed.")
        self.log(f"[github] PR #{release['pr_number']}: approval not carried out ({why})")
        return "approve_refused"

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