# Verification — pcai-llm-gateway

After a PCAI apply (or a `helm template` render + install), verify in this
order: health → admin bootstrap → a proxied model call → the MCP surface →
any newer feature you rely on (§7). Replace `<endpoint>` with your
`ezua.virtualService.endpoint` and
`REPLACE_WITH_*` with your real values — never commit filled-in commands.

## 1. Health and readiness

```bash
# Through the PCAI gateway (the path consumers use):
curl -s https://pcai-llm-gateway.<your-domain>/health
# -> {"status":"ok",...}

curl -s https://pcai-llm-gateway.<your-domain>/ready
# -> 200 = Postgres reachable; 503 = the gateway is up but its ledger is not.

# In-cluster (bypasses the gateway):
curl -s http://pcai-llm-gateway-service.<namespace>.svc.cluster.local:9200/ready
```

`/health` is the liveness probe (process is up); `/ready` is the readiness
probe and checks the Postgres connection — a failing `/ready` takes the pod
out of the Service (fail closed, never serve keyless).

## 2. Become the admin (get the admin key out of the Secret)

The admin key unlocks the **admin console at `GET /admin`** (open it in a
browser — it is the key/budget/endpoint/MCP management UI) **and** the
`/admin/*` API; the chat page at `/` is the key-holder surface. The curl
flows below are the automation path — the console does the same through the
browser. The admin key is whatever
was written into the Secret at deploy time:

```bash
kubectl -n <namespace> get secret pcai-llm-gateway-admin \
  -o jsonpath='{.data.admin-api-keys}' | base64 -d; echo
```

Lost it? Re-create the Secret with a new key and RESTART the gateway pod
(env from Secrets is read once at process start):

```bash
kubectl -n <namespace> create secret generic pcai-llm-gateway-admin \
  --from-literal="admin-api-keys=$(openssl rand -hex 32)" \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl -n <namespace> rollout restart deploy/pcai-llm-gateway
```

Then every `/admin` call is `Authorization: Bearer <that-key>`.
(A bare `GET /admin` in a browser returns the console; without an admin key
the API sub-paths stay auth-gated — the console prompts for the key. Use
**https://** — a bare hostname lets the browser fall back to http, which
shows "Not Secure" and must never carry a bearer key.)

## 3. Admin bootstrap (admin key from the operator Secret)

Payload shapes below match the real request models (routes_admin.py) —
earlier drafts of this section 422'd on every step (audit P1-3).

```bash
export BASE=https://pcai-llm-gateway.<your-domain>
export ADMIN_KEY=REPLACE_WITH_ADMIN_KEY   # from pcai-llm-gateway-admin Secret
AUTH="Authorization: Bearer $ADMIN_KEY"
CT='Content-Type: application/json'

# 1) tenant -> user -> key (plaintext shown ONCE — save it):
TENANT=$(curl -s $BASE/admin/tenants -H "$AUTH" -H "$CT" -d '{"name":"team-alpha"}' | jq -r .id)
USER=$(curl -s $BASE/admin/tenants/$TENANT/users -H "$AUTH" -H "$CT" -d '{"name":"ci-bot"}' | jq -r .id)
# Optional at creation: "expires_at": "<ISO-8601 timestamp>" — an expired key
# fails auth with 401 and shows an "expired" pill in the console's keys table.
curl -s $BASE/admin/keys -H "$AUTH" -H "$CT" -d '{
  "user_id": "'$USER'", "name": "ci-key",
  "scopes": ["proxy","mcp"], "rate_limit_rpm": 60, "rate_limit_tpm": 100000 }'
# -> {"id":"<key_id>","key":"hllm-<PLAINTEXT>","key_prefix":"hllm-xxxxxx"}

# 2) register a model endpoint pool (REPLACE semantics per model — endpoints
#    not in the payload are pruned; in-cluster vLLM via svc.cluster.local):
curl -s $BASE/admin/models -H "$AUTH" -H "$CT" -d '{
  "model_name": "llama-8b",
  "endpoints": [{"base_url": "http://vllm-svc.models.svc.cluster.local/v1",
                 "priority": 0, "weight": 1, "api_key_secret": "vllm-upstream-key"}] }'
# -> ["llama-8b"]   (live router pool names post-refresh)

# 3) give the key a monthly budget (REPLACE-all per key, one transaction):
KEY_ID=<from step 1>
curl -s $BASE/admin/keys/$KEY_ID/budgets -H "$AUTH" -H "$CT" -d '{
  "budgets": [{"model": "llama-8b", "period": "month", "limit_tokens": 1000000}] }'
```

## 4. The proxy surface (/v1)

