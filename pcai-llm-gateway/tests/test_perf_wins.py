"""Regression tests for the three measured performance wins (Wave 2).

FIX 1 — core/toolrelay._estimate_relay_tokens kept BOTH audited bug shapes
    that routes_proxy._estimate_tokens had already fixed:
      * ``len(str(m.get("content")))`` stringified multimodal content-part
        lists, billing a 1.5 MB base64 screenshot as ~501K prompt tokens;
      * ``int(max_tokens)`` ran unguarded — a non-numeric value raised
        ValueError -> 500, and a negative value produced a negative estimate
        that check_rate_limit (core/budgets.py) adds verbatim to the key's
        TPM window, DECREMENTING it (denial-of-wallet).
    Pinned here: never raises, never negative, TEXT-ONLY prompt accounting,
    and byte-parity with the routes_proxy helpers it was ported from.

FIX 2 — core/guardrails.guardrails_for_model SELECTed on every guarded
    request. Now TTL-cached per model name (60s default); only SUCCESSFUL
    reads are cached, DB errors still propagate uncached, and
    guardrails_spec_cache_clear() restores the uncached behavior.

FIX 3 — core/conversations.replace_messages used the UNBOUNDED db.acquire()
    on a tenant-facing route; it now takes the bounded hot-path acquire
    (db.acquire_bounded -> settings.db_acquire_timeout_s -> PoolUnavailable,
    the established fast-503 path).

Test style mirrors tests/test_estimate_fix.py (hostile-input sweeps against
the real helpers) and tests/test_backend_hardening.py (NeverPool for the
bounded-acquire path).
"""

from __future__ import annotations

import asyncio
import time
import uuid
from contextlib import asynccontextmanager
from types import TracebackType
from typing import Any, Self

import pytest

from pcai_llm.api import routes_proxy
from pcai_llm.config import settings
from pcai_llm.core import budgets as budgets_mod
from pcai_llm.core import conversations as conv_mod
from pcai_llm.core import guardrails as gr
from pcai_llm.core import toolrelay
from pcai_llm.core.guardrails import guardrails_for_model
from pcai_llm.db import Database, PoolUnavailable

# 1024 = the default completion expectation for absent/falsy max_tokens.
DEFAULT_COMPLETION = 1024


# ===========================================================================
# FIX 1 — relay fallback estimator
# ===========================================================================


def _est(msgs: list[Any], max_tokens: Any = None) -> int:
    return toolrelay._estimate_relay_tokens(msgs, max_tokens)


def _msg(content: Any) -> dict[str, Any]:
    return {"role": "user", "content": content}


def _prompt_est(content: Any) -> int:
    """Prompt term for one message: max_tokens=1 sentinel, minus its 1 token."""
    return _est([_msg(content)], 1) - 1


def test_relay_negative_max_tokens_floors_completion_at_zero() -> None:
    """max_tokens=-100000 must not produce a negative estimate.

    The estimate is added to the key's TPM window by check_rate_limit; a
    negative value credited the window and admitted over-cap keys.
    """
    est = _est([], -100000)
    assert est == 0, f"negative max_tokens must floor the estimate at 0, got {est}"
    assert est >= 0


def test_relay_negative_max_tokens_keeps_positive_prompt_term() -> None:
    """Only the completion term floors at 0 — real prompt text still bills."""
    assert _est([_msg("x" * 40)], -5) == 10


def test_relay_negative_string_max_tokens_floors_at_zero() -> None:
    """A numeric STRING survives int() and then floors like a negative int."""
    assert _est([], "-100000") == 0


def test_relay_non_numeric_string_max_tokens_falls_back_to_default() -> None:
    """A non-numeric string used to raise ValueError -> HTTP 500."""
    assert _est([], "lots") == DEFAULT_COMPLETION


def test_relay_nan_and_inf_max_tokens_fall_back_to_default() -> None:
    """NaN/inf cannot be int()'d — the estimator degrades to the default."""
    assert _est([], float("nan")) == DEFAULT_COMPLETION
    assert _est([], float("inf")) == DEFAULT_COMPLETION


def test_relay_falsy_max_tokens_keep_the_historical_default() -> None:
    """None / missing / 0 keep the pre-fix default branch exactly."""
    assert _est([]) == DEFAULT_COMPLETION
    assert _est([], None) == DEFAULT_COMPLETION
    assert _est([], 0) == DEFAULT_COMPLETION


def test_relay_max_tokens_still_capped_at_8192() -> None:
    """The completion cap from the pre-fix code is unchanged."""
    assert _est([], 262144) == 8192
    assert _est([], 8192) == 8192


