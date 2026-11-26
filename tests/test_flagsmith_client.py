"""Offline tests: check flagsmith_client sends exactly the requests that
rollout_check.py proved work. A fake transport records each request, so no
network or token is needed."""

import json

import httpx
import pytest

from autopilot.flagsmith_client import FlagsmithClient, FlagsmithError, percentage_split_rules

ENV_KEY = "JM8wUtydhVkCAb9SNF2zE9"


def make_client(handler):
    requests = []

    def record(request):
        requests.append(request)
        return handler(request)

    fs = FlagsmithClient("secret", 56321, ENV_KEY, transport=httpx.MockTransport(record))
    return fs, requests


def body(request):
    return json.loads(request.content)


def test_percentage_rules_use_null_property():
    rules = percentage_split_rules(5)
    condition = rules[0]["rules"][0]["conditions"][0]
    assert condition == {"operator": "PERCENTAGE_SPLIT", "property": None, "value": "5"}
    assert rules[0]["type"] == "ALL" and rules[0]["rules"][0]["type"] == "ANY"


@pytest.mark.parametrize("bad", [-1, 101])
def test_percentage_out_of_range_rejected(bad):
    with pytest.raises(ValueError):
        percentage_split_rules(bad)


def test_token_scheme_still_supported():
    fs = FlagsmithClient("secret", 1, "k", auth_scheme="Token",
                         transport=httpx.MockTransport(lambda r: httpx.Response(200, json=[])))
    fs.list_projects()  # no error


def test_bad_auth_scheme_rejected():
    with pytest.raises(ValueError):
        FlagsmithClient("t", 1, "k", auth_scheme="Bearer")


def test_auth_headers():
    fs, reqs = make_client(lambda r: httpx.Response(200, json=[] if "projects" in str(r.url) else {}))
    fs.list_projects()
    fs.get_identity_flags("u1")
    assert reqs[0].headers["Authorization"] == "Api-Key secret"
    assert reqs[1].headers["X-Environment-Key"] == ENV_KEY
    assert "Authorization" not in reqs[1].headers  # admin token never sent to edge


def test_create_and_widen_segment_in_place():
    fs, reqs = make_client(lambda r: httpx.Response(201 if r.method == "POST" else 200, json={"id": 7}))
    assert fs.create_percentage_segment("ap_x_rollout", 5) == 7
    fs.set_segment_percentage(7, "ap_x_rollout", 25)

    post, put = reqs
    assert post.method == "POST"
    assert str(post.url) == "https://api.flagsmith.com/api/v1/projects/56321/segments/"
    assert body(post) == {"name": "ap_x_rollout", "project": 56321, "rules": percentage_split_rules(5)}
    assert put.method == "PUT"
    assert str(put.url) == "https://api.flagsmith.com/api/v1/projects/56321/segments/7/"
    assert body(put)["rules"] == percentage_split_rules(25)


def test_flag_updates_use_future_endpoint():
    fs, reqs = make_client(lambda r: httpx.Response(200, json={}))
    fs.enable_for_segment_only(304593, 7)
    fs.set_flag_for_everyone(304593, True)

    url = f"https://api.flagsmith.com/api/__future__/environments/{ENV_KEY}/features/304593/"
    assert all(r.method == "PATCH" and str(r.url) == url for r in reqs)
    assert body(reqs[0]) == {
        "environment_default": {"enabled": False},
        "segment_overrides": [{"segment": {"id": 7}, "enabled": True}],
    }
    assert body(reqs[1]) == {"environment_default": {"enabled": True}}


def test_deletes_accept_204():
    fs, reqs = make_client(lambda r: httpx.Response(204))
    fs.remove_segment_override(304593, 7)
    fs.delete_segment(7)
    assert str(reqs[0].url) == (
        f"https://api.flagsmith.com/api/__future__/environments/{ENV_KEY}"
        "/features/304593/segment-overrides/7/"
    )
    assert str(reqs[1].url) == "https://api.flagsmith.com/api/v1/projects/56321/segments/7/"


def test_error_raises_with_status_and_body():
    fs, _ = make_client(lambda r: httpx.Response(400, text='{"property": ["bad"]}'))
    with pytest.raises(FlagsmithError) as e:
        fs.create_percentage_segment("x", 5)
    assert e.value.status_code == 400 and "property" in e.value.body


def test_network_error_becomes_flagsmith_error():
    def handler(r):
        raise httpx.ConnectTimeout("timed out", request=r)

    fs, _ = make_client(handler)
    with pytest.raises(FlagsmithError) as e:
        fs.set_flag_for_everyone(304593, False)
    assert e.value.status_code is None and "ConnectTimeout" in e.value.body


def test_list_features_follows_pagination():
    page2 = "https://api.flagsmith.com/api/v1/projects/56321/features/?page=2"

    def handler(r):
        if str(r.url) == page2:
            return httpx.Response(200, json={"results": [{"id": 2, "name": "b"}], "next": None})
        return httpx.Response(200, json={"results": [{"id": 1, "name": "a"}], "next": page2})

    fs, _ = make_client(handler)
    assert [f["id"] for f in fs.list_features()] == [1, 2]
    assert fs.get_feature_by_name("b")["id"] == 2
    assert fs.get_feature_by_name("missing") is None


def test_identity_flags_parsed():
    payload = {"flags": [
        {"feature": {"name": "show_demo_button"}, "enabled": True},
        {"feature": {"name": "other"}, "enabled": False},
    ]}
    fs, reqs = make_client(lambda r: httpx.Response(200, json=payload))
    assert fs.get_identity_flags("user-1") == {"show_demo_button": True, "other": False}
    assert reqs[0].url.params["identifier"] == "user-1"


def test_from_env_reports_missing_vars(monkeypatch):
    # Stop it loading your real .env, which would make the vars present.
    monkeypatch.setattr("autopilot.flagsmith_client.load_dotenv", lambda: None)
    for name in ("FS_TOKEN", "FS_PROJECT_ID", "FS_ENV_KEY"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(RuntimeError, match="FS_TOKEN"):
        FlagsmithClient.from_env()


def test_internal_or_split_rule():
    rules = percentage_split_rules(0, include_internal=True)
    any_rule = rules[0]["rules"][0]
    assert any_rule["type"] == "ANY"
    assert any_rule["conditions"] == [
        {"operator": "EQUAL", "property": "is_internal", "value": "true"},
        {"operator": "PERCENTAGE_SPLIT", "property": None, "value": "0"},
    ]


def test_segment_calls_pass_internal_rule_through():
    fs, reqs = make_client(lambda r: httpx.Response(201 if r.method == "POST" else 200, json={"id": 7}))
    fs.create_percentage_segment("s", 0, include_internal=True)
    fs.set_segment_percentage(7, "s", 25, include_internal=True)
    assert body(reqs[0])["rules"] == percentage_split_rules(0, include_internal=True)
    assert body(reqs[1])["rules"] == percentage_split_rules(25, include_internal=True)


def test_create_and_delete_feature():
    fs, reqs = make_client(lambda r: httpx.Response(201 if r.method == "POST" else 204, json={"id": 42}))
    assert fs.create_feature("ap_x", "why") == 42
    fs.delete_feature(42)
    assert str(reqs[0].url) == "https://api.flagsmith.com/api/v1/projects/56321/features/"
    assert body(reqs[0]) == {"name": "ap_x", "description": "why", "default_enabled": False}
    assert reqs[1].method == "DELETE"
    assert str(reqs[1].url) == "https://api.flagsmith.com/api/v1/projects/56321/features/42/"