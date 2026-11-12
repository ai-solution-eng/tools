# Features

The full feature list for **mooncake** (chart ``mooncake` 0.2.4`) with the
reason each design decision was made. The README has the one-paragraph version;
[ARCHITECTURE.md](ARCHITECTURE.md) is component-level, [PRODUCTION.md](PRODUCTION.md) is
operational, and every procedure lives in [DEPLOYMENT.md](DEPLOYMENT.md). This file
explains *why*, so a reader with no prior contact with the package can judge it.

---

## The KV cache, and why it deserves a store

When an LLM answers a prompt, it computes per-token attention state — the keys and
values (the "KV") that every later token attends over. Recomputing that state for a
long prompt is the expensive part of a request (**prefill**), and it happens from
scratch every time the same prefix is served again: a shared system prompt, a document
the user references twice, a conversation continued after the process died.

SGLang keeps this state in a hierarchical cache — **HiCache** — with three tiers:

| Tier | Where it lives | Survives |
|---|---|---|
| L1 | GPU HBM | the process |
| L2 | host DRAM (`--hicache-ratio`) | the process |
| L3 | an external storage backend | **whatever tier you give it** |

Mooncake Store is an L3 backend: a pool of KV pages that lives *outside* the serving
pods. A prefix that replica A computed once becomes readable by replica B on another
node, and by a restarted replica, because L1/L2 die with the process while L3 does
not. It is a cache, not storage: when a page is evicted or expires the model simply
recomputes it — a miss is never an error.

**Why an L3 tier helps at all:**

- **Cross-replica sharing** — one prefill serves every replica of the model. Without
  L3, each replica pays the prefill cost for the same prefix independently.
- **Restart survival** — a rescheduled or restarted replica starts with L1/L2 empty
  but an L3 full.
- **Pooling** — cache capacity comes from a dedicated store deployment, not from what
  one serving node happens to have free; the store needs no GPU at all.

The alternative on this fleet — SGLang's file backend — is per-replica, node-local
ephemeral disk, with no eviction cap by default. Mooncake is the shared, PVC-backed
pool that replaces it. (The one exception: some SGLang forks refuse the mooncake
backend at boot — see [DEPLOYMENT.md](DEPLOYMENT.md) §6 per-model notes.)

---

## The pool: a control plane plus data pods

The store is a Deployment (`<release>-master`, one pod) plus a StatefulSet
(`<release>-store`, N pods). The design split is deliberate:

- **The master holds metadata only** — the object registry, the segment registry,
  eviction, client liveness. **No KV bytes ever flow through it**, so it is a
  50m-CPU pod; the data path is peer-to-peer between client and store pod.
- **Each store pod owns one DRAM segment and one PVC** — the actual pool.

[ARCHITECTURE.md](ARCHITECTURE.md) covers the data flow and failure behaviour; the
three control-plane ports are:

| Port (values key) | Purpose |
|---|---|
| 50051 (`master.rpcPort`) | control-plane RPC; clients register here |
| 8080 (`master.metadataPort`) | the embedded HTTP metadata server for the transfer engine |
| 9003 (`master.metricsPort`) | JSON metrics (`/metrics/summary`, `/get_all_segments`) that the dashboard and the client's boot check read |

**Eviction and liveness knobs** — these are what make a shared pool safe:

- `master.evictionHighWatermarkRatio` (default `0.95`) — eviction begins when the
  pool crosses this fraction of capacity. The pool fills by design; eviction is the
  normal exit.
- `master.clientTtlSeconds` (default `10`) — a client silent for this long is
  treated as dead and its segments unmounted. A pod that crashes without
  deregistering cannot pin pool memory for its whole lifetime.
- `master.allocationStrategy` (default `local_first`) — an object stays on the
  segment that wrote it. When a store pod dies, losing it invalidates only *its*
  share instead of scattering reads across the survivors.
- `store.localBufferSize` (default `4194304` bytes) — per-store-pod transfer
  scratch buffer, registered host memory. It must fit inside the pod's memory
  request alongside the segment: `segmentSize + local buffer + interpreter overhead`.

---

## The two sizing tiers: DRAM hot, PVC capacity

