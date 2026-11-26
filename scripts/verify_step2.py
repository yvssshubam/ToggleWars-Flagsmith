"""Thursday step 2: check the plan's three unverified items on the real account.

    python -m scripts.verify_step2

1. Creating (and deleting) a flag through the API.
2. One segment rule "is_internal == true OR % split", starting at "0".
3. The Python SDK's per-user results matching the edge API.

It creates a throwaway flag `ap_verify_<time>` and its segment in the
FS_ENV_KEY environment, and deletes both at the end. show_demo_button is not
touched. The SDK records the test users and their is_internal trait as
identities in that environment; those stay behind (harmless, but visible in
the dashboard's Identities list).
"""

import os
import sys
import time

from dotenv import load_dotenv
from flagsmith import Flagsmith

from autopilot.flagsmith_client import INTERNAL_TRAIT, FlagsmithClient, FlagsmithError

PROPAGATION_WAIT_SECONDS = 20
N_INTERNAL = 5
N_USERS = 100


def sdk_flag(sdk, identifier, flag_name, internal):
    """What the SDK says for one user: True, False, or None if the flag is missing."""
    flags = sdk.get_identity_flags(identifier, traits={INTERNAL_TRAIT: internal})
    for flag in flags.all_flags():
        if flag.feature_name == flag_name:
            return flag.enabled
    return None


def evaluate(fs, sdk, flag_name, users):
    """Ask the SDK (with traits) and the edge API (no traits) about every user."""
    results = {}
    for identifier, internal in users:
        via_sdk = sdk_flag(sdk, identifier, flag_name, internal)
        via_edge = fs.get_identity_flags(identifier).get(flag_name)
        results[identifier] = (internal, via_sdk, via_edge)
    return results


def report(label, results, expect_internal, expect_regular_range):
    internal = [r for r in results.values() if r[0]]
    regular = [r for r in results.values() if not r[0]]
    internal_on = sum(1 for r in internal if r[1])
    regular_on = sum(1 for r in regular if r[1])
    missing = sum(1 for r in results.values() if r[1] is None)
    mismatches = [i for i, r in results.items() if r[1] != r[2]]
    low, high = expect_regular_range

    print(f"   {label}:")
    print(f"     internal users on (SDK): {internal_on}/{len(internal)} (expect {expect_internal})")
    print(f"     regular users on (SDK):  {regular_on}/{len(regular)} (expect {low}-{high})")
    print(f"     flag missing from SDK result: {missing} (expect 0)")
    print(f"     SDK vs edge API disagreements: {len(mismatches)} (expect 0)"
          + (f", e.g. {mismatches[:3]}" if mismatches else ""))
    return (internal_on == expect_internal and low <= regular_on <= high
            and missing == 0 and not mismatches)


def wait_for_edge():
    print(f"   waiting {PROPAGATION_WAIT_SECONDS}s for the edge...")
    time.sleep(PROPAGATION_WAIT_SECONDS)


def create_rollout_segment(fs, name):
    """Try the plan's rule (internal OR 0% split). If it's rejected, find out
    whether "0" or the combination is the problem, then use what works."""
    try:
        return fs.create_percentage_segment(name, 0, include_internal=True), 0
    except FlagsmithError as e:
        print(f"   internal OR 0% REJECTED: HTTP {e.status_code}: {e.body[:300]}")
    try:
        segment_id = fs.create_percentage_segment(name, 1, include_internal=True)
        print("   internal OR 1% accepted, so \"0\" is the problem; continuing with 1%")
        return segment_id, 1
    except FlagsmithError as e:
        print(f"   internal OR 1% also REJECTED: HTTP {e.status_code}: {e.body[:300]}")
        print("   The combined rule doesn't work; the plan's fallback is a separate "
              "internal-only segment for Stage 0.")
        return None, None


