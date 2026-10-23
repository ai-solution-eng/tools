"""GET /v1/identity — the whoami probe (brief §Z2).

Pins the anti-oracle contract and the resolution semantics:

* ANONYMOUS SHAPE (the MM RAG lesson): an unauthenticated request returns
  EXACTLY {"authenticated": false, "subject": null, "idp": null,
  "key_prefix": null, "scopes": [], "flags": {"self_mint": <bool>}} — six
  top-level keys, one nested flag, no more, ever.
* self_mint = trust_proxy_identity AND sso_key_create_enabled AND the
  request resolves a browser-header identity (ladder rung 1) AND a
  parsable PCAI_LLM_TOKEN_SHADOW_TENANT_ID (2026-10-08 parity: the mint
  503s without a tenant — a key needs a tenancy holder — and the SPA
  silently hides the card on any non-201/409 mint response, so a probe
  that ignored the tenant offered a card that vanished on click)
  AND the subject passes the eligibility hook — the exact
  precondition under which POST /v1/keys/for-me would succeed (the full
  flag/header matrix).
* A valid virtual key request shows its OWN key_prefix/scopes and its
  binding's subject/idp; an unbound key is pseudonymous (null subject, no
  binding idp). An admin-presented key is reported like any other key —
  no special casing, no admin flag.
* Z1 precedence (mirrored here — auth.py is z1's file): an explicit
  X-API-Key outranks the co-forwarded proxy Bearer; a dead X-API-Key is
  NOT rescued by the bearer except when the bearer is itself a live key
  (D19: the fall-through exists for invalid tokens, not dead keys).
* PUBLIC route: no auth dependency; resolver/DB/JWKS exceptions → 503
  (fail-closed — the anonymous shape is for RESOLVED-anonymous only).

Suite convention (test_identity_phaseb.py): no live Postgres, NO NETWORK —
db calls are monkeypatched stateful fakes; JWTs are signed with a local
RSA key pair and the JWKS fetch is faked to the local key.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from fastapi.testclient import TestClient

import pcai_llm.core.keys as keys_mod
from pcai_llm.api import routes_identity
from pcai_llm.api.routes_identity import resolve_request_identity
from pcai_llm.config import settings
from pcai_llm.core import identity
from pcai_llm.core.keys import KeyRecord, hash_key

ANONYMOUS: dict[str, Any] = {
    "authenticated": False,
    "subject": None,
    "idp": None,
    "key_prefix": None,
    "scopes": [],
    "flags": {"self_mint": False},
}


class _FakeState:
    """Starlette request.state stub (plain attribute bag) for direct
    resolve_request_identity calls outside a TestClient."""

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    def __getattr__(self, name: str) -> Any:
        try:
            return self.__dict__["_data"][name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def __setattr__(self, name: str, value: Any) -> None:
        self.__dict__.setdefault("_data", {})[name] = value


# ---------------------------------------------------------------------------
# fixtures: a local IdP (for token-rung tests) and a stateful fake ledger
# ---------------------------------------------------------------------------

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_JWK = jwt.algorithms.RSAAlgorithm.to_jwk(_KEY.public_key(), as_dict=True)
_JWK["kid"] = "probe-key-1"
_JWK["use"] = "sig"
_JWKS_DOC = {"keys": [_JWK]}


def _sign(claims: dict[str, Any], kid: str = "probe-key-1") -> str:
    return jwt.encode(claims, _KEY, algorithm="RS256", headers={"kid": kid})


def _claims(**over: Any) -> dict[str, Any]:
    now = datetime.now(UTC)
    base = {
        "iss": "https://idp.example/realms/pcai",
        "aud": "pcai-gateway",
        "sub": "user-alice-1",
        "exp": int((now + timedelta(minutes=30)).timestamp()),
        "iat": int(now.timestamp()),
    }
    base.update(over)
    return base


def _token_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The token-configured posture with the LOCAL JWKS (no network)."""
    monkeypatch.setattr(settings, "token_issuer", "https://idp.example/realms/pcai")
    monkeypatch.setattr(settings, "token_jwks_url", "https://idp.example/jwks.json")
    monkeypatch.setattr(settings, "token_audience", "pcai-gateway")
    monkeypatch.setattr(identity, "_fetch_jwks", lambda url: _JWKS_DOC)
    identity._reset_token_cache()


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


