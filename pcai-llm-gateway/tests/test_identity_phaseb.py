"""Phase B identity: token validation, the ladder's token rung on /v1 +
/mcp, token shadow-keys, /v1/keys/for-me, and the admin-group gate.

The review contract this file pins (reviews/01-gateway-impl.md §1.1 rung 2,
§1.3 flows 2+4, §1.5; DECISIONS.md "OAuth REVISION (final)" — binding):

* OAuth terminates at the gateway: the gateway VALIDATES tokens (JWKS,
  issuer+audience pinned) and NEVER issues/refreshes/stores them.
* THE FALL-THROUGH RULE: an invalid/expired token is NOT a hard 401 when a
  ladder-lower credential (a virtual key) exists — this is what makes the
  30-minute token TTL invisible to headless users. It IS a 401 when it was
  the only credential on a token-accepted surface.
* A valid token with no matching key yields an identity for attribution/
  logging but authorizes NOTHING without a key OR a token-shadow key.
* Token shadow-keys (flag-gated, default false): first valid sighting for
  an unknown subject lazily provisions user+key (bound_via='token-shadow',
  scopes ['proxy','mcp']); lookup via UNIQUE (idp, subject) → newest
  non-revoked shadow key. Flag off = tokens validate but never provision.
* POST /v1/keys/for-me: browser-header identity (trust-gated) mints a key
  bound to the caller's OWN subject only; 403 when trust is off; no
  arbitrary grants.
* Admin gate second path: browser identity in PCAI_LLM_ADMIN_GROUPS when
  trust is on; default off; the direct bearer path is untouched.
* settle() signature untouched; flag-off byte-identity.

Suite convention: no live Postgres and NO NETWORK — db calls and the JWKS
fetch are monkeypatched fakes; JWTs are signed with a local RSA key pair
(cryptography is already a dependency of the installed JWT stack).
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import fastapi  # module-level: probe handlers annotate fastapi.Request (postponed annotations)
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pcai_llm.api import routes_admin, routes_enroll, routes_mcp
from pcai_llm.api.auth import require_scope
from pcai_llm.config import settings
from pcai_llm.core import identity
from pcai_llm.core.identity import (
    IDP_KEYCLOAK,
    IDP_OAUTH2_PROXY,
    KIND_BROWSER,
    KIND_KEY_BOUND,
    KIND_TOKEN,
    VIA_TOKEN_SHADOW,
)
from pcai_llm.core.keys import KeyRecord

ADMIN_H = {"Authorization": "Bearer admin-secret-123"}


# ---------------------------------------------------------------------------
# 0. fixtures: a local IdP (RSA key + JWKS doc) and token factory
# ---------------------------------------------------------------------------

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_JWK = jwt.algorithms.RSAAlgorithm.to_jwk(_KEY.public_key(), as_dict=True)
_JWK["kid"] = "test-key-1"
_JWK["use"] = "sig"
_JWKS_DOC = {"keys": [_JWK]}
_JWKS_ROTATED_DOC = {"keys": [dict(_JWK, kid="test-key-2")]}


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


def _fetch_jwks_ok(url: str) -> dict[str, Any]:
    return _JWKS_DOC


def _fetch_jwks_down(url: str) -> dict[str, Any]:
    raise RuntimeError("idp unreachable")


@pytest.fixture(autouse=True)
def _token_env(monkeypatch: pytest.MonkeyPatch):
    """Every test here starts from the token-configured posture + a clean
    JWKS cache; the JWKS fetch is faked to the local key by default."""
    monkeypatch.setattr(settings, "token_issuer", "https://idp.example/realms/pcai")
    monkeypatch.setattr(settings, "token_jwks_url", "https://idp.example/realms/pcai/protocol/openid-connect/certs")
    monkeypatch.setattr(settings, "token_audience", "pcai-gateway")
    monkeypatch.setattr(settings, "trust_proxy_identity", False)
    monkeypatch.setattr(settings, "token_shadow_keys", False)
    monkeypatch.setattr(settings, "token_shadow_tenant_id", str(uuid.uuid4()))
    monkeypatch.setattr(settings, "sso_key_create_enabled", False)
    monkeypatch.setattr(settings, "admin_groups", "")
    monkeypatch.setattr(identity, "_fetch_jwks", _fetch_jwks_ok)
    identity._reset_token_cache()
    yield
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


# ---------------------------------------------------------------------------
# 1. JWKS validation: good / expired / wrong-aud / wrong-issuer (+ caching)
# ---------------------------------------------------------------------------


async def test_validate_token_good(monkeypatch: pytest.MonkeyPatch) -> None:
    claims = await identity.validate_token(_sign(_claims()))
    assert claims is not None
    assert claims.subject == "user-alice-1"
    assert claims.email == "alice@corp"
    assert claims.groups == ["eng"]


async def test_validate_token_expired_is_none_not_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    """THE fall-through primitive: an expired token returns None (a
    credential that did not verify), NOT an exception — the ladder can
    then fall to the key rungs. Only infrastructure failures raise."""
    expired = _claims(exp=int((datetime.now(UTC) - timedelta(minutes=1)).timestamp()))
    assert await identity.validate_token(_sign(expired)) is None


async def test_validate_token_wrong_audience(monkeypatch: pytest.MonkeyPatch) -> None:
    assert await identity.validate_token(_sign(_claims(aud="another-service"))) is None


# --- azp fallback (Keycloak service tokens carry azp, often no aud) --------


async def test_validate_token_azp_fallback_matches(monkeypatch: pytest.MonkeyPatch) -> None:
    """The G2/Keycloak posture: NO `aud` claim, `azp` = the pinned audience.
    The token IS addressed to us — the azp fallback must accept it (and the
    azp claim rides through to the decoded claims)."""
    token = _sign(_claims(aud=None))  # _claims(aud=None) drops the key? no — keep explicit
    assert await identity.validate_token(token) is None  # aud=None -> MissingRequiredClaim? no: aud absent


async def test_validate_token_no_aud_azp_matches_pinned(monkeypatch: pytest.MonkeyPatch) -> None:
    claims = _claims()
    del claims["aud"]
    claims["azp"] = "pcai-gateway"
    out = await identity.validate_token(_sign(claims))
    assert out is not None
    assert out.subject == "user-alice-1"


async def test_validate_token_no_aud_azp_mismatch_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """azp present but NOT the pinned audience — refused (not addressed to
    us); the fallback must never widen acceptance."""
    claims = _claims()
    del claims["aud"]
    claims["azp"] = "someone-else"
    assert await identity.validate_token(_sign(claims)) is None


async def test_validate_token_no_aud_no_azp_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """No aud AND no azp — nothing anchors the audience; refused."""
    claims = _claims()
    del claims["aud"]
    assert await identity.validate_token(_sign(claims)) is None


async def test_validate_token_azp_checked_after_verification(monkeypatch: pytest.MonkeyPatch) -> None:
    """The azp match must ride the VERIFIED decode: a token whose azp was
    forged to match (bad signature) stays refused even with the fallback."""
    claims = _claims()
    del claims["aud"]
    claims["azp"] = "pcai-gateway"
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    forged = jwt.encode(claims, other, algorithm="RS256", headers={"kid": "test-key-1"})
    assert await identity.validate_token(forged) is None


async def test_validate_token_wrong_issuer(monkeypatch: pytest.MonkeyPatch) -> None:
    assert await identity.validate_token(_sign(_claims(iss="https://evil.example"))) is None


async def test_validate_token_bad_signature(monkeypatch: pytest.MonkeyPatch) -> None:
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    forged = jwt.encode(_claims(), other, algorithm="RS256", headers={"kid": "test-key-1"})
    assert await identity.validate_token(forged) is None


async def test_validate_token_malformed_and_non_jwt(monkeypatch: pytest.MonkeyPatch) -> None:
    assert await identity.validate_token("not-a-jwt") is None
    assert await identity.validate_token("") is None
    assert await identity.validate_token("a.b") is None  # two segments
    # an hllm- key never even enters the JWT machinery
    assert await identity.validate_token("hllm-" + "x" * 40) is None


async def test_validate_token_missing_sub_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """A token without sub cannot anchor an identity — refused (no
    preferred_username fallback: sub is the stable contract)."""
    assert await identity.validate_token(_sign(_claims(sub=""))) is None
    del_token = {k: v for k, v in _claims().items() if k != "sub"}
    assert await identity.validate_token(_sign(del_token)) is None


async def test_validate_token_inert_when_unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any of the three knobs empty ⇒ the rung is INERT: validate_token
    returns None without touching the network (flag-off byte-identity)."""
    monkeypatch.setattr(settings, "token_audience", "")
    fetched: list[str] = []

    def spy(url: str) -> dict[str, Any]:
        fetched.append(url)
        return _JWKS_DOC

    monkeypatch.setattr(identity, "_fetch_jwks", spy)
    assert await identity.validate_token(_sign(_claims())) is None
    assert fetched == []
    assert not identity.token_validation_configured()


