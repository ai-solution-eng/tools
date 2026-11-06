"""Cross-replica registry convergence (2026-10-30).

WHY THIS FILE EXISTS — the incident it pins: core.registry snapshot is
PROCESS-LOCAL and refresh() used to run only at boot and after a mutation the
serving process handled. On an HPA'd deployment (minReplicas 2) a server
registered on one replica was therefore invisible to its sibling until that pod
restarted: the console's "test" button returned

    404 — MCP server 'skills-mcp' is not in the enabled registry

on roughly half the clicks (and the row itself rendered fine from the OTHER
replica's cache), while the relay answered "unknown server" for the same server
on the stale pod. Same failure mode as the 2026-10-29 context-window incident.

The fix is a DB watermark poll (row counts + max(updated_at), maintained by a
trigger so out-of-band SQL edits count too). These tests pin its contract:

  * a moved watermark refreshes exactly once, an unchanged one never;
  * a refresh that FAILS must not advance the watermark (retry next tick);
  * an unreadable / unknown-shape watermark is a SKIP, never an error;
  * the admin fast path is rate-limited, disableable, and fail-open;
  * both console read routes consult it — including the 404 path itself.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from typing import Any

import pytest
from fastapi import HTTPException

from pcai_llm.api import routes_admin
from pcai_llm.config import settings
from pcai_llm.core import registry

_TS_BOOT = dt.datetime(2026, 10, 30, 16, 26, tzinfo=dt.UTC)
_TS_REGISTER = dt.datetime(2026, 10, 30, 19, 57, tzinfo=dt.UTC)

# (mcp rows, newest mcp write, endpoint rows, newest endpoint write)
W_BOOT = (10, _TS_BOOT, 3, _TS_BOOT)
W_AFTER_REGISTRATION = (11, _TS_REGISTER, 3, _TS_BOOT)


def _const(value: Any):
    """Async callable that always returns the given value."""

    async def _call() -> Any:
        return value

    return _call


def _seq(*values: Any):
    """Async callable yielding each value in turn, then repeating the last."""
    pending = list(values)

    async def _call() -> Any:
        return pending.pop(0) if len(pending) > 1 else pending[0]

    return _call


@pytest.fixture(autouse=True)
def _clean_watch_state(monkeypatch: pytest.MonkeyPatch):
    """Every test starts from a process that has never polled."""
    monkeypatch.setattr(registry, "_last_watermark", None)
    monkeypatch.setattr(registry, "_last_check", 0.0)
    monkeypatch.setattr(registry, "_refreshing", False)
    monkeypatch.setattr(registry, "_watch_failures", 0)
    yield


# ---------------------------------------------------------------------------
# the watermark itself
# ---------------------------------------------------------------------------


async def test_watermark_is_none_when_the_db_errors(monkeypatch: pytest.MonkeyPatch):
    async def _boom(sql: str, *args: Any):
        raise RuntimeError("db down")

    monkeypatch.setattr(registry.db, "fetchrow", _boom)
    assert await registry._watermark() is None


async def test_watermark_is_none_for_an_unknown_row_shape(monkeypatch: pytest.MonkeyPatch):
    """A pre-migration schema (or a test double) must degrade to "no opinion".

    Returning None here is what keeps an older database from turning every
    admin read into a 500: the caller simply keeps its current snapshot.
    """

    async def _odd(sql: str, *args: Any):
        return {"something": 1}

    monkeypatch.setattr(registry.db, "fetchrow", _odd)
    assert await registry._watermark() is None


async def test_watermark_reads_the_four_fields(monkeypatch: pytest.MonkeyPatch):
    async def _row(sql: str, *args: Any):
        assert "mcp_servers" in sql and "model_endpoints" in sql
        return {"mcp_count": 11, "mcp_ts": _TS_REGISTER, "ep_count": 3, "ep_ts": _TS_BOOT}

    monkeypatch.setattr(registry.db, "fetchrow", _row)
    assert await registry._watermark() == W_AFTER_REGISTRATION


# ---------------------------------------------------------------------------
# refresh_if_changed — the convergence primitive
# ---------------------------------------------------------------------------


async def test_stale_replica_learns_a_new_server_on_the_next_tick(monkeypatch: pytest.MonkeyPatch):
    """THE incident: replica B booted before skills-mcp existed.

    Its cache says 10 servers; the DB watermark moves when replica A accepts
    the registration. One tick must be enough for B to serve the new server —
    no restart, no manual refresh, no operator action.
    """
    monkeypatch.setattr(
        registry,
        "_mcp_servers_cache",
        [{"name": "k8s-ops", "url": "http://k8s-ops/mcp", "headers": {}}],
    )

    async def _refresh() -> None:
        registry._mcp_servers_cache = [
            {"name": "k8s-ops", "url": "http://k8s-ops/mcp", "headers": {}},
            {"name": "skills-mcp", "url": "http://skills-mcp/mcp", "headers": {}},
        ]

    monkeypatch.setattr(registry, "refresh", _refresh)
    monkeypatch.setattr(registry, "_watermark", _const(W_AFTER_REGISTRATION))
    # This replica snapshot was taken at the pre-registration watermark.
    monkeypatch.setattr(registry, "_last_watermark", W_BOOT)

    assert [s["name"] for s in await registry.mcp_servers()] == ["k8s-ops"]

    assert await registry.refresh_if_changed() is True
    assert [s["name"] for s in await registry.mcp_servers()] == ["k8s-ops", "skills-mcp"]

    # Converged: a second tick is a no-op, so the steady state costs one
    # query, not a refresh.
    assert await registry.refresh_if_changed() is False


async def test_unchanged_watermark_never_refreshes(monkeypatch: pytest.MonkeyPatch):
    calls: list[int] = []

    async def _refresh() -> None:
        calls.append(1)

    monkeypatch.setattr(registry, "refresh", _refresh)
    monkeypatch.setattr(registry, "_watermark", _const(W_BOOT))
    monkeypatch.setattr(registry, "_last_watermark", W_BOOT)

    assert await registry.refresh_if_changed() is False
    assert calls == []


async def test_first_ever_tick_refreshes_once(monkeypatch: pytest.MonkeyPatch):
    """No watermark recorded yet (fresh process) => one refresh, then quiet."""
    calls: list[int] = []

    async def _refresh() -> None:
        calls.append(1)

    monkeypatch.setattr(registry, "refresh", _refresh)
    monkeypatch.setattr(registry, "_watermark", _const(W_BOOT))

    assert await registry.refresh_if_changed() is True
    assert await registry.refresh_if_changed() is False
    assert calls == [1]


async def test_failed_refresh_does_not_advance_the_watermark(monkeypatch: pytest.MonkeyPatch):
    """A DB blip during refresh must RETRY, not silently accept a stale snapshot."""
    calls: list[int] = []

    async def _boom() -> None:
        calls.append(1)
        raise RuntimeError("db down")

    monkeypatch.setattr(registry, "refresh", _boom)
    monkeypatch.setattr(registry, "_watermark", _const(W_AFTER_REGISTRATION))

    with pytest.raises(RuntimeError):
        await registry.refresh_if_changed()

    assert registry._last_watermark is None  # not advanced => next tick retries
    assert registry._refreshing is False  # the guard is released in a finally
    with pytest.raises(RuntimeError):
        await registry.refresh_if_changed()
    assert calls == [1, 1]


async def test_watermark_is_read_before_the_refresh(monkeypatch: pytest.MonkeyPatch):
    """The stored watermark must PREDATE the snapshot it describes.

    Storing the post-refresh value would swallow a write that landed
    mid-refresh (the next tick would compare against a watermark already
    covering a change the snapshot never saw). One redundant refresh is the
    acceptable price.
    """
    order: list[str] = []

    async def _wm():
        order.append("watermark")
        return W_AFTER_REGISTRATION

    async def _refresh() -> None:
        order.append("refresh")

    monkeypatch.setattr(registry, "_watermark", _wm)
    monkeypatch.setattr(registry, "refresh", _refresh)

    assert await registry.refresh_if_changed() is True
    assert order == ["watermark", "refresh"]


async def test_two_concurrent_callers_refresh_exactly_once(monkeypatch: pytest.MonkeyPatch):
    """The in-flight flag is taken BEFORE the first await.

    Regression guard for the await hole: if the flag were set after the
    watermark read, both callers would pass the check and run two concurrent
    whole-snapshot swaps — and the OLDER swap can land last, leaving a stale
    snapshot behind a watermark that already moved past it.
    """
    calls: list[int] = []

    async def _wm():
        await asyncio.sleep(0)  # yield control: the sibling task runs here
        return W_AFTER_REGISTRATION

    async def _refresh() -> None:
        calls.append(1)

    monkeypatch.setattr(registry, "_watermark", _wm)
    monkeypatch.setattr(registry, "refresh", _refresh)

    results = await asyncio.gather(
        registry.refresh_if_changed(),
        registry.refresh_if_changed(),
    )

    assert calls == [1]  # exactly one swap
    assert sorted(results) == [False, True]  # the loser skipped, did not queue


async def test_concurrent_refresh_is_skipped(monkeypatch: pytest.MonkeyPatch):
    """A refresh already in flight must not be stacked by the watcher."""
    monkeypatch.setattr(registry, "_refreshing", True)
    called: list[int] = []

    async def _refresh() -> None:
        called.append(1)

    monkeypatch.setattr(registry, "refresh", _refresh)
    monkeypatch.setattr(registry, "_watermark", _const(W_AFTER_REGISTRATION))

    assert await registry.refresh_if_changed() is False
    assert called == []


# ---------------------------------------------------------------------------
# ensure_fresh — the admin console bounded fast path
# ---------------------------------------------------------------------------


async def test_ensure_fresh_is_rate_limited(monkeypatch: pytest.MonkeyPatch):
    checks: list[int] = []

    async def _wm():
        checks.append(1)
        return W_BOOT

    async def _refresh() -> None:
        raise AssertionError("no change => no refresh")

    monkeypatch.setattr(registry, "_watermark", _wm)
    monkeypatch.setattr(registry, "refresh", _refresh)
    monkeypatch.setattr(settings, "registry_admin_fresh_s", 60.0)

    await registry.ensure_fresh()
    await registry.ensure_fresh()
    await registry.ensure_fresh()

    assert checks == [1]  # one watermark query for the burst


async def test_ensure_fresh_refreshes_when_the_watermark_moved(monkeypatch: pytest.MonkeyPatch):
    calls: list[int] = []

    async def _refresh() -> None:
        calls.append(1)

    monkeypatch.setattr(registry, "refresh", _refresh)
    monkeypatch.setattr(registry, "_watermark", _const(W_AFTER_REGISTRATION))
    monkeypatch.setattr(settings, "registry_admin_fresh_s", 1.0)

    await registry.ensure_fresh()

    assert calls == [1]
    assert registry._last_watermark == W_AFTER_REGISTRATION


async def test_ensure_fresh_is_fail_open(monkeypatch: pytest.MonkeyPatch):
    """Freshness is an optimization: a broken watermark must never 500 an admin GET."""

    async def _boom() -> None:
        raise RuntimeError("db down")

    monkeypatch.setattr(registry, "refresh", _boom)
    monkeypatch.setattr(registry, "_watermark", _const(W_AFTER_REGISTRATION))

    await registry.ensure_fresh()  # must not raise


async def test_ensure_fresh_disabled_at_zero(monkeypatch: pytest.MonkeyPatch):
    checks: list[int] = []

    async def _wm():
        checks.append(1)
        return W_BOOT

    monkeypatch.setattr(registry, "_watermark", _wm)
    monkeypatch.setattr(settings, "registry_admin_fresh_s", 0.0)

    await registry.ensure_fresh()
    assert checks == []


# ---------------------------------------------------------------------------
# the console routes that produced the 404
# ---------------------------------------------------------------------------


async def test_server_list_consults_the_watermark(monkeypatch: pytest.MonkeyPatch):
    seen: list[int] = []

    async def _ensure() -> None:
        seen.append(1)

    async def _servers() -> list[dict[str, Any]]:
        return []

    monkeypatch.setattr(registry, "ensure_fresh", _ensure)
    monkeypatch.setattr(registry, "mcp_servers", _servers)

    assert await routes_admin.list_mcp_servers(None) == []
    assert seen == [1]


async def test_test_button_path_consults_the_watermark_before_404(monkeypatch: pytest.MonkeyPatch):
    """The exact reported symptom: the test button 404 must be decided on a
    FRESH snapshot, not on whatever this pod happened to boot with."""
    seen: list[int] = []

    async def _ensure() -> None:
        seen.append(1)

    async def _servers() -> list[dict[str, Any]]:
        return []

    monkeypatch.setattr(registry, "ensure_fresh", _ensure)
    monkeypatch.setattr(registry, "mcp_servers", _servers)

    with pytest.raises(HTTPException) as excinfo:
        await routes_admin.list_server_tools("skills-mcp", None)

    assert excinfo.value.status_code == 404
    assert seen == [1]


# ---------------------------------------------------------------------------
# watcher lifecycle
# ---------------------------------------------------------------------------


async def test_watcher_is_disabled_at_zero(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "registry_watch_s", 0.0)
    await registry.start_watcher()
    assert registry._watch_task is None


async def test_watcher_start_is_idempotent_and_stops(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "registry_watch_s", 3600.0)

    await registry.start_watcher()
    first = registry._watch_task
    assert first is not None

    await registry.start_watcher()  # no second task
    assert registry._watch_task is first

    await registry.stop_watcher()
    assert registry._watch_task is None
    assert first.cancelled() or first.done()

    await registry.stop_watcher()  # idempotent
