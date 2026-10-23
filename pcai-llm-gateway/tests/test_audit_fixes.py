"""Regression tests for the sproket-audit fixes:

1. List-input embeddings BYPASS the cache (a hit would return 1 embedding
   for an N-item request — silently corrupt for RAG batching).
2. Streaming requests HOLD their concurrency slot for the stream lifetime,
   not just until the response object is constructed.
3. Streaming responses relay the caller's X-Budget-Warning header.
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx2
import pytest
from fastapi.testclient import TestClient

from pcai_llm.api import routes_proxy
from pcai_llm.api.auth import KeyRecord
from pcai_llm.core import budgets as budgets_mod
from pcai_llm.core import costs as costs_mod
from pcai_llm.core import embedcache
from pcai_llm.routing.router import Endpoint, Pool, Router

MODEL = "audit-model"


class FakeDB:
    def __init__(self) -> None:
        self.cache_rows: dict[str, Any] = {}
        self.budget_budgets: list[dict] = []
        self.budget_spent = 0

    async def fetch(self, sql: str, *a: Any):
        if "FROM budgets" in sql:
            # single aggregate query: budget rows joined to window spend
            return [{**b, "spent": self.budget_spent} for b in self.budget_budgets]
        if "FROM cost_budgets" in sql:
            return []
        return []

    async def fetchrow(self, sql: str, *a: Any):
        if "profiles" in sql:
            return None
        return {"requests": 1, "tokens": 10}

    async def fetchval(self, sql: str, *a: Any):
        if "embedding_cache" in sql:
            return self.cache_rows.get("embedding")
        if "SUM" in str(sql):
            return self.budget_spent
        return None

    async def execute(self, sql: str, *a: Any):
        if "embedding_cache" in sql:
            self.cache_rows["last_stored"] = a
        return "INSERT 0 1"


def _key(**over: Any) -> KeyRecord:
    base: dict[str, Any] = {
        "id": uuid.uuid4(),
        "user_id": uuid.uuid4(),
        "name": "k",
        "key_prefix": "hllm-XX",
        "scopes": ["proxy"],
        "allowed_models": [],
        "rate_limit_rpm": 1000,
        "rate_limit_tpm": 1_000_000,
        "max_concurrent": 0,
        "expires_at": None,
    }
    base.update(over)
    return KeyRecord(**base)


def _client(
    monkeypatch: pytest.MonkeyPatch, key: KeyRecord, fake: FakeDB, handler, model_type: str = "embedding"
) -> TestClient:
    import pcai_llm.api.auth as auth_mod
    import pcai_llm.core.cascade as cascade_mod

    async def fake_auth(p: str) -> KeyRecord:
        return key

    monkeypatch.setattr(auth_mod, "authenticate", fake_auth)
    monkeypatch.setattr(budgets_mod, "db", fake)
    monkeypatch.setattr(costs_mod, "db", fake)
    monkeypatch.setattr(cascade_mod, "db", fake)
    monkeypatch.setattr(embedcache, "db", fake)
    monkeypatch.setattr(routes_proxy, "db", fake)
    r = Router.with_mock(handler)
    r.set_endpoints(MODEL, [Endpoint(id="ep", model_name=MODEL, base_url="http://fake/v1", model_type=model_type)])
    monkeypatch.setattr(routes_proxy, "model_router", r)
    from pcai_llm.app import create_app

    return TestClient(create_app(), raise_server_exceptions=False)


def test_list_input_bypasses_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """List input + a poisoned cache row for that key → upstream IS called
    and the FULL response (N embeddings) is relayed, not data[0]."""
    fake = FakeDB()
    fake.cache_rows["embedding"] = [9.9]  # poisoned row
    calls = {"n": 0}

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls["n"] += 1
        n = 3  # upstream echoes 3 embeddings for the 3-item input
        return httpx2.Response(
            200,
            json={
                "object": "list",
                "model": MODEL,
                "data": [{"object": "embedding", "index": i, "embedding": [float(i)]} for i in range(n)],
                "usage": {"prompt_tokens": 9, "total_tokens": 9},
            },
        )

    tc = _client(monkeypatch, _key(), fake, handler)
    r = tc.post(
        "/v1/embeddings", json={"model": MODEL, "input": ["a", "b", "c"]}, headers={"Authorization": "Bearer x"}
    )
    assert r.status_code == 200
    assert len(r.json()["data"]) == 3, "list input must relay ALL embeddings"
    assert calls["n"] == 1, "list input must reach upstream (cache bypass)"
    assert r.headers.get("x-cache") is None
    # and nothing was stored under the unstable str(list) key
    assert "last_stored" not in fake.cache_rows


def test_string_input_still_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeDB()
    calls = {"n": 0}

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls["n"] += 1
        return httpx2.Response(
            200,
            json={
                "object": "list",
                "model": MODEL,
                "data": [{"object": "embedding", "index": 0, "embedding": [0.5]}],
                "usage": {"prompt_tokens": 2, "total_tokens": 2},
            },
        )

    tc = _client(monkeypatch, _key(), fake, handler)
    r1 = tc.post("/v1/embeddings", json={"model": MODEL, "input": "hello"}, headers={"Authorization": "Bearer x"})
    assert r1.status_code == 200 and calls["n"] == 1
    assert "last_stored" in fake.cache_rows, "string input must be cached"


def test_relay_sse_releases_slot_at_generator_end(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unit contract: _relay_sse holds the slot until the generator exits.

    Direct async test — no ASGI transport involved (starlette's TestClient
    buffers streaming bodies before the with-block runs, so mid-stream
    server state is unobservable through it; the timing contract must be
    pinned at the generator level).
    """
    import asyncio
    import time

    fake = FakeDB()
    chunk1 = b'data: {"model":"audit-model","choices":[{"delta":{"content":"hi"}}]}\n\n'
    chunk2 = b"data: [DONE]\n\n"
    parked = asyncio.Event()

    async def chunked() -> Any:
        yield chunk1
        await parked.wait()  # deterministic mid-stream suspension
        yield chunk2

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, content=chunked(), headers={"content-type": "text/event-stream"})

    key = _key()
    r = Router.with_mock(handler)
    r.set_endpoints(MODEL, [Endpoint(id="ep", model_name=MODEL, base_url="http://fake/v1")])
    # this test bypasses _client, so patch the ledger's settle path directly
    fake = FakeDB()
    monkeypatch.setattr(budgets_mod, "db", fake)
    monkeypatch.setattr(costs_mod, "db", fake)

    async def run() -> None:
        req = r.client.build_request("POST", "http://fake/v1/chat/completions", json={})
        resp = await r.client.send(req, stream=True)
        releases: list[int] = []
        ledger = routes_proxy._StreamLedger(key, MODEL, 10, "rid", time.monotonic())
        agen = routes_proxy._relay_sse(resp, ledger, release_slot=lambda: releases.append(1))
        first = await agen.__anext__()
        assert first == chunk1
        # parked mid-stream, one chunk delivered: slot MUST still be held
        assert releases == [], "slot released while the stream is mid-flight"
        parked.set()
        rest = b"".join([chunk2]) if False else b""
        async for c in agen:
            rest += c
        assert b"[DONE]" in rest
        assert releases == [1], "exactly one release at generator end"
        await resp.aclose()

    asyncio.run(run())


