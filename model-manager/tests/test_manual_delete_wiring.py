"""Wiring tests for the admin manual-deletion endpoints (main.py).

These exercise the real FastAPI app, so they cover the parts a pure-function
test cannot: the auth gate, the isolation rule (a target must come from the
current scan), the verdict_id pinning, and the override rules.

psycopg2 is stubbed: it is only needed by the AIOLI reader, which every test
here replaces with a fake. The app image has it; a dev box may not.
"""

import sys
import types

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

import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="md-wiring-")
os.environ.setdefault("JOB_TEMPLATE_CONFIGMAP", "test-template")
os.environ.setdefault("JOB_TEMPLATE_CONFIGMAP_NAMESPACE", "test-ns")
# The app seeds its catalog on import; without this it writes to /mnt, which is
# read-only off-cluster.
os.environ["CATALOG_PATH"] = os.path.join(_TMP, "catalog.json")
os.environ["MANUAL_DELETE_ENABLED"] = "true"
os.environ["MANUAL_DELETE_AUTH_MODE"] = "sso"
os.environ["MANUAL_DELETE_ADMINS"] = "andrew"
os.environ["MANUAL_DELETE_ADMIN_GROUPS"] = "mlis-admins"
os.environ["MANUAL_DELETE_SECRET_NAME"] = "model-delete-key"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from model_manager.app import main as md  # noqa: E402

ADMIN = {"X-Auth-Request-Preferred-Username": "andrew"}
GROUP_ADMIN = {"X-Auth-Request-Preferred-Username": "jane", "X-Auth-Request-Groups": "staff, mlis-admins"}
NOT_ADMIN = {"X-Auth-Request-Preferred-Username": "bob", "X-Auth-Request-Groups": "staff"}

PATH_A = "/mnt/large-models/models--acme--Alpha"
PATH_B = "/mnt/large-models/models--acme--Beta"


class _M:
    def __init__(self, name, cachepath, nbytes=100, manifest=True, scanned="clean", mtime=1_700_000_000.0):
        self.model_name = name
        self.cachepath = cachepath
        self.bytes = nbytes
        self.provenance_manifest = manifest
        self.scanned = scanned
        self.last_modified = mtime


@pytest.fixture()
def client():
    return TestClient(md.app)


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setattr(md, "MANUAL_DELETE_ENABLED", True)
    monkeypatch.setattr(md, "MANUAL_DELETE_AUTH_MODE", "sso")
    monkeypatch.setattr(md, "MANUAL_DELETE_ADMINS", ["andrew"])
    monkeypatch.setattr(md, "MANUAL_DELETE_ADMIN_GROUPS", ["mlis-admins"])
    md._manual_delete_previews.clear()
    md._delete_key_cache.clear()

    calls = []

    async def fake_models(*a, **k):
        calls.append(k)
        return [_M("acme/Alpha", PATH_A), _M("acme/Beta", PATH_B, manifest=False)]

    monkeypatch.setattr(md, "get_models_calls", calls, raising=False)

    monkeypatch.setattr(md.downloaded_cache, "get_models", fake_models)

    async def fake_uris():
        return []

    monkeypatch.setattr(md.aioli_db, "fetch_packaged_uris", fake_uris)

    async def fake_jobs():
        return []

    monkeypatch.setattr(md.k8s_client, "list_managed_jobs_by_annotation", fake_jobs)
    yield


# ---------------------------------------------------------------------------
# the gate
# ---------------------------------------------------------------------------


def test_preview_reuses_the_cached_scan(client, monkeypatch):
    # Sharing the DownloadedModelsCache with /api/downloaded is the point: the
    # admin page must not force a scanner Job when the list is still fresh.
    seen = []

    async def fake_models(*a, **k):
        seen.append(k.get("force"))
        return [_M("acme/Alpha", PATH_A)]

    monkeypatch.setattr(md.downloaded_cache, "get_models", fake_models)
    client.post("/api/admin/deletions/preview", json={"targets": []}, headers=ADMIN)
    assert seen == [False]


def test_disabled_returns_400_and_no_page(client, monkeypatch):
    monkeypatch.setattr(md, "MANUAL_DELETE_ENABLED", False)
    assert client.post("/api/admin/deletions/preview", json={"targets": [PATH_A]}).status_code == 400
    assert client.get("/admin/delete").status_code == 404


# ---------------------------------------------------------------------------
# the form's default namespace comes from SSO
# ---------------------------------------------------------------------------


