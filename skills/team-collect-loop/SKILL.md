---
name: team-collect-loop
description: Use when waiting on or collecting results from running teammates — during an audit wave, remediation wave, or any fan-out where several agents run in parallel and the lead must gather reports without duplicating or losing any. Message-driven collection first (completion notices arrive as lead messages — do not busy-wait); chunked waits only as a fallback (no-wakeup delivery, explicit user request) or as the heartbeat for unattended/autonomous runs; per-agent report file conventions; completion reconciliation. Companion to subagent-teams (spawn discipline) and ptc-team-runtime (call mechanics). Use ptc-team-runtime for a single tool call's mechanics; use THIS when a multi-agent wave is in flight.
---

# team-collect-loop — gathering a fan-out without duplicates or losses

## The collection model (Mode A is the default)

Two collection modes exist, and conflating them wastes turns or misses reports.

**Mode A — message-driven (the DEFAULT).** The default for every operation with a
human on the other end. In this deployment, teammate completion notices and
reports arrive as **team messages to the lead** — the same channel as everything
else in the turn stream. All five audit reports and all implementer completion
reports in the 2026-10-16 SQLhandler wave arrived this way, unrequested. In this
mode the collect loop is:

1. **Do not busy-wait.** When you have nothing else to do, END YOUR TURN and let
   the messages wake you. The harness prompts you on arrival.
2. Handle each report as it lands: spot-check (lead duty), route cross-zone items,
   log on the task board, retire the agent if finished.

If you catch yourself polling in Mode A you are double-waiting — stop and let
messages drive.

**Mode B — chunked waits (fallback / autonomous heartbeat).** Use it ONLY when:

- notices do NOT arrive as messages (the delivery mechanism lacks a wakeup), or
- the user explicitly asked you to keep waiting (e.g. "let DeepSeek work" — the
  chunk is the politeness mechanism, not a necessity), or
- the run is **unattended/autonomous** — then the bounded chunk is a
  **heartbeat**: it exists to enforce budgets (the silent-teammate nudge below,
  wave deadlines), NOT to poll for reports, which still arrive as messages.

```js
// one run_code call = one chunk
const agents = await tools.list_agents({});          // re-list FIRST
const w = await tools.wait_agent({timeout_ms: 90000}); // never above ~100000
// on ANY outcome — wakeup / noProgress / timeout / abort:
//   next run_code call starts at step 1. NEVER spawn a replacement here.
```

`run_code` kills the program at ~120s, so a larger timeout is a guaranteed program
kill. `{"timedOut":true}` chunks are normal for long auditors — just loop.

**Choosing:** at wave launch, ask "do completion notices arrive as messages that
wake the lead in this deployment?" If yes (probe once if unsure), you are in Mode
A — park and respond. If you are in Mode B and notice reports are ALSO arriving
as messages, you have been double-waiting — stop polling and let messages drive.

## Never wait blind past the budget

Regardless of mode, if a teammate has been silent for ~20 minutes with zero
completions on a wave, send ONE `send_message` asking for a **partial report**
("send what is already evidence-backed; continue afterward"). Partials surface
"stuck reading a 3000-line file" early and cost one message. Do not re-send.

## Per-agent report convention (set at SPAWN time)

For waves over ~3 agents, have each teammate END by writing its report to a file,
and say so in the spawn prompt:

> "Finish by writing your full findings report to `.scratch/<wave-name>/impl-<your-name>.md`
> (dir exists). Your final message may be a one-line pointer to the file."

Why: completion messages are a one-shot, context-bounded channel — five 8KB reports
can overflow the lead's turn. Files are re-readable, greppable, and survive lead
context compaction. Note: some teammates have **no send_message tool** — they reply
via the report file; the lead must read files as the primary channel for those.
Announce the convention in the spawn prompt and it just works.

## Reconciliation rules

- **Completion notice** → authoritative. Verify the report exists (file or message
  body), record it on the board, retire the agent (interrupt) once integrated.
- **Registry says running but a notice arrived** → trust the notice.
- **noProgress / timeout / abort** → normal; wait for messages (Mode A) or loop
  (Mode B — fallback/heartbeat only). An abort with a useless payload is a
  serialization gap, not agent death.
- **Long silence** → one partial-report nudge (see budget rule). If a teammate is
  demonstrably producing nothing AND cannot be reached, only then consider a
  replacement — with a prompt noting a prior instance may exist (subagent-teams).
- **Writers only:** before integrating or re-spawning anything that writes files,
  run `guard-uncommitted-work`'s four-step diff check first.

## Cross-zone routing during collection (leads of parallel implementers)

Collecting is not passive. For every report that lands, the lead routes each
"not mine / outside my zone" item to the owner with a targeted `send_message`
before the next wait — otherwise the last implementer finishes and blocked items
are only discovered at the full-suite check. The 2026-10-16 wave needed three
routing rounds (config→engine rawfiles unblock; engine→server dispatch/observability;
writes→server run_sql caller); each was cheap because it was routed immediately.

## Capacity and reuse

- Teammate slots are limited and include inactive members (8 in this deployment,
  immutable per team; no removal API). **Prefer reactivating an inactive teammate
  via `send_message`** — an audit-zone auditor becoming that zone's implementer or
  reviewer carries unmatched context. Spawn new members only for genuinely new roles.
- Delegation depth is 1; fan out only as wide as you will actually read.

## Anti-patterns

- Busy-polling `wait_agent`/list in Mode A — the message wakes you; polling burns
  turns. Mode B waits are for no-wakeup delivery, explicit user requests, and the
  autonomous heartbeat only.
- One `wait_agent` at 600000ms — guaranteed program timeout.
- Re-spawning after an abort/timeout — re-list first; the agent is almost always alive.
- Collecting five big reports through messages instead of files.
- Trusting `list_agents` status over the completion notice.
- Letting "not my zone" items pile up until the end of the wave — route immediately.
