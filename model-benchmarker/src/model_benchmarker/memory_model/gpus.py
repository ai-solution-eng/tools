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

    @property
    def vram_bytes(self) -> float:
        return self.vram_gib * 1024**3


def _spec(name: str, vram: float, bw: int) -> GpuSpec:
    return GpuSpec(name=name, vram_gib=vram, mem_bw_gbps=bw)


# Keys are lowercase match tokens; the first match wins, so put the more
# specific key first (e.g. "h200 pcie" before "h200").
GPU_DB: list[tuple[tuple[str, ...], GpuSpec]] = [
    (("h200 pcie", "h200 nvlp", "h200 pcie lx"), _spec("NVIDIA H200 PCIe", 141.0, 4800)),
    (("h200 sxm", "h200 (sxm)", "h200"), _spec("NVIDIA H200 SXM", 141.0, 4800)),
    (("rtx pro 6000", "rtxpro6000", "rtx 6000 pro"), _spec("NVIDIA RTX PRO 6000 Blackwell", 96.0, 1792)),
    (("h100 pcie", "h100 nvl"), _spec("NVIDIA H100 PCIe", 80.0, 2040)),
    (("h100 sxm", "h100 (sxm)", "h100"), _spec("NVIDIA H100 SXM", 80.0, 3350)),
    (("l40s", "l40"), _spec("NVIDIA L40S", 48.0, 864)),
    (("a100 80", "a100"), _spec("NVIDIA A100 80GB", 80.0, 2040)),
    (("b200",), _spec("NVIDIA B200", 192.0, 8000)),
    (("gb200",), _spec("NVIDIA GB200", 192.0, 8000)),
    (("mi300x",), _spec("AMD MI300X", 192.0, 5300)),
    (("mi325x",), _spec("AMD MI325X", 256.0, 6000)),
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
