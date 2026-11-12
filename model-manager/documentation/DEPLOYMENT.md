# Model Manager — deployment on PCAI

**Deployment is a values problem:** import the packaged chart into PCAI once, create the required Secrets out-of-band, then everything below is edited in the chart's values. There is no `helm install` and no `kubectl apply` of rendered manifests on the PCAI path — those exist only as the developer/secondary path ([§6](#6-developer-path-sanitary-checks-and-plain-helm)).

## The deployment contract

- The chart is **packaged and imported into PCAI once** (AI Essentials / EzAppConfig — PCAI's application custom resource — import; PCAI uploads the chart to its ChartMuseum, a Helm chart repository server, and its controller installs it). The packaged artifact is `model-manager-<version>.tar.gz` (2.1.2 today, built by `./automation.sh <version>`).
- **The operator's only surface is the values editor.** Every site-specific decision must be a value. No template edits, no post-render patching, no editing rendered manifests.
- **What PCAI does for you:** substitutes `${DOMAIN_NAME}` everywhere before rendering (leave the placeholders as-is), provides the platform Istio gateway and oauth2-proxy SSO that the chart's `ezua` block wires into, creates the release namespace, and provides the shared `models-pvc` in every `project-user-*` namespace.
- **What you must do out-of-band, because it cannot be a value:** the Secrets in [§1.1](#11-secrets-the-operator-creates) and the cluster objects/patches in [§1.2](#12-cluster-objects-and-patches).
- **Upgrade = import the new chart version, keep the values, re-apply** ([§4](#4-upgrade-rollback-uninstall)).
- **Naming:** the product, the chart, the Python package (`model_manager`), the container image (`ghcr.io/ai-solution-eng/model-manager`; the job image `ghcr.io/ai-solution-eng/hf-downloader` is a separate component), the Kubernetes labels/annotations (`model-manager/*`), the provenance schema (`model-manager.provenance/1.0`) and the UI host (`model-manager.${DOMAIN_NAME}`) all use the new name. Objects written by older releases keep working: the app and the GC CronJob accept the legacy `model-downloader/*` labels/annotations on read. The legacy names are deprecated and will be removed in a later release.

## 1. Prerequisites

Each item states its purpose, its failure mode, and how to verify it — from inside the cluster, not from a laptop. The chart renders its own RBAC, ConfigMaps and Jobs; what it cannot render for you is the storage, the bucket, the platform-policy admission and the Secrets below.

### 1.1 Secrets the operator creates

| Secret | Keys | Consumed by | Why it exists | Fails how without it | Create with |
|---|---|---|---|---|---|
| `aioli-db-password` (in namespace `mlis` by default) | `password` | Catalog **Push to MLIS** (`/api/push`), the GC CronJob's read-only cross-check, the manual-deletion AIOLI re-verify | The MLIS/AIOLI database password. The chart stores only the *reference* (`aioli.dbPasswordSecret.name/namespace/key`) in values and reads the value at runtime through the Kubernetes API — the credential never appears in the pod spec or in git | Push requests fail with a database error; the GC report and manual-deletion verdicts fail closed (cross-check "unavailable" — everything blocked or kept, nothing deleted); no model ever reaches MLIS | `kubectl -n mlis create secret generic aioli-db-password --from-literal=password='<aioli-db-password>'` — then point `aioli.dbPasswordSecret.*` at it (defaults shown) |
| `model-delete-key` (release namespace unless `manualDelete.auth.secret.namespace` says otherwise) | `token` | Manual deletion **only** when `manualDelete.auth.mode: secret` | Shared-secret fallback for installs with no SSO gate; every deletion is then attributed to `shared-key:<fingerprint>` instead of a person | Every admin-deletion request returns 403 "deletion is not configured on this deployment"; an empty Secret disables the feature | `kubectl -n <release-ns> create secret generic model-delete-key --from-literal=token="$(openssl rand -hex 32)"` |

Notes:

- **HF tokens and NGC API keys are not deploy-time Secrets.** Every download creates its own `hf-token-<model>-<suffix>` Secret in the target namespace from the token typed in the UI and deletes it when the Job finishes; every NIM mirror creates `ngc-api-...` (plus an `nvcr.io` pull Secret) per submission. The chart ships neither credential.
- **`s3.accessKeyId` / `s3.secretAccessKey` are plain values** — this chart has no Secret indirection for them. Keep the real keys only in `helm/local/values.<site>.yaml` (gitignored, hardlink-ignored, never packaged — see [helm/local/README.md](../helm/local/README.md)); the shareable examples under `helm/values-examples/` carry role-named placeholders.
- Skip `aioli-db-password` entirely only if you never use Push to MLIS — the GC and manual-deletion cross-checks then always report the AIOLI signal as unavailable and fail safe.

### 1.2 Cluster objects and patches

| Object | Why it is required | What breaks without it | Verify |
|---|---|---|---|
| Shared `models-pvc` in every target `project-user-*` namespace (PCAI provides it) | The PVC backend writes HF caches there; downloader Jobs mount it at `/mnt`; the scanner, debug, delete and GC Jobs mount the same volume | Download Jobs go Pending/CrashLoop on `persistentvolumeclaim not found`; manual-deletion Jobs must run in the namespace that owns the PVC (`defaultNamespace`) — a wrong namespace gives the same Pending | `kubectl -n project-user-<name> get pvc models-pvc` |
| Platform Kyverno ClusterPolicy `protect-models-pvc` admitting job-controller-created pods that carry `hpe-ezua/app: mlis` | Every Job this chart submits (downloader, NIM, debug, scanner, delete, GC) mounts `models-pvc` and carries that label; the policy denies any pod mounting the PVC without it | Every Job denied at admission: `prevent-unauthorized-create-with-mlis: Insufficient authorization to set the 'hpe-ezua/app' label to 'mlis'` — surfacing verbatim in the Jobs table's Error column or the debug-pod message box. See [§5](#5-troubleshooting) | Submit one small download and watch the Jobs table; or `kubectl get clusterpolicy protect-models-pvc -o yaml` (customer admin) |
| Kyverno installed on the cluster, with `kyverno.enabled: true` | The chart's own pre-install ClusterPolicy `add-vendor-app-labels-<release>-<chart>` stamps the `hpe-ezua/type` / `hpe-ezua/app` vendor labels that EZUA app discovery requires | The Deployment/Service can disappear from EZUA's app discovery (no tile / no route) even though the pods run | `kubectl get clusterpolicy ` — look for `add-vendor-app-labels-` |
| Platform gateway `istio-system/ezaf-gateway` + the `oauth2-proxy` auth provider (PCAI-provided) | The VirtualService attaches to that gateway; the `CUSTOM` AuthorizationPolicy delegates authentication to that provider | No ingress route to the UI, or 403s at the gateway for every request | `kubectl -n istio-system get gateway`; `kubectl -n istio-system get authorizationpolicy` |
| Image pull Secret for private/local registries (`nim.imagePullSecrets`) | NIM images mirrored into a local registry need pull credentials; NGC pulls are covered by the per-submission Secret instead | NIM mirror Jobs fail with `ImagePullBackOff` | `kubectl -n <ns> get secret <pull-secret>` |
| Chart-rendered RBAC (informational — nothing to create) | The chart renders a ClusterRole + Binding for its ServiceAccount: Jobs create/get/list/watch/delete, Secrets create/get/list/delete, namespaces `list`, pods + pods/log `get/list` — and `persistentvolumeclaims get/list` only when `preflight.enabled` | A cluster policy that forbids ClusterRole creation (or that trims these verbs) breaks submission with 403s — the namespace dropdown empties and Jobs cannot be created | `kubectl get clusterrole` — look for the release's `model-manager` role |

**The hosted-trial patch.** On SE G2 the stock `protect-models-pvc` policy admits these Jobs out of the box. On a hosted trial the customer's installed policy may be stricter than the stock one and deny job-controller-created pods; the customer's PCAI admin then appends a `NotEquals` condition exempting the kube-system job-controller — the creator of every pod for every Job this chart submits — while leaving the original conditions in force for every other creator:

```bash
kubectl patch clusterpolicy protect-models-pvc --type json -p='[
  {"op": "add", "path": "/spec/rules/1/validate/deny/conditions/all/-",
   "value": {"key": "{{request.userInfo.username}}", "operator": "NotEquals", "value": "system:serviceaccount:kube-system:job-controller"}}
]'
```

This mutates a customer-owned, cluster-wide platform policy — apply it only with the customer's PCAI admin, and only if their installed policy does not already carry the exemption. The admission webhook picks up the change within seconds. Confirm a denial is really the customer policy before treating it as an app bug: the denial message names the rule (`prevent-unauthorized-create-with-mlis`).

### 1.3 External dependencies

Prove each from inside the cluster — a check from your laptop proves nothing about the cluster's egress, DNS or proxy path.

| Dependency | Used for | Prove it from the cluster |
|---|---|---|
| S3/MinIO (`s3.endpointUrl`, default `http://minio.minio.svc.cluster.local:9000`) | Model files on the S3 backend; the NIM mirror destination. **The bucket must already exist** (`s3.bucket`, default `mlis-models`) — the chart does not create buckets | `kubectl run s3check --rm -it --image=curlimages/curl -- curl -s <endpoint>/minio/health/live` (expect `200`); then list the bucket with the values' credentials from any pod |
| MLIS/AIOLI database (`aioli.dbHost`:`aioli.dbPort`, default `aioli-db-service-hpe-mlis.mlis.svc.cluster.local:5432`) | Push to MLIS; the GC and manual-deletion cross-checks | `kubectl run pgcheck --rm -it --image=postgres:16 -- pg_isready -h <dbHost> -p <dbPort>` (expect `accepting connections`) |
| NGC (`nvcr.io` + the NGC API) | NIM profile mirrors — source images and profiles, authenticated by the per-submission NGC key | Submit one small NIM mirror from the UI; `401 Unauthorized` from `nvcr.io` means a bad or expired key, an unsupported-action error means a NIM 1.x image (2.x required) |
| GitHub raw (`catalog.githubUrl`) | The catalog's **Refresh from GitHub** button. Reachable directly or through the wired proxy; the fetch honors `catalog.githubVerifyTls` | Click the button (the result table reports the outcome), or `kubectl run ghcheck --rm -it --image=curlimages/curl -- curl -sI <githubUrl>` (expect `200`; the default URL verified live 2026-11-11) |
| Keycloak (`oidc.issuer` / `oidc.jwksUrl`) | Verifying the admin-deletion gate's bearer tokens. Point `oidc.jwksUrl` at the in-cluster Keycloak headless service over plain HTTP — the fetch then bypasses the egress proxy and the platform-CA TLS gap | Open the deletion page as an admin; a 403 naming token verification means issuer/JWKS mismatch, not a missing role |

## 2. Values

The canonical, fully commented default for every key is [helm/values.yaml](../helm/values.yaml) — read it before inventing anything. Two paste-ready, sanitized full documents exist: [helm/values-examples/values.g2.yaml](../helm/values-examples/values.g2.yaml) (proxied corporate site) and [helm/values-examples/values.hosted-trial.yaml](../helm/values-examples/values.hosted-trial.yaml) (direct egress, `${DOMAIN_NAME}` ingress); their usage and placeholder convention are in [helm/values-examples/README.md](../helm/values-examples/README.md). Real per-site values live in `helm/local/` and never leave the machine.

Required — every deployment sets or confirms these:

| Value | Why |
|---|---|
| `defaultNamespace` | Namespace pre-filled in the download form; must be a `project-user-*` namespace that owns `models-pvc` |
| `storage.backend` + `storage.default` | `pvc`, `s3` or `both` — which backends the form offers, and which is preselected. `backend: pvc` removes all S3 configuration from the app |
| `s3.endpointUrl` / `.bucket` / `.accessKeyId` / `.secretAccessKey` | Required when the backend includes `s3`. The bucket must exist; `s3.prefix` is optional |
| `aioli.*` | MLIS/AIOLI database connection + the `dbPasswordSecret` reference for Push to MLIS and the GC/manual-deletion cross-checks |
| `ezua.virtualService.endpoint` | **Required while `ezua.enabled` is true (the default) — the render fails without it.** Set `model-manager.${DOMAIN_NAME}` and leave the placeholder as-is; pair with `ezua.virtualService.istioGateway` (`istio-system/ezaf-gateway`) |
| Image tags | Packaged with the chart — `image.tag` matches the chart version, `downloader.image.tag` is the downloader image. Leave the defaults unless you build your own images |

Optional groups — the default is already sane; touch a group when your site needs it:

- **Egress proxy** — `proxy.http` / `.https` / `.noProxy`: per-key corporate-proxy wiring into downloader Jobs, debug pods and the app pod; each key renders only when non-empty, so a direct-egress site leaves `proxy: {}`. Keep cluster-internal hosts in `noProxy`.
- **TLS exceptions** — `downloader.hf.verifyTls` and `catalog.githubVerifyTls`: both default to verify-on; set `false` only behind a MITM egress proxy with an untrusted certificate, and only as explicitly as possible (per knob).
- **Job tuning** — `downloader.*` (resources, `nodeSelector`, `backoffLimit`, `ttlSecondsAfterFinished`, security context, S3 scratch `s3Work*`, HF timeouts `downloader.hf.*`) and the mirror-shaped `nim.*` (scratch `workSize`/`workPvcName`, `imagePullSecrets`, `backoffLimit`): these size and place the worker Jobs.
- **Integrity gates** — `provenance.*` (per-download `manifest.json`), `gate.*` (ModelScan, `warn`/`block` + `threshold`), `preflight.*` + `quota.*` (submit-time disk-fit and quota refusal, exact math in the error): all on by default except the GC-side ones — turn them off deliberately, not by accident.
- **Storage lifecycle** — `gc.*` (nightly TTL eviction; keep `dryRun: true` until the report is trusted) and `manualDelete.*` (admin deletion, `auth.*`, `verdictTtlSeconds`).
- **Admin identity** — `oidc.*`: verified-token gate for manual deletion. Set `issuer` + `jwksUrl` on any shared cluster; an empty issuer silently weakens the gate to the proxy header.
- **Catalog** — `catalog.*`: PVC size/class, the GitHub refresh URL, `defaultProject` (which project owns pushed models that name none).
- **Inventory** — `downloadList.*`: scanner image, auto-scan switch and interval.
- **Debug** — `debugPod.*`: the inspection shell's image, PVC override, TTL.
- **PCAI gates** — `pcai.enabled` (**defaults to false — set it true explicitly on PCAI**; it no longer follows any other flag), `kyverno.enabled` (keep true), `ezua.authorizationPolicy.*`.
- **Boilerplate** — `resources`, `service.*`, `nameOverride`/`fullnameOverride`: standard Kubernetes knobs, same meaning as in any chart.

## 3. Import and install

1. **Obtain the packaged chart** — `model-manager-<version>.tar.gz` from the release (the maintainer's `./automation.sh <version>` bumps chart + image tag, builds and pushes the server image, packages the chart and prunes old archives).
2. **Import it into PCAI once:** AI Essentials → Applications (EzAppConfig) → import the chart archive. PCAI uploads it to its ChartMuseum and its controller installs it into the target namespace.
3. **Open the chart's Helm Values editor** and paste a full values document — start from the example matching your site type ([values.g2.yaml](../helm/values-examples/values.g2.yaml) for a proxied corporate site, [values.hosted-trial.yaml](../helm/values-examples/values.hosted-trial.yaml) for a hosted trial). Both are complete documents; they do not depend on chart defaults being merged first.
4. **Adjust the `# SITE:` lines** — the namespace, usernames, MinIO endpoint/bucket/keys, the AIOLI host if it differs, and the `oidc.*` realm URL for the admin gate. Leave every `${DOMAIN_NAME}` placeholder untouched — PCAI substitutes the real domain before rendering.
5. **Apply.** A first render that fails with `Valid .Values.ezua.virtualService.endpoint is required !` means `ezua.enabled` stayed true while the endpoint was left empty — set it.
6. **Verify:**
   - Pod: `kubectl -n <release-ns> get pods` — the app pod `Running`.
   - UI: `https://model-manager.${DOMAIN_NAME}` opens behind oauth2-proxy SSO; the header shows the three tabs (Manual Deletion only when enabled).
   - Health: `/api/healthz` answers on the UI host.
   - End to end: submit one small model you can re-download; the Jobs table shows success and the Downloaded-models row appears.
   - (Developer path only: the chart's `helm install` NOTES recap proxy/Kyverno/ezua state after install.)

## 4. Upgrade, rollback, uninstall

- **Upgrade (the PCAI way):** import the new chart version, keep your values document, re-apply it in the new version's values editor. PCAI substitutes the domain placeholder again; nothing else changes.
- **Chart and image move together.** `image.tag` is packaged with the chart on purpose: a tag overwrite alone is **not** enough, because the Job-template ConfigMap — the downloader/NIM/debug/scan/delete Job definitions the app reads — only updates with a chart upgrade.
- **Enable `manualDelete` only after both halves shipped.** The feature needs the rebuilt app image (it ships `app.delete_targets`) *and* the re-packaged chart (it carries the delete-Job template); values alone cannot deliver it. Enabling against an older image yields a delete Job that dies with `ModuleNotFoundError` — an ordering bug, not a design bug.
- **Identifier rename (Model Downloader -> Model Manager):** this release renames the chart, the Python package, the image repo (`ghcr.io/ai-solution-eng/model-manager` — build and push it before upgrading), the Kubernetes labels/annotations and the UI host (`model-manager.${DOMAIN_NAME}` — update DNS and any oauth2-proxy domain allowlist; the old host no longer routes). Objects written by older releases keep working: the app and the GC CronJob read the legacy `model-downloader/*` labels/annotations as a fallback, and the live-job selectors match both generations. The provenance schema string is now `model-manager.provenance/1.0`; existing `manifest.json` files are read by their `scan` block, not by schema, so they stay valid.
- **Resource names follow the chart name.** Rendered objects are `<release>-model-manager*` (they were `<release>-model-downloader*`). An in-place upgrade prunes the old-name objects, and the catalog PVC is chart-owned with no keep annotation: export the catalog JSON first if you edited it — the new PVC re-seeds from `catalog.githubUrl`.
- **Rollback:** re-import the previous chart version and re-apply your values. Downloaded models live on `models-pvc` / S3 and survive app downgrades. Two caveats after the rename: an older app selects `managed-by=model-downloader` only, so Jobs created by this release are invisible to it until it is rolled forward again; and the old chart points at the old image repo (`ghcr.io/ai-solution-eng/model-downloader`), which the rename does not delete — keep that package published for rollback.
- **Uninstall:** delete the app in PCAI. The release's Deployment, Service, VirtualService, AuthorizationPolicy, RBAC, ConfigMaps and CronJob go with it. **Downloaded models stay** on `models-pvc` and in S3 — intentional; deleting weights is a separate, audited act. The `aioli-db-password` Secret is untouched. Finished Jobs have already self-cleaned via `ttlSecondsAfterFinished`. The catalog PVC and its entries go with the release — export the catalog JSON first if you need it elsewhere.
- **Migrating from `hpe_proxies` (removed flag):** proxy env is now the per-key `proxy:` dict — each key wired only when non-empty; `pcai.enabled` no longer follows any proxy flag and **defaults to false — set it explicitly**; `downloader.hf.verifyTls` defaults to verify-on, so the TLS bypass happens only with an explicit `false`.

### Plain Helm (developer path — not the PCAI path)

```bash
helm lint helm
helm template <release> helm -f helm/values-examples/values.g2.yaml   # eyeball the render
helm upgrade --install <release> helm -n <namespace> -f helm/local/values.<site>.yaml
```

`${DOMAIN_NAME}` stays a literal string in the render — pass `--set ezua.domainName=<real-domain>` on a non-PCAI cluster to check ingress wiring. The Secrets from §1.1 must exist before install, exactly as on PCAI.

## 5. Troubleshooting

Symptom → cause → fix. The rows are real failure histories from this package's design notes, values comments and code guards.

| Symptom | Cause | Fix |
|---|---|---|
| Job denied: `k8s API returned 400/403: ... prevent-unauthorized-create-with-mlis: Insufficient authorization to set the 'hpe-ezua/app' label to 'mlis'` | The platform Kyverno `protect-models-pvc` policy denies the Job's pod; on hosted trials the customer policy lacks the job-controller exemption | Customer admin applies the `NotEquals` patch in §1.2; the chart's Jobs carry the exact label the policy wants |
| Chart install/upgrade fails: `Valid .Values.ezua.virtualService.endpoint is required !` | `ezua.enabled: true` (the default) with an empty endpoint | Set `ezua.virtualService.endpoint: model-manager.${DOMAIN_NAME}` — leave the placeholder |
| Delete/scan/debug Job pod Pending forever, events show `persistentvolumeclaim not found` | The Job was created in a namespace that does not own `models-pvc` | `defaultNamespace` must be the PVC-owning `project-user-*` namespace; update the chart (fixed templates run the delete Job in the PVC's namespace) |
| Manual-deletion commit dies with `template is missing` | Chart and app image are skewed — the Job-template ConfigMap lacks `manual-delete-job.yaml` because the chart is older than the feature | Re-import the current chart so ConfigMap and image ship together (§4) |
| NIM mirror: `ImagePullBackOff` / `401 Unauthorized` from `nvcr.io` | Bad or expired NGC key; or a local-registry image with no pull Secret | Re-submit with a valid `nvapi-...` key; set `nim.imagePullSecrets` for a local registry |
| Mirror succeeds but the NIM deployment still downloads from NGC | The serving deployment lacks `NIM_REPOSITORY_OVERRIDE` — or has a prefix in it | Set exactly `s3://<bucket>` (bucket root, no prefix) plus S3 credentials, region, and `AWS_S3_USE_PATH_STYLE=true` on MinIO/Ceph |
| GC report shows everything protected; `gc.dryRun: false` deletes nothing | The GC CronJob runs the scanner image with no explicit service account, so its live-Job and AIOLI cross-checks fail safe to "keep everything" (known limitation — [FEATURES.md](FEATURES.md#storage-gc-ttl-eviction)) | Confirm with the `gc-probe` command in FEATURES; the fix (app image + service account) is chart work — do not tune values to work around it |
| Submission refused (422) with bytes-needed vs bytes-free / used-vs-quota math | Preflight or quota refusal — working as designed | Free space, raise `quota.namespaces.<ns>`, or tune `preflight.safetyMargin`; unknown sizes degrade to a warning, never to a refusal |
| Namespace dropdown empty; `/api/namespaces` returns 403 | The app ServiceAccount lost namespace-`list` (cluster policy trimmed the chart's ClusterRole, or the binding was removed) | Restore the chart-rendered ClusterRole/Binding; users can still type a namespace manually in the form |
| Deletion page: 403 "no identity header; this deployment is not behind oauth2-proxy, set manualDelete.auth.mode=secret" | The page was reached outside the SSO gate, or the install has no oauth2-proxy | Reach the UI through `model-manager.${DOMAIN_NAME}`; or switch to `manualDelete.auth.mode: secret` plus the `model-delete-key` Secret |
| Deletion page: 403 naming token verification while `oidc.enabled: true` | The bearer token is missing or does not verify — wrong `oidc.issuer` (claim-compared) or `oidc.jwksUrl` | Set `issuer` to the exact Keycloak realm URL and `jwksUrl` to the in-cluster Keycloak certs endpoint (§1.3) |
| After an app restart, in-flight rows vanished from the Jobs table | Queue state is in-process memory (single replica by design) | Expected: submitted Kubernetes Jobs keep running and self-clean; still-running Jobs reappear via reconciliation |
| Push to MLIS fails with a database error; GC report says the AIOLI cross-check is unavailable | The `aioli-db-password` Secret is missing, or the database is unreachable from the cluster | Create the Secret (§1.1) and prove reachability (§1.3); both paths fail closed rather than write blind |

## 6. Developer path: sanitary checks and plain Helm

This section exists so it is never mistaken for the deployment path. To eyeball a render before touching PCAI:

```bash
helm lint helm
helm template helm -f helm/values-examples/values.g2.yaml
helm template helm -f helm/values-examples/values.hosted-trial.yaml
```

Both example files are full values documents and render standalone. The full test suite (`PYTHONPATH=src python3 -m pytest tests/ -q`, with `helm` on PATH) renders the chart and asserts which templates appear in each feature state — see [tests/README.md](../tests/README.md).
