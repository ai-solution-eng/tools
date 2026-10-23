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

import json
import logging
import os
import time
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import UTC, datetime
from typing import Any

import anyio
import fastapi
import httpx2
from fastapi import Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from ..config import settings
from ..core import (
    canonical,
    clientside,
    embedcache,
    guardrails,
    model_fallbacks,
    model_routing,
    offpeak,
    registry,
    secretbox,
    toolrelay,
)

# TTFT / budget-warning / concurrency metrics (settle-side counters live in
# core.budgets so metric and ledger agree by construction). No-op shims when
# prometheus_client is absent.
from ..core import metrics as _metrics
from ..core.budgets import budget_status, check_budgets, check_rate_limit, settle
from ..core.cascade import classify_request, profile_order_for_key, resolve_cascade
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


def _assistant_message_text(payload: dict[str, Any]) -> str:
    """The assistant text of a non-streaming chat response ("" when absent).

    Handles both the plain string content shape and the multimodal
    content-parts shape (only text parts carry policy-relevant content).
    """
    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(p.get("text", "") for p in content if isinstance(p, dict) and isinstance(p.get("text"), str))
    return ""


def _sse_delta_texts(chunk: bytes) -> list[str]:
    """Assistant text deltas carried in one raw SSE byte chunk.

    Byte chunks may straddle SSE line boundaries — a delta JSON split
    across two chunks simply fails json.loads and is skipped; the FINAL
    assembled text is a best-effort guard input, not a transcript (the
    upstream re-parse in _proxy_json is the exact one). `pcai` relay
    events and usage-only chunks carry no assistant text and are skipped.
    """
    out: list[str] = []
    for line in chunk.split(b"\n"):
        line = line.strip()
        if not line.startswith(b"data:"):
            continue
        payload = line[len(b"data:") :].strip()
        if not payload or payload == b"[DONE]":
            continue
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict) or "pcai" in obj or "error" in obj:
            continue
        try:
            delta = obj["choices"][0].get("delta") or {}
        except (KeyError, IndexError, TypeError, AttributeError):
            continue
        content = delta.get("content")
        if isinstance(content, str) and content:
            out.append(content)
    return out


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