def test_relay_float_max_tokens_truncates_like_int() -> None:
    assert _est([], 100.7) == 100
    assert _est([], -1.5) == 0


def test_relay_estimate_never_raises_and_never_negative() -> None:
    """Sweep the hostile shapes: every one yields a non-negative int."""
    hostile: list[Any] = [
        -1,
        -(2**63),
        "-99999999999999999999",
        True,  # bool is an int subclass
        [],
        {},
        object(),
        "lots",
        float("nan"),
        float("-inf"),
    ]
    for mt in hostile:
        est = _est([_msg("x" * 8)], mt)
        assert isinstance(est, int) and est >= 0, f"max_tokens={mt!r} -> {est!r}"


def test_relay_hostile_message_shapes_never_raise() -> None:
    """Junk message shapes must not crash the settle fallback either."""
    msgs: list[Any] = [
        "not-a-message",
        None,
        42,
        {"role": "user"},  # no content
        {"role": "user", "content": None},
        {"role": "user", "content": 12345},
        {"role": "user", "content": {"nested": "dict"}},
        {"role": "user", "content": "y" * 12},
    ]
    assert _est(msgs) == 3 + DEFAULT_COMPLETION
    assert _est("not-a-list") == DEFAULT_COMPLETION  # type: ignore[arg-type]


def test_relay_plain_string_content_keeps_len_over_4() -> None:
    assert _prompt_est("x" * 40) == 10


def test_relay_image_url_base64_contributes_zero() -> None:
    """The verified bug: a 1.5 MB base64 screenshot billed as ~501K tokens."""
    b64 = "data:image/png;base64," + "A" * 1_500_000
    parts = [{"type": "image_url", "image_url": {"url": b64}}]
    assert _prompt_est(parts) == 0, "base64 image data must not bill as tokens"
    assert _est([_msg(parts)]) == DEFAULT_COMPLETION


def test_relay_audio_and_file_parts_contribute_zero() -> None:
    parts = [
        {"type": "input_audio", "input_audio": {"data": "A" * 100_000, "format": "wav"}},
        {"type": "file", "file": {"file_data": "B" * 100_000}},
    ]
    assert _prompt_est(parts) == 0


def test_relay_mixed_list_counts_only_text_parts() -> None:
    b64 = "data:image/png;base64," + "A" * 200_000
    parts = [
        {"type": "text", "text": "c" * 80},
        {"type": "image_url", "image_url": {"url": b64}},
        {"type": "input_audio", "input_audio": {"data": "A" * 50_000, "format": "mp3"}},
        {"type": "text", "text": "d" * 8},
    ]
    assert _prompt_est(parts) == 22  # (80 + 8) // 4


def test_relay_textless_and_non_dict_parts_are_skipped() -> None:
    parts: list[Any] = [
        "bare-string-part",
        {"type": "image_url", "image_url": {"url": "http://x/i.png"}},
        {"type": "text"},  # no text field
        {"type": "text", "text": None},
        {"type": "text", "text": 12345},  # non-str text is not billed
        {"type": "text", "text": "e" * 12},
    ]
    assert _prompt_est(parts) == 3


def test_relay_multimodal_request_with_negative_max_tokens_stays_non_negative() -> None:
    """Both fixes together: the DoW vector with a base64 payload."""
    b64 = "data:image/png;base64," + "A" * 1_500_000
    msgs = [_msg([{"type": "text", "text": "hi"}, {"type": "image_url", "image_url": {"url": b64}}])]
    assert _est(msgs, -100000) == 0


# --- parity with the routes_proxy source of truth -------------------------


def test_relay_message_helper_matches_routes_proxy() -> None:
    """The replicated twin must stay byte-identical to the API helper.

    routes_proxy imports core.toolrelay at module scope, so toolrelay cannot
    import routes_proxy back (circular) — the helper is replicated with a
    comment naming the source of truth. This test is the drift alarm.
    """
    contents: list[Any] = [
        "x" * 40,
        None,
        12345,
        ["bare"],
        [{"type": "text", "text": "a" * 40}, {"type": "text", "text": "b" * 20}],
        [{"type": "image_url", "image_url": {"url": "data:image/png;base64," + "A" * 1_000_000}}],
        [{"type": "input_audio", "input_audio": {"data": "A" * 10}}],
        [{"type": "text"}, {"type": "text", "text": None}, {"type": "text", "text": 7}],
        {"nested": "dict"},
    ]
    for content in contents:
        assert toolrelay._message_text_tokens(_msg(content)) == routes_proxy._message_text_tokens(_msg(content)), (
            f"helper drift on {content!r:.60}"
        )
    assert toolrelay._message_text_tokens("not-a-message") == routes_proxy._message_text_tokens("not-a-message")
    assert toolrelay._message_text_tokens(None) == routes_proxy._message_text_tokens(None)


