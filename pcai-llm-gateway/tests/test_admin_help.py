"""Admin console help panels: GET /admin/readiness + the [?] checklists.

WHY these exist: the three pages the console can configure — off-peak billing,
the memory distiller, SSO self-mint — are each gated by a BOOT-TIME env flag
that no admin GET exposed. An operator could save a profile, a fraction or a
threshold, see "saved", and then watch nothing happen with no surface anywhere
explaining why. The [?] beside each title answers that with a required-config
checklist whose state is LIVE.

The tests pin the two ways such a panel starts LYING:
  * a knob renamed in config.py but not in the help copy (drift), and
  * a [?] wired to a page that does not exist, or an entry nobody can open.
Plus the renderer's three states, run under node against real payloads — a
checklist that cannot tell "missing" from "unverifiable" is prose with a
checkbox drawn on it.
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pcai_llm.api import routes_admin
from pcai_llm.config import settings

ADMIN_HTML = Path(__file__).resolve().parents[1] / "src" / "pcai_llm" / "ui" / "admin.html"
HELP_TABS = ("offpeak", "distiller", "enroll")


def _h() -> dict[str, str]:
    return {"Authorization": "Bearer admin-secret"}


@pytest.fixture
def tc(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(settings, "admin_api_keys", "admin-secret")
    app = FastAPI()
    app.include_router(routes_admin.router)
    return TestClient(app, raise_server_exceptions=False)


def _help_source() -> tuple[str, str]:
    """(the whole page, the HELP map block) — the copy the tests inspect."""
    s = ADMIN_HTML.read_text(encoding="utf-8")
    start = s.index("const HELP = {")
    return s, s[start : s.index("\n};", start) + 3]


# ---------------------------------------------------------------------------
# the endpoint
# ---------------------------------------------------------------------------


def test_readiness_is_admin_gated(tc: TestClient) -> None:
    """The console's own key opens it; the deployment's posture is not public
    (it names the tenant the mint lands in)."""
    assert tc.get("/admin/readiness").status_code == 401
    r = tc.get("/admin/readiness", headers=_h())
    assert r.status_code == 200
    assert set(r.json()) == {"sso", "memory", "offpeak"}


def test_readiness_reports_the_settings_the_features_actually_gate_on(
    tc: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The values are the SAME settings attributes the enforcement paths read
    (routes_enroll's mint gate, core.identity's ladder, routes_distill's mount,
    core.offpeak's evaluator) — the panel cannot drift from the gate."""
    monkeypatch.setattr(settings, "trust_proxy_identity", True)
    monkeypatch.setattr(settings, "trust_forwarded_groups", False)
    monkeypatch.setattr(settings, "sso_key_create_enabled", True)
    monkeypatch.setattr(settings, "token_shadow_tenant_id", "0b6a4d0c-6a4e-4b7e-9a1e-2f3c4d5e6f70")
    monkeypatch.setattr(settings, "distiller_enabled", True)
    monkeypatch.setattr(settings, "distiller_memory_server", "mm-rag-g2")
    monkeypatch.setattr(settings, "distiller_memory_probe_tool", "memory_status")
    monkeypatch.setattr(settings, "distiller_memory_tool", "add_memory")
    monkeypatch.setattr(settings, "offpeak_enabled", True)
    monkeypatch.setattr(settings, "offpeak_mcp_server", "prometheus")
    monkeypatch.setattr(settings, "offpeak_tool", "prom_query")
    monkeypatch.setattr(settings, "offpeak_model_promql", {"Qwen/Qwen3-8B": "avg(gpu)"})
    monkeypatch.setattr(settings, "offpeak_enter_pct", 20.0)
    monkeypatch.setattr(settings, "offpeak_exit_pct", 35.0)

    b = tc.get("/admin/readiness", headers=_h()).json()
    assert b["sso"] == {
        "trust_proxy_identity": True,
        "trust_forwarded_groups": False,
        "sso_key_create_enabled": True,
        "token_shadow_tenant_id_set": True,
        "token_shadow_tenant_id_valid": True,
    }
    assert b["memory"] == {
        "distiller_enabled": True,
        "memory_server": "mm-rag-g2",
        "probe_tool": "memory_status",
        "write_tool": "add_memory",
        "enroll_memory_default": settings.enroll_memory_default,
    }
    assert b["offpeak"] == {
        "enabled": True,
        "mcp_server": "prometheus",
        "tool": "prom_query",
        "per_model_queries": 1,
        "enter_pct": 20.0,
        "exit_pct": 35.0,
        "fraction": settings.offpeak_fraction,
    }


