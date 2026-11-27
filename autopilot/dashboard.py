"""Data for the dashboard page (autopilot/dashboard.html), served by server.py.

Read-only views of the store: one summary per release, and for one release
its event log plus a new-vs-old time series in 10-second buckets. The live
gate status uses the same `engine.judge` the engine acts on, so the page and
the engine can't disagree.
"""

import json
import re

from autopilot import experiment
from autopilot.engine import STAGE_LABEL, judge
from autopilot.health import path_stats
from autopilot.policy import Policy

BUCKET_SECONDS = 10
MAX_RELEASES = 30

# The happy path, in order, as shown in the progress bar.
STEPS = [
    ("proposed", "Plan"), ("pending_merge", "Approved"), ("stage_0", "Internal"),
    ("stage_1", "5%"), ("stage_2", "25%"), ("awaiting_approval", "Approval"),
    ("full", "100%"), ("released", "Released"), ("cleanup_pr_open", "Cleanup"), ("completed", "Done"),
]
STEP_INDEX = {state: i for i, (state, _) in enumerate(STEPS)}
STEP_INDEX.update({"approving": 0, "cleanup_merged": STEP_INDEX["cleanup_pr_open"]})
LIVE_STATES = ("stage_0", "stage_1", "stage_2", "full")
STATE_TEXT = {
    "proposed": "waiting for plan approval", "approving": "plan approved, being carried out",
    "pending_merge": "approved, waiting for merge", "stage_0": "rolling out: internal only",
    "stage_1": "rolling out: internal + 5%", "stage_2": "rolling out: internal + 25%",
    "awaiting_approval": "waiting for approval to release to everyone", "full": "on for everyone, watching",
    "released": "released", "cleanup_pr_open": "cleanup PR open", "cleanup_merged": "cleanup merged, deploying",
    "completed": "completed", "rolled_back": "switched off", "rejected": "plan rejected",
    "abandoned": "closed", "error": "rollout blocked",
}


def _diagnosis(events):
    """The latest diagnosis, as {cause, confidence}, from the section posted on the PR."""
    message = next((e["message"] for e in reversed(events) if e["kind"] == "diagnosis"), None)
    if not message:
        return None
    cause = re.search(r"\*\*Likely cause:\*\*\s*(.+)", message)
    confidence = re.search(r"\*\*Confidence:\*\*\s*(\w+)", message)
    if cause:
        return {"cause": cause.group(1).strip(), "confidence": confidence.group(1) if confidence else None}
    unavailable = re.search(r"Diagnosis unavailable[^\n]*", message)
    return {"cause": unavailable.group(0) if unavailable else "diagnosis unavailable", "confidence": None}


def _stats(s):
    return {"n": s.n, "errors": s.errors, "error_rate": round(100 * s.error_rate, 2), "p95_ms": round(s.p95_ms, 1)}


def _step_index(release, events):
    """Where the release got to. A switched-off or blocked release shows the stage it stopped at."""
    state = release["state"]
    if state in STEP_INDEX:
        return STEP_INDEX[state]
    reached = [STEP_INDEX[e["kind_state"]] for e in events if e.get("kind_state") in STEP_INDEX]
    return max(reached, default=0)


