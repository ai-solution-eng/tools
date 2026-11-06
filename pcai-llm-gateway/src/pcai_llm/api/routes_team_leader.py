"""Team leader console API: a delegated admin surface scoped to ONE enrollment.

An enrollment code is a team invite (routes_enroll). Each enrollment can carry
a TEAM LEADER KEY — a control-plane credential, hash-only, the analog of an
admin key but scoped to exactly one enrollment. Its holder gets this router: a
mini admin console over the keys minted from that enrollment, that
enrollment's usage, and that enrollment's traces. The advertised-models page
is shown in full (presented_models_payload — no key-ACL filtering to explain).

SCOPING DISCIPLINE (the security property of the whole surface): every id a
caller names is resolved with "WHERE id = $1 AND enrollment_id = $2" — never
fetch-then-compare. An id that exists but belongs to ANOTHER enrollment is a
404, exactly like an id that does not exist at all: no existence oracle, no
403 that confirms "there is a key with this id somewhere".

BUDGET ENVELOPE: the enrollment carries a team-wide cap (tokens and/or USD per
budget_period). Each teammate key carries its own ALLOCATION (budgets /
cost_budgets rows — the same per-key budgets the admin console writes). The
allocation writes here ENFORCE the envelope: same-period allocations across
the enrollment plus the new one must fit under the cap (409 otherwise, with
the numbers named). With overflow_enabled (default) a key may exceed its own
allocation while the envelope has headroom — the unallocated remainder is the
shared pool; with it off, core.budgets hard-caps each key at its allocation.
The gate side (core.budgets + core.teams) is what actually refuses spend; this
surface is where the promises get written.

AUDIT: every mutation rides routes_admin._audit with the team.* actions and
actor_source='team-leader', actor_subject=the enrollment LABEL (never key
material — the leader key's hash lives only in enrollments).

AUTH: the two credential envelopes api/auth.py serves — "Authorization:
Bearer <leader key>" and "X-API-Key: <leader key>" — with the same
precedence (an explicit X-API-Key outranks a co-forwarded proxy Bearer, and a
dead explicit key is a hard 401, never laundered through the other envelope).
Behind the SSO chain the browser often cannot set Authorization at all
(oauth2-proxy injects its own Bearer), which is why the second envelope
exists. core.teams.authenticate_leader is the only credential check; it fails
closed on every miss.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ..core import teams, traces
from ..core.budgets import (
    _COSTED_SQL,
    MAX_LIMIT_TOKENS,
    MAX_LIMIT_USD,
    _budget_token_value,
    budget_status,
    budget_status_many,
)
from ..core.identity import IDP_ADMIN_ASSERTED, VIA_ENROLLMENT, bind_key_subject, normalize_subject
from ..core.keys import create_key, list_keys, revoke_key
from ..db import db
from ..routing.router import router as model_router
from .routes_admin import (
    _MAX_WINDOW_DAYS,
    _NAME_PATTERN,
    TRACE_LIMIT_CAP,
    USAGE_GROUP_BYS,
    VALID_PERIODS,
    BudgetSpec,
    BudgetsReplace,
    _audit,
    _bounded_window,
    _check_list_len,
    _csv_response,
    key_usage,
    presented_models_payload,
)
from .routes_enroll import (
    ENROLL_SCOPES,
    _create_enroll_user,
    _read_profile_seed_tx,
    _seed_profile_defaults,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/team_leader", tags=["team-leader"])


# ---------------------------------------------------------------------------
# auth dependency
# ---------------------------------------------------------------------------


@dataclass
class TeamContext:
    """The authenticated team-leader request: the enrollment the key governs
    (core.teams.TeamEnrollment — the envelope the gate enforces) plus the
    leader key's PREFIX for display (prefixes are the console's key
    identifier; the plaintext never round-trips)."""

    enrollment: teams.TeamEnrollment
    key_prefix: str


def _rfc3339(value: Any) -> Any:
    """RFC3339 text for a datetime, or the value unchanged when it is not one.

    asyncpg hands back tz-aware datetimes while a test fake may hand back a
    str; hasattr() does not narrow for the type checker, so the attribute
    lookup is explicit.
    """
    iso = getattr(value, "isoformat", None)
    return iso() if callable(iso) else value


def _leader_credential(request: Request) -> str:
    """The presented leader key from EITHER envelope (api/auth.py's pair).

    Precedence mirrors resolve_bearer_key (fleet convention D19,
    de-escalation-only): an explicit X-API-Key IS the credential when
    present — it outranks the co-forwarded proxy Bearer, and it failing is a
    hard 401 (never a fall-through: two envelopes with one dead key is a
    refusal, fail-closed). No X-API-Key: the raw bearer, exactly as before
    the envelope existed.
    """
    envelope = request.headers.get("x-api-key", "").strip()
    if envelope:
        return envelope
    auth = request.headers.get("authorization", "")
    return auth[7:].strip() if auth.lower().startswith("bearer ") else ""


async def require_team_leader(request: Request) -> TeamContext:
    """Dependency: resolve the presented credential to its enrollment.

    Fail closed: no credential and a bad credential answer the SAME 401
    (the console shows one login error either way — and a missing/bad
    distinction would be a free oracle for key probing).
    core.teams.authenticate_leader never raises on a miss; DB errors
    propagate (infrastructure failure, never a silent open door).
    """
    plaintext = _leader_credential(request)
    if not plaintext:
        raise HTTPException(401, "invalid team leader key")
    enrollment = await teams.authenticate_leader(plaintext)
    if enrollment is None:
        raise HTTPException(401, "invalid team leader key")
    # Leader-key health (admin visibility): stamp the use so the admin
    # console can show how stale a leader key is. Best-effort TWICE over —
    # core already swallows and self-throttles (min_interval_s), and this
    # belt-and-braces catch keeps a telemetry write from ever failing an
    # authenticated request (a fake seam must not break the console).
    try:
        await teams.stamp_leader_used(enrollment.id)
    except Exception:
        log.warning("team leader usage stamp failed for %s", enrollment.id, exc_info=True)
    # The display prefix is read directly (authenticate_leader returns the
    # envelope, not the credential metadata): one PK-indexed SELECT.
    row = await db.fetchrow(
        "SELECT team_leader_key_prefix FROM enrollments WHERE id = $1",
        enrollment.id,
    )
    prefix = row.get("team_leader_key_prefix", "") if row is not None else ""
    return TeamContext(enrollment=enrollment, key_prefix=str(prefix or ""))


TeamDep = Annotated[TeamContext, Depends(require_team_leader)]


# ---------------------------------------------------------------------------
# scoping + envelope helpers
# ---------------------------------------------------------------------------


async def _scoped_key_or_404(key_id: uuid.UUID, enrollment_id: uuid.UUID) -> None:
    """Existence WITHIN the enrollment, in the WHERE clause (never
    fetch-then-compare): a key from another enrollment is indistinguishable
    from a missing one — 404, no oracle."""
    found = await db.fetchval(
        "SELECT 1 FROM api_keys WHERE id = $1 AND enrollment_id = $2",
        key_id,
        enrollment_id,
    )
    if found is None:
        raise HTTPException(404, f"key {key_id} not found")


async def _check_envelope(
    enrollment: teams.TeamEnrollment,
    requested: list[BudgetSpec],
    *,
    key_id: uuid.UUID | None = None,
) -> None:
    """THE ENVELOPE CHECK (wave 2, SPEND-AWARE): actual spend consumes the
    team pool exactly like a seat's allocation does, so the question is
    never "do the promises fit" but "is there pool LEFT":

        committed_i = max(0, alloc_i - spent_i)   # what key i can still use
        available   = T - S - SUM(committed_i for i != this key)

    where T is the enrollment cap for its period and S the team's spend in
    that window. The math lives in core.teams (team_available) — the
    console's pool display and this check call the SAME helper, so they
    can never disagree. A later seat therefore gets what earlier seats
    did not spend: 12 seats x 1M with 9M already spent leaves 3M for the
    next three seats and 0 for the rest.

    - Only rows whose period equals the enrollment's budget_period count:
      the cap is defined FOR that period; a row in another period is
      advisory (no cap exists to violate).
    - available None (= no cap of that kind) -> no check: allocations are
      advisory (the per-key gate still enforces the allocation itself).
    - key_id (the replace-all path) is EXCLUDED from the committed sum by
      the helper — replace-all removes this key's own commitment, so it
      must not count against its new request.
    - The refusal names ALL FOUR pool numbers (cap, spent, committed to
      other keys, available) plus the request: a leader staring at a 0
      has to see WHY it is 0. The message numbers are read from the same
      helpers (team_spend + team_allocation_state) — only SUMMED here,
      never re-derived.
    """
    period = enrollment.budget_period
    new_tokens = sum(int(b.limit_tokens) for b in requested if b.period == period and b.limit_tokens is not None)
    new_usd = sum(float(b.limit_usd) for b in requested if b.period == period and b.limit_usd is not None)
    if new_tokens == 0 and new_usd == 0:
        return
    available_tokens, available_usd = await teams.team_available(
        enrollment.id,
        period,
        exclude_key_id=key_id,
    )
    if (
        enrollment.budget_tokens is not None
        and new_tokens > 0
        and available_tokens is not None
        and new_tokens > available_tokens
    ):
        raise HTTPException(
            409,
            await _envelope_refusal_detail(
                enrollment,
                kind="token",
                cap=enrollment.budget_tokens,
                available=available_tokens,
                requested=new_tokens,
                exclude_key_id=key_id,
            ),
        )
    if enrollment.budget_usd is not None and new_usd > 0 and available_usd is not None and new_usd > available_usd:
        raise HTTPException(
            409,
            await _envelope_refusal_detail(
                enrollment,
                kind="USD",
                cap=enrollment.budget_usd,
                available=available_usd,
                requested=new_usd,
                exclude_key_id=key_id,
            ),
        )


async def _envelope_refusal_detail(
    enrollment: teams.TeamEnrollment,
    *,
    kind: str,
    cap: float,
    available: float,
    requested: float,
    exclude_key_id: uuid.UUID | None,
) -> str:
    """The 409 message: cap, spent, committed-to-OTHERS, available,
    requested — all four pool numbers from the core helpers. Called only
    on the refusal path (the happy path never pays for it).
    """
    period = enrollment.budget_period
    spent_tokens, spent_usd = await teams.team_spend(enrollment.id, period)
    state = await teams.team_allocation_state(enrollment.id, period)
    # Annotated: both branches feed the same f-string, and the token branch
    # yields int while the USD branch yields float (mypy otherwise pins the
    # inferred type from the first assignment).
    committed_others: float
    spent: float
    if kind == "token":
        committed_others = sum(int(a.committed_tokens) for kid, a in state.items() if kid != exclude_key_id)
        spent = int(spent_tokens)
    else:
        committed_others = sum(float(a.committed_usd) for kid, a in state.items() if kid != exclude_key_id)
        spent = float(spent_usd)
    return (
        f"team {period} {kind} budget: cap {cap}, spent {spent}, "
        f"committed to other keys {committed_others}, available {available}, requested {requested}"
    )


# ---------------------------------------------------------------------------
# request / response models
# ---------------------------------------------------------------------------


# THE NAME CHARSET (audit W6 #6, defence in depth) — ONE definition, imported
# from routes_admin (which also closes the admin twin, and routes_enroll's user
# name): a security regex that exists twice drifts eventually. A key name is a
# DISPLAY value: the team console, the admin traces explorer and the CSV exports
# all render it. The sinks escape (team_leader.html / admin.html), so this closes
# the STORED value at the source: a delegated (non-admin) leader must not be able
# to persist a tag or a control character that a future sink forgets to escape.
# CONSERVATIVE on purpose — spaces, unicode and . - _ / : ( ) all pass; only HTML
# metacharacters (< > " ' &) and control characters are refused, as a clean 422
# at the edge. (pydantic-core compiles pattern= with Rust's regex engine: no
# \A/\Z there, and its "$" is strict end-of-text, so a trailing newline is
# refused too — pinned in tests/test_team_leader_api.py.)


class TeamKeyMint(BaseModel):
    """POST /team_leader/keys body. Deliberately NARROWER than the admin
    KeyCreate: no scopes (fixed ENROLL_SCOPES), no profile choice (the
    enrollment's own profile seeds the key), no user_id (a per-teammate user
    row is created in the enrollment's tenant), no mcp_admin (the P0-3
    control — a delegated console must never mint credential-minting keys)."""

    name: str = Field(min_length=1, max_length=200, pattern=_NAME_PATTERN)
    # OPTIONAL identity binding: the leader ASSERTS the subject ("who this
    # key is for") — admin-asserted provenance (an authenticated console
    # moment), bound_via="enrollment" (the key is minted FROM this
    # enrollment). Blank after normalization is a 400, the admin rule.
    subject: str | None = Field(default=None, max_length=200)
    rate_limit_rpm: int = Field(60, gt=0)
    rate_limit_tpm: int = Field(100_000, gt=0)
    # le: a huge value overflows timedelta and 500s inside the handler
    # (expires_days=999999999 -> OverflowError). 100 years is past any
    # deployment's horizon and keeps the failure a 422 at the edge.
    expires_days: int | None = Field(default=None, ge=1, le=36500)
    # OPTIONAL allocation out of the team envelope (enforced by the envelope
    # check when the enrollment carries a cap of that kind).
    budget_tokens: int | None = Field(default=None, gt=0, le=MAX_LIMIT_TOKENS)
    budget_usd: float | None = Field(default=None, gt=0, allow_inf_nan=False, le=MAX_LIMIT_USD)


class TeamKeyPatch(BaseModel):
    """Subset of the admin KeyPatch — the fields a team leader may edit on
    their own keys. Identity material and scope/ACL fields are absent (not
    merely ignored): a delegated console never edits what it cannot see."""

    name: str | None = Field(None, min_length=1, max_length=200, pattern=_NAME_PATTERN)
    rate_limit_rpm: int | None = Field(None, gt=0)
    rate_limit_tpm: int | None = Field(None, gt=0)
    max_concurrent: int | None = Field(None, ge=0)
    # Explicit null (in the JSON body) clears the expiry; omitted = untouched
    # (distinguished via model_fields_set, the admin KeyPatch convention).
    expires_at: datetime | None = None


class TeamSettingsPatch(BaseModel):
    """PATCH /team_leader/settings body — EXACTLY one field.

    extra='forbid' (deliberate, unlike the admin patch's
    ignore-unknowns): a leader is a DELEGATED actor, and a typo'd field
    ({'overflow': true}) must be a loud 422, never a silent no-op that
    reads as success. The one field is the overflow SWITCH only: the
    team CAP stays admin-only — a leader must never raise their own
    total, so no cap field exists on this model to accept."""

    model_config = {"extra": "forbid"}

    overflow_enabled: bool


# ---------------------------------------------------------------------------
# discovery + overview
# ---------------------------------------------------------------------------


@router.get("/help", include_in_schema=False)
async def team_leader_index(_: TeamDep) -> dict[str, Any]:
    """JSON discovery index (the /admin/help shape, team-scoped)."""
    return {
        "service": "PCAI LLM Gateway team leader API",
        "scope": "this console sees ONLY the enrollment its key was minted from",
        "auth": "Authorization: Bearer <team leader key> (or X-API-Key)",
        "endpoints": [
            "GET    /team_leader/overview",
            "PATCH  /team_leader/settings",
            "GET    /team_leader/keys",
            "POST   /team_leader/keys",
            "PATCH  /team_leader/keys/{key_id}",
            "DELETE /team_leader/keys/{key_id}",
            "PUT    /team_leader/keys/{key_id}/budgets",
            "GET    /team_leader/keys/{key_id}/usage",
            "GET    /team_leader/usage",
            "GET    /team_leader/usage/summary",
            "GET    /team_leader/traces",
            "GET    /team_leader/traces/{request_id}",
            "GET    /team_leader/presented-models",
        ],
    }


@router.get("/overview")
async def overview(ctx: TeamDep) -> dict[str, Any]:
    """The console's landing card: the enrollment, its envelope, the team's
    spend THIS period (the gate's own math via core.teams.team_spend), the
    allocations promised to keys, the unallocated remainder (the shared
    overflow pool), key counts, and the leader key's display metadata."""
    e = ctx.enrollment
    names = await db.fetchrow(
        "SELECT t.name AS tenant_name, p.name AS profile_name "
        "FROM enrollments e "
        "JOIN tenants t ON t.id = e.tenant_id "
        "LEFT JOIN profiles p ON p.id = e.profile_id "
        "WHERE e.id = $1",
        e.id,
    )
    counts = await db.fetchrow(
        "SELECT count(*) AS total, count(*) FILTER (WHERE revoked_at IS NULL) AS active "
        "FROM api_keys WHERE enrollment_id = $1",
        e.id,
    )
    leader = await db.fetchrow(
        "SELECT team_leader_key_prefix, team_leader_key_created_at FROM enrollments WHERE id = $1",
        e.id,
    )
    spend_tokens, spend_usd = await teams.team_spend(e.id, e.budget_period)
    alloc_tokens, alloc_usd = await teams.team_allocated(e.id, e.budget_period)
    # the wave-2 pool numbers: committed sums the per-key remainder
    # (max(0, alloc - spent)) over ALL keys; available is the helper's own
    # answer (None = no cap of that kind). DO NOT "optimize" the second
    # statement away by inverting team_available (cap - spend - available):
    # the helper CLAMPS at 0, so the inversion reports a wrong committed
    # exactly when the pool is over-committed — the state an operator is
    # most likely staring at. See core.teams.team_available's docstring.
    state = await teams.team_allocation_state(e.id, e.budget_period)
    committed_tokens = sum(int(a.committed_tokens) for a in state.values())
    committed_usd = sum(float(a.committed_usd) for a in state.values())
    available_tokens, available_usd = await teams.team_available(e.id, e.budget_period)
    # PER-MODEL caps (2026-10-30): a derived envelope is not one pooled number
    # — every profile preset x max_uses is its own team pool, and the gate
    # enforces exactly these. Each cap carries its own spend so the console
    # can draw one filled bar per limit. '*' scopes pool across models (the
    # any-model preset); model scopes are that model's spend alone.
    caps_payload: list[dict[str, Any]] = []
    if e.budget_derived and e.model_caps:
        cap_spends = await teams.team_cap_spend(e.id, [(c.model, c.period) for c in e.model_caps])
        for c in e.model_caps:
            mtok, musd = cap_spends.get((c.model, c.period), (0, 0.0))
            caps_payload.append(
                {
                    "model": c.model,
                    "period": c.period,
                    "limit_tokens": c.limit_tokens,
                    "limit_usd": c.limit_usd,
                    "spent_tokens": int(mtok),
                    "spent_usd": float(musd),
                }
            )
    created_at = leader.get("team_leader_key_created_at") if leader is not None else None
    return {
        "enrollment": {
            "id": str(e.id),
            "label": e.label,
            "tenant_name": names["tenant_name"] if names is not None else "",
            "profile_name": names.get("profile_name") if names is not None else None,
        },
        "budget": {
            "period": e.budget_period,
            "tokens": e.budget_tokens,
            "usd": e.budget_usd,
            "overflow_enabled": e.overflow_enabled,
            # DERIVED (2026-10-30): True when the caps above were NOT set on
            # the row but computed as max_uses x the profile's per-key presets.
            # The console says so — an operator must be able to tell "I capped
            # this" from "the profile did", and the gate enforces either.
            "derived": e.budget_derived,
            "max_uses": e.max_uses,
        },
        "spend": {"tokens": spend_tokens, "usd": spend_usd},
        "allocated": {"tokens": alloc_tokens, "usd": alloc_usd},
        # cap - allocated: what NO key has been promised yet (the overflow
        # pool). Plain subtraction — an over-committed envelope shows negative,
        # which is the honest number for the console to flag.
        "unallocated": {
            "tokens": (e.budget_tokens - alloc_tokens) if e.budget_tokens is not None else None,
            "usd": (e.budget_usd - alloc_usd) if e.budget_usd is not None else None,
        },
        # Wave-2 pool numbers (spend-aware envelope): committed = what the
        # keys can STILL consume under their own allocations
        # (SUM max(0, alloc_i - spent_i)), available = what a NEW seat could
        # be granted right now (cap - spend - committed). Both come from the
        # core helpers the envelope check uses — the card and the check
        # cannot disagree. available None = no cap of that kind.
        "committed": {"tokens": committed_tokens, "usd": committed_usd},
        "available": {"tokens": available_tokens, "usd": available_usd},
        # One entry per derived per-model cap (empty for explicit envelopes —
        # an operator-typed pooled cap has exactly the two bars above).
        "caps": caps_payload,
        "keys": {
            # "total" is the number of keys that EXIST; "max_uses" is how many
            # the enrollment may EVER mint (0 = unlimited). The console renders
            # "active / max_uses" — the old "active / total" read as 2/2 on an
            # enrollment that could mint four, which is what made the ceiling
            # look like a coincidence (2026-10-30).
            "total": int(counts["total"]) if counts is not None else 0,
            "active": int(counts["active"]) if counts is not None else 0,
            "max_uses": e.max_uses,
        },
        "leader": {
            "key_prefix": ctx.key_prefix,
            # asyncpg returns tz-aware datetimes; isoformat() is the RFC3339
            # shape the SPA parses. Guard the TYPE, not just truthiness (the
            # usage_summary guard): a fake may hand back a str.
            "created_at": _rfc3339(created_at),
        },
    }


@router.patch("/settings")
async def patch_team_settings(body: TeamSettingsPatch, request: Request, ctx: TeamDep) -> dict[str, Any]:
    """The team leader toggles OVERFLOW for their own enrollment.

    The ONE delegated setting ('configurable by the team leader'): with
    overflow ON a teammate key may exceed its own allocation while the
    envelope has headroom (the unallocated remainder is the shared pool);
    OFF, core.budgets hard-caps every key at its own allocation. The CAP
    itself is deliberately NOT leader-editable (no cap field on the
    model, extra='forbid' on the body) — a leader must never raise their
    own total; that stays an admin action on /admin/enrollments/{id}.

    teams.invalidate() runs after the write: the gate reads
    overflow_enabled through its cached enrollment lookup, so the next
    request must not serve the old switch for up to a TTL."""
    await db.execute(
        "UPDATE enrollments SET overflow_enabled = $2 WHERE id = $1",
        ctx.enrollment.id,
        body.overflow_enabled,
    )
    teams.invalidate(ctx.enrollment.id)
    await _audit(
        request,
        action="team.settings.set",
        resource_type="enrollment",
        resource_id=str(ctx.enrollment.id),
        details={
            "enrollment_label": ctx.enrollment.label,
            "overflow_enabled": body.overflow_enabled,
        },
        actor_source="team-leader",
        actor_subject=ctx.enrollment.label,
    )
    return {"enrollment_id": str(ctx.enrollment.id), "overflow_enabled": body.overflow_enabled}


# ---------------------------------------------------------------------------
# budget top-up requests (THREAD B, closeout): the leader's ONE legitimate
# way to ask for more pool
# ---------------------------------------------------------------------------


class TeamTopupRequest(BaseModel):
    """POST /team_leader/keys body twin for the ASK: at least one of the
    two amounts, both column-bounded exactly like the allocation fields
    (a request the cap column could not hold would 500 the admin's
    approve — a mint-site contract of 400/422). The note is the leader's
    one free-text line of justification, bounded at 500."""

    tokens: int | None = Field(default=None, gt=0, le=MAX_LIMIT_TOKENS)
    usd: float | None = Field(default=None, gt=0, allow_inf_nan=False, le=MAX_LIMIT_USD)
    note: str | None = Field(default=None, max_length=500)


@router.post("/topup-requests", status_code=201)
async def create_topup_request(body: TeamTopupRequest, request: Request, ctx: TeamDep) -> dict[str, Any]:
    """Ask the admin for more team pool. Writes a PENDING row and nothing
    else — the leader has NO write access to the cap; the queue row is the
    whole surface of this endpoint. ONE open ask at a time (a second
    pending request is a 409): the queue would otherwise fill with
    duplicates of the same ask, and the admin could approve two of them
    without noticing they are the same request twice."""
    if body.tokens is None and body.usd is None:
        raise HTTPException(400, "a top-up request needs at least one of tokens / usd")
    pending = await db.fetchval(
        "SELECT 1 FROM enrollment_topup_requests WHERE enrollment_id = $1 AND status = 'pending'",
        ctx.enrollment.id,
    )
    if pending is not None:
        raise HTTPException(
            409,
            "this team already has a pending top-up request — wait for it to be approved or denied before asking again",
        )
    try:
        row = await db.fetchrow(
            "INSERT INTO enrollment_topup_requests (enrollment_id, requested_tokens, requested_usd, note) "
            "VALUES ($1, $2, $3, $4) "
            "RETURNING id, enrollment_id, requested_tokens, requested_usd, note, status, created_at, resolved_at",
            ctx.enrollment.id,
            body.tokens,
            body.usd,
            body.note or "",
        )
    except asyncpg.UniqueViolationError:
        # The uq_topup_one_pending partial index lost the race against a
        # concurrent POST (the pre-check above cannot see an uncommitted row).
        # Same 409 the pre-check raises — one contract for the caller, and the
        # invariant holds under concurrency instead of only in the happy path.
        raise HTTPException(
            409,
            "this team already has a pending top-up request — wait for it to be approved or denied before asking again",
        ) from None
    assert row is not None
    await _audit(
        request,
        action="team.topup.request",
        resource_type="topup",
        resource_id=str(row["id"]),
        details={
            "enrollment_id": str(ctx.enrollment.id),
            "enrollment_label": ctx.enrollment.label,
            "requested_tokens": body.tokens,
            "requested_usd": body.usd,
            "note": body.note or "",
        },
        actor_source="team-leader",
        actor_subject=ctx.enrollment.label,
    )
    return dict(row)


@router.get("/topup-requests")
async def list_topup_requests(ctx: TeamDep) -> list[dict[str, Any]]:
    """This enrollment's own asks, newest first, bounded at 50 (the
    console shows the open one and the recent history; older rows are the
    admin queue's concern). Scoped IN THE WHERE — another enrollment's
    requests never enter the query, let alone the response."""
    rows = await db.fetch(
        "SELECT id, enrollment_id, requested_tokens, requested_usd, note, status, created_at, resolved_at "
        "FROM enrollment_topup_requests WHERE enrollment_id = $1 "
        "ORDER BY created_at DESC, id DESC LIMIT 50",
        ctx.enrollment.id,
    )
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# team keys (scoped CRUD)
# ---------------------------------------------------------------------------


@router.get("/keys")
async def list_team_keys(ctx: TeamDep, include_revoked: bool = False) -> list[dict[str, Any]]:
    """The enrollment's keys, the /admin/keys row shape (+ budget_status).

    The scoping lives in core.keys.list_keys' enrollment_id filter (the
    same JOIN the admin list gained) — one SQL, no post-filter, so a key
    outside the enrollment never enters this payload at all."""
    rows = await list_keys(include_revoked=include_revoked, enrollment_id=ctx.enrollment.id)
    statuses = await budget_status_many([r["id"] for r in rows])
    for row in rows:
        row["budget_status"] = statuses.get(row["id"], [])
    return rows


@router.post("/keys", status_code=201)
async def mint_team_key(body: TeamKeyMint, request: Request, ctx: TeamDep) -> dict[str, Any]:
    """Mint a TEAMMATE key from this enrollment.

    One transaction (the redeem flow's shape): user row (enrollment's
    tenant) + key (enrollment_id + the enrollment profile's presets) +
    optional allocation + optional subject binding commit or roll back AS
    ONE — a half-seeded teammate key is worse than a failed mint.

    The envelope check runs INSIDE the transaction, AFTER the seat claim: the
    claim's row lock is what makes a concurrent mint see this mint's committed
    allocation, and a 409 still burns nothing (the raise unwinds the claim with
    the rest of the transaction). The plaintext is returned HERE exactly once."""
    e = ctx.enrollment
    # The profile seed is read HERE, before the envelope check, because the
    # mint STAMPS the profile's preset budget rows onto the new key below —
    # and those presets are allocations against the team pool exactly like the
    # body's amounts. Checking only the body let two mints promise 2x the cap
    # (a 1M preset, no explicit allocation, cap 1M): the documented invariant
    # "allocations across the enrollment plus the new one fit under the cap"
    # was simply false on this path. The read is a plain SELECT, so it costs
    # nothing to hoist; the seed is reused inside the transaction below.
    profile_seed = await _read_profile_seed_tx(e.profile_id, db) if e.profile_id is not None else None
    alloc_specs: list[BudgetSpec] = []
    if body.budget_tokens is not None:
        alloc_specs.append(
            BudgetSpec(model="*", period=e.budget_period, limit_tokens=body.budget_tokens, limit_usd=None)
        )
    if body.budget_usd is not None:
        alloc_specs.append(BudgetSpec(model="*", period=e.budget_period, limit_tokens=None, limit_usd=body.budget_usd))
    if profile_seed is not None:
        # Preset shape: [{"model","period","limit_tokens"|"limit_usd"}, ...]
        # (validated at save by core.budgets.split_preset_budgets). Rows for
        # OTHER periods are skipped by _check_envelope itself — they are
        # advisory, since the cap exists for one window.
        for preset in profile_seed[0] or []:
            if preset.get("limit_tokens") is not None:
                alloc_specs.append(
                    BudgetSpec(
                        model=preset.get("model") or "*",
                        period=preset.get("period") or "",
                        limit_tokens=int(preset["limit_tokens"]),
                        limit_usd=None,
                    )
                )
            elif preset.get("limit_usd") is not None:
                alloc_specs.append(
                    BudgetSpec(
                        model=preset.get("model") or "*",
                        period=preset.get("period") or "",
                        limit_tokens=None,
                        limit_usd=float(preset["limit_usd"]),
                    )
                )
    subject = normalize_subject(body.subject) if body.subject is not None else ""
    if body.subject is not None and not subject:
        raise HTTPException(400, "subject must not be blank when provided")
    expires_at = datetime.now(UTC) + timedelta(days=body.expires_days) if body.expires_days else None
    registered = set(model_router.models())
    async with db.acquire() as conn, conn.transaction():
        # THE MINT ALLOWANCE (audit W6 #2, 2026-11-02): an enrollment's ceiling is
        # ONE counter, shared with the redeem door — this is that door's own
        # conditional UPDATE (routes_enroll._REDEEM_SQL), so a delegated leader
        # and a public redeemer can never spend more than max_uses between them.
        # The UPDATE IS the enforcement: no row means every seat is taken, and
        # the raise rolls the transaction back having written nothing.
        claimed = await conn.execute(
            "UPDATE enrollments SET uses = uses + 1 WHERE id = $1 AND (uses < max_uses)",
            e.id,
        )
        if claimed != "UPDATE 1":
            raise HTTPException(
                409,
                f"enrollment {e.id} has spent its whole mint allowance "
                f"(max_uses={e.max_uses}) — an admin must raise it before another key can be minted",
            )
        # THE ENVELOPE CHECK RUNS HERE (audit W6 F4, 2026-11-02): the seat claim
        # above takes the enrollment row lock, so a concurrent mint serializes
        # behind this transaction and its check then sees THIS mint's committed
        # allocation. Before the move the check ran before the transaction
        # opened, and two mints could read the same headroom and both write
        # (over-committed promises; the spend gate still bound, so it was never
        # a money hole). A 409 here unwinds the seat claim with the rest of the
        # transaction — nothing is burned.
        if alloc_specs:
            await _check_envelope(e, alloc_specs, key_id=None)
        # profile_seed was read BEFORE the transaction (the envelope check needs
        # its presets); the stamp below uses the same parsed object, so the rows
        # that were accounted for are exactly the rows that get written.
        if profile_seed is not None:
            _budgets, _defaults, profile_order = profile_seed
            # the same ACL stamping the redeem door does: the profile's
            # model_order ∩ registered models IS the admin's signed-off set
            allowed_models = [m for m in profile_order if m in registered] if profile_order else []
        else:
            allowed_models = []
        user = await _create_enroll_user(e.tenant_id, body.name, profile_id=e.profile_id, conn=conn)
        # stream_pcai_events=True: teammates are the redeem-door population
        # (DSH parses the in-stream telemetry; consumer-inventory finding).
        plaintext, key_id = await create_key(
            user["id"],
            body.name,
            ENROLL_SCOPES,
            allowed_models or None,
            rate_limit_rpm=body.rate_limit_rpm,
            rate_limit_tpm=body.rate_limit_tpm,
            expires_at=expires_at,
            stream_pcai_events=True,
            conn=conn,
            enrollment_id=e.id,
        )
        if e.profile_id is not None and profile_seed is not None:
            await _seed_profile_defaults(e.profile_id, key_id, profile_seed, conn=conn)
        if body.budget_tokens is not None:
            await conn.execute(
                "INSERT INTO budgets (key_id, model, period, limit_tokens) VALUES ($1,$2,$3,$4)",
                key_id,
                "*",
                e.budget_period,
                body.budget_tokens,
            )
        if body.budget_usd is not None:
            await conn.execute(
                "INSERT INTO cost_budgets (key_id, model, period, limit_usd) VALUES ($1,$2,$3,$4)",
                key_id,
                "*",
                e.budget_period,
                body.budget_usd,
            )
        if subject:
            # JOINS the transaction (the admin create path's posture): a key
            # that exists is either bound or the mint rolled back. idp
            # admin-asserted: the TEAM LEADER asserted the subject (an
            # authenticated console moment), never the callee self-typed it.
            await bind_key_subject(
                key_id,
                subject=subject,
                idp=IDP_ADMIN_ASSERTED,
                bound_via=VIA_ENROLLMENT,
                tenant_id=e.tenant_id,
                display_name=body.name,
                conn=conn,
            )
    # the mint changed a key's enrollment_id (and added promised allocation
    # rows) — the gate's lookup cache must not serve a pre-mint world
    teams.invalidate()
    await _audit(
        request,
        action="team.key.create",
        resource_type="key",
        resource_id=str(key_id),
        details={
            "name": body.name,
            "enrollment_id": str(e.id),
            "enrollment_label": e.label,
            "subject": subject or None,
            "rate_limit_rpm": body.rate_limit_rpm,
            "rate_limit_tpm": body.rate_limit_tpm,
            "budget_tokens": body.budget_tokens,
            "budget_usd": body.budget_usd,
            "budget_period": e.budget_period if (body.budget_tokens or body.budget_usd) else None,
        },
        actor_source="team-leader",
        actor_subject=e.label,
    )
    return {
        "id": str(key_id),
        "key": plaintext,
        "key_prefix": plaintext[:10],
        "message": "copy this key now — it is shown once",
    }


@router.patch("/keys/{key_id}")
async def patch_team_key(key_id: uuid.UUID, body: TeamKeyPatch, request: Request, ctx: TeamDep) -> dict[str, Any]:
    """Edit a teammate key's name/limits/expiry IN PLACE (the admin patch's
    scoped subset). Keys are read per request by the gate, so edits are live
    immediately. Scoped UPDATE: a key outside the enrollment is a 404, and
    the WHERE carries the enrollment so a concurrent re-scope can never
    patch across teams."""
    await _scoped_key_or_404(key_id, ctx.enrollment.id)
    sets: list[str] = []
    args: list[Any] = []
    for col in ("name", "rate_limit_rpm", "rate_limit_tpm", "max_concurrent"):
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
        f"UPDATE api_keys SET {', '.join(sets)} "
        f"WHERE id = ${len(args) + 1} AND enrollment_id = ${len(args) + 2} "
        "RETURNING id, user_id, name, scopes, allowed_models, rate_limit_rpm, "
        "rate_limit_tpm, max_concurrent, expires_at, relay_default, stream_pcai_events, "
        "mcp_admin",
        *args,
        key_id,
        ctx.enrollment.id,
    )
    if row is None:
        # a concurrent revoke between the existence probe and the UPDATE —
        # the scoped write matched nothing; same honest 404
        raise HTTPException(404, f"key {key_id} not found")
    await _audit(
        request,
        action="team.key.patch",
        resource_type="key",
        resource_id=str(key_id),
        details={
            "enrollment_id": str(ctx.enrollment.id),
            **{field: getattr(body, field) for field in sorted(body.model_fields_set)},
        },
        actor_source="team-leader",
        actor_subject=ctx.enrollment.label,
    )
    return dict(row)


@router.delete("/keys/{key_id}")
async def revoke_team_key(key_id: uuid.UUID, request: Request, ctx: TeamDep) -> dict[str, Any]:
    """Revoke a teammate key. Idempotent (the admin contract): revoking an
    already-revoked key reports revoked again."""
    await _scoped_key_or_404(key_id, ctx.enrollment.id)
    revoked = await revoke_key(key_id)
    await _audit(
        request,
        action="team.key.revoke",
        resource_type="key",
        resource_id=str(key_id),
        details={"enrollment_id": str(ctx.enrollment.id), "revoked": revoked},
        actor_source="team-leader",
        actor_subject=ctx.enrollment.label,
    )
    return {"revoked": revoked}


@router.put("/keys/{key_id}/budgets")
async def replace_team_budgets(
    key_id: uuid.UUID, body: BudgetsReplace, request: Request, ctx: TeamDep
) -> dict[str, Any]:
    """Replace ALL allocations on a teammate key — the admin replace-all
    (declarative state, one transaction) PLUS the team envelope check.

    Order matters: body validation (400s, no DB), scoped existence (404,
    no oracle), THEN the envelope check (409, nothing written — the check
    reads the key's current rows only, it does not depend on the write),
    then the replace-all transaction. teams.invalidate() runs after the
    commit: the promised rows changed, the gate's lookups must re-read."""
    e = ctx.enrollment
    _check_list_len(body.budgets, "budgets")
    if not body.budgets:
        raise HTTPException(400, "budgets list must not be empty (DELETE not supported here)")
    await _scoped_key_or_404(key_id, e.id)
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
        # Column bounds (mirrors the admin route and core.budgets.MAX_LIMIT_*):
        # past these the INSERT fails inside asyncpg, i.e. a 500 for what is
        # really a bad request. NaN/Infinity are already refused at the model
        # (BudgetSpec.allow_inf_nan=False) — they would otherwise SKIP the
        # envelope check below, because every comparison with NaN is False.
        if has_tokens and b.limit_tokens is not None and b.limit_tokens > MAX_LIMIT_TOKENS:
            raise HTTPException(400, f"limit_tokens exceeds the column maximum {MAX_LIMIT_TOKENS}")
        if has_usd and b.limit_usd is not None and b.limit_usd > MAX_LIMIT_USD:
            raise HTTPException(400, f"limit_usd exceeds the column maximum {MAX_LIMIT_USD}")
    # THE ENVELOPE CHECK — before any write, with the key's own current
    # same-period rows excluded (replace-all removes them)
    await _check_envelope(e, body.budgets, key_id=key_id)
    token_rows = [(key_id, b.model, b.period, b.limit_tokens) for b in body.budgets if b.limit_tokens is not None]
    usd_rows = [(key_id, b.model, b.period, b.limit_usd) for b in body.budgets if b.limit_usd is not None]
    async with db.acquire() as conn, conn.transaction():
        # REPLACE-ALL across BOTH kinds (declarative state — the admin shape)
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
    teams.invalidate(e.id)
    await _audit(
        request,
        action="team.budget.set",
        resource_type="key",
        resource_id=str(key_id),
        # the SPEC, not the parsed rows — the trail records the request
        details={
            "enrollment_id": str(e.id),
            "enrollment_label": e.label,
            "budgets": [b.model_dump() for b in body.budgets],
        },
        actor_source="team-leader",
        actor_subject=e.label,
    )
    return {"key_id": str(key_id), "budgets": await budget_status(key_id)}


@router.get("/keys/{key_id}/usage")
async def team_key_usage(key_id: uuid.UUID, ctx: TeamDep) -> dict[str, Any]:
    """Per-key view: current allocations with spend + the 50 most recent
    ledger rows (the /admin/keys/{id}/usage shape). Scoped first: a foreign
    key is a 404 before any query the shared helper would run."""
    await _scoped_key_or_404(key_id, ctx.enrollment.id)
    return await key_usage(key_id, None)


# ---------------------------------------------------------------------------
# team usage + traces (scoped ledger)
# ---------------------------------------------------------------------------


@router.get("/usage")
async def team_usage(
    ctx: TeamDep,
    limit: int = 100,
    key_id: uuid.UUID | None = None,
    model: str | None = None,
    status: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    group_by: str | None = None,
    format: str = "json",
) -> Any:
    """Ledger explorer scoped to the enrollment's keys — the /admin/usage
    shape (same filters, group-by dimensions and CSV export) with the
    enrollment scope as the FIRST condition of every query:
    "l.key_id IN (SELECT id FROM api_keys WHERE enrollment_id = $1)". A
    key_id filter that names a foreign key is a 404 (scoped existence), not
    an empty list — filters are for narrowing WITHIN the team."""
    if group_by is not None and group_by not in USAGE_GROUP_BYS:
        raise HTTPException(400, f"group_by must be one of {sorted(USAGE_GROUP_BYS)}")
    if format not in ("json", "csv"):
        raise HTTPException(400, "format must be 'json' or 'csv'")
    if key_id is not None:
        await _scoped_key_or_404(key_id, ctx.enrollment.id)
    capped = max(1, min(limit, 1000))

    conds: list[str] = []
    args: list[Any] = [ctx.enrollment.id]
    # THE SCOPE — always the first predicate (see module docstring)
    conds.append("l.key_id IN (SELECT id FROM api_keys WHERE enrollment_id = $1)")
    if key_id is not None:
        args.append(key_id)
        conds.append(f"l.key_id = ${len(args)}")
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
    where = "WHERE " + " AND ".join(conds)

    if group_by is not None:
        # the admin explorer's dimensions, verbatim (same joins, same
        # projections — the team console renders the same columns)
        dims = {
            "key": "l.key_id, k.name AS key_name, k.key_prefix AS key_prefix",
            "model": "l.model",
            "tenant": "t.id AS tenant_id, t.name AS tenant_name",
            "day": "date_trunc('day', l.ts) AS day",
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


def _billable_tokens_sql(alias: str) -> str:
    """The BUDGET expression for token spend (2026-10-30 / audit W6 F2).

    The usage tables' "Billed tokens" column means "what period budgets
    consumed" — so it must use the gate's OWN expression: cached tokens at the
    configured exemption (0.9 = 10% weight) times the off-peak billing fraction.
    Imported rather than re-spelled, so the console, the team bar and the gate can
    never tell two different stories about one number.

    It must also carry the gate's OWN STATUS predicate: a cache_hit row is free
    upstream, so the gate exempts it (_COSTED_STATUSES) — counting it here
    inflated the team console's billed number above what the budget was charged.
    The status set is imported, never re-spelled: a duplicated money predicate
    drifts.
    """
    return f"SUM({_budget_token_value(alias)}) FILTER (WHERE {alias}.status IN ({_COSTED_SQL}))"


@router.get("/usage/summary")
async def team_usage_summary(ctx: TeamDep, since: str | None = None) -> dict[str, Any]:
    """Scoped rollup of the enrollment's ledger (the /admin/usage/summary
    shape: totals, by-model, by-key, window). Same bounded window (30d
    default, 366 max); the scope subselect keeps every aggregate to the
    enrollment's keys in ONE statement."""
    _since_dt, _until_dt = _bounded_window(since, default_days=30)
    args: list[Any] = [ctx.enrollment.id]
    conds = ["l.key_id IN (SELECT id FROM api_keys WHERE enrollment_id = $1)"]
    if _since_dt:
        args.append(_since_dt)
        conds.append(f"l.ts >= ${len(args)}::timestamptz")
        args.append(_until_dt)
        conds.append(f"l.ts <= ${len(args)}::timestamptz")
    where_l = " WHERE " + " AND ".join(conds)
    totals = await db.fetchrow(
        f"""
        SELECT
          count(*)                                                    AS requests,
          COALESCE(SUM(l.total_tokens), 0)::bigint                    AS total_tokens,
          COALESCE(SUM(l.prompt_tokens), 0)::bigint                   AS input_tokens,
          COALESCE(SUM(l.completion_tokens), 0)::bigint               AS output_tokens,
          COALESCE(SUM(l.cached_tokens), 0)::bigint                   AS cached_tokens,
          count(*) FILTER (WHERE l.status = 'cache_hit')              AS cache_hit_requests,
          CASE WHEN COALESCE(SUM(l.prompt_tokens) FILTER (WHERE l.status <> 'cache_hit'), 0) > 0
               THEN ROUND(100.0 * SUM(l.cached_tokens) FILTER (WHERE l.status <> 'cache_hit') / SUM(l.prompt_tokens) FILTER (WHERE l.status <> 'cache_hit'), 1)
               ELSE NULL END                                          AS cache_hit_rate_pct,
          COALESCE(SUM(l.cost_usd), 0)::float                         AS cost_usd,
          COALESCE({_billable_tokens_sql("l")}, 0)::float               AS billed_tokens,
          COALESCE(SUM(l.cost_usd * l.billing_fraction), 0)::float        AS billed_cost_usd
        FROM usage_ledger l{where_l}
        """,
        *args,
    )
    by_model = await db.fetch(
        f"""
        SELECT l.model,
               count(*)                                            AS requests,
               COALESCE(SUM(l.prompt_tokens), 0)::bigint           AS input_tokens,
               COALESCE(SUM(l.completion_tokens), 0)::bigint       AS output_tokens,
               COALESCE(SUM(l.cached_tokens), 0)::bigint           AS cached_tokens,
               count(*) FILTER (WHERE l.status = 'cache_hit')      AS cache_hits,
               CASE WHEN COALESCE(SUM(l.prompt_tokens) FILTER (WHERE l.status <> 'cache_hit'), 0) > 0
                    THEN ROUND(100.0 * SUM(l.cached_tokens) FILTER (WHERE l.status <> 'cache_hit') / SUM(l.prompt_tokens) FILTER (WHERE l.status <> 'cache_hit'), 1)
                    ELSE NULL END                                  AS cache_hit_rate_pct,
               COALESCE(SUM(l.cost_usd), 0)::float                 AS cost_usd,
               -- BILLED columns (2026-10-30): the team console renders the
               -- same chargeback table as /admin/usage — tokens/cost x the
               -- billing fraction actually in force (off-peak discounts), so
               -- a leader and an admin reading the same rows see one number.
               COALESCE(SUM(l.total_tokens * l.billing_fraction), 0)::float AS billed_tokens,
               COALESCE({_billable_tokens_sql("l")}, 0)::float             AS billable_tokens,
               COALESCE(SUM(l.cost_usd * l.billing_fraction), 0)::float     AS billed_cost_usd
        FROM usage_ledger l{where_l}
        GROUP BY l.model ORDER BY cost_usd DESC
        """,
        *args,
    )
    by_key = await db.fetch(
        f"""
        SELECT k.name AS key_name, u.name AS user_name,
               count(DISTINCT l.key_id)                            AS generations,
               count(*)                                            AS requests,
               COALESCE(SUM(l.prompt_tokens), 0)::bigint           AS input_tokens,
               COALESCE(SUM(l.completion_tokens), 0)::bigint       AS output_tokens,
               COALESCE(SUM(l.cached_tokens), 0)::bigint           AS cached_tokens,
               CASE WHEN COALESCE(SUM(l.prompt_tokens) FILTER (WHERE l.status <> 'cache_hit'), 0) > 0
                    THEN ROUND(100.0 * SUM(l.cached_tokens) FILTER (WHERE l.status <> 'cache_hit') / SUM(l.prompt_tokens) FILTER (WHERE l.status <> 'cache_hit'), 1)
                    ELSE NULL END                                  AS cache_hit_rate_pct,
               COALESCE(SUM(l.cost_usd), 0)::float                 AS cost_usd,
               COALESCE(SUM(l.total_tokens * l.billing_fraction), 0)::float AS billed_tokens,
               COALESCE({_billable_tokens_sql("l")}, 0)::float             AS billable_tokens,
               COALESCE(SUM(l.cost_usd * l.billing_fraction), 0)::float     AS billed_cost_usd
        FROM usage_ledger l
        LEFT JOIN api_keys k ON k.id = l.key_id
        LEFT JOIN users u ON u.id = k.user_id
        {where_l}
        GROUP BY k.name, u.name ORDER BY cost_usd DESC
        """,
        *args,
    )
    # RETAINED HISTORY, scoped to the team (the admin reports it globally).
    # The console labels its widest window from real retention instead of
    # claiming a data horizon the deployment may not have; a leader asking
    # "ever" gets the truth for THEIR rows, not the fleet's oldest row.
    oldest = await db.fetchval(
        "SELECT MIN(ts) FROM usage_ledger WHERE key_id IN (SELECT id FROM api_keys WHERE enrollment_id = $1)",
        ctx.enrollment.id,
    )
    return {
        "totals": dict(totals) if totals else {},
        "by_model": [dict(r) for r in by_model],
        "by_key": [dict(r) for r in by_key],
        "window": {
            "since": _since_dt.isoformat() if _since_dt else None,
            "until": _until_dt.isoformat() if _until_dt else None,
            "default_days": 30,
            "max_days": _MAX_WINDOW_DAYS,
            "oldest_ts": oldest.isoformat() if hasattr(oldest, "isoformat") else None,
        },
    }


@router.get("/traces")
async def team_traces(
    ctx: TeamDep,
    key_id: uuid.UUID | None = None,
    since: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Request-grouped traces of the enrollment's keys. The scoping rides
    INSIDE traces.recent (its enrollment_id condition is a key_id IN
    subselect — no post-filter, no leak)."""
    return await traces.recent(
        key_id=key_id,
        since=_bounded_window(since, default_days=30)[0],
        limit=max(1, min(limit, TRACE_LIMIT_CAP)),
        enrollment_id=ctx.enrollment.id,
    )


@router.get("/traces/{request_id}")
async def team_trace_detail(request_id: str, ctx: TeamDep) -> dict[str, Any]:
    """One request's trace timeline — 404 when the trace does not exist OR
    belongs to a key outside this enrollment (the id scope is the same
    no-oracle 404 as everywhere else on this surface)."""
    trace = await traces.get(request_id, enrollment_id=ctx.enrollment.id)
    if trace is None:
        raise HTTPException(404, f"no trace for request {request_id!r}")
    return trace


# ---------------------------------------------------------------------------
# advertised models (full payload)
# ---------------------------------------------------------------------------


@router.get("/presented-models")
async def team_presented_models(_: TeamDep) -> dict[str, Any]:
    """The FULL advertised payload (routes_admin.presented_models_payload) —
    deliberately NOT key-ACL-filtered: "what can this team's keys possibly
    reach" is the operator question this tab answers, and the honesty
    contract (registry truth + config truth) is defined on the admin view.
    Byte-identical to GET /admin/presented-models' body."""
    return await presented_models_payload()
