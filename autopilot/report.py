"""The release report (step 13): one summary comment per release, from the event log.

Posted on the release's PR when it completes (released and cleaned up) or is
switched off (after the diagnosis has been added, so the report can point to
it). It covers the outcome, the timeline, every approval (who, when, how),
the health numbers per stage, the conversion result, and what the bot did
instead of a person.

Every number comes from code (the event log and the metrics). The AI writes
only the two opening sentences, which may not contain numbers (llm.py checks).

Only releases that finish while the bot runs get a report automatically, so
starting the bot doesn't post on old PRs. `python -m autopilot report <PR>`
previews any release's report, and `--post` posts it.
"""

import json
import time

from autopilot import experiment
from autopilot.engine import STAGE_LABEL
from autopilot.github_client import GitHubError
from autopilot.health import PathStats
from autopilot.llm import release_summary
from autopilot.policy import Policy

DIAGNOSIS_WAIT_SECONDS = 90   # how long a switched-off release waits for its diagnosis
RETRY_SECONDS = 60
OUTCOMES = ("completed", "rolled_back")

STEP_TEXT = {
    "proposed": "Release plan proposed on the PR",
    "plan_changed": "Plan changed",
    "plan_approved": "Plan approved (approval 1)",
    "flag_created": "Flag created in Flagsmith, off for everyone",
    "committed": "Flag wrapper committed to the PR",
    "deployed": "Merged and deployed (`git pull`)",
    "deploy_failed": "Merged; deploy failed",
    "awaiting_approval": "All stages healthy; asked for the release to everyone",
    "approved": "Release to everyone approved (approval 2)",
    "full": "New code on for everyone",
    "released": "Released: stable at 100%",
    "cleanup_pr_opened": "Cleanup PR opened",
    "cleanup_merged": "Cleanup PR merged (approval 3)",
    "completed": "Flag and segment deleted; done",
    "rollback_requested": "Switch-off requested",
    "rolled_back": "Switched off for everyone",
    "diagnosis": "AI diagnosis added to the switch-off comment",
}


def _data(e):
    return json.loads(e["data"]) if e.get("data") else {}


def _clock(ts):
    return time.strftime("%H:%M:%S", time.localtime(ts))


def _duration(seconds):
    seconds = int(round(seconds))
    if seconds < 60:
        return f"{seconds} s"
    minutes, s = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} min {s} s" if s else f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes} min"


def _stats(d):
    if not d:
        return "–"
    s = PathStats(**d)
    return f"{s.n} requests, {s.error_rate:.1%} errors, p95 {s.p95_ms:.0f} ms"


def _who(by):
    if not by:
        return "someone"
    if by == "dashboard":
        return "the dashboard"
    return by if by.startswith("@") or " " in by else f"@{by}"


