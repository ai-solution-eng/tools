# DeepSeek Harness Web — Multi-User on HPE Private Cloud AI

Deploy and manage multiple isolated [DeepSeek Harness (`dsh`)](https://github.com/deepseek-ai/deepseek-harness) coding-agent environments on **HPE Private Cloud AI**. Each user gets their own PVC-backed workspace, and a shared workspace enables team collaboration.

Port of the `opencode` chart (`opencode-web-helm`): same router, warm pool, admin console, terminal, data manager and preview machinery — with the [`dsh web`](https://github.com/deepseek-ai/deepseek-harness) UI in place of opencode/OpenChamber. **OpenChamber is not used.**

Chart: `dsh-web-helm` · current version **0.4.14** (dsh `0.1.7-rc.2`, Node 22 image).
0.4.4 fixes the VAST quota-collision hang for long usernames: quota names are `csi:<ns>:<pvc>`
**truncated to 64 chars** — with a long namespace (e.g. `project-user-alejandro-morales-martinez`)
only the first ~20 chars of the PVC name survive, so a version *suffix* was invisible to VAST and
re-provisions collided with orphaned quotas ("Quota name must be unique per tenant") → workspace
PVC Pending forever. Per-user PVC names now embed the version **early**
(`dsh-<nameVersion>-<slug>-workspace-pvc`; deployment/service names unchanged) so every
nameVersion bump mints a distinct quota name; units converge via the `dsh-web-helm/claims`
annotation — old claims are left behind and can be label-deleted manually. Per-namespace shared
PVCs are also provisioned RWX now (matching the release-level shared PVC).
0.4.1 fixes a router crash-loop on the first platform (SSO) login: the provision-watch key
(namespace/name composite) leaked into the deployment-name position of the API path, producing a
malformed request whose rejection crashed the process; the watch now passes name and namespace
separately and never rejects (failures surface on the setup-status page instead).
0.4.2 makes unit cleanup orphan-proof: user deletion discovers unit namespaces via a
**cluster-wide anchor scan** (`dsh-anchor-<slug>`) in addition to the registry, and the uninstall
hook adds a **cluster-wide labeled discovery phase** — an uninstall now finds every namespace
hosting `dsh-user-managed=true` objects even when the registry entry is already gone (delete
safety unchanged: label-scoped deletes + the `ezprojects.hpe.com/*` skip-assert).
0.4.3 completes the router's platform ClusterRole: `delete` on configmaps (ownership anchors —
user deletion cascades the unit through them) and on pods (stale cleanup-job sweeper).

---

## Platform volumes for SSO users (0.4.0, study Option B)

With `platformIntegration.enabled: true` (on in `values-g2.yaml`), **SSO** users get their unit
deployed **inside their platform project namespace** (`project-user-<username>`) — the namespace
the EzProject operator created for them — with the platform volumes mounted **read-write**:

| Mount | Platform PVC | What it is |
|---|---|---|
| `/mnt/shared` | `kubeflow-shared-pvc` | **one cluster-wide shared filesystem** (every project namespace's PVC binds to the same VAST view); per-user directories live here |
| `/mnt/user` | `user-pvc` | the user's personal project volume (chowned by the platform to `<uid>:<gid>`) |

Local (non-SSO) users keep the classic single-namespace behavior; DSH's own `personal` / `shared` /
`state` volumes and Data Manager roots are unchanged for everyone.

**Identity = the platform's.** The router reads the project's unix attributes from the
`project-info` ConfigMap (`USER_INFO_UID/GID/USERNAME/GROUP/HOMEDIR`), the unit init chowns the
DSH-owned volumes to that identity (owner-mismatch-only, never the platform volumes), and the
service stack (dsh, data manager, ttyd terminals) execs under it via `setpriv`. Files written
from DSH on the platform volumes are therefore **byte-identical to notebook-written files**
(same uid/gid), and modification rights are exactly "wherever the user's platform privileges
allow" — enforced by the kernel, surfaced as clean errors in the UI.

**Data Manager:** SSO users additionally see **Platform Shared (Kubeflow)** and **Platform
Workspace (Kubeflow)** roots (conditional: hidden for local users and while the platform PVCs
are not `Bound`; a unit provisioned in the pending state is re-stamped automatically once they
are). `platformIntegration.readOnly: true` mounts them but keeps the UI read-only for the
platform roots (unix permissions stay the real enforcement).

**Provisioning semantics:** the unit is *born* in the project namespace (K8s objects cannot move
between namespaces) when the user has **no existing unit** — sticky semantics preserved, so an
upgrade never silently moves or resets existing units. A brand-new SSO user cold-starts once
(the platform warm pool only warms classic/local units).

**Requirements (the admin conversation):**
1. **Cross-namespace RBAC** — enabling the flag renders a `ClusterRole` +
   `ClusterRoleBinding` for the router and cleanup service accounts (pods/PVCs/services/
   configmaps/deployments/jobs, read/manifest, plus PVC+configmap delete). Secrets, leases and
   VirtualServices stay release-namespace-only. This is a real security grant — have the
   platform admins sign off (study §7.7).
2. **SSO host registration** at the platform auth layer (out-of-band, as before).

**Uninstall contract (enforced, study §7.6):** `helm uninstall` deletes only DSH-created objects
in the release namespace **and** each unit namespace from the registry — label-scoped
(`dsh-user-managed=true`), GC-cascaded through per-user **anchor** ConfigMaps
(`dsh-anchor-<slug>`) in the project namespaces, with a skip-assert that refuses any PVC carrying
an `ezprojects.hpe.com/*` label. **The platform PVCs are never deleted**; namespaces are never
deleted. Unit ConfigMap mirrors (the pod template mounts them cross-namespace via mirrored
copies) are DSH-owned and cleaned with the unit.

**PVC-survival contract (since 0.4.13, `storage.keepPvcOnDelete: true`):** user data PVCs —
per-user `dsh-*-ws-*/-st-*` claims, warm-pool claims (`dsh-user-warm-N-*-pvc-v2`) and the
per-namespace `dsh-web-helm-shared-pvc` mirror — are never deleted by the app: not via the
anchor GC cascade (PVCs are no longer owner-referenced), not by the admin user-delete flow,
not by uninstall (the hook lists them and keeps them). The release shared PVC carries
`helm.sh/resource-policy: keep` and survives uninstall too. The first re-login / re-install
re-binds the exact same volumes, data intact. Deliberate purge = manual
`kubectl delete pvc -n <ns> <claim>`. Set `storage.keepPvcOnDelete: false` to restore the
legacy delete-on-uninstall behavior.

**Existing-install migration (run once after upgrading to ≥ 0.4.13):**
`scripts/strip-pvc-owner-refs.sh` removes the legacy anchor `ownerReferences` from all
`dsh-user-managed=true` PVCs; without it, pre-existing PVCs would still be GC-cascaded by
anchor deletion.

---

## Install (from-scratch, lessons baked in)

```bash
# 0. BEFORE choosing the endpoint: make sure no other VirtualService claims
#    the host. A duplicate host hijacks routing and shows the browser
#    "403 RBAC: access denied" (this bit us on g2 — "dsh." was claimed by
#    another tenant's 'deepseek-harness' VS).
kubectl get virtualservice -A -o yaml | grep -c "<your-host>."    # expect 0 (or 1 = yours)

# 1. Namespace — MUST NOT inject Istio sidecars. The router talks to the
#    K8s API directly; a sidecar makes namespace AuthorizationPolicies
#    enforce on the gateway→router hop → "403 RBAC: access denied".
kubectl create namespace dsh-web-helm
kubectl label ns dsh-web-helm istio-injection=disabled --overwrite

# 2. Deploy (dshEnv in values seeds $DSH_HOME/.env with CHANGEME keys;
#    edit them after first login via Data Manager → DSH Home → .env)
helm upgrade --install dsh-web-helm ./dsh-web-helm-0.2.0.tgz -n dsh-web-helm -f values-g2.yaml

# 3. Self-check
helm test dsh-web-helm -n dsh-web-helm

# 4. DNS: <endpoint host> → the ingressgateway LB IP
# 5. Log in at https://<host>/ → create users at /__dsh_admin (admin creds in values)
# 6. Replace CHANGEME keys: Data Manager → DSH Home → .env (hot-reloaded)
```

**Warm-pool boot note:** a freshly claimed unit needs ~60–90s for the dsh web process to boot (init re-checks the toolchain in seconds; dsh itself is the slow part). v0.2.0's readiness probe gates on dsh actually answering, so the router never hands you a mid-boot unit — worst case the provisioning spinner runs longer, never a 502.

---

## Access & Authentication

Entry: `https://<endpoint>` (values: `ezua.virtualService.endpoint`; per-env hosts like `dsh-web.${DOMAIN_NAME}`).

| Purpose | URL |
|---|---|
| Login / account | `https://<host>/` |
| Admin console | `https://<host>/__dsh_admin` |
| Personal environment | `https://<host>/{username}` |
| Terminal | `.../{username}/terminal` (ttyd + tmux, tabbed/persistent) |
| Data Manager | `.../{username}/data-manager` (renamed from `/data_manager` in 0.4.14 — the underscore path is still served, aliased in-app) |
| Preview for port `PORT` | `.../{username}/__preview/{PORT}/` |

Since chart **0.3.5** the per-user URL prefix is the sanitized **username** itself (e.g. `/francesco-caliva/`) — usernames are unique in the registry, so the name is a safe identifier. Pre-0.3.5 links of the form `.../u-{12-hex-slug}/...` keep working (legacy bookmark route).

**Local accounts** are admin-provisioned only (self-registration hidden). **Platform SSO is optional** — `ezua.authorizationPolicy.enabled: false` (as in values-g2) deploys no AuthorizationPolicy and the app is gated by the router's own login. With SSO on (`true`), the host must also be registered with the platform auth layer (as opencode's host is) — that registration lives outside Helm.

### How dsh web is exposed (the token bridge)

`dsh web` binds loopback-only (the CLI refuses `--host 0.0.0.0`) and protects `/api` with a Host/Origin fence (`--trusted-host`) plus a per-process **launch token** exchanged for a cookie at `/?token=…`. The chart preserves that model fully:

```
browser ──https──> ezaf-gateway ──> router :8080 ──> pod nginx :8082 ──> dsh web 127.0.0.1:3080
```

1. The supervisor starts `dsh web --no-open --port 3080 --trusted-host <hosts>` and captures the printed `?token=` URL into the state volume.
2. In-pod nginx (:8082) validates the platform session (`auth_request` → auth validator :7684) and preserves the public `Host` header (dsh's fence).
3. First visit without a dsh cookie: dsh's 401 → nginx `error_page` → `@dsh_boot` → `/__dsh_boot` → validator 302s to `/?token=…` (relative!) → dsh sets its own cookie → clean `/`. The token never leaves the pod.
4. The router passes `/?token=…` through to the pod (it is dsh's exchange path — the router's login-page exemption on `/` does not swallow it).

Ports map: **443→8080 router→8082 nginx→3080 dsh** (loopback), 7681 ttyd, 7682 data manager, 7683 terminal manager, 7684 auth validator; `PREVIEW_EXCLUDE_PORTS` covers all of them.

---

## Configuration (settings.yaml, patch, env)

| Field | What it defines |
|---|---|
| `dshSettings` | `$DSH_HOME/settings.yaml` — providers/models/compat/telemetry |
| `dshCordisPatch` | the web profile's `cordis.patch.yml` (MCP integrations etc.), seeded base64, seed-once, watched + YAML-validated |
| `dshEnv` | Seed `.env` written into `$DSH_HOME` on first start — the credential layer (`apiKeyEnv` resolves from it; edit via Data Manager, hot-reloaded) |
| `dsh.version` / `dsh.port` | dsh npm version / loopback port (3080) |

**Hot reload:** the supervisor watches `settings.yaml`, `.env`, `profiles/web/cordis.patch.yml` and `profiles/web/dsh.profile` as FILES and restarts dsh on change — every changed YAML file is validated (js-yaml from the dsh install, `!!js`-aware) before the restart, so a broken edit can never crash-loop dsh. dsh also re-reads settings per request, so most model edits apply without restart. Edit via the Data Manager's **DSH Home** root or the terminal; seeded on first start only, never overwritten.

**AGENTS.md / skills:** no longer seeded by the chart (the `dshInstructions`/`dshSkills` values were removed). dsh still auto-loads a user-placed `/workspace/personal/AGENTS.md`, and skills dropped into `$DSH_HOME/skills/` hot-refresh natively.

**MLIS providers** (default seed): `llm-pi-ai.providers.*` with `api: openai-completions`, `compat.supportsDeveloperRole: false`, `maxTokensField: max_tokens`, and for DeepSeek-V4-style thinking models `compat.thinkingFormat: deepseek` + `reasoningEfforts`. Keys via `apiKeyEnv` → Secret.

**Telemetry:** dsh ships session-log upload and OTel **on by default**; the seed settings disable both (`session-log-deepseek.enabled: false`, telemetry `DISABLED`). Keep off for PCAI.

---

## Workspaces & Data Manager

| Mount | Type | Purpose |
|---|---|---|
| `/workspace/personal` | RWO PVC | Private workspace; dsh's default workspace root |
| `/workspace/shared` | RWX PVC | Team area (seeded from `shared/` with toy demos + venv) |
| `/var/dsh` (state PVC) | RWO PVC | `$DSH_HOME`, npm prefix, ttyd binary, boot state |

Data Manager roots: **Personal**, **Shared**, **DSH Home** (`$DSH_HOME` — settings.yaml, skills, sessions), State (non-navigable). Monaco editor, upload/download/zip, storage panel — path-traversal clamped to the three navigable roots.

---

## Warm Pool

Same semantics as the opencode chart (pre-provisioned Deployment+Service+PVCs per unit, warm-first assignment, Recreate strategy, stuck-unit recreation). Tradeoff: each unit idles up to 2 CPU / 2Gi. `warmPool.enabled/size` control it. Pre-delete hook cleans up all dynamic compute resources on `helm uninstall`; user data PVCs survive by default (`storage.keepPvcOnDelete: true`) and must be purged manually.

---

## Baked user image (0.4.7)

User pods previously ran vanilla `node:22-bookworm-slim` and installed everything at boot (apt toolchain, `@deepseek-ai/dsh`, uv, ttyd — several minutes per pod, and the two apt stages re-ran on EVERY restart because apt state lives in the container layer, not on the PVCs). `images.user`/`images.init` now point at a **baked image** (`ghcr.io/ai-solution-eng/deepseek-harness:<tag>`) that carries the whole toolchain under `/opt/dsh` — fresh pods reach Ready in seconds.

Design contract (see `docker/user/Dockerfile`):

- Baked artifacts live under **`/opt/dsh`** (`/opt/dsh/npm`, `/opt/dsh/bin`) — never under `/var/dsh`, which the state PVC mounts and would shadow. The init container aliases the PVC paths (`/var/dsh/data/npm`, `/var/dsh/bin/{uv,uvx,ttyd}`) to the baked copies with guarded symlinks; `dsh-startup.sh` is unchanged.
- **Every runtime install step remains as a guarded fallback.** On the baked image the guards no-op; set `images.user/init` back to `node:22-bookworm-slim` (or bump `dsh.version` without rebuilding) and the old install-on-boot behavior resumes.
- The image tag is folded into `user-template-version`, so a `helm upgrade` with a new tag re-stamps all existing units (dedicated + warm pool) onto the new image automatically.
- User pods pull with `IfNotPresent` — use immutable tags (no `latest`). If the registry package is private, set `images.pullSecret` to an imagePullSecret in the release namespace (public ghcr.io packages pull anonymously).

Build & push (single source of truth: the script reads `dsh.version` + `provisioning.aptPackages` from the values file; the image tag defaults to `dsh.version` — a version bump and an image rebuild stay coupled):

```sh
docker buildx build --platform linux/amd64 \
  -t ghcr.io/ai-solution-eng/deepseek-harness:0.1.7-rc.2 --push docker/user
# or simply: scripts/build-user-image.sh --push
```

Then `helm upgrade` (the values files already point at the matching tag).

---

## Login lands inside the unit (0.4.8)

The first login after logout used to strand the user on the login menu ("Signed in as … / Open your environment") even though the session was already live: the in-pod validator's boot flow bounced the browser through `/?token=…` on the **main host**, where dsh's own post-exchange redirect to `/` hit the router's menu. The validator now performs the launch-token exchange **server-side** (the same fetch its readiness probe already used), captures dsh's session cookie, and 302s straight into `/<slug>/` — the entire boot flow stays inside the unit. SSO and local logins are now single-click (including through the provisioning loading screen). Validate locally with `scripts/test-boot-exchange.sh`.

---

## Troubleshooting (each line cost us a debugging session)

| Symptom | Cause | Fix |
|---|---|---|
| SSO user lands in `dsh-web-helm` (no platform volumes) | platform namespace missing, PVCs not `Bound`, or cross-ns RBAC not yet applied | check `project-user-<name>` exists + `project-info` ConfigMap; verify the ClusterRoles (`dsh-web-helm-router-platform`); the 60s context cache then provisions/adopts the platform unit on the next resolve |
| Data Manager shows no Platform roots for an SSO user | unit provisioned before the platform PVCs were Bound (mode `pending`), or `platformIntegration.enabled: false` | the router re-stamps the unit once the PVCs are Bound (check the `dsh-web-helm/platform-mounts` annotation); local users never see them by design |
| Router logs `[platform] context fetch failed … 403` | ClusterRole not applied (feature enabled but RBAC missing) | re-run `helm upgrade` so the gated ClusterRole renders; units keep working in the release namespace meanwhile |
| **Uninstall deleted nothing in a `project-user-*` ns** | registry unreadable during cleanup (secret gone/renamed) | cleanup falls back to the release namespace only; delete leftover `dsh-user-*`/`dsh-anchor-*` objects manually (platform PVCs are intentionally untouched) |
| Browser: **403 "RBAC: access denied"** (after SSO login) | ① Router pod has an Istio sidecar → namespace policies enforce on it; ② another VirtualService claims your host (routing hijack) | ① `kubectl label ns <ns> istio-injection=disabled` + restart router; ② `kubectl get virtualservice -A -o yaml \| grep <host>` → pick an unclaimed host |
| Browser: **500 nginx** on main page | nginx named location missing (`@dsh_boot`) | chart ≥0.1.2 |
| Redirect lands on **`:8082`** or **`127.0.0.1:3080`** | nginx `absolute_redirect` expands relative Locations with its listen port; validator passed dsh's loopback URL verbatim | chart ≥0.1.2 (`absolute_redirect off; port_in_redirect off`) and relative token redirect |
| **`/__dsh_boot → 503`** forever | `dsh-auth` container couldn't read the supervisor's boot-state file (no shared mount) | chart ≥0.1.4 (`state` mount, read-only) |
| **502 Bad Gateway** on main page right after claim | dsh web still booting (~60–90s) while nginx is already up | chart ≥0.1.5/0.1.7: readiness probes dsh's root via nginx `/healthz`; also just wait and reload |
| Login page shows at **`/?token=…`** | Router's `/` login-page reservation swallowed dsh's token exchange | chart ≥0.1.6 (`isDshTokenExchange` passthrough) |
| **settings.yaml edits never picked up** | Supervisor's JSON validator rejected YAML | chart ≥0.1.8 (YAML-aware watcher; dsh also re-reads settings per request) |
| Units **stuck NotReady** after 0.1.5 | Readiness probed dsh's nonexistent `/healthz` route | chart ≥0.1.7 (probe hits dsh root — 302 counts as ready); immediate unblock: revert readinessProbe to `tcpSocket 8082` |
| PVCs hang Pending / VAST quota collisions | VAST CSI 64-char quota-name truncation | `scripts/cleanup-dsh-web-helm-stale-quotas.sh` (dry-run by default) |

**Convergence:** per-user runtime changes roll automatically — the router stamps `dsh-web-helm/user-template-version` (RUNTIME_REV + dsh version + user image tag) and re-templates units whose annotation differs. If ever needed: `kubectl -n <ns> rollout restart deploy -l dsh-user-managed=true`.

---

## Key files in the user environment

| Path | Purpose |
|---|---|
| `/var/dsh/home/.dsh/settings.yaml` | dsh config (editable, hot-reloaded) |
| `/var/dsh/home/.dsh/profiles/web/cordis.patch.yml` | Profile patch layer (from `dshCordisPatch`, watched + validated) |
| `/var/dsh/home/.dsh/skills/` | Skills (user-managed; dsh hot-refreshes natively) |
| `/workspace/personal/AGENTS.md` | Agent instructions (user-managed; auto-loaded when present — no longer chart-seeded) |
| `/workspace/create-server.sh` | Preview server launcher |

## Scripts

- `scripts/build-user-image.sh` — build/push the baked user image (reads `dsh.version` + `provisioning.aptPackages` from the values file)
- `scripts/push_dsh_settings.sh` — push a settings.yaml into every user env (hot-reloaded)
- `scripts/user_manager.ipynb` — bulk user admin against the router API
- `scripts/cleanup-dsh-web-helm-stale-quotas.sh` — VAST quota cleanup (dry-run default)

## Known follow-ups

- dsh is a fast-moving developer preview (`0.1.x`): session-store paths (admin "clear sessions" job) and the remote-MCP config schema should be re-verified per dsh version.
- With SSO enabled, host registration at the platform auth layer remains an out-of-band admin step.
