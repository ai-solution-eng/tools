"""Profile-based budget cascade resolution.

A profile carries an ordered model list (`model_order`). When a request
arrives WITHOUT a pinned model (or with an exhausted one), the cascade picks
the first model in the ordering that has budget headroom. The ordering is
the admin's explicit sign-off on the capability trade (70B → 8B is a
quality change, not just a capacity one); the response's `model` field
reports what actually served, and every ledger row records it.

Resolution rules (deliberate, documented):
- PINNED requests (an explicit, registered, allowed model in the body)
  NEVER cascade — pinning is how a client says "I need this specific
  model". Cascade applies only to exhausted models and aliases.
- Alias models: a model name in the body that is NOT registered resolves
  through the profile ordering instead of 404ing (enables client-side
  names like "auto" or "default").
- Headroom check = the same ledger-derived check_budgets the direct path
  uses, so cascade fallthrough is automatic and stateless: each request
  independently picks the first candidate with headroom; no switch-over
  event exists to manage.
- TWO ALIASES, ONE WALK (operator decision, 2026-10-04). The load-aware
  head is OPT-IN BY NAME, so enabling the feature can never change what an
  existing client's request means:
    * ``auto`` — the LEGACY pure profile-order cascade, byte-identical to
      the pre-feature behavior: no sticky read, no sticky write, no load
      ranking, no health gate, no head-pick counter. This is what `auto`
      has always meant, and what it still means.
    * ``auto-demand-aware`` (``DEMAND_AWARE_ALIAS``) — the SAME budget walk
      with the load-aware head engaged: the entry picks its HEAD model by
      live off-peak state at conversation START, and the choice is STICKY
      for the conversation ("if it went to GLM, stay with GLM; if GLM is
      under heavy sustained GPU load, go to Deepseek"). A sticky record for
      the conversation wins; on a miss the head is ranked by
      ``offpeak.model_active`` and recorded.
  WHY the split: (1) OPT-IN PER CLIENT — a client opts into the new
  behavior by switching the name, so a fleet-wide settings flip cannot
  silently re-route anyone; (2) ROLLBACK IS A NAME SWITCH — pointing a
  client back at ``auto`` restores the legacy cascade immediately, with no
  redeploy and no config change. The head is a REORDER of the candidate
  list — the budget walk, gates, 429 failover and relay fallback tail are
  untouched by it. Feature off (default) or no thread key -> the
  demand-aware alias resolves byte-for-byte like the legacy one (the same
  inertness gate as before).
- HEALTH GATE (defect fix, G2 live 2026-10-04): the load-aware head ranks
  only SERVABLE-AND-HEALTHY candidates. Idle-because-BROKEN is
  indistinguishable from idle-because-FREE to the off-peak signal — a model
  whose upstream 400s every call still reads 0.00% GPU util, so it ranked
  0 (best) and every NEW conversation was headed to the failing model. A
  candidate whose pool has no available endpoint (every endpoint
  breaker-cooling or absent) is now excluded from the ranking and demoted
  behind the healthy candidates; a sticky head that has become unhealthy is
  dropped and re-picked exactly like one that left the registered/allowed
  set. The health predicate is the codebase's ONE pool-availability idiom
  (``Pool.healthy()``, as used by ``routes_proxy._routing_available`` and
  ``_next_fallback_candidate``). If NO candidate is healthy the list is
  returned untouched — the walk below surfaces the real error, so the head
  pick can never make things worse.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field

from ..config import settings
from ..db import db
from ..routing.router import router as model_router
from . import metrics, offpeak, stickiness
from .budgets import GateResult, check_budgets

log = logging.getLogger(__name__)

# The ONE source of truth for the load-aware alias name (module docstring:
# "two aliases, one walk"). The legacy `auto` name is deliberately NOT a
# constant here — it is the absence of this name, so no caller can
# accidentally widen the feature to every alias again. Imported by
# routes_proxy (the /v1/models advertisement) and the tests.
DEMAND_AWARE_ALIAS = "auto-demand-aware"


@dataclass
class Resolution:
    ok: bool
    model: str = ""  # the model that should serve
    reason: str = ""  # denial explanation for the 429 detail
    attempts: list[dict] | None = None  # per-candidate outcome, for the 429 body
    # The budget gate ALREADY evaluated for the winning model (perf audit:
    # _run_gates re-ran the identical check — one wasted gate + its
    # round-trips on every pinned request). Carried so the caller can skip
    # the recheck; None when no gate ran (direct no-profile path).
    gate: GateResult | None = None
    # The profile's model_order as resolved (empty when no ordering exists).
    # The relay's mid-loop budget failover walks this tail: a PINNED request
    # whose model dies of budget MID-STREAM continues on the next ordered
    # model — the same promotion this module applies at admission when the
    # pinned model has no headroom ("budget exhaustion on the pinned model
    # promotes the request to the ordering"), just discovered later.
    order: list[str] = field(default_factory=list)


_sticky_writes: set[asyncio.Task[None]] = set()
# Bound the detached-write set (F6, cross-review 2026-10-16): under a
# pathological write stall the set grows one task per head pick. The canonical
# capture pattern this copies carries the same guard (_CAPTURE_TASKS_MAX) —
# above the bound, drop the OLDEST task (cancel it; set_sticky is idempotent
# upsert, and a lost pick write just re-picks next turn).
_STICKY_WRITES_MAX = 1000


def _sticky_write_later(key_id: uuid.UUID, thread_key: str | None, model: str) -> None:
    """Fire-and-forget set_sticky with a strong-ref leash.

    P3 (perf round 2026-10-16): the pick is already made and observable when
    this runs — awaiting the upsert put a serialized round-trip ahead of the
    first upstream byte (TTFT path). Same pattern as the canonical capture at
    routes_proxy.py:2475: the strong-ref set keeps the task alive against GC,
    the done-callback discards it when done, and set_sticky's never-raises
    contract means the only exception a callback can see is teardown
    cancellation (which is fine to drop).
    """
    while len(_sticky_writes) >= _STICKY_WRITES_MAX:
        oldest = next(iter(_sticky_writes))
        _sticky_writes.discard(oldest)
        oldest.cancel()
    task = asyncio.create_task(stickiness.set_sticky(key_id, thread_key, model))
    _sticky_writes.add(task)
    task.add_done_callback(_sticky_writes.discard)


async def drain_sticky_writes() -> None:
    """Await all pending fire-and-forget sticky writes.

    Test support (and a clean-shutdown convenience): the production path is
    deliberately detached (P3, TTFT), so tests that assert on the store flush
    this first. Idempotent; a no-op when nothing is pending."""
    pending = set(_sticky_writes)
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


async def profile_order_for_key(key_id: uuid.UUID) -> list[str]:
    """The key's profile model_order; empty when unprofiled or no cascade."""
    row = await db.fetchrow(
        """
        SELECT p.model_order FROM api_keys k
        JOIN users u ON u.id = k.user_id
        JOIN profiles p ON p.id = u.profile_id
        WHERE k.id = $1
        """,
        key_id,
    )
    return list(row["model_order"]) if row else []


