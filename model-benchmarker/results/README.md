# results/ — benchmark outputs and how to reproduce them

Each chat-model `.md` under a per-model directory is the `--output` of one `benchmark_chat.py` run, written as Markdown (run configuration as bullets, a `Status:` line, then the results table as a GitHub-flavored Markdown table); the files in `RAG/` are Markdown transcripts of a separate RAG scale-benchmark tool (`benchmark.py`, MCP mode) and are parsed by a dedicated path in the report generator. Full flag documentation lives in the repo-root [`../README.md`](../README.md); this file explains the standard configuration these
results were produced with and what to look for when reading them.

Every `--output` file also carries the run's configuration as a bullet list (model, modes, `extra_body`) and a `Status:` line above the table — `complete` for a full sweep, `INTERRUPTED`/`CRASHED` when the run died mid-sweep and only the completed levels are in the table (the file is rewritten after every level, so an aborted benchmark still leaves its partial results).

## How these were produced

The standard configuration, one run per model:

```bash
export m='deepseek_v4_flash_280B'   # for example
# run from src/model_benchmarker/ (or expand the path); add --remote when
# running from outside the serving cluster:
python benchmark_chat.py --model_class_name "$m" --remote --number_users 1,4,32,64 \
  --requests_per_user 5 --tasks coding,creative,mixed \
  --context_length 0,32768 --multiturn --separate_tasks \
  --output "results/<model-dir>/<SETUP>.md"
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

This models "many opencode users (or back-to-back conversations) on one serving pod."

## Files

Filenames follow the convention `<GPU>[xN][_sglang|_vllm][_MTP][_weights][_hicachexN][_replicasxN].md` (see the repo README for the full grammar); `OLD_`-prefixed files are flagged obsolete in the HTML report.

| File | Hardware / notes |
|---|---|
| `deepseek_v4_flash_0731/H200Sx4_hicachex2.md` | DeepSeek-V4-Flash-0731, 4× H200, HiCache ×2 |
| `deepseek_v4_flash_0731/RTXPRO6000x2_hicachex16.md` | 2× RTX PRO 6000 (memory-tight), HiCache ×16 |
| `qwen_38_27b/H200.md` | Qwen3.8-27B, 1× H200 |
| `qwen_38_27b/H200_sglang_dflash2_hicachex1.md` | Qwen3.8-27B, 1× H200, SGLang + DFlash2, HiCache ×1 |
| `qwen_38_27b/H200_sglang_dflash2_hicachex3.md` | Qwen3.8-27B, 1× H200, SGLang + DFlash2, HiCache ×3 |
| `qwen_38_27b/H200_sglang_dflash2_hicachex3_replicasx3.md` | Same, 3 replicas |
| `qwen_38_27b/RTXPRO6000_vllm_fp8.md` | Qwen3.8-27B, 1× RTX PRO 6000, vLLM, FP8 |
| `qwen_38_27b/RTXPRO6000_vllm_nvfp4.md` | Qwen3.8-27B, 1× RTX PRO 6000, vLLM, NVFP4 |
| `gemma_4_31b/H200x1.md` | Gemma-4-31B, 1× H200 |
| `gemma_4_31b/OLD_RTXPRO6000x1.md` | Gemma-4-31B, 1× RTX PRO 6000 (obsolete) |
| `glm-5.2/H200x8_8waynvlink_hicache_1TB.md` | GLM-5.2-753B, 8× H200 (8-way NVLink), HiCache on + 1 TB host cache (in-progress run) |
| `glm-5.3-flash/H200x4_hicachex4.md` | GLM-5.3-Flash, 4× H200, HiCache ×4 |
| `glm-5.3-flash/H200x4_NVLink2_hicachex6.md` | GLM-5.3-Flash, 4× H200 (dual NVLink), HiCache ×6 |
| `RAG/rag_benchmark_scale_medium_n_100.md` | Multimodal-RAG scale run (medium chart, N=100) |
| `RAG/rag_benchmark_scale_large_n_100.md` | Multimodal-RAG scale run (large chart, N=100) |
| `RAG/rag_benchmark_scale_large_n_200.md` | Multimodal-RAG scale run (large chart, N=200) |
| `benchmark_report.html` | Generated — re-run `results_to_html.py` after adding files |

## Reading a file

The results table is a GitHub-flavored Markdown table with one row per (`ctx`, `users`, `task`) and a percentile group per metric — `TTFT turn1`, `TTFT-post` (multiturn runs only) and `tokens/s`, each as P50/P95/P99/P100:

| ctx | users | task | failed | TTFT turn1 P50 (ms) | … | TTFT-post P50 (ms) | … | tokens/s P50 | … |
|---|---|---|---|---|---|---|---|---|---|

- **TTFT turn1** — first request of a user's conversation: full cold prefill.
- **TTFT-post** — turns 2+: the shared prefix is already cached, so this is the warm/cached path. `TTFT-post << TTFT turn1` = the cache is earning its keep.
- **failed** — requests that errored (usually `Request timed out.` = the client's request timeout under server saturation); they are excluded from the percentile stats.
- **tokens/s** — generation throughput; percentiles are *inverted* (P100 = slowest), higher is better everywhere.

## What to look for

- **TTFT turn1 vs TTFT-post gap** — the hierarchical cache avoids expensive prefill operations, so at higher concurrency TTFT-post should be dramatically lower than turn1 (cached tokens are nearly free). This shows up most strongly when cache memory is tight (e.g. the 2× RTX PRO 6000 box).
- **failed = 0** on the rows you care about; nonzero means that (ctx, users) level pushed the server past its sustainable concurrency / timeout budget.
- **tokens/s stability** — small P50→P100 spread = the pod sustains the load.

There are no saved non-hiCache runs: this setup was built for the hierarchical cache, so that's all we have data for.

## Caveats when comparing results

- Results may look better or worse than the *real* experience: DeepSeek is the shared, premier model — teammates actively driving traffic to it will cut into measured throughput.
- The client httpx pool defaults to 128 connections (`MODEL_POOL_MAX_CONNECTIONS`); keep it ≥ your max `--number_users` or the *high* percentile TTFT becomes a client-side artifact.
- Every request carries a unique nonce (unless `--no-nonce`), so turn1 is a true cold prefill — a deliberate worst case, not a cache hit.