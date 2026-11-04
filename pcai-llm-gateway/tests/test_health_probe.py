"""Active health probe (GOAL A) + model-attributable 4xx failover tests.

Covers the probe lifecycle (ok/fail transitions, transition-only logging,
fail-open for never-probed endpoints, pick() exclusion, interval<=0 inertness,
transport-error containment, replace_pools probe-state carry), the
_is_model_attributable_4xx policy table (including the 401/403/429/generic-400
negatives), both failover sites' behavior on model-attributable 4xx, and the
mixed-pool endpoints[0] type-gate fix at both the fallback-walk and the
chat-admission level.

No live Postgres: budgets/cascade/costs db handles are monkeypatched, the
upstream is an in-process httpx2.MockTransport (the test_proxy.py pattern).
"""

from __future__ import annotations

import json
import logging
import time
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
from pcai_llm.core import health_probe
from pcai_llm.routing.router import Endpoint, Pool, Router

MODEL = "hp-model"
FB_MODEL = "hp-fb-model"


def _ep(eid: str, model: str = MODEL, mtype: str = "chat", url: str = "http://fake/v1") -> Endpoint:
    return Endpoint(id=eid, model_name=model, base_url=url, model_type=mtype)


def _probe_client(handler) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(transport=httpx2.MockTransport(handler))


# ---------------------------------------------------------------------------
# GOAL A — the probe itself
# ---------------------------------------------------------------------------


