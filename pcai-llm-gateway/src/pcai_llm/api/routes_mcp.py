"""Federated /mcp endpoint: MCP 2.0 streamable-http front door to the fleet's
upstream MCP servers, behind gateway virtual-key auth and per-tool ACLs.

Wiring (mirrors mcp_servers/applygate_mcp/server.py + mcp_auth.py):

* The streamable-http ASGI app (json_response=True, stateless_http=True —
  any replica serves any request) is mounted at /mcp behind a pure-ASGI
  bearer-auth wrapper that authenticates EVERY request via
  core.keys.authenticate (fail closed: 401 on missing/invalid/revoked/
  expired) and additionally requires the "mcp" scope (403). 401/403 are
  returned as plain JSON before the MCP session manager ever sees them.
* ``mount(app)`` appends a Starlette Route("/mcp", <wrapper>) directly to
  the app's router and merges the MCP session-manager's run() context into
  the app's lifespan. app.py calls ``routes_mcp.mount(app)`` exactly once
  at factory time. Why a Route (not app.mount): a Mount at "/mcp" makes the
  MCP SDK's internal Route("/mcp") live at /mcp/mcp (and 307-redirects the
  exact /mcp path, which streamable-http clients refuse to follow);
  appending the route keeps the SDK's /mcp path working as-is under the
  FastAPI router.

Tool surface — TWO layers, ONE gate chain:

* Flat per-tool exposure (default, ``PCAI_LLM_MCP_FLAT_TOOLS=1``): tools/list
  returns the CALLING KEY's granted tools as individual MCP tools named
  ``{server}__{tool}`` (sanitized to the MCP name grammar, see
  ``flat_tool_name``), each carrying the upstream tool's real inputSchema and
  annotations. Statelessness makes the per-request listing natural: the auth
  wrapper binds the caller before the MCP app runs, and the gateway's
  MCPServer subclass serves tools/list dynamically per request (see
  _GatewayMCPServer for the inspected SDK extension point and the contextvar
  evidence test). A server on which the key holds ZERO grants contributes NO
  tools and NO URL anywhere in the response — the same discipline as the
  catalog's granted_servers filter. Zero grants also means zero upstream
  fetches.
* The two gateway-level tools — ``mcp_catalog`` (what can this key see?) and
  ``mcp_call`` (call one granted tool on one upstream server by explicit
  name) — KEEP WORKING unchanged as the compat surface for off-cluster
  consumers (sproket et al.) that still list/call by explicit name.

Preserved choke point (the reason this stays safe): tools/call accepts BOTH
flattened names and the gateway tool names, and EVERY path funnels through
the same gate chain in ``_gated_mcp_call`` — validation → RPM rate limit →
registry lookup → per-key ACL (default-deny) → server tool-policy → proxied
call → ledger settle. The flat surface re-exposes upstream tools by NAME
only; it does not create a second enforcement path. ``_mcp_call`` is now a
thin wrapper over that chain (failover disabled — an explicit target never
silently moves), while flattened calls additionally FAILOVER on
transport-level failure to the next priority-ordered enabled server that (a)
exposes the same tool per the cached listing, (b) the key holds an explicit
grant on, (c) passes tool policy — bounded by ``PCAI_LLM_MCP_FAILOVER_
ATTEMPTS`` retries after the primary. THIS ASYMMETRY IS DELIBERATE:
``mcp_call(server, tool)`` names its target, so moving it would silently
change what the caller addressed; a flattened call names only a tool, and
the fleet priority order is the documented tiebreak. Kill switch: setting
``PCAI_LLM_MCP_FLAT_TOOLS=0`` reverts tools/list to exactly the two gateway
tools and unknown-tools flattened calls — the pre-flat surface, bit-for-bit.

Listing cache: raw per-server upstream listings are cached per server for
``PCAI_LLM_MCP_CATALOG_CACHE_S`` seconds (key-independent payload; ACL
filtering stays per-request) and invalidated on every admin MCP-registry
mutation via the registry-change hook (core.registry.on_mcp_registry_change).
A dead upstream is skipped-with-error exactly as before; a STALE cached
listing is preferred over an error (marked choice: discovery availability
beats freshness).

Caller identity: the auth wrapper records the authenticated KeyRecord in a
contextvar. The stateless session manager serves requests inline in the
request task (json_response mode) / spawns handlers from it (task groups
inherit contextvars), so BOTH tools/call and tools/list read the calling key
without the MCP SDK needing custom auth plumbing — pinned by the HTTP-level
test in tests/test_mcp_flat.py.

Upstream calls: a fresh MCP client session per call (MCP servers here are
low-QPS; per-loop client pooling is a known TODO), explicit
settings.upstream_timeout_s timeouts (listings bounded to 15 s — the
metadata-fetch audit), and follow_redirects=False — SSRF hardening from the
fleet audit. The registry is admin-controlled, but a 30x that bounces an
in-cluster URL toward a metadata/private address must not be followed; with
redirects off a redirect surfaces as a plain error.
"""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
import logging
import os
import re
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

import fastapi
import httpx2
from fastapi import APIRouter, FastAPI
from fastapi.routing import _merge_lifespan_context
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server import MCPServer
from mcp.server.mcpserver.context import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, InputRequiredResult, TextContent, ToolAnnotations
from mcp.types import Tool as MCPTool
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from ..config import settings
from ..core import metrics as _metrics
from ..core import registry, secretbox
from ..core.budgets import check_rate_limit, settle
from ..core.identity import (
    KIND_KEY_BOUND,
    MCP_CALLER_CLASS_HEADER,
    MCP_CALLER_HEADER,
    MCP_CALLER_HOP_SUFFIX,
    MCP_CALLER_SUBJECT_HEADER,
    sanitize_caller_value,
    subject_for_key,
)
from ..core.keys import KeyRecord, authenticate
from ..db import db
from ..mcpf.federation import is_allowed, load_grants, server_allows, visible_tools
from . import auth

log = logging.getLogger(__name__)

# Routing/hop headers a tenant credential override must never set (audit
# P1-5 + review round 2): the write path reserves these names
# (routes_enroll.RESERVED_HEADER_NAMES); this is the matching post-merge
# strip, compared case-insensitively. Transfer-Encoding/Connection/TE/
# Upgrade are hop-by-hop (a tenant TE alongside httpx2's own
# Content-Length is smuggling-shaped for a lenient upstream);
# X-HTTP-Method-Override / X-Original-URL / X-Rewrite-URL are classic
# reverse-proxy ACL bypasses against envoy-fronted in-cluster MCPs.
_RESERVED_HOP_HEADERS = frozenset(
    {
        "host",
        "content-length",
        "forwarded",
        "x-forwarded-for",
        "x-forwarded-host",
        "x-forwarded-proto",
        "x-forwarded-port",
        "x-forwarded-prefix",
        "x-real-ip",
        "transfer-encoding",
        "connection",
        "te",
        "upgrade",
        "x-http-method-override",
        "x-original-url",
        "x-rewrite-url",
    }
)


