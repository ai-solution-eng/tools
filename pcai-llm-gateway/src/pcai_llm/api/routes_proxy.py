"""Data-plane proxy routes: /v1/chat/completions, /v1/embeddings, /v1/models.

Design stance: this module is a TRANSPARENT proxy, not a reshaper. The
request body leaves exactly as it arrived and the upstream JSON comes back
exactly as it was received; our added value is the control plane around the
call — key auth, allowed-model enforcement, budgets, rate limits, endpoint
picking with a circuit breaker, and the immutable usage ledger (settle()).

Ledger discipline: `settle` is called EXACTLY once per admitted request.
Gate rejections (budget/rate/no-healthy-upstream) settle immediately with
zero tokens; proxied calls settle with real upstream usage when available
and a token estimate when not (an estimate beats no row — budgets are
ledger-derived, so a missing row silently under-charges).

Streaming: settle runs in the generator's `finally` block because a
StreamingResponse outlives the handler — the handler has already returned
by the time bytes flow, and a client disconnect aborts the generator
mid-iteration. `finally` is the only hook that fires on both the happy
path and disconnect/error paths, so it is the only place the ledger row is
guaranteed for streamed calls.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time
import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import UTC, datetime
from typing import Any, cast

import anyio
import fastapi
import httpx2
from fastapi import Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from ..config import clamp_max_tokens, settings
from ..core import agents as agents_mod
from ..core import (
    canonical,
    clientside,
    context_window,
    embedcache,
    guardrails,
    mcp_weights,
    model_fallbacks,
    model_routing,
    offpeak,
    registry,
    response_cache,
    title_requests,
    toolcompress,
    toolrelay,
    tracing,
    upstream_auth,
    wire_compat,
)

# TTFT / budget-warning / concurrency metrics (settle-side counters live in
# core.budgets so metric and ledger agree by construction). No-op shims when
# prometheus_client is absent.
from ..core import metrics as _metrics
from ..core.budgets import budget_status, check_budgets, check_rate_limit, settle
from ..core.cascade import (
    DEMAND_AWARE_ALIAS,
    classify_request,
    pool_has_type,
    pool_serves_chat,
    profile_order_for_key,
    resolve_cascade,
    serves_chat,
)
from ..core.keys import KeyRecord
from ..db import db
from ..routing.router import Endpoint, Pool
from ..routing.router import router as model_router
from .auth import require_scope

log = logging.getLogger(__name__)

router = fastapi.APIRouter(prefix="/v1", tags=["proxy"])

_RETRY_AFTER_RATE_LIMITED = "5"  # seconds; client should back off briefly


# --------------------------------------------------------------------------
# small shared helpers
# --------------------------------------------------------------------------


def _latency_ms(started: float) -> int:
    """Wall-clock latency in whole ms since `started` (a monotonic stamp)."""
    return int((time.monotonic() - started) * 1000)


def _span_user_id(key: Any) -> str:
    """The ledger owner's id, for the span's user.id attribute.

    str() because KeyRecord.user_id is a UUID and OTLP attributes are text;
    getattr keeps span building safe against the key stubs used in tests.
    Empty string when unknown — the attribute is then simply not emitted,
    rather than recording a misleading blank.
    """
    uid = getattr(key, "user_id", None)
    return str(uid) if uid else ""


def _relay_wants(header_value: str | None, key_default: bool, enabled: bool) -> bool:
    """Three-state tool-relay resolution (pure — unit-tested).

    The per-request header always wins: '1' forces the relay on, '0' opts
    out. An ABSENT header falls back to the key's relay_default (console:
    Keys ▸ edit — lets off-cluster consumers relay with zero client config).
    The global kill-switch (PCAI_LLM_TOOL_RELAY_ENABLED=false) wins over all.
    """
    if not enabled:
        return False
    hdr = (header_value or "").strip().lower()
    if hdr == "1":
        return True
    if hdr == "0":
        return False
    return bool(key_default)


def _stream_events_wants(header_value: str | None, key_default: bool) -> bool:
    """Three-state pcai-event resolution (RFC-PER-KEY-STREAM-EVENTS; pure —
    unit-tested). The per-request header always wins: '1' forces the
    in-stream {"pcai": …} telemetry events on, '0' strips them for a
    byte-clean OpenAI-spec stream (strict validating clients). An ABSENT
    header falls back to the key's stream_pcai_events default (console:
    Keys ▸ edit). NO global kill-switch: the events are the console's live
    telemetry, so unlike the relay there is no fleet-wide off — the key
    default and the header are the whole surface."""
    hdr = (header_value or "").strip().lower()
    if hdr == "1":
        return True
    if hdr == "0":
        return False
    return bool(key_default)


def _split_request_tools(raw_tools: Any) -> tuple[list[dict[str, Any]], list[Any]]:
    """Split the request's tools array into (mcp requests, client tools).

    OpenAI-idiomatic MCP opt-in (ported from hpe-llm-gateway): an entry of
    the form {"type": "mcp", "server"?: str, "require_approval"?:
    "never"|"always"} asks THIS gateway to either execute that server's
    tools in-cluster (never — the relay) or hand the tool calls back to the
    client for human approval (always). Everything else is a plain client
    tool forwarded upstream untouched. Raises ValueError on malformed mcp
    entries — the caller maps that to a 400 (fail loud: silently ignoring
    a tool request would look like a working integration).
    """
    if not isinstance(raw_tools, list):
        if raw_tools is None:
            return [], []
        raise ValueError("tools must be a list")
    mcp_reqs: list[dict[str, Any]] = []
    fn_tools: list[Any] = []
    for t in raw_tools:
        if isinstance(t, dict) and t.get("type") == "mcp":
            server = t.get("server")
            if server is not None and (not isinstance(server, str) or not server.strip()):
                raise ValueError("tools entry {type: mcp}: 'server' must be a non-empty string when present")
            approval = t.get("require_approval")
            if approval not in (None, "never", "always"):
                raise ValueError(
                    f"tools entry {{type: mcp}}: 'require_approval' must be 'never' or 'always' (got {approval!r})"
                )
            mcp_reqs.append(
                {"server": server.strip() if isinstance(server, str) else None, "require_approval": approval or "never"}
            )
        else:
            fn_tools.append(t)
    return mcp_reqs, fn_tools


def _assistant_message_texts(payload: dict[str, Any]) -> list[str]:
    """The assistant text of EVERY choice of a non-streaming response.

    Handles both the plain string content shape and the multimodal
    content-parts shape (only text parts carry policy-relevant content).
    Absent/empty choices contribute nothing. Never raises.

    WHY every choice (audit r5 #2b): a {"n": 2} request carries two SAMPLED
    answers. Reading only choices[0] meant the post_call guard judged one of
    them, and the masked rewrite then replaced that one — leaving the second
    sample's text verbatim in a perfectly well-formed JSON body.
    """
    out: list[str] = []
    choices = payload.get("choices") if isinstance(payload, dict) else None
    for choice in choices if isinstance(choices, list) else ():
        message = choice.get("message") if isinstance(choice, dict) else None
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, str):
            if content:
                out.append(content)
        elif isinstance(content, list):
            joined = "\n".join(
                p.get("text", "") for p in content if isinstance(p, dict) and isinstance(p.get("text"), str)
            )
            if joined:
                out.append(joined)
    return out


def _assistant_message_text(payload: dict[str, Any]) -> str:
    """The assistant text of a non-streaming chat response ("" when absent).

    EVERY choice, joined with a newline (audit r5 #2b) — the post_call guard
    must judge all sampled answers, not just the first. For the ordinary
    single-choice response this is byte-identical to the old read.
    """
    return "\n".join(_assistant_message_texts(payload))


def _mask_chat_body_choices(payload: dict[str, Any], guarded_text: str) -> bool:
    """Rewrite every sampled answer in a chat body with the MASKED text.

    ZC-3 shape, shared by the live forward path and the response-cache hit path
    (audit 2026-11-10 P2-1) so a cached answer is masked exactly like a fresh
    one: the answer becomes the masked text and the OTHER model-text channels
    (the reasoning spellings, tool_calls) are DROPPED — a masked turn delivers a
    clean answer, not executable calls. EVERY choice is rewritten (audit r5
    #2b): the guard input is the join of all sampled answers, so a mask that
    fired must land on all of them — rewriting only choices[0] shipped the
    masked answer in choice 0 next to the UNMASKED sample in choice 1.

    Returns False for an unusable body shape (the caller relays it unmodified
    rather than corrupting it).
    """
    try:
        for choice in payload["choices"]:
            message = choice.get("message")
            if not isinstance(message, dict):
                continue
            message["content"] = guarded_text
            for _rk in toolrelay.REASONING_KEYS:
                message.pop(_rk, None)
            message.pop("tool_calls", None)
            if choice.get("finish_reason") == "tool_calls":
                choice["finish_reason"] = "stop"
    except (KeyError, IndexError, TypeError, AttributeError):
        return False
    return True


def _sse_line_tokens(chunk: bytes) -> list[bytes]:
    """Every SSE line in one raw byte chunk, with the framing normalised.

    SSE (WHATWG) accepts CR, LF and CRLF as line terminators — an upstream
    that frames with bare CR is spec-legal. A splitter that only knows `\n`
    parses a CR-only stream as ONE line, json.loads fails on it, and the
    answer vanishes as a successful empty 200 (audit C-7: the toolrelay twin
    got this fix; these are the proxy's own scanners).
    """
    return re.split(rb"\r\n|\r|\n", chunk)


def _sse_text_of_payload(obj: Any) -> list[str]:
    """EVERY model-text channel carried by ONE parsed SSE chat chunk.

    This is the DROP predicate for the masked replay (audit ZC-3): content,
    all three reasoning spellings the relay itself recognises
    (toolrelay.REASONING_KEYS), tool-call argument fragments, and the pcai
    thinking/tool events. A chunk carrying text on ANY of these is dropped
    whole on the masked path — a fragment can never be re-masked safely
    (the guard saw the assembled text, not the fragment). pcai error
    envelopes carry no model text and are skipped.
    """
    texts: list[str] = []
    # EVERY choice, not choices[0] (audit r5 #2b): an upstream that packs
    # several sampled choices into ONE chunk (choices: [{index:0,...},
    # {index:1,...}]) carries model text under a non-zero index — reading
    # only choices[0] classified such a chunk text-free and replayed it
    # VERBATIM on the masked path, shipping the unmasked sample.
    choices = obj.get("choices") if isinstance(obj, dict) else None
    for choice in choices if isinstance(choices, list) else ():
        delta = choice.get("delta") if isinstance(choice, dict) else None
        if not isinstance(delta, dict):
            continue
        content = delta.get("content")
        if isinstance(content, str) and content:
            texts.append(content)
        for rk in toolrelay.REASONING_KEYS:
            rtxt = delta.get(rk)
            if isinstance(rtxt, str) and rtxt:
                texts.append(rtxt)
        for tc in delta.get("tool_calls") or []:
            fn = tc.get("function") if isinstance(tc, dict) else None
            args = fn.get("arguments") if isinstance(fn, dict) else None
            if isinstance(args, str) and args:
                texts.append(args)
    payload = obj.get("pcai")
    if isinstance(payload, dict):
        for field in ("text", "result", "args"):
            v = payload.get(field)
            if isinstance(v, str) and v:
                texts.append(v)
            elif isinstance(v, (dict, list)):
                # TYPE-BLIND, not str-only (audit r5 #2a): toolrelay emits
                # pcai.args as the PARSED dict (toolrelay.py:2625 does
                # args = json.loads(...), stamped at :2690/:2726), so an
                # ACL-denied tool_error event carries a dict and NO string
                # field at all — the str-only test called it text-free and
                # replayed the model's tool arguments verbatim on the masked
                # path. Serialize whatever the field holds: a container is
                # text the moment its bytes reach the wire.
                try:
                    rendered = json.dumps(v, default=str)
                except (TypeError, ValueError):
                    rendered = str(v)
                if rendered and rendered not in ("{}", "[]"):
                    texts.append(rendered)
    return texts


def _sse_answer_texts_of_payload(obj: Any) -> list[str]:
    """What the ASSISTANT SAID as its REPLY in one parsed SSE chat chunk —
    the post_call guard's input AND the only text the masked final ships.

    Content deltas ONLY, deliberately (xv round 2, findings 2/4): the
    consolidated masked final must carry the assistant ANSWER, not reasoning
    or tool JSON glued into delta.content, and tool-call arguments / tool
    results are caller data or tool output — a blocked keyword in a grep
    pattern or a tool's stdout must not discard an otherwise clean answer.
    Reasoning is safe to exclude from the VERDICT because the masked path
    drops every reasoning chunk wholesale: it can neither leak nor steer the
    verdict, exactly like pre-fix — the widened drop predicate is the fix.
    """
    # EVERY choice (audit r5 #2b): the guard must judge each sampled answer,
    # not just the first — otherwise a multi-choice chunk's non-zero-index
    # content is neither judged nor (see _sse_text_of_payload) classified.
    texts: list[str] = []
    choices = obj.get("choices") if isinstance(obj, dict) else None
    for choice in choices if isinstance(choices, list) else ():
        delta = choice.get("delta") if isinstance(choice, dict) else None
        if not isinstance(delta, dict):
            continue
        content = delta.get("content")
        if isinstance(content, str) and content:
            texts.append(content)
    return texts


# NOTE (audit r5 #2c): the per-CHUNK classifier that used to live here is
# gone on purpose. httpx2 hands over whatever the socket returned, so an SSE
# event can straddle two reads — a per-chunk view classifies both halves as
# text-free and the masked replay then ships them verbatim. The masked path
# and the guard input are both driven by the LINE CURSOR in _guarded_sse
# instead; do not reintroduce a per-read parser for either.


def _guard_body_size(request: Request) -> None:
    """Cheap PRE-READ rejection for declared Content-Length bodies (413).

    A declared oversized body is refused before a single byte is buffered.
    Chunked bodies carry no Content-Length and are bounded by the hard-cap
    read inside _json_body instead.
    """
    raw = request.headers.get("content-length")
    if raw is None:
        return
    try:
        declared = int(raw)
    except ValueError:
        return  # malformed header: let the body read surface it as 400
    if declared > settings.max_body_bytes:
        raise fastapi.HTTPException(
            413,
            f"request body too large: {declared} bytes exceeds limit {settings.max_body_bytes}",
        )


async def _json_body(request: Request) -> Any:
    """Read the request body with a HARD byte cap, then parse as JSON.

    WHY (security audit): the old path read only the declared Content-Length
    and then called request.json() — a chunked body (no Content-Length)
    buffered UNBOUNDED into memory. Reading through request.stream() into a
    bounded accumulator caps memory regardless of framing: 413 the moment
    the accumulator passes settings.max_body_bytes.
    """
    _guard_body_size(request)  # fast pre-read refusal for declared sizes
    buf = bytearray()
    async for chunk in request.stream():
        buf += chunk
        if len(buf) > settings.max_body_bytes:
            raise fastapi.HTTPException(
                413,
                f"request body too large: exceeds limit {settings.max_body_bytes}",
            )
    try:
        return json.loads(bytes(buf))
    except (ValueError, UnicodeDecodeError):
        raise fastapi.HTTPException(400, "request body is not valid JSON") from None


async def _raw_body(request: Request) -> bytes:
    """Read the request body as BYTES with the same hard cap as _json_body.

    The audio passthrough routes must forward whatever bytes arrived, audio
    included — a JSON round-trip would re-encode them (and base64 in a
    multipart `file` field would be impossible to reconstruct by parsing).
    Same framing-agnostic bound as _json_body: a declared oversize body
    413s pre-read, a chunked body is bounded as it accumulates.
    """
    _guard_body_size(request)
    buf = bytearray()
    async for chunk in request.stream():
        buf += chunk
        if len(buf) > settings.max_body_bytes:
            raise fastapi.HTTPException(
                413,
                f"request body too large: exceeds limit {settings.max_body_bytes}",
            )
    return bytes(buf)


def _check_model_allowed(key: KeyRecord, model: str) -> None:
    """Enforce the key's allowed-models list; empty list = all models."""
    if key.allowed_models and model not in key.allowed_models:
        raise fastapi.HTTPException(403, f"key is not allowed to use model {model!r}")


def _message_text_tokens(message: Any) -> int:
    """Text-only token estimate for ONE chat message (len//4 heuristic).

    WHY (security audit): the old inline `len(str(content)) // 4` stringified
    multimodal content-part lists, billing a base64 image payload as TEXT — a
    1.5 MB screenshot read as ~501K tokens and 429'd the key instantly. Only
    the "text" field of dict parts is counted; image_url / input_audio / file
    parts are ignored ENTIRELY (their URLs and base64 are not tokens). A
    plain-string content keeps the original len//4; None/missing/non-text
    content estimates 0 for the message. Never raises.

    NON-ASCII (audit 2026-11-10 P2-21): the charge is core.context_window's
    text_tokens, the SAME function the context-fit estimator uses — ASCII at
    len//4 (byte-identical to the shipped estimator) but at least ONE token per
    non-ASCII character. A bare len//4 here under-counted CJK/emoji ~4x, so the
    admission charge and the billing estimate simply did not bind for that
    traffic: the last audit fixed this defect class in context_window and left
    its hot-path twin behind.
    """
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return context_window.text_tokens(content)
    if isinstance(content, list):
        return sum(
            context_window.text_tokens(part["text"])
            for part in content
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        )
    return 0


def _estimate_tokens(body: dict[str, Any]) -> int:
    """Rough pre-call token estimate (len//4 heuristic + capped completion).

    Charges TPM/period-budget windows at ADMISSION for tokens not yet
    generated. The completion term must be a reasonable EXPECTATION, not the
    request's max_tokens cap: agentic configs set maxTokens 262144 while
    actual completions run in the hundreds — charging the cap made every
    request burn ~4x its real cost against the minute window and 429'd keys
    long before the true token count ("rate limits despite 1M TPM"). The
    ledger settle records the real usage afterward; the len//4 prompt
    heuristic stays deliberately rough.

    VALIDATION (security audit): this estimator is the fail-safe every
    caller relies on, so it validates defensively and NEVER raises, never
    returns a negative number:
      * max_tokens is coerced with int(); a non-numeric value (or NaN/inf)
        falls back to the 1024 expectation — a string used to raise
        ValueError -> 500 — and a negative value floors at 0: a negative
        estimate used to DECREMENT the key's TPM window (check_rate_limit
        adds it verbatim) and let `spent + est > limit` admit over-cap keys,
        i.e. a denial-of-wallet bypass.
      * the prompt term counts TEXT ONLY (see _message_text_tokens), so
        multimodal base64 payloads cannot inflate the admission estimate.
    """
    messages = body.get("messages")
    prompt = sum(_message_text_tokens(m) for m in messages) if isinstance(messages, (list, tuple)) else 0
    max_tokens = body.get("max_tokens")
    completion_est = 1024  # absent/falsy max_tokens keeps the default expectation
    if max_tokens:
        try:
            completion_est = max(0, min(int(max_tokens), 8192))  # negatives floor at 0
        except (TypeError, ValueError, OverflowError):
            completion_est = 1024  # non-numeric / NaN / inf -> the default expectation
    return max(0, prompt + completion_est)  # never negative: a negative would credit the window


def _estimate_embedding_tokens(body: dict[str, Any]) -> int:
    """Estimate tokens for an embeddings call from the raw input payload.

    Charges through context_window.text_tokens (ascii//4 + one per non-ASCII
    char) — the SAME estimator the admission path and the relay twin use, so
    a CJK/emoji embedding input cannot be under-counted ~4x against the TPM
    window while the chat path counts it correctly (audit 2026-11-10 P2-21
    fixed the chat path; these two surfaces were the documented twin left
    behind).
    """
    return context_window.text_tokens(str(body.get("input") or ""))


def _estimate_rerank_tokens(body: dict[str, Any]) -> int:
    """Estimate tokens for a rerank call: query + every document, len//4.

    Rerank bodies carry text (strings) or multimodal content dicts; the
    estimator only needs a rough SIZE, and str() of a content dict still
    scales with the text/base64 it holds. Never raises, never negative -
    same fail-safe contract as _estimate_tokens.
    """
    query = body.get("query")
    total = (
        context_window.text_tokens(query) if isinstance(query, str) else context_window.text_tokens(str(query or ""))
    )
    documents = body.get("documents")
    if isinstance(documents, (list, tuple)):
        for doc in documents:
            total += (
                context_window.text_tokens(doc) if isinstance(doc, str) else context_window.text_tokens(str(doc or ""))
            )
    elif isinstance(documents, str):
        total += context_window.text_tokens(documents)
    return max(0, total)


def _routing_available(
    key: KeyRecord,
    capacity: dict[str, dict] | None = None,
    agents: dict[str, Any] | None = None,
) -> Callable[[str], bool]:
    """Availability predicate for the complexity-router's ladder walk.

    A ladder model is servable for THIS key when it is registered (a pool
    exists), inside the key's allowed_models (the ACL is a hard boundary —
    a routing pseudo-name must never become an ACL bypass), has a healthy
    chat-typed endpoint, and — when `capacity` is supplied — would be
    ADMITTED by the budget gate for this request RIGHT NOW. `capacity` is
    budgets.model_capacity, which since 2026-11-03 mirrors check_budgets:
    per-key rows AND the team envelope (per-model caps, pooled outer bound,
    overflow swallow), charged with the request estimate. The router therefore
    never offers a model the admission gate would then 429 — including one the
    key's TEAM pool has no room for, which the per-key-only projection used to
    advertise. Built per request because it closes over the key;
    core.model_routing stays a pure decision module.

    WHY capacity belongs here and not only in the admission gate: the
    decision model is offered exactly this predicate's survivors as
    `{{model_list}}`, and the heuristic picks from the same window. Without
    it the router can be told a model is available, pick it, and then have
    admission 429 the request — the operator sees "routing chose a model I
    have no budget for". Filtering at the source makes model_list mean what
    it says: the models this key can actually use at this moment.

    `capacity` is the map from budgets.model_capacity (model -> {ok,
    remaining_tokens, blocked_by}). A missing entry means "not evaluated"
    and is treated as available: capacity is a FILTER, never a gate — the
    admission gate still enforces, and a capacity lookup fault must never
    silently empty the ladder (see model_capacity's fail-open contract).
    """

    cap = capacity or {}

    def _ok(name: str) -> bool:
        # MODEL GROUP (2026-11-08): a group is available when the key may use
        # it AND at least one member can serve right now — the same filters the
        # data plane applies (group ACL bridge, healthy chat pool, per-member
        # capacity). Naming the group grants its members; a member grant lets
        # the key use the group; a group never widens the ACL.
        group = _group_by_name(name)
        if group is not None:
            acl = _group_acl(group, key)
            for member in group.members:
                if acl is not None and member not in acl:
                    continue
                p = model_router.pool(member)
                if p is None or not p.healthy():
                    continue
                if not any(e.model_type == "chat" for e in p.healthy()):
                    continue
                state = cap.get(member)
                if state is not None and not state.get("ok", True):
                    continue
                return True
            return False
        # AGENT (2026-11-08): a hard model (surface + pinned model/cascade).
        # Available when it is OFFERED and its pinned model is available; an
        # agent with no pinned model rides the profile cascade, so it is usable
        # while any chat pool is.
        if agents and name in agents:
            agent = agents[name]
            if not getattr(agent, "offered", True):
                return False
            target = str(getattr(agent, "model", "") or "")
            if not target:
                return any(_ok(m) for m in model_router.models())
            if target == name:
                return False
            return _ok(target)
        if key.allowed_models and name not in key.allowed_models:
            return False
        p = model_router.pool(name)
        if p is None or not p.healthy():
            return False
        if not any(e.model_type == "chat" for e in p.healthy()):
            return False
        state = cap.get(name)
        return not (state is not None and not state.get("ok", True))

    return _ok


async def _routing_incumbent_state(key: Any, body: dict[str, Any]) -> tuple[str | None, str | None, Any]:
    """(incumbent, sticky_floor, sticky_writer) for one routed request.

    Extracted from the request path so the bootstrap chain is TESTABLE.
    It was inline, and that is why the same defect shipped twice: unit tests
    passed an incumbent in directly, so nothing exercised the code that
    PRODUCES one -- gating the writer behind sticky_routing left the incumbent
    permanently None while every test stayed green.

    Contract:
      * incumbent -- the conversation's current model, read UNCONDITIONALLY
        (hysteresis is a cost guard and must work with never-downgrade off).
      * sticky_floor -- the never-downgrade floor, opt-in via sticky_routing
        (it CHANGES which model serves, unlike the incumbent which only
        informs a cost decision).
      * writer -- built UNCONDITIONALLY whenever a conversation fingerprint
        exists. This function is the only producer of a routing-scoped
        sticky row; without the writer the incumbent can never exist.

    Fail-open: any fault returns (None, None, None) -- plain routing.
    """
    incumbent: str | None = None
    floor: str | None = None
    writer = None
    try:
        from ..core import routing_config as _rc
        from ..core import stickiness as _sticky

        cfg = await _rc.get_config()
        # NAMESPACED: model_stickiness is shared with the cascade's
        # load-aware head (auto-demand-aware), which keys on the SAME
        # conversation fingerprint. Without this prefix a cascade head
        # silently became routing's capability floor and pinned every
        # later routed turn to it (defect fix 2026-10-23).
        thread = _sticky.scoped_thread_key(
            canonical.thread_key(body.get("messages") or [], key.id),
            _sticky.SCOPE_ROUTING,
        )
        if not thread:
            return None, None, None
        sticky_on = bool(cfg.get("sticky_routing"))
        idle = int(cfg.get("sticky_routing_idle_reset_s") or 0)
        row = await _sticky.get_sticky_row(key.id, thread)
        if row is not None:
            model, age = row
            if idle > 0 and age > idle:
                # Idle reset: the thread went quiet long enough that the old
                # model says nothing about the new work -- neither a floor NOR
                # an incumbent worth defending.
                await _sticky.drop_sticky(key.id, thread)
            else:
                incumbent = model
                if sticky_on:
                    floor = model
        # UNCONDITIONAL (see the docstring): this is the only writer.
        # DEFERRED (audit 2026-10-29 P3, XV-3): the writer returns a finisher
        # instead of persisting, because this runs BEFORE the budget gates and
        # admission failover decide what actually serves. The caller commits
        # finisher(serving_model) once admission settles, so the never-
        # downgrade floor can never name a model that did not bill.
        writer = _sticky.make_writer(key.id, thread, defer=True)
    except Exception as exc:
        # WARNING without a traceback (audit w6, P2): this sits on the request
        # path, so a stickiness/DB blip printed a FULL TRACEBACK PER REQUEST --
        # a log flood at exactly the moment the DB is already unhappy. The
        # exception type keeps the fault identifiable; the request still
        # degrades to plain routing.
        log.warning("routing: incumbent/sticky lookup failed - plain routing (%s)", type(exc).__name__)
        return None, None, None
    return incumbent, floor, writer


def _decision_caller(
    decision_model: str,
    policy: dict[str, Any],
    agents: list[Any] | None = None,
    key_id: uuid.UUID | None = None,
    request_id: str = "",
) -> Callable[..., Any] | None:
    """Build the System One caller for the configured decision model.

    Returns None when the model is not registered or has no usable endpoint --
    the router then keeps its heuristics, which is the pre-decision-model
    behaviour. Returning None (rather than raising) is deliberate: a
    misconfigured decision model must cost a downgrade opportunity, never a
    request.

    The endpoint is resolved through the SAME registry pool every other
    upstream uses, so credentials follow the one documented order (panel
    Fernet token, then by-ref Secret env var) and the decision model is just
    another pool to the operator.

    P10 self-billing: when `key_id` (the CALLING key) is supplied, every
    decision call that reaches the upstream settles its OWN ledger row under
    that key with status='routing' — real upstream usage when the /systemone
    response reports it, otherwise the token estimate over the decision
    payload tagged unit_kind='token_estimate'. Gated by
    settings.routing_charge_enabled (default True); 'routing' is priced by
    settle() but kept OUT of _COSTED_STATUSES, so the row is chargeback-
    visible without consuming the caller's token/completion budget. A settle
    fault is non-fatal: billing the router must never cost the request.
    """
    from ..core import decision_routing

    pool = model_router.pool(decision_model)
    if pool is None or not pool.endpoints:
        return None
    # BREAKER (audit w6, P2): a cooling pool means "no usable decision
    # endpoint", and None is exactly this function's contract for that -- the
    # router then keeps its heuristics. The old `or pool.endpoints[0]` fallback
    # re-picked a COOLING endpoint on every request, so the breaker wired below
    # could never actually relieve a hung decision model.
    ep = pool.pick()
    if ep is None:
        # INFO, not WARNING: this fires once per routed request while the
        # breaker is open, and the trip itself is already visible (the
        # breaker_trip metric + Pool.report's last_error on /admin/endpoints).
        # A per-request WARNING here would be the log flood this wave removed.
        log.info("decision model %r has no healthy endpoint (breaker open) - heuristics stand", decision_model)
        return None
    base = (ep.base_url or "").rstrip("/")
    if not base:
        return None
    url = base + "/systemone"
    instructions = policy.get("prompt") or decision_routing.DEFAULT_INSTRUCTIONS

    async def _call(
        state: str,
        _instructions: str,
        candidates: list[str],
        timeout_ms: int,
        images: list[str] | None = None,
    ) -> Any:
        # The operator prompt, when set, REPLACES the default instructions;
        # the {{models}}/{{gpu_load}} placeholders are rendered by
        # routing_config so the same text the panel previews is the text the
        # model receives.
        from ..core import routing_config

        # ONE live snapshot feeds both channels: the instructions block (only
        # when the operator prompt still carries {{...}}) and the per-model
        # cards, which become the criteria descriptions. Reading it once is
        # also what keeps the two channels from disagreeing about the load.
        cfg = await routing_config.get_config()
        gpu = {**await routing_config.gpu_load_snapshot(), "inject": bool(cfg.get("inject_gpu_load"))}
        # MODEL GROUPS (2026-11-08): a group is a routing candidate like any
        # other hard model, so BOTH decision channels need it: the criteria card
        # describes the set (description + members), and the load row is the
        # LEAST-LOADED member's — the group's own pick targets the idle member —
        # with "active" true when any member is in low demand. A group with no
        # member reading renders "no data", never a fabricated zero.
        _cards = dict(policy.get("cards") or {})
        try:
            from ..core import model_groups as _mg

            _gpu_raw = gpu.get("models")
            _gpu_models: dict[str, Any] = _gpu_raw if isinstance(_gpu_raw, dict) else {}
            for _cand in candidates:
                _g = _mg.group_for(_cand)
                if _g is None:
                    continue
                if _cand not in _cards:
                    _desc = (_g.description or f"model group of {len(_g.members)} equivalent chat models").strip()
                    _cards[_cand] = {
                        "description": (
                            f"{_desc} | members: {', '.join(_g.members)} | the gateway picks the "
                            "least-loaded member and pins the conversation to it"
                        )
                    }
                _rows = [
                    _gpu_models[m]
                    for m in _g.members
                    if isinstance(_gpu_models.get(m), dict) and _gpu_models[m].get("value_pct") is not None
                ]
                if _rows:
                    _gpu_models[_cand] = {
                        "value_pct": min(float(r["value_pct"]) for r in _rows),
                        "active": any(r.get("active") for r in _rows),
                        "fraction": next((r.get("fraction") for r in _rows if r.get("fraction") is not None), None),
                    }
            # AGENTS (2026-11-08): a per-key agent is a hard model with a tool
            # surface. Describe it (surface + pinned model) and borrow its
            # pinned model's load row; a cascade agent (model='') has no single
            # window/load, so it renders "no data" honestly.
            for _agent in agents or []:
                _an = str(getattr(_agent, "name", "") or "")
                if not _an or _an not in candidates or _an in _cards:
                    continue
                _target = str(getattr(_agent, "model", "") or "")
                _servers = ", ".join(getattr(_agent, "servers", ()) or ()) or "none"
                _cards[_an] = {
                    "description": (
                        f"agent {_an}: {getattr(_agent, 'description', '') or 'narrow tool surface'}"
                        f" | model: {_target or 'auto (profile cascade)'} | servers: {_servers}"
                    )
                }
                if _target and isinstance(_gpu_models.get(_target), dict):
                    _gpu_models[_an] = _gpu_models[_target]
            gpu["models"] = _gpu_models
        except Exception:
            log.debug("decision model: group/agent criteria unavailable (fail-open)", exc_info=True)
        text = instructions
        if "{{" in text:
            # Honour the operator's inject_gpu_load flag, exactly as the panel
            # preview does (routes_admin.preview_routing_prompt). Forcing
            # inject=True here made the live call disagree with the preview: a
            # site with injection OFF still sent real GPU numbers, so the text
            # the operator approved was not the text the model received.
            text = routing_config.render_prompt(text, gpu=gpu, models=candidates, decision_model=decision_model)
        # The operator's max_prompt_chars is a COST bound on every decision
        # call, and it used to be enforced only in the preview -- so the panel
        # could show a truncated prompt while the live call shipped the whole
        # thing. Same cap, same truncation, one rule.
        try:
            _cap = max(200, int(cfg.get("max_prompt_chars") or 8000))
        except (TypeError, ValueError):
            _cap = 8000
        if len(text) > _cap:
            text = text[:_cap]
        # The CRITERIA channel: one description per candidate, built from that
        # model's card plus its own live row. This is the only channel whose
        # text is bound to an option label when the choice is scored, so it is
        # where a model's ROLE belongs -- a role list in the instructions made
        # the model map prose onto bare labels across channels. A model with no
        # card renders as its bare name (the pre-cards behaviour).
        # Bounded like the state path: a pricing fault costs a tie-break hint,
        # never the decision.
        try:
            from ..core import costs as _costs

            prices = await asyncio.wait_for(_costs.price_hints(list(candidates)), timeout=0.5)
        except Exception:  # never let pricing cost a decision
            prices = {}
        criteria = routing_config.render_criteria(
            _cards,
            list(candidates),
            gpu=gpu,
            costs=prices,
            modalities=policy.get("modalities") or None,
        )
        payload = {
            "model": decision_model,
            "state": state,
            "questions": {
                "answer": {
                    "type": "choice",
                    "instructions": text,
                    # criteria is {option: description}; a choice question
                    # needs >= 2 options, which the caller guarantees.
                    "criteria": criteria,
                }
            },
        }
        # Images so a vision-capable decision model can READ the request rather
        # than infer "there is an image" from a boolean. data: URLs pass
        # through untouched; a remote http(s) URL is passed as-is and the
        # endpoint fetches it (the model card documents both forms).
        if images:
            payload["images"] = list(images)
        headers = dict(_upstream_headers(ep, uuid.uuid4().hex[:12]))
        headers["Content-Type"] = "application/json"
        _t0 = time.monotonic()
        try:
            resp = await model_router.client.post(url, json=payload, headers=headers, timeout=timeout_ms / 1000.0)
        except Exception as exc:
            # BREAKER (audit w6, P2): every other upstream call in this file
            # reports to its pool, so a dead endpoint cools and stops being
            # picked. The decision endpoint had NO report at all, so a hung
            # decision model cost decision_timeout_ms on EVERY routed request,
            # forever. Timeout and transport failure both count.
            await pool.report(ep, False, error=type(exc).__name__)
            # httpx2's TimeoutException does NOT inherit builtin TimeoutError
            # (it is TransportError -> HTTPError), so decision_routing's
            # `except TimeoutError` would classify a real timeout as
            # call_failed. Translate here: decision_routing must stay
            # import-light (no httpx2 import), and this keeps the reason
            # taxonomy honest for the metric split above.
            if "imeout" in type(exc).__name__:
                raise TimeoutError(str(exc)) from exc
            raise
        if resp.status_code >= 400:
            # DIAGNOSTIC ONLY (2026-10-29). decision_routing collapses every
            # failure to "call failed (HTTPStatusError)", which is the whole
            # reason a decision endpoint returning 4xx/5xx was undiagnosable:
            # the status and the endpoint's own message were both discarded.
            # Seen live during the FP8 d3-turbo swap — 10 of them across two
            # pods — with no way to tell 401 from 422 from 503.
            log.warning(
                "decision model: %s/systemone -> %s body=%s",
                base,
                resp.status_code,
                resp.content[:400].decode("utf-8", "replace"),
            )
            await pool.report(ep, False, error=f"upstream status {resp.status_code}")
        resp.raise_for_status()
        await pool.report(ep, True)
        # P10 self-billing: the decision model's GPU time was previously
        # INVISIBLE (no settle, no ledger row) — real spend by the calling
        # key that no budget or chargeback line could see. Settle it here,
        # after the upstream was paid, guarded like every other settle-adjacent
        # nicety: a billing fault must never turn into a failed request.
        if key_id is not None and settings.routing_charge_enabled:
            try:
                _dusage: dict[str, Any] = {}
                try:
                    _raw = resp.json()
                    _raw_usage = _raw.get("usage") if isinstance(_raw, dict) else None
                    if isinstance(_raw_usage, dict):
                        _dusage = _raw_usage
                except ValueError:
                    pass  # non-JSON body: settle on the estimate
                _in = int(_dusage.get("input_tokens") or 0)
                _out = int(_dusage.get("output_tokens") or 0)
                _tag: str | None = None
                if not _in and not _out:
                    # No upstream usage report: the len//4 estimate over the
                    # DECISION payload (instructions + criteria + state).
                    _in = len(json.dumps(payload)) // 4
                    _tag = "token_estimate"
                await settle(
                    key_id,
                    decision_model,
                    "routing",
                    _in,
                    _out,
                    "routing",
                    int((time.monotonic() - _t0) * 1000),
                    request_id or uuid.uuid4().hex,
                    billable_units=float(_in) if _tag else None,
                    unit_kind=_tag,
                )
            except Exception:
                log.warning("decision model: routing self-settle failed (non-fatal)", exc_info=True)
        return resp.json()

    return _call


def _upstream_headers(ep: Endpoint, request_id: str) -> dict[str, str]:
    """Build upstream headers: request id always, bearer key if resolvable.

    Credential resolution ORDER (first hit wins):
    1. `auth_token_enc` — Fernet ciphertext from the admin panel, decrypted
       per call with PCAI_LLM_SECRETS_MASTER_KEY (never logged, never cached
       beyond the call).
    2. `api_key_secret` — BY-REFERENCE: a K8s Secret name whose contents the
       chart injects as env PCAI_LLM_UPSTREAM_KEY__<NAME_NORMALIZED>.
    Neither → unauthenticated call with a warning (the pool may still serve
    public models; the upstream 401 keeps misconfig visible).
    """
    # Resolution order lives in core.upstream_auth so context-window discovery
    # (which reaches the same /v1/models card through the same credential)
    # cannot drift from the forward path. This wrapper keeps the call sites and
    # the header contract unchanged.
    return upstream_auth.upstream_headers(ep, request_id)


def _log_request(
    key_prefix: str,
    model: str,
    ep: Endpoint | None,
    status: str,
    latency_ms: int,
    request_id: str,
) -> None:
    """One structured line per request. Never logs the bearer key."""
    log.info(
        "proxy request_id=%s key_prefix=%s model=%s endpoint=%s status=%s latency_ms=%d",
        request_id,
        key_prefix,
        model,
        ep.id if ep is not None else "-",
        status,
        latency_ms,
    )


def _is_2xx(status_code: int) -> bool:
    return 200 <= status_code < 300


# Explicit (lowercased) body markers that make a 400 attributable to the
# MODEL the upstream was asked for — not to the caller's request shape.
# "does not exist" is NOT one of them (audit 2026-11-10 P2-20): on its own it
# also matches ordinary field-validation errors ("the field 'temperature' does
# not exist"), which EVERY endpoint of the deployment reproduces — see
# _MODEL_ABSENT_RE, which requires a model noun beside it.
_MODEL_400_MARKERS = (
    "model not found",
    "unknown model",
    "context length",
    "maximum context",
    "message role",
    "unsupported role",
)

# A model NOUN adjacent to an absence marker. "does not exist" alone is not
# evidence: a shape error naming a FIELD is a 400 on every endpoint, so
# re-rolling it spends a second GPU call to reproduce the same refusal. The
# gap is bounded and stops at a sentence break so the noun and the marker must
# belong to the same clause.
_MODEL_ABSENT_RE = re.compile(
    r"\b(?:model|models|engine|deployment|checkpoint)\b[^.\n]{0,60}\bdoes\s+not\s+exist\b"
    r"|\bdoes\s+not\s+exist\b[^.\n]{0,60}\b(?:model|models|engine|deployment|checkpoint)\b"
)

# How far into a 400 body the bare model NAME counts as attribution. An
# upstream that echoes the request payload puts the name in every shape error,
# so an unbounded substring test turned a client-shape 400 into a full
# fallback-chain re-roll. The head of the body is where the error MESSAGE
# lives; a single-QUOTED occurrence is exempt at any offset because prose
# quoting the name is itself attribution (JSON double-quotes are deliberately
# NOT exempt — a payload echo uses them).
_MODEL_NAME_WINDOW = 200


def _is_model_attributable_4xx(status: int, body: bytes, model: str) -> bool:
    """Could a DIFFERENT model from the fallback chain serve this request?

    The gateway's default is "4xx = the caller's/request's fault — never
    re-rolled". That is right for client-shape errors (a malformed payload
    400s on EVERY endpoint, so re-rolling only spends a second GPU call to
    reproduce the same error) — and wrong when the UPSTREAM is rejecting the
    MODEL itself: a 404 model-not-found, or a 400 whose body names the model
    / context length / message role, where another model can legitimately
    succeed. This helper is the ONE predicate both failover sites
    (_proxy_json and the streaming path) use to decide that.

    NARROW BY DESIGN:
      * 404 -> eligible. The call path is fixed (/chat/completions, built by
        the gateway), so a 404 there is the upstream's "no such model", not
        a route miss.
      * 400 -> eligible ONLY when the lowercased body matches an explicit
        marker (model not found / unknown model / context length / maximum
        context / message role / unsupported role), or names the model
        ABSENTLY ("model ... does not exist" — a bare "does not exist" is a
        field-validation error, not a model one), or QUOTES the model name the
        gateway sent near the top of the body (a strict upstream's rejection
        message names the model even when its phrasing misses every marker).
        A generic validation 400 never matches.
      * 401/403/429 and every other status -> NOT eligible: credentials,
        authorization and rate limits are identical on every endpoint of the
        same deployment, so a second attempt reproduces the same refusal.
    Pure by contract (no router/DB/IO) — table-testable, zero hot-path cost.
    """
    if status == 404:
        return True
    if status != 400:
        return False
    text = body.decode("utf-8", errors="replace").lower()
    if any(marker in text for marker in _MODEL_400_MARKERS):
        return True
    if _MODEL_ABSENT_RE.search(text):
        return True
    if not model:
        return False
    name = model.lower()
    return name in text[:_MODEL_NAME_WINDOW] or f"'{name}'" in text


def _context_limit_for(ep: Endpoint | None) -> int | None:
    """The context window learned for THIS upstream; None when not known yet.

    READ-ONLY on purpose. Discovery happens in registry.refresh()
    (core.context_window.refresh_windows), because the window is a property of
    the endpoint set and because probing here made every request an extra
    upstream call — which a transparent proxy must never do, and which silently
    broke every test that counts upstream calls.

    None means "no opinion": the static ceiling in settings.max_tokens_ceiling
    still applies, exactly as before this feature existed.
    """
    if ep is None or not ep.base_url:
        return None
    return context_window.cached(ep.base_url)


def _pool_context_limit(model_name: str) -> int | None:
    """Smallest KNOWN window across a model's endpoints; None = all unknown.

    Smallest, not first: a pool is a set of interchangeable servers, and sizing
    a request for the biggest one sends it to whichever member has capacity —
    including one that cannot hold it.
    """
    group = _group_by_name(model_name)
    if group is not None:
        # A GROUP is sized by its SMALLEST member: the pick may land on any
        # member, so advertising (or context-gating on) the biggest would be
        # the same 1M lie /v1/models refuses for an alias.
        windows = [w for m in group.members if (w := _pool_context_limit(m)) is not None]
        return min(windows) if windows else None
    pool = model_router.pool(model_name)
    if pool is None or not pool.endpoints:
        return None
    known = [w for ep in pool.endpoints if (w := context_window.cached(ep.base_url)) is not None]
    return min(known) if known else None


def _context_gated(
    base: Callable[[str], bool],
    body: dict[str, Any],
    agents: dict[str, Any] | None = None,
) -> Callable[[str], bool]:
    """Wrap a ladder predicate with "this model's window can hold the prompt".

    WHY (2026-10-29): the ladder filtered candidates by ACL, health and budget
    but never by TOKEN CAPACITY, so nothing stopped it offering a 262144-window
    model a request whose PROMPT already exceeded that — the one overflow no
    completion cap can rescue. Measured on the same ladder: the decision model
    picked Qwen3.8-27B (confidence 0.68) for a 100461-token prompt, which fits
    only after the cap is solved (see config.clamp_max_tokens); a session that
    grows past 262144 does not fit at all, and there the model must leave the
    ladder instead of absorbing the request and 400ing.

    Fail-open by construction: an unknown window (fresh pod, endpoint that
    advertises nothing) gates nothing, so this can only ever remove a model the
    gateway can PROVE cannot serve the request.
    """
    estimate = context_window.estimate_context_tokens(body)

    def predicate(model_name: str) -> bool:
        if not base(model_name):
            return False
        # AGENT (2026-11-08): the window is the pinned model's (a group's is
        # its smallest member's); an agent on the cascade has no single window,
        # so the base predicate's answer stands.
        if agents and model_name in agents:
            target = str(getattr(agents[model_name], "model", "") or "")
            if not target:
                return True
            return context_window.fits(context_limit=_pool_context_limit(target), est_prompt=estimate)
        return context_window.fits(context_limit=_pool_context_limit(model_name), est_prompt=estimate)

    return predicate


def _advertised_caps(models: list[str]) -> dict[str, int]:
    """context_length / max_tokens for one /v1/models entry. Empty when unknown.

    Advertised so a client does not have to hardcode what the upstream already
    publishes (DSH's model discovery reads exactly these two field names):
    `context_length` is the smallest KNOWN window across `models`, which for an
    alias means the smallest window ANY model the alias may pick can hold —
    advertising the biggest would be the same 1M lie that hid the Qwen 400.

    Omitted entirely when nothing is known: absent is honest, a guess is not.
    """
    windows = [w for m in models if (w := _pool_context_limit(m)) is not None]
    if not windows:
        return {}
    window = min(windows)
    ceilings = [c for m in models if isinstance(c := settings.max_tokens_ceiling.get(m), int) and c > 0]
    return {"context_length": window, "max_tokens": min(ceilings) if ceilings else window}


@dataclass
class _GateOutcome:
    """Result of the pre-call gate chain: budget warning level for the
    response header plus the rate-limit counters to surface as
    X-RateLimit-* headers (gaps P9: SDK clients back off natively on them)."""

    warn_level: int = 0
    rate_headers: dict[str, str] = dataclass_field(default_factory=dict)


async def _run_gates(
    key: KeyRecord,
    model: str,
    est_tokens: int,
    endpoint: str,
    request_id: str,
    started: float,
    precomputed_budget: Any = None,
) -> _GateOutcome:
    """Budget, rate-limit, and concurrency gates; on refusal, settle + raise.

    Budgets run first (a monthly spend cap should stop the call even if the
    per-minute window has room); rate limiting then increments the atomic
    fixed-window counter; concurrency last (cheapest check that can still
    reject). Returns the budget warning level (0/80/95) for the response
    header — pure visibility, never a refusal — and the remaining
    requests/tokens counters for the X-RateLimit-* headers.

    `precomputed_budget` (perf audit): the cascade ALREADY evaluated
    check_budgets for the winning model a moment ago — passing its
    GateResult here skips the redundant recheck (and its round-trips) on
    the pinned path. resolve_cascade guarantees it is the gate for THIS
    key + model pair.
    """
    outcome = _GateOutcome()
    budget = precomputed_budget if precomputed_budget is not None else await check_budgets(key.id, model, est_tokens)
    if not budget.ok:
        await settle(
            key.id,
            model,
            endpoint,
            0,
            0,
            "budget_exceeded",
            _latency_ms(started),
            request_id,
        )
        raise fastapi.HTTPException(429, budget.reason)
    rate = await check_rate_limit(key.id, key.rate_limit_rpm, key.rate_limit_tpm, est_tokens, request_id, model)
    if not rate.ok:
        await settle(
            key.id,
            model,
            endpoint,
            0,
            0,
            "rate_limited",
            _latency_ms(started),
            request_id,
        )
        raise fastapi.HTTPException(429, rate.reason, headers={"Retry-After": _RETRY_AFTER_RATE_LIMITED})
    if rate.remaining_requests is not None:
        outcome.rate_headers["X-RateLimit-Remaining-Requests"] = str(rate.remaining_requests)
    if rate.remaining_tokens is not None:
        outcome.rate_headers["X-RateLimit-Remaining-Tokens"] = str(rate.remaining_tokens)
    # Concurrency backpressure (NOT a fallback — there is no "next" here):
    # RPM/TPM bound request *rate*; this bounds simultaneous in-flight
    # requests so a low-RPM key cannot hold many long-lived streams open.
    if key.max_concurrent > 0:
        # .get (not []): a key's FIRST request has no counter entry yet —
        # direct indexing 500'd exactly that request (audit finding).
        in_flight = _in_flight.get(key.id, 0)
        if in_flight >= key.max_concurrent:
            # settle() below stamps the rate_limited LEDGER row; the scope
            # label distinguishes the concurrency axis from rpm/tpm.
            _metrics.RATE_LIMITED.labels(scope="concurrency").inc()
            await settle(
                key.id,
                model,
                endpoint,
                0,
                0,
                "rate_limited",
                _latency_ms(started),
                request_id,
            )
            raise fastapi.HTTPException(
                429,
                f"concurrency limit {key.max_concurrent} exceeded ({in_flight} in flight)",
                headers={"Retry-After": "2"},
            )
        _in_flight[key.id] = in_flight + 1
    # Budget-soft-threshold counter: X-Budget-Warning is in-band only (an
    # agent must poll headers to see it); the Prometheus counter is the
    # push-side twin so fleet dashboards can chart tenants nearing caps.
    # Counted at GATE time (not header-emit time) so both stream and JSON
    # paths count once, where the threshold was actually crossed.
    if budget.warn:
        _metrics.budget_warning(budget.warn)
    outcome.warn_level = budget.warn
    return outcome


# In-flight counters per key (in-memory: single-process-per-pod; the
# concurrent-cap value lives in the DB, the live count is process state).
_in_flight: dict[Any, int] = {}

# Relay tool-catalog cache (server_name -> (monotonic_ts, tools)). The
# preflight MCP fan-out ran per request; catalogs change on operator action,
# not per request. TTL + stale-on-error; cleared implicitly by time.
_CATALOG_TTL_S = 120.0
_catalog_cache: dict[str, tuple[float, list[dict[str, str]]]] = {}


def catalog_cache_clear() -> None:
    """Drop the relay catalog cache (registry refresh; tests)."""
    _catalog_cache.clear()


def _release_in_flight(key_id: Any) -> None:
    """Decrement the in-flight counter; floor at 0 (defensive against
    double-release in odd error paths). The entry is popped at zero so
    the dict does not accumulate one entry per key id forever."""
    remaining = max(0, _in_flight.get(key_id, 0) - 1)
    if remaining:
        _in_flight[key_id] = remaining
    else:
        _in_flight.pop(key_id, None)


def _once(fn: Callable[[], None]) -> Callable[[], None]:
    """Wrap a release callback so it runs AT MOST ONCE.

    The concurrency slot has exactly ONE owner at a time but SEVERAL possible
    release sites: the handler's error guard, _stream_chat's own 502 path, the
    stream generator's finally, the post-call guard wrapper's replay tail. A
    naive "add a release everywhere it can leak" is WRONG for this counter — it
    is per-KEY, not per-request, so a second release decrements a CONCURRENT
    request's slot and the cap silently stops binding. The once-wrapper is what
    makes every one of those sites safe to guard, which is why the P1 slot-leak
    fix is a pair (idempotent release + wrap the whole post-gate body).
    """
    done = False

    def _release() -> None:
        nonlocal done
        if done:
            return
        done = True
        fn()

    return _release


# --------------------------------------------------------------------------
# non-streaming forwarding (shared by chat + embeddings)
# --------------------------------------------------------------------------


async def _proxy_json(
    pool: Pool,
    ep: Endpoint,
    url: str,
    body: dict[str, Any],
    headers: dict[str, str],
    key: KeyRecord,
    model: str,
    endpoint: str,
    est_tokens: int,
    request_id: str,
    started: float,
    cache_input: str | None = None,
    pre_events: list[dict[str, Any]] | None = None,
    post_guards: list[Any] | None = None,
    route: model_routing.RouteDecision | None = None,
    fallback_chain: list[str] | None = None,
    fallback_from: str = "",
    want_metadata: bool = False,
    _is_zero_retry: bool = False,
    cache_hash: str | None = None,
) -> Response:
    """Forward one non-streaming call, settle the ledger, relay bytes as-is.

    `cache_input` set (embeddings path only) → a successful 2xx response is
    stored in the embedding cache for future zero-cost hits. Best-effort:
    storage failure never affects the caller.

    `post_guards` set (chat path only) → the assistant text is judged after
    the upstream answer: blocked answers settle 'guardrail_blocked' with the
    REAL token spend (the upstream call happened — the ledger must see it)
    and return a 400 error envelope; masked answers are rewritten in the
    relayed body and flagged with X-PCAI-Guardrail-Modified.

    `route` set (chat path only) → the X-PCAI-Route header announces the
    routing decision and the PRIMARY settle row carries the routing_* audit
    columns (shadow: would-serve; active: the actual served model).

    `fallback_chain` (review §4a): admin-curated per-model fallback names,
    tried in order when the primary cannot serve — an upstream transport
    error or a 5xx (post pool.report; the caller handles pool.pick()==None
    before ever getting here). Each hop is re-gated (registered +
    key-allowed + healthy chat pool + fresh check_budgets headroom — a
    fallback is NEVER a budget bypass) and settles under its ACTUAL model;
    X-PCAI-Fallback names the model failed over FROM. Only chat attempts
    walk a chain (embeddings have no chat pools to fall to).

    `want_metadata` (review §4b, X-PCAI-Metadata: 1): a 2xx JSON body gains
    the `"pcai": {...}` block (served model, endpoint, cost, cached_tokens).
    Body mutation is strictly opt-in; the module stays a transparent proxy
    by default.
    """
    off_base = offpeak.key_fraction(key)  # profile fraction ?? env default
    chain = list(fallback_chain or [])
    hdr_from = fallback_from
    cur_pool, cur_ep, cur_url, cur_headers = pool, ep, url, headers
    cur_model = model

    while True:
        # Per-model max_tokens ceiling (settings.max_tokens_ceiling): a model
        # whose upstream context cannot satisfy the client's cap 400s the
        # whole request. Clamp what we SEND — `cur_model` is the model
        # actually serving THIS attempt (a failover hop gets ITS ceiling, not
        # the requested model's). `body` itself is untouched (settle/estimate
        # read it), and the helper is zero-copy when no clamp applies.
        send_body = clamp_max_tokens(
            # A role the upstream rejects must never reach the wire: SGLang's
            # Qwen templates answer 400 "Unexpected message role" for OpenAI's
            # `developer` role, and only the gateway knows the model it is
            # about to call (see core.wire_compat).
            wire_compat.normalize_system_messages(body),
            cur_model,
            settings.max_tokens_ceiling,
            # The window THIS attempt's upstream publishes. None until registry
            # discovery has learned it, which is why the static ceiling above is
            # still honoured (and still the only guard on a cold pod).
            context_limit=_context_limit_for(cur_ep),
        )
        # C-6 (audit 2026-10-29): the wire body names the model serving THIS
        # attempt — body["model"] is the CLIENT's name (a pin or an alias)
        # and used to survive every fallback hop verbatim, so a strict
        # upstream 400s the hop AFTER the chain entry was consumed (failover
        # became a hard failure) and a permissive one silently served
        # something other than what the ledger and X-PCAI-Served-Model claim.
        if send_body.get("model") != cur_model:
            send_body = {**send_body, "model": cur_model}
        try:
            resp = await model_router.client.post(cur_url, json=send_body, headers=cur_headers)
        except httpx2.HTTPError:
            # Connection refused / timeout / TLS: a breaker-worthy endpoint
            # failure, not a client error.
            latency = _latency_ms(started)
            await cur_pool.report(cur_ep, False, error="HTTPError")
            await settle(key.id, cur_model, endpoint, 0, 0, "error", latency, request_id)
            _log_request(key.key_prefix, cur_model, cur_ep, "error", latency, request_id)
            nxt, chain = await _next_fallback_candidate(
                chain, cur_model, key, endpoint, est_tokens, request_id, started
            )
            if nxt is None:
                raise fastapi.HTTPException(502, "upstream request failed") from None
            cur_pool, cur_ep, cur_url, cur_headers = nxt
            hdr_from = hdr_from or cur_model
            cur_model = cur_ep.model_name
            continue

        latency = _latency_ms(started)
        if not _is_2xx(resp.status_code):
            # Transparent proxy: relay the upstream error body and status code
            # untouched so client SDKs can react to the real upstream error —
            # except on a failover-eligible status with chain left: the
            # breaker report + error settle happen exactly as before (this
            # attempt settled under ITS actual model), then the walk
            # continues. Eligible = a 5xx (upstream fault), OR a 4xx the
            # helper attributes to the MODEL itself (a different model can
            # legitimately serve it — _is_model_attributable_4xx). Every
            # other 4xx is the caller's/request's fault — never re-rolled.
            await cur_pool.report(cur_ep, False, error=f"upstream status {resp.status_code}")
            await settle(key.id, cur_model, endpoint, 0, 0, "error", latency, request_id)
            _log_request(key.key_prefix, cur_model, cur_ep, "error", latency, request_id)
            err_resp = Response(
                content=resp.content,
                status_code=resp.status_code,
                media_type=resp.headers.get("content-type", "application/json"),
                headers=_relay_headers(
                    cur_headers,
                    request_id,
                    offpeak.effective_for(cur_model, off_base),
                    model_routing.route_header(route, cur_model),
                    served_model=cur_model,
                    endpoint_id=cur_ep.id,
                    fallback_from=hdr_from,
                ),
            )
            if (
                not (
                    (500 <= resp.status_code < 600)
                    or _is_model_attributable_4xx(resp.status_code, resp.content, cur_model)
                )
                and chain
            ):
                return err_resp
            nxt, chain = await _next_fallback_candidate(
                chain, cur_model, key, endpoint, est_tokens, request_id, started
            )
            if nxt is None:
                return err_resp
            cur_pool, cur_ep, cur_url, cur_headers = nxt
            hdr_from = hdr_from or cur_model
            cur_model = cur_ep.model_name
            continue

        await cur_pool.report(cur_ep, True)
        embedding_json: dict[str, Any] = {}
        try:
            embedding_json = resp.json()
        except ValueError:
            pass  # non-JSON 2xx body: settle on the estimate
        usage = embedding_json.get("usage") or {}
        prompt_tokens = int(usage.get("prompt_tokens") or 0)
        if not prompt_tokens and endpoint != "chat":
            # Embeddings/rerank servers (vLLM) report total_tokens only, and
            # on those surfaces the total IS the prompt count - there is no
            # generated completion to subtract. Chat is excluded: a chat
            # server that omitted prompt_tokens would otherwise bill the
            # completion as prompt, so it keeps the estimate fallback.
            prompt_tokens = int(usage.get("total_tokens") or 0)
        # P10 honesty tag: an embeddings/rerank row whose numbers came from
        # the len//4 ESTIMATE (the upstream reported no usage at all) settles
        # tagged unit_kind='token_estimate' — chargeback can then tell a
        # measured row from a proxied one. Real upstream usage keeps the
        # legacy untagged shape (NULL unit_kind = measured as always).
        _usage_estimated = endpoint in ("embeddings", "rerank") and not (
            usage.get("prompt_tokens") or usage.get("total_tokens") or usage.get("completion_tokens")
        )
        prompt_tokens = prompt_tokens or est_tokens
        completion_tokens = int(usage.get("completion_tokens") or 0)
        # Usage-detail passthrough (the plain-path twin of toolrelay._cached_tokens):
        # vLLM/SGLang report upstream prompt-cache hits as
        # prompt_tokens_details.cached_tokens — flattened away before 0.9.3, so
        # non-streaming ledger rows read cached_tokens=0 even when the upstream
        # prefilled from cache. Anything absent/odd is 0 (no fabricated facts).
        _details = usage.get("prompt_tokens_details")
        cached_tokens = int(_details.get("cached_tokens") or 0) if isinstance(_details, dict) else 0

        # --- OTLP: one generation span per real model call -------------------
        # Emitted AFTER usage is known and BEFORE the guardrail branches, so a
        # blocked or masked answer still records the tokens it actually spent.
        # Metadata only — model, token counts, latency, status; never the prompt
        # or the completion. No-op when tracing is off (the default).
        # start_ns is backdated so the span carries the real call duration
        # rather than the instant the payload was built.
        _span_attrs: dict[str, Any] = {
            "langfuse.observation.type": "generation",
            "langfuse.trace.name": endpoint,
            "langfuse.observation.model.name": cur_model,
            "gen_ai.operation.name": endpoint,
            "gen_ai.request.model": cur_model,
            "gen_ai.usage.input_tokens": prompt_tokens,
            "gen_ai.usage.output_tokens": completion_tokens,
            "gen_ai.response.id": request_id,
            "duration_ms": latency,
        }
        # user.id is the ledger owner, not a name or email: a pseudonymous UUID
        # that lets Langfuse aggregate cost per user. Omitted entirely when
        # unknown so no blank user rows are created.
        _uid = _span_user_id(key)
        if _uid:
            _span_attrs["user.id"] = _uid
        tracing.start_span(
            "chat",
            _span_attrs,
            start_ns=time.time_ns() - latency * 1_000_000,
        ).end()

        # --- post-call guardrails (chat only): judge the assistant text -------
        guard_all: list[dict[str, Any]] = list(pre_events or [])
        out_content: bytes = resp.content
        out_headers = _relay_headers(
            cur_headers,
            request_id,
            offpeak.effective_for(cur_model, off_base),
            model_routing.route_header(route, cur_model),
            served_model=cur_model,
            endpoint_id=cur_ep.id,
            fallback_from=hdr_from,
        )
        if post_guards and endpoint == "chat":
            text = _assistant_message_text(embedding_json)
            try:
                guarded_text, post_events, blocked = await guardrails.run_post_call(
                    post_guards, text, model_router.client
                )
            except Exception as exc:
                # fail-open with visibility: a broken policy engine must not eat
                # the answer (same posture as the webhook engine itself)
                log.warning("guardrails: post_call evaluation failed: %s", exc)
                guarded_text, post_events, blocked = text, [], False
            post_dicts = [ev.as_dict() for ev in post_events]
            guard_all.extend(post_dicts)
            if blocked:
                await settle(
                    key.id,
                    cur_model,
                    endpoint,
                    prompt_tokens,
                    completion_tokens,
                    "guardrail_blocked",
                    latency,
                    request_id,
                    cached_tokens=cached_tokens,
                    billing_fraction=offpeak.effective_for(cur_model, off_base),
                    guardrail_events=guard_all or None,
                )
                _log_request(key.key_prefix, cur_model, cur_ep, "guardrail_blocked", latency, request_id)
                first = post_events[0] if post_events else None
                message = f"blocked by guardrail '{first.guardrail if first else 'post_call'}'"
                if first is not None and first.detail:
                    message += f": {first.detail}"
                return JSONResponse(
                    status_code=400,
                    content={
                        "error": {
                            "message": message,
                            "type": "guardrail_blocked",
                            "param": "",
                            "code": "guardrail_blocked",
                        }
                    },
                    headers={"X-Request-ID": request_id},
                )
            # ZC-3 on the non-streaming surface (xv round 2, finding 3), shaped
            # EXACTLY like the stream's masked path — see
            # _mask_chat_body_choices, which the response-cache hit path shares
            # (audit 2026-11-10 P2-1) so a cached answer is masked identically
            # to a fresh one.
            if guarded_text != text and embedding_json and _mask_chat_body_choices(embedding_json, guarded_text):
                masked_by = sorted({d["guardrail"] for d in post_dicts if d.get("action") == "masked"})
                if masked_by:
                    out_headers["X-PCAI-Guardrail-Modified"] = ",".join(masked_by)
                out_content = json.dumps(embedding_json).encode()

        # --- zero-completion insurance (review §4c, flag-gated) ---------------
        # 2xx + EMPTY assistant text (both content shapes) + zero completion
        # tokens + finish_reason=='stop' is the "model burned GPU time and
        # answered nothing" shape: ONE identical retry (never streamed, never
        # chained — the retry is the last move). BOTH attempts settle under
        # the shared request_id (the first was real spend); the retry's
        # guardrails re-run on its own answer. `_is_zero_retry` stops the
        # recursion — exactly one retry ever (duplicate-GPU-spend discipline,
        # same argument as the relay's bounded-call retry).
        if (
            settings.zero_completion_retry
            and not _is_zero_retry
            and endpoint == "chat"
            and prompt_tokens > 0
            and completion_tokens == 0
            and _assistant_message_text(embedding_json) == ""
            and _finish_reason_of(embedding_json) == "stop"
        ):
            # The FIRST attempt was real GPU spend: settle it (status ok, real
            # observed usage) BEFORE the retry — both attempts appear in the
            # ledger under the SHARED request_id (correlation + honest spend).
            await settle(
                key.id,
                cur_model,
                endpoint,
                prompt_tokens,
                completion_tokens,
                "ok",
                latency,
                request_id,
                cached_tokens=cached_tokens,
                billing_fraction=offpeak.effective_for(cur_model, off_base),
                guardrail_events=guard_all or None,
                **model_routing.ledger_stamp(route if cur_model == model else None, cur_model),
            )
            _log_request(key.key_prefix, cur_model, cur_ep, "zero_completion_retry", latency, request_id)
            retry_started = time.monotonic()
            return await _proxy_json(
                cur_pool,
                cur_ep,
                cur_url,
                body,
                cur_headers,
                key,
                cur_model,
                endpoint,
                est_tokens,
                request_id,
                retry_started,
                cache_input=None,
                pre_events=pre_events,
                post_guards=post_guards,
                route=None,
                fallback_chain=[],
                fallback_from=hdr_from,
                want_metadata=want_metadata,
                _is_zero_retry=True,
            )

        await settle(
            key.id,
            cur_model,
            endpoint,
            prompt_tokens,
            completion_tokens,
            "ok",
            latency,
            request_id,
            cached_tokens=cached_tokens,
            billing_fraction=offpeak.effective_for(cur_model, off_base),
            guardrail_events=guard_all or None,
            # P10: the estimate tag (None when the upstream measured usage —
            # the legacy shape) with the row's own token count as the
            # provenance quantity. Never raised on the chat path: chat rows
            # stay byte-identical.
            billable_units=float(prompt_tokens + completion_tokens) if _usage_estimated else None,
            unit_kind="token_estimate" if _usage_estimated else None,
            **model_routing.ledger_stamp(route if cur_model == model else None, cur_model),
        )
        if cache_input is not None:
            vector = embedcache.extract_embedding(embedding_json)
            if vector is not None:
                await embedcache.store(cur_model, cache_input, vector)  # string inputs only
        _log_request(key.key_prefix, cur_model, cur_ep, "ok", latency, request_id)
        if want_metadata and embedding_json:
            from ..core import costs as _costs

            # GUARDED (audit 2026-11-10 P2-16 twin, cross-validation wave 1):
            # this runs AFTER settle() wrote the ledger row and after the
            # upstream was PAID, and costs._load() propagates DB faults — so an
            # unguarded read 500'd a metadata-requesting response post-spend and
            # the client's retry was a second GPU bill. The metadata block is
            # OPTIONAL BY DESIGN (X-PCAI-Metadata is an opt-in nicety, never a
            # gate), and _pcai_metadata_body already omits cost_usd when it is
            # None — so a cost-read fault degrades to "cost not reported",
            # exactly like an unpriced model.
            try:
                cost: Any = await _costs.compute(cur_model, prompt_tokens, completion_tokens, cached_tokens)
            except Exception as exc:
                log.warning("costs: metadata cost read failed for %r (cost omitted): %s", cur_model, exc)
                cost = None
            mutated = _pcai_metadata_body(
                embedding_json,
                cur_model,
                cur_ep.id,
                cost_usd=cost,
                cached_tokens=cached_tokens,
            )
            if mutated is not None:
                out_content = json.dumps(mutated).encode()
        # Exact-match response cache: store the FINAL bytes (post-guardrail,
        # post-metadata) under the model that actually served. Best-effort -
        # a store fault never affects the caller (core.response_cache).
        if cache_hash is not None and resp.status_code == 200:
            await response_cache.store(key.id, cur_model, cache_hash, out_content)
        return Response(
            content=out_content,
            status_code=resp.status_code,
            media_type=resp.headers.get("content-type", "application/json"),
            headers=out_headers,
        )


# The multipart model-field scanner for /v1/audio/transcriptions (OpenAI SDKs
# send `model` INSIDE the multipart body, where the gateway's usual JSON-body
# resolution cannot see it). Part header line, blank line, value: the value
# runs to the next CRLF — values never contain newlines (form fields are
# line-oriented), so a non-greedy line match is the whole parse we need.
_MULTIPART_MODEL_RE = re.compile(rb'name="model"[^\r\n]*\r\n\r\n([^\r\n]+)')


def _asr_model_from_multipart(body: bytes) -> str:
    """The `model` field of a multipart transcription body, '' when absent.

    Pure byte scanning — the audio payload is NEVER decoded or validated
    (the upstream owns that contract; the gateway only needs the model
    name to gate and bill). decode-with-replace: a model name is ASCII in
    practice, and a garbage value must 404/403 loudly, not crash the route.
    """
    m = _MULTIPART_MODEL_RE.search(body)
    if m is None:
        return ""
    return m.group(1).decode("utf-8", "replace").strip()


def _positive_float(raw: str | None) -> float | None:
    """The parsed value when `raw` is a finite number > 0, else None."""
    if not raw:
        return None
    try:
        val = float(raw)
    except ValueError:
        return None
    return val if (math.isfinite(val) and val > 0) else None


# Response headers an upstream may use to REPORT the generated audio's real
# duration (P10). There is no standard here — OpenAI-compatible TTS servers
# return bare audio bytes — so the check is a best-effort scan over the
# spellings a fleet upstream plausibly uses, plus a sample-count/sample-rate
# pair (exact when both are present). Absent all of them the char/byte proxy
# rides, tagged, exactly as before. Never raises: billing metadata is not a
# client-facing failure.

# Audio compression envelope (cross-validation wave 2, 2026-11-10): an
# encoded upload (mp3/opus/m4a/flac/...) is never more than ~16x smaller in
# bytes than the raw PCM the byte proxy assumes, and never larger — raw PCM
# is the byte-densest honest shape. The DURATION a caller may honestly hint
# is therefore bounded to ~16x on either side of the byte-proxy estimate:
# outside that window the hint is not metadata, it is a price input the
# caller picked (0.001 s = free ASR; 1e8 s = a 1500 USD bill on a 3.6 MB
# upload — both reproduced on the real route), so it is CLAMPED into the
# envelope with a WARNING naming the clamp instead of being trusted.
_AUDIO_COMPRESSION_ENVELOPE = 16.0


def _clamp_duration_hint(hint: float, byte_estimate_s: float) -> float:
    """The caller's duration hint clamped into the upload's compression
    envelope [byte_estimate/16, byte_estimate*16] (seconds; see the
    _AUDIO_COMPRESSION_ENVELOPE comment). The byte proxy is the only quantity
    the gateway measures itself, so it is the clamp's anchor — a hint within
    the envelope rides unchanged. Logs one WARNING naming the clamp when it
    fires: billing metadata that was rewritten must be visible, not silent.
    """
    if byte_estimate_s <= 0:
        return hint
    lo = byte_estimate_s / _AUDIO_COMPRESSION_ENVELOPE
    hi = byte_estimate_s * _AUDIO_COMPRESSION_ENVELOPE
    if lo <= hint <= hi:
        return hint
    clamped = min(max(hint, lo), hi)
    log.warning(
        "audio: duration hint %gs outside the upload's compression envelope"
        " (%gs..%gs from the byte proxy) — clamped to %gs (send raw-PCM-sized"
        " uploads or let the upstream report the duration)",
        hint,
        lo,
        hi,
        clamped,
    )
    return clamped


_DURATION_HEADERS = (
    "x-audio-duration",
    "x-audio-duration-seconds",
    "x-duration-seconds",
    "x-pcai-duration",
    "content-duration",
)


def _audio_duration_s(headers: Any) -> float | None:
    """Real duration (seconds) reported by an upstream response, or None."""
    for name in _DURATION_HEADERS:
        val = _positive_float(headers.get(name))
        if val is not None:
            return val
    samples = _positive_float(headers.get("x-samples")) or _positive_float(headers.get("x-sample-count"))
    rate = _positive_float(headers.get("x-sample-rate")) or _positive_float(headers.get("x-sample-rate-hz"))
    if samples and rate:
        return samples / rate
    return None


async def _proxy_audio_raw(
    pool: Pool,
    ep: Endpoint,
    url: str,
    body: bytes,
    content_type: str,
    key: KeyRecord,
    model: str,
    label: str,
    est_tokens: int,
    request_id: str,
    started: float,
    gate: _GateOutcome,
    *,
    estimate_units: float | None = None,
    unit_kind_estimate: str | None = None,
    duration_hint: float | None = None,
) -> Response:
    """Forward one audio call BYTES-UNCHANGED, relay status/type/bytes.

    `label` is the ledger endpoint label (audio.speech / audio.transcriptions
    — the audio surface is not an endpoint id, so the label doubles as the
    row's endpoint column, exactly like the "chat"/"embeddings" labels the
    other routes settle under). Audio endpoints report no token usage, so
    the ADMISSION estimate IS the billing fact: the 2xx row settles the
    estimate with completion 0; a transport failure or a relayed non-2xx
    settles an "error" row with zero tokens (the relayed error body goes
    back to the client untouched, same transparent-proxy stance as
    _proxy_json). The client's Content-Type rides upstream — a multipart
    transcription must arrive as multipart, a JSON speech body as JSON.

    P10 billing: when a REAL duration is available — a duration/sample-count
    header on the upstream RESPONSE first (the server measured it; it wins
    over anything the caller supplied), then the caller's duration hint (ASR
    only, already clamped into the upload's compression envelope by the
    route) — the 2xx row settles per SECOND (unit_kind='second', cost from
    costs.compute_units; a model priced per 'token' mismatches and ledgers
    NULL with a guard WARNING, never a wrong-rate bill). Without one, the
    legacy estimate settles exactly as before, tagged
    unit_kind=unit_kind_estimate with billable_units = estimate_units (the
    proxy's provenance quantity: chars for TTS, derived seconds for ASR).
    """
    headers = dict(_upstream_headers(ep, request_id))
    headers["Content-Type"] = content_type
    headers.update(gate.rate_headers)
    if gate.warn_level:
        headers["X-Budget-Warning"] = f"{gate.warn_level}% of a period budget consumed"
    try:
        resp = await model_router.client.post(url, content=body, headers=headers)
    except httpx2.HTTPError:
        # Connection refused / timeout / TLS: a breaker-worthy endpoint
        # failure, not a client error (same classification as _proxy_json).
        latency = _latency_ms(started)
        await pool.report(ep, False, error="HTTPError")
        await settle(key.id, model, label, 0, 0, "error", latency, request_id)
        _log_request(key.key_prefix, model, ep, "error", latency, request_id)
        raise fastapi.HTTPException(502, "upstream request failed") from None
    latency = _latency_ms(started)
    out_headers = _relay_headers(
        headers,
        request_id,
        offpeak.effective_for(model, offpeak.key_fraction(key)),
        served_model=model,
        endpoint_id=ep.id,
    )
    if not _is_2xx(resp.status_code):
        await pool.report(ep, False, error=f"upstream status {resp.status_code}")
        await settle(key.id, model, label, 0, 0, "error", latency, request_id)
        _log_request(key.key_prefix, model, ep, "error", latency, request_id)
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            media_type=resp.headers.get("content-type"),
            headers=out_headers,
        )
    await pool.report(ep, True)
    # P10 (hardened by cross-validation wave 2): the UPSTREAM-reported duration
    # wins — the server that decoded the audio measured it, while the caller's
    # hint is a price input the caller picked and (already clamped into the
    # upload's compression envelope by the ASR route) is only the fallback.
    # Either way the token columns keep the ADMISSION estimate (that is what
    # the gates charged); only the cost and the unit columns change.
    duration = _audio_duration_s(resp.headers) or duration_hint
    if duration is not None:
        await settle(
            key.id,
            model,
            label,
            est_tokens,
            0,
            "ok",
            latency,
            request_id,
            billable_units=duration,
            unit_kind="second",
        )
    else:
        await settle(
            key.id,
            model,
            label,
            est_tokens,
            0,
            "ok",
            latency,
            request_id,
            billable_units=estimate_units,
            unit_kind=unit_kind_estimate,
        )
    _log_request(key.key_prefix, model, ep, "ok", latency, request_id)
    return Response(
        content=resp.content,
        status_code=resp.status_code,
        media_type=resp.headers.get("content-type"),
        headers=out_headers,
    )


def _finish_reason_of(payload: dict[str, Any]) -> str:
    """choices[0].finish_reason of a non-streaming chat response ('' absent)."""
    try:
        val = payload["choices"][0].get("finish_reason")
    except (KeyError, IndexError, TypeError, AttributeError):
        return ""
    return val if isinstance(val, str) else ""


# --------------------------------------------------------------------------
# model groups (F3): a named, load-balanced set of equivalent chat models
# --------------------------------------------------------------------------
#
# A group is a SELECTION path, not a failure path (that is the ordered
# fallback chain): a request naming a group picks a MEMBER by live load,
# pins the conversation to it (grp: scope, core.stickiness) and to one of
# its endpoints (ep: scope), then serves it exactly like a pinned model —
# the MEMBER bills, the member's ACL and budgets apply, and a group never
# makes a model reachable that the key could not reach on its own.
#
# Everything here is FAIL-OPEN and OFF BY DEFAULT. With
# settings.model_groups_enabled false — or the core module absent/older
# than this code — every helper answers 'not a group' and routing is
# byte-identical to the pre-feature path. The core module is imported
# INSIDE the helpers (never at module import time): an optional feature
# must not be able to break the data plane's import.


def _groups_module() -> Any | None:
    """core.model_groups when the feature is enabled and importable, else None."""
    if not settings.model_groups_enabled:
        return None
    try:
        from ..core import model_groups
    except Exception:
        log.warning("model_groups: core module unavailable — groups inert for this request", exc_info=True)
        return None
    return model_groups


def _groups_snapshot() -> dict[str, Any]:
    """The last loaded group map (sync, no I/O); {} when groups are inert."""
    mod = _groups_module()
    if mod is None:
        return {}
    try:
        return dict(mod.snapshot() or {})
    except Exception:
        log.warning("model_groups: snapshot read failed — groups inert for this request", exc_info=True)
        return {}


async def _groups_load() -> dict[str, Any]:
    """Refresh (TTL-cached in core) and return the group map; {} when inert.

    Never raises: a DB fault leaves the last good snapshot in place (the
    core module's own contract) and an import fault leaves groups inert.
    """
    mod = _groups_module()
    if mod is None:
        return {}
    try:
        return dict(await mod.load() or {})
    except Exception:
        log.warning("model_groups: load failed — using the last snapshot", exc_info=True)
        return _groups_snapshot()


def _stick_module() -> Any | None:
    """core.stickiness when it carries the grp: scope, else None (fail-open)."""
    try:
        from ..core import stickiness as _stick
    except Exception:
        return None
    if not hasattr(_stick, "get_group_pin") or not hasattr(_stick, "set_group_pin"):
        return None
    return _stick


def _member_score(member: str) -> float | None:
    """Live load score for one member (None = no data); fail-open."""
    try:
        from ..core import load_snapshot
    except Exception:
        return None
    try:
        return load_snapshot.member_score(member)
    except Exception:
        log.warning("model_groups: load snapshot score failed for %r", member, exc_info=True)
        return None


def _group_by_name(name: str) -> Any | None:
    """The ModelGroup for the name, or None (inert when groups are off)."""
    mod = _groups_module()
    if mod is None:
        return None
    try:
        return mod.group_for(name)
    except Exception:
        log.warning("model_groups: group lookup failed for %r", name, exc_info=True)
        return None


def _group_acl(group: Any, key: KeyRecord) -> set[str] | None:
    """The member ACL this key holds for the group: None = every member.

    Naming the GROUP grants its members (the group is the ACL unit when the
    key names it); otherwise each member's own allowed_models entry decides.
    An allow-all key (empty allowed_models) grants everything, as everywhere
    else in this module.
    """
    if not key.allowed_models:
        return None
    allowed = set(key.allowed_models)
    if group.name in allowed:
        return None
    return allowed


def _eligible_members(group: Any, key: KeyRecord) -> list[str]:
    """The group members this key may use RIGHT NOW, in declared order.

    The same four filters the fallback walker applies to a chain entry —
    a live registered pool, the key's ACL, a chat pool, a healthy pool —
    because a group must never widen what the key could already reach nor
    hand a chat request an embedding pool.
    """
    acl = _group_acl(group, key)
    out: list[str] = []
    for member in group.members:
        if acl is not None and member not in acl:
            continue
        pool = model_router.pool(member)
        if pool is None or not pool.healthy():
            continue
        if not serves_chat(member):
            continue
        out.append(member)
    return out


def _pick_member(group: Any, eligible: list[str]) -> str | None:
    """Power-of-two pick over the eligible members, or None when none qualify.

    core.model_groups.pick_member owns the algorithm (pure); this wrapper
    supplies the live score callback and the already-filtered candidate set,
    and degrades to the first eligible member if the core call faults — a
    selection bug must not turn a servable group into a 503.
    """
    if not eligible:
        return None
    mod = _groups_module()
    if mod is not None:
        try:
            picked = mod.pick_member(group, set(eligible), _member_score)
        except Exception:
            log.warning("model_groups: pick failed for %r — first eligible member", group.name, exc_info=True)
            picked = None
        if picked in eligible:
            return str(picked)
    return eligible[0]


def _group_candidates(group_name: str, provisional: str, key: KeyRecord) -> list[str]:
    """Admission candidates for a group: the provisional member, then the
    rest of the group in LOAD ORDER (lowest queue first, no-data last).

    _admit_with_failover walks this list, so a rate/budget 429 on one
    member fails over INSIDE the group instead of ending the turn or
    escaping to a model the client never named.
    """
    group = _group_by_name(group_name)
    if group is None:
        return [provisional]
    eligible = _eligible_members(group, key)
    rest = [m for m in eligible if m != provisional]
    scored = [(m, _member_score(m)) for m in rest]
    scored.sort(key=lambda item: (item[1] is None, item[1] if item[1] is not None else 0.0))
    return [provisional, *[m for m, _score in scored]]


async def _group_target(
    model: str,
    key: KeyRecord,
    affinity_session: str | None,
    request_id: str = "",
    reason: str = "load",
    exclude: set[str] | None = None,
) -> tuple[str, str | None]:
    """(serving_model, group_name) for one request target.

    Non-group -> (model, None) — the caller's path is untouched. Group ->
    the conversation's pinned member when the pin still qualifies (the pin
    rides settings.session_affinity_ttl_s, the conversation-pin lease, and
    an expired pin reads as absent), else a power-of-two pick over the
    members this key may use right now; the pick is written to the grp: pin
    (best effort). Returns (model, group_name) when NO member qualifies —
    the caller answers 503 rather than serving a name with no pool.

    reason labels a non-pin pick in the decision log (load on the request
    path, failover from the fallback walk). The log line is the audit trail
    for why a group request went where it went.

    exclude drops members a caller already knows cannot serve this request
    (the fallback walk passes the model that just failed) — a hop must not
    re-pick the same dead member.
    """
    group = _group_by_name(model)
    if group is None:
        return model, None
    try:
        eligible = [m for m in _eligible_members(group, key) if exclude is None or m not in exclude]
        if not eligible:
            return model, group.name
        stick = _stick_module() if affinity_session else None
        pinned: str | None = None
        if stick is not None and affinity_session:
            try:
                pinned = await stick.get_group_pin(key.id, affinity_session, settings.session_affinity_ttl_s)
            except Exception:
                log.warning("model_groups: grp pin read failed — picking fresh", exc_info=True)
                pinned = None
        if pinned is not None and pinned in eligible:
            member, why = pinned, "pin"
        else:
            if pinned is not None and stick is not None and affinity_session:
                # The pin no longer qualifies (member left the group / the
                # key's ACL / its pool is unhealthy): drop it and re-pick.
                try:
                    await stick.drop_group_pin(key.id, affinity_session)
                except Exception:
                    log.warning("model_groups: stale grp pin drop failed", exc_info=True)
            picked = _pick_member(group, eligible)
            if picked is None:
                return model, group.name
            member, why = picked, (reason or "load")
        if stick is not None and affinity_session:
            try:
                await stick.set_group_pin(key.id, affinity_session, member)
            except Exception:
                log.warning("model_groups: grp pin write failed — the pick will not stick", exc_info=True)
        log.info(
            "group pick request_id=%s group=%s member=%s reason=%s",
            request_id,
            group.name,
            member,
            why,
        )
        return member, group.name
    except Exception:
        # The caller only reaches here when MODEL IS A GROUP (direct path) or a
        # group chain entry, so report it as an unresolvable group: the caller
        # answers 503 (capacity) instead of falling through to pool(group) ->
        # 404 "not registered" (review F3-3).
        log.warning("model_groups: resolution failed for %r (fail-open)", model, exc_info=True)
        return model, model


async def _group_pin_commit(
    key: KeyRecord,
    affinity_session: str | None,
    group_name: str | None,
    served_model: str,
) -> None:
    """Refresh the grp: pin to the member that ACTUALLY served (best effort).

    The admission walk can fail over to a sibling member, so the pin written
    at resolution time may name a model that never answered. Only a member
    is ever pinned; a serve that ended outside the group leaves the
    resolution-time pin alone (it is still a valid member preference, and
    dropping it would cost the conversation its cache affinity for no
    reason). Never raises.
    """
    if not group_name or not affinity_session:
        return
    stick = _stick_module()
    if stick is None:
        return
    try:
        group = _group_by_name(group_name)
        if group is None:
            await stick.drop_group_pin(key.id, affinity_session)
            return
        if served_model not in group.members:
            return
        await stick.set_group_pin(key.id, affinity_session, served_model)
    except Exception:
        log.warning("model_groups: grp pin commit failed (request already served)", exc_info=True)


async def _group_pin_drop_if_dead(key: KeyRecord, affinity_session: str | None, member: str) -> None:
    """Drop the grp: pin only when the member WHOLE pool is unhealthy.

    That is the one failure the pin cannot recover from: every endpoint is
    cooling/disabled, so the next turn must re-pick. A single transport
    error or upstream 5xx is NOT enough — the member is still the
    conversation's model and switching costs a full re-prefill. Never
    raises.
    """
    if not affinity_session:
        return
    stick = _stick_module()
    if stick is None:
        return
    pool = model_router.pool(member)
    if pool is not None and pool.healthy():
        return
    try:
        await stick.drop_group_pin(key.id, affinity_session)
    except Exception:
        log.warning("model_groups: grp pin drop failed", exc_info=True)


async def _pins_after_serve(
    key: KeyRecord,
    affinity_session: str | None,
    group_name: str | None,
    *,
    endpoint_id: str,
    model: str,
    fallback_from: str = "",
) -> None:
    """Refresh the conversation ep:/grp: pins after a SUCCESSFUL serve.

    The streaming twin of the non-streaming commit block, same rules: the
    endpoint pin is refreshed only when the endpoint that ANSWERED is the
    one the pin names (a fallback hop means the pin no longer names the
    endpoint that kept this conversation's cache warm, so it is dropped),
    and the group pin follows the member that answered. Best-effort: any
    fault is logged and forgotten — a pin store must never surface into a
    stream that already delivered its content.
    """
    try:
        stick = _stick_module()
        if stick is not None and affinity_session and settings.session_affinity_enabled:
            if fallback_from:
                await stick.drop_endpoint_pin(key.id, affinity_session)
            else:
                await stick.set_endpoint_pin(key.id, affinity_session, endpoint_id)
        if group_name is not None:
            await _group_pin_commit(key, affinity_session, group_name, model)
    except Exception:
        log.warning("affinity: post-stream pin refresh failed", exc_info=True)


async def _next_fallback_candidate(
    chain: list[str],
    served: str,
    key: KeyRecord,
    endpoint: str,
    est_tokens: int,
    request_id: str,
    started: float,
    affinity_session: str | None = None,
) -> tuple[tuple[Pool, Endpoint, str, dict[str, str]] | None, list[str]]:
    """The next WALKABLE fallback candidate + the REMAINING chain, or
    (None, chain) when no candidate qualifies (review §4a).

    Every candidate is re-gated at walk time — the chain is a preference
    list, never a policy/budget bypass:
      * registered in the live router snapshot;
      * inside the key's allowed_models (ACL is a hard boundary);
      * a HEALTHY chat pool (embedding/reranker pools never serve chat —
        serves_chat's any() predicate, NOT endpoints[0]: a redeploy that
        re-registers endpoints in a different order must not 400 a pool that
        still has a chat member);
      * fresh `check_budgets` headroom — an exhausted candidate settles its
        own 'budget_exceeded' row (visible, budget-source-of-truth) and is
        skipped, exactly like the admission cascade treats it;
      * pool.pick() non-None (a raced cooldown is skipped, not served).
    Only chat endpoints ever walk (the `endpoint` guard); the consumed
    primary (`served`) is skipped. Bounded: the caller's chain is already
    sliced to settings.model_fallback_max, and each hop consumes its
    prefix — the walk cannot cycle or exceed the cap.

    A chain entry may also name a MODEL GROUP (F3): it is resolved to one
    eligible member here (same filters, via _group_target) and the walk
    continues with that member's pool — the served model is the member, so
    a group in a fallback chain is a usable hop rather than an
    unregistered name. `affinity_session` is the conversation's affinity
    key (None = no pin read/write), so a group hop can honour the
    conversation's grp: pin instead of re-picking by load.
    """
    if endpoint != "chat":
        return None, chain
    registered = set(model_router.models())
    groups = _groups_snapshot()
    for idx, cand in enumerate(chain):
        if cand == served:
            continue
        # MODEL GROUP (F3): a chain entry may name a group. Pick a member
        # with the SAME filters the request path uses and walk on with ITS
        # pool — the served model is the member (the caller names it from
        # cand_ep.model_name, exactly like any other hop). No member
        # qualifies -> the candidate is skipped, never served by name.
        target, group_name = cand, None
        if cand in groups:
            target, group_name = await _group_target(
                cand, key, affinity_session, request_id, reason="failover", exclude={served}
            )
            if group_name is None or target == group_name:
                continue
        if target == served:
            continue
        if target not in registered:
            continue
        if group_name is None and key.allowed_models and target not in key.allowed_models:
            # A group grants its members by name (the helper already applied
            # that filter); any other candidate is gated on its own ACL.
            continue
        cand_pool = model_router.pool(target)
        if cand_pool is None or not cand_pool.healthy():
            continue
        if not serves_chat(target):
            continue
        try:
            budget = await check_budgets(key.id, target, est_tokens)
        except Exception:
            log.warning("fallback: budget check failed for %r — skipping candidate", target)
            continue
        if not budget.ok:
            await settle(key.id, target, endpoint, 0, 0, "budget_exceeded", _latency_ms(started), request_id)
            continue
        cand_ep = cand_pool.pick()
        if cand_ep is None:
            continue
        # Fresh upstream headers for the CANDIDATE's endpoint (its own
        # credential resolution — the primary's Authorization must never
        # leak to a different upstream). No rate/budget passthrough here:
        # those headers describe the ADMISSION gates, which ran for the
        # primary; the fallback's own visibility rides the settle row and
        # the response's X-PCAI-Fallback.
        return (
            cand_pool,
            cand_ep,
            f"{cand_ep.base_url}/chat/completions",
            dict(_upstream_headers(cand_ep, request_id)),
        ), chain[idx + 1 :]
    return None, chain


# --------------------------------------------------------------------------
# streaming (chat only)
# --------------------------------------------------------------------------


class _StreamLedger:
    """Scan-only SSE inspector feeding the final settle().

    Accumulates NOTHING but token facts: it counts `[DONE]`-terminated
    chunks and captures the top-level `usage` object if vLLM emits one in
    the final chunk (`stream_options: {"include_usage": true}`). The raw
    bytes are yielded through untouched.
    """

    def __init__(
        self,
        key: KeyRecord,
        model: str,
        est_tokens: int,
        request_id: str,
        started: float,
        off_base: float = 1.0,
        pre_events: list[dict[str, Any]] | None = None,
        route: model_routing.RouteDecision | None = None,
        on_complete: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._key = key
        self._model = model
        self._est = est_tokens
        self._request_id = request_id
        self._started = started
        self._off_base = off_base
        # Complexity-routing decision (chat path): rides the settled row as
        # the routing_* audit columns (core.model_routing.ledger_stamp).
        self._route = route
        self.usage: dict[str, Any] | None = None
        self.done_chunks = 0
        self.completed = False
        # Guardrails: pre_call verdicts known BEFORE the stream starts ride
        # the row written at finish; post_call verdicts are appended by the
        # buffered guard wrapper via extra_events. defer_finish hands the
        # settle+slot-release responsibility from _relay_sse's finally to
        # the wrapper (which can only judge the content AFTER the stream
        # has been fully consumed); override_status lets a post_call block
        # settle the row as 'guardrail_blocked' with the REAL observed
        # usage — one row, honest spend, clear outcome.
        self.pre_events: list[dict[str, Any]] = list(pre_events or [])
        self.extra_events: list[dict[str, Any]] = []
        self.defer_finish = False
        self.override_status: str | None = None
        # Post-completion hook (session affinity / model groups): runs ONLY
        # when the stream actually completed, so a pin is refreshed on a
        # served turn and left alone on a client disconnect. Fire-and-forget
        # by contract — a pin fault must never corrupt a delivered stream.
        self._on_complete = on_complete

    def feed(self, line: bytes) -> None:
        """Inspect one complete SSE line for [DONE] / usage.

        Cheap pre-filter first (perf audit): only lines that CAN carry the
        two facts read here (`[DONE]`, a top-level usage object) are decoded
        and parsed — pure content deltas, the overwhelming majority of a
        stream, skip json.loads entirely.
        """
        if b'"usage"' not in line and b"[DONE]" not in line:
            return
        text = line.decode("utf-8", errors="replace").strip()
        if not text.startswith("data:"):
            return
        payload = text[len("data:") :].strip()
        if payload == "[DONE]":
            self.done_chunks += 1
            return
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError:
            return
        if isinstance(obj, dict) and isinstance(obj.get("usage"), dict):
            self.usage = obj["usage"]

    def _tokens(self) -> tuple[int, int]:
        if self.usage is not None:
            prompt = int(self.usage.get("prompt_tokens") or 0)
            completion = int(self.usage.get("completion_tokens") or 0)
            return prompt, completion
        return self._est, 0

    def _cached(self) -> int:
        """Upstream prompt-cache hits for this stream (usage-detail
        passthrough — the plain-path twin of toolrelay._cached_tokens).
        vLLM/SGLang report prefix-cache hits as
        prompt_tokens_details.cached_tokens; anything absent/odd is 0."""
        if not isinstance(self.usage, dict):
            return 0
        details = self.usage.get("prompt_tokens_details")
        if not isinstance(details, dict):
            return 0
        try:
            return int(details.get("cached_tokens") or 0)
        except (TypeError, ValueError):
            return 0

    async def finish(self) -> None:
        """Write the ledger row. Safe to call from a finally block."""
        status = self.override_status or ("ok" if self.completed else "error")
        latency = _latency_ms(self._started)
        # Real usage when the stream completed (ok OR a post-call policy
        # refusal — the upstream spend happened either way); the estimate
        # only for genuinely incomplete streams.
        if status == "ok" or (self.override_status and self.completed):
            prompt, completion = self._tokens()
        else:
            prompt, completion = self._est, 0
        events = [*self.pre_events, *self.extra_events] or None
        await settle(
            self._key.id,
            self._model,
            "chat",
            prompt,
            completion,
            status,
            latency,
            self._request_id,
            cached_tokens=self._cached(),
            # PER-MODEL (audit r5 #8): this was the last global effective()
            # call in the tree while all eleven sibling settle sites use
            # effective_for(model, base) — and the X-PCAI-Billing header for
            # THIS response is already computed with the per-model form. The
            # global state averages every model together, so a busy model's
            # stream booked a discount the operator never granted (or lost
            # one they did), and the ledger number is what the budget gates
            # consume. _model is the model this stream was SERVED by.
            billing_fraction=offpeak.effective_for(self._model, self._off_base),
            guardrail_events=events,
            **model_routing.ledger_stamp(self._route, self._model),
        )
        _log_request(
            self._key.key_prefix,
            self._model,
            None,
            status,
            latency,
            self._request_id,
        )
        # A COMPLETED stream is a served turn: refresh the conversation's
        # pins (the streaming half of the non-streaming commit block). A
        # disconnect or an upstream error leaves them as they were.
        if self.completed and self._on_complete is not None:
            try:
                await self._on_complete()
            except Exception:
                log.warning("affinity: post-stream pin refresh failed", exc_info=True)


# Cross-request identical-call loop guard (per key). The relay's IN-LOOP
# loop detection is per-request (RelayGuard); the mixed-mode handback moved
# part of the loop into the HARNESS (each handback = a new request), so a
# model ping-ponging the same call across turns was unbounded — observed live
# as an endless bash/logsearch/edit re-issue cycle, each turn costing a
# full-history prefill. Fingerprints (key, server, tool, args) seen >=5x in
# 10 minutes are refused WITHOUT executing.
_LOOP_GUARD_WINDOW_S = 600.0
_LOOP_GUARD_MAX_HITS = 5
# Distinct-fingerprint cap (audit finding 20): over the cap eviction is by
# STALENESS / least-recent-hit (see loop_guard_check) — never a bulk clear.
_LOOP_GUARD_MAX_ENTRIES = 5000
_loop_guard: dict[tuple[Any, str, str, str], list[float]] = {}
# Amortization gate for the O(n) eviction walk (xv round 2, finding 11):
# over-cap inserts pay the walk at most once per second, not per call.
_evict_not_before = 0.0


def loop_guard_check(key_id: Any, server: str, tool: str, arguments: dict) -> str | None:
    """Return a refusal reason when this exact call is looping across
    requests, else None (and record the attempt). In-process state; the
    map is pruned lazily and hard-capped."""
    import json as _json

    fp = (key_id, server, tool, _json.dumps(arguments, sort_keys=True, default=str))
    now = time.monotonic()
    hits = [t for t in _loop_guard.get(fp, []) if now - t < _LOOP_GUARD_WINDOW_S]
    if len(hits) >= _LOOP_GUARD_MAX_HITS:
        # The blocked attempt is NOT recorded (xv round 2, finding 7): a
        # tombstone keeps the fingerprint's window non-empty under steady
        # retry, turning a 10-minute block into a process-lifetime ban (a
        # legitimate identical call retried once a minute was 0/120 allowed).
        # Pruned-only storage keeps the executed count truthful AND the
        # per-fingerprint state bounded at MAX_HITS entries (finding 8) — so
        # once the caller stops for one window the block lapses, exactly the
        # pre-fix re-arm semantics.
        _loop_guard[fp] = hits
        return (
            f"loop guard: identical call {server}/{tool} made {len(hits)}x in "
            f"{_LOOP_GUARD_WINDOW_S / 60:.0f}m — do not repeat it; use the result you already have or change approach"
        )
    hits.append(now)
    _loop_guard[fp] = hits
    # Hard cap (audit finding 20 — the bulk clear let ANY key wipe EVERY
    # key's loop protection with 5000 distinct fingerprints): evict by
    # STALENESS / least-recent-hit — never by insertion order. Under the old
    # oldest-insertion eviction a tripped fingerprint was ALSO the map's
    # oldest entry (its hits stop advancing while blocked), so 5000 distinct
    # calls re-armed a blocked identical-call loop one eviction at a time
    # (audit C-2). An AT/OVER-threshold fingerprint is an ACTIVE blocked
    # loop and is never evicted while below-threshold ones exist; the
    # below-threshold pool IS the flood (fresh fingerprints have 1 hit), so
    # the flood evicts itself and the block survives (xv round 2 verified
    # the tombstone variant of this; the threshold preference alone holds).
    # Amortized (finding 11): the O(n) walk runs at most once per second,
    # and one walk evicts a BATCH down to 90% of the cap.
    global _evict_not_before
    if len(_loop_guard) > _LOOP_GUARD_MAX_ENTRIES and now >= _evict_not_before:
        _evict_not_before = now + 1.0
        target = int(_LOOP_GUARD_MAX_ENTRIES * 0.9)
        stale = [f for f, ts in _loop_guard.items() if all(now - t >= _LOOP_GUARD_WINDOW_S for t in ts)]
        for f in stale:
            _loop_guard.pop(f, None)
        if len(_loop_guard) > target:
            live_below = [
                (f, max((t for t in ts if now - t < _LOOP_GUARD_WINDOW_S), default=0.0))
                for f, ts in _loop_guard.items()
                if sum(1 for t in ts if now - t < _LOOP_GUARD_WINDOW_S) < _LOOP_GUARD_MAX_HITS
            ]
            live_below.sort(key=lambda kv: kv[1])
            for f, _coldest in live_below[: len(_loop_guard) - target]:
                _loop_guard.pop(f, None)
    return None


async def _admit_with_failover(
    key: KeyRecord,
    candidates: list[str],
    est_tokens: int,
    request_id: str,
    started: float,
    precomputed_budget: Any,
) -> tuple[str, Any, str]:
    """Walk the cascade candidates through the admission gates; return
    (model, gate_outcome, failed_over_from).

    The cascade resolves BUDGET headroom only — a RATE-limit 429 (RPM/TPM
    window) on the chosen model used to fail the whole turn even when the
    profile ordering had a perfectly good next model. This walker retries
    the gates down the ordering on rate/budget 429s so the CONVERSATION
    survives. Deliberately NOT failover triggers:
    * concurrency 429 — the cap is key-wide; another model cannot help;
    * non-429 refusals (403 ACL etc.) — policy is not a capacity problem.
    Pinned requests never reach the walk (the caller builds candidates from
    the profile ordering for alias/auto requests only — documented cascade
    semantics).

    Cost note: check_rate_limit increments the (key-wide) minute window per
    attempt — a failed attempt double-charges that request's est against
    TPM. Bounded by the cascade length; accepted for the conversation
    continuity it buys.
    """
    last_exc: fastapi.HTTPException | None = None
    failed_from = ""
    for i, cand in enumerate(candidates):
        try:
            gate = await _run_gates(
                key,
                cand,
                est_tokens,
                "chat",
                request_id,
                started,
                precomputed_budget=precomputed_budget if i == 0 else None,
            )
            return cand, gate, failed_from
        except fastapi.HTTPException as exc:
            if exc.status_code != 429:
                raise
            detail = exc.detail if isinstance(exc.detail, str) else ""
            if detail.startswith("concurrency limit"):
                raise
            failed_from = failed_from or cand
            last_exc = exc
    assert last_exc is not None  # candidates is non-empty; the loop raised or returned
    raise last_exc


def _relay_fallback_tail(serving: str, order: list[str], registered: set[str], allowed: list[str]) -> list[str]:
    """The mid-relay continuation chain: the profile ordering AFTER the
    serving model, filtered to registered + key-allowed models. When the
    serving model is not itself in the ordering, the whole ordering is the
    chain (the ordering is the admin's fallback sign-off, full stop).

    Two callers:
      * the relay's mid-loop BUDGET failover — a turn whose model dies of
        budget MID-STREAM continues on the next ordered model instead of
        ending answerless. Deliberately BROADER than admission failover: a
        PINNED request still serves (and rate-429s) on its model alone, but
        the same promotion resolve_cascade applies at admission when the
        pinned model has no headroom ("budget exhaustion on the pinned model
        promotes the request to the ordering") applies here when that
        exhaustion is discovered mid-flight.
      * the proxy's runtime DEAD-ENDPOINT failover — the admission block
        appends this tail as the FINAL segment of an ALIAS request's walk
        chain (after the per-model chain), so `auto` keeps cascading when its
        admitted model cannot serve at all. Alias-only: a pinned request's
        runtime net stays its per-model chain.
    Fresh budget gates re-check each candidate, so a candidate that is
    itself exhausted is skipped, not served.
    """
    tail: list[str] = []
    past_serving = serving not in order  # unordered serving model: whole order chains
    for cand in order:
        if cand == serving:
            past_serving = True
            continue
        if not past_serving or cand not in registered:
            continue
        if allowed and cand not in allowed:
            continue
        tail.append(cand)
    return tail


async def _relay_executor(
    key: KeyRecord,
    request_id: str,
    *,
    grants: set[tuple[str, str]] | None = None,
    served_model: str = "",
):
    """Build the relay's MCP executor: ledger-settled, ACL-rechecked,
    in-cluster call to the upstream MCP server.

    Gates mirror the /mcp call path (security audit): the per-server
    tool-policy (`server_allows`) and the key's RPM/TPM window are enforced
    here too — tightening a server to 'allowlist' or exhausting the key's
    rate budget must cut off the RELAY path exactly as it cuts off /mcp,
    otherwise the automated path is a policy bypass.

    THIS IS THE MODEL-DRIVEN PATH. Every tool call a model makes during a
    relay turn lands in `execute` below, so the argument-level policy stage
    (hardening P0-1) runs HERE as well as in /mcp's `_gated_mcp_call`: the
    SAME `mcpf.arg_policy.gate_call` (admin-tool refusal -> argument
    destination policy -> payload telemetry -> read+egress separation), same
    order, same 'policy_blocked' ledger status. A refusal returns
    (False, message) — this path's contract, which the relay surfaces to the
    model as a tool error — and never opens an upstream session.

    grants: the key's effective (server, tool) grant set, already computed by
    the caller (relay_grants). Only the read+egress separation needs it; the
    relay route already enforced the key ACL before calling us. None skips
    that one item (a direct caller that has no grant set in hand).
    """
    from mcp import ClientSession

    from ..core.chain_audit import write_mcp_call
    from ..mcpf import arg_policy
    from ..mcpf.federation import server_allows
    from .routes_mcp import _upstream_session, resolve_mcp_headers

    _ctor_model = served_model

    async def record(server: str, tool: str, arguments: dict, status: str, model: str = "") -> None:
        served_model = model or _ctor_model
        # ONE terminal outcome for a relay tool call: ledger row + P2-1 metric
        # + P2-2 chained audit row.
        #
        # The ledger row names a model when one is known (served_model) instead
        # of always the historical star placeholder - that placeholder is why
        # the ledger could not attribute a tool call to a model at all, which is
        # the P1-1 prerequisite.
        #
        # STATUS (honest): on the RELAY path served_model is still empty, so
        # these rows read model="*" exactly as before. Threading the relay
        # turn's served model here was attempted and REVERTED: the executor's
        # 3-arg contract is shared with core.clientside and faked by ~25 tests,
        # and a keyword-only model= broke 16 of them. The seam is ready (the
        # model= parameter exists and wins when supplied); the wiring needs a
        # contract change done deliberately, with those fakes updated. Until
        # then the MCP attribution this wave DOES deliver is the chained
        # mcp_call_audit row (server/tool/digest/outcome/request_id), which is
        # per-call and greppable even without the model label.
        #
        # policy_blocked is counted by mcpf.arg_policy._count_block, so the
        # metric is ticked here only for ok/error (no double count).
        try:
            await settle(key.id, served_model or "*", f"mcp:{server}:{tool}", 0, 0, status, 0, request_id)
        except Exception:
            log.exception("relay: settle failed (%s %s/%s)", status, server, tool)
        if status in ("ok", "error", "denied"):
            _metrics.observe_mcp_call(server, status, served_model)
        await write_mcp_call(
            key_id=key.id,
            server=server,
            tool=tool,
            arguments=arguments,
            outcome=status,
            model=served_model,
            request_id=request_id,
        )

    async def execute(server: str, tool: str, arguments: dict) -> tuple[bool, Any]:
        servers = {sv["name"]: sv for sv in await registry.mcp_servers()}
        target = servers.get(server)
        if target is None:
            return False, f"unknown server {server!r}"

        # cross-request loop guard BEFORE anything touches an upstream
        guard_reason = loop_guard_check(key.id, server, tool, arguments)
        if guard_reason:
            await record(server, tool, arguments, "denied")
            return False, guard_reason

        # rate gate FIRST (same order as /mcp: burst-guard before anything
        # touches an upstream); tool calls consume 0 tokens, this is purely
        # the RPM axis
        rate = await check_rate_limit(key.id, key.rate_limit_rpm, key.rate_limit_tpm, 0)
        if not rate.ok:
            await record(server, tool, arguments, "denied")
            return False, f"rate limited: {rate.reason}"

        # per-server tool-policy gate (defense-in-depth over the key ACL,
        # which relay_stream already enforced before calling us)
        if not server_allows(target, tool):
            _metrics.observe_acl_denial(server)
            await record(server, tool, arguments, "denied")
            return False, "denied by server policy"

        # argument/grant-level policy stage (hardening P0-1): the SAME call,
        # in the SAME order, as /mcp's gate chain — admin-tool refusal,
        # argument destination policy, payload telemetry, read+egress
        # separation. Runs AFTER the tool-policy gate and BEFORE
        # resolve_mcp_headers/_upstream_session: a refusal must not build
        # credentials or open a session to the server it refuses to reach.
        decision = arg_policy.gate_call(
            server,
            tool,
            arguments,
            key_mcp_admin=getattr(key, "mcp_admin", False),
            grants=grants,
        )
        if decision.refused:
            await record(server, tool, arguments, "policy_blocked")
            return False, decision.message

        # Header build WITH the calling key: resolve_mcp_headers merges the
        # key's per-key MCP credential overrides after the server-level auth
        # headers (last-wins) — the relay path must behave exactly like /mcp.
        headers = await resolve_mcp_headers(target, key_id=key.id)

        async def do(session: ClientSession) -> Any:
            return await session.call_tool(tool, arguments)

        try:
            result = await _upstream_session(target["url"], headers, do)
        except Exception as exc:
            log.warning("relay: %s/%s failed: %s", server, tool, exc)
            await record(server, tool, arguments, "error")
            return False, f"upstream failed: {type(exc).__name__}"
        ok = not result.is_error
        await record(server, tool, arguments, "ok" if ok else "error")
        content = [b.model_dump(mode="json", exclude_none=True) for b in result.content]
        return ok, content

    return execute


def _relay_headers(
    headers: dict[str, str],
    request_id: str,
    eff_fraction: float = 1.0,
    route_value: str | None = None,
    served_model: str = "",
    endpoint_id: str = "",
    fallback_from: str = "",
) -> dict[str, str]:
    """Caller headers worth relaying on the response (request id + budget
    warning + rate-limit counters). Upstream headers are NOT forwarded
    (hop-by-hop hygiene).

    Off-peak billing visibility: when the effective billing fraction differs
    from full price, X-PCAI-Billing: offpeak-<fraction> (e.g. offpeak-0.5)
    announces the discount in effect for THIS response — the caller passes
    its effective fraction (profile/env base under the CURRENT demand state;
    1.0 adds no header: normal billing is the default and needs no signal).

    Complexity-routing visibility (X-PCAI-Route, same precedent): `route_value`
    (core.model_routing.route_header) announces the routing decision in
    effect for THIS response — policy, ceiling, served/would-serve, deciding
    cut. None adds no header (policy 'off' is the default state and needs no
    signal, exactly like full-price billing).

    Router metadata (review §4b, headers ALWAYS — no transparency cost):
    X-PCAI-Served-Model names the model that actually answered (the post-
    cascade/post-routing truth) and X-PCAI-Endpoint the endpoint id that
    served it. Empty values add nothing (defensive: a caller that cannot
    supply the fact yet — e.g. an aborted stream — simply gets no header
    rather than a wrong one).

    Fallback visibility (X-PCAI-Fallback, review §4a): `fallback_from` names
    the model that FAILED and was failed over FROM (the primary when a chain
    hop answered, or the admission-time failed-over-from). Empty = the
    primary answered (no header)."""
    out = {k: v for k, v in headers.items() if k.startswith(("X-Request-ID", "X-Budget-Warning", "X-RateLimit-"))}
    out.setdefault("X-Request-ID", request_id)
    eff = eff_fraction
    if eff != 1.0:
        out["X-PCAI-Billing"] = f"offpeak-{eff:g}"
    if route_value:
        out["X-PCAI-Route"] = route_value
    if served_model:
        out["X-PCAI-Served-Model"] = served_model
    if endpoint_id:
        out["X-PCAI-Endpoint"] = endpoint_id
    if fallback_from:
        out["X-PCAI-Fallback"] = fallback_from
    return out


def _pcai_metadata_body(
    payload: dict[str, Any],
    served_model: str,
    endpoint_id: str,
    cost_usd: Any = None,
    cached_tokens: int = 0,
) -> dict[str, Any] | None:
    """Attach the opt-in `"pcai": {...}` metadata block to a non-streaming
    JSON response body (review §4b) — returns the mutated payload, or None
    when the body is not a shape the block can ride.

    OPT-IN BY DESIGN: this module is a declared TRANSPARENT proxy — the
    upstream JSON comes back exactly as received unless the caller asked
    for metadata with `X-PCAI-Metadata: 1` AND the response is
    non-streaming (an SSE body can never be quietly mutated). The block
    names the serving facts a client cannot otherwise see: the model that
    actually answered (post-cascade/post-routing), the endpoint id, the
    computed USD cost (when the model is priced) and upstream cached
    tokens. Mirrors the X-PCAI-Served-Model/X-PCAI-Endpoint headers which
    carry the same facts with no body mutation."""
    if payload.get("pcai") is not None:
        return None  # upstream already uses the key — never collide
    block: dict[str, Any] = {
        "served_model": served_model,
        "endpoint": endpoint_id,
    }
    if cost_usd is not None:
        block["cost_usd"] = float(cost_usd)
    if cached_tokens:
        block["cached_tokens"] = cached_tokens
    payload["pcai"] = block
    return payload


async def _relay_sse(
    resp: httpx2.Response,
    ledger: _StreamLedger,
    release_slot: Callable[[], None] | None = None,
    defer_finish: bool = False,
    prefix: bytes = b"",
    chunks: AsyncIterator[bytes] | None = None,
) -> AsyncIterator[bytes]:
    """Yield upstream SSE bytes through, scanning lines for usage/[DONE].

    Lines are reassembled across chunk boundaries with a bytearray cursor —
    a `data: {...}` line may straddle two network reads, and json.loads on
    a split line would silently miss the usage object.

    `release_slot` (concurrency backpressure): the per-key in-flight slot is
    HELD for the lifetime of the stream and released in the finally below —
    releasing it when the handler returned would make the cap unable to
    constrain long-lived streams (the exact failure it exists to prevent).

    `defer_finish=True` (post-call guardrails): the buffered guard wrapper
    consumes this generator to the end, judges the assembled content, and
    THEN takes over both the ledger settle and the slot release — this
    generator skips both here (resp.aclose still always runs).

    `prefix` / `chunks` (TTFT peek gate, `_peek_first_data_line`): the gate
    reads the upstream's first bytes BEFORE this generator is built, so the
    bytes it consumed must be replayed, not lost. `prefix` goes through the
    SAME buf/line cursor first (a data line straddling the peek boundary
    still parses as one line — the ledger's usage scan is unaffected), then
    `chunks` — when given — continues the peek's byte iterator instead of
    opening a fresh `resp.aiter_bytes()` (httpx2 streams are one-shot: a
    second iterator raises StreamConsumed). Both default to the untouched
    legacy path (b"" / the response's own iterator).
    """
    buf = bytearray()
    ttft_done = False  # TTFT fires once, on the first upstream byte batch

    def _scan(data: bytes) -> None:
        """Feed one byte batch through the line cursor (shared by the peek
        replay and the live loop — the cursor state is continuous across
        the two, so a line straddling the peek boundary still parses)."""
        buf.extend(data)
        while True:
            # LINE TERMINATORS (audit C-7). SSE (WHATWG) accepts CR, LF and
            # CRLF. This cursor split on LF only, so a CR-only upstream — or
            # an intermediary rewriting line endings — parsed as ONE giant
            # line: json.loads failed on it in the ledger's usage scan, the
            # settle fell back to the estimate, and [DONE] was never seen.
            # Find whichever terminator comes first; CRLF stays one break
            # (the trailing LF is consumed as the next, empty line — a shape
            # the cursor already produced for blank lines, and skipped).
            nl = buf.find(b"\n")
            cr = buf.find(b"\r")
            if nl == -1 and cr == -1:
                break
            idx = cr if (cr != -1 and (nl == -1 or cr < nl)) else nl
            ledger.feed(bytes(buf[:idx]))
            del buf[: idx + 1]

    def _observe_ttft() -> None:
        """TTFT: the first upstream byte batch reaching the client proxy
        loop (covers both pass-through and the guard-buffered wrapper —
        this generator runs inside either). Elapsed against the ledger's
        request clock (both monotonic). getattr guard: unit tests construct
        bare ledgers whose clocks may be absent — metrics must never break
        a stream."""
        nonlocal ttft_done
        if ttft_done:
            return
        ttft_done = True
        _started = getattr(ledger, "_started", None)
        if _started is not None:
            _metrics.observe_ttft(getattr(ledger, "_model", ""), time.monotonic() - _started)

    # --- OTLP: one generation span per streamed model call -------------------
    # Opened here and closed in the finally below, so it covers the WHOLE
    # stream — including a client disconnect, which is why it shares the
    # finally that settles the ledger. Metadata only: model, token counts,
    # latency, completion status; never the prompt or the streamed text.
    # No-op when tracing is off (the default).
    _span_attrs: dict[str, Any] = {
        "langfuse.observation.type": "generation",
        "langfuse.trace.name": "chat",
        "langfuse.observation.model.name": getattr(ledger, "_model", ""),
        "gen_ai.operation.name": "chat",
        "gen_ai.request.model": getattr(ledger, "_model", ""),
        "gen_ai.response.id": getattr(ledger, "_request_id", ""),
    }
    # Same pseudonymous UUID as the non-streaming path; omitted when unknown.
    _uid = _span_user_id(getattr(ledger, "_key", None))
    if _uid:
        _span_attrs["user.id"] = _uid
    span = tracing.start_span("chat", _span_attrs)

    try:
        if prefix:
            # TTFT-peek replay (`_peek_first_data_line`): these bytes were
            # already read off the wire by the gate, so they go out FIRST,
            # before the loop waits on another network read.
            _scan(prefix)
            _observe_ttft()
            yield prefix
        async for chunk in chunks if chunks is not None else resp.aiter_bytes():
            _scan(chunk)
            _observe_ttft()
            yield chunk
        if buf:
            ledger.feed(bytes(buf))
        ledger.completed = True
    finally:
        # Settle in finally, not after the loop: on client disconnect the
        # async generator is closed mid-iteration and the lines after the
        # last yield never run. The ledger row must be written for BOTH the
        # completed-stream and abandoned-stream outcomes or budget spend
        # under-counts (ledger-based accounting, see core.budgets).
        #
        # Cancellation-proofing (perf audit): Starlette's disconnect branch
        # CANCELS this task — a plain await at the first checkpoint here
        # would raise immediately, skipping the ledger settle and leaking
        # the concurrency slot forever (aborted streams are the norm for
        # agent clients). Order: the SYNC slot release runs first (it cannot
        # be interrupted); the awaits then run inside a shielded scope so
        # they complete even under the delivered cancellation — the pending
        # CancelledError is re-raised at scope exit, preserving the abort.
        if release_slot is not None and not defer_finish:
            release_slot()
        with anyio.CancelScope(shield=True):
            await resp.aclose()
            if not defer_finish:
                await ledger.finish()
            # Span close belongs INSIDE the shield, and is a plain (never
            # awaited) call. After the shield exits, any pending cancellation
            # is delivered at the next checkpoint — so a post-shield end()
            # would be skipped precisely on client disconnect, which is the
            # case this has to record. Sync + shielded = always emitted.
            # Read usage off the ledger's public attribute rather than the
            # private _tokens() helper: a completed stream carries the upstream
            # usage object, an abandoned one carries None (recorded as 0 tokens,
            # which the error status marks as "unknown" rather than "free").
            _usage = getattr(ledger, "usage", None) or {}
            span.end(
                {
                    "gen_ai.usage.input_tokens": int(_usage.get("prompt_tokens") or 0),
                    "gen_ai.usage.output_tokens": int(_usage.get("completion_tokens") or 0),
                    "duration_ms": _latency_ms(getattr(ledger, "_started", time.monotonic())),
                    "stream.completed": ledger.completed,
                },
                error=not ledger.completed,
            )


def _sse_guard_error(message: str, code: str = "guardrail_blocked") -> bytes:
    """A terminal SSE error envelope + [DONE] (OpenAI SDKs surface .error)."""
    payload = json.dumps({"error": {"message": message, "type": code, "param": "", "code": code}})
    return b"data: " + payload.encode() + b"\n\ndata: [DONE]\n\n"


def _sse_masked_final(model: str, request_id: str, masked_text: str) -> bytes:
    """Synthesize a single-shot content stream carrying the masked text.

    Used when a post_call guardrail masked the output of a buffered stream:
    the original content deltas are suppressed (they carry the UNMASKED
    text) and one consolidated chunk replaces them.
    """
    base = {
        "id": f"chatcmpl-guarded-{request_id[:16]}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
    }
    first = {
        **base,
        "choices": [{"index": 0, "delta": {"role": "assistant", "content": masked_text}, "finish_reason": None}],
    }
    last = {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
    return b"data: " + json.dumps(first).encode() + b"\n\ndata: " + json.dumps(last).encode() + b"\n\ndata: [DONE]\n\n"


def _guardrail_reply_json(model: str, request_id: str, reply_text: str) -> dict[str, Any]:
    """A normal OpenAI chat.completion body carrying a SOFT guardrail block's
    notice as the assistant message (zero usage — no upstream call happened).

    The soft block must look like a normal assistant turn to agent clients
    (DSH's loop treats a 400 as fatal for the whole thread); the
    X-PCAI-Guardrail-Blocked response header is the machine-detectable
    signal for clients that want to purge the turn and continue.
    """
    return {
        "id": f"chatcmpl-guarded-{request_id[:16]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": reply_text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def _sse_carries_tool_call_delta(obj: Any) -> bool:
    """Does ONE parsed SSE chat chunk carry a tool-call DELTA? PURE.

    The mask classifies tool-call argument fragments as text (they ARE model
    output), so the chunk is dropped — and a client that never receives the
    delta can never execute the call.
    """
    choices = obj.get("choices") if isinstance(obj, dict) else None
    for choice in choices if isinstance(choices, list) else ():
        delta = choice.get("delta") if isinstance(choice, dict) else None
        if isinstance(delta, dict) and delta.get("tool_calls"):
            return True
    return False


def _sse_is_handback_event(obj: Any) -> bool:
    """Is this chunk the relay's {"pcai": {"event": "handback", ...}} frame? PURE.

    Its payload carries the tool NAMES, not the arguments, so the mask leaves it
    alone — which is exactly why a masked turn could still ask the client to
    hand back calls whose deltas were suppressed (audit w6 protocol break).
    """
    payload = obj.get("pcai") if isinstance(obj, dict) else None
    return isinstance(payload, dict) and payload.get("event") == "handback"


def _sse_finishes_with_tool_calls(obj: Any) -> bool:
    """Does this chunk terminate the turn with finish_reason "tool_calls"? PURE."""
    choices = obj.get("choices") if isinstance(obj, dict) else None
    return any(
        isinstance(choice, dict) and choice.get("finish_reason") == "tool_calls"
        for choice in (choices if isinstance(choices, list) else ())
    )


def _downgrade_tool_calls_terminal(chunk: bytes) -> bytes | None:
    """Rewrite ONE buffered SSE chunk's tool_calls finish_reason to "stop".

    Returns the rewritten chunk, or None when the chunk is not a single
    parseable data event (a straddled event: the caller drops it and the
    synthetic masked final supplies the terminal). Only the finish_reason is
    touched — every other field (usage included) survives verbatim.
    """
    text = chunk.strip()
    if not text.startswith(b"data:"):
        return None
    payload = text[len(b"data:") :].strip()
    try:
        obj = json.loads(payload)
    except (TypeError, ValueError):
        return None
    if not isinstance(obj, dict):
        return None
    choices = obj.get("choices")
    if not isinstance(choices, list):
        return None
    changed = False
    for choice in choices:
        if isinstance(choice, dict) and choice.get("finish_reason") == "tool_calls":
            choice["finish_reason"] = "stop"
            changed = True
    if not changed:
        return None
    return b"data: " + json.dumps(obj).encode() + b"\n\n"


# Ceiling on the bytes a post_call-guarded stream may buffer before replay
# (audit 2026-11-10 P2-25). The wrapper holds the WHOLE answer in memory to judge
# it, so an upstream that never stops talking was an unbounded per-request
# allocation on the event loop. 32 MiB is ~20x the largest answer a real
# completion can produce (max_tokens is capped at 262144, i.e. low single-digit
# MB of SSE), so it only ever fires on a pathological or hostile stream — and it
# fires CLOSED: bytes that cannot be judged are never delivered unjudged.
_GUARDED_SSE_MAX_BUFFER_BYTES = 32 * 1024 * 1024


class _SlotReleasingStreamingResponse(StreamingResponse):
    """StreamingResponse that gives the concurrency slot back on EVERY exit.

    WHY a subclass (cross-validation wave 1, P1-2 follow-up): the slot's normal
    release points are the stream generator's own finally (stream ended, client
    walked away) and the handler's error guard. NEITHER fires when the request
    task is cancelled during Starlette's http.response.start — that send happens
    BEFORE the body iterator's first __anext__ (starlette/responses.py
    stream_response), and a NEVER-STARTED async generator executes no body, so
    its finally never runs and the per-key counter stays incremented: the key is
    429-bricked on its concurrency cap until the pod restarts.

    This is the response-layer backstop. Whatever ends the send loop —
    completion, client disconnect, or a cancellation at any point — the slot is
    released. It uses the handler's once-wrapper, so it can never steal a
    CONCURRENT request's slot and the generator's own release stays
    authoritative.
    """

    def __init__(self, *args: Any, release_slot: Callable[[], None] | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._release_slot = release_slot

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            if self._release_slot is not None:
                self._release_slot()


async def _guarded_sse(
    inner: AsyncIterator[bytes],
    ledger: _StreamLedger | None,
    post_guards: list[Any],
    *,
    key: KeyRecord,
    model: str,
    request_id: str,
    started: float,
    release_slot: Callable[[], None] | None = None,
    relay_mode: bool = False,
    pre_events: list[dict[str, Any]] | None = None,
) -> AsyncIterator[bytes]:
    """Buffer an SSE stream, run post_call guardrails on the assembled
    assistant text, then replay.

    Pass   → the buffered bytes replay verbatim (byte-identical output;
             the cost is buffering latency, and only when the model has
             post_call guardrails attached).
    Masked → every TEXT-CARRYING chunk is SUPPRESSED (audit ZC-3: content,
             all three reasoning spellings, tool-call argument fragments and
             the pcai thinking/tool events all carry the model's text — a
             chunk the classifier calls text-bearing is dropped WHOLE, never
             replayed unmasked, because a streaming fragment's guard verdict
             can differ from the assembled one and a mask that misses a
             fragment leaks exactly the text it exists to hide) and ONE
             consolidated chunk carries the masked ASSISTANT ANSWER only.
             Non-text frames (usage, error envelopes, keepalives) replay
             verbatim. A turn whose TOOL-CALL deltas were suppressed is
             DOWNGRADED (audit w6): its handback event is dropped and its
             tool_calls terminal is rewritten to "stop", so the client is
             never asked to execute calls it did not receive.
    Blocked→ the buffered output is discarded; the client receives a
             terminal SSE error envelope; the ledger records the outcome.

    `ledger` set (plain stream): the ledger row is settled HERE with the
    union of pre+post events (defer_finish — _relay_sse skipped it).
    `relay_mode` (relay stream): the relay settled its own per-hop rows, so
    the guard verdicts land on a dedicated 0-token marker row with
    endpoint='chat:guardrail' — filterable in the usage explorer, zero
    impact on budget/token SUMs.
    """
    chunks: list[bytes] = []
    buffered = 0
    overflowed = False
    text_parts: list[str] = []
    # LINE CURSOR, not one parse per network read (audit r5 #2c): httpx2's
    # aiter_bytes() hands over whatever the socket returned, so an SSE event
    # routinely straddles two reads. Parsing each read on its own made a
    # straddled event INVISIBLE — the guard never saw its text (so no mask
    # ever fired) and the masked replay, classifying both halves text-free,
    # replayed them VERBATIM. `tail` carries the unterminated remainder into
    # the next read, `tail_from` is the index of the FIRST chunk it came from,
    # and `dirty` records every chunk that contributed bytes to a
    # text-bearing line so the masked replay drops those chunks WHOLE.
    # (This is why the old per-chunk classifier is gone: a per-chunk view
    # cannot see a straddled event, which was the defect.)
    tail = b""
    tail_from = -1
    dirty: set[int] = set()
    # The handback protocol break (audit w6): the mask drops every chunk that
    # carries a tool-call ARGUMENT fragment, but the handback pcai event (tool
    # NAMES only) and the finish_reason "tool_calls" terminal carry no
    # classified field — so they used to replay while the deltas the client
    # must answer were suppressed. These spans let the masked replay detect
    # that and downgrade the turn to a plain masked completion.
    tool_span: set[int] = set()
    handback_span: set[int] = set()
    finish_span: set[int] = set()
    # Chunks that are part of a [DONE] line, recorded BY INDEX (audit
    # 2026-11-10 P2-19). The masked replay used a b"data: [DONE]" SUBSTRING
    # test, which two legal framings defeat — a [DONE] straddling two network
    # reads ("data: [DO" + "NE]") and the space-less "data:[DONE]". Either one
    # shipped the ORIGINAL terminal ahead of the synthetic masked final, so the
    # client stopped reading at an answer it never received.
    done_span: set[int] = set()

    def _classify(line: bytes, first: int, last: int) -> None:
        """Fold ONE complete SSE line into the guard input + the drop set.

        The guard judges what the ASSISTANT REPLIED: content deltas ONLY —
        see _sse_answer_texts_of_payload (reasoning is dropped wholesale on
        the masked path; tool args/results are caller or tool data and must
        not block or shape the answer). The DROP set is deliberately wider
        than the guard input: every model-text channel makes its chunk
        undeliverable on the masked path.
        """
        stripped = line.strip()
        if not stripped.startswith(b"data:"):
            return
        payload = stripped[len(b"data:") :].strip()
        if payload == b"[DONE]":
            # The terminal frame: recorded by CHUNK INDEX (see done_span), and
            # by the LINE's full span, so a straddled terminal is caught too.
            done_span.update(range(first, last + 1))
            return
        if not payload:
            return
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError:
            return
        if not isinstance(obj, dict):
            return  # a non-object data line carries no chat text (never crash a stream)
        text_parts.extend(_sse_answer_texts_of_payload(obj))
        if _sse_text_of_payload(obj):
            dirty.update(range(first, last + 1))
        if _sse_carries_tool_call_delta(obj):
            tool_span.update(range(first, last + 1))
        if _sse_is_handback_event(obj):
            handback_span.update(range(first, last + 1))
        if _sse_finishes_with_tool_calls(obj):
            finish_span.update(range(first, last + 1))

    try:
        async for chunk in inner:
            if buffered >= _GUARDED_SSE_MAX_BUFFER_BYTES:
                # BUFFER CAP (audit 2026-11-10 P2-25): stop buffering and stop
                # pulling from the upstream — the answer cannot be judged, and
                # an unjudged answer is never delivered (see the abort below).
                overflowed = True
                break
            idx = len(chunks)
            chunks.append(chunk)
            buffered += len(chunk)
            # _sse_line_tokens is CR/LF/CRLF-aware (audit C-7); its LAST
            # element is the unterminated remainder, everything before it is
            # a complete line.
            *complete, tail = _sse_line_tokens(tail + chunk)
            # NOT named `first`: the blocked branch below reuses that name for
            # the first GuardrailEvent and mypy infers one type per function.
            span_first = tail_from if tail_from >= 0 else idx
            for line in complete:
                _classify(line, span_first, idx)
            tail_from = span_first if tail else -1
        if tail:
            # A stream may end WITHOUT a final terminator: classify the
            # remainder like a line (it is a whole event in every real
            # upstream, and the per-read parser used to parse it).
            _classify(tail, max(tail_from, 0), len(chunks) - 1)
            tail = b""
    except BaseException:
        # Consumer walked away mid-buffer: the deferred ledger row and the
        # slot must still be handled (same discipline as _relay_sse's
        # finally — aborted streams are the norm for agent clients).
        if ledger is not None:
            with anyio.CancelScope(shield=True):
                await ledger.finish()
        if release_slot is not None and not relay_mode:
            release_slot()
        raise

    if overflowed:
        # FAIL CLOSED (audit 2026-11-10 P2-25): the answer outgrew the buffer, so
        # post_call guardrails cannot judge it. Delivering the bytes anyway would
        # be a guardrail BYPASS on the one path an operator attached a post_call
        # policy to; the terminal error envelope tells the client the turn was
        # refused rather than silently truncating it.
        aclose = getattr(inner, "aclose", None)
        if aclose is not None:
            # Close the abandoned upstream stream (and let the relay generator's
            # own finally settle/release) before the shielded settle below.
            with anyio.CancelScope(shield=True):
                await aclose()
        detail = (
            f"response exceeded the {_GUARDED_SSE_MAX_BUFFER_BYTES}-byte post-call guardrail "
            "buffer cap and was not delivered unjudged"
        )
        log.warning("guardrails: %s (request_id=%s)", detail, request_id)
        if ledger is not None:
            ledger.override_status = "guardrail_blocked"
            if release_slot is not None:
                release_slot()
            with anyio.CancelScope(shield=True):
                await ledger.finish()
        else:
            await settle(
                key.id,
                model,
                "chat:guardrail",
                0,
                0,
                "guardrail_blocked",
                _latency_ms(started),
                request_id,
            )
        yield _sse_guard_error(detail, code="guardrail_buffer_exceeded")
        return

    text = "".join(text_parts)
    try:
        guarded_text, post_events, blocked = await guardrails.run_post_call(post_guards, text, model_router.client)
    except Exception as exc:  # fail-open, visible: policy engine errors must not eat the answer
        log.warning("guardrails: post_call evaluation failed: %s", exc)
        guarded_text, post_events, blocked = text, [], False
    except BaseException:
        # Client disconnect / task cancellation DURING the guardrail
        # evaluation (audit r5 #11): the buffering loop above is guarded, but
        # this await sits AFTER it — and CancelledError is a BaseException, so
        # a client that walks away while a webhook guardrail is evaluating
        # (10 s default, unbounded by the client) used to skip BOTH
        # ledger.finish() (lost ledger row -> under-billing) and
        # release_slot() (the _in_flight counter never decrements -> the key
        # is 429-bricked on its concurrency cap until the pod restarts).
        # Same discipline as the buffering loop: settle and release shielded,
        # then re-raise the cancellation.
        if ledger is not None:
            with anyio.CancelScope(shield=True):
                await ledger.finish()
        if release_slot is not None and not relay_mode:
            release_slot()
        raise
    post_dicts = [e.as_dict() for e in post_events]
    all_events = [*([*ledger.pre_events] if ledger is not None else [*(pre_events or [])]), *post_dicts]

    if blocked:
        first = post_events[0] if post_events else None
        message = f"blocked by guardrail '{first.guardrail if first else 'post_call'}'" + (
            f": {first.detail}" if first is not None and first.detail else ""
        )
        if ledger is not None:
            ledger.override_status = "guardrail_blocked"
            ledger.extra_events = post_dicts
            if release_slot is not None:
                release_slot()
            with anyio.CancelScope(shield=True):
                await ledger.finish()
        else:
            await settle(
                key.id,
                model,
                "chat:guardrail",
                0,
                0,
                "guardrail_blocked",
                _latency_ms(started),
                request_id,
                guardrail_events=all_events or None,
            )
        yield _sse_guard_error(message)
        return

    if ledger is not None:
        ledger.extra_events = post_dicts
        with anyio.CancelScope(shield=True):
            await ledger.finish()
    elif all_events:
        # relay marker row: policy audit next to the relay's per-hop rows
        await settle(
            key.id,
            model,
            "chat:guardrail",
            0,
            0,
            "ok",
            _latency_ms(started),
            request_id,
            guardrail_events=all_events,
        )

    # THE SLOT IS RELEASED AFTER THE REPLAY, NOT BEFORE (audit 2026-11-10
    # P2-25). The release used to sit beside ledger.finish() — i.e. BEFORE the
    # buffered bytes were handed to the client — so the concurrency cap stopped
    # constraining a guarded stream at exactly the point its delivery began.
    # The finally also covers a client disconnect DURING the replay
    # (GeneratorExit is thrown at the yield, which is where a disconnect lands).
    # Idempotent, so the earlier release on the blocked/overflow paths and this
    # one never conflict.
    try:
        if guarded_text == text:
            # pass: byte-identical replay
            for chunk in chunks:
                yield chunk
            return
        # masked: suppress EVERY text-carrying chunk (and the original [DONE]),
        # replay the rest verbatim, then the consolidated masked answer.
        # ZC-3 (audit 2026-10-29 + xv round 2): the LEAK is fixed by widening the
        # drop predicate, not by re-masking fragments. A streaming delta is a
        # FRAGMENT — a guard rule that fires on the assembled text ("your key is
        # ACME-SECRET-1") can miss both halves of a split secret, so any
        # per-fragment re-masking either leaks (fragment unchanged) or blocks
        # clean answers (tool data judged as assistant speech), and re-running
        # the engine per fragment amplifies one verdict into hundreds of engine
        # calls. The reviewer-mandated shape: drop text-bearing chunks WHOLE —
        # a chunk the classifier calls text-bearing is never replayed unmasked,
        # fail-closed on the channel the mask owns — and ship exactly one
        # consolidated final carrying the MASKED ANSWER ONLY (not reasoning, not
        # tool JSON, not pcai text: those are not the assistant's reply and the
        # old per-channel rewrite glued them into delta.content).
        # The drop set comes from the LINE CURSOR above (audit r5 #2c), not from a
        # per-chunk parse: a chunk that merely CARRIES HALF of a text-bearing
        # event is in `dirty` too, so a straddled event can never be reassembled
        # by the client from two "text-free" halves. The [DONE] drop is by INDEX
        # for the same reason (audit 2026-11-10 P2-19).
        # HANDBACK DOWNGRADE (audit w6, owner decision): a turn whose tool-call
        # deltas the mask dropped can no longer be a handback — the client would be
        # told to execute calls it never received. Drop the handback event and force
        # the terminal to finish_reason "stop", so the masked stream is a clean,
        # self-consistent completion. The arguments stay MASKED: never replay them.
        downgrade_handback = bool(tool_span & dirty)
        for i, chunk in enumerate(chunks):
            if i in dirty or i in done_span:
                continue
            if downgrade_handback and i in handback_span:
                continue
            if downgrade_handback and i in finish_span:
                rewritten = _downgrade_tool_calls_terminal(chunk)
                if rewritten is not None:
                    yield rewritten
                # an unparseable (straddled) terminal is dropped: the synthetic
                # masked final below supplies the one and only stop.
                continue
            yield chunk
        yield _sse_masked_final(model, request_id, guarded_text)
    finally:
        if release_slot is not None:
            release_slot()


async def _peek_first_data_line(
    resp: httpx2.Response,
    budget_s: float,
) -> tuple[bytes, AsyncIterator[bytes] | None]:
    """Wait up to `budget_s` for the upstream stream's FIRST SSE DATA line.

    THE DEFECT this exists for (operator-reproduced): SGLang/vLLM return
    200 headers IMMEDIATELY and then stall in queue (observed: 284s queue,
    41-deep, KV 93%). Pre-first-byte failover never fires — the gateway is
    committed to the wedged upstream and streams client keepalives until the
    600s upstream timeout. This gate bounds that commitment.

    A "data line" is a line whose stripped bytes start with `data:`
    (case-sensitive, per the SSE spec). Comment lines (`: keep-alive`),
    `event:` lines, and blank lines do NOT count: SGLang/vLLM emit
    keepalive comments while queued, and treating those as progress would
    reinstate exactly the hang this gate removes. `[DONE]` DOES count — a
    stream that ends instantly is not stalled.

    Returns `(peeked_bytes, remaining_chunks)`:
      * on a data line inside the budget — every byte read so far, plus the
        iterator to CONTINUE reading (httpx2 streams are one-shot: a fresh
        `aiter_bytes()` would raise StreamConsumed), so the caller threads
        both into `_relay_sse` and nothing is lost;
      * on a stream that ENDED cleanly with no data line (an empty 2xx, or
        a 2xx carrying a non-SSE body) — the bytes read so far plus the
        exhausted iterator: the caller relays them verbatim, exactly the
        legacy outcome, because a stream that already finished is not
        stalled and must not be re-rolled;
      * on `(b"", None)` — the budget expired: the caller treats the
        attempt as failover-eligible (nothing was delivered).

    Reads are individually wrapped in `asyncio.wait_for`, so a read that
    never returns raises TimeoutError at the deadline rather than hanging.
    """
    deadline = time.monotonic() + budget_s
    # EVERY byte read here is replayed to the client on success (the
    # byte-identity contract: a gated stream must equal the ungated one), so
    # `raw` accumulates verbatim and `scan` is a separate cursor over it —
    # the keepalive comments consumed while waiting are kept, not dropped.
    raw = bytearray()
    scan = 0
    # httpx2's runtime object IS an async generator (the BaseException guard
    # below acloses it) — but the two httpx2 versions this repo is checked
    # against annotate aiter_bytes() differently (AsyncGenerator vs plain
    # AsyncIterator), so the aclose goes through an explicit narrowing cast
    # instead of inferring from either annotation.
    chunks = cast("AsyncGenerator[bytes, None]", resp.aiter_bytes())
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return b"", None
            try:
                chunk = await asyncio.wait_for(chunks.__anext__(), remaining)
            except TimeoutError:
                return b"", None
            except StopAsyncIteration:
                # Upstream finished with no data line (empty 2xx / non-SSE
                # body): nothing to wait for — hand the bytes back for the
                # verbatim relay.
                return bytes(raw), chunks
            raw += chunk
            # Same line-cursor semantics as _relay_sse: a data line may
            # straddle two network reads — and (audit C-7) a terminator may
            # be a bare CR, not only LF; a CR-only stream would otherwise
            # never produce a "data line" here and every attempt would wait
            # out the whole TTFT budget before failing over.
            nl = raw.find(b"\n", scan)
            cr = raw.find(b"\r", scan)
            while nl != -1 or cr != -1:
                idx = cr if (cr != -1 and (nl == -1 or cr < nl)) else nl
                if bytes(raw[scan:idx]).strip().startswith(b"data:"):
                    return bytes(raw), chunks
                scan = idx + 1
                nl = raw.find(b"\n", scan)
                cr = raw.find(b"\r", scan)
    except BaseException:
        # The peek must never leak the upstream stream on an unexpected
        # failure (cancellation included) — the caller owns the close for
        # the paths it handles.
        with anyio.CancelScope(shield=True):
            await chunks.aclose()
        raise


async def _stream_chat(
    pool: Pool,
    ep: Endpoint,
    url: str,
    body: dict[str, Any],
    headers: dict[str, str],
    key: KeyRecord,
    model: str,
    est_tokens: int,
    request_id: str,
    started: float,
    release_slot: Callable[[], None] | None = None,
    post_guards: list[Any] | None = None,
    pre_events: list[dict[str, Any]] | None = None,
    route: model_routing.RouteDecision | None = None,
    fallback_from: str = "",
    fallback_chain: list[str] | None = None,
    affinity_session: str | None = None,
    group_name: str | None = None,
) -> Response:
    """Open an upstream SSE stream and wrap it for pass-through relay.

    `route` (chat path): the X-PCAI-Route header goes out with the stream
    HEADERS (the last moment anything but stream bytes can be added) and
    rides the ledger row the stream settles in its finally block.

    `fallback_from` (review §4a): the model this request failed over FROM
    at pick time (pool.pick()==None on the primary and a chain candidate
    answered). Announced via X-PCAI-Fallback on the stream HEADERS — that
    is the last mutable moment; a plain stream never switches models after
    its first byte (mid-stream dies as today). The metadata BODY block
    never rides a stream: transparent SSE, body mutation is opt-in and
    non-streaming-only.

    `fallback_chain` (review §4a): walk candidates ONLY while no byte has
    been sent — a transport failure or a 5xx error response (relayed as a
    non-SSE body) happens before the stream opens, so both are eligible;
    once `client.send` yields a 2xx and bytes start flowing, mid-stream
    death is exactly today's behavior (the ledger settles the partial
    spend; no model switch can rewrite bytes already delivered).

    TTFT gate (failover is "pre-first-CLIENT-byte", not "pre-first-HEADER"):
    a 2xx response is NOT commitment. SGLang/vLLM answer 200 immediately and
    then sit in queue, so after the stream_options negotiation this function
    peeks the upstream stream for its first SSE DATA line under the
    `settings.failover_ttft_s` budget (default 45s; <= 0 disables the gate
    entirely — the byte-identical legacy path). Keepalive COMMENT lines
    (`: keep-alive`) do not satisfy the gate: they are exactly what a queued
    upstream emits while stalled. On budget expiry the attempt is
    failover-eligible — the upstream never produced content, no client byte
    was ever sent, so the breaker report + error settle + chain walk run
    exactly like the pre-open failure paths, and the connection close
    cancels the upstream's queued sequence (SGLang client-disconnect
    semantics), so the gate RELIEVES queue pressure instead of feeding it.
    The peeked bytes are replayed as `_relay_sse(prefix=...)`, so the client
    sees the upstream stream byte-for-byte.

    `affinity_session` / `group_name` (F3): the conversation's affinity key
    and the model group this request named, if any. They arm the
    COMPLETED-stream pin refresh — the streaming half of the non-streaming
    commit block — so a pinned conversation no longer silently loses its
    pin after the TTL just because its turns streamed. The refresh runs in
    the ledger's finish (shielded, after settle), only when the stream
    actually completed; a disconnect leaves the pins untouched.
    """
    off_base = offpeak.key_fraction(key)  # profile fraction ?? env default
    # Ask the upstream for REAL usage so the ledger settles facts, not
    # estimates (the same contract the relay's stream_once applies): without
    # include_usage a plain stream settles on the token estimate with
    # cached_tokens=0 — the "cache hits invisible" accounting gap. A client
    # that already sent stream_options keeps theirs verbatim.
    chain = list(fallback_chain or [])
    cur_pool, cur_ep, cur_url, cur_headers = pool, ep, url, headers
    cur_model = model
    # TTFT peek output for the attempt that BREAKS out of this loop (the one
    # that will actually serve): the bytes already read off the wire plus the
    # iterator to continue reading them (httpx2 streams are one-shot).
    peeked = b""
    peek_chunks: AsyncIterator[bytes] | None = None
    while True:
        send_body = body if body.get("stream_options") else {**body, "stream_options": {"include_usage": True}}
        # Per-model max_tokens ceiling: clamp the body we actually SEND. It is
        # rebuilt each iteration, so this must sit AFTER the rebuild — and
        # `cur_model` is this attempt's model (a failover hop gets ITS
        # ceiling). The client's `body` stays untouched for the settle.
        send_body = clamp_max_tokens(
            wire_compat.normalize_system_messages(send_body),
            cur_model,
            settings.max_tokens_ceiling,
            context_limit=_context_limit_for(cur_ep),
        )
        # C-6 (audit 2026-10-29): same rewrite as the non-streaming path —
        # every attempt names ITS model (this loop IS the failover walk).
        if send_body.get("model") != cur_model:
            send_body = {**send_body, "model": cur_model}
        req = model_router.client.build_request("POST", cur_url, json=send_body, headers=cur_headers)
        try:
            resp = await model_router.client.send(req, stream=True)
        except httpx2.HTTPError:
            # Connection refused / timeout / TLS before ANY byte was sent:
            # failover-eligible (review §4a — failover only pre-first-byte).
            latency = _latency_ms(started)
            await cur_pool.report(cur_ep, False, error="HTTPError")
            await settle(key.id, cur_model, "chat", 0, 0, "error", latency, request_id)
            _log_request(key.key_prefix, cur_model, cur_ep, "error", latency, request_id)
            nxt, chain = await _next_fallback_candidate(chain, cur_model, key, "chat", est_tokens, request_id, started)
            if nxt is None:
                if release_slot is not None:
                    release_slot()
                raise fastapi.HTTPException(502, "upstream request failed") from None
            cur_pool, cur_ep, cur_url, _ = nxt
            cur_headers = dict(_upstream_headers(cur_ep, request_id))
            fallback_from = fallback_from or cur_model
            cur_model = cur_ep.model_name
            continue
        if resp.status_code >= 400 and "stream_options" in send_body:
            # Some upstreams reject stream_options outright — retry the identical
            # call WITHOUT it before surfacing the error (the settle then falls
            # back to the estimate); the same negotiation stream_once applies to
            # every relayed call.
            await resp.aread()
            await resp.aclose()
            retry_body = {k: v for k, v in send_body.items() if k != "stream_options"}
            req = model_router.client.build_request("POST", cur_url, json=retry_body, headers=cur_headers)
            try:
                resp = await model_router.client.send(req, stream=True)
            except httpx2.HTTPError:
                latency = _latency_ms(started)
                await cur_pool.report(cur_ep, False, error="HTTPError")
                await settle(key.id, cur_model, "chat", 0, 0, "error", latency, request_id)
                _log_request(key.key_prefix, cur_model, cur_ep, "error", latency, request_id)
                nxt, chain = await _next_fallback_candidate(
                    chain, cur_model, key, "chat", est_tokens, request_id, started
                )
                if nxt is None:
                    if release_slot is not None:
                        release_slot()
                    raise fastapi.HTTPException(502, "upstream request failed") from None
                cur_pool, cur_ep, cur_url, _ = nxt
                cur_headers = dict(_upstream_headers(cur_ep, request_id))
                fallback_from = fallback_from or cur_model
                cur_model = cur_ep.model_name
                continue

        # TTFT gate (failover is "pre-first-CLIENT-byte", not
        # "pre-first-HEADER"): a 2xx is NOT commitment — SGLang/vLLM answer
        # 200 immediately and stall in queue. Peek for the first SSE DATA
        # line under the budget; keepalive COMMENT lines do not count (they
        # are exactly what a queued upstream emits while stalled). On expiry
        # the upstream never produced content and no client byte was sent,
        # so this is failover-eligible exactly like the paths above — and
        # the aclose() cancels the upstream's queued sequence (SGLang
        # client-disconnect semantics), so the gate RELIEVES queue pressure
        # instead of feeding it. budget <= 0 disables the gate ENTIRELY
        # (byte-identical legacy path). Only a 2xx has an SSE body to peek:
        # an error response is read whole by the branch below.
        if _is_2xx(resp.status_code):
            ttft_budget_s = float(getattr(settings, "failover_ttft_s", 45.0))
            if ttft_budget_s > 0:
                try:
                    peeked, peek_chunks = await _peek_first_data_line(resp, ttft_budget_s)
                except httpx2.HTTPError:
                    # Review-found defect: the upstream can DIE mid-peek
                    # (httpx2.ReadError — e.g. SGLang dropping the request
                    # while queued). Without this handler the exception
                    # escapes _stream_chat: no breaker report, NO settle
                    # row, no chain walk — the exact failures this gate
                    # exists to deliver. Same disposition as a budget
                    # expiry: the upstream delivered nothing and no client
                    # byte was sent.
                    peeked, peek_chunks = b"", None
                if peek_chunks is None:
                    await resp.aclose()
                    latency = _latency_ms(started)
                    await cur_pool.report(cur_ep, False, error="ttft budget exceeded")
                    await settle(key.id, cur_model, "chat", 0, 0, "error", latency, request_id)
                    _log_request(key.key_prefix, cur_model, cur_ep, "error", latency, request_id)
                    nxt, chain = await _next_fallback_candidate(
                        chain, cur_model, key, "chat", est_tokens, request_id, started
                    )
                    if nxt is None:
                        if release_slot is not None:
                            release_slot()
                        raise fastapi.HTTPException(502, "upstream request failed") from None
                    cur_pool, cur_ep, cur_url, _ = nxt
                    cur_headers = dict(_upstream_headers(cur_ep, request_id))
                    fallback_from = fallback_from or cur_model
                    cur_model = cur_ep.model_name
                    continue
        break

    if not _is_2xx(resp.status_code):
        # Read the error body fully, close the stream context, and relay it
        # as an ordinary (non-SSE) response with the upstream status code.
        # A failover-eligible status with chain left walks pre-first-byte
        # (review §4a): the error body never reached the client, so the
        # switch is invisible today and survivable with the chain.
        # Eligible = a 5xx, or a 4xx the helper attributes to the MODEL
        # itself (same policy as _proxy_json — one predicate, both sites).
        payload = await resp.aread()
        await resp.aclose()
        latency = _latency_ms(started)
        await cur_pool.report(cur_ep, False, error=f"upstream status {resp.status_code}")
        await settle(key.id, cur_model, "chat", 0, 0, "error", latency, request_id)
        _log_request(key.key_prefix, cur_model, cur_ep, "error", latency, request_id)
        if (
            500 <= resp.status_code < 600 or _is_model_attributable_4xx(resp.status_code, payload, cur_model)
        ) and chain:
            nxt, chain = await _next_fallback_candidate(chain, cur_model, key, "chat", est_tokens, request_id, started)
            if nxt is not None:
                cur_pool, cur_ep, cur_url, _ = nxt
                cur_headers = dict(_upstream_headers(cur_ep, request_id))
                fallback_from = fallback_from or cur_model
                cur_model = cur_ep.model_name
                return await _stream_chat(
                    cur_pool,
                    cur_ep,
                    cur_url,
                    body,
                    cur_headers,
                    key,
                    cur_model,
                    est_tokens,
                    request_id,
                    started,
                    release_slot=release_slot,
                    post_guards=post_guards,
                    pre_events=pre_events,
                    route=None,
                    fallback_from=fallback_from,
                    fallback_chain=chain,
                    # PIN CONTEXT MUST SURVIVE THE HOP (audit 2026-11-10 P3):
                    # the recursion used to drop both, so a failover turn
                    # served by a chain candidate never refreshed the
                    # conversation's endpoint/group pins — the streaming
                    # pin-refresh silently stopped working for exactly the
                    # turns that moved models. Threaded through unchanged.
                    affinity_session=affinity_session,
                    group_name=group_name,
                )
        if release_slot is not None:
            release_slot()
        return Response(
            content=payload,
            status_code=resp.status_code,
            media_type=resp.headers.get("content-type", "application/json"),
            headers=_relay_headers(
                cur_headers,
                request_id,
                offpeak.effective_for(cur_model, off_base),
                model_routing.route_header(route if cur_model == model else None, cur_model),
                served_model=cur_model,
                endpoint_id=cur_ep.id,
                fallback_from=fallback_from,
            ),
        )

    await cur_pool.report(cur_ep, True)

    async def _refresh_pins() -> None:
        """Pin refresh after a COMPLETED stream (see _pins_after_serve).

        The closure reads the FINAL attempt's facts (cur_ep / cur_model /
        fallback_from are the values of the attempt that is serving now),
        so a mid-walk hop is reflected instead of the original pick.
        """
        await _pins_after_serve(
            key,
            affinity_session,
            group_name,
            endpoint_id=cur_ep.id,
            model=cur_model,
            fallback_from=fallback_from,
        )

    ledger = _StreamLedger(
        key,
        cur_model,
        est_tokens,
        request_id,
        started,
        off_base,
        pre_events=pre_events,
        route=route,
        on_complete=_refresh_pins if (affinity_session or group_name is not None) else None,
    )
    stream_headers = _relay_headers(
        cur_headers,
        request_id,
        offpeak.effective_for(cur_model, off_base),
        model_routing.route_header(route if cur_model == model else None, cur_model),
        served_model=cur_model,
        endpoint_id=cur_ep.id,
        fallback_from=fallback_from,
    )
    stream_headers["Cache-Control"] = "no-cache"
    if post_guards:
        # Buffered guard path: the wrapper settles the ledger (defer_finish)
        # AFTER judging the assembled text, so pre+post verdicts land on ONE
        # row and a blocked stream never delivers its content.
        ledger.defer_finish = True
        return _SlotReleasingStreamingResponse(
            _guarded_sse(
                _relay_sse(
                    resp,
                    ledger,
                    release_slot=release_slot,
                    defer_finish=True,
                    prefix=peeked,
                    chunks=peek_chunks,
                ),
                ledger,
                post_guards,
                key=key,
                model=cur_model,
                request_id=request_id,
                started=started,
                release_slot=release_slot,
            ),
            media_type="text/event-stream",
            headers=stream_headers,
            release_slot=release_slot,
        )
    return _SlotReleasingStreamingResponse(
        _relay_sse(resp, ledger, release_slot=release_slot, prefix=peeked, chunks=peek_chunks),
        media_type="text/event-stream",
        headers=stream_headers,
        release_slot=release_slot,
    )


# --------------------------------------------------------------------------
# routes
# --------------------------------------------------------------------------


async def _relay_catalog(
    key: KeyRecord,
    relay_grants: set[tuple[str, str]],
    requested_servers: set[str] | None = None,
) -> tuple[list[dict[str, Any]], set[tuple[str, str]]]:
    """Build the relay/approval-passthrough tool catalog for a key.

    Returns (catalog, effective_grants). The catalog merges the live tool
    lists (TTL-cached) of every relevant server — filtered by the per-server
    tool policy — with the key's grant set: explicit ACL rows plus, in
    'scope' grant mode, every policy-admitted tool of enabled servers for
    mcp-scoped keys. `requested_servers` (the tools:[{type:"mcp","server":…}]
    opt-in) narrows everything to those servers; an unknown name is a 400
    (fail loud beats a silent skip) and a fully-empty result is a 400 that
    points at grants. Entries carry inputSchema when the upstream declares
    one (client-side execution needs the real argument schema) and
    annotations when it declares those — both are part of the definition
    core.tool_pin fingerprints, so neither may be dropped in transit.

    FIX (was latent in the inline version): the old loop iterated only
    EXPLICITLY granted servers, so a scope-mode key with no explicit ACL
    rows fetched no catalogs and relayed with an empty toolset — silently
    contradicting the documented scope semantics. Scope mode now sweeps all
    enabled (or requested) servers.
    """
    from ..mcpf.federation import load_denies, server_allows
    from .routes_mcp import _fetch_tools, build_mcp_headers

    servers = {sv["name"]: sv for sv in await registry.mcp_servers()}
    if requested_servers:
        unknown = sorted(requested_servers - set(servers))
        if unknown:
            raise fastapi.HTTPException(400, f"requested MCP server(s) not registered: {', '.join(unknown)}")
    scope_mode = settings.tool_relay_grant_mode == "scope" and "mcp" in key.scopes
    if scope_mode:
        wanted = sorted(requested_servers) if requested_servers else sorted(servers)
    else:
        granted_servers = {srv for srv, _ in relay_grants}
        wanted = sorted(granted_servers & requested_servers) if requested_servers else sorted(granted_servers)

    catalog: list[dict[str, Any]] = []
    for server_name in wanted:
        target = servers.get(server_name)
        if target is None:
            continue
        # Catalog TTL cache: the preflight fan-out ran on EVERY relay
        # request (sequential per server, each an MCP round-trip) —
        # handback-heavy agent loops paid ~1-2s per hop before the first
        # token. Tool catalogs change on operator action, not per request;
        # 120s staleness is invisible next to that. On a failed refresh the
        # stale entry still serves (better an old catalog than no catalog).
        cached = _catalog_cache.get(server_name)
        if cached is not None and time.monotonic() - cached[0] < _CATALOG_TTL_S:
            tools = cached[1]
        else:
            try:
                tools = await _fetch_tools(
                    target["url"],
                    build_mcp_headers(
                        target.get("headers", {}), target.get("auth_secret"), target.get("auth_token_enc")
                    ),
                    timeout_s=15.0,  # metadata call — bounded (audit: a hung
                    # MCP server froze the relay's first request
                    # for the full 600s upstream timeout)
                )
                _catalog_cache[server_name] = (time.monotonic(), tools)
            except Exception:
                log.warning("relay: catalog fetch for %r failed", server_name)
                if cached is not None:
                    tools = cached[1]  # stale beats absent
                else:
                    continue
        catalog.extend(
            {
                "server": server_name,
                "tool": t["tool"],
                "description": t.get("description", ""),
                # The SERVER's own description rides on every entry so the
                # hybrid surface can render a server directory without a
                # second lookup (2026-10-28). Carried per-entry rather than
                # as a third return value: _relay_catalog has two callers and
                # a tuple change would ripple through both for a display string.
                "server_description": target.get("description", "") or "",
                **({"inputSchema": t["inputSchema"]} if isinstance(t.get("inputSchema"), dict) else {}),
                # ANNOTATIONS ride too (2026-11-10, tool-trust hardening):
                # destructiveHint/readOnlyHint are SECURITY-relevant — a
                # rug-pull can flip them without touching name, description or
                # schema — and tool_pin.fingerprint() pins all four fields, so
                # dropping them HERE left the one field the defence most needs
                # unpinned. Additive key: consumers that do not know it ignore
                # it, and a server that declares none adds nothing (absent and
                # empty pin the same definition).
                **({"annotations": t["annotations"]} if isinstance(t.get("annotations"), dict) else {}),
            }
            for t in tools
            if server_allows(target, t["tool"])
        )
    if scope_mode:
        # scope-mode: the policy-admitted catalog IS the effective grant
        # set — explicit grants merge in on top, THEN deny rows subtract
        # LAST (audit D-F1): under the default scope mode the union would
        # otherwise re-add pairs the operator explicitly denied, making
        # disable-all a silent no-op for mcp-scoped keys. Denied pairs are
        # therefore removed from the FINAL merged set, exactly the endorsed
        # design (relay_grants | scope_pairs) - denies.
        relay_grants = (relay_grants | {(t["server"], t["tool"]) for t in catalog}) - await load_denies(key.id)
    catalog = [t for t in catalog if (t["server"], t["tool"]) in relay_grants]
    if requested_servers and not catalog:
        raise fastapi.HTTPException(
            400,
            f"no MCP tools granted for requested server(s) {', '.join(sorted(requested_servers))}"
            " — ask the operator for grants, or drop the server filter",
        )
    return catalog, relay_grants


def _persist_conversation_state(
    key: KeyRecord,
    canonical_input: list[dict[str, Any]] | None = None,
    relay_projection: list[dict[str, Any]] | None = None,
):
    """The relay's handback-terminal persistence hook. Returns an
    async callback that saves the AWAITING-CLIENT tail (the relay loop's
    appended messages: the assistant tool_calls + in-loop results) to the
    pending column WITH the iteration count this request's loop reached —
    the durable half of per-turn loop accounting (the next request reads it
    back as iterations_prior ONLY if it is itself a handback continuation;
    a fresh user turn resets the budget — see _reconcile_relay_messages)
    and the substrate the X-PCAI-Continue
    result-only protocol stitches canonical + pending + client results
    from.

    The input boundary is passed IN, not re-derived, because with the
    context budget ON the list the relay loop saw is the TRIMMED projection,
    not the stored canonical list (audit C-4): the old prefix check
    (`final[:len(stored)] == stored`) compared across those two shapes —
    once the budget dropped anything, the store was no longer a prefix of
    what the loop appended to, the flush silently skipped, handback state
    was never persisted and X-PCAI-Continue 400'd for the whole turn.
      * `canonical_input` — the UNTRIMMED reconciled list, i.e. the same
        content the canonical column was saved with; the stored-vs-adopted
        comparison (a real race detector) still runs against it;
      * `relay_projection` — the exact list the relay loop was handed and
        appends to (trimmed or not); the tail slice starts at ITS length.

    The relay hands over its FULL final list (it doesn't know where the
    reconciled input ended); the pending column stores ONLY the tail beyond
    that input — storing the full list duplicates the prefix and the
    result-only merge's shape check then ALWAYS fails (2026-09-25 live
    report: X-PCAI-Continue 400'd on every handback because the merged tail
    began with the re-appended system message).

    Bounded like every persistence path: a failure is logged and
    swallowed (never breaks the turn); the escape hatch
    (X-PCAI-Conversation: raw / canonical_conversations=false) keeps the
    store untouched, matching reconciliation's posture."""

    async def _save(thread_id: str, final_messages: list[dict[str, Any]], iterations_final: int) -> None:
        if not thread_id or not settings.canonical_conversations:
            return  # flag-off = byte-inert: no canonical SQL at all
        try:
            stored = await canonical.load(db, key.id, thread_id)
        except Exception as exc:
            # Cannot read the input boundary -> cannot slice -> persist
            # NOTHING rather than a wrong shape: an absent pending fails
            # the next continuation LOUD ("no awaiting handback state");
            # a wrong pending fails with a confusing prefix mismatch.
            log.warning("canonical: pending-tail flush skipped (store read failed): %s", exc)
            return
        if stored is None:
            # No stored row (flag flipped mid-conversation, pruned row):
            # the reconciled input this turn started from is unknown — the
            # same posture: skip, never guess the boundary.
            log.info("canonical: pending-tail flush skipped (no stored canonical row thread=%s)", thread_id)
            return
        if canonical_input is not None and stored != canonical_input:
            # The store moved under us (a racing request reconciled a
            # different shape): the slice would be a guess — skip loudly.
            # (Compared against the UNTRIMMED list this request relayed and
            # saved — see the docstring: the trimmed projection is a
            # different shape, and comparing across them is what broke the
            # flush entirely whenever the context budget dropped anything.)
            log.warning(
                "canonical: pending-tail flush skipped (stored canonical is not a prefix of the final list, thread=%s)",
                thread_id,
            )
            return
        if relay_projection is not None:
            # The relay loop appended to THIS list (its own copy): the tail
            # starts at its length. The extend check guards a relay that
            # shrank or rebuilt the list — not a shape the flush can slice.
            n = len(relay_projection)
            extends = len(final_messages) >= n and final_messages[:n] == relay_projection
        else:
            # Legacy default (no projection passed): the historical
            # full-prefix check.
            n = len(stored)
            extends = not (n > len(final_messages) or final_messages[:n] != stored)
        if not extends:
            log.warning(
                "canonical: pending-tail flush skipped (final list does not extend the relayed input, thread=%s)",
                thread_id,
            )
            return
        pending_tail = final_messages[n:]
        if not pending_tail:
            # Degenerate: the loop ended with nothing appended. Clear any
            # stale pending rather than persist an empty awaiting state.
            await canonical.save_pending(db, key.id, thread_id, [], iterations_final)
            return
        # Handback-only flush: the AWAITING-CLIENT tail goes to the pending
        # column (the canonical messages column is untouched — a full-history
        # client's re-send keeps reconciling byte-stable), and the iteration
        # count rides the same UPDATE: the durable half of per-turn loop
        # accounting (the next request resumes the guard from here ONLY if
        # it is itself a handback continuation; a fresh user turn resets).
        await canonical.save_pending(db, key.id, thread_id, pending_tail, iterations_final)

    return _save


async def _reconcile_relay_messages(
    request: Request,
    key: KeyRecord,
    body: dict[str, Any],
    reserve_tokens: int = 0,
) -> tuple[list[dict[str, Any]], str, dict[str, Any] | None, int, list[dict[str, Any]]]:
    """Canonical conversation reconciliation for the relay path (ROADMAP §7,
    phase 1) — returns (messages_to_relay, mode, continuation, iterations_prior,
    canonical_input).

    The fifth value is the UNTRIMMED reconciled list — the same list the
    store was (or would have been) written with. It is the input boundary
    the handback-terminal flush slices at (audit C-4): deriving that
    boundary from a prefix comparison against the TRIMMED projection broke
    the flush whenever the context budget dropped anything.

    WHY: a relay HANDBACK ends the stream with finish_reason=tool_calls and
    the harness re-sends the ENTIRE accumulated conversation as a new
    request (a real DSH relay turn measured 214k input tokens). Reconciling
    each turn against the stored canonical list (core.canonical) makes the
    gateway the conversation's owner without any client change: the verdict
    (extend / reset / new / trimmed_incoming) is persisted and logged, and
    the byte-stable "extend" prefix is what keeps vLLM/SGLang APC hitting.

    Phase 2A: when the verdict is "extend" AND the appended tail is exactly
    an assistant tool_calls message followed by its role=tool results
    (canonical.detect_continuation), the request IS a handback CONTINUATION
    of the relay loop — the third return value then carries
    {"tool_call_ids", "result_count"} for relay_stream's trace linkage, and
    is None otherwise. Detection is advisory: it never changes what the
    upstream is asked, only how the relay labels the turn.

    Placement: runs AFTER the inbound reasoning strip and pre-call
    guardrails have settled body["messages"], so the store records exactly
    the shape the upstream is asked to consume (reasoning can never
    re-enter through the store — reconciliation only ever sees stripped
    data). It runs BEFORE relay_stream, so every downstream token estimate
    of the relayed call (the settle fallback, the context-budget trim)
    operates on the reconciled messages. The ADMISSION estimate (the
    budget/rate gates) deliberately stays on the raw incoming list: it has
    already run by then, and when the context budget trims, over-counting
    at admission is the conservative direction for a billing gate.

    The context budget (conversation_context_budget_tokens, 0 = off) trims
    the PROJECTION relayed upstream only — the store keeps the untrimmed
    reconciled conversation, so trimming never rewrites history and the
    next turn's prefix check stays stable.

    Fail-safe by construction (the feature must never break a relay turn):
    the per-request header X-PCAI-Conversation: raw and the fleet-wide
    canonical_conversations=false both skip everything (store untouched,
    incoming used as-is); a fingerprint-less request (no first user
    message) has no store key and passes through; ANY exception — DB down,
    corrupt row, a bug here — logs a warning and continues on the raw
    incoming messages, which in every reconcile mode is content-identical
    to what reconciliation would have adopted.
    """
    incoming = body["messages"]

    # --- Wave C: RESULT-ONLY continuation (the 214k-token re-send killer) ---
    # A client that read the handback event's "thread" ref may answer with
    # X-PCAI-Continue: <thread> + messages = ONLY the new tail (the
    # assistant tool_calls message + its role=tool results). The gateway
    # owns the canonical list; it merges, VERIFIES the continuation shape
    # against the store, and relays the merged conversation. Fail LOUD (400)
    # rather than silently mis-relaying: the client explicitly asked for
    # server-owned state, so a silent raw passthrough would mask a client
    # bug. Escape hatches: canonical_conversations=false disables the
    # protocol entirely (503); X-PCAI-Conversation: raw wins per request.
    continue_hdr = (request.headers.get("x-pcai-continue") or "").strip()
    if continue_hdr:
        if not settings.canonical_conversations:
            raise fastapi.HTTPException(
                503, "X-PCAI-Continue requested but canonical conversations are disabled on this gateway"
            )
        try:
            cont_thread: str = continue_hdr
            stored, iterations_prior = await canonical.load_row(db, key.id, cont_thread)
            if stored is None:
                raise fastapi.HTTPException(
                    400, f"X-PCAI-Continue: no server-owned conversation {cont_thread!r} for this key"
                )
            # The client sends ONLY the new role=tool results; the awaiting
            # state (assistant tool_calls + in-loop results) comes from the
            # store's pending tail. Verification: the merged list must be a
            # handback continuation of canonical (the SAME shape check the
            # full-history path uses) — fail loud, never silently relay.
            pending = await canonical.load_pending(db, key.id, cont_thread)
            if pending is None:
                raise fastapi.HTTPException(
                    400,
                    "X-PCAI-Continue: no awaiting handback state for this conversation"
                    " (the last relay turn must have ended in a handback)",
                )
            merged = [*stored, *pending, *incoming]
            cont_detected, cont_shape = canonical.detect_continuation(stored, merged)
            if not cont_detected:
                raise fastapi.HTTPException(
                    400,
                    "X-PCAI-Continue: results are not a handback continuation of the stored conversation"
                    " (expected ONLY role=tool messages answering the handed-back calls)",
                )
            # Per-turn loop budget (2026-09-25, supersedes the wave-C
            # per-conversation accounting): this protocol is BY SHAPE a
            # handback continuation — the verification above proved this
            # request answers the previous handback — so the carried counter
            # resumes. A fresh user turn can never arrive through
            # X-PCAI-Continue (it would fail the shape check, 400).
            # Consume: the pending tail is now part of the conversation of
            # record (canonical adopts merged — client-authoritative results
            # reconcile the same way a full-history resend would).
            await canonical.save(db, key.id, cont_thread, merged, iterations_prior)
            budget = settings.conversation_context_budget_tokens
            relay_projection = merged
            if budget > 0:
                relay_projection, dropped = canonical.trim_to_budget(
                    merged,
                    budget,
                    # + reserve_tokens: gateway-authored text that rides the
                    # model call but is NOT in this list (an agent profile's
                    # system prompt, prepended by toolrelay). Without it the
                    # context budget under-counts by exactly the prompt size —
                    # the overflow class the budget exists to prevent.
                    lambda msgs: _estimate_tokens({**body, "messages": msgs}) + reserve_tokens,
                )
                if dropped:
                    log.info(
                        "canonical: trimmed %d message(s) to the %d-token context budget (thread=%s)",
                        dropped,
                        budget,
                        cont_thread,
                    )
            log.info(
                "canonical: result-only continuation merged %d+%d+%d messages thread=%s",
                len(stored),
                len(pending),
                len(incoming),
                cont_thread,
            )
            return relay_projection, "continued", cont_shape, iterations_prior, merged
        except fastapi.HTTPException:
            raise
        except Exception as exc:
            log.warning("canonical: continuation merge failed (continuing on raw incoming): %s", exc)
            return incoming, "raw", None, 0, incoming

    escape = (request.headers.get("x-pcai-conversation") or "").strip().lower()
    if escape == "raw" or not settings.canonical_conversations:
        return incoming, "raw", None, 0, incoming
    # TITLE-REQUEST EXCLUSION (2026-10-17): a session-title call must never
    # become a canonical row. The relay is engaged for DSH title calls (its
    # provider config sends X-PCAI-Execute-Tools: 1), so this path captures
    # them just as the plain-chat path does — both need the guard, or one
    # store keeps feeding the distiller. Returning the "raw" mode leaves the
    # relay loop's behavior byte-identical while writing nothing.
    if settings.canonical_skip_title_requests and title_requests.is_title_request(
        {"messages": incoming}, request.headers
    ):
        log.debug("canonical: skipped session-title request (relay path)")
        return incoming, "raw", None, 0, incoming
    try:
        thread_id = canonical.thread_key(incoming, key.id)
        if thread_id is None:
            return incoming, "raw", None, 0, incoming  # no first user message -> nothing to key the store on
        stored, iterations_prior = await canonical.load_row(db, key.id, thread_id)
        # ONE hash walk per turn (audit P2-3 follow-up): the prefix verdict is
        # computed here and consumed by BOTH reconcile (mode decision) and, on
        # the extend path, detect_continuation — previously each ran its own
        # full walk over the canonical prefix (two serialization passes on a
        # 214k-token conversation). This only works because prefix_hashes
        # returns [] for a CLEAN prefix and reserves None for "not computed"
        # (audit 2026-11-10 P1-7): while both meant None, every healthy extend
        # turn re-walked the history three times.
        _prefix = canonical.prefix_hashes(stored, incoming) if stored else None
        messages, mode = canonical.reconcile(stored, incoming, prefix_verdict=_prefix)
        # Phase 2A: an "extend" whose new tail is only the handback's
        # assistant tool_calls + their role=tool results is a handback
        # continuation — surface it for the relay loop's trace linkage.
        # The BOOL decides: detect_continuation's negative return carries a
        # (truthy) shaped-empty info dict, so the info alone must never be
        # trusted as the verdict.
        detected, cont_info = (
            canonical.detect_continuation(stored, incoming, prefix_verdict=_prefix)
            if mode == canonical.MODE_EXTEND
            else (False, None)
        )
        continuation = cont_info if detected else None
        # Per-turn loop budget (2026-09-25, supersedes the wave-C
        # per-conversation accounting): the guard resumes the stored counter
        # ONLY when this request IS a handback continuation — same turn,
        # same budget. Any OTHER shape (a new user message: mode reset /
        # new / trimmed_incoming, or an extend that is not purely the
        # handback's results tail) is a FRESH TURN and starts the budget at
        # 0. The tool-calls-in-a-conversation exhaustion report
        # ("max tool iterations (16) reached" firing repeatedly in one long
        # discussion) was this counter accumulating across turns — the cap
        # was meant to bound ONE turn's relay loop, and per-turn is also
        # where the runaway protection actually lives: the wall clock,
        # identical-call loop detection, and the per-call ledger gates all
        # still bound every request, so a long discussion can no longer
        # starve a legitimate new question's tool budget.
        iterations_for_guard = iterations_prior if (detected and continuation is not None) else 0
        relay_projection = messages
        budget = settings.conversation_context_budget_tokens
        if budget > 0:
            relay_projection, dropped = canonical.trim_to_budget(
                messages,
                budget,
                # + reserve_tokens — see the twin call above (agent prompt).
                lambda msgs: _estimate_tokens({**body, "messages": msgs}) + reserve_tokens,
            )
            if dropped:
                log.info(
                    "canonical: trimmed %d message(s) to the %d-token context budget (thread=%s)",
                    dropped,
                    budget,
                    thread_id,
                )
        # Persist the reconciled (untrimmed) conversation — the store is the
        # owner of record; the budget only shapes what the model sees. The
        # iteration column records the counter THIS request's guard ran
        # under (resumed for a continuation, 0 for a fresh turn): the
        # terminal flush below overwrites it with the count the loop
        # actually reached.
        await canonical.save(db, key.id, thread_id, messages, iterations_for_guard)
        log.info(
            "canonical: reconciled relay conversation mode=%s messages=%d thread=%s%s",
            mode,
            len(messages),
            thread_id,
            " handback-continuation" if detected else "",
        )
        return relay_projection, mode, continuation, iterations_for_guard, messages
    except Exception as exc:
        log.warning("canonical: reconciliation failed (continuing on raw incoming messages): %s", exc)
        return incoming, "raw", None, 0, incoming


_capture_tasks: set[asyncio.Task] = set()
_CAPTURE_TASKS_MAX = 64  # bound concurrent captures (DeepSeek-V4.1 P1): a burst
# must not spawn unbounded DB writers against the shared Postgres; over the
# cap the capture is skipped (bookkeeping never justifies pool exhaustion)


async def _capture_chat_canonical(key: KeyRecord, body: dict[str, Any], raw_header: str, headers: Any = None) -> None:
    """Canonical capture for the PLAIN chat path (non-relay agents: raw SDK
    clients, opencode, OWUI raw mode). Mirrors the relay path's store
    semantics with the same reconcile verdicts — but captures on INPUT only:
    OpenAI-compatible clients resend their full history every turn, so the
    stored row converges to the complete conversation (user AND assistant
    turns) by the next request, with zero response-side coupling and no
    streaming/guardrail interference.

    Fail-safe by construction (the canonical module contract): a capture
    failure is a WARNING, never a request error - the store is bookkeeping,
    never a rewrite. Fire-and-forget from chat_completions."""
    if not settings.canonical_conversations:
        return
    incoming = body.get("messages") or []
    # the escape hatch is absolute (the relay path's own contract): a client
    # that opts out of canonical handling opts out of CAPTURE too
    if raw_header.strip().lower() == "raw":
        return  # the escape hatch is absolute: opt out of capture entirely
    # TITLE-REQUEST EXCLUSION (2026-10-17): an auxiliary session-title call is
    # a real request but NOT a conversation. Capturing one makes the distiller
    # write a bogus "session summary" whose whole content is a title string
    # (DSH fires one after EVERY prompt, so this was one memory per prompt).
    # Clean no-op, same best-effort posture as every other skip here.
    if settings.canonical_skip_title_requests and title_requests.is_title_request(body, headers):
        log.debug("canonical: skipped session-title request (plain chat path)")
        return
    try:
        thread_id = canonical.thread_key(incoming, key.id)
    except Exception:
        return  # fingerprint fault: skip silently (admission already logged its own)
    if not thread_id:
        return  # no user-message seed: nothing to fingerprint
    try:
        stored, iterations_prior = await canonical.load_row(db, key.id, thread_id)
        merged, _mode = canonical.reconcile(stored, incoming)
        # EXTEND-ONLY GUARD (DeepSeek-V4.1 P1): input-only capture converges
        # only for clients that resend full history. A delta-only or
        # rolling-window client must never SHRINK a good stored row - when
        # the merged result is smaller than what's stored, leave the stored
        # row alone (the fragment tells us nothing the row doesn't know).
        if stored and len(merged) < len(stored):
            return
        # NO-OP EXTEND (audit 2026-11-10 P2-30): the client re-sent exactly the
        # stored history — a retry, a cache miss replayed, a client that simply
        # re-asks. reconcile's prefix hash walk has already PROVED
        # message-for-message equality (extend + equal length means every hash
        # matched), so the UPSERT would re-serialise a whole conversation JSONB
        # to write back the bytes already there. The iteration column is the
        # only other field and it is written from the value just READ, so
        # skipping the write loses nothing.
        if stored and _mode == canonical.MODE_EXTEND and len(merged) == len(stored):
            return
        await canonical.save(db, key.id, thread_id, merged, iterations_prior)
    except Exception:
        log.warning(
            "canonical: plain-chat capture failed (thread=%s) - continuing",
            thread_id[:16],
            exc_info=True,
        )


@router.post("/chat/completions")
async def chat_completions(
    request: Request,
    key: KeyRecord = fastapi.Depends(require_scope("proxy")),
) -> Response:
    """OpenAI-compatible chat proxy with the full gate/settle pipeline."""
    request_id = uuid.uuid4().hex
    started = time.monotonic()
    off_base = offpeak.key_fraction(key)  # profile fraction ?? env default

    body = await _json_body(request)  # bounded read (413) + JSON parse (400)
    if not isinstance(body, dict) or "model" not in body or "messages" not in body:
        raise fastapi.HTTPException(400, "chat request must be a JSON object with 'model' and 'messages'")
    model = str(body["model"])

    # --- model resolution: direct (pinned) or budget-driven cascade ---
    # resolve_cascade handles the allowed-models gate (403), registration
    # check (404), and picks the first profile-ordered model with budget
    # headroom when the key's profile has an ordering. Pinned requests to
    # available models never cascade.
    est_tokens = _estimate_tokens(body)
    registered_models = set(model_router.models())
    requested_model = model
    # --- model groups (F3): a group name is a REGISTERED, PINNED target ----
    # A group is a selection path over equivalent chat models: it classifies
    # and admits like a pinned model (never cascades), and the MEMBER chosen
    # below is what bills and what the ACL/budget gates see. Group names are
    # added to the CLASSIFICATION/ADMISSION set only — never to the
    # budget-capacity maps or the routing ladder (a group has no budget of
    # its own, and v1 keeps groups out of profile order/cascade).
    groups = await _groups_load()
    group_names = set(groups)
    registered = registered_models | group_names
    group_name: str | None = None

    # --- complexity routing (pseudo-models `routing` / `routing__X`) --------
    # Engages ONLY for those two name shapes, and even then only when the
    # key's profile opted in (routing_policy shadow/active with the master
    # switch on). 'off' profiles and direct model names pass through this
    # block byte-for-byte unchanged. The decision replaces the NAME only —
    # resolve_cascade below runs the normal pinned/budget machinery on it,
    # so budget gates, admission failover, and the mid-relay walk behave
    # exactly as they do today. Any engagement failure degrades to legacy
    # behavior (serve the name as it arrived), never fails the request.
    route_ctx: model_routing.RouteDecision | None = None
    _pseudo = model_routing.detect_pseudo_model(model)
    # route_default_model promotes the legacy `auto` alias into routing. Read
    # from the SAME cached routing config the decision seam below uses, so the
    # two can never disagree within one request. Fail-safe: a config fault
    # leaves auto on its documented cascade path (the pre-flag behaviour).
    _rdef = False
    _routable_models: list[str] | None = None
    try:
        from ..core import routing_config as _rc_early

        _routing_cfg = await _rc_early.get_config()
        _rdef = bool(_routing_cfg.get("route_default_model"))
        # The console allowlist is ELIGIBILITY; the profile ladder is capability
        # ORDER (core.model_routing.expand_candidates). Before this the panel
        # claimed to bound what the router could pick while the request path
        # never read it (defect, 2026-10-23). Empty/None = every chat model.
        _allow = _routing_cfg.get("allowed_models")
        _routable_models = [str(m) for m in _allow] if isinstance(_allow, list) and _allow else None
    except Exception:
        _rdef = False
        _routable_models = None
    _routable = model_routing.is_routable_name(model, _rdef)
    # AGENT CANDIDATES (2026-11-08): an agent is a hard model (tool surface +
    # pinned model/cascade) but it is PER-KEY (the profile's agent menu), so it
    # cannot live in the global allowlist. Fetch the key's OFFERED agents once,
    # only when routing is armed AND the flag is on; a fault degrades to no
    # agent candidates (fail-open — the router keeps its model ladder).
    _routing_agents: list[Any] = []
    if settings.routing_agents_enabled and _routable:
        try:
            _routing_agents = [a for a in await agents_mod.for_key(key.id) if a.offered]
        except Exception:
            log.warning("routing: agent candidates unavailable (fail-open)", exc_info=True)
            _routing_agents = []
    _agent_map = {a.name: a for a in _routing_agents}
    try:
        # The order is fetched here (not inside the router) so the no-ladder
        # special cases and the cascade reuse ONE lookup; resolve_cascade
        # re-fetches internally for its own walk (unchanged).
        order = await profile_order_for_key(key.id) if _routable else []
        # CHAT-ONLY LADDER (2026-10-29): the ordering is an ACL as well as a
        # ladder, so it can carry embedding/reranker pools. The router's ladder
        # is a CHAT concept — order[0] is the CEILING and reversed(order) the
        # cheap-first walk — so a non-chat head would pin every `routing`
        # request to a pool the chat path then 400s. Filtered HERE (one place,
        # both consumers) rather than inside core.model_routing, which is pure
        # and takes the ladder as given.
        if order:
            # MODEL GROUPS (2026-11-08) are valid ladder entries: a group is a
            # chat-capable target whose MEMBER is picked at request time, so
            # serves_chat(group) is False (it is not a pool) but it must stay
            # on the routing ladder. Pools still need the chat-type filter.
            order = [m for m in order if serves_chat(m) or _group_by_name(m) is not None]
        # GLOBAL LADDER (2026-10-28): the profile order is a CASCADE ordering;
        # a profile that declares none used to make bare `routing` 404
        # ("routing requires a profile model ordering"), i.e. the feature
        # silently did not exist for that profile. Fall back to the fleet-level
        # routing_config.model_order so routing is armed and usable once,
        # globally. `order` is consumed ONLY by resolve_routed_model below, so
        # the cascade walk and the relay fallback tail are untouched.
        if _routable and not order:
            order = await model_routing.global_ladder()
        # --- conversation incumbent -----------------------------------------
        # Read BEFORE the decision seam because the seam needs the
        # conversation's CURRENT model for two things:
        #   1. switch_cost/current_model in the decision state, so the model can
        #      weigh the re-prefill a switch would cost; and
        #   2. hysteresis, so a challenger must beat the incumbent by a margin.
        # Read UNCONDITIONALLY WITHIN THE ROUTING PATH -- hysteresis must work
        # even when never-downgrade (sticky_routing) is off, and that is the
        # default. Fail-open: a stickiness fault means "no incumbent".
        # The incumbent AND the never-downgrade floor are read here, ONCE, and
        # the sticky WRITER is built unconditionally. That last part is load-
        # bearing: this block is the only writer of a `routing:`-scoped row, so
        # gating it behind sticky_routing (default False) left _incumbent
        # permanently None and made the whole hysteresis feature inert in the
        # shipped default (dual review 2026-10-26, HIGH). Hysteresis is a COST
        # guard -- it must work without opting into never-downgrade.
        # The gate is the router's own _routable flag, NOT sticky_routing
        # (audit w6, P2): a DIRECT request never consults the router at all, so
        # its incumbent/floor/writer can never be read -- yet this ran on every
        # chat request and put one model_stickiness SELECT on the pre-upstream
        # (TTFT) path for nothing. A routing pseudo-model still reads it BEFORE
        # resolution, which is what the dual-review fix above requires.
        _incumbent: str | None = None
        _sticky_floor: str | None = None
        _sticky_write: Any = None
        if _routable:
            _incumbent, _sticky_floor, _sticky_write = await _routing_incumbent_state(key, body)

        # --- decision-model seam -------------------------------------------
        # Built only for a pseudo-model request on an opted-in profile: a
        # direct (pinned) request never consults the router at all, and
        # building the caller per request keeps the registry snapshot fresh.
        # decision_mode "off" (the default) passes decide=None, so this whole
        # block is inert and the heuristic path is byte-identical.
        decide_fn = None
        _policy: dict[str, Any] = {}
        if _routable:
            from ..core import routing_config as _rc

            _policy = await _rc.decision_settings()
            if _policy["mode"] in ("shadow", "active") and _policy["model"]:
                # P10: the calling key + this request's id ride along so the
                # decision call can settle its own status='routing' row under
                # THIS key (see _decision_caller).
                _caller = _decision_caller(
                    _policy["model"],
                    _policy,
                    agents=_routing_agents,
                    key_id=key.id,
                    request_id=request_id,
                )
                if _caller is not None:
                    from ..core import decision_routing as _dr

                    async def decide_fn(candidates, _body, score, _c=_caller, _p=_policy, _inc=_incumbent):
                        # Images the decision model may READ. Only offered when
                        # the request carries them; bounded so a 40-image dump
                        # cannot blow the decision latency budget.
                        _imgs = _dr.extract_user_turn(_body)[1][: _dr.MAX_DECISION_IMAGES]
                        # REQUEST-ONLY state (2026-10-30): the per-model facts
                        # (role, live load, cost, modalities) now ride in the
                        # CRITERIA channel, built by _decision_caller from the
                        # same cards and the same live snapshot. Emitting them
                        # here too would put one number in two channels, and the
                        # candidate SET is already the criteria keys.
                        state = _dr.build_state(
                            _body,
                            complexity=score,
                            candidates=candidates,
                            # What this conversation is ALREADY served by, so
                            # the model can weigh the re-prefill a switch costs.
                            current_model=_inc,
                            include_candidates=False,
                        )
                        # The decision is otherwise unobservable after the
                        # fact: nothing logs what the model was shown, so a
                        # wrong pick can only be explained by reproducing the
                        # request by hand. The user's turn is ELIDED: this runs
                        # on every decision (a few per minute per active key)
                        # and the gateway is multi-tenant, so logging it whole
                        # would put user content into INFO logs at volume.
                        try:
                            _shown = json.loads(state)
                            _req = _shown.get("request") or {}
                            for _field in ("user_message", "user_task"):
                                _val = _req.get(_field)
                                if isinstance(_val, str):
                                    _req[_field] = f"{_val[:80]}...({len(_val)} chars)"
                            log.info("decision state: %s", json.dumps(_shown, sort_keys=True))
                        except Exception:
                            log.info("decision state: <%d chars, unparseable>", len(state))
                        result = await _dr.decide(
                            _c,
                            state=state,
                            instructions=_p["prompt"] or _dr.DEFAULT_INSTRUCTIONS,
                            candidates=candidates,
                            min_confidence=_p["min_confidence"],
                            timeout_ms=_p["timeout_ms"],
                            images=_imgs,
                        )
                        return model_routing.DecisionOutcome(
                            ok=result.ok,
                            choice=result.choice,
                            confidence=result.confidence,
                            reason=result.reason,
                            # Carry the full distribution through so the ledger
                            # records how CLOSE the call was, not just the winner
                            # (2026-10-23: needed to tune the confidence floor).
                            probabilities=dict(result.probabilities),
                            # Measured seam latency, for the decision-latency
                            # histogram (the user-facing cost of routing).
                            latency_ms=result.latency_ms or None,
                        )

        # Budget capacity for THIS key, fetched ONCE per routing request
        # (two aggregate queries — NOT one per model) and handed to the
        # availability predicate so a model this key cannot afford right now
        # is never offered to the router. This is what makes {{model_list}}
        # honest: the decision model sees only models with token capacity at
        # this moment. Fail-open by contract — a capacity fault leaves the
        # map empty, which the predicate reads as "not evaluated" = available,
        # so routing degrades to its previous behaviour instead of emptying
        # the ladder (the admission gate still enforces the budget).
        _capacity: dict[str, dict] = {}
        if _routable:
            try:
                from ..core import budgets as _budgets

                # est_tokens (2026-11-03): capacity is asked the SAME
                # question admission will ask — "would a request THIS size be
                # admitted?" — not merely "is any headroom left". A model with
                # 5k tokens left used to read available and get picked for a
                # 100k-token request, which admission then 429'd: a needless
                # routing hop, a 'budget_exceeded' ledger row and a walk the
                # user never asked for.
                _capacity = await _budgets.model_capacity(key.id, sorted(registered_models), est_tokens)
            except Exception as exc:
                log.warning("routing: capacity lookup failed (fail-open): %s", exc)
                _capacity = {}

        # Candidate availability, extended with TOKEN CAPACITY: a model whose
        # context window cannot hold this prompt must not be offered to the
        # router at all (2026-10-29). Same seam, same fail-open posture as the
        # budget predicate it wraps.
        _available = _routing_available(key, _capacity, agents=_agent_map) if _routable else None
        if _available is not None:
            _available = _context_gated(_available, body, agents=_agent_map)
        model, route_ctx = await model_routing.resolve_routed_model(
            key,
            body,
            model,
            # REAL models + GROUPS + AGENTS (2026-11-08): all three are hard
            # routing targets. The union is used ONLY for the named ceiling
            # check inside resolve_routed_model; candidate availability is the
            # group/agent-aware predicate below.
            registered_models | group_names | set(_agent_map),
            key.allowed_models,
            order,
            available=_available,
            decide=decide_fn,
            sticky_floor=_sticky_floor,
            sticky_write=_sticky_write,
            route_default=_rdef,
            routable_models=_routable_models,
            incumbent=_incumbent,
            switch_margin=float(_policy.get("switch_margin") or 0.0) if _policy else 0.0,
            # Per-key agents join the window at the cheap end (undeclared
            # capability must not outrank the operator's ladder).
            extra_candidates=[a.name for a in _routing_agents] or None,
        )
        # ACTIVE mode: the routed model IS the request's effective pinned
        # model — the admission failover below must classify it as pinned.
        # Left alias-classified, a rate/budget 429 would walk the ordering
        # from its top and could promote the request ABOVE the routing
        # ceiling, which routing never does. Shadow/'off' keep the pseudo
        # name (alias semantics = exactly today's behavior).
        if route_ctx is not None and route_ctx.policy == "active" and route_ctx.engaged:
            requested_model = model
    except model_routing.RoutingRefused as refused:
        # Deliberate 4xx: the no-ladder `routing` case, or a routing__X
        # ceiling that is unregistered / outside the key's allowed_models.
        raise fastapi.HTTPException(refused.status, refused.message) from None
    except Exception as exc:
        log.warning("routing: engagement failed (legacy behavior): %s", exc)
        model, route_ctx = requested_model, None

    # Load-aware cascade head (audit feature, v1.10): the sticky/load pick
    # keys on the SAME conversation fingerprint the canonical store uses,
    # computed from the RAW inbound body (pre-reconcile — the first user
    # message is the stable seed either way). thread_key=None (no seed /
    # non-chat) leaves resolve_cascade byte-identical to the legacy walk,
    # as does the feature being off. A fingerprint fault degrades to None:
    # a bad hash must never break admission.
    # NOTE (review nit): computed PRE-guardrail-mask while the canonical
    # STORE keys its thread post-guardrail — masks touch content, not the
    # first user message's presence, so a divergence only re-picks a
    # sticky head (fresh conversation semantics); it can never mis-route
    # a pinned request.
    try:
        _cascade_thread = canonical.thread_key(body.get("messages") or [], key.id)
    except Exception:
        log.warning("cascade: thread fingerprint failed — load-aware head disabled for this request", exc_info=True)
        _cascade_thread = None
    # --- affinity key (session affinity / model groups) --------------------
    # The conversation's pin identity: the client's X-PCAI-Session when it
    # sends one, else the SAME thread fingerprint the canonical store and
    # the load-aware head already use. Deriving it means endpoint and group
    # pins work with NO client change, per conversation. Computed when
    # EITHER feature is on; each pin SCOPE has its own gate at its own seam
    # (ep: only under session_affinity_enabled, grp: only under
    # model_groups_enabled), and None means no pin (no header, no seed).
    #
    # The header is CANONICALISED, never stored raw (audit 2026-11-10 P2-2):
    # this value becomes the thread_key half of a model_stickiness PRIMARY KEY
    # (stickiness.set_endpoint_pin / set_group_pin), so an unbounded
    # client-supplied string mints one row per distinct value up to the server's
    # 1 MB header limit. stickiness.affinity_key applies the documented
    # charset/length bound and hashes; a value outside it is DROPPED (routing
    # state, not policy — the request is still served, it just falls back to the
    # conversation fingerprint, exactly as if no header had been sent).
    from ..core import stickiness as _stick_mod

    affinity_session: str | None = None
    if settings.session_affinity_enabled or settings.model_groups_enabled:
        affinity_session = _stick_mod.affinity_key(request.headers.get("x-pcai-session")) or _cascade_thread
    # --- three-state relay resolution, EARLY (audit L-2): the plain-chat
    # capture below must not run on a RELAYED request — the relay branch owns
    # that turn's store rows (the reconcile save + the loop's own terminal
    # persistence), and the capture was a second load+save against the same
    # (key_id, thread_id) row on the hottest path (the commit that added the
    # relay branch claimed "no capture on relay requests", but the only read
    # of the relay header sat AFTER this scheduling). The verdict uses the
    # SAME inputs the relay pre-flight consumes further down (header three-
    # state + key default + the {type: mcp} body opt-in), so this guard and
    # the resolution that actually routes the turn cannot drift.
    try:
        _probe_mcp_reqs, _probe_fn_tools = _split_request_tools(body.get("tools"))
    except ValueError:
        # the strict parse below (with its 400) still owns validation; the
        # probe only needs the not-a-mcp-tool answer
        _probe_mcp_reqs = []
    header_val = request.headers.get("x-pcai-execute-tools")
    # MIRROR of the pre-flight's FINAL verdict (the block further down): the
    # header's explicit "0" outranks the body opt-in and turns it into a
    # loud 400 (relay_requested stays False there); require_approval:always
    # is the client-execution path, never the relay; the global kill-switch
    # off means the body opt-in is a 400, not a relay. Computing the same
    # value here is what keeps the capture guard from drifting away from the
    # resolution that actually routes the turn.
    relay_requested = _relay_wants(header_val, key.relay_default, settings.tool_relay_enabled)
    _probe_hdr0 = (header_val or "").strip() == "0"
    _probe_approval_always = any(t.get("require_approval") == "always" for t in _probe_mcp_reqs)
    if (
        not relay_requested
        and _probe_mcp_reqs
        and not _probe_approval_always
        and not _probe_hdr0
        and settings.tool_relay_enabled
    ):
        relay_requested = True
    if _probe_approval_always:
        # require_approval:"always" is the CLIENT-execution path — the
        # pre-flight below flips relay_requested to False for it even when
        # the header or the key default armed the relay (xv round 2,
        # finding 10: an approval-always request on a relay-default key is
        # NOT relayed, so its plain-chat capture must run).
        relay_requested = False
    # --- agent profiles (2026-10-29): a MODEL NAME that names an agent ------
    # An agent is advertised in /v1/models as a model entry (pcai.type="agent"),
    # so selecting one is just naming it. Resolution happens HERE — before the
    # cascade — for two reasons:
    #   1. the generic alias path would otherwise treat an unknown name as a
    #      cascade request and serve it with the profile FULL tool surface: a
    #      request that ASKED for a narrow agent would silently WIDEN. The 403
    #      below is what keeps "not an agent" and "an agent you may not use"
    #      distinguishable.
    #   2. an agent may pin a model, which turns the rest of this block into an
    #      ordinary pinned request (budgets, ACL, fallbacks all unchanged).
    # The lookup name is the POST-ROUTING model: in ACTIVE routing the router
    # may have picked a per-key AGENT, and that pick must apply the agent's
    # surface + prompt exactly like a client-named agent (safe: routing happens
    # BEFORE the request is prepared). SKIPPED for registered pools, groups,
    # the reserved aliases, and shadow/off routing (which keeps the pseudo
    # name) — so the dominant `auto` path pays nothing for this feature.
    agent: agents_mod.Agent | None = None
    _agent_lookup = model
    if (
        # A pseudo-model (shadow/off routing keeps the pseudo NAME) is never an
        # agent; checking the POST-routing name keeps the dominant shadow path
        # free of an agent-registry query.
        model_routing.detect_pseudo_model(_agent_lookup) is None
        and _group_by_name(_agent_lookup) is None
        and _agent_lookup not in registered
        and _agent_lookup not in agents_mod.RESERVED_NAMES
    ):
        try:
            _key_agents = _routing_agents or await agents_mod.for_key(key.id)
            agent = agents_mod.pick(_key_agents, _agent_lookup)
            if agent is None:
                # NEAR MISS (case-only): a model id is exact, so this is a
                # typo, not an alias request. Refuse it BY NAME — falling
                # through would serve the profile FULL surface for a caller who
                # asked for a narrow agent, and a silent cascade is
                # indistinguishable from success.
                _near = agents_mod.near_miss(_key_agents, _agent_lookup)
                if _near is not None:
                    raise fastapi.HTTPException(
                        404,
                        detail={
                            "type": "agent_name_case",
                            "did_you_mean": _near.name,
                            "message": (
                                f"{_agent_lookup!r} is not a model id; agents are matched exactly"
                                f" — did you mean {_near.name!r}?"
                            ),
                        },
                    )
        except agents_mod.AgentRegistryUnavailable:
            # FAIL OPEN, LOUDLY (core.agents docstring): an agent only ever
            # HIDES tools the key already holds, so a registry fault degrades to
            # "no narrowing" — the profile surface, which is what any
            # unregistered name gets today — instead of making every alias
            # request depend on this read. A fault is NEVER reported as a
            # permission decision (the 403 below requires a successful read).
            log.warning(
                "agents: registry unavailable — serving %r without agent scoping (profile surface applies)",
                _agent_lookup,
            )
            agent = None
        if agent is not None:
            if not agent.offered:
                raise fastapi.HTTPException(
                    403,
                    detail={
                        "type": "agent_not_offered",
                        "agent": agent.name,
                        "message": (
                            f"agent {agent.name!r} is not offered by this key's profile"
                            " — ask the operator to add it, or name a registered model"
                        ),
                    },
                )
            # '' = cascade: the agent chooses tools + prompt, not the model, so
            # it rides the SAME walk `auto` does (budget headroom, ACL, sticky
            # head, admission failover — nothing special-cased).
            model = agent.model or "auto"

    # --- model group ACL bridge -------------------------------------------
    # A group name classifies as a registered PINNED target (added to the
    # classification set above), and resolve_cascade's ACL test is
    # 'registered and inside allowed_models'. Naming a GROUP grants its
    # members, so when the key's ACL lists a MEMBER rather than the group
    # name, the group name is allowed for THIS resolution — the member-level
    # filter in _group_target then decides which members are reachable. A
    # group never widens the ACL: _eligible_members intersects the same
    # allowed_models (or the group-name grant), never more.
    _resolve_allowed = key.allowed_models
    if (
        model in group_names
        and key.allowed_models
        and model not in key.allowed_models
        and _eligible_members(groups[model], key)
    ):
        _resolve_allowed = [*key.allowed_models, model]
    resolution = await resolve_cascade(
        key.id, model, registered, _resolve_allowed, est_tokens, thread_key=_cascade_thread
    )
    if not resolution.ok:
        detail: dict[str, Any] = {"error": resolution.reason}
        if resolution.attempts is not None:
            detail["cascade_attempts"] = resolution.attempts
        raise fastapi.HTTPException(
            429 if resolution.attempts is not None else 403 if "not allowed" in resolution.reason else 404, detail
        )
    model = resolution.model  # the model that will actually serve
    # Deferred stickiness commit (audit 2026-10-29 P3, XV-3): the router picks
    # BEFORE admission, so the floor is written only now, with the model the
    # gates actually settled on. Fail-open: a sticky fault must never fail a
    # request that admission already approved (the writer is fail-open by
    # contract; this catch is belt-and-braces for the finisher itself).
    if route_ctx is not None and route_ctx.sticky_finish is not None:
        try:
            route_ctx.sticky_finish(model)
        except Exception:
            log.warning("routing: deferred sticky commit failed - floor not persisted", exc_info=True)
    # --- model group -> provisional member (BEFORE the pool/type gate) -----
    # resolve_cascade above resolved the NAME; a group still needs its
    # MEMBER. Resolved here so the type gate, the guardrail lookup, the
    # routing log line and every budget/rate gate below see the model that
    # will actually serve. The grp: pin makes this stable per conversation;
    # with no pin it is a power-of-two pick over the live load snapshot
    # (core.load_snapshot). NO member qualifies -> 503: the group is real
    # but nothing in it can serve right now, which is a capacity answer, not
    # a 404.
    if model in group_names:
        model, group_name = await _group_target(model, key, affinity_session, request_id)
        if group_name is not None and model == group_name:
            raise fastapi.HTTPException(503, f"no member of model group {group_name!r} can serve right now")
    # Per-request routing decision (observability, 2026-10-17): the gateway
    # logged cascade EVENTS only, so "which model actually served this
    # request, and why" was unanswerable in production — a pinned request
    # that ended up on a fallback looked identical in the logs to one that
    # was routed there, and three separate root-cause theories were chased
    # against this codebase in one afternoon. One line per request, at the
    # point the decision is FINAL (post-cascade, pre-pool): it names what
    # arrived, what will serve, and whether the client pinned it.
    # The kind field is the cascade classifier's own verdict (pinned = the
    # client named a registered model and never cascades; alias = resolved
    # through the profile ordering). Deliberately log.info (not debug): it
    # is the audit trail for "why did my request go there", and it carries
    # no prompt content and no bearer key.
    log.info(
        "routing request_id=%s key_prefix=%s requested=%s served=%s kind=%s",
        request_id,
        key.key_prefix,
        requested_model,
        model,
        classify_request(requested_model, registered, key.allowed_models),
    )

    pool = model_router.pool(model)
    if pool is None:
        raise fastapi.HTTPException(404, f"model {model!r} is not registered")
    # Type gate: serves_chat's any() predicate (imported from core.cascade —
    # the SAME rule the cascade walk uses, not duplicated). The old
    # endpoints[0] check 400'd a pool whose FIRST endpoint happened to be
    # non-chat even when another member could serve — a redeploy that
    # re-registers endpoints in a different order produced exactly that.
    if not serves_chat(model):
        raise fastapi.HTTPException(400, f"model {model!r} is a {pool.endpoints[0].model_type} endpoint, not chat")

    # X-PCAI-Metadata: 1 — opt-in body metadata (review §4b). Headers ride
    # ALWAYS; the body block is opt-in and non-streaming-only. (The fallback
    # chain itself is read AFTER admission failover, when the serving model
    # is final — see below.)
    want_metadata = (request.headers.get("x-pcai-metadata") or "").strip() == "1"

    # --- content guardrails (ported from hpe-llm-gateway, reimplemented on
    # our ledger) — BEFORE the gates: a policy block must not consume rate
    # window, budget headroom, or a concurrency slot (none is held yet).
    # LOOKUP failures fail open (a DB hiccup must not take the data plane
    # down — same posture as the webhook engine); blocks/masks apply.
    guardrail_pre: list[Any] = []
    guardrail_post: list[Any] = []
    # Inbound reasoning (2026-10-26, CUT 2). This used to DELETE every
    # reasoning key from the incoming history. That was the answer to the
    # 2026-09-18 "relay storm": a client re-appended each streamed reasoning
    # block once per agent-loop cycle, so one stored event grew to 6,630 copies
    # of a single line (~401 KB) and every subsequent prefill paid for it.
    #
    # Deleting it fixed the symptom by removing the model's own chain of
    # thought: a relayed turn then replayed as "assistant emitted tool calls,
    # no thinking", which teaches the model to skip reasoning. Measured over 140
    # real sessions: 86.1% of relayed tool-call steps carried no reasoning vs
    # 46.1% direct.
    #
    # The BOUND is the real fix, not the deletion. Each replayed block is capped
    # at reasoning_history_max_chars, so growth is linear and small (a 40-message
    # history cannot exceed 40 x cap) instead of unbounded. Verified against the
    # served endpoint: GLM accepts its own reasoning back in every shape,
    # including alongside tool_calls.
    #
    # reasoning_history_enabled=False restores the old strip byte-for-byte, and
    # a cap of 0 also strips — either is a one-knob rollback if a client turns
    # out to re-append without bound.
    _reason_cap = max(0, int(settings.reasoning_history_max_chars))
    _keep_reasoning = bool(settings.reasoning_history_enabled) and _reason_cap > 0
    for _m in body["messages"]:
        if not isinstance(_m, dict):
            continue
        # ALL THREE spellings the relay itself recognises (toolrelay.
        # REASONING_KEYS). "reasoning_text" was missing here (audit ZC-5) —
        # the pi-ai spelling for OpenRouter-style upstreams — so a client
        # replaying reasoning under that key bypassed the cap entirely and
        # the 2026-09-18 amplification hole reopened through it.
        for _rk in toolrelay.REASONING_KEYS:
            _rv = _m.get(_rk)
            if _rv is None:
                continue
            if not _keep_reasoning or not isinstance(_rv, str):
                _m.pop(_rk, None)
                continue
            if len(_rv) > _reason_cap:
                # Keep the TAIL (the most recent reasoning is what the next call
                # needs) and mark the cut so the model knows it is partial.
                _m[_rk] = "…[earlier reasoning trimmed]…" + _rv[-_reason_cap:]

    guard_events: list[dict[str, Any]] = []
    try:
        _all_guards = await guardrails.guardrails_for_model(model)
        guardrail_pre = [g for g in _all_guards if g.mode == "pre_call"]
        guardrail_post = [g for g in _all_guards if g.mode == "post_call"]
    except Exception as exc:
        log.warning("guardrails: lookup failed for %r (fail-open): %s", model, exc)
    if guardrail_pre:
        try:
            body["messages"], _pre_ev = await guardrails.run_pre_call(
                guardrail_pre, body["messages"], model_router.client
            )
            guard_events = [e.as_dict() for e in _pre_ev]
            est_tokens = _estimate_tokens(body)  # masking shrank the text — gates run on facts
        except guardrails.GuardrailBlocked as gb:
            latency = _latency_ms(started)
            await settle(
                key.id,
                model,
                "chat",
                0,
                0,
                "guardrail_blocked",
                latency,
                request_id,
                guardrail_events=[
                    *guard_events,
                    {
                        "guardrail": gb.name,
                        "mode": gb.mode,
                        "engine": getattr(gb, "engine", "unknown"),
                        "action": "blocked",
                        "detail": gb.detail,
                    },
                ],
            )
            _log_request(key.key_prefix, model, None, "guardrail_blocked", latency, request_id)
            # SOFT block (guardrail config carries a non-empty `reply_text`):
            # the refusal stays a ledgered guardrail_blocked row — the audit
            # story is byte-identical to the hard path — but the CLIENT sees a
            # normal assistant turn instead of a 400. An agent loop that dies
            # on INVALID_REQUEST (DSH kills the whole thread) keeps running;
            # X-PCAI-Guardrail-Blocked is the machine signal to purge the
            # offending turn client-side before the next request (the gateway
            # is stateless and cannot do it: the text rides back in messages).
            reply_text = getattr(gb, "reply_text", "")
            if isinstance(reply_text, str) and reply_text.strip():
                reply_headers = {"X-Request-ID": request_id, "X-PCAI-Guardrail-Blocked": gb.name}
                if bool(body.get("stream")):
                    # _sse_masked_final already emits the exact shape a
                    # single-shot assistant answer needs: one content delta +
                    # finish_reason stop + [DONE].
                    return StreamingResponse(
                        iter([_sse_masked_final(model, request_id, reply_text)]),
                        media_type="text/event-stream",
                        headers=reply_headers,
                    )
                return JSONResponse(_guardrail_reply_json(model, request_id, reply_text), headers=reply_headers)
            raise fastapi.HTTPException(400, f"blocked by guardrail '{gb.name}': {gb.detail}") from None

    # --- MCP request-body opt-in (OpenAI-idiomatic; ported from
    # hpe-llm-gateway) — tools:[{"type":"mcp","server"?:…,
    # "require_approval"?:…}]. "never" (default) = execute in-cluster via
    # the relay; "always" = hand the tool calls back to the client: the
    # granted catalog is exposed as plain OpenAI function tools and the
    # model's tool_calls are forwarded untouched (client executes them
    # directly against /mcp or its own path, human-approval flows work).
    try:
        mcp_reqs, fn_tools = _split_request_tools(body.get("tools"))
    except ValueError as exc:
        raise fastapi.HTTPException(400, str(exc)) from None
    requested_servers = {t["server"] for t in mcp_reqs if t["server"]}
    approval_always = any(t["require_approval"] == "always" for t in mcp_reqs)
    if mcp_reqs:
        # mcp entries must never reach an upstream (unknown tool type)
        if fn_tools:
            body["tools"] = fn_tools
        else:
            body.pop("tools", None)

    # --- agent scope vs an EXPLICIT server request (both tool paths) --------
    # The client asked for specific servers AND named an agent. If the agent
    # excludes one of them the request is self-contradictory: serving it either
    # way lies to someone (the model gets a surface it was told it had, or the
    # client gets a surface it asked not to have). FAIL LOUD, here, so the relay
    # path and the approval-passthrough path cannot drift apart — the passthrough
    # used to narrow silently and could ship an EMPTY tools array upstream
    # (cross-validation 2026-10-29).
    if agent is not None and requested_servers:
        excluded = sorted(set(requested_servers) - set(agent.servers))
        if excluded:
            raise fastapi.HTTPException(
                400,
                f"agent {agent.name!r} does not include server(s) {excluded} (agent servers: {list(agent.servers)})",
            )

    # --- relay pre-flight BEFORE the gates (slot-leak fix, security audit):
    # the "no MCP grants" 400 is deterministic from the key alone, so it runs
    # before _run_gates — the old order raised it AFTER the concurrency
    # increment with nothing to release, permanently leaking the slot (a
    # grantless proxy key + max_concurrent=N was 429-bricked after N such
    # requests, until pod restart).
    # Three-state relay resolution: the per-request header always wins —
    # '1' forces the relay on (even for default-off keys), '0' opts out
    # (even for default-on keys) — and an ABSENT header falls back to the
    # key's relay_default (console: Keys ▸ edit). This lets off-cluster
    # consumers (sproket) relay without any client config while local
    # harnesses keep their own tools, per key, flipped live in the console.
    # The request-body opt-in participates the same way: {type: mcp} with
    # require_approval 'never' engages the relay; an explicit header '0'
    # still wins (with a loud 400 rather than silently ignoring the
    # execution request).
    # relay_requested was resolved EARLY above (the L-2 capture guard shares
    # the exact verdict); header_val rides along for the opt-out 400s below.
    # Per-request pcai-event override (RFC-PER-KEY-STREAM-EVENTS): '1' forces
    # telemetry events on, '0' strips them (strict clients), absent = the
    # key's stream_pcai_events default. Resolved ONCE here and passed into
    # relay_stream below.
    stream_events = _stream_events_wants(request.headers.get("x-pcai-stream-events"), key.stream_pcai_events)
    if mcp_reqs and not approval_always and not relay_requested:
        if (header_val or "").strip() == "0":
            raise fastapi.HTTPException(
                400,
                "tools include {type: mcp} execution requests but X-PCAI-Execute-Tools: 0 disabled the relay;"
                " drop the header, or use require_approval: 'always' for client-side execution",
            )
        if not settings.tool_relay_enabled:
            raise fastapi.HTTPException(
                400,
                "tool relay is disabled on this gateway (PCAI_LLM_TOOL_RELAY_ENABLED=false);"
                " use require_approval: 'always' for client-side execution",
            )
        relay_requested = True
    relay_grants: set[tuple[str, str]] = set()
    if relay_requested or approval_always:
        from ..mcpf.federation import load_denies, load_grants

        relay_grants = await load_grants(key.id)
        # Deny rows subtract EVERYWHERE (audit D-F1): under
        # tool_relay_grant_mode="scope" (the LEGACY opt-in — the default is
        # "explicit", config.tool_relay_grant_mode) the policy-admitted catalog
        # is re-added below AFTER this filter, so denies must be removed from
        # the FINAL set — subtracting them from load_grants alone would not
        # survive the scope merge. load_denies FAILS CLOSED since audit
        # 2026-11-10 P2-11: a DB fault serves the key's last known snapshot, and
        # with no snapshot it propagates so the turn is refused rather than
        # silently un-denied.
        denies = await load_denies(key.id)
        relay_grants = relay_grants - denies
        scope_granted = settings.tool_relay_grant_mode == "scope" and "mcp" in key.scopes
        if not relay_grants and not scope_granted:
            raise fastapi.HTTPException(400, "tool execution requested but this key has no MCP grants")
    if approval_always:
        # Client-side execution: expose the granted catalog as OpenAI
        # function tools; the model's tool_calls stream back untouched and
        # the client decides what to execute. No relay, no server-side
        # execution, normal gates/settle on the model call itself.
        catalog, catalog_grants = await _relay_catalog(key, relay_grants, requested_servers)
        if not catalog:
            raise fastapi.HTTPException(400, "approval passthrough requested but no MCP tools are granted to this key")
        # An agent narrows the CLIENT-execution surface too: a model handed an
        # agent must never see a tool the agent excludes, whichever side runs it.
        catalog = agents_mod.narrow_catalog(catalog, agent.servers if agent else None, agent.tools if agent else None)
        # Tool-definition pinning rides THIS offer too (2026-11-10,
        # cross-validation P1): the passthrough hands the model the same
        # definitions the relay would, so a rug-pulled description used to
        # reach its context with no pin observation at all. block mode drops
        # a changed (or vanished) tool from this surface; warn logs it.
        offered_grants = agents_mod.narrow_grants(
            catalog_grants, agent.servers if agent else None, agent.tools if agent else None
        )
        catalog = toolrelay.pin_catalog_with_verdicts(catalog, offered_grants)[0]
        fn_defs = [
            {
                "type": "function",
                "function": {
                    "name": f"{t['server']}{toolrelay.TOOLSEP}{t['tool']}",
                    "description": t.get("description") or f"{t['server']}/{t['tool']}",
                    "parameters": t.get("inputSchema") or {"type": "object", "properties": {}},
                },
            }
            for t in catalog
        ]
        body["tools"] = [*fn_tools, *fn_defs]
        # An agent can narrow the surface to NOTHING (a chat-only agent) and the
        # emptiness guard above ran BEFORE that narrowing. `tools: []` is not the
        # same as having no tools key: strict upstreams (SGLang) 400 an empty
        # array, and the model would see a tool channel with nothing in it
        # (cross-validation 2026-10-29).
        if not body["tools"]:
            body.pop("tools", None)
        relay_requested = False

    # --- admission failover (rate/budget 429 -> next cascade candidate) ---
    # The cascade already failovers BUDGET headroom; this walk adds the RATE
    # axis. Alias/auto requests only — pinned requests never cascade (the
    # client asked for THAT model; documented cascade semantics).
    #
    # MODEL GROUP (F3): a group admits its MEMBERS. The provisional pick
    # leads and the rest of the group follows in load order, so a
    # rate/budget 429 on one member fails over INSIDE the group instead of
    # ending the turn. resolution.gate is the GROUP NAME's gate (key-level
    # only), so it is NOT handed to the walk as a precomputed member gate —
    # each member gets its own budget check (a group must never bypass a
    # member's budget).
    if group_name is not None:
        candidates = _group_candidates(group_name, model, key)
    else:
        candidates = [resolution.model]
    is_alias = classify_request(requested_model, registered, key.allowed_models) == "alias"
    if is_alias:
        # P1 (perf round 2026-10-16): resolution.order already carries the
        # key's profile ordering — resolve_cascade read it (cascade.py
        # profile_order_for_key) moments ago in THIS request. Re-reading it
        # here duplicated that serialized round-trip on every alias request;
        # the ordering is an admin-set preference, so within-request
        # staleness is not a concern (the failover walk is a preference
        # list, not a policy boundary — the ACL above already filtered it).
        order = resolution.order
        for cand in order:
            if cand in candidates or cand not in registered:
                continue
            if key.allowed_models and cand not in key.allowed_models:
                continue
            # CHAT-ONLY (audit w6, P1): resolution.order is the profile ACL as
            # well as the ladder, so it legitimately carries embedding /
            # reranker pools (the seeder ships them). This walk has no type
            # check of its own -- _next_fallback_candidate does -- so an
            # unfiltered candidate here promotes a CHAT request onto an
            # embedding pool, which every upstream 400s.
            if not serves_chat(cand):
                continue
            candidates.append(cand)

    # --- exact-match response cache (off | shadow | active) ----------------
    # Placed BEFORE admission so a hit costs no budget and no rate-window
    # token: the whole point is that a repeated deterministic request skips
    # the model. Eligibility is deliberately narrow (see core.response_cache):
    # temperature == 0, non-streaming, non-relay, and no routing/cascade
    # rewrite (model must still be the name that arrived). Shadow mode only
    # RECORDS would-be hits; it still stores, because a would-be hit needs an
    # entry to be measured against.
    _cache_hash: str | None = None
    _cache_mode = response_cache.mode()
    if (
        _cache_mode != "off"
        and not relay_requested
        and model == requested_model
        and model not in ("auto",)
        and model_routing.detect_pseudo_model(model) is None
        and response_cache.eligible(body)
        and model_router.pool(model) is not None
    ):
        # The METADATA variant is part of the key (audit 2026-11-10 P2-1): the
        # stored bytes are the FINAL response body, so one key for both variants
        # served a caller who never opted in a "pcai" block naming the ORIGINAL
        # request's endpoint and cost. Two variants, two keys.
        _cache_hash = response_cache.request_hash(body, want_metadata=want_metadata)
        _cached = await response_cache.lookup(key.id, model, _cache_hash)
        _metrics.RESPONSE_CACHE.labels(mode=_cache_mode, result="hit" if _cached is not None else "miss").inc()
        if _cache_mode == "active" and _cached is not None:
            _cache_latency = _latency_ms(started)
            _hit_body: bytes = _cached
            _hit_headers: dict[str, str] = {
                "X-Request-ID": request_id,
                "X-Cache": "HIT",
                "X-PCAI-Served-Model": model,
            }
            _hit_blocked = ""
            # POST-CALL GUARDRAILS ON A HIT (audit 2026-11-10 P2-1). The cached
            # bytes were judged by whatever post_call set was attached when they
            # were STORED, and a hit used to replay them verbatim: a masked
            # answer came back UNMASKED (and X-PCAI-Guardrail-Modified was
            # dropped), and a guardrail added since the store never saw the
            # answer at all. Re-running the current set costs one policy
            # evaluation and makes a hit behave exactly like a fresh serve on
            # the policy axis — no cache invalidation to get wrong.
            if guardrail_post:
                _hit_json: Any = None
                try:
                    _hit_json = json.loads(_cached)
                except (TypeError, ValueError):
                    _hit_json = None
                if isinstance(_hit_json, dict):
                    _hit_text = _assistant_message_text(_hit_json)
                    try:
                        _gtext, _pev, _blocked = await guardrails.run_post_call(
                            guardrail_post, _hit_text, model_router.client
                        )
                    except Exception as exc:
                        # fail-open with visibility (the forward path's posture)
                        log.warning("guardrails: post_call evaluation on cache hit failed: %s", exc)
                        _gtext, _pev, _blocked = _hit_text, [], False
                    if _blocked:
                        _first = _pev[0] if _pev else None
                        _hit_blocked = f"blocked by guardrail '{_first.guardrail if _first else 'post_call'}'"
                        if _first is not None and _first.detail:
                            _hit_blocked += f": {_first.detail}"
                    elif _gtext != _hit_text and _mask_chat_body_choices(_hit_json, _gtext):
                        _masked_by = sorted(
                            {d["guardrail"] for d in (e.as_dict() for e in _pev) if d.get("action") == "masked"}
                        )
                        if _masked_by:
                            _hit_headers["X-PCAI-Guardrail-Modified"] = ",".join(_masked_by)
                        _hit_body = json.dumps(_hit_json).encode()
            if _hit_blocked:
                await settle(
                    key.id,
                    model,
                    "chat",
                    est_tokens,
                    0,
                    "guardrail_blocked",
                    _cache_latency,
                    request_id,
                    cached_tokens=est_tokens,
                )
                _log_request(key.key_prefix, model, None, "guardrail_blocked", _cache_latency, request_id)
                return JSONResponse(
                    status_code=400,
                    content={
                        "error": {
                            "message": _hit_blocked,
                            "type": "guardrail_blocked",
                            "param": "",
                            "code": "guardrail_blocked",
                        }
                    },
                    headers={"X-Request-ID": request_id},
                )
            await settle(
                key.id,
                model,
                "chat",
                est_tokens,
                0,
                "cache_hit",
                _cache_latency,
                request_id,
                cached_tokens=est_tokens,
            )
            _log_request(key.key_prefix, model, None, "cache_hit", _cache_latency, request_id)
            return fastapi.responses.Response(
                content=_hit_body,
                media_type="application/json",
                headers=_hit_headers,
            )

    model, gate, admission_fallback_from = await _admit_with_failover(
        key,
        candidates,
        est_tokens,
        request_id,
        started,
        # A group's gate belongs to the GROUP NAME, not to the member about
        # to be admitted: re-check each member instead (no budget bypass).
        None if group_name is not None else resolution.gate,
    )
    # ONE release path per request (audit 2026-11-10 P1-2). _run_gates took the
    # concurrency slot above, and from here on SEVERAL exits may give it back:
    # the relay generator's finally, _stream_chat's own 502 path, the plain
    # streaming guard below, the non-streaming finally. Every one of them calls
    # this once-wrapper, so "add a release wherever it can leak" can never
    # decrement a CONCURRENT request's slot — the per-key counter's only
    # invariant is that ONE admitted request releases exactly once.
    release_slot = _once(lambda: _release_in_flight(key.id))
    warn_level = gate.warn_level
    # --- canonical capture for the PLAIN chat path (fire-and-forget; the relay
    # path captures its own): a NON-relayed agent using this key converges into
    # the canonical store and gets automatic memories. A relayed request is
    # skipped — relay_requested is the guard (resolved early, above).
    #
    # SCHEDULED HERE, AFTER ADMISSION (audit 2026-11-10 P2-30). Pre-fix this ran
    # BEFORE resolve_cascade/_admit_with_failover, so every rejected request —
    # 403 ACL, 404 unregistered, 429 rate/budget, the 503 no-healthy-upstream —
    # still paid a full SELECT plus an UPSERT that re-serialised the WHOLE
    # conversation JSONB, and the cache-hit path (which returns above) paid it
    # twice. Nothing about a rejected or cached turn belongs in the store.
    # It also removes a race: the task closes over the LIVE body dict, so
    # scheduling it before the guardrails meant the pre-call mask/reasoning
    # strip could land underneath the capture mid-write.
    try:
        if not relay_requested and len(_capture_tasks) < _CAPTURE_TASKS_MAX:
            # strong ref via the module set (a bare create_task can be GC'd
            # mid-flight - the classic fire-and-forget footgun); the done
            # callback discards AND logs (never a silent swallow)
            _hdr = request.headers.get("x-pcai-conversation") or ""
            # headers ride along so the capture can recognize an auxiliary
            # session-title call (X-PCAI-Purpose) without re-deriving it
            _t = asyncio.create_task(_capture_chat_canonical(key, body, _hdr, request.headers))
            _capture_tasks.add(_t)
            _t.add_done_callback(_capture_tasks.discard)
    except Exception:
        log.warning("canonical: plain-chat capture could not be scheduled", exc_info=True)
    # The admission walk may have PROMOTED the request to a different model
    # (rate/budget 429 down the profile ordering): the per-model chain must
    # be the CHAIN OF THE MODEL THAT WILL ACTUALLY SERVE, not the name that
    # arrived. A fresh read; a chain read failure is fail-safe (empty —
    # the fallback feature never breaks a request).
    #
    # The whole block sits in the release guard (audit 2026-11-10 P1-2): this
    # is the ONE await between the gates taking the slot and the big post-gate
    # try below, and its own fail-safe catches Exception only — a client
    # disconnect here is a BaseException and used to leak the slot.
    try:
        fb_chain = await model_fallbacks.chain_for(model)
        # Runtime cascade continuation (ALIAS requests only). The profile
        # ordering AFTER the serving model is appended as the FINAL segment of
        # the walk chain: an `auto` request whose admitted model then cannot
        # serve (pool.pick()==None, transport error, upstream 5xx) fails over
        # DOWN THE ADMIN'S OWN ORDERING instead of ending answerless when no
        # per-model chain was curated. The per-model chain stays FIRST — an
        # explicit per-model sign-off outranks the ordering — and the combined
        # walk is still sliced to settings.model_fallback_max, so the
        # documented bound (and its 0 kill switch) is unchanged. Every appended
        # candidate is re-gated at walk time exactly like a chain entry
        # (registered + key-allowed + healthy chat pool + fresh
        # check_budgets): the ordering tail is a preference list, never a
        # budget or ACL bypass.
        #
        # NOT for pinned requests: a pin never cascades (the client asked for
        # THAT model — documented cascade semantics), so its runtime net stays
        # the per-model chain. For an alias the ordering IS the routing policy,
        # so it keeps applying after admission.
        if is_alias and settings.model_fallback_max > 0:
            seen = {model, *fb_chain}
            tail = _relay_fallback_tail(model, resolution.order or [], registered, key.allowed_models)
            fb_chain = [*fb_chain, *[c for c in tail if c not in seen]][: settings.model_fallback_max]
    except BaseException:
        release_slot()
        raise
    fallback_from = admission_fallback_from

    try:
        # P1 (audit w6): the admission walk above may have PROMOTED the request
        # to a DIFFERENT model (a rate/budget 429 walks the ordering by
        # design), while `pool` was resolved for the model that ARRIVED.
        # Serving the promoted model through the old pool sends the request to
        # the endpoint that just refused it -- a strict upstream answers 400/404
        # (so the failover turns a 429 into a hard failure) and a permissive one
        # serves the budget-refused model while the ledger books another (a
        # per-model budget bypass). Re-resolve for the model that will serve.
        # Inside the try: the gates have already taken the in-flight slot, so
        # every raise here must go through the release path below.
        #
        # MODEL GROUP (F3, defensive): every path above resolves a group to a
        # MEMBER before admission, so this should be a no-op. Kept because a
        # group name reaching the pool lookup would 404 a servable request —
        # resolve it (and 503 when nothing qualifies) instead of serving a
        # name that has no pool.
        if model in group_names:
            model, group_name = await _group_target(model, key, affinity_session, request_id, reason="failover")
            if group_name is not None and model == group_name:
                raise fastapi.HTTPException(503, f"no member of model group {group_name!r} can serve right now")
        pool = model_router.pool(model)
        if pool is None:
            raise fastapi.HTTPException(404, f"model {model!r} is not registered")
        # Same any()-based type gate as the first admission (serves_chat —
        # see its docstring for the endpoints[0] order-dependence defect).
        if not serves_chat(model):
            raise fastapi.HTTPException(400, f"model {model!r} is a {pool.endpoints[0].model_type} endpoint, not chat")
        # --- session affinity (X-PCAI-Session) -------------------------------
        # Pin the conversation to an ENDPOINT so its prefix keeps hitting the
        # same server's KV cache. Three gates before a pin is even read:
        # the fleet flag, a non-relay request (the relay re-picks per
        # iteration and would fight the pin), and an affinity key (the
        # client's session header, else the conversation fingerprint computed
        # above). Routing STATE, never policy: the pin is validated against
        # the LIVE pool by pick() itself, and the store is refreshed on a
        # successful serve and dropped the moment the endpoint proves it
        # cannot serve (breaker trip / transport error / upstream error).
        # ENDPOINT affinity only: the grp: pin has its OWN gate (model
        # groups are independent of endpoint affinity), so the affinity key
        # may exist for a group request while ep pins stay off.
        ep_session: str | None = affinity_session if settings.session_affinity_enabled and not relay_requested else None
        preferred_ep: str | None = None
        if ep_session:
            from ..core import stickiness as _stick

            preferred_ep = await _stick.get_endpoint_pin(key.id, ep_session, settings.session_affinity_ttl_s)
        ep = pool.pick(preferred_ep)
        if ep is None:
            # No healthy endpoint at all: the pinned one (if any) is dead
            # with the rest — drop the pin so the next turn re-picks fresh.
            if ep_session:
                from ..core import stickiness as _stick

                await _stick.drop_endpoint_pin(key.id, ep_session)
            # The group pin goes too: the member's WHOLE pool is unhealthy —
            # the one failure a pin cannot recover from. A single transport
            # error or upstream 5xx is NOT enough (see _group_pin_drop_if_dead).
            await _group_pin_drop_if_dead(key, affinity_session, model)
            # Ledger-worthy error outcome: the request was admitted but served
            # by nobody, so budgets should still see that it happened.
            await settle(
                key.id,
                model,
                "chat",
                0,
                0,
                "error",
                _latency_ms(started),
                request_id,
            )
            _log_request(key.key_prefix, model, None, "error", _latency_ms(started), request_id)
            # Per-model fallback chain (review §4a): a model whose pool cannot
            # pick (all endpoints cooling/disabled) walks its chain instead of
            # ending the pinned request answerless. Candidates are re-gated in
            # _next_fallback_candidate (registered + allowed + healthy chat +
            # fresh budget headroom — the fallback is never a budget bypass).
            # Release still happens in the except below (one release path for
            # every post-gate outcome).
            nxt, fb_rest = await _next_fallback_candidate(
                fb_chain, "", key, "chat", est_tokens, request_id, started, affinity_session
            )
            if nxt is None:
                raise fastapi.HTTPException(503, "no healthy upstream")
            fallback_from = model  # the chain hop answers; the primary failed
            pool, ep = nxt[0], nxt[1]
            # The ACTUAL serving model is the fallback from here on: every
            # settle, header, and relay behavior below names what really
            # served (ledger authoritative — settle under the actual model).
            model = ep.model_name
            fb_chain = [c for c in fb_rest if c != model]
            _log_request(
                key.key_prefix,
                fallback_from,
                None,
                "fallback",
                _latency_ms(started),
                request_id,
            )
        url = f"{ep.base_url}/chat/completions"
        headers = dict(_upstream_headers(ep, request_id))
        headers.update(gate.rate_headers)
        if warn_level:
            headers["X-Budget-Warning"] = f"{warn_level}% of a period budget consumed"

        # --- server-side tool-use relay (X-PCAI-Execute-Tools) ----------------
        # The loop runs INSIDE the SSE response: intermediate thinking streams
        # live, tool executions emit events, the final answer streams after the
        # last model call. Guards + ledger settle every hop (see core.toolrelay).
        # Thinking-stream opt-out for harness clients whose agent loop
        # re-appends whole reasoning blocks per cycle (the 2026-09-18 DSH
        # amplification): X-PCAI-Stream-Thinking: 0 suppresses reasoning
        # deltas entirely — steps stay traced, chips stay rendered.
        stream_thinking = request.headers.get("x-pcai-stream-thinking", "1") != "0"
        # Stamped inert render deltas (Wave R1): TWO gates — the deployment
        # flag AND the client's explicit opt-in header. A client that does
        # not understand `pcai.relay: true` would execute the stamped call
        # a second time (the 2026-09-21 double-execution lesson), so an
        # absent header never receives the deltas, flag on or off.
        render_deltas = settings.tool_relay_render_deltas and request.headers.get("x-pcai-relay-deltas", "0") == "1"

        if relay_requested:
            catalog, relay_grants = await _relay_catalog(key, relay_grants, requested_servers)
            # --- AGENT SCOPE (three-set rule, core.agents) ------------------
            # The OFFERED surface and the DISPATCH gate narrow to the agent's
            # servers; the EXECUTOR keeps the key's FULL set, so the read+egress
            # gate still sees every tool this key could ever call (a per-request
            # switchable selector must never become a way to split read and
            # egress across two agents).
            offered_grants = agents_mod.narrow_grants(
                relay_grants, agent.servers if agent else None, agent.tools if agent else None
            )
            catalog = agents_mod.narrow_catalog(
                catalog, agent.servers if agent else None, agent.tools if agent else None
            )
            # relay_grants rides along for the argument stage's read+egress
            # separation — already computed here, so no extra DB round-trip
            # on the hot path.
            execute = await _relay_executor(key, request_id, grants=relay_grants)

            def client_loop_guard(name: str, args: dict) -> str | None:
                """Cross-request loop guard for CLIENT calls — same counter
                namespaced under server="client" so client and in-cluster
                executions never share a fingerprint. Shared by BOTH client
                surfaces: the dispatch executor below (wave B2) and the
                handback path (relay_stream calls it for client_calls)."""
                return loop_guard_check(key.id, "client", name, args)

            # --- client tool-server (ROADMAP §7 phase 2 wave B2, flag-gated):
            # merge the key's live client offer into the offered catalog and
            # arm the dispatch executor, so a model call naming
            # `client__<tool>` DISPATCHES down the long-poll channel and the
            # result returns INTO the loop (no handback). FAIL-SAFE
            # (non-negotiable): any fault here degrades to today's handback
            # behavior (client_tool_execute=None) — an offer/catalog/executor
            # bug must never break the relay turn, it only loses the feature.
            client_tool_execute = None
            relay_deadline: float | None = None
            if settings.client_tool_dispatch:
                try:
                    offer = clientside.hub.offer_lookup(key.id)
                    if offer is not None:
                        # Catalog extension: the model is OFFERED
                        # `client__<tool>` with the client's real
                        # inputSchema — same shape an MCP tool rides under,
                        # pseudo-server namespace keeps it un-shadowable and
                        # unambiguous against granted MCP tools.
                        catalog = [*catalog, *clientside.catalog_entry(offer)]
                        # ONE wall-clock deadline for the guard AND the
                        # dispatch executor (toolrelay.wall_clock_deadline is
                        # the SAME formula RelayGuard applies — extracted so
                        # the two sides cannot drift). relay_stream below
                        # adopts it as its own guard deadline.
                        relay_deadline = toolrelay.wall_clock_deadline(started)
                        client_tool_execute = clientside.build_client_tool_executor(
                            key.id,
                            request_id,
                            # the SAME gates _relay_executor chains: the key's
                            # RPM window (0 tokens — RPM axis only) and the
                            # cross-request loop guard; settle lands under
                            # `local:<tool>` (never billed as an MCP call).
                            rate_gate=check_rate_limit,
                            settle_fn=settle,
                            loop_guard_fn=client_loop_guard,
                            # trace: None here DELIBERATELY — relay_stream's
                            # dispatch branch already writes the per-call
                            # phase='tool' server='client' trace row through
                            # its own _trace_step (it owns iteration/
                            # thread_id/model context this route cannot
                            # supply); a second recorder would double-count
                            # the same execution on the trace card.
                            trace_fn=None,
                            wall_clock_deadline=relay_deadline,
                            # the key's own limits — the adapter cannot read
                            # the key record, so the pair rides here (see the
                            # B2 ADAPTER NOTE in build_client_tool_executor)
                            rate_limits=(key.rate_limit_rpm, key.rate_limit_tpm),
                        )
                except Exception:
                    log.exception(
                        "relay: client-tool offer/executor wiring failed — proceeding with handback behavior (request_id=%s)",
                        request_id,
                    )
                    client_tool_execute = None
                    relay_deadline = None

            # --- canonical conversation reconciliation (ROADMAP §7 phase 1):
            # the gateway owns the conversation record. Handback continuations
            # still arrive as full-history re-sends (no client change yet);
            # they are reconciled against the stored canonical list here and
            # the RECONCILED list — never the raw body — drives the relay
            # loop below. Fail-safe: any fault degrades to the raw incoming
            # messages inside the helper. Phase 2A: when the request is the
            # client answering a handback (extend + tool-result-only tail),
            # the relay loop below is told so it can label the turn as a
            # continuation instead of a fresh conversation.
            # The agent prompt rides every model call but is NOT in the client's
            # message list, so the context budget must reserve its size (one
            # rough token per 4 chars, the repo's estimator convention) or the
            # trim would let the call overflow by exactly that much.
            _agent_reserve = len(agent.system_prompt) // 4 if agent is not None and agent.system_prompt else 0
            (
                relay_messages,
                _recon_mode,
                continuation,
                iterations_prior,
                canonical_input,
            ) = await _reconcile_relay_messages(request, key, body, reserve_tokens=_agent_reserve)

            # Synthetic tool-call id counter for the WHOLE relay turn (audit
            # 2026-10-26, F-4). stream_model_call mints an id when an upstream
            # omits one; the counter must not restart per model call (one call
            # per iteration) or two iterations mint the same id and the client
            # joins both onto one card. relay_stream owns the turn but the
            # counter has to reach stream_model_call, which is built here — so
            # it lives at this scope and every call closes over it.
            relay_id_counter: list[int] = [0]
            # Model-call counter for THIS relay turn (audit ZC-7): stream_once
            # downgrades the client's pinned tool_choice from the second call
            # on — see the comment there.
            call_index: list[int] = [0]

            def stream_once(msgs: list[dict], tools: list[dict] | None, pool_for: Any = None):
                """One model call as an async GENERATOR of typed events.
                `pool_for` overrides the request's pool — the relay's mid-loop
                model failover re-points iterations at the fallback pool."""
                p = pool_for or pool
                # NO `or p.endpoints[0]` FALLBACK (audit 2026-11-10 P1-3):
                # Pool.pick() returns None PRECISELY when every endpoint is
                # breaker-cooling or probe-failed (routing/router.py:147-149), so
                # the old fallback served a COOLING endpoint anyway — ignoring
                # the breaker, the priority tiers and weight==0 standby. That is
                # the exact construct _decision_caller documents as the bug it
                # removed, and the relay is the dominant agent path (one
                # stream_once per iteration), so a tripped breaker bought
                # nothing.
                #
                # RAISE, do not synthesise events (cross-validation wave 1). The
                # first fix yielded ("error", ...) + ("content", ...), but
                # relay_stream's failover walk fires ONLY on a transport
                # exception (call_failed) or budget exhaustion — an ("error", …)
                # event merely stamps the settle status. So a breaker-cooled
                # primary with healthy fallbacks configured streamed
                # "(model error: no healthy endpoint)" as the ASSISTANT ANSWER and
                # ended the turn, leaving every fallback unused. ConnectError is a
                # TransportError (hence an httpx2.HTTPError), which is exactly the
                # class _bounded_call's caller maps to call_failed — so the walk
                # now takes the hop, and with no fallback configured the relay
                # still closes cleanly with its own visible error chunk.
                ep2 = p.pick()
                if ep2 is None:
                    raise httpx2.ConnectError(
                        "no healthy endpoint in the pool (every endpoint is breaker-cooling or probe-failed)"
                    )
                url2 = f"{ep2.base_url}/chat/completions"
                h2 = _upstream_headers(ep2, request_id)
                b2 = {
                    **body,
                    "messages": msgs,
                    "stream": True,
                    # ask the upstream for REAL usage so the ledger settles
                    # facts, not estimates (the relay budget-bypass fix)
                    "stream_options": {"include_usage": True},
                }
                # Per-model max_tokens ceiling — THE relay-path clamp: this is
                # the body that actually goes upstream, and `ep2` is the
                # endpoint (hence model) serving THIS call. The relay's
                # mid-loop failover re-points iterations at a fallback pool
                # via `pool_for`, so the clamp is re-evaluated per call with
                # that call's model instead of the request's requested one.
                b2 = clamp_max_tokens(
                    wire_compat.normalize_system_messages(b2),
                    ep2.model_name,
                    settings.max_tokens_ceiling,
                    context_limit=_context_limit_for(ep2),
                )
                # C-6 (audit 2026-10-29): the wire body names the model
                # serving THIS call. b2 still carries the CLIENT's name (a
                # pin, or an alias like auto/routing); the relay's per-hop
                # ledger rows, X-PCAI-Served-Model and the mid-loop failover
                # all speak in serving-model names, and a strict upstream
                # 400s a body naming a model it does not serve — the failover
                # would lose the hop it just gained.
                b2["model"] = ep2.model_name
                call_index[0] += 1
                if tools:
                    b2["tools"] = tools
                    # ZC-7 (audit 2026-10-29): the client's tool_choice used
                    # to ride into EVERY loop call verbatim — "required" (or
                    # a pinned function) forces each upstream call to return
                    # tool calls, so the loop could only end at the guards:
                    # the caller waited out the whole iteration/wall-clock
                    # budget and got the max-iterations wrap-up instead of an
                    # answer. The client's choice governs the FIRST call;
                    # from the second on (tool results are in msgs) the model
                    # must be free to answer in text.
                    if call_index[0] > 1:
                        b2["tool_choice"] = "auto"
                else:
                    # tool_choice without tools is an unconditional 400 on
                    # strict upstreams: a body stripped of its mcp entries
                    # (or a catalog-less turn) must not carry the client's
                    # choice either.
                    b2.pop("tool_choice", None)

                async def gen():
                    try:
                        resp = await model_router.client.send(
                            model_router.client.build_request("POST", url2, json=b2, headers=h2), stream=True
                        )
                    except httpx2.HTTPError:
                        # Pre-first-byte upstream failure on the RELAY path. This
                        # used to escape unhandled and KILL the client's SSE
                        # stream (the 2026-10-06 GLM outage: a raw ConnectError
                        # traceback in the logs, no breaker report, no settle, no
                        # _log_request — and a harness client that burned all 5
                        # retries on the same dead model). Report the failure so
                        # the endpoint COOLS (which is what lets the pick-None
                        # admission path walk the chain on later requests), log
                        # it, and re-raise for the relay's own failover.
                        await p.report(ep2, False, error="HTTPError")
                        _log_request(key.key_prefix, ep2.model_name, ep2, "error", _latency_ms(started), request_id)
                        raise
                    try:
                        if resp.status_code >= 400 and "stream_options" in b2:
                            # some upstreams reject stream_options outright —
                            # retry the identical call without it before giving
                            # up (the settle then falls back to the estimate)
                            await resp.aread()
                            await resp.aclose()
                            retry = {k: v for k, v in b2.items() if k != "stream_options"}
                            resp = await model_router.client.send(
                                model_router.client.build_request("POST", url2, json=retry, headers=h2), stream=True
                            )
                        if resp.status_code >= 400:
                            await resp.aread()
                            # DIAGNOSTIC ONLY (2026-10-29). The relay renders an
                            # upstream 4xx as assistant text "(model error 400)"
                            # and used to DISCARD the body, which made a real
                            # incident undiagnosable from outside: a Qwen 400 on
                            # a 13163-token prompt with max_tokens 65536 — 79k
                            # against a 262144 window, so no context overflow —
                            # and the only surviving evidence was the status
                            # code. The body is the upstream's own error text
                            # (never our credentials), truncated because some
                            # backends echo the request.
                            log.warning(
                                "relay: upstream %s from model=%s endpoint=%s body=%s",
                                resp.status_code,
                                ep2.model_name,
                                ep2.id,
                                resp.content[:600].decode("utf-8", "replace"),
                            )
                            yield ("error", str(resp.status_code))
                            yield ("content", f"(model error {resp.status_code})")
                            return
                        # id_prefix=request_id scopes the SYNTHETIC call ids
                        # the relay mints when an upstream omits them (audit
                        # 2026-10-26, F-4). It must be unique per TURN, not per
                        # model call: stream_model_call runs once per iteration,
                        # so a per-call counter would restart at 1 every
                        # iteration and two iterations would mint the same id —
                        # which the client joins by, collapsing two calls onto
                        # one card.
                        async for ev in toolrelay.stream_model_call(
                            resp, id_prefix=request_id, id_counter=relay_id_counter
                        ):
                            yield ev
                    finally:
                        await resp.aclose()

                return gen()

            # Mid-relay failover chain: the NEXT cascade candidates with
            # healthy chat pools, as ready-to-call stream factories. When the
            # mid-loop budget check exhausts the current model, the relay
            # continues on the first fallback whose budget has headroom — the
            # conversation survives instead of ending answerless. The tail
            # comes from the profile ordering for BOTH alias and pinned
            # requests (a pinned GLM that dies of budget mid-stream flows
            # into the ordering's next model — the same promotion admission
            # applies; see _relay_fallback_tail).
            # Bounded like the admission path (model_fallbacks contract: the caller's
            # chain is already sliced to settings.model_fallback_max) — a mid-stream
            # failover walk must not run the entire profile ordering.
            relay_tail = _relay_fallback_tail(model, resolution.order or [], registered, key.allowed_models)[
                : settings.model_fallback_max
            ]
            fallbacks: list[tuple[str, Any]] = []
            if relay_tail:
                for cand in relay_tail:
                    fp = model_router.pool(cand)
                    if fp is None or not fp.healthy():
                        continue
                    # serves_chat's any() predicate — the ONE chat-type rule,
                    # shared with the admission gates and the cascade walk. The
                    # old endpoints[0] test dropped a servable fallback from the
                    # relay tail whenever a pool's FIRST endpoint happened to be
                    # non-chat (endpoint re-registration order is not a policy).
                    if not serves_chat(cand):
                        continue

                    def _fb(p=fp):
                        def _stream(msgs, tools):
                            return stream_once(msgs, tools, pool_for=p)

                        return _stream

                    fallbacks.append((cand, _fb()))
            failover_note = (
                f"automatically failed over from {fallback_from} to {model} (rate or budget limit)"
                if fallback_from
                else ""
            )

            # Tool-result compression (X-PCAI-Compress-Tools): resolved ONCE
            # per request from the header, falling back to the deployment
            # default. "off" is byte-for-byte the pre-feature behavior. The
            # MODE is announced on the response headers before the stream
            # starts; the saving itself rides in-stream pcai.compress events,
            # because headers are sent before any result exists.
            compress_mode = toolcompress.resolve_mode(
                request.headers.get("x-pcai-compress-tools"), settings.tool_compress_default
            )
            compress_stats: list[dict] = []

            async def relay_gen():
                iteration_depth = 0  # observed in finally — see below
                relay_iterations: list[int] = []  # C-8 out-param (see below)
                try:
                    async for chunk in toolrelay.relay_stream(
                        relay_messages,
                        # OFFERED (agent-narrowed) grants, NOT the key's full
                        # set: the surface and the dispatch gate must agree with
                        # what the model was shown. The exfil gate above still
                        # reads the full set.
                        offered_grants,
                        catalog,
                        stream_once,
                        execute,
                        request_id,
                        model,
                        key_id=key.id,
                        user_id=key.user_id,
                        key_fraction=off_base,
                        # GOVERNED PER PROFILE (2026-11-03): the relay cap is a
                        # BUDGET IN UNITS, not a round count. The profile's
                        # tool_relay_budget overrides the site default (NULL =
                        # inherit settings.tool_relay_max_iterations), and each
                        # call charges its SERVER's weight (mcp_servers.tool_weight
                        # — 0.1 = a cheap call, 3 = an expensive one). Weights are
                        # TTL-cached and degrade to {} (= every server at 1.0) on
                        # a DB fault, so governance can never fail a request.
                        max_iterations=key.tool_relay_budget,
                        tool_weights=await mcp_weights.weights(),
                        started=started,
                        estimate=lambda msgs: _estimate_tokens({**body, "messages": msgs}),
                        # Per-model max_tokens ceiling for the relay path: the
                        # value is only the ESTIMATE fallback here (the wire
                        # body is built in stream_once below, where the clamp
                        # is authoritative) — clamped for the serving model so
                        # the two cannot disagree.
                        max_tokens=clamp_max_tokens(body, model, settings.max_tokens_ceiling).get("max_tokens"),
                        client_tools=list(body.get("tools") or []),
                        client_loop_guard=client_loop_guard,
                        # Forced like the plain-stream path (~line 1731): the
                        # relay must emit the standard final usage chunk even
                        # when the client never asked — OpenAI-compatible
                        # clients (DSH's context meter) otherwise settle on
                        # zero and misreport every relayed turn's usage
                        # (2026-10-08 live report: "Usage 0 tok" on relayed
                        # steps while plain turns reported fine).
                        include_usage=True,
                        stream_thinking=stream_thinking,
                        # Deterministic tool-result compression + the
                        # synthetic pcai__retrieve tool (both resolved above).
                        compress_mode=compress_mode,
                        compress_stats_out=compress_stats,
                        fallbacks=fallbacks,
                        failover_note=failover_note,
                        continuation=continuation,
                        # Wave R1 stamped render deltas (both-gates resolved
                        # above): display-only tool_call chunks for clients
                        # that opted in via X-PCAI-Relay-Deltas: 1.
                        render_deltas=render_deltas,
                        render_index_base=settings.tool_relay_render_index_base,
                        # The agent profile's system prompt ('' = none). Applied
                        # per CALL by toolrelay.with_system_prefix — never to
                        # relay_msgs, whose length is the handback boundary.
                        system_prefix=agent.system_prompt if agent is not None else "",
                        # Wave B2 client-tool dispatch: the executor (None
                        # when the flag is off, no offer, or wiring failed —
                        # handback behavior) and the shared wall-clock
                        # deadline the guard + dispatch both run under.
                        client_tool_execute=client_tool_execute,
                        wall_clock_deadline=relay_deadline,
                        # Per-turn loop accounting (2026-09-25): this
                        # request's guard budget starts where the TURN's
                        # prior handback continuations left off — 0 on a
                        # fresh user turn (see _reconcile_relay_messages for
                        # the reset decision).
                        iterations_prior=iterations_prior,
                        # Wave C gateway-owned state: persist the FULL final
                        # message list (tool results + final answer) under the
                        # conversation's thread fingerprint at loop end — the
                        # substrate a next-wave client's session-ref
                        # continuation reads instead of a full re-send.
                        conversation_state=_persist_conversation_state(
                            key, canonical_input=canonical_input, relay_projection=relay_messages
                        ),
                        # RFC-PER-KEY-STREAM-EVENTS: key default + header
                        # override resolved above.
                        stream_pcai_events=stream_events,
                        # C-8 (audit 2026-10-29): the relay's OWN iteration
                        # count, published as it goes. The histogram used to be
                        # derived by byte-matching the emitted pcai model_usage
                        # event, which reads 0 for every X-PCAI-Stream-Events: 0
                        # client (all strict OpenAI clients) and is the wrong
                        # signal for a metric that means "how deep did the loop
                        # go". This is the loop's accounting, not a re-derivation.
                        iterations_out=relay_iterations,
                    ):
                        # C-8 (audit 2026-10-29): the depth now comes from the
                        # relay's own counter (iterations_out, above), so this
                        # loop is a pure pass-through — no byte-matching, which
                        # read 0 for strict clients and could be perturbed by
                        # model text. iteration_depth is refreshed after the
                        # loop from the out-param.
                        yield chunk
                finally:
                    # Every exit path (done, guard, disconnect) observes the
                    # depth reached; histogram in [0..max_iterations]. The
                    # relay publishes after every completed call, so a closed
                    # generator still carries the depth it reached.
                    if relay_iterations:
                        iteration_depth = relay_iterations[0]
                    _metrics.observe_relay_iterations(model, iteration_depth)
                    if compress_stats:
                        # Honest accounting: only entries that actually
                        # REWROTE the text count as savings. A store failure
                        # ("stored": false) and every shadow entry kept the
                        # original, so their before/after delta is
                        # hypothetical — report those separately.
                        rewritten = [s for s in compress_stats if s.get("mode") == "on" and s.get("stored", True)]
                        shadowed = [s for s in compress_stats if s.get("mode") == "shadow"]
                        if rewritten:
                            log.info(
                                "relay: compressed %d tool result(s), %d chars saved (request_id=%s)",
                                len(rewritten),
                                sum(int(s.get("before", 0)) - int(s.get("after", 0)) for s in rewritten),
                                request_id,
                            )
                        if shadowed:
                            log.info(
                                "relay: compression shadow would save %d chars across %d result(s) (request_id=%s)",
                                sum(int(s.get("before", 0)) - int(s.get("after", 0)) for s in shadowed),
                                len(shadowed),
                                request_id,
                            )
                    release_slot()

            stream_headers = _relay_headers(
                headers,
                request_id,
                offpeak.effective_for(model, off_base),
                model_routing.route_header(route_ctx, model),
                served_model=model,
                endpoint_id=ep.id,
                fallback_from=fallback_from,
            )
            stream_headers["Cache-Control"] = "no-cache"
            stream_headers["X-PCAI-Relay"] = "streaming"
            if group_name is not None:
                # The route was a GROUP: name it (the member rides
                # X-PCAI-Served-Model, the endpoint X-PCAI-Endpoint).
                stream_headers["X-PCAI-Group"] = group_name
            if compress_mode != "off":
                stream_headers["X-PCAI-Compress-Tools"] = compress_mode
            if guardrail_post:
                # Post-call guardrails on the relayed stream: buffer, judge
                # the FINAL answer, replay (verbatim / masked / blocked).
                # Pre-call verdicts ride the marker row with the post ones.
                return _SlotReleasingStreamingResponse(
                    _guarded_sse(
                        relay_gen(),
                        None,
                        guardrail_post,
                        key=key,
                        model=model,
                        request_id=request_id,
                        started=started,
                        relay_mode=True,
                        pre_events=guard_events,
                    ),
                    media_type="text/event-stream",
                    headers=stream_headers,
                    release_slot=release_slot,
                )
            return _SlotReleasingStreamingResponse(
                relay_gen(),
                media_type="text/event-stream",
                headers=stream_headers,
                release_slot=release_slot,
            )
    except BaseException:
        # Slot-leak fix (security audit): EVERY post-gate raise must give back
        # the concurrency slot _run_gates took — relay catalog failures,
        # unexpected DB errors, the 503 above. Re-raised untouched.
        # Idempotent (audit 2026-11-10 P1-2): the same slot is also released by
        # the relay generator's finally / _stream_chat's own 502 path, and the
        # once-wrapper makes the overlap harmless.
        release_slot()
        raise

    # --- agent system prompt on the PLAIN path (no relay) -------------------
    # The relay path applies the prompt per CALL inside toolrelay (so it never
    # enters the canonical store or the handback boundary). The plain path has no
    # such seam, so the body is COPIED here with the prompt prepended — after the
    # canonical capture was scheduled (it holds the original object) and after
    # the guardrails ran (an operator's own prose is not what a PII guardrail
    # should be scanning). Both plain dispatches below (_stream_chat and
    # _proxy_json) read this body.
    # The prompt rewrite and the streaming dispatch are INSIDE the release
    # guard (audit 2026-11-10 P1-2). Pre-fix the plain streaming branch sat
    # outside BOTH release paths — the relay's except above and the
    # non-streaming finally further down — so any raise between the gates and
    # response construction leaked the concurrency slot: a client disconnect
    # during the up-to-45s TTFT peek (_peek_first_data_line re-raises
    # cancellation and the call site only handles httpx2.HTTPError), an
    # unguarded resp.aclose(), or a with_system_prefix fault. The leaked
    # counter never decrements, so the key is 429-bricked on its concurrency
    # cap until the pod restarts (see the P1 note at _StreamLedger). The
    # release is idempotent, so the generator's own release stays authoritative
    # once the stream is handed off.
    try:
        if agent is not None and agent.system_prompt:
            body = {
                **body,
                "messages": toolrelay.with_system_prefix(list(body.get("messages") or []), agent.system_prompt),
            }
        if bool(body.get("stream")):
            # Streaming: slot ownership TRANSFERS to the stream generator
            # (released in _relay_sse's finally, when the stream actually ends).
            # Releasing here — at response-object construction — would free the
            # slot before a single chunk was consumed, voiding the cap for the
            # exact workload it exists to constrain. _stream_chat wraps the
            # response in _SlotReleasingStreamingResponse, which is the backstop
            # for the one exit the generator cannot cover: a cancellation during
            # http.response.start, before its first __anext__.
            stream_resp = await _stream_chat(
                pool,
                ep,
                url,
                body,
                headers,
                key,
                model,
                est_tokens,
                request_id,
                started,
                release_slot=release_slot,
                post_guards=guardrail_post or None,
                pre_events=guard_events or None,
                route=route_ctx,
                fallback_from=fallback_from,
                fallback_chain=fb_chain,
                affinity_session=affinity_session,
                group_name=group_name,
            )
            if group_name is not None:
                # Headers are still mutable here: the stream has not started
                # (the first body chunk is what sends them).
                stream_resp.headers["X-PCAI-Group"] = group_name
            return stream_resp
    except BaseException:
        # Nothing was handed to a generator, so this request still owns the
        # slot: give it back and re-raise untouched.
        release_slot()
        raise
    try:
        resp = await _proxy_json(
            pool,
            ep,
            url,
            body,
            headers,
            key,
            model,
            "chat",
            est_tokens,
            request_id,
            started,
            pre_events=guard_events or None,
            post_guards=guardrail_post or None,
            route=route_ctx,
            fallback_chain=fb_chain,
            fallback_from=fallback_from,
            want_metadata=want_metadata,
            cache_hash=_cache_hash,
        )
    except BaseException:
        # Transport failure / chain exhausted: the pinned endpoint just
        # proved dead — drop the pin so the next turn re-picks fresh, then
        # re-raise untouched (the slot-leak release below still runs).
        if ep_session:
            from ..core import stickiness as _stick

            await _stick.drop_endpoint_pin(key.id, ep_session)
        # The GROUP pin survives a single failure: the member is still the
        # conversation's model, and re-picking would cost a full re-prefill.
        # It is dropped only when the member's WHOLE pool is unhealthy.
        await _group_pin_drop_if_dead(key, affinity_session, model)
        raise
    finally:
        release_slot()
    # --- group / session affinity commit (non-streaming surface) -----------
    # The streaming surface refreshes the SAME pins, but at its own
    # completion point: _stream_chat hands the ledger an on_complete hook
    # (_pins_after_serve) that runs only when the stream actually finished,
    # so a streamed turn is not a pin-refresh gap. This block is the
    # non-streaming half of the same rule.
    if group_name is not None:
        # The route was a GROUP: name it on the response (the member rides
        # X-PCAI-Served-Model, the endpoint X-PCAI-Endpoint).
        resp.headers["X-PCAI-Group"] = group_name
        if _is_2xx(resp.status_code):
            # The admission walk may have failed over to a SIBLING member:
            # pin what actually served, not the provisional pick.
            await _group_pin_commit(key, affinity_session, group_name, model)
    if ep_session:
        from ..core import stickiness as _stick

        if (
            _is_2xx(resp.status_code)
            and resp.headers.get("X-PCAI-Endpoint") == ep.id
            and resp.headers.get("X-PCAI-Fallback") is None
        ):
            # The pinned endpoint served THIS turn: refresh the pin (and
            # its TTL clock) with what actually answered. Best-effort by
            # contract — a store fault never fails the request.
            await _stick.set_endpoint_pin(key.id, ep_session, ep.id)
        else:
            # Upstream error, or a fallback hop served instead of the pin:
            # the pin no longer names the endpoint that kept this
            # conversation's cache warm — drop it (same trigger that drops
            # it on a breaker trip: the endpoint proved it cannot serve).
            await _stick.drop_endpoint_pin(key.id, ep_session)
    return resp


@router.post("/embeddings")
async def embeddings(
    request: Request,
    key: KeyRecord = fastapi.Depends(require_scope("proxy")),
) -> Response:
    """OpenAI-compatible embeddings proxy with the embedding cache.

    Cache flow: identical (model, input) pairs seen before are answered
    from the cache — zero upstream GPU work, zero budget consumption (no
    gate run, no settle). Misses flow through the normal gate pipeline and
    are stored on success. The RAG app (MultimodalRAG et al.) gets this by
    pointing its embedding base_url at the gateway — no client changes.
    """
    request_id = uuid.uuid4().hex
    started = time.monotonic()

    body = await _json_body(request)  # bounded read (413) + JSON parse (400)
    if not isinstance(body, dict) or "model" not in body or "input" not in body:
        raise fastapi.HTTPException(400, "embeddings request must be a JSON object with 'model' and 'input'")
    model = str(body["model"])
    _check_model_allowed(key, model)
    pool = model_router.pool(model)
    if pool is None:
        raise fastapi.HTTPException(404, f"model {model!r} is not registered")
    if not pool_has_type(pool, "embedding"):
        raise fastapi.HTTPException(400, f"model {model!r} is a {pool.endpoints[0].model_type} endpoint, not embedding")

    # --- cache hit path: serve without gates or ledger write ---
    # STRING inputs only: a list input would (a) key on an unstable str(list)
    # repr and (b) on a hit return ONE embedding where N were requested —
    # silently corrupt for RAG batch calls. Per-item fanout is a future
    # refinement; list inputs pass through untouched today.
    input_str = body["input"] if isinstance(body["input"], str) else None
    cached = await embedcache.lookup(model, input_str) if input_str is not None else None
    if cached is not None:
        latency = _latency_ms(started)
        est = _estimate_embedding_tokens(body)
        # visible + free: a ledger row the budgets SUM ignores. P10: the count
        # is the len//4 ESTIMATE — tagged so chargeback reads it honestly.
        await settle(
            key.id,
            model,
            "embeddings",
            est,
            0,
            "cache_hit",
            latency,
            request_id,
            cached_tokens=est,
            billable_units=float(est),
            unit_kind="token_estimate",
        )
        _log_request(key.key_prefix, model, None, "cache_hit", latency, request_id)
        return fastapi.responses.JSONResponse(
            {
                "object": "list",
                "model": model,
                "data": [{"object": "embedding", "index": 0, "embedding": cached}],
                "usage": {"prompt_tokens": 0, "total_tokens": 0},
            },
            headers={"X-Request-ID": request_id, "X-Cache": "HIT"},
        )

    est_tokens = _estimate_embedding_tokens(body)
    gate = await _run_gates(key, model, est_tokens, "embeddings", request_id, started)
    warn_level = gate.warn_level

    ep = pool.pick()
    if ep is None:
        await settle(
            key.id,
            model,
            "embeddings",
            0,
            0,
            "error",
            _latency_ms(started),
            request_id,
        )
        _log_request(key.key_prefix, model, None, "error", _latency_ms(started), request_id)
        # Same release-before-raise reasoning as the chat path above.
        _release_in_flight(key.id)
        raise fastapi.HTTPException(503, "no healthy upstream")

    url = f"{ep.base_url}/embeddings"
    headers = dict(_upstream_headers(ep, request_id))
    headers.update(gate.rate_headers)
    if warn_level:
        headers["X-Budget-Warning"] = f"{warn_level}% of a period budget consumed"
    try:
        return await _proxy_json(
            pool,
            ep,
            url,
            body,
            headers,
            key,
            model,
            "embeddings",
            est_tokens,
            request_id,
            started,
            cache_input=input_str,
        )
    finally:
        _release_in_flight(key.id)


@router.post("/rerank")
async def rerank(
    request: Request,
    key: KeyRecord = fastapi.Depends(require_scope("proxy")),
) -> Response:
    """OpenAI-style rerank proxy (vLLM's `/rerank`), gated and ledgered.

    The fleet already runs rerankers (Qwen3-VL-Reranker) that the RAG
    pipeline calls DIRECTLY at the upstream; this route puts them behind the
    same key, budget/rate gates, ledger rows and served-model headers as
    chat and embeddings, so a retrieval hop becomes attributable and
    metered like every other call.

    The body is forwarded UNCHANGED: `query` and `documents` may be
    strings or multimodal content dicts, and vendor knobs such as
    `mm_processor_kwargs` pass straight through. The upstream's own
    contract is the contract - the gateway is a governor here, not a
    translator.

    NO response cache: a rerank answer is a score vector tied to one
    (query, document-set) pair, and this fleet's rerank traffic is one-shot
    (same reasoning that keeps semantic caching out of scope). Token
    estimate is the len//4 heuristic over query + documents; settle prefers
    the upstream's own usage when it reports one, and falls back to the
    estimate when it does not (vLLM reports total_tokens, not
    prompt_tokens).
    """
    request_id = uuid.uuid4().hex
    started = time.monotonic()

    body = await _json_body(request)  # bounded read (413) + JSON parse (400)
    if not isinstance(body, dict) or "model" not in body or "query" not in body or "documents" not in body:
        raise fastapi.HTTPException(
            400,
            "rerank request must be a JSON object with 'model', 'query' and 'documents'",
        )
    model = str(body["model"])
    _check_model_allowed(key, model)
    pool = model_router.pool(model)
    if pool is None:
        raise fastapi.HTTPException(404, f"model {model!r} is not registered")
    if not pool_has_type(pool, "reranker"):
        raise fastapi.HTTPException(400, f"model {model!r} is a {pool.endpoints[0].model_type} endpoint, not reranker")

    est_tokens = _estimate_rerank_tokens(body)
    gate = await _run_gates(key, model, est_tokens, "rerank", request_id, started)
    warn_level = gate.warn_level

    ep = pool.pick()
    if ep is None:
        await settle(
            key.id,
            model,
            "rerank",
            0,
            0,
            "error",
            _latency_ms(started),
            request_id,
        )
        _log_request(key.key_prefix, model, None, "error", _latency_ms(started), request_id)
        # Same release-before-raise reasoning as the chat/embeddings paths.
        _release_in_flight(key.id)
        raise fastapi.HTTPException(503, "no healthy upstream")

    url = f"{ep.base_url}/rerank"
    headers = dict(_upstream_headers(ep, request_id))
    headers.update(gate.rate_headers)
    if warn_level:
        headers["X-Budget-Warning"] = f"{warn_level}% of a period budget consumed"
    try:
        return await _proxy_json(
            pool,
            ep,
            url,
            body,
            headers,
            key,
            model,
            "rerank",
            est_tokens,
            request_id,
            started,
        )
    finally:
        _release_in_flight(key.id)


# Native-path alias. vLLM serves rerank at BOTH /rerank and /v1/rerank, and
# clients that speak the native shape strip the trailing "/v1" from their
# base_url before posting (the RAG pipeline's MultiModalReranker does exactly
# that: `base_url[:-3]` then POST "/rerank"). Without this alias such a client
# pointed at the gateway would 404 - and the tempting fix is to point it back
# at the upstream, which is the governance bypass the route exists to close.
# Same handler, both spellings.
rerank_alias_router = fastapi.APIRouter(tags=["proxy"])


@rerank_alias_router.post("/rerank")
async def rerank_native_path(
    request: Request,
    key: KeyRecord = fastapi.Depends(require_scope("proxy")),
) -> Response:
    """POST /rerank (no /v1) - the native vLLM spelling of /v1/rerank."""
    return await rerank(request, key)


# --------------------------------------------------------------------------
# audio passthrough: /v1/audio/speech (TTS) and /v1/audio/transcriptions (ASR)
# --------------------------------------------------------------------------
#
# Transparent proxy over the fleet's voice endpoints (pool model_type
# "voice"): the request body BYTES leave exactly as they arrived (JSON for
# speech, multipart for transcriptions — the audio payload is never parsed,
# decoded or re-encoded) and the upstream's status, content-type and body
# bytes come back the same way. Added value is the control plane: key auth,
# allowed-model enforcement, the pool's voice-type gate, budgets/rate gates,
# the circuit breaker, and ONE ledger row per request settling an ESTIMATE
# (audio endpoints report no token usage; a zero-token row would be a
# budget bypass — see settings.audio_tts_chars_per_token /
# audio_asr_tokens_per_second).


@router.post("/audio/speech")
async def audio_speech(
    request: Request,
    key: KeyRecord = fastapi.Depends(require_scope("proxy")),
) -> Response:
    """OpenAI-compatible TTS passthrough: JSON in, audio bytes out.

    Model resolution is the JSON body's `model` field (the OpenAI speech
    shape); the body's BYTES are forwarded unchanged (the client's
    Content-Type rides upstream, not a re-serialization). Token estimate:
    len(input) // audio_tts_chars_per_token — the same len//4-family
    heuristic the text path uses, so a TTS call bills proportionally to
    the text it speaks.
    """
    request_id = uuid.uuid4().hex
    started = time.monotonic()

    body = await _raw_body(request)
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        raise fastapi.HTTPException(400, "speech request must be a JSON body") from None
    if not isinstance(payload, dict) or not payload.get("model"):
        raise fastapi.HTTPException(400, "speech request must be a JSON object with a 'model' field")
    model = str(payload["model"])
    _check_model_allowed(key, model)
    pool = model_router.pool(model)
    if pool is None:
        raise fastapi.HTTPException(404, f"model {model!r} is not registered")
    if not pool_has_type(pool, "voice"):
        raise fastapi.HTTPException(400, f"model {model!r} is a {pool.endpoints[0].model_type} endpoint, not voice")

    # TTS estimate: characters of the spoken text (4 chars ~ 1 token). A
    # non-string input (list of strings) is summed the same way; anything
    # else estimates 0 — the gates still run on the request axis.
    # P10: this char count is ALSO the row's provenance quantity — when the
    # upstream reports no real duration, the settle tags unit_kind
    # 'char_estimate' with billable_units = chars, so the ledger says exactly
    # what was measured ("we billed a token proxy over N chars") instead of
    # passing the proxy off as a token measurement.
    text = payload.get("input")
    if isinstance(text, str):
        chars = len(text)
    elif isinstance(text, list):
        chars = sum(len(t) for t in text if isinstance(t, str))
    else:
        chars = 0
    est_tokens = max(0, chars // settings.audio_tts_chars_per_token)

    gate = await _run_gates(key, model, est_tokens, "audio.speech", request_id, started)

    ep = pool.pick()
    if ep is None:
        await settle(key.id, model, "audio.speech", 0, 0, "error", _latency_ms(started), request_id)
        _log_request(key.key_prefix, model, None, "error", _latency_ms(started), request_id)
        _release_in_flight(key.id)
        raise fastapi.HTTPException(503, "no healthy upstream")

    url = f"{ep.base_url}/audio/speech"
    try:
        return await _proxy_audio_raw(
            pool,
            ep,
            url,
            body,
            request.headers.get("content-type") or "application/json",
            key,
            model,
            "audio.speech",
            est_tokens,
            request_id,
            started,
            gate,
            # P10: real duration (upstream response headers) bills per SECOND;
            # otherwise the char proxy rides, tagged, exactly as before.
            estimate_units=float(chars),
            unit_kind_estimate="char_estimate",
        )
    finally:
        _release_in_flight(key.id)


@router.post("/audio/transcriptions")
async def audio_transcriptions(
    request: Request,
    key: KeyRecord = fastapi.Depends(require_scope("proxy")),
) -> Response:
    """OpenAI-compatible ASR passthrough: audio bytes in, JSON out.

    Model resolution: OpenAI SDKs send `model` INSIDE the multipart body,
    where the gateway's usual JSON-body resolution cannot see it, so the
    order is (1) ?model= query, (2) X-PCAI-Model header, (3) the multipart
    model field scanned out of the raw body — and a loud 400 when none
    yields a name. The body BYTES are forwarded unchanged (multipart stays
    multipart; the audio is never parsed). Token estimate: audio duration
    at the fleet's assumed 16 kHz 16-bit mono rate (audio_asr_bytes_per_second,
    default ~32 kB/s) times audio_asr_tokens_per_second (~3/s ≈ 150 spoken
    words/min), floored at one second so a tiny upload still bills the
    request it carried.

    P10 duration (hardened by cross-validation wave 2) — precedence, unit,
    bound: the row bills in SECONDS from the first REAL duration available,
    in this order (1) an upstream-reported duration response header — the
    server that decoded the audio measured it, so it beats anything the
    caller says; (2) the ?duration= query param in SECONDS; (3) the
    X-PCAI-Duration header (same, seconds). The caller hint is a PRICE INPUT,
    not trusted metadata: it is clamped into the upload's compression
    envelope [byte_proxy/16, byte_proxy*16] seconds (a compressed upload is
    never more than ~16x smaller than raw PCM and never larger — outside
    that window the hint would let a caller bill 0.001 s of ASR as free or
    1e8 s as a 1500 USD row) with a WARNING naming the clamp. With no
    duration at all, the byte proxy rides tagged unit_kind='second_estimate'
    with billable_units = the derived seconds. All three paths price through
    model_costs.unit='second' (a token-priced model ledgers NULL + a
    WARNING — never a wrong-rate conversion). Clients SHOULD send the hint
    for compressed uploads (mp3/opus/m4a are 2-8x smaller than raw PCM in
    bytes — the byte proxy under-bills them).
    """
    request_id = uuid.uuid4().hex
    started = time.monotonic()

    body = await _raw_body(request)
    # Query first, header second, multipart scan last: explicit beats
    # ambient beats embedded. All three are stripped before the ACL so a
    # padded value cannot sneak past the allowed-models list.
    model = request.query_params.get("model") or request.headers.get("x-pcai-model") or _asr_model_from_multipart(body)
    model = (model or "").strip()
    if not model:
        raise fastapi.HTTPException(
            400,
            "transcription request must name a model: pass ?model=, the X-PCAI-Model "
            "header, or a 'model' field in the multipart body",
        )
    _check_model_allowed(key, model)
    pool = model_router.pool(model)
    if pool is None:
        raise fastapi.HTTPException(404, f"model {model!r} is not registered")
    if not pool_has_type(pool, "voice"):
        raise fastapi.HTTPException(400, f"model {model!r} is a {pool.endpoints[0].model_type} endpoint, not voice")

    # P10 duration hint: ?duration= (seconds) first, X-PCAI-Duration header
    # second — explicit beats ambient, the SAME precedence the model name
    # uses above. A junk/negative hint is ignored, not a 400: billing
    # metadata must never be a client-facing failure (the byte proxy is
    # always available as the fallback). It is also CLAMPED into the
    # upload's compression envelope once the byte-proxy estimate exists
    # below — the hint is a price input, and an unbounded one billed
    # 0.001 s as free ASR and 1e8 s as a 1500 USD row (both reproduced).
    duration_hint = _positive_float(request.query_params.get("duration"))
    if duration_hint is None:
        duration_hint = _positive_float(request.headers.get("x-pcai-duration"))

    # ASR estimate: raw body bytes / audio_asr_bytes_per_second (the default
    # 32000 assumes RAW 16 kHz 16-bit mono ≈ 32 kB/s; compressed formats are
    # 2-8x denser — send the duration hint instead), floored at 1 second — a
    # multipart envelope around a tiny clip still carried a real
    # transcription request.
    est_seconds = max(1.0, len(body) / settings.audio_asr_bytes_per_second)
    est_tokens = max(1, int(est_seconds * settings.audio_asr_tokens_per_second))
    # The envelope anchor is the byte proxy (the only quantity the gateway
    # measures itself): clamp the caller hint INTO it, both ways, WARNING on
    # the rewrite. The upstream-reported duration (which wins over the hint)
    # is NOT clamped — a measurement is not a claim.
    if duration_hint is not None:
        duration_hint = _clamp_duration_hint(duration_hint, est_seconds)

    gate = await _run_gates(key, model, est_tokens, "audio.transcriptions", request_id, started)

    ep = pool.pick()
    if ep is None:
        await settle(key.id, model, "audio.transcriptions", 0, 0, "error", _latency_ms(started), request_id)
        _log_request(key.key_prefix, model, None, "error", _latency_ms(started), request_id)
        _release_in_flight(key.id)
        raise fastapi.HTTPException(503, "no healthy upstream")

    url = f"{ep.base_url}/audio/transcriptions"
    try:
        return await _proxy_audio_raw(
            pool,
            ep,
            url,
            body,
            request.headers.get("content-type") or "multipart/form-data",
            key,
            model,
            "audio.transcriptions",
            est_tokens,
            request_id,
            started,
            gate,
            # P10: a real duration (hint, then upstream header) bills per
            # SECOND; otherwise the byte proxy rides, tagged, exactly as before.
            estimate_units=est_seconds,
            unit_kind_estimate="second_estimate",
            duration_hint=duration_hint,
        )
    finally:
        _release_in_flight(key.id)


@router.post("/responses")
async def responses_not_implemented() -> None:
    """The OpenAI Responses API is deliberately NOT implemented — a self-
    explaining 501 instead of a confusing 404.

    WHY (2026-10-11): two blockers, one each side of the proxy.
    UPSTREAM (SGLang): the plain `sglang serve` HTTP server exposes
    /v1/responses with limited compatibility — array-form input (the standard
    Responses shape) fails validation (sgl-project/sglang#17853), and custom
    function tools are unsupported (sgl-project/sglang#13292: only built-in
    tools + MCP). Every registered upstream here is SGLang, so a translated
    request with tools would break at the model call — the exact surface the
    relay exists to serve.
    GATEWAY: the relay machinery (SSE chunk parser, settle-once ledger,
    guardrail events, PCAI stream events, handback continuation) is built
    on chat.completions chunk shapes; a Responses facade is a translation
    project (Responses in -> chat/completions out -> Responses events back),
    planned, not yet built.

    The rejection is LOUD and route-shaped on purpose: an Agents-SDK client
    gets a machine-readable "use /v1/chat/completions" instead of a 404 that
    reads as "wrong URL". No ledger row: nothing was admitted, nothing was
    spent (the settle-once discipline counts refusals only at gates this
    request actually passed — auth has not even run here).
    """
    raise fastapi.HTTPException(
        501,
        detail={
            "type": "not_implemented",
            "code": "responses_api_unsupported",
            "message": (
                "The OpenAI Responses API is not implemented by this gateway. "
                "Use POST /v1/chat/completions (fully supported, including "
                "streaming and tool calls). The Responses facade is planned; "
                "it is blocked upstream on SGLang /v1/responses parity "
                "(sgl-project/sglang#17853 array-input validation, #13292 "
                "custom function tools)."
            ),
        },
    )


@router.get("/models")
async def list_models(
    key: KeyRecord = fastapi.Depends(require_scope("proxy")),
    # LAST + defaulted on purpose: FastAPI injects the real Response here (it
    # is a non-field param, detected by annotation), while a DIRECT call —
    # every /v1/models test calls this coroutine positionally with just a key
    # — gets None and skips the header. Putting it first (the obvious shape)
    # would silently rebind those callers' key argument to the response slot.
    response: fastapi.Response = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """List registered virtual models in OpenAI /v1/models shape.

    Read-only listing: no budget/rate-limit gate and no ledger row — the
    listing itself consumes no model tokens and polluting the ledger would
    skew spend reporting.

    When the key's profile carries a model ordering, TWO synthetic cascade
    entries PRECEDE the registered models: `auto` (the legacy pure
    profile-order cascade — what `auto` has always meant) and
    `auto-demand-aware` (the same walk with the load-aware sticky/off-peak
    head engaged, marked `pcai.demand_aware: true`). Requests naming either
    resolve through the ordering (first candidate with budget headroom — the
    alias path), and the ordering rides `pcai.order` so clients can show
    what each alias means. Advertising both makes the feature OPT-IN PER
    CLIENT (switch the name) and makes rollback a name switch, with no
    redeploy. The chat console's model dropdown picks this up with no
    special-casing.
    """
    allowed = set(key.allowed_models) if key.allowed_models else None
    every_registered = model_router.models()
    registered = [m for m in every_registered if allowed is None or m in allowed]
    # ANNOUNCE THE TRUNCATION (2026-10-23). The ACL filter above is silent by
    # construction: a key whose allowed_models omits a model gets a shorter
    # list and no signal, so "the gateway does not serve that model" and "my
    # key may not use it" look identical from the client side — the exact
    # confusion that made a missing Qwen in the console dropdown an
    # unexplainable bug rather than a one-line ACL answer. The count is
    # registered-minus-shown, so it is 0 for an allow-all key and 0 when every
    # registered model is allowed. HEADER ONLY: the JSON body below stays
    # byte-identical, so every existing client is unaffected.
    if response is not None:  # None only on a direct (non-HTTP) call
        response.headers["X-PCAI-Models-Denied"] = str(len(every_registered) - len(registered))
    # Caller-aware metadata (roadmap §6, documented intent now shipped): each
    # registered entry gains `pcai.type` (the pool's endpoint model_type —
    # chat|embedding|reranker|voice, so an OpenAI-SDK client can tell what a
    # name serves without probing) and `pcai.primary: true` on the FIRST
    # model of the profile cascade that this key can actually call (the
    # cascade's natural default — the same model `auto` resolves to when
    # every candidate has headroom). The chat console has rendered both
    # fields since the meta-chip wave (m.pcai?.primary / m.pcai?.type) —
    # this lights them up for EVERY OpenAI-compatible consumer with zero
    # UI change. Unprofiled keys: no primary (nothing to prefer).
    order = await profile_order_for_key(key.id)
    registered_set = set(registered)
    # CHAT-ONLY (2026-10-29): `primary` means "what `auto` would resolve to",
    # and `auto` is chat-only now (core.cascade.serves_chat) — a non-chat head
    # must not be marked primary, or the console preselects an embedding pool in
    # the model dropdown and the first send 400s.
    primary = next((m for m in order if m in registered_set and serves_chat(m)), "")

    def _pool_type(m: str) -> str:
        # any(), not endpoints[0] (audit 2026-11-10 P3): registration order is
        # not a policy, so a pool whose FIRST endpoint happens to be another
        # type used to advertise the wrong pcai.type for a model that does
        # serve chat — the same defect cascade.pool_has_type was introduced to
        # replace on the request paths. The vocabulary is the one this listing
        # documents (chat|embedding|reranker|voice); "chat" first because it is
        # the surface a mixed pool is overwhelmingly reached for, and because
        # an unknown/empty pool keeps the old fail-open default.
        p = model_router.pool(m)
        for _t in ("chat", "embedding", "reranker", "voice"):
            if pool_has_type(p, _t):
                return _t
        return "chat"

    # CAPACITY (operator requirement, 2026-10-28): model_list should show, for
    # a given api key, what models still have token capacity at that moment.
    # model_capacity is NOT-N: a FIXED set of round trips for the whole listing
    # (2 catalog probes + 2 aggregates + 2 lineage lookups, plus — only for a
    # key with a capped enrollment — one team aggregate and at most one overflow
    # probe per configured cap scope), so a 40-model listing costs the same as a
    # 2-model one.
    #
    # ANNOTATE, do not filter. Dropping exhausted entries would make a drained
    # budget indistinguishable from an ACL narrowing or a registry outage --
    # exactly the confusion X-PCAI-Models-Denied was added to fix (see the
    # truncation note above). pcai.capacity lets a client SEE the headroom and
    # decide; the admission gate still enforces either way, and model_capacity
    # fails open, so a budget-table fault degrades to the previous listing
    # rather than an empty one.
    try:
        from ..core import budgets as _budgets

        capacity = await _budgets.model_capacity(key.id, registered)
    except Exception as exc:
        log.warning("/v1/models: capacity lookup failed (fail-open): %s", exc)
        capacity = {}

    data = [
        {
            "id": m,
            "object": "model",
            "created": int(time.time()),
            "owned_by": "pcai-llm-gateway",
            # Capacity the UPSTREAM publishes, so a client does not have to
            # hardcode what the server already knows. DSH's model discovery
            # reads exactly these two field names; absent means "not learned",
            # never a guess.
            **_advertised_caps([m]),
            "pcai": {
                "type": _pool_type(m),
                **({"primary": True} if m == primary else {}),
                **({"capacity": capacity[m]} if m in capacity else {}),
            },
        }
        for m in registered
    ]
    # --- MODEL GROUPS (F3) -------------------------------------------------
    # A group is a callable model id, so it is advertised like one: one entry
    # per group this key may use, owned_by 'group' (a client can tell a
    # SELECTION SET from a real pool), sized by the SMALLEST member window —
    # the same 'an alias is sized by its smallest candidate' rule the auto
    # entry follows. A group the key's ACL reaches neither by name nor
    # through a member is never listed: an entry that always 403s is a lie.
    if settings.model_groups_enabled:
        # AWAIT the load (10s TTL inside the core module): the group map is
        # read from the cache below, and a cold cache would hide every group
        # until the first admin write.
        for _gname, _group in (await _groups_load()).items():
            _gacl = _group_acl(_group, key)
            _gmembers = [
                m for m in _group.members if (_gacl is None or m in _gacl) and model_router.pool(m) is not None
            ]
            if not _gmembers:
                continue
            data.append(
                {
                    "id": _gname,
                    "object": "model",
                    "created": int(time.time()),
                    "owned_by": "group",
                    **_advertised_caps(_gmembers),
                    "pcai": {"type": "group", "members": _gmembers},
                }
            )
    # `auto` only when the ordering offers at least one callable candidate —
    # an auto with no resolvable model would just 404 at send time.
    # (order/registered_set computed above for the primary flag.)
    #
    # The ALIAS PAIR (operator decision, 2026-10-04): `auto` is the LEGACY
    # pure profile-order cascade and `auto-demand-aware` is the SAME walk
    # with the load-aware head engaged (sticky head, off-peak ranking,
    # health gate) — see core.cascade's "two aliases, one walk". Both are
    # advertised under the identical callable-candidate condition, and
    # `auto` keeps its exact entry shape (asserted by tests). The new entry
    # carries `pcai.demand_aware: true` as the distinguishing marker, so a
    # client can tell the pair apart without knowing the names.
    if any(m in registered_set for m in order):
        data.insert(
            0,
            {
                "id": "auto",
                "object": "model",
                "created": int(time.time()),
                "owned_by": "pcai-llm-gateway",
                # An alias is sized by its SMALLEST candidate: the walk may
                # pick any of them, so advertising the biggest window would be
                # the 1M lie that hid the Qwen 400.
                **_advertised_caps([m for m in order if m in registered_set]),
                "pcai": {"type": "cascade", "alias": True, "order": order},
            },
        )
        data.insert(
            1,
            {
                "id": DEMAND_AWARE_ALIAS,
                "object": "model",
                "created": int(time.time()),
                "owned_by": "pcai-llm-gateway",
                **_advertised_caps([m for m in order if m in registered_set]),
                "pcai": {
                    "type": "cascade",
                    "alias": True,
                    "order": order,
                    "demand_aware": True,
                },
            },
        )
    # Complexity-routing catalog entries (caller-aware, minimal): when the
    # CALLING key's profile opted into routing (and the master switch is
    # on), advertise the pseudo-models alongside the real ones so clients
    # can discover them like any other model. Off-policy keys see nothing
    # new — for them the names behave as plain aliases, and listing them
    # would suggest semantics the profile has not signed up for. Each entry
    # carries pcai.type="routing" next to the auto entry's pcai.type="cascade".
    try:
        policy, _min_model = await model_routing.profile_routing_config(key.user_id)
    except Exception:
        policy = ""  # fail-safe: a lookup blip must not break the listing
        _min_model = ""  # defined on every path: the window math below reads it
    # AND the fleet-level kill switch (audit r5 #10 residual): the panel's
    # 'enabled' toggle is a runtime pause, and the request path now honours it
    # (core.model_routing.decide gates on global_enabled()). Advertising the
    # pseudo-models while the switch is OFF offered names that then behave as
    # plain aliases — a listing that contradicts the behaviour.
    if settings.routing_enabled and policy in ("shadow", "active") and await model_routing.global_enabled():
        # MODEL GROUPS (2026-11-08): a group is a routing candidate like any
        # chat model, so it is advertised as "routing__<group>" too. Loaded via
        # the same TTL-cached helper the group listing above uses.
        _routing_groups = await _groups_load()
        # AGENTS (2026-11-08): per-key hard models, advertised as
        # "routing__<agent>" only when the flag is on and the key is offered it.
        _routing_agent_names: list[str] = []
        if settings.routing_agents_enabled:
            try:
                _routing_agent_names = [a.name for a in await agents_mod.for_key(key.id) if a.offered]
            except Exception:
                _routing_agent_names = []  # fail-open: no agent entries
        chat_registered = (
            [m for m in registered if (p := model_router.pool(m)) is not None and p.endpoints and pool_serves_chat(p)]
            + [g.name for g in _routing_groups.values() if getattr(g, "model_type", "chat") == "chat"]
            + _routing_agent_names
        )
        # WINDOWED CAPACITY (2026-11-03): a routing pseudo-model is usable only
        # while SOME rung of its floor..ceiling window has budget headroom, and
        # "about to run out" is the tightest rung's band. The window is built
        # the SAME way the request path builds its candidate ladder — the
        # routing panel's allowlist BOUNDS the ladder and adds ticked off-ladder
        # models at the cheap end (expand_candidates), and bare "routing"
        # demotes an unticked ladder head to the most capable ticked model (R2)
        # — so the listing cannot promise a rung the router will never pick, nor
        # hide one it could.
        try:
            from ..core import routing_config as _rc_list

            _cfg = await _rc_list.get_config()
            _allow = _cfg.get("allowed_models")
            _routable = [str(m) for m in _allow] if isinstance(_allow, list) and _allow else None
        except Exception:
            _routable = None  # fail-open: the plain profile ladder, as before
        cheap_first = model_routing.expand_candidates(list(reversed(order)), _routable, None)
        for rid in ("routing", *(f"routing__{m}" for m in chat_registered)):
            # Bare "routing" ceilings at the head of the profile ordering; a
            # "routing__X" at X. An EMPTY ordering (no ladder to walk) leaves
            # the bare entry without a window: it 404s at request time anyway,
            # and inventing a window would advertise a ladder that does not
            # exist.
            if rid == "routing":
                ceiling = order[0] if order else ""
                if ceiling and _routable and ceiling not in {str(m) for m in _routable}:
                    for cand in order:
                        if cand in {str(m) for m in _routable}:
                            ceiling = cand
                            break
            else:
                ceiling = rid.split("__", 1)[1]
            window = (
                [
                    m
                    for m in model_routing._window(cheap_first, ceiling, _min_model)
                    if m in registered_set or m in _routing_groups or m in _routing_agent_names
                ]
                if ceiling
                else []
            )
            entry: dict[str, Any] = {
                "id": rid,
                "object": "model",
                "created": int(time.time()),
                "owned_by": "pcai-llm-gateway",
                # Sized by the whole chat ladder, not by the ceiling: a
                # routing request may be downgraded to ANY of them.
                **_advertised_caps(chat_registered),
                "pcai": {"type": "routing"},
            }
            if capacity and window:
                blocked = [m for m in window if capacity.get(m, {}).get("ok") is False]
                cap: dict[str, Any] = {
                    # ok = at least one rung can serve: the router walks the
                    # window, so one healthy rung is a usable selection.
                    "ok": len(blocked) < len(window),
                    # The band is the TIGHTEST rung's: a rung about to run out
                    # is exactly the ladder losing a step, which is what the
                    # "about to run out" signal must say.
                    "warn": max((capacity.get(m, {}).get("warn", 0) for m in window), default=0),
                    "candidates": {"ok": len(window) - len(blocked), "blocked": len(blocked)},
                }
                if blocked and len(blocked) == len(window):
                    cap["blocked_by"] = "every model in this routing window is out of budget"
                entry["pcai"]["capacity"] = cap
            data.append(entry)
    # --- AGENT PROFILES (2026-10-29) ---------------------------------------
    # An agent is selected by NAME, so it is advertised as a model entry: a
    # client that pins provider+model (DSH's teammate spawn does exactly that)
    # can name an agent with no other configuration. Filtered by the SAME
    # function the request path uses (core.agents.for_key), so the listing
    # cannot offer an agent the request would then 403.
    try:
        _agents = await agents_mod.for_key(key.id)
    except agents_mod.AgentRegistryUnavailable:
        # FAIL-SOFT HERE, unlike the request path: a listing that cannot read
        # the registry shows no agents (the truth it can prove) instead of
        # 400ing a call that needs no agent at all.
        _agents = []
    for _a in _agents:
        if not _a.offered:
            continue
        # An agent PINNED to a model this key may not call would 403 at request
        # time — never advertise it. Same for a pinned model that cannot serve
        # CHAT (the admin API refuses that at save, but the registry can move
        # under a stored row, and a listed entry that always 400s is a lie).
        # Unpinned agents ride the profile cascade, which the ACL already
        # governs; a pinned-but-unregistered name falls through to the cascade
        # by design (core.cascade.classify_request), so it stays listed.
        if _a.model and ((allowed is not None and _a.model not in allowed) or not serves_chat(_a.model)):
            continue
        data.append(
            {
                "id": _a.name,
                "object": "model",
                "created": int(time.time()),
                "owned_by": "pcai-llm-gateway",
                "pcai": {
                    "type": "agent",
                    "description": _a.description,
                    "servers": list(_a.servers),
                    "model": _a.model or "auto",
                },
            }
        )
    return {"object": "list", "data": data}


@router.get("/usage")
async def own_usage(
    key: KeyRecord = fastapi.Depends(require_scope("proxy")),
) -> dict[str, Any]:
    """Self-service usage view for the CALLING key.

    Read-only, ledger-derived: budgets (limit vs spent per period), the
    current-minute rate window, and recent request outcomes. This is the
    "why am I 429ing" surface — a key owner can see their burn before they
    hit a wall, without admin involvement.
    """
    budgets = await budget_status(key.id)
    now = datetime.now(UTC)
    window_start = now.replace(second=0, microsecond=0)
    row = await db.fetchrow(
        "SELECT requests, tokens FROM rate_windows WHERE key_id = $1 AND window_start = $2",
        key.id,
        window_start,
    )
    recent = await db.fetch(
        """
        SELECT ts, model, endpoint, prompt_tokens, completion_tokens,
               total_tokens, status, latency_ms
        FROM usage_ledger WHERE key_id = $1
        ORDER BY ts DESC LIMIT 20
        """,
        key.id,
    )
    return {
        "key": {"id": str(key.id), "name": key.name, "prefix": key.key_prefix},
        "limits": {"rpm": key.rate_limit_rpm, "tpm": key.rate_limit_tpm},
        "this_minute": dict(row) if row else {"requests": 0, "tokens": 0},
        "budgets": budgets,
        "recent": [dict(r) for r in recent],
    }