The pool is sized along **two axes that are not interchangeable**, and conflating
them is the single most common misconfiguration:

| | DRAM segment | PVC tier |
|---|---|---|
| Backed by | host memory inside the store pod | the storage class (G2: `gl4f-filesystem`, VAST CSI) |
| Speed | memory | storage latency |
| Survives pod restart | no | **yes** |
| Sized by | `store.segmentSize` (default `8gb`) | `store.persistence.size` (default `500Gi`) |
| Total | `replicaCount x segmentSize` | `replicaCount x persistence.size` |

- `segmentSize` is **not disk**: the store serves KV pages out of it at memory
  speed, and a pod with no segment has nowhere to put a page. A PVC cannot replace it.
- `store.persistence` is the **capacity tier**: one PVC per store pod, created from
  the StatefulSet volume claim template so it survives restarts and reschedules. The
  bulk of the pool lives here; it is what makes the pool bigger than the nodes' RAM.

Two sharp edges, documented because both have bitten:

1. **The PVC claim template is immutable.** After a StatefulSet exists you can
   change the *size* of existing claims (the storage class supports expand) but not
   `accessModes` or `storageClassName` — those require deleting the store PVCs
   **and** the StatefulSet with `--cascade=orphan` (see DEPLOYMENT.md §8).
2. **A PVC here is a quota, not a reservation.** The CSI class is thin-provisioned
   and shared by every PVC on it; there is no storage scheduler and overcommit is
   silent, ending in `ENOSPC` on whichever tenant writes last. ARCHITECTURE.md §4
   carries the measured G2 numbers and the sizing rules.

---

## Transport: one decision for the whole pool

The client-to-store transfer engine can run over **TCP** (the chart default) or
**RDMA**. This is a **whole-pool decision**, not a per-model one: transfers are
peer-to-peer, and a TCP client against an RDMA store (or the reverse) fails at
*transfer time*, after the model has already booted and started serving.

- **Profiles, not flags.** `helm/values-examples/values.transport-tcp.yaml` and
  `values.transport-rdma.yaml` are the two sanctioned configurations; the later
  `-f` wins, so the profile goes last on the command line.
- **TCP needs nothing**: no device, no `IPC_LOCK`, no `/dev/infiniband`. A TCP
  client failure is almost certainly configuration (master address, `MOONCAKE_PROTOCOL`,
  flags), which makes it a cheap bring-up.
- **RDMA buys throughput on the page copy only** — the capacity tier behind the store
  is itself network storage, so RDMA does not remove the tier's latency. It costs a
  device resource limit + `/dev/infiniband` on both sides, the `IPC_LOCK` capability
  (the store `mlock()`s its registered segment; without it `mlock: cannot allocate
  memory` kills boot), and it introduces the worst failure mode in the package:
- **The silent fallback.** When the transfer engine finds no HCA it falls back to
  TCP *silently* — the model serves, nothing alarms, and the cache is quietly never
  reused. Setting `MC_FORCE_HCA=true` on clients turns that fallback into a boot
  error. Any RDMA bring-up should set it; the RDMA profile documents the full
  prerequisite list (device plugin, node advertising, `ibv_devices` check).

PRODUCTION.md §1 carries the measured G2 state and the staged TCP→RDMA decision;
DEPLOYMENT.md §5.0 is the operator-facing version.

---

## Multi-model sharing and isolation

Several models share one store. Isolation is by **key prefix**: the SGLang mooncake
backend names each client's key partition after the model, so replicas of the same
model share a prefix and different models never collide. Per-model capping is
possible but manual — Mooncake has no per-client size cap; the levers are
`master.quotaBytes` (the pool-wide SSD-tier quota), the tenant namespace
(`MOONCAKE_TENANT`, which scopes quotas per tenant), and the per-model env
overrides in DEPLOYMENT.md §5.5. The design consequence to accept: one tenant can
fill the pool and evict another model's cache — that is a *cache miss*, never a
data loss, and it is the price of a thin shared pool.

---

## The read-only dashboard

The dashboard (`<release>-ui`, stock `python:3.14-slim`, source in a ConfigMap —
no image build, see PORTING.md §2.5) is the ops surface:

- **Discovery by convention, not registration.** It finds Mooncake clients by scanning
  pods for a `MOONCAKE_MASTER` env var, then scrapes each replica's own `/metrics`.
  There is nothing to configure per client and no client-side change beyond the env
  itself.
- **Why the Models panel shows the replicas' own cache state, not per-model store
  usage.** The master reports **aggregate** store counters (capacity, keys, clients,
  throughput) but no per-tenant split. The only honest per-model signal is what each
  replica reports about its *own* cache (`cached_tokens`, hit rate) — that is what
  the panel renders, per replica.
- **Refresh semantics.** `ui.refreshIntervalSeconds` (default `1800`) is the
  backend's re-read cadence; the page polls the cached snapshot every 30 s, so a
  30-minute-old "cache hit rate" is the worst case, not an error.
- **Reader RBAC is the feature's trust boundary.** The chart creates a read-only
  `Role` (and `RoleBinding`) in the release namespace and in each namespace under
  `ui.discovery.namespaces`: `get`/`list` on `pods` (discovery) and on
  `persistentvolumeclaims` (the only honest denominator for the disk tier — the
  master self-reports a pool figure that does not match the claims it sits on).
  `ui.rbac.create: false` disables creation (e.g. when an admin role already
  grants it); `ui.serviceAccount` names the account the Role binds.
- **SSO is a values toggle.** `ezua.authorizationPolicy.enabled: true` puts the
  PCAI oauth2-proxy gate in front of the dashboard — the store's stats then stop
  being world-readable inside the cluster domain.

---

## The metadata token (optional, fail-closed by construction)

The master's embedded HTTP metadata server **refuses to bind a non-loopback address
with no token** — the upstream project's stance is that the metadata endpoint is an
attack surface, not a convenience. In a trusted namespace the chart leaves
`master.metadata.allowInsecure: true` (the default) and binds anyway. If you want the
tokened path: create a Secret, point `master.metadata.existingSecret` at it
(`existingSecretKey` defaults to `metadata-token`), and the same token must reach
the **store pods and every client** via the `MC_METADATA_HTTP_TOKEN` env. The
failure mode of a half-configured token — master tokened, clients untokened — is a
client that registers but cannot authenticate transfers; DEPLOYMENT.md §2 lists the
verification. The dashboard does **not** use it; it reads the master over 9003.

---

## PCAI integration

- The chart is PCAI-ported: it carries the `ezua` block (gateway `istio-system/ezaf-gateway`,
  endpoint host, 30 s timeout), the `VirtualService`, the optional oauth2-proxy
  `AuthorizationPolicy`, and the `hpe-ezua/` labels that let the platform route
  and manage the objects. PCAI substitutes the domain on import.
- **No custom image.** The dashboard app is stdlib Python served by stock
  `python:3.14-slim` from a ConfigMap; master/store run the published
  `kvcacheai/mooncake:0.3.14-cuda12` image. Nothing to build, tag, or push.
- PORTING.md is the point-in-time record of what was changed from the upstream chart
  and why; deploy/README.md documents the Kyverno mutating policy that clients need.

---

## Availability design

- `store.pdb.enabled` (default true, `minAvailable: 2`) + soft pod anti-affinity
  (`store.podAntiAffinity`) spread store pods so a node drain keeps the pool serving.
- **The master is a single point of control, deliberately.** Clients keep serving on
  their own L1/L2 while it is down; no new objects are placed and reads are not
  routed. High-availability master (etcd-backed) is out of scope for this chart.
- **Uninstalling deletes the pool.** There is no backup/restore story by design —
  cache state is worth its read-back rate and nothing more. PRODUCTION.md §2 lists
  what the chart does *not* do (no auth on the RPC data path, no NetworkPolicy, the
  SSD tier is young).

---

## Theming

The dashboard ships the fleet's **Auto / Light / Dark** pattern: one CSS custom-property
set per mode, persisted per browser, defaulting to the OS preference — so the panel
matches the rest of the PCAI operator UI in all three. THEMING.md documents the pattern
and how to reuse it in other apps.
