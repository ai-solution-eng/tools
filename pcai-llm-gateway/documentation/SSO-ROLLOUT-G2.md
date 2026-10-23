# SSO Key-Mint Enablement — G2 Runbook (pcai-llm-gateway)

**Date:** 2026-10-07 · **Scope:** the coupled rollout that turns the already-deployed
SSO self-mint surfaces (v2.1.0 image) ON, per DEPLOYMENT.md §"SSO self-mint
rollout (coupled, ordered)". **This file is G2-specific**: exact values, exact
commands, live-verified starting state, and who executes each step.

**Roles:** `ops` = cluster/platform actions needing privileges the k8s-ops tool
SA lacks (namespace labels, the PCAI values editor / helm upgrade, the
ezaf-gateway edge config). `verify` = read-only live checks any session can run.

## Starting state (verified live 2026-10-07)

| # | Fact | Evidence |
|---|------|----------|
| 1 | v2.1.0 image deployed **with** the SSO surfaces | `GET /v1/identity` → 200, exact 6-key anonymous shape; `/health` → `{"status":"ok","version":"2.1.0"}` |
| 2 | Flags OFF | deployment env: `PCAI_LLM_TRUST_PROXY_IDENTITY=false`, `PCAI_LLM_SSO_KEY_CREATE_ENABLED=false`, `PCAI_LLM_TOKEN_SHADOW_KEYS=false` |
| 3 | No sidecar | namespace `pcai-llm` labels: only `kubernetes.io/metadata.name` + `name`; pods carry no `istio-proxy` |
| 4 | Host NOT behind the SSO edge | VS `pcai-llm-gateway-vs` routes every path straight to `pcai-llm-gateway-service:9200`; unauthenticated curl to `/health` and `/v1/identity` gets 200 **from the app** (no Keycloak 302) |
| 5 | Metrics scraping is ON and lives in ns `prometheus` | `metrics.enabled: true` (G2 convention); verified live 2026-10-06 that the prometheus ns scrapes pcai-llm's service port |

The order below is **load-bearing**: flags before the chain = trusting headers
no proxy overwrites (in-cluster forgery mints keys); injection without the
policy = the sidecar passes traffic straight through (same exposure); policy
without injection = a verified no-op.

## Step 1 — Policy relief rules (ops; chart values)

In the G2 values document (`helm/local/values.g2.yaml`, or the equivalent block
in the PCAI values editor) — these ride in the SAME values change as step 2:

```yaml
ezua:
  authorizationPolicy:
    enabled: true              # with step 2, ATOMICALLY
    ingressNamespace: istio-system
    allowMetricsScrape: true   # REQUIRED: metrics.enabled=true + policy on, or /metrics scrapes die
    metricsNamespace: prometheus
    excludeInboundPorts: []    # KEEP EMPTY — 9200 is the real traffic port; an excluded port bypasses ALL policy rules
```

Probe note: liveness + readiness share the app port (9200); the Istio sidecar's
probe rewriting carries kubelet probes — do NOT "relieve" them via
`excludeInboundPorts` (that punches the policy out of the port that serves
everything).

**2026-10-07 corrections (live-verified during this rollout):**
- `allowMetricsScrape` renders as an ANY-SOURCE rule scoped to `/metrics` +
  `/metrics/` on the service port. A namespace-scoped relief rule cannot
  work: kube-prometheus scrapes raw POD IPs, and raw-IP outbound is envoy
  passthrough — PLAINTEXT with no peer identity EVEN FROM A MESHED SCRAPER
  (prometheus-0 was given a sidecar; `up` stayed 0 until this fix).
