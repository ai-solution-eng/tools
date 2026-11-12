# Deploying mooncake on PCAI

Two parts: (1) install the store, (2) point model deployments at it. Part 2 is the one
people get wrong, because a serving pod needs **per-pod identity** that a static values
file cannot express — see §5.3.

---

## 1. The deployment contract

**Deployment is a values problem.** The chart is packaged once
(`./automation.sh release` → `mooncake-0.2.4.tar.gz`), imported into PCAI once, and the
operator's only surface after that is the chart's values in the PCAI **Helm Values**
editor. No template edits, no post-render patches, no `helm upgrade` on PCAI: a new
chart *version* means importing the new package, a new *configuration* means editing
values.

What PCAI does for you (the chart is already PCAI-ported — [helm/PORTING.md](../helm/PORTING.md)):

- substitutes the domain into `ezua.virtualService.endpoint` before rendering;
- creates the application in the namespace you choose;
- the chart carries the `ezua` block (gateway `istio-system/ezaf-gateway`), the
  VirtualService, the optional oauth2-proxy AuthorizationPolicy, and the `hpe-ezua/`
  labels the platform uses to route and manage the objects.

Everything that cannot be a value is an explicit prerequisite, and on this chart that
list is short:

- **No Secret is required for the default TCP deployment.** The only Secret the chart
  can consume is the optional master metadata token (§2.A).
- **Cluster objects** — a storage class for the PVC tier, and the Kyverno client
  policy (§5); RDMA adds the device plugin. All in §2.B.
- **One external dependency** — the SGLang model deployments that will become clients
  (§2.C). They are *consumers* of this deployment, not preconditions: the store comes
  up fine with zero clients.

Two things are deliberately **not** values and stay out-of-band:

1. **Pointing a model at the store** (§5) needs per-pod podSpec fields (a
   `fieldRef` hostname, a node label, and for RDMA a device limit + capability) that
   a static values file cannot express. That is a per-model patch, documented as a
   procedure with its own verification — never "just set it in values".
2. **Transport is one pool-wide decision** (§5.0): the store's protocol and every
   client's `MOONCAKE_PROTOCOL` must agree; a mismatch surfaces only at transfer
   time.

**Upgrade path:** re-import the new chart version with the same values; the pool keeps
serving and clients refill after. **Uninstalling deletes the pool, PVCs included** —
there is no backup/restore by design (cache state is worth its read-back rate).

---

## 2. Prerequisites

### 2.A Secrets the operator creates

| Secret | Keys | Consumer | Purpose | Failure mode | Create with |
|---|---|---|---|---|---|
| *(none for the default TCP deployment)* | — | — | — | — | — |
| master metadata token — **optional** | `metadata-token` by default (`master.metadata.existingSecretKey`) | master (binds the metadata server), the store pods, and every model client (`MC_METADATA_HTTP_TOKEN`) | authenticates the transfer-engine metadata server; the embedded server refuses to bind a non-loopback address without a token | set on the master but missing on clients → clients register but transfers fail; `allowInsecure: false` with no Secret → the master fails to start | `kubectl -n <ns> create secret generic <name> --from-literal=metadata-token=<token>`, then `master.metadata.existingSecret: <name>` in values + the env on clients. No template file ships with the chart; the name is yours to choose. |

### 2.B Patches and cluster objects

