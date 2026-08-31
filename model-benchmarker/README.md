# benchmark_chat — PCAI Chat Model Benchmarker

Measures **time-to-first-token (TTFT)** and **tokens/s** for OpenAI-compatible
chat endpoints (PCAI/vLLM/SGLang serving clusters) under realistic concurrency.

Run the script from anywhere in the repo or its hardlinked deployment:

```bash
python src/model_benchmarker/benchmark_chat.py ...
```

## Setup

Python ≥ 3.12. No package install is needed — the scripts bootstrap `sys.path`
themselves and are run by path:

```bash
pip install -r requirements.txt
```

`httpx2` is a hard dependency of the shared client module (previously missing
from this file). `tokenizers` is optional — without it, text chunking falls
back to character-based.

> The examples below abbreviate the script path to `python benchmark_chat.py`;
> substitute `python src/model_benchmarker/benchmark_chat.py` when running from
> anywhere else in the repo.

## What it measures

| Metric | Definition |
|---|---|
| **TTFT (ms)** | Time from request send until the first token of any kind (`content` or `reasoning_content`, or any other non-empty string delta field — exotic stream shapes never read TTFT = 0) arrives. |
| **tokens/s** | `usage.completion_tokens / (stream_end − first_token)` — generation throughput **excluding TTFT**. Falls back to counting content+reasoning chunks when the stream carries no `usage`, and to the full request time when the whole response arrives in one burst. |

The output table shows percentile columns **P50 / P95 / P99 / P100**. For TTFT the
values are ascending (bigger = worse). For tokens/s the percentiles are *inverted*:
P100 is the *slowest* stream, so higher is better everywhere.

```
    ctx  users   task failed | TTFT (ms) P50 P95 P99 P100 | tokens/s P50 ... P100
```

- **`ctx`** — target input-token prefill length (0 = unpadded).
- **`users`** — concurrent simulated users for that row.
- **`failed`** — number of requests in that row that errored (excluded from stats).
- **TTFT turn1** vs **TTFT-post** — shown only with `--multiturn`. `turn1` is the
  cold request (full prefill); `turns 2+` reuse the conversation prefix, so the
  gap shows the server prefix/KV cache benefit.

### Why the nonce matters

By default every request gets a **unique random nonce** prepended, so no two
requests share any prefix tokens. This defeats server-side prefix/KV caches
(RadixAttention, SGLang radix cache, HiCache) and forces a cold prefill every
time — measuring *worst-case* latency, not cache hits. That is the benchmark's
intent, but it is why numbers can look poor at high concurrency and long
context (32 users × 32k tokens = ~1M tokens of cold prefill).

Pass `--no-nonce` to measure the cache-friendly regime instead.

## Flags

Flag style is mixed: most flags use underscores exactly as written
(`--model_class_name`, `--number_users`); `--no-nonce` is the only dash-style
flag in this script.

### Model selection

| Flag | Description |
|---|---|
| `--model_class_name NAME` | Use a registered model from `utils/pcai_models.py` (e.g. `deepseek_v4_flash_280B`, `qwen38_27B`, `qwen36_27B`, `gemma4_31B`, `glm_52_753B`). Imported lazily; not available in the hardlinked deployment. **Caveat:** entries with `currently_deployed=False` in `pcai_models.py` (currently `qwen36_27B`, `glm_52_753B`) fail every request — pick a deployed entry or use `--url`. |
| `--url URL` | OpenAI-compatible endpoint root (no `/v1`). Required when `--model_class_name` is omitted. |
| `--api_key KEY` | Bearer token for the endpoint. **Optional** — omit it for endpoints that don't require auth; no `Authorization` header is then sent. Prefer `--api_key_file` / `PCAI_API_KEY` so the secret stays out of shell history and `ps`. |
| `--api_key_file FILE` | Read the bearer token from a file (first line, stripped). Priority: `--api_key` > `--api_key_file` > `$PCAI_API_KEY`. |
| `--remote` | Use the public serving URL. Without it, the model targets its **in-cluster** URL (`.svc.cluster.local`). Omit `--remote` when running inside the same cluster. |

### Load shape