@pytest.mark.parametrize(
    ("raw", "is_set", "valid"),
    [
        ("", False, False),
        ("   ", False, False),
        ("not-a-uuid", True, False),
        ("0b6a4d0c-6a4e-4b7e-9a1e-2f3c4d5e6f70", True, True),
    ],
)
def test_token_shadow_tenant_id_reports_set_and_valid_separately(
    tc: TestClient, monkeypatch: pytest.MonkeyPatch, raw: str, is_set: bool, valid: bool
) -> None:
    """Unset and malformed block the mint with the SAME 503, but they are
    different fixes — the help row names which one it is, so the two booleans
    must not collapse into one."""
    monkeypatch.setattr(settings, "token_shadow_tenant_id", raw)
    sso = tc.get("/admin/readiness", headers=_h()).json()["sso"]
    assert (sso["token_shadow_tenant_id_set"], sso["token_shadow_tenant_id_valid"]) == (is_set, valid)


# ---------------------------------------------------------------------------
# the console copy
# ---------------------------------------------------------------------------


def test_every_help_button_maps_to_a_real_page_and_a_copy_entry() -> None:
    """The key of a HELP entry IS a tab id, so a [?] can never open nothing —
    and a renamed tab cannot silently orphan its checklist."""
    s, help_block = _help_source()
    tabs = set(re.findall(r'data-tab="([^"]+)"', s))
    targets = re.findall(r'data-help="([^"]+)"', s)
    entries = dict(re.findall(r"\n  (\w+): \{\n    title: '([^']+)'", help_block))

    assert set(targets) == set(entries) == set(HELP_TABS), (targets, sorted(entries))
    assert set(entries) <= tabs, f"help entry keyed by a tab that does not exist: {set(entries) - tabs}"

    # the [?] rides the TITLE of the card it explains (the ask: "by their title")
    for title, key in (
        ("SSO self-mint default profile", "enroll"),
        ("Off-peak billing", "offpeak"),
        ("Session memory distiller", "distiller"),
    ):
        assert f'<h2>{title} <button class="help-btn" type="button" data-help="{key}"' in s, title

    # every panel carries the four sections that make it a checklist, not prose
    for key in HELP_TABS:
        block = help_block[help_block.index(f"\n  {key}: {{") :]
        block = block[: block.index("\n  },")]
        assert "items: [" in block and "verify:" in block and "docs:" in block, key
        assert block.count("state:") >= 3, f"{key}: expected live-state rows"


def test_help_copy_names_only_real_settings_knobs() -> None:
    """DRIFT GUARD. The copy spells env knobs as PCAI_LLM_<FIELD>; config.py's
    env_prefix is PCAI_LLM_ and pydantic lowercases the field, so every knob
    named in the help MUST resolve to a settings attribute. Rename a field and
    this fails instead of the panel quietly telling operators to set a knob
    that no longer exists."""
    _, help_block = _help_source()
    knobs = sorted(set(re.findall(r"PCAI_LLM_[A-Z0-9_]+", help_block)))
    assert knobs, "help copy names no knobs — the checklist lost its required configs"
    for knob in knobs:
        attr = knob[len("PCAI_LLM_") :].lower()
        assert hasattr(settings, attr), f"help copy names {knob}, but settings has no attribute {attr!r}"


def test_help_panel_is_a_scrolling_dialog_with_three_states() -> None:
    """Mechanics the copy depends on: the [?] opens the shared <dialog> (Esc and
    the focus trap come free), long panels scroll instead of clipping, and the
    state vocabulary is THREE-valued — a two-state checklist would have to
    claim "configured" about things no code can see (the proxy chain, the RAG
    star), which is how a help panel starts lying."""
    s, _ = _help_source()
    assert "api('/readiness')" in s
    assert "dialog.dlg-scroll" in s and "d.classList.toggle('dlg-scroll'" in s
    assert "HELP_GLYPH = { ok: '✓', no: '✗', un: '⚠' }" in s
    assert "help-ok" in s and "help-no" in s and "help-un" in s
    assert "data-help-close" in s  # in-dialog close, reachable without scrolling to the footer
    # the honesty section is present for the two features with unverifiable halves
    assert "Cannot be checked from here" in s


# ---------------------------------------------------------------------------
# the renderer, run under node against real payloads
# ---------------------------------------------------------------------------

_CONFIGURED = {
    "readiness": {
        "sso": {
            "trust_proxy_identity": True,
            "trust_forwarded_groups": False,
            "sso_key_create_enabled": True,
            "token_shadow_tenant_id_set": True,
            "token_shadow_tenant_id_valid": True,
        },
        "memory": {
            "distiller_enabled": True,
            "memory_server": "mm-rag",
            "probe_tool": "memory_status",
            "write_tool": "add_memory",
            "enroll_memory_default": False,
        },
        "offpeak": {
            "enabled": True,
            "mcp_server": "prometheus",
            "tool": "prom_query",
            "per_model_queries": 2,
            "enter_pct": 20.0,
            "exit_pct": 35.0,
            "fraction": 0.5,
        },
    },
    "servers": [{"name": "mm-rag", "enabled": True}, {"name": "prometheus", "enabled": True}],
    "distiller": {"config": {"enabled": True, "model": "Qwen/Qwen3-8B"}},
    "models": ["Qwen/Qwen3-8B"],
    "tenants": [{"sso_default_profile_id": "p1"}],
    "offpeak": {},
}

