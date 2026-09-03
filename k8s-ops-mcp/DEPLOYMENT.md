# Deploying k8s-mcp-2-0-server

A read-only Kubernetes ops MCP server (MCP 2.0 / protocol `2026-07-28`) with
API-key auth, optional namespace governance, and an opt-in hardened exec tool.

## Prerequisites

- `kubectl` configured against your cluster
- `docker` (to build/push the image)
- A gateway/Ingress for external exposure (the shipped manifest uses an Istio
  VirtualService on `ezaf-gateway`)

## 1. Build & push

```bash
# Dockerfile lives at the repo root — no -f needed (buildx auto-detects it).
docker login ghcr.io   # once, if not already logged in
docker buildx build -t ghcr.io/ai-solution-eng/k8s-mcp:v0.1.3 . --push
```

kubectl is pinned + checksum-verified inside the Dockerfile — no extra steps.
`TARGETARCH` is picked up automatically, so `--platform linux/amd64` (or
multi-arch builds) fetch the matching kubectl binary.

### Private image registry (ghcr)

Packages pushed to ghcr.io **with a personal-access token are PRIVATE by
default** (packages pushed from CI with `GITHUB_TOKEN` inherit the repo's
visibility instead). Verify/flip visibility under
`github.com → <user/org> → Packages → k8s-mcp → Package settings`. A private
package needs a pull credential in the cluster:

1. **Get the key**: GitHub → Settings → Developer settings → Personal access
   tokens (classic) → generate a token with the **`read:packages`** scope.
   That PAT is the pull password (read-only; it cannot push).
2. **Create the pull secret** in the server's namespace:

   ```bash
   kubectl -n $NAMESPACE create secret docker-registry ghcr-pull \
     --docker-server=ghcr.io \
     --docker-username=<github-username> \
     --docker-password=<PAT-with-read:packages> \
     --docker-email=you@example.com
   ```

3. **Reference it**: uncomment `imagePullSecrets: [{name: ghcr-pull}]` in
   `k8s-mcp-2-0-server.yaml` and re-apply, then
   `kubectl -n $NAMESPACE rollout restart deploy/k8s-mcp-2-0-server`.
4. Symptom check: `ErrImagePull` / `401 Unauthorized` on ghcr.io in pod
   events means the secret is missing, wrong, or the PAT lacks
   `read:packages` — or the package is private and step 3 wasn't done.

This pull credential is unrelated to the MCP API key below: it authenticates
the **cluster against ghcr**, the API key authenticates **MCP clients against
the server**.

## 2. Deploy (one command + one secret)

```bash
export NAMESPACE=project-user-francesco-caliva
# The public hostname must be UNIQUE per deployment: two VirtualServices
# claiming the same host on the same gateway split traffic between their
# backends. Full FQDN, required.
export MCP_HOSTNAME=k8s-hardened-mcp-server.pcai-se-ai-application.hst.rdlabs.hpecorp.net
export DOMAIN_NAME=pcai-se-ai-application.hst.rdlabs.hpecorp.net

# The API key is managed OUT OF BAND (never via envsubst — an unset API_KEY
# there once silently emptied the secret and opened the endpoint):
kubectl -n $NAMESPACE create secret generic k8s-mcp-2-0-apikey \
  --from-literal="api-key=$(openssl rand -hex 32)" \
  --dry-run=client -o yaml | kubectl apply -f -

envsubst < k8s-mcp-2-0-server.yaml | kubectl apply -f -
```

The table below mentions the Secret — it is created by the command above, not
by the manifest apply.

What gets installed:

| Object | Purpose |
| --- | --- |
| Secret `k8s-mcp-2-0-apikey` | the API key (wired into the Deployment) |
| ServiceAccount `k8s-mcp-2-0-sa` | the server's identity |
| ClusterRole `k8s-mcp-2-0-readonly` (+binding) | read-only cluster access — **no secrets, no RBAC objects** |
| ClusterRole `k8s-mcp-pods-exec` | exec **template** — grants nothing until bound per namespace |
| ClusterRole `k8s-mcp-2-0-rbac-provisioner` (+binding) | lets the server bind the exec template itself (bind-scoped; cannot escalate) |
| Deployment `k8s-mcp-2-0-server` | non-root, read-only rootfs, dropped capabilities, seccomp |
| Service + VirtualService | exposure on `mcp-k8s-2-0-server.${DOMAIN_NAME}` |