def test_relay_estimate_matches_routes_proxy_estimate() -> None:
    """Whole-estimate parity across the hostile max_tokens shapes."""
    cases: list[tuple[list[Any], Any]] = [
        ([_msg("x" * 40)], 7),
        ([_msg("x" * 40)], -100000),
        ([], "lots"),
        ([], float("nan")),
        ([], None),
        ([], 0),
        ([], 262144),
        ([_msg([{"type": "text", "text": "t" * 40}])], 500),
        ([{"role": "user"}], 12),
    ]
    for msgs, mt in cases:
        body = {"messages": msgs, "max_tokens": mt}
        assert _est(msgs, mt) == routes_proxy._estimate_tokens(body), f"estimate drift on {body!r}"


@pytest.mark.asyncio
async def test_relay_negative_estimate_can_no_longer_decrement_the_tpm_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """The exploit path end-to-end for the RELAY estimator: a hostile
    max_tokens reaches the gate as a NON-NEGATIVE estimate, so the window is
    charged, never credited."""
    fake = _WindowDB(tokens=500)
    monkeypatch.setattr(budgets_mod, "db", fake)
    key_id = uuid.uuid4()

    est = _est([_msg("x" * 40)], -100000)
    gate = await budgets_mod.check_rate_limit(key_id, rpm=1000, tpm=1_000_000, est_tokens=est)

    assert fake.added == [10], f"window must be charged the 10-token prompt, got {fake.added}"
    assert fake.tokens == 510, "the TPM window must never be decremented"
    assert gate.ok


class _WindowDB:
    """db stub replaying the rate_windows upsert arithmetic in SQL.

    ``tokens = tokens + $3`` is the exact statement under attack: a negative
    $3 DECREMENTS the window. Recording the bound parameter proves what the
    gate would have added (same stub as tests/test_estimate_fix.py).
    """

    def __init__(self, tokens: int = 0) -> None:
        self.tokens = tokens
        self.added: list[int] = []

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any]:
        est = int(args[2])
        self.added.append(est)
        self.tokens += est
        return {"requests": 1, "tokens": self.tokens}


# ===========================================================================
# FIX 2 — guardrail-spec TTL cache
# ===========================================================================


@pytest.fixture(autouse=True)
def _clear_spec_cache() -> Any:
    """Module-global cache: isolate every test in this file."""
    gr.guardrails_spec_cache_clear()
    yield
    gr.guardrails_spec_cache_clear()


def _row(name: str, engine: str = "length", config_json: str | None = None, enabled: bool = True) -> dict[str, Any]:
    return {
        "id": uuid.uuid4(),
        "name": name,
        "mode": "pre_call",
        "engine": engine,
        "config_json": config_json,
        "enabled": enabled,
    }


class CountingGuardrailDB:
    """Counting fake for the guardrails_for_model SELECT.

    Same shape as tests/test_guardrails_core.FakeDB (rows sorted by name, the
    query's $1 is the model name) plus a call counter and optional per-model
    row sets so cache-key collisions are observable.
    """

    def __init__(
        self,
        rows: list[dict[str, Any]] | None = None,
        error: Exception | None = None,
        by_model: dict[str, list[dict[str, Any]]] | None = None,
    ) -> None:
        self.rows = rows or []
        self.error = error
        self.by_model = by_model or {}
        self.calls = 0
        self.models: list[Any] = []

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        self.calls += 1
        self.models.append(args[0] if args else None)
        if self.error is not None:
            raise self.error
        rows = self.by_model.get(args[0], []) if self.by_model else self.rows
        return sorted((dict(r) for r in rows), key=lambda r: r["name"])


