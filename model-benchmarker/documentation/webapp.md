# The ModelBenchmarker web app (PCAI deployment)

One image, one chart, three pages:

| Page | Path | Access | What it does |
|---|---|---|---|
| **LLM memory estimator** (the default page) | / | public | Deterministic fit / KV-token / concurrency math from a model's config.json + your serving parameters. The same math as the memory-estimate CLI. |
| **Chat benchmark** | /benchmark/chat | API key | Fire benchmark_chat (TTFT / tokens-s / ITL / TPOT / goodput) at an allowlisted OpenAI-compatible endpoint -- closed- or open-loop. |
| **Endpoint benchmark** | /benchmark/endpoint | API key | Fire endpoint_benchmarker (any REST endpoint or MCP server, sweeps, GPU scaling curve) at an allowlisted target. |

The benchmark pages replace the notebook flow: fill a form, launch, watch the
log tail, download the artifacts (report.md, report.html, run.json, run.csv,
run.log) when the run finishes. Runs execute on the server as subprocesses of
the documented CLIs; nothing about the bench engines changes.

## Deployment (PCAI)

1. Import the packaged chart (model-benchmarker-<version>.tgz) into PCAI once.
2. Create the key Secret (or let the chart auto-generate one -- see the release NOTES):

        kubectl -n <ns> create secret generic bench-platform-keys \
          --from-literal=BENCH_API_KEYS="$(openssl rand -hex 16)"

3. Open the release's **Helm Values** editor and paste
   helm/values-examples/values.g2.yaml (or values.hosted-trial.yaml), then adjust
   the "# SITE:" lines -- above all **bench.endpoints**, the allowlist of
   benchmarkable targets, and security.existingSecret for the key.
4. Apply. PCAI substitutes the ${DOMAIN_NAME} placeholder before rendering.

Operators with cluster access can render-check first:

    helm template <release> helm/ -f helm/values-examples/values.g2.yaml

## The seed-catalog dropdown (memory estimator)

The memory page's Model field is a searchable combobox (same widget as the
benchmark pages' target field) fed by GET /api/catalog: every deployment in
the Model-Downloader **seed catalog** (the same seed_catalog.json the
downloader and the report use), with its serving preset -- GPU tier/count,
TP, KV dtype, mem-fraction. Picking an entry fills the GPU/TP/KV/mem-fraction
controls so the estimate starts from the real deployment shape. Like the
target suggestions, it is ADVICE ONLY: any HF repo id or local path stays
typeable, and pasting a config.json still works offline. Config fetches are
cached on the work dir (.hf-cache on the PVC).

### HiCache tiers on the memory page

The KV-cache section has a **HiCache L2 (host RAM)** selector plus an
optional **L3 (backing tier)** field. The estimate renders a per-tier table
— L1 device pool, L2 host tier, L3 backing tier — with cached-token counts
and per-tier concurrency at the largest context, plus an addressable-total
line. Semantics match the engines (SGLang HiCache): L2 is instance-private
(one pool per replica, so N replicas multiply it), ratio mode satisfies
"L2 tokens = ratio × device tokens per replica", L3 is capped at the L2
pool, and MLA/MQA layouts store one deduplicated stream per token while
GQA/MHA shards 1/tp per rank (an explicit layout override exists for
engines that differ). Host tiers extend what the deployment can HOLD, not
the hot decode pool — the page says so next to the total.

## Comparing runs on the results page