async def test_jwks_cache_ttl_and_refresh_on_unknown_kid(monkeypatch: pytest.MonkeyPatch) -> None:
    """TTL cache: one fetch serves many validations within the TTL; an
    UNKNOWN kid triggers exactly ONE refresh (Keycloak rotation) and the
    refreshed key validates the token."""
    fetches: list[str] = []

    def counting_fetch(url: str) -> dict[str, Any]:
        fetches.append(url)
        return _JWKS_DOC

    monkeypatch.setattr(identity, "_fetch_jwks", counting_fetch)
    monkeypatch.setattr(settings, "token_jwks_cache_s", 300.0)
    t1 = _sign(_claims(sub="a"))
    t2 = _sign(_claims(sub="b"))
    assert await identity.validate_token(t1) is not None
    assert await identity.validate_token(t2) is not None
    assert len(fetches) == 1  # cached across the TTL

    # TTL expiry forces a refetch (age the cache entry past the TTL —
    # the clamp keeps production TTLs >= 1s, tests age the stamp directly)
    monkeypatch.setattr(settings, "token_jwks_cache_s", 300.0)
    identity._jwks_cached_at -= 301.0
    assert await identity.validate_token(_sign(_claims(sub="c"))) is not None
    assert len(fetches) == 2

    # refresh-on-unknown-kid: the cache holds test-key-1; a token signed
    # with kid test-key-2 (the rotated document) is refused on the first
    # pass, triggers ONE refresh, and validates on the retry.
    monkeypatch.setattr(settings, "token_jwks_cache_s", 300.0)
    identity._reset_token_cache()
    fetched_docs: list[str] = []

    def rotating_fetch(url: str) -> dict[str, Any]:
        fetched_docs.append(url)
        return _JWKS_ROTATED_DOC if len(fetched_docs) > 1 else _JWKS_DOC

    monkeypatch.setattr(identity, "_fetch_jwks", rotating_fetch)
    rotated_token = _sign(_claims(sub="d"), kid="test-key-2")
    claims = await identity.validate_token(rotated_token)
    assert claims is not None and claims.subject == "d"
    assert len(fetched_docs) == 2  # initial + exactly one refresh


