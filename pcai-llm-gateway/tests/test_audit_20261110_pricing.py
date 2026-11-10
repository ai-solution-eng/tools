"""P10 — unit-aware pricing (audit 2026-11-10 zone P10).

What is pinned here:

* costs.compute_units: a unit mismatch is REPORTED (WARNING) and the row
  prices NULL — never a silent wrong-rate conversion through the token rate.
* settle() pricing is gated on the STORED model_costs.unit, never on the
  estimate tag (cross-validation wave 2): stored 'token' keeps the legacy
  token arithmetic (every pre-P10 row, plain chat); any other stored unit
  prices over the tag's EFFECTIVE unit ('char_estimate'->'char',
  'second_estimate'->'second', 'token_estimate'->'token'), so a per-1M-SECOND
  rate can never ride the per-1M-TOKEN arithmetic (the silent 3x ASR
  over-bill) and a char estimate on a second-priced model ledgers NULL
  (no fabricated chars->seconds conversion). A REAL unit tag always prices
  through compute_units. A billable_units quantity past NUMERIC(18,6)
  ledgers the row UNPRICED (quantity and cost NULL) instead of aborting
  the INSERT and losing the immutable row.
* /v1/audio/speech: a REAL duration (upstream response header) bills per
  SECOND (unit_kind='second', cost from the per-1M-second rate); without one
  the legacy char proxy rides, tagged unit_kind='char_estimate' with
  billable_units = the char count.
* /v1/audio/transcriptions: an explicit duration hint (?duration=, then
  X-PCAI-Duration) beats the byte proxy but is CLAMPED into the upload's
  compression envelope ([byte_proxy/16, byte_proxy*16] — a compressed
  upload is never more than ~16x smaller than raw PCM and never larger;
  the unbounded hint billed 0.001 s as free ASR and 1e8 s as 1500 USD),
  and an UPSTREAM-reported duration beats any hint (the server measured
  it; the caller only claims it).
* an unpriced model (no model_costs row) still ledgers cost NULL, unit-aware
  or not.
* chat pricing is BYTE-IDENTICAL to the pre-P10 arithmetic: same inputs ->
  same Decimal, exact quantized values pinned.
* the decision model's own call settles a status='routing' row under the
  CALLING key when routing_charge_enabled, and nothing when disabled.
* schema idempotency: pinned by SUBSTRING/DO-block shape (no live Postgres in
  this suite — the same posture tests/test_audit_20261110_ops.py uses): every
  new DDL line must be an IF NOT EXISTS / DO-block-gated statement so a
  second boot and a pre-migration DB both apply it cleanly.

No live Postgres: the module-db monkeypatch pattern from tests/test_audio_routes.py.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from decimal import Decimal
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
from pcai_llm.core import decision_routing as dr
from pcai_llm.db.schema import SCHEMA_SQL
from pcai_llm.routing.router import Endpoint, Router

VOICE = "p10-voice"
DM = "p10-decision"
AUTH = {"Authorization": "Bearer hllm-x"}


class FakeDB:
    """Budget/rate stubs; records every execute() as (sql, args)."""

    def __init__(self, cost_rows: list[dict[str, Any]] | None = None) -> None:
        self.settled: list[tuple[str, tuple]] = []
        self.cost_rows = cost_rows or []

    async def fetch(self, sql: str, *args: Any):
        if "FROM model_costs" in sql:
            return self.cost_rows
        return []

    async def fetchrow(self, sql: str, *args: Any):
        if "profiles" in sql:
            return None
        return {"requests": 1, "tokens": 10}

    async def execute(self, sql: str, *args: Any):
        self.settled.append((sql, args))
        return "INSERT 0 1"

    def ledger_rows(self) -> list[tuple]:
        """Only the usage_ledger INSERTs (settle also issues window updates)."""
        return [args for sql, args in self.settled if "INSERT INTO usage_ledger" in sql]


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


def _price_cache(monkeypatch: pytest.MonkeyPatch, rows: dict[str, dict[str, Any]]) -> None:
    """Seed the costs cache directly (the 60s TTL cache is process-global)."""
    monkeypatch.setattr(costs_mod, "_cache", rows)
    monkeypatch.setattr(costs_mod, "_loaded_at", time.monotonic())
    monkeypatch.setattr(costs_mod, "_mismatch_warned", set())


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch):
    """Fake auth + fake db + one voice pool and one decision pool (mock upstream)."""
    key = _key()

    import pcai_llm.api.auth as auth_mod

    async def fake_authenticate(plaintext: str) -> KeyRecord:
        return key

    monkeypatch.setattr(auth_mod, "authenticate", fake_authenticate)

    fake_db = FakeDB()
    monkeypatch.setattr(budgets_mod, "db", fake_db)
    monkeypatch.setattr(cascade_mod, "db", fake_db)
    monkeypatch.setattr(costs_mod, "db", fake_db)

    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        if request.url.path.endswith("/audio/speech"):
            return httpx2.Response(200, content=b"RIFF-WAV-BYTES", headers={"Content-Type": "audio/wav"})
        if request.url.path.endswith("/systemone"):
            return httpx2.Response(
                200,
                json={
                    "answers": {
                        "answer": {
                            "type": "choice",
                            "choice": "a",
                            "probabilities": {"a": 0.9, "b": 0.1},
                            "confidence": 0.9,
                        }
                    },
                    "usage": {"input_tokens": 42, "output_tokens": 0},
                },
            )
        return httpx2.Response(200, json={"text": "hello world"})

    mock_router = Router.with_mock(handler)
    mock_router.set_endpoints(
        VOICE, [Endpoint(id="vp1", model_name=VOICE, base_url="http://fake-a/v1", model_type="voice")]
    )
    mock_router.set_endpoints(DM, [Endpoint(id="dm1", model_name=DM, base_url="http://fake-d/v1", model_type="chat")])
    monkeypatch.setattr(routes_proxy, "model_router", mock_router)

    app = FastAPI()
    app.include_router(routes_proxy.router)
    return TestClient(app, raise_server_exceptions=False), fake_db, seen, key, monkeypatch


# ---------------------------------------------------------------------------
# 1. unit mismatch is REPORTED, not silently mis-billed
# ---------------------------------------------------------------------------


def test_compute_units_mismatch_is_reported_and_not_billed(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A model priced per 'token' whose route bills 'second' must price NULL
    (never a converted number) and say WHY in a WARNING."""
    _price_cache(
        monkeypatch,
        # wrong: a voice model still on the pre-P10 token default
        {
            VOICE: {
                "model_name": VOICE,
                "input_per_m": 2.0,
                "output_per_m": 0.0,
                "cached_per_m": 0.0,
                "unit": "token",
            }
        },
    )
    with caplog.at_level("WARNING", logger="pcai_llm.core.costs"):
        cost = asyncio.run(costs_mod.compute_units(VOICE, 120.0, "second"))
    assert cost is None, "a mismatched unit must NOT be priced through the token rate"
    assert "priced per token" in caplog.text and "second" in caplog.text
    # once per (model, unit) per cache generation, not once per request:
    with caplog.at_level("WARNING", logger="pcai_llm.core.costs"):
        asyncio.run(costs_mod.compute_units(VOICE, 60.0, "second"))
    assert caplog.text.count("priced per token") == 1

    # the matching unit prices over the MEASURED quantity, at input_per_m as
    # the per-1M-UNIT rate: 120 s / 1e6 * 2.0 = 0.000240
    costs_mod._cache[VOICE]["unit"] = "second"
    cost = asyncio.run(costs_mod.compute_units(VOICE, 120.0, "second"))
    assert cost == Decimal("0.000240")


