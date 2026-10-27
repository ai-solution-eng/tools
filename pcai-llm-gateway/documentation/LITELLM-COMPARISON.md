# LiteLLM & Bifrost vs. pcai-llm-gateway — the short version

> **Companion to the deep-dive.** The full analysis — nine sections, a 26-row
> master table, the SSO end-to-end walkthrough and the claim-to-source map —
> lives in `general_documentation/LiteLLM_Comparison/PCAI_LLM_Gateway_vs_LiteLLM_Bifrost_v4.md`
> (external documentation repo, read-only). This page is the five-minute version
> to read next to [`architecture.png`](architecture.png) (source:
> [`architecture.dot`](architecture.dot)). Gateway claims are grounded in this
> repo with `file:line` citations; the competitor columns summarize the long
> doc's research against LiteLLM's and Bifrost's public documentation
> (September 2026) — re-verify there before quoting externally.

## Positioning in one paragraph

LiteLLM and Bifrost are products in the *model routing* market: they make many
LLM providers safely usable behind one OpenAI-compatible endpoint. The PCAI LLM
Gateway is the **governance substrate for an agentic fleet on a private PCAI
cluster** — the component that makes it safe to run cluster-mutating MCP servers
(applygate), cluster reads (k8s-ops), web search (searxng) and model pools
behind one endpoint, because authority is enumerated per tool, every call is
attributed to a key or subject, and every call is an immutable ledger row
([README.md:3-15](../README.md), long doc §1). Concretely, on top of being an
LLM proxy it is: a **budget engine** (day/week/month token + USD budgets derived
as SUMs over an append-only `usage_ledger` — no "remaining" column, no
read-modify-write race; [README.md:26-34](../README.md), [FEATURES.md:9-13](FEATURES.md)),
a **federated MCP plane** with per-(key, server, tool) default-DENY ACLs
([README.md:37-39](../README.md), [FEATURES.md:545-565](FEATURES.md)), a
**server-side tool relay** that runs the model↔MCP agentic loop in-cluster
([README.md:122-137](../README.md), [FEATURES.md:147-214](FEATURES.md)), an
**SSO self-service front door** where a signed-in human mints their own governed
key ([README.md:144-151](../README.md), [FEATURES.md:360-407](FEATURES.md)), and
a **session-memory distiller** that turns conversations into structured project
memories in the calling key's RAG dataset ([FEATURES.md:874-931](FEATURES.md)).
The architecture diagram draws exactly this: one entry point, routing and
fallback deciding *which* model serves, the relay corridor reaching the MCP
fleet, and the demand evaluator feeding both the head pick and the price.

## At a glance — the rows that matter for the architecture diagram

Legend: **Gateway** = pcai-llm-gateway (this repo) · competitor claims per the
long doc (§2 master table, §6, §8). ✔ ships · ◐ partial/config-dependent ·
💰 enterprise-licensed · ✘ absent.