def facts(store, release):
    """Everything the report says, collected from the store. Plain data, easy to test."""
    events = store.events(release["id"])
    plan = json.loads(release.get("plan") or "{}")
    policy = Policy(**json.loads(release["policy"]))
    by_kind = {}
    for e in events:
        by_kind.setdefault(e["kind"], []).append(e)
    first = lambda kind: (by_kind.get(kind) or [None])[0]  # noqa: E731

    start = first("stage")
    end = first("completed") or first("rolled_back") or (events[-1] if events else None)
    health = []
    for e in events:  # each stage's numbers are recorded when the stage is left
        d = _data(e)
        if e["kind"] == "stage" and d.get("stage") in ("stage_1", "stage_2"):
            left = {"stage_1": "stage_0", "stage_2": "stage_1"}[d["stage"]]
            health.append((STAGE_LABEL[left], d.get("new"), d.get("old"), "passed"))
        elif e["kind"] == "awaiting_approval":
            health.append((STAGE_LABEL["stage_2"], d.get("new"), d.get("old"), "passed"))
        elif e["kind"] == "released":
            health.append(("everyone (100%)", d.get("new"), None, "passed"))
        elif e["kind"] == "rolled_back" and d.get("new"):
            health.append((STAGE_LABEL.get(d.get("stage"), d.get("stage", "?")), d.get("new"), d.get("old"),
                           "switched off"))

    approvals = []
    if first("plan_approved"):
        approvals.append(("Release plan", _who(_data(first("plan_approved")).get("by") or plan.get("approved_by")),
                          first("plan_approved")["ts"], "`/approve` on the PR"))
    if first("approved"):
        by = _data(first("approved")).get("by") or release.get("approved_by")
        approvals.append(("Release to everyone", _who(by), first("approved")["ts"],
                          "dashboard button" if by == "dashboard" else "`/approve` on the PR"))
    if first("cleanup_merged"):
        approvals.append((f"Cleanup PR #{release.get('cleanup_pr')}", "merged on GitHub",
                          first("cleanup_merged")["ts"], "merge"))

    conv_data = _data(first("awaiting_approval")).get("conversion") if first("awaiting_approval") else None
    requests = None
    watched_until = first("released") or first("rolled_back")  # the gates stop being checked here
    if start and watched_until:
        rows = store.metrics_since(release["flag"], start["ts"])
        rows = [r for r in rows if r["ts"] <= watched_until["ts"]]
        requests = {"new": sum(r["path"] == "new" for r in rows), "old": sum(r["path"] == "old" for r in rows)}

    actions = []
    if first("flag_created"):
        actions.append(f"Created the flag `{release['flag']}` in Flagsmith, off for everyone")
    commits = plan.get("bot_commits") or []
    if commits:
        actions.append(f"Committed the flag wrapper to the PR ({len(commits)} commit"
                       f"{'s' if len(commits) != 1 else ''}, made by code, no AI)")
    if first("deployed"):
        actions.append("Deployed the merge (`git pull` in the shop's folder)")
    if start:
        actions.append("Created the rollout segment (internal staff first)")
    widened = [_data(e).get("stage") for e in by_kind.get("stage", []) if _data(e).get("stage") != "stage_0"]
    if widened:
        actions.append(f"Widened it {len(widened)} time{'s' if len(widened) != 1 else ''} in place "
                       f"({', '.join(STAGE_LABEL[s] for s in widened)}), without reshuffling users")
    if requests:
        actions.append(f"Checked the health gates every few seconds over {requests['new'] + requests['old']} requests "
                       f"({requests['new']} on the new path)")
    if conv_data:
        actions.append("Compared checkout conversion between the new and old paths")
    if first("full"):
        actions.append("Turned the flag on for everyone after the second approval")
    if first("rolled_back"):
        stage_start = next((e["ts"] for e in reversed(by_kind.get("stage", []))
                            if e["ts"] <= first("rolled_back")["ts"]), None)
        into = f", {_duration(first('rolled_back')['ts'] - stage_start)} into the stage" if stage_start else ""
        actions.append(f"Switched the flag off for everyone by the approved rule{into}")
    if first("diagnosis"):
        actions.append("Posted an AI diagnosis, every cited line checked against the diff")
    if first("cleanup_pr_opened"):
        actions.append(f"Opened cleanup PR #{release.get('cleanup_pr')} removing the flag code (made by code, no AI)")
    if first("completed") and first("cleanup_merged"):
        actions.append("Deployed the cleanup, then deleted the flag and its segment from Flagsmith")

    timeline = [(e["ts"], STEP_TEXT[e["kind"]] if e["kind"] != "stage"
                 else f"Stage: new code on for {STAGE_LABEL.get(_data(e).get('stage'), '?')}")
                for e in events if e["kind"] in STEP_TEXT or e["kind"] == "stage"]

    return {
        "flag": release["flag"], "state": release["state"], "pr": release.get("pr_number"),
        "title": plan.get("title", ""), "tier": policy.tier, "reason": release.get("rollback_reason"),
        "start": start["ts"] if start else None, "end": end["ts"] if end else None,
        "full_at": first("full")["ts"] if first("full") else None,
        "timeline": timeline, "approvals": approvals, "health": health, "conversion": conv_data,
        "actions": actions, "diagnosed": bool(first("diagnosis")), "cleanup_pr": release.get("cleanup_pr"),
    }


def fallback_summary(f):
    """The opening written by code, used when the AI is unavailable."""
    if f["state"] == "rolled_back":
        return (f"`{f['flag']}` was switched off for everyone by the approved rule during its rollout. "
                "The numbers and the diagnosis are below.")
    return (f"`{f['flag']}` was released to everyone in stages with human approvals, and its flag code "
            "was then removed. The numbers are below.")


