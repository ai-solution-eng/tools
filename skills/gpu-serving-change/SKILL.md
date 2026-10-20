---
name: gpu-serving-change
description: Use when changing GPU serving config (mem-fraction, KV cache dtype, speculative decoding, attention/MoE backend) or diagnosing an OOM, failed boot, or serving regression on a self-hosted GPU inference deployment. Do NOT use for generic pod/alert triage — that is the cluster-triage skill.
---

# GPU serving change

Applies to **any** model on any GPU fleet; the reference deployment's values (H200 fleet) are fenced at the end.

## THE KERNEL RULE — read before touching any backend/dtype flag

**dtype/backend constraints follow the KERNEL, not the GPU.** A recipe validated on one accelerator
family is not evidence for another.

| Backend | Kernel family / silicon | On Hopper (SM90, e.g. H200) |
|---|---|---|
| `fa4` | **Blackwell SM100/SM12x** | **WRONG — do not use** |
| `fa3` | Hopper | **use this** (SGLang does end-to-end FP8 attention on SM90 with fa3) |
| `triton` | fallback | if `fa3` rejects the draft pattern; `flashinfer` after that |

A model card that said **`fa4`** was a Blackwell recipe (same trap as the GB300-only `deep_gemm`
recipe); on Hopper the value is `--speculative-draft-attention-backend fa3`. Before pasting any
backend/dtype value from a card, ask which kernel family validated it.

## Serving is arithmetic, not vibes

- Every image is **digest-pinned**; every boot has a recorded **memory ledger**; **every OOM is root-caused** — "it
  OOM'd" is not a finding, and a retry without a cause is a coin flip.
- **A negative mem-usage line VOIDS the datapoint.** `Load weight end. mem usage=-8.91` means the planner sized pools
  against a transient drain window from a dying predecessor — the boot is poisoned and its KV-pool numbers are
  meaningless. Never cite them, or treat that boot as evidence for or against the config under test. Clean-boot tells
  (reference model, 4×H200): `Load weight begin. avail mem=137.96` **and** `Load weight end. mem usage=+72.61`.
- **"Ready-state free is a snapshot, not a floor — keep ≥5 GB."** The allocator reserves for every new shape
  production throws (batch sizes, spec widths, prefill chunks); DeepGEMM workspaces are per-shape and fragmentation
  accumulates. A boot reporting "13.51 GB available at ready" can sit at ~2 GiB/rank under load — headroom you saw
  once is not headroom you have.
- **Graph memory scales super-linearly.** Verify/draft graphs for the sparse bs tail (e.g. 49→128) are large forwards:
  treat growth as ~linear in the **SUM of captured batch sizes**, not the count of sizes — an estimate built from "how
  many sizes" is optimistic by construction.
- **Measure before deciding.** Benchmarks drive the calls (5.2-vs-5.3 tallies, TTFT recalibration, the SPEC=OFF rule)
  — and a measurement tool was wrong once: its KV model was corrected only after the operator challenged it. If a
  measurement contradicts your model, suspect the model. Reference derivation: mamba-slot concurrency 94 → 96 slots
  via mem-fraction 0.865.

## mem-fraction is a RESERVE, not a usage target

On a hybrid-pool planner: `leftover_after_pools ≈ (1 − mem_fraction) × 139.81 GB`
(`available_bytes = fraction×TOTAL − static_usage`). The intuition is inverted — **lowering**
mem-fraction *frees* boot headroom for graphs and autotune transients.

| mem-fraction / cap (reference model, 4×H200) | Outcome |
|---|---|
| 0.85 / 256 | died in warmup autotune (transient gathers 3–6 GiB) |
| 0.80 / 128 | died in decode capture (wanted 2.81, 0.68 free; 18.34 GiB in graph pools) |
| 0.75 / 128 | died in decode capture (5.63 wanted / 4.64 free) |
| 0.70 / 128 | **Ready**, 17.5 GB/GPU headroom, KV full pool 8.74M tokens |

Boot ledger to budget against: weights+engram+draft 74.7 GB/rank; eager warmup+tuners ≈11.2; verify
graph pools 18.34 GiB (deterministic); decode graph pools ≈6–12; boot runtime ≈36–42 GB.