def _check_model_allowed(key: KeyRecord, model: str) -> None:
    """Enforce the key's allowed-models list; empty list = all models."""
    if key.allowed_models and model not in key.allowed_models:
        raise fastapi.HTTPException(403, f"key is not allowed to use model {model!r}")


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
    """
    prompt = sum(len(str(m.get("content") or "")) // 4 for m in body.get("messages") or [])
    max_tokens = body.get("max_tokens")
    completion_est = min(int(max_tokens), 8192) if max_tokens else 1024
    return prompt + completion_est


def _estimate_embedding_tokens(body: dict[str, Any]) -> int:
    """Estimate tokens for an embeddings call from the raw input payload."""
    return len(str(body.get("input") or "")) // 4


def _routing_available(key: KeyRecord) -> Callable[[str], bool]:
    """Availability predicate for the complexity-router's ladder walk.

    A ladder model is servable for THIS key when it is registered (a pool
    exists), inside the key's allowed_models (the ACL is a hard boundary —
    a routing pseudo-name must never become an ACL bypass), and has a
    healthy chat-typed endpoint. Built per request because it closes over
    the key; core.model_routing stays a pure decision module.
    """

    def _ok(name: str) -> bool:
        if key.allowed_models and name not in key.allowed_models:
            return False
        p = model_router.pool(name)
        if p is None or not p.healthy():
            return False
        return any(e.model_type == "chat" for e in p.healthy())

    return _ok


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
    headers = {"X-Request-ID": request_id}
    if ep.auth_token_enc:
        token = secretbox.decrypt(ep.auth_token_enc)
        if token:
            headers["Authorization"] = f"Bearer {token}"
            return headers
        log.error(
            "endpoint %s has a stored credential but it is undecryptable "
            "(master key missing/rotated) — calling unauthenticated",
            ep.id,
        )
    if ep.api_key_secret:
        env_name = "PCAI_LLM_UPSTREAM_KEY__" + ep.api_key_secret.upper().replace("-", "_")
        secret = os.environ.get(env_name)
        if secret:
            headers["Authorization"] = f"Bearer {secret}"
        else:
            log.warning(
                "upstream key secret %r not found as env var %s; forwarding without Authorization header",
                ep.api_key_secret,
                env_name,
            )
    return headers


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
    rate = await check_rate_limit(key.id, key.rate_limit_rpm, key.rate_limit_tpm, est_tokens)
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
    double-release in odd error paths)."""
    _in_flight[key_id] = max(0, _in_flight.get(key_id, 0) - 1)


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
        try:
            resp = await model_router.client.post(cur_url, json=body, headers=cur_headers)
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
            # except on a failover-eligible 5xx with chain left: the breaker
            # report + error settle happen exactly as before, then the walk
            # continues. 4xx is the caller's/request's fault — never re-rolled.
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
            if not (500 <= resp.status_code < 600 and chain):
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
        prompt_tokens = int(usage.get("prompt_tokens") or 0) or est_tokens
        completion_tokens = int(usage.get("completion_tokens") or 0)
        # Usage-detail passthrough (the plain-path twin of toolrelay._cached_tokens):
        # vLLM/SGLang report upstream prompt-cache hits as
        # prompt_tokens_details.cached_tokens — flattened away before 0.9.3, so
        # non-streaming ledger rows read cached_tokens=0 even when the upstream
        # prefilled from cache. Anything absent/odd is 0 (no fabricated facts).
        _details = usage.get("prompt_tokens_details")
        cached_tokens = int(_details.get("cached_tokens") or 0) if isinstance(_details, dict) else 0

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
            if guarded_text != text and embedding_json:
                try:
                    embedding_json["choices"][0]["message"]["content"] = guarded_text
                except (KeyError, IndexError, TypeError):
                    pass  # unusual shape: relay unmodified rather than corrupt
                else:
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
            **model_routing.ledger_stamp(route if cur_model == model else None, cur_model),
        )
        if cache_input is not None:
            vector = embedcache.extract_embedding(embedding_json)
            if vector is not None:
                await embedcache.store(cur_model, cache_input, vector)  # string inputs only
        _log_request(key.key_prefix, cur_model, cur_ep, "ok", latency, request_id)
        if want_metadata and embedding_json:
            from ..core import costs as _costs

            cost = await _costs.compute(cur_model, prompt_tokens, completion_tokens, cached_tokens)
            mutated = _pcai_metadata_body(
                embedding_json,
                cur_model,
                cur_ep.id,
                cost_usd=cost,
                cached_tokens=cached_tokens,
            )
            if mutated is not None:
                out_content = json.dumps(mutated).encode()
        return Response(
            content=out_content,
            status_code=resp.status_code,
            media_type=resp.headers.get("content-type", "application/json"),
            headers=out_headers,
        )


def _finish_reason_of(payload: dict[str, Any]) -> str:
    """choices[0].finish_reason of a non-streaming chat response ('' absent)."""
    try:
        val = payload["choices"][0].get("finish_reason")
    except (KeyError, IndexError, TypeError, AttributeError):
        return ""
    return val if isinstance(val, str) else ""