def classify_request(model: str, registered: set[str], allowed: list[str]) -> str:
    """Classify the body's model name for resolution. Pure function.

    Returns one of:
      'pinned'    — registered + allowed → serve directly, never cascade
      'forbidden' — registered but not in key.allowed_models → 403
      'alias'     — not registered → resolve through profile ordering
                    (whether an ordering exists is decided by the caller,
                    which knows the profile; the classifier is deliberately
                    ordering-agnostic)
      'unknown'   — not registered and no cascade path applies → 404
    """
    if model in registered:
        if allowed and model not in allowed:
            return "forbidden"
        return "pinned"
    return "alias"


@dataclass(frozen=True)
class HeadPick:
    """One load-aware head decision (pure result of ``pick_load_aware``)."""

    model: str
    reason: str  # load | default (sticky picks are decided by _reorder_head)
    ranks: list[tuple[str, int]]  # (model, rank) in the order handed in


def load_rank(candidates: list[str]) -> list[tuple[str, int]]:
    """Rank cascade candidates by the off-peak signal (rule 1). Pure.

    model ACTIVE (low demand) -> rank 0 (preferred); not-active -> rank 1;
    no per-model eval configured / evaluator off -> NO SIGNAL, rank 0. The
    returned list keeps the input order, which is the tie-break the pick
    relies on (rule 5: ties preserve the profile's configured order).
    """
    return [(m, 1 if offpeak.model_active(m) is False else 0) for m in candidates]


