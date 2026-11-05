"""Tests for MLIS packaged-model versioning (app.versioning + catalog normalisation).

The catalog authors a configuration; MLIS owns the version.  These tests pin
the rules that decide what a push lands as -- v1 for a new name, max+1 when the
name exists with a different configuration, skip when the configuration is
identical -- plus the seed-catalog policy that keeps descriptions short and
every stored version at 1.
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src" / "model_downloader"))

import app.catalog as catalog_module  # noqa: E402
from app.catalog import Catalog  # noqa: E402
from app.versioning import (  # noqa: E402
    FINGERPRINT_COLUMNS,
    config_fingerprint,
    normalize_project,
    plan_push,
    resolve_project,
)

SEED = json.loads((REPO / "src" / "model_downloader" / "app" / "seed_catalog.json").read_text())


def _cfg(**overrides):
    base = {
        "name": "Qwen3.8-27B-vllm-FP8",
        "version": 1,
        "uri": "pvc://models-pvc/large-models/Qwen/Qwen3.8-27B-FP8?containerPath=/mnt/models",
        "image": "vllm/vllm-openai:latest",
        "arguments": ["Qwen/Qwen3.8-27B-FP8", "--port", "8080"],
        "environment": {"OMP_NUM_THREADS": "8"},
        "resource_request_cpu": "8",
        "resource_request_memory": "64Gi",
        "resource_request_gpu": "1",
        "resource_limit_cpu": "16",
        "resource_limit_memory": "128Gi",
        "resource_limit_gpu": "1",
        "resource_gpu_type": "H200",
        "model_format": "custom",
        "caching_enabled": False,
        "project": "",
        "description": "A model.",
        "metadata": {"tags": "", "modelCategory": "llm"},
    }
    base.update(overrides)
    return base


# ---- fingerprint ----


def test_fingerprint_is_stable_across_key_order():
    a = _cfg()
    b = dict(reversed(list(_cfg().items())))
    assert config_fingerprint(a) == config_fingerprint(b)


def test_fingerprint_ignores_cosmetic_and_assigned_fields():
    """A description typo fix or an MLIS version must not mint a new version."""
    base = config_fingerprint(_cfg())
    assert config_fingerprint(_cfg(description="A different description.")) == base
    assert config_fingerprint(_cfg(version=9)) == base
    assert config_fingerprint(_cfg(metadata={"tags": "x", "modelCategory": "llm"})) == base
    assert config_fingerprint(_cfg(name="some-other-name")) == base
    assert config_fingerprint(_cfg(tier="l40s")) == base


@pytest.mark.parametrize(
    "field,value",
    [
        ("image", "vllm/vllm-openai:v0.9.0"),
        ("uri", "pvc://models-pvc/large-models/Qwen/Qwen3.8-27B-FP8-NVFP4"),
        ("arguments", ["Qwen/Qwen3.8-27B-FP8", "--port", "8081"]),
        ("environment", {"OMP_NUM_THREADS": "16"}),
        ("resource_limit_gpu", "2"),
        ("resource_gpu_type", "RTX Pro 6000"),
        ("model_format", "vllm"),
        ("caching_enabled", True),
        ("project", "andrew-bydlon"),
        ("registry", "ghcr.io/example"),
    ],
)
def test_fingerprint_tracks_every_deployment_defining_field(field, value):
    assert config_fingerprint(_cfg(**{field: value})) != config_fingerprint(_cfg())


def test_fingerprint_matches_catalog_payload_against_db_row():
    """The payload and the psycopg2 row carry the same data in different shapes.

    jsonb arrives as a dict, text[] as a list, and a resource may be an integer
    column value while the catalog stores the string "4"; the fingerprint must
    not care, or every re-push would look like a different configuration.
    """
    payload = _cfg(resource_limit_gpu=4, arguments=("a", "b"), caching_enabled=True)
    row = _cfg(resource_limit_gpu="4", arguments=["a", "b"], caching_enabled=True)
    assert config_fingerprint(payload) == config_fingerprint(row)
    row_from_db = {col: row.get(col) for col in ("version", *FINGERPRINT_COLUMNS)}
    assert config_fingerprint(row_from_db) == config_fingerprint(payload)


def test_fingerprint_treats_missing_arguments_as_an_empty_list():
    """A raw /api/push payload that omits arguments inserts SQL NULL; the
    catalog always sends [].  Both mean "no arguments" and must not mint a
    version the first time the two representations meet."""
    assert config_fingerprint(_cfg(arguments=None)) == config_fingerprint(_cfg(arguments=[]))
    assert config_fingerprint(_cfg(environment=None)) == config_fingerprint(_cfg(environment={}))


def test_fingerprint_is_short_and_hex():
    fp = config_fingerprint(_cfg())
    assert len(fp) == 16
    assert all(c in "0123456789abcdef" for c in fp)


# ---- plan_push: the version sequence ----


def test_new_name_lands_as_v1():
    plan = plan_push([], _cfg())
    assert plan["version"] == 1
    assert plan["status"] == "pushed"
    assert plan["bumped"] is False


def test_identical_config_is_skipped_not_versioned_up():
    plan = plan_push([_cfg(version=1)], _cfg())
    assert plan["status"] == "skipped"
    assert plan["version"] == 1
    assert "already exists in MLIS as v1" in plan["detail"]
    assert plan["bumped"] is False


def test_identical_config_is_skipped_at_whatever_version_mlis_has():
    plan = plan_push([_cfg(version=1), _cfg(version=2), _cfg(version=3)], _cfg())
    assert plan["status"] == "skipped"
    assert plan["version"] == 1


def test_different_config_of_a_known_name_gets_the_next_version():
    plan = plan_push([_cfg(version=1)], _cfg(image="vllm/vllm-openai:v0.9.0"))
    assert plan["status"] == "pushed"
    assert plan["version"] == 2
    assert plan["bumped"] is True
    assert "v1 already exists" in plan["detail"]


def test_next_version_is_max_plus_one_not_count_plus_one():
    existing = [_cfg(version=1), _cfg(version=4, image="a"), _cfg(version=7, image="b")]
    plan = plan_push(existing, _cfg(image="c"))
    assert plan["version"] == 8


def test_authored_version_in_the_payload_is_ignored():
    """A catalog entry pulled from a local MLIS carries v9; it must still land as v1."""
    plan = plan_push([], _cfg(version=9))
    assert plan["version"] == 1
    plan = plan_push([_cfg(version=1, image="other")], _cfg(version=9))
    assert plan["version"] == 2


def test_two_configs_of_one_name_get_distinct_versions():
    """vLLM and SGLang on the same GPU class are two rows, not one duplicate."""
    first = _cfg(image="vllm/vllm-openai:latest")
    second = _cfg(image="lmsysorg/sglang:latest")
    plan1 = plan_push([], first)
    plan2 = plan_push([dict(first, version=plan1["version"])], second)
    assert (plan1["version"], plan2["version"]) == (1, 2)


# ---- catalog normalisation ----


@pytest.fixture
def no_seed(monkeypatch, tmp_path):
    """Keep Catalog() off the bundled seed file so entries are exactly ours."""
    monkeypatch.setattr(catalog_module, "SEED_FILE", tmp_path / "missing-seed.json")
    return tmp_path


def test_catalog_add_forces_version_one(no_seed):
    cat = Catalog(str(no_seed / "catalog.json"))
    entry = cat.add({"name": "m", "image": "img:1", "version": 9})
    assert entry["version"] == 1
    assert json.loads((no_seed / "catalog.json").read_text())[0]["version"] == 1


def test_catalog_load_normalises_an_inherited_version(no_seed):
    """A deployed catalog.json already carrying v9/v2 must be repaired on start."""
    path = no_seed / "catalog.json"
    path.write_text(
        json.dumps(
            [
                {"catalog_id": "a", "name": "deepseek", "image": "i", "version": 9},
                {"catalog_id": "b", "name": "glm", "image": "i", "version": 2},
                {"catalog_id": "c", "name": "qwen", "image": "i", "version": 1},
            ]
        )
    )
    cat = Catalog(str(path))
    assert [e["version"] for e in cat.entries] == [1, 1, 1]
    assert [e["version"] for e in json.loads(path.read_text())] == [1, 1, 1]


def test_catalog_merge_normalises_an_inherited_version(no_seed):
    cat = Catalog(str(no_seed / "catalog.json"))
    merged = cat.merge_entries([{"catalog_id": "x", "name": "m", "image": "i", "version": 8}], source="github")
    assert merged["added"] == 1
    assert cat.all()[0]["version"] == 1


# ---- the real DB path: AioliDB._push_one against a fake cursor ----
#
# plan_push() is the rule; _push_one() is where it meets SQL.  These tests run
# the actual method (no psycopg2 needed - the driver is stubbed when absent) and
# assert what reaches the INSERT, so a broken SELECT column list or a version
# that never reaches the row would fail here.


def _import_db():
    """Import app.db, stubbing the psycopg2 driver when it is not installed."""
    if "psycopg2" not in sys.modules:
        try:
            import psycopg2  # noqa: F401
        except ModuleNotFoundError:
            pg = types.ModuleType("psycopg2")
            extras = types.ModuleType("psycopg2.extras")

            class Json:
                def __init__(self, value):
                    self.value = value

            extras.Json = Json
            pg.extras = extras

            def _no_database(**kwargs):
                raise RuntimeError("tests must not open a real database connection")

            pg.connect = _no_database
            sys.modules["psycopg2"] = pg
            sys.modules["psycopg2.extras"] = extras
    import app.db

    return app.db


class FakeCursor:
    """Minimal cursor: serves the existing-rows SELECT, records the INSERT."""

    def __init__(self, existing):
        self.existing = existing  # name -> [row dict with EXISTING_COLUMNS]
        self.inserts = []
        self.queries = []
        self.select_columns = []
        self._rows = []

    def execute(self, sql, params=None):
        self.queries.append(sql)
        if sql.lstrip().startswith("SELECT"):
            # Serve the row in the order the SQL actually asked for, so a drift
            # between the SELECT string and EXISTING_COLUMNS cannot pass here.
            cols = [c.strip() for c in sql.split("SELECT", 1)[1].split("FROM", 1)[0].split(",")]
            self.select_columns = cols
            rows = self.existing.get(params[0], [])
            self._rows = [tuple(row.get(col) for col in cols) for row in rows]
        elif sql.lstrip().startswith("INSERT"):
            self.inserts.append(list(params))
            self._rows = [(4242,)]

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeConn:
    """Connection stub: enough for _push_sync's 'with conn, conn.cursor()'."""

    def __init__(self, cursor):
        self._cursor = cursor
        self.closed = False

    def cursor(self):
        return self._cursor

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def close(self):
        self.closed = True


