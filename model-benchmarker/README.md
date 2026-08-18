# benchmark_chat — PCAI Chat Model Benchmarker

Measures **time-to-first-token (TTFT)** and **tokens/s** for OpenAI-compatible
chat endpoints (PCAI/vLLM/SGLang serving clusters) under realistic concurrency.

Run the script from anywhere in the repo or its hardlinked deployment:

```bash
python src/model_benchmarker/benchmark_chat.py ...
```

## What it measures

| Metric | Definition |
|---|---|
| **TTFT (ms)** | Time from request send until the first token of any kind (`content` or `reasoning_content`) arrives. |
| **tokens/s** | `usage.completion_tokens / (stream_end − first_token)` — generation throughput **excluding TTFT**. |

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

### Model selection

| Flag | Description |
|---|---|
| `--model_class_name NAME` | Use a registered model from `utils/pcai_models.py` (e.g. `deepseek_v4_flash_280B`, `qwen38_27B`, `qwen36_27B`, `gemma4_31B`, `glm_52_753B`). Imported lazily; not available in the hardlinked deployment. |
| `--url URL` | OpenAI-compatible endpoint root (no `/v1`). Required when `--model_class_name` is omitted. |
| `--api_key KEY` | Bearer token for the endpoint. |
| `--remote` | Use the public serving URL. Without it, the model targets its **in-cluster** URL (`.svc.cluster.local`). Omit `--remote` when running inside the same cluster. |

### Load shape

| Flag | Description |
|---|---|
| `--number_users N,M,...` | Concurrency levels to sweep. Default `1`. |
| `--requests_per_user N` | Sequential requests per user, cycling through `--tasks`. Default `1`. |
| `--tasks t1,t2,...` | Task types: `coding` (max_tokens 8192), `creative` (16384), `mixed` (24576 — one request that must do coding+creative), `custom`. Default `coding,creative`. |
| `--context_length 0,32768,...` | Prefill-length sweep; each level pads every prompt to ~that many input tokens (≈4 chars/token). `0` disables padding. Default `0`. |
| `--multiturn` | Each user runs `--requests_per_user` **turns of one growing conversation** (full history replayed each turn) instead of independent requests, so the shared prefix is reused and the server's prefix/KV cache engages. Gives the `turn1` vs `turns-post` split. |
| `--separate_tasks` | Run each task in its own (ctx, users) pass, so every task gets a clean turn-1 vs turns-post comparison without cross-task interference. |
| `--custom` args | `--prompt` (the prompt text), `--max_tokens`, `--temperature`, `--top_p` for the `custom` task. |

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
| `--output FILE` | Write the final summary table to FILE (in addition to stdout). |

### Environment

| Var | Default | Purpose |
|---|---|---|
| `MODEL_POOL_MAX_CONNECTIONS` | `128` | httpx connection-pool ceiling. With fewer than the concurrency level, requests serialize client-side and the *high percentile* TTFT blows up (your benchmark is pool-bound, not server-bound). Raise it to match your max `--number_users`. |
| `MODEL_POOL_MAX_KEEPALIVE_CONNECTIONS` | `10` | Idle keep-alive connections kept by the pool. |

The client read timeout is hardcoded (300 s) in `utils/pcai_model_classes.py`; very
long generations at high concurrency can exceed it and log `FAILED: Request timed out.`
That is the server being saturated, not a client bug.

---

## Examples

### 1. The standard capacity suite (reproduces `results/`)

One run per model: 1/4/32/64 concurrent users, 5-turn conversations, all tasks,
both cold (ctx=0) and 32k prefilled, each task isolated. This is exactly what
populated `results/`:

```bash
for m in deepseek_v4_flash_280B qwen38_27B gemma4_31B; do
  python src/model_benchmarker/benchmark_chat.py \
    --model_class_name $m --remote \
    --number_users 1,4,32,64 \
    --requests_per_user 5 \
    --tasks coding,creative,mixed \
    --context_length 0,32768 \
    --multiturn --separate_tasks \
    --output results/${m}.txt
done
```

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
  --output results/agentic_shared_prefix.txt
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
  --output results/agentic_prewarm_32k.txt
```

Compare Example 2 vs 3 to quantify how much of the gain was "cache already
resident" vs "requests joining the graph."

### 4. One-off smoke test

Quick sanity check against a single endpoint (no sweep, mixed tasks):

```bash
python benchmark_chat.py --url https://<endpoint> --api_key "$PCAI_KEY" \
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
    --output results/qwen_$lvl.txt
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
python benchmark_chat.py --model_class_name glm_52_753B --remote \
  --number_users 64 --requests_per_user 1 \
  --tasks coding --context_length 32768
```

### 11. Server-endpoint via `--url` on a cluster node (no `--remote`)

From a pod inside the same cluster, hit the in-cluster service DNS directly
(no external hop):

```bash
python benchmark_chat.py --url https://<model>.project-user-<name>.serving.<cluster>.<domain> \
  --api_key <token> \
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