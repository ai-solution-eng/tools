# DeepSeek-V4.1-Flash on G2 — the exact Mooncake client change

_The live predictor is `deepseek-v41-flash-offload` in `project-user-andrew-bydlon` (SGLang v0.5.23, TP4/EP4, 4x H200). This page is the complete, minimal diff to make it a Mooncake client. Nothing is removed._

**Two arguments added, seven environment variables added, nothing else changes.**

---

## 1. Environment variables

### Keep exactly as they are (12)

| Variable | Current value |
|---|---|
| `AIOLI_DISABLE_LOGGER` | `1` |
| `AIOLI_PROGRESS_DEADLINE` | `90000s` |
| `HF_HUB_OFFLINE` | `1` |
| `HUGGINGFACE_HUB_CACHE` | `/mnt/models/` |
| `OMP_NUM_THREADS` | `8` |
| `PYTORCH_CUDA_ALLOC_CONF` | `expandable_segments:True` |
| `PYTORCH_KERNEL_CACHE_PATH` | `/mnt/models/dsv41_torch_kernel_cache` |
| `SGLANG_DG_CACHE_DIR` | `/mnt/models/dsv41_deepgemm_compile/` |
| `SGLANG_DSV4_COMPRESS_STATE_DTYPE` | `bf16` |
| `SGLANG_ENABLE_DSV41_ENGRAM_HOST_TABLE` | `1` |
| `TRANSFORMERS_OFFLINE` | `1` |
| `TRITON_CACHE_DIR` | `/mnt/models/dsv41_triton_cache` |

### Add these seven

| Variable | Value | Why |
|---|---|---|
| `MOONCAKE_MASTER` | `mooncake-master.mooncake.svc.cluster.local:50051` | the store RPC endpoint (host:port, no scheme) |
| `MOONCAKE_TE_META_DATA_SERVER` | `http://mooncake-master.mooncake.svc.cluster.local:8080/metadata` | transfer-engine metadata; keep the `/metadata` path |
| `MOONCAKE_PROTOCOL` | `tcp` | must match `store.protocol` — the G2 store is `tcp`. A TCP client needs no device, `IPC_LOCK` or `/dev/infiniband`, and must not set `MOONCAKE_RDMA` (that is the RDMA opt-in the policy keys on) |
| `MOONCAKE_GLOBAL_SEGMENT_SIZE` | `0` | **pure client** — the store pods hold the memory, not this pod |
| `MOONCAKE_CHECK_SERVER` | `1` | block boot until the master answers `/get_all_segments` |
| `MOONCAKE_LOCAL_HOSTNAME` | `valueFrom: fieldRef: status.podIP` | **per-pod** transfer endpoint; a literal breaks peer dialing |
| `MOONCAKE_HOST_ID` | `valueFrom: fieldRef: spec.nodeName` | node identity for local-first placement |

```yaml
- name: MOONCAKE_MASTER
  value: "mooncake-master.mooncake.svc.cluster.local:50051"
- name: MOONCAKE_TE_META_DATA_SERVER
  value: "http://mooncake-master.mooncake.svc.cluster.local:8080/metadata"
- name: MOONCAKE_PROTOCOL
  value: "tcp"
- name: MOONCAKE_GLOBAL_SEGMENT_SIZE
  value: "0"
- name: MOONCAKE_CHECK_SERVER
  value: "1"
- name: MOONCAKE_LOCAL_HOSTNAME
  valueFrom:
    fieldRef:
      fieldPath: status.podIP
- name: MOONCAKE_HOST_ID
  valueFrom:
    fieldRef:
      fieldPath: spec.nodeName
```

---

## 2. Arguments

### Keep exactly as they are

```
sglang serve --model-path deepseek-ai/DeepSeek-V4.1-Flash-1017 --trust-remote-code \
  --served-model-name deepseek-v4.1-flash --tp-size 4 --ep-size 4 \
  --mem-fraction-static 0.8 --chunked-prefill-size 8192 --max-running-requests 128 \
  --cuda-graph-max-bs-decode 128 --speculative-algorithm DSPARK \
  --reasoning-parser deepseek-v41 --tool-call-parser deepseekv41 --enable-cache-report \
  --preferred-sampling-params {"temperature":1.0,"top_p":0.95} \
  --host 0.0.0.0 --port 8080 --enable-metrics --enable-mfu-metrics \
  --enable-request-time-stats-logging \
  --uvicorn-access-log-exclude-prefixes /metrics /health \
  --enable-hierarchical-cache --hicache-ratio 3 --hicache-io-backend kernel \
  --hicache-mem-layout page_first --hicache-write-policy write_through
```

