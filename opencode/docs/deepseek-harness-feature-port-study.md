# Porting deepseek-harness features to the opencode chart — study

> **STATUS: IMPLEMENTED** (chart 1.2.0). All four features are in the working tree:
> readable slugs, preview host from values, platform integration (opt-in via
> `platformIntegration.enabled`), and the baked user image
> (`docker/user/Dockerfile` + `scripts/build-user-image.sh` +
> `scripts/test-user-image.sh`). Verified: router/preview JS compiles clean
> (JavaScriptCore), and the baked-image init-guard logic passes all four
> contract scenarios (empty PVC → baked path; matching PVC respected;
> version mismatch → runtime fallback; chamber-mode fallback).

This document maps exactly what has to change in `opencode` (chart `opencode-web-helm`)
to reach parity with `deepseek-harness` (chart `dsh-web-helm`) for:

1. **Preview URLs built from the deployed host** — the port watcher / `preview-url`
   print `https://<subdomain-from-helm-values>/…` instead of a hardcoded `opencode.<domain>` host.
2. **SSO users get their unit inside their platform project namespace** (`project-user-<username>`)
   with the Kubeflow platform PVCs (`kubeflow-shared-pvc`, `user-pvc`) attached and surfaced
   in the Data Manager.
3. **Readable per-user URLs** — `/francesco-caliva/…` (the username shown on the admin page)
   instead of `/u-<12-hex-hash>/…`.
4. **Baked user image** — the apt toolchain, `opencode-ai`, `@openchamber/web`, uv and ttyd
   are baked into one image (e.g. `ghcr.io/ai-solution-eng/opencode-1.18.11-openchamber-1.17.2:0.0.1`)
   so pods stop installing everything at boot and reach Ready in seconds.

dsh implemented these in three layers (all verified against its git history):

| dsh change | Commit / version | Feature |
|---|---|---|
| `fixed bug on preview link generation` (opencode pkg, Sep 11) | opencode `84611caa` | 1 (partial: runtime env, but still hardcoded `opencode.` subdomain + `/u-` prefix) |
| RUNTIME_REV 23 — `PREVIEW_UI_HOST` passthrough | dsh `f5ed94b0` port | 1 |
| RUNTIME_REV 24 — readable slugs, legacy migration | dsh chart 0.3.5 | 3 |
| RUNTIME_REV 25 — platform integration "Option B" | dsh chart 0.4.0 | 2 |
| 0.4.1–0.4.6 hardening (crash-loop fix, anchor-scan uninstall, VAST quota claim-name scheme) | dsh `5b140aed`, `439efc15` | 2 (production fixes) |
| RUNTIME_REV 26 — baked user image (`docker/user/Dockerfile`, `scripts/build-user-image.sh`, guarded `/opt/dsh` fallbacks) | dsh chart 0.4.7 | 4 |

Current opencode state: working tree == commit `84611caa`'s `opencode` subtree (`f7c6a45d`),
chart 1.1.0, none of the three features' final form present.

---

## Feature 1 — preview host from helm values

### Problem today (opencode)
- `templates/configmap-preview.yaml` `port_watcher.mjs`:
  `url(port)` returns `` `https://opencode.${domain}/u-${userSlug}/__preview/${port}/` ``
  with `domain = process.env.PREVIEW_DOMAIN_SUFFIX || '{{ .Values.previewDomain }}'`.
  The `opencode.` subdomain is **hardcoded**; if the release is deployed on a different
  endpoint host (what `ezua.virtualService.endpoint` actually says), the printed/broadcast
  URL is wrong.
- `preview-url` shell script: `DOMAIN="${PREVIEW_DOMAIN_SUFFIX:-}"` then
  `echo "https://opencode.${DOMAIN}/u-$USER_SLUG/__preview/$1/"` — same hardcoding.

### dsh implementation (target)
- `templates/deployment.yaml` — router container gets:
  ```yaml
  - name: UI_ENDPOINT_HOST
    value: {{ .Values.ezua.virtualService.endpoint }}
  ```
- `templates/configmap-router.yaml`:
  ```js
  // Rendered ONLY as an env fallback — never into JS template literals —
  // because the platform pipeline ships it as a literal "${DOMAIN_NAME}" placeholder.
  const uiEndpointHost = process.env.UI_ENDPOINT_HOST || 'deepseekharness.${DOMAIN_NAME}'
  ```
  and stamps it into every user pod: `{ name: 'PREVIEW_UI_HOST', value: uiEndpointHost }`.
