# Production checklist

What this chart does for you, what it deliberately does not, and the checks that
separate "it booted" from "it is safe to show a customer".

---

## 1. Transport: TCP by default, RDMA as the upgrade

`store.protocol: tcp` is the chart default and what the G2 site file pins. It is the safe
bring-up: no device plugin, no added capability, no `hostPath`, nothing that can mismatch.
The cost is one memcpy per page plus CPU on the transfer thread — a throughput cost on a
cache tier, never a correctness cost.

RDMA is a supported one-file upgrade (`helm/values-examples/values.transport-rdma.yaml`),
and it is only half the change — **every client must use the same protocol**, because a TCP
client cannot transfer to an RDMA segment. Four things have to be true first:

1. **The store pod mounts the device.** The chart mounts `store.rdma.infinibandHostPath`
   (default `/dev/infiniband`) when `protocol: rdma`. A store pod with the resource limit
   but no mount finds no HCA and silently falls back to TCP — `ibv_devices` inside the pod
   is the check.
2. **Do not rely on the resource limit to produce the device nodes.** The device plugin on
   this unit is `k8s-rdma-shared-dev-plugin:v1.5.3` with no `--use-cdi` and no CDI
   directory mounted. Whether it also injects `/dev/infiniband` into the pod is not something
   to depend on — the `deploy/` policy records a store pod that ran RDMA with no mount, so
   both outcomes have been seen. The chart mounts the path explicitly, which is correct
   either way.
3. **The client policy is in place and verified.** `deploy/kyverno-mlis-mooncake-rdma.yaml`
   adds the device, `IPC_LOCK` and the mount to client pods, which a Knative revision cannot
   carry itself (DEPLOYMENT.md §4.3). Confirm it fires with the server-side dry-run in
   `deploy/README.md`.
4. **The pool is already proven on TCP.** Change one variable at a time.

Store side (the chart does this when `protocol: rdma`):

| Field | Value | Why |
|---|---|---|
| `resources.limits.rdma/rdma_shared_device_a` | `1` | asks the NVIDIA RDMA shared-device plugin for a device |
| `securityContext.capabilities.add` | `IPC_LOCK` | lifts `RLIMIT_MEMLOCK` so the segment can be mlocked and registered; Kubernetes cannot set the memlock ulimit directly |
| `/dev/infiniband` hostPath | `store.rdma.infinibandHostPath` | the transfer engine opens the HCA through these device nodes; the chart mounts them rather than relying on the device plugin to inject them |
| `MOONCAKE_PROTOCOL` | `rdma` | selects the transport |

Client side (not expressible in the catalog — run the script):

```bash
scripts/patch-model-mooncake.py --isvc <isvc> --protocol rdma
```

It adds the same three things to the serving pod (device limit, `IPC_LOCK`,
`/dev/infiniband`) plus the client env.

### Verify, don't assume

```bash
kubectl -n mooncake exec mooncake-store-0 -- ibv_devices        # a real HCA, not empty
kubectl -n mooncake logs mooncake-store-0 | grep -iE "tcp_transport|memcpy|infiniband"
```

- **Good**: no `TcpTransport: listen on port ...`, no `TCP-only environment, memcpy enabled`.
- **Bad**: either line present means it silently fell back to TCP. The pod still
  works, so nothing will alert you — this is the check that matters.

Common RDMA failures and what they mean:

| Symptom | Cause |
|---|---|
| `Requested address ... not found` | the engine tried to register an address the NIC cannot see — usually no `/dev/infiniband` in the pod, or the wrong interface selected |
| pod Pending, `rdma/rdma_shared_device_a` | no device plugin on that node, or all devices allocated |
| `Failed to register buffer` | mlock refused — `IPC_LOCK` missing |
| transfers hang after a NIC flap | check `dmesg` for RoCE/IB errors on the node; MOFED 25.01 is installed |

---

## 2. Availability

| Concern | State |
|---|---|
| Store pod loss | tolerated: 3 replicas, soft anti-affinity spreads them, `minAvailable: 2` PDB keeps a drain from taking two |
| **Master loss** | **not tolerated.** One master, in-memory metadata. A restart empties the index; the PVCs keep files that nothing references. HA needs etcd (`--enable_ha=true --ha_backend_type=etcd`) and is out of scope here |
| Master restart blast radius | all clients re-register and re-warm (up to 600 s poll); the pool repopulates from writes |

If the master is a hard requirement for uptime, this chart is not yet the answer —
say so rather than demoing around it.

---

## 3. Capacity

- **Pool** = `replicaCount × (segmentSize DRAM + persistence.size PVC)`.
- **`master.quotaBytes`** caps the SSD tier (`0` = 90% of the mounted volume).
- **Eviction** starts at `master.evictionHighWatermarkRatio` (0.95).
- The PVC is a **quota on a shared thin pool** — binding reserves nothing, and a
  full array surfaces as `ENOSPC` at write time. Size against the array, not the
  sum of PVCs (see ARCHITECTURE.md).
- **KV pages do not dedup.** Model weights compress and dedup heavily on the
  array; KV cache does not. Budget the tier at close to its logical size.

---

## 4. Observability

- Dashboard: `https://<endpoint>` — store counters plus per-replica cache state.
- Master: `/metrics` (Prometheus), `/metrics/summary` (human), `/health`,
  `/get_all_segments` on `:9003`.
- **Do not trust Mooncake's storage prefetch/backup counters on DeepSeek-V4** —
  they are fed only by the v1 code path, which returns early on that model's
  logical KV anchor. Use `usage.prompt_tokens_details.cached_tokens` as the byte
  oracle.

---

## 5. Change management

- Pin the chart version and the image tag (both are moving otherwise).
- One variable per roll. A transport change (tcp→rdma) touches the store **and**
  every client, so it is a maintenance-window change, not a rolling one.
- After any master restart, compare the dashboard's `Keys` against the PVC
  `Used`; files that outlive their keys are orphans.

---

## 6. Known gaps

1. **Client pod spec is not catalog-expressible.** RDMA devices, `IPC_LOCK` and
   the per-pod hostname all need fields the catalog has no slot for. Every
   deployment needs the patch script; a platform that exposes these would remove
   the manual step.
2. **Single master.** See §2.
3. **SSD tier is young.** It works on 0.3.14 (bucket backend, 90% of the PVC),
   but treat it as a cache tier with a young failure history, not as storage.
4. **No NetworkPolicy.** The store is reachable by any pod in the cluster that
   can resolve the service. Add one if the namespace is shared.
