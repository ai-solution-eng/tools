# Features — ModelBenchmarker

Every capability in this package, what each one is *for*, and the
philosophy behind it — written for a reader with zero prior context.
"Where to look next" at the end of each section; the deployment story is
in [DEPLOYMENT.md](DEPLOYMENT.md), command-first playbooks in
[runbook.md](runbook.md).

**The vocabulary (defined once, used everywhere):**
a *serving endpoint* is anything that answers an HTTP request about a
model (an OpenAI-compatible `/v1/chat/completions` server, a RAG search
API, an MCP tool server). *TTFT* (time to first token) is how long until
the first token of a response arrives — the user-visible "is it thinking?"
delay. *tokens/s* is the decode speed of a single stream. *KV cache* is
the per-context memory a serving engine keeps so it does not recompute
past tokens; the *prefix cache* (RadixAttention, SGLang radix cache,
HiCache) reuses KV across requests that share a prefix. *Concurrency*
(`-N`, 32 users) means 32 requests in flight at once; a *sweep*
(`1,4,32,64`) repeats the run at several concurrency levels so you can
plot the degradation curve. A *measurement* (an "atom") is one (model,
context, concurrency, task, …) row of results.

---

## endpoint-benchmarker — the universal tool

The main tool: a client-side load generator that benchmarks **any**
serving endpoint — an arbitrary REST API *or* an MCP server, where an MCP
server is just a URL that speaks the Model Context Protocol (the
handshake is the connection test, `list_tools` is the discovery step,
and each tool's JSON-Schema tells the tool which argument to fill).

**What it measures:** a reference query pool (or a single query) replayed
across a concurrency sweep, with per-level latency percentiles and
throughput — and, when pointed at an in-cluster Prometheus (`--prom-url`),
a **GPU scaling curve**: DCGM exporter series (utilization, GPU memory,
power) joined over the exact measured window of each level, with an idle
baseline subtraction for time-shared nodes. That is the "how many GPUs /
which sizing tier" answer for a customer workload.

**Philosophy:**
- *One URL is the whole target spec* for MCP; for REST the request shape
  is explicit (method/path/params/body templates), with MM-RAG-shaped
  defaults so a RAG search API works out of the box.
- *The client measures what the client sees* — no server-side probes, so
  what you report is what a real user experienced.
- *Honest about shape, not absolutes:* the curve tells you where the knee
  is and whether degradation is graceful or a cliff; it does not predict
  another GPU model's numbers. Shared GPUs contaminate (use
  `--baseline-duration`); cache policy shapes the curve (state it with
  `--note`).
- *Telemetry is a join, not a dependency:* no Prometheus → the run still
  completes, just without GPU columns.

Artifacts per run: `run.json` (machine-readable), `run.csv`, `--md`
report in the committed results-tree convention, `--html` customer
report. Deep reference: [endpoint-benchmarker.md](endpoint-benchmarker.md);
playbooks: runbook.md §2–3.

## benchmark_chat — chat TTFT / tokens-per-second

The specialist for chat serving: closed-loop or open-loop user simulation
against an OpenAI-compatible endpoint, measuring TTFT, tokens/s,
inter-token smoothness and (optionally) goodput under SLO.

**What it measures and why:**
- *TTFT vs concurrency and context length* (sweep × sweep) — where the
  serving config falls over as users or prompts grow.
- *Inter-token latency (ITL) histograms, not just averages* — a server
  with identical average tokens/s can be smooth or stall-prone; ITL P99 /
  max-stall is where decode stalls live. Per-request TPOT carries a
  burst guard so a single stalled first delta can't poison the metric.
- *Goodput with SLO* (`--goodput "ttft<=2000,tpot<=50"`) — the fraction
  of requests that actually meet the SLA, the number an SLO is really
  about.
- *Open-loop arrivals (MB-C)* — requests issued on a schedule instead of
  "next when a slot frees", so **server-side queueing shows up in TTFT by
  design** (closed loop hides it). Closed and open numbers are never
  comparable; artifacts are stamped and the compare view refuses to mix
  modes.

**Cache regimes — the nonce:** by default every request gets a unique
random nonce, so prefix caches never hit and every measurement is a cold
prefill — the deliberate worst case. `--no-nonce` + `--prewarm` explore
the cache-friendly regime; the difference *is* the price of caching, and
it is measured, not assumed.

