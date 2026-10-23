"""Load-aware cascade head — CONFIG SURFACE tests (my slice).

The cascade implementation itself (cascade.py/stickiness.py) is covered by
the cascade agent's suite; these tests pin the pieces THIS slice owns:

1. CHART WIRING — `helm template` renders PCAI_LLM_LOAD_AWARE_CASCADE
   from settings.loadAwareCascade.enabled: "false" by default, "true"
   when set, and "false" (never an empty value) when the key is absent
   from site values (chained `| default false` for backward-compatible
   pastes). Rendered via subprocess helm — the same subprocess pattern
   test_audit_fixes.py uses for its chart checks (helm is a CI/dev
   prerequisite; the render gates the env name the app reads).
2. VALUES PARITY — the fleet default (values.yaml) is OFF; the G2
   example is ON (Andrew's G2 wants the toggle on) — the exact
   divergence the design brief mandates (rule 5).
3. METRIC — the counter series exists with labels {picked, reason},
   the cascade_head_pick helper increments it, unknown reasons are
   recorded (never raise — metrics must not fail an admission), and the
   no-op shim path stays silent when prometheus_client is absent.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
HELM_CHART = REPO_ROOT / "helm"
VALUES_YAML = HELM_CHART / "values.yaml"
G2_YAML = HELM_CHART / "values-examples" / "values.g2.yaml"
ENV_NAME = "PCAI_LLM_LOAD_AWARE_CASCADE"

helm_skip = pytest.mark.skipif(
    shutil.which("helm") is None,
    reason="helm binary not on PATH (chart-render tests need it)",
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _render(env_value: str | None = None) -> str:
    """`helm template` the chart; with env_value, force the knob via --set
    (exactly how a site would flip it); with None, render the file defaults."""
    cmd = ["helm", "template", "pcai-llm-gateway", str(HELM_CHART)]
    if env_value is not None:
        cmd += ["--set", f"settings.loadAwareCascade.enabled={env_value}"]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, f"helm template failed: {r.stderr}"
    return r.stdout


def _env_value(rendered: str, name: str = ENV_NAME) -> str:
    """Extract one env var's quoted value from the rendered deployment
    (the value rides the line AFTER the `- name:` entry)."""
    lines = rendered.splitlines()
    for i, line in enumerate(lines):
        if line.strip() == f"- name: {name}":
            for follow in lines[i + 1 : i + 3]:
                stripped = follow.strip()
                if stripped.startswith("value:"):
                    return stripped.partition("value: ")[2].strip('"')
            pytest.fail(f"{name} has no value in the rendered chart")
    pytest.fail(f"{name} not found in rendered chart")


def _load_yaml(path: Path) -> dict[str, Any]:
    import yaml

    docs = list(yaml.safe_load_all(path.read_text()))
    data: dict[str, Any] = {}
    for doc in docs:
        if isinstance(doc, dict):
            data.update(doc)
    return data


# ---------------------------------------------------------------------------
# 1. chart wiring
# ---------------------------------------------------------------------------


@helm_skip
class TestChartWiring:
    def test_default_render_is_false(self) -> None:
        """values.yaml default (enabled: false) renders value "false" —
        chart-off = app-off, never an empty/unset env."""
        assert _env_value(_render()) == "false"

    def test_set_true_renders_true(self) -> None:
        """--set loadAwareCascade.enabled=true — the operator's flip —
        renders value "true"."""
        assert _env_value(_render("true")) == "true"

    def test_set_false_renders_false(self) -> None:
        assert _env_value(_render("false")) == "false"

    def test_absent_key_defaults_false(self) -> None:
        """Older site values pastes without the knob still render "false"
        (the template's chained `| default false`), so the app default and
        the chart default can never disagree. Renders with a values file
        that GENUINELY OMITS the key (a temp override file carrying a
        different settings key), not just the chart default — review nit:
        the default render exercises the default VALUE, not the absent
        KEY path."""
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write("settings:\n  upstreamTimeoutS: 600\n")
            override = f.name
        try:
            cmd = ["helm", "template", "pcai-llm-gateway", str(HELM_CHART), "-f", override]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            assert r.returncode == 0, f"helm template failed: {r.stderr}"
            assert _env_value(r.stdout) == "false"
        finally:
            os.unlink(override)

    def test_env_sits_in_deployment_container(self) -> None:
        """The env belongs to the gateway container spec (not some other
        object) — parse the rendered Deployment, not just grep the doc."""
        import yaml

        rendered = _render("true")
        dep = next(d for d in yaml.safe_load_all(rendered) if isinstance(d, dict) and d.get("kind") == "Deployment")
        env = dep["spec"]["template"]["spec"]["containers"][0]["env"]
        pairs = {e["name"]: e.get("value") for e in env}
        assert pairs[ENV_NAME] == "true"


# ---------------------------------------------------------------------------
# 2. values parity (brief rule 5: OFF by fleet default, ON in G2 only)
# ---------------------------------------------------------------------------


class TestValuesParity:
    def test_fleet_default_is_off(self) -> None:
        block = _load_yaml(VALUES_YAML)["settings"]["loadAwareCascade"]
        assert block["enabled"] is False

    def test_g2_example_is_on(self) -> None:
        block = _load_yaml(G2_YAML)["settings"]["loadAwareCascade"]
        assert block["enabled"] is True

    def test_g2_example_documents_the_site_divergence(self) -> None:
        text = G2_YAML.read_text()
        # The divergence must be documented IN WORDS (values-blueprint 2026-10
        # bans "# SITE:" markers - state the fact as a plain one-liner).
        assert "OFF by fleet default" in text, "G2 must document the fleet-default divergence"
        # The comment names what it toggles (the brief's operator framing).
        assert "load-aware cascade" in text

    def test_values_yaml_documents_off_by_default(self) -> None:
        text = VALUES_YAML.read_text()
        assert "loadAwareCascade:" in text
        assert "OFF" in text  # the brief's backwards-compat gate is stated


# ---------------------------------------------------------------------------
# 3. metric surface
# ---------------------------------------------------------------------------


class TestCascadeHeadPicksMetric:
    def test_series_registered_with_labels(self) -> None:
        from pcai_llm.core import metrics

        # A labeled counter exposes samples only after a child is created —
        # seed one, then assert the exported series/labels.
        metrics.cascade_head_pick("seed-model", "default")
        samples = [s for fam in metrics.CASCADE_HEAD_PICKS.collect() for s in fam.samples]
        assert any(s.name == "gateway_cascade_head_picks_total" for s in samples), (
            "the gateway_cascade_head_picks_total series must be exported"
        )
        assert all({"picked", "reason"} <= set(s.labels) for s in samples)

    @pytest.mark.parametrize("reason", ("sticky", "load", "default"))
    def test_helper_increments_each_reason(self, reason: str) -> None:
        from pcai_llm.core import metrics

        def sample() -> float:
            for fam in metrics.CASCADE_HEAD_PICKS.collect():
                for s in fam.samples:
                    if s.labels == {"picked": "GLM-5.3-Flash", "reason": reason}:
                        return s.value
            return 0.0

        before = sample()
        metrics.cascade_head_pick("GLM-5.3-Flash", reason)
        assert sample() == before + 1

    def test_unknown_reason_recorded_not_raised(self) -> None:
        """A metrics call must never fail an admission (metrics.py design
        rule 2 posture): an unexpected reason string is counted verbatim
        (the cascade agent derives reasons from CASCADE_PICK_REASONS)."""
        from pcai_llm.core import metrics

        metrics.cascade_head_pick("m-x", "unexpected")
        found = [
            s.value
            for fam in metrics.CASCADE_HEAD_PICKS.collect()
            for s in fam.samples
            if s.labels == {"picked": "m-x", "reason": "unexpected"}
        ]
        assert found and found[-1] >= 1

    def test_reason_vocabulary_constant(self) -> None:
        from pcai_llm.core import metrics

        assert metrics.CASCADE_PICK_REASONS == ("sticky", "load", "default", "unhealthy")

    def test_noop_shim_silently_accepts_pick(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """With prometheus_client absent, the fresh-module shim absorbs the
        call without raising (same importlib pattern as test_metrics.py)."""
        import importlib.util
        import sys

        from pcai_llm.core import metrics as metrics_mod

        saved = {k: v for k, v in sys.modules.items() if "prometheus" in k}
        for k in list(saved):
            monkeypatch.delitem(sys.modules, k, raising=False)
        monkeypatch.setitem(sys.modules, "prometheus_client", None)

        spec = importlib.util.spec_from_file_location("pcai_llm.core.metrics_noop_loadaware", metrics_mod.__file__)
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        monkeypatch.undo()
        for k, v in saved.items():
            sys.modules.setdefault(k, v)

        assert mod._REAL is False
        mod.cascade_head_pick("GLM-5.3-Flash", "load")  # must not raise
        mod.CASCADE_HEAD_PICKS.labels(picked="x", reason="load").inc()
