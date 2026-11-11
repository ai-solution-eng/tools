# deploy/

Cluster-scoped pieces that are applied once, outside the chart.

| File | What | Who |
|---|---|---|
| `kyverno-mlis-mooncake-rdma.yaml` | Mutating policy, two rules: every Mooncake client pod gets its per-pod transfer endpoint; pods with `MOONCAKE_RDMA=true` also get the RDMA device, `IPC_LOCK` and `/dev/infiniband` | cluster administrator, once |

Pick the transport before installing the store — it is a whole-pool decision, and every
client must match it. The store has to be `rdma` before a client may set
`MOONCAKE_RDMA=true`; a TCP client must leave it unset, because rule 2 keys on it and would
otherwise add a device and a `/dev/infiniband` mount to a pod that speaks TCP. See
[DEPLOYMENT.md §4.0](../documentation/DEPLOYMENT.md#40-choose-the-transport-first-tcp-or-rdma).

## Why this is not in the chart

Two independent walls, and the policy is the only thing that clears both.

**MLIS** accepts only image, arguments, environment, `/dev/shm` size and one PVC — no
device resource, no capability, no second volume. Its environment map holds literal
strings, so it cannot express a `valueFrom.fieldRef` either.

**Knative** is the second wall, and it is the one that bites hardest. A KServe
Serverless predictor is a Knative Revision, and Knative validates that revision template
far more strictly than it validates a Pod:

| Field | Knative |
|---|---|
| `hostPath` volume | never allowed |
| `env[].valueFrom.fieldRef` | gated by `kubernetes.podspec-fieldref` |
| `securityContext.capabilities` | gated by `kubernetes.podspec-securitycontext` |

On this unit both feature gates are absent from `knative-serving/config-features`, so
they are **closed**. An InferenceService carrying any of the three is rejected by
`validation.webhook.serving.knative.dev` and the ISVC stops reconciling — the service
keeps serving the previous revision, but no new one can be created.

Mutating the **Pod** is the only injection point downstream of that validation: Knative
has already admitted the Revision by the time the Pod is created, and a Pod accepts all
three fields normally.

## What a model needs

Everything that is expressible goes in the MLIS catalog entry — two arguments and five
literal environment variables:

```
arguments:   --hicache-storage-backend mooncake --hicache-storage-prefetch-policy timeout
environment: MOONCAKE_MASTER, MOONCAKE_TE_META_DATA_SERVER, MOONCAKE_PROTOCOL,
             MOONCAKE_GLOBAL_SEGMENT_SIZE=0, MOONCAKE_CHECK_SERVER=1
             MOONCAKE_RDMA=true          # RDMA only; omit for a TCP store
```

The policy supplies the rest:

| Rule | Fires when | Injects |
|---|---|---|
| `add-mooncake-fieldrefs` | the pod has `MOONCAKE_MASTER` | `MOONCAKE_LOCAL_HOSTNAME` ← `status.podIP`, `MOONCAKE_HOST_ID` ← `spec.nodeName` |
| `add-mooncake-rdma` | the pod has `MOONCAKE_RDMA=true` | `rdma/rdma_shared_device_a: 1`, `IPC_LOCK`, `/dev/infiniband` |

Rule 1 applies to **both** transports — the per-pod transfer endpoint is needed even over
TCP. Rule 2 is RDMA only.

## Verify the policy before you trust it

Server-side dry-run runs the whole admission chain, including this policy, and returns the
mutated object without creating anything. Do this once after installing, and again after
any edit to the policy:

```bash
kubectl -n project-user-andrew-bydlon run mc-policy-test --image=busybox --restart=Never \
  --dry-run=server -o yaml \
  --labels='serving.kserve.io/inferenceservice=mc-policy-test' \
  --overrides='{"spec":{"containers":[{"name":"kserve-container","image":"busybox",
    "command":["sleep","600"],
    "env":[{"name":"MOONCAKE_MASTER","value":"mooncake-master.mooncake.svc.cluster.local:50051"},
           {"name":"MOONCAKE_RDMA","value":"true"},
           {"name":"KEEP_ME","value":"1"}]}]}}' \
  | grep -nE 'MOONCAKE|KEEP_ME|IPC_LOCK|rdma|infiniband'
```

You want to see **all** of:

- `MOONCAKE_LOCAL_HOSTNAME` with a `fieldRef: status.podIP`
- `MOONCAKE_HOST_ID` with a `fieldRef: spec.nodeName`
- `rdma/rdma_shared_device_a: "1"` under limits, `IPC_LOCK` under capabilities
- the `infiniband` volume and mount
- **`KEEP_ME` still present** — that is the check that the policy merged into the env
  list rather than replacing it. If `KEEP_ME` disappears, stop: the policy would strip
  a model's own environment and the pod would boot without its HF/proxy settings.

Repeat without `MOONCAKE_RDMA` to confirm rule 2 does not fire for a TCP client.

## What the script does now

`scripts/patch-model-mooncake.py` writes only the catalog-expressible part into the
ISVC, and in Knative-safe mode it also **removes** anything a previous run left behind
that Knative would reject (`fieldRef` env vars, `IPC_LOCK`, the device limit, the
`hostPath` volume). `--raw-deployment` switches it to the full pod-spec patch for a
KServe RawDeployment cluster with no Knative in front.