async def _next_fallback_candidate(
    chain: list[str],
    served: str,
    key: KeyRecord,
    endpoint: str,
    est_tokens: int,
    request_id: str,
    started: float,
) -> tuple[tuple[Pool, Endpoint, str, dict[str, str]] | None, list[str]]:
    """The next WALKABLE fallback candidate + the REMAINING chain, or
    (None, chain) when no candidate qualifies (review §4a).

    Every candidate is re-gated at walk time — the chain is a preference
    list, never a policy/budget bypass:
      * registered in the live router snapshot;
      * inside the key's allowed_models (ACL is a hard boundary);
      * a HEALTHY chat pool (embedding/reranker pools never serve chat);
      * fresh `check_budgets` headroom — an exhausted candidate settles its
        own 'budget_exceeded' row (visible, budget-source-of-truth) and is
        skipped, exactly like the admission cascade treats it;
      * pool.pick() non-None (a raced cooldown is skipped, not served).
    Only chat endpoints ever walk (the `endpoint` guard); the consumed
    primary (`served`) is skipped. Bounded: the caller's chain is already
    sliced to settings.model_fallback_max, and each hop consumes its
    prefix — the walk cannot cycle or exceed the cap.
    """
    if endpoint != "chat":
        return None, chain
    registered = set(model_router.models())
    for idx, cand in enumerate(chain):
        if cand == served:
            continue
        if cand not in registered:
            continue
        if key.allowed_models and cand not in key.allowed_models:
            continue
        cand_pool = model_router.pool(cand)
        if cand_pool is None or not cand_pool.healthy():
            continue
        if cand_pool.endpoints and cand_pool.endpoints[0].model_type != "chat":
            continue
        try:
            budget = await check_budgets(key.id, cand, est_tokens)
        except Exception:
            log.warning("fallback: budget check failed for %r — skipping candidate", cand)
            continue
        if not budget.ok:
            await settle(key.id, cand, endpoint, 0, 0, "budget_exceeded", _latency_ms(started), request_id)
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
            billing_fraction=offpeak.effective(self._off_base),
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


# Cross-request identical-call loop guard (per key). The relay's IN-LOOP
# loop detection is per-request (RelayGuard); the mixed-mode handback moved
# part of the loop into the HARNESS (each handback = a new request), so a
# model ping-ponging the same call across turns was unbounded — observed live
# as an endless bash/logsearch/edit re-issue cycle, each turn costing a
# full-history prefill. Fingerprints (key, server, tool, args) seen >=5x in
# 10 minutes are refused WITHOUT executing.
_LOOP_GUARD_WINDOW_S = 600.0
_LOOP_GUARD_MAX_HITS = 5
# Distinct-fingerprint cap (audit finding 20): over the cap the OLDEST
# fingerprint is evicted (insertion order), never a bulk clear.
_LOOP_GUARD_MAX_ENTRIES = 5000
_loop_guard: dict[tuple[Any, str, str, str], list[float]] = {}