async def test_probe_ok_fail_transitions_log_only_on_change(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    # threshold 1 here: this test is about LOGGING transitions, not hysteresis
    # (which test_probe_hysteresis covers).
    monkeypatch.setattr(settings, "health_probe_failures_to_trip", 1)
    status = {"code": 200}

    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url.path == "/v1/models"
        return httpx2.Response(status["code"], text="x")

    router = Router()
    ep = _ep("ep1")
    router.set_endpoints(MODEL, [ep])
    client = _probe_client(handler)
    try:
        with caplog.at_level(logging.INFO, logger="pcai_llm.core.health_probe"):
            # tick 1: ok. prev_ok None (never probed) -> NO log line: the
            # first tick on a healthy fleet is not an event.
            await health_probe.run_once(client=client, router=router)
            assert ep.probe_ok is True
            assert ep.probe_error == ""
            assert caplog.text == "", "first probe must not log (no transition)"

            # tick 2: 500 -> ok->fail transition: state flips AND is logged.
            status["code"] = 500
            await health_probe.run_once(client=client, router=router)
            assert ep.probe_ok is False
            assert ep.probe_error == "upstream status 500"
            assert "degraded" in caplog.text

            # tick 3: still 500 -> same state, transition-only logging holds.
            caplog.clear()
            await health_probe.run_once(client=client, router=router)
            assert ep.probe_ok is False
            assert "degraded" not in caplog.text, "a repeated fail must not re-log"

            # tick 4: 200 again -> fail->ok: state flips back AND is logged.
            status["code"] = 200
            await health_probe.run_once(client=client, router=router)
            assert ep.probe_ok is True
            assert "recovered" in caplog.text
    finally:
        await client.aclose()


async def test_probe_4xx_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    """THE 2026-11-03 OUTAGE, pinned at the classification layer.

    The probe first treated EVERY non-2xx as a failure. An upstream that
    auth-gates /models answered 401, the whole fleet was marked probe-failed,
    every pool went empty and every chat request answered
    "503 no healthy upstream".

    * 401/403 = the server ANSWERED, so it is alive. Availability is not
      authorization; the forward path surfaces the 401 honestly.
    * other 4xx (404/405/400/422) = the PROBE is wrong (path shape, method, a
      server with no /models card). Our misconfiguration must never remove a
      healthy model from rotation.
    """
    monkeypatch.setattr(settings, "health_probe_failures_to_trip", 1)
    for code in (401, 403):
        router = Router()
        ep = _ep(f"ep-{code}")
        router.set_endpoints(MODEL, [ep])
        client = _probe_client(lambda r, c=code: httpx2.Response(c, text="nope"))
        try:
            await health_probe.run_once(client=client, router=router)
            assert ep.probe_ok is True, f"{code} means the server is ALIVE"
            assert router.pool(MODEL).pick() is ep, "an auth-gated card must not drop the endpoint"
        finally:
            await client.aclose()
    for code in (404, 405, 422):
        router = Router()
        ep = _ep(f"ep-{code}")
        router.set_endpoints(MODEL, [ep])
        client = _probe_client(lambda r, c=code: httpx2.Response(c, text="nope"))
        try:
            await health_probe.run_once(client=client, router=router)
            assert ep.probe_ok is None, f"{code} is UNKNOWN: our probe was wrong, state untouched"
            assert router.pool(MODEL).pick() is ep
            assert f"probe unsupported ({code})" == ep.probe_error
        finally:
            await client.aclose()


async def test_probe_sends_the_endpoint_own_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    """The probe must present the SAME credential the forward path presents.

    It shipped auth-less, which is what turned an auth-gated /models into a
    fleet-wide outage (2026-11-03)."""
    monkeypatch.setenv("PCAI_LLM_UPSTREAM_KEY__PROBE_SECRET", "s3cr3t")
    seen: list[dict[str, str]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(dict(request.headers))
        return httpx2.Response(200, text="x")

    router = Router()
    ep = _ep("ep1")
    ep.api_key_secret = "probe-secret"
    router.set_endpoints(MODEL, [ep])
    client = _probe_client(handler)
    try:
        await health_probe.run_once(client=client, router=router)
    finally:
        await client.aclose()
    assert seen and seen[0].get("authorization") == "Bearer s3cr3t", (
        "the probe must authenticate like the forward path — an auth-less probe reads an auth-gated upstream as DOWN"
    )


def test_never_probed_stays_healthy_fail_open() -> None:
    """probe_ok None = never probed = HEALTHY: a cold pod (first tick not
    run) or a probe-disabled deployment must not read broken."""
    pool = Pool(endpoints=[_ep("ep1")])
    assert pool.healthy() == pool.endpoints
    assert pool.pick() is not None
    # probe_ok True is also healthy (the gate only excludes False):
    pool.report_probe(pool.endpoints[0], True)
    assert pool.healthy() == pool.endpoints


def test_probe_fail_excludes_from_pick(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "health_probe_failures_to_trip", 1)
    pool = Pool(endpoints=[_ep("bad"), _ep("good")])
    pool.report_probe(pool.endpoints[0], False, "upstream status 500")
    assert pool.healthy() == [pool.endpoints[1]]
    assert pool.pick() is pool.endpoints[1]
    # Session-affinity preference must not resurrect a probe-failed endpoint:
    # pick() validates preferred_id against healthy() candidates only.
    assert pool.pick(preferred_id="bad") is pool.endpoints[1]


def test_probe_hysteresis_needs_consecutive_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    """One blip must not drop an endpoint out of rotation."""
    monkeypatch.setattr(settings, "health_probe_failures_to_trip", 2)
    # TWO endpoints: exclusion is only OBSERVABLE while another member remains
    # (see test_probe_can_never_empty_a_pool — a probe failure alone can never
    # empty a pool, so a single-endpoint pool would always look unaffected).
    pool = Pool(endpoints=[_ep("ep1"), _ep("ep2")])
    ep = pool.endpoints[0]
    pool.report_probe(ep, False, "upstream status 503")
    assert ep.probe_ok is None, "a single failed sweep leaves the gate untouched (fail-open)"
    assert pool.healthy() == pool.endpoints
    pool.report_probe(ep, False, "upstream status 503")
    assert ep.probe_ok is False, "the SECOND consecutive failure trips the gate"
    assert pool.healthy() == [pool.endpoints[1]]
    # A success resets the streak, so the next single failure is a blip again.
    pool.report_probe(ep, True)
    assert ep.probe_ok is True and ep.probe_failures == 0
    pool.report_probe(ep, False, "upstream status 503")
    assert ep.probe_ok is True


def test_probe_can_never_empty_a_pool(caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    """THE 2026-11-03 OUTAGE AS A REGRESSION TEST.

    Every endpoint probe-failed used to mean Pool.healthy() == [] and every
    chat request answered "503 no healthy upstream". The NEVER-EMPTY INVARIANT
    makes that structurally impossible: when the probe alone would exclude
    every endpoint, the probe is IGNORED for that pool and the breaker view is
    served. The breaker (fed by real traffic) is still fully enforced.
    """
    monkeypatch.setattr(settings, "health_probe_failures_to_trip", 1)
    pool = Pool(endpoints=[_ep("a"), _ep("b")])
    with caplog.at_level(logging.ERROR, logger="pcai_llm.routing.router"):
        for ep in pool.endpoints:
            pool.report_probe(ep, False, "upstream status 401")
        assert [e.probe_ok for e in pool.endpoints] == [False, False]
        assert pool.healthy() == pool.endpoints, "the probe must never dark out a pool"
        assert pool.pick() is not None
        assert "IGNORING the probe" in caplog.text, "the fail-open must be LOUD"
        # Logged once per state change, not per request: healthy() runs per call.
        caplog.clear()
        pool.healthy()
        assert "IGNORING the probe" not in caplog.text
    # The breaker still gates: a cooling endpoint stays out even under fail-open.
    pool.endpoints[0].cooldown_until = 1e18
    assert pool.healthy() == [pool.endpoints[1]]


def test_report_probe_clears_error_on_ok() -> None:
    pool = Pool(endpoints=[_ep("ep1")])
    ep = pool.endpoints[0]
    pool.report_probe(ep, False, "upstream status 500")
    assert ep.probe_error == "upstream status 500"
    pool.report_probe(ep, True)
    assert ep.probe_ok is True
    assert ep.probe_error == ""
    assert ep.probe_at > 0


async def test_interval_zero_is_inert(monkeypatch: pytest.MonkeyPatch) -> None:
    """interval <= 0 (and the flag off) = no task is created at all, and
    stop() stays a clean no-op."""
    await health_probe.stop()
    try:
        monkeypatch.setattr(settings, "health_probe_interval_s", 0.0)
        monkeypatch.setattr(settings, "health_probe_enabled", True)
        await health_probe.start()
        assert health_probe._task is None, "interval 0 must not start a task"
        await health_probe.stop()  # no-op, must not raise

        monkeypatch.setattr(settings, "health_probe_interval_s", 30.0)
        monkeypatch.setattr(settings, "health_probe_enabled", False)
        await health_probe.start()
        assert health_probe._task is None, "flag off must not start a task"
    finally:
        await health_probe.stop()


async def test_start_stop_lifecycle(monkeypatch: pytest.MonkeyPatch) -> None:
    # The probe ships DISABLED after the 2026-11-03 incident; the lifecycle
    # still has to work when an operator opts in.
    monkeypatch.setattr(settings, "health_probe_enabled", True)
    monkeypatch.setattr(settings, "health_probe_interval_s", 30.0)
    await health_probe.stop()
    try:
        await health_probe.start()
        assert health_probe._task is not None
        assert health_probe._task.get_name() == "health-probe"
        await health_probe.start()  # idempotent
        await health_probe.stop()
        assert health_probe._task is None
        await health_probe.stop()  # double stop is a no-op
    finally:
        await health_probe.stop()


async def test_transport_error_never_raises() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("connection refused", request=request)

    router = Router()
    ep = _ep("ep1")
    router.set_endpoints(MODEL, [ep])
    client = _probe_client(handler)
    try:
        await health_probe.run_once(client=client, router=router)  # must not raise
        # Hysteresis: one failed sweep records the reason but does NOT flip the
        # gate (probe_ok stays None = never probed = healthy).
        assert ep.probe_ok is None
        assert ep.probe_failures == 1
        assert ep.probe_error == "ConnectError"
        await health_probe.run_once(client=client, router=router)
        assert ep.probe_ok is False, "the second consecutive failure trips the gate"
    finally:
        await client.aclose()


async def test_closed_client_never_raises() -> None:
    """Even an HTTP-level failure the probe cannot classify (a client closed
    mid-sweep) is swallowed: the probe task must never die."""
    router = Router()
    router.set_endpoints(MODEL, [_ep("ep1")])
    client = _probe_client(lambda r: httpx2.Response(200, text="x"))
    await client.aclose()
    await health_probe.run_once(client=client, router=router)  # must not raise


async def test_probe_skips_unprobed_model_types() -> None:
    """Only chat|embedding|reranker pools are probed (voice/decision pools
    are not OpenAI-compatible liveness surfaces)."""
    calls: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(request.url.host)
        return httpx2.Response(200, text="x")

    router = Router()
    router.set_endpoints("voice-m", [_ep("v1", model="voice-m", mtype="voice", url="http://voice")])
    router.set_endpoints("emb-m", [_ep("e1", model="emb-m", mtype="embedding", url="http://emb")])
    router.set_endpoints("rr-m", [_ep("r1", model="rr-m", mtype="reranker", url="http://rr")])
    client = _probe_client(handler)
    try:
        await health_probe.run_once(client=client, router=router)
    finally:
        await client.aclose()
    assert sorted(calls) == ["emb", "rr"], f"voice must not be probed: {calls}"
    assert router.pool("voice-m").endpoints[0].probe_ok is None


# ---------------------------------------------------------------------------
# GOAL A — replace_pools carries probe state (same rule as the breaker)
# ---------------------------------------------------------------------------


def test_replace_pools_carries_probe_state() -> None:
    router = Router()
    old = _ep("ep1")
    old.probe_ok = False
    old.probe_at = 1234.0
    old.probe_error = "upstream status 404"
    old.consecutive_failures = 2
    ok_ep = _ep("ep2")
    ok_ep.probe_ok = True
    ok_ep.probe_at = 4321.0
    router.set_endpoints(MODEL, [old, ok_ep])

    new_eps = [_ep("ep1"), _ep("ep2")]
    router.replace_pools({MODEL: new_eps})
    pool = router.pool(MODEL)
    assert pool.endpoints[0] is new_eps[0]
    # probe-failed endpoint STAYS failed across the refresh (a console save
    # during an outage must not hand it a fresh never-probed healthy state):
    assert new_eps[0].probe_ok is False
    assert new_eps[0].probe_at == 1234.0
    assert new_eps[0].probe_error == "upstream status 404"
    assert new_eps[0].consecutive_failures == 2
    # probe_ok True is carried too (is not None, not truthy-only):
    assert new_eps[1].probe_ok is True
    assert new_eps[1].probe_at == 4321.0
    # and the carried state still excludes it from the live pick:
    assert pool.pick(preferred_id="ep1") is new_eps[1]


def test_replace_pools_carries_breaker_only_when_probe_absent() -> None:
    router = Router()
    old = _ep("ep1")
    old.consecutive_failures = 3  # breaker state, but never probed
    router.set_endpoints(MODEL, [old])
    new_ep = _ep("ep1")
    router.replace_pools({MODEL: [new_ep]})
    assert new_ep.consecutive_failures == 3
    assert new_ep.probe_ok is None, "breaker-only state must not mint a probe verdict"


# ---------------------------------------------------------------------------
# GOAL B — the model-attributable 4xx policy (pure helper, table test)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "body", "model", "expected"),
    [
        # 404: model-not-found — the call path is fixed, so always eligible.
        (404, b"{}", "glm-5.3-flash", True),
        (404, b"", "glm-5.3-flash", True),
        (404, b'"not found"', "glm-5.3-flash", True),
        # 400 with an explicit marker in the body.
        (400, b"\"The model 'glm-5.3-flash' does not exist\"", "glm-5.3-flash", True),
        (400, b"Model not found", "glm-5.3-flash", True),
        (400, b'"Unknown model: foo"', "foo", True),
        (400, b"this model's maximum context length is 8192 tokens", "glm-5.3-flash", True),
        (400, b"context length exceeded: 200000 > 128000", "glm-5.3-flash", True),
        (400, b'"Unexpected message role"', "glm-5.3-flash", True),
        (400, b"Unsupported role 'developer' for this model", "glm-5.3-flash", True),
        # 400 that QUOTES the model name the gateway sent (phrasing misses
        # every marker; the name match catches it).
        (400, b"Model 'GLM-5.3-Flash' is not found in the model registry", "GLM-5.3-Flash", True),
        # NEGATIVES — the caller's/request's fault, never re-rolled.
        (400, b'"Invalid request: max_tokens must be positive"', "glm-5.3-flash", False),
        (400, b"body validation failed: field 'model' missing", "glm-5.3-flash", False),
        (400, b"some generic validation error", "glm-5.3-flash", False),
        (400, b"some generic validation error", "", False),  # empty model: name match off
        (401, b"invalid api key", "glm-5.3-flash", False),
        (403, b"forbidden", "glm-5.3-flash", False),
        (429, b"rate limit exceeded", "glm-5.3-flash", False),
        (405, b"method not allowed", "glm-5.3-flash", False),
        # 5xx is NOT this helper's business — the sites' 5xx branch owns it.
        (500, b"internal error", "glm-5.3-flash", False),
        (503, b"", "glm-5.3-flash", False),
    ],
)
def test_is_model_attributable_4xx_table(status: int, body: bytes, model: str, expected: bool) -> None:
    assert routes_proxy._is_model_attributable_4xx(status, body, model) is expected


class FakeDB:
    """Budget engine stub (the test_proxy pattern): no budgets, low counters."""

    def __init__(self) -> None:
        self.settled: list[tuple] = []

    async def fetch(self, sql: str, *args: Any):
        return []

    async def fetchrow(self, sql: str, *args: Any):
        if "profiles" in sql:
            return None
        return {"requests": 1, "tokens": 10}

    async def execute(self, sql: str, *args: Any):
        self.settled.append(args)
        return "INSERT 0 1"


def _key(allowed_models: list[str] | None = None) -> KeyRecord:
    return KeyRecord(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        name="k",
        key_prefix="hllm-XX",
        scopes=["proxy"],
        allowed_models=allowed_models or [],
        rate_limit_rpm=1000,
        rate_limit_tpm=1_000_000,
        max_concurrent=0,
        expires_at=None,
    )


def _wire_budgets(monkeypatch: pytest.MonkeyPatch) -> FakeDB:
    fake_db = FakeDB()
    monkeypatch.setattr(budgets_mod, "db", fake_db)
    monkeypatch.setattr(costs_mod, "db", fake_db)
    monkeypatch.setattr(cascade_mod, "db", fake_db)
    return fake_db


_CHAT_OK = {
    "id": "cmpl-fb",
    "object": "chat.completion",
    "model": FB_MODEL,
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6},
}


# ---------------------------------------------------------------------------
# GOAL B — the helper wired into BOTH failover sites (behavioral)
# ---------------------------------------------------------------------------


async def test_proxy_json_404_model_not_found_walks_the_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 404 from the primary (model-not-found) is model-attributable: the
    breaker report + error settle happen, then the walk continues to the
    fallback, which serves 200."""
    fake_db = _wire_budgets(monkeypatch)
    bodies: list[dict] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content.decode())
        bodies.append(body)
        if body.get("model") == MODEL:
            return httpx2.Response(404, text="model not found")
        return httpx2.Response(200, json=_CHAT_OK)

    mock_router = Router.with_mock(handler)
    mock_router.set_endpoints(MODEL, [_ep("ep1", url="http://fake/v1")])
    mock_router.set_endpoints(FB_MODEL, [_ep("ep2", model=FB_MODEL, url="http://fake/v1")])
    monkeypatch.setattr(routes_proxy, "model_router", mock_router)

    key = _key()
    pool = mock_router.pool(MODEL)
    resp = await routes_proxy._proxy_json(
        pool,
        pool.pick(),
        "http://fake/v1/chat/completions",
        {"model": MODEL, "messages": [{"role": "user", "content": "hello"}]},
        {},
        key,
        MODEL,
        "chat",
        10,
        "req-404fb",
        time.monotonic(),
        fallback_chain=[FB_MODEL],
    )
    assert resp.status_code == 200, resp.body
    assert [b["model"] for b in bodies] == [MODEL, FB_MODEL]
    assert resp.headers.get("x-pcai-fallback") == MODEL
    # Every attempt settled under its ACTUAL model: the 404 row is the
    # primary, the ok row the fallback.
    assert [row[1] for row in fake_db.settled] == [MODEL, FB_MODEL]


async def test_proxy_json_model_attributable_400_walks_the_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 400 whose body names the model is model-attributable: the walk
    continues exactly like the 404 case."""
    fake_db = _wire_budgets(monkeypatch)
    bodies: list[dict] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content.decode())
        bodies.append(body)
        if body.get("model") == MODEL:
            return httpx2.Response(
                400,
                json={"error": {"message": "The model 'hp-model' does not exist", "type": "invalid_request_error"}},
            )
        return httpx2.Response(200, json=_CHAT_OK)

    mock_router = Router.with_mock(handler)
    mock_router.set_endpoints(MODEL, [_ep("ep1", url="http://fake/v1")])
    mock_router.set_endpoints(FB_MODEL, [_ep("ep2", model=FB_MODEL, url="http://fake/v1")])
    monkeypatch.setattr(routes_proxy, "model_router", mock_router)

    key = _key()
    pool = mock_router.pool(MODEL)
    resp = await routes_proxy._proxy_json(
        pool,
        pool.pick(),
        "http://fake/v1/chat/completions",
        {"model": MODEL, "messages": [{"role": "user", "content": "hello"}]},
        {},
        key,
        MODEL,
        "chat",
        10,
        "req-400fb",
        time.monotonic(),
        fallback_chain=[FB_MODEL],
    )
    assert resp.status_code == 200, resp.body
    assert [b["model"] for b in bodies] == [MODEL, FB_MODEL]
    assert [row[1] for row in fake_db.settled] == [MODEL, FB_MODEL]


