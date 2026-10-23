"""Client tool-server: the offer registry + dispatch futures (ROADMAP §7,
phase 2 wave B — the SPIKE core, behind settings.client_tool_dispatch).

WHY this module exists: the gateway cannot reach an off-cluster laptop —
the client dials IN to /v1. So a relayed turn that needs the client's
`bash` cannot "call" it the way it calls an in-cluster MCP server; it
needs a reverse channel over the connection the client already holds.
This module is the gateway half of that channel (design doc: ROADMAP
§ 7 phase 2 wave B, shape (b) — long-poll dispatch):

- the client REGISTERs its tools (`offer`) — one live offer per key;
- the RELAY calls `dispatch(key_id, tool, args)` when the model names a
  client-offered tool: a job is enqueued and the relay parks on an
  asyncio future;
- the client's long-poll (`routes_dispatch.py` GET /v1/dispatch/poll)
  claims the job ATOMICALLY (dispatch-once: the queue pop IS the
  delivery), executes it under its local approval policy, and POSTs the
  answer; `settle_result` resolves the future and the relay loop
  CONTINUES — no handback, no finish_reason=tool_calls.

Fail-safe defaults (the wave's constraint set):
- every wait is bounded — an offline/busy client surfaces as a tool
  ERROR after `timeout_s`, never a hang;
- state is IN-MEMORY (no schema change this wave): a restart empties
  offers and pending jobs; pending dispatches then time out as clean
  tool errors — the ledger/trace rows still record what happened;
- results are delivered once: a second result POST for the same
  dispatch_id is refused (replay denial), and a job claimed by one poll
  can never be re-delivered by another.

The executor-side gate discipline (rate limit, loop guard, settle under
`local:<tool>`, trace under server="client") lives with the executor
adapter; this module holds ONLY the channel state machines.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from ..config import settings

log = logging.getLogger(__name__)

# Tool-name grammar (mirrors the MCP tool-name rule the flat surface
# already enforces in routes_mcp._FLAT_NAME_MAX): an offered tool becomes
# part of the MODEL's toolset (`client__<name>`), so it must be a legal
# OpenAI/MCP function name before it is ever offered.
TOOL_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")

# Offer/tool bounds (mirror the conversations caps' philosophy: abuse
# must fail with 400/413, never inflate the model prompt or the memory).
MAX_TOOLS = 64
MAX_SCHEMA_BYTES = 32 * 1024
# Pending-job queue bound per key: a client that stopped polling must not
# accumulate unbounded dispatch work; overflow refuses the NEW dispatch
# immediately (the model sees a tool error and can pick another path).
MAX_PENDING_PER_KEY = 64
# TTL clamp for the offer itself (design doc: default 900 s).
MIN_TTL_S = 60.0
MAX_TTL_S = 86400.0


class DispatchError(Exception):
    """Caller-facing refusal (validation, unknown offer, queue overflow).

    The routes layer maps subclasses to HTTP statuses; the executor maps
    them to (False, <message>) tool results. Carries a short stable code
    so mappings stay explicit.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class ClientTool:
    """One tool the client serves, with the schema the MODEL will see."""

    name: str
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=lambda: {"type": "object"})
    timeout_s: float | None = None  # None = the gateway default


@dataclass
class ClientOffer:
    """One key's live tool-server registration (latest-wins)."""

    key_id: uuid.UUID
    client_id: str
    tools: dict[str, ClientTool]
    expires_at: float  # monotonic clock — compare against time.monotonic()

    def live(self, now: float | None = None) -> bool:
        return (now if now is not None else time.monotonic()) < self.expires_at


@dataclass
class DispatchJob:
    """One claimed-or-claimable tool call headed to the client."""

    dispatch_id: str
    key_id: uuid.UUID
    tool: str
    arguments: dict[str, Any]
    dispatched_at: float  # wall clock (echoed to the client)


@dataclass
class _Pending:
    """The future + bookkeeping for one in-flight dispatch."""

    job: DispatchJob
    future: asyncio.Future[tuple[bool, Any]]
    deadline: float  # monotonic
    settled: bool = False