| Flag | Description |
|---|---|
| `--number_users N,M,...` | Concurrency levels to sweep. Default `1`. |
| `--requests_per_user N` | Sequential requests per user, cycling through `--tasks`. Default `1`. |
| `--tasks t1,t2,...` | Task types: `coding` (max_tokens 8192), `creative` (16384), `mixed` (24576 — one request that must do coding+creative), `custom`. Default `coding,creative`. |
| `--context_length 0,32768,...` | Prefill-length sweep; each level pads every prompt to ~that many input tokens (≈4 chars/token). With `--multiturn` only **turn 1** is padded (later turns reuse the prefix). `0` disables padding. Default `0`. |
| `--multiturn` | Each user runs `--requests_per_user` **turns of one growing conversation** (full history replayed each turn) instead of independent requests, so the shared prefix is reused and the server's prefix/KV cache engages. Gives the `turn1` vs `turns-post` split. Assistant turns replay **`content` only** — reasoning tokens are not resent (matches real chat clients; runs made before this change are not directly comparable). |
| `--separate_tasks` | Run each task in its own (ctx, users) pass, so every task gets a clean turn-1 vs turns-post comparison without cross-task interference. |
| `--custom` args | `--prompt` (the prompt text), `--max_tokens`, `--temperature`, `--top_p` for the `custom` task. Defaults when omitted: max_tokens 512, temperature 1.0, top_p 1.0, and a built-in essay prompt if `--prompt` is not given. |

### Cache behavior

| Flag | Description |
|---|---|
| `--no-nonce` | Replace the per-request random nonce with a fixed shared marker, so **every request (across all users) shares one identical prefix** and the server can reuse its prefix cache across users. Isolates cache-sharing benefit from cold-prefill cost. |
| `--prewarm` | Before each (ctx) sweep, fire one shared-prefix request per task so the prefix graph is populated *ahead of* the users ("prefill happens before the users"). Pairs with `--no-nonce`; alone it warms a prefix no request will reuse. |

### Reasoning / sampling overrides

| Flag | Description |
|---|---|
| `--max_tokens N` | Override output cap for every task (default per-task). |
| `--temperature F` | Override sampling temperature (default per-task). |
| `--top_p F` | Override top_p (default: coding 0.95, else 1.0). |
| `--enable_thinking` / `--disable_thinking` | Force `chat_template_kwargs.enable_thinking` True/False (Qwen3-style; default leaves server default). |
| `--thinking_budget N` | Cap reasoning tokens via `thinking_token_budget`. Must be < task max_tokens or the model may reason the whole budget and TTFT reads 0. |
| `--thinking_level LEVEL` | Preset budget: `off=0, low=1024, medium=4096, high=16384, x-high=32768`. Overrides `--thinking_budget`. |

### Diagnostics / output

| Flag | Description |
|---|---|
| `--debug_stream` | Print the first delta's field names and per-request content/reasoning chunk counts — diagnose TTFT=0 when a server streams under non-standard delta fields. |
| `--quiet` | Suppress the per-request progress lines (keeps prewarm notices, level previews and the final table). Recommended when stdout is piped/redirected — per-request `print()` runs on the event loop, and a backpressured stdout can stall all streams and distort TTFT/tokens-s. |
| `--output FILE` | Write the final summary table to FILE (in addition to stdout). |

### Environment

| Var | Default | Purpose |
|---|---|---|
| `MODEL_POOL_MAX_CONNECTIONS` | `128` | httpx connection-pool ceiling. With fewer than the concurrency level, requests serialize client-side and the *high percentile* TTFT blows up (your benchmark is pool-bound, not server-bound). Raise it to match your max `--number_users`; the script prints a warning when a sweep level exceeds it. |
| `MODEL_POOL_MAX_KEEPALIVE_CONNECTIONS` | `32` | Idle keep-alive connections kept by the pool. Below your concurrency level, wave boundaries pay fresh TCP+TLS handshakes. |
| `PCAI_API_KEY` | *(unset)* | Fallback bearer token for `--url` mode. Resolution order: `--api_key` > `--api_key_file` > this env var. |
| `REMOTE_CA_BUNDLE` | *(unset)* | Path to a CA bundle (`.pem`/`.crt`). When set (and the file exists), **TLS certificate verification is ON** for remote endpoints — against that bundle. When unset, remote endpoints run with verification **disabled** (self-signed PCAI ingress) and a one-time `[security]` warning is printed to stderr. |
| `DISCOVERED_MODEL_CACHE_MAX` | `512` | Bound on the per-URL `/v1/models` model-name discovery cache. |
| `SYNC_POOL_SIZE` | `12` | Threads in the sync→async bridge pool (shared platform modules). |