def _fake_cluster(monkeypatch, namespaces, pvc_in):
    async def fake_ns():
        return namespaces

    async def fake_pvc(namespace, pvc_name):
        if namespace in pvc_in:
            return 1_000_000
        raise RuntimeError("not found")

    monkeypatch.setattr(md.k8s_client, "list_namespaces", fake_ns)
    monkeypatch.setattr(md.k8s_client, "read_pvc_capacity", fake_pvc)
    md._namespace_cache.update({"at": 0.0, "names": frozenset()})
    md._pvc_ns_cache.clear()


def test_default_namespace_follows_the_sso_user(client, monkeypatch):
    _fake_cluster(
        monkeypatch,
        ["project-user-andrew-bydlon", "project-user-jane", "mlis", "default"],
        pvc_in={"project-user-andrew-bydlon"},
    )
    body = client.get("/", headers={"X-Auth-Request-Preferred-Username": "andrew-bydlon"}).text
    assert 'data-default-ns="project-user-andrew-bydlon"' in body


def test_user_namespace_without_the_pvc_is_not_defaulted(client, monkeypatch):
    # jane's namespace exists but does not serve models-pvc: defaulting there
    # would hand her a Job that waits forever on the claim.
    _fake_cluster(
        monkeypatch,
        ["project-user-andrew-bydlon", "project-user-jane"],
        pvc_in={"project-user-andrew-bydlon"},
    )
    body = client.get("/", headers={"X-Auth-Request-Preferred-Username": "jane"}).text
    assert f'data-default-ns="{md.DEFAULT_NAMESPACE}"' in body


def test_namespace_field_is_prefilled_not_just_defaulted(client, monkeypatch):
    # The default used to be applied only at submit time, so the field looked
    # empty and the operator could not see which namespace they would use.
    _fake_cluster(
        monkeypatch,
        ["project-user-andrew-bydlon", "project-user-jane"],
        pvc_in={"project-user-andrew-bydlon"},
    )
    body = client.get("/", headers={"X-Auth-Request-Preferred-Username": "andrew-bydlon"}).text
    # The submit form's field carries the resolved default as its VALUE, not
    # just data-default-ns (which only applied at submit time).
    assert 'id="ns-input"' in body
    assert 'value="project-user-andrew-bydlon"' in body
    # The debug form carries the same value, but only renders when debug pods
    # are available — which they are not in this test environment.
    assert body.count('value="project-user-andrew-bydlon"') >= 1


def test_unknown_user_falls_back_to_the_configured_default(client, monkeypatch):
    async def fake_ns():
        return ["project-user-andrew-bydlon", "mlis"]

    monkeypatch.setattr(md.k8s_client, "list_namespaces", fake_ns)
    md._namespace_cache.update({"at": 0.0, "names": frozenset()})
    md._pvc_ns_cache.clear()
    body = client.get("/", headers={"X-Auth-Request-Preferred-Username": "someone-else"}).text
    assert f'data-default-ns="{md.DEFAULT_NAMESPACE}"' in body


def test_no_identity_header_keeps_the_default(client, monkeypatch):
    async def fake_ns():
        return ["project-user-andrew-bydlon"]

    monkeypatch.setattr(md.k8s_client, "list_namespaces", fake_ns)
    md._namespace_cache.update({"at": 0.0, "names": frozenset()})
    md._pvc_ns_cache.clear()
    body = client.get("/").text
    assert f'data-default-ns="{md.DEFAULT_NAMESPACE}"' in body


def test_namespace_list_failure_falls_back(client, monkeypatch):
    async def boom():
        raise RuntimeError("rbac denied")

    monkeypatch.setattr(md.k8s_client, "list_namespaces", boom)
    md._namespace_cache.update({"at": 0.0, "names": frozenset()})
    md._pvc_ns_cache.clear()
    body = client.get("/", headers={"X-Auth-Request-Preferred-Username": "andrew-bydlon"}).text
    assert f'data-default-ns="{md.DEFAULT_NAMESPACE}"' in body


def test_page_renders_when_enabled(client):
    # Catches template/context breakage (missing asset key, undefined var) that
    # a route-exists check would miss.
    r = client.get("/admin/delete")
    assert r.status_code == 200, r.text
    body = r.text
    for element_id in ("md-load", "md-msg", "md-table", "md-delete", "md-modal"):
        assert 'id="' + element_id + '"' in body
    assert "admin_delete.js" in body


def test_sso_mode_renders_no_key_field(client):
    # The regression this guards: in SSO mode there IS no key, so a key field
    # (and a client-side requirement for one) made the page unusable.
    body = client.get("/admin/delete").text
    assert 'id="md-key"' not in body
    assert 'data-auth-mode="sso"' in body
    assert "no key" in body