# Contract surface: the /mcp endpoint itself is a mounted ASGI app (the MCP
# SDK owns its route table), so this router intentionally carries no routes;
# anything FastAPI-native later added under /mcp/* can live here.
router = APIRouter(tags=["mcp"])


# ---------------------------------------------------------------------------
# Caller identity (contextvar set by the auth wrapper, read by the tools)
# ---------------------------------------------------------------------------

_caller: contextvars.ContextVar[KeyRecord | None] = contextvars.ContextVar("mcp_caller", default=None)


def _current_key() -> KeyRecord:
    """The authenticated calling key, or raise (defensive: the auth wrapper
    guarantees this is set for every request that reaches a tool)."""
    key = _caller.get()
    if key is None:  # pragma: no cover - wrapper guarantees presence
        raise RuntimeError("no authenticated key on /mcp request")
    return key


# ---------------------------------------------------------------------------
# Pure-ASGI bearer-auth wrapper (applygate mcp_auth.py pattern, DB-backed)
# ---------------------------------------------------------------------------

_UNAUTHORIZED_BODY = b'{"error": "unauthorized: missing, invalid, or revoked key"}'
_FORBIDDEN_BODY = b'{"error": "key lacks the required scope: mcp"}'


def _send_json_error(status: int, body: bytes, send: Send) -> Awaitable[None]:
    """Emit a small JSON error response over raw ASGI (before the MCP app)."""
    return _send_json_error_impl(status, body, send)


