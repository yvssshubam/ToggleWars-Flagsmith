"""Every call the bot makes to Flagsmith's Admin API goes through this module.

The `__future__` update endpoints are experimental and may change without
notice, so keeping all paths here means a Flagsmith API change is a one-line
fix. Documented fallback if they break: the versioning endpoints
(POST /environments/{env}/features/{feature}/versions/, then /publish/).

Every call below is ported from rollout_check.py, which passed against the
real account on 25 Nov 2026. Calls that haven't been verified yet (creating a
flag, trait conditions) are added in Thursday step 2, after testing them.
"""

import os

import httpx
from dotenv import load_dotenv

API_URL = "https://api.flagsmith.com/api/v1"
FUTURE_URL = "https://api.flagsmith.com/api/__future__"
EDGE_URL = "https://edge.api.flagsmith.com/api/v1"
TIMEOUT_SECONDS = 30
AUTH_SCHEMES = ("Token", "Api-Key")


class FlagsmithError(Exception):
    """A Flagsmith call returned an unexpected status.

    The engine catches this and pauses the release rather than guessing.
    status_code is None when no response arrived (timeout, network error).
    """

    def __init__(self, action, status_code, body):
        self.action = action
        self.status_code = status_code
        self.body = body
        status = f"HTTP {status_code}" if status_code is not None else "no response"
        super().__init__(f"{action} failed: {status}: {body[:500]}")


def percentage_split_rules(percent):
    """Segment rules matching `percent`% of identities.

    "property" must be null: the API rejects an empty string with HTTP 400.
    """
    if not 0 <= percent <= 100:
        raise ValueError(f"percent must be between 0 and 100, got {percent}")
    condition = {"operator": "PERCENTAGE_SPLIT", "property": None, "value": str(percent)}
    return [
        {
            "type": "ALL",
            "conditions": [],
            "rules": [{"type": "ANY", "rules": [], "conditions": [condition]}],
        }
    ]


