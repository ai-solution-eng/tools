"""Model structure for the memory estimator: config loading + per-family
attention math (GQA / MLA / MQA-sparse / sliding-window / linear-attention
hybrids) and parameter counting.

Zero dependencies: the model's ``config.json`` carries everything — read it
from a local model directory (or let huggingface_hub fetch it from a repo id
when available). The seed deployment catalog is NOT a structure source: it
carries serving args (tp / kv dtype / mem-fraction), not architecture fields.

The per-token KV figures are the *conservative* footprint (every token cached
in every layer, as paged pools allocate); engine-side tricks that only free
memory (sliding-window reuse, sparse selection reading less) make real
capacity better than this estimate, never worse. Calibration against a live
server's reported ``max_total_num_tokens`` (phase 2) closes the loop.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from .dtypes import kv_bytes_per_elem, normalize_dtype, weight_bytes_per_param

# ---------------------------------------------------------------------------
# Model config
# ---------------------------------------------------------------------------


@dataclass
class ModelConfig:
    """The subset of HF ``config.json`` the memory math needs.

    Built by :func:`config_from_dict`; unknown families simply leave the
    family-specific fields at their defaults and fall back to the dense-GQA
    math with a warning.
    """

    model_type: str = ""
    architectures: list[str] = field(default_factory=list)

    # core transformer shape
    hidden_size: int = 0
    num_hidden_layers: int = 0
    num_attention_heads: int = 0
    num_key_value_heads: int | None = None
    head_dim: int | None = None
    vocab_size: int = 0
    tie_word_embeddings: bool = False

    # sliding-window / hybrid layer schedules
    sliding_window: int | None = None
    layer_types: list | None = None
    full_attention_interval: int | None = None

    # MLA (DeepSeek-V2/V3 family)
    kv_lora_rank: int | None = None
    q_lora_rank: int | None = None
    qk_nope_head_dim: int | None = None
    qk_rope_head_dim: int | None = None
    v_head_dim: int | None = None

    # sparse attention (DeepSeek-V4 CSA/HCA, GLM DSA)
    index_topk: int | None = None
    compress_rates: dict | None = None
    index_n_heads: int | None = None
    index_head_dim: int | None = None

    # linear attention (Qwen3-Next gated deltanet and friends)
    linear_key_head_dim: int | None = None
    linear_value_head_dim: int | None = None
    linear_num_key_heads: int | None = None
    linear_num_value_heads: int | None = None
    conv_kernel: int | None = None

    # MoE
    n_routed_experts: int | None = None
    num_experts_per_tok: int | None = None
    moe_intermediate_size: int | None = None
    intermediate_size: int | None = None
    shared_expert_intermediate_size: int | None = None
    n_shared_experts: int | None = None
    first_k_dense_replace: int | None = None
    mlp_only_layers: list | None = None
    mlp_layer_types: list | None = None
    num_local_experts: int | None = None  # attribute_map alias for MoE configs

    # multi-token prediction (extra KV layers when speculative decoding on)
    num_nextn_predict_layers: int | None = None

    # weights quantization as shipped (quantization_config in config.json)
    quant_method: str | None = None
    quant_bits: int | None = None

    raw: dict = field(default_factory=dict)

    @property
    def is_moe(self) -> bool:
        experts = self.n_routed_experts or self.num_local_experts
        return bool(experts and experts > 1)

    @property
    def experts(self) -> int | None:
        return self.n_routed_experts or self.num_local_experts

    @property
    def is_hybrid_default_pattern(self) -> bool:
        """True when the config implies a linear/full pattern without an
        explicit per-layer schedule (e.g. qwen3_next full_attention_interval)."""
        return self.full_attention_interval is not None and not self.layer_types


# ---------------------------------------------------------------------------
# config.json loading
# ---------------------------------------------------------------------------


def config_from_dict(d: dict) -> ModelConfig:
    """Build a :class:`ModelConfig` from a raw ``config.json`` dict.

    Multimodal checkpoints (VL / omni) nest the text stack under
    ``text_config``; when the core fields are absent at top level, that
    sub-dict is unwrapped and used (vision towers stay excluded — a note
    says so at weight-count time).
    """
    core = ("hidden_size", "num_hidden_layers")
    if not any(k in d for k in core) and isinstance(d.get("text_config"), dict):
        merged = dict(d["text_config"])
        merged.update({k: v for k, v in d.items() if k not in ("text_config",)})
        d = merged

    def _int(key: str) -> int | None:
        v = d.get(key)
        return int(v) if isinstance(v, (int, float)) else None

    def _first(*keys: str) -> int | None:
        for k in keys:
            v = _int(k)
            if v is not None:
                return v
        return None

    quant = d.get("quantization_config") or {}
    quant_method = quant.get("quant_method") if isinstance(quant, dict) else None
    quant_bits = quant.get("bits") if isinstance(quant, dict) else None
    if quant_method and not quant_bits:
        quant_bits = {"fp8": 8, "nvfp4": 4, "gptq": 4, "awq": 4, "compressed-tensors": 8}.get(str(quant_method).lower())

    cfg = ModelConfig(
        model_type=str(d.get("model_type") or ""),
        architectures=list(d.get("architectures") or []),
        hidden_size=_int("hidden_size") or 0,
        num_hidden_layers=_int("num_hidden_layers") or 0,
        num_attention_heads=_int("num_attention_heads") or 0,
        num_key_value_heads=_first("num_key_value_heads"),
        head_dim=_first("head_dim"),
        vocab_size=_int("vocab_size") or 0,
        tie_word_embeddings=bool(d.get("tie_word_embeddings", False)),
        sliding_window=_first("sliding_window"),
        layer_types=d.get("layer_types"),
        full_attention_interval=_first("full_attention_interval"),
        kv_lora_rank=_first("kv_lora_rank"),
        q_lora_rank=_first("q_lora_rank"),
        qk_nope_head_dim=_first("qk_nope_head_dim"),
        qk_rope_head_dim=_first("qk_rope_head_dim"),
        v_head_dim=_first("v_head_dim"),
        index_topk=_first("index_topk"),
        compress_rates=d.get("compress_rates") if isinstance(d.get("compress_rates"), dict) else None,
        index_n_heads=_first("index_n_heads"),
        index_head_dim=_first("index_head_dim"),
        linear_key_head_dim=_first("linear_key_head_dim"),
        linear_value_head_dim=_first("linear_value_head_dim"),
        linear_num_key_heads=_first("linear_num_key_heads"),
        linear_num_value_heads=_first("linear_num_value_heads"),
        conv_kernel=_first("conv_kernel"),
        n_routed_experts=_first("n_routed_experts", "n_experts", "num_experts"),
        num_experts_per_tok=_first("num_experts_per_tok"),
        moe_intermediate_size=_first("moe_intermediate_size"),
        intermediate_size=_first("intermediate_size"),
        shared_expert_intermediate_size=_first("shared_expert_intermediate_size"),
        n_shared_experts=_first("n_shared_experts"),
        first_k_dense_replace=_first("first_k_dense_replace"),
        mlp_only_layers=d.get("mlp_only_layers") if isinstance(d.get("mlp_only_layers"), list) else None,
        mlp_layer_types=d.get("mlp_layer_types") if isinstance(d.get("mlp_layer_types"), list) else None,
        num_local_experts=_first("num_local_experts"),
        num_nextn_predict_layers=_first("num_nextn_predict_layers"),
        quant_method=str(quant_method) if quant_method else None,
        quant_bits=quant_bits,
        raw=dict(d),
    )
    return cfg


def _mirror_candidates(model_ref: str) -> list[str]:
    """Webapp-bundled / PVC-mirrored config.json locations for a repo id
    (webapp/hf_configs ships the seed-catalog models; <work>/hf-configs is the
    runtime mirror). Checked before the HF cache and the network."""
    if "/" not in model_ref or model_ref.startswith((".", "/", "~")):
        return []
    name = model_ref.replace("/", "__") + ".json"
    here = Path(__file__).resolve()
    return [
        str(here.parents[1] / "webapp" / "hf_configs" / name),
        str(Path(os.environ.get("BENCH_WORK_DIR") or "/data") / "hf-configs" / name),
    ]


def load_config_json(model_ref: str | os.PathLike) -> tuple[ModelConfig, str]:
    """Load config.json for a local dir / HF repo id. Returns (config, source).

    Resolution order: directory containing config.json -> HF cache
    (~/.cache/huggingface) -> huggingface_hub download (if importable).
    Raises FileNotFoundError with a self-describing message otherwise.
    """
    ref = str(model_ref).strip()
    p = Path(ref).expanduser()
    candidates: list[Path] = []
    if p.is_dir():
        candidates.append(p / "config.json")
    else:
        # bundled webapp mirror + runtime PVC mirror (seed-catalog models)
        candidates += [Path(c) for c in _mirror_candidates(ref) if Path(c).is_file()]
    if not p.is_file() and "/" in ref:
        # HF repo id: check the local cache first, then try the hub.
        hub = Path.home() / ".cache" / "huggingface" / "hub"
        safe = "models--" + ref.replace("/", "--")
        candidates += sorted((hub / safe / "snapshots").glob("*/config.json"))
    for cand in candidates:
        if cand.is_file():
            try:
                data = json.loads(cand.read_text(encoding="utf-8"))
            except (OSError, ValueError) as e:
                raise ValueError(f"cannot parse {cand}: {e}") from e
            return config_from_dict(data), str(cand)
    # last resort: let huggingface_hub fetch it (if installed and online)
    last_hub_error = ""
    if "/" in ref and not ref.startswith((".", "/", "~")):
        try:
            from huggingface_hub import hf_hub_download
        except ImportError:
            pass
        else:
            try:
                path = hf_hub_download(ref, "config.json")
                data = json.loads(Path(path).read_text(encoding="utf-8"))
                return config_from_dict(data), str(path)
            except Exception as hub_exc:
                last_hub_error = " (" + type(hub_exc).__name__ + ": " + str(hub_exc)[:200] + ")"
    raise FileNotFoundError(
        f"config.json not found for {ref!r}: pass a local model directory "
        "(containing config.json), an HF repo id, or --structure overrides"
        + (last_hub_error if last_hub_error else "")
    )


# ---------------------------------------------------------------------------
# layer-type classification (sliding / linear / full)
# ---------------------------------------------------------------------------

_LINEAR_TYPES = frozenset(
    {"linear_attention", "gated_deltanet", "gated_delta_net", "mamba", "mamba2", "rectified_flow"}
)
_SLIDING_TYPES = frozenset({"sliding_attention", "sliding_attention_full"}) - {""}
_FULL_TYPES = frozenset({"full_attention", "fullattention", "global_attention"})


def _layer_type_names(cfg: ModelConfig) -> list[str]:
    """Per-layer type names, length == num_hidden_layers (best effort)."""
    n = cfg.num_hidden_layers
    if not n:
        return []
    lt = cfg.layer_types
    if isinstance(lt, list) and lt:
        if lt and isinstance(lt[0], dict):
            names = [str(x.get("layer_type", x.get("type", "full_attention"))) for x in lt]
        else:
            names = [str(x) for x in lt]
        if len(names) >= n:
            return names[:n]
        # pattern shorter than the layer count: tile it (common compact form)
        return [names[i % len(names)] for i in range(n)]
    if cfg.is_hybrid_default_pattern:
        interval = cfg.full_attention_interval or 4
        return ["linear_attention" if (i % interval) != (interval - 1) else "full_attention" for i in range(n)]
    if cfg.sliding_window:
        return ["sliding_attention"] * n
    return ["full_attention"] * n


@dataclass
class LayerMix:
    """Counts of layer kinds in the stack."""

    full: int = 0
    sliding: int = 0
    linear: int = 0

    @property
    def kv_layers(self) -> int:
        return self.full + self.sliding

    def label(self) -> str:
        parts = []
        if self.full:
            parts.append(f"{self.full} full")
        if self.sliding:
            parts.append(f"{self.sliding} sliding (win={self._win})" if self._win else f"{self.sliding} sliding")
        if self.linear:
            parts.append(f"{self.linear} linear (no KV)")
        return " + ".join(parts) if parts else "?"

    _win: int | None = None


def classify_layers(cfg: ModelConfig) -> LayerMix:
    mix = LayerMix(_win=cfg.sliding_window)
    for t in _layer_type_names(cfg):
        low = t.lower()
        if low in _LINEAR_TYPES:
            mix.linear += 1
        elif low in _SLIDING_TYPES or "sliding" in low:
            mix.sliding += 1
        else:
            mix.full += 1
    return mix


# ---------------------------------------------------------------------------
# per-family KV math — bytes per token per LAYER, then stacked
# ---------------------------------------------------------------------------


class UnsupportedArch(ValueError):
    """Raised when the architecture needs fields the config doesn't carry."""