| Object | Why it is needed | What breaks without it | Verify |
|---|---|---|---|
| A storage class (G2: `gl4f-filesystem`, VAST CSI) | the PVC capacity tier (§4.3) | store PVCs stay `Pending`, pods stuck in `ContainerCreating` | `kubectl get sc`; PVCs `Bound` after install |
| `istio-system/ezaf-gateway` (platform-managed) | the dashboard URL | dashboard unreachable, 404 at the gateway | `kubectl -n istio-system get gateway` |
| `deploy/kyverno-mlis-mooncake-rdma.yaml` — a Kyverno **ClusterPolicy** (mutating) | the only path to the per-pod identity fields every client recipe uses (a `fieldRef` hostname, a node label) | clients boot, but peer transfers fail at dial time — the classic "model is up, cache never works" | server-side dry run per [deploy/README.md](../deploy/README.md) |
| (RDMA only) the `k8s-rdma-shared-dev-plugin` DaemonSet + nodes advertising `rdma/rdma_shared_device_a` + `/dev/infiniband` on the nodes | the device the store and clients register against | pod `Pending` on the resource limit, or — worse — a **silent** TCP fallback: the model serves, nothing alarms, and the cache is quietly never used | `ibv_devices` inside a store pod; set `MC_FORCE_HCA=true` on clients so a fallback becomes a boot error |
| (clients only) the Knative feature gates `kubernetes.podspec-fieldref` and `kubernetes.podspec-securitycontext` **closed** | precondition for the Kyverno policy being the *only* identity path on this platform | — | `kubectl -n knative-serving get cm config-knative-serving` (G2: both closed) |

### 2.C External dependencies

Model deployments on **SGLang v0.5.22 or newer** whose image ships the `mooncake`
python module — the client code is in the serving image, not in this chart. Check once
per image, from inside the cluster:

```bash
python3 -c "import mooncake; print(mooncake.__version__)"
```

They must be able to reach `mooncake-master.<ns>.svc.cluster.local` (50051/8080/9003).
The client sets `MOONCAKE_CHECK_SERVER=1`, so a model whose store is unreachable
refuses to boot — a deliberate fail-fast: it is cheaper to find an unreachable store
at model boot than to serve with a dead cache.

The store itself needs no GPU. The dashboard is a 50m-CPU pod.

---

## 3. Values

`helm/values.yaml` is the complete base (read its storage-model header — the DRAM vs
PVC distinction is the whole sizing story). Site files:
[`helm/local/values.g2.yaml`](../helm/local/values.g2.yaml) (G2 demo shape, 3 × 350Gi,
16 GB DRAM per pod, TCP) and
[`helm/values-examples/values.g2.yaml`](../helm/values-examples/values.g2.yaml)
(generic template, 3 × 1Ti) — plus a transport profile, last on the command line
([helm/values-examples/README.md](../helm/values-examples/README.md)).

**Must set per site:**

| Value | Default | Why |
|---|---|---|
| `ezua.virtualService.endpoint` | `mooncake.${DOMAIN_NAME}` | the dashboard host; must be unique on the shared gateway — two VirtualServices claiming one host split traffic |
| `store.persistence.storageClassName` | `gl4f-filesystem` | the class the cluster actually exposes; wrong name → PVCs `Pending` |
| `store.replicaCount` | 3 | pool replicas; also the PVC count |
| `store.persistence.size` | 500Gi | per-PVC **quota** (the class is thin — a quota, not a reservation) |
| `store.segmentSize` | 8gb | the DRAM hot tier per pod; `store.resources.requests.memory` must cover it + the local buffer + interpreter overhead |

**Optional, with defaults that are already right for most sites:**

| Value | Default | What it does |
|---|---|---|
| `store.protocol` | `tcp` | whole-pool transport; pick the RDMA profile instead of editing this by hand (§5.0) |
| `store.rdma.deviceResource` / `infinibandHostPath` | `rdma/rdma_shared_device_a` / `/dev/infiniband` | the device limit and the belt-and-braces hostPath, applied only when `protocol: rdma` |
| `master.enableOffload` | true | the master must accept disk segments; off → the PVC tier silently does nothing (`UNABLE_OFFLOAD`) |
| `master.quotaBytes` | 0 (= 90% of the mounted volume) | the SSD-tier quota, **pool-wide**; Mooncake has no per-client cap |
| `master.evictionHighWatermarkRatio` | 0.95 | eviction begins past this fraction of capacity |
| `master.clientTtlSeconds` | 10 | a silent client's segment is unmounted after this |
| `master.metadata.allowInsecure` | true | set `false` + `existingSecret` for the tokened metadata path (§2.A) |
| `ui.refreshIntervalSeconds` | 1800 | dashboard backend re-read cadence (the page polls a 30 s snapshot) |
| `ui.discovery.namespaces` | release namespace | where the dashboard looks for clients (`MOONCAKE_MASTER` pods) |
| `ui.rbac.create` | true | create the read-only Role (pods + PVCs) in the release + discovery namespaces |
| `ezua.authorizationPolicy.enabled` | false | the oauth2-proxy SSO gate in front of the dashboard |
| `store.pdb.enabled` / `store.pdb.minAvailable` | true / 2 | keep the pool serving through node drains |
| `store.podAntiAffinity` | true | soft spread of store pods across nodes |
| `imagePullSecrets` | [] | for a private registry |
| `nodeSelector` / `tolerations` / `affinity` / `podAnnotations` | — | standard placement pass-throughs, merged with the chart's own rules |

