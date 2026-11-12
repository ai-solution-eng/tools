# Model Manager — features and design

This is the depth layer for the [README](../README.md) spine: what each capability does, the design decision behind it, and where the implementation lives. The deployment procedure and prerequisites live in [DEPLOYMENT.md](DEPLOYMENT.md). The canonical, fully commented default for every chart value is [helm/values.yaml](../helm/values.yaml).

Jargon used throughout: **PVC** (PersistentVolumeClaim — a Kubernetes storage volume a pod mounts), **Job** (a Kubernetes workload that runs pods to completion and then stops), **NGC** (NVIDIA GPU Cloud, the registry NIM images and profiles come from), **MLIS** (HPE Machine Learning Inference Software, the model-serving stack on PCAI) and **AIOLI** (the MLIS model-registry database service), **oauth2-proxy** (the SSO authentication gate PCAI puts in front of apps), and **ModelScan** (an open-source scanner for unsafe pickle-based model serialization formats).

## The download pipeline

Downloads run as Kubernetes Jobs, not inside the web process. The web pod stays small and restartable; a multi-gigabyte download must survive an app restart, hold no GPU, and retry without restarting from zero — so the work happens in a Job created in the target namespace and tracked through the Kubernetes API.

### HuggingFace models onto the shared PVC

- The Job runs `huggingface_hub.snapshot_download` with `max_workers=8` per-file parallelism into `HUGGINGFACE_HUB_CACHE` on the shared PVC mounted at `/mnt` (`helm/templates/configmap-job-template.yaml`, the `job.yaml` template).
- The default cache root is `/mnt/large-models/<model>`; an explicit custom absolute path is accepted and validated (no `..` traversal).
- **A retry resumes instead of restarting**, because HF cache blobs and `.incomplete` files live on the PVC and survive pod death. `downloader.backoffLimit` (default 2) counts retries before the Job is Failed.
- The cache layout is the huggingface_hub convention `models--<org>--<repo>` — the same layout the `huggingface-cli`, serving images and every other consumer of the shared cache expect. The scanner, the GC pass and manual deletion all key on this shape.

### HuggingFace models to S3

- The Job lists the repo files and uploads each one with a thread pool of 8 concurrent files; each upload itself uses S3 multipart (`multipart_threshold=64MiB, max_concurrency=8` in the job template).
- Scratch space holds only in-flight files: an `emptyDir` sized by `downloader.s3WorkSize` (default 20Gi), or an existing PVC named by `downloader.s3WorkPvcName`, with the cache root under `downloader.s3WorkPath`.
- Objects land at `s3://<bucket>/<prefix>/<org>/<Model>/`. The PVC preflight does not apply to S3 submissions — there is no PVC to fill.

### Chat templates

A chat-template preset (from the catalog) or explicit path + contents is written into the model cache by the downloader Job during the download. A template shipped with the download means serving needs no post-download fix-up to make the checkpoint usable.

### Per-submission credentials

- Each submission creates a Secret `hf-token-<model>-<suffix>` in the target namespace (`src/model_manager/app/k8s.py`), referenced only by that Job's pod, and **deletes it as soon as the Job finishes — or immediately if Job creation is denied** (`queue.py` deletes in `finally` blocks; the reconciler cleans up after an app restart using the Job's `model-manager/hf-secret` annotation (the legacy `model-downloader/hf-secret` is also accepted).
- **The credential exists for exactly the lifetime of the work it authorizes.** There is no deploy-time HF token value, no shared token in a ConfigMap, nothing to rotate. The chart ships no token at all.

### The queue

- At most `maxConcurrency` (default 4) downloads run at once; extra submissions queue in-process (an asyncio semaphore).
- On startup the queue reconciles Jobs it previously created from Kubernetes annotations (`model-manager/*` on each Job, with the legacy `model-downloader/*` keys still accepted), so downloads submitted before an app restart reappear in the UI.
- **Queue state is in-process memory of a single-replica Deployment** (`replicas: 1`, hardcoded). A restart loses queued-but-unsubmitted records; already-submitted Kubernetes Jobs keep running and self-clean via `ttlSecondsAfterFinished` (see `helm/templates/NOTES.txt`). Scale-out would need a shared store first.
- NIM mirrors share the same queue and the same concurrency slots.

