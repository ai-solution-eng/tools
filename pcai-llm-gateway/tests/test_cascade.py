"""Budget-driven cascade resolution tests.

Pure-logic + mocked-db coverage of the profile ordering semantics: pinned
never cascades, exhausted falls through in order, ACL boundary holds, and
full-exhaustion produces a per-candidate breakdown.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from pcai_llm.core import cascade as cascade_mod
from pcai_llm.core.cascade import classify_request, resolve_cascade

REGISTERED = {"llm-0", "llm-1", "llm-2"}


class FakeDB:
    def __init__(self, order: list[str], budget_ok: dict[str, bool] | None = None) -> None:
        self.order = order
        self.budget_ok = budget_ok or {}

    async def fetchrow(self, sql: str, *args: Any):
        if "profiles" in sql:
            return {"model_order": self.order} if self.order else None
        return None

    async def fetch(self, sql: str, *args: Any):
        return []


@pytest.fixture
def patch_db(monkeypatch: pytest.MonkeyPatch):
    def _install(order: list[str], budget_ok: dict[str, bool] | None = None):
        fake = FakeDB(order, budget_ok)
        monkeypatch.setattr(cascade_mod, "db", fake)
        # check_budgets (imported by value into cascade) uses core.budgets'
        # own module-level db handle — patch it to the same fake so the
        # no-budgets-configured path (all headroom) resolves in-memory.
        import pcai_llm.core.budgets as budgets_mod

        monkeypatch.setattr(budgets_mod, "db", fake)
        return fake

    return _install


# --- classification (pure) ---


def test_classify_pinned() -> None:
    assert classify_request("llm-1", REGISTERED, []) == "pinned"


def test_classify_alias() -> None:
    assert classify_request("auto", REGISTERED, []) == "alias"


def test_classify_forbidden() -> None:
    assert classify_request("llm-0", REGISTERED, ["llm-1"]) == "forbidden"


def test_classify_unregistered_is_alias() -> None:
    # classifier is ordering-agnostic; unregistered → alias (caller decides
    # 404 when no ordering exists)
    assert classify_request("ghost", REGISTERED, []) == "alias"


# --- resolution (mocked db) ---


async def test_pinned_no_ordering_serves_direct(patch_db) -> None:
    patch_db(order=[])
    res = await resolve_cascade(uuid.uuid4(), "llm-0", REGISTERED, [], 100)
    assert res.ok and res.model == "llm-0"


async def test_pinned_ignores_ordering(patch_db, monkeypatch: pytest.MonkeyPatch) -> None:
    """A pinned request NEVER cascades, even when the profile has an order."""
    patch_db(order=["llm-2", "llm-1"])
    res = await resolve_cascade(uuid.uuid4(), "llm-1", REGISTERED, [], 100)
    assert res.ok and res.model == "llm-1"  # served directly, no budget walk


async def test_alias_walks_ordering(patch_db, monkeypatch: pytest.MonkeyPatch) -> None:
    patch_db(order=["llm-0", "llm-1"])

    # first candidate exhausted → falls to second
    async def exhausted(key_id, model, est=0):
        class R:
            ok = False
            reason = "monthly budget exceeded"

        return R()

    monkeypatch.setattr(cascade_mod, "check_budgets", exhausted)
    res = await resolve_cascade(uuid.uuid4(), "auto", REGISTERED, [], 100)
    assert not res.ok and res.attempts and res.attempts[0]["model"] == "llm-0"


async def test_alias_first_with_headroom_wins(patch_db) -> None:
    patch_db(order=["llm-0", "llm-1"])  # no budgets configured → all ok
    res = await resolve_cascade(uuid.uuid4(), "auto", REGISTERED, [], 100)
    assert res.ok and res.model == "llm-0"


async def test_acl_boundary_holds_in_cascade(patch_db) -> None:
    """The ordering is a preference; allowed_models is a hard boundary."""
    patch_db(order=["llm-0", "llm-2"])
    res = await resolve_cascade(uuid.uuid4(), "auto", REGISTERED, ["llm-2"], 100)
    assert res.ok and res.model == "llm-2"  # llm-0 skipped (not allowed)


async def test_all_exhausted_gives_breakdown(patch_db, monkeypatch: pytest.MonkeyPatch) -> None:
    patch_db(order=["llm-0", "llm-1"])

    async def exhausted(key_id, model, est=0):
        class R:
            ok = False
            reason = f"{model} monthly budget exceeded"

        return R()

    monkeypatch.setattr(cascade_mod, "check_budgets", exhausted)
    res = await resolve_cascade(uuid.uuid4(), "auto", REGISTERED, [], 100)
    assert not res.ok
    assert res.attempts is not None
    assert [a["model"] for a in res.attempts] == ["llm-0", "llm-1"]
    assert all(a["status"] == "budget_exhausted" for a in res.attempts)


async def test_unregistered_ordering_entry_skipped(patch_db) -> None:
    patch_db(order=["ghost", "llm-1"])
    res = await resolve_cascade(uuid.uuid4(), "auto", REGISTERED, [], 100)
    assert res.ok and res.model == "llm-1"


async def test_no_ordering_alias_404(patch_db) -> None:
    patch_db(order=[])
    res = await resolve_cascade(uuid.uuid4(), "auto", REGISTERED, [], 100)
    assert not res.ok and "not registered" in res.reason


async def test_unprofiled_key_direct_path(patch_db) -> None:
    """No profile at all → identical to 0.1 behavior (direct resolution)."""
    patch_db(order=[])
    res = await resolve_cascade(uuid.uuid4(), "llm-2", REGISTERED, [], 100)
    assert res.ok and res.model == "llm-2"


async def test_resolution_carries_order(patch_db) -> None:
    """The resolved ordering rides the Resolution — the relay's mid-loop
    budget failover walks it for PINNED requests too (the 'GLM budget dies
    mid-turn → flow into Qwen' fix). Zero extra queries: resolve_cascade
    already fetched the order for its own walk."""
    patch_db(order=["llm-0", "llm-1"])
    res = await resolve_cascade(uuid.uuid4(), "llm-0", REGISTERED, [], 100)  # pinned, headroom
    assert res.ok and res.model == "llm-0" and res.order == ["llm-0", "llm-1"]

    res = await resolve_cascade(uuid.uuid4(), "auto", REGISTERED, [], 100)  # alias cascade
    assert res.ok and res.model == "llm-0" and res.order == ["llm-0", "llm-1"]

    patch_db(order=[])
    res = await resolve_cascade(uuid.uuid4(), "llm-0", REGISTERED, [], 100)
    assert res.ok and res.order == []


# --- chat-only walk (2026-10-29: "auto -> only chat models") -----------------
#
# REGRESSION CLASS: a profile's model_order is BOTH the chat cascade AND the
# source of every enrolled key's allowed_models, so it legitimately carries
# embedding/reranker pools (the seeder's default profile ships the embedding
# model in it). Before this, ``auto`` walked the whole list, could resolve
# onto an embedding pool, and the chat path then refused the request
# ("is a embedding endpoint, not chat") instead of falling through to the next
# chat model with headroom.


@pytest.fixture
def chat_and_embed_router(monkeypatch: pytest.MonkeyPatch):
    """llm-1 serves CHAT, llm-2 serves EMBEDDING — one registry, two types."""
    from pcai_llm.routing.router import Endpoint, Router

    r = Router()
    r.set_endpoints("llm-1", [Endpoint(id="e1", model_name="llm-1", base_url="http://a/v1", model_type="chat")])
    r.set_endpoints("llm-2", [Endpoint(id="e2", model_name="llm-2", base_url="http://b/v1", model_type="embedding")])
    monkeypatch.setattr(cascade_mod, "model_router", r)
    return r


async def test_auto_skips_non_chat_ordering_entry(patch_db, chat_and_embed_router) -> None:
    """The embedding pool sits at the HEAD of the ordering (the seeder's
    shape). ``auto`` must skip it and serve the chat pool behind it."""
    patch_db(order=["llm-2", "llm-1"])
    res = await resolve_cascade(uuid.uuid4(), "auto", REGISTERED, [], 100)
    assert res.ok and res.model == "llm-1"
    # the skip is REPORTED, not silent — same discipline as not_registered
    assert res.attempts == [{"model": "llm-2", "status": "not_chat"}]
    # the ACL ordering itself is untouched (enrolled keys still get llm-2)
    assert res.order == ["llm-2", "llm-1"]


async def test_auto_with_only_non_chat_entries_refuses_loudly(patch_db, chat_and_embed_router) -> None:
    """A profile whose whole ordering is non-chat has no chat cascade at all:
    refuse with a reason that names the skipped entries, never a bare 404."""
    patch_db(order=["llm-2"])
    res = await resolve_cascade(uuid.uuid4(), "auto", REGISTERED, [], 100)
    assert not res.ok
    assert "no CHAT models" in res.reason and "llm-2" in res.reason
    assert res.attempts == [{"model": "llm-2", "status": "not_chat"}]


async def test_unknown_pool_is_not_treated_as_non_chat(patch_db, monkeypatch: pytest.MonkeyPatch) -> None:
    """UNKNOWN IS KEPT: a name with no pool in the snapshot keeps today's
    behaviour (the walk's own not_registered attempt, then the caller's pool
    checks). Only a pool we can SEE serving non-chat is skipped."""
    from pcai_llm.routing.router import Router

    monkeypatch.setattr(cascade_mod, "model_router", Router())
    patch_db(order=["ghost", "llm-1"])
    res = await resolve_cascade(uuid.uuid4(), "auto", REGISTERED, [], 100)
    assert res.ok and res.model == "llm-1"
    assert res.attempts == [{"model": "ghost", "status": "not_registered"}]


def test_serves_chat_ignores_endpoint_health(monkeypatch: pytest.MonkeyPatch) -> None:
    """TYPE is not AVAILABILITY: a cooled-down chat endpoint is still a chat
    pool (the walk's own health/breaker handling is a separate axis, and
    folding it in here would change which candidates the budget walk sees)."""
    from pcai_llm.routing.router import Endpoint, Router

    r = Router()
    r.set_endpoints("c", [Endpoint(id="e1", model_name="c", base_url="http://a/v1", model_type="chat")])
    monkeypatch.setattr(cascade_mod, "model_router", r)
    assert cascade_mod.serves_chat("c") is True
    assert cascade_mod.serves_chat("absent") is True  # unknown kept
