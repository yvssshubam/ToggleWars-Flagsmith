"""The fix PR (step 14): after a switch-off, apply the diagnosis's suggested fix
to the new version only, and send it through the normal approvals again.

  rolled_back, with a diagnosis whose fix is pinned to lines in the diff
    -> checks, all code, no AI:
         the lines on main are still exactly what the diagnosis saw;
         every replaced line is inside the body of `_f__new`;
         the fixed file parses and compiles;
         the old version and the switch are byte-for-byte the same shape;
         the fix actually changes the new version.
    -> branch `autopilot/fix-<flag>-<release id>` from main, one commit, a PR,
       and a release plan for it (same flag, same risk plan), waiting for /approve.
  /approve -> pending_merge (no new flag, nothing to wrap: the wrapper is on main)
  merge    -> the PR handler checks the wrapper again and deploys; the switched-off
              release's segment is removed and the SAME flag restarts from the
              internal stage; the second /approve before 100% works as usual.

The AI wrote the fix, so it gets no shortcut: it is rolled out from the
internal stage like any other change. The bot never merges and never turns the
flag on. At most one fix PR per switch-off; if the fix can't be applied safely,
the reason is posted once and the fix stays a code block in the switch-off comment.
"""

import ast
import difflib
import json
import time

from autopilot import plan_comment
from autopilot.github_client import GitHubError
from autopilot.policy import Policy
from autopilot.transformer import TransformError, _shape, _top_level_functions, wrapper_parts

FIX_PREFIX = "autopilot/fix-"
RETRY_SECONDS = 60


class FixError(Exception):
    """The suggested fix can't be applied safely; the message says why."""


def apply_fix(main_text, diagnosed_text, fix, func_name, flag):
    """Return main's file with the fix applied, or raise FixError. Pure: no I/O."""
    start, end, code = fix["start"], fix["end"], fix["code"]
    main_lines, seen_lines = main_text.split("\n"), diagnosed_text.split("\n")
    if not (1 <= start <= end <= len(main_lines)) or end > len(seen_lines):
        raise FixError(f"lines {start}-{end} aren't in the file")
    if main_lines[start - 1:end] != seen_lines[start - 1:end]:
        raise FixError(f"lines {start}-{end} changed on main since the diagnosis")
    try:
        new_fn = _top_level_functions(ast.parse(main_text)).get(f"_{func_name}__new")
    except SyntaxError as e:
        raise FixError("the file on main isn't valid Python") from e
    if new_fn is None:
        raise FixError(f"`_{func_name}__new` isn't on main")
    if not (new_fn.lineno < start and end <= new_fn.end_lineno):
        raise FixError(f"lines {start}-{end} aren't inside the body of `_{func_name}__new`; "
                       "only the new version may change")
    fixed = "\n".join(main_lines[:start - 1] + code.split("\n") + main_lines[end:])
    try:
        compile(fixed, "<fixed>", "exec")
    except SyntaxError as e:
        raise FixError(f"the fixed file isn't valid Python (line {e.lineno})") from e
    try:
        before, after = wrapper_parts(main_text, func_name, flag), wrapper_parts(fixed, func_name, flag)
    except TransformError as e:
        raise FixError(str(e)) from e
    if before != after:
        raise FixError("the fix would change the old version or the switch")
    fns_before = _top_level_functions(ast.parse(main_text))
    fns_after = _top_level_functions(ast.parse(fixed))
    if _shape(fns_before[f"_{func_name}__new"]) == _shape(fns_after[f"_{func_name}__new"]):
        raise FixError("the suggested code doesn't change what the new version does")
    others = lambda t: [ast.dump(n) for n in t.body  # noqa: E731
                        if not (isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                                and n.name == f"_{func_name}__new")]
    if others(ast.parse(main_text)) != others(ast.parse(fixed)):
        raise FixError("the fix would change code outside the new version")
    return fixed


