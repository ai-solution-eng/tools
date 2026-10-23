"""Caller identity resolution — Phase A+B of the fleet identity architecture.

Decision contract: ``scratch_workspace/feature-ideas-2026-09/DECISIONS.md``
("OAuth REVISION (final)", binding). Implementation review:
``reviews/01-gateway-impl.md`` §1.1–1.3 — this module is the fleet's identity
keystone for the gateway: ONE pure resolver produces ONE ``Caller`` per
request, and BOTH entry surfaces (the FastAPI dependency, ``api/auth.py
require_key``, and the pure-ASGI ``/mcp`` wrapper, ``routes_mcp
._McpAuthASGI``) consume it — the wrapper cannot use FastAPI deps, so the
resolver must live outside them (verified: the wrapper is a raw ASGI class,
routes_mcp.py).

THE LADDER (per request, first hit wins):

1. **Browser headers** (``X-Auth-Request-User`` / ``X-Email``, groups from
   ``X-Forwarded-Groups``) — trusted ONLY when
   ``PCAI_LLM_TRUST_PROXY_IDENTITY=true`` (settings.trust_proxy_identity).
   Exactly MultimodalRAG's ``RAG_TRUST_PROXY_IDENTITY`` rule (api_server.py
   ``_request_identity``) — the fleet convention this module mirrors,
   including the fall-through to the key rungs when the flag is off.
2. **Keycloak token** (``Authorization: Bearer eyJ…``, Phase B) — validated
   via the JWKS endpoint (PyJWT, issuer+audience pinned, TTL-cached keys,
   refresh-once on an unknown kid). Extracts sub/email/groups → subject.
   THE FALL-THROUGH RULE (review §1.1; what makes the 30-minute token TTL
   invisible to headless users): an INVALID or EXPIRED token is NOT a hard
   401 when a ladder-lower credential (a virtual key) exists on the
   request — the key identity wins. It IS a 401 when the token was the
   only credential presented on a token-ACCEPTING surface. NEVER refreshed,
   NEVER stored; the gateway is not an OAuth client (DECISIONS, binding).
3. **Identity-bound virtual key** (``Bearer hllm-…``, already authenticated
   by the caller via core.keys.authenticate) — when ``api_keys.subject_id``
   is set, the subject is resolved by FK join to ``subjects``.
4. **Anonymous key-fp** — an unbound key stays pseudonymous: subject=None,
   identity = the key fingerprint (the stable fp is the key_id;
   ``key_prefix`` is display-only). Enforcement is today's behavior exactly
   (NULL subject_id ⇒ unchanged enforcement — the Phase A back-compat
   contract).

Token identity (rung 2, valid token) does NOT authorize by itself: a valid
token with no matching key yields an identity for attribution/logging, but
authorization continues to require a virtual key — which the OPTIONAL
shadow-key layer (``PCAI_LLM_TOKEN_SHADOW_KEYS``, default off) lazily
provisions on first sighting (bound_via='token-shadow') so the ledger keeps
its key_id anchor.

DEPLOYMENT REQUIREMENT (stated here because no code can check it): the
browser-header rung is client-spoofable at the pod. It is safe ONLY where an
oauth2-proxy front door OVERWRITES the identity headers on every request AND
an Istio AuthorizationPolicy (pcai-helm-lib ``_authorizationpolicy.tpl``, off
by default) restricts reachability so unauthenticated callers cannot bypass
the proxy and forge headers. The flag alone is NOT the control — flipping
``PCAI_LLM_TRUST_PROXY_IDENTITY`` without proxy + AuthorizationPolicy in
front lets any caller assert any identity. Default OFF.

FAIL-CLOSED POSTURE (the two jobs, two postures — both deliberate):

- ``resolve_caller`` (auth-critical): resolver exceptions PROPAGATE. An
  unavailable identity ledger is a refusal condition, never a bypass — the
  exact ``_McpAuthASGI`` DB-failure posture (routes_mcp turns any exception
  into 401; never default-open).
- ``subject_for_key`` (the X-MCP-Caller attribution stamp): DEGRADE-NOT-FAIL
  — the stamp is enrichment on a call that was already fully authorized, so
  a failed lookup logs and returns None and the stamp falls back to the key
  fingerprint (the same posture as ``key_override_headers``).
- ``validate_token`` (the JWT verifier): malformed/expired/foreign tokens
  are NOT exceptions — they return None so the ladder can fall through
  (a bearer JWT that fails validation must not kill a request that also
  carries a valid virtual key). Infrastructure failures (JWKS endpoint
  down, DB errors) DO propagate — the caller decides (the resolver's
  fail-closed posture).

Phase B scope: token rung live, shadow-keys, /v1/keys/for-me, admin header
trust (review §1.6). Phase C (fleet-side mcp_auth v2) is out of scope here.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from ..db import db
from .keys import KeyRecord, generate_key

log = logging.getLogger(__name__)

# subject_kind values (the review §1.1 vocabulary; 'token' is live Phase B)
KIND_BROWSER = "browser"
KIND_TOKEN = "token"
KIND_KEY_BOUND = "key-bound"
KIND_KEY_ANON = "key-anon"

# bound_via values written by issuance flows (server-stamped, never
# client-writable).
VIA_ENROLLMENT = "enrollment"
VIA_ADMIN_CONSOLE = "admin-console"
VIA_SSO_CREATE = "sso-create"
VIA_TOKEN_SHADOW = "token-shadow"

# idp values written by issuance flows (the subjects.idp vocabulary).
IDP_SELF_ASSERTED = "self-asserted"
IDP_OAUTH2_PROXY = "oauth2-proxy"
IDP_ADMIN_ASSERTED = "admin-asserted"
IDP_KEYCLOAK = "keycloak"

# oauth2-proxy identity headers (the MultimodalRAG convention): the USER
# claim first, email fallback; groups ride X-Forwarded-Groups (comma-
# separated). Lookups are CASE-INSENSITIVE: Starlette's `Headers` is, but a
# plain dict (tests, or `dict(request.headers)` which lowercases) is not —
# the helper accepts both shapes.
#
# 2026-10-06 (SSO self-mint §Z1 additions, verified live against the
# platform oauth2-proxy pod config — it does NOT inject X-Forwarded-Groups):
# - SUBJECT: X-Auth-Request-Preferred-Username (the OIDC preferred_username
#   claim shape — the admin console's subject convention, MM RAG's
#   RAG_OIDC_IDENTITY_CLAIM=preferred_username) is preferred when present;
#   X-Auth-Request-User and X-Email stay as fallbacks in order. All three
#   assert the SAME person behind the proxy chain; the preferred form just
#   matches the subject string the console/ledger already anchors on.
# - GROUPS: X-Auth-Request-Groups is the FALLBACK after X-Forwarded-Groups
#   (per-header precedence, additive) — behind the platform chain the
#   browser/admin group path would otherwise silently see no groups.
_BROWSER_USER_HEADERS = (
    "X-Auth-Request-Preferred-Username",
    "X-Auth-Request-User",
    "X-Email",
)
_BROWSER_GROUPS_HEADERS = ("X-Forwarded-Groups", "X-Auth-Request-Groups")

# X-MCP-Caller: attribution-never-authorization. The gateway stamps it on
# every upstream fleet MCP call; upstreams may OPTIONALLY record it in their
# audit chain and must NEVER authorize off it (DECISIONS "OAuth REVISION",
# point 5; review §1.4). Reserved name: tenants cannot override it via
# mcp_server_creds (400 at the write path, overwritten at the merge path —
# belt and braces against the verified last-wins spoofing vector).
MCP_CALLER_HEADER = "X-MCP-Caller"
# Hop legibility: the suffix marks the value as GATEWAY-ASSERTED (vs anything
# a client once claimed), so an upstream log line reads "who the gateway says
# this is".
MCP_CALLER_HOP_SUFFIX = "@gateway"
# Relay-attribution companions (SQLhandler identity.py relay rung 1): the
# bare SUBJECT bound to the presented key + the resolution rung's kind.
# Stamped ONLY when a real subject resolved — an unbound (key-fp) key
# advertises no subject, so a stale/tenant-supplied value can never upgrade
# a pseudonymous call downstream. The class value stays in the gateway's own
# subject_kind vocabulary: SQLhandler's _KIND_CLASS_MAP consumes exactly
# these kinds ('browser' | 'token' | 'key-bound' | 'key-anon') and maps them
# onto ITS bounded class labels — remapping here would just re-invent its
# `user`-on-unknown default and hide a vocabulary drift.
MCP_CALLER_SUBJECT_HEADER = "X-MCP-Caller-Subject"
MCP_CALLER_CLASS_HEADER = "X-MCP-Caller-Class"
_MCP_CALLER_MAX_LEN = 200
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")


@dataclass(frozen=True)
class Caller:
    """The resolved identity of one request's caller.

    subject      — the stable identity string, or None for the anonymous
                   key-fp rung (pseudonymous by design: legacy keys keep
                   today's exact enforcement with a NULL binding).
    subject_kind — 'browser' | 'token' | 'key-bound' | 'key-anon'.
    groups       — IdP groups (browser + token rungs; empty otherwise).
    key          — the authenticated KeyRecord when the caller presented a
                   virtual key, else None (browser/token-only caller).
    key_fp       — the stable key fingerprint (the key_id, '' when keyless).
    """

    subject: str | None
    subject_kind: str
    groups: list[str] = field(default_factory=list)
    key: KeyRecord | None = None
    key_fp: str = ""


def key_fingerprint(key: KeyRecord | None) -> str:
    """The stable pseudonymous identity of a key: its id (UUID string).

    ``key_prefix`` is display-only (first 10 chars of a shown-once secret);
    the id is what the ledger and every join anchor on, so it is the fp."""
    return str(key.id) if key is not None else ""


def browser_identity_from_headers(headers: Mapping[str, str]) -> tuple[str, list[str]] | None:
    """(subject, groups) from oauth2-proxy identity headers, or None.

    PURE header read — no trust decision here. Callers gate on
    ``settings.trust_proxy_identity`` (and the deployment AuthorizationPolicy
    requirement documented at module level) BEFORE consulting this; tests and
    the enrollment redemption flow use it directly.

    Lookup is CASE-INSENSITIVE (lowercased keys first, exact-case fallback):
    `dict(request.headers)` lowercases every name (ASGI gives raw bytes
    lowercased), while Starlette's Headers object and hand-built test dicts
    keep canonical case.

    Subject precedence (first present wins): X-Auth-Request-Preferred-
    Username → X-Auth-Request-User → X-Email. Groups precedence (per-header
    fallback, additive): X-Forwarded-Groups → X-Auth-Request-Groups — the
    platform oauth2-proxy injects the latter, not the former, so the
    fallback is what keeps the browser/admin group rung alive behind the
    chain; when X-Forwarded-Groups IS present the behavior is byte-for-byte
    unchanged.
    """

    def get(name: str) -> str:
        value = headers.get(name.lower())
        if value is None:
            value = headers.get(name)
        return value or ""

    subject = ""
    for name in _BROWSER_USER_HEADERS:
        value = get(name).strip()
        if value:
            subject = value
            break
    if not subject:
        return None
    groups: list[str] = []
    for header in _BROWSER_GROUPS_HEADERS:
        raw_groups = get(header)
        if raw_groups.strip():
            groups = [g.strip() for g in raw_groups.split(",") if g.strip()]
            break
    return subject, groups


def sanitize_caller_value(value: str) -> str:
    """Make a string safe as an outbound header VALUE: strip CR/LF and every
    other C0 control plus DEL (header-injection hygiene), trim, and cap at
    200 chars. An input that sanitizes to '' stays '' — callers skip the
    stamp rather than emit an empty header."""
    cleaned = _CONTROL_CHARS_RE.sub("", value).strip()
    return cleaned[:_MCP_CALLER_MAX_LEN]


def normalize_subject(value: str) -> str:
    """Normalize a caller-SUPPLIED subject string (self-asserted at
    redemption, admin-asserted at the console): trim, drop control chars,
    cap 200. The stable IdP-claim column deserves the same hygiene as any
    outbound identity value; empty input stays empty (callers refuse)."""
    return sanitize_caller_value(value)


# ---------------------------------------------------------------------------
# Rung 2 — Keycloak token validation (Phase B; review §1.1)
# ---------------------------------------------------------------------------

_JWT_SHAPE_RE = re.compile(r"^eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*$")
"""A JWT-shaped bearer (three dot-separated base64url segments; the
signature may be empty for unsecured JWS which validation then refuses).
Anything else (an hllm- key, garbage, empty) never reaches PyJWT."""

# JWKS cache: (fetched_at_monotonic, {kid: signing key}). Module-level
# BY DESIGN — one fetch per TTL per PROCESS, not per request (the review's
# TTL-cache requirement); tests reset it via _reset_token_cache().
_jwks_cache: dict[str, Any] | None = None
_jwks_cached_at: float = 0.0
# single-flight JWKS refresh (one fetch serves all waiters). LAZY per-
# loop binding (review flag: a module-level asyncio.Lock() binds to the
# first loop that CONTENDS — uncontended acquire works anywhere, so this
# only bites a second loop's contended acquire, e.g. a cross-test
# single-flight test). Production is one loop per process; tests get a
# fresh lock per loop.
_jwks_lock: asyncio.Lock | None = None
_jwks_lock_loop_id: int | None = None


def _token_config() -> tuple[str, str, str]:
    """(issuer, jwks_url, audience) as a tuple, read per call (tests flip
    settings without reloads). Empty jwks_url ⇒ the rung is inert."""
    from ..config import settings

    return settings.token_issuer, settings.token_jwks_url, settings.token_audience


def token_validation_configured() -> bool:
    """True when all three token-validation knobs are set. With any of
    them empty the token rung is INERT — bearer JWTs are just unknown
    keys (today's Phase A behavior, byte-for-byte)."""
    _, jwks_url, audience = _token_config()
    return bool(jwks_url) and bool(audience)


def _reset_token_cache() -> None:
    """Test hook: drop the JWKS cache and the refresh-once stamp."""
    global _jwks_cache, _jwks_cached_at
    _jwks_cache = None
    _jwks_cached_at = 0.0


class TokenClaims:
    """The validated token's identity claims (sub/email/groups).

    A frozen snapshot: validation either produces this or None — there is
    no partially-valid token state, and nothing here is persisted
    (NEVER-store rule; the object lives for one request).
    """

    __slots__ = ("email", "groups", "subject")

    def __init__(self, subject: str, email: str, groups: list[str]) -> None:
        self.subject = subject
        self.email = email
        self.groups = groups

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"TokenClaims(subject={self.subject!r}, email={self.email!r}, groups={self.groups!r})"


def _fetch_jwks(jwks_url: str) -> dict[str, Any]:
    """Fetch the IdP's JWKS document (SYNC core — always call via
    asyncio.to_thread; a blocking httpx2.get on the event loop stalls the
    whole worker). Import-guarded httpx2 (the gateway already depends on
    it); a failure raises — the caller decides whether that is fatal
    (resolve path) or a fall-through (validate path). Tests monkeypatch
    this seam with sync fakes, which to_thread calls unchanged."""
    import httpx2

    resp = httpx2.get(jwks_url, timeout=5.0, follow_redirects=False)
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, dict) or "keys" not in data:
        raise ValueError("JWKS document missing 'keys'")
    return data


async def _jwks_signing_keys(jwks_url: str) -> dict[str, Any]:
    """TTL-cached {kid: PyJWK} map from the JWKS endpoint.

    TTL: settings.token_jwks_cache_s (monotonic clock). REFRESH-ONCE on an
    unknown kid is handled by the caller (validate path): a rotated-in key
    must not wait out the TTL.

    Single-flight: concurrent cache misses share ONE fetch (the lock
    holder fetches off-loop via to_thread; the rest re-check the cache
    and reuse) — previously every concurrent miss fetched serially.
    """
    global _jwks_cache, _jwks_cached_at
    import jwt

    now = time.monotonic()
    if _jwks_cache is not None and now - _jwks_cached_at < _jwks_ttl():
        return _jwks_cache
    global _jwks_lock, _jwks_lock_loop_id
    loop_id = id(asyncio.get_running_loop())
    if _jwks_lock is None or _jwks_lock_loop_id != loop_id:
        _jwks_lock = asyncio.Lock()
        _jwks_lock_loop_id = loop_id
    async with _jwks_lock:
        now = time.monotonic()
        if _jwks_cache is not None and now - _jwks_cached_at < _jwks_ttl():
            return _jwks_cache
        doc = await asyncio.to_thread(_fetch_jwks, jwks_url)
    keys: dict[str, Any] = {}
    for jwk in doc["keys"]:
        if not isinstance(jwk, dict):
            continue
        # signing keys only: 'sig' or absent use; RSA/EC families PyJWT
        # verifies from a JWK directly.
        if jwk.get("use", "sig") != "sig":
            continue
        kid = jwk.get("kid")
        if not kid:
            continue
        try:
            keys[str(kid)] = jwt.PyJWK.from_dict(jwk)
        except Exception:  # unsupported key type — skip, never fail the map
            log.debug("identity: skipping unsupported JWKS key (alg=%s)", jwk.get("alg"))
    if not keys:
        raise ValueError("JWKS document contains no usable signing keys")
    _jwks_cache = keys
    _jwks_cached_at = now
    return keys


def _jwks_ttl() -> float:
    from ..config import settings

    return max(1.0, float(settings.token_jwks_cache_s))


def _validate_token_authorized(claims_out: dict[str, Any], token: str, kid: str | None) -> None:
    """The PyJWT verify call, split out so the unknown-kid refresh path is
    unit-testable without network. Raises jwt.PyJWTError on any failure.

    AZP FALLBACK (2026-09-23, G2/Keycloak): Keycloak service tokens carry
    `azp` (authorized party) and often NO `aud` claim at all — PyJWT then
    raises MissingRequiredClaimError('aud') even when the pinned audience
    is perfectly correct as an azp. When the pinned audience is absent
    from the token's claims but `azp` matches it, the token IS addressed
    to us — retry the decode un-pinned (the audience check passes as
    None; everything else — signature, issuer, exp — is unchanged), then
    enforce the azp match explicitly. A mismatching azp still refuses.
    """
    import jwt

    issuer, _, audience = _token_config()
    cache = _jwks_cache or {}
    if kid is None or kid not in cache:
        # not a validation failure to retry — a key-selection failure
        # (unknown kid after the bounded refresh, or no kid at all).
        raise jwt.InvalidTokenError(f"no JWKS signing key for kid={kid!r}")
    key = cache[kid]
    # runtime PyJWT accepts the plain dict here (the installed version's
    # Options TypedDict is structurally the same shape)
    base_options: dict[str, list[str]] = {"require": ["exp", "iat"]}
    decode_kwargs = {
        "key": key.key,
        "algorithms": [key.algorithm_name],
        "issuer": issuer or None,
        "leeway": _token_leeway(),
    }
    try:
        claims_out.update(
            jwt.decode(token, audience=audience or None, options=base_options, **decode_kwargs)  # type: ignore[arg-type]
        )
        return
    except jwt.MissingRequiredClaimError as exc:
        if not audience or exc.claim != "aud":
            raise
        # aud-missing only: the Keycloak azp posture. Check the unverified
        # azp FIRST (cheap; a mismatch is a refusal without the re-decode)
        # — but the SECURITY decision rides the re-decode below, which
        # still verifies signature/issuer/exp under the same key.
        unverified = jwt.decode(token, options={"verify_signature": False})
        if unverified.get("azp") != audience:
            # not addressed to us (or no azp) — the original error stands
            raise
        claims_out.update(
            jwt.decode(token, audience=None, options=base_options, **decode_kwargs)  # type: ignore[arg-type]
        )
        # explicit azp enforcement (the aud check that just got skipped)
        if claims_out.get("azp") != audience:
            raise jwt.InvalidAudienceError(f"Invalid audience: azp {claims_out.get('azp')!r} != {audience!r}")


def _token_leeway() -> float:
    from ..config import settings

    return max(0.0, float(settings.token_leeway_s))


async def validate_token(token: str, *, _refreshed: bool = False) -> TokenClaims | None:
    """Validate one JWT against the configured IdP → TokenClaims, or None.

    None (NOT an exception) for every CRYPTOLOGICALLY-meaningful failure:
    malformed shape, unknown kid after one refresh, bad signature, expired,
    wrong issuer/audience, missing required claims. THE FALL-THROUGH
    CONTRACT: the ladder treats None as "this credential did not verify"
    and moves to the next rung — an expired token beside a valid virtual
    key must yield the KEY identity, not a 401 (review §1.1; DECISIONS:
    the 30-min TTL stays invisible to headless users).

    INFRASTRUCTURE failures (JWKS fetch died, malformed JWKS doc) RAISE —
    the resolver's fail-closed posture: an unavailable IdP is a refusal
    condition, never a silent open door (a raise is turned into 401 by the
    auth surfaces exactly like a DB failure).

    ``_refreshed`` is the internal refresh-once guard: an unknown kid
    forces ONE cache refresh (Keycloak rotated keys in); a still-unknown
    kid on the retry refuses (bounded — no refresh loop against a
    misbehaving IdP).

    PINNED, per the review gotcha: issuer + audience + algorithms-from-
    JWKS. Never 'none', never HS-mode confusion — keys come only from the
    configured JWKS endpoint. Tokens are NEVER refreshed, NEVER stored.
    """
    _, jwks_url, _audience = _token_config()
    if not token_validation_configured():
        return None
    if not _JWT_SHAPE_RE.match(token):
        return None
    import jwt

    # kid peek (unverified header read — the kid only SELECTS a key; every
    # security decision happens inside jwt.decode below).
    try:
        kid = jwt.get_unverified_header(token).get("kid")
    except jwt.PyJWTError:
        return None
    try:
        keys = await _jwks_signing_keys(jwks_url)
    except Exception:
        log.exception("identity: JWKS fetch failed for %s — refusing token", jwks_url)
        raise
    if kid is not None and kid not in keys and not _refreshed:
        # refresh-once on unknown kid, then re-validate (bounded retry)
        _reset_token_cache()
        return await validate_token(token, _refreshed=True)
    decoded: dict[str, Any] = {}
    try:
        _validate_token_authorized(decoded, token, str(kid) if kid is not None else None)
    except jwt.PyJWTError as exc:
        log.info("identity: token rejected (%s: %s)", type(exc).__name__, exc)
        return None
    sub = str(decoded.get("sub") or "").strip()
    if not sub:
        # a token without a subject cannot anchor an identity — refuse (a
        # 'preferred_username'/email fallback would forge a different
        # subject per IdP config; sub is the stable contract).
        log.info("identity: token rejected: no sub claim")
        return None
    email = str(decoded.get("email") or "").strip()
    raw_groups = decoded.get("groups")
    if not isinstance(raw_groups, list):
        raw_groups = []
    groups = [str(g).strip() for g in raw_groups if str(g).strip()]
    return TokenClaims(subject=sub, email=email, groups=groups)


_BINDING_SQL = """
SELECT s.subject, s.idp, s.kind, s.groups
FROM api_keys k JOIN subjects s ON s.id = k.subject_id
WHERE k.id = $1
"""


async def _fetch_key_binding(key_id: uuid.UUID) -> dict[str, Any] | None:
    """The subjects row bound to one key, or None when the key is unbound.

    Raises on DB failure — the caller chooses the posture (resolve_caller
    propagates = fail-closed; subject_for_key catches = degrade-not-fail)."""
    row = await db.fetchrow(_BINDING_SQL, key_id)
    return dict(row) if row is not None else None


async def subject_for_key(key_id: uuid.UUID) -> str | None:
    """The subject bound to one key, or None (unbound, or the lookup failed).

    DEGRADE-NOT-FAIL on purpose (see module docstring): this feeds the
    X-MCP-Caller attribution stamp on already-authorized calls — enrichment
    must never take down a call the gates admitted, exactly like
    key_override_headers."""
    try:
        binding = await _fetch_key_binding(key_id)
    except Exception as exc:
        log.warning("identity: subject lookup failed for key %s — stamping key-fp: %s", key_id, type(exc).__name__)
        return None
    return str(binding["subject"]) if binding else None


async def resolve_caller(
    key: KeyRecord | None,
    *,
    headers: Mapping[str, str] | None = None,
    token: str | None = None,
) -> Caller | None:
    """The resolution ladder, one ``Caller`` per request (module docstring).

    Args:
        key: the ALREADY-AUTHENTICATED key record (core.keys.authenticate),
            or None when the surface is keyless (browser/token-only
            callers).
        headers: the request headers, for the browser rung. None (or a
            mapping without identity headers) skips the rung entirely.
        token: the RAW bearer credential when it was NOT a gateway key
            (the JWT-shaped bearer the auth surfaces extract before falling
            back to key authentication). None skips the token rung.

    Returns None only when NO rung produced an identity (no trusted browser
    headers AND no token identity AND no key) — callers treat that as
    unauthenticated, fail-closed.
    Raises on DB or JWKS failure (the fail-closed posture — an unavailable
    identity ledger or IdP is a refusal, never a bypass; the auth surfaces
    turn any exception into 401 exactly as _McpAuthASGI does for key
    lookups today).
    """
    # Rung 1 — browser headers, ONLY under the trust flag (module docstring:
    # flag + enforced AuthorizationPolicy is the deployment precondition).
    if headers is not None and settings_trust_proxy_identity():
        browser = browser_identity_from_headers(headers)
        if browser is not None:
            return Caller(
                subject=browser[0],
                subject_kind=KIND_BROWSER,
                groups=browser[1],
                key=key,
                key_fp=key_fingerprint(key),
            )

    # Rung 2 — the Keycloak token. Valid token wins over the key rungs for
    # ATTRIBUTION (it is the freshest identity assertion); authorization
    # still rides the key (or the token's shadow key). A token that FAILS
    # validation (None) falls through to the key rungs — never a hard 401
    # while a ladder-lower credential exists (the TTL-invisibility rule).
    if token is not None and token_validation_configured():
        claims = await validate_token(token)
        if claims is not None:
            return Caller(
                subject=claims.subject,
                subject_kind=KIND_TOKEN,
                groups=claims.groups,
                key=key,
                key_fp=key_fingerprint(key),
            )

    # Rungs 3-4 — the key anchor. No key at all: nothing resolved.
    if key is None:
        return None

    # Rung 3 — identity-bound key (subject via FK). Exceptions propagate
    # (fail-closed).
    binding = await _fetch_key_binding(key.id)
    if binding is not None:
        return Caller(
            subject=str(binding["subject"]),
            subject_kind=KIND_KEY_BOUND,
            groups=list(binding["groups"] or []),
            key=key,
            key_fp=key_fingerprint(key),
        )

    # Rung 4 — anonymous key-fp: pseudonymous, enforcement unchanged.
    return Caller(
        subject=None,
        subject_kind=KIND_KEY_ANON,
        groups=[],
        key=key,
        key_fp=key_fingerprint(key),
    )


def settings_trust_proxy_identity() -> bool:
    """The trust flag, read per call (tests flip it via monkeypatched
    settings without reloads)."""
    from ..config import settings

    return bool(settings.trust_proxy_identity)


_BIND_SUBJECT_SQL = """
INSERT INTO subjects (tenant_id, kind, idp, subject, display_name, groups)
VALUES ($1, $2, $3, $4, $5, $6)
ON CONFLICT (idp, subject) DO UPDATE
SET display_name = EXCLUDED.display_name, groups = EXCLUDED.groups
RETURNING id
"""

_STAMP_BINDING_SQL = """
UPDATE api_keys SET subject_id = $2, bound_via = $3, bound_at = now() WHERE id = $1
"""


async def bind_key_subject(
    key_id: uuid.UUID,
    *,
    subject: str,
    idp: str,
    bound_via: str,
    tenant_id: uuid.UUID | None = None,
    display_name: str = "",
    groups: list[str] | None = None,
    conn: Any | None = None,
) -> uuid.UUID | None:
    """Bind one issued key to a subject: upsert the subject row (UNIQUE
    (idp, subject) — the same human redeeming twice lands on ONE subject
    row) and stamp subject_id/bound_via/bound_at onto the key.

    Postures (module docstring "two jobs, two postures"):
    - ``conn=None`` (default): module-pool writes, BEST-EFFORT — a failed
      binding is logged and None returned, never raised. The issuance flow
      that already minted a shown-once credential must not be failed by its
      enrichment (the over-provision ordering rotate/create share); the key
      simply stays anonymous (subject_id NULL = today's enforcement).
    - ``conn=<acquired connection>``: the statements join the caller's
      transaction and failures PROPAGATE (rollback) — for flows that bind
      before anything irreplaceable exists (admin console create: the whole
      create rolls back and the operator retries clean).

    Returns the subject id, or None only in the best-effort posture when the
    binding did not land. ``groups`` must be a plain list (asyncpg TEXT[]).
    """
    if conn is not None:
        row = await conn.fetchrow(
            _BIND_SUBJECT_SQL,
            tenant_id,
            "user",  # Phase A flows bind humans; 'service' arrives with Phase B shadow-keys
            idp,
            subject,
            display_name,
            list(groups or []),
        )
        assert row is not None  # INSERT .. ON CONFLICT .. RETURNING yields a row
        subject_id: uuid.UUID = row["id"]
        await conn.execute(_STAMP_BINDING_SQL, key_id, subject_id, bound_via)
        return subject_id
    try:
        row = await db.fetchrow(
            _BIND_SUBJECT_SQL,
            tenant_id,
            "user",
            idp,
            subject,
            display_name,
            list(groups or []),
        )
        if row is None:  # pragma: no cover - RETURNING always yields a row
            return None
        subject_id = row["id"]
        await db.execute(_STAMP_BINDING_SQL, key_id, subject_id, bound_via)
        return subject_id
    except Exception:
        log.warning(
            "identity: binding key %s to subject %r (%s/%s) failed — key stays anonymous",
            key_id,
            subject,
            idp,
            bound_via,
            exc_info=True,
        )
        return None


# ---------------------------------------------------------------------------
# Token shadow-keys (Phase B; review §1.3 flow 4)
# ---------------------------------------------------------------------------

_SHADOW_KEY_SQL = """
SELECT k.id, k.user_id, k.name, k.key_prefix, k.scopes, k.allowed_models,
       k.rate_limit_rpm, k.rate_limit_tpm, k.max_concurrent,
       k.expires_at, k.revoked_at, k.relay_default, k.stream_pcai_events,
       p.offpeak_fraction
FROM api_keys k
JOIN users u ON u.id = k.user_id
LEFT JOIN profiles p ON p.id = u.profile_id
WHERE k.subject_id = (SELECT id FROM subjects WHERE idp = $1 AND subject = $2)
  AND k.bound_via = $3 AND k.revoked_at IS NULL
ORDER BY k.created_at DESC, k.id DESC
LIMIT 1
"""
# (audit P2-6) ONE round-trip: the subject lookup rides as a subselect on the
# UNIQUE (idp, subject) index; stream_pcai_events now selected so shadow keys
# honor their stored column instead of the dataclass default (False since the
# schema flip).

_SHADOW_TENANT_SQL = "SELECT id FROM tenants WHERE id = $1"


def shadow_keys_enabled() -> bool:
    """The shadow-key flag AND a target tenant — both required (a shadow
    key must live in SOME tenant to satisfy api_keys.user_id). Read per
    call (tests flip settings without reloads)."""
    from ..config import settings

    return bool(settings.token_shadow_keys) and bool(settings.token_shadow_tenant_id)


def _shadow_settings() -> dict[str, Any]:
    from ..config import settings

    return {
        "tenant_id": settings.token_shadow_tenant_id,
        "key_name": settings.token_shadow_key_name,
        "rpm": settings.token_shadow_rate_limit_rpm,
        "tpm": settings.token_shadow_rate_limit_tpm,
    }


def _row_to_key_record(row: Any) -> KeyRecord:
    """The authenticate() SELECT shape → KeyRecord (the shadow-key lookup
    mirrors authenticate's own row→record mapping exactly)."""
    return KeyRecord(
        id=row["id"],
        user_id=row["user_id"],
        name=row["name"],
        key_prefix=row["key_prefix"],
        scopes=list(row["scopes"]),
        allowed_models=list(row["allowed_models"]),
        rate_limit_rpm=row["rate_limit_rpm"],
        rate_limit_tpm=row["rate_limit_tpm"],
        max_concurrent=row["max_concurrent"],
        expires_at=row["expires_at"],
        relay_default=bool(row["relay_default"]),
        stream_pcai_events=bool(row["stream_pcai_events"]),
        offpeak_fraction=row["offpeak_fraction"],
    )


async def _provision_shadow_key(subject: str, claims: TokenClaims | None) -> KeyRecord | None:
    """Create user + subject + shadow key for a FIRST-seen token subject.

    Ordering (over-provision, like rotate): user → subject upsert (bind) →
    key INSERT with subject_id stamped in the SAME statement (a crash
    leaves an orphan user row — harmless; it NEVER leaves a keyless half).
    best-effort at the top level: a provisioning failure must not turn an
    otherwise-valid token into a 401 (the token identity still resolves;
    only the ledger anchor is missing — the next sighting retries).
    """

    cfg = _shadow_settings()
    try:
        tenant_id = uuid.UUID(str(cfg["tenant_id"]))
    except ValueError:
        log.error("identity: token_shadow_tenant_id %r is not a UUID — shadow-key skipped", cfg["tenant_id"])
        return None
    try:
        display_name = (claims.email if claims and claims.email else subject)[:64] or subject[:64]
        groups = list(claims.groups) if claims else []
        user_row = await db.fetchrow(
            "INSERT INTO users (tenant_id, name) VALUES ($1, $2) RETURNING id",
            tenant_id,
            display_name,
        )
        if user_row is None:  # pragma: no cover - RETURNING always yields a row
            return None
        user_id: uuid.UUID = user_row["id"]
        # Subject upsert: re-redeem lands on ONE subject row (the
        # UNIQUE (idp, subject) contract). DO UPDATE keeps groups fresh.
        subject_row = await db.fetchrow(
            """
            INSERT INTO subjects (tenant_id, kind, idp, subject, display_name, groups)
            VALUES ($1, 'user', $2, $3, $4, $5)
            ON CONFLICT (idp, subject) DO UPDATE
            SET display_name = EXCLUDED.display_name, groups = EXCLUDED.groups
            RETURNING id
            """,
            tenant_id,
            IDP_KEYCLOAK,
            subject,
            display_name,
            groups,
        )
        assert subject_row is not None  # INSERT .. ON CONFLICT .. RETURNING yields a row
        subject_id: uuid.UUID = subject_row["id"]
        _plaintext, key_hash = generate_key()
        # NOTE: the plaintext is DISCARDED — the caller authenticates with
        # its TOKEN, not with a key secret; the shadow key is the ledger's
        # anchor (key_id), never a distributed credential.
        row = await db.fetchrow(
            "INSERT INTO api_keys (user_id, name, key_hash, key_prefix, scopes, "
            "allowed_models, rate_limit_rpm, rate_limit_tpm, subject_id, bound_via, bound_at) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,now()) RETURNING id",
            user_id,
            cfg["key_name"],
            key_hash,
            _plaintext[:10],
            ["proxy", "mcp"],
            [],
            int(cfg["rpm"]),
            int(cfg["tpm"]),
            subject_id,
            VIA_TOKEN_SHADOW,
        )
        assert row is not None
        return await find_shadow_key(subject)
    except Exception:
        log.exception("identity: shadow-key provisioning failed for subject %r — token resolves keyless", subject)
        return None


async def find_shadow_key(subject: str) -> KeyRecord | None:
    """The subject's NEWEST NON-REVOKED shadow key (bound_via='token-
    shadow'), via UNIQUE (idp, subject) → subject_id → newest key. None
    when the subject has no live shadow key. Raises on DB failure (the
    fail-closed posture — callers are auth surfaces)."""
    krow = await db.fetchrow(_SHADOW_KEY_SQL, IDP_KEYCLOAK, subject, VIA_TOKEN_SHADOW)
    return _row_to_key_record(krow) if krow is not None else None


async def resolve_shadow_key(subject: str, claims: TokenClaims | None = None) -> KeyRecord | None:
    """The shadow-key entry point the auth surfaces call after a token
    resolves: the subject's newest live shadow key, provisioned on first
    sighting when the flag is on.

    The flag gates PROVISIONING, not RECOGNITION (review §1.3: "off =
    tokens validate but don't provision"): an already-provisioned shadow
    key keeps working when the operator flips the flag back off — console
    revocation is the off switch for an existing shadow key. Flag off +
    unknown subject ⇒ None WITHOUT provisioning (the anonymous-fp
    posture). DB failures PROPAGATE (fail-closed — the auth surfaces turn
    them into 401 exactly like a key-lookup failure)."""
    existing = await find_shadow_key(subject)
    if existing is not None:
        return existing
    if not shadow_keys_enabled():
        return None
    return await _provision_shadow_key(subject, claims)
