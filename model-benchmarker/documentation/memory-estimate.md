# CLI reference — `memory-estimate`

Complete flag reference for
[`src/model_benchmarker/memory_model/cli.py`](../src/model_benchmarker/memory_model/cli.py).
The math (weights / KV-per-token / capacity formulas per attention
structure), worked examples and the interactive calculator are in this
document (§ How the math works); for the artifact's place in the
results tree and the report's Memory tab see
[`results-and-report.md`](results-and-report.md).

Run as a module or via the installed console script:

```bash
PYTHONPATH=src python -m model_benchmarker.memory_model --help
# or, after `pip install -e .`:
memory-estimate --help
```

## How the math works

A deterministic GPU-memory estimator for the "can I deploy this model?"
question that precedes every benchmark run: does it fit, how many KV tokens
fit in GPU memory, and how many concurrent N-token requests does that
support. Zero load, no endpoint — and its Markdown artifacts land in the
results tree like every other tool's (the report's **Memory** tab).

The formula chain (all three answers, one chain):

```
weights_per_gpu   = weight_bytes / tp_size          # all MoE experts are resident
usable_per_gpu    = vram_gib x 1024^3 x mem_fraction - overhead
kv_pool_per_gpu   = usable_per_gpu - weights_per_gpu
kv_tokens_total   = sum over GPUs of (kv_pool_per_gpu / kv_bytes_per_token)
concurrency(ctx)  = floor(kv_tokens_total / ctx)    # vLLM's "Maximum concurrency" line, predicted
```

The one number people get wrong is **KV bytes per token**, and it depends on
the attention structure — which is why the structure comes from the model's
own `config.json`, not from a hardcoded table:

| Structure | KV bytes/token/layer | Models |
|---|---|---|
| MHA | `2 x heads x head_dim x B` | (rare, dense) |
| GQA | `2 x kv_heads x head_dim x B` — the naive `hidden/heads` formula overestimates by the head-grouping factor (Llama-3-8B: 128 KiB/token, not 512) | Qwen3.x, Llama, GLM |
| MLA | `(kv_lora_rank + qk_rope_head_dim) x B` — one compressed latent per layer | DeepSeek-V2/V3 |
| MQA + sparse (CSA/HCA/DSA) | per-token MQA footprint; sparse selection changes what attention *reads*, not what the pool holds | DeepSeek-V4, GLM-DSA |
| linear-attention hybrid | linear layers carry **no** per-token KV (recurrent state only) | Qwen3-Next |
| sliding-window layers | budgeted at full per-token cost (conservative; engine reuse only frees memory) | Gemma |

`--kv-dtype fp8_e4m3` halves the KV figure (the seed catalog already
serves DeepSeek and Qwen this way).

### Worked examples

```bash
# the seed-catalog DeepSeek deployment: H200 x4, TP4, fp8 KV, mem-fraction 0.8
PYTHONPATH=src python -m model_benchmarker.memory_model \
    --model deepseek-v4-flash-0731 --catalog-id seed-h200-deepseek

# explicit shape + TP sweep + context grid -> artifact for the report's Memory tab
PYTHONPATH=src python -m model_benchmarker.memory_model --model Qwen/Qwen3-8B \
    --gpu "RTX Pro 6000" --gpus 1 --grid --output results/Qwen3.8-27B/memory.md
```

Exit codes: `0` fits (or a scenario grid was produced), `2` config error
(model/config.json not found, unknown catalog id). A run that cannot fit
still writes the artifact (the grid shows which TP level starts to fit).

### Interactive HTML deployment check — for users

```bash
PYTHONPATH=src python -m model_benchmarker.memory_model --calculator
```

writes `results/memory_check.html` — a self-contained page (no server, no
external assets; put it up or share the file by itself) where a user
validates **their deployment configuration** before submitting it: paste
the engine launch arguments
(`sglang serve ... --tp-size 4 --kv-cache-dtype fp8_e4m3 --hicache-ratio
2`), drop a `seed_catalog.json` on the file input, or fill the controls by
hand. The page answers the deployment questions with the same capacity math
(model structures from `config.json` are embedded at generation time;
`--add-model REF` adds more, `--no-fetch` goes offline):

- **Does it fit?** — fit verdict with a stacked VRAM bar (weights / KV pool / overhead).
- **Tokens in GPU memory** — the hot KV pool, per replica and total.
- **Tokens in HiCache** — host-tier tokens (ratio mode or fixed host-RAM mode), and the concurrency table with a "with HiCache tier" row (addressable ≠ hot).
- **A deterministic PASS/WARN/FAIL checklist** — weights fit, pool size, largest selected context vs pool, mem-fraction band (PCAI catalogs use 0.8–0.92), TP schedulability, NVLink-group crossing, MoE-without-EP, MTP/speculative consistency, HiCache tier sanity.
- Model picker with embedded structures (GQA/MLA/sparse/linear-hybrid aware), custom model form, TP sweep cards, light/dark.

The JS math is cross-checked against the Python estimator (node smoke tests
pin the catalog DeepSeek/GLM numbers); regeneration refreshes the embedded
models.

### Honest boundaries

- Deterministic capacity math only — no throughput/latency prediction, no
  "optimal config" guessing; pair it with `benchmark_chat` for the
  performance story.
- Parameter counts are shape-derived (all experts counted; MTP module and
  multimodal towers excluded — both stated in the artifact).
- The activation overhead is a knob, not a law: calibrate it against a live
  server's reported KV pool (`max_total_num_tokens` / `GPU KV cache
  size`) and record the predicted-vs-actual delta in the artifact.

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
| `--overhead GIB` | Per-GPU activation/CUDA-graph reserve (default 2). The honesty knob — see § Honest boundaries above. |
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