| # | Capability | pcai-llm-gateway | LiteLLM | Bifrost |
|---|------------|------------------|---------|---------|
| 1 | **Ledger-derived budgets** | Spend is `SUM(...)` over `usage_ledger` per (key, model, period), in tokens AND USD; no mutable counter to corrupt; USD checked post-paid (overshoot bounded by one response); fraction-weighted during off-peak while the ledger keeps true usage — [FEATURES.md:9-27](FEATURES.md), [schema.py:188](../src/pcai_llm/db/schema.py) | Mutable spend counters on key/team/user, reconciled from logs; documented fail-open with no DB (long doc §2 row 1) | Counter + reconciliation model; immutable audit trail is paid-tier (long doc §2 row 1) |
| 2 | **Per-tool MCP ACLs, default-deny** | ACL row = (key, server, tool); a server that gains one tool grants nothing new; a zero-grant key discovers nothing — zero upstream fetches — [FEATURES.md:551-565](FEATURES.md), [schema.py:353](../src/pcai_llm/db/schema.py) | Server-scoped access control with tool-level options; authority widens silently when a server ships a new tool (long doc §2 row 2, §4 D1) | Per-virtual-key tool allow-lists (long doc §2 row 2) |
| 3 | **In-cluster tool relay** | `X-PCAI-Execute-Tools: 1` (or the key's `relay_default`, or `tools:[{"type":"mcp"}]`) runs the model↔MCP loop in-cluster — the client sees only the request and the final answer; guards: 16 model rounds per turn, wall-clock clamp under the ingress timeout, loop detection; every model call AND tool call settles the ledger; `require_approval:"always"` hands calls back untouched — [FEATURES.md:147-214](FEATURES.md) | Client-side tool loops: every tool call surfaces to the harness, which calls the MCP and re-sends the transcript (long doc §2 row 16) | Gateway-side agent mode, not fabric-resident (long doc §2 row 16) |
| 4 | **Complexity routing (`routing` / `routing__X`)** | Profile-opted pseudo-models: a deterministic score (chars/4 + tool JSON//64 + …) walks the profile's own ladder cheap-first; can only DOWNGRADE on positive evidence, never on error; `off\|shadow\|active` rollout stamps `X-PCAI-Route` and ledger columns — [FEATURES.md:646-713](FEATURES.md) | Router strategies / cost-based routing configured per deployment; no budget-coupled ladder (long doc §2 rows 10-11) | Routing/fallback layer (long doc §2 row 10) |
| 5 | **Per-model fallback chains** | Admin-curated chains (+ the `auto` ordering tail) walked when the primary cannot serve; EVERY hop re-gated at walk time: registered + key-allowed + healthy pool + fresh budget headroom — failover can never become a budget bypass; settles under the actual model — [FEATURES.md:975-1000](FEATURES.md), [schema.py:742](../src/pcai_llm/db/schema.py) | Router fallbacks + retries; no spend re-gating documented (long doc §2 row 10) | Routing/fallback layer (long doc §2 row 10) |
| 6 | **Off-peak demand billing** | PromQL evaluator (DCGM GPU util via the registered Prometheus MCP, 30 s loop, 20/35 hysteresis) switches a discount fraction consumed by the budget gates only; per-profile override + per-model evaluators; any evaluator failure bills full price — [FEATURES.md:474-543](FEATURES.md), [values.yaml:348-359](../helm/values.yaml) | ✘ no equivalent documented (long doc §2 row 1 note) | ✘ (long doc §2 row 1 note) |
| 7 | **Session memory distiller** | `POST /v1/distill` (+ the per-conversation route): one gateway-owned prompt + map-reduce call writes the calling key's RAG memory dataset; attachment-gated (an unattached key gets an inert 200 — nothing runs, nothing spends); off-peak sweep re-distills on continuation; every distill call settles one ledger row — [FEATURES.md:874-931](FEATURES.md), [API.md:286-310](API.md) | ✘ not in docs — memory is harness-side plugin work, each harness carrying its own credentials (long doc §2 row 26, §4 D9) | ✘ not documented (long doc §2 row 26) |
| 8 | **SSO self-service** | `GET /v1/identity` whoami probe + `POST /v1/keys/for-me`: one key per browser identity, fixed scopes/limits, transactional rotate, spend lineage survives rotation (`spend_ancestors`); grants stay admin-controlled — [FEATURES.md:360-407](FEATURES.md), [schema.py:146](../src/pcai_llm/db/schema.py) | SSO-driven key generation is 💰 enterprise; invitation flows are admin-run (long doc §2 row 8, §3.2) | IdP integration 💰 enterprise; no self-service mint (long doc §2 row 8) |
| 9 | **Deployment surface** | One stateless container + external Postgres (embedded single-node PG 16 for units without a site DB); values-only Helm; credentials by-reference; exposure via the PCAI Istio gateway; the gateway itself holds no Kubernetes API access — [README.md:168-187](../README.md), [README.md:99-103](../README.md), [values.yaml:72-87](../helm/values.yaml) | Postgres + Redis recommended for production; some governance/UI behind enterprise (long doc §2 row 19) | Redis for clustering/HA; clustering/RBAC/IdP/audit 💰 enterprise (long doc §2 row 19) |
| 10 | **Provider breadth** | Registered local endpoint pools (vLLM/SGLang, priority/weight, circuit breaker) — deliberately no cloud API translation — [FEATURES.md:841-868](FEATURES.md) | **✔ wins: 100+ providers** — the category leader (long doc §2 row 20, §6) | 20+ native providers (long doc §2 row 20) |
| 11 | **Ecosystem & community** | Single-team internal product; HPE-internal support; the self-audit loop (`AUDIT.md` → `REMEDIATION.md` with per-fix regression tests) is the differentiator | **✔ wins: large community, hosted UI, plugins, mature gateway knobs** (long doc §2 row 21, §6) | Go performance (~11 µs added at 5k rps), WASM plugins, community (long doc §2 rows 21, 24) |
| 12 | **Design center** | In-cluster MCP + agentic relay; fleet identity + metering plane (long doc §8) | Provider breadth, cloud spend (long doc §8) | Raw throughput, router knobs (long doc §8) |

## Reading the diagram with this table

The current [`architecture.dot`](architecture.dot) (rendered as
[`architecture.png`](architecture.png)) shows one request path from the harness
through the gateway, with the two ways a model gets picked — each maps to the
rows above:

- **Blue — routing (row 4).** The gateway asks **d3-turbo** (vLLM-SR System One,
  `decision_mode: shadow | active`), which picks exactly ONE model: DeepSeek for
  hard code, GLM for review / lowest hallucination, Qwen3.8-27B for lighter
  work. It sees a SUMMARY of the request — estimated tokens, message/tool
  counts, per-candidate cost and live GPU load — never the message content, and
  a confidence under 0.60 leaves the heuristics standing rather than downgrading
  on a guess ([FEATURES.md:742-787](FEATURES.md)).
- **Green — fallback (row 5).** With no routing decision the gateway walks the
  cascade **DeepSeek → GLM → Qwen**, promoting on budget exhaustion or a dead /
  5xx primary. Every hop is re-gated at walk time — registered, inside the key's
  `allowed_models`, a healthy chat pool, fresh budget headroom — so failover can
  never become a budget bypass, and each attempt settles under the model that
  actually served ([FEATURES.md:975-1000](FEATURES.md)).
- **Orange — the demand-aware path (row 6, opt-in).** The load-aware head picks
  the cascade's ENTRY point once per conversation from the per-model off-peak
  state and stays sticky for the thread; the *same* evaluator that picks the
  head sets the billing fraction, so the prompt and the price cannot disagree
  ([FEATURES.md:631-644](FEATURES.md), [FEATURES.md:480-496](FEATURES.md)).
- **The MCP fleet panel (bottom) — rows 2-3.** Broken out by task group — data
  handling, visualization, research (searxng web search + page fetch), cluster
  admin, and **workbench** (scratch write + execution env) — every server is reached only through the gateway's in-cluster
  relay (③ tool call, ④ result back to the model), gated per tool by the key's
  default-DENY ACLs ([FEATURES.md:551-565](FEATURES.md)).

## Where LiteLLM / Bifrost are genuinely better

Stated honestly, unchanged in substance from the long doc (§6):

- **Provider breadth.** LiteLLM's 100+ provider translation layer and Bifrost's
  20+ native providers dwarf the gateway's endpoint-pool model. For a team whose
  center of gravity is cloud model access with frequent provider churn, they
  are the right tools (long doc §6; the gateway's posture:
  [FEATURES.md:841-843](FEATURES.md)).
- **Raw latency.** Bifrost documents ~11 µs added overhead at 5 000 rps. The
  gateway publishes its own number: ~100 µs of framework stack per request
  (p99 ≈ 127 µs) and ≈ 1.3 ms p50 through a real loopback gateway hop — roughly
  two orders of magnitude behind Bifrost's proxy cost, and irrelevant next to
  hundreds-of-ms GPU TTFT. The bench harness ships in-tree
  ([scripts/bench_overhead.py](../scripts/bench_overhead.py); long doc §2 row 24, §6).
- **Ecosystem and staffing.** Large communities, plugin ecosystems, hosted UIs,
  commercial support. The gateway is a single-team internal product with
  HPE-internal support (long doc §6).
- **Mature gateway knobs & enterprise bundles.** LiteLLM's callbacks, caching
  integrations and observability sinks; Bifrost's clustering/HA and audit
  exports — real, mature capabilities (some behind a license) that the gateway
  does not attempt to match feature-for-feature (long doc §6).

## When to choose which

Condensed from the long doc (§7):

**Choose LiteLLM or Bifrost when** the workload is many *cloud* providers and
the pain is API translation; the deployment is public cloud or a non-tenant
cluster, and budgets are a cost-*reporting* concern rather than a hard
multi-tenant boundary; you need raw proxy throughput (Bifrost) or ecosystem
breadth (LiteLLM) more than per-tool governance; or you already pay for the
enterprise tier carrying the identity/SSO features you need — and your SSO
story can stop at login.

**Choose the PCAI LLM Gateway when** the deployment is an air-gapped,
Kubernetes-hosted PCAI cluster serving local models (vLLM/SGLang) to multiple
tenants; SSO should be the front door, not just the login page (humans help
themselves to a governed key, headless tokens still get ledger anchors);
session memories should be written by the fabric once instead of every harness
carrying its own plugin, prompt copy and RAG credentials; MCP tool authority
must be least-privilege *per tool* and survive upstream server upgrades (fleet
servers include cluster-mutating surfaces, where a server-level allowlist is
not an acceptable primitive); spend must be *enforced* as a query over
immutable facts — including aborted streams, mid-loop iterations and across key
rotation; and the audit story matters — findings, fixes and open items in
writing, with the regression tests that pin them.

**One sentence:** LiteLLM and Bifrost are gateways that make many providers
safely usable; the PCAI LLM Gateway is the governance plane that makes a
private model+tool fleet safely *shareable* (long doc §7).

## Sources

- Long external deep-dive (read-only):
  `general_documentation/LiteLLM_Comparison/PCAI_LLM_Gateway_vs_LiteLLM_Bifrost_v4.md`
  — 9 sections, 26-row master table, SSO walkthrough (§3), differentiators
  LiteLLM cannot replicate (§4), the self-audit (§5), the honest
  genuinely-better list (§6), when-to-choose (§7), positioning summary (§8),
  claim-to-source map (§9).
- This repo: [README.md](../README.md) · [FEATURES.md](FEATURES.md) (budget
  semantics, relay, off-peak billing, MCP federation, routing, session memory
  distiller, router extras, ledger schema) · [API.md](API.md) ·
  [MEMORY.md](MEMORY.md) · [DEPLOYMENT.md](DEPLOYMENT.md) · `AUDIT.md` +
  `REMEDIATION.md` (the self-audit trail row 11 above references).
- Diagram: [architecture.png](architecture.png) — source
  [architecture.dot](architecture.dot); re-render with
  `dot -Tpng -Gdpi=150 documentation/architecture.dot -o documentation/architecture.png`.
- Version note: the long doc documents gateway v2.3.1; this repo is at 3.0.3
  (`helm/Chart.yaml`). The structural rows above (ledger budgets, per-tool
  ACLs, relay, SSO self-mint, distiller) are design properties, not release
  snapshots; competitor claims were verified against September 2026 public
  documentation as recorded in the long doc — re-verify there before quoting
  externally.
