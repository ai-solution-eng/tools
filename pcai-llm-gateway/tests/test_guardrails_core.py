"""Guardrails core engine tests — pure engines, fake DB, mock webhooks.

No live Postgres: guardrails_for_model runs against a monkeypatched
pcai_llm.core.guardrails.db (the repo's fake-db pattern, emulating the
query's WHERE enabled / ORDER BY name server-side); webhook engines run
against httpx2.MockTransport clients. The reference pipeline's known bugs
(first-hit-only masking, phone-matching inside longer digit runs, non-Luhn
card flags, multi-part webhook mask overwrite) each get a regression test.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx2
import pytest

from pcai_llm.config import settings
from pcai_llm.core import guardrails as gr
from pcai_llm.core.guardrails import (
    GUARDRAIL_ENGINES,
    GUARDRAIL_MODES,
    MASK,
    GuardrailBlocked,
    GuardrailEvent,
    GuardrailSpec,
    _luhn_ok,
    concat_user_text,
    guardrails_for_model,
    rewrite_user_texts,
    run_post_call,
    run_pre_call,
)

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _spec(
    engine: str,
    config: dict[str, Any] | None = None,
    mode: str = "pre_call",
    name: str | None = None,
) -> GuardrailSpec:
    return GuardrailSpec(
        id=uuid.uuid4(),
        name=name or f"{engine}-{mode}",
        mode=mode,
        engine=engine,
        config=config or {},
        enabled=True,
    )


def _client(handler: Any) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(transport=httpx2.MockTransport(handler))


def _never(request: httpx2.Request) -> httpx2.Response:
    raise AssertionError(f"unexpected HTTP call to {request.url}")


def _json_handler(payload: dict[str, Any] | list[int]):
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json=payload)

    return handler


def _recording_handler(seen: dict[str, Any]):
    def handler(request: httpx2.Request) -> httpx2.Response:
        seen["authorization"] = request.headers.get("authorization")
        seen["content_type"] = request.headers.get("content-type")
        seen["payload"] = json.loads(request.content)
        return httpx2.Response(200, json={"verdict": "pass"})

    return handler


def _connect_error_handler(request: httpx2.Request) -> httpx2.Response:
    raise httpx2.ConnectError("connection refused", request=request)


class RecordingClient:
    """Duck-typed stand-in for httpx2.AsyncClient: records post() kwargs and
    answers with a canned verdict (the engine only ever calls http.post)."""

    def __init__(self, payload: dict[str, Any] | None = None) -> None:
        self.payload = payload or {"verdict": "pass"}
        self.calls: list[dict[str, Any]] = []

    async def post(self, url: str, json: Any = None, headers: Any = None, timeout: Any = None):
        self.calls.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        return httpx2.Response(200, json=self.payload)


def _row(
    name: str,
    engine: str,
    config_json: str | None,
    enabled: bool = True,
    mode: str = "pre_call",
    gid: Any = None,
) -> dict[str, Any]:
    return {
        "id": gid if gid is not None else uuid.uuid4(),
        "name": name,
        "mode": mode,
        "engine": engine,
        "config_json": config_json,
        "enabled": enabled,
    }


class FakeDB:
    """Emulates the guardrails_for_model query server-side: enabled-only
    rows (the SQL's WHERE g.enabled), sorted by name (ORDER BY g.name), and
    records the (sql, args) it was called with."""

    def __init__(self, rows: list[dict[str, Any]] | None = None, error: Exception | None = None) -> None:
        self.rows = rows or []
        self.error = error
        self.calls: list[tuple[str, tuple]] = []

    async def fetch(self, sql: str, *args: Any):
        self.calls.append((sql, args))
        if self.error is not None:
            raise self.error
        return sorted((dict(r) for r in self.rows if r.get("enabled")), key=lambda r: r["name"])

    async def fetchrow(self, sql: str, *args: Any):
        raise AssertionError("guardrails_for_model must only fetch")

    async def execute(self, sql: str, *args: Any):
        raise AssertionError("guardrails_for_model must only fetch")


# ---------------------------------------------------------------------------
# public surface / pure helpers
# ---------------------------------------------------------------------------


def test_public_constants() -> None:
    assert GUARDRAIL_MODES == ("pre_call", "post_call")
    assert GUARDRAIL_ENGINES == ("blocklist", "regex_pii", "length", "webhook")
    assert MASK == "[REDACTED]"


def test_luhn_ok() -> None:
    assert _luhn_ok("4111111111111111")
    assert not _luhn_ok("4111111111111112")
    assert not _luhn_ok("12345")  # wrong checksum
    assert not _luhn_ok("")
    assert not _luhn_ok("4111-1111-1111-1111")  # bare digits only


def test_concat_user_text() -> None:
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": "two"},
        {"role": "user", "content": [{"type": "text", "text": "three"}, {"type": "text", "text": "four"}]},
        {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "http://x/i.png"}}]},
        {"role": "user"},
    ]
    assert concat_user_text(messages) == "one\nthree\nfour"


def test_rewrite_user_texts_preserves_structure() -> None:
    image_part = {"type": "image_url", "image_url": {"url": "http://x/i.png"}}
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": "sys"},
        {"role": "user", "name": "alice", "content": [{"type": "text", "text": "hi"}, image_part]},
        {"role": "user", "content": "plain"},
        {"role": "user", "content": None},
    ]
    out = rewrite_user_texts(messages, str.upper)
    # structure: other message keys survive, non-text parts keep identity,
    # text parts rewritten, empty/None content untouched
    assert out[0] is messages[0]
    assert out[1]["name"] == "alice"
    assert out[1]["content"][0] == {"type": "text", "text": "HI"}
    assert out[1]["content"][1] is image_part
    assert out[2]["content"] == "PLAIN"
    assert out[3]["content"] is None
    # the input is never mutated
    assert messages[2]["content"] == "plain"


async def test_pre_call_ignores_post_call_guards() -> None:
    guards = [_spec("blocklist", {"keywords": ["x"]}, mode="post_call")]
    out, events = await run_pre_call(guards, [{"role": "user", "content": "x"}], _client(_never))
    assert out[0]["content"] == "x"
    assert events == []


async def test_post_call_ignores_pre_call_guards() -> None:
    text, events, blocked = await run_post_call([_spec("length", {"max_chars": 2})], "not scanned", _client(_never))
    assert (text, events, blocked) == ("not scanned", [], False)


async def test_unknown_engine_skipped_without_event() -> None:
    guards = [_spec("magic", {"whatever": 1})]
    out, events = await run_pre_call(guards, [{"role": "user", "content": "x"}], _client(_never))
    assert out[0]["content"] == "x"
    assert events == []


def test_event_as_dict_shape() -> None:
    ev = GuardrailEvent("g", "pre_call", "length", "pass", "d")
    assert ev.as_dict() == {"guardrail": "g", "mode": "pre_call", "engine": "length", "action": "pass", "detail": "d"}


def test_guardrail_blocked_defaults_and_str() -> None:
    exc = GuardrailBlocked("n", "pre_call", "d")
    assert (exc.name, exc.mode, exc.detail, exc.engine) == ("n", "pre_call", "d", "")
    assert str(exc) == "blocked by guardrail 'n': d"


# ---------------------------------------------------------------------------
# blocklist
# ---------------------------------------------------------------------------


async def test_blocklist_block_raises_on_first_config_order_keyword() -> None:
    guards = [_spec("blocklist", {"keywords": ["zebra", "apple"]})]
    messages = [
        {"role": "system", "content": "you are helpful"},
        {"role": "user", "content": "i love APPLE pie and zebra crossings"},
    ]
    with pytest.raises(GuardrailBlocked) as excinfo:
        await run_pre_call(guards, messages, _client(_never))
    exc = excinfo.value
    assert exc.name == "blocklist-pre_call"
    assert exc.mode == "pre_call"
    assert exc.engine == "blocklist"
    # FIRST keyword in CONFIG order, original casing, matched case-insensitively
    assert exc.detail == "blocked keyword 'zebra'"
    assert str(exc) == "blocked by guardrail 'blocklist-pre_call': blocked keyword 'zebra'"


async def test_blocklist_soft_block_carries_reply_text() -> None:
    """A non-empty `reply_text` in the config rides the exception — the
    proxy's signal to return the notice as a normal completion (soft block)
    instead of the 400."""
    guards = [_spec("blocklist", {"keywords": ["zebra"], "reply_text": "blocked topic; turn dropped"})]
    with pytest.raises(GuardrailBlocked) as excinfo:
        await run_pre_call(guards, [{"role": "user", "content": "zebra"}], _client(_never))
    assert excinfo.value.reply_text == "blocked topic; turn dropped"
    # default (no reply_text) stays the hard block
    with pytest.raises(GuardrailBlocked) as excinfo2:
        await run_pre_call(
            [_spec("blocklist", {"keywords": ["zebra"]})],
            [{"role": "user", "content": "zebra"}],
            _client(_never),
        )
    assert excinfo2.value.reply_text == ""


async def test_length_soft_block_carries_reply_text() -> None:
    guards = [_spec("length", {"max_chars": 5, "reply_text": "input too long"})]
    with pytest.raises(GuardrailBlocked) as excinfo:
        await run_pre_call(guards, [{"role": "user", "content": "way too long"}], _client(_never))
    assert excinfo.value.engine == "length"
    assert excinfo.value.reply_text == "input too long"


async def test_blocklist_mask_replaces_every_occurrence_in_every_part() -> None:
    guards = [_spec("blocklist", {"keywords": ["alpha", "beta"], "action": "mask"})]
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "alpha and ALPHA again"},
        {"role": "assistant", "content": "alpha beta"},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "beta BETA beta"},
                {"type": "image_url", "image_url": {"url": "http://x/img.png"}},
            ],
        },
    ]
    out, events = await run_pre_call(guards, messages, _client(_never))
    assert out[1]["content"] == f"{MASK} and {MASK} again"
    parts = out[3]["content"]
    assert parts[0]["text"] == f"{MASK} {MASK} {MASK}"
    assert parts[1] == {"type": "image_url", "image_url": {"url": "http://x/img.png"}}
    assert out[0]["content"] == "sys"  # system untouched
    assert out[2]["content"] == "alpha beta"  # assistant untouched
    assert messages[1]["content"] == "alpha and ALPHA again"  # input not mutated
    assert len(events) == 1
    ev = events[0]
    assert (ev.guardrail, ev.mode, ev.engine, ev.action) == (
        "blocklist-pre_call",
        "pre_call",
        "blocklist",
        "masked",
    )
    assert ev.detail == "masked 5 occurrence(s)"


async def test_blocklist_mask_escapes_regex_metacharacters() -> None:
    guards = [_spec("blocklist", {"keywords": ["c++"], "action": "mask"})]
    out, events = await run_pre_call(guards, [{"role": "user", "content": "C++ not cc"}], _client(_never))
    assert out[0]["content"] == f"{MASK} not cc"
    assert events[0].detail == "masked 1 occurrence(s)"


async def test_blocklist_pass_event_when_no_hit() -> None:
    guards = [_spec("blocklist", {"keywords": ["forbidden", ""]})]  # empty keyword ignored
    out, events = await run_pre_call(guards, [{"role": "user", "content": "hello"}], _client(_never))
    assert out[0]["content"] == "hello"
    assert events == [GuardrailEvent("blocklist-pre_call", "pre_call", "blocklist", "pass", "")]


async def test_blocklist_post_call_block_and_mask() -> None:
    blockers = [_spec("blocklist", {"keywords": ["secret"]}, mode="post_call")]
    text, events, blocked = await run_post_call(blockers, "the SECRET is out", _client(_never))
    assert blocked is True
    assert text == "the SECRET is out"  # refused as-is, not rewritten
    assert events[0].action == "blocked"

    maskers = [_spec("blocklist", {"keywords": ["secret"], "action": "mask"}, mode="post_call")]
    text, events, blocked = await run_post_call(maskers, "the SECRET is out", _client(_never))
    assert blocked is False
    assert text == f"the {MASK} is out"
    assert events[0].detail == "masked 1 occurrence(s)"


# ---------------------------------------------------------------------------
# regex_pii
# ---------------------------------------------------------------------------


async def test_regex_pii_masks_email_phone_and_card_with_detail_format() -> None:
    guards = [_spec("regex_pii")]
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "contact alice@example.com please"},
                {"type": "text", "text": "call 555-123-4567 now"},
                {"type": "text", "text": "pay with 4111111111111111"},
            ],
        },
    ]
    out, events = await run_pre_call(guards, messages, _client(_never))
    parts = out[0]["content"]
    assert parts[0]["text"] == f"contact {MASK} please"
    assert parts[1]["text"] == f"call {MASK} now"
    assert parts[2]["text"] == f"pay with {MASK}"
    assert len(events) == 1
    assert events[0].action == "masked"
    assert events[0].detail == "EMAILx1, PHONEx1, CREDIT_CARDx1"  # canonical order


async def test_regex_pii_counts_aggregate_across_parts() -> None:
    guards = [_spec("regex_pii", {"entities": ["EMAIL"]})]
    messages = [{"role": "user", "content": "a@b.co then c@d.io"}]
    out, events = await run_pre_call(guards, messages, _client(_never))
    assert out[0]["content"] == f"{MASK} then {MASK}"
    assert events[0].detail == "EMAILx2"


async def test_regex_pii_phone_not_matched_inside_longer_digit_run() -> None:
    guards = [_spec("regex_pii", {"entities": ["PHONE"]})]
    messages = [{"role": "user", "content": "order id 555-123-45678901 ref"}]
    out, events = await run_pre_call(guards, messages, _client(_never))
    assert "555-123-45678901" in out[0]["content"]  # untouched
    assert events[0].action == "pass"
    assert events[0].detail == ""


async def test_regex_pii_card_requires_luhn() -> None:
    guards = [_spec("regex_pii", {"entities": ["CREDIT_CARD"]})]
    messages = [{"role": "user", "content": "cards 4111111111111111 and 4111111111111112"}]
    out, events = await run_pre_call(guards, messages, _client(_never))
    assert out[0]["content"] == f"cards {MASK} and 4111111111111112"
    assert events[0].detail == "CREDIT_CARDx1"


async def test_regex_pii_spaced_card_masked_without_eating_next_word() -> None:
    guards = [_spec("regex_pii", {"entities": ["CREDIT_CARD"]})]
    messages = [{"role": "user", "content": "card 4111 1111 1111 1111 end"}]
    out, _ = await run_pre_call(guards, messages, _client(_never))
    assert out[0]["content"] == f"card {MASK} end"


async def test_regex_pii_entity_subset_selection() -> None:
    guards = [_spec("regex_pii", {"entities": ["EMAIL"]})]
    messages = [{"role": "user", "content": "mail a@b.co phone 555-123-4567"}]
    out, events = await run_pre_call(guards, messages, _client(_never))
    assert out[0]["content"] == f"mail {MASK} phone 555-123-4567"
    assert events[0].detail == "EMAILx1"

    # missing OR empty entities config = all three
    empty = [_spec("regex_pii", {"entities": []})]
    out, events = await run_pre_call(empty, [{"role": "user", "content": "a@b.co"}], _client(_never))
    assert events[0].detail == "EMAILx1"


async def test_regex_pii_pass_event_when_nothing_matches() -> None:
    guards = [_spec("regex_pii")]
    _, events = await run_pre_call(guards, [{"role": "user", "content": "nothing here"}], _client(_never))
    assert events == [GuardrailEvent("regex_pii-pre_call", "pre_call", "regex_pii", "pass", "")]


async def test_regex_pii_post_call_masks_text() -> None:
    guards = [_spec("regex_pii", mode="post_call")]
    text, events, blocked = await run_post_call(guards, "reach me at bob@example.org or 555-123-4567", _client(_never))
    assert blocked is False
    assert text == f"reach me at {MASK} or {MASK}"
    assert events[0].detail == "EMAILx1, PHONEx1"


# ---------------------------------------------------------------------------
# length
# ---------------------------------------------------------------------------


async def test_length_default_pre_limit() -> None:
    guards = [_spec("length")]
    _, events = await run_pre_call(guards, [{"role": "user", "content": "x" * 100_000}], _client(_never))
    assert events[0].action == "pass"
    with pytest.raises(GuardrailBlocked) as excinfo:
        await run_pre_call(guards, [{"role": "user", "content": "x" * 100_001}], _client(_never))
    assert excinfo.value.engine == "length"
    assert excinfo.value.detail == "input length 100001 exceeds max_chars 100000"


async def test_length_custom_max_chars() -> None:
    guards = [_spec("length", {"max_chars": 10})]
    with pytest.raises(GuardrailBlocked) as excinfo:
        await run_pre_call(guards, [{"role": "user", "content": "a" * 11}], _client(_never))
    assert excinfo.value.detail == "input length 11 exceeds max_chars 10"
    _, events = await run_pre_call(guards, [{"role": "user", "content": "a" * 10}], _client(_never))
    assert events[0].action == "pass"


async def test_length_counts_concat_across_user_parts() -> None:
    guards = [_spec("length", {"max_chars": 11})]
    messages = [
        {"role": "system", "content": "x" * 50},  # system text never counts
        {"role": "user", "content": "12345"},
        {"role": "user", "content": [{"type": "text", "text": "67890"}]},
    ]
    # user parts join with a newline: 5 + 1 + 5 = 11 chars, exactly at the cap
    _, events = await run_pre_call(guards, messages, _client(_never))
    assert events[0].action == "pass"
    with pytest.raises(GuardrailBlocked):
        await run_pre_call([_spec("length", {"max_chars": 10})], messages, _client(_never))


async def test_length_post_call_default_and_custom() -> None:
    post = [_spec("length", {"max_chars": 5}, mode="post_call")]
    text, events, blocked = await run_post_call(post, "abcdef", _client(_never))
    assert (text, blocked, events[0].action) == ("abcdef", True, "blocked")
    assert events[0].detail == "input length 6 exceeds max_chars 5"
    _, events, blocked = await run_post_call(post, "abcde", _client(_never))
    assert (blocked, events[0].action) == (False, "pass")

    default = [_spec("length", mode="post_call")]
    _, _, blocked = await run_post_call(default, "x" * 200_000, _client(_never))
    assert blocked is False
    _, _, blocked = await run_post_call(default, "x" * 200_001, _client(_never))
    assert blocked is True


# ---------------------------------------------------------------------------
# webhook
# ---------------------------------------------------------------------------


async def test_webhook_block_verdict_pre_and_post() -> None:
    with pytest.raises(GuardrailBlocked) as excinfo:
        await run_pre_call(
            [_spec("webhook", {"url": "http://mod/score"})],
            [{"role": "user", "content": "bad"}],
            _client(_json_handler({"verdict": "block", "reason": "disallowed"})),
        )
    assert excinfo.value.engine == "webhook"
    assert excinfo.value.detail == "disallowed"

    post_guards = [_spec("webhook", {"url": "http://mod/score"}, mode="post_call")]
    text, events, blocked = await run_post_call(post_guards, "bad output", _client(_json_handler({"verdict": "block"})))
    assert (text, blocked) == ("bad output", True)
    assert events[0].action == "blocked"
    assert events[0].detail == "webhook blocked"  # absent reason falls back


async def test_webhook_mask_single_user_part() -> None:
    guards = [_spec("webhook", {"url": "http://mod/score"})]
    messages = [{"role": "system", "content": "sys"}, {"role": "user", "content": "naughty input"}]
    out, events = await run_pre_call(guards, messages, _client(_json_handler({"verdict": "mask", "text": "sanitized"})))
    assert out[0]["content"] == "sys"
    assert out[1]["content"] == "sanitized"
    assert events[0].action == "masked"
    assert events[0].detail == "webhook returned masked text"


async def test_webhook_mask_multiple_user_parts_skipped() -> None:
    guards = [_spec("webhook", {"url": "http://mod/score"})]
    messages = [
        {"role": "user", "content": "part one"},
        {"role": "user", "content": [{"type": "text", "text": "part two"}]},
    ]
    out, events = await run_pre_call(guards, messages, _client(_json_handler({"verdict": "mask", "text": "sanitized"})))
    assert out[0]["content"] == "part one"  # nothing overwritten
    assert out[1]["content"][0]["text"] == "part two"
    assert events[0].action == "pass"
    assert events[0].detail == "webhook mask skipped: multiple user text parts"


async def test_webhook_mask_single_nonempty_part_among_empties() -> None:
    guards = [_spec("webhook", {"url": "http://mod/score"})]
    messages = [{"role": "user", "content": ""}, {"role": "user", "content": "only real part"}]
    out, events = await run_pre_call(guards, messages, _client(_json_handler({"verdict": "mask", "text": "sanitized"})))
    assert out[0]["content"] == ""  # the empty part is preserved, not overwritten
    assert out[1]["content"] == "sanitized"
    assert events[0].action == "masked"


async def test_webhook_mask_post_call_always_applies() -> None:
    guards = [_spec("webhook", {"url": "http://mod/score"}, mode="post_call")]
    text, events, blocked = await run_post_call(
        guards, "raw output", _client(_json_handler({"verdict": "mask", "text": "clean"}))
    )
    assert (text, blocked, events[0].action) == ("clean", False, "masked")


async def test_webhook_fail_open_on_connect_error() -> None:
    guards = [_spec("webhook", {"url": "http://mod/score"})]
    out, events = await run_pre_call(guards, [{"role": "user", "content": "x"}], _client(_connect_error_handler))
    assert out[0]["content"] == "x"  # request passes through untouched
    assert events[0].action == "pass"
    assert events[0].detail.startswith("webhook error:")

    post_guards = [_spec("webhook", {"url": "http://mod/score"}, mode="post_call")]
    text, events, blocked = await run_post_call(post_guards, "x", _client(_connect_error_handler))
    assert (text, blocked, events[0].action) == ("x", False, "pass")


async def test_webhook_error_detail_truncated_at_200() -> None:
    async def explode(url: str, **kwargs: Any) -> httpx2.Response:
        raise httpx2.ConnectError("e" * 500, request=None)

    class Exploder:
        post = staticmethod(explode)

    guards = [_spec("webhook", {"url": "http://mod/score"})]
    _, events = await run_pre_call(guards, [{"role": "user", "content": "x"}], Exploder())
    assert len(events[0].detail) == 200
    assert events[0].detail.startswith("webhook error: eeee")


async def test_webhook_fail_open_on_http_500() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(500, text="boom")

    guards = [_spec("webhook", {"url": "http://mod/score"})]
    _, events = await run_pre_call(guards, [{"role": "user", "content": "x"}], _client(handler))
    assert events[0].action == "pass"
    assert events[0].detail == "webhook error: HTTP 500"


async def test_webhook_fail_open_on_non_json_body() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, text="<html>oops</html>")

    guards = [_spec("webhook", {"url": "http://mod/score"})]
    _, events = await run_pre_call(guards, [{"role": "user", "content": "x"}], _client(handler))
    assert events[0].action == "pass"
    assert events[0].detail == "webhook error: non-JSON response"


async def test_webhook_missing_url_passes_without_http() -> None:
    for cfg in ({}, {"url": ""}, {"url": None}):
        guards = [_spec("webhook", cfg)]
        _, events = await run_pre_call(guards, [{"role": "user", "content": "x"}], _client(_never))
        assert events[0].action == "pass"
        assert events[0].detail == "no url configured"


async def test_webhook_pass_verdict_and_garbage() -> None:
    guards = [_spec("webhook", {"url": "http://mod/score"})]
    for payload in ({"verdict": "pass"}, {"verdict": "mask"}, {"verdict": "allow"}, [1, 2]):
        _, events = await run_pre_call(guards, [{"role": "user", "content": "x"}], _client(_json_handler(payload)))
        assert events[0].action == "pass"
        assert events[0].detail == ""


async def test_webhook_api_key_env_and_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}
    monkeypatch.setenv("PCAI_TEST_GUARD_KEY", "sekrit-123")
    guards = [_spec("webhook", {"url": "http://mod/score", "api_key_env": "PCAI_TEST_GUARD_KEY"})]
    await run_pre_call(guards, [{"role": "user", "content": "hello"}], _client(_recording_handler(seen)))
    assert seen["authorization"] == "Bearer sekrit-123"
    assert seen["content_type"] == "application/json"
    assert seen["payload"] == {"text": "hello", "guardrail": "webhook-pre_call"}

    # unset env var -> no Authorization header, request still fires
    seen.clear()
    guards = [_spec("webhook", {"url": "http://mod/score", "api_key_env": "PCAI_TEST_GUARD_MISSING"})]
    await run_pre_call(guards, [{"role": "user", "content": "hello"}], _client(_recording_handler(seen)))
    assert seen["authorization"] is None

    # empty env value -> no Authorization header either
    monkeypatch.setenv("PCAI_TEST_GUARD_EMPTY", "")
    seen.clear()
    guards = [_spec("webhook", {"url": "http://mod/score", "api_key_env": "PCAI_TEST_GUARD_EMPTY"})]
    await run_pre_call(guards, [{"role": "user", "content": "hello"}], _client(_recording_handler(seen)))
    assert seen["authorization"] is None


async def test_webhook_timeout_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "guardrail_webhook_timeout_s", 3.5)
    client = RecordingClient()
    guards = [_spec("webhook", {"url": "http://mod/score", "timeout_s": 1.25})]
    await run_pre_call(guards, [{"role": "user", "content": "hi"}], client)
    assert client.calls[0]["timeout"] == 1.25  # per-guardrail config wins

    client = RecordingClient()
    await run_pre_call([_spec("webhook", {"url": "http://mod/score"})], [{"role": "user", "content": "hi"}], client)
    assert client.calls[0]["timeout"] == 3.5  # otherwise the setting


# ---------------------------------------------------------------------------
# composition, ordering, caps
# ---------------------------------------------------------------------------


async def test_pre_call_composes_in_order() -> None:
    guards = [
        _spec("regex_pii", name="pii"),
        _spec("blocklist", {"keywords": ["redacted"]}, name="deny-mask-token"),
    ]
    with pytest.raises(GuardrailBlocked) as excinfo:
        await run_pre_call(guards, [{"role": "user", "content": "write a@b.com now"}], _client(_never))
    # the second guard SAW the first guard's masked output and blocked on it
    assert excinfo.value.detail == "blocked keyword 'redacted'"


async def test_events_capped_at_50() -> None:
    pre = [_spec("length", {"max_chars": 10_000_000}, name=f"pre-{i:02d}") for i in range(60)]
    _, events = await run_pre_call(pre, [{"role": "user", "content": "hi"}], _client(_never))
    assert len(events) == 50
    assert [e.guardrail for e in events] == [f"pre-{i:02d}" for i in range(50)]

    post = [_spec("length", {"max_chars": 10_000_000}, mode="post_call", name=f"post-{i:02d}") for i in range(60)]
    _, events, _ = await run_post_call(post, "hi", _client(_never))
    assert len(events) == 50


# ---------------------------------------------------------------------------
# guardrails_for_model (FakeDB — no live Postgres)
# ---------------------------------------------------------------------------


async def test_guardrails_for_model_parses_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    str_id = str(uuid.uuid4())
    rows = [
        _row("a-mask", "blocklist", '{"keywords": ["x"], "action": "mask"}'),
        _row("b-len", "length", '{"max_chars": 10}', mode="post_call"),
        _row("c-strid", "length", None, gid=str_id),
    ]
    fake = FakeDB(rows)
    monkeypatch.setattr(gr, "db", fake)
    specs = await guardrails_for_model("chat-model")
    sql, args = fake.calls[0]
    assert args == ("chat-model",)
    assert "model_guardrails" in sql and "ORDER BY g.name" in sql
    assert [s.name for s in specs] == ["a-mask", "b-len", "c-strid"]
    first = specs[0]
    assert isinstance(first.id, uuid.UUID)
    assert (first.mode, first.engine, first.enabled) == ("pre_call", "blocklist", True)
    assert first.config == {"keywords": ["x"], "action": "mask"}
    assert specs[1].config == {"max_chars": 10}
    assert specs[2].config == {}  # NULL config_json tolerated
    assert isinstance(specs[2].id, uuid.UUID)  # string ids coerced


async def test_guardrails_for_model_empty_string_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gr, "db", FakeDB([_row("e", "length", "")]))
    specs = await guardrails_for_model("m")
    assert specs[0].config == {}


async def test_guardrails_for_model_excludes_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [
        _row("on", "length", None, enabled=True),
        _row("off", "length", None, enabled=False),
    ]
    monkeypatch.setattr(gr, "db", FakeDB(rows))
    specs = await guardrails_for_model("m")
    assert [s.name for s in specs] == ["on"]


async def test_guardrails_for_model_db_error_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gr, "db", FakeDB(error=RuntimeError("pool dead")))
    with pytest.raises(RuntimeError, match="pool dead"):
        await guardrails_for_model("m")


async def test_guardrails_for_model_round_trips_into_runners(monkeypatch: pytest.MonkeyPatch) -> None:
    """The parsed specs drive the engines directly — the same path the
    proxy wiring uses."""
    rows = [_row("pii", "regex_pii", None)]
    monkeypatch.setattr(gr, "db", FakeDB(rows))
    specs = await guardrails_for_model("m")
    out, events = await run_pre_call(specs, [{"role": "user", "content": "a@b.co"}], _client(_never))
    assert out[0]["content"] == MASK
    assert events[0].detail == "EMAILx1"
