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
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import anyio
import httpx2

from ..config import clamp_max_tokens, settings
from . import clientside, effective, offpeak, tool_surface, traces
from .budgets import check_budgets, settle
from .canonical import thread_key

log = logging.getLogger(__name__)

TOOLSEP = "__"

# Per-turn ceiling on tool-surface discovery calls. High enough for a real
# agent turn (list, a few server lists, a couple of describes), low enough
# that a spinner cannot burn the wall clock on them.
_META_CALL_MAX = 12

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


# Reasoning-delta key spellings an OpenAI-compatible upstream may use. pi-ai
# (the client library DSH uses) accepts all three — "reasoning_content"
# (vLLM/llama.cpp), "reasoning" (many OpenAI-compatible servers) and
# "reasoning_text". The relay recognised ONLY "reasoning_content", and its SSE
# prefilter is a raw BYTE scan that runs BEFORE json.loads — so a delta under
# either other spelling was discarded without ever being parsed: the model's
# thinking was generated, billed upstream, and silently thrown away before it
# reached the client (audit 2026-10-26, CUT 1).
REASONING_KEYS = ("reasoning_content", "reasoning", "reasoning_text")


def _reasoning_text_of(delta: dict) -> str:
    """The reasoning text carried by ONE upstream delta, or "".

    First non-empty spelling wins — an upstream that emits both would
    otherwise duplicate the trace. Mirrors pi-ai's own resolution order.
    """
    for key in REASONING_KEYS:
        value = delta.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


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


