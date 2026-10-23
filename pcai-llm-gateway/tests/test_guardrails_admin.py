"""Guardrails admin-plane tests — validation, atomic attachment replace, mocked DB.

Mirrors tests/test_admin.py's FakeDB approach (monkeypatched onto
routes_admin.db, SQL matched on substrings, TestClient with the admin key
patched). The fake is honest about what it matches: every branch names the
SQL fragment it answers, and the write-path assertions look at the exact
statements the handlers issued (DELETE/INSERT ordering for the attachment
replace, nothing-at-all after a refused request).
"""

from __future__ import annotations

import re
import uuid
from typing import Any

import asyncpg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pcai_llm.api import routes_admin
from pcai_llm.config import settings


class FakeConn:
    """Stands in for an acquired asyncpg connection (transaction + all)."""

    def __init__(self, db: FakeDB) -> None:
        self._db = db

    def transaction(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc: object):
        return False

    async def fetchrow(self, sql: str, *args: Any):
        return await self._db.fetchrow(sql, *args)

    async def fetch(self, sql: str, *args: Any):
        return await self._db.fetch(sql, *args)

    async def execute(self, sql: str, *args: Any):
        self._db.executed.append((sql, args))
        return "DELETE 0" if "DELETE" in sql else "INSERT 0 1"

    async def executemany(self, sql: str, args: Any):
        self._db.executed.append((sql, list(args)))


class FakeDB:
    """Minimal asyncpg stand-in dispatching on SQL substrings (house style).

    `guardrail` is the single source of truth for one staged row: id lookups
    (`_exists` via fetchval), the PATCH read, and the UPDATE apply all see it.
    """

    def __init__(self) -> None:
        self.executed: list[tuple] = []
        self.known_models: list[str] = ["llama-8b", "qwen-32b"]
        self.guardrail: dict[str, Any] | None = None
        self.guardrails: list[dict[str, Any]] = []
        self.attachments: list[dict[str, Any]] = []
        self.insert_error: Exception | None = None  # staged INSERT failure (409 test)

    def acquire(self):
        return FakeConn(self)

    async def fetchrow(self, sql: str, *args: Any):
        if "INSERT INTO guardrails" in sql:
            self.executed.append((sql, args))
            if self.insert_error is not None:
                raise self.insert_error
            return {
                "id": uuid.uuid4(),
                "name": args[0],
                "mode": args[1],
                "engine": args[2],
                "config_json": args[3],
                "enabled": args[4],
                "created_at": "2025-06-01T00:00:00+00:00",
            }
        if "UPDATE guardrails" in sql:
            self.executed.append((sql, args))
            # apply the SET clause positionally — the honest way to return
            # what RETURNING would (values live in args, columns in the SQL)
            row = dict(self.guardrail or {})
            for col, ph in re.findall(r"(\w+) = \$(\d+)", sql.split("WHERE")[0]):
                row[col] = args[int(ph) - 1]
            self.guardrail = row
            return row
        if "FROM guardrails" in sql:  # the staged row decides (None = no such row)
            return self.guardrail
        return {"id": uuid.uuid4()}

    async def fetch(self, sql: str, *args: Any):
        # model_guardrails FIRST — "FROM guardrails" is a substring of it
        if "FROM model_guardrails" in sql:
            if args:  # per-guardrail query (WHERE guardrail_id = $1)
                return [a for a in self.attachments if a["guardrail_id"] == args[0]]
            return self.attachments
        if "DISTINCT model_name" in sql:
            return [{"model_name": m} for m in self.known_models]
        if "FROM guardrails" in sql:
            return self.guardrails
        return []

    async def fetchval(self, sql: str, *args: Any):
        if "FROM guardrails" in sql:  # _exists(): the staged row decides
            return 1 if self.guardrail is not None else None
        return 1

    async def execute(self, sql: str, *args: Any):
        self.executed.append((sql, args))
        return "OK"


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "admin_api_keys", "admin-secret-123")
    fake = FakeDB()
    monkeypatch.setattr(routes_admin, "db", fake)
    app = FastAPI()
    app.include_router(routes_admin.router)
    return TestClient(app, raise_server_exceptions=False), fake


def _h() -> dict[str, str]:
    return {"Authorization": "Bearer admin-secret-123"}


