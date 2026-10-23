"""Key-scoped saved conversations: data layer + /v1/conversations routes.

Fake-db style per repo convention — but here the fake is a small IN-MEMORY
ENGINE implementing the exact SQL shapes core/conversations.py issues
(WHERE key_id scoping, COUNT subselect, FOR UPDATE head, delete+insert
swap), so ownership isolation, ordering, and transaction discipline are
tested behaviorally, and any drift between the data layer and its SQL
fails loudly (unexpected SQL → AssertionError).

Route tests follow the test_features_02.py pattern: monkeypatch
auth.authenticate to a fixed KeyRecord, patch the core module's db, mount
the router on a bare FastAPI app.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Any, Self

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pcai_llm.api import routes_conversations
from pcai_llm.api.auth import KeyRecord
from pcai_llm.core import conversations as conv_mod

HEADERS = {"Authorization": "Bearer test-key"}
KEY_A = uuid.uuid4()
KEY_B = uuid.uuid4()
USER_A = uuid.uuid4()


# ---------------------------------------------------------------------------
# in-memory db engine
# ---------------------------------------------------------------------------


class FakeTxn:
    def __init__(self, fake: FakeDB) -> None:
        self.fake = fake

    async def __aenter__(self) -> Self:
        self.fake.log.append("txn-begin")
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool:
        self.fake.log.append("txn-rollback" if exc_type else "txn-commit")
        return False


class FakeConn:
    """Connection face over the same store (used inside db.acquire())."""

    def __init__(self, fake: FakeDB) -> None:
        self.fake = fake

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        return await self.fake.fetchrow(sql, *args)

    async def execute(self, sql: str, *args: Any) -> str:
        return await self.fake.execute(sql, *args)

    async def executemany(self, sql: str, rows: list[tuple]) -> None:
        self.fake.log.append("insert-msgs")
        for cid, seq, role, content in rows:
            self.fake.msgs.setdefault(cid, []).append(
                {"seq": seq, "role": role, "content": content, "ts": self.fake._now()}
            )

    def transaction(self) -> FakeTxn:
        return FakeTxn(self.fake)


class FakeAcquire:
    def __init__(self, fake: FakeDB) -> None:
        self.fake = fake

    async def __aenter__(self) -> FakeConn:
        self.fake.log.append("acquire")
        return FakeConn(self.fake)

    async def __aexit__(self, *exc: object) -> bool:
        self.fake.log.append("release")
        return False


class FakeDB:
    """In-memory conversations + messages store with a strictly increasing
    clock (deterministic updated_at ordering)."""

    def __init__(self) -> None:
        self.convs: dict[uuid.UUID, dict[str, Any]] = {}
        self.msgs: dict[uuid.UUID, list[dict[str, Any]]] = {}
        self.log: list[str] = []
        self._clock = datetime.now(UTC)

    def _now(self) -> datetime:
        self._clock += timedelta(seconds=1)
        return self._clock

    def acquire(self) -> FakeAcquire:
        return FakeAcquire(self)

    def _tag(self, sql: str) -> str:
        s = " ".join(sql.split())
        if s.startswith("INSERT INTO conversations"):
            return "create"
        if "FOR UPDATE" in s:
            return "lock-head"
        if s.startswith("SELECT id, title, updated_at FROM conversations"):
            return "head"
        if s.startswith("SELECT c.id"):
            return "list"
        if s.startswith("SELECT seq, role, content, ts"):
            return "msgs"
        if s.startswith("UPDATE conversations SET title"):
            return "rename" if "RETURNING" in s else "bump"
        if s.startswith("DELETE FROM conversation_messages"):
            return "delete-msgs"
        if s.startswith("INSERT INTO conversation_messages"):
            return "insert-msgs"
        if s.startswith("DELETE FROM conversations"):
            return "delete-conv"
        return s[:50]

    def _owned(self, cid: uuid.UUID, key_id: uuid.UUID) -> dict[str, Any] | None:
        row = self.convs.get(cid)
        return row if row is not None and row["key_id"] == key_id else None

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        self.log.append(self._tag(sql))
        s = " ".join(sql.split())
        # reused across the branches below; the FOR UPDATE / SELECT / UPDATE
        # arms rebind it to _owned()'s possibly-None result
        row: dict[str, Any] | None
        if s.startswith("INSERT INTO conversations"):
            row = {
                "id": uuid.uuid4(),
                "key_id": args[0],
                "user_id": args[1],
                "title": args[2],
                "created_at": self._now(),
                "updated_at": self._now(),
            }
            self.convs[row["id"]] = row
            self.msgs.setdefault(row["id"], [])
            return dict(row)
        if "FOR UPDATE" in s:
            row = self._owned(args[0], args[1])
            return {"title": row["title"]} if row is not None else None
        if s.startswith("SELECT id, title, updated_at FROM conversations"):
            row = self._owned(args[0], args[1])
            if row is None:
                return None
            return {"id": row["id"], "title": row["title"], "updated_at": row["updated_at"]}
        if s.startswith("UPDATE conversations SET title") and "RETURNING" in s:
            row = self._owned(args[0], args[2])
            if row is None:
                return None
            row["title"] = args[1]
            row["updated_at"] = self._now()
            return {"id": row["id"], "title": row["title"], "updated_at": row["updated_at"]}
        raise AssertionError(f"unexpected fetchrow SQL: {s[:80]}")

    async def fetch(self, sql: str, *args: Any) -> Any:
        self.log.append(self._tag(sql))
        s = " ".join(sql.split())
        if s.startswith("SELECT c.id"):
            rows = [
                {
                    "id": c["id"],
                    "title": c["title"],
                    "updated_at": c["updated_at"],
                    "message_count": len(self.msgs.get(c["id"], [])),
                }
                for c in self.convs.values()
                if c["key_id"] == args[0]
            ]
            rows.sort(key=lambda r: r["updated_at"], reverse=True)
            return rows[: args[1]]
        if s.startswith("SELECT seq, role, content, ts"):
            return [dict(m) for m in sorted(self.msgs.get(args[0], []), key=lambda m: m["seq"])]
        raise AssertionError(f"unexpected fetch SQL: {s[:80]}")

    async def execute(self, sql: str, *args: Any) -> str:
        self.log.append(self._tag(sql))
        s = " ".join(sql.split())
        if s.startswith("UPDATE conversations SET title"):
            row = self.convs.get(args[0])
            assert row is not None, "bump targeted a missing conversation"
            row["title"] = args[1]
            row["updated_at"] = self._now()
            return "UPDATE 1"
        if s.startswith("DELETE FROM conversation_messages"):
            removed = len(self.msgs.get(args[0], []))
            self.msgs[args[0]] = []
            return f"DELETE {removed}"
        if s.startswith("INSERT INTO conversation_messages"):
            cid, seq, role, content = args
            self.msgs.setdefault(cid, []).append({"seq": seq, "role": role, "content": content, "ts": self._now()})
            return "INSERT 0 1"
        if s.startswith("DELETE FROM conversations"):
            row = self._owned(args[0], args[1])
            if row is None:
                return "DELETE 0"
            del self.convs[row["id"]]
            self.msgs.pop(row["id"], None)
            return "DELETE 1"
        raise AssertionError(f"unexpected execute SQL: {s[:80]}")

    async def fetchval(self, sql: str, *args: Any) -> Any:
        raise AssertionError(f"unexpected fetchval SQL: {sql[:80]}")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _key() -> KeyRecord:
    return KeyRecord(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        name="k",
        key_prefix="hllm-XX",
        scopes=["proxy"],
        allowed_models=[],
        rate_limit_rpm=1000,
        rate_limit_tpm=1_000_000,
        max_concurrent=0,
        expires_at=None,
    )


@pytest.fixture
def fake() -> FakeDB:
    return FakeDB()


def _client(monkeypatch: pytest.MonkeyPatch, fake: FakeDB, *keys: KeyRecord) -> TestClient:
    """Mount the router with authenticate resolving bearer ``tok-1`` .. to the
    given keys in order (anything else → None → 401), and the core data layer
    on ``fake``. One client can therefore act as several keys."""
    import pcai_llm.api.auth as auth_mod

    table = {f"tok-{i}": k for i, k in enumerate(keys, start=1)}

    async def fake_auth(plaintext: str) -> KeyRecord | None:
        return table.get(plaintext)

    monkeypatch.setattr(auth_mod, "authenticate", fake_auth)
    monkeypatch.setattr(conv_mod, "db", fake)
    app = FastAPI()
    app.include_router(routes_conversations.router)
    return TestClient(app, raise_server_exceptions=False)


HEADERS_A = {"Authorization": "Bearer tok-1"}
HEADERS_B = {"Authorization": "Bearer tok-2"}


# ---------------------------------------------------------------------------
# title helpers
# ---------------------------------------------------------------------------


def test_sanitize_title_rules() -> None:
    assert conv_mod.sanitize_title("  Hello   World  ") == "Hello World"
    assert conv_mod.sanitize_title(" \n\t ") == conv_mod.DEFAULT_TITLE
    assert conv_mod.sanitize_title("") == conv_mod.DEFAULT_TITLE
    assert conv_mod.sanitize_title("x" * 500) == "x" * conv_mod.MAX_TITLE


def test_auto_title_first_user_message() -> None:
    msgs = [
        {"role": "assistant", "content": "greeting first, ignored"},
        {"role": "user", "content": "  How do I   rotate keys?\n"},
        {"role": "user", "content": "later message ignored"},
    ]
    assert conv_mod.auto_title(msgs) == "How do I rotate keys?"
    # only-bot transcripts and non-list payloads keep the placeholder
    assert conv_mod.auto_title([{"role": "assistant", "content": "only bot"}]) == conv_mod.DEFAULT_TITLE
    assert conv_mod.auto_title([]) == conv_mod.DEFAULT_TITLE
    assert conv_mod.auto_title(None) == conv_mod.DEFAULT_TITLE
    assert conv_mod.auto_title([{"role": "user", "content": "y" * 100}]) == "y" * conv_mod.TITLE_CHARS


# ---------------------------------------------------------------------------
# data layer
# ---------------------------------------------------------------------------


async def test_create_defaults_and_title_cap(fake: FakeDB, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(conv_mod, "db", fake)
    conv = await conv_mod.create(KEY_A, USER_A)
    assert conv["title"] == conv_mod.DEFAULT_TITLE
    assert fake.convs[conv["id"]]["key_id"] == KEY_A
    assert fake.convs[conv["id"]]["user_id"] == USER_A
    named = await conv_mod.create(KEY_A, USER_A, title="  A\n B  ")
    assert named["title"] == "A B"
    over = await conv_mod.create(KEY_A, USER_A, title="t" * 500)
    assert len(over["title"]) == conv_mod.MAX_TITLE


async def test_list_newest_first_and_counts(fake: FakeDB, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(conv_mod, "db", fake)
    first = await conv_mod.create(KEY_A, USER_A)
    second = await conv_mod.create(KEY_A, USER_A)
    # replace bumps updated_at: first jumps above the newer-created second
    n = await conv_mod.replace_messages(
        KEY_A,
        first["id"],
        [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}],
    )
    assert n == 2
    rows = await conv_mod.list(KEY_A)
    assert [r["id"] for r in rows] == [first["id"], second["id"]]
    assert rows[0]["message_count"] == 2
    assert rows[1]["message_count"] == 0
    # another key sees nothing at all
    assert await conv_mod.list(KEY_B) == []
    # limit bounds the page, newest first
    assert [r["id"] for r in await conv_mod.list(KEY_A, limit=1)] == [first["id"]]


async def test_ownership_isolation_core(fake: FakeDB, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(conv_mod, "db", fake)
    conv = await conv_mod.create(KEY_A, USER_A)
    await conv_mod.replace_messages(KEY_A, conv["id"], [{"role": "user", "content": "secret"}])
    # key B cannot read, rename, overwrite, or delete — and cannot confirm existence
    assert await conv_mod.get(KEY_B, conv["id"]) is None
    assert await conv_mod.get_meta(KEY_B, conv["id"]) is None
    assert await conv_mod.rename(KEY_B, conv["id"], "hijack") is None
    assert await conv_mod.delete(KEY_B, conv["id"]) is False
    with pytest.raises(conv_mod.ConversationNotFound):
        await conv_mod.replace_messages(KEY_B, conv["id"], [{"role": "user", "content": "x"}])
    stored = await conv_mod.get(KEY_A, conv["id"])
    assert stored is not None
    assert stored["title"] == "secret"
    assert len(stored["messages"]) == 1


async def test_replace_messages_validation_precise(fake: FakeDB, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(conv_mod, "db", fake)
    conv = await conv_mod.create(KEY_A, USER_A)

    too_many = [{"role": "user", "content": "m"} for _ in range(conv_mod.MAX_MSGS + 1)]
    with pytest.raises(conv_mod.ConversationValidationError) as exc:
        await conv_mod.replace_messages(KEY_A, conv["id"], too_many)
    assert exc.value.code == "too_many_messages"
    assert "too many messages" in str(exc.value)

    with pytest.raises(conv_mod.ConversationValidationError) as exc:
        await conv_mod.replace_messages(KEY_A, conv["id"], [{"role": "system", "content": "x"}])
    assert exc.value.code == "invalid_role"
    assert "role" in str(exc.value)

    oversized = [{"role": "user", "content": "z" * (conv_mod.MAX_MSG_CHARS + 1)}]
    with pytest.raises(conv_mod.ConversationValidationError) as exc:
        await conv_mod.replace_messages(KEY_A, conv["id"], oversized)
    assert exc.value.code == "message_too_large"
    assert "too large" in str(exc.value)

    with pytest.raises(conv_mod.ConversationValidationError) as exc:
        await conv_mod.replace_messages(KEY_A, conv["id"], [{"role": "user", "content": 5}])
    assert exc.value.code == "invalid_content"

    with pytest.raises(conv_mod.ConversationValidationError) as exc:
        await conv_mod.replace_messages(KEY_A, conv["id"], ["nope"])
    assert exc.value.code == "invalid_message"

    with pytest.raises(conv_mod.ConversationValidationError) as exc:
        await conv_mod.replace_messages(KEY_A, conv["id"], "not-a-list")
    assert exc.value.code == "invalid_list"

    # exact caps pass and nothing from the failed attempts ever landed
    edge = "z" * conv_mod.MAX_MSG_CHARS
    assert await conv_mod.replace_messages(KEY_A, conv["id"], [{"role": "user", "content": edge}]) == 1
    stored = await conv_mod.get(KEY_A, conv["id"])
    assert stored is not None
    assert len(stored["messages"]) == 1
    assert stored["messages"][0]["content"] == edge


async def test_replace_messages_transaction_discipline(fake: FakeDB, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(conv_mod, "db", fake)
    conv = await conv_mod.create(KEY_A, USER_A)
    fake.log.clear()  # drop the create noise; observe only the replace txn
    assert (
        await conv_mod.replace_messages(
            KEY_A,
            conv["id"],
            [
                {"role": "user", "content": "q1"},
                {"role": "assistant", "content": "a1"},
                {"role": "user", "content": "q2"},
            ],
        )
        == 3
    )
    # delete+insert swap ran inside ONE acquired transaction, in order
    assert fake.log == [
        "acquire",
        "txn-begin",
        "lock-head",
        "bump",
        "delete-msgs",
        "insert-msgs",
        "txn-commit",
        "release",
    ]
    stored = await conv_mod.get(KEY_A, conv["id"])
    assert stored is not None
    assert [m["seq"] for m in stored["messages"]] == [0, 1, 2]

    # second save REPLACES (no duplicates), clearing works too
    assert await conv_mod.replace_messages(KEY_A, conv["id"], [{"role": "user", "content": "q3"}]) == 1
    stored = await conv_mod.get(KEY_A, conv["id"])
    assert stored is not None
    assert [m["content"] for m in stored["messages"]] == ["q3"]
    assert await conv_mod.replace_messages(KEY_A, conv["id"], []) == 0
    stored = await conv_mod.get(KEY_A, conv["id"])
    assert stored is not None
    assert stored["messages"] == []

    # a foreign/unknown id fails inside the transaction → rollback marker
    fake.log.clear()
    with pytest.raises(conv_mod.ConversationNotFound):
        await conv_mod.replace_messages(KEY_A, uuid.uuid4(), [{"role": "user", "content": "x"}])
    assert fake.log == ["acquire", "txn-begin", "lock-head", "txn-rollback", "release"]


async def test_auto_title_on_first_save_then_respects_rename(fake: FakeDB, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(conv_mod, "db", fake)
    conv = await conv_mod.create(KEY_A, USER_A)
    assert conv["title"] == conv_mod.DEFAULT_TITLE
    await conv_mod.replace_messages(
        KEY_A, conv["id"], [{"role": "user", "content": "What is the PCAI gateway? Explain please"}]
    )
    stored = await conv_mod.get(KEY_A, conv["id"])
    assert stored is not None
    assert stored["title"] == "What is the PCAI gateway? Explain please"
    # an explicit rename survives subsequent saves (auto-title fires only
    # while the placeholder is still in place)
    await conv_mod.rename(KEY_A, conv["id"], "Custom name")
    await conv_mod.replace_messages(KEY_A, conv["id"], [{"role": "user", "content": "second turn"}])
    stored = await conv_mod.get(KEY_A, conv["id"])
    assert stored is not None
    assert stored["title"] == "Custom name"


async def test_rename_bumps_updated_at_ordering(fake: FakeDB, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(conv_mod, "db", fake)
    older = await conv_mod.create(KEY_A, USER_A)
    newer = await conv_mod.create(KEY_A, USER_A)
    assert [r["id"] for r in await conv_mod.list(KEY_A)] == [newer["id"], older["id"]]
    renamed = await conv_mod.rename(KEY_A, older["id"], "Pinned topic")
    assert renamed is not None
    assert renamed["title"] == "Pinned topic"
    assert renamed["id"] == older["id"]
    assert "updated_at" in renamed
    assert [r["id"] for r in await conv_mod.list(KEY_A)] == [older["id"], newer["id"]]


async def test_delete_removes_messages(fake: FakeDB, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(conv_mod, "db", fake)
    conv = await conv_mod.create(KEY_A, USER_A)
    await conv_mod.replace_messages(KEY_A, conv["id"], [{"role": "user", "content": "bye"}])
    assert await conv_mod.delete(KEY_A, conv["id"]) is True
    assert await conv_mod.delete(KEY_A, conv["id"]) is False  # idempotent-ish: gone
    assert await conv_mod.get(KEY_A, conv["id"]) is None
    assert await conv_mod.list(KEY_A) == []
    assert fake.msgs.get(conv["id"], []) == []


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------


def test_routes_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch, FakeDB(), _key())

    r = client.post("/v1/conversations", json={"title": "  My   Chat "}, headers=HEADERS_A)
    assert r.status_code == 201
    body = r.json()
    assert body["title"] == "My Chat"
    cid = body["id"]
    assert "updated_at" in body

    r = client.put(
        f"/v1/conversations/{cid}/messages",
        json={"messages": [{"role": "user", "content": "hello"}, {"role": "assistant", "content": "hi there"}]},
        headers=HEADERS_A,
    )
    assert r.status_code == 200
    assert r.json()["saved"] == 2
    assert "updated_at" in r.json()

    r = client.get(f"/v1/conversations/{cid}", headers=HEADERS_A)
    assert r.status_code == 200
    got = r.json()
    assert got["id"] == cid
    assert [m["seq"] for m in got["messages"]] == [0, 1]
    assert got["messages"][0]["content"] == "hello"

    r = client.patch(f"/v1/conversations/{cid}", json={"title": "Renamed"}, headers=HEADERS_A)
    assert r.status_code == 200
    assert r.json()["title"] == "Renamed"

    r = client.get("/v1/conversations", headers=HEADERS_A)
    assert r.status_code == 200
    data = r.json()["data"]
    assert len(data) == 1
    assert data[0]["id"] == cid
    assert data[0]["title"] == "Renamed"
    assert data[0]["message_count"] == 2

    r = client.delete(f"/v1/conversations/{cid}", headers=HEADERS_A)
    assert r.status_code == 200
    assert r.json() == {"deleted": True}
    assert client.get(f"/v1/conversations/{cid}", headers=HEADERS_A).status_code == 404


def test_routes_require_key(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch, FakeDB(), _key())
    assert client.get("/v1/conversations").status_code == 401  # no header
    assert client.get("/v1/conversations", headers={"Authorization": "Basic nope"}).status_code == 401
    # unknown token (authenticate → None) fails closed too
    assert client.get("/v1/conversations", headers={"Authorization": "Bearer tok-9"}).status_code == 401


def test_routes_404_unknown_and_garbage_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch, FakeDB(), _key())
    cases: list[tuple[str, str, dict[str, Any]]] = [
        ("get", "/v1/conversations/not-a-uuid", {}),
        ("get", "/v1/conversations/00000000-0000-0000-0000-000000000000", {}),
        ("patch", "/v1/conversations/not-a-uuid", {"json": {"title": "t"}}),
        ("patch", f"/v1/conversations/{uuid.uuid4()}", {"json": {"title": "t"}}),
        ("put", "/v1/conversations/also-bad/messages", {"json": {"messages": []}}),
        ("put", f"/v1/conversations/{uuid.uuid4()}/messages", {"json": {"messages": []}}),
        ("delete", "/v1/conversations/zzz", {}),
        ("delete", f"/v1/conversations/{uuid.uuid4()}", {}),
    ]
    for method, path, kwargs in cases:
        r = getattr(client, method)(path, headers=HEADERS_A, **kwargs)
        assert r.status_code == 404, (method, path, r.status_code)
        assert r.json() == {"error": "not found"}


def test_routes_invalid_bodies_400(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch, FakeDB(), _key())
    cid = client.post("/v1/conversations", json={}, headers=HEADERS_A).json()["id"]
    base = f"/v1/conversations/{cid}"

    cases: list[tuple[str, str, dict[str, Any]]] = [
        ("post", "/v1/conversations", {"content": b"{nope"}),  # malformed JSON
        ("post", "/v1/conversations", {"json": [1, 2]}),  # not an object
        ("post", "/v1/conversations", {"json": {"title": 5}}),
        ("patch", base, {"json": {}}),  # title missing
        ("patch", base, {"json": {"title": 5}}),
        ("put", f"{base}/messages", {"json": {}}),  # messages missing
        ("put", f"{base}/messages", {"json": {"messages": "nope"}}),
        ("put", f"{base}/messages", {"json": {"messages": [{"role": "tool", "content": "x"}]}}),
        ("put", f"{base}/messages", {"json": {"messages": [{"role": "user"}]}}),  # content missing
    ]
    for method, path, kwargs in cases:
        r = getattr(client, method)(path, headers=HEADERS_A, **kwargs)
        assert r.status_code == 400, (method, path, r.status_code, r.text)
        assert "error" in r.json(), (method, path, r.text)
    # precise messages
    r = client.put(f"{base}/messages", json={"messages": [{"role": "tool", "content": "x"}]}, headers=HEADERS_A)
    assert "role" in r.json()["error"]


def test_routes_put_caps_413(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch, FakeDB(), _key())
    cid = client.post("/v1/conversations", json={}, headers=HEADERS_A).json()["id"]

    many = [{"role": "user", "content": "m"} for _ in range(conv_mod.MAX_MSGS + 1)]
    r = client.put(f"/v1/conversations/{cid}/messages", json={"messages": many}, headers=HEADERS_A)
    assert r.status_code == 413
    assert "too many messages" in r.json()["error"]

    big = [{"role": "user", "content": "z" * (conv_mod.MAX_MSG_CHARS + 1)}]
    r = client.put(f"/v1/conversations/{cid}/messages", json={"messages": big}, headers=HEADERS_A)
    assert r.status_code == 413
    assert "too large" in r.json()["error"]


def test_routes_ownership_isolation(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeDB()
    client = _client(monkeypatch, fake, _key(), _key())  # tok-1 → key A, tok-2 → key B

    cid = client.post("/v1/conversations", json={"title": "A private"}, headers=HEADERS_A).json()["id"]
    r = client.put(
        f"/v1/conversations/{cid}/messages",
        json={"messages": [{"role": "user", "content": "secret"}]},
        headers=HEADERS_A,
    )
    assert r.status_code == 200

    # key B: 404 on every verb, empty list, nothing leaked
    assert client.get(f"/v1/conversations/{cid}", headers=HEADERS_B).status_code == 404
    assert client.patch(f"/v1/conversations/{cid}", json={"title": "hijack"}, headers=HEADERS_B).status_code == 404
    r = client.put(
        f"/v1/conversations/{cid}/messages",
        json={"messages": [{"role": "user", "content": "x"}]},
        headers=HEADERS_B,
    )
    assert r.status_code == 404
    assert client.delete(f"/v1/conversations/{cid}", headers=HEADERS_B).status_code == 404
    assert client.get("/v1/conversations", headers=HEADERS_B).json()["data"] == []

    # A is intact — explicit title survived B's attempted hijack and PUT
    got = client.get(f"/v1/conversations/{cid}", headers=HEADERS_A).json()
    assert got["title"] == "A private"
    assert len(got["messages"]) == 1


def test_routes_auto_title_flows_through_api(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(monkeypatch, FakeDB(), _key())
    cid = client.post("/v1/conversations", json={}, headers=HEADERS_A).json()["id"]
    assert client.post("/v1/conversations", json={}, headers=HEADERS_A).json()["title"] == "New conversation"
    client.put(
        f"/v1/conversations/{cid}/messages",
        json={"messages": [{"role": "user", "content": "Explain budget cascades in one line"}]},
        headers=HEADERS_A,
    )
    rows = client.get("/v1/conversations", headers=HEADERS_A).json()["data"]
    assert rows[0]["title"] == "Explain budget cascades in one line"