---

## 4. Import and install

### 4.1 Import (the PCAI path)

1. **Package** — `./automation.sh release` (or `helm package ./helm`) produces
   `mooncake-<version>.tgz` at the repo root.
2. **Import** — AI Essentials → **Tools & Frameworks → Import Framework**, upload the
   tarball. Alternatively apply the generated artifact:
   `kubectl apply -f ezapp/ezappconfig-mooncake.yaml` (it pins the chart and the
   values override; regenerate it with `python3 ezapp/make_ezappconfig.py` after any
   values change).
3. **Create the application** — PCAI asks for the target namespace and shows the
   **Helm Values** editor; paste the full values document (start from
   `helm/local/values.g2.yaml` + the TCP profile).
4. **Deploy** — PCAI resolves the domain, renders the templates, and creates the
   master Deployment, store StatefulSet (+PVCs), UI, the reader RBAC, and the
   VirtualService.

*(Developer path — not the PCAI one:)* `helm install mooncake ./helm -n mooncake
--create-namespace -f helm/local/values.g2.yaml
-f helm/values-examples/values.transport-tcp.yaml`. On a plain install you must
substitute the domain by hand in `ezua.virtualService.endpoint`; PCAI does it for you
on import.

### 4.2 Verify the store

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

Then open `https://mooncake.<your-domain>` — the dashboard should show pool capacity
and zero models (until you add a client in §5).

### 4.3 The PVC tier (the capacity tier)

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

The claim template is **immutable**: changing `store
.persistence.accessModes` or `storageClassName` after the StatefulSet exists fails
(`persistentvolumeclaimtemplates is immutable`). To change them you must delete the store
PVCs **and** the StatefulSet with `--cascade=orphan` (delete the StatefulSet *first* with
that flag so it does not delete the PVCs with it). Changing `store.persistence.size` is
safe in place — the class supports expand, so the existing claims grow.

---

## 5. Pointing a model deployment at the store

This part is a **per-model, per-deployment patch**, not a chart value — for the reason
in §5.3. It takes four inputs:

| Input | From | Example (this store, TCP) |
|---|---|---|
| master address | `store.service` + release namespace | `mooncake-master.mooncake.svc.cluster.local:50051` |
| metadata address | `master.metadataPort` | `http://mooncake-master.mooncake.svc.cluster.local:8080/metadata` |
| protocol | `store.protocol` | `tcp` or `rdma` — **must match the store** |
| model name | the InferenceService | `deepseek-v41-flash-offload` |

### 5.0 The transport decision (read this first)

**The transport is one decision for the whole pool, and every client must match the
store.** The store's `store.protocol` and each client's `MOONCAKE_PROTOCOL` are
independent settings that the transfer engine only reconciles at *transfer time*.
A client/store mismatch therefore does not fail boot: the model starts, serves, and
then fails on its first Mooncake transfer — `Failed to register buffer, error code: N`
or, for a protocol mismatch, `Failed to submit all transfers ... NotSupportedTransport`.
That is the "model is up but the cache never works" failure, and it is invisible
unless you check the logs.

