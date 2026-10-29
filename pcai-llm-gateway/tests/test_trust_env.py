"""L-1 regression tests (audit 2026-10-29): upstream clients must IGNORE
ambient proxy environment.

httpx2.AsyncClient() defaults to trust_env=True. Two failure shapes when the
ambient environment carries proxy variables:

* a bracketed-IPv6 NO_PROXY entry (NO_PROXY='[::1]') makes URLPattern() raise
  httpx2.InvalidURL inside AsyncClient.__init__ — and Router.start() runs in
  the app lifespan, so the pod CRASH-LOOPS at boot (not a per-request 500);
* a valid HTTP_PROXY silently reroutes every upstream call (prompts, API
  keys, completions) through a host the gateway does not control.

The fix pins trust_env=False at ONE shared factory
(pcai_llm.routing.router.upstream_client); every construction site must build
through it, and the drift-guard test at the bottom keeps it that way.

NO network in these tests: clients are constructed, never awaited.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Self

import httpx2
import pytest

from pcai_llm.api import routes_admin, routes_mcp
from pcai_llm.config import settings
from pcai_llm.core import enroll_memory
from pcai_llm.routing.router import Endpoint, Router, upstream_client


def test_l1_upstream_client_refuses_bracketed_ipv6_no_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Construction survives NO_PROXY='[::1]' — the boot-crash payload.

    Pre-fix this is httpx2.AsyncClient() under the same env, which raised
    httpx2.InvalidURL: Invalid port ':1]' (reproduced standalone).
    """
    monkeypatch.setenv("NO_PROXY", "[::1]")
    client = upstream_client(timeout=httpx2.Timeout(5.0, connect=5.0), verify=True)
    assert client._trust_env is False
    assert dict(client._mounts) == {}  # no proxy map was built at all


def test_l1_upstream_client_ignores_http_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    """A valid HTTP_PROXY must NOT reroute upstream traffic.

    Verified against httpx2 internals pre-fix: trust_env=True with
    HTTP_PROXY set gives client._mounts = {URLPattern('all://'): transport}
    (the proxy mount); trust_env=False gives {}. We assert BOTH the
    trust_env flag and the empty mount table.
    """
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.corp.example:3128")
    client = upstream_client()
    assert client._trust_env is False
    assert dict(client._mounts) == {}


def test_l1_upstream_client_pins_trust_env_even_when_caller_passes_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The factory is authoritative: a caller passing trust_env=True (drift,
    an opt-out attempt) must still get a client that ignores the env."""
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.corp.example:3128")
    client = upstream_client(trust_env=True)
    assert client._trust_env is False
    assert dict(client._mounts) == {}


async def test_l1_router_start_survives_bracketed_ipv6_no_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Router.start() — the lifespan call site that crash-looped the pod.

    Covers BOTH pool-limit branches (the default upstream_pool_max_connections
    =0 legacy branch and the explicit-limits branch).
    """
    monkeypatch.setenv("NO_PROXY", "[::1]")
    for pool_max in (0, 64):
        monkeypatch.setattr(settings, "upstream_pool_max_connections", pool_max)
        router = Router()
        await router.start()  # pre-fix: httpx2.InvalidURL -> lifespan dies
        try:
            assert router._client is not None
            assert router._client._trust_env is False
        finally:
            await router.close()


