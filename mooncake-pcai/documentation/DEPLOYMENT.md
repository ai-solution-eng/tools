# Deploying mooncake on PCAI

Two parts: (1) install the store, (2) point model deployments at it. Part 2 is the one
people get wrong, because a serving pod needs **per-pod identity** that a static values
file cannot express — see §4.3.

---

## 1. Prerequisites

- A PCAI unit with the ezua gateway (`istio-system/ezaf-gateway`).
- `helm` 3.x on the machine you deploy from (or use the PCAI import flow, §2.2).
- Model deployments on **SGLang v0.5.22 or newer** whose image ships the `mooncake`
  python module. Check once per image:

  ```bash
  python3 -c "import mooncake; print(mooncake.__version__)"
  ```

- No GPU is required for the store itself. The dashboard is a 50m-CPU pod.

---

## 2. Install

### 2.1 Helm (direct)

```bash
helm install mooncake ./helm \
  --namespace mooncake --create-namespace \
  --set ezua.virtualService.endpoint=mooncake.${DOMAIN_NAME} \
  --set store.persistence.storageClassName=gl4f-filesystem   # or your X10000 class
```

Useful overrides:

```bash
--set store.replicaCount=5 \
--set store.persistence.size=2Ti \
--set store.persistence.storageClassName=gl4f-filesystem \
--set store.segmentSize=32gb \
--set ui.refreshIntervalSeconds=900 \
--set ezua.authorizationPolicy.enabled=true
```

The transport is a whole-pool decision, not a per-model one — see §4.0. `--set store.protocol=tcp`
is the low-risk start and the chart default; `rdma` needs the device mounted on the store pods
plus the client plumbing in §4.3.

Or use the G2 overlay plus a transport profile from `helm/values-examples/`:
`-f helm/values-examples/values.g2.yaml -f helm/values-examples/values.transport-tcp.yaml`
(§4.0).

### 2.2 PCAI import (EzAppConfig)

Package the chart and import it through **AI Essentials → Tools & Frameworks → Import
Framework** (or apply the generated `EzAppConfig` CR). The chart is already PCAI-ported:
it carries the `ezua` block, the VirtualService and the `hpe-ezua` labels.

```bash
helm package ./helm                   # -> ./mooncake-<version>.tgz (repo root)
```

---

## 3. Verify the store

```bash
kubectl -n mooncake get pods
kubectl -n mooncake logs deploy/mooncake-master | tail -20
```

You want the master log to show its storage line, e.g.:

```
Master Metrics: Storage: 0 B / 150.00 GB (0.0%) | Keys: 0 (soft-pinned: 0) | Clients: 0
```

The store pods registering shows up as `Clients: 3` once they connect. Check the pool
endpoint directly:

```bash
kubectl -n mooncake port-forward svc/mooncake-master 9003:9003 &
curl -s localhost:9003/metrics/summary
curl -s localhost:9003/get_all_segments | head -c 400
```

### 3.1 The PVC tier (the capacity tier)

Each store pod owns one PVC, created from the StatefulSet volume claim template, so it
survives pod restarts and reschedules:

```bash
kubectl -n mooncake get pvc                      # one per store pod: ssd-mooncake-store-N
kubectl -n mooncake get sts mooncake-store \
  -o jsonpath='{.spec.volumeClaimTemplates[0].spec.resources.requests.storage}{"\n"}'
```

The SSD tier needs two things and will silently do nothing without either:

1. **`master.enableOffload: true`** — the master must accept disk segments. Without it the
   store pods log `MountLocalDiskSegment failed, error code is UNABLE_OFFLOAD` and the PVC
   stays empty.
2. **`store.persistence.enabled: true`** — the pods then start with
   `-Denable_ssd_offload=true -Dssd_offload_path=<mountPath>`. Those `-D` overrides are the
   only supported way to set them; the SGLang-style `MOONCAKE_*` env names do nothing for
   this service.

By default the backend claims 90% of the mounted volume (`Bucket backend max_total_size not
set; using 90% of disk capacity`), so a 350Gi claim yields ~315 GiB of usable tier.

The claim template is **immutable**: changing `store.persistence.size` on an existing
release does not resize the claims. Either expand the PVCs in place
(`kubectl -n mooncake patch pvc ssd-mooncake-store-0 -p '{"spec":{"resources":{"requests":{"storage":"2Ti"}}}}'`
— only if the storage class allows expansion), or scale the StatefulSet out so new pods
get the new size, or reinstall.

