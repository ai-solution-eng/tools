# Approval gate for destructive tools — design

**Status:** proposal, 2026-11-23. Not implemented.
**Related:** `core/tooljobs.py` (the durable job registry this reuses), `mcpf/arg_policy.py` (the only enforcing gate
today), `api/routes_mcp.py` (the /mcp chain), `core/chain_audit.py` (the audit chain an approval must join).

## 1. The hole, stated exactly

`InputRequiredResult` has **zero construction sites** in the tree. Every tool call — including `applygate plan_apply`,
`workbench run` and `k8s exec_in_pod` — executes on a static grant. The only thing between an agent and a cluster
mutation is the ACL, which answers "may this key call this tool", never "should this specific call happen".

What shipped in place of a gate (`require_approval: "always"`) is **client-side execution**: the gateway hands the
`tool_calls` back untouched and the client approves and runs them. That is a real control for a cooperative client, and
it is off by default, per request, and records nothing. It is not a gateway gate.

## 2. Why the obvious design is wrong

The tempting shape is "block the turn, ask a human, resume". **Do not build that.**

A relay turn has a wall clock (870 s on G2) and a client that may be a headless agent. Blocking on a human means: the
turn burns its budget waiting, the client times out at 30-60 s, and the work is lost — which is precisely the failure
`core/tooljobs.py` was written to fix ("a 20-minute lake query pays 14 minutes and receives an error string").

So approval must be **out of band**. The call is not paused; it is *declined for now and made resumable*.

## 3. Design: pre-authorization, not interruption

Three mechanisms, in the order they should be built. (1) is the gate; (2)+(3) make it usable.

### 3.1 Pre-authorization tokens (the gate)

An operator (or an automated policy) issues an **approval token** that authorizes a *shape* of call:

```
approval { id, key_id, server, tool, args_fingerprint, max_uses, expires_at, issued_by, reason }
```

- `args_fingerprint` is a hash over the canonical JSON arguments, **or** `NULL` for "any arguments of this tool"
  (weaker, and must be labelled as such in the audit row).
- The gate sits in `_gated_mcp_call` **after ACL and arg-policy, before the upstream session opens** — the same place
  the pin gate sits, for the same reason: refuse before side effects, not after.
- A destructive tool with **no matching token is refused** with a `ToolError` whose message names the exact command an
  operator can run to issue one. Refusal is the default; that is the whole point.
- `max_uses` and `expires_at` are mandatory. An approval that never expires is a grant wearing a disguise.

**Which tools require a token** is a settings list (`approval_required_tools`), defaulting to the destructive set:
`applygate.*apply*`, `workbench.run*`, `k8s-ops.exec_in_pod`. Default-deny for the named tools; everything else is
unaffected, so enabling this cannot break the fleet's read paths.

### 3.2 Durable handoff (why `tooljobs` exists)

For a long destructive operation, the refusal should be *actionable*: the response carries a `job_id` and the client
polls. `core/tooljobs.py` already implements exactly this registry (MCP `TaskStatus` vocabulary, one-statement state
transitions, `claim_result()` once, owner scope = API key) and is currently **inert, unwired**. This is its first real
consumer: a refused call is recorded as a pending job with its arguments; when a token is issued for it, the job is
claimable and the *next* identical call (same `args_fingerprint`) succeeds and consumes one use.

That keeps the invariant that matters: **no call ever blocks a turn**, and the work is not lost.

### 3.3 The operator surface

An admin route + the `scripts/` CLI pattern already used for pins:

```
tool_approval_admin.py list                     # pending requests + live tokens
tool_approval_admin.py grant --request <id> --uses 1 --ttl 15m
tool_approval_admin.py revoke <token-id>
```

Every grant and every use writes to `admin_audit` **and** joins the hash-chained `mcp_call_audit` entry for the call
(`chain_audit` is the existing mechanism) — an approval that is not in the chain did not happen, as far as an auditor
is concerned.

## 4. Fail-closed, precisely

| Situation | Behaviour |
|---|---|
| token missing / expired / exhausted | **refuse** (`denied`, `detail=approval_required`) |
| args fingerprint mismatch | **refuse** — a token for `delete ns=test` must not authorize `ns=prod` |
| approval store unreachable | **refuse** — this is the one gate where a fault must not fail open; a read-only tool with a broken ACL is an outage, a destructive tool with a broken approval gate is an incident |
| tool not in `approval_required_tools` | unchanged path, zero cost |

Note the deliberate asymmetry with pinning: pinning **fails open on fault** because a pinning bug must not break a turn.
Approval **fails closed on fault** because the failure mode it prevents is irreversible. Different blast radius,
different default.

## 5. Rollout

1. **Observe** — build the token table and the gate in `mode: warn`: record "would have required approval", change no
   behaviour. Two weeks of that tells you which tools actually need it, and how often.
2. **Enforce on G2** for the three named tools, with the CLI as the only way to grant. If the fleet cannot operate with
   it on for a week, the tool list is wrong, not the gate.
3. **Default-on** in the tracked chart for the destructive set only. Never for the whole catalogue.

## 6. Non-goals

- **Not** MRTR wire-protocol conformance in v1. `resultType: "input_required"` is the spec-shaped answer and the spec is
  frozen to 2027-03-30, so there is no rush to be an early implementer; the token model above is what MRTR would carry
  anyway. Adopt the envelope when a client actually speaks it.
- **Not** a human-in-the-loop UI in v1. The CLI is the operator surface; a console page can read the same table later.
- **Not** per-argument review by a model. An LLM approving an LLM's destructive call is a second opinion from the same
  failure mode.
