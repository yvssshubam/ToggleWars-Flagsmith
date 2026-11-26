"""ASGI middleware: sets the user context, times the request, reports paths.

Plain ASGI rather than Starlette's BaseHTTPMiddleware, so the contextvar set
here is the one the endpoint sees, for both async and sync endpoints.

For a failed request it also reports what failed and where (error type, a
truncated message, and the innermost line of app code), which the diagnosis
needs. It never reports request bodies, headers or user ids.
"""

import os
import time
import traceback

from .flags import RequestContext, _request

MAX_MESSAGE = 200


def _app_location(exc):
    """'path/to/file.py:LINE in function' for the innermost frame in app code,
    skipping installed libraries and this SDK."""
    frames = traceback.extract_tb(exc.__traceback__)
    sdk_dir = os.path.dirname(os.path.abspath(__file__))
    for frame in reversed(frames):
        filename = os.path.abspath(frame.filename)
        if "site-packages" in filename or filename.startswith(sdk_dir):
            continue
        try:
            shown = os.path.relpath(filename)
        except ValueError:  # different drive on Windows
            shown = os.path.basename(filename)
        return f"{shown.replace(os.sep, '/')}:{frame.lineno} in {frame.name}"
    return None


def describe_error(exc):
    return {
        "error_type": type(exc).__name__,
        "error_message": str(exc)[:MAX_MESSAGE],
        "error_where": _app_location(exc),
    }


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
        error = None
        try:
            await self.app(scope, receive, send_and_record)
            # 4xx is the client's mistake, not a broken code path.
            ok = status["code"] < 500
        except Exception as exc:
            error = describe_error(exc)
            raise
        finally:
            # Runs on success and on an unhandled exception (ok stays False).
            latency_ms = (time.perf_counter() - start) * 1000
            now = time.time()
            for flag, enabled in ctx.flags.items():
                event = {
                    "flag": flag,
                    "path": "new" if enabled else "old",
                    "ok": ok,
                    "latency_ms": round(latency_ms, 2),
                    "ts": now,
                }
                if not ok:
                    event.update(error or {"error_type": f"HTTP {status['code']}",
                                           "error_message": "", "error_where": None})
                self.reporter.report(event)
            _request.reset(token)