"""Pricing logic for the demo shop. This is the file the demo PRs change.

calculate_total is written in the shape the transformer will produce on
Friday: the old body, the new body, and a dispatcher that asks the flag.
For Thursday this wrapper is written by hand.
"""

import os
import random
import time
# test
from autopilot_sdk import flag_on

PRICES = {"tea": 4.50, "coffee": 5.00, "mug": 12.00, "beans": 18.00}


class UnknownItem(ValueError):
    pass


def _demo_fault():
    """TESTING ONLY (Thursday checkpoint). Set DEMO_NEW_PATH_FAULT before
    starting the shop to break the new path on purpose:
      errors -> ~30% of new-path requests raise an error
      slow   -> every new-path request takes 250 ms longer
    Saturday's bad demo PR replaces this with a real bug."""
    fault = os.environ.get("DEMO_NEW_PATH_FAULT", "").strip().lower()
    if fault == "errors" and random.random() < 0.3:
        raise RuntimeError("injected fault: new pricing path failed")
    if fault == "slow":
        time.sleep(0.25)


def _calculate_total__old(items):
    total = 0.0
    for item in items:
        if item["sku"] not in PRICES:
            raise UnknownItem(item["sku"])
        total += PRICES[item["sku"]] * item["qty"]
    return round(total, 2)


def _calculate_total__new(items):
    _demo_fault()
    # New: 10% off orders of 30.00 or more.
    total = _calculate_total__old(items)
    if total >= 30:
        total *= 0.9
    return round(total, 2)


def calculate_total(*args, **kwargs):
    if flag_on("ap_checkout_v2"):
        return _calculate_total__new(*args, **kwargs)
    return _calculate_total__old(*args, **kwargs)
