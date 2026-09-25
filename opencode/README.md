# OpenCode Web — Multi-User version on HPE Private Cloud AI

Deploy and manage multiple isolated [opencode](https://opencode.ai) coding-agent environments on **HPE Private Cloud AI**. Each user gets their own PVC-backed workspace, and a shared workspace enables team collaboration.

---

## Access & Authentication

The app entry point is `https://opencode.{DOMAIN_NAME}` (protected by platform
SSO). From there you choose how to log in:

- **Continue with SSO** — uses your platform identity. Each distinct SSO
  account gets its own pod.
- **Use a local account** — log in with a username/password that an **admin
  pre-provisioned** for you. (Self-registration is disabled on the login page;
  accounts are created by an admin — see [Admin Console](#admin-console).)

After logging in you are redirected to your personal path
`https://opencode.{DOMAIN_NAME}/{username}`, which serves your UI, terminal,
data manager, and previews directly from your own pod. Access is gated by the
signed session cookie issued at login, so each user can only reach their own
pod.

### URL scheme

Everything lives on the single main host; each user's environment is addressed
by its readable slug — the **sanitized username itself**:

| Purpose | URL |
|---|---|
| Login / SSO / Admin | `https://opencode.{DOMAIN_NAME}` |
| Admin console | `https://opencode.{DOMAIN_NAME}/__oc_admin` |
| Personal environment | `https://opencode.{DOMAIN_NAME}/{username}` |
| Terminal | `.../{username}/terminal` |
| Data Manager | `.../{username}/data_manager` |
| Preview for port `PORT` | `.../{username}/__preview/{PORT}/` |

Since chart **1.2.0** the per-user URL prefix is the sanitized **username**
(e.g. `/francesco-caliva/`) — usernames are unique in the registry, so the name
is a safe identifier. Pre-1.2.0 links of the form `.../u-{12-hex-slug}/...`
keep working (legacy bookmark route); an environment created under the legacy
hash slug is adopted under the readable slug (keeping its volumes) the first
time its user logs in after the upgrade.

Previews are auth-gated (same cookie as the rest of the pod). Share them with
someone who can authenticate to that pod.

### DNS

The platform's DNS must resolve the single app host to the Istio ingress
gateway IP:

- `opencode.{DOMAIN_NAME}` → the gateway IP

No wildcard is needed — per-user traffic is routed to pods by path, not by
per-user subdomains.

## Admin Console

Admins manage accounts at `https://opencode.{DOMAIN_NAME}/__oc_admin`, reached
via the **Admin login** form on the login page (or by navigating directly).
Credentials come from your values file:

```yaml
admin:
  username: admin-user
  password: "admin-password"
```

From the admin console you can:

- **Create user** — add a username/password; the account is provisioned
  immediately (spinner → **ready**). This is the **only** way to create local
  accounts (self-registration is hidden on the login page), so pre-provision
  accounts for a workshop and hand out the credentials.
- **Delete user** — remove the account and all its pods/PVCs.
- **Reset password** — set a new password for a user.
- **Rename** — change a username (recreates the environment under the new name).

For bulk operations the same endpoints are available programmatically — see
`scripts/user_manager.ipynb`. The router additionally exposes
`POST /__oc_admin/api/sessions/clear` (same admin basic-auth): send `{username}`
to clear one user or `{all: true}` / an empty body to clear every user's opencode
chat history. Under the hood the router runs a short-lived cleanup Job that mounts
the user's state PVC and deletes the conversation database (`opencode.db` under
`/var/opencode/data/opencode`; legacy `storage/` subtrees included), then restarts
the user's pod so the running server recreates a fresh, empty database —
conversations are deleted, while workspace files, `opencode.json`/agents/skills
and `auth.json` (model API keys) are preserved, and users stay logged in.
Irreversible.

## Configuration

### opencode.json, Agents & Skills

All configuration lives in `values.yaml`:

| Field | What it defines |
|---|---|
| `opencodeConfig` | The `opencode.json` config (models, providers, MCP servers, permissions) |
| `opencodeSkills` | Skill markdown files seeded into `~/.config/opencode/skills/` |
| `opencodeAgents` | Agent markdown files seeded into `~/.config/opencode/agents/` |
| `opencode.version` | The opencode npm version to install (e.g. `1.18.11`) |
| `openchamber.version` | The OpenChamber npm version to install (used when `ui.mode: openchamber`) |
| `ui.mode` | `opencode` (default, built-in web UI) or `openchamber` (OpenChamber frontend) |

To switch the version of either opencode or OpenChamber, change the number in `values.yaml` and repackage. The chart `version` (in `Chart.yaml`) is bumped manually per release; `appVersion` tracks the opencode major.minor family.

### UI modes

- **`ui.mode: opencode`** (default) — the built-in `opencode web` UI is served on the app port, exactly as before.
- **`ui.mode: openchamber`** — a headless `opencode serve` runs on `ui.serverPort` (loopback, `127.0.0.1`) and [OpenChamber](https://openchamber.dev) is served on `ui.chamberPort` (default `3000`), connecting to it via `OPENCODE_HOST` / `OPENCODE_SKIP_START`. OpenChamber is npm-installed into the user state volume (no separate runtime needed; it runs on the same Node 22 image). The terminal / data-manager / preview portals are unchanged. Because the opencode server is loopback-only in this mode, its HTTP basic auth is disabled and OpenChamber's own password prompt is turned off — access is gated entirely by the signed session cookie (the in-pod nginx `auth_request` gate), so there is no additional "Unlock OpenChamber" prompt.

These are bundled into a ConfigMap and copied into each user's `~/.config/opencode/` on first start. Users can **edit any of these files directly** inside their environment — a watcher (`opencode_supervisor.mjs`) monitors `opencode.json`, `config.json`, `opencode.jsonc` and the `skills/` / `agents/` directories for changes and automatically restarts the opencode process. Just **refresh the browser** to see changes take effect.

---

## Warm Pool (instant first launch)

By default, a brand-new user's first launch waits while the router provisions a
per-user Deployment + PVCs and runs a heavy init (`npm install` of opencode,
uv, ttyd, apt packages) — this can take 3–5 minutes.

The router can instead keep a small pool of **already-provisioned, always-ready
user units** so a first-time user's environment is usable in roughly seconds.

```yaml
warmPool:
  enabled: false   # set to true to opt in
  size: 2          # number of always-ready idle user units to maintain
  refreshIntervalSeconds: 15
```

### How it works

- A "warm unit" is the full per-user set — its own Deployment, Service, workspace
  PVC, and **state PVC**. The toolchain (opencode/uv/ttyd/openchamber) is installed
  into the state PVC, so it is built **once** when the unit enters the pool.
- The router runs a background reconcile loop that keeps `size` warm, un-owned
  units at all times (it builds replacements in parallel whenever one is claimed).
  An un-owned unit that stays non-Ready for more than 15 minutes
  (`WARM_STUCK_THRESHOLD_MS`) is recreated so the pool cannot dry up on a broken
  unit.
- **Assignment is warm-first for new users.** On a first launch the router claims
  the lowest-free warm unit — preferring a Ready unit, but it will also claim a
  unit that is still starting (the loading screen then waits for it, the same UX
  as a cold start, without spawning a dedicated pod). Claiming patches the pod
  template to set the user's slug (for preview/ttyd URLs) and records the user as
  the unit's owner. Claims are serialized per-unit, so two concurrent logins can
  never grab the same unit.
- **Rollouts use `Recreate`, not RollingUpdate.** User pods mount RWO PVCs; a
  RollingUpdate surge pod scheduled on a different node would deadlock on a
  Multi-Attach error while the old pod holds the volumes (surfacing as an
  endless loading page on multi-node clusters). Recreate terminates the old pod
  first; the claim downtime is absorbed by the loading screen and the restart
  is fast because the toolchain is already on the state PVC. Existing
  deployments are converged to Recreate automatically by the reconcile loop
  (owner labels preserved).
- **Users who already have a dedicated environment stay on it** (sticky). Their
  data lives in the dedicated PVCs and is not carried over to warm units, so the
  router never moves them. Only users with no existing environment are assigned
  warm units; a dedicated pod is provisioned only when the pool is disabled or
  has nothing claimable (e.g. pool creation failing).
- `size: 0` or `enabled: false` behaves **exactly like before** (on-demand cold
  spin-up; zero idle pods).
- Versions are **not** baked into an image. opencode/OpenChamber remain runtime-
  parameterized, so bumping `opencode.version` / `openchamber.version` in
  `values.yaml` and repackaging still works. Warm units adopt a new version the
  next time they are claimed/re-initialized via the existing template-version
  logic — no image rebuild.

### Tradeoff

Each warm unit is an idle user pod consuming up to the per-user pod limits
(2 cores / 2 Gi, hardcoded in the user template). `warmPool.size` therefore
adds a permanent overhead of `size × those limits`. This is the explicit cost
of instant first launch; keep it `enabled: false` if idle capacity is a
concern.

### Uninstall / `helm uninstall`

A `pre-delete` hook deletes the dynamically-created per-user/warm resources
(which Helm does not own) so the namespace does not hang in `Terminating`.
The hook:

1. **Stops the router first** (deletes the router `Deployment`) so it can no
   longer reconcile/re-create warm units while teardown is running.
2. Deletes the per-user **Deployments, Services, and Leases in parallel**.
3. Waits for their **pods to terminate** (releasing the PVC mounts).
4. **Keeps every user data PVC** (default `storage.keepPvcOnDelete: true`):
   per-user `opencode-*-ws-*/-st-*` claims, warm-pool claims
   (`opencode-user-warm-N-*-pvc-v2`) and the per-namespace shared mirror are
   listed and logged, never deleted — the first re-login / re-install re-binds
   the exact same volumes and the data is still there. Set
   `storage.keepPvcOnDelete: false` to restore the legacy
   delete-PVCs-on-uninstall behavior.
5. Leaves the Helm-owned shared PVC intact (it carries
   `helm.sh/resource-policy: keep` plus only `hpe-ezua/*` labels, never
   `opencode-user-managed`).

**PVC-survival contract:** user data PVCs are never deleted by the app — not
via the anchor GC cascade (PVCs are no longer owner-referenced), not by the
admin user-delete flow, not by uninstall. Deliberate purge = manual
`kubectl delete pvc -n <ns> <claim>`. If you manually delete the kept release
shared PVC before reinstalling, nothing else needs to change.

**Existing-install migration (run once after upgrading to ≥ 1.2.3):**
`scripts/strip-pvc-owner-refs.sh` removes the legacy anchor `ownerReferences`
from all `opencode-user-managed=true` PVCs; without it, pre-existing PVCs
would still be GC-cascaded by anchor deletion.

---

## Provisioning Loading Screen

While a user's pod is being assigned/provisioned (a cold first launch can take
3–5 minutes for the per-user `npm install`, uv, ttyd, etc.), the router no
longer leaves the browser hanging on a blank page. It returns a **loading
screen** immediately:

- A spinner plus the text **"Setting up your account…"**.
- The page polls the router's `/__oc_setup_status` endpoint every 2 seconds.
- When the pod becomes ready, the page **auto-reloads** and redirects you to
  `/u-{slug}` automatically.
- If provisioning fails or times out, the page swaps to an error state with a
  **Retry** button (the retry clears the failed provisioning attempt and
  starts over).

With a [warm pool](#warm-pool-instant-first-launch) enabled the loading
screen appears only briefly during the fast rolling restart of a claimed
warm unit (no reinstall), so it is near-instant.

### Configuration

```yaml
provisioning:
  loadingUI:
    enabled: true                      # set false to restore the original blocking behavior
    heading: "Setting up your account…"
    subtext: "This usually takes a few minutes on first launch. Please wait."
```

- `enabled: false` reverts the router to its previous behavior (the request
  blocks until the pod is ready and no loading page is shown).
- The heading and subtext are the messages shown on the loading screen.
- The status/reset endpoint is the reserved router path `/__oc_setup_status`;
  it is handled by the router itself (never proxied to the user pod) and
  requires no ready pod.

---

## Workspaces

HPE Private Cloud AI provides PVC-backed persistent storage:

| Mount point | Type | Purpose |
|---|---|---|
| `/workspace/personal` | `ReadWriteOnce` PVC | **Personal workspace** — private to each user |
| `/workspace/shared` | `ReadWriteMany` PVC | **Team collaboration** — all users share this space |

Every user gets their own personal PVC (sized via `storage.workspaceSize`) and state PVC (`storage.stateSize`). The shared PVC (`storage.sharedSize`) is populated with initial content from the `shared/` directory at deploy time.

---

## Platform volumes for SSO users

With `platformIntegration.enabled: true` (on in `values-g2.yaml`), **SSO** users get
their unit deployed **inside their platform project namespace** (`project-user-<username>`)
— the namespace the EzProject operator created for them — with the platform volumes
mounted **read-write**:

| Mount | Platform PVC | What it is |
|---|---|---|
| `/mnt/shared` | `kubeflow-shared-pvc` | **one cluster-wide shared filesystem** (every project namespace's PVC binds to the same VAST view); per-user directories live here |
| `/mnt/user` | `user-pvc` | the user's personal project volume (chowned by the platform to `<uid>:<gid>`) |

Local (non-SSO) users keep the classic single-namespace behavior; the chart's own
`personal` / `shared` / `state` volumes and Data Manager roots are unchanged for everyone.

**Platform namespace naming:** the router derives the candidate project namespace from
the SSO username. By default it uses `platformIntegration.namespacePrefix` +
username (`project-user-<username>`). Platforms that name projects differently can set
`platformIntegration.namespacePatterns` — a comma-separated list of templates with a
`{username}` placeholder, tried in order (first candidate whose namespace exists and
whose `project-info` ConfigMap carries `uid`/`gid` wins). A trailing `*` acts as a
wildcard: any namespace whose name starts with the expanded prefix becomes a candidate
(resolved via a namespace-list scan and verified for **ownership** through the
project-info `user` field, so one user's prefix can never adopt another user's
namespace). Examples:

```yaml
platformIntegration:
  namespacePatterns: "project-user-{username}"          # default, = namespacePrefix behavior
  # namespacePatterns: "user-{username},{username}-slug" # several shapes, tried in order
  # namespacePatterns: "{username}-*"                    # username followed by anything
```

**Identity = the platform's.** The router reads the project's unix attributes from the
`project-info` ConfigMap (`USER_INFO_UID/GID/USERNAME/GROUP/HOMEDIR`), the unit init
chowns the chart-owned volumes to that identity (owner-mismatch-only, never the platform
volumes), and the service stack (opencode, data manager, ttyd terminals) execs under it
via `setpriv`. Files written from the unit on the platform volumes are therefore
**byte-identical to notebook-written files** (same uid/gid), and modification rights are
exactly "wherever the user's platform privileges allow" — enforced by the kernel,
surfaced as clean errors in the UI.

**Secrets:** the pod template references the release auth Secret
(`<release>-auth`, basic-auth pair) — cross-namespace secretRefs cannot resolve, so the
router mirrors a **filtered copy** (only `OPENCODE_SERVER_USERNAME`/`OPENCODE_SERVER_PASSWORD`)
into the project namespace, owner-anchored and cleaned with the unit. `SESSION_SECRET` and
the admin credentials stay release-namespace-only (the project namespace is user-readable —
mirroring the session key there would allow forging any user's session cookie). The
`images.pullSecret` (private registries) is mirrored the same way when set. The router
never *reads* a mirror back (create + merge-patch on conflict) — the platform ClusterRole
grants no secret read, so platform secrets remain untouched.

**Data Manager:** SSO users additionally see **Platform Shared (Kubeflow)** and
**Platform Workspace (Kubeflow)** roots (conditional: hidden for local users and while
the platform PVCs are not `Bound`; a unit provisioned in the pending state is re-stamped
automatically once they are). `platformIntegration.readOnly: true` mounts them but keeps
the UI read-only for the platform roots (unix permissions stay the real enforcement).

**Provisioning semantics:** the unit is *born* in the project namespace (K8s objects
cannot move between namespaces) when the user has **no existing unit** — sticky
semantics preserved, so an upgrade never silently moves or resets existing units. A
brand-new SSO user cold-starts once (the warm pool only warms classic/local units).

**Requirements (platform admin sign-off needed):**

1. **Cross-namespace RBAC** — enabling the flag renders a `ClusterRole` +
   `ClusterRoleBinding` for the router and cleanup service accounts (pods/PVCs/services/
   configmaps/deployments/jobs, read/manifest, plus PVC+configmap delete). Secrets,
   leases and VirtualServices stay release-namespace-only. This is a real security grant
   — have the platform admins sign off.
2. **SSO host registration** at the platform auth layer (out-of-band, as before).

**Uninstall contract (enforced):** `helm uninstall` deletes only chart-created objects
in the release namespace **and** each unit namespace from the registry — label-scoped
(`opencode-user-managed=true`), GC-cascaded through per-user **anchor** ConfigMaps
(`opencode-anchor-<slug>`) in the project namespaces, with a skip-assert that refuses any
PVC carrying an `ezprojects.hpe.com/*` label. **The platform PVCs are never deleted** —
and by default (since 1.2.3, `storage.keepPvcOnDelete: true`) **neither are the user data
PVCs** (ws/st, warm-pool claims, per-namespace shared mirror): the hook lists them, keeps
them, and the anchor cascade no longer references them. Namespaces are never deleted.
Unit ConfigMap mirrors (the pod template mounts them
cross-namespace via mirrored copies) are chart-owned and cleaned with the unit.

---

## Demo Content

Pre-seeded demos help you get started immediately.

### Personal workspace (`/workspace/personal/`)

| File | Description |
|---|---|
| `README.html` | Interactive landing page with copy-paste tutorials for three use cases: Basic Web App (confetti button), Intermediate Web App (coin flip), and Advanced Agentic Workflow (financial advisor with stock research, email draft, feedback). Also lists available MCP tools and provides quick-start instructions. |
| `README.md` | Markdown version of the same demo guide and use-case walkthroughs. Alternative to the html file.|

Each tutorial ends with a `create-server.sh` command to launch a preview server and get a shareable URL.

### Shared workspace (`/workspace/shared/`)

| Directory | Tech | Description |
|---|---|---|
| `toy-web/` | Static HTML/CSS | Simple web page — no dependencies, run with `python3 -m http.server` |
| `toy-gradio/` | Gradio | Interactive ML/demo UI — run with the shared venv |
| `toy-streamlit/` | Streamlit | Data app dashboard — run with the shared venv |
| `toy-uvicorn/` | FastAPI + Uvicorn | REST API backend — run with the shared venv |

A shared Python virtual environment (`/workspace/shared/.venv-preview/`) and `requirements.txt` are provided so you can install dependencies once and run any of the Python-based demos.

---

## Terminal

The built-in opencode web terminal can be unreliable in this environment, with text leaking across terminals. To address this, a dedicated **tabbed, multi-terminal UI** (built on ttyd + tmux) is served at:

```
https://opencode.{DOMAIN_NAME}/u-{slug}/terminal
```

It is always available and gives you **multiple independent terminal tabs**, each with the same environment as the opencode agent. Run `opencode` in one tab, `python` in another, and `vim` in a third — they are fully isolated subprocesses.

### Tab model

- **`+`** opens a new terminal tab (a fresh, independent subprocess).
- **Closing the browser tab / reloading the page** only *detaches* — each terminal keeps running in an independent tmux session.
- Reopening `/terminal` lists your still-running terminals and re-attaches them, so you pick up where you left off from your last session.
- **`✕`** on a tab *destroys* it: the subprocess is terminated and the terminal is freed (it will not reappear on reload).
- Terminals persist for the lifetime of your running pod (they are not restored across a pod restart).

---

## Data Manager

A web-based file manager is served at:

```
https://opencode.{DOMAIN_NAME}/u-{slug}/data_manager
```

It provides a full UI for navigating, uploading, downloading, and editing files across the **Personal**, **Shared**, and **Config** (`~/.config`) roots:

- **Browse** — Folder tree with breadcrumb navigation across **Personal**, **Shared**, and **Config** (`~/.config`) roots. Hidden dotfiles (`.config`, `.git`, `.bashrc`, …) are shown in all roots.
- **Upload** — Drag-and-drop or click-to-select file upload into any folder.
- **Download** — Single files download directly (with optional rename); multiple files or folders are archived as ZIP or TAR.GZ with a custom filename.
- **Edit** — Double-click any text file to open it in the Monaco editor (VS Code's editor engine) with syntax highlighting and save support. Binary or large files show metadata with a download option.
- **CRUD** — Create folders, create files, rename/move, and delete (recursive for directories).
- **Storage Status** — A collapsible panel shows real-time PVC usage (bytes + inodes) for Personal, Shared, and State volumes, with color-coded usage bars.

The data manager runs as a zero-dependency Node.js process (`data_manager.mjs`) inside the user pod on port 7682, alongside ttyd and the opencode supervisor. All file operations are path-traversal-protected (clamped to `/workspace/personal`, `/workspace/shared`, and `/var/opencode/home/.config`).

---

## Preview URLs & Port Watcher

A background watcher (`port_watcher.mjs`) polls `/proc/net/tcp` every 3 seconds. When any process listens on a port in the 3000–9999 range (excluding reserved ports), the watcher:

1. Generates a public preview URL: `https://<endpoint>/{username}/__preview/{port}/` — the host is the chart's `ezua.virtualService.endpoint` value, forwarded to the pod as `PREVIEW_UI_HOST` (falls back to `opencode.{DOMAIN_NAME}` when unset), so the printed URL always matches the deployed host
2. Prints the URL directly to the terminal (and all PTY sessions)
3. Writes it to a state file for querying via the `preview-url` helper

The platform's routing layer — the router proxy (`/{username}` + the in-pod
nginx) — forwards traffic to the correct local port.

### Quick start a preview server

```bash
/workspace/create-server.sh /workspace/personal/my-file.html 8000
# → [preview] Preview available for port 8000: https://opencode.{DOMAIN_NAME}/francesco-caliva/__preview/8000/
```

---

## Baked user image (1.2.0)

User pods previously ran vanilla `node:22-bookworm-slim` and installed everything at boot (apt toolchain, `opencode-ai`, `@openchamber/web`, uv, ttyd — several minutes per pod, and the two apt stages re-ran on EVERY restart because apt state lives in the container layer, not on the PVCs). `images.user`/`images.init` now point at a **baked image** (`ghcr.io/ai-solution-eng/opencode-<opencode-version>-openchamber-<openchamber-version>:<tag>`) that carries the whole toolchain under `/opt/opencode` — fresh pods reach Ready in seconds.

Design contract (see `docker/user/Dockerfile`):

- Baked artifacts live under **`/opt/opencode`** (`/opt/opencode/npm`, `/opt/opencode/bin`) — never under `/var/opencode`, which the state PVC mounts and would shadow. The init container aliases the PVC paths (`/var/opencode/data/npm`, `/var/opencode/bin/{uv,uvx,ttyd}`) to the baked copies with guarded symlinks; `opencode-startup.sh` is unchanged.
- **Every runtime install step remains as a guarded fallback.** On the baked image the guards no-op; set `images.user/init` back to `node:22-bookworm-slim` (or bump `opencode.version`/`openchamber.version` without rebuilding) and the old install-on-boot behavior resumes.
- The image tag is folded into `user-template-version`, so a `helm upgrade` with a new tag re-stamps all existing units (dedicated + warm pool) onto the new image automatically.
- User pods pull with `IfNotPresent` — use immutable tags (no `latest`). If the registry package is private, set `images.pullSecret` to an imagePullSecret in the release namespace (public ghcr.io packages pull anonymously).

Build & push (single source of truth: the script reads `opencode.version`, `openchamber.version` and `provisioning.aptPackages` from the values file; the repo name embeds both app versions and the tag is the image revision):

```sh
docker buildx build --platform linux/amd64 \
  -t ghcr.io/ai-solution-eng/opencode-1.18.11-openchamber-1.17.2:0.0.1 --push docker/user
# or simply: scripts/build-user-image.sh --push
```

Then `helm upgrade` (the values files already point at the matching image).

Sanity-test the image without a cluster:

```sh
scripts/test-user-image.sh   # verifies baked artifacts + replays the router's real init guards
```

---

## Quick Reference

```bash
# List active preview ports
preview-url

# Get URL for a specific port
preview-url 8000

# Launch a static file server
/workspace/create-server.sh <file-or-dir> <port>
```

### Key files inside the user environment

| Path | Purpose |
|---|---|
| `~/.config/opencode/opencode.json` | Agent config (editable, hot-reloaded) |
| `~/.config/opencode/agents/` | Custom agent definitions |
| `~/.config/opencode/skills/` | Custom skill definitions |
| `/workspace/personal/` | Your private workspace |
| `/workspace/shared/` | Team shared workspace |
| `/mnt/shared/`, `/mnt/user/` | Platform volumes (SSO users with `platformIntegration.enabled` only) |
| `/workspace/create-server.sh` | Preview server launcher |

## Scripts

- `scripts/build-user-image.sh` — build/push the baked user image (reads `opencode.version` + `openchamber.version` + `provisioning.aptPackages` from the values file; repo name embeds both versions)
- `scripts/test-user-image.sh` — sanity-test the baked image locally (no cluster needed)
- `scripts/push_opencode_json_config.sh` — push an opencode.json into every user env (hot-reloaded)
- `scripts/user_manager.ipynb` — bulk user admin against the router API
- `scripts/cleanup-opencode-web-helm-stale-quotas.sh` — VAST quota cleanup (dry-run default)