async def _send_json_error_impl(status: int, body: bytes, send: Send) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
                (b"www-authenticate", b"Bearer"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


class _McpAuthASGI:
    """Authenticate every request BEFORE the MCP session manager sees it.

    Fail closed on every error path: missing header, non-bearer scheme,
    unknown/revoked/expired key (authenticate -> None) and DB failures all
    end in 401 — a broken key ledger must never open the federation surface.
    A valid key without the "mcp" scope gets 403 (authenticated, not
    authorized — the two conditions are reported differently on purpose).

    Phase B — the identity ladder's TOKEN RUNG (core.identity): a
    JWT-shaped bearer is first tried as a Keycloak token when token
    validation is configured. THE FALL-THROUGH RULE (review §1.1): an
    INVALID token is NOT a hard 401 while the same bearer also
    authenticates as a virtual key — the key wins. A token that VALIDATES
    still authorizes nothing by itself: authorization requires the
    subject's ledger key — the shadow key (existing, or lazily
    provisioned under PCAI_LLM_TOKEN_SHADOW_KEYS; its scopes carry the
    "mcp" the gate below requires). Valid token, no key: the resolved
    identity is recorded on the scope (attribution) and the request is
    401'd — anonymous-fp posture, flag off. An hllm- key NEVER reaches
    the token path (the JWT shape gate filters it), so the pure-key path
    is byte-for-byte today's behavior.

    SSO envelope (brief §Z1) — ``X-API-Key``: the SAME envelope and
    precedence the REST surface (api/auth.require_key) applies, via the
    SAME shared resolver (api/auth.resolve_bearer_key) so the two
    surfaces cannot drift: an explicit ``X-API-Key`` OUTRANKS the
    co-forwarded proxy Bearer (fleet convention D19, de-escalation-only),
    and an X-API-Key that fails authenticate() is a hard 401 regardless
    of the Bearer (two keys presented with one dead = refuse). With no
    X-API-Key header the resolution is byte-for-byte the pre-envelope
    behavior.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {name.decode("latin-1").lower(): value.decode("latin-1") for name, value in scope.get("headers", [])}

        class _ScopeState:
            """Attribute facade over the scope's state dict — the
            write-through the resolver's caller-record needs (F1, wave-1
            review): the resolver records the attributed caller as a
            plain ATTRIBUTE assignment (``request.state.caller = caller``),
            so a bare dict there raised AttributeError on a ROUTINE
            valid-token-no-key refusal — broken attribution plus a logged
            traceback from the fail-closed 401 handler. Writes land in the
            scope's state dict (the pre-envelope contract,
            ``scope['state']['pcai_caller']``, readable downstream holds
            byte-for-byte); reads resolve from the same dict."""

            def __init__(self, state: dict[str, Any]) -> None:
                self._state = state

            def __getattr__(self, name: str) -> Any:
                return self._state[name]

            def __setattr__(self, name: str, value: Any) -> None:
                if name == "_state":
                    super().__setattr__(name, value)
                    return
                self._state[name] = value

        class _ScopeRequest:
            """Minimal request shim over the raw scope so the shared
            resolver's caller-record path (``request.state.caller``)
            writes the same scope-state slot this wrapper has always
            used (``scope['state']['pcai_caller']``)."""

            def __init__(self, scope: Scope) -> None:
                self.headers = headers
                self.state = _ScopeState(scope.setdefault("state", {}))

        plaintext = auth._bearer_of_mapping(headers)
        envelope = headers.get(auth.API_KEY_HEADER, "").strip()
        if not plaintext and not envelope:
            await _send_json_error(401, _UNAUTHORIZED_BODY, send)
            return
        try:
            key, _caller_obj = await auth.resolve_bearer_key(
                _ScopeRequest(scope),
                authenticate=authenticate,
                headers=headers,
            )
        except fastapi.HTTPException as exc:
            # The resolver's credential refusals (missing/invalid/revoked/
            # expired key, or the token-validation-unavailable posture)
            # surface here as this wrapper's own 401 body — the same
            # refusal shapes the inline paths below have always emitted.
            log.info("mcp auth: refusing request: %s", exc.detail)
            await _send_json_error(401, _UNAUTHORIZED_BODY, send)
            return
        except Exception:
            # DB down / pool exhausted / JWKS failure: fail CLOSED (the
            # applygate lesson — an unavailable ledger is a refusal
            # condition, not a bypass).
            log.exception("mcp auth: key lookup failed; refusing request")
            await _send_json_error(401, _UNAUTHORIZED_BODY, send)
            return
        if "mcp" not in key.scopes:
            await _send_json_error(403, _FORBIDDEN_BODY, send)
            return
        token = _caller.set(key)
        try:
            await self.app(scope, receive, send)
        finally:
            _caller.reset(token)


# ---------------------------------------------------------------------------
# Upstream MCP client helpers (per-call client; SSRF-safe)
# ---------------------------------------------------------------------------


def build_mcp_headers(
    base: dict[str, str],
    auth_secret: str | None,
    auth_token_enc: str | None = None,
) -> dict[str, str]:
    """Static headers + Bearer auth for one upstream MCP server.

    Resolution ORDER (first hit wins):
    1. `auth_token_enc` — Fernet ciphertext from the admin panel, decrypted
       per call with PCAI_LLM_SECRETS_MASTER_KEY.
    2. `auth_secret` — BY-REFERENCE: K8s Secret NAME whose contents the chart
       injects as env PCAI_LLM_MCP_KEY__<NAME_NORMALIZED>. Missing env ->
       warn + unauthenticated (the fleet server 401s visibly).
    """
    headers = dict(base)
    if auth_token_enc:
        from ..core import secretbox

        token = secretbox.decrypt(auth_token_enc)
        if token:
            headers["Authorization"] = f"Bearer {token}"
            _metrics.upstream_auth_degraded(auth_secret or "panel-token", False)
            return headers
        log.error(
            "mcp server has a stored credential but it is undecryptable "
            "(master key missing/rotated) — calling unauthenticated"
        )
    if auth_secret:
        env_name = "PCAI_LLM_MCP_KEY__" + auth_secret.upper().replace("-", "_")
        secret = os.environ.get(env_name)
        if secret:
            headers["Authorization"] = f"Bearer {secret}"
            _metrics.upstream_auth_degraded(auth_secret or "panel-token", False)
        else:
            log.warning(
                "mcp auth secret %r not found as env %s — calling without Authorization header",
                auth_secret,
                env_name,
            )
            _metrics.upstream_auth_degraded(auth_secret or "panel-token", True)
    return headers


# ---------------------------------------------------------------------------
# Per-key MCP credential overrides (memory-MCP dataset/password pattern)
# ---------------------------------------------------------------------------


async def key_override_headers(key_id: uuid.UUID, server_name: str) -> dict[str, str]:
    """Decrypt one key's stored header overrides (mcp_server_creds) for one
    server — the per-tenant identity headers (e.g. X-Memory-Dataset /
    X-Dataset-Password) an admin attached to this key.

    Degrade-not-fail on EVERY failure path (no row, master key missing or
    rotated, corrupt JSON, DB unavailable): overrides are enrichment, and a
    broken lookup must never take down a call that was otherwise fully
    authorized — the upstream's own 401/404 keeps a missing-credential
    misconfiguration visible.
    """
    try:
        row = await db.fetchrow(
            "SELECT headers_enc FROM mcp_server_creds WHERE key_id = $1 AND server_name = $2",
            key_id,
            server_name,
        )
    except Exception as exc:
        log.warning(
            "mcp creds lookup failed for key %s/%s — overrides ignored: %s", key_id, server_name, type(exc).__name__
        )
        return {}
    if row is None:
        return {}
    raw = secretbox.decrypt(row["headers_enc"])
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:
        log.warning("mcp creds for key %s/%r: stored JSON undecodable — overrides ignored", key_id, server_name)
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {str(name): str(value) for name, value in parsed.items()}


async def resolve_mcp_headers(
    target: dict[str, Any],
    key_id: uuid.UUID | None = None,
    *,
    caller_subject: str | None = None,
) -> dict[str, str]:
    """Full header resolution for one upstream MCP call, for one calling key.

    Merge ORDER: build_mcp_headers first (static headers + server-level
    auth, first-hit wins inside it), then the key's decrypted overrides
    merged LAST-WINS — a tenant override may ADD headers or REPLACE a
    server-level one (e.g. a per-tenant Authorization). Only the CALL paths
    (direct /mcp via _mcp_call and the relay executor) use this; catalog /
    metadata fetches stay key-independent.

    Phase A attribution: AFTER the last-wins override merge, the gateway
    stamps ``X-MCP-Caller: <subject or key-fp>`` (gated on
    PCAI_LLM_MCP_CALLER_ATtribution, default on — see
    settings.mcp_caller_attribution; set to False to restore the pre-stamp
    surface byte-for-byte). THE STAMP RUNS AFTER THE MERGE AND OVERWRITES:
    per-key credential overrides could otherwise spoof the header (the name
    passes the token-ish grammar and the merge is last-wins — the verified
    vector from review §1.4; the write path additionally reserves the name
    with a 400). The value is sanitized (control chars stripped, 200 cap)
    and suffixed ``@gateway`` for hop legibility. ATTRIBUTION-NEVER-
    AUTHORIZATION: upstreams may record it in their audit chain; they must
    never authorize off it.

    Relay-attribution companions (SQLhandler identity.py rung 1): when a
    REAL subject resolved — a bound key, the degrade-not-fail lookup found a
    row — the SAME gated, post-merge block additionally stamps
    ``X-MCP-Caller-Subject: <subject>`` (bare, no @gateway suffix: the
    downstream sanitizes/parses it as an identity) and
    ``X-MCP-Caller-Class: <subject_kind>``. Only the key-fp fallback
    (unbound key, or the lookup failed) stamps NEITHER companion — an
    unbound key must not advertise a subject it does not have, and a stale
    tenant-supplied value must never upgrade a pseudonymous call downstream.
    The class value stays in the gateway's subject_kind vocabulary
    (KIND_KEY_BOUND here; SQLhandler's _KIND_CLASS_MAP maps it to its own
    bounded class labels).

    Args:
        caller_subject: the caller's resolved subject (core.identity), when
            known. None (the common case until the resolver is wired into
            the auth surfaces in Phase B) falls back to a degrade-not-fail
            subject lookup for the key (identity.subject_for_key), then to
            the key fingerprint; key_id=None (server-level callers like the
            offpeak evaluator) stamps nothing.
    """
    headers = build_mcp_headers(
        target.get("headers") or {},
        target.get("auth_secret"),
        target.get("auth_token_enc"),
    )
    if key_id is not None:
        headers.update(await key_override_headers(key_id, str(target.get("name") or "")))
        # Belt and braces (audit P1-5 + review flag: the stored dict is
        # case-PRESERVING, so a lowercase .pop is a no-op for any other
        # spelling — a pre-existing 'Host'/'HOST' row survived an earlier
        # version of this strip). Rebuild case-insensitively over the full
        # hop/routing-reserved set: caller-chosen hop-by-hop and proxy
        # routing headers never reach an in-cluster MCP upstream.
        headers = {name: value for name, value in headers.items() if name.lower() not in _RESERVED_HOP_HEADERS}
        if settings.mcp_caller_attribution:
            subject = caller_subject if caller_subject is not None else await subject_for_key(key_id)
            stamp = sanitize_caller_value(subject or "") or str(key_id)
            if stamp:
                headers[MCP_CALLER_HEADER] = stamp + MCP_CALLER_HOP_SUFFIX
            # Companion rung only on a REAL subject: '' (or whitespace-only)
            # means unbound key / failed lookup → key-fp rung → neither
            # companion header (an unbound key must not advertise a subject).
            attributed = sanitize_caller_value(subject or "")
            # Belt and braces (the X-MCP-Caller precedent): a row written
            # before the reservation must not survive the merge — for a
            # bound key the stamps below overwrite it; for an UNBOUND key
            # there is no stamp, so a stale tenant value is dropped outright.
            headers.pop(MCP_CALLER_SUBJECT_HEADER, None)
            headers.pop(MCP_CALLER_CLASS_HEADER, None)
            if attributed:
                headers[MCP_CALLER_SUBJECT_HEADER] = attributed
                headers[MCP_CALLER_CLASS_HEADER] = sanitize_caller_value(KIND_KEY_BOUND)
    return headers


async def _upstream_session[T](
    url: str,
    headers: dict[str, str],
    operation: Callable[[ClientSession], Awaitable[T]],
    timeout_s: float | None = None,
) -> T:
    """Run one async operation against an upstream MCP server in a fresh
    session. Per-call client (TODO: pool per running loop once upstream
    traffic justifies it); follow_redirects=False for SSRF hardening.

    SSRF note (moved from the deleted _upstream_timeout helper — audit
    finding 20, dead code carrying the load-bearing rationale): the MCP SDK
    never consults client-level follow_redirects — it origin-pins redirects
    itself (same-origin 30x followed transparently, cross-origin refused).
    The client-level flag is inert for MCP traffic; the origin pinning is
    the control that holds.

    timeout_s=None = the full upstream timeout (model-paced tool calls);
    metadata callers (catalogs) pass a short bound — a hung server must not
    park the caller for 600s (the "frozen console/chat" incident).
    """
    eff_timeout = timeout_s if timeout_s is not None else settings.upstream_timeout_s
    # Same upstream-TLS contract as the model router (config.py): verify=True
    # unconditional; the site CA bundle rides when configured.
    verify: str | bool = settings.upstream_ca_bundle or True
    async with (
        httpx2.AsyncClient(
            timeout=httpx2.Timeout(eff_timeout, connect=10.0),
            follow_redirects=False,
            headers=headers,
            verify=verify,
        ) as http,
        streamable_http_client(url, http_client=http) as (read, write),
    ):
        session = ClientSession(read, write, read_timeout_seconds=eff_timeout)
        async with session:
            await session.initialize()
            return await operation(session)


async def _fetch_tools(url: str, headers: dict[str, str], timeout_s: float | None = None) -> list[dict[str, Any]]:
    """Live list_tools from one upstream server.

    Returns [{"tool", "description", "inputSchema"?, "annotations"?}].
    inputSchema (the MCP Tool.inputSchema JSON Schema, when the server
    declares one) rides along for the approval-passthrough path: forwarded
    tool calls are executed by the CLIENT against the real server, so the
    model needs the real argument schema, not a generic {"type":"object"}.
    annotations (read_only_hint/destructive_hint/... — dumped to plain JSON)
    ride along so the flat per-tool surface can re-expose them verbatim.
    Both keys are ADDITIVE: the relay catalog and /mcp catalog consumers
    read the keys they know and ignore the rest.
    """

    async def do(session: ClientSession) -> list[dict[str, Any]]:
        result = await session.list_tools()
        out: list[dict[str, Any]] = []
        for tool in result.tools:
            entry: dict[str, Any] = {"tool": tool.name, "description": tool.description or ""}
            # Attribute spelling depends on the installed SDK generation: the
            # current mcp_types uses snake_case (input_schema) with camelCase
            # aliases only on the WIRE, so reading 'inputSchema' off the
            # object silently yielded None and DROPPED every upstream schema
            # (the relay's approval passthrough then sent a generic
            # {"type":"object"}). Read snake_case first, camelCase as the
            # older-SDK fallback.
            schema = getattr(tool, "input_schema", None) or getattr(tool, "inputSchema", None)
            if isinstance(schema, dict):
                entry["inputSchema"] = schema
            annotations = getattr(tool, "annotations", None)
            if annotations is not None:
                entry["annotations"] = annotations.model_dump(mode="json", exclude_none=True)
            out.append(entry)
        return out

    return await _upstream_session(url, headers, do, timeout_s=timeout_s)


# ---------------------------------------------------------------------------
# Per-server upstream listing cache (TTL, key-independent)
# ---------------------------------------------------------------------------

_LISTING_TIMEOUT_S = 15.0
"""Metadata-fetch bound (the catalog audit): a hung upstream must not park
discovery — or now the flat tools/list — for the full 600 s upstream timeout."""

_tools_listing_cache: dict[tuple[str, ...], tuple[float, list[dict[str, Any]]]] = {}


def _listing_cache_key(server: dict[str, Any]) -> tuple[str, ...]:
    """Identity of everything a listing REQUEST depends on (name/url/headers/
    credential state) — deliberately NOT the calling key: the cached payload
    is the KEY-INDEPENDENT raw upstream listing; per-key ACL filtering stays
    per-request, after the cache."""
    return (
        str(server.get("name")),
        str(server.get("url")),
        json.dumps(sorted((server.get("headers") or {}).items()), sort_keys=True, default=str),
        str(server.get("auth_secret") or ""),
        str(server.get("auth_token_enc") or ""),
    )


def listing_cache_clear() -> None:
    """Drop the whole listing cache.

    Registered as a registry-change hook (core.registry.on_mcp_registry_
    change): every admin mutation of the MCP registry funnels through
    registry.refresh(), which fires the hook — the cache is DERIVED state of
    the registry snapshot and must never outlive it. routes_admin never
    reaches into this module's cache directly.
    """
    _tools_listing_cache.clear()


def _peek_cached_tools(server: dict[str, Any]) -> list[dict[str, Any]] | None:
    """Cached listing WITHOUT triggering a fetch (stale entries included).

    Failover eligibility only: a candidate is tried when its last known
    listing exposes the tool. Stale-on-purpose — the primary has already
    failed; a fresh fan-out to probe candidates would turn one dead server
    into a fleet-wide latency spike, and a stale candidate that no longer
    exposes the tool simply reports a real upstream error.
    """
    entry = _tools_listing_cache.get(_listing_cache_key(server))
    return None if entry is None else entry[1]


async def _cached_upstream_tools(server: dict[str, Any]) -> list[dict[str, Any]]:
    """Raw upstream list_tools for one server, TTL-cached.

    - Payload is KEY-INDEPENDENT (design doc §5): shared by tools/list and
      mcp_catalog; per-key ACL filtering happens per request, after the
      fetch. Listings authenticate with the SERVER-level headers only —
      per-key overrides never apply to discovery.
    - TTL: settings.mcp_catalog_cache_s (monotonic clock).
    - Stale-on-error (chosen, marked): on a fetch failure with ANY cached
      entry present, serve the STALE listing (logged) — discovery
      availability beats freshness; without an entry the error propagates
      and the caller skips the server with an error entry.
    """
    cache_key = _listing_cache_key(server)
    now = time.monotonic()
    entry = _tools_listing_cache.get(cache_key)
    if entry is not None and entry[0] > now:
        return entry[1]
    headers = build_mcp_headers(
        server.get("headers") or {},
        server.get("auth_secret"),
        server.get("auth_token_enc"),
    )
    try:
        tools = await _fetch_tools(str(server["url"]), headers, timeout_s=_LISTING_TIMEOUT_S)
    except Exception:
        if entry is not None:
            log.warning("mcp listing cache: upstream %r failed — serving stale listing", server.get("name"))
            return entry[1]
        raise
    _tools_listing_cache[cache_key] = (now + settings.mcp_catalog_cache_s, tools)
    return tools


# ---------------------------------------------------------------------------
# Flat per-tool naming: sanitize + resolve (design doc §2/§4)
# ---------------------------------------------------------------------------

_FLAT_SEP = "__"
_FLAT_NAME_MAX = 64  # MCP tool-name grammar: [a-zA-Z0-9_-]{1,64}
_FLAT_SUFFIX_LEN = 8
_MCP_NAME_STRIP_RE = re.compile(r"[^A-Za-z0-9_-]")

# sanitized flat name -> (server, tool). See _remember_sanitized for the
# lifecycle; the primary parse path is longest-prefix against the CURRENT
# registry, this map is only the fallback for names sanitization mangled.
_sanitized_flat_names: dict[str, tuple[str, str]] = {}


def flat_tool_name(server: str, tool: str) -> str:
    """Deterministic flattened MCP tool name for one upstream (server, tool).

    MCP tool names must match [a-zA-Z0-9_-]{1,64}; server names may contain
    dots (and tools anything), so disallowed characters are replaced with
    "_". Names longer than 64 chars are truncated to 56 and suffixed with an
    8-char sha256 of the FULL raw name — deterministic (same input, same
    output across replicas/restarts), collision-proof for practical
    purposes, and never ambiguous because server names are unique.

    Mapping lifecycle (design doc §4): every flat tools/list records the
    sanitized -> (server, tool) pairs it produced (see
    _remember_sanitized); resolution tries longest-prefix against the live
    registry FIRST and only falls back to the map — a sanitized name stays
    resolvable even after a registry change reshuffled the map, as long as
    either the raw server prefix or a recorded entry survives.
    """
    raw = f"{server}{_FLAT_SEP}{tool}"
    sanitized = _MCP_NAME_STRIP_RE.sub("_", raw)
    if len(sanitized) > _FLAT_NAME_MAX:
        digest = hashlib.sha256(raw.encode()).hexdigest()[:_FLAT_SUFFIX_LEN]
        sanitized = sanitized[: _FLAT_NAME_MAX - _FLAT_SUFFIX_LEN] + digest
    return sanitized


def _remember_sanitized(mapping: dict[str, tuple[str, str]]) -> None:
    """Record one listing's sanitized-name mappings (MERGE, not replace).

    Lifecycle (design doc §4): extended by every flat tools/list; the map
    exists so parsing stays reversible for names sanitization mangled.
    Merging (instead of swapping to only the last listing) keeps a
    concurrent key's names resolvable — two keys list different grant sets,
    and a name either key listed must stay callable for that key. A stale
    entry is harmless: it can only resolve into the gate chain's registry
    lookup + ACL check, which refuse anything the current state no longer
    admits. The only true ambiguity (two servers whose names collide AFTER
    sanitization, e.g. 'a-b' vs 'a_b') resolves last-write-wins; operators
    should avoid such names — server names are unique raw, not necessarily
    post-sanitization.
    """
    _sanitized_flat_names.update(mapping)


def resolve_flat_name(name: str, server_names: list[str]) -> tuple[str, str]:
    """Split a flattened ``{server}__{tool}`` name into (server, tool).

    Resolution ORDER (design doc §2):
    1. LONGEST-PREFIX match against the CURRENT enabled registry server
       names — server names are unique and tool names may themselves
       contain ``__``, so the split point is the longest server name whose
       ``<name>__`` prefix covers the whole string;
    2. fallback to the sanitization map (for names whose raw form was
       mangled, e.g. dots replaced).

    No match -> ToolError: the caller surfaces it as a tool-error
    CallToolResult (never an HTTP error — streamable-http clients expect a
    JSON-RPC result for tools/call).
    """
    for server in sorted(server_names, key=len, reverse=True):
        prefix = server + _FLAT_SEP
        if name.startswith(prefix) and len(name) > len(prefix):
            return server, name[len(prefix) :]
    hit = _sanitized_flat_names.get(name)
    if hit is not None:
        return hit
    raise ToolError(f"unknown tool {name!r}: not a '<server>__<tool>' name in the current registry")


# ---------------------------------------------------------------------------
# The two gateway-level tools
# ---------------------------------------------------------------------------


async def _mcp_catalog() -> str:
    """Catalog of the calling key's visible MCP tools across enabled servers.

    For each enabled registry server (PRIORITY-ORDERED: priority ASC, name
    ASC — the fleet failover order): fetch its (TTL-cached) tool list, then
    filter the union through the key's ACL grants (default-deny). A server
    that is down, slow, or misconfigured is SKIPPED with a warning entry —
    never a raised error — so one dead upstream cannot hide the rest of the
    catalog.

    Returns:
        JSON string: {"servers": [{"server", "url", "tools": [{"tool",
        "description"}...]} | {"server", "error"}], "granted": [{"server",
        "tool", "description"}...]} — each server's "tools" list carrying
        ONLY the (server, tool) pairs the calling key holds (REMEDIATION
        11c; see the per-server filter below).
    """
    key = _current_key()
    servers = await registry.mcp_servers()  # priority-ordered (priority ASC, name ASC)
    grants = await load_grants(key.id)
    # REMEDIATION 11c closure — granted-tool sets per server, derived once
    # from the key's grant pairs. The raw upstream listing is
    # KEY-INDEPENDENT (the TTL cache payload is shared fleet-wide), so the
    # per-key ACL filter MUST be applied per request, after the fetch:
    # both to the flat `catalog` union (through visible_tools below) and to
    # each server's "tools" list in out_servers — the latter previously
    # shipped the server's FULL raw inventory to any key holding a single
    # grant on it (inventory disclosure; calls were already gated).
    granted_by_server: dict[str, set[str]] = {}
    for g_server, g_tool in grants:
        granted_by_server.setdefault(g_server, set()).add(g_tool)
    out_servers: list[dict[str, Any]] = []
    catalog: list[dict[str, str]] = []
    # Perf (audit P2-2): the fetch stays UNCONDITIONAL over every enabled
    # server (deliberate, test-pinned: the 15 s metadata bound is exercised
    # fleet-wide and the TTL cache warms for all servers, not just this
    # key's grants) — but the fan-out is now CONCURRENT (asyncio.gather;
    # independent bounded fetches) instead of serial. A cold cache costs
    # max(per-server bound) instead of N x bound.
    fetches = [_cached_upstream_tools(server) for server in servers]
    listings = await asyncio.gather(*fetches, return_exceptions=True)
    for server, listing in zip(servers, listings):
        granted_tools = granted_by_server.get(server["name"]) or set()
        if isinstance(listing, BaseException):
            # Skip-and-warn: catalog availability must not depend on the
            # healthiest denominator of upstreams. REDACTED: raw exception
            # text can carry internal hostnames — tenants get a stable
            # marker, the detail goes to the log only.
            log.warning("mcp_catalog: server %r unavailable: %s", server["name"], type(listing).__name__)
            out_servers.append({"server": server["name"], "error": "unavailable"})
            continue
        tools = listing
        # REMEDIATION 11c closure: per-server "tools" carries ONLY the
        # granted (server, tool) pairs. The key holds ≥1 grant on this
        # server (else the entry is dropped below), so the server and its
        # URL — which operators may need for debugging — stay visible, but
        # the rest of its raw inventory does not (it used to: any single
        # grant exposed the full tool listing + the in-cluster URL).
        granted_tools_here = [tool for tool in tools if tool["tool"] in granted_tools]
        catalog.extend(
            {
                "server": server["name"],
                "tool": tool["tool"],
                "description": tool["description"],
            }
            for tool in granted_tools_here
        )
        out_servers.append({"server": server["name"], "url": server["url"], "tools": granted_tools_here})
    granted = visible_tools(catalog, grants)
    # Tenant-visible inventory is ACL-FILTERED: servers/URLs the key has no
    # grant on are dropped entirely (previously the full fleet inventory +
    # internal URLs leaked to any mcp-scoped key). A server with ZERO
    # granted tools therefore never appears in "servers" at all.
    granted_servers = {g_server for g_server, _ in grants}
    visible_servers = [sv for sv in out_servers if sv["server"] in granted_servers]
    return json.dumps({"servers": visible_servers, "granted": granted}, indent=2)


async def _settle_quiet(
    key_id: uuid.UUID,
    endpoint: str,
    status: str,
    started: float,
    request_id: str,
) -> None:
    """Best-effort ledger settle for one gated call attempt (ledger truth:
    refusals and errors are recorded like real calls, but a settle failure
    must never mask the caller's actual result — swallowed + logged)."""
    try:
        await settle(key_id, "*", endpoint, 0, 0, status, int((time.monotonic() - started) * 1000), request_id)
    except Exception:
        log.exception("mcp: settle failed (%s %s)", status, endpoint)


async def _gated_mcp_call(
    server: str,
    tool: str,
    arguments: dict[str, Any],
    *,
    failover: bool = False,
) -> str:
    """The ONE gate chain for upstream tool calls — the choke point.

    Both surfaces funnel through here: the ``mcp_call`` gateway tool
    (failover=False — the caller NAMED the server, so it never silently
    moves) and flattened ``{server}__{tool}`` calls (failover=True — a
    transport-dead primary hands off to the next priority-ordered eligible
    server, see ``_failover_candidates``).

    Chain, in order: explicit validation → RPM rate gate (once per request)
    → registry lookup → per-key ACL (default-deny) → server tool-policy →
    proxied call → ledger settle per attempt under its actual
    ``mcp:<server>:<tool>`` endpoint. ALL refusals raise ToolError so the
    SDK turns them into is_error CallToolResults (not HTTP errors —
    streamable-http clients expect a JSON-RPC result for tools/call).

    Returns:
        JSON string {"ok": bool, "server", "tool", ...} — on upstream tool
        error or transport failure, ok=false with the upstream's error text
        (plus "attempts" when failover had more than one candidate).

    Raises:
        ToolError on validation/gate refusal.
    """
    key = _current_key()
    started = time.monotonic()
    request_id = f"mcp-{uuid.uuid4()}"

    # --- validation (defensive; the JSON schema already narrows most) ---
    arguments = _unwrap_stringy_arguments(arguments)
    if not isinstance(server, str) or not server:
        raise ToolError("invalid call: server must be a non-empty string")
    if not isinstance(tool, str) or not tool:
        raise ToolError("invalid call: tool must be a non-empty string")
    if not isinstance(arguments, dict):
        raise ToolError("invalid call: arguments must be an object")
    # Cap + sanitize the ledger endpoint string (audit finding 20): on the
    # EARLY refusal paths below, caller-chosen server/tool text lands in
    # usage_ledger.endpoint verbatim — unbounded and unfiltered otherwise.
    server = server[:200]
    tool = tool[:200]
    endpoint = f"mcp:{server}:{tool}"

    # Rate gate (RPM axis): the proxy path rate-limits every call; MCP
    # calls consume 0 tokens so token budgets are blind to them — this is
    # the only burst guard (F6, audit). Once per REQUEST, not per attempt.
    gate = await check_rate_limit(key.id, key.rate_limit_rpm, key.rate_limit_tpm, 0)
    if not gate.ok:
        await _settle_quiet(key.id, endpoint, "error", started, request_id)
        raise ToolError(f"rate limited: {gate.reason}")

    servers = await registry.mcp_servers()  # priority-ordered (priority ASC, name ASC)
    target = next((s for s in servers if s["name"] == server), None)
    if target is None:
        await _settle_quiet(key.id, endpoint, "error", started, request_id)
        raise ToolError(f"unknown server {server!r}: not in the enabled registry")

    # --- ACL gate (default-deny; denials are results, not HTTP errors) ---
    grants = await load_grants(key.id)
    if not is_allowed(grants, server, tool):
        _metrics.observe_acl_denial(server)
        await _settle_quiet(key.id, endpoint, "error", started, request_id)
        raise ToolError(f"denied: key is not granted tool {tool!r} on server {server!r}")

    # --- server tool-policy gate (defense-in-depth over the key ACL) ---
    if not server_allows(target, tool):
        _metrics.observe_acl_denial(server)
        await _settle_quiet(key.id, endpoint, "error", started, request_id)
        raise ToolError(
            f"denied: {server!r} policy is '{target.get('tool_policy')}' and tool {tool!r} is not in its allowlist"
        )

    # --- candidates: primary first; flattened calls may fail over ---
    candidates: list[dict[str, Any]] = [target]
    if failover:
        candidates.extend(await _failover_candidates(servers, server, tool, grants))

    transport_errors: list[str] = []
    for candidate in candidates:
        candidate_name = str(candidate["name"])
        # Each attempt settles under its ACTUAL endpoint — a failover hit is
        # billed/audited against the server that really served it.
        candidate_endpoint = f"mcp:{candidate_name}:{tool}"
        call_headers = await resolve_mcp_headers(candidate, key_id=key.id)

        async def do(session: ClientSession) -> Any:
            return await session.call_tool(tool, arguments)

        try:
            result = await _upstream_session(candidate["url"], call_headers, do)
        except Exception as exc:
            # Transport/protocol failure (unreachable, timeout, 5xx): settle
            # as error and — on flattened calls — hand off to the next
            # candidate. Never an HTTP error; reported INSIDE the result.
            log.warning("mcp call: upstream %r/%r failed: %s", candidate_name, tool, exc)
            await _settle_quiet(key.id, candidate_endpoint, "error", started, request_id)
            transport_errors.append(f"{candidate_name}: {type(exc).__name__}")
            continue

        if result.is_error:
            # Upstream tool-error results are REAL ANSWERS (the tool ran and
            # refused/failed) — never a failover trigger.
            await _settle_quiet(key.id, candidate_endpoint, "error", started, request_id)
            return json.dumps(
                {
                    "ok": False,
                    "server": candidate_name,
                    "tool": tool,
                    "error": "; ".join(getattr(block, "text", str(block)) for block in result.content),
                },
                indent=2,
            )
        await _settle_quiet(key.id, candidate_endpoint, "ok", started, request_id)
        return json.dumps(
            {
                "ok": True,
                "server": candidate_name,
                "tool": tool,
                "content": [block.model_dump(mode="json", exclude_none=True) for block in result.content],
            },
            indent=2,
        )

    # Every attempt transport-failed. Single-candidate shape is unchanged
    # from the pre-failover contract; multi-candidate adds "attempts".
    payload: dict[str, Any] = {
        "ok": False,
        "server": server,
        "tool": tool,
        "error": f"upstream call failed: {transport_errors[-1].split(': ', 1)[-1]}"
        if transport_errors
        else "upstream call failed",
    }
    if len(candidates) > 1:
        payload["attempts"] = transport_errors
    return json.dumps(payload, indent=2)


async def _failover_candidates(
    servers: list[dict[str, Any]],
    primary: str,
    tool: str,
    grants: set[tuple[str, str]],
) -> list[dict[str, Any]]:
    """Priority-ordered failover candidates for a FLATTENED call whose
    primary transport failed. Eligibility — ALL three, so default-deny
    survives failover (no grant, no candidate):
    (a) the candidate's CACHED listing exposes the same tool name — cache
        peek only, never a fresh fetch: one dead server must not become a
        fleet-wide fan-out;
    (b) the calling key holds an explicit (server, tool) grant;
    (c) the candidate's tool_policy admits the tool.

    Bounded by settings.mcp_failover_attempts retries AFTER the primary.
    Explicit ``mcp_call(server, tool)`` targets never get here (the caller
    named the server — documented asymmetry).
    """
    out: list[dict[str, Any]] = []
    for candidate in servers:  # already priority-ordered
        if len(out) >= max(0, settings.mcp_failover_attempts):
            break
        if candidate["name"] == primary:
            continue
        if not is_allowed(grants, candidate["name"], tool):
            continue
        if not server_allows(candidate, tool):
            continue
        cached = _peek_cached_tools(candidate)
        if cached is None or not any(entry.get("tool") == tool for entry in cached):
            continue
        out.append(candidate)
    return out


async def _mcp_call(server: str, tool: str, arguments: dict[str, Any]) -> str:
    """Proxy one tool call to one upstream MCP server, gated by ACLs — the
    explicit-name compat surface (sproket et al.).

    Thin wrapper over the shared gate chain (``_gated_mcp_call``) with
    failover DISABLED: the caller named this server explicitly, so a
    transport failure is reported as-is rather than silently moving the
    call to another server (the documented failover asymmetry).

    Returns:
        JSON string {"ok": bool, "server", "tool", ...} — see
        ``_gated_mcp_call``.

    Raises:
        ToolError on validation/ACL refusal so the SDK turns it into an
        is_error CallToolResult (verified against the installed SDK).
    """
    return await _gated_mcp_call(server, tool, arguments, failover=False)


# ---------------------------------------------------------------------------
# Flat per-tool surface: the MCPServer subclass (design doc §1/§3)
# ---------------------------------------------------------------------------

_GATEWAY_TOOLS = frozenset({"mcp_catalog", "mcp_call"})


def _unwrap_stringy_arguments(arguments: Any) -> Any:
    """Undo the ONE pathological client-side double-encoding: a call whose
    entire arguments object is a single 'arguments' key carrying either a
    JSON string or an object — the exact shape observed live from a harness
    that stringifies the model's arguments and re-wraps them (fleet pydantic
    models then reject it as missing-field, or silently ignore the payload
    when every field is defaulted).

    Unwraps exactly one level, ONLY for the unambiguous pathological shape:
    {'arguments': '<json-string>'} where the string parses to a JSON OBJECT.
    Everything else passes through untouched — in particular a dict payload
    or a non-JSON string, because a compliant upstream tool may legitimately
    own a parameter named 'arguments' (e.g. a meta-tool or a code runner
    taking raw source); guessing there would rewrite valid calls. A wrapped
    but non-JSON string raises the upstream's own missing-field error so
    misshapen calls stay visible instead of being masked.

    Returns the original object when no rule fires — the input is NEVER
    mutated.
    """
    if (
        not settings.mcp_unwrap_arguments
        or not isinstance(arguments, dict)
        or set(arguments.keys()) != {"arguments"}
        or not isinstance(arguments["arguments"], str)
    ):
        return arguments
    try:
        decoded = json.loads(arguments["arguments"])
    except (json.JSONDecodeError, ValueError):
        return arguments  # wrapped-but-not-JSON: let the upstream error on it
    if isinstance(decoded, dict):
        return decoded
    return arguments  # JSON list/str/num: arity-changing, refuse to guess


def _tool_annotations(dump: dict[str, Any] | None) -> ToolAnnotations | None:
    """Rebuild ToolAnnotations from a _fetch_tools dump — degrade-not-fail:
    unparsable upstream hints are DROPPED (annotations are advisory), never
    allowed to fail the listing."""
    if not dump:
        return None
    try:
        return ToolAnnotations(**dump)
    except Exception:
        log.warning("flat tools/list: unparsable upstream annotations — dropping them")
        return None


class _GatewayMCPServer(MCPServer):
    """MCPServer subclass serving tools/list AND tools/call dynamically per
    request — the extension point for the flat per-tool surface.

    SDK evidence (inspected in the installed distribution,
    mcp/server/mcpserver/server.py): ``MCPServer.__init__`` wires the
    lowlevel Server with ``on_list_tools=self._handle_list_tools`` and
    ``on_call_tool=self._handle_call_tool``; ``_handle_list_tools`` awaits
    ``self.list_tools()`` and ``_handle_call_tool`` awaits ``self.call_tool(
    name, arguments, context)``. Overriding exactly these two async methods
    therefore serves per-request, per-key behavior WITHOUT touching the SDK
    and without re-registering handlers. In json_response mode the handler
    runs inline in the ASGI request task (mcp/server/_streamable_http_modern
    .py ``handle_modern_request``), so the auth wrapper's ``_caller``
    contextvar is visible here; tests/test_mcp_flat.py pins that with an
    HTTP round trip (the flat listing can only appear when the caller IS
    visible inside the SDK's dispatch path).

    Refusals inside call_tool raise ToolError, which the SDK's
    ``_handle_call_tool`` converts into an is_error CallToolResult — a
    tool-error RESULT, never an HTTP error (streamable-http contract).
    """

    async def list_tools(self) -> list[MCPTool]:
        """The two gateway tools + (flat mode) this key's granted flattened
        upstream tools. Fail-safe: any failure computing the flat layer
        degrades to the gateway tools — a broken OPTIONAL surface must never
        break discovery."""
        base = await super().list_tools()
        if not settings.mcp_flat_tools:
            return base
        try:
            return base + await self._flat_tools()
        except Exception:
            log.exception("flat tools/list failed — serving the gateway tools only")
            return base

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        context: Context[Any, Any] | None = None,
    ) -> CallToolResult | InputRequiredResult:
        """Route one tools/call: flattened names through the shared gate
        chain (with failover), gateway tool names through the SDK's
        ToolManager as before. With the kill switch off, EVERY name takes
        the legacy path — flattened names then produce the same
        unknown-tool ToolError as before the flat surface existed."""
        if settings.mcp_flat_tools and name not in _GATEWAY_TOOLS:
            servers = await registry.mcp_servers()
            server, tool = resolve_flat_name(name, [s["name"] for s in servers])
            result = await _gated_mcp_call(server, tool, arguments, failover=True)
            return CallToolResult(content=[TextContent(type="text", text=result)])
        return await super().call_tool(name, arguments, context)

    async def _flat_tools(self) -> list[MCPTool]:
        """The calling key's granted tools as individual MCP tools.

        Per-key ACL filter applied AFTER the cached per-server listing is
        fetched; a server with ZERO grants for this key contributes NO tools
        and NO URL anywhere (it is not even fetched). Same default-deny
        discipline as the catalog's granted_servers filter."""
        key = _current_key()
        grants = await load_grants(key.id)
        if not grants:
            # default-deny fast path: zero grants -> ZERO upstream fetches,
            # zero URLs, zero tool names.
            return []
        granted_servers = {s for s, _ in grants}
        mapping: dict[str, tuple[str, str]] = {}
        out: list[MCPTool] = []
        for server in await registry.mcp_servers():  # priority-ordered
            if server["name"] not in granted_servers:
                continue
            try:
                tools = await _cached_upstream_tools(server)
            except Exception as exc:
                # skip-with-error, same discipline as _mcp_catalog: a dead
                # upstream must not break discovery. (The catalog JSON's
                # error entry has no tools/list equivalent — the skip is
                # silent to the client, loud in the logs.)
                log.warning("flat tools/list: server %r unavailable: %s", server["name"], exc)
                continue
            for entry in tools:
                tool_name = entry["tool"]
                if (server["name"], tool_name) not in grants:
                    continue  # per-tool ACL filter, after the cached fetch
                flat = flat_tool_name(server["name"], tool_name)
                mapping[flat] = (server["name"], tool_name)
                out.append(
                    MCPTool(
                        name=flat,
                        description=entry.get("description") or None,
                        input_schema=entry.get("inputSchema") or {"type": "object"},
                        annotations=_tool_annotations(entry.get("annotations")),
                    )
                )
        _remember_sanitized(mapping)
        return out


