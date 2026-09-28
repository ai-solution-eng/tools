"""Standalone HTML deployment checker for the memory estimator.

Generates one self-contained page (no server, no external assets) meant to
be put up for users: they bring their deployment configuration — engine
launch arguments, GPU shape, HiCache tiering — and the page validates it
with the same deterministic math as ``memory-estimate``:

    weights_per_gpu = weight_bytes / tp           (experts / ep under MoE)
    usable_per_gpu  = vram x mem_fraction - overhead
    gpu_kv_tokens   = pool / kv_bytes_per_token   (per replica x replicas)
    host_kv_tokens  = hicache_bytes / kv_bytes_per_token

The user can paste launch arguments (``sglang serve m --tp-size 4 ...``),
import a seed_catalog.json (file or paste), or fill the controls by hand.
Every configuration is scored by a deterministic checklist (pass / warn /
fail): weights fit, KV pool, context vs pool, mem-fraction headroom,
NVLink topology, HiCache tier sanity, speculative/MTP consistency.

Regenerate with ``memory-estimate --calculator``.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from . import configs as C
from .configs import ModelConfig, count_parameters, default_weight_dtype, load_config_json
from .estimate import _expert_param_share

# Curated at generation time; entries that cannot be resolved (offline,
# renamed) are skipped with a note — the page still builds.
DEFAULT_MODEL_REFS = [
    "deepseek-ai/DeepSeek-V4-Flash-0731",
    "deepseek-ai/DeepSeek-V3",
    "Qwen/Qwen3-8B",
    "Qwen/Qwen3-Next-80B-A3B-Instruct",
    "Qwen/Qwen3.6-27B",
    "Qwen/Qwen3.8-27B",
    "google/gemma-4-31b-it",
    "google/gemma-4-26B-A4B-it",
    "zai-org/GLM-5.2-FP8",
    "zai-org/GLM-5.3-Flash",
]

# Offline fallbacks: shapes verified against the real configs (tests pin the
# math against these), labeled as approximations in the UI.
BUILTIN_FALLBACKS: list[tuple[str, dict]] = [
    (
        "Llama-3-8B (built-in shape)",
        {
            "model_type": "llama",
            "hidden_size": 4096,
            "num_hidden_layers": 32,
            "num_attention_heads": 32,
            "num_key_value_heads": 8,
            "head_dim": 128,
            "vocab_size": 128256,
            "intermediate_size": 14336,
            "torch_dtype": "bfloat16",
        },
    ),
    (
        "DeepSeek-V3 (built-in shape)",
        {
            "model_type": "deepseek_v3",
            "hidden_size": 7168,
            "num_hidden_layers": 61,
            "num_attention_heads": 128,
            "num_key_value_heads": 128,
            "kv_lora_rank": 512,
            "q_lora_rank": 1536,
            "qk_nope_head_dim": 128,
            "qk_rope_head_dim": 64,
            "v_head_dim": 128,
            "vocab_size": 129280,
            "n_routed_experts": 256,
            "moe_intermediate_size": 2048,
            "first_k_dense_replace": 3,
            "num_nextn_predict_layers": 1,
            "quantization_config": {"quant_method": "fp8"},
        },
    ),
]


def model_to_flat(cfg: ModelConfig, name: str, source: str, approximate: bool = False) -> dict:
    """Flatten a ModelConfig to the fields the JS math needs."""
    params, _ = count_parameters(cfg)
    return {
        "name": name,
        "source": source,
        "approximate": approximate,
        "model_type": cfg.model_type,
        "params": params,
        "weight_dtype": default_weight_dtype(cfg) or "",
        "nextn": cfg.num_nextn_predict_layers or 0,
        "is_moe": cfg.is_moe,
        "moe_share": round(_expert_param_share(cfg), 4),
        "hidden_size": cfg.hidden_size,
        "num_hidden_layers": cfg.num_hidden_layers,
        "num_attention_heads": cfg.num_attention_heads,
        "num_key_value_heads": cfg.num_key_value_heads,
        "head_dim": cfg.head_dim,
        "sliding_window": cfg.sliding_window,
        "layer_types": cfg.layer_types if isinstance(cfg.layer_types, list) else None,
        "full_attention_interval": cfg.full_attention_interval,
        "kv_lora_rank": cfg.kv_lora_rank,
        "qk_nope_head_dim": cfg.qk_nope_head_dim,
        "qk_rope_head_dim": cfg.qk_rope_head_dim,
        "v_head_dim": cfg.v_head_dim,
        "linear_key_head_dim": cfg.linear_key_head_dim,
        "linear_value_head_dim": cfg.linear_value_head_dim,
        "linear_num_key_heads": cfg.linear_num_key_heads,
        "linear_num_value_heads": cfg.linear_num_value_heads,
    }


def collect_models(
    extra_refs: list[str] | None = None,
    allow_fetch: bool = True,
) -> tuple[list[dict], list[str]]:
    """Resolve model configs to flat dicts. Returns (models, notes)."""
    notes: list[str] = []
    models: list[dict] = []
    seen: set[str] = set()
    for ref in [*DEFAULT_MODEL_REFS, *(extra_refs or [])]:
        if ref in seen:
            continue
        seen.add(ref)
        try:
            cfg, source = load_config_json(ref)
        except Exception:  # offline/renamed/gated: skip quietly
            notes.append(f"skipped {ref} (config unavailable)")
            continue
        models.append(model_to_flat(cfg, ref.rsplit("/", 1)[-1], source))
    for fname, raw in BUILTIN_FALLBACKS:
        if any(m["name"].lower().startswith(fname.split(" (")[0].lower()) for m in models):
            continue
        models.append(model_to_flat(C.config_from_dict(raw), fname, "built-in shape", approximate=True))
    return models, notes


# ---------------------------------------------------------------------------
# page template — placeholders: __TITLE__, __GENERATED__, __MODELS__
# math mirrors configs.py/estimate.py; MemCalc is exported for tests
# ---------------------------------------------------------------------------

TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
:root{
  --bg:#0d1117; --panel:#161b22; --panel2:#1c2330; --border:#2d333b; --text:#e6edf3;
  --muted:#8b949e; --accent:#58a6ff; --good:#3fb950; --warn:#d29922; --bad:#f85149;
  --code:#0d1117; --chip:#21262d; --host:#a371f7;
}
html[data-theme="light"]{
  --bg:#f6f8fa; --panel:#ffffff; --panel2:#f0f3f7; --border:#d0d7de; --text:#1f2328;
  --muted:#59636e; --accent:#0969da; --good:#1a7f37; --warn:#9a6700; --bad:#cf222e;
  --code:#f6f8fa; --chip:#eaeef2; --host:#8250df;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 -apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
.wrap{max-width:1180px;margin:0 auto;padding:0 18px 60px}
header{padding:18px;border-bottom:1px solid var(--border);display:flex;align-items:flex-start;justify-content:space-between;gap:12px}
header h1{margin:0;font-size:20px}
header .sub{color:var(--muted);font-size:12px;margin-top:4px}
button#btn-theme{background:var(--panel);border:1px solid var(--border);color:var(--text);border-radius:8px;padding:6px 12px;cursor:pointer}
.panel{background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:14px;margin-top:16px}
.panel h2{margin:0 0 10px;font-size:14px;color:var(--muted);text-transform:uppercase;letter-spacing:.4px}
.grid-ctrl{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px}
label{display:flex;flex-direction:column;gap:4px;font-size:12px;color:var(--muted)}
select,input[type=number],input[type=text],textarea{background:var(--panel2);border:1px solid var(--border);color:var(--text);border-radius:6px;padding:6px 8px;font-size:13px}
textarea{width:100%;font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px;min-height:84px;resize:vertical}
input[type=range]{accent-color:var(--accent)}
input[type=checkbox]{accent-color:var(--accent)}
.val{color:var(--text);font-weight:600;font-size:13px}
.ctxs{display:flex;flex-wrap:wrap;gap:8px;margin-top:6px}
.ctxs label{flex-direction:row;align-items:center;gap:5px;color:var(--text);background:var(--chip);border:1px solid var(--border);border-radius:16px;padding:4px 10px;cursor:pointer}
.btn{background:var(--accent);border:1px solid var(--accent);color:#fff;border-radius:6px;padding:6px 14px;cursor:pointer;font-size:13px;font-weight:600}
.btn.secondary{background:var(--panel2);color:var(--text);border-color:var(--border);font-weight:400}
.verdict{border-radius:10px;padding:14px 16px;margin-top:16px;border:1px solid var(--border)}
.verdict.ok{border-color:var(--good);background:color-mix(in srgb,var(--good) 10%,var(--panel))}
.verdict.bad{border-color:var(--bad);background:color-mix(in srgb,var(--bad) 10%,var(--panel))}
.verdict.warn{border-color:var(--warn);background:color-mix(in srgb,var(--warn) 10%,var(--panel))}
.verdict h2{margin:0 0 6px;font-size:16px;color:var(--text);text-transform:none;letter-spacing:0}
.verdict .big{font-size:22px;font-weight:700}
.metrics{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-top:10px}
.metric{background:var(--panel2);border:1px solid var(--border);border-radius:8px;padding:8px 10px}
.metric .k{color:var(--muted);font-size:11px}
.metric .v{font-weight:700;font-size:15px;margin-top:2px}
table{border-collapse:collapse;width:100%;margin-top:8px}
th,td{border:1px solid var(--border);padding:5px 9px;text-align:left;font-size:13px}
th{background:var(--panel2)}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
.bar{display:flex;height:26px;border-radius:6px;overflow:hidden;border:1px solid var(--border);margin-top:8px}
.bar div{display:flex;align-items:center;justify-content:center;font-size:11px;color:#fff;white-space:nowrap;overflow:hidden}
.legend{display:flex;flex-wrap:wrap;gap:14px;margin-top:6px;font-size:12px;color:var(--muted)}
.dot{display:inline-block;width:10px;height:10px;border-radius:3px;margin-right:4px;vertical-align:-1px}
.notes{margin-top:10px;color:var(--muted);font-size:12px}
.notes li{margin-bottom:3px}
.mini{font-size:12px;color:var(--muted)}
h3{margin:18px 0 4px;font-size:14px}
code{background:var(--code);border:1px solid var(--border);border-radius:4px;padding:1px 5px;font-size:12px}
.checks{margin-top:10px}
.check{display:flex;gap:8px;align-items:baseline;padding:4px 0;border-bottom:1px dashed var(--border);font-size:13px}
.check:last-child{border-bottom:none}
.badge{flex:0 0 46px;text-align:center;border-radius:10px;font-size:11px;font-weight:700;padding:1px 0;color:#fff}
.badge.pass{background:var(--good)}
.badge.warn{background:var(--warn)}
.badge.fail{background:var(--bad)}
.check .why{color:var(--muted)}
.import{display:none;flex-direction:column;gap:8px;margin-top:10px}
.import.show{display:flex}
.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
details summary{cursor:pointer;color:var(--accent);font-size:13px}
details.howto{margin-top:10px;color:var(--muted);font-size:12px}
details.howto code{font-size:11px}
</style>
</head>
<body>
<header>
  <div>
    <h1>__TITLE__</h1>
    <div class="sub">Generated __GENERATED__ &middot; validate a deployment before you submit it: does the model fit, how many KV tokens live in GPU memory and HiCache, how many concurrent requests does that support &middot; same math as <code>memory-estimate</code></div>
  </div>
  <button id="btn-theme" title="Toggle light / dark mode">&#x2600;</button>
</header>
<div class="wrap">

<div class="panel">
  <h2>Your deployment</h2>
  <div class="row">
    <button class="btn secondary" id="btn-import">Import a configuration</button>
    <span class="mini">paste launch arguments or a Model-Downloader catalog entry &mdash; or fill the controls below by hand</span>
  </div>
  <div class="import" id="import">
    <textarea id="import-text" placeholder="paste either:&#10;  sglang serve deepseek-ai/DeepSeek-V4-Flash-0731 --tp-size 4 --kv-cache-dtype fp8_e4m3 --mem-fraction-static 0.8 --hicache-ratio 2&#10;or a seed_catalog.json entry / the whole catalog file"></textarea>
    <div class="row">
      <button class="btn" id="btn-apply">Check this configuration</button>
      <input type="file" id="import-file" accept=".json,.txt">
      <span class="mini" id="import-status"></span>
    </div>
  </div>

  <div class="grid-ctrl" style="margin-top:14px">
    <label>Model
      <select id="f-model"></select>
    </label>
    <label>GPU
      <select id="f-gpu"></select>
    </label>
    <label>GPUs <span class="val" id="v-gpus">4</span>
      <input type="range" id="f-gpus" min="1" max="16" step="1" value="4">
    </label>
    <label>Tensor parallel (TP)
      <select id="f-tp"></select>
    </label>
    <label>Expert parallel (EP) <span class="mini" id="ep-note"></span>
      <select id="f-ep">
        <option value="1">none (pure TP)</option>
        <option value="2">EP2</option>
        <option value="4">EP4</option>
        <option value="8">EP8</option>
        <option value="16">EP16</option>
      </select>
    </label>
    <label>Replicas
      <select id="f-replicas">
        <option value="1">1</option><option value="2">2</option><option value="4">4</option>
      </select>
    </label>
    <label>Weights dtype
      <select id="f-wdt"></select>
    </label>
    <label>KV cache dtype
      <select id="f-kvdt">
        <option value="bf16">bf16 (auto)</option>
        <option value="fp8_e4m3">FP8 (e4m3)</option>
      </select>
    </label>
    <label>mem-fraction-static <span class="val" id="v-mf">0.90</span>
      <input type="range" id="f-mf" min="0.5" max="0.98" step="0.01" value="0.9">
    </label>
    <label>Overhead / GPU <span class="val" id="v-oh">2.0 GiB</span>
      <input type="range" id="f-oh" min="0" max="8" step="0.5" value="2">
    </label>
  </div>

  <div class="grid-ctrl" style="margin-top:12px">
    <label>HiCache host tier
      <select id="f-hicache">
        <option value="off">off (GPU only)</option>
        <option value="ratio">ratio &times; GPU pool</option>
        <option value="size">fixed size</option>
      </select>
    </label>
    <label id="hicache-ratio-wrap">HiCache ratio <span class="val" id="v-hcr">2.0&times;</span>
      <input type="range" id="f-hcr" min="1" max="16" step="0.5" value="2">
    </label>
    <label id="hicache-size-wrap">HiCache host RAM <span class="val" id="v-hcs">720 GiB</span>
      <input type="range" id="f-hcs" min="16" max="2048" step="16" value="720">
    </label>
    <label style="flex-direction:row;align-items:center;gap:8px;align-self:end"><input type="checkbox" id="f-mtp"> MTP/speculative draft KV</label>
    <label style="flex-direction:row;align-items:center;gap:8px"><input type="checkbox" id="f-sweep"> sweep TP sizes</label>
  </div>

  <div class="ctxs" id="ctxs"></div>
  <div class="custom" id="custom" style="display:none">
    <div class="grid-ctrl" style="margin-top:10px">
      <label>Name<input type="text" id="c-name" value="My model"></label>
      <label>Params (B)<input type="number" id="c-params" value="8"></label>
      <label>Layers<input type="number" id="c-layers" value="32"></label>
      <label>KV heads<input type="number" id="c-kv_heads" value="8"></label>
      <label>Head dim<input type="number" id="c-head_dim" value="128"></label>
      <label>kv_lora_rank (MLA)<input type="number" id="c-kv_lora" value=""></label>
      <label>qk_rope_head_dim<input type="number" id="c-qk_rope" value=""></label>
      <label>Linear-hybrid interval<input type="number" id="c-interval" value=""></label>
      <label>Sliding window<input type="number" id="c-sliding" value=""></label>
      <label>MTP layers<input type="number" id="c-nextn" value="0"></label>
      <label>Style
        <select id="c-style"><option value="gqa">GQA/MHA</option><option value="mla">MLA</option><option value="sparse">MQA + sparse</option></select>
      </label>
    </div>
  </div>
  <div class="mini" id="model-info" style="margin-top:10px"></div>

  <details class="howto">
    <summary>How this is computed</summary>
    <div style="margin-top:6px">
      <code>weights/GPU = params &times; dtype_bytes &divide; TP</code> (routed experts &divide; EP for MoE) &nbsp;&rarr;&nbsp;
      <code>usable/GPU = VRAM &times; mem-fraction &minus; overhead</code> &nbsp;&rarr;&nbsp;
      <code>GPU KV tokens = (usable &minus; weights/GPU) &divide; KV bytes/token</code>, &times; replicas &nbsp;&rarr;&nbsp;
      <code>HiCache tokens = host RAM &divide; KV bytes/token</code>.<br>
      KV bytes/token comes from the attention structure in the model's <code>config.json</code>:
      <code>2 &times; kv_heads &times; head_dim</code> (GQA), <code>kv_lora_rank + qk_rope_head_dim</code> (MLA), one MQA head for
      sparse-MQA; linear-attention layers carry no per-token KV; sliding-window layers are budgeted conservatively.
      The HiCache tier holds the same KV dtype in host RAM: it multiplies <i>addressable</i> tokens, while the
      hot working set that serves requests without PCIe traffic stays the GPU pool.
    </div>
  </details>
</div>

<div id="out"></div>

</div>
<script>
const G = (typeof window !== 'undefined') ? window : globalThis;
const GPU_DB = [
  {k:['h200 pcie','h200 pcie lx'], name:'NVIDIA H200 PCIe', vram:141},
  {k:['h200 sxm','h200 (sxm)','h200'], name:'NVIDIA H200 SXM', vram:141},
  {k:['rtx pro 6000','rtxpro6000','rtx 6000 pro'], name:'NVIDIA RTX PRO 6000 Blackwell', vram:96},
  {k:['b200'], name:'NVIDIA B200', vram:192},
  {k:['h100 pcie','h100 nvl'], name:'NVIDIA H100 PCIe', vram:80},
  {k:['h100 sxm','h100 (sxm)','h100'], name:'NVIDIA H100 SXM', vram:80},
  {k:['l40s','l40'], name:'NVIDIA L40S', vram:48},
  {k:['a100'], name:'NVIDIA A100 80GB', vram:80},
  {k:['mi325x'], name:'AMD MI325X', vram:256},
  {k:['mi300x'], name:'AMD MI300X', vram:192},
];
const MODELS = __MODELS__;
const WB = {fp64:8,fp32:4,tf32:4,bf16:2,fp16:2,fp8:1,fp8_e4m3:1,fp8_e5m2:1,int8:1,nvfp4:0.5,fp4:0.5,int4:0.5,awq:0.5,gptq:0.5};
const KB = {bf16:2,fp16:2,fp8:1,fp8_e4m3:1,fp8_e5m2:1};
const GIB = 1024**3;
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmtGiB = (b) => (b/GIB).toFixed(1) + ' GiB';
const fmtTok = (t) => t >= 1e6 ? (t/1e6).toFixed(2) + 'M' : Math.round(t).toLocaleString('en-US');

function resolveGpu(low){
  low = String(low||'').toLowerCase();
  for (const g of GPU_DB) for (const k of g.k) if (low.includes(k)) return g;
  return GPU_DB[1];
}
function normDtype(d){ d = String(d||'').toLowerCase().replace('float8','fp8').replace('bfloat16','bf16').replace('float16','fp16'); return WB[d]!==undefined || KB[d]!==undefined ? d : ''; }

// ---- structure math (mirrors memory_model/configs.py) ----
const LIN = new Set(['linear_attention','gated_deltanet','gated_delta_net','mamba','mamba2']);
function layerTypes(m){
  const n = m.num_hidden_layers || 0;
  if (!n) return [];
  let lt = m.layer_types;
  if (Array.isArray(lt) && lt.length){
    const names = lt.map(x => typeof x === 'object' ? String(x.layer_type || x.type || 'full_attention') : String(x));
    return Array.from({length:n}, (_,i) => names[i % names.length]);
  }
  if (m.full_attention_interval){
    const iv = m.full_attention_interval;
    return Array.from({length:n}, (_,i) => (i % iv) === iv-1 ? 'full_attention' : 'linear_attention');
  }
  if (m.sliding_window) return Array.from({length:n}, () => 'sliding_attention');
  return Array.from({length:n}, () => 'full_attention');
}
function layerMix(m){
  const mix = {full:0, sliding:0, linear:0};
  for (const t of layerTypes(m)){
    const low = t.toLowerCase();
    if (LIN.has(low)) mix.linear++;
    else if (low.includes('sliding')) mix.sliding++;
    else mix.full++;
  }
  return mix;
}
function kvPerLayer(m, kvB){
  if (String(m.model_type).startsWith('deepseek_v2') || String(m.model_type).startsWith('deepseek_v3') || String(m.model_type).includes('dsa')){
    return {bytes:(m.kv_lora_rank + m.qk_rope_head_dim) * kvB, arch:'MLA' + (String(m.model_type).includes('dsa') ? ' + sparse (DSA)' : ''),
            formula:`(kv_lora_rank ${m.kv_lora_rank} + qk_rope_head_dim ${m.qk_rope_head_dim}) x ${kvB} B`};
  }
  if (String(m.model_type).startsWith('deepseek_v4')){
    const rope = (m.head_dim && m.qk_rope_head_dim && m.head_dim < 256) ? m.qk_rope_head_dim : 0;
    return {bytes:(m.head_dim + rope) * Math.max(m.num_key_value_heads||1,1) * kvB, arch:'MQA + sparse (CSA/HCA)',
            formula:`(head_dim ${m.head_dim} + rope ${rope}) x kv_heads ${m.num_key_value_heads||1} x ${kvB} B`};
  }
  const kvH = m.num_key_value_heads ?? m.num_attention_heads;
  const hd = m.head_dim || (m.hidden_size / m.num_attention_heads);
  return {bytes:2 * kvH * hd * kvB, arch:(kvH !== m.num_attention_heads ? 'GQA' : 'MHA') + (layerMix(m).sliding ? ' + sliding-window' : ''),
          formula:`2 (K+V) x kv_heads ${kvH} x head_dim ${hd} x ${kvB} B`};
}
function kvBytesPerToken(m, kvDtype){
  const kvB = KB[normDtype(kvDtype)] ?? 2;
  const mix = layerMix(m);
  const pl = kvPerLayer(m, kvB);
  let bpt = pl.bytes * (mix.full + mix.sliding);
  const notes = [];
  if (mix.sliding) notes.push('sliding-window layers budgeted at full per-token cost (conservative; engine window reuse only frees memory)');
  if (mix.linear){
    notes.push(`${mix.linear} linear-attention layers carry no per-token KV (recurrent state only)`);
    if (m.linear_num_value_heads && m.linear_value_head_dim && m.linear_key_head_dim)
      notes.push(`linear-state pool ≈ ${(m.linear_num_value_heads * m.linear_value_head_dim * m.linear_key_head_dim * (KB[normDtype(kvDtype)] ?? 2) / GIB).toFixed(2)} GiB per active sequence`);
  }
  return {bpt, kvB, mix, arch:pl.arch, formula:pl.formula, notes};
}
// ---- capacity math (mirrors memory_model/estimate.py) ----
function estimate(m, o){
  const kv = kvBytesPerToken(m, o.kvDtype);
  let bpt = kv.bpt;
  if (o.mtp && m.nextn) { bpt += kvPerLayer(m, kv.kvB).bytes * Math.min(o.mtpLayers||1, m.nextn); kv.notes.push('MTP draft layer KV added'); }
  const gpu = resolveGpu(o.gpu);
  const vram = gpu.vram * GIB;
  const usable = vram * o.mf - o.overhead * GIB;
  const wEff = normDtype(o.wDtype) || normDtype(m.weight_dtype) || 'bf16';  // 'auto' = config dtype
  const wBytes = (m.params || 0) * (WB[wEff] ?? 2);
  let wPerGpu = wBytes / o.tp;
  const ep = (o.ep > 1 && o.ep <= o.gpus && m.is_moe) ? o.ep : 1;
  if (ep > 1){
    wPerGpu = wBytes * m.moe_share / ep + wBytes * (1 - m.moe_share) / o.tp;
    kv.notes.push(`expert parallel EP${ep}: routed experts (~${Math.round(m.moe_share*100)}% of weights) sharded ${ep}-ways, attention TP${o.tp}`);
  }
  const pool = usable - wPerGpu;
  const replicas = o.replicas || Math.max(1, Math.floor(o.gpus / o.tp));
  const fits = wBytes > 0 && pool >= 0;
  const gpuTokens = fits ? pool / bpt : 0;
  const gpuTokensTotal = gpuTokens * replicas;
  // HiCache host tier: same KV dtype in host RAM
  let hostGib = 0;
  if (o.hicache === 'ratio') hostGib = (pool/GIB) * o.hicacheRatio;
  else if (o.hicache === 'size') hostGib = o.hicacheSize;
  const hostTokens = fits && hostGib > 0 ? hostGib * GIB / bpt * replicas : 0;
  const hostBytes = hostGib * GIB;
  const totalTokens = gpuTokensTotal + hostTokens;
  const conc = (ctx, t) => Math.floor((t === undefined ? gpuTokensTotal : t) / ctx);
  return {gpu, gpuName:gpu.name, vram, usable, wBytes, wPerGpu, pool, gpuTokens, gpuTokensTotal,
          hostGib, hostBytes, hostTokens, totalTokens, fits, replicas, ep, conc,
          kvBpt:bpt, arch:kv.arch, formula:kv.formula, notes:kv.notes,
          unknown:!m.params};
}

// ---- deterministic configuration checklist ----
function buildChecks(r, m, o){
  const c = [];
  const add = (level, text, why) => c.push({level, text, why});
  if (r.unknown){
    add('warn', 'Model size unknown', 'the embedded shape has no parameter count — pick a known model or fill the custom form');
  } else if (!r.fits){
    add('fail', 'Weights do not fit per GPU', `weights need ${fmtGiB(r.wPerGpu)} per GPU but only ${fmtGiB(r.usable)} is usable at mem-fraction ${o.mf} — raise TP${m.is_moe ? '/EP' : ''} or GPU count`);
  } else {
    add('pass', 'Weights fit per GPU', `${fmtGiB(r.wPerGpu)} of ${fmtGiB(r.usable)} usable (mem-fraction ${o.mf})`);
  }
  if (r.fits && r.pool / GIB < 4){
    add('warn', 'Very small KV pool', `only ${fmtGiB(r.pool)} per GPU left for KV — capacity will be tiny; adjust TP/EP/dtype`);
  } else if (r.fits){
    add('pass', 'KV pool positive', `${fmtGiB(r.pool)} per GPU → ${fmtTok(r.gpuTokens)} tokens/replica`);
  }
  const maxCtx = Math.max(...o.ctxs);
  if (r.fits && r.gpuTokensTotal < maxCtx){
    add('fail', 'One request cannot fit', `the largest selected context (${maxCtx/1024}k) exceeds the GPU pool (${fmtTok(r.gpuTokensTotal)} tokens) — a single request would not decode`);
  } else if (r.fits){
    add('pass', `Largest context (${maxCtx/1024}k) fits`, `${r.conc(maxCtx)} concurrent request(s) without HiCache`);
  }
  if (o.mf >= 0.95) add('warn', 'mem-fraction-static very high', `at ${o.mf} CUDA graphs / activation spikes may OOM at runtime; PCAI catalogs typically use 0.8–0.92`);
  else if (o.mf < 0.7) add('warn', 'mem-fraction-static low', `at ${o.mf} a lot of VRAM stays unused — capacity is being left on the table`);
  else add('pass', 'mem-fraction-static in the normal band', `${o.mf} (PCAI catalogs use 0.8–0.92)`);
  if (o.tp > o.gpus) add('fail', 'TP exceeds GPU count', `TP${o.tp} on ${o.gpus} GPU(s) is not schedulable`);
  else add('pass', 'TP schedulable', `TP${o.tp} on ${o.gpus} GPU(s)${r.replicas > 1 ? ` → ${r.replicas} replicas` : ''}`);
  if (o.ep > 1 && !m.is_moe) add('warn', 'EP set on a dense model', 'expert parallelism only shards MoE experts — ignored here');
  if (m.is_moe && o.ep === 1 && o.tp > 4 && r.wPerGpu / r.vram > 0.8)
    add('warn', 'MoE without EP', `at TP${o.tp} weights take ${fmtGiB(r.wPerGpu)}/GPU — sharding experts with EP (e.g. EP${Math.min(8, o.gpus)}) usually fits larger MoE models`);
  if (o.tp > 4 && o.gpus >= 8) add('warn', 'TP crosses NVLink groups', `8-GPU PCAI boxes are 2 groups of 4 — TP${o.tp} all-reduces across PCIe; prefer TP4 + EP/replicas for MoE, or accept the bandwidth cost`);
  if (m.nextn && !o.mtp) add('warn', 'Checkpoint ships MTP layers', `${m.nextn} MTP layer(s) in the checkpoint but speculative decoding is off — tick the box if the deployment enables it`);
  if (o.mtp && !m.nextn && !m.approximate) add('warn', 'MTP requested but checkpoint has none', 'draft KV will be small but this checkpoint does not ship MTP layers');
  if (o.hicache !== 'off'){
    if (r.hostGib < (r.pool/GIB)) add('warn', 'HiCache tier smaller than GPU pool', `${r.hostGib.toFixed(1)} GiB host RAM vs ${fmtGiB(r.pool)} GPU pool per replica — the tier will thrash on large working sets`);
    else add('pass', 'HiCache tier sized', `${r.hostGib.toFixed(1)} GiB host RAM per replica → ${fmtTok(r.hostTokens/r.replicas)} extra tokens/replica (same KV dtype)`);
  }
  return c;
}

// ---- launch-args / catalog import ----
function tokenize(s){
  const out = []; const re = /"([^"]*)"|'([^']*)'|(\\S+)/g; let mt;
  while ((mt = re.exec(s))) out.push(mt[1] ?? mt[2] ?? mt[3]);
  return out;
}
function applyLaunchArgs(arr, noteEl){
  const flag = (name) => { const i = arr.indexOf(name); return i >= 0 && i+1 < arr.length ? arr[i+1] : null; };
  let applied = [];
  const si = arr.indexOf('serve');
  if (si >= 0 && arr[si+1] && arr[si+1].includes('/')){
    const ref = arr[si+1];
    const short = ref.split('/').pop();
    const idx = MODELS.findIndex(m => m.name.toLowerCase() === short.toLowerCase());
    if (idx >= 0){ el('f-model').value = String(idx); syncModel(); applied.push('model ' + ref); }
    else if (noteEl){ noteEl.textContent = `model ${ref} is not in the embedded set — pick the closest or use the custom form`; }
  }
  const tp = flag('--tp-size'); if (tp && +tp > 0){ el('f-tp').value = String(Math.min(+tp, 16)); applied.push('--tp-size ' + tp); }
  const mf = flag('--mem-fraction-static'); if (mf && +mf > 0 && +mf <= 1){ el('f-mf').value = String(+mf); applied.push('--mem-fraction-static ' + mf); }
  const kv = flag('--kv-cache-dtype'); if (kv){ const n = normDtype(kv); if (n){ el('f-kvdt').value = n; applied.push('--kv-cache-dtype ' + kv); } }
  const spec = flag('--speculative-algorithm');
  if (spec){ const m = MODELS[+el('f-model').value]; if (m && m.nextn){ el('f-mtp').checked = true; applied.push('--speculative-algorithm ' + spec); } }
  const hcr = flag('--hicache-ratio'); if (hcr && +hcr > 0){ el('f-hicache').value = 'ratio'; el('f-hcr').value = String(+hcr); applied.push('--hicache-ratio ' + hcr); }
  const hcs = flag('--hicache-size'); if (hcs && +hcs > 0){ el('f-hicache').value = 'size'; el('f-hcs').value = String(Math.min(2048, Math.round(+hcs))); applied.push('--hicache-size ' + hcs); }
  return applied;
}
function applyCatalog(entry, noteEl){
  el('f-gpus').value = String(Math.min(16, Math.max(1, +entry.resource_request_gpu || 1)));
  el('v-gpus').textContent = el('f-gpus').value;
  fillTp();
  const applied = applyLaunchArgs(entry.arguments || [], noteEl);
  applied.unshift(`catalog entry ${entry.catalog_id || entry.name || '?'}`);
  return applied;
}
function handleImport(){
  const text = el('import-text').value.trim();
  const noteEl = el('import-status');
  if (!text){ noteEl.textContent = 'nothing to import'; return; }
  let applied = [];
  try {
    const j = JSON.parse(text);
    const entry = Array.isArray(j) ? (j.find(e => String(e.name||'').toLowerCase().includes('deepseek')) || j[0]) : j;
    applied = applyCatalog(entry, noteEl);
  } catch (e) {
    applied = applyLaunchArgs(tokenize(text), noteEl);
  }
  noteEl.textContent = applied.length ? 'applied: ' + applied.join(' · ') : 'no recognizable settings found';
  render();
}

// ---- page wiring ----
function el(id){ return document.getElementById(id); }
function state(){
  const i = el('f-model').value;
  return {
    model: i === '__custom__' ? null : MODELS[i],
    gpu: el('f-gpu').value,
    gpus: +el('f-gpus').value,
    tp: +el('f-tp').value,
    ep: +el('f-ep').value,
    replicas: +el('f-replicas').value,
    wDtype: el('f-wdt').value,
    kvDtype: el('f-kvdt').value,
    mf: +el('f-mf').value,
    overhead: +el('f-oh').value,
    sweep: el('f-sweep').checked,
    mtp: el('f-mtp').checked && !el('f-mtp').disabled,
    mtpLayers: 1,
    hicache: el('f-hicache').value,
    hicacheRatio: +el('f-hcr').value,
    hicacheSize: +el('f-hcs').value,
    ctxs: [...document.querySelectorAll('#ctxs input:checked')].map(c => +c.value),
  };
}
function fillStatic(){
  el('f-gpu').innerHTML = GPU_DB.map(g => `<option value="${esc(g.name)}">${esc(g.name)} — ${g.vram} GiB</option>`).join('');
  el('f-gpu').value = 'NVIDIA H200 PCIe';
  el('f-gpus').value = '4';
  el('v-gpus').textContent = '4';
  el('f-model').innerHTML = MODELS.map((m,i) => `<option value="${i}">${esc(m.name)}${m.approximate ? ' (built-in shape)' : ''}</option>`).join('') + '<option value="__custom__">Custom model…</option>';
  el('f-model').value = '0';
  el('f-wdt').innerHTML = ['auto','bf16','fp8','nvfp4','int8'].map(d => `<option value="${d}">${d === 'auto' ? 'auto (from config)' : d}</option>`).join('');
  const ctxDefs = [4096,16384,65536,262144,1048576];
  el('ctxs').innerHTML = ctxDefs.map(c => `<label><input type="checkbox" value="${c}" checked> ${c/1024}k</label>`).join('');
}
function tpOptions(n){
  const out = [];
  for (const t of [1,2,4,8,16]) if (t <= n) out.push(t);
  return out;
}
function syncModel(){
  const i = el('f-model').value || '0';
  const custom = i === '__custom__';
  el('custom').style.display = custom ? '' : 'none';
  if (custom){ el('model-info').textContent = 'Custom: fill the shape fields (KV math needs the style + dims).'; fillTp(); return; }
  const m = MODELS[i] || MODELS[0];
  el('f-wdt').value = 'auto';
  el('f-mtp').disabled = !m.nextn;
  el('f-ep').disabled = !m.is_moe;
  if (!m.is_moe) el('f-ep').value = '1';
  el('ep-note').textContent = m.is_moe ? '' : '(MoE only)';
  el('model-info').innerHTML = `${esc(m.model_type || '?')} · ${(m.params/1e9).toFixed(1)}B params · weights dtype ${esc(m.weight_dtype || 'unknown')} · <code>${esc(m.source || '')}</code>${m.approximate ? ' · <b>built-in approximation</b>' : ''}`;
  fillTp();
}
function fillTp(){
  const n = +el('f-gpus').value;
  const opts = tpOptions(n);
  const cur = +el('f-tp').value || n;
  el('f-tp').innerHTML = opts.map(t => `<option value="${t}">TP${t}${t<n ? ` (${n/t} replica${n/t>1?'s':''})` : ''}</option>`).join('');
  el('f-tp').value = opts.includes(cur) ? cur : n;
}
function modelFromCustom(){
  const num = (id) => { const v = el(id).value; return v === '' ? null : +v; };
  const style = el('c-style').value;
  return {
    name: el('c-name').value || 'Custom model', source: 'manual entry', approximate: true,
    model_type: style === 'mla' ? 'deepseek_v3' : (style === 'sparse' ? 'deepseek_v4' : 'llama'),
    params: (num('c-params') || 0) * 1e9,
    weight_dtype: '', nextn: num('c-nextn') || 0, is_moe: false, moe_share: 0,
    hidden_size: 0, num_hidden_layers: num('c-layers') || 0,
    num_attention_heads: num('c-kv_heads') || 1, num_key_value_heads: num('c-kv_heads'),
    head_dim: num('c-head_dim'),
    kv_lora_rank: num('c-kv_lora'), qk_rope_head_dim: num('c-qk_rope'),
    sliding_window: num('c-sliding'), full_attention_interval: num('c-interval'),
    layer_types: null,
  };
}

function checksHtml(checks){
  return '<div class="checks">' + checks.map(c =>
    `<div class="check"><span class="badge ${c.level}">${c.level === 'pass' ? 'PASS' : c.level === 'warn' ? 'WARN' : 'FAIL'}</span>` +
    `<span>${esc(c.text)}${c.why ? ` <span class="why">— ${esc(c.why)}</span>` : ''}</span></div>`).join('') + '</div>';
}
function barHtml(r, o){
  const v = r.vram;
  if (!r.fits) return `<div class="bar"><div style="width:${Math.min(100, r.wPerGpu/v*100).toFixed(1)}%;background:var(--bad)" title="weights">weights ${esc(fmtGiB(r.wPerGpu))} / ${esc(fmtGiB(v))}</div></div>`;
  const wPct = r.wPerGpu/v*100, kvPct = r.pool/v*100, ovhPct = o.overhead*GIB/v*100;
  return `<div class="bar">` +
    `<div style="width:${wPct.toFixed(1)}%;background:var(--accent)" title="weights">${esc(fmtGiB(r.wPerGpu))}</div>` +
    `<div style="width:${kvPct.toFixed(1)}%;background:var(--good)" title="KV pool">KV ${esc(fmtGiB(r.pool))}</div>` +
    `<div style="width:${ovhPct.toFixed(1)}%;background:var(--warn)" title="overhead">ovh</div>` +
    `<div style="width:${Math.max(0,100-wPct-kvPct-ovhPct).toFixed(1)}%;background:var(--chip);color:var(--muted)"></div>` +
    `</div>
    <div class="legend"><span><span class="dot" style="background:var(--accent)"></span>weights</span><span><span class="dot" style="background:var(--good)"></span>GPU KV pool</span><span><span class="dot" style="background:var(--warn)"></span>overhead</span><span><span class="dot" style="background:var(--chip)"></span>unused</span>${r.hostTokens ? `<span><span class="dot" style="background:var(--host)"></span>HiCache host tier (not in bar)</span>` : ''}</div>`;
}
function cardHtml(r, m, o){
  const cls = r.unknown ? 'warn' : (r.fits ? 'ok' : 'bad');
  const head = r.unknown ? 'Cannot verify' : (r.fits ? 'Configuration fits' : 'Does not fit');
  const maxCtx = Math.max(...o.ctxs);
  let headline;
  if (r.unknown) headline = 'pick a known model or fill the custom form';
  else if (!r.fits) headline = `weights need ${esc(fmtGiB(r.wPerGpu))}/GPU > ${esc(fmtGiB(r.usable))} usable`;
  else headline = `<span class="big">${esc(fmtTok(r.gpuTokensTotal))}</span> KV tokens in GPU memory` +
    (r.hostTokens ? ` + <span class="big">${esc(fmtTok(r.hostTokens))}</span> in HiCache = <span class="big">${esc(fmtTok(r.totalTokens))}</span> addressable` : '') +
    ` · <span class="big">${r.conc(maxCtx).toLocaleString('en-US')}</span> concurrent x ${maxCtx/1024}k`;
  let html = `<div class="verdict ${cls}"><h2>${esc(r.gpuName)} ×${o.gpus} · TP${o.tp}` +
    (r.ep > 1 ? ` · EP${r.ep}` : '') + (r.replicas > 1 ? ` · ${r.replicas} replicas` : '') + ` · ${esc(r.arch)}</h2>
    <div>${head} — ${headline}</div>
    <div class="metrics">
      <div class="metric"><div class="k">weights / GPU</div><div class="v">${esc(fmtGiB(r.wPerGpu))}</div></div>
      <div class="metric"><div class="k">GPU KV tokens</div><div class="v">${r.fits ? esc(fmtTok(r.gpuTokensTotal)) : '—'}</div></div>
      <div class="metric"><div class="k">HiCache tokens</div><div class="v">${r.hostTokens ? esc(fmtTok(r.hostTokens)) : '—'}</div></div>
      <div class="metric"><div class="k">addressable total</div><div class="v">${r.fits ? esc(fmtTok(r.totalTokens)) : '—'}</div></div>
      <div class="metric"><div class="k">KV bytes / token</div><div class="v">${(r.kvBpt/1024).toFixed(1)} KiB</div></div>
      <div class="metric"><div class="k">KV formula</div><div class="v" style="font-weight:400;font-size:12px">${esc(r.formula)}</div></div>
    </div>` + barHtml(r, o);
  if (r.fits){
    html += `<h3>Concurrent requests by context</h3><table><tr><th>context</th>` + o.ctxs.map(c=>`<th class="num">${c/1024}k</th>`).join('') + `</tr>
      <tr><td>GPU-resident (hot)</td>` + o.ctxs.map(c=>`<td class="num">${r.conc(c).toLocaleString('en-US')}</td>`).join('') + `</tr>` +
      (r.hostTokens ? `<tr><td>with HiCache tier</td>` + o.ctxs.map(c=>`<td class="num">${r.conc(c, r.totalTokens).toLocaleString('en-US')}</td>`).join('') + `</tr>` : '') +
      `</table>`;
  }
  const checks = buildChecks(r, m, o);
  html += checksHtml(checks);
  const fails = checks.filter(c => c.level === 'fail').length;
  const warns = checks.filter(c => c.level === 'warn').length;
  html += `<div class="mini" style="margin-top:8px">${fails ? `${fails} blocking issue(s) · ` : ''}${warns ? `${warns} warning(s) · ` : ''}${!fails && !warns ? 'all checks passed' : ''}</div>`;
  const notes = [...r.notes];
  if (m.nextn && !o.mtp && !m.approximate) notes.push(`checkpoint ships ${m.nextn} MTP layer(s) — tick "MTP/speculative" if the deployment enables speculative decoding`);
  if (notes.length) html += `<ul class="notes">` + notes.map(n=>`<li>${esc(n)}</li>`).join('') + `</ul>`;
  return html + `</div>`;
}

function render(){
  const s = state();
  el('v-gpus').textContent = s.gpus;
  el('v-mf').textContent = s.mf.toFixed(2);
  el('v-oh').textContent = s.overhead.toFixed(1) + ' GiB';
  el('v-hcr').textContent = s.hicacheRatio.toFixed(1) + '×';
  el('v-hcs').textContent = s.hicacheSize + ' GiB';
  el('hicache-ratio-wrap').style.opacity = s.hicache === 'ratio' ? '1' : '.4';
  el('hicache-size-wrap').style.opacity = s.hicache === 'size' ? '1' : '.4';
  const m = s.model || modelFromCustom();
  const ctxs = s.ctxs.length ? s.ctxs : [32768];
  const tps = s.sweep ? tpOptions(s.gpus) : [s.tp];
  el('out').innerHTML = tps.map(tp => cardHtml(estimate(m, {...s, tp}), m, {...s, ctxs})).join('');
}

function wire(){
  for (const id of ['f-model','f-gpu','f-gpus','f-tp','f-ep','f-replicas','f-wdt','f-kvdt','f-mf','f-oh','f-sweep','f-mtp','f-hicache','f-hcr','f-hcs'])
    el(id).addEventListener('input', () => { if (id==='f-model') syncModel(); if (id==='f-gpus') fillTp(); render(); });
  for (const id of ['c-name','c-params','c-layers','c-kv_heads','c-head_dim','c-kv_lora','c-qk_rope','c-interval','c-sliding','c-nextn','c-style'])
    el(id).addEventListener('input', render);
  el('btn-import').addEventListener('click', () => el('import').classList.toggle('show'));
  el('btn-apply').addEventListener('click', handleImport);
  el('import-file').addEventListener('change', (ev) => {
    const f = ev.target.files && ev.target.files[0];
    if (!f) return;
    f.text().then(t => { el('import-text').value = t; handleImport(); });
  });
}
// ---- light / dark ----
function applyTheme(t){
  document.documentElement.setAttribute('data-theme', t);
  el('btn-theme').innerHTML = t==='light' ? '&#x1F319;' : '&#x2600;';
}
function initTheme(){
  let t; try { t = localStorage.getItem('memcalc-theme'); } catch(e){}
  if (!t) t = (window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches) ? 'light' : 'dark';
  applyTheme(t);
  el('btn-theme').addEventListener('click', () => {
    const next = document.documentElement.getAttribute('data-theme')==='light' ? 'dark' : 'light';
    applyTheme(next); try { localStorage.setItem('memcalc-theme', next); } catch(e){}
  });
}

G.MemCalc = {GPU_DB, MODELS, resolveGpu, kvPerLayer, kvBytesPerToken, layerMix, estimate, normDtype,
             buildChecks, tokenize, applyLaunchArgs, applyCatalog, handleImport};

if (typeof document !== 'undefined' && document.getElementById) {
  fillStatic();
  initTheme();
  syncModel();
  wire();
  render();
}
</script>
</body>
</html>
"""


