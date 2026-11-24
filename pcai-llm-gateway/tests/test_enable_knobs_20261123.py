"""Enablement wave (2026-11-23) — CONFIG SURFACE tests (my slice).

The features themselves live in W1/W2 suites (playbook recall, payload store,
deterministic scorer). This file pins the slice the chart/docs zone owns: the
knobs must be REACHABLE, and their two postures must be what the brief says.

1. CHART WIRING — `helm template` renders every PCAI_LLM_* env name with the
   documented chart default, each one MOVES when its values path is set, and
   each sits in the gateway container's env list. The "moves" half matters:
   Helm renders an EMPTY value for a missing `.Values` path with no error, so
   a typo'd path is a phantom knob — the pod takes the code default while the
   operator believes the site is configured (test_values_layout.py catches the
   other direction: a template path with no values.yaml key).
2. ENV -> SETTINGS — the exact strings helm emits parse into the pydantic
   Settings fields the app reads. A chart that renders PCAI_LLM_FOO while
   config.py declares `bar` is as inert as a missing path.
3. VALUES PARITY — the TRACKED chart is conservative (no behaviour surprise:
   behaviour knobs off, defence knobs keep every result/tool); the LOCAL G2
   site is aggressive, and every aggressive flip documents its rollback value
   and the failure it can cause.
4. DOCS — FEATURES.md carries ONE section naming every knob, and it states the
   two limits rather than overclaiming: pins are in-memory (a restart
   re-approves) and injection screening does not cover the /mcp surface yet.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
HELM_CHART = REPO_ROOT / "helm"
VALUES_YAML = HELM_CHART / "values.yaml"
G2_YAML = HELM_CHART / "local" / "values.g2.yaml"
FEATURES_MD = REPO_ROOT / "documentation" / "FEATURES.md"
DOC_SECTION = "## Scorecard enablement (2026-11-23)"

helm_skip = pytest.mark.skipif(
    shutil.which("helm") is None,
    reason="helm binary not on PATH (chart-render tests need it)",
)
# helm/local/ is GITIGNORED (a local site document, not part of the chart), so
# the G2 assertions skip — rather than fail — where that file is absent.
g2_skip = pytest.mark.skipif(
    not G2_YAML.is_file(),
    reason="helm/local/values.g2.yaml is a gitignored local site file",
)

# (env name, values path, chart default, G2 value, probe) — the probe is set
# through the SAME values path and must come back out of the rendered env,
# which is what proves the path is real rather than silently defaulted.
KNOBS: tuple[tuple[str, str, str, str, str], ...] = (
    ("PCAI_LLM_TOOL_PIN_MODE", "settings.relay.toolPinMode", "warn", "block", "off"),
    ("PCAI_LLM_TOOL_INJECTION_SCREEN", "settings.relay.toolInjectionScreen", "annotate", "annotate", "block"),
    ("PCAI_LLM_PLAYBOOK_INJECT_ENABLED", "settings.memory.playbookInject.enabled", "false", "true", "true"),
    ("PCAI_LLM_PLAYBOOK_INJECT_K", "settings.memory.playbookInject.k", "3", "3", "5"),
    ("PCAI_LLM_PLAYBOOK_INJECT_MAX_CHARS", "settings.memory.playbookInject.maxChars", "2000", "2000", "1500"),
    # chart default stays 800 (conservative); G2 raised to 3000 on 2026-11-23 after 800 timed out live.
    ("PCAI_LLM_PLAYBOOK_INJECT_TIMEOUT_MS", "settings.memory.playbookInject.timeoutMs", "800", "3000", "250"),
    (
        "PCAI_LLM_PLAYBOOK_INJECT_MIN_SUCCESS_RATE",
        "settings.memory.playbookInject.minSuccessRate",
        "0",
        "0",
        "0.5",
    ),
    ("PCAI_LLM_TRACE_PAYLOAD_SAMPLING", "settings.tracing.tracePayload.sampling", "0", "0.1", "0.5"),
    ("PCAI_LLM_TRACE_PAYLOAD_RETENTION_DAYS", "settings.tracing.tracePayload.retentionDays", "7", "7", "3"),
    ("PCAI_LLM_TRACE_PAYLOAD_MAX_CHARS", "settings.tracing.tracePayload.maxChars", "2000", "2000", "512"),
    ("PCAI_LLM_TRACE_PAYLOAD_REDACT", "settings.tracing.tracePayload.redact", "true", "true", "false"),
    ("PCAI_LLM_AGENT_EVAL_ENABLED", "settings.tracing.agentEval.enabled", "false", "true", "true"),
    ("PCAI_LLM_AGENT_EVAL_MAX_STEPS", "settings.tracing.agentEval.maxSteps", "40", "40", "12"),
    ("PCAI_LLM_AGENT_EVAL_LOOP_THRESHOLD", "settings.tracing.agentEval.loopThreshold", "3", "3", "5"),
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _render(*extra: str) -> str:
    """`helm template` the chart; `extra` are -f/--set args (site values, a probe)."""
    cmd = ["helm", "template", "pcai-llm-gateway", str(HELM_CHART), *extra]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, f"helm template failed: {r.stderr}"
    return r.stdout


def _deployment(rendered: str) -> dict[str, Any]:
    """The rendered Deployment object (there is exactly one)."""
    for doc in yaml.safe_load_all(rendered):
        if isinstance(doc, dict) and doc.get("kind") == "Deployment":
            return doc
    pytest.fail("the render carries no Deployment")


def _env(rendered: str) -> dict[str, str]:
    """Container env as {name: value}; valueFrom entries are skipped."""
    env = _deployment(rendered)["spec"]["template"]["spec"]["containers"][0]["env"]
    return {e["name"]: str(e["value"]) for e in env if "value" in e}


def _load(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load(path.read_text())
    return data


# ---------------------------------------------------------------------------
# 1. chart wiring
# ---------------------------------------------------------------------------


@helm_skip
class TestChartWiring:
    def test_default_render_carries_every_knob(self) -> None:
        """Chart defaults: all fourteen envs exist with the conservative value —
        never empty, which is what a phantom path renders."""
        env = _env(_render())
        for name, path, default, _g2, _probe in KNOBS:
            assert name in env, f"{name} is not rendered from {path}"
            assert env[name] == default, f"{name} rendered {env[name]!r}, expected {default!r}"

    @g2_skip
    def test_site_values_render_the_aggressive_posture(self) -> None:
        """The G2 file is the site the wave pushes: its flips must reach the pod."""
        env = _env(_render("-f", str(G2_YAML)))
        for name, path, _default, g2, _probe in KNOBS:
            assert env[name] == g2, f"{name} ({path}) rendered {env[name]!r}, expected {g2!r}"  # g2 posture

    @pytest.mark.parametrize(("name", "path", "_default", "_g2", "probe"), KNOBS)
    def test_every_values_path_is_wired(self, name: str, path: str, _default: str, _g2: str, probe: str) -> None:
        """--set the documented values path and the env must follow. A typo'd
        path would silently keep the default here and on a real deployment."""
        env = _env(_render("--set", f"{path}={probe}"))
        assert env[name] == probe, f"{name} did not follow --set {path}={probe} (got {env[name]!r})"

    def test_envs_sit_in_the_gateway_container(self) -> None:
        """Parsed from the container spec, not grepped: the knobs must be real
        env entries, not comments or another object's fields."""
        env = _env(_render())
        missing = [name for name, *_rest in KNOBS if name not in env]
        assert missing == [], f"envs missing from the Deployment container: {missing}"


