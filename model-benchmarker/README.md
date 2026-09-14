# ModelBenchmarker

One repo, two complementary benchmarking tools:

| Tool | Entry point | Measures | Use it for |
|---|---|---|---|
| **endpoint-benchmarker** — the universal, main tool | `PYTHONPATH=src python -m model_benchmarker.endpoint_benchmarker` (or `endpoint-benchmarker` once `pip install -e .`) | Load benchmarking of **any REST endpoint or any MCP server**: req/s, latency percentiles, error rates, GPU scaling curves via a Prometheus DCGM join | RAG search APIs, MCP tools, arbitrary customer endpoints — the hosted-trial "how does *your* app scale" run |
| **benchmark_chat.py** | `PYTHONPATH=src python -m model_benchmarker.benchmark_chat` (or `benchmark-chat`) | **TTFT + tokens/s** for OpenAI-compatible chat endpoints under concurrency | Chat-model capacity studies: multiturn, context padding, thinking budgets |

`benchmark_chat.py` load-benchmarks **OpenAI-compatible chat-completion endpoints** — model endpoints deployed through MLIS on HPE Private Cloud AI (PCAI) / Ezmeral Unified Analytics and served by stacks such as vLLM or SGLang — under configurable concurrency and context-length sweeps. It reports **time-to-first-token (TTFT)** and **generation throughput (tokens/s)** as P50/P95/P99/P100 percentiles per (context, users, task) level, writes each run to a Markdown artifact under `results/`, and ships `results_to_html.py`, which renders the whole `results/` tree into one self-contained HTML report for comparing serving setups (GPU count, engine, speculative decoding, weight format, HiCache tiering, replicas).

The tool is a client-side load generator: it points at endpoints that already exist. It contains no Dockerfile, no Helm charts and no deployment logic — this repo never runs `helm`/`kubectl`; where cluster context matters, it is about *reaching* endpoints deployed through MLIS/PCAI.

---

# Part 1 — endpoint-benchmarker (universal, main tool)

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

---

# Part 2 — benchmark_chat (chat TTFT / tokens-per-second)


## What problem(s) it solves

- **Compare serving configurations before committing to one.** The same model on different hardware counts, engines (vLLM vs SGLang), speculative-decoding/MTP configs, weight formats (FP8/NVFP4), HiCache tiering or replica counts, measured on identical workloads — exactly what the committed artifacts in `results/` encode per file.
- **Measure sustained concurrency.** How TTFT and tokens/s degrade as concurrent users scale (e.g. 1 → 4 → 32 → 64) and as the input prefill grows (0 → 8k → 32k → 64k tokens).
- **Separate cold prefill from cache benefit.** A per-request random nonce defeats server prefix/KV caches by default (worst case); `--multiturn`, `--no-nonce` and `--prewarm` engage the prefix/KV/HiCache path instead, and multiturn runs split TTFT into `turn1` (cold) vs `TTFT-post` (cached) so the cache's contribution is quantified.
- **Produce repeatable result artifacts.** Every run writes a GitHub-flavored Markdown table that is rewritten after each completed sweep level (atomic write, `Status:` stamp), so even an interrupted benchmark leaves its completed levels on disk; the HTML report then compares setups side by side on shared (ctx, users, task) workloads.
- **Diagnose endpoint behavior.** `--debug_stream` reveals what a server actually streams (TTFT reading 0 usually means text arriving under a non-standard delta field), and the client warns when a concurrency level exceeds the local connection pool — the classic case of benchmarking your client instead of the server.

## How it works

```
benchmark_chat.py
  ├─ resolve endpoint ── --model_class_name (registered entry in utils/pcai_models.py)
  │                      or --url (any OpenAI-compatible root, no /v1; served model id
  │                        auto-discovered via GET /v1/models when not set)
  │                      --remote switches the in-cluster service URL to the public one
  ├─ for each --context_length level   (optional --prewarm first)
  │    ├─ for each --number_users level
  │    │    ├─ default:        N asyncio users × requests_per_user independent requests
  │    │    ├─ --multiturn:    each user runs N turns of ONE growing conversation
  │    │    └─ --separate_tasks: one clean pass per task (no cross-task mixing)
  │    └─ per (ctx, users, task): P50/P95/P99/P100 for TTFT and tokens/s
  └─ print table to stdout + rewrite --output Markdown after every completed level
```

