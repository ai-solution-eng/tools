# openwork-web-helm — Consolidated Requirements & Build Spec

Multi-user OpenWork (different-ai/openwork) on HPE Private Cloud AI, ported from the sibling
`opencode` chart (`opencode-web-helm` v1.2.14). Folder: `tools/openwork/`.

Provenance: DS (Deepseek-V4.1-Flash) proposal → GLM (GLM-5.3-Flash) factual-integrity review
(7 findings, all incorporated) → machine verification against upstream docs.
Marked **[V]** = verified in this repo / upstream docs; **[U]** = gated on the named spike.

---

## 0. Machine-verified foundations (2026-10-21)

| Fact | Status |
|---|---|
| `different-ai/openwork` exists; "The open-source alternative to Claude Cowork (powered by opencode)"; desktop app MIT-licensed | [V] github.com/different-ai/openwork |
| Official self-host path: `npm i -g openwork-server && openwork-server web` runs the full web app from one process (single origin) | [V] openworklabs.com/docs/changelog |
| Upstream also publishes its own Helm chart for the **Den control plane** (openwork-den-web :3005, openwork-den-api :8788, MySQL 8, images on GHCR) | [V] openworklabs.com/docs/start-here/self-host |
| "For a standalone workspace runtime, deploy OpenWork server and use its web client" | [V] same |
| Desktop embedded server listens only on loopback | [V] same |
| `ee/` (Den) is the licensing boundary; `apps/`+`packages/` MIT | [V] repo README |
| Our chart ≠ upstream chart: ours is a **per-user workspace-runtime** chart (opencode-chart lineage); upstream chart is the **org control-plane** (Den) chart. Complementary, not competing. | [V] shapes differ |

---

## 1. Architecture decisions (D1–D7)

