"""Check flagsmith_client.py against the real Flagsmith account.

    python -m scripts.smoke_flagsmith           read-only: changes nothing
    python -m scripts.smoke_flagsmith --write   also runs a rollout round trip

--write only touches the test flag (FS_TEST_FEATURE_NAME, default
show_demo_button) in the FS_ENV_KEY environment, and refuses to run if that
flag is already on there. It creates a temporary segment, turns the flag on
for 25% of users, counts them on the edge, then removes the override and the
segment, so the flag ends off for everyone. It is also the check that the
Organisation API key is accepted by the experimental __future__ endpoints.
"""

import argparse
import os
import sys
import time

from dotenv import load_dotenv

from autopilot.flagsmith_client import FlagsmithClient, FlagsmithError

PROPAGATION_WAIT_SECONDS = 20
N_USERS = 100


def count_enabled(fs, flag_name):
    enabled = 0
    for i in range(N_USERS):
        if fs.get_identity_flags(f"smoke-user-{i}").get(flag_name):
            enabled += 1
    return enabled


def read_only_checks(fs, flag_name):
    print("1. Projects:")
    for p in fs.list_projects():
        print(f"   {p['id']}  {p['name']}")

    print(f"2. Environments in project {fs.project_id}:")
    envs = fs.list_environments()
    for e in envs:
        marker = "  <- FS_ENV_KEY" if e.get("api_key") == fs.env_key else ""
        print(f"   {e['id']}  {e['name']}{marker}")
    if not any(e.get("api_key") == fs.env_key for e in envs):
        print("   WARNING: FS_ENV_KEY matches none of these environments.")

    features = fs.list_features()
    print(f"3. Features: {len(features)} found")
    feature = fs.get_feature_by_name(flag_name)
    if feature is None:
        print(f"   Test flag '{flag_name}' not found. Set FS_TEST_FEATURE_NAME.")
        sys.exit(1)
    print(f"   test flag '{flag_name}' has id {feature['id']}")

    flags = fs.get_identity_flags("smoke-user-0")
    print(f"4. Edge API: smoke-user-0 sees {len(flags)} flags; "
          f"'{flag_name}' enabled = {flags.get(flag_name)}")
    return feature["id"], bool(flags.get(flag_name))


def write_round_trip(fs, feature_id, flag_name):
    name = f"ap_smoke_{int(time.time())}"
    print(f"\n5. Creating segment '{name}' at 5%...")
    segment_id = fs.create_percentage_segment(name, 5)
    print(f"   segment id {segment_id}")
    try:
        print("6. Flag off for everyone, on for the segment only...")
        fs.enable_for_segment_only(feature_id, segment_id)
        print("7. Widening the same segment to 25% in place...")
        fs.set_segment_percentage(segment_id, name, 25)

        print(f"   waiting {PROPAGATION_WAIT_SECONDS}s for the edge...")
        time.sleep(PROPAGATION_WAIT_SECONDS)
        enabled = count_enabled(fs, flag_name)
        if enabled == 0:
            print("   nobody has it yet; waiting another 30s...")
            time.sleep(30)
            enabled = count_enabled(fs, flag_name)
        print(f"   {enabled}/{N_USERS} users have the flag (expect roughly 25)")
    finally:
        # Each cleanup step runs even if an earlier one failed, and a failure
        # here only warns, so the original error (if any) is the one reported.
        print("8. Cleaning up...")
        for label, step in (
            ("remove the segment override", lambda: fs.remove_segment_override(feature_id, segment_id)),
            ("delete the test segment", lambda: fs.delete_segment(segment_id)),
        ):
            try:
                step()
                print(f"   done: {label}")
            except FlagsmithError as e:
                print(f"   WARNING: could not {label} ({e}). "
                      f"Do it in the dashboard: segment '{name}', id {segment_id}.")

    # The rollback path may try to remove an override that's already gone.
    # Record what Flagsmith returns so the engine can treat it correctly.
    print("9. Probe: removing the same override a second time...")
    try:
        fs.remove_segment_override(feature_id, segment_id)
        print("   accepted (2xx): a repeat delete is harmless")
    except FlagsmithError as e:
        print(f"   HTTP {e.status_code}: {e.body[:200]}")

    ok = 10 <= enabled <= 40
    print("\nPASS" if ok else "\nCHECK: the count is off. Paste this output.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true", help="also run the rollout round trip")
    args = parser.parse_args()

    load_dotenv()  # before reading FS_TEST_FEATURE_NAME, which may be in .env
    flag_name = os.environ.get("FS_TEST_FEATURE_NAME", "show_demo_button")
    try:
        with FlagsmithClient.from_env() as fs:
            feature_id, already_on = read_only_checks(fs, flag_name)
            if args.write and already_on:
                print(f"\nNot running --write: '{flag_name}' is already on in this "
                      "environment, and the round trip would switch it off.")
                sys.exit(1)
            if args.write:
                write_round_trip(fs, feature_id, flag_name)
            else:
                print("\nRead-only checks passed. Run with --write for the round trip.")
    except (FlagsmithError, RuntimeError) as e:
        print(f"\nFAILED: {e}")
        scheme = os.environ.get("FS_AUTH_SCHEME", "Api-Key")
        if isinstance(e, FlagsmithError) and e.status_code in (401, 403) and scheme == "Api-Key":
            print("\nThe Organisation API key was rejected for this call. Fallback: in .env set\n"
                  "FS_AUTH_SCHEME=Token and FS_TOKEN=<your personal token>, then re-run.")
        sys.exit(1)


if __name__ == "__main__":
    main()