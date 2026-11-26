"""Library an app imports to be released by Release Autopilot.

    import autopilot_sdk
    autopilot = autopilot_sdk.setup(app, server_key="ser.…", metrics_url="http://…/metrics")

    if autopilot_sdk.flag_on("ap_checkout_v2"):
        ...new code...

Requests identify the user with `X-User-Id` and internal staff with
`X-Internal: true` (trait is_internal). Flags are evaluated by the Flagsmith
SDK in local evaluation mode, so flag checks make no billable API calls; the
SDK polls the environment document every `refresh_seconds` instead.

This library only ever receives a server-side environment key. It never sees
the Organisation API key the bot uses.
"""

from flagsmith import Flagsmith

from .flags import clear_client, flag_on, set_client
from .middleware import AutopilotMiddleware
from .reporter import Reporter

__all__ = ["flag_on", "setup", "Autopilot", "Reporter"]


class Autopilot:
    def __init__(self, client, reporter):
        self.client = client
        self.reporter = reporter

    def close(self):
        self.reporter.close()
        if hasattr(self.client, "close"):
            self.client.close()
        clear_client(self.client)


def setup(app, server_key=None, metrics_url=None, refresh_seconds=10, sink=None, client=None):
    """Wire autopilot into a FastAPI/Starlette app. Call once, before startup.

    `client` lets tests pass a fake SDK instead of a server key.
    """
    if client is None:
        if not server_key or not server_key.startswith("ser."):
            raise ValueError(
                "autopilot_sdk needs a Flagsmith server-side environment key (starts with "
                "'ser.'): Environment settings -> SDK keys."
            )
        client = Flagsmith(
            environment_key=server_key,
            enable_local_evaluation=True,
            environment_refresh_interval_seconds=refresh_seconds,
        )
    set_client(client)
    reporter = Reporter(url=metrics_url, sink=sink)
    app.add_middleware(AutopilotMiddleware, reporter=reporter)
    return Autopilot(client, reporter)