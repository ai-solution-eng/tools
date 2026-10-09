"""Tests for model_benchmarker.memory_model — the deterministic memory
estimator (weights + KV cache + capacity math) and its artifact/report path.

All fixtures are synthetic config dicts: no network, no local model dirs.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from model_benchmarker.memory_model import configs as C
from model_benchmarker.memory_model import estimate as E
from model_benchmarker.memory_model import gpus as G
from model_benchmarker.memory_model.cli import build_payload
from model_benchmarker.memory_model.report import render_markdown

# ---------------------------------------------------------------------------
# fixtures: synthetic configs shaped like the real families
# ---------------------------------------------------------------------------


def llama8b() -> dict:
    """Llama-3-8B shape: 32 heads / 8 kv heads / 128 head_dim / 32 layers."""
    return {
        "model_type": "llama",
        "hidden_size": 4096,
        "num_hidden_layers": 32,
        "num_attention_heads": 32,
        "num_key_value_heads": 8,
        "head_dim": 128,
        "vocab_size": 128256,
        "intermediate_size": 14336,
        "torch_dtype": "bfloat16",
    }


def deepseek_v3() -> dict:
    """DeepSeek-V3 shape: MLA."""
    return {
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
        "num_experts_per_tok": 8,
        "moe_intermediate_size": 2048,
        "first_k_dense_replace": 3,
        "num_nextn_predict_layers": 1,
    }


def sliding_mix() -> dict:
    """Gemma-ish 5:1 sliding/full mix."""
    return {
        "model_type": "gemma4",
        "hidden_size": 2304,
        "num_hidden_layers": 30,
        "num_attention_heads": 8,
        "num_key_value_heads": 4,
        "head_dim": 256,
        "sliding_window": 512,
        "vocab_size": 262208,
        "intermediate_size": 36864,
        "layer_types": ["sliding_attention"] * 25 + ["full_attention"] * 5,
    }


def hybrid_linear() -> dict:
    """Qwen3-Next shape: 3:1 linear/full, no explicit layer_types."""
    return {
        "model_type": "qwen3_next",
        "hidden_size": 2048,
        "num_hidden_layers": 48,
        "num_attention_heads": 16,
        "num_key_value_heads": 2,
        "head_dim": 256,
        "full_attention_interval": 4,
        "linear_key_head_dim": 128,
        "linear_value_head_dim": 128,
        "linear_num_key_heads": 16,
        "linear_num_value_heads": 32,
        "conv_kernel": 4,
        "vocab_size": 151936,
    }


H200 = G.resolve_gpu("H200")
RTX = G.resolve_gpu("RTX Pro 6000")


# ---------------------------------------------------------------------------
# GPU DB + dtypes
# ---------------------------------------------------------------------------


def test_gpu_db_matches_free_text():
    assert G.resolve_gpu("NVIDIA H200 (PCIe)").vram_gib == 141.0
    assert G.resolve_gpu("h200 sxm").name == "NVIDIA H200 SXM"
    assert G.resolve_gpu("RTX PRO 6000 Blackwell").vram_gib == 96.0
    assert G.resolve_gpu("4x L40S").vram_gib == 48.0
    assert G.resolve_gpu("totally-unknown-gpu") is None
    assert G.resolve_gpu(None) is None


def test_dtype_tables():
    from model_benchmarker.memory_model.dtypes import (
        dtype_label,
        kv_bytes_per_elem,
        weight_bytes_per_param,
    )

    assert weight_bytes_per_param("BF16") == 2.0
    assert weight_bytes_per_param("fp8_e4m3") == 1.0
    assert weight_bytes_per_param("nvfp4") == 0.5
    assert weight_bytes_per_param("mxfp8") is None
    assert kv_bytes_per_elem("fp8_e4m3") == 1.0
    assert kv_bytes_per_elem("bfloat16") == 2.0
    assert dtype_label("fp8_e4m3") == "FP8 (e4m3)"


# ---------------------------------------------------------------------------
# KV per-token math — the heart of the tool
# ---------------------------------------------------------------------------


def test_gqa_kv_per_token():
    """Llama-3-8B: 2 * 8 kv-heads * 128 * 2B = 4096 B/layer = 128 KiB/token."""
    cfg = C.config_from_dict(llama8b())
    bpt, det = C.kv_bytes_per_token(cfg, "bf16")
    assert bpt == 32 * 4096
    assert det["arch"] == "GQA"
    assert det["layer_mix"].label() == "32 full"


def test_gqa_f16_vs_fp8_halves():
    cfg = C.config_from_dict(llama8b())
    b16, _ = C.kv_bytes_per_token(cfg, "fp16")
    b8, _ = C.kv_bytes_per_token(cfg, "fp8_e4m3")
    assert b8 * 2 == b16


def test_mla_kv_per_token():
    """DeepSeek-V3 MLA: (512 + 64) B/layer fp16 -> 61 layers = 68.25 KiB/token;
    fp8 KV halves it to ~34 KiB."""
    cfg = C.config_from_dict(deepseek_v3())
    b16, det = C.kv_bytes_per_token(cfg, "bf16")
    assert det["arch"].startswith("MLA")
    assert b16 == 61 * (512 + 64) * 2
    b8, _ = C.kv_bytes_per_token(cfg, "fp8_e4m3")
    assert b8 * 2 == b16


def test_sliding_window_layer_mix():
    cfg = C.config_from_dict(sliding_mix())
    _, det = C.kv_bytes_per_token(cfg, "bf16")
    mix = det["layer_mix"]
    assert mix.full == 5 and mix.sliding == 25 and mix.linear == 0
    assert mix.kv_layers == 30  # conservative: sliding still budgeted
    assert "sliding" in det["arch"]


def test_hybrid_linear_layers_excluded():
    """Qwen3-Next: linear layers carry no per-token KV."""
    cfg = C.config_from_dict(hybrid_linear())
    _, det = C.kv_bytes_per_token(cfg, "bf16")
    mix = det["layer_mix"]
    assert mix.linear == 36 and mix.full == 12
    assert mix.kv_layers == 12
    # per-token = 12 kv layers * 2 * 2 kv-heads * 256 * 2B
    bpt, _ = C.kv_bytes_per_token(cfg, "bf16")
    assert bpt == 12 * 2 * 2 * 256 * 2


# ---------------------------------------------------------------------------
# parameter counting
# ---------------------------------------------------------------------------


def test_dense_param_count_matches_reference():
    """Llama-3-8B reference shape -> ~8.03B parameters."""
    cfg = C.config_from_dict(llama8b())
    params, _ = C.count_parameters(cfg)
    assert abs(params / 1e9 - 8.03) < 0.05


def test_moe_counts_all_experts():
    """MoE weights count every expert (all resident)."""
    cfg = C.config_from_dict(deepseek_v3())
    params, notes = C.count_parameters(cfg)
    # official 671B includes the MTP module (~0.6B) which we exclude
    assert 650e9 < params < 690e9
    assert any("MTP" in n for n in notes)


def test_weight_bytes_dtype_from_config():
    cfg = C.config_from_dict({**llama8b(), "quantization_config": {"quant_method": "fp8"}})
    assert C.default_weight_dtype(cfg) == "fp8"
    b, det = C.weight_bytes_estimate(cfg, None)
    assert b == pytest.approx(params := det["params"] * 1.0)
    assert params * 1.0 == det["bytes"]


def test_weight_bytes_unknown_dtype_is_none():
    cfg = C.config_from_dict(llama8b())
    b, _ = C.weight_bytes_estimate(cfg, "mxfp8-unknown")
    assert b is None


# ---------------------------------------------------------------------------
# capacity estimation
# ---------------------------------------------------------------------------


def test_estimate_qwen8b_rtx_fit():
    """bf16 8B on a 96GB RTX Pro 6000: 15.3 GiB weights, ~1M-token KV pool."""
    cfg = C.config_from_dict(llama8b())
    res = E.run_estimate(
        E.EstimateRequest(
            cfg=cfg, gpu=RTX, gpu_count=1, weight_dtype="bf16", kv_dtype="fp8", mem_fraction=0.9, context=32768
        )
    )
    assert res.fits is True
    assert res.weights_bytes == pytest.approx(2 * 8.03e9, rel=0.02)
    assert 0.9e6 < res.kv_tokens_total < 1.3e6
    assert res.concurrency == 34


def test_estimate_concurrency_scales_with_context():
    cfg = C.config_from_dict(llama8b())
    for ctx, expected in ((4096, 277), (8192, 138), (32768, 34)):
        res = E.run_estimate(
            E.EstimateRequest(cfg=cfg, gpu=RTX, gpu_count=1, weight_dtype="bf16", kv_dtype="fp8", context=ctx)
        )
        assert res.concurrency == expected


def test_estimate_does_not_fit():
    """DSV3 fp8 weights (~625 GiB) cannot fit on 4x H200 (564 usable)."""
    cfg = C.config_from_dict(deepseek_v3())
    res = E.run_estimate(
        E.EstimateRequest(
            cfg=cfg, gpu=H200, gpu_count=4, tp_size=4, weight_dtype="fp8", kv_dtype="fp8_e4m3", mem_fraction=0.8
        )
    )
    assert res.fits is False
    assert res.concurrency == 0
    assert any("exceed usable VRAM" in w for w in res.warnings)


def test_estimate_replicas_double_pool():
    """2 GPUs at TP1 = two replicas = 2x the KV pool of one GPU."""
    cfg = C.config_from_dict(llama8b())
    one = E.run_estimate(
        E.EstimateRequest(cfg=cfg, gpu=RTX, gpu_count=1, tp_size=1, weight_dtype="bf16", kv_dtype="fp8")
    )
    two = E.run_estimate(
        E.EstimateRequest(cfg=cfg, gpu=RTX, gpu_count=2, tp_size=1, weight_dtype="bf16", kv_dtype="fp8")
    )
    assert two.kv_tokens_total == pytest.approx(one.kv_tokens_total * 2)


def test_estimate_tp_pools_memory():
    cfg = C.config_from_dict(deepseek_v3())
    res = E.run_estimate(
        E.EstimateRequest(
            cfg=cfg, gpu=H200, gpu_count=8, tp_size=8, weight_dtype="fp8", kv_dtype="fp8_e4m3", mem_fraction=0.8
        )
    )
    assert res.fits is True
    # weights 625GiB/8 = 78 GiB/GPU; usable 110.8 -> ~30 GiB KV per GPU
    assert 0.9e6 < res.kv_tokens_per_gpu < 1.2e6
    assert res.concurrency > 0


def test_grid_cells_ordered():
    cfg = C.config_from_dict(llama8b())
    cells = E.grid(
        cfg, RTX, gpu_counts=[1, 2], tp_sizes=[1, 2], contexts=[8192, 32768], weight_dtype="bf16", kv_dtype="fp8"
    )
    # (1, tp1), (2, tp1), (2, tp2) — tp>gpus and duplicate (g,tp) skipped
    pairs = list(dict.fromkeys((c.gpus, c.tp) for c in cells))
    assert pairs == [(1, 1), (2, 1), (2, 2)]
    for c in cells:
        assert c.kv_tokens > 0
        # concurrency at 32k must be ~1/4 of the 8k figure
        by_ctx = {x.context: x.concurrency for x in cells if (x.gpus, x.tp) == (c.gpus, c.tp)}
        assert by_ctx[8192] > by_ctx[32768]


def test_speculative_layers_add_kv():
    cfg = C.config_from_dict(deepseek_v3())
    base = E.run_estimate(
        E.EstimateRequest(cfg=cfg, gpu=H200, gpu_count=8, tp_size=8, weight_dtype="fp8", kv_dtype="fp8_e4m3")
    )
    spec = E.run_estimate(
        E.EstimateRequest(
            cfg=cfg,
            gpu=H200,
            gpu_count=8,
            tp_size=8,
            weight_dtype="fp8",
            kv_dtype="fp8_e4m3",
            speculative=E.SpeculativeSpec(algorithm="MTP", layers=1),
        )
    )
    assert spec.kv_tokens_total < base.kv_tokens_total
    assert any("MTP draft KV" in w for w in spec.warnings)


# ---------------------------------------------------------------------------
# artifact + report ingestion
# ---------------------------------------------------------------------------


def _args(tmp_path: Path, **over):
    import argparse

    ns = argparse.Namespace(
        model="llama-test-8b",
        gpu="RTX Pro 6000",
        gpu_vram=None,
        gpu_count=1,
        tp=None,
        pp=None,
        weight_dtype=None,
        kv_dtype="fp8",
        mem_fraction=0.9,
        overhead=E.DEFAULT_OVERHEAD_GIB,
        context=[32768],
        grid=True,
        grid_gpus=None,
        speculative=0,
        ep=1,
        catalog=None,
        catalog_id=None,
        output=tmp_path / "mem.md",
        json=False,
        # hierarchical cache (HiCache) flags
        hicache="",
        hicache_ratio=0.0,
        hicache_size=0.0,
        hicache_l3=0.0,
        hicache_tp_replicated=False,
        hicache_tp_sharded=False,
    )
    for k, v in over.items():
        setattr(ns, k, v)
    return ns


def test_build_payload_and_markdown(tmp_path, monkeypatch):
    from model_benchmarker.memory_model import cli as CLI

    monkeypatch.setattr(CLI, "load_config_json", lambda ref: (C.config_from_dict(llama8b()), "test-fixture"))
    payload, code = build_payload(_args(tmp_path))
    assert code == 0
    assert payload["config"][0] == ("tool", "memory-estimate")
    assert payload["fits"] is True
    md = render_markdown(payload)
    assert md.startswith("# Memory estimate:")
    assert "| tool | memory-estimate |" in md
    assert "## Capacity grid" in md
    # the wide grid row must NOT parse as a chat row (first cell non-numeric)
    from model_benchmarker.results_to_html import _parse_md_chat_row, parse_memory_estimate, parse_rag_table

    assert _parse_md_chat_row("| NVIDIA RTX PRO 6000 Blackwell x1 TP1 | 1.01M | 30 |") is None
    assert parse_rag_table(md) is None  # must not be mistaken for a RAG artifact
    mem = parse_memory_estimate(md)
    assert mem is not None
    assert mem["config"]["tool"] == "memory-estimate"
    assert mem["scenarios"], "scenario rows must be ingested"
    assert mem["grid"], "grid rows must be ingested"


def test_results_tree_end_to_end(tmp_path, monkeypatch):
    """Artifact written into a results dir shows up in report data."""
    from model_benchmarker.memory_model import cli as CLI
    from model_benchmarker.results_to_html import collect_report_data

    monkeypatch.setattr(CLI, "load_config_json", lambda ref: (C.config_from_dict(llama8b()), "test-fixture"))
    payload, code = build_payload(_args(tmp_path))
    assert code == 0
    model_dir = tmp_path / "llama_8b"
    model_dir.mkdir()
    (model_dir / "memory.md").write_text(render_markdown(payload), encoding="utf-8")
    data = collect_report_data(tmp_path, catalog_path=None)
    assert data["memory_count"] == 1
    setups = [s for m in data["memory_models"] for s in m["setups"]]
    assert setups[0]["memory"]["config"]["tool"] == "memory-estimate"
    assert "KV pool" in setups[0]["memory"]["scenarios"][0]["verdict"]


def test_catalog_args_fill(tmp_path, monkeypatch):
    """--catalog-id fills tp / kv dtype / gpu count from a seed entry."""
    from model_benchmarker.memory_model import cli as CLI

    catalog = tmp_path / "seed_catalog.json"
    catalog.write_text(
        json.dumps(
            [
                {
                    "catalog_id": "seed-test",
                    "tier": "h200",
                    "name": "my-model-h200",
                    "arguments": [
                        "sglang",
                        "serve",
                        "org/model-x",
                        "--tp-size",
                        "4",
                        "--kv-cache-dtype",
                        "fp8_e4m3",
                        "--mem-fraction-static",
                        "0.8",
                    ],
                    "resource_request_gpu": "4",
                }
            ]
        )
    )
    monkeypatch.setattr(CLI, "load_config_json", lambda ref: (C.config_from_dict(deepseek_v3()), "test-fixture"))
    ns = _args(
        tmp_path,
        model="my-model-h200",
        catalog=catalog,
        catalog_id="seed-test",
        gpu=None,
        gpu_count=None,
        tp=None,
        mem_fraction=None,
        kv_dtype=None,
    )
    payload, code = build_payload(ns)
    assert code == 0
    # gpu/tp/mem-fraction came from the catalog entry
    cfg_rows = dict(payload["config"])
    assert cfg_rows["gpu"] == "NVIDIA H200 SXM x4 (TP4)"
    assert cfg_rows["mem fraction"] == "0.8"
    assert cfg_rows["kv dtype"] == "FP8 (e4m3)"


def test_load_config_json_local_dir(tmp_path):
    d = tmp_path / "model"
    d.mkdir()
    (d / "config.json").write_text(json.dumps(llama8b()))
    cfg, src = C.load_config_json(d)
    assert cfg.num_key_value_heads == 8
    assert "config.json" in src


def test_load_config_json_missing():
    with pytest.raises(FileNotFoundError):
        C.load_config_json("/nonexistent/path/that/does/not/exist")


# ---------------------------------------------------------------------------
# hierarchical cache (HiCache) tiers: L2 host RAM / L3 backing tier
# ---------------------------------------------------------------------------


def _gqa_cfg() -> dict:
    """GQA shape whose kv_heads == heads (sharded L2 layout, any TP)."""
    return llama8b()


def test_hicache_off_by_default():
    res = E.run_estimate(E.EstimateRequest(cfg=C.config_from_dict(_gqa_cfg()), gpu=H200, weight_dtype="bf16"))
    assert res.tiers.l2_tokens is None
    assert res.tiers.l3_tokens is None
    assert not any("HiCache" in w for w in res.warnings)


def test_hicache_l2_ratio_matches_device_pool():
    """ratio X -> L2 pool = X x device pool per replica, same KV dtype."""
    req_kw = {"cfg": C.config_from_dict(_gqa_cfg()), "gpu": H200, "gpu_count": 4, "tp_size": 4, "weight_dtype": "bf16"}
    base = E.run_estimate(E.EstimateRequest(**req_kw))
    tiered = E.run_estimate(E.EstimateRequest(**req_kw, hicache=E.HicacheSpec(l2_mode="ratio", l2_ratio=3.0)))
    assert tiered.tiers.l2_tokens == pytest.approx(base.kv_tokens_total * 3.0)
    assert tiered.tiers.total_tokens == pytest.approx(base.kv_tokens_total * 4.0)


def test_hicache_l2_size_mode():
    cfg = C.config_from_dict(_gqa_cfg())
    res = E.run_estimate(
        E.EstimateRequest(
            cfg=cfg,
            gpu=H200,
            gpu_count=4,
            tp_size=4,
            weight_dtype="bf16",
            hicache=E.HicacheSpec(l2_mode="size", l2_gib=128),
        )
    )
    # replica-collective frame: the TP group's host pool stores ONE copy of
    # each token (each rank holds its 1/tp share), so a 128 GiB L2 holds
    # 128 GiB / full-stream-bytes-per-token tokens.
    assert res.tiers.l2_tokens == pytest.approx(128 * 1024**3 / res.kv_bytes_per_token)
    assert res.tiers.per_token_bytes_l2 == pytest.approx(res.kv_bytes_per_token)


def test_hicache_l3_capped_at_l2_pool():
    cfg = C.config_from_dict(_gqa_cfg())
    res = E.run_estimate(
        E.EstimateRequest(
            cfg=cfg,
            gpu=H200,
            gpu_count=4,
            tp_size=4,
            weight_dtype="bf16",
            hicache=E.HicacheSpec(l2_mode="size", l2_gib=100, l3_gib=900),
        )
    )
    assert res.tiers.l3_bytes_per_replica == pytest.approx(100 * 1024**3)
    assert any("never filled" in n for n in res.tiers.notes)
    # and when L3 < L2 it is honored as-is
    res2 = E.run_estimate(
        E.EstimateRequest(
            cfg=cfg,
            gpu=H200,
            gpu_count=4,
            tp_size=4,
            weight_dtype="bf16",
            hicache=E.HicacheSpec(l2_mode="size", l2_gib=100, l3_gib=50),
        )
    )
    assert res2.tiers.l3_bytes_per_replica == pytest.approx(50 * 1024**3)
    assert not any("never filled" in n for n in res2.tiers.notes)


def test_hicache_mla_replicates_across_tp():
    """MLA: one latent stream per token -> every TP rank's host pool holds the
    FULL per-token stream (auto), matching the device-pool rule."""
    cfg = C.config_from_dict(deepseek_v3())
    cfg_dict = {**deepseek_v3(), "torch_dtype": "bfloat16"}
    cfg = C.config_from_dict(cfg_dict)
    res = E.run_estimate(
        E.EstimateRequest(
            cfg=cfg,
            gpu=H200,
            gpu_count=8,
            tp_size=8,
            weight_dtype="fp8",
            kv_dtype="fp8_e4m3",
            hicache=E.HicacheSpec(l2_mode="ratio", l2_ratio=2.0),
        )
    )
    full_stream = res.kv_bytes_per_token  # MLA: device bpt is already full-stream
    assert res.tiers.per_token_bytes_l2 == pytest.approx(full_stream)
    # explicit override restores the sharded figure: the SAME host pool now
    # stores 1/8 shares, so it fits 8x more tokens
    res_sharded = E.run_estimate(
        E.EstimateRequest(
            cfg=cfg,
            gpu=H200,
            gpu_count=8,
            tp_size=8,
            weight_dtype="fp8",
            kv_dtype="fp8_e4m3",
            hicache=E.HicacheSpec(l2_mode="ratio", l2_ratio=2.0, l2_tp_replicated=False),
        )
    )
    assert res_sharded.tiers.l2_tokens == pytest.approx(res.tiers.l2_tokens * 8)


def test_hicache_mqa_wide_tp_replicates():
    """MQA on wide TP (kv_heads < tp): the layout replicates, same as device."""
    cfg_dict = {**_gqa_cfg(), "num_key_value_heads": 1, "head_dim": 512}
    cfg = C.config_from_dict(cfg_dict)
    res = E.run_estimate(
        E.EstimateRequest(
            cfg=cfg,
            gpu=H200,
            gpu_count=4,
            tp_size=4,
            weight_dtype="bf16",
            hicache=E.HicacheSpec(l2_mode="ratio", l2_ratio=2.0),
        )
    )
    assert res.tiers.per_token_bytes_l2 == pytest.approx(res.kv_bytes_per_token)


def test_hicache_per_replica_multiplication():
    """8 GPUs TP4 = 2 replicas -> the host pool is allocated twice."""
    req_kw = {"cfg": C.config_from_dict(_gqa_cfg()), "gpu": H200, "gpu_count": 8, "tp_size": 4, "weight_dtype": "bf16"}
    single = E.run_estimate(E.EstimateRequest(**req_kw, hicache=E.HicacheSpec(l2_mode="size", l2_gib=64)))
    # two replicas: total L2 tokens = 2x a single replica's pool
    bpt = single.tiers.per_token_bytes_l2
    assert single.tiers.l2_tokens == pytest.approx(2 * 64 * 1024**3 / bpt)
    assert any("PER REPLICA" in n for n in single.tiers.notes)


def test_hicache_unknown_weights_no_tiers():
    """No dtype -> no weights -> tier token counts stay None (never invented)."""
    raw = {**_gqa_cfg()}
    raw.pop("torch_dtype", None)
    cfg2 = C.config_from_dict(raw)
    res = E.run_estimate(E.EstimateRequest(cfg=cfg2, gpu=H200, hicache=E.HicacheSpec(l2_mode="ratio", l2_ratio=2.0)))
    assert res.tiers.l2_tokens is None
    assert res.tiers.total_tokens is None


def test_hicache_warning_reports_l2_tokens():
    cfg = C.config_from_dict(_gqa_cfg())
    res = E.run_estimate(
        E.EstimateRequest(
            cfg=cfg,
            gpu=H200,
            gpu_count=4,
            tp_size=4,
            weight_dtype="bf16",
            hicache=E.HicacheSpec(l2_mode="ratio", l2_ratio=3.0, l3_gib=100),
        )
    )
    hits = [w for w in res.warnings if w.startswith("HiCache L2:")]
    assert hits and "cached tokens" in hits[0] and "L3" in hits[0]


def test_grid_carries_hicache():
    cfg = C.config_from_dict(_gqa_cfg())
    cells = E.grid(
        cfg,
        H200,
        gpu_counts=[4],
        tp_sizes=[4],
        contexts=[32768],
        weight_dtype="bf16",
        hicache=E.HicacheSpec(l2_mode="ratio", l2_ratio=2.0),
    )
    assert cells  # smoke: grid accepts + returns with the spec in flight


def test_summary_line_includes_tiers():
    cfg = C.config_from_dict(_gqa_cfg())
    res = E.run_estimate(
        E.EstimateRequest(
            cfg=cfg,
            gpu=H200,
            gpu_count=4,
            tp_size=4,
            weight_dtype="bf16",
            context=32768,
            hicache=E.HicacheSpec(l2_mode="ratio", l2_ratio=2.0),
        )
    )
    line = E.summary_line(res)
    assert "HiCache" in line and "L2" in line


# ---------------------------------------------------------------------------
# pipeline parallel (PP) — the GLM-5.2 TP4/PP2 shape on 8x H200
# ---------------------------------------------------------------------------


def glm52_fp8() -> dict:
    """GLM-5.2-FP8 shape (glm_moe_dsa): MLA + DSA, 256 experts, MTP=1."""
    return {
        "model_type": "glm_moe_dsa",
        "hidden_size": 6144,
        "num_hidden_layers": 78,
        "num_attention_heads": 96,
        "num_key_value_heads": 96,
        "kv_lora_rank": 512,
        "qk_rope_head_dim": 64,
        "index_head_dim": 128,
        "index_n_heads": 32,
        "index_topk": 2048,
        "vocab_size": 154624,
        "n_routed_experts": 256,
        "num_experts_per_tok": 8,
        "moe_intermediate_size": 2048,
        "first_k_dense_replace": 3,
        "num_nextn_predict_layers": 1,
        "quantization_config": {"quant_method": "fp8"},
    }


def test_pp_shards_weights_kv_pool_unchanged():
    """PP2 halves per-GPU weights; the KV token pool is NOT re-divided by pp."""
    cfg = C.config_from_dict(_gqa_cfg())
    base = E.run_estimate(E.EstimateRequest(cfg=cfg, gpu=H200, gpu_count=8, tp_size=4, weight_dtype="bf16"))
    with_pp = E.run_estimate(
        E.EstimateRequest(cfg=cfg, gpu=H200, gpu_count=8, tp_size=4, pp_size=2, weight_dtype="bf16")
    )
    assert with_pp.weights_per_gpu == pytest.approx(base.weights_per_gpu / 2)
    assert with_pp.fits and base.fits
    # 8 GPUs hold one TP4xPP2 group: same total token pool as the TP4 base
    # (weights freed by PP become KV pool, on every GPU of the group:
    # delta = 8 x (w/2/8) / bpt = w_total/8... -> total weights / bpt)
    assert with_pp.kv_tokens_total == pytest.approx(
        base.kv_tokens_total + base.weights_per_gpu * 4 / base.kv_bytes_per_token
    )
    assert any("PP2" in w for w in with_pp.warnings)


def test_pp_clamped_to_gpu_count():
    cfg = C.config_from_dict(_gqa_cfg())
    res = E.run_estimate(E.EstimateRequest(cfg=cfg, gpu=H200, gpu_count=2, tp_size=2, pp_size=8, weight_dtype="bf16"))
    assert res.request.pp_size == 2


def test_glm52_tp4pp2_fits_on_8x_h200():
    """The exact seed-catalog shape: TP4 + PP2 on 8 GPUs must FIT.

    Regression: before PP support this computed weights/4 = 172.9 GiB/GPU
    and returned DOES NOT FIT for a deployment that ran in production.
    """
    cfg = C.config_from_dict(glm52_fp8())
    res = E.run_estimate(
        E.EstimateRequest(
            cfg=cfg,
            gpu=H200,
            gpu_count=8,
            tp_size=4,
            pp_size=2,
            weight_dtype="fp8",
            kv_dtype="fp8_e4m3",
            mem_fraction=0.8,
            context=786432,
        )
    )
    assert res.fits, f"GLM-5.2 TP4/PP2 must fit, got {res.weights_per_gpu / 1024**3:.1f} GiB/GPU"
    assert res.weights_per_gpu == pytest.approx(691.6 / 4 / 2 * 1024**3, rel=0.02)
    assert res.kv_tokens_total and res.kv_tokens_total > 786432  # at least one 768k request
    assert E.summary_line(res).startswith("4xNVIDIA H200 SXM PP2:")


def test_glm52_pure_tp4_does_not_fit():
    """Sanity on the same model WITHOUT pp: TP4 on 4 GPUs really is too big."""
    cfg = C.config_from_dict(glm52_fp8())
    res = E.run_estimate(
        E.EstimateRequest(cfg=cfg, gpu=H200, gpu_count=4, tp_size=4, weight_dtype="fp8", kv_dtype="fp8_e4m3")
    )
    assert res.fits is False


def test_dsa_index_stream_priced():
    """glm_moe_dsa adds the sparse-index key stream (index_head_dim) per layer."""
    cfg = C.config_from_dict(glm52_fp8())
    _, details = C.kv_bytes_per_token(cfg, "fp8_e4m3")
    assert details.get("indexer_bytes_per_layer") == 128 * 1.0
    assert "DSA index" in details["formula"]
    # 512 + 64 latent + 128 index = 704 B/token/layer at fp8
    assert details["per_layer_bytes"] == pytest.approx(704.0)
    # unmodeled-fields warning must NOT fire for fields this family now prices
    consumed = details.get("_consumed_fields", set())
    assert "index_head_dim" in consumed and "index_n_heads" in consumed


# ---------------------------------------------------------------------------
# qwen4_exp — lightning indexer, num_experts/mtp_num_hidden_layers aliases
# ---------------------------------------------------------------------------


def qwen38_flash_next() -> dict:
    """Qwen3.8-Flash-Next text stack (qwen4_exp): hybrid GQA+linear, 512
    experts keyed num_experts, lightning indexer, mtp_num_hidden_layers=1."""
    return {
        "model_type": "qwen4_exp_text",
        "hidden_size": 2560,
        "num_hidden_layers": 48,
        "num_attention_heads": 24,
        "num_key_value_heads": 2,
        "head_dim": 256,
        "full_attention_interval": 4,
        "layer_types": ["linear_attention", "linear_attention", "linear_attention", "full_attention"],
        "linear_key_head_dim": 128,
        "linear_value_head_dim": 128,
        "linear_num_key_heads": 16,
        "linear_num_value_heads": 48,
        "indexer_head_dim": 128,
        "indexer_n_heads": 4,
        "indexer_kv_heads": 1,
        "indexer_budget": 2048,
        "num_experts": 512,
        "num_experts_per_tok": 10,
        "moe_intermediate_size": 640,
        "shared_expert_intermediate_size": 640,
        "mtp_num_hidden_layers": 1,
        "mamba_ssm_dtype": "float32",
        "vocab_size": 248320,
    }


def test_qwen4_exp_aliases_and_indexer():
    cfg = C.config_from_dict(qwen38_flash_next())
    assert cfg.experts == 512, "num_experts must resolve as the MoE expert count"
    assert cfg.num_nextn_predict_layers == 1, "mtp_num_hidden_layers must alias the MTP module"
    assert cfg.indexer_head_dim == 128
    bpt, details = C.kv_bytes_per_token(cfg, "bf16")
    # per full layer: 2 x 2 x 256 x 2 B (KV) + 128 x 2 B (indexer K) = 2304 B
    # per linear layer: 0 -> total = 12 full layers x 2304 B
    assert details["per_layer_bytes"] == pytest.approx(2304.0)
    assert bpt == pytest.approx(12 * 2304.0)
    assert "lightning indexer" in details["arch"]
    # the linear-attention state must be reported per sequence, not per token
    assert details.get("linear_state_bytes_per_seq") == pytest.approx(48 * 128 * 128 * 4.0)
    assert "mamba/KDA pool" in str(details.get("note", ""))


def test_qwen4_exp_experts_counted_in_weights():
    """512 routed experts (num_experts key) must show up in the param count."""
    cfg = C.config_from_dict(qwen38_flash_next())
    params, _ = C.count_parameters(cfg)
    # the config carries no first_k_dense_replace/mlp_layer_types -> all 48
    # layers are MoE by the config's own statement: 48 x 512 x 3 x 2560 x 640
    moe_idx = C._moe_layer_indices(cfg)
    assert len(moe_idx) == 48
    expert_params = 48 * 512 * 3 * 2560 * 640
    assert params > expert_params, "expert weights must dominate the count"
    # a 510-fewer-expert variant must be smaller by exactly those experts.
    # (num_experts=1 would flip is_moe False and reclassify EVERY layer
    # dense — not an expert-only delta. num_experts=2 keeps the layer
    # classification identical; dense-layer width doesn't enter either way:
    # this fixture carries no intermediate_size.)
    d = qwen38_flash_next()
    d["num_experts"] = 2
    params_fewer, _ = C.count_parameters(C.config_from_dict(d))
    # 510 experts dropped x 3 matrices x h x inter, x 48 MoE layers
    assert params - params_fewer == pytest.approx(510 * 3 * 2560 * 640 * 48, rel=0.01)


def test_https_fetch_config_mirrors_hf(tmp_path, monkeypatch):
    """The stdlib fallback: hits the resolve URL, parses JSON. Offline envs
    get a clean error; the message carries the fetch error verbatim."""
    from model_benchmarker.memory_model.configs import _https_fetch_config

    data, url, err = _https_fetch_config("Qwen/Qwen3-8B")
    if err:  # offline CI: fail soft, but the error must be self-describing
        assert data is None and "huggingface.co" not in err or "URLError" in err or "HTTP" in err
        return
    assert data and data.get("model_type") == "qwen3"
    assert url.endswith("/resolve/main/config.json")


def test_https_fetch_bad_repo_error_message(monkeypatch):
    """A nonexistent repo surfaces the HTTP error in load_config_json's message."""
    from model_benchmarker.memory_model import configs as CFG

    monkeypatch.setattr(CFG, "_mirror_candidates", lambda ref: [])
    with pytest.raises(FileNotFoundError) as ei:
        CFG.load_config_json("org-does-not-exist-xyz/no-such-model-xyz")
    msg = str(ei.value)
    assert "config.json not found" in msg
    assert "https fetch:" in msg