@pytest.mark.parametrize(
    "status, body", [(400, b'{"error": {"message": "Invalid value for max_tokens"}}'), (401, b"invalid api key")]
)
async def test_proxy_json_non_attributable_4xx_never_rolls(
    monkeypatch: pytest.MonkeyPatch, status: int, body: bytes
) -> None:
    """The NARROW policy: a generic validation 400 and a 401 are the
    caller's/request's fault — relayed untouched, NO second GPU call."""
    _wire_budgets(monkeypatch)
    bodies: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        bodies.append(request.content.decode()[:200])
        return httpx2.Response(status, content=body)

    mock_router = Router.with_mock(handler)
    mock_router.set_endpoints(MODEL, [_ep("ep1", url="http://fake/v1")])
    mock_router.set_endpoints(FB_MODEL, [_ep("ep2", model=FB_MODEL, url="http://fake/v1")])
    monkeypatch.setattr(routes_proxy, "model_router", mock_router)

    key = _key()
    pool = mock_router.pool(MODEL)
    resp = await routes_proxy._proxy_json(
        pool,
        pool.pick(),
        "http://fake/v1/chat/completions",
        {"model": MODEL, "messages": [{"role": "user", "content": "hello"}]},
        {},
        key,
        MODEL,
        "chat",
        10,
        f"req-noroll-{status}",
        time.monotonic(),
        fallback_chain=[FB_MODEL],
    )
    assert resp.status_code == status, "the upstream error must be relayed untouched"
    assert len(bodies) == 1, f"status {status} must never re-roll: {len(bodies)} upstream calls"