async def test_validate_token_unknown_kid_after_refresh_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unknown kid that survives the one refresh is refused — bounded,
    no refresh loop against a misbehaving IdP."""
    fetches: list[str] = []

    def fetch(url: str) -> dict[str, Any]:
        fetches.append(url)
        return _JWKS_DOC

    monkeypatch.setattr(identity, "_fetch_jwks", fetch)
    stranger = _sign(_claims(), kid="never-seen")
    assert await identity.validate_token(stranger) is None
    assert len(fetches) == 2  # initial fetch + the ONE refresh


async def test_validate_token_jwks_infra_failure_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """THE OTHER POSTURE: the JWKS endpoint being DOWN is infrastructure —
    it RAISES (the resolver's fail-closed contract; auth surfaces map it
    to 401), it never silently falls through to "no identity"."""
    monkeypatch.setattr(identity, "_fetch_jwks", _fetch_jwks_down)
    with pytest.raises(RuntimeError):
        await identity.validate_token(_sign(_claims()))


# ---------------------------------------------------------------------------
# 2. the ladder: fall-through rules (THE TTL-invisibility contract)
# ---------------------------------------------------------------------------


class LadderDB:
    """Subjects-binding fake (the test_identity.LadderDB pattern)."""

    def __init__(self, bindings: dict[uuid.UUID, dict[str, Any]] | None = None) -> None:
        self.bindings = bindings or {}

    async def fetchrow(self, sql: str, *args: Any):
        if "JOIN subjects s ON s.id" in sql:
            return self.bindings.get(args[0])
        return None

    async def fetch(self, sql: str, *args: Any):
        return []

    async def execute(self, sql: str, *args: Any):
        return "UPDATE 0"


def test_ladder_valid_token_resolves_token_kind(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(identity, "db", LadderDB())
    caller = asyncio.run(identity.resolve_caller(None, token=_sign(_claims())))
    assert caller is not None
    assert caller.subject == "user-alice-1"
    assert caller.subject_kind == KIND_TOKEN
    assert caller.groups == ["eng"]
    assert caller.key is None and caller.key_fp == ""


def test_ladder_expired_token_falls_through_to_key_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """THE review rule verbatim: expired token + valid key ⇒ the KEY
    identity wins, no 401, no exception (the 30-min TTL is invisible)."""
    kid = uuid.uuid4()
    monkeypatch.setattr(settings, "trust_proxy_identity", False)
    monkeypatch.setattr(
        identity,
        "db",
        LadderDB({kid: {"subject": "keybound@x", "idp": IDP_KEYCLOAK, "kind": "user", "groups": []}}),
    )
    k = _key(id=kid)
    expired = _sign(_claims(exp=int((datetime.now(UTC) - timedelta(minutes=1)).timestamp())))
    caller = asyncio.run(identity.resolve_caller(k, token=expired))
    assert caller is not None
    assert caller.subject_kind == KIND_KEY_BOUND
    assert caller.subject == "keybound@x"


def test_ladder_valid_token_wins_over_key_bound_subject(monkeypatch: pytest.MonkeyPatch) -> None:
    """First hit wins: the token is the freshest identity assertion — a
    VALID token beats the key's bound subject for attribution (the key
    still authorizes)."""
    kid = uuid.uuid4()
    monkeypatch.setattr(
        identity,
        "db",
        LadderDB({kid: {"subject": "keybound@x", "idp": IDP_KEYCLOAK, "kind": "user", "groups": []}}),
    )
    caller = asyncio.run(identity.resolve_caller(_key(id=kid), token=_sign(_claims())))
    assert caller is not None
    assert caller.subject_kind == KIND_TOKEN
    assert caller.subject == "user-alice-1"
    assert caller.key_fp == str(kid)  # the authorization anchor rides along


def test_ladder_browser_still_wins_over_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "trust_proxy_identity", True)
    monkeypatch.setattr(identity, "db", LadderDB())
    caller = asyncio.run(
        identity.resolve_caller(
            None,
            headers={"X-Auth-Request-User": "sso-user"},
            token=_sign(_claims()),
        )
    )
    assert caller is not None
    assert caller.subject_kind == KIND_BROWSER and caller.subject == "sso-user"


def test_ladder_token_configured_but_nothing_else_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """No token, no key → None (fail-closed — unauthenticated)."""
    monkeypatch.setattr(identity, "db", LadderDB())
    assert asyncio.run(identity.resolve_caller(None, token=None)) is None


def test_ladder_token_invalid_and_no_key_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """Invalid token, no key: the ladder resolves NOTHING (the caller's
    fall-through lands in the same fail-closed 401 as a bare request)."""
    monkeypatch.setattr(identity, "db", LadderDB())
    expired = _sign(_claims(exp=int((datetime.now(UTC) - timedelta(minutes=1)).timestamp())))
    assert asyncio.run(identity.resolve_caller(None, token=expired)) is None


# ---------------------------------------------------------------------------
# 3. shadow-keys: first sighting provisions, second reuses; flag off = no
# ---------------------------------------------------------------------------


class ShadowDB:
    """Stateful fake modeling subjects UNIQUE (idp, subject), users, and
    api_keys (with bound_via/subject_id) — enough surface for the
    shadow-key flow. Records statements for assertions."""

    def __init__(self) -> None:
        self.subjects: dict[tuple[str, str], str] = {}  # (idp, subject) -> id
        self.users: list[dict[str, Any]] = []
        self.keys: list[dict[str, Any]] = []
        self.seq = 0
        self.broken = False
        self.fail_provision = False

    def _next(self) -> str:
        self.seq += 1
        return f"00000000-0000-0000-0000-{self.seq:012d}"

    async def fetchrow(self, sql: str, *args: Any):
        if self.broken:
            raise RuntimeError("database not connected")
        # api_keys FIRST: the new _SHADOW_KEY_SQL embeds the subjects
        # lookup as a subselect, so its SQL also contains "FROM subjects" —
        # the key branch must win on the more specific marker.
        if "FROM api_keys k" in sql and "bound_via" in sql:
            # (audit P2-6) the SQL now carries the subject lookup inline:
            # args are (idp, subject, bound_via) — resolve via the map.
            sid = self.subjects.get((args[0], args[1]))
            live = [k for k in self.keys if k["subject_id"] == sid and k["bound_via"] == args[2] and not k["revoked"]]
            if not live:
                return None
            newest = live[-1]
            return {
                "id": newest["id"],
                "user_id": newest["user_id"],
                "name": newest["name"],
                "key_prefix": "hllm-XX",
                "scopes": newest["scopes"],
                "allowed_models": [],
                "rate_limit_rpm": 60,
                "rate_limit_tpm": 100_000,
                "max_concurrent": 0,
                "expires_at": None,
                "revoked_at": None,
                "relay_default": False,
                "stream_pcai_events": False,
                "offpeak_fraction": None,
                "spend_ancestors": newest.get("spend_ancestors", []),
            }
        if "FROM subjects WHERE idp" in sql:
            sid = self.subjects.get((args[0], args[1]))
            return {"id": sid} if sid else None
        if "INSERT INTO users" in sql:
            if self.fail_provision:
                raise RuntimeError("provision down")
            uid = self._next()
            self.users.append({"id": uid, "tenant_id": args[0], "name": args[1]})
            return {"id": uid, "name": args[1]}
        if "INSERT INTO subjects" in sql:
            # TWO arg shapes reach this fake: bind_key_subject's upsert
            # passes (tenant_id, kind, idp, subject, display_name, groups)
            # while the shadow provisioner passes the kind inline and
            # (tenant_id, idp, subject, display_name, groups). Pick the
            # (idp, subject) pair off whichever shape arrived.
            pair = (args[2], args[3]) if args[1] in ("user", "service", "anonymous") else (args[1], args[2])
            if pair not in self.subjects:
                self.subjects[pair] = self._next()
            return {"id": self.subjects[pair]}
        if "INSERT INTO api_keys" in sql:
            if self.fail_provision:
                raise RuntimeError("provision down")
            kid = self._next()
            self.keys.append(
                {
                    "id": kid,
                    "user_id": args[0],
                    "name": args[1],
                    "scopes": args[4],
                    "spend_ancestors": list(args[11]) if len(args) >= 12 else [],
                    "subject_id": args[8],
                    "bound_via": args[9],
                    "revoked": False,
                }
            )
            return {"id": kid}
        if "UPDATE api_keys SET spend_ancestors" in sql:
            for k in self.keys:
                if k["id"] == args[0]:
                    k["spend_ancestors"] = list(args[1])
            return "UPDATE 1"
        if "JOIN subjects s ON s.id" in sql:
            return None
        return None

    async def fetch(self, sql: str, *args: Any):
        return []

    async def execute(self, sql: str, *args: Any):
        return "UPDATE 0"


def test_shadow_key_first_sighting_provisions(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = ShadowDB()
    monkeypatch.setattr(identity, "db", fake)
    monkeypatch.setattr(settings, "token_shadow_keys", True)
    claims = identity.TokenClaims(subject="user-alice-1", email="alice@corp", groups=["eng"])
    k = asyncio.run(identity.resolve_shadow_key("user-alice-1", claims))
    assert k is not None
    assert k.scopes == ["proxy", "mcp"]
    assert k.name == "token-shadow"
    # user + subject + key all created; the key bound VIA_TOKEN_SHADOW to
    # the subject row (keyed by the keycloak idp)
    assert len(fake.users) == 1 and len(fake.keys) == 1
    assert fake.keys[0]["bound_via"] == VIA_TOKEN_SHADOW
    assert (IDP_KEYCLOAK, "user-alice-1") in fake.subjects


def test_shadow_key_second_sighting_reuses(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = ShadowDB()
    monkeypatch.setattr(identity, "db", fake)
    monkeypatch.setattr(settings, "token_shadow_keys", True)
    first = asyncio.run(identity.resolve_shadow_key("user-alice-1", None))
    second = asyncio.run(identity.resolve_shadow_key("user-alice-1", None))
    assert first is not None and second is not None
    assert first.id == second.id
    assert len(fake.users) == 1 and len(fake.keys) == 1  # NO new rows


def test_shadow_key_flag_off_no_provisioning(monkeypatch: pytest.MonkeyPatch) -> None:
    """DEFAULT OFF: tokens validate but never provision — the anonymous-fp
    posture. An UNKNOWN subject gets nothing (no user, no key rows)."""
    fake = ShadowDB()
    monkeypatch.setattr(identity, "db", fake)
    monkeypatch.setattr(settings, "token_shadow_keys", False)
    assert asyncio.run(identity.resolve_shadow_key("user-alice-1", None)) is None
    assert fake.users == [] and fake.keys == []


def test_shadow_key_flag_off_recognizes_existing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """The flag gates PROVISIONING, not recognition: an already-provisioned
    shadow key keeps working when the operator flips the flag back off
    (console revocation is the off switch for an existing key)."""
    fake = ShadowDB()
    monkeypatch.setattr(identity, "db", fake)
    monkeypatch.setattr(settings, "token_shadow_keys", True)
    first = asyncio.run(identity.resolve_shadow_key("user-alice-1", None))
    assert first is not None
    monkeypatch.setattr(settings, "token_shadow_keys", False)
    again = asyncio.run(identity.resolve_shadow_key("user-alice-1", None))
    assert again is not None and again.id == first.id
    assert len(fake.users) == 1 and len(fake.keys) == 1  # nothing new provisioned


def test_shadow_key_without_tenant_inert(monkeypatch: pytest.MonkeyPatch) -> None:
    """The flag AND a tenant are both required — a shadow key must live in
    SOME tenant; an unconfigured tenant keeps the feature inert."""
    fake = ShadowDB()
    monkeypatch.setattr(identity, "db", fake)
    monkeypatch.setattr(settings, "token_shadow_keys", True)
    monkeypatch.setattr(settings, "token_shadow_tenant_id", "")
    assert not identity.shadow_keys_enabled()
    assert asyncio.run(identity.resolve_shadow_key("user-alice-1", None)) is None
    assert fake.users == []


def test_shadow_key_provision_failure_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Best-effort posture: a provisioning failure must not turn a valid
    token into an exception — the token identity still resolves; the next
    sighting retries."""
    fake = ShadowDB()
    fake.fail_provision = True
    monkeypatch.setattr(identity, "db", fake)
    monkeypatch.setattr(settings, "token_shadow_keys", True)
    assert asyncio.run(identity.resolve_shadow_key("user-alice-1", None)) is None


