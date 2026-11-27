"""Stage 3.5, "prove it helps": does the new code convert better than the old?

Checkout conversion = orders / checkouts, per path, since the rollout started
(events the shop tags with autopilot_sdk.track). Compared with a two-sided
two-proportion z-test: the difference in percentage points, a 95% confidence
interval and a p-value.

Computed by Release Autopilot from its own events. Flagsmith Experimentation
(Enterprise beta, multivariate flags) has no documented API for results, so
these numbers are never presented as Flagsmith's.

The result informs the human who approves the release to everyone. It never
approves, blocks or switches anything off by itself.
"""

import math
from dataclasses import asdict, dataclass

MIN_PER_PATH = 100   # checkouts per path before a result is reported
ALPHA = 0.05


@dataclass
class Conversion:
    new_checkouts: int
    new_orders: int
    old_checkouts: int
    old_orders: int
    enough: bool = False
    new_rate: float = 0.0       # percent
    old_rate: float = 0.0
    diff_pp: float = 0.0        # new - old, percentage points
    ci_low_pp: float = 0.0
    ci_high_pp: float = 0.0
    p_value: float = 1.0
    verdict: str = "not_enough_data"  # "better" | "worse" | "no_difference" | "not_enough_data"

    def to_dict(self):
        return asdict(self)


def compare(new_checkouts, new_orders, old_checkouts, old_orders, min_per_path=MIN_PER_PATH):
    # An order placed just after a stage change can come from the other path; never exceed 100%.
    new_orders, old_orders = min(new_orders, new_checkouts), min(old_orders, old_checkouts)
    c = Conversion(new_checkouts, new_orders, old_checkouts, old_orders)
    if new_checkouts < min_per_path or old_checkouts < min_per_path:
        return c
    p1, p2 = new_orders / new_checkouts, old_orders / old_checkouts
    c.enough, c.new_rate, c.old_rate, c.diff_pp = True, 100 * p1, 100 * p2, 100 * (p1 - p2)
    pooled = (new_orders + old_orders) / (new_checkouts + old_checkouts)
    se_pooled = math.sqrt(pooled * (1 - pooled) * (1 / new_checkouts + 1 / old_checkouts))
    se = math.sqrt(p1 * (1 - p1) / new_checkouts + p2 * (1 - p2) / old_checkouts)
    c.ci_low_pp, c.ci_high_pp = 100 * (p1 - p2 - 1.96 * se), 100 * (p1 - p2 + 1.96 * se)
    if se_pooled == 0:
        c.p_value = 1.0
    else:
        z = (p1 - p2) / se_pooled
        c.p_value = math.erfc(abs(z) / math.sqrt(2))
    if c.p_value < ALPHA:
        c.verdict = "better" if c.diff_pp > 0 else "worse"
    else:
        c.verdict = "no_difference"
    return c


def conversion(store, flag, since, until=None):
    counts = store.event_counts(flag, since, until)
    return compare(counts.get(("new", "checkout"), 0), counts.get(("new", "order"), 0),
                   counts.get(("old", "checkout"), 0), counts.get(("old", "order"), 0))


def rollout_window(store, release):
    """(since, until) for the comparison: from stage 0 until the release went to
    everyone (after that there is no old path to compare with)."""
    events = store.events(release["id"])
    since = next((e["ts"] for e in events if e["kind"] == "stage"), None)
    until = next((e["ts"] for e in events if e["kind"] == "full"), None)
    return since, until


def for_release(store, release):
    since, until = rollout_window(store, release)
    return None if since is None else conversion(store, release["flag"], since, until)


def p_text(p):
    return "p < 0.001" if p < 0.001 else f"p = {p:.3f}"


def summary_line(c):
    """One line for logs and the dashboard. Numbers only; computed here, never by the AI."""
    if not c.enough:
        return (f"not enough data yet ({min(c.new_checkouts, c.old_checkouts)}/{MIN_PER_PATH} checkouts "
                "on the smaller path)")
    word = {"better": "better", "worse": "WORSE", "no_difference": "no significant difference"}[c.verdict]
    return (f"conversion new {c.new_rate:.1f}% vs old {c.old_rate:.1f}%: {c.diff_pp:+.1f} pp "
            f"(95% CI {c.ci_low_pp:+.1f} to {c.ci_high_pp:+.1f}), {p_text(c.p_value)}: {word}")