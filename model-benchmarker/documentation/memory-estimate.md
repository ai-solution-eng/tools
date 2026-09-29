# CLI reference — `memory-estimate`

Complete flag reference for
[`src/model_benchmarker/memory_model/cli.py`](../src/model_benchmarker/memory_model/cli.py).
For the math (weights / KV-per-token / capacity formulas per attention
structure) and worked examples see the repo
[`README.md`](../README.md) (Part 3); for the artifact's place in the
results tree and the report's Memory tab see
[`results-and-report.md`](results-and-report.md).

Run as a module or via the installed console script:

```bash
PYTHONPATH=src python -m model_benchmarker.memory_model --help
# or, after `pip install -e .`:
memory-estimate --help
```

Exit codes: `0` fits (or scenario grid produced), `2` config error
(model/config.json not found, unknown catalog id).

## Selection

| Flag | Description |
|---|---|
| `--model REF` | **Required.** One of: a local model directory (its `config.json` is the structure source), an HF repo id (local cache first, then hub fetch if `huggingface_hub` is installed and online), or a catalog deployment name (e.g. `deepseek-v4-flash-0731`) — the last only when `--catalog-id`/`--catalog` resolves it to a repo id. |
| `--catalog-id ID` | Take serving args from this `seed_catalog.json` entry: `resource_request_gpu` → GPU count, `--tp-size` → TP, `--kv-cache-dtype` → KV dtype, `--mem-fraction-static` → mem fraction, `tier` → GPU model. Explicit flags win over catalog values. |
| `--catalog PATH` | Seed catalog path (default: auto-discovered next to the repo, same lookup as `results_to_html.py`). |

## Hardware / serving shape

| Flag | Description |
|---|---|
| `--gpu NAME` | Free-text GPU name resolved against the built-in DB (`H200 PCIe`, `H200 SXM`, `RTX Pro 6000`, `H100`, `L40S`, `A100`, `B200`, `MI300X`, ...). Default when omitted: H200. |
| `--gpu-vram GIB` | Override VRAM for GPUs not in the DB (implies a custom GPU named by `--gpu`). |
| `--gpus N` | GPU count (default 1). |
| `--tp N` | Tensor-parallel size (default: all GPUs). TP cannot exceed `--gpus`; `gpu_count > tp` models N/tp replicas. |
| `--weight-dtype DT` | Weights dtype (`bf16`, `fp8`, `nvfp4`, ...). Default: `quantization_config.quant_method`, falling back to `torch_dtype`, in the model's config. |
| `--kv-dtype DT` | KV cache dtype (`fp16`/`bf16` default; `fp8_e4m3` halves it). |
| `--mem-fraction F` | Engine `mem-fraction-static` (default 0.9; catalog entries override with their own value). |
| `--overhead GIB` | Per-GPU activation/CUDA-graph reserve (default 2). The honesty knob — see README Part 3. |
| `--speculative N` | N active MTP/draft layers: adds their KV bytes per token (capped by the checkpoint's `num_nextn_predict_layers`). |
| `--hicache ratio\|size` | Enable the hierarchical KV cache (SGLang HiCache) L2 host tier: `ratio` sizes it from `--hicache-ratio` (× the device KV pool), `size` from `--hicache-size` (GiB per replica). |
| `--hicache-ratio X` | L2 = X × the device KV pool, per replica (with `--hicache ratio`; the engines' `--hicache-ratio`). |
| `--hicache-size GIB` | Explicit L2 host RAM per replica (with `--hicache size`). |
| `--hicache-l3 GIB` | L3 backing-tier (NVMe / object store) budget per replica. Capped at the L2 pool — a backing tier larger than host RAM is never filled. |
| `--hicache-tp-replicated` / `--hicache-tp-sharded` | Force the L2 layout across TP ranks. The auto rule already matches every family in the estimator: MLA/MQA ranks hold the full stream per token (L2 dedupes to one copy per token), GQA/MHA ranks hold their 1/tp share. Only force this when benchmarking an engine that differs. |

### How the tier math works

The device pool (L1) and both host tiers share one per-token figure: the
full-model KV bytes/token (`kv_bpt`). A TP group of P GPUs jointly stores
ONE copy of each token (sharded layouts put 1/tp on each rank; replicated
layouts put the full stream on every rank and the capacity math treats the
group as the unit), so:

```text
device_tokens = pool_per_gpu x gpus / kv_bpt          (x replicas when replicated)
L2 bytes/replica = hicache_ratio x pool_per_gpu x tp  (sharded, engine layout)
                 | hicache_ratio x pool_per_gpu       (replicated: write-back dedupes)
                 | hicache_size (explicit)
L2 tokens = L2 bytes x replicas / kv_bpt
L3 tokens = min(l3_gib, l2_gib) x replicas / kv_bpt
```

Invariant (verified by tests, both layouts): **ratio-mode L2 tokens =
ratio × device tokens per replica.** L2 is instance-private (one pool per
replica), so N replicas multiply the tier. The artifact reports each tier's
tokens separately plus an `addressable tokens (all tiers)` row; the web
estimator renders the same table with per-tier concurrency columns.

## Output shape

| Flag | Description |
|---|---|
| `--context CTX` | Repeatable; context lengths for the capacity grid (default 4k→128k). The last value is the headline scenario's context. |
| `--grid` | Sweep TP sizes (1, 2, 4, ... ≤ GPUs) as separate scenario rows, each with its own grid row. |
| `--grid-gpus 1,2,4,8` | With `--grid`: also sweep GPU counts. |
| `--output PATH` | Write the Markdown artifact (results-tree convention: `Status:` line, 2-cell config tables, wide capacity grid). |
| `--json` | Print the payload as JSON instead of markdown (artifact fields: `config`, `structure`, `scenarios`, `grid`, `warnings`). |

## Notes

- NVLink topology (1×8 vs 2×4) changes performance, not capacity — TP pools
  memory identically either way; the estimate needs no topology input.
- `gpu_count > tp` means replicas: each replica contributes its own KV pool
  to the total (checked by tests: TP1 ×2 GPUs = 2× one GPU's pool).
- HiCache host tiers extend what a deployment can HOLD (cold/prefix pages
  served on fault), not the hot decode pool — per-tier concurrency columns
  in the web UI make that distinction explicit.
- Unknown model families fall back to dense-GQA math with a warning in the
  artifact; fields the math needs but the config lacks raise a
  self-describing error (`UnsupportedArch`).
