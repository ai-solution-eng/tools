# results/ — benchmark outputs and how to reproduce them

Each chat-model `.txt` under a per-model directory is the `--output` of one
`benchmark_chat.py` run; the files in `RAG/` are transcripts of a separate
RAG scale-benchmark tool (`benchmark.py`, MCP mode) and are parsed by a
dedicated path in the report generator. Full flag documentation lives in the
repo-root [`../README.md`](../README.md); this file explains the standard
configuration these results were produced with and what to look for when
reading them.

## How these were produced

The standard configuration, one run per model:

```bash
export m='deepseek_v4_flash_280B'   # for example
# run from src/model_benchmarker/ (or expand the path); add --remote when
# running from outside the serving cluster:
python benchmark_chat.py --model_class_name "$m" --remote --number_users 1,4,32,64 \
  --requests_per_user 5 --tasks coding,creative,mixed \
  --context_length 0,32768 --multiturn --separate_tasks \
  --output "results/<model-dir>/<SETUP>.txt"
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

Filenames follow the convention `<GPU>[xN][_sglang|_vllm][_MTP][_weights][_hicachexN][_replicasxN].txt`
(see the repo README for the full grammar); `OLD_`-prefixed files are flagged
obsolete in the HTML report.

| File | Hardware / notes |
|---|---|
| `deepseek_v4_flash_0731/H200Sx4_hicachex2.txt` | DeepSeek-V4-Flash-0731, 4× H200, HiCache ×2 |
| `deepseek_v4_flash_0731/RTXPRO6000x2_hicachex16.txt` | 2× RTX PRO 6000 (memory-tight), HiCache ×16 |
| `qwen_38_27b/H200.txt` | Qwen3.8-27B, 1× H200 |
| `qwen_38_27b/H200_sglang_dflash2_hicachex1.txt` | Qwen3.8-27B, 1× H200, SGLang + DFlash2, HiCache ×1 |
| `qwen_38_27b/H200_sglang_dflash2_hicachex3.txt` | Qwen3.8-27B, 1× H200, SGLang + DFlash2, HiCache ×3 |
| `qwen_38_27b/H200_sglang_dflash2_hicachex3_replicasx3.txt` | Same, 3 replicas |
| `qwen_38_27b/RTXPRO6000_vllm_fp8.txt` | Qwen3.8-27B, 1× RTX PRO 6000, vLLM, FP8 |
| `qwen_38_27b/RTXPRO6000_vllm_nvfp4.txt` | Qwen3.8-27B, 1× RTX PRO 6000, vLLM, NVFP4 |
| `gemma_4_31b/H200x1.txt` | Gemma-4-31B, 1× H200 |
| `gemma_4_31b/OLD_RTXPRO6000x1.txt` | Gemma-4-31B, 1× RTX PRO 6000 (obsolete) |
| `glm-5.3-flash/H200x4_hicachex4.txt` | GLM-5.3-Flash, 4× H200, HiCache ×4 |
| `RAG/rag_benchmark_scale_medium_n_100.txt` | Multimodal-RAG scale run (medium chart, N=100) |
| `RAG/rag_benchmark_scale_large_n_100.txt` | Multimodal-RAG scale run (large chart, N=100) |
| `RAG/rag_benchmark_scale_large_n_200.txt` | Multimodal-RAG scale run (large chart, N=200) |
| `benchmark_report.html` | Generated — re-run `results_to_html.py` after adding files |

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
  client's request timeout under server saturation); they are excluded
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