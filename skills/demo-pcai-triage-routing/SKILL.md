---
name: demo-pcai-triage-routing
description: "'DEMO ONLY — not a team skill. Use when deciding which PCAI team owns a support ticket, from the researcher's evidence bundle: applies the gate chain to owner_tier (infrastructure / ezmeral / aisw / model_serving). Holds the gate chain, the namespace map, and how to read the bundle. Do NOT use outside the Nov 3 demo — for real triage use cluster-triage, for fix verification use release-verify.'"
---

# PCAI triage routing (demo)

You read an evidence bundle. You do not query anything.

Decides one thing: which layer owns the ticket. **The gate chain is the definition of the
tiers** — there is no separate glossary to consult.

`model_serving` means **anything the customer deployed in their own project**: predictors,
notebooks, pipelines. Not just models.

## How to read the bundle

- `signals` — the gate input. Each line carries a label naming what fired.
- `coverage` — if any source is `failed`, drop confidence one level.
- `gaps` — if non-empty, drop confidence one level.
- `pods` — the detail behind a signal: memory limit, exit code, restarts, age.
- `docs` — nature only. Nothing relevant → `unknown`.

## Gate chain — first gate that fires wins

```
GATE 0  a bundle.logs line matches one of these:
        "no such host" + "k8registry.internal"                            -> infrastructure
        "pg_controldata" | "chmod 0755" + "/pv" | "Read-only file system" -> ezmeral
GATE 1  nodes_notready lists a node, and the pod_node signal puts the ticket pod on it
                                                                         -> infrastructure
GATE 2  pvc_not_bound > 0                                                -> ezmeral
GATE 3  not_scheduled > 0 or pending_pods > 0                            -> aisw
GATE 4  oomkilled | restarts_1h | restarts_24h | not_ready | containers_waiting
        tenant namespace (project-*, user-*)                             -> model_serving
        platform namespace                                               -> that layer, per the map
GATE 5  scrape_down > 0                                                  -> ezmeral (observability)
        scrape_targets = 0 and the ticket reports no metrics             -> ezmeral (observability)
GATE 6  http_5xx_rps > 0, and the signal names its caller:
        caller matches *prometheus*   -> GATE 5
        caller is a gateway/ingress   -> ezmeral (mesh)
        caller is the customer        -> model_serving
        no source_workload label      -> the bundle did not decompose it.
                                         Drop confidence one level. Do not query.
GATE 7  nothing fired, namespace matches the map                         -> that tier, confidence low
        namespace matches nothing                                        -> refuse ambiguous
```

Tie-break: the lowest broken layer wins. Storage before model, node before storage.

## Namespace map

| Pattern | Tier |
|---|---|
| `project-*`, `user-*` | model_serving |
| `kubeflow*`, `kuberay*`, `airflow*`, `mlis` | aisw |
| `ezua-system`, `ezdata-*`, `keycloak`, `postgresql`, `redis`, `garage`, `istio-system` | ezmeral |
| `monitoring`, `kubeprom*`, `ez-otel*` | ezmeral |
| `ezaddon-system`, `ezctl`, `hpe-*`, any other `*-system` | infrastructure |
