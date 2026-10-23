"""Per-key MCP credential overrides: admin CRUD + header injection.

Pins the memory-MCP dataset/password pattern end to end: values are stored
Fernet-encrypted, NEVER returned by any endpoint (names only), the writes
fail 503 without a master key, and the decrypted overrides merge AFTER
server-level auth headers on BOTH call paths — the direct /mcp call and the
relay executor (a fake upstream server asserts the headers it receives).
"""

from __future__ import annotations

import asyncio
import json
import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pcai_llm.api import routes_enroll, routes_mcp, routes_proxy
from pcai_llm.api.auth import KeyRecord
from pcai_llm.config import settings
from pcai_llm.core import registry, secretbox
from pcai_llm.core.budgets import GateResult

ADMIN_H = {"Authorization": "Bearer admin-secret-123"}


class CredsFakeDB:
    """mcp_server_creds + existence checks for the admin surface."""

    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple]] = []
        self.keys_exist = True
        self.server_exists = True
        self.creds: dict[tuple[uuid.UUID, str], str] = {}  # (key_id, server) -> ciphertext

    async def fetchval(self, sql: str, *args: Any):
        if "FROM api_keys" in sql:
            return 1 if self.keys_exist else None
        if "FROM mcp_servers" in sql:
            return 1 if self.server_exists else None
        return 1

    async def execute(self, sql: str, *args: Any):
        self.executed.append((sql, args))
        if sql.lstrip().startswith("INSERT INTO mcp_server_creds"):
            self.creds[(args[0], args[1])] = args[2]
            return "INSERT 0 1"
        if sql.lstrip().startswith("DELETE FROM mcp_server_creds"):
            if (args[0], args[1]) in self.creds:
                del self.creds[(args[0], args[1])]
                return "DELETE 1"
            return "DELETE 0"
        return "UPDATE 1"

    async def fetch(self, sql: str, *args: Any):
        if "FROM mcp_server_creds" in sql:
            return [
                {"server_name": server, "headers_enc": enc}
                for (kid, server), enc in self.creds.items()
                if kid == args[0]
            ]
        return []

    async def fetchrow(self, sql: str, *args: Any):
        if "FROM mcp_server_creds" in sql:
            enc = self.creds.get((args[0], args[1]))
            return {"headers_enc": enc} if enc is not None else None
        return None


@pytest.fixture
def master_key(monkeypatch: pytest.MonkeyPatch):
    """Fresh Fernet derivation per test (the suite's secretbox pattern)."""
    monkeypatch.setattr(settings, "secrets_master_key", "creds-test-master")
    monkeypatch.setattr(secretbox, "_fernet", None)
    yield


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, master_key):
    monkeypatch.setattr(settings, "admin_api_keys", "admin-secret-123")
    fake = CredsFakeDB()
    monkeypatch.setattr(routes_enroll, "db", fake)
    app = FastAPI()
    app.include_router(routes_enroll.admin_router)
    return TestClient(app, raise_server_exceptions=False), fake


def _key(**over: Any) -> KeyRecord:
    base: dict[str, Any] = {
        "id": uuid.uuid4(),
        "user_id": uuid.uuid4(),
        "name": "k",
        "key_prefix": "hllm-XX",
        "scopes": ["mcp"],
        "allowed_models": [],
        "rate_limit_rpm": 60,
        "rate_limit_tpm": 100_000,
        "max_concurrent": 0,
        "expires_at": None,
    }
    base.update(over)
    return KeyRecord(**base)


def _store_creds(fake: CredsFakeDB, key_id: uuid.UUID, server: str, headers: dict[str, str]) -> None:
    fake.creds[(key_id, server)] = secretbox.encrypt(json.dumps(headers))


# ---------------------------------------------------------------------------
# admin CRUD
# ---------------------------------------------------------------------------


