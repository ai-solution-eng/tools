"""Never-downgrade sticky routing + route_default_model flag.

Covers the pieces the operator asked for (2026-10-23):

  * apply_never_downgrade is PURE and total: it holds a conversation at or
    above the capability it was already served at, allows escalation, and
    ignores a stale head that left the window/registry/ACL.
  * is_routable_name promotes the legacy 'auto' alias ONLY when the
    route_default_model flag is armed -- arming routing must never silently
    change what an existing client's request does.
  * the routing seam persists a floor ONLY in the branch that actually serves
    (active), never in shadow (which harvests a decision without serving it).
  * stickiness.make_writer / get_sticky_row are fail-open: a DB fault costs
    the floor, never a request.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from pcai_llm.core import model_routing as mr
from pcai_llm.core import stickiness

QWEN = "Qwen3.8-27B"
GLM = "GLM-5.3-Flash"
DS = "Deepseek-V4.1-Flash-1017"
WINDOW = [QWEN, GLM, DS]  # cheap-first: index IS capability
KEY = uuid.UUID("11111111-2222-3333-4444-555555555555")
THREAD = "0123456789abcdef"


# ---------------------------------------------------------------------------
# apply_never_downgrade — the floor
# ---------------------------------------------------------------------------


def test_no_head_leaves_choice_untouched() -> None:
    c = mr.RoutingChoice(QWEN, "prompt_tokens<tier1")
    assert mr.apply_never_downgrade(c, WINDOW, None) is c


def test_same_model_passes_through() -> None:
    c = mr.RoutingChoice(GLM, "heuristic")
    assert mr.apply_never_downgrade(c, WINDOW, GLM).model == GLM


def test_downgrade_is_blocked() -> None:
    """The whole point: a thread served at GLM may not fall back to Qwen."""
    c = mr.RoutingChoice(QWEN, "prompt_tokens<tier1")
    out = mr.apply_never_downgrade(c, WINDOW, GLM)
    assert out.model == GLM
    assert out.reason.startswith("sticky_floor")


def test_escalation_is_allowed() -> None:
    """A 'hi' that becomes a hard refactor MUST be able to climb."""
    c = mr.RoutingChoice(DS, "prompt_tokens>=tier2")
    out = mr.apply_never_downgrade(c, WINDOW, QWEN)
    assert out.model == DS


def test_ceiling_head_holds_against_every_lower_pick() -> None:
    for pick in (QWEN, GLM):
        c = mr.RoutingChoice(pick, "x")
        assert mr.apply_never_downgrade(c, WINDOW, DS).model == DS


def test_stale_head_outside_window_is_ignored() -> None:
    """A head that left the ladder/registry/ACL must not be resurrected."""
    c = mr.RoutingChoice(QWEN, "x")
    assert mr.apply_never_downgrade(c, WINDOW, "Retired-Model").model == QWEN


def test_choice_outside_window_is_ignored_not_raised() -> None:
    """Total by construction: an off-window pick cannot raise."""
    c = mr.RoutingChoice("Not-In-Window", "x")
    assert mr.apply_never_downgrade(c, WINDOW, GLM).model == "Not-In-Window"


def test_empty_candidates_is_inert() -> None:
    c = mr.RoutingChoice(QWEN, "x")
    assert mr.apply_never_downgrade(c, [], GLM).model == QWEN


# ---------------------------------------------------------------------------
# is_routable_name — the route_default_model flag
# ---------------------------------------------------------------------------


def test_pseudo_names_always_route() -> None:
    assert mr.is_routable_name("routing", False)
    assert mr.is_routable_name("routing__" + GLM, False)
    assert mr.is_routable_name("routing", True)


def test_auto_is_opt_in_only() -> None:
    """THE flag's contract: auto keeps its cascade meaning until armed."""
    assert mr.is_routable_name("auto", False) is False
    assert mr.is_routable_name("auto", True) is True


def test_real_models_never_route() -> None:
    for name in (GLM, DS, QWEN):
        assert mr.is_routable_name(name, True) is False


