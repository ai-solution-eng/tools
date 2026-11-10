# OpenTelemetry Collector — trace fan-out for the LLM gateway

A small, stateless OTLP collector that receives traces from in-cluster senders
and forwards them to one or more observability backends.

**Today:** Langfuse (PCAI, namespace `langfuse-v4`).
**Tomorrow:** append another exporter — SigNoz, Tempo, Jaeger — without touching
any sender.

---

## Why this exists

The `pcai-llm-gateway` can emit OTLP spans. Two problems with pointing it
straight at Langfuse:

1. The gateway would have to hold Langfuse credentials.
2. Adding a second backend would mean changing gateway code again.

Putting a collector in the middle fixes both. Senders talk plain OTLP to one
address; the collector owns the credentials and the routing.

```
  pcai-llm-gateway ──OTLP/HTTP──► otel-collector ──┬──► Langfuse
     (no creds)                   (adds auth)      │
                                                     └──► SigNoz (later)
```

## What it does NOT do (yet)

**This deployment is inert.** Nothing sends to it until the gateway is pointed
at it. That is deliberate: deploy and verify the collector first, wire the
sender afterwards.

No VirtualService, no external endpoint. It is reachable only over cluster DNS.

---

## Deploying on PCAI

### 1. Package the chart

```bash
cd tools/otel-collector
helm package helm -d .
```

Produces `otel-collector-0.1.0.tgz` next to this README.

### 2. Upload to ChartMuseum

PCAI UI → **Tools & Frameworks** → import, and upload the `.tgz`.

### 3. Configure the framework

| Field | Value |
|---|---|
| Chart | `otel-collector` **0.1.0** |
| Namespace | `otel-traces` |
| Release name | `otel-collector` |
| Wait | checked |

Then paste the contents of `helm/values.yaml` into the values box.
The only thing you normally need to change is the Langfuse key pair.

### 4. Verify

```bash
kubectl get pods -n otel-traces
kubectl logs -n otel-traces deploy/otel-collector --tail=30
```

Healthy startup logs the receiver and exporter pipeline, and `Everything is ready`
appears at the end.

---

## Changing configuration later

**Values changes** (endpoint, credentials, sampling, adding a backend):
PCAI UI → the framework → **Configure** → edit → apply. No new chart needed.

**Chart changes** (new port, new template): bump `version` in
`helm/Chart.yaml`, re-package, re-upload. ChartMuseum will not overwrite an
existing version, so the bump is mandatory.

---

## Adding a second backend (e.g. SigNoz)

Two edits in `helm/values.yaml`, then Configure in the UI:

```yaml
config:
  exporters:
    otlphttp/langfuse: { ...existing... }
    otlphttp/signoz:
      endpoint: http://signoz-otel-collector.observability.svc.cluster.local:4318

  service:
    pipelines:
      traces:
        exporters: [otlphttp/langfuse, otlphttp/signoz]   # ← add here
```

Nothing else changes. The gateway keeps sending to the same address.

---

## Configuration reference

| Value | Default | Notes |
|---|---|---|
| `image.tag` | `0.165.0` | contrib distro, for filter/transform processors |
| `replicaCount` | `1` | stateless; raise only for ingestion volume |
| `langfuse.publicKey` / `secretKey` | set | rendered into a Secret, never into the ConfigMap |
| `langfuse.existingSecret` | `""` | use a pre-made Secret instead (key `auth`) |
| `config` | see values | passed verbatim into the ConfigMap |
| `resources` | 100m/128Mi → 500m/512Mi | `memory_limiter.limit_mib` (400) must stay below the limit |

### Endpoints exposed

| Port | Protocol | Used by |
|---|---|---|
| 4318 | OTLP/HTTP | the gateway (it speaks OTLP/HTTP JSON) |
| 4317 | OTLP/gRPC | optional, for future senders |
| 13133 | HTTP | liveness/readiness probes only |

In-cluster address for senders:

```
http://otel-collector.otel-traces.svc.cluster.local:4318
```

---

## Troubleshooting

**Pod CrashLoopBackOff** — usually a bad collector config. The collector exits
on an invalid config rather than starting degraded:

```bash
kubectl logs -n otel-traces deploy/otel-collector --tail=50
```

**Traces not appearing in Langfuse** — check in this order:
1. Is anything actually sending? (the gateway is not wired up yet by design)
2. `kubectl logs` — look for export errors (401 = bad key pair)
3. Confirm the Langfuse endpoint includes `/api/public/otel` — the collector
   appends `/v1/traces` itself
4. Confirm the `x-langfuse-ingestion-version: "4"` header is present; Langfuse v4
   rejects or ignores older ingestion paths

**Nothing in the collector logs at all** — that is normal when idle. It only
logs when it receives or fails to export.
