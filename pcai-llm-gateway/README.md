# pcai-llm-gateway

The multi-tenant LLM + MCP gateway for HPE Private Cloud AI (PCAI): one
service — packaged as the `pcai-llm-gateway` Helm chart, current release
**4.1.0** — that sits between PCAI-served models, in-cluster MCP servers and
the people and agents that use them, and accounts for every call. It deploys
into a PCAI AI Essentials / Ezmeral unit as an EzAppConfig import and
targets PCAI clusters running vLLM/SGLang model endpoints and fleet MCP
servers.

## What it is

A **multi-tenant gateway** — one endpoint that fronts many tenants' traffic
to many shared backends, deciding for every call WHO is asking, WHAT they
may touch, and WHAT it cost. Concretely it is three services behind one
port (9200):

- an **LLM proxy** — an OpenAI-compatible `/v1` API (`chat/completions`,
  `embeddings`, `models`, `usage`, `conversations`, plus `audio` and
  `rerank`) over aggregated model endpoint pools with health, failover and
  an in-cluster tool relay;
- an **MCP gateway** — a federated `/mcp` endpoint (MCP, the Model Context
  Protocol, is the standard way LLM agents discover and call tools) that
  exposes the cluster's MCP servers behind per-tool, default-deny ACLs;
- a **control plane** — `/admin` + web consoles for keys, budgets, pools,
  guardrails, enrollment, usage and traces, over one Postgres.

Every consumer authenticates with a **virtual key** (`hllm-…`, stored
SHA-256-hashed, shown once at creation); no tenant ever sees an upstream
endpoint or an upstream credential.

> The posture in one sentence: **every token is accounted for in an
> append-only ledger; every tool call is checked against a default-DENY
> ACL; the ledger IS the budget engine** — there is no "remaining" counter
> to corrupt and no read-modify-write race anywhere.

## The problem it solves

- **You have shared GPU capacity and no safe way to hand it out.** Virtual
  keys carry day/week/month token + USD budgets **per key per model**,
  checked before the upstream call and settled after with real usage — so a
  team's limit holds without taking a global admin credential hostage.
- **You have MCP servers whose tools are not equally safe for every
  tenant.** Per-tool ACLs are **default-DENY** — a server-level allowlist is
  trivially bypassed by a server that grows one tool, so the grant is the
  (key, server, tool) pair.
- **You have agents that need tools, and a boundary that says the client
  cannot be trusted to run them.** The server-side **tool relay** executes
  the model↔tool loop in-cluster — the raw tool surface, upstream
  credentials and arguments never leave the cluster fabric, and every hop
  settles the ledger.
- **You have model endpoints with different sizes, speeds and moods.** One
  virtual model name maps to a priority/weight pool with a circuit breaker,
  admin-curated fallback chains and live-load model groups — failover and
  idle-picking are values problems, not rewrites.
- **You have users, teams and no admin toil budget.** Enrollment codes and
  the SSO self-mint path let users self-serve a correctly-scoped key —
  budgets, cascade and MCP grants pre-applied; team leaders manage their own
  team inside a hard budget envelope.
- **You have billable shared capacity and no accounting.** Every outcome —
  `ok`, `error`, `budget_exceeded`, `rate_limited`, `cache_hit` — is one
  immutable ledger row with tokens, USD, billing fraction, latency and a
  request id, filterable and CSV-exportable; that ledger is the source for
  budgets, rate windows, chargeback and the audit story.

## How it fits in PCAI

The gateway is a pure in-cluster consumer: it holds **no Kubernetes API
access** (no RBAC, no ServiceAccount token) — endpoint pools, MCP servers
and keys are configured through `/admin` into Postgres, and upstream
credentials are stored **by reference** (the name of a Secret) or
Fernet-encrypted under an operator-provisioned master key.

