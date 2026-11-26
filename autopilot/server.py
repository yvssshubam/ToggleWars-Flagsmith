"""The bot's HTTP side. For now: metrics ingest from the shop and a status
endpoint. GitHub webhooks and the dashboard are added here from Friday."""

from fastapi import FastAPI, Request


def create_app(store):
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

    @app.get("/releases")
    def releases():
        return [{k: r[k] for k in ("id", "flag", "state", "approved_by", "rollback_reason", "created_at")}
                for r in store.all_releases()]

    @app.get("/health")
    def health():
        return {"ok": True}

    return app