- **TCP** is the chart default, the G2 install, and the low-risk start: no device, no
  `IPC_LOCK`, no `/dev/infiniband`, and no silent-fallback mode. A TCP failure is
  almost certainly configuration (master address, `MOONCAKE_PROTOCOL`, flags), which
  makes it cheap to debug.
- **RDMA** is the throughput upgrade (the G2 nodes advertise
  `rdma/rdma_shared_device_a: 240`). It needs the device **and** `/dev/infiniband` on
  both the store pods and the client pods, plus `IPC_LOCK` (the store `mlock()`s its
  registered segment; without it you get `mlock: cannot allocate memory` at boot).
  It also has a failure mode with no alert: **if no HCA is found the transfer engine
  falls back to TCP silently**, so set `MC_FORCE_HCA=true` on RDMA clients to make a
  fallback a loud boot error instead.
- **Switching transport is a maintenance-window change.** It touches the store
  *and* every client (repatch all of them with the new `--protocol`), and it is not a
  rolling operation.

The two profiles in `helm/values-examples/` encode exactly this:
`values.transport-tcp.yaml` (no device, no capability, no fallback) and
`values.transport-rdma.yaml` (device limit + `/dev/infiniband` + `IPC_LOCK`, with the
prerequisites listed in the file). Apply one, last on the command line — a later
`-f` wins.

### 5.1 The environment (per serving pod)

| Variable | Value | Why |
|---|---|---|
| `MOONCAKE_MASTER` | `<master svc>.<ns>.svc.cluster.local:50051` | the store endpoint, host:port, no scheme |
| `MOONCAKE_TE_META_DATA_SERVER` | `http://<master svc>.<ns>.svc.cluster.local:8080/metadata` | the transfer-engine metadata server — keep the `/metadata` path |
| `MOONCAKE_PROTOCOL` | `tcp` **or** `rdma` — must equal `store.protocol` | mismatch = silent failure at transfer time (§5.0) |
| `MOONCAKE_GLOBAL_SEGMENT_SIZE` | `0` | **pure client** — this pod contributes no segment; the store pods hold the memory |
| `MOONCAKE_CHECK_SERVER` | `1` | block boot until the master answers `/get_all_segments` |
| `MOONCAKE_LOCAL_HOSTNAME` | `valueFrom: fieldRef: status.podIP` | **per-pod** transfer endpoint; a literal breaks peer dialing |
| `MOONCAKE_HOST_ID` | `valueFrom: fieldRef: spec.nodeName` | node identity, used by `allocationStrategy: local_first` |

### 5.2 The flags (per InferenceService)

```
--hicache-storage-backend mooncake
--hicache-storage-prefetch-policy timeout
```

`timeout` bounds how long a prefill waits on a storage read before computing anyway —
the right default for a cache tier. The backend requires a page-first HiCache layout;
`--hicache-io-backend kernel` resolves to it (see
[CLIENT-DEEPSEEK-V4.1.md](CLIENT-DEEPSEEK-V4.1.md) for a complete, worked example
against the live G2 predictor, including the exact env var names in the fork).

### 5.3 Why a patch script and not values: the Knative catalog gap

The `MOONCAKE_LOCAL_HOSTNAME` and `MOONCAKE_HOST_ID` entries are **per-pod values**:
each replica must advertise *its own* IP and node. In the raw InferenceService spec
that is `valueFrom: fieldRef` — expressible, but the PCAI **catalog** (the model
deployment the user edits in the UI) only offers per-replica *static* env vars: no
`fieldRef`, no `valueFrom` of any kind.

That gap is structural on this platform, and it is why this section exists:

1. The catalog cannot express `fieldRef`. The two gates that would have been needed
   are **closed** on G2 (`kubernetes.podspec-fieldref`,
   `kubernetes.podspec-securitycontext` in `config-knative-serving`) — verified
   2026-11-05, and re-verify per unit.
