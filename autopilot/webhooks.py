"""GitHub webhook checks and parsing (no network, no side effects).

Every webhook is signed with the shared secret (X-Hub-Signature-256). An
unsigned or wrongly signed request is rejected before anything is parsed.
"""

import hashlib
import hmac

from autopilot.github_client import BOT_MARKER

PR_ACTIONS = ("opened", "reopened", "synchronize", "closed")


def verify_signature(secret, body, header):
    """True only if `header` is the HMAC-SHA256 of the raw body with `secret`."""
    if not secret or not header or not header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)


def parse_event(event_type, payload):
    """Turn a webhook into the small dict the bot acts on, or None to ignore it."""
    if event_type == "pull_request" and payload.get("action") in PR_ACTIONS:
        pr = payload["pull_request"]
        return {
            "kind": "pr",
            "action": payload["action"],
            "number": pr["number"],
            "title": pr.get("title", ""),
            "author": pr["user"]["login"],
            "head_ref": pr["head"]["ref"],
            "head_sha": pr["head"]["sha"],
            "base_ref": pr["base"]["ref"],
            "merged": bool(pr.get("merged")),
        }
    if event_type == "issue_comment" and payload.get("action") == "created":
        issue, comment = payload["issue"], payload["comment"]
        if "pull_request" not in issue:
            return None  # a comment on a plain issue, not a PR
        body = comment.get("body") or ""
        if BOT_MARKER in body:
            return None  # the bot's own comment
        return {
            "kind": "comment",
            "number": issue["number"],
            "author": comment["user"]["login"],
            "body": body,
            "comment_id": comment["id"],
        }
    return None