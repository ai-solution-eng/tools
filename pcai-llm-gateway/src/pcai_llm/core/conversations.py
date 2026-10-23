"""Key-scoped server-side saved conversations (data layer).

Per API token ("per key") transcripts: every conversation row belongs to
the key that created it and EVERY query in this module filters by
``key_id`` — a foreign key id can neither read, mutate, nor even confirm
the existence of another key's conversation (missing and foreign are the
same ``None``/``False``, so no existence oracle).

Storage model: the client pushes the FULL transcript per turn (replace-all
— idempotent, no partial state, matches the chat console's left panel) and
the server stores it verbatim under an enumerated ``seq``. Size caps are
enforced app-side (≤ MAX_MSGS messages, ≤ MAX_MSG_CHARS each) so the
database never becomes the abuse surface.

Transactions: the delete+insert swap runs in ONE transaction using the
repo pattern (``db.acquire_bounded`` + ``conn.transaction``, as routes_admin
does with ``db.acquire`` — the db layer deliberately exposes no txn helper)
so a concurrent reader sees either the old transcript or the new one, never
a half-applied mix. This is a TENANT-FACING write, so it takes the BOUNDED
acquire (D-P11): pool exhaustion surfaces as PoolUnavailable -> fast 503
instead of queueing silently behind the pool.
"""

from __future__ import annotations

import builtins
import re
import uuid
from typing import Any

from ..db import db

MAX_MSGS = 200
MAX_MSG_CHARS = 32_000
MAX_TITLE = 120
TITLE_CHARS = 60  # auto-title length: first user message's first 60 chars

DEFAULT_TITLE = "New conversation"
ALLOWED_ROLES = ("user", "assistant")

_WS = re.compile(r"\s+")

# Aliases evaluated BEFORE `def list` below shadows the builtin within this
# module (the API surface is named `list` per contract).
ConvRow = dict[str, Any]
ConvRows = list[dict[str, Any]]


class ConversationValidationError(ValueError):
    """A payload violates the conversation caps / shape.

    ``code`` classifies the failure so the API layer maps it precisely:
    ``too_many_messages`` / ``message_too_large`` → 413 (parsed fine, just
    too big); ``invalid_list`` / ``invalid_message`` / ``invalid_role`` /
    ``invalid_content`` → 400 (malformed).
    """

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


class ConversationNotFound(LookupError):
    """A write targeted an id that is missing or owned by another key."""


# ---------------------------------------------------------------------------
# title helpers
# ---------------------------------------------------------------------------


def _collapse(text: str) -> str:
    """Collapse all whitespace runs to single spaces and strip the ends."""
    return _WS.sub(" ", text).strip()


def sanitize_title(title: str) -> str:
    """Strip, collapse whitespace, empty → 'New conversation', cap 120."""
    cleaned = _collapse(title)[:MAX_TITLE].strip()
    return cleaned or DEFAULT_TITLE


def auto_title(messages: Any) -> str:
    """Title from the FIRST user message's first TITLE_CHARS characters.

    Applied when a conversation still carries the default placeholder: the
    left panel then shows what the chat is about instead of a column of
    identical 'New conversation' rows. No user message (or a non-list
    payload) → the placeholder survives.
    """
    if not isinstance(messages, builtins.list):
        return DEFAULT_TITLE
    for item in messages:
        if isinstance(item, dict) and item.get("role") == "user" and isinstance(item.get("content"), str):
            candidate = _collapse(item["content"])[:TITLE_CHARS].strip()
            if candidate:
                return candidate
    return DEFAULT_TITLE


# ---------------------------------------------------------------------------
# data access — every query is key_id-scoped
# ---------------------------------------------------------------------------


async def create(key_id: uuid.UUID, user_id: uuid.UUID | None, title: str | None = None) -> ConvRow:
    """Create an empty conversation owned by ``key_id``.

    The title is sanitized; None/empty → the default placeholder, which the
    first message save later replaces via auto-title (see replace_messages).
    """
    row = await db.fetchrow(
        """
        INSERT INTO conversations (key_id, user_id, title)
        VALUES ($1, $2, $3)
        RETURNING id, key_id, user_id, title, created_at, updated_at
        """,
        key_id,
        user_id,
        DEFAULT_TITLE if title is None else sanitize_title(title),
    )
    assert row is not None  # INSERT ... RETURNING always yields a row
    return dict(row)


async def list(key_id: uuid.UUID, limit: int = 200) -> ConvRows:
    """The key's conversations, newest activity first, with message counts.

    One round-trip: the count is a correlated subselect and the
    (key_id, updated_at DESC) index drives the ordering. ``limit`` bounds
    the page.
    """
    rows = await db.fetch(
        """
        SELECT c.id, c.title, c.updated_at,
               (SELECT count(*) FROM conversation_messages m
                WHERE m.conversation_id = c.id) AS message_count
        FROM conversations c
        WHERE c.key_id = $1
        ORDER BY c.updated_at DESC
        LIMIT $2
        """,
        key_id,
        limit,
    )
    return [dict(r) for r in rows]


async def get_meta(key_id: uuid.UUID, conversation_id: uuid.UUID) -> ConvRow | None:
    """Header only (no transcript) — {id, title, updated_at}.

    None when the conversation is missing OR owned by a different key.
    """
    row = await db.fetchrow(
        "SELECT id, title, updated_at FROM conversations WHERE id = $1 AND key_id = $2",
        conversation_id,
        key_id,
    )
    return dict(row) if row is not None else None