```bash
export KEY=hllm-REPLACE_WITH_RETURNED_PLAINTEXT

# Model catalog (virtual names only — upstreams never leak):
curl -s https://pcai-llm-gateway.<your-domain>/v1/models \
  -H "Authorization: Bearer $KEY"

# A chat completion through the pool:
curl -s https://pcai-llm-gateway.<your-domain>/v1/chat/completions \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"model":"llama-8b","messages":[{"role":"user","content":"ping"}],"max_tokens":16}'

# Negative checks (fail closed by design):
curl -s -o /dev/null -w '%{http_code}\n' \
  https://pcai-llm-gateway.<your-domain>/v1/models                      # -> 401 (no key)
curl -s -o /dev/null -w '%{http_code}\n' \
  https://pcai-llm-gateway.<your-domain>/v1/models -H "Authorization: Bearer bogus"  # -> 401
```

## 5. Check the ledger (/admin/usage)

```bash
curl -s https://pcai-llm-gateway.<your-domain>/admin/usage \
  -H "Authorization: Bearer $ADMIN_KEY"
# -> the last 100 ledger ROWS (one per request outcome) with the actor join:
#    key_name/key_prefix/user_name/tenant_name per row.
```

Expect one ledger row per request from step 3 (status `ok`), token counts
matching the upstream response's `usage`. The explorer is filterable and
exportable (`?key_id=&model=&status=&since=&until=&group_by=key|model|tenant|day&format=csv`);
rollups live at `/admin/usage/summary`, and per-key budget status
(limit vs spent, both kinds) rides every row of `GET /admin/keys`.

## 6. The MCP surface (/mcp)

```bash
# Tool catalog, filtered by the key's per-tool ACLs (default-DENY — an ACL
# row must exist per (key, server, tool) before a tool is callable):
curl -s https://pcai-llm-gateway.<your-domain>/mcp \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

An empty or absent tool list is the **default-DENY working as designed**
until an operator grants tools to the key — in the console (**Keys ▸ MCP**
per-key grant editor, or **Profiles ▸ Default MCP grants** to preset grants
for every key that attaches the profile) or via the ACL API:

```bash
curl -s -X PUT $BASE/admin/keys/$KEY_ID/mcp-grants \
  -H "Authorization: Bearer $ADMIN_KEY" -H 'Content-Type: application/json' \
  -d '{"grants": [{"server": "<server-name>", "tool": "<tool-name>"}]}'
# -> {"key_id": "...", "grants": 1}   (replace-all per key, one transaction)
```

Then re-list the catalog — the granted tools appear (grant changes are live;
keys are read per-request, no restart).

## 7. Feature checks (the newer feature waves)

One smoke per newer surface — run the ones you plan to use after any
upgrade (§3's key/model/budget remain the base).

```bash
export KEY_ID=<key id from §3>      # a key with proxy+mcp scopes
export KEY=hllm-REPLACE_WITH_PLAINTEXT
```

**a. Relay + trace waterfall** (agent traces + relay events):

```bash
# Relay a chat with tools (needs granted MCP tools — §6):
curl -sN $BASE/v1/chat/completions -H "Authorization: Bearer $KEY" \
  -H 'Content-Type: application/json' -H 'X-PCAI-Execute-Tools: 1' \
  -d '{"model":"llama-8b","messages":[{"role":"user","content":"use the search tool to find anything about the gateway"}]}'
# -> SSE: data: {"pcai": {"event": "thinking"|"iteration"|"tool_call"|"model_usage"|"guard", ...}}
#    tool_call events carry args + a truncated result preview; the response
#    header X-PCAI-Relay: streaming marks the mode.

curl -s "$BASE/admin/traces?limit=5" -H "Authorization: Bearer $ADMIN_KEY" | jq '.[0]'
# -> one card per relay request: steps[] (phase chat|tool with ms + tokens)
#    and totals {steps, tool_ms, chat_ms, tool_calls}.
```

**b. Per-key relay default** (relay WITHOUT any client header):

```bash
curl -s -X PATCH $BASE/admin/keys/$KEY_ID -H "Authorization: Bearer $ADMIN_KEY" \
  -H 'Content-Type: application/json' -d '{"relay_default": true}'
# Then the same chat WITHOUT -H 'X-PCAI-Execute-Tools: 1' -> relay engages.
# '0' in the header opts back out; the console does the same via Keys ▸ edit.
```

**c. Key edit in place** (PATCH — no reissue, live immediately):

```bash
curl -s -X PATCH $BASE/admin/keys/$KEY_ID -H "Authorization: Bearer $ADMIN_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"rate_limit_rpm": 120, "max_concurrent": 4, "expires_at": null}'
# -> the updated row; explicit null CLEARS expiry (omitted fields untouched).
```

**d. Mint → redeem (self-service enrollment):**

```bash
CODE=$(curl -s $BASE/admin/enrollments -H "$AUTH" -H "$CT" \
  -d '{"label":"onboarding","tenant_id":"'$TENANT'","profile_id":null,"max_uses":5}' | jq -r .code)
