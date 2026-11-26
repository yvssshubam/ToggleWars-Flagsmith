"""Renders the release-plan comment posted on a PR.

Words come from the AI (already checked by llm.py); every number comes from
the policy template here. The comment is re-rendered whenever the plan
changes (new commits, /set, /reject), so it always shows the current plan.
"""

from autopilot.policy import STAGES

MAX_PREVIEW_LINES = 120
def status_line(state, plan, flag, repo):
    by = plan.get("approved_by")
    commits = ", ".join(f"[`{sha[:7]}`](https://github.com/{repo}/commit/{sha})"
                        for sha in plan.get("bot_commits", []))
    if state == "proposed":
        return "⏳ **Waiting for approval.** Nothing has been created or changed in Flagsmith."
    if state == "approving":
        return (f"🔧 **Approved by @{by}; being carried out.** If this doesn't change to ✅ within a "
                "minute, the bot hit an error (see its reply below); comment `/approve` again to retry.")
    if state == "pending_merge":
        return (f"✅ **Approved by @{by}.** Flag `{flag}` created in Flagsmith, **off for everyone**. "
                f"Wrapped code committed to this PR ({commits}). **The rollout starts when this PR is merged.**")
    if state == "rejected":
        return "❌ **Rejected.** Nothing was created in Flagsmith."
    if state == "abandoned":
        if plan.get("flag_deleted"):
            return "⚪ **Closed.** The flag was deleted from Flagsmith; nothing reached users."
        return "⚪ **Closed.** Nothing was created in Flagsmith."
    return f"**State:** `{state}`"
SET_NAMES = {   # what reviewers type -> Policy field
    "max_error_rate": "max_error_rate_pct",
    "max_error_increase": "max_error_increase_pp",
    "max_p95_ratio": "max_p95_ratio",
    "min_p95_increase_ms": "min_p95_increase_ms",
    "min_requests": "min_new_requests",
    "soak": "soak_seconds",
}


def line_link(repo, sha, file, line):
    return f"[`{file}:{line}`](https://github.com/{repo}/blob/{sha}/{file}#L{line})"


def render(plan, policy, flag, state, repo, approvers):
    p = plan
    prose = p["prose"]
    out = [f"## Release Autopilot: release plan for `{flag}`", "", status_line(state, p, flag, repo), ""]
    out += [prose["summary"], ""]

    # --- risk -----------------------------------------------------------------
    tier = policy.tier
    tier_line = f"### Risk: **{tier.upper()}**"
    if p.get("tier_set_by"):
        tier_line += f" (set by @{p['tier_set_by']}; the AI suggested {p['tier_ai']})"
    elif p["tier_defaulted"]:
        tier_line += " (default: the AI's reasons couldn't be verified)"
    explanation = prose["risk_explanation"]
    if p.get("tier_set_by"):
        explanation = f"The AI's reasoning for its suggestion: {explanation}"
    out += [tier_line, explanation, ""]
    for r in p["reasons"]:
        out.append(f"- {line_link(repo, p['head_sha'], r['file'], r['line'])}: {r['claim']}")
    if p["reasons"]:
        out.append("")

    # --- what gets flagged --------------------------------------------------------
    out.append("### What gets put behind the flag")
    for file, name in p["functions"]:
        out.append(f"- `{name}` in `{file}`: the old and the new version both ship, and the flag "
                   f"`{flag}` decides per user which one runs. Everyone gets the old version until the rollout starts.")
    preview = p["preview"].split("\n")
    shown = preview[:MAX_PREVIEW_LINES]
    if len(preview) > MAX_PREVIEW_LINES:
        shown.append(f"... ({len(preview) - MAX_PREVIEW_LINES} more lines)")
    out += ["", "<details><summary>Preview: the code the bot will commit to this PR after <code>/approve</code></summary>",
            "", "```diff", *shown, "```", "", "</details>", ""]

    # --- rollout (numbers from the policy) -----------------------------------------
    gate = f"{policy.soak_seconds}s with no gate failing, and at least {policy.min_new_requests} new-path requests"
    out += [f"### Rollout ({tier} risk template)", "",
            "| Stage | Who gets the new code | Moves on when |", "|---|---|---|"]
    for i, (_, pct) in enumerate(STAGES):
        who = "internal staff only" if pct == 0 else f"internal staff + {pct}% of users"
        out.append(f"| {i} | {who} | {gate} |")
    out += ["| Approval | (unchanged) | a second `/approve` from an approver |",
            f"| {len(STAGES)} | everyone | {policy.soak_seconds}s with no gate failing → released |", ""]

    out += ["### Switch-off rule (part of what you approve)",
            "At any stage, the bot turns the flag **off for everyone**, without asking, if the new path has:",
            f"- an error rate above **{policy.max_error_rate_pct:g}%**, or more than "
            f"**{policy.max_error_increase_pp:g} percentage point{'' if policy.max_error_increase_pp == 1 else 's'}**"
            " above the old path's, or",
            f"- p95 latency above **{policy.max_p95_ratio:g}×** the old path's "
            f"(and at least {policy.min_p95_increase_ms:g} ms slower).",
            "It then posts the numbers here, followed by a diagnosis.", ""]

    out += ["### Success metric", prose["metric_explanation"]
            + " It's compared between the new and old paths before the release to everyone.", ""]

    notes = list(p["notes"])
    if p.get("updates"):
        notes.append(f"Re-checked after new commits (now at `{p['head_sha'][:7]}`).")
    if notes:
        out += ["<details><summary>Checks the bot made on the AI's answer</summary>", ""]
        out += [f"- {n}" for n in notes] + ["", "</details>", ""]
    if p.get("changes"):
        out += ["**Changes to this plan:** " + "; ".join(p["changes"]), ""]

    if state == "proposed":
        who = ", ".join(f"@{a}" for a in approvers) or "nobody (ALLOWED_APPROVERS is empty)"
        names = ", ".join(f"`{n}`" for n in SET_NAMES)
        out += [f"**Commands** (approvers: {who}):",
                "- `/approve`: wrap the code as previewed, commit it to this PR, and create the flag (off).",
                "- `/reject`: drop this plan.",
                "- `/set risk low|medium|high`: switch to another risk template.",
                f"- `/set <name> <value>` to change one threshold ({names}); safety bounds can't be loosened.",
                ""]
    out.append(f"<sub>AI wording: {'yes' if prose.get('from_ai') else 'no (plain text from code)'} · "
               "numbers: from the policy template, not the AI.</sub>")
    return "\n".join(out)