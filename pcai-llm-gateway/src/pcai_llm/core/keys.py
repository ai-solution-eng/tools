"""Virtual API key management.

Keys are random 32-byte urlsafe tokens, stored only as SHA-256 hashes.
Plaintext is returned exactly once at creation. Rotation = create new key,
migrate, revoke old (fleet convention, same as the applygate api-keys
Secret rotation story).
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from ..db import db

KEY_BYTES = 32


def hash_key(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode()).hexdigest()


@dataclass
class KeyRecord:
    id: uuid.UUID
    user_id: uuid.UUID
    name: str
    key_prefix: str
    scopes: list[str]
    allowed_models: list[str]
    rate_limit_rpm: int
    rate_limit_tpm: int
    max_concurrent: int  # 0 = unlimited (backpressure cap, NOT a fallback)
    expires_at: datetime | None
    # TRUE = tool relay engages WITHOUT the client header (per-key default;
    # the X-PCAI-Execute-Tools header still overrides per request)
    relay_default: bool = False
    # TRUE = the in-stream {"pcai": …} SSE telemetry events are emitted on
    # this key's relayed streams (RFC-PER-KEY-STREAM-EVENTS). TRUE keeps
    # today's bytes (console/DSH parse these events); a key flipped OFF gets
    # a byte-clean OpenAI-spec stream — strict validating clients (opencode,
    # clearwing) abort on the non-spec lines. The X-PCAI-Stream-Events
    # header (0|1) still overrides per request, like relay_default's.
    stream_pcai_events: bool = True
    # Per-profile off-peak billing fraction (NULL = gateway default). Read
    # via the profile JOIN at authenticate time so every request bills by
    # the CURRENT profile setting — console edits are live immediately.
    offpeak_fraction: float | None = None
    # MCP-surface admin opt-in (P0-3 hardening, 2026-10-26): DEFAULT FALSE is
    # the control. admin-tool verbs (sqlhandler admin_*) are refused for any
    # key whose record carries False — and a row that predates the column or
    # a test fake that omits the field reads as False (getattr/get-safe), so
    # no un-flagged key can ever authenticate into admin capability.
    mcp_admin: bool = False


def generate_key() -> tuple[str, str]:
    """Return (plaintext, sha256-hash). The plaintext is never stored."""
    plaintext = "hllm-" + secrets.token_urlsafe(KEY_BYTES)
    return plaintext, hash_key(plaintext)


async def create_key(
    user_id: uuid.UUID,
    name: str,
    scopes: list[str],
    allowed_models: list[str] | None = None,
    rate_limit_rpm: int = 60,
    rate_limit_tpm: int = 100_000,
    expires_at: datetime | None = None,
    relay_default: bool = False,
    # DEFAULT-FLIP (RFC-PER-KEY-STREAM-EVENTS consumer inventory, 2026-09-24):
    # new keys are spec-clean by default (unknown OpenAI-compatible clients
    # never see the non-spec {"pcai": …} lines). The inventory's two
    # event-PARSING consumers opt in explicitly: the console's create form
    # posts stream_pcai_events=true, and enrollment (the DSH key path)
    # passes True below. Existing keys keep their stored value.
    stream_pcai_events: bool = False,
    *,
    # Transactional variant (audit fix, enroll-redeem): pass an ACQUIRED
    # connection to join the caller's transaction — the redeem flow needs
    # code-consumption + user + key + preset seeding to commit or roll back
    # AS ONE, so a post-increment crash can never burn a single-use code
    # (finding 8). conn=None (default) = module-pool write, today's behavior.
    conn: Any | None = None,
    # Spend lineage (budget-carry across rotation, 2026-10-09): the rotate
    # paths pass the OLD key's spend_ancestors || [old_id] so the fresh key's
    # budget gates sum the whole lineage's in-window ledger spend — rotation
    # must never reset a period budget (the SSO rotation budget-reset bypass).
    # Default [] = a fresh mint (spend starts at zero, which is correct).
    spend_ancestors: list[uuid.UUID] | None = None,
) -> tuple[str, uuid.UUID]:
    plaintext, key_hash = generate_key()
    row = await (conn if conn is not None else db).fetchrow(
        """
        INSERT INTO api_keys
            (user_id, name, key_hash, key_prefix, scopes, allowed_models,
             rate_limit_rpm, rate_limit_tpm, expires_at, relay_default,
             stream_pcai_events, spend_ancestors)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
        RETURNING id
        """,
        user_id,
        name,
        key_hash,
        plaintext[:10],
        scopes,
        allowed_models or [],
        rate_limit_rpm,
        rate_limit_tpm,
        expires_at,
        relay_default,
        stream_pcai_events,
        spend_ancestors or [],
    )
    assert row is not None  # INSERT ... RETURNING always yields a row
    return plaintext, row["id"]


async def authenticate(plaintext: str) -> KeyRecord | None:
    """Resolve a bearer key to its record, enforcing revocation/expiry.

    Returns None for unknown/revoked/expired keys — callers MUST treat that
    as 401 (fail closed; the AgentBuilder audit lesson: never default-open).
    """
    row = await db.fetchrow(
        """
        SELECT k.id, k.user_id, k.name, k.key_prefix, k.scopes, k.allowed_models,
               k.rate_limit_rpm, k.rate_limit_tpm, k.max_concurrent,
               k.expires_at, k.revoked_at, k.relay_default, k.stream_pcai_events,
               k.mcp_admin, p.offpeak_fraction
        FROM api_keys k
        JOIN users u ON u.id = k.user_id
        LEFT JOIN profiles p ON p.id = u.profile_id
        WHERE k.key_hash = $1
        """,
        hash_key(plaintext),
    )
    if row is None or row["revoked_at"] is not None:
        return None
    if row["expires_at"] is not None and row["expires_at"] <= datetime.now(UTC):
        return None
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
        # row.get (not row[...]): asyncpg Records AND the tests' dict fakes
        # both implement .get — a SELECT or a fake row missing the column
        # degrades to False (the control default), never to a KeyError.
        mcp_admin=bool(row.get("mcp_admin", False)),
    )


async def load_key_record(key_id: uuid.UUID) -> KeyRecord | None:
    """Resolve a key BY ID to its record (the scheduled-distill path: a sweep
    row is a key id, not a bearer token). Same SELECT shape and the SAME
    fail-closed contract as authenticate - revoked/expired keys are None,
    never a record (a revoked key's conversation must not mint spend)."""
    row = await db.fetchrow(
        """
        SELECT k.id, k.user_id, k.name, k.key_prefix, k.scopes, k.allowed_models,
               k.rate_limit_rpm, k.rate_limit_tpm, k.max_concurrent,
               k.expires_at, k.revoked_at, k.relay_default, k.stream_pcai_events,
               p.offpeak_fraction
        FROM api_keys k
        JOIN users u ON u.id = k.user_id
        LEFT JOIN profiles p ON p.id = u.profile_id
        WHERE k.id = $1
        """,
        key_id,
    )
    if row is None or row["revoked_at"] is not None:
        return None
    if row["expires_at"] is not None and row["expires_at"] <= datetime.now(UTC):
        return None
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
        # row.get (not row[...]): asyncpg Records AND the tests' dict fakes
        # both implement .get — a SELECT or a fake row missing the column
        # degrades to False (the control default), never to a KeyError.
        mcp_admin=bool(row.get("mcp_admin", False)),
    )


