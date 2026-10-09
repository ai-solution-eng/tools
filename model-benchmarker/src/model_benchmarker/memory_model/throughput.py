"""First-order throughput estimate (tokens/second) for a serving deployment.

Deterministic roofline math — BOUNDS, not a benchmark:

    decode (memory-bandwidth-bound, per aggregated step):
        bytes_moved_per_step/rank = weights_active/tp + B x kv_read x r/tp
        tok/s                     = B x eta x bw / bytes_moved_per_step
        (each step produces ~1 token per sequence; batch B amortizes the
         weight read across sequences; r = kv_replication_factor -- MLA/MQA
         latents are replicated per rank, GQA shards)

    prefill (compute-bound):
        tok/s = mfu x flops x (gpu_count // tp) / (2 x active_params)

BOUND DIRECTION: prefill is an UPPER bound (MFU ceiling). Decode with the
full KV stream priced per step is a LOWER bound at long context for
sparse/indexed-attention families (the indexer reads only top-k blocks) and
approaches an upper bound at short context. Quote decode with the context
length attached.

``active`` is the per-token parameter set (MoE: only the routed top-k
experts + shared experts + attention + embeddings are READ per token —
the standard approximation; at large batch the router saturates and the
whole expert set is read, which `batch_saturation_point()` surfaces).

Everything is deliberately first-order: no kernel efficiency curves, no
MLA absorb/non-absorb split, no chunked-prefill overlap, no speculative
acceptance modeling (draft_tokens are surfaced as an optimistic
multiplier only). The labels say so. Calibrate `eta`/`mfu` against a live
server before trusting any number to two significant figures.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .configs import ModelConfig
from .dtypes import weight_bytes_per_param
from .gpus import GpuSpec

# deliverable efficiency factors (fraction of the hardware peak a well-tuned
# serving engine reaches). Calibratable: these are the two knobs that move
# real deployments off the roofline.
DEFAULT_DECODE_BW_EFFICIENCY = 0.7  # HBM bandwidth utilization during decode
DEFAULT_PREFILL_MFU = 0.5  # model flops utilization during prefill


@dataclass
class ThroughputRequest:
    """One throughput scenario (mirrors the memory EstimateRequest knobs)."""

    cfg: ModelConfig
    gpu: GpuSpec
    gpu_count: int = 1
    tp_size: int = 1
    ep_size: int = 1
    weight_dtype: str | None = None
    context: int = 32768
    batch: int = 1
    decode_bw_efficiency: float = DEFAULT_DECODE_BW_EFFICIENCY
    prefill_mfu: float = DEFAULT_PREFILL_MFU
    # speculative decoding: accepted tokens per step (optimistic ceiling =
    # draft_tokens); multiplies the decode token rate, adds nothing to bytes
    draft_tokens: int = 0
    # compressed-state / SSM storage dtypes carried through for KV-read math
    # (the KV bytes/token itself comes from estimate.kv_bytes_per_token)
    kv_bytes_per_token: float = 0.0  # full-model stream, computed by the caller


@dataclass
class ThroughputResult:
    """Everything the artifact prints for one throughput scenario."""

    decode_tokens_per_sec: float | None  # aggregate over the batch
    decode_per_seq: float | None  # per sequence (the latency users feel)
    prefill_tokens_per_sec: float | None  # aggregate
    active_params: int
    active_params_note: str
    bytes_moved_per_step: float
    kv_read_bytes_per_seq: float
    weights_bytes_per_gpu: float
    batch_saturation: int | None  # B at which all experts are read
    # sparse/indexed families only: the OPTIMISTIC decode when the selector
    # reads only its top-k selected entries instead of the full stored stream
    decode_tokens_per_sec_optimistic: float | None = None
    sparse_select_tokens: int | None = None  # tokens' entries the selector reads
    notes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def sparse_select_tokens(cfg: ModelConfig) -> int | None:
    """Tokens' worth of KV entries a sparse/indexed attention layer actually
    READS per decode step, or None when the family has no sparse selector.

    Sources: ``index_topk`` (DeepSeek V4 CSA/HCA + V4.1 CSA2, GLM DSA) scaled
    by the selector's block granularity when the config names one
    (``candidate_block_size``), else the lightning indexer's
    ``indexer_budget`` (Qwen3-Next / qwen4_exp). This is an ENGINE-DEPENDENT
    optimistic bound: kernels that fall back to dense attention read the full
    stream instead (the conservative figure the headline uses).
    """
    raw = cfg.raw.get("text_config", cfg.raw) if isinstance(cfg.raw, dict) else {}
    raw = raw if isinstance(raw, dict) else {}
    topk = cfg.index_topk or raw.get("index_topk")
    if topk:
        try:
            block = int(raw.get("candidate_block_size") or 1)
        except (TypeError, ValueError):
            block = 1
        try:
            return max(1, int(topk) * max(1, block))
        except (TypeError, ValueError):
            return None
    budget = raw.get("indexer_budget")
    if budget:
        try:
            return max(1, int(budget))
        except (TypeError, ValueError):
            return None
    return None


def active_params_per_token(cfg: ModelConfig) -> tuple[int, str]:
    """Parameters READ per token (not resident): attention + dense MLPs +
    top-k routed experts + shared experts + (un)tied embeddings.

    The MoE routed-expert term uses ``num_experts_per_tok`` x 3 matrices
    (gate/up/down). Verified against the vendor figure: DeepSeek-V3 shape
    -> ~36.5B active vs the published 37B.
    """
    h = cfg.hidden_size or 0
    n = cfg.num_hidden_layers or 0
    if not (h and n):
        return 0, "config missing hidden_size / num_hidden_layers"
    n_heads = cfg.num_attention_heads or 0
    n_kv = cfg.num_key_value_heads if cfg.num_key_value_heads is not None else n_heads
    head_dim = cfg.head_dim or (h // n_heads if n_heads else 0)

    # attention block per layer (matches count_parameters' shapes)
    if cfg.kv_lora_rank and cfg.qk_rope_head_dim is not None:
        q_lora = cfg.q_lora_rank or h
        qk_dim = (cfg.qk_rope_head_dim or 0) + (cfg.qk_nope_head_dim or 0)
        v_dim = cfg.v_head_dim or (cfg.qk_nope_head_dim or head_dim)
        attn = (
            h * q_lora
            + q_lora * n_heads * qk_dim
            + h * (cfg.kv_lora_rank + (cfg.qk_rope_head_dim or 0))
            + cfg.kv_lora_rank * n_heads * ((cfg.qk_nope_head_dim or 0) + v_dim)
            + n_heads * v_dim * h
        )
    else:
        qk_dim = head_dim
        v_dim = cfg.v_head_dim or head_dim
        attn = h * (n_heads * qk_dim) + 2 * h * (n_kv * head_dim) + n_heads * v_dim * h

    moe_idx = _moe_layer_indices_active(cfg)
    inter = cfg.moe_intermediate_size or cfg.intermediate_size or 4 * h
    experts = cfg.experts or 1
    top_k = cfg.num_experts_per_tok or 1
    mlp_active = 0
    routed = 0
    for i in range(n):
        if i in moe_idx:
            routed += top_k * 3 * h * inter
            if cfg.n_shared_experts:
                shared_inter = cfg.shared_expert_intermediate_size or inter
                mlp_active += cfg.n_shared_experts * 3 * h * shared_inter
            routed += h * experts  # router gate
        else:
            mlp_active += 3 * h * inter

    params = cfg.vocab_size * h  # embedding read (lm_head read at the last step too)
    if not cfg.tie_word_embeddings:
        params += cfg.vocab_size * h
    total = params + n * attn + mlp_active + routed + 2 * h
    note = (
        f"{top_k} of {experts} routed experts active per token" if cfg.is_moe else "dense model: all MLP weights active"
    )
    return total, note


def top_k_label(cfg: ModelConfig) -> str:
    return str(cfg.num_experts_per_tok or 1)


def _moe_layer_indices_active(cfg: ModelConfig) -> set[int]:
    """Same layer split count_parameters uses (import to stay in sync)."""
    from .configs import _moe_layer_indices

    return _moe_layer_indices(cfg)


def batch_saturation_point(cfg: ModelConfig) -> int | None:
    """Batch size at which the router likely touches EVERY routed expert in
    a step (B x top_k >= n_experts): beyond it the per-step weight read
    grows from top-k experts toward the whole expert set."""
    if not cfg.is_moe:
        return None
    experts = cfg.experts or 1
    top_k = cfg.num_experts_per_tok or 1
    return max(1, math.ceil(experts / max(top_k, 1)))


def estimate_throughput(req: ThroughputRequest) -> ThroughputResult:
    """Run one throughput scenario (roofline; see module docstring)."""
    cfg = req.cfg
    active, note = active_params_per_token(cfg)
    if not active:
        return ThroughputResult(
            decode_tokens_per_sec=None,
            decode_per_seq=None,
            prefill_tokens_per_sec=None,
            active_params=0,
            active_params_note=note,
            bytes_moved_per_step=0.0,
            kv_read_bytes_per_seq=0.0,
            weights_bytes_per_gpu=0.0,
            batch_saturation=batch_saturation_point(cfg),
            warnings=[f"throughput unknown: {note}"],
        )

    dtype = req.weight_dtype or "bf16"
    bpp = weight_bytes_per_param(dtype) or 2.0
    stage = max(1, req.tp_size)
    weights_active_bytes = active * bpp / stage  # per-GPU read share per step

    # KV read per sequence: the full-model stream at this context. CSA
    # families store less but still READ their stored entries per step; the
    # conservative read is the stored stream (indexer reads only top-k
    # blocks at long context -- surfaced as a note, not modeled).
    kv_read = req.kv_bytes_per_token * req.context if req.kv_bytes_per_token else 0.0

    batch = max(1, req.batch)
    # per-rank per-step KV read follows the REPLICATION SPECTRUM (the memory
    # side's kv_replication_factor): sharded GQA reads its 1/tp share, but
    # MLA latents / MQA single streams are replicated r=tp-ways -- each rank
    # reads its FULL copy every step (missing this overstated DSV3 4xH200
    # TP4 decode ~3x). bytes/step per rank = weights_share + B x kv_read x r/tp.
    from .estimate import kv_replication_factor

    rep_factor = kv_replication_factor(cfg, req.tp_size)
    bytes_step = weights_active_bytes + batch * kv_read * rep_factor / max(stage, 1)
    bw = req.gpu.mem_bw_gbps * 1e9  # GB/s -> B/s
    eff_bw = req.decode_bw_efficiency * bw
    decode = batch * eff_bw / bytes_step if bytes_step > 0 else 0.0
    if req.draft_tokens > 1:
        # speculative: ~draft_tokens accepted tokens per forward pass at the
        # SAME weight-read cost (optimistic ceiling; real acceptance < 1)
        decode *= req.draft_tokens

    flops_tf = getattr(req.gpu, "peak_tflops_bf16", 0) or 0
    flops = flops_tf * 1e12
    prefill = None
    if flops:
        # each TP stage holds 1/tp of the weights and computes with its own
        # FLOPS; additional PIPELINE stages / replicas add more independent
        # groups -- count only whole tp-groups, not raw gpu_count (a
        # PP2 x 2-replica 8-GPU box runs 4 groups of TP2, not TP8 FLOPS).
        groups = max(1, req.gpu_count // max(stage, 1))
        prefill = req.prefill_mfu * flops * groups / (2.0 * active)

    # --- sparse-selector OPTIMISTIC decode (indexed families only) ----------
    # The headline prices the full stored stream per step (a LOWER bound at
    # long context for CSA/DSA/indexer families). When the config names a
    # selector, the optimistic figure prices only the selected entries:
    # kv_read_sparse = bpt x select_tokens (capped at the full stream). The
    # truth for a real engine sits between the two, so BOTH are published.
    select_tokens = sparse_select_tokens(cfg)
    decode_optimistic: float | None = None
    if select_tokens and kv_read > 0:
        sparse_kv = min(kv_read, req.kv_bytes_per_token * select_tokens)
        bytes_step_sparse = weights_active_bytes + batch * sparse_kv * rep_factor / max(stage, 1)
        decode_optimistic = batch * eff_bw / bytes_step_sparse if bytes_step_sparse > 0 else None
        if decode_optimistic is not None and req.draft_tokens > 1:
            decode_optimistic *= req.draft_tokens

    warnings: list[str] = []
    sat = batch_saturation_point(cfg)
    if sat and batch >= sat:
        warnings.append(
            f"batch {batch} >= the router saturation point (~{sat}): the step likely reads "
            "MOST routed experts, not just top-k — decode ceiling is optimistic past this point"
        )
    kv_read_per_rank = batch * kv_read * rep_factor / max(stage, 1)
    if kv_read_per_rank > weights_active_bytes:
        warnings.append(
            f"KV reads dominate at {req.context:,}-ctx ({kv_read_per_rank / 1e9:.2f} GB/step/rank vs "
            f"{weights_active_bytes / 1e9:.2f} GB weights/rank"
            + (f", replicated x{rep_factor} across TP{req.tp_size}" if rep_factor > 1 else "")
            + ") — long-context decode is KV-bandwidth-bound; smaller contexts raise the ceiling"
        )
    if req.draft_tokens > 1:
        warnings.append(
            f"speculative decoding: rate multiplied x{req.draft_tokens} (the draft-tokens "
            "ceiling; real acceptance is lower)"
        )

    # FP4/MXFP4 expert storage: dequantization is NATIVE on Blackwell-class
    # parts (B200/RTX PRO 6000) but emulated (marlin-style kernels) on
    # Hopper -- the roofline sees the same bytes either way, but the
    # emulated path gives up kernel efficiency the eta factor can't see.
    if (
        bpp <= 0.5
        and req.gpu.peak_tflops_bf16
        and "rtx" not in req.gpu.name.lower()
        and "b200" not in req.gpu.name.lower()
    ):
        warnings.append(
            f"{dtype} weights on {req.gpu.name}: 4-bit dequantization is emulated on "
            "Hopper-class hardware (marlin-style kernels) — the real rate sits further below "
            "this roofline than the eta factor assumes"
        )

    # sparse-selector families: publish BOTH bounds so the reader sees the
    # spread the engine's kernel choice lands inside
    if decode_optimistic is not None and select_tokens:
        warnings.append(
            f"sparse selector ({select_tokens:,} tokens' entries read per step): the headline decode "
            f"prices the FULL stored stream; with selection the ceiling is ~{decode_optimistic:,.0f} tok/s. "
            "The engine's real rate sits between the two — compare against a live server's "
            "max_total_num_tokens and measured tok/s"
        )

    return ThroughputResult(
        decode_tokens_per_sec=decode,
        decode_per_seq=decode / batch if decode else None,
        prefill_tokens_per_sec=prefill,
        active_params=active,
        active_params_note=note,
        bytes_moved_per_step=bytes_step,
        kv_read_bytes_per_seq=kv_read,
        weights_bytes_per_gpu=weights_active_bytes,
        batch_saturation=sat,
        decode_tokens_per_sec_optimistic=decode_optimistic,
        sparse_select_tokens=select_tokens if decode_optimistic is not None else None,
        notes=[
            (
                "first-order roofline: decode = B x eta x BW / (weights/rank + B x KV-read/rank); "
                f"prefill = {int(req.prefill_mfu * 100)}% MFU x peak FLOPS / (2 x active params)"
            ),
            (
                "bound direction: prefill is an UPPER bound (MFU ceiling); decode is a LOWER "
                "bound at long context (the full KV stream is priced per step — sparse/indexed "
                "attention reads less) and approaches an upper bound at short context"
            ),
            (
                f"batch {batch} is the modeled decode batch (a representative target, not this "
                "deployment's setting); figures are PER REPLICA (tp x pp x replicas within "
                f"{req.gpu_count} GPUs)"
            ),
            (
                "calibrate decode_bw_efficiency / prefill_mfu against a live "
                "server before quoting these to two significant figures"
            ),
        ],
        warnings=warnings,
    )


def summary_throughput(res: ThroughputResult) -> str:
    """One-line human verdict for the artifact."""
    if res.decode_tokens_per_sec is None:
        return "throughput: unknown (active params unresolvable)"
    dec = res.decode_tokens_per_sec
    per_seq = res.decode_per_seq or 0
    pre = res.prefill_tokens_per_sec
    line = f"decode ~{dec:,.0f} tok/s aggregate ({per_seq:,.0f}/seq)"
    if res.decode_tokens_per_sec_optimistic:
        line += f" (sparse-selector ceiling ~{res.decode_tokens_per_sec_optimistic:,.0f})"
    if pre:
        line += f" · prefill ceiling ~{pre:,.0f} tok/s"
    return line
