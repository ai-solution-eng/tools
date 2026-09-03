"""Read-only Kubernetes ops MCP server (MCP SDK v2, protocol 2026-07-28).

Security model
--------------
- kubectl is never invoked through a shell: every call is an argv list via
  asyncio.create_subprocess_exec, gated by a read-verb allowlist and a
  rejected-flag list (--server/--token/--kubeconfig/... cannot redirect the
  API connection or leak the service-account token).
- Tool parameters (namespace, name, resource type, output) are validated
  against DNS-label/name character sets before they reach kubectl or the
  Kubernetes API.
- Optional namespace governance via environment variables (see below);
  blacklist always wins over whitelist.
- The shipped manifest binds a custom read-only ClusterRole (no secrets, no
  RBAC objects) instead of cluster-admin, and hardens the pod (non-root,
  read-only root filesystem, dropped capabilities, seccomp).

NAMESPACE GOVERNANCE
--------------------
K8S_MCP_ALLOWED_NAMESPACES   comma-separated allow patterns (fnmatch globs,
                             e.g. "project-user-*,team-a"); unset = all
                             namespaces allowed.
K8S_MCP_BLOCKED_NAMESPACES   comma-separated deny patterns (e.g.
                             "kube-system,kube-*"); always denied, even when
                             whitelisted.
When a policy is active, namespaced kubectl queries must pass -n; -A/--all-
namespaces is rewritten into one query per allowed namespace (limit 20) when
a whitelist is set, and rejected when only a blacklist is set (arbitrary
kubectl output cannot be filtered reliably). Python-API-backed tools
(cluster_health, list_*, get_events) filter denied namespaces from results.
"""

import asyncio
import base64
import contextvars
import datetime
import fnmatch
import hmac
import json
import os
import re
import shlex
import sys
import tempfile
from typing import NamedTuple

from mcp.server.caching import CacheHint
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from kubernetes import client, config, dynamic
from kubernetes.client.rest import ApiException
from kubernetes.dynamic.exceptions import ResourceNotFoundError

# Initialize MCP server (MCP SDK v2 / protocol 2026-07-28).
# Transport options (host, port, stateless_http, ...) are passed to run(),
# not the constructor.
# cache_hints: the tool list is static and identical for every caller (the
# namespace policy affects call results, not schemas), so clients may cache
# tools/list for 5 minutes and share the cached copy.
mcp = MCPServer(
    "k8s-ops-server",
    title="Kubernetes Ops MCP Server",
    description=(
        "Read-only Kubernetes inspection: pods, workloads, events, logs, "
        "services, ConfigMaps, Secrets, PVCs, CRDs, RBAC, and generic "
        "kubectl, plus an opt-in hardened exec_in_pod (disabled unless "
        "K8S_MCP_EXEC_ENABLED=true). Serves both 2025-era and 2026-07-28 "
        "(MCP 2.0) clients."
    ),
    version="2.1.3",
    cache_hints={"tools/list": CacheHint(ttl_ms=300_000, scope="public")},
)

# Disable proxy for in-cluster K8s API access
_saved_proxies = {}
for key in ('HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy'):
    val = os.environ.pop(key, None)
    if val:
        _saved_proxies[key] = val

# Authenticate
try:
    config.load_incluster_config()
    print("Loaded In-Cluster Service Account Config")
except config.ConfigException:
    try:
        config.load_kube_config()
        print("Loaded Local Kubeconfig")
    except Exception as e:
        print(f"Failed to load K8s config: {e}")

# Restore proxy settings
for key, val in _saved_proxies.items():
    os.environ[key] = val

# Clients
api_client = client.ApiClient()
v1 = client.CoreV1Api()
apps_v1 = client.AppsV1Api()
batch_v1 = client.BatchV1Api()
rbac_v1 = client.RbacAuthorizationV1Api()
dyn_client = dynamic.DynamicClient(api_client)


# ─── Namespace access policy (optional) ──────────────────────────────────

ALLOWED_NAMESPACES_ENV = "K8S_MCP_ALLOWED_NAMESPACES"
BLOCKED_NAMESPACES_ENV = "K8S_MCP_BLOCKED_NAMESPACES"
MAX_NS_QUERY_REWRITE = 20
_NS_NAME_RE = re.compile(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?")
_NS_PATTERN_RE = re.compile(r"[a-z0-9*?][a-z0-9*?-]{0,61}")


def _parse_ns_patterns(raw: str) -> tuple:
    patterns = []
    for part in raw.split(","):
        pattern = part.strip().lower()
        if not pattern:
            continue
        if not _NS_PATTERN_RE.fullmatch(pattern):
            raise ValueError(
                f"invalid namespace pattern {pattern!r} in {raw!r}: use "
                "lowercase DNS labels with optional * / ? globs"
            )
        patterns.append(pattern)
    return tuple(patterns)


def _namespace_policy():
    """(allowed_patterns | None, blocked_patterns | None); None means unset."""
    allowed_raw = os.environ.get(ALLOWED_NAMESPACES_ENV, "").strip()
    blocked_raw = os.environ.get(BLOCKED_NAMESPACES_ENV, "").strip()
    return (
        _parse_ns_patterns(allowed_raw) if allowed_raw else None,
        _parse_ns_patterns(blocked_raw) if blocked_raw else None,
    )


def _namespace_policy_active() -> bool:
    allowed, blocked = _namespace_policy()
    return allowed is not None or blocked is not None


def _pattern_hit(patterns, namespace: str) -> bool:
    return any(fnmatch.fnmatchcase(namespace, pattern) for pattern in patterns)


def _policy_hint() -> str:
    allowed, _ = _namespace_policy()
    if allowed is None:
        return ""
    shown = ", ".join(sorted(allowed))
    if len(shown) > 200:
        shown = shown[:197] + "..."
    return f" Allowed namespaces: {shown}."


def namespace_violation(namespace: str):
    """Return an error message when `namespace` is not allowed, else None."""
    ns = (namespace or "").strip().lower()
    if not ns:
        return None
    if not _NS_NAME_RE.fullmatch(ns):
        return (
            f"invalid namespace {namespace!r}: must be a lowercase DNS "
            "label (max 63 chars)"
        )
    try:
        allowed, blocked = _namespace_policy()
    except ValueError as e:
        return f"server namespace policy is misconfigured: {e}"
    if blocked is not None and _pattern_hit(blocked, ns):
        return f"namespace '{ns}' is denied by the namespace policy.{_policy_hint()}"
    if allowed is not None and not _pattern_hit(allowed, ns):
        return f"namespace '{ns}' is not covered by the namespace policy.{_policy_hint()}"
    return None


def _visible(namespace: str) -> bool:
    return namespace_violation(namespace) is None


def _filter_visible(items):
    """Drop namespaced items that violate the policy (cluster-scoped pass)."""
    kept = []
    for item in items:
        meta = getattr(item, "metadata", None)
        if meta is None or meta.namespace is None or _visible(meta.namespace):
            kept.append(item)
    return kept


# ─── Input validation for kubectl parameters ─────────────────────────────

_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,250}")
_VERB_RE = re.compile(r"[a-z*]{1,64}")


def _validated_namespace(namespace: str) -> str:
    ns = (namespace or "").strip().lower()
    if ns and not _NS_NAME_RE.fullmatch(ns):
        raise ValueError(f"invalid namespace {namespace!r}: must be a lowercase DNS label")
    return ns


def _validated_resource_type(resource_type: str) -> str:
    rt = (resource_type or "").strip()
    if not _NAME_RE.fullmatch(rt):
        raise ValueError(f"invalid resource type {resource_type!r}")
    return rt


def _validated_name(name: str) -> str:
    nm = (name or "").strip()
    if not _NAME_RE.fullmatch(nm):
        raise ValueError(f"invalid resource name {name!r}")
    return nm


def _validated_output(output: str) -> str:
    out = (output or "").strip()
    if out in {"yaml", "json", "wide", "name"} or out.startswith(("jsonpath=", "custom-columns=")):
        if "\n" not in out and "\x00" not in out:
            return out
    raise ValueError(
        f"unsupported output format {output!r}; use yaml, json, wide, name, "
        "jsonpath=... or custom-columns=..."
    )


# ─── kubectl execution (no shell, argv only) ─────────────────────────────

