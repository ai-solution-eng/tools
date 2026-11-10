# Manual deletion of downloaded models — design note

**Status:** proposal for review. No code written; nothing in the repo changed.
**Audience:** Andrew + the colleague who raised the feature.
**Goal:** agree the shape before any `rm` exists.

---

## 1. What this is

An admin-gated page that lets an operator select one or more model cache
directories on the shared model PVC and delete them, irreversibly.

It reuses the verdict logic the GC subsystem already has, keeps the protection
signals that guard against corruption as hard blocks (§4), and adds exactly two
new things: an **admin** authentication gate, and a Job that performs the
deletion on request instead of on a cron schedule.

## 2. What this is not

- **Not a checkbox column on the Downloaded models table.** That table is a
  dedupe-by-`model_name` merge of three sources
  ([storage.py](src/model_downloader/app/storage.py#L64-L104)), so rows can
  have an empty `cachepath`, two different directories can collapse into one
  row, and the whole `tbody` is rebuilt every 10 s
  ([app.js](src/model_downloader/app/static/app.js#L611-L656)) which would
  destroy checkbox state mid-flow. The GC report rows are already
  per-cache-directory with a path, a size and a reason
  ([gc.py](src/model_downloader/app/gc.py#L207-L237)) — that is the right
  surface for a destructive action.
- **Not S3 deletion.** `README.md` already scopes S3 eviction out; this stays
  consistent. A row that still lists after a PVC delete must say so in the UI.
- **Not an AIOLI row deletion.** The MLIS `packaged_models` table is read-only
  from this app and stays that way.
- **Not a multi-tenancy fix.** See §3 — read that before approving.

---

## 3. Threat model, and the decision this design makes

`/api/downloaded` lists every model on the shared model PVC, cluster-wide:
one `PVC_NAME`, one `PVC_SUBPATH`, no ownership check
([main.py](src/model_downloader/app/main.py#L492-L538)). The app has no identity
model — the only gate is an Istio `CUSTOM` AuthorizationPolicy delegating to
oauth2-proxy ([values.yaml](helm/values.yaml#L286)), which authenticates a user
but says nothing about which models are theirs.

So a plain checkbox-and-delete button would mean *any authenticated user can
irreversibly delete any project's weights*.

**The isolation question is settled: there is none, by design.** The model PVC
is a shared resource with no per-project separation of weights, and that is
deliberate. Space management is an operator concern, so **any admin may delete
any model**. This note therefore does no ownership scoping, and should not:
`submitted_by` in the provenance manifest is worth wiring for *audit*, but it
is not an authorization input here.

So the capability becomes: **admin-only, off by default, every deletion
attributed to a named person.** A non-admin cannot delete anything at all. An
admin can delete anything the verdict admits — which, on a shared PVC, is
everything except what §4 refuses.

---

## 4. Two gates, not one

This is the most important structural point.

| Gate | Question | Mechanism |
|---|---|---|
| **Auth** | *Who may delete?* | admin identity — SSO group or explicit username, §5 |
| **Verdict** | *May this be deleted?* | path guards + protection signals, §8 |

They are independent. Being an admin does not make a directory safe to delete;
it only makes the request admissible. What the verdict still refuses, and why:

| Condition | Result | Overridable? |
|---|---|---|
| live job pins the cache root | **hard block** | **never** — deleting under an in-flight download corrupts it |
| matches a `gc.protectedModels` glob | **hard block** | no — edit the values if the deny-list is wrong |
| not under the scan root, a symlink, or not a directory | **hard block** | never — this is the `rm -rf /` guard |
| AIOLI `packaged_models` row references it | **soft block** | yes, per request, with a typed acknowledgement |
| no provenance manifest | warning only | n/a |

The AIOLI case is the one deliberate softening, because "clear space" and
"protect what is serving" genuinely conflict: an MLIS row may be stale (a model
registered once and no longer deployed), and a hard block would make those
weights undeletable forever. But the same row may equally mean a live serving
deployment. So the override is an **act, not a setting**: the admin types the
model name into the modal, the confirmation states plainly that the model is
registered in MLIS and may be serving, and the tombstone records
`override: true` alongside the matching uri. There is no values knob that
disables the block wholesale — a per-request acknowledgement is the point.

---

## 5. Values

```yaml
manualDelete:
  # Master switch. false => the page 404s, the API 404s, and the Job template
  # is not rendered into the ConfigMap at all. Default off, on purpose.
  enabled: false

  # Hard cap on a single request. Guards against select-all accidents and
  # bounds the JSON payload handed to the Job.
  maxTargets: 20

  # Keep the Job around long enough that its logs are a usable audit record.
  # Matches the GC CronJob's 86400 (helm/templates/gc-cronjob.yaml#L44).
  jobTtlSeconds: 86400

  # Image for the delete Job. Empty => the app image (image.repository:tag).
  # Do NOT default to the scanner image: the in-pod re-verify imports
  # `kubernetes` and `psycopg2`, which the app image ships and the scanner
  # image does not (§9.1).
  image: ""

  auth:
    # sso | secret. Default sso: this chart is PCAI-first (ezua.enabled), where
    # oauth2-proxy injects the identity, and sso gives real per-person
    # attribution. secret is an opt-in fallback for installs with no
    # Istio/oauth2-proxy gate. Both fail closed — no configured admin, or an
    # unreadable Secret, denies everyone. The mode only decides which
    # credential is read.
    mode: sso

    # mode=sso. Admin = membership in ANY of these Keycloak groups, OR an exact
    # username match in `admins`. Union, not intersection. Both empty => nobody
    # is an admin and the page is therefore unusable. Group names match
    # case-sensitively after whitespace trimming, mirroring the gateway's
    # sso_admin_groups semantics (PCAI_LLM routes_enroll.py).
    adminGroups: []
    admins: []

    # mode=secret ONLY, and not recommended: a shared key cannot distinguish
    # people, so every deletion is attributed to "shared-key:<fingerprint>".
    # The chart NEVER creates this Secret; the operator does (same convention
    # as aioli.dbPasswordSecret, helm/values.yaml#L259-L262).
    secret:
      name: model-delete-key
      namespace: ""          # empty => release namespace
      key: token
```

Deliberately **absent**: any key that weakens the verdict globally (no
`allowProtected`, no `skipAioliCheck`, no `allowLiveJob`), and any key that
puts a credential inline in values. The AIOLI override exists only as a
per-request acknowledgement (§4), never as configuration.

### 5.1 Env wiring

Same shape as the GC block ([deployment.yaml](helm/templates/deployment.yaml#L118-L133)):
values → env var → module constant in `main.py`, with the `{{- if }}` guards
only where a zero value is meaningful.

| Values key | Env var |
|---|---|
| `manualDelete.enabled` | `MANUAL_DELETE_ENABLED` |
| `manualDelete.maxTargets` | `MANUAL_DELETE_MAX_TARGETS` |
| `manualDelete.jobTtlSeconds` | `MANUAL_DELETE_JOB_TTL` |
| `manualDelete.image` | `MANUAL_DELETE_IMAGE` (empty ⇒ the app image) |
| `manualDelete.auth.mode` | `MANUAL_DELETE_AUTH_MODE` |
| `manualDelete.auth.secret.name` | `MANUAL_DELETE_SECRET_NAME` |
| `manualDelete.auth.secret.namespace` | `MANUAL_DELETE_SECRET_NAMESPACE` (empty ⇒ `POD_NAMESPACE`) |
| `manualDelete.auth.secret.key` | `MANUAL_DELETE_SECRET_KEY` |
| `manualDelete.auth.adminGroups` | `MANUAL_DELETE_ADMIN_GROUPS` (comma-joined) |
| `manualDelete.auth.admins` | `MANUAL_DELETE_ADMINS` (comma-joined, as `GC_PROTECTED` does) |

Only the Secret *name/namespace/key* cross the boundary — never the value,
matching `AIOLI_DB_SECRET_NAME` ([deployment.yaml](helm/templates/deployment.yaml#L179-L184)).

`manualDelete.auth.secret.namespace: ""` must be rendered as
`.Release.Namespace` **in the template**, not left empty for the app to
resolve: `read_secret()` passes the namespace straight through to the API and
an empty string fails ([k8s.py](src/model_downloader/app/k8s.py#L465-L470)).

---

## 6. Credential handling

### 6.1 Why not `MCP_G2_API_KEY`

That key is the harness's own credential for the MCP relay: a 64-char string in
a workstation dotfile, handed to every agent and subagent in a session. Reusing
it here would mean anything that can call a read-only MCP tool can also
irreversibly delete model weights, rotating it would break MCP for every agent,
and the audit log would record no person. It is also a *host* secret being
promoted into a *cluster* secret, doubling its exposure for no gain.

Different credential. Same *pattern*.

### 6.2 The pattern to follow

`aioli.dbPasswordSecret` ([values.yaml](helm/values.yaml#L259-L262)) is the
house style: the chart names a Secret it never creates, the operator creates it,
and the app reads it at runtime through the Kubernetes API
([db.py](src/model_downloader/app/db.py#L81) → [k8s.py](src/model_downloader/app/k8s.py#L465))
so the value never appears in the pod spec or in values.

```bash
kubectl -n <app-namespace> create secret generic model-delete-key \
  --from-literal=token="$(openssl rand -hex 32)"
```

Read it once at first use and cache it in memory for the process lifetime
(re-reading per request is an API call per keystroke, and rotation on an
irreversible-action gate should require a pod restart anyway).

### 6.3 Comparison rules (secret mode)

- Reject an empty or whitespace-only secret **at startup**, and refuse to
  enable the feature if it is empty.
- Reject an empty presented value. `presented == expected` with both `""`
  is the classic fail-open.
- Compare with `hmac.compare_digest` on encoded bytes.
- Log a fingerprint, never the key: `sha256(key).hexdigest()[:8]`, so the audit
  trail can distinguish two keys without leaking either.

### 6.4 SSO mode

**Verified on this cluster** (read-only `kubectl` against
`oauth2-proxy/oauth2-proxy-alpha`, run 2026-11-09). The provider is OIDC and
`injectResponseHeaders` is set, so identity headers do reach upstreams:

| Header | Claim |
|---|---|
| `X-Auth-Request-Preferred-Username` | `preferred_username` |
| `kubeflow-userid` | `preferred_username` |
| `username` | `preferred_username` |
| `X-Auth-Request-Groups`, `kubeflow-groups` | `groups` |

Read **`X-Auth-Request-Preferred-Username`**, falling back to
`kubeflow-userid`. Both derive from `preferred_username` so they should agree;
the fallback is belt-and-braces, not a second identity. That value is what
`admins` matches, and it is one of the claims the identity is derived from
when groups are read from a verified token (§6.5).

**Do not dump `request.headers` on this deployment.** The same block injects
`Authorization: Bearer <id_token>` and `X-Refresh-Token`, so the obvious
debugging move — logging headers to discover the name — would write users' OIDC
tokens into pod logs. Use the table above.

Caveat: a forwarded header is only trustworthy while the Service is reachable
**only** through the ingress — anything in-cluster that can reach the pod
directly can spoof it. That is acceptable here (the shared key is equally
obtainable by anyone with cluster access), but the Service must not be exposed
directly.

SSO mode is preferred where it works: real per-person attribution, per-person
revocation, no shared secret to distribute or rotate.

### 6.5 Where the groups come from

`adminGroups` is only as trustworthy as the claim it matches against. Two
sources are available on this deployment, and they are **not** equivalent:

1. **`X-Auth-Request-Groups`** — injected by oauth2-proxy (§6.4). One line to
   read, but it is an unvalidated header: anything in-cluster that can reach the
   pod directly can set it, and it carries no binding to the token it came from.
2. **The `groups` claim of the injected `Authorization: Bearer <id_token>`** —
   verify the token, then read the claim. This is what the gateway does:
   `groups_from_validated_token(bearer, subject)` in `PCAI_LLM`'s
   `core/identity.py`, subject-bound so one subject's token can never stamp
   another's groups. Its own comment is explicit that the token's claim
   "outranks the proxy-injected header".

Prefer (2). The raw material is already present — oauth2-proxy injects
`Authorization: Bearer <id_token>` (§6.4) — and the D21 verifier
(`pcai_utils/oidc_identity.py`: RS256/JWKS, iss/aud/exp, fail-closed) is the
fleet's ratified machinery for exactly this.

**Cost, stated plainly:** this app has no `pcai_utils` dependency today
([docker/requirements.txt](docker/requirements.txt) is eight packages). That
module reaches consumers through the hardlink mesh with a drift gate, not
`pip install`, so adopting it is a fleet-integration step. If that is too much
for v1, ship with (1), require the Service to be ingress-only, and record the
upgrade as a follow-up — but do not describe the two as equally strong.

One governance note: fleet decision D21 says "a JWT is never an admin
identity" — that is about *registry* identity. Conferring an admin *tier* from
SSO groups is what the gateway already does for self-mint profiles
(`tenants.sso_admin_groups`), so this is consistent, but it is a fleet-level
decision and should be recorded as one rather than invented locally here.

---

## 7. HTTP surface

```
GET  /admin/delete
     200  HTML page (verdict table + checkboxes + red delete button)
     404  the route is simply not registered when manualDelete.enabled is false
     401/403 as below

POST /api/admin/deletions/preview
     headers: X-Delete-Key: <token>          # mode=secret
     body:    {"targets": ["/mnt/large-models/models--org--Repo", ...]}
     200  {
            "verdict_id": "<opaque>",
            "expires_at": 1762700000,
            "resolved": [
              {
                "path": "/mnt/large-models/models--org--Repo",
                "model_name": "org/Repo",
                "bytes": 123456789,
                "age_days": 12.4,
                "manifest": true,
                "scanned": "clean",
                "verdict": "deletable" | "blocked",
                "blocks": ["in AIOLI packaged_models: pvc://models-pvc/..."],
                "warnings": ["no provenance manifest — cannot confirm this app downloaded it (predates provenance tracking, or fetched elsewhere)"]
              }
            ],
            "summary": {"deletable": 2, "blocked": 1, "bytes": 234567890}
          }
     401  missing/blank key
     403  wrong key / not in admins
     400  feature disabled — house convention is 400 + detail, as _require_gc()
          does ([main.py](src/model_downloader/app/main.py#L544-L546))
     422  malformed target, or > maxTargets

POST /api/admin/deletions
     headers: X-Delete-Key: <token>
     body:    {"verdict_id": "<opaque>", "confirm": "DELETE"}
     202  {"job_name": "md-delete-3f9a1c2e", "targets": [...], "job_ttl_seconds": 86400}
     409  unknown/expired verdict_id — re-preview
     409  no deletable targets in the pinned set
     400  feature disabled

GET  /api/admin/deletions/{job_name}
     200  {"status": "running"|"complete"|"failed", "logs": "..."}
```

The status endpoint reads the Job's pod logs through the Kubernetes API
directly ([k8s.py](src/model_downloader/app/k8s.py#L434)) — a delete Job is not
a queue `JobRecord`, so `queue.get_logs()` does not apply to it.

**Two modes on one endpoint.** An empty `targets` list is *list mode*: the
page's initial load, returning every candidate on the scan root with its
verdict and **no** `verdict_id` — a pin requires an explicit selection. A
non-empty list is *pin mode* and issues the `verdict_id` described below.

**`verdict_id` is the important part.** The preview resolves the selection to a
concrete, ordered set of absolute paths and stores it server-side for a short
TTL, in memory, single process — the same pattern as `gc_report_cache` at
[main.py](src/model_downloader/app/main.py#L564).

The clock starts when the preview **finishes**, not when it starts: the preview
may itself run a scanner Job with a 300 s timeout
([main.py](src/model_downloader/app/main.py#L580)), so a TTL measured from
request time can expire before the operator ever sees the result. Size it above
the scan timeout (900 s), bind the id to the presenting actor so one key cannot
consume another's preview, and cap the store (e.g. 32 entries, oldest evicted) —
each preview can spawn a scanner Job, so an uncapped loop is a cluster-cost
vector. The
delete call deletes *exactly that pinned set*, not a fresh re-resolution. What
the operator reviewed is what dies, and a rescan between the two calls cannot
silently change the target set. An app restart between preview and delete
invalidates the id — the operator re-previews, which is the safe failure.
The id is also **consumed on commit**: it is popped before the Job is created,
so one pin authorises exactly one Job and a replayed commit cannot spawn a
duplicate delete against directories the first Job already removed.

This relies on the deployment being **one replica** (hardcoded at
[deployment.yaml](helm/templates/deployment.yaml#L9)). If that ever changes,
the pinned set must move to a shared store before the second replica ships, or
a delete can 409 on a preview the user just made.

`confirm: "DELETE"` is a typed confirmation, not a boolean, so a stray
programmatic POST cannot delete. The modal shows the count and total bytes.

---

## 8. Verdict function

New pure function in [gc.py](src/model_downloader/app/gc.py), next to
`build_gc_plan`, sharing its helpers (`uri_matches_cache_dir`,
`live_job_protects`, `glob_protected`):

```python
def evaluate_manual_targets(
    entries: list[dict],
    *,
    aioli_uris: list[str],
    live_jobs: list[dict],
    protected_models: list[str],
    aioli_available: bool = True,
    jobs_available: bool = True,
) -> list[dict]:
    """Per-directory verdict for a manual deletion request.

    Differs from build_gc_plan in three ways:
      * the TTL line is NOT applied — manual selection overrides age;
      * a missing manifest is a WARNING, not a block (externally downloaded
        models have no manifest and must remain deletable);
      * an AIOLI hit is reported as `soft_block` rather than `block`, so the
        caller can offer the per-request override (§4).
    The app calls this TWICE: at preview, and again at commit immediately
    before creating the Job. The Job itself does not re-run it — it has no
    cluster RBAC (§9.1).
    An unavailable cross-check blocks everything (fail-closed).
    """
```

Verdict rules:

| Condition | Result |
|---|---|
| path not under scan root, symlink, or not a directory | `block` (hard) |
| `aioli_available` is False | `block` all (hard) |
| `jobs_available` is False | `block` all (hard) |
| live job pins the cache root | `block` (hard), quoting ns/job |
| matches a `gc.protectedModels` glob | `block` (hard) |
| AIOLI uri matches the cache dir | `soft_block`, quoting the matching uri |
| no provenance manifest | `deletable` + warning |
| otherwise | `deletable` |

**Fail-open trap to avoid.** The existing report path swallows a live-jobs
listing failure and proceeds with an empty list
([main.py](src/model_downloader/app/main.py#L610-L614)); the CronJob correctly
fails closed instead ([gc-cronjob.yaml](helm/templates/gc-cronjob.yaml#L121-L125)).
The manual path must follow the CronJob, not the report: a jobs-API failure
blocks every target and says why.

---

## 9. The delete Job

A new `manual-delete-job.yaml` entry in the Job-template ConfigMap
([configmap-job-template.yaml](helm/templates/configmap-job-template.yaml)),
rendered only when `manualDelete.enabled`.

Admission is copied verbatim from the GC CronJob and the scan Job —
`hpe-ezua/app: mlis`, `hpe-ezua/disable-sc: "true"`, `runAsUser: 0`,
`sidecar.istio.io/inject: "false"` — and it must be a **Job**, not a Pod: the
job-controller is the creator that satisfies the platformwide Kyverno
`protect-models-pvc` policy ([configmap-job-template.yaml](helm/templates/configmap-job-template.yaml#L665-L672)).

Differences from the scan Job, all deliberate:

| | scan Job | delete Job |
|---|---|---|
| PVC mount | `readOnly: true` ([L854](helm/templates/configmap-job-template.yaml#L854)) | read-write, as the GC Job |
| targets | derived from the scan root | explicit list, `MD_TARGETS` (base64 JSON) env |
| managed-by label | `model-downloader-scan` | `model-downloader-delete` |
| annotations | scan flag | `model-downloader/delete-actor` only — **no `cache-root`** (below) |
| namespace | `DEFAULT_NAMESPACE` | `DEFAULT_NAMESPACE` — the namespace that OWNS the PVC |
| ServiceAccount | `default` | `default` — no cluster RBAC |
| `ttlSecondsAfterFinished` | 120 | `jobTtlSeconds` (86400) |
| `activeDeadlineSeconds` | none | `jobDeadlineSeconds` (3600) |

The read-write mount is **not speculative**: the GC CronJob already mounts the
same PVC read-write (no `readOnly` flag,
[gc-cronjob.yaml](helm/templates/gc-cronjob.yaml#L322-L325)) under the same
admission labels and `runAsUser: 0`, and it is the thing that does the deleting
today. This Job is that mount, on request, with a target list.

**Namespace and ServiceAccount — the two things that broke the first builds.**
A PVC is namespace-scoped, so the Job must be created where `models-pvc` lives:
`DEFAULT_NAMESPACE`, the same namespace the scan Job mounts it from and the
download Jobs are created in. Creating it in the app's namespace left the pod
Pending forever (`persistentvolumeclaim not found`). And because the pod runs
*there*, the app's ServiceAccount does not exist in that namespace — a
cross-namespace SA reference is impossible — so the template sets **no**
`serviceAccountName` and the pod runs as `default`, exactly like a download
Job. That is only viable because the pod needs no cluster RBAC (§9.1).

**No `cache-root` annotation.** It was in the first implementation so the GC
cross-check would see a delete in progress. But `live_job_protects()` treats
any managed Job whose `cache-root` covers an entry's parent as pinning it, and
it ignores Job status — so annotating the delete Job with the scan root made it
pin EVERY model on the PVC: it blocked its own targets and kept every later
preview fully blocked for the whole `jobTtlSeconds`. Delete Jobs are now
dropped from the live-job list entirely (`gc.drop_delete_jobs`).

**Add the new managed-by value to both existing selectors** — otherwise the
GC cannot see a delete in progress and the downloaded list treats it as a
normal job:

- [k8s.py](src/model_downloader/app/k8s.py#L652) `list_managed_jobs_by_annotation`
- [gc-cronjob.yaml](helm/templates/gc-cronjob.yaml#L109) (the selector is a hardcoded string in the python pass)

`gc.concurrencyPolicy: Forbid` only serializes GC-vs-GC
([values.yaml](helm/values.yaml#L159)); it does not cover this Job. A concurrent
GC pass removing a directory first just fails structural validation and aborts
the run, which is safe.

### 9.1 What the pod runs — and the two things the GC Job gets wrong

The delete Job must **not** copy the GC CronJob's implementation. Two
properties of that template make it a trap:

The pod is deliberately as dumb as a download Job. It runs the **app image**
(`python3 -m app.delete_targets`, resolved from the image's `WORKDIR /app`
because the Dockerfile flattens `src/model_downloader/app/` to `/app/app/`),
mounts the PVC read-write, and makes **no Kubernetes API calls and no database
calls**. That is exactly what lets it run in the PVC's namespace under the
`default` ServiceAccount.

The GC CronJob's shape is a trap here for two opposite reasons. It runs the
scanner image and probes `import kubernetes`
([gc-cronjob.yaml](helm/templates/gc-cronjob.yaml#L79-L84)); and it sets no
`serviceAccountName` at all, so its own cross-checks fail-safe to "keep
everything". Copying the first would leave the pod without the clients it
needs; copying the second *and* naming the app SA leaves it Pending
(`serviceaccount not found`). See §17 — the GC path itself may still have that
second bug.

So the protection moved **up**: the app re-runs the full verdict at commit
time, immediately before creating the Job, using the RBAC it already has. The
pod's own checks are structural only (§9.1.1).

**Payload:** `MD_TARGETS` as **base64-encoded** JSON. Template substitution is
a literal `.replace()` ([k8s.py](src/model_downloader/app/k8s.py#L523-L527));
splicing raw JSON into a double-quoted YAML env value breaks on a quote or a
newline in any path. Base64 makes the payload YAML-inert.

Hosted trials need the same job-controller `NotEquals` platform patch the GC
Job documents ([gc-cronjob.yaml](helm/templates/gc-cronjob.yaml#L8-L9)).

### 9.1.1 In-pod algorithm (structural only; abort-all before any `rmtree`)

1. Decode `MD_TARGETS_B64`; dedupe by path.
2. For each target: absolute; not a symlink; a directory; basename
   `models--<org>--<repo>`; `realpath` strictly under `MD_SCAN_ROOT`; not the
   root itself. **Any** failure aborts the whole run — a bad path means the
   payload is not what we think it is, so nothing is deleted.
3. Rename the directory to `.deleting-<name>` in the same parent (atomic), so
   the scanner's `models--*--*` glob no longer matches it.
4. Write the tombstone (§10) — stdout is mandatory, the `_deleted/` copy is
   best-effort.
5. `shutil.rmtree(<renamed path>)`.
6. Best-effort: remove `<parent>/.locks/<cache id>/` (and the `.locks/` parent
   when it ends up empty). `huggingface_hub` keeps per-repo locks there, and the
   leaf carries the same `models--<org>--<repo>` name — left behind, it makes the
   scanner report the deleted model as a 0-byte phantom forever. The scanner and
   the GC walk also skip `.locks/` outright, so the two halves agree.

The rename is what makes a killed run safe: the model leaves the listing the
instant deletion starts, and an interrupted run leaves an obviously-junk
`.deleting-*` directory instead of a half-deleted model that the scanner would
still present as complete.

`backoffLimit: 0`, `restartPolicy: Never` — a failed delete must not retry
blindly. `activeDeadlineSeconds` (`jobDeadlineSeconds`, 3600) exists to catch a
pod that never schedules, **not** to bound the deletion: a Pending pod is not a
failure, so `backoffLimit` never fires and the Job would otherwise hang with no
signal. It must stay comfortably above the slowest `rmtree`.

---

## 10. Audit trail

The provenance `manifest.json` lives **inside** the cache directory
(`${CACHE_ROOT}/${CACHE_ID}/manifest.json`) and dies with the `rmtree`, so
the app's "traceability record" claim in
[gc.py](src/model_downloader/app/gc.py#L25-L27) only holds until deletion. A
manual delete therefore needs a record of its own:

- **stdout JSON line per deletion**, before the `rmtree`:
  `{"ts","actor","key_fingerprint","job","path","model","bytes","manifest_copy","verdict"}`
  — survives in the Job logs for `jobTtlSeconds`.
- **`<scan_root>/_deleted/<ts>-<models--org--Repo>-<job>.json`** on the PVC,
  best-effort, containing the copied manifest. Use the cache-dir basename, not
  `model_name` — the latter is `org/Repo` and the slash would nest the file or
  fail the write. `_deleted/` does not match the scanner's
  `models--*--*` glob, so it is invisible to listing and GC; it needs its own
  retention decision (§14, Q4).

Actor is the SSO username, or `shared-key:<fingerprint>` in secret mode.

---

## 11. Cache invalidation and UI truthfulness

A delete that the UI does not reflect reads as a failure. After the Job is
created, invalidate the scanner-backed caches so the next poll is honest:

- `DownloadedModelsCache` — serves PVC rows for `PVC_REFRESH_INTERVAL` and,
  with `PVC_SCAN_ENABLED` off, **never** refreshes on its own
  ([storage.py](src/model_downloader/app/storage.py#L475-L480)). Force a rescan.
- `gc_report_cache` — 120 s TTL ([main.py](src/model_downloader/app/main.py#L124)).
- preflight usage/size caches — 120 s / 300 s
  ([main.py](src/model_downloader/app/main.py#L110-L111)). Freed bytes reach
  quota checks late; that errs toward refusal, not over-admission, so it is
  acceptable. Document it rather than fix it.

UI copy requirements: the page shows the Job name and a link to its logs; a
`["pvc","s3"]` row that persists after a PVC delete is labelled
"still present on S3", not shown as a failed delete.

---

## 12. Fail-closed matrix

| Condition | Behaviour |
|---|---|
| `enabled: false` | page route not registered (404), API 400 + detail, Job template not rendered |
| `mode=secret`, Secret missing/unreadable | every request 403, message "deletion is not configured on this deployment" |
| `mode=secret`, Secret present but empty | refuse to start the feature; 403 |
| key header absent or blank | 401 |
| key wrong | 403 (constant-time) |
| `mode: sso`, header absent | 403 that *names* the misconfiguration — "no identity header; this deployment is not behind oauth2-proxy, set manualDelete.auth.mode=secret" — not a bare "forbidden" |
| `mode: sso`, caller is not in `admins` and in no `adminGroups` entry | 403 "admin required" — no verdict work performed, nothing about the target disclosed |
| no admin configured (`admins` and `adminGroups` both empty) | every request 403; the page says so rather than looking broken |
| `oidc.enabled` and the bearer token is missing or unverifiable | 403 naming the fix — a bare identity header is NOT accepted when an IdP is configured |
| `oidc.issuer` empty | verifier inert; the gate falls back to the proxy header (weaker — documented, not silent) |
| AIOLI soft block, no override | target reported `soft_block` and excluded from the Job's set |
| override requested for a hard block | 422 naming the condition; the target is never sent to the Job |
| AIOLI read fails at preview | all targets blocked |
| jobs list fails at preview | all targets blocked |
| AIOLI/jobs fail at **commit** | nothing admitted → 409, and the preview is restored so a retry needs no re-scan |
| a target vanished or became blocked between preview and commit | dropped from the Job's set and named in the 409 detail |
| target outside root / symlink / not a dir | blocked at preview; the Job re-validates structurally and aborts the whole run |
| `len(targets) > maxTargets` | 422 |
| `verdict_id` unknown, expired, or already used | 409, re-preview |

Every row is a test in §13. The two that matter most are "Secret missing ⇒
deny" and "IdP configured + bare header ⇒ deny" — the rows where a plausible
implementation fails open.

---

## 13. Tests

Pure logic (no cluster, mirrors the existing `tests/test_gc_plan.py` style):

- `evaluate_manual_targets`: each verdict rule; TTL not applied; missing
  manifest warns; AIOLI-unavailable blocks all; jobs-unavailable blocks all;
  the matching uri appears in the reason; AIOLI reports `soft_block` while
  live-job and glob report `block`.
- override: a soft block is admitted with an override, a hard block is refused
  even with one, and the refusal names the condition.
- path guards: outside root, symlink, non-directory, bad basename.
- auth: empty secret, missing header, blank header, wrong key, correct key;
  sso `admins` hit/miss, `adminGroups` hit/miss, union semantics, trimming and
  case sensitivity; both lists empty => deny all; constant-time path exercised;
  the sso-misconfigured 403 message actually names the fix.
- the admin handler does not log `request.headers` (guard against the
  `Authorization`/`X-Refresh-Token` leak called out in §6.4).

Helm render (`tests/test_helm_render.py` pattern):

- disabled ⇒ no `manual-delete-job.yaml` in the ConfigMap, no env vars, page
  route absent from the deployment env.
- enabled ⇒ template present, PVC mount is read-write, managed-by value is
  `model-downloader-delete`, `ttlSecondsAfterFinished` matches
  `jobTtlSeconds`, **`serviceAccountName` is the app service account** (not
  absent — that is the GC Job's bug, §17), and the image is the app image, not
  `scanImage`.
- enabled without a secret name ⇒ template still renders but the app refuses at
  request time (this is the intended split: no secret value ever lives in
  values).

---

## 14. Rollout

0. **Ship the module and the chart together.** The delete Job runs the app
   image, so `image.tag` must point at a build that contains
   `app/delete_targets.py`. `./automation.sh <version>` already does this in
   the right order — it bumps the chart version and the values tag first, then
   builds and pushes the image from the bumped tree
   ([automation.sh](automation.sh#L10-L17)). Enabling `manualDelete` against an
   older tag yields a Job that dies with `ModuleNotFoundError`, which is the
   one failure mode that looks like a design bug but is an ordering bug.
1. Create the Secret (§6.2).
2. `helm upgrade … --set manualDelete.enabled=true` (leave `gc.enabled` as-is —
   the two features are independent).
3. Verify: `GET /admin/delete` 200; the same without a key 401/403; a preview
   against a scratch model returns `deletable`; a model with an MLIS row
   returns `blocked` with the matching uri.
4. Before the first real deletion, confirm the Job's own credentials:
   `kubectl -n <ns> get job md-delete-… -o jsonpath='{.spec.template.spec.serviceAccountName}'`
   must print the app SA, and its logs must show the live-job and AIOLI
   cross-checks *succeeding*, not fail-safing. A delete Job that inherits the
   GC Job's service account aborts every run.
4. First real deletion: a model you can re-download, then confirm the tombstone
   in the Job logs and in `_deleted/`, and that the listing reflects it after
   the forced rescan.

---

## 15. Open questions for review

1. ~~**`mode: secret` or `mode: sso`?**~~ **Resolved:** oauth2-proxy injects
   `X-Auth-Request-Preferred-Username` / `kubeflow-userid` here (§6.4), so
   `mode: sso` is the values default and `admins: [...]` is the only setup
   step. `secret` remains the fallback for gate-less installs.
2. ~~**Is per-project isolation required?**~~ **Resolved: no.** The PVC is a
   shared resource by design and any admin may delete any model to reclaim
   space (§3). No ownership scoping, no per-project cache roots.
3. **Is the AIOLI override acceptable as designed** (§4) — a per-request typed
   acknowledgement by an admin, recorded as `override: true` in the tombstone,
   with live-job and `protectedModels` remaining hard blocks? The alternative
   is AIOLI as a hard block too, which means stale MLIS rows make weights
   permanently undeletable.
4. **Should the Downloaded models table link to the admin page** with the
   target pre-filled (`/admin/delete?target=…`)? Nice UX, keeps the destructive
   button off the daily page — recommended, small.
5. **Retention for `_deleted/` tombstones** — forever, or prune by age in the
   GC pass?
6. **Fix the GC Job's service account and image now, or in the same PR?** (§17)
   They are two lines, and without them `gc.dryRun: false` is a switch that
   does nothing. I would fold it into the same change — the delete Job needs
   the identical fix, so the diff is smaller together than apart.

---

## 16. Rejected alternatives

| Option | Why not |
|---|---|
| Checkbox column on the Downloaded models table | dedupe by `model_name`, empty `cachepath` rows, 10 s full re-render kills selection (§2) |
| Reuse `MCP_G2_API_KEY` | host-side agent credential; privilege creep onto an irreversible action; no attribution; rotation couples MCP and deletion (§6.1) |
| Reuse the GC CronJob for manual targets | a CronJob carries no per-request payload |
| Key inline in `values.yaml` / env var | breaks the existingSecret convention; leaks the credential into the pod spec and into git |
| Gate only the page, not the API | hiding a page is not access control |
| Let the admin key override the three-key verdict | moves the footgun behind a login (§4) |

---

## 17. Adjacent finding — the shipped GC Job probably deletes nothing

Found while designing the delete Job. It is not part of this feature, but it
changes what "the GC already handles this" means.

As shipped, the GC CronJob pod:

- runs as the **`default`** service account — its pod spec sets no
  `serviceAccountName` ([gc-cronjob.yaml](helm/templates/gc-cronjob.yaml#L53-L56)),
  while the chart's ClusterRole is bound only to the app service account
  ([rbac.yaml](helm/templates/rbac.yaml#L47-L49)). So its live-job list and its
  AIOLI secret read are both RBAC-denied.
- runs `downloadList.scanImage`, a basic Ubuntu image
  ([values.yaml](helm/values.yaml#L98)) that almost certainly lacks
  `kubernetes` and `psycopg2`. The template itself probes for the first and
  expects it to be absent ([gc-cronjob.yaml](helm/templates/gc-cronjob.yaml#L79-L84)).

Both failures land in documented fail-safes that keep everything
([gc-cronjob.yaml](helm/templates/gc-cronjob.yaml#L264-L270)). Net effect: even
with `gc.dryRun: false` the GC Job would delete nothing, and the report would
read as "everything is protected" rather than "the cross-checks are broken".

Verify from inside the cluster (one command, read-only in effect):

```bash
kubectl -n <ns> create job --from=cronjob/<release>-gc gc-probe
kubectl -n <ns> logs job/gc-probe | head -20
```

If it prints `JOBS: unavailable (...) — fail-safe keeps everything`, the
finding is confirmed. The fix is the same two lines this feature needs: an
explicit `serviceAccountName` and the app image instead of the scanner image.
Worth fixing regardless of whether manual delete ships — otherwise `dryRun:
false` is a switch that changes nothing.

---

## 18. Implementation status

Shipped and **verified on the G2 cluster**: a real deletion of four models
(~530 GiB) completed in 32 seconds, each directory tombstoned before removal.
Default off; nothing can delete until `manualDelete.enabled` is flipped.

| File | Role |
|---|---|
| [gc.py](src/model_downloader/app/gc.py) | `evaluate_manual_targets` (the verdict), `drop_delete_jobs` |
| [delete_targets.py](src/model_downloader/app/delete_targets.py) | the in-pod module: decode, validate structurally, tombstone, rename, `rmtree` |
| [main.py](src/model_downloader/app/main.py) | the two gates (verified identity, verdict), list/pin preview, commit-time re-verify, status |
| [oidc_identity.py](src/model_downloader/app/oidc_identity.py) | vendored `pcai_utils` RS256/JWKS verifier (SHA-guarded) |
| [k8s.py](src/model_downloader/app/k8s.py) | `create_delete_job`, `parse_job_templates`, `model-downloader-delete` in the live-job selector |
| [admin_delete.html](src/model_downloader/app/templates/admin_delete.html), [admin_delete.js](src/model_downloader/app/static/admin_delete.js) | the page |
| [values.yaml](helm/values.yaml), [deployment.yaml](helm/templates/deployment.yaml), [configmap-job-template.yaml](helm/templates/configmap-job-template.yaml) | `manualDelete` + `oidc` blocks, env wiring, the Job template |
| [test_manual_delete_plan.py](tests/test_manual_delete_plan.py), [test_manual_delete_wiring.py](tests/test_manual_delete_wiring.py), [test_delete_targets.py](tests/test_delete_targets.py), [test_job_templates.py](tests/test_job_templates.py), [test_oidc_vendored.py](tests/test_oidc_vendored.py) | 93 tests: verdict, HTTP surface, in-pod guards, template wiring, vendored-module drift |

### Deltas from the design as written — and the bugs they fixed

Each of these was found by deploying, not by the original review.

* **The Job runs in the namespace that OWNS the PVC** (`DEFAULT_NAMESPACE`), not
  the app's namespace. PVCs are namespace-scoped; naming the app namespace left
  the pod Pending forever on `persistentvolumeclaim not found` (§9).
* **No `serviceAccountName`.** The app SA does not exist in the PVC's namespace,
  and a cross-namespace reference is impossible; naming it left the pod Pending
  on `serviceaccount not found`. The pod uses `default`, like a download Job.
* **The protection checks moved to commit time in the app**, which is what makes
  the ServiceAccount omission viable. The pod has no cluster RBAC and no
  database access at all (§9.1).
* **No `cache-root` annotation.** It pinned every model on the PVC — the Job
  blocked its own targets, and a finished Job blocked every later preview for
  the full `jobTtlSeconds`. Delete Jobs are dropped from the live-job list
  outright (`gc.drop_delete_jobs`).
* **Every optional template key must be mapped** in `K8sClient.start()`.
  `manual-delete-job.yaml` was not, so every commit died with "template is
  missing" even though the chart shipped it. `parse_job_templates` is now a
  pure, tested function.
* **The page is auth-mode aware.** It demanded a deletion key in `sso` mode,
  where no key exists, making the page unusable.
* **`verdict_id` is single-use** and targets are deduped — a replay would have
  spawned duplicate Jobs against already-deleted directories.
* **Rename before `rmtree`** (§9.1.1): a killed run leaves `.deleting-*` junk
  instead of a half-deleted model the scanner still lists.
* **`jobDeadlineSeconds` (3600)** replaced a 600s constant that would have
  killed a large `rmtree` mid-flight. It exists to catch a pod that never
  schedules.
* **The admin gate verifies the OIDC token** rather than trusting a header any
  pod in the cluster could forge. With `oidc.issuer` empty it falls back to the
  header, documented as weaker.
* A transient Secret-read failure is not cached; the delete-Job image has no
  scanner-image fallback.

### Verified, and not

Verified on the cluster: image roll-out, the deployed Job template (no
ServiceAccount, no AIOLI env, no `cache-root`, deadline placeholder), the
runtime env values, and a full deletion end to end.

Verified locally: 343 tests, `ruff` clean, `mypy` clean on the changed modules,
`helm template` in both states, and `MANUAL_DELETE_*`/`OIDC_*` env names
matching what `main.py` reads with no gaps in either direction.

**Not verified:** concurrent GC vs a running delete, and the `_deleted/`
tombstone file surviving on the PVC (the stdout record is what was confirmed).

### Enabling

```bash
helm upgrade <release> helm --set manualDelete.enabled=true \
  --set 'manualDelete.auth.admins={andrew-bydlon}' \
  --set oidc.issuer='https://keycloak.${DOMAIN_NAME}/realms/UA' \
  --set oidc.jwksUrl='http://keycloak-headless.keycloak.svc.cluster.local:8080/realms/UA/protocol/openid-connect/certs'
```

`manualDelete.auth.mode=secret` (+ a `model-delete-key` Secret) is the fallback
for installs with no SSO gate. Then `/admin/delete` — there is no nav link to it
on purpose; it is reached by URL.
