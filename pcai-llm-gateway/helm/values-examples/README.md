# helm/values-examples — paste-ready values for PCAI

Example **full values documents** for the pcai-llm-gateway Helm chart. Each
file is complete (every chart key listed) and **intentionally secret-free** —
every credential is a `REPLACE_WITH_*` placeholder or an out-of-band Secret
reference (see *Placeholder convention* below).

| File | Deployment target |
|---|---|
| [`values.g2.yaml`](values.g2.yaml) | **Proxied corporate site** example (placeholder addresses): literal endpoint, corporate proxy on (`proxy.http`/`proxy.https` set, with the `NO_PROXY` cluster-local entries), network isolation ON (`networkPolicy.enabled` + `ezua.authorizationPolicy.enabled` with both relief rules). |
| [`values.hosted-trial.yaml`](values.hosted-trial.yaml) | **Hosted trial** on a customer PCAI: `${DOMAIN_NAME}` endpoint placeholder preserved (PCAI resolves it before rendering). Network isolation ON (`networkPolicy.enabled` + `ezua.authorizationPolicy.enabled` — default-deny NetworkPolicy + Istio AuthorizationPolicy; see `documentation/DEPLOYMENT.md` § Network isolation). |
| [`values.hosted-trial-sso-prometheus.yaml`](values.hosted-trial-sso-prometheus.yaml) | **Hosted trial + SSO + off-peak** — the trial profile with the SSO self-mint rollout applied (`settings.auth.trustProxyIdentity` / `settings.auth.ssoKeyCreate.enabled` / `settings.auth.tokenShadow.tenantId`, `ingressGatewayPrincipals` pinned, metrics + in-cluster relief rules) and off-peak billing enabled. Both features carry deploy-time prerequisites — **read the file header**: the platform edge chain + namespace injection must be live before the flags, and `prometheus` must be a **registered, enabled MCP server** (a registry row, never a values key). |

## Settings layout

The `settings:` block is grouped by subsystem — one `PCAI_LLM_*` env var per leaf — and
every example file follows the same order:

`server` · `upstream` · `dbPool` · `retention` · `cache` (`embedding`, `response`,
`metering`) · `relay` (`reasoning`, `compress`, `clientDispatch`) · `mcp` (`argPolicy`) ·
`routing` (`sticky`, `sessionAffinity`, `loadAwareCascade`, `modelGroups`) · `models` ·
`healthProbe` · `auth` (`tokenValidation`, `tokenShadow`, `ssoKeyCreate`) · `memory`
(`enroll`, `distiller`, `capture`) · `offpeak` · `audio` · `tracing`.

Field reference: [`../values.yaml`](../values.yaml). Migrating a values document that
still carries the old flat `settings:` keys: `documentation/DEPLOYMENT.md` § Values
migration.

## Why this folder exists (and why it is secret-free)

- This folder is **hardlink-synced to GitHub** (and into delivery trees) by the
  repo hardlinker — anything here is public. Keep it free of real credentials,
  real customer endpoints, and personal emails. In-cluster service DNS names
  are fine.
- The **real per-site values** (credentials, endpoints) live in
  **`helm/local/`** — gitignored, ignored by the hardlinker, and excluded from
  the packaged chart (`.helmignore`: `local/`).
- By default the chart NEVER creates a Secret and NEVER inlines a credential
  in values (the K8S-MCP lesson): the Postgres URL and the admin keys come
  from **operator-created Secrets referenced by name**. Trial-friendly
  exception: the `*.create: true` flags (`adminApiKey.create`,
  `secretsMasterKey.create`, `database.embedded.auth.create`) make ONE
  helm install generate the Secrets — lookup-preserved on every later apply
  (not for offline-render apply pipelines; see `helm/values.yaml`).
  See `documentation/DEPLOYMENT.md` for the exact `kubectl create secret`
  commands.

## Using an example the PCAI way (no helm, no kubectl)

1. **Import the packaged chart** (`.tar.gz`) into PCAI once — PCAI users never
   run `helm install` or `kubectl apply`.
2. **Secrets — choose one path:**
   - *Operator-managed (default):* create the Secrets the chart references
     out-of-band — the Postgres URL Secret and the admin-keys Secret
     (commands in `documentation/DEPLOYMENT.md`; the chart's NOTES.txt prints
     them verbatim).
   - *Trial-friendly:* flip the `*.create: true` flags in the pasted values
     document (`adminApiKey`, `secretsMasterKey`, and, in embedded-Postgres
     mode, `database.embedded.auth`) and re-apply — the chart generates and
     preserves the Secrets itself.
3. Open the deployment's **Helm Values** editor (or the PCAI API).
4. **Paste the whole example file** as the values document.
5. **Adjust the site-specific lines** — the endpoint host, the existingSecret
   names if your Secrets differ, timeouts.
6. **Apply.** PCAI resolves `${DOMAIN_NAME}` before rendering. To change
   anything later, edit the same values document and re-apply (an upgrade).

