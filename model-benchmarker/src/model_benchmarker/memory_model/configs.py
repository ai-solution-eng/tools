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

from .dtypes import _KV_DTYPE_BYTES, kv_bytes_per_elem, normalize_dtype, weight_bytes_per_param

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

    # sparse attention (DeepSeek-V4 CSA/HCA, GLM DSA, Qwen lightning indexer)
    index_topk: int | None = None
    compress_rates: dict | None = None
    index_n_heads: int | None = None
    index_head_dim: int | None = None
    # lightning indexer (Qwen3-Next / qwen4_exp families): a small compressed
    # key stream kept alongside the GQA KV for token selection
    indexer_head_dim: int | None = None
    indexer_n_heads: int | None = None
    indexer_kv_heads: int | None = None

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

    # engram (DeepSeek-V4.1 n-gram memory): one big lookup TABLE per engram
    # layer. Static WEIGHTS, not per-token KV -- the engine may pin them in
    # host RAM (SGLANG_ENABLE_DSV41_ENGRAM_HOST_TABLE=1), freeing HBM for
    # the KV pool. See engram_table_bytes().
    engram_layer_ids: list | None = None
    engram_num_embeddings: list | None = None
    engram_head_dim: int | None = None
    engram_n_heads: int | None = None
    engram_max_ngram_size: int | None = None
    engram_vocab_size: int | None = None
    engram_compressed_vocab_size: int | None = None

    # weights quantization as shipped (quantization_config in config.json)
    quant_method: str | None = None
    quant_bits: int | None = None
    # per-block dtype the checkpoint itself ships for ROUTED EXPERTS
    # (DeepSeek-V4.1-Flash: quantization_config.expert_dtype = "fp4" while
    # attention/dense are FP8 -- pricing experts at the checkpoint dtype)
    quant_expert_dtype: str | None = None

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
    # modelopt (NVIDIA TensorRT-LLM / ModelOpt checkpoint format): quant_method
    # is the literal "modelopt" and the real width lives in config_groups'
    # weights.num_bits (4 = NVFP4). Store a synthetic "modelopt4"/"modelopt8"
    # method the weight-dtype table can map.
    if str(quant_method).lower() == "modelopt" and isinstance(quant.get("config_groups"), dict):
        for _g in quant["config_groups"].values():
            if isinstance(_g, dict) and _g.get("weights", {}).get("num_bits") in (4, 8):
                quant_method = f"modelopt{_g['weights']['num_bits']}"
                quant_bits = int(_g["weights"]["num_bits"])
                break

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
        indexer_head_dim=_first("indexer_head_dim"),
        indexer_n_heads=_first("indexer_n_heads"),
        indexer_kv_heads=_first("indexer_kv_heads"),
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
        num_nextn_predict_layers=_first(
            "num_nextn_predict_layers", "mtp_num_hidden_layers"
        ),  # qwen4_exp names the built-in MTP module mtp_num_hidden_layers
        engram_layer_ids=d.get("engram_layer_ids") if isinstance(d.get("engram_layer_ids"), list) else None,
        engram_num_embeddings=(
            d.get("engram_num_embeddings") if isinstance(d.get("engram_num_embeddings"), list) else None
        ),
        engram_head_dim=_first("engram_head_dim"),
        engram_n_heads=_first("engram_n_heads"),
        engram_max_ngram_size=_first("engram_max_ngram_size"),
        engram_vocab_size=_first("engram_vocab_size"),
        engram_compressed_vocab_size=_first("engram_compressed_vocab_size"),
        quant_method=str(quant_method) if quant_method else None,
        quant_bits=quant_bits,
        quant_expert_dtype=(
            str(quant.get("expert_dtype")) if isinstance(quant, dict) and quant.get("expert_dtype") else None
        ),
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
    (~/.cache/huggingface) -> plain HTTPS fetch of the repo's config.json
    (stdlib, always available -- a config.json is a ~2-50 KB JSON file and
    the resolve endpoint redirects to the CDN) -> huggingface_hub download
    (if importable; the proper path when HF_TOKEN auth or a proxy is
    needed). Raises FileNotFoundError with a self-describing message
    otherwise.
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
        hub = Path(os.environ.get("HF_HOME", str(Path.home() / ".cache" / "huggingface"))) / "hub"
        safe = "models--" + ref.replace("/", "--")
        candidates += sorted((hub / safe / "snapshots").glob("*/config.json"))
    for cand in candidates:
        if cand.is_file():
            try:
                data = json.loads(cand.read_text(encoding="utf-8"))
            except (OSError, ValueError) as e:
                raise ValueError(f"cannot parse {cand}: {e}") from e
            return config_from_dict(data), str(cand)
    # network fetches (repo ids only). First a plain stdlib HTTPS GET of the
    # resolve endpoint -- zero dependencies, works in the slim serving image
    # where huggingface_hub is not installed; then the hub library (proper
    # auth/proxy handling) when importable.
    last_hub_error = ""
    if "/" in ref and not ref.startswith((".", "/", "~")):
        data, url, fetch_err = _https_fetch_config(ref)
        if isinstance(data, dict):
            return config_from_dict(data), url
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
        + (f" [https fetch: {fetch_err}]" if "/" in ref and not ref.startswith((".", "/", "~")) else "")
        + (last_hub_error if last_hub_error else "")
    )


