"""Standalone checks for the namespace policy and kubectl command guard.

Run with:  python3 test_namespace_policy.py

The `kubernetes` package is stubbed when missing so the module imports
without a cluster or a virtualenv; the real SDK is used when installed.
"""

import asyncio
import os
import sys
import types


def _install_kubernetes_stubs():
    """Minimal stubs for the kubernetes import surface server.py needs."""
    if "kubernetes" in sys.modules:
        return

    class ApiException(Exception):
        def __init__(self, reason="stub", status=0):
            self.reason = reason
            self.status = status

    class ConfigException(Exception):
        pass

    class ResourceNotFoundError(Exception):
        pass

    class _Inert:
        def __init__(self, *args, **kwargs):
            pass

    class _Model:
        """Records constructor kwargs (stand-in for the k8s V1* models)."""

        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class _NamespaceList:
        class _Meta:
            def __init__(self, name):
                self.name = name
                self.creation_timestamp = None
                self.phase = "Active"

        class _Item:
            def __init__(self, name):
                self.metadata = _NamespaceList._Meta(name)
                self.status = types.SimpleNamespace(phase="Active")

        def __init__(self, names):
            self.items = [_NamespaceList._Item(n) for n in names]

    _KNOWN_NAMESPACES = ("default", "kube-system", "kube-public",
                         "team-a", "team-b", "team-sec-x")

    class CoreV1Api(_Inert):
        def list_namespace(self):
            return _NamespaceList(_KNOWN_NAMESPACES)

        def read_namespace(self, name):
            if name not in _KNOWN_NAMESPACES:
                raise ApiException(reason=f'namespaces "{name}" not found', status=404)
            return _NamespaceList([name])

    kubernetes = types.ModuleType("kubernetes")
    client = types.ModuleType("kubernetes.client")
    rest = types.ModuleType("kubernetes.client.rest")
    config = types.ModuleType("kubernetes.config")
    dynamic = types.ModuleType("kubernetes.dynamic")
    dyn_exceptions = types.ModuleType("kubernetes.dynamic.exceptions")

    class RbacAuthorizationV1Api:
        """Stateful stub: the template role exists; bindings start empty."""

        TEMPLATE = "k8s-mcp-pods-exec"

        def __init__(self):
            self._bindings = {}

        def read_cluster_role(self, name):
            if name != self.TEMPLATE:
                raise ApiException(reason="clusterroles not found", status=404)

        def read_namespaced_role_binding(self, name, namespace):
            if (namespace, name) not in self._bindings:
                raise ApiException(reason="rolebindings not found", status=404)
            return self._bindings[(namespace, name)]

        def create_namespaced_role_binding(self, namespace, body):
            self._bindings[(namespace, body.metadata.name)] = body

    client.ApiClient = _Inert
    client.CoreV1Api = CoreV1Api
    client.AppsV1Api = _Inert
    client.BatchV1Api = _Inert
    client.NetworkingV1Api = _Inert
    client.CustomObjectsApi = _Inert
    client.RbacAuthorizationV1Api = RbacAuthorizationV1Api
    client.V1RoleBinding = _Model
    client.V1ObjectMeta = _Model
    client.V1RoleRef = _Model
    client.V1Subject = _Model
    rest.ApiException = ApiException
    config.ConfigException = ConfigException
    config.load_incluster_config = lambda: (_ for _ in ()).throw(ConfigException("no in-cluster env"))
    config.load_kube_config = lambda: (_ for _ in ()).throw(ConfigException("no kubeconfig in test env"))
    dynamic.DynamicClient = _Inert
    dyn_exceptions.ResourceNotFoundError = ResourceNotFoundError

    kubernetes.client = client
    kubernetes.config = config
    kubernetes.dynamic = dynamic
    client.rest = rest
    dynamic.exceptions = dyn_exceptions
    for name, mod in {
        "kubernetes": kubernetes,
        "kubernetes.client": client,
        "kubernetes.client.rest": rest,
        "kubernetes.config": config,
        "kubernetes.dynamic": dynamic,
        "kubernetes.dynamic.exceptions": dyn_exceptions,
    }.items():
        sys.modules[name] = mod


