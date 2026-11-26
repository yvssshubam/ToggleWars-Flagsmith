"""Offline tests for the AI layer. Groq is faked with recorded-shape responses,
including garbage, cut-off answers, invalid citations and HTTP errors."""

import json

import httpx
import pytest

from autopilot import llm
from autopilot.llm import (DIAGNOSIS_SCHEMA, PLAN_SCHEMA, TRIAGE_SCHEMA, GroqClient, LLMError,
                           diagnose, diff_lines, plan_prose, render_diff, triage)

KEY = "gsk_test_not_a_real_key_123"

# The checkout change as GitHub's list-PR-files returns it.
CHECKOUT_PATCH = (
    "@@ -20,4 +20,7 @@ def calculate_total(items):\n"
    "         if item[\"sku\"] not in PRICES:\n"
    "             raise UnknownItem(item[\"sku\"])\n"
    "         total += PRICES[item[\"sku\"]] * item[\"qty\"]\n"
    "-    return round(total, 2)\n"
    "+    # New: 10% off orders of 30.00 or more.\n"
    "+    if total >= 30:\n"
    "+        total *= 0.9\n"
    "+    return round(total, 2)"
)
PR_FILES = [
    {"filename": "demo_shop/checkout.py", "status": "modified", "patch": CHECKOUT_PATCH},
    {"filename": "README.md", "status": "modified", "patch": "@@ -1 +1 @@\n-Old\n+New"},
    {"filename": "logo.png", "status": "added"},                         # binary: no patch
    {"filename": "old.py", "status": "removed", "patch": "@@ -1 +0,0 @@\n-x = 1"},
]
CHANGED = {"demo_shop/checkout.py": ["calculate_total"]}


def groq_reply(content, finish_reason="stop", status=200, headers=None):
    body = {"id": "chatcmpl-x", "object": "chat.completion", "model": "openai/gpt-oss-120b",
            "choices": [{"index": 0, "finish_reason": finish_reason,
                         "message": {"role": "assistant",
                                     "content": content if isinstance(content, str) else json.dumps(content)}}],
            "usage": {"prompt_tokens": 900, "completion_tokens": 200, "total_tokens": 1100}}
    return httpx.Response(status, json=body, headers=headers or {})


class FakeGroq:
    """Replays queued responses (or exceptions) and records every request."""

    def __init__(self, *replies):
        self.replies, self.requests, self.slept = list(replies), [], []

    def handler(self, request):
        self.requests.append(request)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    def client(self):
        return GroqClient(KEY, transport=httpx.MockTransport(self.handler), sleep=self.slept.append)

    def body(self, i=0):
        return json.loads(self.requests[i].content)


GOOD_TRIAGE = {
    "user_facing": True,
    "summary": "Adds a discount to large orders.",
    "functions": [{"file": "demo_shop/checkout.py", "name": "calculate_total"}],
    "flag_name": "ap_checkout_discount",
    "risk": {"tier": "high", "reasons": [
        {"claim": "Changes the price customers pay", "file": "demo_shop/checkout.py", "line": 25},
        {"claim": "Made-up line", "file": "demo_shop/checkout.py", "line": 999},
        {"claim": "Wrong file", "file": "demo_shop/app.py", "line": 25},
    ]},
    "success_metric": {"name": "checkout conversion", "why": "Cheaper large orders should convert better."},
}


# --- diff numbering -------------------------------------------------------------------

def test_diff_lines_numbers_the_new_file_and_skips_removed_lines():
    lines = diff_lines(PR_FILES)
    checkout = [(d.line, d.kind, d.text) for d in lines if d.file == "demo_shop/checkout.py"]
    assert checkout[0] == (20, " ", '        if item["sku"] not in PRICES:')
    assert checkout[3] == (23, "+", "    # New: 10% off orders of 30.00 or more.")
    assert checkout[-1] == (26, "+", "    return round(total, 2)")
    assert [d.line for d in lines if d.file == "README.md"] == [1]
    assert not [d for d in lines if d.file in ("logo.png", "old.py")]


def test_diff_lines_handles_several_hunks_and_no_newline_marker():
    patch = "@@ -1,2 +1,2 @@\n a\n-b\n+B\n\\ No newline at end of file\n@@ -10 +10,2 @@\n x\n+y"
    lines = diff_lines([{"filename": "f.py", "status": "modified", "patch": patch}])
    assert [(d.line, d.kind) for d in lines] == [(1, " "), (2, "+"), (10, " "), (11, "+")]


