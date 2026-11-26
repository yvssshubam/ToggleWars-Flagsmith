"""ASGI middleware: sets the user context, times the request, reports paths.

Plain ASGI rather than Starlette's BaseHTTPMiddleware, so the contextvar set
here is the one the endpoint sees, for both async and sync endpoints.
"""

import time

from .flags import RequestContext, _request


class AutopilotMiddleware:
    def __init__(self, app, reporter):
        self.app = app
        self.reporter = reporter

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers") or [])
        user_id = headers.get(b"x-user-id", b"").decode("latin-1").strip() or None
        internal = headers.get(b"x-internal", b"").decode("latin-1").strip().lower() == "true"
        ctx = RequestContext(user_id=user_id, internal=internal)
        token = _request.set(ctx)

        status = {"code": 500}

        async def send_and_record(message):
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
            await send(message)

        start = time.perf_counter()
        ok = False
        try:
            await self.app(scope, receive, send_and_record)
            # 4xx is the client's mistake, not a broken code path.
            ok = status["code"] < 500
        finally:
            # Runs on success and on an unhandled exception (ok stays False).
            latency_ms = (time.perf_counter() - start) * 1000
            now = time.time()
            for flag, enabled in ctx.flags.items():
                self.reporter.report({
                    "flag": flag,
                    "path": "new" if enabled else "old",
                    "ok": ok,
                    "latency_ms": round(latency_ms, 2),
                    "ts": now,
                })
            _request.reset(token)