def test_demand_aware_alias_is_not_hijacked() -> None:
    """auto-demand-aware has its own load-aware path; routing must not take it."""
    assert mr.is_routable_name("auto-demand-aware", True) is False


# ---------------------------------------------------------------------------
# the routing seam: persist ONLY when serving
# ---------------------------------------------------------------------------


class _Key:
    def __init__(self) -> None:
        self.user_id = KEY
        self.allowed_models: list[str] = []


async def _run_resolve(monkeypatch, *, policy: str, body_model: str, writes: list):
    """Drive resolve_routed_model with the profile policy pinned."""

    async def _fake_policy(user_id):
        return policy, QWEN

    monkeypatch.setattr(mr, "profile_routing_config", _fake_policy)
    return await mr.resolve_routed_model(
        _Key(),
        {"model": body_model, "messages": [{"role": "user", "content": "hi"}]},
        body_model,
        {QWEN, GLM, DS},
        [],
        [DS, GLM, QWEN],
        available=None,
        decide=None,
        sticky_floor=QWEN,
        sticky_write=writes.append,
    )


@pytest.mark.asyncio
async def test_active_persists_the_served_model(monkeypatch) -> None:
    writes: list[str] = []
    model, ctx = await _run_resolve(monkeypatch, policy="active", body_model="routing", writes=writes)
    assert ctx.policy == "active"
    assert writes == [model], "the SERVED model must be persisted as the new floor"


@pytest.mark.asyncio
async def test_shadow_persists_nothing(monkeypatch) -> None:
    """Shadow harvests a decision without serving it, so there is no floor to write."""
    writes: list[str] = []
    _, ctx = await _run_resolve(monkeypatch, policy="shadow", body_model="routing", writes=writes)
    assert ctx.policy == "shadow"
    assert writes == []


@pytest.mark.asyncio
async def test_floor_holds_in_active_mode(monkeypatch) -> None:
    """A trivial 'hi' would heuristically pick the floor; the floor is GLM here."""
    writes: list[str] = []
    model, _ = await _run_resolve(monkeypatch, policy="active", body_model="routing", writes=writes)
    assert model == QWEN  # heuristic tier1 pick == the floor passed in
    # Now prove a floor ABOVE the heuristic pick is honoured:
    writes2: list[str] = []
    m2, _ = await mr.resolve_routed_model(
        _Key(),
        {"model": "routing", "messages": [{"role": "user", "content": "hi"}]},
        "routing",
        {QWEN, GLM, DS},
        [],
        [DS, GLM, QWEN],
        available=None,
        decide=None,
        sticky_floor=GLM,
        sticky_write=writes2.append,
    )
    assert m2 == GLM, "a conversation served at GLM may not drop to Qwen"
    assert writes2 == [GLM]


# ---------------------------------------------------------------------------
# stickiness helpers — fail-open
# ---------------------------------------------------------------------------


def test_make_writer_none_without_thread() -> None:
    assert stickiness.make_writer(KEY, None) is None


@pytest.mark.asyncio
async def test_make_writer_persists(monkeypatch) -> None:
    seen: list[tuple] = []

    async def _fake_set(k, t, m):
        seen.append((k, t, m))

    monkeypatch.setattr(stickiness, "set_sticky", _fake_set)
    w = stickiness.make_writer(KEY, THREAD)
    assert w is not None
    w(GLM)
    await asyncio.sleep(0)  # let the scheduled task run
    assert seen == [(KEY, THREAD, GLM)]


@pytest.mark.asyncio
async def test_get_sticky_row_never_raises(monkeypatch) -> None:
    async def _boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(stickiness.db, "fetchrow", _boom)
    assert await stickiness.get_sticky_row(KEY, THREAD) is None


@pytest.mark.asyncio
async def test_get_sticky_row_returns_model_and_age(monkeypatch) -> None:
    async def _row(sql, *a):
        return {"model": GLM, "age_s": 12.5}

    monkeypatch.setattr(stickiness.db, "fetchrow", _row)
    assert await stickiness.get_sticky_row(KEY, THREAD) == (GLM, 12.5)


# ---------------------------------------------------------------------------
# config defaults
# ---------------------------------------------------------------------------


