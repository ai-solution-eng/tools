# pcai-llm-gateway

**The multi-tenant LLM + MCP gateway for HPE PCAI.** PCAI clusters serve
powerful models (vLLM/SGLang endpoints) and a growing fleet of MCP servers —
but each consumer that talks to them directly gets the raw endpoint, the raw
tool surface and no accounting. This gateway sits between them: virtual API
keys with **ledger-based token + USD budgets**, an **OpenAI-compatible /v1
proxy** over **aggregated model endpoint pools** with health/failover and a
**server-side tool relay**, a **federated MCP** surface with **per-tool
ACLs**, **self-service enrollment codes**, and **agent traces** — one
service, one control plane, one usage ledger in Postgres.

> Every token is accounted for in an append-only ledger; every tool call is
> checked against a default-DENY ACL; the ledger is the budget engine — no
> read-modify-write counters anywhere.

---

## What problem(s) it solves

- **Multi-tenant access to PCAI models + MCPs behind virtual keys.** Teams
  get `hllm-...` keys — not upstream endpoints or upstream API keys. Keys are
  stored **hashed** (SHA-256, plaintext shown once at creation), scoped
  (`proxy` / `mcp` / `admin`), restricted to specific models, editable in
  place, and revocable/expirable without touching upstreams.
- **Budgets that actually hold.** Day/week/month budgets **per key, per
  model** — in tokens AND USD — derived by SUM over the usage ledger; no
  check-then-act race, because there is no "remaining" column to corrupt.
  Calls are checked **before** the upstream call and settled **after** with
  real usage — including every call inside the tool relay (its loop cannot
  bypass the ledger).
- **Onboarding without admin toil.** Enrollment codes minted against a
  tenant + profile let new users self-serve a key with the right budgets,
  cascade and MCP grants already applied; per-key MCP credential overrides
  (write-only) give each tenant its own identity on header-keyed servers
  such as the memory MCP.
- **Per-TOOL MCP ACLs.** MCP federation is default-DENY per (key, server,
  tool): a server-level allowlist is trivially bypassed by a server that
  grows one tool, so the ACL row is per tool. Catalog + call tools let a
  scoped agent discover and use only what its key allows.
- **Aggregated endpoint pools with health/failover.** One virtual model name
  maps to N physical endpoints; selection is (priority, weight)-ordered with
  weighted random among equals, and a per-endpoint circuit breaker cools an
  endpoint down after consecutive failures — observable in the console
  (status dots, test buttons) and quarantined automatically.
- **Agentic observability.** The tool relay executes model tool calls
  in-cluster and writes one trace row per step (model iteration / tool
  call) grouped by request — the per-user "what did the agent do and how
  long did each hop take" waterfall, next to the cost ledger.
- **Demand-shaped billing (opt-in).** When GPU demand sits below a
  hysteresis band, budget consumption bills at a reduced fraction
  (per-profile configurable) while the ledger keeps true usage — any
  evaluator failure bills full price.
- **Cluster-internal routing.** Upstream endpoints and MCP servers are
  in-cluster services (`http://vllm-svc.ns.svc.cluster.local/v1`), so proxy
  traffic never leaves the cluster fabric and never traverses the corporate
  proxy.
- **Ledger-based usage accounting.** Every request outcome (`ok`, `error`,
  `budget_exceeded`, `rate_limited`, `cache_hit`) writes one immutable
  ledger row with token counts, USD cost, billing fraction, latency and a
  request id — filterable/exportable (CSV) through the admin usage
  explorer, and the basis for budgets, rate windows and reporting.

## Architecture

![pcai-llm-gateway — routing, fallback & MCP fleet. Blue: the gateway routes via d3-turbo, which picks one model (DeepSeek for hard code, GLM for review, Qwen for lighter work). Green: the fallback cascade DeepSeek → GLM → Qwen, re-gated per hop. Orange: the opt-in demand-aware path, where the off-peak evaluator picks the cascade entry and sets the billing fraction. Bottom: the MCP fleet broken out by task group — data handling, visualization, research (searxng web search + fetch), cluster admin, and workbench (scratch write + execution env).](documentation/architecture.png)

