"""Thursday step 3: check the demo shop + autopilot_sdk against the real account.

    python -m scripts.verify_step3

Needs demo_shop/.env with FLAGSMITH_SERVER_KEY (Production server-side key).

What it does, in Production:
 1. Creates the flag ap_checkout_v2 (off) and a segment "internal OR 25%".
 2. Runs the real shop in-process (SDK in local evaluation mode) and sends
    /checkout as 5 internal users, 100 regular users and 1 anonymous request.
 3. Checks each user's path against remote evaluation, which step 2 showed
    matches the edge API, and checks the metrics events.
 4. Removes the override (what a rollback does) and times how long until the
    shop serves the old path again.
 5. Deletes the segment and the flag.

The shop runs inside this script's process for convenience. In normal use it
runs on its own and only reads demo_shop/.env.

Since step 9, demo_shop/checkout.py has no flag of its own, so this script
runs the shop with the transformer's output for a sample discount change.
"""

import os
import sys
import time

from dotenv import load_dotenv
from fastapi.testclient import TestClient
from flagsmith import Flagsmith

from autopilot.flagsmith_client import INTERNAL_TRAIT, FlagsmithClient, FlagsmithError
import demo_shop.app as shop_module
from autopilot.transformer import wrap
from demo_shop import checkout as plain_checkout
from demo_shop.app import create_app, shop_config

FLAG = "ap_checkout_v2"


def flagged_checkout():
    """calculate_total wrapped behind FLAG, with a 10% discount at 30+ on the new path."""
    base = open(plain_checkout.__file__).read()
    head = base.replace("    return round(total, 2)",
                        "    if total >= 30:\n        total *= 0.9\n    return round(total, 2)")
    namespace = {}
    exec(compile(wrap(base, head, "calculate_total", FLAG), plain_checkout.__file__, "exec"), namespace)
    namespace["UnknownItem"], namespace["PRICES"] = plain_checkout.UnknownItem, plain_checkout.PRICES
    return namespace["calculate_total"]
SEGMENT = f"{FLAG}_verify"
CART = {"items": [{"sku": "beans", "qty": 2}]}  # old path 36.0, new path 32.4
NEW_TOTAL, OLD_TOTAL = 32.4, 36.0
N_INTERNAL, N_USERS = 5, 100
WAIT_SECONDS = 25  # edge propagation (~20s) + SDK refresh
SWITCH_OFF_TIMEOUT = 120


def checkout(client, user, internal):
    headers = {}
    if user:
        headers["X-User-Id"] = user
    if internal:
        headers["X-Internal"] = "true"
    r = client.post("/checkout", json=CART, headers=headers)
    total = r.json().get("total") if r.status_code == 200 else None
    path = {NEW_TOTAL: "new", OLD_TOTAL: "old"}.get(total, f"HTTP {r.status_code} {r.text[:80]}")
    return path


def remote_path(remote, user, internal):
    flags = remote.get_identity_flags(user, traits={INTERNAL_TRAIT: internal})
    return "new" if flags.is_feature_enabled(FLAG) else "old"


