"""Built-in agent presets — the shipped definitions and their installer contract.

WHAT THIS FILE PINS
-------------------
1. INSTALLABLE: the preset's body is accepted by the admin route's OWN request
   model, its name is one the resolver would reach, and its prompt fits the
   API's 8 000-char cap. A preset that fails these is not a config choice — it
   is an install that would 422 at a deployment window.
2. READ-ONLY BY CONSTRUCTION: the sweep's server list omits applygate and its
   tool narrowing excludes every mutating tool the reference fleet exposes
   (exec/kubectl/secrets on k8s-ops, the saved-query writer on prometheus, the
   export writer on logsearch). The prompt says "report-only" too, but the
   surface is what enforces it.
3. NAMES ARE REAL: every narrowed tool exists in the reference fleet catalog
   pinned below. A typo is silent at request time (narrowing can only hide, so
   an unknown name matches nothing), which is exactly why it must fail here.
4. THE DETERMINISTIC CONTRACT: fixed procedure, priority ladder, output
   schema, sort keys, and the prompt version echoed into every report.
5. THE INSTALLER'S GUARD: scripts/seed_agents.py refuses a preset whose
   servers are absent from the live registry (and downgrades to a warning only
   under --force).
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
from typing import Any

import pytest

from pcai_llm.api import routes_admin
from pcai_llm.core import agent_presets
from pcai_llm.core.agents import RESERVED_NAMES

# The reference fleet's MCP catalog (live list_tools, 2026-11-06). Pinned here
# on purpose: a server that renames or drops a tool must fail a test at review
# time, not silently hide the tool when a sweep needs it.
_CATALOG: dict[str, frozenset[str]] = {
    "prometheus": frozenset(
        {
            "prom_alerts",
            "prom_label_values",
            "prom_query",
            "prom_query_range",
            "prom_rules",
            "prom_series",
            "query_delete",
            "query_list",
            "query_save",
            "query_saved",
        }
    ),
    "k8s-ops": frozenset(
        {
            "check_rbac",
            "cluster_health",
            "describe_resource",
            "exec_in_pod",
            "get_configmap",
            "get_custom_resource",
            "get_events",
            "get_pod_logs",
            "get_resource",
            "list_api_resources",
            "list_crds",
            "list_namespaces",
            "list_pods",
            "list_pvcs",
            "list_secrets",
            "list_services",
            "list_virtual_services",
            "list_workloads",
            "run_kubectl",
            "triage",
        }
    ),
    "logsearch": frozenset(
        {
            "count_matches",
            "export_matches",
            "get_pod_logs",
            "list_log_sources",
            "search_logs",
        }
    ),
}

# Tools that change something (or write a file to a host path). None may appear
# in a report-only sweep's surface, whatever the prompt says.
_MUTATING = {
    "applygate": frozenset({"plan_apply", "apply_manifest", "delete_manifest"}),
    "prometheus": frozenset({"query_save", "query_delete"}),
    "k8s-ops": frozenset({"exec_in_pod", "run_kubectl", "list_secrets", "get_configmap"}),
    "logsearch": frozenset({"export_matches"}),
}


def _load_seeder() -> Any:
    """scripts/ is not a package; load the installer by path (no sys.path edit)."""
    path = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "seed_agents.py"
    spec = importlib.util.spec_from_file_location("seed_agents", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# installable
# ---------------------------------------------------------------------------


def test_preset_body_is_accepted_by_the_admin_request_model() -> None:
    """The seeder posts this body verbatim; the route's model is the contract."""
    body = agent_presets.PROMETHEUS_SWEEP.upsert_body()
    parsed = routes_admin.AgentUpsert(**body)
    assert parsed.name == "prometheus-sweep"
    assert parsed.servers == ["prometheus", "k8s-ops", "logsearch"]
    assert parsed.tools["prometheus"][0] == "prom_alerts"
    # '' = the profile cascade decides; the procedure survives a model swap.
    assert parsed.model == ""
    assert parsed.enabled is True


def test_preset_name_is_one_the_resolver_can_reach() -> None:
    name = agent_presets.PROMETHEUS_SWEEP.name
    assert name == name.strip() and name
    assert name not in RESERVED_NAMES
    assert not name.startswith("routing")


def test_prompt_and_description_fit_the_api_caps() -> None:
    preset = agent_presets.PROMETHEUS_SWEEP
    assert len(preset.system_prompt) <= agent_presets.MAX_SYSTEM_PROMPT_CHARS
    assert len(preset.description) <= 400


def test_body_survives_a_json_round_trip() -> None:
    body = agent_presets.PROMETHEUS_SWEEP.upsert_body()
    assert json.loads(json.dumps(body)) == body


def test_presets_registry_is_keyed_by_the_preset_name() -> None:
    for name, preset in agent_presets.PRESETS.items():
        assert name == preset.name


# ---------------------------------------------------------------------------
# report-only by construction
# ---------------------------------------------------------------------------


def test_sweep_cannot_reach_applygate() -> None:
    assert "applygate" not in agent_presets.PROMETHEUS_SWEEP.servers


def test_no_mutating_tool_appears_in_the_surface() -> None:
    preset = agent_presets.PROMETHEUS_SWEEP
    for server, names in preset.tools.items():
        forbidden = _MUTATING.get(server, frozenset())
        assert not (set(names) & forbidden), (server, sorted(set(names) & forbidden))
    # A tool name that reads like a verb must not have slipped in either.
    for names in preset.tools.values():
        for tool in names:
            assert not any(tool.startswith(prefix) for prefix in ("apply_", "delete_", "patch_", "scale_", "exec_")), (
                tool
            )