def test_hf_endpoint_guard():
    from model_benchmarker.memory_model.configs import _https_fetch_config

    monkey = None
    import os

    old = os.environ.get("HF_ENDPOINT")
    try:
        os.environ["HF_ENDPOINT"] = "http://insecure.example.com"
        data, _url, err = _https_fetch_config("a/b")
        assert data is None and "https" in err
    finally:
        if old is None:
            os.environ.pop("HF_ENDPOINT", None)
        else:
            os.environ["HF_ENDPOINT"] = old
    _ = monkey


# ---------------------------------------------------------------------------
# modelopt (NVIDIA ModelOpt) quantization_config -> weight dtype
# ---------------------------------------------------------------------------


def test_modelopt4_weights_resolve():
    """nvidia/Qwen3.8-Flash-Next-NVFP4 shape: quant_method=modelopt with
    config_groups weights.num_bits=4 -> NVFP4 pricing (0.5 B/param)."""
    cfg = C.config_from_dict(
        {
            "model_type": "qwen4_exp_text",
            "hidden_size": 2560,
            "num_hidden_layers": 48,
            "num_attention_heads": 24,
            "num_key_value_heads": 2,
            "head_dim": 256,
            "vocab_size": 248320,
            "quantization_config": {
                "quant_method": "modelopt",
                "config_groups": {"group_0": {"weights": {"num_bits": 4, "type": "float", "group_size": 16}}},
            },
        }
    )
    assert cfg.quant_method == "modelopt4" and cfg.quant_bits == 4
    wd = C.default_weight_dtype(cfg)
    assert wd == "modelopt4"
    _wbytes, details = C.weight_bytes_estimate(cfg, wd)
    assert details["bytes_per_param"] == 0.5
    # and the pipeline end-to-end: modelopt4 is accepted by the estimate path
    res = E.run_estimate(E.EstimateRequest(cfg=cfg, gpu=H200, weight_dtype=None))
    assert res.weights_bytes is not None
    assert res.details["weights"]["bytes_per_param"] == 0.5