class ClientToolHub:
    """The in-memory channel state: offers, poll queues, pending futures.

    One hub per process (module-level `hub` below). All methods are sync
    except `dispatch`/`wait` — the futures are the only async part, so the
    routes layer can serve polls without locks leaking across awaits (the
    queue pop happens synchronously inside the request handler).
    """

    def __init__(self) -> None:
        self._offers: dict[uuid.UUID, ClientOffer] = {}
        self._queues: dict[uuid.UUID, list[DispatchJob]] = {}
        self._pending: dict[str, _Pending] = {}  # dispatch_id -> pending
        self._generation = 0  # bumped on every mutation (tests observe churn)

    # -- introspection -----------------------------------------------------

    @property
    def generation(self) -> int:
        return self._generation

    def pending_count(self, key_id: uuid.UUID) -> int:
        return len(self._queues.get(key_id, ()))

    def in_flight_count(self, key_id: uuid.UUID) -> int:
        return sum(1 for p in self._pending.values() if p.job.key_id == key_id and not p.settled)

    # -- offers ------------------------------------------------------------

    def offer(self, key_id: uuid.UUID, tools: list[ClientTool], ttl_s: float) -> ClientOffer:
        """(Re-)register the key's tool-server offer. Latest wins: a
        re-register REPLACES the previous offer (and drops any jobs queued
        against the old one — the client that re-registered is a NEW
        registration; stale jobs would dispatch to an identity that just
        changed shape). TTL clamped to [MIN_TTL_S, MAX_TTL_S]."""
        ttl = min(max(float(ttl_s), MIN_TTL_S), MAX_TTL_S)
        now = time.monotonic()
        old = self._offers.get(key_id)
        if old is not None:
            # Drop queued jobs the old offer never got polled: their
            # futures resolve as errors (the dispatching relay turn sees a
            # clean tool error, never a parked future).
            for job in self._queues.pop(key_id, []):
                self._fail(job.dispatch_id, f"error: client re-registered; dispatch {job.dispatch_id} dropped")
        client_id = old.client_id if old is not None else uuid.uuid4().hex
        # Re-register REFRESHES the identity binding (same client_id — the
        # key's offer is one identity) and resets the clock.
        refreshed = ClientOffer(
            key_id=key_id,
            client_id=client_id,
            tools={t.name: t for t in tools},
            expires_at=now + ttl,
        )
        self._offers[key_id] = refreshed
        self._generation += 1
        return refreshed

    def offer_lookup(self, key_id: uuid.UUID) -> ClientOffer | None:
        """The key's LIVE offer, or None (expired offers are absent —
        the catalog reads this per request, so expiry takes a tool out of
        the model's offer without operator action)."""
        got = self._offers.get(key_id)
        if got is None:
            return None
        if not got.live():
            self._drop_offer(key_id)
            return None
        return got

    def _drop_offer(self, key_id: uuid.UUID) -> None:
        self._offers.pop(key_id, None)
        for job in self._queues.pop(key_id, []):
            self._fail(job.dispatch_id, "error: client tool offer expired")
        self._generation += 1

    def revoke(self, key_id: uuid.UUID) -> None:
        """Forget the key's offer + queued jobs (tests; key revocation
        relies on auth failing first)."""
        self._drop_offer(key_id)

    # -- dispatch (the relay side) ------------------------------------------

    async def dispatch(
        self,
        key_id: uuid.UUID,
        tool: str,
        arguments: dict[str, Any],
        timeout_s: float | None = None,
    ) -> tuple[bool, Any]:
        """Enqueue one tool call for the client and wait (bounded) for the
        answer. Returns (ok, content-or-error-text) — the executor's
        contract shape, so a client tool and an MCP tool are
        indistinguishable to the relay loop.

        timeout_s=None uses the key's per-tool timeout when the offer
        declares one, else the gateway default; ALWAYS clamped to
        [1, settings.client_tool_dispatch_timeout_s] — a per-tool value
        can extend the wait, never past the knob's ceiling.
        """
        offer = self.offer_lookup(key_id)
        if offer is None:
            return False, "error: no client tool offer for this key (client offline or never registered)"
        tool_def = offer.tools.get(tool)
        if tool_def is None:
            return False, f"denied: {tool!r} is not in the client's live tool offer"
        eff_timeout = timeout_s if timeout_s is not None else tool_def.timeout_s
        eff_timeout = min(
            max(float(eff_timeout or settings.client_tool_dispatch_timeout_s), 1.0),
            settings.client_tool_dispatch_timeout_s,
        )

        queue = self._queues.setdefault(key_id, [])
        if len(queue) >= MAX_PENDING_PER_KEY:
            return False, "error: client dispatch queue full (client not polling)"
        job = DispatchJob(
            dispatch_id=uuid.uuid4().hex,
            key_id=key_id,
            tool=tool,
            arguments=dict(arguments or {}),
            dispatched_at=time.time(),
        )
        loop = asyncio.get_running_loop()
        future: asyncio.Future[tuple[bool, Any]] = loop.create_future()
        self._pending[job.dispatch_id] = _Pending(job=job, future=future, deadline=time.monotonic() + eff_timeout)
        queue.append(job)
        self._generation += 1
        try:
            return await asyncio.wait_for(future, timeout=eff_timeout)
        except TimeoutError:
            return False, f"error: client tool timed out after {eff_timeout:.0f}s (client offline or busy)"
        finally:
            # Pop our own bookkeeping on EVERY exit path (resolved, timed
            # out, or cancelled) so a finished dispatch never leaks.
            self._pending.pop(job.dispatch_id, None)
            self._generation += 1

    # -- poll (the client side) ---------------------------------------------

    def claim(self, key_id: uuid.UUID) -> DispatchJob | None:
        """Atomically pop the next dispatch job for this key.

        THE dispatch-once guarantee: the pop happens here, synchronously,
        before any response is written — a second poll (another tab, a
        retry after a dropped response) can never see a claimed job. The
        claimant that crashes before answering lets the job time out; the
        relay side already holds a bounded future (documented tradeoff:
        no redelivery — a duplicate execution of a side-effect tool is
        the worse outcome).
        """
        queue = self._queues.get(key_id)
        while queue:
            job = queue.pop(0)
            self._generation += 1
            pend = self._pending.get(job.dispatch_id)
            if pend is None or pend.settled:
                continue  # raced with timeout/cancel — drop, never redeliver
            return job
        return None

    # -- result (the client side) -------------------------------------------

    def settle_result(self, key_id: uuid.UUID, dispatch_id: str, ok: bool, payload: Any) -> bool:
        """Deliver ONE answer to a pending dispatch. Returns False when the
        id is unknown, expired, or already settled (the routes layer maps
        that to 409 — replay denial: the first answer wins, a replayed one
        is refused and dropped)."""
        pend = self._pending.get(dispatch_id)
        if pend is None or pend.settled:
            return False
        if pend.job.key_id != key_id:
            # Cross-key answer: refuse without leaking that the id exists.
            return False
        pend.settled = True
        if not pend.future.done():
            if ok:
                pend.future.set_result((True, payload))
            else:
                text = payload if isinstance(payload, str) and payload else "client tool refused"
                pend.future.set_result((False, f"error: {text}"))
        self._generation += 1
        return True

    def _fail(self, dispatch_id: str, message: str) -> None:
        pend = self._pending.get(dispatch_id)
        if pend is None or pend.settled:
            return
        pend.settled = True
        if not pend.future.done():
            pend.future.set_result((False, message))
        self._generation += 1

    def reap_expired(self) -> int:
        """Fail dispatches whose client never answered past their deadline
        (safety net; `dispatch` itself already bounds the wait via
        asyncio.wait_for — this reaps the bookkeeping a waiter raced).
        Returns the reaped count."""
        now = time.monotonic()
        expired = [did for did, p in self._pending.items() if not p.settled and p.deadline <= now]
        for did in expired:
            self._fail(did, "error: client tool timed out (deadline reaped)")
        return len(expired)