Each request is one **streamed** chat completion (`stream_options.include_usage`). The two metrics:

| Metric | Definition |
|---|---|
| **TTFT (ms)** | Time from request send to the first chunk carrying any text delta — `content`, `reasoning_content`, or any other non-empty string delta field, so exotic stream shapes never read TTFT = 0. |
| **tokens/s** | `usage.completion_tokens / (stream_end − first_token)` — generation throughput **excluding TTFT**. Falls back to counting content+reasoning chunks when the stream carries no `usage`, and to the full request time when the response arrives in one burst (short generations). |

Percentile semantics: for TTFT the percentiles are ascending (P100 = slowest request, bigger = worse); for tokens/s they are **inverted** (P100 = slowest stream), so higher is better everywhere on that block.

### Built-in workloads

`--tasks` selects from a fixed registry (`coding` = production-grade Python LRU-cache module, `creative` = a 1,500–2,000-word literary story, `mixed` = both in one response) plus `custom` (your `--prompt` with `--max_tokens`/`--temperature`/`--top_p`). Output caps: coding 8192, creative 16384, mixed 24576 tokens.

### Cache regimes — why the nonce matters

By default every request gets a unique random nonce prepended, so no two requests share prefix tokens: prefix caches (RadixAttention, SGLang radix cache, HiCache tiers) never hit, and every measurement is a cold prefill — the deliberate worst case. Three flags explore the cache-friendly regimes:

- `--multiturn` — each user runs `--requests_per_user` turns of one growing conversation (full history replayed each turn, assistant `content` only — reasoning tokens are not resent, matching real chat clients). Turns 2+ reuse the shared prefix, giving the `turn1` vs `TTFT-post` split.
- `--no-nonce` — all requests (across all users) share one identical fixed prefix, so the server can reuse its prefix cache across users. Compare against the same run without it to price cache sharing.
- `--prewarm` — before each context level, fire one shared-prefix request per task so the prefix graph is resident *before* users arrive. Pairs with `--no-nonce`; alone it warms a prefix nothing will reuse.

## Usage

Requirements: Python ≥ 3.12, then:

```bash
pip install -r requirements.txt
```

No package install is needed — the scripts bootstrap `sys.path` and are run by path from anywhere in the repo (or its hardlinked mirror):

```bash
python src/model_benchmarker/benchmark_chat.py ...
python src/model_benchmarker/results_to_html.py ...
PYTHONPATH=src python -m model_benchmarker.endpoint_benchmarker ...
```

Or install editable with console scripts (`endpoint-benchmarker`, `benchmark-chat`):

```bash
pip install -e .
```

The examples below abbreviate to `python benchmark_chat.py`; substitute the full path when running from elsewhere.

### Pointing at an endpoint

- `--model_class_name NAME` — a registered model from `src/model_benchmarker/utils/pcai_models.py` (`deepseek_v4_flash_280B`, `qwen38_27B`, `gemma4_31B`, `glm_53_flash_331B`, …). Entries are tagged `currently_deployed`; a not-deployed entry fails every request, so pick a deployed one or use `--url`. This module embeds cluster-internal URLs and bearer keys, so it is **excluded from the mirrored deployment** — there, `--url` is required.
- `--url URL` — any OpenAI-compatible endpoint root (no `/v1`). Model endpoints deployed through MLIS/PCAI are reached via their public serving URL with `--remote`, or via the in-cluster service DNS (`*.serving.<cluster>…`, or `.svc.cluster.local` from inside the cluster) without it.
- Credentials: `--api_key` > `--api_key_file` (first line) > `$PCAI_API_KEY`. All are optional — key-less endpoints just get no `Authorization` header. Prefer file/env so the token stays out of shell history and `ps`.
- TLS: remote endpoints verify against `$REMOTE_CA_BUNDLE` when set; unset means verification disabled (self-signed PCAI ingress) with a one-time `[security]` warning.

