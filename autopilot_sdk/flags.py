"""Per-request user context and flag_on().

The middleware puts a RequestContext in a contextvar at the start of each
request. flag_on() reads it, asks the Flagsmith SDK (local evaluation, so no
network call), and records the answer so the middleware can report which code
path this request took.
"""

import contextvars
import logging
from dataclasses import dataclass, field

log = logging.getLogger("autopilot_sdk")

INTERNAL_TRAIT = "is_internal"

_client = None  # Flagsmith SDK instance, set by setup()
_request = contextvars.ContextVar("autopilot_request", default=None)


@dataclass
class RequestContext:
    user_id: str | None
    internal: bool
    # flag name -> True (new path) / False (old path), filled in by flag_on().
    # A mutable dict, so answers recorded in a threadpool (sync endpoints)
    # are still visible to the middleware afterwards.
    flags: dict = field(default_factory=dict)
    # A business event this request stands for ("checkout", "order"), set by track().
    event: str | None = None


def set_client(client):
    global _client
    _client = client


def clear_client(client):
    """Clear the client only if it's still `client`, so an app shutting down
    can't remove a newer app's client."""
    global _client
    if _client is client:
        _client = None


def track(event):
    """Tag the current request as a business event, e.g. track("order").

    The middleware adds it to the report for every flag this request asked
    about, so the bot can compare conversion between the new and old paths.
    Does nothing outside a request."""
    ctx = _request.get()
    if ctx is not None:
        ctx.event = str(event)


def flag_on(name):
    """True if the current user should get the new code path for `name`.

    Fails safe: any doubt (no request, no user id, SDK not ready, flag missing,
    any error) means False, i.e. the old, known-good path.
    """
    ctx = _request.get()
    if ctx is None:
        return False
    if name in ctx.flags:  # one answer per flag per request
        return ctx.flags[name]

    enabled = False
    if not ctx.user_id:
        log.debug("no X-User-Id header; using old path for %s", name)
    elif _client is None:
        log.warning("autopilot_sdk.setup() not called; using old path for %s", name)
    elif getattr(_client, "_local_evaluation", True) is None:
        # The environment document hasn't loaded. The SDK would silently fall
        # back to one billable API call per lookup, so refuse instead.
        # (_local_evaluation is private; checked against flagsmith==6.7.0.)
        log.warning("Flagsmith environment not loaded yet; using old path for %s", name)
    else:
        try:
            flags = _client.get_identity_flags(ctx.user_id, traits={INTERNAL_TRAIT: ctx.internal})
            enabled = bool(flags.is_feature_enabled(name))
        except Exception:  # missing flag, SDK error: never break the request
            log.exception("flag lookup failed for %s; using old path", name)
            enabled = False

    ctx.flags[name] = enabled
    return enabled