def test_render_diff_caps_size():
    text, truncated = render_diff(diff_lines(PR_FILES), max_chars=80)
    assert truncated and len(text) <= 80
    text, truncated = render_diff(diff_lines(PR_FILES))
    assert not truncated and "demo_shop/checkout.py:25 +         total *= 0.9" in text


# --- the request we send -----------------------------------------------------------------

def _assert_strict(schema, path="root"):
    """Groq strict mode: every object lists all its properties as required, nothing extra."""
    if schema.get("type") == "object":
        assert schema["additionalProperties"] is False, path
        assert set(schema["required"]) == set(schema["properties"]), path
        for name, sub in schema["properties"].items():
            _assert_strict(sub, f"{path}.{name}")
    if schema.get("type") == "array":
        _assert_strict(schema["items"], f"{path}[]")


@pytest.mark.parametrize("schema", [TRIAGE_SCHEMA, PLAN_SCHEMA, DIAGNOSIS_SCHEMA])
def test_schemas_meet_strict_mode_rules(schema):
    _assert_strict(schema)


def test_request_shape():
    fake = FakeGroq(groq_reply(GOOD_TRIAGE))
    triage(fake.client(), PR_FILES, CHANGED, pr_title="Discount")
    req = fake.requests[0]
    assert str(req.url) == "https://api.groq.com/openai/v1/chat/completions"
    assert req.headers["Authorization"] == f"Bearer {KEY}"
    body = fake.body()
    assert body["model"] == "openai/gpt-oss-120b"
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["strict"] is True
    assert body["response_format"]["json_schema"]["schema"] == TRIAGE_SCHEMA
    assert body["include_reasoning"] is False
    user = body["messages"][1]["content"]
    assert "<diff>" in user and "demo_shop/checkout.py:25 +" in user
    assert "untrusted" in body["messages"][0]["content"]


# --- 1. triage ---------------------------------------------------------------------------------

def test_triage_keeps_only_citations_in_the_diff():
    t = triage(FakeGroq(groq_reply(GOOD_TRIAGE)).client(), PR_FILES, CHANGED)
    assert t.ok and t.user_facing
    assert t.functions == [("demo_shop/checkout.py", "calculate_total")]
    assert t.flag_name == "ap_checkout_discount"
    assert t.tier == "high" and not t.tier_defaulted
    assert [(c.file, c.line) for c in t.reasons] == [("demo_shop/checkout.py", 25)]
    assert t.dropped_reasons == 2
    assert t.success_metric == "checkout conversion"


def test_triage_with_no_valid_reason_defaults_to_high():
    raw = dict(GOOD_TRIAGE, risk={"tier": "low", "reasons": [
        {"claim": "Trust me", "file": "demo_shop/checkout.py", "line": 1}]})
    t = triage(FakeGroq(groq_reply(raw)).client(), PR_FILES, CHANGED)
    assert t.ok and t.tier == "high" and t.tier_defaulted
    assert any("defaults to high" in n for n in t.notes)


def test_triage_low_tier_kept_when_justified():
    raw = dict(GOOD_TRIAGE, risk={"tier": "low", "reasons": [
        {"claim": "Only a comment", "file": "demo_shop/checkout.py", "line": 23}]})
    t = triage(FakeGroq(groq_reply(raw)).client(), PR_FILES, CHANGED)
    assert t.tier == "low" and not t.tier_defaulted


def test_triage_drops_functions_the_pr_did_not_change():
    raw = dict(GOOD_TRIAGE, functions=[
        {"file": "demo_shop/checkout.py", "name": "shipping"},
        {"file": "README.md", "name": "calculate_total"},
        {"file": "demo_shop/checkout.py", "name": "calculate_total"},
        {"file": "demo_shop/checkout.py", "name": "calculate_total"},  # duplicate
    ])
    t = triage(FakeGroq(groq_reply(raw)).client(), PR_FILES, CHANGED)
    assert t.functions == [("demo_shop/checkout.py", "calculate_total")]
    assert sum("Ignored" in n for n in t.notes) == 3


def test_triage_with_no_valid_function():
    raw = dict(GOOD_TRIAGE, functions=[{"file": "demo_shop/checkout.py", "name": "nope"}], flag_name="bad name")
    t = triage(FakeGroq(groq_reply(raw)).client(), PR_FILES, CHANGED)
    assert t.ok and t.functions == [] and t.flag_name == ""


def test_triage_replaces_invalid_flag_name():
    raw = dict(GOOD_TRIAGE, flag_name="Checkout-Discount!")
    t = triage(FakeGroq(groq_reply(raw)).client(), PR_FILES, CHANGED)
    assert t.flag_name == "ap_calculate_total"


