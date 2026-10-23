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
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
HELM_CHART = REPO_ROOT / "helm"
VALUES_YAML = HELM_CHART / "values.yaml"
G2_YAML = HELM_CHART / "local" / "values.g2.yaml"
HOSTED_YAML = HELM_CHART / "values-examples" / "values.hosted-trial.yaml"

helm_skip = pytest.mark.skipif(
    shutil.which("helm") is None,
    reason="helm binary not on PATH (chart-render tests need it)",
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _render(*extra_args: str, chart: Path = HELM_CHART) -> str:
    """`helm template` the chart with optional extra args; assert rc=0."""
    cmd = ["helm", "template", "pcai-llm-gateway", str(chart), *extra_args]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, f"helm template failed: {r.stderr}"
    return r.stdout


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
        # minus the identity-bearing paths).
        assert len(rules) == 4
        assert rules[2]["to"][0]["operation"]["paths"] == ["/metrics", "/metrics/"]
        assert "from" not in rules[2]  # ANY-source metrics rule
        assert rules[3]["to"][0]["operation"]["notPaths"] == ["/v1/identity", "/v1/keys/for-me"]