_SSE = (
    b'data: {"id":"c","choices":[{"delta":{"content":"hi"},"finish_reason":null}]}\n\n'
    b'data: {"id":"c","choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
    b"data: [DONE]\n\n"
)


async def test_stream_chat_model_attributable_400_walks_the_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    """The streaming twin of the 400 case: the pre-first-byte failover walk
    picks up model-attributable 400s (the stream_options negotiation retry
    fires first on ANY 4xx — pre-existing behavior — and lands on the same
    400; the walk then continues to the fallback, which streams 200)."""
    fake_db = _wire_budgets(monkeypatch)
    models: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content.decode())
        models.append(body["model"])
        if body["model"] == MODEL:
            return httpx2.Response(
                400,
                json={"error": {"message": "The model 'hp-model' does not exist", "type": "invalid_request_error"}},
            )
        return httpx2.Response(200, content=_SSE, headers={"content-type": "text/event-stream"})

    mock_router = Router.with_mock(handler)
    mock_router.set_endpoints(MODEL, [_ep("ep1", url="http://fake/v1")])
    mock_router.set_endpoints(FB_MODEL, [_ep("ep2", model=FB_MODEL, url="http://fake/v1")])
    monkeypatch.setattr(routes_proxy, "model_router", mock_router)

    key = _key()
    pool = mock_router.pool(MODEL)
    resp = await routes_proxy._stream_chat(
        pool,
        pool.pick(),
        "http://fake/v1/chat/completions",
        {"model": MODEL, "messages": [{"role": "user", "content": "hello"}], "stream": True},
        {},
        key,
        MODEL,
        10,
        "req-stream-400fb",
        time.monotonic(),
        fallback_chain=[FB_MODEL],
    )
    payload = b"".join([chunk async for chunk in resp.body_iterator])
    assert b"[DONE]" in payload, f"the fallback stream must serve: {payload[:200]!r}"
    assert models[-1] == FB_MODEL, f"the serving attempt must be the fallback: {models}"
    assert resp.headers.get("x-pcai-fallback") == MODEL
    # Both attempts settled under their ACTUAL models.
    assert [row[1] for row in fake_db.settled] == [MODEL, FB_MODEL]


