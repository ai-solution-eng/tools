# ModelBenchmarker

## What it is

Benchmarking tools for HPE Private Cloud AI (PCAI) / Ezmeral model
endpoints, in one repo and one Python package (`model_benchmarker`). The
bench tools are **client-side load generators**: they point at endpoints
that already exist and measure what a real client sees. Four entry points
around that idea:

| Tool | Entry point | Measures | Use it for |
|---|---|---|---|
| **endpoint-benchmarker** — the universal, main tool | `PYTHONPATH=src python -m model_benchmarker.endpoint_benchmarker` (or `endpoint-benchmarker` once `pip install -e .`) | Load benchmarking of **any REST endpoint or any MCP server**: req/s, latency percentiles, error rates, GPU scaling curves via a Prometheus DCGM join | RAG search APIs, MCP tools, arbitrary customer endpoints — the hosted-trial "how does *your* app scale" run |
| **benchmark_chat** | `PYTHONPATH=src python -m model_benchmarker.benchmark_chat` (or `benchmark-chat`) | **TTFT + tokens/s + ITL/TPOT + goodput** for OpenAI-compatible chat endpoints under closed- or open-loop load | Chat-model capacity studies: multiturn, context padding, thinking budgets, SLO capacity |
| **memory-estimate** — deterministic memory math | `PYTHONPATH=src python -m model_benchmarker.memory_model` (or `memory-estimate`) | **Fit verdict + KV token capacity + concurrent requests** from the model's own `config.json` structure (GQA/MLA/sparse/linear-hybrid) and GPU/TP/mem-fraction inputs — no load, no endpoint | Sizing *before* deploying: which GPU count / TP level fits, how many N-ctx users the KV pool holds |
| **web app** — the PCAI deployment | `PYTHONPATH=src python -m model_benchmarker.webapp` (or `model-benchmarker-web`); deployed via `helm/` | The memory estimator as the **default web page** plus **browser-fired benchmarks** (chat + universal), API-key gated, one benchmark per endpoint at a time | Fire benchmarks from an HTML page instead of a notebook; share the estimator with users who will never open a terminal |

What each one is *for*, and the philosophy behind the metrics, is
[documentation/FEATURES.md](documentation/FEATURES.md).

## The problem it solves

Every deployment decision in a PCAI cluster is one of these four
questions, and each has a tool here:

- **"How does *their* app scale?"** (hosted trials) — a customer RAG/MCP
  workload on shared GPUs: at what concurrency does it degrade, where is
  the knee, and what are the GPUs actually doing at each level?
  endpoint-benchmarker + the Prometheus GPU join.
- **"Will it fit?"** — this model, this TP size, this KV dtype, this
  mem-fraction: does it deploy, and how many concurrent contexts does the
  KV pool hold? memory-estimate — deterministic math, honest boundaries,
  no throughput guessing.
- **"Does it meet the SLO?"** — chat TTFT/tokens-s/ITL under sustained
  concurrency and context length, goodput against explicit thresholds.
  benchmark_chat, closed-loop for saturation, open-loop to expose
  server-side queueing by design.
- **"Which config do I commit to?"** — the same model across hardware
  counts, engines, speculative decoding, weight formats, HiCache tiering:
  the committed `results/` tree is the evidence; the HTML report compares
  it side by side.

## Where it fits in PCAI (architecture)

- **The web app is the PCAI deployment.** One image
  (`ghcr.io/ai-solution-eng/model-benchmarker`), one Helm chart
  (`helm/`, chart `model-benchmarker`), one gateway route (a VirtualService
  on the shared `ezaf-gateway`). The estimator is the default page; the
  benchmark launcher pages fire the CLIs **as subprocesses of the app
  pod** — there is no separate engine deployment.
- **Targets are whatever already runs on the unit:** model endpoints
  served through MLIS (their public URL, or the in-cluster
  `.serving.`/service DNS), MCP servers, or arbitrary REST APIs. The
  app never deploys or mutates anything it benchmarks; it is a client.
- **Evidence is committed, not ephemeral.** Every run writes artifacts
  into a per-run directory on the app's PVC; the repo's `results/` tree
  holds the curated, committed runs that back fleet claims, and the web
  app serves that same tree as its committed-results library.