def _valid_body(**over: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "name": "pii-scrub",
        "mode": "pre_call",
        "engine": "regex_pii",
        "config": {"entities": ["EMAIL"]},
    }
    body.update(over)
    return body


def _stage_guardrail(fake: FakeDB, **over: Any) -> dict[str, Any]:
    """Stage one existing guardrail row (id lookups + PATCH read + _exists)."""
    row: dict[str, Any] = {
        "id": uuid.uuid4(),
        "name": "pii-scrub",
        "mode": "pre_call",
        "engine": "blocklist",
        "config_json": '{"keywords": ["secret"], "action": "mask"}',
        "enabled": True,
        "created_at": "2025-06-01T00:00:00+00:00",
    }
    row.update(over)
    fake.guardrail = dict(row)
    return row


# ---------------------------------------------------------------------------
# auth + discovery
# ---------------------------------------------------------------------------


def test_guardrails_require_admin(client) -> None:
    tc, _ = client
    assert tc.get("/admin/guardrails").status_code == 401
    assert tc.get("/admin/guardrails/models").status_code == 401
    assert tc.post("/admin/guardrails", json=_valid_body()).status_code == 401


def test_help_lists_guardrail_routes(client) -> None:
    tc, _ = client
    endpoints = tc.get("/admin/help", headers=_h()).json()["endpoints"]
    rows = [e for e in endpoints if "/admin/guardrails" in e]
    assert len(rows) == 6  # list/create/picker/put-models/patch/delete inventoried


# ---------------------------------------------------------------------------
# POST /guardrails
# ---------------------------------------------------------------------------


