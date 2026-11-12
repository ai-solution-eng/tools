# Deployment — pcai-llm-gateway

## The deployment contract

Deployment is a values problem: **import the packaged chart into PCAI once,
create the required Secrets out-of-band, then everything below is edited in
the chart's values** (PCAI **Helm Values** editor, or the PCAI API) and
re-applied. Spelled out:

- **The chart is packaged and imported once** (AI Essentials / EzAppConfig
  import — EzAppConfig is PCAI's custom resource for a vendor-packaged app;
  PCAI uploads the chart to its ChartMuseum — the in-cluster Helm chart
  registry — and its controller installs it). The packaged artifact is
  `pcai-llm-gateway-<ver>.tar.gz`; this document matches chart 4.1.0.
- **The operator's only surface is the values editor.** Every site-specific
  decision must be a value. No template edits, no post-render patching, no
  editing rendered manifests.
- **What PCAI does for you:** `${DOMAIN_NAME}` substitution in pasted values
  before rendering, the Istio VirtualService / `ezua` block on the
  `ezaf-gateway`, namespace creation, and the `hpe-ezua` vendor labels.
- **What you must do out-of-band**, because it cannot be a value: **Secrets**
  (admin keys always; the Postgres URL Secret in the default
  external-Postgres mode; optionally the Fernet master key) and **cluster
  objects** (the namespace Istio-injection label that must move atomically
  with the AuthorizationPolicy, the SSO edge chain's route classes, optional
  Kyverno). See [Prerequisites](#prerequisites) — each item there carries its
  purpose, failure mode and verification.
- **Upgrade = import the new chart version, keep the values, re-apply**
  (§ Upgrading). Operators running plain Helm do the same with
  `-f <values-file>` — that is the developer/secondary path, not the PCAI
  path; per-cluster credentials-bearing values live in `helm/local/`
  (gitignored, never packaged).

## Prerequisites

Ordered: create the Secrets (A), confirm the cluster objects (B) and the posture values that gate a safe install (B2), prove the external dependencies **from inside the cluster** (C), then import. (D) is
external dependencies **from inside the cluster** (C), then import. (D) is
not something you create — it is the set of values the chart refuses to
render without.

The chart **never creates a Secret and never inlines a credential in
values** by default — an envsubst apply once emptied a live key Secret (the
K8S-MCP lesson). Every credential is referenced by name; the pod fails loud
(`CreateContainerConfigError`) until the referenced Secrets exist. These
are operator-only steps — PCAI users import the chart and edit values only.

### A. Secrets the operator creates (before install)

| Secret (chart default name) | Keys | Consumed by | Why it exists | Fails how without it | Create with |
|---|---|---|---|---|---|
| `pcai-llm-gateway-admin` | `admin-api-keys` (comma-separated = staged rotation) | `/admin` API + console (`require_admin` hash-compares against every listed key) | The control plane's only credential — the admin key IS the authorization | `CreateContainerConfigError` — the pod never starts; nothing else in the deployment substitutes for it | `kubectl -n <ns> create secret generic pcai-llm-gateway-admin --from-literal="admin-api-keys=$(openssl rand -hex 32)"` |
| `pcai-llm-gateway-db` | `database-url` (`postgresql://user:pass@host:5432/db`) | The gateway (`PCAI_LLM_DATABASE_URL`) — **external-Postgres mode only** (the default) | Carries the Postgres credentials the chart must never inline; the schema auto-applies at startup | `CreateContainerConfigError` at pod start; Secret present but DB unreachable → `/ready` fails and the pod leaves the Service | `kubectl -n <ns> create secret generic pcai-llm-gateway-db --from-literal=database-url='postgresql://<user>:<password>@<host>:5432/<database>'` |
| `pcai-llm-gateway-pg-auth` | `password` | The embedded Postgres StatefulSet AND the gateway (`PCAI_LLM_DB_PASSWORD` — one Secret feeds both) — **embedded mode only** | One credential for both containers of the co-deployed pair; never inlined in values | Render fails earlier when the *name* is unset (see "Keys the chart refuses to render without"); name set but Secret absent → `CreateContainerConfigError` on both pods | `kubectl -n <ns> create secret generic pcai-llm-gateway-pg-auth --from-literal=password=$(openssl rand -hex 24)`, or `database.embedded.auth.create: true` |
| `pcai-llm-gateway-secrets` | `master-key` | Panel-stored Fernet credentials (endpoint/MCP tokens, per-key MCP header overrides) and the audit-chain HMAC anchors — recommended, and required by the DEFAULT render | Fernet-encrypts every console-pasted credential; the audit trail's tail anchor is keyed from it | Default render (the name is preset in values.yaml): pod never starts (`CreateContainerConfigError`). Opt OUT by setting `secretsMasterKey.existingSecret: ""` — then panel credential **writes 503** (by-ref Secret names keep working) and `GET /admin/audit/chain` reports `no-master-key`/`unanchored` — treat as *not verified*, not as tamper | `kubectl -n <ns> create secret generic pcai-llm-gateway-secrets --from-literal="master-key=$(openssl rand -hex 32)"` |

```bash
# 1. Admin API keys for /admin (comma-separated = rotation mechanism:
#    staged rotation: append the new key AND restart the pod (env from Secrets is read once at process start), migrate clients, drop the old key at the next restart).
kubectl -n <namespace> create secret generic pcai-llm-gateway-admin \
  --from-literal="admin-api-keys=$(openssl rand -hex 32)"

# 2. PostgreSQL connection string — EXTERNAL mode only (the default).
#    Point it at a database the gateway may create tables in — the schema is
#    applied automatically at startup.
kubectl -n <namespace> create secret generic pcai-llm-gateway-db \
  --from-literal=database-url='postgresql://<user>:<password>@<host>:5432/<database>'

# 3. Optional (recommended) — Fernet master key for panel-stored credentials.
kubectl -n <namespace> create secret generic pcai-llm-gateway-secrets \
  --from-literal="master-key=$(openssl rand -hex 32)"
```

Names are the chart defaults (`adminApiKey.existingSecret` /
`database.existingSecret` / `secretsMasterKey.existingSecret` /
`database.embedded.auth.existingSecret`); if your Secrets differ, change the
names in values, not the chart.

**Trial-friendly alternative — chart-generated Secrets.** Flip
`adminApiKey.create: true`, `secretsMasterKey.create: true` and (in embedded
mode) `database.embedded.auth.create: true` and ONE helm install generates
the Secrets; every later apply re-reads the existing value via `lookup()`
(nothing is regenerated or emptied, and `helm uninstall` deliberately leaves
them in place). ⚠️ Do NOT use the `*.create` flags if your apply pipeline
renders without cluster read access (offline `helm template`, CI apply gates)
— `lookup()` returns nil there and an apply would regenerate the values.
Such sites stay on the out-of-band path above. In embedded mode,
`POSTGRES_PASSWORD` seeds the data dir on FIRST init only — never rotate
that Secret without `ALTER USER` on the database too.

### B. Cluster objects (the things that cannot be a value)

| Object | Why it is required | What breaks without it | Verify |
|---|---|---|---|
| Release namespace labeled `istio-injection=enabled` — ONLY together with the AuthorizationPolicy (one change, pods restarted once) | AuthorizationPolicy is mesh L7: no sidecar, no enforcement point | Policy without injection = a silent NO-OP (verified live on G2: pods carried no istio-proxy); injection without policy = the proxy identity headers become spoofable at the pod | `kubectl get ns <ns> --show-labels | grep istio-injection`; `kubectl get pods -n <ns> -o jsonpath='{.items[*].spec.containers[*].name}' | tr ' ' '\n' | grep -c istio-proxy` |
| SSO edge-chain route classes at the ezaf-gateway/oauth2-proxy layer | Browser paths (`/`, `/ui`) chain through Keycloak; `/v1/*`, `/mcp/*`, `/admin` are skip-auth — the app's own key is the control; `/v1/identity` + `/v1/keys/for-me` MUST stay chained (their identity IS the injected headers) | A blanket 302 to Keycloak (the MM RAG lesson, D25) leaves bare REST clients with `hllm-` keys unable to reach the app at all; unchained identity endpoints let `for-me` mint keys for whoever asks | Browser: chat page renders after the Keycloak redirect. Off-cluster: `curl -s https://<host>/v1/models -H "Authorization: Bearer hllm-<key>"` returns 200 |
| DNS for the route host (the `${DOMAIN_NAME}` name) | Clients must resolve the VirtualService host to the ezaf-gateway | Off-cluster clients cannot connect at all; in-cluster `*.svc.cluster.local` traffic is unaffected | `getent hosts pcai-llm-gateway.<your-domain>` from a consumer network |
| ServiceMonitor CRD — when `metrics.enabled`; PrometheusRule CRD — when `metrics.enabled` AND `metrics.prometheusRule.enabled` (BOTH flags; `prometheusrule.yaml:1` — one flag alone renders no object and NO error) | prometheus-operator custom resources the chart renders into | The apply fails (missing CRD) or the objects exist but never scrape/evaluate; with only `prometheusRule.enabled` set you silently get no rules at all | `kubectl get crd servicemonitors.monitoring.coreos.com prometheusrules.monitoring.coreos.com` |
| Metrics API provider (metrics-server or equivalent) — only when `autoscaling.enabled` | The HPA reads resource metrics through `metrics.k8s.io` | HPA shows `<unknown>/70%` forever and never scales | `kubectl get --raw /apis/metrics.k8s.io/v1beta1` returns (not 404) |
| Kyverno ClusterPolicy (`kyverno.enabled`) — optional, needs cluster-admin to CREATE (the object is cluster-scoped) | Stamps the `hpe-ezua/*` vendor labels on THIS release's objects only — the rules match the release namespace (`kyverno.yaml:34-35`); labelling any OTHER namespace is the operator's prerequisite, not this policy | Console tile shows "Unknown" — cosmetic: the chart stamps the labels statically on the pod template + Service, so this release's pods are covered either way | EZUA console app tile; `kubectl get clusterpolicy` |

<!-- VERIFY (SSO edge-chain row): the skip-auth / chained path classification is
platform-edge configuration (oauth2-proxy / ezaf-gateway) that NO template in
this chart renders or verifies — only the live cluster confirms a given site's
classes. The G2 classification is recorded live-verified in
changelogs/SSO-ROLLOUT-G2.md; other sites must confirm their own edge. -->

### B2. Posture values that gate a safe install (these ARE values — set them deliberately)

These are values in the Helm Values editor — the deployment contract above
holds. They sit in the prerequisites because a default-valued install is
either broken or exposed, the correct fix is always a VALUES edit (never a
cluster object), and each has a verification step worth running at install
time rather than after the first incident.

| Value | Why it must be set deliberately | What breaks without it | Verify |
|---|---|---|---|
| `ezua.authorizationPolicy` relief rules (`allowMetricsScrape`, `allowInClusterApi`, optionally `excludeInboundPorts`) — under `ezua.authorizationPolicy.enabled: true`, which itself defaults **false** | Once the policy is enabled, its ALLOW baseline (ingress-gateway + same-namespace) admits neither kube-prometheus scrapes nor unmeshed in-cluster callers | `allowMetricsScrape` missing → Prometheus `up == 0` for this target forever (a raw-IP scrape is envoy passthrough — plaintext, no peer identity — even from a meshed scraper); `allowInClusterApi` missing → unmeshed consumers (dsh-web-helm, OpenWebUI namespaces, opencode-web-helm, clearwing) 403 at the mesh on their first call | Prometheus targets page shows the gateway target `up`; one real model call from an in-cluster consumer |
| `database.embedded.persistence.storageClass` (`""` = cluster default) — embedded mode | The StatefulSet's `pgdata` PVC must bind before Postgres starts | Postgres StatefulSet Pending (unbound PVC) → gateway `/ready` fails and the pod leaves the Service | `kubectl get pvc -n <ns>` shows Bound |
| `imagePullSecrets` — only when the image comes from a mirror/private GHCR | Both the gateway Deployment AND the embedded Postgres StatefulSet pull through them | `ImagePullBackOff` on first boot | `kubectl describe pod -n <ns>` → Events |

### C. External dependencies

| Dependency | Used for | Prove it from the cluster |
|---|---|---|
| Postgres (external mode) | The entire control plane — ledger, budgets, keys, registry | After install: `curl -s https://<host>/ready` → 200 (a live Postgres round-trip), or from any pod: `kubectl exec -n <ns> deploy/pcai-llm-gateway -- python -c "import asyncio,asyncpg;asyncio.run(asyncpg.connect('postgresql://...'))"` |
| Model endpoints (vLLM/SGLang / MLIS) | The `/v1` proxy pools | Admin console Models tab `test` button — a real `GET /v1/models` with the endpoint's own credential (`POST /admin/models/{name}/probe`); the result (latency, error) renders in the row |
| MCP servers | `/mcp` federation + the tool relay | `GET /admin/mcp-servers/{name}/tools` — a real `list_tools`, 15 s bound; or the MCP tab's test button |
| Prometheus — only for off-peak billing, model groups, endpoint metrics | Demand evaluators, live-load snapshots, KV-cache strips | Admin ▸ Off-peak shows a live sample or a named error; `GET /admin/endpoint-metrics` returns non-null `kv_*` fields |
| Keycloak JWKS — only when `settings.tokenValidation.*` is set | JWT validation (identity Phase B) | The JWKS URL is fetched by the POD and cached (`jwksCacheS`, default 300 s) — it must be reachable from the release namespace, not just from your laptop |

### Keys the chart refuses to render without

Four templates enforce values with `required` — an apply missing one fails
at render time (helm/PCAI apply error, before anything is created), not at
pod start:

| Key | Required when | Failure behavior |
|---|---|---|
| `ezua.virtualService.endpoint` | `ezua.enabled: true` | template error `Valid .Values.ezua.virtualService.endpoint is required !` — the VirtualService host must be the literal route host |
| `ingress.host` | `ingress.enabled: true` | template error `ingress.host is required when ingress.enabled` (checked on both the `rules[].host` and the TLS block) |
| `database.embedded.auth.existingSecret` | `database.embedded.enabled: true` | template error in BOTH `postgres.yaml` (the Postgres StatefulSet's `POSTGRES_PASSWORD` secretKeyRef) and `deployment.yaml` (the gateway's `PCAI_LLM_DB_PASSWORD` secretKeyRef) |
| `upstreamKeySecrets[].secretName` / `.secretKey` | every entry in the list | template error `upstreamKeySecrets[].secretName is required` (and the `.secretKey` twin) — each entry must name an operator-created Secret and the key inside it |
| `mcpKeySecrets[].secretName` / `.secretKey` | every entry in the list | same mechanism: `mcpKeySecrets[].secretName is required` — the by-ref handle for `mcp_servers.auth_secret` |

These are render-time guards, distinct from the pod-start failures in the
Secrets table above: a referenced Secret that does not exist yields
`CreateContainerConfigError` on the pod; a missing required *value* never
renders at all.

## Import and install — trial unit quickstart (hosted PCAI, ~15 minutes)

The numbered path for a hosted trial unit. Prereqs: you can reach the unit's
PCAI console and have a namespace to deploy into. No cluster-admin required
(the Kyverno policy stays off; the chart stamps the `hpe-ezua` labels
itself). The rest of this document is the reference detail.

### 1. Import and install

1. Import the packaged chart (`pcai-llm-gateway-<ver>.tar.gz`) into PCAI once.
2. Open the deployment's **Helm Values** editor and paste
   `helm/values-examples/values.hosted-trial.yaml` wholesale.
3. Adjust the `# SITE:` lines: `ezua.virtualService.endpoint` (keep
   `${DOMAIN_NAME}` — PCAI substitutes it before rendering; CLI deploys
   must sed-resolve it first — an unresolved literal is DENIED by istio's
   webhook and fails the release, § Gateway exposure),
   `database.embedded.persistence.storageClass`
   (leave `""` for the cluster default), and `image.repository` /
   `imagePullSecrets` if your unit pulls from a mirror.
4. Apply. First boot pulls `ghcr.io/ai-solution-eng/pcai-llm-gateway` and
   `postgres:16` — if pulls fail, set the mirror + `imagePullSecrets`.

### 2. Secrets (choose ONE path)

- **Chart-generated (recommended on trials):** set
  `adminApiKey.create: true`, `secretsMasterKey.create: true`,
  `database.embedded.auth.create: true` in the values document and re-apply.
  The chart generates the Secrets (and preserves them on every later apply).
  Fetch the admin key:
  ```bash
  kubectl -n <namespace> get secret pcai-llm-gateway-admin \
    -o jsonpath='{.data.admin-api-keys}' | base64 -d; echo
  ```
- **Operator-managed:** create the Secrets by hand before applying —
  [Prerequisites A](#a-secrets-the-operator-creates-before-install) has the
  table and the `kubectl` commands.

### 3. Smoke test

```bash
kubectl -n <namespace> rollout status deploy/pcai-llm-gateway
curl -s https://pcai-llm-gateway.<your-domain>/health   # -> {"status":"ok",...}
curl -s https://pcai-llm-gateway.<your-domain>/ready    # 200 = Postgres reachable
# In-cluster fallback if DNS hasn't propagated yet:
kubectl -n <namespace> port-forward deploy/pcai-llm-gateway 9200:9200
curl -s http://localhost:9200/ready
```

### 4. Seed the registry (tenant → user → virtual key → model pool)

The admin surface is API + console; the chat page at `/` is for key holders.

```bash
export BASE=https://pcai-llm-gateway.<your-domain>
export ADMIN_KEY=<the key from step 2>
# Full copy-paste sequence in documentation/changelogs/VERIFICATION.md §3
# (tenant, user, hllm- key with scopes ["proxy","mcp"], model pool, budget).
# Template users: scripts/seed_registry.py automates the fleet's
# model + MCP seeding (set GATEWAY_URL/ADMIN_KEY; fleet-specific).
```

Then prove the proxy:

```bash
curl -s $BASE/v1/chat/completions -H "Authorization: Bearer hllm-<key>" \
  -H 'Content-Type: application/json' \
  -d '{"model":"<registered-model>","messages":[{"role":"user","content":"ping"}]}'
```

### 5. Verify the newer surfaces after an upgrade

The upgrade path only guarantees the pod rolls; each feature wave deserves
one smoke check (full battery in [`VERIFICATION.md`](changelogs/VERIFICATION.md) §7):

- **Mint → redeem → trace waterfall**: `POST /admin/enrollments` (code shown
  once) → `POST /v1/enroll` with that code (key shown once) → chat through
  the new key with the relay header → `GET /admin/traces` shows the
  request's model-iteration + tool-call waterfall.
- **Relay header test from a plain client**: any OpenAI-shaped client that
  cannot set custom headers can still relay if the key's `relay_default` is
  on (console Keys ▸ edit) — or verify the header path with
  `-H 'X-PCAI-Execute-Tools: 1'` and watch for `X-PCAI-Relay: streaming`
  plus `data: {"pcai": ...}` events.
- **Off-peak tab**: with `offpeakEnabled: true` and the Prometheus MCP
  registered, Admin ▸ Off-peak shows live state (enabled/active/fraction/
  last sample or error); it auto-refreshes only while the tab is visible.
- **Creds dialog (per-user MCP headers)**: Admin ▸ Keys ▸ `creds` on a key
  → set e.g. `X-API-Key` for the memory MCP (a key minted in the RAG app;
  the legacy `X-Memory-Dataset` / `X-Dataset-Password` pair still works) —
  values are write-only (the dialog lists header NAMES back, never values).
- **Audit tab + metrics**: after an upgrade that adds them — Admin ▸ Audit
  lists one row per admin mutation; with `metrics.enabled: true` the
  `/metrics` endpoint serves the `gateway_*` series (§ metrics row above).
- **Rotation round-trip**: `POST /admin/keys/{id}/rotate` returns
  `grants_copied`/`creds_copied` — both counts must be non-zero for a key
  that had grants + memory creds (the rotate-carries-identity fix).

### 6. Upgrade / rollback / uninstall

Upgrade is a values edit + re-apply (full procedure, rollback and the
uninstall persistence contract: [§ Upgrading](#upgrading) below). Import a
newer chart package first if the release bumps `image.tag` — image tags
flow from the chart.

## Deployment profiles: Internal G2 vs Hosted trial

Everything above is identical for both site classes — the profile only
changes a handful of values (and the Secret posture). `helm/values-examples/`
ships a sanitized paste-ready document for each (`helm/local/` holds the
gitignored per-site copies with real credentials): pick the matching file,
paste it into the *Helm Values* editor, adjust the `# SITE:` lines, apply.

| | **Internal G2** (`values-examples/values.g2.yaml`) | **Hosted trial** (`values-examples/values.hosted-trial.yaml`) |
|---|---|---|
| `ezua.virtualService.endpoint` | Literal host in the packaged example (`pcai-llm-gateway.your-cluster.example`); the live site copy carries `pcai-llm-gateway.${DOMAIN_NAME}` — fine through the PCAI values editor, but a raw CLI `helm upgrade` must sed-resolve the placeholder FIRST: istio's webhook DENIES the literal and the release lands `failed` (§ Gateway exposure) | `pcai-llm-gateway.${DOMAIN_NAME}` — keep the placeholder; PCAI substitutes pasted values before rendering |
| `ezua.authorizationPolicy` | **ON (live 2026-10-07)** with the G2 relief knobs: `allowMetricsScrape: true` (the ANY-SOURCE `/metrics` rule), `allowInClusterApi: true` (unmeshed in-cluster consumers; `/v1/identity` + `/v1/keys/for-me` + `/v1/enroll` + `/admin` stay edge-only), `excludeInboundPorts: []` — applied in the ordered § SSO self-mint rollout. The packaged `values.g2.yaml` ships exactly this posture (2026-10-07 sync — enable stays ATOMIC with the namespace injection) | `enabled: true` (example default) — baseline ALLOW rules only (ezaf-gateway + same-namespace); no relief knobs until the SSO rollout is applied to the trial |
| Edge path classification (once the host is SSO-chained through oauth2-proxy) | Same classification for both profiles (§ SSO self-mint rollout step 3): `/`, `/ui` SSO-chained; `/v1/identity` + `/v1/keys/for-me` SSO-chained (NOT skip-auth); every other `/v1/*`, `/mcp/*` and `/admin` skip-auth — the app's own key is the control; `/health`, `/ready`, `/metrics` skip-auth or unrouted. **No blanket 302** (the MM RAG lesson) | Same table; the hosted-trial example additionally sets `ezua.routeAdmin: true` (the policy caller-gates the mesh). G2 keeps the chart default `false` — with the catch-all route still forwarding `/admin` to the same destination, so the difference is routing-declarative only |
| Secrets strategy | Operator-managed out-of-band, referenced BY NAME (the packaged example is deliberately secret-free); the live release also runs `database.embedded.enabled: true` (co-deployed PG + the `pcai-llm-gateway-pg-auth` Secret) | Chart-generated `*.create: true` recommended on trials (admin keys, Fernet master key, embedded-PG auth) — generated once, `lookup()`-preserved on every later apply |
| Metrics | `metrics.enabled: true` (the live G2 convention; the packaged example ships it `true`) — REQUIRED to pair with `allowMetricsScrape` once the policy is on, or the scrapes die | `metrics.enabled: true` + the PrometheusRule default ruleset on (dashboards are part of what a trial demonstrates) |
| Egress / proxy | HPE corporate proxy ON (`proxy.http/https` placeholders) with the cluster-local `NO_PROXY` entries kept | `proxy: {}` — direct egress; set keys only if the unit needs one |
| Autoscaling | ON (min 2 / max 4 @ CPU 70%) — maxReplicas sized against the embedded PG's `max_connections` | OFF (trial default) |
| Kyverno vendor labels | OFF — the chart stamps the `hpe-ezua` labels statically; the ClusterPolicy needs cluster-admin | OFF — same; locked-down customer clusters may not permit ClusterPolicy creation at all |

The G2-specific, live-verified execution of the SSO + policy posture (exact
values, exact commands, verified starting state, per-step verification) is
[`SSO-ROLLOUT-G2.md`](changelogs/SSO-ROLLOUT-G2.md).

## Chart values walkthrough

| Key | Why it matters | Default |
|---|---|---|
| `adminApiKey.existingSecret` / `existingSecretKey` / `create` | **Required before the pod will start** — the admin API rejects everything without a key; the pod fails loud until the Secret exists. Comma-separated keys authenticate all at once. `create: true` flips to the chart-managed mode (generated once, lookup-preserved; see the caveat above). | `pcai-llm-gateway-admin` / `admin-api-keys` / `false` |
| `secretsMasterKey.existingSecret` / `existingSecretKey` / `create` | Fernet master key for panel-stored credentials (optional but recommended). Same opt-in chart-managed mode via `create: true`; rotating the key invalidates stored credentials. | `pcai-llm-gateway-secrets` / `master-key` / `false` |
| `database.existingSecret` / `existingSecretKey` | **EXTERNAL mode only** (`database.embedded.enabled=false`, the default) — the Postgres URL (carries credentials) comes from the Secret by reference; the chart never renders a connection string. Ignored in embedded mode. | `pcai-llm-gateway-db` / `database-url` |
| `ezua.enabled` + `ezua.virtualService.endpoint` | Gateway exposure through the PCAI Istio gateway: routes `/v1`, `/mcp`, `/admin`, `/health`, `/ready`, (`/metrics` when enabled), `/ui`, `/` (chat UI). `endpoint` is **required exactly** when enabled — the literal route host (PCAI resolves `${DOMAIN_NAME}` before rendering; CLI deploys must sed-resolve it first — § Gateway exposure). | `false` (ClusterIP-only) |
| `ingress.enabled` / `className` / `host` / `tlsSecretName` | Alternative plain-k8s Ingress flavor (ADDITIVE, off by default) — for non-PCAI units with an Ingress controller. Enable only together with `ezua.enabled: false`; `host` is required when enabled; `/admin` is intentionally not routed. | `false` / `nginx` / `""` / `""` |
| `settings.*` | Runtime knobs rendered as `PCAI_LLM_*` env (complete list in the env table below): `host`, `port` (9200), `logLevel`, `upstreamTimeoutS` (900), `upstreamCaBundle` ("" — private-CA sites, § Upstream CA bundle), `retentionTraceDays`/`retentionConversationDays`/`retentionCanonicalDays` (0/0/0 — traces + conversations + distilled-only canonical-store TTL prunes; the ledger is never pruned), `endpointFailureThreshold` (5), `endpointCooldownS` (60), `maxBodyBytes` (32 MiB), `dbPoolMin`/`dbPoolMax`/`dbAcquireTimeoutS` (5/25/2), `embeddingCacheEnabled`/`MaxInputBytes`, `toolRelay*` (incl. `toolRelayGrantMode`: `explicit` default \| `scope` legacy, `toolRelayStreamWireTools`: false), `clientToolDispatch`/`TimeoutS` (false/120 — client tool-server dispatch), `routing*` (complexity-routing pseudo-models, on by default), `mcpFlatTools`/`mcpCatalogCacheS`/`mcpFailoverAttempts` (the `/mcp` flat per-tool surface), `modelFallbackMax` (2 — per-model fallback chains) + `zeroCompletionRetry` (false), `offpeak*` (all, default-off, incl. `offpeakModelPromql` per-model queries), `trustProxyIdentity` + `mcpCallerAttribution` (identity Phase A), `tokenValidation.*` (issuer/jwksUrl/audience/jwksCacheS/leewayS — Keycloak JWT validation, INERT until all three of issuer+jwksUrl+audience are set), `tokenShadowKeys`/`tokenShadowTenantId`/`tokenShadowKeyName`/`tokenShadowRateLimitRpm`/`Tpm` (lazy key provisioning for first-seen token subjects; default off), `ssoKeyCreateEnabled` (POST /v1/keys/for-me; default off), `adminGroups` (browser-identity admin path; default empty = bearer-only), `enrollMemoryDefault`/`Server`/`NameRetries` (enrollment-time memory datasets; default false / mm-rag / 3), `distiller*` (the session memory distiller: `distillerEnabled` false, memory server/tool/probe-tool, `distillerGateBackoffS` 900, `distillerRawFallback` true — full rows in the env table below). | sane defaults; rarely touched |
| `metrics.enabled` / `interval` | **ADDITIVE and OFF by default** — `false` renders NOTHING (no ServiceMonitor, no `/metrics` VirtualService route; the default render stays byte-identical to the baseline). This key also gates the app: `PCAI_LLM_METRICS_ENABLED` is rendered from it unconditionally — **chart-off = app-off**. `true` ships the prometheus-operator ServiceMonitor; the server serves `/metrics` on the same port and exports the app's own series (`gateway_requests_total{model,status,kind}`, latency/TTFT histograms, breaker/budget/rate/ACL counters — defined in `src/pcai_llm/core/metrics.py`). Requires the ServiceMonitor CRD in-cluster. | `false` / `30s` |
| `metrics.prometheusRule.enabled` / `.rules` | PrometheusRule for the gateway's OWN series — ADDITIVE and OFF by default; requires `metrics.enabled: true` AND the prometheus-operator PrometheusRule CRD. `rules: []` renders the default ruleset (GatewayHighErrorRatio, GatewayBreakerOpen, GatewayBudgetRejectionsSpike); a non-empty `rules:` list replaces it wholesale. The default ruleset is spelled out in `helm/values.yaml`'s comment block; the hosted-trial example enables it. | `false` / `[]` |
| `replicaCount` | Stateless (all state in Postgres) — replicas share the ledger with no coordination; raise freely (see [Sizing](#sizing-and-scale-signals) for when to). | `1` |
| `terminationGracePeriodSeconds` | SIGTERM grace for in-flight SSE streams to settle their ledger rows before exit (`_StreamLedger.finish`); a `preStop` sleep of 5s lets endpoint removal precede SIGTERM. Raise if your upstream streams routinely outlive the default. Counts the preStop sleep against the grace. | `120` |
| `deployment.name` / `deployment.appName` | Release and app-label identity for every rendered object (Deployment/Service/Ingress/VirtualService labels + selectors, the embedded Postgres StatefulSet name and its `wait-for-pg` init container target). Leave both at the chart default: renaming is a whole-release rename (new Service DNS, new PVC label lineage), not a tweak. | `pcai-llm-gateway` / `pcai-llm-gateway` |
| `deployment.rolloutOnUpgrade` | **Lab "redeploy in place"** (pairs with `image.pullPolicy: Always`): `true` stamps a fresh timestamp annotation on every upgrade, forcing the pods to re-roll even when nothing else changed — so re-applying the SAME values (PCA UI) re-rolls pods onto the freshly rebuilt tag, no version bump needed. `false` (default) rolls only when the pod template actually changes. Also sets `progressDeadlineSeconds: 600` while on. Production style: `IfNotPresent` + immutable tags. | `false` |
| `image.repository` / `tag` / `pullPolicy` | `ghcr.io/ai-solution-eng/pcai-llm-gateway` / chart-managed (lockstep with `Chart.yaml`). Kept in lockstep by release tooling — leave at the chart default; pinning a stale tag in a site file is how "old gateway" pods happen. | chart-managed |
| `imagePullSecrets` | Only if the GHCR package is private (public packages pull anonymously). Wired on the Deployment AND the embedded Postgres StatefulSet. | `[]` |
| `proxy.http` / `proxy.https` / `proxy.noProxy` | Corporate proxy env (`HTTP(S)_PROXY`/`NO_PROXY`, upper + lowercase) — **per-key gating: each key is wired into the pod only when non-empty**; the default `proxy: {}` renders nothing (direct egress). In-cluster peers (model endpoints, MCP servers) are covered by the `NO_PROXY` cluster-local entries — keep them (and note `172.16.0.0/12`, not /8: /8 would exempt public 172.x). HPE sites set the corporate proxy addresses in their private `helm/local/` values. | `{}` |
| `service.type` / `port` / `targetPort` | `ClusterIP` / 9200 / 9200. Don't move the port without moving the probes' target. | 9200 |
| `image.pullPolicy: Always` + `deployment.rolloutOnUpgrade: true` | the lab "redeploy in place" combo — see the `deployment.rolloutOnUpgrade` row above. | `IfNotPresent` / `false` |

Standard Kubernetes knobs — individual paths listed for explicitness; sane
hardened/sized defaults, keep unless a platform rule says otherwise:

| Key path | Default | Effect |
|---|---|---|
| `resources.requests.cpu` / `.memory` | `250m` / `512Mi` | Gateway scheduling request (the examples' trial profile drops this to `100m`/`256Mi`). See [Sizing](#sizing-and-scale-signals). |
| `resources.limits.cpu` / `.memory` | `2` / `2Gi` | Gateway container cap — the gateway streams bodies; it is not a compute node. |
| `database.embedded.resources.requests.cpu` / `.memory` | `250m` / `512Mi` | Embedded-Postgres container + its `wait-for-pg` init container (same block — LimitRange namespaces reject resource-less containers). |
| `database.embedded.resources.limits.cpu` / `.memory` | `4` / `4Gi` | Embedded-Postgres container cap. |
| `podSecurityContext.runAsNonRoot` / `.runAsUser` / `.runAsGroup` / `.fsGroup` / `.seccompProfile.type` | `true` / `10001` / `10001` / `10001` / `RuntimeDefault` | Pod-level hardening, applied to BOTH the gateway pod and the embedded Postgres pod (`fsGroup` gives the Postgres user write access to the `pgdata` volume). |
| `containerSecurityContext.readOnlyRootFilesystem` / `.allowPrivilegeEscalation` / `.capabilities.drop` | `true` / `false` / `[ALL]` | Gateway container hardening; the writable surface is an unconditional `emptyDir` at `/tmp` (no PVC — see the persistence row below). |
| `serviceAccount.create` / `.name` / `.automountServiceAccountToken`, `rbac.create` | `false` / `pcai-llm-gateway` / `false` / `false` | **All off** — the gateway makes no Kubernetes API calls (pools, MCP servers and keys live in Postgres; upstream keys are Secret *names* resolved out-of-band). `templates/rbac.yaml` renders empty; no token on a pod that will never use one. |
| `database.embedded.*` | **Embedded Postgres** (`enabled: true`): the chart co-deploys a single-node Postgres 16 (StatefulSet + Service + PVC) in the release namespace. The password comes from `auth.existingSecret` BY REFERENCE (the same Secret feeds both the Postgres StatefulSet and the gateway's `PCAI_LLM_DB_PASSWORD`) — never inlined in values — or chart-generated via `auth.create: true` (both paths fail at render time when the Secret name is missing — see "Keys the chart refuses to render without"). Sizing knobs are in the standard-knobs table above; Kyverno vendor-label sites are covered (labels included); the PG StatefulSet honors `imagePullSecrets`. | `false` |
| persistence | **Deliberately absent** — every durable state (ledger, budgets, keys, pools, MCP registry) lives in Postgres; `readOnlyRootFilesystem` needs only an unconditional `emptyDir` at `/tmp`. Adding a PVC would imply local state the ledger model explicitly avoids. | — |

Minimal values document (the PCAI flow — paste, adjust `# SITE:` lines,
apply):

```yaml
ezua:
  enabled: true                       # SITE: expose through the PCAI gateway
  virtualService:
    endpoint: pcai-llm-gateway.<your-domain>   # SITE: host on the ezaf-gateway
    istioGateway: istio-system/ezaf-gateway
    timeout: 120s
# Secrets are referenced, not created — see Prerequisites A above.
```

Full paste-ready documents: [`../helm/values-examples/README.md`](../helm/values-examples/README.md)
(`values.g2.yaml` for SE G2 with the HPE proxy, `values.hosted-trial.yaml`
with the `${DOMAIN_NAME}` placeholder PCAI resolves before rendering).

## Underlying detail: values → environment variables

`helm/values.yaml` does not template env vars (Helm cannot template inside a
values file), so `templates/deployment.yaml` renders them; you normally never
touch these directly. Every app knob in `src/pcai_llm/config.py` is
`PCAI_LLM_*`-prefixed and chart-wired:

| Env var | Rendered from |
|---|---|
| `PCAI_LLM_HOST` / `PCAI_LLM_PORT` | `settings.host` / `settings.port` |
| `PCAI_LLM_LOG_LEVEL` | `settings.logLevel` |
| `PCAI_LLM_KEEP_ALIVE_TIMEOUT_S` / `PCAI_LLM_LIMIT_CONCURRENCY` | `settings.keepAliveTimeoutS` / `settings.limitConcurrency` — uvicorn keep-alive idle timeout (default 75s, matches LB idle windows; the 5s default caused constant TLS re-handshakes for pooled consumers) and the concurrency cap (default 0 = unbounded; a cap sheds load with 503, the same fail-fast discipline as pool exhaustion) |
| `PCAI_LLM_UPSTREAM_TIMEOUT_S` | `settings.upstreamTimeoutS` (chart default 900 — timeout for a single upstream model call, chat/embeddings) |
| `PCAI_LLM_ENDPOINT_FAILURE_THRESHOLD` | `settings.endpointFailureThreshold` |
| `PCAI_LLM_ENDPOINT_COOLDOWN_S` | `settings.endpointCooldownS` |
| `PCAI_LLM_MAX_BODY_BYTES` | `settings.maxBodyBytes` |
| `PCAI_LLM_DB_POOL_MIN` / `PCAI_LLM_DB_POOL_MAX` / `PCAI_LLM_DB_ACQUIRE_TIMEOUT_S` | `settings.dbPoolMin` / `settings.dbPoolMax` / `settings.dbAcquireTimeoutS` — Postgres pool bounds (defaults 5 / 25) and bounded acquire in seconds (default 2): under saturation a request fails fast (503 `database busy`) instead of queueing forever |
| `PCAI_LLM_EMBEDDING_CACHE_ENABLED` | `settings.embeddingCacheEnabled` (default true) |
| `PCAI_LLM_EMBEDDING_CACHE_MAX_INPUT_BYTES` | `settings.embeddingCacheMaxInputBytes` (default 65536) |
| `PCAI_LLM_TOOL_RELAY_ENABLED` | `settings.toolRelayEnabled` (default true) |
| `PCAI_LLM_TOOL_RELAY_MAX_ITERATIONS` | `settings.toolRelayMaxIterations` (default 16 — one iteration is ONE model round, which may batch several parallel tool calls; the cap bounds ONE user turn's relay loop — a handback-continuation request resumes the turn's counter, a fresh user message resets it) |
| `PCAI_LLM_TOOL_RELAY_WALL_CLOCK_S` | `settings.toolRelayWallClockS` (chart default 900 — the whole-turn relay budget; the APP default in `config.py` is 600, and in code the budget is clamped to `upstreamTimeoutS − 30s` so the relay can always close its stream cleanly before the ingress shoots it down) |
| `PCAI_LLM_TOOL_RELAY_CALL_RETRIES` | `settings.toolRelayCallRetries` (default 1) — TOTAL extra attempts per relay turn for a bounded model call that expired on timeout (prefill-queueing resilience: the retry re-runs the call instead of closing the turn when the queue was about to drain). The retry spends only the REMAINING wall clock (never extends it) and only fires when ≥10s of it is left; only the timeout failure class retries — an answered call is never re-rolled. `0` = fail on the first timeout |
| `PCAI_LLM_TOOL_RELAY_GRANT_MODE` | `settings.toolRelayGrantMode` — `explicit` (default; a key sees ONLY its held (server, tool) pairs: the profile's live-grant presets ∪ its own ACL rows — the profile is the envelope, and the self-service panel lists nothing else) or `scope` (legacy, opt-in: mcp-scoped keys get the policy-admitted tools of enabled servers — incompatible with profile least-privilege when self-mint stamps the mcp scope on every key) |
| `PCAI_LLM_RELAY_STREAM_WIRE_TOOLS` | `settings.toolRelayStreamWireTools` (default **false**) — native wire visibility: re-emit the model's `tool_call` deltas as native OpenAI chunks DURING the relay loop (no `finish_reason` — that stays at the turn's true end). **Keep false for DSH**: its stream client EXECUTES any tool_call delta it sees, so the informational re-emission double-executes the call in-cluster + client-side, and for flat `<server>__<tool>` names DSH holds no registration, so the harness errors `unknown tool` and feeds the phantom back to the model while the relay's real result dies with the stream (observed 2026-09-21). In-cluster visibility survives as the `[tool] server__tool → ok (ms)` reasoning trace + `pcai` SSE events. Handback deltas for client-offered tools are emitted unconditionally and are NOT affected. Set true only for clients that render deltas without executing them |
| `PCAI_LLM_CLIENT_TOOL_DISPATCH` / `PCAI_LLM_CLIENT_TOOL_DISPATCH_TIMEOUT_S` | `settings.clientToolDispatch` (default **false**) / `settings.clientToolDispatchTimeoutS` (default 120.0) — client tool-server dispatch (ROADMAP §7 wave B2): when on, a key may REGISTER its local tools (`POST /v1/dispatch/register`, answered via the `/v1/dispatch/poll` + `/v1/dispatch/result` long-poll channel) and the relay merges `client__<tool>` into the model toolset per key, returning results INTO the running loop. Fail-safe: routes 404 while off; keys without a live offer see byte-for-byte today's handback behavior. The timeout bounds ONE dispatch — an offline/busy client produces a tool ERROR after this long, never a hang; per-tool `timeout_s` values from an offer are clamped to `[1, this]` |
| `PCAI_LLM_ROUTING_ENABLED` | `settings.routingEnabled` (default true) — master switch for the complexity-routing pseudo-models (`routing` / `routing__X`, see FEATURES.md § Routing); the per-profile `routing_policy` still gates WHO participates. `false` makes an opted-in profile behave exactly like `off` — its `X-PCAI-Route` header reads `off` so the state stays visible |
| `PCAI_LLM_ROUTING_AGENTS_ENABLED` | `settings.routingAgentsEnabled` (default **false**) — agents as routing candidates: the calling key's OFFERED agents join the router's candidate window (an agent is a hard model: tool surface + pinned model/cascade), `routing__<agent>` is a legal ceiling, and a pick applies the agent's surface + prompt for the whole turn (routing runs before the request is prepared). Agents are PER-KEY (the profile's agent menu), so they are not in the global routing allowlist; they join at the cheap end (undeclared capability). Off = the router's model/group ladder is byte-identical |
| `PCAI_LLM_ROUTING_TIER1_TOKENS` / `PCAI_LLM_ROUTING_TIER2_TOKENS` | `settings.routingTier1Tokens` / `settings.routingTier2Tokens` (defaults 1500 / 6000) — the two cut points mapping the request's complexity score onto the profile's cheap-first ladder: below tier 1 serves the floor (`routing_min_model`), below tier 2 the middle of the floor..ceiling window, at/above tier 2 the ceiling. The score is prompt text (chars/4) + a flat per-image charge + a per-turn adder + capped `max_tokens`; tool definitions are deliberately NOT scored (an agent harness ships the same catalog every turn, so they are a constant, not a signal). Tune per fleet from the shadow-ledger data |
| `PCAI_LLM_ROUTING_TURN_ADDER_TOKENS` | `settings.routingTurnAdderTokens` (default 100) — score adder per message beyond the first (conversation state demands capability even when the latest turn is short) |
| `PCAI_LLM_MCP_FLAT_TOOLS` | `settings.mcpFlatTools` (default true) — `/mcp` tools/list re-exposes the calling key's GRANTED upstream tools as individual `{server}__{tool}` MCP tools (upstream inputSchemas + annotations, ACL-filtered per request; the gateway tools `mcp_catalog`/`mcp_call` stay listed first). `0` = kill switch back to the exact pre-flat two-tool surface |
| `PCAI_LLM_REGISTRY_WATCH_S` | `settings.registryWatchS` (default 5) — cross-replica registry convergence: each replica polls `(count(*), max(updated_at))` over `mcp_servers` + `model_endpoints` on this cadence and refreshes its in-memory snapshot only when the watermark moved (the stamps are maintained by a DB trigger, so out-of-band SQL edits converge too). `0` disables the poll — correct ONLY on a single replica; with the HPA at 2+ pods, disabling it restores the "registered on one pod, 404 on the other" bug |
| `PCAI_LLM_REGISTRY_ADMIN_FRESH_S` | `settings.registryAdminFreshS` (default 1) — the admin console's `GET /admin/mcp-servers` and `GET /admin/mcp-servers/{name}/tools` (the "test" button) re-check the watermark on read, at most once per this interval; fail-open (a watermark failure keeps the current snapshot and never 500s the read). `0` disables the fast path, leaving the console on the watcher cadence |
| `PCAI_LLM_MCP_CATALOG_CACHE_S` | `settings.mcpCatalogCacheS` (default 60) — TTL seconds for the per-server upstream tool-listing cache (key-independent payload, server-level auth only, invalidated on every registry mutation, stale listing served on upstream error) |
| `PCAI_LLM_MCP_FAILOVER_ATTEMPTS` | `settings.mcpFailoverAttempts` (default 2) — retries AFTER the primary when a FLATTENED `{server}__{tool}` call hits a transport failure (next priority-ordered enabled server with an explicit key grant + policy-admitted tool in its cached listing); explicit `mcp_call` targets never fail over |
| `PCAI_LLM_MCP_ARG_POLICY` | `settings.mcpArgPolicy` (default **enforce**) — argument-level destination policy, the P0-1 stage that runs on BOTH MCP paths (`/mcp`'s gate chain and the RELAY executor, i.e. the tool calls a model makes during a chat turn). `enforce` \| `warn` \| `off`; an unrecognised value fails CLOSED to `enforce`. It blocks *where* a tool may send a request, not *what* it sends — exfiltration by URL payload survives this stage by design |
| `PCAI_LLM_MCP_ARG_POLICY_EXEMPT` | comma-separated `server.tool` globs exempt from the argument stage (empty = none). Prefer an exemption over turning the stage off |
| `PCAI_LLM_MCP_ARG_EGRESS_SERVERS` | comma-separated egress-class server globs (empty = the federation egress class, `searxng`). Only these deny cluster-internal names and resolve hostnames |
| `PCAI_LLM_MCP_ARG_DENY_CLUSTER_HOSTS` | default `true` — deny `.svc` / `.svc.cluster.local` / `.cluster.local` targets on egress-class servers (a web fetcher has no business reading the cluster control plane) |
| `PCAI_LLM_MCP_ARG_POLICY_RESOLVE` | default `true` — resolve egress-class hostnames (2 s bound) and deny a name that maps into a denied range. A resolution FAILURE allows (a resolver that cannot answer says nothing about the name) |
| `PCAI_LLM_MCP_ARG_TELEMETRY` | default `true` — observe base64-blob / long-query / long-path argument shapes (`gateway_mcp_arg_telemetry_total`). Metric only, never refuses |
| `PCAI_LLM_MODEL_FALLBACK_MAX` | `settings.modelFallbackMax` (default 2) — caps how many fallback MODELS are tried per request: a model's admin-curated fallback chain (managed via `GET/PUT /admin/models/{name}/fallbacks`, tried FIRST) plus, for ALIAS requests (`auto`), the profile ordering's tail (appended AFTER the chain). The walk engages when the serving model cannot serve (`pool.pick()==None`) or answers with an upstream 5xx / transport error; every hop re-runs the budget gate — a fallback is never a budget bypass. `0` = runtime model failover is never walked (kill switch). See FEATURES.md § Router extras |
| `PCAI_LLM_MODEL_GROUPS_ENABLED` | `settings.modelGroupsEnabled` (default **false**) — master switch for model groups: named sets of equivalent chat models whose member is chosen by LIVE LOAD (power-of-two over queue depth) instead of a fixed order (FEATURES.md § Model groups). Off = a group name is not routable and routing is byte-identical; the admin API still stores and curates the rows either way |
| `PCAI_LLM_MODEL_GROUP_LOAD_REFRESH_S` | `settings.modelGroupLoadRefreshS` (default 5) — how often the BACKGROUND load snapshot refreshes from Prometheus (running / waiting / kv_cache_pct per endpoint). Prometheus dependency note: the snapshot is a background task and the pick reads the in-memory view — Prometheus is NEVER queried on the request path. Needs a reachable Prometheus, same dependency class as the off-peak evaluator |
| `PCAI_LLM_MODEL_GROUP_LOAD_STALE_S` | `settings.modelGroupLoadStaleS` (default 30) — a snapshot older than this is treated as "no data": member selection degrades to random rather than routing on stale load. Fail-open by design — a Prometheus outage must never stop model selection |
| `PCAI_LLM_ZERO_COMPLETION_RETRY` | `settings.zeroCompletionRetry` (default **false**) — zero-completion insurance: when a chat call answers 2xx with EMPTY content, zero completion tokens and `finish_reason=stop`, retry the identical call EXACTLY once (both attempts settle under the shared `request_id` — the first was real GPU spend). Non-streaming JSON path + the relay's final answer only; plain streaming is never buffered/re-rolled. Flip on after observing the empty-completion rate in the ledger |
| `PCAI_LLM_OFFPEAK_ENABLED` | `settings.offpeakEnabled` (default **false**) — demand-shaped billing: when avg GPU util (PromQL below) is under `ENTER_PCT`, new requests settle with `billing_fraction` = the key's fraction; budget gates consume `tokens × fraction` / `USD × fraction` (ledger keeps true usage). Needs a Prometheus reachable through the registered MCP server named `OFFPEAK_MCP_SERVER` — enable ONLY where that MCP is deployed. Fail-safe: any evaluator failure bills normal. Console: Admin ▸ Off-peak shows live state |
| `PCAI_LLM_OFFPEAK_CHECK_S` | `settings.offpeakCheckS` (default 30) — evaluator cadence seconds |
| `PCAI_LLM_OFFPEAK_ENTER_PCT` / `PCAI_LLM_OFFPEAK_EXIT_PCT` | `settings.offpeakEnterPct` / `settings.offpeakExitPct` (defaults 20 / 35) — hysteresis thresholds: enter the low-demand state below enter, exit only above exit, so billing never flaps at the boundary |
| `PCAI_LLM_OFFPEAK_PROMQL` | `settings.offpeakPromql` (default `avg(avg_over_time(DCGM_FI_DEV_GPU_UTIL[5m]))` — DCGM exporter; adjust per cluster: vLLM-only sites may prefer a queue-depth metric) — the GLOBAL demand query, used by every model without a per-model entry |
| `PCAI_LLM_OFFPEAK_FRACTION` | `settings.offpeakFraction` (default 0.5 — budgets last 2× during off-peak). PER-PROFILE override: set `offpeak_fraction` on a profile (Admin ▸ Profiles — console-editable, live immediately); keys under that profile bill at its fraction during low-demand windows, all others use this default |
| `PCAI_LLM_OFFPEAK_MCP_SERVER` / `PCAI_LLM_OFFPEAK_TOOL` | `settings.offpeakMcpServer` / `settings.offpeakTool` (defaults `prometheus` / `prom_query`) — which registered MCP server + tool serves the query |
| `PCAI_LLM_OFFPEAK_MODEL_PROMQL` | `settings.offpeakModelPromql` (default `{}`) — per-model off-peak queries, JSON object: model name → PromQL returning a 0-100 load percentage for THE RESOURCES THAT MODEL SERVES ON. A model with an entry bills by its OWN hysteresis state; models without one fall back to the global `offpeakPromql`. WHY: the global GPU query averages EVERY GPU in the cluster — idle nodes, training jobs and other tenants dilute a busy model's utilization below the enter threshold while its own GPUs are pegged. DCGM series carry NO pod label — scope by `Hostname` + device range (or `exported_namespace`), and validate the query in Prometheus FIRST (a scoped query matching zero series fails safe to FULL price). Keys must match registered model names EXACTLY. Omitted from the render entirely when unset (default render byte-identical). See FEATURES.md § Off-peak billing |
| `PCAI_LLM_ENDPOINT_METRICS_DISK_SELECTOR` | `settings.endpointMetricsDiskSelector` (default `""`) — the Off-peak tab's all-models cache summary DISK tier: a PromQL label selector over `kubelet_volume_stats_*` naming the volumes backing the KV cache's L3 store (the inference engine exports no storage-occupancy gauge). Two extra Prometheus queries per snapshot ONLY when set; unset/empty/unknown reads null, never 0%. G2 example: `'namespace="mooncake",persistentvolumeclaim=~"ssd-mooncake-store-.*"'` (Mooncake SSD offload, 3 × 350 Gi) |
| `PCAI_LLM_URL_DENY_CIDRS` | extra SSRF-deny CIDRs for admin-registered upstream URLs, JSON array env (e.g. `'["10.0.0.0/8"]'`). Loopback + link-local/metadata (169.254.169.254) are ALWAYS denied; RFC1918 and `*.svc.cluster.local` stay ALLOWED by default — in-cluster upstreams ARE private addresses here. Not rendered by the chart (app default `[]`); set via env only on stricter sites |
| `PCAI_LLM_METRICS_ENABLED` | `metrics.enabled` — always rendered (chart-off = app-off; this row appears once — the app env and the ServiceMonitor gate are the same key) |
| `PCAI_LLM_TOKEN_ISSUER` / `PCAI_LLM_TOKEN_JWKS_URL` / `PCAI_LLM_TOKEN_AUDIENCE` | `settings.tokenValidation.issuer` / `.jwksUrl` / `.audience` (all default `""`) — identity Phase B: validate Keycloak-issued JWTs on the `Authorization: Bearer` header via the IdP's JWKS endpoint. **ALL THREE must be set** for the token rung to engage; empty (the default) leaves it INERT — every request resolves exactly as before. An invalid/expired token falls through the ladder when a virtual key is also presented (the ~30-min token TTL stays invisible to headless key holders); it 401s only when the token was the sole credential. The gateway only VALIDATES tokens — it never issues, refreshes, or stores them |
| `PCAI_LLM_TOKEN_JWKS_CACHE_S` / `PCAI_LLM_TOKEN_LEEWAY_S` | `settings.tokenValidation.jwksCacheS` (default 300.0) / `.leewayS` (default 5.0) — TTL seconds for the cached JWKS signing keys, and clock leeway seconds for JWT exp/nbf checks |
| `PCAI_LLM_TOKEN_SHADOW_KEYS` / `PCAI_LLM_TOKEN_SHADOW_TENANT_ID` / `PCAI_LLM_TOKEN_SHADOW_KEY_NAME` | `settings.tokenShadowKeys` (default **false**) / `.tokenShadowTenantId` (default `""`) / `.tokenShadowKeyName` (default `token-shadow`) — token shadow-keys: a first-seen VALID token subject lazily gets a user + ledger key (`bound_via='token-shadow'`, scopes proxy+mcp) so usage attribution keeps its `key_id` anchor. Requires `tokenShadowTenantId` — without it the flag is inert. Off = tokens validate but never provision (anonymous-fp posture) |
| `PCAI_LLM_TOKEN_SHADOW_RATE_LIMIT_RPM` / `PCAI_LLM_TOKEN_SHADOW_RATE_LIMIT_TPM` | `settings.tokenShadowRateLimitRpm` / `.tokenShadowRateLimitTpm` (defaults 60 / 100000) — the rate limits stamped on lazily-provisioned shadow keys (config-sourced; the console can tighten them per key afterwards, exactly like enrollment-created keys) |
| `PCAI_LLM_SSO_KEY_CREATE_ENABLED` | `settings.ssoKeyCreateEnabled` (default **false**) — `POST /v1/keys/for-me`: a trusted browser identity may mint a key bound to its OWN subject (`sso-create`). Requires `trustProxyIdentity=true`; 403 otherwise. Scopes/limits are fixed (proxy+mcp, 60 rpm / 100k tpm); grants stay admin-controlled. The tenant's SSO profiles (default + admin tier, `sso_default_profile_id` / `sso_admin_profile_id` / `sso_admin_groups`) are tenant-scoped DB state set via `PUT /admin/tenants/{id}/sso-default-profile` — NO env vars |
| `PCAI_LLM_TRUST_PROXY_IDENTITY` | `settings.trustProxyIdentity` (default **false**) — trust the oauth2-proxy identity headers (`X-Auth-Request-User` / `X-Email` / `X-Auth-Request-Groups`) on the gateway's browser identity rung. The headers are client-spoofable at the pod — set this ONLY where an enforcing proxy overwrites them on every request AND an Istio AuthorizationPolicy keeps unauthenticated callers from bypassing the proxy (pcai-helm-lib `_authorizationpolicy.tpl`) |
| `PCAI_LLM_TRUST_FORWARDED_GROUPS` | `settings.trustForwardedGroups` (default **false**) — consult `X-Forwarded-Groups` for browser-rung groups. Fail closed: the platform oauth2-proxy does NOT inject that header (see the Platform oauth2-proxy facts below), so an arriving value is client-supplied — a groupless proxy identity could otherwise self-grant `PCAI_LLM_ADMIN_GROUPS` by adding it (audit r5 #4, 2026-10-30). Enable ONLY for chains whose proxy injects and overwrites the forwarded header on every request |
| `PCAI_LLM_MCP_CALLER_ATTRIBUTION` | `settings.mcpCallerAttribution` (default true) — stamp `X-MCP-Caller` (`<subject or key-fp>@gateway`) plus, when a real subject resolved, the companions `X-MCP-Caller-Subject` / `X-MCP-Caller-Class` on every upstream fleet MCP call (attribution-never-authorization; upstreams may record it in their audit chain, never authorize off it). All three names are RESERVED — tenants cannot supply them via per-key MCP credential overrides (400) and the gateway stamp overwrites any stale value after the last-wins merge |
| `PCAI_LLM_MCP_SEPARATE_READ_EGRESS` | `settings.mcpSeparateReadEgress` (default `warn`) — read+egress separation (P0-3): flag or refuse a key whose grant set holds BOTH a read-class server (k8s-ops, logsearch, mm-rag, prometheus, seaborn, sqlhandler read tools) and an egress-class server (searxng) — the read-data-then-send-it combination. Server classes come from ONE constant, `mcpf/federation.py::MCP_SERVER_CLASSES`. **`warn` (default):** the call PROCEEDS; ticks metric `gateway_mcp_policy_blocked_total{reason="read+egress-warn"}` (the `-warn` suffix is deliberate — the series name says *blocked* and warn does not block) + one WARNING naming the key id and the offending (read, egress) pair. **`enforce`:** ticks `{reason="read+egress"}` and refuses EVERY call from a both-classes key (the whole grant set is the violation — the key is mis-scoped, not the individual call), so the rollout order is: run `warn`, review the flagged pairs, RE-SCOPE/split the keys (preset stamps via POST /admin/profiles `mcp_grant_preset`, or per-key PUT /admin/keys/{id}/mcp-grants) until warn is silent, THEN flip to `enforce`. `off` disables. The admin-tool hard stop (sqlhandler `admin_*` refused on the MCP surface unless the key carries `mcp_admin=TRUE`, default FALSE) is NOT behind this knob — it is unconditional |
| `PCAI_LLM_ADMIN_GROUPS` | `settings.adminGroups` (default `""`) — admin gate second path: a trusted browser identity in ANY of these groups (comma-separated) may call the admin API. Empty = the group path is off; the bearer admin-key path is unchanged and always works. Same deployment precondition as `trustProxyIdentity` |
| `PCAI_LLM_ENROLL_MEMORY_DEFAULT` / `PCAI_LLM_ENROLL_MEMORY_SERVER` / `PCAI_LLM_ENROLL_MEMORY_NAME_RETRIES` *(see [MEMORY.md](MEMORY.md) for the end-to-end configuration walkthrough)* | `settings.enrollMemoryDefault` (default **false**) / `.enrollMemoryServer` (default `mm-rag`) / `.enrollMemoryNameRetries` (default 3) — enrollment-time per-user memory datasets (FEATURES.md § Enrollment-time memory datasets): when a redeemer opts in (`POST /v1/enroll` `memory_opt_in`, or a profile `"memory": {"provision": true}` preset), the gateway creates a private RAG dataset `mem-<tenant>-<user>-<key6>` and stamps `X-Memory-Dataset` / `X-Dataset-Password` onto the key's per-server credential row. The server name resolves through the `mcp_servers` REGISTRY (its URL is admin-registered + SSRF-guarded — no free-form RAG URL env); the row must be registered and enabled for provisioning to work. Provisioning failure never fails a redemption; requires `PCAI_LLM_SECRETS_MASTER_KEY` (without it nothing is created and the response says so) |
| `PCAI_LLM_RETENTION_TRACE_DAYS` / `PCAI_LLM_RETENTION_CONVERSATION_DAYS` | `settings.retentionTraceDays` / `.retentionConversationDays` (defaults 0/0 = disabled, the same opt-in contract) — TTL prunes for the OPERATIONAL tables (relay-step traces / saved chat transcripts). The usage LEDGER is never pruned — it is the audit/budget artifact |
| `PCAI_LLM_RETENTION_CANONICAL_DAYS` | `settings.retentionCanonicalDays` (default 0 = disabled) — TTL prune for `canonical_conversations`, the distiller's INPUT store (full message history as JSONB; the biggest table on a busy gateway, pruned by nothing else). **DISTILLED-ONLY**: a row is deleted only once a `canonical_distills` marker proves a memory was written for it, so un-distilled conversations are never lost. 0 = keep forever, same opt-in contract as the two knobs above |
| `PCAI_LLM_DISTILLER_ENABLED` | `settings.distillerEnabled` (default **false**) — master switch for the session memory distiller surface (§ FEATURES.md "Session memory distiller"). `false` renders no distiller attachment and `/v1/distill` answers inert no-ops |
| `PCAI_LLM_DISTILLER_MEMORY_SERVER` / `PCAI_LLM_DISTILLER_MEMORY_TOOL` / `PCAI_LLM_DISTILLER_MEMORY_PROBE_TOOL` | `settings.distillerMemoryServer` / `.distillerMemoryTool` / `.distillerMemoryProbeTool` (defaults `mm-rag` / `add_memory` / `memory_status`) — which registered MCP server writes memories for the distiller, which tool writes them, and which capability probe gates attachment (any backend with an identity-routed write tool integrates, not just mm-rag) |
| `PCAI_LLM_DISTILLER_GATE_BACKOFF_S` | `settings.distillerGateBackoffS` (default 900; 0 = retry every pass, not recommended) — skip backoff (2026-10-09 stall fix): a row the engine skips (memory gate down, transcript collapsed) leaves the oldest-first due window for this long before retry, so a broken dependency can no longer pin the window and starve newer rows. NOT a distilled marker: the row returns to the due set when the window lapses |
| `PCAI_LLM_DISTILLER_RAW_FALLBACK` | `settings.distillerRawFallback` (default **true**) — raw-transcript fallback when a distill FAILS after the gates passed. `true` = the historical DSH contract: write the raw transcript as `session_history` so a session is never lost to a model error (caveat: mm-rag CHUNKS a raw transcript — measured 126 chunks from one session — which dilutes recall). `false` = leave the row due and retry next sweep; the conversation is still in `canonical_conversations`, so nothing is lost and the memory store only ever receives distilled summaries. The shipped G2 example sets it `false` |
| `PCAI_LLM_DB_HOST` / `PCAI_LLM_DB_PORT` / `PCAI_LLM_DB_USER` / `PCAI_LLM_DB_NAME` | **embedded mode only** — derived from `database.embedded.serviceName` + `auth.username`/`auth.database` |
| `PCAI_LLM_DB_PASSWORD` | **embedded mode only** — `database.embedded.auth.existingSecret{,Key}` (secretKeyRef; the same Secret the Postgres StatefulSet uses) |
| `PCAI_LLM_UPSTREAM_KEY__<SUFFIX>` | from `upstreamKeySecrets[]` — the by-reference env handle for `model_endpoints.api_key_secret` |
| `PCAI_LLM_MCP_KEY__<SUFFIX>` | from `mcpKeySecrets[]` — the by-reference env handle for `mcp_servers.auth_secret` |
| `PCAI_LLM_ADMIN_API_KEYS` | `adminApiKey.existingSecret{,Key}` — always from the referenced Secret (operator-created, or chart-generated with `adminApiKey.create: true`) |
| `PCAI_LLM_SECRETS_MASTER_KEY` | `secretsMasterKey.existingSecret{,Key}` — always from the referenced Secret (operator-created, or chart-generated with `secretsMasterKey.create: true`) |
| `PCAI_LLM_DATABASE_URL` | `database.existingSecret{,Key}` — **external mode only**; from the referenced Secret |
| `HTTP_PROXY`/`HTTPS_PROXY`/`NO_PROXY` (+ lowercase) | `proxy.http`/`proxy.https`/`proxy.noProxy`, each wired **only when non-empty** (`proxy: {}` default = none) |

> **Migrating from `hpe_proxies` (removed in chart 1.7.2).** The old boolean
> gate `hpe_proxies` is gone: charts now take a generic per-key `proxy:` dict
> and every key is active when non-empty. Sites that ran `hpe_proxies: true`
> already had a `proxy` block — keep it as-is, just delete the `hpe_proxies`
> line (a site that relied on the old default values must now set the proxy
> block explicitly). Direct-egress sites drop nothing: `proxy: {}` is the
> default and renders no proxy env. The `NO_PROXY` cluster-local entries
> guidance is unchanged.

**Long-turn fleets (agent workloads).** Three timeout knobs are COUPLED and
must be raised together: `settings.upstreamTimeoutS` (the per-model-call
cap — every relay model call is bounded by it), `settings.toolRelayWallClockS`
(the whole-turn relay budget, clamped to `upstreamTimeoutS − 30s` so the
relay can always close its stream cleanly before the ingress shoots it
down) and `ezua.virtualService.timeout` (the hard ingress deadline — it
must exceed the wall clock). Ordering: per-call cap ≤ wall clock < VS
deadline. A turn that dies as `(model error: relay model call timed out)`
had ONE model call exceed its budget — under parallel agent fan-out the
usual trigger is prefill queueing on the model endpoint, not a wedged
upstream; raise all three together (e.g. 900 / 900 / 960) rather than one.
The keepalive + clean-close machinery guarantees such failures are always
VISIBLE to the client (guard event + error chunk + `[DONE]`), never a
silent 504.
### Settings knobs added in the 4.0/4.1 waves (one line each)

The knobs below ship in `helm/values.yaml` under `settings.*` (env
`PCAI_LLM_*` — same render rule as the table above) but predate their
FEATURES.md coverage; the design rationale for each lives in
[FEATURES.md](FEATURES.md). Helm-only availability knobs follow the table.

| Values key (env) | Default | Why |
|---|---|---|
| `settings.responseCacheMode` / `responseCacheTtlS` (`PCAI_LLM_RESPONSE_CACHE_MODE` / `_TTL_S`) | `off` / 3600 | Exact-match response cache for non-streaming chat (`off \| shadow \| active`); `shadow` measures the hit rate without serving — run shadow first, then flip `active` (FEATURES.md § Response cache) |
| `settings.toolCompressDefault` / `MinChars` / `RatioFloor` / `StoreTtlS` / `MaxStoreChars` (`PCAI_LLM_TOOL_COMPRESS_*`) | off / 4096 / 0.15 / 86400 / 1000000 | Relay tool-result compression defaults (the per-request `X-PCAI-Compress-Tools` header overrides; FEATURES.md § Tool-use relay) |
| `settings.toolRelaySurface` (`PCAI_LLM_TOOL_RELAY_SURFACE`) | `hybrid` | Relay tool surface: `hybrid` offers a compact discover-then-call catalog (~4.6-6k tokens for the whole fleet); `full` re-offers every definition — only for a client that cannot discover-then-call |
| `settings.toolRelayRenderDeltas` / `toolRelayRenderIndexBase` (`PCAI_LLM_TOOL_RELAY_RENDER_DELTAS` / `_INDEX_BASE`) | false / 1000 | Re-emit each in-cluster tool call as a NATIVE `tool_call` delta stamped `pcai.relay: true`; requires request header `X-PCAI-Relay-Deltas: 1` — the stamp blocks client execution (unlike `toolRelayStreamWireTools`, safe to enable) |
| `settings.stickyRouting` / `stickyRoutingIdleResetS` (`PCAI_LLM_STICKY_ROUTING` / `_IDLE_RESET_S`) | false / 1800 | Conversation stickiness for the ROUTER (never-downgrade): a routed conversation holds a capability FLOOR, may escalate, never falls below; idle reset re-decides fresh (0 = never) |
| `settings.sessionAffinityEnabled` / `sessionAffinityTtlS` (`PCAI_LLM_SESSION_AFFINITY_ENABLED` / `_TTL_S`) | false / 3600 | Endpoint stickiness (KV-cache reuse): a pin to the serving endpoint survives the turn — routing STATE, never policy; a pin that no longer qualifies is discarded on read |
| `settings.rateLimitExcludeCachedTokens` / `rateLimitPredictCached` / `rateLimitPredictReconcile` (`PCAI_LLM_RATE_LIMIT_*`) | true / false / true | TPM accounting for prompt-cache hits: exclude cached tokens from the window, optionally PREDICT admission from the key's own cached-token history, and settle the predicted-vs-actual difference afterward (prediction decides WHEN, never HOW MANY) |
| `settings.budgetCachedExemptFraction` (`PCAI_LLM_BUDGET_CACHED_EXEMPT_FRACTION`) | 0.9 | Discount cached tokens against day/week/month TOKEN budgets (1.0 = fully exempt, 0.0 = legacy full count); USD budgets unaffected (cache is already priced at `cached_per_m`) |
| `settings.failoverTtftS` (`PCAI_LLM_FAILOVER_TTFT_S`) | 45 | TTFT failover budget: after a 2xx, the stream waits this long for the upstream's FIRST SSE data line (keepalives do not count); 0 disables — a wedged-but-200 upstream no longer holds the turn |
| `settings.healthProbe.enabled` / `intervalS` / `timeoutS` / `failuresToTrip` (`PCAI_LLM_HEALTH_PROBE_*`) | false / 30 / 5 / 2 | Background proactive probe (`GET {base_url}/models` with the endpoint's OWN credential) that trips the breaker between requests; `Pool.healthy()` ignores a probe verdict that would empty a pool — degraded routing, never an outage |
| `settings.effectivePromptCapture` / `effectivePromptRetainDays` (`PCAI_LLM_EFFECTIVE_PROMPT_CAPTURE` / `_RETAIN_S`) | true / 7 | Write a ≤64 KB preview of every REAL model call to `effective_prompts` (transparency/debugging; `GET /admin/effective-prompts`) |
| `settings.maxTokensCeiling` (`PCAI_LLM_MAX_TOKENS_CEILING`, JSON object) | `{}` | Per-model `max_tokens` ceiling — silently LOWERS a client's larger value for that virtual model (never raises); the static complement of live context-window discovery (FEATURES.md § Context-window discovery) |
| `settings.demandAwareExcludedModels` (`PCAI_LLM_DEMAND_AWARE_EXCLUDED_MODELS`) | `[]` | Models the conversation-start head must never pick (they stay on the plain `auto` ladder and the failover tail) |
| `settings.routeDefaultModel` (`PCAI_LLM_ROUTE_DEFAULT_MODEL`) | false | Promote the legacy `auto` alias INTO routing — off by default so arming routing never silently changes what an existing client request does |
| `settings.audioTtsCharsPerToken` / `audioAsrTokensPerSecond` (`PCAI_LLM_AUDIO_TTS_CHARS_PER_TOKEN` / `_ASR_TOKENS_PER_SECOND`) | 4 / 3.0 | Audio billing estimates (TTS/ASR report no token usage; a zero-token row would be a budget bypass) — superseded per model by unit-aware pricing (`model_costs.unit`) |
| `settings.distillerMaxTokens` (`PCAI_LLM_DISTILLER_MAX_TOKENS`) | 4096 | Distill OUTPUT cap — an overrun is rejected whole by `document_is_complete()`, so the memory write is lost rather than shortened |
| `settings.distillerDisableThinking` (`PCAI_LLM_DISTILLER_DISABLE_THINKING`) | true | Send `chat_template_kwargs {"enable_thinking": false}` to the summarizer (engines without the kwarg ignore it) |
| `settings.canonicalSkipTitleRequests` (`PCAI_LLM_CANONICAL_SKIP_TITLE_REQUESTS`) | true | Never capture a title-generation request as a canonical conversation (it would distill a bogus "session summary" of a title string) |
| `settings.mcpSelfService` (`PCAI_LLM_MCP_SELF_SERVICE`) | true | Keyed users manage their OWN key's per-server MCP credential overrides and toggle grants within their entitlement catalog (`/v1/mcp/creds`, `/v1/mcp/selfservice`); false hides the routes |
| `settings.otelEnabled` / `otelEndpoint` / `otelHeaders` / `otelSampleRatio` / `otelServiceName` (`PCAI_LLM_OTEL_*`) | false / `http://otel-collector:4318` / "" / 1.0 / `pcai-llm-gateway` | OTLP/Langfuse spans (FEATURES.md § OTLP / Langfuse spans); `otelHeaders` is comma-separated `Key=Value` pairs for exporter auth; spans carry metadata only — never prompt/completion content |
| `database.maxConnectionsBudget` (helm-only — consumed by `templates/NOTES.txt`, not an env) | 0 | The real external-Postgres `max_connections`: makes the post-install connection-budget arithmetic (`autoscaling.maxReplicas × settings.dbPoolMax`) print a WARNING instead of an informational NOTE; embedded mode uses `database.embedded.maxConnections` instead |
| `podDisruptionBudget.enabled` / `minAvailable` / `maxUnavailable` (helm-only) | false / — / 1 | PDB so a node drain cannot take every replica at once; the template REFUSES `enabled` with a single replica (a PDB there blocks every drain forever) |
| `topologySpreadConstraints` / `affinity` (helm-only passthrough) | `[]` / `{}` | Standard scheduling spread/anti-affinity for multi-replica posture (commented example in `helm/values.yaml`) |
| `autoscaling.targetCPUUtilizationPercentage` (helm-only) | 70 | The HPA's CPU target (see § Sizing for why CPU and not memory) |
| `networkPolicy.probeCidrs` / `probeExceptCidrs` / `ingressGatewayNamespace` (helm-only) | `[]` / `[]` / `istio-system` | NetworkPolicy rule (iii) node-CIDR allowance for kubelet probes — `0.0.0.0/0` in `probeCidrs` FAILS the render unless `probeExceptCidrs` carves the pod CIDR out |

## Postgres

**External by default**: PCAI provides/hosts Postgres — point
`database.existingSecret` at a database the gateway may create tables in.
On startup the gateway connects and **auto-creates its schema** (all tables
listed in [`FEATURES.md`](FEATURES.md) § Ledger schema). It needs CREATE
privileges on the target database; existing tables are left alone
(`CREATE TABLE IF NOT EXISTS` + guarded idempotent migrations). If Postgres
is unreachable, `/ready` fails and the pod leaves the Service — fail
closed, never serve keyless.

**Embedded vs external — the trade-off:**

| | Embedded (`database.embedded.enabled: true`) | External (default) |
|---|---|---|
| Dependencies | none — one install, one namespace, unit is self-contained | needs a site/PCAI Postgres + a database the gateway may CREATE TABLEs in |
| Durability | PVC in the release namespace (5 Gi default); survives upgrades, dies only if the PVC is deleted | site DB's own backup/HA story |
| Scale | single-node — fine for trial/small-team load; watch its resources if the ledger grows | scale Postgres with site tooling; gateway replicas attach freely |
| Ops surface | one more StatefulSet to babysit (pulls `postgres:16` — mirror if Docker Hub is unreachable) | the gateway is the only thing in the namespace |
| Secrets | `pcai-llm-gateway-pg-auth` (or `auth.create: true`) | `pcai-llm-gateway-db` with a full URL |

Rule of thumb: no site DB → embedded; a site DB exists and is managed →
external. Switching modes later is a values change + re-apply, but the
ledger does not migrate itself — move data at the Postgres level before
flipping.

## Gateway exposure (ezua / Istio)

When `ezua.enabled=true` the chart renders one VirtualService on
`istio-system/ezaf-gateway` routing `/v1`, `/mcp`, `/admin`, `/health`, `/ui`, `/`,
`/ready` and — when `metrics.enabled=true` — `/metrics` to the service. `/v1` and `/mcp` require a **virtual
key** at the pod (`Authorization: Bearer hllm-...`); `/admin` requires an
**admin key** and mutates the control plane — protect the endpoint host at
the platform level (SSO/bearer at the ezaf-gateway) before exposing it.
`/team_leader` (the enrollment-scoped team console, page + API) has no
explicit route: it rides the catch-all `/` route, and the **team-leader
key** — a hash-only credential stored on the enrollment row — is its
authorization. Classify it with `/admin` when you decide which paths the
edge chain skips.
By default this chart ships no policy templates; the optional
`networkPolicy.*` / `ezua.authorizationPolicy.*` flags below (§ Network
isolation) render the in-chart equivalents where the platform does not
provide them. PCAI envsubsts
`${DOMAIN_NAME}` in pasted values before rendering — keep the placeholder in
pasted values; plain `helm -f` sites must resolve it FIRST. ⚠️ Live-verified
2026-10-07 (G2): a raw `helm upgrade -f values.g2.yaml` passes the literal
`${DOMAIN_NAME}` through, and istio's validation webhook DENIES the
VirtualService patch (`domain name "${DOMAIN_NAME}" invalid`) — leaving the
release `failed` while everything-before-the-VS (the AuthorizationPolicy!)
still applied. CLI deploys: generate the resolved copy once per session and
upgrade from it:

```bash
sed 's/\${DOMAIN_NAME}/pcai-llm-gateway.<your-domain>/' helm/local/values.g2.yaml \
  > .scratch/values.g2.resolved.yaml
helm upgrade pcai-llm-gateway helm -n <namespace> -f .scratch/values.g2.resolved.yaml
```

(The istio webhook that rejected the bad hostname is also what PROTECTED the
live VirtualService — the garbage value never reached the cluster. A render
path WITHOUT the webhook, e.g. `helm template`, would instead register a
gateway host that matches nothing: the route silently vanishes.) The
G2-specific sequence around this step is [`SSO-ROLLOUT-G2.md`](changelogs/SSO-ROLLOUT-G2.md).

## Network isolation (optional — off by default)

Two independent, ADDITIVE controls; both default **false** (default render
byte-identical; `values.hosted-trial.yaml` enables both). `networkPolicy.*`
is CNI-layer; `ezua.authorizationPolicy.*` is mesh L7 (requires
`ezua.enabled`).

- **networkPolicy** baseline (empty `ingress`/`egress` lists) — default-deny
  on the gateway pods; allows (i) istio-system ingress-gateway pods
  (`ingressGatewayLabels` — SITE-verify), (ii) same-namespace callers,
  (iii) ipBlock for NODE-originated kubelet probes / `kubectl port-forward`
  (no selector matches the node; carve your pod CIDR out with
  `probeExceptCidrs`). Egress: kube-dns (53 UDP+TCP), embedded Postgres
  (5432, only when `database.embedded.enabled`), pod/service CIDRs
  (`inClusterEgressCidrs` — mirror `proxy.noProxy`). Anything else
  (external DB, corporate proxy, JWKS, istiod) rides
  `networkPolicy.ingress`/`egress` — non-empty lists REPLACE the baseline.
- **authorizationPolicy** baseline (empty `rules`) — ALLOW (i) istio-system
  ingress-gateway workloads (`ingressNamespace`, tightenable via
  `ingressGatewayPrincipals` SPIFFE IDs), (ii) same-namespace callers;
  everything else is denied. Two ADDITIVE relief knobs (both live-verified
  on G2 2026-10-07 — the full story is in § SSO self-mint rollout step 1):
  - `allowMetricsScrape: true` — an ANY-SOURCE rule scoped to exactly
    `/metrics` + `/metrics/` on the service port. Deliberately NOT
    namespace-scoped: kube-prometheus scrapes raw POD IPs, and a raw-IP
    outbound connection is envoy passthrough — PLAINTEXT, no peer
    identity — EVEN FROM A MESHED SCRAPER (verified live: prometheus-0
    given a sidecar, `up` stayed 0), so no `namespaces:`/`principals:`
    matcher can ever admit it. `/metrics` labels carry endpoint/outcome
    only (never tenant, key or prompt material — see
    templates/servicemonitor.yaml), so this restores the pre-policy
    posture for that one path.
  - `allowInClusterApi: true` (SITE call, default false) — ANY source on
    the traffic port EXCEPT `/v1/identity` + `/v1/keys/for-me`: for
    in-cluster consumers that call the gateway's SERVICE directly and are
    unmeshed (dsh-web-helm, the OpenWebUI namespaces, opencode-web-helm,
    clearwing…) — unmeshed callers carry no peer identity, so
    per-namespace rules cannot match them either. The app's own key auth
    remains the control on every admitted path (the edge classifies
    `/v1/*` identically). The identity-bearing surfaces — `/v1/identity`,
    `/v1/keys/for-me`, `/v1/enroll` and `/admin` — are the routes whose
    resolvers consume the browser-header rung (`routes_identity.py`,
    `routes_enroll.py`, `routes_admin.require_admin`), so they stay
    reachable ONLY from the edge namespace. That exclusion is what makes
    `settings.trustProxyIdentity` safe: an in-cluster pod can never
    deliver spoofed `X-Auth-Request-*` headers to a mint or an admin
    action. (Rule (ii)'s same-namespace leg carries the same `notPaths`
    set as this one — audit r5 #6.)
Kubelet probes cannot be admitted by AuthorizationPolicy (outside the mesh,
no separate probe port). Relief valve: `excludeInboundPorts: [9200]` stamps
`traffic.sidecar.istio.io/excludeInboundPorts` on the pod — that port then
bypasses Envoy AND all AuthorizationPolicy rules (the ipBlock rule becomes
its control); never exclude a port serving real traffic. Without this (or a
platform-side probe-port exemption) probes fail.
`settings.trustProxyIdentity` coupling: the oauth2-proxy identity headers
are client-spoofable at the pod — the flag alone is NOT the control. Safe
only where an enforcing proxy overwrites the headers on every request AND
this AuthorizationPolicy keeps unauthenticated callers from bypassing it.

## SSO self-mint rollout (coupled, ordered)

The SSO surfaces (homepage card, `POST /v1/keys/for-me`,
`GET /v1/identity`) are gated by three app flags that are inert — or
UNSAFE — individually. The pieces move as ONE ordered rollout; doing the
steps out of order either exposes spoofable identity headers or flips
flags nothing enforces. The steps below assume `ezua.enabled=true` and
`metrics.enabled=true` (the G2 convention). **G2-specific runbook:**
[`SSO-ROLLOUT-G2.md`](changelogs/SSO-ROLLOUT-G2.md) — the same steps with exact
values, exact commands, the live-verified starting state and who executes
each step.

1. **Policy relief rules** — BEFORE anything enforces, give the traffic
   that must survive enforcement its allowance:
   - `ezua.authorizationPolicy.allowMetricsScrape: true`: admits the
     `/metrics` scrape as an ANY-SOURCE rule scoped to `/metrics` +
     `/metrics/` on the service port. ⚠️ 2026-10-07 correction (verified
     live on G2): a NAMESPACE-scoped rule cannot do this job —
     kube-prometheus scrapes raw POD IPs, and raw-IP outbound is envoy
     passthrough (PLAINTEXT, no peer identity) EVEN FROM A MESHED SCRAPER
     (prometheus-0 was given a sidecar and `up` stayed 0 until this
     fix). `/metrics` labels carry endpoint/outcome only (see
     templates/servicemonitor.yaml), so the any-source scope restores the
     pre-policy posture for that path. (`metricsNamespace` is legacy now
     — kept so existing site values stay valid.)
   - `ezua.authorizationPolicy.allowInClusterApi: true` (SITE call): the
     in-cluster consumers call the gateway's SERVICE DIRECTLY and are
     unmeshed — dsh-web-helm, the OpenWebUI namespaces, opencode-web-helm,
     clearwing — and without this rule the first policy render 403-denied
     them at the mesh (their model/MCP calls never reached the app;
     verified live 2026-10-07). Admits ANY source on the traffic port
     EXCEPT the identity-bearing paths: the app's own key auth is the
     control there (the edge classifies `/v1/*` the same way), while
     `/v1/identity` + `/v1/keys/for-me` stay edge-only so the step-4
     header rung can never be spoofed from inside the cluster.
   - `ezua.authorizationPolicy.excludeInboundPorts: [9200]` ONLY if your
     kubelet probes must bypass the mesh (see § Network isolation for the
     trade-off; NEVER a port serving real traffic).
2. **Namespace sidecar injection + policy enabled — ATOMIC.** Label the
   release namespace for Istio injection (`istio-injection=enabled`) and
   set `ezua.authorizationPolicy.enabled: true` in the same change, then
   restart the pods so both land together:
   - injection WITHOUT the policy is exposure — the sidecar passes
     in-cluster traffic straight through, and the proxy identity headers
     become spoofable at the pod;
   - policy WITHOUT injection is a NO-OP — AuthorizationPolicy is mesh L7;
     no sidecar, no enforcement point (verified live on G2 2026-10-06:
     the pods carried no istio-proxy);
   - ⚠️ CLI deploys MUST resolve `${DOMAIN_NAME}` in the values file
     first (sed — see § Gateway exposure): a raw `helm upgrade` fails the
     VirtualService at the istio webhook and the release lands `failed`
     with the policy already applied. The PCAI values editor substitutes
     the placeholder itself.
3. **Platform edge chain** — at the ezaf-gateway/oauth2-proxy layer:
   - BROWSER paths (`/`, `/ui`) go behind SSO (Keycloak redirect).
   - API paths (`/v1/*`, `/mcp/*`) get a **skip-auth carve-out** — the
     chain admits them unauthenticated and the gateway's own bearer
     auth (`Authorization: Bearer hllm-…` / `X-API-Key`) is the control.
     ⚠️ MM RAG lesson (D25): their host 302s EVERYTHING to Keycloak, so
     bare REST clients with API keys cannot reach the app at all —
     key-based REST is still broken there. Do not repeat it: no
     blanket-302 on this host.
     - **`/admin` is ALSO a skip-auth path** (classify it with `/v1` and
       `/mcp`). The console authenticates with the admin key in its
       `Authorization: Bearer` header — behind an SSO chain that header
       would be overwritten by the proxy's id_token, which is NOT an
       admin key (`require_admin` reads Authorization and hash-compares
       against the provisioned admin keys). The console therefore ALSO
       sends `X-API-Key: <admin key>` as the surviving envelope for any
       deployment that DOES chain `/admin` — the header is inert on
       skip-auth paths. Group-based admin over the chain (no console key
       at all) is the documented alternative: `PCAI_LLM_ADMIN_GROUPS` +
       keep `/admin` chained.
     - **EXCEPT** `/v1/keys/for-me` and `/v1/identity`: these two MUST stay
       BEHIND the SSO chain (no skip-auth). The SPA calls them with no
       credentials on purpose — their identity IS the headers the chain
       injects; exposed unauthenticated, `for-me` mints keys for whoever
       asks.
4. **App flags** — only once the chain above actually enforces:
   `settings.trustProxyIdentity: true` and
   `settings.ssoKeyCreateEnabled: true` (the mint flow's two gates);
   `settings.tokenShadowKeys: true` (+ `tokenShadowTenantId`) is OPTIONAL
   — needed only for token-only callers to get a ledger anchor. Flipping
   any of them earlier than step 3 = trusting headers no proxy overwrites
   (in-cluster forgery mints keys).
5. **Homepage card lights up** — the SPA calls `GET /v1/identity` on
   boot; when the identity ladder resolves a browser-header identity AND
   the flags are on (`flags.self_mint: true`), the "Sign in with SSO"
   card renders. Verify with a browser session: mint → shown-once key →
   409 on second mint → rotate flow replaces the old key. Sign-out
   navigates to bare `/oauth2/sign_out` (no `rd=` return-to parameter) —
   the gateway serves no logged-out landing page; if one is wanted later
   it is an edge/oauth2-proxy route concern (allowlist a `rd=` target),
   not a UI change.

Platform oauth2-proxy facts this rollout relies on (read from the live
pod config, 2026-10-06): on chained hosts it injects
`Authorization: Bearer <id_token>`, `X-Auth-Request-User` and
`X-Auth-Request-Groups`. It does **NOT** inject `X-Forwarded-Groups` —
the gateway's browser-identity reader accepts the `X-Auth-Request-Groups`
spelling too, but any module that reads only `X-Forwarded-Groups` sees no
groups behind the chain. The gateway itself reads `X-Forwarded-Groups`
only behind `PCAI_LLM_TRUST_FORWARDED_GROUPS` (default **false** = fail
closed): without that flag a groupless proxy identity plus a
client-supplied forwarded header self-granted admin groups (audit r5 #4,
2026-10-30).

## Kyverno vendor labels (optional)

`kyverno.enabled: true` ships a ClusterPolicy stamping the `hpe-ezua/*`
vendor labels so the EZUA console shows the app as "Ready" instead of
"Unknown". The ClusterPolicy OBJECT is cluster-scoped (creating it needs
cluster-admin), but its rules mutate only this release's namespace (scope
reminder below) — on hosted
trial units without it, leave it off: the chart also stamps the labels
statically on the pod template, which covers the same gap for pods this
chart creates. G2 (where Kyverno is installed) may enable it so the labels come
from one mechanism instead of two. Scope reminder: the policy's rules mutate only the release namespace (`kyverno.yaml:34-35`) — other namespaces need their own labelling story.

## Upstream credentials (models + MCP servers)

Most PCAI model endpoints (MLIS) and ALL fleet MCP servers require a bearer
token, even in-cluster. Three modes, first hit wins:

1. **Admin panel (recommended)** — paste the key in the console (Models or
   MCP tab). Stored Fernet-encrypted in Postgres under the master key
   (`PCAI_LLM_SECRETS_MASTER_KEY`, by-ref Secret
   `pcai-llm-gateway-secrets`, key `master-key` — create it:
   `kubectl -n <namespace> create secret generic pcai-llm-gateway-secrets --from-literal=master-key="$(openssl rand -hex 32)"`).
   Rotation = paste the new key + Save — **no pod restart**. Per-endpoint
   keys are the normal case: every model can have its own.

   **Rotating the MASTER key also invalidates the audit-chain tail anchors.**
   The chains themselves stay valid (their hashes are keyless), but each
   trail's HMAC anchor is keyed from the master key, so after a rotation
   `GET /admin/audit/chain` reports `anchor_status: "no-master-key"` /
   `"unanchored"` until the next chained write re-anchors it (bounded by the
   60 s anchor-refresh interval). Treat "unanchored" as *not verified* rather
   than as a failure — a genuinely tampered chain reports
   `anchor-mismatch`/`anchor-invalid`, which the console renders in red. The same panel
   path stores per-KEY MCP header overrides (Admin ▸ Keys ▸ `creds`) —
   e.g. the memory-MCP `X-API-Key` that gives each tenant its own identity
   (the legacy `X-Memory-Dataset` / `X-Dataset-Password` pair also works).
2. **By-reference Secret** (ops-managed tokens): `api_key_secret` /
   `auth_secret` names + `upstreamKeySecrets[]` / `mcpKeySecrets[]` chart
   values (env `PCAI_LLM_UPSTREAM_KEY__*` / `PCAI_LLM_MCP_KEY__*`).
   Rotation needs a pod restart.
3. **Open** (neither set): called without Authorization.

Rules: the master key is any string (SHA-256-derived to the Fernet key);
`openssl rand -hex 32` is the habit. **Rotating the master key invalidates
every stored credential** — re-save them from the panel. Plaintext keys are
never returned by any endpoint and never logged; the panel's re-save keeps
the stored key when the field is left blank.

## Sizing and scale signals

The gateway is a streaming proxy + a Postgres client, not a compute node.
All state lives in Postgres; the pod itself is stateless and cheap.

- **Defaults are deliberate**: 1 replica, `250m`/`512Mi` requests,
  `2`/`2Gi` limits, pool `min 5 / max 25`, 2 s bounded acquire. This serves
  the trial/small-team load class (≈10k requests/day) comfortably.
- **The bottleneck is the Postgres pool, not CPU.** Every admitted request
  touches Postgres several times (auth, gates, settle; a relay iteration
  adds a settle per model call). The hot path acquires with a bounded wait:
  when the pool is saturated the request fails FAST with
  **503 `database busy`** (retryable) instead of queueing forever — that
  503 is your saturation signal, by design.
- **Escalation order when you see it**:
  1. `settings.dbPoolMax` (25 → 50+) — cheapest fix; watch the shared
     Postgres' own `max_connections` (external mode: it serves other
     tenants too).
  2. Pod limits (`resources.limits`) if the loop is CPU-starved by SSE
     fan-out (many concurrent streams).
  3. `replicaCount` — replicas share the ledger with no coordination; the
     per-key concurrency cap and redeem throttle are per-pod, so a raised
     replica count multiplies those two soft limits as well.
  4. **Autoscaling (optional, off by default)** — `autoscaling.enabled: true`
     adds an HPA (autoscaling/v2) scaling the Deployment on CPU utilization
     (default target 70%, min 2 / max 6; all tunable in values). Requires a
     Metrics API provider (metrics-server or equivalent) on the unit.
     Behavior is deliberately asymmetric: scale-UP at most doubles per
     minute (a fresh pod needs its DB pool and first registry refresh before
     it is useful, so growth is staged), scale-DOWN sheds at most 25% per
     minute behind a 5-minute stabilization window (no thrash on bursty
     load). The same connection math as `replicaCount` applies: worst-case
     Postgres demand is `maxReplicas × dbPoolMax` — pick them together.
     CPU (not memory) is the signal on purpose: this pod's per-request work
     is CPU-visible (TLS, JSON, SSE framing) while memory is flat, and an
     HPA on memory would autoscale a leak. Note the HPA cannot fix the
     pool-bound case — step 1 stays first, because a new replica only
     multiplies pools when Postgres is the wall.
- **Ledger growth**: every outcome (including refusals) is a row; the
  `rate_windows` counters are pruned opportunistically on registry
  refreshes, the ledger and the embedding cache are not (the ledger is the
  audit artifact; the cache is sized for the corpus). On embedded mode,
  size the PVC for months-not-days and monitor the StatefulSet; trace
  tables grow only on relay traffic.

## Building the image (operators)

The published image comes from `docker/Dockerfile` (base:
`ghcr.io/astral-sh/uv:python3.14-bookworm-slim`, pinned by digest — this tag
moves). `docker/requirements.lock` records the known-good Python dependency
set the release was built with, so a rebuild of the same `pyproject.toml`
resolves identically. Release tooling owns publishes; sites normally pull
from ghcr.io or their mirror and never build.

## Upgrading

Upgrades are a values edit + re-apply, not a redeploy:

1. Import the newer chart package into PCAI (or update the chart source for
   operator installs).
2. Re-apply your values document — the previous `# SITE:`-marked values carry
   over unchanged (maps merge recursively; lists replace, not append).
3. Image tags flow from the chart (`image.tag` is release-managed); a new
   chart version rolls the Deployment with the new tag.
4. The Postgres schema is additive and idempotent — the ledger and budgets
   survive upgrades untouched. External mode has no PVC; in embedded mode
   the StatefulSet's PVC persists untouched across upgrades.

**Rollback.** On PCAI, re-apply the previous values document with the
previous chart package (import it the same way). Plain-Helm equivalent,
labelled as the operator/secondary path: `helm rollback pcai-llm-gateway
<revision> -n <namespace>`. The Postgres schema is additive and idempotent
(`CREATE TABLE IF NOT EXISTS` + guarded migrations); the ledger and budgets
survive both directions untouched. Generated Secrets are never regenerated
by an upgrade or rollback (lookup reuse).

### Uninstall — what persists

`helm uninstall pcai-llm-gateway -n <namespace>` (or the PCAI console's
delete of the deployment):

- Deletes: Deployment, Service(s), VirtualService/Ingress, (optional)
  ServiceMonitor, and in embedded mode the Postgres StatefulSet.
- PERSISTS: the embedded-Postgres PVC — named `pgdata-<serviceName>-<ordinal>`: the volumeClaimTemplate is `pgdata` and the StatefulSet is `database.embedded.serviceName` (default `pcai-llm-gateway-pg`), so the live name is `pgdata-pcai-llm-gateway-pg-0` — the usage
  ledger, tenants, keys and endpoint pools live there. Delete it separately
  if you want a clean slate; keep it to preserve state across a reinstall.
- Chart-generated Secrets persist too (deliberate — so a reinstall doesn't
  mint new admin keys); delete them explicitly if desired.

Sanity check after any change: `/ready` returns 200 (Postgres reachable) and
`kubectl logs deploy/pcai-llm-gateway` shows `database connected and schema
ensured` at startup. Then run the new-feature checks in
[§5](#5-verify-the-newer-surfaces-after-an-upgrade) when the release adds
surfaces you plan to use.

## Ledger partitioning (optional — run-once at scale)

The usage ledger is created unpartitioned and functions fine for a long
time: the budget gate's windowed SUMs ride the `(key_id, ts)` index and
stay fast regardless of total table age. Past tens of millions of rows the
heap itself becomes the cost — a startup warning fires at 50M estimated
rows as the nudge. The conversion to monthly RANGE partitions is a
deliberate run-once operation, NEVER performed at boot (an online
conversion locks/rewrites the table, unacceptable per-deploy):

```bash
# dry-run (prints the plan; touches nothing)
PCAI_LLM_DATABASE_URL=<the gateway's own DB URL> \
    .venv/bin/python scripts/partition_ledger.py
# apply (single ACCESS EXCLUSIVE transaction: legacy rename -> partitioned
# parent -> row copy -> parity check; run in a quiet window)
PCAI_LLM_DATABASE_URL=<...> .venv/bin/python scripts/partition_ledger.py --apply
# first-run belt-and-braces: keep usage_ledger_legacy until you've verified
PCAI_LLM_DATABASE_URL=<...> .venv/bin/python scripts/partition_ledger.py --apply --keep-legacy
```

The script needs only `asyncpg` and the gateway's own DB URL (run it from a
repo checkout — it is not inside the image). The gateway needs NO restart
afterward: inserts route to partitions automatically (a DEFAULT partition
catches out-of-range timestamps), and the budget gate's SUMs prune to the
current-window partitions — they get FASTER with age. The ledger is the
audit artifact: the migration deletes nothing; if you later want bounded
ledger retention, that is a separate, deliberate decision (dropping old
partitions is instant DDL once partitioned).

## Retention for operational tables (optional — off by default)

`agent_traces` (one row per relay step) and `conversations` (transcripts;
messages follow via ON DELETE CASCADE) grow forever without a policy. Two
values opt in (0 = disabled, the default — nothing is deleted until you
ask):

```yaml
settings:
  retentionTraceDays: 30          # relay-step traces
  retentionConversationDays: 90   # saved chat transcripts
```

Prunes piggyback the registry-refresh housekeeping hook (errors swallowed,
never fatal) and log their delete counts. The usage LEDGER is never pruned
regardless — it is the audit and budget artifact.

## Upstream CA bundle (optional — private-CA sites)

When your PCAI ingress presents a certificate from a PRIVATE CA, set:

```yaml
settings:
  upstreamCaBundle: /etc/pcai-ca/ca-bundle.crt
```

You must also mount the bundle file yourself (operator Secret + volume —
the chart does not create the mount). This applies to ALL upstream TLS:
model endpoints, MCP federation, admin probes, enrollment provisioning.
TLS verification is unconditional — there is no verify=False knob (the
AgentBuilder lesson: an awkward CA setup must not push anyone toward
disabling verification).
You must also mount the bundle file yourself (operator Secret + volume —
the chart does not create the mount). This applies to ALL upstream TLS:
model endpoints, MCP federation, admin probes, enrollment provisioning.
TLS verification is unconditional — there is no verify=False knob (the
AgentBuilder lesson: an awkward CA setup must not push anyone toward
disabling verification).

## Troubleshooting

Real failure modes from this package's history, `symptom → cause → fix`.
After ANY fix, re-run the § Upgrading sanity check (`/ready` 200 + a clean
schema startup in the log).

| Symptom | Cause | Fix |
|---|---|---|
| Pod `CreateContainerConfigError`, never starts | A referenced Secret does not exist (admin keys, DB URL, pg-auth, master key — by-ref `upstreamKeySecrets[]`/`mcpKeySecrets[]` names) | Create the Secret(s) from [Prerequisites A](#a-secrets-the-operator-creates-before-install) with the exact name the values reference; no re-apply needed — the pod retries the pull |
| Release lands `failed` at the VirtualService step; istio webhook: `domain name "${DOMAIN_NAME}" invalid` | A raw CLI `helm upgrade -f values.g2.yaml` passed the literal `${DOMAIN_NAME}` through — PCAI substitutes it in pasted values, the CLI does not; everything before the VS (the AuthorizationPolicy!) already applied | sed-resolve the placeholder into a copy first (§ Gateway exposure); apply through the PCAI values editor |
| In-cluster consumers (dsh-web-helm, OpenWebUI, opencode, clearwing) get 403 on model/MCP calls — off-cluster clients fine | The AuthorizationPolicy rendered WITHOUT the `allowInClusterApi` relief: unmeshed callers carry no peer identity, so no namespace rule can match them | Set `ezua.authorizationPolicy.allowInClusterApi: true` and re-apply (§ Network isolation) |
| Prometheus target for the gateway stays `up == 0` even with a meshed scraper | A namespace-scoped policy rule cannot admit the scrape: kube-prometheus scrapes raw POD IPs — envoy passthrough, plaintext, no peer identity, even from a meshed pod (verified live on G2) | `allowMetricsScrape: true` — the ANY-SOURCE rule scoped to `/metrics` + `/metrics/` (§ Network isolation) |
| Policy applied but nothing is enforced (spoofable headers reach the app) | Namespace labeled for injection but pods NOT restarted, or the label missing — AuthorizationPolicy without sidecars is a NO-OP | Move the `istio-injection=enabled` label AND `ezua.authorizationPolicy.enabled: true` in ONE change and restart the pods (§ SSO self-mint rollout step 2 — the pair is deliberately atomic) |
| Pod NotReady: kubelet probes fail while the app answers fine through the gateway | Probes run outside the mesh (no separate probe port) — an enforcing AuthorizationPolicy denies the kubelet | `excludeInboundPorts: [9200]` (the ipBlock rule becomes the control) or a platform-side probe-port exemption; never exclude a port serving real traffic (§ Network isolation) |
| `503 database busy` under load (retryable) | The Postgres pool is saturated — the bounded-acquire fail-fast is your saturation signal, by design | Escalation order: `settings.dbPoolMax` → pod limits → `replicaCount` → autoscaling; check the shared DB's `max_connections` — worst case is `maxReplicas × dbPoolMax` (§ Sizing; `templates/NOTES.txt` prints the arithmetic) |
| A relay turn dies as `(model error: relay model call timed out)` | ONE model call exceeded `upstreamTimeoutS` — under agent fan-out the usual trigger is prefill queueing on the endpoint, not a wedged upstream | Raise the COUPLED trio together: `upstreamTimeoutS` ≤ `toolRelayWallClockS` < `ezua.virtualService.timeout` (e.g. 900 / 900 / 960) — the long-turn note above |
| A model/MCP registered on one pod 404s on another replica (HPA ≥ 2) | `PCAI_LLM_REGISTRY_WATCH_S: 0` disabled the cross-replica watermark poll — correct only on a single replica | Restore the watcher (default 5 s); out-of-band SQL edits converge too (the stamps are trigger-maintained) |
| `/v1/keys/for-me` mints keys for unauthenticated callers | The identity endpoints were exposed without the SSO chain, or `trustProxyIdentity` / `ssoKeyCreateEnabled` were flipped before the chain enforced | Run the § SSO self-mint rollout IN ORDER; `for-me` + `/v1/identity` stay BEHIND oauth2-proxy, never skip-auth |
| Admin console 401 behind the SSO chain with a valid admin key | The proxy overwrote the console's `Authorization: Bearer` with its id_token — not an admin key | The console already sends `X-API-Key` as the surviving envelope; alternatively keep `/admin` skip-auth, or use `PCAI_LLM_ADMIN_GROUPS` (§ SSO self-mint rollout step 3) |
| Every request 401s or bounces to a login page after an SSO rollout | The edge chain 302s EVERYTHING to Keycloak (the MM RAG lesson, D25) — bare REST clients with `hllm-` keys cannot reach the app | Carve `/v1/*` + `/mcp/*` (and `/admin` unless using groups) as skip-auth at the edge; the app's key auth is the control (§ SSO self-mint rollout step 3) |
<!-- VERIFY (blanket-302 row): the 302-everything failure mode is platform-edge
behaviour (the MM RAG lesson, D25) — no template here renders the auth-skip
classes, so only the live cluster can confirm a given site's edge config. -->
| A diagnostic Job in this namespace hangs forever as `1/2 NotReady`, `active: 1` | `pcai-llm` is istio-injection=enabled — the Job's `istio-proxy` sidecar never exits, so the Job never completes and `ttlSecondsAfterFinished` never fires (14 such orphans observed 2026-10-14) | Set `sidecar.istio.io/inject: "false"` on the JOB's pod template + a short `ttlSecondsAfterFinished`; full runbook: [changelogs/DIAGNOSTIC-JOBS.md](changelogs/DIAGNOSTIC-JOBS.md) |
| Reinstall shows an empty ledger / new admin keys (or old state survives a delete) | `helm uninstall` deliberately PERSISTS the Postgres PVC and the chart-generated Secrets | § Upgrading → Uninstall — what persists: delete the PVC (`pgdata-pcai-llm-gateway-pg-*`) and the generated Secrets explicitly for a clean slate |
