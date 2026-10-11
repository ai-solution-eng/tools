---
name: cluster-triage
description: Use when something is wrong in the cluster — an alert fired, a pod is unhealthy, a service is down, or the user asks "what's going on / is anything broken". Covers the prometheus → logsearch/k8s → applygate ops loop and the fail-closed apply contract. Do NOT use for GPU serving-config changes (that is gpu-serving-change) or for "is my fix deployed" (that is release-verify).
---

# Cluster triage

## The ops loop

**Prometheus says WHAT. logsearch + k8s say WHERE/WHY. applygate FIXES.**

Never skip a stage, and never jump from "an alert fired" straight to "I'll patch it". The loop is
also the reporting order: symptom → evidence → cause → fix.

1. **WHAT (prometheus)** — is anything wrong, and what is the exact condition that fired?
2. **WHERE/WHY (logsearch + k8s-ops)** — which pod/node/namespace, and what do its own logs and events say?
3. **FIX (applygate)** — only after 1 and 2, and only through the plan/apply contract below.

## Tool names — use the FOUR-PART form

Gateway tools are called as `mcp__<gateway-connection>__<server>__<tool>`; the connection prefix is
whatever your client namespaced the gateway under — read it off your own tool list:

- `mcp__<gateway-connection>__prometheus__prom_alerts`
- `mcp__<gateway-connection>__k8s-ops__triage`
- `mcp__<gateway-connection>__logsearch__search_logs`
- `mcp__<gateway-connection>__applygate__plan_apply`

**Flat two-part names (`<server>__<tool>` — a bare server alias) are refused as "unknown tool"**; that
cost 3 wasted calls in one reference incident (2026-09-19). If a call errors "unknown tool", retry
with the four-part form before anything else.

## Procedure

1. `prom_alerts` — the first call for "is anything wrong?". Read severity, labels, and how long it has been active (a
   9-day-old firing is a different animal from a 90-second one).
2. `prom_rules` (`state: "firing"`, `search: "<name>"`) — get the **exact `expr` and threshold** that fired. Never
   reason about an alert whose expression you have not read.
3. `prom_query` / `prom_query_range` — confirm the condition with your own numbers. Use `prom_query_range` for
   anything with a shape (leaks, sawtooth, plateau, "it keeps restarting"): **a restart wants the memory CURVE, not
   one sample.** Use `prom_series` / `prom_label_values` to discover real label sets instead of guessing selectors.
4. `k8s-ops triage` (`namespace`) — one call gives the attention list (restarts, not-Ready,
   OOMKilled/CrashLoopBackOff, failing probes), pods, workloads ready-vs-desired, recent Warning events, and PVC
   states. Narrow with `app=` when the namespace is busy.
5. `logsearch count_matches` — "where is this error coming from?" Ranked per-pod counts for one pattern. Do this
   **before** pulling full logs; it tells you which pod holds the problem.
6. `logsearch search_logs` — fan-out regex across the namespace, merged chronologically with `podname/container:`
   provenance. This is the read that produces citable evidence.
7. `logsearch get_pod_logs` (or `k8s-ops get_pod_logs`) — one known pod's tail. **`previous: true` for a crashed
   container** — the first move for a CrashLoopBackOff pod whose current container has nothing to say.
8. **Only now** consider a fix. If the cause is a stale image, hand off to **release-verify**.

For node/GPU symptoms use `k8s-ops cluster_health`, `get_resource("nodes")`, and the DCGM series
(`GPU_UTIL`, `FB_USED`/`FB_FREE`, `XID_ERRORS`, `NVLINK_BANDWIDTH_TOTAL`). Workload attribution
labels are **`exported_namespace` / `exported_pod` / `exported_container`** — plain `pod`/`namespace`
attributes the metric to the exporter itself.

## The evidence rule

**Quote evidence; never recall it.** Cite the log line, event, or metric series verbatim, with its
source. A model may never override a log line, a dry-run result, or a file's contents.

- Bad: "the pod looks like it's OOMing." Good: `OOMKilled` in the pod status, plus the quoted line
  `torch.OutOfMemoryError: CUDA out of memory. Tried to allocate 1.49 GiB` from the previous container, plus restart
  count and timestamp.
- If you cannot quote it, you have not established it — go read it, or say you could not.

Corollary: **a subagent's finding is a hypothesis until reproduced.** Reference incident
(2026-10-02): 26/26 lead spot-checks held, but 2 subagent over-claims were corrected. Reproduce
before reporting.

## Making a change — applygate is fail-closed

Any manifest change goes through the plan/apply contract. There is no shortcut.

1. **`plan_apply` FIRST — always a dry-run** (`dry_run=All`): nothing is created, patched, or deleted. No flag turns a
   plan into a mutation; `force` is accepted but never changes the plan.
2. **Read the per-document verdicts.** The result carries `manifest_sha256` — the sha256 of the EXACT planned bytes.
3. **`apply_manifest` with `confirm_apply: true` and the plan sha.** D11 binds the write to the planned bytes: the
   call must carry the plan sha (or reference it from session state), and it must match both this call's manifest
   bytes and the recorded plan. A missing plan or tampered bytes **refuses by default**. Then **verify** with
   `get_resource_status` — do not assume the apply took.

