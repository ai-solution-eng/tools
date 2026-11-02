---
name: demo-pcai-triage-routing
description: '"DEMO ONLY — not a team skill. Use when routing one PCAI support ticket to the owning layer: owner_tier (infrastructure / ezmeral / aisw / model_serving) from Prometheus metrics and pod logs, in gate order. Holds the gate chain, the namespace map, the 13-family probe and the two 5xx decompositions. Do NOT use outside the Nov 3 demo — for real triage use cluster-triage, for fix verification use release-verify."'
---

# PCAI triage routing (demo)

Decides one thing: which layer owns the ticket. **The gate chain is the definition of the
tiers** — there is no separate glossary to consult.

`model_serving` means **anything the customer deployed in their own project**: predictors,
notebooks, pipelines. Not just models.

## Gate chain — first gate that fires wins

```
GATE 0  log override
        "no such host" + "k8registry.internal"                  -> infrastructure
        "pg_controldata" | "chmod 0755" + "/pv" | "Read-only file system" -> ezmeral
GATE 1  nodes_notready names a node, and pod_node puts the ticket pod on it -> infrastructure
GATE 2  pvc_not_bound > 0                                       -> ezmeral
GATE 3  not_scheduled > 0 or pending_pods > 0                   -> aisw
GATE 4  oomkilled | restarts_1h | restarts_24h | not_ready | containers_waiting
        tenant namespace (project-*, user-*)                    -> model_serving
        platform namespace                                      -> that layer, per the map
GATE 5  scrape_down > 0                                         -> ezmeral (observability)
        scrape_targets = 0 and the ticket reports no metrics    -> ezmeral (observability)
GATE 6  http_5xx_rps > 0, caller decomposed (§ below):
        caller matches *prometheus*  -> GATE 5
        caller is a gateway/ingress  -> ezmeral (mesh)
        caller is the customer       -> model_serving
GATE 7  nothing fired, namespace matches the map                -> that tier, confidence low
        namespace matches nothing                               -> refuse ambiguous
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

## The probe — one call, 13 families

```
label_replace((sum by (pod) (increase(kube_pod_container_status_restarts_total{namespace="<NS>"}[1h])) > 0 or on() vector(0)),"family","restarts_1h","","")
or label_replace((sum by (pod) (increase(kube_pod_container_status_restarts_total{namespace="<NS>"}[24h])) > 0 or on() vector(0)),"family","restarts_24h","","")
or label_replace((count by (pod) (kube_pod_status_ready{namespace="<NS>",condition="true"} == 0) or on() vector(0)),"family","not_ready","","")
or label_replace((count by (pod) (kube_pod_status_scheduled{namespace="<NS>",condition="true"} == 0) or on() vector(0)),"family","not_scheduled","","")
or label_replace((count by (pod) (kube_pod_status_phase{namespace="<NS>",phase="Pending"} == 1) or on() vector(0)),"family","pending_pods","","")
or label_replace((count by (pod) (kube_pod_container_status_waiting{namespace="<NS>"} == 1) or on() vector(0)),"family","containers_waiting","","")
or label_replace((sum by (persistentvolumeclaim, phase) (kube_persistentvolumeclaim_status_phase{namespace="<NS>",phase!="Bound"} == 1) or on() vector(0)),"family","pvc_not_bound","","")
or label_replace((sum by (pod) (kube_pod_container_status_last_terminated_reason{namespace="<NS>",reason="OOMKilled"} == 1) or on() vector(0)),"family","oomkilled","","")
or label_replace((sum by (pod, node) (kube_pod_info{namespace="<NS>"}) or on() vector(0)),"family","pod_node","","")
or label_replace((count by (node) (kube_node_status_condition{condition="Ready",status="true"} == 0) or on() vector(0)),"family","nodes_notready","","")
or label_replace((sum by (source_workload, response_code) (rate(istio_requests_total{response_code=~"5..",destination_workload_namespace="<NS>"}[5m])) or on() vector(0)),"family","http_5xx_rps","","")
or label_replace((count by (job) (up{namespace="<NS>",job=~".*podmonitor.*"}) or on() vector(0)),"family","scrape_targets","","")
or label_replace((count by (job, instance) (up{namespace="<NS>",job=~".*podmonitor.*"} == 0) or on() vector(0)),"family","scrape_down","","")
```

Three rules:
- **`or on() vector(0)`, never `or vector(0)`.** A plain `or` makes a family that has data
  report its real rows *and* a contradictory zero. Measured: `oomkilled{pod=x}=1` and
  `oomkilled=0` from one query.
- **`count by`, never `sum by`, on a boolean filter.** `up == 0` keeps the value 0, so `sum`
  reports 0 for a target that is down.
- A family with a `pod=`, `persistentvolumeclaim=`, `node=`, `job=` or `source_workload=` label
  **names** what fired. That label is the proof. A bare number is not.

## Before you route on a 5xx — decompose

```
sum by (source_workload, response_code) (rate(istio_requests_total{response_code=~"5..",destination_workload_namespace="<NS>"}[5m]))
sum by (source_workload) (rate(istio_requests_total{destination_workload_namespace="<NS>"}[5m]))
```

The second tells you whether any real traffic exists. If the only caller is `prometheus-*`,
the ticket is about monitoring, not about users.

## Two traps

- A metric that does not exist returns **empty**, not an error. Confirm it exists before
  trusting a zero.
- `prom_series` and `prom_label_values` fail on every call here. Do not use them.
