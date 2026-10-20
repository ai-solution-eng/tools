"""Capacity estimation: weights + KV cache + overhead -> does it fit, how
many tokens fit, how many concurrent N-token requests are supported.

The math (all deterministic, no throughput predictions):

    weights_per_gpu     = weight_bytes / tp_size / pp_size
    usable_per_gpu      = vram_gib x 1024^3 x mem_fraction - overhead_bytes
    kv_pool_per_gpu     = usable - weights_per_gpu
    kv_tokens_total     = sum over GPUs of (kv_pool_per_gpu / kv_bytes_per_token)
    concurrency(ctx)    = floor(kv_tokens_total / ctx)

PP (pipeline parallel) splits the LAYERS across ``pp`` stage groups, so
weights shrink by another ``pp`` factor. The KV cache does not get cheaper
per token (each token's KV lives once, on the stage owning that layer), so
the token pool divides the full stream across the TP x PP group exactly
once — the same rule as TP — and extra replicas beyond tp*pp multiply.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .configs import (
    ModelConfig,
    _moe_layer_indices,
    count_parameters,
    default_weight_dtype,
    kv_bytes_per_token,
    weight_bytes_estimate,
)
from .dtypes import dtype_label
from .gpus import GpuSpec

# activation + CUDA/graph working-set reserve per GPU (calibrate via phase 2)
DEFAULT_OVERHEAD_GIB = 2.0

# Host-RAM the engine itself needs on top of the engram tables when
# SGLANG_ENABLE_DSV41_ENGRAM_HOST_TABLE=1 pins them in host memory. Measured
# on the live seed deployment: 207 GiB container working set - 183.1 GiB
# tables = ~24 GiB (tokenizer, schedulers, host-side KV/HiCache bookkeeping,
# the SGLang process itself). Used ONLY for the host-budget adequacy check.
ENGRAM_HOST_RUNTIME_GIB = 24.0


@dataclass
class HicacheSpec:
    """Hierarchical KV cache (SGLang HiCache / vLLM hierarchical cache).

    Tier naming follows the serving docs: L2 = host RAM (CPU), L3 = optional
    NVMe/object-storage backing tier beyond host RAM. L1 is the device pool
    already priced in :func:`run_estimate`'s KV pool.

    Modes:
      - ``ratio``: L2 size = hicache_ratio x the device KV pool, per replica.
      - ``size``: explicit per-replica budget in GiB (``--hicache-size``).

    L3 is an absolute per-replica budget in GiB (an eviction target beyond
    L2; the engines cap it at L2's size when it would exceed it).

    l2_tp_replicated: by default (False) the L2 pool is partitioned across
    TP ranks (each rank's host pool holds its KV share). ``True`` prices the
    MQA/MLA replication semantics where each rank's host pool holds the FULL
    per-token stream — the same rule the device pool already applies.
    """

    l2_mode: str = ""  # "" (off) | "ratio" | "size"
    l2_ratio: float = 0.0  # x the device KV pool (mode=ratio)
    l2_gib: float = 0.0  # explicit host RAM per replica (mode=size)
    l3_gib: float = 0.0  # L3 backing-tier budget per replica (0 = off)
    l2_tp_replicated: bool | None = None  # None = auto (MQA/MLA replicate; else shard)

    @property
    def active(self) -> bool:
        return self.l2_mode in ("ratio", "size")

    @classmethod
    def off(cls) -> HicacheSpec:
        return cls()


@dataclass
class EngramSpec:
    """DeepSeek-V4.1 engram (n-gram memory) table placement.

    The checkpoint ships one fp8 lookup table per engram layer (rows x
    engram_head_dim). SGLang rows-shards them across the TP group on GPU by
    DEFAULT; ``SGLANG_ENABLE_DSV41_ENGRAM_HOST_TABLE=1`` pins them in host
    RAM instead, freeing HBM for the KV pool (layout knob
    SGLANG_DSV41_ENGRAM_HOST_TABLE_LAYOUT = shared | per_rank; either way
    the host holds the full table -- 'shared' one copy all ranks read,
    'per_rank' one shard per rank).

    mode: "" (tables are VRAM weights, the default layout)
        | "host" (host-table offload: out of HBM, into the host-RAM budget)
    dtype: table storage dtype (None = the checkpoint's fp8)
    host_gib: host-RAM budget for the tables (0 = no budget check) -- the
        deployment's container memory limit is the natural value.
    """

    mode: str = ""  # "" (VRAM default layout) | "host"
    dtype: str | None = None
    host_gib: float = 0.0

    @property
    def active(self) -> bool:
        return self.mode in ("", "host")


@dataclass
class SpeculativeSpec:
    """Speculative-decoding shape (the engines' --speculative-* semantics).

    algorithm: DSPARK | EAGLE | DFLASH | NEXTN | MTP | empty (off).
      - DFLASH: a SEPARATE draft model (draft_model repo/path) with its own
        weights resident on the GPUs AND its own KV per token -- both priced
        from the draft's config when available.
      - EAGLE / NEXTN / DSPARK / MTP: draft head(s) attached to the target
        (layers = extra KV layers; draft_tokens = the acceptance window).
    """

    algorithm: str = ""
    draft_tokens: int = 0
    draft_model: str = ""  # repo id / path (DFLASH)
    layers: int = 0  # extra KV layers (MTP-family)

    @property
    def active(self) -> bool:
        return bool(self.algorithm) and (self.layers > 0 or bool(self.draft_model))


@dataclass
class MambaSpec:
    """The mamba/KDA recurrent-state pool knobs (linear-attention models:
    Qwen3-Next / qwen4_exp, GLM-5.3-Flash's KDA layers, DeepSeek sparse
    hybrids). The state pool is a SEPARATE concurrency ceiling on top of
    the KV pool: SGLang splits device memory between them.

    full_memory_ratio: --mamba-full-memory-ratio — the fraction of the
        (mem-fraction) device budget the mamba pool may occupy when the KV
        pool is under pressure. Default ~0.15-0.3 engine-side for hybrid
        models; sized up only for mamba-heavy serving. The state needed is
        bytes_per_seq x layers x concurrent sequences: a deployment aiming
        at C concurrent x L-token requests needs ratio >=
        (state_per_seq x C) / (mem_fraction x vram) — the estimator warns
        when the configured ratio cannot carry the requested concurrency.
    ssm_dtype: --mamba-ssm-dtype (float32 | bfloat16) — the state storage
        width; float32 doubles per-sequence state vs bfloat16.
    """

    full_memory_ratio: float | None = None  # None = engine default, no split modeled
    ssm_dtype: str | None = None
    # --max-mamba-cache-size: the sequence bound the OPERATOR set (SGLang
    # sizes the recurrent-state pool by demand up to this many sequences).
    # 0/None = unknown — the pool is modeled as claiming the full ratio cap
    # (the conservative pre-demand-sizing behavior, with a loud warning).
    max_cache_size: int = 0

    @property
    def active(self) -> bool:
        return self.full_memory_ratio is not None


@dataclass
class EstimateRequest:
    """One estimation scenario."""

    cfg: ModelConfig
    gpu: GpuSpec
    gpu_count: int = 1
    tp_size: int = 1
    pp_size: int = 1
    # the model ref the config came from (HF repo id / local dir) -- enables
    # the checkpoint-size lookup (model.safetensors.index.json)
    model_ref: str = ""
    weight_dtype: str | None = None
    kv_dtype: str | None = None
    # compressed-state storage dtype (DeepSeek families: the deployment's
    # SGLANG_DSV4_COMPRESS_STATE_DTYPE env -- fp4 checkpoint default vs bf16)
    state_dtype: str | None = None
    # linear-attention recurrent-state dtype (mamba float32 vs bfloat16;
    # default from the config's mamba_ssm_dtype). CANONICAL home: the same
    # knob also exists as MambaSpec.ssm_dtype (the engine's
    # --mamba-ssm-dtype lives with the other mamba flags); __post_init__
    # reconciles the two so either spelling prices identically.
    ssm_dtype: str | None = None
    mem_fraction: float = 0.9
    overhead_gib: float = DEFAULT_OVERHEAD_GIB
    context: int = 32768
    # speculative decoding: DFLASH draft model / MTP-family draft layers
    speculative: SpeculativeSpec | None = None
    # engram n-gram memory tables (DeepSeek-V4.1): VRAM (default layout) or host
    engram: EngramSpec = field(default_factory=EngramSpec)
    # mamba/KDA recurrent-state pool split + dtype (linear-attention models)
    mamba: MambaSpec = field(default_factory=MambaSpec)
    # concurrent sequences the mamba pool must carry (default: the context
    # concurrency) — used only for the ratio adequacy warning
    mamba_concurrency: int = 0  # 0 = check against the KV-pool concurrency
    # hierarchical KV cache tiers (L2 host RAM / L3 backing store)
    hicache: HicacheSpec = field(default_factory=HicacheSpec.off)
    # expert-parallel size: experts shard ep-ways (attention stays TP)
    ep_size: int = 1
    # MoE runner backend (flashinfer_mxfp4/marlin store experts at 4-bit)
    moe_runner: str | None = None
    # advisory notes populated during the estimate (e.g. EP/PP cap warnings);
    # merged into the result warnings by run_estimate
    notes_ep: list[str] | None = None

    def __post_init__(self) -> None:
        self.tp_size = min(self.tp_size, self.gpu_count)  # TP cannot exceed the GPU count
        self.pp_size = min(self.pp_size, self.gpu_count)  # PP cannot exceed the GPU count
        # ONE SSM-state dtype, two spellings (EstimateRequest.ssm_dtype and
        # MambaSpec.ssm_dtype). Reconcile here so every consumer -- including
        # the mamba-pool adequacy check -- sees the same value regardless of
        # which spelling the caller used.
        if self.ssm_dtype and not self.mamba.ssm_dtype:
            self.mamba.ssm_dtype = self.ssm_dtype
        elif self.mamba.ssm_dtype and not self.ssm_dtype:
            self.ssm_dtype = self.mamba.ssm_dtype


@dataclass
class CacheTiers:
    """Per-tier token capacity for the hierarchical cache.

    Device (L1) tokens are the per-GPU pool already summed in
    :class:`EstimateResult`. L2/L3 follow the per-replica budgeting the
    engines use: the host pool is allocated per replica (each replica's
    scheduler owns its own L2 pool), and a TP-replicated layout holds the
    full per-token stream on every rank rather than a shard.
    """

    l2_bytes_per_replica: float = 0.0
    l2_bytes_total: float = 0.0
    l2_tokens: float | None = None  # None = capacity unknowable (no weights)
    l3_bytes_per_replica: float = 0.0
    l3_bytes_total: float = 0.0
    l3_tokens: float | None = None
    total_tokens: float | None = None
    per_token_bytes_l2: float | None = None  # effective L2 bytes/token/replica
    per_token_bytes_l3: float | None = None
    notes: list[str] = field(default_factory=list)


@dataclass
class EstimateResult:
    """Everything the artifact prints for one scenario."""

    request: EstimateRequest
    weights_bytes: float | None
    kv_bytes_per_token: float
    usable_per_gpu: float
    weights_per_gpu: float | None
    kv_pool_per_gpu: float | None
    kv_tokens_total: float | None
    kv_tokens_per_gpu: float | None
    fits: bool | None
    concurrency: int | None
    details: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    tiers: CacheTiers = field(default_factory=CacheTiers)


def _expert_param_share(cfg: ModelConfig) -> float:
    """Fraction of parameters that live in routed experts (0..1)."""
    h = cfg.hidden_size or 0
    n = cfg.num_hidden_layers or 0
    if not (h and n) or not cfg.is_moe:
        return 0.0
    experts = cfg.experts or 1
    inter = cfg.moe_intermediate_size or cfg.intermediate_size or 4 * h
    n_moe = len(_moe_layer_indices(cfg))
    expert_params = n_moe * experts * 3 * h * inter
    total, _ = count_parameters(cfg)
    return expert_params / total if total else 0.0


def _weights_per_gpu(wbytes: float, req: EstimateRequest) -> float:
    """Per-GPU weight footprint under the parallelism split.

    Pure TP shards every weight ``tp``-ways. PP additionally stages the
    layers across ``pp`` pipeline groups, so per-GPU weight bytes shrink by
    another ``pp`` factor (the KV cache does NOT: every token's KV lives on
    exactly one pipeline stage — the layer-owning rank — so the pool math
    divides the whole-model stream across the TP x PP group exactly once,
    same as TP). MoE checkpoints deployed with expert parallelism shard the
    *experts* across all GPUs while attention shards by TP only — when
    ``req.ep_size`` divides the GPU count, experts go ep-ways and the rest
    tp-ways (the PCAI MoE serving shape, e.g. GLM-5.2 742B at TP4/PP2/EP8
    on 8 GPUs). PP combines with TP/EP the same way it does in the engines:
    it multiplies the stage split (weights/GPU = bytes / tp / pp).
    """
    stage = max(1, req.tp_size * req.pp_size)
    if req.ep_size > 1 and req.ep_size <= req.gpu_count and req.cfg.is_moe:
        share = _expert_param_share(req.cfg)
        ep_bytes = wbytes * share / req.ep_size
        dense_bytes = wbytes * (1 - share) / stage
        if req.ep_size > max(1, req.gpu_count // max(req.pp_size, 1)):
            # experts shard ep-ways GLOBALLY; when PP stages the layers, each
            # stage group only spans gpu_count/pp GPUs -- an ep_size beyond
            # that cannot be realized (the engines cap ep at the stage width)
            req.notes_ep = req.notes_ep or []
            req.notes_ep.append(
                f"EP{req.ep_size} exceeds the PP{req.pp_size} stage width "
                f"({max(1, req.gpu_count // req.pp_size)} GPUs/stage) -- engines cap EP there; "
                "the estimate below assumes the full EP split and is optimistic"
            )
        return ep_bytes + dense_bytes
    return wbytes / stage


def kv_replication_factor(cfg: ModelConfig, tp_size: int) -> int:
    """How many TP ranks jointly store each token's KV stream (>=1).

    The layout is a spectrum, not binary:
    - ``kv_heads >= tp``: heads shard cleanly, the group stores ONE copy of
      each token (factor 1).
    - ``1 < kv_heads < tp``: heads cannot shard tp-ways, so each head is
      REPLICATED across tp/kv_heads ranks (e.g. Qwen3.8-27B hybrid,
      kv_heads=4 on TP8: every head lives on 2 ranks). The group stores
      tp/kv_heads copies — the old binary rule called this "replicated" and
      undercounted pool capacity by kv_heads x.
    - ``kv_heads == 1`` (MQA) / MLA latent streams: every rank holds the
      FULL stream (factor tp) — wide TP adds speed, not tokens.
    """
    mt = cfg.model_type or ""
    # glm5_next (GLM-5.3-Flash hybrid) routes to MLA latent pricing — its
    # full-attention layers store ONE compressed latent per token, replicated
    # per rank under TP (same as the DSA families), NOT 64 sharded KV heads
    # (num_key_value_heads=64 is the pre-latent MHA shape, stale here).
    if (
        mt.startswith(("deepseek_v2", "deepseek_v3", "deepseek_v4", "glm5_next"))
        or "dsa" in mt
        or (cfg.kv_lora_rank and cfg.model_type in ("glm_moe_dsa", "glm5_next"))
    ):
        return max(tp_size, 1)  # MLA latent / V4-family MQA store: one per rank
    kv_heads = cfg.num_key_value_heads if cfg.num_key_value_heads is not None else cfg.num_attention_heads
    return max(1, tp_size // max(kv_heads or 1, 1))


def _tp_replicated_kv(cfg: ModelConfig, tp_size: int) -> bool:
    """True when the device KV layout is TP-REPLICATED: the pool cannot shard
    because the per-token stream lives once per rank (MQA/MLA latent streams,
    or kv_heads that do not reach the TP width). See
    :func:`kv_replication_factor` for the partial-shard spectrum."""
    return kv_replication_factor(cfg, tp_size) > 1


def _cache_tiers(
    req: EstimateRequest,
    kv_bpt: float,
    kv_pool_per_gpu: float | None,
    weights_known: bool,
    device_tokens: float | None,
) -> CacheTiers:
    """L2 (host RAM) / L3 (backing tier) token capacity from the request.

    Engine semantics (SGLang HiCache): L2 is INSTANCE-PRIVATE — one host
    pool per replica. MHA/GQA ranks each hold ``1/tp`` of a token's KV while
    MLA ranks hold the full stream (and write-back dedupes to one copy), so
    in the replica-collective frame every layout stores ONE full-model
    stream per token: per-token cost is ``kv_bpt`` and only the per-replica
    BYTE budget differs by layout. Both paths then satisfy the engine's
    invariant:  L2 tokens (ratio mode) = ratio x device tokens per replica.

    L3 is an absolute per-replica budget (an eviction target beyond L2;
    capped at L2's pool — a backing tier larger than host RAM is never
    filled). ``device_tokens`` comes from :func:`run_estimate` so both tiers
    and the headline share one expression.
    """
    h = req.hicache
    if not h.active or not weights_known:
        return CacheTiers()

    replicas = max(1, req.gpu_count // req.tp_size) if req.tp_size >= 1 else 1
    rep_factor = (
        kv_replication_factor(req.cfg, req.tp_size)
        if h.l2_tp_replicated is None
        else (req.tp_size if h.l2_tp_replicated else 1)
    )
    notes: list[str] = []

    pool_gib = (kv_pool_per_gpu or 0.0) / 1024**3
    if h.l2_mode == "ratio":
        # per-replica L2 bytes: ratio x the device pool, in the engine's layout
        # (sharded: every rank's pool = ratio x that rank's device pool;
        #  replicated: each rank's pool holds the full stream, but write-back
        #  dedupes to one copy per tp/rep-factor ranks = one rank's pool)
        l2_gib = pool_gib * h.l2_ratio * (rep_factor / req.tp_size if rep_factor > 1 else req.tp_size)
        if h.l2_ratio > 0:
            notes.append(f"L2 = hicache-ratio {h.l2_ratio:g} x the {pool_gib:.1f} GiB/GPU device pool")
    elif h.l2_mode == "size":
        l2_gib = h.l2_gib
    else:
        l2_gib = 0.0

    # L3 is an absolute per-replica budget; the engines cap it at the L2 pool
    # (a backing tier larger than host RAM would never be filled).
    l3_gib = min(h.l3_gib, l2_gib) if h.l3_gib else 0.0
    if h.l3_gib and l3_gib < h.l3_gib:
        notes.append(
            f"L3 budget capped at the L2 pool ({l2_gib:.0f} GiB) — an L3 tier larger than host RAM is never filled"
        )

    l2_bytes = l2_gib * 1024**3
    l3_bytes = l3_gib * 1024**3
    # replica-collective frame: one full-model stream per token
    l2_tokens: float | None = l2_bytes * replicas / kv_bpt if l2_bytes else None
    l3_tokens: float | None = l3_bytes * replicas / kv_bpt if l3_bytes else None
    if replicas > 1:
        notes.append(
            f"{replicas} replicas x {req.tp_size} GPUs: the host pool is allocated PER REPLICA (instance-private)"
        )
    if rep_factor > 1:
        note = (
            "MLA/MQA layout: host tier stores ONE deduplicated stream per token (per-rank pools hold the full stream; write-back dedupes)"
            if rep_factor == req.tp_size
            else f"partial-shard layout: kv_heads replicate x{rep_factor} across TP{req.tp_size}; host tier dedupes to one stream per {rep_factor} ranks"
        )
        notes.append(note)
    else:
        notes.append(f"TP{req.tp_size} layout: each rank's host pool holds its 1/{req.tp_size} share of each token")

    total = None
    if device_tokens is not None and (l2_tokens or l3_tokens):
        total = device_tokens + (l2_tokens or 0.0) + (l3_tokens or 0.0)
    elif device_tokens is not None:
        total = device_tokens
    return CacheTiers(
        l2_bytes_per_replica=l2_bytes,
        l2_bytes_total=l2_bytes * replicas,
        l2_tokens=l2_tokens,
        l3_bytes_per_replica=l3_bytes,
        l3_bytes_total=l3_bytes * replicas,
        l3_tokens=l3_tokens,
        total_tokens=total,
        per_token_bytes_l2=kv_bpt,
        per_token_bytes_l3=kv_bpt,
        notes=notes,
    )


def run_estimate(req: EstimateRequest) -> EstimateResult:
    """Run one estimation scenario."""
    cfg = req.cfg
    # mamba ssm dtype: the MambaSpec carries the --mamba-ssm-dtype value;
    # the standalone ssm_dtype field stays as the manual override
    ssm_dtype_eff = req.ssm_dtype or req.mamba.ssm_dtype
    # engram tables first (they ride INSIDE the checkpoint index total and
    # are lifted out of the weight figure; placed separately below)
    engram_bytes_total: float | None = None
    engram_details: dict = {}
    es = req.engram
    try:
        from .configs import checkpoint_total_bytes, engram_table_bytes

        engram_bytes_total, engram_details = engram_table_bytes(cfg, es.dtype)
        # the EXACT stored checkpoint size (model.safetensors.index.json
        # metadata.total_size) -- authoritative for mixed-dtype checkpoints
        # (MoE fp4 + attention fp8, Kimi-style mxfp4 experts). Falls back to
        # the shape-derived bytes/param figure when the index is absent.
        ckpt_bytes, ckpt_source = checkpoint_total_bytes(req.model_ref or "")
    except Exception:  # pragma: no cover — never break an estimate on the engram path
        engram_details = {"present": True, "parse_ok": False}
        ckpt_bytes, ckpt_source = None, ""
    wbytes, wdetails = weight_bytes_estimate(
        cfg,
        req.weight_dtype,
        req.moe_runner,
        int(ckpt_bytes) if ckpt_bytes else None,
        int(engram_bytes_total) if engram_bytes_total else None,
    )
    kv_bpt, kvdetails = kv_bytes_per_token(cfg, req.kv_dtype, req.state_dtype, ssm_dtype_eff)
    if ckpt_bytes:
        wdetails["checkpoint_bytes_total"] = float(ckpt_bytes)
        wdetails["checkpoint_source"] = ckpt_source
        wdetails["notes"] = list(wdetails.get("notes", [])) + [
            f"weights = the checkpoint's exact stored size ({ckpt_bytes / 1024**3:.1f} GiB total, "
            + (
                f"engram tables {engram_bytes_total / 1024**3:.1f} GiB placed separately, "
                if engram_bytes_total
                else ""
            )
            + "per model.safetensors.index.json)"
        ]
    warnings: list[str] = wdetails.get("notes", [])
    if kvdetails.get("note"):
        # family notes (e.g. the linear-attention per-sequence state pool)
        # ride the warnings block -- they qualify the headline number
        warnings.append(str(kvdetails["note"]))
    for csa2w in kvdetails.get("csa2_structure_warnings") or []:
        # schedule-structure deviations (e.g. decoder layer count not a
        # multiple of the report's group size) qualify the stored-layer model
        warnings.append(f"CSA2 schedule structure: {csa2w} — the stored-layer count assumes the report's grouping")
    if req.pp_size > 1:
        warnings.append(
            f"pipeline parallel PP{req.pp_size}: weights stage-split (x{req.pp_size} lighter per GPU); "
            "the KV pool shards across the TP x PP group exactly once (each token's KV lives on its layer's stage)"
        )

    spec = req.speculative
    draft_weights_bytes: float | None = None
    # MTP-family default (BEFORE the active gate — spec.active requires layers or
    # a draft model, so an algorithm-only EAGLE/NEXTN/MTP spec would never reach
    # the block inside): the checkpoint's built-in draft head is what those
    # algorithms serve; when speculative is enabled without a layer count, take
    # it from the config (GLM-5.3-Flash ships num_nextn_predict_layers=1 and the
    # seed deployment runs EAGLE on it).
    if spec and spec.algorithm and not spec.layers and not spec.draft_model and cfg.num_nextn_predict_layers:
        spec.layers = cfg.num_nextn_predict_layers
        warnings.append(
            f"speculative {spec.algorithm or 'MTP'}: draft layers defaulted to the checkpoint's "
            f"built-in MTP module ({spec.layers} layer(s)) — pass --speculative 0 to model without it"
        )
    if spec and spec.active:
        if spec.algorithm.upper() == "DFLASH" and spec.draft_model:
            # DFLASH: a separate draft model -- price its weights AND KV from
            # its own config when resolvable (bundled mirror / hub / path).
            try:
                from .configs import load_config_json

                draft_cfg, draft_src = load_config_json(spec.draft_model)
                dw, _ = weight_bytes_estimate(draft_cfg, req.weight_dtype, req.moe_runner)
                d_bpt, _d_details = kv_bytes_per_token(draft_cfg, req.kv_dtype)
                if dw is None:
                    warnings.append(
                        f"DFLASH draft {spec.draft_model!r}: weights size unknown (dtype?) --"
                        + " draft weights NOT counted; only its KV is priced"
                    )
                else:
                    draft_weights_bytes = dw
                # draft KV rides per target token (draft attention runs every step)
                kv_bpt += d_bpt
                kvdetails["draft_kv_bytes_per_token"] = d_bpt
                kvdetails["draft_config_source"] = draft_src
                weights_note = (
                    f"{dw / 1024**3:.1f} GiB weights @ {req.weight_dtype or 'auto'} + " if dw is not None else ""
                )
                warnings.append(
                    f"DFLASH draft {spec.draft_model!r}: {weights_note}{d_bpt / 1024:.1f} KiB/token draft KV"
                )
            except Exception as exc:
                warnings.append(
                    f"DFLASH draft {spec.draft_model!r} could not be priced ({type(exc).__name__}) --"
                    + " draft weights/KV NOT counted; sizes may be optimistic"
                )
        elif spec.layers:
            # MTP/EAGLE/NEXTN/DSPARK: draft layers extend the target KV stack
            per_layer = kvdetails.get("per_layer_bytes", 0.0)
            extra = per_layer * spec.layers
            kv_bpt += extra
            kvdetails["speculative_kv_bytes_per_token"] = extra
            warnings.append(
                f"speculative {spec.algorithm} draft KV: +{spec.layers} draft layer(s) add {extra / 1024:.1f} KiB/token"
            )
        if spec.draft_tokens:
            kvdetails["speculative_draft_tokens"] = spec.draft_tokens

    usable_per_gpu = req.gpu.vram_bytes * req.mem_fraction - req.overhead_gib * 1024**3

    # --- engram (n-gram memory) tables: placement ---------------------------
    # (engram_bytes_total was computed at the top of run_estimate and lifted
    # out of the checkpoint weight figure; here it is PLACED: the default
    # SGLang layout rows-shards the tables across TP on GPU — added back to
    # weights_per_gpu — while the host-table offload keeps them out of HBM
    # entirely, a host-RAM requirement instead.)
    es = req.engram
    if engram_bytes_total is not None:
        eb = float(engram_bytes_total)
        if es.mode == "host":
            engram_details["mode"] = "host"
            engram_details["host_bytes"] = eb
            warnings.append(
                f"engram host table: {eb / 1024**3:.1f} GiB of fp8 n-gram tables pinned in host RAM "
                "(SGLANG_ENABLE_DSV41_ENGRAM_HOST_TABLE=1) — freed HBM goes to the KV pool; "
                "no HiCache tier is added (engram tables are weights, not tokens)"
            )
            # The pod must hold the TABLES **plus** the engine's own runtime
            # (measured ~24 GiB on the seed V4.1-Flash deployment: 183.1 GiB
            # tables + ~24 GiB = 207 GiB working set). Comparing the budget
            # against the tables alone would pass a 190 GiB limit that OOMs.
            need = eb + ENGRAM_HOST_RUNTIME_GIB * 1024**3
            engram_details["host_runtime_gib"] = ENGRAM_HOST_RUNTIME_GIB
            engram_details["host_need_gib"] = need / 1024**3
            if es.host_gib and need > es.host_gib * 1024**3:
                warnings.append(
                    f"engram host offload needs ~{need / 1024**3:.0f} GiB host RAM "
                    f"({eb / 1024**3:.1f} GiB tables + ~{ENGRAM_HOST_RUNTIME_GIB:.0f} GiB engine runtime) "
                    f"but the budget is {es.host_gib:.0f} GiB — raise the pod memory limit or turn the offload off"
                )
        else:
            # default layout: row-sharded across the TP group on GPU
            engram_details["mode"] = "vram"
            engram_details["vram_bytes_per_gpu"] = eb / max(req.tp_size, 1)
            warnings.append(
                f"engram tables: {eb / 1024**3:.1f} GiB counted as VRAM weights (default row-sharded "
                f"across TP{req.tp_size}); SGLANG_ENABLE_DSV41_ENGRAM_HOST_TABLE=1 would free them"
            )
        warnings.append(f"engram table size assumption: {engram_details.get('assumption', 'rows x head_dim x 1B')}")
        kvdetails["_consumed_fields"] = set(kvdetails.get("_consumed_fields") or set()) | {
            "engram_layer_ids",
            "engram_num_embeddings",
            "engram_head_dim",
        }

    weights_per_gpu = _weights_per_gpu(wbytes, req) if wbytes is not None else None
    warnings.extend(list(req.notes_ep or []))
    if weights_per_gpu is not None and engram_bytes_total is not None and req.engram.mode != "host":
        # default layout: the tables shard across TP like any weight
        weights_per_gpu = weights_per_gpu + engram_bytes_total / max(req.tp_size, 1)
    if weights_per_gpu is not None and draft_weights_bytes is not None:
        # the DFLASH draft model is fully resident alongside the target
        weights_per_gpu = weights_per_gpu + draft_weights_bytes / req.tp_size
        warnings.append(f"draft weights add {draft_weights_bytes / req.tp_size / 1024**3:.1f} GiB/GPU (TP-shared)")
    kv_pool_per_gpu = usable_per_gpu - weights_per_gpu if weights_per_gpu is not None else None

    # --- mamba/KDA state pool: it SPLITs the post-weights device budget ----
    # (--mamba-full-memory-ratio: the mamba pool may claim up to ratio x the
    # mem-fraction budget when KV is under pressure, shrinking the KV pool
    # correspondingly; the engine sizes it by demand, we model the cap.)
    mamba_details: dict = {}
    ms = req.mamba
    if ms.active and kv_pool_per_gpu is not None and ms.full_memory_ratio is not None:
        # A mamba/KDA pool can only exist where linear-attention layers exist.
        # A ratio/cache-size on a model with none (DeepSeek-V4.1-Flash: pure
        # MLA+CSA) is inert engine-side (no pool is allocated) — model it the
        # same way instead of splitting the budget against a phantom pool.
        _mix_guard = kvdetails.get("layer_mix")
        _linear_guard = _mix_guard.linear if _mix_guard else 0
        if not _linear_guard:
            warnings.append(
                f"mamba flags ignored: {req.cfg.model_type} has no linear-attention layers — "
                "no mamba/KDA pool exists to reserve (full budget stays in the KV pool)"
            )
            mamba_details = {"ignored": True, "reason": "no linear-attention layers"}
        else:
            ratio = ms.full_memory_ratio
            budget_per_gpu = usable_per_gpu - (weights_per_gpu or 0.0)
            # Engine-faithful sizing (SGLang model_runner_kv_cache_mixin.
            # handle_max_mamba_cache, v0.5.x): the post-weights budget ("rest") is
            # split mamba-vs-KV. The RATIO is mamba:kv, NOT mamba:budget — the
            # engine solves mamba + kv = rest with mamba/kv = ratio, i.e.
            # mamba = rest x ratio/(1+ratio) (0.9 -> 47% of rest, never 90%).
            # --max-mamba-cache-size OVERRIDES the ratio: the pool is then exactly
            # max_cache_size x per-request state. Speculative decoding adds an
            # intermediate reserve of per_req x target_concurrency x draft_tokens.
            # The recurrent state SHARDS across the TP group (verified against the
            # live GLM-5.3-Flash TP4 deployment: 480-seq pool + 96x6 draft reserve
            # => ~23.9 GiB/GPU KV => ~1.72M tokens vs the engine's reported 1.73M).
            _pool_per_gpu: float | None = None
            _per_req = 0.0
            _n_linear = 0
            try:
                from .configs import _linear_state_bytes_per_seq, _ssm_bytes_per_elem, state_bytes_per_elem

                _mix = kvdetails.get("layer_mix")
                _n_linear = _mix.linear if _mix else 0
                _per_req = _linear_state_bytes_per_seq(
                    cfg, state_bytes_per_elem(ssm_dtype_eff) or _ssm_bytes_per_elem(cfg)
                ) * _n_linear
            except Exception:  # pragma: no cover — advisory path must never break an estimate
                _per_req = 0.0
            _tp = max(req.tp_size, 1)
            if ms.max_cache_size and _per_req:
                # explicit cache size: the ratio is ignored (engine elif order)
                _pool_per_gpu = ms.max_cache_size * _per_req / _tp
                mamba_details["max_cache_size"] = ms.max_cache_size
                mamba_details["state_bytes_per_seq"] = _per_req / max(_n_linear, 1)
                mamba_details["linear_layers"] = _n_linear
                mamba_details["sizing"] = "max-mamba-cache-size (ratio overridden)"
            elif _per_req:
                # ratio split: mamba = rest x ratio/(1+ratio)
                _pool_per_gpu = budget_per_gpu * ratio / (1.0 + ratio)
                mamba_details["sizing"] = f"ratio {ratio:g} (mamba:kv split of the post-weights budget)"
            if _pool_per_gpu is not None:
                # speculative decoding: intermediate mamba states for the draft
                # window are reserved ON TOP (engine subtracts them from rest
                # before the KV split); target concurrency = mamba_concurrency
                # (the form's "target mamba concurrency" / --mamba-concurrency)
                _draft = 0
                if spec is not None and spec.active and spec.draft_tokens:
                    _draft = spec.draft_tokens
                _conc = req.mamba_concurrency or 0
                if _draft and _conc and _per_req:
                    _inter = _conc * _draft * _per_req / _tp
                    _pool_per_gpu += _inter
                    mamba_details["spec_intermediate_bytes_per_gpu"] = _inter
                mamba_pool_per_gpu = min(_pool_per_gpu, max(budget_per_gpu, 0.0))
                kv_pool_per_gpu = max(budget_per_gpu - mamba_pool_per_gpu, 0.0)
                mamba_details["pool_bytes_per_gpu"] = mamba_pool_per_gpu
                warnings.append(
                    f"mamba state pool: {_pool_per_gpu / 1024**3:.1f} GiB/GPU "
                    f"({mamba_details.get('sizing', '')}"
                    + (f"; +{_conc}x{_draft} draft-window intermediate" if _draft and _conc else "")
                    + f") — the KV pool is what remains "
                    f"({kv_pool_per_gpu / 1024**3:.1f} GiB/GPU)"
                )
            else:
                mamba_pool_per_gpu = budget_per_gpu * ratio / (1.0 + ratio)
                kv_pool_per_gpu = max(budget_per_gpu - mamba_pool_per_gpu, 0.0)
                mamba_details["pool_bytes_per_gpu"] = mamba_pool_per_gpu
                warnings.append(
                    f"mamba state pool: --mamba-full-memory-ratio {ratio:g} splits the post-weights budget "
                    f"mamba:kv = {ratio:g}:1 -> ~{mamba_pool_per_gpu / 1024**3:.1f} GiB/GPU mamba, "
                    f"{kv_pool_per_gpu / 1024**3:.1f} GiB/GPU KV (per-sequence state unknown: "
                    "pass --max-mamba-cache-size to size the pool exactly)"
                )

    kv_tokens_total: float | None = None
    kv_tokens_per_gpu: float | None = None
    fits: bool | None = None
    concurrency: int | None = None

    if kv_pool_per_gpu is not None:
        # TP x KV-heads interaction (the subtle one). A TP group of P GPUs
        # jointly holds r copies of each token's KV, where
        # r = kv_replication_factor(cfg, tp): 1 when heads shard cleanly,
        # tp/kv_heads when each head is replicated across ranks, tp for
        # single-stream layouts (MQA kv_heads=1 / MLA latents). So
        #     tokens = (summed pool) / (bpt x r),
        # and each additional replica (gpu_count // tp) adds one pool.
        # Toy check: 2xTP sharded, 100B full-stream, 1000B/GPU pool -> 2000/100
        # = 20 tokens (each rank stores 50B x 20 = its whole 1000B pool;
        # dividing the summed pool by the per-rank 50B would claim 40).
        # Classic check: Llama-70B 8xA100 TP8 mf0.9 -> ~1.41M tokens, the
        # engines' reported figure (dividing by bpt/tp claims 11.3M).
        # Partial-shard check: kv_heads=4 on TP8 -> r=2 (each head on 2
        # ranks). At the SAME per-GPU pool TP8 holds the SAME tokens as
        # sharded TP4 (2x the bytes, 2x the copies); with real pools TP8
        # adds a little (weights shard 8-ways). The old binary rule called
        # TP8 fully replicated and undercounted it by 4x.
        # Fully-replicated check: DeepSeek-V4-Flash (kv_heads=1) on 4xH200
        # TP4 @ mf0.8 -> 3.76M vs the engine's 3.67M (2.4% = DSPARK draft KV).
        rep_factor = kv_replication_factor(cfg, req.tp_size)
        kv_tokens_total = kv_pool_per_gpu * req.gpu_count / (kv_bpt * rep_factor)
        # per-GPU figure is the replica total when fully replicated (every
        # rank addresses the whole pool) and the sharded share otherwise
        kv_tokens_per_gpu = (
            kv_tokens_total * rep_factor / req.gpu_count if rep_factor > 1 else kv_tokens_total / req.gpu_count
        )
        # fits must be False when weights alone bust the budget: the mamba
        # split clamps a negative post-weights budget to 0, which would make
        # this check vacuously true (GLM-5.3 743B on 4xH200 printed "fits"
        # with a 0-token pool).
        fits = kv_pool_per_gpu >= 0 and (weights_per_gpu is None or weights_per_gpu <= usable_per_gpu)
        concurrency = math.floor(kv_tokens_total / req.context) if fits and kv_tokens_total is not None else 0
        if not fits and weights_per_gpu is not None:
            warnings.append(
                f"weights ({weights_per_gpu / 1024**3:.1f} GiB/GPU) exceed usable VRAM "
                f"({usable_per_gpu / 1024**3:.1f} GiB/GPU) — no room for KV cache"
            )
        elif kv_tokens_total < req.context:
            concurrency = 0
            warnings.append(
                f"KV pool holds {kv_tokens_total:,.0f} tokens but one request needs "
                f"{req.context:,} — increase TP/GPUs or reduce context"
            )

    # --- mamba state adequacy: can the reserved pool carry the target ------
    # concurrency? (state is PER SEQUENCE: bytes_per_seq x layers x C must
    # fit the reserved pool -- a separate ceiling from the KV-token pool.)
    if mamba_details and fits:
        try:
            from .configs import _linear_state_bytes_per_seq, _ssm_bytes_per_elem, state_bytes_per_elem

            mix0 = kvdetails.get("layer_mix")
            n_linear = mix0.linear if mix0 else 0
            # use the RECONCILED dtype (ssm_dtype_eff), not ms.ssm_dtype:
            # a caller who set only EstimateRequest.ssm_dtype must not have
            # this check silently fall back to the config default
            per_seq = _linear_state_bytes_per_seq(cfg, state_bytes_per_elem(ssm_dtype_eff) or _ssm_bytes_per_elem(cfg))
            if n_linear and per_seq:
                pool = mamba_details["pool_bytes_per_gpu"] * max(1, req.gpu_count // req.tp_size)
                target_c = req.mamba_concurrency or (concurrency or 0)
                need = per_seq * n_linear * max(target_c, 1)
                mamba_details["state_bytes_per_seq"] = per_seq
                mamba_details["linear_layers"] = n_linear
                mamba_details["seqs_supported"] = pool / (per_seq * n_linear) if per_seq else None
                if need > pool:
                    carried = int(pool / (per_seq * n_linear))
                    warnings.append(
                        f"mamba pool too small for {target_c} concurrent sequences: "
                        f"{per_seq * n_linear / 1024**2:.0f} MiB/seq x {n_linear} layers x {target_c} = "
                        f"{need / 1024**3:.1f} GiB needed vs {pool / 1024**3:.1f} GiB reserved "
                        f"(--mamba-full-memory-ratio carries ~{carried} sequences)"
                    )
        except Exception:  # pragma: no cover — advisory check must never break an estimate
            pass

    tiers = _cache_tiers(
        req,
        kv_bpt,
        kv_pool_per_gpu if fits is not False else None,
        wbytes is not None,
        kv_tokens_total if fits else None,
    )
    if tiers.l2_tokens is not None and tiers.l2_bytes_per_replica:
        warnings.append(
            f"HiCache L2: {tiers.l2_bytes_per_replica / 1024**3:.1f} GiB host RAM per replica "
            f"holds ~{tiers.l2_tokens / 1e6:.2f}M cached tokens"
            + (f" (L3: ~{tiers.l3_tokens / 1e6:.2f}M more)" if tiers.l3_tokens else "")
        )

    # the DeepSeek-V4 lesson, made structural: any config field this family's
    # storage model does not price gets a loud warning (unparsed indexer /
    # compression schedules have shifted KV figures by 3-27x in practice)
    try:
        from .configs import unmodeled_structural_fields

        unmodeled = unmodeled_structural_fields(req.cfg, kvdetails)
        kvdetails.pop("_consumed_fields", None)
        if unmodeled:
            warnings.append(
                "config carries structural fields this storage model does not price: "
                + "; ".join(unmodeled)
                + " — the KV figure may be materially off; check the family's paper"
            )
    except Exception:  # pragma: no cover — warning path must never break an estimate
        pass

    return EstimateResult(
        request=req,
        weights_bytes=wbytes,
        kv_bytes_per_token=kv_bpt,
        usable_per_gpu=usable_per_gpu,
        weights_per_gpu=weights_per_gpu,
        kv_pool_per_gpu=kv_pool_per_gpu,
        kv_tokens_total=kv_tokens_total,
        kv_tokens_per_gpu=kv_tokens_per_gpu,
        fits=fits,
        concurrency=concurrency,
        details={"weights": wdetails, "kv": kvdetails, "engram": engram_details, "mamba": mamba_details},
        warnings=warnings,
        tiers=tiers,
    )


# ---------------------------------------------------------------------------
# grids: the sizing sweep people actually want
# ---------------------------------------------------------------------------


@dataclass
class GridCell:
    tp: int
    gpus: int
    context: int
    fits: bool | None
    kv_tokens: float | None
    concurrency: int | None


def grid(
    cfg: ModelConfig,
    gpu: GpuSpec,
    gpu_counts: list[int],
    tp_sizes: list[int],
    contexts: list[int],
    weight_dtype: str | None = None,
    kv_dtype: str | None = None,
    mem_fraction: float = 0.9,
    overhead_gib: float = DEFAULT_OVERHEAD_GIB,
    speculative: SpeculativeSpec | None = None,
    moe_runner: str | None = None,
    hicache: HicacheSpec | None = None,
    ep_size: int = 1,
    pp_size: int = 1,
) -> list[GridCell]:
    """Cross-product of (gpus, tp, context) -> capacity cells, ordered."""
    cells: list[GridCell] = []
    seen: set[tuple[int, int]] = set()
    for g in gpu_counts:
        for tp in tp_sizes:
            if tp > g or tp < 1 or (g, tp) in seen:
                continue
            seen.add((g, tp))
            res = run_estimate(
                EstimateRequest(
                    cfg=cfg,
                    gpu=gpu,
                    gpu_count=g,
                    tp_size=tp,
                    weight_dtype=weight_dtype,
                    kv_dtype=kv_dtype,
                    mem_fraction=mem_fraction,
                    overhead_gib=overhead_gib,
                    context=max(contexts) if contexts else 32768,
                    speculative=speculative,
                    moe_runner=moe_runner,
                    hicache=hicache or HicacheSpec.off(),
                    ep_size=ep_size,
                    pp_size=pp_size,
                )
            )
            for ctx in contexts:
                conc = math.floor(res.kv_tokens_total / ctx) if res.fits and res.kv_tokens_total else 0
                cells.append(
                    GridCell(tp=tp, gpus=g, context=ctx, fits=res.fits, kv_tokens=res.kv_tokens_total, concurrency=conc)
                )
    return cells


def resolve_dtype_weight(cfg: ModelConfig, explicit: str | None) -> str | None:
    return explicit or default_weight_dtype(cfg)


def summary_line(res: EstimateResult) -> str:
    """One-line human verdict, e.g. ``fits: yes | KV pool: 1.42M tokens | 43 x 32k-ctx requests``."""
    name = f"{res.request.tp_size}x{res.request.gpu.name}" if res.request.tp_size > 1 else res.request.gpu.name
    if res.request.pp_size > 1:
        name += f" PP{res.request.pp_size}"
    if res.fits is None:
        return f"{name}: unknown weights size (dtype?) · KV/token {res.kv_bytes_per_token / 1024:.1f} KiB"
    if not res.fits:
        return f"{name}: DOES NOT FIT (weights exceed usable VRAM)"
    conc = res.concurrency or 0
    ctx = res.request.context
    if res.kv_tokens_total is None:
        return f"{name}: fits (pool size unknown — weights dtype?)"
    line = (
        f"{name}: fits · KV pool {res.kv_tokens_total / 1e6:.2f}M tokens "
        f"· {conc} concurrent x {ctx / 1024:.0f}k-ctx requests"
    )
    t = res.tiers
    if t and (t.l2_tokens or t.l3_tokens):
        parts = []
        if t.l2_tokens:
            parts.append(f"L2 {t.l2_tokens / 1e6:.2f}M")
        if t.l3_tokens:
            parts.append(f"L3 {t.l3_tokens / 1e6:.2f}M")
        if t.total_tokens:
            parts.append(f"total {t.total_tokens / 1e6:.2f}M")
        line += " · HiCache " + ", ".join(parts)
    return line


def kv_label(res: EstimateResult) -> str:
    return f"{res.kv_bytes_per_token / 1024:.1f} KiB/token ({dtype_label(res.request.kv_dtype)})"
