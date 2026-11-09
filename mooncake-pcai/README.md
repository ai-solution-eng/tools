# mooncake — a PVC-backed KV-cache store for PCAI

A PCAI (HPE Private Cloud AI / Ezmeral Unified Analytics) application that runs
**Mooncake Store** as a cluster-wide KV-cache pool, plus a read-only dashboard that
shows how the pool and each model deployment are doing.

Model deployments (SGLang on this fleet) use it as a shared **L3 HiCache tier**: a
prefix computed by one replica can be reused by any other replica of the same model,
on any node, after a restart. Several models can share the same store — they are
isolated by key prefix and never see each other's KV.

**The capacity tier is a PVC per store pod** (a StatefulSet volume claim template), so
the pool is as large as the storage class you point it at — on G2, HPE Alletra
class the cluster exposes — and it survives pod restarts and reschedules.

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

## Layout

```
mooncake-pcai/
├── README.md
├── helm/                       # the chart (packaged from here)
│   ├── Chart.yaml
│   ├── values.yaml
│   ├── .helmignore
│   ├── PORTING.md              # what was changed for PCAI, and why
│   ├── files/app.py            # the dashboard app (stdlib Python, ConfigMap-mounted)
│   ├── templates/
│   ├── values-examples/        # site overlays (values.g2.yaml)
│   └── local/                  # your site values, ignored by packaging
├── documentation/
│   ├── ARCHITECTURE.md         # components, data flow, the two storage tiers
│   ├── DEPLOYMENT.md           # install + how to point model deployments at it
│   ├── PRODUCTION.md           # transport, availability, capacity, the known gaps
│   └── THEMING.md              # the Auto/Light/Dark pattern, reusable in other apps
├── scripts/
│   └── patch-model-mooncake.py # make an InferenceService a client (tcp or rdma)
└── ezapp/                      # PCAI import artifact (EzAppConfig + logo)
```

## Quick start

```bash
# site values: 3 store pods x 350Gi (~1.05 TiB), gl4f-filesystem, 16 GB DRAM/pod
# then a transport profile: tcp (the default) — values.transport-rdma.yaml for RDMA
helm install mooncake ./helm \
  --namespace mooncake --create-namespace \
  -f helm/local/values.g2.yaml \
  -f helm/values-examples/values.transport-tcp.yaml
```

That file is the **demo** footprint (3 x 350Gi). `helm/values-examples/values.g2.yaml` is
the same shape sized as a generic template (3 x 1Ti) with a placeholder host and class,
for sharing.

Then open `https://mooncake.<your-domain>` (or port-forward the UI service) and follow
[documentation/DEPLOYMENT.md](documentation/DEPLOYMENT.md) to add a model deployment as
a client.

### Import the PCAI way

```bash
helm package ./helm                             # -> ./mooncake-<version>.tgz (repo root)
# upload that archive in AI Essentials -> Tools & Frameworks -> Import Framework
#   (./automation.sh <version> does this and the pruning: helm-p yields
#    mooncake-<version>.tar.gz, plain helm package yields .tgz),
# or apply the pre-built artifact:
kubectl apply -f ezapp/ezappconfig-mooncake.yaml
```

The artifact installs into the `mooncake` namespace and publishes
`mooncake.${DOMAIN_NAME}`. Its values are `ezapp/values-override.yaml`; change them and
re-run `python3 ezapp/make_ezappconfig.py` to regenerate.

## Sizing: two tiers, not one

| Tier | Value | What it is |
|---|---|---|
| DRAM hot cache | `store.segmentSize` (default `8gb`) | registered host memory **inside the store pod**. The store serves pages from here at memory speed. **Not pod storage — a PVC cannot replace it.** |
| PVC capacity | `store.persistence.size` (default `500Gi`) | one PVC per store pod, from the StatefulSet volume claim template. The bulk of the pool. Survives restarts. |

