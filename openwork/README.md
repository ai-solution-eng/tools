# OpenWork Web — Multi-User on HPE Private Cloud AI (P1 scaffold)

Multi-user [OpenWork](https://github.com/different-ai/openwork) deployment on **HPE Private Cloud AI**,
ported from the sibling `opencode-web-helm` chart. Each user gets their own PVC-backed workspace;
a shared workspace enables team collaboration.

> **Status: P1 + P2 + P3 template strip done.** Chart lints/renders clean; unit image bakes
> openwork-server + the pinned OpenCode engine (D1/D4/D5); router and preview supervisor
> fully stripped of OpenChamber machinery and rewired to `openwork-server web` on 127.0.0.1:8787
> (rendered router/preview JS passes `node --check`). Remaining before deploy: S1/S2 spikes
> (loopback/Origin behavior + headless UI flows) and config-surface wiring (P5).

## Credentials (pre-created Secret)

The chart does **not** store credentials in `values.yaml`. It reads them from a
Secret you create in the release namespace and name via `existingAuthSecret`
(default `openwork-web-helm-credentials`). Create it **before**
`helm install`/`helm upgrade`:

```sh
kubectl -n <namespace> create secret generic openwork-web-helm-credentials \
  --from-literal=SESSION_SECRET="$(openssl rand -hex 32)" \
  --from-literal=OPENCODE_SERVER_USERNAME='opencode' \
  --from-literal=OPENCODE_SERVER_PASSWORD='<token>' \
  --from-literal=ADMIN_USERNAME='admin-user' \
  --from-literal=ADMIN_PASSWORD='<password>'
```

All five keys are required:

| Key | Used for |
|---|---|
| `SESSION_SECRET` | Signs the `oc_sess` login cookie and the `oc_admin` console cookie |
| `OPENCODE_SERVER_USERNAME` / `OPENCODE_SERVER_PASSWORD` | OpenWork's bearer/bootstrap token — the preview proxy forwards the browser's `Authorization` header, so this is the token the UI presents, not a Basic credential |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` | Admin console login |

For platform (SSO) units the router mirrors only the `OPENCODE_SERVER_*` keys
into the unit's project namespace — never `SESSION_SECRET`. The Secret is not
managed by Helm: it survives `helm upgrade` and `helm uninstall`. Leaving
`existingAuthSecret` empty falls back to a chart-managed Secret built from
`sessionSecret`/`auth`/`admin` — all five must then be non-empty or the render
fails.

**Rotating a value needs a router restart**, because the router reads these as
env at container start — a patched Secret alone changes nothing:

```sh
kubectl -n <namespace> rollout restart deploy/<release>
```

Rotating `SESSION_SECRET` additionally requires bumping `RUNTIME_REV`: each
unit's auth sidecar carries a literal `SESSION_KEY` derived from it, and only a
re-stamp refreshes that.

**Upgrading from a release that rendered its own Secret (<= 0.1.28):** Helm
deletes `<release>-auth` during the upgrade. Copy the values out first and reuse
the same `SESSION_SECRET`, or every logged-in user is signed out:

```sh
kubectl -n <namespace> get secret <release>-auth -o jsonpath='{.data}' > /tmp/auth.json
```

Note on the image: the unit init **fails fast** if the baked OpenCode engine
version differs from the pinned `opencode.version` (`FATAL: baked opencode engine
… != pinned …`). Bumping `opencode.version` therefore requires rebuilding and
pushing the user image; there is no runtime install fallback in this chart.

## Requirements / build spec

The full DS×GLM cross-validated build spec lives in [`REQUIREMENTS.md`](REQUIREMENTS.md):
- D1: `openwork-server web` single-origin runtime on 127.0.0.1:8787 behind the nginx sidecar (:8082)
- D2: loopback-only server bind; nginx is the sole non-loopback listener
- D3: router cookie gate is the auth boundary; OpenWork bearer tokens as defence-in-depth
- D4: zero `ee/` (OpenWork Den) code — apps/ + packages/ are MIT
- S1/S2 spikes are hard gates before template work continues

## Layout

```
templates/        16 templates, mechanically renamed (openwork-*)
docker/user/      unit image (P2: rewrite to bake openwork-server + pinned OpenCode engine)
scripts/          build/test/push/cleanup scripts (renamed; P2: adapt for openwork)
values.yaml       reference-derived + openwork.* block (serverVersion/opencodeVersion pins)
values.yaml.opencode-reference   pristine copy of the opencode chart values, port provenance
```

## Pushing config to user envs

`scripts/show-current-config.sh` prints the latest config as rendered from `values.yaml`
(read from the `<release>-project-config` ConfigMap, read-only, no pod needed).

`scripts/push_openwork_json_config.sh` **stages** an `opencode.json` into user envs
(`~/.config/opencode/` under the openwork home — the filename is unchanged from the
opencode chart). The push lands as `opencode-update.json` beside the live
`opencode.json`, which is never written: opencode resolves its config from a fixed
candidate list (`opencode.jsonc`/`opencode.json`/`config.json`, not a glob) and the
supervisor watches an explicit file list, so the sibling is inert — nothing reloads
until you promote it (the script prints the exact per-unit command). `--user` scopes a
push to specific users; `TARGET_NAME=opencode.json` restores the old overwrite behaviour.

## Machine gates passed (P1)

- `helm lint` — clean
- `helm template` — 19 docs render; zero stale `opencode-*` identifiers
- VirtualService host: `openwork.${DOMAIN_NAME}`

## Data Manager

Every unit serves a web file manager at `https://openwork.{DOMAIN_NAME}/data-manager` (the legacy
`/data_manager` spelling is aliased in-app), with **Personal**, **Shared** and **Config** roots — plus
**Platform Shared** (`/mnt/shared`) and **Platform Workspace** (`/mnt/user`) on SSO units with
`platformIntegration.enabled`.

