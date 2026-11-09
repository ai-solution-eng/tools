# values-examples

Site overlays for `helm/values.yaml`. They are **not packaged** into the chart
(`.helmignore` excludes this directory) — pass them with `-f` at install time.

| File | Use |
|---|---|
| `values.g2.yaml` | generic G2 template: 3 x 1Ti = 3 TiB of PVC on `gl4f-filesystem` (swap for the X10000 class when one exists), dashboard scanning the model namespace. **Not the demo size** - that lives in `../local/values.g2.yaml` at ~1.05 TiB |
| `values.transport-tcp.yaml` | transport profile: TCP (chart default). No device, no capability, no silent fallback |
| `values.transport-rdma.yaml` | transport profile: RDMA. Needs the device **and** `/dev/infiniband` on both sides, plus every client repatched |

```bash
helm install mooncake ./helm -n mooncake --create-namespace \
  -f helm/values-examples/values.g2.yaml \
  -f helm/values-examples/values.transport-tcp.yaml
```

The transport profile goes **last**: a later `-f` wins, so it overrides the
`store.protocol` a site file pins inline. Pick one profile per install - the
transport is a whole-pool decision, not a per-model one.

Keep real site files (credentials, storage class names, hostnames) in `helm/local/`
if they must not be committed; that directory is ignored too.