class FlagsmithClient:
    def __init__(self, token, project_id, env_key, auth_scheme="Api-Key", transport=None):
        if auth_scheme not in AUTH_SCHEMES:
            raise ValueError(f"auth_scheme must be one of {AUTH_SCHEMES}, got {auth_scheme!r}")
        self.project_id = int(project_id)
        self.env_key = env_key
        # `transport` lets tests substitute a fake server; leave it None for real use.
        self._admin = httpx.Client(
            headers={"Authorization": f"{auth_scheme} {token}"},
            timeout=TIMEOUT_SECONDS,
            transport=transport,
        )
        self._edge = httpx.Client(
            headers={"X-Environment-Key": env_key},
            timeout=TIMEOUT_SECONDS,
            transport=transport,
        )

    @classmethod
    def from_env(cls):
        """Build a client from .env / environment variables."""
        load_dotenv()
        missing = [n for n in ("FS_TOKEN", "FS_PROJECT_ID", "FS_ENV_KEY") if not os.environ.get(n)]
        if missing:
            raise RuntimeError(
                f"Missing {', '.join(missing)}. Copy .env.example to .env and fill it in."
            )
        return cls(
            token=os.environ["FS_TOKEN"],
            project_id=os.environ["FS_PROJECT_ID"],
            env_key=os.environ["FS_ENV_KEY"],
            auth_scheme=os.environ.get("FS_AUTH_SCHEME", "Api-Key"),
        )

    def close(self):
        self._admin.close()
        self._edge.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    @staticmethod
    def _request(client, method, url, action, ok=(200,), **kwargs):
        """Send one request. Any failure, including a timeout or a network
        error, raises FlagsmithError, so callers have one thing to catch."""
        try:
            response = client.request(method, url, **kwargs)
        except httpx.HTTPError as e:
            raise FlagsmithError(action, None, f"{type(e).__name__}: {e}") from e
        if response.status_code not in ok:
            raise FlagsmithError(action, response.status_code, response.text)
        return response

    def _get_all(self, url, action, params=None):
        """GET a list endpoint, following pagination if the response is paged."""
        data = self._request(self._admin, "GET", url, action, params=params).json()
        if isinstance(data, list):
            return data
        items = list(data.get("results", []))
        while data.get("next"):
            data = self._request(self._admin, "GET", data["next"], action).json()
            items.extend(data.get("results", []))
        return items

    # --- Reads -------------------------------------------------------------

    def list_projects(self):
        return self._get_all(f"{API_URL}/projects/", "list projects")

    def list_environments(self):
        return self._get_all(
            f"{API_URL}/environments/",
            "list environments",
            params={"project": self.project_id},
        )

    def list_features(self):
        """Project features; each includes `last_modified_in_any_environment`."""
        return self._get_all(f"{API_URL}/projects/{self.project_id}/features/", "list features")

    def get_feature_by_name(self, name):
        """The feature dict with this name, or None."""
        for feature in self.list_features():
            if feature["name"] == name:
                return feature
        return None

    def get_identity_flags(self, identifier):
        """What one user sees, read from the edge API: {flag_name: enabled}."""
        r = self._request(
            self._edge, "GET", f"{EDGE_URL}/identities/", f"read flags for {identifier}",
            params={"identifier": identifier},
        )
        return {f["feature"]["name"]: f["enabled"] for f in r.json().get("flags", [])}

    # --- Flag state (versioning-v2-safe update API) ------------------------

    def _feature_url(self, feature_id):
        return f"{FUTURE_URL}/environments/{self.env_key}/features/{feature_id}/"

    def set_flag_for_everyone(self, feature_id, enabled):
        """Turn a flag on or off as the environment default."""
        self._request(
            self._admin, "PATCH", self._feature_url(feature_id),
            f"set feature {feature_id} {'on' if enabled else 'off'} for everyone",
            json={"environment_default": {"enabled": bool(enabled)}},
        )

    def enable_for_segment_only(self, feature_id, segment_id):
        """Flag off for everyone, on for one segment."""
        self._request(
            self._admin, "PATCH", self._feature_url(feature_id),
            f"enable feature {feature_id} for segment {segment_id} only",
            json={
                "environment_default": {"enabled": False},
                "segment_overrides": [{"segment": {"id": segment_id}, "enabled": True}],
            },
        )

    def remove_segment_override(self, feature_id, segment_id):
        self._request(
            self._admin, "DELETE", f"{self._feature_url(feature_id)}segment-overrides/{segment_id}/",
            f"remove segment {segment_id} override from feature {feature_id}",
            ok=(200, 204),
        )

    # --- Segments ----------------------------------------------------------

    def _segments_url(self):
        return f"{API_URL}/projects/{self.project_id}/segments/"

    def _segment_body(self, name, percent):
        return {"name": name, "project": self.project_id, "rules": percentage_split_rules(percent)}

    def create_percentage_segment(self, name, percent):
        """Create a % split segment and return its id."""
        r = self._request(
            self._admin, "POST", self._segments_url(), f"create segment {name} at {percent}%",
            ok=(200, 201), json=self._segment_body(name, percent),
        )
        return r.json()["id"]

    def set_segment_percentage(self, segment_id, name, percent):
        """Widen (or narrow) a split IN PLACE.

        Never replace the segment with a new one: buckets are only sticky
        within one segment, so a new segment reshuffles users and some lose
        the feature mid-rollout.
        """
        self._request(
            self._admin, "PUT", f"{self._segments_url()}{segment_id}/",
            f"set segment {segment_id} to {percent}%",
            json=self._segment_body(name, percent),
        )

    def delete_segment(self, segment_id):
        self._request(
            self._admin, "DELETE", f"{self._segments_url()}{segment_id}/",
            f"delete segment {segment_id}", ok=(200, 204),
        )