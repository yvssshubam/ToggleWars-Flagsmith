"""Simulated shoppers for the demo shop.

Sends POST /checkout at a steady rate as rotating users. A share of requests
come from staff (X-Internal: true), so the internal stage gets enough traffic:
the risk tiers need 20-50 new-path requests within a 30-120s soak, i.e. about
0.5-0.7 staff requests per second. 10 req/s with 10% staff gives ~1/s.

After seeing the price, a shopper places the order (POST /order) with a
probability that falls as the total rises (buy_probability), so a change that
lowers prices really converts better and a price rise converts worse. That's
what stage 3.5, "prove it helps", measures.

The simulator talks only to the shop. It makes no Flagsmith calls.
"""

import asyncio
import math
import random
import time
from dataclasses import dataclass, field

import httpx

SKUS = ("tea", "coffee", "mug", "beans")


def make_cart(rng):
    """1-3 distinct items, quantity 1-3 each."""
    skus = rng.sample(SKUS, rng.randint(1, 3))
    return {"items": [{"sku": s, "qty": rng.randint(1, 3)} for s in skus]}


def pick_user(rng, users, staff, internal_share):
    """(user_id, is_internal). Staff ids are separate from regular ids, so a
    user never switches between internal and regular."""
    if staff and rng.random() < internal_share:
        return f"staff-{rng.randrange(staff)}", True
    return f"user-{rng.randrange(users)}", False


def buy_probability(total):
    """Chance a shopper accepts a price: about 90% for small carts, 50% at 35.00,
    about 10% above 55.00. Price-sensitive enough that a 10% discount on bigger
    carts shows up in a few hundred checkouts."""
    return 1 / (1 + math.exp((float(total) - 35) / 8))


def headers_for(user_id, internal):
    headers = {"X-User-Id": user_id}
    if internal:
        headers["X-Internal"] = "true"
    return headers


@dataclass
class Window:
    """Counts for one reporting window."""
    sent: int = 0
    internal: int = 0
    orders: int = 0
    by_status: dict = field(default_factory=dict)  # "2xx" / "4xx" / "5xx" / "error"
    latencies_ms: list = field(default_factory=list)

    def record(self, internal, status_class, latency_ms):
        self.sent += 1
        self.internal += internal
        self.by_status[status_class] = self.by_status.get(status_class, 0) + 1
        if latency_ms is not None:
            self.latencies_ms.append(latency_ms)

    def line(self, seconds):
        lat = sorted(self.latencies_ms)
        p50 = lat[len(lat) // 2] if lat else 0
        p95 = lat[min(len(lat) - 1, int(len(lat) * 0.95))] if lat else 0
        statuses = " ".join(f"{k}={self.by_status[k]}" for k in ("2xx", "4xx", "5xx", "error")
                            if k in self.by_status) or "none"
        ok = self.by_status.get("2xx", 0)
        orders = f" | {self.orders} orders ({self.orders / ok:.0%} of priced carts)" if ok else ""
        return (f"{self.sent / seconds:5.1f} req/s | {self.sent} sent ({self.internal} staff) | "
                f"{statuses} | p50 {p50:.0f} ms, p95 {p95:.0f} ms{orders}")


async def _one(client, url, rng, users, staff, internal_share, window, total, orders=True):
    user_id, internal = pick_user(rng, users, staff, internal_share)
    cart, headers = make_cart(rng), headers_for(user_id, internal)
    start = time.perf_counter()
    r = None
    try:
        r = await client.post(url, json=cart, headers=headers)
        status_class = f"{r.status_code // 100}xx"
        latency = (time.perf_counter() - start) * 1000
    except httpx.HTTPError:
        status_class, latency = "error", None
    window.record(internal, status_class, latency)
    total.record(internal, status_class, latency)
    if not orders or r is None or r.status_code != 200:
        return  # a failed checkout never becomes an order
    try:
        price = r.json()["total"]
    except (ValueError, KeyError, TypeError):
        return
    if rng.random() < buy_probability(price):
        try:
            placed = await client.post(url.rsplit("/", 1)[0] + "/order", json=cart, headers=headers)
        except httpx.HTTPError:
            return
        if placed.status_code == 200:
            window.orders += 1
            total.orders += 1


async def run(base_url, rps=10.0, duration=0, users=200, staff=10, internal_share=0.10,
              seed=None, report_every=5.0, transport=None, out=print, total=None, orders=True):
    """Send traffic until `duration` seconds pass (0 = until cancelled).
    Returns the totals as a Window. Pass your own `total` to keep the counts
    even if the run is interrupted (Ctrl+C)."""
    if rps <= 0:
        raise ValueError("rps must be positive")
    rng = random.Random(seed)
    url = base_url.rstrip("/") + "/checkout"
    total = total if total is not None else Window()
    window = Window()
    tasks = set()
    started = last_report = time.monotonic()
    next_send = started

    async with httpx.AsyncClient(timeout=5, transport=transport) as client:
        try:
            while duration <= 0 or time.monotonic() - started < duration:
                now = time.monotonic()
                if now < next_send:
                    await asyncio.sleep(next_send - now)
                next_send += 1 / rps
                task = asyncio.create_task(
                    _one(client, url, rng, users, staff, internal_share, window, total, orders))
                tasks.add(task)
                task.add_done_callback(tasks.discard)

                if time.monotonic() - last_report >= report_every:
                    out(window.line(time.monotonic() - last_report))
                    window = Window()
                    last_report = time.monotonic()
        finally:
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
    return total