- `templates/configmap-preview.yaml`:
  ```js
  const host = process.env.PREVIEW_UI_HOST || `dsh.${domain}`
  return `https://${host}/${userSlug}/__preview/${port}/`
  ```
  ```sh
  UI_HOST="${PREVIEW_UI_HOST:-dsh.${DOMAIN}}"
  echo "https://${UI_HOST}/$USER_SLUG/__preview/$1/"
  ```
  Result: the watcher prints `[preview] Preview available for port 9090: https://<endpoint>/…`
  where `<endpoint>` is the chart value.

### Changes for opencode
1. `templates/deployment.yaml`: add `UI_ENDPOINT_HOST` env (= `.Values.ezua.virtualService.endpoint`).
2. `templates/configmap-router.yaml`: add the `uiEndpointHost` const (fallback
   `opencode.${DOMAIN_NAME}`) and add `{ name: 'PREVIEW_UI_HOST', value: uiEndpointHost }`
   to the user-pod main-container env (next to `PREVIEW_DOMAIN_SUFFIX`).
3. `templates/configmap-preview.yaml`: `url()` and `preview-url` use `PREVIEW_UI_HOST`
   with fallback `opencode.${domain}`. **Keep the Helm value out of the JS template
   literal** (pipeline placeholder rule, already learned once in commit `84611caa`).

Size: small (3 files, ~15 lines).

---

## Feature 3 — username as the per-user URL prefix

### dsh implementation (chart 0.3.5, RUNTIME_REV 24)
All in `templates/configmap-router.yaml`:

- **Readable slug**:
  ```js
  const slugMaxLength = 54           // "dsh-user-" + slug must stay DNS-1123 (<= 63)
  const slugReservedNames = new Set(['healthz', 'terminal', 'term', 'u'])
  function slugifyUsername(userId) {
    const cleaned = (userId || '').toLowerCase().replace(/[^a-z0-9]+/g, '-')
      .replace(/-+/g, '-').replace(/^-+|-+$/g, '')
    if (!cleaned) return ''
    if (cleaned.length > slugMaxLength) {
      return cleaned.slice(0, slugMaxLength - 7).replace(/-+$/, '')
        + '-' + crypto.createHash('sha256').update(userId.toLowerCase()).digest('hex').slice(0, 6)
    }
    return cleaned
  }
  function legacySlugForUser(userId) {           // pre-0.3.5 hash, kept for bookmarks
    return crypto.createHash('sha256').update(userId.toLowerCase()).digest('hex').slice(0, 12)
  }
  function slugForUser(userId) {
    const readable = slugifyUsername(userId)
    if (!readable || readable.startsWith('__') || readable.startsWith('u-') || slugReservedNames.has(readable)) {
      return legacySlugForUser(userId)
    }
    return readable
  }
  ```
- **Legacy compatibility**: every lookup that matches an environment by slug
  (`deploymentForSlug`, `podForSlug`, `claimWarmForUser`, admin listing) also accepts the
  `legacySlugForUser(username)`; `deleteUserEnvironment(slug, legacySlug)` cleans up both.
- **One-time migration** in `resolveUserEnvironment`: if a dedicated environment exists
  under the legacy hash slug, retire the legacy deployment/service/lease/VS and re-ensure
  the deployment under the readable name **mounting the legacy claims** (PVCs cannot be
  renamed; delete legacy deployment first so RWO volumes detach).
- **Routing**: `/u-{12-hex}` continues to match (legacy bookmark route, strict: only the
  owner). A second, readable route `^\/([a-z0-9][a-z0-9-]{0,53})(\/.*)?$` serves
  `/{username}/…` — but a single-segment match that is *not* the caller's slug must
  **fall through** to the session-passthrough/login handling (never swallow
  root-relative pod paths like `/terminal`, `/data_manager`). The per-request body is
  factored into `serveUserPath(request, response, identity, prefix)`.
- `redirectToPod` → `location: /${slug}`; `targetForUserPath(…, prefix)` now takes the
  browser-visible prefix (readable or legacy) for `forwardedPrefix`/`stripSlugPrefix`;
  WebSocket `upgrade` handler handles both forms with the same rules.