_UNCONFIGURED = {
    "readiness": {
        "sso": {
            "trust_proxy_identity": False,
            "trust_forwarded_groups": False,
            "sso_key_create_enabled": False,
            "token_shadow_tenant_id_set": True,
            "token_shadow_tenant_id_valid": False,
        },
        "memory": {
            "distiller_enabled": False,
            "memory_server": "mm-rag",
            "probe_tool": "memory_status",
            "write_tool": "add_memory",
            "enroll_memory_default": False,
        },
        "offpeak": {
            "enabled": False,
            "mcp_server": "prometheus",
            "tool": "prom_query",
            "per_model_queries": 0,
            "enter_pct": 40.0,
            "exit_pct": 30.0,
            "fraction": 0.5,
        },
    },
    "servers": [{"name": "prometheus", "enabled": False}],
    "distiller": {"config": {"enabled": False, "model": "typo-model"}},
    "models": [],
    "tenants": [],
    "offpeak": {},
}

_UNREADABLE = {k: None for k in ("readiness", "servers", "distiller", "models", "tenants", "offpeak")}

# The per-key credential (distiller) has no global state, so it stays ⚠ in
# EVERY posture — that is the contract, not an accident.
_EXPECTED = {
    "configured": {"offpeak": "ok ok ok ok", "distiller": "ok ok un ok ok", "enroll": "ok ok ok ok"},
    "unconfigured": {"offpeak": "no no no no", "distiller": "no no un no no", "enroll": "no no no no"},
    "unreadable": {"offpeak": "un un un un", "distiller": "un un un un un", "enroll": "un un un un"},
}


def test_help_renderer_three_states_behaviour() -> None:
    """Extract the helpers + the HELP map from the page and run them under node
    (the same behavioural-test pattern the console's rich-render test uses):
    ✓ where the deployment is configured, ✗ where it is not, ⚠ where the read
    itself failed — and a hostile server name comes out ESCAPED."""
    s = ADMIN_HTML.read_text(encoding="utf-8")
    esc_fn = re.search(r"function esc\(s\) \{.*?\n\}", s, re.DOTALL)
    assert esc_fn, "esc() not extractable from admin.html"
    helpers = s[s.index("const HELP_GLYPH") : s.index("const HELP = {")]
    start = s.index("const HELP = {")
    help_map = s[start : s.index("\n};", start) + 3]

    harness = """
const STATES = /<th class="help-(ok|no|un)"/g;
const run = (d) => {
  const out = {};
  for (const key of Object.keys(HELP)) {
    out[key] = [...helpHtml(HELP[key], d).matchAll(STATES)].map(m => m[1]).join(' ');
  }
  return out;
};
const D = JSON.parse(process.argv[2]);
console.log(JSON.stringify({
  configured: run(D.configured),
  unconfigured: run(D.unconfigured),
  unreadable: run(D.unreadable),
  hostile: helpHtml(HELP.offpeak, D.hostile),
}));
"""
    payload = json.dumps(
        {
            "configured": _CONFIGURED,
            "unconfigured": _UNCONFIGURED,
            "unreadable": _UNREADABLE,
            "hostile": {
                "readiness": {
                    "offpeak": {
                        "enabled": True,
                        "mcp_server": 'x"><img src=x onerror=alert(1)>',
                        "tool": "t",
                        "per_model_queries": 0,
                        "enter_pct": 1.0,
                        "exit_pct": 2.0,
                    }
                },
                "servers": [],
                "distiller": None,
                "models": None,
                "tenants": None,
                "offpeak": None,
            },
        }
    )
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
        f.write(esc_fn.group(0) + "\n" + helpers + "\n" + help_map + "\n" + harness)
        path = f.name
    r = subprocess.run(["node", path, payload], capture_output=True, text=True)
    assert r.returncode == 0, f"help renderer failed under node:\n{r.stdout}\n{r.stderr}"
    got = json.loads(r.stdout)
    assert got["configured"] == _EXPECTED["configured"]
    assert got["unconfigured"] == _EXPECTED["unconfigured"]
    assert got["unreadable"] == _EXPECTED["unreadable"]
    assert "<img" not in got["hostile"], "hostile server name reached the panel as markup"
    assert "&lt;img" in got["hostile"], "hostile server name was not escaped"