def test_secret_mode_renders_the_key_field(client, monkeypatch):
    monkeypatch.setattr(md, "MANUAL_DELETE_AUTH_MODE", "secret")
    body = client.get("/admin/delete").text
    assert 'id="md-key"' in body
    assert 'data-auth-mode="secret"' in body


def test_page_has_cache_busted_script(client):
    # Assert the hash is a real 10-hex digest, not the "0" fallback a missing
    # asset would produce. (A prefix test is wrong: digests can start with 0.)
    import re

    body = client.get("/admin/delete").text
    match = re.search(r"admin_delete\.js\?v=([0-9a-f]+)", body)
    assert match is not None
    assert match.group(1) != "0"
    assert len(match.group(1)) == 10


def test_missing_identity_is_403_and_names_the_fix(client):
    r = client.post("/api/admin/deletions/preview", json={"targets": [PATH_A]})
    assert r.status_code == 403
    assert "manualDelete.auth.mode=secret" in r.json()["detail"]


def test_non_admin_is_403(client):
    r = client.post("/api/admin/deletions/preview", json={"targets": [PATH_A]}, headers=NOT_ADMIN)
    assert r.status_code == 403
    assert "admin required" in r.json()["detail"]


# ---------------------------------------------------------------------------
# the admin gate requires a VERIFIED token once an IdP is configured
# ---------------------------------------------------------------------------


def test_forged_header_is_refused_when_oidc_is_enabled(client, monkeypatch):
    # The regression this pins: any pod that can reach the Service could POST a
    # forged identity header and be treated as an admin. With an IdP configured,
    # a header alone must not authenticate.
    monkeypatch.setattr(md.oidc_identity, "oidc_enabled", lambda: True)
    r = client.post("/api/admin/deletions/preview", json={"targets": [PATH_A]}, headers=ADMIN)
    assert r.status_code == 403
    assert "verified" in r.json()["detail"].lower() or "token" in r.json()["detail"].lower()


def test_valid_token_is_admitted_when_oidc_is_enabled(client, monkeypatch):
    monkeypatch.setattr(md.oidc_identity, "oidc_enabled", lambda: True)
    monkeypatch.setattr(
        md.oidc_identity,
        "verify_and_decode",
        lambda token: {"preferred_username": "andrew"} if token == "good" else None,
    )
    headers = {"Authorization": "Bearer good", **ADMIN}
    r = client.post("/api/admin/deletions/preview", json={"targets": [PATH_A]}, headers=headers)
    assert r.status_code == 200, r.text


def test_bad_token_is_refused(client, monkeypatch):
    monkeypatch.setattr(md.oidc_identity, "oidc_enabled", lambda: True)
    monkeypatch.setattr(md.oidc_identity, "verify_and_decode", lambda token: None)
    headers = {"Authorization": "Bearer nope", **ADMIN}
    assert client.post("/api/admin/deletions/preview", json={"targets": [PATH_A]}, headers=headers).status_code == 403


def test_token_subject_must_still_be_an_admin(client, monkeypatch):
    # A valid token for a non-admin is still refused.
    monkeypatch.setattr(md.oidc_identity, "oidc_enabled", lambda: True)
    monkeypatch.setattr(md.oidc_identity, "verify_and_decode", lambda token: {"preferred_username": "bob"})
    headers = {"Authorization": "Bearer good", **NOT_ADMIN}
    r = client.post("/api/admin/deletions/preview", json={"targets": [PATH_A]}, headers=headers)
    assert r.status_code == 403


def test_header_fallback_still_works_without_an_idp(client, monkeypatch):
    # No IdP configured: behaviour is unchanged (and documented as weaker).
    monkeypatch.setattr(md.oidc_identity, "oidc_enabled", lambda: False)
    assert client.post("/api/admin/deletions/preview", json={"targets": [PATH_A]}, headers=ADMIN).status_code == 200


def test_admin_by_username_is_admitted(client):
    r = client.post("/api/admin/deletions/preview", json={"targets": [PATH_A]}, headers=ADMIN)
    assert r.status_code == 200, r.text


def test_admin_by_group_is_admitted(client):
    r = client.post("/api/admin/deletions/preview", json={"targets": [PATH_A]}, headers=GROUP_ADMIN)
    assert r.status_code == 200, r.text


def test_no_admins_configured_denies_everyone(client, monkeypatch):
    monkeypatch.setattr(md, "MANUAL_DELETE_ADMINS", [])
    monkeypatch.setattr(md, "MANUAL_DELETE_ADMIN_GROUPS", [])
    r = client.post("/api/admin/deletions/preview", json={"targets": [PATH_A]}, headers=ADMIN)
    assert r.status_code == 403
    assert "no admins configured" in r.json()["detail"]


