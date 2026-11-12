# Mooncake — a shared KV-cache store for PCAI

A PCAI (HPE Private Cloud AI / Ezmeral Unified Analytics) application that runs
**Mooncake Store** as a cluster-wide **KV-cache pool** for LLM serving, plus a
read-only dashboard that shows how the pool and each model deployment are doing.
Chart: `mooncake` 0.2.4 (image `kvcacheai/mooncake:0.3.14-cuda12`).

---

## 1. What it is

**A KV cache** is the attention state (keys and values) a large language model
computes while reading a prompt. Recomputing it — *prefill* — is the expensive part
of a request, and it is repeated from scratch whenever the same prompt prefix is
served again: a shared system prompt, a document the user references twice, a
conversation continued after the process restarted.

**Mooncake Store is where those KV pages live outside the model's own process.**
Model deployments (SGLang on this fleet) use it as a shared **L3 HiCache tier** —
the third level of SGLang's hierarchical cache, after L1 (GPU memory) and
L2 (host DRAM), both of which die with the process. With an L3 tier:

- a prefix computed by **one replica** is reused by **any other replica** of the
  same model, on any node, after a restart — one prefill serves the whole fleet;
- cache capacity comes from a **dedicated store deployment** (no GPU needed) instead
  of what one serving node happens to have free;
- it is a *cache*, not storage: an evicted page is a miss that gets recomputed,
  never an error.

**The capacity tier is a PVC**: each store pod owns one persistent volume claim
(StatefulSet volume claim template), so the pool is as large as the storage class
you point it at (on G2: `gl4f-filesystem`, VAST CSI) and it survives pod restarts
and reschedules. A small DRAM segment inside each pod is the hot tier in front of
it — the two are sized independently (see [documentation/FEATURES.md](documentation/FEATURES.md)).

```
                 ┌───────────────────────────────┐
                 │  mooncake-master (1 pod)      │  control plane only
                 │  50051 RPC / 8080 metadata    │  KV bytes never flow through it
                 │  9003 metrics                 │
                 └───────────────┬───────────────┘
                                 │
     ┌───────────────────────────┼───────────────────────────┐
     │                           │                           │
┌────▼──────────────┐   ┌────────▼───────────┐   ┌───────────▼────────┐
│ mooncake-store-0  │   │ mooncake-store-1   │   │ mooncake-store-2   │
│ DRAM 16 GB        │   │ DRAM 16 GB         │   │ DRAM 16 GB         │
│ PVC  350Gi        │   │ PVC  350Gi         │   │ PVC  350Gi         │
└────▲──────────────┘   └────────▲───────────┘   └───────────▲────────┘
     │      peer-to-peer KV transfer (RDMA or TCP)          │
┌────┴──────────────┐   ┌────────┴───────────┐   ┌───────────┴────────┐
│ DSV4.1 replica A  │   │ DSV4.1 replica B   │   │ GLM / Qwen ...     │
│ client-only       │   │ client-only        │   │ client-only        │
└───────────────────┘   └────────────────────┘   └────────────────────┘
```

(Numbers are the G2 demo footprint; yours will differ with your values.)

## 2. The problem it solves

- **Replicas recompute the same prefixes.** → One shared pool: replica B reads what
  replica A already wrote, isolated by key prefix so models never see each other's KV.
- **A restart or reschedule empties the cache.** → The PVC-backed tier outlives the
  pod; a restarted replica starts with its L3 full.
- **Per-replica file caches don't share and die with the pod** (and SGLang's file
  backend has no eviction cap by default). → A pooled, quota-capped store outside the
  serving pods.
- **Nobody can see whether the cache is helping.** → The dashboard: pool capacity,
  keys, clients, throughput, and each model replica's own cache hit rate.
- **Cache capacity is bounded by one node's RAM.** → Pooling across N store pods on
  network storage, with per-PVC quotas.

## 3. How it fits in PCAI