KUBECTL_TIMEOUT_SECONDS = 30
KUBECTL_READ_VERBS = frozenset({
    "get", "describe", "logs", "top", "explain", "api-resources",
    "api-versions", "cluster-info", "version", "auth", "events",
})
# Verbs whose results carry namespace-scoped object data.
KUBECTL_NS_DATA_VERBS = frozenset({"get", "describe", "logs", "top", "events"})
# Flags that could redirect the API connection, swap credentials, or leak the
# service-account token; rejected outright.
KUBECTL_UNSAFE_FLAGS = frozenset({
    "--server", "--token", "--kubeconfig", "--client-certificate",
    "--client-key", "--username", "--password", "--certificate-authority",
    "--insecure-skip-tls-verify",
})
_CLUSTER_SCOPED_RESOURCES = frozenset({
    "nodes", "node", "no", "namespaces", "namespace", "ns",
    "persistentvolumes", "pv", "customresourcedefinitions",
    "customresourcedefinition", "crd", "crds", "clusterroles",
    "clusterrolebindings", "storageclasses", "storageclass", "sc",
    "priorityclasses", "priorityclass", "pc", "ingressclasses",
    "runtimeclasses", "mutatingwebhookconfigurations",
    "validatingwebhookconfigurations", "validatingadmissionpolicies",
    "validatingadmissionpolicybindings", "csidrivers", "csinodes",
    "volumeattachments", "certificatesigningrequests", "csr", "apiservices",
    "flowschemas", "prioritylevelconfigurations", "clusterissuers",
    "componentstatuses", "cs",
})


class KubectlError(Exception):
    """kubectl could not be executed or returned a non-zero exit."""


class NamespacePolicyError(Exception):
    """The command targets a namespace the policy does not allow."""


# In-cluster kubeconfig for the kubectl escape hatch. Written once at startup
# to a mkstemp file (mode 0600) instead of a predictable /tmp/kubeconfig, and
# only when actually running in-cluster (dev kubeconfig is left alone).
_IN_CLUSTER_KUBECONFIG = """
apiVersion: v1
kind: Config
clusters:
- cluster:
    certificate-authority: /var/run/secrets/kubernetes.io/serviceaccount/ca.crt
    server: https://kubernetes.default.svc.cluster.local
  name: default
contexts:
- context:
    cluster: default
    namespace: default
    user: default
  name: default
current-context: default
users:
- name: default
  user:
    tokenFile: /var/run/secrets/kubernetes.io/serviceaccount/token
"""
_KUBECONFIG_PATH = None


def _create_incluster_kubeconfig():
    if not os.environ.get("KUBERNETES_SERVICE_HOST"):
        return None
    try:
        fd, path = tempfile.mkstemp(prefix="kubeconfig-", suffix=".yaml", dir="/tmp")
        with os.fdopen(fd, "w") as f:
            f.write(_IN_CLUSTER_KUBECONFIG)
        os.chmod(path, 0o600)
        return path
    except OSError as e:
        print(f"Could not create in-cluster kubeconfig: {e}")
        return None


_KUBECONFIG_PATH = _create_incluster_kubeconfig()


def _kubectl_env() -> dict:
    env = os.environ.copy()
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        env.pop(key, None)
    if _KUBECONFIG_PATH:
        env["KUBECONFIG"] = _KUBECONFIG_PATH
    else:
        env.pop("KUBECONFIG", None)
    return env