class ProbeDB:
    """The fake ledger surface the probe touches: authenticate()'s SELECT
    (via core.keys.db), the subjects binding join (identity.py), the
    eligibility hook's subjects→api_keys→users JOIN, and a break-glass
    switch for the 503 tests."""

    def __init__(self) -> None:
        self.broken = False
        self.entries: list[dict[str, Any]] = []
        # subject -> users.self_mint_enabled (the admin-governed flag the
        # eligibility hook reads; a subject absent here = no user row /
        # no flag → the hook's default-eligible branch)
        self.self_mint_flags: dict[str, bool | None] = {}
        # when set, the eligibility statement itself raises (the hook must
        # propagate — distinct from `broken`, which kills every read)
        self.break_eligibility = False
        # args seen on the eligibility statement (idp, bound_via, subject)
        self.eligibility_calls: list[tuple[Any, ...]] = []

    def add_key(
        self,
        plaintext: str,
        record: KeyRecord,
        *,
        subject: str | None = None,
        idp: str | None = None,
    ) -> None:
        # Store the hash alongside the plaintext: authenticate() queries by
        # key_hash (the fake indexes on the hash — same discipline as the
        # real ledger), the binding join by the record's id.
        self.entries.append(
            {
                "plaintext": plaintext,
                "hash": hash_key(plaintext),
                "record": record,
                "subject": subject,
                "idp": idp,
            }
        )

    def _by_hash(self, key_hash: str) -> dict[str, Any] | None:
        return next((e for e in self.entries if e["hash"] == key_hash), None)

    async def fetchrow(self, sql: str, *args: Any):
        if "self_mint_enabled" in sql and "FROM subjects s" in sql:
            # the eligibility hook's lookup (args: idp, bound_via, subject)
            self.eligibility_calls.append(args)
            if self.break_eligibility:
                raise RuntimeError("eligibility store down")
            flag = self.self_mint_flags.get(str(args[2]))
            if args[2] not in self.self_mint_flags:
                return None  # no bound user row → default-eligible branch
            return {"self_mint_enabled": flag}
        if self.broken:
            raise RuntimeError("database not connected")
        if "JOIN subjects s ON s.id" in sql:
            # the binding join (matches BEFORE the api_keys branch — the
            # binding SQL also contains "FROM api_keys k")
            entry = next((e for e in self.entries if e["record"].id == args[0]), None)
            if entry is None or entry["subject"] is None:
                return None
            return {"subject": entry["subject"], "idp": entry["idp"], "kind": "user", "groups": []}
        if "FROM api_keys k" in sql:
            entry = self._by_hash(str(args[0]))
            if entry is None:
                return None
            rec = entry["record"]
            # the full authenticate() projection (a missing column breaks
            # its row->record mapping)
            return {
                "id": rec.id,
                "user_id": rec.user_id,
                "name": rec.name,
                "key_prefix": rec.key_prefix,
                "scopes": rec.scopes,
                "allowed_models": rec.allowed_models,
                "rate_limit_rpm": rec.rate_limit_rpm,
                "rate_limit_tpm": rec.rate_limit_tpm,
                "max_concurrent": rec.max_concurrent,
                "expires_at": None,
                "revoked_at": None,
                "relay_default": False,
                "stream_pcai_events": False,
                "offpeak_fraction": None,
            }
        return None

    async def fetch(self, sql: str, *args: Any):
        return []

    async def fetchval(self, sql: str, *args: Any):
        return None

    async def execute(self, sql: str, *args: Any):
        return "UPDATE 0"


@pytest.fixture
def probe_env(monkeypatch: pytest.MonkeyPatch):
    """Default posture: flags OFF (the shipped default), ledger empty and
    healthy, token rung inert. Returns (client, fake-db)."""
    fake = ProbeDB()
    monkeypatch.setattr(identity, "db", fake)
    monkeypatch.setattr(keys_mod, "db", fake)  # authenticate() reads THIS module-level db
    monkeypatch.setattr(settings, "trust_proxy_identity", False)
    monkeypatch.setattr(settings, "sso_key_create_enabled", False)
    monkeypatch.setattr(settings, "token_issuer", "")
    monkeypatch.setattr(settings, "token_jwks_url", "")
    monkeypatch.setattr(settings, "token_audience", "")
    monkeypatch.setattr(settings, "token_shadow_keys", False)
    # a WORKING mint deployment's baseline (2026-10-08 tenant-parity fix):
    # the mint route 503s without a parsable PCAI_LLM_TOKEN_SHADOW_TENANT_ID,
    # so the probe's self_mint must read it too — the fixture baseline is the
    # configured case; the no-tenant / bad-tenant regressions pin the fix.
    monkeypatch.setattr(settings, "token_shadow_tenant_id", "00000000-0000-0000-0000-000000000001")
    identity._reset_token_cache()
    yield _probe_app(), fake
    identity._reset_token_cache()


def _probe_app() -> TestClient:
    """A fresh app with ONLY the identity router mounted (unit scope: the
    probe must not depend on any other surface being wired)."""
    app = FastAPI()
    app.include_router(routes_identity.router)
    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# 1. THE anonymous shape — exact keys, no more (the anti-oracle)
# ---------------------------------------------------------------------------


def test_anonymous_shape_exact_keys(probe_env) -> None:
    """Six top-level keys, exactly one nested flag — no extra keys, no
    status field, nothing. This is the byte-shape the SPA branches on."""
    tc, _ = probe_env
    r = tc.get("/v1/identity")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body == ANONYMOUS
    assert set(body) == {"authenticated", "subject", "idp", "key_prefix", "scopes", "flags"}
    assert set(body["flags"]) == {"self_mint"}
    assert body["scopes"] == []


