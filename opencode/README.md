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
| Data Manager | `.../{username}/data-manager` |
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
| `sessionCookieTtlDays` | How long a login lasts, in **days** (default `180`; historically hardcoded to 7). It sets the signed `oc_sess` cookie's `Max-Age` and its `exp` claim, so it controls only **when a user must log in again** — it deletes nothing. Conversations (`opencode.db`), workspace files, `opencode.json`/agents/skills and `auth.json` all survive. Invalid values (`0`, negative, non-numeric) fall back to 7 so a typo cannot lock everyone out, and the result is clamped to [1 hour, 400 days] — 400 days is the hard cap browsers put on cookie lifetime (RFC 6265bis §5.5), so a larger number would just be truncated client-side. The expiry is baked in at issue time, so raising this does not retroactively extend cookies already in users' browsers — they pick it up at their next login. |
| `RUNTIME_REV` | **Bump this to force every user unit to re-stamp** onto the current chart-rendered pod template (nginx.conf, supervisor, probes, env) on the next router reconcile — no chart or template edit needed. It is folded into the `opencode-web-helm/user-template-version` annotation; when the value changes the router patches each Deployment and the unit rolls. Leave it **unset** for normal operation: unset renders the historical marker unchanged (no surprise rollout on `helm upgrade`); any value you set becomes the `-r<value>` suffix. Also settable from an EzAppConfig values blob. |
| `nginx.clientMaxBodySize` | max request body the in-pod nginx accepts (default `64m`). nginx's own default is **1 MB**, which rejects a pasted/dropped image with **HTTP 413** once the browser base64-encodes it into a prompt body — the request never reaches the app. Raise it if users paste very large images. The platform gateway in front of the pod may impose its own, separate limit. |
| `dataManager.clientMaxBodySize` | nginx body cap for the `/data-manager` locations only (default `2g`). The `http{}`-level `nginx.clientMaxBodySize` stays as the UI cap (pasted images); raise both if users need bigger single files. |
| `dataManager.maxUploadMb` | server-side per-file guard (`DATA_MANAGER_MAX_UPLOAD_MB`, default `2048`). Keep it at or above `dataManager.clientMaxBodySize` so nginx stays the binding limit and an oversized file fails before it is streamed. |

To switch the version of either opencode or OpenChamber, change the number in `values.yaml` and repackage. The chart `version` (in `Chart.yaml`) is bumped manually per release; `appVersion` tracks the opencode major.minor family.

### Conversation retention

