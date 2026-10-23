"""Client tool-server dispatch routes (ROADMAP §7 phase 2 wave B — the
SPIKE surface, mounted ONLY when settings.client_tool_dispatch is on).

The three-message protocol (design doc: ROADMAP § 7 phase 2 wave B):

1. POST /v1/dispatch/register — the client OFFERS its local tools
   (name + description + inputSchema + optional per-tool timeout).
2. GET  /v1/dispatch/poll — the client long-polls; the response IS the
   delivery (dispatch-once: core.clientside.claim pops the queue
   synchronously before any response is written, so a retry or second
   tab can never re-deliver a claimed job).
3. POST /v1/dispatch/result — the client answers; the first answer
   settles the dispatch, a replay gets 409 and is dropped.

Auth: the SAME bearer key as /v1/chat/completions (require_scope
"proxy") — no second credential, no new revocation surface; revoking
the key kills the channel by the same 401 that kills chat. Everything
is scoped by key_id: an offer is visible only to that key's jobs and a
result is acceptable only from the offering key.

Error contract mirrors routes_conversations: {"error": "<message>"}
bodies everywhere (the chat console renders one shape), 401/403 from
the shared dependency, 404 unknown offer, 409 settled/unknown
dispatch_id, 413 oversized payloads.

This router is NOT included by app.py in the spike — mounting happens
in wave B2 behind the flag. Tests mount it on a bare FastAPI app with
the flag flipped via monkeypatch.
"""

from __future__ import annotations

import asyncio
import json
import math
import time
from typing import Any

import fastapi
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from ..config import settings
from ..core import clientside
from .auth import require_scope

router = APIRouter(tags=["dispatch"])

# Long-poll ceiling (seconds): must stay comfortably inside the ingress
# timeout — a poll is a plain GET, and the clean-close rule (relay wave)
# applies to any request that holds a connection open.
MAX_POLL_WAIT_S = 60.0
DEFAULT_POLL_WAIT_S = 25.0
# Result payload bound (bytes) — mirrors the conversation message cap
# philosophy: a tool result bigger than this is abuse, not data.
MAX_RESULT_BYTES = 1024 * 1024


def _error(status: int, message: str) -> JSONResponse:
    """Uniform {"error": ...} body — this surface's error envelope."""
    return JSONResponse(status_code=status, content={"error": message})


async def _json_body(request: Request) -> dict[str, Any] | JSONResponse:
    """Parse the body as a JSON object, else a precise 400. Hard byte cap
    (audit P1-3): the raw request.json() path buffered unbounded bodies."""
    from .routes_proxy import _json_body as _bounded_json_body

    try:
        body = await _bounded_json_body(request)
    except HTTPException as exc:
        return _error(exc.status_code, exc.detail)
    except Exception:
        return _error(400, "request body must be valid JSON")
    if not isinstance(body, dict):
        return _error(400, "body must be a JSON object")
    return body


def _flag_guard() -> JSONResponse | None:
    """404 when the flag is off — the surface must be INERT, not
    permission-denied (a 403 would advertise that the route exists)."""
    if not settings.client_tool_dispatch:
        return _error(404, "not found")
    return None


@router.post("/v1/dispatch/register")
async def register(request: Request, key: Any = fastapi.Depends(require_scope("proxy"))) -> JSONResponse:
    """The OFFER: register/refresh this key's local tool-server identity.

    Re-register is an UPDATE (latest wins): same client_id, refreshed
    TTL, replaced toolset. Any jobs queued against the previous offer are
    failed as errors — the client that re-registered is a new
    registration, and stale jobs must never dispatch into it.
    """
    if (resp := _flag_guard()) is not None:
        return resp
    body = await _json_body(request)  # type: ignore[arg-type]
    if isinstance(body, JSONResponse):
        return body
    tools, err = clientside.validate_offer_tools(body.get("tools"))
    if err:
        status = 413 if "exceeds" in err or "too many" in err else 400
        return _error(status, err)
    try:
        ttl_s = float(body.get("ttl_s") or 900)
    except (TypeError, ValueError):
        return _error(400, "ttl_s must be a number")
    if not math.isfinite(ttl_s):
        # NaN/inf survive the min/max clamp (NaN compares False) — a NaN
        # ttl produced a never-live offer AND a JSONResponse body with bare
        # `NaN`, invalid per RFC 8259 (audit finding 11).
        return _error(400, "ttl_s must be a finite number")
    offer = clientside.hub.offer(key.id, tools, ttl_s)
    return JSONResponse(
        {
            "client_id": offer.client_id,
            "expires_at": offer.expires_at,  # monotonic — opaque to callers, stable within a process
            "tools": sorted(offer.tools),
        }
    )