# ---------------------------------------------------------------------------
# names are real
# ---------------------------------------------------------------------------


def test_every_named_tool_exists_in_the_reference_catalog() -> None:
    preset = agent_presets.PROMETHEUS_SWEEP
    assert set(preset.tools) == set(preset.servers)
    for server, names in preset.tools.items():
        assert server in _CATALOG, f"unknown server {server!r} — update _CATALOG"
        missing = sorted(set(names) - _CATALOG[server])
        assert not missing, f"{server}: not in the fleet catalog: {missing}"


def test_tool_surface_covers_the_sweep_essentials() -> None:
    tools = agent_presets.PROMETHEUS_SWEEP.tools
    assert {"prom_alerts", "prom_query", "prom_query_range", "prom_rules"} <= set(tools["prometheus"])
    assert {"triage", "describe_resource", "get_events"} <= set(tools["k8s-ops"])
    assert {"count_matches", "search_logs"} <= set(tools["logsearch"])


# ---------------------------------------------------------------------------
# the deterministic contract
# ---------------------------------------------------------------------------


def test_prompt_carries_its_version() -> None:
    """The model echoes this into every report, so a stored row's provenance
    is answerable from its own output."""
    assert agent_presets.PROMPT_VERSION in agent_presets.SWEEP_SYSTEM_PROMPT


def test_prompt_pins_the_procedure_ladder_and_schema() -> None:
    prompt = agent_presets.SWEEP_SYSTEM_PROMPT
    for marker in (
        "PROCEDURE (fixed order)",
        "prom_alerts ONCE",
        "prom_rules ONCE",
        "P1 act now",
        "P2 schedule",
        "P3 noise",
        "ORDERING (deterministic)",
        '"schema_version":"sweep-1"',
        '"counts"',
        '"decisions_owed"',
        "unverified",
    ):
        assert marker in prompt, marker


def test_prompt_is_report_only() -> None:
    prompt = agent_presets.SWEEP_SYSTEM_PROMPT
    assert "READ-ONLY" in prompt
    assert "cannot change the cluster" in prompt
    assert "Report-only" in prompt
    assert "never claim to have changed anything" in prompt


# ---------------------------------------------------------------------------
# the installer's guard
# ---------------------------------------------------------------------------


def test_seeder_refuses_a_preset_whose_servers_are_absent() -> None:
    seeder = _load_seeder()
    problems = seeder.validate(agent_presets.PROMETHEUS_SWEEP, {"prometheus"}, None, force=False)
    assert any("k8s-ops" in p and "logsearch" in p for p in problems)


def test_seeder_accepts_the_shipped_preset() -> None:
    seeder = _load_seeder()
    live = set(agent_presets.PROMETHEUS_SWEEP.servers)
    assert seeder.validate(agent_presets.PROMETHEUS_SWEEP, live, None, force=False) == []


def test_seeder_force_downgrades_problems_to_warnings(capsys: pytest.CaptureFixture[str]) -> None:
    seeder = _load_seeder()
    problems = seeder.validate(agent_presets.PROMETHEUS_SWEEP, {"prometheus"}, None, force=True)
    assert problems == []
    assert "--force" in capsys.readouterr().out


def test_seeder_refuses_an_oversized_prompt() -> None:
    seeder = _load_seeder()
    huge = agent_presets.AgentPreset(
        name="too-big",
        description="x",
        servers=(),
        tools={},
        model="",
        enabled=True,
        system_prompt="x" * (agent_presets.MAX_SYSTEM_PROMPT_CHARS + 1),
        prompt_version="test",
    )
    problems = seeder.validate(huge, set(), None, force=False)
    assert any("caps it at" in p for p in problems)


class _FakeGateway:
    """Only the one call the tool check makes."""

    def __init__(self, catalogs: dict[str, list[dict[str, Any]]]) -> None:
        self._catalogs = catalogs

    def call(self, method: str, path: str, body: Any = None) -> Any:
        server = path.rsplit("/", 2)[-2]
        return {"tools": self._catalogs.get(server, [])}


def _catalogs(*, denied: str | None = None) -> dict[str, list[dict[str, Any]]]:
    return {
        server: [{"tool": name, "allowed": name != denied} for name in sorted(names)]
        for server, names in _CATALOG.items()
    }


def test_seeder_refuses_a_tool_the_server_policy_denies() -> None:
    """A catalog entry the server's OWN allowlist denies installs cleanly and
    is then denied at call time — dead config that reads as a working sweep."""
    seeder = _load_seeder()
    live = set(agent_presets.PROMETHEUS_SWEEP.servers)
    problems = seeder.validate(
        agent_presets.PROMETHEUS_SWEEP, live, _FakeGateway(_catalogs(denied="prom_alerts")), force=False
    )
    assert any("does not admit" in p and "prom_alerts" in p for p in problems)


def test_seeder_accepts_the_preset_against_a_healthy_catalog() -> None:
    seeder = _load_seeder()
    live = set(agent_presets.PROMETHEUS_SWEEP.servers)
    assert seeder.validate(agent_presets.PROMETHEUS_SWEEP, live, _FakeGateway(_catalogs()), force=False) == []