def _db_row(**overrides):
    """A packaged_models row as psycopg2 would hand it back."""
    db = _import_db()
    row = _cfg(**overrides)
    return {col: row.get(col) for col in db.EXISTING_COLUMNS}


def test_push_one_inserts_v1_for_a_new_name():
    db = _import_db()
    cur = FakeCursor({})
    res = db.AioliDB._push_one(cur, _cfg())
    assert (res["status"], res["version"], res["bumped"]) == ("pushed", 1, False)
    assert len(cur.inserts) == 1
    # COLUMNS starts with name, version: the assigned version must reach the row.
    assert cur.inserts[0][0] == "Qwen3.8-27B-vllm-FP8"
    assert cur.inserts[0][1] == 1
    assert "id=4242" in res["detail"]


def test_push_one_ignores_the_authored_version_and_inserts_v1():
    db = _import_db()
    cur = FakeCursor({})
    res = db.AioliDB._push_one(cur, _cfg(version=9))
    assert res["version"] == 1
    assert cur.inserts[0][1] == 1


def test_push_one_skips_an_identical_row_without_inserting():
    db = _import_db()
    cur = FakeCursor({"Qwen3.8-27B-vllm-FP8": [_db_row(version=1)]})
    res = db.AioliDB._push_one(cur, _cfg())
    assert (res["status"], res["version"]) == ("skipped", 1)
    assert cur.inserts == []
    assert "already exists in MLIS as v1" in res["detail"]