def test_new_keys_default_to_off() -> None:
    from pcai_llm.core.routing_config import DEFAULTS

    assert DEFAULTS["sticky_routing"] is False
    assert DEFAULTS["route_default_model"] is False
    assert DEFAULTS["sticky_routing_idle_reset_s"] == 1800


# ---------------------------------------------------------------------------
# env <-> DB layering (the helm wiring is only real if something reads it)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_env_default_reaches_config_and_db_still_wins(monkeypatch) -> None:
    """The env is the deployment DEFAULT, the DB row the OPERATOR override.

    Regression: the three new knobs were first wired into helm + Settings only,
    where nothing read them -- _config() merged DEFAULTS <- DB, so a helm value
    was silently inert. This pins the middle layer.
    """
    from pcai_llm.config import settings
    from pcai_llm.core import routing_config as rc

    class _Empty:
        async def fetch(self, sql, *a):
            return []

    monkeypatch.setattr(rc, "db", _Empty())
    monkeypatch.setattr(settings, "sticky_routing", True)
    monkeypatch.setattr(settings, "route_default_model", True)
    await rc.invalidate()
    cfg = await rc.get_config()
    assert cfg["sticky_routing"] is True, "env must act as the deployment default"
    assert cfg["route_default_model"] is True

    class _Override:
        async def fetch(self, sql, *a):
            return [{"key": "sticky_routing", "value": "b:false"}]

    monkeypatch.setattr(rc, "db", _Override())
    await rc.invalidate()
    assert (await rc.get_config())["sticky_routing"] is False, "DB must beat env"
    await rc.invalidate()


# ---------------------------------------------------------------------------
# CROSS-VALIDATION REGRESSIONS (GLM review, 2026-10-23)
# Each of these FAILED before the fix; they exist so the bug cannot come back.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fresh_thread_bootstraps_a_floor(monkeypatch) -> None:
    """BUG 1: the persist gate must be the WRITER, not a pre-existing floor.

    Before the fix, sticky_write was only invoked when sticky_floor was already
    non-None -- so a brand-new conversation never wrote a row, and the next turn
    again had no floor. The feature could never engage on real traffic.
    """

    async def _fake_policy(user_id):
        return "active", QWEN

    monkeypatch.setattr(mr, "profile_routing_config", _fake_policy)
    writes: list[str] = []
    model, ctx = await mr.resolve_routed_model(
        _Key(),
        {"model": "routing", "messages": [{"role": "user", "content": "hi"}]},
        "routing",
        {QWEN, GLM, DS},
        [],
        [DS, GLM, QWEN],
        available=None,
        decide=None,
        sticky_floor=None,  # <-- fresh conversation
        sticky_write=writes.append,
    )
    assert ctx.policy == "active"
    assert writes == [model], "a fresh thread MUST persist its first floor"


@pytest.mark.asyncio
async def test_auto_does_not_route_unless_armed(monkeypatch) -> None:
    """C3: 'auto' keeps its documented cascade meaning by default."""

    async def _fake_policy(user_id):
        return "active", QWEN

    monkeypatch.setattr(mr, "profile_routing_config", _fake_policy)
    body = {"model": "auto", "messages": [{"role": "user", "content": "x" * 40000}]}
    model, ctx = await mr.resolve_routed_model(
        _Key(),
        body,
        "auto",
        {QWEN, GLM, DS},
        [],
        [DS, GLM, QWEN],
        available=None,
        decide=None,
        route_default=False,
    )
    assert model == "auto", "unarmed auto must ride the cascade unchanged"
    assert ctx.reason == "direct"


@pytest.mark.asyncio
async def test_auto_routes_when_armed(monkeypatch) -> None:
    """C3: arming route_default_model must actually engage the router."""

    async def _fake_policy(user_id):
        return "active", QWEN

    monkeypatch.setattr(mr, "profile_routing_config", _fake_policy)
    body = {"model": "auto", "messages": [{"role": "user", "content": "x" * 40000}]}
    model, ctx = await mr.resolve_routed_model(
        _Key(),
        body,
        "auto",
        {QWEN, GLM, DS},
        [],
        [DS, GLM, QWEN],
        available=None,
        decide=None,
        route_default=True,
    )
    assert model == DS, "a tier2-sized prompt must escalate to the ceiling"
    assert ctx.reason != "direct", "armed auto must produce a real routing decision"


