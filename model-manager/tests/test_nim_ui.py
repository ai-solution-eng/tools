"""UI contract tests for the NIM profile-mirror source switch (Zone C).

The names pinned here are exact on purpose: the form posts the field names the
SubmitRequest validators expect, and the jobs / mirrors tables read the keys
the queue records expose, so a UI-side rename would break the API silently.
Static substring checks cover the template, app.js and style.css; the
live-render tests drive the real FastAPI app.

psycopg2 is stubbed when missing — same shim contract as
test_manual_delete_wiring.py (it is only needed by the AIOLI reader, the app
image has it, a dev box may not). If the app import still fails in this
environment the live tests skip instead of failing. The NIM-enabled render
assertion also skips while the app module predates the NIM wiring (Zone A),
so this file stays green before and after that merge.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INDEX_HTML = ROOT / "src" / "model_manager" / "app" / "templates" / "index.html"
APP_JS = ROOT / "src" / "model_manager" / "app" / "static" / "app.js"
STYLE_CSS = ROOT / "src" / "model_manager" / "app" / "static" / "style.css"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# The app seeds its catalog and reads the job-template ConfigMap name at
# import; point both at throwaway locations so an off-cluster import never
# touches /mnt. setdefault keeps this compatible with whichever test module
# imports the app first in a shared pytest run.
_TMP = tempfile.mkdtemp(prefix="md-nim-ui-")
os.environ.setdefault("JOB_TEMPLATE_CONFIGMAP", "test-template")
os.environ.setdefault("JOB_TEMPLATE_CONFIGMAP_NAMESPACE", "test-ns")
os.environ.setdefault("CATALOG_PATH", os.path.join(_TMP, "catalog.json"))


def _install_psycopg2_shim() -> None:
    if "psycopg2" in sys.modules:  # pragma: no cover - already provided
        return
    _pg = types.ModuleType("psycopg2")
    _pg.__path__ = []  # mark as a package so psycopg2.extras resolves
    _pg.connect = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no db in tests"))
    _ext = types.ModuleType("psycopg2.extensions")
    _ext.ISOLATION_LEVEL_AUTOCOMMIT = 0
    _extras = types.ModuleType("psycopg2.extras")
    _extras.Json = lambda value: value
    sys.modules["psycopg2"] = _pg
    sys.modules["psycopg2.extensions"] = _ext
    sys.modules["psycopg2.extras"] = _extras


@pytest.fixture(scope="module")
def md():
    _install_psycopg2_shim()
    try:
        from model_manager.app import main as md
    except Exception as e:  # pragma: no cover - environment-dependent
        pytest.skip(f"app import failed in this environment: {e}")
    return md


@pytest.fixture()
def client(md):
    from fastapi.testclient import TestClient

    return TestClient(md.app)


@pytest.fixture()
def nim_on(md, monkeypatch):
    if not hasattr(md, "NIM_AVAILABLE"):
        pytest.skip("app NIM wiring (Zone A) not merged yet")
    monkeypatch.setattr(md, "NIM_AVAILABLE", True)
    monkeypatch.setattr(md, "NIM_BUCKET", "nim-mirror-box")
    monkeypatch.setattr(md, "NIM_REGION", "us-east-1")


# ---------------------------------------------------------------------------
# static assertions: template
# ---------------------------------------------------------------------------


class TestTemplateStatic:
    def test_source_segmented_control(self):
        html = _read(INDEX_HTML)
        assert 'id="source-seg"' in html
        assert 'name="source"' in html
        assert 'id="source-input"' in html
        assert 'data-source="hf"' in html
        assert 'data-source="nim"' in html

    def test_nim_fields_present(self):
        html = _read(INDEX_HTML)
        for present in (
            'name="nim_image"',
            'name="ngc_api_key"',
            'name="nim_profiles"',
            'class="nim-field"',
            "nim-note",
        ):
            assert present in html, present

    def test_nim_enabled_guards(self):
        # Segmented control, NIM fields and the mirrors table are each behind
        # the same guard, so a NIM-disabled page renders exactly as before.
        html = _read(INDEX_HTML)
        assert html.count("{% if nim_enabled %}") >= 3

    def test_mirrors_table_columns(self):
        html = _read(INDEX_HTML)
        assert 'id="nim-mirrors"' in html
        for col in ("<th>Image</th>", "<th>Profiles</th>", "<th>Bucket</th>", "<th>Status</th>"):
            assert col in html, col

    def test_hf_fields_not_dropped_or_renamed(self):
        # Byte-compatibility guard for the HF submit path: every pre-NIM field
        # keeps its exact name, and the hf-field hook exists for JS toggling.
        html = _read(INDEX_HTML)
        for present in (
            'name="model_name"',
            'name="hf_token"',
            'name="storage"',
            'name="s3_path"',
            'name="cache_root"',
            'name="chat_template_path"',
            'name="chat_template_contents"',
            'class="hf-field"',
        ):
            assert present in html, present


# ---------------------------------------------------------------------------
# static assertions: app.js
# ---------------------------------------------------------------------------


class TestAppJsStatic:
    def test_source_wiring_symbols(self):
        js = _read(APP_JS)
        for present in (
            "syncSourceFields",
            "data-source",
            "source-input",
            "nim_image",
            "ngc_api_key",
            "output_url",
            "renderNimMirrors",
        ):
            assert present in js, present

    def test_output_url_fallback(self):
        # NIM rows carry output_url (s3://bucket); HF rows keep pvc_url.
        js = _read(APP_JS)
        assert "j.output_url || j.pvc_url" in js

    def test_source_key_only_when_selector_exists(self):
        # A NIM-disabled page must keep the exact pre-NIM POST body: the
        # source key is only added when the segmented control rendered.
        js = _read(APP_JS)
        assert "if (sourceInput) body.source = sourceInput.value;" in js

    def test_js_parses(self):
        node = shutil.which("node")
        if not node:
            pytest.skip("node not available for a syntax check")
        proc = subprocess.run([node, "--check", str(APP_JS)], capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr


# ---------------------------------------------------------------------------
# static assertions: style.css
# ---------------------------------------------------------------------------


class TestStyleStatic:
    def test_nim_selectors_exist(self):
        css = _read(STYLE_CSS)
        for present in (".source-seg", ".src-badge", ".nim-note"):
            assert present in css, present


# ---------------------------------------------------------------------------
# live render: the real app, real Jinja pass
# ---------------------------------------------------------------------------


class TestLiveRenderDisabled:
    def test_no_nim_ui_when_disabled(self, client, md, monkeypatch):
        # Pin the gate explicitly: another test module may have imported the
        # app with NIM enabled (test_nim_wiring.py sets the env at collection
        # time), and main.py freezes NIM_AVAILABLE at import. Without this the
        # assertion passes standalone but fails in the combined command.
        monkeypatch.setattr(md, "NIM_AVAILABLE", False)
        monkeypatch.setattr(md, "NIM_BUCKET", "")
        html = client.get("/").text
        for absent in (
            'id="source-seg"',
            'name="nim_image"',
            'name="ngc_api_key"',
            'name="nim_profiles"',
            'id="nim-mirrors"',
            "NIM_REPOSITORY_OVERRIDE",
        ):
            assert absent not in html, absent

    def test_hf_form_intact(self, client):
        html = client.get("/").text
        for present in (
            'id="submit-form"',
            'name="namespace"',
            'name="model_name"',
            'name="hf_token"',
            'name="storage"',
        ):
            assert present in html, present


class TestLiveRenderEnabled:
    def test_source_control_and_nim_fields(self, client, nim_on):
        html = client.get("/").text
        assert 'id="source-seg"' in html
        for present in (
            'id="source-input"',
            'data-source="hf"',
            'data-source="nim"',
            'name="nim_image"',
            'name="ngc_api_key"',
            'name="nim_profiles"',
            'id="nim-mirrors"',
        ):
            assert present in html, present

    def test_bucket_and_override_reach_the_page(self, client, nim_on):
        # The target note must show the real bucket and the exact env var the
        # serving side consumes.
        html = client.get("/").text
        assert "s3://nim-mirror-box" in html
        assert "NIM_REPOSITORY_OVERRIDE=s3://nim-mirror-box" in html
