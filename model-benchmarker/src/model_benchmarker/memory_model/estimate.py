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
class EstimateRequest:
    """One estimation scenario."""

    cfg: ModelConfig
    gpu: GpuSpec
    gpu_count: int = 1
    tp_size: int = 1
    pp_size: int = 1
    weight_dtype: str | None = None
    kv_dtype: str | None = None
    mem_fraction: float = 0.9
    overhead_gib: float = DEFAULT_OVERHEAD_GIB
    context: int = 32768
    # speculative decoding: DFLASH draft model / MTP-family draft layers
    speculative: SpeculativeSpec | None = None
    # hierarchical KV cache tiers (L2 host RAM / L3 backing store)
    hicache: HicacheSpec = field(default_factory=HicacheSpec.off)
    # expert-parallel size: experts shard ep-ways (attention stays TP)
    ep_size: int = 1
    # MoE runner backend (flashinfer_mxfp4/marlin store experts at 4-bit)
    moe_runner: str | None = None

    def __post_init__(self) -> None:
        self.tp_size = min(self.tp_size, self.gpu_count)  # TP cannot exceed the GPU count
        self.pp_size = min(self.pp_size, self.gpu_count)  # PP cannot exceed the GPU count


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
        return ep_bytes + dense_bytes
    return wbytes / stage


def _tp_replicated_kv(cfg: ModelConfig, tp_size: int) -> bool:
    """True when the device KV layout is TP-REPLICATED: the pool cannot shard
    because the per-token stream lives once per rank (MQA/MLA latent streams).

    The two structural cases:
    - ``kv_heads < tp`` (MQA on wide TP): a single KV head cannot shard, so
      engines replicate it on every rank.
    - MLA families: the compressed latent is ONE stream per token (the
      config's ``num_key_value_heads`` counts projection heads, not latent
      streams), so the latent is replicated across TP ranks regardless of tp.
    """
    mt = cfg.model_type or ""
    if mt.startswith(("deepseek_v2", "deepseek_v3")) or "dsa" in mt:
        return True  # MLA latent stream: one per token per rank
    kv_heads = cfg.num_key_value_heads if cfg.num_key_value_heads is not None else cfg.num_attention_heads
    return (kv_heads or 1) < tp_size


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
    tp_replicated = _tp_replicated_kv(req.cfg, req.tp_size) if h.l2_tp_replicated is None else h.l2_tp_replicated
    notes: list[str] = []

    pool_gib = (kv_pool_per_gpu or 0.0) / 1024**3
    if h.l2_mode == "ratio":
        # per-replica L2 bytes: ratio x the device pool, in the engine's layout
        # (sharded: every rank's pool = ratio x that rank's device pool;
        #  replicated/MLA: write-back dedupes to one copy = one rank's pool)
        l2_gib = pool_gib * h.l2_ratio * (1 if tp_replicated else req.tp_size)
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
    if tp_replicated:
        notes.append(
            "MLA/MQA layout: host tier stores ONE deduplicated stream per token (per-rank pools hold the full stream; write-back dedupes)"
        )
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
    wbytes, wdetails = weight_bytes_estimate(cfg, req.weight_dtype, req.moe_runner)
    kv_bpt, kvdetails = kv_bytes_per_token(cfg, req.kv_dtype)
    warnings: list[str] = wdetails.get("notes", [])
    if kvdetails.get("note"):
        # family notes (e.g. the linear-attention per-sequence state pool)
        # ride the warnings block -- they qualify the headline number
        warnings.append(str(kvdetails["note"]))
    if req.pp_size > 1:
        warnings.append(
            f"pipeline parallel PP{req.pp_size}: weights stage-split (x{req.pp_size} lighter per GPU); "
            "the KV pool shards across the TP x PP group exactly once (each token's KV lives on its layer's stage)"
        )

    spec = req.speculative
    draft_weights_bytes: float | None = None
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
    weights_per_gpu = _weights_per_gpu(wbytes, req) if wbytes is not None else None
    if weights_per_gpu is not None and draft_weights_bytes is not None:
        # the DFLASH draft model is fully resident alongside the target
        weights_per_gpu = weights_per_gpu + draft_weights_bytes / req.tp_size
        warnings.append(f"draft weights add {draft_weights_bytes / req.tp_size / 1024**3:.1f} GiB/GPU (TP-shared)")
    kv_pool_per_gpu = usable_per_gpu - weights_per_gpu if weights_per_gpu is not None else None

    kv_tokens_total: float | None = None
    kv_tokens_per_gpu: float | None = None
    fits: bool | None = None
    concurrency: int | None = None

    if kv_pool_per_gpu is not None:
        # TP x KV-heads interaction (the subtle one). A TP group of P GPUs
        # jointly holds ONE copy of each token's KV when the layout shards,
        # so sharded capacity = (summed pool) / (full-model bytes/token):
        #     tokens = pool_per_gpu x gpus / bpt
        # Toy check: 2xTP, 100B full-stream, 1000B/GPU pool -> 2000/100 = 20
        # tokens (each rank stores 50B x 20 = its whole 1000B pool; dividing
        # the summed pool by the per-rank 50B would claim 40).
        # Classic check: Llama-70B 8xA100 TP8 mf0.9 -> ~1.41M tokens, the
        # engines' reported figure (dividing by bpt/tp claims 11.3M).
        # Replicated layouts (MQA on wide TP / MLA latent streams) hold the
        # full stream on EVERY rank: one replica = pool/bpt tokens, and each
        # additional replica (gpu_count // tp) adds one pool's worth.
        replicated = _tp_replicated_kv(cfg, req.tp_size)
        if not replicated:
            # sharded: the TP group jointly stores ONE copy of each token
            # (bpt/tp on every rank), and each replica adds its own pool.
            # capacity = (summed pool) / (full-stream bytes/token).
            # Toy check: 2xTP, 100B full-stream, 1000B/GPU pool -> 2000/100
            # = 20 tokens (each rank stores 50B x 20 = its whole 1000B pool;
            # dividing the summed pool by the per-rank 50B would claim 40).
            # Classic check: Llama-70B 8xA100 TP8 mf0.9 -> ~1.41M tokens (the
            # engines' reported figure; the prior bpt/tp divisor said 11.3M).
            kv_tokens_total = kv_pool_per_gpu * req.gpu_count / kv_bpt
            kv_tokens_per_gpu = kv_tokens_total / req.gpu_count
        else:
            # replicated (MQA on wide TP / MLA latent): every rank of a
            # replica holds the full stream -- wide TP adds speed, not
            # tokens; additional replicas each add one pool's worth.
            # kv_tokens_per_gpu stays the replica total: EVERY rank can
            # address the whole pool (that is what replication means).
            # Validated: DeepSeek-V4-Flash (kv_heads=1) on 4xH200 TP4 @
            # mf0.8 -> 3.76M vs the engine's 3.67M (2.4% = DSPARK draft KV).
            kv_tokens_total = kv_pool_per_gpu * max(1, req.gpu_count // req.tp_size) / kv_bpt
            kv_tokens_per_gpu = kv_tokens_total
        fits = kv_pool_per_gpu >= 0
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
        details={"weights": wdetails, "kv": kvdetails},
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
