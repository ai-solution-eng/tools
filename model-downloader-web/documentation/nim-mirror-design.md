# NIM profile mirror — design and operations

Status: implemented in chart/app 2.1.0. Source: NGC NIM 2.x model registry.
Target: an S3-compatible bucket that a NIM deployment reads through
`NIM_REPOSITORY_OVERRIDE`.

## Why this exists

The downloader already pulls HuggingFace snapshots into the shared `models-pvc`
or an S3 prefix. NVIDIA NIM deployments do not consume either layout: NIM reads
model **profiles** from NGC, or from an object store whose keys are
percent-encoded exactly as NIM expects when `NIM_REPOSITORY_OVERRIDE` points at
it. For an air-gapped or proxied PCAI cluster, those profiles must be staged
ahead of time.

NVIDIA ships the encoder in the NIM image itself: `mirror s3` downloads the
profiles and uploads them with the correct keys. This feature runs that command
as a Kubernetes Job from the same UI, with a per-submission NGC API key, so an
operator never needs a workstation with Docker, NGC credentials and bucket
credentials to seed the bucket.

## What the Job does

1. The app creates two short-lived Secrets in the target namespace:
   - `ngc-api-<image>-<suffix>` — generic Secret, key `NGC_API_KEY`.
   - `ngc-pull-<image>-<suffix>` — `kubernetes.io/dockerconfigjson` for
     `nvcr.io` (username `$oauthtoken`, password = the NGC key), created only
     when the image ref starts with `nvcr.io/`.
2. The Job runs the NIM image with:
       mirror s3 -b <bucket> --manifest-file /opt/nim/etc/default/model_manifest.yaml \
         --model-cache-path /opt/nim/.cache [--profiles <id> ...]
   The local NIM cache is an `emptyDir` at `/opt/nim/.cache` (or
   `nim.workPvcName` when set), so only the in-flight profile is on node disk.
3. Both Secrets are deleted when the Job finishes. If the app restarts while
   the Job is still present, the reconciler reads the Secret names from the Job
   annotations and cleans up too (a Job that has already TTL'd away before the
   restart leaves its Secrets behind — keep `nim.ttlSecondsAfterFinished`
   comfortably longer than an app restart).
4. The Jobs table shows the image, status, logs and the exact
   `NIM_REPOSITORY_OVERRIDE` value to set on the NIM deployment
   (`s3://<bucket>`). The **NIM profile mirrors** table lists the NIM job
   history.

The Job does **not** request a GPU: NVIDIA's own AWS mirror example runs
`mirror s3` without `--gpus all`, and this feature keeps the app's
"downloads without holding GPUs" property.

## Prerequisites

- **NIM 2.x image.** The `mirror` action is documented for NIM 2.x; a NIM 1.x
  image does not implement it (expect an unsupported-action / usage error at
  startup). Use the NIM 2.x image for the model from NVIDIA's support matrix.
- **Image reachability.** Either the cluster can pull from `nvcr.io` (the
  per-submission pull Secret handles auth), or the image is mirrored into a
  local registry and `nim.imagePullSecrets` names that registry's Secret.
- **NGC API key** with access to the NIM model. Use a personal API key
  (`nvapi-...`), the form NVIDIA documents for `NGC_API_KEY`.
- **S3 configuration.** The chart's existing `s3.endpointUrl`, `s3.bucket`,
  `s3.accessKeyId` and `s3.secretAccessKey` are reused. `nim.bucket`
  defaults to `s3.bucket`; set it when NIM profiles live in a dedicated
  bucket. The mirror writes at the **bucket root** (the CLI takes a bucket name,
  not a prefix), so a dedicated bucket is the clean choice.
- **Serving side.** The NIM deployment needs the same S3 endpoint/credentials
  and `NIM_REPOSITORY_OVERRIDE=s3://<bucket>` (no prefix). For MinIO/Ceph it
  also needs `AWS_S3_USE_PATH_STYLE=true` and a region.

## Choosing profiles

A NIM image can carry several profiles (GPU model/count, precision, engine).
Leaving **Profiles** blank mirrors the image's default profile. To pin the
profiles your serving GPUs need, pass one or more profile IDs, separated by
spaces or commas. Get the IDs from:

- the NGC model page for the image (Model Profiles), or
- `docker run --rm <image> list-model-profiles` on a machine with the target
  GPU (the command reports profiles for the current hardware).

The IDs are opaque hashes; the app validates their character set and appends
them as one `--profiles` list. Up to 50 IDs per submission.

## Values

`nim.*` in `helm/values.yaml`:

| Key | Default | Effect |
|---|---|---|
| `nim.enabled` | `true` | Ships the Job template and the UI source. A PVC-only backend also disables NIM. |
| `nim.bucket` | `` (empty) | Destination bucket; empty = `s3.bucket`. Profiles land at the bucket root. |
| `nim.region` | `us-east-1` | `AWS_REGION` / `AWS_DEFAULT_REGION` for the in-container SDK. |
| `nim.pathStyle` | `true` | `AWS_S3_USE_PATH_STYLE`; MinIO/Ceph need true, AWS S3 false. |
| `nim.manifestPath` | `/opt/nim/etc/default/model_manifest.yaml` | The image's built-in manifest passed to `-m`. |
| `nim.workPvcName` / `nim.workSize` | `` / `100Gi` | NIM cache scratch: existing PVC, else `emptyDir` with that size limit. |
| `nim.imagePullSecrets` | `[]` | Extra pull secrets for a private/local registry. |
| `nim.disableSecurityContext` | `true` | `hpe-ezua/disable-sc` annotation (PCAI). |
| `nim.securityContext` | root/root | Container security context (the NIM cache must be writable). |
| `nim.resources` | 2/4Gi req, 4/8Gi lim | No GPU request. |
| `nim.nodeSelector` | `{}` | Scheduling for the mirror Job. |
| `nim.backoffLimit` / `nim.ttlSecondsAfterFinished` | `1` / `3600` | Retries / self-cleanup. |

NIM mirrors share `maxConcurrency` with HF downloads (one queue, one slot pool).

## Sizing

The scratch volume must hold the largest profile being mirrored before it is
uploaded. A 70B profile can exceed 100 GiB; either raise `nim.workSize` and
ensure the node has that much ephemeral storage, or point
`nim.workPvcName` at a PVC large enough. The emptyDir is deleted with the
Pod, so the node gets the space back after the Job.

## Security

- The NGC key is **per submission**: typed in the browser, stored only in the
  per-job Secret, never written to the app database or logs, and deleted when
  the Job finishes. It is also used as the `nvcr.io` pull credential, so the
  key never sits in a chart value.
- S3 credentials follow the chart's existing `s3.*` pattern (values or an
  external Secret via your own values overlay) and are injected into the Job
  exactly as the HF S3 job does.
- The Job runs in the target `project-user-*` namespace with the default
  ServiceAccount, like a download Job; it gets no cluster RBAC.
- A dedicated NIM bucket is recommended. The mirror writes at the bucket root;
  do not point `nim.bucket` at a bucket that also holds unrelated objects
  unless you accept the mixed key space.

## Failure modes and what they look like

| Symptom (Jobs table / logs) | Cause | Fix |
|---|---|---|
| `ImagePullBackOff`, `401 Unauthorized` from `nvcr.io` | Bad/expired NGC key, or a local registry without a pull Secret | Re-submit with a valid key; set `nim.imagePullSecrets` for a local registry. |
| Unsupported action / usage error | NIM 1.x image (no `mirror` action) | Use a NIM 2.x image. |
| `401` / `403` while downloading | NGC key lacks access to the model | Use a key with NIM model access. |
| S3 `AccessDenied` / `NoSuchBucket` | Wrong bucket/credentials/region | Check `s3.*` and `nim.bucket`; the bucket must exist. |
| S3 signature/endpoint errors on MinIO | Path-style or endpoint mismatch | Keep `nim.pathStyle: true` and the MinIO `s3.endpointUrl`. |
| `no space left on device` | Profile larger than `nim.workSize` | Raise `nim.workSize` or use `nim.workPvcName`. |
| Mirror succeeded but NIM still downloads from NGC | Serving deployment lacks `NIM_REPOSITORY_OVERRIDE` (or has a prefix) | Set it to exactly `s3://<bucket>`; give the pod the S3 credentials and region. |

## Verification

1. Submit a NIM mirror from the UI (image + NGC key + optional profile IDs).
2. Watch the Job logs: the NIM CLI prints the profiles it downloads and the S3
   uploads; the Jobs table's output URL is `s3://<bucket>`.
3. `aws s3 ls s3://<bucket>/ --recursive | head` (or `mc ls`) shows the
   percent-encoded keys.
4. Deploy the NIM with `NIM_REPOSITORY_OVERRIDE=s3://<bucket>` and confirm it
   starts without egress to NGC.

## Out of scope

- NIM 1.x profile caches.
- NIM container-image mirroring into a local registry.
- NIM profiles on `models-pvc` (the mirror is S3-only).
- S3 garbage collection / deletion of mirrored profiles; the existing GC pass is
  PVC-only by design.
- Listing NIM mirrors in **Downloaded models** (that view is the HF model
  inventory); the **NIM profile mirrors** table is the NIM history.