async def test_stream_chat_401_never_rolls(monkeypatch: pytest.MonkeyPatch) -> None:
    """The streaming negative: a 401 is relayed pre-first-byte, no walk.
    (The stream_options negotiation retry still fires on the 4xx —
    pre-existing behavior — so exactly TWO primary attempts, zero fallback.)"""
    _wire_budgets(monkeypatch)
    models: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        models.append(json.loads(request.content.decode())["model"])
        return httpx2.Response(401, text="invalid api key")

    mock_router = Router.with_mock(handler)
    mock_router.set_endpoints(MODEL, [_ep("ep1", url="http://fake/v1")])
    mock_router.set_endpoints(FB_MODEL, [_ep("ep2", model=FB_MODEL, url="http://fake/v1")])
    monkeypatch.setattr(routes_proxy, "model_router", mock_router)

    key = _key()
    pool = mock_router.pool(MODEL)
    resp = await routes_proxy._stream_chat(
        pool,
        pool.pick(),
        "http://fake/v1/chat/completions",
        {"model": MODEL, "messages": [{"role": "user", "content": "hello"}], "stream": True},
        {},
        key,
        MODEL,
        10,
        "req-stream-401",
        time.monotonic(),
        fallback_chain=[FB_MODEL],
    )
    # The pre-first-byte error is relayed as an ORDINARY (non-SSE) Response:
    # the body sits in .body — there is no body_iterator to drain.
    payload = resp.body
    assert resp.status_code == 401
    assert b"invalid api key" in payload
    assert models == [MODEL, MODEL], f"no fallback attempt: {models}"