curl -s $BASE/v1/enroll -H 'Content-Type: application/json' \
  -d '{"code":"'$CODE'","name":"new-user"}'
# -> {"key": "hllm-...", "user": "new-user", ..., "memory": {"provisioned": false}}
#    plaintext shown once; a profile-bound code stamps the profile's budgets
#    + MCP grants on. "memory" always rides the response (see below).
```

**d2. Enrollment memory opt-in** (only where a `mm-rag` MCP server is
registered and `PCAI_LLM_SECRETS_MASTER_KEY` is set):

```bash
curl -s $BASE/v1/enroll -H "$CT" \
  -d '{"code":"'$CODE'","name":"memory-user","memory_opt_in":true}' | jq .memory
# -> {"provisioned": true, "dataset": "mem-<tenant>-memory-user-<key6>",
#     "password": "<shown once>"} — the dataset exists on the RAG REST API
#    and the key's relay calls to mm-rag now carry the pair automatically
#    (verify: PUT-free — GET /admin/keys/$KEY_ID/mcp-creds lists the
#    header NAMES; values never return). Omitting memory_opt_in falls back
#    to the profile preset then PCAI_LLM_ENROLL_MEMORY_DEFAULT (false).
#    Failure shapes: {"provisioned": false, "error": "..."} with the key
#    STILL issued — check the registry row exists + the mcpKeySecret env.
```

**e. Off-peak status** (only where `offpeakEnabled: true` + the Prometheus
MCP is registered):

```bash
curl -s $BASE/admin/offpeak -H "Authorization: Bearer $ADMIN_KEY"
# -> {enabled, active, fraction, enter_pct, exit_pct, last_value_pct, last_check, last_error, ...}
# Console: Admin ▸ Off-peak (auto-refreshes while the tab is visible).
```

**f. Endpoint health + probe:**

```bash
curl -s $BASE/admin/endpoints -H "Authorization: Bearer $ADMIN_KEY" | jq '.[0]'
curl -s -X POST $BASE/admin/models/llama-8b/probe -H "Authorization: Bearer $ADMIN_KEY"
# -> breaker state (state/consecutive_failures/cooldown_remaining_s/last_error)
#    and a 5s live probe of the endpoint the router would pick.
```

**g. Per-key MCP credential overrides** (console Keys ▸ `creds`, or):

```bash
curl -s -X PUT $BASE/admin/keys/$KEY_ID/mcp-creds -H "Authorization: Bearer $ADMIN_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"server":"mm-rag","headers":{"X-Memory-Dataset":"alice-memory","X-Dataset-Password":"<pw>"}}'
# -> {"header_names": ["X-Dataset-Password", "X-Memory-Dataset"]} — values are
#    write-only and never return; they merge into /mcp + relay calls last-wins.
```

**h. Admin audit trail + rotation carry-over** (post-upgrade, any site):

```bash
curl -s "$BASE/admin/audit?limit=5" -H "Authorization: Bearer $ADMIN_KEY" | jq '.[0]'
# -> {id, ts, actor_key_id, action, resource_type, resource_id, details, ...}
#    one row per admin mutation, append-only; console: Admin ▸ Audit.
curl -s -X POST $BASE/admin/keys/$KEY_ID/rotate -H "Authorization: Bearer $ADMIN_KEY" | jq '{grants_copied, creds_copied}'
# -> a key with grants + memory creds must report BOTH non-zero (rotation
#    carries federation grants + per-server credential overrides).
```

**i. Per-key stream events** (strict validating clients — relayed streams):

```bash
# Flip the key's default OFF, then relay: the stream carries NO {"pcai": ...}
# events — only spec chunks + [DONE] (safe for opencode / openai-py ≥2.45):
curl -s -X PATCH $BASE/admin/keys/$KEY_ID -H "Authorization: Bearer $ADMIN_KEY" \
  -H 'Content-Type: application/json' -d '{"stream_pcai_events": false}'
