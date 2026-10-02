# Platform PVC Integration Study — dsh-web-helm on G2 (PCAI)

**Status:** implemented — chart 0.4.1 (Option B). Rollout: **fresh reinstall** (user decision —
no migration machinery; existing units keep the sticky release-namespace shape, so even an
upgrade never moves or resets a unit). 0.4.1 fixes the router crash-loop on first platform login
(provision-watch key leaked into the API-path name slot; watch now passes name+ns separately and
never rejects).
**Chart:** `dsh-web-helm` 0.4.0 (dsh `0.1.6-alpha.2`)
**Scope:** design study + implementation record — the implementation follows §7 exactly:
namespace-aware router (`resolvePlatformEnvironment`), identity env from `project-info`
(init chown-own-volumes + `setpriv` drop for the whole service stack), platform PVCs mounted by
reference (never labeled/owned/deleted), anchor + ownerReferences uninstall contract with the
`ezprojects.hpe.com/*` skip-assert, mirrored unit ConfigMaps, conditional Data Manager platform
roots, and the gated cross-namespace ClusterRoles.
**Related:** README.md (chart operations, "Platform volumes for SSO users" section),
`scripts/cleanup-dsh-web-helm-stale-quotas.sh` (VAST quota hygiene)

---

## 0. TL;DR / Decision

**Goal.** When a user logs into DSH with **SSO**, mount the two PCAI platform volumes that already
exist in their Kubeflow project namespace — `kubeflow-shared-pvc` (cluster-wide shared) and
`user-pvc` (personal project volume) — into their DSH unit, **read-write**, with the platform's
per-user privileges fully propagated. Users keep the existing DSH `personal` / `shared` / `state`
volumes unchanged. The volumes must be visible in `/data-manager` (renamed from
`/data_manager` in chart 0.4.14; the underscore spelling stays aliased in-app).

**Hard requirement (evolved during study):** not read-only "monitoring" — **modification**,
"when personal privileges allow that". This rules out passive designs and requires the DSH process
to run with the project's **unix identity** (uid/gid from the platform's `project-info` ConfigMap).

**Decision: implement Option B** — for SSO users, deploy each DSH unit **inside the user's project
namespace** (`project-user-<name>`), where the platform PVCs already exist, mounted by plain
`claimName` — the same pattern the platform's own apps (`opencode-lab`, notebooks) use. Local
(non-SSO) users keep the current single-namespace behavior.

**Uninstall contract (explicit, user-set):** `helm uninstall` deletes **only DSH-created objects**
(pods/deployments/services/DSH-owned PVCs) in **both** the release namespace and the project
namespace(s). The platform PVCs are **never** deleted — guaranteed by label-scoped deletion +
ownerReference anchoring + an EzProject-label skip-assert (§7.6).

**Option A** (mirror the platform volumes as static PVs inside the release namespace) is the
fallback if the cross-namespace RBAC approval stalls. It can also satisfy the modify requirement
if upgraded with identity injection, at the price of PV-mirror lifecycle and a ClusterRole.

---

## 1. Background

DSH users on the G2 PCAI cluster also work in Kubeflow notebooks. Kubeflow gives every user two
persistent volumes: a private workspace and a shared one. Today DSH units only see their own
three PVCs (personal / shared / state) in the release namespace `dsh-web-helm`; the platform
volumes are invisible. Users must switch between DSH and notebooks to reach their platform files.

This study documents **how the platform connects an SSO login to those PVCs**, whether DSH can
reuse that mechanism, and how to do it without endangering platform data — in particular the
uninstall path.

---

## 2. The platform mechanism (verified on the cluster)

All findings below were verified live on G2 unless marked *inferred* (see Appendix A for the
RBAC-forbidden resources).

### 2.1 SSO chain

1. **Keycloak** — realm `UA`, client `ua`, is the IdP.
2. **oauth2-proxy** (`oauth2-proxy` ns, `oauth2-proxy-ua:v7.4.0`, `cookie-domain=.<domain>`) sits
   at the `ezaf-gateway`. Its alpha-config injects the identity headers into every request to
   upstream apps:
   - `kubeflow-userid` ← `preferred_username` claim  ← *the platform identity header*
   - `kubeflow-groups` ← `groups` claim
   - `X-Auth-Request-Preferred-Username`, `X-Auth-Request-User`, `X-Auth-Request-Groups`
   - `Authorization: Bearer <id_token>`, `X-Refresh-Token`
