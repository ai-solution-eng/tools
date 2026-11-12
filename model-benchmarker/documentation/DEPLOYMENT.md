# Deployment — ModelBenchmarker

Deployment is a values problem: import the packaged chart into PCAI once,
create the **required Secrets** out-of-band (or let the chart generate the
web-key Secret on first install), then everything below is edited in the
chart's values (the PCAI **Helm Values** editor, or the PCAI API) and
re-applied. Operators running plain Helm do the same with
`-f <values-file>`; per-cluster credentials-bearing values live in
`helm/local/` (gitignored, never packaged). The chart is
`model-benchmarker` 1.1.2 (image
`ghcr.io/ai-solution-eng/model-benchmarker:v1.1.2`), and the web app it
deploys is the PCAI surface for this package — the benchmark engines run
inside it as subprocesses, not as separate deployments.

## Hosted-trial quickstart (first deploy in ~15 minutes)

Prereqs: you can reach the unit's PCAI console and have a namespace to
deploy into. No cluster-admin required (the optional Kyverno policy stays
off; the chart stamps the `hpe-ezua` labels itself). The rest of this
document is the reference detail.

### 1. Import and install

1. Import the packaged chart (`model-benchmarker-1.1.2.tar.gz`) into PCAI once.
2. Open the deployment's **Helm Values** editor and paste
   `helm/values-examples/values.hosted-trial.yaml` wholesale.
3. Adjust the site lines: `ezua.virtualService.endpoint` (keep the
   `${DOMAIN_NAME}` placeholder — PCAI substitutes it before rendering;
   a raw CLI deploy must resolve it first, § Developer and secondary
   paths), `persistence.storageClass` (leave `""` for the cluster
   default), and `image.repository` / `imagePullSecrets` if your unit
   pulls from a mirror.
4. Apply. First boot pulls the app image — if pulls fail, set the mirror
   + `imagePullSecrets`.

### 2. Secrets (choose ONE path)

- **Chart-generated (recommended on trials):** leave
  `security.existingSecret` empty. On first install the chart creates
  `model-benchmarker-keys` with a freshly generated 32-character web key
  (and, if you set `bench.endpointApiKeys`, the endpoint keys). Later
  applies never regenerate it — the released value is looked up and kept.
  Fetch the web key:
  `kubectl -n <namespace> get secret model-benchmarker-keys -o
  jsonpath='{.data.BENCH_API_KEYS}' | base64 -d; echo`
- **Operator-managed:** create the web-key and/or endpoint-key Secrets by
  hand before applying ("Required Secrets" below) and point
  `security.existingSecret` / `bench.endpointApiKeysExistingSecret`
  at them.

### 3. Smoke test

```bash
kubectl -n <namespace> rollout status deploy/model-benchmarker
# In-cluster (no DNS / gateway needed):
kubectl -n <namespace> port-forward svc/model-benchmarker-api 8000:80
curl -s http://localhost:8000/healthz          # -> {"status":"ok",...}
curl -s http://localhost:8000/                 # memory estimator page
```

Through the gateway, the app is served at
`https://<ezua.virtualService.endpoint>` (the PCAI console shows the
app card once the `hpe-ezua` label match lands — the chart stamps the
labels statically, so no Kyverno is needed for the card to go Ready).

### 4. First benchmark

