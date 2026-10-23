# Diagnostic Jobs in sidecar-injected namespaces (pcai-llm)

## The trap

`pcai-llm` carries `istio-injection: enabled`. Any pod created there — **including
Job pods** — gets an `istio-proxy` sidecar injected automatically.

A Job is `Complete` only when **every** container in its pod exits. The
`istio-proxy` sidecar never exits on its own, so:

1. the Job's real container finishes (`exitCode: 0`, `reason: Completed`),
2. the sidecar keeps running → the pod never reaches Ready (`1/2`) and the
   Job never reaches `Complete` — it sits in `active: 1` forever,
3. `ttlSecondsAfterFinished` only starts counting **after** completion, so it
   never fires and nothing is ever garbage-collected,
4. the pod lingers indefinitely as a `1/2 NotReady` orphan.

Observed 2026-10-14: 14 diagnostic Jobs from a memory-feed RCA accumulated in
`pcai-llm`, every one showing `1/2 NotReady` with `istio-proxy ready=true` and
the probe container `terminated/Completed`. None of them were failing — they had
all finished their work and could not be reaped.

## The fix

Set the injection annotation on the **pod template** of every one-shot workload:

```yaml
spec:
  template:
    metadata:
      annotations:
        sidecar.istio.io/inject: "false"   # <- REQUIRED for Job/Pod one-shots
```

That is the whole fix: the pod then has a single container, the Job completes,
and `ttlSecondsAfterFinished` reaps it normally.

## Cleanup for an already-stuck pod

Deleting the Job does not always remove its pod (the GC may strip the owner
reference first, leaving a true orphan). If a `1/2 NotReady` probe pod survives
its Job:

```bash
kubectl -n pcai-llm delete pod <name> --force --grace-period=0
```

## Rules for future diagnostics in this namespace

- Never apply a bare `Pod` (applygate refuses the kind anyway, and an
  ownerless pod can never be reaped). Use a `Job`.
- Always set `sidecar.istio.io/inject: "false"` on the Job's pod template.
- Always set a short `ttlSecondsAfterFinished` (e.g. `120`) so the Job — and
  therefore its pod — disappears on its own.
- Prefer one reusable probe Job over many one-off Jobs.