# ---------------------------------------------------------------------------
# 2. rendered env -> Settings (the app reads these exact strings)
# ---------------------------------------------------------------------------


def _settings_from_env(env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> Any:
    """Settings built from the rendered env strings, exactly as the pod would."""
    from pcai_llm.config import Settings

    for name, value in env.items():
        monkeypatch.setenv(name, value)
    return Settings(database_url="postgresql://t:t@localhost/t")


@helm_skip
class TestRenderedEnvParsesIntoSettings:
    def test_chart_defaults_parse(self, monkeypatch: pytest.MonkeyPatch) -> None:
        s = _settings_from_env(_env(_render()), monkeypatch)
        assert s.tool_pin_mode == "warn"
        assert s.tool_injection_screen == "annotate"
        assert s.playbook_inject_enabled is False
        assert (s.playbook_inject_k, s.playbook_inject_max_chars) == (3, 2000)
        assert (s.playbook_inject_timeout_ms, s.playbook_inject_min_success_rate) == (800, 0.0)
        assert s.trace_payload_sampling == 0.0
        assert (s.trace_payload_retention_days, s.trace_payload_max_chars) == (7, 2000)
        assert s.trace_payload_redact is True
        assert s.agent_eval_enabled is False
        assert (s.agent_eval_max_steps, s.agent_eval_loop_threshold) == (40, 3)

    @g2_skip
    def test_g2_posture_parses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Floats are the sharp edge: helm drops the .0 ("0.1" stays, 0.0
        renders "0") and the app must still parse the emitted strings."""
        s = _settings_from_env(_env(_render("-f", str(G2_YAML))), monkeypatch)
        assert s.tool_pin_mode == "block"
        assert s.tool_injection_screen == "annotate"
        assert s.playbook_inject_enabled is True
        assert s.trace_payload_sampling == pytest.approx(0.1)
        assert s.agent_eval_enabled is True


# ---------------------------------------------------------------------------
# 3. values parity: tracked = conservative, G2 = aggressive
# ---------------------------------------------------------------------------


class TestTrackedValuesAreConservative:
    """A tracked default must not change what a deployment DOES."""

    def test_defence_knobs_keep_every_result_and_tool(self) -> None:
        settings = _load(VALUES_YAML)["settings"]
        assert settings["relay"]["toolPinMode"] == "warn", "warn keeps offering the tool (block would drop it)"
        assert settings["relay"]["toolInjectionScreen"] == "annotate", "annotate keeps the body"

    def test_behaviour_knobs_are_off(self) -> None:
        settings = _load(VALUES_YAML)["settings"]
        assert settings["memory"]["playbookInject"]["enabled"] is False
        assert settings["tracing"]["tracePayload"]["sampling"] == 0.0
        assert settings["tracing"]["agentEval"]["enabled"] is False


@g2_skip
class TestG2ValuesAreAggressive:
    def test_site_flips_are_on(self) -> None:
        settings = _load(G2_YAML)["settings"]
        assert settings["relay"]["toolPinMode"] == "block"
        assert settings["memory"]["playbookInject"] == {
            "enabled": True,
            "k": 3,
            "maxChars": 2000,
            "timeoutMs": 3000,  # raised 800 -> 3000 on 2026-11-23: 800 timed out in production
            "minSuccessRate": 0.0,
        }
        assert settings["tracing"]["tracePayload"]["sampling"] == pytest.approx(0.1)
        assert settings["tracing"]["tracePayload"]["retentionDays"] == 7
        assert settings["tracing"]["agentEval"]["enabled"] is True

    @pytest.mark.parametrize(
        "key", ("toolPinMode", "toolInjectionScreen", "playbookInject", "tracePayload", "agentEval")
    )
    def test_each_site_knob_documents_rollback_and_failure(self, key: str) -> None:
        """The brief's rule for a local site: every flip names its rollback
        value and the failure it could cause, next to the flip."""
        text = G2_YAML.read_text()
        at = text.index(f"{key}:")
        near = text[max(0, at - 800) : at + 500]
        assert "ROLLBACK:" in near, f"{key} must document its rollback value beside the flip"
        assert "FAILURE:" in near, f"{key} must document the failure it could cause"


# ---------------------------------------------------------------------------
# 4. documentation (one section, no overclaiming)
# ---------------------------------------------------------------------------


class TestDocs:
    def _section(self) -> str:
        text = FEATURES_MD.read_text()
        assert DOC_SECTION in text, f"FEATURES.md is missing the {DOC_SECTION!r} section"
        return text.split(DOC_SECTION, 1)[1].split("\n## ", 1)[0]

    def test_documents_every_knob_family(self) -> None:
        section = self._section()
        for path in (
            "settings.relay.toolPinMode",
            "settings.relay.toolInjectionScreen",
            "settings.memory.playbookInject.*",
            "settings.tracing.tracePayload.*",
            "settings.tracing.agentEval.*",
        ):
            assert path in section, f"the docs must name {path}"

    def test_states_the_two_limits_without_overclaiming(self) -> None:
        section = self._section()
        # 2026-11-23 wave 2: pins are PERSISTENT and /mcp IS screened. This assertion used to
        # pin the OPPOSITE (in-memory, relay-only) — it locked in a claim the same release made
        # false. Keep it pointed at the current truth so the next change must update it.
        assert "PERSISTENT" in section, "the docs must say pins survive a restart (B2)"
        assert "not screened yet" not in section, "the docs must not claim /mcp is unscreened (B3 screened it)"
        assert "in-memory" in section, "the docs must still describe the degraded-mode fallback"
        assert "best-effort" in section, "redaction must be described as best-effort"
        assert "not a guarantee" in section.lower(), "redaction must not be claimed as a guarantee"

    def test_names_the_env_prefix_per_family(self) -> None:
        section = self._section()
        for token in (
            "PCAI_LLM_TOOL_PIN_MODE",
            "PCAI_LLM_TOOL_INJECTION_SCREEN",
            "PCAI_LLM_PLAYBOOK_INJECT_*",
            "PCAI_LLM_TRACE_PAYLOAD_*",
            "PCAI_LLM_AGENT_EVAL_*",
        ):
            assert token in section, f"the docs must name {token}"
