"""Key-scoped saved-conversation API (data-plane, per API token).

Server-side transcripts behind the SAME bearer auth as the proxy: every
route resolves the virtual key via ``require_key`` and hands ``key.id`` to
the core layer, which scopes every query by it — a key can only ever see
and mutate its own conversations, and a foreign/unknown id is a plain 404
(no existence oracle). The router deliberately carries NO prefix: the
paths below carry ``/v1/conversations`` literally (the parent wires
``app.include_router(router)``).

Error contract: this surface returns ``{"error": "<message>"}`` bodies —
not FastAPI's default ``{"detail": ...}`` — so the chat console renders
one error shape everywhere. Messages name the offending field/index and
the cap exceeded. Payload caps map to 413 (body parsed fine, too big)
while malformed bodies map to 400. Path ids are taken as raw strings and
validated with ``uuid.UUID`` (ValueError → 404): a garbage id can never
500 or 422.
"""

from __future__ import annotations

import uuid
from typing import Any

import fastapi
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from ..core import conversations
from ..core.conversations import ConversationNotFound, ConversationValidationError
from ..core.keys import KeyRecord
from .auth import require_key

router = APIRouter(tags=["conversations"])

# Parsed-fine-but-over-cap violations → 413; everything else malformed → 400.
_TOO_LARGE_CODES = {"too_many_messages", "message_too_large"}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _error(status: int, message: str) -> JSONResponse:
    """Uniform {"error": ...} body — this surface's error envelope."""
    return JSONResponse(status_code=status, content={"error": message})


def _conversation_id(raw: str) -> uuid.UUID | None:
    """Parse a raw path segment; garbage is None (→ 404, never 500/422)."""
    try:
        return uuid.UUID(raw)
    except ValueError:
        return None


async def _json_object(request: Request) -> dict[str, Any] | JSONResponse:
    """Parse the body as a JSON object, else a precise 400.

    Manual parsing instead of pydantic request models: pydantic failures
    would surface as 422 with generic field details, and this surface
    promises 400 + a specific message.
    """
    # HARD byte cap (audit P1-3): request.json() buffers the WHOLE body in
    # memory with no cap — a chunked body amplifies worker memory. Read via
    # the bounded helper (same contract as routes_proxy._json_body: 413 the
    # moment the stream passes settings.max_body_bytes) instead.
    from .routes_proxy import _json_body as _bounded_json_body

    try:
        body = await _bounded_json_body(request)
    except HTTPException as exc:
        return _error(exc.status_code, exc.detail)
    except Exception:
        return _error(400, "request body must be valid JSON")
    if not isinstance(body, dict):
        return _error(400, "request body must be a JSON object")
    return body


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------


@router.get("/v1/conversations")
async def list_conversations(key: KeyRecord = fastapi.Depends(require_key)) -> dict[str, Any]:
    """The calling key's conversations, newest activity first."""
    return {"data": await conversations.list(key.id)}


@router.post("/v1/conversations", status_code=201)
async def create_conversation(
    request: Request,
    key: KeyRecord = fastapi.Depends(require_key),
) -> Any:
    """Create an empty conversation owned by the calling key ({title?})."""
    body = await _json_object(request)
    if isinstance(body, JSONResponse):
        return body
    title = body.get("title")
    if title is not None and not isinstance(title, str):
        return _error(400, "title must be a string")
    conv = await conversations.create(key.id, key.user_id, title=title)
    return {"id": conv["id"], "title": conv["title"], "updated_at": conv["updated_at"]}


@router.get("/v1/conversations/{conversation_id}")
async def get_conversation(
    conversation_id: str,
    key: KeyRecord = fastapi.Depends(require_key),
) -> Any:
    """Full seq-ordered transcript of one owned conversation."""
    cid = _conversation_id(conversation_id)
    if cid is None:
        return _error(404, "not found")
    conv = await conversations.get(key.id, cid)
    if conv is None:
        return _error(404, "not found")
    return conv


@router.patch("/v1/conversations/{conversation_id}")
async def rename_conversation(
    conversation_id: str,
    request: Request,
    key: KeyRecord = fastapi.Depends(require_key),
) -> Any:
    """Rename one owned conversation ({title} — sanitized, capped at 120)."""
    cid = _conversation_id(conversation_id)
    if cid is None:
        return _error(404, "not found")
    body = await _json_object(request)
    if isinstance(body, JSONResponse):
        return body
    title = body.get("title")
    if title is None:
        return _error(400, "title is required")
    if not isinstance(title, str):
        return _error(400, "title must be a string")
    renamed = await conversations.rename(key.id, cid, title)
    if renamed is None:
        return _error(404, "not found")
    return renamed


@router.put("/v1/conversations/{conversation_id}/messages")
async def replace_conversation_messages(
    conversation_id: str,
    request: Request,
    key: KeyRecord = fastapi.Depends(require_key),
) -> Any:
    """Replace the full transcript ({messages: [...]}) — the client pushes
    it per turn, so the swap is idempotent replace-all inside one
    transaction. 413 when over a cap (too many messages / one too large),
    400 when malformed, 404 when missing or foreign."""
    cid = _conversation_id(conversation_id)
    if cid is None:
        return _error(404, "not found")
    body = await _json_object(request)
    if isinstance(body, JSONResponse):
        return body
    if "messages" not in body:
        return _error(400, "messages is required")
    messages = body["messages"]
    if not isinstance(messages, list):
        return _error(400, "messages must be a list")
    try:
        saved = await conversations.replace_messages(key.id, cid, messages)
    except ConversationNotFound:
        return _error(404, "not found")
    except ConversationValidationError as exc:
        return _error(413 if exc.code in _TOO_LARGE_CODES else 400, str(exc))
    head = await conversations.get_meta(key.id, cid)
    # The conversation continued: drop the distilled marker so the scheduled
    # sweep RE-distills it (the DSH plugin lifecycle - the stored memory
    # tracks the conversation's latest state). Best-effort, never affects
    # the response; a no-op when the distiller never touched this
    # conversation (no marker row).
    try:
        from ..core import distill_schedule

        await distill_schedule.mark_stale(cid, key.id)
    except Exception:
        pass
    return {"saved": saved, "updated_at": head["updated_at"] if head else None}


@router.delete("/v1/conversations/{conversation_id}")
async def delete_conversation(
    conversation_id: str,
    key: KeyRecord = fastapi.Depends(require_key),
) -> Any:
    """Delete one owned conversation (its messages cascade with it)."""
    cid = _conversation_id(conversation_id)
    if cid is None:
        return _error(404, "not found")
    if not await conversations.delete(key.id, cid):
        return _error(404, "not found")
    return {"deleted": True}