def test_handler_transfers_slot_only_for_streams(monkeypatch: pytest.MonkeyPatch) -> None:
    """Handler wiring: stream requests pass release_slot into _stream_chat
    (ownership transfer); non-stream requests release in the handler's own
    finally and _proxy_json gets no release_slot."""
    fake = FakeDB()
    key = _key(max_concurrent=1)

    def sse_handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, content=b"data: [DONE]\n\n", headers={"content-type": "text/event-stream"})

    tc = _client(monkeypatch, key, fake, sse_handler, model_type="chat")

    stream_calls: list[Any] = []
    json_calls: list[Any] = []

    async def spy_stream(*args: Any, **kwargs: Any) -> Any:
        stream_calls.append(kwargs.get("release_slot"))
        from fastapi import Response

        return Response(content=b"data: [DONE]\n\n", media_type="text/event-stream")

    async def spy_json(*args: Any, **kwargs: Any) -> Any:
        json_calls.append(kwargs.get("cache_input"))
        from fastapi import Response

        return Response(content=b"{}", media_type="application/json")

    monkeypatch.setattr(routes_proxy, "_stream_chat", spy_stream)
    monkeypatch.setattr(routes_proxy, "_proxy_json", spy_json)
    routes_proxy._in_flight.clear()

    r = tc.post(
        "/v1/chat/completions",
        json={"model": MODEL, "messages": [{"role": "user", "content": "x"}], "stream": True},
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
    assert stream_calls and stream_calls[0] is not None, "stream request must transfer slot ownership to _stream_chat"

    # the spy bypasses the real generator (which would release the slot),
    # so reset the counter to simulate a healthy prior stream
    routes_proxy._in_flight[key.id] = 0
    r2 = tc.post(
        "/v1/chat/completions",
        json={"model": MODEL, "messages": [{"role": "user", "content": "x"}]},
        headers={"Authorization": "Bearer x"},
    )
    assert r2.status_code == 200
    assert routes_proxy._in_flight.get(key.id, 0) == 0, "non-stream must self-release"


def test_stream_replays_budget_warning_header(monkeypatch: pytest.MonkeyPatch) -> None:
    """X-Budget-Warning must survive the streaming path (was dropped)."""
    fake = FakeDB()
    fake.budget_budgets = [{"model": "*", "period": "month", "limit_tokens": 100000}]
    fake.budget_spent = 97000  # 97%

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, content=b"data: [DONE]\n\n", headers={"content-type": "text/event-stream"})

    tc = _client(monkeypatch, _key(), fake, handler, model_type="chat")
    with tc.stream(
        "POST",
        "/v1/chat/completions",
        json={"model": MODEL, "messages": [{"role": "user", "content": "x"}], "stream": True},
        headers={"Authorization": "Bearer x"},
    ) as r:
        assert r.status_code == 200
        assert r.headers.get("x-budget-warning", "").startswith("95%")