### Examples

**1. The standard capacity suite** — the sweep shape that produced most of `results/` (concurrency 1/4/32/64, 5-turn conversations, all tasks, cold vs 32k prefill, each task isolated):

```bash
for m in deepseek_v4_flash_280B qwen38_27B gemma4_31B; do
  python src/model_benchmarker/benchmark_chat.py \
    --model_class_name $m --remote \
    --number_users 1,4,32,64 --requests_per_user 5 \
    --tasks coding,creative,mixed --context_length 0,32768 \
    --multiturn --separate_tasks \
    --output results/<model-dir>/<SETUP>.md
done
```

*Question it answers:* what TTFT/tokens-s can each concurrency level sustain, with and without 32k of context already in memory?

**2. Agentic shared prefix** — every user carries the same 32k prefix (fleet of agents with one system prompt); the server prefills it once and everyone reuses it:

```bash
python benchmark_chat.py --model_class_name deepseek_v4_flash_280B --remote \
  --number_users 1,4,32,128 --requests_per_user 5 \
  --tasks coding,creative,mixed --context_length 0,32768 \
  --no-nonce \
  --output results/deepseek_v4_flash_0731/agentic_shared_prefix.md
```

Compare with the same command **without** `--no-nonce` to price the prefix cache; add `--prewarm` to measure the warmed-ahead-of-users best case.

**3. One-off smoke test** against an arbitrary endpoint (no sweep):

```bash
python benchmark_chat.py --url https://<endpoint-host> \
  --api_key_file ~/.config/pcai/pcai.key \
  --number_users 4 --requests_per_user 3 --tasks coding,creative,mixed
```

**4. Context/prefill scaling study** — TTFT and tokens/s as input grows 0 → 8k → 32k → 64k:

```bash
python benchmark_chat.py --model_class_name qwen38_27B --remote \
  --number_users 16 --tasks coding \
  --context_length 0,8192,32768,65536
```

**5. Reasoning-budget sensitivity** — how a thinking cap moves latency and throughput:

```bash
for lvl in off low high; do
  python benchmark_chat.py --model_class_name qwen38_27B --remote \
    --number_users 8 --requests_per_user 5 --tasks coding \
    --thinking_level $lvl \
    --output results/qwen_38_27b/qwen_think_$lvl.md
done
```

**6. Custom workload** — a bespoke prompt with tight sampling (e.g. summarization):

```bash
python benchmark_chat.py --model_class_name deepseek_v4_flash_280B --remote \
  --tasks custom --prompt "Summarize the attached report in 5 bullets." \
  --max_tokens 256 --temperature 0.2 --top_p 0.9 \
  --number_users 16 --requests_per_user 10
```

**7. Debug a weird streaming format** (TTFT shows `0` or `FAILED`):

```bash
python benchmark_chat.py --url https://<endpoint-host> --api_key <key> \
  --number_users 1 --tasks coding --debug_stream
```

More scenarios (pool sizing above 128 users, key-less endpoints, in-cluster runs) and every flag/environment knob are documented in [`documentation/cli-reference.md`](documentation/cli-reference.md).

## Results — artifacts and how to read them

Runs land in `results/<model-dir>/<SETUP>.md` — one file per (model × serving setup), filename encoding the setup (`H200_sglang_dflash2_hicachex3_replicasx3.md`). Files placed directly in `results/` are ignored by the report. The committed tree:

| Artifact | What it is |
|---|---|
| `results/qwen_38_27b/H200_sglang_dflash2_hicachex3_replicasx3.md` | Full capacity suite: 1× H200, SGLang + DFlash2, HiCache ×3, 3 replicas — 1/4/32/64 users, ctx 0/32768, multiturn + separate tasks, `Status: complete` |
| `results/qwen_38_27b/RTXPRO6000_vllm_fp8.md` vs `…_vllm_nvfp4.md` | Same 1× RTX PRO 6000 box under vLLM, FP8 vs NVFP4 weights — the quantization A/B pair |
| `results/deepseek_v4_flash_0731/H200Sx4_hicachex2.md`, `…/RTXPRO6000x2_hicachex16.md` | DeepSeek-V4-Flash-0731 on 4× H200 (HiCache ×2) and the memory-tight 2× RTX PRO 6000 (HiCache ×16) |
| `results/glm-5.3-flash/H200x4_NVLink2_hicachex6.md` | GLM-5.3-Flash, 4× H200 dual NVLink, HiCache ×6 |
| `results/glm-5.2/H200x8_8waynvlink_hicache_1TB.md` | GLM-5.2-753B, 8× H200 — `Status: in progress — 25/30 levels`: an interrupted run whose completed levels are still valid |
| `results/RAG/rag_benchmark_scale_large_n_100.md` | Multimodal-RAG scale-run transcripts — historically produced by the retired `MultimodalRAG/tests/benchmark.py`; going forward, produce these with the universal tool's `--md` (same `results/<dir>/` convention, parsed by the same report path) |
| `results/benchmark_report.html` | Generated snapshot of all of the above — **re-run `results_to_html.py` after adding files**; it is not live |
| `results/README.md` | The exact commands these results were produced with and reading guidance |

Each file contains the run configuration as bullets, a mode explanation, a `Status:` line (`complete`, `in progress — k/n levels`, `INTERRUPTED`, `CRASHED`), then one table row per (ctx, users, task):

```
| ctx | users | task | failed | TTFT turn1 P50 (ms) | … | TTFT-post P50 (ms) | … | tokens/s P50 | … |
```

- **`ctx` / `users` / `task`** — prefill padding, concurrency level, workload.
- **`failed`** — errored requests in that row (usually client 600 s read timeouts under saturation); excluded from stats. Non-zero on a level you care about means the server is past its sustainable concurrency.
- **`TTFT turn1` vs `TTFT-post`** — multiturn runs only. Turn 1 pays the full prefill; turns 2+ reuse the cached prefix. At padded contexts (ctx=32768) `TTFT-post ≪ turn1` is the cache earning its keep; at ctx=0 `TTFT-post` can *exceed* turn1, because the replayed history (long assistant answers) is bigger than turn 1's prompt — both directions are informative.
- **`tokens/s`** — inverted percentiles (P100 = slowest stream). A small P50→P100 spread means the pod sustains the load.

Reading one real row (`qwen_38_27b/H200_sglang_dflash2_hicachex3_replicasx3.md`, ctx=32768, 32 users, coding): turn-1 TTFT P50 ≈ 18.0 s (cold 32k prefill) vs TTFT-post P50 ≈ 4.6 s (cached prefix); tokens/s P50 85.2 falling to P100 49.8. Caveats when comparing runs: DeepSeek is a shared premier model — teammates' live traffic cuts into its numbers; keep `MODEL_POOL_MAX_CONNECTIONS` ≥ your max users or high-percentile TTFT becomes a client-side artifact; the default nonce makes turn 1 a true cold prefill by design. The full artifact format, filename grammar and report options live in [`documentation/results-and-report.md`](documentation/results-and-report.md).

Regenerate the HTML report after any new `results/<model>/*.md`:

```bash
python3 src/model_benchmarker/results_to_html.py            # <repo-root>/results, self-contained HTML
python3 src/model_benchmarker/results_to_html.py --open     # + open in browser
```

## Related tools

- **ModelDownloader** (`../ModelDownloader`) — `results_to_html.py` auto-discovers its `seed_catalog.json` to link each benchmark run to the matching deployment catalog entry (image, serving arguments, resources) in the report's "PCAI deployment config" expander.
- **MultimodalRAG** (`../MultimodalRAG`) — its retired `tests/benchmark.py` (MCP mode) produced the `results/RAG/*.md` transcripts; that script has been removed in favor of this repo's universal benchmarker (Part 1), which covers the same MCP/REST runs — with sweeps, GPU telemetry and the extended reports — for any target, not just MM RAG.
- **ModelDeploymentApproaches** (`../ModelDeploymentApproaches`) — ad-hoc serving-config experiments (HiCache tweaks, serve scripts) with no stable interface; not referenced by this repo's code, but the benchmarks here are how candidate setups from it get evaluated.

