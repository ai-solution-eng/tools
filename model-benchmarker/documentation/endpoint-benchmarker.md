# endpoint-benchmarker — the universal tool

Deep reference for the main tool: load-benchmarking **any REST endpoint or
any MCP server** with N concurrent users looping over a reference query
pool — and, with a Prometheus URL, joining per-level GPU telemetry into a
scaling curve. Command-first playbooks: [runbook.md](runbook.md) §2–3.
What it is *for*: [FEATURES.md](FEATURES.md) § endpoint-benchmarker.

Load-benchmark **any REST endpoint or any MCP server** with N concurrent users
looping over a reference query pool — and, with a Prometheus URL, join
**per-level GPU telemetry** to produce a scaling curve (concurrency →
latency/throughput + GPU utilization). It generalizes the retired
`MultimodalRAG/tests/benchmark.py` (N simulated users over a query pool,
latency percentiles, error rates) so it drives arbitrary customer endpoints.
The historical `results/RAG/*.md` transcripts came from that retired script;
equivalent — and richer — output now comes from `--md` here.

## Highlights

- **One URL for MCP servers.** An MCP target needs *only the MCP URL*:
  connectivity is the MCP session handshake itself, discovery is `list_tools`,
  and tool arguments are built from the tool's `inputSchema` (with `--arg`
  overrides).
- **Any REST endpoint.** Method, path template, query params, JSON body
  template (`{query}` placeholder), headers — all flag-driven. Defaults still
  match the MM RAG search endpoint.
- **Sweep mode with the protocol done right.** Per-level warm-up rounds,
  settle gaps between levels, per-level wall-clock windows
  (`t_start`/`t_end`), and the telemetry fetch overlapped with the settle sleep.
- **Prometheus join over the HTTP API** (`/api/v1/query_range`). DCGM gauges
  are averaged **per GPU** over each level's measured window; an optional idle
  baseline is subtracted for shared-GPU clusters.
