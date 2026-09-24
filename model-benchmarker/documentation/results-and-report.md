# Result artifacts and the HTML report

How `benchmark_chat.py --output` files are structured, how filenames encode
the serving setup, and how
[`src/model_benchmarker/results_to_html.py`](../src/model_benchmarker/results_to_html.py)
turns the `results/` tree into one self-contained HTML report.

## Layout

```
results/
├── <model-dir>/          # one directory per model slug (e.g. qwen_38_27b)
│   └── <SETUP>.md        # one file per model × serving setup — the --output of one run
├── RAG/                  # transcripts of a separate RAG scale-benchmark tool
├── benchmark_report.html # GENERATED snapshot — re-run results_to_html.py after adding files
└── README.md             # the exact commands the committed results were produced with
```

Files must live in a per-model subdirectory — files placed directly in
`results/` are not picked up by the report. The directory name is the model
slug; add it to `MODEL_NAMES` in `results_to_html.py` for a pretty display
name (otherwise it is title-cased).

## Anatomy of a result file

Each `<SETUP>.md` is written by `write_results_file()` in `benchmark_chat.py`:

1. `# <filename stem>` — the H1 title.
2. The **run configuration as bullets** — model class/name, tasks with their
   `max_tokens`, `requests_per_user`, `context_lengths`, one MODE line per
   active mode (`multiturn`, `no-nonce`, `prewarm`), and the effective
   `extra_body` (thinking overrides). This banner is built once and embedded
   in every rewrite, so even a crashed run's file records what produced it.
3. Two fixed explanation lines (percentile semantics; turn1 vs TTFT-post).
4. A **`Status:` line** — `complete — all N levels completed`, or
   `in progress — k/n levels completed` (rewritten after every level), or
   `INTERRUPTED`/`CRASHED` when the run died mid-sweep. The file is written
   atomically (`.partial` temp + `os.replace`), so an aborted benchmark
   always leaves its completed levels on disk, and never a truncated file.
5. The results table as a **GitHub-flavored Markdown table**, one row per
   (ctx, users, task), which `results_to_html.py` parses back:

```
| ctx | users | task | failed | TTFT turn1 P50 (ms) | … | TTFT-post P50 (ms) | … | tokens/s P50 | … |
```

- **`ctx`** — target input-token prefill length (0 = unpadded).
- **`users`** — concurrent simulated users for that level.
- **`failed`** — requests in that row that errored (usually the 600 s read
  timeout under saturation); excluded from the percentile stats. Non-zero
  means that (ctx, users) level pushed the server past its sustainable
  concurrency/timeout budget.
- **`TTFT turn1`** — first request of a user's conversation: full cold
  prefill. Percentiles ascending; bigger is worse.
- **`TTFT-post`** — turns 2+: the shared prefix is already cached. Shown
  only in `--multiturn` runs. At padded contexts `TTFT-post ≪ turn1` is the
  prefix/KV/HiCache cache earning its keep; at `ctx=0` the post turns can be
  *slower* than turn 1, because the replayed history (long assistant
  answers) outgrows turn 1's unpadded prompt — read the pair in context.
- **`tokens/s`** — generation throughput excluding TTFT. Percentiles are
  **inverted**: P100 is the slowest stream, so higher is better everywhere.
  A small P50→P100 spread means the setup sustains the load.

### Worked example

From `results/qwen_38_27b/H200_sglang_dflash2_hicachex3_replicasx3.md`
(Qwen3.8-27B, 1× H200, SGLang + DFlash2, HiCache ×3, 3 replicas;
`Status: complete`):

| Row | Reading |
|---|---|
| ctx=0, users=1, coding | turn1 TTFT P50 1008 ms; TTFT-post P50 2879 ms — the post turn's replayed history is bigger than the unpadded turn-1 prompt. |
| ctx=32768, users=32, coding | turn1 TTFT P50 ≈ 18.0 s (cold 32k prefill under 32-user load) vs TTFT-post P50 ≈ 4.6 s (cached prefix) — the cache cuts TTFT ~4×. tokens/s P50 85.2 → P100 49.8. |
| ctx=32768, users=64, mixed | `failed = 2` of 320 requests; TTFT-post P99 ≈ 761 s — this level is past the setup's sustainable concurrency. |

## Filename grammar — `<SETUP>`

The filename is the only place the serving setup is recorded, so it follows
a fixed grammar that the report parses:

```
<GPU>[x<count>][_sglang|_vllm][_dflash|_dflash2|_dspark|_dsp|_eagle|_eagle3|_mtp|_mtp2|_disabled]
    [_nvfp4|_fp8|_fp8_e4m3|_bf16|_fp16][_hicache[xN]][_replicasxN].md
```

e.g. `H200_sglang_dflash2_hicachex3_replicasx3.md`, `RTXPRO6000x2_hicachex16.md`,
`H200.md`, `OLD_RTXPRO6000x1.md` (the `OLD_` prefix flags the file obsolete
in the report).

What the report derives from the filename (falling back to the
Model-Downloader catalog, then to `Disabled`/inference):

| Field | Source |
|---|---|
| **Model** | results sub-directory |
| **GPU type + count** | filename (e.g. `H200Sx4` → 4× NVIDIA H200 PCIe — the trailing `S` is a legacy filename marker, not SXM) |
| **MTP / speculative decoding** | `dflash2`, `dspark`, `eagle`, `dsp`, `mtp` token or catalog; else `Disabled` |
| **Weights** | `nvfp4` / `fp8` / `bf16` / `fp16` token |
| **Engine** | `sglang` / `vllm` token, or catalog image |
| **HiCache** | `hicache` or `hicachexN` token |
| **Replicas** | `replicasxN` token (default 1) |
| **Obsolete** | files starting `OLD_` |

## HTML report — `results_to_html.py`

```bash
python3 src/model_benchmarker/results_to_html.py                    # <repo-root>/results
python3 src/model_benchmarker/results_to_html.py --results path --output out.html
python3 src/model_benchmarker/results_to_html.py --catalog path/seed_catalog.json
python3 src/model_benchmarker/results_to_html.py --title "My benchmarks" --open
python3 src/model_benchmarker/results_to_html.py \
  --results_label "tools/model-benchmarker/results/" \
  --catalog_label "tools/model-downloader-web/src/model_downloader/app/seed_catalog.json"
```

| Flag | Default | Purpose |
|---|---|---|
| `--results` | `results/` two levels above the script | Results directory to scan (globs `*.md`; also parses legacy fixed-width `.txt`). |
| `--output` | `results/benchmark_report.html` | Output HTML path. |
| `--catalog` | auto-discovered | Path to the Model-Downloader `seed_catalog.json`. Discovery walks up from the script checking `ModelDownloader/src/model_downloader/app/seed_catalog.json` and `pcai-solutions/tools/model-downloader-web/src/model_downloader/app/seed_catalog.json` at each level. The catalog is a JSON list using `name` / `catalog_id` / `image` / `arguments` / `tier` / `resource_request_*` keys. Without it, MTP shows `Disabled` and the engine is inferred from the filename only. |
| `--title` | `PCAI Model Benchmarks` | Page title. |
| `--open` | off | Open the report in the default browser. |
| `--results_label` / `--catalog_label` | pcai-solutions layout paths | Display-only path labels shown in the report header. |

The report groups everything by model, shows the full serving metadata per
setup, links each run to the matching Model-Downloader catalog entry (image,
serving arguments, resources) in a "PCAI deployment config" expander, and
lets you tick 2+ setups for a **side-by-side comparison** on shared
(ctx, users, task) workloads with best/worst highlighted and a metric
selector. The HTML is fully self-contained (no external CSS/JS) — share the
file by itself, no repo checkout needed.

## RAG transcripts

The files in `results/RAG/` are Markdown transcripts of a separate RAG
scale-benchmark tool (`benchmark.py --mode mcp` from the MultimodalRAG
project: N concurrent users, fixed duration, top-k retrieval against an
MCP server). They carry their own configuration table and are parsed by a
dedicated path in the report generator — do not mix that format with
`benchmark_chat.py` output files.

## Caveats when comparing runs

- Shared premier models (e.g. DeepSeek) serve teammates' live traffic;
  measured throughput reflects that contention, not the pod's ceiling.
- Keep `MODEL_POOL_MAX_CONNECTIONS` ≥ your max `--number_users`, or
  high-percentile TTFT becomes a client-side artifact (see
  [`cli-reference.md`](cli-reference.md)).
- The default per-request nonce makes turn 1 a true cold prefill by design —
  a deliberate worst case. Only compare runs that used the same
  nonce/multiturn/prewarm regime.
- There are no saved non-HiCache runs in the committed tree: this setup was
  built for the hierarchical cache, so that is all there is data for.