def test_secret_mode_rejects_empty_and_wrong(client, monkeypatch):
    monkeypatch.setattr(md, "MANUAL_DELETE_AUTH_MODE", "secret")

    async def no_key():
        return ""

    monkeypatch.setattr(md, "_expected_delete_key", no_key)
    # no usable key configured => refuse, never "no gate"
    assert (
        client.post(
            "/api/admin/deletions/preview", json={"targets": [PATH_A]}, headers={"X-Delete-Key": "x"}
        ).status_code
        == 403
    )

    async def good_key():
        return "s3cret"

    monkeypatch.setattr(md, "_expected_delete_key", good_key)
    assert client.post("/api/admin/deletions/preview", json={"targets": [PATH_A]}).status_code == 401
    assert (
        client.post(
            "/api/admin/deletions/preview", json={"targets": [PATH_A]}, headers={"X-Delete-Key": ""}
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/api/admin/deletions/preview", json={"targets": [PATH_A]}, headers={"X-Delete-Key": "wrong"}
        ).status_code
        == 403
    )
    ok = client.post("/api/admin/deletions/preview", json={"targets": [PATH_A]}, headers={"X-Delete-Key": "s3cret"})
    assert ok.status_code == 200, ok.text


# ---------------------------------------------------------------------------
# the isolation rule
# ---------------------------------------------------------------------------


def test_target_not_in_scan_is_rejected(client):
    r = client.post(
        "/api/admin/deletions/preview", json={"targets": ["/mnt/large-models/models--evil--X"]}, headers=ADMIN
    )
    assert r.status_code == 422
    assert "not in the current scan" in r.json()["detail"]


def test_path_traversal_is_rejected(client):
    r = client.post("/api/admin/deletions/preview", json={"targets": ["/etc/passwd"]}, headers=ADMIN)
    assert r.status_code == 422


def test_too_many_targets(client, monkeypatch):
    monkeypatch.setattr(md, "MANUAL_DELETE_MAX_TARGETS", 1)
    r = client.post("/api/admin/deletions/preview", json={"targets": [PATH_A, PATH_B]}, headers=ADMIN)
    assert r.status_code == 422


def test_empty_targets_is_list_mode_and_issues_no_pin(client):
    # No selection = the page's initial load: every candidate, no verdict_id,
    # because a pin requires an explicit selection.
    r = client.post("/api/admin/deletions/preview", json={"targets": []}, headers=ADMIN)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["verdict_id"] == ""
    assert {row["cachepath"] for row in body["rows"]} == {PATH_A, PATH_B}


def test_list_mode_cannot_be_committed(client):
    v = client.post("/api/admin/deletions/preview", json={"targets": []}, headers=ADMIN).json()
    r = client.post("/api/admin/deletions", json={"verdict_id": v["verdict_id"], "confirm": "DELETE"}, headers=ADMIN)
    assert r.status_code == 409


# ---------------------------------------------------------------------------
# preview -> commit
# ---------------------------------------------------------------------------


