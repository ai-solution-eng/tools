# Async tool execution at the gateway

**Status:** design accepted for review; phase 1 (the job registry) implemented.
**Author:** design session 2026-11-20.
**Code:** `src/pcai_llm/core/tooljobs.py` (phase 1), `tests/test_tooljobs.py`.
**Supersedes:** nothing. Related: ROADMAP "Client-dispatch persistence" and
"MCP spec-watch: retire the long-poll dispatch endpoints".

---

## 1. The problem, in numbers

A relay tool call is bounded by the **ingress envelope**, not by the tool:

| Bound | Value | Where |
|---|---|---|
| Relay wall clock | min(`tool_relay_wall_clock_s`, `upstream_timeout_s` - 30 s) | `core/toolrelay.py:846` |
| - code defaults | **570 s** (600 - 30) | `config.py:58,338,343` |
| - G2 values | **870 s** (900 - 30) | `values.g2.yaml:143,205` |
| Istio virtualService | **960 s** | `values.g2.yaml:581` |
| Client patience | **30-60 s** ("most clients' 30-60s timeout") | `routes_mcp.py:1651` |
| MCP SDK session read | `upstream_timeout_s` (900 on G2) | `routes_mcp.py:577` |

Past the wall clock, `core/toolrelay.py:3640` cancels the call and the model is
told `"error: tool execution exceeded the relay wall-clock budget"`. **The work
is lost.** A 20-minute lake query pays 14 minutes and receives an error string.

Two fleet MCP servers have already worked around this by growing their own job
stores - SQLhandler (`src/sqlhandler/jobs.py`) and MultimodalRAG
(`_UploadJobTracker`). Both hit the same scale-out bug: a per-process registry
means a poll that lands on another replica answers "unknown job id"
(SQLhandler's live 2026-09 incident: 3 of every 4 status calls 404'd on a
4-replica release).

**The generalization is worth doing at the gateway because the gateway is the
only component that owns all four of: the deadline, the identity, the resume
substrate, and the audit/billing record.** A server-side quartet owns none of
them.

---

## 2. What exists today (and why none of it can carry a call)

| Substrate | What it does | Why it cannot carry a detached tool call |
|---|---|---|
| **Handback** (`canonical_conversations.pending` + `X-PCAI-Continue`) | Suspends a TURN, persists the awaiting tail, resumes with tool results | Ends the CALL inside the request; only the turn crosses requests |
| **Client dispatch** (`/v1/dispatch/*`, `core/clientside.py`) | Real job protocol: enqueue, atomic claim, out-of-band result | Parks its caller on `asyncio.wait_for` (`clientside.py:255`); in-memory; flag-off |
| **SQLhandler quartet** | Job id, poll, fetch-once, cancel | Invisible to the gateway: no idempotency, no affinity, a retried submit mints a second job |

So the gateway has never actually detached a tool call. What it HAS proven is
the hard half - **a turn can be suspended and resumed across requests with its
budget intact** (`schema.py:1120-1129`). Detaching a call is a smaller step
than it looks, provided the result can re-enter at the one seam that already
exists (`toolrelay.py:3660`).

---

## 3. Design principle

> **The model should not schedule. It should ask, and get an answer when the
> answer exists.**

Polling inside the model loop is the wrong shape: it burns turns and context,
it is non-deterministic (models forget to poll, or poll forever), and it makes
every async-capable server pay for four extra tools in the prompt. The gateway
can do the scheduling invisibly because it owns the loop.

That is the difference between what SQLhandler built (necessary, because a
stateless MCP server has no other option) and what the gateway should build.

---

## 4. Three delivery modes

The user-facing goal - *"let the model check in for results while continuing
work"* - is met by all three, in increasing order of visibility. Mode A is the
default and is **invisible to the model**.

### Mode A - opportunistic injection (default)

1. A tool call is still running at the soft deadline (`tool_relay_detach_after_s`,
   default 90 s).
2. The relay **detaches** it: a job row is created, the outbound MCP call keeps
   running in a gateway-owned background task, and the model receives a typed
   pending tool result:

   ```json
   {"pcai_job": {"job_id": "...", "state": "working", "note":
     "still running; the result will be delivered automatically. Do not re-issue this call."}}
   ```

3. The loop continues. The model does other work in the same turn.
4. When the job completes, the relay injects its result as a synthetic
   `role: tool` message **before the next model call**. No polling.

The pending result's `note` is load-bearing: without an explicit "do not
re-issue", a model that sees a non-result re-issues the call and duplicates the
work. This is the single most important string in the whole design.

### Mode B - resume (turn ended first)

If the loop ends before the job completes, the pending jobs are persisted with
the handback tail. The client resumes with `X-PCAI-Continue`; the relay injects
any finished results and re-attaches waits for unfinished ones. Reuses the
existing substrate end to end - no new client protocol.