The client request timeout is `connect=30s, read=600s` (`_MODEL_REQUEST_TIMEOUT`
in `utils/pcai_model_classes.py`) and is applied to both the httpx clients and
the OpenAI SDK clients — the SDK ignores the timeout configured on an injected
httpx client and would otherwise fall back to its own `connect=5s` default,
turning slow high-concurrency handshakes into spurious failures. Very long
generations at high concurrency can still log `FAILED: Request timed out.`
That is the server being saturated, not a client bug.

---

## Examples

### 1. The standard capacity suite (the sweep shape that populated `results/`)

One run per model: 1/4/32/64 concurrent users, 5-turn conversations, all tasks,
both cold (ctx=0) and 32k prefilled, each task isolated. The committed results
use this sweep with per-setup filenames — `<SETUP>` encodes the serving config
(see the naming convention in the HTML report section below):

```bash
for m in deepseek_v4_flash_280B qwen38_27B gemma4_31B; do
  python src/model_benchmarker/benchmark_chat.py \
    --model_class_name $m --remote \
    --number_users 1,4,32,64 \
    --requests_per_user 5 \
    --tasks coding,creative,mixed \
    --context_length 0,32768 \
    --multiturn --separate_tasks \
    --output results/<model-dir>/<SETUP>.txt
done
```

Files must live under a per-model subdirectory (`results/<model-dir>/…`) —
`.txt` files placed directly in `results/` are not picked up by the report.

*Question it answers:* "What TTFT/tokens-s can each concurrency level sustain,
with and without 32k of memory already present?" — i.e. many back-to-back
opencode-style conversations.

### 2. Agentic shared-prefix (the new scenario)

All users share one fixed prompt prefix (e.g. a fleet of agents all carrying the
same system prompt / context head). The server can prefill the shared 32k once
and every instance reuses it — no per-user cold prefill:

```bash
python benchmark_chat.py \
  --model_class_name deepseek_v4_flash_280B --remote \
  --number_users 1,4,32,128 \
  --requests_per_user 5 \
  --tasks coding,creative,mixed \
  --context_length 0,32768 \
  --no-nonce \
  --output results/deepseek_v4_flash_0731/agentic_shared_prefix.txt
```

*Question it asks:* "When N instances share the same prefix, does the server
amortize the prefill?" (Compare TTFT here vs the same command **without**
`--no-nonce`.)

### 3. Agentic + prewarm (prefix populated *before* users arrive)

Warm the prefix graph first, then measure: the serving stack "prefill happens
ahead of the users" — the best-case for cache warmth:

```bash
python benchmark_chat.py --model_class_name deepseek_v4_flash_280B --remote \
  --number_users 32 \
  --requests_per_user 5 \
  --tasks coding,creative,mixed \
  --context_length 32768 \
  --no-nonce --prewarm \
  --output results/deepseek_v4_flash_0731/agentic_prewarm_32k.txt
```

Compare Example 2 vs 3 to quantify how much of the gain was "cache already
resident" vs "requests joining the graph."

### 4. One-off smoke test

Quick sanity check against a single endpoint (no sweep, mixed tasks):

```bash
python benchmark_chat.py --url https://<endpoint> \
  --api_key_file ~/.config/pcai/pcai.key \
  --number_users 4 --requests_per_user 3 --tasks coding,creative,mixed
```

### 5. Context/prefill scaling study

How TTFT and tokens/s scale as the input prefill grows 0 → 8k → 32k → 64k:

```bash
python benchmark_chat.py --model_class_name qwen38_27B --remote \
  --number_users 16 --tasks coding \
  --context_length 0,8192,32768,65536
```

### 6. Reasoning-budget sensitivity

How much a `thinking_level`/`thinking_budget` cap changes latency and
throughput:

