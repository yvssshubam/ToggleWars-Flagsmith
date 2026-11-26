"""A fixed copy of the demo shop's pricing code, for tests and checks.

demo_shop/checkout.py changes with every demo PR (that's its job), so tests
that need "the plain checkout" and "a PR that adds a discount" use this copy
instead. Totals for the cart 2 x beans: plain 36.00, with the discount 32.40.
"""

from autopilot.transformer import wrap
from demo_shop import checkout as live_checkout

PLAIN = '''"""Pricing logic for the demo shop."""

PRICES = {"tea": 4.50, "coffee": 5.00, "mug": 12.00, "beans": 18.00}


class UnknownItem(ValueError):
    pass


def calculate_total(items):
    total = 0.0
    for item in items:
        if item["sku"] not in PRICES:
            raise UnknownItem(item["sku"])
        total += PRICES[item["sku"]] * item["qty"]
    return round(total, 2)
'''

DISCOUNT_PR = PLAIN.replace(
    "    return round(total, 2)\n",
    "    # New: 10% off orders of 30.00 or more.\n"
    "    if total >= 30:\n"
    "        total *= 0.9\n"
    "    return round(total, 2)\n")


def load(source, name="calculate_total"):
    """Compile `source` and return its function, using the shop's real
    UnknownItem class (the shop catches that exact class)."""
    namespace = {}
    exec(compile(source, "checkout.py", "exec"), namespace)
    namespace["UnknownItem"] = live_checkout.UnknownItem
    return namespace[name]


def plain_calculate_total():
    return load(PLAIN)


def wrapped_calculate_total(flag="ap_checkout_v2"):
    """The transformer's real output for the discount PR, as the bot would commit it."""
    return load(wrap(PLAIN, DISCOUNT_PR, "calculate_total", flag))