# ---------------------------------------------------------------------------
# Server construction + mount wiring
# ---------------------------------------------------------------------------


def _new_gateway_server() -> _GatewayMCPServer:
    """Fresh gateway MCPServer with the two compat tools registered.

    Production calls this exactly once at import (``_mcp`` below). Tests call
    it per test: the SDK's StreamableHTTPSessionManager refuses run() twice
    on one instance and each TestClient lifespan runs it, so a shared
    singleton would make the second HTTP-level test fail.
    """
    server = _GatewayMCPServer(
        "pcai-llm-gateway",
        title="PCAI LLM Gateway MCP",
        instructions=(
            "Federated MCP surface for this gateway key. tools/list exposes "
            "your granted upstream tools directly as individual "
            "'<server>__<tool>' entries. NAME RESOLUTION: a client harness "
            "may namespace these under its own connection prefix (e.g. "
            "'mcp__<gateway-connection>__<server>__<tool>') — CALL THE NAME "
            "EXACTLY AS YOUR CLIENT'S TOOL LIST SHOWS IT, never the bare "
            "'<server>__<tool>' form learned from docs or memory; if a call "
            "errors 'unknown tool', retry with the client-prefixed form "
            "before anything else. mcp_catalog lists the same inventory "
            "with server URLs and "
            "mcp_call(server, tool, arguments) invokes a tool by explicit "
            "name (compat surface for off-cluster consumers; an explicit "
            "target never silently fails over, while a flat call retries "
            "the next priority-ordered enabled server that also exposes the "
            "tool when the primary is unreachable). EVERY call — flat or "
            "explicit — passes one gate chain: per-key ACL (default-deny), "
            "server tool policy, RPM limit, ledger settle. Operators: "
            "PCAI_LLM_MCP_FLAT_TOOLS=0 reverts to the two-tool surface; "
            "listings are TTL-cached (PCAI_LLM_MCP_CATALOG_CACHE_S) and "
            "invalidated on registry changes."
        ),
    )
    server.add_tool(
        _mcp_catalog,
        name="mcp_catalog",
        title="MCP Catalog",
        description=(
            "List the MCP tools this key may call, per enabled server in "
            "fleet priority order, filtered by the key's per-tool ACL "
            "grants (default-deny). Unreachable servers appear with an "
            "error entry instead of failing the call."
        ),
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
    )
    server.add_tool(
        _mcp_call,
        name="mcp_call",
        title="MCP Call",
        description=(
            "Invoke one tool on one upstream MCP server by explicit name. "
            "Requires an explicit (server, tool) grant for the calling key; "
            "denials come back as tool-error results. An explicit target is "
            "NEVER silently failed over — transport failures are reported "
            "as-is (flat <server>__<tool> calls are the failover surface)."
        ),
        annotations=ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=True),
    )
    return server