def test_triage_cannot_skip_the_flag_for_changed_code():
    raw = dict(GOOD_TRIAGE, user_facing=False)
    t = triage(FakeGroq(groq_reply(raw)).client(), PR_FILES, CHANGED)
    assert t.user_facing is True
    t = triage(FakeGroq(groq_reply(raw)).client(), PR_FILES, {})
    assert t.user_facing is False


@pytest.mark.parametrize("reply", [
    groq_reply("Sure! Here is the triage: tier high"),           # not JSON
    groq_reply('{"user_facing": true, "summ'),                     # cut-off JSON
    groq_reply(GOOD_TRIAGE, finish_reason="length"),               # hit max tokens
    groq_reply("[1, 2, 3]"),                                       # JSON, not an object
    groq_reply({"functions": "calculate_total", "risk": "high"}),  # wrong shape
    httpx.Response(200, text="<html>bad gateway</html>"),           # not a chat response
])
def test_triage_garbage_fails_safe(reply):
    t = triage(FakeGroq(reply).client(), PR_FILES, CHANGED)
    assert t.tier == "high" and t.tier_defaulted
    assert t.functions == []
    assert not t.ok or t.dropped_reasons == 0


def test_triage_http_error_fails_safe_without_leaking_the_key():
    fake = FakeGroq(httpx.Response(401, json={"error": {"message": "Invalid API Key"}}))
    t = triage(fake.client(), PR_FILES, CHANGED)
    assert not t.ok and "401" in t.error and KEY not in t.error
    assert len(fake.requests) == 1  # no retry on 401


# --- retries ------------------------------------------------------------------------------

def test_retries_once_after_server_error():
    fake = FakeGroq(httpx.Response(503, text="busy"), groq_reply(GOOD_TRIAGE))
    assert triage(fake.client(), PR_FILES, CHANGED).ok
    assert len(fake.requests) == 2 and fake.slept == [2]


def test_waits_retry_after_on_rate_limit():
    fake = FakeGroq(httpx.Response(429, text="slow down", headers={"retry-after": "7"}), groq_reply(GOOD_TRIAGE))
    assert triage(fake.client(), PR_FILES, CHANGED).ok
    assert fake.slept == [7.0]


def test_gives_up_on_long_rate_limit():
    fake = FakeGroq(httpx.Response(429, text="daily limit", headers={"retry-after": "3600"}))
    t = triage(fake.client(), PR_FILES, CHANGED)
    assert not t.ok and "429" in t.error and fake.slept == []


def test_network_error_twice_fails_safe():
    fake = FakeGroq(httpx.ConnectError("down"), httpx.ConnectError("down"))
    t = triage(fake.client(), PR_FILES, CHANGED)
    assert not t.ok and "unreachable" in t.error


def test_chat_json_raises_llm_error_directly():
    with pytest.raises(LLMError):
        FakeGroq(groq_reply("nope")).client().chat_json("s", "u", "x", PLAN_SCHEMA)


def test_from_env_requires_key(monkeypatch):
    monkeypatch.setattr(llm, "load_dotenv", lambda: None)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="GROQ_API_KEY"):
        GroqClient.from_env()


# --- 2. plan wording -----------------------------------------------------------------------

def _triaged():
    return triage(FakeGroq(groq_reply(GOOD_TRIAGE)).client(), PR_FILES, CHANGED)


def test_plan_prose_uses_ai_wording_without_numbers():
    fake = FakeGroq(groq_reply({"summary": "Gives big orders a discount in `calculate_total`.",
                                "risk_explanation": "It changes what customers pay.",
                                "metric_explanation": "Conversion shows whether buyers respond."}))
    p = plan_prose(fake.client(), _triaged())
    assert p.from_ai and p.summary.startswith("Gives big orders") and not p.notes
    sent = json.loads(fake.body()["messages"][1]["content"])
    assert sent["risk_tier"] == "high"


def test_plan_prose_replaces_wording_with_numbers():
    fake = FakeGroq(groq_reply({"summary": "Adds 10% off orders over 30.",
                                "risk_explanation": "Soak for 120 seconds.",
                                "metric_explanation": "Uses `ap_v2` conversion."}))  # digits in code are fine
    p = plan_prose(fake.client(), _triaged())
    assert p.summary == "Adds a discount to large orders."
    assert "120" not in p.risk_explanation
    assert p.metric_explanation == "Uses `ap_v2` conversion."
    assert len(p.notes) == 2


def test_plan_prose_falls_back_when_call_fails():
    p = plan_prose(FakeGroq(httpx.Response(500, text="x"), httpx.Response(500, text="x")).client(), _triaged())
    assert not p.from_ai and p.summary == "Adds a discount to large orders."
    assert "checkout conversion" in p.metric_explanation


