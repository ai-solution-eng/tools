"""Numeric dtype handling for the memory estimator: bytes per element for
weights and for the KV cache, plus dtype-name normalization.

Zero dependencies, stdlib only.
"""

from __future__ import annotations

# bytes per element for weight dtypes
_WEIGHT_DTYPE_BYTES: dict[str, float] = {
    "fp64": 8.0,
    "f64": 8.0,
    "float64": 8.0,
    "double": 8.0,
    "fp32": 4.0,
    "f32": 4.0,
    "float32": 4.0,
    "float": 4.0,
    "tf32": 4.0,
    "bf16": 2.0,
    "bfloat16": 2.0,
    "fp16": 2.0,
    "f16": 2.0,
    "float16": 2.0,
    "half": 2.0,
    "fp8": 1.0,
    "fp8_e4m3": 1.0,
    "fp8_e5m2": 1.0,
    "e4m3": 1.0,
    "e5m2": 1.0,
    "float8": 1.0,
    "int8": 1.0,
    "nvfp4": 0.5,
    "fp4": 0.5,
    "nvfp4a16": 0.5,
    "int4": 0.5,
    "awq": 0.5,
    "gptq": 0.5,  # 4-bit weight quant families
    # modelopt (NVIDIA ModelOpt/TensorRT-LLM checkpoints): the width is
    # resolved from quantization_config.config_groups -> synthetic
    # modelopt4/modelopt8 names (see configs.config_from_dict)
    "modelopt4": 0.5,
    "modelopt8": 1.0,
}

# bytes per element for KV-cache dtypes
_KV_DTYPE_BYTES: dict[str, float] = {
    "fp8": 1.0,
    "fp8_e4m3": 1.0,
    "fp8_e5m2": 1.0,
    "e4m3": 1.0,
    "e5m2": 1.0,
    "float8": 1.0,
    "fp16": 2.0,
    "float16": 2.0,
    "half": 2.0,
    "bf16": 2.0,
    "bfloat16": 2.0,  # sglang/vllm "auto" stores KV in model dtype
}

WEIGHT_QUANT_ALIASES = ("awq", "gptq", "int4", "fp4", "nvfp4")


def normalize_dtype(name: str | None) -> str:
    """Normalize a dtype string (``FP8``, ``bfloat16``, ``float16`` ...)."""
    if not name:
        return ""
    low = name.lower().replace(" ", "").replace("_", "_")
    low = low.replace("float8_e4m3", "fp8_e4m3").replace("float8_e5m2", "fp8_e5m2")
    low = low.replace("float8", "fp8").replace("bfloat16", "bf16").replace("float16", "fp16")
    low = low.replace("float32", "fp32").replace("float64", "fp64")
    return low


def weight_bytes_per_param(dtype: str | None) -> float | None:
    """Bytes/parameter for a weight dtype, or None if unknown."""
    d = normalize_dtype(dtype)
    return _WEIGHT_DTYPE_BYTES.get(d) if d else None


def kv_bytes_per_elem(dtype: str | None) -> float | None:
    """Bytes/element for a KV-cache dtype, or None if unknown."""
    d = normalize_dtype(dtype)
    return _KV_DTYPE_BYTES.get(d) if d else None


def is_kv_dtype(dtype: str | None) -> bool:
    return normalize_dtype(dtype or "") in _KV_DTYPE_BYTES


def dtype_label(dtype: str | None) -> str:
    """Display label, e.g. ``fp8_e4m3`` -> ``FP8 (e4m3)``."""
    d = normalize_dtype(dtype)
    if not d:
        return "?"
    return {"fp8_e4m3": "FP8 (e4m3)", "fp8_e5m2": "FP8 (e5m2)", "fp8": "FP8", "modelopt4": "NVFP4 (modelopt)", "modelopt8": "FP8 (modelopt)"}.get(
        d, d.upper()
    )
