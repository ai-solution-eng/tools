"""Session affinity: a conversation pinned to an ENDPOINT (X-PCAI-Session).

Covers the two halves of the feature:

* Pool.pick(preferred_id) — a pinned endpoint wins while it is in the pool
  and healthy; cooling-down, disabled/absent or unknown ids fall through to
  the normal priority/weight pick (routing state, never policy).
* The chat route's pin lifecycle — read (flag + header + non-relay gates),
  store on a successful serve, drop on a breaker trip / transport error,
  header ignored with the flag off, and the ep: row scope staying out of
  the cascade head's unprefixed scope (the 2026-10-23 collision defect).

No live Postgres: the module-db monkeypatch pattern from tests/test_proxy.py
with an in-memory model_stickiness table that speaks the real SQL shapes
(INSERT ... ON CONFLICT upsert, DELETE, aged SELECT).
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

import httpx2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pcai_llm.api import routes_proxy
from pcai_llm.api.auth import KeyRecord
from pcai_llm.config import settings
from pcai_llm.core import budgets as budgets_mod
from pcai_llm.core import canonical, stickiness
from pcai_llm.core import cascade as cascade_mod
from pcai_llm.core import costs as costs_mod
from pcai_llm.routing.router import Endpoint, Pool, Router

MODEL = "test-model"
SESSION = "sess-1"
# The route no longer stores the raw header: stickiness.affinity_key() bounds
# and sha256-hashes it first (audit 2026-11-10 P2-2 — an unvalidated header was
# a per-request row-creation path). Seeds and assertions below must therefore
# use the SAME canonical key the route derives; _chat() still sends the raw
# header, which is the point.
AKEY = stickiness.affinity_key(SESSION)
AUTH = {"Authorization": "Bearer hllm-x"}


class FakeDB:
    """Budget/rate stubs PLUS an in-memory model_stickiness table.

    The sticky table answers the aged SELECT (model + age_s) and the
    upsert/delete the module issues; every other statement gets the
    test_proxy FakeDB behaviour (no profile, low window counters).
    """

    def __init__(self) -> None:
        self.sticky: dict[tuple[Any, str], tuple[str, float]] = {}  # (key_id, thread_key) -> (model, age_s)
        self.settled: list[tuple] = []

    async def fetch(self, sql: str, *args: Any):
        return []

    async def fetchrow(self, sql: str, *args: Any):
        if "FROM model_stickiness" in sql:
            row = self.sticky.get((args[0], args[1]))
            if row is None:
                return None
            return {"model": row[0], "age_s": row[1]}
        if "profiles" in sql:
            return None
        return {"requests": 1, "tokens": 10}

    async def execute(self, sql: str, *args: Any):
        if "INSERT INTO model_stickiness" in sql:
            self.sticky[(args[0], args[1])] = (args[2], 0.0)  # upsert: last write wins
            return "OK"
        if "DELETE FROM model_stickiness" in sql:
            self.sticky.pop((args[0], args[1]), None)
            return "OK"
        self.settled.append((sql, args))
        return "INSERT 0 1"


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
def env(monkeypatch: pytest.MonkeyPatch):
    """Fake auth + fake db (with sticky table) + two-endpoint mock pool."""
    key = _key()

    import pcai_llm.api.auth as auth_mod

    async def fake_authenticate(plaintext: str) -> KeyRecord:
        return key

    monkeypatch.setattr(auth_mod, "authenticate", fake_authenticate)

    fake_db = FakeDB()
    monkeypatch.setattr(budgets_mod, "db", fake_db)
    monkeypatch.setattr(cascade_mod, "db", fake_db)
    monkeypatch.setattr(costs_mod, "db", fake_db)
    monkeypatch.setattr(stickiness, "db", fake_db)

    seen: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(str(request.url))
        return httpx2.Response(
            200,
            json={
                "id": "cmpl-1",
                "object": "chat.completion",
                "model": MODEL,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 11, "completion_tokens": 5, "total_tokens": 16},
            },
        )

    mock_router = Router.with_mock(handler)
    # ep1 is the priority-0 default pick; ep2 (priority 1) is reachable ONLY
    # through a pin, so a pin-honoured request is unambiguous.
    mock_router.set_endpoints(
        MODEL,
        [
            Endpoint(id="ep1", model_name=MODEL, base_url="http://fake-a/v1", priority=0, weight=1),
            Endpoint(id="ep2", model_name=MODEL, base_url="http://fake-b/v1", priority=1, weight=1),
        ],
    )
    monkeypatch.setattr(routes_proxy, "model_router", mock_router)

    app = FastAPI()
    app.include_router(routes_proxy.router)
    tc = TestClient(app, raise_server_exceptions=False)
    return tc, fake_db, mock_router, seen, key


def _chat(tc: TestClient, session: str | None = None) -> Any:
    headers = dict(AUTH)
    if session is not None:
        headers["X-PCAI-Session"] = session
    return tc.post(
        "/v1/chat/completions",
        json={"model": MODEL, "messages": [{"role": "user", "content": "hello"}]},
        headers=headers,
    )


# ---------------------------------------------------------------------------
# 1. Pool.pick(preferred_id) — pure selection semantics
# ---------------------------------------------------------------------------


def _ep(name: str, priority: int = 0, weight: int = 1) -> Endpoint:
    return Endpoint(id=name, model_name="m", base_url=f"http://{name}", priority=priority, weight=weight)


def _pick_id(pool: Pool, preferred: str | None = None) -> str:
    ep = pool.pick(preferred)
    assert ep is not None, f"pick({preferred!r}) returned None"
    return ep.id


def test_pick_preferred_when_healthy() -> None:
    pool = Pool(endpoints=[_ep("a"), _ep("b")])
    for _ in range(20):
        assert _pick_id(pool, "b") == "b"
    assert _pick_id(pool, "a") == "a"


def test_pick_preferred_cooling_falls_back() -> None:
    pool = Pool(endpoints=[_ep("a"), _ep("b")])
    pool.endpoints[1].cooldown_until = time.monotonic() + 999
    assert _pick_id(pool, "b") == "a"  # pin points at a dead endpoint: normal pick
    assert _pick_id(pool, "ghost") == "a"  # unknown id: normal pick


def test_pick_preferred_absent_falls_back() -> None:
    pool = Pool(endpoints=[_ep("a"), _ep("b")])
    assert _pick_id(pool, "ghost") in ("a", "b")


def test_pick_no_preferred_is_backward_compatible() -> None:
    pool = Pool(endpoints=[_ep("a", priority=0), _ep("b", priority=1)])
    for _ in range(10):
        assert _pick_id(pool) == "a"


# ---------------------------------------------------------------------------
# 2. ep: scope isolation (the 2026-10-23 collision discipline)
# ---------------------------------------------------------------------------


def test_ep_scope_does_not_disturb_cascade_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    key = _key()
    thread = "0123456789abcdef"
    f = FakeDB()
    monkeypatch.setattr(stickiness, "db", f)
    # The cascade head (unprefixed, historical scope) and the endpoint pin
    # (ep: scope) for the SAME conversation fingerprint must be independent.
    asyncio.run(stickiness.set_sticky(key.id, thread, "GLM-5.3-Flash"))
    assert asyncio.run(stickiness.get_endpoint_pin(key.id, thread, settings.session_affinity_ttl_s)) is None
    asyncio.run(stickiness.set_endpoint_pin(key.id, thread, "ep2"))
    assert asyncio.run(stickiness.get_sticky(key.id, thread)) == "GLM-5.3-Flash"
    assert f.sticky[(key.id, thread)] == ("GLM-5.3-Flash", 0.0)
    assert f.sticky[(key.id, stickiness.SCOPE_ENDPOINT + thread)] == ("ep2", 0.0)
    assert asyncio.run(stickiness.get_endpoint_pin(key.id, thread, settings.session_affinity_ttl_s)) == "ep2"
    # Drop the pin: the cascade row must survive.
    asyncio.run(stickiness.drop_endpoint_pin(key.id, thread))
    assert (key.id, thread) in f.sticky
    assert (key.id, stickiness.SCOPE_ENDPOINT + thread) not in f.sticky


def test_endpoint_pin_ttl_expiry_drops_the_row(monkeypatch: pytest.MonkeyPatch) -> None:
    key = _key()
    f = FakeDB()
    monkeypatch.setattr(stickiness, "db", f)
    # A pin older than the TTL: expired — dropped, not just ignored.
    f.sticky[(key.id, stickiness.SCOPE_ENDPOINT + AKEY)] = ("ep2", float(settings.session_affinity_ttl_s + 1))
    assert asyncio.run(stickiness.get_endpoint_pin(key.id, AKEY, settings.session_affinity_ttl_s)) is None
    assert f.sticky == {}, "expired pin must be dropped, not just ignored"
    # A fresh pin under the TTL reads back.
    f.sticky[(key.id, stickiness.SCOPE_ENDPOINT + AKEY)] = ("ep2", 100.0)
    assert asyncio.run(stickiness.get_endpoint_pin(key.id, AKEY, settings.session_affinity_ttl_s)) == "ep2"
    # ttl <= 0 disables the expiry (housekeeping retention still applies).
    f.sticky[(key.id, stickiness.SCOPE_ENDPOINT + AKEY)] = ("ep2", 1e9)
    assert asyncio.run(stickiness.get_endpoint_pin(key.id, AKEY, 0)) == "ep2"


# ---------------------------------------------------------------------------
# 3. Chat route pin lifecycle
# ---------------------------------------------------------------------------


def test_pinned_endpoint_preferred_while_healthy(env) -> None:
    tc, fake_db, _, seen, key = env
    monkeypatch_flag = settings.session_affinity_enabled
    settings.session_affinity_enabled = True
    try:
        asyncio.run(stickiness.set_endpoint_pin(key.id, AKEY, "ep2"))
        r = _chat(tc, session=SESSION)
        assert r.status_code == 200, r.text
        assert seen[-1].startswith("http://fake-b"), f"pin not honoured: {seen}"
        # Successful serve refreshed the pin (TTL clock reset to a fresh row).
        assert fake_db.sticky.get((key.id, stickiness.SCOPE_ENDPOINT + AKEY), ("", -1.0))[0] == "ep2"
    finally:
        settings.session_affinity_enabled = monkeypatch_flag


def test_pin_stored_after_successful_serve(env) -> None:
    tc, fake_db, _, _, key = env
    settings.session_affinity_enabled = True
    try:
        r = _chat(tc, session=SESSION)  # no pin yet: normal pick -> ep1
        assert r.status_code == 200, r.text
        assert fake_db.sticky.get((key.id, stickiness.SCOPE_ENDPOINT + AKEY)) == ("ep1", 0.0)
        # Next turn with the same session re-reads the pin (ep1, already the
        # default pick — the point is the row exists and was served on).
        r2 = _chat(tc, session=SESSION)
        assert r2.status_code == 200, r2.text
    finally:
        settings.session_affinity_enabled = False


def test_cooling_down_pin_falls_back_and_follows_serving_endpoint(env) -> None:
    tc, fake_db, mock_router, seen, key = env
    settings.session_affinity_enabled = True
    try:
        asyncio.run(stickiness.set_endpoint_pin(key.id, AKEY, "ep2"))
        ep2 = next(e for e in mock_router.pool(MODEL).endpoints if e.id == "ep2")
        ep2.cooldown_until = time.monotonic() + 999  # breaker open
        r = _chat(tc, session=SESSION)
        assert r.status_code == 200, r.text
        assert seen[-1].startswith("http://fake-a"), f"cooling pin must fall back: {seen}"
        # The conversation now lives on ep1's cache: the pin followed the
        # serving endpoint (a pin that can never qualify is dead weight).
        assert fake_db.sticky.get((key.id, stickiness.SCOPE_ENDPOINT + AKEY)) == ("ep1", 0.0)
    finally:
        settings.session_affinity_enabled = False


def test_pin_to_absent_endpoint_falls_back(env) -> None:
    tc, fake_db, _, seen, key = env
    settings.session_affinity_enabled = True
    try:
        asyncio.run(stickiness.set_endpoint_pin(key.id, AKEY, "ghost"))  # not in the pool
        r = _chat(tc, session=SESSION)
        assert r.status_code == 200, r.text
        assert seen[-1].startswith("http://fake-a")
        assert fake_db.sticky.get((key.id, stickiness.SCOPE_ENDPOINT + AKEY)) == ("ep1", 0.0)
    finally:
        settings.session_affinity_enabled = False


def test_header_ignored_when_flag_off(env) -> None:
    tc, fake_db, _, seen, key = env
    settings.session_affinity_enabled = False  # default; state it
    try:
        asyncio.run(stickiness.set_endpoint_pin(key.id, AKEY, "ep2"))
        r = _chat(tc, session=SESSION)
        assert r.status_code == 200, r.text
        assert seen[-1].startswith("http://fake-a"), "flag off: the header must be inert"
        # No affinity read/write happened at all: the pin is untouched.
        assert fake_db.sticky.get((key.id, stickiness.SCOPE_ENDPOINT + AKEY)) == ("ep2", 0.0)
    finally:
        settings.session_affinity_enabled = False


def test_no_header_uses_conversation_fingerprint_as_affinity_key(env) -> None:
    """No X-PCAI-Session: the affinity key is the conversation fingerprint.

    CONTRACT CHANGE (2026-11-08, model groups F3): the affinity key is the
    client's session header OR the conversation's own fingerprint
    (canonical.thread_key), so endpoint/group pins work with NO client
    change, per conversation. Before this, a header-less request had no pin
    identity at all.
    """
    tc, fake_db, _, _, key = env
    settings.session_affinity_enabled = True
    try:
        r = _chat(tc)  # no header
        assert r.status_code == 200, r.text
        fp = canonical.thread_key([{"role": "user", "content": "hello"}], key.id)
        assert fp, "the conversation must have a fingerprint"
        assert fake_db.sticky == {(key.id, stickiness.SCOPE_ENDPOINT + fp): ("ep1", 0.0)}, (
            "no header: the pin is keyed by the conversation fingerprint"
        )
    finally:
        settings.session_affinity_enabled = False


def test_no_header_and_no_seed_writes_no_pin(env) -> None:
    """No session header AND no user-message seed: no fingerprint, no pin."""
    tc, fake_db, _, _, _ = env
    settings.session_affinity_enabled = True
    try:
        r = tc.post(
            "/v1/chat/completions",
            json={"model": MODEL, "messages": [{"role": "assistant", "content": "hi"}]},
            headers=dict(AUTH),
        )
        assert r.status_code == 200, r.text
        assert fake_db.sticky == {}, "no user-message seed: no affinity key, no pin"
    finally:
        settings.session_affinity_enabled = False


def test_transport_error_drops_the_pin(env, monkeypatch: pytest.MonkeyPatch) -> None:
    tc, fake_db, mock_router, _, key = env
    settings.session_affinity_enabled = True
    try:
        asyncio.run(stickiness.set_endpoint_pin(key.id, AKEY, "ep1"))

        def broken(request: httpx2.Request) -> httpx2.Response:
            raise httpx2.ConnectError("upstream refused")

        # `client` is a property over _client; swap the private handle.
        monkeypatch.setattr(mock_router, "_client", Router.with_mock(broken)._client)
        r = _chat(tc, session=SESSION)
        assert r.status_code == 502, r.text
        assert fake_db.sticky == {}, "breaker trip must drop the pin"
    finally:
        settings.session_affinity_enabled = False
