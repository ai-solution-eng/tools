"""Exact-match response cache for non-streaming chat (opt-in).

WHY: a repeated identical request (an agent retry, a title regeneration, a
client re-sending the same turn) currently costs a full prefill + decode.
The upstream PREFIX cache only saves the re-prefill of a shared prefix; a
response cache skips the model entirely. This is the embeddings cache idea
generalised to chat, with three deliberate constraints:

* **Deterministic requests only** — the caller must set `temperature: 0`.
  A temperature-1 request is a SAMPLE; replaying one answer would silently
  change the client's expected distribution. Absent temperature is not 0.
* **Non-streaming only** — a streamed answer is a sequence of SSE frames;
  replay is a different feature. Relay (tool-loop) turns are never cached.
* **Opt-in by mode** — `response_cache_mode` is off | shadow | active.
  Shadow computes the key and records WOULD-BE hits without serving, so the
  duplicate rate is measured before any answer is ever replayed.

Contract (same discipline as core.embedcache and core.stickiness): every DB
call is fail-open. A cache fault must never fail a request — a miss is
always a correct answer, just a more expensive one. A hit settles ONE
`cache_hit` ledger row (free: the budget SUM excludes that status), exactly
like the embeddings cache, so the hit is visible without being billable.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from typing import Any

from ..config import settings
from ..db import db

log = logging.getLogger(__name__)

# Transport-level / non-semantic fields: excluding them lets the same
# question hit the cache whether it was asked streamed, with a user tag, or
# with a request id. Anything that CHANGES THE ANSWER stays in the hash.
_VOLATILE = frozenset({"stream", "stream_options", "user", "metadata", "request_id"})


def eligible(body: dict[str, Any]) -> bool:
    """True when this request may be cached at all (mode-independent).

    temperature must be PRESENT and exactly 0. A missing temperature means
    the server default (1) — a sample, not a fact. Tools are allowed: a
    deterministic tool-call response is as cacheable as any other.
    """
    if bool(body.get("stream")):
        return False
    raw = body.get("temperature")
    if raw is None:
        return False
    try:
        return float(raw) == 0.0
    except (TypeError, ValueError):
        return False


def request_hash(body: dict[str, Any]) -> str:
    """sha256 of the canonical request (volatile fields excluded). PURE."""
    payload = {k: v for k, v in body.items() if k not in _VOLATILE}
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


def mode() -> str:
    """The effective cache mode, normalised; unknown values degrade to off."""
    value = str(getattr(settings, "response_cache_mode", "off") or "off").strip().lower()
    return value if value in ("shadow", "active") else "off"


async def lookup(key_id: uuid.UUID, model: str, req_hash: str) -> bytes | None:
    """A live cached answer, or None. NEVER raises (fail-open miss)."""
    try:
        row = await db.fetchrow(
            """
            SELECT response_body
            FROM response_cache
            WHERE key_id = $1 AND model = $2 AND request_hash = $3
              AND expires_at > now()
            """,
            key_id,
            model,
            req_hash,
        )
    except Exception:
        log.warning("response_cache: lookup failed (treating as miss)", exc_info=True)
        return None
    if row is None:
        return None
    body = row["response_body"]
    if body is None:
        return None
    return bytes(body)


async def store(key_id: uuid.UUID, model: str, req_hash: str, body: bytes) -> None:
    """Store (or refresh) one answer. NEVER raises; oversize bodies skipped."""
    if not body:
        return
    max_bytes = int(getattr(settings, "response_cache_max_bytes", 256 * 1024) or 0)
    if max_bytes > 0 and len(body) > max_bytes:
        return
    ttl_s = int(getattr(settings, "response_cache_ttl_s", 3600) or 3600)
    if ttl_s <= 0:
        return
    try:
        await db.execute(
            """
            INSERT INTO response_cache (key_id, model, request_hash, response_body, expires_at)
            VALUES ($1, $2, $3, $4, now() + make_interval(secs => $5))
            ON CONFLICT (key_id, model, request_hash) DO UPDATE
               SET response_body = EXCLUDED.response_body,
                   created_at    = now(),
                   expires_at    = EXCLUDED.expires_at
            """,
            key_id,
            model,
            req_hash,
            body,
            ttl_s,
        )
    except Exception:
        log.warning("response_cache: store failed (answer still served)", exc_info=True)


async def purge_expired() -> None:
    """Housekeeping: drop expired rows. NEVER raises."""
    try:
        await db.execute("DELETE FROM response_cache WHERE expires_at < now()")
    except Exception:
        log.debug("response_cache: expiry purge skipped", exc_info=True)