_HF_RESOLVE_URL = "/{repo}/resolve/main/config.json"
_HF_INDEX_URL = "/{repo}/resolve/main/model.safetensors.index.json"


def checkpoint_total_bytes(model_ref: str | os.PathLike) -> tuple[int | None, str]:
    """The checkpoint's EXACT stored weight bytes, from
    ``model.safetensors.index.json`` -> ``metadata.total_size``.

    That field is the sum of every tensor's stored size the shards actually
    write (HF's own save-prep writes it), so it reflects the checkpoint's
    real dtype mix -- MoE-in-FP4/attention-in-FP8 like DeepSeek-V4-Flash, or
    Kimi K3's ~2.8T MXFP4 expert params -- without any per-dtype guessing.
    The shape-derived bytes/param estimate stays as the fallback for repos
    whose index is absent, and --weight-dtype remains the manual override.

    Resolution mirrors :func:`load_config_json`: local dir -> bundled/PVC
    mirror -> HF cache -> plain HTTPS fetch (the index is a small JSON; the
    big shards are never downloaded). Returns (bytes | None, source).
    """
    ref = str(model_ref).strip()
    p = Path(ref).expanduser()
    candidates: list[Path] = []
    if p.is_dir():
        candidates.append(p / "model.safetensors.index.json")
    if not p.is_file() and "/" in ref:
        # mirror the config.json lookup for the same bundled/PVC names
        name = ref.replace("/", "__") + ".safetensors.index.json"
        here = Path(__file__).resolve()
        candidates += [
            Path(c)
            for c in (
                str(here.parents[1] / "webapp" / "hf_configs" / name),
                str(Path(os.environ.get("BENCH_WORK_DIR") or "/data") / "hf-configs" / name),
            )
            if Path(c).is_file()
        ]
        hub = Path(os.environ.get("HF_HOME", str(Path.home() / ".cache" / "huggingface"))) / "hub"
        safe = "models--" + ref.replace("/", "--")
        candidates += sorted((hub / safe / "snapshots").glob("*/model.safetensors.index.json"))
    for cand in candidates:
        if cand.is_file():
            try:
                data = json.loads(cand.read_text(encoding="utf-8"))
                total = int((data.get("metadata") or {}).get("total_size") or 0)
            except (OSError, ValueError, TypeError):
                return None, str(cand)
            return (total or None), str(cand)
    if "/" in ref and not ref.startswith((".", "/", "~")):
        base = (os.environ.get("HF_ENDPOINT") or "https://huggingface.co").rstrip("/")
        if base.startswith("https://"):
            data, _url, _err = _https_fetch_json(base + _HF_INDEX_URL.format(repo=ref))
            if isinstance(data, dict):
                try:
                    total = int((data.get("metadata") or {}).get("total_size") or 0)
                except (TypeError, ValueError):
                    total = 0
                if total:
                    return total, base + _HF_INDEX_URL.format(repo=ref)
    return None, ""


def _ca_bundle_candidates() -> list[str]:
    """Trust-store paths to try, most-authoritative first: the operator's
    explicit env (REQUESTS_CA_BUNDLE / SSL_CERT_FILE — the standard way to
    inject a corporate TLS-intercept root), then certifi's bundle when the
    package ships (a Python whose default verify paths are stale — the
    conda/python3.14 pattern — still verifies correctly through certifi)."""
    out: list[str] = []
    for var in ("REQUESTS_CA_BUNDLE", "SSL_CERT_FILE"):
        v = os.environ.get(var)
        if v and Path(v).is_file():
            out.append(v)
    try:
        import certifi

        out.append(certifi.where())
    except ImportError:
        pass
    return out