- **Four artifacts:** JSON run artifact (secrets redacted), per-level CSV,
  self-contained HTML report (inline SVG charts), and an **easy-to-read
  Markdown report** (`--md`) in the results-tree convention — ingested by
  `results_to_html.py` unchanged (it appears in the report's RAG tab).
- **Extended reporting:** configurable percentiles (`--percentiles 50,90,99`),
  run annotations (`--note`, repeatable — stamped into every artifact), an
  **error deep-dive** (errors bucketed http_error/timeout/connection/other,
  top raw errors per level, HTTP status-code tables), and **re-rendering a
  saved run without re-running the load** (`--from run.json`, knee re-detected
  under the current `--knee-factor`).
- **Logging you can operate.** Timestamped leveled logging (`-v` per-request
  DEBUG, `--quiet`), progress tables, per-level summaries with top errors,
  bounded error keys carrying the actual message, `--log-file` tee, and a
  `note:` system that flags weak telemetry windows.

## Quickstart

### REST — the MM RAG API (backward-compatible shape)

```bash
PYTHONPATH=src python -m model_benchmarker.endpoint_benchmarker \
    --url http://localhost:8000 --dataset my-ds -N 50
```

### REST — any endpoint (custom shape, auth, POST body)

```bash
PYTHONPATH=src python -m model_benchmarker.endpoint_benchmarker \
    --url https://their-rag.example.com \
    --method POST --path /api/v1/answer \
    --body '{"question": "{query}", "top_k": 5}' \
    --header 'Authorization=Bearer '"$TOKEN" \
    --queries-file their_queries.txt \
    -N 32 --duration 120
```

The `{query}` placeholder must sit inside quotes in the body template; it is
spliced JSON-escaped, so queries containing quotes/newlines survive.
`{dataset}` in `--path` is filled from `--dataset` (auto-discovered from
`/api/datasets` when omitted — the MM RAG behavior).

### MCP — one URL, tool auto-discovered

```bash
# inspect what the server offers (name, required args, inferred query arg):
PYTHONPATH=src python -m model_benchmarker.endpoint_benchmarker --mode mcp \
    --url https://rag.example.com/mcp --list-tools

# benchmark it: only the MCP URL — no REST URL, no port guessing
PYTHONPATH=src python -m model_benchmarker.endpoint_benchmarker --mode mcp \
    --url https://rag.example.com/mcp \
    --dataset their-dataset -N 32 --duration 120
```

Tool selection: exact `--tool` if given; otherwise a single tool is used
as-is, and among several the first search-ish name (`search`, `query`, `ask`,
`retrieve`, `find`, `lookup`) wins. The sampled query goes to the schema's
query-ish string argument (`query`, `q`, `prompt`, `text`, `search`,
`question`, `input`, `message` — or `--query-arg` to force it). `--dataset`
fills a required dataset-ish argument (`dataset_name`, `dataset`,
`collection`, …). Required arguments that can't be filled are reported
loudly — never silently sent as empty strings:

```bash
PYTHONPATH=src python -m model_benchmarker.endpoint_benchmarker --mode mcp \
    --url https://rag.example.com/mcp \
    --tool search_dataset \
    --arg top_k=10 --arg use_reranker=true \
    --arg base_llm_modalities='["text","image"]' \
    -N 8
```

Each simulated user holds its own persistent MCP session for the whole level —
the same connection model as a real LLM client. `--transport sse` is available
for older servers; `streamable-http` is the default.

## The hosted-trial GPU scaling run

```bash
PYTHONPATH=src python -m model_benchmarker.endpoint_benchmarker --mode mcp \
    --url https://rag.example.com/mcp \
    --dataset their-dataset \
    --sweep 1,4,32,64 \
    --duration 180 --settle 30 \
    --prom-url http://prometheus:9090 \
    --prom-selector 'exported_namespace="their-ns"' \
    --baseline-duration 120 \
    --output scaling.json --csv scaling.csv --md scaling.md --html report.html \
    --note 'warm-cache, HiCache x3' \
    --log-file scaling.log
```

| Piece | Why |
| --- | --- |
| `--sweep 1,4,32,64` | Run each concurrency level sequentially; per-level stats are independent, so each point on the curve is clean. |
| `--duration 180` | DCGM is scraped every ~15s. A level must outlast ~3 scrape intervals or the "GPU average" rests on 1–2 samples (the tool warns and annotates the report with per-level sample counts). 2–3 minutes per level is the sweet spot. |
| `--settle 30` | GPU utilization decays asynchronously after load stops; averaging through the decay understates the busy level. The next level's telemetry fetch is overlapped with this sleep, so settle time is nearly free. |
| `--warmup-rounds 1` (default) | One unmeasured burst of N concurrent requests per level: opens pools/sessions and populates caches before the clock starts. |
| `--prom-url` + `--prom-selector` | GPU metrics joined over the exact measured window `[t_start, t_end]` per level. With the DCGM exporter's k8s pod-mapping, `exported_namespace="their-ns"` attributes utilization to the customer's pods; fall back to `Hostname="..."` if their GPUs aren't in the mapped set. |
| `--baseline-duration 120` | Captures an **idle baseline** first (sleep, then query the window just slept) and reports `mean−idle` next to raw means — the contamination check for time-shared GPU nodes. Both raw and subtracted values are in every artifact. |
| `--md/--output/--csv/--html` | Markdown report for the results tree (and `results_to_html.py`), JSON artifact per run, flat CSV for spreadsheets, HTML report for the customer. |

Default GPU metrics: `DCGM_FI_DEV_GPU_UTIL`, `DCGM_FI_PROF_GR_ENGINE_ACTIVE`,
`DCGM_FI_DEV_FB_USED`, `DCGM_FI_DEV_MEM_COPY_UTIL`, `DCGM_FI_DEV_POWER_USAGE`
(override with `--gpu-metrics`). Add any other range query — e.g. replica
counts from kube-state-metrics, which matters if their app autoscales and
per-pod utilization stays flat while aggregate throughput rises:

```bash
--extra-range-query 'replicas=max(kube_deployment_status_replicas{namespace="their-ns"})'
```

### The Markdown report (`--md`)

Written in the results-tree convention so `results_to_html.py` ingests it
unchanged:

````markdown
# <file stem>

Status: complete (last updated 2026-09-12 14:09:36)

Universal endpoint benchmark (endpoint-benchmarker 0.2.0). Run id: …

## Benchmark configuration      ← | Metric | Value | table (mode, target, tool,
                                  dataset, load shape, percentiles, notes…)
## Benchmark results            ← run-level: totals, success rate, best
                                  throughput, knee point
### Scaling by concurrency level  ← wide table: N, reqs, ok%, rps, rps/user,
                                    configured percentiles, max, avg results/bytes,
                                    GPU_UTIL (+ −idle, samples)