def test_modelopt8_weights_resolve():
    cfg = C.config_from_dict(
        {
            "model_type": "qwen4_exp_text",
            "hidden_size": 2560,
            "num_hidden_layers": 2,
            "num_attention_heads": 4,
            "vocab_size": 1024,
            "quantization_config": {
                "quant_method": "modelopt",
                "config_groups": {"g": {"weights": {"num_bits": 8}}},
            },
        }
    )
    assert cfg.quant_method == "modelopt8"
    assert C.weight_bytes_estimate(cfg, "modelopt8")[1]["bytes_per_param"] == 1.0


def test_dtype_label_modelopt():
    from model_benchmarker.memory_model.dtypes import dtype_label

    assert dtype_label("modelopt4") == "NVFP4 (modelopt)"
    assert dtype_label("modelopt8") == "FP8 (modelopt)"


# ---------------------------------------------------------------------------
# engram (DeepSeek-V4.1 n-gram memory tables) + state-dtype knobs
# ---------------------------------------------------------------------------


def _dsv41_cfg():
    """The shipped V4.1-Flash mirror: the engram-capable family fixture."""
    path = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "model_benchmarker"
        / "webapp"
        / "hf_configs"
        / ("deepseek-ai__DeepSeek-V4.1-Flash.json")
    )
    return C.config_from_dict(json.loads(path.read_text()))