def _https_fetch_json(url: str, timeout: float = 15.0) -> tuple[dict | list | None, str, str]:
    """Stdlib HTTPS GET of a small public JSON document (config.json,
    model.safetensors.index.json, hub list APIs) with the fleet trust
    ladder: default verify paths -> REQUESTS_CA_BUNDLE/SSL_CERT_FILE ->
    certifi -> (last resort: public metadata, no token on the wire)
    unverified TLS with an explicit marker in the returned source so the
    artifact stays honest. Returns (data | None, source_url, error) where
    data is a JSON object OR array (the hub's list APIs return arrays;
    config.json callers pass dicts onward)."""
    try:
        import ssl
        import urllib.request

        token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN") or ""
        headers = {"User-Agent": "model-benchmarker/0.4"}
        if token:
            headers["Authorization"] = f"Bearer {token}"

        attempts: list[tuple[str, ssl.SSLContext | None]] = [("system default", None)]
        for ca in _ca_bundle_candidates():
            bundle_ctx = ssl.create_default_context(cafile=ca)
            attempts.append((f"CA bundle {ca}", bundle_ctx))
        # last resort: public metadata, no credentials on the wire, marked
        attempts.append(("unverified TLS (no CA bundle verified the chain)", ssl._create_unverified_context()))

        last_err = ""
        for label, attempt_ctx in attempts:
            req = urllib.request.Request(url, headers=headers)
            try:
                with urllib.request.urlopen(
                    req, timeout=timeout, context=attempt_ctx
                ) as resp:  # https pinned by the guard above
                    charset = resp.headers.get_content_charset() or "utf-8"
                    payload = resp.read().decode(charset, errors="replace")
                data = json.loads(payload)
            except Exception as exc:
                last_err = f"{type(exc).__name__}: {exc}"[:200]
                continue  # next trust rung
            if isinstance(data, (dict, list)):
                verified = label.startswith(("system", "CA"))
                return data, url if verified else url + " (unverified TLS)", ""
            return None, url, "not a JSON document"
        return None, url, last_err or "fetch failed"
    except Exception as exc:  # env/guard errors surface in the final message
        return None, url, f"{type(exc).__name__}: {exc}"[:200]


def _https_fetch_config(repo_id: str, timeout: float = 15.0) -> tuple[dict | list | None, str, str]:
    """Stdlib HTTPS GET of a repo's config.json (no huggingface_hub needed).

    Honors HF_ENDPOINT for mirror deployments. See :func:`_https_fetch_json`
    for the trust ladder. Returns (data | None, source_url, error).
    """
    base = (os.environ.get("HF_ENDPOINT") or "https://huggingface.co").rstrip("/")
    if not base.startswith("https://"):
        return None, "", "HF_ENDPOINT must be an https:// URL"
    return _https_fetch_json(base + _HF_RESOLVE_URL.format(repo=repo_id), timeout=timeout)


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
    # compressed-attention families (deepseek_v4/v41): per-layer compression
    # rates -- csa = m=4 dual-stream blocks, hca = big-m single-stream
    # blocks, uncompressed = rate-0 layers. These are NOT sliding-window
    # layers even when the config carries sliding_window (V4's 128 is the
    # HCA window, not SWA), so they get their own counts + label.
    csa: int = 0
    hca: int = 0
    uncompressed: int = 0
    # CSA2 (deepseek_v41): layers that reuse a Full-mode layer's stored
    # stream instead of storing their own (rate-2 encoder layers)
    reuse: int = 0

    @property
    def kv_layers(self) -> int:
        return self.full + self.sliding

    def label(self) -> str:
        if self.csa or self.hca or self.uncompressed or self.reuse:
            parts = []
            if self.csa:
                parts.append(f"{self.csa} CSA (m=4)")
            if self.hca:
                parts.append(f"{self.hca} HCA (m=128, win={self._win})" if self._win else f"{self.hca} HCA (m=128)")
            if self.full:
                parts.append(f"{self.full} stored (m=1)")
            if self.reuse:
                parts.append(f"{self.reuse} CSA2-reuse (2:1)")
            if self.uncompressed:
                parts.append(f"{self.uncompressed} uncompressed")
            if self.linear:
                parts.append(f"{self.linear} linear (no KV)")
            return " + ".join(parts)
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
    # compressed-attention families: classify from the compression schedule
    # (back-to-back CSA/HCA -- sliding_window is the HCA window, not SWA).
    # V4.1 (CSA2) semantics: rate-2 ENCODER layers compress 2 positions into
    # 1 and REUSE the nearest Full-mode layer's stored stream (no store of
    # their own); rate-1 DECODER layers store per token but only the group
    # LEADER is Full Mode -- the rest are Reindex Mode (reuse, no store).
    raw0 = cfg.raw.get("text_config", cfg.raw)
    sched = raw0.get("compress_ratios") if isinstance(raw0, dict) else None
    if (cfg.model_type or "").startswith("deepseek_v4") and isinstance(sched, list) and sched:
        is_csa2 = (cfg.model_type or "").startswith("deepseek_v41")
        n = cfg.num_hidden_layers or len(sched)
        for r in sched[:n]:
            if r == 0:
                mix.uncompressed += 1
            elif r == 4:
                mix.csa += 1
            elif r == 2 and is_csa2:
                mix.reuse += 1  # CSA2 encoder: 2:1 into a shared stored stream
            elif r == 1:
                mix.full += 1  # m=1 decoder: per-token store (group leaders)
            else:
                mix.hca += 1
        return mix
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