- **Neighbouring repos:** **ModelDownloader** (`../ModelDownloader`)
  provides the `seed_catalog.json` that seeds the estimator's model
  dropdown and links runs to their deployment config in the report;
  **MultimodalRAG**'s retired `tests/benchmark.py` (MCP mode) is what the
  universal tool replaced — its historical transcripts live in
  `results/RAG/`; **ModelDeploymentApproaches** holds ad-hoc serving
  experiments that the benchmarks here are how candidate setups get
  evaluated. (The former sibling repo `EndpointBenchmarker` is merged in
  as `src/model_benchmarker/endpoint_benchmarker/`.)

## Capabilities

| Capability | What it does | Documented in |
|---|---|---|
| Universal REST/MCP load benchmarking | Any endpoint, N users over a reference query pool, per-level percentiles, error deep-dive, `--from` re-render without re-running | [endpoint-benchmarker.md](documentation/endpoint-benchmarker.md) |
| GPU scaling curves | DCGM telemetry joined over each measured level window, idle-baseline subtraction, knee detection | [endpoint-benchmarker.md](documentation/endpoint-benchmarker.md) |
| Chat capacity benchmarking | TTFT/tokens-s/ITL/TPOT + goodput, closed- and open-loop arrivals, multiturn, cache regimes (nonce/no-nonce/prewarm), thinking budgets, `--resume-from` | [cli-reference.md](documentation/cli-reference.md) |
| Deterministic memory estimation | Fit verdict, KV capacity, concurrent-request grid per attention structure; HiCache L2/L3 tier math; PASS/WARN/FAIL checklist | [memory-estimate.md](documentation/memory-estimate.md) |
| Interactive HTML deployment check | Self-contained `memory_check.html` for users to validate their own serving config offline | [memory-estimate.md](documentation/memory-estimate.md) |
| Results tree + HTML report | Committed `results/<model>/<SETUP>.md` evidence with a filename grammar; `results_to_html.py` renders one self-contained report (sweeps, scaling curves, compare view, Memory tab) | [results-and-report.md](documentation/results-and-report.md), [`results/README.md`](results/README.md) |
| Web app (the PCAI deployment) | Estimator page; key-gated chat/endpoint launchers; public results history; committed library (`lib:` rows); **measurement store (MB-DB)** with atom browser; 2–6-run compare with charts; measured panel on the estimator; `/unlock` key form; MB-SEQ plan sequencer | [webapp.md](documentation/webapp.md) |
| Fleet utilities | K8S-MCP-pattern API-key auth (per-request re-read, zero-downtime rotation); OIDC token verification (D21); the D22 browser-SSO module (bundled, **inert** in this app — the deployed SSO surface is the gateway policy); platform model classes + `.serving.` URL rewrite; token-aware splitter; hardlink-mesh reconcile tool | [FEATURES.md](documentation/FEATURES.md) |

**Roadmap — the staged tool fusion:** (1) done — one repo, one package,
the universal tool is the main tool; (2) next — chat streaming as a
target mode of the universal engine (`--mode chat`), one sweep producing
client stats *and* streaming metrics with the same GPU join; (3) later —
agent benchmarking: one "task" is an agentic loop (LLM calling tools over
turns) measured as a span tree with per-phase (LLM / tool / queue) time
attribution and query-band grouping.

## Deploying (the PCAI way)

Deployment is a values problem. Import the packaged chart into PCAI
**once**; after that, the **Helm Values** editor is the only operator
surface — every change below is edited in values and re-applied. Plain
`helm install`/`kubectl apply` exist as a labelled developer/secondary
path only ([DEPLOYMENT.md](documentation/DEPLOYMENT.md) § Developer and
secondary paths).

1. **Import** `model-benchmarker-1.1.2.tar.gz` into PCAI; open the
   deployment's **Helm Values** editor.
2. **Paste** `helm/values-examples/values.hosted-trial.yaml` (hosted
   trial) or `values.g2.yaml` (internal G2), then adjust the site lines:
   `ezua.virtualService.endpoint` (keep the `${DOMAIN_NAME}`
   placeholder — PCAI substitutes it before rendering),
   `persistence.storageClass`, `image.repository`/`imagePullSecrets`
   if the unit pulls from a mirror.
3. **Secrets:** either let the chart generate the web key on first
   install (`model-benchmarker-keys`, 32-char, preserved on every later
   apply) or point `security.existingSecret` at your own; add endpoint
   keys via `bench.endpointApiKeys` or
   `bench.endpointApiKeysExistingSecret`.
4. **Apply**, then smoke-test: `/healthz` → ok, `/` → the estimator
   page; the first benchmark page shows the `/unlock` key form.