async def _kubectl_run(argv: list):
    """Run one kubectl invocation; returns (returncode, stdout, stderr)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "kubectl", *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_kubectl_env(),
        )
    except (OSError, ValueError) as e:
        raise KubectlError(f"could not execute kubectl: {e}") from e
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=KUBECTL_TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError:
        # The child may exit between the cancellation and the kill; never let
        # reaping errors or orphaned grandchildren stall the caller.
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            pass
        raise KubectlError(
            f"kubectl timed out after {KUBECTL_TIMEOUT_SECONDS} seconds"
        ) from None
    out = stdout.decode("utf-8", errors="replace").strip()
    err = stderr.decode("utf-8", errors="replace").strip()
    return proc.returncode, out, err


async def _kubectl_exec(argv: list) -> str:
    """Run one kubectl invocation, raising KubectlError on failure."""
    rc, out, err = await _kubectl_run(argv)
    if rc != 0:
        raise KubectlError(err or f"kubectl exited with code {rc}")
    return _truncate(out) if out else "(no output)"


def _extract_ns_target(argv: list):
    """Return (namespace, all_namespaces) from kubectl flags, if present."""
    ns, allns, i = None, False, 0
    while i < len(argv):
        tok = argv[i]
        if tok in ("-n", "--namespace"):
            if i + 1 < len(argv):
                ns = argv[i + 1]
            i += 2
        elif tok.startswith("--namespace="):
            ns = tok.split("=", 1)[1] or None
            i += 1
        elif tok.startswith("-n") and len(tok) > 2:
            ns = tok[3:] if tok[2] == "=" else tok[2:]
            i += 1
        elif tok in ("-A", "--all-namespaces"):
            allns = True
            i += 1
        else:
            i += 1
    return ns, allns


def _short_resource(token: str) -> str:
    return token.split(".", 1)[0].split("/", 1)[0].lower()


def _is_cluster_scoped(resource_token: str) -> bool:
    return _short_resource(resource_token) in _CLUSTER_SCOPED_RESOURCES


async def _expand_allowed(allowed) -> list:
    """Expand glob patterns against the live namespace list; pass exact names."""
    if not any(ch in pattern for pattern in allowed for ch in "*?"):
        return sorted(allowed)
    try:
        ns_list = await asyncio.to_thread(v1.list_namespace)
    except ApiException as e:
        raise NamespacePolicyError(
            f"could not expand namespace glob patterns: {e.reason}"
        ) from e
    matched = {n.metadata.name for n in ns_list.items if _visible(n.metadata.name)}
    return sorted(matched)


async def _namespace_plan(argv: list):
    """Return [(label, argv), ...] to execute, honoring the namespace policy.

    label is the namespace a rewritten query targets, or None when the
    original argv passes through unchanged. Raises NamespacePolicyError when
    the command cannot be allowed.
    """
    allowed, blocked = _namespace_policy()
    if allowed is None and blocked is None:
        return [(None, argv)]
    verb = argv[0].lower() if argv else ""
    if verb not in KUBECTL_NS_DATA_VERBS or len(argv) < 2:
        return [(None, argv)]
    if _is_cluster_scoped(argv[1]):
        return [(None, argv)]
    ns, allns = _extract_ns_target(argv)
    if ns:
        violation = namespace_violation(ns)
        if violation:
            raise NamespacePolicyError(violation)
        return [(None, argv)]
    if not allns:
        raise NamespacePolicyError(
            "no namespace given and a namespace policy is active; pass "
            f"-n <namespace>.{_policy_hint()}"
        )
    if allowed is None:
        raise NamespacePolicyError(
            "cluster-wide (-A/--all-namespaces) queries are disabled while "
            f"{BLOCKED_NAMESPACES_ENV} is set; pass -n <namespace> instead."
        )
    expanded = await _expand_allowed(allowed)
    if not expanded:
        raise NamespacePolicyError(
            f"the {ALLOWED_NAMESPACES_ENV} patterns matched no live namespaces."
        )
    if len(expanded) > MAX_NS_QUERY_REWRITE:
        raise NamespacePolicyError(
            f"{len(expanded)} allowed namespaces exceed the "
            f"{MAX_NS_QUERY_REWRITE}-namespace cluster-wide rewrite limit; "
            "pass -n <namespace> instead."
        )
    base = [tok for tok in argv if tok not in ("-A", "--all-namespaces")]
    return [(nsx, base + ["-n", nsx]) for nsx in expanded]


async def _kubectl_plan_execute(argv: list) -> str:
    """Enforce the namespace policy, then execute the (possibly rewritten) plan."""
    try:
        plan = await _namespace_plan(argv)
    except NamespacePolicyError as e:
        return f"Error: {e}"
    if len(plan) == 1:
        try:
            return await _kubectl_exec(plan[0][1])
        except KubectlError as e:
            return f"Error: {e}"
    blocks = []
    for label, args in plan:
        try:
            out = await _kubectl_exec(args)
        except KubectlError as e:
            out = f"Error: {e}"
        blocks.append(f"=== namespace {label} ===\n{out}")
    return _truncate("\n".join(blocks))


def _parse_kubectl_command(command: str) -> list:
    """Parse the raw tool argument into argv, enforcing the read-verb allowlist."""
    try:
        argv = shlex.split(command)
    except ValueError as e:
        raise KubectlError(f"could not parse command: {e}") from e
    if not argv:
        raise KubectlError("empty command")
    if argv[0] not in KUBECTL_READ_VERBS:
        raise KubectlError(
            f"command '{argv[0]}' is not allowed; allowed read verbs: "
            + ", ".join(sorted(KUBECTL_READ_VERBS))
        )
    for tok in argv[1:]:
        head = tok.split("=", 1)[0]
        if tok in KUBECTL_UNSAFE_FLAGS or head in KUBECTL_UNSAFE_FLAGS:
            raise KubectlError(f"flag '{head}' is not allowed")
    return argv


# ─── Helpers ─────────────────────────────────────────────────────────────

def _age(ts):
    if not ts:
        return "Unknown"
    delta = datetime.datetime.now(datetime.timezone.utc) - ts
    secs = int(delta.total_seconds())
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h"
    return f"{secs // 86400}d"


def _truncate(text: str, max_len: int = 50000) -> str:
    if len(text) > max_len:
        return text[:max_len] + f"\n... (truncated, total {len(text)} chars)"
    return text


# ─── Generic K8s API Tools ───────────────────────────────────────────────

@mcp.tool()
async def get_resource(
    resource_type: str,
    name: str = "",
    namespace: str = "",
    output: str = "yaml",
) -> str:
    """Get any Kubernetes resource by type and optional name/namespace.
    Works with built-in and custom resources (CRDs).
    Examples:
      get_resource("pods", namespace="default")
      get_resource("pods", name="my-pod", namespace="default")
      get_resource("inferenceservices", namespace="ml-ns")
      get_resource("nodes")
      get_resource("customresourcedefinitions")
    Namespace policy applies: an omitted namespace means cluster-wide
    (rewritten per allowed namespace, or rejected under a blacklist-only
    policy)."""
    try:
        argv = ["get", _validated_resource_type(resource_type)]
        if name:
            argv.append(_validated_name(name))
        ns = _validated_namespace(namespace)
        if ns:
            argv += ["-n", ns]
        else:
            argv.append("--all-namespaces")
        argv += ["-o", _validated_output(output)]
    except ValueError as e:
        return f"Error: {e}"
    return await _kubectl_plan_execute(argv)


@mcp.tool()
async def describe_resource(
    resource_type: str,
    name: str,
    namespace: str = "",
) -> str:
    """Describe any Kubernetes resource. Shows events, conditions, and full status.
    Examples:
      describe_resource("pod", "my-pod", namespace="default")
      describe_resource("node", "worker-01")
      describe_resource("inferenceservice", "my-model", namespace="ml-ns")
    When a namespace policy is active, namespaced resources require -n."""
    try:
        argv = ["describe", _validated_resource_type(resource_type), _validated_name(name)]
        ns = _validated_namespace(namespace)
        if ns:
            argv += ["-n", ns]
    except ValueError as e:
        return f"Error: {e}"
    return await _kubectl_plan_execute(argv)


@mcp.tool()
async def list_api_resources() -> str:
    """List all available API resource types in the cluster, including CRDs.
    Use this to discover what resource types exist before querying them."""
    return await _kubectl_plan_execute(["api-resources", "--verbs=list", "-o", "wide"])


# ─── Cluster Health ──────────────────────────────────────────────────────

@mcp.tool()
async def cluster_health() -> str:
    """Get overall cluster health: node status, component status, resource usage.
    Use this as the first tool to understand the cluster state."""
    sections = []
    policy_on = _namespace_policy_active()

    nodes_res, ns_res, pods_res = await asyncio.gather(
        asyncio.to_thread(v1.list_node),
        asyncio.to_thread(v1.list_namespace),
        asyncio.to_thread(v1.list_pod_for_all_namespaces),
        return_exceptions=True,
    )

    # Nodes
    if isinstance(nodes_res, BaseException):
        sections.append(f"NODES: Error - {nodes_res}")
    else:
        node_lines = []
        for n in nodes_res.items:
            conditions = {c.type: c.status for c in n.status.conditions}
            ready = conditions.get("Ready", "Unknown")
            roles = ",".join(
                l.replace("node-role.kubernetes.io/", "")
                for l in n.metadata.labels or {}
                if l.startswith("node-role.kubernetes.io/")
            ) or "worker"
            alloc = n.status.allocatable or {}
            node_lines.append(
                f"  {n.metadata.name}: Ready={ready}, Roles={roles}, "
                f"CPU={alloc.get('cpu','?')}, Mem={alloc.get('memory','?')}, "
                f"GPU={alloc.get('nvidia.com/gpu', '0')}"
            )
        sections.append("NODES:\n" + "\n".join(node_lines))

    # Namespaces count (policy-visible namespaces when a policy is active)
    if isinstance(ns_res, BaseException):
        sections.append(f"NAMESPACES: Error - {ns_res}")
    else:
        ns_items = [n for n in ns_res.items if _visible(n.metadata.name)] if policy_on else ns_res.items
        sections.append(f"NAMESPACES: {len(ns_items)} total")

    # Pods summary across cluster
    if isinstance(pods_res, BaseException):
        sections.append(f"PODS: Error - {pods_res}")
    else:
        pod_items = _filter_visible(pods_res.items) if policy_on else pods_res.items
        total = len(pod_items)
        running = sum(1 for p in pod_items if p.status.phase == "Running")
        pending = sum(1 for p in pod_items if p.status.phase == "Pending")
        failed = sum(1 for p in pod_items if p.status.phase == "Failed")
        crash = sum(
            1 for p in pod_items
            if p.status.container_statuses
            and any(c.restart_count > 5 for c in p.status.container_statuses)
        )
        sections.append(
            f"PODS: {total} total, {running} running, {pending} pending, "
            f"{failed} failed, {crash} high-restart (>5)"
        )

    # Top nodes (if metrics available)
    try:
        top = await _kubectl_exec(["top", "nodes", "--no-headers"])
    except KubectlError:
        top = "metrics-server not available"
    sections.append(f"RESOURCE USAGE:\n{top}")

    return "\n\n".join(sections)


# ─── Namespaces ──────────────────────────────────────────────────────────

@mcp.tool()
async def list_namespaces() -> str:
    """List all namespaces with status and age. Namespace policy applies:
    only policy-allowed namespaces are shown."""
    try:
        ns_list = await asyncio.to_thread(v1.list_namespace)
        items = [n for n in ns_list.items if _visible(n.metadata.name)] if _namespace_policy_active() else ns_list.items
        if not items:
            return "No namespaces found (or none allowed by the namespace policy)."
        lines = []
        for ns in items:
            age = _age(ns.metadata.creation_timestamp)
            lines.append(f"  {ns.metadata.name:<40} {ns.status.phase:<10} Age: {age}")
        return "NAMESPACES:\n" + "\n".join(lines)
    except ApiException as e:
        return f"K8s API Error: {e.reason}"


# ─── Pods ────────────────────────────────────────────────────────────────

@mcp.tool()
async def list_pods(namespace: str = "", label_selector: str = "") -> str:
    """List pods with health status, restarts, and age.
    If namespace is empty, lists across all namespaces (namespace policy
    filters the results). Optional label_selector e.g. 'app=nginx'."""
    try:
        kwargs = {}
        if label_selector:
            kwargs["label_selector"] = label_selector
        if namespace:
            violation = namespace_violation(namespace)
            if violation:
                return f"Error: {violation}"
            pods = await asyncio.to_thread(v1.list_namespaced_pod, namespace, **kwargs)
            items = pods.items
        else:
            pods = await asyncio.to_thread(v1.list_pod_for_all_namespaces, **kwargs)
            items = _filter_visible(pods.items) if _namespace_policy_active() else pods.items

        if not items:
            return "No pods found."

        lines = []
        for p in items:
            ns = p.metadata.namespace
            restarts = sum(c.restart_count for c in p.status.container_statuses) if p.status.container_statuses else 0
            age = _age(p.status.start_time)
            lines.append(f"  {ns}/{p.metadata.name}: {p.status.phase} | Restarts: {restarts} | Age: {age}")
        return f"PODS ({len(lines)}):\n" + "\n".join(lines)
    except ApiException as e:
        return f"K8s API Error: {e.reason} ({e.status})"


@mcp.tool()
async def get_pod_logs(
    pod_name: str,
    namespace: str = "default",
    container: str = "",
    tail: int = 100,
    previous: bool = False,
) -> str:
    """Get logs from a pod. Specify container for multi-container pods.
    Set previous=True to get logs from the previous crashed container."""
    violation = namespace_violation(namespace)
    if violation:
        return f"Error: {violation}"
    try:
        tail = max(1, min(int(tail), 10000))
    except (TypeError, ValueError):
        tail = 100
    try:
        kwargs = {"name": pod_name, "namespace": namespace, "tail_lines": tail}
        if container:
            kwargs["container"] = container
        if previous:
            kwargs["previous"] = True
        logs = await asyncio.to_thread(v1.read_namespaced_pod_log, **kwargs)
        return _truncate(logs) if logs else "No logs found."
    except ApiException as e:
        return f"Error reading logs: {e.reason}"


# ─── Events ──────────────────────────────────────────────────────────────

@mcp.tool()
async def get_events(
    namespace: str = "",
    resource_name: str = "",
    event_type: str = "",
) -> str:
    """Get Kubernetes events. Filters: namespace, resource_name, event_type (Normal/Warning).
    If namespace is empty, gets events across all namespaces (namespace
    policy filters the results)."""
    try:
        if namespace:
            violation = namespace_violation(namespace)
            if violation:
                return f"Error: {violation}"
            events = await asyncio.to_thread(v1.list_namespaced_event, namespace)
            items = events.items
        else:
            events = await asyncio.to_thread(v1.list_event_for_all_namespaces)
            items = _filter_visible(events.items) if _namespace_policy_active() else events.items

        if event_type:
            items = [e for e in items if e.type == event_type]
        if resource_name:
            items = [e for e in items if resource_name in (e.involved_object.name or "")]

        # Sort by last timestamp, most recent first
        items.sort(key=lambda e: e.last_timestamp or e.event_time or datetime.datetime.min.replace(tzinfo=datetime.timezone.utc), reverse=True)
        items = items[:100]  # limit

        if not items:
            return "No events found matching filters."

        lines = []
        for e in items:
            age = _age(e.last_timestamp or e.event_time)
            lines.append(
                f"  [{e.type}] {e.involved_object.kind}/{e.involved_object.name} "
                f"in {e.metadata.namespace}: {e.reason} - {e.message} (Age: {age})"
            )
        return f"EVENTS ({len(lines)}):\n" + "\n".join(lines)
    except ApiException as e:
        return f"K8s API Error: {e.reason}"


# ─── Deployments / StatefulSets / DaemonSets ─────────────────────────────

@mcp.tool()
async def list_workloads(namespace: str = "") -> str:
    """List all workloads (Deployments, StatefulSets, DaemonSets, Jobs) in a
    namespace or cluster-wide (namespace policy filters the results)."""
    if namespace:
        violation = namespace_violation(namespace)
        if violation:
            return f"Error: {violation}"
    policy_on = _namespace_policy_active()

    async def _section(fetch, fmt):
        try:
            result = await fetch
        except ApiException:
            return None
        items = _filter_visible(result.items) if (policy_on and not namespace) else result.items
        if not items:
            return None
        return "\n".join(fmt(i) for i in items)

    if namespace:
        deps = _section(asyncio.to_thread(apps_v1.list_namespaced_deployment, namespace),
                        lambda d: f"  Deployment {d.metadata.namespace}/{d.metadata.name}: {d.status.ready_replicas or 0}/{d.spec.replicas} ready")
        sts = _section(asyncio.to_thread(apps_v1.list_namespaced_stateful_set, namespace),
                       lambda s: f"  StatefulSet {s.metadata.namespace}/{s.metadata.name}: {s.status.ready_replicas or 0}/{s.spec.replicas} ready")
        ds = _section(asyncio.to_thread(apps_v1.list_namespaced_daemon_set, namespace),
                      lambda d: f"  DaemonSet {d.metadata.namespace}/{d.metadata.name}: {d.status.number_ready}/{d.status.desired_number_scheduled} ready")
        jobs = _section(asyncio.to_thread(batch_v1.list_namespaced_job, namespace),
                        lambda j: f"  Job {j.metadata.namespace}/{j.metadata.name}: succeeded={j.status.succeeded or 0}, failed={j.status.failed or 0}")
    else:
        deps = _section(asyncio.to_thread(apps_v1.list_deployment_for_all_namespaces),
                        lambda d: f"  Deployment {d.metadata.namespace}/{d.metadata.name}: {d.status.ready_replicas or 0}/{d.spec.replicas} ready")
        sts = _section(asyncio.to_thread(apps_v1.list_stateful_set_for_all_namespaces),
                       lambda s: f"  StatefulSet {s.metadata.namespace}/{s.metadata.name}: {s.status.ready_replicas or 0}/{s.spec.replicas} ready")
        ds = _section(asyncio.to_thread(apps_v1.list_daemon_set_for_all_namespaces),
                      lambda d: f"  DaemonSet {d.metadata.namespace}/{d.metadata.name}: {d.status.number_ready}/{d.status.desired_number_scheduled} ready")
        jobs = _section(asyncio.to_thread(batch_v1.list_job_for_all_namespaces),
                        lambda j: f"  Job {j.metadata.namespace}/{j.metadata.name}: succeeded={j.status.succeeded or 0}, failed={j.status.failed or 0}")

    sections = [s for s in await asyncio.gather(deps, sts, ds, jobs) if s]
    return "WORKLOADS:\n" + "\n".join(sections) if sections else "No workloads found."


# ─── Services & Networking ───────────────────────────────────────────────

@mcp.tool()
async def list_services(namespace: str = "") -> str:
    """List services with type, cluster IP, and ports.
    If namespace is empty, lists across all namespaces (namespace policy
    filters the results)."""
    try:
        if namespace:
            violation = namespace_violation(namespace)
            if violation:
                return f"Error: {violation}"
            svcs = await asyncio.to_thread(v1.list_namespaced_service, namespace)
            items = svcs.items
        else:
            svcs = await asyncio.to_thread(v1.list_service_for_all_namespaces)
            items = _filter_visible(svcs.items) if _namespace_policy_active() else svcs.items
        if not items:
            return "No services found."
        lines = []
        for s in items:
            ports = ",".join(f"{p.port}/{p.protocol}" for p in (s.spec.ports or []))
            lines.append(f"  {s.metadata.namespace}/{s.metadata.name}: {s.spec.type} {s.spec.cluster_ip} [{ports}]")
        return f"SERVICES ({len(lines)}):\n" + "\n".join(lines)
    except ApiException as e:
        return f"K8s API Error: {e.reason}"


# ─── ConfigMaps & Secrets ────────────────────────────────────────────────

@mcp.tool()
async def get_configmap(name: str, namespace: str = "default") -> str:
    """Read a ConfigMap's data keys and values."""
    violation = namespace_violation(namespace)
    if violation:
        return f"Error: {violation}"
    try:
        cm = await asyncio.to_thread(v1.read_namespaced_config_map, name, namespace)
        if not cm.data:
            return f"ConfigMap '{name}' exists but has no data."
        return _truncate(json.dumps(cm.data, indent=2))
    except ApiException as e:
        return f"Error: {e.reason}"