### Mode C - explicit check-in (the model asks)

One generic meta-tool, **not four per server**:

```text
pcai_jobs(action: "list" | "status" | "result" | "cancel", job_id?)
```

This mirrors the existing `pcai_tools` hybrid-surface precedent (one discovery
tool carrying a server directory, rather than 111 inline tool definitions -
`values.g2.yaml:196-203`). It is the escape hatch for an impatient model, and
the **only** available mechanism on the stateless `/mcp` surface.

---

## 5. Wire alignment: use the MCP task shape

Do not invent a fifth dialect. The tool interface already has the slot, and the
SDK in this repo implements it:

| Piece | Shape | Source |
|---|---|---|
| Per-tool declaration | `Tool.task_support: "forbidden" \| "optional" \| "required"` | `mcp_types` 2025-11-25 |
| Call opt-in | `tools/call` params gain `task: {ttl}`; returns `CreateTaskResult{task}` immediately | same |
| Task object | `task_id, status, status_message, created_at, last_updated_at, ttl, poll_interval` | same |
| Status | `working \| input_required \| completed \| failed \| cancelled` | same |
| Methods | `tasks/get`, `tasks/result`, `tasks/cancel`, `tasks/list` | same |
| Linkage | `_meta["io.modelcontextprotocol/related-task"]` | same |

**Revision note (verify before quoting externally):** tasks are core in the
2025-11-25 revision and demoted to the `io.modelcontextprotocol/tasks`
**extension** in 2026-07-28 - the revision this fleet speaks
(`MODERN_PROTOCOL_VERSIONS = ("2026-07-28",)`, stateless per-request envelope).
The SDK's SEP-2133 extension mechanism (`mcp/server/extension.py`) uses
`tasks/get` as its documented example of a contributed method. Evidence is the
installed SDK, not a fetched spec page.

**Consequence:** the gateway's job states ARE the spec's states
(`core/tooljobs.py` design commitment 4), so the same rows can answer
`tasks/get` later without a translation layer.

---

## 6. Lifecycle

```text
                 create()
                    |
                    v
              +-----------+
              |  working  |<-------------+
              +-----------+              |
                |      |                 | (input_required
   complete()   |      |  request_cancel()|  -> working)
                |      |  (flag only)     |
                v      v                 |
          completed   [cancel_requested]  |
          failed      effective_state()   |
          cancelled   reports cancelled   |
                    ^                    |
                    +--------------------+
                     input_required
```

- **Terminal states are immutable**, enforced in SQL (`state <> ALL(TERMINAL)`),
  not by callers.
- **Cancel is a flag, not a state.** "We asked, the work may still be unwinding"
  is a moment every cancel passes through; modelling it as a state forces every
  reader to handle a phantom. `effective_state()` answers the poller's real
  question ("will I get a result?") immediately.
- **The result is claimed once** (`result_claimed_at IS NULL` in the UPDATE
  predicate). A replayed request cannot inject the same tool result twice.

---

## 7. Schema (phase 1, implemented)

See `core/tooljobs.py:DDL`. Columns: `job_id, key_id, server, tool,
args_sha256, state, status_message, cancel_requested, result_body,
result_bytes, result_claimed_at, created_at, updated_at, expires_at`, plus a
state CHECK and two partial indexes (active-list, expiry sweep).

Deliberate choices:

- **Postgres, not memory.** ROADMAP already rejected stateful `/mcp` because it
  "sacrifices the any-replica property for the whole federation"; an in-memory
  hub would reintroduce that for tool calls.
- **No `queued` state.** A job exists only once it is working: the row IS the
  acceptance. (SQLhandler's `submit` can block 30 s on a concurrency slot and
  has no `queued` state - a client that times out mid-submit does not know
  whether a job was created. Not inheriting that.)
- **Owner scope = API key**, and a wrong key is indistinguishable from a bogus
  id. A job id is a bearer capability; distinguishing "not yours" from "not
  there" confirms existence to a guessing caller.

---

## 8. The seam

Two anchors, twenty lines apart in one function:

| What | Where | Change |
|---|---|---|
| Blocking wait + timeout | `toolrelay.py:3640-3643` | On soft-deadline expiry: detach instead of `TimeoutError` |
| Result re-entry | `toolrelay.py:3660-3666` | Also the injection point for a completed job |
| Executor | `routes_proxy.py:2802` (`execute(server, tool, args) -> (bool, Any)`) | Widen to a third outcome: pending |

The executor contract (`Awaitable[tuple[bool, Any]]`) is shared with
`core.clientside` and faked by `25 tests`; widening it is the highest-blast-radius
change in the design and belongs behind a flag in phase 2, not phase 1.

---