```bash
for lvl in off low high; do
  python benchmark_chat.py --model_class_name qwen38_27B --remote \
    --number_users 8 --requests_per_user 5 \
    --tasks coding --thinking_level $lvl \
    --output results/qwen_38_27b/qwen_think_$lvl.txt
done
```

### 7. Custom prompt, custom caps

A bespoke workload (e.g. summarization with a tight token budget):

```bash
python benchmark_chat.py --model_class_name deepseek_v4_flash_280B --remote \
  --tasks custom \
  --prompt "Summarize the attached report in 5 bullets." \
  --max_tokens 256 --temperature 0.2 --top_p 0.9 \
  --number_users 16 --requests_per_user 10
```

### 8. Debug a weird streaming format (TTFT = 0)

When TTFT shows `0` or `FAILED`, inspect what the endpoint actually streams:

```bash
python benchmark_chat.py --url <endpoint> --api_key <key> \
  --number_users 1 --tasks coding --debug_stream
```

You'll see the first delta's field names and content vs reasoning chunk counts.

### 9. Stress the connection pool

With many concurrent users, make sure the client is not the bottleneck — the
pool default (128) should cover you, but be explicit if you go higher:

```bash
MODEL_POOL_MAX_CONNECTIONS=256 \
python benchmark_chat.py --model_class_name deepseek_v4_flash_280B --remote \
  --number_users 256 --requests_per_user 3 --tasks creative
```

If TTFT *high-percentiles* jump when you omit this, you're pool-bound
(client-side) — raise it.

### 10. Single turn, many users (pure prefill/decode capacity)

```bash
python benchmark_chat.py --model_class_name deepseek_v4_flash_280B --remote \
  --number_users 64 --requests_per_user 1 \
  --tasks coding --context_length 32768
```

(Any model with `currently_deployed=True` in `pcai_models.py` works here —
`qwen36_27B`/`glm_52_753B` are currently marked not-deployed and would fail
every request.)

### 11. Key-less endpoint (no API key)

Endpoints that don't require auth work with `--url` alone — drop `--api_key`
entirely and no `Authorization` header is sent (openai SDK credential
enforcement is relaxed automatically):

```bash
python benchmark_chat.py --url http://localhost:8080   --number_users 4 --requests_per_user 3 --tasks coding,creative,mixed
```

For a *public* key-less endpoint pass `--remote` too (see Example 12).

### 12. Server-endpoint via `--url` on a cluster node (no `--remote`)

From a pod inside the same cluster, hit the in-cluster service DNS directly
(no external hop):

```bash
python benchmark_chat.py --url https://<model>.project-user-<name>.serving.<cluster>.<domain> \
  --api_key_file ~/.config/pcai/pcai.key \
  --number_users 32 --requests_per_user 5 \
  --tasks coding,creative,mixed \
  --context_length 0,32768 \
  --multiturn --separate_tasks
```

---

## Reading a row

```
  ctx  users   task failed |    TTFT P50  P95  P99 P100  |   tokens/s P50  P95  P99  P100
 32768    32  coding     0 |  53636 213291 358814 359281 |    23.8  20.3  19.6  18.9
```

At 32 concurrent users with 32k of prefill: median TTFT ~54 s, tail (P99)
~359 s; median 23.8 tok/s, slowest stream ~19 tok/s. The gap between P50 and
P100 is queueing (client pool and/or server saturation), not variance in the
model itself.

---

## Deployment to pcai-solutions (hardlink mirror)

The canonical copy of this repo lives here; `pcai-solutions/tools/model-benchmarker/`
is a **hardlink mirror** — every mirrored file is the same inode, so an edit
through either path updates both. Sync is driven by `hardlinker.py` +
`hardlink_config.json`:

```bash
python3 hardlinker.py --config hardlink_config.json                 # preview (dry run)
python3 hardlinker.py --config hardlink_config.json --run           # link/overwrite for real
python3 hardlinker.py --config hardlink_config.json --run --prune   # also offer to delete dest orphans
```

- The shipped config sets `"dry_run": true`, so a bare run is a **preview
  only** — pass `--run` to apply.
- `on_conflict: overwrite` relinks any dest file whose inode differs from
  the source.
