"""Tiny shop whose /checkout Release Autopilot releases.

    uvicorn demo_shop.app:app --port 8001 --reload

Reads ONLY demo_shop/.env (server-side Flagsmith key, metrics URL). It never
loads the repo-root .env, so the Organisation API key never enters this process.
"""

import os
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import dotenv_values
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

import autopilot_sdk
from demo_shop.checkout import PRICES, UnknownItem, calculate_total

SHOP_ENV = Path(__file__).with_name(".env")


def shop_config():
    """demo_shop/.env, with real environment variables taking precedence."""
    values = dotenv_values(SHOP_ENV) if SHOP_ENV.exists() else {}
    keys = ("FLAGSMITH_SERVER_KEY", "AUTOPILOT_METRICS_URL", "FLAGSMITH_REFRESH_SECONDS")
    return {k: os.environ.get(k) or values.get(k) or "" for k in keys}


class Item(BaseModel):
    sku: str
    qty: int = Field(ge=1, le=100)


class Cart(BaseModel):
    items: list[Item] = Field(min_length=1)


def create_app(**setup_overrides):
    """Build the shop. Overrides are passed to autopilot_sdk.setup (used by checks)."""
    @asynccontextmanager
    async def lifespan(app):
        yield
        app.state.autopilot.close()

    app = FastAPI(title="Release Autopilot demo shop", lifespan=lifespan)

    config = shop_config()
    setup_args = {
        "server_key": config["FLAGSMITH_SERVER_KEY"],
        "metrics_url": config["AUTOPILOT_METRICS_URL"] or None,
        "refresh_seconds": int(config["FLAGSMITH_REFRESH_SECONDS"] or 10),
    }
    setup_args.update(setup_overrides)
    app.state.autopilot = autopilot_sdk.setup(app, **setup_args)

    @app.get("/health")
    def health():
        return {"ok": True}

    @app.get("/products")
    def products():
        return PRICES

    @app.post("/checkout")
    def checkout(cart: Cart):
        try:
            total = calculate_total([item.model_dump() for item in cart.items])
        except UnknownItem as e:
            raise HTTPException(status_code=400, detail=f"unknown item: {e}")
        return {"total": total}

    return app


# Built lazily, so importing this module (e.g. in tests) needs no key.
def __getattr__(name):
    if name == "app":
        global app
        app = create_app()
        return app
    raise AttributeError(name)
