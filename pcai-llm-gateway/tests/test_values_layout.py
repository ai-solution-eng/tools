"""Values-layout drift guards for the GROUPED settings block.

The `settings:` map is grouped by subsystem (server, upstream, dbPool, retention,
cache, relay, mcp, routing, models, healthProbe, auth, memory, offpeak, audio,
tracing). Regrouping is a values-UI refactor only: every key must still render
the SAME `PCAI_LLM_*` env var, and the app is untouched because it reads env
vars, not values paths.

Two SILENT failure modes are what these tests exist for:

1. A template referencing a settings path that no longer exists. Helm renders an
   EMPTY string for a missing `.Values` lookup — no error, no warning — so the
   pod takes the app's code default while the operator believes values.yaml
   configured it. That is how a renamed key becomes a phantom knob.

2. A site values file written against the FLAT layout. Helm IGNORES unknown keys,
   so every moved key would configure NOTHING (the ssoGate dead-config class:
   a values key that is indented/wired wrong and silently does nothing).
   `helm/templates/values-guard.yaml` must fail the render instead, naming the
   offending keys.

The migration table lives in DEPLOYMENT.md § Values migration.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
HELM_CHART = REPO_ROOT / "helm"
TEMPLATES = HELM_CHART / "templates"
VALUES_YAML = HELM_CHART / "values.yaml"

# The subsystem sections the flat map was regrouped into, in file order.
SUBSYSTEMS = [
    "server",
    "upstream",
    "dbPool",
    "retention",
    "cache",
    "relay",
    "mcp",
    "routing",
    "models",
    "healthProbe",
    "auth",
    "memory",
    "offpeak",
    "audio",
    "tracing",
]

# First path segments of the OLD flat layout that no longer exist. Any of these
# under `settings:` means a site file is stale and must be migrated.
LEGACY_FLAT_KEYS = [
    "host",
    "port",
    "logLevel",
    "keepAliveTimeoutS",
    "limitConcurrency",
    "maxBodyBytes",
    "upstreamTimeoutS",
    "upstreamCaBundle",
    "endpointFailureThreshold",
    "endpointCooldownS",
    "failoverTtftS",
    "zeroCompletionRetry",
    "dbPoolMin",
    "dbPoolMax",
    "dbAcquireTimeoutS",
    "effectivePromptCapture",
    "effectivePromptRetainDays",
    "retentionTraceDays",
    "retentionConversationDays",
    "retentionCanonicalDays",
    "embeddingCacheEnabled",
    "embeddingCacheMaxInputBytes",
    "responseCacheMode",
    "responseCacheTtlS",
    "rateLimitExcludeCachedTokens",
    "budgetCachedExemptFraction",
    "rateLimitPredictCached",
    "rateLimitPredictReconcile",
    "toolRelayEnabled",
    "toolRelaySurface",
    "toolRelayMaxIterations",
    "toolRelayWallClockS",
    "toolRelayCallRetries",
    "toolRelayGrantMode",
    "toolRelayStreamWireTools",
    "toolRelayRenderDeltas",
    "toolRelayRenderIndexBase",
    "reasoningHistoryEnabled",
    "reasoningHistoryMaxChars",
    "toolCompressDefault",
    "toolCompressMinChars",
    "toolCompressRatioFloor",
    "toolCompressStoreTtlS",
    "toolCompressMaxStoreChars",
    "clientToolDispatch",
    "clientToolDispatchTimeoutS",
    "mcpFlatTools",
    "mcpCatalogCacheS",
    "mcpFailoverAttempts",
    "mcpSelfService",
    "mcpCallerAttribution",
    "registryWatchS",
    "registryAdminFreshS",
    "mcpArgPolicy",
    "mcpArgPolicyExempt",
    "mcpArgEgressServers",
    "mcpArgDenyClusterHosts",
    "mcpArgPolicyResolve",
    "mcpArgTelemetry",
    "mcpSeparateReadEgress",
    "routingEnabled",
    "routingTier1Tokens",
    "routingTier2Tokens",
    "routingTurnAdderTokens",
    "routingAgentsEnabled",
    "routeDefaultModel",
    "modelFallbackMax",
    "stickyRouting",
    "stickyRoutingIdleResetS",
    "loadAwareCascade",
    "demandAwareExcludedModels",
    "sessionAffinityEnabled",
    "sessionAffinityTtlS",
    "modelGroupsEnabled",
    "modelGroupLoadRefreshS",
    "modelGroupLoadStaleS",
    "maxTokensCeiling",
    "trustProxyIdentity",
    "trustForwardedGroups",
    "adminGroups",
    "ssoKeyCreateEnabled",
    "ssoSignOutUrl",
    "tokenValidation",
    "tokenShadowKeys",
    "tokenShadowTenantId",
    "tokenShadowKeyName",
    "tokenShadowRateLimitRpm",
    "tokenShadowRateLimitTpm",
    "enrollMemoryDefault",
    "enrollMemoryServer",
    "enrollMemoryNameRetries",
    "distillerEnabled",
    "distillerMaxTokens",
    "distillerMemoryServer",
    "distillerMemoryTool",
    "distillerMemoryProbeTool",
    "distillerGateBackoffS",
    "distillerRawFallback",
    "distillerMinDocChars",
    "distillerDisableThinking",
    "canonicalSkipTitleRequests",
    "offpeakEnabled",
    "offpeakCheckS",
    "offpeakPromql",
    "offpeakEnterPct",
    "offpeakExitPct",
    "offpeakFraction",
    "offpeakMcpServer",
    "offpeakTool",
    "offpeakModelPromql",
    "endpointMetricsDiskSelector",
    "audioTtsCharsPerToken",
    "audioAsrTokensPerSecond",
    "otelEnabled",
    "otelEndpoint",
    "otelServiceName",
    "otelSampleRatio",
    "otelHeaders",
]

helm_skip = pytest.mark.skipif(
    shutil.which("helm") is None,
    reason="helm binary not on PATH (chart-render tests need it)",
)


def _values() -> dict:
    return yaml.safe_load(VALUES_YAML.read_text())


def _leaf_paths(node: object, prefix: str = "") -> set[str]:
    """Every dotted leaf path under `node` (a map of maps).

    An EMPTY map is a leaf, not a branch: `maxTokensCeiling: {}` and
    `offpeak.modelPromql: {}` are real, settable keys (rendered as an empty
    JSON object), and a template referencing them must resolve."""
    out: set[str] = set()
    if not isinstance(node, dict) or not node:
        return {prefix}
    for key, value in node.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        out |= _leaf_paths(value, path)
    return out


def _resolves(path: str, leaves: set[str]) -> bool:
    """A reference resolves if it is a leaf, or a PREFIX of a leaf (a map the
    template iterates with `with`)."""
    return path in leaves or any(leaf.startswith(path + ".") for leaf in leaves)


# ---------------------------------------------------------------------------
# 1. the values file itself
# ---------------------------------------------------------------------------


class TestGroupedValuesLayout:
    def test_subsystem_sections_present_in_order(self) -> None:
        settings = _values()["settings"]
        assert list(settings) == SUBSYSTEMS, (
            "the settings block is grouped by subsystem; the section order IS the "
            f"reading order — expected {SUBSYSTEMS}, got {list(settings)}"
        )

    def test_no_flat_legacy_key_survives(self) -> None:
        settings = _values()["settings"]
        survivors = sorted(set(settings) & set(LEGACY_FLAT_KEYS))
        assert survivors == [], (
            "these keys belong to the OLD flat layout and must live under a subsystem "
            f"section now: {survivors}"
        )

    def test_every_legacy_key_is_actually_gone(self) -> None:
        # Guards the list itself: if a future edit re-adds a key to LEGACY_FLAT_KEYS
        # but the values file legitimately keeps that name, this catches the drift.
        settings = _values()["settings"]
        assert set(LEGACY_FLAT_KEYS) - set(settings) == set(LEGACY_FLAT_KEYS)


# ---------------------------------------------------------------------------
# 2. every template reference resolves (the phantom-knob guard)
# ---------------------------------------------------------------------------


class TestTemplateReferences:
    def test_every_settings_reference_resolves(self) -> None:
        leaves = _leaf_paths(_values()["settings"], "settings")
        broken: list[str] = []
        for path in sorted(TEMPLATES.iterdir()):
            if not path.is_file():
                continue
            text = path.read_text()
            for match in re.finditer(r"\.Values\.(settings(?:\.[A-Za-z0-9_]+)+)", text):
                ref = match.group(1)
                if not _resolves(ref, leaves):
                    broken.append(f"{path.name}: .Values.{ref}")
            # the nil-safe form: dig "relay" "reasoning" "enabled" <default> .Values.settings
            for match in re.finditer(r'dig\s+((?:"[A-Za-z0-9_]+"\s+)+)[^.]*\.Values\.settings', text):
                parts = re.findall(r'"([A-Za-z0-9_]+)"', match.group(1))
                ref = "settings." + ".".join(parts)
                if not _resolves(ref, leaves):
                    broken.append(f"{path.name}: dig {ref}")
        assert broken == [], (
            "a missing .Values.settings path renders an EMPTY env value (Helm does not "
            "error), leaving the pod on its code default while the operator thinks the "
            "chart configured it:\n" + "\n".join(broken)
        )

    def test_templates_do_not_reference_a_flat_key(self) -> None:
        legacy = set(LEGACY_FLAT_KEYS)
        hits: list[str] = []
        for path in sorted(TEMPLATES.iterdir()):
            if not path.is_file():
                continue
            for match in re.finditer(r"\.Values\.settings\.([A-Za-z0-9_]+)", path.read_text()):
                if match.group(1) in legacy:
                    hits.append(f"{path.name}: .Values.settings.{match.group(1)}")
        assert hits == [], "templates still read flat settings keys: " + ", ".join(hits)


# ---------------------------------------------------------------------------
# 3. the legacy-key render guard
# ---------------------------------------------------------------------------


class TestLegacyKeyRenderGuard:
    def test_guard_template_exists(self) -> None:
        assert (TEMPLATES / "values-guard.yaml").is_file(), (
            "helm/templates/values-guard.yaml is what turns a stale site values file "
            "from a SILENT no-op into a failed render"
        )

    @helm_skip
    @pytest.mark.parametrize(
        "body,needle",
        [
            # class 1: a legacy key still at the TOP level of settings
            ("settings:\n  toolRelayEnabled: false\n", "toolRelayEnabled"),
            ("settings:\n  dbPoolMax: 40\n", "dbPoolMax"),
            ("settings:\n  tokenValidation:\n    issuer: x\n", "tokenValidation"),
            # class 2: a legacy key NESTED under a new section — a half-migrated
            # file, which is what a hand-edit of a migrated file produces (the
            # old name is still in the surrounding comments). Top-level-only
            # detection would miss every one of these and the key would be
            # silently discarded.
            ("settings:\n  relay:\n    toolRelayEnabled: false\n", "toolRelayEnabled"),
            ("settings:\n  retention:\n    retentionTraceDays: 30\n", "retentionTraceDays"),
            ("settings:\n  offpeak:\n    offpeakPromql: x\n", "offpeakPromql"),
            ("settings:\n  mcp:\n    argPolicy:\n      mcpArgPolicyExempt: x\n", "mcpArgPolicyExempt"),
        ],
    )
    def test_stale_flat_site_values_fail_the_render(self, body: str, needle: str) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
            fh.write(body)
            override = fh.name
        try:
            proc = subprocess.run(
                ["helm", "template", "rel", str(HELM_CHART), "-f", override],
                capture_output=True,
                text=True,
                timeout=120,
            )
        finally:
            Path(override).unlink(missing_ok=True)
        assert proc.returncode != 0, (
            f"a values file carrying the flat key {needle!r} must FAIL the render — "
            "Helm would otherwise ignore it and the key would configure nothing"
        )
        assert needle in proc.stderr, f"the failure must name the offending key; got:\n{proc.stderr}"

    @helm_skip
    @pytest.mark.parametrize(
        "body",
        [
            # Keys whose SPELLING did not change must never be flagged — the
            # guard's leaf check is restricted to names that moved.
            "settings:\n  models:\n    maxTokensCeiling:\n      Qwen3.8-27B: 32768\n",
            "settings:\n  healthProbe:\n    enabled: true\n",
            "settings:\n  relay:\n    reasoning:\n      maxChars: 4096\n",
            "settings:\n  upstream:\n    failoverTtftS: 0\n",
            "settings: {}\n",
        ],
    )
    def test_current_layout_never_trips_the_guard(self, body: str) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
            fh.write(body)
            override = fh.name
        try:
            proc = subprocess.run(
                ["helm", "template", "rel", str(HELM_CHART), "-f", override],
                capture_output=True,
                text=True,
                timeout=120,
            )
        finally:
            Path(override).unlink(missing_ok=True)
        assert proc.returncode == 0, f"a VALID grouped values file must render; got:\n{proc.stderr}"

    @helm_skip
    def test_grouped_values_render_cleanly(self) -> None:
        # The guard must never fire on the chart's own (grouped) values.
        proc = subprocess.run(
            ["helm", "template", "rel", str(HELM_CHART)],
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert proc.returncode == 0, proc.stderr
        assert "settings." not in proc.stderr