### Output URLs

PVC rows are shown as `pvc://models-pvc/large-models/<model>?containerPath=/mnt/models`, S3 rows as `s3://<bucket>/<prefix>/<org>/<Model>/` — handles other PCAI components can resolve against the mount conventions they already use.

## NIM profile mirror

**What it is:** a second download source in the submit form. It runs the NVIDIA NIM 2.x image's own `mirror s3` command as a Kubernetes Job, staging NIM model **profiles** (GPU/precision/engine variants) into the configured S3 bucket with the percent-encoded object keys that `NIM_REPOSITORY_OVERRIDE` requires.

**Why it exists:** NIM deployments read profiles from NGC or from an object store whose keys must be percent-encoded exactly as NIM expects. On an air-gapped or proxied PCAI cluster, seeding that bucket otherwise requires a workstation with Docker, NGC credentials and bucket credentials. NVIDIA ships the encoder inside the NIM image itself — so the feature runs that image as a Job rather than reimplementing the encoding. Full design and operations: [nim-mirror-design.md](nim-mirror-design.md).

Design decisions, and why:

- **Per-submission NGC key.** Each mirror creates `ngc-api-<image>-<suffix>` (the key as `NGC_API_KEY`) and — when the image comes from `nvcr.io/` — an `ngc-pull-<image>-<suffix>` docker-registry Secret (`$oauthtoken` / the key). Both are deleted when the Job finishes, and the reconciler deletes them after an app restart by reading the Job's annotations. The key never sits in a chart value, the database or the logs.
- **No GPU is requested.** `mirror s3` is CPU work; a mirroring Job must never hold a GPU a serving pod needs.
- **NIM 2.x images only, S3 only.** NIM 1.x images have no `mirror` action, and NIM does not read the HF cache layout on the PVC — so profiles on `models-pvc` are out of scope by design.
- **Bucket root, no prefix.** The mirror CLI takes a bucket name only, and NIM serves it with `NIM_REPOSITORY_OVERRIDE=s3://<bucket>`. `nim.bucket` defaults to `s3.bucket`; a dedicated bucket is the clean choice because the mirror owns the whole key space.
- **Shared queue.** Mirrors consume `maxConcurrency` slots like HF downloads — one pool, no second throttling system to tune.
- **Scratch sizing matters.** Profiles land in an `emptyDir` of `nim.workSize` (default 100Gi) or on `nim.workPvcName` before upload; a 70B profile can exceed 100 GiB. The `emptyDir` is deleted with the pod.

The Jobs table shows the exact `NIM_REPOSITORY_OVERRIDE` value to set on the NIM deployment, and a **NIM profile mirrors** table lists mirror history. Failure modes (image pull, NGC auth, S3 path style, scratch exhaustion) have their own table in [nim-mirror-design.md](nim-mirror-design.md).

## The downloaded-models inventory

