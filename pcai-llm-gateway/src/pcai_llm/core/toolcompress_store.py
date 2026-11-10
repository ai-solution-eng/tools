"""Durable store for compressed tool-result originals (the retrieval half).

WHY: compression is only safe if the original survives somewhere the model
can reach. The relay writes every compressed original here under its content
digest, and `pcai__retrieve` reads it back. Two properties are load-bearing:

  * KEY-SCOPED. The primary key is (key_id, digest): a digest resolves only
    for the key that produced it. A globally addressable store would be a
    cross-tenant read primitive — the digest is a hash of content an attacker
    may be able to guess — so the key is part of the identity, not a filter
    applied afterwards.
  * TTL-BOUND. Rows are reclamation-only: the lookup never checks age (a row
    that outlives its TTL is still CORRECT to serve), and housekeeping
    deletes by created_at. Expiry therefore degrades to "the model is told
    the digest is gone", never to a wrong answer.

WHY POSTGRES AND NOT AN IN-PROCESS DICT: retrieval can happen on a LATER
turn, and a later turn can land on a different replica. An in-process map
would make retrieval a replica lottery. The write is one INSERT on the
compression path (only for results that actually compressed); a failure
there fails the COMPRESSION, not the turn — the caller passes the original
through untouched.
"""

from __future__ import annotations

import logging
import uuid

from ..db import db

log = logging.getLogger(__name__)

# Per-key retention quota (audit P3). The store had NO per-key cap, so one key
# could pin unbounded text for the whole TTL (a day by default) on a shared
# Postgres. The cap is on the SUM of `chars` — which is also what finally READS
# that column: it was written and never read, so the quota and the column are
# one change. 8 MB per key admits ~8 maximum-size originals (the per-row cap is
# config.tool_compress_max_store_chars, 1 MB) or a long tail of ordinary tool
# results, while keeping a tenant's footprint a rounding error of the database.
# Deliberately a module constant, not a Settings field: config.py belongs to
# another zone this wave (a PCAI_LLM_TOOL_COMPRESS_KEY_QUOTA_CHARS knob is the
# natural follow-up).
KEY_QUOTA_CHARS = 8_000_000


class StoreQuotaExceeded(Exception):
    """The key's retained-original quota is full — save() stored NOTHING.

    A distinct type because the CALLER must treat it differently from a write
    fault: it is a POLICY outcome (the compression simply does not happen and
    the original passes through), not an infrastructure error worth a
    traceback. The relay catches it by name.
    """


async def save(key_id: uuid.UUID, digest: str, body: str) -> None:
    """Persist one original under (key_id, digest). Idempotent by design.

    ON CONFLICT DO NOTHING, not DO UPDATE: the digest IS the content address,
    so an existing row holds byte-identical text. Rewriting it would only
    move created_at forward and extend the TTL of an unchanged payload.

    QUOTA (audit P3): the key's retained total is checked BEFORE the INSERT and
    a breach raises StoreQuotaExceeded — the caller passes the original
    through, so a full quota can never produce a marker pointing at a row that
    was not written. An idempotent re-save of a digest the key ALREADY holds is
    always allowed: ON CONFLICT DO NOTHING means it cannot grow the total, and
    refusing it would break a repeated tool call at the cap.
    """
    row = await db.fetchrow(
        """
        SELECT COALESCE(SUM(chars), 0) AS used,
               COALESCE(BOOL_OR(digest = $2), FALSE) AS present
          FROM tool_compress_store
         WHERE key_id = $1
        """,
        key_id,
        digest,
    )
    used = int((row or {}).get("used") or 0)
    present = bool((row or {}).get("present"))
    if not present and used + len(body) > KEY_QUOTA_CHARS:
        log.warning(
            "tool_compress_store: key %s is at its retention quota (%d + %d > %d chars) — not storing %s",
            key_id,
            used,
            len(body),
            KEY_QUOTA_CHARS,
            digest,
        )
        raise StoreQuotaExceeded(f"key retention quota exhausted ({used} chars retained)")
    await db.execute(
        """
        INSERT INTO tool_compress_store (key_id, digest, body, chars)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (key_id, digest) DO NOTHING
        """,
        key_id,
        digest,
        body,
        len(body),
    )


async def load(key_id: uuid.UUID, digest: str) -> str | None:
    """The stored original, or None when unknown/expired/another key's."""
    row = await db.fetchrow(
        "SELECT body FROM tool_compress_store WHERE key_id = $1 AND digest = $2",
        key_id,
        digest,
    )
    if row is None:
        return None
    body = row["body"]
    return body if isinstance(body, str) else None


async def prune(ttl_seconds: int) -> int:
    """Delete rows older than the TTL; returns the number reclaimed.

    0 (or negative) disables reclamation — the operator contract shared with
    the other retention knobs: nothing is deleted until a policy exists.
    """
    if ttl_seconds <= 0:
        return 0
    tag = await db.execute(
        "DELETE FROM tool_compress_store WHERE created_at < now() - make_interval(secs => $1)",
        float(ttl_seconds),
    )
    try:
        return int(str(tag).split()[-1])
    except (ValueError, IndexError):
        return 0