hub = ClientToolHub()
"""Process-wide channel state. In-memory BY DESIGN this wave (design doc
§security): a restart empties offers and pending jobs — clients
re-register on their next loop, pending dispatches resolve as tool
errors. Persistence (offers surviving restarts) is wave B2's explicit
decision, gated on whether multi-replica routing needs it at all."""


def validate_offer_tools(raw: Any) -> tuple[list[ClientTool], str]:
    """Parse/validate a register body's tools array. Returns (tools,
    error) — error empty on success. Bounds map to the error contract the
    routes layer turns into 400/413 (schema too big -> 'too_large' code)."""
    if not isinstance(raw, list) or not raw:
        return [], "tools must be a non-empty list"
    if len(raw) > MAX_TOOLS:
        return [], f"too many tools (max {MAX_TOOLS})"
    out: list[ClientTool] = []
    for t in raw:
        if not isinstance(t, dict):
            return [], "each tool must be an object"
        name = t.get("name")
        if not isinstance(name, str) or not TOOL_NAME_RE.match(name):
            return [], f"tool name {name!r} must match {TOOL_NAME_RE.pattern}"
        if name in {o.name for o in out}:
            return [], f"duplicate tool name {name!r}"
        desc = t.get("description")
        schema = t.get("inputSchema")
        if schema is None:
            schema = {"type": "object"}
        if not isinstance(schema, dict):
            return [], f"tool {name!r}: inputSchema must be an object"
        try:
            schema_bytes = len(json.dumps(schema, default=str).encode())
        except (TypeError, ValueError):
            return [], f"tool {name!r}: inputSchema is not JSON-serializable"
        if schema_bytes > MAX_SCHEMA_BYTES:
            return [], f"tool {name!r}: inputSchema exceeds {MAX_SCHEMA_BYTES} bytes"
        if desc is not None and not isinstance(desc, str):
            return [], f"tool {name!r}: description must be a string"
        timeout_s = t.get("timeout_s")
        if timeout_s is not None and (not isinstance(timeout_s, (int, float)) or isinstance(timeout_s, bool)):
            return [], f"tool {name!r}: timeout_s must be a number"
        out.append(
            ClientTool(
                name=name,
                description=desc or "",
                input_schema=schema,
                timeout_s=float(timeout_s) if timeout_s is not None else None,
            )
        )
    return out, ""