def test_engram_table_bytes_from_config():
    cfg = _dsv41_cfg()
    eb, ed = C.engram_table_bytes(cfg, None)
    assert eb is not None and ed["present"] and ed["parse_ok"]
    rows = sum(ed["per_table"][i]["rows"] for i in range(len(ed["per_table"])))
    assert rows == 384006168 + 384016682
    # fp8 x head_dim(256): the SGLang page's "two fp8 tables"; the live seed
    # deployment measured 183 GiB tables + ~24 GiB runtime = 207 GiB working set
    assert ed["bytes_per_row"] == 256
    assert abs(eb / 1024**3 - 183.1) < 1.0
    # dtype override re-prices (bf16 tables would double it)
    eb16, _ = C.engram_table_bytes(cfg, "bf16")
    assert abs(eb16 / eb - 2.0) < 1e-6
    # no engram block -> (None, present False)
    plain = C.config_from_dict(
        {
            "model_type": "llama",
            "hidden_size": 512,
            "num_hidden_layers": 4,
            "num_attention_heads": 8,
            "vocab_size": 1024,
        }
    )
    assert C.engram_table_bytes(plain, None) == (None, {"present": False})
    # broken block (count mismatch) reports itself rather than guessing
    broken = C.config_from_dict(
        {
            "model_type": "deepseek_v41",
            "hidden_size": 512,
            "num_hidden_layers": 4,
            "num_attention_heads": 8,
            "vocab_size": 1024,
            "engram_layer_ids": [1, 14],
            "engram_num_embeddings": [100],
        }
    )
    eb2, ed2 = C.engram_table_bytes(broken, None)
    assert eb2 is None and ed2["present"] and not ed2["parse_ok"]


