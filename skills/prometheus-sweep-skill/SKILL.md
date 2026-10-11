---
name: prometheus-sweep
description: Use when running a periodic Prometheus alert sweep, asked to "do a sweep" or produce a prioritized remediation report, or when deciding which alerts to publish vs suppress (alert hygiene, threshold tuning, cache-tiered inference workloads that legitimately run near their memory limit). Companion to cluster-triage (the single-incident loop) — the sweep batches ALL current alerts into one triaged report. Do NOT use for GPU serving-config changes (that is gpu-serving-change) or one specific broken pod (that is cluster-triage).
---

# Prometheus sweep

## The sweep loop

**Alerts → clusters → report.** A sweep is not forty separate incidents: most alert storms
collapse into a handful of root causes plus known noise. The job is to find the real signal,
verify each cluster of alerts with the **cluster-triage** loop, and hand back one prioritized
report. Nothing is mutated without the plan/apply contract.

1. `prom_alerts` — the first and only broad call. Capture severity, labels, and **active-since
   duration** (a 10-day-old firing is a different animal from a 90-second one — old + otherwise
   healthy usually means monitoring-config drift, not an outage).
2. **Cluster related alerts into defects.** Group before investigating: one stuck Job yields
   `KubeJobNotCompleted` + `KubeContainerWaiting` + a pod-level waiting alert — three alerts,
   one defect. One misconfigured scrape yields `TargetDown` + phantom "service down" reports.
   Target list: usually ≤10 defects behind dozens of alerts.
3. For each defect, run the cluster-triage loop (WHAT → WHERE/WHY → FIX): `prom_rules` for the
   exact `expr` that fired, `prom_query_range` for anything with a shape (leak vs plateau vs
   sawtooth — a plateau and a slow climb are opposite remediations), `k8s-ops triage`,
   `logsearch count_matches` then `search_logs`. Quote evidence; never recall it.
4. **Prioritized report** (below). Apply nothing without the plan/apply contract.
5. Re-sweep on a cadence even when nothing is on fire — calibration on the reference deployment
   went 123 → 68 alerts and surfaced a TSDB compaction quarantine and a node-exporter DaemonSet roll.

## Report format

- **P1 act now** — active damage or self-escalating (near-OOM, data dropped, jobs stuck burning
  retries). Symptom → evidence (quoted) → cause → remediation. Say what is BLOCKED and on what.
- **P2 schedule** — chronic throttling, capacity headroom, expirations with dates.
- **P3 noise/expected** — policy working as designed, info-severity. Naming these explicitly is
  what keeps the next sweep fast.
- End with the decision the human owes you (apply/delete/allowlist), never a silent mutation.

## Alert hygiene — publish growth, not steady state

Some workload classes **legitimately pin near their memory limit**. Any cache-tiered inference
server does: a host-side KV cache tier that writes through until the host tier fills (HiCache),
or an embedded KV/engram table that grows to its configured budget. For these the **plateau IS
the healthy steady state**. A generic absolute threshold (>80% of limit for 300s) fires
chronically on them — that is noise, and chronic criticals train humans to ignore alerts.

| Workload class | Generic absolute-% memory alert | Publish instead |
|---|---|---|
| HiCache predictor (write-through host KV cache tier) | suppress / inhibit | growth ladder (below) |
| Embedded KV / engram table sized to a budget | suppress / inhibit | growth ladder (below) |
| Everything else | keep as-is | — |

The replacement is an escalation **ladder** scoped to the workload class (container + namespace
selector, not pod name):

- **warning — growth detector**: usage sustained **above the known plateau** with a `for:` dwell
  (answers "is the cache growing, or is this just plateau?" — a 10m dwell absorbs long-context
  bursts).
- **critical — near-limit**: usage above ~95%. cgroup OOM happens at 100%, so this is the
  actionable one for the plateaued class.

Plus an **Alertmanager inhibit rule** so the generic container-memory alert stops double-firing
on the same pods. Deploy as a `PrometheusRule` labelled to match the Prometheus CR's
`ruleSelector` (discover the label — do not guess), then verify against live data that the
plateaued workloads produce ZERO firing at their known plateaus. **Replace, don't delete**: the
generic alert is correct for every other container in the cluster.

## The fail-closed apply contract (same as cluster-triage)

Plan first (`plan_apply` is always a dry-run), read the per-doc verdicts, then
`apply_manifest(confirm_apply: true, plan_sha256)`, then verify with `get_resource_status`.
Refusals are answers, not bugs: non-allowlisted namespace → default-deny; `Secret` and
cluster-scoped kinds → unconditional refusal; deletes need `confirm_delete: true`.
Do not hunt for a bypass flag; do not edit around a refusal.

**Blocked-remediation pattern** (the common sweep outcome — sweep rights exceed apply rights):
write the exact ready-to-apply manifest to the workspace with a TO APPLY header — only-change
vs live object, the allowlist extension needed, the apply sequence, and the post-apply
verification query. Re-validate it against the live object on every later sweep (generation
number / resourceVersion tells you whether it drifted) before trusting it.

## Gotchas

- `scrape_duration_seconds` pinned at **exactly** the scrape timeout (e.g. 10.0016 for a 10s
  timeout) means the scrape is silently **dropped**, not refused — think NetworkPolicy before
  app health. Check where the scraper actually runs vs what the policy allowlists (a
  namespaceSelector naming `monitoring` does nothing for a Prometheus in a `prometheus`
  namespace).
- An `up==0` with healthy pods and clean app logs for days = scrape-config mismatch (wrong
  port, missing namespaceSelector, relabel drift). Fix the monitoring config; do not restart
  the app.
- `KubeCPUOvercommit` can disagree with a naive requests-vs-allocatable sum — the rule's
  formula may scope differently; read the `expr` before quoting a number.
- ImagePullBackOff with `TLS handshake timeout` to an internal registry is an environment
  fault (registry reachability/clock/proxy), not a bad tag — retries will not fix it, and a
  10-day Job with thousands of pull attempts proves it.
- DaemonSet log/telemetry agents restarting in pairs = shared defect (OOM or a rejected
  upstream), not two incidents.

## Reference instantiation (one deployment's values — re-derive locally)

- Gateway connection prefix `pcai-llm-gateway` → `mcp__pcai-llm-gateway__<server>__<tool>`;
  servers: `prometheus`, `logsearch`, `k8s-ops`, `applygate`, `workbench`.
- HiCache predictors: `glm-53-flash` (~85% plateau) and `qwen-38-27b` (~81%), ns
  `project-user-andrew-bydlon`, container `kserve-container`. Ladder deployed as
  PrometheusRule `mlis-predictor-memory-ladder` (90% warn growth / 95% crit, 10m dwell;
  ruleSelector label `release: prometheus`), with inhibit rule for the generic 80% alert.
- DeepSeek-class deployments: engram KV table — same ladder treatment.
- 2026-10-09 sweep findings: (a) `TargetDown` `logbook/logbook` — NetworkPolicy
  `logbook-loki-ingress` allowlists only grafana/monitoring namespaces; Prometheus runs in ns
  `prometheus` → scrapes dropped at exactly the 10s timeout, Loki pods healthy. Ready patch:
  `scratch_workspace/logbook-loki-ingress-prometheus-allow.yaml` (adds the prometheus
  namespaceSelector) — blocked on APPLYGATE_ALLOWED_NAMESPACES += `logbook`.
  (b) Job `word-count-spark-submit` (ns `project-user-ashish-kumarj`) ImagePullBackOff —
  TLS handshake timeout to the internal harbor registry; delete blocked on allowlist += that ns.
