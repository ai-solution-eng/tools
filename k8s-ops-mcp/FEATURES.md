# FEATURES.md — Hardening & Capability changelog (v0.0.1 → v0.1.3)

This file summarizes everything that changed across the hardening session that took
`k8s-mcp-2-0-server` from the original pre-audit build (v0.0.1: `shell=True` kubectl,
`cluster-admin`, unauthenticated endpoint) to the current v0.1.3 — deployed, verified
live, and in daily use from DSH.

---

## 1. Security audit — the original attack surface, closed

| # | Vulnerability (v0.0.1) | Fix |
| --- | --- | --- |
| 1 | **Shell injection** — `subprocess.run(f"kubectl {command}", shell=True)`; the write-verb *denylist* checked only the first word, so `get pods && kubectl delete ns foo`, `;`, `` ` ``, `$()`, `>`, newlines all executed | kubectl is invoked as an **argv list** via `asyncio.create_subprocess_exec` — never a shell. Metacharacters arrive as inert tokens (verified: `get pods; touch /tmp/pwned` never executes) |
| 2 | **`cluster-admin` ClusterRoleBinding** on a "read-only" server | Custom read-only `ClusterRole` enumerating exactly what the tools need. Secrets and RBAC objects deliberately excluded — `list_secrets` / `get secret` return **403 from the API server**, not from tool formatting |
| 3 | **Secret exfiltration by design** — `list_secrets` hid values client-side while the escape hatch allowed `get secret -o yaml` | Closed at the RBAC layer (no secret verbs at all) |
| 4 | **Denylist gaps** — `run`, `exec`, `attach`, `cp`, `port-forward`, `proxy`, `debug`, `set`, `autoscale`, `certificate`, `plugin` all permitted | Deny-by-default **read-verb allowlist**: `get, describe, logs, top, explain, api-resources, api-versions, cluster-info, version, auth, events` |
| 5 | **Credential-redirection flags** — `--server`, `--token`, `--kubeconfig`, `--insecure-skip-tls-verify`, … could steal the SA token | Rejected outright before execution |
| 6 | **Unvalidated tool parameters** flowed into command strings | Namespace = DNS-label regex; names/types/output strictly validated (`yaml/json/wide/name/jsonpath=/custom-columns=`) |
| 7 | **Predictable `/tmp/kubeconfig`** written by every call (also broke local dev) | `tempfile.mkstemp` mode `0600`, created only when actually in-cluster |
| 8 | **Container ran as root, writable rootfs, unpinned kubectl** | Non-root uid/gid 10001, read-only root filesystem, all capabilities dropped, RuntimeDefault seccomp, `/tmp` as emptyDir; kubectl **pinned (`v1.37.0`) + sha256-verified** at build; `TARGETARCH`-aware downloads |
| 9 | **Unauthenticated HTTP endpoint** | API-key auth — see §3 |

## 2. MCP 2.0 conformance (protocol `2026-07-28`)

- Stateless operation verified live against the real app: stateless `tools/list`,
  `tools/call`, `server/discover` (`supportedVersions=["2026-07-28"]`), legacy
  `2025-03-26` handshake honored on the same process, strict per-request envelope
  rejection (`-32602` naming the missing key), standard `-32602` for missing resources.
- **Cacheable list results enabled**: `tools/list` carries `ttlMs=300000, cacheScope=public`
  via `MCPServer(cache_hints=…)` — safe because the tool list is static and identical for
  every caller.
- Header-based routing (`Mcp-Method`/`Mcp-Name`) validated against the body.

## 3. Authentication & multi-user support

- **API-key auth middleware** (pure ASGI, zero extra dependencies): every request must
  present `Authorization: Bearer <key>` or `X-API-Key: <key>`; constant-time comparison;
  401 before the MCP app is reached. Auth disabled only when no key is configured
  (local dev) — with a loud startup warning.
- **The key is out-of-band by design**: managed exclusively via
  `kubectl create secret …` — deliberately *removed* from the envsubst manifest after an
  unset `API_KEY` silently emptied the live Secret (incident, see §7). The Deployment now
  **fails loud** (`CreateContainerConfigError`) until the Secret exists.
- **Per-user keys with per-user exec assignments** (`K8S_MCP_CLIENTS`):
  `name:key[:exec-ns-patterns];…` — capabilities travel with the credential; users cannot
  widen their own scope. The shared key keeps working alongside (deployment-wide ceiling).
- **`X-Exec-Namespaces` request header**: narrows a request's exec scope (intersected
  with the key's assignment); can never widen. Malformed patterns → 400.
- Every exec attempt is audit-logged with the calling client's name.

## 4. Namespace governance

- `K8S_MCP_ALLOWED_NAMESPACES` / `K8S_MCP_BLOCKED_NAMESPACES` (comma-separated fnmatch
  globs; blacklist always wins). Fail-loud at startup on malformed values; malformed
  values at request time degrade to a clean denial.
- Reads: cluster-wide results are **filtered** to the policy; `list_namespaces` shows only
  policy-visible namespaces.
- kubectl-backed tools: explicit `-n` checked; namespaced queries without `-n` rejected
  with a hint; `-A`/`--all-namespaces` rewritten per allowed namespace (glob expansion
  against the live namespace list, cap 20) or rejected under blacklist-only policy.
- Cluster-scoped resources unaffected; `auth can-i` is context-only by design.

## 5. Hardened container exec (`exec_in_pod`) — opt-in

Off unless `K8S_MCP_EXEC_ENABLED=true` (the tool is not even registered). When enabled,
eight independent layers:

1. **Opt-in registration** — unlisted tools cannot be called.
2. **RBAC** — `pods/exec` `create` granted only by **namespaced** Rolebindings (never a
   ClusterRole/ClusterRoleBinding) — the API server is the real boundary.
3. **Per-pod opt-in label** — `k8s-mcp.io/exec: "true"` required (RBAC cannot match
   labels; the app can). Gate configurable, default on.
4. **Argv-typed command** — `command: list[str]`, one command, passed after kubectl's
   `--` (flag parsing stops; no shell on either side).
5. **Binary allowlist** — read-only introspection set (`ps, ls, cat, tail, head, grep,
   df, du, free, ss, netstat, ip, stat, id, …`), basename-matched, extendable via
   `K8S_MCP_EXEC_ALLOWED_COMMANDS`. `env`/`printenv` (secret dumps) and
   `curl`/`wget`/`nc` (exfil channels) excluded by default.
6. **Hard denies** — shells, interpreters, `su`/`sudo`, `nsenter`/`unshare`; `find -exec`
   and `ip netns` argument tokens rejected. Not overridable by config.
7. **Self-pod guard** — exec into the MCP server's own pod refused (credential isolation).
8. **Bounded + audited** — no TTY/stdin, 30 s timeout with race-safe kill/reap, 50k
   output truncation, `AUDIT exec decision=…` log line for every allow *and* deny.

## 6. Automatic exec RBAC provisioning

- At startup (or `python server.py --provision-rbac`), the server ensures its own
  `pods/exec` RoleBindings for every namespace on the exec list — glob patterns expanded
  against the live namespace list, general-policy-blocked and nonexistent namespaces
  skipped, idempotent (`already present`), foreign bindings never touched.
- **Escalation-proof by construction**: the provisioner permission is `bind` +
  `get` **resourceNames-scoped** to the single exec-template ClusterRole — it can never
  reference another role, create or modify role content, or create ClusterRoleBindings.
- **Migration-safe**: bindings whose subject points at a previous ServiceAccount/namespace
  are **repointed automatically** (the fix that made the dedicated-namespace migration a
  one-command operation).
- **Degrade, don't die**: any provisioning failure is returned as a log line — it can
  never crash the server (a v0.1.0 crashloop taught us this the hard way).

## 7. Reliability & performance

- **Event loop never blocks**: kubectl via `asyncio.create_subprocess_exec` + bounded
  `wait_for`; Kubernetes client calls via `asyncio.to_thread`. A hung kubectl is killed
  and reaped in bounded time.
- **Concurrent fan-out** (`asyncio.gather`) in `cluster_health` and `list_workloads`.
- `get_custom_resource` uses the dynamic client (no kubectl subprocess) with kubectl
  fallback only on discovery/schema errors — the previous always-fallback dead code is gone.
- Duplicate `list_namespaces` registration removed; dead client vars removed.
- Output truncation on every kubectl path.

## 8. Deployment experience

- **`DEPLOYMENT.md`** — build, one-command install, MCP client setup, namespace policy,
  exec enablement, per-user keys, migration, rotation, troubleshooting table.
- **All knobs envsubst-driven** (`K8S_MCP_*`): unset exports → documented defaults;
  deploy-time exports and `kubectl set env` both work.
- **`MCP_HOSTNAME` knob** — the public hostname is unique per deployment (two
  VirtualServices claiming one host split traffic between their backends — observed live
  between two namespaces) and is passed to the container for host validation.
- **Host-header (DNS-rebinding) protection correctly scoped**: when `MCP_HOSTNAME` is set,
  the SDK's protection stays **on** with the real hostname allowlisted. The implicit
  default (loopback-only) silently 421'd every gateway-fronted request that carried a
  valid key — requests that failed auth never reached it, which is why only real users
  found it (v0.1.3).
- **The API key Secret is out-of-band**: an envsubst apply can no longer empty it.
- Private ghcr images: `imagePullSecrets` shipped in the manifest (pull secrets are
  namespace-scoped — a live `kubectl patch` does not survive envsubst applies).
- `check_rbac` surfaces `kubectl auth can-i` "no" answers (which exit 1) instead of
  masking them as errors.

## 9. Verification

- `test_namespace_policy.py` — **144 self-contained checks**: namespace policy and globs,
  precedence (blacklist wins), exec command guard (allowlist, hard-denies, token denials),
  argv building, per-client parsing/auth/narrowing, provisioning (create/keep/patch/
  skip/refuse/degrade), API-key middleware (401 paths, both headers, dev mode), MCP 2.0
  envelope behavior, and `check_rbac` answer surfacing. Runs without a cluster (the
  `kubernetes` package is stubbed when absent).
- **Live protocol conformance**: stateless tools/list+call, `server/discover`, cache
  hints, legacy-era fallback, strict envelope rejection — verified over real HTTP.
- **Live deployment verification** (production cluster): 236 exec RoleBindings
  provisioned and repointed across a namespace migration; `can-i create pods/exec` yes in
  kept namespaces / no elsewhere; real `ps` executed inside a running predictor through
  the full gateway→auth→policy→allowlist→RBAC chain; 401 gate and audit trail confirmed.

## 10. Version history

| Tag | Server | Highlights |
| --- | --- | --- |
| v0.0.1 | 2.0.0 | Original: shell injection, cluster-admin, no auth |
| v0.1.0 | 2.1.0 | Security audit fixes, API-key auth, namespace governance, hardened exec + auto-RBAC |
| v0.1.1 | 2.1.1 | Crashloop fix (dict bodies, degrade-don't-die provisioning), imagePullSecrets, `check_rbac` truth |
| v0.1.2 | 2.1.2 | `check_rbac` "no" surfacing, provisioner `get` grant, migration repointing, `MCP_HOSTNAME` knob |
| v0.1.3 | 2.1.3 | Host-header protection correctly scoped (the 421 fix), Secret out-of-band, `MCP_HOSTNAME` reaches the container |

## 11. Files

| File | Purpose |
| --- | --- |
| `server.py` | The server — 18 tools, auth, policy, exec, provisioning |
| `k8s-mcp-2-0-server.yaml` | Full deployment: RBAC (read-only + exec template + scoped provisioner), hardened pod, envsubst knobs |
| `Dockerfile` | Pinned + checksum-verified kubectl, non-root, arch-aware |
| `DEPLOYMENT.md` | Operator runbook (deploy, clients, exec, migration, rotation, troubleshooting) |
| `FEATURES.md` | This file |
| `test_namespace_policy.py` | 144-check verification suite (no cluster required) |