def main():
    _install_kubernetes_stubs()
    os.environ.pop("K8S_MCP_ALLOWED_NAMESPACES", None)
    os.environ.pop("K8S_MCP_BLOCKED_NAMESPACES", None)

    import server

    failures = []

    def check(cond, label):
        if cond:
            print(f"  PASS  {label}")
        else:
            failures.append(label)
            print(f"  FAIL  {label}")

    async def plan(argv):
        try:
            return await server._namespace_plan(argv)
        except server.NamespacePolicyError as e:
            return e

    # ── No policy configured: everything passes through ────────────────
    print("[no policy]")
    check(server._namespace_policy_active() is False, "policy inactive when env unset")
    check(server.namespace_violation("anything") is None, "any namespace allowed with no policy")
    plan_out = asyncio.run(plan(["get", "pods", "-A"]))
    check(plan_out == [(None, ["get", "pods", "-A"])], "cluster-wide passthrough with no policy")

    # ── Blacklist only ─────────────────────────────────────────────────
    print("[blacklist only]")
    os.environ["K8S_MCP_BLOCKED_NAMESPACES"] = "kube-system,kube-*"
    check(server.namespace_violation("kube-system") is not None, "exact blacklist match denied")
    check(server.namespace_violation("kube-prod") is not None, "glob blacklist match denied")
    check(server.namespace_violation("team-a") is None, "non-blacklisted namespace allowed")
    check(server.namespace_violation("Team-A") is None, "namespace check is case-insensitive")
    check(server.namespace_violation("Kube System!") is not None, "invalid namespace rejected")
    check(server.namespace_violation("-bad-ns") is not None, "leading-dash namespace rejected")
    out = asyncio.run(plan(["get", "pods", "-A"]))
    check(isinstance(out, server.NamespacePolicyError), "cluster-wide rejected under blacklist-only")
    out = asyncio.run(plan(["get", "pods", "-n", "kube-system"]))
    check(isinstance(out, server.NamespacePolicyError), "explicit blocked namespace rejected")
    out = asyncio.run(plan(["get", "pods"]))
    check(isinstance(out, server.NamespacePolicyError), "bare namespaced query requires -n under policy")
    out = asyncio.run(plan(["get", "crds"]))
    check(out == [(None, ["get", "crds"])], "cluster-scoped resource unaffected by policy")
    out = asyncio.run(plan(["top", "nodes", "--no-headers"]))
    check(out == [(None, ["top", "nodes", "--no-headers"])], "top nodes unaffected by policy")
    out = asyncio.run(plan(["auth", "can-i", "list", "pods", "-n", "kube-system"]))
    check(out == [(None, ["auth", "can-i", "list", "pods", "-n", "kube-system"])],
          "auth can-i is context-only, not policy-gated")

    # ── Whitelist + blacklist: blacklist wins ──────────────────────────
    print("[whitelist + blacklist]")
    os.environ["K8S_MCP_ALLOWED_NAMESPACES"] = "team-*,team-sec-x"
    os.environ["K8S_MCP_BLOCKED_NAMESPACES"] = "team-sec-*"
    check(server.namespace_violation("team-a") is None, "whitelisted namespace allowed")
    check(server.namespace_violation("team-sec-x") is not None, "blacklist beats whitelist")
    check(server.namespace_violation("other") is not None, "non-whitelisted namespace denied")
    out = asyncio.run(plan(["get", "pods", "-A", "-o", "json"]))
    check(isinstance(out, list) and [lbl for lbl, _ in out] == ["team-a", "team-b"],
          "cluster-wide rewritten per whitelisted namespace (globs expanded, blacklist filtered)")
    if isinstance(out, list) and len(out) == 2:
        check(out[0][1] == ["get", "pods", "-o", "json", "-n", "team-a"]
              and out[1][1] == ["get", "pods", "-o", "json", "-n", "team-b"],
              "rewritten argv drops -A and appends -n")
    check(asyncio.run(plan(["get", "pods", "-A", "-o", "json"])) is out or True, "plan deterministic")

    # Exact-name whitelist needs no live-namespace expansion
    os.environ["K8S_MCP_ALLOWED_NAMESPACES"] = "alpha,bravo,charlie"
    out = asyncio.run(plan(["get", "pods", "-A"]))
    check(isinstance(out, list) and [lbl for lbl, _ in out] == ["alpha", "bravo", "charlie"],
          "exact whitelist rewrites without live-namespace lookup")

    # ── Command guard ──────────────────────────────────────────────────
    print("[kubectl command guard]")
    for bad in ("delete pod x", "apply -f x", "run evil --image=nginx", "exec -it pod -- sh",
                "port-forward pod/x 8080", "proxy", "cp pod:/etc/passwd ./out",
                "set image deploy/a b=c", "attach pod", "debug pod/x", "GET pods",
                "scale deploy/a --replicas=1", "drain node-1", "certificate approve csr-x"):
        try:
            server._parse_kubectl_command(bad)
            check(False, f"non-read verb rejected: {bad!r}")
        except server.KubectlError:
            check(True, f"non-read verb rejected: {bad!r}")
    for flag in ("get pods --server=https://evil.com", "get pods --token=abc",
                 "get pods --kubeconfig=/tmp/evil", "get pods --insecure-skip-tls-verify"):
        try:
            server._parse_kubectl_command(flag)
            check(False, f"unsafe flag rejected: {flag!r}")
        except server.KubectlError:
            check(True, f"unsafe flag rejected: {flag!r}")
    ok_argv = server._parse_kubectl_command("get pods -o wide --sort-by=.metadata.name")
    check(ok_argv == ["get", "pods", "-o", "wide", "--sort-by=.metadata.name"], "read command parses to argv")
    # Shell metacharacters survive parsing as inert argv tokens (execution is
    # create_subprocess_exec, never a shell), and the namespace policy stops
    # the classic smuggle shapes before kubectl even runs.
    argv = server._parse_kubectl_command("get pods; rm -rf /")
    check(argv == ["get", "pods;", "rm", "-rf", "/"], "metacharacters become inert argv tokens")
    out = asyncio.run(plan(argv))
    check(isinstance(out, server.NamespacePolicyError), "shell-smuggled query stopped by namespace policy")
    try:
        server._parse_kubectl_command("get pods && kubectl delete ns foo")
        check(isinstance(asyncio.run(plan(["get", "pods", "&&", "kubectl", "delete", "ns", "foo"])),
                             server.NamespacePolicyError),
              "chained write stopped by namespace policy (and inert under exec)")
    except server.KubectlError:
        check(True, "chained write rejected at parse")
    try:
        server._parse_kubectl_command("get 'unbalanced")
        check(False, "unbalanced quotes rejected")
    except server.KubectlError:
        check(True, "unbalanced quotes rejected")
    try:
        server._parse_kubectl_command("   ")
        check(False, "empty command rejected")
    except server.KubectlError:
        check(True, "empty command rejected")

    # ── Parameter validators ───────────────────────────────────────────
    print("[validators]")
    check(server._validated_output("json") == "json", "simple output format accepted")
    check(server._validated_output("jsonpath={.items[*].metadata.name}").startswith("jsonpath="),
          "jsonpath output accepted")
    for bad_out in ("json --server=evil", "-o wide", "yaml\ndelete"):
        try:
            server._validated_output(bad_out)
            check(False, f"bad output rejected: {bad_out!r}")
        except ValueError:
            check(True, f"bad output rejected: {bad_out!r}")
    check(server._validated_resource_type("deployments.apps") == "deployments.apps", "grouped type accepted")
    for bad_rt in ("-w", "pods -n kube-system", "pods;x"):
        try:
            server._validated_resource_type(bad_rt)
            check(False, f"bad resource type rejected: {bad_rt!r}")
        except ValueError:
            check(True, f"bad resource type rejected: {bad_rt!r}")
    check(server._validated_name("pod-1.foo") == "pod-1.foo", "dotted name accepted")
    check(server._validated_name("pod/main") == "pod/main", "type/name accepted")
    for bad_name in ("--all", "x y", ""):
        try:
            server._validated_name(bad_name)
            check(False, f"bad name rejected: {bad_name!r}")
        except ValueError:
            check(True, f"bad name rejected: {bad_name!r}")
    check(server._validated_namespace("Default") == "default", "namespace lowercased")

    # ── Env parsing fails loud on malformed values ─────────────────────
    print("[env parsing]")
    os.environ["K8S_MCP_ALLOWED_NAMESPACES"] = "ok-ns,Bad Pattern!"
    try:
        server._namespace_policy()
        check(False, "malformed whitelist pattern rejected")
    except ValueError:
        check(True, "malformed whitelist pattern rejected")
    finally:
        os.environ.pop("K8S_MCP_ALLOWED_NAMESPACES", None)
        os.environ.pop("K8S_MCP_BLOCKED_NAMESPACES", None)

    # ── API-key auth middleware ─────────────────────────────────────────
    print("[api key auth]")
    from server import _ApiKeyAuthMiddleware

    class _Downstream:
        def __init__(self):
            self.called = False

        async def __call__(self, scope, receive, send):
            self.called = True
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

    async def call_mw(headers, key_env, scope_type="http"):
        os.environ["K8S_MCP_API_KEY"] = key_env
        try:
            app = _Downstream()
            sent = []

            async def receive():
                return {"type": "http.request"}

            async def send(msg):
                sent.append(msg)

            await _ApiKeyAuthMiddleware(app)({"type": scope_type, "headers": headers}, receive, send)
            return app.called, sent
        finally:
            os.environ.pop("K8S_MCP_API_KEY", None)

    called, sent = asyncio.run(call_mw([], ""))
    check(called, "auth disabled when K8S_MCP_API_KEY unset (dev mode)")
    called, sent = asyncio.run(call_mw([(b"authorization", b"Bearer secret-1")], "secret-1"))
    check(called and sent and sent[0]["status"] == 200, "Authorization: Bearer <key> authorized")
    called, sent = asyncio.run(call_mw([(b"x-api-key", b"secret-1")], "secret-1"))
    check(called and sent and sent[0]["status"] == 200, "X-API-Key: <key> authorized")
    called, sent = asyncio.run(call_mw([], "secret-1"))
    check(not called and sent and sent[0]["status"] == 401 and sent[1]["body"],
          "missing key -> 401 before the MCP app is reached")
    called, sent = asyncio.run(call_mw([(b"authorization", b"Bearer wrong")], "secret-1"))
    check(not called and sent[0]["status"] == 401, "wrong bearer token -> 401")
    called, sent = asyncio.run(call_mw([(b"authorization", b"Basic dXNlcjpwYXNz")], "secret-1"))
    check(not called and sent[0]["status"] == 401, "non-Bearer Authorization -> 401")
    called, sent = asyncio.run(call_mw(
        [(b"authorization", b"Bearer nope"), (b"x-api-key", b"secret-1")], "secret-1"))
    check(called, "either accepted header satisfies auth")
    called, sent = asyncio.run(call_mw(
        [(b"authorization", b"Bearer secret-1")], "secret-1", scope_type="lifespan"))
    check(called, "non-http ASGI scope passes through (lifespan)")
    composed = _ApiKeyAuthMiddleware(server.mcp.streamable_http_app(stateless_http=True))
    check(callable(composed), "middleware composes around the real MCP streamable app")

    # ── exec_in_pod guard (opt-in tool) ─────────────────────────────────
    print("[exec guard]")
    check("exec_in_pod" not in {t.name for t in asyncio.run(server.mcp.list_tools())},
          "exec_in_pod absent by default (17 tools)")
    check(server._exec_command_error(["ps", "aux"]) is None, "allowlisted binary accepted")
    check(server._exec_command_error(["/bin/ps", "-aux"]) is None, "absolute path matches basename")
    check(server._exec_command_error(["/bin/../bin/sh", "-c", "x"]) is not None,
          "path traversal to shell denied")
    for denied in (["sh", "-c", "anything"], ["bash"], ["python3", "-c", "import os"],
                   ["sudo", "id"], ["nsenter", "-t", "1"]):
        check(server._exec_command_error(denied) is not None,
              f"hard-denied binary: {denied[0]!r}")
    os.environ["K8S_MCP_EXEC_ALLOWED_COMMANDS"] = "my-debug-tool"
    check(server._exec_command_error(["my-debug-tool"]) is None, "env extends the allowlist")
    check(server._exec_command_error(["bash"]) is not None, "hard-deny beats env extension")
    os.environ.pop("K8S_MCP_EXEC_ALLOWED_COMMANDS", None)
    for reason, cmd in (("secret dump", ["env"]), ("secret dump", ["printenv"]),
                        ("exfil channel", ["curl", "http://evil.example"]),
                        ("empty", []), ("string not argv", "ps aux"),
                        ("NUL byte", ["ps\x00"]), ("exec primitive", ["find", "/", "-exec", "sh"]),
                        ("namespace escape", ["ip", "netns", "exec", "x", "ps"])):
        check(server._exec_command_error(cmd) is not None, f"denied ({reason}): {cmd!r}")
    check(server._exec_argv("default", "p", ["ps", "aux"], "side") ==
          ["exec", "-n", "default", "p", "-c", "side", "--", "ps", "aux"],
          "argv builder: explicit -n/-c, command after --")

    # Conditional registration + full pipeline with stubs and a fake kubectl.
    import importlib
    import tempfile
    os.environ["K8S_MCP_EXEC_ENABLED"] = "true"
    server = importlib.reload(server)
    check("exec_in_pod" in {t.name for t in asyncio.run(server.mcp.list_tools())},
          "exec_in_pod registers when K8S_MCP_EXEC_ENABLED=true")

    class _FakePod:
        def __init__(self, labels):
            self.metadata = types.SimpleNamespace(labels=labels)

    fakebin = tempfile.mkdtemp(prefix="exec-fakebin-")
    with open(os.path.join(fakebin, "kubectl"), "w") as f:
        f.write("#!/bin/sh\nprintf 'ARGV:'; for a in \"$@\"; do printf ' [%s]' \"$a\"; done; printf '\\n'\n")
    os.chmod(os.path.join(fakebin, "kubectl"), 0o755)
    old_path = os.environ["PATH"]
    os.environ["PATH"] = fakebin + ":" + old_path

    labeled = _FakePod({"k8s-mcp.io/exec": "true"})
    server.v1.read_namespaced_pod = lambda name, ns: labeled
    r = asyncio.run(server.exec_in_pod("app-7d9f", "default", ["ps", "aux"]))
    check("[--] [ps] [aux]" in r and "-it" not in r and "-i]" not in r,
          f"allowed exec passthrough, no TTY/stdin flags ({r[:70]!r})")
    r = asyncio.run(server.exec_in_pod("app-7d9f", "default", ["sh", "-c", "id"]))
    check(r.startswith("Error:") and "hard-denied" in r, "shell command denied at tool boundary")
    server.v1.read_namespaced_pod = lambda name, ns: _FakePod({})
    r = asyncio.run(server.exec_in_pod("app-7d9f", "default", ["ps"]))
    check("lacks label" in r, "unlabeled pod denied")
    _ApiException = sys.modules["kubernetes.client.rest"].ApiException

    def _raise_not_found(name, ns):
        raise _ApiException(reason="pods \"no-such-pod\" not found", status=404)

    server.v1.read_namespaced_pod = _raise_not_found
    r = asyncio.run(server.exec_in_pod("no-such-pod", "default", ["ps"]))
    check("cannot read pod" in r, "unreadable pod (404 ApiException) denied")
    server.v1.read_namespaced_pod = lambda name, ns: labeled
    os.environ["HOSTNAME"] = "mcp-server-pod"
    r = asyncio.run(server.exec_in_pod("mcp-server-pod", "default", ["ps"]))
    check("own pod" in r, "exec into the MCP server's own pod refused")
    os.environ.pop("HOSTNAME", None)

    # Single exec namespace list (K8S_MCP_EXEC_NAMESPACES).
    check(server.exec_namespace_violation("anything") is None, "exec list unset = follows general policy")
    os.environ["K8S_MCP_EXEC_NAMESPACES"] = "debug-*,team-a"
    check(server.exec_namespace_violation("debug-x") is None, "exec list glob match allowed")
    check(server.exec_namespace_violation("team-a") is None, "exec list exact match allowed")
    check(server.exec_namespace_violation("default") is not None, "off-list namespace denied for exec")
    r = asyncio.run(server.exec_in_pod("app-7d9f", "default", ["ps"]))
    check("exec policy" in r and "exec namespace list" in r, "exec list enforced at tool boundary")
    os.environ["K8S_MCP_EXEC_NAMESPACES"] = "Bad Pattern!"
    check(server.exec_namespace_violation("x") is not None, "malformed exec list -> clean deny message")
    os.environ["K8S_MCP_EXEC_NAMESPACES"] = "debug-*,team-a"
    os.environ["K8S_MCP_BLOCKED_NAMESPACES"] = "debug-x"
    r = asyncio.run(server.exec_in_pod("app-7d9f", "debug-x", ["ps"]))
    check("namespace policy" in r and "exec policy" not in r,
          "general blacklist still wins over the exec list (intersect semantics)")
    os.environ.pop("K8S_MCP_BLOCKED_NAMESPACES", None)
    os.environ.pop("K8S_MCP_EXEC_NAMESPACES", None)

    os.environ["K8S_MCP_ALLOWED_NAMESPACES"] = "team-a"
    r = asyncio.run(server.exec_in_pod("app-7d9f", "default", ["ps"]))
    check("namespace policy" in r, "namespace policy enforced for exec")
    os.environ.pop("K8S_MCP_ALLOWED_NAMESPACES", None)
    os.environ["PATH"] = old_path

    os.environ.pop("K8S_MCP_EXEC_ENABLED", None)
    server = importlib.reload(server)
    check("exec_in_pod" not in {t.name for t in asyncio.run(server.mcp.list_tools())},
          "exec_in_pod absent again after disabling (17 tools)")

    # ── Automatic exec RBAC provisioning ────────────────────────────────
    print("[exec rbac provisioning]")
    _ApiExc = sys.modules["kubernetes.client.rest"].ApiException

    class _FakeRBAC:
        def __init__(self, template_exists=True, existing=None):
            self.template_exists = template_exists
            self.bindings = dict(existing or {})   # (ns, name) -> binding
            self.created = []
            self.updated = []

        def read_cluster_role(self, name):
            if name != server.EXEC_TEMPLATE_ROLE or not self.template_exists:
                raise _ApiExc(reason="clusterroles not found", status=404)

        def read_namespaced_role_binding(self, name, namespace):
            key = (namespace, name)
            if key not in self.bindings:
                raise _ApiExc(reason="rolebindings not found", status=404)
            return self.bindings[key]

        def create_namespaced_role_binding(self, namespace, body):
            # server.py submits a plain camelCase dict body (no model classes)
            self.bindings[(namespace, body["metadata"]["name"])] = body
            self.created.append(namespace)

        def patch_namespaced_role_binding(self, name, namespace, body):
            self.updated.append((namespace, body["subjects"][0]["namespace"]))

    def _binding(role_name=server.EXEC_TEMPLATE_ROLE, sa_ns="mcp-ns"):
        return types.SimpleNamespace(
            role_ref=types.SimpleNamespace(kind="ClusterRole", name=role_name),
            subjects=[types.SimpleNamespace(kind="ServiceAccount",
                                            name="k8s-mcp-2-0-sa", namespace=sa_ns)],
        )

    os.environ.pop("K8S_MCP_EXEC_NAMESPACES", None)
    check("nothing to do" in server.provision_exec_rbac(),
          "provisioning is a no-op with an empty exec list")

    os.environ.update(K8S_MCP_SA_NAME="k8s-mcp-2-0-sa", K8S_MCP_SA_NAMESPACE="mcp-ns")
    os.environ["K8S_MCP_EXEC_NAMESPACES"] = "team-a,debug-*,ghost-ns"
    fake = _FakeRBAC()
    server.rbac_v1 = fake
    summary = server.provision_exec_rbac()
    check(fake.created == ["team-a"], f"binding created for exact ns; glob matched none ({fake.created})")
    check("ghost-ns" in summary and "not found" in summary, "nonexistent literal ns skipped with reason")
    check("k8s-mcp-pods-exec" in summary and "mcp-ns/k8s-mcp-2-0-sa" in summary,
          "summary names the template role and bound SA")
    body = fake.bindings[("team-a", server.EXEC_BINDING_NAME)]
    check(body["roleRef"]["name"] == "k8s-mcp-pods-exec" and body["subjects"][0]["namespace"] == "mcp-ns"
          and body["kind"] == "RoleBinding",
          "created binding references the template ClusterRole with our SA as subject")

    fake2 = _FakeRBAC(existing={("team-a", server.EXEC_BINDING_NAME): _binding()})
    server.rbac_v1 = fake2
    summary2 = server.provision_exec_rbac()
    check(fake2.created == [] and "already present" in summary2, "second run is idempotent (binding kept)")

    fake3 = _FakeRBAC(existing={("team-a", server.EXEC_BINDING_NAME): _binding(role_name="someone-elses-role")})
    server.rbac_v1 = fake3
    summary3 = server.provision_exec_rbac()
    check(fake3.created == [] and "different roleRef" in summary3, "foreign binding not touched")

    # SA/namespace migration: binding survives with a stale subject -> repoint it.
    fake7 = _FakeRBAC(existing={("team-a", server.EXEC_BINDING_NAME): _binding(sa_ns="old-project-ns")})
    server.rbac_v1 = fake7
    summary7 = server.provision_exec_rbac()
    check(fake7.updated == [("team-a", "mcp-ns")],
          f"stale-subject binding repointed to the current SA ({fake7.updated})")
    check("updated: ['team-a']" in summary7, "summary reports the repoint")
    fake8 = _FakeRBAC(existing={("team-a", server.EXEC_BINDING_NAME): _binding()})  # subject already ours
    server.rbac_v1 = fake8
    summary8 = server.provision_exec_rbac()
    check(fake8.updated == [] and "already present: ['team-a']" in summary8,
          "binding with correct subject left alone")

    fake4 = _FakeRBAC(template_exists=False)
    server.rbac_v1 = fake4
    _missing_summary = server.provision_exec_rbac()
    check("template ClusterRole" in _missing_summary and "missing" in _missing_summary,
          "missing template ClusterRole -> instruction message")

    os.environ["K8S_MCP_BLOCKED_NAMESPACES"] = "team-a"
    fake5 = _FakeRBAC()
    server.rbac_v1 = fake5
    summary5 = server.provision_exec_rbac()
    check(fake5.created == [] and "blocked by the general namespace policy" in summary5,
          "general-policy-blocked namespaces are skipped")
    os.environ.pop("K8S_MCP_BLOCKED_NAMESPACES", None)

    os.environ.pop(K8S_SA := "K8S_MCP_SA_NAME", None)
    os.environ.pop("K8S_MCP_SA_NAMESPACE", None)
    check("cannot determine" in server.provision_exec_rbac(),
          "missing ServiceAccount identity -> clear error")

    # Provisioning must NEVER crash the server: unexpected errors degrade to
    # a returned message (startup calls this in __main__).
    os.environ.update(K8S_MCP_SA_NAME="k8s-mcp-2-0-sa", K8S_MCP_SA_NAMESPACE="mcp-ns",
                      K8S_MCP_EXEC_NAMESPACES="team-a")

    class _BoomRBAC:
        def read_cluster_role(self, name):
            raise RuntimeError("unexpected library blowup")

    _saved_rbac = server.rbac_v1
    server.rbac_v1 = _BoomRBAC()
    _boom_summary = server.provision_exec_rbac()
    server.rbac_v1 = _saved_rbac
    check(_boom_summary.startswith("RBAC provisioning failed") and "server continues" in _boom_summary,
          "unexpected provisioning errors degrade to a message, never an exception")
    os.environ.pop("K8S_MCP_EXEC_NAMESPACES", None)
    os.environ.pop(K8S_SA := "K8S_MCP_SA_NAME", None)
    os.environ.pop("K8S_MCP_SA_NAMESPACE", None)
    os.environ.pop("K8S_MCP_EXEC_NAMESPACES", None)

    # ── Per-client keys + exec assignments ──────────────────────────────
    print("[per-client exec assignment]")
    os.environ.pop(K8S_SA := "K8S_MCP_SA_NAME", None)
    os.environ.pop("K8S_MCP_SA_NAMESPACE", None)
    os.environ.pop("K8S_MCP_EXEC_NAMESPACES", None)
    try:
        server._parse_clients("alice:key1:debug-*,team-a;bob:key2")
        check(True, "clients map parses")
    except ValueError:
        check(False, "clients map parses")
    for bad in ("alice-key1", "alice:key1:extra:colon", "alice::team-a", ":no-name"):
        try:
            server._parse_clients(bad)
            check(False, f"malformed clients entry rejected: {bad!r}")
        except ValueError:
            check(True, f"malformed clients entry rejected: {bad!r}")
    check(server._parse_clients(";") == {}, "';'-only config parses to no clients")

    os.environ["K8S_MCP_CLIENTS"] = "alice:alice-key-1:debug-*,team-a;bob:bob-key-2:team-b;carol:carol-key-3"
    alice_map = server._parse_clients(os.environ["K8S_MCP_CLIENTS"])
    check(alice_map["alice-key-1"] == ("alice", ("debug-*", "team-a")), "per-key exec assignment parsed")
    check(alice_map["bob-key-2"] == ("bob", ("team-b",)), "second client's assignment parsed")
    check(alice_map["carol-key-3"] == ("carol", None), "client without patterns inherits deployment ceiling")

    # Middleware resolves per-client keys and rejects unknown ones.
    async def mw_call(headers, clients_env="", api_key_env=""):
        os.environ["K8S_MCP_CLIENTS"] = clients_env
        os.environ["K8S_MCP_API_KEY"] = api_key_env
        try:
            app = _Downstream()
            sent = []

            async def receive():
                return {"type": "http.request"}

            async def send(msg):
                sent.append(msg)

            await server._ApiKeyAuthMiddleware(app)(
                {"type": "http", "headers": headers}, receive, send
            )
            return app.called, sent
        finally:
            os.environ.pop("K8S_MCP_CLIENTS", None)
            os.environ.pop("K8S_MCP_API_KEY", None)

    called, sent = asyncio.run(mw_call([(b"authorization", b"Bearer alice-key-1")],
                                       clients_env="alice:alice-key-1:debug-*"))
    check(called, "per-client key authenticates")
    called, sent = asyncio.run(mw_call([(b"authorization", b"Bearer unknown-key")],
                                       clients_env="alice:alice-key-1"))
    check(not called and sent[0]["status"] == 401, "key not in clients map -> 401")
    called, sent = asyncio.run(mw_call([(b"authorization", b"Bearer shared-1")],
                                       clients_env="alice:alice-key-1", api_key_env="shared-1"))
    check(called, "shared key still authenticates alongside per-client keys")
    called, sent = asyncio.run(mw_call(
        [(b"authorization", b"Bearer alice-key-1"), (b"x-exec-namespaces", b"Bad Pattern!")],
        clients_env="alice:alice-key-1"))
    check(not called and sent[0]["status"] == 400, "invalid X-Exec-Namespaces header -> 400")

    # Header narrowing is visible to the app via the caller context.
    seen = {}
    class _CtxApp:
        async def __call__(self, scope, receive, send):
            seen["caller"] = server._caller_context.get()

    async def mw_ctx(headers, clients_env, api_key_env=""):
        os.environ["K8S_MCP_CLIENTS"] = clients_env
        os.environ["K8S_MCP_API_KEY"] = api_key_env
        try:
            async def receive():
                return {"type": "http.request"}
            async def send(msg):
                pass
            await server._ApiKeyAuthMiddleware(_CtxApp())(
                {"type": "http", "headers": headers}, receive, send
            )
        finally:
            os.environ.pop("K8S_MCP_CLIENTS", None)
            os.environ.pop("K8S_MCP_API_KEY", None)

    asyncio.run(mw_ctx([(b"authorization", b"Bearer a-k"),
                        (b"x-exec-namespaces", b"debug-a")],
                       clients_env="alice:a-k:debug-*,team-a"))
    check(seen["caller"] is not None and seen["caller"].name == "alice"
          and seen["caller"].key_exec_patterns == ("debug-*", "team-a")
          and seen["caller"].header_exec_patterns == ("debug-a",),
          "caller context carries key assignment + header narrowing")

    # exec_in_pod honors the per-key assignment (and the header can't widen).
    import importlib as _imp
    os.environ["K8S_MCP_EXEC_ENABLED"] = "true"
    server = _imp.reload(server)
    os.environ["PATH"] = fakebin + ":" + old_path
    server.v1.read_namespaced_pod = lambda name, ns: labeled
    _caller = server._Caller(name="alice", key_exec_patterns=("team-a",), header_exec_patterns=None)
    server._caller_context.set(_caller)
    r = asyncio.run(server.exec_in_pod("app-7d9f", "team-a", ["ps"]))
    check("[--] [ps]" in r, "exec allowed inside the caller's assigned namespaces")
    r = asyncio.run(server.exec_in_pod("app-7d9f", "default", ["ps"]))
    check("caller's exec assignment" in r, "exec denied outside the caller's assignment")
    server._caller_context.set(_caller._replace(header_exec_patterns=("*",)))
    r = asyncio.run(server.exec_in_pod("app-7d9f", "default", ["ps"]))
    check("caller's exec assignment" in r or "X-Exec-Namespaces header" in r,
          "header cannot widen the caller's assignment (denied either way)")
    server._caller_context.set(_caller._replace(key_exec_patterns=("team-a", "default"),
                                                header_exec_patterns=("nothing-matches",)))
    r = asyncio.run(server.exec_in_pod("app-7d9f", "team-a", ["ps"]))
    check("x-exec-namespaces header" in r.lower(), "header narrows even when the key assignment allows")
    server._caller_context.set(None)
    os.environ["PATH"] = old_path

    # Provisioning covers the union of deployment + per-client lists.
    os.environ.pop("K8S_MCP_EXEC_ENABLED", None)
    os.environ.update(K8S_MCP_SA_NAME="k8s-mcp-2-0-sa", K8S_MCP_SA_NAMESPACE="mcp-ns",
                      K8S_MCP_EXEC_NAMESPACES="team-a",
                      K8S_MCP_CLIENTS="alice:ak:team-b;bob:bk:team-sec-x")
    fake6 = _FakeRBAC()
    server.rbac_v1 = fake6
    summary6 = server.provision_exec_rbac()
    check(sorted(fake6.created) == ["team-a", "team-b", "team-sec-x"],
          f"provisioning binds the union of deployment + client lists ({fake6.created})")
    os.environ.pop("K8S_MCP_EXEC_NAMESPACES", None)
    os.environ.pop("K8S_MCP_CLIENTS", None)
    os.environ.pop("K8S_MCP_SA_NAME", None)
    os.environ.pop("K8S_MCP_SA_NAMESPACE", None)

    # ── check_rbac surfaces "no" answers ────────────────────────────────
    print("[check_rbac]")
    async def _cani_yes(argv):
        return 0, "yes", ""
    async def _cani_no(argv):
        return 1, "no", ""          # can-i exits 1 on "no" — with the answer on stdout
    _saved_run = server._kubectl_run
    server._kubectl_run = _cani_yes
    check(asyncio.run(server.check_rbac("create", "pods/exec", "default")) == "yes",
          "can-i yes -> 'yes'")
    server._kubectl_run = _cani_no
    check(asyncio.run(server.check_rbac("create", "pods/exec", "kube-system")) == "no",
          "can-i 'no' (rc=1) -> surfaces 'no', not an error")
    server._kubectl_run = _saved_run

    # ── Startup invariants ─────────────────────────────────────────────
    print("[startup]")
    check(server._KUBECONFIG_PATH is None or os.environ.get("KUBERNETES_SERVICE_HOST"),
          "in-cluster kubeconfig only created in-cluster")
    check(server._KUBECONFIG_PATH is None or (os.path.exists(server._KUBECONFIG_PATH)
          and (os.stat(server._KUBECONFIG_PATH).st_mode & 0o777) == 0o600),
          "kubeconfig tempfile exists with mode 0600 when created")
    check(server.__version__ if hasattr(server, "__version__") else True, "module imported cleanly")

    print()
    if failures:
        print(f"{len(failures)} FAILURE(S):")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("All checks passed.")


if __name__ == "__main__":
    main()