def main():
    load_dotenv()
    if not shop_config()["FLAGSMITH_SERVER_KEY"].startswith("ser."):
        print("FAILED: demo_shop/.env needs FLAGSMITH_SERVER_KEY=ser.… "
              "(Production -> Settings -> SDK Keys -> server-side key).")
        sys.exit(1)

    users = [(f"ap-shop-internal-{i}", True) for i in range(N_INTERNAL)]
    users += [(f"ap-shop-user-{i}", False) for i in range(N_USERS)]
    results, feature_id, segment_id = {}, None, None

    try:
        with FlagsmithClient.from_env() as fs:
            if fs.get_feature_by_name(FLAG):
                print(f"FAILED: a flag named {FLAG} already exists. This check creates and "
                      "deletes its own; delete the old one in the dashboard first.")
                sys.exit(1)
            remote = Flagsmith(environment_key=os.environ["FS_ENV_KEY"])
            try:
                print(f"1. Creating flag {FLAG} (off) and segment '{SEGMENT}' (internal OR 25%)...")
                feature_id = fs.create_feature(FLAG, "Temporary: Release Autopilot step 3 check")
                segment_id = fs.create_percentage_segment(SEGMENT, 25, include_internal=True)
                fs.enable_for_segment_only(feature_id, segment_id)
                print(f"   flag id {feature_id}, segment id {segment_id}; "
                      f"waiting {WAIT_SECONDS}s for the edge and the SDK refresh...")
                time.sleep(WAIT_SECONDS)

                events = []
                shop_module.calculate_total = flagged_checkout()
                app = create_app(sink=events.extend, metrics_url=None)
                with TestClient(app) as shop:
                    print("2. Sending /checkout as 105 users + 1 anonymous request...")
                    shop_paths = {u: checkout(shop, u, internal) for u, internal in users}
                    anonymous = checkout(shop, None, False)
                    app.state.autopilot.reporter.flush()

                    print("3. Comparing with remote evaluation...")
                    mismatches = [u for u, internal in users
                                  if shop_paths[u] != remote_path(remote, u, internal)]
                    internal_new = sum(shop_paths[u] == "new" for u, i in users if i)
                    regular_new = sum(shop_paths[u] == "new" for u, i in users if not i)
                    errors = [p for p in shop_paths.values() if p not in ("new", "old")]

                    print(f"   internal users on new path: {internal_new}/{N_INTERNAL} (expect {N_INTERNAL})")
                    print(f"   regular users on new path:  {regular_new}/{N_USERS} (expect 10-40)")
                    print(f"   anonymous request path: {anonymous} (expect old)")
                    print(f"   shop vs remote evaluation disagreements: {len(mismatches)} (expect 0)"
                          + (f", e.g. {mismatches[:3]}" if mismatches else ""))
                    print(f"   requests that failed: {len(errors)} (expect 0)"
                          + (f", e.g. {errors[:2]}" if errors else ""))

                    flagged = [e for e in events if e["flag"] == FLAG]
                    event_paths = [e["path"] for e in flagged]
                    expected_paths = [shop_paths[u] for u, _ in users] + [anonymous]
                    events_ok = (event_paths == expected_paths
                                 and all(e["ok"] and e["latency_ms"] > 0 for e in flagged))
                    print(f"   metrics events: {len(flagged)} (expect {len(users) + 1}); "
                          f"paths match responses and all ok: {events_ok}")

                    results["local_eval_matches_remote"] = not mismatches and not errors
                    results["internal_header_works"] = internal_new == N_INTERNAL
                    results["split_about_25_percent"] = 10 <= regular_new <= 40
                    results["anonymous_gets_old_path"] = anonymous == "old"
                    results["metrics_events_correct"] = events_ok

                    print("4. Switching the flag off (removing the override, as a rollback does)...")
                    fs.remove_segment_override(feature_id, segment_id)
                    started = time.monotonic()
                    seconds = None
                    while time.monotonic() - started < SWITCH_OFF_TIMEOUT:
                        if all(checkout(shop, u, True) == "old" for u, i in users if i):
                            seconds = round(time.monotonic() - started)
                            break
                        time.sleep(2)
                    if seconds is None:
                        print(f"   still on after {SWITCH_OFF_TIMEOUT}s")
                    else:
                        print(f"   the shop served the old path to everyone after {seconds}s")
                    results["switch_off_seconds"] = seconds
            finally:
                print("5. Cleaning up...")
                steps = []
                if segment_id is not None:
                    steps += [
                        ("remove the override (404 = already removed)",
                         lambda: fs.remove_segment_override(feature_id, segment_id)),
                        ("delete the segment", lambda: fs.delete_segment(segment_id)),
                    ]
                if feature_id is not None:
                    steps.append(("delete the flag", lambda: fs.delete_feature(feature_id)))
                for label, step in steps:
                    try:
                        step()
                        print(f"   done: {label}")
                    except FlagsmithError as e:
                        if e.status_code == 404 and "override" in label:
                            print(f"   done: {label}")
                        else:
                            print(f"   WARNING: could not {label} ({e}). Do it in the dashboard "
                                  f"(flag {FLAG}, segment '{SEGMENT}').")
                remote.close()
    except (FlagsmithError, RuntimeError, ValueError) as e:
        print(f"\nFAILED: {e}")
    except Exception as e:  # SDK errors have their own types
        print(f"\nFAILED (SDK or unexpected): {type(e).__name__}: {e}")
    finally:
        print("\nSUMMARY")
        for key in ("local_eval_matches_remote", "internal_header_works", "split_about_25_percent",
                    "anonymous_gets_old_path", "metrics_events_correct"):
            value = results.get(key)
            print(f"   {key}: {'not reached' if value is None else ('PASS' if value else 'FAIL')}")
        seconds = results.get("switch_off_seconds", "not reached")
        print(f"   switch_off_seconds: {seconds}")
        checks = [results.get(k) for k in ("local_eval_matches_remote", "internal_header_works",
                                           "split_about_25_percent", "anonymous_gets_old_path",
                                           "metrics_events_correct")]
        if not all(checks) or not isinstance(results.get("switch_off_seconds"), int):
            sys.exit(1)


if __name__ == "__main__":
    main()