**Built-in workloads:** a fixed registry — `coding` (a production-grade
Python module), `creative` (a 1,500–2,000-word story), `mixed` — plus
`custom`. Fixed prompts make runs comparable across machines and time.

**Resume:** `--resume-from` continues an interrupted sweep; completed
levels are skipped and the final file is the union, guarded against
model/target/run-shape mismatch.

Full flag reference: [cli-reference.md](cli-reference.md); capacity-suite
playbooks: runbook.md §1.

## memory-estimate — the deterministic deployment calculator

Given a model (its `config.json`, or the deployment catalog dropdown)
and a serving configuration (GPU tier/count, tensor parallelism, KV dtype,
mem-fraction, HiCache tiers), it computes **how many concurrent contexts
fit in memory** — attention-structure-aware (GQA/MHA shard the KV head
count by TP; MLA/MQA store one deduplicated stream per token; sparse
attention, linear hybrids, Mamba2/Engram and speculative-decoding
structures are priced where the math is defined) — and renders a
PASS/WARN/FAIL checklist plus, on the web app, an interactive calculator.

**Philosophy:**
- *Deterministic math, not prediction.* It sizes memory exactly as the
  serving stack does; it deliberately does **not** predict throughput —
  a memory fit is a necessary condition, and the honest boundary is
  stated in the output.
- *Honesty knobs, visible in the output:* overhead factors are explicit
  and tunable, never a hidden fudge.
- *Measured beats estimated:* when the committed results library
  (documentation below) contains a run that matches the query, the web
  app shows the **measured** numbers with provenance instead of — never
  on top of — the estimate (measured-lookup never interpolates).

Reference: [memory-estimate.md](memory-estimate.md); the HiCache tier
math: webapp.md § HiCache tiers.

## The results tree and report generator

Committed evidence lives in `results/<model>/<SETUP>.md` — a filename
grammar (model slug + machine + engine + KV config + cache tier + replica
count) that keeps every claim about "the 4×H200 SGLang config"
traceable to a specific file. `results_to_html.py` turns the tree into a
self-contained HTML report: sweep tables, scaling-curve charts, a compare
view, and a Memory tab that joins each run to the estimate. The committed
tree is the *library*: every number in fleet documentation links to one
of these files, and the web app serves it as `lib:` rows on /results.
Format spec: [results-and-report.md](results-and-report.md); the index:
[`results/README.md`](../results/README.md).

## The web app — the PCAI deployment

A small FastAPI service (image `ghcr.io/ai-solution-eng/model-benchmarker`)
that is the PCAI deployment of this package. Pages: the **memory
estimator** (default page, public), **/benchmark/chat** and
**/benchmark/endpoint** (the launchers, API-key gated), **/results**
(public history), **/unlock** (the key form). Runs fire the bench CLIs as
subprocesses; a per-run registry tracks them (one in-flight run per
endpoint origin, deployment-wide caps) and re-reads run directories on
restart.

Capabilities beyond "button that runs the CLI":

- **Target model, not a URL whitelist:** the combobox offers
  `bench.suggestions` (advisory); any http(s) URL is acceptable,
  validated (scheme/host, no embedded credentials). Protected endpoints
  get their key from the operator's endpoint-key Secret
  (origin → key JSON, injected into the subprocess, redacted from every
  artifact); public endpoints run unauthenticated.
- **Model discovery:** `POST /api/models` probes
  `{target}/v1/models` so the chat page's Model dropdown lists what the
  endpoint actually serves (fails soft; manual entry and auto-discover
  always work). The seed-catalog dropdown (`/api/catalog`) offers model
  classes with their serving presets for the memory page.
- **The committed library (lib:):** on first launch the image's
  `results/` tree is copied onto the PVC; /results shows committed
  baselines alongside live runs, and hide/unhide is page management, not
  deletion (hidden runs stay comparable and downloadable by id).
