"""The bot's HTTP side: metrics ingest from the shop, GitHub webhooks, a
status endpoint, and the dashboard.

The dashboard is local-only. Port 8000 is also reachable from the internet
through ngrok (for GitHub's webhooks), and a page with an Approve button must
not be: requests that came through a proxy (X-Forwarded-For) or name any host
but localhost get a 404. The two buttons also need a token that is made fresh
each run and only appears inside the page, so another website open in the
same browser can't press them.
"""

import json
import secrets
import time
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from autopilot import dashboard
from autopilot.webhooks import parse_event, verify_signature

PAGE = Path(__file__).with_name("dashboard.html")
LOCAL_HOSTS = ("127.0.0.1", "localhost", "[::1]")


def is_local(request):
    """True only for a browser on this machine talking to the bot directly."""
    if request.headers.get("x-forwarded-for") or request.headers.get("forwarded"):
        return False  # came through ngrok (or another proxy)
    host = request.headers.get("host", "")
    host = host.rsplit(":", 1)[0] if not host.endswith("]") else host
    return host in LOCAL_HOSTS


def create_app(store, github_secret=None, github_repo=None, log=print, engine=None,
               dashboard_token=None, clock=time.time):
    app = FastAPI(title="Release Autopilot")
    token = dashboard_token or secrets.token_urlsafe(24)
    not_found = JSONResponse({"detail": "Not Found"}, status_code=404)

    @app.post("/metrics")
    async def metrics(request: Request):
        try:
            body = await request.json()
            events = body.get("events", []) if isinstance(body, dict) else []
        except ValueError:
            events = []
        stored = store.add_metrics(events if isinstance(events, list) else [])
        return {"stored": stored}

    @app.post("/webhooks/github")
    async def github_webhook(request: Request):
        if not github_secret or not github_repo:
            return JSONResponse({"error": "GitHub is not configured on this bot"}, status_code=503)
        body = await request.body()
        if not verify_signature(github_secret, body, request.headers.get("X-Hub-Signature-256")):
            log("[github] rejected a webhook with a missing or wrong signature")
            return JSONResponse({"error": "bad signature"}, status_code=401)

        event_type = request.headers.get("X-GitHub-Event", "")
        try:
            payload = json.loads(body)
        except ValueError:
            return JSONResponse({"error": "body is not JSON (set content type to application/json)"},
                                status_code=400)
        if event_type == "ping":
            log("[github] webhook connected: GitHub ping received and signature verified")
            return {"pong": True}
        repo = (payload.get("repository") or {}).get("full_name", "")
        if repo.lower() != github_repo.lower():
            return JSONResponse({"ignored": f"not {github_repo}"}, status_code=202)

        event = parse_event(event_type, payload)
        if event is None:
            return JSONResponse({"ignored": True}, status_code=202)
        queued = store.add_github_event(request.headers.get("X-GitHub-Delivery"), event)
        return JSONResponse({"queued": queued}, status_code=202)

    @app.get("/releases")
    def releases():
        return [{k: r[k] for k in ("id", "flag", "state", "approved_by", "rollback_reason", "created_at")}
                for r in store.all_releases()]

    @app.get("/health")
    def health():
        return {"ok": True}

    # --- dashboard (local only) ------------------------------------------------

    @app.get("/")
    @app.get("/dashboard")
    def dashboard_page(request: Request):
        if not is_local(request):
            return not_found
        html = PAGE.read_text(encoding="utf-8")
        html = html.replace("__DASHBOARD_TOKEN__", token).replace("__REPO__", github_repo or "")
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})

    @app.get("/api/releases")
    def api_releases(request: Request):
        if not is_local(request):
            return not_found
        return dashboard.list_releases(store, clock, github_repo)

    @app.get("/api/releases/{release_id}")
    def api_release(release_id: int, request: Request):
        if not is_local(request):
            return not_found
        release = store.get_release(release_id)
        if not release:
            return JSONResponse({"error": "no such release"}, status_code=404)
        return dashboard.detail(store, release, clock, github_repo)

    @app.post("/api/releases/{release_id}/{action}")
    def api_action(release_id: int, action: str, request: Request):
        if not is_local(request):
            return not_found
        if not secrets.compare_digest(request.headers.get("x-dashboard-token", ""), token):
            return JSONResponse({"error": "missing or wrong dashboard token; reload the page"}, status_code=403)
        if action not in ("approve", "switch-off"):
            return JSONResponse({"error": f"unknown action {action}"}, status_code=404)
        if engine is None:
            return JSONResponse({"error": "the release engine isn't running"}, status_code=503)
        release = store.get_release(release_id)
        if not release:
            return JSONResponse({"error": "no such release"}, status_code=404)
        latest = store.latest_release(release["flag"])
        if latest["id"] != release["id"]:
            return JSONResponse({"error": "a newer release of this flag exists"}, status_code=409)
        if action == "approve":
            result = engine.approve(release["flag"], by="dashboard")
            ok = result.startswith("approved by")
            message = "Approved: the new code goes on for everyone within a few seconds." if ok else result
        else:
            result = engine.request_rollback(release["flag"], by="the dashboard")
            ok = result.startswith("rollback requested")
            message = "Switch-off requested: the flag goes off for everyone within a few seconds." if ok else result
        log(f"[dashboard] {action} {release['flag']}: {result}")
        return JSONResponse({"ok": ok, "message": message}, status_code=200 if ok else 409)

    return app