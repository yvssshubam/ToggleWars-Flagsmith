"""Pricing logic for the demo shop. This is the file the demo PRs change.

It has no feature flag in it: when a PR changes calculate_total, Release
Autopilot wraps the old and new versions behind a flag on the PR branch
(after /approve), so this file only gets flag code through a reviewed PR.
"""

PRICES = {"tea": 4.50, "coffee": 5.00, "mug": 12.00, "beans": 18.00}


class UnknownItem(ValueError):
    pass


def calculate_total(items):
    total = 0.0
    for item in items:
        if item["sku"] not in PRICES:
            raise UnknownItem(item["sku"])
        total += PRICES[item["sku"]] * item["qty"]
    # New: Black Friday - 25% off orders of 50.00 or more, 20% off orders of 30.00 or more.
    if total >= 50:
        total *= 0.75
    elif total >= 30:
        total *= 0.8
    return round(total, 2)