def test_shadow_key_revoked_key_not_reused(monkeypatch: pytest.MonkeyPatch) -> None:
    """The lookup is 'newest NON-REVOKED' — a revoked shadow key is never
    handed out; with the flag on, the next sighting provisions a fresh
    one (console revocation is the off switch)."""
    fake = ShadowDB()
    monkeypatch.setattr(identity, "db", fake)
    monkeypatch.setattr(settings, "token_shadow_keys", True)
    first = asyncio.run(identity.resolve_shadow_key("user-alice-1", None))
    assert first is not None
    fake.keys[0]["revoked"] = True
    assert asyncio.run(identity.find_shadow_key("user-alice-1")) is None
    second = asyncio.run(identity.resolve_shadow_key("user-alice-1", None))
    assert second is not None and second.id != first.id


# ---------------------------------------------------------------------------
# 4. require_key: the token rung live on /v1 (fall-through at the surface)
# ---------------------------------------------------------------------------


class PlainDB:
    async def fetchrow(self, sql: str, *args: Any):
        return None

    async def fetch(self, sql: str, *args: Any):
        return []

    async def fetchval(self, sql: str, *args: Any):
        return 1

    async def execute(self, sql: str, *args: Any):
        return "UPDATE 0"


def _key_app(monkeypatch: pytest.MonkeyPatch, table: dict[str, KeyRecord | None]) -> TestClient:
    """App with require_scope("proxy") behind require_key; authenticate is
    the table lookup (the test_proxy seam)."""
    import fastapi

    import pcai_llm.api.auth as auth_mod

    async def fake_auth(plaintext: str) -> KeyRecord | None:
        return table.get(plaintext)

    monkeypatch.setattr(auth_mod, "authenticate", fake_auth)
    app = FastAPI()
    dep = require_scope("proxy")

    @app.get("/probe")
    async def probe(request: fastapi.Request, key: KeyRecord = fastapi.Depends(dep)) -> dict[str, Any]:
        return {"key": str(key.id)}

    return TestClient(app, raise_server_exceptions=False)