(The former sibling repo `EndpointBenchmarker` is now merged into this one as the universal tool — `src/model_benchmarker/endpoint_benchmarker/`.)

## Maintainer tooling

- `./automation.sh <version>` — release stub: runs the shared version bumper and `prune_charts.py`. It exists so the fleet release interface stays uniform; there is nothing to build or ship yet. (A `pyproject.toml` now exists — `pip install -e .` provides the `endpoint-benchmarker` and `benchmark-chat` console scripts — but there are still no Helm charts/Dockerfile.)
- `python3 hardlinker.py --config hardlink_config.json [--run] [--prune] [--no-charts]` — syncs this repo into `pcai-solutions/tools/model-benchmarker/` as a **hardlink mirror** (same inodes; an edit through either path updates both). Bare runs are a dry-run preview (the shipped config sets `"dry_run": true`); `--run` applies, `--prune` also removes destination orphans. The ignore list excludes `pcai_models.py` (embedded cluster URLs/keys), caches and `*.zip`. The vendored `endpoint_benchmarker` package carries no secrets and ships with the mirror. Stale `*-<version>.tgz`/`.tar.gz` chart archives in the repo root are auto-pruned (newest kept) on every run; `--no-charts` disables that.

## Documentation

- [`documentation/runbook.md`](documentation/runbook.md) — **start here**: command-first playbooks per use case (chat capacity suites, any-MCP sweeps + GPU scaling, worked RAG/SQL examples, artifact flow, troubleshooting table).
- [`documentation/cli-reference.md`](documentation/cli-reference.md) — every `benchmark_chat.py` flag, environment variables, timeouts, pool tuning and failure behavior.
- [`documentation/results-and-report.md`](documentation/results-and-report.md) — result-file anatomy, the `<SETUP>` filename grammar, `results_to_html.py` options and comparison caveats.
- [`results/README.md`](results/README.md) — how the committed results were produced and what to look for in them.
- Universal tool: the CLI reference and artifact schema live in Part 1 above (`python -m model_benchmarker.endpoint_benchmarker --help` is the executable source of truth).

---

# Roadmap — the staged tool fusion

The two tools are converging into one engine:

1. **Done — one repo, one package.** The universal benchmarker (Part 1) is the
   main tool; chat benchmarking (Part 2) stays as the specialist for
   TTFT/tokens-s. Retired the last single-purpose script
   (`MultimodalRAG/tests/benchmark.py`).
2. **Chat streaming as a target mode.** Port TTFT/tokens-per-second
   measurement into the universal engine as an OpenAI-compatible chat target
   (`--mode chat`), retiring the duplicated driver. Then one sweep produces
   client stats *and* streaming metrics with the same Prometheus telemetry
   join.
3. **Agent benchmarking with observability.** A workload where one "task" is
   an agentic loop — an LLM calling attached tools (e.g. MCP `search_dataset`)
   over multiple turns — measured as a **span tree**
   (`task → [llm_turn, tool_call, …]`):
   - *Query bands:* banded groups of queries in `--queries-file` (e.g.
     `[band:code]` sections), assigned to user groups, so each band gets its
     own curve.
   - *Time attribution:* per-phase aggregation (LLM generation vs tool
     execution vs queueing) — median/p95 per phase type, per level.
   - *Reporting:* per-phase tables in the Markdown report, a waterfall for
     sampled tasks in the HTML report; the Prometheus GPU join keeps working
     unchanged (a task is just the new "request").
   - Caveat carried over from the tool's own design: client spans see what the
     client sees; per-stage GPU attribution still comes from the telemetry
     join.

## Development

```bash
python3 -m pytest tests/           # full suite (no network needed)
```

The suite runs the universal tool's real drivers end-to-end against a local
aiohttp REST server and a real MCP server (`MCPServer.streamable_http_app()`
under uvicorn), plus a fake Prometheus for the telemetry join, and checks the
markdown output round-trips through `results_to_html.py`'s parsers.