### Errors by category          ← http_error / timeout / connection / other
### Error detail (top errors per level)
### HTTP status codes
### GPU telemetry per level
## Notes                        ← weak-telemetry-window warnings etc.
````

The write is atomic (`.partial` + `os.replace`): a run killed mid-write can
never leave a truncated report behind, same rule as `benchmark_chat --output`.

### Reading the curve

- The **headline number is the knee point**, printed in the summary and
  highlighted in every report: the lowest concurrency where errors appear
  (success < 99%) or tail latency exceeds `--knee-factor` (default 2.0) × the
  best level's tail. Re-tune it after the fact with
  `--from scaling.json --knee-factor 1.5` — no re-run needed.
- `GPU_UTIL` saturates early and is a poor linearity signal on its own — read
  it alongside `PROF_GR_ENGINE_ACTIVE` (truer "engine busy"), `POWER_USAGE`
  (watts ≈ real work → a throughput-per-watt story), and especially `FB_USED`
  (VRAM pressure is usually what breaks first at high concurrency).
- `rps/user` in the table is the linear-scaling yardstick: on a healthy curve
  it stays flat until the knee, then decays.

### Honest caveats (worth saying to the customer)

- **Shape, not absolutes.** On L40S the curve tells you where the knee is and
  whether degradation is graceful or a cliff — it does not predict RTX Pro 6k
  / H200 numbers.
- **Shared GPUs contaminate.** Same-tenant neighbors inflate utilization; use
  the idle baseline and, ideally, a quiet window.
- **Cache policy shapes the curve.** If their app caches embeddings/answers,
  repeated reference queries flatter the numbers. Decide up front: warm-cache
  (steady state) vs cache-busting nonces (worst case) — and stamp which one
  the report represents with `--note`.
- The client measures what the client sees. If the three models (embedder,
  reranker, chat) live in separate pods, per-GPU attribution gives you
  per-stage curves for free; if they share a GPU, ask for per-stage timings in
  their API response — client data locates the saturating stage, GPU metrics
  confirm it.

## CLI reference (universal tool)

| Flag | Meaning |
| --- | --- |
| `--mode rest\|mcp` | Target kind (default `rest`). |
| `--url` | REST base URL, or the MCP endpoint URL (the only URL MCP mode needs). |
| `--method`, `--path`, `--query-param`, `--param`, `--body`, `--body-file` | REST request shape. Defaults match the MM RAG search API. |
| `--header K=V` | Repeatable headers; applies to REST requests and the MCP HTTP transport. Redacted in artifacts. |
| `--dataset` | REST: fills `{dataset}` (auto-discovered if omitted and needed). MCP: fills a dataset-ish tool arg. |
| `--health-path` | REST probe (default `/healthz`); `''` skips. Connection failure aborts; HTTP error status only warns. |
| `--tool`, `--arg`, `--query-arg`, `--transport`, `--list-tools` | MCP selection and arguments. |
| `-N` / `--sweep` | Single level / comma-separated sweep. |
| `--duration`, `--ramp-up`, `--warmup-rounds`, `--settle`, `--call-timeout` | Load shape per level. |
| `--seed` | Reproducible query sampling (default: derived per user). |
| `--queries-file`, `--query-set generic\|vlm\|mixed` | Reference query pool (`#` comments allowed in files). |
| `--prom-url`, `--prom-selector`, `--prom-step`, `--gpu-metrics`, `--extra-range-query`, `--baseline-duration`, `--knee-factor` | Telemetry join. |
| `--output`, `--csv`, `--md/--markdown`, `--html`, `--log-file` | Artifacts (JSON / CSV / Markdown / HTML / log tee). |
| `--note TEXT` | Repeatable free-text annotation stamped into every artifact. |
| `--percentiles P1,P2,...` | Latency percentiles to report (default `50,95,99`); every table adapts. |
| `--from RUN.JSON` | Re-render `--md/--html/--csv/--output` from a saved run artifact; knee re-detected with the current `--knee-factor`. |
| `-v` / `--quiet` / `--progress-interval` | Logging controls. |
| `--insecure`, `--http2` | TLS verify off / HTTP-2 (needs `h2`). |

Exit codes: `0` run completed (with at least one success), `1` every request
failed, `2` configuration/connection error (clean message, no traceback —
`-v` re-raises), `130` interrupted.