*Source: [`documentation/architecture.dot`](documentation/architecture.dot) — re-render with
`dot -Tpng -Gdpi=150 documentation/architecture.dot -o documentation/architecture.png` (or `-Tsvg`).*

```
   consumers (agents, OpenAI SDKs, MCP clients)
        │  Authorization: Bearer hllm-...
        ▼
┌───────────────────────────────────────────────┐
│              pcai-llm-gateway :9200            │
│  /v1   OpenAI-compatible proxy (chat, embed,  │
│        models, usage, conversations) — budget │
│        + rate gates, pool routing, breaker,   │
│        optional in-cluster tool relay         │
│  /mcp  federated MCP (MCP 2.0 streamable-http)│
│        — per-tool default-DENY ACLs           │
│  /admin key/budget/endpoint/MCP management,   │
│        usage + traces explorers, off-peak,    │
│        enrollment + credential consoles       │
│  /health /ready /metrics                      │
└───────────────┬───────────────────────────────┘
                │  http over svc.cluster.local (in-cluster fabric)
     ┌──────────┴───────────────┬──────────────────┐
     ▼                          ▼                  ▼
 model endpoints           MCP servers         PostgreSQL
 (vLLM/SGLang pools,       (applygate-mcp,     ledger: usage_ledger,
  priority/weight,          k8s-mcp, mm-rag,   budgets, api_keys,
  circuit breaker,          sqlhandler,        rate_windows, endpoints,
  routing ladder +          seaborn,           traces, conversations,
  fallback chains)          workbench, … at    enrollments, …
                            *.svc.cluster.local)
```

The gateway holds **no Kubernetes API access**: endpoint pools, MCP servers
and virtual keys are configured through `/admin` into Postgres; upstream
credentials are stored **by reference** (the *name* of a Kubernetes Secret in
`model_endpoints.api_key_secret`) or Fernet-encrypted in the panel under an
operator-provisioned master key.

## Features

- **Virtual API keys** — `hllm-` prefixed, 32-byte random, SHA-256-hashed at
  rest, prefix shown for UI display; scopes, `allowed_models`, RPM/TPM,
  concurrency cap and expiry per key; **in-place editing** (PATCH — limits,
  scopes, expiry, relay default, live immediately); rotation = create →
  migrate → revoke.
- **Period budgets** — day/week/month per key per model, tokens or USD,
  ledger-derived (fraction-weighted during off-peak); checked pre-call with
  a token estimate, settled post-call with real usage; 80%/95% warnings via
  `X-Budget-Warning`.
- **Rate limits + backpressure** — atomic fixed-window RPM/TPM per key
  (`X-RateLimit-Remaining-*` on responses) plus a per-key in-flight cap.
- **OpenAI-compatible proxy** — `/v1/chat/completions` (streaming),
  `/v1/embeddings` (deduplicating cache), `/v1/models`, `/v1/usage` (the
  key's own "why am I 429ing" view), `/v1/conversations` (per-key saved
  transcripts); 32 MiB body cap (chunked bodies included).
- **Tool relay** — header `X-PCAI-Execute-Tools: 1` (or the key's
  `relay_default`, or the OpenAI-idiomatic
  `tools:[{"type":"mcp"}]` body opt-in) runs the model↔MCP loop
  in-cluster; thinking, tool steps (args + result previews) and guards
  stream as `pcai` SSE events; every model call and tool call settles the
  ledger; per-request traces group the waterfall. `require_approval:
  "always"` hands tool calls back to the client untouched.
- **Content guardrails** — named per-model pre_call/post_call policies:
  keyword blocklist, regex PII masking (Luhn-gated cards), length caps,
  external webhook moderation (fail-open). Blocks refuse before the gates
  (pre) or refuse the answer with real spend ledgered (post); every
  verdict lands in `usage_ledger.guardrail_events`.
- **Endpoint pools** — priority/weight selection per virtual model name,
  in-memory per-endpoint circuit breaker (configurable threshold/cooldown,
  preserved across registry refreshes); health surface + on-demand probe in
  the console.