def _preview(client, headers=ADMIN, targets=None):
    r = client.post("/api/admin/deletions/preview", json={"targets": targets or [PATH_A]}, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def test_preview_does_not_create_a_job(client, monkeypatch):
    called = []

    async def boom(*a, **k):
        called.append(1)
        raise AssertionError("must not create a job during preview")

    monkeypatch.setattr(md.k8s_client, "create_delete_job", boom)
    _preview(client)
    assert not called


def test_commit_requires_exact_confirmation(client):
    v = _preview(client)
    r = client.post("/api/admin/deletions", json={"verdict_id": v["verdict_id"], "confirm": "yes"}, headers=ADMIN)
    assert r.status_code == 422


def test_commit_unknown_verdict_id(client):
    r = client.post("/api/admin/deletions", json={"verdict_id": "nope", "confirm": "DELETE"}, headers=ADMIN)
    assert r.status_code == 409


def test_commit_deletes_exactly_the_pinned_set(client, monkeypatch):
    captured = {}

    async def fake_create(**kw):
        captured.update(kw)
        return "md-delete-abc123"

    monkeypatch.setattr(md.k8s_client, "create_delete_job", fake_create)
    v = _preview(client, targets=[PATH_A])
    r = client.post("/api/admin/deletions", json={"verdict_id": v["verdict_id"], "confirm": "DELETE"}, headers=ADMIN)
    assert r.status_code == 200, r.text
    assert r.json()["targets"] == [PATH_A]
    import base64
    import json as _json

    assert _json.loads(base64.b64decode(captured["targets_b64"])) == [
        {"cachepath": PATH_A, "override": False, "verdict": "deletable"}
    ]
    assert captured["actor"] == "andrew"
    # The Job must be created where models-pvc LIVES, not in the app namespace:
    # a PVC is namespace-scoped, and naming the app namespace left the pod
    # Pending forever with "persistentvolumeclaim not found".
    assert captured["namespace"] == md.DEFAULT_NAMESPACE


def test_verdict_id_is_bound_to_the_caller(client, monkeypatch):
    async def fake_create(**kw):
        return "md-delete-abc123"

    monkeypatch.setattr(md.k8s_client, "create_delete_job", fake_create)
    v = _preview(client, headers=ADMIN)
    r = client.post(
        "/api/admin/deletions",
        json={"verdict_id": v["verdict_id"], "confirm": "DELETE"},
        headers=GROUP_ADMIN,
    )
    assert r.status_code == 403


def test_commit_with_no_deletable_targets_is_409(client, monkeypatch):
    async def fake_jobs():
        return [{"namespace": "ns", "job_name": "md-live", "cache_root": "/mnt/large-models", "model_name": ""}]

    monkeypatch.setattr(md.k8s_client, "list_managed_jobs_by_annotation", fake_jobs)
    v = _preview(client, targets=[PATH_A])
    assert v["rows"][0]["verdict"] == "block"
    r = client.post("/api/admin/deletions", json={"verdict_id": v["verdict_id"], "confirm": "DELETE"}, headers=ADMIN)
    assert r.status_code == 409


# ---------------------------------------------------------------------------
# overrides
# ---------------------------------------------------------------------------


def test_soft_block_requires_override_and_is_refused_for_hard_block(client, monkeypatch):
    async def fake_uris():
        # parse_aioli_rows accepts (name, uri) tuples or {"uri": ...} dicts —
        # a bare string list is silently dropped, which would make this test
        # pass for the wrong reason.
        return [("Alpha", "pvc://models-pvc/large-models/acme/Alpha")]

    monkeypatch.setattr(md.aioli_db, "fetch_packaged_uris", fake_uris)

    captured = {}

    async def fake_create(**kw):
        captured.update(kw)
        return "md-delete-abc123"

    monkeypatch.setattr(md.k8s_client, "create_delete_job", fake_create)

    v = _preview(client, targets=[PATH_A])
    assert v["rows"][0]["verdict"] == "soft_block"

    # without the override, nothing is deletable
    r = client.post("/api/admin/deletions", json={"verdict_id": v["verdict_id"], "confirm": "DELETE"}, headers=ADMIN)
    assert r.status_code == 409

    # with it, the target is admitted and marked
    r = client.post(
        "/api/admin/deletions",
        json={"verdict_id": v["verdict_id"], "confirm": "DELETE", "overrides": [PATH_A]},
        headers=ADMIN,
    )
    assert r.status_code == 200, r.text
    import base64
    import json as _json

    assert _json.loads(base64.b64decode(captured["targets_b64"])) == [
        {"cachepath": PATH_A, "override": True, "verdict": "soft_block"}
    ]


def test_override_of_a_hard_block_is_422(client, monkeypatch):
    async def fake_jobs():
        return [{"namespace": "ns", "job_name": "md-live", "cache_root": "/mnt/large-models", "model_name": ""}]

    monkeypatch.setattr(md.k8s_client, "list_managed_jobs_by_annotation", fake_jobs)
    v = _preview(client, targets=[PATH_A])
    r = client.post(
        "/api/admin/deletions",
        json={"verdict_id": v["verdict_id"], "confirm": "DELETE", "overrides": [PATH_A]},
        headers=ADMIN,
    )
    assert r.status_code == 422
    assert "hard block" in r.json()["detail"]


def test_aioli_unavailable_blocks_preview(client, monkeypatch):
    async def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(md.aioli_db, "fetch_packaged_uris", boom)
    v = _preview(client, targets=[PATH_A])
    assert v["rows"][0]["verdict"] == "block"


def test_jobs_unavailable_fails_closed(client, monkeypatch):
    async def boom():
        raise RuntimeError("api down")

    monkeypatch.setattr(md.k8s_client, "list_managed_jobs_by_annotation", boom)
    v = _preview(client, targets=[PATH_A])
    assert v["rows"][0]["verdict"] == "block"
    assert any("live jobs" in b for b in v["rows"][0]["blocks"])