async def revoke_key(key_id: uuid.UUID) -> bool:
    tag = await db.execute(
        "UPDATE api_keys SET revoked_at = now() WHERE id = $1 AND revoked_at IS NULL",
        key_id,
    )
    return tag == "UPDATE 1"


async def list_keys(user_id: uuid.UUID | None = None, include_revoked: bool = False) -> list[dict[str, Any]]:
    """Active keys by default, alphabetical by key name (the console's
    scan order); same-name ties (a rotation reissues under the SAME name)
    keep newest-first. Revoked rows stay in the DB (the ledger
    references them) but are hidden from the list unless asked for.

    Phase A identity projection: bound_via + the bound subject string ride
    along via a read-time LEFT JOIN through api_keys.subject_id (the binding
    is immutable after issuance, so the join is faithful); unbound keys read
    NULL/'' and render as "no binding" in the console.

    Self-mint projection (review F1/F5): users.self_mint_enabled rides along
    from the existing users JOIN — the admin console's Self-mint column and
    its toggle render from the SAME payload as every other column. Without
    it the column's `typeof k.self_mint_enabled === 'boolean'` gate saw
    undefined on every row and the toggle was unreachable from the UI. A
    per-USER constant repeated across the user's key rows is safe: the
    console renders one toggle per key row and PUT /keys/self-mint-
    eligibility + loadKeys() re-read re-converge every row of that user.

    Profile projection (2026-10-17): the user's PROFILE rides along from the
    same users JOIN, plus the profile NAME from a LEFT JOIN on profiles.
    WHY: profiles are USER-scoped (api_keys carries NO profile column — the
    cascade resolves through api_keys -> users -> profiles, see
    core.cascade.profile_order_for_key), so a key row is the ONLY place the
    console can show and edit which ordering actually governs that key. The
    admin had no control to reassign an existing user's profile at all:
    Keys > Profile was create-time only, and the tenant SSO default only
    affects NEW SSO mints. LEFT JOIN because a user may carry no profile
    (NULL profile_id = no cascade ordering; the console renders the dash).
    Repeated per-key like self_mint_enabled — a per-USER constant across the
    user's rows is safe, and every row re-converges on the next loadKeys()."""
    sql = """
        SELECT k.id, k.user_id, u.name AS user_name, k.name, k.key_prefix,
               k.scopes, k.allowed_models, k.rate_limit_rpm, k.rate_limit_tpm,
               k.expires_at, k.revoked_at, k.created_at, k.relay_default,
               k.stream_pcai_events, k.mcp_admin,
               k.bound_via, k.bound_at, s.subject AS subject, s.idp AS subject_idp,
               u.self_mint_enabled AS self_mint_enabled,
               u.profile_id AS user_profile_id, p.name AS user_profile_name
        FROM api_keys k JOIN users u ON u.id = k.user_id
        LEFT JOIN subjects s ON s.id = k.subject_id
        LEFT JOIN profiles p ON p.id = u.profile_id
    """
    args: list[Any] = []
    conds = []
    if user_id is not None:
        args.append(user_id)
        conds.append(f"k.user_id = ${len(args)}")
    if not include_revoked:
        conds.append("k.revoked_at IS NULL")
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    sql += " ORDER BY k.name, k.created_at DESC"
    return [dict(r) for r in await db.fetch(sql, *args)]