# ---------------------------------------------------------------------------
# GOAL C — the mixed-pool type gate (any() predicate, not endpoints[0])
# ---------------------------------------------------------------------------


def test_mixed_pool_predicate(monkeypatch: pytest.MonkeyPatch) -> None:
    """The predicate level: serves_chat accepts a pool whose FIRST endpoint
    is embedding but which still has a chat member (the old endpoints[0] gate
    would have 400/skipped it); a no-chat pool is still excluded; an empty
    pool stays unknown-kept (the serves_chat contract)."""
    router = Router()
    router.set_endpoints(
        "mixed",
        [
            _ep("emb-first", model="mixed", mtype="embedding", url="http://mixed/v1"),
            _ep("chat-second", model="mixed", mtype="chat", url="http://mixed/v1"),
        ],
    )
    # The old gate's reading, kept for the regression assertion:
    assert router.pool("mixed").endpoints[0].model_type != "chat"
    monkeypatch.setattr(cascade_mod, "model_router", router)
    assert cascade_mod.serves_chat("mixed") is True
    router.set_endpoints("emb-only", [_ep("e1", model="emb-only", mtype="embedding", url="http://x/v1")])
    assert cascade_mod.serves_chat("emb-only") is False
    router.set_endpoints("empty", [])
    assert cascade_mod.serves_chat("empty") is True


