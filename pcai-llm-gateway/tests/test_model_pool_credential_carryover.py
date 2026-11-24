"""Model-pool credentials across a base_url edit — the (model_name, base_url) trap.

Scenario (operator report, 2026-11-18): register a pool against the REMOTE
address with a panel API key; it fails to connect; edit the endpoint's base URL
to the LOCAL address. The console's API key field still read "(unchanged)"
(blank = keep the stored credential), but the stored credential did not survive
the edit.

WHY IT HAPPENED (routes_admin.upsert_model): "null = keep" was implemented by
OMITTING auth_token_enc from the ON CONFLICT (model_name, base_url) DO UPDATE
SET list. That keeps something only when a row with the SAME base_url already
exists. A URL edit is a NEW (model_name, base_url) pair: the INSERT has no
conflict, so auth_token_enc landed NULL — and the REPLACE-prune then DELETEd the
old row, which held the only copy of the ciphertext. The credential was not
"not carried over"; it was destroyed, and the audit row recorded
panel_credential_set=false with no value, so it was not recoverable anywhere.

THE FIX (A+B, same day): the console names the row's previous base_url in
`api_key_from` when an endpoint MOVES, and the server copies the stored
ciphertext onto the new row inside the write's own transaction, before the
prune. The copy is verbatim (no decrypt/re-encrypt), a typed api_key wins, and
a source that names no stored credential is a LOUD 400 rather than the silent
credential-less row this parameter exists to prevent.

test_control_same_url_edit_keeps_the_stored_key is the counterweight: the
tri-state itself was never broken, so the same-URL keep path must stay intact.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pcai_llm.api import routes_admin
from pcai_llm.config import settings
from pcai_llm.core import secretbox

H = {"Authorization": "Bearer admin-secret-123"}
REMOTE = "http://glm.example.com/v1"
LOCAL = "http://vllm-svc.ai.svc.cluster.local/v1"
KEY = "sk-remote-secret"


class _Conn:
    def __init__(self, db: EndpointsDB) -> None:
        self._db = db

    def transaction(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc: object):
        return False

    async def execute(self, sql: str, *a: Any):
        return await self._db.execute(sql, *a)

    async def fetch(self, sql: str, *a: Any):
        return await self._db.fetch(sql, *a)

    async def fetchrow(self, sql: str, *a: Any):
        return None

    async def fetchval(self, sql: str, *a: Any):
        return 1


class EndpointsDB:
    """(model_name, base_url) -> row, with the semantics of the handler's SQL:
    ON CONFLICT (model_name, base_url), a keep-write that omits the column, and
    the carry-over read that resolves `api_key_from` sources.
    """

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], dict[str, Any]] = {}
        self.log: list[str] = []

    def acquire(self):
        return _Conn(self)

    async def execute(self, sql: str, *a: Any):
        head = sql.split("VALUES")[0]
        if "INSERT INTO model_endpoints" in sql:
            if "auth_token_enc" in head:
                model, url, pri, weight, secret, enc, mtype = a
                row = self.rows.setdefault((model, url), {"auth_token_enc": None})
                row.update(
                    priority=pri,
                    weight=weight,
                    api_key_secret=secret,
                    auth_token_enc=(None if enc in ("", None) else enc),
                    model_type=mtype,
                )
                self.log.append(f"upsert(WRITE key)          {url}")
            else:
                # "keep": auth_token_enc is absent from the SET list entirely,
                # so a conflicting row keeps its value and a NEW row gets NULL.
                model, url, pri, weight, secret, mtype = a
                key = (model, url)
                existed = key in self.rows
                row = self.rows.setdefault(key, {"auth_token_enc": None})
                row.update(priority=pri, weight=weight, api_key_secret=secret, model_type=mtype)
                self.log.append(f"upsert(keep key, {'UPDATE' if existed else 'INSERT'}) {url}")
            return "INSERT 0 1"
        if "DELETE FROM model_endpoints" in sql:
            model, urls = a
            for key in [k for k in self.rows if k[0] == model and k[1] not in urls]:
                del self.rows[key]
                self.log.append(f"PRUNE (row + ciphertext dropped) {key[1]}")
            return "DELETE 1"
        return "OK"

    async def fetch(self, sql: str, *a: Any):
        # the carry-over source read: SELECT base_url, auth_token_enc ... ANY($2)
        if "SELECT base_url, auth_token_enc" in sql:
            model, urls = a
            return [
                {"base_url": url, "auth_token_enc": r.get("auth_token_enc")}
                for (m, url), r in self.rows.items()
                if m == model and url in urls
            ]
        # the console's edit read: GET /admin/models/{name}/pool
        if "FROM model_endpoints" in sql and "WHERE model_name" in sql:
            return [
                {
                    "base_url": url,
                    "priority": r.get("priority", 0),
                    "weight": r.get("weight", 1),
                    "api_key_secret": r.get("api_key_secret"),
                    "auth_configured": r.get("auth_token_enc") is not None,
                    "model_type": r.get("model_type", "chat"),
                }
                for (m, url), r in self.rows.items()
                if m == a[0]
            ]
        return []

    async def fetchrow(self, sql: str, *a: Any):
        return None


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "admin_api_keys", "admin-secret-123")
    monkeypatch.setattr(settings, "secrets_master_key", "repro-master")
    monkeypatch.setattr(secretbox, "_fernet", None)
    fake = EndpointsDB()
    monkeypatch.setattr(routes_admin, "db", fake)
    audited: list[dict[str, Any]] = []

    async def _fake_audit(_request, **kw: Any):
        audited.append(kw)

    async def _noop_refresh():
        return None

    monkeypatch.setattr(routes_admin, "_audit", _fake_audit)
    monkeypatch.setattr(routes_admin, "_refresh_registry_best_effort", _noop_refresh)
    app = FastAPI()
    app.include_router(routes_admin.router)
    tc = TestClient(app, raise_server_exceptions=False)
    # register the pool against the REMOTE address WITH the panel key
    r = tc.post(
        "/admin/models",
        headers=H,
        json={
            "model_name": "glm",
            "model_type": "chat",
            "endpoints": [{"base_url": REMOTE, "api_key": KEY}],
        },
    )
    assert r.status_code == 200, r.text
    assert fake.rows[("glm", REMOTE)]["auth_token_enc"], "precondition: key stored"
    # the console's edit read renders "(unchanged)" (auth_configured true)
    pool = tc.get("/admin/models/glm/pool", headers=H).json()
    assert pool["endpoints"][0]["auth_configured"] is True
    return tc, fake, audited


def test_control_same_url_edit_keeps_the_stored_key(env) -> None:
    """Control: editing priority/weight with the URL UNCHANGED keeps the key.

    The tri-state was never broken — the row identity was. This is the path that
    must not regress while the moved case is fixed.
    """
    tc, fake, _ = env
    r = tc.post(
        "/admin/models",
        headers=H,
        json={
            "model_name": "glm",
            "model_type": "chat",
            "endpoints": [{"base_url": REMOTE, "priority": 2, "api_key": None}],
        },
    )
    assert r.status_code == 200, r.text
    assert fake.rows[("glm", REMOTE)]["auth_token_enc"] is not None
    assert fake.rows[("glm", REMOTE)]["priority"] == 2
    assert secretbox.decrypt(fake.rows[("glm", REMOTE)]["auth_token_enc"]) == KEY


def test_url_change_carries_the_stored_key(env) -> None:
    """THE BUG (remote -> local): the credential must survive the move.

    The console sends api_key_from=<the row it loaded>; the server copies the
    ciphertext onto the new row before the prune removes the old one.
    """
    tc, fake, audited = env
    before = fake.rows[("glm", REMOTE)]["auth_token_enc"]
    r = tc.post(
        "/admin/models",
        headers=H,
        json={
            "model_name": "glm",
            "model_type": "chat",
            "endpoints": [{"base_url": LOCAL, "api_key": None, "api_key_from": REMOTE}],
        },
    )
    assert r.status_code == 200, r.text
    print("\nSQL trace:")
    for line in fake.log:
        print("   ", line)
    # the moved endpoint serves WITH the credential
    assert fake.rows[("glm", LOCAL)]["auth_token_enc"] is not None
    assert secretbox.decrypt(fake.rows[("glm", LOCAL)]["auth_token_enc"]) == KEY
    # copied VERBATIM: no decrypt/re-encrypt round trip (Fernet is randomized,
    # so an equal ciphertext proves the bytes moved, not the plaintext)
    assert fake.rows[("glm", LOCAL)]["auth_token_enc"] == before
    # and the stale row (with its now-redundant copy) is pruned
    assert ("glm", REMOTE) not in fake.rows
    # the trail says the credential was carried, not cleared
    details = audited[-1]["details"]["endpoints"][0]
    assert details["panel_credential_carried_from"] == REMOTE
    assert details["panel_credential_set"] is False


def test_typed_key_wins_over_carry_over(env) -> None:
    """Rotation in the same save as a move: the pasted key is the one stored."""
    tc, fake, _ = env
    r = tc.post(
        "/admin/models",
        headers=H,
        json={
            "model_name": "glm",
            "model_type": "chat",
            "endpoints": [{"base_url": LOCAL, "api_key": "sk-local", "api_key_from": REMOTE}],
        },
    )
    assert r.status_code == 200, r.text
    assert secretbox.decrypt(fake.rows[("glm", LOCAL)]["auth_token_enc"]) == "sk-local"


def test_unknown_carry_over_source_is_a_loud_400(env) -> None:
    """A source that names no stored credential must NOT write a credential-less
    row — that silent outcome is the bug being fixed."""
    tc, fake, _ = env
    r = tc.post(
        "/admin/models",
        headers=H,
        json={
            "model_name": "glm",
            "model_type": "chat",
            "endpoints": [{"base_url": LOCAL, "api_key": None, "api_key_from": "http://typo.example.com/v1"}],
        },
    )
    assert r.status_code == 400, r.text
    assert "names no stored credential" in r.text
    # nothing was written and nothing was pruned
    assert ("glm", LOCAL) not in fake.rows
    assert fake.rows[("glm", REMOTE)]["auth_token_enc"] is not None


def test_carry_over_from_a_credential_less_row_is_a_loud_400(env) -> None:
    """A source row that EXISTS but stores nothing has nothing to carry: 400.

    Otherwise the console's promise ("carried over") would produce exactly the
    credential-less row the parameter exists to prevent."""
    tc, fake, _ = env
    keyless = "http://plain.example.com/v1"
    r = tc.post(
        "/admin/models",
        headers=H,
        json={
            "model_name": "glm",
            "model_type": "chat",
            "endpoints": [
                {"base_url": REMOTE, "api_key": None},
                {"base_url": keyless, "api_key": None},
            ],
        },
    )
    assert r.status_code == 200, r.text
    assert fake.rows[("glm", keyless)]["auth_token_enc"] is None
    r2 = tc.post(
        "/admin/models",
        headers=H,
        json={
            "model_name": "glm",
            "model_type": "chat",
            "endpoints": [{"base_url": LOCAL, "api_key": None, "api_key_from": keyless}],
        },
    )
    assert r2.status_code == 400, r2.text
    assert ("glm", LOCAL) not in fake.rows


def test_api_key_from_equal_to_base_url_is_a_plain_keep(env) -> None:
    """A source equal to the target is not a move — it must not 400."""
    tc, fake, _ = env
    r = tc.post(
        "/admin/models",
        headers=H,
        json={
            "model_name": "glm",
            "model_type": "chat",
            "endpoints": [{"base_url": REMOTE, "api_key": None, "api_key_from": REMOTE}],
        },
    )
    assert r.status_code == 200, r.text
    assert secretbox.decrypt(fake.rows[("glm", REMOTE)]["auth_token_enc"]) == KEY


def test_explicit_clear_still_clears(env) -> None:
    """api_key: "" stays the explicit clear — the carry-over must not revive it."""
    tc, fake, _ = env
    r = tc.post(
        "/admin/models",
        headers=H,
        json={
            "model_name": "glm",
            "model_type": "chat",
            "endpoints": [{"base_url": REMOTE, "api_key": "", "api_key_from": REMOTE}],
        },
    )
    assert r.status_code == 200, r.text
    assert fake.rows[("glm", REMOTE)]["auth_token_enc"] is None