def summary(store, release, clock, repo=None):
    plan = json.loads(release.get("plan") or "{}")
    policy = Policy(**json.loads(release["policy"]))
    state = release["state"]
    events = store.events(release["id"])
    for e in events:  # which stage each "stage" event entered, for the progress bar
        data = json.loads(e["data"]) if e.get("data") else {}
        e["kind_state"] = data.get("stage") if e["kind"] == "stage" else {"full": "full"}.get(e["kind"])
    out = {
        "id": release["id"], "flag": release["flag"], "state": state,
        "state_text": STATE_TEXT.get(state, state), "tier": policy.tier,
        "pr_number": release.get("pr_number"), "cleanup_pr": release.get("cleanup_pr"),
        "pr_url": f"https://github.com/{repo}/pull/{release['pr_number']}" if repo and release.get("pr_number") else None,
        "title": plan.get("title") or "",
        "step": _step_index(release, events), "steps": [label for _, label in STEPS],
        "stopped": state in ("rolled_back", "error", "rejected", "abandoned"),
        "approved_by": release.get("approved_by"), "rollback_reason": release.get("rollback_reason"),
        "created_at": release["created_at"], "updated_at": release["updated_at"],
        "can_approve": state == "awaiting_approval" and not release.get("approved_by"),
        "can_switch_off": state in ("stage_0", "stage_1", "stage_2", "awaiting_approval", "full")
                          and not release.get("rollback_requested_by"),
        "policy": {"soak_seconds": policy.soak_seconds, "min_new_requests": policy.min_new_requests,
                   "max_error_rate_pct": policy.max_error_rate_pct,
                   "max_error_increase_pp": policy.max_error_increase_pp, "max_p95_ratio": policy.max_p95_ratio},
        "gate": None, "diagnosis": _diagnosis(events),
        "success_metric": plan.get("success_metric") or None,
    }
    conv = experiment.for_release(store, release)
    out["conversion"] = conv.to_dict() if conv else None
    if conv:
        out["conversion"]["min_per_path"] = experiment.MIN_PER_PATH
        out["conversion"]["summary"] = experiment.summary_line(conv)
    if state in LIVE_STATES and release.get("stage_started_at"):
        verdict = judge(store, release, policy)
        out["gate"] = {
            "stage_label": STAGE_LABEL.get(state, state),
            "elapsed": round(clock() - release["stage_started_at"]), "soak": policy.soak_seconds,
            "status": verdict.status, "reason": verdict.reason,
            "new": _stats(verdict.new), "old": _stats(verdict.old),
        }
    return out


def list_releases(store, clock, repo=None):
    releases = sorted(store.all_releases(), key=lambda r: r["id"], reverse=True)[:MAX_RELEASES]
    return [summary(store, r, clock, repo) for r in releases]


def detail(store, release, clock, repo=None):
    out = summary(store, release, clock, repo)
    events = store.events(release["id"])
    out["events"] = [{"ts": e["ts"], "kind": e["kind"], "message": e["message"]} for e in events]
    for e in out["events"]:
        if e["kind"] == "diagnosis":  # the full section is on the PR; the log shows its gist
            d = _diagnosis([e])
            e["message"] = f"AI diagnosis: {d['cause']}" + (f" (confidence {d['confidence']})" if d["confidence"] else "")
    start = next((e["ts"] for e in events if e["kind"] == "stage"), None)
    out["series"], out["markers"] = [], []
    if start is None:
        return out
    end = next((e["ts"] for e in events if e["kind"] in ("released", "rolled_back")), clock())
    rows = [r for r in store.metrics_since(release["flag"], start) if r["ts"] <= end + BUCKET_SECONDS]
    buckets = {}
    for r in rows:
        buckets.setdefault(int((r["ts"] - start) // BUCKET_SECONDS), {"new": [], "old": []})[r["path"]].append(r)
    for b in range(max(buckets, default=-1) + 1):
        paths = buckets.get(b, {"new": [], "old": []})
        point = {"t": b * BUCKET_SECONDS}
        for path in ("new", "old"):
            s = path_stats(paths[path])
            point[path] = _stats(s) if s.n else None
        out["series"].append(point)
    labels = {"stage": None, "full": "100%", "rolled_back": "switched off", "released": "released"}
    for e in events:
        if e["kind"] in labels and e["ts"] >= start:
            data = json.loads(e["data"]) if e.get("data") else {}
            label = labels[e["kind"]] or {"stage_0": "internal", "stage_1": "5%", "stage_2": "25%"}.get(
                data.get("stage"), "stage")
            out["markers"].append({"t": round(e["ts"] - start), "label": label})
    return out