2. So a per-pod value has exactly **one** path: a **Kyverno mutating policy** that
   injects it at admission. That policy is
   [`deploy/kyverno-mlis-mooncake-rdma.yaml`](../deploy/kyverno-mlis-mooncake-rdma.yaml)
   — it adds `MOONCAKE_LOCAL_HOSTNAME` (podIP `fieldRef`) and `MOONCAKE_HOST_ID`
   (nodeName `fieldRef`) to any pod in a namespace carrying the label
   `mooncake-client: "true"`, and for `rdma` clients also the device limit,
   `IPC_LOCK` and the `/dev/infiniband` mount. It is applied once per cluster
   (`kubectl apply -f deploy/kyverno-mlis-mooncake-rdma.yaml`), not per model.
   Verify it actually fires before blaming the model:
   ```bash
   kubectl create --dry-run=server -f <rendered-pod.yaml> -o yaml   # expect the fieldRefs
   ```
   If the pod renders **without** the fields, the policy is not firing (label,
   namespace, or policy absent) — the model will then fail with
   `Failed to register buffer, error code: N`.
3. A static `MOONCAKE_LOCAL_HOSTNAME` (one value shared by all replicas) would
   "work" for one replica and break every other peer dial — never use a literal.

### 5.4 Apply it

```bash
# 1. the cluster-wide policy, once
kubectl apply -f deploy/kyverno-mlis-mooncake-rdma.yaml

# 2. label the model's namespace
kubectl label namespace <model-ns> mooncake-client=true

# 3. patch the InferenceService (adds env + flags +, for rdma, the device plumbing)
python3 scripts/patch-model-mooncake.py --namespace <model-ns> \
  --name <inferenceservice> --protocol tcp \
  --master mooncake-master.mooncake.svc.cluster.local:50051
```

The script prints the `env`, `args` and (rdma) `resources.limits` / `volumeMounts`
blocks it applies, and refuses to run if the deployment already has a conflicting
`--hicache-storage-backend`.

### 5.5 Verify the client is actually using the store

```bash
# store side: Clients count climbs
kubectl -n mooncake port-forward svc/mooncake-master 9003:9003 &
curl -s localhost:9003/metrics/summary | python3 -m json.tool

# client side: the register line, then real transfers
kubectl -n <model-ns> logs deploy/<model>-predictor-<hash> | \
  grep -Ei "mooncake|hicache" | tail -30
```

You want to see the client's register log followed by put/get activity; the dashboard
Models panel then lists the replica with its cache hit rate.

### 5.6 Per-model capping (how to stop one tenant from filling the pool)

Mooncake has **no per-client size cap** — `quotaBytes` is pool-wide. A large model
can fill the pool and evict another model's cache (a miss, not a failure, but
noisy). The levers, in descending strength:

| Lever | Scope | How |
|---|---|---|
| `MOONCAKE_TENANT` (client env) | per tenant | different tenants get separate quota namespaces; set it per model so each has its own `quotaBytes` bucket |
| `master.quotaBytes` | per tenant/pool, bytes | the SSD-tier quota; 0 = 90% of the mounted volume |
| per-model `MOONCAKE_*` overrides | per client | e.g. a different prefetch policy or a smaller working set for a small model |

Practical default: give each *model* its own `MOONCAKE_TENANT` and size
`master.quotaBytes` to the biggest one.

---

## 6. Per-model notes (live fleet)

- **DeepSeek-V4.1-Flash-1017 (G2 predictor, SGLang v0.5.23, TP4/EP4)** — the reference
  client. Full diff in [CLIENT-DEEPSEEK-V4.1.md](CLIENT-DEEPSEEK-V4.1.md): two args,
  seven env vars, nothing removed; `page_first` layout + `kernel` IO + `timeout`
  prefetch policy; TCP.
- **GLM-5.3-Flash (the v2 fork image)** — keep the file backend. The v2 fork
  **refuses** the mooncake backend at boot (it carries the stock 0.5.22/0.5.23
  `#38031` corruption class), so for this model "mooncake for Qwen and DeepSeek" is
  the correct statement, never "never use file".