def test_push_one_versions_up_a_changed_config_and_says_so():
    db = _import_db()
    cur = FakeCursor({"Qwen3.8-27B-vllm-FP8": [_db_row(version=1)]})
    res = db.AioliDB._push_one(cur, _cfg(image="vllm/vllm-openai:v0.9.0"))
    assert (res["status"], res["version"], res["bumped"]) == ("pushed", 2, True)
    assert cur.inserts[0][1] == 2
    assert "v1 already exists" in res["detail"]


def test_push_one_selects_every_existing_version_for_the_name():
    db = _import_db()
    cur = FakeCursor(
        {
            "Qwen3.8-27B-vllm-FP8": [
                _db_row(version=1),
                _db_row(version=3, image="a"),
                _db_row(version=5, image="b"),
            ]
        }
    )
    res = db.AioliDB._push_one(cur, _cfg(image="c"))
    assert res["version"] == 6
    assert "ORDER BY version" in cur.queries[0]
    assert "WHERE name = %s" in cur.queries[0]


def _db_service(default_project=""):
    db = _import_db()
    return db.AioliDB(
        k8s=None,
        host="",
        port=0,
        dbname="",
        user="",
        secret_name="",
        secret_ns="",
        secret_key="",
        default_project=default_project,
    )


def test_default_project_is_applied_before_the_fingerprint():
    """catalog.defaultProject must reach the row AND the fingerprint.

    Filling it in at INSERT time only would store a value the fingerprint
    never sees, so every re-push would look like a new configuration.
    """
    svc = _db_service("project-user-x")
    effective = svc._with_default_project(_cfg())
    assert effective["project"] == "project-user-x"
    cur = FakeCursor({"Qwen3.8-27B-vllm-FP8": [_db_row(version=1, project="project-user-x")]})
    res = svc._push_one(cur, effective)
    assert res["status"] == "skipped"
    assert cur.inserts == []


