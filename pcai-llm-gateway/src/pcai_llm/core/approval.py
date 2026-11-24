"""Destructive-tool approval gate, phase 1: pre-authorization tokens.

Design: documentation/design/tool-approval-gate.md (§3.1 the gate, §4 the
fail-closed table).

THE HOLE THIS CLOSES. The gateway has never constructed an InputRequiredResult:
every tool call -- including ``applygate plan_apply``, ``workbench run`` and
``k8s exec_in_pod`` -- executes on a static grant. The ACL answers "may this key
call this tool"; nothing answers "should THIS call happen". This module is the
decision half of that answer: an operator (or an automated policy) issues an
approval TOKEN that authorizes a SHAPE of call -- one (server, tool), one
arguments fingerprint (or ANY arguments, the weaker shape), a MANDATORY use count
and a MANDATORY expiry. The gate in ``api/routes_mcp.py::_gated_mcp_call``
refuses a named destructive tool that has no matching live token, before the
upstream session opens.

PHASE 1 SHIPS OBSERVE-ONLY. ``settings.tool_approval_mode`` defaults to "off",
where the gate returns before any work: the off path is byte-identical to
pre-B8 and pays no database round trip. "warn" records what WOULD have needed
approval in ``tool_approval_requests`` and lets the call proceed untouched;
"enforce" is the real gate and is reachable only by an operator setting the
mode. Two weeks of "warn" is the data that decides whether the default tool
list is right (design §5).

THE CONTRACT (non-negotiable):
  * PURE DECISION. requires_approval(), args_fingerprint() and match() do no
    I/O, read no clock and mutate no settings: ``now`` is passed in, so every
    rejection reason is testable in isolation.
  * A FINGERPRINT MISMATCH NEVER MATCHES. A token issued for "delete ns=test"
    must not authorize "ns=prod": the fingerprint is compared for EQUALITY and
    a NULL fingerprint (any arguments) is the ONLY wildcard -- and that weaker
    shape is labelled wherever a token is shown.
  * FAIL CLOSED, ASYMMETRICALLY (design §4). A store fault in WARN mode never
    fails a call (the same best-effort posture as traces). A store fault in
    ENFORCE mode REFUSES: a read-only tool with a broken ACL is an outage, a
    destructive tool with a broken approval gate is an incident. Pinning makes
    the OPPOSITE trade (fail open) because a pinning bug must not break a turn;
    different blast radius, different default.
  * ONE STATEMENT PER DECISION on the hot path: off = zero, warn = one indexed
    upsert, enforce = one indexed select + (one conditional consume UPDATE, or
    the same upsert when refusing). See the SQL constants below.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ..config import settings
from ..db import db

log = logging.getLogger(__name__)

# Policy modes (the operator's decision, not this module's).
MODES = ("off", "warn", "enforce")
MODE_OFF, MODE_WARN, MODE_ENFORCE = MODES
# Fail-safe default: a typo must NOT start refusing destructive calls
# fleet-wide, so anything unrecognised lands on the inert setting. The coercion
# is logged once per distinct value (a per-call warning would be a flood).
DEFAULT_MODE = MODE_OFF
_UNKNOWN_MODE_LOGGED: set[str] = set()

# 16 hex chars = 64 bits, the pin digest's width (core.tool_pin.DIGEST_CHARS):
# consistent operator-facing digests, and forging a collision needs ~2^32
# crafted argument sets for a coin flip. This is a SCOPING device, not a
# signature -- the token row itself is the authority.
DIGEST_CHARS = 16

# The ``detail`` on the denied ledger/audit row for an approval refusal
# (design §4's table: "refuse (denied, detail=approval_required)"). One
# constant so the gate, the route and the tests cannot drift.
REFUSAL_DETAIL = "approval_required"


def resolve_mode(value: Any) -> str:
    """Normalise a configured mode; anything unrecognised becomes "off".

    Same discipline as every other mode knob in the gateway (tool_pin
    resolves unknown values to "warn", tool_injection to "annotate"): an
    unparsable value lands on the visible-but-inert setting instead of
    guessing. The direction here is the opposite of the other two ON PURPOSE --
    they are defences whose "off" is the dangerous typo, while this gate
    REFUSES calls in enforce mode, so a typo must never be able to start
    refusing destructive calls fleet-wide. An unset value (None/"") is the
    documented default, not a typo, so it is silent.
    """
    mode = str(value or "").strip().lower()
    if mode in MODES:
        return mode
    if mode and mode not in _UNKNOWN_MODE_LOGGED:
        _UNKNOWN_MODE_LOGGED.add(mode)
        log.warning("approval: unknown tool_approval_mode %r -- using %r", mode, DEFAULT_MODE)
    return DEFAULT_MODE


# ---------------------------------------------------------------------------
# Which tools require a token
# ---------------------------------------------------------------------------

# Parsed glob lists, keyed by the raw setting string. The setting is a config
# value, so this is one tiny entry per distinct value (normally exactly one) --
# not an unbounded cache of caller-controlled keys.
_PATTERN_CACHE: dict[str, tuple[str, ...]] = {}


def required_patterns(required: str | None = None) -> tuple[str, ...]:
    """The configured ``server.tool`` globs as a tuple (PURE, cached).

    ``required`` overrides the setting for tests and for a future per-tenant
    policy; None reads ``settings.tool_approval_required``. Whitespace around
    a pattern is stripped and empty entries are dropped, so a trailing comma
    is harmless.
    """
    raw = getattr(settings, "tool_approval_required", "") if required is None else required
    key = str(raw or "")
    cached = _PATTERN_CACHE.get(key)
    if cached is None:
        cached = tuple(part.strip() for part in key.split(",") if part.strip())
        _PATTERN_CACHE[key] = cached
    return cached


def requires_approval(server: str, tool: str, required: str | None = None) -> bool:
    """Does this (server, tool) need a token? PURE; default-deny for the NAMED
    tools only.

    Matching is fnmatch against the joined ``server.tool`` name, CASE-SENSITIVE
    (MCP tool names are case-sensitive; fnmatchcase makes that explicit and
    platform-independent). ``*`` spans dots, so "applygate.*apply*" covers
    "applygate.plan_apply" -- the fleet convention for these lists.
    Everything not named is unaffected, so enabling the gate cannot break the
    fleet's read paths (design §4's last row: unchanged path, zero cost).
    """
    name = f"{server}.{tool}"
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in required_patterns(required))


# ---------------------------------------------------------------------------
# The arguments fingerprint
# ---------------------------------------------------------------------------


def args_fingerprint(arguments: Any) -> str:
    """16-hex sha256 of the CANONICAL JSON arguments. PURE and TOTAL.

    Sorted keys at every depth and no insignificant whitespace, so dict key
    order (or a re-serialising client) cannot move the digest -- only a
    semantic argument change can. Types are preserved: 1 and "1" digest
    differently (json.dumps is type-aware), which is the property that makes
    "a token for ns=test must not authorize ns=prod" enforceable.

    None and {} digest IDENTICALLY: both mean "no arguments", and a token
    issued for a call with no arguments must authorize the same call whether
    the client sent {} or omitted the object.

    NEVER raises: the last-resort repr fallbacks keep the digest total (the
    same discipline as core.tool_pin._canonical and chain_audit.args_digest),
    because a fingerprint that can fail a tool call is a liability.
    """
    payload = {} if arguments is None else arguments
    try:
        blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    except Exception:  # broad-by-design: a digest must never fail a tool call
        try:
            blob = repr(payload)
        except Exception:  # a hostile __repr__ must not escape either
            blob = "<undigestable>"
    return hashlib.sha256(blob.encode("utf-8", "replace")).hexdigest()[:DIGEST_CHARS]


# ---------------------------------------------------------------------------
# The token and the pure match decision
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ApprovalToken:
    """ONE tool_approvals row, as the decision consumes it.

    ``key_id is None`` = any key, and ``args_fingerprint is None`` = any
    arguments: the two weaker shapes the design permits, both labelled where
    a token is shown so an operator can see what they granted.
    """

    id: uuid.UUID
    key_id: uuid.UUID | None
    server: str
    tool: str
    args_fingerprint: str | None
    max_uses: int
    uses_so_far: int
    expires_at: float
    issued_by: str = ""
    reason: str = ""
    revoked_at: float | None = None


def match(
    tokens: list[ApprovalToken],
    key_id: Any,
    server: str,
    tool: str,
    fingerprint: str,
    now: float,
) -> ApprovalToken | None:
    """The ONE token that authorizes this call, or None. PURE.

    A token matches only if ALL of:
      * unrevoked (revoked_at is None),
      * unexpired (expires_at > now -- strictly: a token expiring AT now is
        spent, the conservative reading),
      * not exhausted (uses_so_far < max_uses),
      * key scope matches (key_id is None = any key, else the caller's id),
      * the server and tool are the token's own (defence in depth: the
        store query already filters them), and
      * the fingerprint is NULL (any arguments, the weaker shape) or EQUAL.
        A MISMATCH NEVER MATCHES -- this is the rule that stops a token for
        "delete ns=test" from authorizing "delete ns=prod".

    When several match, the NARROWEST authority is spent first: an exact
    fingerprint beats a NULL-fingerprint wildcard, then the soonest expiry
    (spend the perishable token before the durable one), then id for a
    deterministic order. The route consumes exactly the token returned here.
    """
    caller = "" if key_id is None else str(key_id)
    live = [
        token
        for token in tokens
        if token.server == server
        and token.tool == tool
        and token.revoked_at is None
        and token.expires_at > now
        and token.uses_so_far < token.max_uses
        and (token.key_id is None or str(token.key_id) == caller)
        and (token.args_fingerprint is None or token.args_fingerprint == fingerprint)
    ]
    if not live:
        return None
    live.sort(key=lambda token: (token.args_fingerprint is None, token.expires_at, str(token.id)))
    return live[0]


# ---------------------------------------------------------------------------
# Persistence (the only I/O in this module)
# ---------------------------------------------------------------------------

# WARN mode's one statement: upsert the observation and hand back the STABLE
# request id (a repeat of the same call keeps the id an operator may already
# have read). The unique index on the conflict target is the upsert's lookup.
_REQUEST_UPSERT_SQL = """
INSERT INTO tool_approval_requests
    (key_id, server, tool, args_fingerprint, mode)