def loop_guard_check(key_id: Any, server: str, tool: str, arguments: dict) -> str | None:
    """Return a refusal reason when this exact call is looping across
    requests, else None (and record the attempt). In-process state; the
    map is pruned lazily and hard-capped."""
    import json as _json

    fp = (key_id, server, tool, _json.dumps(arguments, sort_keys=True, default=str))
    now = time.monotonic()
    hits = [t for t in _loop_guard.get(fp, []) if now - t < _LOOP_GUARD_WINDOW_S]
    if len(hits) >= _LOOP_GUARD_MAX_HITS:
        _loop_guard[fp] = hits  # keep pruning without recording the blocked hit
        return (
            f"loop guard: identical call {server}/{tool} made {len(hits) + 1}x in "
            f"{_LOOP_GUARD_WINDOW_S / 60:.0f}m — do not repeat it; use the result you already have or change approach"
        )
    hits.append(now)
    _loop_guard[fp] = hits
    # Hard cap (audit finding 20 — the bulk clear let ANY key wipe EVERY
    # key's loop protection with 5000 distinct fingerprints): evict the
    # OLDEST fingerprint instead. _loop_guard is plain-insertion-ordered
    # (dict); the oldest entry is the first key — pop it until under the
    # cap. Cross-tenant interference becomes bounded (one evicted entry per
    # over-cap insert) instead of total.
    while len(_loop_guard) > _LOOP_GUARD_MAX_ENTRIES:
        oldest = next(iter(_loop_guard))
        _loop_guard.pop(oldest, None)
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

    Used ONLY by the relay's mid-loop budget failover — a turn whose model
    dies of budget MID-STREAM continues on the next ordered model instead of
    ending answerless. Deliberately BROADER than admission failover: a
    PINNED request still serves (and rate-429s) on its model alone, but the
    same promotion resolve_cascade applies at admission when the pinned
    model has no headroom ("budget exhaustion on the pinned model promotes
    the request to the ordering") applies here when that exhaustion is
    discovered mid-flight. Fresh budget gates re-check each candidate, so a
    candidate that is itself exhausted is skipped, not served.
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
):
    """Build the relay's MCP executor: ledger-settled, ACL-rechecked,
    in-cluster call to the upstream MCP server.

    Gates mirror the /mcp call path (security audit): the per-server
    tool-policy (`server_allows`) and the key's RPM/TPM window are enforced
    here too — tightening a server to 'allowlist' or exhausting the key's
    rate budget must cut off the RELAY path exactly as it cuts off /mcp,
    otherwise the automated path is a policy bypass.
    """
    from mcp import ClientSession

    from ..mcpf.federation import server_allows
    from .routes_mcp import _upstream_session, resolve_mcp_headers

    async def execute(server: str, tool: str, arguments: dict) -> tuple[bool, Any]:
        servers = {sv["name"]: sv for sv in await registry.mcp_servers()}
        target = servers.get(server)
        if target is None:
            return False, f"unknown server {server!r}"

        # cross-request loop guard BEFORE anything touches an upstream
        guard_reason = loop_guard_check(key.id, server, tool, arguments)
        if guard_reason:
            await settle(key.id, "*", f"mcp:{server}:{tool}", 0, 0, "error", 0, request_id)
            return False, guard_reason

        # rate gate FIRST (same order as /mcp: burst-guard before anything
        # touches an upstream); tool calls consume 0 tokens, this is purely
        # the RPM axis
        rate = await check_rate_limit(key.id, key.rate_limit_rpm, key.rate_limit_tpm, 0)
        if not rate.ok:
            await settle(key.id, "*", f"mcp:{server}:{tool}", 0, 0, "error", 0, request_id)
            return False, f"rate limited: {rate.reason}"

        # per-server tool-policy gate (defense-in-depth over the key ACL,
        # which relay_stream already enforced before calling us)
        if not server_allows(target, tool):
            _metrics.observe_acl_denial(server)
            await settle(key.id, "*", f"mcp:{server}:{tool}", 0, 0, "error", 0, request_id)
            return False, "denied by server policy"

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
            await settle(key.id, "*", f"mcp:{server}:{tool}", 0, 0, "error", 0, request_id)
            return False, f"upstream failed: {type(exc).__name__}"
        ok = not result.is_error
        await settle(key.id, "*", f"mcp:{server}:{tool}", 0, 0, "ok" if ok else "error", 0, request_id)
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
    """
    buf = bytearray()
    ttft_done = False  # TTFT fires once, on the first upstream byte batch
    try:
        async for chunk in resp.aiter_bytes():
            buf += chunk
            idx = buf.find(b"\n")
            while idx != -1:
                ledger.feed(bytes(buf[:idx]))
                del buf[: idx + 1]
                idx = buf.find(b"\n")
            if not ttft_done:
                # TTFT: the first upstream byte batch reaching the client
                # proxy loop (covers both pass-through and the
                # guard-buffered wrapper — this generator runs inside
                # either). Elapsed against the ledger's request clock
                # (both monotonic). getattr guard: unit tests construct
                # bare ledgers whose clocks may be absent — metrics must
                # never break a stream.
                ttft_done = True
                _started = getattr(ledger, "_started", None)
                if _started is not None:
                    _metrics.observe_ttft(getattr(ledger, "_model", ""), time.monotonic() - _started)
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
    Masked → content-carrying chunks are SUPPRESSED and one consolidated
             chunk with the masked text is emitted (tool/`pcai` events and
             non-content chunks replay verbatim — the mask must not leak).
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
    text_parts: list[str] = []
    try:
        async for chunk in inner:
            chunks.append(chunk)
            text_parts.extend(_sse_delta_texts(chunk))
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

    text = "".join(text_parts)
    try:
        guarded_text, post_events, blocked = await guardrails.run_post_call(post_guards, text, model_router.client)
    except Exception as exc:  # fail-open, visible: policy engine errors must not eat the answer
        log.warning("guardrails: post_call evaluation failed: %s", exc)
        guarded_text, post_events, blocked = text, [], False
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
        if release_slot is not None:
            release_slot()
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

    if guarded_text == text:
        # pass: byte-identical replay
        for chunk in chunks:
            yield chunk
        return
    # masked: suppress content-carrying chunks (and any original [DONE]),
    # replay the rest verbatim, then the consolidated masked content.
    for chunk in chunks:
        if not _sse_delta_texts(chunk) and b"data: [DONE]" not in chunk:
            yield chunk
    yield _sse_masked_final(model, request_id, guarded_text)


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
    while True:
        send_body = body if body.get("stream_options") else {**body, "stream_options": {"include_usage": True}}
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
        break

    if not _is_2xx(resp.status_code):
        # Read the error body fully, close the stream context, and relay it
        # as an ordinary (non-SSE) response with the upstream status code.
        # A failover-eligible 5xx with chain left walks pre-first-byte
        # (review §4a): the error body never reached the client, so the
        # switch is invisible today and survivable with the chain.
        payload = await resp.aread()
        await resp.aclose()
        latency = _latency_ms(started)
        await cur_pool.report(cur_ep, False, error=f"upstream status {resp.status_code}")
        await settle(key.id, cur_model, "chat", 0, 0, "error", latency, request_id)
        _log_request(key.key_prefix, cur_model, cur_ep, "error", latency, request_id)
        if 500 <= resp.status_code < 600 and chain:
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
    ledger = _StreamLedger(
        key, cur_model, est_tokens, request_id, started, off_base, pre_events=pre_events, route=route
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
        return StreamingResponse(
            _guarded_sse(
                _relay_sse(resp, ledger, release_slot=release_slot, defer_finish=True),
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
        )
    return StreamingResponse(
        _relay_sse(resp, ledger, release_slot=release_slot),
        media_type="text/event-stream",
        headers=stream_headers,
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
    one (client-side execution needs the real argument schema).

    FIX (was latent in the inline version): the old loop iterated only
    EXPLICITLY granted servers, so a scope-mode key with no explicit ACL
    rows fetched no catalogs and relayed with an empty toolset — silently
    contradicting the documented scope semantics. Scope mode now sweeps all
    enabled (or requested) servers.
    """
    from ..mcpf.federation import server_allows
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
                **({"inputSchema": t["inputSchema"]} if isinstance(t.get("inputSchema"), dict) else {}),
            }
            for t in tools
            if server_allows(target, t["tool"])
        )
    if scope_mode:
        # scope-mode: the policy-admitted catalog IS the effective grant
        # set — explicit grants merge in on top
        relay_grants = relay_grants | {(t["server"], t["tool"]) for t in catalog}
    catalog = [t for t in catalog if (t["server"], t["tool"]) in relay_grants]
    if requested_servers and not catalog:
        raise fastapi.HTTPException(
            400,
            f"no MCP tools granted for requested server(s) {', '.join(sorted(requested_servers))}"
            " — ask the operator for grants, or drop the server filter",
        )
    return catalog, relay_grants


