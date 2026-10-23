"""Regression tests for the admission token-estimate fixes (security audit).

Two verified bugs in routes_proxy._estimate_tokens:

1. Negative/invalid max_tokens (denial-of-wallet): `int(max_tokens)` ran
   unvalidated, so max_tokens=-100000 produced a negative estimate that
   check_rate_limit (core/budgets.py) added verbatim to the key's TPM
   window — DECREMENTING it — and `spent + est > limit` then admitted
   over-cap keys. A non-numeric string raised ValueError -> 500.

2. Base64 vision billing: `len(str(m["content"])) // 4` stringified
   multimodal content-part lists, so a 1.5 MB base64 screenshot read as
   ~501K prompt tokens and 429'd the key instantly.

The estimator is the fail-safe every caller relies on, so these tests
pin: never raises, never negative, and TEXT-ONLY prompt accounting.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from pcai_llm.api import routes_proxy
from pcai_llm.core import budgets as budgets_mod

# 1024 = the default completion expectation for absent/falsy max_tokens.
DEFAULT_COMPLETION = 1024


def _est(body: dict[str, Any]) -> int:
    return routes_proxy._estimate_tokens(body)


def _msg(content: Any) -> dict[str, Any]:
    return {"role": "user", "content": content}


# --------------------------------------------------------------------------
# Fix 1 — max_tokens validation: never raises, never negative
# --------------------------------------------------------------------------


def test_negative_max_tokens_floors_completion_at_zero() -> None:
    """max_tokens=-100000 must not produce a negative estimate.

    The estimate is added to the key's TPM window by check_rate_limit; a
    negative value credited the window and admitted over-cap keys.
    """
    est = _est({"messages": [], "max_tokens": -100000})
    assert est == 0, f"negative max_tokens must floor the estimate at 0, got {est}"
    assert est >= 0


def test_negative_max_tokens_keeps_positive_prompt_term() -> None:
    """Only the completion term floors at 0 — real prompt text still bills."""
    est = _est({"messages": [_msg("x" * 40)], "max_tokens": -5})
    assert est == 10, f"expected prompt 40//4 = 10 with 0 completion, got {est}"


def test_negative_string_max_tokens_floors_at_zero() -> None:
    """A numeric STRING survives int() and then floors like a negative int."""
    assert _est({"messages": [], "max_tokens": "-100000"}) == 0


def test_float_max_tokens_truncates_like_int() -> None:
    """float coercion keeps the prior int() semantics (truncation, not round)."""
    assert _est({"messages": [], "max_tokens": 100.7}) == 100
    assert _est({"messages": [], "max_tokens": 100.2}) == 100


def test_float_max_tokens_negative_floors_at_zero() -> None:
    assert _est({"messages": [], "max_tokens": -1.5}) == 0


def test_non_numeric_string_max_tokens_falls_back_to_default() -> None:
    """A non-numeric string used to raise ValueError -> HTTP 500."""
    assert _est({"messages": [], "max_tokens": "lots"}) == DEFAULT_COMPLETION


def test_nan_and_inf_max_tokens_fall_back_to_default() -> None:
    """NaN/inf cannot be int()'d — the estimator degrades to the default."""
    assert _est({"messages": [], "max_tokens": float("nan")}) == DEFAULT_COMPLETION
    assert _est({"messages": [], "max_tokens": float("inf")}) == DEFAULT_COMPLETION


def test_none_max_tokens_falls_back_to_default() -> None:
    assert _est({"messages": [], "max_tokens": None}) == DEFAULT_COMPLETION


def test_missing_max_tokens_falls_back_to_default() -> None:
    assert _est({"messages": []}) == DEFAULT_COMPLETION


def test_zero_max_tokens_falls_back_to_default() -> None:
    """0 is falsy — the historical default branch is preserved exactly."""
    assert _est({"messages": [], "max_tokens": 0}) == DEFAULT_COMPLETION