## Worked example — a model-specific config (any model; this is the reference instantiation)

- **BF16 KV is forced, not a compromise.** SGLang's `--kv-cache-dtype` is **global across the target's hybrid
  layers**, and the **11 TileLang-DSA layers** force the whole pool to bf16. Cheap anyway: 34/45 text layers are KDA
  linear-attention (no full KV), and the 11 DSA layers are MLA-compressed (`kv_lora_rank 512`) + topk-2048 sparse.
  `(tilelang-DSA, fp8-KV)` is **invalid on H200**; `(fa3, fp8-KV)` is valid, and the TRT-LLM DSA FP8-KV path (~1.8×
  KV) is Blackwell-only.
- **Draft KV fp8 is optional sugar.** The draft has no DSA layers, so `--speculative-draft-kv-cache-dtype fp8_e4m3` is
  only a ~10 KiB/token pool-halving nicety — **drop it at first friction**. Flags: `--speculative-algorithm DFLASH`,
  `--speculative-draft-model-path <repo-id>`, `--speculative-dflash-block-size 8` (alias of
  `--speculative-num-draft-tokens`).
- **The draft path must be the REPO-ID form** — with a hub cache + `HF_HUB_OFFLINE=1` the hub-cache layout resolves it
  offline; a literal path under the cache root does **not** exist in that layout.
- **mem-fraction 0.83 for the first draft boot** (draft ~2–3 GB + 8-token verify graphs); the base H200 recipe in the
  serve script ships `--mem-fraction-static 0.82` — step, don't leap.