def test_default_project_is_what_reaches_the_insert():
    db = _import_db()
    svc = _db_service("project-user-x")
    cur = FakeCursor({})
    svc._push_one(cur, svc._with_default_project(_cfg()))
    assert cur.inserts[0][db.COLUMNS.index("project")] == "project-user-x"


def test_default_project_does_not_override_an_explicit_project():
    svc = _db_service("project-user-x")
    assert svc._with_default_project(_cfg(project="project-user-y"))["project"] == "project-user-y"


def test_empty_default_project_leaves_the_config_alone():
    svc = _db_service("")
    cfg = _cfg()
    assert svc._with_default_project(cfg) is cfg
    assert svc._with_default_project(cfg)["project"] == ""


def test_existing_rows_selects_exactly_the_fingerprint_columns():
    db = _import_db()
    cur = FakeCursor({})
    db.AioliDB._existing_rows(cur, "m")
    assert cur.select_columns == list(db.EXISTING_COLUMNS)


def test_push_sync_applies_the_default_project_end_to_end(monkeypatch):
    """The default-project wiring lives in _push_sync (db.py), not in _push_one."""
    db = _import_db()
    cur = FakeCursor({})
    conn = FakeConn(cur)
    monkeypatch.setattr(db.psycopg2, "connect", lambda **kwargs: conn)
    svc = _db_service("project-user-x")

    results = svc._push_sync([_cfg()], "password")

    assert results[0]["status"] == "pushed"
    assert cur.inserts[0][db.COLUMNS.index("project")] == "project-user-x"
    assert conn.closed is True


# ---- the project field ----


@pytest.mark.parametrize("value", ["", "   ", "project-user-andrew-bydlon", "default", "a1-b2"])
def test_normalize_project_accepts_namespace_names(value):
    assert normalize_project(value) == value.strip()


@pytest.mark.parametrize(
    "value",
    ["Project-User-X", "-leading", "trailing-", "has space", "under_score", "project-user-x!", "a" * 64],
)
def test_normalize_project_rejects_non_namespace_values(value):
    with pytest.raises(ValueError):
        normalize_project(value)


def test_resolve_project_precedence():
    """Explicit entry > project picked for this push > chart default."""
    assert resolve_project({"project": "entry"}, "push", "default") == "entry"
    assert resolve_project({}, "push", "default") == "push"
    assert resolve_project({}, "", "default") == "default"
    assert resolve_project({}, "", "") == ""
    assert resolve_project({"project": "   "}, "", "default") == "default"


def test_push_one_inserts_the_project_picked_for_the_push():
    db = _import_db()
    svc = _db_service("")
    cur = FakeCursor({})
    svc._push_one(cur, svc._with_default_project(_cfg(), "project-user-x"))
    assert cur.inserts[0][db.COLUMNS.index("project")] == "project-user-x"


def test_per_push_project_wins_over_the_chart_default():
    svc = _db_service("project-user-default")
    assert svc._with_default_project(_cfg(), "project-user-picked")["project"] == "project-user-picked"


def test_entry_project_wins_over_the_per_push_project():
    svc = _db_service("project-user-default")
    effective = svc._with_default_project(_cfg(project="project-user-entry"), "project-user-picked")
    assert effective["project"] == "project-user-entry"


def test_push_sync_threads_the_per_push_project(monkeypatch):
    db = _import_db()
    cur = FakeCursor({})
    conn = FakeConn(cur)
    monkeypatch.setattr(db.psycopg2, "connect", lambda **kwargs: conn)
    svc = _db_service("")

    results = svc._push_sync([_cfg()], "password", "project-user-x")

    assert results[0]["status"] == "pushed"
    assert cur.inserts[0][db.COLUMNS.index("project")] == "project-user-x"


def test_push_one_rejects_a_config_without_a_name_or_image():
    db = _import_db()
    cur = FakeCursor({})
    res = db.AioliDB._push_one(cur, {"name": "", "image": "x"})
    assert res["status"] == "error"
    assert cur.inserts == []
    assert cur.queries == []


# ---- seed catalog policy ----


def test_every_seed_entry_stores_version_one():
    assert {e.get("version") for e in SEED} == {1}


def test_every_seed_description_is_concise():
    """MLIS shows this text in a side panel; multi-KB agent reasoning does not belong there."""
    too_long = [
        (e["catalog_id"], len(e.get("description") or "")) for e in SEED if len(e.get("description") or "") > 400
    ]
    assert not too_long, f"descriptions over 400 chars (move the detail to notes): {too_long}"


def test_long_form_notes_stay_longer_than_the_description():
    for entry in SEED:
        notes = entry.get("notes")
        if notes is None:
            continue
        assert notes.strip(), entry["catalog_id"] + ": notes is empty"
        assert len(notes) > len(entry.get("description") or ""), (
            entry["catalog_id"] + ": notes is not the long-form text"
        )