@mcp.tool()
async def list_secrets(namespace: str = "default") -> str:
    """List secret names and types in a namespace (values are NOT shown).
    Requires the server's RBAC to grant secret read — the shipped read-only
    ClusterRole intentionally does not."""
    violation = namespace_violation(namespace)
    if violation:
        return f"Error: {violation}"
    try:
        secrets = await asyncio.to_thread(v1.list_namespaced_secret, namespace)
        if not secrets.items:
            return "No secrets found."
        lines = [f"  {s.metadata.name}: type={s.type}, keys={list(s.data.keys()) if s.data else []}"
                 for s in secrets.items]
        return f"SECRETS ({len(lines)}):\n" + "\n".join(lines)
    except ApiException as e:
        return f"K8s API Error: {e.reason}"


# ─── PVCs & Storage ─────────────────────────────────────────────────────

@mcp.tool()
async def list_pvcs(namespace: str = "") -> str:
    """List PersistentVolumeClaims with status, capacity, and storage class.
    If namespace is empty, lists across all namespaces (namespace policy
    filters the results)."""
    try:
        if namespace:
            violation = namespace_violation(namespace)
            if violation:
                return f"Error: {violation}"
            pvcs = await asyncio.to_thread(v1.list_namespaced_persistent_volume_claim, namespace)
            items = pvcs.items
        else:
            pvcs = await asyncio.to_thread(v1.list_persistent_volume_claim_for_all_namespaces)
            items = _filter_visible(pvcs.items) if _namespace_policy_active() else pvcs.items
        if not items:
            return "No PVCs found."
        lines = []
        for p in items:
            cap = p.status.capacity.get("storage", "?") if p.status.capacity else "?"
            lines.append(
                f"  {p.metadata.namespace}/{p.metadata.name}: {p.status.phase} "
                f"| {cap} | SC: {p.spec.storage_class_name}"
            )
        return f"PVCs ({len(lines)}):\n" + "\n".join(lines)
    except ApiException as e:
        return f"K8s API Error: {e.reason}"