def _persist_conversation_state(key: KeyRecord):
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
        n = len(stored)
        if n > len(final_messages) or final_messages[:n] != stored:
            # The store moved under us (a racing request reconciled a
            # different shape): the slice would be a guess — skip loudly.
            log.warning(
                "canonical: pending-tail flush skipped (stored canonical is not a prefix of the final list, thread=%s)",
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
) -> tuple[list[dict[str, Any]], str, dict[str, Any] | None, int]:
    """Canonical conversation reconciliation for the relay path (ROADMAP §7,
    phase 1) — returns (messages_to_relay, mode, continuation, iterations_prior).

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
                    lambda msgs: _estimate_tokens({**body, "messages": msgs}),
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
            return relay_projection, "continued", cont_shape, iterations_prior
        except fastapi.HTTPException:
            raise
        except Exception as exc:
            log.warning("canonical: continuation merge failed (continuing on raw incoming): %s", exc)
            return incoming, "raw", None, 0

    escape = (request.headers.get("x-pcai-conversation") or "").strip().lower()
    if escape == "raw" or not settings.canonical_conversations:
        return incoming, "raw", None, 0
    try:
        thread_id = canonical.thread_key(incoming, key.id)
        if thread_id is None:
            return incoming, "raw", None, 0  # no first user message -> nothing to key the store on
        stored, iterations_prior = await canonical.load_row(db, key.id, thread_id)
        messages, mode = canonical.reconcile(stored, incoming)
        # Phase 2A: an "extend" whose new tail is only the handback's
        # assistant tool_calls + their role=tool results is a handback
        # continuation — surface it for the relay loop's trace linkage.
        # The BOOL decides: detect_continuation's negative return carries a
        # (truthy) shaped-empty info dict, so the info alone must never be
        # trusted as the verdict.
        detected, cont_info = (
            canonical.detect_continuation(stored, incoming) if mode == canonical.MODE_EXTEND else (False, None)
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
                lambda msgs: _estimate_tokens({**body, "messages": msgs}),
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
        return relay_projection, mode, continuation, iterations_for_guard
    except Exception as exc:
        log.warning("canonical: reconciliation failed (continuing on raw incoming messages): %s", exc)
        return incoming, "raw", None, 0


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
    registered = set(model_router.models())
    requested_model = model

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
    try:
        # The order is fetched here (not inside the router) so the no-ladder
        # special cases and the cascade reuse ONE lookup; resolve_cascade
        # re-fetches internally for its own walk (unchanged).
        order = await profile_order_for_key(key.id) if _pseudo is not None else []
        model, route_ctx = await model_routing.resolve_routed_model(
            key,
            body,
            model,
            registered,
            key.allowed_models,
            order,
            available=_routing_available(key) if _pseudo is not None else None,
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

    resolution = await resolve_cascade(key.id, model, registered, key.allowed_models, est_tokens)
    if not resolution.ok:
        detail: dict[str, Any] = {"error": resolution.reason}
        if resolution.attempts is not None:
            detail["cascade_attempts"] = resolution.attempts
        raise fastapi.HTTPException(
            429 if resolution.attempts is not None else 403 if "not allowed" in resolution.reason else 404, detail
        )
    model = resolution.model  # the model that will actually serve

    pool = model_router.pool(model)
    if pool is None:
        raise fastapi.HTTPException(404, f"model {model!r} is not registered")
    if pool.endpoints and pool.endpoints[0].model_type != "chat":
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
    # Strip reasoning_content / reasoning from INBOUND message history before
    # anything downstream sees it (guardrails, token gates, the upstream).
    # Chat models don't consume reasoning blocks; some reject them outright.
    # Clients whose agent loop re-appends streamed reasoning (DSH 2026-09-18)
    # otherwise inflate every handback cycle's input by the whole accumulated
    # block — streamed OUT for review, never sent BACK in. Deliberately
    # message-shape-preserving: only the reasoning key is dropped, everything
    # else (content, tool_calls, ids) rides untouched.
    for _m in body["messages"]:
        if isinstance(_m, dict):
            _m.pop("reasoning_content", None)
            _m.pop("reasoning", None)

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
    header_val = request.headers.get("x-pcai-execute-tools")
    relay_requested = _relay_wants(header_val, key.relay_default, settings.tool_relay_enabled)
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
        from ..mcpf.federation import load_grants

        relay_grants = await load_grants(key.id)
        scope_granted = settings.tool_relay_grant_mode == "scope" and "mcp" in key.scopes
        if not relay_grants and not scope_granted:
            raise fastapi.HTTPException(400, "tool execution requested but this key has no MCP grants")
    if approval_always:
        # Client-side execution: expose the granted catalog as OpenAI
        # function tools; the model's tool_calls stream back untouched and
        # the client decides what to execute. No relay, no server-side
        # execution, normal gates/settle on the model call itself.
        catalog, _ = await _relay_catalog(key, relay_grants, requested_servers)
        if not catalog:
            raise fastapi.HTTPException(400, "approval passthrough requested but no MCP tools are granted to this key")
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
        relay_requested = False

    # --- admission failover (rate/budget 429 -> next cascade candidate) ---
    # The cascade already failovers BUDGET headroom; this walk adds the RATE
    # axis. Alias/auto requests only — pinned requests never cascade (the
    # client asked for THAT model; documented cascade semantics).
    candidates = [resolution.model]
    if classify_request(requested_model, registered, key.allowed_models) == "alias":
        try:
            order = await profile_order_for_key(key.id)
        except Exception:
            order = []
        for cand in order:
            if cand in candidates or cand not in registered:
                continue
            if key.allowed_models and cand not in key.allowed_models:
                continue
            candidates.append(cand)

    model, gate, admission_fallback_from = await _admit_with_failover(
        key, candidates, est_tokens, request_id, started, resolution.gate
    )
    warn_level = gate.warn_level
    # The admission walk may have PROMOTED the request to a different model
    # (rate/budget 429 down the profile ordering): the per-model chain must
    # be the CHAIN OF THE MODEL THAT WILL ACTUALLY SERVE, not the name that
    # arrived. A fresh read; a chain read failure is fail-safe (empty —
    # the fallback feature never breaks a request).
    fb_chain = await model_fallbacks.chain_for(model)
    fallback_from = admission_fallback_from

    try:
        ep = pool.pick()
        if ep is None:
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
            nxt, fb_rest = await _next_fallback_candidate(fb_chain, "", key, "chat", est_tokens, request_id, started)
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
        render_deltas = (
            settings.tool_relay_render_deltas
            and request.headers.get("x-pcai-relay-deltas", "0") == "1"
        )

        if relay_requested:
            catalog, relay_grants = await _relay_catalog(key, relay_grants, requested_servers)
            execute = await _relay_executor(key, request_id)

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
            relay_messages, _recon_mode, continuation, iterations_prior = await _reconcile_relay_messages(
                request, key, body
            )

            def stream_once(msgs: list[dict], tools: list[dict] | None, pool_for: Any = None):
                """One model call as an async GENERATOR of typed events.
                `pool_for` overrides the request's pool — the relay's mid-loop
                model failover re-points iterations at the fallback pool."""
                p = pool_for or pool
                ep2 = p.pick() or (p.endpoints[0] if p.endpoints else ep)
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
                if tools:
                    b2["tools"] = tools

                async def gen():
                    resp = await model_router.client.send(
                        model_router.client.build_request("POST", url2, json=b2, headers=h2), stream=True
                    )
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
                            yield ("error", str(resp.status_code))
                            yield ("content", f"(model error {resp.status_code})")
                            return
                        async for ev in toolrelay.stream_model_call(resp):
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
            relay_tail = _relay_fallback_tail(model, resolution.order or [], registered, key.allowed_models)
            fallbacks: list[tuple[str, Any]] = []
            if relay_tail:
                for cand in relay_tail:
                    fp = model_router.pool(cand)
                    if fp is None or not fp.healthy():
                        continue
                    if fp.endpoints and fp.endpoints[0].model_type != "chat":
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

            async def relay_gen():
                iteration_depth = 0  # observed in finally — see below
                try:
                    async for chunk in toolrelay.relay_stream(
                        relay_messages,
                        relay_grants,
                        catalog,
                        stream_once,
                        execute,
                        request_id,
                        model,
                        key_id=key.id,
                        user_id=key.user_id,
                        key_fraction=off_base,
                        started=started,
                        estimate=lambda msgs: _estimate_tokens({**body, "messages": msgs}),
                        max_tokens=body.get("max_tokens"),
                        client_tools=list(body.get("tools") or []),
                        client_loop_guard=client_loop_guard,
                        include_usage=bool((body.get("stream_options") or {}).get("include_usage")),
                        stream_thinking=stream_thinking,
                        fallbacks=fallbacks,
                        failover_note=failover_note,
                        continuation=continuation,
                        # Wave R1 stamped render deltas (both-gates resolved
                        # above): display-only tool_call chunks for clients
                        # that opted in via X-PCAI-Relay-Deltas: 1.
                        render_deltas=render_deltas,
                        render_index_base=settings.tool_relay_render_index_base,
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
                        conversation_state=_persist_conversation_state(key),
                        # RFC-PER-KEY-STREAM-EVENTS: key default + header
                        # override resolved above.
                        stream_pcai_events=stream_events,
                    ):
                        # Count model iterations WITHOUT parsing protocol
                        # internals: the relay emits exactly one
                        # model_usage pcai-event per completed model call
                        # (its ledger contract), so byte-matching the event
                        # name gives the iteration depth cheaply and never
                        # drifts from the settle contract.
                        if b'"event": "model_usage"' in chunk or b'"event":"model_usage"' in chunk:
                            iteration_depth += 1
                        yield chunk
                finally:
                    # Every exit path (done, guard, disconnect) observes the
                    # depth reached; histogram in [0..max_iterations].
                    _metrics.observe_relay_iterations(model, iteration_depth)
                    _release_in_flight(key.id)

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
            if guardrail_post:
                # Post-call guardrails on the relayed stream: buffer, judge
                # the FINAL answer, replay (verbatim / masked / blocked).
                # Pre-call verdicts ride the marker row with the post ones.
                return StreamingResponse(
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
                )
            return StreamingResponse(
                relay_gen(),
                media_type="text/event-stream",
                headers=stream_headers,
            )
    except BaseException:
        # Slot-leak fix (security audit): EVERY post-gate raise must give back
        # the concurrency slot _run_gates took — relay catalog failures,
        # unexpected DB errors, the 503 above. Re-raised untouched.
        _release_in_flight(key.id)
        raise

    if bool(body.get("stream")):
        # Streaming: slot ownership TRANSFERS to the stream generator
        # (released in _relay_sse's finally, when the stream actually ends).
        # Releasing here — at response-object construction — would free the
        # slot before a single chunk was consumed, voiding the cap for the
        # exact workload it exists to constrain.
        return await _stream_chat(
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
            release_slot=lambda: _release_in_flight(key.id),
            post_guards=guardrail_post or None,
            pre_events=guard_events or None,
            route=route_ctx,
            fallback_from=fallback_from,
            fallback_chain=fb_chain,
        )
    try:
        return await _proxy_json(
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
        )
    finally:
        _release_in_flight(key.id)


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
    if pool.endpoints and pool.endpoints[0].model_type not in ("embedding",):
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
        # visible + free: a ledger row the budgets SUM ignores
        await settle(key.id, model, "embeddings", est, 0, "cache_hit", latency, request_id, cached_tokens=est)
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


@router.get("/models")
async def list_models(
    key: KeyRecord = fastapi.Depends(require_scope("proxy")),
) -> dict[str, Any]:
    """List registered virtual models in OpenAI /v1/models shape.

    Read-only listing: no budget/rate-limit gate and no ledger row — the
    listing itself consumes no model tokens and polluting the ledger would
    skew spend reporting.

    When the key's profile carries a model ordering, a synthetic `auto`
    entry PRECEDES the registered models: requests naming it resolve through
    the ordering (first candidate with budget headroom — the alias path),
    and the ordering rides `pcai.order` so clients can show what auto means.
    The chat console's model dropdown picks this up with no special-casing.
    """
    allowed = set(key.allowed_models) if key.allowed_models else None
    registered = [m for m in model_router.models() if allowed is None or m in allowed]
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
    primary = next((m for m in order if m in registered_set), "")

    def _pool_type(m: str) -> str:
        p = model_router.pool(m)
        return p.endpoints[0].model_type if p is not None and p.endpoints else "chat"

    data = [
        {
            "id": m,
            "object": "model",
            "created": int(time.time()),
            "owned_by": "pcai-llm-gateway",
            "pcai": {"type": _pool_type(m), **({"primary": True} if m == primary else {})},
        }
        for m in registered
    ]
    # `auto` only when the ordering offers at least one callable candidate —
    # an auto with no resolvable model would just 404 at send time.
    # (order/registered_set computed above for the primary flag.)
    if any(m in registered_set for m in order):
        data.insert(
            0,
            {
                "id": "auto",
                "object": "model",
                "created": int(time.time()),
                "owned_by": "pcai-llm-gateway",
                "pcai": {"type": "cascade", "alias": True, "order": order},
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
    if settings.routing_enabled and policy in ("shadow", "active"):
        chat_registered = [
            m
            for m in registered
            if (p := model_router.pool(m)) is not None and p.endpoints and p.endpoints[0].model_type == "chat"
        ]
        for rid in ("routing", *(f"routing__{m}" for m in chat_registered)):
            data.append(
                {
                    "id": rid,
                    "object": "model",
                    "created": int(time.time()),
                    "owned_by": "pcai-llm-gateway",
                    "pcai": {"type": "routing"},
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