# Advisory notes from _csa2_stored_layers (schedule-structure deviations the
# shipped group model cannot represent); read by kv_bytes_per_token's v41
# branch into the module-level holder and attached to the KV details below
# so they reach the artifact.
_csa2_structure_notes: list[str] = []
_csa2_notes_holder: dict = {}


def _csa2_stored_layers(ratios: list) -> list[tuple[int, int]]:
    """V4.1 CSA2 mode structure from the tech report (sec 4.2.1): encoder
    layers of rate m=2 form groups of six with one Full Mode layer
    each; decoder layers of rate m=1 form groups of four where only the
    FIRST group's leader is Full Mode -- the remaining group leaders are
    Reindex Mode, which reuse (do not store) the main KV and indexer K.
    Only Full Mode layers store global KV. Returns
    [(m, n_stored_layers), ...].

    The group sizes derive from the schedule's own runs of consecutive
    equal ratios (the shipped schedule: 18 x m=2 -> 3 Full, 20 x m=1 ->
    1 Full; remainder layers in a partial trailing group still get their
    group's Full-mode leader -- ceil, never fewer stores than a group
    boundary would imply)."""
    stored: list[tuple[int, int]] = []
    notes: list[str] = []
    for m, group_size in ((2, 6), (1, 4)):
        n = sum(1 for r in ratios if r == m)
        if not n:
            continue
        if m == 1:
            # decoder groups: only the FIRST group's leader is Full Mode
            # (stores); the remaining group leaders are Reindex Mode and
            # reuse the main KV stream -- 1 stored layer for any n_dec.
            # Schedule-dependent: the tech report's shipped structure is
            # groups of four (20 = 5 x 4); a variant with a different
            # decoder group size would silently change the true store
            # count -- flagged by the caller's unmodeled-fields path.
            if n % group_size:
                notes.append(f"decoder m=1 layer count {n} is not a multiple of the report's group size 4")
            stored.append((1, 1))
        else:
            # encoder groups: one Full Mode (storing) layer per group of
            # six; a trailing partial group still carries its leader
            # (ceil -- never fewer stores than a group boundary implies).
            stored.append((2, max(1, -(-n // group_size))))
    _csa2_structure_notes.extend(notes)
    return stored


def state_bytes_per_elem(dtype: str | None, default: float = 2.0) -> float | None:
    """Bytes/elem for a compressed-state / SSM-state dtype (the engine's
    state-storage knobs accept the same names the KV table does). None when
    unknown (caller keeps its formula default)."""
    d = normalize_dtype(dtype)
    if not d:
        return None
    table = {**_KV_DTYPE_BYTES, "fp4": 0.5, "nvfp4": 0.5, "int4": 0.5, "fp32": 4.0, "fp64": 8.0}
    return table.get(d)


def _compressed_attn_bytes_per_gpu(cfg: ModelConfig, bytes_per_elem: float, state_bpe: float | None = None) -> float:
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

    ``state_bpe`` overrides the compressed-state bytes/elem (the deployment's
    SGLANG_DSV4_COMPRESS_STATE_DTYPE env: 'bf16' prices the stored entries at
    2 B/elem instead of the FP4+scales layout -- a real engine knob, the
    seed deployment runs bf16). RoPE dims in the V4 mixed-precision entry
    keep their BF16 cost regardless (they are stored bf16 by construction).
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
        # CSA2: FP4 entries + scale bytes, only Full Mode layers store.
        # state_bpe override (compress-state dtype knob): entry priced flat
        # at state_bpe per element, indexer likewise (no FP4 microscaling).
        _csa2_structure_notes.clear()
        per_token = 0.0
        for m, n in _csa2_stored_layers(ratios):
            if state_bpe is not None:
                entry = dims * state_bpe
                indexer = idx_dims * state_bpe
            else:
                entry = dims * 0.5 + dims / 16.0  # FP4 + E4M3 scale per 16 ch
                indexer = idx_dims * 0.5 + idx_dims / 16.0
            per_token += n * (entry + indexer) / m
        # schedule-structure deviations collected by _csa2_stored_layers
        # surface in the KV details (they qualify the headline figure)
        if _csa2_structure_notes:
            _csa2_notes_holder["notes"] = list(_csa2_structure_notes)
            _csa2_structure_notes.clear()
        return per_token

    # V4 CSA/HCA hybrid: every layer stores; mixed-precision entry
    # (RoPE dims BF16, rest FP8 per the tech-notes table). When the caller
    # passes no state dtype, the FP8 portion prices at its CHECKPOINT width
    # (1 B/elem) — the engine stores compressed streams fp8 unless
    # SGLANG_DSV4_COMPRESS_STATE_DTYPE says otherwise, and the docstring's
    # 6,810 B/token worked check must reproduce from default arguments.
    # (The old behavior priced the default at the KV dtype's 2 B, yielding
    # 12,256 for the bundled V4-Flash config unless fp8 was passed.)
    rope = cfg.qk_rope_head_dim or 0
    fp8_dims = dims - rope
    entry = fp8_dims * (state_bpe if state_bpe is not None else 1.0) + rope * 2.0
    n_csa = sum(1 for r in ratios if r == 4)
    n_hca = sum(1 for r in ratios if r not in (0, 1, 4))
    per_token = n_csa * (2 * entry + idx_dims * (state_bpe if state_bpe is not None else 1.0)) / 4.0
    per_token += n_hca * entry / 128.0
    return per_token


def engram_table_bytes(cfg: ModelConfig, dtype: str | None = None) -> tuple[float | None, dict]:
    """Bytes for the checkpoint's engram (n-gram memory) tables.

    DeepSeek-V4.1 ships one lookup table per engram layer
    (``engram_layer_ids`` x ``engram_num_embeddings`` rows of
    ``engram_head_dim`` values). The SGLang model page prices these as FP8
    tables -- "the largest single block of weight in the checkpoint" --
    row-sharded across the TP group by DEFAULT (GPU-resident), with
    ``SGLANG_ENABLE_DSV41_ENGRAM_HOST_TABLE=1`` moving them to host RAM
    (layout knob SGLANG_DSV41_ENGRAM_HOST_TABLE_LAYOUT = shared | per_rank;
    the host table holds the FULL table either way -- 'shared' one copy all
    ranks read, 'per_rank' one shard per rank).

    Calibration (live seed deployment, 4xH200 TP4/EP4): 2 tables x 384.0M
    rows x 256 dims x 1 B (fp8) = 183.1 GiB -- container_memory_working_set
    207 GiB = the tables + ~24 GiB runtime; pod request 350Gi / limit 512Gi
    bracket it with huge-page headroom.

    These are WEIGHTS, not per-token KV: they do not scale with context.
    Returns (bytes, details); bytes None when the config carries no engram
    block (or a broken one -- a mismatched ids/counts pair reports itself in
    details and through unmodeled_structural_fields).
    """
    raw = cfg.raw.get("text_config", cfg.raw)
    ids = cfg.engram_layer_ids or (raw.get("engram_layer_ids") if isinstance(raw, dict) else None)
    counts = cfg.engram_num_embeddings or (raw.get("engram_num_embeddings") if isinstance(raw, dict) else None)
    if not (isinstance(ids, list) and isinstance(counts, list) and ids and len(ids) == len(counts)):
        if ids or counts:
            return None, {"present": True, "parse_ok": False}
        return None, {"present": False}
    hdim = cfg.engram_head_dim
    if not hdim:
        return None, {"present": True, "parse_ok": False}
    dt = normalize_dtype(dtype) or "fp8"  # the engine ships them fp8
    bpe = state_bytes_per_elem(dt) or 1.0
    total_rows = 0
    per_table: list[dict] = []
    for layer_id, rows in zip(ids, counts):
        if not isinstance(rows, (int, float)):
            continue
        total_rows += int(rows)
        per_table.append({"layer": int(layer_id), "rows": int(rows), "gib": int(rows) * hdim * bpe / 1024**3})
    bytes_total = total_rows * hdim * bpe
    details = {
        "present": True,
        "parse_ok": True,
        "layers": len(per_table),
        "rows": total_rows,
        "head_dim": hdim,
        "dtype": dt,
        "bytes_per_row": hdim * bpe,
        "bytes": bytes_total,
        "per_table": per_table,
        "assumption": (
            f"rows x engram_head_dim({hdim}) x {bpe:g} B ({dt}); SGLang ships the tables fp8 "
            "-- calibrated against the live seed deployment (183 GiB tables + ~24 GiB runtime = 207 GiB working set)"
        ),
    }
    if cfg.engram_compressed_vocab_size:
        details["compressed_vocab_size"] = cfg.engram_compressed_vocab_size
    if cfg.engram_max_ngram_size:
        details["max_ngram_size"] = cfg.engram_max_ngram_size
    return bytes_total, details


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
        ("indexer_head_dim", "lightning-indexer key stream"),
        ("indexer_budget", "lightning-indexer selection budget"),
        ("linear_key_head_dim", "linear-attention state"),
        ("dspark_block_size", "DSPARK speculative state"),
        ("engram_layer_ids", "engram n-gram memory tables (static weights)"),
        ("engram_num_embeddings", "engram n-gram memory tables (static weights)"),
        ("engram_head_dim", "engram n-gram memory tables (static weights)"),
    ):
        if (raw.get(fname) is not None or getattr(cfg, fname, None) is not None) and fname not in consumed:
            carried.append(fname + " (" + meaning + ")")
    if carried:
        carried = list(dict.fromkeys(carried))
    return carried


def _linear_state_bytes_per_seq(cfg: ModelConfig, bytes_per_elem: float) -> float:
    """Recurrent state per sequence for gated-deltanet/mamba2 layers."""
    khd = cfg.linear_key_head_dim or 0
    vhd = cfg.linear_value_head_dim or 0
    vh = cfg.linear_num_value_heads or 0
    if not (khd and vhd and vh):
        return 0.0
    return vh * vhd * khd * bytes_per_elem


def _ssm_bytes_per_elem(cfg: ModelConfig) -> float:
    """Bytes per element of the linear-attention recurrent state: the
    config's mamba_ssm_dtype when present (qwen4_exp ships float32), else
    the KV dtype guess (2 B)."""
    raw = cfg.raw.get("text_config", cfg.raw)
    ssm = str(raw.get("mamba_ssm_dtype") or "").lower()
    return {"float32": 4.0, "fp32": 4.0, "bfloat16": 2.0, "bf16": 2.0, "float16": 2.0, "fp16": 2.0, "float8": 1.0}.get(
        ssm, 2.0
    )


def kv_bytes_per_token(
    cfg: ModelConfig, kv_dtype: str | None, state_dtype: str | None = None, ssm_dtype: str | None = None
) -> tuple[float, dict]:
    """Bytes per token for the whole stack (KV-cache dtype applied).

    ``state_dtype``: the compressed-state storage dtype override (the
    deployment's SGLANG_DSV4_COMPRESS_STATE_DTYPE env for the DeepSeek
    families) -- applies to the compressed-latent entries, NOT to plain
    GQA/MLA KV (those follow ``kv_dtype``).
    ``ssm_dtype``: linear-attention recurrent-state dtype override
    (mamba float32 vs bfloat16 -- config default from mamba_ssm_dtype).

    Returns (bytes_per_token, details) where details carries the per-layer
    figure, layer mix, and the formula used — everything the artifact prints.
    """
    bpe = 2.0  # default: fp16/bf16 KV

    got = kv_bytes_per_elem(kv_dtype)
    if got is not None:
        bpe = got

    state_bpe = state_bytes_per_elem(state_dtype) if state_dtype else None
    ssm_bpe = state_bytes_per_elem(ssm_dtype) if ssm_dtype else None
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
        if "dsa" in mt and cfg.index_head_dim:
            # GLM DSA (glm_moe_dsa): the sparse selector keeps its own small
            # index-K stream per FULL layer alongside the latent. Priced per
            # the GLM-5 family paper: the selection reads the index keys
            # compressed at the DSA token-granularity, one index_head_dim
            # entry per token per layer (the index_*_heads count projection
            # heads, not stored streams). Only makes the estimate MORE
            # conservative; engines reading less make real capacity better.
            idx = cfg.index_head_dim * bpe
            per_layer += idx
            details["per_layer_bytes"] = per_layer
            details["indexer_bytes_per_layer"] = idx
            details["formula"] += f" + DSA index {cfg.index_head_dim} x {bpe:g} B"
            details["arch"] += " + index-K stream"
            details["_consumed_fields"] |= {"index_head_dim", "index_n_heads", "index_topk"}
        return per_layer * mix.kv_layers, details

    # --- shared-KV MQA + compressed sparse attention ------------------------
    if mt.startswith("deepseek_v4") or "dsa" in mt:
        per_gpu_bpt = _compressed_attn_bytes_per_gpu(cfg, bpe, state_bpe)
        mix = classify_layers(cfg)
        details.update(
            arch="MQA + compressed sparse attention (CSA/HCA + DSPARK)"
            + (f" + {state_dtype} compress-state" if state_bpe is not None else ""),
            formula=(
                f"{cfg.model_type}: "
                + (
                    (
                        f"4 Full-mode layers x ({cfg.head_dim}ch x {state_bpe:g}B + indexer {cfg.index_head_dim or 0}ch"
                        f" x {state_bpe:g}B) / m -- CSA2 reuse, compress-state {state_dtype}"
                    )
                    if (mt.startswith("deepseek_v41") and state_bpe is not None)
                    else (
                        "4 Full-mode layers x (FP4 entry 288B + indexer 72B) / m -- CSA2 reuse"
                        if mt.startswith("deepseek_v41")
                        else "CSA dual-stream + indexer / m=4, HCA / m'=128 -- every layer stores"
                    )
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
        if _csa2_notes_holder.get("notes"):
            details["csa2_structure_warnings"] = list(_csa2_notes_holder["notes"])
            _csa2_notes_holder.clear()
        if state_bpe is not None:
            details["compress_state_dtype"] = state_dtype
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
    if mix.linear:
        # linear-attention layers carry NO per-token KV, but each keeps a
        # recurrent state PER SEQUENCE per layer (SGLang's mamba/KDA pool —
        # a separate concurrency ceiling, not part of the per-token figure).
        state = _linear_state_bytes_per_seq(cfg, ssm_bpe if ssm_bpe is not None else _ssm_bytes_per_elem(cfg))
        details["_consumed_fields"] |= {
            "linear_key_head_dim",
            "linear_value_head_dim",
            "linear_num_key_heads",
            "linear_num_value_heads",
        }
        if state:
            details["linear_state_bytes_per_seq"] = state
            details["note"] = (
                f"linear-attention layers: ~{state * mix.linear / 1024**2:.0f} MiB recurrent state per "
                "concurrent sequence (all linear layers; SGLang mamba/KDA pool — a separate "
                "concurrency ceiling on top of the KV pool, not priced per token)"
                + (f" @ {ssm_dtype} state dtype (explicit)" if ssm_bpe is not None else "")
            )
    if cfg.indexer_head_dim and mix.full:
        # lightning indexer (Qwen3-Next / qwen4_exp): full-attention layers
        # keep ONE small compressed key stream per token for token selection
        # (fp8-quantizable; priced at the KV dtype — conservative). The
        # indexer_*_heads count projection heads, not stored streams.
        idx = cfg.indexer_head_dim * bpe
        per_layer_full = per_layer + idx
        details["indexer_bytes_per_layer"] = idx
        details["per_layer_bytes"] = per_layer_full
        details["formula"] += f" + indexer K {cfg.indexer_head_dim} x {bpe:g} B (full layers)"
        details["arch"] += " + lightning indexer"
        details["_consumed_fields"] |= {"indexer_head_dim", "indexer_n_heads", "indexer_kv_heads", "indexer_budget"}
        if mix.sliding:
            return per_layer_full * mix.full + per_layer * mix.sliding, details
        return per_layer_full * mix.full, details
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
        # expert FFN width (routed/shared experts); dense layers below use
        # the config's own intermediate_size -- the two widths differ in
        # real configs (GLM-5.x: 2048 vs 12288)
        inter = cfg.moe_intermediate_size or cfg.intermediate_size or 4 * h
        if i in moe_layer_idx:
            experts = cfg.experts or 0
            mlp_total += experts * 3 * h * inter
            # shared experts: n_shared_experts counts TABLES,
            # shared_expert_intermediate_size sizes ONE. When both ship
            # (real DeepSeek-V3: n_shared_experts=1 + shared_expert_
            # intermediate_size), count n_shared x shared_inter ONCE —
            # never fall through to the generic inter branch as well
            # (the old double count added +2.55B params to DSV3).
            if cfg.n_shared_experts:
                shared_inter = cfg.shared_expert_intermediate_size or inter
                mlp_total += cfg.n_shared_experts * 3 * h * shared_inter
            elif cfg.shared_expert_intermediate_size:
                mlp_total += 3 * h * cfg.shared_expert_intermediate_size
            mlp_total += h * experts  # router gate
        else:
            # DENSE layers use intermediate_size, NOT moe_intermediate_size
            # (GLM-5.x / Gemma-A4B configs carry both with different values;
            # pricing dense at the MoE width understates their weights)
            mlp_total += 3 * h * (cfg.intermediate_size or 4 * h)

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
# the checkpoint dtype (the engine dequantizes on the fly): the PCAI serving
# shapes for FP8 MoE checkpoints on smaller GPUs. Matching is by SUBSTRING on
# the normalized runner name -- engines spell these with prefixes/suffixes
# (e.g. "flashinfer_mxfp4", "marlin_mxfp4", "cutlass_mxfp4").
_MOE_4BIT_RUNNERS = ("flashinfer_mxfp4", "marlin", "mxfp4")


def is_moe_4bit_runner(runner: str | None) -> bool:
    """True when the runner keeps routed experts at 4-bit at runtime."""
    r = (runner or "").strip().lower()
    return bool(r) and any(tag in r for tag in _MOE_4BIT_RUNNERS)


def weight_bytes_estimate(
    cfg: ModelConfig,
    weight_dtype: str | None,
    moe_runner: str | None = None,
    checkpoint_bytes: int | None = None,
    engram_bytes: int | None = None,
) -> tuple[float | None, dict]:
    """(bytes, details) for weights; None bytes when the dtype is unknown.

    ``weight_dtype=None`` resolves from the config first
    (quantization_config -> torch_dtype), so callers only override when
    they know better.

    ``checkpoint_bytes``: the EXACT stored size from the checkpoint's
    model.safetensors.index.json (metadata.total_size) when resolvable --
    the authoritative figure for mixed-precision checkpoints (MoE-in-FP4 +
    attention-in-FP8 like DeepSeek-V4-Flash, or Kimi K3's MXFP4 experts),
    since it reflects what the shards actually write. ``engram_bytes``: the
    engram tables' bytes when the config carries an engram block -- they
    are INSIDE the index total but placed separately (TP-shard in VRAM or
    host-table offload), so they are subtracted here and re-accounted by
    the caller. The shape-derived bytes/param estimate is the fallback.

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
    details["dtype_known"] = bpp is not None
    details["bytes_per_param"] = bpp
    runner = (moe_runner or "").strip().lower()

    if checkpoint_bytes and not is_moe_4bit_runner(runner):
        # authoritative: what the shards actually write -- independent of the
        # dtype tables (this is the point: a mixed-precision checkpoint's
        # dtype mix is unknowable from shape math). The engram tables ride
        # inside the index total; lift them out (the caller places them).
        stored = float(checkpoint_bytes)
        details["checkpoint_bytes"] = stored
        details["checkpoint_source"] = "model.safetensors.index.json metadata.total_size"
        lifted = 0.0
        if engram_bytes:
            lifted = min(float(engram_bytes), stored)
            details["engram_bytes_lifted"] = lifted
        details["bytes"] = stored - lifted
        return details["bytes"], details
    if checkpoint_bytes and is_moe_4bit_runner(runner):
        # a 4-bit MoE RUNNER re-stores routed experts at 4-bit at runtime
        # regardless of the checkpoint's dtype (the PCAI small-GPU shape:
        # DeepSeek-V4-Flash 291B fits 2x RTX PRO 6000 ONLY this way), so the
        # checkpoint total cannot be used as-is -- split it: attention/dense
        # keep the stored share, routed experts re-price at 0.5 B/param.
        from .estimate import _expert_param_share

        share = _expert_param_share(cfg)
        stored = float(checkpoint_bytes)
        lifted = min(float(engram_bytes), stored) if engram_bytes else 0.0
        body = stored - lifted
        dense_share = max(1.0 - share, 0.0)
        expert_bpp = weight_bytes_per_param("nvfp4") or 0.5
        params, _notes = count_parameters(cfg)
        main_bpp = body / params if params else bpp  # checkpoint's blended B/param
        details["checkpoint_bytes"] = stored
        details["checkpoint_source"] = "model.safetensors.index.json metadata.total_size"
        details["moe_runner"] = runner
        details["moe_expert_share"] = share
        details["bytes"] = body * dense_share + params * share * expert_bpp
        details["notes"] = list(notes) + [
            (
                f"moe-runner {runner}: checkpoint total re-split -- routed experts (~{share * 100:.0f}% of params) "
                f"re-priced at 4-bit ({expert_bpp:g} B/param), rest kept at the checkpoint's blended "
                f"{main_bpp:.2f} B/param"
            ),
        ]
        return details["bytes"], details
    if bpp is None:
        return None, details

    # --- shape-derived fallback (bytes/param x params) ----------------------
    # checkpoint-declared expert dtype (quantization_config.expert_dtype --
    # DeepSeek-V4.1-Flash ships routed experts as fp4 inside an otherwise
    # FP8 checkpoint): split the cost instead of pricing experts at the
    # checkpoint's main dtype.
    expert_bpp_checkpoint: float | None = None
    if cfg.quant_expert_dtype and cfg.is_moe:
        expert_bpp_checkpoint = weight_bytes_per_param(cfg.quant_expert_dtype)
        if expert_bpp_checkpoint is not None and expert_bpp_checkpoint != bpp:
            details["quant_expert_dtype"] = cfg.quant_expert_dtype
    if cfg.is_moe and is_moe_4bit_runner(runner):
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
    elif expert_bpp_checkpoint is not None:
        from .estimate import _expert_param_share

        share = _expert_param_share(cfg)
        details["moe_expert_share"] = share
        details["bytes"] = params * ((1 - share) * bpp + share * expert_bpp_checkpoint)
        details["notes"] = list(notes) + [
            (
                f"checkpoint quantization_config.expert_dtype={cfg.quant_expert_dtype}: routed experts "
                f"(~{share * 100:.0f}% of params) at {expert_bpp_checkpoint:g} B/param, rest at {bpp:g} B/param"
            ),
        ]
    else:
        details["bytes"] = params * bpp
    return details["bytes"], details


def default_weight_dtype(cfg: ModelConfig) -> str | None:
    """Weights dtype guess: quantization_config first, else torch_dtype."""
    if cfg.quant_method:
        q = normalize_dtype(cfg.quant_method)
        if q in ("fp8", "nvfp4", "fp4", "mxfp4", "int4", "gptq", "awq", "int8", "modelopt4", "modelopt8"):
            return q
    td = cfg.raw.get("torch_dtype") or cfg.raw.get("dtype")
    return normalize_dtype(str(td)) if td else None