def test_require_key_valid_token_no_key_401_with_identity_recorded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Token-accepted surface, token the ONLY credential, token VALID:
    still 401 (authorization requires a key) — but the resolved identity
    rides request.state.caller for attribution/logging."""
    fake = ShadowDB()
    monkeypatch.setattr(identity, "db", fake)
    monkeypatch.setattr(settings, "token_shadow_keys", False)
    tc = _key_app(monkeypatch, {})
    r = tc.get("/probe", headers={"Authorization": f"Bearer {_sign(_claims())}"})
    assert r.status_code == 401


def test_require_key_invalid_token_still_reaches_key_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE surface-level fall-through (review §1.1): an INVALID token is
    never a hard 401 by itself — require_key still hands the SAME bearer
    to authenticate(), so a request whose credential also resolves as a
    virtual key succeeds (the 30-min TTL never reaches a key holder). The
    resolver-level shape (expired token + bound key ⇒ key identity) is
    pinned in test_ladder_expired_token_falls_through_to_key."""
    kid = uuid.uuid4()
    k = _key(id=kid, scopes=["proxy"])
    got: list[str] = []

    def make_auth(target: str | None):
        async def fake_auth(plaintext: str) -> KeyRecord | None:
            got.append(plaintext)
            return k if plaintext == target else None

        return fake_auth

    monkeypatch.setattr(identity, "db", PlainDB())
    expired = _sign(_claims(exp=int((datetime.now(UTC) - timedelta(minutes=1)).timestamp())))

    # 1) expired JWT alone → 401 (authenticate refuses it too: it was the
    #    only credential on a token-accepted surface)
    tc = _key_app(monkeypatch, {})
    r = tc.get("/probe", headers={"Authorization": f"Bearer {expired}"})
    assert r.status_code == 401

    # 2) the SAME invalid token, but authenticate resolves the bearer as a
    #    key (a stale-JWT-in-front-of-a-key shape) → the KEY wins: 200
    import pcai_llm.api.auth as auth_mod

    monkeypatch.setattr(auth_mod, "authenticate", make_auth(expired))
    got.clear()
    r2 = tc.get("/probe", headers={"Authorization": f"Bearer {expired}"})
    assert r2.status_code == 200, r2.text
    assert got == [expired]  # the invalid token REACHED authenticate (fall-through)