def test_max_tokens_still_capped_at_8192() -> None:
    """The completion cap from the pre-fix code is unchanged."""
    assert _est({"messages": [], "max_tokens": 262144}) == 8192
    assert _est({"messages": [], "max_tokens": 8192}) == 8192


def test_valid_max_tokens_charges_the_expectation_not_the_cap() -> None:
    """The 7-token completion expectation used by the relay-settle test."""
    assert _est({"messages": [_msg("x" * 40)], "max_tokens": 7}) == 17


def test_estimate_is_never_negative_across_hostile_bodies() -> None:
    """Sweep the hostile shapes: every one must yield a non-negative int."""
    hostile: list[dict[str, Any]] = [
        {"messages": [], "max_tokens": -1},
        {"messages": [], "max_tokens": -(2**63)},
        {"messages": [], "max_tokens": "-99999999999999999999"},
        {"messages": [], "max_tokens": True},  # bool is an int subclass
        {"messages": [], "max_tokens": []},
        {"messages": [], "max_tokens": {}},
        {"messages": [], "max_tokens": object()},
        {"messages": None, "max_tokens": -3},
        {"messages": "not-a-list", "max_tokens": -3},
    ]
    for body in hostile:
        est = _est(body)
        assert isinstance(est, int) and est >= 0, f"{body!r} -> {est!r}"


# --------------------------------------------------------------------------
# Fix 2 — prompt estimate is TEXT ONLY (base64 vision payloads ignored)


def _prompt_est(content: Any) -> int:
    """Prompt term for one message: max_tokens=1 sentinel, minus its 1 token."""
    return _est({"messages": [_msg(content)], "max_tokens": 1}) - 1


def test_plain_string_content_keeps_len_over_4() -> None:
    assert _prompt_est("x" * 40) == 10


def test_content_none_or_missing_estimates_zero() -> None:
    assert _prompt_est(None) == 0
    assert _est({"messages": [{"role": "user"}], "max_tokens": 1}) == 1  # 0 prompt + 1


def test_list_of_text_parts_sums_text_lengths() -> None:
    parts = [
        {"type": "text", "text": "a" * 40},
        {"type": "text", "text": "b" * 20},
    ]
    assert _prompt_est(parts) == 15  # (40 + 20) // 4


def test_image_url_base64_part_contributes_zero() -> None:
    """The verified bug: a 1.5 MB base64 screenshot billed as ~501K tokens."""
    b64 = "data:image/png;base64," + "A" * 1_500_000
    parts = [{"type": "image_url", "image_url": {"url": b64}}]
    assert _prompt_est(parts) == 0, "base64 image data must not bill as tokens"
    # and the whole-request estimate stays at the default completion
    assert _est({"messages": [_msg(parts)]}) == DEFAULT_COMPLETION


def test_audio_and_file_parts_contribute_zero() -> None:
    parts = [
        {"type": "input_audio", "input_audio": {"data": "A" * 100_000, "format": "wav"}},
        {"type": "file", "file": {"file_data": "B" * 100_000}},
    ]
    assert _prompt_est(parts) == 0


def test_mixed_list_counts_only_text_parts() -> None:
    """Mixed text + image + audio: only the text field is billed."""
    b64 = "data:image/png;base64," + "A" * 200_000
    parts = [
        {"type": "text", "text": "c" * 80},
        {"type": "image_url", "image_url": {"url": b64}},
        {"type": "input_audio", "input_audio": {"data": "A" * 50_000, "format": "mp3"}},
        {"type": "text", "text": "d" * 8},
    ]
    est = _prompt_est(parts)
    assert est == 22, f"expected (80 + 8) // 4 = 22 text tokens only, got {est}"