# ─── Custom Resources (CRDs) ────────────────────────────────────────────

@mcp.tool()
async def list_crds() -> str:
    """List all Custom Resource Definitions (CRDs) installed in the cluster."""
    return await _kubectl_plan_execute([
        "get", "crd", "-o",
        "custom-columns=NAME:.metadata.name,GROUP:.spec.group,SCOPE:.spec.scope",
    ])


@mcp.tool()
async def get_custom_resource(
    group: str,
    version: str,
    plural: str,
    name: str = "",
    namespace: str = "",
) -> str:
    """Get custom resources using the K8s dynamic API (kubectl fallback).
    Examples:
      get_custom_resource("serving.kserve.io", "v1beta1", "inferenceservices", namespace="ml-ns")
      get_custom_resource("genai.hpe.com", "v1", "openwebuis", name="my-ui", namespace="my-ns")
    An empty namespace lists across all namespaces (namespace policy filters
    the results)."""
    try:
        group = _validated_resource_type(group)
        plural = _validated_resource_type(plural)
        name = _validated_name(name) if name else ""
        ns = _validated_namespace(namespace)
    except ValueError as e:
        return f"Error: {e}"
    violation = namespace_violation(ns)
    if violation:
        return f"Error: {violation}"

    try:
        resource = await asyncio.to_thread(
            dyn_client.resources.get, api_version=f"{group}/{version}", plural=plural,
        )
        if name:
            obj = await asyncio.to_thread(resource.get, name=name, namespace=ns or None)
            instances = [obj]
        else:
            listing = await asyncio.to_thread(resource.get, namespace=ns or None)
            instances = list(getattr(listing, "items", None) or [])
        if _namespace_policy_active() and not ns:
            instances = _filter_visible(instances)
        if not instances:
            return "No custom resources found."
        payload = json.dumps([i.to_dict() for i in instances], indent=2, default=str)
        return _truncate(payload)
    except (ApiException, ResourceNotFoundError, ValueError, TypeError, AttributeError):
        # Discovery or schema issues fall back to kubectl for reliability.
        argv = ["get", f"{plural}.{group}"]
        if name:
            argv.append(name)
        if ns:
            argv += ["-n", ns]
        else:
            argv.append("-A")
        argv += ["-o", "yaml"]
        return await _kubectl_plan_execute(argv)


# ─── RBAC ────────────────────────────────────────────────────────────────

@mcp.tool()
async def check_rbac(
    verb: str,
    resource: str,
    namespace: str = "",
) -> str:
    """Check if the current service account can perform an action.
    Examples: check_rbac("list", "pods"), check_rbac("get", "inferenceservices")"""
    verb_v = (verb or "").strip().lower()
    resource_v = (resource or "").strip()
    if not _VERB_RE.fullmatch(verb_v):
        return f"Error: invalid verb {verb!r}"
    if not _NAME_RE.fullmatch(resource_v):
        return f"Error: invalid resource {resource!r}"
    argv = ["auth", "can-i", verb_v, resource_v]
    # `auth can-i -n` selects the evaluation context, not a data scope, so the
    # namespace policy intentionally does not apply here.
    try:
        ns = _validated_namespace(namespace)
        if ns:
            argv += ["-n", ns]
    except ValueError as e:
        return f"Error: {e}"
    # `kubectl auth can-i` exits 1 when the answer is "no" — that answer is
    # the tool's RESULT, not an error; surface stdout whenever it exists.
    rc, out, err = await _kubectl_run(argv)
    if out:
        return _truncate(out)
    if rc != 0:
        return f"Error: {err or f'kubectl exited with code {rc}'}"
    return "(no output)"


# ─── Exec in pod (opt-in, hardened) ──────────────────────────────────────
#
# exec is arbitrary code execution inside a workload container — it cannot be
# made read-only. The hardening is layered, each layer independently
# enforceable:
#   1. tool not registered unless K8S_MCP_EXEC_ENABLED=true (opt-in);
#   2. RBAC: pods/exec `create` granted only by namespaced Roles (manifest) —
#      the API server is the real boundary;
#   3. the target pod must carry label k8s-mcp.io/exec="true" (per-workload
#      opt-in; RBAC cannot match labels, the app can);
#   4. command is an argv list (no shell on this side), one command, whose
#      binary basename must be in the allowlist;
#   5. shells/interpreters/escalation binaries are hard-denied (config cannot
#      re-enable them);
#   6. exec into the MCP server's own pod is refused (its token is the
#      crown jewels);
#   7. non-interactive (no TTY/stdin), 30s timeout, truncated output, and an
#      AUDIT log line for every allow and deny decision.

EXEC_ENABLED_ENV = "K8S_MCP_EXEC_ENABLED"
EXEC_COMMANDS_ENV = "K8S_MCP_EXEC_ALLOWED_COMMANDS"
EXEC_REQUIRE_LABEL_ENV = "K8S_MCP_EXEC_REQUIRE_LABEL"
EXEC_NAMESPACES_ENV = "K8S_MCP_EXEC_NAMESPACES"
EXEC_AUTO_RBAC_ENV = "K8S_MCP_EXEC_AUTO_RBAC"
SA_NAME_ENV = "K8S_MCP_SA_NAME"
SA_NAMESPACE_ENV = "K8S_MCP_SA_NAMESPACE"
EXEC_LABEL = "k8s-mcp.io/exec"
EXEC_TEMPLATE_ROLE = "k8s-mcp-pods-exec"
EXEC_BINDING_NAME = "k8s-mcp-2-0-exec"
_SA_NAMESPACE_FILE = "/var/run/secrets/kubernetes.io/serviceaccount/namespace"
_SA_TOKEN_FILE = "/var/run/secrets/kubernetes.io/serviceaccount/token"

# Fail loud at startup on malformed namespace-policy values (env is static in
# a container; a crashloop beats a silently wrong policy).
_namespace_policy()


def _exec_enabled() -> bool:
    return os.environ.get(EXEC_ENABLED_ENV, "").strip().lower() == "true"


def _exec_auto_rbac() -> bool:
    return os.environ.get(EXEC_AUTO_RBAC_ENV, "true").strip().lower() != "false"


def _exec_namespace_policy():
    """The single exec allowlist: (pattern, ...) or None when unset.

    When set, exec_in_pod works only into these namespaces (same fnmatch-glob
    syntax as the general policy). Unset = exec follows the general namespace
    policy with no extra restriction. RBAC must still bind pods/exec per
    namespace — this list narrows the app layer; the API server remains the
    enforcement boundary.
    """
    raw = os.environ.get(EXEC_NAMESPACES_ENV, "").strip()
    return _parse_ns_patterns(raw) if raw else None