def pick_load_aware(candidates: list[str]) -> HeadPick:
    """The load-aware head across ``candidates`` (profile order). Pure.

    The first candidate at the best rank wins; ties keep the profile order
    (rule 5). ``reason`` is "load" when the load-rank path produced the head
    because at least one candidate carried a real signal, and "default" when
    NO candidate did (no per-model eval configured, or the evaluator is off)
    — reason=default is therefore exactly the feature-inert case, where the
    head is byte-identical to the pre-feature profile head.
    """
    if not candidates:
        return HeadPick("", "default", [])
    ranks = load_rank(candidates)
    has_signal = any(offpeak.model_active(m) is not None for m in candidates)
    best = min(r for _, r in ranks)
    picked = next(m for m, r in ranks if r == best)
    return HeadPick(picked, "load" if has_signal else "default", ranks)


def _ranks_text(ranks: list[tuple[str, int]]) -> str:
    return ",".join(f"{m}={r}" for m, r in ranks)


def _pool_servable(model: str) -> bool:
    """Is this model's pool currently servable (has an available endpoint)?

    The codebase's ONE pool-availability idiom — ``model_router.pool(m)``
    plus ``Pool.healthy()`` (endpoints not breaker-cooled) — the same
    predicate ``routes_proxy._routing_available`` (routes_proxy.py:333) and
    ``_next_fallback_candidate`` (routes_proxy.py:880) gate on. A name with
    NO pool in the live router snapshot cannot serve either (the proxy 404s
    it), so absence is UNHEALTHY, not unknown — that is what "one definition
    of healthy" means here.

    Deliberately synchronous: the breaker state (``Endpoint.cooldown_until``,
    router.py:52) is in-process memory, so there is no IO to await.
    """
    pool = model_router.pool(model)
    return pool is not None and bool(pool.healthy())


def _head_first(head: str, candidates: list[str], unhealthy: list[str]) -> list[str]:
    """The decided head first, then the remaining candidates in their
    existing relative order, with the UNHEALTHY ones demoted to the very
    end. Pure.

    Never drops anyone: the budget walk and the 429 failover may still
    legitimately use an unhealthy candidate (a breaker cools for seconds —
    ``settings.endpoint_cooldown_s``), and changing the candidate SET would
    change Resolution semantics. With ``unhealthy`` empty this is
    byte-for-byte the pre-health-gate reorder.
    """
    bad = set(unhealthy)
    return [head, *(m for m in candidates if m != head and m not in bad), *unhealthy]