Full walkthrough, both deployment profiles, the complete values
walkthrough and the values→env-var mapping:
[documentation/DEPLOYMENT.md](documentation/DEPLOYMENT.md).

## Prerequisites (before you deploy)

**Secrets** (purpose + what breaks without each):

| Secret | Key(s) | Purpose | Failure mode if absent |
|---|---|---|---|
| `model-benchmarker-keys` (**chart-created** on first install; lookup-preserved on upgrade; deleted with the release) | `BENCH_API_KEYS` (seed via `security.apiKey` or auto-generated 32-char); `BENCH_ENDPOINT_API_KEYS` (JSON, seeded via `bench.endpointApiKeys`, sticky on upgrade) | The app's web key + target endpoint keys | None for the chart one — it is created. A wrong `security.existingSecret` name instead fails the pod at creation (`CreateContainerConfigError`) |
| Web-key Secret, any name (e.g. `bench-platform-keys`) — **operator** | `security.existingSecretApiKey` (default `BENCH_API_KEYS`) | Operator-managed web key (G2 pattern); sole source when set | Pod stuck `CreateContainerConfigError` |
| Endpoint-key Secret, any name — **operator** | `bench.endpointApiKeysExistingSecretKey` (default `endpoint-api-keys`) — JSON object: exact target origin → key | Target credentials for protected endpoints (never served to the browser; injected into the bench subprocess, redacted from artifacts) | Pod fails at creation; invalid JSON → keys ignored at startup, protected targets 401 |
| Image pull Secret — **operator**, only for private mirrors | standard pull-secret | Pulling the app image | `ErrImagePull` back-off, pod never starts |

**Patches / cluster objects** (what the chart does *not* create):

| Object | Why | What breaks without it |
|---|---|---|
| VirtualService on `istio-system/ezaf-gateway` (chart-rendered; needs `ezua.virtualService.endpoint`) | The app's public HTTPS URL | No gateway URL (in-cluster access works); empty endpoint fails the render while `ezua.enabled` |
| oauth2-proxy CUSTOM action provider (customer clusters) + `ezua.authorizationPolicy` | Gateway-level SSO in front of the app's key gate | SSO policy can't resolve its action; the app's own key gate remains the protection |
| StorageClass (`persistence.storageClass`) | Binds the 10Gi RWO PVC `<name>-data` | PVC `Pending` → pod `ContainerCreating` |
| Kyverno ClusterPolicy (optional, `kyverno.enabled`; needs cluster-admin) | Restamps the `hpe-ezua` vendor labels at admission | Nothing — the chart carries those labels statically; the EZUA card badges on label match |
| In-cluster Prometheus (`bench.prometheusUrl`, optional) | The GPU scaling curve | Runs complete without GPU columns |

Full detail — verify commands, kubectl create examples, the complete
values walkthrough: [DEPLOYMENT.md](documentation/DEPLOYMENT.md) §
Required Secrets and § Prerequisites.

## Day-2 operations

- **Rotate the web key without downtime:** set
  `BENCH_API_KEYS` to `"oldkey,newkey"` (comma-separated) in the
  Secret, re-apply — the app re-reads the key list on **every request**,
  so both work simultaneously; drop the old key afterwards.
- **Upgrade (new image / chart values):** the PVC is untouched and the
  chart-generated keys are preserved by lookup — nothing to do. The run
  registry re-reads run directories on startup; a run in flight during
  the rollout is marked interrupted (not resumable mid-run).
- **Uninstall:** run history SURVIVES (the PVC carries
  `helm.sh/resource-policy: keep`), but the Secrets do not — the next
  install generates a fresh web key; re-key callers and clear browser
  cookies. Delete the PVC by hand to wipe history.
- **Run lifecycle:** one in-flight benchmark per endpoint origin
  (second one → HTTP 409, the UI says which run holds it); deployment-wide
  caps via `extraEnv` — `BENCH_MAX_CONCURRENT_RUNS` (default 2, 429
  beyond), `BENCH_MAX_RUN_SECONDS` (default 7200) hard-stops runaway
  runs. Keep `replicaCount: 1` — the in-flight lock is per-replica.
- **Long trials:** grow the PVC before runs out of room (10Gi default
  holds a substantial history; artifacts are per-run directories).
- **Customer-cluster SSO:** enable `ezua.authorizationPolicy` when the
  unit's oauth2-proxy CUSTOM provider exists; internally leave it off.

Operational playbooks and the troubleshooting table:
[documentation/runbook.md](documentation/runbook.md); app-level detail:
[documentation/webapp.md](documentation/webapp.md).

