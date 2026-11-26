"""Sends one event per flagged request to the bot, in the background.

Reporting must never slow down or break a request, so report() only puts the
event on a queue. A daemon thread sends batches every flush interval. If the
bot is down, the batch is dropped and counted; the shop keeps working.
"""

import logging
import queue
import threading

import httpx

log = logging.getLogger("autopilot_sdk")


class Reporter:
    def __init__(self, url=None, sink=None, flush_seconds=1.0, max_queue=10_000):
        """url: the bot's metrics endpoint (POST {"events": [...]}).
        sink: optional callable(list_of_events), used by tests and checks."""
        self.url = url
        self.sink = sink
        self.flush_seconds = flush_seconds
        self.sent = 0
        self.dropped = 0
        self._queue = queue.Queue(maxsize=max_queue)
        self._stop = threading.Event()
        self._http = httpx.Client(timeout=2) if url else None
        if not url and not sink:
            log.warning("no AUTOPILOT_METRICS_URL set; metrics events are discarded")
        self._thread = threading.Thread(target=self._run, daemon=True, name="autopilot-reporter")
        self._thread.start()

    def report(self, event):
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            self.dropped += 1

    def _drain(self):
        batch = []
        while True:
            try:
                batch.append(self._queue.get_nowait())
            except queue.Empty:
                return batch

    def flush(self):
        batch = self._drain()
        if not batch:
            return
        if self.sink:
            self.sink(batch)
        if self._http:
            try:
                r = self._http.post(self.url, json={"events": batch})
                r.raise_for_status()
            except httpx.HTTPError as e:
                self.dropped += len(batch)
                log.warning("could not send %d metrics events: %s", len(batch), e)
                return
        self.sent += len(batch)

    def _run(self):
        while not self._stop.wait(self.flush_seconds):
            self.flush()

    def close(self):
        self._stop.set()
        self._thread.join(timeout=5)
        self.flush()
        if self._http:
            self._http.close()