- **The measurement store (MB-DB):** every completed run is decomposed
  into *atoms* (one row per (model, ctx, users, task, …) measurement) and
  stored in a SQLite DB on the PVC with content-hash idempotency —
  re-running a run never duplicates its rows. /results' **atom browser**
  facets the whole fleet's measurements (model × task × context ×
  concurrency) so "has anything ever measured this?" is a filter, not an
  archaeology project. Read-only APIs: `/api/store`,
  `/api/store/facets`, `/api/store/query`.
- **Compare:** tick 2–6 runs (live or `lib:`) → side-by-side tables with
  signed deltas against a movable reference, line charts with
  log-x scaling, smoothing/spike-suppression, per-section raw tables, CSV
  export. Public read-only like the rest of /results.
- **The measured panel:** on the memory page, when the library has a run
  matching the query, measured numbers appear *below* the estimate with
  provenance — measured, never interpolated into the estimate.
- **Sequencer (MB-SEQ):** `model-benchmarker-seq` runs a committed plan
  (JSON: ordered runs, settle delays, stop-on-failure) against the web
  app with its own key — the scripted version of "run the whole capacity
  suite overnight".
- **Gating philosophy:** the surface that *starts or mutates* work is
  key-gated; the surface that *reads results* is public by design —
  "reports are the point; share links without handing out the launch
  key." Full route map: DEPLOYMENT.md § Authentication and SSO; app
  walkthrough: webapp.md.

## Fleet utilities (what the package carries, and why)

The package vendors the fleet's shared utilities (`src/model_benchmarker/utils/`)
— the same inodes as the other fleet repos via the hardlink mesh. The
ones a reader of this package actually meets:

- **`mcp_auth.py`** — the K8S-MCP auth pattern the web app runs:
  API-key middleware that is *not* an auth server — keys live in per-app
  Secrets, are **re-read on every request** (comma-separated old+new =
  zero-downtime rotation), matched in constant time, with caller
  *attribution* (never authorization) logging, trusted-CIDR bypasses for
  in-cluster scrapers, and acceptance of the SSO-forwarded
  `X-Auth-Request-Access-Token` as a key envelope.
- **`oidc_identity.py`** — the D21 token-verification machinery:
  RS256/JWKS OIDC token validation (iss/aud/exp checks, a short negative
  cache so a down IdP is not hammered per request, blocked-client
  overlay).
- **`oidc_sso.py`** — the D22 browser SSO flow (OIDC authorization code,
  `/oauth/login` + callback, 12h stateless HttpOnly cookie, in-cluster
  IdP discovery, inert by default). **Bundled with this package but not
  wired into the web app's routes** — the deployed SSO surface is the
  gateway's oauth2-proxy AuthorizationPolicy (DEPLOYMENT.md §
  Authentication and SSO); this module documents where in-app SSO would
  attach if the app ever grows its own.
- **`pcai_model_classes.py`** — the platform model classes: the
  `.serving.` → in-cluster Service URL rewrite rule the chat page's
  local/remote toggle uses, the request timeout contract (connect 30s /
  read 600s), and the sync→async bridge pool.
- **`pcai_models.py`** — the *developer* model registry (named entries
  like `qwen38_27B`). **Excluded from the shipped image by the
  Dockerfile's build-time credential boundary** — the entries carry
  platform JWTs, and the build FAILS if the file ever reaches the context.
  The deployed app addresses targets by URL + Secret-sourced keys and
  never imports it; `--model_class_name` is a dev-machine convenience
  only.
- **`token_text_splitter.py`** — token-count-aware text chunking via a
  bundled HuggingFace tokenizer (CPU-only, no runtime download; falls back
  to character chunking when the tokenizer file is absent).
- **`fleet_reconcile.py`** — the operator's drift tool for the
  hardlink mesh: sha256-baselined, direction-decidable reconcile
  (PROMOTE/REPAIR/NORMALIZE/CONFLICT) across the pcai_utils source, the
  repo copy and the pcai-solutions mirror; dry-run by default.

## Roadmap — the staged tool fusion

1. *Done:* one repo, one package — the universal tool is the main tool;
   chat benchmarking stays the TTFT/tokens-s specialist; the last
   single-purpose script (MultimodalRAG's `tests/benchmark.py`) is
   retired.
2. *Next:* chat streaming as a target mode of the universal engine
   (TTFT/tokens-s per level alongside the REST/MCP path), unifying the
   two sweep loops.