def test_settle_mismatch_row_is_priced_null_but_fully_tagged(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """settle(unit_kind='second') on a token-priced model: the ledger row keeps
    its billable_units/unit_kind evidence and carries cost NULL — the
    'reported, not silently mis-billed' contract, end to end."""
    _price_cache(
        monkeypatch,
        {
            VOICE: {
                "model_name": VOICE,
                "input_per_m": 2.0,
                "output_per_m": 0.0,
                "cached_per_m": 0.0,
                "unit": "token",
            }
        },
    )
    fake_db = FakeDB()
    monkeypatch.setattr(budgets_mod, "db", fake_db)
    kid = uuid.uuid4()
    with caplog.at_level("WARNING", logger="pcai_llm.core.costs"):
        asyncio.run(
            budgets_mod.settle(
                kid, VOICE, "audio.speech", 2, 0, "ok", 5, "req-mismatch", billable_units=12.5, unit_kind="second"
            )
        )
    row = fake_db.ledger_rows()[0]
    assert row[8] == "ok"
    assert row[18] == 12.5  # billable_units — the evidence stays on the row
    assert row[19] == "second"
    assert row[7] is None, "a mismatched unit must ledger cost NULL"
    assert "MISCONFIGURED unit" in caplog.text or "priced per token" in caplog.text


# ---------------------------------------------------------------------------
# 2. TTS: duration header bills seconds; without one, the tagged char estimate
# ---------------------------------------------------------------------------


def test_tts_with_duration_header_bills_seconds(env) -> None:
    tc, fake_db, seen, _, monkeypatch = env
    _price_cache(
        monkeypatch,
        {
            VOICE: {
                "model_name": VOICE,
                "input_per_m": 2.0,
                "output_per_m": 0.0,
                "cached_per_m": 0.0,
                "unit": "second",
            }
        },
    )

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(
            200,
            content=b"RIFF",
            headers={"Content-Type": "audio/wav", "X-Audio-Duration": "12.5"},
        )

    # This test needs a duration-REPORTING upstream: rebuild the mock router
    # with its own handler (the env fixture's TTS response carries no headers).
    mock_router = Router.with_mock(handler)
    mock_router.set_endpoints(
        VOICE, [Endpoint(id="vp1", model_name=VOICE, base_url="http://fake-a/v1", model_type="voice")]
    )
    monkeypatch.setattr(routes_proxy, "model_router", mock_router)

    body = json.dumps({"model": VOICE, "input": "hello world"}).encode()
    r = tc.post("/v1/audio/speech", content=body, headers={**AUTH, "Content-Type": "application/json"})
    assert r.status_code == 200, r.text
    rows = fake_db.ledger_rows()
    assert len(rows) == 1
    row = rows[0]
    assert row[2] == "audio.speech"
    assert row[8] == "ok"
    assert row[18] == 12.5, "a REAL duration must bill the measured seconds"
    assert row[19] == "second"
    # 12.5 s / 1e6 * 2.0 USD per 1M seconds = 0.000025 — priced from the
    # MEASURED quantity, not the char proxy.
    assert row[7] == Decimal("0.000025")


def test_tts_without_duration_bills_tagged_char_estimate(env, monkeypatch: pytest.MonkeyPatch) -> None:
    """A token-PRICED model (stored unit 'token' — every pre-P10 row) keeps the
    legacy token arithmetic on its tagged char estimate: the gate reads the
    STORED unit, never the tag, and the rate IS per-1M-token. (Cross-validation
    wave 2 flagged this test as pinning the token arithmetic; the hardened
    settle() confirms it — but ONLY for a token-stored model. The
    second-stored variants live in the tests below.)"""
    tc, fake_db, _, _, mp = env
    _price_cache(
        mp,
        {
            VOICE: {
                "model_name": VOICE,
                "input_per_m": 1.0,
                "output_per_m": 0.0,
                "cached_per_m": 0.0,
                "unit": "token",
            }
        },
    )
    body = json.dumps({"model": VOICE, "input": "hello world"}).encode()
    r = tc.post("/v1/audio/speech", content=body, headers={**AUTH, "Content-Type": "application/json"})
    assert r.status_code == 200, r.text
    row = fake_db.ledger_rows()[0]
    est = len("hello world") // settings.audio_tts_chars_per_token
    assert row[3] == est  # the token columns keep the (gated) estimate
    assert row[4] == 0
    assert row[18] == float(len("hello world")), "billable_units = the PROVENANCE quantity (chars)"
    assert row[19] == "char_estimate"
    # stored unit 'token' -> the LEGACY token arithmetic (the rate IS per-1M-
    # token, so this is dimensionally honest): est tokens at the token rate.
    assert row[7] == Decimal(est) / Decimal(1_000_000) * Decimal("1.0")


# ---------------------------------------------------------------------------
# 3. ASR: the duration hint wins over the byte proxy
# ---------------------------------------------------------------------------


def test_asr_duration_hint_wins_over_byte_proxy(env) -> None:
    tc, fake_db, _, _, monkeypatch = env
    _price_cache(
        monkeypatch,
        {
            VOICE: {
                "model_name": VOICE,
                "input_per_m": 2.0,
                "output_per_m": 0.0,
                "cached_per_m": 0.0,
                "unit": "second",
            }
        },
    )
    # ~96 KiB: byte proxy ~3 s, so the 30 s hint sits INSIDE the compression
    # envelope (byte_proxy*16 ~ 49 s) and is honored rather than clamped.
    audio = bytes(range(256)) * 384
    boundary = "----p10-boundary"
    body = (
        (f"--{boundary}\r\n".encode() + b'Content-Disposition: form-data; name="file"; filename="clip.wav"\r\n\r\n')
        + audio
        + f"\r\n--{boundary}--\r\n".encode()
    )
    ctype = f"multipart/form-data; boundary={boundary}"

    r = tc.post(
        "/v1/audio/transcriptions",
        content=body,
        headers={**AUTH, "Content-Type": ctype},
        params={"model": VOICE, "duration": "30"},
    )
    assert r.status_code == 200, r.text
    row = fake_db.ledger_rows()[0]
    assert row[2] == "audio.transcriptions"
    assert row[18] == 30.0, "the explicit ?duration= hint must beat the byte proxy"
    assert row[19] == "second"
    assert row[7] == Decimal("0.000060")  # 30 / 1e6 * 2.0


def test_asr_header_hint_used_when_query_absent(env) -> None:
    tc, fake_db, _, _, monkeypatch = env
    _price_cache(monkeypatch, {})  # no model_costs rows at all
    boundary = "----p10-boundary"
    body = (
        f"--{boundary}\r\n".encode()
        + b'Content-Disposition: form-data; name="file"; filename="clip.wav"\r\n\r\n'
        + b"\x00" * 32000
        + f"\r\n--{boundary}--\r\n".encode()
    )
    ctype = f"multipart/form-data; boundary={boundary}"
    r = tc.post(
        "/v1/audio/transcriptions",
        content=body,
        headers={**AUTH, "Content-Type": ctype, "X-PCAI-Duration": "7.5"},
        params={"model": VOICE},
    )
    assert r.status_code == 200, r.text
    row = fake_db.ledger_rows()[0]
    assert row[18] == 7.5
    assert row[19] == "second"
    # UNPRICED model: cost NULL even on a real-duration row (case 4 rides here)
    assert row[7] is None


def test_asr_byte_proxy_fallback_is_tagged(env, monkeypatch: pytest.MonkeyPatch) -> None:
    """No hint, no upstream header: the byte proxy rides TAGGED (second_estimate)
    and — on a correctly second-priced model — bills by SECONDS, not by the
    token estimate. This is the 3x regression the wave-2 review measured: the
    estimate tag used to fall to the legacy token arithmetic, applying the
    per-1M-SECOND rate to est_tokens = 3 tokens/second (30 s billed 0.001350
    at a 15 USD/1M s rate instead of the honest 0.000450). The old version of
    this test never priced anything (unpriced model) so it could not see it."""
    tc, fake_db, _, _, mp = env
    _price_cache(
        mp,
        {
            VOICE: {
                "model_name": VOICE,
                "input_per_m": 15.0,
                "output_per_m": 0.0,
                "cached_per_m": 0.0,
                "unit": "second",
            }
        },
    )
    boundary = "----p10-boundary"
    body = (
        f"--{boundary}\r\n".encode()
        + b'Content-Disposition: form-data; name="file"; filename="clip.wav"\r\n\r\n'
        + b"\x00" * 64000  # 2 "seconds" of raw PCM at the default divisor
        + f"\r\n--{boundary}--\r\n".encode()
    )
    ctype = f"multipart/form-data; boundary={boundary}"
    r = tc.post(
        "/v1/audio/transcriptions",
        content=body,
        headers={**AUTH, "Content-Type": ctype},
        params={"model": VOICE},
    )
    assert r.status_code == 200, r.text
    row = fake_db.ledger_rows()[0]
    est_seconds = max(1.0, len(body) / settings.audio_asr_bytes_per_second)
    # billable_units = the DERIVED seconds (whole multipart body at the divisor)
    assert row[18] == pytest.approx(est_seconds)
    assert row[19] == "second_estimate"
    # billed by SECONDS at the per-1M-second rate — NOT by the token estimate
    # (which is 3 tokens/second and would over-bill ~3x):
    expected = (Decimal(str(est_seconds)) / Decimal(1_000_000) * Decimal("15.0")).quantize(Decimal("0.000001"))
    assert row[7] == expected
    token_arithmetic = (
        Decimal(int(est_seconds * settings.audio_asr_tokens_per_second)) / Decimal(1_000_000) * Decimal("15.0")
    ).quantize(Decimal("0.000001"))
    assert row[7] != token_arithmetic, "the per-second rate must not ride the token arithmetic"


# ---------------------------------------------------------------------------
# 4. an unpriced model still ledgers NULL cost
# ---------------------------------------------------------------------------


def test_unpriced_model_ledgers_null_cost(monkeypatch: pytest.MonkeyPatch) -> None:
    _price_cache(monkeypatch, {})  # no model_costs rows at all
    fake_db = FakeDB()
    monkeypatch.setattr(budgets_mod, "db", fake_db)
    kid = uuid.uuid4()
    # legacy token row
    asyncio.run(budgets_mod.settle(kid, "ghost", "chat", 100, 50, "ok", 5, "req-a"))
    # unit-aware row
    asyncio.run(
        budgets_mod.settle(
            kid, "ghost", "audio.speech", 2, 0, "ok", 5, "req-b", billable_units=12.5, unit_kind="second"
        )
    )
    rows = fake_db.ledger_rows()
    assert rows[0][7] is None
    assert rows[0][18] is None and rows[0][19] is None, "legacy rows keep the NULL/NULL shape"
    assert rows[1][7] is None, "unpriced + unit-aware still ledgers NULL"
    assert rows[1][18] == 12.5 and rows[1][19] == "second"


# ---------------------------------------------------------------------------
# 5. chat pricing is byte-identical to before
# ---------------------------------------------------------------------------


def test_chat_pricing_byte_identical(monkeypatch: pytest.MonkeyPatch) -> None:
    """costs.compute() must keep the EXACT pre-P10 arithmetic: the same rates
    and token counts produce the same quantized Decimal, with the unit column
    ignored entirely on the token path."""
    _price_cache(
        monkeypatch,
        {
            "chat-model": {
                "model_name": "chat-model",
                "input_per_m": 1.5,
                "output_per_m": 2.5,
                "cached_per_m": 0.25,
                "unit": "token",
            }
        },
    )
    compute = costs_mod.compute
    # uncached 1145 @1.5 + 567 @2.5 + 89 @0.25 (all /1e6) = 0.00315725 -> 0.003157
    assert asyncio.run(compute("chat-model", 1234, 567, 89)) == Decimal("0.003157")
    # cache-only: 0 uncached + 100 cached @0.25 /1e6 = 0.000025
    assert asyncio.run(compute("chat-model", 100, 0, 100)) == Decimal("0.000025")
    # zero rates price zero — never None
    costs_mod._cache["chat-model"]["input_per_m"] = 0.0
    costs_mod._cache["chat-model"]["output_per_m"] = 0.0
    costs_mod._cache["chat-model"]["cached_per_m"] = 0.0
    assert asyncio.run(compute("chat-model", 10**9, 10**9, 0)) == Decimal("0.000000")
    # unpriced + fallback off (the default) = None, as always
    assert asyncio.run(compute("ghost", 1, 1, 0)) is None


def test_chat_settle_row_shape_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """A plain chat settle writes NULL billable_units/unit_kind — the INSERT
    args tuple only APPENDS (the documented positional contract)."""
    _price_cache(
        monkeypatch,
        {
            "chat-model": {
                "model_name": "chat-model",
                "input_per_m": 1.5,
                "output_per_m": 2.5,
                "cached_per_m": 0.25,
                "unit": "token",
            }
        },
    )
    fake_db = FakeDB()
    monkeypatch.setattr(budgets_mod, "db", fake_db)
    asyncio.run(budgets_mod.settle(uuid.uuid4(), "chat-model", "chat", 1234, 567, "ok", 5, "req-c", cached_tokens=89))
    row = fake_db.ledger_rows()[0]
    assert row[7] == Decimal("0.003157")
    assert row[18] is None and row[19] is None


# ---------------------------------------------------------------------------
# 6. the decision call settles a 'routing' row when enabled, none when disabled
# ---------------------------------------------------------------------------


def test_decision_call_settles_routing_row_when_enabled(env, monkeypatch: pytest.MonkeyPatch) -> None:
    _tc, fake_db, _seen, key, mp = env
    _price_cache(
        mp,
        {
            DM: {
                "model_name": DM,
                "input_per_m": 1.0,
                "output_per_m": 0.0,
                "cached_per_m": 0.0,
                "unit": "token",
            }
        },
    )
    caller = routes_proxy._decision_caller(DM, {"prompt": "pick"}, key_id=key.id, request_id="req-r1")
    assert caller is not None

    async def drive() -> None:
        await dr.decide(
            caller,
            state="{}",
            instructions="pick",
            candidates=["a", "b"],
            min_confidence=0.6,
            timeout_ms=1000,
        )

    asyncio.run(drive())
    routing_rows = [r for r in fake_db.ledger_rows() if r[8] == "routing"]
    assert len(routing_rows) == 1, fake_db.ledger_rows()
    row = routing_rows[0]
    assert row[0] == key.id, "the routing row settles under the CALLING key"
    assert row[1] == DM
    assert row[2] == "routing"
    assert row[3] == 42 and row[4] == 0, "real upstream usage when reported"
    assert row[7] == Decimal("0.000042"), "the routing row IS priced (42/1e6 @ 1.0)"
    assert row[18] is None and row[19] is None  # measured usage: untagged


def test_decision_call_without_usage_settles_tagged_estimate(env, monkeypatch: pytest.MonkeyPatch) -> None:
    _tc, fake_db, seen, key, mp = env
    _price_cache(
        mp,
        {DM: {"model_name": DM, "input_per_m": 1.0, "output_per_m": 0.0, "cached_per_m": 0.0, "unit": "token"}},
    )

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        # the /systemone response carries NO usage block
        return httpx2.Response(
            200,
            json={
                "answers": {
                    "answer": {
                        "type": "choice",
                        "choice": "a",
                        "probabilities": {"a": 0.9, "b": 0.1},
                        "confidence": 0.9,
                    }
                }
            },
        )

    mock_router = Router.with_mock(handler)
    mock_router.set_endpoints(DM, [Endpoint(id="dm1", model_name=DM, base_url="http://fake-d/v1", model_type="chat")])
    mp.setattr(routes_proxy, "model_router", mock_router)

    caller = routes_proxy._decision_caller(DM, {"prompt": "pick"}, key_id=key.id, request_id="req-r2")
    assert caller is not None, "the routing-enabled caller must be constructible"

    async def drive() -> None:
        await dr.decide(
            caller, state="{}", instructions="pick", candidates=["a", "b"], min_confidence=0.6, timeout_ms=1000
        )

    asyncio.run(drive())
    row = next(r for r in fake_db.ledger_rows() if r[8] == "routing")
    sent = json.loads(seen[-1].content)
    est = len(json.dumps(sent)) // 4
    assert row[3] == est
    assert row[19] == "token_estimate"
    assert row[18] == float(est)
    assert row[7] == Decimal(est) / Decimal(1_000_000)


def test_decision_call_settles_nothing_when_disabled(env, monkeypatch: pytest.MonkeyPatch) -> None:
    _tc, fake_db, seen, key, mp = env
    _price_cache(
        mp,
        {DM: {"model_name": DM, "input_per_m": 1.0, "output_per_m": 0.0, "cached_per_m": 0.0, "unit": "token"}},
    )
    mp.setattr(settings, "routing_charge_enabled", False)
    caller = routes_proxy._decision_caller(DM, {"prompt": "pick"}, key_id=key.id, request_id="req-r3")
    assert caller is not None, "the routing caller must be constructible even when charging is off"

    async def drive() -> None:
        await dr.decide(
            caller, state="{}", instructions="pick", candidates=["a", "b"], min_confidence=0.6, timeout_ms=1000
        )

    asyncio.run(drive())
    # The knob gates only the LEDGER row: the decision call itself still ran.
    assert len(seen) == 1 and seen[0].url.path.endswith("/systemone")
    assert fake_db.ledger_rows() == [], "routing_charge_enabled=False must settle NO routing row"


# ---------------------------------------------------------------------------
# 7. schema is idempotent on a second apply (substring/DO-block pin)
# ---------------------------------------------------------------------------


def test_schema_unit_column_is_idempotent_ddl() -> None:
    """Every P10 DDL line must survive a SECOND boot and a pre-migration DB:
    ADD COLUMN IF NOT EXISTS for columns, a DO-block-gated CHECK add for the
    unit vocabulary. (Substring/DO-block pin — this suite has no live
    Postgres; the statements' idempotent FORM is the contract.)"""
    assert "ALTER TABLE model_costs ADD COLUMN IF NOT EXISTS unit TEXT NOT NULL DEFAULT 'token';" in SCHEMA_SQL
    assert "ck_model_costs_unit" in SCHEMA_SQL
    assert "CHECK (unit IN ('token','char','second','image','request')) NOT VALID" in SCHEMA_SQL
    # the gate and the ADD CONSTRAINT live in the SAME DO-block (order matters:
    # the check must wrap the add, not merely precede it)
    gate_at = SCHEMA_SQL.index("pg_get_constraintdef")
    add_at = SCHEMA_SQL.index("ADD CONSTRAINT ck_model_costs_unit")
    begin_at = SCHEMA_SQL.rindex("DO $$", 0, gate_at)
    end_block = SCHEMA_SQL.index("END $$;", gate_at)
    assert begin_at < gate_at < add_at < end_block, "the unit CHECK add must sit inside its DO-block gate"


def test_schema_unit_check_is_not_valid_and_definition_gated() -> None:
    """Cross-validation wave 2: the unit CHECK must be NOT VALID and the DO
    block must gate on the constraint DEFINITION (pg_get_constraintdef), not
    mere existence.

    NOT VALID: SCHEMA_SQL is ONE implicit transaction — a VALID add full-scans
    model_costs inside boot, so ONE pre-existing out-of-vocabulary row
    (unit='seconds' from an older build) would roll back the ENTIRE boot DDL
    and crash-loop every replica. (Same discipline as ck_ledger_status,
    budgets_period_check, guardrails_engine_check.)

    Definition-gated: a constraint left by an older vocabulary would otherwise
    survive boot and reject the new singular vocabulary at write time (500).
    """
    do_start = SCHEMA_SQL.index("DO $$", SCHEMA_SQL.index("ck_model_costs_unit") - 2000)
    block = SCHEMA_SQL[do_start : SCHEMA_SQL.index("END $$;", do_start)]
    assert "ADD CONSTRAINT ck_model_costs_unit" in block
    assert ") NOT VALID;" in block, "the unit CHECK must be added NOT VALID"
    assert "pg_get_constraintdef" in block, "the gate must compare the constraint DEFINITION"
    assert "DROP CONSTRAINT ck_model_costs_unit" in block, "a stale-vocabulary constraint must be dropped and re-added"
    # the vocabulary the re-added constraint enforces (singular forms)
    assert "CHECK (unit IN ('token','char','second','image','request'))" in block


def test_schema_ledger_unit_columns_are_idempotent_ddl() -> None:
    assert "ALTER TABLE usage_ledger ADD COLUMN IF NOT EXISTS billable_units NUMERIC(18,6);" in SCHEMA_SQL
    assert "ALTER TABLE usage_ledger ADD COLUMN IF NOT EXISTS unit_kind TEXT;" in SCHEMA_SQL
    # 'routing' joined the status vocabulary (the routing row must INSERT at all)
    assert "'denied','routing'" in SCHEMA_SQL


def test_schema_status_vocabulary_matches_partition_script() -> None:
    """The partition runbook stamps ck_ledger_status verbatim from SCHEMA_SQL
    (pinned by tests/test_deploy_contract.py); P10's 'routing' must not break
    that parity."""
    import re
    from pathlib import Path

    schema_m = re.search(r"ck_ledger_status\s+CHECK \(status IN \(([^)]+)\)\) NOT VALID", SCHEMA_SQL)
    assert schema_m, "ck_ledger_status not found in SCHEMA_SQL"
    partition = Path(__file__).resolve().parents[1].joinpath("scripts/partition_ledger.py").read_text(encoding="utf-8")
    script_m = re.search(r"ck_ledger_status\s+CHECK \(status IN \(([^)]+)\)\) NOT VALID", partition)
    assert script_m, "ck_ledger_status not found in scripts/partition_ledger.py"
    assert re.findall(r"'([^']+)'", script_m.group(1)) == re.findall(r"'([^']+)'", schema_m.group(1))
    # and the runbook's COLUMNS block carries the two new columns IN SCHEMA ORDER
    cols_m = re.search(r'COLUMNS = """(.*?)"""', partition, re.DOTALL)
    assert cols_m
    cols = re.findall(r"^\s*(\w+)\s+[A-Z]", cols_m.group(1), re.MULTILINE)
    assert cols[-2:] == ["billable_units", "unit_kind"]


# ---------------------------------------------------------------------------
# 8. wave-2 cross-validation regressions (2026-11-10)
# ---------------------------------------------------------------------------


def test_second_estimate_settle_bills_seconds_not_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    """THE 3x regression, settle-level: a voice model priced per SECOND whose
    row carries the ASR byte-proxy tag (second_estimate) must bill the
    ESTIMATED SECONDS through the per-1M-second rate — the tag used to fall
    through to the legacy token arithmetic and bill est_tokens (3 tokens/second)
    instead: 30 s billed 0.001350 at 15 USD/1M s instead of the honest 0.000450,
    exactly 3.0x. (TTS measured the same defect at 3.75x on its char tag.)"""
    _price_cache(
        monkeypatch,
        {
            VOICE: {
                "model_name": VOICE,
                "input_per_m": 15.0,
                "output_per_m": 0.0,
                "cached_per_m": 0.0,
                "unit": "second",
            }
        },
    )
    fake_db = FakeDB()
    monkeypatch.setattr(budgets_mod, "db", fake_db)
    asyncio.run(
        budgets_mod.settle(
            uuid.uuid4(),
            VOICE,
            "audio.transcriptions",
            90,
            0,
            "ok",
            5,
            "req-3x",
            billable_units=30.0,
            unit_kind="second_estimate",
        )
    )
    row = fake_db.ledger_rows()[0]
    assert row[19] == "second_estimate"
    assert row[7] == Decimal("0.000450"), "30 s / 1e6 * 15.0 USD per 1M s"
    # the old (wrong) arithmetic, pinned only to prove it is gone:
    assert Decimal("0.001350") == (Decimal(90) / Decimal(1_000_000) * Decimal("15.0")).quantize(Decimal("0.000001"))
    assert row[7] != Decimal("0.001350"), "the per-second rate must not ride the token arithmetic"


def test_tts_char_estimate_on_second_priced_model_ledgers_null(
    env,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A second-priced voice model with ONLY a char estimate (no upstream
    duration header): the row ledgers cost NULL with the mismatch WARNING —
    chars are never converted to seconds, and the token columns are never
    billed through the per-1M-SECOND rate."""
    tc, fake_db, _, _, mp = env
    _price_cache(
        mp,
        {
            VOICE: {
                "model_name": VOICE,
                "input_per_m": 15.0,
                "output_per_m": 0.0,
                "cached_per_m": 0.0,
                "unit": "second",
            }
        },
    )
    with caplog.at_level("WARNING", logger="pcai_llm.core.costs"):
        body = json.dumps({"model": VOICE, "input": "hello world"}).encode()
        r = tc.post("/v1/audio/speech", content=body, headers={**AUTH, "Content-Type": "application/json"})
    assert r.status_code == 200, r.text
    row = fake_db.ledger_rows()[0]
    assert row[19] == "char_estimate"
    assert row[18] == float(len("hello world"))
    assert row[7] is None, "a char estimate cannot bill a second-priced model"
    assert "priced per second" in caplog.text and "char" in caplog.text


def test_asr_hint_clamped_both_ways_and_upstream_duration_wins(
    env,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The caller duration hint is a PRICE INPUT, not trusted metadata: it
    is clamped into the upload compression envelope both ways (0.001 s no
    longer mints free ASR; 99999999 s no longer mints a 1500 USD row), each
    clamp logs a WARNING — and an UPSTREAM-reported duration beats any hint."""
    tc, fake_db, _seen, _, mp = env
    _price_cache(
        mp,
        {
            VOICE: {
                "model_name": VOICE,
                "input_per_m": 15.0,
                "output_per_m": 0.0,
                "cached_per_m": 0.0,
                "unit": "second",
            }
        },
    )
    boundary = "----p10-boundary"
    audio = b"\x00" * 32000
    body = (
        f"--{boundary}\r\n".encode()
        + b'Content-Disposition: form-data; name="file"; filename="clip.wav"\r\n\r\n'
        + audio
        + f"\r\n--{boundary}--\r\n".encode()
    )
    ctype = f"multipart/form-data; boundary={boundary}"
    est = max(1.0, len(body) / settings.audio_asr_bytes_per_second)
    lo, hi = est / 16.0, est * 16.0

    with caplog.at_level("WARNING", logger="pcai_llm.api.routes_proxy"):
        r_low = tc.post(
            "/v1/audio/transcriptions",
            content=body,
            headers={**AUTH, "Content-Type": ctype},
            params={"model": VOICE, "duration": "0.001"},
        )
        r_high = tc.post(
            "/v1/audio/transcriptions",
            content=body,
            headers={**AUTH, "Content-Type": ctype},
            params={"model": VOICE, "duration": "99999999"},
        )
    assert r_low.status_code == 200 and r_high.status_code == 200
    rows = fake_db.ledger_rows()
    assert len(rows) == 2
    # clamped UP off the floor: 0.001 s of free ASR becomes the envelope floor
    assert rows[0][18] == pytest.approx(lo), "the low hint must clamp UP to byte_proxy/16"
    assert rows[0][19] == "second"
    # clamped DOWN off the ceiling: 99999999 s becomes the envelope ceiling
    assert rows[1][18] == pytest.approx(hi), "the huge hint must clamp DOWN to byte_proxy*16"
    assert rows[1][19] == "second"
    # both rewrites are NAMED, never silent
    assert "clamp" in caplog.text and caplog.text.count("clamped to") == 2

    # the UPSTREAM-reported duration beats ANY hint (measured > claimed)
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, content=b"{}", headers={"X-Audio-Duration": "5.0"})

    mock_router = Router.with_mock(handler)
    mock_router.set_endpoints(
        VOICE, [Endpoint(id="vp1", model_name=VOICE, base_url="http://fake-a/v1", model_type="voice")]
    )
    monkeypatch.setattr(routes_proxy, "model_router", mock_router)
    r_up = tc.post(
        "/v1/audio/transcriptions",
        content=body,
        headers={**AUTH, "Content-Type": ctype},
        params={"model": VOICE, "duration": "99999999"},
    )
    assert r_up.status_code == 200, r_up.text
    row_up = fake_db.ledger_rows()[2]
    assert row_up[18] == 5.0, "the upstream-measured duration must beat the (clamped) hint"
    assert row_up[19] == "second"
    assert row_up[7] == Decimal("0.000075")  # 5.0 / 1e6 * 15.0


def test_oversized_billable_units_ledgers_null_not_lost(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """billable_units past NUMERIC(18,6) used to raise
    NumericValueOutOfRangeError from the INSERT — the request 500ed AND the
    immutable billing row for upstream spend already served was LOST. The
    quantity (and the unpriceable cost) ledger NULL instead; the row survives."""
    _price_cache(
        monkeypatch,
        {
            VOICE: {
                "model_name": VOICE,
                "input_per_m": 15.0,
                "output_per_m": 0.0,
                "cached_per_m": 0.0,
                "unit": "second",
            }
        },
    )
    fake_db = FakeDB()
    monkeypatch.setattr(budgets_mod, "db", fake_db)
    with caplog.at_level("WARNING", logger="pcai_llm.core.budgets"):
        asyncio.run(
            budgets_mod.settle(
                uuid.uuid4(),
                VOICE,
                "audio.transcriptions",
                3,
                0,
                "ok",
                5,
                "req-big",
                billable_units=2e12,
                unit_kind="second",
            )
        )
    rows = fake_db.ledger_rows()
    assert len(rows) == 1, "the billing row itself must NEVER be lost"
    row = rows[0]
    assert row[8] == "ok"
    assert row[18] is None, "the un-storable quantity must ledger NULL, not abort"
    assert row[19] == "second"
    assert row[7] is None, "a quantity that cannot be stored must not be priced"
    assert "NUMERIC(18,6)" in caplog.text

    # a huge-but-in-range quantity stores — and a cost past NUMERIC(12,6)
    # (999999999999 s * 15 USD/1M s = 15M USD) ledgers NULL the same way,
    # for the same reason: the row must survive its own price.
    with caplog.at_level("WARNING", logger="pcai_llm.core.budgets"):
        asyncio.run(
            budgets_mod.settle(
                uuid.uuid4(),
                VOICE,
                "audio.transcriptions",
                3,
                0,
                "ok",
                5,
                "req-huge-ok",
                billable_units=999_999_999_999.0,
                unit_kind="second",
            )
        )
    row2 = fake_db.ledger_rows()[1]
    assert row2[18] == 999_999_999_999.0
    assert row2[7] is None and "NUMERIC(12,6)" in caplog.text

    # ordinary in-range quantities price and store exactly as before
    asyncio.run(
        budgets_mod.settle(
            uuid.uuid4(),
            VOICE,
            "audio.transcriptions",
            3,
            0,
            "ok",
            5,
            "req-normal",
            billable_units=1_000_000.0,
            unit_kind="second",
        )
    )
    row3 = fake_db.ledger_rows()[2]
    assert row3[18] == 1_000_000.0
    assert row3[7] == Decimal("15.000000")


def test_usage_summary_excludes_routing_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cross-validation wave 2: /admin/usage/summary counted the gateway OWN
    routing rows in requests and billed_tokens — the gate said 1500, the
    summary said 1900 (+26.7% phantom spend), and 2 requests stood where the
    tenant made 1. The totals row now carries the gate OWN status predicate
    (status IN _COSTED_SQL) on billed_tokens and excludes routing from
    requests, with the routing spend VISIBLE as its own line. The per-unit
    sums are folded per unit kind — chars and seconds are never added up."""
    from pcai_llm.api import routes_admin as ra

    class SummaryDB:
        def __init__(self) -> None:
            self.sqls: list[tuple[str, str]] = []

        async def fetchrow(self, sql: str, *a: Any):
            self.sqls.append(("fetchrow", sql))
            return {}

        async def fetch(self, sql: str, *a: Any):
            self.sqls.append(("fetch", sql))
            if "GROUPING SETS" in sql:
                base = {
                    "requests": 1,
                    "input_tokens": 10,
                    "output_tokens": 0,
                    "cached_tokens": 0,
                    "cache_hits": 0,
                    "cache_hit_rate_pct": None,
                    "cost_usd": 0.5,
                    "billed_tokens": 10.0,
                    "billable_tokens": 10.0,
                    "billed_cost_usd": 0.5,
                }
                total = dict(
                    base, model="m", key_name="k", user_name="u", generations=1, unit_kind=None, unit_units=None
                )
                second = dict(
                    base, model="m", key_name="k", user_name="u", generations=1, unit_kind="second", unit_units=30.0
                )
                chars = dict(
                    base,
                    model="m",
                    key_name="k",
                    user_name="u",
                    generations=1,
                    unit_kind="char_estimate",
                    unit_units=1000.0,
                )
                return [total, second, chars]
            return []

        async def fetchval(self, sql: str, *a: Any):
            self.sqls.append(("fetchval", sql))

    fake = SummaryDB()
    monkeypatch.setattr(ra, "db", fake)
    out = asyncio.run(ra.usage_summary(None))

    totals_sql = next(s for kind, s in fake.sqls if kind == "fetchrow")
    # requests exclude the gateway's own routing turns; routing stays VISIBLE
    assert "count(*) FILTER (WHERE status <> 'routing')                AS requests" in totals_sql
    assert "AS routing_requests" in totals_sql and "AS routing_cost_usd" in totals_sql
    # billed_tokens carries the GATE own money predicate (never the raw SUM):
    # the per-row budget VALUE is interpolated from _budget_token_value (its
    # cache-discount arithmetic appears expanded, exact shape depends on the
    # exempt-fraction setting), wrapped in SUM(...) FILTER over the gate's
    # costed statuses.
    assert "status IN ('ok', 'error', 'guardrail_blocked')" in totals_sql
    assert "usage_ledger.total_tokens" in totals_sql and "usage_ledger.billing_fraction" in totals_sql

    rollups = [s for kind, s in fake.sqls if kind == "fetch" and "GROUPING SETS" in s]
    assert len(rollups) == 2, "by_model + by_key group per unit kind"
    for sql in rollups:
        assert "GROUP BY GROUPING SETS" in sql
        assert "AS billable_units" not in sql, "no cross-unit SUM anywhere"

    # the fold: the model total row carries an object keyed by UNIT —
    # {"second": 30.0, "char_estimate": 1000.0} — never one mixed number
    assert out["by_model"][0]["billable_units_by_unit"] == {"second": 30.0, "char_estimate": 1000.0}
    assert out["by_key"][0]["billable_units_by_unit"] == {"second": 30.0, "char_estimate": 1000.0}
    assert "unit_units" not in out["by_model"][0] and "unit_kind" not in out["by_model"][0]