async def get(key_id: uuid.UUID, conversation_id: uuid.UUID) -> ConvRow | None:
    """Full transcript for one conversation, seq-ordered.

    404-semantics: None when the conversation is missing OR foreign — the
    two are indistinguishable by design.
    """
    head = await get_meta(key_id, conversation_id)
    if head is None:
        return None
    msgs = await db.fetch(
        "SELECT seq, role, content, ts FROM conversation_messages WHERE conversation_id = $1 ORDER BY seq",
        conversation_id,
    )
    return {**head, "messages": [dict(m) for m in msgs]}


def _validate_messages(messages: Any) -> builtins.list[ConvRow]:
    """Shape/cap validation with precise, index-addressed errors.

    Runs BEFORE any SQL: a malformed payload must fail without ever
    touching the stored transcript (the replace is all-or-nothing even at
    the validation stage).
    """
    if not isinstance(messages, builtins.list):
        raise ConversationValidationError("messages must be a list", "invalid_list")
    if len(messages) > MAX_MSGS:
        raise ConversationValidationError(
            f"too many messages: {len(messages)} exceeds the cap of {MAX_MSGS}",
            "too_many_messages",
        )
    checked: builtins.list[ConvRow] = []
    for i, item in enumerate(messages):
        if not isinstance(item, dict) or "role" not in item or "content" not in item:
            raise ConversationValidationError(
                f"messages[{i}] must be an object with 'role' and 'content'", "invalid_message"
            )
        role = item["role"]
        if role not in ALLOWED_ROLES:
            raise ConversationValidationError(
                f"messages[{i}].role must be one of {ALLOWED_ROLES}, got {role!r}", "invalid_role"
            )
        content = item["content"]
        if not isinstance(content, str):
            raise ConversationValidationError(f"messages[{i}].content must be a string", "invalid_content")
        if len(content) > MAX_MSG_CHARS:
            raise ConversationValidationError(
                f"messages[{i}] is too large: {len(content)} characters exceeds the cap of {MAX_MSG_CHARS}",
                "message_too_large",
            )
        checked.append({"role": role, "content": content})
    return checked


async def replace_messages(key_id: uuid.UUID, conversation_id: uuid.UUID, messages: Any) -> int:
    """Replace the ENTIRE transcript of one owned conversation.

    Single transaction (repo pattern): row lock (FOR UPDATE) → ownership
    check → title refresh → DELETE all → INSERT enumerated seq. Messages are
    stored 0-based under UNIQUE(conversation_id, seq); the write bumps
    conversations.updated_at (drives the newest-first listing).

    Auto-title: when the conversation still carries the default placeholder
    title, it becomes the first user message's first 60 chars. An explicit
    (or already auto-set) title is never overwritten.

    Returns the stored message count. Raises ConversationNotFound when the
    id is missing or foreign; ConversationValidationError on bad payloads
    (raised before the transaction opens — nothing is written).
    """
    checked = _validate_messages(messages)
    # BOUNDED acquire (D-P11): this is a tenant-facing route, so pool
    # exhaustion must surface as the fast 503 (PoolUnavailable) the hot-path
    # reads already raise — an unbounded db.acquire() would queue the request
    # silently and explode p99 under exhaustion. Admin-plane writes keep the
    # unbounded acquire (they are operator-driven, not request-volume).
    #
    # getattr, not db.acquire_bounded: the repo's fake-db doubles (and any
    # injected handle) only promise db.acquire — a bare attribute access
    # would break them with AttributeError. The REAL Database always has
    # acquire_bounded (db/__init__.py), so production never takes the
    # fallback; a fake without it keeps its previous unbounded behavior.
    acquire = getattr(db, "acquire_bounded", None) or db.acquire
    async with acquire() as conn, conn.transaction():
        head = await conn.fetchrow(
            "SELECT title FROM conversations WHERE id = $1 AND key_id = $2 FOR UPDATE",
            conversation_id,
            key_id,
        )
        if head is None:
            raise ConversationNotFound(f"conversation {conversation_id} not found")
        title = head["title"]
        if title == DEFAULT_TITLE:
            title = auto_title(checked)
        await conn.execute(
            "UPDATE conversations SET title = $2, updated_at = now() WHERE id = $1",
            conversation_id,
            title,
        )
        await conn.execute("DELETE FROM conversation_messages WHERE conversation_id = $1", conversation_id)
        if checked:
            await conn.executemany(
                "INSERT INTO conversation_messages (conversation_id, seq, role, content) VALUES ($1, $2, $3, $4)",
                [(conversation_id, seq, m["role"], m["content"]) for seq, m in enumerate(checked)],
            )
    return len(checked)


async def rename(key_id: uuid.UUID, conversation_id: uuid.UUID, title: str) -> ConvRow | None:
    """Set one owned conversation's title (sanitized, capped).

    Bumps updated_at: the row changed and the newest-first listing should
    reflect it. None when the id is missing or foreign.
    """
    row = await db.fetchrow(
        """
        UPDATE conversations SET title = $2, updated_at = now()
        WHERE id = $1 AND key_id = $3
        RETURNING id, title, updated_at
        """,
        conversation_id,
        sanitize_title(title),
        key_id,
    )
    return dict(row) if row is not None else None


async def delete(key_id: uuid.UUID, conversation_id: uuid.UUID) -> bool:
    """Delete one owned conversation (its messages cascade). False when the
    id is missing or foreign."""
    tag = await db.execute(
        "DELETE FROM conversations WHERE id = $1 AND key_id = $2",
        conversation_id,
        key_id,
    )
    return tag == "DELETE 1"
