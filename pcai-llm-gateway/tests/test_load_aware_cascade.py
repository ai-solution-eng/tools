"""Load-aware cascade head tests (the `auto-demand-aware` entry, off-peak-driven).

The design brief's test matrix, one section per row:

1. signal-off invariance — feature off / no per-model eval / no thread key:
   the resolution is byte-identical to the pre-feature cascade (the critical
   backwards-compat gate), and nothing touches the stickiness store.
2. sticky write on the first demand-aware request with a thread key; sticky
   HIT on the second (even when the off-peak signal has since flipped).
3. 429-on-head: the admission walker (`_admit_with_failover`) still walks —
   a rate-429 head does not fail the turn, and the walk order is the
   reordered one.
4. sticky model removed from the registry / the key's ACL -> the record is
   dropped and a fresh pick is made.
5. pinned request -> no sticky read and no sticky write.
6. no user-message seed -> no sticky machinery at all (canonical.thread_key
   returns None; resolve_cascade never consults the store).
7. off-peak feature disabled (no per-model evals) -> default head, and the
   counter records reason=default.
8. rank/order semantics: ties preserve the profile order; an un-sampled
   model is conservative; a not-active model yields to an active one.
9. HEALTH GATE (defect fix, G2 live 2026-10-04): the ranking only ever sees
   candidates whose pool has an available endpoint. Idle-because-BROKEN must
   never outrank loaded-but-healthy (the live shape: Qwen3.8-27B at 0.00%
   util while 400-ing ~86% of calls ranked 0 and took every new
   conversation); an unhealthy sticky head is dropped and re-picked; with NO
   healthy candidate the list is returned unchanged and the store is
   untouched; the inert paths (feature off / no thread key) never even probe
   breaker state.
10. ALIAS SPLIT (operator decision, 2026-10-04): the load-aware head is
   opt-in BY NAME. `auto-demand-aware` engages it; the legacy `auto` never
   does — no sticky read, no sticky write, no pool lookup, no counter tick,
   order unchanged — so enabling the feature cannot silently change what an
   existing client's `auto` request means, and rollback is a name switch.
   Every engaging test above now names DEMAND_AWARE_ALIAS; section 10 pins
   the legacy `auto` inertness, the feature-off fallback, the /v1/models
   advertisement of the pair, and the pinned-name shadowing consequence.

No live Postgres and no network: the module-db monkeypatch pattern from
tests/test_cascade.py, an in-memory model_stickiness, offpeak's own
public model_active() reading states the test builds directly, and a
pool-recording router stand-in for the health seam.
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from typing import Any

import fastapi
import pytest

from pcai_llm.api import routes_proxy
from pcai_llm.config import settings
from pcai_llm.core import budgets as budgets_mod
from pcai_llm.core import cascade as cascade_mod
from pcai_llm.core import offpeak, stickiness
from pcai_llm.core.cascade import DEMAND_AWARE_ALIAS, drain_sticky_writes, load_rank, pick_load_aware, resolve_cascade
from pcai_llm.routing.router import Endpoint, Pool

# The brief's operator framing: GLM first in the profile order, Deepseek the
# capacity escape hatch.
GLM = "GLM-5.3-Flash"
DS = "Deepseek-V4.1-Flash"
# The model from the live defect: idle on the GPU metric while its upstream
# 400'd ~86% of calls, so the off-peak signal ranked it 0 (best).
QWEN = "Qwen3.8-27B"
REGISTERED = {GLM, DS}
ROSTER = {GLM, DS, QWEN}
THREAD = "0123456789abcdef"


def _endpoint(model: str, *, cooling: bool = False) -> Endpoint:
    """One endpoint for the model; cooling=True models a tripped breaker
    (Pool.report sets cooldown_until = now + endpoint_cooldown_s)."""
    ep = Endpoint(id=f"{model}-ep", model_name=model, base_url="http://upstream.invalid/v1")
    if cooling:
        ep.cooldown_until = float("inf")  # never < now: permanently out of healthy()
    return ep


class CascadeFakeRouter:
    """The router seam the health gate reads (``core.cascade.model_router``).

    Records EVERY ``pool()`` lookup so the inertness tests can assert the
    health probe never runs at all (feature off / no thread key — the brief's
    backwards-compat gate), and holds the live pools the gate partitions on.
    Every roster model starts with one healthy endpoint, which is what the
    production wiring guarantees: ``registered`` comes from
    ``model_router.models()``, so a registered model has a pool.
    """

    def __init__(self, models: list[str]) -> None:
        self.pools: dict[str, Pool] = {m: Pool(endpoints=[_endpoint(m)]) for m in models}
        self.lookups: list[str] = []

    def pool(self, name: str) -> Pool | None:
        self.lookups.append(name)
        return self.pools.get(name)

    def cool(self, model: str) -> None:
        """Trip the breaker on every endpoint of `model` — the model is
        registered and up, but cannot serve (the live G2 shape)."""
        self.pools[model] = Pool(endpoints=[_endpoint(model, cooling=True)])

    def unregister(self, model: str) -> None:
        """Drop the model's pool entirely (absent = nothing can serve)."""
        self.pools.pop(model, None)


def _install_router(monkeypatch: pytest.MonkeyPatch) -> CascadeFakeRouter:
    """Point cascade's router seam at a fresh stand-in and hand it back."""
    router = CascadeFakeRouter([GLM, DS, QWEN])
    monkeypatch.setattr(cascade_mod, "model_router", router)
    return router