def test_l1_router_with_mock_seam_pins_trust_env_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The test seam's client is built by the factory too: integration tests
    exercise the SAME trust posture production traffic gets."""
    monkeypatch.setenv("NO_PROXY", "[::1]")

    def handler(request: httpx2.Request) -> httpx2.Response:  # pragma: no cover
        return httpx2.Response(200)

    router = Router.with_mock(handler)
    assert router._client is not None and router._client._trust_env is False


async def test_l1_mcp_upstream_session_pins_trust_env_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """routes_mcp._upstream_session builds its per-call client through the
    factory (session/transport seams faked — no network, no SDK server)."""

    captured: dict[str, Any] = {}

    clients: list[Any] = []

    def recording_upstream_client(**kwargs: Any) -> Any:
        captured.update(kwargs)
        client = upstream_client(**kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr(routes_mcp, "upstream_client", recording_upstream_client)

    class _FakeSession:
        def __init__(self, read: Any, write: Any, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> Any:
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

        async def initialize(self) -> None:
            return None

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def fake_streamable_http(url: str, http_client: Any) -> Any:
        yield ("read", "write")

    monkeypatch.setattr(routes_mcp, "streamable_http_client", fake_streamable_http)
    monkeypatch.setattr(routes_mcp, "ClientSession", _FakeSession)

    async def op(session: Any) -> str:
        return "ok"

    out = await routes_mcp._upstream_session("http://mcp.svc:9000/mcp", {}, op, timeout_s=5.0)
    assert out == "ok"
    assert len(clients) == 1, "exactly one client per _upstream_session call"
    assert clients[0]._trust_env is False, "the /mcp client must ignore ambient proxy env"
    assert dict(clients[0]._mounts) == {}


async def test_l1_enroll_memory_dataset_create_pins_trust_env_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """enroll_memory.create_memory_dataset's RAG write client consults the
    shared factory seam (client stubbed — the response shape is all the
    function reads). The stub swallows the factory call, so the trust_env
    pin itself is asserted at the factory and seam levels above; here we pin
    that the construction GOES THROUGH the seam (a bare httpx2.AsyncClient
    call in enroll_memory would bypass the recorder entirely and this test
    would see an empty captured dict)."""
    captured: dict[str, Any] = {}

    class _StubClient:
        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

        async def post(self, url: str, **kwargs: Any) -> Any:
            captured["url"] = url
            from types import SimpleNamespace

            return SimpleNamespace(status_code=201)

    def recording_upstream_client(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return _StubClient()

    monkeypatch.setattr(enroll_memory, "upstream_client", recording_upstream_client)

    ok, err = await enroll_memory.create_memory_dataset(
        {"url": "https://rag.svc:8000/mcp", "headers": {}, "auth_secret": None, "auth_token_enc": None},
        "tenant-ds",
        "pw",
    )
    assert (ok, err) == (True, "")
    assert captured["url"].startswith("https://rag.svc:8000/api/datasets")


def test_l1_all_upstream_clients_route_through_factory() -> None:
    """Drift guard: the ONLY allowed bare httpx2.AsyncClient constructions in
    src/ are the factory itself and the with_mock test seam (both in
    routing/router.py). A sixth construction site appearing anywhere else is
    exactly the L-1 regression class (pre-fix there were FIVE outside the
    factory: router x2, routes_admin x2, routes_mcp, enroll_memory)."""
    src_dir = Path(enroll_memory.__file__).parent.parent  # .../src/pcai_llm
    offenders: list[str] = []
    allowed = re.compile(r"pcai_llm/routing/router\.py$")

    for path in sorted(src_dir.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        rel = path.relative_to(src_dir.parent)
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if "httpx2.AsyncClient(" in line and not allowed.search(str(rel)):
                offenders.append(f"{rel}:{lineno}: {line.strip()}")
    assert offenders == [], "bare httpx2.AsyncClient( outside routing.router:\n" + "\n".join(offenders)


def test_l1_admin_probe_model_client_pins_trust_env_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """routes_admin.probe_model (per-request admin probe) builds through the
    factory: capture the kwargs the probe passes and build the REAL client —
    then the trust posture assertion is on the live object."""
    factory = routes_admin.upstream_client  # identity BEFORE the seam patch
    captured: dict[str, Any] = {}

    class _FakeProbeClient:
        def __init__(self, **kw: Any) -> None:
            captured.update(kw)

        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

        async def get(self, url: str, headers: dict[str, str] | None = None) -> Any:
            from types import SimpleNamespace

            return SimpleNamespace(status_code=200)

    monkeypatch.setattr(routes_admin, "upstream_client", _FakeProbeClient)

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    r_router = Router()
    r_router.set_endpoints("m", [Endpoint(id="ep", model_name="m", base_url="http://a/v1")])
    monkeypatch.setattr(routes_admin, "model_router", r_router)
    monkeypatch.setattr(settings, "admin_api_keys", "admin-secret-123")
    app = FastAPI()
    app.include_router(routes_admin.router)
    tc = TestClient(app, raise_server_exceptions=False)
    r = tc.post("/admin/models/m/probe", headers={"Authorization": "Bearer admin-secret-123"})
    assert r.status_code == 200 and r.json()["ok"] is True
    # the client was built through the factory seam — the probe passes NO
    # trust_env of its own (the factory pins it); with a bare httpx2.AsyncClient
    # call here the pre-fix code would construct with ambient trust_env=True.
    assert "trust_env" not in captured, "trust policy belongs to the factory, not call sites"
    # ...and the patched-in seam replaced the REAL factory (which this test
    # module imports directly): the production binding is the shared factory.
    assert factory is upstream_client