| Direction | What |
|---|---|
| Depends on | the PCAI gateway (`istio-system/ezaf-gateway`) for the dashboard URL; a storage class for the PVC tier (G2: `gl4f-filesystem`) |
| What depends on it | model deployments patched into **clients** — a per-model change, not a chart value ([DEPLOYMENT.md §5](documentation/DEPLOYMENT.md#5-pointing-a-model-deployment-at-the-store)) |
| What it does not do | no GPU work, no model serving, no data loss on evict — it is a cache tier |

The chart is PCAI-ported: `ezua` block, VirtualService, optional oauth2-proxy SSO
gate, `hpe-ezua/` labels — see [helm/PORTING.md](helm/PORTING.md).

## 4. Capabilities, by theme

One line each with the design rationale; depth in [documentation/FEATURES.md](documentation/FEATURES.md).

- **A shared pool any model deployment can join** — key-prefix isolation, tenant
  scoping, pool-wide SSD quota; eviction and client-TTL make a shared pool safe
  ([FEATURES: the pool](documentation/FEATURES.md#the-pool-a-control-plane-plus-data-pods)).
- **Two sizing tiers, not one** — DRAM hot tier (`segmentSize`) + PVC capacity tier
  (`persistence.size`); a PVC is a quota, not a reservation
  ([FEATURES: the two sizing tiers](documentation/FEATURES.md#the-two-sizing-tiers-dram-hot-pvc-capacity)).
- **Transport is one decision for the whole pool** — TCP default (no devices, no
  silent failure) or RDMA (throughput, plus the silent-fallback trap); shipped as
  profile files, chosen last on the command line
  ([FEATURES: transport](documentation/FEATURES.md#transport-one-decision-for-the-whole-pool)).
- **A read-only dashboard** — discovers clients by their env var, renders pool and
  per-replica cache state, reads PVCs for the honest disk denominator, ships reader
  RBAC, SSO-gateable
  ([FEATURES: the dashboard](documentation/FEATURES.md#the-read-only-dashboard)).
- **An optional metadata token** — the embedded metadata server's fail-closed
  default, wired as one Secret reaching master, stores and clients
  ([FEATURES: the metadata token](documentation/FEATURES.md#the-metadata-token-optional-fail-closed-by-construction)).
- **PCAI-native theming** — Auto/Light/Dark matching the operator UI
  ([THEMING.md](documentation/THEMING.md)).

## 5. Deploy on PCAI (one screen)

The PCAI path is: import the packaged chart once, then the only operator surface is
the values editor. Full procedure: [documentation/DEPLOYMENT.md](documentation/DEPLOYMENT.md).

1. **Prerequisites** — a storage class (G2: `gl4f-filesystem`); for RDMA only, the
   Kyverno client policy (`deploy/kyverno-mlis-mooncake-rdma.yaml`) and nodes with
   the RDMA device plugin. No Secrets are required for the TCP default.
2. **Import the chart** — AI Essentials → Tools & Frameworks → Import Framework,
   upload `mooncake-0.2.4.tar.gz` (rebuild with `./automation.sh`).
3. **Set the values** — the Helm Values editor; start from
   [`helm/local/values.g2.yaml`](helm/local/values.g2.yaml) (the G2 demo shape:
   3 × 350Gi, 16 GB DRAM per pod, TCP) or
   [`helm/values-examples/values.g2.yaml`](helm/values-examples/values.g2.yaml)
   (generic template, 3 × 1Ti), plus a transport profile.
4. **Apply** — PCAI creates the namespace, renders the templates, substitutes the
   domain, and installs.
5. **Verify** — pods and PVCs up; the master log shows its storage line; open
   `https://mooncake.<your-domain>` and the dashboard loads
   ([DEPLOYMENT.md §4](documentation/DEPLOYMENT.md#4-import-and-install)).

## 6. Prerequisites at a glance

**Secrets** (create with `kubectl create secret`; details in
[DEPLOYMENT.md §2](documentation/DEPLOYMENT.md#2-prerequisites)):

| Secret | Keys | Used by | Purpose | Failure mode |
|---|---|---|---|---|
| *(none required — TCP default)* | — | — | — | — |
| master metadata token (optional) | `metadata-token` (name set by `master.metadata.existingSecretKey`) | master + store pods + every client (`MC_METADATA_HTTP_TOKEN`) | authenticates the transfer-engine metadata server | half-configured token → clients register but cannot transfer; missing token with `allowInsecure: false` → master refuses to bind |

**Patches / cluster objects:**

| Object | Why it is needed | What breaks without it | Verify |
|---|---|---|---|
| A storage class (G2: `gl4f-filesystem`) | the PVC capacity tier | store PVCs stay `Pending`, pods stuck in `ContainerCreating` | `kubectl get sc`; PVCs `Bound` after install |
| `deploy/kyverno-mlis-mooncake-rdma.yaml` (mutating policy) | injects the per-pod identity fields the client recipe uses | client boots, transfer fails at dial time | server-side dry run — [deploy/README.md](deploy/README.md) |
| (RDMA only) NVIDIA RDMA shared-device plugin + nodes advertising `rdma/rdma_shared_device_a` + `/dev/infiniband` on the nodes | the device the store and clients register | pod `Pending` on the resource limit, or **silent** TCP fallback — serving with a cache that is never used | `ibv_devices` inside a store pod; set `MC_FORCE_HCA=true` on clients to make a fallback a boot error |
| (clients only) Knative feature gates `kubernetes.podspec-fieldref` and `kubernetes.podspec-securitycontext` **closed** | the Kyverno policy is the only path to per-pod podSpec identity on this platform | — | `kubectl -n knative-serving get cm config-knative-serving` (G2: closed) |

**External dependencies:** model deployments running **SGLang v0.5.22 or newer**
whose image ships the `mooncake` python module (check:
`python3 -c "import mooncake; print(mooncake.__version__)"` from inside the
cluster), reachable at `mooncake-master.<ns>.svc.cluster.local`.

## 7. Day-2

- **Upgrade** — import the new chart version, keep your values, re-apply. The pool
  keeps serving; clients miss until the pool refills.
- **Health** — the dashboard (capacity, keys, clients, hit rate);
  `kubectl -n <ns> logs deploy/mooncake-master | tail` (the storage line);
  `curl /metrics/summary` via port-forward on 9003.
- **Logs** — master (control plane), store pods (transfers), UI (scrapes).
- **Uninstall** — deletes the pool, including the PVCs. There is no backup/restore by
  design; cache state is worth its read-back rate.
- **Troubleshooting** — the failure-mode table in
  [DEPLOYMENT.md §9](documentation/DEPLOYMENT.md#9-troubleshooting);
  [PRODUCTION.md](documentation/PRODUCTION.md) for transport, availability, capacity
  and the known gaps.

## 8. Documentation map

| File | Answers |
|---|---|
| [README.md](README.md) | what it is, how to deploy it, where to go deeper |
| [documentation/FEATURES.md](documentation/FEATURES.md) | every capability and the reason it is designed that way |
| [documentation/ARCHITECTURE.md](documentation/ARCHITECTURE.md) | components, data flow, the two storage tiers, thin-pool physics |
| [documentation/DEPLOYMENT.md](documentation/DEPLOYMENT.md) | PCAI-way deployment, prerequisites, values, the client recipe, upgrades, troubleshooting |
| [documentation/PRODUCTION.md](documentation/PRODUCTION.md) | transport decisions, availability, capacity, known gaps |
| [documentation/THEMING.md](documentation/THEMING.md) | the Auto/Light/Dark pattern (reusable in other apps) |
| [documentation/CLIENT-DEEPSEEK-V4.1.md](documentation/CLIENT-DEEPSEEK-V4.1.md) | a worked, minimal client diff for the live G2 predictor |
| [helm/PORTING.md](helm/PORTING.md) | what was changed from the upstream chart, and why |
| [deploy/README.md](deploy/README.md) | the Kyverno client policy and how to verify it fires |
| [helm/values-examples/README.md](helm/values-examples/README.md) | the paste-ready site values and the transport profiles |
| [helm/local/README.md](helm/local/README.md) | where your real site values live (never packaged) |

## 9. Developing and releasing (not the deployment path)

`helm install` / `helm upgrade` / plain `kubectl apply` are the developer and
secondary paths — on PCAI the chart is imported and only its values are edited.

```bash
# developer install with the G2 demo shape + the TCP profile
helm install mooncake ./helm --namespace mooncake --create-namespace \
  -f helm/local/values.g2.yaml \
  -f helm/values-examples/values.transport-tcp.yaml
```

- **Releasing** — `./automation.sh release` rebuilds the root
  `mooncake-<chart version>.tar.gz`, bumps nothing manually (the version comes from
  `helm/Chart.yaml`), and prunes stale archives. The PCAI artifact lives in
  `ezapp/` (EzAppConfig + logo + values override).
- **Delivery mirror** — the repo is mirrored into
  `../pcai-solutions/tools/mooncake-pcai` by the shared hardlinker
  (`python3 hardlinker.py -c hardlink_config.json`, dry-run by default). The mirror
  is built from **hardlinks**: an edit in either tree is the same inode — never copy
  files between them, and never rewrite one with a temp-file-and-rename. It carries
  the chart, docs, the ezapp artifact and the packaged tarball; `helm/local/`,
  caches and stale archives are ignored. Each run is gated by
  `pcai_utils/secret_scan.sh`, and `bash ../pcai_utils/fleet_drift_check.sh` proves
  the shared files are still true hardlinks (this repo is in that gate's scan set).

## 10. Repository layout

```
MooncakePCAI/
├── README.md
├── helm/                       # the chart (packaged from here)
│   ├── Chart.yaml              # mooncake 0.2.4
│   ├── values.yaml             # base values; read the storage-model header
│   ├── PORTING.md              # what was changed for PCAI, and why
│   ├── files/app.py            # the dashboard app (stdlib Python, ConfigMap-mounted)
│   ├── templates/              # master, store StatefulSet, UI, RBAC, VirtualService...
│   ├── values-examples/        # site overlays: values.g2.yaml + transport profiles
│   └── local/                  # your real site values — never packaged
├── documentation/              # FEATURES, ARCHITECTURE, DEPLOYMENT, PRODUCTION,
│                               # THEMING, CLIENT-DEEPSEEK-V4.1 (a worked client diff)
├── deploy/                     # kyverno-mlis-mooncake-rdma.yaml + its README
├── scripts/
│   └── patch-model-mooncake.py # make an InferenceService a client (tcp or rdma)
├── ezapp/                      # PCAI import artifact (EzAppConfig + logo + values)
├── automation.sh               # release pipeline (build + prune + scan)
└── hardlinker.py               # delivery-mirror tooling (pcai_utils)
```
