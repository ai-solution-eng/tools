"""TTFT failover gate — the "200 headers then queue-stall" defect.

SGLang/vLLM return 200 headers IMMEDIATELY and then sit in queue (observed
live: 284s queue, 41-deep, KV 93%). The old contract ("failover only
pre-first-byte") treated that 200 as commitment, so a cascade request
streamed client keepalives until the 600s upstream timeout and `auto` could
never degrade away from a wedged-but-alive upstream.

These tests pin the new contract: after the stream_options negotiation and
BEFORE anything reaches the client, `_stream_chat` peeks the upstream for
its first SSE DATA line under `settings.failover_ttft_s`. Comment/keepalive
lines do not satisfy the gate; `[DONE]` does; budget <= 0 disables the gate
entirely (byte-identical legacy path).

The mock-upstream discipline mirrors test_proxy.py / test_router_extras.py:
an in-process httpx2.MockTransport upstream, no live Postgres (the db
handles in budgets/costs/cascade/model_fallbacks are monkeypatched), and
settle rows accumulated from the FakeDB INSERT args (positional per
core.budgets.settle: 0 key_id, 1 model, 2 endpoint, 3 prompt, 4 completion,
5 total, 6 cached, 7 cost, 8 status, 9 latency, 10 request_id, ...).
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

import httpx2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pcai_llm.api import routes_proxy
from pcai_llm.api.auth import KeyRecord
from pcai_llm.config import settings
from pcai_llm.core import budgets as budgets_mod
from pcai_llm.core import cascade as cascade_mod
from pcai_llm.core import costs as costs_mod
from pcai_llm.core import model_fallbacks
from pcai_llm.routing.router import Endpoint, Router

PRIMARY = "wedged-model"
FALLBACK = "healthy-model"


# ---------------------------------------------------------------------------
# fakes (test_router_extras.py shape)
# ---------------------------------------------------------------------------


class FakeDB:
    """Budget/chain stub: no budgets, accumulates settle INSERT args."""

    def __init__(self) -> None:
        self.settled: list[tuple] = []
        self.chain_rows: dict[str, list[str]] = {}

    async def fetch(self, sql: str, *a: Any):
        if "FROM model_fallbacks" in sql:
            return [{"fallback_model": n} for n in self.chain_rows.get(a[0], [])]
        return []

    async def fetchrow(self, sql: str, *a: Any):
        if "profiles" in sql:
            return None
        if "rate_windows" in sql:
            return {"requests": 1, "tokens": 10}
        return None

    async def fetchval(self, sql: str, *a: Any):
        return None

    async def execute(self, sql: str, *a: Any):
        if "usage_ledger" in sql:
            self.settled.append(a)
        return "INSERT 0 1"

    def settles(self, status: str | None = None, model: str | None = None) -> list[tuple]:
        return [r for r in self.settled if (status is None or r[8] == status) and (model is None or r[1] == model)]


def _key() -> KeyRecord:
    return KeyRecord(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        name="k",
        key_prefix="hllm-XX",
        scopes=["proxy"],
        allowed_models=[],
        rate_limit_rpm=1000,
        rate_limit_tpm=1_000_000,
        max_concurrent=0,
        expires_at=None,
    )


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch):
    """Fake auth + fake db + per-test router. Returns (fake_db, key, client factory)."""
    fake = FakeDB()
    key = _key()

    import pcai_llm.api.auth as auth_mod

    async def fake_authenticate(plaintext: str) -> KeyRecord:
        return key

    monkeypatch.setattr(auth_mod, "authenticate", fake_authenticate)
    monkeypatch.setattr(budgets_mod, "db", fake)
    monkeypatch.setattr(costs_mod, "db", fake)
    monkeypatch.setattr(cascade_mod, "db", fake)
    monkeypatch.setattr(routes_proxy, "db", fake)
    monkeypatch.setattr(model_fallbacks, "db", fake)
    routes_proxy._in_flight.clear()

    def make(handler, models: dict[str, str]) -> TestClient:
        r = Router.with_mock(handler)
        for name, base in models.items():
            r.set_endpoints(name, [Endpoint(id=f"ep-{name}", model_name=name, base_url=base, model_type="chat")])
        monkeypatch.setattr(routes_proxy, "model_router", r)
        app = FastAPI()
        app.include_router(routes_proxy.router)
        return TestClient(app, raise_server_exceptions=False)

    return fake, key, make


def _h() -> dict[str, str]:
    return {"Authorization": "Bearer hllm-x"}


def _sse_bytes(chunks: list[dict]) -> bytes:
    return b"".join(b"data: " + json.dumps(c).encode() + b"\n\n" for c in chunks) + b"data: [DONE]\n\n"


def _delta(text: str) -> dict:
    return {"choices": [{"index": 0, "delta": {"content": text}}]}


def _budget(monkeypatch: pytest.MonkeyPatch, value: float) -> None:
    """Set the (not-yet-declared) failover_ttft_s knob for one test.

    The gate reads it via `getattr(settings, "failover_ttft_s", 45.0)` so it
    works before the concurrent config.py agent lands the real field. That
    makes the patch site conditional:
      * NOT declared yet (today) — pydantic v2's `__setattr__` refuses
        undeclared names, but a CLASS attribute is invisible to its field
        lookup, so the gate's getattr finds it;
      * declared (after config.py lands) — a class attribute no longer
        shadows a real field, so the value goes into the instance dict.
    Both spellings are undone by monkeypatch.
    """
    if "failover_ttft_s" in type(settings).model_fields:
        monkeypatch.setitem(settings.__dict__, "failover_ttft_s", value)
    else:
        monkeypatch.setattr(type(settings), "failover_ttft_s", value, raising=False)


def _stalling_then(events: list[str], *, keepalive_s: float = 0.05, release: asyncio.Event | None = None):
    """Async generator: keepalive comments every `keepalive_s`, then content.

    `events` collects markers so tests can assert how far the upstream got
    (e.g. that the wedged upstream was CLOSED rather than drained).
    """

    async def gen():
        try:
            for _ in range(5):
                events.append("keepalive")
                yield b": keep-alive\n\n"
                await asyncio.sleep(keepalive_s)
            events.append("data")
            yield b'data: {"choices":[{"index":0,"delta":{"content":"late"}}]}\n\n'
            await asyncio.sleep(keepalive_s)
            yield b"data: [DONE]\n\n"
        except asyncio.CancelledError:
            events.append("cancelled")
            raise
        finally:
            events.append("closed")
            if release is not None:
                release.set()

    return gen()


# ---------------------------------------------------------------------------
# (a) stall -> failover to the healthy second candidate
# ---------------------------------------------------------------------------


def test_stall_fails_over_to_next_candidate(env, monkeypatch: pytest.MonkeyPatch) -> None:
    """THE defect: the primary answers 200 headers and never sends a data
    line. The gate expires, the attempt is reported to the breaker, a
    0-token error row is settled, and the client gets the SECOND candidate's
    SSE — including X-PCAI-Fallback naming the model failed over FROM."""
    fake, _key_rec, make = env
    fake.chain_rows[PRIMARY] = [FALLBACK]
    _budget(monkeypatch, 0.2)
    upstream: dict[str, Any] = {"primary_calls": 0, "fallback_calls": 0}

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.host == "wedged-upstream":
            upstream["primary_calls"] += 1

            async def wedged():
                # 200 headers already sent; the body NEVER yields anything.
                await asyncio.Event().wait()
                yield b"never"

            return httpx2.Response(200, content=wedged(), headers={"content-type": "text/event-stream"})
        upstream["fallback_calls"] += 1
        return httpx2.Response(
            200,
            content=_sse_bytes([_delta("healthy answer")]),
            headers={"content-type": "text/event-stream"},
        )

    tc = make(handler, {PRIMARY: "http://wedged-upstream/v1", FALLBACK: "http://healthy-upstream/v1"})
    r = tc.post(
        "/v1/chat/completions",
        json={"model": PRIMARY, "messages": [{"role": "user", "content": "hi"}], "stream": True},
        headers=_h(),
    )

    assert r.status_code == 200, r.text
    assert b"healthy answer" in r.content, "the client must receive the FALLBACK candidate's stream"
    assert b"never" not in r.content
    assert r.headers.get("x-pcai-served-model") == FALLBACK
    assert r.headers.get("x-pcai-fallback") == PRIMARY
    assert r.headers.get("x-pcai-endpoint") == f"ep-{FALLBACK}"
    assert upstream["fallback_calls"] >= 1

    # the wedged endpoint was reported to its breaker (one failure report)
    ep = routes_proxy.model_router.pool(PRIMARY).endpoints[0]
    assert ep.consecutive_failures == 1, "the stall must count against the breaker"
    assert ep.last_error == "ttft budget exceeded"

    # ...and settled as a 0-token error row under the WEDGED model
    err_rows = fake.settles("error", model=PRIMARY)
    assert err_rows, "the stalled attempt must settle an error row"
    assert err_rows[0][3] == 0 and err_rows[0][4] == 0, "nothing was delivered — 0 tokens"
    # the serving model settles its own row
    assert fake.settles("ok", model=FALLBACK), "the fallback's stream must settle"


def test_stall_closes_the_wedged_upstream(env, monkeypatch: pytest.MonkeyPatch) -> None:
    """On budget expiry the upstream connection is CLOSED — the disconnect
    cancels the upstream's queued sequence (SGLang client-disconnect
    semantics), which is what makes the gate relieve queue pressure."""
    fake, _key_rec, make = env
    fake.chain_rows[PRIMARY] = [FALLBACK]
    _budget(monkeypatch, 0.2)
    released = asyncio.Event()
    events: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.host == "wedged-upstream":
            return httpx2.Response(
                200, content=_stalling_then(events, release=released), headers={"content-type": "text/event-stream"}
            )
        return httpx2.Response(200, content=_sse_bytes([_delta("ok")]), headers={"content-type": "text/event-stream"})

    tc = make(handler, {PRIMARY: "http://wedged-upstream/v1", FALLBACK: "http://healthy-upstream/v1"})
    r = tc.post(
        "/v1/chat/completions",
        json={"model": PRIMARY, "messages": [{"role": "user", "content": "hi"}], "stream": True},
        headers=_h(),
    )
    assert r.status_code == 200, r.text
    assert b"ok" in r.content
    assert "keepalive" in events, "the upstream emitted keepalives while queued (the observed wedge shape)"
    assert "closed" in events, "the wedged upstream stream must be closed on budget expiry"
    assert "data" not in events, "the gate must not wait for the upstream's late data"


# ---------------------------------------------------------------------------
# (b) keepalive comments do NOT satisfy the gate; prefix replay is exact
# ---------------------------------------------------------------------------


def test_keepalives_then_data_succeeds_with_identical_bytes(env, monkeypatch: pytest.MonkeyPatch) -> None:
    """A queued upstream that emits `: keep-alive` comments before its first
    data line is NOT a stall: the gate keeps waiting within budget and the
    client sees the upstream stream BYTE-FOR-BYTE — the peeked prefix is
    replayed, comments included."""
    _fake, _key_rec, make = env
    _budget(monkeypatch, 1.0)
    body = _sse_bytes([_delta("hi")])

    async def keepalive_then_data():
        yield b": keep-alive\n\n"
        await asyncio.sleep(0.05)
        yield b": keep-alive\n\n"
        await asyncio.sleep(0.05)
        yield body

    def gated_handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, content=keepalive_then_data(), headers={"content-type": "text/event-stream"})

    tc = make(gated_handler, {PRIMARY: "http://wedged-upstream/v1"})
    gated = tc.post(
        "/v1/chat/completions",
        json={"model": PRIMARY, "messages": [{"role": "user", "content": "hi"}], "stream": True},
        headers=_h(),
    )
    assert gated.status_code == 200, gated.text
    assert b"hi" in gated.content

    # control: the SAME stream with the gate OFF (budget 0 = disabled) — the
    # gated run's bytes must equal it exactly (prefix replay correctness).
    _budget(monkeypatch, 0.0)

    def control_handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, content=keepalive_then_data(), headers={"content-type": "text/event-stream"})

    tc2 = make(control_handler, {PRIMARY: "http://wedged-upstream/v1"})
    control = tc2.post(
        "/v1/chat/completions",
        json={"model": PRIMARY, "messages": [{"role": "user", "content": "hi"}], "stream": True},
        headers=_h(),
    )
    assert control.status_code == 200, control.text
    assert gated.content == control.content, "the gated stream must be byte-identical to the ungated one"
    assert gated.content.startswith(b": keep-alive\n\n"), "peeked comment bytes must reach the client"


def test_data_line_split_across_reads_still_gates(env, monkeypatch: pytest.MonkeyPatch) -> None:
    """A data line straddling two network reads still counts as progress
    (the peek uses the same line cursor as _relay_sse) and replays whole."""
    _fake, _key_rec, make = env
    _budget(monkeypatch, 1.0)

    async def split():
        yield b'data: {"choices":[{"index":0,"del'
        await asyncio.sleep(0.02)
        yield b'ta":{"content":"split"}}]}\n\ndata: [DONE]\n\n'

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, content=split(), headers={"content-type": "text/event-stream"})

    tc = make(handler, {PRIMARY: "http://wedged-upstream/v1"})
    r = tc.post(
        "/v1/chat/completions",
        json={"model": PRIMARY, "messages": [{"role": "user", "content": "hi"}], "stream": True},
        headers=_h(),
    )
    assert r.status_code == 200, r.text
    assert b"split" in r.content
    assert r.content.startswith(b'data: {"choices"'), "the split line must replay from its first byte"


# ---------------------------------------------------------------------------
# (c) immediate data: zero-overhead path
# ---------------------------------------------------------------------------


def test_immediate_data_is_byte_identical_to_control(env, monkeypatch: pytest.MonkeyPatch) -> None:
    """A normal (fast) upstream: the gate returns on the first data line and
    the stream is byte-identical to the ungated control."""
    _fake, _key_rec, make = env
    body = _sse_bytes([_delta("fast")])

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, content=body, headers={"content-type": "text/event-stream"})

    _budget(monkeypatch, 5.0)
    tc = make(handler, {PRIMARY: "http://wedged-upstream/v1"})
    gated = tc.post(
        "/v1/chat/completions",
        json={"model": PRIMARY, "messages": [{"role": "user", "content": "hi"}], "stream": True},
        headers=_h(),
    )
    assert gated.status_code == 200, gated.text
    assert gated.content == body

    _budget(monkeypatch, 0.0)
    tc2 = make(handler, {PRIMARY: "http://wedged-upstream/v1"})
    control = tc2.post(
        "/v1/chat/completions",
        json={"model": PRIMARY, "messages": [{"role": "user", "content": "hi"}], "stream": True},
        headers=_h(),
    )
    assert control.content == gated.content == body


# ---------------------------------------------------------------------------
# (d) budget <= 0 disables the gate entirely (legacy path)
# ---------------------------------------------------------------------------


def test_gate_disabled_keeps_the_stalled_candidate(env, monkeypatch: pytest.MonkeyPatch) -> None:
    """budget 0 = gate OFF: today's behavior. The client WAITS OUT the
    upstream's queue delay and is served by the SAME (wedged) candidate —
    no failover, no breaker report, no error row. The compat gate for the
    rollout: the gate is opt-in until the operator sets a budget."""
    fake, _key_rec, make = env
    fake.chain_rows[PRIMARY] = [FALLBACK]
    calls: dict[str, int] = {"primary": 0, "fallback": 0}

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.host == "wedged-upstream":
            calls["primary"] += 1

            async def slow_but_alive():
                # The queue stall: no bytes for 0.5s, then the stream flows.
                await asyncio.sleep(0.5)
                yield b'data: {"choices":[{"index":0,"delta":{"content":"late"}}]}\n\n'
                yield b"data: [DONE]\n\n"

            return httpx2.Response(200, content=slow_but_alive(), headers={"content-type": "text/event-stream"})
        calls["fallback"] += 1
        return httpx2.Response(
            200, content=_sse_bytes([_delta("fallback")]), headers={"content-type": "text/event-stream"}
        )

    models = {PRIMARY: "http://wedged-upstream/v1", FALLBACK: "http://healthy-upstream/v1"}

    # GATED (0.2s budget): the same stall IS failed over — the contrast that
    # proves the disabled case below is the legacy path, not a broken gate.
    _budget(monkeypatch, 0.2)
    gated = make(handler, models).post(
        "/v1/chat/completions",
        json={"model": PRIMARY, "messages": [{"role": "user", "content": "hi"}], "stream": True},
        headers=_h(),
    )
    assert gated.status_code == 200, gated.text
    assert b"fallback" in gated.content
    assert calls["fallback"] == 1

    # DISABLED (0): the primary is KEPT. The client receives the primary's
    # late bytes (in production it would sit in the 600s upstream timeout —
    # exactly the behavior the gate exists to change).
    _budget(monkeypatch, 0.0)
    errors_before = len(fake.settles("error", model=PRIMARY))
    tc = make(handler, models)
    r = tc.post(
        "/v1/chat/completions",
        json={"model": PRIMARY, "messages": [{"role": "user", "content": "hi"}], "stream": True},
        headers=_h(),
    )
    assert r.status_code == 200, r.text
    assert b"late" in r.content, "gate disabled: the wedged candidate's own stream is relayed"
    assert calls["fallback"] == 1, "gate disabled: the chain must NOT be walked"
    assert r.headers.get("x-pcai-served-model") == PRIMARY
    assert "x-pcai-fallback" not in {k.lower() for k in r.headers}
    ep = routes_proxy.model_router.pool(PRIMARY).endpoints[0]
    assert ep.consecutive_failures == 0, "gate disabled: no breaker report"
    assert len(fake.settles("error", model=PRIMARY)) == errors_before, "gate disabled: no TTFT error row"


# ---------------------------------------------------------------------------
# (e) stall with an EMPTY chain -> 502
# ---------------------------------------------------------------------------


def test_stall_with_empty_chain_is_502(env, monkeypatch: pytest.MonkeyPatch) -> None:
    """No candidate to fail over to: the walk ends in the same 502 the other
    pre-first-byte paths raise (and the slot is released)."""
    fake, key, make = env
    _budget(monkeypatch, 0.2)

    def handler(request: httpx2.Request) -> httpx2.Response:
        async def wedged():
            await asyncio.Event().wait()
            yield b"never"

        return httpx2.Response(200, content=wedged(), headers={"content-type": "text/event-stream"})

    tc = make(handler, {PRIMARY: "http://wedged-upstream/v1"})
    r = tc.post(
        "/v1/chat/completions",
        json={"model": PRIMARY, "messages": [{"role": "user", "content": "hi"}], "stream": True},
        headers=_h(),
    )
    assert r.status_code == 502, r.text
    assert fake.settles("error", model=PRIMARY), "the stalled attempt still settles"
    # the concurrency slot is handed back on the give-up path
    assert routes_proxy._in_flight.get(key.id, 0) == 0, "the 502 path must release the slot"


# ---------------------------------------------------------------------------
# (f) [DONE]-only stream passes the gate
# ---------------------------------------------------------------------------


def test_done_only_stream_passes_the_gate(env, monkeypatch: pytest.MonkeyPatch) -> None:
    """`[DONE]` is a data line: a stream that ends instantly is not stalled
    and must not be failed over (it would otherwise look like a wedge)."""
    fake, _key_rec, make = env
    fake.chain_rows[PRIMARY] = [FALLBACK]
    _budget(monkeypatch, 0.5)
    calls: dict[str, int] = {"fallback": 0}

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.host == "wedged-upstream":
            return httpx2.Response(200, content=b"data: [DONE]\n\n", headers={"content-type": "text/event-stream"})
        calls["fallback"] += 1
        return httpx2.Response(
            200, content=_sse_bytes([_delta("should not be used")]), headers={"content-type": "text/event-stream"}
        )

    tc = make(handler, {PRIMARY: "http://wedged-upstream/v1", FALLBACK: "http://healthy-upstream/v1"})
    r = tc.post(
        "/v1/chat/completions",
        json={"model": PRIMARY, "messages": [{"role": "user", "content": "hi"}], "stream": True},
        headers=_h(),
    )
    assert r.status_code == 200, r.text
    assert r.content == b"data: [DONE]\n\n"
    assert calls["fallback"] == 0, "[DONE] is progress — the gate must not fail over"
    assert not fake.settles("error", model=PRIMARY)
    ep = routes_proxy.model_router.pool(PRIMARY).endpoints[0]
    assert ep.consecutive_failures == 0


# ---------------------------------------------------------------------------
# unit-level: the peek helper itself
# ---------------------------------------------------------------------------


async def test_peek_ignores_comment_and_event_lines() -> None:
    """Comment (`:`) and `event:` lines do not satisfy the gate; only a
    stripped `data:` line does. Returns EVERY byte read so far (comments
    included — they must still reach the client) + a live iterator to
    continue."""
    from pcai_llm.api import routes_proxy as rp

    async def stream():
        yield b": keep-alive\n\n"
        yield b"event: ping\n\n"
        yield b"\n"
        yield b'data: {"ok":1}\n\n'
        yield b"data: [DONE]\n\n"

    resp = httpx2.Response(200, content=stream(), headers={"content-type": "text/event-stream"})
    peeked, chunks = await rp._peek_first_data_line(resp, 1.0)
    assert chunks is not None, "the gate must keep waiting through comments/events"
    assert peeked == b': keep-alive\n\nevent: ping\n\n\ndata: {"ok":1}\n\n'
    rest = b""
    async for chunk in chunks:
        rest += chunk
    assert rest == b"data: [DONE]\n\n"
    await resp.aclose()


async def test_peek_returns_none_on_budget_expiry() -> None:
    """A stream that never produces a data line expires the budget and
    reports the stall as (b'', None) — the caller's failover signal."""
    from pcai_llm.api import routes_proxy as rp

    async def stalled():
        while True:
            yield b": keep-alive\n\n"
            await asyncio.sleep(0.02)

    resp = httpx2.Response(200, content=stalled(), headers={"content-type": "text/event-stream"})
    peeked, chunks = await rp._peek_first_data_line(resp, 0.2)
    assert peeked == b"" and chunks is None
    await resp.aclose()


