---
name: subagent-teams
description: Use when about to spawn any subagent or reviewer model, when a spawned subagent goes invisible or its job id stops resolving, or when stopping or collecting spawned agents. Requires durable agent-team teammates over anonymous background children, with spawn → verify-addressability → collect discipline, because an invisible spawn plus a blind retry is how the same agent gets set up twice.
---

# subagent-teams (spawn discipline for subagents)

## The standing rule

**Default every spawn to a durable agent-team teammate (`spawn_teammate`).** Only durable teammates
are addressable: `list_agents`, `send_message`, `wait_agent`, and `interrupt_agent` see **only**
teammates. Anonymous background subagent children are fire-and-forget — not listed, not messaged, not
interruptible — and their job id may not even resolve in `job_list`. When one goes invisible you
cannot tell "running" from "lost", so the retry you spawn becomes a **duplicate agent** doing the
same job a second time.

Reference incident (2026-10-06, cross-validation reviewer session): a reviewer spawned as an
anonymous background subagent. Its job id returned `Error: unknown job 5941b56a-…`, `list_agents`
showed only the lead, and the wait reported `No active subagents` — while the reviewer was in fact
running. The lead re-spawned synchronously and the SAME review ran twice (two cards, 7m19s + 6m16s).
A duplicate is not just double cost: two verdicts on one artifact invite cherry-picking the
convenient one.

## The path: how team tools are reached in THIS deployment (PTC mode)

This deployment runs the `ptc` agent preset: `run_code` is the ONLY tool callable directly — a tool
call naming any other tool fails. Team tools are reached inside a program via the SDK:
`await tools.spawn_teammate({...})`, `await tools.list_agents({})`, `await tools.send_message({...})`,
`await tools.wait_agent({...})`, `await tools.interrupt_agent({...})`, `await tools.team_task_*({...})`.

**Do not conclude a capability is missing just because it is absent from the declared function list.**
The declared list shows only `run_code` (plus the few host-injected functions); the SDK catalog inside
the system prompt is the real surface. Reference misdiagnosis (2026-10-10): a session whose summary
claimed "no subagent-spawn capability" — while the session's own system prompt said "Agent Teams is
available in this session" and `tools.list_agents({})` succeeded on first probe. Before ever claiming
a spawn tool is missing, (1) check the session log for "Agent Teams is available", (2) probe
`tools.list_agents({})`.

Also note the Agent Teams profile bundle deliberately DISABLES the plain `subagent`/`subagent_fork`
tools (they are replaced, not broken). Their absence from the SDK catalog is by design, not a gap.

Verify the surface live, never from memory: `tools.list_agents({})` returns the roster (lead +
teammates) or throws — both are informative. Only if that throws AND the session log lacks the
Agent Teams block is the plugin genuinely not mounted for the session.

## Route choice (pick deliberately, every spawn)

| Need | Use |
|---|---|
| A result you must collect, steer, or stop (reviewers, implementers, auditors) | **durable teammate** (`spawn_teammate`) — the default |
| One blocking result, nothing else — no continuation, no interruption | one-shot **synchronous** subagent (`run_in_background: false`); the call does not return until the result does, so it cannot go invisible |
| True fire-and-forget with a file artifact | anonymous background child **only**, with the report path written into the prompt |

Fire-and-forget caveat: "no report materialized" means NOT DONE, not "still working". Reference
precedent (2026-10-03): four subagents on a pinned route echoed a ready line but no report landed in
~40 minutes — treat such waves as UNVERIFIED until the artifact exists.

## Schema discovery: __schema, never a failed call

`await tools.__schema()` lists every callable SDK tool with its description and full parameter
schema; `await tools.__schema("spawn_teammate")` returns one. Errors are serialized losslessly —
a thrown object now reaches you as JSON, not `[object Object]`. Before inventing a parameter
name or guessing required fields, call `__schema` for the tool first; a failed call is a bug in
your preparation, not a documentation source.

## Pinned LLM routes (since the 2026-10-10 teammate-routing fix)

`spawn_teammate` accepts optional `provider` + `model` (+ `reasoning_effort`) for the teammate's OWN
LLM route. Pin `provider` and `model` TOGETHER (same rule as `subagent`); omit both to inherit the
Lead's route. Verify the pin landed: the spawn result's `member.model` echoes the pinned model —
`"model": "auto"` there means the pin was NOT applied (pre-fix host, or args dropped). Direction-
matched review (`cross-validate`) REQUIRES the pin: without it every teammate rides the Lead
cascade and a "reviewer" can be the same model under an alias. Reference incident (2026-10-10,
pre-fix): two "different-cell" reviewers both ran the GLM cascade; the DeepSeek one identified
itself by guessing from its own teammate name.

## Spawn discipline (spawn → verify → work → collect)

1. **Name the teammate for its role** — `reviewer-factual-integrity`, not `agent-2`. One
   responsibility per teammate; the name is how you find it again in `list_agents`.
