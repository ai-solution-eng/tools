# HPE Model Downloader

Web frontend + queueing service that downloads Hugging Face models on-demand in
Kubernetes. A FastAPI app submits per-model k8s `Job`s (running the
`hf-downloader` image) that cache a snapshot into either:

- a **shared model PVC** (default, e.g. `models-pvc` / `/mnt/models`), or
- an **S3-compatible** bucket/path.

It also keeps a **model catalog** (grouped by GPU tier) and can **push model
configs into MLIS** (the AIOLI `packaged_models` table). An optional **chat
template** (Jinja) can be written into the model directory with each download.

```
┌──────────┐  POST /api/jobs   ┌──────────────┐   create   ┌──────────────────────┐
│  Browser │ ───────────────► │  FastAPI app │ ─────────► │ Downloader Job (k8s) │
│   (UI)   │ ◄─────────────── │  (queue)     │ ◄───────── │  hf-downloader image │
└──────────┘   /api/jobs/*    └──────────────┘   logs     └──────────┬───────────┘
                                                                     │ snapshot
                                                       ┌─────────────┴─────────────┐
                                                       │ PVC: /mnt/models          │
                                                       │ S3:  s3://bucket/prefix/… │
                                                       └───────────────────────────┘
```

## Architecture

| Component | What it does |
|---|---|
| `app/` (FastAPI) | UI + REST API, in-process download queue (bounded concurrency), reads a Job-template ConfigMap, creates Jobs + HF-token Secrets in the requested namespace, reads pod logs for progress. |
| `downloader/` | `hf-downloader` image: `snapshot_download()` → writes to a PVC or pushes to S3. |
| `helm/` | Chart installing the app (Deployment + Service + RBAC + catalog PVC (+ ezua ingress / kyverno policy on PCAI). |

Queue state is **in-process**: after an app restart the UI only shows Jobs that
still exist in k8s (TTL-cleaned by `ttlSecondsAfterFinished`, 1h default).

## Table of contents

- [Prerequisites](#prerequisites)
- [Quick install — SE G2 (PCAI) cluster](#quick-install--se-g2-pcai-cluster)
- [Quick install — any other cluster](#quick-install--any-other-cluster)
- [One flag: `hpe_proxies`](#one-flag-hpe_proxies)
- [Storage backends](#storage-backends)
  - [Model PVC (default)](#model-pvc-default)
  - [S3 path](#s3-path)
- [Chat template files (optional)](#chat-template-files-optional)
- [Configuration reference](#configuration-reference)
- [Hosted trial notes — Kyverno / mounting the model PVC](#hosted-trial-notes--kyverno--mounting-the-model-pvc)
- [Rebuild & re-deploy](#rebuild--re-deploy)
- [Troubleshooting](#troubleshooting)

## Prerequisites

- A Kubernetes cluster (>= 1.26) with Helm.
- A **models PVC** mounted at `/mnt/models` (`large-models/` subpath) present in
  the namespace where download Jobs run — this is the default target.
  - HPE PCAI clusters ship a shared `models-pvc` per project namespace (RWX,
    `gl4f-filesystem`). See “Hosted trial notes” below.
- The app’s ServiceAccount needs cluster-wide create/delete for `jobs` and
  `secrets` (see `helm/templates/rbac.yaml`). Keep the default unless you
  restrict Jobs to a fixed set of namespaces.
- Optionally: a catalog PVC (tiny, default 100 Mi) and, to use *Push to MLIS*,
  network access + DB credentials for the AIOLI database.

## Quick install — SE G2 (PCAI) cluster

Everything is on by default: `hpe_proxies: true` for the corporate
`http(s)_proxy` + `no_proxy` env on downloader Jobs (with the httpx-TLS
verification bypass for the Zscaler MITM proxy), plus the kyverno vendor-app
label ClusterPolicy and the ezua Istio ingress, which default to enabled since
this is an HPE PCAI app.

```bash
# 1. build & push images first (see "Rebuild & re-deploy")
docker build -t andrewbydlon/pcai-model-downloader:0.5.0 . && docker push andrewbydlon/pcai-model-downloader:0.5.0
docker build -t andrewbydlon/hf-downloader:v4.0 downloader/ && docker push andrewbydlon/hf-downloader:v4.0

# 2. install
DOMAIN_NAME=pcai.rdlabs.hpecorp.net
helm install model-downloader helm/ \
  --namespace model-downloader --create-namespace \
  --set ezua.domainName="${DOMAIN_NAME}" \
  --set ezua.virtualService.endpoint="model-downloader.${DOMAIN_NAME}"

# 3. UI
kubectl port-forward -n model-downloader svc/model-downloader 8000:8000
open http://localhost:8000
```

On SE G2 the chart also renders:

- a **kyverno ClusterPolicy** (`add-vendor-app-labels-<release>-<chart>`) that
  stamps `hpe-ezua/app`, `hpe-ezua/type` labels on the app's Deployment/Service
  so the HPE UI groups it correctly, and
- the **ezua** `VirtualService` + `AuthorizationPolicy` (oauth2-proxy) exposing
  the UI. See [Hosted trial notes](#hosted-trial-notes--kyverno--mounting-the-model-pvc).

## Quick install — any other cluster

This is still an HPE PCAI app, so kyverno and the ezua ingress are installed by
default. For a generic cluster, disable the proxies **and** the PCAI resources
explicitly:

```bash
helm install model-downloader helm/ \
  --namespace model-downloader --create-namespace \
  --set hpe_proxies=false \
  --set kyverno.enabled=false \
  --set ezua.enabled=false
```

## One flag: `hpe_proxies`

`hpe_proxies` controls **only the corporate proxy**: the `http(s)_proxy` /
`no_proxy` env on downloader Jobs and the httpx TLS-verification bypass needed
for the Zscaler MITM. It does not control kyverno or ezua — those are separate
switches that default to **on** (this is an HPE PCAI app).

| value | default | proxy env | httpx verify |
|---|---|---|---|
| `hpe_proxies` | `true` | ✅ | bypassed (Zscaler) |
| `hpe_proxies=false` | — | ❌ | intact |

| switch | default | meaning |
|---|---|---|
| `kyverno.enabled` | `true` | install the `add-vendor-app-labels` ClusterPolicy (pre-install hook) |
| `ezua.enabled` | `true` | Istio `VirtualService` + `AuthorizationPolicy` (oauth2-proxy) ingress |

```bash
# PCAI default — only the domain needs filling in
helm upgrade model ... --set ezua.domainName=...

# generic cluster — opt out of the PCAI-only bits explicitly
helm upgrade model ... --set hpe_proxies=false --set kyverno.enabled=false --set ezua.enabled=false

# fine-grained: keep the ingress but drop the corporate proxy
helm upgrade model ... --set hpe_proxies=false --set ezua.domainName=...
```

## Storage backends

Users pick PVC or S3 per submission in the UI form. Defaults:

```
storage:
  backend: both     # pvc | s3 | both — which options the user sees
  default: pvc      # preselected option
```

### Model PVC (default)

Downloads write HF cache under `pvc://<pvcName>/<pvcSubpath>/<model>?containerPath=/mnt/models`
(a path MLIS/AIOLI consumes). The Job mounts `downloader.pvcName` (default
`models-pvc`), subpath `large-models/`.

When a job succeeds the UI shows the `pvc://` destination and a copy button.

### S3 path

If `storage.backend` includes `s3`, the downloader Job **streams the snapshot
directly to the S3-compatible store** (MinIO): each repo file is downloaded and
uploaded one at a time, 8 files in parallel, so only a handful of in-flight
files ever sit on local scratch.

The destination is **chosen per submission** — the UI is not tied to one bucket.
When *S3 path* is selected, a text field appears prefilled with the configured
default (`s3://<bucket>/<prefix>/`); type any `s3://bucket/prefix/` the
credentials can reach. Objects land at:

```
s3://<bucket>/<prefix>/<org>/<Repo-Name>/<repo file path>
```

`endpointUrl` is the MinIO server; the bucket must already exist. Credentials
are set via `s3.accessKeyId` + `s3.secretAccessKey` (required for MinIO). The
`bucket` / `prefix` values are only used as the form's prefill default (and as a
fallback for Jobs created before per-submission destinations existed).

```yaml
s3:
  endpointUrl: "http://minio.minio.svc.cluster.local:9000"  # where boto3 connects (required)
  bucket: "mlis-models"            # default shown in the UI (prefill only)
  prefix: "large-models"           # default shown in the UI (prefill only)
  accessKeyId: "admin-io"          # required for MinIO
  secretAccessKey: "MinIO$2K"      # required for MinIO

# small scratch volume used while downloading (download → upload → free)
downloader:
  s3WorkPvcName: ""           # empty → emptyDir
  s3WorkSize: 20Gi
  s3WorkPath: /mnt/s3work
```

> `s3WorkSize` (or `s3WorkPvcName`) only needs to hold a few in-flight files —
> files are streamed one-by-one to S3, 8 in parallel, so it no longer needs to
> fit the whole model.

## Chat template files (optional)

Some serving stacks need a chat template file next to the model (e.g. vLLM's
`--chat-template /mnt/models/chat_template.jinja`). The downloader can write one
after the snapshot completes:

- tick **Write chat template file** in the submit form,
- set the **path** — a relative path resolves under the model's cache dir (e.g.
  `chat_template.jinja` → `/mnt/large-models/<org>/<model>/chat_template.jinja`),
  an absolute path is used as-is, and
- paste the **contents** in the multiline box.

Catalog entries can carry `chat_template_path` + `chat_template_contents`; the
seeded Gemma (`chat_template.jinja`) and Qwen3-VL-Reranker
(`templates/qwen3_vl_reranker.jinja`) entries ship with them. Rows that have a
template show a **Template** button that prefills the whole form — model name,
path, contents, and the checkbox. Template files apply to the PVC backend only;
the S3 job ignores them.

## Configuration reference

### Application

| value | default | meaning |
|---|---|---|
| `image.repository` / `image.tag` | `andrewbydlon/pcai-model-downloader:0.5.0` | app image |
| `maxConcurrency` | `4` | concurrent download Jobs; rest are queued in-process |
| `hpe_proxies` | `true` | HPE PCAI SE G2 proxy/kyverno/ezua features vs `false` for a generic cluster |

### Downloader job

| value | default | meaning |
|---|---|---|
| `downloader.image.repository`/`tag` | `andrewbydlon/hf-downloader:v4.0` | worker image |
| `downloader.pvcName` | `models-pvc` | PVC used for the PVC backend |
| `downloader.backoffLimit` | `2` | Job retries on failure; the HF cache persists on the PVC so retries resume the partial snapshot (`0` disables) |
| `downloader.securityContext` | `{runAsUser:0, runAsGroup:0}` | write permission on the shared PV |
| `downloader.disableSecurityContext` | `true` | adds `hpe-ezua/disable-sc: "true"` pod annotation (see Kyverno section) |
| `downloader.hf.*` | — | `HF_HUB_*` knobs: timeouts, hf_transfer, Xet |
| `downloader.resources` | 2/4 CPU · 4/8 Gi | job resource requests/limits |

### HPE features (only used with `hpe_proxies=true`)

| value | default | meaning |
|---|---|---|
| `pcai.httpsProxy` | `http://hpeproxy.its.hpecorp.net:8080` | corporate proxy for HF downloads |
| `pcai.noProxy` | big local list | no_proxy entries (cluster + internal hosts) |
| `kyverno.enabled` | `true` | render the vendor-app-labels ClusterPolicy (disable outside HPE PCAI) |
| `ezua.enabled` | `true` | render the Istio ingress + AuthorizationPolicy (disable outside HPE PCAI) |
| `ezua.domainName` | `"${DOMAIN_NAME}"` | Istio gateway host for the UI |
| `ezua.virtualService.istioGateway` | `istio-system/ezaf-gateway` | gateway |

### Catalog / AIOLI

| value | default | meaning |
|---|---|---|
| `catalog.enabled` / `catalog.size` | `true` / `100Mi` | small PVC storing the catalog JSON |
| `aioli.dbHost` | `aioli-db-service-hpe-mlis.mlis.svc.cluster.local` | postgres host for Push to MLIS |
| `aioli.dbPasswordSecret.*` | `aioli-db-password` in `mlis` | where the app reads the DB pwd (ClusterRole already grants it) |

### App storage mapping

| value | default | meaning |
|---|---|---|
| `storage.backend` | `both` | `pvc` \| `s3` \| `both` |
| `storage.default` | `pvc` | preselected in the UI form |
| `s3.bucket` / `s3.prefix` | `mlis-models` / `large-models` | prefill default for the per-submission S3 destination input |

## Hosted trial notes — Kyverno / mounting the model PVC

On PCAI SE G2 the shared `models-pvc` is a Glade NFS (VASTdata / `gl4f-filesystem`) PV
mounted at `/mnt/models` in every project namespace. The downloader Job must:

- run as **root** (`runAsUser: 0`) to write into that shared volume, and
- be able to mount that block device via the CSI driver.

The platform has a Kyverno mutating webhook
(`user-namespace-pods-security-context-policy`) that **overwrites** pod
`securityContext` (`fsGroup`/`runAsUser`) with the *user* UID from
`user-info-cm` — this silently breaks downloader pods that try to mount the
shared model PVC (the container suddenly runs as a non-root user and can’t
write to the usually root-owned mount).

**That is why a change was historically required to get model-downloader
running in a hosted/trial namespace.** The fix is to opt the downloader pod out
of that mutation with the annotation:

```
hpe-ezua/disable-sc: "true"
```

> If you deploy into a hosted trial and the download Jobs fail with pod
> creation / mount errors (e.g. `FailedScheduling` “PersistentVolumeClaim is not
> bound”, or `create pod … is forbidden` from the admission webhook), first check
> that `downloader.disableSecurityContext` is `true`. That handles the
> security-context mutation below; a second, separate hosted-trial problem is the
> `protect-models-pvc` policy covered next.

### `protect-models-pvc` policy — allow the downloader's pods to mount it

This was a separate issue observed in the hosted trial, in addition to the
security-context mutation above: the app could **not** connect to `models-pvc`
because of the platformwide `protect-models-pvc` Kyverno ClusterPolicy, which
**denies** creation of pods that mount the shared model volume. The downloader’s
Job pods are actually created by the Kubernetes **job-controller**
(`system:serviceaccount:kube-system:job-controller`), not by the
model-downloader ServiceAccount, so they hit the deny rule.

The fix is to add an exception for the job-controller's username to the deny
conditions of that policy (`rules[1]`):

```bash
kubectl patch clusterpolicy protect-models-pvc --type json -p='[
  {"op": "add", "path": "/spec/rules/1/validate/deny/conditions/all/-",
   "value": {"key": "{{request.userInfo.username}}", "operator": "NotEquals", "value": "system:serviceaccount:kube-system:job-controller"}}
]'
```

After applying, downloader Job pods created by the job-controller are no longer
denied and the Job can mount `models-pvc`. It may take a few seconds for the
admission webhook to pick up the updated policy.

Other Kyverno quirks worth knowing from this cluster:

- **ClusterPolicy name length**: the chart’s policy is
  `add-vendor-app-labels-<release>-<chart>`. With long release + chart names this
  can exceed the k8s 63-char label limit. Keep release/chart names ≤ ~30 chars
  each or shorten the policy name.
- Reports controller (`BackgroundScanReport`) re-scans hourly, so a brand new
  release can show `unknown` for up to ~1 h in the HPE platform UI. Not a bug.
- The `hpe-ezua/disable-sc` annotation also makes Kyverno skip the generic
  `mandatory-security-context-check-policy` check for `csi.ezdata.io` volumes.

## Rebuild & re-deploy

```bash
# app image
docker build -t andrewbydlon/pcai-model-downloader:0.5.0 .
docker push andrewbydlon/pcai-model-downloader:0.5.0

# worker (only when S3 support / boto3 changed)
docker build -t andrewbydlon/hf-downloader:v4.0 downloader/
docker push andrewbydlon/hf-downloader:v4.0

helm upgrade model-downloader helm/ -n model-downloader \
  --set image.tag=0.5.0 \
  --set ezua.domainName="${DOMAIN_NAME}"
```

> Chart changes other than the image tag roll the Deployment via `helm upgrade`:
> it re-renders the ConfigMap (job template) with new env, so the pod is
> recreated. If unsure do `kubectl rollout restart deploy/model-downloader -n
> model-downloader`.

## Troubleshooting

- **Job fails to be created** → check RBAC (`create jobs` across namespaces) and
  uniqueness/length of the job name (`md-<org>-<model>` truncated to 63).
- **"Config-missing key / missing `job.yaml`"** → the app reads the
  `-job-template` Configmap in its namespace; make sure chart applied.
- **PVC mount errors on a hosted cluster** → see the Kyverno section.
- **Slow/parallel**: raised concurrency hurts; `downloader.hf.enableHfTransfer`
  and `disableXet` reshape transfer behaviour in a proxy network.
- **Transient network / DNS failures** (e.g. `BackoffLimitExceeded` after
  `ConnectError [Errno -5] No address associated with hostname`) → Jobs retry by
  default (`downloader.backoffLimit`, default `2`, with `restartPolicy:
  OnFailure`). The HF cache lives on the PVC, so a retry resumes the partial
  snapshot instead of re-downloading. Set `downloader.backoffLimit=0` to disable.
- **S3 jobs hang after download**: confirm the bucket the user entered exists
  and that the pod can reach the endpoint (add the S3 endpoint to `no_proxy` if
  needed).