All `K8S_MCP_*` knobs are envsubst-driven: you can export them at deploy time
(`K8S_MCP_ALLOWED_NAMESPACES`, `K8S_MCP_BLOCKED_NAMESPACES`,
`K8S_MCP_EXEC_ENABLED`, `K8S_MCP_EXEC_NAMESPACES`,
`K8S_MCP_EXEC_REQUIRE_LABEL`, `K8S_MCP_EXEC_AUTO_RBAC`, `K8S_MCP_CLIENTS`)
and they land in the Deployment on first apply; unset exports mean the
documented defaults (all namespaces readable, exec disabled). Changing them
later via `kubectl set env` works identically (it triggers a rollout, and
exec RBAC re-provisions at startup).

## 3. Verify

```bash
kubectl -n $NAMESPACE rollout status deploy/k8s-mcp-2-0-server
kubectl -n $NAMESPACE logs deploy/k8s-mcp-2-0-server | head -5
# → "Loaded In-Cluster Service Account Config" and NO API-key warning

# unauthenticated → 401
curl -o /dev/null -s -w '%{http_code}\n' https://mcp-k8s-2-0-server.$DOMAIN_NAME/mcp

# authenticated → served (a real MCP tools/list)
curl -s https://mcp-k8s-2-0-server.$DOMAIN_NAME/mcp \
  -H "Authorization: Bearer $API_KEY" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -H 'Mcp-Protocol-Version: 2026-07-28' \
  -H 'Mcp-Method: tools/list' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{"_meta":{"io.modelcontextprotocol/protocolVersion":"2026-07-28","io.modelcontextprotocol/clientCapabilities":{}}}}'
```

## 4. Connect an MCP client

Retrieve the API key from the cluster Secret (the value you exported as
`API_KEY` at apply time — same value either way):

```bash
kubectl -n $NAMESPACE get secret k8s-mcp-2-0-apikey \
  -o jsonpath='{.data.api-key}' | base64 -d
```

Then configure the client:

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

(`X-API-Key: <API_KEY>` is accepted as an alternative header.)

## 5. Namespace policy (read access)

By default the server can read every namespace. Narrow it on the Deployment:

```bash
kubectl -n $NAMESPACE set env deploy/k8s-mcp-2-0-server \
  K8S_MCP_ALLOWED_NAMESPACES="project-user-francesco-caliva,team-a-*"
# blacklist (always wins): K8S_MCP_BLOCKED_NAMESPACES="kube-system,kube-public"
```

The set env triggers a rollout automatically. Read tools filter cluster-wide
results to the allowed namespaces; kubectl-backed tools require `-n` and
rewrite/reject `-A` per the policy.

## 6. Exec in certain namespaces (opt-in)

Exec is **off by default** — the tool doesn't even exist for clients. Turning
it on is two variables, and the RBAC follows automatically:

```bash
kubectl -n $NAMESPACE set env deploy/k8s-mcp-2-0-server \
  K8S_MCP_EXEC_ENABLED=true \
  K8S_MCP_EXEC_NAMESPACES="debug-*,team-a"
```

On startup the server **provisions the pods/exec RoleBindings itself** for
every namespace on the list (globs expanded against the live namespaces) and
logs the summary:

```
RBAC provisioning: SA mcp-ns/k8s-mcp-2-0-sa bound to 'k8s-mcp-pods-exec' —
created: ['team-a']; already present: none; skipped: [('debug-*', 'no live namespaces matched')]; refused: none
```

**How the automatic RBAC stays safe** — the provisioner permission is
scoped so it cannot escalate:

- it may reference **only** the `k8s-mcp-pods-exec` template (`bind` is
  `resourceNames`-restricted) — never `cluster-admin` or any other role;
- it cannot create or modify role *content*;
- it never creates ClusterRoleBindings;
- it skips namespaces the general namespace policy blocks and namespaces that
  don't exist;
- it never touches an existing binding with a different roleRef.

The four gates that must agree before an exec succeeds: **exec list →
general namespace policy → RoleBinding (RBAC) → pod label
`k8s-mcp.io/exec: "true"`** (drop the label gate with
`K8S_MCP_EXEC_REQUIRE_LABEL=false` — discouraged).