def test_engram_host_offload_frees_hbm():
    cfg = _dsv41_cfg()
    gpu = G.resolve_gpu("H200")
    spec = E.SpeculativeSpec(algorithm="DSPARK", layers=3)
    host = E.run_estimate(
        E.EstimateRequest(
            cfg=cfg,
            gpu=gpu,
            gpu_count=4,
            tp_size=4,
            ep_size=4,
            mem_fraction=0.7,
            state_dtype="bf16",
            speculative=spec,
            engram=E.EngramSpec(mode="host"),
        )
    )
    vram = E.run_estimate(
        E.EstimateRequest(
            cfg=cfg,
            gpu=gpu,
            gpu_count=4,
            tp_size=4,
            ep_size=4,
            mem_fraction=0.7,
            state_dtype="bf16",
            speculative=spec,
            engram=E.EngramSpec(mode="vram"),
        )
    )
    # VRAM default: 183 GiB/4 = ~46 GiB per GPU of tables on top of ~67 GiB -> does NOT fit
    assert vram.fits is False
    assert vram.details["engram"]["mode"] == "vram"
    # host offload: out of HBM entirely -> fits, ~46 GiB/GPU freed to the KV pool
    assert host.fits is True
    freed_gib = (vram.weights_per_gpu - host.weights_per_gpu) / 1024**3
    assert abs(freed_gib - 183.1 / 4) < 0.5
    assert host.kv_tokens_total > 0 and vram.kv_tokens_total < 0
    # host-budget check fires when the tables exceed the stated budget
    tight = E.run_estimate(
        E.EstimateRequest(
            cfg=cfg,
            gpu=gpu,
            gpu_count=4,
            tp_size=4,
            ep_size=4,
            mem_fraction=0.7,
            state_dtype="bf16",
            speculative=spec,
            engram=E.EngramSpec(mode="host", host_gib=64),
        )
    )
    # host-budget check fires when the budget cannot hold tables + runtime
    # (the message reports the decomposed requirement, ~207 GiB for V4.1)
    assert any("host offload needs" in w for w in tight.warnings)


def test_engram_fields_never_silently_unmodeled():
    cfg = _dsv41_cfg()
    gpu = G.resolve_gpu("H200")
    # consumed when the engram path prices them (both layouts)
    for mode in ("host", "vram"):
        res = E.run_estimate(
            E.EstimateRequest(cfg=cfg, gpu=gpu, gpu_count=4, tp_size=4, engram=E.EngramSpec(mode=mode))
        )
        unmodeled = [u for u in res.warnings if "structural fields" in u and "engram" in u]
        assert not unmodeled, f"engram fields leaked into unmodeled warnings (mode={mode})"
    # and the parse-broken config DOES warn loudly
    broken = C.config_from_dict(
        {
            "model_type": "deepseek_v41",
            "hidden_size": 512,
            "num_hidden_layers": 4,
            "num_attention_heads": 8,
            "vocab_size": 1024,
            "head_dim": 64,
            "compress_ratios": [0, 0, 2, 1],
            "engram_layer_ids": [1, 14],
            "engram_num_embeddings": [100],
        }
    )
    res = E.run_estimate(E.EstimateRequest(cfg=broken, gpu=gpu, gpu_count=1, tp_size=1))
    assert any("engram" in w for w in res.warnings)


def test_compress_state_dtype_changes_csa2_kv():
    cfg = _dsv41_cfg()
    fp4_default = C.kv_bytes_per_token(cfg, None)[0]
    bf16 = C.kv_bytes_per_token(cfg, None, state_dtype="bf16")[0]
    # the deployment's SGLANG_DSV4_COMPRESS_STATE_DTYPE=bf16 prices entries
    # flat at 2 B/elem: (512+128) = 1280 B per stored layer, with CSA2 reuse
    # (3 encoder groups at m=2, 1 decoder at m=1): 3*1280/2 + 1280 = 3200
    assert abs(bf16 - 3200.0) < 1e-6
    assert bf16 > fp4_default * 3  # the env var quadruples the stored state
    assert abs(fp4_default - 900.0) < 2.0  # the paper's 890 B/token worked check


def test_ssm_dtype_override_float32_vs_bf16():
    cfg = C.config_from_dict(
        {
            "model_type": "qwen3_next",
            "hidden_size": 2048,
            "num_hidden_layers": 4,
            "num_attention_heads": 16,
            "num_key_value_heads": 2,
            "head_dim": 128,
            "vocab_size": 4096,
            "linear_key_head_dim": 128,
            "linear_value_head_dim": 128,
            "linear_num_value_heads": 32,
            "full_attention_interval": 4,  # 3 linear + 1 full per group of 4
            "mamba_ssm_dtype": "bfloat16",
            "torch_dtype": "bfloat16",
        }
    )
    bf16_state = C.kv_bytes_per_token(cfg, None)[1]["linear_state_bytes_per_seq"]
    assert bf16_state == 32 * 128 * 128 * 2.0
    fp32_state = C.kv_bytes_per_token(cfg, None, ssm_dtype="fp32")[1]["linear_state_bytes_per_seq"]
    assert abs(fp32_state / bf16_state - 2.0) < 1e-6  # mamba fp32 doubles the per-seq state