- **MCP federation** — MCP 2.0 streamable-http at `/mcp`; registered servers
  are reachable through one endpoint, gated per (key, server, tool), with
  per-server tool policies and per-key credential overrides.
- **Self-service enrollment** — hashed invite codes bound to tenant +
  profile; redemption mints a personal key with the profile's budgets and
  grants pre-applied.
- **SSO self-mint (browser identity → key)** — behind the platform's
  oauth2-proxy chain the chat page shows who you are (the identity chip)
  and eligible users mint their OWN virtual key with one click
  (`GET /v1/identity` probe; `POST /v1/keys/for-me` — one key per
  identity, in-place rotate), carried past the proxy's Bearer by the
  `X-API-Key` envelope. Inert until the coupled rollout in
  [`documentation/DEPLOYMENT.md`](documentation/DEPLOYMENT.md) § SSO
  self-mint rollout lands.
- **Off-peak billing** (opt-in) — a PromQL-driven evaluator with
  hysteresis switches a low-demand billing fraction; per-profile
  override; fail-safe to full price on any error; live status in the
  console.
- **Usage ledger + explorers** — one immutable row per request outcome in
  Postgres (schema auto-created at startup), actor-joined, filterable,
  groupable and CSV-exportable through `/admin/usage`; agent traces next to
  it for the "what happened" half.
- **Admin API + console** — key/budget/endpoint/MCP/profile management,
  usage + trace explorers, off-peak status, enrollment and credential
  consoles, and the append-only admin audit trail at `/admin`, gated by
  operator-provisioned admin keys.
- **Fail-closed posture** — unknown/revoked/expired key → 401; DB
  unavailable → `/ready` fails and the pod leaves the Service; no key ledger
  → no service.

## Deployment

Deployment is a **values problem**: import the packaged chart into PCAI,
create the required operator Secrets (admin keys always; the Postgres URL
Secret in the default external-Postgres mode — the chart by default creates
or inlines no credentials), then edit only the Helm Values editor and
apply. **Postgres is external by default** (PCAI provides/hosts it); for
units without a site DB the chart offers an **embedded single-node Postgres
16** via `database.embedded.enabled: true` — see
[`documentation/DEPLOYMENT.md`](documentation/DEPLOYMENT.md). The full
schema (tenants, profiles, users, api_keys, budgets, cost_budgets,
usage_ledger, model_costs, rate_windows, model_endpoints, mcp_servers,
mcp_tool_acls, embedding_cache, agent_traces, conversations,
conversation_messages, enrollments, mcp_server_creds, guardrails,
model_fallbacks, admin_audit, …) is applied
automatically at startup. Routes (`/v1`, `/mcp`, `/admin`, `/health`,
`/ready`, `/metrics`, `/ui`, `/`) expose through the PCAI Istio gateway when
`ezua.enabled=true`. Full walkthrough:
[`documentation/DEPLOYMENT.md`](documentation/DEPLOYMENT.md); paste-ready
values: [`helm/values-examples/README.md`](helm/values-examples/README.md).

## Documentation

- **[LiteLLM & Bifrost comparison](documentation/LITELLM-COMPARISON.md)** — the five-minute version of how this gateway differs from LiteLLM and Bifrost (full deep-dive lives in the external `general_documentation/LiteLLM_Comparison/` doc).

- [Deployment](documentation/DEPLOYMENT.md) — chart values, required Secrets, Postgres modes, sizing, exposure, upgrading
- [Features](documentation/FEATURES.md) — budget semantics, relay protocol, traces, enrollment, off-peak billing, routing, ledger schema
- [API](documentation/API.md) — every endpoint: method/path/auth/body/response for the data plane and the admin plane
- [Memory](documentation/MEMORY.md) — mint a RAG key, create + ★ a memory dataset, attach it to a gateway key, distiller contract, other memory MCPs
- [Verification](documentation/VERIFICATION.md) — smoke tests, admin flows, feature checks, troubleshooting
- [Roadmap](documentation/ROADMAP.md) — brainstormed candidates, explicitly not committed

## Changelog

Version history: [CHANGELOG.md](CHANGELOG.md).

## License

Proprietary — © HPE AI Solution Engineering.
