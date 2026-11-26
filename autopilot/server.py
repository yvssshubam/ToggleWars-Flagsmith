"""The bot's HTTP side: metrics ingest from the shop, GitHub webhooks, and a
status endpoint. The dashboard is added here on Saturday."""

import json

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from autopilot.webhooks import parse_event, verify_signature


def create_app(store, github_secret=None, github_repo=None, log=print):
    app = FastAPI(title="Release Autopilot")

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

    return app