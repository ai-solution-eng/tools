"""The catalog's MLIS link and push feedback contract.

Two user-facing gaps this pins down:

* **The MLIS home page link.** The catalog renders an "Open MLIS" link beside
  the push button from `catalog.mlisUrl` (default
  `https://mlis.${DOMAIN_NAME}`, which PCAI substitutes).  The link must be a
  real anchor (middle-click / copy-address), open in a new tab, and disappear
  when the value is empty or still an unresolved placeholder.
* **Push feedback.** Pushing worked but said nothing: the per-model result
  table renders at the BOTTOM of the page, so a push started from the top
  looked like a no-op.  The script must report progress and the outcome in the
  status line beside the buttons, disable the push buttons while the request is
  in flight, and scroll the result table into view.

Static checks pin the markup and the script; the live-render checks drive the
real FastAPI app; the helm checks pin the value -> env wiring (default,
override, empty).
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import types
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

TEMPLATES = REPO / "src" / "model_manager" / "app" / "templates"
STATIC = REPO / "src" / "model_manager" / "app" / "static"
HELM_DIR = REPO / "helm"

_TMP = tempfile.mkdtemp(prefix="md-mlis-link-")
os.environ.setdefault("JOB_TEMPLATE_CONFIGMAP", "test-template")
os.environ.setdefault("JOB_TEMPLATE_CONFIGMAP_NAMESPACE", "test-ns")
# The app seeds its catalog on import; point it at a throwaway path so an
# off-cluster import never touches /mnt.
os.environ.setdefault("CATALOG_PATH", os.path.join(_TMP, "catalog.json"))


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _install_psycopg2_shim() -> None:
    if "psycopg2" in sys.modules:  # pragma: no cover - already provided
        return

    def _no_db(*_args, **_kwargs):
        raise RuntimeError("no db in tests")

    _pg = types.ModuleType("psycopg2")
    _pg.__path__ = []  # mark as a package so psycopg2.extras resolves
    _pg.connect = _no_db  # type: ignore[attr-defined]
    _ext = types.ModuleType("psycopg2.extensions")
    _ext.ISOLATION_LEVEL_AUTOCOMMIT = 0  # type: ignore[attr-defined]
    _extras = types.ModuleType("psycopg2.extras")
    _extras.Json = lambda value: value  # type: ignore[attr-defined]
    sys.modules["psycopg2"] = _pg
    sys.modules["psycopg2.extensions"] = _ext
    sys.modules["psycopg2.extras"] = _extras


@pytest.fixture(scope="module")
def app_module():
    _install_psycopg2_shim()
    try:
        from model_manager.app import main as md
    except Exception as exc:  # pragma: no cover - dev box without app deps
        pytest.skip(f"app import unavailable: {exc}")
    return md


@pytest.fixture(scope="module")
def client(app_module):
    from fastapi.testclient import TestClient

    return TestClient(app_module.app)


# ---- template / script contract ----


def test_template_renders_a_real_anchor_gated_on_the_url():
    html = _read(TEMPLATES / "catalog.html")
    assert "{% if mlis_url %}" in html
    assert 'id="mlis-link"' in html
    assert 'href="{{ mlis_url }}"' in html
    # A new tab, with the opener severed (the URL is operator-supplied).
    assert 'target="_blank"' in html
    assert 'rel="noopener noreferrer"' in html


def test_script_reports_push_progress_and_outcome_beside_the_buttons():
    js = _read(STATIC / "catalog.js")
    assert "function pushStatusText(data)" in js
    assert "setStatus('Pushing ' + configs.length" in js
    assert "setStatus(pushStatusText(data)" in js
    assert "setStatus('Push to MLIS failed: " in js
    # The result table lives at the bottom: bring it into view.
    assert "pushResults.scrollIntoView(" in js


def test_script_disables_both_push_buttons_while_in_flight():
    js = _read(STATIC / "catalog.js")
    assert "function setPushBusy(busy)" in js
    assert "'push-selected-btn', 'push-direct-btn'" in js
    assert "setPushBusy(true)" in js
    assert "setPushBusy(false)" in js


def test_push_result_summary_links_back_to_mlis():
    js = _read(STATIC / "catalog.js")
    assert "document.getElementById('mlis-link')" in js
    assert "Open MLIS" in js


# ---- URL resolution ----


def test_resolved_url_is_used_verbatim(app_module):
    assert app_module._resolve_mlis_url("https://mlis.example.com") == "https://mlis.example.com"
    assert app_module._resolve_mlis_url("  https://mlis.example.com  ") == "https://mlis.example.com"


def test_empty_value_means_no_link(app_module):
    assert app_module._resolve_mlis_url("") == ""
    assert app_module._resolve_mlis_url("   ") == ""
    assert app_module._resolve_mlis_url(None) == ""  # type: ignore[arg-type]


def test_unresolved_placeholder_is_not_a_link(app_module):
    """A render outside PCAI leaves the literal ${DOMAIN_NAME}: hide it."""
    assert app_module._resolve_mlis_url("https://mlis.${DOMAIN_NAME}") == ""


# ---- live page ----


def test_catalog_page_renders_the_link_when_configured(client, app_module, monkeypatch):
    monkeypatch.setattr(app_module, "MLIS_URL", "https://mlis.pcai.example.net")
    html = client.get("/catalog").text
    assert 'id="mlis-link"' in html
    assert 'href="https://mlis.pcai.example.net"' in html
    assert "Open MLIS" in html


def test_catalog_page_hides_the_link_when_unset(client, app_module, monkeypatch):
    monkeypatch.setattr(app_module, "MLIS_URL", "")
    html = client.get("/catalog").text
    assert 'id="mlis-link"' not in html
    assert "Open MLIS" not in html


# ---- helm wiring ----


def _deployment_env(*sets: str) -> dict[str, str]:
    """Render the Deployment and return its env as a name -> value map."""
    cmd = ["helm", "template", "mdtest", ".", "-n", "project-user-x", "--show-only", "templates/deployment.yaml"]
    for s in sets:
        cmd += ["--set", s]
    proc = subprocess.run(cmd, cwd=HELM_DIR, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    doc = next(d for d in yaml.safe_load_all(proc.stdout) if d)
    env = doc["spec"]["template"]["spec"]["containers"][0]["env"]
    return {e["name"]: e.get("value", "") for e in env}


def test_chart_default_follows_the_pcai_domain():
    env = _deployment_env()
    assert env["MLIS_URL"] == "https://mlis.${DOMAIN_NAME}"


def test_chart_override_is_used_verbatim():
    env = _deployment_env("catalog.mlisUrl=https://mlis.example.com")
    assert env["MLIS_URL"] == "https://mlis.example.com"


def test_chart_empty_value_omits_the_env():
    env = _deployment_env("catalog.mlisUrl=")
    assert "MLIS_URL" not in env