3. The domain-wide cookie means a user already logged into any platform app (Kubeflow, …) lands
   on a new SSO app **already authenticated** — no re-login.
4. App routes are protected by labels (`kubeflow-istio-auth: required`,
   `hpe-ezua/created-by: ezua`); the gateway enforces the login.

### 2.2 EzProject → namespace → identity + storage

- On first platform login, a **private `EzProject`** (`user-<username>`, cluster-scoped CRD
  `ezprojects.hpe.com/v1alpha1`) is created: `spec.owner` = the Keycloak user, `spec.private=true`
  ("single member"), `status.ownerKeycloakID` = the Keycloak sub.
- The **`ezprojects-operator`** (ns `ezprojects-system`) reconciles each EzProject and creates:
  - Namespace **`project-<name>`** (env `EZPROJECTS_NAMESPACE_NAME: project-{{ .EzProjectName }}`)
    → for private projects: `project-user-<username>`.
  - **`project-info` ConfigMap** — the project's **unix identity**: `uid`, `gid`, `group`,
    `user`, `homedir`. Private project → the owner's identity (e.g. francesco-caliva:
    uid `10000029`, gid `1001`); team project → dynamic uid from `DYNAMIC_UID_MIN/MAX`
    `70000–75000` (e.g. datapipeline: uid=gid=70005).
  - **`user-pvc`** (10Gi, **RWX**, dynamic VAST CSI; label `ezprojects.hpe.com/resource:
    project-pvc`) + a **`chown-project-pvc` Job** (busybox) running
    `chown -R <uid>:<gid> /project` — the operator does its own ownership setup because kubelet
    fsGroup does **not** chown these RWX NFS volumes.
  - **`kubeflow-shared-pvc`** (100Gi, **RWX**) bound to a static PV `<namespace>-kf-pv`.
  - **`fs`** Deployment (`hpecp-webhdfs:2.5.0`) — the platform file server, mounting `user-pvc`
    at `/mnt/project-<ns>` and `kubeflow-shared-pvc` at `/mnt/shared`, exposed at
    `fs.<domain>/project-<ns>/`.
  - RoleBindings: owner → `ezproject-admin` (EzProject-owned); later members get per-user
    RoleBindings (label `ezprojects.hpe.com/user: <name>`) — this is project *membership*.
- **Critical verified fact:** the static PVs of *different* users (francesco-caliva,
  andrea-carboni, nandita-goyal02) all reference the **same** VAST volume:
  `volumeHandle: pvc-a357f5f9-…`, `view_id: 18`, `quota_id: 13`, root export `/aiessentials`.
  `kubeflow-shared-pvc` is therefore **one cluster-wide shared filesystem** that every project
  namespace mounts under a per-namespace PVC alias. (The platform itself relies on many PV
  objects sharing one volumeHandle.)

### 2.3 Notebook wiring (how the two PVCs get mounted)

From the Jupyter Web App spawner defaults (`jupyter-web-app-config` ConfigMap, ns `kubeflow`):

- **Workspace volume:** new PVC `{notebook-name}-workspace` (10Gi, RWO, dynamic) mounted at
  **`/home`** — the *private* volume.
- **Data volumes (defaults):** existing PVCs
  - `kubeflow-shared-pvc` → **`/mnt/shared`**
  - `user-pvc` → **`/mnt/user`**
- Default PodDefault: `add-ezua-proxy`; the notebook controller adds labels from
  `notebook-labels-cm`, and the PodDefault webhook injects env **from `project-info`**:
  `USER_INFO_UID / USER_INFO_GID / USER_INFO_USERNAME / USER_INFO_GROUP / USER_INFO_HOMEDIR`,
  plus the `access-token` secret (mounted at `/etc/secrets/ezua/.auth_token`), CA cert,
  kubeconfig secret.
- The notebook controller builds a StatefulSet with **`fsGroup: 100`** (platform-wide convention,
  also seen on `opencode-lab`) + istio sidecar + VirtualService at `/notebook/<ns>/<name>/`.

### 2.4 Privilege model on the two volumes (the part that matters)