Uploads: **Upload** (files **and** compressed archives — .zip, .tar, .tar.gz, .tar.xz, .tar.zst, unpacked
server-side unless *Extract archives* is unticked), **Upload folder** (`webkitdirectory`) and drag & drop of
files *or* folders (empty folders preserved via the entries API).
Per-file streamed requests with progress, cancel and retry; the pre-flight dialog sets the conflict
policy (keep both / skip / overwrite), the permission policy (644/664/755/775) and hidden/temporary-entry
skipping (`.git`, `.venv` — on by default). Per-file ceiling `dataManager.clientMaxBodySize`
(default `2g`); `dataManager.maxUploadMb` (default `2048`) is the server-side guard.

**Previews (0.1.27).** Double-clicking a file previews it in the dialog: images, PDF (native viewer),
audio and video render inline, and **markdown and HTML now render too** — every other extension still
opens as text in Monaco. `.md` is rendered server-side by `/api/preview` inside `data_manager.mjs`:
escape-first, zero dependencies, **no raw-HTML passthrough** (a `<script>` written in a `.md` file is
displayed as text, never executed), with GFM tables, task lists, nested lists, fenced code and relative
links/images resolved against the file's own folder. `.html` is served **verbatim**, so an exported
report keeps its own markup, but under `Content-Security-Policy: sandbox` — opaque origin, scripts
cannot run, nothing remote may load — inside an iframe that also carries `sandbox=""`. Both are
read-only previews; a **Source / Rendered** toggle in the dialog switches back to Monaco so `.md` and
`.html` stay editable. Nothing on this path can execute file content in the data-manager origin:
markdown is escaped before parsing, HTML is isolated twice over.

The server (`data_manager.mjs`, port 7682, zero dependencies) clamps every operation to the
navigable roots, writes through a temp file + `rename()`, and refuses to follow symlinks out of a
root.

## Workspaces

Every unit exposes exactly two workspaces, declared chart-wide in `openworkConfig.serverJson`
(rendered into `<release>-project-config` → `openwork-server.json`, copied onto the unit's state PVC
on first boot):

| Workspace | Path | Backing |
|---|---|---|
| **personal** | `/workspace/personal` | the user's own workspace PVC |
| **shared** | `/workspace/shared` | the release-wide shared PVC |

SSO/platform units (`platformIntegration.enabled`) additionally get **platform-shared**
(`/mnt/shared`, `kubeflow-shared-pvc`) and **platform-user** (`/mnt/user`, `user-pvc`). Those two are
not in `server.json` — that file is chart-wide and shared by classic and platform units — so
`opencode_supervisor.mjs` (`templates/configmap-preview.yaml`) merges them into the unit's own copy at
start when `OPENCODE_PLATFORM_ROOTS === 'mounted'`: chart workspaces first (so picker order follows
`values.yaml`), then the platform pair, deduplicated by path. The same idempotent merge converges a
pre-upgrade unit whose state PVC still carries an older `server.json` (init only seeds that file when
missing), so existing units pick up both chart workspaces on their next restart without losing
workspaces a user added.

Precedence inside openwork-server: `--workspace` CLI > `OPENWORK_WORKSPACES` env > `workspaces` in the
`--config` file. Its `web` command auto-pushes `process.cwd()` as the only workspace when the CLI list
is empty **and** `OPENWORK_WORKSPACES` is unset — which would expose `/workspace` (the parent of
`personal/` and `shared/`) and lose the per-workspace names. Both the supervisor (when `server.json`
declares workspaces; otherwise it falls back to `--workspace /workspace/personal`) and
`docker/user/entrypoint.sh` suppress that fallback by exporting a whitespace-only `OPENWORK_WORKSPACES`
(`parseList()` returns `[]`, but the variable is defined), keeping `server.json` authoritative.