# Per-request override without touching the key: -H 'X-PCAI-Stream-Events: 0'
# ('1' forces them back on for this stream; absent header = key default).
```

**j. Custom metrics** (only where `metrics.enabled: true`):

```bash
curl -s https://pcai-llm-gateway.<your-domain>/metrics | grep -E '^gateway_'
# -> gateway_requests_total{model,status,kind}, gateway_request_latency_seconds,
#    gateway_ttft_seconds, gateway_budget_rejections_total,
#    gateway_rate_limited_total, gateway_breaker_open, gateway_relay_iterations,
#    gateway_mcp_acl_denied_total, gateway_budget_warning_total
# Scraped via the ServiceMonitor; alert rules ship when
# metrics.prometheusRule.enabled=true (default ruleset: error ratio,
# breaker-open, budget-rejection spike).
```

**k. Framework-overhead bench** (off-box dev measurement — not a deployment check):

```bash
# In-process ASGI benchmark: pure per-request framework overhead, four
# stacks side by side (no TLS, no sockets, no DB — /health only; real
# end-to-end numbers come from gateway_request_latency_seconds above).
.venv/bin/python scripts/bench_overhead.py --duration 3 --concurrency 16
# Reference numbers (dev box, 2026-09, stable across runs):
#   raw-asgi       p50 ≈  0.5 µs   (the event-loop floor)
#   starlette-bare p50 ≈    8 µs
#   fastapi        p50 ≈   30 µs
#   gateway        p50 ≈  100 µs   p99 ≈ 127 µs   (~9.3k req/s per core)
# The ~200x gateway-vs-raw ratio is honest but the ABSOLUTE framing
# matters: 100 µs CPU/request against a ~1.2 ms loopback transport hop
# (--mode gateway, real uvicorn + keep-alive sockets, needs reachable
# Postgres) and multi-100-ms upstream TTFT.
```

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Pod in `CreateContainerConfigError` | A referenced Secret is missing (admin key always; `pcai-llm-gateway-db` in external mode; the PG auth Secret in embedded mode). | Create them (commands in [`DEPLOYMENT.md`](DEPLOYMENT.md) / NOTES.txt) or flip the `*.create: true` flags and re-apply — then re-apply the release. |
| `/ready` returns 503, pod NotReady | Postgres unreachable or credentials wrong. | Check the `database-url` value in the Secret (host/port/db/user); verify the DB allows connections from the cluster. |
| Startup log lacks `database connected and schema ensured` | Schema application failed — usually missing CREATE privilege on the database. | Grant CREATE on the target database; the gateway applies its schema at startup and needs it. |
| `/v1/models` returns an empty list | No endpoint pools registered. | Register endpoints via `/admin/models` (step 2). |
| All calls to one model return 503-ish errors | Every endpoint in the pool is in circuit-breaker cooldown. | Fix the upstream endpoints; the breaker retries after `endpointCooldownS` (default 60 s). |
| 401 on `/v1` or `/mcp` with a real key | Key revoked/expired (the console keys table shows an **expired** pill; expiry is set at creation via `expires_at`), or the plaintext was regenerated. | Create a new key via `/admin` (rotation = create → migrate → revoke). |
| 403 on `/v1` calls with a valid key | Key lacks the `proxy` scope or the model is not in `allowed_models`. | Recreate/adjust the key via `/admin/keys`. |
| EZUA console shows "Unknown" instead of "Ready" | The Kyverno vendor-label policy isn't installed (cluster-scoped, additive — off by default). | Set `kyverno.enabled: true` and re-apply; the chart also stamps the `hpe-ezua/*` labels statically so the upgrade roll fixes existing pods. |
| MCP tools/list empty | Per-tool ACLs are default-DENY and none were granted for this key. | Grant tools in the console (**Keys ▸ MCP**, or **Profiles ▸ Default MCP grants** to preset them) or `PUT /admin/keys/{key_id}/mcp-grants`. |
| Gateway host matches nothing (route "vanished") | Endpoint blanked or edited to an empty/host-mismatched string (an unresolved `${DOMAIN_NAME}` on a plain-Helm site registers a host that matches nothing). | Keep `${DOMAIN_NAME}` when pasting into PCAI (it envsubsts pasted values before rendering); plain `helm -f` sites write the literal domain in `ezua.virtualService.endpoint`. |
| 413 on proxied calls | Body exceeds `maxBodyBytes` (default 32 MiB). | Raise `settings.maxBodyBytes` in values and re-apply. |
| 503 `{"error": "database busy..."}` | Postgres pool saturated (bounded acquire timed out — fail-fast by design). | Raise `settings.dbPoolMax` (mind the shared Postgres' `max_connections`), then pod limits, then `replicaCount` — see [`DEPLOYMENT.md`](DEPLOYMENT.md) § Sizing. |
| Relay chat returns 400 "tool execution requested but this key has no MCP grants" | The relay pre-flight found no grants and the key is not mcp-scoped (in `scope` grant mode). | Grant tools (console **Keys ▸ MCP**, or Profiles ▸ Default MCP grants) or `PUT /admin/keys/{key_id}/mcp-grants` — then retry. |