def test_create_guardrail_201(client) -> None:
    tc, _ = client
    r = tc.post(
        "/admin/guardrails",
        json=_valid_body(engine="regex_pii", config={"entities": ["EMAIL", "PHONE"]}),
        headers=_h(),
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["name"] == "pii-scrub"
    assert body["mode"] == "pre_call"
    assert body["config"] == {"entities": ["EMAIL", "PHONE"]}  # parsed dict…
    assert "config_json" not in body  # …never the raw TEXT column
    assert body["models"] == []  # fresh guardrail: nothing attached yet


@pytest.mark.parametrize(
    ("payload", "fragment"),
    [
        (_valid_body(mode="mid_call"), "mode must be one of"),
        (_valid_body(engine="regex"), "engine must be one of"),
        (_valid_body(engine="blocklist", config={}), "'keywords'"),
        (_valid_body(engine="blocklist", config={"keywords": "secret"}), "'keywords'"),
        (_valid_body(engine="blocklist", config={"keywords": [1]}), "'keywords'"),
        (_valid_body(engine="blocklist", config={"keywords": [], "action": "delete"}), "action"),
        (_valid_body(engine="regex_pii", config={"entities": ["SSN"]}), "unknown PII entity"),
        (_valid_body(engine="regex_pii", config={"entities": "EMAIL"}), "'entities'"),
        (_valid_body(engine="length", config={}), "max_chars"),
        (_valid_body(engine="length", config={"max_chars": 0}), "max_chars"),
        (_valid_body(engine="length", config={"max_chars": -5}), "max_chars"),
        (_valid_body(engine="length", config={"max_chars": "400"}), "max_chars"),
        (_valid_body(engine="length", config={"max_chars": True}), "max_chars"),
        (_valid_body(engine="webhook", config={}), "url"),
        (_valid_body(engine="webhook", config={"url": "ftp://hook"}), "http:// or https://"),
        (_valid_body(engine="webhook", config={"url": "https://x", "timeout_s": 0}), "timeout_s"),
        (_valid_body(engine="webhook", config={"url": "https://x", "api_key_env": 5}), "api_key_env"),
        (_valid_body(name=""), "name"),
        (_valid_body(name="   "), "name"),
        (_valid_body(name="x" * 201), "200"),
        (_valid_body(config=["not", "an", "object"]), "config must be a JSON object"),
        (_valid_body(config={"keywords": ["x"], "reply_text": 5}), "reply_text"),
        (_valid_body(config={"keywords": ["x"], "reply_text": ""}), "reply_text"),
        (_valid_body(config={"keywords": ["x"], "reply_text": "   "}), "reply_text"),
    ],
)
def test_create_guardrail_validation_400(client, payload: dict[str, Any], fragment: str) -> None:
    tc, fake = client
    r = tc.post("/admin/guardrails", json=payload, headers=_h())
    assert r.status_code == 400, r.text
    assert fragment in r.json()["detail"]
    assert not any("INSERT INTO guardrails" in sql for sql, _ in fake.executed)


def test_create_guardrail_duplicate_name_409(client) -> None:
    tc, fake = client
    fake.insert_error = asyncpg.UniqueViolationError(
        'duplicate key value violates unique constraint "guardrails_name_key"'
    )
    r = tc.post("/admin/guardrails", json=_valid_body(), headers=_h())
    assert r.status_code == 409, r.text
    assert "already exists" in r.json()["detail"]


# ---------------------------------------------------------------------------
# PATCH /guardrails/{id}
# ---------------------------------------------------------------------------


def test_patch_config_only_validated_against_row_engine(client) -> None:
    """The combined-triple rule: config arrives ALONE, so it is validated
    against the row's CURRENT engine — a length config on a blocklist row
    must 400 (not strand a guardrail the engine misreads at request time)."""
    tc, fake = client
    row = _stage_guardrail(fake)  # engine=blocklist
    r = tc.patch(f"/admin/guardrails/{row['id']}", json={"config": {"max_chars": 10}}, headers=_h())
    assert r.status_code == 400, r.text
    assert "'keywords'" in r.json()["detail"]
    assert not any("UPDATE guardrails" in sql for sql, _ in fake.executed)
    # and the row's own engine accepts a proper blocklist config
    r2 = tc.patch(f"/admin/guardrails/{row['id']}", json={"config": {"keywords": []}}, headers=_h())
    assert r2.status_code == 200, r2.text
    assert r2.json()["config"] == {"keywords": []}
    assert r2.json()["engine"] == "blocklist"  # engine untouched by a config-only PATCH


def test_patch_engine_and_config_together(client) -> None:
    tc, fake = client
    row = _stage_guardrail(fake)
    fake.attachments = [{"guardrail_id": row["id"], "model_name": "qwen-32b"}]
    r = tc.patch(
        f"/admin/guardrails/{row['id']}",
        json={"engine": "length", "config": {"max_chars": 500}},
        headers=_h(),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["engine"] == "length"
    assert body["config"] == {"max_chars": 500}
    assert body["mode"] == "pre_call"  # untouched
    assert body["models"] == ["qwen-32b"]  # attachments unaffected by the edit
    assert any("UPDATE guardrails" in sql for sql, _ in fake.executed)


def test_patch_enabled_only_explicit_false(client) -> None:
    tc, fake = client
    row = _stage_guardrail(fake)
    r = tc.patch(f"/admin/guardrails/{row['id']}", json={"enabled": False}, headers=_h())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["enabled"] is False
    assert body["engine"] == "blocklist"  # untouched
    assert body["config"] == {"keywords": ["secret"], "action": "mask"}  # untouched, still parsed


def test_patch_no_fields_400(client) -> None:
    tc, fake = client
    row = _stage_guardrail(fake)
    r = tc.patch(f"/admin/guardrails/{row['id']}", json={}, headers=_h())
    assert r.status_code == 400
    assert "no editable fields" in r.json()["detail"]


def test_patch_unknown_guardrail_404(client) -> None:
    tc, _ = client
    r = tc.patch(f"/admin/guardrails/{uuid.uuid4()}", json={"enabled": True}, headers=_h())
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# DELETE /guardrails/{id}
# ---------------------------------------------------------------------------


def test_delete_guardrail_204(client) -> None:
    tc, fake = client
    row = _stage_guardrail(fake)
    r = tc.delete(f"/admin/guardrails/{row['id']}", headers=_h())
    assert r.status_code == 204
    assert r.content == b""
    assert any("DELETE FROM guardrails" in sql for sql, _ in fake.executed)


def test_delete_unknown_guardrail_404(client) -> None:
    tc, _ = client
    r = tc.delete(f"/admin/guardrails/{uuid.uuid4()}", headers=_h())
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# PUT /guardrails/{id}/models — atomic attachment replace
# ---------------------------------------------------------------------------


def test_put_models_unknown_name_400_writes_nothing(client) -> None:
    tc, fake = client
    row = _stage_guardrail(fake)
    fake.known_models = ["llama-8b"]
    r = tc.put(
        f"/admin/guardrails/{row['id']}/models",
        json={"model_names": ["llama-8b", "ghost-model"]},
        headers=_h(),
    )
    assert r.status_code == 400, r.text
    assert "ghost-model" in r.json()["detail"]
    assert fake.executed == []  # refused BEFORE the transaction: no DELETE, no INSERT


def test_put_models_replace_semantics(client) -> None:
    tc, fake = client
    row = _stage_guardrail(fake)
    fake.attachments = [{"guardrail_id": row["id"], "model_name": "old-model"}]
    r = tc.put(
        f"/admin/guardrails/{row['id']}/models",
        json={"model_names": ["qwen-32b", "llama-8b", "qwen-32b"]},
        headers=_h(),
    )
    assert r.status_code == 200, r.text
    assert r.json()["models"] == ["llama-8b", "qwen-32b"]  # sorted, duplicates collapsed
    sqls = [sql for sql, _ in fake.executed]
    assert any("DELETE FROM model_guardrails" in s for s in sqls)
    inserts = [args for sql, args in fake.executed if "INSERT INTO model_guardrails" in sql]
    assert len(inserts) == 1  # one executemany batch…
    assert len(inserts[0]) == 2  # …carrying both rows
    assert all(t[1] == row["id"] for t in inserts[0])  # every row targets this guardrail


def test_put_models_empty_list_detaches_all(client) -> None:
    tc, fake = client
    row = _stage_guardrail(fake)
    fake.attachments = [{"guardrail_id": row["id"], "model_name": "llama-8b"}]
    r = tc.put(f"/admin/guardrails/{row['id']}/models", json={"model_names": []}, headers=_h())
    assert r.status_code == 200, r.text
    assert r.json()["models"] == []
    sqls = [sql for sql, _ in fake.executed]
    assert any("DELETE FROM model_guardrails" in s for s in sqls)
    assert not any("INSERT INTO model_guardrails" in s for s in sqls)


def test_put_models_unknown_guardrail_404(client) -> None:
    tc, _ = client
    r = tc.put(
        f"/admin/guardrails/{uuid.uuid4()}/models",
        json={"model_names": ["llama-8b"]},
        headers=_h(),
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# GET /guardrails + GET /guardrails/models
# ---------------------------------------------------------------------------


def test_list_guardrails_aggregates_attachments(client) -> None:
    tc, fake = client
    g1, g2 = uuid.uuid4(), uuid.uuid4()
    fake.guardrails = [
        {
            "id": g1,
            "name": "a-scrub",
            "mode": "pre_call",
            "engine": "blocklist",
            "config_json": '{"keywords": ["x"]}',
            "enabled": True,
            "created_at": "t1",
        },
        {
            "id": g2,
            "name": "b-len",
            "mode": "post_call",
            "engine": "length",
            "config_json": '{"max_chars": 10}',
            "enabled": False,
            "created_at": "t2",
        },
    ]
    fake.attachments = [
        {"guardrail_id": g2, "model_name": "alpha"},
        {"guardrail_id": g1, "model_name": "zeta"},
        {"guardrail_id": g1, "model_name": "alpha"},
    ]
    r = tc.get("/admin/guardrails", headers=_h())
    assert r.status_code == 200, r.text
    items = r.json()
    assert [i["name"] for i in items] == ["a-scrub", "b-len"]  # name-ordered
    assert items[0]["models"] == ["alpha", "zeta"]  # per-row, sorted
    assert items[1]["models"] == ["alpha"]
    assert items[0]["config"] == {"keywords": ["x"]}  # parsed dict, not the TEXT column
    assert items[1]["enabled"] is False
    assert all("config_json" not in i for i in items)


def test_list_guardrail_models_picker(client) -> None:
    tc, fake = client
    fake.known_models = ["a-model", "b-model"]  # staged as the DB's ORDER BY returns them
    r = tc.get("/admin/guardrails/models", headers=_h())
    assert r.status_code == 200
    assert r.json() == {"models": ["a-model", "b-model"]}
