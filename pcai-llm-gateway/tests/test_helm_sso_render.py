"""SSO self-mint rollout — HELM RENDER tests (zone Z5).

These pin the CHART side of the SSO self-mint rollout (the app side is
covered by test_identity_phaseb.py / test_sso_selfmint.py / test_ui.py):

1. FLAG-OFF BYTE-IDENTITY (the cross-cutting invariant): the default
   render and the hosted-trial render are byte-identical to the chart
   WITHOUT the Z5 additive block — only the new conditional keys
   (allowMetricsScrape / metricsNamespace) exist, and with them off
   nothing new renders. The baseline is captured at module import by
   rendering from a THROWAWAY copy of the chart with the new template
   lines stripped (self-computed oracle — no committed snapshot to go
   stale; the invariant check is a whole-file byte diff of every doc).
2. METRICS RELIEF RULE — with ezua.authorizationPolicy.enabled +
   allowMetricsScrape=true, the AuthorizationPolicy gains exactly ONE
   extra ALLOW rule: from metricsNamespace (default "prometheus"),
   port-restricted to the SERVICE port. Without it, the /metrics
   scrapes die when the policy enforces (verified live 2026-10-06: the
   prometheus namespace scrapes pcai-llm — the ServiceMonitor targets
   the same service port the policy gates).
3. REPLACE SEMANTICS preserved — operator-supplied `rules` still REPLACE
   the baseline (the metrics rule must NOT leak into the operator path).
4. VALUES WIRING — the new keys exist in values.yaml with the documented
   defaults; helm/local/values.g2.yaml keeps every authorizationPolicy
   flag COMMENTED OFF (guidance only — the coupled-flip flags move
   together and G2 is not flipped yet).
5. EDGE-ONLY IDENTITY SURFACES (audit #6) — the baseline rules (ii) and
   (iv) both exclude the identity/mint/admin paths (/v1/identity,
   /v1/keys/for-me, /v1/enroll, /admin): an in-cluster pod must never be
   able to deliver spoofed X-Auth-Request-* headers to a mint, while
   /metrics and the health/ready paths stay reachable. Each path in BOTH
   Istio spellings — exact + /*-subtree — because notPaths are exact
   matches otherwise and bare "/admin" left /admin/keys etc. admitted
   (x-val on audit #6).

Rendered via subprocess `helm` — the same pattern test_loadaware_config.py
uses (helm is a CI/dev prerequisite; tests SKIP with a clear message when
the binary is absent).
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, ClassVar

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
HELM_CHART = REPO_ROOT / "helm"
VALUES_YAML = HELM_CHART / "values.yaml"
G2_YAML = HELM_CHART / "local" / "values.g2.yaml"
HOSTED_YAML = HELM_CHART / "values-examples" / "values.hosted-trial.yaml"
G2_EXAMPLE_YAML = HELM_CHART / "values-examples" / "values.g2.yaml"

# AUDIT #6: the identity/mint/admin surfaces — reachable ONLY from the edge
# (rule i). BOTH baseline in-cluster rules ((ii) same-namespace and (iv)
# allowInClusterApi) must exclude the SAME set: rules OR together, so the
# rule that drops an exclusion re-admits the path in-cluster. Every path in
# BOTH Istio spellings: notPaths are EXACT matches unless suffixed /*, so
# the bare form covers the literal path and the /* form the subtree the
# admin API router (prefix=/admin) actually serves (x-val on audit #6).
IDENTITY_MINT_ADMIN_PATHS = [
    "/v1/identity",
    "/v1/identity/*",
    "/v1/keys/for-me",
    "/v1/keys/for-me/*",
    "/v1/enroll",
    "/v1/enroll/*",
    "/admin",
    "/admin/*",
]

helm_skip = pytest.mark.skipif(
    shutil.which("helm") is None,
    reason="helm binary not on PATH (chart-render tests need it)",
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _helm_argv(*extra_args: str) -> list[str]:
    """helm argv from extra args. A "--set key=value" given as ONE token is
    split into the two argv elements helm expects — subprocess runs no
    shell to word-split for it."""
    argv: list[str] = []
    for a in extra_args:
        if a.startswith("--set ") and " " not in a[6:]:
            argv.extend(("--set", a[6:]))
        else:
            argv.append(a)
    return argv


def _render(*extra_args: str, chart: Path = HELM_CHART) -> str:
    """`helm template` the chart with optional extra args; assert rc=0."""
    cmd = ["helm", "template", "pcai-llm-gateway", str(chart), *_helm_argv(*extra_args)]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, f"helm template failed: {r.stderr}"
    return r.stdout


def _render_notes(*extra_args: str, chart: Path = HELM_CHART) -> str:
    """The NOTES section of `helm install --dry-run=client` (fully
    client-side — no cluster needed). helm v4's `helm template` does not
    emit NOTES.txt, so NOTES-only output (the W6 F1 spoofing-risk warning)
    is pinned through this path."""
    cmd = ["helm", "install", "--dry-run=client", "pcai-llm-gateway", str(chart), *_helm_argv(*extra_args)]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, f"helm install --dry-run=client failed: {r.stderr}"
    m = re.search(r"^NOTES:\n(.*)$", r.stdout, re.MULTILINE | re.DOTALL)
    assert m, "NOTES section missing from the dry-run output"
    return m.group(1)


def _render_docs(*extra_args: str) -> list[dict[str, Any]]:
    """Render and return every YAML document as a parsed dict."""
    return [d for d in yaml.safe_load_all(_render(*extra_args)) if isinstance(d, dict)]


def _authorization_policy(docs: list[dict[str, Any]]) -> dict[str, Any]:
    """Pull the AuthorizationPolicy doc out of a render (exactly one when
    the policy is enabled — fail loud otherwise)."""
    aps = [d for d in docs if d.get("kind") == "AuthorizationPolicy"]
    assert len(aps) == 1, f"expected exactly 1 AuthorizationPolicy, got {len(aps)}"
    return aps[0]


def _metrics_rule_bodies() -> list[str]:
    """The lines of the (iii) metrics-rule conditional block in
    authorizationpolicy.yaml — the additive Z5 delta."""
    tpl = (HELM_CHART / "templates" / "authorizationpolicy.yaml").read_text()
    m = re.search(r"(\{\{- if \$ap\.allowMetricsScrape \}\}.*?\{\{- end \}\})", tpl, re.DOTALL)
    assert m, "allowMetricsScrape conditional block missing from the template"
    return m.group(1).splitlines()


@pytest.fixture(scope="module")
def baseline_renders() -> dict[str, str]:
    """Render the chart WITHOUT the Z5 additive block, from a throwaway
    chart copy with the new conditional block excised from the template —
    the byte-identity oracle for the flag-off invariant. (The excised
    substring is replaced verbatim — line-filtering would strip the
    identically-spelled `{{- end }}` of the sibling `rules` conditional
    and unbalance the template.)"""
    tpl_text = (HELM_CHART / "templates" / "authorizationpolicy.yaml").read_text()
    m = re.search(r"\{\{- if \$ap\.allowMetricsScrape \}\}.*?\{\{- end \}\}", tpl_text, re.DOTALL)
    assert m, "allowMetricsScrape conditional block missing from the template"
    with tempfile.TemporaryDirectory() as td:
        stripped_chart = Path(td) / "helm"
        shutil.copytree(HELM_CHART, stripped_chart)
        tpl_path = stripped_chart / "templates" / "authorizationpolicy.yaml"
        tpl_path.write_text(tpl_text.replace(m.group(0), ""))
        return {
            "default": _render(chart=stripped_chart),
            "hosted": _render("-f", str(HOSTED_YAML), chart=stripped_chart),
        }


@pytest.fixture(scope="module")
def metrics_on() -> dict[str, Any]:
    """Policy enabled + allowMetricsScrape=true (+ a valid VS endpoint,
    which the template fails loud without when ezua.enabled=true)."""
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        f.write(
            "ezua:\n"
            "  enabled: true\n"
            "  virtualService:\n"
            "    endpoint: pcai-llm-gateway.example.internal\n"
            "  authorizationPolicy:\n"
            "    enabled: true\n"
            "    allowMetricsScrape: true\n"
        )
        path = f.name
    try:
        return _authorization_policy(_render_docs("-f", path))
    finally:
        Path(path).unlink()


# ---------------------------------------------------------------------------
# 1. flag-off byte-identity (the invariant)
# ---------------------------------------------------------------------------


@helm_skip
class TestFlagOffByteIdentity:
    def test_default_render_byte_identical(self, baseline_renders: dict[str, str]) -> None:
        """Default render (all flags off) is byte-identical to the chart
        without the Z5 block — the additive rule renders NOTHING when off."""
        assert _render() == baseline_renders["default"]

    def test_hosted_trial_render_byte_identical(self, baseline_renders: dict[str, str]) -> None:
        """The hosted-trial example (policy ENABLED, metrics flag untouched)
        also renders byte-identical — the new rule is opt-in per key, not
        implied by enabling the policy."""
        assert _render("-f", str(HOSTED_YAML)) == baseline_renders["hosted"]

    def test_deployment_pod_spec_untouched_when_policy_off(self, baseline_renders: dict[str, str]) -> None:
        """With the policy off, no Deployment pod template carries the
        excludeInboundPorts annotation (the exclusion is also policy-gated
        in deployment.yaml — verified here on the parsed doc, not just
        read; the raw render necessarily mentions the name in comments)."""
        for label, rendered in (
            ("default", _render()),
            ("hosted", _render("-f", str(HOSTED_YAML))),
        ):
            assert rendered == baseline_renders[label]
            dep = next(d for d in yaml.safe_load_all(rendered) if d and d.get("kind") == "Deployment")
            ann = dep["spec"]["template"]["metadata"].get("annotations") or {}
            assert "traffic.sidecar.istio.io/excludeInboundPorts" not in ann


# ---------------------------------------------------------------------------
# 2. the metrics relief rule
# ---------------------------------------------------------------------------


@helm_skip
class TestMetricsReliefRule:
    def test_rule_appended(self, metrics_on: dict[str, Any]) -> None:
        """allowMetricsScrape=true appends ONE extra rule after the baseline
        (i)/(ii): an ANY-SOURCE rule scoped to the metrics path on the
        service port.

        The rule is deliberately NOT namespace-scoped (2026-10-07 G2
        rollout): kube-prometheus scrapes raw POD IPs, and a raw-IP outbound
        connection is envoy passthrough — PLAINTEXT even from a meshed
        scraper — so no peer identity exists for a namespaces/principals
        matcher to match. A namespace-scoped rule therefore never matched and
        metrics stayed down (verified live: prometheus-0 with a sidecar, up
        stayed 0). Hence a to:-only rule with no from: at all."""
        rules = metrics_on["spec"]["rules"]
        assert len(rules) == 3, f"expected 3 rules (i,ii,iii), got {len(rules)}"
        scraper = rules[2]
        # ANY source: the absence of "from" IS the contract.
        assert "from" not in scraper
        assert scraper["to"][0]["operation"]["ports"] == ["9200"]
        assert scraper["to"][0]["operation"]["paths"] == ["/metrics", "/metrics/"]

    def test_metrics_rule_stays_any_source(self) -> None:
        """metricsNamespace is retained for compatibility only and is a
        no-op: setting it must NOT reintroduce a namespace-scoped (and thus
        never-matching) metrics rule."""
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write(
                "ezua:\n"
                "  enabled: true\n"
                "  virtualService:\n"
                "    endpoint: pcai-llm-gateway.example.internal\n"
                "  authorizationPolicy:\n"
                "    enabled: true\n"
                "    allowMetricsScrape: true\n"
                "    metricsNamespace: monitoring\n"
            )
            path = f.name
        try:
            ap = _authorization_policy(_render_docs("-f", path))
        finally:
            Path(path).unlink()
        scraper = ap["spec"]["rules"][2]
        assert "from" not in scraper  # no namespace-scoped rule is emitted
        assert scraper["to"][0]["operation"]["paths"] == ["/metrics", "/metrics/"]

    def test_service_port_follows_values(self) -> None:
        """The allowed port is the SERVICE port (what the ServiceMonitor
        targets) — follows service.port, not a hardcoded 9200."""
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write(
                "ezua:\n"
                "  enabled: true\n"
                "  virtualService:\n"
                "    endpoint: pcai-llm-gateway.example.internal\n"
                "  authorizationPolicy:\n"
                "    enabled: true\n"
                "    allowMetricsScrape: true\n"
                "service:\n"
                "  port: 9300\n"
                "  targetPort: 9300\n"
            )
            path = f.name
        try:
            ap = _authorization_policy(_render_docs("-f", path))
        finally:
            Path(path).unlink()
        assert ap["spec"]["rules"][2]["to"][0]["operation"]["ports"] == ["9300"]

    def test_operator_rules_still_replace(self) -> None:
        """Operator-supplied `rules` REPLACE the baseline (existing
        semantic) — the metrics rule must not leak into that path even
        when the flag is on."""
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write(
                "ezua:\n"
                "  enabled: true\n"
                "  virtualService:\n"
                "    endpoint: pcai-llm-gateway.example.internal\n"
                "  authorizationPolicy:\n"
                "    enabled: true\n"
                "    allowMetricsScrape: true\n"
                "    rules:\n"
                "      - from:\n"
                "          - source:\n"
                "              principals:\n"
                '                - "cluster.local/ns/istio-system/sa/httpbin"\n'
            )
            path = f.name
        try:
            ap = _authorization_policy(_render_docs("-f", path))
        finally:
            Path(path).unlink()
        rules = ap["spec"]["rules"]
        assert len(rules) == 1
        assert "principals" in rules[0]["from"][0]["source"]

    def test_hosted_trial_plus_flag(self) -> None:
        """The DEPLOYMENT.md rollout path end-to-end: hosted-trial values +
        the relief flag = baseline rules + the scraper rule (nothing else
        moves)."""
        ap = _authorization_policy(
            _render_docs("-f", str(HOSTED_YAML), "--set", "ezua.authorizationPolicy.allowMetricsScrape=true")
        )
        rules = ap["spec"]["rules"]
        assert len(rules) == 3
        assert "from" not in rules[2]  # ANY-source metrics rule
        assert rules[2]["to"][0]["operation"]["ports"] == ["9200"]
        assert rules[2]["to"][0]["operation"]["paths"] == ["/metrics", "/metrics/"]


# ---------------------------------------------------------------------------
# 3. excludeInboundPorts end-to-end wiring (verification, not assumption)
# ---------------------------------------------------------------------------


@helm_skip
class TestExcludeInboundPortsWiring:
    def test_annotation_stamps_when_policy_on(self) -> None:
        """excludeInboundPorts (non-empty, policy on) stamps
        traffic.sidecar.istio.io/excludeInboundPorts on the DEPLOYMENT pod
        template — the wiring the template comments claim; verified here
        end-to-end instead of trusted."""
        rendered = _render(
            "-f",
            str(HOSTED_YAML),
            "--set",
            "ezua.authorizationPolicy.enabled=true",
            "--set",
            "ezua.authorizationPolicy.excludeInboundPorts={9200}",
        )
        dep = next(d for d in yaml.safe_load_all(rendered) if d and d.get("kind") == "Deployment")
        ann = dep["spec"]["template"]["metadata"]["annotations"]
        assert ann["traffic.sidecar.istio.io/excludeInboundPorts"] == "9200"

    def test_no_annotation_when_policy_off(self) -> None:
        """Policy off (even with a non-empty port list): no annotation —
        the exclusion is gated on the policy being rendered at all."""
        rendered = _render("--set", "ezua.authorizationPolicy.excludeInboundPorts={9200}")
        dep = next(d for d in yaml.safe_load_all(rendered) if d and d.get("kind") == "Deployment")
        ann = (dep["spec"]["template"]["metadata"] or {}).get("annotations") or {}
        assert "traffic.sidecar.istio.io/excludeInboundPorts" not in ann

    def test_no_annotation_when_list_empty(self) -> None:
        """Policy on but the list empty (the hosted-trial default): no
        annotation on the Deployment pod template, pod spec byte-unchanged.
        (The template's explanatory COMMENTS mention the annotation name —
        assert on the parsed Deployment doc, not the raw render.)"""
        rendered = _render("-f", str(HOSTED_YAML))
        dep = next(d for d in yaml.safe_load_all(rendered) if d and d.get("kind") == "Deployment")
        ann = dep["spec"]["template"]["metadata"].get("annotations") or {}
        assert "traffic.sidecar.istio.io/excludeInboundPorts" not in ann


# ---------------------------------------------------------------------------
# 4. values wiring (static — no helm needed, run even without the binary)
# ---------------------------------------------------------------------------


class TestValuesWiring:
    def test_values_yaml_defaults(self) -> None:
        """values.yaml carries the new keys with the documented defaults:
        allowMetricsScrape false (ADDITIVE, flag-off identity),
        metricsNamespace prometheus."""
        data: dict[str, Any] = {}

        for doc in yaml.safe_load_all(VALUES_YAML.read_text()):
            if isinstance(doc, dict):
                for k, v in doc.items():  # merge top-level maps across docs
                    data.setdefault(k, {}).update(v or {})
        ap = data["ezua"]["authorizationPolicy"]
        assert ap["allowMetricsScrape"] is False
        assert ap["metricsNamespace"] == "prometheus"
        assert ap["excludeInboundPorts"] == []

    def test_g2_flags_ship_enabled_with_relief(self) -> None:
        """helm/local/values.g2.yaml ships the SSO authorizationPolicy ON with
        the G2 relief knobs (2026-10-07 rollout).

        The previous contract — every toggle commented OFF as guidance-only —
        was superseded when the coupled chain (namespace injection + policy +
        edge) was stood up: the file now carries the LIVE posture, and the
        enable is atomic with the injection label. Pinned here so a future
        edit cannot silently disable the policy or drop the relief knobs
        (allowMetricsScrape / allowInClusterApi), which are what keep
        Prometheus scraping and unmeshed in-cluster consumers working."""
        text = G2_YAML.read_text()
        # Extract the authorizationPolicy block (to the next same-indent key).
        m = re.search(r"(^  authorizationPolicy:\n(?:^[ ]{4}.*\n)+)", text, re.MULTILINE)
        assert m, "authorizationPolicy block not found in values.g2.yaml"
        block = m.group(1)
        active = [
            re.sub(r"\s+#.*$", "", ln).rstrip()  # strip inline comments
            for ln in block.splitlines()
            if ln.strip() and not ln.lstrip().startswith("#")
        ]
        assert active == [
            "  authorizationPolicy:",
            "    enabled: true",
            "    ingressNamespace: istio-system",
            "    ingressGatewayPrincipals: []",
            "    excludeInboundPorts: []",
            "    allowMetricsScrape: true",
            "    metricsNamespace: prometheus",
            "    allowInClusterApi: true",
            "    rules: []",
        ], active
        # The load-bearing relief knobs must stay ON: without them Prometheus
        # cannot scrape (any-source /metrics rule) and unmeshed in-cluster
        # callers (DSH, OpenWebUI) are locked out.
        assert "allowMetricsScrape: true" in block
        assert "allowInClusterApi: true" in block

    def test_g2_yaml_still_renders(self) -> None:
        """The G2 values file stays a valid Helm values paste: it
        merge-renders clean AND the now-enabled policy renders with its
        relief knobs intact."""
        if not shutil.which("helm"):
            pytest.skip("helm binary not on PATH (chart-render tests need it)")
        rendered = _render("-f", str(G2_YAML))
        docs = [d for d in yaml.safe_load_all(rendered) if d]
        aps = [d for d in docs if d.get("kind") == "AuthorizationPolicy"]
        assert len(aps) == 1, "G2 values must render exactly one AuthorizationPolicy"
        rules = aps[0]["spec"]["rules"]
        # (i) ingress, (ii) same-namespace, (iii) ANY-source metrics scrape,
        # (iv) unmeshed in-cluster API callers (allowInClusterApi=true on G2,
        # minus the SAME edge-only paths rule (ii) excludes — audit #6).
        assert len(rules) == 4
        assert rules[2]["to"][0]["operation"]["paths"] == ["/metrics", "/metrics/"]
        assert "from" not in rules[2]  # ANY-source metrics rule
        assert rules[3]["to"][0]["operation"]["notPaths"] == IDENTITY_MINT_ADMIN_PATHS


# ---------------------------------------------------------------------------
# 5. edge-only identity/mint/admin surfaces (audit #6)
# ---------------------------------------------------------------------------


@helm_skip
class TestEdgeOnlyIdentitySurfaces:
    """AUDIT #6 pin: the same-namespace rule (ii) used to render as a bare
    from: — every meshed pod in the release namespace could reach
    /v1/identity, /v1/keys/for-me, /v1/enroll and /admin, exactly the
    surfaces the chart and DEPLOYMENT.md promise stay edge-only. Both
    baseline in-cluster rules now carry the same notPaths contract."""

    def _ap(self, **flags: bool) -> dict[str, Any]:
        """Policy-on render (default baseline rules) with extra flags."""
        lines = [
            "ezua:",
            "  enabled: true",
            "  virtualService:",
            "    endpoint: pcai-llm-gateway.example.internal",
            "  authorizationPolicy:",
            "    enabled: true",
            *[f"    {k}: {str(v).lower()}" for k, v in flags.items()],
        ]
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write("\n".join(lines) + "\n")
            path = f.name
        try:
            return _authorization_policy(_render_docs("-f", path))
        finally:
            Path(path).unlink()

    def test_same_namespace_rule_excludes_identity_mint_admin(self) -> None:
        """Rule (ii) is from+to in ONE rule (AND semantics — two separate
        list items would OR and re-admit the namespace wholesale): from
        the release namespace, on the service port, minus the edge-only
        paths in both spellings (exact + /* subtree)."""
        rules = self._ap()["spec"]["rules"]
        assert len(rules) == 2
        ii = rules[1]
        assert ii["from"][0]["source"]["namespaces"]  # same-ns source kept
        assert ii["to"][0]["operation"]["notPaths"] == IDENTITY_MINT_ADMIN_PATHS
        assert ii["to"][0]["operation"]["ports"] == ["9200"]

    def test_metrics_and_health_stay_reachable(self) -> None:
        """The exclusions name ONLY the identity/mint/admin paths —
        /metrics and the health/ready probes are not among them, so the
        namespace rule and the relief rules keep serving them."""
        for rule in self._ap()["spec"]["rules"]:
            for op in rule.get("to", []):
                excluded = op.get("operation", {}).get("notPaths") or []
                for keep in ("/metrics", "/metrics/", "/healthz", "/ready"):
                    assert keep not in excluded, f"{keep} must stay reachable, is excluded"

    def test_rule_two_and_rule_four_share_the_notpaths_contract(self) -> None:
        """allowInClusterApi=true (the live G2 posture): rule (iv) admits
        ANY source, so its notPaths set MUST equal rule (ii)'s — the rule
        that drops an exclusion re-admits the path in-cluster (rules OR)."""
        rules = self._ap(allowInClusterApi=True)["spec"]["rules"]
        assert len(rules) == 3
        ii, iv = rules[1], rules[2]
        assert ii["to"][0]["operation"]["notPaths"] == IDENTITY_MINT_ADMIN_PATHS
        assert iv["to"][0]["operation"]["notPaths"] == IDENTITY_MINT_ADMIN_PATHS
        # The bite-proof (x-val on audit #6): the SUBTREE form must be
        # present — a bare "/admin" excludes only the literal path and
        # left the whole /admin/* API router subtree admitted.
        assert all("/admin/*" in r["to"][0]["operation"]["notPaths"] for r in (ii, iv))


# ---------------------------------------------------------------------------
# 6. zero-value numeric knobs (W6 H1) + full probeCidrs list (W6 H3)
# ---------------------------------------------------------------------------


def _deployment_envs(docs: list[dict[str, Any]]) -> dict[str, str]:
    """Plain (non-secretKeyRef) env values from the single Deployment doc."""
    deps = [d for d in docs if d.get("kind") == "Deployment"]
    assert len(deps) == 1, f"expected exactly 1 Deployment, got {len(deps)}"
    env = deps[0]["spec"]["template"]["spec"]["containers"][0]["env"]
    return {e["name"]: e["value"] for e in env if "value" in e}


def _netpol_ingress_cidrs(doc: dict[str, Any]) -> list[str]:
    """Every ipBlock CIDR in a NetworkPolicy's INGRESS rules."""
    out: list[str] = []
    for rule in doc.get("spec", {}).get("ingress") or []:
        for frm in rule.get("from") or []:
            if "ipBlock" in frm:
                out.append(frm["ipBlock"]["cidr"])
    return out


@helm_skip
class TestZeroValueNumericKnobs:
    """W6 H1 (P2): five numeric knobs rendered through `| default`, which
    treats 0 as EMPTY — so the documented zero kill-switches (failoverTtftS
    "0 disables", stickyRoutingIdleResetS "0 = never reset",
    registryWatchS "0 disables the poll", registryAdminFreshS "0 = disabled")
    were silently rewritten to the non-zero defaults. The file already uses
    the hasKey pattern for exactly this hazard (the reasoningHistoryMaxChars
    lesson) — these pins keep the documented 0s reaching the pod as "0"."""

    ZERO_KNOBS: ClassVar[list[tuple[str, str]]] = [
        ("failoverTtftS", "PCAI_LLM_FAILOVER_TTFT_S"),
        ("stickyRoutingIdleResetS", "PCAI_LLM_STICKY_ROUTING_IDLE_RESET_S"),
        ("registryWatchS", "PCAI_LLM_REGISTRY_WATCH_S"),
        ("registryAdminFreshS", "PCAI_LLM_REGISTRY_ADMIN_FRESH_S"),
    ]

    @pytest.mark.parametrize("knob,env_name", ZERO_KNOBS)
    def test_zero_renders_as_zero(self, knob: str, env_name: str) -> None:
        envs = _deployment_envs(_render_docs(f"--set settings.{knob}=0"))
        assert envs.get(env_name) == "0", (
            f"settings.{knob}=0 must reach the pod as 0 (documented kill-switch); "
            f"got {envs.get(env_name)!r} — `| default` rewrites 0 to the non-zero default"
        )


@helm_skip
class TestProbeCidrsFullList:
    """W6 H3 (P3): probeCidrs is a list, but the templates rendered only
    `| first` — a multi-node site lost the kubelet-probe allowance for
    every node but the first (probe failure → liveness kill loop). BOTH
    NetworkPolicies (gateway + embedded PG) must render every entry."""

    CIDR_A = "172.28.1.161/32"
    CIDR_B = "172.28.1.162/32"
    EXTRA = (
        "--set networkPolicy.enabled=true",
        "--set networkPolicy.probeCidrs[0]=172.28.1.161/32",
        "--set networkPolicy.probeCidrs[1]=172.28.1.162/32",
        "--set networkPolicy.probeExceptCidrs[0]=10.224.0.0/16",
    )

    def test_gateway_netpol_renders_every_probe_cidr(self) -> None:
        docs = _render_docs(*self.EXTRA)
        nets = [d for d in docs if d.get("kind") == "NetworkPolicy"]
        assert len(nets) == 1, "embedded PG off → exactly one NetworkPolicy"
        cidrs = _netpol_ingress_cidrs(nets[0])
        assert self.CIDR_A in cidrs and self.CIDR_B in cidrs, f"both probe CIDRs must render, got {cidrs}"

    def test_pg_netpol_renders_every_probe_cidr(self) -> None:
        docs = _render_docs(*self.EXTRA, "--set database.embedded.enabled=true")
        pg = [
            d
            for d in docs
            if d.get("kind") == "NetworkPolicy" and d["metadata"]["name"] == "pcai-llm-gateway-pg-netpol"
        ]
        assert len(pg) == 1, "embedded PG on → the PG netpol must render"
        cidrs = _netpol_ingress_cidrs(pg[0])
        assert self.CIDR_A in cidrs and self.CIDR_B in cidrs, (
            f"both probe CIDRs must render in the PG policy, got {cidrs}"
        )


# ---------------------------------------------------------------------------
# 7. trust-principal NOTES warning (W6 F1) + g2-example PDB (W6 F2)
# ---------------------------------------------------------------------------


@helm_skip
class TestTrustPrincipalWarning:
    """W6 F1: trustProxyIdentity ON + empty ingressGatewayPrincipals is a
    spoofable-header posture (the AP edge rule admits the whole
    ingressNamespace to every path) — NOTES.txt must warn LOUDLY without
    failing the render (a fail would break the live G2 deploy)."""

    WARN = "IDENTITY SPOOFING RISK"

    def test_warning_emitted_when_trust_on_and_principals_empty(self) -> None:
        notes = _render_notes("--set settings.trustProxyIdentity=true")
        assert self.WARN in notes, (
            "trustProxyIdentity=true with empty ingressGatewayPrincipals must emit the NOTES.txt spoofing-risk warning"
        )

    def test_warning_suppressed_when_principals_set(self) -> None:
        notes = _render_notes(
            "--set settings.trustProxyIdentity=true",
            "--set ezua.authorizationPolicy.ingressGatewayPrincipals[0]=cluster.local/ns/istio-system/sa/ezaf-gateway",
        )
        assert self.WARN not in notes, "warning must be gone once principals are set"

    def test_warning_absent_from_default_render(self) -> None:
        assert self.WARN not in _render_notes(), "default render (trust off) stays warning-free"


@helm_skip
class TestG2ExamplePdb:
    """W6 F2: the g2 example runs HPA min 2 / max 4 — the PDB must render
    (minAvailable 1: blocks draining the LAST replica while still allowing
    a rolling update at the 2-replica floor; minAvailable 2 would deadlock
    a 2-replica set that can never replace one of two)."""

    def test_g2_example_renders_pdb(self) -> None:
        docs = _render_docs("-f", str(G2_EXAMPLE_YAML))
        pdbs = [d for d in docs if d.get("kind") == "PodDisruptionBudget"]
        assert len(pdbs) == 1, f"exactly one PDB expected from the g2 example, got {len(pdbs)}"
        assert pdbs[0]["spec"].get("minAvailable") == 1, "minAvailable 1 (see class docstring)"


@helm_skip
class TestTraceRetentionG2:
    """W6 F4: agent_traces grows unbounded at the 0 default and no site
    overlay set it — the CHART layer opts in explicitly (a chart value is an
    operator decision at install time, not a silent prune on upgrade). The
    g2 example ships 30; the base chart keeps the code default 0, which the
    template renders as NO env at all (0 → omitted)."""

    def test_g2_example_renders_trace_retention_30(self) -> None:
        envs = _deployment_envs(_render_docs("-f", str(G2_EXAMPLE_YAML)))
        assert envs.get("PCAI_LLM_RETENTION_TRACE_DAYS") == "30", (
            f"g2 example must opt in explicitly; got {envs.get('PCAI_LLM_RETENTION_TRACE_DAYS')!r}"
        )

    def test_base_chart_stays_at_code_default(self) -> None:
        envs = _deployment_envs(_render_docs())
        assert "PCAI_LLM_RETENTION_TRACE_DAYS" not in envs, (
            "base chart must keep the code default (0 → env omitted) — no silent prune for everyone"
        )


@helm_skip
class TestHpaPoolOversubscription:
    """W6 F5: each replica holds its own pool of dbPoolMax connections, so the
    HPA's worst-case aggregate demand is maxReplicas × dbPoolMax — exceeding
    the DB's connection budget makes pods fail to ACQUIRE connections at the
    very spike that triggered the scale-up. NOTES.txt must make the arithmetic
    LOUD (warn, never fail — existing installs must still upgrade):
    demand > budget (embedded or explicit) → ⚠️ naming both numbers;
    external DB with no known budget → the arithmetic as a one-line NOTE;
    HPA off or within budget → nothing at all."""

    def test_oversubscribed_explicit_budget_warns(self) -> None:
        notes = _render_notes(
            "--set autoscaling.enabled=true",
            "--set database.maxConnectionsBudget=100",
        )
        assert "POSTGRES CONNECTION OVERSUBSCRIPTION" in notes
        assert "150" in notes, "the computed product (6 × 25) must be shown"
        assert "100" in notes, "the budget must be shown"
        assert "maxConnectionsBudget" in notes, "the budget knob must be named"

    def test_oversubscribed_embedded_budget_warns(self) -> None:
        notes = _render_notes(
            "--set autoscaling.enabled=true",
            "--set database.embedded.enabled=true",
            "--set database.embedded.maxConnections=120",
        )
        assert "POSTGRES CONNECTION OVERSUBSCRIPTION" in notes
        assert "150" in notes and "120" in notes
        assert "database.embedded.maxConnections" in notes, "the embedded knob must be named"

    def test_g2_example_does_not_warn(self) -> None:
        notes = _render_notes("-f", str(G2_EXAMPLE_YAML))
        assert "POSTGRES CONNECTION OVERSUBSCRIPTION" not in notes, (
            "g2: 4 × 25 = 100 <= 200 — self-checking, must not warn"
        )

    def test_external_db_without_budget_gets_arithmetic_note(self) -> None:
        notes = _render_notes("--set autoscaling.enabled=true")
        assert "POSTGRES CONNECTIONS" in notes, "the informational note must render"
        assert "150" in notes, "the computed product (6 × 25) must be shown"
        assert "POSTGRES CONNECTION OVERSUBSCRIPTION" not in notes, "no budget known → arithmetic note, not a warning"

    def test_hpa_off_renders_nothing(self) -> None:
        notes = _render_notes()
        assert "POSTGRES CONNECTION" not in notes, "HPA off → no block at all"