class FixPR:
    def __init__(self, store, gh, clock=time.time, log=print, approvers=(), since=None):
        self.store, self.gh, self.clock, self.log, self.approvers = store, gh, clock, log, approvers
        self.since = clock() if since is None else since  # only switch-offs that happen while the bot runs
        self._next_try = {}

    # --- after a switch-off: open the fix PR ------------------------------------------

    def tick(self):
        for release in self.store.releases_in(("rolled_back",)):
            plan = json.loads(release.get("plan") or "{}")
            if not release.get("pr_number") or plan.get("fix_pr_number") or plan.get("fix_skipped"):
                continue
            if release["updated_at"] < self.since or self.clock() < self._next_try.get(release["id"], 0):
                continue
            diagnosis = next((e for e in reversed(self.store.events(release["id"]))
                              if e["kind"] == "diagnosis"), None)
            if diagnosis is None:
                continue
            fix = (json.loads(diagnosis["data"]) if diagnosis.get("data") else {}).get("fix")
            if not fix:
                self._skip(release, plan, "the diagnosis didn't pin its fix to lines in the diff", quiet=True)
                continue
            try:
                self._open(release, plan, fix)
            except GitHubError as e:
                self._next_try[release["id"]] = self.clock() + RETRY_SECONDS
                self.log(f"[{release['flag']}] fix PR step failed ({e}); retrying in {RETRY_SECONDS}s")

    def _skip(self, release, plan, why, quiet=False):
        plan["fix_skipped"] = why
        self.store.update_release(release["id"], plan=json.dumps(plan))
        self.store.add_event(release["id"], "fix_pr_skipped", why)
        if not quiet:
            self.gh.post_comment(release["pr_number"], (
                f"🔧 No fix PR: {why}. The suggested fix stays in the switch-off comment as a code block, and "
                f"`{release['flag']}` stays off for everyone."))
        self.log(f"[{release['flag']}] no fix PR: {why}")

    def _open(self, release, plan, fix):
        flag, number = release["flag"], release["pr_number"]
        path, name = plan["functions"][0]
        if fix["file"] != path:
            return self._skip(release, plan, f"the fix is in `{fix['file']}`, not in `{path}`")
        base = plan["base_ref"]
        main, seen = self.gh.get_file(path, base), self.gh.get_file(path, fix["sha"])
        if main is None or seen is None:
            return self._skip(release, plan, f"`{path}` couldn't be read on `{base}`")
        try:
            fixed = apply_fix(main[0], seen[0], fix, name, flag)
        except FixError as e:
            return self._skip(release, plan, str(e))

        branch = f"{FIX_PREFIX}{flag}-{release['id']}"
        self.gh.create_branch(branch, self.gh.branch_sha(base))  # False if a retry finds it already there
        on_branch = self.gh.get_file(path, branch)
        if on_branch[0] == fixed:  # committed on an earlier try
            commit = plan.get("fix_commit")
        else:
            commit = self.gh.update_file(path, fixed, on_branch[1], branch, (
                f"Release Autopilot: suggested fix for {flag}\n\nApplies the diagnosis's fix to lines "
                f"{fix['start']}-{fix['end']} of _{name}__new only (switched off in #{number}). The code was "
                "suggested by AI and applied and checked by code; it is rolled out again from the internal stage."))
            plan["fix_commit"] = commit
            self.store.update_release(release["id"], plan=json.dumps(plan))
        lines = (f"line {fix['start']}" if fix["end"] == fix["start"] else f"lines {fix['start']}–{fix['end']}")
        pr = self.gh.create_pull(f"Release Autopilot: fix for {flag} (switched off in #{number})", branch, base, (
            f"## Suggested fix for `{flag}`\n\n`{flag}` was switched off in #{number}. The bot's diagnosis "
            f"(confidence {fix.get('confidence') or 'unknown'}): {fix.get('cause') or 'see the switch-off comment'}\n\n"
            f"**What this PR changes:** {lines} of `_{name}__new` in `{path}`. {fix.get('description') or ''}\n\n"
            "The code was **suggested by AI**. The bot applied it and checked, in code, that only the new version "
            "changes, the old version and the switch are untouched, and the file parses. Review it like any change.\n\n"
            f"`{flag}` stays **off for everyone** until this PR is approved (`/approve`) **and** merged. Then the "
            "rollout restarts from the internal stage with the same plan and gates, and the release to everyone "
            "still needs a second `/approve`."))

        fix_plan = {
            "kind": "fix", "fixes_release": release["id"], "original_pr": number,
            "head_sha": commit, "base_ref": base, "title": f"Fix for {flag}",
            "functions": [[path, name]], "summary": "", "tier_ai": json.loads(release["policy"])["tier"],
            "tier_defaulted": False, "reasons": [], "notes": [], "changes": [],
            "success_metric": plan.get("success_metric", ""), "success_metric_why": "",
            "prose": {"summary": (f"Applies the diagnosis's suggested fix to the new version of `{name}`, which was "
                                  f"switched off in #{number}."),
                      "risk_explanation": (f"Same plan as the switched-off release in #{number}: same stages, soak "
                                           "times and switch-off rule."),
                      "metric_explanation": "Success metric: checkout conversion, as for the switched-off release.",
                      "from_ai": False},
            "preview": "\n".join(difflib.unified_diff(main[0].split("\n"), fixed.split("\n"), f"{path} (main)",
                                                     f"{path} (with the fix)", lineterm="")),
            "bot_commits": [commit], "old_segment_id": release.get("segment_id"),
        }
        policy = json.loads(release["policy"])
        fix_id = self.store.create_proposal(pr, flag, policy, fix_plan)
        self.store.update_release(fix_id, feature_id=release["feature_id"])
        fix_release = self.store.get_release(fix_id)
        body = plan_comment.render(fix_plan, Policy(**policy), flag, "proposed", self.gh.repo,
                                   sorted(self.approvers))
        self.store.update_release(fix_id, plan_comment_id=self.gh.post_comment(pr, body))
        self.store.add_event(fix_id, "proposed", f"fix plan proposed for {flag} (from #{number})")

        plan["fix_pr_number"] = pr
        self.store.update_release(release["id"], plan=json.dumps(plan))
        self.store.add_event(release["id"], "fix_pr_opened", f"fix PR #{pr} opened")
        self.gh.post_comment(number, (
            f"🔧 **Fix PR #{pr} opened** with the diagnosis's suggested fix (new version only). `{flag}` stays off "
            "until it's approved and merged; then the rollout restarts from the internal stage."))
        self.log(f"[{flag}] fix PR #{pr} opened (release #{fix_release['id']}, waiting for /approve)")

    # --- the fix PR's own events, routed here by the PR handler --------------------------

    def on_pr(self, ev, release):
        """Handle a fix PR's opened/synchronize/closed events. Returns None when the
        PR handler should carry on itself (an approved fix PR being merged)."""
        plan, number, flag = json.loads(release["plan"]), ev["number"], release["flag"]
        state = release["state"]
        if ev["action"] == "closed":
            if ev["merged"] and state == "pending_merge":
                return None  # the normal merge path: wrapper check, deploy, restart the rollout
            if state not in ("proposed", "pending_merge"):
                return "fix_pr_closed"
            self.store.update_release(release["id"], state="abandoned")
            self.store.add_event(release["id"], "abandoned", "fix PR closed" + (" (merged unapproved)" if ev["merged"] else ""))
            plan_comment.refresh(self.gh, self.store.get_release(release["id"]), self.approvers)
            if ev["merged"]:
                self.gh.post_comment(number, (
                    f"⚠️ This fix was merged **without an approved plan**, so no rollout was started. The fix is on "
                    f"main inside the new version, which `{flag}` keeps switched **off**, so users aren't affected."))
            else:
                self.gh.post_comment(number, (f"Fix PR closed without merging. `{flag}` stays off for everyone; "
                                              f"the release switched off in #{plan['original_pr']} stays switched off."))
            self.log(f"[{flag}] fix PR #{number} closed ({'merged unapproved' if ev['merged'] else 'not merged'})")
            return "fix_pr_closed"
        if ev["head_sha"] in plan.get("bot_commits", []):
            return "own_commit"
        if state != "proposed":
            return None  # after approval: the handler's usual "new commits after approval" warning
        return self._recheck(ev, release, plan)

    def _recheck(self, ev, release, plan):
        """A person pushed to a fix PR before /approve: it may only change the new version."""
        path, name = plan["functions"][0]
        main, head = self.gh.get_file(path, plan["base_ref"]), self.gh.get_file(path, ev["head_sha"])
        problem = None
        try:
            if main is None or head is None:
                problem = f"`{path}` couldn't be read"
            elif wrapper_parts(main[0], name, release["flag"]) != wrapper_parts(head[0], name, release["flag"]):
                problem = "the new commits change the old version or the switch"
        except TransformError as e:
            problem = str(e)
        if problem:
            self.store.update_release(release["id"], state="abandoned")
            self.store.add_event(release["id"], "abandoned", f"fix plan dropped: {problem}")
            plan_comment.refresh(self.gh, self.store.get_release(release["id"]), self.approvers)
            self.gh.post_comment(ev["number"], f"Fix plan dropped: {problem}. `{release['flag']}` stays off.")
            return "fix_plan_dropped"
        plan["head_sha"] = ev["head_sha"]
        plan["preview"] = "\n".join(difflib.unified_diff(main[0].split("\n"), head[0].split("\n"),
                                                         f"{path} (main)", f"{path} (this PR)", lineterm=""))
        plan["updates"] = plan.get("updates", 0) + 1
        self.store.update_release(release["id"], plan=json.dumps(plan))
        self.store.add_event(release["id"], "plan_updated", f"fix plan re-checked at {ev['head_sha'][:7]}")
        plan_comment.refresh(self.gh, self.store.get_release(release["id"]), self.approvers)
        return "fix_plan_updated"