## The results you get

Every run writes a per-run directory (`runs/<run-id>/` on the PVC:
`run_meta.json`, `run.log`, plus artifacts), and committed runs live in
the results tree:

| Run kind | Artifacts |
|---|---|
| Chat benchmark | `report.md` in results-tree convention (per-level tables: TTFT, tokens/s, ITL, TPOT, goodput) |
| Endpoint benchmark | `run.json` (machine-readable, secrets redacted), `run.csv`, `--md` report, self-contained `report.html` |
| Memory estimate | results-tree Markdown (rendered in the report's Memory tab) |

Committed files follow a filename grammar —
`results/<model>/<SETUP>.md` where SETUP encodes machine, engine, KV
config, cache tier and replica count — so "the 4×H200 SGLang config" is
always a link to a specific file. `results_to_html.py` renders the whole
tree into one self-contained HTML report:

```bash
python3 src/model_benchmarker/results_to_html.py            # self-contained HTML
python3 src/model_benchmarker/results_to_html.py --open     # + open in browser
```

Reading one real row (worked example), the ITL/TPOT/goodput semantics and
the comparison caveats: [results-and-report.md](documentation/results-and-report.md);
the index of committed runs and what to look for:
[`results/README.md`](results/README.md).

## Documentation map

| Document | What it is | Read it when |
|---|---|---|
| [FEATURES.md](documentation/FEATURES.md) | Every capability, what it's for, and the philosophy — zero-context orientation | First |
| [DEPLOYMENT.md](documentation/DEPLOYMENT.md) | Deployment the PCAI way: quickstart, profiles, required Secrets, cluster-object prerequisites, full values walkthrough, auth/SSO, storage lifecycle | Deploying, or operating, the app |
| [webapp.md](documentation/webapp.md) | The web app end to end: pages, the exact gate map, target model, model discovery, run registry, library, measurement store, compare, sequencer, honest boundaries | Using or tuning the deployed app |
| [runbook.md](documentation/runbook.md) | Command-first playbooks per use case + the troubleshooting table | Running a benchmark |
| [cli-reference.md](documentation/cli-reference.md) | Every `benchmark_chat` flag, how it works, metric definitions, env vars, failure behavior | Tuning a chat run |
| [endpoint-benchmarker.md](documentation/endpoint-benchmarker.md) | The universal tool: MCP/REST targets, the GPU scaling run, CLI reference, reading the curve, honest caveats | Benchmarking an arbitrary endpoint |
| [memory-estimate.md](documentation/memory-estimate.md) | The math (per attention structure), HiCache tier math, the interactive calculator, every flag | Sizing a deployment |
| [results-and-report.md](documentation/results-and-report.md) | Result-file anatomy, filename grammar, report options, comparison caveats | Reading or publishing results |
| [`results/README.md`](results/README.md) | Index of the committed runs and how they were produced | Chasing a specific number |
| `helm/values-examples/README.md` | The two paste-ready values documents (G2 / hosted trial) and what to adjust | Pasting values |

## Development

```bash
pip install -r requirements.txt
python3 -m pytest tests/           # full suite (no network needed)
ruff check src tests && ruff format --check src tests
mypy src/model_benchmarker
```

The suite runs the universal tool's real drivers end-to-end against a
local aiohttp REST server and a real MCP server
(`MCPServer.streamable_http_app()` under uvicorn), plus a fake Prometheus
for the telemetry join, and checks the markdown output round-trips through
`results_to_html.py`'s parsers. Locally, the web app runs without a
cluster (estimator open, benchmark pages gated-open with a loud warning
until `BENCH_API_KEYS` is set):

```bash
PYTHONPATH=src python -m model_benchmarker.webapp --port 8000
```

**Maintainer tooling:** `./automation.sh <version>` is the release stub
(shared version bumper + `prune_charts.py`) that keeps the fleet release
interface uniform. `python3 hardlinker.py --config hardlink_config.json
[--run] [--prune]` syncs this repo into
`pcai-solutions/tools/model-benchmarker/` as a hardlink mirror (bare
runs are dry-run; the ignore list excludes `pcai_models.py` — embedded
cluster URLs/keys — caches and archives). `src/model_benchmarker/utils/pcai_models.py`
is likewise **excluded from the shipped image** by the Dockerfile's
build-time credential boundary: the deployed app addresses targets by URL
+ Secret-sourced keys and never imports it, so `--model_class_name` is a
dev-machine convenience only.