def test_non_dict_and_textless_parts_are_skipped() -> None:
    """Junk parts (strings, dicts without a text field, non-str text) are 0."""
    parts: list[Any] = [
        "bare-string-part",
        {"type": "image_url", "image_url": {"url": "http://x/i.png"}},
        {"type": "text"},  # no text field
        {"type": "text", "text": None},
        {"type": "text", "text": 12345},  # non-str text is not billed
        {"type": "text", "text": "e" * 12},
    ]
    assert _prompt_est(parts) == 3


def test_message_entries_that_are_not_dicts_are_skipped() -> None:
    messages: list[Any] = ["not-a-message", None, 42, {"role": "user", "content": "f" * 8}]
    assert _est({"messages": messages, "max_tokens": 1}) == 3  # 8//4 + 1


def test_multimodal_request_with_negative_max_tokens_stays_non_negative() -> None:
    """Both fixes together: the DoW vector with a base64 payload."""
    b64 = "data:image/png;base64," + "A" * 1_500_000
    body = {
        "messages": [_msg([{"type": "text", "text": "hi"}, {"type": "image_url", "image_url": {"url": b64}}])],
        "max_tokens": -100000,
    }
    est = _est(body)
    assert est == 0, f"expected 0 (0 text + floored completion), got {est}"


def test_full_request_estimate_combines_prompt_and_completion() -> None:
    body = {
        "messages": [
            {"role": "system", "content": "s" * 40},
            _msg(
                [
                    {"type": "text", "text": "u" * 80},
                    {"type": "image_url", "image_url": {"url": "data:x"}},
                ]
            ),
        ],
        "max_tokens": 500,
    }
    assert _est(body) == (40 // 4 + 80 // 4) + 500


# --------------------------------------------------------------------------
# Exploit-path closure: the estimate as it reaches check_rate_limit
# --------------------------------------------------------------------------


class _WindowDB:
    """Minimal db stub replaying the rate_windows upsert arithmetic in SQL.

    `tokens = tokens + $3` is the exact statement under attack: a negative
    $3 DECREMENTS the window (the denial-of-wallet vector). Recording the
    bound parameter proves what the gate would have added.
    """

    def __init__(self, tokens: int = 0) -> None:
        self.tokens = tokens
        self.added: list[int] = []

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any]:
        est = int(args[2])
        self.added.append(est)
        self.tokens += est
        return {"requests": 1, "tokens": self.tokens}


@pytest.mark.asyncio
async def test_negative_estimate_can_no_longer_decrement_the_tpm_window(monkeypatch) -> None:
    """The verified vector end-to-end: a hostile max_tokens reaches the gate
    as a NON-NEGATIVE estimate, so the window is charged, not credited."""
    fake = _WindowDB(tokens=500)
    monkeypatch.setattr(budgets_mod, "db", fake)
    key_id = uuid.uuid4()

    est = routes_proxy._estimate_tokens({"messages": [_msg("x" * 40)], "max_tokens": -100000})
    gate = await budgets_mod.check_rate_limit(key_id, rpm=1000, tpm=1_000_000, est_tokens=est)

    assert fake.added == [10], f"window must be charged the 10-token prompt, got {fake.added}"
    assert fake.tokens == 510, "the TPM window must never be decremented"
    assert gate.ok


@pytest.mark.asyncio
async def test_over_cap_key_is_no_longer_admitted_by_a_negative_estimate(monkeypatch) -> None:
    """The second half of the vector: `spent + est > limit` admitted an
    over-cap key because est was negative. With est floored at 0 the budget
    gate still refuses an over-cap key."""
    spent, limit = 5000, 1000  # spent ALREADY past the limit
    est = routes_proxy._estimate_tokens({"messages": [], "max_tokens": -100000})
    assert est >= 0
    assert spent + est > limit, "an over-cap key must not be admitted by the estimate"

    # Sanity on the pre-fix arithmetic this pins: the same -100000 estimate
    # would have made the comparison FALSE (5000 - 100000 <= 1000), i.e.
    # the over-cap key sailed through.
    assert not (spent + (-100000) > limit)