async def _reorder_head(
    key_id: uuid.UUID,
    thread_key: str,
    candidates: list[str],
    registered: set[str],
) -> list[str]:
    """Rule 3: this conversation's `auto` head, applied as a REORDER.

    The sticky READ happens here, AFTER the health gate (F5/F8 fixes,
    cross-review 2026-10-16): the pinned / legacy-auto / feature-off paths
    never touch the store (their "never consults stickiness" contracts are
    restored), and an all-unhealthy turn consults nothing either. The P3
    TTFT win is retained by the fire-and-forget WRITE below; the read stays
    one standalone round-trip on the one path that can act on it. The
    fused-read variant (profile_order_and_sticky) was removed in the F5
    remediation: re-wiring a fused sticky parameter here would silently
    re-violate the never-consults-stickiness contracts on the pinned /
    legacy-auto / feature-off paths.

    Sticky hit -> that model first (it must still be registered AND in the
    candidate list — i.e. key-allowed and in the profile ordering — AND
    healthy; if it fell out, the record is dropped and we pick fresh).
    Sticky miss -> rank the SERVABLE-AND-HEALTHY candidates by the off-peak
    signal, record the pick, put it first.

    HEALTH GATE (defect fix, G2 live 2026-10-04): the ranking only ever sees
    candidates whose pool has an available endpoint. Idle-because-BROKEN is
    indistinguishable from idle-because-FREE to the off-peak signal — a
    model whose upstream 400s every call still reads 0.00% GPU util, so it
    ranked 0 (best) and every NEW conversation was headed to the failing
    model. Unhealthy candidates are excluded from the ranking and demoted
    behind the healthy ones; if NONE is healthy the list is returned
    UNCHANGED (the walk below surfaces the real error — the head pick must
    never make things worse).

    NEVER raises and never changes the candidate SET (only its order): any
    fault in the stickiness store, the signal, or the health probe degrades
    to the profile order. Unregistered candidates are never picked as head
    (they cannot serve — the walk below skips them anyway), so a stored head
    always names a model that can actually answer.

    EXCLUSION LIST (settings.demand_aware_excluded_models, operator decision
    2026-10): excluded models are demoted OUT of the head-eligibility set —
    they never win the pick, sticky or ranked — but they STAY in the
    returned candidate list (order preserved), so the budget walk below
    still reaches them as a last rung and `auto`/failover semantics are
    untouched. If EVERY candidate is excluded the head pick has no opinion:
    the list is returned unchanged (feature self-disables; admission never
    bricks). An excluded STICKY head is dropped and re-picked — the
    exclusion is a live operator decision, not a stale preference.
    """
    servable = [m for m in candidates if m in registered]
    if not servable:
        # Nothing in the ordering can serve: leave the walk exactly as it
        # is today (it will report the not_registered attempts) and write
        # no stickiness.
        return candidates

    excluded = {m for m in settings.demand_aware_excluded_models}
    eligible = [m for m in servable if m not in excluded]
    if excluded & set(servable):
        log.info(
            "cascade: demand-aware head excludes operator-listed model(s) %s (thread %s)",
            ",".join(sorted(excluded & set(servable))),
            thread_key[:8],
        )
    if not eligible:
        # Every candidate is operator-excluded: the head pick has no
        # opinion it could act on. Return UNCHANGED (plain profile order),
        # touch no stickiness — the exclusion list must never brick
        # admission, and the walk below serves the first candidate with
        # headroom exactly as the legacy `auto` would.
        return candidates

    sticky: str | None = None

    # --- health gate: partition BEFORE ranking (rules 2-4) ---------------
    healthy_set = {m for m in eligible if _pool_servable(m)}
    healthy = [m for m in eligible if m in healthy_set]
    unhealthy = [m for m in eligible if m not in healthy_set]
    if not healthy:
        # Every ELIGIBLE candidate is breaker-cooling / absent: the head
        # pick has no opinion it could act on. Leave the list UNCHANGED and
        # touch no
        # stickiness — a record that is merely unhealthy NOW is valid again
        # once the breaker cools (cooldowns are seconds, not a revocation
        # like the registry/ACL sets), and dropping it would lose the
        # conversation's head for good. The walk below reports the real
        # error, so the pick never makes things worse.
        log.info(
            "cascade: head pick skipped — no healthy candidate (thread %s, candidates %s)",
            thread_key[:8],
            ",".join(eligible),
        )
        return candidates
    if sticky is None and thread_key:
        sticky = await stickiness.get_sticky(key_id, thread_key)
    if unhealthy:
        # Observability (rule 7): the exclusion is recorded on the counter
        # with its own reason value (metrics.CASCADE_PICK_REASONS carries
        # "unhealthy" — the follow-up the health-gate fix deferred while the
        # vocabulary was pinned at three values). This is an ADDITIONAL tick
        # beside the pick's own reason, never a replacement: one tick per
        # DECISION with the vocabulary that says WHY the head won
        # (sticky/load/default), plus one per EXCLUSION EVENT with
        # reason=unhealthy, so a dashboard can alert on the exclusion rate
        # without a second counter. The counter's contract already allows
        # any reason string (unknown values are recorded verbatim), and the
        # `picked` label is the EXCLUDED model here — a real registered
        # name, so cardinality stays bounded (metrics.py design rule 3).
        # The INFO line stays: it names the thread, which the counter
        # deliberately does not label.
        for _excluded in unhealthy:
            metrics.cascade_head_pick(_excluded, "unhealthy")
        log.info(
            "cascade: head pick excludes unhealthy model(s) %s (thread %s)",
            ",".join(unhealthy),
            thread_key[:8],
        )

    # P3/F8 (cross-review 2026-10-16): the sticky value comes from the
    # standalone get_sticky read BELOW — deliberately after the no-healthy
    # early return, so the store is not even consulted when nothing can
    # serve. A None here (and from the read) means "no sticky head"; the
    # drop-and-repick branches below are unchanged.
    if sticky and sticky in excluded:
        # Operator-excluded sticky head: the exclusion is a LIVE decision
        # (stricter than staleness — a model that merely cooled down is
        # valid again in seconds, an excluded one is out until the operator
        # says otherwise), so drop the record and re-pick from the eligible
        # set. Without this, a sticky record written before the operator
        # added the exclusion would keep heading the conversation to the
        # excluded model for the thread's lifetime.
        await stickiness.drop_sticky(key_id, thread_key)
        log.info(
            "cascade: sticky head %r is operator-excluded from the demand-aware head (thread %s) — dropped, re-picking",
            sticky,
            thread_key[:8],
        )
        sticky = None
    if sticky and sticky in healthy_set:
        if sticky != candidates[0] or unhealthy:
            stickiness.observe_head_pick(sticky, "sticky", thread_key, _ranks_text(load_rank(healthy)))
            return _head_first(sticky, candidates, unhealthy)
        # Already the profile head: no reorder needed, no row rewrite.
        stickiness.observe_head_pick(sticky, "sticky", thread_key, f"{sticky}=0")
        return candidates
    if sticky:
        # The stored model is no longer registered / key-allowed / in the
        # ordering, OR its pool has no available endpoint: the live sets are
        # authoritative, so drop the record and re-pick fresh rather than
        # serving a stale preference. Same disposition for both cases —
        # idle-because-broken must never pin a conversation to a model that
        # cannot answer.
        await stickiness.drop_sticky(key_id, thread_key)
        if sticky in servable:
            log.info(
                "cascade: sticky head %r unhealthy (no available endpoint, thread %s) — dropped, re-picking",
                sticky,
                thread_key[:8],
            )
        else:
            log.info(
                "cascade: sticky head %r no longer allowed/registered (thread %s) — dropped, re-picking",
                sticky,
                thread_key[:8],
            )

    pick = pick_load_aware(healthy)
    if pick.reason == "default":
        # NO SIGNAL ANYWHERE (no per-model eval configured, or the evaluator
        # is off): the feature is INERT — the brief's critical backwards-
        # compat gate. Do not reorder and do not record a head: writing a
        # row here would pin the conversation to a model chosen with no
        # signal, outliving the inertness (a later-enabled signal could
        # never move the conversation again). The decision is still
        # observable as reason=default. The return is BYTE-IDENTICAL to the
        # pre-health-gate code: the health partition must not reorder an
        # inert path.
        stickiness.observe_head_pick(pick.model, "default", thread_key, _ranks_text(pick.ranks))
        return candidates
    _sticky_write_later(key_id, thread_key, pick.model)
    stickiness.observe_head_pick(pick.model, pick.reason, thread_key, _ranks_text(pick.ranks))
    if pick.model == candidates[0] and not unhealthy:
        return candidates
    # Operator-excluded models are demoted to the very end alongside the
    # unhealthy ones (same _head_first tail): the head wins, everyone else
    # keeps profile order, the excluded rungs trail — reachable by the
    # budget walk only when every earlier candidate is exhausted, which is
    # exactly the "Qwen as last resort" intent.
    return _head_first(pick.model, candidates, unhealthy + [m for m in servable if m in excluded])