```
   consumers (agents, OpenAI SDKs, chat UIs, MCP clients)
        │  Authorization: Bearer hllm-…   (or the oauth2-proxy identity chain)
        ▼
┌─────────────────────────────────────────────────┐
│            pcai-llm-gateway :9200               │
│  /v1    OpenAI-compatible proxy — budget +      │
│         rate gates, pool routing, breaker,      │
│         optional in-cluster tool relay          │
│  /mcp   federated MCP — per-tool default-DENY   │
│         ACLs, argument-level destination policy │
│  /admin  + /team_leader  consoles (keys, pools, │
│         budgets, usage, traces, enrollment)     │
│  /health /ready /metrics                        │
└───────────────┬─────────────────────────────────┘
                │  http over svc.cluster.local (in-cluster fabric)
     ┌──────────┴───────────────┬──────────────────┐
     ▼                          ▼                  ▼
 model endpoints           MCP servers         PostgreSQL
 (vLLM/SGLang pools,       (fleet MCPs at      ledger, budgets, keys,
  priority/weight,          *.svc.cluster.      pools, traces — external
  breaker, fallback         local)              (site DB) or embedded
  chains, model groups)                         single-node Postgres 16
```

| Depends on | Depended on by |
|---|---|
| PCAI Istio gateway (`ezaf-gateway`) + optional oauth2-proxy SSO chain for exposure | Agents and harnesses (DSH, OpenWebUI, opencode, raw SDKs) via `/v1` |
| Postgres — external (PCAI-provided, the default) or the chart's embedded single-node mode | MCP clients via `/mcp` |
| Model endpoints (vLLM/SGLang / MLIS) registered in `/admin` | Team leaders via `/team_leader`; admins via `/admin` |
| Fleet MCP servers registered in `/admin`; Prometheus only for off-peak/model groups/metrics | |

## Capabilities, by theme

Each line states the design decision and why; the link is the deep dive.

**Access and tenancy**