def test_put_then_get_names_only_values_never_returned(env) -> None:
    tc, fake = env
    kid = uuid.uuid4()
    headers = {"X-Memory-Dataset": "tenant-ds-1", "X-Dataset-Password": "s3cret-pw"}
    r = tc.put(f"/admin/keys/{kid}/mcp-creds", json={"server": "memory", "headers": headers}, headers=ADMIN_H)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["header_names"] == ["X-Dataset-Password", "X-Memory-Dataset"]
    assert "s3cret-pw" not in r.text and "tenant-ds-1" not in r.text

    # stored as Fernet ciphertext — the plaintext JSON is nowhere in the row
    (enc,) = fake.creds.values()
    assert "s3cret-pw" not in enc and "tenant-ds-1" not in enc
    assert secretbox.decrypt(enc) == json.dumps(headers)

    g = tc.get(f"/admin/keys/{kid}/mcp-creds", headers=ADMIN_H)
    assert g.status_code == 200
    assert g.json() == [{"server": "memory", "header_names": ["X-Dataset-Password", "X-Memory-Dataset"]}]
    assert "s3cret-pw" not in g.text and "tenant-ds-1" not in g.text


def test_put_creds_upsert_replaces_per_server(env) -> None:
    tc, fake = env
    kid = uuid.uuid4()
    tc.put(
        f"/admin/keys/{kid}/mcp-creds", json={"server": "memory", "headers": {"X-Memory-Dataset": "a"}}, headers=ADMIN_H
    )
    tc.put(
        f"/admin/keys/{kid}/mcp-creds",
        json={"server": "memory", "headers": {"X-Memory-Dataset": "b", "X-Extra": "1"}},
        headers=ADMIN_H,
    )
    assert len(fake.creds) == 1  # same (key, server) row replaced
    g = tc.get(f"/admin/keys/{kid}/mcp-creds", headers=ADMIN_H)
    assert g.json() == [{"server": "memory", "header_names": ["X-Extra", "X-Memory-Dataset"]}]


def test_put_creds_unknown_key_404(env) -> None:
    tc, fake = env
    fake.keys_exist = False
    r = tc.put(
        f"/admin/keys/{uuid.uuid4()}/mcp-creds", json={"server": "memory", "headers": {"A": "b"}}, headers=ADMIN_H
    )
    assert r.status_code == 404


def test_put_creds_unknown_server_404(env) -> None:
    tc, fake = env
    fake.server_exists = False
    r = tc.put(
        f"/admin/keys/{uuid.uuid4()}/mcp-creds", json={"server": "ghost", "headers": {"A": "b"}}, headers=ADMIN_H
    )
    assert r.status_code == 404


def test_put_creds_bad_header_name_400(env) -> None:
    tc, _ = env
    kid = uuid.uuid4()
    for bad in ("X_Bad", "has space", "X/Slash", ""):
        r = tc.put(f"/admin/keys/{kid}/mcp-creds", json={"server": "memory", "headers": {bad: "v"}}, headers=ADMIN_H)
        assert r.status_code == 400, bad


def test_put_creds_too_many_headers_400(env) -> None:
    tc, _ = env
    kid = uuid.uuid4()
    headers = {f"X-H{i}": "v" for i in range(17)}
    r = tc.put(f"/admin/keys/{kid}/mcp-creds", json={"server": "memory", "headers": headers}, headers=ADMIN_H)
    assert r.status_code == 400
    ok = {f"X-H{i}": "v" for i in range(16)}
    assert (
        tc.put(f"/admin/keys/{kid}/mcp-creds", json={"server": "memory", "headers": ok}, headers=ADMIN_H).status_code
        == 200
    )


def test_put_creds_value_too_long_400(env) -> None:
    tc, _ = env
    kid = uuid.uuid4()
    r = tc.put(
        f"/admin/keys/{kid}/mcp-creds", json={"server": "memory", "headers": {"X-Big": "v" * 513}}, headers=ADMIN_H
    )
    assert r.status_code == 400


def test_put_creds_empty_headers_400(env) -> None:
    tc, _ = env
    kid = uuid.uuid4()
    r = tc.put(f"/admin/keys/{kid}/mcp-creds", json={"server": "memory", "headers": {}}, headers=ADMIN_H)
    assert r.status_code == 400