Pool ≈ `replicaCount x (segmentSize + PVC size)`, independent of how many models or
replicas use it.

| | 3 pods give |
|---|---|
| Chart defaults (`values.yaml`) | 24 GB DRAM + 1.5 TiB PVC |
| G2 site file (`helm/local/values.g2.yaml`) | 48 GB DRAM + **~1.05 TiB PVC** |

## The dashboard

Two panels, refreshed from a cached snapshot every 30 s in the browser and re-read from
the cluster every `ui.refreshIntervalSeconds` (default 1800 s = 30 min) in the backend,
with a **Refresh** button for an immediate re-read:

- **Store** — capacity, used %, keys, clients, segments, evictions, allocation failures.
- **Models** — per model deployment: replicas, node, KV pool size (tokens), used tokens,
  token-usage %, cache-hit rate. Models are auto-discovered: pods carrying a
  `MOONCAKE_MASTER` env var, grouped by the `serving.kserve.io/inferenceservice` label.

> Per-model *store* usage (how many keys a model holds) is **not** exposed by Mooncake:
> the master reports pool-wide counters only. The Models panel therefore shows each
> replica's own cache state, which is what actually predicts behaviour.

## Limitations worth knowing

- **Single master = single point of failure.** HA needs etcd and is out of scope; a
  master restart empties the pool metadata (the PVC data survives, the index does not).
- **It is a cache, not storage.** Losing a store pod loses its DRAM share; the PVC
  survives, but objects whose metadata died with the master are misses.
- **Protocol is one-size.** The store sets it at install time and every client must match
  it. TCP is the default and what the G2 site file pins; RDMA is a supported one-file switch
  (`helm/values-examples/values.transport-rdma.yaml`) that needs the device mounted on the
  store pods and the client policy in place — see
  [documentation/DEPLOYMENT.md §4.0](documentation/DEPLOYMENT.md).
- **Network storage latency.** The PVCs are network-attached (VAST CSI today): fine for a cache
  tier, but restore latency is higher than local NVMe. Keep the DRAM segment big enough
  to absorb the hot set.

## Releasing

`./automation.sh <version>` is the fleet-standard release entry point (same
`bump -> package -> prune` interface as the other PCAI repos):

```bash
./automation.sh 0.2.3      # bump chart -> helm-p -> prune old chart archives
```

It bumps `helm/Chart.yaml` `version`/`appVersion` through the shared
`bump_version.sh`, packages the chart with `helm-p` (falling back to plain
`helm package` when that wrapper is not on `PATH`), and prunes stale
`mooncake-*.tar.gz` / `*.tgz` archives from the repo root with the shared
`prune_charts.py` (newest per chart kept). The script fails closed if the
chart version did not actually move.

There is **no image build step**: the chart deploys the upstream
`kvcacheai/mooncake` image, and its `image.tag` (`0.3.14-cuda12`) is a
third-party pin — a mooncake release moves the chart version only.

## Delivery mirror

The repo is mirrored into `../pcai-solutions/tools/mooncake-pcai` (the
git-tracked delivery tree) by the shared hardlinker:

```bash
python3 hardlinker.py -c hardlink_config.json          # preview (config is dry_run: true)
python3 hardlinker.py -c hardlink_config.json --run    # apply; add --prune to drop orphans
```

The mirror is built from **hardlinks**, so an edit in either tree is the same
inode — never copy files between them, and never rewrite one with a
temp-file-and-rename. It carries the chart, docs, the ezapp artifact and the
packaged `mooncake-<version>.tar.gz`; `automation.sh` and stale
`helm/*.tgz` archives are ignored, as are the shared base rules
(`helm/local/`, caches, `__pycache__`, and the pcai_utils toolkit files).
Each run is gated by `pcai_utils/secret_scan.sh`, and
`bash ../pcai_utils/fleet_drift_check.sh` proves the shared files are still
true hardlinks (this repo is in that gate's scan set).