def test_build_payload_engram_catalog_auto(tmp_path, monkeypatch):
    import argparse

    cfg = _dsv41_cfg()
    monkeypatch.setattr(C, "load_config_json", lambda ref: (cfg, "test-fixture"))
    entry = {
        "catalog_id": "seed-h200-dsv41-flash-engram",
        "tier": "h200",
        "name": "deepseek-v4.1-flash-engram-offload",
        "arguments": [
            "sglang",
            "serve",
            "deepseek-ai/DeepSeek-V4.1-Flash",
            "--tp-size",
            "4",
            "--ep-size",
            "4",
            "--mem-fraction-static",
            "0.7",
            "--speculative-algorithm",
            "DSPARK",
        ],
        "environment": {"SGLANG_ENABLE_DSV41_ENGRAM_HOST_TABLE": "1", "SGLANG_DSV4_COMPRESS_STATE_DTYPE": "bf16"},
        "resource_limit_memory": "512Gi",
        "resource_request_gpu": 4,
    }
    cat = tmp_path / "seed_catalog.json"
    cat.write_text(json.dumps([entry]))
    args = argparse.Namespace(
        model="deepseek-ai/DeepSeek-V4.1-Flash",
        gpu=None,
        gpu_vram=None,
        gpu_count=None,
        tp=None,
        pp=None,
        weight_dtype=None,
        kv_dtype=None,
        mem_fraction=None,
        overhead=2.0,
        context=[32768],
        grid=False,
        grid_gpus=None,
        speculative=0,
        speculative_algorithm="DSPARK",
        draft_model="",
        draft_tokens=0,
        moe_runner=None,
        ep=4,
        hicache=None,
        hicache_ratio=0.0,
        hicache_size=0.0,
        hicache_l3=0.0,
        hicache_tp_sharded=False,
        hicache_tp_replicated=False,
        engram="auto",
        engram_dtype=None,
        engram_host_gib=0.0,
        compress_state_dtype=None,
        ssm_dtype=None,
        catalog=cat,
        catalog_id="seed-h200-dsv41-flash-engram",
        output=None,
        json=False,
    )
    payload, code = build_payload(args)
    assert code == 0
    rows = {k: v for k, v in payload["config"]}
    assert "HOST offload" in rows["engram"]
    assert rows["engram host budget"] == "512 GiB (pod memory limit)"
    assert "bf16" in rows["compress state"]
    struct = {k: v for k, v in payload["structure"]}
    assert "183.1 GiB" in struct["engram tables"] and "host-table offload" in struct["engram tables"]
    md = render_markdown(payload)
    assert "engram" in md and "HOST offload" in md


# ---------------------------------------------------------------------------
# checkpoint-exact weights (model.safetensors.index.json) + mxfp4
# ---------------------------------------------------------------------------


def test_checkpoint_total_bytes_local_dir(tmp_path):
    idx = {"metadata": {"total_size": 12345678}, "weight_map": {"a": "m-00001"}}
    (tmp_path / "model.safetensors.index.json").write_text(json.dumps(idx))
    total, src = C.checkpoint_total_bytes(str(tmp_path))
    assert total == 12345678 and "index.json" in src
    # missing index -> None (shape fallback applies)
    empty = tmp_path / "empty"
    empty.mkdir()
    assert C.checkpoint_total_bytes(str(empty))[0] is None
    # corrupt index -> None, not an exception
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "model.safetensors.index.json").write_text("{not json")
    assert C.checkpoint_total_bytes(str(bad))[0] is None


def test_checkpoint_bytes_authoritative_over_shape(monkeypatch):
    cfg = _dsv41_cfg()
    monkeypatch.setattr(C, "checkpoint_total_bytes", lambda ref: (475 * 1024**3, "fixture-index"))
    eb, _ = C.engram_table_bytes(cfg, None)
    wb, wd = C.weight_bytes_estimate(cfg, None, None, checkpoint_bytes=475 * 1024**3, engram_bytes=eb)
    # the index total minus the lifted engram tables IS the weight figure,
    # regardless of the bytes/param math
    assert abs(wb / 1024**3 - (475 - eb / 1024**3)) < 0.01
    assert wd["checkpoint_bytes"] == 475 * 1024**3
    assert wd["engram_bytes_lifted"] == eb
    # no engram block -> nothing lifted
    plain = C.config_from_dict(
        {
            "model_type": "llama",
            "hidden_size": 512,
            "num_hidden_layers": 4,
            "num_attention_heads": 8,
            "vocab_size": 1024,
        }
    )
    wb2, wd2 = C.weight_bytes_estimate(plain, None, None, checkpoint_bytes=100 * 1024**3)
    assert abs(wb2 / 1024**3 - 100.0) < 0.01 and "engram_bytes_lifted" not in wd2


def test_mxfp4_dtype_in_tables():
    from model_benchmarker.memory_model.dtypes import weight_bytes_per_param

    assert weight_bytes_per_param("mxfp4") == 0.5
    assert weight_bytes_per_param("MXFP4") == 0.5


def test_kimi_k3_uses_checkpoint_bytes_not_bf16_guess(monkeypatch):
    # the reported failure: Kimi K3 (kimi_k3, ~5462B params) estimated at
    # BF16 (2566 GiB) when the real checkpoint stores MXFP4-ish weights.
    # With the index present, the shape path never runs.
    cfg = C.config_from_dict(
        {
            "model_type": "kimi_k3",
            "hidden_size": 5120,
            "num_hidden_layers": 40,
            "num_attention_heads": 64,
            "vocab_size": 160000,
            "quantization_config": {"quant_method": "mxfp4"},  # what the page reports
        }
    )
    real = 1453.7 * 1024**3  # the live index total (lead-verified)
    monkeypatch.setattr(C, "checkpoint_total_bytes", lambda ref: (int(real), "fixture-index"))
    wb, wd = C.weight_bytes_estimate(cfg, None, None, checkpoint_bytes=int(real))
    assert abs(wb / 1024**3 - 1453.7) < 0.1
    assert "metadata.total_size" in wd["checkpoint_source"]
    # and WITHOUT the index, the mxfp4 quant_method now prices correctly
    # (0.5 B/param) instead of falling back to a bf16 guess
    assert C.default_weight_dtype(cfg) == "mxfp4"
    wb2, wd2 = C.weight_bytes_estimate(cfg, None, None)
    assert wb2 is not None and abs(wb2 / wd2["params"] - 0.5) < 0.01  # 0.5 B/param


# ---------------------------------------------------------------------------
# mamba/KDA pool knobs (--mamba-full-memory-ratio / --mamba-ssm-dtype)
# ---------------------------------------------------------------------------


def _qwen38_hybrid_cfg():
    """A Qwen3.8-27B-shaped hybrid (full+linear mix) for the mamba pool math."""
    return C.config_from_dict(
        {
            "model_type": "qwen3_next",
            "hidden_size": 5120,
            "num_hidden_layers": 48,
            "num_attention_heads": 40,
            "num_key_value_heads": 4,
            "head_dim": 128,
            "vocab_size": 151936,
            "linear_key_head_dim": 128,
            "linear_value_head_dim": 128,
            "linear_num_value_heads": 32,
            "full_attention_interval": 4,
            "mamba_ssm_dtype": "bfloat16",
            "torch_dtype": "bfloat16",
        }
    )


def test_mamba_pool_split_shrinks_kv_and_warns():
    cfg = _qwen38_hybrid_cfg()
    gpu = G.resolve_gpu("H200")
    base = E.run_estimate(E.EstimateRequest(cfg=cfg, gpu=gpu, gpu_count=4, tp_size=4, kv_dtype="fp8_e4m3"))
    split = E.run_estimate(
        E.EstimateRequest(
            cfg=cfg,
            gpu=gpu,
            gpu_count=4,
            tp_size=4,
            kv_dtype="fp8_e4m3",
            mamba=E.MambaSpec(full_memory_ratio=0.5, ssm_dtype="float32"),
        )
    )
    assert split.fits and base.fits
    # the ratio reserves half the device budget: KV pool shrinks accordingly
    reserved = split.details["mamba"]["pool_bytes_per_gpu"]
    assert abs(reserved / (base.usable_per_gpu) - 0.5) < 0.01
    assert split.kv_tokens_total < base.kv_tokens_total
    assert any("reserves" in w and "recurrent-state pool" in w for w in split.warnings)


def test_mamba_pool_adequacy_warning():
    cfg = _qwen38_hybrid_cfg()
    gpu = G.resolve_gpu("H200")
    # ratio 0.02 on 4xH200: ~2.4 GiB/GPU x 4 = ~9.6 GiB pool; 64 fp32 seqs
    # need per_seq(144 MiB) x 36 linear layers x 64 ~= 32x that -> warning
    tight = E.run_estimate(
        E.EstimateRequest(
            cfg=cfg,
            gpu=gpu,
            gpu_count=4,
            tp_size=4,
            kv_dtype="fp8_e4m3",
            mamba=E.MambaSpec(full_memory_ratio=0.02, ssm_dtype="float32"),
            mamba_concurrency=64,
        )
    )
    warn = [w for w in tight.warnings if "mamba pool too small" in w]
    assert warn, "expected the adequacy warning"
    assert "64 concurrent sequences" in warn[0] and "carries ~" in warn[0]
    # a generous ratio carries the target silently (no adequacy warning)
    ok = E.run_estimate(
        E.EstimateRequest(
            cfg=cfg,
            gpu=gpu,
            gpu_count=4,
            tp_size=4,
            kv_dtype="fp8_e4m3",
            mamba=E.MambaSpec(full_memory_ratio=0.5, ssm_dtype="float32"),
            mamba_concurrency=64,
        )
    )
    assert not [w for w in ok.warnings if "mamba pool too small" in w]