VALUES ($1, $2, $3, $4, $5)
ON CONFLICT (key_id, server, tool, args_fingerprint) DO UPDATE
    SET last_seen  = now(),
        times_seen = tool_approval_requests.times_seen + 1,
        mode       = EXCLUDED.mode
RETURNING request_id
"""

# ENFORCE mode's read. Revocation and EXPIRY are filtered by the DATABASE
# clock -- the authority for a time bound, so app/DB clock skew cannot revive
# an expired token. Everything shape-related (key scope, use count,
# fingerprint) is left to match(), which stays the single decision point.
_LIVE_TOKENS_SQL = """
SELECT id, key_id, server, tool, args_fingerprint, max_uses, uses_so_far,
       expires_at, issued_by, reason, revoked_at
  FROM tool_approvals
 WHERE server = $1 AND tool = $2 AND revoked_at IS NULL AND expires_at > now()
"""

# The CONSUME, and the reason it is an UPDATE and not a read-then-write:
# two concurrent calls must not both spend the last use of a one-use token.
# The WHERE clause re-checks every bound the pure decision checked, so a race
# (or a revocation that landed after the read) updates ZERO rows and the call
# is refused -- fail closed, never double-spend.
_CONSUME_SQL = """
UPDATE tool_approvals
   SET uses_so_far = uses_so_far + 1
 WHERE id = $1 AND revoked_at IS NULL AND expires_at > now() AND uses_so_far < max_uses