/results lists every run on the PVC and supports **side-by-side
comparison**: tick 2–4 runs (checkbox per row, or "add to compare" inside a
run's detail view) and press Compare — or deep-link `/results?compare=<id1>,<id2>`.
The comparison is backed by public GET /api/results/compare?ids=… (read-only,
same posture as the rest of the results surface): rows are aligned across
runs by level label, each later run's values carry signed percent deltas
against the reference run (the first selected run of each kind; positive =
better, direction-aware — faster TTFT and higher throughput both show
green), runs of different kinds land in separate sections with their own
reference and columns, and a "copy as CSV" button exports the merged
table. Runs missing their machine-readable artifacts (interrupted runs)
appear with a note rather than silently vanishing.

### The results library (committed baselines)

Below the PVC run table, /results also renders the **results library**: the
repo's committed `results/` tree — the curated per-(model × setup)
benchmarks whose filenames encode the serving setup
(`H200_sglang_dflash2_hicachex3_replicasx3.md`), scanned by the same
parsers as the HTML report and served by public GET /api/library. Chat
artifacts from the library join the SAME compare flow: their checkbox
picks them as a baseline (`lib:<model>/<stem>` id), so a fresh PVC run can
be compared against a committed HiCache×3 setup directly. Memory-estimate
artifacts appear for reference but are not comparable (capacity tables,
no latency rows). Deployed images without a `results/` tree simply hide
the section; `BENCH_RESULTS_DIR` points at an explicit tree (an
explicit-but-absent value disables the library).

## The two gates

**1. The web-app API key** (your Secret, BENCH_API_KEYS) protects the
benchmark surface: /benchmark/chat, /benchmark/endpoint, /api/runs*,
/api/endpoints. The fleet mcp_auth middleware enforces it (constant-time
compare, X-API-Key or Authorization: Bearer), and keys are re-read **per
request**, so rotating the Secret takes effect without a restart. A browser
without the key gets a 401; the page asks for the key once and keeps it in
sessionStorage for the tab (it never lands in a URL or a cookie). The
estimator page and its /api/estimate are deliberately public -- deterministic
math, no secrets, no cluster access.

**2. The endpoint allowlist** (bench.endpoints) decides *what* can be
benchmarked. Targets are configured by the operator in chart values; the
browser can never supply a URL. An **empty allowlist disables benchmarking
outright** (fail-closed) while the estimator page keeps working. Endpoint API
keys (bench.endpointApiKeys / endpointApiKeysExistingSecret) are injected
into the bench subprocess environment (PCAI_API_KEY / a Bearer header) and
are never served back to the browser -- the API redacts them.

## One endpoint at a time

A benchmark run is a load generator: two concurrent sweeps against the same
endpoint would contaminate each other's measurements. The run registry keeps
one in-flight run per endpoint **deployment-wide** -- a second run naming a
busy endpoint is refused with HTTP 409 and the UI says which run holds it.
Different endpoints can run in parallel, up to
BENCH_MAX_CONCURRENT_RUNS (default 2). A deployment-wide cap
(BENCH_MAX_RUN_SECONDS, default 7200 s) hard-stops runaway runs.

## Prometheus telemetry (the scaling curve)

bench.prometheusUrl is operator-configured (the browser never picks the
scrape target); the user-facing knob is the PromQL label selector
(exported_namespace="their-ns" and friends). With a URL configured, the
endpoint benchmark joins per-GPU DCGM telemetry over each level's measured
window -- the hosted-trial scaling curve -- exactly as the CLI's --prom-url.

## Values that matter

| Values key | Meaning |
|---|---|
| bench.endpoints[] | The allowlist (name/url/kind). Empty = benchmarks disabled. |
| bench.endpointApiKeys | JSON object of target keys (Secret-sourced; never served back). |
| bench.prometheusUrl | Operator Prometheus for the GPU join (empty = latency-only). |
| security.existingSecret | Your Secret holding BENCH_API_KEYS (recommended). |
| security.apiKey | Inline seed for the auto-managed release Secret (first install). |
| persistence.* | Run-artifact PVC (kept on release delete: resource-policy: keep). |
| extraEnv | Pass-through: BENCH_MAX_CONCURRENT_RUNS, BENCH_MAX_RUN_SECONDS. |

## Runs and artifacts

Every run writes to <persistence.mountPath>/runs/<run-id>/:

- run_meta.json -- status/params (redacted); the registry re-reads these on
  pod restart, so history survives updates;
- run.log -- the CLI's full log (streamed as a tail in the UI);
- chat: report.md -- the results-tree artifact;
- endpoint: run.json, run.csv, report.md, report.html.

## Honest boundaries

- The webapp starts runs; it is not a job scheduler. A killed pod marks its
  in-flight runs "interrupted" on restart.
- Chat benchmarks have no GPU-telemetry join (the CLI doesn't either -- yet;
  roadmap item 2 fuses the engines).
- The registry's single-flight guarantee is per-pod-state: with
  replicaCount > 1 on an RWX volume, two replicas each see the run history
  from the shared PVC, but a brand-new run is admitted per-replica (the
  in-flight lock is in-memory). Keep replicaCount: 1 unless you accept that.