Refusals are **fail-closed and are not bugs to work around**: a non-allowlisted namespace is refused
(default-deny), **`Secret` and cluster-scoped kinds are refused unconditionally**, and
`delete_resource` needs `confirm_delete: true` within the same namespace/kind guardrails. A refusal
is the answer — do not hunt for a flag that bypasses it, and do not edit around it.

## RCA is a genuine ensemble

The two ensemble cells are **peers**, not student/teacher — on the reference benchmark (kube RCA)
they scored **51% vs 47%**. Both are competent and they fail differently.

- Run **one** derivation first. **Run both only when the first pass fails to converge** — a disagreement you can
  adjudicate, or evidence that contradicts itself.
- When you do run both, adjudicate point-by-point **against machine evidence** (log lines, events, dry-runs) — never
  by which output sounds more confident. See the **cross-validate** skill.
- Per-namespace **read-only verification subagents** are the established pattern for fleet sweeps (8 used 2026-09-10);
  auditors are read-only — say so in their prompt.

## Platform traps (each has burned time before)

- **A scheduler-stamping admission policy makes LABELED cron pods undeletable.** A platform policy
  (`failurePolicy=Fail`) that selects pods by a label and stamps `spec.schedulerName` on every admission: a pod not
  already carrying that schedulerName fails every update, so the job-tracking finalizer is never removed and the Job
  never completes — 145 cron ticks suppressed in the reference deployment. **Cron pods must stay unlabeled**; those
  labels exist for the platform console's app-discovery, which reads **Deployment/Service only**.
- **A console "Unknown" card** = the app-discovery **label match** over Deployments/Services failed. The badge is
  **not** computed from live probes, policyreports, or pod health — a healthy app shows "Unknown" if the labels are
  missing or the chart name changed.
- **`/metrics` is per-process.** A multi-worker server (e.g. gunicorn with N uvicorn workers) registers
  `prometheus_client` counters per-PROCESS: **one scrape shows only the counters of the worker that served it**
  (search counters can look empty right after a search). Check the deployment's worker count; scrape repeatedly or
  aggregate across scrapes; the proper fix is multiproc mode (`PROMETHEUS_MULTIPROC_DIR`) or a sidecar.
- **`imagePullPolicy: IfNotPresent` + mutable tags explain most "my fix isn't there" reports** — hand off to
  **release-verify**. Reads are namespace-scoped: check the RBAC trio for the namespace you are reading before
  concluding "no access" is a bug.
- A sweep is worth running even when nothing is on fire (reference calibration: alert count 123 → 68, surfacing a TSDB
  compaction quarantine, a rule-eval `on(namespace,pod)` join bug, and a node-exporter DaemonSet roll).

## Gotchas

- `prom_query` rate windows need **≥2× the scrape interval** — for a 30s scrape use `rate(...[2m])` or wider, or you
  will read noise as signal.
- `run_kubectl` is **read verbs only** (`get`, `describe`, `logs`, `top`, `explain`, `api-resources`, `api-versions`,
  `cluster-info`, `version`, `auth`, `events`); connection/impersonation flags are rejected. Prefer the purpose-built
  tools for objects and events.
- `exec_in_pod` is opt-in and narrow: the pod must carry `k8s-mcp.io/exec="true"`, the namespace must be on the exec
  list, and only allowlisted binaries run (`ps`, `ls`, `cat`, `tail`, `head`, `grep`, `df`, `du`, `free`, `ss`,
  `netstat`, `ip`, …) — no shells/interpreters/`curl`/`wget`. Check `check_rbac("create", "pods/exec", namespace)`
  first.
- A "down" service is often a **wrong probe port** — the same pod-monitor `TargetDown` pattern recurs across different
  users' predictors. `/tmp` does not persist between bash calls: every verification must be one self-contained call.
  Never run `git checkout`/`restore`/`clean`/`stash` while investigating (see **guard-uncommitted-work**).

## Reference instantiation (one deployment's values — re-derive locally)

Examples, not defaults: resolve your own equivalents (see PORTABILITY.md) before using any of them.

- Gateway connection prefix `pcai-llm-gateway` → `mcp__pcai-llm-gateway__<server>__<tool>`; servers seen:
  `prometheus`, `logsearch`, `k8s-ops`, `applygate`, `mm-rag`, `sqlhandler`, `workbench`.
- Namespaces: app `mm-rag`; MLIS/KServe predictor isvcs in `project-user-andrew-bydlon`.
- Platform: "G2" cluster with an EzUA console; scheduler-stamping admission policy
  `assign-custom-scheduler-for-ezua-user-vendor-pods` (`failurePolicy=Fail`) selecting on the `hpe-ezua/type` label;
  console app-discovery keys `hpe-ezua/type: vendor-service` + `hpe-ezua/app: <chart-name>`.
- Web server: gunicorn with 4 uvicorn workers (per-process `/metrics`).
- Sweep calibration: alert count 123 → 68; a TSDB compaction quarantine (28 orphan blocks); a rule-eval
  `on(namespace,pod)` join bug; a node-exporter DaemonSet roll.