def test_mamba_ssm_dtype_via_spec_matches_standalone_knob():
    """ONE knob, two spellings: MambaSpec.ssm_dtype (the --mamba-ssm-dtype
    home) and EstimateRequest.ssm_dtype (the legacy standalone field) must
    price identically -- for the KV figure AND for the mamba-pool adequacy
    check, which used to read the raw field and silently fall back to the
    config default when only the standalone spelling was set."""
    cfg = _qwen38_hybrid_cfg()
    gpu = G.resolve_gpu("H200")
    via_spec = E.run_estimate(
        E.EstimateRequest(
            cfg=cfg, gpu=gpu, gpu_count=1, tp_size=1, mamba=E.MambaSpec(full_memory_ratio=0.1, ssm_dtype="float32")
        )
    )
    via_flag = E.run_estimate(
        E.EstimateRequest(
            cfg=cfg, gpu=gpu, gpu_count=1, tp_size=1, mamba=E.MambaSpec(full_memory_ratio=0.1), ssm_dtype="float32"
        )
    )
    a = via_spec.details["kv"]["linear_state_bytes_per_seq"]
    b = via_flag.details["kv"]["linear_state_bytes_per_seq"]
    assert a == b  # the two spellings price the same state
    # reconciliation: BOTH spellings are populated after __post_init__
    req = E.EstimateRequest(
        cfg=cfg, gpu=gpu, gpu_count=1, tp_size=1, mamba=E.MambaSpec(full_memory_ratio=0.1), ssm_dtype="float32"
    )
    assert req.ssm_dtype == "float32" and req.mamba.ssm_dtype == "float32"
    req2 = E.EstimateRequest(
        cfg=cfg, gpu=gpu, gpu_count=1, tp_size=1, mamba=E.MambaSpec(full_memory_ratio=0.1, ssm_dtype="fp32")
    )
    assert req2.ssm_dtype == "fp32" and req2.mamba.ssm_dtype == "fp32"
    # and the adequacy check agrees across spellings (the old bug: it read
    # ms.ssm_dtype only, so the standalone field was invisible there)
    spec_mamba = via_spec.details["mamba"].get("state_bytes_per_seq")
    flag_mamba = via_flag.details["mamba"].get("state_bytes_per_seq")
    assert spec_mamba is not None and spec_mamba == flag_mamba


def test_build_payload_parses_mamba_catalog_flags(tmp_path):
    import argparse

    cfg = _qwen38_hybrid_cfg()
    monkeypatch = __import__("pytest").MonkeyPatch()
    monkeypatch.setattr(C, "load_config_json", lambda ref: (cfg, "test-fixture"))
    entry = {
        "catalog_id": "seed-h200-qwen38-27b-dflash2",
        "tier": "h200",
        "name": "Qwen3.8-27B-sglang-FP8-hicachex3",
        "arguments": [
            "sglang",
            "serve",
            "Qwen/Qwen3.8-27B-FP8",
            "--tp-size",
            "4",
            "--kv-cache-dtype",
            "fp8_e4m3",
            "--mem-fraction-static",
            "0.88",
            "--mamba-full-memory-ratio",
            ".65",
            "--mamba-ssm-dtype",
            "bfloat16",
            "--speculative-algorithm",
            "DFLASH",
            "--speculative-num-draft-tokens",
            "8",
        ],
        "resource_request_gpu": 4,
    }
    cat = tmp_path / "seed_catalog.json"
    cat.write_text(json.dumps([entry]))
    args = argparse.Namespace(
        model="Qwen/Qwen3.8-27B-FP8",
        gpu=None,
        gpu_vram=None,
        gpu_count=None,
        tp=None,
        pp=None,
        weight_dtype=None,
        kv_dtype=None,
        mem_fraction=None,
        overhead=2.0,
        context=[65536],
        grid=False,
        grid_gpus=None,
        speculative=0,
        speculative_algorithm="",
        draft_model="",
        draft_tokens=0,
        moe_runner=None,
        ep=1,
        hicache=None,
        hicache_ratio=0.0,
        hicache_size=0.0,
        hicache_l3=0.0,
        hicache_tp_sharded=False,
        hicache_tp_replicated=False,
        engram="auto",
        engram_dtype=None,
        engram_host_gib=0.0,
        compress_state_dtype=None,
        ssm_dtype=None,
        mamba_full_memory_ratio=None,
        mamba_ssm_dtype=None,
        mamba_concurrency=0,
        catalog=cat,
        catalog_id="seed-h200-qwen38-27b-dflash2",
        output=None,
        json=False,
    )
    payload, code = build_payload(args)
    assert code == 0
    rows = {k: v for k, v in payload["config"]}
    assert rows["mamba full-memory ratio"] == "0.65"
    # ONE row for the single SSM-state knob (was duplicated as
    # "ssm state dtype" + "mamba ssm dtype" before the consolidation)
    assert rows["ssm state dtype"] == "bfloat16"
    monkeypatch.undo()


# ---------------------------------------------------------------------------
# KV replication factor (P0 fix): partial shards are a factor, not binary
# ---------------------------------------------------------------------------


def test_kv_replication_factor_spectrum():
    """r = tp//kv_heads for partial shards; tp for MQA/MLA single streams."""
    gqa4 = C.config_from_dict(
        {
            "model_type": "llama",
            "hidden_size": 5120,
            "num_hidden_layers": 40,
            "num_attention_heads": 40,
            "num_key_value_heads": 4,
            "head_dim": 128,
            "vocab_size": 1000,
            "torch_dtype": "bfloat16",
        }
    )
    assert E.kv_replication_factor(gqa4, 4) == 1  # heads shard cleanly
    assert E.kv_replication_factor(gqa4, 8) == 2  # each head on 2 ranks
    mqa = C.config_from_dict(
        {
            "model_type": "deepseek_v41",
            "num_key_value_heads": 1,
            "hidden_size": 512,
            "num_hidden_layers": 2,
            "num_attention_heads": 8,
        }
    )
    assert E.kv_replication_factor(mqa, 4) == 4  # MQA: full stream on every rank
    mla = C.config_from_dict(
        {
            "model_type": "deepseek_v3",
            "kv_lora_rank": 512,
            "qk_rope_head_dim": 64,
            "hidden_size": 7168,
            "num_hidden_layers": 2,
            "num_attention_heads": 128,
        }
    )
    assert E.kv_replication_factor(mla, 8) == 8  # MLA latent: replicated regardless


def test_kv_heads4_tp8_matches_tp4_pool():
    """The old binary rule UNDERCOUNTED TP8 4x for kv_heads=4: doubling the
    GPUs at the same per-GPU pool must never shrink the token pool."""
    cfg = C.config_from_dict(
        {
            "model_type": "llama",
            "hidden_size": 5120,
            "num_hidden_layers": 40,
            "num_attention_heads": 40,
            "num_key_value_heads": 4,
            "head_dim": 128,
            "vocab_size": 152064,
            "intermediate_size": 30720,
            "torch_dtype": "bfloat16",
        }
    )
    gpu = G.resolve_gpu("H200")
    toks = {}
    for tp in (4, 8):
        res = E.run_estimate(E.EstimateRequest(cfg=cfg, gpu=gpu, gpu_count=tp, tp_size=tp, context=32768))
        toks[tp] = res.kv_tokens_total
    assert toks[8] > toks[4]  # more GPUs -> strictly more tokens (weights shard further)
    # and the pool identity: tokens = pool*gpus/(bpt*r) with r=2 at TP8
    res8 = E.run_estimate(E.EstimateRequest(cfg=cfg, gpu=gpu, gpu_count=8, tp_size=8, context=32768))
    assert res8.kv_tokens_total == pytest.approx(res8.kv_pool_per_gpu * 8 / (res8.kv_bytes_per_token * 2))


def test_mqa_tp_replication_unchanged_by_factor_form():
    """V4.1-Flash (kv_heads=1) keeps the validated replicated semantics."""
    cfg = _dsv41_cfg()
    assert E.kv_replication_factor(cfg, 4) == 4


# ---------------------------------------------------------------------------
# V4 compress-state default (P1): the 6,810 worked check must reproduce
# from default arguments (no state_dtype passed)
# ---------------------------------------------------------------------------


def test_v4_default_compress_state_is_fp8():
    d = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "src/model_benchmarker/webapp/hf_configs"
            / "deepseek-ai__DeepSeek-V4-Flash-0731.json"
        ).read_text()
    )
    cfg = C.config_from_dict(d)
    default_bpt, det = C.kv_bytes_per_token(cfg, None, None, None)
    assert abs(default_bpt - 6810.0) < 2.0  # the docstring's worked check
    assert det["arch"].startswith("MQA")
    # explicit bf16 override still re-prices (the seed deployment's env)
    bf16_bpt, _ = C.kv_bytes_per_token(cfg, None, state_dtype="bf16", ssm_dtype=None)
    assert bf16_bpt > default_bpt * 1.5


# ---------------------------------------------------------------------------
# Shared-expert single count (P1): n_shared + shared_intermediate together
# ---------------------------------------------------------------------------


