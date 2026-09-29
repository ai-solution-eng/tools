"""memory_model — deterministic GPU memory estimation for LLM serving.

Answers, for a given model + GPU deployment shape:
  * can it fit in memory?          -> EstimateResult.fits
  * how many tokens fit in memory? -> EstimateResult.kv_tokens_total
  * how many N-token requests?     -> EstimateResult.concurrency

Structure comes from the model's own config.json; serving parameters
(TP, KV dtype, mem-fraction) come from flags or the seed deployment
catalog. Zero dependencies beyond stdlib (huggingface_hub optional).
"""

from .configs import ModelConfig, config_from_dict, load_config_json
from .estimate import (
    DEFAULT_OVERHEAD_GIB,
    EstimateRequest,
    EstimateResult,
    grid,
    run_estimate,
    summary_line,
)
from .gpus import GpuSpec, resolve_gpu

# NOTE: the estimate *module* keeps the name `estimate` — importing
# `model_benchmarker.memory_model.estimate` yields the module, and the
# entry point function is exposed as `run_estimate` (a module/function
# name collision would shadow one or the other).

__all__ = [
    "DEFAULT_OVERHEAD_GIB",
    "EstimateRequest",
    "EstimateResult",
    "GpuSpec",
    "ModelConfig",
    "config_from_dict",
    "grid",
    "load_config_json",
    "resolve_gpu",
    "run_estimate",
    "summary_line",
]