# ---------------------------------------------------------------------------
# Relay integration (the executor adapter + catalog entry)
# ---------------------------------------------------------------------------

CLIENT_PSEUDO_SERVER = "client"
"""The catalog pseudo-server name client-offered tools ride under. The
model sees `client__bash` next to `logsearch__search_logs` — same
`<server>__<tool>` grammar, so parse_tool_name and the offer-partition
logic need NO special cases. The namespace also makes a name collision
between a client tool and a granted MCP tool impossible (client__bash vs
logsearch__bash), and shadow_client_mcp never touches it (it matches on
TOOL name against the MCP catalog; the pseudo-server is not in that
catalog)."""


def catalog_entry(offer: ClientOffer) -> list[dict[str, Any]]:
    """The relay-catalog entries for one live offer: same shape as the MCP
    catalog's ({server, tool, description, inputSchema?}) so the model's
    tools_payload builder and every downstream consumer need no changes.
    schema fidelity: the client's real inputSchema rides along exactly
    like an upstream MCP tool's (the approval-passthrough lesson)."""
    return [
        {
            "server": CLIENT_PSEUDO_SERVER,
            "tool": t.name,
            "description": t.description or f"{CLIENT_PSEUDO_SERVER}/{t.name}",
            "inputSchema": t.input_schema,
        }
        for t in sorted(offer.tools.values(), key=lambda t: t.name)
    ]


