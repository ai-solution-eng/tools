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
| `deepseek_v41_flash/H200x4_engram_offload.md` | DeepSeek-V4.1-Flash, 4× H200, weight/ expert-offload serving (webapp run 20261004T193334Z, fused with its two resume-source attempts) |
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
| `glm-5.3-flash/H200x8_smx_hicache_1_5TB.md` | GLM-5.3-Flash, 8× H200 (SMX), HiCache + 1.5 TB host cache |
| `RAG/rag_benchmark_scale_medium_n_100.md` | Multimodal-RAG scale run (medium chart, N=100) |
| `RAG/rag_benchmark_scale_large_n_100.md` | Multimodal-RAG scale run (large chart, N=100) |
| `RAG/rag_benchmark_scale_large_n_200.md` | Multimodal-RAG scale run (large chart, N=200) |
| `benchmark_report.html` | Generated — re-run `results_to_html.py` after adding files |
| `prefill_inferred/H200x4_32k_logs.md` | Prefill throughput inferred from SGLang server logs (not a `benchmark_chat` run); see below |
| `prefill_inferred/H200x4_32k_logs.log` | Reconstructed `Prefill batch` step records behind the above |

## Reading a file

The results table is a GitHub-flavored Markdown table with one row per (`ctx`, `users`, `task`) and a percentile group per metric — `TTFT turn1`, `TTFT-post` (multiturn runs only) and `tokens/s`, each as P50/P95/P99/P100:

| ctx | users | task | failed | TTFT turn1 P50 (ms) | … | TTFT-post P50 (ms) | … | tokens/s P50 | … |
|---|---|---|---|---|---|---|---|---|---|

