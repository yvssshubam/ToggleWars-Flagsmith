"""Run simulated traffic against the demo shop.

    python -m simulator                         # 10 req/s until Ctrl+C
    python -m simulator --rps 20 --duration 60  # 20 req/s for one minute

Start the shop first:  uvicorn demo_shop.app:app --port 8001
"""

import argparse
import asyncio
import time

from simulator.traffic import Window, run


def main():
    p = argparse.ArgumentParser(description="Simulated shoppers for the demo shop.")
    p.add_argument("--url", default="http://127.0.0.1:8001", help="shop base URL")
    p.add_argument("--rps", type=float, default=10.0, help="requests per second (default 10)")
    p.add_argument("--duration", type=float, default=0, help="seconds to run; 0 = until Ctrl+C")
    p.add_argument("--users", type=int, default=200, help="regular users (default 200)")
    p.add_argument("--staff", type=int, default=10, help="internal staff users (default 10)")
    p.add_argument("--internal-share", type=float, default=0.10,
                   help="share of requests from staff (default 0.10)")
    p.add_argument("--seed", type=int, default=None, help="random seed, for repeatable runs")
    args = p.parse_args()

    if not 0 <= args.internal_share <= 1:
        p.error("--internal-share must be between 0 and 1")

    print(f"Sending {args.rps:g} req/s to {args.url}/checkout as {args.users} users + "
          f"{args.staff} staff ({args.internal_share:.0%} of requests). Ctrl+C to stop.")
    started = time.monotonic()
    totals = Window()  # created here so the counts survive Ctrl+C
    try:
        asyncio.run(run(
            args.url, rps=args.rps, duration=args.duration, users=args.users,
            staff=args.staff, internal_share=args.internal_share, seed=args.seed, total=totals))
    except KeyboardInterrupt:
        print("\nstopped")
    print("TOTAL  " + totals.line(max(time.monotonic() - started, 0.001)))
    if totals.sent and totals.by_status.get("error") == totals.sent:
        print("Every request failed to connect. Is the shop running on that URL?")


if __name__ == "__main__":
    main()