def exec_namespace_violation(namespace: str, key_patterns=None, header_patterns=None):
    """Exec-specific restriction layered over the general namespace policy.

    Checks, in order: the deployment-wide exec list (K8S_MCP_EXEC_NAMESPACES),
    then the caller's per-key assignment (K8S_MCP_CLIENTS), then the
    request's X-Exec-Namespaces narrowing header. A None pattern set means
    "no restriction at that layer".
    """
    ns = (namespace or "").strip().lower()
    if not ns:
        return None
    try:
        deployment = _exec_namespace_policy()
    except ValueError as e:
        return f"exec namespace policy is misconfigured: {e}"
    if deployment is not None and not _pattern_hit(deployment, ns):
        shown = ", ".join(sorted(deployment))[:200]
        return (
            f"namespace '{ns}' is not in the exec namespace list ({shown}); "
            f"extend {EXEC_NAMESPACES_ENV} to debug it"
        )
    for label, patterns in (
        ("the caller's exec assignment", key_patterns),
        (f"the {EXEC_NS_HEADER} header", header_patterns),
    ):
        if patterns is not None and not _pattern_hit(patterns, ns):
            shown = ", ".join(sorted(patterns))[:200]
            return f"namespace '{ns}' is not covered by {label} ({shown})"
    return None


def _own_service_account():
    """(name, namespace) of this pod's ServiceAccount, for self-binding.

    Resolution order: explicit env overrides, then the in-cluster projected
    files (namespace file + the `sub` claim of the SA token). Returns
    (None, None) when neither is available (e.g. bare local runs).
    """
    name = os.environ.get(SA_NAME_ENV, "").strip() or None
    ns = os.environ.get(SA_NAMESPACE_ENV, "").strip() or None
    if not ns and os.path.isfile(_SA_NAMESPACE_FILE):
        try:
            with open(_SA_NAMESPACE_FILE) as f:
                ns = f.read().strip() or None
        except OSError:
            pass
    if not name and os.path.isfile(_SA_TOKEN_FILE):
        try:
            with open(_SA_TOKEN_FILE) as f:
                payload_b64 = f.read().strip().split(".")[1]
            payload_b64 += "=" * (-len(payload_b64) % 4)
            sub = json.loads(base64.urlsafe_b64decode(payload_b64)).get("sub", "")
            # sub: system:serviceaccount:<namespace>:<name>
            if sub.startswith("system:serviceaccount:"):
                _, _, rest = sub.partition("system:serviceaccount:")
                claim_ns, _, claim_name = rest.rpartition(":")
                if claim_name:
                    name = claim_name
                    ns = ns or claim_ns
        except (OSError, ValueError, IndexError):
            pass
    return name, ns


def _all_exec_patterns():
    """Union of the deployment exec list and every per-client assignment —
    the namespace set the RBAC provisioner must cover. None when empty."""
    merged = []
    deployment = _exec_namespace_policy()
    if deployment:
        merged.extend(deployment)
    for _, (_, patterns) in _parse_clients(os.environ.get(CLIENTS_ENV, "")).items():
        if patterns:
            merged.extend(patterns)
    return tuple(dict.fromkeys(merged)) if merged else None


def provision_exec_rbac(patterns=None) -> str:
    """Ensure a pods/exec RoleBinding exists for our SA in every exec namespace.

    Idempotent, startup-time (or `python server.py --provision-rbac`). Covers
    the union of the deployment exec list and all per-client exec assignments
    (per-key scoping is then enforced by the app layer per request). It can
    only bind the EXEC_TEMPLATE_ROLE ClusterRole (the SA's `bind` permission is
    resourceNames-scoped to it) — it cannot create or modify any role content,
    never creates ClusterRoleBindings, never touches a binding it did not
    create, and skips namespaces the general namespace policy blocks.
    Provisioning must never take the server down: any unexpected failure is
    returned as a message instead of raised.
    """
    try:
        return _provision_exec_rbac(patterns)
    except Exception as e:  # noqa: BLE001 — degrade loudly, keep serving
        return f"RBAC provisioning failed (server continues; exec RBAC may be incomplete): {e}"


def _provision_exec_rbac(patterns=None) -> str:
    if patterns is None:
        patterns = _all_exec_patterns()
    if not patterns:
        return (
            f"RBAC provisioning: no exec namespaces configured ({EXEC_NAMESPACES_ENV} "
            f"and {CLIENTS_ENV} assignments) — nothing to do"
        )
    sa_name, sa_ns = _own_service_account()
    if not (sa_name and sa_ns):
        return (
            "RBAC provisioning: cannot determine this pod's ServiceAccount — "
            f"set {SA_NAME_ENV} and {SA_NAMESPACE_ENV}"
        )
    try:
        rbac_v1.read_cluster_role(EXEC_TEMPLATE_ROLE)
    except ApiException as e:
        if e.status == 404:
            return (
                f"RBAC provisioning: template ClusterRole '{EXEC_TEMPLATE_ROLE}' "
                "is missing — apply k8s-mcp-2-0-server.yaml (see DEPLOYMENT.md)"
            )
        return f"RBAC provisioning: cannot read template ClusterRole: {e.reason}"

    targets, skipped = [], []
    for pattern in patterns:
        if any(ch in pattern for ch in "*?"):
            try:
                ns_list = v1.list_namespace()
            except ApiException as e:
                skipped.append((pattern, f"cannot list namespaces: {e.reason}"))
                continue
            matches = sorted(
                n.metadata.name for n in ns_list.items
                if fnmatch.fnmatchcase(n.metadata.name, pattern)
                and _visible(n.metadata.name)
            )
            if matches:
                targets.extend(matches)
            else:
                skipped.append((pattern, "no live namespaces matched"))
        else:
            if not _NS_NAME_RE.fullmatch(pattern):
                skipped.append((pattern, "invalid namespace name"))
                continue
            try:
                v1.read_namespace(pattern)
            except ApiException as e:
                skipped.append((pattern, "namespace not found" if e.status == 404 else e.reason))
                continue
            if not _visible(pattern):
                skipped.append((pattern, "blocked by the general namespace policy"))
                continue
            targets.append(pattern)

    created, updated, kept, refused = [], [], [], []
    for ns in sorted(set(targets)):
        try:
            existing = rbac_v1.read_namespaced_role_binding(EXEC_BINDING_NAME, ns)
        except ApiException as e:
            if e.status != 404:
                refused.append((ns, e.reason))
                continue
            # Plain camelCase dict body: the REST client sends it verbatim, so
            # this has no dependency on kubernetes-client model class names
            # (V1RoleBinding/V1Subject availability varies across versions).
            binding = {
                "apiVersion": "rbac.authorization.k8s.io/v1",
                "kind": "RoleBinding",
                "metadata": {"name": EXEC_BINDING_NAME, "namespace": ns},
                "roleRef": {
                    "apiGroup": "rbac.authorization.k8s.io",
                    "kind": "ClusterRole",
                    "name": EXEC_TEMPLATE_ROLE,
                },
                "subjects": [
                    {"kind": "ServiceAccount", "name": sa_name, "namespace": sa_ns}
                ],
            }
            try:
                rbac_v1.create_namespaced_role_binding(namespace=ns, body=binding)
                created.append(ns)
                print(f"AUDIT rbac provisioning created RoleBinding {EXEC_BINDING_NAME} in {ns}", flush=True)
            except ApiException as e:
                refused.append((ns, e.reason))
            continue
        role_ref = getattr(existing, "role_ref", None)
        if role_ref is not None and getattr(role_ref, "name", "") == EXEC_TEMPLATE_ROLE \
                and getattr(role_ref, "kind", "") == "ClusterRole":
            subjects = getattr(existing, "subjects", None) or []
            subject_ok = any(
                getattr(s, "name", "") == sa_name and getattr(s, "namespace", "") == sa_ns
                for s in subjects
            )
            if subject_ok:
                kept.append(ns)
                continue
            # Binding survived an SA/namespace migration (or predates us) and
            # references a ServiceAccount that is no longer ours — repoint it.
            # Same name + same roleRef, so the patch is ownership-safe.
            try:
                rbac_v1.patch_namespaced_role_binding(
                    name=EXEC_BINDING_NAME, namespace=ns,
                    body={"subjects": [
                        {"kind": "ServiceAccount", "name": sa_name, "namespace": sa_ns}
                    ]},
                )
                updated.append(ns)
                print(
                    f"AUDIT rbac provisioning repointed RoleBinding {EXEC_BINDING_NAME} "
                    f"in {ns} to {sa_ns}/{sa_name}",
                    flush=True,
                )
            except ApiException as e:
                refused.append((ns, e.reason))
        else:
            refused.append((ns, f"a binding named '{EXEC_BINDING_NAME}' with a different roleRef already exists; not touching it"))

    return (
        f"RBAC provisioning: SA {sa_ns}/{sa_name} bound to '{EXEC_TEMPLATE_ROLE}' — "
        f"created: {created or 'none'}; updated: {updated or 'none'}; "
        f"already present: {kept or 'none'}; "
        f"skipped: {skipped or 'none'}; refused: {refused or 'none'}"
    )