Watch the two tiers on the master:

```
Storage: 12.34 GB / 150.00 GB (8.2%) | SSD Storage: 40.1 GB | Keys: 12345 | Clients: 3
```

`Storage` is the DRAM tier (sum of `segmentSize`), `SSD Storage` is what the pods have
offloaded to their PVCs. If `SSD Storage` stays at 0 while objects are being written, the
offload flags did not take effect — check that the store pod's args carry
`-Denable_ssd_offload=true` and `-Dssd_offload_path=<mountPath>`, and that
`store.persistence.mountPath` is writable in the pod.

---

## 4. Point a model deployment at it

A serving replica needs three things: the **client env**, two **serve flags**, and a
**per-pod hostname** (the pod IP) — plus a transport that matches the store.

> Worked example, with the live command and env vars: [CLIENT-DEEPSEEK-V4.1.md](CLIENT-DEEPSEEK-V4.1.md).

### 4.0 Choose the transport first: TCP or RDMA

The transport belongs to the **store** (`store.protocol`, set at install time) and
**every client must match it** — a TCP client cannot transfer to an RDMA segment, and a
mixed pool does not work. So it is one decision for the whole pool, not a per-model
choice.

| | **TCP** | **RDMA** |
|---|---|---|
| Store | `store.protocol: tcp` (chart default, and the G2 site file) | `store.protocol: rdma` (the RDMA transport profile) |
| Client pod needs | the env and the flags | those **plus** a device limit, `IPC_LOCK` and `/dev/infiniband` |
| Who can enable a client | MLIS access alone, once the fieldRef gap is closed (§4.3) | MLIS **plus** a cluster admin for the policy in `deploy/`, or the script |
| Node requirement | none | the node must advertise `rdma/rdma_shared_device_a` |
| Cost | one memcpy per page: more CPU, less throughput | zero-copy |
| When it goes wrong | no pod-level failure mode: the memcpy path always works | boot failure (`Failed to register buffer`), or a silent TCP fallback that then cannot talk to an RDMA store |

**TCP is the default and the safe bring-up.** No device plugin, no added capability, no
`hostPath`, no cluster-admin policy, and nothing that can fail at boot. What it costs is CPU
and throughput on the transfer, not correctness. The chart defaults to `tcp` and the G2 site
file pins it.

**RDMA is a supported upgrade, gated on three things:** the store pods actually mounting
`/dev/infiniband` (`store.rdma.infinibandHostPath`), the client policy in `deploy/` verified
to fire (§4.3), and the pool proven on TCP first. The plugin on this unit is not
CDI-enabled, so the device nodes are not something to assume — the chart mounts
`/dev/infiniband` explicitly. Without a working device an RDMA store falls back to TCP while
every client is told RDMA — a mismatched pool that never alerts. The checks are in
[PRODUCTION.md](PRODUCTION.md) §1.

The transport is one overlay file from `helm/values-examples/` (`values.transport-tcp.yaml`
or `values.transport-rdma.yaml`), applied **after** the site file — later `-f` wins, so it
overrides a site file's inline `store.protocol`. Moving a running pool to RDMA is a
maintenance-window change, because it touches the store **and** every client
([PRODUCTION.md](PRODUCTION.md) §5).

### 4.1 The client env

```yaml
env:
  - name: MOONCAKE_MASTER
    value: "mooncake-master.<namespace>.svc.cluster.local:50051"
  - name: MOONCAKE_TE_META_DATA_SERVER
    value: "http://mooncake-master.<namespace>.svc.cluster.local:8080/metadata"
  - name: MOONCAKE_PROTOCOL
    value: "rdma"                # tcp | rdma — MUST equal the store setting
  - name: MOONCAKE_GLOBAL_SEGMENT_SIZE
    value: "0"                   # pure client: the store pods hold the memory
  - name: MOONCAKE_CHECK_SERVER
    value: "1"                   # block boot until the master answers
  - name: MOONCAKE_LOCAL_HOSTNAME
    valueFrom:
      fieldRef:
        fieldPath: status.podIP  # REQUIRED - see 4.3
  - name: MOONCAKE_HOST_ID
    valueFrom:
      fieldRef:
        fieldPath: spec.nodeName
```

If the store was installed with a metadata token (`master.metadata.existingSecret`), add
`MC_METADATA_HTTP_TOKEN` from the same Secret to **every** client and store pod.