def test_window_without_a_floor_offers_the_whole_ladder() -> None:
    """The decision window deliberately DIFFERS from the heuristic's pick.

    With no routing_min_model, pick_serving_model pins to the ceiling (no
    downgrade) while _window offers the full ladder, so a configured decision
    model may pick any of it. Cross-validation flagged the two as inconsistent;
    it is a real design divergence but PRE-EXISTING and pinned by
    tests/test_decision_routing.py, so harmonising it here would silently change
    which models a decision model may pick. This test pins the intent.
    """
    window = mr._window([QWEN, GLM, DS], DS, "")
    assert window == [QWEN, GLM, DS], "no floor -> full ladder for the decision model"

    # With a floor set, the window starts there (the capability-order invariant
    # the sticky floor depends on).
    assert mr._window([QWEN, GLM, DS], DS, GLM) == [GLM, DS]


def test_pending_task_set_is_bounded() -> None:
    """BUG 3: _PENDING_MAX was declared but never enforced."""
    import asyncio as _aio

    async def _drive() -> None:
        async def _noop(*a, **k):
            return None

        stickiness.set_sticky = _noop  # type: ignore[assignment]
        stickiness._PENDING.clear()
        w = stickiness.make_writer(KEY, THREAD)
        assert w is not None
        for _ in range(stickiness._PENDING_MAX + 50):
            w(GLM)
        await _aio.sleep(0)
        assert len(stickiness._PENDING) <= stickiness._PENDING_MAX

    _aio.run(_drive())


# ---------------------------------------------------------------------------
# WIRING GUARD (GLM review round 2, 2026-10-23)
# A unit test that supplies a kwarg directly cannot catch the proxy FORGETTING
# to forward it -- which is exactly how the C3 fix shipped as dead code. This
# asserts on the call site's source, the same way tests/test_admin_ui.py pins
# UI contracts.
# ---------------------------------------------------------------------------


def test_proxy_forwards_route_default_to_the_router() -> None:
    """route_default_model is inert unless routes_proxy forwards it."""
    import inspect

    from pcai_llm.api import routes_proxy

    src = inspect.getsource(routes_proxy)
    assert "route_default=_rdef," in src, (
        "resolve_routed_model must receive route_default -- without it the "
        "route_default_model flag is dead code and 'auto' never routes"
    )
    # and the flag must actually be READ from the routing config
    assert "route_default_model" in src


def test_proxy_forwards_sticky_arguments() -> None:
    """The floor/writer seam must reach the router too."""
    import inspect

    from pcai_llm.api import routes_proxy

    src = inspect.getsource(routes_proxy)
    assert "sticky_floor=_sticky_floor," in src
    assert "sticky_write=_sticky_write," in src


# ---------------------------------------------------------------------------
# CROSS-FEATURE NAMESPACING (operator question, 2026-10-23)
# model_stickiness is shared by the cascade's load-aware HEAD and routing's
# never-downgrade FLOOR, and both key on the same conversation fingerprint.
# Without a namespace, a cascade head silently became routing's floor.
# ---------------------------------------------------------------------------


def test_routing_and_cascade_keys_never_collide() -> None:
    """Same conversation -> two DIFFERENT rows."""
    conv = "abc123def456abcd"
    routing_key = stickiness.scoped_thread_key(conv, stickiness.SCOPE_ROUTING)
    cascade_key = conv  # the cascade path uses the bare fingerprint
    assert routing_key != cascade_key
    assert routing_key == stickiness.SCOPE_ROUTING + conv


def test_scoped_thread_key_preserves_none() -> None:
    """No fingerprint -> no stickiness, for both scopes."""
    assert stickiness.scoped_thread_key(None, stickiness.SCOPE_ROUTING) is None
    assert stickiness.scoped_thread_key("", stickiness.SCOPE_ROUTING) is None