async def test_peek_treats_clean_end_without_data_as_not_stalled() -> None:
    """A 2xx that ENDS with no data line (empty body) is not a stall: the
    bytes read so far are handed back for the verbatim relay."""
    from pcai_llm.api import routes_proxy as rp

    async def empty():
        return
        yield b""

    resp = httpx2.Response(200, content=empty(), headers={"content-type": "text/event-stream"})
    peeked, chunks = await rp._peek_first_data_line(resp, 1.0)
    assert chunks is not None, "an already-finished stream is not a wedge"
    rest = b""
    async for chunk in chunks:
        rest += chunk
    assert peeked + rest == b""
    await resp.aclose()


def test_disabled_gate_is_byte_identical_to_no_peek(env, monkeypatch: pytest.MonkeyPatch) -> None:
    """With budget <= 0 the peek never runs: `_peek_first_data_line` is not
    even called (the critical compat gate — the legacy path must not pay a
    single extra read)."""
    _fake, _key_rec, make = env
    _budget(monkeypatch, 0.0)
    calls: list[float] = []
    real = routes_proxy._peek_first_data_line

    async def spy(resp, budget_s):
        calls.append(budget_s)
        return await real(resp, budget_s)

    monkeypatch.setattr(routes_proxy, "_peek_first_data_line", spy)

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200, content=_sse_bytes([_delta("legacy")]), headers={"content-type": "text/event-stream"}
        )

    tc = make(handler, {PRIMARY: "http://wedged-upstream/v1"})
    r = tc.post(
        "/v1/chat/completions",
        json={"model": PRIMARY, "messages": [{"role": "user", "content": "hi"}], "stream": True},
        headers=_h(),
    )
    assert r.status_code == 200, r.text
    assert b"legacy" in r.content
    assert calls == [], "budget 0 must disable the gate ENTIRELY (no peek at all)"