- **One row per model, merged from three sources** — succeeded job history, a live S3 listing, and a short-lived PVC scanner Job (`md-scan-<id>`) — deduplicated by model name, most recent first (`storage.py`, `merge_models`).
- **No single source is complete, so none is trusted alone.** Job history dies with the record; the S3 listing cannot see PVC models; only the scanner sees on-disk truth (the `du -sb` size, the provenance manifest's presence, the ModelScan verdict). The merge is the point.
- The scanner Job's output is a six-column contract (cache dir, model, bytes, mtime, manifest, verdict), read by a parser that stays backward-compatible with the older three- and four-column formats (`tests/test_scanner_columns.py`).
- Automatic PVC rescans are opt-in: `downloadList.pvcScanEnabled` (default false) with a `pvcRefreshInterval` (default 60 s) cache TTL; S3 and job history are always fresh; **Rescan storage** forces a scan on demand.

## The model catalog

The catalog (`/catalog`) is a curated set of model **configurations** — the concrete serving recipe (GPU class, framework, quantization, GPU count, cache tier) a model can be deployed with — stored in a small PVC, seeded at startup, browsable and editable in the UI, and pushable straight into the MLIS/AIOLI `packaged_models` table. That push is the step that makes a model appear in MLIS model serving.

- **Seeded, not clobbered.** The bundled `seed_catalog.json` seeds a fresh catalog on startup without overwriting user edits or resurrecting removed entries.
- **The tree is a view, not storage.** Entries are flat; the lab → model → configuration tree is derived at render time. The lab comes from the model family, not the URI org — a re-upload under a personal namespace still files under its lab — so the stored catalog and the Push-to-MLIS payload never change shape with the tree.
- **Two filters that compose.** GPU-class radio buttons (All / H200 / RTX Pro 6000 / L40S, each showing its supported quants and VRAM) plus a search box that matches every displayed dimension (`nvfp4`, `sglang`, `hicache`, `mooncake`, `8 gpu` ...). Filters are linkable (`?gpu=h200&q=sglang`) and selection survives filter changes, so **Push Selected always pushes exactly what was picked**.
- **The Mooncake label and the RDMA chip are derived, not stored.** A configuration whose HiCache L3 tier is the shared Mooncake store is labelled `Mooncake` and carries an **RDMA setup** chip; both are computed from the serving arguments at render time. The catalog cannot express the RDMA client (device, `IPC_LOCK`, per-pod hostname), so the chip surfaces the `patch-model-mooncake.sh` step and the transport check instead of pretending the entry is complete. Because they are derived, they appear on catalogs that already hold the entry — and never ride the Push-to-MLIS payload.
- **Versions are assigned by MLIS, not authored in the catalog.** Every stored entry is `version` 1. A push lands as v1 for a name that MLIS has never seen, `max(version)+1` when the name exists with a different configuration, and a reported skip when the configuration is identical (`versioning.py`). The fingerprint covers the columns that define *what gets deployed* and deliberately excludes name, version, description and metadata — fixing a description typo must not mint a new version. A catalog JSON moved between MLIS instances therefore does not carry another instance's version numbers.
- **Projects are canonicalized.** A PCAI `project-user-<name>` namespace is accepted wherever a project is named — the push form's Project field, the entry's own project, or `catalog.defaultProject` — and the `project-` prefix is stripped before the row is written and before the fingerprint is computed, so both spellings resolve to one value. Project is part of the fingerprint: **changing a model's project mints a new version** on the next push. An empty project files the model only under **All projects** in MLIS.
- **Refresh from GitHub** pulls the upstream seed JSON from `catalog.githubUrl` (the button hides when unset; the default URL was verified live on 2026-11-11). `catalog.githubVerifyTls` is an explicit TLS-verify override for that one fetch — the default is verify-on, and `false` is for a MITM egress proxy with an untrusted certificate.
- **Descriptions stay short; notes stay local.** Descriptions are capped at 400 characters (test-enforced) because MLIS shows them in a side panel; the long-form operational reasoning lives in the catalog-only `notes` field — searchable in the UI, never pushed to MLIS.
- **Metric fields are validated.** `eval.*` blocks must be finite numbers in 0..100 (`catalog.py`, `validate_eval`) — a garbage metric is rejected at the API instead of rendered.

## Provenance manifests

After a successful PVC download the Job writes `manifest.json` next to the cache directory: the repo, the **resolved** revision commit, per-file sha256 + size (LFS files are cross-checked against Hub metadata when reachable), the license, the download time, the job id, the namespace, and a free-text `submitted_by` (empty today; identity arrives later). `provenance.revision` (default `"main"`) pins the download to a revision — a tag or commit sha gives reproducible re-downloads.

**A cache directory without a record of how it got there is unmanageable the moment more than one person shares the PVC.** The manifest is the join key for the inventory, the GC pass and manual deletion; `provenance.enabled: false` omits the step entirely and downloads exactly as before.

## The ModelScan gate

- Every PVC download is scanned **inside the downloader Job** by modelscan over the unsafe serialization formats (`.bin`/`.pt`/`.pth`/`.ckpt` plus pickle-class files); safetensors-only snapshots are skipped by modelscan itself.
- The verdict lands in `manifest.json`'s `scan` block (`verdict`/`mode`/`threshold`/`detail`) and surfaces as a per-model column in the Downloaded-models and GC views.
- `gate.mode: warn` (default) records the verdict and proceeds; `gate.mode: block` makes the Job exit 1 so the failure appears verbatim in the Jobs table's Error column. **A broken scanner is always warn-only** — the gate must never fail a download because the scanner itself failed. `gate.threshold` (default `low`) downgrades findings at or below it to clean.
- The `hf-downloader` image must ship modelscan for the gate to judge anything; on an image without it the gate records an error verdict instead of pretending to have scanned.

## Preflight and quotas

- **The gate runs before any Job is created.** Two checks: (a) disk fit — the Hub-reported model size versus the PVC's `status.capacity` minus used bytes (a `du -sb` pass in the scanner Job); (b) the namespace's byte quota (`quota.namespaces.<ns>` plus `quota.default`; values accept `500Gi`-style quantities).
- `preflight.mode: refuse` (default) returns a 422 that names the exact bytes-needed vs bytes-free / used-vs-quota math in the submit error path; `warn` reports without refusing; `off` skips. `quota.mode` is **independent of `preflight.mode`** — a site can warn on disk-fit and still hard-refuse on quota.
- **An unknown model size degrades to warn-only.** Refusing a submission because an estimate was unavailable would block legitimate work; the degradation is stated in the message rather than being silent.
- Caching and headroom: `preflight.usageTtl` (120 s) caches the used-bytes census, `preflight.sizeTtl` (300 s) caches Hub size estimates, `preflight.safetyMargin` (default 0) subtracts headroom for writes the `du` snapshot cannot see yet, and in-flight submissions are charged between scans.
- `preflight.enabled: false` also drops the read-only `persistentvolumeclaims` get/list RBAC addition (`helm/templates/rbac.yaml`) — the permission exists to serve the check and disappears with it.

## Storage GC (TTL eviction)

- Opt-in (`gc.enabled`, default false). Renders a nightly CronJob (`gc.schedule`, default `0 3 * * *`) plus the UI's **Storage GC (dry-run report)** section.
- **The deletion rule is a three-key AND — all three must hold:** a `manifest.json` is present next to the `models--*` directory; **no** AIOLI `packaged_models` row references the model (matched by cache-dir name or repo id inside the uri — never exact path equality, because custom cache roots exist); and **no** live managed Job pins the cache root. Each signal alone is unreliable — Job TTLs erase evidence, paths vary, MLIS rows go stale — hence the AND (`gc.py`).
- `gc.dryRun: true` is the default: the CronJob evaluates and logs every verdict and deletes nothing. The `/api/gc/report` view (cached 120 s; `?force=1` rebuilds) is the review surface — flip `dryRun` only after reading it.
- `gc.ttlDays` (30) is the age line, `gc.minKeep` an LRU floor, `gc.protectedModels` a glob list that is never deleted, `gc.concurrencyPolicy: Forbid` keeps two GC pods from ever running at once.
- The GC Job's AIOLI access is a **strictly read-only SELECT** (`default_transaction_read_only=on`, `db.py`). S3 eviction is out of scope; GC is a PVC-lifecycle tool.
- **Known limitation, kept visible on purpose:** the GC CronJob pod runs the scanner image and sets no `serviceAccountName`, so its live-Job and AIOLI cross-checks hit their fail-safe and **keep everything** — with `gc.dryRun: false` the CronJob currently deletes nothing. Verify from inside the cluster with `kubectl -n <ns> create job --from=cronjob/<release>-gc gc-probe` then `kubectl -n <ns> logs job/gc-probe` (`JOBS: unavailable ...` confirms it). The fix — the app image plus an explicit service account — is chart work, tracked as a defect, not a documentation fix (background: [manual-delete-design.md §17](manual-delete-design.md)).

## Admin manual deletion

Opt-in (`manualDelete.enabled`, default **off**): an SSO-gated page (`/admin/delete`, the third header tab) that lists model cache directories on the PVC with size, age and verdict, and deletes the ones an admin selects — the on-demand counterpart to the scheduled GC pass. When disabled, the route is not registered (the page 404s) and `/api/admin/*` return 400.

- **Two independent gates: auth (who may delete) and verdict (may this be deleted).** Being an admin makes a request admissible; it does not make a directory safe to delete.
- **Hard blocks, never overridable:** a live managed Job pinning the cache root (deleting under an in-flight download corrupts it), a `gc.protectedModels` glob, and any structural failure — outside the scan root, a symlink, not a directory, a basename that is not `models--<org>--<repo>`. **Soft block, overridable per request:** an AIOLI `packaged_models` row — stale rows must not make weights undeletable forever, so the override is a typed, per-request acknowledgement recorded as `override: true` in the tombstone. There is deliberately no values knob that disables a protection wholesale.
- **The isolation rule:** the destructive Job acts on exactly the directories the preview resolved, nothing else. The preview intersects the selection with the live scan output (exact string equality; a path absent from the current scan is refused with 422), pins the resolved set server-side under a **single-use `verdict_id`** (`manualDelete.verdictTtlSeconds`, 900 s — sized above the 300 s scanner timeout; expiry is a 409), and the app **re-runs the full verdict at commit time**, immediately before creating the Job, against a fresh scan, a fresh AIOLI read and a fresh live-Job list. What the operator reviewed is what dies; anything that became blocked or vanished is dropped and reported.
- **The delete Job is deliberately as dumb as a download Job.** It runs in the namespace that *owns* `models-pvc` (PVCs are namespace-scoped — creating it in the app's namespace left pods Pending forever on `persistentvolumeclaim not found`), with the default ServiceAccount and no cluster RBAC, mounts the PVC read-write, and makes no Kubernetes API or database calls. That is exactly why the protection runs in the app at commit time.
- **In-pod, structural checks only, abort-all before any delete:** every target must be absolute, not a symlink, a directory, carry the `models--<org>--<repo>` basename, and `realpath` under the scan root; any failure aborts the whole run. Each directory is renamed to `.deleting-<name>` before removal — the model leaves the listing the moment deletion starts, and a killed run leaves obvious junk instead of a half-deleted model the scanner would still present as complete.
- **Every deletion leaves a tombstone:** a JSON line on stdout (survives in the Job logs for `manualDelete.jobTtlSeconds`, 86400 — the audit surface) plus a best-effort `<scan-root>/_deleted/<ts>-<cachedir>-<job>.json` carrying a copy of the model's `manifest.json`. The actor is the verified SSO username, or `shared-key:<fingerprint>` in secret mode.
- **huggingface_hub lock cleanup:** the Job also removes `<cache-root>/.locks/<cache-id>/`, which `huggingface_hub` leaves behind under the same `models--<org>--<repo>` name — otherwise a deleted model resurfaces as a 0-byte phantom forever. The scanner and the GC walk both skip `.locks/`, so the halves agree.
- **Caps:** `manualDelete.maxTargets` (20) per request; `manualDelete.jobDeadlineSeconds` (3600) exists to catch a pod that never schedules, **not** to bound the deletion — killing a large `rmtree` mid-flight is worse than waiting.
- **Auth:** `manualDelete.auth.mode: sso` (default) reads the identity oauth2-proxy injects. `adminGroups` defaults to `[admin]` — PCAI's own admin group: the EzUA policy server grants admin on `"admin" in input.groups`, and oauth2-proxy forwards the Keycloak `groups` claim — so **every AIE Admin is an admin here with no per-person values edit**. `admins` adds named principals (union, not intersection); both empty means nobody. `mode: secret` is the fallback for installs with no SSO gate: an **existing** `model-delete-key` Secret the chart never creates, with fingerprint-only attribution. Both modes fail closed.
- Verified on the SE G2 cluster: a four-model, ~530 GiB deletion completed in 32 seconds with a tombstone per directory (design note §18).
- Full design — threat model, HTTP surface, the fail-closed matrix, rejected alternatives: [manual-delete-design.md](manual-delete-design.md).

## Verified SSO identity for the admin gate

- `oidc.enabled` (default true) means the admin gate requires a **verified OIDC bearer token** — the `Authorization: Bearer <id_token>` oauth2-proxy injects — instead of trusting the identity header, which any pod that can reach the Service could forge. A bare header is refused whenever an IdP is configured.
- The verifier is a **verbatim vendored copy** of the fleet's `pcai_utils/oidc_identity.py` (RS256/JWKS — RSA-signed JWTs checked against the identity provider's published signing keys — with issuer/audience/expiry validation, fail-closed, and no token material logged). It is vendored rather than hardlinked because this repo is not a `pcai_utils` mesh consumer. `tests/test_oidc_vendored.py` SHA-256-guards the copy: edit upstream, re-copy, bump the recorded hash — never patch it here.
- `oidc.issuer` is **claim-compared only** — it must equal the token's `iss` and is never fetched. `oidc.jwksUrl` *is* fetched: on PCAI point it at the in-cluster Keycloak headless service over plain HTTP, so the fetch bypasses the egress proxy and the platform-CA TLS gap. An empty issuer leaves the verifier inert and the gate falls back to the header — **weaker by construction; set the issuer on any shared cluster**.
- The chart maps `oidc.*` onto the fleet's canonical `OIDC_*` env names (`helm/templates/deployment.yaml`) so the module stays byte-identical to the fleet copy; the pre-rename `RAG_OIDC_*` names still read as a deprecated alias, and the shared module's `PCAI_OIDC_ENV_PREFIX` mechanism exists for apps that need namespacing — this app uses the canonical names. The `cryptography` dependency is imported lazily; without it the gate fails closed.

## Debug pods

- A one-click long-running shell (`md-debug-<suffix>`) mounting the same PVC at `/mnt` with `HUGGINGFACE_HUB_CACHE` preset, optional HF token (`hf-token-debug-<suffix>`, deleted together with the Job); the UI prints the ready-made `kubectl exec` command.
- **It is a Job on purpose, not a bare Pod.** The kube-system job-controller must be the pod's creator: a pod created by the app's ServiceAccount while carrying the `hpe-ezua/app: mlis` label is denied by the platform-wide `protect-models-pvc` Kyverno policy (see [DEPLOYMENT.md](DEPLOYMENT.md)). Do not "fix" an admission denial by switching it to a bare Pod — that will always be denied.
- Deleting a debug job from the UI also deletes its token Secret. `debugPod.ttlSecondsAfterFinished` (86400) applies once the Job finishes — it normally runs `tail -f /dev/null` and never completes on its own. Scheduling follows `downloader.nodeSelector` (there is no separate `debugPod.nodeSelector`).

## The web UI

- **Three header tabs:** Download (`/`), Model Catalog (`/catalog`) and Manual Deletion (`/admin/delete`, rendered only when `manualDelete.enabled` — the route 404s when off, so an unconditional tab would be a dead link). Every page is titled Model Manager.
- **Download tab:** searchable namespace dropdown filtered to `project-user-*` (`/api/namespaces`; an RBAC denial degrades to typing a namespace manually), model name, HF token, optional custom cache location, chat-template preset or custom contents, the HuggingFace / NVIDIA NIM source selector (when NIM is available), and the backend choice when both backends are configured.
- **Jobs table:** status, output URL, submitted/finished timestamps, live logs and progress parsed from pod logs, and delete for finished jobs. Preflight refusals show `Submission refused: ...` with the exact bytes math (422); warn-mode decisions appear as a `— warning: ...` suffix on the success message; a gate-mode-block scan failure surfaces verbatim in the Error column.
- **Downloaded models table:** one row per model with the on-disk checkpoint size from the scanner's `du` (`—` when unknown, e.g. job-history or S3-only rows); **Rescan storage** forces a fresh scan; the status line states which automatic scans are enabled.
- **Storage GC (dry-run report)** (when `gc.enabled`): one row per cache directory with size, age, manifest presence, scan verdict and the three-key verdict (`delete`/`keep` + reason). Read-only — the CronJob does the actual (still dry-run-by-default) deletion. **Rebuild GC report** forces a fresh evaluation.
- **NIM profile mirrors** table: mirror history with the exact `NIM_REPOSITORY_OVERRIDE=s3://<bucket>` to set on the NIM deployment.
- Dark/light theme; the UI is server-rendered Jinja2 templates plus static JS served by the same FastAPI app — there is no separate frontend build or Node toolchain.

## PCAI integration mechanics

- **Ingress:** an Istio VirtualService (an Istio routing object) on `istio-system/ezaf-gateway` serving `model-manager.${DOMAIN_NAME}`, plus a `CUSTOM` AuthorizationPolicy in `istio-system` delegating to the `oauth2-proxy` provider. PCAI substitutes the domain placeholder before rendering — leave it as-is in values.
- **Discovery labels:** a pre-install Kyverno ClusterPolicy (`add-vendor-app-labels-<release>-<chart>`) stamps `hpe-ezua/type: app-service-core` and `hpe-ezua/app: <chart name>` onto the release's Deployments and Services so EZUA app discovery picks the app up. Keep `kyverno.enabled: true` on PCAI; disabling it stops the stamping.
- **Job admission:** the platform-wide Kyverno policy `protect-models-pvc` **denies any pod mounting the shared `models-pvc` unless it carries the MLIS authorization label `hpe-ezua/app: mlis`**. Every Job this chart submits — downloader, NIM, debug, scanner, delete, GC — carries that label plus the `hpe-ezua/disable-sc` annotation (the platform security-context opt-out that lets the pod run as root so the root-owned shared PVC is traversable), and is admitted through the job-controller path. The prerequisite detail and the hosted-trial patch are in [DEPLOYMENT.md](DEPLOYMENT.md).
- **RBAC:** a ClusterRole bound to the app ServiceAccount lets the service create/get/list/watch/delete Jobs and create/get/list/delete Secrets cluster-wide (downloads are submitted into the user's namespace), list namespaces (the dropdown), and read pods + pod logs (progress and log streaming); `persistentvolumeclaims` get/list is added only when preflight is enabled. The debug/scan/delete Jobs themselves run with the default ServiceAccount and need none of it.
- **Proxy wiring:** `proxy.http` / `proxy.https` / `proxy.noProxy` are injected per-key into downloader Jobs, debug pods and the app pod — **each key renders only when non-empty**; a direct-egress site leaves `proxy: {}` and nothing renders. `noProxy` must keep cluster-internal traffic (MinIO, the AIOLI database, the Kubernetes API, Keycloak) off the corporate proxy.
- **Single replica by design:** the download queue, the GC report cache and the manual-delete verdict pins live in process memory (`deployment.yaml`, `replicas: 1`). Scaling out requires moving those to a shared store first.

## Runtime and dependencies

- Base image `python:3.14-slim` — the fleet base; the app code is 3.14-clean and the test suite runs on 3.14.
- `boto3`/`botocore`/`s3transfer` are **version-locked** (`boto3==1.43.71`, `botocore==1.43.71`, `s3transfer==0.19.2`): an open `boto3` range sent pip's resolver into an unbounded backtrack that hung image builds. Bump all three together, to versions with cp314 wheels.
- `psycopg2-binary` provides the AIOLI/MLIS database access; `cryptography` backs the RS256 JWT verification and is imported lazily so its absence fails closed instead of crash-looping the app.
- The app image ships the delete module (`app.delete_targets`) and the Kubernetes client — which is why `manualDelete.image` defaults to the app image and the delete Job must never use the scanner image.
- Tests: `PYTHONPATH=src python3 -m pytest tests/ -q` (render tests need `helm` on PATH) — the per-file map is in [tests/README.md](../tests/README.md).
