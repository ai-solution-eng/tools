---
name: ptc-team-runtime
description: 'Use when calling any agent-team tool (spawn_teammate, list_agents, send_message, wait_agent, team_task_*, list_subagent_models) or any SDK tool from run_code in PTC mode. Covers the invocation path, the 120-second program deadline, collection modes, and return-shape landmines. NOT for choosing which agent or model to spawn (that is subagent-teams), NOT for the collect strategy across a whole wave (that is team-collect-loop), and not for ordinary non-agent tool use.'
---

# ptc-team-runtime — agent-team tools under the PTC runtime

## The invocation path (the only one that works)

In this deployment the session runs the `ptc` preset: **`run_code` is the only callable
tool.** Team tools do not exist as direct tool calls — the system prompt's
"Use list_agents({})" means:

```js
// inside run_code:
const agents = await tools.list_agents({});
```

The full surface is on `tools.*`: `spawn_teammate`, `list_agents`, `send_message`,
`wait_agent`, `interrupt_agent`, `team_task_create/list/get/update`,
`list_subagent_models`, `skill`, `__schema`.

**MCP/relay tools are NOT on `tools.*`** (verified 2026-10-30). The `tools` map is
generated from the harness's own registered-tool store; gateway relay tools are
server-side and never injected. Call them as ordinary top-level tools named
`<server>__<tool>` (DOUBLE underscore) after
`pcai_tools({action:"describe", server:"<name>"})`. Both `tools.pcai_tools` and
`tools.searxng__search` raise a bare `TypeError: ... is not a function` - that
error always means wrong invocation path, never "server not configured".

**Do not conclude a capability is missing because it is absent from the declared
function list.** Probe `tools.list_agents({})` before ever claiming a spawn tool is
missing (reference misdiagnosis 2026-10-10, repeated 2026-10-16 — both cost ~10 probes).

## Rules that are easy to get wrong

1. **`__schema` before first use — every session, every tool.**
   `await tools.__schema("team_task_create")`. Parameter names drift between
   deployments (`title` vs `subject` bit both 2026-10 runs). A failed call is a bug
   in YOUR preparation, never a documentation source.
2. **Lossless-JSON args only.** Pass `{}`, never a bare `undefined`; strings not
   numbers where the schema says string.
3. **Return shapes are not objects.** `list_subagent_models` returns a plain
   **string** (one route per line, newline-separated). Treating it as an array iterates
   CHARACTERS. `String(x)` it first; slice to bound output.
4. **Errors thrown by bindings are wrapped** as `ToolCallError` with the schema
   message inside — read the `missing required property "..."` text; it names the field.

## The 120-second program deadline

`run_code` kills the program at ~120s regardless of what you asked a tool to wait
for. `wait_agent` accepts `timeout_ms` up to 3,600,000 — a value above ~100,000 is
a guaranteed program timeout. **Never pass more than ~100000.**

## Collection mode: messages FIRST, chunks only as fallback

Chunked waits are the FALLBACK mechanism, not the default. Completion notices
arrive as team messages to the lead in this deployment — when you have nothing
else to do, END YOUR TURN and let the message wake you (Mode A, per the
team-collect-loop skill). Enter the chunk loop below ONLY when notices do not
arrive as messages, the user explicitly asked you to wait, or you are
batch-collecting before a phase transition (Mode B).

```js
// Mode B only — one run_code call = one chunk
const w = await tools.wait_agent({timeout_ms: 90000});
// w may be: a wakeup (completion notice / message / task change), noProgress,
// or the call ABORTS on shutdown/race — all three are "wake and re-list" events.
```

After EVERY chunk — wake, noProgress, timeout, or abort:

```js
const agents = await tools.list_agents({});   // re-list, never re-spawn
const tasks = await tools.team_task_list({}); // board may have moved
```

Abort with a useless payload (`[object Object]`, seen 2026-10-16) is a KNOWN harness
serialization gap — treat it as "unknown, re-list". It is NOT evidence an agent died.
Completion notices are authoritative; `list_agents` status goes stale in BOTH
directions (shows running after finish, ready after close). Trust notices, verify
artifacts.

## Spawn-result verification

`spawn_teammate` echoes the route as `member.model`. `"model": "auto"` = pin NOT
applied — do not proceed on that teammate. Pin `provider` AND `model` together;
`provider` alone is a spawn error; `"provider/model"` double-prefixed is
route-not-allowed. Enumerate routes with `list_subagent_models({})` (a STRING) then
`list_subagent_models({provider: "<id>"})` before pinning, and check advertised
`reasoning_effort` values with the two-arg form — an unsupported effort is a hard
spawn error, not a silent downgrade.

## Anti-patterns

- Direct tool call `list_agents({})` outside `run_code` — fails in PTC mode.
- `wait_agent({timeout_ms: 600000})` — burns the whole program deadline, loses the
  chunk loop, produces a bare timeout with no partial state.
- Re-spawning after an abort/timeout — re-list first; the agent is almost always alive.
- Iterating a string return as if it were an array.
- Guessing a parameter name instead of `__schema`-ing it.