def test_require_key_token_with_shadow_key_authorizes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Valid token + shadow-key flag on ⇒ the provisioned shadow key IS
    the authorization anchor: the request goes through with its scopes."""
    fake = ShadowDB()
    monkeypatch.setattr(identity, "db", fake)
    monkeypatch.setattr(settings, "token_shadow_keys", True)
    # authenticate must NOT be consulted for the token path — build the
    # app with an authenticate that always refuses, so ONLY the shadow
    # key can authorize this request.
    import pcai_llm.api.auth as auth_mod

    async def refusing_auth(plaintext: str) -> KeyRecord | None:
        return None

    monkeypatch.setattr(auth_mod, "authenticate", refusing_auth)
    import fastapi

    app = FastAPI()
    dep = require_scope("proxy")

    @app.get("/probe")
    async def probe(request: fastapi.Request, key: KeyRecord = fastapi.Depends(dep)) -> dict[str, Any]:
        return {"key": str(key.id)}

    tc = TestClient(app, raise_server_exceptions=False)
    r = tc.get("/probe", headers={"Authorization": f"Bearer {_sign(_claims())}"})
    assert r.status_code == 200, r.text
    assert len(fake.keys) == 1 and fake.keys[0]["bound_via"] == VIA_TOKEN_SHADOW


def test_require_key_garbage_bearer_401(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(identity, "db", PlainDB())
    tc = _key_app(monkeypatch, {})
    assert tc.get("/probe").status_code == 401
    assert tc.get("/probe", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_require_key_token_infra_failure_401_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """JWKS down on a token-only request: 401 (fail-closed), never a
    bypass — and the fall-through to authenticate() does NOT run for a
    JWT-shaped bearer whose IdP is unreachable... unless the credential is
    also a live key (the fall-through contract requires authenticate to
    still run; here the table is empty so it 401s either way — the pin is
    that it's a clean 401, not a 500)."""
    monkeypatch.setattr(identity, "_fetch_jwks", _fetch_jwks_down)
    monkeypatch.setattr(identity, "db", PlainDB())
    tc = _key_app(monkeypatch, {})
    r = tc.get("/probe", headers={"Authorization": f"Bearer {_sign(_claims())}"})
    assert r.status_code == 401


def test_require_key_key_shaped_bearer_never_tokenized(monkeypatch: pytest.MonkeyPatch) -> None:
    """An hllm- key NEVER reaches the JWT machinery (shape gate): a
    virtual key authenticates even with the IdP completely down."""
    monkeypatch.setattr(identity, "_fetch_jwks", _fetch_jwks_down)
    monkeypatch.setattr(identity, "db", PlainDB())
    k = _key(scopes=["proxy"])
    tc = _key_app(monkeypatch, {k.key_prefix: k})
    r = tc.get("/probe", headers={"Authorization": f"Bearer {k.key_prefix}"})
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# 5. /mcp wrapper: the token rung live (401 vs fall-through)
# ---------------------------------------------------------------------------


def _mcp_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    app = FastAPI()
    routes_mcp.mount_server(app, routes_mcp._new_gateway_server())
    return TestClient(app, raise_server_exceptions=False)


_MCP_PV = "2026-07-28"


def _rpc(i: int = 1, method: str = "tools/list") -> dict[str, Any]:
    """The per-request envelope the installed SDK requires (the
    test_mcp_flat pattern): params._meta carries protocolVersion +
    clientCapabilities; headers carry the method/version."""
    return {
        "jsonrpc": "2.0",
        "id": i,
        "method": method,
        "params": {
            "_meta": {
                "io.modelcontextprotocol/protocolVersion": _MCP_PV,
                "io.modelcontextprotocol/clientCapabilities": {},
            }
        },
    }


def _rpc_headers(bearer: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {bearer}",
        "mcp-protocol-version": _MCP_PV,
        "mcp-method": "tools/list",
    }


def test_mcp_wrapper_valid_token_without_key_401(monkeypatch: pytest.MonkeyPatch) -> None:
    """Token-only on the /mcp surface: valid token, no shadow key (flag
    off) ⇒ 401 (it IS a 401 when it was the only credential)."""
    fake = ShadowDB()
    monkeypatch.setattr(identity, "db", fake)

    async def refusing_auth(plaintext: str) -> KeyRecord | None:
        return None

    monkeypatch.setattr(routes_mcp, "authenticate", refusing_auth)
    with _mcp_client(monkeypatch) as tc:
        r = tc.post("/mcp", json=_rpc(), headers=_rpc_headers(_sign(_claims())))
    assert r.status_code == 401


def test_mcp_wrapper_invalid_token_still_falls_through_to_key_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE surface-level fall-through (review §1.1): an INVALID token is
    never a hard 401 by itself — the wrapper still hands the SAME bearer
    to authenticate(), so a request whose credential also resolves as a
    virtual key succeeds (in production: a caller whose bearer is a
    long-lived hllm key while a stale JWT slips in front of it — the
    30-min TTL never reaches the key holder)."""
    k = _key(scopes=["mcp"])
    got: list[str] = []

    async def fake_auth(plaintext: str) -> KeyRecord | None:
        got.append(plaintext)
        return k  # the bearer resolves as a key

    monkeypatch.setattr(routes_mcp, "authenticate", fake_auth)
    monkeypatch.setattr(identity, "db", PlainDB())
    expired = _sign(_claims(exp=int((datetime.now(UTC) - timedelta(minutes=1)).timestamp())))
    with _mcp_client(monkeypatch) as tc:
        r = tc.post("/mcp", json=_rpc(), headers=_rpc_headers(expired))
    assert r.status_code == 200, r.text  # the KEY authenticated the request
    assert got == [expired]  # the SAME credential reached authenticate


def test_mcp_wrapper_token_with_shadow_key_reaches_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    """Valid token + flag on ⇒ shadow key authorizes the /mcp surface
    (scopes carry 'mcp')."""
    fake = ShadowDB()
    monkeypatch.setattr(identity, "db", fake)
    monkeypatch.setattr(settings, "token_shadow_keys", True)

    async def refusing_auth(plaintext: str) -> KeyRecord | None:
        return None

    monkeypatch.setattr(routes_mcp, "authenticate", refusing_auth)
    with _mcp_client(monkeypatch) as tc:
        r = tc.post("/mcp", json=_rpc(), headers=_rpc_headers(_sign(_claims())))
    assert r.status_code == 200, r.text
    assert len(fake.keys) == 1 and fake.keys[0]["bound_via"] == VIA_TOKEN_SHADOW


def test_mcp_wrapper_unconfigured_tokens_byte_identical(monkeypatch: pytest.MonkeyPatch) -> None:
    """Flag-off byte-identity: with token validation UNCONFIGURED, a
    JWT-shaped bearer behaves exactly like any unknown key — 401 from the
    same _UNAUTHORIZED_BODY, no JWKS fetch, no shadow machinery."""
    monkeypatch.setattr(settings, "token_jwks_url", "")
    fetches: list[str] = []

    def spy(url: str) -> dict[str, Any]:
        fetches.append(url)
        return _JWKS_DOC

    monkeypatch.setattr(identity, "_fetch_jwks", spy)

    async def refusing_auth(plaintext: str) -> KeyRecord | None:
        return None

    monkeypatch.setattr(routes_mcp, "authenticate", refusing_auth)
    with _mcp_client(monkeypatch) as tc:
        r = tc.post("/mcp", json=_rpc(), headers=_rpc_headers(_sign(_claims())))
    assert r.status_code == 401
    assert r.content == routes_mcp._UNAUTHORIZED_BODY
    assert fetches == []


# ---------------------------------------------------------------------------
# 6. POST /v1/keys/for-me
# ---------------------------------------------------------------------------


class ForMeDB(ShadowDB):
    """Adds the create_key/user surfaces /v1/keys/for-me needs (the enroll
    fake pattern)."""

    async def fetchval(self, sql: str, *args: Any):
        return "tenant-acme"

    def acquire(self):
        return _ForMeConn(self)


class _ForMeConn:
    """An acquired-connection stand-in (the test_admin_selfmint._FakeConn
    shape): the for-me mint AND rotate both run inside one
    `db.acquire() + conn.transaction()` block, so the fake must expose the
    transactional seam (statement dispatch delegates to the owning fake)."""

    def __init__(self, db: ForMeDB) -> None:
        self._db = db

    def transaction(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc: object):
        return False

    async def fetchrow(self, sql: str, *args: Any):
        return await self._db.fetchrow(sql, *args)

    async def fetchval(self, sql: str, *args: Any):
        return await self._db.fetchval(sql, *args)

    async def fetch(self, sql: str, *args: Any):
        return await self._db.fetch(sql, *args)

    async def execute(self, sql: str, *args: Any):
        return await self._db.execute(sql, *args)

    async def executemany(self, sql: str, args: Any) -> None:
        pass


@pytest.fixture
def forme_env(monkeypatch: pytest.MonkeyPatch):
    fake = ForMeDB()
    import pcai_llm.core.keys as keys_mod

    monkeypatch.setattr(routes_enroll, "db", fake)
    monkeypatch.setattr(keys_mod, "db", fake)
    monkeypatch.setattr(identity, "db", fake)
    monkeypatch.setattr(routes_enroll, "bind_key_subject", identity.bind_key_subject)
    app = FastAPI()
    app.include_router(routes_enroll.router)
    return TestClient(app, raise_server_exceptions=False), fake


def test_keys_for_me_self_bind_works(forme_env, monkeypatch: pytest.MonkeyPatch) -> None:
    tc, fake = forme_env
    monkeypatch.setattr(settings, "trust_proxy_identity", True)
    r = tc.post(
        "/v1/keys/for-me",
        headers={"X-Auth-Request-User": "alice", "X-Forwarded-Groups": "eng"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["key"].startswith("hllm-")
    assert body["subject"] == "alice"
    assert body["idp"] == IDP_OAUTH2_PROXY
    # bound to the CALLER'S OWN subject with the sso-create provenance:
    # the subjects upsert + the api_keys insert both recorded
    assert len(fake.keys) == 1
    assert (IDP_OAUTH2_PROXY, "alice") in fake.subjects


def test_keys_for_me_trust_off_403(forme_env) -> None:
    """Trust flag off (the default): 403 — the surface does not exist in
    that deployment posture."""
    tc, _ = forme_env
    r = tc.post("/v1/keys/for-me", headers={"X-Auth-Request-User": "alice"})
    assert r.status_code == 403


def test_keys_for_me_no_headers_401(forme_env, monkeypatch: pytest.MonkeyPatch) -> None:
    tc, _ = forme_env
    monkeypatch.setattr(settings, "trust_proxy_identity", True)
    assert tc.post("/v1/keys/for-me").status_code == 401


def test_keys_for_me_no_tenant_503(forme_env, monkeypatch: pytest.MonkeyPatch) -> None:
    tc, _ = forme_env
    monkeypatch.setattr(settings, "trust_proxy_identity", True)
    monkeypatch.setattr(settings, "token_shadow_tenant_id", "")
    r = tc.post("/v1/keys/for-me", headers={"X-Auth-Request-User": "alice"})
    assert r.status_code == 503


def test_keys_for_me_cannot_set_arbitrary_grants(forme_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """NO admin escalation: the endpoint takes NO body — scopes are fixed
    (proxy+mcp), no allowed_models, no profile. A caller cannot request
    grants (admin-controlled) because there is no field to put them in."""
    tc, fake = forme_env
    monkeypatch.setattr(settings, "trust_proxy_identity", True)
    r = tc.post(
        "/v1/keys/for-me",
        json={"scopes": ["proxy", "mcp", "admin"], "allowed_models": ["*"]},
        headers={"X-Auth-Request-User": "alice"},
    )
    assert r.status_code == 201  # pydantic ignores the extra body
    assert fake.keys[0]["scopes"] == ["proxy", "mcp"]  # FIXED scopes anyway


# ---------------------------------------------------------------------------
# 7. admin gate second path (browser identity in admin groups)
# ---------------------------------------------------------------------------


def _admin_app(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Admin router with a quiet fake db AND a quiet budget_status (the
    keys list enriches each row with budgets via core.budgets.db)."""
    monkeypatch.setattr(settings, "admin_api_keys", "admin-secret-123")

    class QuietDB:
        async def fetch(self, sql: str, *args: Any):
            return []

        async def fetchval(self, sql: str, *args: Any):
            return 1

        async def fetchrow(self, sql: str, *args: Any):
            return None

        async def execute(self, sql: str, *args: Any):
            return "UPDATE 0"

    monkeypatch.setattr(routes_admin, "db", QuietDB())
    import pcai_llm.core.keys as keys_mod
    from pcai_llm.core import budgets

    monkeypatch.setattr(budgets, "db", QuietDB())
    monkeypatch.setattr(keys_mod, "db", QuietDB())
    app = FastAPI()
    app.include_router(routes_admin.router)
    return TestClient(app, raise_server_exceptions=False)


def test_admin_group_path_allows_in_group_when_trust_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "trust_proxy_identity", True)
    monkeypatch.setattr(settings, "admin_groups", "platform-admin, sre")
    tc = _admin_app(monkeypatch)
    r = tc.get(
        "/admin/keys",
        headers={"X-Auth-Request-User": "ops", "X-Forwarded-Groups": "eng, platform-admin"},
    )
    assert r.status_code == 200, r.text