def _gqa_head_bytes_per_token(cfg: ModelConfig, bytes_per_elem: float) -> tuple[int, float]:
    """(n_kv_heads_effective, head_dim_effective) for the standard K/V store."""
    n_heads = cfg.num_attention_heads or 0
    n_kv = cfg.num_key_value_heads if cfg.num_key_value_heads is not None else n_heads
    head_dim = cfg.head_dim or (cfg.hidden_size // n_heads if n_heads else 0)
    return max(n_kv, 1), head_dim


def _mla_bytes_per_layer(cfg: ModelConfig, bytes_per_elem: float) -> float:
    """Multi-head Latent Attention (DeepSeek-V2/V3): a single compressed KV
    latent per token per layer + the decoupled RoPE key."""
    if not cfg.kv_lora_rank or cfg.qk_rope_head_dim is None:
        raise UnsupportedArch("MLA model missing kv_lora_rank / qk_rope_head_dim")
    _ = cfg  # keep signature stable for family overrides
    return (cfg.kv_lora_rank + cfg.qk_rope_head_dim) * bytes_per_elem


def _csa2_stored_layers(ratios: list) -> list[tuple[int, int]]:
    """V4.1 CSA2 mode structure from the tech report (sec 4.2.1): encoder
    layers of rate m=2 form three groups of six with one Full Mode layer
    each; decoder layers of rate m=1 form five groups of four where only the
    FIRST group's leader is Full Mode -- the remaining group leaders are
    Reindex Mode, which reuse (do not store) the main KV and indexer K.
    Only Full Mode layers store global KV. Returns
    [(m, n_stored_layers), ...]."""
    stored: list[tuple[int, int]] = []
    n_enc = sum(1 for r in ratios if r == 2)
    n_dec = sum(1 for r in ratios if r == 1)
    if n_enc:
        stored.append((2, max(n_enc // 6, 1)))  # 3 Full (one per encoder group)
    if n_dec:
        stored.append((1, 1))  # 1 Full; other decoder leaders are Reindex (no store)
    return stored


def _compressed_attn_bytes_per_gpu(cfg: ModelConfig, bytes_per_elem: float) -> float:
    """Per-GPU HBM bytes per token for the DeepSeek-V4 compressed-attention
    family (MQA latent => TP-replicated: each rank holds the full state).

    Two generations, two storage models -- each derived from its own paper:

    deepseek_v4 (CSA/HCA hybrid, tech-notes table 4.5.2): EVERY layer stores
    its own compressed stream. CSA layers keep dual-stream entries (C^a, C^b,
    512 dims each, mixed precision: 448 FP8 + 64 RoPE-BF16 = 576 B/entry) plus
    indexer K (128 dims) per block of m=4; HCA layers a single stream per
    block of m-prime=128. Worked check: 21x320 + 20x4.5 = 6,810 B/token => 11.9M
    tokens on 4xH200 mf0.8 -- matches the engine launch report (~11M) and the
    paper's own '6-7 GB at 1M tokens'.

    deepseek_v41 (CSA2 cross-layer reuse, tech report sec 2.3.1/4.2.1): only
    Full Mode layers store global KV (4 of 38 CSA2 layers); entries are FP4
    (MXFP4: 0.5 B/elem + one E4M3 scale per 16 channels) for BOTH main KV and
    indexer K. Worked check: 3x(288+72)/2 + 1x(288+72)/1 = 900 B/token --
    matches the vendor's published 890 B/token (98.9%).
    """
    dims = cfg.head_dim or 0
    idx_dims = cfg.index_head_dim or 0
    if not dims:
        raise UnsupportedArch("compressed-attention model missing head_dim")
    mt = cfg.model_type or ""
    raw = cfg.raw.get("text_config", cfg.raw)
    ratios = raw.get("compress_ratios") if isinstance(raw, dict) else None
    if not (isinstance(ratios, list) and ratios):
        # no schedule: fall back to per-layer full latent (plain MQA store)
        return dims * bytes_per_elem * (cfg.num_hidden_layers or 1)

    if mt.startswith("deepseek_v41"):
        # CSA2: FP4 entries + scale bytes, only Full Mode layers store
        per_token = 0.0
        for m, n in _csa2_stored_layers(ratios):
            entry = dims * 0.5 + dims / 16.0  # FP4 + E4M3 scale per 16 ch
            indexer = idx_dims * 0.5 + idx_dims / 16.0
            per_token += n * (entry + indexer) / m
        return per_token

    # V4 CSA/HCA hybrid: every layer stores; mixed-precision entry
    # (RoPE dims BF16, rest FP8 per the tech-notes table)
    rope = cfg.qk_rope_head_dim or 0
    entry = (dims - rope) * bytes_per_elem + rope * 2.0
    n_csa = sum(1 for r in ratios if r == 4)
    n_hca = sum(1 for r in ratios if r not in (0, 1, 4))
    per_token = n_csa * (2 * entry + idx_dims * bytes_per_elem) / 4.0
    per_token += n_hca * entry / 128.0
    return per_token


def unmodeled_structural_fields(cfg: ModelConfig, details: dict) -> list[str]:
    """Structural fields the config carries that THIS family's storage model does
    not price. The DeepSeek-V4 lesson: an unparsed indexer/compression field can
    shift the KV figure by 3-27x. Any family whose formula ignores a present
    structural field gets a loud warning in the artifact -- the estimate stays
    honest about what it does NOT know."""
    consumed = details.get("_consumed_fields") or set()
    raw = cfg.raw.get("text_config", cfg.raw)
    carried: list[str] = []
    for fname, meaning in (
        ("compress_ratios", "per-layer compression schedule"),
        ("index_head_dim", "sparse-index key stream"),
        ("index_topk", "sparse top-k selection"),
        ("kv_lora_rank", "MLA compressed latent"),
        ("linear_key_head_dim", "linear-attention state"),
        ("dspark_block_size", "DSPARK speculative state"),
    ):
        if (raw.get(fname) is not None or getattr(cfg, fname, None) is not None) and fname not in consumed:
            carried.append(fname + " (" + meaning + ")")
    return carried


def _linear_state_bytes_per_seq(cfg: ModelConfig, bytes_per_elem: float) -> float:
    """Recurrent state per sequence for gated-deltanet/mamba2 layers."""
    khd = cfg.linear_key_head_dim or 0
    vhd = cfg.linear_value_head_dim or 0
    vh = cfg.linear_num_value_heads or 0
    if not (khd and vhd and vh):
        return 0.0
    return vh * vhd * khd * bytes_per_elem


def kv_bytes_per_token(cfg: ModelConfig, kv_dtype: str | None) -> tuple[float, dict]:
    """Bytes per token for the whole stack (KV-cache dtype applied).

    Returns (bytes_per_token, details) where details carries the per-layer
    figure, layer mix, and the formula used — everything the artifact prints.
    """
    bpe = 2.0  # default: fp16/bf16 KV

    got = kv_bytes_per_elem(kv_dtype)
    if got is not None:
        bpe = got

    mt = cfg.model_type
    details: dict = {"kv_bytes_per_elem": bpe}

    # --- MLA family -------------------------------------------------------
    if mt.startswith(("deepseek_v2", "deepseek_v3")) or "dsa" in mt:
        per_layer = _mla_bytes_per_layer(cfg, bpe)
        mix = classify_layers(cfg)
        details.update(
            arch="MLA" + (" + sparse selection (DSA)" if "dsa" in mt else ""),
            formula=f"(kv_lora_rank {cfg.kv_lora_rank} + qk_rope_head_dim {cfg.qk_rope_head_dim}) x {bpe:g} B",
            per_layer_bytes=per_layer,
            layer_mix=mix,
            _consumed_fields={"kv_lora_rank", "qk_rope_head_dim"},
        )
        return per_layer * mix.kv_layers, details

    # --- shared-KV MQA + compressed sparse attention ------------------------
    if mt.startswith("deepseek_v4") or "dsa" in mt:
        per_gpu_bpt = _compressed_attn_bytes_per_gpu(cfg, bpe)
        mix = classify_layers(cfg)
        details.update(
            arch="MQA + compressed sparse attention (CSA/HCA + DSPARK)",
            formula=(
                f"{cfg.model_type}: "
                + (
                    "4 Full-mode layers x (FP4 entry 288B + indexer 72B) / m -- CSA2 reuse"
                    if mt.startswith("deepseek_v41")
                    else "CSA dual-stream + indexer / m=4, HCA / m'=128 -- every layer stores"
                )
            ),
            per_layer_bytes=per_gpu_bpt / max(mix.kv_layers, 1),
            layer_mix=mix,
            note=(
                "per-generation storage model from the respective paper"
                " (V4: tech-notes table 4.5.2; V4.1: tech report sec 2.3.1/4.2.1),"
                " TP-replicated (MQA kv_heads=1); SWA window branches are"
                " per-sequence constants (~2.7 MiB/seq), excluded from the"
                " per-token figure"
            ),
            _consumed_fields={"compress_ratios", "index_n_heads", "index_head_dim", "index_topk", "sliding_window"},
        )
        return per_gpu_bpt, details

    # --- generic (dense GQA / MQA / sliding-window mix) --------------------
    n_kv, head_dim = _gqa_head_bytes_per_token(cfg, bpe)
    if not head_dim:
        raise UnsupportedArch("config missing head_dim / hidden_size÷heads")
    per_layer = 2 * n_kv * head_dim * bpe  # K and V
    mix = classify_layers(cfg)
    details.update(
        arch=("GQA" if (cfg.num_key_value_heads or cfg.num_attention_heads) != cfg.num_attention_heads else "MHA")
        + (" + sliding-window" if mix.sliding else ""),
        formula=f"2 (K+V) x kv_heads {n_kv} x head_dim {head_dim} x {bpe:g} B",
        per_layer_bytes=per_layer,
        layer_mix=mix,
        _consumed_fields={"sliding_window", "layer_types"},
    )
    if mix.sliding:
        details["note"] = (
            "sliding-window layers are budgeted at full per-token cost "
            "(paged pools allocate every token); engines that reuse window "
            "slots only make real capacity larger"
        )
    return per_layer * mix.kv_layers, details


# ---------------------------------------------------------------------------
# parameter counting (weights)
# ---------------------------------------------------------------------------


def count_parameters(cfg: ModelConfig) -> tuple[int, list[str]]:
    """Best-effort parameter count from config shape. Returns (params, notes).

    Counts embeddings + per-layer attention + MLP/MoE + untied lm_head.
    Vision towers / audio encoders in multimodal configs are NOT counted
    (a note says so).
    """
    notes: list[str] = []
    h = cfg.hidden_size
    n = cfg.num_hidden_layers
    if not (h and n):
        return 0, ["config missing hidden_size / num_hidden_layers"]
    n_heads = cfg.num_attention_heads or 0
    n_kv = cfg.num_key_value_heads if cfg.num_key_value_heads is not None else n_heads
    head_dim = cfg.head_dim or (h // n_heads if n_heads else 0)

    params = cfg.vocab_size * h  # input embeddings
    if not cfg.tie_word_embeddings:
        params += cfg.vocab_size * h  # lm_head

    # MLA attention block shapes
    if cfg.kv_lora_rank and cfg.qk_rope_head_dim is not None:
        q_lora = cfg.q_lora_rank or h
        qk_dim = (cfg.qk_nope_head_dim or 0) + (cfg.qk_rope_head_dim or 0)
        v_dim = cfg.v_head_dim or (cfg.qk_nope_head_dim or head_dim)
        attn = (
            h * q_lora  # q_a
            + q_lora * n_heads * qk_dim  # q_b
            + h * (cfg.kv_lora_rank + (cfg.qk_rope_head_dim or 0))  # kv_a
            + cfg.kv_lora_rank * n_heads * ((cfg.qk_nope_head_dim or 0) + v_dim)  # kv_b
            + n_heads * v_dim * h  # o_proj
        )
    else:
        qk_dim = head_dim
        v_dim = cfg.v_head_dim or head_dim
        attn = h * (n_heads * qk_dim) + 2 * h * (n_kv * head_dim) + n_heads * v_dim * h

    moe_layer_idx = _moe_layer_indices(cfg)
    mlp_total = 0
    for i in range(n):
        inter = cfg.moe_intermediate_size or cfg.intermediate_size or 4 * h
        if i in moe_layer_idx:
            experts = cfg.experts or 0
            mlp_total += experts * 3 * h * inter
            if cfg.n_shared_experts:
                mlp_total += cfg.n_shared_experts * 3 * h * inter
            if cfg.shared_expert_intermediate_size:
                mlp_total += 3 * h * cfg.shared_expert_intermediate_size
            mlp_total += h * experts  # router gate
        else:
            mlp_total += 3 * h * inter

    params += n * attn + mlp_total + 2 * h  # + final norm(s)

    if cfg.num_nextn_predict_layers:
        notes.append(
            f"MTP module ({cfg.num_nextn_predict_layers} layer(s)) not counted in weights; "
            "add its KV via --speculative when enabled"
        )
    if "vision_config" in cfg.raw or "audio_config" in cfg.raw:
        notes.append("multimodal encoder (vision/audio tower) present in config and NOT counted")
    return params, notes


def _moe_layer_indices(cfg: ModelConfig) -> set[int]:
    n = cfg.num_hidden_layers
    if not n or not cfg.is_moe:
        return set()
    if cfg.mlp_only_layers:
        return {i for i in cfg.mlp_only_layers if isinstance(i, int) and 0 <= i < n}
    dense_first = cfg.first_k_dense_replace or 0
    if cfg.mlp_layer_types:
        # e.g. deepseek_v4 ["hash_moe", "moe", ...] — dense layers are absent
        # from the list; compact schedules tile.
        if len(cfg.mlp_layer_types) >= n:
            return {i for i, t in enumerate(cfg.mlp_layer_types[:n]) if str(t) != "dense"}
        tiled = [cfg.mlp_layer_types[i % len(cfg.mlp_layer_types)] for i in range(n)]
        return {i for i, t in enumerate(tiled) if str(t) != "dense"}
    return set(range(dense_first, n))


# MoE runner backends that store ROUTED EXPERT weights at 4-bit regardless of
# the checkpoint dtype (the engine dequantizes on the fly): the two PCAI
# serving shapes for FP8 MoE checkpoints on smaller GPUs.
_MOE_4BIT_RUNNERS = ("flashinfer_mxfp4", "marlin", "mxfp4")


def weight_bytes_estimate(
    cfg: ModelConfig, weight_dtype: str | None, moe_runner: str | None = None
) -> tuple[float | None, dict]:
    """(bytes, details) for weights; None bytes when the dtype is unknown.

    ``weight_dtype=None`` resolves from the config first
    (quantization_config -> torch_dtype), so callers only override when
    they know better.

    ``moe_runner`` (optional): the deployment's --moe-runner-backend. Backends
    that keep routed experts at 4-bit (flashinfer_mxfp4/marlin) split the
    weight cost: attention/dense params at the checkpoint dtype, expert
    params at 0.5 B/param. This is the PCAI shape for FP8 MoE checkpoints on
    smaller GPUs -- DeepSeek-V4-Flash (291B) fits 2x RTX PRO 6000 ONLY this
    way, and the live benchmark in results/ confirms it.
    """
    params, notes = count_parameters(cfg)
    bpp = weight_bytes_per_param(weight_dtype) if weight_dtype else weight_bytes_per_param(default_weight_dtype(cfg))
    details: dict = {"params": params, "notes": notes, "params_b": params}
    if not params:
        return None, details
    if bpp is None:
        details["dtype_known"] = False
        return None, details
    details["dtype_known"] = True
    details["bytes_per_param"] = bpp
    runner = (moe_runner or "").strip().lower()
    if cfg.is_moe and runner in _MOE_4BIT_RUNNERS:
        from .estimate import _expert_param_share

        share = _expert_param_share(cfg)
        expert_bpp = weight_bytes_per_param("nvfp4") or 0.5
        details["moe_runner"] = runner
        details["moe_expert_share"] = share
        details["bytes"] = params * ((1 - share) * bpp + share * expert_bpp)
        details["notes"] = list(notes) + [
            (
                f"moe-runner {runner}: routed experts (~{share * 100:.0f}% of params) stored at 4-bit "
                f"({expert_bpp:g} B/param), attention/dense at {bpp:g} B/param"
            ),
        ]
    else:
        details["bytes"] = params * bpp
    return details["bytes"], details


def default_weight_dtype(cfg: ModelConfig) -> str | None:
    """Weights dtype guess: quantization_config first, else torch_dtype."""
    if cfg.quant_method:
        q = normalize_dtype(cfg.quant_method)
        if q in ("fp8", "nvfp4", "fp4", "int4", "gptq", "awq", "int8"):
            return q
    td = cfg.raw.get("torch_dtype") or cfg.raw.get("dtype")
    return normalize_dtype(str(td)) if td else None
