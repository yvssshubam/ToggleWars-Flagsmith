"""Announces a release's progress on its PR, and adds the diagnosis after a switch-off.

The engine calls this after it has acted (`Engine.notify`), so a GitHub or
Groq problem here can delay a comment but never a stage change or a switch-off.
Every number in these comments comes from the engine's measurements; the only
AI text is the diagnosis, which llm.py has already checked against the diff.
"""

import json

from autopilot import plan_comment
from autopilot.engine import STAGE_LABEL
from autopilot.health import PathStats
from autopilot.llm import diagnose
from autopilot.policy import Policy
from autopilot.transformer import function_source

STAGE_NUMBER = {"stage_0": 0, "stage_1": 1, "stage_2": 2}


def stats(d):
    """'60 requests, 33.3% errors, p95 5 ms' (numbers measured by the engine)."""
    if not d:
        return "no data"
    s = PathStats(**d)
    return f"{s.n} requests, {s.error_rate:.1%} errors, p95 {s.p95_ms:.0f} ms"


class PRNotifier:
    def __init__(self, store, gh, llm=None, log=print):
        self.store, self.gh, self.llm, self.log = store, gh, llm, log

    def __call__(self, release_id, kind, message, data):
        release = self.store.get_release(release_id)
        if not release or not release.get("pr_number"):
            return  # a CLI release: nothing to announce
        plan = json.loads(release["plan"] or "{}")
        policy = Policy(**json.loads(release["policy"]))
        body = getattr(self, f"_{kind}")(release, plan, policy, data)
        comment_id = self.gh.post_comment(release["pr_number"], body)
        plan_comment.refresh(self.gh, release)  # the status line at the top follows the rollout
        if kind == "rolled_back":
            self._add_diagnosis(release, plan, data, body, comment_id)

    # --- one comment per event -------------------------------------------------

    def _stage(self, release, plan, policy, data):
        stage = data.get("stage", release["state"])
        gate = (f"Moves on after {policy.soak_seconds}s with no gate failing and at least "
                f"{policy.min_new_requests} new-path requests.")
        if stage == "stage_0":
            return (f"🚀 **Rollout started** for `{release['flag']}` ({policy.tier} risk plan).\n\n"
                    f"**Stage 0:** new code on for **{STAGE_LABEL['stage_0']}**. {gate}")
        return (f"**Stage {STAGE_NUMBER[stage]}:** new code on for **{STAGE_LABEL[stage]}**. {gate}\n\n"
                f"Previous stage, measured: new path {stats(data.get('new'))}; old path {stats(data.get('old'))}.")

    def _awaiting_approval(self, release, plan, policy, data):
        return ("✅ **All stages healthy. Release to everyone?**\n\n"
                f"At {STAGE_LABEL['stage_2']}: new path {stats(data.get('new'))}; old path {stats(data.get('old'))}.\n\n"
                "An approver can comment **`/approve`** to turn the new code on for everyone, or "
                "`/rollback` to switch it off. Nothing changes until then.")

    def _full(self, release, plan, policy, data):
        return (f"**Released to everyone**, approved by @{data.get('by')}. The bot keeps watching for "
                f"{policy.soak_seconds}s, against the old path's numbers from stage 2; the switch-off rule "
                "still applies.")

    def _released(self, release, plan, policy, data):
        return (f"🎉 **Released.** `{release['flag']}` has been on for everyone for {policy.soak_seconds}s "
                f"with no gate failing (new path {stats(data.get('new'))}). Next, the bot opens a cleanup PR "
                "that removes the flag code.")

    def _rollback_failed(self, release, plan, policy, data):
        return (f"🚨 **Switch-off FAILED**: {data.get('error')}. The bot retries every few seconds. "
                f"If this doesn't clear, turn `{release['flag']}` off in the Flagsmith dashboard now.")

    def _rolled_back(self, release, plan, policy, data):
        stage = data.get("stage", "")
        lines = [f"🛑 **Switched off.** `{release['flag']}` is now **off for everyone**: everyone gets the old code.",
                 "", f"**Why:** {data.get('reason')}"]
        if stage in STAGE_LABEL:
            lines.append(f"**Stage:** {STAGE_LABEL[stage]}")
        if data.get("new"):
            lines += ["", "| Path | Measured |", "|---|---|",
                      f"| new | {stats(data['new'])} |", f"| old | {stats(data['old'])} |"]
        kinds = {}
        for s in data.get("error_samples", []):
            key = (s.get("error_type"), s.get("error_where"))
            kinds[key] = kinds.get(key, 0) + 1
        if kinds:
            lines += ["", "Errors on the new path (latest samples):"]
            lines += [f"- {n} × `{etype}` at `{where or 'unknown location'}`"
                      for (etype, where), n in sorted(kinds.items(), key=lambda kv: -kv[1])]
        lines += ["", "This is the switch-off rule approved with the plan; no one had to act."]
        if data.get("new"):
            lines += ["", "_Diagnosis: working on it…_"]
        return "\n".join(lines)

    # --- diagnosis (after the switch-off, never before) ----------------------------

    def _add_diagnosis(self, release, plan, data, body, comment_id):
        if not data.get("new"):
            return  # a manual /rollback: no failing measurements to explain
        try:
            section = self._diagnosis_section(release, plan, data)
        except Exception as e:  # noqa: BLE001 - the numbers comment stays whatever happens here
            section = f"### Diagnosis\nDiagnosis unavailable ({type(e).__name__}: {e}). The numbers above stand."
        self.gh.edit_comment(comment_id, body.replace("_Diagnosis: working on it…_", section))
        self.store.add_event(release["id"], "diagnosis", section[:2000])
        self.log(f"[{release['flag']}] diagnosis added to the switch-off comment on PR #{release['pr_number']}")

    def _diagnosis_section(self, release, plan, data):
        if self.llm is None:
            return "### Diagnosis\nDiagnosis unavailable (GROQ_API_KEY isn't set). The numbers above stand."
        number, flag = release["pr_number"], release["flag"]
        files = self.gh.list_pull_files(number)
        sha = (plan.get("bot_commits") or [plan.get("head_sha")])[-1]
        path, name = plan["functions"][0]
        merged = self.gh.get_file(path, sha)
        text = merged[0] if merged else ""
        old_src = function_source(text, f"_{name}__old")
        new_src = function_source(text, f"_{name}__new")
        verdict_text = f"new path {stats(data.get('new'))}; old path {stats(data.get('old'))}"
        d = diagnose(self.llm, files, data.get("reason", ""), verdict_text, data.get("error_samples", []),
                     name, old_src, new_src)
        if not d.available:
            return f"### Diagnosis\nDiagnosis unavailable ({d.error}). The numbers above stand."
        link = lambda f, n: f"[`{f}:{n}`](https://github.com/{self.gh.repo}/blob/{sha}/{f}#L{n})"  # noqa: E731
        out = ["### Diagnosis (AI, every cited line checked against this PR's diff)", "",
               f"**Likely cause:** {d.likely_cause}", ""]
        out += [f"- {link(c.file, c.line)}: {c.claim}" for c in d.evidence]
        out += ["", f"**Confidence:** {d.confidence}"]
        if d.fix_description or d.fix_code:
            out += ["", f"**Suggested fix:** {d.fix_description}"]
            if d.fix_code:
                where = (f" (replaces {link(d.fix_file, d.fix_start)}"
                         + (f"–{d.fix_end}" if d.fix_end != d.fix_start else "") + ")") if d.fix_file else ""
                out += [f"Proposed code{where}:", "```python", d.fix_code, "```"]
        if d.notes:
            out += ["", "<details><summary>Checks the bot made on the AI's answer</summary>", ""]
            out += [f"- {n}" for n in d.notes] + ["", "</details>"]
        out += ["", f"<sub>The diagnosis only explains; it never changes the flag. `{flag}` stays off "
                    "until a new PR goes through the plan and approvals again.</sub>"]
        return "\n".join(out)