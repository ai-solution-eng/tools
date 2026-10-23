"""Server-side tool-use relay: the model's tool_calls executed IN-CLUSTER.

WHY: with client-side loops every tool round-trip crosses the user's VPN
(DSH -> gateway -> MCP -> back, xN). With the relay, the whole loop runs in
the cluster (gateway<->model and gateway<->MCP are in-cluster hops) and only
the final answer + tool events cross to the client.

Protocol (OpenAI-compatible, gateway-specific extensions):
- Client sends the standard `tools` array OR sets header X-PCAI-Execute-Tools
  (tools then injected automatically from the key's MCP grants).
- The gateway iterates: model -> tool_calls -> execute granted MCP tools ->
  append role=tool results -> model again, until a final answer or a guard
  trips.
- Tool naming convention: `<server>__<tool>` (double underscore) — the flat
  OpenAI name maps to the MCP (server, tool) pair.
- Guards (AgentBuilder lessons): max iterations, wall-clock cap, per-call ACL
  (default-deny), loop detection (identical tool+args twice -> abort), and a
  ledger settle for every model call and every executed tool call.
- Client visibility: tool events are emitted as SSE lines
  `data: {"pcai": {...}}` interleaved before the final answer stream.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import anyio

from ..config import settings
from . import clientside, effective, offpeak, traces
from .budgets import check_budgets, settle
from .canonical import thread_key

log = logging.getLogger(__name__)

TOOLSEP = "__"

# SSE keepalive cadence inside a relay model call (seconds). A comment line
# every KEEPALIVE_S keeps proxy/VPN idle timers (and the operator's eyes)
# convinced the stream lives while a big generation or a queue wait runs.
KEEPALIVE_S = 15.0

# Minimum remaining wall-clock budget required to ATTEMPT a bounded-call
# timeout retry: the retry only fires when min(remaining wall clock,
# upstream_timeout_s) leaves at least this much — a doomed 3-second attempt
# against a still-queued endpoint is worse than failing now with the clean
# close the client already knows how to render.
TOOL_RELAY_RETRY_MIN_BUDGET_S = 10.0


def _cached_tokens(usage: dict | None) -> int:
    """Upstream prompt-cache hits for one model call (usage detail passthrough
    — parent addendum): vLLM/SGLang report prefix-cache hits as
    prompt_tokens_details.cached_tokens; the relay previously FLATTENED them
    away, so relay-settled ledger rows read cached_tokens=0 even when the
    upstream prefilled from cache (budgets carve cache out of spend, so the
    flat zero overcharged budget burn). Anything absent/odd is 0."""
    if not isinstance(usage, dict):
        return 0
    details = usage.get("prompt_tokens_details")
    if not isinstance(details, dict):
        return 0
    try:
        return int(details.get("cached_tokens") or 0)
    except (TypeError, ValueError):
        return 0


def _tool_trace_status(ok: bool, detail: Any) -> str:
    """Map one executed tool call to its trace status.

    The executor collapses its refusals into (False, <detail>): the detail
    text distinguishes a rate-limit refusal / policy denial / cross-request
    loop guard from a genuine upstream failure. Best-effort sniffing of the
    OWNED detail prefixes the routes-layer executor emits — anything
    unrecognized is simply 'error'.
    """
    if ok:
        return "ok"
    text = detail if isinstance(detail, str) else ""
    if text.startswith("rate limited"):
        return "rate_limited"
    if text.startswith("denied"):
        return "denied"
    if text.startswith("loop guard"):
        return "loop_guard"
    return "error"


def _preview(content: Any, cap: int = 4000) -> str:
    """Compact display preview of a tool result (wire-only; the model gets
    the full content via the relayed messages)."""
    text = content if isinstance(content, str) else json.dumps(content, default=str)
    return text if len(text) <= cap else text[:cap] + f" …[truncated, {len(text)} chars total]"


# Tool-result content cap for the RELAYED MESSAGES (audit finding: results
# entered `relay_msgs` at full size — one large result was re-prefilled on
# every remaining iteration (amplified GPU cost), buffered per hop, and
# persisted unbounded into the conversations pending column; client results
# ride in at up to 1 MB × 16 iterations). Chosen well above any legitimate
# tool output a model can act on (a model cannot attend past ~100K tokens);
# the full result still rides the wire events' preview cap and the trace.
RELAY_TOOL_RESULT_CAP_CHARS = 32_000


def _relay_content(content: Any) -> str:
    """The role=tool content string for relay_msgs: serialized + CAPPED.

    Truncation is explicit (the model sees the marker and can re-request
    narrower output), matching _preview's discipline but at a model-facing
    bound rather than a display bound."""
    text = content if isinstance(content, str) else json.dumps(content, default=str)
    if len(text) <= RELAY_TOOL_RESULT_CAP_CHARS:
        return text
    return (
        text[:RELAY_TOOL_RESULT_CAP_CHARS]
        + f" …[truncated by the gateway: tool result was {len(text)} chars; request narrower output]"
    )


@dataclass
class RelayEvent:
    """One intermediate step, reported to the client and the log."""

    kind: str  # tool_call | tool_error | guard
    server: str = ""
    tool: str = ""
    ok: bool = True
    ms: int = 0
    detail: str = ""
    # display-only payload so the chat console can expand a tool step:
    # the parsed arguments and a TRUNCATED result preview (the model still
    # gets the full result via relay_msgs — this is wire-only)
    args: Any = None
    result: str = ""

    def sse(self) -> bytes:
        payload: dict[str, Any] = {
            "event": self.kind,
            "server": self.server,
            "tool": self.tool,
            "ok": self.ok,
            "ms": self.ms,
            "detail": self.detail,
        }
        if self.args is not None:
            payload["args"] = self.args
        if self.result:
            payload["result"] = self.result
        return b"data: " + json.dumps({"pcai": payload}, default=str).encode() + b"\n\n"


@dataclass
class RelayResult:
    messages: list[dict]  # full conversation incl. tool results
    events: list[RelayEvent] = field(default_factory=list)
    iterations: int = 0
    aborted: str = ""  # guard reason when aborted


def tools_payload(catalog: list[dict]) -> list[dict]:
    """OpenAI tools array from the granted (server, tool) catalog.

    The `parameters` schema rides (audit fix): the catalog carries
    `inputSchema` all the way from the upstream listing and the
    approval-passthrough path maps it — omitting it here made the relayed
    model guess argument shapes from descriptions alone, and a hallucinated
    arguments string then hit the silent `{}` execution path. Schema-absent
    tools (upstream declared none) get the minimal object schema.
    """
    return [
        {
            "type": "function",
            "function": {
                "name": f"{t['server']}{TOOLSEP}{t['tool']}",
                "description": t.get("description") or f"{t['server']}/{t['tool']}",
                "parameters": t.get("inputSchema")
                if isinstance(t.get("inputSchema"), dict)
                else {"type": "object", "properties": {}},
            },
        }
        for t in catalog
    ]


def shadow_client_mcp(client_tools: list[dict], catalog: list[dict]) -> tuple[list[dict], dict[str, str]]:
    """Drop client MCP tools that have an unambiguous IN-CLUSTER twin.

    A client tool named `mcp__<server>__<tool>` shadows against a granted
    gateway tool with the SAME <tool> name when exactly one such grant exists
    (a unique match; ambiguity keeps the client variant). DSH's MCP servers
    are the same services the gateway registers — one reached over the
    client's VPN, one in-cluster — so the twin is a faster implementation of
    the same capability, not a different tool. Plain client tools (bash/edit
    — no MCP naming) never shadow: they have no cloud twin. Handback still
    accepts calls made under the client's original name, so shadowing only
    changes what the model is OFFERED, never correctness.

    HARNESS-PREFIXED NAMES (opencode-style `mcp__<connection>__<server>__<tool>`):
    a client connection may wrap the gateway's OWN /mcp endpoint, so the same
    tool rides under a DOUBLE prefix. Twin matching is therefore SUFFIX-based:
    the client name's `__`-separated tail is walked from the left and matched
    against `<server>__<tool>` full names — `mcp__pcai-llm-gateway__mm-rag__search_memory`
    carries tail `mm-rag__search_memory`, which is the granted gateway tool
    itself. First matching tail wins; ambiguity (one tail matching several
    grants, or several tails matching different grants) keeps the client
    variant — shadowing must never guess.

    Returns (kept_client_tools, reverse_map) — the reverse map translates a
    gateway tool's full name (`gwserver__tool`) back to the client's original
    name for PROTOCOL-TRUE mixed-batch handbacks: when a model turn batches a
    client tool with a cloud tool, rewriting the cloud name to the client's
    twin and letting the CLIENT execute the whole batch keeps every call
    paired with a proper role=tool result in the replayed history. (Executing
    in-cluster and injecting the result as assistant content makes the
    history lie about what the model called — observed live as an infinite
    bash/logsearch/edit re-issue loop.) The reverse mapping requires the
    client side to be unique too; ambiguous twins are shadowed from the offer
    but not rewritable, and fall back to in-cluster execution.
    """
    if not client_tools or not catalog or not settings.tool_relay_shadow_mcp:
        return client_tools, {}
    # gateway full names (`<server>__<tool>`) from the granted catalog
    gw_names = [f"{t['server']}{TOOLSEP}{t['tool']}" for t in catalog]
    counts: dict[str, int] = {}
    for t in catalog:
        counts[t["tool"]] = counts.get(t["tool"], 0) + 1
    kept: list[dict] = []
    shadowed: list[str] = []  # client original names dropped from the offer
    for t in client_tools:
        name = t.get("function", {}).get("name", "")
        if _suffix_matches_gateway(name, gw_names):
            shadowed.append(name)
            continue  # in-cluster twin exists (suffix match) — shadow it
        stripped = name.removeprefix("mcp" + TOOLSEP)
        if TOOLSEP in stripped:
            tool = stripped.split(TOOLSEP, 1)[1]
            if counts.get(tool) == 1:
                shadowed.append(name)
                continue  # legacy one-hop twin — shadow it
        kept.append(t)
    # reverse map: gateway full name -> client original name, only when the
    # client side is unambiguous (exactly one shadowed client tool per twin)
    client_counts: dict[str, int] = {}
    for name in shadowed:
        twin = _suffix_matches_gateway(name, gw_names)
        if twin is None:
            twin = (
                name.removeprefix("mcp" + TOOLSEP).split(TOOLSEP, 1)[1]
                if TOOLSEP in name.removeprefix("mcp" + TOOLSEP)
                else None
            )
        if twin is not None:
            client_counts[twin] = client_counts.get(twin, 0) + 1
    catalog_by_tool: dict[str, str] = {}
    for t in catalog:
        if counts[t["tool"]] == 1:
            catalog_by_tool[t["tool"]] = f"{t['server']}{TOOLSEP}{t['tool']}"
    reverse: dict[str, str] = {}
    for name in shadowed:
        twin = _suffix_matches_gateway(name, gw_names)
        if twin is None:
            stripped = name.removeprefix("mcp" + TOOLSEP)
            twin = stripped.split(TOOLSEP, 1)[1] if TOOLSEP in stripped else None
        if twin is not None and client_counts.get(twin) == 1 and twin in catalog_by_tool.values():
            reverse[twin] = name
    return kept, reverse


def _suffix_matches_gateway(name: str, gw_names: list[str]) -> str | None:
    """The gateway full name this client tool shadows, or None.

    Suffix matching for harness-prefixed names: a client that wraps the
    gateway's own /mcp endpoint under a connection prefix (opencode's
    `mcp__pcai-llm-gateway__<server>__<tool>`) offers the same tool under a
    name whose `__`-separated TAIL is the gateway's own full name. The tail
    is walked left-to-right over the `__` boundaries (a name has at most a
    handful) and the first tail that equals a granted gateway full name wins.
    ONLY whole-boundary tails match — `search_memory` never shadows
    `mm-rag__search_memory` (substring luck must not hijack a call).

    Returns None for plain client tools (bash/edit — no gateway tail), for
    client MCP servers that merely SHARE a tool name with a differently-
    named gateway server (their tail is `<their-server>__<tool>`, not one of
    our full names), and for any name whose matching is ambiguous — one
    name matching several gateway grants shadows nothing.
    """
    if name in gw_names:
        return name
    parts = name.split(TOOLSEP)
    if len(parts) < 2:
        return None
    matches: list[str] = []
    for i in range(1, len(parts)):
        tail = TOOLSEP.join(parts[i:])
        if tail in gw_names and tail not in matches:
            matches.append(tail)
    return matches[0] if len(matches) == 1 else None


def parse_tool_name(name: str) -> tuple[str, str] | None:
    """`server__tool` -> (server, tool); None for non-MCP names.

    A leading "mcp__" is stripped first: clients whose OWN harness names
    tools `mcp__<server>__<tool>` (opencode-style) leak that convention into
    model output when their conversations are replayed through the relay —
    without the strip, the parse yields a bogus server "mcp" and every
    converted-conversation tool call denies. No gateway server is named
    "mcp", so the prefix is unambiguous.
    """
    name = name.removeprefix("mcp" + TOOLSEP)
    if TOOLSEP not in name:
        return None
    server, tool = name.split(TOOLSEP, 1)
    return (server, tool) if server and tool else None


def alias_granted_gateway_name(name: str, catalog: list[dict]) -> tuple[str, str] | None:
    """Resolve a model-imitated tool name to its GRANTED gateway twin, or None.

    The name-duality failure mode (observed live): the harness registers the
    gateway's tools under its own prefix (`mcp__pcai-llm-gateway__mm-rag__search_memory`)
    while the relay OFFERS the bare flat form (`mm-rag__search_memory`) —
    shadowing dedupes the offer going forward, but conversations replayed
    from BEFORE that offer merge (or model text quoting one spelling) leak
    the prefixed spelling into the model's output. The call then parses as
    server="mcp" (bogus) or as an unknown server and errors "unknown tool",
    burning a relay iteration on a name the gateway plainly serves.

    Resolution is SUFFIX-based over the granted catalog: the name's
    `__`-separated tails are matched against `<server>__<tool>` full names
    and a UNIQUE match is returned as (server, tool). Never guesses — an
    ambiguous or non-matching name returns None and takes the normal
    unknown-tool path. Grant re-check stays at the call site (this function
    is name algebra, not ACL).
    """
    gw_names = [f"{t['server']}{TOOLSEP}{t['tool']}" for t in catalog or []]
    twin = _suffix_matches_gateway(name, gw_names)
    if twin is None:
        return None
    server, tool = twin.split(TOOLSEP, 1)
    return (server, tool)


def wall_clock_deadline(started: float | None = None, wall_clock_s: float | None = None) -> float:
    """The relay's wall-clock DEADLINE (a monotonic instant): the ingress
    clamp formula RelayGuard applies, as a standalone function.

    WHY a public helper (wave B2): routes_proxy must pass the SAME deadline
    into the client-tool dispatch executor that relay_stream derives for its
    own guard — a deadline the two sides computed differently would let a
    dispatch wait past the relay's clean-close budget (or choke one
    spuriously). One formula, both callers; the clamp (the wall clock,
    upstream_timeout minus ingress headroom, floored at 1s) can no longer
    drift. RelayGuard.__init__ is the second caller; `wall_clock_s=None`
    reads the settings knob (the routes-layer shape).
    """
    return (started if started is not None else time.monotonic()) + min(
        settings.tool_relay_wall_clock_s if wall_clock_s is None else wall_clock_s,
        max(1.0, settings.upstream_timeout_s - settings.tool_relay_ingress_headroom_s),
    )


class RelayGuard:
    """Iteration/wall-clock/loop guards — the AgentBuilder lessons, ported."""

    def __init__(self, max_iterations: int, wall_clock_s: float) -> None:
        self.max_iterations = max_iterations
        # Ingress clamp (the "avoid actual failures" guardrail): the relay's
        # whole response must close INSIDE the Istio virtualService timeout
        # (600s) — a wall clock at/above it means a burned budget races the
        # ingress 504 and the client gets an unparseable gateway error instead
        # of our clean guard event + error chunk + [DONE]. Clamping to the
        # timeout minus headroom keeps the clean close guaranteed; the wedge
        # test (wall_clock_s=0.05) still bottoms out at the 1s budget floor.
        # wall_clock_deadline() is the SAME formula (see its comment); the
        # guard derives its deadline through it so the two cannot drift.
        self.deadline = wall_clock_deadline(wall_clock_s=wall_clock_s)
        self.seen: set[tuple] = set()

    def check(self, iteration: int, tool_calls: list[dict]) -> str:
        """Return an abort reason, or '' to continue."""
        if iteration >= self.max_iterations:
            return f"max tool iterations ({self.max_iterations}) reached"
        if time.monotonic() > self.deadline:
            return "tool relay wall-clock budget exceeded"
        for tc in tool_calls:
            fn = tc.get("function", {})
            # PARSE the arguments before fingerprinting (audit finding 20):
            # `arguments` arrives as the model's JSON *string* — hashing the
            # raw string made '{"a":1}' and '{ "a" : 1}' different calls, so
            # whitespace/key-order re-issues evaded the abort. The
            # cross-request guard (routes_proxy.loop_guard_check) parses;
            # this one now matches it. Unparseable → fingerprint the raw
            # string (still dedupes exact repeats).
            raw_args = fn.get("arguments")
            try:
                norm_args = json.dumps(json.loads(raw_args) if isinstance(raw_args, str) else raw_args, sort_keys=True)
            except (json.JSONDecodeError, TypeError):
                norm_args = str(raw_args)
            key = (fn.get("name"), norm_args)
            if key in self.seen:
                return f"loop detected: identical call {key[0]} repeated"
            self.seen.add(key)
        return ""



def _thread_key(msgs: list[dict], key_id: Any) -> str | None:
    """Back-compat alias for canonical.thread_key (same behavior, pinned by
    tests/test_traces.py): the fingerprint implementation moved to
    core.canonical so the relay path's canonical-conversation reconciliation
    shares ONE definition with trace grouping — a fingerprint that disagreed
    between the two would split one conversation across trace threads and
    store rows."""
    return thread_key(msgs, key_id)



# ---------------------------------------------------------------------------
# streaming machinery — the relay loop runs INSIDE the SSE response, so
# thinking + intermediate text + tool events reach the client live.
# ---------------------------------------------------------------------------


async def parse_sse_payloads(byte_iter):
    """Yield parsed JSON objects from an upstream SSE byte stream.

    Handles lines straddling chunk boundaries (same reassembly rule as the
    proxy's _relay_sse) and skips non-data / [DONE] lines.

    Pre-filter (perf audit): lines carrying NONE of the fields the relay
    reads (`content` / `reasoning_content` / `tool_calls` / top-level
    `usage`) skip decode + json.loads entirely — empty keepalive deltas
    (`{"delta":{}}`) and comment/heartbeat lines are the no-op majority of
    a chatty stream. A naive `"tool_calls"`-only filter would silently drop
    final-answer content deltas, so all four markers gate the fast path.
    """
    interesting = (b'"tool_calls"', b'"usage"', b'"content"', b'"reasoning_content"')
    buf = bytearray()
    async for chunk in byte_iter:
        buf += chunk
        while (idx := buf.find(b"\n")) != -1:
            line = bytes(buf[:idx])
            del buf[: idx + 1]
            if not any(marker in line for marker in interesting):
                continue
            text = line.decode("utf-8", errors="replace").strip()
            if not text.startswith("data:"):
                continue
            payload = text[5:].strip()
            if payload == "[DONE]":
                continue
            try:
                yield json.loads(payload)
            except json.JSONDecodeError:
                continue
    if buf:
        if not any(marker in buf for marker in interesting):
            return
        text = buf.decode("utf-8", errors="replace").strip()
        if text.startswith("data:"):
            payload = text[5:].strip()
            if payload and payload != "[DONE]":
                try:
                    yield json.loads(payload)
                except json.JSONDecodeError:
                    pass


async def stream_model_call(resp):
    """Consume one upstream chat stream (an httpx2 streaming Response); yield
    typed events: ('reasoning', text) | ('content', text) | ('usage', dict) |
    ('tool_calls', [assembled]) — tool_call argument fragments are assembled
    across deltas (vLLM streams them in pieces).
    """
    acc: dict[int, dict] = {}
    usage = None
    async for obj in parse_sse_payloads(resp.aiter_bytes()):
        choices = obj.get("choices") or []
        delta = (choices[0].get("delta") or {}) if choices else {}
        if delta.get("reasoning_content"):
            yield ("reasoning", delta["reasoning_content"])
        if delta.get("content"):
            yield ("content", delta["content"])
        for tcd in delta.get("tool_calls") or []:
            idx = tcd.get("index", 0)
            slot = acc.setdefault(
                idx,
                {
                    "id": "",
                    "type": "function",
                    "function": {"name": "", "arguments": ""},
                },
            )
            if tcd.get("id"):
                slot["id"] = tcd["id"]
            fn = tcd.get("function") or {}
            if fn.get("name"):
                slot["function"]["name"] += fn["name"]
            if fn.get("arguments"):
                slot["function"]["arguments"] += fn["arguments"]
        if isinstance(obj.get("usage"), dict):
            usage = obj["usage"]
    if usage is not None:
        yield ("usage", usage)
    if acc:
        yield ("tool_calls", [acc[i] for i in sorted(acc)])


def sse_event(obj: dict) -> bytes:
    return b"data: " + json.dumps(obj).encode() + b"\n\n"


def _estimate_relay_tokens(msgs: list[dict], max_tokens: int | None = None) -> int:
    """Token estimate for one relay model call (same len//4 heuristic as the
    proxy's _estimate_tokens). The settle fallback when an upstream omits
    stream usage — an estimate beats no row (budgets are ledger-derived)."""
    prompt = sum(len(str(m.get("content") or "")) // 4 for m in msgs)
    return prompt + (int(max_tokens) if max_tokens else 1024)


async def relay_stream(
    messages: list[dict],
    grants: set[tuple[str, str]],
    catalog: list[dict],
    stream_once: Callable[[list[dict], list[dict] | None], Any],
    execute: Callable[[str, str, dict], Awaitable[tuple[bool, Any]]],
    request_id: str,
    model: str,
    key_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
    # the KEY's configured off-peak base (profile fraction ?? env default);
    # effective() applies the CURRENT demand state at each settle moment
    key_fraction: float = 1.0,
    started: float | None = None,
    estimate: Callable[[list[dict]], int] | None = None,
    max_tokens: int | None = None,
    client_tools: list[dict] | None = None,
    # Cross-request loop guard for CLIENT (handback) calls: the executor guard
    # never sees handback-path calls, so a harness-side loop repeating the same
    # client call is invisible to it. Same fingerprint semantics, distinct
    # server namespace ("client"). None = unguarded (unit tests).
    client_loop_guard: Callable[[str, dict], str | None] | None = None,
    # Stream thinking/reasoning deltas to the CLIENT. Default ON (the chat
    # console renders them live). Clients whose agent loop re-appends whole
    # reasoning blocks per cycle (DSH 2026-09-18: one stored event grew to
    # 6,630 copies of one line) opt OUT with X-PCAI-Stream-Thinking: 0 —
    # nothing accumulates, nothing amplifies; steps stay fully traced and
    # the console's chips still render.
    stream_thinking: bool = True,
    # OpenAI stream_options.include_usage: when the client asked (or the
    # routes layer decides), the standard empty-choices usage chunk rides
    # before [DONE] with the LAST upstream usage verbatim (details intact).
    include_usage: bool = False,
    # Mid-relay failover chain: (model_name, stream_once) pairs from the
    # profile ordering. When the mid-loop budget check exhausts the current
    # model, the relay continues on the first fallback whose budget has
    # headroom instead of ending the turn answerless.
    fallbacks: list[tuple[str, Callable]] | None = None,
    failover_note: str = "",
    # Phase 2A handback-continuation linkage (ROADMAP §7): when the routes
    # layer recognized this request as the client ANSWERING a previous
    # handback (canonical "extend" whose new tail is only the assistant
    # tool_calls message + its role=tool results), it passes
    # {"tool_call_ids": [...], "result_count": N} here so the relay labels
    # the turn as a CONTINUATION of that conversation's relay loop (a
    # linkage log + "continuation": true on the handback event) instead of a
    # fresh one. None (default) = byte-for-byte today's behavior. Known
    # phase-B boundary: the loop guards, the bounded model-call retry and
    # the wall clock stay PER-REQUEST — a continuation does not resume the
    # previous request's iteration counter, retry budget, or deadline; full
    # cross-request loop accounting is the next wave's work.
    continuation: dict | None = None,
    # Per-turn loop accounting (2026-09-25, supersedes the wave-C
    # per-conversation accounting): iterations_prior is the iteration count
    # the CURRENT TURN's prior handback-continuation requests already spent
    # (the routes layer zeroes it on any fresh user turn — the cap bounds
    # ONE turn's relay loop, and a new question starts a new budget); this
    # request's guard starts there, so a handback→result→handback cycle
    # within one turn cannot reset the cap per request and burn unbounded
    # model calls under one logical turn.
    iterations_prior: int = 0,
    # conversation_state(thread_ref, final_messages, iterations_final):
    # called ONCE at loop end (handback terminals) with the FULL final
    # relay_msgs list AND the iteration count the loop reached. The routes
    # layer slices the AWAITING-CLIENT tail off (everything beyond the
    # reconciled input list — the assistant tool_calls message + any
    # in-loop-executed results) and persists that as the pending column;
    # storing the full list would duplicate the prefix and break the
    # result-only continuation's shape check (2026-09-25 live report: the
    # X-PCAI-Continue merge verified a tail that began with the re-appended
    # system message). The routes layer knows the input boundary (it built
    # the relayed list); the relay does not — hence the full list here and
    # the slice there. The iteration count rides the same flush (per-turn
    # loop accounting). None = no persistence (unit tests). Failures are
    # swallowed (logged): state persistence must never break the turn.
    conversation_state: Callable[[str, list[dict], int], Awaitable[None]] | None = None,
    # Phase 2 wave B (ROADMAP §7 — client tool-server, SPIKE behind the
    # default-off flag): the executor for calls routed to the "client"
    # pseudo-server (the key's registered local tools — bash/edit served
    # over the long-poll dispatch channel, core.clientside). When set,
    # a model call naming `client__<tool>` is DISPATCHED to the client and
    # the result returns INTO the loop — the stream continues to a final
    # answer instead of ending in finish_reason=tool_calls. None (default,
    # flag off) = byte-for-byte today's behavior: `client__<name>` was
    # never offered before this wave, so the legacy unknown-tool path
    # already handled it.
    client_tool_execute: Callable[[str, str, dict], Awaitable[tuple[bool, Any]]] | None = None,
    # Wave B2: the relay's wall-clock DEADLINE (a monotonic instant, from
    # toolrelay.wall_clock_deadline()). The routes layer computes it ONCE and
    # passes it BOTH here and into the client-tool executor, so a dispatch's
    # bounded wait is clamped against exactly the budget this loop runs under
    # (identical formula, zero drift — see the helper's comment). None (every
    # pre-B2 caller) = the guard computes it internally, byte-for-byte
    # unchanged.
    wall_clock_deadline: float | None = None,
    # RFC-PER-KEY-STREAM-EVENTS: emit the in-stream {"pcai": …} telemetry
    # events at all. TRUE (default) = today's bytes, every existing consumer
    # (console, DSH) unchanged. FALSE = a byte-clean OpenAI-spec stream —
    # strict validating clients (opencode @ai-sdk/openai-compatible,
    # clearwing openai-py >= 2.45) abort on a {"pcai": …} data: line (it
    # carries neither choices nor error, the entire chunk contract). Both
    # flags coexist: stream_thinking gates the COMPLIANT reasoning_content
    # deltas; this gates the NON-COMPLIANT {"pcai": …} lines. Resolved
    # per-request at the routes layer (key default + X-PCAI-Stream-Events).
    stream_pcai_events: bool = True,
):
    """The streaming relay loop — yields SSE bytes for the client.

    stream_once(msgs, tools) is an async context manager factory yielding the
    typed events of ONE model call (see stream_model_call). Intermediate
    thinking streams LIVE as pcai events; intermediate content is buffered
    and emitted as a per-iteration event (we only learn an iteration was
    intermediate when its tool_calls arrive); the FINAL answer's content is
    emitted as a normal OpenAI delta once its stream completes.

    Ledger contract (module docstring): EVERY model call is settled —
    `settle(key_id, model, "chat", prompt, completion, ok|error, latency,
    request_id)` with the observed upstream usage, falling back to the token
    estimate when the upstream omits it. Each settle stamps the row with
    offpeak.fraction() read AT the settle moment (off-peak billing: a
    mid-loop flip is honored per iteration; 1.0 when the feature is off).
    Tool calls stay settled by the routes-layer executor as 0-token `mcp:*`
    rows (no double-settle here).

    When `key_id` is set, a budget check also runs before each subsequent
    model call: mid-loop exhaustion stops the loop cleanly, settles a
    `budget_exceeded` marker row for what was already spent in this relay
    (the per-call `ok` rows remain the budget source of truth; budgets SUM
    only ok/error statuses, so the marker is reporting-visible without
    double-counting), and closes the SSE stream with a guard event.
    Unit tests that drive the loop without a key pass key_id=None and get
    neither settle nor budget checks.

    Agent traces (0.3.x observability wave, ADDITIVE): when `key_id` is
    set, one best-effort trace row rides every settle — phase='chat' per
    model iteration (same status the settle writes: ok | error |
    budget_exceeded) and phase='tool' per tool call (ok | error | denied |
    rate_limited). `user_id` threads through from the caller's key record
    for attribution. A trace write failure never affects the stream
    (traces.record swallows + logs); every event, guard and settle here is
    unchanged.

    Mixed-mode relay: `client_tools` (the caller's OWN OpenAI tools — a
    harness's bash/edit/read) are merged UNDER the MCP catalog at every model
    call, and a model call naming a client tool HANDBACKS: the stream ends
    with standard OpenAI tool_calls deltas so the client executes it in its
    own environment and replays the result on the next request (a fresh relay
    turn). Mixed batches (client + cloud calls in one turn) hand back WHOLE
    with cloud names REWRITTEN to the client's twins (twin_rewrite) — the
    client executes everything and every call pairs with a proper role=tool
    result in the replayed history; gateway-only calls (no client twin)
    execute in-cluster and their results are injected as content so they
    survive the handback. Thinking and tool steps are dual-emitted as native
    `reasoning_content` deltas so OpenAI-compatible clients (opencode et al.)
    render the loop live; the `{"pcai": ...}` events remain for the console.

    Handback continuations (§7 phase 2A): the replayed request after a
    handback still arrives as a fresh HTTP request carrying the full
    history — the gateway cannot change that — but when `continuation` is
    set the RELAY's identity carries across the boundary: the turn is logged
    as continuing the conversation's relay loop (same stable `thread_id`,
    which was already per-conversation) and any handback this request
    produces is emitted with `"continuation": true` in its pcai event, so
    one logical conversation reads as one linked loop in the traces/console
    instead of N disconnected requests. Per-request loop accounting
    (iterations, wall clock, retries) intentionally does NOT carry — see
    the parameter comment.
    """
    if continuation and not continuation.get("tool_call_ids"):
        # Boundary normalization: a shaped-empty dict ({"tool_call_ids": [],
        # "result_count": 0} — detect_continuation's NEGATIVE info shape) is
        # truthy but is not a continuation. Only an actually-detected one
        # (>=1 answered call id) may label the turn.
        continuation = None
    relay_msgs = list(messages)
    # Conversation fingerprint for trace grouping — one long multi-turn
    # harness session = one trace thread (see _thread_key).
    thread_id = _thread_key(relay_msgs, key_id)
    if continuation:
        # §7 phase 2A linkage: this request continues a handback loop, it is
        # not a fresh conversation. Log-only here (the trace dimension needs
        # no new rows — thread_id already groups the conversation, and
        # iteration numbers stay per-request until phase B).
        log.info(
            "toolrelay: handback continuation thread=%s answering tool_calls=%s results=%d",
            thread_id,
            ",".join(str(x) for x in (continuation.get("tool_call_ids") or [])) or "-",
            int(continuation.get("result_count") or 0),
        )
    # Wave B2: when the caller passed an explicit deadline (routes_proxy
    # computes ONE deadline for both the guard and the client-tool executor —
    # see the wall_clock_deadline param comment), convert it back to the
    # remaining wall clock the guard applies verbatim (the internal clamp
    # re-adds the same constants, so the instant is reproduced exactly).
    guard = RelayGuard(
        settings.tool_relay_max_iterations,
        settings.tool_relay_wall_clock_s
        if wall_clock_deadline is None
        else max(0.0, wall_clock_deadline - time.monotonic()),
    )
    tools = tools_payload(catalog) if grants else []
    # mixed-mode: the caller's OWN tools ride along under the MCP catalog —
    # a model call that names one hands back to the client (partition below).
    # Membership is by EXACT offered name, which also covers harness MCP
    # conventions ("mcp__<server>__<tool>", opencode/DSH-style): parse_tool_name
    # strips that prefix, so a pure name-shape partition would hijack the
    # client's own MCP servers into the gateway ACL and deny them.
    #
    # CLOUD SHADOWING (shadow_client_mcp): client MCP twins of granted
    # in-cluster tools are dropped from the OFFER — the model calls the
    # in-cluster variant and skips the client's VPN hop. client_names keeps
    # the FULL client set, so a history-imitating call under the old name
    # handbacks (correct, just slower) instead of erroring. twin_rewrite is
    # the reverse map: on a MIXED batch the cloud names are rewritten back to
    # the client's twins so the CLIENT executes the whole batch and every
    # call gets a proper role=tool result in the replayed history.
    offered_client, twin_rewrite = shadow_client_mcp(client_tools or [], catalog)
    merged_tools = tools + offered_client
    client_names = {t.get("function", {}).get("name", "") for t in (client_tools or [])}
    # Per-turn baseline (2026-09-25): a handback continuation resumes the
    # TURN's budget (not its request-scoped state — traces, retries stay per
    # request); a fresh user turn arrives with iterations_prior=0 (the
    # routes layer decides — canonical.py owns the turn-boundary shape
    # check). tool_relay_max_iterations bounds one turn's relay work.
    iteration = max(0, int(iterations_prior or 0))
    relay_start = started if started is not None else time.monotonic()
    spent_prompt = 0
    spent_completion = 0
    # the LAST upstream usage dict (verbatim) — re-emitted as the OpenAI
    # standard usage chunk when the client asked for it
    # (stream_options.include_usage). The relay path used to flatten usage
    # into the pcai model_usage event only, starving OpenAI-compatible
    # clients (DSH's context meter) of the detail they consume natively.
    last_usage: dict | None = None
    # Per-TURN retry budget for bounded model calls that expired on timeout
    # (tool_relay_call_retries; 0 = today's behavior byte-for-byte). Spans
    # EVERY iteration of the loop below — worst case stays bounded — and each
    # retry is paid out of the REMAINING wall clock (never new budget), so
    # the ingress clean-close guarantee still holds.
    total_retries = max(0, int(settings.tool_relay_call_retries))
    retries_left = total_retries
    # Zero-completion insurance (review §4c): ONE retry per REQUEST for a
    # final answer that produced no content at all. Default-off flag; the
    # consumption is per-request so a flaky upstream can never multiply the
    # spend by more than one extra call per turn.
    zero_retry_used = False

    # Offered tools of the LAST attempted model call (single-slot cell so
    # the capture hook can record the exact tools array that went upstream
    # without threading it through every generator hop; written at ATTEMPT
    # START by the retry loop / wrap-up path, read at attempt END by the
    # settle path — same coroutine, so the slot is always current).
    _last_offered_tools: list[list[dict] | None] = [None]

    async def _trace_step(
        step_iteration: int,
        phase: str,
        server: str = "",
        tool: str = "",
        ms: int | None = None,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        status: str = "ok",
    ) -> None:
        """Best-effort agent-trace write for ONE relay step.

        Traces key on key_id (NOT NULL in the schema), so keyless runs —
        the unit-test path — write nothing: the same gate the settle
        contract uses. traces.record swallows its own failures.
        """
        if key_id is None:
            return
        await traces.record(
            request_id,
            key_id,
            user_id,
            model,
            step_iteration,
            phase,
            server=server,
            tool=tool,
            ms=ms,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            status=status,
            thread_id=thread_id,
        )

    async def _capture_prompt(
        msgs: list[dict],
        tools_arg: list[dict] | None,
        attempt: int,
    ) -> None:
        """Effective-prompt capture (ROADMAP §7 phase 2 wave C): persist what
        THIS model call will actually see. Andrew's transparency requirement —
        with the reasoning strip + canonical reconciliation + context trim in
        the path, "the conversation the user reads is not the conversation the
        model received"; this freezes the received side per call.

        STRICTLY OBSERVATIONAL + fail-safe: effective.record swallows every
        failure; a broken capture is a silent gap in the admin view, never a
        relay-turn effect. Called AFTER the upstream stream closes (from the
        settle path) — capture can never delay or gate the wire. `attempt`
        distinguishes a bounded-call timeout retry from its first try; every
        real call (iterations, retries, the wrap-up pass) gets a row."""
        if key_id is None:
            return  # keyless unit-test runs: same gate the settle contract uses
        await effective.record(
            key_id,
            thread_id,
            request_id,
            iteration,
            attempt,
            model,
            msgs,
            tools_arg,
        )

    async def _settle_model_call(
        msgs: list[dict],
        usage: dict | None,
        call_started: float,
        model_error: bool,
        call_iteration: int = 0,
        call_attempt: int = 1,
    ) -> tuple[int, int]:
        """Settle ONE model call per the documented contract; returns the
        token counts recorded. Also writes the step's agent-trace row
        (phase='chat', status matching the settle) — best-effort."""
        u = usage or {}
        prompt = int(u.get("prompt_tokens") or 0)
        completion = int(u.get("completion_tokens") or 0)
        if usage is None:
            # upstream did not report usage — fall back to the estimate
            prompt = estimate(msgs) if estimate is not None else _estimate_relay_tokens(msgs, max_tokens)
        if model_error:
            completion = 0  # a failed call completes nothing
        latency = int((time.monotonic() - call_started) * 1000)
        status = "error" if model_error else "ok"
        if key_id is not None:
            # failed upstream calls still settle (status "error", prompt
            # estimate) — same shape the plain-stream path uses for errors
            # billing_fraction is read at the settle moment: a mid-loop
            # off-peak flip is honored per iteration (1.0 when off — zero
            # behavior change with the feature disabled)
            await settle(
                key_id,
                model,
                "chat",
                prompt,
                completion,
                status,
                latency,
                request_id,
                cached_tokens=_cached_tokens(usage),
                billing_fraction=offpeak.effective_for(model, key_fraction),
            )
            # agent trace: one row per model iteration (additive, best-effort)
            await _trace_step(
                call_iteration,
                "chat",
                ms=latency,
                prompt_tokens=prompt,
                completion_tokens=completion,
                status=status,
            )
            # effective-prompt capture (additive, best-effort, strictly AFTER
            # the upstream call completed — observation never blocks the wire)
            await _capture_prompt(msgs, _last_offered_tools[0], call_attempt)
        return prompt, completion

    async def _bounded_call(msgs: list[dict], tools_arg: list[dict] | None, budget_s: float):
        """ONE model call, bounded: SSE keepalives every 15s while it runs,
        hard TimeoutError when `budget_s` elapses.

        WHY: a queued/wedged upstream previously parked the relay for the full
        600s upstream timeout per call — the wall-clock guard only evaluates
        BETWEEN calls, so it cannot preempt a stuck one, and the silent
        stretch let proxy/ingress idle timers kill the client stream (the
        "relay model appears to hang" incident). The keepalive is an SSE
        comment line (`: keepalive`) — ignored by every conforming parser.
        The budget keeps the WHOLE relay inside the ingress timeout (istio
        VS: 600s) even in the worst case.
        """
        budget_s = max(1.0, min(budget_s, settings.upstream_timeout_s))
        call_deadline = time.monotonic() + budget_s
        ait = stream_once(msgs, tools_arg).__aiter__()
        pending = asyncio.ensure_future(anext(ait))
        try:
            while True:
                now = time.monotonic()
                slice_s = min(call_deadline, now + KEEPALIVE_S) - now
                if slice_s <= 0:
                    raise TimeoutError(f"model call exceeded its {budget_s:.0f}s budget")
                done, _ = await asyncio.wait({pending}, timeout=slice_s)
                if pending not in done:
                    yield ("heartbeat", None)
                    continue
                try:
                    ev = pending.result()
                except StopAsyncIteration:
                    return
                pending = asyncio.ensure_future(anext(ait))
                yield ev
        finally:
            # Reap the pending __anext__: cancel it so the underlying stream
            # generator unwinds (its finally acloses the httpx2 response).
            # The CancelledError surfacing here is the CHILD task's — ours
            # (client disconnect) was raised at the asyncio.wait line and
            # keeps propagating after this finally.
            if not pending.done():
                pending.cancel()
            try:
                await pending
            except (asyncio.CancelledError, Exception):
                pass

    def _pcai(payload: dict[str, Any]) -> bytes:
        """pcai progress/telemetry event. SUPPRESSED ENTIRELY when
        stream_pcai_events is off (RFC-PER-KEY-STREAM-EVENTS): a
        {"pcai": …} payload on a data: line carries neither `choices` nor
        `error` — the entire OpenAI streaming chunk contract — so
        strict-validating clients (opencode, clearwing) abort the stream on
        the FIRST such line. Everything a compliant client needs already
        arrives via spec-clean channels: reasoning deltas
        (reasoning_content), usage (the standard include_usage chunk),
        errors ({"error": …} envelope), termination ([DONE]). Single gate:
        every emitter below routes through this helper so the OFF mode is
        provably clean (a missed site would leak a non-spec line).
        Keepalives are SSE comments (`: …`) — spec-clean for everyone — and
        intentionally bypass this gate."""
        if not stream_pcai_events:
            return b""
        return sse_event(payload)

    def _reasoning_delta(text: str) -> bytes:
        """Native reasoning_content delta: OpenAI-compatible clients render
        this live — dual emission with the pcai event keeps the console rich
        and the user's UI honest about what the loop is doing. SUPPRESSED
        entirely when stream_thinking is off (harness opt-out): clients that
        re-append reasoning blocks per agent-loop cycle turn this stream
        into unbounded UI/session growth — the pcai events carry everything
        they need functionally."""
        if not stream_thinking:
            return b""
        return sse_event(
            {
                "id": request_id,
                "object": "chat.completion.chunk",
                "model": model,
                "choices": [{"index": 0, "delta": {"reasoning_content": text}}],
            }
        )

    def _tc_delta(tc: dict, idx: int, final_name: str | None = None) -> bytes:
        """One assembled tool_call as a complete delta fragment. Clients
        reassemble fragments by index; a single full fragment assembles
        trivially (and vLLM-style assemblers accept it). `final_name`
        overrides the name for twin-rewritten handbacks (cloud name -> the
        client's own name)."""
        fn = tc.get("function", {})
        return sse_event(
            {
                "id": request_id,
                "object": "chat.completion.chunk",
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": idx,
                                    "id": tc.get("id", ""),
                                    "type": "function",
                                    "function": {
                                        "name": final_name or fn.get("name", ""),
                                        "arguments": fn.get("arguments", ""),
                                    },
                                }
                            ]
                        },
                    }
                ],
            }
        )

    if failover_note:
        yield _reasoning_delta(f"\n[relay] {failover_note}\n")

    done_state: list[dict] | None = None

    async def _flush_conversation_state(iterations_final: int) -> None:
        """Handback terminals ONLY: persist the awaiting-client
        state — the conversation UP TO AND INCLUDING the assistant
        tool_calls message (plus any in-loop-executed results on a mixed
        batch) — together with this turn's iteration counter. This
        is exactly what the result-only continuation reads back: the
        client answers with X-PCAI-Continue + ONLY the role=tool results,
        no transcript re-send. The counter is PER-TURN (the routes layer
        zeroes it on a fresh user turn — only a detected handback
        continuation resumes it). Final-answer terminals do NOT flush (the
        store keeps the reconciled incoming list — full-history clients
        keep today's byte-stable extend semantics). Failures are swallowed
        (logged): state persistence must never break the turn."""
        nonlocal done_state
        if conversation_state is None or done_state is not None or key_id is None:
            return
        done_state = list(relay_msgs)
        try:
            await conversation_state(_thread_key(relay_msgs, key_id) or "", list(relay_msgs), int(iterations_final))
        except Exception:
            log.exception("relay: conversation-state persistence failed (turn continues)")

    while True:
        # --- mid-loop budget check: exhaustion stops the loop CLEANLY ---
        # (the pre-loop gate in routes_proxy saw only the original body's
        # estimate; iterations 2..N are new model calls that must re-gate)
        # Audit fix (fail CLOSED on gate error): a swallowed exception here
        # let a key spend unbounded tokens during a Postgres incident — the
        # gate exists precisely for iterations 2..N. Admission already
        # happened, so failing closed merely ENDS THE TURN (the same clean
        # guard-event close as exhaustion); never a client-visible crash.
        if key_id is not None and iteration > 0:
            try:
                gate = await check_budgets(key_id, model, 0)
            except Exception as exc:
                log.exception("relay: mid-loop budget check failed — ending the turn (fail closed)")
                gate = None
                gate_error = str(exc) or type(exc).__name__
            else:
                gate_error = ""
            if gate is None or not gate.ok:
                latency = int((time.monotonic() - relay_start) * 1000)
                await settle(
                    key_id,
                    model,
                    "chat",
                    spent_prompt,
                    spent_completion,
                    "budget_exceeded",
                    latency,
                    request_id,
                    billing_fraction=offpeak.effective_for(model, key_fraction),
                )
                # trace mirrors the marker settle (status budget_exceeded)
                await _trace_step(
                    iteration,
                    "chat",
                    ms=latency,
                    prompt_tokens=spent_prompt,
                    completion_tokens=spent_completion,
                    status="budget_exceeded",
                )
                # MID-RELAY FAILOVER: walk the fallback chain before ending
                # the turn answerless — the harness conversation must survive
                # a mid-task budget exhaustion. The first fallback whose
                # budget still has headroom takes over the remaining loop
                # (settles + traces stamp ITS name from here on).
                # Failover eligibility keeps the fail-closed rule: a candidate
                # whose check RAISES is skipped (log + next), not served —
                # a DB incident must not become a spending window.
                switched = False
                while fallbacks:
                    fb_model, fb_stream = fallbacks.pop(0)
                    try:
                        fb_gate = await check_budgets(key_id, fb_model, 0)
                    except Exception:
                        log.exception("relay: failover budget check failed for %s — skipping candidate", fb_model)
                        yield _reasoning_delta(f"\n[relay] {fb_model} unavailable — trying next\n")
                        continue
                    if fb_gate.ok:
                        # Near-transparent handoff: ONE compact mention that
                        # the switch happened. The why (which model ran out,
                        # which gate reason) lives in the audit trail — the
                        # budget_exceeded settle row + the trace card — not
                        # in the user's reading flow.
                        yield _reasoning_delta(f"\n[relay] failing over to {fb_model}\n")
                        model = fb_model
                        stream_once = fb_stream
                        switched = True
                        break
                    yield _reasoning_delta(f"\n[relay] {fb_model} unavailable — trying next\n")
                if not switched:
                    detail = (
                        f"budget check unavailable mid-relay ({gate_error}); turn ended fail-closed"
                        if gate is None
                        else f"budget exceeded mid-relay: {gate.reason}"
                    )
                    yield _pcai({"pcai": {"event": "guard", "detail": detail}})
                    yield b"data: [DONE]\n\n"
                    return
                # switched: the loop continues on the fallback model — the next
                # iteration's budget check gates the NEW model, and every
                # settle/trace from here stamps its name

        # ONE bounded model call, with the per-turn TIMEOUT retry: the loop
        # body runs once for the plain call; a bounded-call timeout with
        # per-turn retries left AND enough remaining wall clock re-runs it
        # (`continue`) — everything else `break`s into the code below exactly
        # as a no-retry run would. Keepalive, settle and the model_usage
        # event run PER ATTEMPT, so the timed-out attempt keeps today's error
        # settle and every retry settles as the model call it really was.
        # `attempt` labels this real upstream call (1 = first try, 2.. = the
        # bounded-call timeout retries) — recomputed at the TOP of the inner
        # loop so a `continue` retry advances it; capture context: this
        # attempt's exact offered tools (see the cell comment above).
        _last_offered_tools[0] = merged_tools or None
        while True:
            attempt = total_retries - retries_left + 1
            reasoning_acc: list[str] = []
            content_acc: list[str] = []
            tool_calls: list[dict] = []
            usage = None
            model_error = False
            call_started = time.monotonic()
            call_timed_out = ""
            settled = False

            async def _settle_this_call() -> tuple[int, int]:
                """Settle the in-flight model call EXACTLY ONCE per attempt.

                Cancellation-safe (audit fix, the _relay_sse lesson ported to
                the relay loop): a mid-relay client disconnect CANCELS this
                generator, and a plain await after the cancel point would
                raise before the ledger row is written — under-counting
                budgets by exactly the biggest prefills. The settle therefore
                runs in the finally below inside a SHIELDED scope: it
                completes even under the delivered cancellation; the pending
                CancelledError is re-raised at scope exit (the abort itself
                is preserved).
                """
                nonlocal settled
                prompt, completion = await _settle_model_call(
                    relay_msgs, usage, call_started, model_error or bool(call_timed_out), iteration, attempt
                )
                settled = True
                return prompt, completion

            try:
                async for kind, payload in _bounded_call(
                    relay_msgs, merged_tools or None, guard.deadline - time.monotonic()
                ):
                    if kind == "heartbeat":
                        yield b": keepalive\n\n"
                    elif kind == "reasoning":
                        reasoning_acc.append(payload)
                        yield _pcai({"pcai": {"event": "thinking", "iteration": iteration, "text": payload}})
                        yield _reasoning_delta(payload)
                    elif kind == "content":
                        content_acc.append(payload)
                        # STREAM LIVE: the client reads as the model writes. These
                        # pieces are between-tool narration on intermediate
                        # iterations and THE ANSWER on the final one — the native
                        # agentic shape (text, tool block, text...). The old
                        # buffer-everything design emitted the final answer as one
                        # post-generation blob ("arrives all at once").
                        yield sse_event(
                            {
                                "id": request_id,
                                "object": "chat.completion.chunk",
                                "model": model,
                                "choices": [{"index": 0, "delta": {"content": payload}}],
                            }
                        )
                    elif kind == "usage":
                        usage = payload
                        last_usage = payload
                    elif kind == "tool_calls":
                        tool_calls = payload
                    elif kind == "error":
                        # upstream 4xx/5xx surfaced by the routes-layer stream_once
                        model_error = True
            except TimeoutError as exc:
                call_timed_out = str(exc) or "model call budget exceeded"
            finally:
                # Settle in finally (the _relay_sse discipline, audit fix):
                # the happy path settles here too — the code after the
                # try/finally just reads the recorded pair. On mid-relay
                # client disconnect this generator is CANCELLED; a plain
                # await after the cancel point would skip the ledger row
                # (under-counting budgets by the biggest prefills). The
                # settle therefore runs inside a SHIELDED scope: it completes
                # even under the delivered cancellation, and the pending
                # CancelledError resumes at scope exit (the abort itself is
                # preserved). `attempt_prompt/attempt_completion` carry the
                # per-attempt pair to the code below; the spent_* totals
                # accumulate inside the scope.
                attempt_prompt = 0
                attempt_completion = 0
                with anyio.CancelScope(shield=True):
                    if not settled:
                        attempt_prompt, attempt_completion = await _settle_this_call()
                        spent_prompt += attempt_prompt
                        spent_completion += attempt_completion
            prompt, completion = attempt_prompt, attempt_completion
            # model_usage event with the usage-detail passthrough: cached
            # tokens (upstream prompt-cache hits) surface next to the two
            # ints, and the full upstream usage dict rides VERBATIM when the
            # upstream reported one — the console (and any consumer) reads
            # the details instead of re-deriving them from a flattened pair.
            mu_event: dict[str, Any] = {
                "pcai": {
                    "event": "model_usage",
                    "iteration": iteration,
                    "prompt": prompt,
                    "completion": completion,
                    "cached_tokens": _cached_tokens(usage),
                }
            }
            if isinstance(usage, dict):
                mu_event["pcai"]["usage"] = usage
            yield _pcai(mu_event)

            if not call_timed_out:
                break
            # The bounded call expired (queued/wedged upstream). Retry at most
            # `tool_relay_call_retries` times PER TURN, only while the retry
            # fits inside the REMAINING wall clock (never extends it — the
            # ingress clean-close guarantee is untouched) with at least
            # TOOL_RELAY_RETRY_MIN_BUDGET_S to run in. Only the TIMEOUT class
            # retries: a call that returned an answer (even an error answer)
            # is real work, never re-rolled — duplicate GPU spend on
            # generations that actually completed server-side is why the
            # budget is bounded at all.
            remaining = guard.deadline - time.monotonic()
            retry_budget = min(remaining, settings.upstream_timeout_s)
            if retries_left > 0 and retry_budget >= TOOL_RELAY_RETRY_MIN_BUDGET_S:
                retries_left -= 1
                detail = (
                    f"model call timed out — retrying "
                    f"({total_retries - retries_left} of {total_retries} retries left, "
                    f"{retry_budget:.0f}s budget)"
                )
                yield _pcai({"pcai": {"event": "guard", "detail": detail}})
                yield _reasoning_delta(f"\n[relay] {detail}\n")
                continue
            break

        if call_timed_out:
            # A bounded model call expired (queued/wedged upstream): close the
            # stream CLEANLY — guard event, visible error chunk, [DONE] — so
            # the client never stares at a silent dead socket and the ingress
            # timeout never has to shoot the stream down mid-flight.
            yield _pcai({"pcai": {"event": "guard", "detail": call_timed_out}})
            yield _reasoning_delta(f"\n[relay] {call_timed_out}\n")
            yield sse_event(
                {
                    "id": request_id,
                    "object": "chat.completion.chunk",
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": "(model error: relay model call timed out)"},
                            "finish_reason": "stop",
                        }
                    ],
                }
            )
            yield b"data: [DONE]\n\n"
            return

        if not tool_calls:
            # --- zero-completion insurance (review §4c, flag-gated) -----------
            # The FINAL answer iteration came back with NO content, no
            # tool_calls, zero completion tokens: the model burned GPU time
            # and answered nothing. ONE identical retry (never streamed mid-
            # answer — the retry happens BEFORE anything about this empty
            # answer reaches the client), BOTH attempts settled (the first
            # was real spend), shared request_id. Plain (non-relay) streams
            # are EXCLUDED at the routes layer — buffering breaks
            # transparency; here the relay owns assembly, so the retry is a
            # bounded internal re-roll of one model call.
            zero_completion = (
                settings.zero_completion_retry
                and not zero_retry_used
                and not content_acc
                and usage is not None
                and int((usage or {}).get("completion_tokens") or 0) == 0
            )
            if zero_completion:
                zero_retry_used = True
                log.info(
                    "relay: zero-completion answer on model=%s iteration=%d — one identical retry (request_id=%s)",
                    model,
                    iteration,
                    request_id,
                )
                # re-run THIS iteration's model call once; its events replace
                # the empty answer (nothing was emitted yet for it)
                _last_offered_tools[0] = merged_tools or None
                retry_started = time.monotonic()
                retry_usage: dict | None = None
                try:
                    async for kind, payload in _bounded_call(
                        relay_msgs, merged_tools or None, guard.deadline - time.monotonic()
                    ):
                        if kind == "heartbeat":
                            yield b": keepalive\n\n"
                        elif kind == "reasoning":
                            reasoning_acc.append(payload)
                            yield _pcai({"pcai": {"event": "thinking", "iteration": iteration, "text": payload}})
                            yield _reasoning_delta(payload)
                        elif kind == "content":
                            content_acc.append(payload)
                            yield sse_event(
                                {
                                    "id": request_id,
                                    "object": "chat.completion.chunk",
                                    "model": model,
                                    "choices": [{"index": 0, "delta": {"content": payload}}],
                                }
                            )
                        elif kind == "usage":
                            retry_usage = payload
                            last_usage = payload
                        elif kind == "tool_calls":
                            tool_calls = payload
                        elif kind == "error":
                            model_error = True
                except TimeoutError as exc:
                    call_timed_out = str(exc) or "model call budget exceeded"
                # settle the RETRY as its own real model call (both attempts
                # in the ledger; the empty first attempt already settled above)
                if not call_timed_out:
                    usage = retry_usage
                    rp, rc = await _settle_model_call(
                        relay_msgs, usage, retry_started, model_error, iteration, attempt + 1
                    )
                    spent_prompt += rp
                    spent_completion += rc
                    mu = {
                        "pcai": {
                            "event": "model_usage",
                            "iteration": iteration,
                            "retry": "zero_completion",
                            "prompt": rp,
                            "completion": rc,
                            "cached_tokens": _cached_tokens(usage),
                        }
                    }
                    if isinstance(usage, dict):
                        mu["pcai"]["usage"] = usage
                    yield _pcai(mu)
                else:
                    # the retry itself timed out: fall through to today's
                    # timeout close (guard event + error chunk + [DONE])
                    yield _pcai({"pcai": {"event": "guard", "detail": call_timed_out}})
                    yield _reasoning_delta(f"\n[relay] {call_timed_out}\n")
                    yield sse_event(
                        {
                            "id": request_id,
                            "object": "chat.completion.chunk",
                            "model": model,
                            "choices": [
                                {
                                    "index": 0,
                                    "delta": {"content": "(model error: relay model call timed out)"},
                                    "finish_reason": "stop",
                                }
                            ],
                        }
                    )
                    yield b"data: [DONE]\n\n"
                    return
            if not tool_calls:
                relay_msgs.append({"role": "assistant", "content": "".join(content_acc)})
                # the answer already streamed live piece-by-piece — just close
                yield sse_event(
                    {
                        "id": request_id,
                        "object": "chat.completion.chunk",
                        "model": model,
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    }
                )
            if include_usage and last_usage:
                # OpenAI-standard final usage chunk (choices: []) — the
                # non-relay proxy path already forwards the upstream's
                # verbatim; the relay path synthesized none, starving
                # OpenAI-compatible clients (DSH's context meter) of the
                # per-call consumption they consume natively.
                yield sse_event(
                    {
                        "id": request_id,
                        "object": "chat.completion.chunk",
                        "model": model,
                        "choices": [],
                        "usage": last_usage,
                    }
                )
            yield b"data: [DONE]\n\n"
            return

        relay_msgs.append(
            {
                "role": "assistant",
                "content": "".join(content_acc) or None,
                "tool_calls": tool_calls,
            }
        )
        if content_acc:
            # intermediate text: per-iteration collapsible event
            yield _pcai({"pcai": {"event": "iteration", "iteration": iteration, "text": "".join(content_acc)}})

        # mixed-mode partition: harness tools belong to the CLIENT — but only
        # when the client actually OFFERED tools. Partition by offered-name
        # membership FIRST (covers "mcp__<server>__<tool>" client MCP servers,
        # which parse_tool_name would otherwise treat as gateway-shaped and
        # ACL-deny — the live relay-hijack incident), then by name shape for
        # plain client tools. With client_tools empty, a non-MCP name is a
        # model hallucination: keep the legacy in-loop error path so the
        # model self-corrects (no bogus tool_call handed to a client that
        # never asked for tools).
        def _is_client_call(tc: dict) -> bool:
            # Handback ONLY for names the client actually OFFERED. A parse-None
            # name that was never offered is a model hallucination: it goes to
            # the in-loop unknown-tool error path and self-corrects (handing a
            # bogus call to a client that never offered it would just error
            # there). client_names is empty without client_tools, so the
            # no-client-tools case routes everything to the gateway paths.
            name = tc.get("function", {}).get("name", "")
            # Wave B dispatch: `client__<tool>` calls with a dispatch executor
            # armed NEVER hand back — they execute via the reverse channel and
            # the loop continues (the wave's whole point). Parse-first so the
            # pseudo-server namespace is matched exactly, never by prefix luck.
            if client_tool_execute is not None:
                parsed_dispatch = parse_tool_name(name)
                if parsed_dispatch is not None and parsed_dispatch[0] == clientside.CLIENT_PSEUDO_SERVER:
                    return False
            return name in client_names

        client_calls = [tc for tc in tool_calls if _is_client_call(tc)]
        mcp_calls = [tc for tc in tool_calls if not _is_client_call(tc)]

        # NATIVE WIRE VISIBILITY: re-emit the model's tool_calls as native
        # deltas (no finish_reason — the turn's real finish_reason comes at
        # the end). WHY: when every tool executes in-cluster, an OpenAI-
        # shaped client (the harness) sees NOTHING between its request and
        # the answer — a silent gap where its whole agent loop used to be
        # visible ("I see nothing in the harness", 2026-09-18). The deltas
        # are informational: clients reassemble them for display; the relay
        # still owns execution and the turn's one true finish_reason.
        if settings.relay_stream_wire_tools:
            for _i, _tc in enumerate(tool_calls):
                yield _tc_delta(_tc, _i)

        # CLIENT-side loop guard (see signature): a handback call whose exact
        # fingerprint repeats across requests gets its refusal fed to the model
        # IN-LOOP instead of another blind handback — the loop turns into a
        # self-correction signal. mcp_summaries collects these so a mixed
        # batch's handback still carries them; a pure-client batch that is
        # FULLY guarded falls through to the normal loop (no handback at all).
        mcp_summaries: list[str] = []
        if client_loop_guard is not None and client_calls:
            still_client: list[dict] = []
            for tc in client_calls:
                fn = tc.get("function", {})
                try:
                    call_args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    call_args = {"_raw": fn.get("arguments", "")}
                reason = client_loop_guard(fn.get("name", ""), call_args)
                if reason is None:
                    still_client.append(tc)
                    continue
                relay_msgs.append({"role": "tool", "tool_call_id": tc.get("id", ""), "content": reason})
                yield _pcai({"pcai": {"event": "guard", "detail": reason}})
                yield _reasoning_delta(f"\n[relay] {reason}\n")
                mcp_summaries.append(f"[tool result] {fn.get('name', '')} → {reason}\n")
                await _trace_step(
                    iteration,
                    "tool",
                    server="client",
                    tool=str(fn.get("name", "")),
                    status="denied",
                )
            client_calls = still_client

        # Pure-client batch: nothing server-side to run — handback NOW, the
        # client's own loop budget takes over (no guard needed: no relay work).
        # Handbacks TRACE as phase='tool' server='client' rows: a harness
        # running its OWN tool loop (DSH et al.) turns every request into
        # model-call + handback, and without these rows the trace card
        # degenerates to "1 step, 0 tools" — hiding what the agent actually
        # asked to do. ms stays NULL: the client executes it later, the
        # wall-clock belongs to the client.
        if client_calls and not mcp_calls:
            for tc in client_calls:
                await _trace_step(
                    iteration,
                    "tool",
                    server="client",
                    tool=str(tc.get("function", {}).get("name", "")),
                    status="handed_back",
                )
            handback_event: dict[str, Any] = {
                "pcai": {
                    "event": "handback",
                    "tools": [tc.get("function", {}).get("name", "") for tc in client_calls],
                    "iteration": iteration,
                    # Wave C session ref: the client can answer THIS handback
                    # with "result of call X" + this thread ref instead of the
                    # full transcript (the gateway owns the canonical list).
                    # Absent when the request has no fingerprint (no key or
                    # no first user message).
                    "thread": (_thread_key(relay_msgs, key_id) if key_id is not None else None),
                }
            }
            if continuation:
                # §7 phase 2A: this request itself ANSWERED a prior handback —
                # label the new handback as the loop continuing.
                handback_event["pcai"]["continuation"] = True
            yield _pcai(handback_event)
            # NOTE: no reasoning delta for the handback notice — it used to
            # stream "[relay] handing back to client tools: ..." as thinking
            # text, and the harness (DSH) re-renders/re-appends the reasoning
            # block once per agent-loop cycle, amplifying one line into
            # thousands in the UI and the session store (2026-09-18 storm:
            # one stored event carried 6,630 copies). The information is
            # fully carried by the `handback` event above (rendered as tool
            # chips) and the tool-call deltas below.
            for idx, tc in enumerate(client_calls):
                yield _tc_delta(tc, idx)
            yield sse_event(
                {
                    "id": request_id,
                    "object": "chat.completion.chunk",
                    "model": model,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
                }
            )
            await _flush_conversation_state(iteration + 1)
            yield b"data: [DONE]\n\n"
            return

        abort = guard.check(iteration, tool_calls)
        if abort:
            yield _pcai({"pcai": {"event": "guard", "detail": abort}})
            yield _reasoning_delta(f"\n[relay] {abort}\n")
            # Role hygiene (2026-09-24 live report) + protocol compliance
            # (audit finding 13): EVERY tool_call id in the aborted batch
            # gets a role=tool result — an assistant message with N
            # tool_calls followed by fewer than N role=tool messages is
            # invalid OpenAI history and strict upstreams 400 the wrap-up
            # call. The FIRST call carries the halt reason; the rest are
            # answered "halted" (the model never gets to act on them
            # anyway — the loop is over).
            _all_ids = [tc.get("id", "") for tc in tool_calls if tc.get("id")]
            for _i, _tid in enumerate(_all_ids):
                relay_msgs.append(
                    {
                        "role": "tool",
                        "tool_call_id": _tid or f"relay-guard-{_i}",
                        "content": (
                            f"Tool use halted: {abort}. Answer from what you have."
                            if _i == 0
                            else f"Tool use halted before this call ran: {abort}."
                        ),
                    }
                )
            # the wrap-up pass is its OWN real model call: toolless (tools=None
            # into the capture cell) and labeled as the next attempt of this
            # iteration for the capture row.
            _last_offered_tools[0] = None
            wrap_attempt = total_retries - retries_left + 1
            wrap_started = time.monotonic()
            wrap_usage = None
            wrap_error = False
            wrap_timed_out = False
            _wrap_chunks: list[str] = []
            try:
                async for kind, payload in _bounded_call(
                    relay_msgs, None, max(30.0, guard.deadline - time.monotonic())
                ):
                    if kind == "heartbeat":
                        yield b": keepalive\n\n"
                    elif kind == "reasoning":
                        yield _pcai({"pcai": {"event": "thinking", "iteration": iteration + 1, "text": payload}})
                        yield _reasoning_delta(payload)
                    elif kind == "content":
                        # Protocol compliance (audit finding 13): buffer the
                        # wrap-up answer and emit ONE terminal chunk — a
                        # finish_reason on EVERY delta treated the first
                        # chunk as terminal for strict clients, truncating
                        # the answer; and per-delta assistant messages
                        # fragmented the canonical history.
                        _wrap_chunks.append(payload)
                    elif kind == "usage":
                        wrap_usage = payload
                    elif kind == "error":
                        wrap_error = True
            except TimeoutError:
                wrap_timed_out = True
            if _wrap_chunks:
                _wrap_text = "".join(_wrap_chunks)
                relay_msgs.append({"role": "assistant", "content": _wrap_text})
                yield sse_event(
                    {
                        "id": request_id,
                        "object": "chat.completion.chunk",
                        "model": model,
                        "choices": [
                            {"index": 0, "delta": {"content": _wrap_text}, "finish_reason": "stop"}
                        ],
                    }
                )
            wrap_prompt, wrap_completion = await _settle_model_call(
                relay_msgs, wrap_usage, wrap_started, wrap_error or wrap_timed_out, iteration + 1, wrap_attempt
            )
            spent_prompt += wrap_prompt
            spent_completion += wrap_completion
            if wrap_timed_out:
                yield _pcai({"pcai": {"event": "guard", "detail": "wrap-up model call timed out"}})
                yield _reasoning_delta("\n[relay] wrap-up model call timed out\n")
            yield b"data: [DONE]\n\n"
            return

        # Execute the gateway-only MCP calls (pure-MCP turn: the whole batch).
        # Twin-rewritable calls are NOT executed here — on a mixed batch they
        # are rewritten to the client's own names and handed back, so the
        # CLIENT executes them and every call in the replayed history pairs
        # with a proper role=tool result. (The old execute+content-inject
        # design made the history lie about what the model called — observed
        # live as an infinite bash/logsearch/edit re-issue loop.) Summaries
        # of in-loop executions are injected as content for mixed batches:
        # relay memory dies with this stream, so those results must survive
        # into the client's replayed history.
        exec_idx: set[int] = set()
        rewrite_by_idx: dict[int, str] = {}
        for i, tc in enumerate(tool_calls):
            if _is_client_call(tc):
                continue
            # Twin-rewrite applies ONLY in mixed batches: a client call in the
            # batch forces the turn to end at a handback, so cloud twins are
            # rewritten for protocol-true client execution. A PURE cloud batch
            # (no client calls) must execute IN-CLUSTER — that is the entire
            # point of shadowing; rewriting here sent every shadowed call over
            # the client's VPN and starved the executor (and the loop guard)
            # entirely. Found live: pure-cloud turns handed back the shadowed
            # calls and the loop guard never armed.
            twin = twin_rewrite.get(tc.get("function", {}).get("name", "")) if client_calls else None
            if twin:
                rewrite_by_idx[i] = twin
            else:
                exec_idx.add(i)

        mcp_summaries.clear()  # exec-batch summaries start empty (mypy: no redef)
        for i, tc in enumerate(tool_calls):
            if i not in exec_idx:
                continue
            fn = tc.get("function", {})
            fn_name = str(fn.get("name", ""))
            parsed = parse_tool_name(fn_name)
            if parsed is None:
                # parse-None (no separator): a harness-prefixed ghost name
                # whose suffix matches a GRANTED gateway tool resolves to
                # that twin — never guesses (alias returns None otherwise).
                parsed = alias_granted_gateway_name(fn_name, catalog)
            elif parsed not in grants:
                # parsed-but-ungranted: the double-prefix shape
                # (mcp__<connection>__<server>__<tool>) parses to a BOGUS
                # server after the single mcp__ strip — re-resolve by suffix
                # before denying, so history replay under the harness's
                # spelling reaches the tool the offer actually showed.
                aliased = alias_granted_gateway_name(fn_name, catalog)
                if aliased is not None:
                    parsed = aliased
            started = time.monotonic()
            if parsed is None:
                granted_hint = ""
                if grants and TOOLSEP in fn_name:
                    # the call is gateway-SHAPED but refused: point the model
                    # at what it may call instead of a bare "unknown tool"
                    granted_hint = " — granted tools: " + ", ".join(sorted(f"{s}{TOOLSEP}{t}" for s, t in grants))
                relay_msgs.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.get("id", ""),
                        "content": f"error: unknown tool {fn_name!r}{granted_hint}",
                    }
                )
                yield _pcai(
                    {
                        "pcai": {
                            "event": "tool_error",
                            "tool": fn_name,
                            "detail": "not an MCP tool name",
                            "args": _preview(fn.get("arguments") or "{}"),
                        }
                    }
                )
                await _trace_step(
                    iteration,
                    "tool",
                    tool=fn_name,
                    ms=int((time.monotonic() - started) * 1000),
                    status="error",
                )
                mcp_summaries.append(f"[tool result] {fn_name} → unknown tool (not an MCP name)\n")
                continue
            server, tool = parsed
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                # Audit fix (no silent {} execution): malformed model output
                # used to become EMPTY arguments and the tool executed with
                # defaults — for all-defaulted tools that is an unintended
                # action. Feed the model an explicit role=tool error instead
                # so it self-corrects (same shape as the unknown-tool path).
                err_text = (
                    f"error: arguments for {server}/{tool} were not valid JSON — "
                    "re-issue the call with a JSON object"
                )
                relay_msgs.append({"role": "tool", "tool_call_id": tc.get("id", ""), "content": err_text})
                yield _pcai(
                    {
                        "pcai": {
                            "event": "tool_error",
                            "server": server,
                            "tool": tool,
                            "detail": "arguments not valid JSON",
                            "args": _preview(fn.get("arguments") or ""),
                        }
                    }
                )
                yield _reasoning_delta(f"\n[tool] {server}__{tool} → invalid arguments JSON\n")
                await _trace_step(
                    iteration,
                    "tool",
                    server=server,
                    tool=tool,
                    ms=int((time.monotonic() - started) * 1000),
                    status="error",
                )
                mcp_summaries.append(f"[tool result] {server}__{tool} → invalid arguments JSON\n")
                continue
            # Wave B dispatch branch (flag-gated by the executor's presence):
            # the "client" pseudo-server is NOT an entry in `grants` — its
            # calls route to the client-tool dispatch executor, which carries
            # its own gate chain (loop guard, rate, settle under
            # local:<tool>, trace under server="client"). The remaining
            # wall-clock budget bounds it below exactly like an MCP call.
            if server == clientside.CLIENT_PSEUDO_SERVER and client_tool_execute is not None:
                try:
                    async with asyncio.timeout(max(1.0, guard.deadline - time.monotonic())):
                        exec_ok, content = await client_tool_execute(server, tool, args)
                except TimeoutError:
                    exec_ok, content = False, "error: client tool exceeded the relay wall-clock budget"
                ms = int((time.monotonic() - started) * 1000)
                relay_msgs.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.get("id", ""),
                        "content": _relay_content(content),
                    }
                )
                yield _pcai(
                    {
                        "pcai": {
                            "event": "tool_call",
                            "server": server,
                            "tool": tool,
                            "ok": exec_ok,
                            "ms": ms,
                            "args": args,
                            "result": _preview(content),
                        }
                    }
                )
                yield _reasoning_delta(f"\n[tool] {server}__{tool} → {'ok' if exec_ok else 'error'} ({ms}ms)\n")
                await _trace_step(
                    iteration,
                    "tool",
                    server=server,
                    tool=tool,
                    ms=ms,
                    status=_tool_trace_status(exec_ok, content),
                )
                continue
            if (server, tool) not in grants:
                relay_msgs.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.get("id", ""),
                        "content": (
                            f"denied: {server}/{tool} is not granted — granted tools: "
                            + ", ".join(sorted(f"{s}{TOOLSEP}{t}" for s, t in grants))
                            if grants
                            else f"denied: {server}/{tool} is not granted"
                        ),
                    }
                )
                yield _pcai(
                    {
                        "pcai": {
                            "event": "tool_error",
                            "server": server,
                            "tool": tool,
                            "detail": "denied by ACL",
                            "args": args,
                        }
                    }
                )
                yield _reasoning_delta(f"\n[tool] {server}__{tool} → denied by ACL\n")
                await _trace_step(
                    iteration,
                    "tool",
                    server=server,
                    tool=tool,
                    ms=int((time.monotonic() - started) * 1000),
                    status="denied",
                )
                mcp_summaries.append(f"[tool result] {server}__{tool} → denied by ACL\n")
                continue
            try:
                # Bound the tool execution to the REMAINING wall-clock budget:
                # a hung MCP server previously parked the loop for the full
                # 600s per call (timeout_s=None on the MCP session).
                async with asyncio.timeout(max(1.0, guard.deadline - time.monotonic())):
                    ok, content = await execute(server, tool, args)
            except TimeoutError:
                ok, content = False, "error: tool execution exceeded the relay wall-clock budget"
            except asyncio.CancelledError:
                raise  # the stream is being torn down — preserve the abort
            except Exception as exc:
                # Audit fix: a non-timeout executor failure (a transient DB
                # error inside the executor's rate/settle chain, an upstream
                # protocol bug) previously propagated OUT of relay_stream —
                # killing the whole SSE stream with no error chunk, no
                # [DONE], no settle, destroying an otherwise healthy turn.
                # Convert to the standard (False, text) tool-result shape so
                # the model sees a tool error and can self-correct.
                log.warning("relay: executor %s/%s failed: %s", server, tool, type(exc).__name__)
                ok, content = False, f"error: tool execution failed ({type(exc).__name__})"
            ms = int((time.monotonic() - started) * 1000)
            relay_msgs.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.get("id", ""),
                    "content": _relay_content(content),
                }
            )
            yield _pcai(
                {
                    "pcai": {
                        "event": "tool_call",
                        "server": server,
                        "tool": tool,
                        "ok": ok,
                        "ms": ms,
                        "args": args,
                        "result": _preview(content),
                    }
                }
            )
            yield _reasoning_delta(f"\n[tool] {server}__{tool} → {'ok' if ok else 'error'} ({ms}ms)\n")
            await _trace_step(
                iteration, "tool", server=server, tool=tool, ms=ms, status=_tool_trace_status(ok, content)
            )
            mcp_summaries.append(
                f"[tool result] {server}__{tool} → {'ok' if ok else 'error'} ({ms}ms)\n{_preview(content)}\n"
            )

        if client_calls:  # rewrite_by_idx implies client_calls (mixed batch)
            # Handback: client calls as-is + twin-rewritten cloud calls, in
            # the model's original batch order. The client executes them all
            # and replays proper role=tool results — protocol-true history.
            handback_calls: list[tuple[dict, str]] = []
            for i, tc in enumerate(tool_calls):
                if i in rewrite_by_idx:
                    handback_calls.append((tc, rewrite_by_idx[i]))
                elif _is_client_call(tc):
                    handback_calls.append((tc, tc.get("function", {}).get("name", "")))
            # Handback rows: same discipline as the pure-client path — the
            # CLIENT-facing name is what the client executes (a twin-rewritten
            # cloud call hands back under the client's own name).
            for _tc, final_name in handback_calls:
                await _trace_step(
                    iteration,
                    "tool",
                    server="client",
                    tool=str(final_name),
                    status="handed_back",
                )
            # NO content-delta echo of the executed MCP results on the
            # handback path: OpenAI-spec clients (DSH) render delta.content
            # VERBATIM into the visible answer, so "[tool result]
            # mm-rag__search_memory → ok … {347k of raw JSON}" landed in main
            # chat (2026-09-24 live report). The information is already
            # delivered everywhere it matters: full results ride relay_msgs
            # role=tool rows (the model), the tool_call pcai events carry
            # {args, result preview} (the console chips), _trace_step rows
            # (the trace card). Only the single consolidated iteration event
            # survives, gated by stream_pcai_events like every other site.
            if mcp_summaries:
                yield _pcai({"pcai": {"event": "iteration", "iteration": iteration, "text": "\n".join(mcp_summaries)}})
            handback_event_mixed: dict[str, Any] = {
                "pcai": {
                    "event": "handback",
                    "tools": [name for _, name in handback_calls],
                    "iteration": iteration,
                    "thread": (_thread_key(relay_msgs, key_id) if key_id is not None else None),
                }
            }
            if continuation:
                handback_event_mixed["pcai"]["continuation"] = True
            yield _pcai(handback_event_mixed)
            # NOTE: no reasoning delta here either — see the pure-client
            # handback above (DSH re-appends reasoning blocks per cycle, and
            # this line was the text it multiplied into UI storms).
            for idx, (tc, final_name) in enumerate(handback_calls):
                yield _tc_delta(tc, idx, final_name)
            yield sse_event(
                {
                    "id": request_id,
                    "object": "chat.completion.chunk",
                    "model": model,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
                }
            )
            await _flush_conversation_state(iteration + 1)
            yield b"data: [DONE]\n\n"
            return

        iteration += 1
