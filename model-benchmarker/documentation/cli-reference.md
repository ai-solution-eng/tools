# CLI reference — `benchmark_chat.py`

Complete flag, environment and failure-behavior reference for
[`src/model_benchmarker/benchmark_chat.py`](../src/model_benchmarker/benchmark_chat.py).
For the tool's purpose and philosophy see
[FEATURES.md](FEATURES.md); for result files and the HTML report see
[`results-and-report.md`](results-and-report.md).

Run by path from anywhere in the repo (the script bootstraps `sys.path`):

```bash
python src/model_benchmarker/benchmark_chat.py --help
```

Flag style is mixed: most flags use underscores exactly as written
(`--model_class_name`, `--number_users`); `--no-nonce` is the only
dash-style flag.

## How it works

```
benchmark_chat.py
  ├─ resolve endpoint ── --model_class_name (registered entry in utils/pcai_models.py)
  │                      or --url (any OpenAI-compatible root, no /v1; served model id
  │                        auto-discovered via GET /v1/models when not set)
  │                      --remote switches the in-cluster service URL to the public one
  ├─ for each --context_length level   (optional --prewarm first)
  │    ├─ for each --number_users level
  │    │    ├─ default (closed-loop):  N asyncio users × requests_per_user requests
  │    │    ├─ --arrival_mode open:    requests on a Poisson/gamma schedule at
  │    │    │                          --request_rate; levels are --level_duration windows
  │    │    ├─ --multiturn:            each user runs N turns of ONE growing conversation
  │    │    └─ --separate_tasks:       one clean pass per task (no cross-task mixing)
  │    └─ per (ctx, users, task): P50/P95/P99/P100 for TTFT, tokens/s, ITL, TPOT
  │                                (+ goodput fraction per --goodput SLOs)
  └─ print table to stdout + rewrite --output Markdown after every completed level
```

Each request is one **streamed** chat completion (`stream_options.include_usage`). The metrics:

| Metric | Definition |
|---|---|
| **TTFT (ms)** | Time from request send to the first chunk carrying any text delta — `content`, `reasoning_content`, or any other non-empty string delta field, so exotic stream shapes never read TTFT = 0. |
| **tokens/s** | `usage.completion_tokens / (stream_end − first_token)` — generation throughput **excluding TTFT**. Falls back to counting content+reasoning chunks when the stream carries no `usage`, and to the full request time when the response arrives in one burst (short generations). |
| **ITL (ms)** | Inter-token latency: gaps between consecutive text-bearing deltas, collected into a fixed **log-spaced histogram** per request (96 buckets over 0.1 ms–60 s — memory stays O(buckets) even at 64 users × 24 576 tokens) plus an exact running **max stall**, merged per level. Recorded whenever *any* text-bearing delta arrives, independent of the content/reasoning branch order (a dual-field delta counts once). |
| **TPOT (ms)** | Time per output token per request: `(stream_end − first_token) / (tokens − 1)`, same usage-token precedence as tokens/s. Burst-guarded requests (single-burst responses) get *approximate* TPOT — flagged, and excluded from TPOT percentiles and goodput's TPOT predicate. ITL stays raw (never inherits the burst fallback). |
| **goodput** | With `--goodput "ttft<=2000,tpot<=50"`: the fraction of *successful* requests meeting **every** SLO (thresholds in ms). Failed requests are not goodput (counted in `failed`, vLLM convention); thresholds are stamped in the artifact header; the summary names the max goodput-satisfying load. |

Percentile semantics: for TTFT the percentiles are ascending (P100 = slowest request, bigger = worse); for tokens/s they are **inverted** (P100 = slowest stream), so higher is better everywhere on that block. ITL/TPOT ascend like TTFT.

