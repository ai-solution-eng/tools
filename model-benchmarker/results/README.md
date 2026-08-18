# results/ — benchmark outputs and how to reproduce them

Each `.txt` here is the `--output` of one `benchmark_chat.py` run. Full flag
documentation lives in the repo-root [`../README.md`](../README.md); this file
explains the standard configuration these results were produced with and what
to look for when reading them.

## How these were produced

The standard configuration, one run per model:

```bash
export m='deepseek_v4_flash_280B'   # for example
python benchmark_chat.py --model_class_name "$m" --number_users 1,4,32,64 \
  --requests_per_user 5 --tasks coding,creative,mixed \
  --context_length 0,32768 --multiturn --separate_tasks --output ${m}.txt
```

For small systems I only run up to `--number_users 32`.

### What each knob does

| Setting | Meaning |
|---|---|
| `--number_users 1,4,32,64` | Sweep of concurrent users. All conversations start **simultaneously** (no ramp). |
| `--requests_per_user 5` | Each user sends 5 back-to-back messages in one growing conversation → isolates prefill (turn 1) vs cached-prefix (turns 2+) performance. |
| `--tasks coding,creative,mixed` | Vary the workload; some MTP (multi-token-prediction) algorithms, for example, are noticeably better on `coding` than on `creative`. |
| `--context_length 0,32768` | Cold start (no history) vs. ~2^15 = 32k tokens already in memory — the high-context case leans heavily on caching. |
| `--multiturn` | Same-prefix reuse across each user's 5 turns (engages the server's prefix/KV/hierarchical cache). |
| `--separate_tasks` | Each task gets its own clean pass so tasks don't interfere in one mixed batch. |

This models "many opencode users (or back-to-back conversations) on one
serving pod."

## Files

| File | Hardware / notes |
|---|---|
| `deepseek_v4_flash_0731/H200Sx4_2x_hicache.txt` | DeepSeek-V4-Flash-0731, 4× H200, hierarchical cache |
| `deepseek_v4_flash_0731/RTX_PRO_6000_x2_16x_hicache.txt` | 2× RTX PRO 6000 (memory-tight) |
| `qwen_38_27b/H200.txt` | Qwen3.8-27B, H200 |
| `qwen_38_27b/RTX_PRO_6000_x2.txt` | Qwen3.8-27B, 2× RTX PRO 6000 |
| `gemma_4_31b/H200x1.txt` | Gemma-4-31B, 1× H200 |
| `gemma_4_31b/RTX_PRO_6000x1.txt` | Gemma-4-31B, 1× RTX PRO 6000 |
| `RAG/rag_benchmark_scale_medium_n_100.txt` | Multimodal-RAG scale run (N=100) |

## Reading a file

Column layout (one block per `ctx`+`users`, dotted separator between groups):

```
 ctx  users    task failed |  TTFT turn1 (ms)  |  TTFT-post (ms) |   tokens/s
```

- **TTFT turn1** — first request of a user's conversation: full cold prefill.
- **TTFT-post** — turns 2+: the shared prefix is already cached, so this is
  the warm/cached path. `TTFT-post << TTFT turn1` = the cache is earning its
  keep.
- **failed** — requests that errored (usually `Request timed out.` = the
  client's 300 s read timeout under server saturation); they are excluded
  from the percentile stats.
- **tokens/s** — generation throughput; percentiles are *inverted* (P100 =
  slowest), higher is better everywhere.

## What to look for

- **TTFT turn1 vs TTFT-post gap** — the hierarchical cache avoids expensive
  prefill operations, so at higher concurrency TTFT-post should be
  dramatically lower than turn1 (cached tokens are nearly free). This shows
  up most strongly when cache memory is tight (e.g. the 2× RTX PRO 6000 box).
- **failed = 0** on the rows you care about; nonzero means that (ctx, users)
  level pushed the server past its sustainable concurrency / timeout budget.
- **tokens/s stability** — small P50→P100 spread = the pod sustains the load.

There are no saved non-hiCache runs: this setup was built for the hierarchical
cache, so that's all we have data for.

## Caveats when comparing results

- Results may look better or worse than the *real* experience: DeepSeek is the
  shared, premier model — teammates actively driving traffic to it will cut
  into measured throughput.
- The client httpx pool defaults to 128 connections (`MODEL_POOL_MAX_CONNECTIONS`);
  keep it ≥ your max `--number_users` or the *high* percentile TTFT becomes a
  client-side artifact.
- Every request carries a unique nonce (unless `--no-nonce`), so turn1 is a
  true cold prefill — a deliberate worst case, not a cache hit.