def with_system_prefix(msgs: list[dict], prefix: str) -> list[dict]:
    """The message list a model call ACTUALLY sees: ``prefix`` prepended. PURE.

    WHERE IT GOES: AFTER a leading system/developer message when the client sent
    one — the client framing owns the tool protocol and must stay first — else at
    index 0. Two system messages rather than a merge: the client text stays
    byte-intact for its own hashing and prefix cache, and every chat template
    this fleet serves concatenates consecutive system turns in order.

    WHY IT IS NOT APPLIED TO relay_msgs: that list is the input boundary the
    handback flush slices at (``final_messages[:n] == relay_projection``,
    routes_proxy._persist_conversation_state). Growing it here would shift the
    boundary, skip the pending-tail write, and make X-PCAI-Continue 400 on the
    next request — the exact defect toolrelay.py:941 documents. Callers prepend
    per CALL instead, which is also what keeps the canonical store (the client
    conversation of record) free of gateway-authored text.
    """
    if not prefix:
        return msgs
    head = {"role": "system", "content": prefix}
    lead = 0
    if msgs and isinstance(msgs[0], dict) and str(msgs[0].get("role") or "") in ("system", "developer"):
        lead = 1
    return [*msgs[:lead], head, *msgs[lead:]]

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
        # Discovery calls are counted, not fingerprinted (2026-10-28): the
        # meta-tool is idempotent and REPETITIVE by design (list, then list
        # again for a server, then describe), so the identical-call abort
        # fired on legitimate discovery and killed the turn. A spinner still
        # needs bounding, so a per-turn budget replaces the fingerprint.
        self.meta_calls = 0

    def check(self, iteration: int, tool_calls: list[dict]) -> str:
        """Return an abort reason, or '' to continue."""
        if iteration >= self.max_iterations:
            return f"max tool iterations ({self.max_iterations}) reached"
        if time.monotonic() > self.deadline:
            return "tool relay wall-clock budget exceeded"
        for tc in tool_calls:
            fn = tc.get("function", {})
            if fn.get("name") == tool_surface.META_TOOL:
                # Local, idempotent discovery: never a "loop", but bounded.
                self.meta_calls += 1
                if self.meta_calls > _META_CALL_MAX:
                    return f"discovery budget exceeded ({_META_CALL_MAX} tool-surface calls)"
                continue
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

    NO FIELD PRE-FILTER (audit 2026-10-26, CUT 1). This function used to skip
    any line that did not contain one of four literal BYTE markers
    (`"tool_calls"`, `"usage"`, `"content"`, `"reasoning_content"`) BEFORE
    json.loads ran. That gate was unsound: a delta carrying `reasoning` or
    `reasoning_text` (both accepted by pi-ai, the DSH client library) matched
    no marker and was discarded unparsed, so the model's thinking vanished with
    no error and no log line. Any escaped spelling (`reasoning\u005fcontent`)
    was lost the same way, and so would be any field added later.

    The cheap, sound gate is the `data:` prefix itself — it must be present for
    a line to be parseable at all, and it rejects SSE comment/keepalive lines
    (`: keepalive`) in the same byte comparison. Every remaining line is
    parsed; the per-line cost on the old fast-path majority (`{"delta":{}}`,
    a few dozen bytes) is negligible, and correctness on the reasoning channel
    is worth far more than the micro-optimisation it replaces.
    """
    buf = bytearray()
    async for chunk in byte_iter:
        buf += chunk
        while True:
            # LINE TERMINATORS (audit 2026-10-29, C-7). SSE (WHATWG) accepts
            # CR, LF and CRLF. This cursor split on LF only, so a CR-only
            # upstream — or an intermediary rewriting line endings — parsed as
            # ONE giant unparseable line: json.loads failed on the whole
            # concatenation, every event was dropped, and the client received a
            # successful EMPTY completion instead of an answer. Find whichever
            # terminator comes first; CRLF stays one break (the trailing LF is
            # consumed as the next, empty line and skipped by the prefix test).
            nl = buf.find(b"\n")
            cr = buf.find(b"\r")
            if nl == -1 and cr == -1:
                break
            idx = cr if (cr != -1 and (nl == -1 or cr < nl)) else nl
            line = bytes(buf[:idx])
            del buf[: idx + 1]
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
        text = buf.decode("utf-8", errors="replace").strip()
        if text.startswith("data:"):
            payload = text[5:].strip()
            if payload and payload != "[DONE]":
                try:
                    yield json.loads(payload)
                except json.JSONDecodeError:
                    pass


def _sole_open_unclaimed(order: list[dict]) -> dict | None:
    """The one still-id-less open call in `order`, when exactly one exists.

    C-3 (audit 2026-10-29): identity adoption used to REQUIRE an index on the
    id-bearing fragment, so an upstream that split id and index across fragments
    forked ONE call into two. Two or more open unclaimed calls is genuinely
    ambiguous — guessing would bind one call's identity to ANOTHER call's
    fragments, which is worse than the fork it replaces — so return None and let
    the caller open a new call.
    """
    open_slots = [s for s in order if not s.get("_claimed")]
    return open_slots[0] if len(open_slots) == 1 else None


async def stream_model_call(
    resp: httpx2.Response,
    id_prefix: str = "",
    id_counter: list[int] | None = None,
) -> AsyncIterator[tuple[str, Any]]:
    """Consume one upstream chat stream (an httpx2 streaming Response); yield
    typed events: ('reasoning', text) | ('content', text) | ('usage', dict) |
    ('tool_calls', [assembled]) — tool_call argument fragments are assembled
    across deltas (vLLM streams them in pieces).

    `id_prefix` + `id_counter` scope the SYNTHETIC ids minted for calls the
    upstream never numbered (see below). Both must span the whole relay TURN,
    not one model call: the relay makes one model call per iteration, so a
    per-call counter restarts at 1 every iteration and two iterations both mint
    "…_1" — which the client's join (keyed on call id for the whole response)
    would collapse onto one card, re-creating the very defect the synthetic ids
    exist to prevent. relay_stream owns the counter (a one-element list, so the
    generator can mutate it) and passes the request id as the prefix; the
    defaults keep direct/test callers working.
    """
    # Assembly state. Order is the upstream's FIRST-SEEN order (a list), never a
    # sort of the index space — see the ordering note at the end of this function.
    order: list[dict] = []
    by_index: dict[int, dict] = {}
    by_id: dict[str, dict] = {}
    # Synthetic ids for calls the upstream never numbered, so the wire deltas,
    # the pcai telemetry events and the client's join all agree on one key
    # (audit 2026-10-26, F-4: an empty id made the DSH renderer join BOTH
    # telemetry events to ONE card, losing one result and mis-attributing the
    # other). Prefixed by the caller's turn-scoped id — see the docstring.
    # Caller-owned across the whole turn; a local counter would restart per
    # model call (see the docstring).
    counter = id_counter if id_counter is not None else [0]
    # A synthetic id must be unmistakably NOT an upstream id (review P1c: the old
    # fixed default "call_relay" could equal a real upstream id such as
    # "call_relay_1", fusing two different calls onto one). The prefix is the
    # caller's turn id; when absent, a per-call random tag keeps it unique.
    mint_prefix = id_prefix or f"relay{uuid.uuid4().hex[:8]}"
    usage = None
    async for obj in parse_sse_payloads(resp.aiter_bytes()):
        # TYPE GUARD (review E-1, P0). parse_sse_payloads yields whatever
        # json.loads returned, so a legal-but-non-object SSE data line
        # (`data: null`, `data: [1,2]`, `data: 42`, `data: "x"`) reaches this
        # loop. The old byte-marker prefilter happened to reject those before
        # parsing — an ACCIDENTAL type guard that vanished when the prefilter
        # was removed, and the resulting AttributeError killed the whole SSE
        # stream with no guard event, no error chunk, no [DONE] and no settle.
        # The guard is now explicit.
        if not isinstance(obj, dict):
            continue
        choices = obj.get("choices")
        if not isinstance(choices, list) or not choices:
            choices = []
        first_choice = choices[0] if choices and isinstance(choices[0], dict) else {}
        delta = first_choice.get("delta")
        if not isinstance(delta, dict):
            delta = {}
        reasoning_text = _reasoning_text_of(delta)
        if reasoning_text:
            yield ("reasoning", reasoning_text)
        if delta.get("content"):
            yield ("content", delta["content"])
        # ELEMENT TYPE GUARD (audit 2026-10-29, ZC-4). The E-1 guard above
        # validates the top-level object and choices[0], but the PER-FRAGMENT
        # element was still trusted to be a dict: {"delta":{"tool_calls":[42]}}
        # reached tcd.get() and raised AttributeError, which is neither
        # TimeoutError nor httpx2.HTTPError — so it escaped relay_stream and
        # killed the client's SSE stream (no guard event, no error chunk, no
        # [DONE], no settle): exactly the blast radius E-1 describes, one level
        # lower. The container gets the same treatment (a bare object there
        # would be iterated as its KEY STRINGS). A malformed upstream must
        # never kill the turn.
        _tc_frags = delta.get("tool_calls")
        if not isinstance(_tc_frags, list):
            _tc_frags = []
        for tcd in _tc_frags:
            if not isinstance(tcd, dict):
                continue
            fn = tcd.get("function")
            if not isinstance(fn, dict):
                fn = {}
            raw_id = tcd.get("id")
            frag_id = raw_id if isinstance(raw_id, str) else ("" if raw_id is None else str(raw_id))
            frag_name = fn.get("name") or ""
            # INDEX COERCION (review E-3). A non-int index ("0", 0.0) used to be
            # discarded as "absent", after which the fragment fell through to the
            # most-recently-opened call and MERGED two distinct calls into one —
            # the exact F-2 symptom, on a shape the pre-fix code handled fine.
            # Accept anything that is unambiguously an index.
            raw_index = tcd.get("index")
            index: int | None = None
            if isinstance(raw_index, bool):
                index = None
            elif isinstance(raw_index, int):
                index = raw_index
            elif isinstance(raw_index, float) and raw_index.is_integer():
                index = int(raw_index)
            elif isinstance(raw_index, str) and raw_index.strip().lstrip("-").isdigit():
                index = int(raw_index.strip())

            # Resolve WHICH call this fragment belongs to (audit 2026-10-26,
            # F-2/F-3). The OpenAI streaming contract does not require an index
            # on every fragment, and several servers stamp it only on the first
            # delta — so keying blindly on the index (the old get("index", 0))
            # merged distinct parallel calls into slot 0, CONCATENATING their
            # names and arguments: a call that either fails JSON parsing or,
            # worse, parses as valid JSON and executes the wrong thing.
            # Resolution order:
            #   1. a fragment carrying an id we already know -> that call;
            #   2. an explicit index we already know         -> that call;
            #   3. a fragment carrying a NEW id              -> a NEW call (even
            #      if its index collides with an open slot: the id is the
            #      authoritative identity);
            #   4. otherwise                                 -> the open call at
            #      that index, or, with no index, the most recently opened call.
            #
            # KNOWN LIMIT (review P1a, accepted): if an upstream reuses one index
            # for a second call while the FIRST call is still streaming id-less
            # fragments, those tail fragments land on the newer call. The stream
            # carries no signal that disambiguates them (no id, and the index is
            # shared by construction), so no assignment is derivable — the choice
            # is which call gets corrupted. We take the newer one and never
            # raise: a malformed upstream must not kill the turn. Every
            # well-formed upstream numbers its calls distinctly, so this needs
            # an upstream that reuses indexes AND omits ids on continuations.
            # LATE-ID ADOPTION (review E-2): an upstream may reveal a call's id
            # only on a LATER fragment. The id is then unknown to by_id, but the
            # call is already open under its index — creating a fresh slot would
            # SPLIT one call into two (the second with an empty name, which
            # became an unknown-tool error). So an unknown id adopts the open,
            # still-id-less call at that index when there is one.
            slot = None
            if frag_id and frag_id in by_id:
                # 1. an id we already know -> that call.
                slot = by_id[frag_id]
            elif frag_id:
                # 2. an id we do NOT know. It may be a call that only revealed
                #    its id on a later fragment: adopt the open, still-unclaimed
                #    call at this index. A CLAIMED slot belongs to a different
                #    id, so this really is a new call (slot stays None).
                if index is not None:
                    candidate = by_index.get(index)
                    if candidate is not None and not candidate.get("_claimed"):
                        slot = candidate
                if slot is None and index is None:
                    # C-3: the id-bearing fragment carried NO index at all, so
                    # there was nothing to look up and the E-2 adoption above
                    # could not fire. Without this the fork is exactly the one
                    # the E-2 note describes: the id-less prefix keeps the
                    # arguments and loses the name (unknown-tool error), while
                    # the id-bearing fragment keeps the name with NO arguments —
                    # json.loads("" or "{}") yields {} and the tool EXECUTES with
                    # defaults. Adopt the sole open, unclaimed call.
                    # An index that maps to NOTHING is deliberately NOT adopted:
                    # a new index means a new call (the E-3/parallel-call case),
                    # not a late id.
                    slot = _sole_open_unclaimed(order)
            elif index is not None:
                # 3. no id: continue the call at this index (the ordinary
                #    continuation-fragment case, and the F-3 repeat-name case).
                slot = by_index.get(index)
                if slot is None and not frag_name:
                    # C-3, the MIRROR shape (the one the audit REPRODUCED): the
                    # id+name arrived FIRST and without an index, so no by_index
                    # entry was ever created — this arguments fragment then
                    # forked a second, nameless call while the real one kept the
                    # name and executed with {} arguments. A NAMELESS fragment
                    # cannot be opening a new call (a call is born with its
                    # name), so adopt the open call when it has no arguments
                    # yet. Both guards matter: an already-argumented call
                    # receiving arguments at an unseen index is a DIFFERENT
                    # call, and two open unclaimed calls are ambiguous.
                    # FUSION GUARD (cross-validation XV-1): adopt ONLY while NO
                    # call has claimed an index yet. The mirror shape opens its
                    # call WITHOUT an index, so by_index is still empty when the
                    # arguments arrive. Once any call owns an index, an unseen
                    # index is a GENUINE new call — adopting there fused two
                    # parallel calls into one (name 'tool_atool_b', args of call
                    # 2), and neither tool then executed. This strictly narrows
                    # the adoption surface: the fork fix above (id-bearing
                    # fragment with no index) is untouched.
                    # EXACTLY ONE OPEN CALL, AND NOBODY OWNS AN INDEX YET. The
                    # mirror shape's call carries an id (so it is _claimed and
                    # _sole_open_unclaimed would refuse it), which is why the
                    # rule is positional rather than claim-based. Two or more
                    # calls, or any call already mapped to an index, means this
                    # unseen index is a GENUINE new call — adopting there fused
                    # two parallel calls into one (name 'tool_atool_b', args of
                    # call 2) and neither tool then executed (cross-validation
                    # XV-1).
                    candidate = order[0] if (len(order) == 1 and not by_index) else None
                    if candidate is not None and not candidate["function"]["arguments"]:
                        slot = candidate
                        by_index[index] = slot
            elif order:
                # 4. no id, no index: the most recently opened call.
                slot = order[-1]

            if slot is None:
                counter[0] += 1
                slot = {
                    "id": frag_id or f"{mint_prefix}_call{counter[0]}",
                    "type": "function",
                    "function": {"name": "", "arguments": ""},
                }
                if frag_id:
                    slot["_claimed"] = True
                order.append(slot)
                if index is not None:
                    by_index[index] = slot
                by_id[slot["id"]] = slot

            if frag_id and slot["id"] != frag_id:
                # The upstream only revealed the id on a later fragment (or this
                # fragment adopted the open call at its index): bind the id and
                # mark the slot claimed so a DIFFERENT id never adopts it.
                slot["id"] = frag_id
                slot["_claimed"] = True
                by_id[frag_id] = slot

            # NAME ASSEMBLY (F-3 + review P2). Two opposite upstream shapes:
            #   * REPEAT — every fragment restates the whole name ("bash","bash"):
            #     concatenating produced "bashbash", which missed both the ACL
            #     parse and the alias path and became an unknown-tool error.
            #   * SPLIT  — the name arrives in pieces ("ba","sh"): setting it
            #     once would truncate to "ba" and execute the wrong tool.
            # Append only when the fragment EXTENDS what we already have (split);
            # ignore it when it merely restates it (repeat).
            if frag_name:
                current_name = slot["function"]["name"]
                piece = str(frag_name)
                if not current_name or (piece != current_name and piece.startswith(current_name)):
                    # Empty slot, or the fragment carries a LONGER form of the
                    # name we have so far (split across fragments).
                    slot["function"]["name"] = piece
                elif not current_name.endswith(piece) and not piece.startswith(current_name):
                    # A genuinely new tail piece: append it.
                    slot["function"]["name"] = current_name + piece
            frag_args = fn.get("arguments")
            if frag_args:
                # Defensive: a non-string `arguments` (some servers send a
                # pre-parsed object) would raise TypeError on += and kill the
                # stream mid-turn. Coerce, and never lose the fragment.
                slot["function"]["arguments"] += (
                    frag_args if isinstance(frag_args, str) else json.dumps(frag_args, default=str)
                )
        if isinstance(obj.get("usage"), dict):
            usage = obj["usage"]
    if usage is not None:
        yield ("usage", usage)
    if order:
        # Strip the internal claim marker: the wire shape must stay a clean
        # OpenAI tool_call.
        for slot in order:
            slot.pop("_claimed", None)
        # FIRST-SEEN order, not a sort of the index space: the upstream arrival
        # order is the model intent order, and a synthetic slot has no index.
        yield ("tool_calls", order)


def sse_event(obj: dict) -> bytes:
    return b"data: " + json.dumps(obj).encode() + b"\n\n"


def _message_text_tokens(message: Any) -> int:
    """Text-only token estimate for ONE chat message (len//4 heuristic).

    SOURCE OF TRUTH: ``pcai_llm.api.routes_proxy._message_text_tokens``.
    Replicated here because routes_proxy imports THIS module at import time
    (``from ..core import ... toolrelay``) — a module-level import back into
    the API layer would be circular. Keep the two byte-identical; the parity
    test in tests/test_perf_wins.py pins them together.

    Only the "text" field of dict parts is counted; image_url / input_audio /
    file parts are ignored ENTIRELY (their URLs and base64 are not tokens).
    Never raises.
    """
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return len(content) // 4
    if isinstance(content, list):
        return sum(
            len(part["text"]) // 4 for part in content if isinstance(part, dict) and isinstance(part.get("text"), str)
        )
    return 0


def _estimate_relay_tokens(msgs: list[dict], max_tokens: int | None = None) -> int:
    """Token estimate for one relay model call (same len//4 heuristic as the
    proxy's _estimate_tokens). The settle fallback when an upstream omits
    stream usage — an estimate beats no row (budgets are ledger-derived).

    SECURITY (the routes_proxy._estimate_tokens fixes, ported — this twin
    kept BOTH audited bug shapes):
      * the prompt term counts TEXT ONLY (see _message_text_tokens): the old
        ``len(str(m.get("content")))`` stringified multimodal content-part
        lists, billing a 1.5 MB base64 screenshot as ~501K prompt tokens.
      * max_tokens is coerced EXACTLY like routes_proxy._estimate_tokens
        (falsy -> 1024; ``max(0, min(int(x), 8192))``; a non-numeric value
        or NaN/inf -> 1024). A string used to raise ValueError -> 500, and a
        negative value DECREMENTED the key's TPM window (check_rate_limit
        adds the estimate verbatim) — a denial-of-wallet bypass.
    Never raises, never returns a negative number.
    """
    prompt = sum(_message_text_tokens(m) for m in msgs) if isinstance(msgs, (list, tuple)) else 0
    completion_est = 1024  # absent/falsy max_tokens keeps the default expectation
    if max_tokens:
        try:
            completion_est = max(0, min(int(max_tokens), 8192))  # negatives floor at 0
        except (TypeError, ValueError, OverflowError):
            completion_est = 1024  # non-numeric / NaN / inf -> the default expectation
    return max(0, prompt + completion_est)  # never negative: a negative would credit the window


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
    # iterations_out: OUT-PARAM (audit C-8, 2026-10-29). The loop publishes the
    # number of COMPLETED model calls here as it goes, so the caller can
    # histogram the depth actually reached on EVERY exit path — normal end,
    # guard abort, client disconnect (the generator is closed mid-stream) —
    # instead of re-deriving it by byte-matching the emitted SSE. The old
    # derivation read 0 for every X-PCAI-Stream-Events: 0 client, i.e. for
    # every strict OpenAI client. None = the caller does not want it.
    iterations_out: list[int] | None = None,
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
    # Stamped inert tool-call deltas (Wave R1): emit each in-cluster tool
    # call as a NATIVE tool_call delta stamped `pcai.relay: true` (display
    # contract — a client that understands the stamp renders it and never
    # executes; every other client must never see it, so this is gated by
    # the routes layer on BOTH settings.tool_relay_render_deltas AND the
    # request header X-PCAI-Relay-Deltas: 1). Indexes start at
    # render_index_base (>> any handback enumeration) so a legacy client
    # that assembles deltas by index cannot merge rendered and handback
    # calls even if it never notices the stamp.
    render_deltas: bool = False,
    render_index_base: int = 1000,
    # Gateway-authored system text prepended to EVERY model call of this turn
    # (an agent profile's prompt; '' = none, byte-for-byte today's behaviour).
    # Applied per CALL by with_system_prefix — never to relay_msgs, whose length
    # is the handback boundary — and it therefore rides into the effective-prompt
    # capture (the admin's "what the model actually saw" view) and into the
    # settle-time token estimate, so neither under-reports by its size.
    system_prefix: str = "",
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
    # --- TOOL SURFACE (2026-10-28) ---------------------------------------
    # Inlining every granted tool cost 34,459 prompt tokens for a 111-tool /
    # 10-server catalogue (measured) -- a floor on EVERY relayed request, and
    # the reason a 236k conversation overflowed a 262k model window. In hybrid
    # mode the model is offered ONE discovery tool and pays for a server's real
    # definitions only after asking to see them (promotion below).
    #
    # "full" is the pre-change behaviour byte-for-byte, so rollback is config:
    # PCAI_LLM_TOOL_RELAY_SURFACE=full.
    _surface_mode = str(getattr(settings, "tool_relay_surface", "full") or "full").strip().lower()
    if _surface_mode not in tool_surface.MODES:
        log.warning("toolrelay: unknown tool_relay_surface %r - using 'full'", _surface_mode)
        _surface_mode = tool_surface.MODE_FULL
    # Promoted servers for THIS relay turn. Mutated by the meta-tool handler in
    # the dispatch loop; read by _tools_now() on the next iteration.
    _promoted: set[str] = set()

    def _tools_now() -> list[dict]:
        """The tools to offer RIGHT NOW (gateway surface + client tools).

        Recomputed per iteration so a server promoted by a meta-tool call is
        callable on the very next model call. The client half never changes.
        """
        gw = tool_surface.build_tools(catalog, mode=_surface_mode, promoted=_promoted, sep=TOOLSEP) if grants else []
        # COLLISION: if the client offers its own tool named META_TOOL, our
        # discovery tool can never be called (client_names wins the partition),
        # and two same-named entries would make the offer ambiguous. Drop OURS:
        # the warning below already tells the operator to rename.
        if any(t.get("function", {}).get("name") == tool_surface.META_TOOL for t in offered_client):
            gw = [t for t in gw if t.get("function", {}).get("name") != tool_surface.META_TOOL]
        return gw + offered_client

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
    client_names = {t.get("function", {}).get("name", "") for t in (client_tools or [])}
    # NAME COLLISION (2026-10-28): if the CLIENT offers its own tool called
    # `pcai_tools`, _is_client_call() claims that name (client_names wins the
    # partition), so a discovery call is handed back to the client instead of
    # being handled locally — discovery is unreachable for the whole request and
    # the hybrid surface offers NO MCP tools at all. Granted MCP tools can never
    # collide (they always carry TOOLSEP), so the client is the only vector.
    # Loud, because the symptom (a model that stops reaching for tools) is
    # otherwise indistinguishable from the model just choosing not to.
    if tool_surface.META_TOOL in client_names:
        log.warning(
            "toolrelay: the client offered a tool named %r, shadowing the tool-surface"
            " discovery tool — MCP tools will be unreachable this request. Rename the"
            " client tool, or run this key with tool_relay_surface=full.",
            tool_surface.META_TOOL,
        )
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
        (phase='chat', status matching the settle) — best-effort.

        The AGENT PROMPT is applied to the message list HERE for the same reason
        _bounded_call applies it to the wire: the estimate fallback and the
        effective-prompt capture must describe the request that was actually
        sent, not the client list without the gateway own prefix.
        """
        msgs = with_system_prefix(msgs, system_prefix)
        u = usage or {}
        prompt = int(u.get("prompt_tokens") or 0)
        completion = int(u.get("completion_tokens") or 0)
        if usage is None:
            # upstream did not report usage — fall back to the estimate
            # Per-model max_tokens ceiling: the WIRE body is built by the
            # routes layer's stream_once (the relay never assembles an
            # upstream request itself), so the authoritative clamp lives
            # there — re-evaluated per call with that call's endpoint model.
            # THIS clamp keeps the estimate fallback consistent with what the
            # upstream was actually allowed: `model` is read at call time and
            # the mid-loop failover above rebinds it to the fallback's name,
            # so each iteration estimates against ITS OWN serving model.
            # Computed only in the fallback branch — the common path (upstream
            # reported usage) must not pay for it.
            if estimate is not None:
                prompt = estimate(msgs)
            else:
                capped = clamp_max_tokens({"max_tokens": max_tokens}, model, settings.max_tokens_ceiling)
                prompt = _estimate_relay_tokens(msgs, capped.get("max_tokens"))
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
        if iterations_out is not None:
            # C-8: publish the completed-call count at the ONE point every
            # model call passes through, so EVERY terminal (plain finish,
            # handback, guard abort, client disconnect) reports the depth it
            # reached. call_iteration is 0-based, so +1 is the count.
            iterations_out[:] = [max(iterations_out[0] if iterations_out else 0, call_iteration + 1)]
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
        # THE one place a model call is assembled: the agent prompt (if any) is
        # prepended HERE, so what goes upstream and what the capture records are
        # the same list by construction.
        ait = stream_once(with_system_prefix(msgs, system_prefix), tools_arg).__aiter__()
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

    async def _next_failover() -> tuple[str, Any, list[bytes]] | None:
        """Pop the next USABLE fallback candidate: (model, stream factory) plus
        the user-visible marker chunks for the handoff, or None when the list
        is exhausted.

        Shared by BOTH mid-relay failover triggers: budget exhaustion BEFORE a
        call and an upstream transport failure ON a call (see the catch in the
        model-call loop below). Eligibility keeps the fail-closed rule: a
        candidate whose budget check RAISES is skipped (log + next), never
        served — a DB incident must not become a spending window.
        """
        markers: list[bytes] = []
        # Keyless runs (unit tests) have no budget identity to gate on — the
        # fail-closed rule leaves no eligible candidate to serve, so the walk
        # ends before the loop (check_budgets requires a real UUID).
        if key_id is None:
            return None
        while fallbacks:
            fb_model, fb_stream = fallbacks.pop(0)
            try:
                fb_gate = await check_budgets(key_id, fb_model, 0)
            except Exception:
                log.exception("relay: failover budget check failed for %s — skipping candidate", fb_model)
                markers.append(_reasoning_delta(f"\n[relay] {fb_model} unavailable — trying next\n"))
                continue
            if fb_gate.ok:
                # Near-transparent handoff: ONE compact mention that the switch
                # happened. The why (which model failed, which gate reason)
                # lives in the audit trail — the error settle row + the trace
                # card — not in the user's reading flow.
                markers.append(_reasoning_delta(f"\n[relay] failing over to {fb_model}\n"))
                return fb_model, fb_stream, markers
            markers.append(_reasoning_delta(f"\n[relay] {fb_model} unavailable — trying next\n"))
        return None

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

    def _rendered_delta(tc: dict, idx: int) -> bytes:
        """Stamped inert tool-call delta (Wave R1): the SAME wire shape as a
        handback delta plus the top-level `pcai.relay: true` stamp. The
        stamp is the contract — a rendering client shows this call as a
        completed display-only card (the relay owns execution) and NEVER
        executes it; the off-base index (render_index_base, default 1000)
        keeps a stamp-blind client from merging it with handback deltas.
        Only reachable when render_deltas is on, which the routes layer
        gates on settings AND the client's X-PCAI-Relay-Deltas: 1."""
        fn = tc.get("function", {})
        chunk: dict[str, Any] = {
            "id": request_id,
            "object": "chat.completion.chunk",
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "index": render_index_base + idx,
                                "id": tc.get("id", ""),
                                "type": "function",
                                "function": {
                                    "name": fn.get("name", ""),
                                    "arguments": fn.get("arguments", ""),
                                },
                            }
                        ]
                    },
                }
            ],
            "pcai": {"relay": True},
        }
        return sse_event(chunk)

    def _trace_call(
        server: str,
        tool: str,
        args: dict | str,
        status: str,
        ms: int | None = None,
        content: Any = None,
    ) -> str:
        """One tombstone line for the reasoning stream. Prefixed exactly
        `[tool] {server}__{tool} →` (tests and humans pattern-match that);
        the enriched form adds the (collapsed) arguments and, on completion,
        a bounded result preview, so the line is self-sufficient for clients
        that never parse the pcai events. Preview/args bounds mirror the
        mcp_summaries discipline (model-facing caps, not display caps)."""
        line = f"\n[tool] {server}__{tool}"
        if args:
            if isinstance(args, str):
                args_s = args
            else:
                try:
                    args_s = json.dumps(args, default=str)
                except (TypeError, ValueError):
                    args_s = str(args)
            if len(args_s) > 200:
                args_s = args_s[:200] + "…"
            line += f"({args_s})"
        if status in ("ok", "error"):
            line += f" → {status} ({ms}ms)"
        elif status == "denied":
            line += " → denied by ACL"
        elif status == "invalid arguments":
            line += " → invalid arguments JSON"
        else:
            line += f" → {status}"
        if content is not None:
            preview = _preview(content, cap=300)
            if preview:
                flat = " ".join(preview.split())
                line += f"\n  ↳ {flat}"
        return line + "\n"

    def _reasoning_trace(
        server: str,
        tool: str,
        args: dict | str,
        status: str,
        ms: int | None = None,
        content: Any = None,
    ) -> bytes:
        """The reasoning-channel tombstone for one tool outcome — or b"" when
        the client is rendering the stamped inert deltas (render_deltas). A
        delta-rendering client already shows the call as a native tool-call
        card joined to the tool_call/tool_error pcai event's (uncapped-args,
        ≤4000-char preview) payload, so the text tombstone is redundant
        duplication — and worse, it streams tool information INTO the next
        reasoning block, welding the tool section to the following cycle's
        thinking instead of letting the cycle boundary close the panel
        (the DSH 2026-10-02 stale-panel report). Clients without the deltas
        keep today's tombstone bytes: there it is the only visible view of
        the call."""
        if render_deltas:
            return b""
        return _reasoning_delta(_trace_call(server, tool, args, status, ms=ms, content=content))

    if failover_note:
        yield _reasoning_delta(f"\n[relay] {failover_note}\n")

    done_state: list[dict] | None = None

    # Monotone wire-index cursor for stamped render deltas (see the emission
    # site). MUST NOT reset per iteration — the client's block map is keyed by
    # this index for the whole response.
    render_index_next = 0

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
                picked = await _next_failover()
                if picked is None:
                    detail = (
                        f"budget check unavailable mid-relay ({gate_error}); turn ended fail-closed"
                        if gate is None
                        else f"budget exceeded mid-relay: {gate.reason}"
                    )
                    yield _pcai({"pcai": {"event": "guard", "detail": detail}})
                    yield b"data: [DONE]\n\n"
                    return
                for marker in picked[2]:
                    yield marker
                model, stream_once = picked[0], picked[1]
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
        _last_offered_tools[0] = _tools_now() or None
        while True:
            attempt = total_retries - retries_left + 1
            reasoning_acc: list[str] = []
            content_acc: list[str] = []
            tool_calls: list[dict] = []
            usage = None
            model_error = False
            call_started = time.monotonic()
            call_timed_out = ""
            call_failed = ""
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
                    relay_msgs, _tools_now() or None, guard.deadline - time.monotonic()
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
            except httpx2.HTTPError as exc:
                # Upstream TRANSPORT failure (ConnectError / DNS / TLS / a
                # connection reset between chunks — same class _stream_chat
                # fails over on). Pre-fix this escaped relay_stream and killed
                # the client's SSE stream mid-flight: the harness saw a
                # truncated stream (TRANSPORT), burned its retry budget on the
                # SAME dead model, and the gateway never tripped the breaker
                # (report() lives in the routes-layer paths only). Recorded as
                # a failed attempt (the settle below stamps status=error); the
                # post-call block decides failover vs a clean close.
                call_failed = f"{type(exc).__name__}: {exc}".strip() or "upstream call failed"
                model_error = True
                # The failure surfaced through _bounded_call's pending
                # __anext__ task: retrieving it here marks the task retrieved,
                # but `_bounded_call`'s finally does a bare `await pending` to
                # reap it — a task whose exception was ALREADY fetched raises
                # that exception AGAIN on every await (CPython semantics), and
                # that reaper only suppresses what it catches for the
                # CancelledError/Exception pair it wraps — a second retrieval
                # from OUR re-raise would escape. Prevent the re-raise at the
                # source: consume the exception out of the task so every
                # future await on it is clean, then raise a FRESH instance we
                # own.
                exc = type(exc)(str(exc))
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
            # NOTHING-EMITTED GATE (audit 2026-10-26, F-6). Reasoning and content
            # deltas stream LIVE during the attempt, so an attempt that produced
            # output before expiring cannot be re-run without the client seeing
            # that output twice for one logical step — the retry's premise ("the
            # upstream was queued/wedged and said nothing") only holds when the
            # accumulators are empty. The transport-failure path below already
            # gates on exactly this tuple; the timeout path did not, and
            # PCAI_LLM_TOOL_RELAY_CALL_RETRIES=1 is live in the deployment.
            # "Silent" means silent TO THE CLIENT (review E-4): reasoning that was
            # suppressed by stream_thinking never reached anyone, so it does not
            # make the attempt non-retryable.
            reasoning_seen = bool(reasoning_acc) and stream_thinking
            attempt_silent = not (content_acc or reasoning_seen or tool_calls)
            if retries_left > 0 and attempt_silent and retry_budget >= TOOL_RELAY_RETRY_MIN_BUDGET_S:
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

        if call_failed:
            # Upstream transport failure. The attempt's deltas stream LIVE, so
            # empty reasoning/content/tool accumulators mean the client saw
            # NOTHING of it — the attempt can be replaced wholesale. Walk the
            # ordering's remaining models (the same walk the budget failover
            # uses); a mid-conversation switch is already an established,
            # marked event here. With nothing emitted and nothing left to try,
            # close CLEANLY (guard event + visible error chunk + [DONE]) —
            # never let the exception kill the stream: a terminal answer lets
            # the client stop retrying a dead model.
            failed_model = model
            # SAME client-visibility predicate as the timeout gate above
            # (audit 2026-10-29, ZC-2). This branch keyed on the RAW
            # reasoning_acc, so an attempt that reasoned before the endpoint
            # died was never replaced — even when stream_thinking is OFF and
            # the client saw NONE of it (X-PCAI-Stream-Thinking: 0 is DSH's
            # live posture, so the 2026-10-06 outage protection was silently
            # disabled there). One concept, one policy: "silent" means silent
            # TO THE CLIENT on both triggers.
            if not (content_acc or (bool(reasoning_acc) and stream_thinking) or tool_calls):
                picked = await _next_failover()
                if picked is not None:
                    fb_model, fb_stream, markers = picked
                    for marker in markers:
                        yield marker
                    log.warning(
                        "relay: upstream failure %s on model=%s iteration=%d — failing over to %s (request_id=%s)",
                        call_failed,
                        failed_model,
                        iteration,
                        fb_model,
                        request_id,
                    )
                    model, stream_once = fb_model, fb_stream
                    continue
            detail = f"upstream call failed on {failed_model}: {call_failed}"
            log.error(
                "relay: upstream failure %s on model=%s iteration=%d, no usable fallback (request_id=%s)",
                call_failed,
                failed_model,
                iteration,
                request_id,
            )
            yield _pcai({"pcai": {"event": "guard", "detail": detail}})
            yield _reasoning_delta(f"\n[relay] {detail}\n")
            yield sse_event(
                {
                    "id": request_id,
                    "object": "chat.completion.chunk",
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": "(model error: upstream call failed)"},
                            "finish_reason": "stop",
                        }
                    ],
                }
            )
            yield b"data: [DONE]\n\n"
            return

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
            # The gate must key on what the CLIENT SAW, not on what the upstream
            # produced (review E-4). Reasoning is only visible to the client when
            # stream_thinking is on — with it off the deltas are suppressed
            # (_reasoning_delta returns b""), so re-rolling cannot duplicate
            # anything the user saw, and suppressing the recovery would turn a
            # recoverable empty answer into a guaranteed empty one.
            # F-8 (audit 2026-10-26): the old predicate checked only content_acc,
            # and its comment ("nothing was emitted yet for it") was false for the
            # reasoning channel whenever thinking WAS streaming. Both halves now
            # hold: no content, and no reasoning the client could have seen.
            reasoning_seen_by_client = bool(reasoning_acc) and stream_thinking
            zero_completion = (
                settings.zero_completion_retry
                and not zero_retry_used
                and not content_acc
                and not reasoning_seen_by_client
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
                _last_offered_tools[0] = _tools_now() or None
                retry_started = time.monotonic()
                retry_usage: dict | None = None
                retry_failed = ""
                try:
                    async for kind, payload in _bounded_call(
                        relay_msgs, _tools_now() or None, guard.deadline - time.monotonic()
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
                except httpx2.HTTPError as exc:
                    # TRANSPORT FAILURE ON THE RETRY (audit 2026-10-29, ZC-1).
                    # This branch had NO httpx2 handling at all: _bounded_call
                    # re-raises the pending __anext__ failure (ConnectError /
                    # DNS / TLS / a reset between chunks) and pre-fix it escaped
                    # relay_stream — the SSE died with no [DONE], no error
                    # envelope, and the retry was never settled (the exact
                    # ledger under-count the settle-in-finally fix exists to
                    # prevent). Same treatment as the main attempt loop above:
                    # record the failure, settle it as an error row below, and
                    # close the stream cleanly.
                    retry_failed = f"{type(exc).__name__}: {exc}".strip() or "upstream call failed"
                    model_error = True
                    # Same task-reap hazard the main loop documents: re-raise a
                    # FRESH instance so _bounded_call's finally cannot surface
                    # the already-retrieved exception a second time.
                    exc = type(exc)(str(exc))
                except Exception as exc:  # the relay must never die on an upstream fault
                    # ANYTHING ELSE ON THE RETRY (cross-validation XV-2). The
                    # two clauses above cover the expected transport shapes, but
                    # the retry is a fresh stream parse: a malformed payload, a
                    # client-side error or a cancellation raised between chunks
                    # would still escape relay_stream, killing the SSE with no
                    # [DONE] and leaving THIS attempt unsettled (the first
                    # attempt settles in its own finally; the retry does not).
                    # Same treatment as the transport clause: record, settle
                    # below, close cleanly. The relay's contract is that a
                    # malformed upstream must never kill the turn.
                    retry_failed = f"{type(exc).__name__}: {exc}".strip() or "upstream call failed"
                    model_error = True
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
                if retry_failed:
                    # ZC-1: the retry's transport failure gets the same CLEAN
                    # close the main attempt loop's failure path gives — guard
                    # event + visible error chunk + [DONE] — never an exception
                    # escaping relay_stream (a dead socket is what made the
                    # client burn its retry budget on a dead model).
                    detail = f"upstream call failed on {model}: {retry_failed}"
                    log.error(
                        "relay: upstream failure %s on model=%s iteration=%d during the zero-completion retry (request_id=%s)",
                        retry_failed,
                        model,
                        iteration,
                        request_id,
                    )
                    yield _pcai({"pcai": {"event": "guard", "detail": detail}})
                    yield _reasoning_delta(f"\n[relay] {detail}\n")
                    yield sse_event(
                        {
                            "id": request_id,
                            "object": "chat.completion.chunk",
                            "model": model,
                            "choices": [
                                {
                                    "index": 0,
                                    "delta": {"content": "(model error: upstream call failed)"},
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

        # REASONING CONTINUITY (2026-10-26, CUT 4a). The reasoning this iteration
        # produced was collected and then DISCARDED, so across a 32-iteration
        # turn the model saw up to 31 consecutive examples of itself emitting
        # tool calls with no thinking in front of them — an in-context
        # demonstration to stop reasoning. Feeding the block back keeps the
        # chain intact.
        #
        # BOUNDED BY CONSTRUCTION: the block is capped (reasoning_history_max_chars),
        # so the 2026-09-18 storm shape — unbounded per-cycle growth, one event
        # at 6,630 copies / ~401 KB — cannot recur: worst case is
        # 32 x cap (~63 KB at the default) for a whole turn. The cap is applied
        # to the TAIL (the most recent reasoning, which is what the next call
        # actually needs), and the cut is marked so the model can tell.
        _assistant_row: dict[str, Any] = {
            "role": "assistant",
            "content": "".join(content_acc) or None,
            "tool_calls": tool_calls,
        }
        if settings.reasoning_history_enabled:
            _reasoning_block = "".join(reasoning_acc)
            _cap = max(0, int(settings.reasoning_history_max_chars))
            if _reasoning_block and _cap:
                if len(_reasoning_block) > _cap:
                    _reasoning_block = "…[earlier reasoning trimmed]…" + _reasoning_block[-_cap:]
                _assistant_row["reasoning_content"] = _reasoning_block
        relay_msgs.append(_assistant_row)
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
            # Same exclusion as the stamped-render path below: the discovery
            # meta-tool is relay-internal and must never be advertised as a
            # callable tool on the wire (2026-10-28).
            for _i, _tc in enumerate(tool_calls):
                if str(_tc.get("function", {}).get("name", "")) == tool_surface.META_TOOL:
                    continue
                yield _tc_delta(_tc, _i)

        # Stamped inert render deltas (Wave R1): in-cluster MCP calls re-emitted
        # as `pcai.relay: true` chunks for rendering clients. Only the
        # IN-CLUSTER side — handback deltas below carry NO stamp and remain
        # executable; a client that honors the stamp treats stamped calls as
        # display-only, so double execution stays structurally impossible.
        if render_deltas:
            # INDEX SPACE IS GLOBAL, NOT PER-ITERATION (audit 2026-10-26, F-1).
            # This used to enumerate from 0 on every iteration, so iteration 2's
            # calls reused wire indexes 1000,1001,... — and the client's block
            # map (pi-ai toolCallBlocksByIndex) is keyed by that index and is
            # never cleared, so iteration 2's argument fragments were appended
            # to iteration 1's blocks: two calls fused into one card whose
            # arguments were invalid JSON, while the gateway had really executed
            # both. The counter is now monotone for the whole response; the
            # per-iteration enumeration only decides WITHIN-iteration order.
            for _tc in mcp_calls:
                # NEVER render the DISCOVERY meta-tool (2026-10-28). Rendering
                # it tells the client a `pcai_tools` tool exists, and the client
                # then believes it can call it — measured live: the harness
                # tried `tools.pcai_tools(...)` and got "not a function",
                # burning turns on a tool that only exists inside the relay.
                # Every other relayed call still renders (native visibility).
                if str(_tc.get("function", {}).get("name", "")) == tool_surface.META_TOOL:
                    continue
                yield _rendered_delta(_tc, render_index_next)
                render_index_next += 1

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
            # OpenAI-standard final usage chunk (audit 2026-10-26, F-7): the
            # relay forces include_usage=True at the routes layer precisely so
            # OpenAI-compatible clients (DSH's context meter) see per-call
            # consumption — but BOTH handback terminals used to close with
            # finish_reason + [DONE] and no usage chunk at all. DSH is a
            # handback client, so every DSH turn hit it: measured over 140 real
            # sessions, 21,693 of 21,696 relayed tool-call steps reported
            # totalTokens=0 while direct glm53-flash reported usage on all 449.
            if include_usage and last_usage:
                yield sse_event(
                    {
                        "id": request_id,
                        "object": "chat.completion.chunk",
                        "model": model,
                        "choices": [],
                        "usage": last_usage,
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
            #
            # MAX-ITERATIONS HANDOFF (2026-10 operator request): for the
            # iterations cap specifically, the wrap-up instruction tells
            # the model to report progress and ASK THE USER whether to
            # continue with more tool calls — the user's next message is a
            # FRESH TURN (routes_proxy zeroes iterations_prior on any
            # non-continuation shape), so their "yes" restarts the budget
            # legitimately instead of the cap silently growing. The other
            # abort reasons (wall clock, loop detection) keep the original
            # "answer from what you have" instruction — a repeated identical
            # call must not be laundered into a permission request, and a
            # wall-clock stop is not the model's decision to resume.
            _halt_instruction = (
                (
                    f"Tool use paused: {abort}. Do NOT attempt further tool calls now. "
                    "Briefly summarize the progress made so far and what remains, then ASK THE USER "
                    "whether to continue with more tool calls (their next message starts a fresh "
                    "tool budget)."
                )
                if abort.startswith("max tool iterations")
                else f"Tool use halted: {abort}. Answer from what you have."
            )
            _all_ids = [tc.get("id", "") for tc in tool_calls if tc.get("id")]
            for _i, _tid in enumerate(_all_ids):
                relay_msgs.append(
                    {
                        "role": "tool",
                        "tool_call_id": _tid or f"relay-guard-{_i}",
                        "content": (
                            _halt_instruction if _i == 0 else f"Tool use halted before this call ran: {abort}."
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
                        "choices": [{"index": 0, "delta": {"content": _wrap_text}, "finish_reason": "stop"}],
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
            # OpenAI-standard final usage chunk (audit 2026-10-29, ZC-6). The
            # F-7 fix covered the handback and final-answer terminals and
            # MISSED this third one: a guard-aborted turn settled the wrap-up
            # call in the ledger but put no usage on the wire, so every aborted
            # turn reported 0 usage to OpenAI-compatible clients (DSH's context
            # meter). Same chunk shape as the other terminals; the wrap-up
            # call's OWN usage, since that is the last real model call of the
            # turn.
            if include_usage and wrap_usage:
                yield sse_event(
                    {
                        "id": request_id,
                        "object": "chat.completion.chunk",
                        "model": model,
                        "choices": [],
                        "usage": wrap_usage,
                    }
                )
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
            # --- discovery meta-tool (hybrid surface, 2026-10-28) ----------
            # Handled LOCALLY: it is not a granted MCP tool, so parse_tool_name
            # returns None and the legacy path would answer "unknown tool".
            # `describe` promotes a server, and _tools_now() then offers that
            # server's real tools on the NEXT model call -- the model pays for
            # definitions only where it needs them.
            if fn_name == tool_surface.META_TOOL and _surface_mode == tool_surface.MODE_HYBRID:
                _meta_text, _promoted = tool_surface.handle_meta_call(fn.get("arguments"), catalog, _promoted)
                relay_msgs.append(
                    # Cap like every other tool result: a describe payload is
                    # re-prefilled on every later iteration, so an uncapped one
                    # is exactly the "big result" hazard this cap exists for.
                    {"role": "tool", "tool_call_id": tc.get("id", ""), "content": _relay_content(_meta_text)}
                )
                yield _pcai(
                    {
                        "pcai": {
                            "event": "tool",
                            "iteration": iteration,
                            "tool": fn_name,
                            "status": "ok",
                            "detail": "promoted=" + ",".join(sorted(_promoted)),
                        }
                    }
                )
                continue
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
                            "id": tc.get("id", ""),
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
                    f"error: arguments for {server}/{tool} were not valid JSON — re-issue the call with a JSON object"
                )
                relay_msgs.append({"role": "tool", "tool_call_id": tc.get("id", ""), "content": err_text})
                yield _pcai(
                    {
                        "pcai": {
                            "event": "tool_error",
                            "id": tc.get("id", ""),
                            "server": server,
                            "tool": tool,
                            "detail": "arguments not valid JSON",
                            "args": _preview(fn.get("arguments") or ""),
                        }
                    }
                )
                yield _reasoning_trace(
                    server, tool, _preview(fn.get("arguments") or "{}", cap=200), "invalid arguments"
                )
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
                            "id": tc.get("id", ""),
                            "server": server,
                            "tool": tool,
                            "ok": exec_ok,
                            "ms": ms,
                            "args": args,
                            "result": _preview(content),
                        }
                    }
                )
                yield _reasoning_trace(server, tool, args, "ok" if exec_ok else "error", ms=ms, content=content)
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
                            "id": tc.get("id", ""),
                            "server": server,
                            "tool": tool,
                            "detail": "denied by ACL",
                            "args": args,
                        }
                    }
                )
                yield _reasoning_trace(server, tool, args, "denied")
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
                        "id": tc.get("id", ""),
                        "server": server,
                        "tool": tool,
                        "ok": ok,
                        "ms": ms,
                        "args": args,
                        "result": _preview(content),
                    }
                }
            )
            yield _reasoning_trace(server, tool, args, "ok" if ok else "error", ms=ms, content=content)
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
            # OpenAI-standard final usage chunk (audit 2026-10-26, F-7): the
            # relay forces include_usage=True at the routes layer precisely so
            # OpenAI-compatible clients (DSH's context meter) see per-call
            # consumption — but BOTH handback terminals used to close with
            # finish_reason + [DONE] and no usage chunk at all. DSH is a
            # handback client, so every DSH turn hit it: measured over 140 real
            # sessions, 21,693 of 21,696 relayed tool-call steps reported
            # totalTokens=0 while direct glm53-flash reported usage on all 449.
            if include_usage and last_usage:
                yield sse_event(
                    {
                        "id": request_id,
                        "object": "chat.completion.chunk",
                        "model": model,
                        "choices": [],
                        "usage": last_usage,
                    }
                )
            await _flush_conversation_state(iteration + 1)
            if iterations_out is not None:
                iterations_out[:] = [iteration + 1]
            yield b"data: [DONE]\n\n"
            return

        iteration += 1
        if iterations_out is not None:
            # Published after EVERY completed call, not at the end: the caller
            # may close this generator at any point (client disconnect), and a
            # finally-style publish would never run then.
            iterations_out[:] = [iteration]