_mcp = _new_gateway_server()

# Registry-change invalidation (see listing_cache_clear): routes_mcp is
# imported by app.py at factory time, so the hook is registered before any
# request can be served, and every admin registry mutation (which all await
# registry.refresh()) drops the listing cache.
registry.on_mcp_registry_change(listing_cache_clear)


def mount_server(app: FastAPI, server: MCPServer) -> None:
    """Attach one gateway server's authenticated ASGI app to a FastAPI app.

    Stateless, JSON-only streamable-http transport (fleet convention: any
    replica serves any request; no Mcp-Session-Id tracking). DNS-rebinding
    protection is disabled deliberately: this app sits behind the gateway's
    own bearer auth on every path (there is no browser-origin surface to
    protect). Split from mount() so tests can mount a FRESH server instance
    (see _new_gateway_server) while production wiring stays mount().
    """
    http_app = server.streamable_http_app(
        json_response=True,
        stateless_http=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    @asynccontextmanager
    async def session_manager_lifespan(_app: FastAPI) -> AsyncIterator[None]:
        """Run the MCP session manager's task group for the app's lifetime.

        Starlette never runs lifespans of MOUNTED apps, and FastAPI's custom
        lifespan replaces (not merges) the default one — so mount merges
        this context into app.router.lifespan_context explicitly.
        """
        async with server.session_manager.run():
            yield

    # Auth-first wrapper: every request is authenticated (401) / authorized
    # (403) before the MCP transport sees it.
    app.router.routes.append(Route("/mcp", _McpAuthASGI(http_app), name="mcp"))
    app.router.lifespan_context = _merge_lifespan_context(app.router.lifespan_context, session_manager_lifespan)


def mount(app: FastAPI) -> None:
    """Attach the authenticated /mcp ASGI app to the gateway FastAPI app.

    app.py calls ``routes_mcp.mount(app)`` once at factory time (verified
    signature: ``from pcai_llm.api.routes_mcp import mount as mount_mcp``).
    """
    mount_server(app, _mcp)