def test_shared_expert_counted_once_when_both_fields_present():
    base = {
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
        "num_experts_per_tok": 8,
        "moe_intermediate_size": 2048,
        "first_k_dense_replace": 3,
    }
    both = C.config_from_dict({**base, "n_shared_experts": 1, "shared_expert_intermediate_size": 2048})
    neither = C.config_from_dict(base)
    n_shared_only = C.config_from_dict({**base, "n_shared_experts": 1})
    p_both, _ = C.count_parameters(both)
    p_neither, _ = C.count_parameters(neither)
    p_nshared, _ = C.count_parameters(n_shared_only)
    shared_params = 3 * 7168 * 2048  # one shared expert per MoE layer
    n_moe = 58
    assert p_both == pytest.approx(p_nshared)  # inter sizes equal -> identical
    assert p_both == pytest.approx(p_neither + n_moe * shared_params)  # exactly ONE shared, not two


# ---------------------------------------------------------------------------
# checkpoint bytes x moe_runner (P2): 4-bit runner re-prices routed experts
# ---------------------------------------------------------------------------


def test_checkpoint_bytes_respect_moe_runner_4bit():
    cfg = _dsv41_cfg()
    params, _ = C.count_parameters(cfg)
    ckpt = int(params * 1.0)  # 1 B/param "checkpoint" (fp8-blended)
    plain, d_plain = C.weight_bytes_estimate(cfg, None, None, checkpoint_bytes=ckpt)
    marlin, d_marlin = C.weight_bytes_estimate(cfg, None, "marlin", checkpoint_bytes=ckpt)
    assert d_plain.get("checkpoint_bytes") == ckpt
    assert d_marlin.get("moe_runner") == "marlin"
    assert marlin < plain  # experts re-priced at 0.5 B/param -> strictly lighter
    share = d_marlin["moe_expert_share"]
    expected = ckpt * (1 - share) + params * share * 0.5
    assert marlin == pytest.approx(expected, rel=0.01)


# ---------------------------------------------------------------------------
# EP x PP advisory (P2) + CLI EP>GPUs warning (P1)
# ---------------------------------------------------------------------------


def test_ep_beyond_pp_stage_width_warns():
    cfg = _dsv41_cfg()
    gpu = G.resolve_gpu("H200")
    res = E.run_estimate(
        E.EstimateRequest(cfg=cfg, gpu=gpu, gpu_count=8, tp_size=4, pp_size=2, ep_size=8, context=8192)
    )
    assert any("stage width" in w for w in res.warnings)


def test_csa2_stored_layers_ceil_for_partial_groups():
    """A trailing partial encoder group still carries its Full-mode leader."""
    # 20 m=2 layers -> ceil(20/6) = 4 stored (was 3 under floor-division)
    stored = C._csa2_stored_layers([2] * 20 + [1] * 4)
    by_m = dict(stored)
    assert by_m[2] == 4
    assert by_m[1] == 1  # decoder: only the FIRST group's leader stores
    # the shipped schedule stays at the validated 3 + 1
    cfg = _dsv41_cfg()
    ratios = cfg.raw.get("text_config", cfg.raw)["compress_ratios"]
    shipped = dict(C._csa2_stored_layers(ratios))
    assert shipped[2] == 3 and shipped[1] == 1


# ---------------------------------------------------------------------------
# throughput (first-order roofline)
# ---------------------------------------------------------------------------


def _dsv3_active_cfg() -> dict:
    return {
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
        "num_experts_per_tok": 8,
        "moe_intermediate_size": 2048,
        "n_shared_experts": 1,
        "first_k_dense_replace": 3,
    }


def test_active_params_match_vendor():
    """Active params/token is an ARCHITECTURE fact shown in model structure
    (which weights a token reads) — not a throughput prediction."""
    from model_benchmarker.memory_model.throughput import active_params_per_token

    active, note = active_params_per_token(C.config_from_dict(_dsv3_active_cfg()))
    assert abs(active / 1e9 - 36.5) < 1.5  # vendor: ~37B active
    assert "8 of 256" in note


def test_csa2_decoder_structure_deviation_warns():
    """A decoder schedule NOT in the report's groups-of-4 flags itself (the
    stored-layer model is schedule-dependent; compress_ratios is consumed,
    so the generic unmodeled-fields warning cannot catch this)."""
    cfg = C.config_from_dict(
        {
            "model_type": "deepseek_v41",
            "hidden_size": 5120,
            "num_hidden_layers": 9,
            "num_attention_heads": 64,
            "num_key_value_heads": 1,
            "head_dim": 512,
            "index_head_dim": 128,
            "vocab_size": 1000,
            "compress_ratios": [2] * 6 + [1] * 3,  # 3 decoder layers: NOT %4
            "torch_dtype": "bf16",
        }
    )
    _, details = C.kv_bytes_per_token(cfg, None)
    assert any("not a multiple" in w for w in details.get("csa2_structure_warnings", []))


def test_dense_layers_use_intermediate_size_not_moe_width():
    """GLM-5.x / Gemma-A4B configs carry BOTH intermediate_size (dense
    layers) and moe_intermediate_size (expert FFNs) with different values;
    pricing dense layers at the MoE width understated GLM-5.3-Flash's
    weights. The bundled mirror pins the corrected count."""
    p = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "model_benchmarker"
        / "webapp"
        / "hf_configs"
        / "zai-org__GLM-5.3-Flash.json"
    )
    if not p.exists():  # pragma: no cover — mirror ships with the repo
        pytest.skip("GLM-5.3-Flash mirror not present")
    cfg = C.config_from_dict(json.loads(p.read_text()))
    params, _ = C.count_parameters(cfg)
    h, n = cfg.hidden_size, cfg.num_hidden_layers
    n_dense = n - len(C._moe_layer_indices(cfg))
    dense_w = cfg.intermediate_size
    moe_w = cfg.moe_intermediate_size
    # direct delta check: raise ONLY the dense width and the count must
    # move by exactly n_dense x 3h x delta (expert FFNs untouched)
    d = json.loads(p.read_text())
    bumped = d.get("text_config", d)
    bumped["intermediate_size"] = dense_w + 1024
    params_bumped, _ = C.count_parameters(C.config_from_dict(bumped))
    assert params_bumped - params == pytest.approx(n_dense * 3 * h * 1024, rel=0.001)
    # and the corrected full count is materially above the old mispriced one
    old_priced = params - n_dense * 3 * h * (dense_w - moe_w)
    assert params > old_priced
    assert params > 300e9  # GLM-5.3-Flash: ~312.5B corrected


# ---------------------------------------------------------------------------
# sparse-selector optimistic decode (v2) + calibration knobs + runner match
# ---------------------------------------------------------------------------


def test_moe_4bit_runner_substring_matching():
    """Engine runner names carry prefixes/suffixes; match by substring so
    marlin_mxfp4 / cutlass_mxfp4 re-price like their base names."""
    assert C.is_moe_4bit_runner("marlin")
    assert C.is_moe_4bit_runner("flashinfer_mxfp4")
    assert C.is_moe_4bit_runner("marlin_mxfp4")
    assert C.is_moe_4bit_runner("CUTLASS_MXFP4")
    assert not C.is_moe_4bit_runner("triton")
    assert not C.is_moe_4bit_runner("")
    assert not C.is_moe_4bit_runner(None)
    # the checkpoint re-split fires for a suffixed runner name too
    cfg = _dsv41_cfg()
    params, _ = C.count_parameters(cfg)
    ckpt = int(params)
    plain, _ = C.weight_bytes_estimate(cfg, None, None, checkpoint_bytes=ckpt)
    suffixed, d = C.weight_bytes_estimate(cfg, None, "marlin_mxfp4", checkpoint_bytes=ckpt)
    assert suffixed < plain and d.get("moe_runner") == "marlin_mxfp4"


def test_engram_host_budget_includes_engine_runtime():
    """The pod must hold the tables PLUS the engine's own runtime (~24 GiB
    measured on the seed V4.1-Flash deployment: 183.1 GiB tables -> 207 GiB
    working set). Comparing against the tables alone passed a 190 GiB limit
    that would OOM."""
    cfg = _dsv41_cfg()
    gpu = G.resolve_gpu("H200")

    def _host(budget: float):
        return E.run_estimate(
            E.EstimateRequest(
                cfg=cfg,
                gpu=gpu,
                gpu_count=4,
                tp_size=4,
                ep_size=4,
                kv_dtype="fp8_e4m3",
                mem_fraction=0.8,
                context=32768,
                engram=E.EngramSpec(mode="host", host_gib=budget),
            )
        )

    # 190 GiB holds the tables (183.1) but NOT tables+runtime -> must warn
    res190 = _host(190.0)
    warn = [w for w in res190.warnings if "host offload needs" in w]
    assert warn, "a 190 GiB budget must warn: 183.1 tables + ~24 runtime > 190"
    assert "207 GiB" in warn[0] and "183.1 GiB tables" in warn[0]
    # details carry the decomposed requirement
    ed = res190.details["engram"]
    assert ed["host_need_gib"] == pytest.approx(207.1, abs=0.5)
    assert ed["host_runtime_gib"] == 24.0
    # 256 GiB is comfortable -> no warning; 0 = no check
    assert not [w for w in _host(256.0).warnings if "host offload needs" in w]
    assert not [w for w in _host(0.0).warnings if "host offload needs" in w]