Optional knobs:

```yaml
K8S_MCP_EXEC_ALLOWED_COMMANDS   # extra binaries beyond the built-in read-only set
K8S_MCP_EXEC_AUTO_RBAC=false    # disable auto-provisioning → bind manually (below)
```

### Per-user keys: exec scoping per person/team (optional)

Handing everyone the shared key makes the exec list an all-or-nothing
affair. Instead, issue **per-user keys with their own exec namespace
assignment** via `K8S_MCP_CLIENTS` (format `name:key[:exec-ns-patterns];…`,
entries `;`-separated, patterns comma-separated globs):

```yaml
env:
- name: K8S_MCP_CLIENTS
  value: "alice:<alice-key>:debug-*,team-a;bob:<bob-key>:team-b;ops:<ops-key>"
```

- `alice` can exec in `debug-*` and `team-a` only; `bob` only in `team-b`;
  `ops` (no third field) inherits the deployment-wide exec list.
- Users connect with their own key — no client-side configuration beyond the
  normal Authorization header, and **they cannot widen their own scope**
  (capabilities travel with the credential).
- The optional `X-Exec-Namespaces: team-a` request header lets a client
  further NARROW its own requests (self-restriction for specific MCP client
  profiles); it is intersected with the key's assignment and can never widen
  it. Invalid patterns → 400.
- The shared `K8S_MCP_API_KEY` (if still set) keeps working with the
  deployment-wide ceiling.
- Every exec AUDIT line records the calling client's name — per-user
  attribution without JWT infrastructure.
- RBAC provisioning binds the **union** of the deployment list and all client
  assignments (restart after changing the map).

### Manual RBAC fallback (`K8S_MCP_EXEC_AUTO_RBAC=false`)

Bind each namespace yourself — permissions stay namespaced:

```bash
SERVER_NS=$NAMESPACE
for NS in team-a debug-x; do
kubectl apply -n "$NS" -f - <<EOF
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata: {name: k8s-mcp-2-0-exec}
subjects:
- {kind: ServiceAccount, name: k8s-mcp-2-0-sa, namespace: "${SERVER_NS}"}
roleRef: {kind: ClusterRole, name: k8s-mcp-pods-exec, apiGroup: rbac.authorization.k8s.io}
EOF
done
```

(Standalone pre-provisioning is also available without starting the server:
`kubectl -n $NAMESPACE exec deploy/k8s-mcp-2-0-server -- python server.py --provision-rbac`,
or run it locally with SA env overrides.)

### Moving the server to a dedicated namespace

Recommended — the server is cluster-scoped in effect (its ClusterRoles and
cross-namespace bindings), so a dedicated namespace (e.g. `k8s-mcp-ops`)
beats a personal project one. Migration (v0.1.2+):

```bash
export OLD_NS=project-user-andrew-bydlon
export NAMESPACE=k8s-mcp-ops
kubectl create namespace $NAMESPACE          # the namespace must exist first
# reuse the current key (clients keep working) or mint a fresh one:
export API_KEY=$(kubectl -n $OLD_NS get secret k8s-mcp-2-0-apikey -o jsonpath='{.data.api-key}' | base64 -d)
kubectl -n $NAMESPACE create secret generic k8s-mcp-2-0-apikey \
  --from-literal="api-key=$API_KEY" --dry-run=client -o yaml | kubectl apply -f -
# ... plus your K8S_MCP_* exports ...
envsubst < k8s-mcp-2-0-server.yaml | kubectl apply -f -
kubectl -n $NAMESPACE rollout status deploy/k8s-mcp-2-0-server
```

What happens automatically:

- The same API key is reused (clients keep working).
- The provisioner ClusterRoleBinding and the Deployment/Secret/Service/
  VirtualService are created in (or updated to) the new namespace.
- At startup, provisioning **repoints** all existing exec RoleBindings from
  the old ServiceAccount to the new one — look for
  `AUDIT rbac provisioning repointed ...` lines; no manual binding cleanup is
  needed (v0.1.2+; earlier versions would leave them stale).

Then remove the leftovers of the old install:

```bash
kubectl -n $OLD_NS delete deployment/k8s-mcp-2-0-server service/k8s-mcp-2-0-svc \
  serviceaccount/k8s-mcp-2-0-sa secret/k8s-mcp-2-0-apikey \
  virtualservice.networking.istio.io/k8s-mcp-2-0-server
# cluster-scoped: the old viewer binding is named after the old namespace
kubectl delete clusterrolebinding k8s-mcp-2-0-viewer-${OLD_NS}
```

Since the server no longer lives in your personal namespace, you can drop it
from `K8S_MCP_BLOCKED_NAMESPACES` (making your own workloads debuggable) and
blacklist the new dedicated one instead: `kube-system,kube-public,k8s-mcp-ops`.

### Removing exec

```bash
kubectl -n $NAMESPACE set env deploy/k8s-mcp-2-0-server K8S_MCP_EXEC_ENABLED=false
```

The tool disappears on the next rollout. Leftover RoleBindings are harmless
(the tool that would use them is gone) but can be removed:

```bash
for NS in team-a debug-x; do kubectl -n "$NS" delete rolebinding k8s-mcp-2-0-exec; done
```

## 7. Operations

- **API key rotation**: `kubectl -n $NAMESPACE create secret generic
  k8s-mcp-2-0-apikey --from-literal=api-key=<new> --dry-run=client -o yaml |
  kubectl apply -f -` then `kubectl -n $NAMESPACE rollout restart
  deploy/k8s-mcp-2-0-server`; update clients.
- **Changing the exec list / namespace policy**: `kubectl set env …` (rolls
  automatically; provisioning re-runs at startup).
- **Audit trail**: `kubectl -n $NAMESPACE logs deploy/k8s-mcp-2-0-server |
  grep AUDIT` — every exec allow/deny and RBAC provisioning action.
- Changing `K8S_MCP_EXEC_NAMESPACES` does **not** delete RoleBindings for
  removed namespaces (idempotent ensure, not a reconciler) — remove them with
  the loop above if you want the RBAC pruned.

## Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| HTTP 401 from the endpoint | API key missing/mismatch in the client header |
| Startup warning "endpoint is UNAUTHENTICATED" | `K8S_MCP_API_KEY` not set — do not expose this |
| `RBAC provisioning: cannot read template ClusterRole: Forbidden` | the provisioner ClusterRole predates the `get` grant — re-apply the manifest (export your knobs again first, see below) |
| `RBAC provisioning: template ClusterRole missing` | the manifest wasn't fully applied — re-run step 2 |
| Re-applying the manifest wiped my env knobs | `envsubst` renders `K8S_MCP_*` from the current shell — re-run your `export` block before every `envsubst \| kubectl apply`, or change knobs with `kubectl set env` instead |
| Pod `CreateContainerConfigError` (`couldn't find key api-key in Secret …`) | the Secret is missing or was emptied by an envsubst apply with an unset `API_KEY`. Recreate it (see step 2); the pod recovers on its own once the key exists |
| Pod crashloops (`Error`/`CrashLoopBackOff`) right after enabling exec | v0.1.0 bug: startup provisioning crashed the process on a missing kubernetes-client model class. Fixed in v0.1.1 (plain dict bodies + provisioning degrades to a log line instead of dying). Recover: `kubectl rollout undo deploy/k8s-mcp-2-0-server`, rebuild/push v0.1.1, re-apply |
| `exec policy: namespace 'x' is not in the exec namespace list` | add it to `K8S_MCP_EXEC_NAMESPACES` and restart |
| exec fails `403 Forbidden` | RoleBinding missing — check startup logs for the provisioning summary, or bind manually |
| `pod lacks label k8s-mcp.io/exec="true"` | label the workload |
| `binary 'x' is not in the exec allowlist` | add to `K8S_MCP_EXEC_ALLOWED_COMMANDS` (shells/interpreters stay hard-denied) |
| `not covered by the caller's exec assignment` | the key's `K8S_MCP_CLIENTS` pattern list doesn't include the namespace — widen the assignment (admin) or use the right key |
| `not covered by the x-exec-namespaces header` | the client's own narrowing header excludes the namespace — remove/adjust the header |
| `invalid x-exec-namespaces header` → 400 | header patterns malformed (lowercase DNS labels / globs) |
| `refusing exec into the MCP server's own pod` | working as intended (credential isolation) |
| `RBAC provisioning: template ClusterRole missing` | the manifest wasn't fully applied — re-run step 2 |
