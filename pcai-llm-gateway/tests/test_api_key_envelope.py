"""The X-API-Key credential envelope (brief §Z1, 2026-10-06).

Pins the PRECEDENCE MATRIX for the SSO envelope on BOTH auth surfaces —
the REST dependency ``pcai_llm.api.auth.require_key`` and the pure-ASGI
``/mcp`` wrapper ``routes_mcp._McpAuthASGI`` — through the ONE shared
resolver (``auth.resolve_bearer_key``) so the surfaces cannot drift.

Fleet convention D19, DE-ESCALATION-ONLY: an explicit ``X-API-Key``
outranks the co-forwarded proxy ``Authorization: Bearer``. Behind the
edge SSO chain the oauth2-proxy injects a 30-minute identity Bearer into
every forwarded request; the X-API-Key is the caller's own long-lived
ledger key for the SAME subject.

THE FAIL-CLOSED RULE (the brief's final word): an X-API-Key that fails
authenticate() is a hard 401 regardless of the co-forwarded Bearer. The
fall-through rule exists for INVALID TOKENS (the proxy envelope), never
for a failed EXPLICIT key — and even in the one bearer-key-valid shape,
two keys presented with one dead REFUSE (never silently rescued).

Flag-off byte-identity: every no-envelope row below exercises the
pre-envelope ladder and must match today's behavior exactly.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import fastapi
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pcai_llm.api import routes_admin, routes_mcp
from pcai_llm.api.auth import require_scope
from pcai_llm.config import settings
from pcai_llm.core import identity
from pcai_llm.core.identity import browser_identity_from_headers
from pcai_llm.core.keys import KeyRecord

# ---------------------------------------------------------------------------
# harness: the local IdP + token factory (the test_identity_phaseb pattern)
# ---------------------------------------------------------------------------

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_JWK = jwt.algorithms.RSAAlgorithm.to_jwk(_KEY.public_key(), as_dict=True)
_JWK["kid"] = "test-key-1"
_JWK["use"] = "sig"
_JWKS_DOC = {"keys": [_JWK]}


def _sign(claims: dict[str, Any], kid: str = "test-key-1") -> str:
    return jwt.encode(claims, _KEY, algorithm="RS256", headers={"kid": kid})


def _claims(**over: Any) -> dict[str, Any]:
    now = datetime.now(UTC)
    base = {
        "iss": "https://idp.example/realms/pcai",
        "aud": "pcai-gateway",
        "sub": "user-alice-1",
        "email": "alice@corp",
        "groups": ["eng"],
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=30)).timestamp()),
    }
    base.update(over)
    return base


def _expired_token() -> str:
    return _sign(_claims(exp=int((datetime.now(UTC) - timedelta(minutes=1)).timestamp())))


@pytest.fixture(autouse=True)
def _token_env(monkeypatch: pytest.MonkeyPatch):
    """Token-configured posture + clean JWKS cache, exactly the phaseb
    autouse fixture (the envelope tests run the SAME IdP posture)."""
    monkeypatch.setattr(settings, "token_issuer", "https://idp.example/realms/pcai")
    monkeypatch.setattr(settings, "token_jwks_url", "https://idp.example/realms/pcai/protocol/openid-connect/certs")
    monkeypatch.setattr(settings, "token_audience", "pcai-gateway")
    monkeypatch.setattr(settings, "trust_proxy_identity", False)
    monkeypatch.setattr(settings, "token_shadow_keys", False)
    monkeypatch.setattr(settings, "token_shadow_tenant_id", str(uuid.uuid4()))
    monkeypatch.setattr(settings, "sso_key_create_enabled", False)
    monkeypatch.setattr(settings, "admin_groups", "")
    monkeypatch.setattr(identity, "_fetch_jwks", lambda url: _JWKS_DOC)
    identity._reset_token_cache()
    yield
    identity._reset_token_cache()


def _key(**over: Any) -> KeyRecord:
    base: dict[str, Any] = {
        "id": uuid.uuid4(),
        "user_id": uuid.uuid4(),
        "name": "k",
        "key_prefix": "hllm-XX",
        "scopes": ["proxy", "mcp"],
        "allowed_models": [],
        "rate_limit_rpm": 60,
        "rate_limit_tpm": 100_000,
        "max_concurrent": 0,
        "expires_at": None,
    }
    base.update(over)
    return KeyRecord(**base)


class TableDB:
    """A quiet db fake (no subjects rows): the envelope tests key off the
    authenticate TABLE, not the binding ladder."""

    async def fetchrow(self, sql: str, *args: Any):
        return None

    async def fetch(self, sql: str, *args: Any):
        return []

    async def fetchval(self, sql: str, *args: Any):
        return 1

    async def execute(self, sql: str, *args: Any):
        return "UPDATE 0"


def _rest_client(monkeypatch: pytest.MonkeyPatch, table: dict[str, KeyRecord | None]) -> TestClient:
    """The phaseb _key_app pattern: require_scope("proxy") behind a table
    authenticate seam."""
    import pcai_llm.api.auth as auth_mod

    async def fake_auth(plaintext: str) -> KeyRecord | None:
        return table.get(plaintext)

    monkeypatch.setattr(auth_mod, "authenticate", fake_auth)
    monkeypatch.setattr(identity, "db", TableDB())
    app = FastAPI()

    @app.get("/probe")
    async def probe(
        request: fastapi.Request, key: KeyRecord = fastapi.Depends(require_scope("proxy"))
    ) -> dict[str, Any]:
        return {"key": str(key.id)}

    return TestClient(app, raise_server_exceptions=False)


def _mcp_client(monkeypatch: pytest.MonkeyPatch, table: dict[str, KeyRecord | None]) -> TestClient:
    """The /mcp surface with the SAME table seam (the phaseb wrapper-test
    pattern: fresh server per test — the SDK refuses run() twice)."""

    async def fake_auth(plaintext: str) -> KeyRecord | None:
        return table.get(plaintext)

    monkeypatch.setattr(routes_mcp, "authenticate", fake_auth)
    monkeypatch.setattr(identity, "db", TableDB())
    app = FastAPI()
    routes_mcp.mount_server(app, routes_mcp._new_gateway_server())
    return TestClient(app, raise_server_exceptions=False)


_MCP_PV = "2026-07-28"


def _rpc(i: int = 1) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": i,
        "method": "tools/list",
        "params": {
            "_meta": {
                "io.modelcontextprotocol/protocolVersion": _MCP_PV,
                "io.modelcontextprotocol/clientCapabilities": {},
            }
        },
    }


def _mcp_headers(bearer: str, api_key: str | None = None) -> dict[str, str]:
    headers = {
        "mcp-protocol-version": _MCP_PV,
        "mcp-method": "tools/list",
    }
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    if api_key is not None:
        headers["X-API-Key"] = api_key
    return headers


def _scenario(table: dict[str, KeyRecord | None]) -> tuple[dict[str, KeyRecord | None], str, KeyRecord, str]:
    """One live key + its plaintext in the table; returns (table, key
    plaintext, key record, a dead (unknown) key plaintext)."""
    k = _key()
    dead = "hllm-" + uuid.uuid4().hex
    table[k.key_prefix] = k
    return table, k.key_prefix, k, dead


# ---------------------------------------------------------------------------
# 1. REST precedence matrix (require_key)
# ---------------------------------------------------------------------------


def test_rest_api_key_only_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    """Envelope alone (the SPA behind the SSO chain): the minted key
    authorizes — no Authorization header involved."""
    table, plaintext, k, _dead = _scenario({})
    tc = _rest_client(monkeypatch, table)
    r = tc.get("/probe", headers={"X-API-Key": plaintext})
    assert r.status_code == 200, r.text
    assert r.json() == {"key": str(k.id)}


def test_rest_bearer_only_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bearer alone: today's behavior byte-for-byte (flag-off oracle)."""
    table, plaintext, k, _dead = _scenario({})
    tc = _rest_client(monkeypatch, table)
    r = tc.get("/probe", headers={"Authorization": f"Bearer {plaintext}"})
    assert r.status_code == 200
    assert r.json() == {"key": str(k.id)}


