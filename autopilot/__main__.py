"""Release Autopilot command line (Thursday: no GitHub yet).

    python -m autopilot release ap_checkout_v2 --risk medium   start (or resume) and run the bot
    python -m autopilot run                                    run the bot for active releases
    python -m autopilot approve ap_checkout_v2                 approve the release to everyone
    python -m autopilot rollback ap_checkout_v2                switch it off now
    python -m autopilot status                                 list releases and their events
    python -m autopilot teardown ap_checkout_v2                delete the flag + segment (testing)

`release` and `run` start the bot: a metrics endpoint on port 8000 (the shop
posts to it) and the engine loop. Stop with Ctrl+C; state is kept in
autopilot.db, so running it again resumes where it stopped. The other
commands are one-shot and talk to the running bot through autopilot.db.
"""

import argparse
import getpass
import threading
import time

import uvicorn

from autopilot.engine import Engine
from autopilot.flagsmith_client import FlagsmithClient, FlagsmithError
from autopilot.policy import TIERS, describe, for_tier
from autopilot.server import create_app
from autopilot.store import Store

DB_PATH = "autopilot.db"


def serve_metrics(store, port):
    config = uvicorn.Config(create_app(store), host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(50):  # wait up to 5s for it to start
        if server.started:
            return server
        if not thread.is_alive():
            break
        time.sleep(0.1)
    raise SystemExit(f"Couldn't start the metrics endpoint on port {port}. Is another bot running?")


def run_bot(engine, store, port, tick_seconds):
    server = serve_metrics(store, port)
    print(f"Bot running: metrics at http://127.0.0.1:{port}/metrics, engine tick every {tick_seconds:g}s. "
          "Ctrl+C to stop (state is kept).")
    try:
        while True:
            try:
                engine.tick_all()
            except FlagsmithError as e:  # never crash the loop on a Flagsmith hiccup
                print(f"Flagsmith error, will retry: {e}")
            if not store.active_releases():
                print("No active releases. Waiting (Ctrl+C to stop)...")
            time.sleep(tick_seconds)
    except KeyboardInterrupt:
        print("\nstopped; run the same command again to resume")
    finally:
        server.should_exit = True


def print_status(store, flag=None):
    releases = store.all_releases()
    if flag:
        releases = [r for r in releases if r["flag"] == flag]
    if not releases:
        print("No releases yet. Start one with: python -m autopilot release ap_checkout_v2 --risk medium")
        return
    for r in releases[:5]:
        print(f"#{r['id']} {r['flag']}: {r['state']}"
              + (f" (approved by {r['approved_by']})" if r["approved_by"] else "")
              + (f" (switch-off reason: {r['rollback_reason']})" if r["rollback_reason"] else ""))
        for e in store.events(r["id"])[-8:]:
            print(f"   {time.strftime('%H:%M:%S', time.localtime(e['ts']))}  {e['message']}")


def main():
    p = argparse.ArgumentParser(prog="python -m autopilot", description="Release Autopilot (CLI)")
    sub = p.add_subparsers(dest="cmd", required=True)
    rel = sub.add_parser("release", help="start (or resume) a release and run the bot")
    rel.add_argument("flag")
    rel.add_argument("--risk", choices=list(TIERS), default="medium")
    for name in ("run",):
        sub.add_parser(name, help="run the bot for active releases")
    for name, helptext in (("approve", "approve the release to everyone"),
                           ("rollback", "switch the flag off now"),
                           ("teardown", "delete the flag and its segment (testing)")):
        sp = sub.add_parser(name, help=helptext)
        sp.add_argument("flag")
    st = sub.add_parser("status", help="show releases and recent events")
    st.add_argument("flag", nargs="?")
    for sp in sub.choices.values():
        sp.add_argument("--port", type=int, default=8000)
        sp.add_argument("--tick", type=float, default=5.0, help="engine tick in seconds")
    args = p.parse_args()

    store = Store(DB_PATH)
    if args.cmd == "status":
        print_status(store, args.flag)
        return

    user = getpass.getuser()
    if args.cmd in ("approve", "rollback"):
        # These only record a request in autopilot.db; the running bot acts on it.
        engine = Engine(store, fs=None)
        action = engine.approve if args.cmd == "approve" else engine.request_rollback
        print(action(args.flag, by=user))
        return

    with FlagsmithClient.from_env() as fs:
        engine = Engine(store, fs)
        if args.cmd == "release":
            existing = store.latest_release(args.flag)
            if existing and existing["state"] in ("stage_0", "stage_1", "stage_2", "awaiting_approval", "full"):
                print(f"Resuming release #{existing['id']} of {args.flag} (state {existing['state']}).")
            else:
                print(f"Plan: {describe(for_tier(args.risk))}")
                engine.start_release(args.flag, for_tier(args.risk), started_by=user)
            run_bot(engine, store, args.port, args.tick)
        elif args.cmd == "run":
            run_bot(engine, store, args.port, args.tick)
        elif args.cmd == "teardown":
            typed = input(f"This deletes the flag {args.flag} and its segment from Flagsmith. "
                          f"Type the flag name to confirm: ").strip()
            if typed != args.flag:
                print("Not confirmed; nothing deleted.")
                return
            for line in engine.teardown(args.flag):
                print(" -", line)


if __name__ == "__main__":
    main()