def test_cascade_head_cannot_become_a_routing_floor() -> None:
    """The regression: a GLM cascade head must NOT hold a routed turn at GLM.

    Reproduces the pre-fix leak: both features used the raw fingerprint, so the
    cascade's stored head was read back as routing's capability floor and every
    later routed turn was pinned to it regardless of the request.
    """
    conv = "sameconv12345678"
    store: dict[str, str] = {conv: GLM}  # cascade wrote its head at the raw key
    routing_key = stickiness.scoped_thread_key(conv, stickiness.SCOPE_ROUTING)
    assert store.get(routing_key) is None, "routing must not see the cascade head"

    # With no floor, a trivial request routes to the cheap model as normal.
    out = mr.apply_never_downgrade(
        mr.RoutingChoice(QWEN, "prompt_tokens<tier1"), [QWEN, GLM, DS], store.get(routing_key)
    )
    assert out.model == QWEN, "routing must not be pinned by the cascade's head"


# ---------------------------------------------------------------------------
# PANEL CONTROLS (operator ask, 2026-10-23)
# The three settings had no UI at all: RoutingConfigPatch omitted them, so they
# could only be set via helm or a direct DB write. These pin the wiring end to
# end -- HTML control, load path, save path, and the PATCH contract.
# ---------------------------------------------------------------------------


def _admin_html() -> str:
    from pathlib import Path

    return (Path(__file__).resolve().parents[1] / "src/pcai_llm/ui/admin.html").read_text()


def test_panel_renders_the_three_controls() -> None:
    s = _admin_html()
    assert 'id="rt-routedefault"' in s, "route_default_model needs a checkbox"
    assert 'id="rt-sticky"' in s, "sticky_routing needs a checkbox"
    assert 'id="rt-stickyidle"' in s, "idle reset needs a number input"


def test_panel_loads_saved_values_into_the_controls() -> None:
    s = _admin_html()
    assert "$('rt-routedefault').checked = c.route_default_model === true;" in s
    assert "$('rt-sticky').checked = c.sticky_routing === true;" in s
    assert "$('rt-stickyidle').value = c.sticky_routing_idle_reset_s" in s


def test_panel_saves_the_three_controls() -> None:
    s = _admin_html()
    assert "route_default_model: $('rt-routedefault').checked," in s
    assert "sticky_routing: $('rt-sticky').checked," in s
    assert "sticky_routing_idle_reset_s: $('rt-stickyidle').value" in s


def test_patch_model_accepts_the_three_keys() -> None:
    """A control whose key the PATCH model rejects would 400 on save."""
    from pcai_llm.api.routes_admin import RoutingConfigPatch

    p = RoutingConfigPatch(route_default_model=True, sticky_routing=True, sticky_routing_idle_reset_s=900)
    assert p.model_dump(exclude_unset=True) == {
        "route_default_model": True,
        "sticky_routing": True,
        "sticky_routing_idle_reset_s": 900,
    }


def test_patch_model_bounds_the_idle_reset() -> None:
    from pydantic import ValidationError

    from pcai_llm.api.routes_admin import RoutingConfigPatch

    for bad in (-1, 86401):
        with pytest.raises(ValidationError):
            RoutingConfigPatch(sticky_routing_idle_reset_s=bad)


def test_patch_model_does_not_clear_omitted_keys() -> None:
    """exclude_unset discipline: an omitted key must not become a clear."""
    from pcai_llm.api.routes_admin import RoutingConfigPatch

    assert RoutingConfigPatch().model_dump(exclude_unset=True) == {}
    # ...but an explicit False IS a real choice and must survive.
    assert RoutingConfigPatch(sticky_routing=False).model_dump(exclude_unset=True) == {"sticky_routing": False}


# ---------------------------------------------------------------------------
# ALLOWLIST AS ELIGIBILITY (operator ask, 2026-10-23)
# The console checklist claimed to bound what the router could pick while the
# request path never read it. The ladder is CAPABILITY ORDER; the allowlist is
# ELIGIBILITY. These pin both jobs.
# ---------------------------------------------------------------------------


def test_expand_without_allowlist_is_the_ladder() -> None:
    assert mr.expand_candidates([GLM, DS], None) == [GLM, DS]
    assert mr.expand_candidates([GLM, DS], []) == [GLM, DS]


