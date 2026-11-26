"""Release engine: steps a flag through the stages with fixed rules.

    stage_0 (internal) -> stage_1 (+5%) -> stage_2 (+25%) -> awaiting_approval
        -> (human approves) -> full (100%, soaking) -> released

Any failed health gate, or a manual rollback request, switches the flag off
for everyone: the switch-off rule the human approved with the plan. Flagsmith
errors never cause a guess: the engine keeps the current state and retries on
the next tick. A pending rollback is retried before anything else.
"""

import json
import time
from dataclasses import asdict

from autopilot.flagsmith_client import FlagsmithError
from autopilot.health import PathStats, Verdict, evaluate
from autopilot.policy import STAGES, Policy, describe

STAGE_PERCENT = dict(STAGES)
STAGE_LABEL = {"stage_0": "internal only", "stage_1": "internal + 5%", "stage_2": "internal + 25%",
               "full": "everyone (100%)"}


def segment_name(flag):
    return f"{flag}_rollout"


class Engine:
    def __init__(self, store, fs, clock=time.time, log=print):
        self.store, self.fs, self.clock, self.log = store, fs, clock, log

    # --- helpers -------------------------------------------------------------

    def _event(self, release, kind, message, data=None, echo=True):
        self.store.add_event(release["id"], kind, message, data)
        if echo:
            self.log(f"[{release['flag']}] {message}")

    @staticmethod
    def policy_of(release):
        return Policy(**json.loads(release["policy"]))

    # --- start ---------------------------------------------------------------

    def start_release(self, flag, policy, started_by):
        """Create the flag (off) if needed, a rollout segment at internal-only,
        and turn the flag on for that segment. Returns the release id."""
        existing = self.store.latest_release(flag)
        if existing and existing["state"] in ("stage_0", "stage_1", "stage_2", "awaiting_approval", "full"):
            raise ValueError(f"{flag} already has an active release (state {existing['state']})")

        feature = self.fs.get_feature_by_name(flag)
        created = feature is None
        feature_id = self.fs.create_feature(flag, "Managed by Release Autopilot") if created else feature["id"]
        segment_id = self.fs.create_percentage_segment(segment_name(flag), 0, include_internal=True)
        try:
            self.fs.enable_for_segment_only(feature_id, segment_id)
        except FlagsmithError:
            self.fs.delete_segment(segment_id)  # don't leave a half-made release behind
            raise

        release_id = self.store.create_release(flag, feature_id, segment_id, asdict(policy), "stage_0")
        release = self.store.get_release(release_id)
        self._event(release, "started", f"release started by {started_by}; plan: {describe(policy)}",
                    {"by": started_by, "flag_created": created})
        if created:
            self._event(release, "flag_created", f"created flag {flag} (off for everyone)")
        self._event(release, "stage", f"stage 0: new code on for {STAGE_LABEL['stage_0']}")
        return release_id

    # --- the loop body -------------------------------------------------------

    def tick_all(self):
        for release in self.store.active_releases():
            self.tick(release["id"])

    def tick(self, release_id):
        release = self.store.get_release(release_id)
        if release["state"] not in ("stage_0", "stage_1", "stage_2", "awaiting_approval", "full"):
            return
        if release["rollback_reason"] or release["rollback_requested_by"]:
            reason = release["rollback_reason"] or f"manual rollback by {release['rollback_requested_by']}"
            self._rollback(release, reason, None)
            return

        state = release["state"]
        if state == "awaiting_approval":
            if release["approved_by"]:
                self._go_full(release)
            return

        policy = self.policy_of(release)
        verdict = self._judge(release, policy)
        elapsed = self.clock() - release["stage_started_at"]
        self.log(f"[{release['flag']}] {state} ({STAGE_LABEL[state]}) {elapsed:.0f}s/{policy.soak_seconds}s | "
                 f"new {verdict.new.text()} | old {verdict.old.text()} | {verdict.status}: {verdict.reason}")

        if verdict.status == "fail":
            self._rollback(release, verdict.reason, verdict)
        elif verdict.status == "pass" and elapsed >= policy.soak_seconds:
            self._advance(release, verdict)

    def _judge(self, release, policy):
        since = release["stage_started_at"]
        new = self.store.metrics_since(release["flag"], since, "new")
        if release["state"] == "full":
            baseline = PathStats(**json.loads(release["baseline_old"])) if release["baseline_old"] else None
            if baseline is None:
                return Verdict("wait", "no old-path baseline recorded", PathStats(), PathStats())
            return evaluate(new, [], policy, baseline_old=baseline)
        old = self.store.metrics_since(release["flag"], since, "old")
        return evaluate(new, old, policy)

    # --- transitions ---------------------------------------------------------

    def _advance(self, release, verdict):
        state = release["state"]
        if state == "full":
            self.store.update_release(release["id"], state="released")
            self._event(release, "released", "stable at 100%; ready for cleanup (Phase C)",
                        {"new": asdict(verdict.new)})
            return
        if state == "stage_2":
            self.store.update_release(release["id"], state="awaiting_approval",
                                      baseline_old=json.dumps(asdict(verdict.old)))
            self._event(release, "awaiting_approval",
                        "all stages healthy. Release to everyone? Waiting for a human approval "
                        f"(python -m autopilot approve {release['flag']})",
                        {"new": asdict(verdict.new), "old": asdict(verdict.old)})
            return

        next_state = {"stage_0": "stage_1", "stage_1": "stage_2"}[state]
        try:
            self.fs.set_segment_percentage(release["segment_id"], segment_name(release["flag"]),
                                           STAGE_PERCENT[next_state], include_internal=True)
        except FlagsmithError as e:
            self._event(release, "flagsmith_error", f"couldn't widen the segment ({e}); "
                        "staying at the current stage and retrying", echo=True)
            return
        self.store.update_release(release["id"], state=next_state, stage_started_at=self.clock())
        self._event(release, "stage", f"{next_state.replace('_', ' ')}: new code on for {STAGE_LABEL[next_state]} "
                    f"(previous stage: new {verdict.new.text()}, old {verdict.old.text()})",
                    {"new": asdict(verdict.new), "old": asdict(verdict.old)})

    def _go_full(self, release):
        try:
            self.fs.set_flag_for_everyone(release["feature_id"], True)
            self._remove_override(release)
        except FlagsmithError as e:
            self._event(release, "flagsmith_error", f"couldn't release to everyone ({e}); retrying")
            return
        self.store.update_release(release["id"], state="full", stage_started_at=self.clock())
        self._event(release, "full", f"approved by {release['approved_by']}: new code on for everyone",
                    {"by": release["approved_by"]})

    def _remove_override(self, release):
        try:
            self.fs.remove_segment_override(release["feature_id"], release["segment_id"])
        except FlagsmithError as e:
            if e.status_code != 404:  # 404 = already removed (verified 26 Nov)
                raise

    def _rollback(self, release, reason, verdict):
        """The approved switch-off rule. Records the reason first, so a failed
        Flagsmith call is retried on every tick until it succeeds."""
        if not release["rollback_reason"]:
            self.store.update_release(release["id"], rollback_reason=reason)
        try:
            self.fs.set_flag_for_everyone(release["feature_id"], False)
            self._remove_override(release)
        except FlagsmithError as e:
            self._event(release, "rollback_failed",
                        f"!!! SWITCH-OFF FAILED ({e}). Retrying every tick. "
                        "Turn the flag off in the Flagsmith dashboard if this persists.")
            return
        self.store.update_release(release["id"], state="rolled_back")
        data = {"reason": reason, "stage": release["state"]}
        if verdict is not None:
            data.update(gate=verdict.gate, new=asdict(verdict.new), old=asdict(verdict.old))
            data["error_samples"] = self._error_samples(release)
        self._event(release, "rolled_back", f"SWITCHED OFF at {release['state']}: {reason}", data)
        self.log(rollback_report(release, reason, verdict, data.get("error_samples", [])))

    def _error_samples(self, release, limit=20):
        rows = self.store.metrics_since(release["flag"], release["stage_started_at"], "new")
        failed = [r for r in rows if not r["ok"]]
        return [{k: r[k] for k in ("error_type", "error_message", "error_where")} for r in failed[-limit:]]

    # --- commands from humans (CLI now; PR comments from Friday) ---------------

    def approve(self, flag, by):
        release = self.store.latest_release(flag)
        if not release:
            return f"no release for {flag}"
        if release["approved_by"]:
            return f"already approved by {release['approved_by']}"
        if not self.store.request(release["id"], "approved_by", by, ("awaiting_approval",)):
            return f"{flag} isn't waiting for approval (state {release['state']})"
        self.store.add_event(release["id"], "approved", f"release to everyone approved by {by}", {"by": by})
        return f"approved by {by}; the engine releases to everyone on its next tick"

    def request_rollback(self, flag, by):
        release = self.store.latest_release(flag)
        if not release:
            return f"no release for {flag}"
        if not self.store.request(release["id"], "rollback_requested_by", by,
                                  ("stage_0", "stage_1", "stage_2", "awaiting_approval", "full")):
            return f"{flag} has no active release to roll back (state {release['state']})"
        self.store.add_event(release["id"], "rollback_requested", f"manual rollback requested by {by}", {"by": by})
        return "rollback requested; the engine switches the flag off on its next tick"

    def teardown(self, flag):
        """Test helper: remove this flag's override, segment and the flag itself
        from Flagsmith, and close the release. Returns a list of what happened."""
        done = []
        release = self.store.latest_release(flag)
        feature = self.fs.get_feature_by_name(flag)
        if release and release["segment_id"] and feature:
            try:
                self.fs.remove_segment_override(feature["id"], release["segment_id"])
                done.append("removed the segment override")
            except FlagsmithError as e:
                done.append("override already gone" if e.status_code == 404 else f"override: {e}")
        if release and release["segment_id"]:
            try:
                self.fs.delete_segment(release["segment_id"])
                done.append("deleted the segment")
            except FlagsmithError as e:
                done.append("segment already gone" if e.status_code == 404 else f"segment: {e}")
        if feature:
            self.fs.delete_feature(feature["id"])
            done.append(f"deleted the flag {flag}")
        if release and release["state"] not in ("released", "rolled_back", "abandoned"):
            self.store.update_release(release["id"], state="abandoned")
            self.store.add_event(release["id"], "abandoned", "torn down from the CLI")
            done.append("marked the release abandoned")
        return done


def rollback_report(release, reason, verdict, error_samples):
    """Numbers-only switch-off report (the diagnosis is added from Friday)."""
    lines = ["", "=" * 72, f"SWITCHED OFF: {release['flag']} at {release['state']} "
             f"({STAGE_LABEL.get(release['state'], release['state'])})", f"Reason: {reason}"]
    if verdict is not None:
        lines += [f"New path: {verdict.new.text()}", f"Old path: {verdict.old.text()}"]
    if error_samples:
        kinds = {}
        for s in error_samples:
            key = (s["error_type"], s["error_where"])
            kinds[key] = kinds.get(key, 0) + 1
        lines.append("Errors on the new path (latest samples):")
        for (etype, where), count in sorted(kinds.items(), key=lambda kv: -kv[1]):
            lines.append(f"  {count} x {etype} at {where or 'unknown location'}")
    lines += ["Flag is now off for everyone (old code path).", "=" * 72]
    return "\n".join(lines)