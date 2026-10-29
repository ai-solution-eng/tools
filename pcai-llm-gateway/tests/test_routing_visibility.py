"""Per-request routing visibility - the "which model actually served this?" line.

WHY THIS FILE EXISTS (2026-10-17): the gateway logged cascade EVENTS only
(cascade: head pick ...), which fire on a handful of requests an hour. The
decision that matters - which model actually served this request - was
invisible. On the G2 deployment that gap produced a genuinely unanswerable
incident: a client pinned to one model appeared to be answered by another,
and three separate root-cause theories (load-aware cascade, sticky head,
TTFT failover) were each chased against this codebase before being ruled out
by evidence. Two of those theories were wrong precisely because the logs
could not distinguish "pinned and served as asked" from "routed elsewhere".

The contract these tests pin:

  * EVERY chat request logs exactly one "routing ..." line at the point the
    decision is FINAL (post-cascade, pre-pool), naming requested vs served.
  * kind=pinned when the client named a registered model - the cascade
    returns that model immediately and never consults stickiness or load
    ranking (cascade.resolve_cascade, rule 1). If this ever regresses, the
    log line is how it becomes visible rather than a mystery.
  * kind=alias when the body's name resolved through the profile ordering,
    so a fallback is visibly a fallback.
  * The line carries no prompt content and no bearer key (it logs the key
    PREFIX, matching _log_request's discipline).

Mock-upstream discipline mirrors test_ttft_gate.py / test_proxy.py: an
in-process httpx2.MockTransport, no live Postgres.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

import httpx2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pcai_llm.api import routes_proxy
from pcai_llm.api.auth import KeyRecord
from pcai_llm.core import budgets as budgets_mod
from pcai_llm.core import cascade as cascade_mod
from pcai_llm.core import costs as costs_mod
from pcai_llm.core import model_fallbacks
from pcai_llm.routing.router import Endpoint, Router

PINNED = "pinned-model"


class FakeDB:
    """Budget/chain stub: no budgets, accumulates settle INSERT args."""

    def __init__(self) -> None:
        self.settled: list[tuple] = []
        self.chain_rows: dict[str, list[str]] = {}
        self.order: list[str] = []

    async def fetch(self, sql: str, *a: Any):
        if "FROM model_fallbacks" in sql:
            return [{"fallback_model": n} for n in self.chain_rows.get(a[0], [])]
        return []

    async def fetchrow(self, sql: str, *a: Any):
        if "profiles" in sql:
            return {"model_order": self.order} if self.order else None
        if "rate_windows" in sql:
            return {"requests": 1, "tokens": 10}
        return None

    async def fetchval(self, sql: str, *a: Any):
        return None

    async def execute(self, sql: str, *a: Any):
        if "usage_ledger" in sql:
            self.settled.append(a)
        return "INSERT 0 1"


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

    # The routing-config read must be deterministic here: the module-level
    # 60s cache is warmed/cold depending on which OTHER test files ran before
    # this one, and a cold cache makes get_config hit the real (unconnected)
    # DB and log a "routing config refresh failed" warning -- which also
    # starts with "routing " and broke this file's exactly-one-line count
    # depending on distant test order (found during the 2026-10-29 wave).
    # Pin the config to its defaults: no DB, no warning, stable order.
    import pcai_llm.core.routing_config as _rc

    async def _pinned_cfg():
        return dict(_rc.DEFAULTS)

    monkeypatch.setattr(_rc, "get_config", _pinned_cfg)

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


def _sse_bytes(text: str) -> bytes:
    delta = {"choices": [{"index": 0, "delta": {"content": text}}]}
    return b"data: " + json.dumps(delta).encode() + b"\n\n" + b"data: [DONE]\n\n"


def _ok_handler(request: httpx2.Request) -> httpx2.Response:
    return httpx2.Response(200, content=_sse_bytes("ok"), headers={"content-type": "text/event-stream"})


def _routing_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith("routing ")]


def test_pinned_request_logs_requested_equals_served(env, caplog: pytest.LogCaptureFixture) -> None:
    """THE regression guard: a pinned request is served by the model it named.

    This is the observation that was impossible before this line existed. If a
    future change routes a pinned request somewhere else (a load-aware head, a
    sticky record, a silent failover), served stops equalling requested here.
    """
    _fake, _key_rec, make = env
    tc = make(_ok_handler, {PINNED: "http://pinned-upstream/v1"})

    with caplog.at_level(logging.INFO, logger="pcai_llm.api.routes_proxy"):
        r = tc.post(
            "/v1/chat/completions",
            json={"model": PINNED, "messages": [{"role": "user", "content": "hi"}], "stream": True},
            headers=_h(),
        )

    assert r.status_code == 200, r.text
    lines = _routing_lines(caplog)
    assert len(lines) == 1, f"exactly one routing line per request, got: {lines}"

    line = lines[0]
    assert "requested=" + PINNED in line
    assert "served=" + PINNED in line
    assert "kind=pinned" in line


def test_routing_line_carries_no_prompt_or_key(env, caplog: pytest.LogCaptureFixture) -> None:
    """The audit line must never leak prompt content or the bearer key."""
    _fake, _key_rec, make = env
    tc = make(_ok_handler, {PINNED: "http://pinned-upstream/v1"})
    secret_prompt = "SUPER-SECRET-PROMPT-CONTENT"

    with caplog.at_level(logging.INFO, logger="pcai_llm.api.routes_proxy"):
        r = tc.post(
            "/v1/chat/completions",
            json={"model": PINNED, "messages": [{"role": "user", "content": secret_prompt}], "stream": True},
            headers=_h(),
        )

    assert r.status_code == 200, r.text
    joined = "\n".join(_routing_lines(caplog))
    assert secret_prompt not in joined
    assert "hllm-x" not in joined, "the raw bearer key must never be logged"
    assert "key_prefix=hllm-XX" in joined


def test_alias_request_logs_alias_kind(env, caplog: pytest.LogCaptureFixture) -> None:
    """An alias resolves through the profile ordering, and the line says so."""
    fake, _key_rec, make = env
    fake.order = [PINNED]
    tc = make(_ok_handler, {PINNED: "http://pinned-upstream/v1"})

    with caplog.at_level(logging.INFO, logger="pcai_llm.api.routes_proxy"):
        r = tc.post(
            "/v1/chat/completions",
            json={"model": "auto", "messages": [{"role": "user", "content": "hi"}], "stream": True},
            headers=_h(),
        )

    assert r.status_code == 200, r.text
    line = _routing_lines(caplog)[0]
    assert "requested=auto" in line
    assert "served=" + PINNED in line
    assert "kind=alias" in line
