"""Health gates: plain rules comparing the new path with the old one.

No AI here. Given the metrics for one stage and the approved policy, return
WAIT (not enough data), PASS or FAIL (with the gate that failed and the numbers).
"""

from dataclasses import dataclass

from autopilot.policy import MIN_OLD_REQUESTS


@dataclass
class PathStats:
    n: int = 0
    errors: int = 0
    p95_ms: float = 0.0

    @property
    def error_rate(self):
        return self.errors / self.n if self.n else 0.0

    def text(self):
        return f"n={self.n} errors {self.error_rate:.1%} p95 {self.p95_ms:.0f}ms"


@dataclass
class Verdict:
    status: str            # "wait" | "pass" | "fail"
    reason: str
    new: PathStats
    old: PathStats
    gate: str | None = None  # which gate failed: "error_rate_absolute" | "error_rate_vs_old" | "latency"


def p95(values):
    """Nearest-rank 95th percentile."""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, -(-95 * len(ordered) // 100))  # ceil(0.95 * n)
    return ordered[rank - 1]


def path_stats(rows):
    """rows: dicts with 'ok' and 'latency_ms'."""
    return PathStats(
        n=len(rows),
        errors=sum(1 for r in rows if not r["ok"]),
        p95_ms=p95([r["latency_ms"] for r in rows]),
    )


def evaluate(new_rows, old_rows, policy, baseline_old=None):
    """Judge one stage. At 100% there is no old path any more, so the caller
    passes the old-path stats measured at the last partial stage as `baseline_old`."""
    new = path_stats(new_rows)
    old = baseline_old if baseline_old is not None else path_stats(old_rows)

    if new.n < policy.min_new_requests:
        return Verdict("wait", f"waiting for traffic ({new.n}/{policy.min_new_requests} new-path requests)", new, old)
    if old.n < MIN_OLD_REQUESTS:
        return Verdict("wait", f"waiting for traffic ({old.n}/{MIN_OLD_REQUESTS} old-path requests)", new, old)

    new_pct, old_pct = new.error_rate * 100, old.error_rate * 100
    if new_pct > policy.max_error_rate_pct:
        return Verdict("fail", f"new-path error rate {new_pct:.1f}% is above the {policy.max_error_rate_pct:g}% limit "
                       f"(old path {old_pct:.1f}%)", new, old, "error_rate_absolute")
    if new_pct - old_pct > policy.max_error_increase_pp:
        return Verdict("fail", f"new-path error rate {new_pct:.1f}% is more than {policy.max_error_increase_pp:g} points "
                       f"above the old path's {old_pct:.1f}%", new, old, "error_rate_vs_old")
    if (new.p95_ms > policy.max_p95_ratio * old.p95_ms
            and new.p95_ms - old.p95_ms >= policy.min_p95_increase_ms):
        return Verdict("fail", f"new-path p95 latency {new.p95_ms:.0f}ms is more than {policy.max_p95_ratio:g}x "
                       f"the old path's {old.p95_ms:.0f}ms", new, old, "latency")
    return Verdict("pass", "healthy", new, old)