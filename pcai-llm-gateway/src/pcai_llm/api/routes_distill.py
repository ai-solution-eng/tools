"""Session-distiller API: POST /v1/distill (+ per-conversation distill).

The harness-independent trigger surface. A harness (DSH, opencode, OWUI)
reduces to ONE call: hand the gateway a transcript (inline) or a stored
conversation id, and the gateway owns the rest - canonical prompt, model
call (budget-settled, traced), memory write through the enrolled dataset
pair.

ATTACHMENT GATE (the inert contract): the surface does nothing for a key
whose memory feature is not attached. Attached means BOTH:
  * the memory server (settings.enroll_memory_server, default mm-rag) is
    an ENABLED row in the MCP registry, and
  * the calling key carries the stamped {X-Memory-Dataset,
    X-Dataset-Password} pair in mcp_server_creds (enroll_memory put it
    there at redemption; admin-attached rows count identically).
An unattached key gets HTTP 200 {"ok": false, "inert": true, ...} - a
harmless no-op, so harness shims can call unconditionally with no gating
logic of their own (the flag-off 404 of routes_mcp_self extended to per-
key attachment). Unauthenticated callers still 401 at require_key - the
X-API-Key envelope (auth.py) is the credential, and nothing here runs
without one.

Error envelope: {"error": "<message>"} like the conversations surface.
The distill itself is BEST-EFFORT: on the conversation path a model failure
degrades to the raw transcript written as session_history (the DSH plugin
contract - a session is never lost to a model error); the inline path has no
stored fallback and reports ok=false instead; a memory-write failure is
reported in the body with ok=false, never a 5xx.

Flag: settings.distiller_enabled (chart settings.distillerEnabled). app.py
leaves the router UNMOUNTED when off - an off surface is indistinguishable
from one that was never deployed.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from typing import Any

import httpx2
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from ..config import settings
from ..core import conversations, distiller_config, enroll_memory, memory_write, traces
from ..core.budgets import check_budgets, settle
from ..core.cascade import resolve_cascade
from ..core.distiller import (
    MAX_HISTORY_CHARS,
    DistillBudgetExceeded,
    DistillTruncatedError,
    build_transcript,
    run_map_reduce,
)
from ..core.keys import KeyRecord
from ..db import db
from ..routing.router import router as model_router
from .auth import require_key

log = logging.getLogger(__name__)

router = APIRouter(tags=["distill"])

# Shortest transcript worth distilling (the DSH plugin floor).
#
# MEASURED ON THE BODY, NOT THE RENDER (2026-10-20). This floor exists to
# catch a transcript that rendered to nothing (an extraction-shape gap), and
# for years it measured len(transcript.strip()) - which is the WRONG number,
# because build_transcript prepends a `# Session History - <title>` header.
# A conversation with zero extractable MESSAGE text still renders to 28-48
# chars, sailed past a 20-char floor, and reached the distill model as a
# "transcript" containing nothing but a title. The model correctly answered
# with that title, emitted no [MEMORY INDEX] header (nothing to summarize),
# and synthesize_memory_index prepended the generic one - writing the
# title-only stub memories found in the live store ("Greeting", "Updating
# dsh package version"). Count body text via transcript_body_chars so the
# header can never satisfy the floor.
MIN_TRANSCRIPT_CHARS = 20

# The header build_transcript prepends when a title is known. Stripped by
# transcript_body_chars; kept here so the two stay greppable together.
_TRANSCRIPT_HEADER_PREFIX = "# Session History"

MEMORY_DATASET_HEADER = "X-Memory-Dataset"


def transcript_body_chars(transcript: str) -> int:
    """Length of the transcript BODY: everything but the session header.

    The collapse guard's input (see MIN_TRANSCRIPT_CHARS). A title-only
    render is a header and nothing else, so this returns 0 and the gate
    fires; a real conversation returns its message text length.

    Never raises: a malformed or unexpected render degrades to the raw
    length, so a guard fault can never abort a sweep.
    """
    if not isinstance(transcript, str):
        return 0
    try:
        lines = transcript.splitlines()
        # Drop the leading header line (and any blank run right after it)
        # only when the render actually starts with it - an inline
        # transcript (the explicit /distill surface) has no header.
        start = 0
        if lines and lines[0].lstrip().startswith(_TRANSCRIPT_HEADER_PREFIX):
            start = 1
            while start < len(lines) and not lines[start].strip():
                start += 1
        return len("\n".join(lines[start:]).strip())
    except Exception:  # pragma: no cover - defensive: never break a sweep
        return len(transcript.strip())


async def _gate() -> None:
    """Gate: 404 when the env flag is off (unmounted surface). The admin
    runtime toggle (distiller_config.enabled) additionally pauses the
    surface with 503 — the panel's "Runtime enabled" semantics hold for
    explicit calls too, not just the sweep (DeepSeek-V4.1 review)."""
    if not settings.distiller_enabled:
        raise HTTPException(404, "not found")
    if not await distiller_config.is_enabled():
        raise HTTPException(503, "distiller paused by admin")


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": message})


def _latency_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


async def _json_object(request: Request) -> dict[str, Any] | JSONResponse:
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


_PROBE_TTL_S = 60.0
_PROBE_TIMEOUT_S = 10.0  # bounded like the MCP metadata calls: a hung memory server must not park the gate
_probe_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_PROBE_CACHE_MAX = 512  # monotonic-growth bound (DeepSeek-V4.1 P2): prune-to-TTL on write beyond this


async def memory_status_for_key(key: KeyRecord, request_id: str) -> dict[str, Any]:
    """The memory-write capability probe for ONE key: call the memory
    server's probe tool (settings.distiller_memory_probe_tool, default
    mm-rag memory_status) THROUGH the relay path with the KEY'S OWN
    credentials (resolve_mcp_headers merge - the key's X-API-Key for the
    memory backend travels exactly as it would on a user-initiated call).

    The BACKEND answers about its own caller (identity resolved from the
    presented key - e.g. mm-rag's D15 registry + the /access-page binding);
    the gateway never guesses datasets. Shape:
      {memory_available: bool, dataset, writable, unlocked, detail}
    unlocked may be None (unknown) - the writer attempts the real write,
    which re-checks everything.

    TTL-cached per key (the scheduled sweep would otherwise probe 20 rows
    per pass). Errors read as NOT available (fail-safe inert), never raise.
    """
    cache_key = str(key.id)
    now = time.monotonic()
    hit = _probe_cache.get(cache_key)
    if hit and now - hit[0] < _PROBE_TTL_S:
        return hit[1]

    out: dict[str, Any] = {
        "memory_available": False,
        "dataset": None,
        "writable": False,
        "unlocked": False,
        "detail": "",
    }
    try:
        target = await enroll_memory.resolve_memory_server(settings.distiller_memory_server)
    except Exception:
        target = None
    if target is None:
        out["detail"] = "memory server " + repr(settings.distiller_memory_server) + " is not in the enabled registry"
        _probe_cache[cache_key] = (now, out)
        return out

    async def do(session: Any) -> Any:
        return await session.call_tool(settings.distiller_memory_probe_tool, {})

    try:
        from .routes_mcp import _upstream_session, resolve_mcp_headers

        headers = await resolve_mcp_headers(target, key_id=key.id)
        # read-shaped call: a dedicated short bound, not the write timeout —
        # a slow memory server must not park the gate (DeepSeek-V4.1 P2)
        result = await _upstream_session(str(target["url"]), headers, do, timeout_s=_PROBE_TIMEOUT_S)
        text = ""
        for block in result.content or []:
            t = getattr(block, "text", None)
            if isinstance(t, str) and t:
                text = t
                break
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            out.update({k: parsed.get(k, out[k]) for k in out})
    except Exception as exc:
        out["detail"] = "memory probe failed: " + type(exc).__name__
    _probe_cache[cache_key] = (
        time.monotonic(),
        out,
    )  # birth = store time (DeepSeek-V4.1 P2: a pre-probe timestamp made a slow probe born stale)
    if len(_probe_cache) > _PROBE_CACHE_MAX:
        # prune expired entries (keys that stop being swept otherwise live
        # for the process lifetime — a slow leak)
        cutoff = time.monotonic() - _PROBE_TTL_S
        for k in [k for k, (ts, _) in _probe_cache.items() if ts < cutoff]:
            _probe_cache.pop(k, None)
    return out


async def memory_attached(key: KeyRecord, request_id: str) -> bool:
    """The attachment gate, CAPABILITY-based: the backend says whether THIS
    key can write memories (identity resolved from the key's own credential
    merge). Replaces the pair-stamp heuristic - the gateway does not know
    the backend's dataset model, and must not. attached = available AND
    writable (unlocked unknown counts as attached: the write re-checks)."""
    st = await memory_status_for_key(key, request_id)
    return bool(st.get("memory_available")) and bool(st.get("writable"))


def _inert() -> JSONResponse:
    """The inert no-op body: 200 so fire-and-forget harness shims stay
    error-free, with inert=true so a caller that WANTS to know can tell
    the no-op from a write."""
    return JSONResponse(
        status_code=200,
        content={
            "ok": False,
            "inert": True,
        },
    )


class DistillModelFailure(Exception):
    """A model call failed after gates passed - degrades to fallback."""


class DistillUnavailable(Exception):
    """No usable model route or memory server - a 503 (nothing tried)."""


def _llm_once_for(key: KeyRecord, request_id: str):
    """Bind the distiller llm_once to the gateway model routing.

    Model selection: settings.distiller_model (a fleet-designated
    summarizer - operator-chosen, settled against the calling key for
    auditability) when set; otherwise the key OWN routing (resolve_cascade
    on auto - profile ordering, budget gates, the tenant-visible path).
    Every call settles ONE ledger row (the distill is real spend) and one
    trace row (phase=distill, best-effort).
    """

    async def llm_once(system: str, user_text: str, max_tokens: int) -> str:
        est_tokens = (len(system) + len(user_text)) // 4
        started = time.monotonic()
        # DB override first (admin panel), env default second (cross-review
        # finding: explicit paths must honor the same knobs the sweep does)
        model = await distiller_config.get_model()
        if model:
            registered = set(model_router.models())
            if model not in registered:
                raise DistillUnavailable("distiller model " + repr(model) + " is not registered")
            pool = model_router.pool(model)
            if pool is None or not pool.healthy():
                raise DistillUnavailable("distiller model " + repr(model) + " has no healthy endpoint")
            gate = await check_budgets(key.id, model, est_tokens)
            if not gate.ok:
                raise DistillUnavailable("distiller model budget exhausted: " + str(getattr(gate, "reason", "")))
        else:
            resolution = await resolve_cascade(
                key.id,
                "auto",
                set(model_router.models()),
                key.allowed_models,
                est_tokens,
            )
            if not resolution.ok or resolution.model is None:
                raise DistillUnavailable("no model route for the calling key: " + str(resolution.reason))
            model = resolution.model
            pool = model_router.pool(model)
            if pool is None or not pool.healthy():
                raise DistillUnavailable("resolved model " + repr(model) + " has no healthy endpoint")
        ep = pool.pick()
        if ep is None:
            raise DistillUnavailable("no endpoint picked for " + repr(model))

        from .routes_proxy import _upstream_headers

        url = ep.base_url + "/chat/completions"
        headers = _upstream_headers(ep, request_id)
        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user_text},
            ],
            "max_tokens": max_tokens,
            "stream": False,
        }
        # Skip the reasoning trace at the SOURCE (see the setting's docstring).
        # Without this a reasoning model spends the whole max_tokens on
        # reasoning_content, returns content="" with a 200, and every distill
        # fails while the upstream looks healthy.
        #
        # REJECTION FALLBACK: chat_template_kwargs is honored by vLLM and
        # SGLang (the documented Qwen3 switch), but it is a chat-TEMPLATE
        # kwarg, not an OpenAI sampling field — an engine or proxy that
        # validates its body strictly may answer 400. That 400 would both fail
        # the distill AND trip the endpoint's breaker (report() below demotes on
        # any non-2xx), so a single unsupported field could take a healthy
        # endpoint out of the pool. Retry ONCE without the kwarg: a distill
        # that works with a thinking trace beats an endpoint marked dead.
        attempts: list[dict[str, Any]] = []
        if settings.distiller_disable_thinking:
            attempts.append({"chat_template_kwargs": {"enable_thinking": False}})
        attempts.append({})
        resp = None
        for extra in attempts:
            try:
                resp = await model_router.client.post(
                    url,
                    json={**body, **extra},
                    headers=headers,
                    timeout=settings.distiller_llm_timeout_s,
                )
            except httpx2.HTTPError:
                await pool.report(ep, False, error="HTTPError")
                await settle(key.id, model, "chat", 0, 0, "error", _latency_ms(started), request_id)
                raise DistillModelFailure("upstream request failed") from None
            if not (400 <= resp.status_code < 500) or not extra:
                break
            # ONLY a 4xx retries: that is the engine rejecting the FIELD. A
            # 5xx (or a 2xx) falls through — retrying an upstream fault would
            # double the load on a struggling endpoint for no benefit.
            log.info(
                "distill: upstream rejected chat_template_kwargs (%s) - retrying without thinking suppression",
                resp.status_code,
            )
        assert resp is not None  # attempts is never empty
        latency = _latency_ms(started)
        # 4xx is a breaker failure too (DeepSeek-V4.1 review; the proxy own
        # non-stream path reports False on ANY non-2xx - a mis-keyed endpoint
        # must demote, not be hammered forever)
        await pool.report(
            ep,
            200 <= resp.status_code < 300,
            error=str(resp.status_code) if resp.status_code >= 400 else "",
        )
        if resp.status_code != 200:
            await settle(key.id, model, "chat", 0, 0, "error", latency, request_id)
            raise DistillModelFailure("upstream status " + str(resp.status_code))
        try:
            payload = resp.json()
            message = payload["choices"][0]["message"]
            text = message["content"]
        except Exception:
            await settle(key.id, model, "chat", 0, 0, "error", latency, request_id)
            raise DistillModelFailure("upstream returned an unreadable body") from None
        if not isinstance(text, str) or not text.strip():
            await settle(key.id, model, "chat", 0, 0, "error", latency, request_id)
            # NAME the cause instead of the generic "no text" (2026-10-20):
            # an empty content alongside a populated reasoning_content is a
            # THINKING-TRACE problem, not a broken model - the reader of this
            # log line needs to know which knob to turn.
            reasoning = message.get("reasoning_content") or message.get("reasoning")
            if isinstance(reasoning, str) and reasoning.strip():
                # Do NOT assert "spent the whole budget" on the presence of a
                # trace alone: a SHORT trace with finish_reason=stop (a
                # refusal, a degenerate answer) has the same shape. Only claim
                # the burn when the finish reason or the completion count
                # actually says the cap was hit; otherwise report the
                # observation and let the operator judge.
                finish = str(payload["choices"][0].get("finish_reason") or "")
                used = int((payload.get("usage") or {}).get("completion_tokens") or 0)
                if finish == "length" or (used and used >= max_tokens):
                    raise DistillModelFailure(
                        "upstream spent the whole max_tokens budget on the reasoning trace "
                        f"({len(reasoning)} chars of reasoning_content, content empty, "
                        f"finish_reason={finish or '?'}) - raise distiller_max_tokens "
                        "or keep distiller_disable_thinking on"
                    )
                raise DistillModelFailure(
                    "upstream returned empty content with a populated reasoning_content "
                    f"({len(reasoning)} chars, finish_reason={finish or '?'}, "
                    f"completion_tokens={used}) - the answer never left the thinking "
                    "trace; keep distiller_disable_thinking on or check the model template"
                )
            raise DistillModelFailure("upstream produced no text")
        usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
        prompt_tokens = int(usage.get("prompt_tokens") or 0)
        completion_tokens = int(usage.get("completion_tokens") or 0)
        details = usage.get("prompt_tokens_details")
        cached = 0
        if isinstance(details, dict):
            try:
                cached = int(details.get("cached_tokens") or 0)
            except (TypeError, ValueError):
                cached = 0
        await settle(
            key.id,
            model,
            "chat",
            prompt_tokens,
            completion_tokens,
            "ok",
            latency,
            request_id,
            cached_tokens=cached,
        )
        # TRUNCATION GUARD (2026-10-23): a NON-empty reply that hit the
        # max_tokens cap was treated as a successful distill -- two live
        # memories shipped cut mid-document (one ends at a Pytest line, one
        # mid-bullet). The empty-content branch above already names this
        # cause for the reasoning-trace shape; this guard covers the
        # content-present shape it misses. Map-stage calls (partial notes
        # are legitimate intermediates) and callers that opted out via
        # settings.distiller_reject_truncated are exempt.
        finish = str(payload["choices"][0].get("finish_reason") or "")
        used = int((payload.get("usage") or {}).get("completion_tokens") or 0)
        map_stage = bool(getattr(llm_once, "_distill_map_stage", False))
        if (
            settings.distiller_reject_truncated
            and not map_stage
            and (finish == "length" or (used and max_tokens and used >= max_tokens))
        ):
            raise DistillTruncatedError(
                "distill reply hit the max_tokens cap mid-document "
                f"(finish_reason={finish or '?'}, completion_tokens={used}, "
                f"max_tokens={max_tokens}) - document rejected before write; "
                "raise distiller_max_tokens or retry"
            )
        call_seq = getattr(llm_once, "_call_seq", None)
        if call_seq is None:
            call_seq = {"n": 0}
            llm_once._call_seq = call_seq  # type: ignore[attr-defined]
        call_seq["n"] += 1
        await traces.record(
            request_id,
            key.id,
            key.user_id,
            model,
            call_seq["n"],
            "distill",
            ms=latency,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            status="ok",
        )
        return text

    return llm_once


async def _mcp_call_for(key: KeyRecord):
    """Bind memory_write.mcp_call to the registered memory server.

    Headers: routes_mcp.resolve_mcp_headers - server-level auth first, the
    key stamped {X-Memory-Dataset, X-Dataset-Password} pair LAST-WINS (the
    exact merge the relay uses; no second credential path). The URL is the
    registry row (enroll_memory.resolve_memory_server) - never an env.
    """
    target = await enroll_memory.resolve_memory_server(settings.enroll_memory_server)
    if target is None:
        raise DistillUnavailable(
            "memory server " + repr(settings.enroll_memory_server) + " is not in the enabled registry"
        )
    from .routes_mcp import _upstream_session, resolve_mcp_headers

    async def mcp_call(arguments: dict[str, Any]) -> tuple[bool, str]:
        headers = await resolve_mcp_headers(target, key_id=key.id)

        async def do(session: Any) -> Any:
            # the tool name is operator-configurable (config.py distiller_memory_tool)
            return await session.call_tool(settings.distiller_memory_tool, arguments)

        result = await _upstream_session(str(target["url"]), headers, do, timeout_s=memory_write.WRITE_TIMEOUT_S)
        text = ""
        for block in result.content or []:
            t = getattr(block, "text", None)
            if isinstance(t, str) and t:
                text = t
                break
        if result.is_error:
            return False, text or (settings.distiller_memory_tool + " refused")
        return True, text or "ok"

    return mcp_call


async def _mcp_search_for(key: KeyRecord):
    """Bind a search_memory probe to the registered memory server.

    Same credential/URL discipline as _mcp_call_for (the registry row +
    resolve_mcp_headers merge; no second credential path). The tool is
    settings.distiller_memory_search_tool (operator-configurable, mirroring
    distiller_memory_tool). Distinct helper, not a parameter of mcp_call:
    the add tool and the search tool have different argument shapes, and the
    dedup probe must never be able to write.
    """
    target = await enroll_memory.resolve_memory_server(settings.enroll_memory_server)
    if target is None:
        return None  # dedup is best-effort: no server -> no probe -> write anyway
    from .routes_mcp import _upstream_session, resolve_mcp_headers

    async def mcp_search(arguments: dict[str, Any]) -> tuple[bool, str]:
        headers = await resolve_mcp_headers(target, key_id=key.id)

        async def do(session: Any) -> Any:
            return await session.call_tool(settings.distiller_memory_search_tool, arguments)

        result = await _upstream_session(
            str(target["url"]), headers, do, timeout_s=memory_write.WRITE_TIMEOUT_S
        )
        text = ""
        for block in result.content or []:
            t = getattr(block, "text", None)
            if isinstance(t, str) and t:
                text = t
                break
        if result.is_error:
            return False, text or (settings.distiller_memory_search_tool + " refused")
        return True, text

    return mcp_search


def _conversation_id(raw: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(raw)
    except ValueError:
        return None


def _raw_fallback_enabled() -> bool:
    """Whether a FAILED distill may write the raw transcript (session_history).

    The historical DSH contract answers True: a session is never lost to a model
    error. The cost is that mm-rag CHUNKS the raw transcript, so ONE failed
    distill can land 100+ transcript fragments in the memory store and dilute
    recall (measured 2026-10-10: 126 chunks from a single session).

    With settings.distiller_raw_fallback False the caller leaves the row DUE
    instead (the conversation is still in canonical_conversations, so nothing is
    lost) and the store only ever receives distilled summaries. This governs
    ONLY the "model reachable but failed / returned no text" case; a missing
    model route or memory server never wrote a fallback either way."""
    return bool(settings.distiller_raw_fallback)


@router.post("/v1/distill")
async def distill_inline(
    request: Request,
    key: KeyRecord = Depends(require_key),
) -> Any:
    """Distill an inline transcript ({session_id?, transcript, max_tokens?}).

    The thin-shim contract for harnesses that do NOT push conversations:
    one POST with the transcript text, everything else gateway-side.
    Inert no-op when the memory feature is not attached to the key.
    """
    await _gate()
    if not await memory_attached(key, "probe-" + uuid.uuid4().hex[:12]):
        return _inert()
    body = await _json_object(request)
    if isinstance(body, JSONResponse):
        return body
    transcript = body.get("transcript")
    # MEASURED ON THE BODY (2026-10-20). This surface takes a CALLER-supplied
    # transcript, and harnesses render theirs with the same
    # `# Session History - <title>` opening line build_transcript uses. A
    # header + title is ~48 raw chars, so a raw-length gate would admit a
    # transcript with no message text at all and reproduce the title stub
    # here. transcript_body_chars strips that header ONLY when line 0 is
    # actually it, so genuine caller content is never eaten.
    if not isinstance(transcript, str) or transcript_body_chars(transcript) < MIN_TRANSCRIPT_CHARS:
        return _error(400, "transcript is required (at least " + str(MIN_TRANSCRIPT_CHARS) + " characters)")
    # The cap is core.distiller's own transcript bound, NOT a second literal
    # (audit D-12/F10): the two numbers were the same value written twice, so
    # a change to the distiller's bound would have left this gate disagreeing
    # with the renderer's truncation point.
    if len(transcript) > MAX_HISTORY_CHARS:
        return _error(413, f"transcript exceeds the {MAX_HISTORY_CHARS:,} character cap")
    session_id = body.get("session_id")
    if session_id is not None and not isinstance(session_id, str):
        return _error(400, "session_id must be a string")
    session_id = session_id or ("req-" + uuid.uuid4().hex[:12])
    max_tokens = body.get("max_tokens")
    if max_tokens is not None and (
        not isinstance(max_tokens, int)
        or isinstance(max_tokens, bool)
        or max_tokens <= 0
        or max_tokens > 32768  # same ceiling the admin config enforces
    ):
        return _error(400, "max_tokens must be a positive integer up to 32768")
    request_id = "distill-" + uuid.uuid4().hex[:12]
    llm_once = _llm_once_for(key, request_id)
    distilled = True
    try:
        doc = await run_map_reduce(
            transcript, llm_once, max_distill_tokens=max_tokens or await distiller_config.max_tokens()
        )
    except DistillUnavailable as exc:
        return _error(503, str(exc))
    except Exception:
        # model failure. Default (raw fallback ON): the caller HANDED us the
        # transcript, so preserve it as session_history (the DSH contract - a
        # session is never lost to a model error). With the fallback OFF we
        # write NOTHING and report the failure: a raw transcript is chunked by
        # mm-rag into many fragments, so degrading here would pollute the
        # memory store with transcript noise.
        if not _raw_fallback_enabled():
            log.warning(
                "inline distill failed (key=%s); raw fallback disabled - nothing written",
                key.id,
                exc_info=True,
            )
            return _error(503, "distillation failed and the raw-transcript fallback is disabled")
        log.warning("inline distill failed (key=%s); degrading to raw transcript", key.id, exc_info=True)
        doc = transcript
        distilled = False
    try:
        mcp_call = await _mcp_call_for(key)
    except DistillUnavailable as exc:
        return _error(503, str(exc))
    ok, detail = await memory_write.write_memory(
        key.id,
        mcp_call,
        doc,
        session_id=session_id,
        kind=memory_write.KIND_SUMMARY if distilled else memory_write.KIND_FALLBACK,
        fallback_doc=transcript if distilled else "",
        fallback_session_id=session_id,
        mcp_search=await _mcp_search_for(key),
    )
    return {
        "ok": ok,
        "inert": False,
        "kind": memory_write.KIND_SUMMARY if distilled else memory_write.KIND_FALLBACK,
        "detail": detail,
        "session_id": session_id,
    }


@router.post("/v1/distill/conversations/{conversation_id}")
async def distill_conversation(
    conversation_id: str,
    key: KeyRecord = Depends(require_key),
) -> Any:
    """Distill a STORED conversation (the zero-payload shim for harnesses
    that already push transcripts via /v1/conversations).

    Model failure degrades to the raw transcript written as session_history;
    the degrade lives in memory_write.write_memory (summary first, fallback
    once). Inert no-op when the memory feature is not attached.
    """
    await _gate()
    if not await memory_attached(key, "probe-" + uuid.uuid4().hex[:12]):
        return _inert()
    cid = _conversation_id(conversation_id)
    if cid is None:
        return _error(404, "not found")
    conv = await conversations.get(key.id, cid)
    if conv is None:
        return _error(404, "not found")
    transcript = build_transcript(conv.get("messages") or [], str(conv.get("title") or ""))
    if transcript_body_chars(transcript) < MIN_TRANSCRIPT_CHARS:
        return _error(400, "conversation is too short to distill")
    request_id = "distill-" + uuid.uuid4().hex[:12]
    llm_once = _llm_once_for(key, request_id)
    distilled = True
    try:
        doc = await run_map_reduce(transcript, llm_once, max_distill_tokens=await distiller_config.max_tokens())
    except DistillUnavailable as exc:
        return _error(503, str(exc))
    except Exception:
        if not _raw_fallback_enabled():
            log.warning(
                "conversation distill failed (key=%s conv=%s); raw fallback disabled - nothing written",
                key.id,
                cid,
                exc_info=True,
            )
            return _error(503, "distillation failed and the raw-transcript fallback is disabled")
        log.warning(
            "conversation distill failed (key=%s conv=%s); degrading to raw transcript",
            key.id,
            cid,
            exc_info=True,
        )
        doc = transcript
        distilled = False
    try:
        mcp_call = await _mcp_call_for(key)
    except DistillUnavailable as exc:
        return _error(503, str(exc))
    verdict: list[str] = []
    ok, detail = await memory_write.write_memory(
        key.id,
        mcp_call,
        doc,
        session_id=str(cid),
        # the kind is HONEST: when the distill itself failed, the raw
        # transcript is written AS session_history (the DSH plugin contract -
        # tagged so the periodic rebuild can distill it later), never as a
        # mislabeled project-summary
        kind=memory_write.KIND_SUMMARY if distilled else memory_write.KIND_FALLBACK,
        # and when the summary write fails (RAG refused, transport), the
        # raw transcript still lands: write_memory retries once as
        # session_history
        fallback_doc=transcript if distilled else "",
        fallback_session_id=str(cid),
        mcp_search=await _mcp_search_for(key),
        verdict_out=verdict,
    )
    kind = memory_write.KIND_SUMMARY if distilled else memory_write.KIND_FALLBACK
    # seed the marker on success: an explicit distill claims the conversation
    # so the scheduled sweep does not re-distill it (duplicate spend). Marker
    # seq = transcript length at distill time (current message count).
    #
    # A SUPPRESSED write claims it too (audit D-2): the near-duplicate verdict
    # is permanent, so leaving the row unclaimed would make the SWEEP re-run
    # the same map-reduce that was just suppressed.
    if ok or (verdict and verdict[-1] == memory_write.VERDICT_SUPPRESSED):
        from ..core import distill_schedule

        await distill_schedule.mark_distilled(cid, key.id, len(conv.get("messages") or []))
    return {"ok": ok, "inert": False, "kind": kind, "detail": detail, "conversation_id": str(cid)}


# db is imported for the attachment-gate SQL path (key_override_headers
# uses its own module-level db); keep the reference so linters do not flag
# the module-level import while tests monkeypatch core.db.
_ = db


async def distill_due_conversation(row: dict[str, Any], *, deadline: float | None = None) -> bool:
    """The scheduled (housekeeping) distill for one due conversation row
    (core.distill_schedule.sweep). The SAME engine as the explicit
    conversation path - canonical prompt, attachment gate, degrade - plus
    the marker write so the sweep does not re-distill until it continues.
    offpeak_only gates the CALLER (housekeeping), not this engine: an
    operator-configured quiet-hours policy is enforced in one place.

    deadline (monotonic, optional): the sweep's per-pass wall-clock budget,
    re-checked before every model call (audit D-6) so one long row cannot
    hold the shared housekeeping loop for its full map-reduce.

    Returns True ONLY when a memory write landed (audit D-4). Every skip path
    returns False, so the sweep can keep writes_total{ok} and the last-write
    gauge honest."""
    from ..core import distill_schedule, distiller_config

    cid = row["conversation_id"]
    key_id = row["key_id"]
    cid = cid if isinstance(cid, uuid.UUID) else uuid.UUID(str(cid))
    key_id = key_id if isinstance(key_id, uuid.UUID) else uuid.UUID(str(key_id))

    # The FULL attachment gate (capability-based): due_conversations' SQL
    # join proves only that A creds row exists for the server name — the
    # PROBE answers whether the key can actually write (backend-resolved
    # identity). An unattached key must never spend model tokens; the gate
    # runs before any spend, same as the explicit paths.
    from ..core.keys import load_key_record

    key = await load_key_record(key_id)
    if key is None:
        await distill_schedule.mark_distilled(cid, key_id, 0)  # gone: stop retrying
        log.info(
            "scheduled distill conv=%s: key %s gone since selection - marked and skipping",
            cid,
            key_id,
        )
        return False
    if not await memory_attached(key, "probe-sched-" + uuid.uuid4().hex[:12]):
        # Same gate-backoff contract as the canonical path: no write landed,
        # so no distilled marker -- but the row must leave the oldest-first
        # window or a broken gate pins the sweep forever.
        await distill_schedule.mark_gate_skip(cid, key_id)
        if row.get("gate_failed_at") is None:
            log.warning(
                "scheduled distill conv=%s: key %s NOT memory-attached - deferring %ss "
                "(check the memory server registry row + probe tool)",
                cid,
                key_id,
                int(distill_schedule.gate_backoff_seconds()),
            )
        return False
    conv = await conversations.get(key_id, cid)
    if conv is None:
        # gone/purged: the FK on conversation_distills (REFERENCES conversations
        # ON DELETE CASCADE) REJECTS a marker INSERT for a nonexistent row - the
        # old mark_distilled(0) here was swallowed as a warning and the sweep
        # re-selected the same row every pass (head-of-line starvation, audit
        # P1-1). Drop any marker row explicitly and warn once.
        await distill_schedule.drop_marker(cid)
        log.warning("scheduled distill conv=%s: conversation gone/purged - marker row dropped (stop retrying)", cid)
        return False
    transcript = build_transcript(conv.get("messages") or [], str(conv.get("title") or ""))
    if transcript_body_chars(transcript) < MIN_TRANSCRIPT_CHARS:
        # Same contract as the canonical path: an empty transcript is an
        # extraction gap, not a distillation — defer, never mark. Measured on
        # the BODY: the `# Session History - <title>` header must never
        # satisfy the floor (2026-10-20 title-stub leak).
        await distill_schedule.mark_gate_skip(cid, key_id)
        if row.get("gate_failed_at") is None:
            log.warning(
                "scheduled distill conv=%s: transcript collapsed to %d body chars - deferring %ss",
                cid,
                transcript_body_chars(transcript),
                int(distill_schedule.gate_backoff_seconds()),
            )
        return False
    request_id = "distill-sched-" + uuid.uuid4().hex[:12]
    llm_once = _llm_once_for(key, request_id)
    try:
        doc = await run_map_reduce(
            transcript,
            llm_once,
            max_distill_tokens=await distiller_config.max_tokens(),
            deadline=deadline,
        )
        distilled = True
    except DistillBudgetExceeded:
        # The sweep's budget lapsed mid-row: no marker (nothing was written),
        # no backoff (the row is healthy, the PASS ran out of time) and no
        # raw-transcript degrade (this is not a model failure - writing a
        # partially-mapped transcript would be noise). The next pass resumes.
        log.info("scheduled distill conv=%s: sweep budget exhausted mid-row - deferred", cid)
        return False
    except DistillUnavailable:
        # nothing tried; back off like a gate skip (audit P2-1): without a
        # marker the sweep re-attempts the FULL map-reduce chain every pass
        # for a permanently exhausted key — the head-of-line starvation the
        # gate_failed_at mechanism exists to prevent. Safe since P0-1: the
        # skip marker keeps the row due once the backoff lapses.
        await distill_schedule.mark_gate_skip(cid, key_id)
        return False
    except Exception:
        if not _raw_fallback_enabled():
            # Leave the row DUE: the conversation is still in the store, so
            # nothing is lost and the next sweep retries. Writing the raw
            # transcript here would land 100+ mm-rag chunks of noise.
            log.warning(
                "scheduled distill failed (conv=%s); raw fallback disabled - deferring %ss",
                cid,
                int(distill_schedule.gate_backoff_seconds()),
                exc_info=True,
            )
            # GATE-SKIP BACKOFF (audit D-5): "left due" alone means the FULL
            # map-reduce is re-spent every pass (default: every 60 s) on the
            # window head, which is the head-of-line starvation the backoff
            # exists to prevent. Nothing was written, so this is NOT a
            # distilled marker -- the row returns to the due set when the
            # window lapses.
            await distill_schedule.mark_gate_skip(cid, key_id)
            return False
        log.warning("scheduled distill failed (conv=%s); degrading to raw transcript", cid, exc_info=True)
        doc = transcript
        distilled = False
    try:
        mcp_call = await _mcp_call_for(key)
    except DistillUnavailable:
        # memory server gone from the registry; back off (review FLAG-4):
        # the map-reduce already spent, and without a marker the sweep
        # re-spends it every pass until the server returns.
        await distill_schedule.mark_gate_skip(cid, key_id)
        return False
    ok, detail = await memory_write.write_memory(
        key_id,
        mcp_call,
        doc,
        session_id=str(cid),
        kind=memory_write.KIND_SUMMARY if distilled else memory_write.KIND_FALLBACK,
        fallback_doc=transcript if distilled else "",
        fallback_session_id=str(cid),
    )
    # the marker is written ONLY on a successful write: a failed write (both
    # summary and fallback refused) leaves the conversation due, so the next
    # sweep retries — a transient RAG outage must not permanently mark
    # sessions as distilled-with-nothing-written (cross-review finding).
    if ok:
        await distill_schedule.mark_distilled(cid, key_id, int(row.get("msg_count") or 0))
    log.info("scheduled distill conv=%s ok=%s detail=%s", cid, ok, detail[:80])
    return bool(ok)


# (the scheduled path loads the KeyRecord once for the attachment gate and
# reuses it for llm/mcp binding — no per-chunk reloads: the DeepSeek-V4.1
# N+1 finding resolved structurally)


async def distill_due_canonical(row: dict[str, Any], *, deadline: float | None = None) -> bool:
    """The scheduled distill for ONE relay-captured canonical row (the store
    every harness feeding the gateway converges into - relayed or not).
    Same engine contract as the conversations path: attachment gate before
    any spend, canonical prompt, session_history degrade, marker ONLY on a
    successful write (a transient RAG outage must not silently mark the
    session distilled).

    deadline (monotonic, optional): the sweep's per-pass wall-clock budget,
    re-checked before every model call (audit D-6).

    Returns True ONLY when a memory write landed (audit D-4); every skip path
    returns False."""
    from ..core import distill_schedule, distiller_config

    key_id = row["key_id"]
    thread_id = str(row["thread_id"])
    key_id = key_id if isinstance(key_id, uuid.UUID) else uuid.UUID(str(key_id))
    messages = row.get("messages") or []

    # ONE-SHOT NOISE FILTER: a real conversation has at least a user turn,
    # an assistant reply, and a follow-up. Single-exchange rows (curl tests,
    # one-shot SDK calls) are never distilled.
    if len(messages) < 4:
        await distill_schedule.mark_canonical_distilled(key_id, thread_id)
        return False

    # FULL attachment gate (capability probe) before any model spend - the
    # same gate every entry point runs.
    from ..core.keys import load_key_record

    key = await load_key_record(key_id)
    if key is None:
        # Revoked/deleted since selection. Mark the row (seq 0 = nothing
        # written) so it stops consuming the oldest-first window: without a
        # marker it is re-selected every pass forever.
        await distill_schedule.mark_canonical_distilled(key_id, thread_id)
        log.info(
            "canonical distill thread=%s: key %s gone since selection - marked and skipping",
            thread_id[:16],
            key_id,
        )
        return False
    if not await memory_attached(key, "probe-sched-" + uuid.uuid4().hex[:12]):
        # The gate itself is failing (e.g. an empty registry snapshot makes
        # resolve_memory_server() return None for EVERY key). No write landed,
        # so this is NOT a distilled marker -- but returning with no state at
        # all is what froze the queue for days: the row stays due and the
        # same oldest-first LIMIT window is retried forever. Suppress it for
        # the backoff window so newer rows get their turn; it returns to the
        # due set automatically once the gate recovers.
        await distill_schedule.mark_canonical_gate_skip(key_id, thread_id)
        if row.get("gate_failed_at") is None:
            # First skip of this row in the current streak: say so loudly.
            # Repeat skips are silent (one line per backoff window, not per
            # pass) so a persistent gate outage cannot flood the log.
            log.warning(
                "canonical distill thread=%s: key %s NOT memory-attached - deferring %ss "
                "(check the memory server registry row + probe tool)",
                thread_id[:16],
                key_id,
                int(distill_schedule.gate_backoff_seconds()),
            )
        return False

    transcript = build_transcript(messages)
    if transcript_body_chars(transcript) < MIN_TRANSCRIPT_CHARS:
        # 2026-10-09 data-loss finding: marking here PERMANENTLY discards the
        # row — a marker means "a write landed", and nothing was written. A
        # row that renders empty is an extraction-shape gap (observed: a
        # 202k-message relay row collapsing to the bare 19-char header), and
        # the extractor can be fixed later only if the row is still due. So
        # defer with the same bounded backoff as the gate skip: the row
        # leaves the oldest-first window, stays unmarked, and comes back.
        await distill_schedule.mark_canonical_gate_skip(key_id, thread_id)
        if row.get("gate_failed_at") is None:
            log.warning(
                "canonical distill thread=%s: transcript collapsed to %d body chars from %d messages "
                "- deferring %ss (extraction shape gap? NOT marking distilled)",
                thread_id[:16],
                transcript_body_chars(transcript),
                len(messages),
                int(distill_schedule.gate_backoff_seconds()),
            )
            # SHAPE DIAGNOSTIC (pulse-check 2026-10-11): if this fires again
            # after the _text_of hardening, the row holds a shape the extractor
            # still misses - say exactly what it is (roles + content types of
            # the first 3 messages, no content bytes).
            _shapes = [
                {
                    "role": (m.get("role") if isinstance(m, dict) else type(m).__name__),
                    "content_type": (type(m.get("content")).__name__ if isinstance(m, dict) else "-"),
                    "part_types": (
                        [b.get("type") for b in m.get("content", []) if isinstance(b, dict)][:4]
                        if isinstance(m, dict) and isinstance(m.get("content"), list)
                        else None
                    ),
                }
                for m in messages[:3]
            ]
            log.warning("canonical distill thread=%s: shape sample %s", thread_id[:16], _shapes)
        return False

    # UNCHANGED-CONTENT SKIP (2026-10-11 duplication fix, part 2): a continued
    # conversation re-becomes due on every turn; if the rendered transcript is
    # byte-identical to what the last distill consumed, there is nothing new
    # to learn - refresh the marker (row-state snapshot moves forward, the
    # row leaves the due window) and skip the 25-call map-reduce entirely.
    _content_hash = hashlib.sha256(transcript.encode()).hexdigest()
    if row.get("distilled_content_hash") == _content_hash:
        await distill_schedule.mark_canonical_distilled(key_id, thread_id, row.get("updated_at"), _content_hash)
        log.info(
            "canonical distill thread=%s: transcript unchanged since last distill - skipping",
            thread_id[:16],
        )
        return False
    request_id = "distill-canon-" + uuid.uuid4().hex[:12]
    llm_once = _llm_once_for(key, request_id)
    distilled = True
    try:
        doc = await run_map_reduce(
            transcript,
            llm_once,
            max_distill_tokens=await distiller_config.max_tokens(),
            deadline=deadline,
        )
    except DistillBudgetExceeded:
        # Sweep budget lapsed mid-row (audit D-6): leave the row due, write
        # nothing, back off nothing -- the row is healthy, the PASS ran out of
        # time, and the next pass picks it up from the start.
        log.info(
            "canonical distill thread=%s: sweep budget exhausted mid-row - deferred",
            thread_id[:16],
        )
        return False
    except DistillUnavailable as exc:
        # 2026-10-10 RCA: this exit was SILENT. A distill that spent nothing and
        # wrote nothing looked identical to a successful pass -- the row stayed
        # due, the store never grew, and NOTHING was logged anywhere. Say why.
        log.warning(
            "canonical distill thread=%s: model route unavailable (%s) - no spend, retried next sweep",
            thread_id[:16],
            exc,
        )
        # Back off like a gate skip (audit P2-1): without a marker the sweep
        # re-attempts the FULL map-reduce chain every pass for a permanently
        # exhausted key — the starvation gate_failed_at exists to prevent.
        # Safe since P0-1: the skip marker keeps the row due once the backoff
        # lapses.
        await distill_schedule.mark_canonical_gate_skip(key_id, thread_id)
        return False
    except Exception:
        if not _raw_fallback_enabled():
            # Leave the row DUE (no marker): canonical_conversations still holds
            # the conversation, so the next sweep retries and nothing is lost.
            # Writing the raw transcript would be CHUNKED by mm-rag into 100+
            # fragments -- measured 2026-10-10: 126 chunks from one session.
            log.warning(
                "canonical distill thread=%s: model failed; raw fallback disabled - deferring %ss",
                thread_id[:16],
                int(distill_schedule.gate_backoff_seconds()),
                exc_info=True,
            )
            # GATE-SKIP BACKOFF (audit D-5): with the config-RECOMMENDED
            # distiller_raw_fallback=false this branch is the common one, and
            # "left due" alone re-spent the whole map-reduce every 60 s on the
            # window head. Not a distilled marker: nothing was written, and the
            # row returns to the due set when the window lapses.
            await distill_schedule.mark_canonical_gate_skip(key_id, thread_id)
            return False
        log.warning(
            "canonical distill failed (thread=%s); degrading to raw transcript",
            thread_id[:16],
            exc_info=True,
        )
        doc = transcript
        distilled = False
    try:
        mcp_call = await _mcp_call_for(key)
    except DistillUnavailable as exc:
        log.warning(
            "canonical distill thread=%s: memory server unavailable (%s) - backoff applied (review FLAG-4)",
            thread_id[:16],
            exc,
        )
        await distill_schedule.mark_canonical_gate_skip(key_id, thread_id)
        return False
    verdict: list[str] = []
    ok, detail = await memory_write.write_memory(
        key_id,
        mcp_call,
        doc,
        session_id=thread_id[:64],
        kind=memory_write.KIND_SUMMARY if distilled else memory_write.KIND_FALLBACK,
        fallback_doc=transcript if distilled else "",
        fallback_session_id=thread_id[:64],
        mcp_search=await _mcp_search_for(key),
        verdict_out=verdict,
    )
    if ok:
        await distill_schedule.mark_canonical_distilled(key_id, thread_id, row.get("updated_at"), _content_hash)
    elif verdict and verdict[-1] == memory_write.VERDICT_SUPPRESSED:
        # NEAR-DUPLICATE SUPPRESSION IS A VERDICT, NOT A FAILURE (audit D-2).
        # The condition is PERMANENT: re-distilling the same row produces the
        # same near-copy and is suppressed again, so treating it as a failure
        # left the row due and re-ran the whole ~25-call map-reduce on every
        # sweep forever (measured: 4 passes -> 4 map-reduces, 0 writes, 0
        # markers). Mark the row with the ROW-STATE SNAPSHOT exactly like a
        # successful distill: the store already holds the memory this document
        # duplicates, so nothing is lost, and a conversation that CONTINUES
        # re-becomes due normally (updated_at moves past the snapshot).
        await distill_schedule.mark_canonical_distilled(key_id, thread_id, row.get("updated_at"), _content_hash)
        log.info(
            "canonical distill thread=%s: write suppressed as a near-duplicate - marked so it stops re-distilling",
            thread_id[:16],
        )
    log.info("canonical distill thread=%s ok=%s detail=%s", thread_id[:16], ok, detail[:80])
    return bool(ok)