- **Pod re-template**: `userTemplateVersion` bump (`terminal-tab-v8-…` → new rev) so
  existing pods get re-stamped `USER_SLUG` / `SESSION_KEY` (the in-pod auth validator
  derives the cookie key from the slug). Users stay logged in (session cookie is
  identity-based).
- In-pod HTML links: `terminal_manager.js` replaces `__OPENCODE_HOME__` with
  `/${USER_SLUG}/` (was `/u-${USER_SLUG}/`); same for the data-manager header link.
- Admin page gains an **Open** button (`window.location.href = '/' + slug`) and the
  unknown-row suppression also accounts for legacy slugs.

### Changes for opencode
Mirror the above in `opencode/templates/configmap-router.yaml` (labels/names keep the
`opencode-` prefixes: `opencode-user-<slug>`, `opencode-user-vs-<slug>`,
`opencode-user-managed`, …) and in `opencode/templates/configmap-preview.yaml`
(`terminal_manager.js` `__OPENCODE_HOME__` replacement, data-manager "To Login"/home links).
Reserved names stay the same plus `u`; fallback host unchanged.

Size: medium (2 files, ~120 lines + README URL-scheme table update).

---

## Feature 2 — SSO units in the platform project namespace + Kubeflow PVCs

dsh calls this "Platform integration (Option B)" (chart 0.4.0 + hardening to 0.4.6).
Semantics:

- With `platformIntegration.enabled: true`, an **SSO** user whose platform project
  namespace exists (`project-user-<username>`, created by the ezprojects-operator,
  verified via the namespace's `project-info` ConfigMap carrying `USER_INFO_UID/GID/
  USERNAME/GROUP/HOMEDIR`) gets the unit Deployment/Service/DSH-PVCs **born inside that
  namespace** (K8s objects can't move namespaces; sticky semantics preserved — existing
  release-namespace units are never moved).
- The platform PVCs are mounted **by reference** (never labeled, owner-referenced, patched
  or deleted by the chart): `kubeflow-shared-pvc → /mnt/shared` (cluster-wide shared,
  RWX semantics) and `user-pvc → /mnt/user` (personal project volume).
- The unit runs under the **project's unix identity**: init chowns DSH-owned volumes
  owner-mismatch-only, the whole service stack (ttyd, watcher, data manager, terminal
  manager, supervisor) drops to `USER_INFO_UID/GID` via `setpriv`, so files written are
  byte-identical to notebook-written files.
- Data Manager: two extra conditional roots — `Platform Shared (Kubeflow)` and
  `Platform Workspace (Kubeflow)` — shown only when mode = `mounted`; `pending` units are
  re-stamped by the router once the platform PVCs bind (`…/platform-mounts` annotation +
  convergence check). Optional `readOnly` mode blocks writes through the Data Manager.
- Cross-namespace RBAC: gated `ClusterRole`/`ClusterRoleBinding` for router + cleanup SAs
  (pods, PVCs, services, configmaps, deployments, jobs; namespaces get). Secrets, leases,
  VirtualServices stay release-namespace-only.
- Ownership & uninstall contract: per-unit **anchor ConfigMap** `dsh-anchor-<slug>` in the
  unit namespace; every DSH-created object there is owner-referenced to it; deleting a user
  (or `helm uninstall`) cascades through anchors + label-scoped deletes with a
  cluster-wide discovery sweep, and a **skip-assert refuses any PVC carrying an
  `ezprojects.hpe.com/*` label**. Platform PVCs and namespaces are never deleted.
- Release ConfigMaps mounted by the pod template (project-config, personal-content,
  preview-config, shared-content) are **mirrored** into the unit namespace (cross-namespace
  volume mounts are impossible), rate-limited sync, owner-anchored.
- Per-namespace **shared PVC**: platform units can't mount the release-level shared PVC, so
  each unit provisions its own `<release>-shared-pvc` (RWX) in the unit namespace, seeded
  via a generated `seed.sh` from the mirrored shared-content ConfigMap (`cp -n` seed-once).
- VAST quota hardening (0.4.4–0.4.6, directly relevant once units live in long-named
  `project-user-*` namespaces): per-user claim names embed the nameVersion and
  volume-kind EARLY (`dsh-<ver>-ws-<slug>` / `dsh-<ver>-st-<slug>`) because VAST quota
  names are `csi:<ns>:<pvc>` truncated to 64 chars; claims are part of the unit
  convergence contract via the `…/claims` annotation; 0.4.1 fix: provision-watch must pass
  namespace separately from name and must never reject (router crash-loop).

### Changes for opencode, file by file

1. **`values.yaml`** — add the `platformIntegration` block (default `enabled: false`,
   `namespacePrefix: "project-user-"`, `volumes.shared: {claim: kubeflow-shared-pvc,
   mountPath: /mnt/shared}`, `volumes.user: {claim: user-pvc, mountPath: /mnt/user}`,
   `readOnly: false`). Mirror the same block into `values-g2.yaml` with `enabled: true`
   (as dsh's g2 values do). Chart version bump in `Chart.yaml`.
2. **`templates/deployment.yaml`** — router env: `UI_ENDPOINT_HOST` (feature 1),
   `SHARED_WORKSPACE_SIZE`, and the gated `PLATFORM_*` block
   (`PLATFORM_INTEGRATION_ENABLED`, `PLATFORM_NAMESPACE_PREFIX`, `PLATFORM_SHARED_CLAIM`,
   `PLATFORM_SHARED_MOUNT`, `PLATFORM_USER_CLAIM`, `PLATFORM_USER_MOUNT`,
   `PLATFORM_VOLUMES_READONLY`).
3. **`templates/rbac.yaml`** — append the gated platform `ClusterRole` +
   `ClusterRoleBinding` for the router SA (exact rules from dsh: pods g/l/d; PVCs
   g/l/create/delete; services g/l/create/patch/update/delete; configmaps
   g/l/create/update/delete; namespaces get; deployments g/l/create/patch/update/delete;
   jobs g/l/create/delete).
4. **`templates/configmap-router.yaml`** (the bulk, ~500 lines) — port:
   - consts: `platformEnabled`, `platformNsPrefix`, `platformSharedClaim/Mount`,
     `platformUserClaim/Mount`, `platformReadOnly`, `sharedWorkspaceSize`,
     `unitConfigMapNames` (release ConfigMap list), `platformContextCache`,
     `mirrorSyncCache`;
   - `slugifyUsername`/`legacySlugForUser`/`slugForUser` (feature 3) and
     `namesForSlug(slug, unitNs)` gaining `namespace`;
   - namespace-aware variants: `waitForDeploymentGone(…, ns)`, `podForSlug(…, ns)`,
     `jobPodPhase(…, ns)`, `listUserDeployments(ns)`, `knownUnitNamespaces()`,
     `sweepStaleClearJobs()` and `fetchEnvironmentSnapshot()` looping namespaces;
   - `isPlatformOwnedClaim` tripwire + `anchorNamespacesForSlug` cluster-wide anchor scan;
     rewritten `deleteUserEnvironment(slug, legacySlug)` (candidates, registry + anchor
     namespaces, anchor-first GC, claims derived from the deployment spec, per-ns shared
     claim deleted only for non-release namespaces);
   - `ensurePersistentVolumeClaim(name, size, ns, ownerRefs, accessModes)` (RWX for the
     per-namespace shared claim), `userServiceManifest/upsertService/ensureService`
     ns+ownerRefs;
   - `userDeploymentManifest`: `platform` param → `platformIdentityEnv`,
     `platformVolumes`, `platformVolumeMounts`, `platformRootsMode` env
     (`OPENCODE_PLATFORM_ROOTS`/`OPENCODE_PLATFORM_ROOTS_READONLY`), `dsh-platform-unit`
     label, `opencode-web-helm/platform-mounts` + `opencode-web-helm/claims`
     annotations, namespace + ownerReferences in metadata, identity-aware init
     (chown-once, `$RUN_AS_USER` wrapping, root-owned chmod reconciliation from 0.4.6)
     and the `start_services()` setpriv block in the startup script;
   - `upsertUserDeployment`/`ensureDeployment`: ns-aware GET/POST/PATCH, convergence on
     `platform-mounts` + `claims`;
   - platform context: `platformNamespaceForUser`, `fetchPlatformContext` (project-info
     ConfigMap + both platform PVCs Bound), `platformContextForUser` (60 s cache,
     403-safe);
   - anchor + mirror: `anchorManifest`, `ensureUnitAnchor`, `ownerRefToAnchor`,
     `syncUnitConfigMaps`, `syncUnitConfigMapsRateLimited`;
   - `resolvePlatformEnvironment` + the platform branch (and the feature-3 legacy
     migration) in `resolveUserEnvironment`;
   - `targetForUserPath`/`targetForPassthrough` use `names.namespace`;
   - `startProvisionWatch`: `${ns}/${name}` task key + never-rejecting watch (0.4.1);
     `handleSetupStatus` uses the same key;
   - user-pod env: `PREVIEW_UI_HOST` (feature 1), `…_PLATFORM_ROOTS*`, platform identity
     env;
   - admin API/page: platform-unit lookup via `record.unitNs`, `namespace` field,
     `Open` button, legacy-slug suppression; `deleteUserEnvironment(slug, legacySlug)`
     call sites.
5. **`templates/configmap-preview.yaml`** — `data_manager.mjs`: `BASE_ROOTS` +
   `PLATFORM_ROOTS`, `OPENCODE_PLATFORM_ROOTS(_READONLY)` envs, `rebuildRoots()` with the
   60 s self-heal interval, `platformWriteBlocked()` guard on every write path
   (upload/save/mkdir/delete/move), statfs label `— all users` for the cluster-wide
   platform share, `platform: !!r.platform` in the roots/storage payloads, select-label
   handling; plus the feature-1/3 URL changes above.
6. **`templates/configmap-shared.yaml`** — add the generated `seed.sh`
   (`SEED_SRC`/`SEED_DST`, `cp -n` per-file seed-once) used by platform units.
7. **`templates/pre-delete-cleanup.yaml`** — cleanup SA gets configmaps get/list/delete in
   the release Role; gated cleanup `ClusterRole`/`ClusterRoleBinding` (hook weights −3/−2);
   job env `REGISTRY_SECRET`; `isPlatformOwned` skip-assert; `unitNamespaces()` from the
   registry; Phase 0b cluster-wide discovery; all phases loop over the namespace list.
8. **`README.md`** — URL scheme (`/{username}`), platform-volumes section,
   requirements (cross-ns RBAC sign-off, SSO host registration), troubleshooting rows.

Size: large (~700–800 lines across 8 files), but mechanical — dsh's
`templates/configmap-router.yaml`, `configmap-preview.yaml`, `pre-delete-cleanup.yaml`,
`rbac.yaml`, `deployment.yaml` and `values.yaml` are the reference implementation, and the
two charts share the same router architecture (only `dsh-`→`opencode-` label/path prefix
and `/var/dsh`→`/var/opencode` differ).

---

## Feature 4 — baked user image (toolchain in the image, not installed at boot)

### Problem today (opencode)
User pods run vanilla `node:22-bookworm-slim` and the init container installs everything on
boot: the apt toolchain (`provisioning.aptPackages`), `opencode-ai@${OPENCODE_VERSION}`,
`@openchamber/web@${OPENCHAMBER_VERSION}` (when `ui.mode: openchamber`), uv and ttyd.
Several minutes per fresh pod, and the apt stages re-run on EVERY restart (apt state lives
in the container layer, not on the PVCs). Warm-pool claim restarts also pay the apt cost.

### dsh implementation (chart 0.4.7, RUNTIME_REV 26) — reference files
- **`docker/user/Dockerfile`** — bakes everything under **`/opt/dsh`** (`/opt/dsh/npm`,
  `/opt/dsh/bin`), never under `/var/dsh` (the state PVC mounts there and would shadow
  image content). Design contract in its header:
  1. baked artifacts under `/opt/dsh` only;
  2. `APT_PACKAGES` / `DSH_VERSION` / `TTYD_VERSION` build args must mirror `values.yaml`
     (`provisioning.aptPackages`, `dsh.version`) and the `ttydVersion` const in
     `configmap-router.yaml` — the build script reads the values file so there is one
     source of truth.
  The apt layer reproduces the startup's alias block (`EXTERNALLY-MANAGED` removal,
  `vim`/`fd`/`python`/`pip`/`xdg-open` symlinks) so the startup's `command -v` guards
  short-circuit and no apt runs at boot.
- **`scripts/build-user-image.sh`** — `docker buildx build` wrapper: reads
  `dsh.version` + `provisioning.aptPackages` from the selected values file, passes them as
  build args, tag defaults to the app version, flags `--tag/--values/--ttyd-version/
  --platform/--repo/--push/--load`. Resolves the docker CLI + credential-helper PATH for
  non-interactive shells.
- **`scripts/test-user-image.sh`** — 4-scenario sanity test with no cluster needed:
  (1) baked artifacts exist in the image; (2) replaying the router's REAL init guards
  (extracted from `templates/configmap-router.yaml` — no drift) against an EMPTY simulated
  state PVC takes the baked path (PVC aliases created, nothing installed); (3) an existing
  version-matching PVC copy is respected, not clobbered; (4) a version-mismatched PVC
  falls back to the runtime npm install.
- **`templates/configmap-router.yaml`**:
  - `userImageRev` derived from the image tag and folded into the convergence key:
    ```js
    const userImageRev = (() => { const tag = String(userInstanceImage||'').split('@')[0].split(':').pop() || ''; return (tag.replace(/[^a-zA-Z0-9._-]/g,'').slice(0,32)) || 'untagged' })()
    const userTemplateVersion = `dsh-web-v${RUNTIME_REV}-${[...new Set([userDshVersion, userImageRev])].join('-')}`
    ```
    → a `helm upgrade` with a new tag re-stamps every existing unit (dedicated + warm).
  - guarded init fallbacks (all still work on vanilla `node:22-bookworm-slim`):
    - uv: download only when neither `/var/dsh/bin/uv` nor `/opt/dsh/bin/uv` exists;
    - npm: if `/opt/dsh/npm/.dsh-version` matches `${DSH_VERSION}` → on a FRESH state PVC
      alias `ln -sfn /opt/dsh/npm /var/dsh/data/npm` + `/usr/local/bin` symlink and skip
      the install; else the original install-into-the-PVC path (version marker compare);
    - ttyd: download only when neither PVC nor `/opt/dsh/bin/ttyd` has one;
    - compat loop `for b in uv uvx ttyd`: alias baked → `/var/dsh/bin/<b>` when the PVC
      copy is missing or not executable (PVC copies win when present).
  - `imagePullPolicy: IfNotPresent` on user containers; immutable tags (no `latest`).
- **`templates/deployment.yaml`** — router env `USER_IMAGE_PULL_SECRET`
  (= `.Values.images.pullSecret`); the pod template adds
  `imagePullSecrets: [{ name: userPullSecret }]` when set (private ghcr packages).
- **`values.yaml`** — `images.user`/`images.init` point at the baked image;
  `images.pullSecret: ""`; comment documents the rebuild rules and the revert path
  (point the values back at `node:22-bookworm-slim` → install-on-boot resumes).

### Changes for opencode
1. **New `docker/user/Dockerfile`** (adapted from dsh, two npm packages instead of one):
   base `node:22-bookworm-slim`; build args `OPENCODE_VERSION=1.18.11`,
   `OPENCHAMBER_VERSION=1.17.2`, `TTYD_VERSION=1.7.7` (mirrors the router const),
   `APT_PACKAGES` (mirrors `provisioning.aptPackages`); layers:
   - apt toolchain + the alias block the startup recreates;
   - `npm install -g --silent opencode-ai@${OPENCODE_VERSION} --prefix /opt/opencode/npm`
     + marker `/opt/opencode/npm/.opencode-version`;
   - `npm install -g --silent @openchamber/web@${OPENCHAMBER_VERSION} --prefix
     /opt/opencode/npm` + marker `/opt/opencode/npm/.openchamber-version` (bake BOTH
     packages unconditionally so one image serves `ui.mode: opencode` and `openchamber`);
   - uv + ttyd under `/opt/opencode/bin`;
   - `/usr/local/bin` symlinks for `opencode`/`openchamber`/`uv`/`uvx`/`ttyd`.
2. **New `scripts/build-user-image.sh`** — dsh's script adapted:
   reads `opencode.version`, `openchamber.version` and `provisioning.aptPackages` from the
   selected values file; **image repo name encodes both app versions** per the requested
   convention — `ghcr.io/ai-solution-eng/opencode-${OPENCODE_VERSION}-openchamber-${OPENCHAMBER_VERSION}`
   — and the tag is the image revision (`--tag`, default `0.0.1`), so an app-version bump
   naturally mints a new repo while `--tag` iterates image-only fixes.
3. **New `scripts/test-user-image.sh`** — dsh's 4-scenario test adapted (guard extraction
   patterns: `/opt/opencode/npm/.opencode-version`, `.openchamber-version`,
   `astral.sh/uv/install.sh`, `ttyd/releases/download`, `for b in uv uvx ttyd`).
4. **`templates/configmap-router.yaml`**:
   - `userImageRev` + folded `userTemplateVersion`
     (`terminal-tab-v10-opencode-${[...new Set([userOpencodeVersion, userOpenChamberVersion, userImageRev])].join('-')}`) —
     existing units re-stamp on image change;
   - guarded init fallbacks mirroring dsh, generalized to the two packages:
     alias `/var/opencode/data/npm → /opt/opencode/npm` (fresh PVC only, `/usr/local/bin`
     symlinks for both binaries) **only when BOTH baked markers match**
     `${OPENCODE_VERSION}`/`${OPENCHAMBER_VERSION}`; otherwise the current
     install-into-the-PVC guards run unchanged (note the current guards install into the
     same `/var/opencode/data/npm` prefix — keep that);
     uv/ttyd guards gain the `/opt/opencode/bin` checks; add the
     `for b in uv uvx ttyd` compat-alias loop;
   - `userPullSecret` env → pod `imagePullSecrets`.
5. **`templates/deployment.yaml`** — add `USER_IMAGE_PULL_SECRET` router env.
6. **`values.yaml` + `values-g2/devday/ht`** — `images.user`/`images.init` →
   `ghcr.io/ai-solution-eng/opencode-1.18.11-openchamber-1.17.2:0.0.1` (per-env files can
   override), `images.pullSecret: ""`.
7. **`README.md`** — "Baked user image" section (problem, `/opt/opencode` contract,
   guarded fallbacks, build/push via the script, immutable tags + pullSecret note).

Size: medium (2 new files + ~60 lines across chart files). **Independent of features
1–3** — but share the `userTemplateVersion`/`RUNTIME_REV` bump so one release rolls
everything at once.



## Implementation order & compatibility

1. **Feature 4** first — isolated, immediately valuable (boot time drops from minutes to
   seconds for every fresh pod and warm-claim restart), and its `userTemplateVersion`
   bump rides the same release as the other features.
2. **Feature 1** second (small, no data migration).
3. **Feature 3** third — implement `slugifyUsername` once (feature 2's
   `platformNamespaceForUser` reuses it); keep the legacy `/u-{hash}` route + migration so
   existing bookmarks and pre-existing environments keep working.
4. **Feature 2** last — biggest surface; includes the 0.4.1–0.4.6 hardening
   (never-rejecting provision watch, anchor-scan uninstall, VAST-safe claim names,
   RWX per-namespace shared PVCs) since per-user namespaces make those constraints real.
5. Bump `userTemplateVersion` (e.g. `terminal-tab-v8-…` → `terminal-tab-v10-…`) once for
   the whole release so running units re-template to the new `USER_SLUG`/`SESSION_KEY`/
   env/image; users stay logged in.
6. `values.yaml` ships `platformIntegration.enabled: false` (opt-in); enable it in
   `values-g2.yaml`.
7. Requirements to flag: cross-namespace ClusterRole needs platform-admin sign-off; the
   SSO host registration stays out-of-band (unchanged); warm pool stays release-namespace
   only (SSO users cold-start once).

## Verification plan
- `helm lint` / `helm template` per values file (devday/g2/ht) to catch rendering.
- Image: `scripts/test-user-image.sh` (4 scenarios: baked artifacts present; empty PVC →
  baked path; matching PVC respected; mismatched PVC → runtime npm fallback), plus a boot
  smoke of both `ui.mode` values against the baked image.
- Regression: local (non-SSO) login → release-namespace unit, `/u-{hash}` and `/{username}`
  both route; admin Open/rename/delete/reset flows.
- Migration: an environment created under the legacy slug is adopted with its volumes.
- Platform: SSO login with `project-user-<name>` present + PVCs Bound → unit in that
  namespace, `/mnt/shared` + `/mnt/user` visible in Data Manager; pending→mounted
  re-stamp; delete-user and `helm uninstall` leave platform PVCs untouched.
- Preview: start a server on a port → watcher prints `https://<endpoint>/…` URL.
