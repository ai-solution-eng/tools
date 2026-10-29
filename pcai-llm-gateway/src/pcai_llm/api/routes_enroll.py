"""Self-service enrollment (invite codes) + per-key MCP credential overrides.

Three surfaces live here, all backend for the "admin-burden killer" batch:

1. ``POST /v1/enroll`` — the PUBLIC self-service door. No auth BY DESIGN: the
   bearer of a minted code IS the credential. The brute-force surface is
   neutralized by code entropy (32 random bytes, urlsafe), hash-only storage
   (SHA-256, same discipline as api_keys), a generic rejection message (no
   expired/exhausted/revoked oracle), and an in-process per-IP redeem
   throttle (10 attempts/hour -> 429).

2. Admin enrollment management (AdminDep-gated): mint / list / revoke codes.
   The plaintext code is returned exactly once at mint time; only its hash
   is stored, so a database leak cannot redeem anything.

3. Per-key MCP credential overrides (``/admin/keys/{id}/mcp-creds``): the
   memory-MCP pattern — a tenant identifies itself to an upstream MCP server
   with request headers (X-Memory-Dataset / X-Dataset-Password). Headers are
   stored Fernet-encrypted JSON per (key, server), never returned in
   plaintext by any endpoint, and merged into upstream call headers
   AFTER server-level auth (see routes_mcp.resolve_mcp_headers).

Profile seeding replicates routes_admin's create_key seeding statements
verbatim (this module may not edit routes_admin): a key created through an
enrollment with a profile gets the profile's budgets + MCP grants stamped on
at creation, in one transaction.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from ..config import settings
from ..core import enroll_memory, secretbox
from ..core.audit import audit_action
from ..core.budgets import split_preset_budgets
from ..core.identity import (
    IDP_OAUTH2_PROXY,
    IDP_SELF_ASSERTED,
    MCP_CALLER_CLASS_HEADER,
    MCP_CALLER_HEADER,
    MCP_CALLER_SUBJECT_HEADER,
    VIA_ENROLLMENT,
    VIA_SSO_CREATE,
    bind_key_subject,
    browser_identity_from_headers,
    normalize_subject,
    settings_trust_proxy_identity,
)
from ..core.keys import create_key, hash_key
from ..db import db
from ..routing.router import router as model_router
from .routes_admin import AdminDep

log = logging.getLogger(__name__)

# Public self-service router (the enroll door) and the admin management
# router. BOTH are exported; app.py wires them alongside the other routers.
router = APIRouter(prefix="/v1", tags=["enroll"])
admin_router = APIRouter(prefix="/admin", tags=["enroll-admin"])

CODE_PREFIX = "enroll-"
CODE_BYTES = 32  # brute-force-proof: 2^256 keyspace, same size as api_keys
ENROLL_CODE_RE = re.compile(r"^enroll-[A-Za-z0-9_-]+$")

# Redeem throttle (in-process, same spirit as any in-process counter):
# keyed by client IP, sliding 1h window, attempts counted BEFORE validation
# so garbage-code spraying is throttled identically to real attempts.
REDEEM_WINDOW_S = 3600.0
REDEEM_MAX_ATTEMPTS = 10
_MAX_TRACKED_IPS = 10_000  # bound the dict; stale IPs pruned opportunistically
_redeem_attempts: dict[str, list[float]] = {}

# Single-statement ATOMIC redeem: Postgres serializes concurrent redeemers on
# the row lock and re-evaluates the WHERE clause for the loser (READ
# COMMITTED), so a code cannot be double-spent past max_uses. Every refusal
# shape (unknown / expired / exhausted / revoked) returns no row — callers
# emit ONE generic message, never a per-cause oracle.
_REDEEM_SQL = (
    "UPDATE enrollments SET uses = uses + 1 "
    "WHERE code_hash = $1 AND revoked_at IS NULL "
    "AND (expires_at IS NULL OR expires_at > now()) "
    "AND (max_uses = 0 OR uses < max_uses) "
    "RETURNING id, tenant_id, profile_id"
)
_GENERIC_CODE_REJECTION = "invalid, expired, or exhausted code"

ENROLL_SCOPES = ["proxy", "mcp"]

# Per-key MCP credential override limits (header-injection hygiene: these
# become upstream request headers, so both names and values are bounded).
MAX_CRED_HEADERS = 16
MAX_CRED_VALUE_LEN = 512
HEADER_NAME_RE = re.compile(r"^[A-Za-z0-9-]+$")
# Reserved override names (see _validate_creds_headers): the gateway's own
# attribution stamps, imported from the identity module so the stamp sites
# and the reservation site can never drift apart. X-MCP-Caller rides every
# keyed call; -Subject/-Class ride it when a real subject resolved (the
# SQLhandler relay-attribution vocabulary).
RESERVED_HEADER_NAMES = (
    MCP_CALLER_HEADER,
    MCP_CALLER_SUBJECT_HEADER,
    MCP_CALLER_CLASS_HEADER,
    # Routing-relevant hop headers (audit P1-5): a tenant override that set
    # Host / X-Forwarded-* / X-Real-IP / Forwarded / Content-Length picked
    # the upstream vhost or forged forwarding metadata on an in-cluster MCP
    # call (verified: httpx2 emits a caller-supplied Host verbatim). These
    # are transport/hop concerns the CALLER does not get to choose.
    "Host",
    "Content-Length",
    "Forwarded",
    "X-Forwarded-For",
    "X-Forwarded-Host",
    "X-Forwarded-Proto",
    "X-Forwarded-Port",
    "X-Real-IP",
    # Aligned with routes_mcp._RESERVED_HOP_HEADERS (the post-merge strip is
    # the matching pair): hop-by-hop names a caller never chooses, plus the
    # reverse-proxy override/routing headers. A 400 here keeps the refusal
    # loud — the strip alone would silently no-op a stored row.
    "X-Forwarded-Prefix",
    "Transfer-Encoding",
    "Connection",
    "TE",
    "Upgrade",
    "X-HTTP-Method-Override",
    "X-Original-URL",
    "X-Rewrite-URL",
)


def generate_code() -> str:
    """A fresh enrollment code: `enroll-` + 32 random urlsafe bytes."""
    return CODE_PREFIX + secrets.token_urlsafe(CODE_BYTES)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


async def _exists(table: str, row_id: uuid.UUID) -> bool:
    """Existence check up front — clearer 404s than FK violations (the
    routes_admin pattern, implemented locally: this module does not edit
    routes_admin). `table` is always a literal from this file, never input."""
    return await db.fetchval(f"SELECT 1 FROM {table} WHERE id = $1", row_id) is not None


def _client_ip(request: Request) -> str:
    """Best-effort client IP for the redeem throttle.

    Behind the Istio ingress the FIRST X-Forwarded-For entry is the client
    address the mesh observed; without the header (direct/dev traffic) the
    TCP peer is used. Trade-off accepted for a soft in-process control: a
    directly-exposed deployment lets clients rotate spoofed XFF buckets —
    the code's 32-byte entropy, not this throttle, is the brute-force
    defense (the throttle exists to make spraying loud, not impossible).
    """
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


async def _enroll_audit(
    request: Request,
    *,
    action: str,
    resource_type: str,
    resource_id: str,
    actor_source: str,
    actor_subject: str = "",
    details: dict[str, Any] | None = None,
) -> None:
    """Enroll-surface audit write — the same best-effort contract as
    routes_admin._audit (core.audit: an audit failure is logged loudly and
    never fails the operation), with the enroll-specific actor vocabulary:
    actor_source='enroll' for the PUBLIC redeem door (unauthenticated by
    design — the code is the credential, so the actor is the created
    key/subject, never an admin), 'admin-key' for the AdminDep-gated
    management routes, 'proxy-identity' when a trusted browser identity is
    present. Redaction happens at the single writer; details here carry
    counts and ids only."""
    auth = request.headers.get("authorization", "")
    if actor_source != "enroll" and auth.lower().startswith("bearer "):
        from .routes_admin import stable_actor_label

        actor_subject = actor_subject or stable_actor_label(auth[7:].strip())
        actor_source = "admin-key"
    elif actor_source != "enroll" and settings_trust_proxy_identity():
        browser = browser_identity_from_headers(dict(request.headers))
        if browser is not None:
            actor_subject = actor_subject or browser[0]
            actor_source = "proxy-identity"
    rid = getattr(request.state, "request_id", None)
    fwd = request.headers.get("x-forwarded-for", "")
    source_ip = fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else None)
    await audit_action(
        None,  # the audit module's own seam (see core.audit.audit_action)
        actor_subject=actor_subject or "",
        actor_source=actor_source,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        details=details,
        request_id=str(rid) if rid else (request.headers.get("x-request-id") or None),
        source_ip=source_ip or None,
    )


def _throttle_redeem(request: Request) -> None:
    """Count one redeem attempt for this IP; 429 past 10 in the hour window.

    In-process by design (single-process-per-pod, same as the concurrency
    counter): multi-replica deployments get per-pod budgets, which is the
    accepted posture for every in-process counter in this gateway.
    """
    ip = _client_ip(request)
    now = time.monotonic()
    recent = [t for t in _redeem_attempts.get(ip, []) if now - t < REDEEM_WINDOW_S]
    recent.append(now)
    over = len(recent) > REDEEM_MAX_ATTEMPTS
    # cap the STORED list (review flag: the trim must bound memory even while
    # every attempt 429s — check the TRUE count first, then trim before
    # storing; the next attempt's filter+append re-crosses the threshold).
    del recent[:-REDEEM_MAX_ATTEMPTS]
    _redeem_attempts[ip] = recent
    # HARD cap on distinct tracked IPs (audit: spoofed-XFF spray minted one
    # dict entry per forged address and the old prune only removed entries
    # whose last attempt was >= 1h old — N fresh entries all survived).
    # Evict oldest-INSERTED (FIFO) beyond the cap; a spoofed spray then
    # evicts itself rather than OOMing the pod. Tracking is a soft, loud-
    # spray control (see _client_ip), so FIFO loss is acceptable.
    while len(_redeem_attempts) > _MAX_TRACKED_IPS:
        oldest = next(iter(_redeem_attempts))
        _redeem_attempts.pop(oldest, None)
    stale = [k for k, v in _redeem_attempts.items() if not v or now - v[-1] >= REDEEM_WINDOW_S]
    for k in stale:
        _redeem_attempts.pop(k, None)
    if over:
        raise HTTPException(
            429,
            "too many enrollment attempts from this address; try again later",
            headers={"Retry-After": "3600"},
        )


# ---------------------------------------------------------------------------
# 1. public self-service enrollment
# ---------------------------------------------------------------------------


class EnrollRequest(BaseModel):
    code: str = Field(min_length=1, max_length=200)
    name: str  # 1-64 chars AFTER strip (validated below — pydantic sees the raw string)
    # OPTIONAL self-asserted subject ("who am I"): bound to the minted key
    # with idp='self-asserted' — a CLAIM, honestly provenance-stamped (the
    # UI renders the "self-asserted" chip) and never a verification. When
    # SSO identity headers are present on the redeem request (the redeem
    # page served behind oauth2-proxy), the BROWSER identity wins and this
    # field is ignored — no silent upgrade, but the verified identity beats
    # a typed one. Max 200 (subjects.subject column hygiene).
    subject: str | None = Field(default=None, max_length=200)
    # OPTIONAL memory opt-in (review §2; DECISIONS: "opt-in at redemption"):
    # True/False = the redeemer's explicit answer to "also provision my
    # private memory dataset?"; None (omitted) falls back to the
    # deployment default (PCAI_LLM_ENROLL_MEMORY_DEFAULT) — or the
    # enrollment's profile preset ("memory": {"provision": …}) when the
    # profile declares one. A provisioning FAILURE never fails the
    # redemption: the response reports it in `memory.provisioned=False`.
    memory_opt_in: bool | None = None

    @field_validator("name")
    @classmethod
    def _clean_name(cls, v: str) -> str:
        v = v.strip()
        if not 1 <= len(v) <= 64:
            raise ValueError("name must be 1-64 characters after trimming whitespace")
        return v


async def _create_enroll_user(
    tenant_id: uuid.UUID,
    name: str,
    *,
    profile_id: uuid.UUID | None = None,
    conn: Any | None = None,
) -> dict[str, Any]:
    """Insert the enrollment user; keys are identity, names are labels.

    users carries UNIQUE (tenant_id, name), but enrollment must let two
    people redeem into the same tenant as "Alice" — a collision is retried
    with a short random suffix instead of failing the redemption (schema.py
    is off-limits for this feature, so the API absorbs the constraint).

    Audit fixes: (a) `profile_id` is now STAMPED at insert (finding 8c —
    enrolled users previously never linked the profile, so cascade ordering,
    routing policy, and the profile off-peak fraction silently never applied
    to those keys); (b) an optional `conn` joins the caller's transaction so
    the redeem flow (code consumption + user + key + presets) commits or
    rolls back AS ONE.
    """
    target = conn if conn is not None else db
    for attempt in range(3):
        candidate = name if attempt == 0 else f"{name}-{secrets.token_hex(2)}"
        try:
            row = await target.fetchrow(
                "INSERT INTO users (tenant_id, name, profile_id) VALUES ($1, $2, $3) RETURNING id, name",
                tenant_id,
                candidate,
                profile_id,
            )
        except asyncpg.UniqueViolationError:
            continue
        assert row is not None
        return dict(row)
    raise HTTPException(503, "could not allocate a user name; retry the enrollment")


async def _read_profile_seed_tx(
    profile_id: uuid.UUID,
    conn: Any | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any], list[str]]:
    """READ + VALIDATE a profile's presets WITHOUT writing anything (the
    validate-before-write half of the redeem fix, finding 8): an invalid
    budget preset is a 400 naming the profile and — because the caller runs
    this INSIDE the redeem transaction — consumed NOTHING (previously an
    invalid preset raised an unhandled CheckViolation 500 AFTER the redeem
    had burned a single-use code).

    Returns (budgets, parsed defaults, model_order). The model_order rides
    back so the redeem flow can stamp the key's model ACL (finding 8b —
    enrolled keys previously minted allow-all)."""
    target = conn if conn is not None else db
    prow = await target.fetchrow("SELECT id, name, defaults_json, model_order FROM profiles WHERE id = $1", profile_id)
    if prow is None:
        # Profile deleted between mint and redeem: the enrollment still
        # succeeds (the key is valid) — it just carries no presets.
        log.warning("enrollment profile %s vanished before redeem; key created without presets", profile_id)
        return [], {}, []
    defaults = enroll_memory.parse_profile_defaults(prow["defaults_json"])
    budgets = defaults.get("budgets", [])
    # Validate BEFORE any write: a schema CHECK violation here used to be a
    # 500 mid-redeem (code burned). Refuse with the profile named. The
    # splitter is the SAME validator the admin profile save runs, so a
    # preset this build would refuse on save can never reach a redeem; the
    # refusal names the entry because the operator has to go fix the profile.
    try:
        split_preset_budgets(budgets)
    except ValueError as exc:
        raise HTTPException(
            400,
            f"profile {prow['name']!r} has an invalid budget preset ({exc}) — ask the operator to fix the profile",
        ) from None
    model_order_raw = prow["model_order"] if (hasattr(prow, "keys") and "model_order" in prow.keys()) else []  # noqa: SIM118 — fake rows are dicts, Record membership is fine either way
    model_order = [str(m) for m in (model_order_raw or [])]
    return budgets, defaults, model_order


async def _seed_profile_defaults(
    profile_id: uuid.UUID,
    key_id: uuid.UUID,
    profile_seed: tuple[list[dict[str, Any]], dict[str, Any], list[str]],
    *,
    conn: Any | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any], list[str]]:
    """WRITE the (already-validated) profile presets onto the fresh key —
    the SAME statements routes_admin's create-key seeding runs (budgets rows
    + mcp_tool_acls grants). Called INSIDE the redeem transaction (conn
    passed) so code consumption + user + key + presets commit or roll back
    as one. Returns (budgets, defaults, model_order) — the parsed defaults
    ride along so the memory decision (core.enroll_memory) reads the SAME
    parse the seeding used, never a second divergent one."""
    budgets, defaults, model_order = profile_seed
    grants = defaults.get("mcp_grants", [])
    # The seed writer is only ever called from the redeem transaction (conn
    # always provided); the Any-typed conn carries executemany — the
    # module-pool fallback below exists for direct/test callers whose fakes
    # expose executemany too.
    target: Any = conn if conn is not None else db
    # Both preset KINDS are stamped (core.budgets.split_preset_budgets):
    # token presets -> budgets, dollar presets -> cost_budgets. The
    # validation already ran in _read_profile_seed_tx, so the split here
    # cannot raise.
    token_rows, usd_rows = split_preset_budgets(budgets)
    if token_rows:
        await target.executemany(
            "INSERT INTO budgets (key_id, model, period, limit_tokens) VALUES ($1,$2,$3,$4)",
            [(key_id, m, p, lim) for m, p, lim in token_rows],
        )
    if usd_rows:
        await target.executemany(
            "INSERT INTO cost_budgets (key_id, model, period, limit_usd) VALUES ($1,$2,$3,$4)",
            [(key_id, m, p, lim) for m, p, lim in usd_rows],
        )
    if grants:
        await target.executemany(
            "INSERT INTO mcp_tool_acls (key_id, server, tool, allowed) "
            "VALUES ($1,$2,$3,TRUE) ON CONFLICT (key_id, server, tool) DO UPDATE SET allowed = TRUE",
            [(key_id, g[0], g[1]) for g in grants],
        )
    return budgets, defaults, model_order


@router.post("/enroll", status_code=201)
async def enroll(body: EnrollRequest, request: Request) -> dict[str, Any]:
    """Redeem an enrollment code for a personal API key. NO AUTH — this is
    the self-service door; the code is the credential.

    Flow: throttle -> format gate -> ATOMIC redeem (single UPDATE) -> create
    user + key (proxy+mcp scopes) -> apply the enrollment's profile presets
    -> OPTIONAL per-user memory dataset (opt-in; never fatal) -> optional
    subject binding (Phase A identity). The plaintext key is returned
    exactly once (hash-only storage, same convention as every key this
    gateway mints).

    Binding (Phase A, additive — a failed binding NEVER fails the
    redemption; the key then stays anonymous, i.e. today's behavior):
    - SSO headers on the redeem request (the page is served behind
      oauth2-proxy) AND PCAI_LLM_TRUST_PROXY_IDENTITY=true → the browser
      identity is bound, idp='oauth2-proxy' — the VERIFIED identity wins.
    - else the request's optional self-asserted `subject` → idp='self-
      asserted' (a claim, provenance-stamped so the UI can chip it honestly).
    bound_via='enrollment' in both cases.

    Memory (review §2, opt-in at redemption): when the opt-in resolves
    True (request field → profile preset → deployment default), a private
    RAG dataset `mem-<tenant>-<user>-<key6>` is provisioned through the
    REGISTERED memory MCP server's REST API and its credential pair
    stamped onto this key's mcp_server_creds row (write-only, merged into
    this key's relay calls to that server only). The response's `memory`
    block reports the outcome; the password rides it ONCE. ORDERING: key
    first, then provision, then stamp — the over-provision-on-crash
    posture rotate_key uses (a crash can only leave a live key without
    memory, never a provisioned dataset without a live key).
    """
    _throttle_redeem(request)  # counts BEFORE validation: spraying is throttled too
    code = body.code.strip()
    if not ENROLL_CODE_RE.match(code):
        # Same generic message as every dead-code path — the format gate
        # must not become a cheap oracle distinguishing "malformed" from
        # "real but spent" codes.
        raise HTTPException(400, _GENERIC_CODE_REJECTION)

    # --- TRANSACTIONAL REDEEM (audit finding 8) ---
    # Code consumption + user + key + preset seeding now commit or roll back
    # AS ONE: any failure inside the transaction (name allocation, key
    # insert, a preset insert, the preset-validation 400 below) rolls the
    # uses-counter BACK, so a single-use code is never burned by a transient
    # failure or a bad profile preset — the redeemer can simply retry, and
    # the validation 400 consumes nothing.
    # Model ACL (finding 8b): the key's allowed_models is stamped from the
    # profile's model_order ∩ registered models — the ordering is the
    # admin's signed-off model set for this enrollment; an empty ordering
    # keeps allow-all (unchanged semantics for un-profiled codes).
    registered = set(model_router.models())
    async with db.acquire() as conn, conn.transaction():
        redeemed = await conn.fetchrow(_REDEEM_SQL, hash_key(code))
        if redeemed is None:
            raise HTTPException(400, _GENERIC_CODE_REJECTION)
        tenant_id: uuid.UUID = redeemed["tenant_id"]
        profile_id: uuid.UUID | None = redeemed["profile_id"]

        # Read + VALIDATE the profile seed INSIDE the transaction, before
        # any other write: an invalid preset raises 400 → the whole
        # transaction (including the uses-increment) rolls back.
        profile_seed = await _read_profile_seed_tx(profile_id, conn) if profile_id is not None else None
        if profile_seed is not None:
            _, profile_defaults, profile_order = profile_seed
            allowed_models = [m for m in profile_order if m in registered] if profile_order else []
        else:
            profile_defaults, allowed_models = {}, []

        user = await _create_enroll_user(tenant_id, body.name, profile_id=profile_id, conn=conn)
        # stream_pcai_events=True: the DSH harness (this enrollment's primary
        # client) PARSES the in-stream {"pcai": …} telemetry (consumer-inventory
        # finding) — enroll keys opt in even though the new-key default is now
        # spec-clean FALSE. SSO for-me keys below take the new default.
        plaintext, key_id = await create_key(
            user["id"], body.name, ENROLL_SCOPES, allowed_models or None, stream_pcai_events=True, conn=conn
        )
        if profile_id is not None and profile_seed is not None:
            budgets, _pd, _order = await _seed_profile_defaults(profile_id, key_id, profile_seed, conn=conn)
        else:
            budgets = []
    tenant_name = await db.fetchval("SELECT name FROM tenants WHERE id = $1", tenant_id)

    # --- optional per-user memory dataset (review §2; NEVER fatal) ---
    # The opt-in resolution happens even before the "should we" check so a
    # profile preset is honored exactly once per redeem; provisioning is a
    # black box that returns a report block and cannot raise.
    memory_block: dict[str, Any] = {"provisioned": False}
    if enroll_memory.memory_opt_in_requested(body.memory_opt_in, profile_defaults):
        memory_block = await enroll_memory.provision_memory_for_key(
            key_id,
            tenant_name=tenant_name or "",
            user_name=user["name"],
            profile_defaults=profile_defaults,
        )

    # --- Phase A subject binding (best-effort; see the docstring) ---
    bound_subject: str | None = None
    bound_idp = ""
    # Browser identity wins when present AND the trust flag is on — the same
    # rule as the resolver's rung 1 (identity.py): flag + an ENFORCING proxy
    # in front is the deployment precondition.
    browser: tuple[str, list[str]] | None = None
    if settings_trust_proxy_identity():
        browser = browser_identity_from_headers(dict(request.headers))
    if browser is not None:
        bound_subject, bound_idp = browser[0], IDP_OAUTH2_PROXY
    elif body.subject is not None and normalize_subject(body.subject):
        bound_subject, bound_idp = normalize_subject(body.subject), IDP_SELF_ASSERTED
    if bound_subject is not None:
        subject_id = await bind_key_subject(
            key_id,
            subject=bound_subject,
            idp=bound_idp,
            bound_via=VIA_ENROLLMENT,
            tenant_id=tenant_id,
            display_name=user["name"],
            groups=browser[1] if browser else [],
        )
        if subject_id is None:
            # degrade-not-fail: the key is live and returned; only the
            # identity enrichment was refused.
            bound_subject, bound_idp = None, ""

    # AUDIT (enroll.redeem): the redemption IS an admin-plane mutation (it
    # creates a key, user, budgets, grants) — actor_source='enroll' marks
    # the unauthenticated-by-design door. The CODE itself is never recorded
    # (not even its hash — enrollments.code_hash already holds it); details
    # carry the provisioning outcome, never credential values.
    await _enroll_audit(
        request,
        action="enroll.redeem",
        resource_type="key",
        resource_id=str(key_id),
        actor_source="enroll",
        actor_subject=bound_subject or user["name"],
        details={
            "user": user["name"],
            "tenant_id": str(tenant_id),
            "profile_id": str(profile_id) if profile_id else None,
            "budgets_applied": len(budgets),
            "memory_provisioned": bool(memory_block.get("provisioned")),
            "subject_bound": bound_subject,
            "bound_idp": bound_idp,
        },
    )

    return {
        "key": plaintext,
        "key_prefix": plaintext[:10],
        "user": user["name"],
        "tenant": tenant_name or "",
        "budgets": budgets,
        "subject": bound_subject,
        "idp": bound_idp,
        # Review §2: the memory outcome rides EVERY response (provisioned
        # false when not opted in — the field's presence is the contract;
        # callers branch on it instead of sniffing for missing keys). The
        # dataset password, when present, is shown ONCE here alongside the
        # key and is then only ever stored Fernet-encrypted.
        "memory": memory_block,
        "message": "copy this key now — it is shown once",
    }


# ---------------------------------------------------------------------------
# 1b. SSO-authenticated self-service key creation (Phase B; review §1.3 flow 2)
# ---------------------------------------------------------------------------


class ForMeKeyCreated(BaseModel):
    """The /v1/keys/for-me response: the shown-once plaintext, the binding
    provenance (subject + idp — always the CALLER'S OWN identity), and the
    message convention every key-minting surface shares."""

    key: str
    key_prefix: str
    subject: str
    idp: str
    message: str = "copy this key now — it is shown once"


FOR_ME_SCOPES = ["proxy", "mcp"]
FOR_ME_RATE_LIMIT_RPM = 60
FOR_ME_RATE_LIMIT_TPM = 100_000


class ForMeMintRequest(BaseModel):
    """The OPTIONAL /v1/keys/for-me body. One field, deliberately: a mint
    can never carry grants (scopes/limits are fixed — the no-admin-
    escalation contract the no-body tests pin); ``rotate`` is the
    one-key-per-identity replacement consent (a second blind mint for a
    subject that already holds a live key is a 409 — the D25 mirror).
    Extra body fields are ignored, so legacy clients that POST junk (or
    nothing at all) keep minting exactly as before.

    NOTE: this model is only ever built by _parse_for_me_body (the
    endpoint reads the request manually — see that helper for why a
    pydantic body parameter was rejected)."""

    rotate: bool = False


async def _parse_for_me_body(request: Request) -> ForMeMintRequest:
    """Tolerant body parse for the mint endpoint.

    WHY not a pydantic body parameter: FastAPI's parser 422s a MALFORMED
    body (invalid JSON, a non-object like `[1,2]`) BEFORE the handler
    runs — but this surface's legacy callers (pre-feature scripts, curl
    one-liners) POST no body or junk, and pre-change those bodies reached
    the mint and succeeded (the endpoint took no body at all). A 422
    would be a flag-OFF-visible behavior change (cross-cutting invariant
    1: with the flags off every response byte equals the baseline). So
    the body is read manually: absent, blank, non-object, or unparseable
    → ForMeMintRequest() (rotate=False — the exact legacy semantics); an
    object body → pydantic (extra fields ignored, unknown types coerce
    as pydantic always has)."""
    raw = await request.body()
    if not raw or not raw.strip():
        return ForMeMintRequest()
    try:
        import json as _json

        parsed = _json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return ForMeMintRequest()
    if not isinstance(parsed, dict):
        return ForMeMintRequest()
    try:
        return ForMeMintRequest.model_validate(parsed)
    except Exception:
        # a JSON object whose fields don't validate (e.g. rotate: "yes")
        # is a junk body like any other — legacy semantics, rotate=False
        return ForMeMintRequest()


# The owner-view / rotate-lookup query pair: UNIQUE (idp, subject) →
# subject row → the subject's NEWEST NON-REVOKED sso-create key (the same
# newest-wins shape find_shadow_key uses for the token-shadow keys).
_FOR_ME_SUBJECT_SQL = "SELECT id FROM subjects WHERE idp = $1 AND subject = $2"

_FOR_ME_KEY_SQL = """
SELECT k.id, k.user_id, k.key_prefix, k.name, k.created_at, k.spend_ancestors
FROM api_keys k
WHERE k.subject_id = $1 AND k.bound_via = $2 AND k.revoked_at IS NULL
ORDER BY k.created_at DESC, k.id DESC
LIMIT 1
"""

_FOR_ME_REVOKE_SQL = "UPDATE api_keys SET revoked_at = now() WHERE id = $1 AND revoked_at IS NULL"

async def _bind_user_profile(conn: Any, user_id: uuid.UUID, profile: dict[str, Any] | None) -> None:
    """Bind an SSO-created user to the tenant default profile when it has none.

    WHY (2026-10-29): the tenant default profile used to land ONLY as stamped
    presets on the key row, so everything that reads users.profile_id — the
    cascade ordering (core.cascade.profile_order_for_key), the routing policy,
    the off-peak fraction (keys.authenticate's LEFT JOIN), and the agents menu
    (core.agents.for_key) — saw NO profile. "SSO defaults to <profile>" has to
    mean the user ROW or the profile is only half applied: the key gets its
    budgets and grants while every profile-scoped feature silently ignores it.

    ONLY WHEN NULL: an operator who assigned a profile by hand (Keys > Profile)
    keeps it. The tenant setting is a DEFAULT, not an override — a mint must
    never silently re-scope a user an operator already placed.

    Runs INSIDE the mint transaction (same conn), so a failed stamp and a
    failed bind roll back together: no key is issued against a half-applied
    profile.
    """
    if profile is None:
        return
    await conn.execute(
        "UPDATE users SET profile_id = $2 WHERE id = $1 AND profile_id IS NULL",
        user_id,
        profile["id"],
    )



async def _sso_default_profile(conn: Any, tenant_id: uuid.UUID) -> dict[str, Any] | None:
    """The tenant's configured SSO self-mint default profile (admin sets it
    via PUT /admin/tenants/{id}/sso-default-profile), or None when unset /
    the profile row vanished (profiles FK is ON DELETE SET NULL — a deleted
    profile degrades to the bare grant, never blocks a mint). Shape mirrors
    routes_admin's create-key preset resolution: budgets (model/period plus
    exactly one of limit_tokens / limit_usd), mcp_grants ([server, tool]
    pairs), limits (rpm/tpm/max_concurrent presets that OVERRIDE the
    mint's bare defaults)."""
    row = await conn.fetchrow(
        "SELECT p.id, p.name, p.defaults_json FROM tenants t "
        "JOIN profiles p ON p.id = t.sso_default_profile_id WHERE t.id = $1",
        tenant_id,
    )
    if row is None:
        return None
    try:
        defaults = json.loads(row["defaults_json"] or "{}")
    except (json.JSONDecodeError, TypeError):
        defaults = {}
    return {
        "id": row["id"],
        # name rides along so a stamp-time config failure can NAME the
        # profile in the 503 the self-mint returns (an operator cannot act
        # on "invalid budget preset" without knowing which profile).
        "name": row.get("name", ""),
        "budgets": defaults.get("budgets", []),
        "mcp_grants": defaults.get("mcp_grants", []),
        "limits": defaults.get("limits", {}) or {},
    }


def _profile_limits(profile: dict[str, Any] | None, rpm: int, tpm: int) -> tuple[int, int]:
    """The profile's limit presets OVERRIDE the mint's bare defaults (the
    console create path's exact rule: 'the profile is the operator's
    declared baseline for this population')."""
    lim = (profile or {}).get("limits", {}) if profile else {}
    out_rpm = int(lim.get("rate_limit_rpm") or rpm)
    out_tpm = int(lim.get("rate_limit_tpm") or tpm)
    return out_rpm, out_tpm


class ProfilePresetConfigError(Exception):
    """A tenant/profile preset is invalid at STAMP time — a config failure,
    not a caller error. Raised so the for-me route can answer 503 naming the
    profile instead of leaking a bare 500 from a key-creation transaction."""


async def _stamp_profile_grants(conn: Any, key_id: uuid.UUID, profile: dict[str, Any]) -> None:
    """Stamp the profile's budgets + MCP grants onto a FRESH key row, on the
    caller's transaction (mint or rotate — both create new rows, so both
    stamp; a half-stamped key is worse than a failed mint).

    Both budget KINDS are stamped (token -> budgets, dollar ->
    cost_budgets) through the shared splitter. An invalid preset here is
    a deployment misconfiguration rather than caller input — it raises,
    rolling the mint/rotate back, instead of minting a key whose budgets
    silently differ from the profile. Callers translate the ValueError to
    a status the client can act on (the for-me route: 503 naming the
    profile) — a bare 500 would tell a user nothing they could report."""
    try:
        token_rows, usd_rows = split_preset_budgets(profile.get("budgets") or [])
    except ValueError as exc:
        raise ProfilePresetConfigError(f"profile {profile.get('name') or profile.get('id')!r}: {exc}") from None
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
    if profile.get("mcp_grants"):
        await conn.executemany(
            "INSERT INTO mcp_tool_acls (key_id, server, tool, allowed) "
            "VALUES ($1,$2,$3,TRUE) ON CONFLICT (key_id, server, tool) DO UPDATE SET allowed = TRUE",
            [(key_id, g[0], g[1]) for g in profile["mcp_grants"]],
        )


async def _live_sso_key_for_subject(subject: str) -> Any | None:
    """The subject's newest non-revoked ``sso-create`` key row, or None.

    ONE helper for BOTH readers that must agree — GET /v1/keys/for-me's
    masked view and POST's one-key conflict check (a GET that misses a
    key the POST would 409 on, or vice versa, lets the SPA and the mint
    disagree about whether this identity already holds a key). The idp is
    pinned to oauth2-proxy: this surface only ever sees browser-header
    subjects (the 401 gate upstream), and bound_via='sso-create' keys are
    minted only here. Raises on DB failure — fail-closed, like every
    identity-ledger read on an auth-adjacent surface.
    """
    if not subject:
        return None
    srow = await db.fetchrow(_FOR_ME_SUBJECT_SQL, IDP_OAUTH2_PROXY, subject)
    if srow is None:
        return None
    return await db.fetchrow(_FOR_ME_KEY_SQL, srow["id"], VIA_SSO_CREATE)


async def _self_mint_allowed(subject: str) -> bool:
    """Admin-governed eligibility for ONE browser subject, delegated to
    routes_identity._self_mint_allowed_for_subject (z2's function — the
    ONE eligibility lookup the probe's flags.self_mint and this mint both
    consult, so a blocked subject sees a consistent "not allowed" on both
    surfaces and the console toggle governs them together). The function
    reads users.self_mint_enabled (db/schema.py; written only by the
    admin API, default TRUE — existing users stay eligible).

    POSTURE (agreed with z2, wave 1): this wrapper is PASS-THROUGH — it
    never converts a failure into False. An eligibility-store outage
    raising out of the hook PROPAGATES to the route handlers, which map
    it to 503 — the probe's exact fail-closed semantics (z2's
    test_eligibility_hook_failure_is_503_fail_closed): an unavailable
    eligibility store must surface as an error, never read as "not
    eligible" — a silent False would deny every user during an outage
    with a 403 that lies about the cause. A False RETURN is the only
    "blocked" signal, and it means the lookup SUCCEEDED and said no."""
    from .routes_identity import _self_mint_allowed_for_subject

    return bool(await _self_mint_allowed_for_subject(subject))


@router.get("/keys/for-me")
async def get_key_for_me(request: Request) -> dict[str, Any]:
    """Owner-safe masked view of the CALLING BROWSER IDENTITY's own key.

    Same gates as the POST, read-only otherwise: 403 when the trust flag
    is off (the surface does not exist in that deployment posture), 401
    without the oauth2-proxy browser headers. The response is the SPA's
    branching contract — HTTP 200 ALWAYS (a 404 here would be
    indistinguishable from route-absent on older backends), with
    ``exists`` carrying the state: {"exists": false} when the subject
    holds no live sso-create key, else {"exists": true, "key_prefix",
    "name", "created_at"}. NEVER the plaintext: prefix/name/timestamp
    only — the same masked vocabulary the console keys list shows, for
    the owner's own key. No audit: a read mutates nothing.
    """
    if not settings_trust_proxy_identity():
        raise HTTPException(403, "SSO key creation is not enabled on this deployment")
    browser = browser_identity_from_headers(dict(request.headers))
    if browser is None:
        raise HTTPException(401, "no browser identity headers present")
    # ADMIN-GOVERNED ELIGIBILITY: a subject the console blocked reads the
    # SAME 403 a flags-off deployment gives — the refusal detail never
    # distinguishes "surface off" from "you are not eligible" (a blocked
    # subject probing the endpoint learns nothing). An eligibility-store
    # FAILURE is a different class entirely (the agreed z2 posture): the
    # hook's exception propagates here and maps to 503 — infrastructure
    # failure must surface as an error, never read as "not eligible".
    try:
        allowed = await _self_mint_allowed(browser[0])
    except Exception as exc:
        log.exception("keys/for-me GET: eligibility lookup failed — failing closed as 503")
        raise HTTPException(503, "SSO key creation is not available (eligibility store)") from exc
    if not allowed:
        raise HTTPException(403, "SSO key creation is not enabled on this deployment")
    row = await _live_sso_key_for_subject(browser[0])
    if row is None:
        return {"exists": False}
    created_at = row["created_at"]
    return {
        "exists": True,
        "key_prefix": row["key_prefix"],
        "name": row["name"],
        # asyncpg returns a timezone-aware datetime; isoformat() renders the
        # RFC3339 shape the SPA parses. (String in → string out: any caller
        # that pre-formats stays byte-identical.)
        "created_at": created_at.isoformat() if hasattr(created_at, "isoformat") else created_at,
    }


@router.post("/keys/for-me", status_code=201)
async def create_key_for_me(request: Request) -> ForMeKeyCreated:
    """Mint a key bound to the CALLING BROWSER IDENTITY's own subject.

    WHO may call: a request whose identity resolves through the
    oauth2-proxy BROWSER HEADERS under PCAI_LLM_TRUST_PROXY_IDENTITY=true
    (ladder rung 1 — the deployment precondition is the enforcing proxy +
    AuthorizationPolicy, documented in core/identity.py). Any other
    caller shape (bearer key, token, nothing) is a 401: this surface is
    the browser's, deliberately NOT a bearer-key self-service door (a
    leaked key could then mint replacements forever).

    WHAT it grants: a key bound to the caller's OWN subject only —
    idp='oauth2-proxy', bound_via='sso-create'. There is NO admin
    escalation here: scopes are fixed (proxy+mcp), limits are fixed
    defaults, no allowed_models, no profile, no subject choice — grants
    remain admin-controlled (federation ACLs are default-deny until an
    admin grants). 403 when the trust flag is off (the surface does not
    exist in that deployment posture).

    WHY the user row: api_keys.user_id is NOT NULL (schema contract,
    review §1.2 "do NOT merge users into subjects") — the key needs a
    tenancy/policy holder. A per-subject user named after the identity is
    created on demand (UNIQUE (tenant_id, name) collisions get the
    enrollment suffix-retry), so repeated mints for the same human land
    on the same tenant user; the SUBJECT (UNIQUE (idp, subject)) stays
    the identity anchor. The minting tenant is the shadow-key tenant
    (PCAI_LLM_TOKEN_SHADOW_TENANT_ID) — the deployment's SSO population
    home; refusing when it is unset (503: config incompleteness, fail
    loud like db.connect) rather than inventing a tenant.

    ONE KEY PER IDENTITY (the MM RAG D25 mirror): a blind second mint for
    a subject that already holds a live sso-create key is a 409 — the old
    key must be consciously replaced, not silently orphaned. The
    replacement is EXPLICIT: body {"rotate": true} creates the new key
    and revokes the old one INSIDE ONE TRANSACTION (create-then-revoke,
    both on the acquired connection) — a crash mid-rotate can leave the
    old key live and no new key (the pre-rotate state, retryable), never
    two live keys and never zero. The revoke is the same revoked_at=now()
    the admin console writes; authenticate() already refuses revoked
    keys, so the old plaintext stops working the moment the transaction
    commits. The plaintext rides the response exactly once, mint or
    rotate alike.
    """
    if not settings_trust_proxy_identity():
        raise HTTPException(403, "SSO key creation is not enabled on this deployment")
    browser = browser_identity_from_headers(dict(request.headers))
    if browser is None:
        raise HTTPException(401, "no browser identity headers present")
    subject, groups = browser
    tenant_setting = settings.token_shadow_tenant_id
    try:
        tenant_id = uuid.UUID(tenant_setting)
    except ValueError:
        raise HTTPException(503, "SSO key creation is not configured (no tenant)") from None

    # ADMIN-GOVERNED ELIGIBILITY (before the one-key lookup — a blocked
    # subject must not even learn whether it holds a key): the SAME 403
    # detail a flags-off deployment gives; the refusal never says which
    # of the two gates refused (a blocked subject probing learns nothing).
    # An eligibility-store FAILURE is a different class (the agreed z2
    # posture): the hook's exception maps to 503 here — infrastructure
    # failure must surface as an error, never read as "not eligible".
    try:
        allowed = await _self_mint_allowed(subject)
    except Exception as exc:
        log.exception("keys/for-me POST: eligibility lookup failed — failing closed as 503")
        raise HTTPException(503, "SSO key creation is not available (eligibility store)") from exc
    if not allowed:
        raise HTTPException(403, "SSO key creation is not enabled on this deployment")

    # One-key-per-identity: consult the SAME helper GET reads (the SPA's
    # exists:true and the 409 must never disagree). A rotate consent (or
    # a legacy body without the field) proceeds; a blind second mint
    # refuses with the exact shape the SPA branches on. The lookup sits
    # BEFORE the mint transaction — a concurrent double-mint is
    # serialized by the caller, and the conflict check is advisory
    # consistency, not the concurrency control. The body is parsed
    # TOLERANTLY (_parse_for_me_body: junk/absent → rotate=False, never a
    # 422 — the pre-change surface took no body, and legacy junk callers
    # must keep minting exactly as before).
    mint = await _parse_for_me_body(request)
    rotate = bool(mint.rotate)
    existing = await _live_sso_key_for_subject(subject)
    if existing is not None and not rotate:
        raise HTTPException(
            409,
            'key already exists for this identity — POST {"rotate": true} to replace it',
        )

    if existing is not None:
        # ROTATE: create-then-revoke in ONE transaction (the enroll-redeem
        # ACQUIRED-connection pattern — key + binding + revoke all on the
        # acquired connection). Any failure inside rolls EVERYTHING back —
        # the old key survives live, no new key exists (never two live
        # keys, never zero; the crash-safety contract). The revoke is the
        # same revoked_at=now() the admin console writes; authenticate()
        # already refuses revoked keys, so the old plaintext stops working
        # once the transaction commits.
        #
        # USER ROW REUSE (why NOT _create_enroll_user here): the live key
        # row IS the subject's per-subject user anchor (api_keys.user_id —
        # the invariant the admin eligibility PUT resolves subjects
        # through). _create_enroll_user's UNIQUE (tenant_id, name) retry
        # cannot run inside a transaction: attempt 1's collision aborts
        # the tx (Postgres InFailedSQLTransactionError), so every retry
        # re-raises on the SAME aborted connection — and the collision is
        # GUARANTEED on rotate, because the first mint already created
        # users.name=<subject>. Reusing the existing user_id sidesteps the
        # insert entirely AND keeps the model-consistency contract: the
        # rotated key lands on the SAME user row as the original mint
        # (no orphaned "alice-2887" user holding the revoked key's
        # attribution).
        rotate_user_id: uuid.UUID = existing["user_id"]
        async with db.acquire() as conn, conn.transaction():
            profile = await _sso_default_profile(conn, tenant_id)
            rpm, tpm = _profile_limits(profile, FOR_ME_RATE_LIMIT_RPM, FOR_ME_RATE_LIMIT_TPM)
            plaintext, key_id = await create_key(
                rotate_user_id,
                f"sso-{subject[:40]}",
                FOR_ME_SCOPES,
                rate_limit_rpm=rpm,
                rate_limit_tpm=tpm,
                conn=conn,
            )
            # bind_key_subject's conn= posture JOINS this transaction (its
            # failures propagate → rollback) — a committed mint can never
            # be anonymous, and the revoked old key can never be the
            # subject's only key.
            await bind_key_subject(
                key_id,
                subject=subject,
                idp=IDP_OAUTH2_PROXY,
                bound_via=VIA_SSO_CREATE,
                tenant_id=tenant_id,
                display_name=subject,  # the user row already carries the name
                groups=groups,
                conn=conn,
            )
            # Tenant default profile stamping on ROTATE too (the rotate's
            # fresh key row would otherwise silently DROP the budgets/grants
            # the original mint stamped — grants dying at first rotation).
            if profile is not None:
                # The profile must reach the USER row too, not just the key's
                # stamped presets — otherwise the cascade ordering, routing
                # policy, off-peak fraction and agents menu all see "no profile"
                # (see _bind_user_profile). Only when the user has none.
                await _bind_user_profile(conn, rotate_user_id, profile)
                try:
                    await _stamp_profile_grants(conn, key_id, profile)
                except ProfilePresetConfigError as exc:
                    # A tenant-default profile with a bad preset is a CONFIG
                    # failure: 503 (retryable once the operator fixes it) that
                    # NAMES the profile, instead of the opaque 500 a bare raise
                    # produced. The transaction rolls back — no key is issued.
                    log.error("keys/for-me rotate: tenant default profile unusable (%s)", exc)
                    raise HTTPException(503, f"tenant default profile unusable: {exc}") from None
            # Spend lineage + budget LIMIT carry (the budget-reset bypass
            # fix): the fresh key inherits the old key's spend_ancestors
            # (+ the old id), so in-window budget spend survives rotation —
            # rotating mid-month can never reset the period budget. And the
            # old key's EXISTING budget/cost_budget rows copy over (their
            # limits may have drifted from the profile since minting; the
            # operator's per-key reality wins, same INSERT..SELECT + ON
            # CONFLICT DO NOTHING shape the admin rotate uses — profile
            # presets land first, per-key edits merge on top without
            # duplication). Both writes JOIN the transaction.
            await conn.fetchrow(
                "UPDATE api_keys SET spend_ancestors = $2 WHERE id = $1",
                key_id,
                list(existing["spend_ancestors"] or []) + [existing["id"]],
            )
            for btable, bcols in (
                ("budgets", "model, period, limit_tokens"),
                ("cost_budgets", "model, period, limit_usd"),
            ):
                await conn.fetchrow(
                    f"INSERT INTO {btable} (key_id, {bcols}) "
                    f"SELECT $1, {bcols} FROM {btable} WHERE key_id = $2 "
                    "ON CONFLICT (key_id, model, period) DO NOTHING",
                    key_id,
                    existing["id"],
                )
            await conn.execute(_FOR_ME_REVOKE_SQL, existing["id"])
    else:
        user = await _create_enroll_user(tenant_id, subject)
        async with db.acquire() as conn, conn.transaction():
            profile = await _sso_default_profile(conn, tenant_id)
            rpm, tpm = _profile_limits(profile, FOR_ME_RATE_LIMIT_RPM, FOR_ME_RATE_LIMIT_TPM)
            plaintext, key_id = await create_key(
                user["id"],
                f"sso-{subject[:40]}",
                FOR_ME_SCOPES,
                rate_limit_rpm=rpm,
                rate_limit_tpm=tpm,
                conn=conn,
            )
            # Bind to the caller's OWN subject — verified identity, provenance
            # 'sso-create'. JOINS the transaction now (the rotate branch's
            # posture): with tenant default-profile stamping inside the same
            # tx, a best-effort-outside-tx bind could commit a stamped key
            # that is anonymous — binding + stamping must commit or fail as
            # one. (The plaintext is still irreplaceable; a rollback here
            # fails the mint before it exists, which is the honest outcome.)
            bound = await bind_key_subject(
                key_id,
                subject=subject,
                idp=IDP_OAUTH2_PROXY,
                bound_via=VIA_SSO_CREATE,
                tenant_id=tenant_id,
                display_name=user["name"],
                groups=groups,
                conn=conn,
            )
            if profile is not None:
                await _bind_user_profile(conn, user["id"], profile)
                try:
                    await _stamp_profile_grants(conn, key_id, profile)
                except ProfilePresetConfigError as exc:
                    log.error("keys/for-me mint: tenant default profile unusable (%s)", exc)
                    raise HTTPException(503, f"tenant default profile unusable: {exc}") from None
        if bound is None:
            log.warning("keys/for-me: binding failed for subject %r — key issued unbound", subject)
    # AUDIT (sso key creation): a verified-identity mint or rotate,
    # attributed to the caller's OWN subject — never an admin action.
    await _enroll_audit(
        request,
        action="enroll.redeem",
        resource_type="key",
        resource_id=str(key_id),
        actor_source="proxy-identity",
        actor_subject=subject,
        details={"via": "sso-create", "bound_via": VIA_SSO_CREATE, "rotate": rotate},
    )
    return ForMeKeyCreated(key=plaintext, key_prefix=plaintext[:10], subject=subject, idp=IDP_OAUTH2_PROXY)


# ---------------------------------------------------------------------------
# 2. admin enrollment management
# ---------------------------------------------------------------------------


class EnrollmentCreate(BaseModel):
    label: str = Field(min_length=1, max_length=200)
    tenant_id: uuid.UUID
    profile_id: uuid.UUID | None = None
    max_uses: int = Field(default=0, ge=0)  # 0 = unlimited
    expires_days: int | None = Field(default=None, ge=1)


@admin_router.post("/enrollments", status_code=201)
async def create_enrollment(body: EnrollmentCreate, request: Request, _: AdminDep) -> dict[str, Any]:
    """Mint an enrollment code bound to tenant (+ optional profile).

    The plaintext code is in THIS response only — the row stores its
    SHA-256 hash, so no admin endpoint (and no database leak) can ever
    recover it. Reissue = mint a new code.
    """
    if not await _exists("tenants", body.tenant_id):
        raise HTTPException(404, f"tenant {body.tenant_id} not found")
    if body.profile_id is not None and not await _exists("profiles", body.profile_id):
        raise HTTPException(404, f"profile {body.profile_id} not found")
    code = generate_code()
    expires_at = datetime.now(UTC) + timedelta(days=body.expires_days) if body.expires_days else None
    row = await db.fetchrow(
        "INSERT INTO enrollments (code_hash, label, tenant_id, profile_id, max_uses, expires_at) "
        "VALUES ($1, $2, $3, $4, $5, $6) "
        "RETURNING id, label, tenant_id, profile_id, max_uses, uses, expires_at, revoked_at, created_at",
        hash_key(code),
        body.label,
        body.tenant_id,
        body.profile_id,
        body.max_uses,
        expires_at,
    )
    assert row is not None
    out = dict(row)
    out["code"] = code
    # AUDIT (enroll.code.mint): the CODE/hash never rides the trail — the
    # label + bounds are the metadata a compliance review needs.
    await _enroll_audit(
        request,
        action="enroll.code.mint",
        resource_type="enrollment",
        resource_id=str(row["id"]),
        actor_source="admin-key",
        details={
            "label": body.label,
            "tenant_id": str(body.tenant_id),
            "profile_id": str(body.profile_id) if body.profile_id else None,
            "max_uses": body.max_uses,
            "expires_days": body.expires_days,
        },
    )
    return out


@admin_router.get("/enrollments")
async def list_enrollments(_: AdminDep) -> list[dict[str, Any]]:
    """All enrollment codes with usage state. code_hash is deliberately
    absent from the projection — the hash never needs to leave the DB."""
    rows = await db.fetch(
        "SELECT e.id, e.label, t.name AS tenant_name, p.name AS profile_name, "
        "e.max_uses, e.uses, e.expires_at, e.revoked_at, e.created_at "
        "FROM enrollments e "
        "JOIN tenants t ON t.id = e.tenant_id "
        "LEFT JOIN profiles p ON p.id = e.profile_id "
        "ORDER BY e.created_at DESC"
    )
    return [dict(r) for r in rows]


@admin_router.post("/enrollments/{enrollment_id}/revoke")
async def revoke_enrollment(enrollment_id: uuid.UUID, request: Request, _: AdminDep) -> dict[str, Any]:
    """Kill a code. Idempotent: an already-revoked code reports revoked
    again (the UPDATE simply matches nothing)."""
    if not await _exists("enrollments", enrollment_id):
        raise HTTPException(404, f"enrollment {enrollment_id} not found")
    await db.execute(
        "UPDATE enrollments SET revoked_at = now() WHERE id = $1 AND revoked_at IS NULL",
        enrollment_id,
    )
    await _enroll_audit(
        request,
        action="enroll.code.revoke",
        resource_type="enrollment",
        resource_id=str(enrollment_id),
        actor_source="admin-key",
    )
    return {"revoked": True}


# ---------------------------------------------------------------------------
# 3. per-key MCP credential overrides (admin management)
# ---------------------------------------------------------------------------


class McpCredsUpsert(BaseModel):
    server: str = Field(min_length=1, max_length=200)
    headers: dict[str, str]


def _validate_creds_headers(headers: dict[str, str]) -> dict[str, str]:
    """Bound + normalize the override headers before they become upstream
    request headers: RFC token-ish names (^[A-Za-z0-9-]+$), <=16 headers,
    values <=512 chars. Empty set is refused — clearing goes through
    DELETE, so an empty PUT is always a mistake, not a semantic.

    RESERVED NAMES: the gateway's attribution stamps (identity.py) —
    ``X-MCP-Caller`` on every keyed upstream call, plus
    ``X-MCP-Caller-Subject`` / ``X-MCP-Caller-Class`` whenever a real
    subject resolved (the SQLhandler relay-attribution companions). All
    three are set AFTER the last-wins override merge, so a stored value
    would be dead weight at best; accepting one would also advertise a
    spoofing surface the merge order no longer has (an unbound key must not
    inherit a subject a tenant once stored). Tenants get a 400 naming the
    reservation (the verified vector from review §1.4: the names are valid
    per the token-ish grammar and a last-wins merge would otherwise let
    them through)."""
    if not headers:
        raise HTTPException(400, "headers must not be empty (use DELETE to clear stored credentials)")
    if len(headers) > MAX_CRED_HEADERS:
        raise HTTPException(400, f"too many headers: {len(headers)} exceeds the maximum of {MAX_CRED_HEADERS}")
    reserved_lower = {reserved.lower() for reserved in RESERVED_HEADER_NAMES}
    clean: dict[str, str] = {}
    for name, value in headers.items():
        if not HEADER_NAME_RE.fullmatch(name):
            raise HTTPException(400, f"invalid header name {name!r}: must match {HEADER_NAME_RE.pattern}")
        if name.lower() in reserved_lower:
            raise HTTPException(
                400,
                f"header name {name} is reserved: the gateway stamps it "
                "(attribution-never-authorization) and tenant overrides cannot supply it",
            )
        if len(value) > MAX_CRED_VALUE_LEN:
            raise HTTPException(400, f"header {name!r} value exceeds {MAX_CRED_VALUE_LEN} characters")
        clean[name] = value
    return clean


@admin_router.put("/keys/{key_id}/mcp-creds")
async def put_mcp_creds(key_id: uuid.UUID, body: McpCredsUpsert, request: Request, _: AdminDep) -> dict[str, Any]:
    """Set (or replace) one key's header overrides for one MCP server.

    Stored Fernet-encrypted JSON (fail 503 when the master key is
    unconfigured — identical posture to endpoint-credential writes).
    Upsert on (key_id, server_name). Values are NEVER returned by any
    endpoint; only the header NAMES echo back.
    """
    if not await _exists("api_keys", key_id):
        raise HTTPException(404, f"key {key_id} not found")
    if not await db.fetchval("SELECT 1 FROM mcp_servers WHERE name = $1", body.server):
        raise HTTPException(404, f"MCP server {body.server!r} not found")
    clean = _validate_creds_headers(body.headers)
    if not secretbox.enabled():
        raise HTTPException(
            503,
            "credential storage disabled: PCAI_LLM_SECRETS_MASTER_KEY is not set",
        )
    headers_enc = secretbox.encrypt(json.dumps(clean))
    await db.execute(
        "INSERT INTO mcp_server_creds (key_id, server_name, headers_enc) VALUES ($1, $2, $3) "
        "ON CONFLICT (key_id, server_name) DO UPDATE SET headers_enc = $3, updated_at = now()",
        key_id,
        body.server,
        headers_enc,
    )
    # AUDIT (mcp.creds.set): THE redaction case. The credential VALUES
    # (X-Dataset-Password et al.) are Fernet-encrypted into
    # mcp_server_creds; the trail records ONLY the header NAMES + count —
    # details_redact (applied at the single writer) would redact them, but
    # the payload here never contains values in the first place (defense
    # in depth: the redaction is the safety net, not the plan).
    await _enroll_audit(
        request,
        action="mcp.creds.set",
        resource_type="key",
        resource_id=str(key_id),
        actor_source="admin-key",
        details={"server": body.server, "header_names": sorted(clean), "header_count": len(clean)},
    )
    return {"key_id": str(key_id), "server": body.server, "header_names": sorted(clean)}


@admin_router.get("/keys/{key_id}/mcp-creds")
async def list_mcp_creds(key_id: uuid.UUID, _: AdminDep) -> list[dict[str, Any]]:
    """Which servers this key carries overrides for, and which header NAMES
    — never the values (write-only credentials, same discipline as the
    panel's endpoint tokens). Undecryptable rows (rotated master key) list
    with empty names rather than failing the listing."""
    if not await _exists("api_keys", key_id):
        raise HTTPException(404, f"key {key_id} not found")
    rows = await db.fetch(
        "SELECT server_name, headers_enc FROM mcp_server_creds WHERE key_id = $1 ORDER BY server_name",
        key_id,
    )
    out: list[dict[str, Any]] = []
    for r in rows:
        names: list[str] = []
        raw = secretbox.decrypt(r["headers_enc"])
        if raw:
            try:
                parsed = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                parsed = None
            if isinstance(parsed, dict):
                names = sorted(str(k) for k in parsed)
        out.append({"server": r["server_name"], "header_names": names})
    return out


@admin_router.delete("/keys/{key_id}/mcp-creds/{server}")
async def delete_mcp_creds(key_id: uuid.UUID, server: str, request: Request, _: AdminDep) -> dict[str, Any]:
    """Clear one key's overrides for one server."""
    if not await _exists("api_keys", key_id):
        raise HTTPException(404, f"key {key_id} not found")
    tag = await db.execute(
        "DELETE FROM mcp_server_creds WHERE key_id = $1 AND server_name = $2",
        key_id,
        server,
    )
    if tag == "DELETE 0":
        raise HTTPException(404, f"no stored MCP credentials for server {server!r} on key {key_id}")
    await _enroll_audit(
        request,
        action="mcp.creds.delete",
        resource_type="key",
        resource_id=str(key_id),
        actor_source="admin-key",
        details={"server": server},
    )
    return {"deleted": True, "server": server}