def build_calculator_html(models: list[dict], title: str = "PCAI deployment memory check") -> str:
    return (
        TEMPLATE.replace("__TITLE__", title)
        .replace("__GENERATED__", datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"))
        .replace("__MODELS__", json.dumps(models).replace("</", "<\\/"))
    )


def default_output_path() -> Path:
    cwd = Path.cwd()
    return cwd / "results" / "memory_check.html" if (cwd / "results").is_dir() else cwd / "memory_check.html"


def write_calculator(
    output: Path | None = None,
    extra_refs: list[str] | None = None,
    allow_fetch: bool = True,
    title: str = "PCAI deployment memory check",
) -> tuple[Path, list[str]]:
    """Build and write the page. Returns (path, notes)."""
    models, notes = collect_models(extra_refs=extra_refs, allow_fetch=allow_fetch)
    out = output or default_output_path()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(build_calculator_html(models, title), encoding="utf-8")
    return out, notes


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="memory-calculator",
        description="Generate the standalone HTML deployment memory-check page.",
    )
    ap.add_argument("--output", type=Path, help="page path (default: results/memory_check.html)")
    ap.add_argument(
        "--add-model",
        action="append",
        default=[],
        metavar="REF",
        help="extra HF repo id / model dir to embed (repeatable)",
    )
    ap.add_argument("--no-fetch", action="store_true", help="do not touch the network (cache/built-ins only)")
    ap.add_argument("--title", default="PCAI deployment memory check")
    args = ap.parse_args(argv)
    out, notes = write_calculator(
        output=args.output, extra_refs=args.add_model, allow_fetch=not args.no_fetch, title=args.title
    )
    ok = sum(1 for m in json.loads(out.read_text().split("const MODELS = ")[1].split(";\n")[0]) if m.get("params"))
    print(f"Memory-check page written to {out}")
    print(f"  models embedded: {ok} (with params) / {len(DEFAULT_MODEL_REFS) + len(args.add_model)} requested")
    for n in notes:
        print(f"  note: {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
