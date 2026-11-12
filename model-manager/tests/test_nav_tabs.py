"""Header navigation contract: three tabs, the third one gated on the feature.

The manual-deletion route returns 404 when MANUAL_DELETE_ENABLED is false, so the
third tab has to be conditional or every other deployment ships a dead link.
Static checks pin the markup in all three templates; the live-render checks drive
the real FastAPI app so the context wiring is exercised, not just the template.

The rename guard is here too: the product name is Model Manager. The chart,
Python package and cluster identifiers are model-manager; the image repos and UI
host keep the model-downloader identifier, and legacy model-downloader labels and
annotations are still accepted on read.
"""

import os
import re
import sys
import tempfile
import types
from pathlib import Path

if "psycopg2" not in sys.modules:  # pragma: no cover - environment shim
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

_TMP = tempfile.mkdtemp(prefix="md-nav-")
os.environ.setdefault("JOB_TEMPLATE_CONFIGMAP", "test-template")
os.environ.setdefault("JOB_TEMPLATE_CONFIGMAP_NAMESPACE", "test-ns")
# The app seeds its catalog on import; without this it writes to /mnt, which is
# read-only off-cluster.
os.environ.setdefault("CATALOG_PATH", os.path.join(_TMP, "catalog.json"))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from model_manager.app import main as md  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "src" / "model_manager" / "app" / "templates"
PAGES = {
    "/": ("index.html", "/"),
    "/catalog": ("catalog.html", "/catalog"),
    "/admin/delete": ("admin_delete.html", "/admin/delete"),
}
LABELS = ["Download", "Model Catalog", "Manual Deletion"]

NAV_RE = re.compile(r'<nav class="main-nav">(.*?)</nav>', re.DOTALL)
LINK_RE = re.compile(r'<a href="([^"]+)" class="([^"]*)"([^>]*)>([^<]+)</a>')


def _nav(html: str) -> list[tuple[str, str, str, str]]:
    """(href, class, other-attrs, label) for every link in the header nav."""
    match = NAV_RE.search(html)
    assert match, 'page has no <nav class="main-nav"> block'
    return LINK_RE.findall(match.group(1))


@pytest.fixture()
def client():
    return TestClient(md.app)


@pytest.fixture()
def delete_on(monkeypatch):
    monkeypatch.setattr(md, "MANUAL_DELETE_ENABLED", True)


@pytest.fixture()
def delete_off(monkeypatch):
    monkeypatch.setattr(md, "MANUAL_DELETE_ENABLED", False)


class TestTemplatesStatic:
    def test_every_page_carries_all_three_tabs(self):
        for name in ("index.html", "catalog.html", "admin_delete.html"):
            html = (TEMPLATES / name).read_text(encoding="utf-8")
            hrefs = [link[0] for link in _nav(html)]
            assert hrefs == ["/", "/catalog", "/admin/delete"], name
            assert [link[3] for link in _nav(html)] == LABELS, name

    def test_delete_tab_is_conditional_on_the_feature_flag(self):
        for name in ("index.html", "catalog.html", "admin_delete.html"):
            html = (TEMPLATES / name).read_text(encoding="utf-8")
            nav = NAV_RE.search(html).group(1)
            assert "{%- if manual_delete_enabled %}" in nav, name
            assert nav.count("{%- endif %}") == 1, name

    def test_each_page_marks_only_its_own_tab_active(self):
        for name, own_href in PAGES.values():
            html = (TEMPLATES / name).read_text(encoding="utf-8")
            active = [link[0] for link in _nav(html) if "active" in link[1].split()]
            assert active == [own_href], (name, active)
            marked = [link[0] for link in _nav(html) if 'aria-current="page"' in link[2]]
            assert marked == [own_href], (name, marked)

    def test_product_name_is_model_manager(self):
        for name in ("index.html", "catalog.html", "admin_delete.html"):
            html = (TEMPLATES / name).read_text(encoding="utf-8")
            assert '<span class="app-title">Model Manager</span>' in html, name
            assert "Model Downloader" not in html, name
            assert "<title>Model Manager</title>" in html or "Model Manager</title>" in html, name


class TestLivePages:
    def test_three_tabs_render_when_the_feature_is_on(self, client, delete_on):
        for path, (_, own_href) in PAGES.items():
            response = client.get(path)
            assert response.status_code == 200, path
            links = _nav(response.text)
            assert [link[0] for link in links] == ["/", "/catalog", "/admin/delete"], path
            assert [link[3] for link in links] == LABELS, path
            active = [link[0] for link in links if "active" in link[1].split()]
            assert active == [own_href], (path, active)

    def test_two_tabs_render_when_the_feature_is_off(self, client, delete_off):
        for path in ("/", "/catalog"):
            response = client.get(path)
            assert response.status_code == 200, path
            assert [link[0] for link in _nav(response.text)] == ["/", "/catalog"], path

    def test_delete_page_is_404_when_the_feature_is_off(self, client, delete_off):
        assert client.get("/admin/delete").status_code == 404

    def test_pages_render_the_new_product_name(self, client, delete_on):
        for path in PAGES:
            html = client.get(path).text
            assert '<span class="app-title">Model Manager</span>' in html, path
            assert "Model Downloader" not in html, path
