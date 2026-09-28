
import sys, json
sys.path.insert(0, "src")
from model_benchmarker.memory_model.configs import config_from_dict, count_parameters, weight_bytes_estimate, _expert_param_share

d = json.load(open("src/model_benchmarker/webapp/hf_configs/deepseek-ai__DeepSeek-V4-Flash-0731.json"))
cfg = config_from_dict(d)
t = cfg.raw.get("text_config", cfg.raw)
params, _ = count_parameters(cfg)
share = _expert_param_share(cfg)
expert_params = params * share
dense_params = params - expert_params
print("params: %.1fB total | experts %.1fB (%.0f%%) | dense %.1fB" % (
    params/1e9, expert_params/1e9, share*100, dense_params/1e9))
print()
print("checkpoint (HF, fp8 quant_method): everything fp8 = 271 GiB")
print("marlin/mxfp4 runner: experts at 4-bit = 0.5 B/param:")
print("  experts %.1f GiB + dense fp8 %.1f GiB = %.1f GiB" % (
    expert_params*0.5/1024**3, dense_params*1/1024**3,
    (expert_params*0.5 + dense_params)/1024**3))
print()
print("USER RECOLLECTION: 170 GiB — between 142 and 271.")
print("Candidate: fp8 BLOCK-QUANT scale overhead on the fp8 checkpoint:")
print("  weight_block_size 128 + ue8m0 scales add ~6-12%% to raw fp8:")
for pct in (0.06, 0.09, 0.12):
    print("  271 GiB x %.0f%% = %.0f GiB" % (pct*100, 271*(1+pct)))
print("Candidate: 4-bit experts + fp8 dense + bf16 embed/lm_head/head parts:")
embed = cfg.vocab_size * cfg.hidden_size
print("  vocab embed @ fp8: %.2f GiB; @ bf16: %.2f GiB" % (embed/1024**3, embed*2/1024**3))