- The notebook image's s6 entrypoint consumes `USER_INFO_*`, creates the unix user, and runs
  Jupyter **as that uid:gid** — verified in-pod: `jupyterhub-singleuser` runs as `frances+`
  (uid `10000029`), while the supervisor runs as root.
- `/mnt/shared` (verified listing inside the running notebook): **per-user directories, all
  group `1001`** (the common gid across personal projects), each owned by its user's uid —
  e.g. `califra` → uid `10000029` (francesco), `andrea` → uid `10000007` (andrea-carboni) —
  with mixed modes: **755 (owner-writable, others read) and 777 (world-writable)**.
- Therefore **all modification privileges on the platform volumes are plain unix uid/gid + mode
  bits** (no squashing in play — notebook-written files land owned `10000029:1001`). A process
  writes where its uid owns the dir, where the dir is group-writable with its primary gid
  (1001), or where the dir is 777.
- `user-pvc` is chowned to the project's `<uid>:<gid>` → writable by that uid everywhere.
- fsGroup is inert on these NFS RWX volumes (no kubelet chown — hence the operator's chown Job),
  so mounting pods cannot mangle platform ownership by accident.

### 2.5 Companion services

- **PVCViewer** (`pvcviewers.kubeflow.org/v1alpha1`): per-PVC filebrowser (`filebrowser:v2.25.0`,
  `FB_NOAUTH`) served at `/pvcviewers/<ns>/<pvc>/` — how the UI browses a volume without
  attaching it. Template: `volumes-web-app-viewer-spec` ConfigMap.
- **`fs`** WebHDFS service (above): platform file browsing over project + shared volumes.

### 2.6 Evidence index

| Fact | Source |
|---|---|
| Identity headers | `configmap/oauth2-proxy-alpha -n oauth2-proxy` |
| EzProject schema (owner/private/unixAttributes/ownerKeycloakID) | `crd ezprojects.ezprojects.hpe.com` |
| Operator env (ns naming, uid range, chown/FS images, shared-pvc template) | `deploy ezprojects-operator -n ezprojects-system` |
| `project-info` contents (private vs team) | `cm project-info -n project-user-francesco-caliva` / `-n project-datapipeline` |
| Shared PVC → same volumeHandle across users | `pv project-user-{francesco-caliva,andrea-carboni,nandita-goyal02}-kf-pv` |
| `user-pvc` chown Job | `job chown-project-pvc -n project-user-francesco-caliva` |
| Notebook mounts + fsGroup + USER_INFO env | `pod default-notebook-0 -n project-user-francesco-caliva` |
| spawner defaults (the two data volumes) | `cm jupyter-web-app-config -n kubeflow` |
| Shared FS dir ownership/modes | `ls -ln /mnt/shared` (exec in notebook) |
| Process runs as project uid | `ps aux` (exec in notebook) |
| `opencode-lab` precedent (app in project ns mounting platform PVCs) | `sts opencode-lab -n project-user-francesco-caliva` |
| RoleBindings = membership | `rolebindings -n project-user-francesco-caliva` / `-n project-datapipeline` |
| Static shared PV reclaim policy Retain | `pv project-user-francesco-caliva-kf-pv` |
| `user-pvc` dynamic (→ Delete-class reclaim) | PVC annotations (`bound-by-controller`, provisioner csi.vastdata.com) |

---

## 3. DSH chart today (mechanics relevant to this integration)

- **Router** (`server.mjs` in ConfigMap `{{ .Release.Name }}-router-config`): creates per user —
  3 PVCs (personal `RWO 5Gi`, shared `RWX 20Gi` release-level, state `RWO 2Gi`;
  label `dsh-user-managed: "true"`, `nameVersion` suffix scheme), a Deployment
  (`fsGroup: 1000`, `fsGroupChangePolicy: OnRootMismatch`, `sidecar.istio.io/inject: "false"`,
  `strategy: Recreate`; 4 containers: dsh-web, nginx :8082, auth-validator :7684; ports
  8082/7681/7682/7683), a Service — **all in the release namespace**.
- **SSO already wired:** router reads `x-auth-request-preferred-username` (fallback
  `x-auth-request-user`); `/__dsh_sso`, auto-SSO landing flow; registry tracks identities with
  `sso: true`; per-user URL prefix = username.
- **Warm pool:** pre-provisioned Deployment+Service+PVCs per unit, claimed by **adoption in
  place** (re-label + re-stamp `user-template-version` + restart; the state PVC — the installed
  toolchain — survives the claim). Claiming never moves objects.
- **Convergence:** router re-templates units whose `user-template-version` annotation differs —
  this is the self-healing hook any template change can ride on.
- **Data Manager** (`data_manager.mjs`, :7682): roots are a static JS array —
  `personal` (/workspace/personal), `shared` (/workspace/shared), `DSH Home`
  (/var/dsh/home/.dsh), `State` (non-navigable). Path traversal clamped to navigable roots;
  storage panel = per-root `statfs` (cached).
- **Uninstall:** `pre-delete` hook Job — Phase 0 stop router; Phase 1 delete
  `dsh-user-managed=true` deployments/services/leases/VS/secrets; Phase 2 wait `app=dsh-user`
  pods gone; Phase 3 delete `dsh-user-managed=true` PVCs. **Strictly label-scoped, namespaced
  Role RBAC** — the cleanup SA cannot touch anything outside the release namespace today.
- **Known ops hazards (README):** istio sidecar in the release ns → 403 RBAC; VS host collisions;
  VAST 64-char quota-name truncation.

---

## 4. The hard constraint & requirement evolution

**Constraint:** PVCs are namespace-scoped. A pod can only mount PVCs from its own namespace.
DSH units live in `dsh-web-helm`; the platform PVCs live in `project-user-<name>`.
→ "Just add two volumes" is impossible without either moving the unit or mirroring the volume.

**Requirement evolution:** initial framing was read-only "monitor content of these PVCs from
/data-manager" (then spelled /data_manager); the user then required **modification** ("modify files in their personal pvc and
also shared pvc when personal privileges allow that"). Modification ⇒ the DSH process must run
with the project's unix identity (§2.4). Passive uid-mismatch (read-mostly) designs are no
longer sufficient as an end state.

---

## 5. Options

### Option A — mirror the platform volumes as static PVs in the release namespace

For each SSO user the router creates static PVs (same VAST `volumeHandle`) + alias PVCs in
`dsh-web-helm`, mounted at `/mnt/shared` and `/mnt/user`.

- *Feasible:* the platform itself runs many PV objects against one volumeHandle (§2.2); both
  volumes are RWX (concurrent notebook + DSH mounts are legal).
- *To satisfy "modify"* it must be **upgraded**: router reads `project-info` cross-namespace and
  injects `USER_INFO_*` into the unit; entrypoint creates the unix user and drops privileges
  (same pattern as notebook images); the init container **chowns DSH's own volumes**
  (personal/state) to `<uid>:<gid>` — replacing the fsGroup-1000/root assumption. **The chown
  step must never touch the mirrored platform volumes** (cluster-wide FS — catastrophic).
- *Uninstall story:* identical to today's (everything deletable stays in the release ns).
- *Costs:* ClusterRole (PVs are cluster-scoped) + cross-ns read of `project-info`; mirror
  lifecycle (stale PVs after EzProject deletion); platform security boundary crossed by design.

### Option B — SSO users' units inside their project namespace (**recommended**)

For SSO users the router creates the unit Deployment/PVCs/Service **in `project-user-<name>`**,
where the platform PVCs already exist — mounted by plain `claimName`, exactly like
`opencode-lab` and notebooks.

- *Identity comes natively:* the pod lives where `project-info`, the `add-user-info-config`
  PodDefault, and the `access-token` secret exist; entrypoint drops to the project uid:gid →
  **DSH writes with byte-identical ownership/privileges to a notebook**. "Modify when privileges
  allow" is enforced by the kernel, not by us.
- *Mounts are plain claimName* — no PV mirroring, no cluster-scoped PV RBAC (the router still
  needs cross-namespace management RBAC, §7.7).
- *Costs:* cross-ns RBAC approval; namespace-aware router logic (FQDN services, warm pool,
  convergence, deletion); two unit shapes (SSO vs local) to maintain; project-ns Istio injection
  must stay off on unit pods (template already annotates `sidecar.istio.io/inject: "false"`).

### Option C — no mount; monitor via platform file services — **demoted**

Data Manager deep-links/proxies to `fs.<domain>/project-<ns>/` (WebHDFS, read-write-capable) or
`/pvcviewers/<ns>/<pvc>/` (filebrowser). Zero mounts, zero RBAC, works today — but integrated
modification would mean reimplementing file ops over HTTP with the user's token inside the pod,
to end up with a worse copy of a direct mount. Does not realistically satisfy the modify
requirement. Keep only as an interim link-out.

### Comparison

| | A (upgraded) | **B (chosen)** | C |
|---|---|---|---|
| Modify with propagated privileges | yes (after upgrade) | **yes, by construction** | only via fs-API proxying (no) |
| Privilege mechanism | replicated (env injection + chown) | **native (platform PodDefault + project-info)** | platform services |
| New RBAC | ClusterRole (PV create, cross-ns reads) | cross-ns management Role-set for router+cleanup | none |
| Lifecycle risk | PV mirrors / stale quotas | none beyond own units | none |
| Uninstall risk surface | unchanged (release ns only) | widened but contract-bound (§7.6) | none |
| Rework size | medium | **largest, one-off** | trivial |

---

## 6. Decision

Implement **Option B**. Fallback: upgraded Option A if the cross-namespace RBAC approval is
refused or stalls. Option C only as an interim navigation affordance, not a fulfillment of the
requirement.

---

## 7. Option B design

### 7.1 Placement & mounts

- SSO user unit = Deployment + Service + DSH-owned PVCs (personal/state) in
  `project-user-<name>`, plus mounts of the platform PVCs by claimName:
  - `kubeflow-shared-pvc` → `/mnt/shared`
  - `user-pvc` → `/mnt/user`
- Keep `sidecar.istio.io/inject: "false"` on unit pods (the 403-RBAC lesson), keep
  `strategy: Recreate` (RWO volumes), keep per-unit labels + `user-template-version`.
- Local users: unchanged current shape in the release namespace.

### 7.2 Identity & privilege propagation

- Unit pod adopts the platform identity path: `USER_INFO_*` env from `project-info`
  (in the project ns this arrives via the `add-user-info-config` PodDefault pattern the platform
  already applies to user workloads; the exact injection mechanism for DSH units is an
  implementation choice — PodDefault labels and/or router-stamped env), plus the `access-token`
  secret mount for platform API parity with notebooks.
- Entrypoint: create the unix user from env, then exec dsh + data manager + ttyd as
  `<uid>:<gid>` (notebook-image pattern; `util-linux`/`setpriv` already in `aptPackages`).
- Init container chowns **DSH's own** volumes (personal/state) to `<uid>:<gid>`, idempotent
  (owner-mismatch-only) to avoid recursive walks on every claim. **Never chown the platform
  volumes.**
- Result: Data Manager / terminal / agent write with `<uid>:<gid>`; files indistinguishable from
  notebook-written ones; kernel-enforced EACCES exactly where the platform forbids.

### 7.3 Warm pool restructure

- Nothing is ever "transferred" between namespaces (K8s objects cannot move; even today's claim
  is adoption-in-place).
- SSO warm units are **born in the user's project namespace** — provisionable only after the
  user is known, i.e. per-SSO-user warming (identity/uid known at creation — no retrofit).
- Brand-new SSO user (no project ns yet): cold spin-up once, warm afterwards. Optional policy:
  warm only local users, SSO users always cold.
- Warm pool stays enabled/disabled via existing `warmPool.*` values; sizing semantics become
  "per known SSO user" rather than "generic pool".

### 7.4 Trigger model (when DSH may act)

- **Trigger = first login/request to the DSH app** (router sees the SSO headers → registry entry
  `sso: true`). **Never** platform-level login — DSH cannot and must not observe it, and must
  not provision for users who never opened DSH.
- At that moment, check `project-user-<name>`:
  - namespace exists **and** `kubeflow-shared-pvc` / `user-pvc` are `Bound` → provision unit
    with platform mounts (common case: any prior platform activity provisions both, since the
    EzProject operator creates them at project creation).
  - missing / not Bound (DSH is the user's first SSO app — rare) → deploy **without** platform
    mounts; Data Manager shows platform roots as "not provisioned yet"; a later reconciliation
    pass (the existing `user-template-version` re-stamp) adds the mounts once ns+PVCs appear.
    Optional UX nudge: "open your PCAI dashboard once to provision your workspace".
- Consequence: the set of project namespaces DSH ever touches == namespaces of its own registry
  users. Cleanup derives its namespace list from the registry, never from a cluster scan.

### 7.5 Data Manager surfacing (agreed: yes)

- Add the two platform volumes as **navigable roots** for SSO users:
  - `kubeflow-shared-pvc` → `/mnt/shared`
  - `user-pvc` → `/mnt/user`
- The ROOTS array drives both navigation and the traversal clamp — new roots inherit the
  guardrails; change is contained (two entries + per-unit volumes/mounts).
- **Conditional visibility:** roots hidden for local users and not-yet-provisioned SSO users
  (router-stamped per-unit flag and/or `/api/roots` filtering on the statfs-failure signal the
  storage endpoint already produces). Self-heals the reconcile case (§7.4).
- **Naming — avoid the Personal/Shared collision:** DSH's own roots are "Personal"/"Shared";
  label the platform ones unambiguously, e.g. **"Platform Shared (Kubeflow)"** /
  **"Platform Workspace (Kubeflow)"**, visually grouped as platform volumes.
- **Storage panel caveat (verified `statfs` per root):** for `/mnt/shared` the numbers reflect
  the **whole platform filesystem** (all users), not the user's share — label accordingly;
  `/mnt/user` numbers are accurate per-project. Avoid recursive `du` on the cluster-wide volume.
- **Destructive ops:** deletes/overwrites on the platform-shared root affect data beyond DSH —
  apply the same caution UX as the existing `shared: true` root (confirm-on-delete); deletes are
  permanent (no trash) on both volumes.
- **Write policy:** writes succeed wherever unix privileges allow (§7.2); an optional per-root
  readOnly admin toggle remains available for monitor-only deployments (unix stays the true
  enforcement).

### 7.6 Uninstall safety contract (user-set invariant)

**`helm uninstall` deletes only DSH-created objects — in the release namespace AND in each
project namespace — and never the platform PVCs.**

Guarantee stack (defense in depth):

1. **Pods and PVCs are lifecycle-independent** — deleting a pod never deletes a mounted PVC.
   The only actor issuing PVC DELETEs is our own cleanup code.
2. **Label contract:** everything DSH creates anywhere carries `dsh-user-managed: "true"`
   (+ `app: dsh-user`, `dsh-user-slug`); cleanup deletes *only* by that exact selector — never
   by name prefix, never "all PVCs in namespace". DSH never labels objects it did not create.
3. **Anchor + ownerReferences:** per SSO user, a small anchor object (e.g. ConfigMap) in the
   project ns owns (via ownerReferences) the unit Deployment/Service/DSH-owned PVCs. Uninstall
   (or admin user-deletion) deletes anchors → GC cascades. Platform PVCs have their own
   controller owner (EzProject) and are never referenced → **structurally unreachable by GC**.
4. **Skip-assert:** cleanup skips with a loud warning any PVC carrying an `ezprojects.hpe.com/`
   label, even if it somehow matched the selector.
5. **Registry-driven namespace list** (§7.4) — cleanup visits only namespaces of known DSH users.
6. **Phase order preserved:** stop router → delete DSH-labeled workloads/services/secrets →
   wait pods gone → delete DSH-labeled PVCs (release ns *and* project ns). Non-blocking on
   error (today's posture); orphans are re-adopted by a reinstall's convergence logic.
7. **Never delete namespaces.** The only actor that can remove `project-user-<name>` (with the
   platform PVCs) is the platform's own EzProject deletion (offboarding) — platform semantics,
   independent of DSH; DSH reconciles (registry cleanup) when a unit's namespace vanishes.

Risk ranking of the PVCs (why the contract matters):

| PVC | PV | Reclaim on claim deletion | Accidental-delete consequence |
|---|---|---|---|
| `kubeflow-shared-pvc` | static, **Retain** (verified) | data retained | **cluster-wide outage**, recoverable |
| `user-pvc` | dynamic VAST CSI | effectively **Delete** | **real data loss** for the user |
| DSH personal/state PVCs | dynamic | Delete | intended on uninstall (optional keep flag) |

### 7.7 RBAC requirements (the one approval needed)

- Router SA: manage deployments/services/pods/PVCs/secrets/leases (and DSH unit
  virtualservice-free — units are router-proxied, no per-unit VS needed cross-ns) in
  `project-user-*` namespaces → realistically a ClusterRole or per-namespace Roles maintained
  for known users; plus read of `project-info` there.
- Cleanup SA: widened from Role to cross-namespace delete rights, bounded by the label contract
  + registry-driven namespace list + kinds allowlist.
- **This is a real security grant — socialize with platform admins before implementation.**
  It is the gating dependency of Option B (and, in PV form, of Option A).

---

## 8. Risks & edge cases

| # | Risk | Mitigation |
|---|---|---|
| 1 | Cleanup selector too broad / misconfig | exact selector, kinds allowlist, per-object delete logging, optional DRY_RUN mode |
| 2 | Platform PVC somehow labeled by DSH | code never labels foreign objects + skip-assert on `ezprojects.hpe.com/*` labels |
| 3 | Namespace deleted by platform offboarding | not ours; DSH reconciles missing-namespace users |
| 4 | User with no platform project yet | §7.4 reconcile path (mounts appear later via re-stamp) |
| 5 | PVCs not yet Bound right after project creation | verify `Bound` before mounting; else defer to reconcile |
| 6 | chown accidentally recursive on shared FS | chown own PVCs only; never platform volumes; owner-mismatch-only |
| 7 | Warm pool semantics change confuses ops | document per-user warming; optional SSO-cold policy |
| 8 | Project-ns AuthorizationPolicies interfering with router→unit traffic | sidecar-less unit pods; verify during implementation (the one 403-class surprise seen before on G2) |
| 9 | Large zip/download over router chain timeouts | existing VS timeouts; check-item, per-root size guidance |
| 10 | Storage panel shows global numbers for platform-shared | label as platform-wide; never recursive du |
| 11 | VAST quota debris from DSH PVCs in project ns | nameVersion scheme + optional keep-workspaces flag + existing cleanup script |
| 12 | Two unit shapes (SSO/local) drift | shared manifest builder, feature-flagged platform section; `user-template-version` bump on change |

---

## 9. Open items before implementation

1. Platform-admin approval for cross-namespace RBAC (§7.7) — **the gating decision**.
2. Out-of-band SSO host registration for the DSH endpoint at the platform auth layer (README
   "known follow-ups") — required for the SSO path at all.
3. Confirm exact injection mechanism for `USER_INFO_*` into DSH unit pods (PodDefault labels vs
   router-stamped env) during implementation; confirm `access-token` secret mount is wanted.
4. Decide warm-pool policy for SSO users (per-user warm vs cold).
5. Decide whether DSH-owned PVCs for SSO users live in the project ns (B as designed) — and
   whether uninstall keeps or deletes them by default (current default: delete).
6. Data Manager root labels + confirm-on-delete UX sign-off.

---

## 10. Invariants (non-negotiable, restating the user's constraints)

1. DSH `personal` / `shared` / `state` volumes and roots remain unchanged for all users.
2. Only SSO users get platform volumes; local users see no difference.
3. Modification honors platform privileges exactly (kernel-enforced via uid parity).
4. Uninstall deletes only DSH-created objects, in both namespaces; **platform PVCs are never
   deleted**; namespaces are never deleted by DSH.
5. The platform volumes appear in `/data-manager` for SSO users, clearly distinguished from
   DSH's own roots.

---

## Appendix A — RBAC-forbidden observations (inferred, not verified)

The MCP service account could not read: `EzProject` CRs, `poddefaults`, `ClusterRoles`,
`EnvoyFilters`, `AuthorizationPolicies`, `StorageClasses`, `CSIDrivers`, Notebook/PodDefault CRs.
Corresponding statements in this document (PodDefault contents, ezproject-admin verbs, gateway
EnvoyFilter behavior, StorageClass reclaim policy, fsGroupPolicy) are **inferred from
observable effects**: pod env/labels/annotations, ownership of created objects, unchanged
platform dir ownership under `fsGroup: 100` mounts, and the existence of the operator's own
chown Job. Each should be re-verified with an admin-level read during implementation.