- **The draft architecture may need a CUSTOM SGLang build.** The pinned image carried model support (PR
  #36507) but **not** the hidden-state capture the draft needs (PR #36708); until you build from the
  support branch, use the stock image's built-in **EAGLE MTP 5/1/6**.
- **MoE runner must stay `triton`** on H100/H200 (keep `--speculative-moe-runner-backend triton` explicit).
  `deep_gemm` is a dead end: num_groups=289 needs ~28 GiB scratch, and the JIT activation path asserts
  `D // 8 >= num_experts` (512/8 = 64 < 289) at CUDA-graph capture. **Hard shape incompatibility — pre-compiling
  and mem-fraction tuning cannot fix it.** Do not pass `--disable-shared-experts-fusion` (breaks model loading).
- **MoE kernel config warning:** the boot log says `Using default MoE kernel config. Performance might be
  sub-optimal!` for exactly the E=288/289, N=512 H200 shapes — bake tuned Triton MoE configs (the tuned-config dir is
  an empty `.gitkeep` today; not baked yet).
- **Never add `--speculative-adaptive` at 4×H200 / mem-fraction 0.85.** The controller re-captures per-step draft
  graphs for `candidate_steps=[1,3,5,7]` and OOMs the ~14 GB dynamic arena at boot (`build_adaptive_runtime_state` →
  `dsa_backend.py:1878` `torch.zeros()`, 1.49 GiB wanted with ~600 MiB free on every rank). Fixed graphs capture fine;
  adaptive dies at "Built adaptive runtime state steps=1". The arena is ~9.3 GB across tiers {0,1,3,7}.

## Change procedure

1. **State the ledger first** — expected weights / pools / graphs / runtime, and the boot lines that confirm each. If
   you cannot, you are guessing.
2. **One variable per boot.** Pin the image digest; change mem-fraction OR a kernel flag OR a pool ratio — not
   several. **Drain-check before the new boot** (below).
3. **Read the boot ledger, not the "Ready"** — a negative mem-usage line voids the run; drain, re-boot. **Load-test
   the edge** at target concurrency, watch for `CUDACachingAllocator` warnings, record it.

## Hardware ops

- **Drain-check mandate (3 incidents).** After ANY pod OOM/crash on shared GPUs, do a DCGM/`nvidia-smi` drain check
  before the replacement boots — residue corrupts **both** scheduling and pool accounting. Teardown of a large pod can
  take 4–14 min while the serving controller (KServe) restarts it ~1 min after death → collision crashloops.
  Deterministic path: **scale to 0, confirm drain (~500 MB/GPU), then scale up.** Target only **your own** GPUs —
  nodes are shared; other tenants occupy the rest. A boot whose "avail mem" is far below clean (e.g. 74 GB vs 138 GB)
  is the teardown race: void the run; it is not a verdict on the config. The clean fix is a **drain-gate
  initContainer** looping `nvidia-smi --query-gpu=memory.used` until max < 5000 MiB (60 iterations, then exit 1 to
  fail loud) — no 503 window, unlike the `maxSurge: 0 / maxUnavailable: 1` KServe annotation (structural, costs a 503
  per redeploy, **unverified** whether the platform's serving path preserves it).
- **Stuck-PID escalation ladder** (node, root): (1) `kubectl delete pod` (no `--force`) → wait 30 s → `nvidia-smi`,
  expect ~200–400 MiB baseline; (2) stuck Terminating → `crictl ps -a | grep <pod>` → `crictl rm -f <id>`; only
  `--force --grace-period=0` past 2 min, and **always** follow with the nvidia-smi → kill → dmesg sequence; (3) orphan
  PID (PPID=1 or dead containerd-shim) → `kill -9`; (4) **used MiB with NO process listed = context stuck in the
  driver** → `dmesg -T | grep -iE 'nvrm|nvidia|xid'` — Xid 31 (context leaked) / 43 (GPU stopped, needs reset) / 79
  (fell off bus) / 119 (GSP RPC timeout); for 31/43 `sudo systemctl restart containerd` then re-check; (5) still
  pinned → stop kubelet+containerd → `sudo nvidia-smi -i <N> -c 3` per stuck GPU (reset needs zero processes) →
  restart both; last resort reboot. **`nvidia-smi` may not be in the k8s exec allowlist** — run it on the node or in a
  privileged debug pod.
- **XID ladder.** `DCGM_FI_DEV_XID_ERRORS` is a **"last error" GAUGE — a latch, not an active fault.** (1) Read the
  XID + `err_msg`; (2) check node uptime — a latch with no driver reset since is stale; (3) correlate the latch
  timestamp against pod-restart series to find the triggering generation; (4) **four GPUs of a TP4 group latching
  together is one process's fault propagating through the collective, not four silicon failures** — check row-remap
  and ECC uncorrectables (zero = hardware healthy); (5) `dcgmi diag -r 3` to certify the node; (6) clear the latch at
  a maintenance window (`nvidia-smi -r` needs those GPUs drained, or let the node reboot). **After the latch clears,
  any fresh XID is a live fault** — correlate with serving logs at once.

## K8s optimization roadmap (agreed order)

1. **Pin a clean NVLink quad** via `NVIDIA_VISIBLE_DEVICES` UUIDs + **pod anti-affinity vs other tenants** (the 3+1
   PCIe split came from co-scheduling two models on one 8-GPU node).
2. **Tuned Triton MoE configs** (kills the default-config warning; the tuned-config dir is still an empty `.gitkeep` —
   a live to-do, not baked).
3. **Pre-warm KDA prefill kernels before Ready** — postStart curl with a ~2–4k-token request; serving-time Triton
   compile stalls the first request ~5 s/rank.
4. **`minReplicas=1`** — the 9.5-min cold boot on scale-to-zero is a user-visible outage.
5. **Experiments** (one at a time): mem-fraction 0.87 text-only but 0.85 with video; chunked-prefill 16384;
   `--enable-mixed-chunk`; NVLS/multimem allreduce (log tell `multimem all-gather disabled`).

## Gotchas

- HiCache ratio scales **every** host pool (main KV + mamba + draft) — three pools on one ratio is what OOM-killed a
  pod at ratio 4. Read `Allocating hierarchical KV cache host pool: <n> tokens, <G> GB` and tune from that line. A
  pool running 99.97% full with only 2/450 requests restoring from host means the ratio buys insurance you are not yet
  collecting — check before paying.
- Deploying a serving change is a manifest change → `plan_apply` dry-run first, then apply bound to the plan sha (see
  **cluster-triage**); a stale image means **release-verify**.

## Launch-argument hygiene (PCAI / KServe)

**Never quote JSON-valued args.** PCAI's serving path passes `--preferred-sampling-params` (and any other
JSON-valued flag) through **unquoted** and **errors out if shell quotes are added**:

```bash
# CORRECT on PCAI
--preferred-sampling-params {"temperature":1.0,"top_p":0.95}

# WRONG on PCAI - the quotes reach the arg and it is rejected
--preferred-sampling-params '{"temperature":1.0,"top_p":0.95}'
```

This is the opposite of a bare shell invocation, where quoting is required. If you are writing a **shell
script** that is not PCAI-mediated, quote it there; if you are writing the **command pasted into the PCAI
values editor**, leave it bare. Do not "fix" an unquoted JSON arg in a PCAI command - it is already correct.
(Recurring operator correction: this has been flagged wrongly more than once.)

## Verify the resolved config, never the intended one

`/get_server_info` is the authority for what the engine actually runs (auto-resolved backends, pool sizes,
parsed sampling params). Two habits:

- **Backends auto-resolve; do not pass them.** `attention_backend` and `moe_runner_backend` are selected from
  model + hardware + shape. Both SGLang and vLLM docs warn that overriding them is the most common cause of a
  disappointing measurement. Confirm them in `/get_server_info` / the boot log instead of pinning them.
- **`preferred_sampling_params` is only a DEFAULT.** It fills in what the client omits; any request that sends
  `top_p`/`temperature` explicitly wins. Verify with the pod's `request.received` log line, which records the
  sampling params actually applied.

## Sampling / effort: match the source you are comparing against

Published benchmark numbers and "recommended serving" values are **not the same thing**, and the two can differ
between a base checkpoint and its delta update. Check the specific card you are serving:

- DeepSeek publishes `temperature=1.0, top_p=0.95` as the **evaluation** setting for V4.1-Flash; the
  **-1017** delta card lists `top_p: 1.0` as its **serving** recommendation but does not restate an eval setting.
  Reproducing scores needs 0.95; following the serving rec needs 1.0. State which you are doing.
- **Thinking mode defaults differ by engine.** SGLang V4.1 leaves thinking OFF unless asked; vLLM V4.1 turns
  thinking ON at effort 50 when both keys are unset. A small `max_tokens` then returns empty `content` with
  `finish_reason=length` - that is the budget spent on the trace, **not** a broken model.
- **Effort alias values are engine-specific.** SGLang/DeepSeek's encoder maps `low/high/max` -> 50/75/100; vLLM
  maps `low/high/xhigh/max` -> 25/50/75/100. A request tuned at `high` on one runs at a different budget on the
  other - pass an integer when parity matters. The numeric 1-100 budget is reachable only via
  `chat_template_kwargs`, not the top-level field.

## Cross-check against the native engine docs

Before finalizing any serve command, diff it against the vendor's own page for that engine and checkpoint -
SGLang cookbook (`docs/cookbook/autoregressive/DeepSeek/DeepSeek-V4_1.mdx` at the **matching release tag**, not
`main`) and vLLM recipes (`recipes.vllm.ai/deepseek-ai/DeepSeek-V4.1-Flash`). Recipes are hardware-specific:
the cookbook's H200 cell is 8xH200 TP8/EP8, so its `--cuda-graph-max-bs-decode 64` and TP/EP values do **not**
transfer to a 4xH200 TP4/EP4 box. Take the *rules*, not the literals.

## Reference instantiation (one deployment's values — re-derive locally)

- Fleet: 4×H200 nodes `scs04` / `scs05`; two models co-scheduled on an 8-GPU node. Models: `GLM-5.3-Flash` (served
  target) + its `DFlash2` draft; `DeepSeek-V4.1-Flash`.
- Serve script of record: `ModelDeploymentApproaches/glm-5.3-flash/glm53-flash-serve.sh`; pinned image
  `lmsysorg/sglang:glm-5.3-flash`; draft repo-id `incoai/GLM-5.3-Flash-DFlash2`; hub cache
  `HUGGINGFACE_HUB_CACHE=/mnt/models` + `HF_HUB_OFFLINE=1`; support branch `xinyuan/glm-5.3-flash-support`.
- Tuned-MoE-config dir `dflash-video/moe_configs/`; node ops runbook `glm-sglang-serving/gpu-operations.md`.