RETURNING id
"""


def _epoch(value: Any) -> float:
    """Wall-clock epoch seconds from a TIMESTAMPTZ value (0.0 when unusable).

    asyncpg hands back an AWARE datetime for TIMESTAMPTZ, so .timestamp() is
    absolute; a naive value (a fake row, hand SQL) is read as local time. The
    only consumer is an expiry comparison, so a skew there costs at most one
    token's lifetime -- and it errs towards refusing, never towards granting.
    """
    if isinstance(value, datetime):
        try:
            return value.timestamp()
        except (OverflowError, OSError, ValueError):
            return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _as_uuid(value: Any) -> uuid.UUID | None:
    """A UUID column as uuid.UUID, or None when the value is unusable.

    A row whose id cannot be parsed cannot be consumed, and a token that cannot
    be consumed must never be treated as matched -- the caller treats None as
    "skip this row", which is fail-closed.
    """
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


def _row_to_token(row: Any) -> ApprovalToken | None:
    """One result row -> ApprovalToken, or None when the row is unreadable.

    A malformed row must not take the whole read down: the other rows are still
    approvals worth honouring, so it is skipped and logged at DEBUG.
    """
    try:
        token_id = _as_uuid(row["id"])
        if token_id is None:
            return None
        key_id = None if row["key_id"] is None else _as_uuid(row["key_id"])
        fingerprint = row["args_fingerprint"]
        return ApprovalToken(
            id=token_id,
            key_id=key_id,
            server=str(row["server"]),
            tool=str(row["tool"]),
            args_fingerprint=None if fingerprint is None else str(fingerprint),
            max_uses=int(row["max_uses"]),
            uses_so_far=int(row["uses_so_far"]),
            expires_at=_epoch(row["expires_at"]),
            issued_by=str(row["issued_by"] or ""),
            reason=str(row["reason"] or ""),
            revoked_at=None if row["revoked_at"] is None else _epoch(row["revoked_at"]),
        )
    except (KeyError, IndexError, TypeError, ValueError):
        log.debug("approval: skipping an unreadable tool_approvals row", exc_info=True)
        return None


async def record_request(key_id: Any, server: str, tool: str, fingerprint: str, mode: str) -> str | None:
    """Upsert ONE "this call would have needed approval" observation.

    Returns the request_id an operator grants against (stable across repeats).
    RAISES on a store fault -- the caller decides what a fault MEANS, and that
    answer is deliberately different per mode: warn swallows it, enforce
    refuses (design §4).
    """
    row = await db.fetchrow(_REQUEST_UPSERT_SQL, key_id, server, tool, fingerprint, mode)
    return None if row is None else str(row["request_id"])


async def fetch_tokens(server: str, tool: str) -> list[ApprovalToken]:
    """Every LIVE token for one (server, tool) -- the input to match().

    Raises on a store fault (see record_request). One indexed read
    (idx_tool_approvals_live); the table holds issued grants, so the row count
    for one tool is an operator-scale number, not a call-scale one.
    """
    rows = await db.fetch(_LIVE_TOKENS_SQL, server, tool)
    tokens: list[ApprovalToken] = []
    for row in rows:
        token = _row_to_token(row)
        if token is not None:
            tokens.append(token)
    return tokens


async def consume(token_id: uuid.UUID) -> bool:
    """Spend ONE use of a token. True = this call owns the use; False = lost a
    race (exhausted, revoked or expired since the read) -- the caller refuses.

    Raises on a store fault (see record_request).
    """
    return await db.fetchrow(_CONSUME_SQL, token_id) is not None


# ---------------------------------------------------------------------------
# The gate: one call in, one decision out
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GateDecision:
    """What the gate decided about ONE (server, tool, arguments) call.

    ``token_id`` is the approval that authorized it (enforce mode only) so the
    route can JOIN the hash-chained mcp_call_audit row to it: an approval that
    is not in the chain did not happen, as far as an auditor is concerned.
    ``request_id`` is the observe row an operator can grant against.
    """

    refused: bool = False
    reason: str = ""
    token_id: str | None = None
    request_id: str | None = None


_ALLOW = GateDecision()


def store_fault_text(server: str, tool: str) -> str:
    """The refusal text for a call the gate cannot judge (store unreachable).

    FAIL CLOSED (design §4): the failure mode this gate prevents is
    irreversible, so "cannot tell" means "no". The text says WHY, so an
    operator reading a model's transcript does not go hunting for a token bug.
    """
    return (
        f"error: the gateway refused to run {server}__{tool}: the approval store is unreachable, so the"
        " gateway cannot prove this call was approved. Approval FAILS CLOSED (design §4): a destructive"
        " tool behind a broken approval gate is an incident, not an outage. The call was NOT executed;"
        " retry once the database is healthy."
    )


def refusal_text(server: str, tool: str, fingerprint: str, request_id: str | None) -> str:
    """The refusal text for a call with no matching live token.

    Design §3.1: the message must name the EXACT command an operator can run to
    issue a token -- a refusal a model can only retry is a refusal that burns
    the turn. The fingerprint shown is the CALL's own (derived from arguments
    the caller sent), never an approved token's, so the refusal cannot become
    an oracle for what shapes are already approved.
    """
    head = (
        f"error: the gateway refused to run {server}__{tool}: this call requires an approval token"
        " (tool_approval_mode=enforce) and no live token matched. The call was NOT executed."
        # Say the quiet part out loud: the caller is often a MODEL, and a bare "error:"
        # reads as transient. Without this clause an agent retries the identical call,
        # which re-records the request, settles another denied row and eventually trips
        # the loop guard -- three signals that all mean "it was told no and kept going".
        " RETRYING WILL NOT HELP: the same arguments are refused until an operator grants"
        " a token, so report this to the user instead of calling again."
    )
    if request_id:
        body = (
            f" It was recorded as approval request {request_id}; an operator can authorize exactly"
            " these arguments with: python scripts/tool_approval_admin.py grant"
            f" --request {request_id} --uses 1 --ttl 15m (scripts/ is not in the gateway image --"
            " run it from a checkout that can reach the database)."
        )
    else:
        body = (
            " The approval store could not record the request (database fault), so no request id is"
            " available; retry once the store is healthy."
        )
    return head + body + f" Arguments fingerprint: {fingerprint}."


async def gate_call(
    key_id: Any,
    server: str,
    tool: str,
    arguments: Any,
    *,
    now: float | None = None,
) -> GateDecision:
    """Decide ONE /mcp call: allow it, or refuse it with an operator-actionable
    reason.

    CALLED FROM _gated_mcp_call after the ACL, argument policy, tool policy and
    dispatch pin, before the upstream session opens -- the same position as the
    pin gate, for the same reason (refuse before side effects, never after).

    OFF: returns before ANY work -- no settings parse beyond the mode read, no
    fingerprint, no store access. This is the shipped default, and it is what
    makes the wave byte-identical to pre-B8 (a test pins the zero statements).

    WARN: if the tool is on the list, RECORD the observation (best-effort: a
    store fault is logged and swallowed) and allow the call untouched. This is
    the observe clock the rollout starts.

    ENFORCE: a matching live token is consumed and the call is allowed;
    anything else refuses -- no token, a fingerprint mismatch, an expired or
    exhausted token, a lost consume race, or an unreachable store (fail
    closed). A refusal is RECORDED first so the refusal text can name the
    request id an operator grants against.
    """
    mode = resolve_mode(getattr(settings, "tool_approval_mode", DEFAULT_MODE))
    if mode == MODE_OFF:
        return _ALLOW
    if not requires_approval(server, tool):
        # Design §4: a tool not on the list keeps the unchanged path at zero
        # cost -- not even a fingerprint is computed.
        return _ALLOW
    fingerprint = args_fingerprint(arguments)

    if mode == MODE_WARN:
        try:
            await record_request(key_id, server, tool, fingerprint, MODE_WARN)
        except Exception:
            # BEST-EFFORT, traces posture: an observe write must never fail a
            # call. Warn mode changes no behaviour BY CONTRACT.
            log.exception("approval: warn-mode record failed for %s/%s -- the call proceeds", server, tool)
        return _ALLOW

    # ENFORCE from here on: every path either consumes a live token or refuses.
    try:
        tokens = await fetch_tokens(server, tool)
    except Exception:
        log.exception("approval: ENFORCE could not read the approval store for %s/%s -- refusing", server, tool)
        return GateDecision(refused=True, reason=store_fault_text(server, tool))

    token = match(tokens, key_id, server, tool, fingerprint, time.time() if now is None else now)
    if token is not None:
        try:
            consumed = await consume(token.id)
        except Exception:
            log.exception("approval: ENFORCE could not consume token %s -- refusing", token.id)
            return GateDecision(refused=True, reason=store_fault_text(server, tool))
        if consumed:
            return GateDecision(token_id=str(token.id))
        # The consume is conditional, so False means the token was spent,
        # revoked or expired between the read and the write. That is a REFUSAL
        # (fail closed) and it falls through to the recorded-refusal path.

    request_id: str | None = None
    try:
        request_id = await record_request(key_id, server, tool, fingerprint, MODE_ENFORCE)
    except Exception:
        # The refusal stands either way (we are refusing); only the operator's
        # request id is missing, and the text says so.
        log.exception("approval: ENFORCE could not record the refused call %s/%s", server, tool)
    return GateDecision(refused=True, reason=refusal_text(server, tool, fingerprint, request_id), request_id=request_id)
