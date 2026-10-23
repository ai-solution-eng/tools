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
