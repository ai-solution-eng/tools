"""Bearer-key extraction for data-plane requests.

FastAPI dependency: extracts the virtual key from the Authorization header,
resolves it via core.keys.authenticate, and attaches it to request.state.
Fail-closed: no key / unknown key / revoked / expired → 401, never an
implicit allow.

Phase B — the identity ladder's TOKEN RUNG (core.identity): a JWT-shaped
bearer is first tried as a Keycloak token when token validation is
configured. THE FALL-THROUGH RULE (review §1.1; what makes the 30-minute
token TTL invisible to headless users): a token that FAILS validation is
NOT a hard 401 when the same bearer also authenticates as a virtual key —
the key identity wins. A token that VALIDATES still authorizes nothing by
itself: authorization requires a ledger key — the subject's shadow key
(existing, or lazily provisioned under PCAI_LLM_TOKEN_SHADOW_KEYS). A
valid token with no key resolves an identity for attribution/logging
(request.state.caller) and then 401s — anonymous-fp posture, flag off.

An hllm- virtual key NEVER reaches the token path (the JWT shape gate
filters it), so the pure-key path is byte-for-byte today's behavior: the
same authenticate() call, the same 401 on unknown/revoked/expired, the
same exception propagation (PoolUnavailable → the app's 503 handler).

SSO envelope (2026-10-06, brief §Z1) — ``X-API-Key: hllm-…``: behind the
edge SSO chain the oauth2-proxy INJECTS ``Authorization: Bearer
<id_token>`` into every forwarded request, so the browser SPA cannot
deliver a minted virtual key via the Authorization header at all (the
MM RAG D25 lesson). ``X-API-Key`` is the additional credential envelope
for exactly that posture. PRECEDENCE — DE-ESCALATION-ONLY (fleet
convention D19, the ``pcai_utils/mcp_auth.presented_keys_with_source``
pattern): an explicit ``X-API-Key`` OUTRANKS the co-forwarded proxy
Bearer, because the Bearer is a 30-minute identity token while the
X-API-Key is the caller's own long-lived ledger key identifying the SAME
subject. The fall-through rule stays scoped to INVALID TOKENS (the proxy
envelope) and never rescues a failed EXPLICIT key: an X-API-Key that
fails authenticate() is a hard 401 regardless of the co-forwarded
Bearer — except the one shape below where the Bearer itself
authenticates as a virtual key, and even there key-vs-key with one dead
key REFUSES (two keys presented, one dead = refuse; fail-closed).

With NO X-API-Key header present the resolution is byte-for-byte
unchanged (the existing suite is the proof).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

import fastapi
from fastapi import Request

from ..core import identity
from ..core.keys import KeyRecord, authenticate

EnvelopeAuth = Callable[[str], Awaitable[KeyRecord | None]]
"""The authenticate seam's shape: one plaintext credential → KeyRecord |
None. Both auth surfaces (REST require_key and the /mcp ASGI wrapper)
pass their own module-global authenticate here so each surface's test
monkeypatch seam keeps working and the two surfaces cannot drift."""

API_KEY_HEADER = "x-api-key"
"""The SSO credential envelope header (lowercase ASGI name)."""


def _bearer_of(request: Request) -> str:
    """The raw bearer credential ('' when absent / wrong scheme)."""
    auth = request.headers.get("authorization", "")
    return auth[7:].strip() if auth.lower().startswith("bearer ") else ""


def _bearer_of_mapping(headers: Mapping[str, str]) -> str:
    """The bearer credential from a plain header mapping ('' when absent /
    wrong scheme) — the mapping-side sibling of ``_bearer_of``. Both feed
    ``resolve_bearer_key`` so the REST and ASGI surfaces extract the
    identical credential."""
    auth = headers.get("authorization", "")
    return auth[7:].strip() if auth.lower().startswith("bearer ") else ""


def _headers_of(request: Request) -> Mapping[str, str]:
    """The request headers as a plain mapping (Starlette Headers are
    case-insensitive, so exact-case lookups on the lowercased name work)."""
    return request.headers  # type: ignore[return-value]


async def _token_ladder(
    plaintext: str,
    authenticate: EnvelopeAuth,
) -> tuple[KeyRecord | None, identity.Caller | None]:
    """One run of the token ladder for ONE bearer credential — the exact
    sequence both auth surfaces have always run (JWT-shape gate → token
    validation → shadow key → fall through to authenticate). Returns
    ``(key, caller)``:

    - ``key``: the authorization anchor the ladder produced (shadow key
      or authenticated virtual key), or None;
    - ``caller``: the resolved identity to attribute (request.state.caller
      / scope state), or None.

    Infrastructure failures (JWKS down, DB errors) propagate — the
    fail-closed posture both surfaces share.
    """
    key: KeyRecord | None = None
    caller = None
    if identity._JWT_SHAPE_RE.match(plaintext) and identity.token_validation_configured():
        try:
            caller = await identity.resolve_caller(None, token=plaintext)
        except Exception:
            # JWKS/infrastructure failure: fail CLOSED (the _McpAuthASGI
            # posture — an unavailable IdP is a refusal, never a bypass).
            raise fastapi.HTTPException(401, "token validation unavailable") from None
        if caller is not None and caller.subject is not None:
            # Token verified. Authorization still needs a ledger key: the
            # subject's shadow key (newest non-revoked, or first-sighting
            # provisioning when the flag is on). DB failures propagate —
            # same posture as authenticate (never a silent open door).
            key = await identity.resolve_shadow_key(caller.subject)
    if key is None:
        # Fall-through (invalid-token and no-token paths land here): the
        # credential may still be a virtual key.
        key = await authenticate(plaintext)
    return key, caller


async def resolve_bearer_key(
    request: Any | None,
    *,
    authenticate: EnvelopeAuth,
    headers: Mapping[str, str],
) -> tuple[KeyRecord, identity.Caller | None]:
    """The ONE envelope+precedence resolver both auth surfaces share —
    REST ``require_key`` and the pure-ASGI ``_McpAuthASGI`` (routes_mcp)
    call this with their own ``authenticate`` seam so REST and MCP cannot
    drift. ``request`` is the FastAPI Request (or any object with a
    ``.state`` attribute — the wrapper passes a minimal shim whose
    ``state`` IS the ASGI scope state) so the caller-record path
    (``request.state.caller``) is written identically on both surfaces.

    Credential order (fleet convention D19, de-escalation-only):

    1. ``X-API-Key`` present → it is THE explicit credential: authenticate
       it. Success → that key. Failure → 401 unless the co-forwarded
       Bearer itself authenticates as a virtual key (a normal key
       presented twice — the historical shape) — and even then REFUSE:
       two keys presented with one dead is a fail-closed refusal, never a
       silent rescue. A dead X-API-Key is never laundered through the
       proxy Bearer's IdP identity (a dead user key must not escalate to
       the token rung on the Bearer's behalf).
    2. No X-API-Key → the bearer exactly as before this envelope existed:
       the token ladder (JWT-shape gate → validate → shadow key), then
       authenticate — byte-for-byte the pre-envelope behavior, which is
       the flag-off oracle.

    Returns ``(key, caller)``. Every refusal raises HTTPException(401);
    infrastructure failures (JWKS/DB) propagate exactly as the surfaces
    have always handled them.
    """
    plaintext = _bearer_of_mapping(headers)
    envelope = headers.get(API_KEY_HEADER, "").strip()
    if not plaintext and not envelope:
        raise fastapi.HTTPException(401, "missing bearer key")

    key: KeyRecord | None = None
    caller = None
    if envelope:
        # The explicit envelope outranks the co-forwarded proxy Bearer
        # (D19). A dead X-API-Key is a 401 on its own — EXCEPT when the
        # Bearer is itself virtual-key-shaped-and-live (both headers
        # carried the same user key), and even there one dead key of two
        # presented = refuse (fail-closed posture, brief §Z1 final word).
        key = await authenticate(envelope)
        if key is None:
            if plaintext and not identity._JWT_SHAPE_RE.match(plaintext):
                bearer_key = await authenticate(plaintext)
                if bearer_key is not None:
                    raise fastapi.HTTPException(401, "invalid, revoked, or expired key") from None
            raise fastapi.HTTPException(401, "invalid, revoked, or expired key") from None
        # Attribution with the co-forwarded Bearer: NON-FATAL by design —
        # the envelope already authorized the request; a token resolution
        # failure here must not fail an authorized call.
        if plaintext and identity._JWT_SHAPE_RE.match(plaintext) and identity.token_validation_configured():
            try:
                caller = await identity.resolve_caller(None, token=plaintext)
            except Exception:
                caller = None
        return key, caller

    key, caller = await _token_ladder(plaintext, authenticate)
    if key is None:
        if caller is not None and request is not None:
            request.state.caller = caller  # resolved identity, refused authorization
        raise fastapi.HTTPException(401, "invalid, revoked, or expired key")
    return key, caller


async def require_key(request: Request) -> KeyRecord:
    """Authenticate the request into a KeyRecord (fail-closed).

    Precedence — fleet convention D19, DE-ESCALATION-ONLY (full rationale
    in the module docstring): an explicit ``X-API-Key`` outranks the
    co-forwarded proxy ``Authorization: Bearer``. An X-API-Key that fails
    authenticate() is 401 REGARDLESS of the Bearer, except the bearer-
    key-valid shape — and even there two keys with one dead refuse. The
    fall-through rule exists for invalid TOKENS (the proxy envelope), not
    for failed explicit keys.

    Without the envelope, the order is exactly the pre-envelope ladder
    (first hit wins):
    1. JWT-shaped bearer + token validation configured → validate_token:
       a VALID token resolves the caller (subject_kind='token') and picks
       up its shadow key when one exists / can be provisioned (flag on).
       The KeyRecord is the AUTHORIZATION anchor (scopes/budgets/limits);
       the token is only ever the IDENTITY assertion.
       - invalid token → falls through to (2); if it was the only
         credential, that lands in the same 401 as an unknown key;
       - valid token, no live/provisionable shadow key → the RESOLVED
         identity rides request.state.caller for attribution/logging,
         then 401 (authenticated identity, unauthorized caller).
    2. core.keys.authenticate — today's behavior exactly (unknown/
       revoked/expired → 401; DB failures propagate, never silently
       become 401s).
    """
    key, caller = await resolve_bearer_key(
        request,
        authenticate=authenticate,
        headers=_headers_of(request),
    )
    if caller is not None:
        request.state.caller = caller
    request.state.key = key
    return key


def require_scope(*needed: str):
    async def dep(key: KeyRecord = fastapi.Depends(require_key)) -> KeyRecord:
        if not set(needed) <= set(key.scopes):
            raise fastapi.HTTPException(403, f"key lacks required scope(s): {needed}")
        return key

    return dep