## 9. Four things that must be rebuilt

These are the reasons this is not a small change.

1. **Settlement happens strictly after the result.** `routes_proxy.py:2787` and
   `routes_mcp.py:1349` settle only once the call returns; there is no
   "accepted" row. A detached job must settle **at completion**, from the
   background task, with the job id as the ledger `request_id`. Otherwise every
   detached call silently under-reports.
2. **`mcp_call_audit` is an append-only hash chain** (`schema.py:1289-1318`)
   with outcome vocabulary `ok|error|denied|policy_blocked` and no pending
   state. A row cannot be updated to "completed". **Fix: two rows, not one
   mutation** - an `accepted` row at detach and a terminal row at completion,
   linked by a new nullable `job_id` column. This is the same shape as MCP's
   `related-task` metadata, which is not a coincidence.
3. **Budget is charged before dispatch** (`toolrelay.py:974-988`: "Charge the
   WHOLE chargeable batch before deciding"). Keep that - the work *was* asked
   for - but cap detaches per turn (suggest 4) so a model cannot detach 32 units
   of work and end the turn.
4. **ACL must be re-checked at injection.** The grant may be revoked while the
   job runs. Injecting a result into a conversation whose key lost the grant is
   an ACL bypass. Fail closed: deliver a tool error, discard the result.

Plus one lifecycle rule: **a detached job does not survive a gateway restart**
(the worker is an in-process `asyncio` task). The startup sweep must fail all
non-terminal rows with "gateway restarted" rather than leaving them `working`
forever. This is strictly better than today, where the work dies anyway - but it
must be documented, because it is why this is working state and not a durable
queue.

---

## 10. Bounds and failure modes

| Condition | Behaviour |
|---|---|
| Per-key active jobs exceed `MAX_ACTIVE_PER_KEY` (64) | `create()` returns None; relay surfaces a normal tool error the model can route around |
| Result exceeds `MAX_RESULT_BYTES` (4 MiB) | Job **fails** with a reason; never store a truncated (corrupt) body |
| Client never resumes | TTL reaper deletes the row; the result is never claimed. Wasted work, bounded by TTL |
| Gateway restarts mid-job | Startup sweep fails non-terminal rows |
| Upstream call hangs past its own timeout | Job fails; the row is the only record (today: nothing is recorded) |
| Cancel requested, worker never observes | Row expires via the reaper; a lost cancel cannot pin a row |
| Two replicas complete the same job | One wins by SQL predicate; the loser returns False and discards |

---

## 11. Phases and gates

| Phase | Scope | Gate |
|---|---|---|
| **0. Measure** | Tool-call duration histogram; count of guard-timeout events per week; how often SQLhandler's quartet is actually used | If detaches would be <0.1% of calls, stop - standardize the quartet instead and revisit |
| **1. Registry** (this change) | `core/tooljobs.py` + DDL + tests. Inert, unwired | Review. No behaviour change by construction |
| **2. Relay detach** | Soft deadline, background task, opportunistic injection, settlement, audit rows, startup sweep. Flag `tool_relay_async_jobs`, default **off** | Shadow mode first: log what WOULD detach, verify against phase 0 numbers |
| **3. Check-in + resume** | `pcai_jobs` meta-tool; injection on `X-PCAI-Continue` | End-to-end with DSH as the client |
| **4. MCP tasks surface** | `tasks/get|result|cancel|list` on `/mcp`; forward `task` augmentation when a server declares `task_support` | Third-party server interop |

Phase 1 is deliberately inert so it can land while phases 2-4 are still being
argued about.

---

## 12. Non-goals

- **Not a general task queue.** No worker pool, no Celery, no cross-restart
  durability. This exists to survive one ingress timeout, not to be a scheduler.
- **Not async-by-default.** Opt-in per tool/server plus a soft deadline; the 95%
  of calls that return in under a second must not pay a resume round-trip.
- **Not a replacement for SQLhandler's `QueryJob`.** Its DuckDB
  `interrupt()` cancel and its submit-time read-only guard are real semantics a
  generic wrapper can only approximate. The quartet becomes *optional*, not
  wrong.
- **No model polling loop** on the relay path.

---

## 13. Open questions

1. **Soft deadline value.** 90 s is a guess; phase 0 should set it from the
   duration histogram (suggest: p99 of successful calls, floored at 60 s).
2. **Per-key opt-in?** Detach changes billing timing and audit shape; some
   operators may want it off for specific profiles.
3. **Result retention.** Claim-once is right for injection, but a human-facing
   console may want to re-read a result. Consider per-tool `result_retention:
   once | ttl`.
4. **Does `tasks/*` land on `/mcp` before or after the relay path?** The relay
   path serves the fleet's own harnesses; `/mcp` serves third parties. Relay
   first is my recommendation.