Open `/<host>/benchmark/chat` (or paste the URL into a browser) — the
**/unlock** page appears: enter the web key from §2. It is stored in a
browser cookie (`mb-key`) for that origin and sent as `X-API-Key` on
every later API call, so the form is the only place a human ever types a
key. Pick a model class (the dropdown is seeded from the deployment
catalog, or paste the model's `config.json` offline), fill in the target
URL (the combobox offers `bench.suggestions`; any http(s) URL is
acceptable), press Launch. The run appears on /results with a live log.

## Deployment profiles: Internal G2 vs Hosted trial

The same chart renders two profiles; the difference is a handful of values
(and the Secret posture). `helm/values-examples/` ships a sanitized
paste-ready document for each (`helm/local/` holds the gitignored
per-site copies with real credentials): pick the matching file, paste it
into the *Helm Values* editor, adjust the site lines, apply.

| | **Internal G2** (`values-examples/values.g2.yaml`) | **Hosted trial** (`values-examples/values.hosted-trial.yaml`) |
|---|---|---|
| `ezua.virtualService.endpoint` | `model-benchmarker.${DOMAIN_NAME}` — keep the placeholder in the PCAI values editor; a raw CLI `helm upgrade` must sed-resolve it first (istio denies the unresolved literal, § Developer and secondary paths) | `model-benchmarker.${DOMAIN_NAME}` — same; the example ships the placeholder |
| Web key | Operator-managed: `security.existingSecret: bench-platform-keys` (key `BENCH_API_KEYS`) — the chart's Secret then carries endpoint keys only | Chart-generated (`security.existingSecret: {}`) |
| Endpoint keys | `bench.endpointApiKeys` seeded in values (the example ships a few SE-G2 entries) — sticky on upgrade (§ Required Secrets) | Empty by default; set when the trial has endpoints to benchmark, or bring an existing Secret via `bench.endpointApiKeysExistingSecret` |
| `bench.suggestions` | Populated (the SE-G2 endpoint list) | Empty by default |
| `bench.prometheusUrl` | Set (in-cluster Prometheus) — the GPU scaling curve works | Empty by default — runs still produce latency data, just no GPU join |
| Gateway SSO (`ezua.authorizationPolicy`) | Off internally | On customer clusters with an oauth2-proxy CUSTOM provider — § Authentication and SSO |
| Egress / proxy | Corporate proxy ON (`proxy.http/https`) with the cluster-local `NO_PROXY` entries kept | `proxy: {}` — direct egress |
| Kyverno | Off — the chart stamps the `hpe-ezua` labels statically; the ClusterPolicy needs cluster-admin | Off — same; a locked-down customer cluster may not permit ClusterPolicy creation at all |

## Required Secrets

The chart creates exactly one Secret, `model-benchmarker-keys`, and only
the keys it does not source elsewhere (`helm/templates/secret.yaml`).
Credentials are referenced by name, never inlined into the rendered
workload: the deployment reads them via `secretKeyRef`, so a missing or
misnamed Secret makes the pod fail loud at creation
(`CreateContainerConfigError`) rather than run unauthenticated.

| Secret | Owned by | Key(s) | Purpose | Failure mode if absent/wrong |
|---|---|---|---|---|
| `model-benchmarker-keys` | **Chart** (created on first install, value preserved on every later apply via `lookup`; deleted with the release — the PVC is kept, the keys are not) | `BENCH_API_KEYS` — web key, inline seed (`security.apiKey`) or generated 32-char | Unlocks the gated browser surface and API launch (X-API-Key / Bearer / cookie) | Not needed explicitly: it is created. A wrong `security.existingSecret` name instead fails the pod at creation |
| Web-key Secret (any name, e.g. `bench-platform-keys`) | **Operator** | key set in `security.existingSecretApiKey` (default `BENCH_API_KEYS`) | The web key, operator-managed (G2 pattern). When `security.existingSecret` is set, this is the SOLE source — the chart's Secret no longer renders `BENCH_API_KEYS` | Pod stuck `CreateContainerConfigError` (secretKeyRef not found). Symptom is identical for a wrong `existingSecretApiKey` key name |
| Endpoint-key Secret (any name) | **Operator** | key set in `bench.endpointApiKeysExistingSecretKey` (default `endpoint-api-keys`) — a **JSON object** mapping exact target origin (`https://host:port`) to the target's API key | Target credentials for protected endpoints; injected into the bench subprocess as the Bearer / `PCAI_API_KEY` header. Mutually exclusive with `bench.endpointApiKeys` inline seeding | Pod fails at creation if the Secret/key is missing. If present but not valid JSON, the app logs `ERROR … endpoint keys ignored` at startup and runs still launch but 401 at the target |
| Image pull Secret (any name) | **Operator** | standard pull-secret format | Only if the unit cannot pull `ghcr.io/ai-solution-eng/model-benchmarker` directly | Image pull back-off (`ErrImagePull`) — the pod never starts |

Operator-managed Secrets, created out-of-band before apply:

```bash
# web key (comma-separate several = zero-downtime rotation, § Authentication and SSO)
kubectl -n <namespace> create secret generic bench-platform-keys \
  --from-literal=BENCH_API_KEYS='<generated key>'

# endpoint keys — a JSON object: exact origin (no trailing slash) -> key
kubectl -n <namespace> create secret generic bench-endpoint-keys \
  --from-literal=endpoint-api-keys='{"https://my-llm.serving.example:443":"<target key>"}'
```

### Keys the chart refuses to render without

- `ezua.virtualService.endpoint` — while `ezua.enabled`, the VirtualService
  template renders this verbatim; an empty value fails the render. PCAI
  substitutes the `${DOMAIN_NAME}` placeholder in pasted values before
  rendering; a raw `helm template`/`install` passes the literal straight
  through and istio's webhook denies it.
- A referenced-but-missing Secret (rows 2–3 above) fails the pod at creation,
  not the render — the release "succeeds" and the pod shows the error.
- There is no other refuse-to-render case: everything else degrades to a
  documented default (estimator-only app, emptyDir instead of PVC, …).

## Prerequisites: patches and cluster objects

Everything the chart does not render itself. Each row states why it exists
and what breaks without it.

| Object | Type / namespace | Why | What breaks without it | How to verify |
|---|---|---|---|---|
| **VirtualService** `model-benchmarker` | rendered by the chart into `istio-system` | Gateway route on the shared `ezaf-gateway` gateway; the app's public HTTPS URL. `ezua.virtualService.endpoint` is its hostname | No public URL for the app (in-cluster access still works). Render fails while the endpoint value is empty | `kubectl -n istio-system get virtualservice model-benchmarker`; `curl -sk https://<endpoint>/healthz` |
| **Istio AuthorizationPolicy** (CUSTOM, provider = oauth2-proxy) | rendered by the chart into `istio-system` when `ezua.authorizationPolicy.enabled: true` | Gateway-level SSO for customer clusters: unauthenticated browser traffic is redirected to the unit's OAuth2 proxy before it reaches the app; the proxy stamps the verified OIDC token into `X-Auth-Request-Access-Token`, which the app accepts as a key envelope (§ Authentication and SSO) | With `enabled: true` but no oauth2-proxy CUSTOM action provider in the cluster, policy creation fails or SSO never engages — the app's own API-key gate remains the only protection. With `enabled: false`, nothing is missing — the app is fully usable behind its key gate | `kubectl -n istio-system get authorizationpolicy -l hpe-ezua/app=model-benchmarker` |
| **Kyverno ClusterPolicy** `add-vendor-app-labels` | rendered when `kyverno.enabled: true` (cluster-scoped) | Belt-and-suspenders: stamps the `hpe-ezua/type: vendor-service` + `hpe-ezua/app` labels onto app objects at admission time | **Nothing** — the chart already carries the same labels statically on the Deployment and Service, and the EZUA card badges on label match. The policy is optional and needs cluster-admin, which locked-down customer clusters may not grant | `kubectl get clusterpolicy add-vendor-app-labels` (only when enabled) |
| **StorageClass** (cluster object) | `persistence.storageClass` (default `""` = cluster default) | Binds the `model-benchmarker-data` PVC (10Gi, RWO) that holds every run's artifacts, the committed-results library copy, and the measurement store | PVC stuck `Pending` → pod stuck `ContainerCreating` (volume mount). `persistence.enabled: false` trades this for an emptyDir: the app works but run history dies with the pod | `kubectl -n <ns> get pvc model-benchmarker-data` — `Bound`. RWX class required only if you ever raise `replicaCount` (keep it 1 — webapp.md § One endpoint at a time) |
| **Prometheus** (in-cluster, operator-managed) | URL in `bench.prometheusUrl` | The GPU scaling curve: DCGM exporter series joined over each measured level window | Runs still complete; the report simply has no GPU columns (latency/throughput only). The client may pick the label selector, never this URL | `curl -sk <prometheusUrl>/api/v1/query --data-urlencode 'query=up' | head` |
| **oauth2-proxy CUSTOM action provider** | cluster-level (customer clusters) | The provider object the AuthorizationPolicy action names | As above: SSO policy cannot resolve its action | Present on PCAI customer clusters; check with the unit team internally |

## Chart values walkthrough

Every values key, full path (chart 1.1.2). "REQUIRED" means the release or
the pod fails without it; the rest degrade to the stated default.

### Identity and image

| Key | Default | Meaning |
|---|---|---|
| `deployment.appName` | `model-benchmarker` | Name of the Deployment, Service (`<appName>-api`), VirtualService, Secret, Kyverno policy and the EZUA app label — one rename moves the whole object family |
| `deployment.name` | (helper) | Base for the PVC claim name `<name>-data` |
| `image.repository` | `ghcr.io/ai-solution-eng/model-benchmarker` | Application image |
| `image.tag` | (empty → chart appVersion `v1.1.2`) | Image tag; pin by digest for production builds |
| `image.pullPolicy` | `IfNotPresent` | Pull behaviour |
| `imagePullSecrets` | `[]` | Registry pull secrets (list of names) — only for private mirrors |
| `replicaCount` | `1` | Keep at 1: the run registry's in-flight lock is per-replica (webapp.md, § One endpoint at a time) |

### Benchmarks (the app's operator surface)

| Key | Default | Meaning |
|---|---|---|
| `bench.endpoints` | `[]` | ⚠ Documented in the values header as "the benchmarkable-target allowlist — empty = benchmarking disabled (fail-closed)", but **no template renders it and the app reads no matching env var in chart 1.1.2** — it is currently a no-op. The live target surface is `bench.suggestions` (advisory combobox) plus free-text, validation-checked targets, and the API-key gate. Do not rely on this key to restrict what gets benchmarked; track the chart work that wires it |
| `bench.suggestions` | `[]` | Curated targets offered in the launcher comboboxes (`[{name, url, kind: chat|endpoint|any}]`). **Advice only — a user can always type any http(s) URL**; entries are validated and bad ones skipped loudly. Rendered to the ConfigMap as `BENCH_TARGET_SUGGESTIONS` only when non-empty |
| `bench.endpointApiKeys` | `{}` | JSON object: exact target origin → that target's API key. Seeded into the chart Secret on first install (or explicit reseed) and **sticky on upgrade** — an upgrade with an empty entry keeps the stored JSON. Mutually exclusive with `bench.endpointApiKeysExistingSecret` |
| `bench.endpointApiKeysExistingSecret` | `""` | Name of an operator Secret holding the same JSON object (preferred for customer clusters: the credential never passes through the values editor). When set, the chart Secret no longer renders `BENCH_ENDPOINT_API_KEYS` |
| `bench.endpointApiKeysExistingSecretKey` | `endpoint-api-keys` | Key inside that Secret |
| `bench.prometheusUrl` | `""` | In-cluster Prometheus for the GPU join → `BENCH_PROM_URL`. Rendered only when non-empty |

### Security

| Key | Default | Meaning |
|---|---|---|
| `security.apiKey` | `""` | Inline seed for the generated web key (first install only — later applies keep the stored value). Prefer a generated key on trials; an inline seed is for sites that want a known value |
| `security.existingSecret` | `""` | Operator web-key Secret name. When set, the deployment reads the web key from it (sole source) and the chart Secret skips `BENCH_API_KEYS` |
| `security.existingSecretApiKey` | `BENCH_API_KEYS` | Key inside `security.existingSecret` |

### Service, resources, persistence

| Key | Default | Meaning |
|---|---|---|
| `service.type` | `ClusterIP` | Service type |
| `service.port` | `80` | Service port (the gateway routes to it) |
| `service.targetPort` | `8000` | Container port — the app always listens on 8000 (the image's `--port 8000`) |
| `resources.requests/limits` | requests cpu 100m + memory 256Mi; limits memory 1Gi (no CPU limit) | CPU/memory. The app is a thin launcher + report server; the heavy work is the bench subprocesses, which inherit the pod's limits |
| `persistence.enabled` | `true` | PVC vs emptyDir for `/data` |
| `persistence.size` | `10Gi` | PVC size |
| `persistence.storageClass` | `""` | Cluster default; set for units with multiple classes |
| `persistence.accessModes` | `[ReadWriteOnce]` | `ReadWriteMany` required only if `replicaCount` ever rises |
| The PVC carries `helm.sh/resource-policy: keep` | — | `helm uninstall` keeps the PVC (run history survives); delete the PVC by hand to wipe |

### EzUA / Istio integration

| Key | Default | Meaning |
|---|---|---|
| `ezua.enabled` | `true` | Renders the VirtualService (and the optional policy) into `istio-system` |
| `ezua.virtualService.endpoint` | **REQUIRED** while `ezua.enabled` | The app's public hostname, e.g. `model-benchmarker.${DOMAIN_NAME}`. PCAI substitutes the placeholder in pasted values; keep it in values files |
| `ezua.virtualService.istioGateway` | `istio-system/ezaf-gateway` | The shared PCAI gateway the route binds to |
| `ezua.virtualService.longTimeout` | (helper default) | Long client/idle timeouts for streaming benchmark responses |
| `ezua.authorizationPolicy.enabled` | `false` | Gateway-level SSO via the CUSTOM oauth2-proxy action — customer clusters only (§ Authentication and SSO) |
| `ezua.authorizationPolicy.providerName` | (unit-specific) | Name of the cluster's oauth2-proxy CUSTOM action provider object |

### Everything else

| Key | Default | Meaning |
|---|---|---|
| `kyverno.enabled` | `false` | Optional ClusterPolicy restamping the `hpe-ezua` labels at admission (`background: false`); needs cluster-admin and changes nothing the static labels already do |
| `pcai.enabled` | `false` | Explicit flag for PCAI-flavoured integrations; no longer follows proxy config (per-key, see below) |
| `proxy.http` / `proxy.https` / `proxy.noProxy` | empty | Corporate egress proxy, wired per-key into the ConfigMap — but only `.noProxy` (→ `no_proxy`/`NO_PROXY`) and `.https` (→ `https_proxy`/`HTTPS_PROXY` *and* `http_proxy`/`HTTP_PROXY`) render; **`.http` is accepted in values but no template reads it** (drift, flagged in the wave report). Each key renders only when non-empty — direct egress leaves `proxy: {}` and no proxy env exists. `noProxy` pre-seeds in-cluster ranges so benchmark traffic never traverses the proxy |
| `extraEnv` | `{}` | Arbitrary extra env for the container, rendered into the ConfigMap — the sanctioned place for the tuning vars below (`BENCH_MAX_CONCURRENT_RUNS`, `BENCH_MAX_RUN_SECONDS`, `BENCH_CATALOG_PATH`, `HF_TOKEN`) |
| `podAnnotations` / `nodeSelector` / `tolerations` / `affinity` | empty | Standard scheduling knobs |

## Underlying detail: values → environment variables

The pod's env comes from two sources — the ConfigMap (`envFrom`) and the
Secret (`envFrom`), plus direct `secretKeyRef` entries when operator
Secrets are configured. What each variable does in the app:

| Env var | Source | Effect |
|---|---|---|
| `BENCH_API_KEYS` | chart Secret (generated/seeded) **or** `security.existingSecret` (sole source when set) | The web key(s) — comma-separated list = zero-downtime rotation, re-read per request (§ Authentication and SSO) |
| `BENCH_ENDPOINT_API_KEYS` | chart Secret (seeded/sticky) **or** `bench.endpointApiKeysExistingSecret` (sole source when set) | Target-origin → key JSON for protected endpoints; injected into the bench subprocess, redacted from every artifact |
| `BENCH_TARGET_SUGGESTIONS` | ConfigMap ← `bench.suggestions` (only when non-empty) | Launcher combobox entries (advisory only) |
| `BENCH_PROM_URL` | ConfigMap ← `bench.prometheusUrl` (only when non-empty) | The GPU-join Prometheus URL (operator-fixed; the user picks only the label selector) |
| `BENCH_MAX_CONCURRENT_RUNS` | `extraEnv` (default 2) | Deployment-wide cap on in-flight benchmark runs (429 beyond it) |
| `BENCH_MAX_RUN_SECONDS` | `extraEnv` (default 7200) | Hard stop for a single run |
| `BENCH_CATALOG_PATH` | `extraEnv` | Override the seed-catalog location (default: env → repo discovery → image snapshot) |
| `HF_TOKEN` | `extraEnv` | HuggingFace token for `config.json` fetches of gated model repos |
| `http_proxy`/…/`no_proxy` | ConfigMap ← `proxy.*` (per-key) | Egress proxying |
| `BENCH_WORK_DIR` | set by the container command (`--work-dir /data`) | Run-artifact root — the PVC mount |
| `BENCH_SEED_RESULTS_DIR` | baked into the image (`/app/results`) | First-launch copy of the committed `results/` tree onto the PVC; the PVC copy is then authoritative and survives image upgrades |
| `BENCH_RESULTS_DIR` | app default (under the work dir) | Where the results library reads committed baselines |

## Authentication and SSO

Two independent layers; both are on by default.

**1. The app's API-key gate** (always active — the chart always wires a
web key). It protects the surface that starts or mutates benchmark work:

- **Gated** (X-API-Key / Bearer / the `mb-key` browser cookie, else 401 →
  the browser is redirected to the /unlock page): the launcher pages
  `/benchmark/chat`, `/benchmark/endpoint`; `POST /api/runs` (launch);
  `POST /api/runs/{id}/cancel`; `GET /api/targets`; `POST /api/models`
  (the target model probe); `POST /api/results/hidden`.
- **Public by design** (reports are the point — share links without handing
  out the launch key): the memory estimator page and API, `/results` and
  every read of past runs (status, logs, artifact downloads), the compare
  and atom-browser APIs, the measurement store, `/healthz`, `/readyz`,
  `/unlock`, `/api/gpus`, `/api/catalog`, `/api/models/search`.

Key mechanics (the K8S-MCP fleet pattern): the key list is **re-read on
every request** — rotating the Secret's `BENCH_API_KEYS` with a
comma-separated old+new value gives zero-downtime rotation; matching is
constant-time; a pasted `Bearer ` prefix is stripped;
`X-Auth-Request-Access-Token` (the token a gateway SSO proxy forwards) is
accepted as a key *envelope* (it is compared against the configured key
list, not trusted). With no key configured at all the gate opens with a
loud startup warning — that is the local-dev posture; the chart never ships
in that state.

**2. Gateway SSO on customer clusters** (`ezua.authorizationPolicy`).
PCA customer clusters expose an oauth2-proxy **CUSTOM** action provider;
enabling this block renders an AuthorizationPolicy on the app's gateway
route so unauthenticated BROWSER traffic is bounced through the unit's
IdP before it reaches the app. The app itself stays behind its key gate —
SSO is the user-experience layer in front, not a replacement for it.
Internally (G2) leave it off; there is no customer IdP chain.

The package also ships the fleet's browser-SSO module
(`src/model_benchmarker/utils/oidc_sso.py`, the D22 authorization-code
flow: `/oauth/login` + `/oauth/oidc/callback`, a 12h HttpOnly cookie,
stateless across replicas, in-cluster IdP discovery). **It is bundled but
not wired into this web app's routes** — it is inert by default and has no
effect on a deployed ModelBenchmarker. The deployment's real SSO surface is
the gateway policy above; the module documents where a future in-app SSO
would attach.

## Storage, and what survives what

`/data` (the PVC) holds `runs/<run-id>/` (one directory per benchmark:
`run_meta.json`, `run.log`, plus the run's artifacts), the committed
`results/` library (seeded from the image on first launch), and the
measurement store (`atoms.db`, the MB-DB atom browser backend).
Consequences:

- **Image/chart upgrade** — nothing lost; the PVC is untouched, and the
  chart-generated keys are preserved by lookup on upgrade.
- **Pod restart** — nothing lost; the run registry re-reads run directories
  on startup (in-flight runs are not resumable mid-run — the registry
  marks them interrupted).
- **`helm uninstall`** — run history SURVIVES (the PVC carries
  `helm.sh/resource-policy: keep`), but the **Secrets do not** — the next
  install generates a fresh web key, so old browser cookies and API callers
  must be re-keyed. Delete the PVC explicitly to wipe history.
- **`persistence.enabled: false`** — everything above is lost on pod
  replacement (emptyDir); acceptable only for throwaway dev deploys.

## Developer and secondary paths (plain Helm — not the PCAI path)

Labelled clearly: these are developer workstations and local clusters, not
how PCAI deployments are operated.

```bash
# Render-check without touching a cluster:
helm template mb helm/ -f helm/values-examples/values.hosted-trial.yaml | less

# Local cluster install/upgrade:
helm upgrade --install mb helm/ -n model-benchmarker -f <values-file>
```

**The ${DOMAIN_NAME} gotcha (CLI only):** PCAI substitutes the placeholder in
pasted values before rendering. A raw `helm template` or `helm install`
passes the literal `model-benchmarker.${DOMAIN_NAME}` into the VirtualService,
and istio's webhook DENIES it — the release fails even though every other
object is valid. Resolve it first
(`sed "s/${DOMAIN_NAME}/<your-domain>/" values.yaml`) or use a literal host in a
local values file.

Locally, the same app runs without any of the above:
`PYTHONPATH=src python -m model_benchmarker.webapp --port 8000`
(estimator open, benchmark pages gated-open with a loud warning until you
set `BENCH_API_KEYS`).