@router.get("/v1/dispatch/poll")
async def poll(request: Request, key: Any = fastapi.Depends(require_scope("proxy"))) -> JSONResponse:
    """The long-poll: next dispatch job for this key, or 204 after
    `wait_s`. The claim is the queue pop (dispatch-once); the deadline
    the client sees (`deadline_s`) is the timeout the relay side is
    waiting with — the client should answer before it or drop the job
    (a timed-out job is a tool error to the model, by design)."""
    if (resp := _flag_guard()) is not None:
        return resp
    raw_wait = request.query_params.get("wait_s")
    try:
        wait_s = min(max(float(raw_wait), 0.0), MAX_POLL_WAIT_S) if raw_wait else DEFAULT_POLL_WAIT_S
    except ValueError:
        return _error(400, "wait_s must be a number")
    if not math.isfinite(wait_s):
        # Audit finding 11: `?wait_s=nan` propagated through both clamps →
        # a NaN deadline → `monotonic() >= deadline` never True → the poll
        # looped forever (one pinned task per such GET, authenticated DoS).
        return _error(400, "wait_s must be a finite number")

    deadline = time.monotonic() + wait_s
    while True:
        job = clientside.hub.claim(key.id)
        if job is not None:
            pend = clientside.hub._pending.get(job.dispatch_id)
            remaining = max(1.0, (pend.deadline - time.monotonic())) if pend else 1.0
            return JSONResponse(
                {
                    "dispatch_id": job.dispatch_id,
                    "tool": job.tool,
                    "arguments": job.arguments,
                    "dispatched_at": job.dispatched_at,
                    "deadline_s": round(remaining, 3),
                }
            )
        if time.monotonic() >= deadline:
            return JSONResponse(status_code=204, content=None)
        # Reap expired bookkeeping while we wait (safety net for waiters
        # whose asyncio.wait_for raced the deadline).
        clientside.hub.reap_expired()
        await _sleep_bounded(0.1)


async def _sleep_bounded(s: float) -> None:
    """Checkpoint sleep for the long-poll loop (bounded, cancellable)."""
    await asyncio.sleep(s)


@router.post("/v1/dispatch/result")
async def result(request: Request, key: Any = fastapi.Depends(require_scope("proxy"))) -> JSONResponse:
    """The ANSWER: settle one dispatch. First answer wins — a second POST
    for the same dispatch_id is 409 and its payload is DROPPED (replay
    denial). ok=false is a legitimate answer: the client's local approval
    policy refused, and the refusal text becomes the model's role=tool
    error content so it can self-correct."""
    if (resp := _flag_guard()) is not None:
        return resp
    body = await _json_body(request)  # type: ignore[arg-type]
    if isinstance(body, JSONResponse):
        return body
    raw_len = request.headers.get("content-length")
    try:
        if raw_len and int(raw_len) > MAX_RESULT_BYTES:
            return _error(413, f"result body exceeds {MAX_RESULT_BYTES} bytes")
    except ValueError:
        return _error(400, "invalid content-length")
    dispatch_id = body.get("dispatch_id")
    if not isinstance(dispatch_id, str) or not dispatch_id:
        return _error(400, "dispatch_id is required")
    ok = body.get("ok")
    if not isinstance(ok, bool):
        return _error(400, "ok must be a boolean")
    payload = body.get("content") if ok else body.get("error")
    if ok and payload is None:
        payload = ""
    if payload is not None and not isinstance(payload, (str, int, float, bool, list, dict)):
        return _error(400, "content must be JSON")
    try:
        size = len(json.dumps(payload, default=str).encode())
    except (TypeError, ValueError):
        return _error(400, "content is not JSON-serializable")
    if size > MAX_RESULT_BYTES:
        return _error(413, f"result exceeds {MAX_RESULT_BYTES} bytes")
    settled = clientside.hub.settle_result(key.id, dispatch_id, ok, payload)
    if not settled:
        return _error(409, "unknown, expired, or already-settled dispatch_id")
    return JSONResponse({"settled": True})


@router.get("/v1/dispatch/offer")
async def get_offer(key: Any = fastapi.Depends(require_scope("proxy"))) -> JSONResponse:
    """Introspection: this key's live offer (the relay's catalog source;
    lets the client verify its own registration). 404 when no live offer."""
    if (resp := _flag_guard()) is not None:
        return resp
    offer = clientside.hub.offer_lookup(key.id)
    if offer is None:
        return _error(404, "no live client tool offer for this key")
    return JSONResponse(
        {
            "client_id": offer.client_id,
            "expires_at": offer.expires_at,
            "tools": [
                {
                    "name": t.name,
                    "description": t.description,
                    "inputSchema": t.input_schema,
                    **({"timeout_s": t.timeout_s} if t.timeout_s is not None else {}),
                }
                for t in sorted(offer.tools.values(), key=lambda t: t.name)
            ],
        }
    )


# (No exception-handler registration: the hub surfaces refusals as
# return values, and DispatchError exists for the executor layer — a bare
# 500 on a bug here is acceptable in the spike and visible in the logs.)