**Nothing ages out conversations.** Neither this chart nor upstream opencode
deletes sessions/messages by age — chat history in `opencode.db` on the user's
state PVC persists indefinitely and is bounded only by the PVC size
(`storage.stateSize`). Chat history is removed **only** by an explicit admin
action: `POST /__oc_admin/api/sessions/clear` (see
[Admin Console](#admin-console)).

Three things are commonly mistaken for a 7-day session purge — none of them
deletes a conversation:

| Looks like retention | What it actually is |
|---|---|
| `sessionCookieTtlDays` (was hardcoded to 7) | Login-cookie lifetime. When it lapses you log in again; your history is still there. |
| Upstream's 7-day `RETENTION` constant | Prunes `~/.local/share/opencode/tool-output/tool_*` spill files only (truncated tool output), never `opencode.db`. ([source](https://github.com/anomalyco/opencode/blob/main/packages/opencode/src/tool/truncate.ts)) |
| The TUI `/sessions` picker hiding sessions older than 30 days | A display filter. `opencode session list` still shows the full history. ([#16733](https://github.com/anomalyco/opencode/issues/16733)) |

Upstream has no retention setting at all — an `opencode session prune` /
`retention_days` knob is still an open feature request
([#22110](https://github.com/anomalyco/opencode/issues/22110)), and the config
schema has no such key.

#### Sessions are pod-local, not browser-local

Conversations belong to **the pod**, not to a browser or a login. Each user gets
one Deployment + state PVC (`opencode-v2-st-<slug>`), and the chat database
lives on that volume at `/var/opencode/data/opencode/opencode.db`. Upstream
scopes sessions by *project directory* inside that DB (`project` /
`project_directory` / `workspace` / `session` tables, keyed by the project's
`worktree` path), so history is grouped by which folder you were working in —
exactly like running opencode on a laptop, where the store is local to the
machine and the sessions are grouped by project path.

Consequently, **a different browser — including incognito — does not lose
anything.** The cookie only answers "may this browser reach this pod, and as
whom"; the moment you authenticate as the same identity, `slugForUser()` maps
you to the same slug, the same Deployment and the same PVC, and the full history
is there. Cookies expiring, cookies cleared, or a brand-new device change
nothing about stored sessions. The workspace directories (`/workspace/personal`,
`/workspace/shared/<slug>`) are likewise on PVCs and are what the sessions are
keyed against.

If a hard retention limit is ever required, it has to be implemented
deliberately (a CronJob pruning `opencode.db` or `opencode session delete` over
`session list --format json`) — the opencode SQLite schema is internal and
unstable, so pin `opencode.version` before relying on it.

### UI modes

- **`ui.mode: opencode`** (default) — the built-in `opencode web` UI is served on the app port, exactly as before.
- **`ui.mode: openchamber`** — a headless `opencode serve` runs on `ui.serverPort` (loopback, `127.0.0.1`) and [OpenChamber](https://openchamber.dev) is served on `ui.chamberPort` (default `3000`), connecting to it via `OPENCODE_HOST` / `OPENCODE_SKIP_START`. OpenChamber is npm-installed into the user state volume (no separate runtime needed; it runs on the same Node 22 image). The terminal / data-manager / preview portals are unchanged. Because the opencode server is loopback-only in this mode, its HTTP basic auth is disabled and OpenChamber's own password prompt is turned off — access is gated entirely by the signed session cookie (the in-pod nginx `auth_request` gate), so there is no additional "Unlock OpenChamber" prompt.

These are bundled into a ConfigMap and copied into each user's `~/.config/opencode/` on first start. Users can **edit any of these files directly** inside their environment — a watcher (`opencode_supervisor.mjs`) monitors `opencode.json`, `config.json`, `opencode.jsonc` and the `skills/` / `agents/` directories for changes and automatically restarts the opencode process. Just **refresh the browser** to see changes take effect.

---

## Uninstall / `helm uninstall`

A `pre-delete` hook deletes the dynamically-created per-user resources
(which Helm does not own) so the namespace does not hang in `Terminating`.
The hook:

1. **Stops the router first** (deletes the router `Deployment`) so it can no
   longer recreate units while teardown is running.
2. Deletes the per-user **Deployments, Services, and Leases in parallel**.
3. Waits for their **pods to terminate** (releasing the PVC mounts).
4. **Keeps every user data PVC** (default `storage.keepPvcOnDelete: true`):
   per-user `opencode-*-ws-*/-st-*` claims and the per-namespace shared mirror are
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
brand-new SSO user cold-starts once.

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
PVCs** (ws/st, per-namespace shared mirror): the hook lists them, keeps
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
https://opencode.{DOMAIN_NAME}/u-{slug}/data-manager
```

It provides a full UI for navigating, uploading, downloading, and editing files across the **Personal**, **Shared**, and **Config** (`~/.config`) roots:

- **Browse** — Folder tree with breadcrumb navigation across **Personal**, **Shared**, and **Config** (`~/.config`) roots. Hidden dotfiles (`.config`, `.git`, `.bashrc`, …) are shown in all roots.
- **Upload** — Drag-and-drop (files **and** folders) or click-to-select. *Upload* also accepts compressed archives (.zip, .tar, .tar.gz, .tar.xz, .tar.zst) and unpacks them server-side in one request unless *Extract archives* is unticked; *Upload folder* keeps the tree (`webkitdirectory`). Per-file streamed uploads with progress, cancel and retry; the dialog sets the conflict policy (keep both / skip / overwrite), the permission policy (644/664/755/775) and hidden/temporary-entry skipping (`.git`, `.venv`, `~$…` Office lock files, `Thumbs.db` — on by default). Per-file ceiling `dataManager.clientMaxBodySize` (default `2g`).
- **Download** — Single files download directly (with optional rename); multiple files or folders are archived as ZIP or TAR.GZ with a custom filename.
- **Edit** — Double-click any text file to open it in the Monaco editor (VS Code's editor engine) with syntax highlighting and save support. Binary or large files show metadata with a download option.
- **CRUD** — Create folders, create files, rename/move, and delete (recursive for directories).
- **Storage Status** — A collapsible panel shows real-time PVC usage (bytes + inodes) for Personal, Shared, and State volumes, with color-coded usage bars.

The data manager runs as a zero-dependency Node.js process (`data_manager.mjs`) inside the user pod on port 7682, alongside ttyd and the opencode supervisor. All file operations are path-traversal-protected (clamped to `/workspace/personal`, `/workspace/shared`, and `/var/opencode/home/.config`). Uploads stream to a temp file beside the destination and `rename()` into place, so a partial transfer never appears as a file; `.zip` extraction validates every entry before writing anything and never installs symlinks.

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
- The image tag is folded into `user-template-version`, so a `helm upgrade` with a new tag re-stamps all existing units (dedicated) onto the new image automatically.
- The **Helm CLI** is baked under `/opt/opencode/bin/helm` (version pinned via `provisioning.helmVersion` — latest Helm 3 release; Helm 4 exists but the chart and the platform-side install tooling are Helm 3), so `helm package` / `helm lint` / `helm template` run from any pod terminal — e.g. packaging this chart itself from inside the app.
- User pods pull with `IfNotPresent` — use immutable tags (no `latest`). If the registry package is private, set `images.pullSecret` to an imagePullSecret in the release namespace (public ghcr.io packages pull anonymously).

Build & push (single source of truth: the script reads `opencode.version`, `openchamber.version`, `provisioning.aptPackages` and `provisioning.helmVersion` from the values file; the repo name embeds both app versions and the tag is the image revision):

```sh
scripts/build-user-image.sh --values values-g2.yaml --push
# raw equivalent (no proxy handling — see below):
#   docker buildx build --platform linux/amd64 \
#     -t ghcr.io/ai-solution-eng/opencode-openchamber:<opencode>-<openchamber>-dind docker/user
```

**Behind a corporate proxy:** nothing proxy-related is stored in this repo. The
script reads `HTTP_PROXY` / `HTTPS_PROXY` / `NO_PROXY` (or lowercase) **from the
environment at run time** and forwards them as `--build-arg`s, and adds
`--network host` when a proxy is set (inside DinD the default bridge cannot
reach the proxy, and Docker's implicit proxy forwarding does not reach `RUN`
steps). The values are never echoed, so a proxy URL carrying credentials does
not land in CI logs. Pass `--no-network-host` on Docker Desktop/macOS, which
does not support that flag. `NO_PROXY` is always extended with `127.0.0.1`,
`::1` and `localhost` so buildx's own dial to the daemon is never proxied.

```sh
export HTTPS_PROXY=http://user:pass@proxy.example:8080   # your shell only
export HTTP_PROXY="$HTTPS_PROXY"
export NO_PROXY=.cluster.local,.svc,10.0.0.0/8,172.0.0.0/8
scripts/build-user-image.sh --values values-g2.yaml --push
```

This mirrors the `dockerbuild` wrapper baked into the user image, so a build
from a laptop and a build from inside a pod behave the same.

Then `helm upgrade` (the values files already point at the matching image).

Sanity-test the image without a cluster:

```sh
scripts/test-user-image.sh   # verifies baked artifacts + replays the router's real init guards
```

---

## Docker in Docker (1.2.6)

**Docker is an SSO-identity capability** (security decision 2026-10-03: the classic login is a *shared* credential and daemon access is node-root-equivalent, so units resolved by a local session never get a daemon). SSO units get full `docker` power — build, run, tag, **push** — with no cluster-wide docker socket and no host mounts:

- **SSO + platform namespace (`project-user-*`, the default SSO path):** the daemon runs in a **per-user broker pod** in the release namespace (below); the unit's container gets `DOCKER_HOST=tcp://opencode-dind-<user>-svc.<release-ns>.svc.cluster.local:2375` and the baked **docker CLI + buildx plugin**.
- **SSO + release-namespace fallback unit** (no platform project yet): the daemon is an **in-pod sidecar** on a loopback-only listener (`DOCKER_HOST=tcp://127.0.0.1:2375`) — reachable solely through the authenticated session.
- **Local (non-SSO) units: no daemon** — the CLI is present but `docker` reports "Cannot connect to the Docker daemon". Deliberate, not a bug.
- Its `/var/lib/docker` is **node-local `emptyDir`**: overlay2 layer extraction fails on VAST network volumes, so the image store is ephemeral — **push after building** (or `docker save` to the workspace).
- The daemon is not a shared socket: the in-pod sidecar is private to the unit's network namespace; the broker is private to the owning user's unit via the per-user NetworkPolicy.

### Proxied clusters (g2)

The router forwards `proxy.env` to the daemon (pulls/pushes go through `hpeproxy`) and mounts the `certificatesPolicy` CA bundle with `SSL_CERT_FILE`, so the Go daemon trusts the corporate MITM TLS for `ghcr.io`/Docker Hub. **Build** `RUN` steps need two extra adjustments (both proven necessary on g2 via the clearwing + container-builder-mcp work) — use the baked wrapper instead of raw `docker build`:

```sh
cd /workspace/personal/my-app

# RECOMMENDED: proxy-aware wrapper (adds --network host + explicit proxy
# build-args only when a proxy is configured; plain passthrough otherwise)
dockerbuild --platform linux/amd64 -t ghcr.io/<org>/my-app:0.0.1 --push .

# Raw equivalent (proxy env assumed set in your shell):
docker buildx build --platform linux/amd64 --network host \
  --build-arg HTTP_PROXY="$HTTP_PROXY" --build-arg HTTPS_PROXY="$HTTPS_PROXY" \
  --build-arg NO_PROXY="$NO_PROXY" \
  -t ghcr.io/<org>/my-app:0.0.1 --push .
```

Why: the daemon's default bridge network cannot reach `hpeproxy` (builds fail with connection errors), and Docker's *implicit* proxy build-arg forwarding does not reach the `RUN` steps' environment inside DinD — explicit `--build-arg`s do. `--network host` shares the pod network, where the proxy is reachable. `NO_PROXY` must contain `127.0.0.1` (the g2 values already do) so the CLI never proxies its own dial to the daemon.

Pushing to a private registry needs credentials (pods ship none):

```sh
echo "$GHCR_TOKEN" | docker login ghcr.io -u <username> --password-stdin
dockerbuild -t ghcr.io/<org>/my-app:0.0.1 --push .   # build + push in one shot
```

### Caveats

- **Bind mounts don't cross the DinD boundary**: `docker run -v /workspace/x:/x ...` resolves paths inside the *sidecar's* filesystem, not your PVC — the classic DinD limitation. Build contexts are fine (the CLI streams them to the daemon over the API).
- Built images are ephemeral (node-local emptyDir, **disk-capped via values `docker.diskSize`, 50Gi by default** — rendered as both the volume `sizeLimit` and the daemon container's `ephemeral-storage` limit, since the pairing is what makes the cap enforceable; exceeding it evicts the daemon's pod and the build fails with `no space left on device`, never node DiskPressure; `docker.diskRequest` tunes the scheduler reservation). **The blast radius depends on where the daemon runs**: broker mode (SSO/platform units) — only the *broker* pod is evicted and its Deployment restarts it with a fresh store; the user's session stays up. In-pod sidecar mode (classic units) — the eviction restarts the *user's* pod (the session dies with it). Either way, builds/pulls can never grow past the cap or put pressure on the node. Check usage with `docker system df`, reclaim with `docker system prune`; everything is re-pulled/re-built after a reschedule, and a `docker build` cache is likewise lost.
- Each running build/container consumes the pod's resources — the sidecar requests 250m/512Mi with 4 CPU / 8Gi limits (values `docker.resources`).
- **Platform units (`project-user-*`, SSO) get their daemon OUT-OF-POD (default).** The platform Kyverno policies deny privileged containers in user namespaces (observed on tr7: `ezprojects-restrict-privileged-pods` — an in-pod sidecar there makes the unit's Deployment POST fail with 400 and **breaks platform SSO entirely**). So the router deploys a **per-user broker** — one privileged `opencode-dind-<user>` Deployment + Service + NetworkPolicy (ingress restricted to the owning unit's pod) in the **release namespace**, where privileged is admitted — and the unit's terminal reaches it via `DOCKER_HOST=tcp://opencode-dind-<user>-svc.<release-ns>.svc.cluster.local:2375`. Same docker UX, no policy change needed. `docker.brokerForPlatformUnits: false` disables it (platform units then have no docker); `docker.onPlatformUnits: true` moves the daemon INTO the platform pod (loopback) — only after the platform team applies the Kyverno `PolicyException` (`opencode-user-managed=true`, kinds Deployment+Pod, policy `ezprojects-restrict-privileged-pods`).
- **Broker lifecycle:** created on the unit's first SSO resolve and converged on image/values changes (the `opencode-web-helm/dind-template-version` annotation), deleted on user delete, swept by the helm-uninstall hook (`opencode-user-managed=true`). Sizing reuses `docker.resources` / `docker.diskSize`.
- **Broker security note:** the docker API on 2375 is **unauthenticated** — the NetworkPolicy scopes dialing to the owning unit's pod; on clusters whose CNI ignores NetworkPolicy the exposure widens to the whole cluster (keep `brokerForPlatformUnits: false` there).
- Enable/disable per environment with values `docker.enabled` (requires the cluster to admit privileged containers; off by default in the HT/devday values).

Ready-made `PolicyException` for the platform team (one per `project-user-*` namespace; the two `any:` entries are ORed — dsh units carry `dsh-user-managed`, opencode units `opencode-user-managed`, never both):

```yaml
apiVersion: kyverno.io/v2
kind: PolicyException
metadata:
  name: dsh-opencode-dind-privileged
  namespace: project-user-<name>
spec:
  background: true
  exceptions:
    - policyName: ezprojects-restrict-privileged-pods
      ruleNames:
        - autogen-restrict-privileged-pods-user-namespace
        - restrict-privileged-pods-user-namespace
  match:
    any:
      - resources:
          kinds: [Pod, Deployment]
          selector:
            matchLabels:
              dsh-user-managed: "true"
      - resources:
          kinds: [Pod, Deployment]
          selector:
            matchLabels:
              opencode-user-managed: "true"
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
- `scripts/show-current-config.sh` — print the **latest** config as rendered from `values.yaml` (read from the `<release>-project-config` ConfigMap; read-only, no pod needed). Use it to hand the current config to the team, or to diff against what a user actually has: `scripts/show-current-config.sh [--out FILE] [--namespace NS]`
- `scripts/push_opencode_json_config.sh` — **stage** an opencode.json into user envs. Its source is `--from-configmap` by default (the same ConfigMap the script above reads), so `helm upgrade` → `push` needs no pod and no local file. The push lands as `~/.config/opencode/opencode-update.json` beside the live `opencode.json`, which is never written: opencode resolves its config from a fixed candidate list (`opencode.jsonc`/`opencode.json`/`config.json`, not a glob) and the supervisor watches an explicit file list, so a sibling is inert — nothing reloads until you promote it (the script prints the exact per-unit command). `--user` scopes a push; `TARGET_NAME=opencode.json` restores the old overwrite behaviour.
- `scripts/user_manager.ipynb` — bulk user admin against the router API
- `scripts/cleanup-opencode-web-helm-stale-quotas.sh` — VAST quota cleanup (dry-run default)