def test_rest_both_valid_api_key_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both valid, DIFFERENT keys: the explicit X-API-Key outranks the
    co-forwarded Bearer (D19) — the envelope key is served."""
    k_bearer = _key(name="bearer-key")
    k_envelope = _key(name="envelope-key")
    table: dict[str, KeyRecord | None] = {k_bearer.key_prefix: k_bearer, k_envelope.key_prefix: k_envelope}
    tc = _rest_client(monkeypatch, table)
    r = tc.get(
        "/probe",
        headers={"Authorization": f"Bearer {k_bearer.key_prefix}", "X-API-Key": k_envelope.key_prefix},
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"key": str(k_envelope.id)}


def test_rest_api_key_invalid_bearer_token_valid_is_401(monkeypatch: pytest.MonkeyPatch) -> None:
    """THE fail-closed rule: a DEAD X-API-Key is NEVER rescued by the
    co-forwarded proxy Bearer — even a VALID token. Two keys presented,
    one dead = refuse (brief §Z1 final word)."""
    table, _plaintext, _k, dead = _scenario({})
    tc = _rest_client(monkeypatch, table)
    r = tc.get(
        "/probe",
        headers={"Authorization": f"Bearer {_sign(_claims())}", "X-API-Key": dead},
    )
    assert r.status_code == 401
    assert "invalid, revoked, or expired key" in r.text


def test_rest_api_key_invalid_bearer_key_valid_is_401(monkeypatch: pytest.MonkeyPatch) -> None:
    """Key-vs-key, one dead: the bearer itself authenticates as a virtual
    key and the X-API-Key is dead — REFUSE (the exact exception the brief
    spells out and then refuses; fail-closed posture)."""
    table, plaintext, _k, dead = _scenario({})
    tc = _rest_client(monkeypatch, table)
    r = tc.get(
        "/probe",
        headers={"Authorization": f"Bearer {plaintext}", "X-API-Key": dead},
    )
    assert r.status_code == 401
    assert "invalid, revoked, or expired key" in r.text


def test_rest_neither_header_401(monkeypatch: pytest.MonkeyPatch) -> None:
    table, _plaintext, _k, _dead = _scenario({})
    tc = _rest_client(monkeypatch, table)
    assert tc.get("/probe").status_code == 401


def test_rest_api_key_valid_bearer_garbage_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    """Envelope + a NON-authenticable Bearer (proxy garbage / wrong
    scheme credential): the valid X-API-Key still outranks it."""
    table, plaintext, k, _dead = _scenario({})
    tc = _rest_client(monkeypatch, table)
    r = tc.get(
        "/probe",
        headers={"Authorization": "Bearer not-a-key", "X-API-Key": plaintext},
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"key": str(k.id)}


def test_rest_api_key_invalid_bearer_garbage_401(monkeypatch: pytest.MonkeyPatch) -> None:
    """Dead envelope + non-authenticable Bearer: 401 (no rescue either
    direction)."""
    table, _plaintext, _k, dead = _scenario({})
    tc = _rest_client(monkeypatch, table)
    r = tc.get("/probe", headers={"Authorization": "Bearer not-a-key", "X-API-Key": dead})
    assert r.status_code == 401


def test_rest_expired_bearer_plus_api_key_envelope_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    """The PRODUCTION shape: the proxy's expired/short-TTL id_token rides
    the Authorization header while the SPA sends its minted key in the
    envelope — the envelope authorizes; the token still resolves the
    caller for attribution (non-fatal), and the KEY is the anchor."""
    table, plaintext, k, _dead = _scenario({})
    tc = _rest_client(monkeypatch, table)
    r = tc.get(
        "/probe",
        headers={"Authorization": f"Bearer {_expired_token()}", "X-API-Key": plaintext},
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"key": str(k.id)}


def test_rest_token_only_ladder_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """No envelope: the pre-envelope ladder is intact — valid token
    without any key still 401s (anonymous-fp posture)."""
    table, _plaintext, _k, _dead = _scenario({})
    tc = _rest_client(monkeypatch, table)
    assert tc.get("/probe", headers={"Authorization": f"Bearer {_sign(_claims())}"}).status_code == 401


def test_rest_token_infra_failure_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    """No envelope + JWKS down: fail-closed 401, exactly as before the
    envelope existed (infrastructure, not a credential miss)."""

    def down(url: str) -> dict[str, Any]:
        raise RuntimeError("idp unreachable")

    table, _plaintext, _k, _dead = _scenario({})
    table.pop(next(iter(table)))  # bearer-only request: the table is empty anyway
    import pcai_llm.api.auth as auth_mod

    async def fake_auth(plaintext: str) -> KeyRecord | None:
        return table.get(plaintext)

    monkeypatch.setattr(auth_mod, "authenticate", fake_auth)
    monkeypatch.setattr(identity, "db", TableDB())
    monkeypatch.setattr(identity, "_fetch_jwks", down)
    app = FastAPI()

    @app.get("/probe")
    async def probe(
        request: fastapi.Request, key: KeyRecord = fastapi.Depends(require_scope("proxy"))
    ) -> dict[str, Any]:
        return {"key": str(key.id)}

    tc = TestClient(app, raise_server_exceptions=False)
    r = tc.get("/probe", headers={"Authorization": f"Bearer {_sign(_claims())}"})
    assert r.status_code == 401


def test_rest_db_failure_propagates_through_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    """THE fail-closed posture on the envelope path itself: an
    authenticate() that RAISES (ledger down) propagates, never a silent
    401-as-open-door and never a fall-through to the Bearer."""
    _table, plaintext, _k, _dead = _scenario({})

    async def raising_auth(_plaintext: str) -> KeyRecord | None:
        raise RuntimeError("database not connected")

    monkeypatch.setattr(identity, "db", TableDB())
    import pcai_llm.api.auth as auth_mod

    monkeypatch.setattr(auth_mod, "authenticate", raising_auth)
    app = FastAPI()

    @app.get("/probe")
    async def probe(
        request: fastapi.Request, key: KeyRecord = fastapi.Depends(require_scope("proxy"))
    ) -> dict[str, Any]:
        return {"key": str(key.id)}

    tc = TestClient(app, raise_server_exceptions=False)
    r = tc.get("/probe", headers={"X-API-Key": plaintext, "Authorization": f"Bearer {plaintext}"})
    assert r.status_code == 500  # the app's generic handler; NOT 200, NOT a Bearer rescue


# ---------------------------------------------------------------------------
# 2. /mcp wrapper parity — SAME resolver, SAME precedence
# ---------------------------------------------------------------------------


def test_mcp_api_key_only_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    table, plaintext, _k, _dead = _scenario({})
    with _mcp_client(monkeypatch, table) as tc:
        r = tc.post("/mcp", json=_rpc(), headers=_mcp_headers("", api_key=plaintext))
    assert r.status_code == 200, r.text


def test_mcp_bearer_only_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    table, plaintext, _k, _dead = _scenario({})
    with _mcp_client(monkeypatch, table) as tc:
        r = tc.post("/mcp", json=_rpc(), headers=_mcp_headers(plaintext))
    assert r.status_code == 200, r.text


def test_mcp_both_valid_api_key_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    k_bearer = _key(name="bearer-key")
    k_envelope = _key(name="envelope-key")
    table: dict[str, KeyRecord | None] = {k_bearer.key_prefix: k_bearer, k_envelope.key_prefix: k_envelope}
    with _mcp_client(monkeypatch, table) as tc:
        r = tc.post(
            "/mcp",
            json=_rpc(),
            headers=_mcp_headers(k_bearer.key_prefix, api_key=k_envelope.key_prefix),
        )
    assert r.status_code == 200, r.text


def test_mcp_api_key_invalid_bearer_token_valid_is_401(monkeypatch: pytest.MonkeyPatch) -> None:
    """MCP parity of the fail-closed rule: dead envelope + VALID proxy
    token → 401, never rescued."""
    table, _plaintext, _k, dead = _scenario({})
    with _mcp_client(monkeypatch, table) as tc:
        r = tc.post("/mcp", json=_rpc(), headers=_mcp_headers(_sign(_claims()), api_key=dead))
    assert r.status_code == 401
    assert r.content == routes_mcp._UNAUTHORIZED_BODY


def test_mcp_api_key_invalid_bearer_key_valid_is_401(monkeypatch: pytest.MonkeyPatch) -> None:
    """MCP parity: key-vs-key with one dead → REFUSE."""
    table, plaintext, _k, dead = _scenario({})
    with _mcp_client(monkeypatch, table) as tc:
        r = tc.post("/mcp", json=_rpc(), headers=_mcp_headers(plaintext, api_key=dead))
    assert r.status_code == 401
    assert r.content == routes_mcp._UNAUTHORIZED_BODY


def test_mcp_neither_header_401(monkeypatch: pytest.MonkeyPatch) -> None:
    table, _plaintext, _k, _dead = _scenario({})
    with _mcp_client(monkeypatch, table) as tc:
        r = tc.post("/mcp", json=_rpc(), headers=_mcp_headers(""))
    assert r.status_code == 401
    assert r.content == routes_mcp._UNAUTHORIZED_BODY


def test_mcp_expired_token_bearer_plus_envelope_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    """The production /mcp shape: expired proxy id_token in front, minted
    key in the envelope → the envelope authorizes."""
    table, plaintext, _k, _dead = _scenario({})
    with _mcp_client(monkeypatch, table) as tc:
        r = tc.post("/mcp", json=_rpc(), headers=_mcp_headers(_expired_token(), api_key=plaintext))
    assert r.status_code == 200, r.text


def test_mcp_envelope_key_without_mcp_scope_403(monkeypatch: pytest.MonkeyPatch) -> None:
    """The envelope resolves the key and the SAME scope gate applies —
    authenticated-but-unauthorized is still 403 (never 401)."""
    k = _key(scopes=["proxy"])
    table: dict[str, KeyRecord | None] = {k.key_prefix: k}
    with _mcp_client(monkeypatch, table) as tc:
        r = tc.post("/mcp", json=_rpc(), headers=_mcp_headers("", api_key=k.key_prefix))
    assert r.status_code == 403
    assert r.content == routes_mcp._FORBIDDEN_BODY


def test_rest_mcp_share_one_resolver() -> None:
    """The no-drift pin: the /mcp wrapper must call the SAME shared
    resolver require_key calls (inspect the wrapper's call target)."""
    import inspect

    src = inspect.getsource(routes_mcp._McpAuthASGI.__call__)
    assert "resolve_bearer_key" in src
    import pcai_llm.api.auth as auth_mod

    auth_src = inspect.getsource(auth_mod.require_key)
    assert "resolve_bearer_key" in auth_src


# ---------------------------------------------------------------------------
# 2b. F1 regression (wave-1 review): the resolver's caller-record write on
#     the /mcp shim — a ROUTINE valid-token-no-shadow-key refusal must
#     (a) record the resolved caller on the scope's state dict exactly as
#     the pre-envelope wrapper did (git fa020d6: the refusal path wrote the
#     caller into scope state; the shared resolver's documented name is
#     ``state.caller``, the SAME slot the REST surface populates) and
#     (b) stay a clean 401 with NO logged traceback.
# ---------------------------------------------------------------------------


def test_mcp_valid_token_no_key_records_caller_on_scope(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """THE F1 CONTRACT: the shim's state object must be attribute-
    writable (the resolver does ``request.state.caller = caller``), the
    write must land in the SCOPE's state dict (downstream reads
    scope['state']['pcai_caller']), and the refusal must log the quiet
    info line — not an exception traceback."""
    import logging

    class CapturingDB:
        """fetchrow returns None (no shadow key); subjects lookup empty —
        the exact valid-token-no-key posture."""

        async def fetchrow(self, sql: str, *args: Any):
            return None

        async def fetch(self, sql: str, *args: Any):
            return []

        async def fetchval(self, sql: str, *args: Any):
            return 1

        async def execute(self, sql: str, *args: Any):
            return "UPDATE 0"

    async def refusing_auth(_plaintext: str) -> KeyRecord | None:
        return None

    monkeypatch.setattr(routes_mcp, "authenticate", refusing_auth)
    monkeypatch.setattr(identity, "db", CapturingDB())

    captured: dict[str, Any] = {}

    # Drive the raw ASGI wrapper directly so we own the scope object and
    # can assert the state write landed in IT (the TestClient hides the
    # scope inside starlette).
    async def run() -> None:
        received: list[dict[str, Any]] = []

        async def receive() -> dict[str, Any]:
            return {"type": "http.disconnect"}

        async def send(message: dict[str, Any]) -> None:
            received.append(message)

        scope: dict[str, Any] = {
            "type": "http",
            "method": "POST",
            "path": "/mcp",
            "headers": [
                (b"authorization", f"Bearer {_sign(_claims())}".encode("latin-1")),
                (b"mcp-protocol-version", _MCP_PV.encode()),
                (b"mcp-method", b"tools/list"),
                (b"content-type", b"application/json"),
            ],
        }
        wrapper = routes_mcp._McpAuthASGI(_NoopApp())
        await wrapper(scope, receive, send)
        captured["scope_state"] = scope.get("state")
        captured["received"] = received

    with caplog.at_level(logging.DEBUG, logger="pcai_llm.api.routes_mcp"):
        asyncio.run(run())

    # (a) the caller IS recorded on the scope — the resolver writes the
    # attribute ``caller``; the pre-envelope wrapper's dict key was
    # 'pcai_caller', the shared resolver's documented state name is
    # request.state.caller (the SAME slot the REST surface populates —
    # that identity-of-contract is the point of the shared shim).
    assert captured["scope_state"] is not None
    assert "caller" in captured["scope_state"]
    assert captured["scope_state"]["caller"].subject == "user-alice-1"
    assert captured["scope_state"]["caller"].subject_kind == identity.KIND_TOKEN
    # the refusal itself: the same 401 body the wrapper always emitted
    assert captured["received"][0]["status"] == 401
    assert captured["received"][1]["body"] == routes_mcp._UNAUTHORIZED_BODY
    # (b) NO traceback in the logs: a clean info refusal, never an
    # exception from the routine path
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert errors == [], f"routine refusal logged exceptions: {[r.getMessage() for r in errors]}"


class _NoopApp:
    """Never reached on a 401 refusal; satisfies the wrapper's __init__."""

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:  # pragma: no cover
        raise AssertionError("the MCP app must not run behind a refusal")


# ---------------------------------------------------------------------------
# 3. envelope edge hygiene
# ---------------------------------------------------------------------------


def test_rest_empty_envelope_header_is_no_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    """An EMPTY X-API-Key header is no credential at all: the request
    resolves exactly as if the header were absent (the bearer ladder
    runs) — an empty envelope must not mask a valid Bearer."""
    table, plaintext, k, _dead = _scenario({})
    tc = _rest_client(monkeypatch, table)
    r = tc.get("/probe", headers={"Authorization": f"Bearer {plaintext}", "X-API-Key": ""})
    assert r.status_code == 200, r.text
    assert r.json() == {"key": str(k.id)}


def test_rest_whitespace_envelope_is_no_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    """Whitespace-only envelope: same posture — no credential, bearer
    ladder runs (fail-closed 401 when no bearer either)."""
    table, plaintext, k, _dead = _scenario({})
    tc = _rest_client(monkeypatch, table)
    r = tc.get("/probe", headers={"Authorization": f"Bearer {plaintext}", "X-API-Key": "   "})
    assert r.status_code == 200, r.text
    assert r.json() == {"key": str(k.id)}


def test_rest_envelope_with_non_bearer_authorization_ignores_scheme(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-Bearer Authorization scheme is not a credential (today's
    rule); the envelope still carries the request on its own."""
    table, plaintext, k, _dead = _scenario({})
    tc = _rest_client(monkeypatch, table)
    r = tc.get("/probe", headers={"Authorization": "Basic dXNlcjpwd2Q=", "X-API-Key": plaintext})
    assert r.status_code == 200, r.text
    assert r.json() == {"key": str(k.id)}


def test_rest_dead_envelope_only_401(monkeypatch: pytest.MonkeyPatch) -> None:
    """Envelope alone and dead: the SAME 401 message as an unknown bearer
    key (no information leak about which header failed)."""
    table, _plaintext, _k, dead = _scenario({})
    tc = _rest_client(monkeypatch, table)
    r = tc.get("/probe", headers={"X-API-Key": dead})
    assert r.status_code == 401
    assert "invalid, revoked, or expired key" in r.text


# ---------------------------------------------------------------------------
# 4. browser-header additions (2026-10-06 lead scope ADDITION, §Z1 zone):
#    preferred_username subject preference + X-Auth-Request-Groups fallback
#    — the platform oauth2-proxy injects these (verified live); WITHOUT the
#    fallback the adminGroups/browser path silently saw no groups behind
#    the chain because the platform proxy does NOT inject
#    X-Forwarded-Groups.
# ---------------------------------------------------------------------------


def test_browser_subject_prefers_preferred_username() -> None:
    """(b) X-Auth-Request-Preferred-Username (the OIDC preferred_username
    claim shape — the admin console's subject convention) wins over
    X-Auth-Request-User when both are injected."""
    assert browser_identity_from_headers(
        {"X-Auth-Request-Preferred-Username": "alice.p", "X-Auth-Request-User": "alice"}
    ) == ("alice.p", [])


def test_browser_subject_falls_back_to_user_then_email() -> None:
    """(b) The fallback order is unchanged when Preferred-Username is
    absent: X-Auth-Request-User first, X-Email last (byte-for-byte the
    pre-addition behavior)."""
    assert browser_identity_from_headers({"X-Auth-Request-User": "alice"}) == ("alice", [])
    assert browser_identity_from_headers({"X-Email": "a@b.c"}) == ("a@b.c", [])
    # Preferred-Username present but WHITESPACE only: falls through to User
    assert browser_identity_from_headers(
        {"X-Auth-Request-Preferred-Username": "   ", "X-Auth-Request-User": "alice"}
    ) == ("alice", [])
    # Preferred-Username present but EMPTY: falls through to User
    assert browser_identity_from_headers({"X-Auth-Request-Preferred-Username": "", "X-Auth-Request-User": "alice"}) == (
        "alice",
        [],
    )


def test_browser_groups_fallback_to_auth_request_groups() -> None:
    """(a) THE FIX: behind the platform chain (no X-Forwarded-Groups
    injected) groups ride X-Auth-Request-Groups — previously the browser/
    admin group path silently saw NO groups there."""
    assert browser_identity_from_headers(
        {"X-Auth-Request-User": "alice", "X-Auth-Request-Groups": "eng, platform-admin ,"}
    ) == ("alice", ["eng", "platform-admin"])


def test_browser_groups_forwarded_still_wins_byte_identical() -> None:
    """(a) Precedence: X-Forwarded-Groups present ⇒ the behavior is
    byte-for-byte unchanged (the fallback never reorders it), including
    the empty-string fall-through to the second header."""
    both = {"X-Auth-Request-User": "a", "X-Forwarded-Groups": "fwd", "X-Auth-Request-Groups": "arg"}
    assert browser_identity_from_headers(both) == ("a", ["fwd"])
    # an EMPTY (not absent) X-Forwarded-Groups falls through — an empty
    # header is no groups assertion
    assert browser_identity_from_headers({**both, "X-Forwarded-Groups": "   "}) == ("a", ["arg"])
    # neither groups header: empty groups (unchanged)
    assert browser_identity_from_headers({"X-Auth-Request-User": "a"}) == ("a", [])


def test_browser_headers_case_insensitive_dict_shapes() -> None:
    """The documented case-insensitivity contract holds for BOTH new
    headers: a lowercased dict (what dict(request.headers) produces from
    raw ASGI) and a canonical-case dict (hand-built tests) agree."""
    lowered = {
        "x-auth-request-preferred-username": "alice.p",
        "x-auth-request-user": "alice",
        "x-auth-request-groups": "eng",
    }
    canonical = {
        "X-Auth-Request-Preferred-Username": "alice.p",
        "X-Auth-Request-User": "alice",
        "X-Auth-Request-Groups": "eng",
    }
    assert browser_identity_from_headers(lowered) == browser_identity_from_headers(canonical) == ("alice.p", ["eng"])


def test_browser_subject_no_headers_still_none() -> None:
    """A groups header alone still resolves NOTHING (subject is the
    anchor; unchanged fail-closed posture)."""
    assert browser_identity_from_headers({"X-Auth-Request-Groups": "g"}) is None
    assert browser_identity_from_headers({"X-Auth-Request-Preferred-Username": "  "}) is None


def test_resolve_caller_browser_rung_with_new_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    """End-to-end through the ladder (rung 1): Preferred-Username subject
    + X-Auth-Request-Groups groups resolve under the trust flag — the
    shape z2's /v1/identity probe and z4's homepage chip both consume."""
    monkeypatch.setattr(settings, "trust_proxy_identity", True)

    class QuietDB:
        async def fetchrow(self, sql: str, *args: Any):
            return None

        async def fetch(self, sql: str, *args: Any):
            return []

        async def execute(self, sql: str, *args: Any):
            return "UPDATE 0"

    monkeypatch.setattr(identity, "db", QuietDB())
    caller = asyncio.run(
        identity.resolve_caller(
            None,
            headers={
                "X-Auth-Request-Preferred-Username": "alice.p",
                "X-Auth-Request-User": "alice",
                "X-Auth-Request-Groups": "eng,platform-admin",
            },
        )
    )
    assert caller is not None
    assert caller.subject_kind == identity.KIND_BROWSER
    assert caller.subject == "alice.p"
    assert caller.groups == ["eng", "platform-admin"]


def test_admin_group_path_with_platform_proxy_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    """THE DEPLOYMENT BUG THIS FIXES: behind the platform oauth2-proxy
    (X-Auth-Request-Groups only — it does NOT inject X-Forwarded-Groups),
    the adminGroups gate previously 403'd every legitimate admin because
    groups resolved empty. With the fallback the SAME live pod config
    admits the group."""
    monkeypatch.setattr(settings, "trust_proxy_identity", True)
    monkeypatch.setattr(settings, "admin_groups", "platform-admin")
    monkeypatch.setattr(settings, "admin_api_keys", "admin-secret-123")
    monkeypatch.setattr(routes_admin, "db", _QuietAdminDB())
    import pcai_llm.core.keys as keys_mod
    from pcai_llm.core import budgets

    monkeypatch.setattr(budgets, "db", _QuietAdminDB())
    monkeypatch.setattr(keys_mod, "db", _QuietAdminDB())
    app = FastAPI()
    app.include_router(routes_admin.router)
    tc = TestClient(app, raise_server_exceptions=False)
    r = tc.get(
        "/admin/keys",
        headers={
            "X-Auth-Request-Preferred-Username": "ops",
            "X-Auth-Request-Groups": "sre, platform-admin",
        },
    )
    assert r.status_code == 200, r.text


class _QuietAdminDB:
    """The phaseb admin-fake pattern (quiet db + budgets), local to this
    file so the file stays self-contained."""

    async def fetch(self, sql: str, *args: Any):
        return []

    async def fetchval(self, sql: str, *args: Any):
        return 1

    async def fetchrow(self, sql: str, *args: Any):
        return None

    async def execute(self, sql: str, *args: Any):
        return "UPDATE 0"