def test_no_healthy_upstream_releases_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression (audit bug 4): the 503 no-healthy-upstream path raised
    AFTER the concurrency increment without releasing — one outage with
    max_concurrent=1 permanently consumed the key's slot."""
    fake = FakeDB()
    key = _key(max_concurrent=1)

    state = {"healthy": False}

    def flaky_handler(request: httpx2.Request) -> httpx2.Response:
        if not state["healthy"]:
            raise AssertionError("upstream must never be reached while unhealthy")
        return httpx2.Response(
            200,
            json={"choices": [{"message": {"content": "ok"}}], "usage": {"prompt_tokens": 1, "completion_tokens": 1}},
        )

    tc = _client(monkeypatch, key, fake, flaky_handler)
    # register the model with an endpoint, then cool it down = no healthy upstream
    ep = Endpoint(id="ep", model_name=MODEL, base_url="http://fake/v1")
    ep.cooldown_until = float("inf")
    routes_proxy.model_router.set_endpoints(MODEL, [ep])
    routes_proxy._in_flight.clear()

    r = tc.post(
        "/v1/chat/completions",
        json={"model": MODEL, "messages": [{"role": "user", "content": "x"}]},
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 503
    assert routes_proxy._in_flight.get(key.id, 0) == 0, "slot leaked on 503"

    # endpoint recovers (cooldown expires) → the key is NOT bricked
    ep.cooldown_until = 0.0
    state["healthy"] = True
    r2 = tc.post(
        "/v1/chat/completions",
        json={"model": MODEL, "messages": [{"role": "user", "content": "x"}]},
        headers={"Authorization": "Bearer x"},
    )
    assert r2.status_code == 200


# ---------------------------------------------------------------------------
# Audit round 2 fixes (chart P0, asyncpg.Json P0, registry stale pools)
# ---------------------------------------------------------------------------


def test_settings_parses_chart_rendered_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """P0 regression: Helm renders numbers >= 1e6 in scientific notation
    ("3.3554432e+07"); pydantic int must still parse the env values the
    chart actually produces (verified with helm template)."""
    from pcai_llm.config import Settings

    s = Settings(
        database_url="postgresql://t:t@localhost/t",
        max_body_bytes=int(float("3.3554432e+07")),  # what helm emits
        embedding_cache_max_input_bytes=int(float("1.048576e+08")),
    )
    assert s.max_body_bytes == 33554432
    # Residual sharp edge (documented): the scientific-notation STRING form
    # is still unparsable by pydantic int — the chart's | int fix ensures
    # that form never reaches the app in practice.
    import pydantic

    monkeypatch.setenv("PCAI_LLM_MAX_BODY_BYTES", "3.3554432e+07")
    with pytest.raises(pydantic.ValidationError):
        Settings(database_url="postgresql://t:t@localhost/t")


def test_client_tool_dispatch_chart_env_parses(monkeypatch: pytest.MonkeyPatch) -> None:
    """Chart wiring for the wave-B2 dispatch flag (ROADMAP §7): the
    deployment template renders PCAI_LLM_CLIENT_TOOL_DISPATCH (bool) and
    PCAI_LLM_CLIENT_TOOL_DISPATCH_TIMEOUT_S (float, chart emits '120' —
    helm drops the .0 like every other float knob). Both must parse from
    the exact rendered strings."""
    from pcai_llm.config import Settings

    monkeypatch.setenv("PCAI_LLM_CLIENT_TOOL_DISPATCH", "true")
    monkeypatch.setenv("PCAI_LLM_CLIENT_TOOL_DISPATCH_TIMEOUT_S", "120")
    s = Settings(database_url="postgresql://t:t@localhost/t")
    assert s.client_tool_dispatch is True
    assert s.client_tool_dispatch_timeout_s == 120.0

    # Flag off (the chart default): routes 404, relay handback behavior.
    monkeypatch.setenv("PCAI_LLM_CLIENT_TOOL_DISPATCH", "false")
    s2 = Settings(database_url="postgresql://t:t@localhost/t")
    assert s2.client_tool_dispatch is False


def test_replace_pools_drops_stale_models(monkeypatch: pytest.MonkeyPatch) -> None:
    """P1 regression (F2): refresh must remove pools for models that
    disappeared from the DB — stale pools were still served + advertised."""
    from pcai_llm.routing.router import Endpoint, Router

    r = Router()
    r.set_endpoints("ghost", [Endpoint(id="g", model_name="ghost", base_url="http://g/v1")])
    assert "ghost" in r.models()
    r.replace_pools({"real": [Endpoint(id="r", model_name="real", base_url="http://r/v1")]})
    assert r.models() == ["real"], "stale pool must be dropped by snapshot swap"


def test_registry_refresh_uses_snapshot_swap(monkeypatch: pytest.MonkeyPatch) -> None:
    """The registry loader must call replace_pools (whole snapshot), not
    per-model set_endpoints (which leaks stale pools)."""
    import inspect

    from pcai_llm.core import registry

    src = inspect.getsource(registry.refresh)
    assert "replace_pools" in src
    assert "for model_name, endpoints in by_model.items()" not in src


# ---------------------------------------------------------------------------
# Panel-managed credentials (Fernet-at-rest, audit round 3)
# ---------------------------------------------------------------------------


def test_secretbox_roundtrip_and_disable(monkeypatch: pytest.MonkeyPatch) -> None:
    from pcai_llm.config import settings
    from pcai_llm.core import secretbox

    # settings snapshot env at import — patch the object + clear the fernet
    # cache per scenario
    def set_master(v: str | None) -> None:
        monkeypatch.setattr(settings, "secrets_master_key", v or "")
        secretbox._fernet = None

    set_master("master-test-1")
    ct = secretbox.encrypt("sk-live-123")
    assert "sk-live-123" not in ct
    assert secretbox.decrypt(ct) == "sk-live-123"

    # master key rotation -> old ciphertext undecryptable (by design)
    set_master("master-test-2")
    assert secretbox.decrypt(ct) is None

    # no master key -> storage disabled, decrypt degrades to None
    set_master(None)
    assert secretbox.enabled() is False
    assert secretbox.decrypt(ct) is None
    import pytest as _pytest

    with _pytest.raises(RuntimeError):
        secretbox.encrypt("x")


def test_upstream_headers_resolution_order(monkeypatch: pytest.MonkeyPatch) -> None:
    """Panel ciphertext wins over by-ref secret; decrypt-failure degrades."""
    from pcai_llm.api.routes_proxy import _upstream_headers
    from pcai_llm.config import settings
    from pcai_llm.core import secretbox as sb
    from pcai_llm.routing.router import Endpoint

    monkeypatch.setattr(settings, "secrets_master_key", "m1")
    sb._fernet = None
    enc = sb.encrypt("sk-panel-key")

    ep = Endpoint(id="e", model_name="m", base_url="http://x", api_key_secret="by-ref-name", auth_token_enc=enc)
    monkeypatch.setenv("PCAI_LLM_UPSTREAM_KEY__BY_REF_NAME", "sk-env-key")
    h = _upstream_headers(ep, "rid")
    assert h["Authorization"] == "Bearer sk-panel-key"  # panel wins

    # undecryptable panel credential (rotated master) -> falls to by-ref
    monkeypatch.setattr(settings, "secrets_master_key", "m2")
    sb._fernet = None
    h2 = _upstream_headers(ep, "rid")
    assert h2["Authorization"] == "Bearer sk-env-key"

    # no credentials at all -> no Authorization header
    ep3 = Endpoint(id="e3", model_name="m", base_url="http://x")
    assert "Authorization" not in _upstream_headers(ep3, "rid")
    monkeypatch.setattr(settings, "secrets_master_key", "m1")
    sb._fernet = None


def test_pick_weight_zero_is_standby() -> None:
    """weight 0 = standby (receives no load-balanced traffic); all-zero tier
    = deterministic list order (strict priority, no randomization)."""
    import asyncio

    a, b, c = (Endpoint(id=n, model_name="m", base_url=f"http://{n}", weight=0) for n in ("a", "b", "c"))
    pool = Pool(endpoints=[a, b, c])
    for _ in range(30):
        ep = pool.pick()
        assert ep is not None and ep.id == "a"  # deterministic: registration order

    # standby: zero-weight endpoint is skipped while weighted ones are healthy
    a2 = Endpoint(id="a2", model_name="m", base_url="http://a2", weight=3)
    s2 = Endpoint(id="standby", model_name="m", base_url="http://s", weight=0)
    pool2 = Pool(endpoints=[s2, a2])
    for _ in range(20):
        ep = pool2.pick()
        assert ep is not None and ep.id == "a2"  # standby gets no balanced traffic

    # ...but serves via failover when the weighted ones are down
    async def cool() -> None:
        for _ in range(5):
            await pool2.report(a2, False)

    asyncio.run(cool())
    standby = pool2.pick()
    assert standby is not None and standby.id == "standby"


def test_registry_selects_cover_row_accesses() -> None:
    """P0 regression (0.1.4 boot crash): the registry cache build read
    row['enabled'] but the SELECT never fetched it — KeyError on every boot,
    gateway crash-looped. Every row[...] access must be covered by a column
    in some SELECT in the module."""
    import inspect
    import re as _re

    from pcai_llm.core import registry

    src = inspect.getsource(registry)
    selects = " ".join(_re.findall(r"SELECT (.*?) FROM", src, _re.DOTALL))
    cols = set(_re.findall(r"(\w+)[,\s]", selects)) | {"id"}
    accessed = set(_re.findall(r'row\["(\w+)"\]', src))
    missing = [c for c in accessed if c not in cols]
    assert not missing, f"row accesses without SELECT coverage: {missing}"
    # and the exact bug: the mcp_servers SELECT must carry enabled
    assert "enabled" in selects


def test_server_allows_policy() -> None:
    """Server tool-policy: 'all' admits everything, 'allowlist' exact list,
    unknown policy denies (fail closed)."""
    from pcai_llm.mcpf.federation import server_allows

    assert server_allows({"tool_policy": "all"}, "anything")
    assert server_allows({"tool_policy": "allowlist", "allowed_tools": ["x"]}, "x")
    assert not server_allows({"tool_policy": "allowlist", "allowed_tools": ["x"]}, "y")
    assert not server_allows({}, "x")  # no policy field -> deny
    assert not server_allows({"tool_policy": "weird"}, "x")


def test_ui_scripts_parse_clean() -> None:
    """P0 regression: a python-edit orphaned a `||` fallback in admin.html's
    script — SyntaxError killed the WHOLE console (page rendered, zero
    behavior). Both UI scripts must parse under a real JS engine."""
    import re as _re
    import subprocess

    for ui in ("admin.html", "index.html"):
        src = open(f"src/pcai_llm/ui/{ui}", encoding="utf-8").read()
        match = _re.search(r"<script>(.*)</script>", src, _re.DOTALL)
        assert match is not None, f"{ui} has no inline <script> block"
        script = match.group(1)
        path = f"/tmp/ui-check-{ui.replace('.', '-')}.js"
        open(path, "w").write(script)
        r = subprocess.run(["node", "--check", path], capture_output=True, text=True)
        assert r.returncode == 0, f"{ui} script has a syntax error:\n{r.stderr[:500]}"


def test_registered_server_defaults_to_all_tools() -> None:
    """Regression: seed-style registration (no tool_policy sent) must ADMIT
    tools — the old 'allowlist'+empty default meant servers admitted nothing
    and every grant picker offered zero tools ('can't add mcp servers')."""

    from pcai_llm.mcpf.federation import server_allows

    # what the API upserts when the body omits tool_policy: default 'all'
    target = {"tool_policy": "all", "allowed_tools": []}
    assert server_allows(target, "any_tool")

    # the schema default agrees (fresh registrations inherit 'all')
    import re

    from pcai_llm.db.schema import SCHEMA_SQL

    m = re.search(r"tool_policy TEXT NOT NULL DEFAULT '(\w+)'", SCHEMA_SQL)
    assert m and m.group(1) == "all"

    # the empty-allowlist migration flips legacy broken rows to 'all'
    assert "UPDATE mcp_servers SET tool_policy = 'all'" in SCHEMA_SQL
    assert "allowed_tools_json IN ('[]', '', NULL)" in SCHEMA_SQL


def test_console_save_handler_present() -> None:
    """P0 regression: the Save-profile handler was deleted as collateral in a
    dead-code removal (the button silently did nothing on 0.1.15-0.1.17).
    The wiring must exist in the page source."""
    s = open("src/pcai_llm/ui/admin.html", encoding="utf-8").read()
    assert "$('p-save').onclick" in s
    assert "await api('/profiles'" in s
    # and every critical control has SOME wiring
    for btn in ("k-create", "m-save", "mo-save", "connect", "p-addbudget"):
        assert f"getElementById('{btn}')" in s or f"$('{btn}')" in s, f"{btn} unwired"


def test_cost_budget_stops_the_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cost budgets: refused when USD spend reaches the limit, whichever
    limit (tokens or USD) hits first."""
    import asyncio
    import uuid as u

    from pcai_llm.core import budgets as bm

    class BudgetDB:
        def __init__(self, usd_budgets, spent_usd):
            self.usd_budgets = usd_budgets
            self.spent_usd = spent_usd

        async def fetch(self, sql, *a):
            if "FROM budgets" in sql:
                return []  # no token budgets
            if "FROM cost_budgets" in sql:
                # single aggregate query: budget rows joined to window spend
                return [{**b, "spent": self.spent_usd} for b in self.usd_budgets]
            return []

        async def fetchval(self, sql, *a):
            if "SUM(cost_usd)" in sql:
                return self.spent_usd
            return 0

    kid = u.uuid4()
    # under the limit -> ok
    monkeypatch.setattr(bm, "db", BudgetDB([{"model": "*", "period": "month", "limit_usd": 10.0}], 9.99))
    res = asyncio.run(bm.check_budgets(kid, "m", 0))
    assert res.ok

    # at/over the limit -> refused with the dollar reason
    monkeypatch.setattr(bm, "db", BudgetDB([{"model": "*", "period": "month", "limit_usd": 10.0}], 10.01))
    res = asyncio.run(bm.check_budgets(kid, "m", 0))
    assert not res.ok and "cost budget" in res.reason

    # warn thresholds fire on USD too
    monkeypatch.setattr(bm, "db", BudgetDB([{"model": "*", "period": "month", "limit_usd": 10.0}], 9.6))
    res = asyncio.run(bm.check_budgets(kid, "m", 0))
    assert res.ok and res.warn == 95