- In-cluster clients that call the service DIRECTLY (unmeshed — DSH,
  OpenWebUI, opencode-web, the `clearwing` agent against
  `pcai-llm-gateway-service…:9200`) are denied by the first policy render.
  The values now set `allowInClusterApi: true`: any source on the traffic
  port EXCEPT `/v1/identity` + `/v1/keys/for-me` — the app's key auth
  remains the control there (the edge classifies /v1/* identically), and
  the identity paths stay edge-only so the step-4 header rung can never be
  spoofed from inside the cluster.

## Step 2 — Namespace injection + policy, ATOMIC (ops)

```bash
kubectl label namespace pcai-llm istio-injection=enabled
# then apply the step-1 values (helm upgrade / values editor) and restart the gateway so
# label + sidecar + policy land together:
kubectl -n pcai-llm rollout restart deployment/pcai-llm-gateway
```

⚠️ **CLI deploys must resolve `${DOMAIN_NAME}` first** (live-verified 2026-10-07:
a raw `helm upgrade -f helm/local/values.g2.yaml` passes the literal
placeholder through, and istio's validation webhook DENIES the
VirtualService patch — `domain name "${DOMAIN_NAME}" invalid` — leaving the
release `failed` while everything-before-the-VS (the AuthorizationPolicy!)
still applies). The PCAI values editor substitutes automatically; for CLI
deploys, generate the resolved copy once per session:

```bash
sed 's/\${DOMAIN_NAME}/pcai-se-ai-application.hst.rdlabs.hpecorp.net/g' \
  helm/local/values.g2.yaml > .scratch/values.g2.resolved.yaml
helm upgrade pcai-llm-gateway helm -n pcai-llm -f .scratch/values.g2.resolved.yaml
```

(The istio webhook that rejected the bad hostname is also what PROTECTED the
live VS — the garbage value never reached the cluster.)

The PG StatefulSet needs NO restart: the AuthorizationPolicy selects only the
gateway pods (`app: pcai-llm-gateway`) — a sidecar-less PG enforces nothing and
loses nothing.

**verify (after rollout):** pods show `2/2` containers (app + istio-proxy);
`up{namespace="pcai-llm"}` still 1 (the relief rule kept scraping alive).

## Step 3 — Platform edge chain (ops; ezaf-gateway / oauth2-proxy config)

Chain the host `pcai-llm-gateway.pcai-se-ai-application.hst.rdlabs.hpecorp.net`
through the platform oauth2-proxy (the mm-rag pattern) with this EXACT path
classification:

| Paths | Edge treatment | Why |
|-------|----------------|-----|
| `/`, `/ui` | **SSO-chained** (Keycloak redirect) | browser shell |
| `/v1/identity`, `/v1/keys/for-me` | **SSO-chained — NOT skip-auth** | their identity IS the injected headers; exposed unauthenticated, `for-me` mints keys for whoever asks |
| `/v1/*` (all other) | **skip-auth** | the app's own `Authorization: Bearer hllm-…` / `X-API-Key` is the control |
| `/mcp/*` | **skip-auth** | same |
| `/admin` | **skip-auth** | console authenticates with the admin key; it also sends the `X-API-Key` envelope for chained deployments |
| `/health`, `/ready`, `/metrics` | **skip-auth** (or leave unrouted) | in-cluster consumers don't traverse the edge |

⚠️ **No blanket 302** (the MM RAG lesson): their host 302s EVERYTHING to
Keycloak and bare REST clients with API keys cannot reach the app at all. Every
`/v1` path except the two above must pass through unauthenticated at the edge.

Platform proxy facts this relies on (read from its live pod config,
2026-10-06): on chained hosts it injects `Authorization: Bearer <id_token>`
(UA realm, aud `ua`), `X-Auth-Request-User` (claim `user`) and
`X-Auth-Request-Groups`. It does NOT inject `X-Forwarded-Groups` (the gateway
reads both spellings). The chip's subject therefore renders from the proxy's
`user` claim — expected `andrew-bydlon`; if the proxy is configured to forward
`preferred_username`, the gateway prefers `X-Auth-Request-Preferred-Username`.

**2026-10-08 — as executed on G2:** the chain is NOT an EnvoyFilter (the
only two envoyfilters in istio-system are platform plumbing). It is an
AuthorizationPolicy in istio-system — `pcai-llm-gateway-edge-chain`
(action CUSTOM, provider `oauth2-proxy`, selector `istio: ingressgateway`),
host+path-scoped: `/`, `/ui`, `/ui/*`, `/v1/identity`, `/v1/keys/for-me`
chained; every other path falls through unchained (CUSTOM fall-through =
the skip-auth half needs no rules at all). Manifest:
`.scratch/pcai-llm-edge-authz.yaml`, cloned from
`dsh-web-helm-auth-policy`'s shape (`security.istio.io/v1`). Mesh providers
live in the istio ConfigMap `extensionProviders`: the base `oauth2-proxy`
flavor forwards `authorization` + `x-auth-request-*` upstream and NOT
`X-Refresh-Token` (least privilege — the gateway never refreshes tokens).
GOTCHA: the first `--verify` ran seconds after apply and still read
200/401/200 — policies propagate over xDS; wait ~1 min and re-run before
concluding NOT CHAINED.

**verify (from outside, unauthenticated):**
```
https://<host>/            → 302 to Keycloak
https://<host>/v1/models   → 401 FROM THE APP (JSON {"detail":…}, no redirect)
https://<host>/v1/identity → 302 (chained — this is correct and required)
```

## Step 4 — App flags (ops; chart values — only once step 3 enforces)

```yaml
settings:
  trustProxyIdentity: true
  ssoKeyCreateEnabled: true
  # REQUIRED for the mint flow (2026-10-08, found live): POST /v1/keys/for-me
  # creates the key under the per-subject user in THIS tenant and 503s
  # without it — and the SPA hides the "Sign in with SSO" card on any
  # non-201/409 mint response (silent, no banner), so without a tenant id
  # the card appears (the probe's pre-parity self_mint ignored the tenant),
  # then vanishes on first click. Get the id from the console's Tenants tab:
  tokenShadowTenantId: "<tenant id — see the console's Tenants tab>"
  # OPTIONAL: lazy user+key for first-seen TOKEN (bearer) subjects — nothing
  # to do with the browser mint flow; leave off unless token-only callers
  # need ledger anchors:
  # tokenShadowKeys: true
```

Apply via the same values document; pods re-roll.

**verify:** from an SSO-signed-in BROWSER (not curl — the identity is the
point), open the chat page: the green chip `Signed in as andrew-bydlon · oidc`
renders, and the "Sign in with SSO" button appears. Mint → shown-once key →
auto-connect → chat. Second mint → 409 → Rotate replaces the key.

**2026-10-08 failure signature (G2, fixed):** chip green + card present but
it VANISHES ON CLICK with no dialog, no error → the mint POST returned
non-201/409 and the SPA hid the card by design. On G2 the cause was the
unset `tokenShadowTenantId` above (mint 503 "no tenant"); the probe-parity
fix (routes_identity now mirrors the mint's tenant precondition) makes the
card stay hidden instead of lying. If a card still disappears on click,
`GET /v1/identity` in the browser shows the current verdict and the mint
route's log shows the exact refusal class.

## Step 5 — Verification sweep (verify; any session can run)

1. `curl -sk https://<host>/health` → `{"status":"ok","version":"2.1.0"}` (app up through the chain).
2. `prom_query up{namespace="pcai-llm"}` → 1 (scrapes survived the policy).
3. The step-3 curl triple (302 / 401-from-app / 302).
4. The step-4 browser flow (the only check that exercises the header rung).
5. Revoked/minted-key sanity in `/admin` → Keys tab: the minted key shows `sso-create` provenance and its self-mint toggle.

## Rollback (each step reverses independently)

- Flags off (step 4 values) → surfaces hide again; chip included.
- Edge unchained (step 3 reverted) → skip-auth paths unaffected (they never
  needed the chain); browser paths unreachable-until-unchained.
- Policy off + label off + restart → byte-identical to the 2026-10-07 state.

## Known residuals

- The browser-header rung trusts `X-Auth-Request-*` headers with no peer/CIDR
  binding (the fleet D24 residual, accepted on mm-rag/sqlhandler with the same
  posture: the ezaf edge applies SSO centrally). The AuthorizationPolicy is
  what makes those headers un-spoofable in-cluster — which is why steps 2 and 4
  must not be reordered.
- Group-based admin over the chain (`PCAI_LLM_ADMIN_GROUPS`) is deliberately
  NOT part of this rollout (empty = bearer-only admin, byte-identical gate).
