"""Live check of the three AI calls against Groq, on a made-up checkout PR.

Uses exactly 3 Groq requests (free tier: 1,000 a day). No GitHub or Flagsmith calls.
Reads GROQ_API_KEY from .env and never prints it.

    python -m scripts.check_groq
"""

import sys
import time

from autopilot.llm import GroqClient, LLMError, diagnose, plan_prose, triage

# A PR that adds a discount, with a real bug: carts without a "coupon" key raise KeyError.
PATCH = (
    "@@ -20,4 +20,7 @@ def calculate_total(items):\n"
    "         if item[\"sku\"] not in PRICES:\n"
    "             raise UnknownItem(item[\"sku\"])\n"
    "         total += PRICES[item[\"sku\"]] * item[\"qty\"]\n"
    "-    return round(total, 2)\n"
    "+    # New: 10% off orders of 30.00 or more, or with a coupon.\n"
    "+    if total >= 30 or items[0][\"coupon\"]:\n"
    "+        total *= 0.9\n"
    "+    return round(total, 2)"
)
PR_FILES = [{"filename": "demo_shop/checkout.py", "status": "modified", "patch": PATCH}]
CHANGED = {"demo_shop/checkout.py": ["calculate_total"]}
OLD = '''def calculate_total(items):
    total = 0.0
    for item in items:
        if item["sku"] not in PRICES:
            raise UnknownItem(item["sku"])
        total += PRICES[item["sku"]] * item["qty"]
    return round(total, 2)'''
NEW = OLD.replace("    return round(total, 2)",
                  "    # New: 10% off orders of 30.00 or more, or with a coupon.\n"
                  "    if total >= 30 or items[0][\"coupon\"]:\n"
                  "        total *= 0.9\n"
                  "    return round(total, 2)")
SAMPLES = [{"error_type": "KeyError", "error_message": "'coupon'",
            "error_where": "demo_shop/checkout.py:24 in _calculate_total__new"}] * 5


def main():
    try:
        client = GroqClient.from_env()
    except (RuntimeError, ValueError) as e:
        print(f"FAIL: {e}")
        return 1
    print(f"Model: {client.model}\n")
    ok = True

    t0 = time.monotonic()
    tri = triage(client, PR_FILES, CHANGED, pr_title="Discount for big orders and coupons")
    print(f"1. Triage ({time.monotonic() - t0:.1f}s): {'OK' if tri.ok else 'FAIL: ' + tri.error}")
    if tri.ok:
        print(f"   functions:  {tri.functions}")
        print(f"   flag name:  {tri.flag_name}")
        print(f"   risk tier:  {tri.tier}{' (defaulted)' if tri.tier_defaulted else ''}")
        for c in tri.reasons:
            print(f"   reason:     {c.file}:{c.line}  {c.claim}")
        print(f"   metric:     {tri.success_metric}  ({tri.success_metric_why})")
        print(f"   summary:    {tri.summary}")
    for n in tri.notes:
        print(f"   note:       {n}")
    ok &= tri.ok and bool(tri.functions)

    t0 = time.monotonic()
    prose = plan_prose(client, tri)
    print(f"\n2. Plan wording ({time.monotonic() - t0:.1f}s): {'from the AI' if prose.from_ai else 'FALLBACK'}")
    print(f"   summary:    {prose.summary}")
    print(f"   risk:       {prose.risk_explanation}")
    print(f"   metric:     {prose.metric_explanation}")
    for n in prose.notes:
        print(f"   note:       {n}")
    ok &= prose.from_ai

    t0 = time.monotonic()
    diag = diagnose(client, PR_FILES, "error rate vs old path",
                    "new path 33.3% errors (n=45), old path 0.0% errors (n=410)",
                    SAMPLES, "calculate_total", OLD, NEW)
    print(f"\n3. Diagnosis ({time.monotonic() - t0:.1f}s): "
          f"{'OK' if diag.available else 'UNAVAILABLE: ' + diag.error}")
    if diag.available:
        print(f"   cause:      {diag.likely_cause}")
        for c in diag.evidence:
            print(f"   evidence:   {c.file}:{c.line}  {c.claim}")
        print(f"   confidence: {diag.confidence}")
        print(f"   fix:        {diag.fix_description}")
        if diag.fix_code:
            where = f"{diag.fix_file}:{diag.fix_start}-{diag.fix_end}" if diag.fix_file else "snippet only"
            print(f"   fix code ({where}):")
            print("\n".join("      | " + line for line in diag.fix_code.split("\n")))
    for n in diag.notes:
        print(f"   note:       {n}")
    ok &= diag.available

    client.close()
    print("\nSUMMARY:", "all three calls worked" if ok else "something needs a look (see above)")
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except LLMError as e:
        print(f"FAIL: {e}")
        sys.exit(1)