@pytest.mark.asyncio
async def test_spec_cache_second_call_does_not_requery(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = CountingGuardrailDB([_row("a"), _row("b")])
    monkeypatch.setattr(gr, "db", fake)

    first = await guardrails_for_model("chat-model")
    assert fake.calls == 1
    assert [s.name for s in first] == ["a", "b"]

    second = await guardrails_for_model("chat-model")
    assert fake.calls == 1, "the second call within the TTL must be served from cache"
    assert [s.name for s in second] == ["a", "b"]
    assert second == first


@pytest.mark.asyncio
async def test_spec_cache_expires_after_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = CountingGuardrailDB([_row("a")])
    monkeypatch.setattr(gr, "db", fake)
    monkeypatch.setattr(gr, "_SPEC_TTL_S", 0.05)

    await guardrails_for_model("m")
    await asyncio.sleep(0.08)  # past the (shortened) TTL
    await guardrails_for_model("m")
    assert fake.calls == 2, "an expired entry must re-query"


@pytest.mark.asyncio
async def test_spec_cache_clear_restores_requery(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = CountingGuardrailDB([_row("a")])
    monkeypatch.setattr(gr, "db", fake)

    await guardrails_for_model("m")
    assert fake.calls == 1
    gr.guardrails_spec_cache_clear()
    await guardrails_for_model("m")
    assert fake.calls == 2, "clear() must drop the entry"


@pytest.mark.asyncio
async def test_spec_cache_clear_is_idempotent_on_empty_cache() -> None:
    gr.guardrails_spec_cache_clear()
    gr.guardrails_spec_cache_clear()
    gr.guardrails_spec_cache_clear("never-cached")


@pytest.mark.asyncio
async def test_spec_cache_keyed_per_model_no_collision(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = CountingGuardrailDB(
        by_model={
            "model-a": [_row("a-only")],
            "model-b": [_row("b-only")],
        }
    )
    monkeypatch.setattr(gr, "db", fake)

    a = await guardrails_for_model("model-a")
    b = await guardrails_for_model("model-b")
    assert [s.name for s in a] == ["a-only"]
    assert [s.name for s in b] == ["b-only"]
    assert fake.calls == 2

    # Both cached — and still no cross-talk.
    assert [s.name for s in await guardrails_for_model("model-a")] == ["a-only"]
    assert [s.name for s in await guardrails_for_model("model-b")] == ["b-only"]
    assert fake.calls == 2


@pytest.mark.asyncio
async def test_spec_cache_clear_one_model_leaves_the_other(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = CountingGuardrailDB(
        by_model={
            "model-a": [_row("a-only")],
            "model-b": [_row("b-only")],
        }
    )
    monkeypatch.setattr(gr, "db", fake)
    await guardrails_for_model("model-a")
    await guardrails_for_model("model-b")
    assert fake.calls == 2

    gr.guardrails_spec_cache_clear("model-a")
    await guardrails_for_model("model-b")  # still cached
    assert fake.calls == 2
    await guardrails_for_model("model-a")  # re-queried
    assert fake.calls == 3


@pytest.mark.asyncio
async def test_spec_cache_does_not_cache_db_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """A DB error must PROPAGATE as today and cache NOTHING."""
    fake = CountingGuardrailDB([_row("a")], error=RuntimeError("pool dead"))
    monkeypatch.setattr(gr, "db", fake)

    with pytest.raises(RuntimeError, match="pool dead"):
        await guardrails_for_model("m")
    with pytest.raises(RuntimeError, match="pool dead"):
        await guardrails_for_model("m")
    assert fake.calls == 2, "a failed read must never be cached"

    fake.error = None  # DB recovered — the next call must read fresh
    specs = await guardrails_for_model("m")
    assert fake.calls == 3
    assert [s.name for s in specs] == ["a"]


@pytest.mark.asyncio
async def test_spec_cache_does_not_cache_parse_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    """A malformed config_json raises (documented) — and caches nothing."""
    fake = CountingGuardrailDB([_row("bad", config_json='["not-an-object"]')])
    monkeypatch.setattr(gr, "db", fake)

    with pytest.raises(ValueError, match="must be a JSON object"):
        await guardrails_for_model("m")
    assert fake.calls == 1

    fake.rows = [_row("good", config_json='{"max_chars": 10}')]
    specs = await guardrails_for_model("m")
    assert fake.calls == 2, "the failed parse must not have been cached"
    assert [s.name for s in specs] == ["good"]


@pytest.mark.asyncio
async def test_spec_cache_caches_the_empty_result(monkeypatch: pytest.MonkeyPatch) -> None:
    """'No guardrails attached' is a valid, cacheable answer (no re-query)."""
    fake = CountingGuardrailDB([])
    monkeypatch.setattr(gr, "db", fake)

    assert await guardrails_for_model("m") == []
    assert await guardrails_for_model("m") == []
    assert fake.calls == 1


@pytest.mark.asyncio
async def test_spec_cache_is_scoped_to_the_db_handle(monkeypatch: pytest.MonkeyPatch) -> None:
    """A cached spec list is only valid for the pool it was read through.

    Swapping the handle (the test suite's monkeypatched fakes, a future
    multi-database split) must never serve another handle's specs — the
    cache entry records the handle it was read with. Without this the
    module-global cache leaks across tests AND across pools.
    """
    fake_a = CountingGuardrailDB([_row("from-a")])
    fake_b = CountingGuardrailDB([_row("from-b")])
    monkeypatch.setattr(gr, "db", fake_a)
    assert [s.name for s in await guardrails_for_model("m")] == ["from-a"]
    assert fake_a.calls == 1

    monkeypatch.setattr(gr, "db", fake_b)
    assert [s.name for s in await guardrails_for_model("m")] == ["from-b"]
    assert fake_b.calls == 1, "a different handle must not be served the other handle's cache"

    await guardrails_for_model("m")  # the new handle is cached in its own right
    assert fake_b.calls == 1


# ===========================================================================
# FIX 3 — bounded acquire for conversation writes
# ===========================================================================


class NeverPool:
    """Pool whose acquire() never returns (mirrors test_backend_hardening)."""

    def __init__(self) -> None:
        self.releases = 0

    async def acquire(self) -> Any:  # pragma: no cover - never returns
        await asyncio.Event().wait()

    async def release(self, conn: Any) -> None:  # pragma: no cover
        self.releases += 1


class TripwireAcquireDB(Database):
    """Real bounded acquire + a tripwire on the UNBOUNDED one.

    Proves both halves of D-P11: the write takes the bounded path AND does
    not fall back to db.acquire() (which would queue unboundedly).
    """

    def __init__(self) -> None:
        super().__init__()
        self.pool = NeverPool()  # type: ignore[assignment]

    def acquire(self) -> Any:  # type: ignore[override]
        raise AssertionError("replace_messages must use acquire_bounded, not the unbounded db.acquire()")


@pytest.mark.asyncio
async def test_replace_messages_fast_fails_when_the_pool_is_exhausted(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pool exhaustion surfaces as PoolUnavailable (-> 503), FAST."""
    monkeypatch.setattr(settings, "db_acquire_timeout_s", 0.05)
    fake = TripwireAcquireDB()
    monkeypatch.setattr(conv_mod, "db", fake)

    started = time.monotonic()
    with pytest.raises(PoolUnavailable):
        await conv_mod.replace_messages(uuid.uuid4(), uuid.uuid4(), [{"role": "user", "content": "x"}])
    elapsed = time.monotonic() - started
    assert elapsed < 1.0, f"the bounded acquire must fail fast, took {elapsed:.2f}s"


@pytest.mark.asyncio
async def test_replace_messages_honors_the_configured_acquire_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """The wait is settings.db_acquire_timeout_s — the codebase's one knob."""
    monkeypatch.setattr(settings, "db_acquire_timeout_s", 0.02)
    monkeypatch.setattr(conv_mod, "db", TripwireAcquireDB())

    started = time.monotonic()
    with pytest.raises(PoolUnavailable):
        await conv_mod.replace_messages(uuid.uuid4(), uuid.uuid4(), [{"role": "user", "content": "x"}])
    assert time.monotonic() - started < 1.0


class _FakeTxn:
    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool:
        return False


class _BoundedFakeConn:
    def __init__(self) -> None:
        self.log: list[str] = []

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        self.log.append("lock-head")
        return {"title": conv_mod.DEFAULT_TITLE}

    async def execute(self, sql: str, *args: Any) -> str:
        self.log.append("bump" if "updated_at" in sql else "delete-msgs")
        return "OK"

    async def executemany(self, sql: str, rows: list[tuple]) -> None:
        self.log.append(f"insert-msgs:{len(rows)}")

    def transaction(self) -> _FakeTxn:
        return _FakeTxn()


class _BoundedFakeDB:
    """db exposing ONLY acquire_bounded() — no unbounded acquire at all.

    The write path must work against exactly the bounded contract (no
    `pool` attribute, no `acquire`), so a regression to db.acquire() fails
    here with AttributeError rather than passing silently.
    """

    def __init__(self) -> None:
        self.conn = _BoundedFakeConn()

    @asynccontextmanager
    async def acquire_bounded(self) -> Any:
        yield self.conn


@pytest.mark.asyncio
async def test_replace_messages_works_against_the_bounded_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _BoundedFakeDB()
    monkeypatch.setattr(conv_mod, "db", fake)

    n = await conv_mod.replace_messages(uuid.uuid4(), uuid.uuid4(), [{"role": "user", "content": "hi"}])
    assert n == 1
    assert fake.conn.log == ["lock-head", "bump", "delete-msgs", "insert-msgs:1"]