# Read-only introspection binaries. Deliberately NO env/printenv (dumps every
# secret the container holds) and NO curl/wget/nc (network exfil channel);
# operators may add such binaries consciously via K8S_MCP_EXEC_ALLOWED_COMMANDS.
_EXEC_DEFAULT_COMMANDS = frozenset({
    "ps", "ls", "cat", "tail", "head", "grep", "df", "du", "free", "uptime",
    "hostname", "id", "whoami", "uname", "date", "stat", "ss", "netstat",
    "ip", "wc", "sleep",
})
# Never runnable, even when allowlisted via env: a shell or interpreter here
# makes the whole allowlist moot; the escalation binaries escape the
# container's boundaries. (Removing entries is a deliberate code change.)
_EXEC_HARD_DENIED = frozenset({
    "sh", "bash", "ash", "dash", "zsh", "fish", "ksh", "csh", "tcsh",
    "python", "python3", "perl", "ruby", "php", "node",
    "su", "sudo", "doas", "nsenter", "unshare", "mount", "setsid",
})
# Argument tokens that turn an allowlisted binary into an arbitrary-execution
# primitive (find -exec) or escape the container's namespaces (ip netns).
_EXEC_TOKEN_DENIED = frozenset({"-exec", "-execdir", "-ok", "-okdir", "netns"})


def _exec_allowed_commands() -> frozenset:
    extra = os.environ.get(EXEC_COMMANDS_ENV, "")
    extra_set = frozenset(p.strip() for p in extra.split(",") if p.strip())
    return _EXEC_DEFAULT_COMMANDS | extra_set


def _exec_require_label() -> bool:
    return os.environ.get(EXEC_REQUIRE_LABEL_ENV, "true").strip().lower() != "false"


def _exec_command_error(command) -> "str | None":
    """Return an error message when the exec command is not allowed, else None."""
    if not isinstance(command, list) or not command:
        return 'command must be a non-empty argv list, e.g. ["ps", "aux"]'
    for elem in command:
        if not isinstance(elem, str) or not elem:
            return "command elements must be non-empty strings"
        if "\x00" in elem:
            return "command elements must not contain NUL bytes"
        if elem in _EXEC_TOKEN_DENIED:
            return f"argument '{elem}' is denied (arbitrary-execution / namespace-escape primitive)"
    binary = command[0].rsplit("/", 1)[-1]
    if binary in _EXEC_HARD_DENIED:
        return (
            f"binary '{binary}' is hard-denied (shells, interpreters, and "
            "privilege/namespace-escalation binaries make the allowlist meaningless)"
        )
    allowed = _exec_allowed_commands()
    if binary not in allowed:
        shown = ", ".join(sorted(allowed))[:200]
        return (
            f"binary '{binary}' is not in the exec allowlist ({shown}...); "
            f"extend {EXEC_COMMANDS_ENV} to add more"
        )
    return None


def _exec_argv(namespace: str, pod_name: str, command: list, container: str) -> list:
    """kubectl exec argv: explicit -n/-c, then the command after `--`.

    Everything after `--` is passed verbatim to the container runtime — kubectl
    flag parsing stops there, so command elements cannot smuggle kubectl flags.
    """
    argv = ["exec", "-n", namespace, pod_name]
    if container:
        argv += ["-c", container]
    return argv + ["--", *command]


if _exec_enabled():

    @mcp.tool()
    async def exec_in_pod(pod_name: str, namespace: str, command: list[str], container: str = "") -> str:
        """Run ONE read-only debugging command inside a container (opt-in tool).

        Examples:
          exec_in_pod("my-pod", "default", ["ps", "aux"])
          exec_in_pod("my-pod", "ml-ns", ["tail", "-n", "50", "/var/log/app.log"], container="worker")
        Hard limits:
          - namespace must satisfy the server's namespace policy;
          - when K8S_MCP_EXEC_NAMESPACES is set, the namespace must also be
            on that exec list;
          - the pod must carry label k8s-mcp.io/exec="true";
          - the binary must be in the exec allowlist (ps, ls, cat, tail, head,
            grep, df, du, free, ss, netstat, ip, ...); shells, interpreters,
            sudo/su, curl/wget and env are not allowed;
          - one-shot, non-interactive (no TTY/stdin), 30s timeout, output
            truncated at 50k chars.
        Every attempt is audit-logged with the calling client's identity. Use
        check_rbac("create", "pods/exec", namespace) to see whether exec is
        permitted at the RBAC layer."""
        caller = _caller_context.get()
        caller_name = caller.name if caller else None
        key_patterns = caller.key_exec_patterns if caller else None
        header_patterns = caller.header_exec_patterns if caller else None

        async def deny(reason: str) -> str:
            print(
                f"AUDIT exec decision=denied client={caller_name or 'shared'} "
                f"pod={namespace}/{pod_name} reason={reason}",
                flush=True,
            )
            return f"Error: {reason}"

        violation = namespace_violation(namespace)
        if violation:
            return await deny(f"namespace policy: {violation}")
        exec_violation = exec_namespace_violation(namespace, key_patterns, header_patterns)
        if exec_violation:
            return await deny(f"exec policy: {exec_violation}")
        try:
            ns = _validated_namespace(namespace)
            pod = _validated_name(pod_name)
            cont = _validated_name(container) if container else ""
        except ValueError as e:
            return await deny(str(e))
        cmd_err = _exec_command_error(command)
        if cmd_err:
            return await deny(cmd_err)
        if pod == os.environ.get("HOSTNAME", ""):
            return await deny(
                "refusing exec into the MCP server's own pod (credential isolation)"
            )
        if _exec_require_label():
            try:
                pod_obj = await asyncio.to_thread(v1.read_namespaced_pod, pod, namespace)
            except ApiException as e:
                return await deny(f"cannot read pod for label check: {e.reason}")
            labels = pod_obj.metadata.labels or {}
            if labels.get(EXEC_LABEL) != "true":
                return await deny(
                    f"pod lacks label {EXEC_LABEL}=\"true\"; label the workload to opt it in to exec"
                )
        print(
            f"AUDIT exec decision=allowed client={caller_name or 'shared'} "
            f"pod={namespace}/{pod} container={container or 'default'} command={command!r}",
            flush=True,
        )
        return await _kubectl_plan_execute(_exec_argv(ns, pod, command, cont))


# ─── kubectl (generic escape hatch) ─────────────────────────────────────

@mcp.tool()
async def run_kubectl(command: str) -> str:
    """Run a read-only kubectl command. This is the most flexible tool.
    Examples:
      run_kubectl("get pods -n default -o wide")
      run_kubectl("get inferenceservices -A")
      run_kubectl("get events -n ml-ns --sort-by=.lastTimestamp")
      run_kubectl("top pods -n default")
      run_kubectl("get nodes -o json")
      run_kubectl("logs deploy/my-app -n default --tail=50")
      run_kubectl("get crd")
      run_kubectl("get virtualservices -A")
      run_kubectl("get destinationrules -n istio-system")
    Only read verbs are allowed: get, describe, logs, top, explain,
    api-resources, api-versions, cluster-info, version, auth, events.
    Everything else (apply, delete, run, exec, port-forward, proxy, ...) is
    rejected, as are connection flags (--server, --token, --kubeconfig, ...).
    Container exec is never available here — when enabled, use the dedicated
    exec_in_pod tool instead.
    Namespace policy applies: namespaced queries need -n; -A/--all-namespaces
    is rewritten per allowed namespace (or rejected under a blacklist-only
    policy)."""
    try:
        argv = _parse_kubectl_command(command)
    except KubectlError as e:
        return f"Error: {e}"
    return await _kubectl_plan_execute(argv)