2. **Route the LLM correctly**: supply `provider` and `model` **together** — provider alone is a
   spawn error, and a double-prefixed `"provider/model"` string is route-not-allowed. Enumerate the
   registered providers with `list_subagent_models` FIRST (ids are deployment-specific; a stale id
   silently routes wrong or fails), and check the model's advertised reasoning efforts before passing
   `reasoning_effort` — an unsupported effort is a hard spawn error, not a silent downgrade.
3. **Verify addressability immediately after the spawn**: `list_agents` must show the new member
   with an addressable target. Tool returned an id but the member is not listed → **do NOT spawn
   again**; that is the duplicate trap (see below).
4. **Record the target** in the todo list / task notes at spawn time. The spawn result is often the
   only durable handle for the member; "I'll remember the id" is how duplicates happen.
5. **Collect through the team loop**: messages first (end the turn; completion notices arrive as lead messages — see team-collect-loop Mode A), `wait_agent` chunks only as fallback; `send_message` for
   follow-ups; `interrupt_agent` to stop. **Completion notices are authoritative; the `list_agents`
   registry goes stale** — it has shown 'running' after agents finished and 'ready' after they
   closed. Trust the notice/result over the registry status.
6. **Retire finished reviewers.** Once the verdict is collected and integrated, the teammate is
   done — interrupt it or leave it inactive; never re-prompt it "just in case".

## When a spawn goes invisible (recovery, in order)

1. **Re-list, never re-spawn first**: `list_agents`, then `job_list`, then one bounded
   `wait_agent`. A "not found" job id usually means the child runs outside the job registry, not
   that it is dead.
2. If the agent **can write files** and may still be running, read `guard-uncommitted-work` BEFORE
   any second spawn — a duplicate writer on the same zone is a rollback hazard, not just a cost.
3. If it stays unreachable, reconcile against a **visible artifact** instead of a second live agent:
   the report file, the shared task board, `git status` in its zone. Only when you can show the
   first instance produced nothing (and cannot be stopped) spawn the replacement — and say in the
   replacement's prompt that a prior instance may exist.

## Capacity and contention

- Delegation depth is 1 — teammates cannot spawn teammates — and live children are capped
  (reference harness: 8). Fan out only as wide as you will actually read.
- **Stagger parallel launches that hit the same upstream service.** Reference incident (2026-09):
  six concurrent research agents exhausted searxng-mcp's upstream engines simultaneously (all
  suspended), and one looped on retries because the error text led with
  "No results found, try rephrasing". Launch fewer, staggered agents and put a fallback instruction
  in the prompt ("on engine failure, fall back to a direct URL fetch — do not retry in a loop").

## Anti-patterns

- **Re-spawn after a visibility failure** — re-list first; the first instance is usually alive.
- **Fire-and-forget a decision-point review** — a reviewer you cannot message or stop is a coin
  flip, not a reviewer.
- **Trusting the registry over the completion notice** — `list_agents` status goes stale in both
  directions.
- **`provider` without `model`**, a double-prefixed route string, or `reasoning_effort` on a model
  that does not advertise it.
- **Two live reviewers on one artifact** — one reviewer, one verdict, spot-checked against machines.

## Handoff

Prompt hygiene for spawned agents (verbatim git prohibition, write zones, timeout-wrapped tests):
`guard-uncommitted-work`. Choosing WHICH model reviews: `cross-validate`. Running a parallel wave:
`audit-wave` — which also owns zone partitioning via the shared task board. This skill governs only
HOW agents are set up and collected, not what they do.

## Reference instantiation

One harness's tools and limits — re-derive locally:

- Team tools verified by probing the live registry (never by trusting a prompt's declaration
  list) — reached via `await tools.*` inside `run_code` (PTC mode; see the path section above):
  `spawn_teammate`, `list_agents`, `send_message`, `wait_agent`, `interrupt_agent`,
  `team_task_*`. `subagent`/`subagent_fork` are DISABLED by the Agent Teams profile bundle
  (replaced by teammates — their absence is by design, not a gap).
  `list_subagent_models` and `job_list`/`job_output`/`job_kill` remain in the SDK catalog
  where the deployment mounts them.
- Limits (reference harness, verified 2026-10): max 8 live continuable children, delegation depth 1;
  tool bindings accept lossless-JSON arguments only — pass `{}`, never a bare undefined.
- Providers verified 2026-10-04 via `list_subagent_models`: `pcai-model-endpoint-flow` (models
  `Deepseek-V4.1-Flash-1017`, `GLM-5.3-Flash`, `Qwen3.8-27B`, `auto`) and `glm53-flash-sproket` (model
  `glm-5.3-flash`).
- The 2026-10-06 incident's exact tell: spawn "succeeded", then `unknown job <uuid>` from job
  output, `list subagents: 1 agents` (lead only), `0 background jobs`,
  `Wait for subagent: No active subagents` — while the UI showed the subagent running.
