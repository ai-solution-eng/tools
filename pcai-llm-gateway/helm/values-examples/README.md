# helm/values-examples — paste-ready values for PCAI

Example **full values documents** for the pcai-llm-gateway Helm chart. Each
file is complete (every chart key listed) and **intentionally secret-free** —
every credential is a `REPLACE_WITH_*` placeholder or an out-of-band Secret
reference (see *Placeholder convention* below).

| File | Deployment target |
|---|---|
| [`values.g2.yaml`](values.g2.yaml) | **Proxied corporate site** example (placeholder addresses): literal endpoint, corporate proxy on (`proxy.http`/`proxy.https` set, with the `NO_PROXY` cluster-local entries), network isolation OFF (chart default — enable deliberately per site). |
| [`values.hosted-trial.yaml`](values.hosted-trial.yaml) | **Hosted trial** on a customer PCAI: `${DOMAIN_NAME}` endpoint placeholder preserved (PCAI resolves it before rendering). Network isolation ON (`networkPolicy.enabled` + `ezua.authorizationPolicy.enabled` — default-deny NetworkPolicy + Istio AuthorizationPolicy; see `documentation/DEPLOYMENT.md` § Network isolation). |

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
5. **Adjust the `# SITE:` lines** — the endpoint host, the existingSecret
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
  `settings.modelFallbackMax` (default 2, the cap on hops walked per
  request). Set it in the pasted document if you plan to use chains.
- **Zero-completion insurance** — `settings.zeroCompletionRetry` (default
  false): flip it on in values only after observing the empty-completion
  rate in the ledger (see DEPLOYMENT.md § values → environment variables).
- **Complexity-routing participation** — `settings.routing*` in values are
  the scoring knobs; WHO participates is per-profile `routing_policy`
  (`off|shadow|active`), console-editable after deploy (Admin ▸ Profiles).
- **Native wire tool deltas** — `settings.toolRelayStreamWireTools`
  (default false): keep false for executing stream clients (DSH); true
  only for render-without-execute clients.
- **Reasoning continuity** — `settings.reasoningHistoryEnabled` (default
  true) + `settings.reasoningHistoryMaxChars` (default 2000). Shows the
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
- **Client tool dispatch** — `settings.clientToolDispatch` (default false)
  + `settings.clientToolDispatchTimeoutS` (120): set true only where a
  harness actually registers local tool offers.
- **Network isolation** — `networkPolicy.*` and
  `ezua.authorizationPolicy.*` (both default false; enabled together in
  `values.hosted-trial.yaml`): default-deny NetworkPolicy + Istio
  AuthorizationPolicy around the gateway pods. The kubelet-probe caveat and
  the `settings.trustProxyIdentity` coupling are documented in
  `documentation/DEPLOYMENT.md` § Network isolation.
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