def main():
    load_dotenv()
    env_key = os.environ.get("FS_ENV_KEY")
    users = [(f"ap-verify-internal-{i}", True) for i in range(N_INTERNAL)]
    users += [(f"ap-verify-user-{i}", False) for i in range(N_USERS)]
    flag_name = f"ap_verify_{int(time.time())}"
    feature_id = segment_id = None
    results = {}

    try:
        with FlagsmithClient.from_env() as fs:
            sdk = Flagsmith(environment_key=env_key)
            try:
                print(f"ITEM 1: create a flag\n   creating '{flag_name}'...")
                feature_id = fs.create_feature(flag_name, "Temporary: Release Autopilot step 2 check")
                found = fs.get_feature_by_name(flag_name)
                print(f"   created, id {feature_id}; listed by the API: {found is not None}")
                results["item1_create"] = found is not None

                print(f"\nITEM 2: one segment rule '{INTERNAL_TRAIT} == true' OR % split")
                segment_id, start_pct = create_rollout_segment(fs, f"{flag_name}_rollout")
                if segment_id is None:
                    results["item2_combined_rule"] = False
                    return
                print(f"   segment id {segment_id}, starting at {start_pct}%")
                fs.enable_for_segment_only(feature_id, segment_id)

                wait_for_edge()
                print("   evaluating 105 users (SDK with traits, then edge API)...")
                # At 0% no regular user should have it; at the 1% fallback, a few may.
                regular_max = 0 if start_pct == 0 else 5
                ok_stage0 = report(f"Stage 0 (internal + {start_pct}%)",
                                   evaluate(fs, sdk, flag_name, users),
                                   N_INTERNAL, (0, regular_max))

                print("\n   widening the same segment to 25% (internal rule kept)...")
                fs.set_segment_percentage(segment_id, f"{flag_name}_rollout", 25,
                                          include_internal=True)
                wait_for_edge()
                print("   evaluating again...")
                at_25 = evaluate(fs, sdk, flag_name, users)
                ok_stage2 = report("Stage 2 (internal + 25%)", at_25, N_INTERNAL, (10, 40))

                results["item2_combined_rule"] = ok_stage0 and ok_stage2
                results["item2_zero_accepted"] = start_pct == 0
                results["item3_sdk_matches_edge"] = all(r[1] == r[2] for r in at_25.values())
            finally:
                # Every step runs even if an earlier one failed; failures only
                # warn, so the original error (if any) is the one reported.
                print("\nCLEANUP")
                steps = []
                if segment_id is not None:
                    steps += [
                        (None, "remove the segment override",
                         lambda: fs.remove_segment_override(feature_id, segment_id)),
                        (None, "delete the segment", lambda: fs.delete_segment(segment_id)),
                    ]
                if feature_id is not None:
                    steps.append(("item1_delete", "delete the flag",
                                  lambda: fs.delete_feature(feature_id)))
                for key, label, step in steps:
                    try:
                        step()
                        print(f"   done: {label}")
                        ok = True
                    except FlagsmithError as e:
                        print(f"   WARNING: could not {label} ({e}). Do it in the dashboard "
                              f"(flag '{flag_name}', segment id {segment_id}).")
                        ok = False
                    if key:
                        results[key] = ok
                sdk.close()
    except (FlagsmithError, RuntimeError) as e:
        print(f"\nFAILED: {e}")
    except Exception as e:  # the SDK raises its own error types
        print(f"\nFAILED (SDK or unexpected): {type(e).__name__}: {e}")
    finally:
        print("\nSUMMARY")
        for key in ("item1_create", "item1_delete", "item2_combined_rule",
                    "item2_zero_accepted", "item3_sdk_matches_edge"):
            value = results.get(key)
            print(f"   {key}: {'not reached' if value is None else ('PASS' if value else 'FAIL')}")
        if not all(results.get(k) for k in ("item1_create", "item1_delete",
                                            "item2_combined_rule", "item3_sdk_matches_edge")):
            sys.exit(1)


if __name__ == "__main__":
    main()