def build_client_tool_executor(
    key_id: uuid.UUID,
    request_id: str,
    *,
    rate_gate: Any,
    settle_fn: Any,
    trace_fn: Any = None,
    loop_guard_fn: Any = None,
    wall_clock_deadline: Any = None,
    rate_limits: tuple[int, int] | None = None,
) -> Any:
    """Build the relay executor for CLIENT-offered tools — the same gate
    discipline as `_gated_mcp_call`, adapted to the reverse channel.

    Gates, in order (mirrors routes_proxy._relay_executor):
    1. cross-request loop guard (`loop_guard_fn`, namespaced under the
       SAME "client" server fingerprint the handback path uses);
    2. RPM rate gate (`rate_gate` — the tool calls consume 0 tokens, this
       is purely the RPM axis);
    3. dispatch (bounded — per-tool timeout clamped to the knob; when
       `wall_clock_deadline` is given (a monotonic time), the dispatch is
       additionally clamped to the relay's REMAINING wall-clock budget so
       a client tool can never extend the relay past the ingress
       clean-close guarantee);
    4. settle under `local:<tool>` (ledger truth: a client execution is
       its own endpoint kind, never billed as an upstream MCP call);
    5. trace under server="client" (the console reads one story for
       client executions, handback or dispatched).

    rate_gate/settle_fn are injected (not imported) so this module stays
    unit-testable without the DB layer — the routes layer passes
    budgets.check_rate_limit / budgets.settle / traces.record; tests pass
    recorders.

    B2 ADAPTER NOTE (the one signature adjustment the production wiring
    needed): `rate_limits=(rpm, tpm)` — the real `check_rate_limit` needs
    the KEY's configured limits (the MCP executor reads them off the key
    record it closes over; this adapter cannot reach that record), so the
    routes layer passes the pair and the gate call is
    `rate_gate(key_id, rpm, tpm, 0)`. None (spike callers and their gate
    doubles, which ignore the arguments) keeps the historical
    `rate_gate(key_id, 0, 0, 0)` shape byte-for-byte.
    """

    async def execute(server: str, tool: str, arguments: dict[str, Any]) -> tuple[bool, Any]:
        started = time.monotonic()
        endpoint = f"local:{tool}"
        if server != CLIENT_PSEUDO_SERVER:
            # Adapter misuse guard: the relay only routes pseudo-server
            # calls here when the flag + offer are live; anything else is
            # a routing bug, refused rather than mis-dispatched.
            return False, f"denied: {server}/{tool} is not a client-served tool"

        if loop_guard_fn is not None:
            reason = loop_guard_fn(tool, arguments)
            if reason:
                await settle_fn(key_id, "*", endpoint, 0, 0, "error", 0, request_id)
                return False, reason

        try:
            if rate_limits is not None:
                rpm, tpm = int(rate_limits[0]), int(rate_limits[1])
            else:
                rpm, tpm = 0, 0  # spike shape: gate doubles ignore the limits
            gate = await rate_gate(key_id, rpm, tpm, 0)
        except Exception as exc:
            # Audit fix (fail CLOSED): the call is 0-token and cheap to
            # refuse; a broken rate ledger previously turned the RPM burst
            # guard off silently. Settle + refuse so a DB incident cannot
            # open a burst window on client-executed tools.
            log.warning(
                "clientside: rate gate failed (%s) — dispatch refused fail-closed",
                type(exc).__name__,
            )
            await settle_fn(key_id, "*", endpoint, 0, 0, "error", 0, request_id)
            return False, "error: rate gate unavailable — dispatch refused, retry shortly"
        if gate is not None and not gate.ok:
            await settle_fn(key_id, "*", endpoint, 0, 0, "error", 0, request_id)
            return False, f"rate limited: {gate.reason}"

        # Bound the dispatch to the per-tool timeout AND the relay's
        # remaining wall clock (whichever is smaller — a client tool can
        # extend NEITHER the knob nor the ingress guarantee).
        timeout_s = settings.client_tool_dispatch_timeout_s
        if wall_clock_deadline is not None:
            remaining = float(wall_clock_deadline) - time.monotonic()
            timeout_s = min(timeout_s, max(1.0, remaining))
        ok, content = await hub.dispatch(key_id, tool, arguments, timeout_s=timeout_s)
        ms = int((time.monotonic() - started) * 1000)
        status = "ok" if ok else "error"
        try:
            await settle_fn(key_id, "*", endpoint, 0, 0, status, ms, request_id)
        except Exception:
            log.exception("clientside: settle failed (%s)", endpoint)
        if trace_fn is not None:
            try:
                await trace_fn(tool, status, ms)
            except Exception:
                log.exception("clientside: trace failed")
        return ok, content

    return execute