def test_anonymous_shape_with_garbage_credentials(probe_env) -> None:
    """Wrong-scheme Authorization, dead bearer, dead X-API-Key, dead pair:
    all resolve nothing and all get the SAME exact anonymous shape (two
    dead credentials are not an oracle either)."""
    tc, _ = probe_env
    for headers in (
        {"Authorization": "Basic dXNlcjpwYXNz"},
        {"Authorization": "Bearer not-a-real-key"},
        {"X-API-Key": "hllm-deadbeefdeadbeef"},
        {"X-API-Key": "hllm-deadbeefdeadbeef", "Authorization": "Bearer not-a-real-key"},
    ):
        r = tc.get("/v1/identity", headers=headers)
        assert r.status_code == 200, headers
        assert r.json() == ANONYMOUS, headers


# ---------------------------------------------------------------------------
# 2. self_mint semantics — the exact for-me precondition (ladder rung 1)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("trust", "create", "browser_headers"),
    [
        (False, False, False),
        (False, False, True),
        (False, True, False),
        (False, True, True),
        (True, False, False),
        (True, False, True),
        (True, True, False),
        (True, True, True),
    ],
)
def test_self_mint_flag_matrix(
    probe_env, monkeypatch: pytest.MonkeyPatch, trust: bool, create: bool, browser_headers: bool
) -> None:
    """self_mint = trust AND sso_key_create_enabled AND browser headers —
    all three, over the full matrix. Headers present with flags off ⇒
    false (no oracle); flags on without headers ⇒ false (for-me would
    401 this request)."""
    tc, _ = probe_env
    monkeypatch.setattr(settings, "trust_proxy_identity", trust)
    monkeypatch.setattr(settings, "sso_key_create_enabled", create)
    headers = {"X-Auth-Request-User": "alice", "X-Forwarded-Groups": "eng"} if browser_headers else {}
    r = tc.get("/v1/identity", headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert body["flags"]["self_mint"] is (trust and create and browser_headers)
    if browser_headers and trust:
        # ladder rung 1 resolved: the caller's subject is visible PRE-mint
        # (who the SPA would mint for), still authenticated=false —
        # enforcement 401s this same request until a key exists.
        assert body["authenticated"] is False
        assert body["subject"] == "alice"
        assert body["key_prefix"] is None
        assert body["idp"] is None


def test_self_mint_precondition_matches_for_me_reader(probe_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """The probe's rung-1 read IS the for-me read: the same pure helper
    (identity.browser_identity_from_headers) over the same headers — the
    X-Email fallback counts, exactly as it does at the mint route."""
    tc, _ = probe_env
    monkeypatch.setattr(settings, "trust_proxy_identity", True)
    monkeypatch.setattr(settings, "sso_key_create_enabled", True)
    r = tc.get("/v1/identity", headers={"X-Email": "alice@corp"})
    assert r.json()["flags"]["self_mint"] is True
    # and the direct read agrees (case-insensitive, email fallback):
    assert identity.browser_identity_from_headers({"x-email": "alice@corp"}) == ("alice@corp", [])


def test_self_mint_requires_configured_tenant_no_tenant(probe_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """⚠️ 2026-10-08 tenant parity (found live on G2): the mint route 503s
    when PCAI_LLM_TOKEN_SHADOW_TENANT_ID is unset — a key needs a tenancy
    holder (users.user_id NOT NULL; the mint refuses to invent a tenant).
    The probe MUST mirror that refusal or it offers the card where the
    click 503s — and the SPA hides the card on any non-201/409 (silent,
    no banner), so the user watches the mint affordance vanish on click.
    Empty tenant ⇒ self_mint false with the trust/create/browser terms
    all green (G2's exact shape)."""
    tc, _ = probe_env
    monkeypatch.setattr(settings, "trust_proxy_identity", True)
    monkeypatch.setattr(settings, "sso_key_create_enabled", True)
    monkeypatch.setattr(settings, "token_shadow_tenant_id", "")
    r = tc.get("/v1/identity", headers={"X-Auth-Request-User": "alice"})
    assert r.status_code == 200
    assert r.json()["flags"]["self_mint"] is False


def test_self_mint_requires_configured_tenant_bad_tenant(probe_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """The mint's parse is uuid.UUID() — a MALFORMED tenant id must read
    the same as an absent one (parity with routes_enroll's ValueError →
    503 branch), not crash the probe and not read as mintable."""
    tc, _ = probe_env
    monkeypatch.setattr(settings, "trust_proxy_identity", True)
    monkeypatch.setattr(settings, "sso_key_create_enabled", True)
    monkeypatch.setattr(settings, "token_shadow_tenant_id", "not-a-tenant-uuid")
    r = tc.get("/v1/identity", headers={"X-Auth-Request-User": "alice"})
    assert r.status_code == 200
    assert r.json()["flags"]["self_mint"] is False


def test_self_mint_with_tenant_configured_still_mintable(probe_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """The tenant term must not over-block: with a VALID tenant UUID set
    (the fixture baseline) and every other term green, self_mint stays
    true — the G2 fix repairs the broken case without breaking the
    working one."""
    tc, _ = probe_env
    monkeypatch.setattr(settings, "trust_proxy_identity", True)
    monkeypatch.setattr(settings, "sso_key_create_enabled", True)
    assert settings.token_shadow_tenant_id  # fixture baseline: a valid UUID
    r = tc.get("/v1/identity", headers={"X-Auth-Request-User": "alice"})
    assert r.json()["flags"]["self_mint"] is True


def test_subject_prefers_preferred_username_header(probe_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """The 'preferred_username approach' (z1's landing): when BOTH
    X-Auth-Request-User and X-Auth-Request-Preferred-Username are present,
    the probe's subject MUST be the Preferred-Username value — the
    identity claim convention (oauth2-proxy forwards the IdP's
    preferred_username), not the bare user claim.

    SELF-ACTIVATING: identity.browser_identity_from_headers is Z1's file
    (core/identity.py is NOT this zone's). Until z1 lands the preference
    the helper returns the X-Auth-Request-User value and this test SKIPS
    with the blocked-on-z1 marker — the moment z1 lands, the skip
    condition flips false and this becomes an enforced assertion. It
    deliberately does NOT pin today's User-header behavior (z1's landing
    would break it); the resolution itself rides the shared helper, so
    the probe inherits the preference with zero route changes."""
    tc, _ = probe_env
    monkeypatch.setattr(settings, "trust_proxy_identity", True)
    monkeypatch.setattr(settings, "sso_key_create_enabled", True)
    helper = identity.browser_identity_from_headers
    sample = {"X-Auth-Request-User": "alice", "X-Auth-Request-Preferred-Username": "alice.corp"}
    if helper(sample) != ("alice.corp", []):
        pytest.skip(
            "blocked-on-z1: browser_identity_from_headers does not prefer X-Auth-Request-Preferred-Username yet"
        )
    r = tc.get(
        "/v1/identity",
        headers={"X-Auth-Request-User": "alice", "X-Auth-Request-Preferred-Username": "alice.corp"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["subject"] == "alice.corp"
    assert body["authenticated"] is False  # browser rung: no key anchored
    assert body["flags"]["self_mint"] is True  # flags on in this fixture posture


# ---------------------------------------------------------------------------
# 2b. self_mint eligibility hook (the shared admin-governance seam) —
#     the REAL lookup (wave-1 finding F4): subjects→api_keys→users JOIN,
#     reading users.self_mint_enabled for the subject's newest live
#     sso-create key's user; default-ELIGIBLE when no row resolves.
# ---------------------------------------------------------------------------


def test_eligibility_unknown_subject_is_default_eligible(probe_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """No sso-create key / no user row for the subject ⇒ default-eligible:
    self_mint stays true with every flag on — the FIRST-mint flow must
    never be gated by a row that cannot exist yet."""
    tc, fake = probe_env
    monkeypatch.setattr(settings, "trust_proxy_identity", True)
    monkeypatch.setattr(settings, "sso_key_create_enabled", True)
    assert fake.self_mint_flags == {}  # no eligibility data at all
    r = tc.get("/v1/identity", headers={"X-Auth-Request-User": "alice"})
    assert r.json()["flags"]["self_mint"] is True


def test_eligibility_flag_true_allows_self_mint(probe_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """A bound user row with self_mint_enabled=TRUE ⇒ self_mint true —
    the explicit admin 'allowed' writes the same answer the default
    gives, through the REAL lookup (not the hook stub)."""
    tc, fake = probe_env
    monkeypatch.setattr(settings, "trust_proxy_identity", True)
    monkeypatch.setattr(settings, "sso_key_create_enabled", True)
    fake.self_mint_flags["alice"] = True
    r = tc.get("/v1/identity", headers={"X-Auth-Request-User": "alice"})
    assert r.json()["flags"]["self_mint"] is True


def test_eligibility_flag_false_blocks_self_mint(probe_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """A bound user row with self_mint_enabled=FALSE ⇒ self_mint FALSE
    even with every flag on and browser headers present — the
    admin-governed denial the flag exists to express (wave-1 F4: the
    console toggle now has a reader). The rest of the shape is unchanged
    (the subject is still who the request resolves to)."""
    tc, fake = probe_env
    monkeypatch.setattr(settings, "trust_proxy_identity", True)
    monkeypatch.setattr(settings, "sso_key_create_enabled", True)
    fake.self_mint_flags["alice"] = False
    r = tc.get("/v1/identity", headers={"X-Auth-Request-User": "alice"})
    assert r.status_code == 200
    body = r.json()
    assert body["flags"]["self_mint"] is False
    assert body["subject"] == "alice"  # identity unaffected by eligibility


def test_eligibility_flag_null_reads_true(probe_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """A NULL flag cell reads True (the un-migrated-DB posture — the
    schema's NOT NULL + server default make NULL unreachable in practice,
    but the lookup never lets a missing value deny anyone)."""
    tc, fake = probe_env
    monkeypatch.setattr(settings, "trust_proxy_identity", True)
    monkeypatch.setattr(settings, "sso_key_create_enabled", True)
    fake.self_mint_flags["alice"] = None
    r = tc.get("/v1/identity", headers={"X-Auth-Request-User": "alice"})
    assert r.json()["flags"]["self_mint"] is True


async def _recorder(subject: str) -> bool:
    _recorder.calls.append(subject)  # type: ignore[attr-defined]
    return True


_recorder.calls = []  # type: ignore[attr-defined]


def test_eligibility_lookup_uses_sso_create_and_oauth2_proxy_idp(probe_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """The lookup is scoped to the MINT flow's own binding shape: pinned
    via the SHARED constants (IDP_OAUTH2_PROXY + bound_via='sso-create') —
    the same binding the mint writes and the admin console's
    subject-selector write resolves through, so write and read always
    land on the same flag row."""
    tc, fake = probe_env
    monkeypatch.setattr(settings, "trust_proxy_identity", True)
    monkeypatch.setattr(settings, "sso_key_create_enabled", True)
    fake.self_mint_flags["alice"] = True
    tc.get("/v1/identity", headers={"X-Auth-Request-User": "alice"})
    assert fake.eligibility_calls
    args = fake.eligibility_calls[-1]
    assert args[0] == identity.IDP_OAUTH2_PROXY
    assert args[1] == identity.VIA_SSO_CREATE
    assert args[2] == "alice"


def test_eligibility_hook_is_consulted_with_resolved_subject(probe_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """The hook receives the RESOLVED subject (the browser rung's claim)
    and is actually consulted — the seam the admin model keys on."""
    tc, _ = probe_env
    monkeypatch.setattr(settings, "trust_proxy_identity", True)
    monkeypatch.setattr(settings, "sso_key_create_enabled", True)
    _recorder.calls.clear()  # type: ignore[attr-defined]
    monkeypatch.setattr(routes_identity, "_self_mint_allowed_for_subject", _recorder)
    r = tc.get("/v1/identity", headers={"X-Auth-Request-User": "alice"})
    assert r.json()["flags"]["self_mint"] is True
    assert _recorder.calls == ["alice"]  # type: ignore[attr-defined]


def test_eligibility_hook_failure_is_503_fail_closed(probe_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """RAISE-DON'T-FALSE (the wave-1 agreement, now against the REAL
    lookup): a broken eligibility store makes the probe 503 — it never
    reads as 'ineligible'. A silent False would deny every user during an
    outage with a refusal that lies about the cause; a False RETURN must
    always mean the lookup SUCCEEDED and said no. Pinned through the
    hook's own DB seam (break_eligibility), not a monkeypatched hook —
    so it guards z3's future body swap too."""
    tc, fake = probe_env
    monkeypatch.setattr(settings, "trust_proxy_identity", True)
    monkeypatch.setattr(settings, "sso_key_create_enabled", True)
    fake.self_mint_flags["alice"] = True  # a healthy lookup would say yes
    fake.break_eligibility = True
    r = tc.get("/v1/identity", headers={"X-Auth-Request-User": "alice"})
    assert r.status_code == 503
    assert "unavailable" in r.json()["detail"]


# ---------------------------------------------------------------------------
# 3. key-presented shapes — the caller's own key, admin keys included
# ---------------------------------------------------------------------------


def test_bound_key_shape(probe_env) -> None:
    """A valid identity-bound key: authenticated=true, its real
    key_prefix/scopes, the binding's subject + idp."""
    tc, fake = probe_env
    rec = _key(key_prefix="hllm-AbCdEf", scopes=["proxy", "mcp"])
    fake.add_key("hllm-live-bound-key", rec, subject="alice", idp=identity.IDP_OAUTH2_PROXY)
    r = tc.get("/v1/identity", headers={"Authorization": "Bearer hllm-live-bound-key"})
    assert r.status_code == 200, r.text
    assert r.json() == {
        "authenticated": True,
        "subject": "alice",
        "idp": identity.IDP_OAUTH2_PROXY,
        "key_prefix": "hllm-AbCdEf",
        "scopes": ["proxy", "mcp"],
        "flags": {"self_mint": False},
    }


def test_unbound_key_pseudonymous_shape(probe_env) -> None:
    """An unbound (anonymous-fp) key: authenticated=true with its OWN
    prefix/scopes, subject null, no binding idp (rung 4 — enforcement
    unchanged; this is the caller's own key, not a leak)."""
    tc, fake = probe_env
    rec = _key(key_prefix="hllm-QrStUv", scopes=["mcp"])
    fake.add_key("hllm-live-anon-key", rec)
    r = tc.get("/v1/identity", headers={"Authorization": "Bearer hllm-live-anon-key"})
    assert r.status_code == 200
    assert r.json() == {
        "authenticated": True,
        "subject": None,
        "idp": None,
        "key_prefix": "hllm-QrStUv",
        "scopes": ["mcp"],
        "flags": {"self_mint": False},
    }


def test_admin_key_seen_like_any_other_key(probe_env) -> None:
    """NO admin special casing on this surface: an admin-presented key is
    reported exactly like anyone else's key — no admin flag, no extra
    fields; nothing in the response reveals its authority beyond the key's
    own scopes."""
    tc, fake = probe_env
    rec = _key(key_prefix="hllm-Adm1nKey", scopes=["admin", "proxy", "mcp"])
    fake.add_key("hllm-admin-key-plaintext", rec, subject="root", idp=identity.IDP_ADMIN_ASSERTED)
    r = tc.get("/v1/identity", headers={"Authorization": "Bearer hllm-admin-key-plaintext"})
    assert r.status_code == 200
    body = r.json()
    assert body["authenticated"] is True
    assert body["subject"] == "root"
    assert body["idp"] == identity.IDP_ADMIN_ASSERTED
    assert body["scopes"] == ["admin", "proxy", "mcp"]
    assert set(body) == set(ANONYMOUS)  # the same six keys, nothing added


def test_browser_headers_do_not_shadow_key_binding(probe_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """Rungs 3-4 rule: once a key resolved, its OWN binding is the word —
    the proxy always injects browser headers, and they must not overwrite
    the ledger's subject (require_key never consults the browser rung for
    a keyed request; neither does the probe)."""
    tc, fake = probe_env
    rec = _key()
    fake.add_key("hllm-live-key", rec, subject="ledger-alice", idp=identity.IDP_KEYCLOAK)
    monkeypatch.setattr(settings, "trust_proxy_identity", True)
    r = tc.get(
        "/v1/identity",
        headers={"Authorization": "Bearer hllm-live-key", "X-Auth-Request-User": "proxy-bob"},
    )
    assert r.json()["subject"] == "ledger-alice"


# ---------------------------------------------------------------------------
# 4. X-API-Key precedence (the Z1 envelope, mirrored — auth.py is z1's)
# ---------------------------------------------------------------------------


def test_x_api_key_outranks_proxy_bearer(probe_env) -> None:
    """Both envelopes present: X-API-Key wins (the persistent ledger anchor
    over the co-forwarded 30-minute proxy token — D19 de-escalation-only)."""
    tc, fake = probe_env
    rec = _key(key_prefix="hllm-ViaXKey", scopes=["proxy"])
    fake.add_key("hllm-explicit-key", rec, subject="alice", idp=identity.IDP_OAUTH2_PROXY)
    r = tc.get(
        "/v1/identity",
        headers={
            "X-API-Key": "hllm-explicit-key",
            "Authorization": "Bearer hllm-not-a-registered-key",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["authenticated"] is True
    assert body["key_prefix"] == "hllm-ViaXKey"
    assert body["subject"] == "alice"


def test_dead_x_api_key_not_rescued_by_dead_bearer(probe_env) -> None:
    """A dead EXPLICIT key plus a co-forwarded bearer that is neither a
    live key nor a verifiable token ⇒ the anonymous shape (no escalation;
    and no refusal either — the probe describes, it does not authorize)."""
    tc, _ = probe_env
    r = tc.get(
        "/v1/identity",
        headers={"X-API-Key": "hllm-dead", "Authorization": "Bearer hllm-alsodead"},
    )
    assert r.json() == ANONYMOUS


def test_dead_x_api_key_with_live_bearer_key_is_refusal_pair(probe_env) -> None:
    """The brief §Z1 final word (z1's landed posture): two keys presented
    with one dead = REFUSE, fail-closed — the dead X-API-Key is not
    rescued by the live co-forwarded bearer key either. On the PUBLIC
    probe a refusal is indistinguishable from anonymous (it must not
    become an oracle)."""
    tc, _fake = probe_env
    r = tc.get(
        "/v1/identity",
        headers={"X-API-Key": "hllm-dead", "Authorization": "Bearer hllm-bearer-key"},
    )
    assert r.json() == ANONYMOUS


# ---------------------------------------------------------------------------
# 5. token rung on the probe (shadow-key anchor / dead-token fall-through)
# ---------------------------------------------------------------------------


def test_valid_token_with_shadow_key_reports_it(probe_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """A VALID token + a live shadow key ⇒ the same shape a key request
    gets: the shadow key is the ledger anchor — what require_key would
    admit. No network: the JWKS fetch is the local doc."""
    tc, fake = probe_env
    _token_env(monkeypatch)
    rec = _key(key_prefix="hllm-ShadowK", scopes=["proxy", "mcp"])
    # a REAL shadow key is bound (bound_via='token-shadow' → a subjects
    # row): register the binding so the key-rung re-resolution agrees with
    # the token rung that found the key.
    fake.add_key("shadow-plaintext-unused", rec, subject="user-alice-1", idp=identity.IDP_KEYCLOAK)

    async def fake_find_shadow(subject: str) -> KeyRecord | None:
        if subject == "user-alice-1":
            return rec
        return None

    monkeypatch.setattr(identity, "resolve_shadow_key", fake_find_shadow)
    r = tc.get("/v1/identity", headers={"Authorization": f"Bearer {_sign(_claims())}"})
    assert r.status_code == 200, r.text
    assert r.json() == {
        "authenticated": True,
        "subject": "user-alice-1",
        "idp": identity.IDP_KEYCLOAK,
        "key_prefix": "hllm-ShadowK",
        "scopes": ["proxy", "mcp"],
        "flags": {"self_mint": False},
    }


def test_valid_token_without_key_still_shows_subject(probe_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """A VALID token with NO shadow key: the identity resolved (subject
    visible — the token was verified), but authenticated=false — no ledger
    key anchors the request, exactly what enforcement would 401."""
    tc, _fake = probe_env
    _token_env(monkeypatch)

    async def no_shadow(subject: str) -> KeyRecord | None:
        return None

    monkeypatch.setattr(identity, "resolve_shadow_key", no_shadow)
    r = tc.get("/v1/identity", headers={"Authorization": f"Bearer {_sign(_claims())}"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body == {
        "authenticated": False,
        "subject": "user-alice-1",
        "idp": None,
        "key_prefix": None,
        "scopes": [],
        "flags": {"self_mint": False},
    }


def test_token_and_browser_headers_report_header_spelling(probe_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """⚠️ 2026-10-08 SUBJECT-SPELLING PARITY (G2): behind the chained edge
    the proxy injects BOTH a Bearer id_token AND the browser headers. The
    token rung validates and its subject is the IdP's ``sub`` (a UUID on
    G2); the MINT binds the HEADER subject (X-Auth-Request-Preferred-
    Username). The probe must report the spelling the mint will bind —
    the chip and the mint anchor must agree, or the pre-mint identity and
    the minted key's subject are two different strings for one human.
    Valid token + browser headers ⇒ subject = header value (NOT sub),
    still authenticated=false (no ledger key yet)."""
    tc, _fake = probe_env
    _token_env(monkeypatch)
    monkeypatch.setattr(settings, "trust_proxy_identity", True)
    monkeypatch.setattr(settings, "sso_key_create_enabled", True)

    async def no_shadow(subject: str) -> KeyRecord | None:
        return None

    monkeypatch.setattr(identity, "resolve_shadow_key", no_shadow)
    r = tc.get(
        "/v1/identity",
        headers={
            "Authorization": f"Bearer {_sign(_claims())}",  # sub = user-alice-1 (the UUID analog)
            "X-Auth-Request-User": "alice",
            "X-Auth-Request-Preferred-Username": "alice",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["subject"] == "alice"  # the MINT's spelling — not "user-alice-1"
    assert body["authenticated"] is False
    assert body["flags"]["self_mint"] is True  # eligibility computed on the SAME anchor


def test_token_only_still_reports_sub_when_no_headers(probe_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """The parity fix must not over-reach: a token caller WITHOUT browser
    headers (pure API/shadow-key surface, or trust off) keeps the token
    rung's sub as the reported subject — token doctrine is unchanged off
    the chained edge."""
    tc, _fake = probe_env
    _token_env(monkeypatch)
    monkeypatch.setattr(settings, "trust_proxy_identity", True)

    async def no_shadow(subject: str) -> KeyRecord | None:
        return None

    monkeypatch.setattr(identity, "resolve_shadow_key", no_shadow)
    r = tc.get("/v1/identity", headers={"Authorization": f"Bearer {_sign(_claims())}"})
    assert r.status_code == 200, r.text
    assert r.json()["subject"] == "user-alice-1"  # token sub, headers absent


def test_token_and_headers_with_trust_off_report_sub(probe_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """Trust off ⇒ headers are untrusted noise — the token rung owns the
    report even when header-shaped values are present (the trust gate is
    the same one the mint applies)."""
    tc, _fake = probe_env
    _token_env(monkeypatch)
    monkeypatch.setattr(settings, "trust_proxy_identity", False)

    async def no_shadow(subject: str) -> KeyRecord | None:
        return None

    monkeypatch.setattr(identity, "resolve_shadow_key", no_shadow)
    r = tc.get(
        "/v1/identity",
        headers={
            "Authorization": f"Bearer {_sign(_claims())}",
            "X-Auth-Request-User": "alice",
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["subject"] == "user-alice-1"


def test_invalid_token_falls_through_to_key(probe_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """THE fall-through rule on the probe: an EXPIRED token (JWT-shaped,
    fails validation) is not the end — the same bearer still reaches
    authenticate(), so a stale-JWT-wrapped key resolves (the 30-min TTL
    stays invisible to key holders, review §1.1)."""
    tc, fake = probe_env
    _token_env(monkeypatch)
    expired = _sign(_claims(exp=int((datetime.now(UTC) - timedelta(minutes=1)).timestamp())))
    rec = _key(key_prefix="hllm-KeyInTok", scopes=["proxy"])
    fake.add_key(expired, rec, subject="carol", idp=identity.IDP_SELF_ASSERTED)
    r = tc.get("/v1/identity", headers={"Authorization": f"Bearer {expired}"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["authenticated"] is True
    assert body["key_prefix"] == "hllm-KeyInTok"
    assert body["subject"] == "carol"


# ---------------------------------------------------------------------------
# 6. PUBLIC route + fail-closed 503
# ---------------------------------------------------------------------------


def test_route_is_public_no_auth_dependency() -> None:
    """The route carries NO auth dependency: nothing from api.auth sits on
    it — the SPA's pre-key boot call cannot trip a 401/403 by construction
    (not just because no credential was sent)."""
    route = next(rt for rt in routes_identity.router.routes if getattr(rt, "path", "") == "/v1/identity")
    dep_calls = [getattr(d.call, "__name__", str(d.call)) for d in route.dependant.dependencies]
    assert not dep_calls or all("require" not in name for name in dep_calls)
    app = FastAPI()
    app.include_router(routes_identity.router)
    with TestClient(app, raise_server_exceptions=False) as tc:
        assert tc.get("/v1/identity").status_code == 200  # not 401/403


def test_db_failure_is_503_never_anonymous(probe_env) -> None:
    """Fail-closed: the identity ledger DOWN + a key presented ⇒ 503. The
    anonymous shape is for RESOLVED-anonymous, never infrastructure
    failure (a silent anonymous here would tell every key holder they are
    logged out whenever the DB blips)."""
    tc, fake = probe_env
    fake.broken = True
    fake.add_key("hllm-live-key", _key())
    r = tc.get("/v1/identity", headers={"Authorization": "Bearer hllm-live-key"})
    assert r.status_code == 503
    assert "unavailable" in r.json()["detail"]


def test_db_failure_on_key_presented_request_never_200(probe_env) -> None:
    """The unauthenticated-looking variant: ANY credential that would
    reach the ledger (an unknown bearer must still be LOOKED UP) makes the
    request 503, not 200-anonymous — a broken ledger is never reported as
    'you are anonymous'."""
    tc, fake = probe_env
    fake.broken = True
    r = tc.get("/v1/identity", headers={"Authorization": "Bearer whatever-goes-to-db"})
    assert r.status_code == 503


def test_resolver_exception_wrapped_as_503(probe_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """Any resolver exception — a JWKS infrastructure failure on a
    token-presented request — is 503: never 200-anonymous, never a 500
    stack-trace leak."""
    tc, _ = probe_env
    monkeypatch.setattr(settings, "token_issuer", "https://idp.example/realms/pcai")
    monkeypatch.setattr(settings, "token_jwks_url", "https://idp.example/jwks.json")
    monkeypatch.setattr(settings, "token_audience", "pcai-gateway")

    def exploding_fetch(url: str) -> dict[str, Any]:
        raise RuntimeError("jwks exploded")

    monkeypatch.setattr(identity, "_fetch_jwks", exploding_fetch)
    identity._reset_token_cache()
    # JWT-shaped dummy (built from parts: the fleet secret scanner flags any
    # literal three-segment eyJ... token — this is not real material).
    token = ".".join(["eyJhbGciOiJSUzI1NiJ9", "eyJzdWIiOiJ4In0", "c2ln"])  # noqa: FLY002 -- scanner-safe construction of a dummy JWT shape
    r = tc.get("/v1/identity", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 503
    identity._reset_token_cache()


# ---------------------------------------------------------------------------
# 7. the helper contract directly (resolve_request_identity)
# ---------------------------------------------------------------------------


def test_resolve_request_identity_helper_contract(probe_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """The helper returns (key, caller) tuples under the SHARED resolver
    (auth.resolve_bearer_key — the drift-prevention contract: the probe
    literally runs enforcement's resolution). (None, None) is the
    resolved-anonymous input; a REFUSED credential also lands there (the
    probe describes, it does not authorize) with Z1's pre-refusal token
    caller surfaced via request.state."""
    fake = ProbeDB()
    monkeypatch.setattr(identity, "db", fake)
    monkeypatch.setattr(keys_mod, "db", fake)  # authenticate() reads THIS module-level db
    rec = _key(key_prefix="hllm-DirectH", scopes=["mcp"])
    fake.add_key("hllm-direct", rec, subject="dave", idp=identity.IDP_SELF_ASSERTED)

    class _Req:
        def __init__(self) -> None:
            self.headers = {"authorization": "Bearer hllm-direct"}
            self.state = _FakeState()

    key, caller = asyncio.run(resolve_request_identity(_Req()))  # type: ignore[arg-type]
    assert key is not None and key.key_prefix == "hllm-DirectH"
    assert caller is not None and caller.subject == "dave"
    assert caller.subject_kind == identity.KIND_KEY_BOUND

    # keyless + trust off ⇒ (None, None): the resolved-anonymous inputs.
    class _EmptyReq:
        def __init__(self) -> None:
            self.headers: dict[str, str] = {}
            self.state = _FakeState()  # Z1 writes request.state.caller on refusals

    key2, caller2 = asyncio.run(resolve_request_identity(_EmptyReq()))  # type: ignore[arg-type]
    assert key2 is None and caller2 is None

    # a dead X-API-Key is a REFUSAL (z1's fail-closed pair posture): the
    # helper resolves (None, state-caller-or-None) — no exception — and
    # the route turns that into the anonymous shape (enforcement: 401).
    class _DeadKeyReq:
        def __init__(self) -> None:
            self.headers = {"x-api-key": "hllm-dead"}
            self.state = _FakeState()

    key3, caller3 = asyncio.run(resolve_request_identity(_DeadKeyReq()))  # type: ignore[arg-type]
    assert key3 is None and caller3 is None
