# Architecture

## Components

| Object | Kind | Holds |
|---|---|---|
| `<release>-master` | Deployment (1 pod) + Service | object metadata, segment registry, eviction, client liveness. **No KV bytes.** |
| `<release>-store` | StatefulSet (N pods) + headless Service | one DRAM segment and one PVC per pod — the actual pool |
| `<release>-ui` | Deployment + Service | the read-only dashboard; source comes from a ConfigMap, image is stock `python:3.14-slim` |
| `<release>-vs` | VirtualService | publishes the dashboard on the ezua gateway |

## Data flow

```
  model replica A ──┐                         ┌── store pod 0 (DRAM 16 GB + PVC 350 Gi)
  model replica B ──┼── master :50051/:8080 ──┼── store pod 1 (DRAM 16 GB + PVC 350 Gi)
  model replica C ──┘   (control plane only)  └── store pod 2 (DRAM 16 GB + PVC 350 Gi)
```

1. Each serving replica registers with the master and contributes **no memory**
   (`MOONCAKE_GLOBAL_SEGMENT_SIZE=0`, pure client).
2. On a prefix miss that the replica wants to keep, TP rank 0 writes the KV pages
   to a store pod; the write goes peer-to-peer, not through the master.
3. Any replica of the same model that later needs those pages reads them from the
   store pod that holds them — the key prefix is the model name, so replicas of
   the same model share and different models never collide.
4. Eviction is driven by the master (`evictionHighWatermarkRatio`); each store pod
   keeps its hot pages in DRAM and the rest on its PVC.

## Why two tiers

`store.segmentSize` is **DRAM**, registered host memory inside the store pod. The
store serves KV pages from it at memory speed. It is not pod storage, and a PVC
cannot replace it — a pod with no segment has nowhere to put a page.

`store.persistence` is the **capacity tier**: a PVC per store pod, created from the
StatefulSet volume claim template so it survives pod restarts and reschedules.
That is where the bulk of the pool lives, and it is what makes the pool larger
than the nodes' RAM.

| | DRAM segment | PVC tier |
|---|---|---|
| Backed by | host memory | the storage class (`gl4f-filesystem` / VAST CSI on G2 today) |
| Speed | memory | storage latency |
| Survives pod restart | no | **yes** |
| Sized by | `store.segmentSize` | `store.persistence.size` |
| Total | `replicaCount x segmentSize` | `replicaCount x size` |

## Failure behaviour

- **A store pod dies** → its PVC survives, its DRAM segment does not. Objects
  homed there become misses and are recomputed; the pool refills.
- **The master dies** → clients keep running on their own L1/L2 caches, but no new
  objects are placed and no reads are routed. The master is a single point of
  failure in this chart (HA needs etcd and is out of scope).
- **A model replica dies** → nothing happens to the pool; it is a client only.

## Capacity: quotas on a shared thin pool

A PVC here is a **quota, not a reservation**. The CSI driver enforces a per-volume cap
(`kubelet_volume_stats_capacity_bytes` reports that cap, and `available_bytes` is what is
left of it), but the physical space is one thin-provisioned pool shared by every PVC on
the class.

That is the same *shape* as CPU/memory requests, with one important difference:

| | CPU / memory | Storage (this class) |
|---|---|---|
| Admission control | kubelet refuses pods that do not fit | **none** — no storage scheduler |
| Overcommit | possible, by design | possible, silently |
| Failure mode | OOM-kill / eviction | `ENOSPC` on write, in whichever tenant writes last |
| Per-tenant protection | requests/limits | the PVC quota |

So a per-PVC quota protects a *tenant* from runaway growth; it does not protect the
*pool* from overcommit. Size against the array's own free space, not the sum of PVCs.

Measured on G2, 2026-11-05:

| Measure | Value |
|---|---|
| Export `172.28.2.5:/aiessentials` (parent mount) | **343.8 TiB** total, 7.5 TiB used, **336.3 TiB available** |
| PVC quotas promised | 328 TB ≈ 298 TiB — **≈87% of the export** |
| Per-PVC usage, summed | 61 TB ≈ 55 TiB logical |

Three things follow:

- **The pool is not near capacity (2% written) but quota allocation is at ~87%.** The
  resource that runs out first is *promise*, not space — a big new PVC can fail to bind
  while 336 TiB sits free.
- **Physical ≪ logical** because the array dedups and compresses; model checkpoints
  duplicate heavily across projects, which is what flatters that 2%.
- **KV cache will not dedup.** fp8/bf16 KV pages are high-entropy, so the Mooncake SSD
  tier consumes close to its logical size. Size it deliberately — the pool's apparent
  headroom is not the headroom a cache gets.

For Mooncake this is a comfortable failure mode: the SSD tier is a cache, so a full pool
turns into misses and recompute, not lost data. Keep `store.persistence.size` inside the
array's real headroom and watch the array, not just the pods.

