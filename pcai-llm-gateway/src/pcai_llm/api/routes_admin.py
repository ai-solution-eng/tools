"""Admin control-plane API: tenants, users, keys, budgets, endpoint pools, MCP registry.

ADMIN AUTH DESIGN
-----------------
Admin keys are operator-provisioned via a K8s Secret (the chart injects
``PCAI_LLM_admin_api_keys``); they are NEVER DB rows, so a database
compromise cannot escalate into admin access. Auth compares in HASH
space: the incoming bearer is SHA-256'd and checked against the SHA-256
set of the configured keys. Reasons:

- the same Secret mechanism / rotation convention as data-plane keys
  applies (hash-compare keeps the admin path identical in shape);
- comparing digests avoids accidentally logging or echoing a live
  plaintext admin key through exception messages or debug dumps;
- digest comparison over a fixed-size set sidesteps the timing-signal
  pitfalls of comparing variable-length secret strings directly.

Fail closed: an EMPTY ``admin_api_keys`` config disables the whole admin
surface with 503 (the applygate lesson — a missing secret must never
look like "allow all").
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import socket
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal
from urllib.parse import urlparse

import asyncpg
import httpx2
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..config import settings
from ..core import costs, registry, secretbox, url_guard
from ..core import metrics as _metrics
from ..core.audit import audit_action, stable_actor_label
from ..core.budgets import (
    MAX_LIMIT_TOKENS,
    MAX_LIMIT_USD,
    budget_status,
    budget_status_many,
    split_preset_budgets,
)
from ..core.identity import (
    IDP_ADMIN_ASSERTED,
    VIA_ADMIN_CONSOLE,
    bind_key_subject,
    browser_identity_from_headers,
    normalize_subject,
)
from ..core.keys import generate_key, hash_key, list_keys, revoke_key
from ..db import db
from ..mcpf.federation import (
    MCP_SERVER_CLASSES,
    load_live_profile_grants,
    preset_catalog_pairs,
    preset_pairs,
)
from ..routing.router import Endpoint
from ..routing.router import router as model_router

log = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])

VALID_SCOPES = {"proxy", "mcp", "admin"}
VALID_PERIODS = {"day", "week", "month"}
MAX_LIST_LEN = 100  # reject absurd payloads early, before they hit the DB
# Bounded registration-time DNS lookup for the SSRF gate (see _check_url):
# a wedged resolver must not hang an admin request. A lookup that does not
# finish in time is a resolution FAILURE, which warns and skips the
# resolved-address check (nothing was SHOWN dangerous) rather than refusing.
_URL_RESOLVE_TIMEOUT_S = 5.0


# ---------------------------------------------------------------------------
# admin auth dependency
# ---------------------------------------------------------------------------


def _admin_key_hashes() -> set[str]:
    """Parse the comma-separated operator-provisioned admin keys into a hash set."""
    return {hash_key(k.strip()) for k in settings.admin_api_keys.split(",") if k.strip()}


def _admin_group_set() -> set[str]:
    """The configured admin groups (comma-separated env) as a set."""
    return {g.strip() for g in settings.admin_groups.split(",") if g.strip()}


def _browser_admin_identity(request: Request) -> list[str] | None:
    """The browser identity's groups when the request carries a TRUSTED
    browser identity (rung 1: PCAI_LLM_TRUST_PROXY_IDENTITY + identity
    headers present), else None. Pure helper for require_admin's second
    path; the group match itself happens at the call site."""
    from ..core.identity import settings_trust_proxy_identity

    if not settings_trust_proxy_identity():
        return None
    browser = browser_identity_from_headers(dict(request.headers))
    if browser is None:
        return None
    return browser[1]


async def require_admin(request: Request) -> None:
    """Dependency: 503 when the admin API is disabled, else hash-set bearer check.

    WHY hash-compare: admin keys arrive via K8s Secret as plaintext config;
    hashing the presented key and comparing against precomputed digests means
    plaintext never participates in a comparison path that could be timed or
    leaked, and mirrors the data-plane key storage convention exactly.

    Phase B — the SECOND path (review §1.5): a TRUSTED browser identity
    (PCAI_LLM_TRUST_PROXY_IDENTITY=true + the oauth2-proxy headers) whose
    X-Forwarded-Groups intersects PCAI_LLM_ADMIN_GROUPS also authenticates.
    DEFAULT OFF (empty admin_groups): the gate is then byte-for-byte
    today's bearer-only check. The group path rides the SAME deployment
    precondition as every browser-rung feature (enforcing proxy + Istio
    AuthorizationPolicy — the core/identity.py docstring), and the DIRECT
    CALLER PATH (bearer admin key) is untouched and still works first:
    a request carrying a valid bearer admin key never reaches the group
    check.
    """
    if not settings.admin_api_keys.strip():
        raise HTTPException(503, "admin API disabled")
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        presented = hash_key(auth[7:].strip())
        if presented in _admin_key_hashes():
            return
        # A bearer that FAILED the admin-hash check: refuse. Deliberately
        # NO fall-through to the group path — a caller presenting admin
        # key material is a direct caller; group trust must not launder a
        # rejected key (and header assertions are the proxy's to make,
        # never the bearer's).
        log.warning("admin auth failed: invalid admin key on %s", request.url.path)
        _metrics.admin_auth_failure("invalid-key")
        raise HTTPException(401, "invalid admin key")
    # no bearer: the browser-identity group path (when CONFIGURED — empty
    # admin_groups = the path does not exist, the request is the same
    # missing-bearer 401 it has always been).
    admin_groups = _admin_group_set()
    if admin_groups:
        groups = _browser_admin_identity(request)
        if groups is not None:
            if admin_groups.intersection(groups):
                return
            log.warning(
                "admin auth failed: browser identity groups %s not in admin_groups on %s",
                groups,
                request.url.path,
            )
            _metrics.admin_auth_failure("group-denied")
            raise HTTPException(403, "identity is not in an admin group")
    # no key material in the log — just the fact + path (brute-force
    # telemetry deferred to an in-process counter; this is the visible
    # minimum the audit asked for)
    log.warning("admin auth failed: missing bearer key on %s", request.url.path)
    _metrics.admin_auth_failure("missing-key")
    raise HTTPException(401, "missing admin bearer key")


def warn_short_admin_keys() -> None:
    """Startup telemetry (deferred-item minimum): flag configured admin keys
    shorter than 32 chars — a brute-force surface. Counts/lengths only,
    never key material; does NOT refuse boot (breaking change)."""
    keys = [k.strip() for k in settings.admin_api_keys.split(",") if k.strip()]
    short = [len(k) for k in keys if len(k) < 32]
    if short:
        log.warning(
            "%d configured admin key(s) are shorter than 32 chars (lengths: %s) "
            "— a brute-force surface; provision longer keys",
            len(short),
            short,
        )


AdminDep = Annotated[None, Depends(require_admin)]


# ---------------------------------------------------------------------------
# admin audit trail — actor/context resolution + the instrumented-write helper
# ---------------------------------------------------------------------------

AUDIT_ACTIONS_DOC = (
    "key.create key.patch key.revoke key.rotate budget.set costs.set costs.delete "
    "model.upsert model.delete model.fallbacks.set mcp.server.upsert mcp.server.patch "
    "mcp.acl.set mcp.acl.server.set mcp.acl.copy mcp.creds.set mcp.creds.delete profile.upsert "
    "profile.delete profile.limits.propagate user.profile.set tenant.create user.create "
    "guardrail.create guardrail.patch guardrail.delete guardrail.models.set offpeak.set "
    "enroll.code.mint enroll.code.revoke enroll.redeem"
)


def _request_id_of(request: Request) -> str | None:
    """The request's correlation id, resolved the same chain app.py's error
    handlers use: request.state.request_id (set by middleware when present),
    else the raw X-Request-Id header, else None. There is deliberately no
    id INVENTION here — the trail records what actually correlated the
    request; absent one, the ts+actor+action columns carry the query."""
    rid = getattr(request.state, "request_id", None)
    if rid:
        return str(rid)
    return request.headers.get("x-request-id") or None


def _source_ip_of(request: Request) -> str | None:
    """Best-effort client address for the audit row, XFF-aware (the same
    first-entry convention routes_enroll._client_ip uses for the redeem
    throttle: behind Istio the mesh-observed client is the first XFF
    entry; direct/dev traffic falls back to the TCP peer)."""
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip() or None
    return request.client.host if request.client else None


def _actor_source_of(request: Request) -> str:
    """WHICH credential kind authenticated this admin request (the
    actor_source vocabulary, mirroring require_admin's two paths):
    'admin-key' for the Secret-configured bearer, 'proxy-identity' for the
    trusted browser-group path, 'token' reserved for the Phase B Keycloak
    rung (require_admin does not accept bare tokens today; the value is
    part of the contract so a future rung audits without a vocabulary
    change). Enrollment redemption audits with 'enroll' at its call site,
    never here."""
    if request.headers.get("authorization", "").lower().startswith("bearer "):
        return "admin-key"
    if _browser_admin_identity(request) is not None:
        return "proxy-identity"
    return "token"


def _actor_subject_of(request: Request) -> str:
    """Stable actor identity for the audit row. Admin bearers are Secret
    material (no DB row): recorded as a short digest label via
    stable_actor_label — stable per key, never reversible, never the full
    hash. The proxy-identity path records the actual subject claim."""
    if request.headers.get("authorization", "").lower().startswith("bearer "):
        return stable_actor_label(request.headers.get("authorization", "")[7:].strip())
    browser = _browser_admin_identity(request)
    if browser is not None:
        return browser[0]
    return ""


async def _audit(
    request: Request,
    *,
    action: str,
    resource_type: str,
    resource_id: str,
    details: dict[str, Any] | None = None,
    actor_key_id: int | None = None,
) -> None:
    """The one call every mutating admin route makes after its write
    commits. Best-effort (core.audit.audit_action): a failed audit row is
    logged loudly and NEVER fails the route — the mutation already
    succeeded; see the module docstring tradeoff.

    The actor's credential kind / subject / request id / source ip resolve
    HERE from the FastAPI request, so each route stays a one-liner.
    ``details`` is redacted at the single writer; passing raw payloads is
    safe (and intended — redaction-at-the-writer is the enforceable point).

    conn is deliberately OMITTED (audit_action's own ``audit.db`` seam):
    the trail must never be an operation-fake's concern — tests fake the
    operation seams, the trail rides its own.
    """
    await audit_action(
        None,
        actor_key_id=actor_key_id,
        actor_subject=_actor_subject_of(request),
        actor_source=_actor_source_of(request),
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        details=details,
        request_id=_request_id_of(request),
        source_ip=_source_ip_of(request),
    )


# ---------------------------------------------------------------------------
# request / response models
# ---------------------------------------------------------------------------


class TenantCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class UserCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class KeyCreate(BaseModel):
    user_id: uuid.UUID
    name: str = Field(min_length=1, max_length=200)
    scopes: list[str] = ["proxy"]
    allowed_models: list[str] = []
    # gt=0 (audit finding 20 — KeyPatch parity): rpm/tpm of 0 or negative
    # brick the key fail-closed (check_rate_limit refuses only when the
    # counter EXCEEDS the limit, so 0 still admits ~60/min) — a typo'd 0
    # must be a 400, not a mysteriously-leaky credential. The fields also
    # feed schema CHECK-free columns, so the API is the only gate.
    rate_limit_rpm: int = Field(60, gt=0)
    rate_limit_tpm: int = Field(100_000, gt=0)
    expires_at: datetime | None = None
    # TRUE = tool relay engages without the client header (per-key default)
    relay_default: bool = False
    # TRUE = relayed streams carry the in-stream {"pcai": …} telemetry events
    # (RFC-PER-KEY-STREAM-EVENTS). DEFAULT-FLIP (consumer inventory
    # 2026-09-24): new keys default FALSE (spec-clean for unknown
    # OpenAI-compatible clients); the console form posts true explicitly,
    # enrollment (DSH path) opts in at create_key. Existing keys unchanged.
    stream_pcai_events: bool = False
    # MCP-surface admin opt-in (P0-3, 2026-10-26). DEFAULT FALSE is the
    # control: sqlhandler's admin_* verbs are refused on the MCP surface for
    # every key that does not carry this explicit flag, so no routine key
    # mint (console OR enrollment/self-mint path — core.keys.create_key does
    # not even accept the field) can ever hold credential-minting verbs.
    # Flip deliberately, for an operator tooling key only, never a
    # model-driven one.
    mcp_admin: bool = False
    # When set: attaches the profile to the user AND stamps the key with the
    # profile's preset budgets + MCP grants ("profiles as defaults").
    profile_id: uuid.UUID | None = None
    # OPTIONAL identity binding (Phase A): the admin asserts the caller's
    # stable subject ("who this key is for") — bound with idp='admin-
    # asserted', bound_via='admin-console'. Admin-asserted = verified
    # provenance (the console is an authenticated admin moment), but still a
    # human claim about the callee, so the UI marks it distinctly from SSO
    # bindings. Empty/whitespace after normalization refuses with 400.
    subject: str | None = Field(default=None, max_length=200)


class KeyCreated(BaseModel):
    id: uuid.UUID
    key: str
    key_prefix: str


class KeyRotated(BaseModel):
    """Rotate response: the NEW key's plaintext (shown ONCE — the only copy
    that will ever exist) plus both ids — the new key to hand out and the old
    (now revoked) one for audit trails. ``grants_copied`` counts the MCP tool
    ACL rows carried onto the new key in the same flow (federation grants are
    part of a key's identity and survive rotation). ``creds_copied`` counts
    the per-server MCP credential-override rows carried the same way (the
    memory-MCP dataset/password identity is also part of a key's identity —
    losing it on rotation forced users to re-enter dataset + password after
    every rotation, which the release-iteration workflow hit every tag
    bump). ``budgets_copied`` counts the budget LIMIT rows (token + USD)
    carried onto the new key server-side — before this a raw-API rotate
    left the fresh key with no budget rows at all (unlimited). The new key
    ALSO inherits the old key's spend_ancestors lineage (+ the old id), so
    in-window budget SPEND carries across rotation and rotation cannot
    reset a period budget. ``subject_id``/``bound_via`` echo the identity
    binding carried onto the new key (Phase A; NULL/'' for an unbound key)."""

    id: uuid.UUID
    key: str
    key_prefix: str
    old_key_id: uuid.UUID
    old_revoked: bool
    grants_copied: int = 0
    creds_copied: int = 0
    budgets_copied: int = 0
    subject_id: uuid.UUID | None = None
    bound_via: str = ""


class BudgetSpec(BaseModel):
    model: str = "*"
    period: str
    # exactly ONE of the two limits per entry (validated below)
    limit_tokens: int | None = None
    limit_usd: float | None = None


class BudgetsReplace(BaseModel):
    budgets: list[BudgetSpec]


class EndpointSpec(BaseModel):
    base_url: str
    priority: int = 0
    weight: int = 1
    api_key_secret: str | None = None
    # Panel-managed key: None = keep existing, "" = clear, "..." = set/replace.
    # Stored Fernet-encrypted (master key); NEVER returned by any endpoint.
    api_key: str | None = None


MODEL_TYPES = {"chat", "embedding", "reranker", "voice", "decision"}
# "decision" (2026-10-23) is the vLLM-SR System One family (d3-turbo): a
# model that answers typed questions with calibrated probabilities and
# NEVER generates text (no /v1/chat/completions, no /v1/responses). It is a
# distinct type rather than "chat" for two load-bearing reasons: a chat
# request naming it must 400 with an honest message instead of failing
# upstream, and _routing_chat_models() must never offer it to the ladder as
# a candidate -- the router picks a model to SERVE the request, and a
# decision model cannot serve one.


class ModelUpsert(BaseModel):
    model_name: str = Field(min_length=1, max_length=200)
    endpoints: list[EndpointSpec]
    # Routing type — /v1/chat only serves 'chat' pools, /v1/embeddings only
    # 'embedding' pools (a chat request must never hit an embedding endpoint).
    #
    # None = DETECT (2026-10-26). This used to default to "chat", so a caller
    # that simply omitted the field silently registered an embedder or a
    # reranker as a chat pool — the console showed "chat" for every model, and a
    # chat request could be routed to an endpoint that can only 500 on it. An
    # omitted type now means "ask the endpoint", and only a failed detection
    # falls back to "chat" (the historical behaviour, now the last resort
    # instead of the silent default).
    model_type: str | None = None


class McpServerUpsert(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    url: str
    headers: dict[str, str] = {}
    # K8s Secret NAME (by-ref) whose `password`/`api-key` content is the
    # server's Bearer token — the fleet MCP servers all require one.
    auth_secret: str | None = None
    # Panel-managed token: None = keep, "" = clear, "..." = set/replace.
    # Stored Fernet-encrypted; NEVER returned by any endpoint.
    auth_token: str | None = None
    # Tool policy: 'all' = every exposed tool grantable (the right default —
    # registering a server means its tools are grantable); 'allowlist' =
    # opt-in restriction for sensitive servers.
    tool_policy: str = "all"
    allowed_tools: list[str] = Field(default_factory=list, max_length=200)
    enabled: bool = True
    # Fleet priority (lower = preferred — the model_endpoints convention):
    # orders the registry snapshot, hence mcp_catalog / tools/list output
    # and the flattened-call failover chain.
    priority: int = 0


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _check_list_len(items: list[Any], what: str) -> None:
    """Reject oversized lists: a 10k-element upsert is a config error, not data."""
    if len(items) > MAX_LIST_LEN:
        raise HTTPException(400, f"{what} exceeds maximum length of {MAX_LIST_LEN}")


def _check_json_size(value: Any, what: str, max_bytes: int = 8192) -> None:
    """Reject oversized JSON payloads (audit finding 20): an unbounded
    headers dict or guardrail config blob round-trips into the registry
    snapshot, every admin GET, the audit details, and the CSV export —
    an MB-scale blob is amplification available to admin-key holders only,
    but it is free to refuse."""
    size = len(json.dumps(value, default=str).encode("utf-8", "replace"))
    if size > max_bytes:
        raise HTTPException(400, f"{what} serializes to {size} bytes — exceeds the {max_bytes}-byte cap")


async def _refresh_registry_best_effort() -> None:
    """registry.refresh() AFTER a committed mutation (audit finding 20).

    Unguarded, a refresh exception 500s a mutation that DURABLY HAPPENED and
    skips its audit row — the operator retries (doubling the mutation), and
    the trail loses the original action: the two worst inconsistencies at
    once. Degrade instead: the registry stays on its previous in-memory
    snapshot until the next successful refresh (startup, next admin write,
    or POST /admin/registry/refresh), which the log line states."""
    try:
        await registry.refresh()
    except Exception:
        log.exception(
            "registry refresh failed after a COMMITTED admin mutation — the in-memory "
            "registry keeps its previous snapshot until the next refresh "
            "(POST /admin/registry/refresh or the next registry write)"
        )


def _redact_server_headers(servers: list[dict]) -> list[dict]:
    """Redact credential-valued headers on MCP server dicts bound for ANY
    admin response (audit P2-1 + review FLAG-2: the vocabulary lives in
    core.audit._SECRET_KEY_RE and both response paths must use it — the
    /registry/refresh response previously echoed the same cleartext the
    /mcp-servers fix had removed).
    """
    from ..core.audit import _SECRET_KEY_RE

    out = []
    for sv in servers:
        sv = dict(sv)
        sv["auth_configured"] = bool(sv.pop("auth_token_enc", None))
        headers = sv.get("headers") or {}
        sv["headers"] = {
            name: ("<redacted>" if _SECRET_KEY_RE.search(name) else value) for name, value in headers.items()
        }
        out.append(sv)
    return out


async def _check_url_async(urls: list[str], what: str) -> None:
    """Async offload of _check_url for route bodies (audit P1-3):
    getaddrinfo is a BLOCKING syscall - a slow or blackholed resolver
    stalled the event loop (and on a single-worker deployment, every
    data-plane request) for up to 5 s per call, x100 endpoints per
    upsert. Offloaded to a worker thread.

    Dedupe is by EXACT URL, not by host: two endpoints on one host with
    different paths still each run the check (they are distinct URLs).
    Host-level memoisation would need a cache keyed on the parsed host -
    a bigger change than this offload, and the redundant resolve is
    bounded by the endpoint count of ONE upsert."""
    distinct = list(dict.fromkeys(urls))  # order-preserving URL dedupe
    for u in distinct:
        await asyncio.to_thread(_check_url, u, what)


def _resolve_host_addresses(host: str) -> list[str]:
    """Thin wrapper over core.url_guard.resolve_host_addresses (2026-10-26
    hardening plan P0-1 moved the deny-list predicates into a shared module so
    the MCP argument stage reuses ONE implementation).

    Kept as a module-level function on purpose: tests monkeypatch
    routes_admin._URL_RESOLVE_TIMEOUT_S and expect this call site to honour it,
    so the timeout is read HERE, at call time.
    """
    return url_guard.resolve_host_addresses(host, timeout_s=_URL_RESOLVE_TIMEOUT_S)

def _check_url(url: str, what: str) -> None:
    """Validate an admin-registered upstream URL (model endpoint base_url,
    MCP server url) — the SSRF gate.

    Posture AMENDED for in-cluster reality (security audit): this gateway's
    legitimate upstreams ARE private — RFC1918 services and
    *.svc.cluster.local names — so private ranges stay ALLOWED. Hard-denied:
    non-http(s) schemes, loopback (127.0.0.0/8, ::1), and the link-local /
    metadata ranges (169.254.0.0/16 incl. 169.254.169.254, fe80::/10), plus
    any operator-added CIDRs (PCAI_LLM_URL_DENY_CIDRS, JSON list). Every
    refusal is logged with the reason.

    HARDENING (audit finding: trailing-dot / inet_aton bypasses):
    (a) a trailing dot is stripped EXACTLY once and the stripped host re-runs
        the full gate (`127.0.0.1.`, `localhost.`, `169.254.169.254.` are the
        same endpoint as their dotless forms); a host still ending in "."
        after that strip (`localhost..`) is refused outright;
    (b) the inet_aton short-form decoder is now COMPLETE (1-, 2-, 3- and
        4-part forms) so `127.0.65535` (= 127.0.255.255) decodes and hits the
        literal check instead of falling through as a "hostname";
    (c) a host that is SHAPED like a literal (a `0x` prefix, or digits and
        dots only) but still fails to decode is REJECTED — a decode failure
        is never a licence to treat a malformed literal as a hostname. The
        shape test is deliberately narrow so genuine hex-letter hostnames
        (`x`, `beef`, `beef.example.com`) keep taking the hostname path;
    (d) hostnames reaching the hostname path are RESOLVED at registration
        (bounded by _URL_RESOLVE_TIMEOUT_S) and every returned address runs
        the SAME denied-CIDR check, so a name that maps into
        loopback/link-local/metadata is refused up front. A resolution
        FAILURE — transient (EAI_AGAIN, or the lookup exceeding the timeout)
        or authoritative (EAI_NONAME/EAI_NODATA) — degrades to the
        pre-hardening posture with a loud warning: a resolver that cannot
        answer says nothing about the name's danger, and refusing would
        reject the legitimate in-cluster *.svc.cluster.local upstreams this
        gateway exists to serve whenever DNS is unavailable.

    Residual: DNS rebinding AFTER registration — a name that resolves to a
    public address here (or that could not be resolved here at all) and is
    re-pointed at loopback before the call — is still out of scope
    (follow_redirects=False + origin pinning remain the call-time controls).
    Registration-time resolution is now enforced for every name that DOES
    resolve, so the documented residual SHRINKS to that
    rebinding-after-registration window.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        log.warning("refused %s registration %r: non-http(s) scheme %r", what, url, parsed.scheme)
        raise HTTPException(400, f"{what} must start with http:// or https://")
    host = parsed.hostname
    if not host:
        log.warning("refused %s registration %r: no host", what, url)
        raise HTTPException(400, f"{what} must include a host")
    # (a) trailing-dot FQDN forms: strip EXACTLY one trailing dot, then re-run
    # the whole gate on the stripped form. `localhost.` resolves exactly like
    # `localhost`, so the dot must not be a bypass.
    if host.endswith("."):
        stripped = host[:-1]
        if stripped.endswith("."):
            log.warning("refused %s registration %r: trailing-dot host form", what, url)
            raise HTTPException(400, f"{what} host is refused: trailing-dot host form")
        host = stripped
    if host.lower() == "localhost" or host.lower().endswith(".localhost"):
        log.warning("refused %s registration %r: loopback host", what, url)
        raise HTTPException(400, f"{what} host is refused: loopback")
    denied_reason = _denied_ip_reason(host)
    if denied_reason:
        log.warning("refused %s registration %r: %s", what, url, denied_reason)
        raise HTTPException(400, f"{what} host is refused: {denied_reason}")
    # (c)/(d) hostname path. A host that decodes to a valid address is a
    # literal: it already ran the denied-range check above, so an ALLOWED
    # literal (e.g. the RFC1918 upstreams this gateway legitimately uses)
    # returns here without a pointless DNS round-trip.
    decoded = _decode_ip_literal(host)
    if decoded is not None:
        return
    # An IP-LOOKING host that failed to decode is a malformed literal, never a
    # hostname: refuse instead of letting it fall through.
    if _looks_like_ip_literal(host):
        log.warning("refused %s registration %r: IP-literal form failed validation", what, url)
        raise HTTPException(400, f"{what} host is refused: IP-literal form failed validation")
    # Resolve NOW (registration time) and check every returned address against
    # the same denied ranges.
    try:
        addresses = _resolve_host_addresses(host)
    except (socket.gaierror, OSError, UnicodeError) as exc:
        # ALL resolution failures degrade to the pre-hardening posture with a
        # loud warning (COORDINATOR RULING — supersedes the transient-vs-
        # authoritative split): a resolver that cannot answer says NOTHING
        # about the name's danger. Refusing would break the gateway's
        # documented legitimate case — registering in-cluster
        # *.svc.cluster.local upstreams — whenever DNS is unavailable, and
        # also rejects names this environment simply cannot resolve
        # (baseline tests register such names for OTHER validation paths:
        # `https://x` webhook configs, api.example.com). The security-
        # critical hardening above (complete inet_aton decode, trailing-dot
        # strip, malformed-literal rejection) is unaffected, and names that
        # DO resolve into denied ranges are still refused below; a name
        # re-pointed after registration is the documented
        # rebinding-after-registration residual (call-time controls apply).
        log.warning(
            "could not resolve %s registration %r (%s) — resolution check SKIPPED "
            "(rebinding-after-registration residual applies)",
            what,
            url,
            exc,
        )
        return
    for address in addresses:
        resolved_reason = _denied_ip_reason(address)
        if resolved_reason:
            log.warning("refused %s registration %r: host resolves to %s", what, url, resolved_reason)
            raise HTTPException(400, f"{what} host is refused: resolves to {resolved_reason}")
    if not addresses:
        # Resolver answered but returned nothing usable — same policy as any
        # other resolution failure: warn + skip (nothing was SHOWN dangerous).
        log.warning(
            "resolution of %s registration %r returned no addresses — resolution check SKIPPED",
            what,
            url,
        )


# --- shared SSRF deny-list (2026-10-26 hardening plan P0-1) ---------------
# The predicates live in core.url_guard now so the MCP argument-level policy
# stage reuses the SAME implementation instead of growing a second one. These
# module-level aliases keep every existing call site and test (which reach for
# routes_admin._decode_ip_literal / ._looks_like_ip_literal) working unchanged.
_looks_like_ip_literal = url_guard.looks_like_ip_literal
_decode_ip_literal = url_guard.decode_ip_literal


def _denied_ip_reason(host: str) -> str:
    """Registration-time deny check - delegates to the shared predicate,
    passing this gateway's operator-configured extra CIDRs
    (PCAI_LLM_URL_DENY_CIDRS) exactly as before."""
    return url_guard.denied_ip_reason(host, extra_cidrs=settings.url_deny_cidrs)

def _check_scopes(scopes: list[str]) -> None:
    _check_list_len(scopes, "scopes")
    bad = set(scopes) - VALID_SCOPES
    if bad:
        raise HTTPException(400, f"invalid scope(s): {sorted(bad)}; allowed: {sorted(VALID_SCOPES)}")


async def _exists(table: str, row_id: uuid.UUID) -> bool:
    """Existence check up front — clearer 404s than relying on FK violations."""
    return await db.fetchval(f"SELECT 1 FROM {table} WHERE id = $1", row_id) is not None


# ---------------------------------------------------------------------------
# tenants + users
# ---------------------------------------------------------------------------


@router.get("/help", include_in_schema=False)
async def admin_index(_: AdminDep) -> dict[str, Any]:
    """JSON discovery index (curl-friendly). The human console lives at
    GET /admin (HTML, served by app.py).

    ADMIN-GATED (security audit): this index enumerated the full admin
    surface — including the Secret name and an extraction recipe — to
    ANYONE on the public hostname. The auth scheme stays (the console
    needs it); the kubectl recipe is gone (an authenticated admin already
    holds the key, and the Secret name is chart-level knowledge).
    """
    return {
        "service": "PCAI LLM Gateway admin API",
        "console": "GET /admin (browser UI, unlocked with the admin key)",
        "auth": "Authorization: Bearer <admin key> — operator-provisioned "
        "via the pcai-llm-gateway-admin Secret (see documentation/DEPLOYMENT.md)",
        "docs": "documentation/VERIFICATION.md §3 (bootstrap flow)",
        "endpoints": [
            "GET    /admin/keys",
            "POST   /admin/keys",
            "PATCH  /admin/keys/{key_id}",
            "DELETE /admin/keys/{key_id}",
            "POST   /admin/keys/{key_id}/rotate",
            "POST   /admin/keys/{key_id}/budgets",
            "GET    /admin/keys/{key_id}/usage",
            "GET    /admin/audit/chain",
            "GET    /admin/keys/{key_id}/mcp-grants",
            "PUT    /admin/keys/{key_id}/mcp-grants",
            "PATCH  /admin/keys/{key_id}/mcp-grants/{server}",
            "POST   /admin/keys/{key_id}/mcp-grants/copy-from",
            "GET    /admin/usage",
            "GET    /admin/usage/summary",
            "GET    /admin/audit",
            "GET    /admin/endpoints",
            "POST   /admin/tenants",
            "GET    /admin/tenants",
            "GET    /admin/tenants/{tenant_id}/users",
            "POST   /admin/tenants/{tenant_id}/users",
            "POST   /admin/models",
            "GET    /admin/models",
            "DELETE /admin/models/{model_name}",
            "POST   /admin/models/{model_name}/probe",
            "POST   /admin/models/{model_name}/detect-type",
            "POST   /admin/mcp-servers",
            "GET    /admin/mcp-servers",
            "PATCH  /admin/mcp-servers/{name}",
            "GET    /admin/mcp-servers/{name}/tools",
            "POST   /admin/costs",
            "GET    /admin/costs",
            "DELETE /admin/costs/{model_name}",
            "POST   /admin/profiles",
            "GET    /admin/profiles",
            "DELETE /admin/profiles/{profile_id}",
            "PUT    /admin/users/{user_id}/profile",
            "POST   /admin/registry/refresh",
            "GET    /admin/traces",
            "GET    /admin/traces/{request_id}",
            "GET    /admin/offpeak",
            "PUT    /admin/offpeak/models/{model}/fraction",
            "GET    /admin/guardrails",
            "POST   /admin/guardrails",
            "GET    /admin/guardrails/models",
            "PUT    /admin/guardrails/{guardrail_id}/models",
            "PATCH  /admin/guardrails/{guardrail_id}",
            "DELETE /admin/guardrails/{guardrail_id}",
            "GET    /admin/routing/config",
            "PUT    /admin/routing/config",
            "POST   /admin/routing/prompt-preview",
            "GET    /admin/presented-models",
        ],
    }


@router.post("/tenants", status_code=201)
async def create_tenant(body: TenantCreate, request: Request, _: AdminDep) -> dict[str, Any]:
    """Create a tenant (top-level isolation boundary for users and keys)."""
    try:
        row = await db.fetchrow("INSERT INTO tenants (name) VALUES ($1) RETURNING id, name, created_at", body.name)
    except asyncpg.UniqueViolationError:
        raise HTTPException(409, f"tenant '{body.name}' already exists") from None
    assert row is not None
    await _audit(
        request, action="tenant.create", resource_type="tenant", resource_id=str(row["id"]), details={"name": body.name}
    )
    return dict(row)


@router.get("/tenants")
async def list_tenants(_: AdminDep) -> list[dict[str, Any]]:
    """All tenants (console dropdown) + each tenant's SSO self-mint default
    profile (the Enroll tab's dropdown writes it; the mint stamps it)."""
    return [
        dict(r)
        for r in await db.fetch(
            "SELECT t.id, t.name, t.created_at, t.sso_default_profile_id, p.name AS sso_default_profile_name "
            "FROM tenants t LEFT JOIN profiles p ON p.id = t.sso_default_profile_id ORDER BY t.name"
        )
    ]


class TenantSsoDefaultProfile(BaseModel):
    """PUT body for the SSO self-mint default profile. profile_id null =
    clear (mints fall back to the bare fixed grant)."""

    profile_id: uuid.UUID | None = None


@router.put("/tenants/{tenant_id}/sso-default-profile", status_code=200)
async def set_sso_default_profile(
    tenant_id: uuid.UUID, body: TenantSsoDefaultProfile, request: Request, _: AdminDep
) -> dict[str, Any]:
    """Configure which profile every SSO self-mint of THIS tenant receives.

    The mint (and every rotate — a rotate issues a NEW key row, so the
    stamping must repeat or grants would die at first rotation) stamps the
    profile's budgets, limit presets, and MCP grants onto the fresh key;
    scopes stay the mint's fixed least-privilege grant. Clearing (null)
    returns mints to the bare grant. Live immediately: the mint reads the
    column per request.
    """
    if not await _exists("tenants", tenant_id):
        raise HTTPException(404, f"tenant {tenant_id} not found")
    if body.profile_id is not None and not await _exists("profiles", body.profile_id):
        raise HTTPException(404, f"profile {body.profile_id} not found")
    if body.profile_id is not None:
        # Validate the presets at WIRE time, not at first use. This profile
        # is stamped onto every self-mint and every rotation for the tenant,
        # so a bad preset here would brick both — surfacing as an opaque 500
        # long after the operator left this screen. Refuse the wiring with
        # the same 400 the profile save uses.
        prow = await db.fetchrow("SELECT name, defaults_json FROM profiles WHERE id = $1", body.profile_id)
        try:
            defaults = json.loads((prow["defaults_json"] if prow else None) or "{}")
        except (json.JSONDecodeError, TypeError):
            defaults = {}
        try:
            split_preset_budgets(defaults.get("budgets") or [])
        except ValueError as exc:
            pname = prow["name"] if prow else str(body.profile_id)
            raise HTTPException(
                400,
                f"profile {pname!r} cannot be the SSO default: it has an invalid budget preset ({exc})",
            ) from None
    row = await db.fetchrow(
        "UPDATE tenants SET sso_default_profile_id = $2 WHERE id = $1 RETURNING id, name, sso_default_profile_id",
        tenant_id,
        body.profile_id,
    )
    assert row is not None
    await _audit(
        request,
        action="tenant.sso_default_profile.set",
        resource_type="tenant",
        resource_id=str(tenant_id),
        details={"profile_id": str(body.profile_id) if body.profile_id else None},
    )
    return dict(row)


@router.get("/tenants/{tenant_id}/users")
async def list_users(tenant_id: uuid.UUID, _: AdminDep) -> list[dict[str, Any]]:
    """Users of one tenant (console dropdown)."""
    if not await _exists("tenants", tenant_id):
        raise HTTPException(404, f"tenant {tenant_id} not found")
    return [
        dict(r)
        for r in await db.fetch(
            "SELECT id, tenant_id, name, profile_id, created_at FROM users WHERE tenant_id = $1 ORDER BY name",
            tenant_id,
        )
    ]


@router.post("/tenants/{tenant_id}/users", status_code=201)
async def create_user(tenant_id: uuid.UUID, body: UserCreate, request: Request, _: AdminDep) -> dict[str, Any]:
    """Create a user under a tenant; the tenant must exist (checked, not FK-driven)."""
    if not await _exists("tenants", tenant_id):
        raise HTTPException(404, f"tenant {tenant_id} not found")
    try:
        row = await db.fetchrow(
            "INSERT INTO users (tenant_id, name) VALUES ($1, $2) RETURNING id, tenant_id, name, created_at",
            tenant_id,
            body.name,
        )
    except asyncpg.UniqueViolationError:
        raise HTTPException(409, f"user '{body.name}' already exists in tenant") from None
    assert row is not None
    await _audit(
        request,
        action="user.create",
        resource_type="user",
        resource_id=str(row["id"]),
        details={"name": body.name, "tenant_id": str(tenant_id)},
    )
    return dict(row)


# ---------------------------------------------------------------------------
# keys
# ---------------------------------------------------------------------------


@router.post("/keys", status_code=201)
async def create_key_endpoint(body: KeyCreate, request: Request, _: AdminDep) -> KeyCreated:
    """Create a virtual API key.

    The PLAINTEXT key is returned here exactly once — it is stored only as a
    SHA-256 hash (core.keys convention). Rotation is create-new / revoke-old;
    there is no key-recovery path by design.

    Identity binding (Phase A, additive): an optional `subject` on the
    request binds the key to that subject (upsert on UNIQUE (idp, subject),
    idp='admin-asserted', bound_via='admin-console') INSIDE the create
    transaction — a failed bind rolls the whole create back, so a key never
    exists half-bound (bind-before-plaintext-exists ordering; the redemption
    flow uses the opposite best-effort posture because its plaintext is
    already irreplaceable when binding runs). The user row supplies the
    tenant for the subject; an unknown user is a 404 above.
    """
    _check_scopes(body.scopes)
    _check_list_len(body.allowed_models, "allowed_models")
    subject = normalize_subject(body.subject) if body.subject is not None else ""
    if body.subject is not None and not subject:
        raise HTTPException(400, "subject must not be blank when provided")
    if not await _exists("users", body.user_id):
        raise HTTPException(404, f"user {body.user_id} not found")

    profile: dict[str, Any] | None = None
    if body.profile_id is not None:
        prow = await db.fetchrow(
            "SELECT id, name, model_order, defaults_json FROM profiles WHERE id = $1",
            body.profile_id,
        )
        if prow is None:
            raise HTTPException(404, f"profile {body.profile_id} not found")
        try:
            defaults = json.loads(prow["defaults_json"] or "{}")
        except (json.JSONDecodeError, TypeError):
            defaults = {}
        profile = {
            "id": prow["id"],
            "name": prow["name"],
            "budgets": defaults.get("budgets", []),
            "mcp_grants": defaults.get("mcp_grants", []),
            # limit presets OVERRIDE the request's values when present —
            # the profile is the operator's declared baseline for this
            # population ("profiles as defaults" extends to rate limits)
            "limits": defaults.get("limits", {}) or {},
        }

    if profile is not None:
        # Validate the stored presets BEFORE opening the transaction:
        # defaults_json is raw JSON that an older build (or a hand edit)
        # may have written in a shape this build refuses, and a ValueError
        # raised inside the transaction would surface as a 500 on a route
        # whose contract is 400 for a bad preset.
        try:
            split_preset_budgets(profile["budgets"])
        except ValueError as exc:
            raise HTTPException(400, f"profile {profile['name']!r} has an invalid budget preset: {exc}") from None
    # profile keys: create + seed budgets/grants + attach profile to the
    # user in ONE transaction (a half-seeded key is worse than a failed one)
    plaintext, key_hash = generate_key()
    lim = (profile or {}).get("limits", {}) if profile else {}
    rpm = int(lim.get("rate_limit_rpm") or body.rate_limit_rpm)
    tpm = int(lim.get("rate_limit_tpm") or body.rate_limit_tpm)
    mc = int(lim["max_concurrent"]) if lim.get("max_concurrent") is not None else 0
    async with db.acquire() as conn, conn.transaction():
        row = await conn.fetchrow(
            "INSERT INTO api_keys (user_id, name, key_hash, key_prefix, scopes, "
            "allowed_models, rate_limit_rpm, rate_limit_tpm, expires_at, relay_default, "
            "stream_pcai_events, max_concurrent, mcp_admin) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13) RETURNING id",
            body.user_id,
            body.name,
            key_hash,
            plaintext[:10],
            body.scopes,
            body.allowed_models or [],
            rpm,
            tpm,
            body.expires_at,
            body.relay_default,
            body.stream_pcai_events,
            mc,
            body.mcp_admin,
        )
        assert row is not None
        key_id = row["id"]
        if subject:
            # Phase A binding inside the SAME transaction (see docstring):
            # tenant from the target user row (the subject rides the tenant
            # dimension), display name = the key name.
            urow = await conn.fetchrow("SELECT tenant_id FROM users WHERE id = $1", body.user_id)
            assert urow is not None  # user existence was checked above
            await bind_key_subject(
                key_id,
                subject=subject,
                idp=IDP_ADMIN_ASSERTED,
                bound_via=VIA_ADMIN_CONSOLE,
                tenant_id=urow["tenant_id"],
                display_name=body.name,
                conn=conn,
            )
        if profile is not None:
            await conn.execute("UPDATE users SET profile_id = $2 WHERE id = $1", body.user_id, profile["id"])
            # Presets split by KIND (core.budgets.split_preset_budgets): token
            # presets -> budgets, dollar presets -> cost_budgets. Before the
            # split existed the seeding was token-only, so a profile could
            # never hand a key a dollar budget.
            token_rows, usd_rows = split_preset_budgets(profile["budgets"])
            if token_rows:
                await conn.executemany(
                    "INSERT INTO budgets (key_id, model, period, limit_tokens) VALUES ($1,$2,$3,$4)",
                    [(key_id, m, p, lim) for m, p, lim in token_rows],
                )
            if usd_rows:
                await conn.executemany(
                    "INSERT INTO cost_budgets (key_id, model, period, limit_usd) VALUES ($1,$2,$3,$4)",
                    [(key_id, m, p, lim) for m, p, lim in usd_rows],
                )
            if profile["mcp_grants"]:
                await conn.executemany(
                    "INSERT INTO mcp_tool_acls (key_id, server, tool, allowed) "
                    "VALUES ($1,$2,$3,TRUE) ON CONFLICT (key_id, server, tool) DO UPDATE SET allowed = TRUE",
                    [(key_id, g[0], g[1]) for g in profile["mcp_grants"]],
                )
    # Audit AFTER the create commits (best-effort; core.audit tradeoff).
    # details: scopes/limits/profile preset COUNTS — never key material (the
    # plaintext exists only in this response; the hash lives in api_keys).
    await _audit(
        request,
        action="key.create",
        resource_type="key",
        resource_id=str(key_id),
        details={
            "name": body.name,
            "user_id": str(body.user_id),
            "scopes": body.scopes,
            "allowed_models": body.allowed_models,
            "rate_limit_rpm": rpm,
            "rate_limit_tpm": tpm,
            "relay_default": body.relay_default,
            "stream_pcai_events": body.stream_pcai_events,
            "mcp_admin": body.mcp_admin,
            "profile_id": str(body.profile_id) if body.profile_id else None,
            "subject": subject or None,
            "seeded_budgets": len((profile or {}).get("budgets") or []),
            "seeded_grants": len((profile or {}).get("mcp_grants") or []),
        },
    )
    return KeyCreated(id=key_id, key=plaintext, key_prefix=plaintext[:10])


@router.delete("/keys/{key_id}")
async def revoke_key_endpoint(key_id: uuid.UUID, request: Request, _: AdminDep) -> dict[str, Any]:
    """Revoke a key. Existence is checked first (404), revocation is idempotent."""
    if not await _exists("api_keys", key_id):
        raise HTTPException(404, f"key {key_id} not found")
    revoked = await revoke_key(key_id)
    await _audit(
        request, action="key.revoke", resource_type="key", resource_id=str(key_id), details={"revoked": revoked}
    )
    return {"revoked": revoked}


@router.get("/keys")
async def list_keys_endpoint(_: AdminDep, include_revoked: bool = False) -> list[dict[str, Any]]:
    """List keys across all users. Hashes are never included in projections.
    Revoked keys are hidden unless include_revoked=true (their ledger rows
    remain — history stays attributable).

    Each row carries its `budget_status` (token + USD budgets with current
    spend) — ONE grouped query per budget kind for the whole page
    (budget_status_many; audit finding 6: the per-key loop ran 2 ledger
    aggregates per key — ~401 sequential queries at 200 keys — against the
    pool the data plane shares).
    """
    rows = await list_keys(include_revoked=include_revoked)
    statuses = await budget_status_many([r["id"] for r in rows])
    for row in rows:
        row["budget_status"] = statuses.get(row["id"], [])
    return rows


class KeyPatch(BaseModel):
    """Partial key update — all fields optional; only provided ones change.

    Identity material is deliberately ABSENT: key_hash / key_prefix never
    appear in the UPDATE or the response (rotate = reissue, fleet
    convention; editing a hash would be a self-inflicted outage).
    """

    name: str | None = Field(None, min_length=1, max_length=200)
    scopes: list[str] | None = None
    allowed_models: list[str] | None = None
    rate_limit_rpm: int | None = Field(None, gt=0)
    rate_limit_tpm: int | None = Field(None, gt=0)
    max_concurrent: int | None = Field(None, ge=0)
    # Explicit null (in the JSON body) clears the expiry; omitted = untouched
    # (distinguished via model_fields_set below).
    expires_at: datetime | None = None
    # TRUE = the tool relay engages without the X-PCAI-Execute-Tools header
    # (for off-cluster consumers like sproket); the header still overrides.
    relay_default: bool | None = None
    # TRUE = relayed streams carry the in-stream {"pcai": …} telemetry
    # events (RFC-PER-KEY-STREAM-EVENTS); flipping OFF serves strict
    # validating clients a byte-clean OpenAI-spec stream. Live immediately
    # (keys are read per request).
    stream_pcai_events: bool | None = None
    # MCP-surface admin opt-in (P0-3): the admin_* hard stop reads this flag
    # per request (keys authenticate from Postgres on every call), so a
    # PATCH is live immediately — same as every other column here. The
    # default-False control is preserved: omitted = untouched, and False
    # (explicit) is always accepted so an operator can strip the flag.
    mcp_admin: bool | None = None


@router.patch("/keys/{key_id}")
async def patch_key(key_id: uuid.UUID, body: KeyPatch, request: Request, _: AdminDep) -> dict[str, Any]:
    """Edit a key's limits/scopes/expiry IN PLACE (gaps P4).

    Keys are read from Postgres per request (core.keys.authenticate), so
    edits are live immediately — no reissue, no client migration, no
    gateway restart. The plaintext key and its hash/prefix are untouched.
    """
    if not await _exists("api_keys", key_id):
        raise HTTPException(404, f"key {key_id} not found")
    if body.scopes is not None:
        _check_scopes(body.scopes)
    if body.allowed_models is not None:
        _check_list_len(body.allowed_models, "allowed_models")
    sets: list[str] = []
    args: list[Any] = []
    for col in (
        "name",
        "scopes",
        "allowed_models",
        "rate_limit_rpm",
        "rate_limit_tpm",
        "max_concurrent",
        "relay_default",
        "stream_pcai_events",
        "mcp_admin",
    ):
        val = getattr(body, col)
        if val is not None:
            args.append(val)
            sets.append(f"{col} = ${len(args)}")
    if "expires_at" in body.model_fields_set:
        args.append(body.expires_at)
        sets.append(f"expires_at = ${len(args)}")
    if not sets:
        raise HTTPException(400, "no editable fields provided")
    # column names above are a fixed tuple, never user input
    row = await db.fetchrow(
        f"UPDATE api_keys SET {', '.join(sets)} WHERE id = ${len(args) + 1} "
        "RETURNING id, user_id, name, scopes, allowed_models, rate_limit_rpm, "
        "rate_limit_tpm, max_concurrent, expires_at, relay_default, stream_pcai_events, "
        "mcp_admin",
        *args,
        key_id,
    )
    assert row is not None
    # details: ONLY the fields the PATCH actually set (body.model_fields_set
    # is the diff — the trail records the change, not the row).
    await _audit(
        request,
        action="key.patch",
        resource_type="key",
        resource_id=str(key_id),
        details={field: getattr(body, field) for field in sorted(body.model_fields_set)},
    )
    return dict(row)


@router.post("/keys/{key_id}/rotate", status_code=201)
async def rotate_key(key_id: uuid.UUID, _: AdminDep, request: Request) -> KeyRotated:
    """Rotate a key: mint a NEW key with the SAME name and attributes, then
    revoke the old one. The new plaintext is returned HERE exactly once —
    the only copy that will ever exist (the DB keeps only the SHA-256 hash;
    there is no recovery path, same convention as POST /keys).

    Attributes carried over verbatim from the old row: name, scopes,
    allowed_models, rate_limit_rpm/tpm, max_concurrent, expires_at,
    relay_default — AND the mcp_tool_acls grant rows (allow AND deny) AND
    the mcp_server_creds per-server credential-override rows (the memory
    dataset/password identity) AND the identity binding (subject_id /
    bound_via / bound_at, Phase A).

    WHY the grants carry-over (2026-09-18 incident): a rotated key dropped
    from ~102 federated tools to 2. The mcp_tool_acls rows are keyed by
    key_id, and rotation mints a NEW key_id — so without a copy, the fresh
    id started with ZERO grants and the fleet's default-deny federation
    policy left every tool refused; live sessions broke. Federation grants
    are part of a key's IDENTITY (like scopes), not a per-rotation choice,
    so the server now carries them over itself. The console's post-rotate
    client-side grant copy becomes a harmless idempotent no-op: it re-PUTs
    the same (server, tool) pairs it reads off the old id, and a replace-all
    PUT with an identical pair set lands the same end state. Budgets stay
    console-copied (ledger-anchored, per the original contract).

    The copy is ONE statement between create and revoke
    (`INSERT ... SELECT`), so it preserves allowed=FALSE rows verbatim —
    a deny row documents a deliberate exception and losing it would
    resurrect the grant the operator had turned off. Response
    `grants_copied` reports how many rows landed on the new key.

    WHY create-first-then-revoke, as two writes rather than one transaction:
    the ordering guarantees every partial failure OVER-provisions, never
    under-provisions.
      - a crash between the two writes leaves the new key active alongside
        the old — harmless duplication the operator closes by revoking the
        old id (DELETE /admin/keys/{key_id}) immediately after;
      - the reverse order (revoke-then-create) is strictly worse: a crash
        there strands every client of the old key on 401s with no
        replacement in hand, and since the plaintext only ever exists in
        the response of the successful create, a revoked old key plus a
        failed create is unrecoverable for that credential.
    Never leave the old key revoked while the new one failed to create.

    Fail closed: unknown id → 404; an already-REVOKED key refuses to rotate
    (409) — a revoked row is audit history the usage ledger points at, not
    a live credential to re-issue from.
    """
    old = await db.fetchrow(
        "SELECT user_id, name, scopes, allowed_models, rate_limit_rpm, "
        "rate_limit_tpm, max_concurrent, expires_at, relay_default, "
        "stream_pcai_events, mcp_admin, revoked_at, spend_ancestors, "
        "subject_id, bound_via, bound_at "
        "FROM api_keys WHERE id = $1",
        key_id,
    )
    if old is None:
        raise HTTPException(404, f"key {key_id} not found")
    if old["revoked_at"] is not None:
        raise HTTPException(409, f"key {key_id} is already revoked — rotating a revoked key is refused")
    plaintext, key_hash = generate_key()
    # Same INSERT shape as POST /keys, but the values come from the OLD row,
    # not the request and not the profile's current defaults — a profile
    # edit since the key was created must not silently re-limit this key
    # under the cover of a rotation. subject_id/bound_via/bound_at join the
    # explicit column list (Phase A): rotation enumerates columns, so a
    # binding NOT named here would be silently stripped — the exact
    # grants-carry-over bug class (2026-09-18), this time for identity. An
    # unbound key carries NULL/'' /NULL verbatim (no behavior change).
    #
    # ONE TRANSACTION (audit finding 5): create → grants → creds → revoke
    # used to be four separate module-pool writes with a check-then-act gap
    # on the revoked_at read above — a crash mid-flow left duplicate live
    # keys with half-copied grants, and two concurrent rotates could both
    # pass the revoked check and mint two live keys from one credential.
    # The transaction closes both: any failure rolls EVERYTHING back (the
    # caller retries clean), and the revoke below is now conditional
    # (WHERE revoked_at IS NULL) so the second concurrent rotate's revoke
    # no-ops against the already-revoked row and its INSERT of a second
    # live key is the only remaining (and detectable — old_revoked=False)
    # window, narrowed from "any interleaving" to "both passed the same
    # pre-check race", which the conditional revoke + audit row surfaces.
    async with db.acquire() as conn, conn.transaction():
        row = await conn.fetchrow(
            "INSERT INTO api_keys (user_id, name, key_hash, key_prefix, scopes, "
            "allowed_models, rate_limit_rpm, rate_limit_tpm, expires_at, relay_default, "
            "stream_pcai_events, max_concurrent, spend_ancestors, subject_id, bound_via, "
            "bound_at, mcp_admin) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17) RETURNING id",
            old["user_id"],
            old["name"],
            key_hash,
            plaintext[:10],
            list(old["scopes"]),
            list(old["allowed_models"]),
            old["rate_limit_rpm"],
            old["rate_limit_tpm"],
            old["expires_at"],
            old["relay_default"],
            old["stream_pcai_events"],
            old["max_concurrent"],
            # Spend lineage: the new key INHERITS the old key's ancestors and
            # APPENDS the old key itself — the budget gates sum the whole
            # chain, so in-window spend survives rotation (the budget-reset
            # bypass fix). Rotation enumerates INSERT columns explicitly; a
            # lineage NOT named here would be silently stripped — the exact
            # grants-carry-over bug class (2026-09-18), this time for spend.
            list(old["spend_ancestors"] or []) + [key_id],
            old["subject_id"],
            old["bound_via"],
            old["bound_at"],
            # mcp_admin carries over like relay_default: an identity attribute
            # of the credential the admin rotated deliberately — silently
            # stripping it would be the grants-carry-over bug class again
            # (2026-09-18), this time for the admin flag.
            # .get (not [..]): the row is an asyncpg Record in production but a
            # plain dict in the test fakes, and a MISSING column must degrade to
            # the fail-closed default rather than raise mid-rotation.
            old.get("mcp_admin", False),
        )
        assert row is not None  # INSERT ... RETURNING always yields a row
        new_id: uuid.UUID = row["id"]
        # Grant carry-over: copy EVERY mcp_tool_acls row (allow AND deny) onto
        # the new key_id in one INSERT..SELECT — after the new key exists (FK)
        # and before the revoke. Default-deny federation means missing grants =
        # every tool refused, so the copy must land before the old key
        # disappears. (Everything below runs INSIDE the transaction opened
        # above — the whole rotation is atomic now; the historical
        # over-provision-on-crash rationale is superseded by all-or-nothing.)
        grant_tag = await conn.execute(
            "INSERT INTO mcp_tool_acls (key_id, server, tool, allowed) "
            "SELECT $1, server, tool, allowed FROM mcp_tool_acls WHERE key_id = $2",
            new_id,
            key_id,
        )
        # asyncpg's execute returns the command tag ("INSERT 0 <n>"): n = rows
        # copied (0 = the old key held no grant rows). fetchval would NOT work
        # here — an INSERT..SELECT without RETURNING yields no result set, so
        # it returns None regardless of rows written; the tag is the count.
        try:
            grants_copied = int(grant_tag.split()[-1])
        except (AttributeError, ValueError):
            grants_copied = 0
        # Credential-override carry-over: same identity argument as the grants
        # above (mcp_server_creds = the memory-MCP dataset/password identity,
        # Fernet ciphertext keyed by (key_id, server_name), key-INDEPENDENT —
        # a straight INSERT..SELECT preserves it verbatim). One statement;
        # UNIQUE (key_id, server_name) cannot collide on a fresh id.
        creds_tag = await conn.execute(
            "INSERT INTO mcp_server_creds (key_id, server_name, headers_enc) "
            "SELECT $1, server_name, headers_enc FROM mcp_server_creds WHERE key_id = $2",
            new_id,
            key_id,
        )
        try:
            creds_copied = int(creds_tag.split()[-1])
        except (AttributeError, ValueError):
            creds_copied = 0
        # Budget LIMIT carry-over (the budget-reset bypass fix): copy BOTH
        # budget families' limit rows onto the new key_id in the same
        # transaction. Before this, the SERVER never copied budgets — the
        # console re-applied them client-side and a raw-API rotate left the
        # new key with ZERO budget rows (unlimited) even ignoring the spend
        # reset. The console's post-rotate re-POST stays an idempotent
        # no-op: a replace-all PUT with the identical (model, period, limit)
        # set lands the same end state. Spend itself is carried by
        # spend_ancestors above — the limits define the ceiling, the
        # lineage carries the consumption.
        budgets_copied = 0
        for btable, bcols, bselect in (
            ("budgets", "model, period, limit_tokens", "model, period, limit_tokens"),
            ("cost_budgets", "model, period, limit_usd", "model, period, limit_usd"),
        ):
            b_tag = await conn.execute(
                f"INSERT INTO {btable} (key_id, {bcols}) "
                f"SELECT $1, {bselect} FROM {btable} WHERE key_id = $2 "
                "ON CONFLICT (key_id, model, period) DO NOTHING",
                new_id,
                key_id,
            )
            try:
                budgets_copied += int(b_tag.split()[-1])
            except (AttributeError, ValueError):
                pass
        # Conditional revoke (audit finding 5 — closes the concurrent-rotate
        # race as far as a single statement can): only a still-live row is
        # revoked; a concurrent rotate that already revoked it no-ops here.
        revoke_row = await conn.fetchrow(
            "UPDATE api_keys SET revoked_at = now() WHERE id = $1 AND revoked_at IS NULL RETURNING id",
            key_id,
        )
        revoked = revoke_row is not None
    if not revoked:
        log.warning("rotate: old key %s was already revoked when the revoke ran (new key %s live)", key_id, new_id)
    # One audit row for the rotation as a WHOLE (old→new linkage in details);
    # key.revoke for the old id is implied by old_key_id — two rows would
    # double-count one operator action.
    await _audit(
        request,
        action="key.rotate",
        resource_type="key",
        resource_id=str(key_id),
        details={
            "new_key_id": str(new_id),
            "old_revoked": revoked,
            "grants_copied": grants_copied,
            "creds_copied": creds_copied,
            "budgets_copied": budgets_copied,
        },
    )
    return KeyRotated(
        id=new_id,
        key=plaintext,
        key_prefix=plaintext[:10],
        old_key_id=key_id,
        old_revoked=revoked,
        grants_copied=int(grants_copied),
        creds_copied=int(creds_copied),
        budgets_copied=int(budgets_copied),
        subject_id=old["subject_id"],
        bound_via=old["bound_via"] or "",
    )


# ---------------------------------------------------------------------------
# self-mint eligibility (SSO self-mint, admin-governed)
# ---------------------------------------------------------------------------


class SelfMintEligibility(BaseModel):
    """The PUT /keys/self-mint-eligibility body. EXACTLY ONE selector —
    user_id (the console's row anchor) OR subject (the browser identity
    string the for-me flow presents) — a request carrying both (or
    neither) is a 400, never an ambiguous write."""

    user_id: uuid.UUID | None = None
    subject: str | None = Field(default=None, min_length=1, max_length=200)
    allowed: bool

    @field_validator("subject")
    @classmethod
    def _clean_subject(cls, v: str | None) -> str | None:
        # the same hygiene every caller-supplied subject gets (trim, drop
        # control chars, cap 200) — a blocked-subject write must target
        # the SAME string the mint lookup will read
        return normalize_subject(v) or None if v is not None else None


@router.get("/keys/self-mint-eligibility")
async def list_self_mint_eligibility(_: AdminDep) -> list[dict[str, Any]]:
    """List every user's self-mint eligibility (the console's column).

    Joined with the users' bound subjects (a user may hold several subject
    bindings over its key set — the LIST carries them all so an operator
    can see exactly which browser identities each row governs). Rows sort
    by user name (the console's stable scan order).

    WRITE-SIDE NOTE: this endpoint and PUT
    below are the flag's only writers; the ENFORCEMENT read is
    routes_identity._self_mint_allowed_for_subject — LIVE, reading
    users.self_mint_enabled through the SAME selector this write targets
    (the newest non-revoked sso-create key of the oauth2-proxy subject;
    audit P1-2 — the selectors were aligned after the original
    divergence let a block land on a user row the mint never consulted)."""
    rows = await db.fetch(
        """
        SELECT u.id AS user_id, u.name AS user_name, u.self_mint_enabled,
               COALESCE(array_agg(s.subject) FILTER (WHERE s.subject IS NOT NULL), '{}') AS subjects
        FROM users u
        LEFT JOIN api_keys k ON k.user_id = u.id
        LEFT JOIN subjects s ON s.id = k.subject_id
        GROUP BY u.id, u.name, u.self_mint_enabled
        ORDER BY u.name, u.id
        """
    )
    return [
        {
            "user_id": str(r["user_id"]),
            "user_name": r["user_name"],
            "self_mint_enabled": bool(r["self_mint_enabled"]),
            "subjects": list(r["subjects"]),
        }
        for r in rows
    ]


@router.put("/keys/self-mint-eligibility")
async def set_self_mint_eligibility(body: SelfMintEligibility, request: Request, _: AdminDep) -> dict[str, Any]:
    """Allow/block ONE user's SSO self-mint (the admin-governance half of
    the browser self-service door).

    The write targets the USER row (self_mint_enabled); a subject selector
    resolves to its user THROUGH the binding (subject → newest key →
    user) so the operator can block by the browser identity they see in
    audit lines. 404 when the selector matches nothing. The enforcement
    read lives in routes_identity._self_mint_allowed_for_subject (z2's
    function, the ONE lookup both the probe and the mint consult — an
    audit line and a blocked mint can never disagree about who's blocked).

    ENFORCEMENT STATUS: the read-side lookup is LIVE (routes_identity
    reads users.self_mint_enabled through the same selector this write
    targets — see the aligned UPDATE below, audit P1-2). This endpoint
    PERSISTS the flag; the GET list and the console toggle report it
    faithfully, and the mint/probe consult it. test_admin_selfmint.py
    pins the end-to-end data flow."""
    if body.user_id is not None and body.subject is not None:
        raise HTTPException(400, "pass either user_id or subject, not both")
    if body.user_id is None and body.subject is None:
        raise HTTPException(400, "pass user_id or subject")
    if body.user_id is not None:
        row = await db.fetchrow(
            "UPDATE users SET self_mint_enabled = $2 WHERE id = $1 RETURNING id, name, self_mint_enabled",
            body.user_id,
            body.allowed,
        )
    else:
        assert body.subject is not None
        # subject → its user: through the binding the mint flow itself
        # reads (subjects UNIQUE (idp, subject); the binding's user is the
        # key holder — an SSO-created key's user IS the per-subject user).
        row = await db.fetchrow(
            """
            UPDATE users SET self_mint_enabled = $2 WHERE id = (
                -- Selector MUST mirror routes_identity._SELF_MINT_ELIGIBILITY_SQL
                -- (audit P1-2): the read gates the mint through the newest
                -- non-revoked sso-create key of the oauth2-proxy subject; a write
                -- through any other key (enrollment/self-asserted, revoked) would
                -- flag a user row the mint never consults — a control that
                -- reports success and does nothing.
                SELECT k.user_id FROM api_keys k
                JOIN subjects s ON s.id = k.subject_id
                WHERE s.subject = $1
                  AND s.idp = 'oauth2-proxy'
                  AND k.bound_via = 'sso-create'
                  AND k.revoked_at IS NULL
                ORDER BY k.created_at DESC, k.id DESC
                LIMIT 1
            ) RETURNING id, name, self_mint_enabled
            """,
            body.subject,
            body.allowed,
        )
    if row is None:
        raise HTTPException(404, "user not found for the given selector")
    await _audit(
        request,
        action="key.self_mint_eligibility",
        resource_type="user",
        resource_id=str(row["id"]),
        details={
            "user": row["name"],
            "allowed": body.allowed,
            "by_subject": body.subject is not None,
        },
    )
    return {
        "user_id": str(row["id"]),
        "user_name": row["name"],
        "self_mint_enabled": bool(row["self_mint_enabled"]),
    }


@router.post("/keys/{key_id}/budgets")
async def replace_budgets(key_id: uuid.UUID, body: BudgetsReplace, request: Request, _: AdminDep) -> dict[str, Any]:
    """Replace ALL budgets on a key in one transaction.

    WHY replace-all: budgets are DECLARATIVE state (the operator's desired
    limit set), not an event log — partial updates would leave stale limits
    interleaved with new ones. One transaction (delete + executemany) makes
    the swap atomic: a reader either sees the old set or the new one, never
    a half-applied mix.
    """
    _check_list_len(body.budgets, "budgets")
    if not body.budgets:
        raise HTTPException(400, "budgets list must not be empty (DELETE not supported here)")
    if not await _exists("api_keys", key_id):
        raise HTTPException(404, f"key {key_id} not found")
    for b in body.budgets:
        if b.period not in VALID_PERIODS:
            raise HTTPException(400, f"period must be one of {sorted(VALID_PERIODS)}")
        has_tokens = b.limit_tokens is not None
        has_usd = b.limit_usd is not None
        if has_tokens == has_usd:  # exactly one limit per entry
            raise HTTPException(400, "each budget needs exactly one of limit_tokens / limit_usd")
        if has_tokens and b.limit_tokens is not None and b.limit_tokens <= 0:
            raise HTTPException(400, "limit_tokens must be > 0")
        if has_usd and b.limit_usd is not None and b.limit_usd <= 0:
            raise HTTPException(400, "limit_usd must be > 0")
    token_rows = [(key_id, b.model, b.period, b.limit_tokens) for b in body.budgets if b.limit_tokens is not None]
    usd_rows = [(key_id, b.model, b.period, b.limit_usd) for b in body.budgets if b.limit_usd is not None]
    async with db.acquire() as conn, conn.transaction():
        # REPLACE-ALL across BOTH kinds (declarative state)
        await conn.execute("DELETE FROM budgets WHERE key_id = $1", key_id)
        await conn.execute("DELETE FROM cost_budgets WHERE key_id = $1", key_id)
        if token_rows:
            await conn.executemany(
                "INSERT INTO budgets (key_id, model, period, limit_tokens) "
                "VALUES ($1, $2, $3, $4) "
                "ON CONFLICT (key_id, model, period) DO UPDATE SET limit_tokens = $4",
                token_rows,
            )
        if usd_rows:
            await conn.executemany(
                "INSERT INTO cost_budgets (key_id, model, period, limit_usd) "
                "VALUES ($1, $2, $3, $4) "
                "ON CONFLICT (key_id, model, period) DO UPDATE SET limit_usd = $4",
                usd_rows,
            )
    await _audit(
        request,
        action="budget.set",
        resource_type="key",
        resource_id=str(key_id),
        # the SPEC (model/period/kind), not the parsed rows — the budgets
        # table itself is the durable state; the trail records the request.
        details={"budgets": [b.model_dump() for b in body.budgets]},
    )
    return {"key_id": str(key_id), "budgets": await budget_status(key_id)}


# ---------------------------------------------------------------------------
# usage
# ---------------------------------------------------------------------------


@router.get("/keys/{key_id}/usage")
async def key_usage(key_id: uuid.UUID, _: AdminDep) -> dict[str, Any]:
    """Per-key view: current budget windows plus the 50 most recent ledger rows."""
    if not await _exists("api_keys", key_id):
        raise HTTPException(404, f"key {key_id} not found")
    recent = await db.fetch(
        "SELECT id, model, endpoint, prompt_tokens, completion_tokens, total_tokens, "
        "status, latency_ms, request_id, ts FROM usage_ledger "
        "WHERE key_id = $1 ORDER BY ts DESC LIMIT 50",
        key_id,
    )
    return {"budgets": await budget_status(key_id), "recent": [dict(r) for r in recent]}


USAGE_GROUP_BYS = {"key", "model", "tenant", "day", "subject"}


@router.get("/usage")
async def usage(
    _: AdminDep,
    limit: int = 100,
    key_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
    model: str | None = None,
    status: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    group_by: str | None = None,
    format: str = "json",
) -> Any:
    """Ledger explorer (gaps P2 backend): last N rows across all keys with
    optional filters (key_id/model/status/since/until), an ACTOR join
    (key_name/key_prefix/user_name/tenant_name — "who called what when"),
    GROUP BY rollups (key|model|tenant|day), and CSV export
    (format=csv -> text/csv + Content-Disposition attachment).

    The 1000-row cap from the original handler is kept as the export bound.
    """
    if group_by is not None and group_by not in USAGE_GROUP_BYS:
        raise HTTPException(400, f"group_by must be one of {sorted(USAGE_GROUP_BYS)}")
    if format not in ("json", "csv"):
        raise HTTPException(400, "format must be 'json' or 'csv'")
    capped = max(1, min(limit, 1000))

    conds: list[str] = []
    args: list[Any] = []
    if key_id is not None:
        args.append(key_id)
        conds.append(f"l.key_id = ${len(args)}")
    if user_id is not None:
        args.append(user_id)
        conds.append(f"k.user_id = ${len(args)}")
    if model is not None:
        args.append(model)
        conds.append(f"l.model = ${len(args)}")
    if status is not None:
        args.append(status)
        conds.append(f"l.status = ${len(args)}")
    if since is not None:
        args.append(since)
        conds.append(f"l.ts >= ${len(args)}::timestamptz")
    if until is not None:
        args.append(until)
        conds.append(f"l.ts <= ${len(args)}::timestamptz")
    where = f"WHERE {' AND '.join(conds)}" if conds else ""

    if group_by is not None:
        dims = {
            "key": "l.key_id, k.name AS key_name, k.key_prefix AS key_prefix",
            "model": "l.model",
            "tenant": "t.id AS tenant_id, t.name AS tenant_name",
            "day": "date_trunc('day', l.ts) AS day",
            # Phase A identity: subject projection via the key→subject FK
            # (read-time LEFT JOIN — the binding is immutable after
            # issuance, so the join is faithful; unbound keys group under a
            # NULL subject — "no subject" is an honest bucket, not an error).
            "subject": "k.subject_id, s.subject AS subject, s.idp AS subject_idp, s.kind AS subject_kind",
        }[group_by]
        args.append(capped)
        lim = f"${len(args)}"
        rows = await db.fetch(
            f"""
            SELECT {dims},
                   count(*)                                      AS requests,
                   COALESCE(SUM(l.prompt_tokens), 0)             AS input_tokens,
                   COALESCE(SUM(l.completion_tokens), 0)         AS output_tokens,
                   COALESCE(SUM(l.cached_tokens), 0)             AS cached_tokens,
                   count(*) FILTER (WHERE l.status = 'cache_hit') AS cache_hits,
                   COALESCE(SUM(l.cost_usd), 0)::float           AS cost_usd
            FROM usage_ledger l
            LEFT JOIN api_keys k ON k.id = l.key_id
            LEFT JOIN users u ON u.id = k.user_id
            LEFT JOIN tenants t ON t.id = u.tenant_id
            LEFT JOIN subjects s ON s.id = k.subject_id
            {where}
            GROUP BY {dims}
            ORDER BY requests DESC
            LIMIT {lim}
            """,
            *args,
        )
        out = [dict(r) for r in rows]
        if format == "csv":
            return _csv_response(out, f"usage-by-{group_by}.csv")
        return out

    args.append(capped)
    rows = await db.fetch(
        f"""
        SELECT l.id, l.key_id, l.model, l.endpoint, l.prompt_tokens,
               l.completion_tokens, l.total_tokens, l.status, l.latency_ms,
               l.request_id, l.ts,
               -- cached_tokens / cost_usd / billing_fraction were MISSING from
               -- this projection while the console rendered all three: every row
               -- showed a hard 0 for cached and a bare dash for cost, which reads
               -- as "the upstream never caches and nothing is priced" — the
               -- opposite of the truth (98.4% of input tokens are cache-served
               -- fleet-wide). The rollups above always summed these columns; only
               -- the per-row view dropped them, so the two halves of the same tab
               -- disagreed with each other.
               l.cached_tokens, l.cost_usd, l.billing_fraction,
               k.name AS key_name, k.key_prefix AS key_prefix,
               u.name AS user_name, t.name AS tenant_name,
               k.bound_via AS bound_via, s.subject AS subject, s.idp AS subject_idp
        FROM usage_ledger l
        LEFT JOIN api_keys k ON k.id = l.key_id
        LEFT JOIN users u ON u.id = k.user_id
        LEFT JOIN tenants t ON t.id = u.tenant_id
        LEFT JOIN subjects s ON s.id = k.subject_id
        {where}
        ORDER BY l.ts DESC LIMIT ${len(args)}
        """,
        *args,
    )
    out = [dict(r) for r in rows]
    if format == "csv":
        return _csv_response(out, "usage.csv")
    return out


def _csv_response(rows: list[dict[str, Any]], filename: str) -> Response:
    """CSV rendering of ledger rows: same columns as the JSON projection."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    if rows:
        writer.writerow(rows[0].keys())
        for r in rows:
            writer.writerow([_csv_cell(r[k]) for k in rows[0]])
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


_CSV_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _csv_cell(v: Any) -> Any:
    """UUIDs/datetimes render via str(); dicts/lists serialize as JSON.

    STRINGS are guarded against spreadsheet formula injection (audit
    finding 10b): a cell starting with =, +, -, @, TAB or CR executes as a
    formula when an admin opens the export in Excel/LibreOffice (data exfil
    via HYPERLINK, DDE in permissive configs). Reachable attacker-influenced
    values include tenant/user/key names and self-asserted enrollment
    subjects — the OWASP escape (prefix a ') neutralizes all of them. The
    prefixed cell displays the original text in every spreadsheet app.
    """
    if isinstance(v, (dict, list)):
        return json.dumps(v)
    if isinstance(v, str) and v.startswith(_CSV_FORMULA_PREFIXES):
        return "'" + v
    return v


# ---------------------------------------------------------------------------
# admin audit trail — read surface (the writes happen at every mutating route)
# ---------------------------------------------------------------------------

AUDIT_LIMIT_CAP = 500  # hard page bound (default 100) — same discipline as /admin/usage


@router.get("/audit/chain")
async def audit_chain_status(_: AdminDep) -> dict[str, Any]:
    """Verify BOTH hash-chained audit trails (2026-10-26 hardening plan P2-2).

    The executable form of "is my trail still trustworthy?" — the gateway-side
    counterpart of applygate's verify_audit_chain. Walks each table by id ASC
    and re-derives every row_hash from (prev_hash, row fields):

      * ok=false + kind="break"          -> a row's content or row_hash was
                                            edited (TAMPER).
      * ok=false + kind="fork-or-legacy" -> a row is internally consistent but
                                            does not follow its predecessor:
                                            a concurrent-writer fork, or a row
                                            written before chaining existed.
      * first_bad_id names the first offending row, so an auditor can page
        straight to it with GET /admin/audit.

    Read-only and cheap enough to poll: both trails are control-plane/tool-call
    volume, not chat volume. Registered ABOVE /audit so the literal path is
    not shadowed by the /audit handler's sibling routes.
    """
    from ..core.chain_audit import verify_admin_audit_chain, verify_mcp_call_chain

    admin_chain = await verify_admin_audit_chain()
    mcp_chain = await verify_mcp_call_chain()
    return {
        "ok": bool(admin_chain.get("ok")) and bool(mcp_chain.get("ok")),
        "admin_audit": admin_chain,
        "mcp_call_audit": mcp_chain,
    }


@router.get("/audit")
async def list_audit(
    _: AdminDep,
    limit: int = 100,
    offset: int = 0,
    since: datetime | None = None,
    until: datetime | None = None,
    actor_key_id: int | None = None,
    action: str | None = None,
    resource_type: str | None = None,
    format: str = "json",
) -> Any:
    """The admin mutation audit trail, newest-first (admin-gated like every
    sibling). Filters AND-ed: since/until (ISO timestamps), actor_key_id,
    action (exact match, e.g. 'key.create'), resource_type. Paging via
    LIMIT/OFFSET — default 100 rows, capped at 500; ``offset`` walks deeper
    (the trail is append-only, so deep pages are stable under reads).

    format=csv exports the SAME filtered projection (the /admin/usage CSV
    pattern: text/csv + Content-Disposition attachment) — the artifact an
    auditor takes away.

    APPEND-ONLY note for consumers: rows are immutable and never pruned;
    ts DESC ordering plus the (ts), (actor_key_id, ts), (resource_type,
    resource_id, ts) indexes make the common queries index-only walks.
    """
    if format not in ("json", "csv"):
        raise HTTPException(400, "format must be 'json' or 'csv'")
    capped = max(1, min(limit, AUDIT_LIMIT_CAP))
    offset = max(0, offset)

    conds: list[str] = []
    args: list[Any] = []
    if since is not None:
        args.append(since)
        conds.append(f"ts >= ${len(args)}::timestamptz")
    if until is not None:
        args.append(until)
        conds.append(f"ts <= ${len(args)}::timestamptz")
    if actor_key_id is not None:
        args.append(actor_key_id)
        conds.append(f"actor_key_id = ${len(args)}")
    if action is not None:
        args.append(action)
        conds.append(f"action = ${len(args)}")
    if resource_type is not None:
        args.append(resource_type)
        conds.append(f"resource_type = ${len(args)}")
    where = f"WHERE {' AND '.join(conds)}" if conds else ""

    args.append(capped)
    lim = f"${len(args)}"
    args.append(offset)
    off = f"${len(args)}"
    rows = await db.fetch(
        f"""
        SELECT id, ts, actor_key_id, actor_subject, actor_source, action,
               resource_type, resource_id, details, request_id, source_ip
        FROM admin_audit
        {where}
        ORDER BY ts DESC, id DESC
        LIMIT {lim} OFFSET {off}
        """,
        *args,
    )
    out = []
    for r in rows:
        d = dict(r)
        # details arrives as an asyncpg-decoded JSONB (dict); a legacy/odd
        # TEXT value degrades to {} rather than 500ing the whole page.
        if not isinstance(d.get("details"), dict):
            try:
                d["details"] = json.loads(d.get("details") or "{}")
            except (json.JSONDecodeError, TypeError):
                d["details"] = {}
        out.append(d)
    if format == "csv":
        return _csv_response(out, "admin-audit.csv")
    return out


# ---------------------------------------------------------------------------
# model endpoint pools
# ---------------------------------------------------------------------------


@router.post("/models")
async def upsert_model(body: ModelUpsert, request: Request, _: AdminDep) -> list[str]:
    """Upsert endpoint pool for a virtual model, then resync the router.

    ON CONFLICT (model_name, base_url) keeps (model, endpoint) pairs stable —
    re-submitting a pool updates priority/weight/secret in place instead of
    duplicating rows. The registry refresh makes the change live immediately
    so the operator can verify with the returned model list.
    """
    _check_list_len(body.endpoints, "endpoints")
    if not body.endpoints:
        raise HTTPException(400, "endpoints list must not be empty")
    # model_type validation (audit finding 20 — MODEL_TYPES was dead code):
    # a typo'd type ("embeding") created a pool that advertises in /v1/models
    # but 400s on every request. model_type is PER-MODEL (one pool, one
    # type — the proxy's per-endpoint[0] gate made mixed pools route chat at
    # embedding endpoints); it lives on ModelUpsert, so the check is here.
    if body.model_type is not None and body.model_type not in MODEL_TYPES:
        raise HTTPException(
            400,
            f"model_type {body.model_type!r} is invalid — must be one of {sorted(MODEL_TYPES)}",
        )
    if body.model_type is None:
        # DETECT (2026-10-26): probe the pool's first endpoint and use the
        # verdict. Detection is best-effort by design — an unreachable endpoint
        # must not block registration, so an unknown verdict falls back to
        # "chat" (the old default) and the reason is surfaced to the operator.
        detected_type = "chat"
        detected_reason = "model_type omitted; detection returned unknown — defaulted to chat"
        try:
            _found = await _probe_endpoint_type(
                body.endpoints[0].base_url,
                body.endpoints[0].api_key_secret,
                None,
            )
            if _found.type:
                detected_type = _found.type
                detected_reason = f"detected: {_found.reason}"
        except Exception:
            log.exception("model_type detection failed for %r — defaulting to chat", body.model_name)
        body = body.model_copy(update={"model_type": detected_type})
        log.info("model_type not supplied for %r — %s (%s)", body.model_name, detected_type, detected_reason)
    await _check_url_async([ep.base_url for ep in body.endpoints], "base_url")
    # Panel credentials: resolve each endpoint's api_key to ciphertext (or a
    # clear/set marker) BEFORE the write; plaintext never leaves this block.
    enc_rows = []
    for ep in body.endpoints:
        if ep.api_key is None:
            enc = None  # marker: leave existing column untouched
        elif ep.api_key == "":
            enc = ""  # marker: explicit clear
        else:
            if not secretbox.enabled():
                raise HTTPException(
                    503,
                    "credential storage disabled: PCAI_LLM_SECRETS_MASTER_KEY "
                    "is not set (deploy the Secret, or use api_key_secret by-ref)",
                )
            enc = secretbox.encrypt(ep.api_key)
        enc_rows.append((body.model_name, ep.base_url, ep.priority, ep.weight, ep.api_key_secret, enc))
    async with db.acquire() as conn, conn.transaction():
        for model_name, base_url, priority, weight, secret_name, enc in enc_rows:
            if enc is None:
                # keep the stored credential as-is
                await conn.execute(
                    "INSERT INTO model_endpoints "
                    "(model_name, base_url, priority, weight, api_key_secret, model_type) "
                    "VALUES ($1, $2, $3, $4, $5, $6) "
                    "ON CONFLICT (model_name, base_url) DO UPDATE SET "
                    "priority = $3, weight = $4, api_key_secret = $5, model_type = $6",
                    model_name,
                    base_url,
                    priority,
                    weight,
                    secret_name,
                    body.model_type,
                )
            else:
                await conn.execute(
                    "INSERT INTO model_endpoints "
                    "(model_name, base_url, priority, weight, api_key_secret, auth_token_enc, model_type) "
                    "VALUES ($1, $2, $3, $4, $5, $6, $7) "
                    "ON CONFLICT (model_name, base_url) DO UPDATE SET "
                    "priority = $3, weight = $4, api_key_secret = $5, "
                    "auth_token_enc = $6, model_type = $7",
                    model_name,
                    base_url,
                    priority,
                    weight,
                    secret_name,
                    enc if enc != "" else None,
                    body.model_type,
                )
        # REPLACE semantics for this model's pool: rows NOT in the payload are
        # pruned — without this there is no API path to shrink a pool (the
        # old rows would linger and keep serving until pod restart).
        await conn.execute(
            "DELETE FROM model_endpoints WHERE model_name = $1 AND NOT (base_url = ANY($2))",
            body.model_name,
            [ep.base_url for ep in body.endpoints],
        )
    await _refresh_registry_best_effort()
    # details: pool shape + WHICH endpoints carry panel credentials — the
    # api_key VALUES are gone (encrypted into the rows); names/count only.
    await _audit(
        request,
        action="model.upsert",
        resource_type="model",
        resource_id=body.model_name,
        details={
            "model_type": body.model_type,
            "endpoints": [
                {
                    "base_url": ep.base_url,
                    "priority": ep.priority,
                    "weight": ep.weight,
                    "api_key_secret": ep.api_key_secret,
                    "panel_credential_set": ep.api_key is not None,  # set/clear marker, never the value
                }
                for ep in body.endpoints
            ],
        },
    )
    return model_router.models()


@router.get("/models")
async def list_models(_: AdminDep) -> list[str]:
    """Current live router pool names (post-refresh state, not raw DB rows)."""
    return model_router.models()


def _iso_epoch(ts: float) -> str | None:
    """Epoch (wall clock) -> ISO-8601 UTC, or None when never stamped."""
    if not ts:
        return None
    return datetime.fromtimestamp(ts, tz=UTC).isoformat()


@router.get("/endpoints")
async def list_endpoints(_: AdminDep) -> list[dict[str, Any]]:
    """Endpoint health surface (gaps P5, passive v1): DB registry rows merged
    with the live in-memory breaker state by endpoint id — the breaker was
    previously invisible, so a dead replica was discovered by END USERS."""
    now_mono = time.monotonic()
    live: dict[str, Endpoint] = {e.id: e for e in model_router.all_endpoints()}
    out = []
    for r in await db.fetch(
        "SELECT id, model_name, base_url, priority, weight, model_type, enabled "
        "FROM model_endpoints ORDER BY model_name, priority, base_url"
    ):
        row = dict(r)
        ep = live.get(str(row["id"]))
        if ep is None:
            # not in the live router (disabled, or a refresh hasn't run):
            # no breaker state to show.
            out.append(
                {
                    **row,
                    "state": "healthy" if row["enabled"] else "disabled",
                    "consecutive_failures": 0,
                    "cooldown_remaining_s": 0,
                    "last_error": None,
                    "last_ok_at": None,
                    "last_fail_at": None,
                }
            )
            continue
        cooling = ep.cooldown_until > now_mono
        out.append(
            {
                **row,
                "state": "cooling" if cooling else "healthy",
                "consecutive_failures": ep.consecutive_failures,
                "cooldown_remaining_s": max(0, int(ep.cooldown_until - now_mono)) if cooling else 0,
                "last_error": ep.last_error or None,
                "last_ok_at": _iso_epoch(ep.last_ok_at),
                "last_fail_at": _iso_epoch(ep.last_fail_at),
            }
        )
    return out


@router.post("/models/{model_name:path}/probe")
async def probe_model(model_name: str, _: AdminDep) -> dict[str, Any]:
    """On-demand upstream probe: GET {base_url}/models (both vLLM and SGLang
    implement it) with a hard 5s timeout. Metadata discipline: NO ledger row.

    Probes the endpoint the router would pick right now (or the first
    registered endpoint when everything is cooling — cooldowns can be stale
    and the operator is explicitly asking). Errors are opaque type names.
    """
    pool = model_router.pool(model_name)
    if pool is None:
        raise HTTPException(404, f"model {model_name!r} is not registered")
    ep = pool.pick() or (pool.endpoints[0] if pool.endpoints else None)
    if ep is None:
        raise HTTPException(404, f"model {model_name!r} has no endpoints")
    started = time.monotonic()
    try:
        # Probe with the SAME headers routing would use: the stored endpoint
        # credential. A bare unauthenticated GET reads as HTTP 403 on authed
        # upstreams (MLIS/KServe front) — a false "fail" for a healthy pool.
        from .routes_proxy import _upstream_headers  # deferred: no import cycle

        async with httpx2.AsyncClient(
            timeout=httpx2.Timeout(5.0, connect=5.0),
            follow_redirects=False,
            # Same upstream-TLS contract as the model router (config.py):
            # verification unconditional; site CA bundle when configured.
            verify=settings.upstream_ca_bundle or True,
        ) as client:
            resp = await client.get(
                f"{ep.base_url}/models", headers=_upstream_headers(ep, f"probe-{uuid.uuid4().hex[:12]}")
            )
    except httpx2.HTTPError as exc:
        return {
            "ok": False,
            "status_code": 0,
            "latency_ms": int((time.monotonic() - started) * 1000),
            "endpoint": ep.base_url,
            "error": type(exc).__name__,
        }
    return {
        "ok": 200 <= resp.status_code < 300,
        "status_code": resp.status_code,
        "latency_ms": int((time.monotonic() - started) * 1000),
        "endpoint": ep.base_url,
    }


async def _probe_endpoint_type(
    base_url: str,
    api_key_secret: str | None = None,
    auth_token_enc: str | None = None,
) -> Any:
    """Probe ONE base_url and classify what it serves. NEVER raises.

    Shared by the explicit /detect-type admin action AND by registration
    (2026-10-26): the register form used to send no `model_type` at all, so
    every endpoint silently took the ModelUpsert default "chat" — an embedder
    and a reranker both showed up as `chat` in the console and, worse, a chat
    request could be routed to them. Detection at register time makes that
    default unreachable.

    Cost: one small request per surface. Registration is an explicit admin
    action, so this is the same budget the operator already accepted by
    clicking Save; the probe emits no ledger row (metadata, not spend).
    """
    from ..core import model_type_detect
    from .routes_proxy import _upstream_headers  # deferred: no import cycle

    class _Ep:  # the minimal shape _upstream_headers needs
        pass

    ep_stub = _Ep()
    ep_stub.base_url = base_url
    ep_stub.api_key_secret = api_key_secret
    ep_stub.auth_token_enc = auth_token_enc

    base = (base_url or "").rstrip("/")
    headers = _upstream_headers(ep_stub, f"detect-{uuid.uuid4().hex[:12]}")
    headers["Content-Type"] = "application/json"

    async with httpx2.AsyncClient(
        timeout=httpx2.Timeout(20.0, connect=5.0),
        follow_redirects=False,
        verify=settings.upstream_ca_bundle or True,
    ) as client:

        async def _get_card() -> dict[str, Any]:
            try:
                r = await client.get(f"{base}/models", headers=headers)
                return r.json() if r.status_code < 400 else {}
            except Exception:
                return {}

        card = await _get_card()
        served = ""
        data = card.get("data")
        if isinstance(data, list) and data and isinstance(data[0], dict):
            served = str(data[0].get("id") or "")

        async def _probe(path: str, payload: dict[str, Any]) -> Any:
            body = dict(payload)
            # The REAL served id, not the virtual pool name: a vLLM/SGLang
            # server 404s a model it does not serve, and that 404 would read
            # as "surface absent" when the truth is "wrong name".
            if served:
                body["model"] = served
            r = await client.post(f"{base}{path}", headers=headers, json=body)
            return r.status_code

        found = await model_type_detect.detect(card, _probe)

    if not found.served_id:
        found.served_id = served
    return found


@router.post("/models/{model_name:path}/detect-type")
async def detect_model_type(model_name: str, _: AdminDep) -> dict[str, Any]:
    """Probe an endpoint and report what TYPE it serves, with the evidence.

    WHY: registering a pool meant knowing the taxonomy by hand, and a mis-typed
    pool fails silently in BOTH directions — a chat pool typed `decision`
    vanishes from /v1/models and the ladder, while a decision pool typed `chat`
    accepts a request it can only 500 on. This asks the endpoint instead.

    COST DISCIPLINE: each probed surface is a real upstream request (a 1-token
    chat, a short embedding, two tiny documents, a yes/no decision). That is
    GPU work, so this is an EXPLICIT admin action — it never runs on a registry
    refresh and writes nothing. It also emits NO ledger row: detection is
    metadata, and polluting spend with probe traffic would be dishonest.

    Returns the detected type PLUS every probed status, so an operator can see
    why the verdict came out as it did. `type: ""` means "could not tell" — an
    honest unknown rather than a guess, because a wrong guess is the exact
    failure this exists to prevent.
    """
    pool = model_router.pool(model_name)
    if pool is None:
        raise HTTPException(404, f"model {model_name!r} is not registered")
    ep = pool.pick() or (pool.endpoints[0] if pool.endpoints else None)
    if ep is None:
        raise HTTPException(404, f"model {model_name!r} has no endpoints")

    found = await _probe_endpoint_type(ep.base_url, ep.api_key_secret, ep.auth_token_enc)

    return {
        "model": model_name,
        "endpoint": ep.base_url,
        "served_id": found.served_id,
        "type": found.type,
        "current_type": ep.model_type,
        "reason": found.reason,
        "evidence": found.evidence,
        # A one-click apply: the operator still calls PUT /admin/models.
        "suggested_model_type": found.type or None,
    }


@router.delete("/models/{model_name:path}")
async def delete_model(model_name: str, request: Request, _: AdminDep) -> dict[str, Any]:
    """Remove a virtual model entirely: every endpoint row for the name is
    deleted and the pool vanishes from routing on refresh.

    `{model_name:path}` — model names may contain slashes ("Qwen/
    Qwen3.8-27B-FP8"): a plain path param matches a single segment only, and
    even a %2F-encoded URL is decoded back into the path before routing, so
    the plain form 404'd on every slash-named pool (console "remove" bug).

    Graceful degradation for references: profile cascade entries skip
    not-registered models (the cascade attempt list reports them); keys'
    allowed_models entries simply never match. No data loss either way.
    """
    row = await db.fetchrow("SELECT count(*) AS n FROM model_endpoints WHERE model_name = $1", model_name)
    n = int(row["n"]) if row else 0
    if n == 0:
        raise HTTPException(404, f"model {model_name!r} is not registered")
    async with db.acquire() as conn, conn.transaction():
        # conn.fetch (NOT fetchval): fetchval returns the FIRST ROW's first
        # COLUMN — a scalar id — and len(scalar) raised TypeError -> 500 on
        # every successful-path delete (the fake-DB test returned a list and
        # masked exactly this). fetch returns the deleted rows.
        deleted = await conn.fetch("DELETE FROM model_endpoints WHERE model_name = $1 RETURNING id", model_name)
        removed = len(deleted)
    await _refresh_registry_best_effort()
    await _audit(
        request,
        action="model.delete",
        resource_type="model",
        resource_id=model_name,
        details={"endpoints_removed": removed},
    )
    return {"deleted": True, "model": model_name, "endpoints_removed": removed, "live_models": model_router.models()}


# ---------------------------------------------------------------------------
# per-model fallback chains (router extras, review §4a) — admin-curated
# failover independent of the profile ordering. The WALK lives in
# core.model_fallbacks + routes_proxy; these endpoints only curate the
# chain rows. The table shape is the contract between the halves (same
# split as guardrails): the admin plane cannot grow a dependency on the
# hot path.
# ---------------------------------------------------------------------------


class FallbackChainReplace(BaseModel):
    """Body of PUT /admin/models/{model_name}/fallbacks — the full desired
    chain, in walk order. Replace-all semantics (same rationale as budgets /
    mcp-grants / guardrail attachments): a diff would leave stale candidates
    routing live traffic to a model the operator believes is detached."""

    fallbacks: list[str] = Field(default_factory=list)


async def _validate_fallback_chain(model_name: str, fallbacks: list[str]) -> list[str]:
    """Validate a chain payload; return the deduped, order-preserving list.

    Validation happens HERE, at save time, against the LIVE registry
    snapshot — dead config must fail loudly, never silently (the guardrail
    attachment lesson). Every name must be a registered model; self-fallback
    is refused (the primary IS the first attempt); duplicates collapse
    (set semantics: a double-ticked model is one candidate). The cap is
    applied last so an over-long chain is rejected, not silently truncated.
    """
    _check_list_len(fallbacks, "fallbacks")
    known = {r["model_name"] for r in await db.fetch("SELECT DISTINCT model_name FROM model_endpoints")}
    if model_name not in known:
        raise HTTPException(404, f"model {model_name!r} is not registered")
    unknown = [n for n in fallbacks if n not in known]
    if unknown:
        raise HTTPException(
            400, f"unknown fallback model name(s): {unknown} — register them under POST /admin/models first"
        )
    deduped: list[str] = []
    for n in fallbacks:
        if n == model_name:
            raise HTTPException(400, f"model {model_name!r} cannot fall back to itself")
        if n not in deduped:
            deduped.append(n)
    if len(deduped) > max(0, settings.model_fallback_max):
        raise HTTPException(
            400,
            f"chain length {len(deduped)} exceeds PCAI_LLM_MODEL_FALLBACK_MAX={settings.model_fallback_max}",
        )
    return deduped


@router.get("/models/{model_name:path}/fallbacks")
async def get_fallbacks(model_name: str, _: AdminDep) -> dict[str, Any]:
    """A model's fallback chain (rank-ordered; `{model: [], max: N}`)."""
    rows = await db.fetch(
        "SELECT fallback_model, rank FROM model_fallbacks WHERE model_name = $1 ORDER BY rank ASC, fallback_model ASC",
        model_name,
    )
    return {
        "model": model_name,
        "fallbacks": [r["fallback_model"] for r in rows],
        "max": settings.model_fallback_max,
    }


@router.put("/models/{model_name:path}/fallbacks")
async def put_fallbacks(model_name: str, body: FallbackChainReplace, request: Request, _: AdminDep) -> dict[str, Any]:
    """Replace a model's whole fallback chain atomically (DELETE all + INSERT
    the list in ONE transaction; declarative state). Ranks are assigned by
    list position — the array order IS the walk order, no separate rank
    field to drift out of sync.

    `{model_name:path}` — model names contain slashes ("Qwen/Qwen3.8-27B-
    FP8"); a plain path param matches one segment only (the DELETE /models
    lesson, verbatim).
    """
    deduped = await _validate_fallback_chain(model_name, body.fallbacks)
    async with db.acquire() as conn, conn.transaction():
        await conn.execute("DELETE FROM model_fallbacks WHERE model_name = $1", model_name)
        if deduped:
            await conn.executemany(
                "INSERT INTO model_fallbacks (model_name, rank, fallback_model) VALUES ($1, $2, $3)",
                [(model_name, rank, name) for rank, name in enumerate(deduped)],
            )
    await _audit(
        request,
        action="model.fallbacks.set",
        resource_type="model",
        resource_id=model_name,
        details={"fallbacks": deduped},
    )
    return {"model": model_name, "fallbacks": deduped}


@router.get("/keys/{key_id}/mcp-grants")
async def get_mcp_grants(key_id: uuid.UUID, _: AdminDep) -> list[dict[str, Any]]:
    """The key's EFFECTIVE MCP grant state (console ACL editor): the key's
    own TRUE grants AND allowed=FALSE deny rows — each flagged with `allowed`
    so the console can render held-deny state (struck-through pairs) instead
    of pretending a documented deny is invisible — UNIONed with the owner
    profile's LIVE grant presets.

    The profile rows matter for the console's deny math (D-F1): under a
    live-grants profile those pairs are callable without an mcp_tool_acls
    row of their own, so a tree built from `mcp_tool_acls` alone renders
    them unticked — and unticking them could never write a deny, because
    the save computes denies from "what was held before". They are returned
    with `allowed=true` and `"source": "profile"` (the field is absent on
    per-key rows, so the pre-existing shape of those rows is unchanged).
    A key-level FALSE row still wins over the preset — `load_grants`
    subtracts it, and it is returned as the pair's single (denied) row, so
    the union never contradicts the enforcement path.
    """
    if not await _exists("api_keys", key_id):
        raise HTTPException(404, f"key {key_id} not found")
    rows = await db.fetch(
        "SELECT server, tool, allowed FROM mcp_tool_acls WHERE key_id = $1 ORDER BY server, tool",
        key_id,
    )
    out = [{"server": r["server"], "tool": r["tool"], "allowed": bool(r["allowed"])} for r in rows]
    known = {(r["server"], r["tool"]) for r in rows}
    # FAIL-OPEN GUARD (same posture as federation.load_grants): the console
    # must still render the key's own rows when the profile layer faults.
    try:
        live = await load_live_profile_grants(key_id)
    except Exception:
        log.exception("mcp grants GET: profile grants lookup failed (key %s) — key rows only", key_id)
        live = None
    if live is not None:
        profile_pairs, key_denies = live
        # (profile_pairs - key_denies): a documented deny is not resurrected
        # by the profile layer; - known: an existing row (either polarity)
        # is authoritative and already in `out`.
        for server, tool in sorted(profile_pairs - key_denies - known):
            out.append({"server": server, "tool": tool, "allowed": True, "source": "profile"})
    return out


@router.put("/keys/{key_id}/mcp-grants")
async def put_mcp_grants(key_id: uuid.UUID, body: McpGrantsReplace, request: Request, _: AdminDep) -> dict[str, Any]:
    """Replace-all the key's MCP ACL rows in one transaction (declarative
    state, same rationale as budgets).

    Deny rows (D-F1, the operator's #1 ask): `denies` writes allowed=FALSE
    rows for pairs the operator explicitly unticked-but-was-offered. The
    replace DELETE is scoped to the TRUE rows only — a grants-only PUT (the
    old console body shape) must NOT silently erase existing documented
    denies, because under the DEFAULT tool_relay_grant_mode="scope" the
    relay merge re-adds the whole policy-admitted catalog AFTER the grant
    filter and those FALSE rows are the only thing holding a pair back
    (load-bearing policy, not bookkeeping). A pair listed in BOTH `grants`
    and `denies` resolves to the grant: the deny is dropped from the write
    set (the TRUE row is what the relay checks — storing its opposite
    would be contradictory bookkeeping), and BOTH inserts are
    ON CONFLICT-protected against the (key_id, server, tool) PK.

    That protection is what makes the re-enable gesture work: `PATCH
    mode="off"` converts held TRUE rows to FALSE rows (… DO UPDATE SET
    allowed = FALSE), so the console's per-tool re-enable — tick the
    struck-through pair, Save → it arrives in `grants` while its FALSE row
    still exists — would otherwise die on the PK with a 500 and leave the
    deny in force. Grant beats deny on explicit re-state: the TRUE insert
    upserts `allowed = TRUE` over the surviving FALSE row.
    """
    if not await _exists("api_keys", key_id):
        raise HTTPException(404, f"key {key_id} not found")
    granted = {(g.server, g.tool) for g in body.grants}
    deny_pairs = [(d.server, d.tool) for d in body.denies if (d.server, d.tool) not in granted]
    async with db.acquire() as conn, conn.transaction():
        # replace semantics apply to the GRANT set only; deny rows survive
        # unless the body's `denies` explicitly re-states them.
        await conn.execute("DELETE FROM mcp_tool_acls WHERE key_id = $1 AND allowed", key_id)
        if body.grants:
            await conn.executemany(
                # ON CONFLICT DO UPDATE (not DO NOTHING): re-granting a pair
                # that holds a deny row is the PRIMARY re-enable workflow
                # (PATCH mode="off" turned it FALSE; ticking it in the
                # console + Save sends it here) — the grant must flip the
                # surviving row TRUE, not 500 on the PK and not stay denied.
                "INSERT INTO mcp_tool_acls (key_id, server, tool, allowed) "
                "VALUES ($1,$2,$3,TRUE) ON CONFLICT (key_id, server, tool) DO UPDATE SET allowed = TRUE",
                [(key_id, g.server, g.tool) for g in body.grants],
            )
        if deny_pairs:
            await conn.executemany(
                # ON CONFLICT DO NOTHING: a pair already holding a FALSE row
                # must not fail the whole replace on the PK.
                "INSERT INTO mcp_tool_acls (key_id, server, tool, allowed) "
                "VALUES ($1,$2,$3,FALSE) ON CONFLICT DO NOTHING",
                [(key_id, server, tool) for server, tool in deny_pairs],
            )
    await _audit(
        request,
        action="mcp.acl.set",
        resource_type="key",
        resource_id=str(key_id),
        details={
            "grants": [[g.server, g.tool] for g in body.grants],
            "denies": [[server, tool] for server, tool in deny_pairs],
        },
    )
    return {"key_id": str(key_id), "grants": len(body.grants), "denies": len(deny_pairs)}


MCP_ACL_SERVER_MODES = ("off", "on", "catalog")


class McpAclServerSet(BaseModel):
    """Body for PATCH /keys/{key_id}/mcp-grants/{server} (the one-press
    per-server deny op): off = deny the whole collection (the key's held
    pairs ∪ the server's live catalog), on = clear the server's denies,
    catalog = restore the server's deny rows exactly as they stand
    (capture → delete → re-insert; grants are never touched)."""

    mode: Literal["off", "on", "catalog"]


# CAP (D-F1): the "off" write set is bounded — a server offering thousands
# of tools must not turn one console click into an unbounded executemany.
MCP_ACL_SERVER_MAX_PAIRS = 500


@router.patch("/keys/{key_id}/mcp-grants/{server}")
async def patch_mcp_grants_server(
    key_id: uuid.UUID, server: str, body: McpAclServerSet, request: Request, _: AdminDep
) -> dict[str, Any]:
    """Per-server deny control for one key — the live one-press op behind
    the console's ✕ disable-all / enable-all buttons (no Save detour).

    Modes (audit D-F1 / fused-audit §H.3):
      "off"     → write allowed=FALSE rows for the UNION of the pairs the
                  key currently holds on this server (TRUE rows + the
                  profile's live-grant presets) and the server's live
                  catalog. This is what makes "disable all" actually hold
                  under the default tool_relay_grant_mode="scope": the
                  scope merge re-adds the whole policy-admitted catalog
                  AFTER the grant filter, so a deny row per offered tool is
                  the only thing that holds it back. Bounded: the write set
                  is capped at MCP_ACL_SERVER_MAX_PAIRS total pairs — a
                  larger server is a 400 (nothing is written).
      "on"      → delete this server's FALSE rows for the key (re-enable;
                  grants themselves are untouched).
      "catalog" → restore this server's deny rows exactly as they stand: the
                  pre-op FALSE set is captured in the transaction, the
                  server's FALSE rows are cleared, and that exact set is
                  re-written. Grants are never touched — no held TRUE row is
                  converted into a deny, and no deny that was documented is
                  lost (D-F1 blocker 2: the old re-insert wrote FALSE for the
                  HELD pairs, i.e. for pairs whose TRUE rows the DELETE had
                  left alone, and died on the (key_id, server, tool) PK with
                  a 500 whenever the key held anything on the server).
    Unknown key/server → 404; unknown mode is a 422 (Literal).
    """
    if not await _exists("api_keys", key_id):
        raise HTTPException(404, f"key {key_id} not found")
    servers = {sv["name"]: sv for sv in await registry.mcp_servers()}
    target = servers.get(server)
    if target is None:
        raise HTTPException(404, f"MCP server {server!r} is not in the enabled registry")

    # The key's currently-held pairs on THIS server: per-key TRUE rows plus
    # the profile's live-grant presets when the owner profile runs them
    # (load_live_profile_grants returns None for stamps-only/no profile —
    # the stamp rows are already in the TRUE set).
    rows = await db.fetch(
        "SELECT server, tool FROM mcp_tool_acls WHERE key_id = $1 AND allowed",
        key_id,
    )
    held_true = {(r["server"], r["tool"]) for r in rows if r["server"] == server}
    profile_pairs: set[tuple[str, str]] = set()
    try:
        live = await load_live_profile_grants(key_id)
    except Exception:
        log.exception("mcp acl server patch: profile grants lookup failed (key %s) — key rows only", key_id)
        live = None
    if live is not None:
        profile_pairs = {pair for pair in live[0] if pair[0] == server}
    held = held_true | profile_pairs

    false_rows: list[tuple[str, str]] = []
    catalog_ok = True
    if body.mode == "off":
        # live catalog of this server (the same bounded probe the console
        # "load tools" uses): deny the offered tools too, not just the held
        # ones — otherwise a scope-merged pair the key never held survives.
        try:
            from .routes_mcp import _fetch_tools, build_mcp_headers  # deferred: no import cycle

            tools = await _fetch_tools(
                target["url"],
                build_mcp_headers(
                    target.get("headers", {}),
                    target.get("auth_secret"),
                    target.get("auth_token_enc"),
                ),
                timeout_s=15.0,  # metadata call — bounded (console "load tools")
            )
        except Exception:
            # Catalog fetch failed (upstream down / timeout): degrade to the
            # HELD pairs only — a partial disable beats a 502, and the
            # operator can retry (same posture as renderGrantTree's
            # "not offered live" block). The degradation is REPORTED, not
            # silent: `catalog_ok: false` rides the response AND the audit
            # row, because a partial disable that reads as a complete one
            # would leave un-fetched catalog pairs callable under scope mode.
            log.warning("mcp acl server patch: catalog fetch for %r failed — denying held pairs only", server)
            catalog_ok = False
            tools = []
        catalog_pairs = {(server, t["tool"]) for t in tools if t.get("tool")}
        pairs = held | catalog_pairs
        if len(pairs) > MCP_ACL_SERVER_MAX_PAIRS:
            raise HTTPException(
                400,
                f"server {server!r} holds/offers {len(pairs)} tools — "
                f"the per-server deny cap is {MCP_ACL_SERVER_MAX_PAIRS}; "
                "narrow the server's allowlist first",
            )
        false_rows = sorted(pairs)
    catalog_restore: list[tuple[str, str]] = []
    async with db.acquire() as conn, conn.transaction():
        if body.mode == "off":
            if false_rows:
                await conn.executemany(
                    "INSERT INTO mcp_tool_acls (key_id, server, tool, allowed) "
                    "VALUES ($1,$2,$3,FALSE) ON CONFLICT (key_id, server, tool) DO UPDATE SET allowed = FALSE",
                    [(key_id, srv, tool) for srv, tool in false_rows],
                )
        else:
            if body.mode == "catalog":
                # CAPTURE the pre-DELETE deny set IN the transaction (D-F1
                # blocker 2, option b): "restore prior deny state" means
                # exactly these rows come back, so the mode cannot surprise
                # anyone by converting a held TRUE row into a deny — the old
                # code re-inserted FALSE for the HELD pairs (pairs that hold
                # a TRUE row, since that is what "held" means) and died on
                # the (key_id, server, tool) PK with a 500 on any key that
                # held anything on this server.
                pre = await conn.fetch(
                    "SELECT server, tool FROM mcp_tool_acls WHERE key_id = $1 AND NOT allowed AND server = $2",
                    key_id,
                    server,
                )
                catalog_restore = sorted({(r["server"], r["tool"]) for r in pre})
            # "on" clears every FALSE row of the server; "catalog" clears
            # them all too and then re-states the captured ones below — one
            # DELETE, one re-INSERT, in-transaction, no TRUE interaction.
            await conn.execute(
                "DELETE FROM mcp_tool_acls WHERE key_id = $1 AND NOT allowed AND server = $2",
                key_id,
                server,
            )
            if catalog_restore:
                await conn.executemany(
                    # ON CONFLICT DO NOTHING is belt-and-braces: every
                    # captured row was FALSE and the DELETE just removed it,
                    # so a collision would mean the capture and the delete
                    # disagreed — refuse to clobber a TRUE row either way.
                    "INSERT INTO mcp_tool_acls (key_id, server, tool, allowed) "
                    "VALUES ($1,$2,$3,FALSE) ON CONFLICT DO NOTHING",
                    [(key_id, srv, tool) for srv, tool in catalog_restore],
                )
    pairs_written = len(false_rows) if body.mode == "off" else len(catalog_restore)
    details: dict[str, Any] = {
        "server": server,
        "mode": body.mode,
        "pairs_written": pairs_written,
    }
    if body.mode == "off":
        # Truthfulness (review §1.4): a degraded "disable all" must not read
        # as a complete one in the audit trail either.
        details["catalog_ok"] = catalog_ok
    await _audit(
        request,
        action="mcp.acl.server.set",
        resource_type="key",
        resource_id=str(key_id),
        details=details,
    )
    response: dict[str, Any] = {
        "key_id": str(key_id),
        "server": server,
        "mode": body.mode,
        "pairs_written": pairs_written,
    }
    if body.mode == "off":
        response["catalog_ok"] = catalog_ok
    return response


class McpGrantsCopyFrom(BaseModel):
    """Body for the copy-from endpoint: the key whose mcp_tool_acls rows
    become the target's new grant set."""

    source_key_id: uuid.UUID


@router.post("/keys/{key_id}/mcp-grants/copy-from")
async def copy_mcp_grants_from(
    key_id: uuid.UUID, body: McpGrantsCopyFrom, request: Request, _: AdminDep
) -> dict[str, Any]:
    """Replace-all the target key's MCP grants with ANOTHER key's grant rows.

    The general repair tool behind rotate's grant carry-over (2026-09-18
    incident: a rotated key lost its federation grants and dropped from
    ~102 tools to 2 under default-deny). This endpoint lets an operator
    re-grant ANY key from any donor key — or from a reference key holding
    the fleet catalog — without clicking every pair back in the console.

    Same replace-all semantics as PUT /keys/{key_id}/mcp-grants (the target
    is deleted-then-refilled in ONE transaction — declarative state, no
    duplicates), but the refill copies the SOURCE's rows verbatim with a
    single INSERT..SELECT, so allow AND allowed=FALSE deny rows both
    survive (the PUT hardcodes allowed=TRUE and would erase documented
    denies).

    Self-copy is a no-op, NOT a delete-then-refill: the DELETE would empty
    the target before the SELECT could read it (they are the same key), so
    the shortcut returns the current count untouched.

    Fail closed: unknown target OR unknown source → 404.
    """
    if not await _exists("api_keys", key_id):
        raise HTTPException(404, f"key {key_id} not found")
    if body.source_key_id == key_id:
        n = await db.fetchval("SELECT count(*) FROM mcp_tool_acls WHERE key_id = $1", key_id)
        await _audit(
            request,
            action="mcp.acl.copy",
            resource_type="key",
            resource_id=str(key_id),
            details={"source_key_id": str(body.source_key_id), "grants": int(n or 0), "self_copy": True},
        )
        return {"key_id": str(key_id), "source_key_id": str(key_id), "grants": int(n or 0)}
    if not await _exists("api_keys", body.source_key_id):
        raise HTTPException(404, f"source key {body.source_key_id} not found")
    async with db.acquire() as conn, conn.transaction():
        await conn.execute("DELETE FROM mcp_tool_acls WHERE key_id = $1", key_id)
        # execute returns the command tag ("INSERT 0 <n>"); n = rows copied
        # — an INSERT..SELECT without RETURNING yields no result set, so
        # fetchval would return None regardless of rows written.
        tag = await conn.execute(
            "INSERT INTO mcp_tool_acls (key_id, server, tool, allowed) "
            "SELECT $1, server, tool, allowed FROM mcp_tool_acls WHERE key_id = $2",
            key_id,
            body.source_key_id,
        )
        try:
            copied = int(tag.split()[-1])
        except (AttributeError, ValueError):
            copied = 0
    await _audit(
        request,
        action="mcp.acl.copy",
        resource_type="key",
        resource_id=str(key_id),
        details={"source_key_id": str(body.source_key_id), "grants": copied},
    )
    return {
        "key_id": str(key_id),
        "source_key_id": str(body.source_key_id),
        "grants": copied,
    }


@router.post("/mcp-servers")
async def upsert_mcp_server(body: McpServerUpsert, request: Request, _: AdminDep) -> dict[str, Any]:
    """Upsert an MCP server registration, then resync the registry."""
    await _check_url_async([body.url], "url")
    if body.tool_policy not in ("all", "allowlist"):
        raise HTTPException(400, "tool_policy must be 'all' or 'allowlist'")
    _check_json_size(body.headers, "headers")
    # auth_token tri-state (audit finding 10c — the clear marker used to be a
    # no-op that the audit trail recorded as a successful clear):
    #   None (omitted)  -> keep any existing stored token (COALESCE below)
    #   ""              -> EXPLICIT CLEAR (writes NULL — the COALESCE is skipped)
    #   non-empty       -> encrypt + store the new token
    if body.auth_token:
        if not secretbox.enabled():
            raise HTTPException(
                503,
                "credential storage disabled: PCAI_LLM_SECRETS_MASTER_KEY is "
                "not set (deploy the Secret, or use auth_secret by-ref)",
            )
        enc: str | None = secretbox.encrypt(body.auth_token)
        token_write: bool = True
    elif body.auth_token == "":
        enc = None
        token_write = True  # explicit clear — write NULL, skip the COALESCE
    else:
        enc = None
        token_write = False  # omitted — keep existing (COALESCE)
    row = await db.fetchrow(
        "INSERT INTO mcp_servers (name, url, headers_json, auth_secret, auth_token_enc, "
        "tool_policy, allowed_tools_json, enabled, priority) "
        "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9) "
        "ON CONFLICT (name) DO UPDATE SET url = $2, headers_json = $3, "
        "auth_secret = $4, "
        + (
            # explicit set-or-clear: write the resolved value, never keep the old one
            "auth_token_enc = $5, "
            if token_write
            # omitted: keep the existing stored token
            else "auth_token_enc = COALESCE($5, mcp_servers.auth_token_enc), "
        )
        + "tool_policy = $6, allowed_tools_json = $7, enabled = $8, priority = $9 "
        "RETURNING id, name, url, auth_secret, "
        "(auth_token_enc IS NOT NULL) AS auth_configured, "
        "tool_policy, allowed_tools_json, enabled, priority",
        body.name,
        body.url,
        # headers_json is a TEXT column — serialize explicitly. (asyncpg has
        # no asyncpg.Json wrapper in 0.31; passing one 500'd every call.)
        json.dumps(body.headers),
        body.auth_secret,
        enc,
        body.tool_policy,
        json.dumps(body.allowed_tools),
        body.enabled,
        body.priority,
    )
    assert row is not None
    await _refresh_registry_best_effort()
    # details: registration shape + credential PRESENCE markers. auth_token
    # and header VALUES never appear — the audit records the DB OUTCOME
    # (auth_configured from RETURNING), not the request intent (audit fix
    # 10c: a "cleared" marker on a token that survived was a lying trail).
    # The keys() guard keeps hand-built fakes (no auth_configured column)
    # from 500ing the audit read — real asyncpg always has it.
    _configured = bool(row["auth_configured"]) if "auth_configured" in row else bool(body.auth_token)
    await _audit(
        request,
        action="mcp.server.upsert",
        resource_type="mcp_server",
        resource_id=body.name,
        details={
            "url": body.url,
            "auth_secret": body.auth_secret,
            "auth_token_set": _configured,
            "auth_token_cleared": body.auth_token == "" and not _configured,
            "tool_policy": body.tool_policy,
            "allowed_tools": body.allowed_tools,
            "enabled": body.enabled,
            "priority": body.priority,
            "header_names": sorted(body.headers),
        },
    )
    return dict(row)


@router.get("/mcp-servers")
async def list_mcp_servers(_: AdminDep) -> list[dict[str, Any]]:
    """Current registry MCP server state (from the registry cache).

    auth_token_enc ciphertext is stripped — the console only needs to know
    WHETHER a panel credential is configured.
    """
    # CREDENTIAL-ISH header redaction (audit finding 20): operators who
    # park an Authorization/Cookie/api-key header in `headers` instead of
    # auth_secret/auth_token got its CLEARTEXT value echoed on every console
    # refresh — inconsistent with the file's own "never returned by any
    # endpoint" posture for panel credentials. Values are now redacted to a
    # presence marker; innocent header names/values (X-Memory-Dataset,
    # X-MCP-Timeout...) pass through untouched.
    # SAME vocabulary as core.audit._SECRET_KEY_RE (audit P2-1: the lists
    # drifted once - X-Dataset-Password was redacted in the audit trail but
    # echoed in cleartext here). Imported, not copied, so they cannot drift.
    return _redact_server_headers(await registry.mcp_servers())


class McpServerPatch(BaseModel):
    """Partial update — only provided fields change (console toggles)."""

    enabled: bool | None = None
    tool_policy: str | None = None
    allowed_tools: list[str] | None = Field(default=None, max_length=200)
    # Fleet priority (lower = preferred): None = keep the row's current
    # value — same preservation rule as `enabled` (omitting a field must
    # never silently reset it; 0 is a legal explicit value despite being
    # falsy, hence the is-None check, not truthiness).
    priority: int | None = None


@router.patch("/mcp-servers/{name}")
async def patch_mcp_server(name: str, body: McpServerPatch, request: Request, _: AdminDep) -> dict[str, Any]:
    """Toggle/adjust one server WITHOUT resubmitting the whole row — the
    console's inline checklist and policy dropdown hit this."""
    row = await db.fetchrow(
        "SELECT url, headers_json, auth_secret, tool_policy, allowed_tools_json, enabled, priority "
        "FROM mcp_servers WHERE name = $1",
        name,
    )
    if row is None:
        raise HTTPException(404, f"MCP server {name!r} not found")
    tool_policy = body.tool_policy if body.tool_policy is not None else row["tool_policy"]
    if tool_policy not in ("all", "allowlist"):
        raise HTTPException(400, "tool_policy must be 'all' or 'allowlist'")
    allowed = body.allowed_tools if body.allowed_tools is not None else json.loads(row["allowed_tools_json"] or "[]")
    # enabled preservation (security audit): omitting `enabled` must keep the
    # row's CURRENT value — defaulting to True silently RE-ENABLED a
    # kill-switched server when the console sent a policy-only edit.
    enabled = body.enabled if body.enabled is not None else bool(row["enabled"])
    # priority preservation: same rule as `enabled` — omitted = keep; an
    # explicit 0 must survive (falsy-trap lesson, checked via is-None).
    # row.get (asyncpg Record supports it) keeps older rows without the
    # column readable during the migration window.
    priority = body.priority if body.priority is not None else int(row.get("priority") or 0)
    updated = await db.fetchrow(
        "UPDATE mcp_servers SET enabled = $2, tool_policy = $3, allowed_tools_json = $4, priority = $5 "
        "WHERE name = $1 "
        "RETURNING id, name, url, auth_secret, tool_policy, allowed_tools_json, enabled, priority",
        name,
        enabled,
        tool_policy,
        json.dumps(allowed),
        priority,
    )
    assert updated is not None
    await _refresh_registry_best_effort()
    # details: only the fields the PATCH actually changed (body.model_fields_set).
    await _audit(
        request,
        action="mcp.server.patch",
        resource_type="mcp_server",
        resource_id=name,
        details={field: getattr(body, field) for field in sorted(body.model_fields_set)},
    )
    out = dict(updated)
    out["allowed_tools"] = json.loads(out.pop("allowed_tools_json") or "[]")
    return out


class ModelCost(BaseModel):
    model_name: str = Field(min_length=1, max_length=200)
    # ge + allow_inf_nan=False (audit P1-1): NaN passes every `< 0` comparison,
    # so the old guard was a no-op for it — and a stored NaN poisons
    # SUM(cost_usd) forever (the ledger is append-only) plus 500s the console
    # rollups and the USD budget gate. Upper bound = the NUMERIC(12,8) column max (9999.99999999) — anything larger is rejected by Postgres at write time (asyncpg numeric overflow -> 500);
    # quantize() raises InvalidOperation for any rate >= ~1e26.
    input_per_m: float = Field(0.0, ge=0, allow_inf_nan=False, le=9999.99999999)
    output_per_m: float = Field(0.0, ge=0, allow_inf_nan=False, le=9999.99999999)
    cached_per_m: float = Field(0.0, ge=0, allow_inf_nan=False, le=9999.99999999)


@router.post("/costs")
async def set_model_cost(body: ModelCost, request: Request, _: AdminDep) -> dict[str, Any]:
    """Price a model: USD per 1M tokens (input / output / cached).

    Costs are computed AND STORED on each ledger row at request time —
    repricing later never rewrites history. Unpriced models ledger NULL.
    """
    for v in (body.input_per_m, body.output_per_m, body.cached_per_m):
        if v < 0:
            raise HTTPException(400, "rates must be >= 0")
    await costs.set_cost(body.model_name, body.input_per_m, body.output_per_m, body.cached_per_m)
    await _audit(
        request,
        action="costs.set",
        resource_type="model",
        resource_id=body.model_name,
        details={"input_per_m": body.input_per_m, "output_per_m": body.output_per_m, "cached_per_m": body.cached_per_m},
    )
    return {"model_name": body.model_name, "priced": True}


@router.get("/costs")
async def list_model_costs(_: AdminDep) -> list[dict[str, Any]]:
    """All configured model prices."""
    return await costs.list_costs()


@router.delete("/costs/{model_name:path}")
async def delete_model_cost(model_name: str, request: Request, _: AdminDep) -> dict[str, Any]:
    """Remove a model's price row — a renamed/decommissioned model leaves the
    old name's price behind forever, and the console had no way to drop it.

    `{model_name:path}` — model names may contain slashes ("Qwen/
    Qwen3.8-27B-FP8"): a plain path param matches a single segment only, and
    even a %2F-encoded URL is decoded back into the path before routing
    (same reason DELETE /models uses it).

    Ledger history is untouched — costs were computed AND STORED per row at
    request time; deletion only affects future requests (they ledger NULL).
    """
    if not await costs.delete_cost(model_name):
        raise HTTPException(404, f"model {model_name!r} is not priced")
    await _audit(request, action="costs.delete", resource_type="model", resource_id=model_name)
    return {"deleted": True, "model": model_name}


@router.get("/mcp-servers/{name}/tools")
async def list_server_tools(name: str, _: AdminDep) -> dict[str, Any]:
    """LIVE list_tools from one registered server — the admin sees the FULL
    catalog (unfiltered; tenant filtering only ever happens on /mcp)."""
    from .routes_mcp import _fetch_tools

    servers = {sv["name"]: sv for sv in await registry.mcp_servers()}
    target = servers.get(name)
    if target is None:
        raise HTTPException(404, f"MCP server {name!r} is not in the enabled registry")
    from .routes_mcp import build_mcp_headers

    try:
        tools = await _fetch_tools(
            target["url"],
            build_mcp_headers(
                target.get("headers", {}),
                target.get("auth_secret"),
                target.get("auth_token_enc"),
            ),
            timeout_s=15.0,  # metadata call — bounded (console "load tools")
        )
    except Exception:
        raise HTTPException(502, f"server {name!r} did not answer list_tools") from None
    # full catalog + which tools the server's own policy admits (admin edits
    # the allowlist HERE; key/profile grant pickers see only allowed ones)
    allowed = set(target.get("allowed_tools") or [])
    policy = target.get("tool_policy", "allowlist")
    return {
        "server": name,
        "policy": policy,
        "allowed_tools": sorted(allowed),
        "tools": [{**t, "allowed": policy == "all" or t["tool"] in allowed} for t in tools],
    }


@router.post("/registry/refresh")
async def refresh_registry(_: AdminDep) -> dict[str, Any]:
    """Manual resync of router/registry caches from the DB.

    Needed after out-of-band changes (a K8s Secret for an upstream key was
    rotated, an endpoint was toggled by tooling) that bypass the admin API.
    Also drops the relay's tool-catalog TTL cache: an operator forcing a
    refresh wants FRESH catalogs, not up-to-120s-stale ones.
    """
    # The MANUAL refresh surfaces the error (the operator asked for a resync
    # — silently keeping a stale snapshot would defeat the endpoint's whole
    # purpose); the post-mutation wrapper is the one that degrades quietly.
    await registry.refresh()
    from .routes_proxy import catalog_cache_clear  # deferred: no import cycle

    catalog_cache_clear()
    _guardrail_spec_cache_clear()  # the operator's resync-everything button clears both data-plane caches
    return {"models": model_router.models(), "mcp_servers": _redact_server_headers(await registry.mcp_servers())}


# ---------------------------------------------------------------------------
# profiles (budget-driven model cascade, 0.2)
# ---------------------------------------------------------------------------


class BudgetPreset(BaseModel):
    """One key-creation budget preset (a template, not live state).

    Exactly ONE limit per entry, the same contract the live-key route
    (POST /admin/keys/{id}/budgets) enforces:
      limit_tokens -> a row in budgets (token budget)
      limit_usd    -> a row in cost_budgets (dollar budget)
    Both kinds are stamped onto a key created with the profile. The
    exactly-one rule is validated in the ROUTE (not here) so the
    enrollment path, which reads raw defaults_json written by an older
    build, gets the identical refusal from core.budgets.split_preset_budgets.
    """

    model: str = "*"
    period: str  # day|week|month (validated below)
    # Bounds mirror the COLUMNS (core.budgets.MAX_LIMIT_*), not a round
    # number: a preset above the column maximum would otherwise pass this
    # model, be saved, and 500 at the INSERT on every later stamp.
    limit_tokens: int | None = Field(None, gt=0, le=MAX_LIMIT_TOKENS)
    limit_usd: float | None = Field(None, gt=0, le=MAX_LIMIT_USD)


class McpGrant(BaseModel):
    server: str = Field(min_length=1, max_length=200)
    tool: str = Field(min_length=1, max_length=200)


class LimitPresets(BaseModel):
    """Optional rate/concurrency presets a profile stamps onto keys.

    Any field left null is ignored (the key keeps whatever the create
    request or API default specified). Propagated to EXISTING attached keys
    only via the explicit propagate-limits action — never as a silent
    side effect of saving the profile.
    """

    rate_limit_rpm: int | None = Field(None, gt=0)
    rate_limit_tpm: int | None = Field(None, gt=0)
    max_concurrent: int | None = Field(None, ge=0)


class MemoryPreset(BaseModel):
    """Enrollment-memory preset (review §2): `provision: true` opts every
    redeem under this profile into per-user memory datasets (the redeemer's
    explicit `memory_opt_in: false` still declines — the human at the
    keyboard beats the preset)."""

    provision: bool


class ProfileCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    model_order: list[str] = Field(default_factory=list, max_length=100)
    # Key-creation presets: budgets + MCP tool grants stamped onto every key
    # created with this profile ("profiles as defaults").
    budgets: list[BudgetPreset] = Field(default_factory=list, max_length=100)
    mcp_grants: list[McpGrant] = Field(default_factory=list, max_length=200)
    # §3a live grants: mcp_grants evaluate AT CALL TIME for every key of
    # this profile (per-key rows stay exceptions) instead of stamping once
    # at creation. Editing this profile re-scopes running keys on their
    # NEXT call — the explicit trust-model decision the roadmap named.
    live_grants: bool = False
    # Named grant-profile preset (P0-3, 2026-10-26): when set, the named
    # preset (observe | egress | mutate | admin — mcpf.federation
    # MCP_PROFILE_PRESETS, resolved from the ONE server-class constant) is
    # expanded against the live per-server tool catalogs and UNIONED into
    # the profile's mcp_grants. Absent (the default) leaves the stored
    # defaults_json byte-identical in shape — no preset is implied, and no
    # existing profile/key grant set changes unless an operator stamps one.
    mcp_grant_preset: Literal["observe", "egress", "mutate", "admin"] | None = None
    limits: LimitPresets | None = None
    # Off-peak billing fraction for keys under this profile (null = inherit
    # the gateway default PCAI_LLM_OFFPEAK_FRACTION during low-demand).
    offpeak_fraction: float | None = Field(None, gt=0.0, le=1.0)
    # Per-profile model-routing rollout (consumed by the proxy through
    # core.model_routing.profile_routing_config, TTL-cached ~60s per user —
    # console edits take effect within a minute, no restart). 'off' keeps
    # today's byte-for-byte behavior; routing_min_model is the ladder FLOOR
    # and must name a model IN model_order (validated fail-closed below) —
    # a floor off the ladder is silently unusable downstream.
    routing_policy: Literal["off", "shadow", "active"] = "off"
    routing_min_model: str = ""
    # Enrollment-time memory preset (review §2): keys created through codes
    # bound to this profile provision a private RAG dataset unless the
    # redeemer explicitly declines. Stored in defaults_json under "memory";
    # null = the profile declares nothing (the request's answer, else the
    # deployment default, applies).
    memory: MemoryPreset | None = None


class McpGrantsReplace(BaseModel):
    grants: list[McpGrant] = Field(default_factory=list, max_length=200)
    # allowed=FALSE deny rows written alongside the grants (D-F1): the
    # console sends pairs it unticked-that-were-held so an mcp-scoped key
    # under the default "scope" grant mode keeps them OFF the relay
    # catalog. Optional + defaulted empty so every existing caller body
    # (PUTs without the field) stays valid.
    denies: list[McpGrant] = Field(default_factory=list, max_length=500)


class ProfileCreated(BaseModel):
    id: uuid.UUID
    name: str
    model_order: list[str]


class UserAssignProfile(BaseModel):
    profile_id: uuid.UUID | None  # null clears the profile


# Stamp-time catalog resolution cap (P0-3): one live list_tools per classified
# server, bounded like the console's "load tools" metadata probe. A wedged
# server burns at most this long before it is reported unavailable.
_PRESET_STAMP_TIMEOUT_S = 15.0


async def _preset_stamp_catalogs() -> tuple[dict[str, list[dict[str, Any]]], list[str]]:
    """Live per-server tool catalogs for EVERY classified server
    (mcpf.federation.MCP_SERVER_CLASSES) — the input preset_pairs resolves a
    named preset against. Returns (catalogs, unavailable): a server that is
    unregistered, unreachable, or times out lands in 'unavailable' and
    contributes NO pairs (fail-safe under-provision for a grant stamp — the
    caller must surface the gap, see create_profile). Live list_tools per
    server (same bounded shape as GET /mcp-servers/{name}/tools), NOT the
    per-key ACL rows: a preset stamps what the server OFFERS, not what some
    key happens to hold.
    """
    from .routes_mcp import _fetch_tools, build_mcp_headers  # deferred: no import cycle

    servers = {sv["name"]: sv for sv in await registry.mcp_servers()}
    catalogs: dict[str, list[dict[str, Any]]] = {}
    unavailable: list[str] = []
    for name in sorted(MCP_SERVER_CLASSES):
        target = servers.get(name)
        if target is None:
            unavailable.append(name)
            continue
        try:
            catalogs[name] = await _fetch_tools(
                target["url"],
                build_mcp_headers(
                    target.get("headers", {}),
                    target.get("auth_secret"),
                    target.get("auth_token_enc"),
                ),
                timeout_s=_PRESET_STAMP_TIMEOUT_S,
            )
        except Exception:
            log.warning("profile preset stamp: catalog fetch for %r failed — server reported unavailable", name)
            unavailable.append(name)
    return catalogs, unavailable


@router.post("/profiles", status_code=201)
async def create_profile(body: ProfileCreate, request: Request, _: AdminDep) -> dict[str, Any]:
    """Create/update a profile: named key-creation defaults.

    model_order = budget-cascade preference (first model with headroom
    serves). budgets + mcp_grants are PRESETS stamped onto every key created
    with this profile. routing_policy/routing_min_model opt the profile into
    model routing (core.model_routing) — the proxy picks the pair up via its
    TTL-cached profile_routing_config (~60s per user), so a save goes live
    within a minute. Upsert by name.
    """
    # Preset validation goes through the SAME splitter the enrollment path
    # and the seeding use (core.budgets.split_preset_budgets): exactly one
    # of limit_tokens / limit_usd per entry, period in day|week|month,
    # positive value. One validator, so a profile saved here can never be
    # a preset the redeem or the create-key seeding would refuse.
    try:
        split_preset_budgets([b.model_dump() for b in body.budgets])
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    # THE ORDERING IS ALSO AN ACL — REFUSE A STALE ID (2026-10-23 defect).
    # /v1/enroll derives an enrolled key's allowed_models as
    # [m for m in model_order if m in registered] (routes_enroll ~line 481),
    # which is the right least-privilege default but makes a never-registered
    # or RENAMED id a SILENT subtraction: the model vanishes from every key
    # minted from this profile, with no error anywhere. The live shape that
    # caused the bug report was an ordering naming "Qwen/Qwen3.8-27B-FP8"
    # while the registry served "Qwen3.8-27B" — an operator's model looked
    # "configured without limits for Qwen" and was in fact "configured without
    # Qwen". The ACL semantics are NOT changed here (an ACL is a security
    # boundary); the truncation is simply made impossible to reach silently.
    # Registered-but-unusual entries stay allowed: this checks registry
    # membership, not good taste.
    #
    # SCOPE — the guard applies only when the registry is NON-EMPTY. With no
    # models registered the derived ACL is [] whatever the ordering says
    # (`[m for m in order if m in registered]`), and routes_enroll passes
    # `allowed_models or None` into create_key — an empty ACL means
    # allow-ALL, so nothing can be hidden and there is no silent truncation to
    # prevent. Refusing there would instead block the legitimate
    # bootstrap/teardown ordering: a fresh install whose chart seeds profiles
    # before endpoints, and a site draining its registry. The defect this
    # guards needs a registry to hide models FROM.
    registered_models = model_router.models()
    stale = [m for m in body.model_order if m not in registered_models]
    if stale and registered_models:
        raise HTTPException(
            400,
            f"model_order entries not registered: {stale} - registered: {registered_models} "
            "(a stale id here silently removes the model from every enrolled key's allowed_models)",
        )
    # routing_min_model must be empty or name a model ON the ladder: a floor
    # outside model_order is unusable downstream (core.model_routing pins the
    # floor to the ceiling and warns per user = "no downgrade"), so refuse it
    # here instead of persisting a config that silently never downgrades.
    if body.routing_min_model and body.routing_min_model not in body.model_order:
        raise HTTPException(
            400,
            "routing_min_model must be one of the profile's models "
            f"{body.model_order} (or empty = no downgrade), got {body.routing_min_model!r}",
        )
    defaults = {
        # exclude_none keeps each stored preset to its ONE limit key, so a
        # reader can branch on presence instead of on a null test.
        "budgets": [b.model_dump(exclude_none=True) for b in body.budgets],
        "mcp_grants": [[g.server, g.tool] for g in body.mcp_grants],
    }
    # §3a live grants: when true, mcp_grants above evaluate AT CALL TIME for
    # every key of this profile (per-key rows stay exceptions) — an admin
    # edit of THIS profile re-scopes running keys on their next call.
    # Absent/false = stamps-at-create (today's semantics).
    if body.live_grants:
        defaults["live_grants"] = True
    preset_unavailable: list[str] = []
    preset_pair_count = 0
    if body.mcp_grant_preset is not None:
        # Preset expansion (P0-3): live catalogs -> (server, tool) pairs ->
        # the named preset's selection. Catalog gaps are REPORTED, never
        # silently swallowed: a server that is down or unregistered at
        # stamp time contributes no pairs, so the stamp is a
        # FAIL-SAFE under-provision (fewer grants, never more) — but the
        # response and the audit row say so, because a stamp that reads
        # complete while a class's whole server was unreachable would
        # mislead the operator about what keys of this profile can call.
        catalogs, preset_unavailable = await _preset_stamp_catalogs()
        catalog_pairs = preset_catalog_pairs(catalogs)
        resolved = preset_pairs(body.mcp_grant_preset, catalog_pairs)
        preset_pair_count = len(resolved)
        explicit = [(g[0], g[1]) for g in defaults["mcp_grants"]]
        merged = list(dict.fromkeys([*explicit, *resolved]))
        defaults["mcp_grants"] = [[s, t] for s, t in merged]
        defaults["mcp_grant_preset"] = body.mcp_grant_preset
    if body.limits is not None:
        defaults["limits"] = body.limits.model_dump(exclude_none=True)
    if body.memory is not None:
        defaults["memory"] = body.memory.model_dump()
    row = await db.fetchrow(
        "INSERT INTO profiles (name, model_order, defaults_json, offpeak_fraction, "
        "routing_policy, routing_min_model) "
        "VALUES ($1, $2, $3, $4, $5, $6) "
        "ON CONFLICT (name) DO UPDATE SET model_order = $2, defaults_json = $3, "
        "offpeak_fraction = $4, routing_policy = $5, routing_min_model = $6 "
        "RETURNING id, name, model_order, defaults_json, offpeak_fraction, "
        "routing_policy, routing_min_model",
        body.name,
        body.model_order,
        json.dumps(defaults),
        body.offpeak_fraction,
        body.routing_policy,
        body.routing_min_model,
    )
    assert row is not None
    out = dict(row)
    out["budgets"] = defaults["budgets"]
    out["mcp_grants"] = defaults["mcp_grants"]
    out["mcp_grant_preset"] = defaults.get("mcp_grant_preset")
    out["preset_grants"] = preset_pair_count
    out["preset_catalog_ok"] = not preset_unavailable
    out["preset_unavailable_servers"] = preset_unavailable
    # details: the preset payload IS the profile's durable shape (no secrets
    # — budgets/grants/limits/routing are all operator policy).
    await _audit(
        request,
        action="profile.upsert",
        resource_type="profile",
        resource_id=str(row["id"]),
        details={
            "name": body.name,
            "model_order": body.model_order,
            "budgets": defaults.get("budgets", []),
            "mcp_grants": defaults.get("mcp_grants", []),
            "limits": defaults.get("limits", {}),
            "offpeak_fraction": body.offpeak_fraction,
            "routing_policy": body.routing_policy,
            "routing_min_model": body.routing_min_model,
            "memory": defaults.get("memory"),
            "live_grants": bool(defaults.get("live_grants")),
            "mcp_grant_preset": defaults.get("mcp_grant_preset"),
            "preset_grants": preset_pair_count,
            "preset_catalog_ok": not preset_unavailable,
            "preset_unavailable_servers": preset_unavailable,
        },
    )
    return out


@router.get("/profiles")
async def list_profiles(_: AdminDep) -> list[dict[str, Any]]:
    """All profiles with cascade orderings + creation presets (+ routing
    rollout knobs, so the console round-trips everything POST accepts).

    ADDITIVE per-profile reporting of the ordering's DOUBLE DUTY (2026-10-23):
    model_order is a cascade preference AND (via the enroll redeem path) the
    source of every enrolled key's allowed_models. The console could not show
    that, so an ordering that had drifted from the registry read as "no
    models configured" while it was actively hiding models from keys.
    'stale_models' names the ordering entries with no pool, and 'acl_effect'
    states what an enrolled key will actually be allowed. Existing fields are
    untouched — this is reporting, not a shape change.
    """
    registered = model_router.models()
    registered_set = set(registered)
    out = []
    for r in await db.fetch(
        "SELECT id, name, model_order, defaults_json, offpeak_fraction, "
        "routing_policy, routing_min_model FROM profiles ORDER BY name"
    ):
        d = dict(r)
        try:
            defaults = json.loads(d.pop("defaults_json") or "{}")
        except (json.JSONDecodeError, TypeError):
            defaults = {}
        d["budgets"] = defaults.get("budgets", [])
        d["mcp_grants"] = defaults.get("mcp_grants", [])
        d["live_grants"] = bool(defaults.get("live_grants"))  # §3a live evaluation flag
        d["mcp_grant_preset"] = defaults.get("mcp_grant_preset")  # P0-3 preset round-trip
        d["limits"] = defaults.get("limits", {})
        d["memory"] = defaults.get("memory")  # the enrollment-memory preset, round-tripped
        order = list(d.get("model_order") or [])
        d["stale_models"] = [m for m in order if m not in registered_set]
        # The exact expression routes_enroll uses. Duplicated deliberately:
        # importing the redeem path here would invert the dependency (the
        # admin plane must not depend on the enrollment route module), and a
        # one-line list comprehension is cheaper to keep in sync than a
        # cross-module import cycle. If that expression changes, this display
        # becomes wrong — which is why the comment names its source.
        d["acl_effect"] = {"mode": "derived", "models": [m for m in order if m in registered_set]}
        out.append(d)
    return out


@router.post("/profiles/{profile_id}/propagate-limits")
async def propagate_profile_limits(profile_id: uuid.UUID, request: Request, _: AdminDep) -> dict[str, Any]:
    """Push the profile's limit presets onto ALL keys of attached users.

    EXPLICIT action (never a side effect of saving): changing a profile's
    presets silently rewriting live keys' rate limits would be a surprise —
    this endpoint is the operator saying "apply now". Only fields the
    profile actually presets are written; keys are read per request, so
    changes are live immediately. Returns the per-field update counts.
    """
    if not await _exists("profiles", profile_id):
        raise HTTPException(404, f"profile {profile_id} not found")
    prow = await db.fetchrow("SELECT defaults_json FROM profiles WHERE id = $1", profile_id)
    try:
        defaults = json.loads((prow["defaults_json"] if prow else None) or "{}")
    except (json.JSONDecodeError, TypeError):
        defaults = {}
    limits = defaults.get("limits", {}) or {}
    sets: list[str] = []
    args: list[Any] = []
    for jcol, lkey in (
        ("rate_limit_rpm", "rate_limit_rpm"),
        ("rate_limit_tpm", "rate_limit_tpm"),
        ("max_concurrent", "max_concurrent"),
    ):
        if limits.get(lkey) is not None:
            args.append(int(limits[lkey]))
            sets.append(f"{jcol} = ${len(args)}")
    if not sets:
        raise HTTPException(400, "profile has no limit presets to propagate")
    args.append(profile_id)
    tag = await db.execute(
        f"UPDATE api_keys SET {', '.join(sets)} "
        f"WHERE user_id IN (SELECT id FROM users WHERE profile_id = ${len(args)}) "
        "AND revoked_at IS NULL",
        *args,
    )
    await _audit(
        request,
        action="profile.limits.propagate",
        resource_type="profile",
        resource_id=str(profile_id),
        details={"fields": [s.split(" =")[0] for s in sets], "tag": tag},
    )
    return {"propagated": tag, "fields": [s.split(" =")[0] for s in sets]}


@router.delete("/profiles/{profile_id}")
async def delete_profile(profile_id: uuid.UUID, request: Request, _: AdminDep, force: bool = False) -> dict[str, Any]:
    """Delete a profile.

    Guard: users still attached to the profile would silently lose their
    cascade behavior (direct model resolution) — refused with a count unless
    ?force=true, which detaches them (users.profile_id -> NULL) and deletes.
    Keys created from the profile are UNAFFECTED either way: budgets and MCP
    grants were copied onto the key at creation, not referenced.
    """
    if not await _exists("profiles", profile_id):
        raise HTTPException(404, f"profile {profile_id} not found")
    attached = await db.fetchval("SELECT count(*) FROM users WHERE profile_id = $1", profile_id)
    n = int(attached or 0)
    if n and not force:
        raise HTTPException(
            409,
            f"profile has {n} attached user(s) — they would lose their cascade. "
            "Reassign them or retry with ?force=true to detach and delete.",
        )
    await db.execute("DELETE FROM profiles WHERE id = $1", profile_id)
    await _audit(
        request,
        action="profile.delete",
        resource_type="profile",
        resource_id=str(profile_id),
        details={"detached_users": n, "force": force},
    )
    return {"deleted": True, "detached_users": n}


@router.put("/users/{user_id}/profile", status_code=200)
async def assign_profile(user_id: uuid.UUID, body: UserAssignProfile, request: Request, _: AdminDep) -> dict[str, Any]:
    """Attach (or clear, with null) a profile on a user."""
    if not await _exists("users", user_id):
        raise HTTPException(404, f"user {user_id} not found")
    if body.profile_id is not None and not await _exists("profiles", body.profile_id):
        raise HTTPException(404, f"profile {body.profile_id} not found")
    row = await db.fetchrow(
        "UPDATE users SET profile_id = $2 WHERE id = $1 RETURNING id, tenant_id, name, profile_id",
        user_id,
        body.profile_id,
    )
    assert row is not None
    await _audit(
        request,
        action="user.profile.set",
        resource_type="user",
        resource_id=str(user_id),
        details={"profile_id": str(body.profile_id) if body.profile_id else None},
    )
    return dict(row)


@router.get("/usage/summary")
async def usage_summary(_: AdminDep, since: str | None = None) -> dict[str, Any]:
    """Ledger rollup: requests, tokens (in/out/cached split), cache hits,
    and USD totals — the numbers a capacity/chargeback view needs.

    The token sums are cast ::bigint: SUM(bigint) yields numeric, which
    asyncpg decodes as Decimal and the JSON layer then stringifies — the
    console received "4427047307" (a string) and its String.toLocaleString
    rendered it comma-less. bigint decodes as a plain int, so the wire
    carries real JSON numbers (exact well past any plausible token count).

    Bounded (audit P1-4): defaults to the last 30 days; the ledger is never
    pruned and an unbounded aggregate on the pool the data plane shares
    stalls tenant traffic on every console refresh. Pass since= to widen
    (max 366 days).
    """
    _since_dt, _until_dt = _bounded_window(since, default_days=30)
    _cond = " WHERE ts >= $1 AND ts <= $2" if _since_dt else ""
    _cond_l = " WHERE l.ts >= $1 AND l.ts <= $2" if _since_dt else ""
    _win = [_since_dt, _until_dt] if _since_dt else []
    row = await db.fetchrow(
        f"""
        SELECT
          count(*)                                                    AS requests,
          COALESCE(SUM(total_tokens), 0)::bigint                      AS total_tokens,
          COALESCE(SUM(prompt_tokens), 0)::bigint                     AS input_tokens,
          COALESCE(SUM(completion_tokens), 0)::bigint                 AS output_tokens,
          COALESCE(SUM(cached_tokens), 0)::bigint                     AS cached_tokens,
          count(*) FILTER (WHERE status = 'cache_hit')                AS cache_hit_requests,
          -- TOKEN-WEIGHTED cache hit rate (audit follow-up: the raw request-level
          -- count read 0 while 4.7B of 4.75B input tokens were upstream
          -- prompt-cached — vLLM/SGLang report those as cached_tokens>0 on 'ok'
          -- rows, not as status='cache_hit' rows, which are the EMBEDDING-cache
          -- hits only). cached/input is the number an operator means.
          CASE WHEN COALESCE(SUM(prompt_tokens), 0) > 0
               THEN ROUND(100.0 * SUM(cached_tokens) / SUM(prompt_tokens), 1)
               ELSE NULL END                                          AS cache_hit_rate_pct,
          COALESCE(SUM(cost_usd), 0)::float                           AS cost_usd,
          COALESCE(SUM(total_tokens * billing_fraction), 0)::float    AS billed_tokens,
          COALESCE(SUM(cost_usd * billing_fraction), 0)::float        AS billed_cost_usd
        FROM usage_ledger{_cond}
        """,
        *_win,
    )
    by_model = await db.fetch(
        f"""
        SELECT model,
               count(*)                                            AS requests,
               COALESCE(SUM(prompt_tokens), 0)::bigint             AS input_tokens,
               COALESCE(SUM(completion_tokens), 0)::bigint         AS output_tokens,
               COALESCE(SUM(cached_tokens), 0)::bigint             AS cached_tokens,
               count(*) FILTER (WHERE status = 'cache_hit')        AS cache_hits,
               CASE WHEN COALESCE(SUM(prompt_tokens), 0) > 0
                    THEN ROUND(100.0 * SUM(cached_tokens) / SUM(prompt_tokens), 1)
                    ELSE NULL END                                  AS cache_hit_rate_pct,
               COALESCE(SUM(cost_usd), 0)::float                   AS cost_usd,
               COALESCE(SUM(total_tokens * billing_fraction), 0)::float AS billed_tokens,
               COALESCE(SUM(cost_usd * billing_fraction), 0)::float     AS billed_cost_usd
        FROM usage_ledger{_cond} GROUP BY model ORDER BY cost_usd DESC
        """,
        *_win,
    )
    # CHARGEBACK ROLLUP (2026-10-20): grouped by (key NAME, user), NOT by
    # key_id. `POST /keys/{id}/rotate` mints a NEW key row with the SAME name
    # and user and revokes the old one, so grouping by id split one logical
    # credential's spend across every rotation generation — the console listed
    # andrew-admin four times with four unrelated cost figures. A chargeback
    # view answers "who consumed what", and that answer must survive rotation.
    #
    # SAFETY of the merge: api_keys.name is NOT unique, but (name, user) is the
    # unit an operator thinks in and u.name is what the console's user filter
    # keys on — so u.name STAYS in the GROUP BY and two different people can
    # never collapse into one row. usage_ledger.key_id has no FK, but keys are
    # only ever REVOKED (never deleted), so every ledger row resolves to a name.
    # `generations` counts the distinct key rows behind a row, so a rotated
    # credential stays visible as rotated instead of masquerading as one key.
    by_key = await db.fetch(
        f"""
        SELECT k.name AS key_name, u.name AS user_name,
               count(DISTINCT l.key_id)                            AS generations,
               count(*)                                            AS requests,
               COALESCE(SUM(l.prompt_tokens), 0)::bigint           AS input_tokens,
               COALESCE(SUM(l.completion_tokens), 0)::bigint       AS output_tokens,
               COALESCE(SUM(l.cached_tokens), 0)::bigint           AS cached_tokens,
               count(*) FILTER (WHERE l.status = 'cache_hit')      AS cache_hits,
               CASE WHEN COALESCE(SUM(l.prompt_tokens), 0) > 0
                    THEN ROUND(100.0 * SUM(l.cached_tokens) / SUM(l.prompt_tokens), 1)
                    ELSE NULL END                                  AS cache_hit_rate_pct,
               COALESCE(SUM(l.cost_usd), 0)::float                 AS cost_usd,
               COALESCE(SUM(l.total_tokens * l.billing_fraction), 0)::float AS billed_tokens,
               COALESCE(SUM(l.cost_usd * l.billing_fraction), 0)::float     AS billed_cost_usd
        FROM usage_ledger l
        LEFT JOIN api_keys k ON k.id = l.key_id
        LEFT JOIN users u ON u.id = k.user_id
        {_cond_l}
        GROUP BY k.name, u.name ORDER BY cost_usd DESC
        """,
        *_win,
    )
    # PER-KEY / PER-MODEL breakdown, for the console's expandable rows.
    # Same (key NAME, user) grouping as by_key so a rotated credential stays
    # one logical row; adding model splits it further without changing the
    # identity. Bounded by the same window as everything else here.
    by_key_model = await db.fetch(
        f"""
        SELECT k.name AS key_name, u.name AS user_name, l.model,
               count(*)                                            AS requests,
               COALESCE(SUM(l.prompt_tokens), 0)::bigint           AS input_tokens,
               COALESCE(SUM(l.completion_tokens), 0)::bigint       AS output_tokens,
               COALESCE(SUM(l.cached_tokens), 0)::bigint           AS cached_tokens,
               CASE WHEN COALESCE(SUM(l.prompt_tokens), 0) > 0
                    THEN ROUND(100.0 * SUM(l.cached_tokens) / SUM(l.prompt_tokens), 1)
                    ELSE NULL END                                  AS cache_hit_rate_pct,
               COALESCE(SUM(l.cost_usd), 0)::float                 AS cost_usd,
               COALESCE(SUM(l.cost_usd * l.billing_fraction), 0)::float AS billed_cost_usd
        FROM usage_ledger l
        LEFT JOIN api_keys k ON k.id = l.key_id
        LEFT JOIN users u ON u.id = k.user_id
        {_cond_l}
        GROUP BY k.name, u.name, l.model
        ORDER BY cost_usd DESC
        """,
        *_win,
    )
    # ACTUAL RETAINED HISTORY (2026-10-26). The usage ledger is NEVER pruned
    # (housekeeping's contract: it is both the audit trail and the budget
    # engine, so deleting rows would silently raise every key's headroom).
    # _MAX_WINDOW_DAYS is a QUERY cost guard, not a retention policy -- so
    # labelling the widest option 'Max (366d)' claimed a data horizon the
    # deployment does not have. Report the real oldest row instead and let the
    # console say 'since <date>' honestly. One cheap indexed MIN() on
    # idx_ledger_ts; bounded by the same window as everything else.
    oldest = await db.fetchval('SELECT MIN(ts) FROM usage_ledger')
    return {
        "totals": dict(row) if row else {},
        "by_model": [dict(r) for r in by_model],
        "by_key": [dict(r) for r in by_key],
        "by_key_model": [dict(r) for r in by_key_model],
        "window": {
            "since": _since_dt.isoformat() if _since_dt else None,
            "until": _until_dt.isoformat() if _until_dt else None,
            "default_days": 30,
            # The hard CAP this endpoint enforces (a query-cost guard, NOT a
            # retention policy). Exposed so the console labels its widest
            # option from the server's limit instead of a drifting copy.
            "max_days": _MAX_WINDOW_DAYS,
            # The true start of retained data (None = empty ledger).
            # Guard the TYPE, not just truthiness: a driver/fake may hand back
            # a non-datetime (int epoch, str) and .isoformat() would 500 the
            # whole summary. A label is not worth failing the endpoint over.
            "oldest_ts": oldest.isoformat() if hasattr(oldest, "isoformat") else None,
        },
    }


# ---------------------------------------------------------------------------
# agent traces (0.3.x observability wave) — request-grouped relay timelines
# ---------------------------------------------------------------------------


def _parse_since(since: str | None) -> datetime | None:
    """ISO-8601 `since` query param -> datetime; 400 on garbage.

    The trace explorer is a human console feed — a clear 400 beats FastAPI's
    generic 422 (and a silent ignore would show stale data with no hint why).
    """
    if since is None:
        return None
    try:
        dt = datetime.fromisoformat(since)
    except ValueError:
        raise HTTPException(400, "since must be an ISO-8601 datetime (e.g. 2025-01-01T00:00:00Z)") from None
    # audit P2-6: a naive datetime would be interpreted as container-local
    # time by asyncpg (astimezone(utc)) - silently shifted on any non-UTC image.
    if dt.tzinfo is None:
        raise HTTPException(400, "since must include a UTC offset (e.g. 2025-01-01T00:00:00Z)")
    return dt


TRACE_LIMIT_CAP = 200

_MAX_WINDOW_DAYS = 366


def _bounded_window(since: str | None, default_days: int | None) -> tuple[datetime | None, datetime | None]:
    """Shared console-window bound (audit P1-4): the ledger and trace
    tables are never pruned; an unbounded aggregate on the pool the data
    plane shares stalls tenant traffic on every console refresh. Returns
    (since_dt, until_dt=now) - or (None, None) when no bound applies.
    A span beyond _MAX_WINDOW_DAYS is a 400 (a typo, not an intent)."""
    dt = _parse_since(since)
    if dt is None:
        if default_days is None:
            return None, None
        dt = datetime.now(UTC) - timedelta(days=default_days)
    now = datetime.now(UTC)
    if (now - dt).days > _MAX_WINDOW_DAYS:
        raise HTTPException(400, f"window too large: more than {_MAX_WINDOW_DAYS} days")
    return dt, now


@router.get("/traces")
async def list_traces(
    _: AdminDep,
    user_id: uuid.UUID | None = None,
    key_id: uuid.UUID | None = None,
    since: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Request-grouped agent traces: one entry per relay request with its
    steps (model iterations + tool calls in ts order), wall-clock totals,
    and the actor join (key_name / user_name).

    Filters (AND-ed): user_id, key_id, since (ISO-8601); `limit` bounds the
    number of REQUESTS returned, capped at 200 (default 50).
    """
    from ..core import traces

    return await traces.recent(
        user_id=user_id,
        key_id=key_id,
        since=(_bounded_window(since, default_days=30))[0],  # audit P1-4: default 30d window, max 366
        limit=max(1, min(limit, TRACE_LIMIT_CAP)),
    )


@router.get("/traces/{request_id}")
async def get_trace(request_id: str, _: AdminDep) -> dict[str, Any]:
    """One request's full trace timeline (same shape as /admin/traces
    entries); 404 when nothing was traced for the request id."""
    from ..core import traces

    trace = await traces.get(request_id)
    if trace is None:
        raise HTTPException(404, f"no trace for request {request_id!r}")
    return trace


# ---------------------------------------------------------------------------
# off-peak billing evaluator (read-only snapshot)
# ---------------------------------------------------------------------------


@router.get("/offpeak")
async def offpeak_status(_: AdminDep) -> dict[str, Any]:
    """Off-peak billing evaluator snapshot — a READ-ONLY observability view.

    Operator-facing state of the demand-shaped billing evaluator
    (core.offpeak): whether the feature is on, whether the hysteresis is
    currently in the low-demand (discounted) state, the effective fraction
    every settle stamps, the evaluator's configuration, and the last
    observation/error. No DB access — a broken ledger cannot hide the
    billing state.
    """
    from ..core import offpeak

    return {
        "models": offpeak.models_snapshot(),
        "enabled": offpeak.state.enabled,
        "active": offpeak.state.active,
        "fraction": offpeak.fraction(),
        "enter_pct": settings.offpeak_enter_pct,
        "exit_pct": settings.offpeak_exit_pct,
        "check_s": settings.offpeak_check_s,
        "promql": settings.offpeak_promql,
        "mcp_server": settings.offpeak_mcp_server,
        "tool": settings.offpeak_tool,
        "last_value_pct": offpeak.state.last_value_pct,
        "last_check": offpeak.state.last_check,
        "last_error": offpeak.state.last_error,
        "state_since": offpeak.state.state_since,
        # utilization panel: the global evaluator's recent samples + the
        # fallback base the per-model ratio editor shows as the default
        "history": offpeak.state.history_json(),
        "default_fraction": settings.offpeak_fraction,
    }


class OffpeakFraction(BaseModel):
    """Body of PUT /offpeak/models/{model}/fraction — None clears the
    override (the model falls back to its profile's fraction, else the
    gateway default). Bounds match the profiles.offpeak_fraction knob:
    a discount in (0, 1]."""

    fraction: float | None = Field(None, gt=0.0, le=1.0)


@router.put("/offpeak/models/{model_name:path}/fraction")
async def set_offpeak_model_fraction(
    model_name: str, body: OffpeakFraction, request: Request, _: AdminDep
) -> dict[str, Any]:
    """Set or clear ONE model's off-peak pricing ratio (the Off-peak panel's
    ratio editor).

    Precedence while the model's governing evaluator (its own when attached,
    else the global one) is in the low-demand state: this override, then the
    key's profile fraction, then the gateway default. Clearing (null) removes
    the row entirely — no tombstone.

    `{model_name:path}` — model names contain slashes ("Qwen/Qwen3.8-27B-
    FP8"); a plain path param matches one segment only (the DELETE /models
    lesson, verbatim).

    The ledger keeps TRUE usage (fraction lives on the row per settle), so
    edits are pure forward-looking commercial terms — no history rewrite.
    """
    from ..core import offpeak

    await offpeak.set_override(model_name, body.fraction)
    # details: the new commercial term (None = the row was CLEARED — the
    # model falls back to profile/gateway defaults). The ledger keeps true
    # usage, so this is forward-looking policy, recorded as such.
    await _audit(
        request,
        action="offpeak.set",
        resource_type="model",
        resource_id=model_name,
        details={"fraction": body.fraction, "cleared": body.fraction is None},
    )
    return {"model": model_name, "fraction": body.fraction}


# ---------------------------------------------------------------------------
# session distiller (admin runtime config)
#
# The DB-backed override layer over the env defaults (core.distiller_config).
# The env flag still owns the ROUTE MOUNT (unmounted = 404); these knobs own
# whether and how distills RUN: summarizer model (the "Qwen" summarizer in
# the fleet bundle), off-peak-only scheduling, the quiet-window debounce,
# and the output cap. Edits are audited like every admin mutation.


class DistillerConfigPatch(BaseModel):
    # All fields optional: only provided keys are written; None CLEARS the
    # row (back to the env default). Unknown keys are refused (400), not
    # silently dropped.
    model: str | None = None
    offpeak_only: bool | None = None
    enabled: bool | None = None
    debounce_minutes: int | None = Field(None, ge=1, le=1440)
    max_tokens: int | None = Field(None, ge=0, le=32768)


@router.get("/distiller/config")
async def get_distiller_config(_: AdminDep) -> dict[str, Any]:
    """The effective distiller runtime config (env defaults <- DB overrides),
    plus which rows are overridden (the UI renders overrides distinctly)."""
    from ..core import distiller_config

    cfg = await distiller_config._config()
    overridden: set[str] = set()
    try:
        rows = await db.fetch("SELECT key FROM distiller_config")
        overridden = {r["key"] for r in rows}
    except Exception:
        pass
    return {"config": cfg, "overridden": sorted(overridden)}


@router.put("/distiller/config")
async def put_distiller_config(body: DistillerConfigPatch, request: Request, _: AdminDep) -> dict[str, Any]:
    """Patch the distiller runtime config. Provided keys are written; a key
    explicitly set to null is CLEARED back to its env default."""
    from ..core import distiller_config

    provided = body.model_dump(exclude_unset=True)
    unknown = [k for k in provided if k not in distiller_config.DEFAULTS]
    if unknown:
        raise HTTPException(400, f"unknown distiller config keys: {unknown}")
    merged = await distiller_config.set_values(provided)
    await _audit(
        request,
        action="distiller.config",
        resource_type="system",
        resource_id="distiller",
        details={"keys": sorted(provided), "values": {k: provided[k] for k in provided}},
    )
    return {"config": merged}


# ---------------------------------------------------------------------------
# content guardrails (admin-plane curation)
#
# These endpoints only REGISTER and WIRE guardrails (the guardrails /
# model_guardrails tables + the model_endpoints picker). The runtime engine
# that EVALUATES them on model calls lives in core.guardrails and is
# deliberately NOT imported here — the contract between the two halves is
# the table shape, so the admin plane cannot grow an accidental dependency
# on (or regress) the hot path.
# ---------------------------------------------------------------------------

GUARDRAIL_MODES = {"pre_call", "post_call"}
GUARDRAIL_ENGINES = {"blocklist", "regex_pii", "length", "webhook"}
REGEX_PII_ENTITIES = {"EMAIL", "PHONE", "CREDIT_CARD"}
GUARDRAIL_ACTIONS = {"block", "mask"}


class GuardrailCreate(BaseModel):
    name: str
    mode: str
    engine: str
    # Any (not dict) on purpose: a wrong-typed config must reach OUR validator
    # and come back as a 400 naming the rule, not a generic 422 dump.
    config: Any = Field(default_factory=dict)
    enabled: bool = True


class GuardrailPatch(BaseModel):
    """Partial guardrail update — only provided fields change.

    mode/engine/config are validated as a COMBINED triple against the row's
    current values (see patch_guardrail); `name` is deliberately absent —
    attachments reference the row, renaming would be an identity change.
    """

    mode: str | None = None
    engine: str | None = None
    config: Any = None  # must be a dict when provided (validated below)
    enabled: bool | None = None


class GuardrailModelsReplace(BaseModel):
    model_names: list[str] = Field(default_factory=list)


def _validate_guardrail_name(name: str) -> None:
    """400 with the exact broken rule — guardrail names are typed into a
    console form, and a specific message beats a generic validation dump."""
    if not name.strip():
        raise HTTPException(400, "name must not be empty")
    if len(name) > 200:
        raise HTTPException(400, "name must be at most 200 characters")


async def _validate_guardrail(mode: str, engine: str, config: Any) -> None:
    """Validate mode / engine / config as ONE triple — the engine gives the
    config its meaning, so they are only coherent together (a length config
    on a blocklist row would strand a guardrail the engine misreads).

    Unknown config KEYS are tolerated (the engine owns its schema and may
    grow); the keys below are the ones the admin plane PROMISES to check.
    The serialized config is size-capped (audit finding 20) — the blob
    round-trips through the registry, every GET, the audit trail, and CSV.
    """
    _check_json_size(config, "guardrail config")
    if mode not in GUARDRAIL_MODES:
        raise HTTPException(400, f"mode must be one of {sorted(GUARDRAIL_MODES)}")
    if engine not in GUARDRAIL_ENGINES:
        raise HTTPException(400, f"engine must be one of {sorted(GUARDRAIL_ENGINES)}")
    if not isinstance(config, dict):
        raise HTTPException(400, "config must be a JSON object")
    # reply_text (all engines): a non-empty string turns a pre_call block
    # into a SOFT block — the proxy returns the notice as a normal chat
    # completion instead of the 400. Must be a string when present, or the
    # proxy would strand the guardrail (misreads non-str as hard-block).
    reply_text = config.get("reply_text")
    if reply_text is not None and (not isinstance(reply_text, str) or not reply_text.strip()):
        raise HTTPException(400, "guardrail 'reply_text' must be a non-empty string (absent/empty = hard 400 block)")
    if engine == "blocklist":
        keywords = config.get("keywords")
        if not isinstance(keywords, list) or any(not isinstance(k, str) for k in keywords):
            raise HTTPException(400, "blocklist config requires 'keywords': a list of strings (may be empty)")
        if config.get("action") is not None and config["action"] not in GUARDRAIL_ACTIONS:
            raise HTTPException(400, f"blocklist 'action' must be one of {sorted(GUARDRAIL_ACTIONS)}")
    elif engine == "regex_pii":
        entities = config.get("entities")
        if entities is not None:
            if not isinstance(entities, list) or any(not isinstance(e, str) for e in entities):
                raise HTTPException(400, "regex_pii 'entities' must be a list of strings")
            unknown = sorted(set(entities) - REGEX_PII_ENTITIES)
            if unknown:
                raise HTTPException(
                    400,
                    f"unknown PII entity name(s) {unknown}; allowed: {sorted(REGEX_PII_ENTITIES)}",
                )
    elif engine == "length":
        max_chars = config.get("max_chars")
        # bool is an int subclass — `true` must not pass as max_chars=1
        if isinstance(max_chars, bool) or not isinstance(max_chars, int) or max_chars <= 0:
            raise HTTPException(400, "length config requires 'max_chars': a positive integer")
    elif engine == "webhook":
        url = config.get("url")
        if not isinstance(url, str) or urlparse(url).scheme not in ("http", "https"):
            raise HTTPException(400, "webhook config requires 'url' starting with http:// or https://")
        # SSRF parity (integration decision): the gateway POSTs to this URL
        # server-side, exactly like an admin-registered model endpoint or
        # MCP server — so it passes the SAME _check_url gate (loopback and
        # metadata ranges hard-denied; private RFC1918 in-cluster allowed).
        await _check_url_async([url], "webhook url")
        api_key_env = config.get("api_key_env")
        if api_key_env is not None and not isinstance(api_key_env, str):
            raise HTTPException(400, "webhook 'api_key_env' must be a string")
        timeout_s = config.get("timeout_s")
        if timeout_s is not None and (
            isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)) or timeout_s <= 0
        ):
            raise HTTPException(400, "webhook 'timeout_s' must be a positive number")


def _guardrail_out(row: Any, models: list[str]) -> dict[str, Any]:
    """DB row -> API shape: config_json TEXT parsed into `config`, the
    attachment model names attached.

    A corrupted config_json degrades to {} instead of 500ing the whole
    console list — the row stays visible and editable, which is the path to
    FIXING it.
    """
    out = dict(row)
    try:
        config = json.loads(out.pop("config_json") or "{}")
    except (json.JSONDecodeError, TypeError):
        config = {}
    out["config"] = config if isinstance(config, dict) else {}
    out["models"] = models
    return out


@router.get("/guardrails")
async def list_guardrails(_: AdminDep) -> list[dict[str, Any]]:
    """All guardrails, name-ordered, each with its parsed config and the
    sorted model names it is attached to.

    Two plain queries aggregated in Python (guardrails, then attachments):
    console-refresh cadence makes the extra round-trip a non-issue, and both
    statements stay trivially testable — no JSON aggregation gymnastics.
    """
    rows = await db.fetch(
        "SELECT id, name, mode, engine, config_json, enabled, created_at FROM guardrails ORDER BY name"
    )
    attached: dict[Any, list[str]] = {}
    for a in await db.fetch("SELECT guardrail_id, model_name FROM model_guardrails"):
        attached.setdefault(a["guardrail_id"], []).append(a["model_name"])
    return [_guardrail_out(r, sorted(attached.get(r["id"], []))) for r in rows]


@router.post("/guardrails", status_code=201)
async def create_guardrail(body: GuardrailCreate, request: Request, _: AdminDep) -> dict[str, Any]:
    """Register a guardrail (mode + engine + engine-specific config).

    Validation runs BEFORE the write; the duplicate-name race is answered by
    the UNIQUE constraint (409), not a pre-SELECT — under concurrent creates
    the index is the only authoritative answer.
    """
    _validate_guardrail_name(body.name)
    await _validate_guardrail(body.mode, body.engine, body.config)
    try:
        row = await db.fetchrow(
            "INSERT INTO guardrails (name, mode, engine, config_json, enabled) "
            "VALUES ($1, $2, $3, $4, $5) "
            "RETURNING id, name, mode, engine, config_json, enabled, created_at",
            body.name,
            body.mode,
            body.engine,
            json.dumps(body.config),
            body.enabled,
        )
    except asyncpg.UniqueViolationError:
        raise HTTPException(409, f"guardrail '{body.name}' already exists") from None
    assert row is not None
    await _audit(
        request,
        action="guardrail.create",
        resource_type="guardrail",
        resource_id=str(row["id"]),
        details={
            "name": body.name,
            "mode": body.mode,
            "engine": body.engine,
            # config KEY NAMES only (audit finding 10 — same rule as the
            # PATCH path: free-text config values never enter the trail)
            "config_keys": sorted(body.config) if isinstance(body.config, dict) else [],
            "enabled": body.enabled,
        },
    )
    _guardrail_spec_cache_clear()  # keep the data-plane spec cache coherent with the write
    return _guardrail_out(row, [])  # a fresh guardrail has no attachments yet


def _guardrail_spec_cache_clear() -> None:
    """Invalidate the core guardrails module's per-model spec TTL cache.

    REQUIRED on every admin-plane guardrail mutation (audit perf wave:
    the 60s TTL cache in guardrails.py serves the data plane; without this
    a PATCHed/attached guardrail is unenforced and a DELETED one keeps
    running for up to 60s — fail-open delivery, so an SLA gap, not a
    security hole, but it ships live without this wiring). Deferred import
    mirrors the catalog_cache_clear pattern — no import cycle.
    """
    from ..core.guardrails import guardrails_spec_cache_clear

    guardrails_spec_cache_clear()


@router.get("/guardrails/models")
async def list_guardrail_model_names(_: AdminDep) -> dict[str, Any]:
    """Registered virtual model names (DISTINCT model_name in
    model_endpoints) for the attachment picker.

    Declared BEFORE the /guardrails/{guardrail_id} routes on purpose: with a
    UUID path route first, FastAPI would match 'models' as the path param
    and fail UUID parsing with a 422 instead of serving this endpoint.
    """
    rows = await db.fetch("SELECT DISTINCT model_name FROM model_endpoints ORDER BY model_name")
    return {"models": [r["model_name"] for r in rows]}


@router.put("/guardrails/{guardrail_id}/models")
async def replace_guardrail_models(
    guardrail_id: uuid.UUID, body: GuardrailModelsReplace, request: Request, _: AdminDep
) -> dict[str, Any]:
    """Replace a guardrail's whole attachment set atomically (DELETE all +
    INSERT the list in ONE transaction).

    WHY replace-all: the checkbox list IS the desired state (same rationale
    as budgets / mcp-grants) — a diff would leave stale attachments routing
    live traffic through a policy the operator believes is detached, and
    readers must see the old set or the new one, never a half-applied mix.

    Every name is checked against model_endpoints BEFORE anything is
    written — an attachment to an unregistered model is dead config that can
    never route, and that mistake must fail at save time, not silently.
    """
    if not await _exists("guardrails", guardrail_id):
        raise HTTPException(404, f"guardrail {guardrail_id} not found")
    _check_list_len(body.model_names, "model_names")
    names = sorted(set(body.model_names))  # set semantics: a double-ticked model is one attachment
    known = {r["model_name"] for r in await db.fetch("SELECT DISTINCT model_name FROM model_endpoints")}
    unknown = [n for n in names if n not in known]
    if unknown:
        raise HTTPException(
            400,
            f"unknown model name(s): {unknown} — register them under POST /admin/models first",
        )
    async with db.acquire() as conn, conn.transaction():
        await conn.execute("DELETE FROM model_guardrails WHERE guardrail_id = $1", guardrail_id)
        if names:
            await conn.executemany(
                "INSERT INTO model_guardrails (model_name, guardrail_id) VALUES ($1, $2)",
                [(n, guardrail_id) for n in names],
            )
    await _audit(
        request,
        action="guardrail.models.set",
        resource_type="guardrail",
        resource_id=str(guardrail_id),
        details={"model_names": names},
    )
    _guardrail_spec_cache_clear()  # attachment change must not wait out the 60s TTL
    return {"models": names}


@router.patch("/guardrails/{guardrail_id}")
async def patch_guardrail(
    guardrail_id: uuid.UUID, body: GuardrailPatch, request: Request, _: AdminDep
) -> dict[str, Any]:
    """Edit mode / engine / config / enabled in place (row is read first).

    The triple is validated as a unit against the row's CURRENT values: a
    config-only PATCH must fit the engine already on the row, and an
    engine-only PATCH must fit the config already there — validating the
    payload in isolation would strand a guardrail the runtime misreads at
    request time. `enabled` merges with `is not None` so an explicit false
    survives the falsy trap (the mcp-servers PATCH lesson)."""
    row = await db.fetchrow(
        "SELECT id, name, mode, engine, config_json, enabled FROM guardrails WHERE id = $1",
        guardrail_id,
    )
    if row is None:
        raise HTTPException(404, f"guardrail {guardrail_id} not found")
    if body.mode is None and body.engine is None and body.config is None and body.enabled is None:
        raise HTTPException(400, "no editable fields provided")
    mode = body.mode if body.mode is not None else row["mode"]
    engine = body.engine if body.engine is not None else row["engine"]
    if body.config is not None:
        config: Any = body.config
    else:
        try:
            config = json.loads(row["config_json"] or "{}")
        except (json.JSONDecodeError, TypeError):
            config = {}
        if not isinstance(config, dict):
            config = {}
    await _validate_guardrail(mode, engine, config)
    sets: list[str] = []
    args: list[Any] = []
    for field, col in (("mode", "mode"), ("engine", "engine"), ("config", "config_json"), ("enabled", "enabled")):
        val = getattr(body, field)
        if val is not None:
            args.append(json.dumps(val) if field == "config" else val)
            sets.append(f"{col} = ${len(args)}")
    args.append(guardrail_id)
    # column names above are a fixed tuple, never user input
    updated = await db.fetchrow(
        f"UPDATE guardrails SET {', '.join(sets)} WHERE id = ${len(args)} "
        "RETURNING id, name, mode, engine, config_json, enabled, created_at",
        *args,
    )
    assert updated is not None
    # details: only the fields the PATCH actually set (mode/engine/enabled as
    # scalars). The config blob is audited as KEY NAMES ONLY (audit finding
    # 10): free-text config values (blocklist keyword lists, webhook URLs and
    # paths) are exactly the content a compliance trail must not copy into a
    # permanent, CSV-exportable table — the trail records THAT the config
    # changed and which knobs it touched, never the values.
    cfg = getattr(body, "config", None)
    details: dict[str, Any] = {}
    for field in sorted(body.model_fields_set):
        if field == "config" and isinstance(cfg, dict):
            details["config_keys"] = sorted(cfg)
        else:
            details[field] = getattr(body, field)
    await _audit(
        request,
        action="guardrail.patch",
        resource_type="guardrail",
        resource_id=str(guardrail_id),
        details=details,
    )
    attached = await db.fetch(
        "SELECT model_name FROM model_guardrails WHERE guardrail_id = $1 ORDER BY model_name",
        guardrail_id,
    )
    _guardrail_spec_cache_clear()  # patch (mode/engine/config/enabled) must not wait out the 60s TTL
    return _guardrail_out(updated, [a["model_name"] for a in attached])


@router.delete("/guardrails/{guardrail_id}", status_code=204)
async def delete_guardrail(guardrail_id: uuid.UUID, request: Request, _: AdminDep) -> None:
    """Delete a guardrail. Attachment rows cascade via the FK
    (ON DELETE CASCADE) — a deleted policy must not keep routing traffic
    through a dangling join row. 204: nothing meaningful to return."""
    if not await _exists("guardrails", guardrail_id):
        raise HTTPException(404, f"guardrail {guardrail_id} not found")
    await db.execute("DELETE FROM guardrails WHERE id = $1", guardrail_id)
    await _audit(request, action="guardrail.delete", resource_type="guardrail", resource_id=str(guardrail_id))
    _guardrail_spec_cache_clear()  # deleted guardrail must not keep running out the 60s TTL


# ---------------------------------------------------------------------------
# model routing (admin runtime config + the decision-model prompt)
#
# The DB-backed operator layer over core.model_routing (core.routing_config):
# which models the router may route TO, the prompt a DECISION model
# (vllm-sr / d3-turbo) would receive, and whether live GPU demand is injected
# into it. The decision model itself is NOT called in this release — that is
# the point: the allowed set, the prompt and the load signal can be reviewed
# against live shadow-ledger traffic BEFORE a model is allowed to spend.
#
# The prompt is a TEMPLATE, not a format string: core.routing_config's
# render_prompt substitutes five named {{placeholders}} and leaves every other
# brace pair VERBATIM (a typo must be visible in the preview). The preview
# route is a pure read — no DB write, no audit row — so an operator can
# iterate on wording without littering the trail.


class RoutingConfigPatch(BaseModel):
    """Body of PUT /routing/config.

    Every field optional: only provided keys are written; a key explicitly set
    to null CLEARS its row (back to the default). exclude_unset in the handler
    is what distinguishes "absent" from "explicitly null" — without it every
    omitted field would read as a clear and a one-checkbox save would wipe the
    prompt.

    extra="allow" is load-bearing: pydantic's default ("ignore") DROPS an
    unknown key, so the handler's unknown-key 400 could never fire and a
    typo'd field name would answer 200 while changing nothing — the exact
    silent-no-op the check exists to prevent. Carrying the key through to the
    handler turns it into the contract's 400.
    """

    model_config = ConfigDict(extra="allow")

    enabled: bool | None = None
    decision_model: str | None = None
    allowed_models: list[str] | None = None
    prompt: str | None = Field(None, max_length=20000)
    inject_gpu_load: bool | None = None
    gpu_load_source: str | None = None
    max_prompt_chars: int | None = Field(None, ge=200, le=60000)
    # --- decision-model execution (2026-10-23) -------------------------
    # decision_mode gates whether a CONFIGURED decision model is consulted:
    # "off" (the default) keeps the shipped heuristics byte-identical even
    # when decision_model is set, so a model can be pointed at without
    # changing behaviour; "shadow" asks it and records what it WOULD have
    # served while the heuristics still serve; "active" serves its choice
    # when it clears decision_min_confidence.
    decision_mode: str | None = None
    decision_min_confidence: float | None = Field(None, ge=0.0, le=1.0)
    decision_timeout_ms: int | None = Field(None, ge=250, le=30000)
    # --- request scope + conversation stickiness (2026-10-23) ----------
    # route_default_model is a DIFFERENT axis from decision_model: that
    # dropdown picks WHO decides, this picks WHICH requests get decided
    # (it promotes the legacy 'auto' alias into routing). Off by default so
    # arming routing never silently changes what an existing client's
    # request does; the explicit routing / routing__X names always route.
    route_default_model: bool | None = None
    # Never-downgrade stickiness: a routed conversation holds a capability
    # floor and may escalate above it but never fall below it. Off by default.
    sticky_routing: bool | None = None
    # Idle reset for that floor (s). 0 = never reset. Bounded so a typo cannot
    # park a floor for years nor thrash it on every turn.
    sticky_routing_idle_reset_s: int | None = Field(None, ge=0, le=86400)


class RoutingPromptPreview(BaseModel):
    """Body of POST /routing/prompt-preview — in-memory overrides over the
    SAVED config. Null/absent = use the saved value."""

    prompt: str | None = None
    decision_model: str | None = None
    inject_gpu_load: bool | None = None


def _routing_decision_models() -> list[str]:
    """Registered models that can ACT as the router: decision or chat pools.

    A decision model (vLLM-SR System One, model_type "decision") is the
    intended choice; "chat" stays accepted for the first-cut deployments that
    pointed the knob at a chat pool before the type existed. Embedding,
    reranker and voice pools are excluded — none of them can answer a choice
    question.
    """
    out: list[str] = []
    for m in model_router.models():
        pool = model_router.pool(m)
        if pool is not None and pool.endpoints and pool.endpoints[0].model_type in ("decision", "chat"):
            out.append(m)
    return out


def _routing_chat_models() -> list[str]:
    """Registered models whose pool serves CHAT.

    The router's ladder is a chat concept: an embedding or reranker pool can
    never be a routing candidate, so the panel's checkboxes and the
    `routing__<model>` advertisement must not offer them. Same predicate as
    routes_proxy's /v1/models routing block — one definition, so the admin view
    and the data plane cannot disagree about what "a chat model" is.
    """
    out: list[str] = []
    for m in model_router.models():
        pool = model_router.pool(m)
        if pool is not None and pool.endpoints and pool.endpoints[0].model_type == "chat":
            out.append(m)
    return out


@router.get("/routing/config")
async def get_routing_config(_: AdminDep) -> dict[str, Any]:
    """The routing runtime config: merged values, defaults, overrides, and the
    registry context the panel needs to render its checkboxes.

    effective_enabled is the AND the rest of the gateway will honor — the env
    flag still owns the MOUNT (unmounted = 404), the DB row owns whether
    routing ACTS. Reported as one field so the console shows one pill instead
    of asking the operator to compute an AND.
    """
    from ..core import routing_config

    cfg = await routing_config.get_config()
    overridden: set[str] = set()
    try:
        rows = await db.fetch("SELECT key FROM routing_config")
        overridden = {r["key"] for r in rows}
    except Exception:
        # The merged config already degraded to defaults/last-known; an
        # override LIST failing must not turn the whole panel into a 500.
        pass
    return {
        "config": cfg,
        "defaults": routing_config.defaults(),
        "overridden": sorted(overridden),
        "registered": model_router.models(),
        "chat_models": _routing_chat_models(),
        # The decision-model dropdown source: decision pools first (the
        # intended type), chat pools accepted for first-cut deployments.
        "decision_models": _routing_decision_models(),
        "env_enabled": settings.routing_enabled,
        "effective_enabled": bool(cfg.get("enabled")) and settings.routing_enabled,
        "placeholders": list(routing_config.PLACEHOLDERS),
        "gpu": await routing_config.gpu_load_snapshot(),
    }


@router.put("/routing/config")
async def put_routing_config(body: RoutingConfigPatch, request: Request, _: AdminDep) -> dict[str, Any]:
    """Patch the routing runtime config. Provided keys are written; a key set
    to null is CLEARED back to its default.

    Validation is against the LIVE registry, not the DB rows (the
    _validate_fallback_chain discipline): a name that is not registered today
    would be silently inert in every routing decision, so it is refused at
    save time where the operator can see why. allowed_models is deliberately
    NOT a key ACL — a key's own allowed_models stays a hard per-key boundary
    that no routing decision can widen — so this validates registry membership
    only.

    CHAT-ONLY (defect fix, cross-validation 2026-10-23): both name fields are
    validated against the CHAT pools, not the whole registry. The router's
    ladder is a chat concept, and an allowlist of embedding pools was
    ACTIVELY HARMFUL: routing_allowed_models intersects it with the chat
    candidates, the intersection came out empty, and its documented
    all-stale fallback then returned EVERY chat model — reporting the exact
    opposite of the restriction the operator saved. Refusing a non-chat name
    at save time is what keeps that fallback meaning what it says (it is
    reserved for ids that VANISHED from the registry, not for a category the
    operator should never have named).
    """
    from ..core import routing_config

    provided = body.model_dump(exclude_unset=True)
    unknown = [k for k in provided if k not in routing_config.DEFAULTS]
    if unknown:
        raise HTTPException(400, f"unknown routing config keys: {unknown}")
    if provided.get("gpu_load_source") is not None and provided["gpu_load_source"] not in (
        routing_config.GPU_LOAD_SOURCES
    ):
        raise HTTPException(
            400,
            f"gpu_load_source must be one of {list(routing_config.GPU_LOAD_SOURCES)}, "
            f"got {provided['gpu_load_source']!r}",
        )
    # CHAT pools, not the whole registry: the ladder is a chat concept, and an
    # allowlist naming only embedding pools would intersect to EMPTY against
    # the chat candidates — which trips routing_allowed_models' all-stale
    # fallback and reports EVERY chat model eligible, the opposite of the
    # saved restriction. Refusing here keeps that fallback reserved for ids
    # that genuinely vanished from the registry.
    chat_models = _routing_chat_models()
    decision_model = provided.get("decision_model")
    # The decision model is NOT a ladder candidate — it is the thing that
    # CHOOSES a ladder candidate, so it is validated against the decision/chat
    # pool set instead. "chat" stays accepted because the first cut shipped
    # before the decision model_type existed and an operator may already have
    # pointed at a chat pool; "decision" is the correct type for d3-turbo.
    decision_capable = _routing_decision_models()
    if decision_model not in (None, "") and decision_model not in decision_capable:
        raise HTTPException(
            400,
            f"decision_model {decision_model!r} is not a registered decision (or chat) model — "
            f"candidates: {decision_capable}. Register the vLLM-SR endpoint with "
            "model_type='decision' under POST /admin/models first.",
        )
    mode = provided.get("decision_mode")
    if mode is not None:
        from ..core import decision_routing

        if mode not in decision_routing.DECISION_MODES:
            raise HTTPException(
                400,
                f"decision_mode must be one of {list(decision_routing.DECISION_MODES)}, got {mode!r}",
            )
    allowed = provided.get("allowed_models")
    if isinstance(allowed, list):
        _check_list_len(allowed, "allowed_models")
        not_chat = [m for m in allowed if m not in chat_models]
        if not_chat:
            raise HTTPException(
                400,
                f"allowed_models entries are not registered CHAT models: {not_chat} — "
                f"chat pools: {chat_models}. An embedding/reranker pool can never be a "
                "routing candidate, and an allowlist of them would intersect to empty "
                '(silently meaning "every chat model", the opposite of the intent)',
            )
    # model_modalities is a {model: [modality...]} map. Validate it the way
    # allowed_models is validated: an entry naming an unregistered model is a
    # typo that would silently never match a candidate, and an unknown
    # modality would be a claim the router cannot act on.
    modalities = provided.get("model_modalities")
    if modalities is not None:
        if not isinstance(modalities, dict):
            raise HTTPException(400, "model_modalities must be an object of {model: [modality]}")
        _check_list_len(modalities, "model_modalities")
        bad_models = [m for m in modalities if m not in chat_models]
        if bad_models:
            raise HTTPException(
                400,
                f"model_modalities entries are not registered CHAT models: {bad_models} — "
                f"chat pools: {chat_models}. A capability declared for a model the router "
                "can never choose is dead config that reads as if it were in effect.",
            )
        bad_mods = sorted({
            str(x) for v in modalities.values() if isinstance(v, (list, tuple)) for x in v
            if str(x) not in ("text", "image", "video", "audio")
        })
        if bad_mods:
            raise HTTPException(
                400,
                f"model_modalities has unknown modalities {bad_mods} — "
                "allowed: text, image, video, audio",
            )
    merged = await routing_config.set_values(provided)
    await _audit(
        request,
        action="routing.config",
        resource_type="system",
        resource_id="routing",
        # The PROMPT is operator prose up to 20 000 chars — auditing it
        # verbatim would put a full prompt body in every row of an append-only,
        # never-pruned trail (put_distiller_config's inherited shape, which had
        # the same problem at a smaller scale). Record its LENGTH instead; every
        # other value is a bounded scalar or a model-name list.
        details={
            "keys": sorted(provided),
            "values": {
                k: (f"<{len(v)} chars>" if k == "prompt" and isinstance(v, str) else v) for k, v in provided.items()
            },
        },
    )
    return {"config": merged}


@router.post("/routing/prompt-preview")
async def preview_routing_prompt(body: RoutingPromptPreview, _: AdminDep) -> dict[str, Any]:
    """Render the decision-model prompt WITHOUT writing anything.

    Read-only by design (no audit row): the preview exists so an operator can
    iterate on wording, and auditing every keystroke-preview would bury the
    real mutations in the trail. Overrides are in-memory only — the saved
    config is untouched, so "preview then discard" needs no revert path.

    used_placeholders is computed against the TEMPLATE (before substitution),
    which is what makes it useful: an empty list means the prompt references no
    live data at all. A name missing from the list means the prompt does not
    contain that exact token — which MAY be a misspelling ({{gpu-load}} instead
    of {{gpu_load}}) or may simply be a placeholder this prompt does not use.
    The field reports PRESENCE, not correctness.
    """
    from ..core import routing_config

    cfg = await routing_config.get_config()
    template = body.prompt if body.prompt is not None else str(cfg.get("prompt") or "")
    decision_model = body.decision_model if body.decision_model is not None else str(cfg.get("decision_model") or "")
    inject = body.inject_gpu_load if body.inject_gpu_load is not None else bool(cfg.get("inject_gpu_load"))
    # CHAT pools only: the router's ladder is a chat concept, and the prompt
    # must not offer the decision model an embedding/reranker candidate it
    # could never serve. Same candidate set presented-models reports as
    # routing_eligible, so the preview and the panel cannot disagree.
    models = await routing_config.routing_allowed_models(_routing_chat_models())
    snapshot = await routing_config.gpu_load_snapshot()
    # 'inject' is the DECISION being previewed (possibly overridden); the
    # snapshot's own 'available' is only the data question. Folding the
    # override in here is what lets an operator preview inject=false on a site
    # whose evaluator does have live samples.
    gpu = {**snapshot, "inject": inject}
    rendered = routing_config.render_prompt(template, gpu=gpu, models=models, decision_model=decision_model)
    try:
        cap = max(200, int(cfg.get("max_prompt_chars") or 8000))
    except (TypeError, ValueError):
        cap = 8000
    truncated = len(rendered) > cap
    if truncated:
        rendered = rendered[:cap]
    used = [name for name in routing_config.PLACEHOLDERS if "{{" + name + "}}" in template]
    return {
        "rendered": rendered,
        "truncated": truncated,
        "chars": len(rendered),
        "gpu": snapshot,
        "models": models,
        "used_placeholders": used,
    }


@router.get("/presented-models")
async def presented_models(_: AdminDep) -> dict[str, Any]:
    """Everything GET /v1/models can advertise — REGISTRY TRUTH + CONFIG TRUTH.

    WHY an admin route instead of reading /v1/models: that listing is
    caller-scoped (a key's allowed_models filters it, and the routing entries
    depend on that key's PROFILE policy), so an operator debugging "why is
    model X missing from my dropdown" needs a view that is not itself filtered
    by one key's ACL. Admin keys hold no profile, so there is no ordering to
    read: 'order' is DERIVED (registered models, chat first, then name) and
    every alias 'reason' says so rather than presenting a derivation as fact.

    Honest by construction: 'present' reflects what the data plane WOULD
    advertise under this registry/config, not what one caller sees. A model
    whose key ACL excludes it is still 'registered' here — that difference is
    exactly the thing this view exists to make legible.
    """
    from ..core import routing_config
    from ..core.cascade import DEMAND_AWARE_ALIAS

    registered = model_router.models()
    snapshot = await routing_config.gpu_load_snapshot()
    raw_offpeak = snapshot.get("models")
    offpeak_models: dict[str, Any] = raw_offpeak if isinstance(raw_offpeak, dict) else {}
    chat_models = _routing_chat_models()
    # DERIVED ordering (no profile on an admin key): chat models first, then
    # the rest, each group name-sorted. Chat-first because the cascade aliases
    # are chat concepts — an embedding pool in the head slot would be a
    # nonsensical "default model".
    order = chat_models + [m for m in sorted(registered) if m not in set(chat_models)]
    routing_eligible_set = set(await routing_config.routing_allowed_models(chat_models))

    models_out: list[dict[str, Any]] = []
    for m in registered:
        pool = model_router.pool(m)
        endpoints = pool.endpoints if pool is not None else []
        healthy = pool.healthy() if pool is not None else []
        off = offpeak_models.get(m) or {}
        models_out.append(
            {
                "id": m,
                "type": endpoints[0].model_type if endpoints else "chat",
                "primary": bool(order) and order[0] == m,
                "endpoints": len(endpoints),
                "healthy": len(healthy),
                # The clamp that actually applies to this model's upstream
                # calls (config.py max_tokens_ceiling); None = uncapped. Shown
                # here because a silently reduced max_tokens is otherwise
                # invisible from the client side.
                "max_tokens_ceiling": settings.max_tokens_ceiling.get(m),
                "offpeak": {
                    "value_pct": off.get("value_pct"),
                    "active": off.get("active"),
                    "fraction": off.get("fraction"),
                },
                # Never picked as the demand-aware conversation head, but still
                # reachable pinned / as a budget fall-through.
                "demand_aware_excluded": m in set(settings.demand_aware_excluded_models),
                # Whether the ROUTER may route to it (core.routing_config
                # allowed_models ∩ chat pools). Config truth, not a key ACL.
                "routing_eligible": m in routing_eligible_set,
            }
        )

    # The alias pair /v1/models inserts ahead of the registered models. Both
    # are advertised under ONE condition there (at least one callable ordering
    # candidate); with a derived ordering that is simply "the registry is not
    # empty".
    alias_present = bool(order)
    aliases = [
        {
            "id": "auto",
            "type": "cascade",
            "order": order,
            "demand_aware": False,
            "present": alias_present,
            "reason": "admin view — a key profile order decides actual presentation",
        },
        {
            "id": DEMAND_AWARE_ALIAS,
            "type": "cascade",
            "order": order,
            "demand_aware": True,
            "present": alias_present,
            "reason": "admin view — a key profile order decides actual presentation",
        },
    ]

    # Pseudo-models. /v1/models gates these on the FLEET switch AND the calling
    # key's profile policy; an admin key has no profile, so only the fleet
    # switch is knowable here and the reason says as much.
    # A registry with no CHAT pool can never resolve a routing decision:
    # model_routing refuses a bare `routing` without an ordering and has no
    # `routing__X` to offer. Claiming present there would be the same kind of
    # lie as an unconditional pool pill.
    routing_present = bool(settings.routing_enabled) and bool(chat_models)
    routing_reason = (
        "fleet switch on — a key profile routing_policy in shadow/active decides actual presentation"
        if routing_present
        else "PCAI_LLM_ROUTING_ENABLED is off — no key sees the routing pseudo-models"
    )
    routing_out = [
        {"id": "routing", "type": "routing", "present": routing_present, "reason": routing_reason},
        *(
            {
                "id": f"routing__{m}",
                "type": "routing",
                "present": routing_present,
                "reason": routing_reason,
            }
            for m in chat_models
        ),
    ]

    presented = len(models_out) + sum(1 for a in aliases if a["present"]) + sum(1 for r in routing_out if r["present"])
    return {
        "aliases": aliases,
        "models": models_out,
        "routing": routing_out,
        "env": {
            "routing_enabled": bool(settings.routing_enabled),
            "load_aware_cascade": bool(settings.load_aware_cascade),
        },
        "counts": {"presented": presented, "registered": len(registered)},
    }