**Pipe/redirect stdout only together with `--quiet`** — a backpressured stdout stall lands in every inter-token gap, distorting ITL/TPOT even more than TTFT (documented in the flag's help).

### Built-in workloads

`--tasks` selects from a fixed registry (`coding` = production-grade Python LRU-cache module, `creative` = a 1,500–2,000-word literary story, `mixed` = both in one response) plus `custom` (your `--prompt` with `--max_tokens`/`--temperature`/`--top_p`). Output caps: coding 8192, creative 16384, mixed 24576 tokens. The fixed prompts are what make runs comparable across machines and time.

### Cache regimes — why the nonce matters

By default every request gets a unique random nonce prepended, so no two requests share prefix tokens: prefix caches (RadixAttention, SGLang radix cache, HiCache tiers) never hit, and every measurement is a cold prefill — the deliberate worst case. Three flags explore the cache-friendly regimes:

- `--multiturn` — each user runs `--requests_per_user` turns of one growing conversation (full history replayed each turn, assistant `content` only — reasoning tokens are not resent, matching real chat clients). Turns 2+ reuse the shared prefix, giving the `turn1` vs `TTFT-post` split.
- `--no-nonce` — all requests (across all users) share one identical fixed prefix, so the server can reuse its prefix cache across users. Compare against the same run without it to price cache sharing.
- `--prewarm` — before each context level, fire one shared-prefix request per task so the prefix graph is resident *before* users arrive. Pairs with `--no-nonce`; alone it warms a prefix nothing will reuse.

### Open-loop arrivals — measuring queueing, not just saturation

Closed-loop (default) measures how the server behaves when N users each fire back-to-back requests. **Open-loop** (`--arrival_mode open --request_rate 5`) instead issues requests on a **schedule** — Poisson exponential inter-arrivals at `--burstiness inf`, gamma-shaped (mean 1/R, cv = 1/b) at any finite b — so **server-side queueing shows up in TTFT by design**: that is the point. Levels become time windows (`--level_duration`, default 30 s); the drain tail (requests issued inside the window but finishing after it) is part of the measurement.

- Open-loop runs are stamped everywhere: `MODE: arrival=open` run-header line, `arrival=open` in the report data, and the `_ol` filename token (closed-loop files stay unmarked). The HTML compare view **refuses** mixing closed- and open-loop setups side by side.
- Multiturn is closed-loop only (a turn needs the previous reply) — the contradiction guard exits before any traffic.
- In-flight cap ≈ `min(request_rate × read-timeout, MODEL_POOL_MAX_CONNECTIONS)` is surfaced; exceed the pool and arrivals queue client-side, polluting the arrival process (the run warns).
- Prewarm is still valid; "users all start simultaneously" no longer holds.

## What problem it solves

- **Compare serving configurations before committing to one.** The same model on different hardware counts, engines (vLLM vs SGLang), speculative-decoding/MTP configs, weight formats (FP8/NVFP4), HiCache tiering or replica counts, measured on identical workloads — exactly what the committed artifacts in `results/` encode per file.
- **Measure sustained concurrency.** How TTFT and tokens/s degrade as concurrent users scale (e.g. 1 → 4 → 32 → 64) and as the input prefill grows (0 → 8k → 32k → 64k tokens).
- **Measure inter-token smoothness, not just averages.** ITL histograms (P99/max-stall) and per-request TPOT expose stalls (prefill pauses, KV eviction, decode hiccups) that TTFT/tokens-s averages hide; goodput turns them into one SLO number.

Command-first worked scenarios (capacity suite, shared-prefix agentic, context
padding, thinking budgets, …): [runbook.md](runbook.md) §1.

## Model / endpoint selection

| Flag | Description |
|---|---|
| `--model_class_name NAME` | Use a registered model from `src/model_benchmarker/utils/pcai_models.py` (chat entries: `deepseek_v4_flash_280B`, `qwen38_27B`, `qwen36_27B`, `gemma4_31B`, `glm_52_753B`, `glm_53_flash_331B`). Imported lazily, only when this flag is used. **Caveat:** entries with `currently_deployed=False` (currently `qwen36_27B`, `glm_52_753B`) fail every request — pick a deployed entry or use `--url`. The module embeds cluster-internal URLs and bearer keys and is excluded from the hardlinked deployment, where this flag is unavailable. |
| `--url URL` | OpenAI-compatible endpoint root, **no `/v1`** (the client appends it). Required when `--model_class_name` is omitted. If the served model id is not set, it is auto-discovered via `GET {base}/v1/models` (first listed id; cache bounded by `DISCOVERED_MODEL_CACHE_MAX`). |
| `--api_key KEY` | Bearer token. Optional — omit for endpoints that don't require auth and no `Authorization` header is sent. Prefer `--api_key_file` / `$PCAI_API_KEY` so the secret stays out of shell history and `ps`. |
| `--api_key_file FILE` | Read the bearer token from a file (first line, stripped). Priority: `--api_key` > `--api_key_file` > `$PCAI_API_KEY`. |
| `--remote` | Use the model's **public serving URL**. Without it, a registered model targets its in-cluster URL (`*.svc.cluster.local`-style service DNS). Omit `--remote` when running inside the same cluster. `--url` endpoints are always used as given. |

Endpoints here are model endpoints deployed through MLIS on PCAI — this tool
only sends OpenAI-compatible chat-completion requests to them; it performs no
deployment itself.

## Load shape

| Flag | Default | Description |
|---|---|---|
| `--number_users N,M,…` | `1` | Concurrency levels to sweep. All conversations start simultaneously — no ramp. |
| `--requests_per_user N` | `1` | Sequential requests per user, cycling through `--tasks` (each round of `len(tasks)` requests is a freshly shuffled permutation per user, so the mix stays even). |
| `--tasks t1,t2,…` | `coding,creative` | Task types: `coding` (max_tokens 8192), `creative` (16384), `mixed` (24576 — one request combining coding + creative), `custom`. |
| `--context_length 0,…` | `0` | Prefill-length sweep; each level pads every prompt to ≈ that many input tokens (≈4 chars/token filler text). `0` disables padding. With `--multiturn` only **turn 1** is padded — later turns reuse the prefix. |
| `--multiturn` | off | Each user runs `--requests_per_user` **turns of one growing conversation**: full prior user+assistant history replayed each turn, only the newest user prompt gets a fresh nonce. Assistant turns replay `content` only — reasoning tokens are not resent (matches real chat clients; runs made before this behavior are not directly comparable). Gives the `turn1` vs `TTFT-post` split. |
| `--separate_tasks` | off | Run each task in its own (ctx, users) pass — every task gets a clean turn-1 vs turns-post comparison without cross-task interference. Status counts one level per (ctx, users, task). |
| `--arrival_mode closed\|open` | `closed` | **Open-loop (MB-C):** `open` issues requests on a schedule (`--request_rate` arrivals/s) instead of the closed-loop "next request when a user slot frees" — server-side queueing then shows up in TTFT **by design**. Levels become time windows (`--level_duration`); `--requests_per_user` does not apply; every row/artifact is stamped `arrival=open` (filename token `_ol`). Multiturn is closed-loop only (guard exits). Closed- and open-loop numbers are never comparable — the HTML compare view refuses mixing them. |
| `--request_rate R` | required with `open` | Open-loop arrival rate (requests/second). Level size ≈ `R × --level_duration`. |
| `--burstiness b` | `inf` | Inter-arrival variability (vLLM convention): `inf` = exponential/Poisson (cv=1); finite b draws Gamma(b², 1/(b²·R)) — mean 1/R, cv = 1/b — so b < 1 is more regular, b > 1 burstier. |
| `--level_duration S` | `30` | Open-loop arrival-window seconds. Arrivals are scheduled inside the window; the level ends when the last issued request completes (drain tail included). |
| `--seed N` | random | Seeds the open-loop arrival schedule and per-request task selection for reproducible runs. |
| `--goodput "m<=X,…"` | off | **Goodput-with-SLO (MB-D):** a request has goodput when every threshold (ms) holds, e.g. `"ttft<=2000,tpot<=50"`. Metrics: `ttft`, `tpot`, `itl_p99`/`itl_p50`, `max_stall`. Failed requests are not goodput (counted in `failed`, vLLM convention); burst-guarded requests (approximate TPOT) are excluded from `tpot` predicates — each row prints its burst share. Adds a goodput fraction per row plus a max goodput-satisfying-load summary line; thresholds are stamped into the artifact header (mandatory). |
| `--prompt TEXT` | built-in essay | Prompt for the `custom` task. |
| `--max_tokens N` | per task | Override output cap for every task. |
| `--temperature F` | per task | Override sampling temperature (built-ins use 1.0). |
| `--top_p F` | per task | Override top_p (built-ins: coding 0.95, else 1.0). |

## Cache behavior

| Flag | Description |
|---|---|
| `--no-nonce` | Replace the per-request random nonce with a fixed shared marker (`my-shared-benchmark-prefix`), so every request across all users shares one identical prefix and the server's prefix cache can be reused across users. Isolates cache-sharing benefit from cold-prefill cost. |
| `--prewarm` | Before each (ctx) level, fire one shared-prefix request per task so the prefix graph is populated *ahead of* the users. Pairs with `--no-nonce`; alone it warms a prefix no request will reuse. |

## Reasoning / sampling overrides

| Flag | Description |
|---|---|
| `--enable_thinking` / `--disable_thinking` | Force `chat_template_kwargs.enable_thinking` True/False (Qwen3-style). Default leaves the server default. |
| `--thinking_budget N` | Cap reasoning tokens via `chat_template_kwargs.thinking_token_budget`. Must be smaller than the task's max_tokens, or the model may spend the whole budget reasoning and TTFT reads 0. |
| `--thinking_level LEVEL` | Preset budget: `off`=0, `low`=1024, `medium`=4096, `high`=16384, `x-high`=32768. Overrides `--thinking_budget`. |

## Diagnostics / output

| Flag | Description |
|---|---|
| `--debug_stream` | Print the first delta's field names/values and per-request content vs reasoning chunk counts — diagnose TTFT=0 when a server streams text under a non-`content` delta field. |
| `--quiet` | Suppress per-request progress lines (keeps prewarm notices, level previews, the final table). Recommended when stdout is piped/redirected: per-request `print()` runs on the event loop, and a backpressured stdout can stall all streams and distort TTFT/tokens-s — and ITL/TPOT even more (every inter-token gap absorbs the stall). Pair `--quiet` with any ITL/TPOT-sensitive run. |
| `--output FILE` | Write the summary table to FILE (in addition to stdout), rewritten after **every completed sweep level** and stamped with a `Status:` line (`complete`, `in progress — k/n levels`, `INTERRUPTED…`, `CRASHED…`). Writes are atomic (`.partial` temp file + `os.replace`), so a killed run never leaves a truncated file. The report generator only picks the file up once at least one level succeeded. Result files must live under a per-model subdirectory (`results/<model-dir>/<SETUP>.md`). |
| `--resume-from FILE` | Continue a timed-out/interrupted sweep: FILE is a previous artifact whose completed (ctx, users, task) tests are **skipped** (the atomize.py decomposition decides what counts as done), and its measurements are copied into this run's `--output`, so the final file is the **union**. Guardrails refuse a model / target / run-shape mismatch with FILE's header (model bullet, `URL:` / `Resume fingerprint:` stamps) or a `Status: complete` FILE — `--resume-force` overrides everything and re-runs all levels. |

## Environment variables

| Var | Default | Purpose |
|---|---|---|
| `MODEL_POOL_MAX_CONNECTIONS` | `128` | httpx connection-pool ceiling. With fewer connections than the concurrency level, requests serialize client-side and high-percentile TTFT blows up — the benchmark becomes pool-bound, not server-bound. Raise to match your max `--number_users`; the script prints a warning when a level exceeds it. |
| `MODEL_POOL_MAX_KEEPALIVE_CONNECTIONS` | `32` | Idle keep-alive connections kept by the pool. Below your concurrency level, wave boundaries pay fresh TCP+TLS handshakes. |
| `PCAI_API_KEY` | unset | Fallback bearer token (lowest priority). |
| `REMOTE_CA_BUNDLE` | unset | Path to a CA bundle (`.pem`/`.crt`). When set (and the file exists), TLS verification is **on** for remote endpoints against that bundle; when unset, remote endpoints run with verification **disabled** (self-signed PCAI ingress) and a one-time `[security]` warning is printed to stderr. |
| `DISCOVERED_MODEL_CACHE_MAX` | `512` | Bound on the per-URL `/v1/models` model-name discovery cache. |
| `SYNC_POOL_SIZE` | `12` | Threads in the sync→async bridge pool of the shared platform modules. |

## Timeouts and failure behavior

- Request timeout is `connect=30s, read=600s` (`_MODEL_REQUEST_TIMEOUT` in `utils/pcai_model_classes.py`), applied to both the httpx clients and the OpenAI SDK clients (the SDK otherwise ignores an injected httpx client's timeout and falls back to its own `connect=5s`, turning slow high-concurrency handshakes into spurious failures).
- Open-loop in-flight cap: the client never holds more than ≈ `min(request_rate × read-timeout, MODEL_POOL_MAX_CONNECTIONS)` streams; the cap is printed per run. When `request_rate × timeout` exceeds the pool, arrivals beyond it queue client-side and pollute the arrival process — the run warns; raise the pool or lower the rate.
- Very long generations at high concurrency can still log `FAILED: Request timed out.` — that is the server being saturated, not a client bug.
- If **all** requests in a level fail, the level is skipped (rows omitted), the first error is printed, and the sweep continues to the next level.
- Failed requests are excluded from percentile stats but counted in the `failed` column (and never counted as goodput).
- Invalid values (`--number_users abc`, negative `--context_length`, `--requests_per_user < 1`, unknown task or model class names, `--arrival_mode open` without `--request_rate`, `--multiturn` with open-loop, malformed `--goodput` terms) exit immediately with a message before any traffic is sent.