- **Virtual API keys** — `hllm-`-prefixed, hashed at rest, scoped
  (`proxy`/`mcp`/`admin`), model-restricted, editable IN PLACE (limits,
  scopes, expiry — live immediately); rotation = create → migrate → revoke.
  Hash-only storage means a database dump is not a key list.
  [§ Key model](documentation/FEATURES.md#key-model)
- **Self-service enrollment codes** — hashed invite codes bound to tenant +
  profile; redemption mints a personal key with the profile's budgets and
  grants pre-applied (the code IS the credential). The admin never keys in a
  user.
  [§ Self-service enrollment](documentation/FEATURES.md#self-service-enrollment-codes--keys)
- **Team leader tier** — an enrollment code is a TEAM invite: a hard budget
  envelope over the team's SUM (enforced by the same evaluator as per-key
  budgets, so the two can never disagree), plus a hash-only leader key whose
  console sees exactly its own enrollment's keys, usage and traces.
  [§ Team leader tier](documentation/FEATURES.md#team-leader-tier-enrollment-scoped-console)
- **SSO self-mint** — behind the platform's oauth2-proxy chain the chat page
  shows who you are and eligible users mint their OWN key (`GET
  /v1/identity`, `POST /v1/keys/for-me`); inert until the coupled,
  ordered rollout turns it on — identity headers are spoofable until an
  enforcing proxy AND an AuthorizationPolicy both stand in front.
  [§ SSO self-mint](documentation/FEATURES.md#sso-self-mint-browser-identity--key)

**Model proxy and routing**

- **OpenAI-compatible proxy** — streaming chat, embeddings (deduplicating
  cache), models, usage ("why am I 429ing"), saved conversations, audio
  passthrough and rerank, all behind the same gates.
  [§ Router extras](documentation/FEATURES.md#router-extras), [§ Audio and rerank](documentation/FEATURES.md#audio-and-rerank-the-governed-data-plane-extends)
- **Endpoint pools + failover** — priority/weight selection, per-endpoint
  circuit breaker (state survives registry refreshes), admin-curated
  fallback chains where every hop re-runs the budget gate — a fallback is
  never a budget bypass.
  [§ Endpoint health](documentation/FEATURES.md#endpoint-health), [§ Router extras](documentation/FEATURES.md#router-extras)
- **Complexity routing + model groups** — optional pseudo-models pick a
  cheap-first ladder by request complexity; model groups load-balance
  equivalent models by live queue depth; conversation stickiness (cascade
  floor, endpoint pin) keeps a thread on its capability class and KV cache.
  [§ Routing](documentation/FEATURES.md#routing), [§ Model groups](documentation/FEATURES.md#model-groups)
- **Context-window discovery** — the gateway reads `max_model_len` from the
  upstream's own `/v1/models` card instead of a hand-maintained constant, so
  request-fit estimation stops failing one-term inequalities.
  [§ Context-window discovery](documentation/FEATURES.md#context-window-discovery-and-request-fit)

**MCP federation and the tool relay**

- **Federated MCP with per-tool ACLs** — registered servers reachable
  through one `/mcp` endpoint; grants are per (key, server, tool),
  default-DENY, with per-server tool policies, priority-ordered failover on
  flattened calls, and per-key credential overrides for header-keyed
  servers.
  [§ MCP federation](documentation/FEATURES.md#mcp-federation)
- **Server-side tool relay** — `X-PCAI-Execute-Tools: 1` (or the key's
  `relay_default`) runs the model↔MCP loop in-cluster; thinking, tool steps
  and guards stream as `pcai` SSE events; every model call AND tool call
  settles the ledger; the loop cannot bypass the ACL or the budget gate
  because it runs through the same executor.
  [§ Tool-use relay](documentation/FEATURES.md#tool-use-relay-server-side-agentic-loop)
- **Tool-definition pinning + injection screening + result compression** —
  rug-pull defence (a rewritten tool definition is refused, not offered),
  screening of tool results fed back to the model, and deterministic
  compression of oversized results with a retrieve-tool escape hatch — the
  tool channel is an attack surface, so it is defended like one.
  [§ Tool-definition pinning](documentation/FEATURES.md#tool-definition-pinning-rug-pull-defence), [§ Tool-result injection screening](documentation/FEATURES.md#tool-result-injection-screening)
- **Agent profiles** — a named model endpoint that bundles a narrowed tool
  surface (+ optional pinned model/prompt); an agent can only HIDE tools the
  key already holds — it can never grant one.
  [§ Agent profiles](documentation/FEATURES.md#agent-profiles-named-tool-surfaces-as-model-endpoints)

**Policy and guardrails**

- **Content guardrails** — named pre/post-call policies per model
  (blocklist, Luhn-gated PII regex, length caps, webhook moderation,
  secrets mask-only engine); every verdict is a ledger event; enforcement
  is strict, delivery is fail-open.
  [§ Content guardrails](documentation/FEATURES.md#content-guardrails)
- **Argument-level destination policy + URL guard** — the ACL is
  pair-granular but exfiltration is argument-granular, so a policy stage
  inspects WHERE a tool call is headed (URL/IP-literal detection with a
  deliberate false-positive budget), backed by the one shared SSRF
  deny-list and read+egress grant separation.
  [§ Argument-level destination policy](documentation/FEATURES.md#argument-level-destination-policy-arg_policy), [§ URL guard](documentation/FEATURES.md#url-guard-the-one-ssrf-deny-list)

**Accounting and observability**

- **The ledger/budget engine** — budgets are SUMs over the append-only
  ledger (no "remaining" column, no check-then-act race); pre-call gate
  with an estimate, post-call settle with real usage; 80%/95% warnings via
  `X-Budget-Warning`.
  [§ Budget semantics](documentation/FEATURES.md#budget-semantics)
- **Unit-aware pricing** — `model_costs.unit` prices by what a model
  actually sells (tokens, chars, audio seconds, images, requests); voice
  models bill measured duration instead of a token guess.
  [§ Unit-aware pricing](documentation/FEATURES.md#unit-aware-pricing-billable-units)
- **Off-peak billing (opt-in)** — a PromQL evaluator with hysteresis
  discounts budget consumption in low-demand windows while the ledger keeps
  true usage; any evaluator failure bills full price.
  [§ Off-peak billing](documentation/FEATURES.md#off-peak-billing)
- **Usage explorers + agent traces + audit trail** — the ledger answers
  "what did the data plane cost"; `agent_traces` answers "what did the
  agent DO" (per-step waterfall, cache-rate columns); `admin_audit`
  answers "who changed the control plane"; optional OTLP/Langfuse spans
  export metadata only.
  [§ Usage surfaces](documentation/FEATURES.md#usage-surfaces), [§ Agent traces](documentation/FEATURES.md#agent-traces), [§ Admin mutation audit trail](documentation/FEATURES.md#admin-mutation-audit-trail), [§ OTLP / Langfuse spans](documentation/FEATURES.md#otlp--langfuse-spans-metadata-only-tracing)

**Session memory**

- **The distiller** — one canonical pipeline turns conversation transcripts
  into structured project memories (map-reduce, re-distill on continue,
  capability-probe attachment gate, cost attributed to the owning key);
  every harness that relays through the gateway feeds it with zero client
  changes.
  [§ Session memory distiller](documentation/FEATURES.md#session-memory-distiller), and [MEMORY.md](documentation/MEMORY.md) for the operator path.

## Deploy on PCAI (one screen)

1. **Prerequisites** — create the Secrets out-of-band (admin keys always;
   the Postgres URL Secret in the default external-Postgres mode; the Fernet
   master key recommended) — table below and
   [DEPLOYMENT.md § Prerequisites](documentation/DEPLOYMENT.md#prerequisites).
2. **Import the chart once** — `pcai-llm-gateway-4.1.0.tar.gz` into PCAI
   (AI Essentials / EzAppConfig import; PCAI uploads it to ChartMuseum and
   installs it).
3. **Set values** — paste a values document into the Helm Values editor:
   start from [helm/values-examples/values.hosted-trial.yaml](helm/values-examples/values.hosted-trial.yaml)
   (or `values.g2.yaml`), adjust the `# SITE:` lines.
4. **Apply** — the deployment rolls; the schema auto-applies at startup.
5. **Verify** — `/health` → `{"status":"ok"}`, `/ready` → 200 (Postgres
   reachable), then one real chat call with a minted key.

Full procedure, prerequisites with failure modes, SSO/policy rollouts,
sizing and troubleshooting:
[**documentation/DEPLOYMENT.md**](documentation/DEPLOYMENT.md).

## Prerequisites at a glance

Secrets (the chart creates none by default and never inlines a credential):

| Secret (default name) | Key | Why | Without it |
|---|---|---|---|
| `pcai-llm-gateway-admin` | `admin-api-keys` | `/admin`'s only credential | Pod never starts (`CreateContainerConfigError`) |
| `pcai-llm-gateway-db` | `database-url` | Postgres URL, external mode (the default) | Pod never starts; DB unreachable → `/ready` fails |
| `pcai-llm-gateway-pg-auth` | `password` | Embedded-Postgres mode only — one Secret feeds PG and the gateway | Pod(s) never start |
| `pcai-llm-gateway-secrets` | `master-key` | Fernet for console-pasted credentials + audit-chain anchors (required by the DEFAULT render — opt out by blanking the name) | Pod never starts; if blanked: panel credential writes 503 |

Cluster objects / dependencies that cannot be a value (full table with
verify steps in [DEPLOYMENT.md § Prerequisites](documentation/DEPLOYMENT.md#prerequisites)):
the release namespace's Istio-injection label **only together with** the
AuthorizationPolicy (one without the other is either exposure or a no-op);
the SSO edge-chain route classes; DNS for the route host; a storage class
for the embedded-PG PVC; ServiceMonitor/PrometheusRule CRDs and a Metrics
API provider only when `metrics.*`/`autoscaling.*` are on; a reachable
Postgres, model endpoints and MCP servers — proven from the cluster.

## Day-2

- **Upgrade** — import the newer chart package, re-apply the same values
  document; the schema is additive and idempotent, the ledger and budgets
  survive. ([§ Upgrading](documentation/DEPLOYMENT.md#upgrading))
- **Health** — `/health` (liveness), `/ready` (Postgres round-trip — a
  failing `/ready` removes the pod from the Service: fail closed, never
  serve keyless).
- **Logs** — `kubectl logs deploy/pcai-llm-gateway` (PCAI console Logs tab);
  startup must show `database connected and schema ensured`.
- **Uninstall** — the Postgres PVC and chart-generated Secrets PERSIST by
  design (the ledger and the admin keys survive a reinstall). What is
  deleted vs kept: [§ Upgrading → Uninstall](documentation/DEPLOYMENT.md#upgrading).
- **Troubleshooting** — symptom → cause → fix matrix for the fourteen real
  failure modes of this package's history:
  [DEPLOYMENT.md § Troubleshooting](documentation/DEPLOYMENT.md#troubleshooting).

## Documentation map

| File | The question it answers |
|---|---|
| [documentation/DEPLOYMENT.md](documentation/DEPLOYMENT.md) | How do I deploy, expose, upgrade and troubleshoot it the PCAI way — and what must exist BEFORE the chart applies? |
| [documentation/FEATURES.md](documentation/FEATURES.md) | How does each mechanism actually behave, and WHY is it designed that way? |
| [documentation/API.md](documentation/API.md) | Every endpoint: method/path/auth/body/response. |
| [documentation/MEMORY.md](documentation/MEMORY.md) | How do I wire session memory end to end (RAG key → dataset → key attachment → distiller)? |
| [documentation/CHANGELOG.md](documentation/CHANGELOG.md) | What changed in each release wave? |
| [documentation/ROADMAP.md](documentation/ROADMAP.md) | What is brainstormed but explicitly NOT committed? |
| [documentation/LITELLM-COMPARISON.md](documentation/LITELLM-COMPARISON.md) | How does this gateway differ from LiteLLM / Bifrost? |
| [documentation/changelogs/VERIFICATION.md](documentation/changelogs/VERIFICATION.md) | How do I smoke-test a deployment or an upgrade? |
| [documentation/changelogs/SSO-ROLLOUT-G2.md](documentation/changelogs/SSO-ROLLOUT-G2.md) | The exact G2 runbook for the SSO + AuthorizationPolicy rollout. |
| [documentation/changelogs/DIAGNOSTIC-JOBS.md](documentation/changelogs/DIAGNOSTIC-JOBS.md) | Why do my diagnostic Jobs hang in this namespace, and how do I run them correctly? |
| [documentation/Audits/](documentation/Audits/) | Audit + remediation reports (what the waves found and fixed). |
| [documentation/architecture.svg](documentation/architecture.svg) (+ `.dot` source) | The routing/fallback/MCP-fleet diagram. |
| [helm/values-examples/README.md](helm/values-examples/README.md) | Paste-ready values documents (G2, hosted trial) and the `# SITE:` convention. |
| [helm/local/README.md](helm/local/README.md) | Where per-site credentials-bearing values live (gitignored, never packaged). |

## Developing locally

This is the developer path — **not the deployment path** (deployment is the
chart import + values editor above; a local checkout never configures a
cluster). Python ≥ 3.11 with the `dev` extras (`pytest`, `ruff`, `mypy` —
the shared `ruff.toml`/`mypy.ini`), tests in `tests/` (`pytest`), the
shared formatter via `formatter.sh`. Release tooling owns image publishes;
registry-seeding helpers live in `scripts/` (`seed_registry.py`,
`seed_agents.py`), ledger maintenance in `scripts/partition_ledger.py`.

## License

Proprietary — © HPE AI Solution Engineering.