async def test_mixed_pool_fallback_walk(monkeypatch: pytest.MonkeyPatch) -> None:
    """The walk level: _next_fallback_candidate now ACCEPTS a mixed candidate
    pool (pre-fix its endpoints[0] gate skipped the whole pool). Priority
    keeps pick() deterministic (the chat member wins the tier)."""
    _wire_budgets(monkeypatch)
    router = Router()
    router.set_endpoints(MODEL, [_ep("ep1", url="http://prim/v1")])
    emb = _ep("emb-first", model=FB_MODEL, mtype="embedding", url="http://mixed/v1")
    chat = _ep("chat-second", model=FB_MODEL, mtype="chat", url="http://mixed/v1")
    emb.priority = 1  # the chat member wins the best tier
    router.set_endpoints(FB_MODEL, [emb, chat])
    monkeypatch.setattr(routes_proxy, "model_router", router)

    key = _key()
    nxt, remaining = await routes_proxy._next_fallback_candidate(
        [FB_MODEL], MODEL, key, "chat", 10, "req-mixed-walk", time.monotonic()
    )
    assert nxt is not None, "the mixed candidate pool must be walked (pre-fix it was skipped)"
    assert remaining == []
    _, cand_ep, cand_url, _ = nxt
    assert cand_ep.model_type == "chat", f"pick() must land on the chat member: {cand_ep.id}"
    assert cand_url.endswith("/chat/completions")