- **D1 — Runtime = `openwork-server web`, single origin, port 8787.** Not the Vite `5178`/`8778` dev-world pair. Matches upstream's own Docker packaging and the verified self-host command.
- **D2 — Server binds loopback only (`127.0.0.1:8787`); the existing per-pod nginx sidecar (`openwork-preview-proxy`, `:8082`) is the only non-loopback listener.** Neutralises the documented loopback limit by construction. (GLM fix #1/#3 applied to the probe/container model — see §4.)
- **D3 — Auth = router cookie gate (SSO + local accounts).** OpenWork's own bearer tokens stay enabled behind it (`webBootstrapToken: true` default); `secret.yaml` can seed tokens if flipped.
- **D4 — Zero `ee/` code shipped.** No Den, no MySQL, no den-* images.
- **D5 — OpenCode engine baked into the image** (`OPENWORK_OPENCODE_BIN=/usr/local/bin/opencode`), pinned to OpenWork's `constants.json.opencodeVersion`; no first-boot GitHub download (air-gap).
- **D6 — OpenChamber machinery removed entirely** (UI, install guards, version markers, `ui.mode`).
- **D7 — Chart ships exactly the sibling 16-template file set; no new templates.** `openwork-server.json` rides in the existing `configmap-project.yaml`.

---

## 2. Chart skeleton

### 2.1 Per-file disposition

| File | Disposition |
|---|---|
| Chart.yaml | rewrite (name `openwork-web-helm`, v0.1.0, appVersion = baked openwork-server version) |
| values.yaml | rewrite (§7) |
| configmap-router.yaml (4214 L) | heavy edit: keep ~85% (anchors, warm pool, leases, SSO, admin, preview, ttyd, data-manager, setup page, VAST logic); strip OpenChamber; rename identifiers; add `OPENWORK_*` env |
| configmap-preview.yaml | edit: add `/` → `127.0.0.1:8787` location + SSE/WS headers; keep preview/term/data_manager; rebrand |
| configmap-personal.yaml, configmap-shared.yaml | verbatim |
| configmap-project.yaml | edit: keep `opencode.json` key; add `openwork-server.json` key |
| persistentvolumeclaims.yaml | **verbatim byte-identical** [V: identical across both sibling charts]. GLM fix #4: any *added* comment or helper rename breaks byte-identity — either keep truly byte-identical or enumerate every deviation; the VAST quota-name-early rule must not be touched. |
| deployment.yaml | edit: env (§5), probes (§4 — **exec-based**, GLM fix #1), checksum/config |
| rbac.yaml, service.yaml, serviceaccount.yaml, virtualservice.yaml, authorizationpolicy.yaml, kyverno.yaml | copy + rename identifiers only |
| secret.yaml | edit: admin creds; optional token seeding when `webBootstrapToken: false` |
| pre-delete-cleanup.yaml | edit: add openwork data paths to the compute-phase list; PVC rule unchanged (list+log, never delete) |

### 2.2 Naming scheme

| Sibling | openwork |
|---|---|
| chart `opencode-web-helm` | `openwork-web-helm` |
| label `opencode-user-managed=true` | `openwork-user-managed=true` |
| anchor CM `opencode-anchor-<slug>` | `openwork-anchor-<slug>` |
| PVCs `opencode-v2-ws/st-<slug>`, `opencode-user-warm-N-workspace-pvc-v2` | same with `openwork-` prefix |
| containers `opencode-web` / `opencode-preview-proxy` | `openwork-web` / `openwork-preview-proxy` |
| paths `/var/opencode/*`, `/opt/opencode*` | `/var/openwork/*`, `/opt/openwork*` |
| admin `/__oc_admin` | `/__ow_admin` |
| user URL `/{username}`, previews `/{username}/__preview/{PORT}/` | unchanged (router contract) |

**VAST 64-char check [V-math]:** `openwork` ≡ `opencode` in length (8 chars), suffixes identical →
inherits the sibling's exact character budget; stale-quota cleanup script transfers with a prefix swap.

### 2.3 Version policy

Three independent pins surfaced in `userTemplateVersion = ow-<serverVersion>-<opencodeVersion>-<imageTag>-rev<RUNTIME_REV>`:
1. `openwork.serverVersion` (npm, drives appVersion) — **MUST be ≥ 0.18.50: that is when the `web` command + bundled browser UI (`web/index.html`) first shipped; 0.18.4 is API-only and serves `{"code":"not_found"}` at `/` (verified live 2026-10-21). Current pin: 0.18.59 (latest).**
2. `openwork.opencodeVersion` (engine binary; app asserts the pin)
3. `images.user.tag`
`RUNTIME_REV` bump → all units re-stamp (Recreate); PVCs untouched.
**GLM fix #6:** token determinism (if used) is stable only while `RUNTIME_REV` is stable — any
re-stamp rotates tokens. State this explicitly in the spec and README; do not imply permanence.

---

## 3. Unit image (`docker/`)

npm-based, mirroring upstream's packaging Dockerfile [V: upstream ships one]:

- Base `node:22-bookworm-slim`; deps: ca-certificates curl git tar unzip tini procps less ripgrep.
- `npm install -g openwork-server@<pin>` (package bundles the built web UI + OpenCode plugin deps — no source build for v0.1.0).
- Bake the OpenCode engine binary for TARGETARCH (x64-baseline / arm64) → `/usr/local/bin/opencode`.
- Non-root default identity 1000:1000, `HOME=/var/openwork/home`, XDG dirs on the state PVC.
- Entrypoint (single source of truth):
  - platform identity block: root-only chown/useradd + `setpriv --reuid --regid --init-groups --` **with the early-exit when admission already forced the tenant uid** [V: tr211 lesson in sibling];
  - seed-if-missing config from `/opt/openwork-defaults` (never clobber user edits);
  - symlink org skills from `/workspace/shared/_org/skills/*` (and `/mnt/shared/_org/skills` when platform integration is on);
  - `exec openwork-server web --host 127.0.0.1 --port 8787 --workspace /workspace/personal --config <server.json> --approval <mode>`.
- Ports: 8787 loopback (server), 8082 nginx (only non-loopback listener), 7681 ttyd, ephemeral managed engine.

---

## 4. Probes — GLM fix #1/#3 (corrections incorporated)

**Machine rule:** kubelet runs `httpGet`/`tcpSocket` probes **from the node against the pod IP**;
they can never reach a loopback-bound process. Only `exec` probes run inside the container.
Since D2 binds the server to 127.0.0.1, **all probes are `exec` probes**:

```yaml
# single app container ("openwork-web"); nginx is the only other container
startupProbe:
  exec: ["curl","-fsS","http://127.0.0.1:8787/health"]   # + engine version check
  periodSeconds: 5
  failureThreshold: 60
readinessProbe:
  exec: ["curl","-fsS","http://127.0.0.1:8787/health"]
  periodSeconds: 10
  failureThreshold: 3
livenessProbe:
  exec: ["curl","-fsS","http://127.0.0.1:8787/health"]
  periodSeconds: 30
  failureThreshold: 6
  initialDelaySeconds: 60
```
nginx sidecar probes: `httpGet :8082/healthz` (nginx binds 0.0.0.0 → pod-IP-reachable, so httpGet is legal there).
**Probe command:** `sh -c 'curl -sS -o /dev/null -m 2 http://127.0.0.1:<port>/'` — no `-f`: any HTTP response
(including 401 from bearer auth) counts as alive; non-zero exit only when nothing is listening.
(Verified live 2026-10-21: first real SSO unit boot passed Init, server listening on 127.0.0.1:8787, old tcpSocket probes failed with connection refused — replaced.)
No per-process probe rows (server/UI/engine are one container under tini).

---

## 5. Router changes

Strip: `ui.mode`, `OPENCHAMBER_*`, chamber version markers, `:3000` proxy branch, chamber branding, chamber-mode image-test scenario.
Keep (rename-only): anchor machinery, warm pool + claim-by-lease, slug derivation (54-char cap, legacy `/u-<hex>` adoption), SSO+local login, setup-status page (+ openwork health sub-checks), preview machinery + port watcher, ttyd, data manager, `userTemplateVersion` convergence, `deleteUserEnvironment` (never deletes PVCs), platform-integration branch, `nginx.clientMaxBodySize`, per-ns shared PVC mirror, pre-delete phases.
Port constants: `UI_PORT=8082` (nginx), `APP_PORT=8787` (loopback), `TTYD_PORT=7681`, `DATA_MANAGER_PORT=7684`; engine port never routed.
Env rule: `OPENWORK_*` for server config; `OPENCODE_*` retained where they are the engine's own contract (`OPENCODE_DB`, `OPENCODE_CONFIG_DIR`); chart-internal names unchanged for code reuse. Key vars: `OPENWORK_DATA_DIR`, `OPENWORK_RUNTIME_DB` (deterministic path → exact clear-chats), `OPENWORK_SERVER_CONFIG`, `OPENWORK_OPENCODE_BIN`, `OPENWORK_NO_UPDATE_CHECK=1`, `OPENWORK_APPROVAL_MODE=auto` (manual has no approvals responder in web mode), `OPENWORK_CORS_ORIGINS`, `NPM_CONFIG_AUDIT=false`, `USER_INFO_*`.
Path B (Vite+server split with an in-container forwarder) is documented under `docker/fallback/` as **unsupported** — it reintroduces the dev-world credential posture upstream gates to loopback. (GLM fix #2/#3: inside a pod all containers share netns, so `127.0.0.1` across containers is correct; spec text must state this once, cleanly.)

---

## 6. Config surface

- `opencodeConfig` reuse unchanged: values → `<release>-project-config` `opencode.json` → `/opt/openwork-defaults` → copy-if-missing to `$XDG_CONFIG_HOME/opencode/opencode.json`; `scripts/push-config.sh --apply --backup` for bulk propagation. **[U] Config precedence (S4):** openwork-server passes its own runtime `OPENCODE_CONFIG` to the engine — verify the chart's providers/MCP defaults still apply; else deliver via the runtime config path.
- `openwork-server.json` (new key in configmap-project.yaml): host/port/approval/workspaces/corsOrigins/`authorizedRoots: [/workspace, /mnt/shared, /mnt/user]`/readOnly/logFormat. `${DOMAIN_NAME}` rendered by Helm as a value, never into JS template literals.
- Skills/MCP sharing: org layer on shared PVC (`_org/skills/*`, `_org/mcp/*`) symlinked into the engine's skill dir at every boot (warm pool included); user layer via OpenWork's own workspace skills/MCP APIs; chart `mcp` defaults via opencode.json.
- auth.json (engine provider keys) stays per-user on the state PVC, never chart-managed; fleet-wide provider keys go through opencode.json (ConfigMap-readable — document; optional `existingSecret` enhancement). OpenWork tokens auto-generated, persisted on state PVC; never handed to the router (approval authority stays in the pod).

---

## 7. values.yaml key inventory (defaults)

`images.{router,user,init,nginx}` (+ pullSecrets, `pullPolicy: IfNotPresent|Always` — user containers + router; infra images keep fixed policies) · `openwork.{serverVersion,opencodeVersion,approvalMode,webBootstrapToken,corsOrigins,readOnly,logFormat,sandboxEnabled=false,tokenSecret}` · `RUNTIME_REV: 1` · `admin.{username,password}` · `ezua.virtualService.{endpoint: openwork.${DOMAIN_NAME}, istioGateway}` + `authorizationPolicy` · `platformIntegration.{enabled:true,readOnly,projectNamespacePrefix}` · `storage.{storageClassName, nameVersion:v2, personalWorkspaceSize:20Gi, stateSize:10Gi, sharedWorkspaceSize:50Gi, keepPvcOnDelete:true, accessModes:[RWX]}` · `warmPool.{enabled:true,count:2,...}` · `user.resources` (500m/2Gi → 4/8Gi) · `router.resources` · `nginx.clientMaxBodySize: 64m` · `previews/terminal/dataManager` toggles · `openworkConfig.{opencodeJson, serverJson}` · `service.port: 8080` · security contexts · `kyverno.enabled`.
`values-g2.yaml` / `values-dev-day.yaml` = **standalone** copies (sibling footgun: overlays silently drop keys).

---

## 8. Sessions / admin

Deterministic DB paths make clear-chats exact (`rm -f` both DBs + WAL/SHM + legacy storage tree;
NFS `.nfs*` retry tolerance) then **restart the unit** (server holds the DB open in WAL mode).
Verify from inside the pod (GLM fix #5): `GET http://127.0.0.1:8787/api/sessions` — *via the forwarder port only in Path B; name the reachable endpoint exactly in the gate script*.
`/__ow_admin` keeps the sibling feature set + two read-only panels: engine health (`/health` reports pinned vs installed engine version) and token mode.

---

## 9. platformIntegration carryover (unchanged behaviour, renamed)

SSO units born in `project-user-<username>` ns; identity from `project-info` CM (`USER_INFO_*` required else degrade); platform PVCs mounted by reference, never owned/deleted (skip-assert on `ezprojects.hpe.com/*`); `setpriv` tenant identity; anchors in release ns; `readOnly` mode; warm pool release-ns-only. OpenWork additions: platform roots into `authorizedRoots`; org-skill symlink source switches to `/mnt/shared/_org/skills`; `OPENWORK_SANDBOX_ENABLED=0`.

---

## 10. Risks / spikes (gate before template work)

| # | Risk | Test | Pass |
|---|---|---|---|
| S1 | Loopback limit broader than docs (Origin/Host validation in `web` mode) | in-pod: `curl -H 'Origin: https://…' http://127.0.0.1:8787/health` + via nginx with public Host; corsOrigins pinned to public origin | 200 both; else S1b |
| S1b | Server-side remote-access refusal path | grep published package dist for refusal strings; locate env/flag | flag found or patch in source build |
| S2 | `web` UI assumes Electron shell | exercise 7 flows (workspace, message, gated write, model switch, skills, MCP, inbox upload) via nginx | all pass, no `window.electron` errors |
| S3 | UI branding patch needed → source build | build `Dockerfile.source` (pnpm 10.x, Node 22), diff vs npm bundle | reproducible <15 min |
| S4 | Config precedence: chart opencode.json vs server runtime config | marker provider in chart config → `GET /workspace/:id/config` | marker visible or documented alternate delivery |
| S5 | Den licensing leakage | `grep -r 'ee/' Dockerfile scripts/` → zero hits | zero artifacts |

**Hard gate: S1 (and S2 for UI usability) must pass before template work.**

---

## 11. Implementation phases (machine-verifiable exits)

- **P0 Spikes S1/S2** — exit: S1 verdict recorded; 7-flow checklist green or Path-B/patch decision escalated.
- **P1 Skeleton** — copy+rename 16 templates; `helm template` renders clean; naming grep: zero `opencode-user-managed`/`/__oc_admin` remnants; PVC template byte-identical to sibling.
- **P2 Unit image** — build + `test-user-image.sh` (3 scenarios, chamber fallback dropped); `verify-openwork-patches.sh` green (pinned engine + server versions, bundle present, zero openchamber strings).
- **P3 Router** — dry-run deploy in a scratch ns; setup-status reaches ready; login → `/{username}` serves UI through nginx.
- **P4 Probes/auth** — exec probes pass; unauthenticated request to pod:8082 → 403/redirect; NetworkPolicy negative test times out.
- **P5 Config/skills** — seed-once semantics (mtime stable across restarts); user A's org skill visible to user B; opencodeConfig round-trip verbatim.
- **P6 Admin/clear-chats** — dry-run lists exact paths, deletes nothing; real run preserves auth.json + workspace hashes; fresh session after restart.
- **P7 platformIntegration** — uid/gid == project-info; mounts correct; both-charts-coexistence test renders the guard `fail`.
- **P8 Hardening** — warm pool ≥ count, claim <5s; anchor scan deletes orphaned units; chart upgrade re-stamps without touching PVCs.