# ─── HTTP API-key auth (transport layer) ─────────────────────────────────

API_KEY_ENV = "K8S_MCP_API_KEY"          # shared key: deployment-wide exec ceiling
CLIENTS_ENV = "K8S_MCP_CLIENTS"          # per-user keys: "name:key[:exec-ns-patterns];…"
EXEC_NS_HEADER = "x-exec-namespaces"     # optional client-side NARROWING (never widening)
_UNAUTHORIZED_BODY = b'{"error": "unauthorized: missing or invalid API key"}'


class _Caller(NamedTuple):
    """Resolved identity of the request's API key + exec scoping for the call."""
    name: str | None                     # client entry name; None = shared key
    key_exec_patterns: tuple | None      # per-key exec namespace patterns (None = deployment ceiling)
    header_exec_patterns: tuple | None   # X-Exec-Namespaces narrowing (set by middleware)


_caller_context: contextvars.ContextVar = contextvars.ContextVar("mcp_caller", default=None)


def _parse_clients(raw: str) -> dict:
    """Parse K8S_MCP_CLIENTS into {key: (name, exec_patterns | None)}.

    Entries are ';'-separated, fields ':'-separated: `name:key[:ns-patterns]`
    (patterns comma-separated, fnmatch globs). A missing/empty third field
    inherits the deployment-wide exec list. Keys must not contain ':' or ';'.
    """
    entries = {}
    raw = (raw or "").strip()
    if not raw:
        return entries
    for chunk in raw.split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = chunk.split(":")
        if len(parts) < 2 or len(parts) > 3 or not parts[0].strip() or not parts[1].strip():
            raise ValueError(
                f"invalid client entry {chunk!r} in {CLIENTS_ENV}: expected "
                "name:key[:exec-ns-patterns] (keys must not contain ':' or ';')"
            )
        name, key = parts[0].strip(), parts[1].strip()
        patterns_raw = parts[2].strip() if len(parts) > 2 else ""
        patterns = _parse_ns_patterns(patterns_raw) if patterns_raw else None
        entries[key] = (name, patterns)
    return entries


# Fail loud at startup on malformed client configuration.
_parse_clients(os.environ.get(CLIENTS_ENV, ""))


def _header_value(scope, lowercase_name: bytes) -> "str | None":
    for name, value in scope.get("headers", []):
        if name.lower() == lowercase_name:
            return value.decode("latin-1")
    return None


def _authenticate(presented: list, shared: str, clients: dict):
    """Resolve the presented key(s) to a _Caller, or None when unauthorized."""
    for candidate in presented:
        if shared and hmac.compare_digest(candidate.encode("utf-8"), shared.encode("utf-8")):
            return _Caller(name=None, key_exec_patterns=None, header_exec_patterns=None)
        for key, (name, patterns) in clients.items():
            if hmac.compare_digest(candidate.encode("utf-8"), key.encode("utf-8")):
                return _Caller(name=name, key_exec_patterns=patterns, header_exec_patterns=None)
    return None


def _json_asgi_response(send, status: int, body: bytes, extra_headers=()):
    """Minimal ASGI JSON response used by middleware short-circuits."""
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode("ascii")),
        *extra_headers,
    ]
    return [
        {"type": "http.response.start", "status": status, "headers": headers},
        {"type": "http.response.body", "body": body},
    ]


def _presented_api_keys(scope) -> list:
    """Candidate API keys from raw ASGI request headers (all lowercase names).

    Accepts `Authorization: Bearer <key>` and `X-API-Key: <key>`; both are
    collected so clients can use whichever header their MCP client exposes.
    """
    candidates = []
    for name, value in scope.get("headers", []):
        if name.lower() == b"authorization":
            scheme, _, token = value.decode("latin-1").partition(" ")
            if scheme.lower() == "bearer" and token.strip():
                candidates.append(token.strip())
        elif name.lower() == b"x-api-key":
            candidates.append(value.decode("latin-1").strip())
    return candidates


class _ApiKeyAuthMiddleware:
    """Pure-ASGI middleware authenticating every HTTP request.

    - K8S_MCP_API_KEY set: the shared key unlocks the deployment-wide exec list.
    - K8S_MCP_CLIENTS set: per-user keys (name:key[:exec-ns-patterns]) unlock
      their own exec namespace assignment — capabilities travel with the
      credential, so users cannot widen their own scope.
    - Neither set: the endpoint is open (local development mode); `__main__`
      logs a loud warning at startup.
    The optional X-Exec-Namespaces header NARROWS the caller's exec scope for
    that request (intersected with the key's assignment); it can never widen
    it. All comparisons are constant-time (hmac.compare_digest) — treat keys
    like passwords.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        shared = os.environ.get(API_KEY_ENV, "").strip()
        clients = _parse_clients(os.environ.get(CLIENTS_ENV, ""))
        if not shared and not clients:
            await self.app(scope, receive, send)   # auth disabled (dev mode)
            return
        caller = _authenticate(_presented_api_keys(scope), shared, clients)
        if caller is None:
            for message in _json_asgi_response(
                send, 401, _UNAUTHORIZED_BODY, [(b"www-authenticate", b"Bearer")]
            ):
                await send(message)
            return
        header_patterns = None
        header_raw = _header_value(scope, EXEC_NS_HEADER.encode("ascii"))
        if header_raw and header_raw.strip():
            try:
                header_patterns = _parse_ns_patterns(header_raw.strip().lower())
            except ValueError as e:
                for message in _json_asgi_response(
                    send, 400,
                    f'{{"error": "invalid {EXEC_NS_HEADER} header: {e}"}}'.encode("utf-8"),
                ):
                    await send(message)
                return
        _caller_context.set(
            caller._replace(header_exec_patterns=header_patterns)
        )
        await self.app(scope, receive, send)


if __name__ == "__main__":
    import uvicorn

    # Standalone RBAC provisioning mode: bind pods/exec for the exec-list
    # namespaces and exit (no server). Same call the startup auto-provision
    # makes; handy for pre-provisioning or re-running after RBAC drift.
    if "--provision-rbac" in sys.argv:
        print(provision_exec_rbac(), flush=True)
        raise SystemExit(0)

    # Automatic exec RBAC: with exec enabled and any exec namespaces configured
    # (deployment list or per-client assignments), the server ensures its own
    # pods/exec RoleBindings exist before serving. K8S_MCP_EXEC_AUTO_RBAC=false
    # falls back to manual provisioning.
    if _exec_enabled() and _exec_auto_rbac():
        print(provision_exec_rbac(), flush=True)

    # MCP 2.0 / protocol 2026-07-28 is stateless: no initialize handshake,
    # no Mcp-Session-Id header. stateless_http=True keeps 2025-era clients
    # served without a shared session store, so this can sit behind a plain
    # round-robin load balancer.
    if not os.environ.get(API_KEY_ENV, "").strip() and not _parse_clients(os.environ.get(CLIENTS_ENV, "")):
        print(
            "WARNING: neither K8S_MCP_API_KEY nor K8S_MCP_CLIENTS is set — the "
            "MCP endpoint is UNAUTHENTICATED. Configure a key (in-cluster: "
            "from a Secret) before exposing this server.",
            flush=True,
        )
    # Host-header (DNS-rebinding) protection: when MCP_HOSTNAME is set, keep
    # the SDK's protection ON but allowlist the real public hostname. The
    # SDK's implicit default (loopback-only) rejects every gateway-fronted
    # request with 421 "Invalid Host header" AFTER auth — only valid-key
    # requests ever saw it. Without MCP_HOSTNAME (local dev), the SDK's
    # implicit loopback protection applies untouched.
    mcp_hostname = os.environ.get("MCP_HOSTNAME", "").strip()
    transport_security = None
    if mcp_hostname:
        transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[mcp_hostname, "localhost:*", "127.0.0.1:*"],
            allowed_origins=[f"https://{mcp_hostname}"],
        )
    uvicorn.run(
        _ApiKeyAuthMiddleware(mcp.streamable_http_app(
            stateless_http=True, transport_security=transport_security,
        )),
        host="0.0.0.0",
        port=9090,
        log_level="info",
    )