@pytest.fixture
def health_gate(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Records every ``_pool_servable`` probe — the HEALTH-GATE seam.

    WHY this seam and not ``router.lookups`` (2026-10-29): the inertness tests
    used to observe "no ``pool()`` call at all". The chat-only filter
    (``auto`` must not walk an embedding pool — core.cascade.serves_chat) now
    consults the router once per candidate for its TYPE. That consult cannot
    move the head: it only removes candidates that could never serve chat. The
    invariant these tests actually pin is that the load-aware HEALTH/load
    machinery never runs, so it is observed at its own seam — which stays
    strict, and is where a regression would actually show up.
    """
    calls: list[str] = []

    def _probe(model: str) -> bool:
        calls.append(model)
        return True  # the gate "passes"; the point is that it never runs

    monkeypatch.setattr(cascade_mod, "_pool_servable", _probe)
    return calls


class CascadeFakeDB:
    """The two seams resolve_cascade touches: profiles.model_order and the
    model_stickiness row set (cascade's own db handle)."""

    def __init__(self, order: list[str]) -> None:
        self.order = order
        self.rows: dict[tuple[Any, str], str] = {}
        self.reads: list[tuple[Any, str]] = []
        self.writes: list[tuple[Any, str, str]] = []
        self.drops: list[tuple[Any, str]] = []
        # The health seam rides THIS fixture's return value: every existing
        # test unpacks the (fake, key_id) 2-tuple, so the router stand-in
        # cannot become a third element without touching them all.
        self.router: CascadeFakeRouter = CascadeFakeRouter([GLM, DS, QWEN])

    async def fetchrow(self, sql: str, *a: Any):
        if "profiles" in sql:
            # P3 note: resolve_cascade reads the ordering via the plain
            # profile query; the sticky read (sticky_for_thread removed in
            # the F5 remediation) hits the "FROM model_stickiness" branch
            # below. No LEFT JOIN fusion remains in production code.
            return {"model_order": self.order} if self.order else None
        if "FROM model_stickiness" in sql:
            self.reads.append((a[0], a[1]))
            model = self.rows.get((a[0], a[1]))
            return {"model": model} if model is not None else None
        return None

    async def fetch(self, sql: str, *a: Any):
        return []

    async def execute(self, sql: str, *a: Any):
        if "INSERT INTO model_stickiness" in sql:
            self.writes.append((a[0], a[1], a[2]))
            self.rows[(a[0], a[1])] = a[2]
        elif "DELETE FROM model_stickiness" in sql:
            self.drops.append((a[0], a[1]))
            self.rows.pop((a[0], a[1]), None)
        return "OK"


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch):
    """(fake_db, key_id) with the cascade + budgets + stickiness db handles
    all pointed at one fake, the health seam pointed at the fake's router
    (every roster model healthy), and the feature ON by default (each test
    that cares flips it)."""
    fake = CascadeFakeDB(order=[GLM, DS])
    key_id = uuid.uuid4()
    monkeypatch.setattr(cascade_mod, "db", fake)
    monkeypatch.setattr(budgets_mod, "db", fake)  # check_budgets reads budgets.db
    monkeypatch.setattr(stickiness, "db", fake)
    monkeypatch.setattr(cascade_mod, "model_router", fake.router)
    monkeypatch.setattr(settings, "load_aware_cascade", True)
    return fake, key_id


@pytest.fixture(autouse=True)
def clean_offpeak():
    """Per-model evaluator state is module-global: start and end pristine.

    NOTE: offpeak.reset() deliberately resets the STATES inside _model_evals
    but does not drop the dict entries (they are rebuilt from settings by
    start()), so the fixture clears the dict itself — otherwise a model
    installed here would leak into every later test in the session (it broke
    tests/test_offpeak.py::test_admin_offpeak_snapshot's empty-models assert
    when this file ran first).
    """
    offpeak.reset()
    offpeak._model_evals.clear()
    yield
    offpeak.reset()
    offpeak._model_evals.clear()


def _eval_state(model: str, *, active: bool, sampled: bool = True) -> None:
    """Install a per-model evaluator state for `model` (enabled, as start()
    would leave it). sampled=False models the never-yet-checked case."""
    st = offpeak._State(label=model, enabled=True, active=active, fraction=0.5 if active else 1.0)
    if sampled:
        st.last_value_pct = 10.0 if active else 90.0
    offpeak._model_evals[model] = (f"q-{model}", st)


def _ranks(pairs: list[tuple[str, int]]) -> dict[str, int]:
    return dict(pairs)


# ---------------------------------------------------------------------------
# 1. signal-off invariance (the critical backwards-compat gate)
# ---------------------------------------------------------------------------


async def test_feature_off_is_byte_identical_and_touches_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """load_aware_cascade=False (the fleet default): profile-order head, no
    sticky read, no sticky write — even with a thread key in hand."""
    fake = CascadeFakeDB(order=[GLM, DS])
    monkeypatch.setattr(cascade_mod, "db", fake)
    monkeypatch.setattr(budgets_mod, "db", fake)
    monkeypatch.setattr(stickiness, "db", fake)
    monkeypatch.setattr(settings, "load_aware_cascade", False)
    _eval_state(GLM, active=False)  # GLM loaded: would reorder if engaged
    _eval_state(DS, active=True)

    res = await resolve_cascade(uuid.uuid4(), DEMAND_AWARE_ALIAS, REGISTERED, [], 100, thread_key=THREAD)
    await drain_sticky_writes()  # P3: the sticky write is fire-and-forget — flush before asserting
    assert res.ok and res.model == GLM  # profile head, NOT the load pick
    assert fake.reads == [] and fake.writes == [] and fake.drops == []


async def test_no_per_model_eval_is_inert(env) -> None:
    """Feature ON but NO per-model eval configured (offpeak off / unconfigured):
    the pick has no signal anywhere, so the feature is INERT — profile head,
    NO reorder, NO row written (a row here would pin the conversation to a
    model chosen with no signal, outliving the inertness), and the counter
    records reason=default."""
    fake, key_id = env
    res = await resolve_cascade(key_id, DEMAND_AWARE_ALIAS, REGISTERED, [], 100, thread_key=THREAD)
    await drain_sticky_writes()  # P3: the sticky write is fire-and-forget — flush before asserting
    assert res.ok and res.model == GLM
    assert fake.writes == [] and fake.rows == {}, "an inert pick must not record a head"


async def test_inert_pick_writes_nothing_so_a_later_signal_can_still_choose(env) -> None:
    """The inertness contract (why the no-signal pick must not record a row):
    a conversation that started while no per-model eval existed must NOT be
    pinned — once the operator configures the signal, the NEXT turn picks
    freely. Writing a row on an inert pick would permanently freeze the
    conversation on the profile head."""
    fake, key_id = env
    # Turn 1: feature on, no per-model eval anywhere -> inert, nothing written.
    assert (await resolve_cascade(key_id, DEMAND_AWARE_ALIAS, REGISTERED, [], 100, thread_key=THREAD)).model == GLM
    await drain_sticky_writes()  # P3: flush the detached sticky write before asserting
    assert fake.rows == {}

    # The operator configures the signal: GLM is now under load.
    _eval_state(GLM, active=False)
    _eval_state(DS, active=True)
    assert (await resolve_cascade(key_id, DEMAND_AWARE_ALIAS, REGISTERED, [], 100, thread_key=THREAD)).model == DS
    await drain_sticky_writes()  # P3: flush the detached sticky write before asserting
    assert fake.rows[(key_id, THREAD)] == DS


async def test_thread_key_none_never_consults_stickiness(env) -> None:
    """No user-message seed -> no stickiness possible or attempted (brief
    rule 2): no read, no write, no drop, profile head."""
    fake, key_id = env
    _eval_state(GLM, active=False)
    _eval_state(DS, active=True)
    res = await resolve_cascade(key_id, DEMAND_AWARE_ALIAS, REGISTERED, [], 100, thread_key=None)
    await drain_sticky_writes()  # P3: the sticky write is fire-and-forget — flush before asserting
    assert res.ok and res.model == GLM
    assert fake.reads == [] and fake.writes == [] and fake.drops == []


async def test_existing_suite_signature_still_works(env) -> None:
    """The thread_key parameter is OPTIONAL: every pre-existing call site
    (and test) keeps its exact behavior — profile-order head, no store
    traffic."""
    fake, key_id = env
    res = await resolve_cascade(key_id, DEMAND_AWARE_ALIAS, REGISTERED, [], 100)
    await drain_sticky_writes()  # P3: the sticky write is fire-and-forget — flush before asserting
    assert res.ok and res.model == GLM
    assert fake.reads == [] and fake.writes == []


# ---------------------------------------------------------------------------
# 2. sticky write on turn 1, sticky hit on turn 2
# ---------------------------------------------------------------------------


async def test_first_turn_writes_the_head_and_second_turn_hits_it(env) -> None:
    """GLM is under load, Deepseek is idle -> turn 1 picks Deepseek and
    RECORDS it; turn 2 (even after the signal flips back) stays on
    Deepseek — "if it went to Deepseek, stay with Deepseek"."""
    fake, key_id = env
    _eval_state(GLM, active=False)  # GLM busy
    _eval_state(DS, active=True)  # Deepseek idle

    first = await resolve_cascade(key_id, DEMAND_AWARE_ALIAS, REGISTERED, [], 100, thread_key=THREAD)
    await drain_sticky_writes()  # P3: the sticky write is fire-and-forget — flush before asserting
    assert first.ok and first.model == DS
    assert fake.writes == [(key_id, THREAD, DS)]

    # The signal flips: GLM goes idle, Deepseek goes busy. The conversation
    # must NOT re-roll its head.
    _eval_state(GLM, active=True)
    _eval_state(DS, active=False)
    second = await resolve_cascade(key_id, DEMAND_AWARE_ALIAS, REGISTERED, [], 100, thread_key=THREAD)
    await drain_sticky_writes()  # P3: the sticky write is fire-and-forget — flush before asserting
    assert second.ok and second.model == DS
    assert len(fake.writes) == 1, "a sticky HIT must not rewrite the row"


async def test_sticky_hit_reorders_only_never_widens(env) -> None:
    """The head is a REORDER: the returned candidate set is unchanged (the
    attempts/order contract downstream is untouched)."""
    fake, key_id = env
    fake.rows[(key_id, THREAD)] = DS
    res = await resolve_cascade(key_id, DEMAND_AWARE_ALIAS, REGISTERED, [], 100, thread_key=THREAD)
    await drain_sticky_writes()  # P3: the sticky write is fire-and-forget — flush before asserting
    assert res.ok and res.model == DS
    assert res.order == [GLM, DS], "the profile ordering itself is returned unchanged"


async def test_sticky_head_is_key_scoped(env) -> None:
    """Two keys, same thread fingerprint (same first message): each keeps
    its OWN head — the record is keyed (key_id, thread_key)."""
    fake, key_id = env
    other = uuid.uuid4()
    fake.rows[(key_id, THREAD)] = DS
    fake.rows[(other, THREAD)] = GLM
    assert (await resolve_cascade(key_id, DEMAND_AWARE_ALIAS, REGISTERED, [], 100, thread_key=THREAD)).model == DS
    await drain_sticky_writes()  # P3: flush the detached sticky write before asserting
    assert (await resolve_cascade(other, DEMAND_AWARE_ALIAS, REGISTERED, [], 100, thread_key=THREAD)).model == GLM
    await drain_sticky_writes()  # P3: flush the detached sticky write before asserting


async def test_other_conversations_re_pick(env) -> None:
    """Stickiness is per conversation: a DIFFERENT thread key picks fresh by
    the live signal."""
    fake, key_id = env
    _eval_state(GLM, active=False)
    _eval_state(DS, active=True)
    assert (await resolve_cascade(key_id, DEMAND_AWARE_ALIAS, REGISTERED, [], 100, thread_key=THREAD)).model == DS
    await drain_sticky_writes()  # P3: flush the detached sticky write before asserting
    other_thread = "ffffffffffffffff"
    assert (await resolve_cascade(key_id, DEMAND_AWARE_ALIAS, REGISTERED, [], 100, thread_key=other_thread)).model == DS
    await drain_sticky_writes()  # P3: flush the detached sticky write before asserting
    assert fake.rows[(key_id, THREAD)] == DS and fake.rows[(key_id, other_thread)] == DS


# ---------------------------------------------------------------------------
# 3. 429 on the head: the existing admission walk still walks
# ---------------------------------------------------------------------------


def test_rate_429_on_the_reordered_head_walks_the_ordering(monkeypatch: pytest.MonkeyPatch) -> None:
    """The sticky/load head rate-429s at admission -> `_admit_with_failover`
    walks on (unchanged machinery). The walk order it is handed is the
    load-aware one: head first, then the rest of the ordering."""
    calls: list[str] = []

    async def gates(key, model, est, endpoint, request_id, started, precomputed_budget=None):
        calls.append(model)
        if model == DS:
            raise fastapi.HTTPException(429, "TPM limit 100000 exceeded (400000 this minute)")
        return SimpleNamespace(warn_level=0, rate_headers={})

    monkeypatch.setattr(routes_proxy, "_run_gates", gates)
    key = SimpleNamespace(id=uuid.uuid4())
    model, _gate, failed_from = asyncio.run(routes_proxy._admit_with_failover(key, [DS, GLM], 100, "rid", 0.0, None))
    assert model == GLM and failed_from == DS
    assert calls == [DS, GLM], "the head is tried first, then the ordering continues"


def test_no_stickiness_to_the_failed_head(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 429 on the head must NOT strand the conversation: the walker
    promotes it, and the promoted model is what the caller serves. (The
    sticky row is deliberately left alone — the next turn re-tries the head
    and only a re-pick rewrites it.)"""
    calls: list[str] = []

    async def gates(key, model, est, endpoint, request_id, started, precomputed_budget=None):
        calls.append(model)
        if model == DS:
            raise fastapi.HTTPException(429, "TPM limit exceeded")
        return SimpleNamespace(warn_level=0, rate_headers={})

    monkeypatch.setattr(routes_proxy, "_run_gates", gates)
    key = SimpleNamespace(id=uuid.uuid4())
    model, _gate, _from = asyncio.run(routes_proxy._admit_with_failover(key, [DS, GLM], 100, "rid", 0.0, None))
    assert model == GLM
    assert calls == [DS, GLM]


# ---------------------------------------------------------------------------
# 4. the sticky model fell out of the allowed/registered set
# ---------------------------------------------------------------------------


async def test_sticky_model_not_registered_is_dropped_and_re_picked(env) -> None:
    """The stored head is no longer in the registry: the live set wins — the
    record is dropped and a fresh pick is made."""
    fake, key_id = env
    fake.rows[(key_id, THREAD)] = "ghost-model"
    _eval_state(GLM, active=False)
    _eval_state(DS, active=True)

    res = await resolve_cascade(key_id, DEMAND_AWARE_ALIAS, REGISTERED, [], 100, thread_key=THREAD)
    await drain_sticky_writes()  # P3: the sticky write is fire-and-forget — flush before asserting
    assert res.ok and res.model == DS
    assert fake.drops == [(key_id, THREAD)]
    assert fake.rows[(key_id, THREAD)] == DS, "the fresh pick is recorded"


async def test_sticky_model_outside_the_acl_is_dropped(env) -> None:
    """The key's allowed_models no longer grants the stored head: it is not
    a candidate, so the record is dropped (never served) and the pick is
    fresh within the ACL."""
    fake, key_id = env
    fake.rows[(key_id, THREAD)] = DS
    res = await resolve_cascade(key_id, DEMAND_AWARE_ALIAS, REGISTERED, [GLM], 100, thread_key=THREAD)
    await drain_sticky_writes()  # P3: the sticky write is fire-and-forget — flush before asserting
    assert res.ok and res.model == GLM
    assert fake.drops == [(key_id, THREAD)]


async def test_sticky_model_dropped_from_the_profile_ordering(env) -> None:
    """A model that left the PROFILE ordering is not a candidate either —
    same drop-and-re-pick path."""
    fake, key_id = env
    fake.rows[(key_id, THREAD)] = DS
    fake.order = [GLM]  # admin removed Deepseek from the ordering
    res = await resolve_cascade(key_id, DEMAND_AWARE_ALIAS, REGISTERED, [], 100, thread_key=THREAD)
    await drain_sticky_writes()  # P3: the sticky write is fire-and-forget — flush before asserting
    assert res.ok and res.model == GLM
    assert fake.drops == [(key_id, THREAD)]


async def test_unregistered_candidate_is_never_the_head(env) -> None:
    """An ordering entry that is not registered can never be picked as the
    head (it cannot serve): the pick runs over the SERVABLE candidates and
    the walk still reports the unregistered one as an attempt."""
    fake, key_id = env
    fake.order = ["ghost", GLM, DS]
    _eval_state(GLM, active=True)
    _eval_state(DS, active=False)
    res = await resolve_cascade(key_id, DEMAND_AWARE_ALIAS, REGISTERED, [], 100, thread_key=THREAD)
    await drain_sticky_writes()  # P3: the sticky write is fire-and-forget — flush before asserting
    assert res.ok and res.model == GLM
    assert fake.rows[(key_id, THREAD)] == GLM


async def test_ordering_with_nothing_servable_is_unchanged(env) -> None:
    """No candidate can serve -> the walk behaves exactly as today and NO
    stickiness is written (nothing to stick to)."""
    fake, key_id = env
    fake.order = ["ghost-a", "ghost-b"]
    res = await resolve_cascade(key_id, DEMAND_AWARE_ALIAS, REGISTERED, [], 100, thread_key=THREAD)
    await drain_sticky_writes()  # P3: the sticky write is fire-and-forget — flush before asserting
    assert not res.ok
    assert [a["model"] for a in res.attempts] == ["ghost-a", "ghost-b"]
    assert fake.writes == [] and fake.rows == {}


# ---------------------------------------------------------------------------
# 5. pinned requests never touch stickiness
# ---------------------------------------------------------------------------


async def test_pinned_request_does_not_read_or_write(env) -> None:
    """Pinning is an explicit choice: the pinned model serves directly with
    no sticky read, no sticky write — even when the off-peak signal says
    otherwise and a thread key is present."""
    fake, key_id = env
    _eval_state(GLM, active=False)
    _eval_state(DS, active=True)
    res = await resolve_cascade(key_id, GLM, REGISTERED, [], 100, thread_key=THREAD)
    await drain_sticky_writes()  # P3: the sticky write is fire-and-forget — flush before asserting
    assert res.ok and res.model == GLM
    assert fake.reads == [] and fake.writes == [] and fake.drops == []


async def test_pinned_budget_exhaustion_does_not_create_stickiness(env) -> None:
    """Budget exhaustion promotes a PINNED request to the ordering — that
    promotion is not a conversation head pick, so it writes nothing."""
    fake, key_id = env
    fake.rows[(key_id, THREAD)] = DS  # a record that must be ignored
    _eval_state(GLM, active=False)
    _eval_state(DS, active=True)

    real = budgets_mod.check_budgets

    async def exhausted_for_glm(kid, model, est=0):
        if model == GLM:
            return budgets_mod.GateResult(False, "monthly budget exceeded")
        return await real(kid, model, est)

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(cascade_mod, "check_budgets", exhausted_for_glm)
    try:
        res = await resolve_cascade(key_id, GLM, REGISTERED, [], 100, thread_key=THREAD)
        await drain_sticky_writes()  # P3: the sticky write is fire-and-forget — flush before asserting
    finally:
        monkeypatch.undo()
    assert res.ok and res.model == DS
    assert fake.reads == [] and fake.writes == [], "pinned never touches stickiness"


async def test_pinned_headroom_returns_the_pinned_gate(env) -> None:
    """The pinned fast path still carries its GateResult (the perf-audit
    contract the proxy depends on) — the new parameter changes nothing."""
    _fake, key_id = env
    res = await resolve_cascade(key_id, GLM, REGISTERED, [], 100, thread_key=THREAD)
    await drain_sticky_writes()  # P3: the sticky write is fire-and-forget — flush before asserting
    assert res.ok and res.gate is not None and res.gate.ok


# ---------------------------------------------------------------------------
# 6. rank semantics (rule 1 / rule 5, pure)
# ---------------------------------------------------------------------------


def test_rank_active_preferred_not_active_yields() -> None:
    _eval_state(GLM, active=False)
    _eval_state(DS, active=True)
    assert _ranks(load_rank([GLM, DS])) == {GLM: 1, DS: 0}
    pick = pick_load_aware([GLM, DS])
    assert pick.model == DS and pick.reason == "load"


def test_rank_no_signal_ranks_zero_for_everyone() -> None:
    """No per-model eval configured / evaluator off: every candidate ranks 0
    -> the profile order stands (the feature is inert)."""
    assert _ranks(load_rank([GLM, DS])) == {GLM: 0, DS: 0}
    pick = pick_load_aware([GLM, DS])
    assert pick.model == GLM and pick.reason == "default"


def test_ties_preserve_the_profile_order() -> None:
    """Both models active (low demand) -> the FIRST in the profile order
    wins (GLM before Deepseek — the brief's tie rule)."""
    _eval_state(GLM, active=True)
    _eval_state(DS, active=True)
    assert pick_load_aware([GLM, DS]).model == GLM
    # ... and the reverse ordering picks the other one: order is the only
    # tie-break.
    assert pick_load_aware([DS, GLM]).model == DS


def test_never_sampled_model_is_conservative() -> None:
    """A configured eval that has never produced a sample ranks 1 — a model
    whose load is unknown must not outrank one known to be idle."""
    _eval_state(GLM, active=False, sampled=False)  # never sampled
    _eval_state(DS, active=True)  # known idle
    assert _ranks(load_rank([GLM, DS])) == {GLM: 1, DS: 0}
    assert pick_load_aware([GLM, DS]).model == DS


def test_all_configured_and_all_loaded_falls_back_to_profile_head() -> None:
    """Every model under load: all rank 1 -> the profile head (GLM) still
    wins; the feature never leaves the ordering headless."""
    _eval_state(GLM, active=False)
    _eval_state(DS, active=False)
    pick = pick_load_aware([GLM, DS])
    assert pick.model == GLM and pick.reason == "load"


def test_partial_signal_only_penalises_configured_models() -> None:
    """Only GLM has an eval; Deepseek has none. GLM under load -> Deepseek
    (no signal, rank 0) outranks it — a configured-but-busy model yields to
    an unconfigured one, which is the operator's stated intent."""
    _eval_state(GLM, active=False)
    assert _ranks(load_rank([GLM, DS])) == {GLM: 1, DS: 0}
    assert pick_load_aware([GLM, DS]).model == DS


def test_pick_load_aware_empty_input() -> None:
    pick = pick_load_aware([])
    assert pick.model == "" and pick.reason == "default" and pick.ranks == []


def test_ranks_text_is_the_observability_shape() -> None:
    _eval_state(GLM, active=False)
    assert cascade_mod._ranks_text(load_rank([GLM, DS])) == f"{GLM}=1,{DS}=0"


# ---------------------------------------------------------------------------
# 7. offpeak accessor (the public signal seam)
# ---------------------------------------------------------------------------


def test_model_active_none_without_an_eval() -> None:
    assert offpeak.model_active("not-configured") is None


def test_model_active_none_while_the_evaluator_is_disabled() -> None:
    """The evaluator is off (stop()/disabled): no signal -> None, which the
    ranker treats as "no opinion" (rank 0) — the feature stays inert rather
    than conservatively penalising every model."""
    st = offpeak._State(label=GLM, enabled=False, active=True, fraction=0.5)
    st.last_value_pct = 10.0
    offpeak._model_evals[GLM] = ("q", st)
    assert offpeak.model_active(GLM) is None


def test_model_active_reads_the_hysteresis_state() -> None:
    _eval_state(GLM, active=True)
    _eval_state(DS, active=False)
    assert offpeak.model_active(GLM) is True
    assert offpeak.model_active(DS) is False


def test_model_active_never_sampled_is_false() -> None:
    """Never sampled (last_value_pct None) but enabled -> False, i.e. rank 1
    (conservative) — distinct from "no signal at all"."""
    _eval_state(GLM, active=False, sampled=False)
    assert offpeak.model_active(GLM) is False


def test_model_active_is_a_pure_read() -> None:
    """The accessor must not mutate evaluator or billing state."""
    _eval_state(GLM, active=True)
    st = offpeak._model_evals[GLM][1]
    before = (st.active, st.fraction, st.last_value_pct, len(st.history))
    offpeak.model_active(GLM)
    assert (st.active, st.fraction, st.last_value_pct, len(st.history)) == before


# ---------------------------------------------------------------------------
# 8. observability: one INFO + one counter tick per decision
# ---------------------------------------------------------------------------


def _counter(picked: str, reason: str) -> float:
    from pcai_llm.core import metrics

    for fam in metrics.CASCADE_HEAD_PICKS.collect():
        for s in fam.samples:
            if s.labels == {"picked": picked, "reason": reason}:
                return float(s.value)
    return 0.0


async def test_reason_load_recorded_when_the_signal_decides(env, caplog: pytest.LogCaptureFixture) -> None:
    _fake, key_id = env
    _eval_state(GLM, active=False)
    _eval_state(DS, active=True)
    before = _counter(DS, "load")
    with caplog.at_level("INFO", logger="pcai_llm.core.stickiness"):
        res = await resolve_cascade(key_id, DEMAND_AWARE_ALIAS, REGISTERED, [], 100, thread_key=THREAD)
        await drain_sticky_writes()  # P3: the sticky write is fire-and-forget — flush before asserting
    assert res.model == DS
    assert _counter(DS, "load") == before + 1
    assert any(f"model={DS} reason=load thread={THREAD[:8]}" in r.message for r in caplog.records)


async def test_reason_sticky_recorded_on_a_hit(env) -> None:
    fake, key_id = env
    fake.rows[(key_id, THREAD)] = DS
    before = _counter(DS, "sticky")
    res = await resolve_cascade(key_id, DEMAND_AWARE_ALIAS, REGISTERED, [], 100, thread_key=THREAD)
    await drain_sticky_writes()  # P3: the sticky write is fire-and-forget — flush before asserting
    assert res.model == DS
    assert _counter(DS, "sticky") == before + 1


async def test_reason_default_recorded_when_inert(env) -> None:
    """Feature ON, no per-model eval anywhere -> the decision is recorded as
    reason=default (the brief's third label)."""
    _fake, key_id = env
    before = _counter(GLM, "default")
    res = await resolve_cascade(key_id, DEMAND_AWARE_ALIAS, REGISTERED, [], 100, thread_key=THREAD)
    await drain_sticky_writes()  # P3: the sticky write is fire-and-forget — flush before asserting
    assert res.model == GLM
    assert _counter(GLM, "default") == before + 1


async def test_no_counter_tick_when_the_feature_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Feature off: no read, no write, and no head-pick record — the metric
    stays exactly as it was (byte-identical observability too)."""
    fake = CascadeFakeDB(order=[GLM, DS])
    monkeypatch.setattr(cascade_mod, "db", fake)
    monkeypatch.setattr(budgets_mod, "db", fake)
    monkeypatch.setattr(stickiness, "db", fake)
    monkeypatch.setattr(settings, "load_aware_cascade", False)
    before = _counter(GLM, "default")
    await resolve_cascade(uuid.uuid4(), DEMAND_AWARE_ALIAS, REGISTERED, [], 100, thread_key=THREAD)
    await drain_sticky_writes()  # P3: the sticky write is fire-and-forget — flush before asserting
    assert _counter(GLM, "default") == before


# ---------------------------------------------------------------------------
# 9. fail-open: a stickiness outage cannot break admission
# ---------------------------------------------------------------------------


async def test_store_outage_still_resolves(env, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every stickiness call failing (DB down / table missing) degrades to
    the profile-order head — admission never fails."""
    _fake, key_id = env
    _eval_state(GLM, active=False)
    _eval_state(DS, active=True)
    monkeypatch.setattr(stickiness, "db", _BoomDB())
    res = await resolve_cascade(key_id, DEMAND_AWARE_ALIAS, REGISTERED, [], 100, thread_key=THREAD)
    await drain_sticky_writes()  # P3: the sticky write is fire-and-forget — flush before asserting
    assert res.ok and res.model == DS, "the load pick still works without the store"


class _BoomDB:
    async def fetchrow(self, sql: str, *a: Any):
        raise RuntimeError("db down")

    async def fetch(self, sql: str, *a: Any):
        raise RuntimeError("db down")

    async def execute(self, sql: str, *a: Any):
        raise RuntimeError("db down")


async def test_single_candidate_ordering_has_only_one_head(env) -> None:
    """A one-model ordering has nothing to CHOOSE between: with no signal the
    pick is inert (no reorder, no row), and with a signal the head is still
    that model."""
    fake, key_id = env
    fake.order = [GLM]
    res = await resolve_cascade(key_id, DEMAND_AWARE_ALIAS, REGISTERED, [], 100, thread_key=THREAD)
    await drain_sticky_writes()  # P3: the sticky write is fire-and-forget — flush before asserting
    assert res.ok and res.model == GLM
    assert fake.writes == [], "no signal -> inert, nothing recorded"

    _eval_state(GLM, active=True)  # a real signal now exists
    second = await resolve_cascade(key_id, DEMAND_AWARE_ALIAS, REGISTERED, [], 100, thread_key=THREAD)
    await drain_sticky_writes()  # P3: the sticky write is fire-and-forget — flush before asserting
    assert second.ok and second.model == GLM
    assert fake.rows[(key_id, THREAD)] == GLM


# ---------------------------------------------------------------------------
# 10. HEALTH GATE — idle-because-broken must never outrank loaded-but-healthy
#     (the G2 live defect: Qwen3.8-27B at 0.00% GPU util while 400-ing ~86%
#     of calls ranked 0 and took every new conversation)
# ---------------------------------------------------------------------------


def test_pool_servable_is_the_shared_availability_predicate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_pool_servable` IS the codebase's pool-availability idiom: a pool with
    a non-cooled endpoint is servable; a cooled one is not; an ABSENT pool is
    not (the proxy 404s a model with no pool, so absence cannot serve)."""
    router = CascadeFakeRouter([GLM, DS])
    monkeypatch.setattr(cascade_mod, "model_router", router)
    assert cascade_mod._pool_servable(GLM) is True
    router.cool(DS)
    assert cascade_mod._pool_servable(DS) is False
    assert cascade_mod._pool_servable("never-registered") is False
    assert router.lookups == [GLM, DS, "never-registered"], "one pool lookup per probe"


async def test_unhealthy_candidate_loses_to_loaded_but_healthy(env) -> None:
    """THE LIVE SHAPE (defect fix): Qwen is idle on the off-peak signal
    (rank 0, "free") but its pool has no available endpoint — every call
    400s. GLM is under load (rank 1) but healthy. The head must be GLM:
    idle-because-BROKEN must never beat loaded-but-healthy."""
    fake, key_id = env
    fake.order = [QWEN, GLM]  # Qwen first, exactly as the live rank-0 pick saw it
    _eval_state(QWEN, active=True)  # idle GPUs: the misleading "free" signal
    _eval_state(GLM, active=False)  # GLM loaded: would normally yield
    fake.router.cool(QWEN)

    res = await resolve_cascade(key_id, DEMAND_AWARE_ALIAS, ROSTER, [], 100, thread_key=THREAD)
    await drain_sticky_writes()  # P3: the sticky write is fire-and-forget — flush before asserting
    assert res.ok and res.model == GLM, "the broken-but-idle model must not win"
    assert fake.rows[(key_id, THREAD)] == GLM, "the healthy pick is what sticks"


async def test_unhealthy_candidates_are_demoted_behind_healthy_ones(env) -> None:
    """The returned ORDER keeps every candidate (never drops one — the budget
    walk/failover may still legitimately use them) with the unhealthy ones
    AFTER the healthy ones."""
    fake, key_id = env
    _eval_state(QWEN, active=True)  # idle -> would rank 0
    _eval_state(GLM, active=False)  # loaded
    _eval_state(DS, active=True)  # idle + healthy
    fake.router.cool(QWEN)

    ordered = await cascade_mod._reorder_head(key_id, THREAD, [QWEN, GLM, DS], ROSTER)
    assert ordered == [DS, GLM, QWEN], "healthy first (load pick then the rest), broken last"
    assert set(ordered) == {QWEN, GLM, DS}, "nobody is dropped — only reordered"


async def test_the_walk_is_handed_the_demoted_order(env, monkeypatch: pytest.MonkeyPatch) -> None:
    """End-to-end: the candidate list the budget walk iterates is the
    health-demoted one. With every model exhausted the walk visits ALL of
    them, so `attempts` is the exact sequence admission would have tried."""
    fake, key_id = env
    fake.order = [QWEN, GLM, DS]
    _eval_state(QWEN, active=True)  # idle -> would rank 0
    _eval_state(GLM, active=False)
    _eval_state(DS, active=True)  # idle + healthy -> the head
    fake.router.cool(QWEN)

    walked: list[str] = []
    real = cascade_mod.check_budgets

    async def exhausted(kid, model, est=0):
        walked.append(model)
        return budgets_mod.GateResult(False, "monthly budget exceeded")

    monkeypatch.setattr(cascade_mod, "check_budgets", exhausted)
    try:
        res = await resolve_cascade(key_id, DEMAND_AWARE_ALIAS, ROSTER, [], 100, thread_key=THREAD)
        await drain_sticky_writes()  # P3: the sticky write is fire-and-forget — flush before asserting
    finally:
        monkeypatch.setattr(cascade_mod, "check_budgets", real)

    assert not res.ok  # everything exhausted
    assert walked == [DS, GLM, QWEN], "healthy head, then the rest, broken LAST"
    assert [a["model"] for a in res.attempts or []] == [DS, GLM, QWEN]


async def test_sticky_head_that_became_unhealthy_is_dropped_and_re_picked(env) -> None:
    """A sticky head that was healthy at conversation start but has since
    gone unhealthy (breaker tripped) is STALE: the record is dropped and the
    conversation re-picks from the healthy set — the same disposition as the
    no-longer-registered branch, with the same counter discipline."""
    fake, key_id = env
    fake.rows[(key_id, THREAD)] = GLM
    _eval_state(GLM, active=True)  # sticky head: idle, but broken below
    _eval_state(DS, active=False)  # loaded-but-healthy: the only way out
    fake.router.cool(GLM)

    res = await resolve_cascade(key_id, DEMAND_AWARE_ALIAS, REGISTERED, [], 100, thread_key=THREAD)
    await drain_sticky_writes()  # P3: the sticky write is fire-and-forget — flush before asserting
    assert res.ok and res.model == DS, "the stale head must not serve"
    assert fake.drops == [(key_id, THREAD)], "the stale record is dropped"
    assert fake.rows[(key_id, THREAD)] == DS, "the fresh healthy pick is recorded"


async def test_sticky_head_that_became_unhealthy_is_logged(env, caplog: pytest.LogCaptureFixture) -> None:
    """Observability: the drop is INFO-logged with the model and the reason,
    and the head-pick counter still ticks with the existing vocabulary
    (metrics.CASCADE_PICK_REASONS has no "unhealthy" value — this file does
    not own metrics.py, so the reason rides the log line)."""
    fake, key_id = env
    fake.rows[(key_id, THREAD)] = GLM
    _eval_state(GLM, active=False)
    _eval_state(DS, active=True)
    fake.router.cool(GLM)
    before = _counter(DS, "load")

    with caplog.at_level("INFO", logger="pcai_llm.core.cascade"):
        res = await resolve_cascade(key_id, DEMAND_AWARE_ALIAS, REGISTERED, [], 100, thread_key=THREAD)
        await drain_sticky_writes()  # P3: the sticky write is fire-and-forget — flush before asserting

    assert res.model == DS
    assert _counter(DS, "load") == before + 1, "the surviving reason vocabulary is reused"
    assert any("sticky head" in r.message and GLM in r.message and "unhealthy" in r.message for r in caplog.records), (
        "the exclusion must be visible at INFO"
    )


async def test_excluded_unhealthy_candidate_is_logged(env, caplog: pytest.LogCaptureFixture) -> None:
    """Rule 7: excluding an unhealthy candidate from the ranking is
    observable at INFO with the excluded model name."""
    fake, key_id = env
    fake.order = [GLM, DS]
    _eval_state(GLM, active=False)  # loaded-but-healthy
    _eval_state(DS, active=True)  # idle-but-broken
    fake.router.cool(DS)

    with caplog.at_level("INFO", logger="pcai_llm.core.cascade"):
        res = await resolve_cascade(key_id, DEMAND_AWARE_ALIAS, REGISTERED, [], 100, thread_key=THREAD)
        await drain_sticky_writes()  # P3: the sticky write is fire-and-forget — flush before asserting

    assert res.model == GLM
    assert any("excludes unhealthy" in r.message and DS in r.message for r in caplog.records), (
        "the excluded model must be named in the log"
    )


async def test_all_candidates_unhealthy_leaves_the_list_unchanged(env) -> None:
    """NO healthy candidate: the head pick has no opinion — the list is
    returned UNCHANGED, nothing is written or dropped, and the walk below
    surfaces the real outcome (the pick never makes things worse)."""
    fake, key_id = env
    fake.rows[(key_id, THREAD)] = GLM  # a record that must survive
    _eval_state(GLM, active=True)
    _eval_state(DS, active=True)
    fake.router.cool(GLM)
    fake.router.cool(DS)

    res = await resolve_cascade(key_id, DEMAND_AWARE_ALIAS, REGISTERED, [], 100, thread_key=THREAD)
    await drain_sticky_writes()  # P3: the sticky write is fire-and-forget — flush before asserting
    # No budgets configured in the fake -> the walk still resolves on the
    # PROFILE head (the health gate did not reorder, the signal never got a
    # say). What matters: no sticky write, no drop, no crash.
    assert res.ok and res.model == GLM
    assert fake.writes == [] and fake.drops == []
    assert fake.rows[(key_id, THREAD)] == GLM, "an unhealthy-now head must not be revoked"


async def test_all_candidates_unhealthy_does_not_crash_or_reorder(env, caplog: pytest.LogCaptureFixture) -> None:
    """... and it is observable: one INFO line naming the skipped pick. The
    signal says Deepseek (idle) but BOTH pools are cooling, so the profile
    order stands."""
    fake, key_id = env
    _eval_state(GLM, active=False)  # would normally yield to DS
    _eval_state(DS, active=True)
    fake.router.cool(GLM)
    fake.router.cool(DS)

    with caplog.at_level("INFO", logger="pcai_llm.core.cascade"):
        res = await resolve_cascade(key_id, DEMAND_AWARE_ALIAS, REGISTERED, [], 100, thread_key=THREAD)
        await drain_sticky_writes()  # P3: the sticky write is fire-and-forget — flush before asserting

    assert res.ok and res.model == GLM, "the profile order stands untouched"
    assert any("no healthy candidate" in r.message for r in caplog.records)
    assert fake.reads == [], "the store is not even consulted when nothing can serve"


async def test_healthy_sticky_head_is_still_honored(env) -> None:
    """Regression guard: the health gate must not disturb the happy path — a
    healthy sticky head still wins even when the signal would pick the
    other model (stickiness is the whole point of the feature)."""
    fake, key_id = env
    fake.rows[(key_id, THREAD)] = DS
    _eval_state(GLM, active=True)  # GLM idle: the load pick would be GLM
    _eval_state(DS, active=False)

    res = await resolve_cascade(key_id, DEMAND_AWARE_ALIAS, REGISTERED, [], 100, thread_key=THREAD)
    await drain_sticky_writes()  # P3: the sticky write is fire-and-forget — flush before asserting
    assert res.ok and res.model == DS, "a healthy sticky head is untouched by the gate"
    assert fake.drops == [] and fake.writes == [], "no drop, no rewrite on a hit"


async def test_healthy_sticky_head_at_profile_head_still_skips_the_rewrite(env) -> None:
    """A healthy sticky head that IS the profile head: no reorder, no row
    rewrite (the pre-existing fast path, preserved under the gate)."""
    fake, key_id = env
    fake.rows[(key_id, THREAD)] = GLM
    _eval_state(GLM, active=False)
    _eval_state(DS, active=True)

    res = await resolve_cascade(key_id, DEMAND_AWARE_ALIAS, REGISTERED, [], 100, thread_key=THREAD)
    await drain_sticky_writes()  # P3: the sticky write is fire-and-forget — flush before asserting
    assert res.ok and res.model == GLM
    assert fake.writes == [] and fake.drops == []


async def test_inert_path_never_reorders_despite_an_unhealthy_head(env) -> None:
    """INERTNESS REGRESSION: feature ON but no per-model eval anywhere -> the
    pick is reason=default and the candidate list is returned BYTE-IDENTICAL
    even when the profile head is unhealthy (rule 3's discipline: the health
    partition must not reorder a path the signal never got to rank)."""
    fake, key_id = env
    fake.router.cool(GLM)  # even an unhealthy profile head changes nothing
    res = await resolve_cascade(key_id, DEMAND_AWARE_ALIAS, REGISTERED, [], 100, thread_key=THREAD)
    await drain_sticky_writes()  # P3: the sticky write is fire-and-forget — flush before asserting
    assert res.ok and res.model == GLM, "no signal -> profile order, health notwithstanding"
    assert fake.writes == [] and fake.rows == {}, "an inert pick must not record a head"


def test_health_predicate_is_not_called_when_the_feature_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The feature-off path must not even LOOK at breaker state: `_reorder_head`
    is never entered, so `_pool_servable` is never reached. Pinned with a
    direct probe-counting spy (the inertness gate is the brief's critical
    backwards-compat contract)."""
    calls: list[str] = []

    def spy(model: str) -> bool:
        calls.append(model)
        return True

    monkeypatch.setattr(cascade_mod, "_pool_servable", spy)
    monkeypatch.setattr(settings, "load_aware_cascade", False)

    fake = CascadeFakeDB(order=[GLM, DS])
    monkeypatch.setattr(cascade_mod, "db", fake)
    monkeypatch.setattr(budgets_mod, "db", fake)
    monkeypatch.setattr(stickiness, "db", fake)

    asyncio.run(resolve_cascade(uuid.uuid4(), DEMAND_AWARE_ALIAS, REGISTERED, [], 100, thread_key=THREAD))
    assert calls == [], "feature off -> the health predicate is never called"


def test_no_pool_lookup_when_the_feature_is_off(monkeypatch: pytest.MonkeyPatch, health_gate) -> None:
    """Same gate, observed at the HEALTH-GATE seam: feature off -> the health
    partition never probes a pool. (The router itself IS consulted once per
    candidate — for its TYPE, by the chat-only filter — so a bare
    ``lookups == []`` would now measure the wrong thing; see health_gate.)"""
    router = _install_router(monkeypatch)
    monkeypatch.setattr(settings, "load_aware_cascade", False)

    fake = CascadeFakeDB(order=[GLM, DS])
    monkeypatch.setattr(cascade_mod, "db", fake)
    monkeypatch.setattr(budgets_mod, "db", fake)
    monkeypatch.setattr(stickiness, "db", fake)
    router.cool(GLM)  # would change the head if the gate ran

    res = asyncio.run(resolve_cascade(uuid.uuid4(), DEMAND_AWARE_ALIAS, REGISTERED, [], 100, thread_key=THREAD))
    assert res.ok and res.model == GLM
    assert health_gate == [], "feature off -> the health gate never runs"
    # ...and the ONLY router consults are the one-per-candidate TYPE checks.
    assert router.lookups == [GLM, DS], "type consult only — no health probe"


async def test_no_pool_lookup_without_a_thread_key(env, health_gate) -> None:
    """The other inertness gate (brief rule 2): no conversation fingerprint ->
    the head machinery never runs, so the health gate never probes a pool."""
    fake, key_id = env
    fake.router.cool(GLM)  # would change the head if the gate ran
    res = await resolve_cascade(key_id, DEMAND_AWARE_ALIAS, REGISTERED, [], 100, thread_key=None)
    await drain_sticky_writes()  # P3: the sticky write is fire-and-forget — flush before asserting
    assert res.ok and res.model == GLM
    assert health_gate == [], "no thread key -> the health gate never runs"
    assert fake.router.lookups == [GLM, DS], "type consult only — no health probe"


def test_head_first_keeps_the_set_and_demotes_unhealthy() -> None:
    """`_head_first` is pure: head first, healthy remainder in their existing
    relative order, unhealthy LAST — and nobody is dropped."""
    assert cascade_mod._head_first(DS, [QWEN, GLM, DS], [QWEN]) == [DS, GLM, QWEN]
    assert cascade_mod._head_first(GLM, [GLM, DS], []) == [GLM, DS]
    # Multiple unhealthy candidates keep their relative order at the tail.
    assert cascade_mod._head_first(GLM, [QWEN, GLM, DS], [QWEN, DS]) == [GLM, QWEN, DS]


def test_head_first_with_no_unhealthy_is_the_pre_gate_reorder() -> None:
    """With an empty unhealthy set the helper reproduces the exact
    pre-health-gate expression `[head, *(m for m in candidates if m != head)]`."""
    candidates = [GLM, DS]
    assert cascade_mod._head_first(DS, candidates, []) == [DS, *[m for m in candidates if m != DS]]


async def test_health_gate_fails_open_when_the_pool_lookup_raises(env, monkeypatch: pytest.MonkeyPatch) -> None:
    """A router fault must not break admission: the probe raising propagates
    to `_reorder_head`'s caller (resolve_cascade's fail-open guard) and the
    profile order serves."""
    _fake, key_id = env
    _eval_state(GLM, active=False)
    _eval_state(DS, active=True)

    def boom(_model: str) -> bool:
        raise RuntimeError("router exploded")

    monkeypatch.setattr(cascade_mod, "_pool_servable", boom)
    res = await resolve_cascade(key_id, DEMAND_AWARE_ALIAS, REGISTERED, [], 100, thread_key=THREAD)
    await drain_sticky_writes()  # P3: the sticky write is fire-and-forget — flush before asserting
    assert res.ok and res.model == GLM, "a probe fault degrades to the profile order"


# ---------------------------------------------------------------------------
# 11. ALIAS SPLIT — the load-aware head is opt-in BY NAME (operator decision,
#     2026-10-04). `auto-demand-aware` engages it; the legacy `auto` NEVER
#     does. Before the split the gate was `kind == "alias"`, i.e. EVERY alias
#     — turning the feature on silently changed what `auto` meant for every
#     existing client. These are the headline regression tests.
# ---------------------------------------------------------------------------


def test_the_alias_constant_is_the_one_source_of_truth() -> None:
    """The name is a module-level constant so callers and tests share ONE
    definition (cascade.py's DEMAND_AWARE_ALIAS) — and it is deliberately
    NOT 'auto', the legacy name the feature must never touch."""
    assert cascade_mod.DEMAND_AWARE_ALIAS == "auto-demand-aware"
    assert cascade_mod.DEMAND_AWARE_ALIAS != "auto"


def test_classify_request_cannot_tell_the_two_aliases_apart() -> None:
    """Both names classify as 'alias' — the classifier is deliberately
    name-agnostic (it knows only registered/not). The split therefore lives
    in resolve_cascade's engagement gate, NOT in the classifier: this pins
    that nobody "fixes" it by teaching classify_request about aliases."""
    from pcai_llm.core.cascade import classify_request

    assert classify_request("auto", REGISTERED, []) == "alias"
    assert classify_request(DEMAND_AWARE_ALIAS, REGISTERED, []) == "alias"


async def test_legacy_auto_is_inert_with_the_feature_on(env, health_gate) -> None:
    """THE HEADLINE REGRESSION TEST (the operator's requirement): feature ON,
    thread key in hand, signal configured and pointing at Deepseek — and the
    LEGACY `auto` still resolves the pure profile-order head with NO sticky
    read, NO sticky write, NO pool lookup, and NO counter tick. `auto` means
    exactly what it meant before the feature existed."""
    fake, key_id = env
    _eval_state(GLM, active=False)  # GLM loaded: would reorder if engaged
    _eval_state(DS, active=True)  # Deepseek idle: the load pick would be DS
    fake.router.cool(GLM)  # ... and the health gate would demote GLM too
    before = _counter(DS, "load")
    before_unhealthy = _counter(GLM, "unhealthy")

    res = await resolve_cascade(key_id, "auto", REGISTERED, [], 100, thread_key=THREAD)
    await drain_sticky_writes()  # P3: the sticky write is fire-and-forget — flush before asserting

    assert res.ok and res.model == GLM, "the profile head stands — no load ranking"
    assert res.order == [GLM, DS], "the returned ordering is the profile order"
    assert fake.reads == [], "no sticky read"
    assert fake.writes == [] and fake.rows == {}, "no sticky write"
    assert fake.drops == [], "no sticky drop"
    assert health_gate == [], "no health probe — the load-aware gate never runs"
    assert _counter(DS, "load") == before, "no head-pick counter tick"
    assert _counter(GLM, "unhealthy") == before_unhealthy, "not even an exclusion tick"


async def test_legacy_auto_is_BYTE_IDENTICAL_to_the_feature_off_resolution(
    monkeypatch: pytest.MonkeyPatch,
    health_gate,
) -> None:
    """THE STRONGEST FORM OF THE HEADLINE REQUIREMENT: with the feature ON,
    the legacy `auto` resolution equals the resolution the SAME environment
    produces with the feature OFF (i.e. the pre-feature behavior) — compared
    FIELD BY FIELD, including the returned `order` and the attempt records.

    The feature-off arm is the reference because it is, by construction, the
    code path that existed before the feature: nothing about the load-aware
    machinery runs in it. If `auto` ever started engaging anything, this
    comparison is what catches it."""
    _eval_state(GLM, active=False)  # GLM loaded: the load pick would be DS
    _eval_state(DS, active=True)
    key_id = uuid.uuid4()

    async def run(feature_on: bool):
        fake = CascadeFakeDB(order=[GLM, DS])
        monkeypatch.setattr(cascade_mod, "db", fake)
        monkeypatch.setattr(budgets_mod, "db", fake)
        monkeypatch.setattr(stickiness, "db", fake)
        monkeypatch.setattr(cascade_mod, "model_router", fake.router)
        monkeypatch.setattr(settings, "load_aware_cascade", feature_on)
        fake.router.cool(GLM)  # would change the head if the gate ran
        res = await resolve_cascade(key_id, "auto", REGISTERED, [], 100, thread_key=THREAD)
        await drain_sticky_writes()  # P3: the sticky write is fire-and-forget — flush before asserting
        return res, fake

    feature_on, on_fake = await run(True)
    feature_off, off_fake = await run(False)

    # Field-by-field: the whole Resolution is identical.
    assert feature_on.ok == feature_off.ok
    assert feature_on.model == feature_off.model == GLM
    assert feature_on.reason == feature_off.reason
    assert feature_on.order == feature_off.order == [GLM, DS]
    assert feature_on.attempts == feature_off.attempts
    # ... and neither arm touched a single seam.
    assert on_fake.reads == off_fake.reads == []
    assert on_fake.writes == off_fake.writes == []
    # Same router consults in both arms (the chat-type check is unconditional),
    # and NEITHER arm ran the health gate: the equality is the contract, the
    # empty health_gate is the proof that the load-aware machinery is inert.
    assert on_fake.router.lookups == off_fake.router.lookups
    assert health_gate == []


async def test_legacy_auto_ignores_an_existing_sticky_record(env) -> None:
    """A sticky record left by the demand-aware alias must not leak into the
    legacy `auto` path: `auto` never READS the store, so a record for the
    conversation is invisible to it (the two aliases share the store but not
    the behavior)."""
    fake, key_id = env
    fake.rows[(key_id, THREAD)] = DS  # written by an earlier demand-aware turn
    _eval_state(GLM, active=False)
    _eval_state(DS, active=True)

    res = await resolve_cascade(key_id, "auto", REGISTERED, [], 100, thread_key=THREAD)
    await drain_sticky_writes()  # P3: the sticky write is fire-and-forget — flush before asserting
    assert res.ok and res.model == GLM, "the legacy path is blind to the sticky head"
    assert fake.reads == [] and fake.writes == []
    assert fake.rows[(key_id, THREAD)] == DS, "the record is left untouched"


async def test_demand_aware_engages_when_the_feature_is_on(env) -> None:
    """The counterpart: the SAME environment that left `auto` inert makes
    `auto-demand-aware` engage — sticky write on turn 1, load ranking, health
    gate. This is the pair's whole point: one environment, two behaviors,
    chosen by name."""
    fake, key_id = env
    _eval_state(GLM, active=False)  # GLM loaded
    _eval_state(DS, active=True)  # Deepseek idle -> the head
    before = _counter(DS, "load")

    res = await resolve_cascade(key_id, DEMAND_AWARE_ALIAS, REGISTERED, [], 100, thread_key=THREAD)
    await drain_sticky_writes()  # P3: the sticky write is fire-and-forget — flush before asserting

    assert res.ok and res.model == DS, "the load pick wins"
    assert fake.writes == [(key_id, THREAD, DS)], "turn 1 records the head"
    assert _counter(DS, "load") == before + 1
    assert fake.router.lookups != [], "the health gate ran"


async def test_demand_aware_health_gate_still_applies(env) -> None:
    """The health gate rides the demand-aware alias (not the legacy one):
    idle-but-broken must not outrank loaded-but-healthy."""
    fake, key_id = env
    fake.order = [QWEN, GLM]
    _eval_state(QWEN, active=True)  # idle -> would rank 0
    _eval_state(GLM, active=False)  # loaded but healthy
    fake.router.cool(QWEN)

    res = await resolve_cascade(key_id, DEMAND_AWARE_ALIAS, ROSTER, [], 100, thread_key=THREAD)
    await drain_sticky_writes()  # P3: the sticky write is fire-and-forget — flush before asserting
    assert res.ok and res.model == GLM, "the broken-but-idle model must not win"


def test_demand_aware_falls_back_to_legacy_when_the_feature_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Feature OFF: the demand-aware alias is INERT — it resolves the legacy
    profile order with nothing touched. The alias name is an OPT-IN, not an
    override of the master switch."""
    fake = CascadeFakeDB(order=[GLM, DS])
    monkeypatch.setattr(cascade_mod, "db", fake)
    monkeypatch.setattr(budgets_mod, "db", fake)
    monkeypatch.setattr(stickiness, "db", fake)
    monkeypatch.setattr(settings, "load_aware_cascade", False)
    _eval_state(GLM, active=False)  # would reorder if engaged
    _eval_state(DS, active=True)

    res = asyncio.run(resolve_cascade(uuid.uuid4(), DEMAND_AWARE_ALIAS, REGISTERED, [], 100, thread_key=THREAD))
    assert res.ok and res.model == GLM, "profile head — the feature is off"
    assert fake.reads == [] and fake.writes == [] and fake.router.lookups == []


def test_demand_aware_without_a_thread_key_is_inert(env, health_gate) -> None:
    """The other gate on the same path: no conversation fingerprint -> no
    head machinery, for the new alias too."""
    fake, key_id = env
    _eval_state(GLM, active=False)
    _eval_state(DS, active=True)

    res = asyncio.run(resolve_cascade(key_id, DEMAND_AWARE_ALIAS, REGISTERED, [], 100, thread_key=None))
    assert res.ok and res.model == GLM
    assert fake.reads == [] and fake.writes == []
    assert health_gate == [], "no health probe — the head machinery never runs"


def test_a_real_model_named_like_the_alias_is_pinned_and_shadows_it() -> None:
    """DOCUMENTED CONSEQUENCE: the alias is only an alias while the name is
    UNREGISTERED. If someone registers a real model literally named
    `auto-demand-aware`, classify_request returns 'pinned' and the request
    serves that model directly — the load-aware cascade never runs for it.

    That is the intended disposition (registration is authoritative: a real
    model of that name is a real model), but it is a sharp edge worth
    pinning: the operator must not name a served model after the alias."""
    from pcai_llm.core.cascade import classify_request

    shadowed = {GLM, DS, DEMAND_AWARE_ALIAS}
    assert classify_request(DEMAND_AWARE_ALIAS, shadowed, []) == "pinned"
    # ... and once it is registered but outside the key's ACL, it is
    # 'forbidden' rather than a cascading alias — the ACL wins over the name.
    assert classify_request(DEMAND_AWARE_ALIAS, shadowed, [GLM]) == "forbidden"


def test_legacy_auto_is_pinned_when_someone_registers_it_too() -> None:
    """The same shadowing applies to `auto` (pre-existing, unchanged by this
    split): a registered model named `auto` is served directly."""
    from pcai_llm.core.cascade import classify_request

    assert classify_request("auto", {GLM, "auto"}, []) == "pinned"


# ---------------------------------------------------------------------------
# 11b. the `unhealthy` counter reason (the deferred follow-up)
# ---------------------------------------------------------------------------


async def test_unhealthy_exclusion_ticks_the_counter_with_its_own_reason(env) -> None:
    """An exclusion is counted with reason=unhealthy (per EXCLUDED model),
    BESIDE the pick's own reason — so the exclusion rate is alertable
    without a second counter, and the pick's vocabulary is unperturbed."""
    fake, key_id = env
    fake.order = [GLM, DS]
    _eval_state(GLM, active=False)  # loaded-but-healthy
    _eval_state(DS, active=True)  # idle-but-broken
    fake.router.cool(DS)
    before_load = _counter(GLM, "load")
    before_unhealthy = _counter(DS, "unhealthy")

    res = await resolve_cascade(key_id, DEMAND_AWARE_ALIAS, REGISTERED, [], 100, thread_key=THREAD)
    await drain_sticky_writes()  # P3: the sticky write is fire-and-forget — flush before asserting

    assert res.model == GLM
    assert _counter(GLM, "load") == before_load + 1, "the pick keeps its own reason"
    assert _counter(DS, "unhealthy") == before_unhealthy + 1, "the excluded model is named"


async def test_unhealthy_exclusion_still_logs_the_info_line(env, caplog: pytest.LogCaptureFixture) -> None:
    """The INFO line stays (it carries the thread key, which the counter
    deliberately does not label) — the counter is an ADDITION, not a
    replacement for the log."""
    fake, key_id = env
    fake.order = [GLM, DS]
    _eval_state(GLM, active=False)
    _eval_state(DS, active=True)
    fake.router.cool(DS)

    with caplog.at_level("INFO", logger="pcai_llm.core.cascade"):
        await resolve_cascade(key_id, DEMAND_AWARE_ALIAS, REGISTERED, [], 100, thread_key=THREAD)
        await drain_sticky_writes()  # P3: the sticky write is fire-and-forget — flush before asserting

    assert any("excludes unhealthy" in r.message and DS in r.message for r in caplog.records)


async def test_no_unhealthy_tick_when_every_candidate_is_healthy(env) -> None:
    """The counter distinguishes "picked normally" from "picked with
    exclusions": a healthy pick ticks ONLY its own reason."""
    _fake, key_id = env
    _eval_state(GLM, active=False)
    _eval_state(DS, active=True)
    before = _counter(DS, "unhealthy")

    res = await resolve_cascade(key_id, DEMAND_AWARE_ALIAS, REGISTERED, [], 100, thread_key=THREAD)
    await drain_sticky_writes()  # P3: the sticky write is fire-and-forget — flush before asserting

    assert res.model == DS
    assert _counter(DS, "unhealthy") == before, "nothing was excluded"


async def test_no_unhealthy_tick_when_nothing_can_serve(env) -> None:
    """All candidates unhealthy -> the pick is SKIPPED entirely (no head
    decision was made), so no unhealthy tick either: the counter records
    EXCLUSIONS FROM A RANKING, not the absence of one. The INFO line names
    the skip."""
    fake, key_id = env
    _eval_state(GLM, active=True)
    _eval_state(DS, active=True)
    fake.router.cool(GLM)
    fake.router.cool(DS)
    before_glm = _counter(GLM, "unhealthy")
    before_ds = _counter(DS, "unhealthy")

    res = await resolve_cascade(key_id, DEMAND_AWARE_ALIAS, REGISTERED, [], 100, thread_key=THREAD)
    await drain_sticky_writes()  # P3: the sticky write is fire-and-forget — flush before asserting

    assert res.ok and res.model == GLM, "the profile order stands"
    assert _counter(GLM, "unhealthy") == before_glm
    assert _counter(DS, "unhealthy") == before_ds


def test_unhealthy_is_in_the_shared_reason_vocabulary() -> None:
    """The reason the cascade now emits is IN the constant beside the
    counter (one place defines the contract)."""
    from pcai_llm.core import metrics

    assert "unhealthy" in metrics.CASCADE_PICK_REASONS


# ---------------------------------------------------------------------------
# 11c. /v1/models advertises the alias pair
# ---------------------------------------------------------------------------


class _ModelsKey:
    """The KeyRecord surface list_models reads (id / allowed_models)."""

    def __init__(self, allowed: list[str] | None = None) -> None:
        self.id = uuid.uuid4()
        self.user_id = uuid.uuid4()
        self.allowed_models = allowed or []


async def _order_for(key_id: Any) -> list[str]:
    """The profile ordering list_models reads (a coroutine, as the real
    profile_order_for_key is)."""
    return [GLM, DS]


async def test_models_advertises_both_aliases_with_distinguishing_markers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both aliases are advertised, under the same callable-candidate
    condition, with `auto` FIRST and its entry shape UNCHANGED (a real test
    asserts that shape), and the new entry marked `demand_aware: True` so a
    client can tell them apart without knowing the names."""
    monkeypatch.setattr(routes_proxy.model_router, "models", lambda: [GLM, DS])
    monkeypatch.setattr(routes_proxy, "profile_order_for_key", _order_for)

    res = await routes_proxy.list_models(_ModelsKey())
    ids = [m["id"] for m in res["data"]]
    assert ids == ["auto", DEMAND_AWARE_ALIAS, GLM, DS], "the pair leads, auto first"

    auto_entry, aware_entry = res["data"][0], res["data"][1]
    # `auto` keeps its EXACT pre-split shape: no extra marker.
    assert auto_entry["pcai"] == {"type": "cascade", "alias": True, "order": [GLM, DS]}
    # The new entry is the same shape plus the marker.
    assert aware_entry["pcai"] == {
        "type": "cascade",
        "alias": True,
        "order": [GLM, DS],
        "demand_aware": True,
    }
    assert aware_entry["object"] == "model" and aware_entry["owned_by"] == "pcai-llm-gateway"


async def test_models_omits_both_aliases_when_nothing_is_callable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The SAME condition gates both: no callable ordered candidate -> no
    alias entries at all (an alias with no resolvable model would 404)."""
    monkeypatch.setattr(routes_proxy.model_router, "models", lambda: [GLM, DS])
    monkeypatch.setattr(routes_proxy, "profile_order_for_key", _order_for)

    res = await routes_proxy.list_models(_ModelsKey(allowed=["other"]))
    assert [m["id"] for m in res["data"]] == [], "neither alias is advertised"


async def test_models_omits_both_aliases_without_an_ordering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(routes_proxy.model_router, "models", lambda: [GLM, DS])

    async def no_order(key_id):
        return []

    monkeypatch.setattr(routes_proxy, "profile_order_for_key", no_order)
    res = await routes_proxy.list_models(_ModelsKey())
    assert [m["id"] for m in res["data"]] == [GLM, DS], "no ordering -> no aliases"


# ---------------------------------------------------------------------------
# 11. OPERATOR EXCLUSION LIST — demand_aware_excluded_models: the head never
#     lands on an excluded model (sticky included); the walk still reaches
#     it as the last rung ("Qwen as budget overflow", 2026-10 decision).
# ---------------------------------------------------------------------------


async def test_excluded_model_never_wins_the_head(env, monkeypatch: pytest.MonkeyPatch) -> None:
    """Qwen idle (rank 0) + healthy would normally WIN; the exclusion list
    demotes it — the head goes to the best-ranked eligible model."""
    _, key_id = env
    monkeypatch.setattr(settings, "demand_aware_excluded_models", [QWEN])
    _eval_state(QWEN, active=True)  # idle -> would rank 0 and win
    _eval_state(GLM, active=False)  # loaded
    _eval_state(DS, active=True)  # idle + eligible -> the head

    ordered = await cascade_mod._reorder_head(key_id, THREAD, [QWEN, GLM, DS], ROSTER)
    assert ordered[0] == DS, "the excluded model must never win the demand-aware head"
    assert ordered == [DS, GLM, QWEN], "excluded model demoted to the tail, nobody dropped"


async def test_excluded_sticky_head_is_dropped_and_repicked(env, monkeypatch: pytest.MonkeyPatch) -> None:
    """A sticky record on an excluded model (written before the operator
    added the exclusion) must not keep heading the conversation there."""
    fake, key_id = env
    monkeypatch.setattr(settings, "demand_aware_excluded_models", [QWEN])
    fake.rows[(key_id, THREAD)] = QWEN  # stale sticky: excluded NOW
    _eval_state(QWEN, active=True)
    _eval_state(GLM, active=False)
    _eval_state(DS, active=True)

    res = await resolve_cascade(key_id, DEMAND_AWARE_ALIAS, ROSTER, [], 100, thread_key=THREAD)
    await drain_sticky_writes()  # P3: the sticky write is fire-and-forget — flush before asserting
    assert res.ok and res.model == DS, "the excluded sticky head is dropped and re-picked"
    assert fake.rows[(key_id, THREAD)] == DS, "the fresh eligible pick is what sticks"


async def test_every_candidate_excluded_degrades_to_profile_order(env, monkeypatch: pytest.MonkeyPatch) -> None:
    """A list excluding EVERY candidate must never brick admission: the head
    pick has no opinion, the list is returned unchanged (plain profile
    order), no stickiness written."""
    fake, key_id = env
    monkeypatch.setattr(settings, "demand_aware_excluded_models", [GLM, DS, QWEN])
    _eval_state(GLM, active=True)
    _eval_state(DS, active=True)

    ordered = await cascade_mod._reorder_head(key_id, THREAD, [GLM, DS, QWEN], ROSTER)
    assert ordered == [GLM, DS, QWEN], "unchanged list — the feature self-disables"
    assert (key_id, THREAD) not in fake.rows, "no sticky written on a no-opinion pick"


async def test_exclusion_leaves_legacy_auto_untouched(env, monkeypatch: pytest.MonkeyPatch) -> None:
    """The exclusion is a DEMAND-AWARE-ONLY demotion: a plain `auto` request
    walks the profile order verbatim — Qwen first in the order is reached
    first if it has headroom, exactly as the legacy walk always did."""
    fake, key_id = env
    fake.order = [QWEN, GLM, DS]
    monkeypatch.setattr(settings, "demand_aware_excluded_models", [QWEN])

    res = await resolve_cascade(key_id, "auto", ROSTER, [], 100, thread_key=THREAD)
    await drain_sticky_writes()  # P3: the sticky write is fire-and-forget — flush before asserting
    assert res.ok and res.model == QWEN, "the plain auto walk ignores the exclusion list"
