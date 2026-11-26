"""Risk tiers: fixed policy templates (rules, not AI).

The LLM (from Friday) only picks a tier. Every number the engine acts on
comes from here, and /set can only tighten a value, never loosen it past the
safety bounds.
"""

from dataclasses import dataclass, replace

# Who sees the new code at each rollout stage: internal staff always, plus
# this % of everyone else. Stage 0 is internal only.
STAGES = (("stage_0", 0), ("stage_1", 5), ("stage_2", 25))

# The old path needs a sample too, or there's nothing to compare against.
MIN_OLD_REQUESTS = 20


@dataclass(frozen=True)
class Policy:
    tier: str
    soak_seconds: int            # time each stage must run cleanly before advancing
    min_new_requests: int        # new-path requests needed before judging a stage
    max_error_increase_pp: float  # new error rate may exceed old by at most this many points
    max_error_rate_pct: float    # ...and never exceed this absolute rate
    max_p95_ratio: float         # new p95 latency <= ratio x old p95
    # Ignore latency differences smaller than this. At ~5 ms p95, normal
    # jitter alone can exceed a 1.5x ratio and would cause false rollbacks.
    min_p95_increase_ms: float = 50.0


TIERS = {
    "low": Policy("low", soak_seconds=30, min_new_requests=20,
                  max_error_increase_pp=3, max_error_rate_pct=5, max_p95_ratio=2.0),
    "medium": Policy("medium", soak_seconds=60, min_new_requests=30,
                     max_error_increase_pp=2, max_error_rate_pct=5, max_p95_ratio=1.5),
    "high": Policy("high", soak_seconds=120, min_new_requests=50,
                   max_error_increase_pp=1, max_error_rate_pct=2, max_p95_ratio=1.3),
}

# Hard limits: no override may go past these in the loose direction.
SAFETY_BOUNDS = {
    "max_error_rate_pct": ("max", 5.0),
    "max_error_increase_pp": ("max", 3.0),
    "max_p95_ratio": ("max", 2.0),
    "min_p95_increase_ms": ("max", 200.0),
    "min_new_requests": ("min", 20),
    "soak_seconds": ("min", 10),
}


def for_tier(tier):
    if tier not in TIERS:
        raise ValueError(f"unknown risk tier {tier!r}; use one of {', '.join(TIERS)}")
    return TIERS[tier]


def with_overrides(policy, **changes):
    """Apply /set-style changes. Raises ValueError for unknown names or any
    value past a safety bound."""
    for name, value in changes.items():
        if name not in SAFETY_BOUNDS:
            raise ValueError(f"{name} can't be changed; allowed: {', '.join(SAFETY_BOUNDS)}")
        kind, bound = SAFETY_BOUNDS[name]
        if kind == "max" and value > bound:
            raise ValueError(f"{name} can't be above {bound} (safety bound)")
        if kind == "min" and value < bound:
            raise ValueError(f"{name} can't be below {bound} (safety bound)")
    return replace(policy, **changes)


def describe(policy):
    return (f"{policy.tier} risk: {policy.soak_seconds}s per stage, "
            f">= {policy.min_new_requests} new-path requests, "
            f"errors <= old + {policy.max_error_increase_pp:g} pts and <= {policy.max_error_rate_pct:g}%, "
            f"p95 <= {policy.max_p95_ratio:g}x old")