# k8s-mcp-2-0-server

A read-only **Kubernetes ops MCP server** migrated to **MCP 2.0** (protocol revision
`2026-07-28`) using the **official MCP Python SDK v2** (`mcp>=2.0.0`).

> **Deploying?** See [DEPLOYMENT.md](DEPLOYMENT.md) — build, one-command install,
> MCP client setup, namespace policy, and enabling exec per namespace.

This is the MCP 2.0 successor to `k8s-mcp-server`. It exposes a read-only
tool surface (pods, workloads, events, logs, services, ConfigMaps, Secrets,
PVCs, CRDs, RBAC, and a generic `kubectl` escape hatch) while speaking the new
stateless protocol.

## MCP 2.0 — what changed (protocol `2026-07-28`)

The headline change is that **MCP is now stateless at the protocol layer**:

- **No handshake, no session.** The `initialize`/`initialized` exchange and the
  `Mcp-Session-Id` header are gone. Every request is self-contained: protocol
  version, client info, and capabilities travel in `_meta`. A new `server/discover`
  RPC lets a client fetch capabilities up front when it needs them.
- **Header-based routing.** Streamable HTTP requests carry `Mcp-Method` and
  `Mcp-Name` headers so gateways/WAFs/rate-limiters can route without parsing the
  JSON body.
- **Multi Round-Trip Requests (MRTR).** Server-to-client asks (sampling, elicitation,
  roots) that previously held open a stream are replaced by an `InputRequiredResult`
  the client answers and retries. Not used here — this server is pure request/response.
- **Cacheable list results.** `tools/list`, `prompts/list`, and `resources/*` responses
  may carry `ttlMs` and `cacheScope`.
- **JSON Schema 2020-12** for tool schemas; missing-resource error is now the standard
  JSON-RPC `-32602` instead of the custom `-32002`.
- **Deprecations.** Roots, Sampling, Logging, and the legacy HTTP+SSE transport are
  deprecated (≥ 12-month offramp). This server does not use any of them.
- **Extensions.** Tasks and MCP Apps are now formal extensions.

### Practical effect for this server

Because this server is **read-only with no cross-call state**, it is naturally
stateless: any request can land on any replica behind a plain round-robin load
balancer — no sticky sessions, no shared session store.

### Verified MCP 2.0 conformance (live endpoint probe)

Verified against the real wrapped app served by uvicorn (kubernetes stubbed):

| Behavior | Result |
| --- | --- |
| Stateless `tools/list` with per-request envelope | 200, all 17 tools |
| Cacheable list result | `ttlMs=300000, cacheScope=public` on `tools/list` |
| Tool `inputSchema` | JSON Schema 2020-12 shape, properties from type hints |
| `server/discover` | `supportedVersions=["2026-07-28"]`, tools/prompts/resources capabilities |
| Stateless `tools/call` | executes with no handshake/session |
| Routing headers | `Mcp-Method`/`Mcp-Name` validated against the body |
| Strict envelope rejection | missing `clientCapabilities` → 400 `-32602` naming the key |
| Missing resource error | standard JSON-RPC `-32602` |
| Legacy era on same process | `initialize` (`2025-03-26`) honored; `tools/list`/`tools/call` served; `server/discover` correctly gated (`-32601`) |

What a conformant **modern** client must send on every request:

- `params._meta` carrying BOTH `io.modelcontextprotocol/protocolVersion: "2026-07-28"`
  AND `io.modelcontextprotocol/clientCapabilities` (client info SHOULD-include).
- Headers matching the envelope: `Mcp-Protocol-Version`, `Mcp-Method` (=
  body method) and `Mcp-Name` (= the named param for `tools/call`,
  `prompts/get`, `resources/read`).

Era routing is header-driven: a request with an `Mcp-Protocol-Version` header
outside the legacy handshake versions enters the modern classifier; a request
with no header is served by the 2025-era stateless path. The official SDK
clients do this automatically.

## Security model

The audit that shipped with v2.1.0 fixed the following; do not regress them:

1. **No shell.** kubectl is invoked as an argv list via
   `asyncio.create_subprocess_exec`. `shell=True` with string interpolation is
   what made the old denylist bypassable (`get pods && kubectl delete ns foo`,
   `;`, `` ` ``, `$()`, `>`, embedded newlines).
2. **Read-verb allowlist.** The escape hatch accepts only `get`, `describe`,
   `logs`, `top`, `explain`, `api-resources`, `api-versions`, `cluster-info`,
   `version`, `auth`, `events`. The old denylist missed write-capable commands
   (`run`, `exec`, `attach`, `cp`, `port-forward`, `proxy`, `debug`, `set`,
   `autoscale`, `certificate`, `plugin`...).
3. **Connection flags rejected.** `--server`, `--token`, `--kubeconfig`,
   `--client-certificate`, `--client-key`, `--username`, `--password`,
   `--certificate-authority`, `--insecure-skip-tls-verify` are refused — they
   could redirect the API connection (stealing the service-account token) or
   swap credentials.
4. **Parameter validation.** Namespace (DNS label), resource names, resource
   types, and output formats are regex-validated before reaching kubectl or
   the API.
5. **Least-privilege RBAC.** The manifest binds a custom read-only
   `ClusterRole` (`k8s-mcp-2-0-readonly`) — **not** `cluster-admin`. Secrets
   and RBAC objects are deliberately excluded, so `list_secrets` and
   `get secret ...` return 403: the "values are NOT shown" promise is now
   enforced by the API server, not by tool formatting. Extend the CRD
   `apiGroups` list per installed operator; never use `apiGroups: ["*"]`
   there (it would re-grant secrets read).
6. **Pod hardening.** Non-root (uid/gid 10001), read-only root filesystem,
   all capabilities dropped, RuntimeDefault seccomp, `/tmp` as emptyDir.
7. **Pinned toolchain.** kubectl is pinned via `KUBECTL_VERSION` and
   checksum-verified against the official `kubectl.sha256` at build time.
8. **API-key auth.** Every HTTP request must present `K8S_MCP_API_KEY` as
   `Authorization: Bearer <key>` or `X-API-Key: <key>`; comparison is
   constant-time (`hmac.compare_digest`) and rejected requests get 401 before
   the MCP app is reached. The key lives in Secret `k8s-mcp-2-0-apikey`,
   wired to the Deployment via `secretKeyRef`. When the env var is unset
   (local development), the endpoint is open and the server logs a loud
   warning at startup — never deploy that way.
9. **Endpoint auth (mesh, optional).** The Istio `RequestAuthentication` /
   `AuthorizationPolicy` examples in the manifest add real identity +
   revocation on top of the static key — a shared API key authenticates the
   *key*, not a user.

### API key authentication

The server requires a static API key on every request (see item 8 above).
Generating and wiring it into the cluster:

```bash
export API_KEY=$(openssl rand -hex 32)   # treat as a password
envsubst < k8s-mcp-2-0-server.yaml | kubectl apply -f -
```

MCP clients configure it as a plain HTTP header — most clients that support
URL-based MCP servers accept custom headers:

```json
{
  "mcpServers": {
    "k8s-ops": {
      "url": "https://k8s-hardened-mcp-server.pcai-se-ai-application.hst.rdlabs.hpecorp.net/mcp",
      "headers": { "Authorization": "Bearer <API_KEY>" }
    }
  }
}
```

`{"X-API-Key": "<API_KEY>"}` is accepted as an alternative header name.
Local development: `export K8S_MCP_API_KEY=dev-secret` before `python server.py`,
or leave it unset to run open (with a startup warning).

Key handling rules (it *is* a password):

- Generate with `openssl rand -hex 32`; never commit the value — the manifest
  only references `${API_KEY}` at apply time.
- Rotation: replace the Secret (`kubectl create secret generic
  k8s-mcp-2-0-apikey --from-literal=api-key=<new> --dry-run=client -o yaml |
  kubectl apply -f -`) and `kubectl rollout restart` the Deployment; update
  clients with the new value.
- The key gates *who can call the tools*, not *which namespaces* — combine
  with the namespace policy below.

### Namespace governance (optional whitelist/blacklist)

Configured with two environment variables (both optional, comma-separated,
fnmatch globs such as `team-*` supported):

| Variable | Effect |
| --- | --- |
| `K8S_MCP_ALLOWED_NAMESPACES` | Whitelist. Unset/empty = every namespace allowed. |
| `K8S_MCP_BLOCKED_NAMESPACES` | Blacklist. Always denied — wins over the whitelist. |

Semantics when a policy is active:

- Every tool that accepts a `namespace` parameter rejects denied namespaces
  with an explanatory error (the error lists the allowed namespaces).
- Python-API-backed tools (`cluster_health`, `list_pods`, `get_events`,
  `list_workloads`, `list_services`, `list_pvcs`, `list_namespaces`,
  `get_custom_resource`) **filter** denied namespaces from cluster-wide
  results.
- kubectl-backed tools enforce through the escape hatch: explicit `-n` is
  checked directly; namespaced queries without `-n` are rejected with a hint;
  `-A`/`--all-namespaces` is **rewritten** into one query per whitelisted
  namespace (glob patterns are expanded against the live namespace list, cap
  20) or **rejected** when only a blacklist is set — arbitrary kubectl output
  cannot be filtered reliably, so the server fails loud instead of guessing.
- Cluster-scoped resources (`nodes`, `crd`, `namespaces`, ...) are unaffected;
  `auth can-i` is context-only and not gated.
- `check_rbac` reports the truth again now that the SA is no longer
  cluster-admin.

Example (Deployment env):

```yaml
env:
- name: K8S_MCP_ALLOWED_NAMESPACES
  value: "project-user-francesco-caliva,team-a-*"
- name: K8S_MCP_BLOCKED_NAMESPACES
  value: "kube-system,kube-public"
```

### Per-user exec assignments (optional)

Exec namespaces can be scoped **per key** so users cannot widen their own
scope. `K8S_MCP_CLIENTS` maps keys to assignments
(`name:key[:exec-ns-patterns];…`; a missing third field inherits the
deployment ceiling):

```yaml
- name: K8S_MCP_CLIENTS
  value: "alice:<alice-key>:debug-*,team-a;bob:<bob-key>:team-b"
```

- Each client connects with their own key — no extra headers needed. The
  shared `K8S_MCP_API_KEY` keeps working (deployment-wide ceiling).
- The optional `X-Exec-Namespaces` request header **narrows** a request's
  exec scope (intersected with the key's assignment); it can never widen it.
  Invalid patterns in the header → 400.
- RBAC provisioning covers the **union** of the deployment list and all
  client assignments; the per-request slicing is the app layer's job. Every
  exec AUDIT line names the calling client.

### kubectl exec (opt-in, hardened)

`exec` is arbitrary code execution inside a workload container — it can never
be made read-only (`cat` in a container reads every secret that container
mounts). The design goal is therefore: keep the debugging power, shrink the
blast radius with **independent layers**, each enforceable on its own:

| Layer | Mechanism | Enforced by |
| --- | --- | --- |
| 1. Opt-in | Tool not registered unless `K8S_MCP_EXEC_ENABLED=true` | server (models cannot call unregistered tools) |
| 2. RBAC | `pods/exec` `create` granted only by **namespaced** `Role`s (never a ClusterRole) | API server — the real boundary |
| 3. Per-pod opt-in | Target pod must carry label `k8s-mcp.io/exec: "true"` | server (RBAC cannot match labels; the app can) |
| 4. Argv-typed tool | `command: list[str]`, one command, passed after kubectl's `--` | server + kubectl flag parsing |
| 5. Binary allowlist | Basename of `command[0]` ∈ read-only set (`ps`, `ls`, `cat`, `tail`, `head`, `grep`, `df`, `du`, `free`, `ss`, `netstat`, `ip`, `stat`, `id`, ...) | server |
| 6. Hard denies | Shells, interpreters, `su`/`sudo`, `nsenter`/`unshare` — never runnable, even when allowlisted; `find -exec` and `ip netns` argument tokens rejected | server |
| 7. Self-pod guard | Exec into the MCP server's own pod refused (its SA token is the crown jewels) | server |
| 8. Bounded + audited | No TTY/stdin, 30s timeout, 50k output truncation, `AUDIT exec decision=…` log line for every allow **and** deny | server / log collection |

Enable:

```yaml
env:
- name: K8S_MCP_EXEC_ENABLED
  value: "true"
# optional: extra binaries beyond the built-in read-only set
- name: K8S_MCP_EXEC_ALLOWED_COMMANDS
  value: "my-debug-tool"
# optional: set "false" to drop the label requirement (discouraged)
- name: K8S_MCP_EXEC_REQUIRE_LABEL
  value: "true"
```

Opt a workload in:

```yaml
metadata:
  labels:
    k8s-mcp.io/exec: "true"
```

**One list for exec namespaces.** `K8S_MCP_EXEC_NAMESPACES` is the single
exec allowlist (comma-separated, globs, same syntax as the general policy):

```yaml
- name: K8S_MCP_EXEC_NAMESPACES
  value: "debug-*,team-a,project-user-francesco-caliva"
```

Unset = exec simply follows the general namespace policy. When set, exec is
further narrowed to these namespaces — intersected with the general policy
(a general blacklist always wins).

**RBAC follows the list automatically.** With exec enabled and the list set,
the server provisions its own `pods/exec` RoleBindings for those namespaces
at startup (`K8S_MCP_EXEC_AUTO_RBAC=true`, the default). The provisioner is
escalation-proof by construction: it may reference **only** the
`k8s-mcp-pods-exec` template role (`bind` is `resourceNames`-restricted), it
cannot create or modify role content, never creates ClusterRoleBindings, and
skips blocked or nonexistent namespaces. Set `K8S_MCP_EXEC_AUTO_RBAC=false`
to bind manually instead (loop in DEPLOYMENT.md). Direction of failure is
safe both ways: list without binding → 403 from the API server; binding
without list → denied by the app layer. The pod label gate still applies.

What the layers deliberately do **not** prevent (inherent to exec):

- An allowlisted binary still runs with the container's privileges and can
  read anything the container can (`cat /var/run/secrets/...`). Treat every
  exec-able pod as readable-by-the-key-holder. Use the label to make that a
  per-workload decision.
- `env` and `curl` are excluded from the default allowlist for a reason (they
  dump in-container secrets / provide an exfil channel); adding them via
  `K8S_MCP_EXEC_ALLOWED_COMMANDS` is an explicit operator decision.
- The generic `run_kubectl` escape hatch NEVER gains `exec` — container exec
  goes only through the dedicated tool where the layers above apply.

If you need stronger isolation than this stack provides: run a **second,
separate deployment** (own ServiceAccount whose only permission is
`pods/exec` in debug namespaces, own API key) so a leaked read key never
carries exec; prefer **ephemeral debug containers** (`kubectl debug`) over
entering the app container where the debugging goal allows it; and keep an
API-server audit policy recording `pods/exec` create calls.

### Limitations (honest boundaries)

- Namespace governance is an application-layer control; the authoritative
  namespace boundary is RBAC. For hard per-namespace isolation, replace the
  ClusterRole with per-namespace `Role`s bound in the allowed namespaces.
- With a blacklist-only policy, cluster-scoped listings could still reveal
  the *existence* of blacklisted namespaces (names only, no content);
  `list_namespaces` filters them, raw kubectl does not.
- Unbounded `--all-namespaces` YAML is still buffered in memory before the
  50 000-character truncation; size the pod memory limit accordingly.
- The API key is a static shared secret: it authenticates the *key*, not a
  user — no per-user identity, audit attribution, or revocation short of
  rotation. Layer the Istio JWT example when you need identity. Delivery via
  env var means the key is readable in the pod's environment; mounting it as
  a read-only file would harden further if your threat model requires it.

## Performance notes

- kubectl runs under `asyncio.create_subprocess_exec` and all Kubernetes
  Python client calls run under `asyncio.to_thread` — neither blocks the
  event loop, so concurrent tool calls no longer serialize.
- `cluster_health` and `list_workloads` issue their independent API calls
  concurrently via `asyncio.gather`.
- `get_custom_resource` uses the dynamic client (no kubectl subprocess) and
  only falls back to kubectl on discovery/schema errors; the previous
  always-fallback dead code is gone.
- Every kubectl call is bounded by a 30 s asyncio timeout — a hung call can
  no longer stall the worker.

## SDK v1 → v2 migration applied

| v1 | v2 |
| --- | --- |
| `from mcp.server.fastmcp import FastMCP` (module removed) | `from mcp.server.mcpserver import MCPServer` |
| `FastMCP("k8s-ops-server")` | `MCPServer("k8s-ops-server", title=..., description=..., version=...)` |
| `mcp.run(transport="streamable-http", host=..., port=...)` | same call, but transport params live on `run()` (constructor no longer takes them) |
| `@mcp.tool()` | unchanged |

### One SDK, both protocol eras

`MCPServer` (SDK v2) speaks `2026-07-28` **and** still answers 2025-era clients
(`initialize` handshake) from the same process with nothing to configure. Modern
2026-era traffic is always stateless; `stateless_http=True` additionally serves
legacy clients without a shared session store.

## Requirements

```
mcp>=2.0.0,<3
kubernetes>=31.0.0
```

The SDK v2 no longer bundles `httpx` (it uses `httpx2`). This server does not
import `httpx` directly, so no extra dependency is needed.

## Build & run

```bash
# build (kubectl version pinned + checksum-verified in the Dockerfile)
docker buildx build -t ghcr.io/ai-solution-eng/k8s-mcp:v0.1.3 . --push

# local run (must have a valid kubeconfig)
export K8S_MCP_API_KEY=dev-secret   # unset = endpoint open, with a startup warning
pip install -r requirements.txt
python server.py            # serves streamable-http on 0.0.0.0:9090

# policy checks (no cluster needed; stubs the kubernetes package if missing)
python3 test_namespace_policy.py
```

In-cluster it uses the pod's service-account token/CA automatically (falls back to
local kubeconfig for development). The `kubectl` escape hatch and the
`auth can-i` RBAC tool run kubectl as an argv list — no shell.

## Notes

- Deployment manifests for this image live in `k8s-mcp-2-0-server.yaml` in this
  repo; the Deployment now carries the read-only ClusterRole, pod hardening,
  and the namespace-policy env vars described above.
- CORS/Starlette mounting: this build uses the simple `mcp.run(...)` entrypoint. The
  older `expose_headers=["Mcp-Session-Id"]` is a v1 concern and is intentionally
  omitted for the stateless 2.0 server.