## Placeholder convention

Every value **you** must replace is spelled `REPLACE_WITH_*` —
`REPLACE_WITH_ADMIN_KEY`, `REPLACE_WITH_DB_PASSWORD` in comments. The one
exception is `${DOMAIN_NAME}`: PCAI substitutes it before rendering, so leave
it as-is. Values that reference operator-created Secrets are already real
Secret *names* (not credentials) and can be kept as-is.

## Using an example as an operator

```bash
# render locally to inspect what the chart would create
helm template pcai-llm-gateway helm -f helm/values-examples/values.g2.yaml

# or deploy/upgrade directly (operators only — not the PCAI flow)
helm upgrade --install pcai-llm-gateway helm -n <namespace> \
  -f helm/values-examples/values.hosted-trial.yaml
```

## Documentation

- Deployment guide: [`../../documentation/DEPLOYMENT.md`](../../documentation/DEPLOYMENT.md)
- Post-deploy verification: [`../../documentation/changelogs/VERIFICATION.md`](../../documentation/changelogs/VERIFICATION.md)
- Field reference for every key: [`../values.yaml`](../values.yaml)

## Keys managed at runtime, not in values

Some feature levers are intentionally absent from these (and every) values
documents because they are admin-curated at RUNTIME through the API/console,
not deploy-time settings:

- **Per-model fallback chains** — `GET/PUT /admin/models/{name}/fallbacks`
  manages each virtual model's failover chain; the only values knob is
  `settings.routing.fallbackMax` (default 2, the cap on hops walked per
  request). Set it in the pasted document if you plan to use chains.
- **Zero-completion insurance** — `settings.upstream.zeroCompletionRetry` (default
  false): flip it on in values only after observing the empty-completion
  rate in the ledger (see DEPLOYMENT.md § values → environment variables).
- **Complexity-routing participation** — `settings.routing.*` in values are
  the scoring knobs; WHO participates is per-profile `routing_policy`
  (`off|shadow|active`), console-editable after deploy (Admin ▸ Profiles).
- **Native wire tool deltas** — `settings.relay.streamWireTools`
  (default false): keep false for executing stream clients (DSH); true
  only for render-without-execute clients.
- **Reasoning continuity** — `settings.relay.reasoning.enabled` (default
  true) + `settings.relay.reasoning.maxChars` (default 2000). Shows the
  model its OWN prior reasoning, both in-loop (between tool calls) and
  inbound (what the client replays). With it off, a long relay turn hands
  the model dozens of consecutive examples of itself calling tools with no
  thinking — measured 86.1% of relayed tool-call steps carried none, vs
  46.1% direct — which teaches it to skip reasoning. The CAP is the storm
  guard: the 2026-09-18 incident was UNBOUNDED per-cycle re-append growth
  (one event reached 6,630 copies of a line, ~401 KB); at 2000 chars a
  32-iteration turn tops out near 63 KB. `enabled: false` restores the old
  full strip byte-for-byte. Note this is input that rides EVERY subsequent
  iteration, so lower the cap if token spend matters more than continuity.
- **Client tool dispatch** — `settings.relay.clientDispatch.enabled` (default
  false) + `settings.relay.clientDispatch.timeoutS` (120): set true only where a
  harness actually registers local tool offers.
- **Network isolation** — `networkPolicy.*` and
  `ezua.authorizationPolicy.*` (both default false; enabled together in
  `values.hosted-trial.yaml`): default-deny NetworkPolicy + Istio
  AuthorizationPolicy around the gateway pods. The kubelet-probe caveat and
  the `settings.auth.trustProxyIdentity` coupling are documented in
  `documentation/DEPLOYMENT.md` § Network isolation.
- **MCP registry rows** — the servers the gateway federates (and the
  `prometheus` row off-peak billing samples through) are Postgres data,
  registered at runtime via Admin ▸ MCP / `POST /admin/mcp-servers`. No
  values key creates one: `settings.offpeak.mcpServer` is a NAME LOOKUP in
  that registry, so a deployment with `offpeak.enabled: true` and no enabled
  `prometheus` row bills at full price and shows
  *"MCP server 'prometheus' is not in the enabled registry"* on the Off-peak tab.
- **Prometheus alert rules** — `metrics.prometheusRule.enabled` (default
  false; requires `metrics.enabled: true` + the PrometheusRule CRD):
  `rules: []` renders the default ruleset (error ratio, breaker-open,
  budget-rejection spike); a non-empty `rules:` list replaces it wholesale.
  The hosted-trial example ships it on.
- **Per-key stream events** — a RUNTIME key attribute, not a values knob:
  `PATCH /admin/keys/{id}` with `{"stream_pcai_events": false}` (or the
  per-request `X-PCAI-Stream-Events: 0` header) serves strict validating
  clients a byte-clean OpenAI-spec stream without the in-`stream`
  `{"pcai": …}` telemetry events (default ON per key).