# --- 3. diagnosis ------------------------------------------------------------------------------

SAMPLES = [{"error_type": "KeyError", "error_message": "'qty'", "error_where": "demo_shop/checkout.py:25 in _calculate_total__new"}]
GOOD_DIAGNOSIS = {
    "likely_cause": "The new branch reads `item['qty']` for carts that don't have it.",
    "evidence": [{"claim": "The failing line is in the new discount branch", "file": "demo_shop/checkout.py", "line": 25},
                 {"claim": "Invented", "file": "demo_shop/checkout.py", "line": 400}],
    "suggested_fix": {"description": "Guard the discount.", "file": "demo_shop/checkout.py",
                      "start_line": 24, "end_line": 25, "code": "    if total >= 30:\n        total *= 0.9\n"},
    "confidence": "high",
}


def run_diagnose(*replies, samples=SAMPLES):
    fake = FakeGroq(*replies)
    d = diagnose(fake.client(), PR_FILES, "error rate vs old", "new 30.0% errors (n=40), old 0.0% (n=400)",
                 samples, "calculate_total", "def calculate_total(items): ...", "def calculate_total(items): ...")
    return d, fake


def test_diagnosis_validated():
    d, fake = run_diagnose(groq_reply(GOOD_DIAGNOSIS))
    assert d.available and d.confidence == "high"
    assert [(c.file, c.line) for c in d.evidence] == [("demo_shop/checkout.py", 25)] and d.dropped_evidence == 1
    assert (d.fix_file, d.fix_start, d.fix_end) == ("demo_shop/checkout.py", 24, 25)
    assert d.fix_code.endswith("total *= 0.9")
    user = fake.body()["messages"][1]["content"]
    assert "KeyError" in user and "error rate vs old" in user


def test_diagnosis_unavailable_when_nothing_cites_the_diff():
    raw = dict(GOOD_DIAGNOSIS, evidence=[{"claim": "x", "file": "nope.py", "line": 1}])
    d, _ = run_diagnose(groq_reply(raw))
    assert not d.available and "no evidence" in d.error


def test_diagnosis_unavailable_when_it_quotes_numbers():
    d, _ = run_diagnose(groq_reply(dict(GOOD_DIAGNOSIS, likely_cause="30% of requests fail.")))
    assert not d.available


def test_diagnosis_drops_evidence_quoting_numbers():
    raw = dict(GOOD_DIAGNOSIS, evidence=GOOD_DIAGNOSIS["evidence"] + [
        {"claim": "Fails 30% of the time", "file": "demo_shop/checkout.py", "line": 24}])
    d, _ = run_diagnose(groq_reply(raw))
    assert d.available and len(d.evidence) == 1 and d.dropped_evidence == 2


def test_diagnosis_fix_outside_diff_becomes_snippet():
    raw = dict(GOOD_DIAGNOSIS, suggested_fix=dict(GOOD_DIAGNOSIS["suggested_fix"], start_line=1, end_line=2))
    d, _ = run_diagnose(groq_reply(raw))
    assert d.available and d.fix_file is None and d.fix_code
    assert any("snippet" in n for n in d.notes)


def test_diagnosis_with_null_fix_location():
    raw = dict(GOOD_DIAGNOSIS, suggested_fix={"description": "Revert it.", "file": None,
                                              "start_line": None, "end_line": None, "code": None})
    d, _ = run_diagnose(groq_reply(raw))
    assert d.available and d.fix_description == "Revert it." and d.fix_code is None


def test_diagnosis_confidence_low_without_app_errors():
    d, fake = run_diagnose(groq_reply(GOOD_DIAGNOSIS), samples=[])
    assert d.available and d.confidence == "low"
    assert "none (no exceptions recorded)" in fake.body()["messages"][1]["content"]


def test_diagnosis_sends_at_most_20_samples_and_no_extra_fields():
    many = [dict(SAMPLES[0], user_id="u1", body="secret") for _ in range(50)]
    _, fake = run_diagnose(groq_reply(GOOD_DIAGNOSIS), samples=many)
    user = fake.body()["messages"][1]["content"]
    assert user.count("KeyError") == 20 and "secret" not in user and "u1" not in user


@pytest.mark.parametrize("reply", [groq_reply("garbage"), groq_reply({"likely_cause": ["x"]}),
                                   httpx.Response(400, text="bad request")])
def test_diagnosis_failures_are_unavailable(reply):
    d, _ = run_diagnose(reply)
    assert not d.available and d.error