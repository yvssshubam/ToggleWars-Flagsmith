"""Pricing logic for the demo shop. This is the file the demo PRs change.

It has no feature flag in it: when a PR changes calculate_total, Release
Autopilot wraps the old and new versions behind a flag on the PR branch
(after /approve), so this file only gets flag code through a reviewed PR.
"""

from autopilot_sdk import flag_on

PRICES = {"tea": 4.50, "coffee": 5.00, "mug": 12.00, "beans": 18.00}


class UnknownItem(ValueError):
    pass


def _calculate_total__old(items):
    total = 0.0
    for item in items:
        if item["sku"] not in PRICES:
            raise UnknownItem(item["sku"])
        total += PRICES[item["sku"]] * item["qty"]
    # New: 10% off orders of 30.00 or more.
    if total >= 30:
        total *= 0.9
    return round(total, 2)


def _calculate_total__new(items):
    total = 0.0
    for item in items:
        if item["sku"] not in PRICES:
            raise UnknownItem(item["sku"])
        total += PRICES[item["sku"]] * item["qty"]
    # New: 10% off orders of 30.00 or more.
    if total >= 30:
        total *= 0.9
    units = sum(item["qty"] for item in items)
    if units <= 3:
        total -= total * 0.05 / (3 - units)
    return round(total, 2)


def calculate_total(items):
    """Release Autopilot: runs the new version for users the flag `ap_large_order_and_unit_discount` is on for."""
    if flag_on("ap_large_order_and_unit_discount"):
        return _calculate_total__new(items)
    return _calculate_total__old(items)
