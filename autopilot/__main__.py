"""Release Autopilot command line.

    python -m autopilot release ap_checkout_v2 --risk medium   start (or resume) and run the bot
    python -m autopilot run                                    run the bot for active releases
    python -m autopilot approve ap_checkout_v2                 approve the release to everyone
    python -m autopilot rollback ap_checkout_v2                switch it off now
    python -m autopilot status                                 list releases and their events
    python -m autopilot teardown ap_checkout_v2                delete the flag + segment (testing)

`release` and `run` start the bot: an HTTP server on port 8000 (the shop posts
metrics to /metrics; GitHub posts to /webhooks/github through ngrok) and the
loop that handles GitHub events and drives releases. Stop with Ctrl+C; state
is kept in autopilot.db, so running it again resumes where it stopped. The
other commands are one-shot and talk to the running bot through autopilot.db.

GitHub is used when .env has GITHUB_TOKEN, GITHUB_REPO and
GITHUB_WEBHOOK_SECRET (and ALLOWED_APPROVERS); without them the bot runs in
CLI-only mode.
"""

import argparse
import getpass
import os
import threading
import time

import uvicorn
from dotenv import load_dotenv

from autopilot.cleanup import Cleanup
from autopilot.engine import Engine
from autopilot.flagsmith_client import FlagsmithClient, FlagsmithError
from autopilot.github_client import GitHubClient, GitHubError
from autopilot.llm import GroqClient
from autopilot.policy import TIERS, describe, for_tier
from autopilot.pr_handler import PRHandler, git_pull
from autopilot.pr_notifier import PRNotifier
from autopilot.server import create_app
from autopilot.store import Store

DB_PATH = "autopilot.db"


def github_settings():
    """(token, repo, secret, approvers), or None if GitHub isn't configured."""
    token, repo, secret = (os.environ.get(n, "").strip()
                           for n in ("GITHUB_TOKEN", "GITHUB_REPO", "GITHUB_WEBHOOK_SECRET"))
    if not (token and repo and secret):
        return None
    approvers = [a for a in os.environ.get("ALLOWED_APPROVERS", "").split(",") if a.strip()]
    return token, repo, secret, approvers


def serve(store, port, github_secret=None, github_repo=None):
    app = create_app(store, github_secret=github_secret, github_repo=github_repo)
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(50):  # wait up to 5s for it to start
        if server.started:
            return server
        if not thread.is_alive():
            break
        time.sleep(0.1)
    raise SystemExit(f"Couldn't start the bot's server on port {port}. Is another bot running?")


def run_bot(engine, store, port, tick_seconds):
    settings = github_settings()
    handler = gh = llm = cleanup = None
    if settings:
        token, repo, secret, approvers = settings
        gh = GitHubClient(token, repo)
        try:
            llm = GroqClient.from_env()
            print(f"AI: Groq {llm.model}")
        except (RuntimeError, ValueError):
            print("AI: GROQ_API_KEY not set; PRs will get 'couldn't auto-flag' instead of a plan")
        engine.notify = PRNotifier(store, gh, llm)
        cleanup = Cleanup(store, gh, engine.fs, git_pull, approvers=approvers)
        handler = PRHandler(store, gh, approvers, llm=llm, fs=engine.fs, engine=engine, cleanup=cleanup)
        server = serve(store, port, github_secret=secret, github_repo=repo)
        print(f"GitHub: watching {repo}; approvers: {', '.join(approvers) or 'NONE (nobody can approve)'}")
        print(f"        webhook endpoint http://127.0.0.1:{port}/webhooks/github "
              f"(expose it with: ngrok http {port})")
    else:
        server = serve(store, port)
        print("GitHub not configured (needs GITHUB_TOKEN, GITHUB_REPO, GITHUB_WEBHOOK_SECRET in .env): CLI-only mode")
    print(f"Bot running: metrics at http://127.0.0.1:{port}/metrics, tick every {tick_seconds:g}s. "
          "Ctrl+C to stop (state is kept).")
    idle_reported = False
    try:
        while True:
            if handler:
                try:
                    handler.process_pending()
                except GitHubError as e:  # never crash the loop on a GitHub hiccup
                    print(f"GitHub error, will retry: {e}")
            try:
                engine.tick_all()
            except FlagsmithError as e:  # never crash the loop on a Flagsmith hiccup
                print(f"Flagsmith error, will retry: {e}")
            if cleanup:
                cleanup.tick()  # handles its own GitHub/Flagsmith errors
            idle = not store.active_releases()
            if idle and not idle_reported:
                print("No active releases. Waiting (Ctrl+C to stop)...")
            idle_reported = idle
            time.sleep(tick_seconds)
    except KeyboardInterrupt:
        print("\nstopped; run the same command again to resume")
    finally:
        server.should_exit = True
        if gh:
            gh.close()
        if llm:
            llm.close()


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

    load_dotenv()
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