- **TTFT turn1** — first request of a user's conversation: full cold prefill.
- **TTFT-post** — turns 2+: the shared prefix is already cached, so this is the warm/cached path. `TTFT-post << TTFT turn1` = the cache is earning its keep.
- **failed** — requests that errored (usually `Request timed out.` = the client's request timeout under server saturation); they are excluded from the percentile stats.
- **tokens/s** — generation throughput; percentiles are *inverted* (P100 = slowest), higher is better everywhere.
- **prefill tok/s (agg)** — aggregate prefill throughput for the level, `users × ctx / TTFT turn1 P100`. See below.

## Prefill throughput

`tokens/s` is *decode* throughput — it deliberately excludes TTFT. Prefill has
to be derived from TTFT, and the `prefill tok/s (agg)` column does that:

    aggregate prefill tok/s = users × ctx / (TTFT turn1 P100 in seconds)

All `users` requests fire at once, so the last first-token (P100) marks when all
`users × ctx` prompt tokens are done. This is an AGGREGATE rate: it is
flat-to-rising with concurrency, because more users each prefilling the same
prompt is more total work in a similar window shape. Divide by `users` for the
per-user rate, which falls ~1/users.

**It does not mean the hardware is at peak.** At ctx=32768 a single user already
saturates prefill — the aggregate barely moves from n=1 to n=32 — but the
achieved FLOPS sit around 30% of the 4× H200 BF16 peak. The limiter is memory
bandwidth and the FP4/FP8 expert weights, not FMA throughput. "The GPU is the
bottleneck" and "the GPU is at peak" are different claims; only the first is
supported here.

### Estimates for every model

Every results file carrying a padded (`ctx > 0`) level has a
`## Derived: aggregate prefill throughput` section appended. Single-user
(ctx=32768, task=coding) figures, which are the cleanest comparison since P100/P50
is exactly 1.00 there. Gemma ran only the `mixed` task, so its row is not
task-matched with the rest:

| setup | agg prefill tok/s (n=1) | at n=32 |
|:---|---:|---:|
| GLM-5.3-Flash 8× H200 SMX, HiCache 1.5 TB | 24,573 | 32,668 |
| GLM-5.3-Flash 4× H200, HiCache ×4 | 15,419 | 26,879 |
| Qwen3.8-27B 1× H200 (plain) | 13,633 | 17,055 |
| GLM-5.3-Flash 4× H200 dual-NVLink, HiCache ×6 | 11,903 | 12,698 |
| DeepSeek-V4.1-Flash 4× H200, offload | 11,516 | 11,918 |
| Qwen3.8-27B 1× RTX PRO 6000, vLLM NVFP4 | 12,022 | 14,118 |
| Qwen3.8-27B 3 replicas, SGLang HiCache ×3 | 11,518 | 35,084 |
| Qwen3.8-27B 1× H200, SGLang HiCache ×1 | 10,980 | 6,338 ⚠ |
| Qwen3.8-27B 1× RTX PRO 6000, vLLM FP8 | 9,400 | 10,235 |
| DeepSeek-V4-Flash 4× H200, HiCache ×2 | 20,331 | 28,761 |
| DeepSeek-V4-Flash 2× RTX PRO 6000, HiCache ×16 | 8,820 | 9,036 |
| GLM-5.2-753B 8× H200, 1 TB host cache | 7,774 | 5,031 ⚠ |
| Gemma-4-31B 1× H200 (mixed task only) | 5,377 | 5,853 |

⚠ = straggler-dominated at that level (see below), not a real regression.

Two files carry a *large*-context level, which is the only place the O(S) per-token
cost shows up directly:

| setup | ctx=32768 | ctx=512k / 524k |
|:---|---:|---:|
| GLM-5.3-Flash 4× H200 HiCache ×4 | 15,419 | 21,425 (512k) |
| GLM-5.3-Flash 8× H200 SMX HiCache 1.5 TB | 24,573 | 25,541 (524k) |

Both hold up at ~20-25k tok/s at half a million tokens, so the expected falloff is
not visible on these deployments — the compressed/DSA attention keeps the
per-token cost nearly flat in S.

Two caveats, both encoded in the column:

- **`ctx` is nominal.** The padding rule assumes 4 chars/token; real tokenizers
  average ~5.3, so the true prompt is ~25% shorter and these figures run ~25%
  high. Runs after MB-PREFILL record the server-reported `prompt_tokens` and use
  that instead.
- **A leading `~` means straggler-dominated.** P100 is one sample. When
  P100/P50 exceeds 2, a single slow request set the level's number; treat it as
  a floor. (Measured case: a GLM n=64 level read P100/P50 = 10.4 while its
  neighbours at the same n read 2.0 and 5.3 — noise, not a hardware cliff.)

For a direct engine-side measurement instead of a TTFT derivation, capture the
serving pod's logs during a run and use `model_benchmarker.prefill_cli`:

    kubectl logs <pod> -n <ns> -c kserve-container > logs.txt
    PYTHONPATH=src python -m model_benchmarker.prefill_cli logs.txt \
        --out results/prefill_inferred/<name>.md \
        --log-out results/prefill_inferred/<name>.log \
        --namespace <ns>

That reads SGLang's own `input throughput (token/s)` and token counters. Capture
the logs *with* the run — container log buffers roll over within hours, and once
they do the measurement cannot be regenerated.

## What to look for

- **TTFT turn1 vs TTFT-post gap** — the hierarchical cache avoids expensive prefill operations, so at higher concurrency TTFT-post should be dramatically lower than turn1 (cached tokens are nearly free). This shows up most strongly when cache memory is tight (e.g. the 2× RTX PRO 6000 box).
- **failed = 0** on the rows you care about; nonzero means that (ctx, users) level pushed the server past its sustainable concurrency / timeout budget.
- **tokens/s stability** — small P50→P100 spread = the pod sustains the load.

There are no saved non-hiCache runs: this setup was built for the hierarchical cache, so that's all we have data for.

## Caveats when comparing results

- Results may look better or worse than the *real* experience: DeepSeek is the shared, premier model — teammates actively driving traffic to it will cut into measured throughput.
- The client httpx pool defaults to 128 connections (`MODEL_POOL_MAX_CONNECTIONS`); keep it ≥ your max `--number_users` or the *high* percentile TTFT becomes a client-side artifact.
- Every request carries a unique nonce (unless `--no-nonce`), so turn1 is a true cold prefill — a deliberate worst case, not a cache hit.