### 4.2 The serve flags

Add to the existing `sglang serve` args (they sit under the HiCache tiers you already run):

```
--hicache-storage-backend mooncake
--hicache-storage-prefetch-policy timeout
```

Keep `--enable-hierarchical-cache`, `--hicache-ratio`, `--hicache-mem-layout page_first`
and `--hicache-io-backend kernel`. **Do not** add `--hicache-size` on DeepSeek-V4 (it is
rejected) and **do not** add `--enable-unified-cache-external-linker` (mutually exclusive
with HiCache).

#### Do you still need the host (L2) tier?

**Physically yes.** The storage backend registers the host pool buffers and moves pages
through them — GPU → host → store. There is no path that writes GPU memory straight to
Mooncake, so `--enable-hierarchical-cache` and a host pool stay.

**Its role can change.** `--hicache-host-memory-mode buffer_only` makes the host pool a
transfer buffer instead of a cache tier, so all reuse comes from the store. It requires a
storage backend (Mooncake qualifies) and is rejected with `--disaggregation-mode decode`.
It does **not** free the memory: the pool is still sized by `--hicache-ratio`, whose default
in this mode is 1.2 rather than 2.0.

What it changes is latency, not footprint: an L2 hit is DRAM, an L3 hit is network storage.
Keeping a modest L2 (ratio 2) is usually both the better demo and the better service; shrink
the ratio first if RAM is the pressure.


### 4.3 What a catalog entry cannot express — and where it has to come from

MLIS accepts an image, `arguments`, `environment`, `/dev/shm` size and one PVC. The gap
is small and well defined — but there is a second wall behind it.

| The pod needs | Catalog (MLIS) | Where it comes from |
|---|---|---|
| the two serve flags | yes — `arguments` | the catalog |
| the five static env vars | yes — `environment` | the catalog |
| `MOONCAKE_LOCAL_HOSTNAME` + `MOONCAKE_HOST_ID` | **no** | the Kyverno policy, at pod create |
| RDMA plumbing (device limit, `IPC_LOCK`, `/dev/infiniband`) | **no** | the Kyverno policy, at pod create |

**Knative is the reason for that second column.** A KServe Serverless predictor is a
Knative Revision, and Knative validates that revision template far more strictly than it
validates a Pod:

| Field | Knative |
|---|---|
| `hostPath` volume | never allowed |
| `env[].valueFrom.fieldRef` | gated by `kubernetes.podspec-fieldref` |
| `securityContext.capabilities` | gated by `kubernetes.podspec-securitycontext` |

Both feature gates are **closed** on this unit. Patching an ISVC with those fields does
not work: `validation.webhook.serving.knative.dev` rejects the update, the ISVC stops
reconciling, and the previous revision keeps serving while no new one is ever created.
The only injection point downstream of that validation is the Pod — which is what
[deploy/kyverno-mlis-mooncake-rdma.yaml](../deploy/README.md) mutates.

So the client config is split in two, and both halves are needed:

1. **The catalog entry** carries the two flags, the five literal env vars, and
   `MOONCAKE_RDMA=true` for an RDMA store. All of it is expressible there.
2. **The policy** adds the two `fieldRef` vars for any Mooncake client, and the device,
   `IPC_LOCK` and `/dev/infiniband` when `MOONCAKE_RDMA=true`.

`MOONCAKE_LOCAL_HOSTNAME` must be **each pod's own routable IP** — it is the transfer
endpoint the store dials. The catalog holds literal strings only, so it cannot carry
that; unset or fixed, every pod advertises the same address (or loopback) and transfer
fails. That is precisely what rule 1 of the policy is for.

If the ISVC is already deployed and you would rather not redeploy it from the catalog:

```bash
python3 scripts/patch-model-mooncake.py --isvc <isvc> --protocol rdma --dry-run
python3 scripts/patch-model-mooncake.py --isvc <isvc> --protocol rdma
```

It writes only the catalog-expressible part into the ISVC, sets `MOONCAKE_RDMA=true`, and
removes anything an earlier run left behind that Knative would reject. It also lowers the
outgoing revision's `minScale` to 0 and waits for its pod to terminate before applying,
so you never run two copies of the model.

The manual equivalent, if you would rather not run the script:

```bash
NS=<model-namespace>; ISVC=<isvc-name>; STORE_NS=mooncake; PROTOCOL=rdma   # or tcp
MASTER="mooncake-master.${STORE_NS}.svc.cluster.local"

kubectl -n "$NS" patch isvc "$ISVC" --type=json -p "[
  {"op":"add","path":"/spec/predictor/containers/0/args/-","value":"--hicache-storage-backend"},
  {"op":"add","path":"/spec/predictor/containers/0/args/-","value":"mooncake"},
  {"op":"add","path":"/spec/predictor/containers/0/args/-","value":"--hicache-storage-prefetch-policy"},
  {"op":"add","path":"/spec/predictor/containers/0/args/-","value":"timeout"},
  {"op":"add","path":"/spec/predictor/containers/0/env/-","value":{"name":"MOONCAKE_MASTER","value":"${MASTER}:50051"}},
  {"op":"add","path":"/spec/predictor/containers/0/env/-","value":{"name":"MOONCAKE_TE_META_DATA_SERVER","value":"http://${MASTER}:8080/metadata"}},
  {"op":"add","path":"/spec/predictor/containers/0/env/-","value":{"name":"MOONCAKE_PROTOCOL","value":"${PROTOCOL}"}},
  {"op":"add","path":"/spec/predictor/containers/0/env/-","value":{"name":"MOONCAKE_GLOBAL_SEGMENT_SIZE","value":"0"}},
  {"op":"add","path":"/spec/predictor/containers/0/env/-","value":{"name":"MOONCAKE_CHECK_SERVER","value":"1"}},
  {"op":"add","path":"/spec/predictor/containers/0/env/-","value":{"name":"MOONCAKE_RDMA","value":"true"}}
]
```

Note what is *not* in that patch: no `fieldRef`, no capability, no volume. Knative would
reject all three, and the policy adds them at the pod instead.

Either way this rolls a new revision: the pod restarts and reloads weights. Schedule it.

### 4.4 Verify the client

```bash
kubectl -n "$NS" logs <new-predictor-pod> -c kserve-container \
  | grep -iE "mooncake|hierarchical|rdma|memcpy"
```

`-c kserve-container` is required: these pods also carry the logger, the queue proxy and
the Istio sidecar, so an unqualified `kubectl logs` refuses with "a container name must
be specified".

The only unambiguous "L3 is live" pair is:

```
Mooncake store setup successfully.
Mooncake store warmup successfully.
```

Then confirm the transport the client actually chose matches the store:

- **RDMA store** — `Found 1 HCAs` and `Using RDMA transport`, and **not**
  `TCP-only environment, memcpy enabled`. A silent TCP fallback still serves traffic, so
  nothing alerts you; this line is the check.
- **TCP store** — `TCP-only environment, memcpy enabled` is expected and correct.

For RDMA, also confirm the policy actually fired. The env vars are in the ISVC, but the
device and the capability are not — they only exist if the pod was mutated:

```bash
kubectl -n "$NS" get pod <new-predictor-pod> -o jsonpath='{.spec.containers[0].securityContext}{"\n"}{.spec.containers[0].resources.limits}{"\n"}{.spec.volumes[*].name}{"\n"}'
```

You want `IPC_LOCK`, `rdma/rdma_shared_device_a`, and `infiniband` in the volume list. If
they are missing, rule 2 of the policy did not match and the client will fail at
`Failed to register buffer` rather than at boot.

Do **not** trust Mooncake's storage counters on DeepSeek-V4: they are fed only by the v1
code path, which returns early on that model's logical KV anchor. They stay at zero even
when L3 works. Use `--enable-cache-report` and `usage.prompt_tokens_details.cached_tokens`
instead — the dashboard's Models panel reads the same server.

---


---

## 4.5 Capping how much one model can take

The file backend had two per-process knobs — `SGLANG_HICACHE_FILE_BACKEND_MAX_SIZE` and
`..._MIN_FREE_SPACE` — because it wrote to a local directory the process owned. **Mooncake
has no per-client equivalent, and cannot have one**: a serving replica contributes no storage
(`MOONCAKE_GLOBAL_SEGMENT_SIZE=0`), so there is nothing local to cap. The data lives in the
store pods' segments, which are shared.

What exists, weakest to strongest:

| Mechanism | Scope | How |
|---|---|---|
| `master.quotaBytes` | **whole pool** | quota for the SSD tier in bytes; `0` = 90% of the mounted volume. The closest analogue of `MAX_SIZE`. |
| `master.evictionHighWatermarkRatio` | whole pool | when the DRAM tier starts evicting (default 0.95). |
| `MOONCAKE_TENANT_ID` per model + master `--enable_multi_tenants` | **per model** | the only real per-model cap: the master admits per-tenant memory against a quota connector (`--tenant_quota_connector_type file\|etcd`). Requires a quota file, and SGLang raises if the installed Mooncake lacks tenant support. |
| one store per model | **per model** | absolute isolation: its own namespace, master, store pods and quota. Blunt but it is what I would use for anything customer-facing. |

So: "max size" has a pool-level equivalent (`quotaBytes`); "min free space" is implicit in the
90% default (the backend reserves the rest). A per-model cap needs either tenants or separate
stores.

If you want tenants, the store side needs:

```yaml
master:
  args: ["--enable_multi_tenants=true", "--tenant_quota_connector_type=file",
         "--tenant_quota_connector_file=/etc/mooncake/quotas.json"]
```

and each model adds `MOONCAKE_TENANT_ID=<model>` to its env. `scratch/add_mooncake_seed_entries.py
--tenants` writes those env vars into the catalog entries.

## 5. Per-model notes on this fleet

| Model | Status |
|---|---|
| **DeepSeek-V4.1-Flash** | supported: purpose-built pool map, rank-replicated pool so the rank-0 single writer is correct. Unproven end-to-end — soak before trusting. |
| **GLM-5.3-Flash** | blocked today: it needs the v2 fork image for the DSA host-load-back fix, and that fork refuses the mooncake backend at boot. Stock v0.5.22 **and v0.5.23** boot it but carry the #38031 corruption class, which is still open — its fix PR was closed unmerged, and the DSA fixes that did land in 0.5.23 are speculative-decode path only. Its **file** L3 tier is the safe one until the fork learns mooncake. |
| **Qwen3.8-27B** | at TP1 its GDN state is unsharded, so both file and mooncake are structurally safe; verify the module exists in its dev image. |

---

## 6. Several models, one store

Point as many deployments at the master as you like. Isolation is by key prefix
(`<backend-tag>_<model-name>`), so:

- same model + same parallelism → **shares** cache across replicas (the feature);
- different models → **never collide**, and never share KV (tokenizers differ);
- same model at different parallelism/quantization → same prefix: partition with
  `--hicache-storage-backend-extra-config '{"extra_backend_tag":"tp4"}'`.

They do compete for one pool: eviction is global, and the master is one SPOF for all of
them. One store per model family is the cleaner operational boundary.

---

## 7. Upgrade and uninstall

```bash
helm upgrade mooncake ./helm -n mooncake
helm uninstall mooncake -n mooncake          # the pool contents go with it
```

Upgrading the **store** restarts pods and loses their shares (cache-miss semantics).
Upgrading a **client** costs nothing to the pool.

---

## 8. Troubleshooting

| Symptom | Cause |
|---|---|
| `Invalid HA backend entry: http://...` | `MOONCAKE_MASTER` carries a URL scheme. It must be plain `host:port`; only `MOONCAKE_TE_META_DATA_SERVER` takes `http://.../metadata` |
| `MountLocalDiskSegment failed, error code is UNABLE_OFFLOAD` | the master was started without `--enable_offload=true` (`master.enableOffload`) |
| `Storage root directory is not set. persisting data is disabled.` | the store pod has no `-Dssd_offload_path`; the SSD tier is off |
| `Failed to setup Mooncake store, error code: N` | wrong master address / protocol / device |
| `Failed to register buffer, error code: N` | transport problem — often `Requested address ... not found` when RDMA is selected without `/dev/infiniband` |
| `Launch mooncake store server timeout` | the client cannot reach `:9003/get_all_segments` (DNS, NetworkPolicy, master down) |
| `Warmup put failed after 10 attempts` | store reachable but unusable — transport again |
| no `Mooncake Configuration loaded ...` line | `--hicache-storage-backend mooncake` never reached the scheduler |
| dashboard Models panel empty | the client pods have no `MOONCAKE_MASTER` env, or the UI Role lacks `list pods` in that namespace |

A pod with no `/dev/infiniband` cannot join an RDMA store, and setting the client to
`MOONCAKE_PROTOCOL=tcp` does not fix that — it only works if the store is tcp too. Give
the pod the device (`scripts/patch-model-mooncake.py --protocol rdma` adds it) instead of
downgrading the client.