def test_allowlist_makes_an_off_ladder_model_reachable() -> None:
    """Qwen is not in model_order; ticking it must put it in the window."""
    out = mr.expand_candidates([GLM, DS], [QWEN, GLM, DS])
    assert QWEN in out
    # off-ladder models go to the CHEAP end (no declared capability position)
    assert out[0] == QWEN


def test_allowlist_bounds_and_does_not_merely_widen() -> None:
    """A model on the ladder but NOT ticked must be excluded."""
    assert mr.expand_candidates([GLM, DS], [GLM]) == [GLM]
    assert mr.expand_candidates([GLM, DS], [QWEN]) == [QWEN]


def test_allowlist_preserves_ladder_capability_order() -> None:
    """Ladder members keep their relative order; additions sort by name."""
    out = mr.expand_candidates([GLM, DS], [GLM, DS, QWEN])
    assert out == [QWEN, GLM, DS]
    # and the ladder order is intact among themselves
    assert out.index(GLM) < out.index(DS)


def test_availability_filters_additions_only() -> None:
    out = mr.expand_candidates([GLM, DS], [QWEN, GLM, DS], available=lambda m: m != QWEN)
    assert QWEN not in out
    assert out == [GLM, DS]


def test_stale_allowlist_never_empties_the_candidate_set() -> None:
    """A panel naming only vanished models must not 404 every request."""
    assert mr.expand_candidates([GLM, DS], ["Retired-Model"]) == ["Retired-Model"]


def test_off_ladder_ceiling_is_offered() -> None:
    """routing__Qwen3.8-27B could not reach Qwen before this fix."""
    assert mr._window([GLM, DS], QWEN, "") == [GLM, DS, QWEN]
    # an on-ladder ceiling is unchanged
    assert mr._window([GLM, DS], DS, "") == [GLM, DS]


@pytest.mark.asyncio
async def test_router_offers_the_allowlisted_off_ladder_model(monkeypatch) -> None:
    """End to end through resolve_routed_model: the decide() seam sees Qwen."""

    async def _fake_policy(user_id):
        return "active", ""

    monkeypatch.setattr(mr, "profile_routing_config", _fake_policy)
    seen: list[list[str]] = []

    async def decide(candidates, body, score):
        seen.append(list(candidates))
        return mr.DecisionOutcome(ok=True, choice=QWEN, confidence=0.9, reason="decision")

    model, ctx = await mr.resolve_routed_model(
        _Key(),
        {"model": "routing", "messages": [{"role": "user", "content": "hi"}]},
        "routing",
        {QWEN, GLM, DS},
        [],
        [DS, GLM],  # ladder does NOT contain Qwen
        available=None,
        decide=decide,
        routable_models=[QWEN, GLM, DS],
    )
    assert seen and QWEN in seen[0], "the decision model must be offered Qwen"
    assert model == QWEN, "and its pick must be served"
    assert ctx.policy == "active"


def test_ledger_stamp_records_the_full_distribution() -> None:
    """routing_reason carries only the winner; the distribution explains it."""
    route = mr.RouteDecision(
        policy="shadow",
        engaged=True,
        ceiling=DS,
        would=GLM,
        reason="decision:0.62",
        stamp=True,
        probabilities={GLM: 0.62, DS: 0.38},
    )
    stamp = mr.ledger_stamp(route, actual_model=DS)
    assert stamp["routing_probabilities"] == '{"Deepseek-V4.1-Flash-1017":0.38,"GLM-5.3-Flash":0.62}'


def test_ledger_stamp_probabilities_empty_when_no_decision() -> None:
    route = mr.RouteDecision(
        policy="shadow", engaged=True, ceiling=DS, would=DS, reason="prompt_tokens<tier1", stamp=True
    )
    assert mr.ledger_stamp(route, actual_model=DS)["routing_probabilities"] == ""


def test_schema_has_the_probabilities_column() -> None:
    from pcai_llm.db.schema import SCHEMA_SQL

    assert "routing_probabilities" in SCHEMA_SQL