The HiCache block already there is exactly what Mooncake needs: `page_first` is one of the two layouts the backend accepts, and `kernel` IO is what resolves to it. Nothing to change.

### Add exactly these two

```
  --hicache-storage-backend mooncake \
  --hicache-storage-prefetch-policy timeout
```

`timeout` bounds how long a prefill waits on a store hit before recomputing — the right default for a tier that is unproven on this model.

### The resulting tail

```
  ... --enable-hierarchical-cache --hicache-ratio 3 --hicache-io-backend kernel \
  --hicache-mem-layout page_first --hicache-write-policy write_through \
  --hicache-storage-backend mooncake \
  --hicache-storage-prefetch-policy timeout
```

### Do NOT add

| Flag | Why not |
|---|---|
| `--hicache-size` | rejected for DeepSeek-V4 (`ValueError`); `--hicache-ratio` is the only sizing knob |
| `--enable-unified-cache-external-linker` | mutually exclusive with `--enable-hierarchical-cache` and with `--hicache-storage-backend` |
| `--page-size 64` | page size 256 is a model invariant here |
| `--dcp-size > 1` | `--hicache-storage-backend` raises `NotImplementedError` under DCP |

---

## 3. How to apply it

**Catalog entry (durable):** add the two args to `arguments` and the seven env vars to `environment`. The catalog map holds literal strings, so the two `fieldRef` entries cannot go there — they need the patch below after deploy.

**ISVC patch (demo / immediate):** the JSON patch in [DEPLOYMENT.md §4.3](DEPLOYMENT.md) appends both flags and all seven env vars, and is idempotent. It rolls a new revision: the pod restarts and reloads the weights.

---

## 4. Verify

```bash
kubectl -n project-user-andrew-bydlon get pods -l serving.kserve.io/inferenceservice=deepseek-v41-flash-offload
kubectl -n project-user-andrew-bydlon logs -f <new-pod> | grep -iE "mooncake|hierarchical|TCP-only|HCA"
```

Both lines must appear — they are the only unambiguous "L3 is live" signal:

```
Mooncake store setup successfully.
Mooncake store warmup successfully.
```

A TCP client is also expected to log `TCP-only environment, memcpy enabled` — against a
TCP store that is the correct path, not a fallback. If the pool is later switched to RDMA,
the same client must log `Found 1 HCAs` / `Using RDMA transport` and **never**
`TCP-only`; `MC_FORCE_HCA=true` turns a silent fallback into a startup error.

Then prove bytes moved with the byte oracle, not the Mooncake counters (they are dead on this model — the v1 code path returns early on the V4 logical KV anchor):

```bash
curl -s localhost:8080/v1/chat/completions -H "content-type: application/json" -d '{
  "model": "deepseek-v4.1-flash",
  "messages": [{"role":"user","content":"hello"}],
  "max_tokens": 8, "stream": true,
  "stream_options": {"include_usage": true}' | tail -2
```

`usage.prompt_tokens_details.cached_tokens` moving on the second identical request is the signal. `--enable-cache-report` is already set, which is what populates that field.

---

## 5. Rollback

Remove the two args and the seven env vars, or point the ISVC back at the catalog entry. The store keeps running; the predictor simply stops writing to it.

---

## 6. One unrelated thing this change exposed

`PYTORCH_KERNEL_CACHE_PATH`, `SGLANG_DG_CACHE_DIR` and `TRITON_CACHE_DIR` all live under `/mnt/models` — the `models-pvc` volume that is currently **88% full** (4.3T of 4.9T). If it fills, JIT kernel caches cannot be written and the next boot fails compiling, which looks like a serving bug rather than a disk problem. Mooncake does not touch that volume (it gets its own PVC), but the margin is worth reclaiming before the next model lands.
