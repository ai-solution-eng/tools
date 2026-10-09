"""GPU specs for the memory estimator.

Pure data, zero dependencies. All VRAM figures are per-GPU totals in GiB
(1024-based, matching nvidia-smi's GiB); HBM bandwidth is informational
(display only, does not affect capacity math).

``2 groups of 4`` NVLink strategy note: NVLink topology changes
*performance*, not capacity — TP still pools memory across all GPUs, so the
capacity math below is identical for 1x8 NVLink and 2x4 NVLink boxes.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class GpuSpec:
    """One GPU model's capacity-relevant spec."""

    name: str
    vram_gib: float
    mem_bw_gbps: int = 0  # informational: HBM bandwidth, GB/s
    # peak DENSE tensor FLOPS (BF16 | FP16-acc-fp32), TFLOPS — used only by
    # the first-order throughput roofline; 0 = unknown (no prefill estimate)
    peak_tflops_bf16: float = 0.0
    # peak FP8 tensor TFLOPS (with FP32 accumulate), when the GPU is FP8-capable
    peak_tflops_fp8: float = 0.0

    @property
    def vram_bytes(self) -> float:
        return self.vram_gib * 1024**3


def _spec(name: str, vram: float, bw: int, bf16: float = 0.0, fp8: float = 0.0) -> GpuSpec:
    return GpuSpec(name=name, vram_gib=vram, mem_bw_gbps=bw, peak_tflops_bf16=bf16, peak_tflops_fp8=fp8)


# Keys are lowercase match tokens; the first match wins, so put the more
# specific key first (e.g. "h200 pcie" before "h200").
# FLOPS are vendor dense-peak figures (no sparsity): H200/H100 SXM 989 TF
# BF16 / 1979 FP8; H100 PCIe 756/1513; B200 2250/4500 dense (the 4.5 PFLOPS
# FP8 headline); RTX PRO 6000 Blackwell ~500 BF16 / ~1000 FP8 (GDDR7 part);
# L40S 362/733; A100 312/624; MI300X 1307.4/2614.9; MI325X ~1307/2614
# (same CDNA3 compute dies, more HBM).
GPU_DB: list[tuple[tuple[str, ...], GpuSpec]] = [
    (("h200 pcie", "h200 nvlp", "h200 pcie lx"), _spec("NVIDIA H200 PCIe", 141.0, 4800, 989.0, 1979.0)),
    (("h200 sxm", "h200 (sxm)", "h200"), _spec("NVIDIA H200 SXM", 141.0, 4800, 989.0, 1979.0)),
    (("rtx pro 6000", "rtxpro6000", "rtx 6000 pro"), _spec("NVIDIA RTX PRO 6000 Blackwell", 96.0, 1792, 500.0, 1000.0)),
    (("h100 pcie", "h100 nvl"), _spec("NVIDIA H100 PCIe", 80.0, 2040, 756.0, 1513.0)),
    (("h100 sxm", "h100 (sxm)", "h100"), _spec("NVIDIA H100 SXM", 80.0, 3350, 989.0, 1979.0)),
    (("l40s", "l40"), _spec("NVIDIA L40S", 48.0, 864, 362.0, 733.0)),
    (("a100 80", "a100"), _spec("NVIDIA A100 80GB", 80.0, 2040, 312.0, 624.0)),
    (("b200",), _spec("NVIDIA B200", 192.0, 8000, 2250.0, 4500.0)),
    (("gb200",), _spec("NVIDIA GB200", 192.0, 8000, 2250.0, 4500.0)),
    (("mi300x",), _spec("AMD MI300X", 192.0, 5300, 1307.4, 2614.9)),
    (("mi325x",), _spec("AMD MI325X", 256.0, 6000, 1307.4, 2614.9)),
]

# Non-GPU / unknown accelerator fallback: pass --gpu-vram to override.
DEFAULT_GPU = _spec("Unknown GPU (override with --gpu-vram)", 0.0, 0)


def resolve_gpu(name: str | None) -> GpuSpec | None:
    """Match a free-text GPU name to a :class:`GpuSpec`, or None."""
    if not name:
        return None
    low = name.lower().strip()
    for keys, spec in GPU_DB:
        for k in keys:
            if k in low:
                return spec
    return None
