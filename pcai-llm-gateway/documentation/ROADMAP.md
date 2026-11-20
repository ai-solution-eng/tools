# Roadmap — Brainstormed, Explicitly Not Committed

**What this file is:** the ideas that are brainstormed but explicitly NOT
committed scope. Nothing here is scheduled, and nothing here is authoritative
about what the gateway does — shipped behaviour lives in
[`FEATURES.md`](FEATURES.md), [`API.md`](API.md) and the code. An entry is a
design conversation with a real constraint behind it, kept so the next one
starts from that constraint instead of from scratch.

- **Current release:** 4.2.1 (`helm/Chart.yaml`, `pyproject.toml`).
- **The October 2026 original** is archived verbatim at
  [`history/ROADMAP-2026-10.md`](history/ROADMAP-2026-10.md). Most of it has
  since shipped (first section below); the archive is the record of that design
  work, not current scope. It is archived, not maintained — do not edit it.
- **Entry shape:** what it is, why it is still open, and the doc/code path that
  would carry it.

## Recently shipped (moved out of this file)

One line each — the detail lives in the linked document, never here.

- Per-model off-peak evaluators + per-model ratio editor —
  [`FEATURES.md`](FEATURES.md#off-peak-billing), `GET /admin/offpeak`.
- Enrollment-time memory datasets — [`FEATURES.md`](FEATURES.md); now LEGACY
  since the RAG model became SSO-managed (2026-11-04).
- Retention policies for traces + conversations — `retention_trace_days` /
  `retention_conversation_days` (default 0 = off).
- Caller-aware `GET /v1/models` (`pcai.type` / `pcai.primary`) —
  [`API.md`](API.md).
- Canonical conversations + result-only continuation (`X-PCAI-Continue`) —
  [`API.md`](API.md#result-only-continuation-7-wave-c),
  `src/pcai_llm/api/routes_proxy.py`.
- Client tool-server dispatch (`client__<tool>` via
  `POST /v1/dispatch/register` + long-poll) —
  `src/pcai_llm/api/routes_dispatch.py`, `settings.relay.clientDispatch`
  (default **off**).
- Per-key `stream_pcai_events` default flipped to spec-clean FALSE —
  `src/pcai_llm/core/keys.py` (DEFAULT-FLIP).
- Profile live grants (`live_grants`), per-server deny, copy-from —
  [`API.md`](API.md) `/admin/keys/{id}/mcp-grants*`.
- Ledger partitioning, custom Prometheus metrics, admin mutation audit, chart
  network isolation — [`CHANGELOG.md`](CHANGELOG.md).

## Open — designed, not committed

- **Final-answer token streaming inside the relay.**
  What: interleave streamed final-answer deltas with the step events instead of
  ending on one chunk.
  Why open: the relay still emits the final answer as ONE
  `chat.completion.chunk` after the step events
  ([`FEATURES.md`](FEATURES.md#tool-use-relay-server-side-agentic-loop)), which
  is not the OpenAI-native interleaved shape.
  Blocker: the chat console's rAF batcher and meta-chip logic both assume one
  final payload, so this needs coordinated UI work.
  Would carry it: `src/pcai_llm/api/routes_proxy.py` + the chat console.

- **Client-dispatch persistence and multi-replica posture.**
  What: make offers and pending dispatch jobs survive a gateway restart.
  Why open: they are per-process memory by design
  (`src/pcai_llm/core/clientside.py`); a restart empties them and clients
  re-register on their next poll.
  Blocker: persistence was wave B2's explicit decision and B2 shipped
  in-memory — whether multi-replica routing needs it is still unanswered.
  Would carry it: `src/pcai_llm/core/clientside.py` + a schema change.

## Open — ideas

- **MCP spec-watch: retire the long-poll dispatch endpoints.**
  What: when the MCP spec (or the installed SDK) admits generic server→client
  requests over streamable-HTTP with a stateless posture, replace
  `/v1/dispatch/*` with that shape and delete the endpoints.
  Why open: the archive's SDK-evidence essay rejected sampling/callback
  inversion today, but named this as the clean long-term replacement — the
  relay-side executor contract (dispatch → result/error) survives unchanged.
  Pointer: [`history/ROADMAP-2026-10.md`](history/ROADMAP-2026-10.md) §7 (the
  full file/line evidence is kept there, not copied here).

## Open — needs confirmation

- **Bulk ACL grant/revoke across a profile's `explicit`-mode keys.**
  Why open: live grants closed the trust-model half (tighten one profile, every
  key follows) and `copy-from` refills one key, but there is still no route
  that applies a grant change to every key of a profile or the fleet.
  Unconfirmed whether operators need it.

- **Client-dispatch default-on.**
  Why open: the feature shipped flag-off; enabling it by default changes what
  tools a key offers the model without the client asking, so it needs an
  explicit rollout decision. No evidence either way.

## Explicitly rejected / deferred

- **MCP sampling/callback inversion for client tools** — rejected on installed
  SDK evidence: the server-request surface is closed (`ping` / `roots/list` /
  `sampling/createMessage` / `elicitation/create` only), and stateless HTTP
  hard-wires `can_send_request=False`. The file/line trail is in the archive
  §7.
- **Loopback-tunnel client MCP server** — rejected: a reverse tunnel is a
  product in itself, and it would route tenant code execution through the
  admin registry plane. The long-poll dispatch reaches the same trust boundary
  with no new infrastructure.
- **Stateful `/mcp` flip to carry server→client requests** — rejected on
  cost/benefit: it sacrifices the any-replica property for the whole federation
  surface to serve one feature.
- **Bounded ledger retention** — deferred, not rejected: traces and
  conversations are the prunable tables, while the ledger is the permanent
  audit artifact; a documented knob for operators who DO want it bounded never
  shipped (archive §5).