def render(f, summary, from_ai):
    out = [f"## 📋 Release report: `{f['flag']}`", "", summary, ""]
    if f["state"] == "completed":
        took = f" Merge to everyone: **{_duration(f['full_at'] - f['start'])}**." if f["full_at"] and f["start"] else ""
        out.append(f"**Outcome:** ✅ released to everyone and cleaned up ({f['tier']} risk plan).{took}")
    else:
        out.append(f"**Outcome:** 🛑 switched off for everyone ({f['tier']} risk plan). Reason: {f['reason']}.")
    out.append("")

    if f["approvals"]:
        out += ["### Approvals", "", "| # | What | Who | When | How |", "|---|---|---|---|---|"]
        out += [f"| {i} | {what} | {who} | {_clock(ts)} | {how} |"
                for i, (what, who, ts, how) in enumerate(f["approvals"], 1)]
        out.append("")

    if f["health"]:
        out += ["### Health at each stage", "", "| Stage | New path | Old path | Result |", "|---|---|---|---|"]
        out += [f"| {label} | {_stats(new)} | {_stats(old) if old else '(old path off)'} | {result} |"
                for label, new, old, result in f["health"]]
        out.append("")

    out += ["### Does it help?"]
    if f["conversion"]:
        c = experiment.Conversion(**{k: v for k, v in f["conversion"].items()
                                     if k in experiment.Conversion.__dataclass_fields__})
        out.append(f"Checkout conversion, new vs old: {experiment.summary_line(c)}. "
                   "<sub>Computed by Release Autopilot from the shop's own events, not by Flagsmith Experimentation.</sub>")
    else:
        out.append("Not measured: the release stopped before the comparison at 25%.")
    out.append("")

    out += ["### What the bot did", ""] + [f"- {a}" for a in f["actions"]] + [""]

    if f["timeline"]:
        day = time.strftime("%d %b %Y", time.localtime(f["timeline"][0][0]))
        out += [f"<details><summary>Timeline ({day})</summary>", "", "| Time | Step |", "|---|---|"]
        out += [f"| {_clock(ts)} | {text} |" for ts, text in f["timeline"]]
        out += ["", "</details>", ""]
    if f["state"] == "rolled_back" and f["diagnosed"]:
        out += ["The likely cause and a suggested fix are in the switch-off comment above.", ""]
    out.append(f"<sub>Every number above comes from the bot's event log and metrics. "
               f"Opening sentences: {'AI-written, checked to contain no numbers' if from_ai else 'written by code'}.</sub>")
    return "\n".join(out)


def build(store, release, llm=None):
    f = facts(store, release)
    words = {"flag": f["flag"], "title": f["title"], "outcome": "switched off" if f["state"] == "rolled_back"
             else "released to everyone and cleaned up", "risk_tier": f["tier"],
             "stages_reached": [row[0] for row in f["health"]],
             "approvals": [f"{what} by {who}" for what, who, _, _ in f["approvals"]],
             "conversion": (f["conversion"] or {}).get("verdict", "not measured"),
             "switched_off_because": "a health gate failed" if f["state"] == "rolled_back" else None,
             "diagnosis_posted": f["diagnosed"]}
    summary, from_ai = release_summary(llm, words, fallback_summary(f))
    return render(f, summary, from_ai)


class ReleaseReporter:
    """Posts each release's report once, when it finishes while the bot runs."""

    def __init__(self, store, gh, llm=None, clock=time.time, log=print, since=None):
        self.store, self.gh, self.llm, self.clock, self.log = store, gh, llm, clock, log
        self.since = clock() if since is None else since
        self._next_try = {}

    def tick(self):
        for release in self.store.releases_in(OUTCOMES):
            if not release.get("pr_number") or release["updated_at"] < self.since:
                continue
            if self.clock() < self._next_try.get(release["id"], 0):
                continue
            events = self.store.events(release["id"])
            if any(e["kind"] == "report_posted" for e in events):
                continue
            if release["state"] == "rolled_back" and self._waiting_for_diagnosis(events):
                continue
            try:
                self.post(release)
            except GitHubError as e:
                self._next_try[release["id"]] = self.clock() + RETRY_SECONDS
                self.log(f"[{release['flag']}] couldn't post the release report ({e}); retrying in {RETRY_SECONDS}s")

    def _waiting_for_diagnosis(self, events):
        off = next((e for e in events if e["kind"] == "rolled_back"), None)
        expects = off is not None and bool(_data(off).get("new"))  # a manual /rollback gets no diagnosis
        has = any(e["kind"] == "diagnosis" for e in events)
        return expects and not has and self.clock() - off["ts"] < DIAGNOSIS_WAIT_SECONDS

    def post(self, release, body=None):
        body = body or build(self.store, release, self.llm)
        comment_id = self.gh.post_comment(release["pr_number"], body)
        self.store.add_event(release["id"], "report_posted", f"release report posted on PR #{release['pr_number']}",
                             {"comment_id": comment_id})
        self.log(f"[{release['flag']}] release report posted on PR #{release['pr_number']}")
        return comment_id