- The ignore list excludes files that must not ship: `pcai_models.py` (it
  embeds cluster-internal URLs and bearer keys — this is why
  `--model_class_name` is unavailable in the deployment; use `--url` /
  `--api_key` there), plus `ruff.toml`, `mypy.ini`, `formatter.sh`,
  `prune_charts.py`, caches and `*.zip`.
- Stale `*-<version>.tgz` / `.tar.gz` chart archives in the repo root are
  auto-pruned (newest kept) on every run; `--no-charts` disables that.

---

## HTML report - results_to_html.py

The text tables under `results/` carry the numbers but not the *setup*
context that produced them (GPU, MTP/speculative-decoding config, replicas,
engine, HiCache, ...). People reading a raw .txt file out of context can lose
that context. `results_to_html.py` turns the whole `results/` tree into one
self-contained HTML file that makes the setup explicit per run:

| Field | Source |
|---|---|
| **Model** | results sub-directory (e.g. `qwen_38_27b`) |
| **GPU type + count** | filename (e.g. `H200Sx4` -> 4x NVIDIA H200 **PCIe** — the trailing `S` is a legacy filename marker, not SXM) |
| **MTP / speculative-decoding config** | filename (`dflash2`, `dspark`, `eagle`, `dsp`, `mtp`) or catalog; else `Disabled` |
| **Weights** | `nvfp4` / `fp8` / `bf16` / `fp16` token in the filename |
| **Engine** | `sglang` / `vllm` token, or catalog image |
| **HiCache** | `hicache` or `hicachexN` token |
| **Replicas** | `replicasxN` token (default 1) |
| **Obsolete** | files starting `OLD_` are flagged |
| **PCAI deployment config** | expandable block linking to the matching Model-Downloader catalog entry (image, serving args, resources) |
| **Compare** | tick 2+ setups for a side-by-side table on shared (ctx, users, task) workloads with best/worst highlighted and a metric selector |

The HTML is fully self-contained (no external CSS/JS), so the file can be
shared by itself - no repo checkout needed. **Re-run it after adding any
`results/<model>/*.txt`** — `results/benchmark_report.html` is a generated
snapshot, not live. See also [`results/README.md`](results/README.md) for how
the committed results were produced.

```bash
python3 src/model_benchmarker/results_to_html.py                 # uses <repo-root>/results (two levels above the script)
python3 src/model_benchmarker/results_to_html.py --results path --output out.html
python3 src/model_benchmarker/results_to_html.py --catalog path/seed_catalog.json
python3 src/model_benchmarker/results_to_html.py --title "My benchmarks" --open
python3 src/model_benchmarker/results_to_html.py \
  --results_label "tools/model-benchmarker/results/" \
  --catalog_label "tools/model-downloader-web/src/model_downloader/app/seed_catalog.json"
```

`--results_label` / `--catalog_label` only change the paths *displayed* in the
report header (defaults show the pcai-solutions layout).

The matching Model-Downloader catalog entry is auto-discovered by walking up
from the script and checking, at each level,
`ModelDownloader/src/model_downloader/app/seed_catalog.json` and
`pcai-solutions/tools/model-downloader-web/src/model_downloader/app/seed_catalog.json`.
Pass `--catalog` explicitly otherwise. The catalog is a JSON **list** of
entries using `name` / `catalog_id` / `image` / `arguments` / `tier` /
`resource_request_*` keys. Without a catalog, MTP shows `Disabled` and the
engine is inferred only from the filename.

Result files must live in a per-model subdirectory — `results/<model-dir>/<SETUP>.txt`.
The directory name is the model slug; add it to `MODEL_NAMES` in
`results_to_html.py` for a pretty display name (otherwise it is title-cased).
The expected result-file naming convention is:

```
<GPU>[x<count>][_sglang|_vllm][_dflash|_dflash2|_dspark|_dsp|_eagle|_eagle3|_mtp|_mtp2|_disabled]
    [_nvfp4|_fp8|_fp8_e4m3|_bf16|_fp16][_hicache[xN]][_replicasxN].txt
```

e.g. `H200_sglang_dflash2_hicachex3_replicasx3.txt`, `RTXPRO6000x2_hicachex16.txt`,
`H200.txt`, `OLD_RTXPRO6000x1.txt` (the `OLD_` prefix flags the file obsolete in
the report).