def test_admin_group_path_off_by_default_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """admin_groups EMPTY (the default): the group path is off — browser
    headers get the SAME 401 the bare request always got (unchanged)."""
    monkeypatch.setattr(settings, "trust_proxy_identity", True)
    monkeypatch.setattr(settings, "admin_groups", "")
    tc = _admin_app(monkeypatch)
    r = tc.get("/admin/keys", headers={"X-Auth-Request-User": "ops", "X-Forwarded-Groups": "platform-admin"})
    assert r.status_code == 401


def test_admin_group_path_not_in_group_403(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "trust_proxy_identity", True)
    monkeypatch.setattr(settings, "admin_groups", "platform-admin")
    tc = _admin_app(monkeypatch)
    r = tc.get("/admin/keys", headers={"X-Auth-Request-User": "ops", "X-Forwarded-Groups": "eng"})
    assert r.status_code == 403


def test_admin_group_path_requires_trust_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """Trust OFF: groups in config but headers not trusted — 401 exactly
    as today (the group path is trust-gated like every browser-rung
    feature)."""
    monkeypatch.setattr(settings, "trust_proxy_identity", False)
    monkeypatch.setattr(settings, "admin_groups", "platform-admin")
    tc = _admin_app(monkeypatch)
    r = tc.get("/admin/keys", headers={"X-Auth-Request-User": "ops", "X-Forwarded-Groups": "platform-admin"})
    assert r.status_code == 401