async def resolve_cascade(
    key_id: uuid.UUID,
    requested: str,
    registered: set[str],
    allowed_models: list[str],
    est_tokens: int,
    thread_key: str | None = None,
) -> Resolution:
    """Pick the serving model: pinned direct, else first candidate with headroom.

    ``requested`` is the body's model name AND the feature's engagement
    switch: the load-aware head engages only when it names
    ``DEMAND_AWARE_ALIAS`` (see the module docstring's "two aliases, one
    walk"). Every other alias — ``auto`` above all — resolves through the
    legacy profile-order walk, byte-identical to the pre-feature code.

    ``thread_key`` (core.canonical.thread_key) is the conversation's stable
    fingerprint — the SECOND switch on that path: None (no user-message
    seed, non-chat caller) leaves the resolution byte-for-byte as it was
    before the feature existed, as does the feature being off
    (settings.load_aware_cascade).
    """
    order = await profile_order_for_key(key_id)
    kind = classify_request(requested, registered, allowed_models)

    if kind == "forbidden":
        return Resolution(False, reason=f"key is not allowed to use model {requested!r}")

    # No cascade ordering → 0.1 direct semantics (pinned serves as-is;
    # alias without an ordering is a 404).
    if not order:
        if kind == "pinned":
            return Resolution(True, model=requested)
        return Resolution(
            False,
            reason=f"model {requested!r} is not registered and key has no profile ordering",
        )

    # Pinned + ordering: the pinned model serves whenever it has headroom —
    # pinning is an explicit choice ("I need THIS model"). Only budget
    # exhaustion on the pinned model promotes the request to the ordering
    # (the pinned model itself is re-walked first; with a fresh gate this
    # deterministically falls through to the next candidate).
    #
    # PINNED NEVER TOUCHES STICKINESS — no read, no write: the client named
    # a model, so there is no head to pick (brief rule 3, first bullet).
    if kind == "pinned":
        gate = await check_budgets(key_id, requested, est_tokens)
        if gate.ok:
            return Resolution(True, model=requested, gate=gate, order=order)

    # Cascade walk: candidates in profile order, intersected with the key's
    # allowed_models (an ordering entry the key can't use is skipped, not a
    # denial — the ordering is a preference, the ACL is a hard boundary).
    candidates = [m for m in order if not allowed_models or m in allowed_models]
    if not candidates:
        return Resolution(False, reason="profile ordering grants no models this key may use")

    # Load-aware head (opt-in, DEMAND-AWARE ALIAS ONLY):
    # reorder the candidate list so the conversation's sticky/load-ranked
    # head is tried FIRST. Everything below is untouched — the budget walk,
    # the gate it carries, the caller's 429 failover (which follows THIS
    # order) and the relay fallback tail (which follows the profile
    # `order` this Resolution still returns unchanged).
    #
    # THE GATE (operator decision, 2026-10-04): the requested NAME must be
    # the demand-aware alias. Before this, the condition was `kind ==
    # "alias"` — i.e. EVERY alias, including the legacy `auto` — so turning
    # the feature on silently changed what `auto` meant for every existing
    # client. It is now opt-in per client and rollback is a name switch.
    # The legacy `auto` path therefore falls straight through to the walk
    # below, byte-identical to the pre-feature resolution (no sticky read,
    # no sticky write, no pool lookup, no counter tick).
    if settings.load_aware_cascade and thread_key and kind == "alias" and requested == DEMAND_AWARE_ALIAS:
        try:
            # F5 fix (cross-review 2026-10-16): the sticky half is consumed
            # ONLY on this path, so stickiness is consulted only here — the
            # pinned / legacy-auto / feature-off paths keep their "never
            # touches stickiness" contract exactly. The ordering itself stays
            # the one plain read at the top of resolve_cascade (no second
            # profile round-trip, no two-read consistency question).
            candidates = await _reorder_head(key_id, thread_key, candidates, registered)
        except Exception:  # pragma: no cover - _reorder_head is fail-open by construction
            log.warning("cascade: load-aware head failed — using the profile order", exc_info=True)

    attempts: list[dict] = []
    for candidate in candidates:
        if candidate not in registered:
            attempts.append({"model": candidate, "status": "not_registered"})
            continue
        gate = await check_budgets(key_id, candidate, est_tokens)
        if gate.ok:
            return Resolution(True, model=candidate, attempts=attempts, gate=gate, order=order)
        attempts.append({"model": candidate, "status": "budget_exhausted", "reason": gate.reason})

    return Resolution(
        False,
        reason="all models in profile ordering are budget-exhausted",
        attempts=attempts,
    )