def test_put_creds_503_without_master_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same fail-closed posture as endpoint-cred writes."""
    monkeypatch.setattr(settings, "admin_api_keys", "admin-secret-123")
    monkeypatch.setattr(settings, "secrets_master_key", "")
    monkeypatch.setattr(secretbox, "_fernet", None)
    fake = CredsFakeDB()
    monkeypatch.setattr(routes_enroll, "db", fake)
    app = FastAPI()
    app.include_router(routes_enroll.admin_router)
    tc = TestClient(app, raise_server_exceptions=False)
    r = tc.put(
        f"/admin/keys/{uuid.uuid4()}/mcp-creds",
        json={"server": "memory", "headers": {"X-Memory-Dataset": "ds"}},
        headers=ADMIN_H,
    )
    assert r.status_code == 503
    assert "PCAI_LLM_SECRETS_MASTER_KEY" in r.json()["detail"]
    assert fake.executed == []  # nothing written


def test_delete_creds_then_404(env) -> None:
    tc, fake = env
    kid = uuid.uuid4()
    _store_creds(fake, kid, "memory", {"X-Memory-Dataset": "ds"})
    r = tc.delete(f"/admin/keys/{kid}/mcp-creds/memory", headers=ADMIN_H)
    assert r.status_code == 200
    assert r.json() == {"deleted": True, "server": "memory"}
    assert fake.creds == {}
    r2 = tc.delete(f"/admin/keys/{kid}/mcp-creds/memory", headers=ADMIN_H)
    assert r2.status_code == 404


def test_delete_creds_unknown_key_404(env) -> None:
    tc, fake = env
    fake.keys_exist = False
    assert tc.delete(f"/admin/keys/{uuid.uuid4()}/mcp-creds/memory", headers=ADMIN_H).status_code == 404


def test_get_creds_unknown_key_404(env) -> None:
    tc, fake = env
    fake.keys_exist = False
    assert tc.get(f"/admin/keys/{uuid.uuid4()}/mcp-creds", headers=ADMIN_H).status_code == 404


def test_creds_surface_requires_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "admin_api_keys", "admin-secret-123")
    app = FastAPI()
    app.include_router(routes_enroll.admin_router)
    tc = TestClient(app, raise_server_exceptions=False)
    kid = uuid.uuid4()
    assert tc.get(f"/admin/keys/{kid}/mcp-creds").status_code == 401
    assert tc.put(f"/admin/keys/{kid}/mcp-creds", json={"server": "s", "headers": {"A": "b"}}).status_code == 401
    assert tc.delete(f"/admin/keys/{kid}/mcp-creds/s").status_code == 401


# ---------------------------------------------------------------------------
# header injection — resolve_mcp_headers (both call paths)
# ---------------------------------------------------------------------------

_MEMORY_SERVER: dict[str, Any] = {
    "id": "srv1",
    "name": "memory",
    "url": "http://memory.svc:8080",
    "headers": {"X-Server-Base": "1"},
    "auth_secret": "mem-secret",
    "auth_token_enc": None,
    "enabled": True,
    "tool_policy": "all",
    "allowed_tools": [],
}


def test_resolve_headers_override_merges_last_wins(master_key, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = CredsFakeDB()
    kid = uuid.uuid4()
    _store_creds(fake, kid, "memory", {"X-Memory-Dataset": "ds1", "Authorization": "Bearer tenant-token"})
    monkeypatch.setattr(routes_mcp, "db", fake)
    monkeypatch.setenv("PCAI_LLM_MCP_KEY__MEM_SECRET", "server-token")  # server-level auth

    headers = asyncio.run(routes_mcp.resolve_mcp_headers(_MEMORY_SERVER, key_id=kid))
    assert headers["X-Server-Base"] == "1"
    assert headers["X-Memory-Dataset"] == "ds1"
    assert headers["Authorization"] == "Bearer tenant-token", "per-key override must beat server-level auth"
    # Phase A attribution: the gateway's X-MCP-Caller stamp rides EVERY keyed
    # resolution and lands AFTER the last-wins merge — a stored override
    # cannot survive it (spoofing vector closed; pinned separately below).
    assert headers["X-MCP-Caller"] == f"{kid}@gateway"


def test_resolve_headers_without_overrides_is_server_level(master_key, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(routes_mcp, "db", CredsFakeDB())  # no rows
    monkeypatch.setenv("PCAI_LLM_MCP_KEY__MEM_SECRET", "server-token")
    kid = uuid.uuid4()
    headers = asyncio.run(routes_mcp.resolve_mcp_headers(_MEMORY_SERVER, key_id=kid))
    assert headers["X-Server-Base"] == "1"
    assert headers["Authorization"] == "Bearer server-token"
    # the attribution stamp is the ONE keyed addition (key-fp fallback: no
    # subject row exists in this fake)
    assert headers["X-MCP-Caller"] == f"{kid}@gateway"


def test_resolve_headers_unstamped_when_flag_off(master_key, monkeypatch: pytest.MonkeyPatch) -> None:
    """Kill switch: PCAI_LLM_MCP_CALLER_ATTRIBUTION=false restores the
    pre-stamp surface byte-for-byte (the flag-off = today's-behavior rule)."""
    monkeypatch.setattr(settings, "mcp_caller_attribution", False)
    monkeypatch.setattr(routes_mcp, "db", CredsFakeDB())
    monkeypatch.setenv("PCAI_LLM_MCP_KEY__MEM_SECRET", "server-token")
    headers = asyncio.run(routes_mcp.resolve_mcp_headers(_MEMORY_SERVER, key_id=uuid.uuid4()))
    assert headers == {"X-Server-Base": "1", "Authorization": "Bearer server-token"}
    assert "X-MCP-Caller" not in headers


def test_resolve_headers_unstamped_without_key(master_key, monkeypatch: pytest.MonkeyPatch) -> None:
    """key_id=None (server-level callers — the offpeak evaluator) stamps
    nothing: attribution is per CALLER, and a keyless resolution has none."""
    monkeypatch.setattr(routes_mcp, "db", CredsFakeDB())
    monkeypatch.setenv("PCAI_LLM_MCP_KEY__MEM_SECRET", "server-token")
    headers = asyncio.run(routes_mcp.resolve_mcp_headers(_MEMORY_SERVER, key_id=None))
    assert headers == {"X-Server-Base": "1", "Authorization": "Bearer server-token"}
    assert "X-MCP-Caller" not in headers


def test_resolve_headers_subject_wins_over_fp(master_key, monkeypatch: pytest.MonkeyPatch) -> None:
    """A resolved subject (bound key) stamps the SUBJECT, not the fp — the
    whole point of the ledger join. caller_subject=None triggers the
    degrade-not-fail lookup; a found row wins."""
    fake = CredsFakeDB()

    async def fake_subject_for_key(key_id: uuid.UUID) -> str | None:
        return "alice@corp"

    monkeypatch.setattr(routes_mcp, "db", fake)
    monkeypatch.setattr(routes_mcp, "subject_for_key", fake_subject_for_key)
    headers = asyncio.run(routes_mcp.resolve_mcp_headers(_MEMORY_SERVER, key_id=uuid.uuid4()))
    assert headers["X-MCP-Caller"] == "alice@corp@gateway"


def test_resolve_headers_stamp_overwrites_tenant_override(master_key, monkeypatch: pytest.MonkeyPatch) -> None:
    """THE SPOOFING VECTOR (review §1.4): a tenant override carrying
    X-MCP-Caller merges LAST-WINS — the gateway stamp must run AFTER the
    merge and overwrite it. (The write path also reserves the name with a
    400; this pins the merge side, e.g. for rows written before the
    reservation.)"""
    fake = CredsFakeDB()
    kid = uuid.uuid4()
    _store_creds(fake, kid, "memory", {"X-MCP-Caller": "victim@corp"})
    monkeypatch.setattr(routes_mcp, "db", fake)
    headers = asyncio.run(routes_mcp.resolve_mcp_headers(_MEMORY_SERVER, key_id=kid))
    assert headers["X-MCP-Caller"] == f"{kid}@gateway"


# ---------------------------------------------------------------------------
# relay-attribution companions (X-MCP-Caller-Subject / X-MCP-Caller-Class) —
# the SQLhandler identity.py rung-1 vocabulary
# ---------------------------------------------------------------------------


def test_resolve_headers_bound_key_emits_all_three(master_key, monkeypatch: pytest.MonkeyPatch) -> None:
    """A BOUND key (real subject resolved via the degrade-not-fail lookup)
    emits all three attribution headers: X-MCP-Caller keeps the @gateway hop
    suffix; -Subject carries the BARE subject (SQLhandler sanitizes/parses
    it as an identity, not a display string); -Class carries the gateway's
    subject_kind — SQLhandler's _KIND_CLASS_MAP accepts 'key-bound' as-is
    and maps it onto its own bounded class labels."""
    fake = CredsFakeDB()

    async def fake_subject_for_key(key_id: uuid.UUID) -> str | None:
        return "alice@corp"

    monkeypatch.setattr(routes_mcp, "db", fake)
    monkeypatch.setattr(routes_mcp, "subject_for_key", fake_subject_for_key)
    kid = uuid.uuid4()
    headers = asyncio.run(routes_mcp.resolve_mcp_headers(_MEMORY_SERVER, key_id=kid))
    assert headers["X-MCP-Caller"] == "alice@corp@gateway"
    assert headers["X-MCP-Caller-Subject"] == "alice@corp"
    assert headers["X-MCP-Caller-Class"] == "key-bound"


def test_resolve_headers_caller_subject_arg_emits_companions(master_key, monkeypatch: pytest.MonkeyPatch) -> None:
    """The same three-header surface via the caller_subject= wiring point
    (the Phase B resolver arg) — no DB consulted, subject is real."""
    monkeypatch.setattr(routes_mcp, "db", CredsFakeDB())
    headers = asyncio.run(routes_mcp.resolve_mcp_headers(_MEMORY_SERVER, key_id=uuid.uuid4(), caller_subject="bob@sso"))
    assert headers["X-MCP-Caller"] == "bob@sso@gateway"
    assert headers["X-MCP-Caller-Subject"] == "bob@sso"
    assert headers["X-MCP-Caller-Class"] == "key-bound"


def test_resolve_headers_unbound_key_emits_only_caller(master_key, monkeypatch: pytest.MonkeyPatch) -> None:
    """An UNBOUND key (subject_for_key → None → the key-fp fallback) emits
    ONLY X-MCP-Caller: neither companion header exists — an unbound key must
    not advertise a subject it does not have (and a stale tenant-supplied
    -Subject/-Class value must not upgrade a pseudonymous call downstream —
    the merge-side drop is pinned by test_resolve_headers_unbound_key_stale_
    tenant_companions_dropped)."""
    fake = CredsFakeDB()

    async def fake_subject_for_key(key_id: uuid.UUID) -> str | None:
        return None

    monkeypatch.setattr(routes_mcp, "db", fake)
    monkeypatch.setattr(routes_mcp, "subject_for_key", fake_subject_for_key)
    kid = uuid.uuid4()
    headers = asyncio.run(routes_mcp.resolve_mcp_headers(_MEMORY_SERVER, key_id=kid))
    assert headers["X-MCP-Caller"] == f"{kid}@gateway"
    assert "X-MCP-Caller-Subject" not in headers
    assert "X-MCP-Caller-Class" not in headers


def test_resolve_headers_unbound_key_stale_tenant_companions_dropped(
    master_key, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The merge-side belt-and-braces for the NEW reserved names: a row
    written before the reservation (or via any pre-check writer) carrying
    -Subject/-Class must not ride the relay on an unbound key — there is no
    stamp to overwrite it, so the gated block drops both outright."""
    fake = CredsFakeDB()
    kid = uuid.uuid4()
    _store_creds(fake, kid, "memory", {"X-MCP-Caller-Subject": "spoofed", "X-MCP-Caller-Class": "user"})
    monkeypatch.setattr(routes_mcp, "db", fake)
    headers = asyncio.run(routes_mcp.resolve_mcp_headers(_MEMORY_SERVER, key_id=kid))
    assert "X-MCP-Caller-Subject" not in headers
    assert "X-MCP-Caller-Class" not in headers
    assert headers["X-MCP-Caller"] == f"{kid}@gateway"


def test_resolve_headers_bound_key_overwrites_stale_tenant_companions(
    master_key, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same pre-reservation row on a BOUND key: the post-merge stamps
    overwrite the stale values (the X-MCP-Caller precedent, extended)."""
    fake = CredsFakeDB()
    kid = uuid.uuid4()
    _store_creds(fake, kid, "memory", {"X-MCP-Caller-Subject": "spoofed", "X-MCP-Caller-Class": "user"})

    async def fake_subject_for_key(key_id: uuid.UUID) -> str | None:
        return "alice@corp"

    monkeypatch.setattr(routes_mcp, "db", fake)
    monkeypatch.setattr(routes_mcp, "subject_for_key", fake_subject_for_key)
    headers = asyncio.run(routes_mcp.resolve_mcp_headers(_MEMORY_SERVER, key_id=kid))
    assert headers["X-MCP-Caller-Subject"] == "alice@corp"
    assert headers["X-MCP-Caller-Class"] == "key-bound"


def test_resolve_headers_companions_absent_when_flag_off(master_key, monkeypatch: pytest.MonkeyPatch) -> None:
    """Kill switch covers all THREE headers: flag off restores the pre-stamp
    surface byte-for-byte (subject lookup not even attempted)."""
    fake = CredsFakeDB()

    async def fake_subject_for_key(key_id: uuid.UUID) -> str | None:
        return "alice@corp"  # a bound row exists — the flag, not the data, gates

    monkeypatch.setattr(settings, "mcp_caller_attribution", False)
    monkeypatch.setattr(routes_mcp, "db", fake)
    monkeypatch.setattr(routes_mcp, "subject_for_key", fake_subject_for_key)
    headers = asyncio.run(routes_mcp.resolve_mcp_headers(_MEMORY_SERVER, key_id=uuid.uuid4()))
    assert "X-MCP-Caller" not in headers
    assert "X-MCP-Caller-Subject" not in headers
    assert "X-MCP-Caller-Class" not in headers


def test_resolve_headers_companion_subject_sanitized(master_key, monkeypatch: pytest.MonkeyPatch) -> None:
    """The -Subject value runs the SAME sanitizer as X-MCP-Caller: CR/LF and
    other control chars stripped, 200-char cap — it is an outbound header
    value on exactly the same injection boundary."""
    fake = CredsFakeDB()

    async def fake_subject_for_key(key_id: uuid.UUID) -> str | None:
        return "bad\r\nX-Injection: 1 subject"

    monkeypatch.setattr(routes_mcp, "db", fake)
    monkeypatch.setattr(routes_mcp, "subject_for_key", fake_subject_for_key)
    headers = asyncio.run(routes_mcp.resolve_mcp_headers(_MEMORY_SERVER, key_id=uuid.uuid4()))
    subject = headers["X-MCP-Caller-Subject"]
    assert "\r" not in subject and "\n" not in subject and ":" in subject
    assert headers["X-MCP-Caller-Class"] == "key-bound"


def test_relay_executor_bound_key_sends_all_three(master_key, monkeypatch: pytest.MonkeyPatch) -> None:
    """The RELAY path (the surface SQLhandler actually receives) forwards
    the full three-header attribution for a bound key — same choke point,
    so this pins the end-to-end relay posture."""
    fake = CredsFakeDB()
    kid = uuid.uuid4()

    async def fake_subject_for_key(key_id: uuid.UUID) -> str | None:
        return "carol@corp"

    async def fake_rate(key_id: uuid.UUID, rpm: int, tpm: int, est: int):
        return GateResult(True)

    async def fake_settle(*a: Any, **k: Any) -> None:
        return None

    async def fake_upstream_session(url: str, headers: dict, op: Any, timeout_s: float | None = None):
        captured["headers"] = dict(headers)
        return await op(_FakeSession())

    captured: dict = {}
    monkeypatch.setattr(routes_mcp, "db", fake)
    monkeypatch.setattr(routes_mcp, "subject_for_key", fake_subject_for_key)
    monkeypatch.setattr(routes_proxy, "check_rate_limit", fake_rate)
    monkeypatch.setattr(routes_proxy, "settle", fake_settle)
    monkeypatch.setattr(routes_mcp, "_upstream_session", fake_upstream_session)
    monkeypatch.setattr(registry, "_mcp_servers_cache", [_MEMORY_SERVER])

    execute = asyncio.run(routes_proxy._relay_executor(_key(id=kid), "req-relay-attrib"))
    ok, _ = asyncio.run(execute("memory", "search", {}))
    assert ok is True
    assert captured["headers"]["X-MCP-Caller"] == "carol@corp@gateway"
    assert captured["headers"]["X-MCP-Caller-Subject"] == "carol@corp"
    assert captured["headers"]["X-MCP-Caller-Class"] == "key-bound"


def test_reserved_companion_names_400(env) -> None:
    """Tenant overrides of the companion names are refused 400 at the WRITE
    path (same reservation grammar as X-MCP-Caller, case-insensitive; the
    400 detail names the offending header)."""
    tc, _fake = env
    kid = uuid.uuid4()
    for variant in (
        "X-MCP-Caller-Subject",
        "x-mcp-caller-subject",
        "X-MCP-Caller-Class",
        "x-mcp-caller-class",
        "X-MCP-CALLER-SUBJECT",
    ):
        r = tc.put(
            f"/admin/keys/{kid}/mcp-creds",
            json={"server": "memory", "headers": {variant: "spoofed-subject"}},
            headers=ADMIN_H,
        )
        assert r.status_code == 400, variant
        assert "reserved" in r.json()["detail"]
        assert variant in r.json()["detail"]
    # sane overrides still pass alongside the reservation
    ok = tc.put(
        f"/admin/keys/{kid}/mcp-creds",
        json={"server": "memory", "headers": {"X-Memory-Dataset": "ds"}},
        headers=ADMIN_H,
    )
    assert ok.status_code == 200


def test_override_lookup_failure_degrades_to_server_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    """DB failure on the overrides lookup must never fail an authorized call
    (this is also what keeps every un-patched-DB test in the suite green).
    The attribution stamp shares the degrade-not-fail posture: the DB failure
    ALSO kills the subject lookup, so the stamp falls back to the key fp."""

    class BrokenDB:
        async def fetchrow(self, sql: str, *args: Any):
            raise RuntimeError("database not connected")

    kid = uuid.uuid4()
    monkeypatch.setattr(routes_mcp, "db", BrokenDB())
    target = {"name": "memory", "headers": {"X-Base": "1"}, "auth_secret": None, "auth_token_enc": None}
    headers = asyncio.run(routes_mcp.resolve_mcp_headers(target, key_id=kid))
    assert headers == {"X-Base": "1", "X-MCP-Caller": f"{kid}@gateway"}


class _FakeBlock:
    def model_dump(self, mode: str = "json", exclude_none: bool = True) -> dict:
        return {"type": "text", "text": "ok"}


class _FakeResult:
    def __init__(self) -> None:
        self.is_error = False
        self.content = [_FakeBlock()]


class _FakeSession:
    async def call_tool(self, tool: str, arguments: dict) -> _FakeResult:
        return _FakeResult()


def _patch_mcp_call_path(monkeypatch: pytest.MonkeyPatch, fake: CredsFakeDB, captured: dict) -> None:
    async def fake_grants(key_id: uuid.UUID) -> set[tuple[str, str]]:
        return {("memory", "search")}

    async def fake_rate(key_id: uuid.UUID, rpm: int, tpm: int, est: int):
        return GateResult(True)

    async def fake_settle(*a: Any, **k: Any) -> None:
        return None

    async def fake_upstream_session(url: str, headers: dict, op: Any, timeout_s: float | None = None):
        captured["url"] = url
        captured["headers"] = dict(headers)  # the headers the fake SERVER would receive
        return await op(_FakeSession())

    monkeypatch.setattr(routes_mcp, "db", fake)
    monkeypatch.setattr(routes_mcp, "load_grants", fake_grants)
    monkeypatch.setattr(routes_mcp, "check_rate_limit", fake_rate)
    monkeypatch.setattr(routes_mcp, "settle", fake_settle)
    monkeypatch.setattr(routes_mcp, "_upstream_session", fake_upstream_session)
    monkeypatch.setattr(registry, "_mcp_servers_cache", [_MEMORY_SERVER])


def test_mcp_direct_call_path_sends_overrides(master_key, monkeypatch: pytest.MonkeyPatch) -> None:
    """The /mcp call path (mcp_call tool) merges the key's overrides."""
    fake = CredsFakeDB()
    kid = uuid.uuid4()
    _store_creds(fake, kid, "memory", {"X-Memory-Dataset": "ds1", "X-Dataset-Password": "pw"})
    captured: dict = {}
    _patch_mcp_call_path(monkeypatch, fake, captured)

    key = _key(id=kid)
    ctx = routes_mcp._caller.set(key)
    try:
        out = asyncio.run(routes_mcp._mcp_call("memory", "search", {}))
    finally:
        routes_mcp._caller.reset(ctx)
    assert json.loads(out)["ok"] is True
    assert captured["url"] == "http://memory.svc:8080"
    assert captured["headers"]["X-Memory-Dataset"] == "ds1"
    assert captured["headers"]["X-Dataset-Password"] == "pw"
    assert captured["headers"]["X-Server-Base"] == "1"


def test_relay_executor_sends_overrides(master_key, monkeypatch: pytest.MonkeyPatch) -> None:
    """The relay executor (tool calls during X-PCAI-Execute-Tools chat) hits
    the same merged header build: the fake server receives the overrides."""
    fake = CredsFakeDB()
    kid = uuid.uuid4()
    _store_creds(fake, kid, "memory", {"X-Memory-Dataset": "ds1", "X-Dataset-Password": "pw"})
    captured: dict = {}

    async def fake_rate(key_id: uuid.UUID, rpm: int, tpm: int, est: int):
        return GateResult(True)

    async def fake_settle(*a: Any, **k: Any) -> None:
        return None

    async def fake_upstream_session(url: str, headers: dict, op: Any, timeout_s: float | None = None):
        captured["url"] = url
        captured["headers"] = dict(headers)
        return await op(_FakeSession())

    monkeypatch.setattr(routes_mcp, "db", fake)
    monkeypatch.setattr(routes_proxy, "check_rate_limit", fake_rate)
    monkeypatch.setattr(routes_proxy, "settle", fake_settle)
    monkeypatch.setattr(routes_mcp, "_upstream_session", fake_upstream_session)
    monkeypatch.setattr(registry, "_mcp_servers_cache", [_MEMORY_SERVER])

    execute = asyncio.run(routes_proxy._relay_executor(_key(id=kid), "req-relay-1"))
    ok, _content = asyncio.run(execute("memory", "search", {}))
    assert ok is True
    assert captured["headers"]["X-Memory-Dataset"] == "ds1"
    assert captured["headers"]["X-Dataset-Password"] == "pw"
    assert captured["headers"]["X-Server-Base"] == "1"


def test_relay_executor_without_overrides_still_server_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    """A key with no stored overrides gets plain server-level headers + the
    attribution stamp (key-fp; the fake has no subject row)."""
    captured: dict = {}
    kid = uuid.uuid4()

    async def fake_rate(key_id: uuid.UUID, rpm: int, tpm: int, est: int):
        return GateResult(True)

    async def fake_settle(*a: Any, **k: Any) -> None:
        return None

    async def fake_upstream_session(url: str, headers: dict, op: Any, timeout_s: float | None = None):
        captured["headers"] = dict(headers)
        return await op(_FakeSession())

    monkeypatch.setattr(routes_mcp, "db", CredsFakeDB())
    monkeypatch.setattr(routes_proxy, "check_rate_limit", fake_rate)
    monkeypatch.setattr(routes_proxy, "settle", fake_settle)
    monkeypatch.setattr(routes_mcp, "_upstream_session", fake_upstream_session)
    monkeypatch.setattr(registry, "_mcp_servers_cache", [_MEMORY_SERVER])

    execute = asyncio.run(routes_proxy._relay_executor(_key(id=kid), "req-relay-2"))
    ok, _ = asyncio.run(execute("memory", "search", {}))
    assert ok is True
    assert captured["headers"] == {"X-Server-Base": "1", "X-MCP-Caller": f"{kid}@gateway"}


def test_mcp_call_without_stored_creds_sends_base_headers(master_key, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = CredsFakeDB()
    captured: dict = {}
    _patch_mcp_call_path(monkeypatch, fake, captured)
    ctx = routes_mcp._caller.set(_key())
    try:
        out = asyncio.run(routes_mcp._mcp_call("memory", "search", {}))
    finally:
        routes_mcp._caller.reset(ctx)
    assert json.loads(out)["ok"] is True
    assert "X-Memory-Dataset" not in captured["headers"]


def test_corrupt_override_json_degrades(master_key, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = CredsFakeDB()
    kid = uuid.uuid4()
    fake.creds[(kid, "memory")] = "not-a-fernet-token"
    monkeypatch.setattr(routes_mcp, "db", fake)
    target = {"name": "memory", "headers": {"X-Base": "1"}, "auth_secret": None, "auth_token_enc": None}
    headers = asyncio.run(routes_mcp.resolve_mcp_headers(target, key_id=kid))
    assert headers == {"X-Base": "1", "X-MCP-Caller": f"{kid}@gateway"}


def test_undecryptable_override_json_degrades(master_key, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = CredsFakeDB()
    kid = uuid.uuid4()
    # valid Fernet token, but garbage inside (e.g. written by another writer)
    fake.creds[(kid, "memory")] = secretbox.encrypt("this is not json")
    monkeypatch.setattr(routes_mcp, "db", fake)
    target = {"name": "memory", "headers": {"X-Base": "1"}, "auth_secret": None, "auth_token_enc": None}
    headers = asyncio.run(routes_mcp.resolve_mcp_headers(target, key_id=kid))
    assert headers == {"X-Base": "1", "X-MCP-Caller": f"{kid}@gateway"}


def test_gate_result_shape_unchanged() -> None:
    """Guard against silent budget-module drift the fakes above depend on."""
    g = GateResult(True)
    assert g.ok and g.remaining_requests is None
    assert SimpleNamespace(ok=True).ok