@pytest.fixture
def mixed_env(monkeypatch: pytest.MonkeyPatch):
    """Full TestClient env whose PRIMARY model is a mixed pool (embedding
    first) plus a registered embedding-only model — the chat-admission gate
    under test, in both its directions."""
    import pcai_llm.api.auth as auth_mod

    key = _key()

    async def fake_authenticate(plaintext: str) -> KeyRecord:
        return key

    monkeypatch.setattr(auth_mod, "authenticate", fake_authenticate)
    _wire_budgets(monkeypatch)

    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url.path.endswith("/chat/completions")
        return httpx2.Response(200, json={**_CHAT_OK, "id": "cmpl-mixed", "model": "mixed"})

    mock_router = Router.with_mock(handler)
    # FIRST endpoint embedding, second chat — the endpoints[0] trap. Priority
    # keeps pool.pick() on the chat member so the request lands sanely.
    emb = _ep("emb-first", model="mixed", mtype="embedding", url="http://mixed/v1")
    chat = _ep("chat-second", model="mixed", mtype="chat", url="http://mixed/v1")
    emb.priority = 1
    mock_router.set_endpoints("mixed", [emb, chat])
    mock_router.set_endpoints("emb-only", [_ep("e1", model="emb-only", mtype="embedding", url="http://x/v1")])
    monkeypatch.setattr(routes_proxy, "model_router", mock_router)
    # serves_chat (the gate's predicate) resolves pools through CASCADE'S OWN
    # model_router binding — patch it too, or the gate reads the live router
    # (no pools -> unknown-kept -> the gate would be a no-op in the test).
    monkeypatch.setattr(cascade_mod, "model_router", mock_router)

    app = FastAPI()
    app.include_router(routes_proxy.router)
    return TestClient(app, raise_server_exceptions=False)


def test_chat_admission_gate_accepts_mixed_pool(mixed_env) -> None:
    """A chat request to a pool whose FIRST endpoint is embedding must NOT
    400 'is a embedding endpoint, not chat' when another member is chat —
    the pre-fix endpoints[0] gate produced exactly that on a redeploy that
    re-registered endpoints in a different order."""
    tc = mixed_env
    r = tc.post(
        "/v1/chat/completions",
        json={"model": "mixed", "messages": [{"role": "user", "content": "hello"}]},
        headers={"Authorization": "Bearer hllm-x"},
    )
    assert r.status_code == 200, f"mixed pool must serve chat: {r.status_code} {r.text[:300]}"
    assert r.json()["choices"][0]["message"]["content"] == "hi"


def test_chat_admission_gate_still_rejects_non_chat_pool(mixed_env) -> None:
    """The gate still refuses a pool with NO chat member (the any() predicate
    is a fix for endpoint ORDER, not a removal of the type check)."""
    tc = mixed_env
    r = tc.post(
        "/v1/chat/completions",
        json={"model": "emb-only", "messages": [{"role": "user", "content": "hello"}]},
        headers={"Authorization": "Bearer hllm-x"},
    )
    assert r.status_code == 400, r.text[:300]
    assert "not chat" in r.text
