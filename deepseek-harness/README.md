# DeepSeek Harness Web — Multi-User on HPE Private Cloud AI

Deploy and manage multiple isolated [DeepSeek Harness (`dsh`)](https://github.com/deepseek-ai/deepseek-harness) coding-agent environments on **HPE Private Cloud AI**. Each user gets their own PVC-backed workspace, and a shared workspace enables team collaboration.

Port of the `opencode` chart (`opencode-web-helm`): same router, warm pool, admin console, terminal, data manager and preview machinery — with the [`dsh web`](https://github.com/deepseek-ai/deepseek-harness) UI in place of opencode/OpenChamber. **OpenChamber is not used.**

Chart: `dsh-web-helm` · current version **0.2.4** (dsh `0.1.5-rc.1`, Node 22 image).

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
| Personal environment | `https://<host>/u-{slug}` |
| Terminal | `.../u-{slug}/terminal` (ttyd + tmux, tabbed/persistent) |
| Data Manager | `.../u-{slug}/data_manager` |
| Preview for port `PORT` | `.../u-{slug}/__preview/{PORT}/` |

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

Same semantics as the opencode chart (pre-provisioned Deployment+Service+PVCs per unit, warm-first assignment, Recreate strategy, stuck-unit recreation). Tradeoff: each unit idles up to 2 CPU / 2Gi. `warmPool.enabled/size` control it. Pre-delete hook cleans up all dynamic resources on `helm uninstall`.

---

## Troubleshooting (each line cost us a debugging session)

| Symptom | Cause | Fix |
|---|---|---|
| Browser: **403 "RBAC: access denied"** (after SSO login) | ① Router pod has an Istio sidecar → namespace policies enforce on it; ② another VirtualService claims your host (routing hijack) | ① `kubectl label ns <ns> istio-injection=disabled` + restart router; ② `kubectl get virtualservice -A -o yaml \| grep <host>` → pick an unclaimed host |
| Browser: **500 nginx** on main page | nginx named location missing (`@dsh_boot`) | chart ≥0.1.2 |
| Redirect lands on **`:8082`** or **`127.0.0.1:3080`** | nginx `absolute_redirect` expands relative Locations with its listen port; validator passed dsh's loopback URL verbatim | chart ≥0.1.2 (`absolute_redirect off; port_in_redirect off`) and relative token redirect |
| **`/__dsh_boot → 503`** forever | `dsh-auth` container couldn't read the supervisor's boot-state file (no shared mount) | chart ≥0.1.4 (`state` mount, read-only) |
| **502 Bad Gateway** on main page right after claim | dsh web still booting (~60–90s) while nginx is already up | chart ≥0.1.5/0.1.7: readiness probes dsh's root via nginx `/healthz`; also just wait and reload |
| Login page shows at **`/?token=…`** | Router's `/` login-page reservation swallowed dsh's token exchange | chart ≥0.1.6 (`isDshTokenExchange` passthrough) |
| **settings.yaml edits never picked up** | Supervisor's JSON validator rejected YAML | chart ≥0.1.8 (YAML-aware watcher; dsh also re-reads settings per request) |
| Units **stuck NotReady** after 0.1.5 | Readiness probed dsh's nonexistent `/healthz` route | chart ≥0.1.7 (probe hits dsh root — 302 counts as ready); immediate unblock: revert readinessProbe to `tcpSocket 8082` |
| PVCs hang Pending / VAST quota collisions | VAST CSI 64-char quota-name truncation | `scripts/cleanup-dsh-web-helm-stale-quotas.sh` (dry-run by default) |

**Convergence:** per-user runtime changes roll automatically — the router stamps `dsh-web-helm/user-template-version` (RUNTIME_REV + dsh version) and re-templates units whose annotation differs. If ever needed: `kubectl -n <ns> rollout restart deploy -l dsh-user-managed=true`.

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

- `scripts/push_dsh_settings.sh` — push a settings.yaml into every user env (hot-reloaded)
- `scripts/user_manager.ipynb` — bulk user admin against the router API
- `scripts/cleanup-dsh-web-helm-stale-quotas.sh` — VAST quota cleanup (dry-run default)

## Known follow-ups

- dsh is a fast-moving developer preview (`0.1.x`): session-store paths (admin "clear sessions" job) and the remote-MCP config schema should be re-verified per dsh version.
- With SSO enabled, host registration at the platform auth layer remains an out-of-band admin step.