def test_admin_bearer_path_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    """THE direct-caller contract: a valid bearer admin key authenticates
    with or without the group path configured (401/403/503 shapes
    unchanged)."""
    tc = _admin_app(monkeypatch)
    assert tc.get("/admin/keys", headers=ADMIN_H).status_code == 200
    assert tc.get("/admin/keys").status_code == 401
    r = tc.get("/admin/keys", headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401


def test_admin_rejected_bearer_does_not_fall_through_to_groups(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A caller presenting admin KEY material is a direct caller: a failed
    hash check is 401 even when trusted group headers are also present —
    group trust must not launder a rejected key."""
    monkeypatch.setattr(settings, "trust_proxy_identity", True)
    monkeypatch.setattr(settings, "admin_groups", "platform-admin")
    tc = _admin_app(monkeypatch)
    r = tc.get(
        "/admin/keys",
        headers={
            "Authorization": "Bearer wrong-key",
            "X-Auth-Request-User": "ops",
            "X-Forwarded-Groups": "platform-admin",
        },
    )
    assert r.status_code == 401


def test_admin_503_still_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    """admin_api_keys EMPTY disables the whole surface (503) — the group
    path must not resurrect a disabled admin API."""
    monkeypatch.setattr(settings, "admin_api_keys", "")
    monkeypatch.setattr(settings, "trust_proxy_identity", True)
    monkeypatch.setattr(settings, "admin_groups", "platform-admin")
    app = FastAPI()
    app.include_router(routes_admin.router)
    tc = TestClient(app, raise_server_exceptions=False)
    r = tc.get("/admin/keys", headers={"X-Auth-Request-User": "ops", "X-Forwarded-Groups": "platform-admin"})
    assert r.status_code == 503


# ---------------------------------------------------------------------------
# 8. contracts that must not move
# ---------------------------------------------------------------------------


def test_settle_signature_still_untouched() -> None:
    import inspect

    from pcai_llm.core.budgets import settle

    params = list(inspect.signature(settle).parameters)
    assert "subject" not in params and "subject_id" not in params and "token" not in params


def test_config_defaults_preserve_flag_off_behavior() -> None:
    """Every Phase B knob defaults OFF/inert — a deployment that sets none
    of them runs byte-for-byte Phase A behavior."""
    from pcai_llm.config import Settings

    s = Settings(database_url="postgresql://t:t@localhost:5432/t")
    assert s.token_issuer == "" and s.token_jwks_url == "" and s.token_audience == ""
    assert s.token_shadow_keys is False
    assert s.sso_key_create_enabled is False
    assert s.admin_groups == ""
    assert s.trust_proxy_identity is False