- **SGLang versions** — the `#38031` corruption fix and the mooncake backend both
  need v0.5.22+. On the G2 fork, confirm `import mooncake` in the image before
  patching.

## 7. Several models, one store

- **Isolation is by key prefix** (the model name), so models never see each other's
  KV — no config needed for coexistence.
- **They share one eviction pool.** A busy model evicts a quiet one's pages: that is a
  cache miss, never an error. If it hurts, give models separate `MOONCAKE_TENANT`
  values (§5.6).
- **The dashboard scans per namespace.** Add each model's namespace to
  `ui.discovery.namespaces` (and the chart creates the reader Role there), or the
  Models panel will not show it.
- **One transport for all of them** — a new model joining an existing pool must take
  the pool's `MOONCAKE_PROTOCOL`, not its own preference (§5.0).

---

## 8. Upgrade, rollback, uninstall

### 8.1 The PCAI path

- **Upgrade:** import the new chart version, keep your values, re-apply. The store
  rolls; the PVC tier survives (it is a quota, and the StatefulSet keeps its
  volumes). Clients see a refill window, not an outage.
- **Rollback:** point the application back at the previous imported version with the
  same values.
- **Uninstall:** deletes the pool **including the PVCs**. There is no backup/restore
  by design — a cache is worth its read-back rate, and resurrecting KV pages from a
  snapshot is not a supported operation.

### 8.2 Plain Helm (developer path — not the PCAI one)

```bash
helm upgrade mooncake ./helm -n mooncake -f helm/local/values.g2.yaml \
  -f helm/values-examples/values.transport-tcp.yaml
helm rollback mooncake -n mooncake
helm uninstall mooncake -n mooncake        # deletes the pool AND the PVCs
```

---

## 9. Troubleshooting

| Symptom (where) | Meaning |
|---|---|
| store pods stuck `ContainerCreating`, PVC `Pending` | `storageClassName` names a class the cluster does not expose (or cannot provision) — `kubectl get sc` |
| store pods crash with `mlock: cannot allocate memory` | `IPC_LOCK` missing (RDMA) — the policy only adds it for `--protocol rdma` |
| master: `MountLocalDiskSegment failed ... UNABLE_OFFLOAD` | `master.enableOffload` is false — the PVC tier is dead, silently |
| store PVC stays empty with no errors | `enableOffload` true but `store.persistence.enabled` false (or the `-D` flags absent) |
| client: `Failed to register buffer, error code: N` | wrong master address / protocol / device |
| client: `Failed to submit all transfers ... NotSupportedTransport` | **client/store protocol mismatch** — a TCP client against an RDMA store (or reverse) |
| model serves, cache never reused, no errors | RDMA selected but no HCA — the transfer engine **fell back to TCP silently**. Check for the fallback line, `ibv_devices` in the pod, set `MC_FORCE_HCA=true` on clients to make it a boot error |
| client: `Failed to register buffer, error code: N` with `Requested address ... not found` | RDMA selected without `/dev/infiniband` in the pod |
| client: `Launch mooncake store server timeout` | the client cannot reach `":9003/get_all_segments"` (DNS, NetworkPolicy, master down) |
| client: `Warmup put failed after 10 attempts` | store reachable but unusable — transport again |
| no `Mooncake Configuration loaded ...` line | `--hicache-storage-backend mooncake` never reached the scheduler |
| dashboard Models panel empty | the client pods have no `MOONCAKE_MASTER` env, or the UI Role lacks `list pods` in that namespace (add it to `ui.discovery.namespaces`) |

A pod with no `/dev/infiniband` cannot join an RDMA store, and setting the client to
`MOONCAKE_PROTOCOL=tcp` does not fix that — it only works if the store is tcp too. Give
the pod the device (`scripts/patch-model-mooncake.py --protocol rdma` adds it) instead of
downgrading the client.
