"""Budget + rate-limit engine.

Two independent gates, both evaluated BEFORE an upstream call and settled
AFTER it:

1. Period budgets (day/week/month/quarter/year, per key and optionally per model) —
   ledger-derived: spent = SUM over usage_ledger in the window,
   FRACTION-WEIGHTED (each row's billing_fraction — off-peak billing keeps
   the ledger TRUE while budgets consume tokens*fraction / cost*fraction),
   and LINEAGE-SCOPED: a key's spend_ancestors (set by every rotate) fold
   into the join key, so in-window spend survives rotation and rotating
   can never reset a period budget. No read-modify-write counters, so no
   check-then-act race; concurrent requests may overshoot a limit by their
   own in-flight usage, which is the correct trade (strictness would
   require serializing all calls).
2. Rate limits (RPM/TPM) — fixed windows in the rate_windows table,
   incremented atomically on accept. These exist to stop bursts (a monthly
   token budget does not stop a 10k-RPM runaway agent).
"""

from __future__ import annotations

import json
import logging
import math
import time
import uuid
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg

from ..config import settings
from ..db import db

# Metrics ride the SAME choke points the ledger uses (settle/check_budgets)
# so a counter and its ledger row can never disagree — core.metrics design
# rule 4. prometheus_client is optional: absent, the metrics module hands
# out no-op shims and these calls are free.
from . import metrics as _metrics

# The team envelope lives in its own module (credential + envelope + console
# queries); this import is safe in this direction because core.teams imports
# the two pure helpers it needs FROM HERE lazily, at call time.
from . import teams

log = logging.getLogger(__name__)


class BudgetExceeded(Exception):
    def __init__(self, scope: str, period: str, limit: int, spent: int) -> None:
        self.scope, self.period = scope, period
        self.limit, self.spent = limit, spent
        super().__init__(f"budget exceeded: {scope} {period} limit={limit} spent={spent}")


class RateLimited(Exception):
    def __init__(self, kind: str, limit: int) -> None:
        self.kind, self.limit = kind, limit
        super().__init__(f"rate limit exceeded: {kind} limit={limit}")


VALID_PERIODS = frozenset({"day", "week", "month", "quarter", "year"})

# The same periods in the fixed order the spend SUMs' CASE binds them in —
# a frozenset has no order, and the numbered placeholders below depend on ONE
# canonical sequence (day first, year last) shared by _budget_sums and
# budget_status_many. Reordering this tuple renumbers the placeholders with it.
_WINDOW_PERIODS: tuple[str, ...] = ("day", "week", "month", "quarter", "year")

# Ledger statuses whose tokens and cost are REAL upstream spend (audit r5 #9).
# Every row where the upstream actually ran must be PRICED and COUNTED:
# 'guardrail_blocked' is a post-call POLICY refusal — the GPU time was already
# spent (the proxy settles it with the real prompt/completion tokens), so
# pricing only 'ok' stored those tokens with cost_usd NULL and the SUMs only
# counting ('ok','error') let a tenant with a blocklist guardrail burn
# unlimited prefill for free, with no budget gate and no chargeback line ever
# seeing it (docs API.md §"real upstream spend" / FEATURES.md promise the
# opposite). 'cache_hit' stays OUT deliberately: those rows are served from
# our cache (no upstream spend) and the admission refusals never called an
# upstream at all. ONE set, consumed by settle()'s price branch and by every
# budget SUM (core.teams carries the same predicate for the team envelope).
_COSTED_STATUSES: tuple[str, ...] = ("ok", "error", "guardrail_blocked")
# The same set as a SQL IN list. Interpolated rather than parameterised
# because a list cannot bind into IN (...); the values are this module
# constant, never request data.
_COSTED_SQL = ", ".join(f"'{s}'" for s in _COSTED_STATUSES)

# P10: an estimate tag maps to the unit its number is a PROXY for. The settle
# path prices a tagged row over this EFFECTIVE unit (never through the
# per-1M-TOKEN arithmetic unless the STORED model unit is 'token'), so a
# per-1M-SECOND rate can never ride the token arithmetic (the silent 3x
# over-bill) and chars are never billed as seconds. Unknown tags stay on the
# legacy path (never invent a unit — and thus a rate — of their own).
_EFFECTIVE_UNIT_BY_TAG: dict[str, str] = {
    "char_estimate": "char",
    "second_estimate": "second",
    "token_estimate": "token",
}
# A real CHECK-vocabulary unit rides as its own effective unit verbatim.
for _u in ("token", "char", "second", "image", "request"):
    _EFFECTIVE_UNIT_BY_TAG.setdefault(_u, _u)

# billable_units column ceiling: NUMERIC(18,6) == 12 integer digits + 6
# decimals (db/schema.py). Validating HERE — before the INSERT — is what keeps
# an oversized quantity from raising NumericValueOutOfRangeError and losing
# the immutable billing row for spend the upstream already served.
_BILLABLE_UNITS_MAX = 999_999_999_999.999999

# cost_usd column ceiling: NUMERIC(12,6) == 6 integer digits + 6 decimals
# (db/schema.py). Same row-loss cliff as the quantity bound above, cost side.
_COST_USD_MAX = 999_999.999999

# Column bounds, mirrored from db/schema.py. These are NOT cosmetic: a
# limit above them passes every shape check and then raises
# NumericValueOutOfRangeError at the INSERT — a 500 on a key-creation site
# (create-key, redeem, SSO mint) whose contract is 400. Validating the
# bound HERE keeps that a client error with a readable message.
#   budgets.limit_tokens       BIGINT        (max 2**63 - 1)
#   cost_budgets.limit_usd     NUMERIC(12,4) (max 99,999,999.9999)
MAX_LIMIT_TOKENS = 2**63 - 1
MAX_LIMIT_USD = 99_999_999.9999


def split_preset_budgets(
    entries: Iterable[Mapping[str, Any]],
) -> tuple[list[tuple[str, str, int]], list[tuple[str, str, float]]]:
    """Validate a PROFILE's budget presets and split them by KIND.

    A preset entry is one budget: {model, period, limit_tokens} XOR
    {model, period, limit_usd} — the same exactly-one-limit contract
    POST /admin/keys/{id}/budgets enforces on a live key, applied to the
    template that gets stamped onto every key created with the profile.
    Token presets land in the budgets table, dollar presets in
    cost_budgets; ONE helper so the admin create-key seeding, the
    enrollment redeem and the SSO self-mint stamping can never disagree
    about what a valid preset is or which table a row belongs to.

    Returns (token_rows, usd_rows) as (model, period, limit) triples.
    Raises ValueError naming the offending entry — callers translate it to
    their own refusal shape (admin: 400; enroll: 400 naming the profile).

    WHY the validation lives here rather than in the pydantic models: the
    enrollment path reads presets out of profiles.defaults_json, which is
    raw JSON written by an older build (or by hand). Pydantic never sees
    those rows, and an unvalidated preset used to surface as an unhandled
    CheckViolation 500 AFTER the redeem had burned a single-use code.
    """
    token_rows: list[tuple[str, str, int]] = []
    usd_rows: list[tuple[str, str, float]] = []
    for b in entries:
        model = str(b.get("model") or "*")
        period = b.get("period")
        if period not in VALID_PERIODS:
            raise ValueError(f"budget period {period!r} must be one of {sorted(VALID_PERIODS)}")
        has_tokens = b.get("limit_tokens") is not None
        has_usd = b.get("limit_usd") is not None
        if has_tokens == has_usd:  # both or neither
            raise ValueError("each budget needs exactly one of limit_tokens / limit_usd")
        if has_tokens:
            raw = b["limit_tokens"]
            # bool is an int subclass: True would silently become 1 token.
            if not isinstance(raw, int) or isinstance(raw, bool) or raw <= 0:
                raise ValueError("limit_tokens must be a positive integer")
            if raw > MAX_LIMIT_TOKENS:
                raise ValueError(f"limit_tokens {raw} exceeds the column maximum {MAX_LIMIT_TOKENS}")
            token_rows.append((model, str(period), raw))
        else:
            raw_usd = b["limit_usd"]
            if isinstance(raw_usd, bool) or not isinstance(raw_usd, (int, float)) or raw_usd <= 0:
                raise ValueError("limit_usd must be a positive number")
            # NON-FINITE (audit F1, 2026-10-29): "nan <= 0" is False, so a NaN
            # limit passed the positivity check above and was persisted — after
            # which "spent >= nan" is ALWAYS False (the USD gate could never
            # refuse that row) and "int(spent*100/nan)" raised ValueError on
            # every admission (500). json.loads parses bare NaN/Infinity, so an
            # admin payload or a console bug reached this. Floats only: an int
            # this large is caught by the column bound below, and
            # math.isfinite(10**400) raises OverflowError.
            if isinstance(raw_usd, float) and not math.isfinite(raw_usd):
                raise ValueError("limit_usd must be a finite number")
            if raw_usd > MAX_LIMIT_USD:
                raise ValueError(f"limit_usd {raw_usd} exceeds the column maximum {MAX_LIMIT_USD}")
            usd_rows.append((model, str(period), float(raw_usd)))
    return token_rows, usd_rows


def _window_start(now: datetime, period: str) -> datetime:
    if period == "day":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "week":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return start - timedelta(days=start.weekday())
    if period == "quarter":
        # First day of the calendar quarter, 00:00 UTC: months 1-3 -> Q1 starts
        # Jan 1, 4-6 -> Apr 1, 7-9 -> Jul 1, 10-12 -> Oct 1.
        return now.replace(month=(now.month - 1) // 3 * 3 + 1, day=1, hour=0, minute=0, second=0, microsecond=0)
    if period == "year":
        return now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)  # month


@dataclass
class GateResult:
    ok: bool
    reason: str = ""
    warn: int = 0  # highest crossed warning threshold (80/95); 0 = none
    # Rate-limit window counters AFTER the increment (gaps P9): surfaced as
    # X-RateLimit-Remaining-Requests/-Tokens on admitted calls. None for
    # budget-gate results (no window counter involved).
    remaining_requests: int | None = None
    remaining_tokens: int | None = None
    # WHICH row refused (2026-10-30, per-model team pools): the period and
    # family of the budget that produced the refusal. The team overlay needs
    # them to answer "does the overflow pool for THIS limit have headroom?" —
    # each N x profile preset is its own pool, so swallowing a day-limit
    # refusal into month headroom (or a GLM refusal into Qwen headroom) would
    # be exactly the incoherence the per-model caps exist to remove. None on
    # admissions and on team-envelope refusals that are their own verdict.
    refused_period: str | None = None
    refused_kind: str | None = None  # 'tokens' | 'usd'


async def _budget_rows_exist(key_id: uuid.UUID, model: str | None) -> bool:
    """Cheap catalog probe: does THIS key have ANY budget row that the gate
    would consider? (REMEDIATION P4 — the short-circuit's predicate.)

    Two tiny EXISTS queries (one per budget family) against the indexed
    key_id columns — the usage_ledger is never touched, unlike _budget_sums'
    windowed aggregate. Model semantics match the gate exactly:
    ``model = '*' OR model = $2`` when a model is pinned; any row at all
    when the gate is unpinned (model=None, budget_status-style scans).

    Raises on DB trouble; returns False only when the DB itself asserted
    the absence of matching rows. Callers must treat every OTHER outcome
    (None-bearing fakes, unexpected scalar shapes, exceptions) as "run the
    full gate" — see check_budgets.
    """
    model_cond = "AND (model = '*' OR model = $2)" if model is not None else ""
    args: list[Any] = [key_id]
    if model is not None:
        args.append(model)
    for table in ("budgets", "cost_budgets"):
        found = await db.fetchval(
            f"SELECT EXISTS (SELECT 1 FROM {table} WHERE key_id = $1 {model_cond})",
            *args,
        )
        if found is True:
            return True
        if found is not False:
            # Not a boolean: the seam under us does not implement this probe
            # (e.g. a test fake) — signal the caller to run the full path.
            raise ValueError(f"budget EXISTS probe unsupported by the db seam ({table})")
    return False


async def _spend_ancestors(key_id: uuid.UUID) -> list[uuid.UUID]:
    """The key's rotation lineage (api_keys.spend_ancestors), read ONCE.

    Perf (audit P2-31): this read used to live INSIDE _budget_sums, so a
    budgeted admission paid the SAME PK round trip twice — the token family
    and the cost family each called _budget_sums. It is now taken once per
    check, through the _SpendLineage memo _shared_lineage scopes.

    fetchrow preferred (the row shape carries the column name); fetchval
    fallback (older seams/fakes expose only scalars). Defensive shape check (a
    fake/other DB seam returning a scalar must not 500 the gate): anything but
    a list/tuple is treated as no lineage.
    """
    _anc_val = None
    if hasattr(db, "fetchrow"):
        _anc_row = await db.fetchrow("SELECT spend_ancestors FROM api_keys WHERE id = $1", key_id)
        try:
            _anc_val = _anc_row["spend_ancestors"] if _anc_row is not None else None
        except (KeyError, IndexError):
            _anc_val = None  # row shape without the column (test seams) = no lineage
    else:
        # fetchval seam (older fakes): the scalar IS the lineage value
        _anc_val = await db.fetchval("SELECT spend_ancestors FROM api_keys WHERE id = $1", key_id)
    return list(_anc_val) if isinstance(_anc_val, (list, tuple)) else []


class _SpendLineage:
    """The key's rotation lineage, read AT MOST ONCE per gate check.

    Perf (audit P2-31): _budget_sums read spend_ancestors itself, so a budgeted
    admission paid the SAME api_keys PK lookup twice — once for the token
    family, once for the cost family. ONE memo is scoped around both calls by
    _shared_lineage.

    The read is LAZY on purpose: a caller that overrides the aggregate (a test
    seam, or a future batched implementation) must not be forced into an
    api_keys round trip it never asked for. It stays FAIL-CLOSED — a read fault
    propagates, because treating an unreadable lineage as "no ancestors" would
    silently reset a rotated key's period budget (the bypass the lineage
    exists to close).
    """

    __slots__ = ("_value", "key_id")

    def __init__(self, key_id: uuid.UUID) -> None:
        self.key_id = key_id
        self._value: list[uuid.UUID] | None = None

    async def get(self) -> list[uuid.UUID]:
        if self._value is None:
            self._value = await _spend_ancestors(self.key_id)
        return self._value


# The task-local lineage memo (see _shared_lineage): set by a check that runs
# BOTH budget families, read by _budget_sums.
_lineage_scope: ContextVar[_SpendLineage | None] = ContextVar("pcai_budget_lineage", default=None)


@contextmanager
def _shared_lineage(key_id: uuid.UUID) -> Iterator[None]:
    """Scope ONE lineage read across every _budget_sums call in this gate check.

    Perf (audit P2-31): the token family and the cost family each call
    _budget_sums, which read the same api_keys row — two identical PK round
    trips for one admission (and one /v1/models listing, via model_capacity).

    WHY a task-local scope instead of a sixth parameter: _budget_sums is a SEAM
    the suite stubs with its production signature (tests/test_budget_capacity.py
    SumsStub: "a call-site drift fails loudly here"), so the shared-lineage
    optimisation must not change its call shape. A ContextVar is per-task, so
    concurrent admissions never share a lineage.
    """
    token = _lineage_scope.set(_SpendLineage(key_id))
    try:
        yield
    finally:
        _lineage_scope.reset(token)


async def _budget_sums(
    table: str,
    limit_col: str,
    sum_col: str,
    key_id: uuid.UUID,
    model: str | None,
) -> list[asyncpg.Record]:
    """Budget rows joined to their window's ledger spend — ONE aggregate
    query per budget kind.

    The spend is FRACTION-WEIGHTED (`SUM(col * billing_fraction)`): a row
    settled inside a low-demand off-peak window consumes only its fraction
    of budget (off-peak billing, core.offpeak), while the ledger keeps TRUE
    tokens/cost for audit/display. Without the feature every row carries
    billing_fraction=1.0 and the math is identical to the plain SUM.

    The spend is LINEAGE-SCOPED (budget-carry across rotation): the key's
    spend_ancestors chain is folded into the join key
    (`l.key_id = ANY(spend_ancestors || id)`), so in-window spend survives
    a rotation — a fresh key_id would otherwise SUM zero ledger rows and
    every rotate would reset the period budget (the SSO rotation
    budget-reset bypass, fixed 2026-10-09). The window predicate is NOT
    carried: calendars reset normally, only the in-window counter survives.
    A key with an empty ancestors array sums ONLY its own rows —
    byte-identical to the pre-lineage behavior. The lineage read is ONE extra
    PK-indexed lookup (api_keys.id), taken ONCE per gate check through the
    `lineage` memo the caller supplies: reading it here cost a SECOND identical
    round trip on every budgeted admission (audit P2-31 — the old "rides the
    same aggregate query" claim was false).

    Perf audit (critical): the gate used to run one serial SUM per budget
    row (N+1 round-trips — ~14 sequential round-trips for a pinned request
    with 2 token + 2 cost budgets). The window start per period is computed
    HERE in Python (same _window_start math as before) and passed in, so
    window boundaries stay UTC-exact regardless of the DB session timezone.
    """
    now = datetime.now(UTC)
    starts = {p: _window_start(now, p) for p in _WINDOW_PERIODS}
    model_cond = "AND (b.model = '*' OR b.model = $2)" if model is not None else ""
    # Perf (audit P2-5): read spend_ancestors ONCE and pass it as a uuid[]
    # parameter. The old join built it via a correlated subselect INSIDE the
    # join predicate; the ANY(array) shape already blocks an equality probe
    # on idx_ledger_key_ts, and per-budget-row PK lookups made that strictly
    # worse.
    # Callers that run BOTH budget families open a _shared_lineage scope (audit
    # P2-31): otherwise this helper re-reads the same api_keys row per family.
    # Outside such a scope the read is local to this call, unchanged.
    _memo = _lineage_scope.get()
    ancestors = await (_memo.get() if _memo is not None else _spend_ancestors(key_id))
    args: list[Any] = [key_id]
    if model is not None:
        args.append(model)
    args += [starts[p] for p in _WINDOW_PERIODS]
    # Number the windows BEFORE appending the lineage (real-PG-verified fix:
    # the old order made the lineage the LAST param and the month window
    # reused its number — one placeholder cannot be both uuid[] and
    # timestamptz, so prepare() failed with CannotCoerceError and every
    # budgeted chat call 500d). Windows sit at n_win-4 .. n_win — five
    # periods since quarter/year joined, bound in _WINDOW_PERIODS order by
    # the CASE below (that SQL and this numbering must move together).
    n_win = len(args)
    args.append(ancestors + [key_id])
    lineage_sql = "$" + str(len(args)) + "::uuid[]"
    # Placeholder-collision guard (real-PG lesson from this wave): each
    # numbered placeholder must map to exactly ONE argument — a number
    # reused with two casts (uuid[] + timestamptz) fails only at real
    # prepare(), which no fake seam can catch.
    # Unconditional (not `assert` — stripped under python -O; S101 class):
    _lineage_num = int(lineage_sql[1:].split("::")[0])
    _win_nums = tuple(range(n_win - len(_WINDOW_PERIODS) + 1, n_win + 1))
    if _lineage_num != len(args) or _lineage_num <= n_win or len(set(_win_nums)) != len(_WINDOW_PERIODS):
        raise RuntimeError(
            f"placeholder numbering drifted: lineage=${_lineage_num} (args={len(args)}), windows={_win_nums}"
        )
    # The token SUM is cache-discounted (budget_cached_exempt_fraction); the
    # COST SUM keeps its exact legacy shape — costs.compute already prices
    # cached tokens at cached_per_m, so discounting them here too would
    # double-count the exemption.
    _expr = _budget_token_expr("l") if sum_col == "total_tokens" else "SUM(l." + sum_col + " * l.billing_fraction)"
    return await db.fetch(
        f"""
        SELECT b.model, b.period, b.{limit_col},
               -- token SUM: cache-discounted (budget_cached_exempt_fraction);
               -- cost SUM unchanged (costs.compute prices cached at cached_per_m)
               COALESCE({_expr}, 0)::float AS spent
        FROM {table} b
        LEFT JOIN usage_ledger l
          ON l.key_id = ANY({lineage_sql})
         AND l.status IN ({_COSTED_SQL})   -- cache_hit rows are FREE
         AND (l.model = b.model OR b.model = '*')
         AND l.ts >= CASE b.period
                     -- explicit casts: bare params inside a CASE fall back to
                     -- text in Postgres' type resolution → "timestamptz >= text"
                     -- one WHEN per _WINDOW_PERIODS entry, ELSE = the last one
                     -- (year); the numbering guard above cross-checks the args.
                     WHEN 'day' THEN ${n_win - 4}::timestamptz
                     WHEN 'week' THEN ${n_win - 3}::timestamptz
                     WHEN 'month' THEN ${n_win - 2}::timestamptz
                     WHEN 'quarter' THEN ${n_win - 1}::timestamptz
                     ELSE ${n_win}::timestamptz END
        WHERE b.key_id = $1 {model_cond}
        GROUP BY b.model, b.period, b.{limit_col}
        ORDER BY b.period, b.model
        """,
        *args,
    )


async def _team_for_key(key_id: uuid.UUID) -> teams.TeamEnrollment | None:
    """The key's team envelope, or None — TOLERANT BY CONSTRUCTION.

    WHY the blanket except: this runs on the admission path (every proxy call,
    every cascade candidate, every relay iteration) and is exercised by db
    seams that implement only a subset of the interface — the unit suite
    monkeypatches the module-level handle with fakes that answer
    fetchrow/fetch/fetchval/execute and may raise on a statement they do not
    recognize. A team lookup that cannot be answered MUST degrade to "this key
    has no team": the per-key budgets below still run, so the failure mode is
    "the envelope was not enforced for this admission", never "the request
    failed" and never "the per-key cap was skipped". Same posture as the
    EXISTS-probe fallback (an unknown catalog must not break the gate).

    A DB fault is NOT cached (core.teams only caches answers), so a recovered
    database re-reads on the very next admission.

    The log carries the exception's text but no traceback: on the test seams
    this fires once per gate call, and a stack per call would drown the
    operator's log for a condition that is already explained by the message.
    """
    try:
        return await teams.enrollment_for_key(key_id)
    except Exception as exc:
        log.warning(
            "team budget lookup failed for key %s — team envelope not enforced this call: %s",
            key_id,
            exc,
        )
        return None


async def check_budgets(key_id: uuid.UUID, model: str, est_tokens: int = 0) -> GateResult:
    """Refuse the call if any matching period budget is exhausted.

    GateResult.warn carries the highest crossed threshold (80/95) when a
    matching budget is near exhaustion — surfaced by the proxy as an
    X-Budget-Warning response header (the log line / gauge claims of an
    earlier docstring were never implemented — doc drift A2). Pure
    visibility: NO behavior change (soft-threshold rerouting was
    deliberately rejected — see the cascade design decision).

    TEAM ENVELOPE (team leader tier): a key minted from an enrollment is ALSO
    capped by that enrollment's budget — a HARD cap on the SUM over every key
    in the team. The per-key verdict is computed FIRST (it is unchanged code —
    see _check_own_budgets), and the envelope only decides what to do with it:

      * no team, or a team with NO cap configured -> the per-key verdict,
        byte-identical to the pre-feature gate;
      * overflow_enabled=FALSE -> the per-key refusal is TERMINAL (a key is
        hard-capped at its own allocation; the envelope still caps the team);
      * overflow_enabled=TRUE  -> a per-key refusal is SWALLOWED while the
        envelope still has UNCOMMITTED headroom (T - S - SUM(alloc_k - spent_k)):
        only the remainder nobody else is promised is a shared overflow pool
        (2026-10-30: raw T - S let an over-budget key eat a teammate's
        allocation). The envelope refusal is what stops the call.

    The envelope's SHAPE depends on where it came from (2026-10-30): an
    EXPLICIT cap is one pooled number per period (today's behavior, and what
    an operator typed); a DERIVED envelope (profile presets x the mint
    allowance) is PER-MODEL — every preset scaled by N becomes its own team
    pool for its (model, period), and the overflow draws ONLY on the pool of
    the limit that refused. See _apply_team_model_caps.

    The envelope test is POST-PAID (spent >= limit) and deliberately does NOT
    charge est_tokens: the estimate is a per-REQUEST projection, while the
    envelope is a pool several concurrent teammates draw from — charging one
    request's estimate against a team-wide cap would refuse a teammate for
    tokens another teammate may never use. The per-key gate (which does charge
    the estimate) is the surface that protects against one big call. Same
    reasoning as the post-paid USD gate.
    """
    team = await _team_for_key(key_id)
    own = await _check_own_budgets(key_id, model, est_tokens)
    if team is None or not team.has_budget:
        return own
    return await _apply_team_envelope(team, own, model)


def _eval_token_rows(rows: Iterable[Any], est_tokens: int) -> tuple[GateResult | None, int]:
    """PURE: the token-family verdict (a refusal, or None) and its warn band.

    ONE implementation of the per-key refusal/warn math, for BOTH consumers:
    the admission gate (_check_own_budgets) and the visibility projection
    (model_capacity). Visibility that disagrees with enforcement is worse than
    no visibility — a listing that says "you have headroom" while admission
    429s is the exact confusion this function exists to prevent.

    The CALLER bumps the rejection metric, not this function: model_capacity
    runs the same math on every /v1/models listing and every routing request,
    and a counter that is supposed to describe refused TRAFFIC must not move
    because somebody looked at the model picker.
    """
    # The admission estimate has no cached split (usage does not exist yet),
    # so it is discounted by the SAME fraction the settle discounts the row
    # with — assume the request may be as cache-served as the conversation
    # it continues. The ledger settle brings the truth either way, so the
    # residual budget overshoot is bounded by the CONCURRENT in-flight
    # requests' undiscounted remainder (the same trade the post-paid USD
    # gate accepts); frac=0 keeps the byte-identical legacy charge.
    _frac = min(1.0, max(0.0, float(settings.budget_cached_exempt_fraction)))
    est_billable = int(est_tokens * (1.0 - _frac))
    warn = 0
    for b in rows:
        spent = int(b["spent"])
        # HARD STOP AT THE CAP (audit C1, 2026-10-29): the projected test below
        # charges only est*(1-frac) (est/10 at the deployed 0.9 fraction), so
        # at spent == limit it ADMITTED every small request — and the two
        # zero-estimate paths (the relay's per-iteration gate and /mcp) were
        # admitted forever. The USD gate and model_capacity both treat
        # spent >= limit as exhausted; the token gate must agree, or the three
        # surfaces that describe one budget contradict each other.
        if spent >= b["limit_tokens"] or spent + est_billable > b["limit_tokens"]:
            return (
                GateResult(
                    False,
                    f"{b['period']} budget {b['limit_tokens']} exceeded (spent {spent})",
                    refused_period=b["period"],
                    refused_kind="tokens",
                ),
                warn,
            )
        pct = spent * 100 // b["limit_tokens"]
        if pct >= 95:
            warn = max(warn, 95)
        elif pct >= 80:
            warn = max(warn, 80)
    return None, warn


def _eval_usd_rows(rows: Iterable[Any]) -> tuple[GateResult | None, int]:
    """PURE: the USD-family verdict (a refusal, or None) and its warn band.

    Same single-implementation contract as _eval_token_rows: the gate and
    model_capacity must read one budget one way. Post-paid: the in-flight
    request's true cost is known only after the response, so the test is
    spent >= limit — the overshoot is bounded by one response's cost.
    """
    warn = 0
    for cb in rows:
        spent_usd = float(cb["spent"] or 0)
        limit = float(cb["limit_usd"])
        # NON-FINITE ROW (audit F1, 2026-10-29): split_preset_budgets now
        # rejects NaN/Infinity at every write site, but a row persisted by a
        # pre-fix build (or written straight into cost_budgets) must not be
        # able to (a) admit forever — "spent >= nan" is always False — or
        # (b) 500 on the percentage below. Fail CLOSED with a named refusal.
        if not math.isfinite(limit) or not math.isfinite(spent_usd):
            # NOT RESCUABLE (2026-10-30 review): an unreadable row is not a
            # pool. refused_period=None makes every swallow rule miss it —
            # the overflow must never admit a call on data it cannot
            # evaluate (audit F1: "must not admit forever").
            return (
                GateResult(
                    False,
                    f"{cb['period']} cost budget has a non-finite limit or spend ({limit!r} / {spent_usd!r})",
                    refused_period=None,
                    refused_kind="usd",
                ),
                warn,
            )
        if spent_usd >= limit:
            return (
                GateResult(
                    False,
                    f"{cb['period']} cost budget ${limit:.2f} exceeded (spent ${spent_usd:.4f})",
                    refused_period=cb["period"],
                    refused_kind="usd",
                ),
                warn,
            )
        pct = int(spent_usd * 100 / limit) if limit else 0
        if pct >= 95:
            warn = max(warn, 95)
        elif pct >= 80:
            warn = max(warn, 80)
    return None, warn


def _token_headroom(rows: Iterable[Any]) -> int | None:
    """The tightest token headroom across a model's matching rows (None = no
    token budget at all, i.e. unlimited). Display-only: the refusal verdict
    comes from _eval_token_rows, which also charges the request estimate."""
    remaining: int | None = None
    for r in rows:
        left = max(0, int(r["limit_tokens"]) - int(r["spent"]))
        remaining = left if remaining is None else min(remaining, left)
    return remaining


async def _check_own_budgets(key_id: uuid.UUID, model: str, est_tokens: int = 0) -> GateResult:
    """The PER-KEY gate: every budget row on THIS key, first refusal wins.

    The pre-team body of check_budgets, moved here verbatim (comments and all)
    so the team branch can reuse the exact same evaluation instead of growing
    a second copy of the refusal/warn math that could drift from it. Returns
    the FIRST refusal (that is the row that stopped the key) or the max warn
    band when nothing refused — the envelope branch decides whether a refusal
    is terminal, so "first refusal" is exactly what it needs.
    """
    # REMEDIATION P4 (budget-gate EXISTS short-circuit): the overwhelming
    # majority of keys carry NO budget rows at all, yet the gate used to pay
    # for BOTH windowed ledger-SUMs on every admission and every relay
    # iteration. Probe the tiny catalog tables first (indexed key_id lookups
    # — no ledger join) and run the expensive aggregate path ONLY when at
    # least one matching row exists.
    #
    # Correctness contract (fail-safe by construction):
    # * no matching row in EITHER family -> admissible without any SUM —
    #   identical to the old path's outcome (no row -> empty list -> loop
    #   body never runs), just without paying for the aggregate.
    # * any matching row -> the FULL aggregate path below, byte-for-byte
    #   unchanged (refusal reasons, warn thresholds, metrics).
    # * the probe itself failing or answering anything but a real boolean
    #   (legacy fakes return None/0; a DB blip raises) falls back to the
    #   SUMs — an unknown catalog must never skip the gate. GateResult
    #   shape and signatures unchanged.
    try:
        any_rows = await _budget_rows_exist(key_id, model)
    except Exception:
        any_rows = None
    if any_rows is False:
        return GateResult(True, warn=0)

    # ONE lineage read for BOTH budget families (audit P2-31): each
    # _budget_sums call used to re-read the same api_keys row, so a budgeted
    # admission paid two identical PK round trips for it.
    with _shared_lineage(key_id):
        token_refusal, token_warn = _eval_token_rows(
            await _budget_sums("budgets", "limit_tokens", "total_tokens", key_id, model), est_tokens
        )
        if token_refusal is not None:
            # WHICH budget family refused — the metric label (tokens|usd) lets an
            # operator tell "tenant hit its token cap" from "tenant hit its dollar
            # cap" without joining the ledger. Bumped HERE, not in the pure
            # evaluator: model_capacity runs the same math for VISIBILITY and must
            # never move a counter that describes refused traffic.
            _metrics.BUDGET_REJECTIONS.labels(kind="tokens").inc()
            return token_refusal
        # SEQUENTIAL SEMANTICS (pinned by tests/test_budget_gate_cache.py): the
        # cost-family aggregate is fetched ONLY when the token family did not
        # refuse — a token refusal returns before the second SUM ever runs.
        usd_refusal, usd_warn = _eval_usd_rows(
            await _budget_sums("cost_budgets", "limit_usd", "cost_usd", key_id, model)
        )
    if usd_refusal is not None:
        _metrics.BUDGET_REJECTIONS.labels(kind="usd").inc()
        return usd_refusal
    return GateResult(True, warn=max(token_warn, usd_warn))


async def _apply_team_envelope(team: teams.TeamEnrollment, own: GateResult, model: str) -> GateResult:
    """Overlay the enrollment envelope on the per-key verdict.

    Order matters and mirrors the contract:

      1. own refusal + overflow OFF -> return the own refusal untouched (the
         allocation is a hard cap; the envelope is never even read, so a hard
         cap costs no extra query);
      2. a DERIVED envelope (model_caps present) enforces PER-MODEL: every
         N x preset is its own team pool for its (model, period) — see
         _apply_team_model_caps;
      3. an explicit envelope stays POOLED: one token cap + one USD cap for
         the enrollment's period — see _apply_pooled_envelope;
      4. otherwise admit, carrying the per-key warn band through (the console
         still wants the "80% of your allocation" signal even when the call is
         riding the team pool).

    Refusals reuse the SAME metric the per-key gate bumps
    (_metrics.BUDGET_REJECTIONS with kind=tokens|usd), so one dashboard row
    answers "how much traffic did budgets refuse" without caring which tier
    refused. Non-finite guards mirror the USD gate's: a NaN/Infinity envelope
    (a pre-validation row, or a hand-written UPDATE) must REFUSE with a named
    reason — "spent >= nan" is always False, so without this guard such a row
    would enforce nothing while looking configured, and the percentage math
    elsewhere in this module would raise on it.

    A DB fault here propagates, exactly like _budget_sums: the gate's verdict
    cannot be computed, and silently admitting would be the one failure mode
    this whole module exists to prevent.
    """
    if not own.ok and not team.overflow_enabled:
        return own
    if team.model_caps:
        return await _apply_team_model_caps(team, own, model)
    return await _apply_pooled_envelope(team, own)


def _cap_label(model: str) -> str:
    """A cap's model in refusal strings: '*' is the any-model preset."""
    return "any model" if model == "*" else model


def _envelope_eval(
    team: teams.TeamEnrollment,
    caps: Iterable[teams.TeamModelCap],
    spends: Mapping[tuple[str, str], tuple[int, float]],
    pooled_scope: tuple[str, str] | None,
) -> tuple[GateResult | None, int]:
    """PURE: the team envelope's refusal (or None) and warn band for ONE model.

    Mirrors _apply_team_model_caps' evaluation order exactly — every matching
    cap in order, then the pooled outer bound — so the admission gate and the
    visibility projection (model_capacity) cannot disagree about which pool
    refused. `spends` carries every scope the caller fetched in ONE aggregate
    (teams.team_cap_spend); a scope the envelope does not bound is simply
    absent. The CALLER bumps the rejection metric, never this function (the
    listing must not move a counter that describes refused traffic).
    """
    warn = 0
    for cap in caps:
        mtok, musd = spends[(cap.model, cap.period)]
        if cap.limit_tokens is not None:
            if mtok >= cap.limit_tokens:
                return (
                    GateResult(
                        False,
                        f"team {cap.period} budget for {_cap_label(cap.model)} {cap.limit_tokens} exceeded (spent {mtok})",
                        refused_period=cap.period,
                        refused_kind="tokens",
                    ),
                    warn,
                )
            pct = mtok * 100 // int(cap.limit_tokens) if cap.limit_tokens else 0
            if pct >= 95:
                warn = max(warn, 95)
            elif pct >= 80:
                warn = max(warn, 80)
        if cap.limit_usd is not None:
            if not math.isfinite(cap.limit_usd) or not math.isfinite(musd):
                return (
                    GateResult(
                        False,
                        f"team {cap.period} cost budget for {_cap_label(cap.model)} has a non-finite limit or spend ({cap.limit_usd!r} / {musd!r})",
                        refused_period=cap.period,
                        refused_kind="usd",
                    ),
                    warn,
                )
            if musd >= cap.limit_usd:
                return (
                    GateResult(
                        False,
                        f"team {cap.period} cost budget for {_cap_label(cap.model)} ${cap.limit_usd:.2f} exceeded (spent ${musd:.4f})",
                        refused_period=cap.period,
                        refused_kind="usd",
                    ),
                    warn,
                )
            pct = int(musd * 100 / cap.limit_usd) if cap.limit_usd else 0
            if pct >= 95:
                warn = max(warn, 95)
            elif pct >= 80:
                warn = max(warn, 80)
    # THE POOLED TOTAL IS THE OUTER BOUND (audit W6 #1): a derived pooled number
    # is the SUM of the per-model pools, so checking it unconditionally does NOT
    # double-count — it is what makes the console's "budget.tokens" true.
    if pooled_scope is not None and pooled_scope in spends:
        ptok, pusd = spends[pooled_scope]
        if team.budget_tokens is not None:
            token_limit = int(team.budget_tokens)
            if ptok >= token_limit:
                return (
                    GateResult(
                        False,
                        f"team {team.budget_period} budget {token_limit} exceeded (spent {ptok})",
                        refused_period=team.budget_period,
                        refused_kind="tokens",
                    ),
                    warn,
                )
            pct = ptok * 100 // token_limit if token_limit else 0
            if pct >= 95:
                warn = max(warn, 95)
            elif pct >= 80:
                warn = max(warn, 80)
        if team.budget_usd is not None:
            usd_limit = float(team.budget_usd)
            if not math.isfinite(usd_limit) or not math.isfinite(pusd):
                return (
                    GateResult(
                        False,
                        f"team {team.budget_period} cost budget has a non-finite limit or spend ({usd_limit!r} / {pusd!r})",
                        refused_period=team.budget_period,
                        refused_kind="usd",
                    ),
                    warn,
                )
            if pusd >= usd_limit:
                return (
                    GateResult(
                        False,
                        f"team {team.budget_period} cost budget ${usd_limit:.2f} exceeded (spent ${pusd:.4f})",
                        refused_period=team.budget_period,
                        refused_kind="usd",
                    ),
                    warn,
                )
            pct = int(pusd * 100 / usd_limit) if usd_limit else 0
            if pct >= 95:
                warn = max(warn, 95)
            elif pct >= 80:
                warn = max(warn, 80)
    return None, warn


async def _cap_overflow_verdict(
    team: teams.TeamEnrollment,
    own: GateResult,
    caps: Iterable[teams.TeamModelCap],
    avail_cache: dict[tuple[str, str, int | None, float | None], tuple[int | None, float | None]] | None = None,
) -> GateResult:
    """The verdict that STANDS after the per-model overflow swallow.

    Returns `own` when nothing rescues it (no pool of the same
    (model, period, kind), overflow disabled, or the pool has no UNCOMMITTED
    headroom), and an admitting GateResult when a pool can absorb the request.
    `avail_cache` lets model_capacity evaluate many models from ONE round trip
    per scope; the gate passes None (one model, one call).
    """
    if not team.overflow_enabled:
        return own
    pool = [
        c
        for c in caps
        if c.period == own.refused_period
        and (c.limit_tokens is not None if own.refused_kind == "tokens" else c.limit_usd is not None)
    ]
    if not pool:
        return own
    cache = {} if avail_cache is None else avail_cache
    # SPEND-AWARE POOL per (model, period, kind): the pool's headroom must also
    # clear the allocations still promised to the OTHER keys, or an
    # over-allocation key would overflow into a teammate's promise.
    for c in pool:
        # The cache key carries the CAPS, not just the scope: two caps may name
        # the same (model, period) with different limits (one token preset, one
        # USD preset), and team_scope_available's answer depends on the caps it
        # is asked about. Keying on the scope alone would let the USD cap's
        # answer stand in for the token cap's — an optimistic verdict.
        key = (c.model, c.period, c.limit_tokens, c.limit_usd)
        if key not in cache:
            cache[key] = await teams.team_scope_available(team.id, c.model, c.period, c.limit_tokens, c.limit_usd)
        avail_t, avail_u = cache[key]
        avail = avail_t if own.refused_kind == "tokens" else avail_u
        if avail is not None and avail <= 0:
            return GateResult(
                False,
                f"team {c.period} pool for {_cap_label(c.model)} fully committed — no overflow headroom left",
                refused_period=c.period,
                refused_kind=own.refused_kind,
            )
    return GateResult(True)


async def _pooled_overflow_verdict(
    team: teams.TeamEnrollment,
    own: GateResult,
    avail_cache: dict[str, tuple[int | None, float | None]] | None = None,
) -> GateResult:
    """The verdict that STANDS after the POOLED overflow swallow.

    A per-key refusal is rescued ONLY by a pool of the same KIND for the
    envelope's own period — the envelope IS the only pool an explicit cap has.
    A token refusal with a USD-only envelope (or a day refusal against a month
    envelope) has no pool to draw on, so the refusal stands.
    """
    if not team.overflow_enabled:
        return own
    if own.refused_kind == "tokens":
        kind_capped = team.budget_tokens is not None
    elif own.refused_kind == "usd":
        kind_capped = team.budget_usd is not None
    else:
        kind_capped = False
    if not kind_capped or own.refused_period != team.budget_period:
        return own
    cache = {} if avail_cache is None else avail_cache
    # SPEND-AWARE POOL (2026-10-30): headroom means UNCOMMITTED headroom —
    # T - S - SUM(max(0, alloc_k - spent_k)), the exact number the leader
    # console grants from (teams.team_available). Raw T - S is not enough:
    # an over-allocation key would otherwise eat the allocations still
    # promised to under-budget teammates.
    if "avail" not in cache:
        cache["avail"] = await teams.team_available(team.id, team.budget_period)
    avail_tokens, avail_usd = cache["avail"]
    avail = avail_tokens if own.refused_kind == "tokens" else avail_usd
    if avail is not None and avail <= 0:
        return GateResult(
            False,
            f"team {team.budget_period} budget fully committed — no overflow headroom left",
            refused_period=team.budget_period,
            refused_kind=own.refused_kind,
        )
    return GateResult(True)


async def _apply_team_model_caps(team: teams.TeamEnrollment, own: GateResult, model: str) -> GateResult:
    """The PER-MODEL team overlay (derived envelopes, 2026-10-30).

    Every N x profile preset is its own team pool, scoped to (model, period):

    * a model-specific preset pools THAT model's team spend only;
    * a '*' preset pools the team's total across models for its period;
    * the request's model is checked against every cap that names it (or a
      '*') — one team_cap_spend round trip, the same single aggregate the
      pooled path paid;
    * the POOLED number is checked AFTER the caps and binds ALWAYS, whatever
      derived it (audit W6 #1, 2026-11-02): a derived pooled number is the SUM
      of the per-model pools, so it is the team's TOTAL bound — the per-model
      caps are sub-bounds of it, never a substitute. Without this a named-model
      request rode its own pool on top of a team total already at the envelope
      (~2x the stated cap);
    * OVERFLOW SWALLOW RULE (the operator's reading, now the code's): a
      per-key refusal is swallowed ONLY into the pool of the same
      (model, period, kind) limit that refused. A day-limit refusal cannot
      draw on month headroom, and a GLM refusal cannot draw on Qwen headroom.
      A refusal whose (model, period, kind) has no pool at all stands —
      nothing was derived for it to overflow into.

    The caps are POST-PAID (spent >= limit) like every team cap: the pool is
    shared by concurrent teammates and the in-flight request's true size is
    known only after the response.
    """
    caps = [c for c in team.model_caps if c.model == model or c.model == "*"]
    if not caps:
        # A model no preset names: the pooled envelope remains the outer
        # bound — the same accidental bound such models always had.
        return await _apply_pooled_envelope(team, own)
    # ONE AGGREGATE (audit W6 #1 follow-up): the pooled total is the '*' scope —
    # no model filter, i.e. the whole team's spend for the enrollment's period —
    # so it rides the SAME statement as the per-model caps instead of paying a
    # second team_spend round trip on the admission hot path. A '*' model_cap for
    # the same period IS that same quantity (team_cap_spend dedupes the scope), so
    # folding it in cannot conflate two different numbers.
    pooled_scope = ("*", team.budget_period)
    scopes = [(c.model, c.period) for c in caps]
    if team.budget_tokens is not None or team.budget_usd is not None:
        scopes.append(pooled_scope)
    spends = await teams.team_cap_spend(team.id, scopes)
    refusal, team_warn = _envelope_eval(team, caps, spends, pooled_scope)
    if refusal is not None:
        _metrics.BUDGET_REJECTIONS.labels(kind=refusal.refused_kind or "tokens").inc()
        return refusal
    if not own.ok:
        standing = await _cap_overflow_verdict(team, own, caps)
        if standing is own:
            return own
        if not standing.ok:
            _metrics.BUDGET_REJECTIONS.labels(kind=standing.refused_kind or "tokens").inc()
            return standing
        # RESCUED: the per-key band still describes this call's headroom (the
        # pre-refactor path returned GateResult(True, warn=own.warn) here), and
        # the team band joins it — the header must carry the tighter one.
        return GateResult(True, warn=max(own.warn, team_warn))
    # The per-key warn band rides through AND the team band joins it: "80% of
    # your allocation" and "95% of the team pool" are both true statements
    # about the call's headroom, and the header must carry the tighter one.
    return GateResult(True, warn=max(own.warn, team_warn))


async def _apply_pooled_envelope(team: teams.TeamEnrollment, own: GateResult) -> GateResult:
    """The POOLED overlay: one token cap + one USD cap for the enrollment's
    period — the explicit-envelope shape, and the fallback bound for a model
    that no preset names on a derived team."""
    period = team.budget_period
    tokens_spent, usd_spent = await teams.team_spend(team.id, period)
    pooled_scope = ("*", period)
    refusal, team_warn = _envelope_eval(team, (), {pooled_scope: (tokens_spent, usd_spent)}, pooled_scope)
    if refusal is not None:
        _metrics.BUDGET_REJECTIONS.labels(kind=refusal.refused_kind or "tokens").inc()
        return refusal
    if not own.ok:
        # OVERFLOW SWALLOW (pooled path, 2026-10-30 review): a per-key refusal
        # is rescued ONLY by a pool of the same KIND for the envelope's own
        # period — the envelope IS the only pool an explicit cap has.
        standing = await _pooled_overflow_verdict(team, own)
        if standing is own:
            return own
        if not standing.ok:
            _metrics.BUDGET_REJECTIONS.labels(kind=standing.refused_kind or "tokens").inc()
            return standing
        # RESCUED: carry the per-key band through (pre-refactor behaviour).
        return GateResult(True, warn=max(own.warn, team_warn))
    return GateResult(True, warn=max(own.warn, team_warn))


async def check_rate_limit(
    key_id: uuid.UUID,
    rpm: int,
    tpm: int,
    est_tokens: int,
    request_id: str = "",
    model: str = "",
) -> GateResult:
    """Atomic fixed-window counter; creates the window row if absent.

    request_id (optional): when the caller supplies it, the charge actually
    applied to the window is recorded so settle can reconcile the PREDICTED
    cache discount against the true usage. Without it the prediction still
    discounts admission but the delta is not settled (reconciliation is
    skipped rather than guessed from a stale ratio).
    """
    now = datetime.now(UTC)
    start = _window_start(now, "day").replace(hour=now.hour, minute=now.minute)  # 1-minute windows
    # ADMISSION CHARGE (rate_limit_predict_cached): discount the estimate by
    # THIS KEY's measured cache ratio FOR THIS MODEL, so a cache-heavy
    # conversation is not refused for tokens the upstream will serve from
    # prefix cache - and a one-shot embedding/rerank call is not discounted
    # by the key's chat traffic. Without the flag (or without a model) this
    # is exactly est_tokens (byte-identical legacy).
    charge = await predicted_charge(key_id, model, est_tokens)
    # REMEMBER THE CHARGE FOR EVERY ADMITTED REQUEST (audit P1-4, 2026-11-10):
    # settle's cache refund must be bounded by what THIS request actually
    # charged the window. The admission estimate is TEXT-ONLY (the tools
    # catalog and image parts contribute 0 — _estimate_tokens), while
    # cached_tokens is the upstream's REAL prompt size, so on agent traffic a
    # repeated request charges ~1k and would refund ~50k. Recorded regardless
    # of the prediction flag: prediction decides WHETHER the charge is
    # discounted, never whether it is remembered.
    # predicted= (charge != est_tokens) is EXACTLY the condition under which
    # _remember_charge records a predicted charge — carried on the admission
    # record so settle's mechanism choice survives the pop that entry gets.
    _remember_admission_charge(request_id, charge, predicted=charge != est_tokens)
    if charge != est_tokens:
        _remember_charge(request_id, charge)
    row = await db.fetchrow(
        """
        INSERT INTO rate_windows (key_id, window_start, requests, tokens)
        VALUES ($1, $2, 1, $3)
        ON CONFLICT (key_id, window_start)
        DO UPDATE SET requests = rate_windows.requests + 1,
                      tokens   = rate_windows.tokens + $3
        RETURNING requests, tokens
        """,
        key_id,
        start,
        charge,
    )
    assert row is not None  # upsert with RETURNING always yields a row
    # rpm/tpm <= 0 = UNLIMITED (audit C6, 2026-10-29). Profile presets are raw
    # JSON and _profile_limits (routes_enroll) does not validate them, so a
    # preset of {"rate_limit_rpm": 0} minted keys whose FIRST request got
    # "RPM limit 0 exceeded (1 this minute)" — and every request after it, for
    # the life of the key. A non-positive limit follows the max_concurrent
    # convention (0 = not configured), never "refuse everything". The window
    # counters are still incremented: only the REFUSAL is skipped, so the
    # numbers stay truthful for accounting and for a later limit change.
    rpm_limited = rpm > 0
    tpm_limited = tpm > 0
    remaining_requests = max(0, rpm - row["requests"]) if rpm_limited else None
    remaining_tokens = max(0, tpm - row["tokens"]) if tpm_limited else None
    if rpm_limited and row["requests"] > rpm:
        _metrics.RATE_LIMITED.labels(scope="rpm").inc()
        await _release_rejected_charge(key_id, start, charge)
        return GateResult(
            False,
            f"RPM limit {rpm} exceeded ({row['requests']} this minute)",
            remaining_requests=remaining_requests,
            remaining_tokens=remaining_tokens,
        )
    if tpm_limited and row["tokens"] > tpm:
        _metrics.RATE_LIMITED.labels(scope="tpm").inc()
        await _release_rejected_charge(key_id, start, charge)
        return GateResult(
            False,
            f"TPM limit {tpm} exceeded ({row['tokens']} this minute)",
            remaining_requests=remaining_requests,
            remaining_tokens=remaining_tokens,
        )
    return GateResult(True, remaining_requests=remaining_requests, remaining_tokens=remaining_tokens)


# --- TPM admission cache prediction -------------------------------------
# The key's measured cache ratio, cached in-process for _PREDICT_TTL_S.
# Module-level (not per-request) so the admission path costs one indexed
# ledger aggregate per key per TTL window, not one per request.
_predict_cache: dict[tuple[uuid.UUID, str], tuple[float, float]] = {}  # (key, model) -> (ratio, expires_at)


def _predict_ttl() -> float:
    return float(getattr(settings, "rate_limit_predict_ttl_s", 60) or 60)


def _predict_enabled() -> bool:
    return bool(getattr(settings, "rate_limit_predict_cached", False))


async def measured_cache_ratio(key_id: uuid.UUID, model: str) -> float:
    """This key's token-weighted cache ratio for ONE model, over its recent rows.

    WHY a MEASURED ratio rather than a blind discount: the admission gate has
    no upstream usage to consult (the call has not run yet), so the only
    honest predictor of "how much of this prompt will the prefix cache serve"
    is how much it served for this key recently. A blind fraction would give
    an uncached caller the same discount as a cached one - a rate-limit
    bypass. A measured ratio is self-limiting: to raise it you must actually
    be cache-served.

    WHY PER MODEL (2026-11-02): cache hit rate is a property of the WORKLOAD,
    not of the key. On this fleet the chat pools run 94-99.7% prefix-cache
    hits while embedding/reranker/voice pools sit near zero (one forward pass
    per unique input - there is no repeated prefix to serve). A key-scoped
    ratio blended those into one number: a chat-heavy key's ~95% discounted
    its embedding/rerank calls to ~5% of the estimate, and a key whose recent
    rows were embedding-heavy charged its first big chat call the FULL
    estimate - still 429ing on tokens the upstream would have served from
    cache. Scoping the lookup to (key, model) makes each model's admission
    charge reflect that model's own measured behaviour.

    Returns 0.0 when there is no model in hand, no history for THIS model (a
    new key, or a model the key has not called yet, pays the FULL estimate),
    or when the feature/ledger is unavailable - fail-safe to the legacy
    charge, never to a discount.
    """
    if not model:
        # No model in hand (a caller that has not resolved one yet): charge
        # the full estimate. Prediction is an optimisation, never a right.
        return 0.0
    now = time.monotonic()
    cache_key = (key_id, model)
    hit = _predict_cache.get(cache_key)
    if hit is not None and hit[1] > now:
        return hit[0]
    rows = int(getattr(settings, "rate_limit_predict_rows", 50) or 50)
    try:
        row = await db.fetchrow(
            """
            SELECT COALESCE(SUM(cached_tokens), 0)::float AS cached,
                   COALESCE(SUM(prompt_tokens), 0)::float AS prompt
            FROM (
                SELECT cached_tokens, prompt_tokens
                FROM usage_ledger
                WHERE key_id = $1 AND model = $2
                  AND status = 'ok' AND prompt_tokens > 0
                ORDER BY ts DESC
                LIMIT $3
            ) recent
            """,
            key_id,
            model,
            rows,
        )
    except Exception:
        log.warning("rate_limit: cache-ratio lookup failed (charging full estimate)", exc_info=True)
        return 0.0
    ratio = 0.0
    if row is not None and float(row["prompt"] or 0) > 0:
        ratio = float(row["cached"] or 0) / float(row["prompt"])
        # Clamp: a poisoned/odd upstream row must never imply a discount > 1.
        ratio = min(1.0, max(0.0, ratio))
    _predict_cache[cache_key] = (ratio, now + _predict_ttl())
    # Bound the cache: a gateway with many keys must not grow this unboundedly.
    if len(_predict_cache) > 4096:
        for k in [k for k, v in _predict_cache.items() if v[1] <= now]:
            _predict_cache.pop(k, None)
    return ratio


async def predicted_charge(key_id: uuid.UUID, model: str, est_tokens: int) -> int:
    """The admission charge: the estimate discounted by the MEASURED ratio.

    Returns est_tokens unchanged when prediction is off (byte-identical
    legacy charge), so the flag is a true kill switch.
    """
    if not _predict_enabled() or est_tokens <= 0:
        return est_tokens
    ratio = await measured_cache_ratio(key_id, model)
    return int(est_tokens * (1.0 - ratio))


# The charge actually applied per in-flight request (request_id -> est charged
# at admission). Bounded: entries are popped at settle or expire by TTL, so a
# lost settle cannot grow this without bound.
_predict_charges: dict[str, tuple[int, float]] = {}


def _remember_charge(request_id: str, est_tokens: int) -> None:
    if not request_id:
        return
    now = time.monotonic()
    _predict_charges[request_id] = (int(est_tokens), now + 3600.0)
    if len(_predict_charges) > 8192:
        for k in [k for k, v in _predict_charges.items() if v[1] <= now]:
            _predict_charges.pop(k, None)


@dataclass
class _AdmissionCharge:
    """One request's admission accounting, kept for its whole settle lifetime.

    WHY it SURVIVES the settle that uses it (cross-validation, 2026-11-10): a
    relay request settles ONCE PER ITERATION with the SAME request_id
    (toolrelay's per-iteration settle). Popping the record at the first settle
    made iterations 2..N fall into the "no recorded charge" branch and refund
    their FULL cached_tokens, unbounded — measured 1_000 charged against
    [1_000, 59_000, 69_000] refunded (129x), which drains the minute window and
    stops TPM enforcing for the rest of the minute. So the record lives until
    its TTL and ACCUMULATES what it has already given back.

    Fields:
      charge     the tokens this request added to the window at admission.
      refunded   how much of that charge has already been given back (the
                 cumulative bound, across every settle of this request_id).
      expires_at monotonic TTL — the ONLY reclaim mechanism, so a lost settle
                 can never leak the entry.
      predicted  whether the admission charge was cache-PREDICTED. Carried here
                 because the mechanism decision must stay stable across the
                 relay's per-iteration settles: _predict_charges is popped by
                 the first reconcile (the C3 memory contract, pinned by
                 tests/test_budgets.py), and a popped entry made later
                 iterations look unpredicted and take the refund branch — i.e.
                 BOTH mechanisms ran for one request, the double-count the
                 settle comment forbids.
    """

    charge: int
    refunded: int = 0
    expires_at: float = 0.0
    predicted: bool = False


# The charge ACTUALLY applied to the window per in-flight request, recorded for
# EVERY admitted call (prediction on or off). It exists so the settle-time cache
# refund can be clamped by the request's OWN admission charge (audit P1-4): the
# refund used to be bounded only by the WINDOW balance, so one agent request
# that charged ~1k and refunded ~50k drained the minute window to 0 and TPM
# stopped enforcing for the rest of the minute. Deliberately SEPARATE from
# _predict_charges, which answers a different question — "did prediction
# discount this request?" — see _AdmissionCharge.predicted.
_admission_charges: dict[str, _AdmissionCharge] = {}


def _remember_admission_charge(request_id: str, charge: int, *, predicted: bool = False) -> None:
    if not request_id:
        return
    now = time.monotonic()
    _admission_charges[request_id] = _AdmissionCharge(
        charge=int(charge),
        expires_at=now + 3600.0,
        predicted=predicted,
    )
    if len(_admission_charges) > 8192:
        for k in [k for k, v in _admission_charges.items() if v.expires_at <= now]:
            _admission_charges.pop(k, None)


def _claim_refund(request_id: str, wanted: int) -> int:
    """How much of the requested refund this settle may actually apply.

    THE CUMULATIVE BOUND (audit P1-4 + cross-validation 2026-11-10): across
    every settle of ONE request_id, the total refunded can never exceed the
    tokens that request charged the window. The argument is this iteration's
    cached_tokens; the answer is min(cached_tokens, charge - already refunded).

    A settle with no recorded charge (no request_id, or a path that never ran
    check_rate_limit) has nothing to bound it by and keeps the legacy
    unclamped refund. The allowance is consumed even when the window UPDATE
    later fails or is discarded (a rolled-over window) — conservative in the
    direction that cannot over-refund.
    """
    if wanted <= 0:
        return 0
    record = _admission_charges.get(request_id) if request_id else None
    if record is None:
        return int(wanted)
    allowed = min(int(wanted), max(0, record.charge - record.refunded))
    record.refunded += allowed
    return allowed


async def _reconcile_predicted_charge(
    key_id: uuid.UUID, request_id: str, prompt_tokens: int, cached_tokens: int
) -> None:
    """Settle the predicted-vs-actual difference into the CURRENT window.

    WHY: the admission charge used a PREDICTED cache ratio. The true billable
    tokens are known at settle. Charging the delta closes the loop: a caller
    who busted its own cache (a nonce in the prompt defeats the prefix cache)
    pays the real cost, so the cheap admission is a ONE-request head start,
    not a sustained multiplier.

    Sign: positive = undercharged at admission (charge the difference),
    negative = overcharged (refund it). Clamped at 0 on the refund side by
    the same UPDATE the cache refund uses.
    """
    # RELEASE FIRST, DECIDE SECOND (audit C3, 2026-10-29): the guards below
    # return without popping, and every 429/refusal path settles with
    # prompt_tokens == 0 — so each refused request left its admission charge
    # in _predict_charges forever (only an hour-TTL prune past the 8192
    # watermark reclaimed it), and a later request reusing that request_id
    # reconciled against ANOTHER request's charge. The pop is unconditional;
    # a zero-prompt settle has nothing to reconcile but must still release.
    #
    # The pop makes this mechanism SINGLE-SHOT, which is what keeps it safe
    # under the relay's per-iteration settles (cross-validation 2026-11-10):
    # the first settle brings the charge to its true billable, later ones find
    # no charge and adjust nothing (they must NOT refund — settle's mechanism
    # choice is carried on the surviving _AdmissionCharge.predicted flag, so a
    # predicted request never falls into the cache-refund branch). The refund
    # side is inherently bounded by the charge: it is charged - true_billable,
    # and true_billable >= 0.
    remembered = _predict_charges.pop(request_id, None) if request_id else None
    if not _predict_enabled() or not bool(getattr(settings, "rate_limit_predict_reconcile", True)):
        return
    if prompt_tokens <= 0 or not request_id:
        return
    # The charge this request ACTUALLY made at admission. Recomputing it from
    # the ratio here would be wrong: the ratio is refreshed from the ledger,
    # which now contains this very row - the prediction would grade itself.
    if remembered is None:
        return  # no recorded charge (prediction off at admission, or expired)
    charged = remembered[0]
    # The cache refund does NOT run for a predicted request (settle picks ONE
    # mechanism), so this is the only correction: bring the charge to the TRUE
    # billable total. charged + (true_billable - charged) == true_billable
    # exactly - the prediction only decides WHEN the tokens are charged, never
    # HOW MANY, so an inaccurate ratio cannot leak throughput.
    true_billable = max(0, int(prompt_tokens) - int(cached_tokens))
    delta = true_billable - charged
    if delta == 0:
        return
    now = datetime.now(UTC)
    start = _window_start(now, "day").replace(hour=now.hour, minute=now.minute)
    try:
        if delta > 0:
            await db.execute(
                """
                UPDATE rate_windows SET tokens = tokens + $3
                WHERE key_id = $1 AND window_start = $2
                """,
                key_id,
                start,
                delta,
            )
        else:
            await _refund_window_tokens(key_id, -delta)
    except Exception:
        log.warning("settle: predicted-charge reconciliation failed (non-fatal)", exc_info=True)


async def _release_rejected_charge(key_id: uuid.UUID, start: datetime, est_tokens: int) -> None:
    """Give back the token charge of a REFUSED request.

    WHY: check_rate_limit must increment atomically before it can compare (a
    read-then-write would race), so a refused call has already added its
    estimate to the window. A refused call consumed no tokens at all, so
    leaving that charge in place makes every 429 raise the odds of the next
    one for the rest of the minute - a refusal storm feeds itself.

    The REQUEST counter is deliberately NOT released: it is the anti-hammer
    axis, and a client looping on 429s must still exhaust its RPM budget.

    Best-effort: the gate's verdict is already decided, and a failure here
    only restores the pre-fix (charge-stays) behaviour - never a bypass, and
    never a refusal that did not happen.
    """
    if est_tokens <= 0:
        return
    try:
        await db.execute(
            """
            UPDATE rate_windows SET tokens = GREATEST(tokens - $3, 0)
            WHERE key_id = $1 AND window_start = $2
            """,
            key_id,
            start,
            est_tokens,
        )
    except Exception:
        log.warning("rate_limit: rejected-charge release failed (non-fatal)", exc_info=True)


async def _refund_window_tokens(key_id: uuid.UUID, tokens: int) -> None:
    """Refund up to `tokens` from the key's CURRENT one-minute TPM window.

    WHY (cache-aware TPM): the admission gate charged the len//4 ESTIMATE -
    it cannot know how much of the prompt the upstream prefix cache served.
    At settle time the real usage (including
    prompt_tokens_details.cached_tokens) is known, and the cache-served
    tokens do not consume inference capacity the way fresh prefill does.

    HOW: a single atomic decrement, CLAMPED to the window's balance. The
    refund actually applied is min(tokens, balance) - a window holding less
    than the reported cache hit gives back what it has and no more. The
    CALLER additionally passes min(cached_tokens, this request's admission
    charge) (audit P1-4): a refund larger than the request's own charge
    refunds tokens the key never spent in this window, which is how one
    agent turn could drain the minute counter to 0 and stop TPM enforcing.
    Never an
    UPSERT: an INSERT here would mint window rows for calls that were never
    admitted against this window (a refund must not create counter state),
    and crediting tokens INTO a future window would re-open the
    denial-of-wallet shape the negative-estimate fix closed.

    WHY the clamp replaced the old all-or-nothing predicate (2026-10-21):
    the previous form carried "AND tokens >= $3" and dropped the ENTIRE
    refund when the balance fell short. Because cached_tokens is the
    upstream's REAL prompt size while the admission charge is a len//4
    estimate, the refund routinely exceeded the balance - and a measured
    56.6% of refunded tokens were being discarded on a live deployment.
    The clamp keeps the safety property (a refund can never drive a window
    negative, and can never create one) while no longer throwing away the
    part that does fit.

    Window-rollover edge: a long streaming call can settle in the window
    AFTER the one its admission charged. Nothing is adjusted then (the
    UPDATE matches no row) - the refund is dropped and reported via
    metrics.observe_tpm_cache_refund(outcome=discarded). Refunds are
    best-effort; the LEDGER row always carries the true usage either way.
    """
    if tokens <= 0:
        return
    now = datetime.now(UTC)
    start = _window_start(now, "day").replace(hour=now.hour, minute=now.minute)
    # One statement: FOR UPDATE pins the row so the prior balance and the
    # decrement cannot interleave with a concurrent admission, and
    # UPDATE ... FROM only matches an EXISTING row (never an UPSERT, so a
    # rolled-over window still discards rather than being resurrected).
    row = await db.fetchrow(
        """
        WITH cur AS (
            SELECT tokens FROM rate_windows
            WHERE key_id = $1 AND window_start = $2
            FOR UPDATE
        )
        UPDATE rate_windows r SET tokens = GREATEST(r.tokens - $3, 0)
        FROM cur
        WHERE r.key_id = $1 AND r.window_start = $2
        RETURNING cur.tokens AS prior, r.tokens AS applied
        """,
        key_id,
        start,
        tokens,
    )
    if row is None:
        _metrics.observe_tpm_cache_refund(tokens, discarded=True)
        return
    _metrics.observe_tpm_cache_refund(int(row["prior"]) - int(row["applied"]), discarded=False)


def _billable_tokens(total_tokens: int, cached_tokens: int) -> int:
    """Budget-consumption tokens for one ledger row:
    total - cached * budget_cached_exempt_fraction.

    The period-budget twin of the TPM cache refund: upstream prompt-cache
    hits are discounted (default fraction 0.9 = cached tokens count at
    one-tenth weight; 1.0 = fully exempt; 0.0 = legacy full count) so a long
    cached conversation cannot hard-limit itself against a day/week/month cap.
    Never negative; cached > total (a poisoned row) clamps to total.
    """
    frac = min(1.0, max(0.0, float(settings.budget_cached_exempt_fraction)))
    if frac <= 0.0:
        return int(total_tokens)
    exempt = int(cached_tokens) if cached_tokens <= total_tokens else int(total_tokens)
    return int(total_tokens) - int(exempt * frac)


def _budget_token_expr(alias: str) -> str:
    """The SQL expression for the token-budget SUM over `alias`.

    Legacy (exempt fraction 0.0): SUM(alias.total_tokens *
    alias.billing_fraction) - byte-identical to the pre-discount shape the
    shape-asserting tests pin. With the exemption on, the cached tokens are
    discounted OUT of the weighted sum: (total - cached*frac) * fraction,
    via GREATEST/LEAST guards that keep a poisoned row (cached > total)
    from going negative. The USD branch is UNCHANGED - costs.compute
    already prices cached tokens at cached_per_m."""
    return f"SUM({_budget_token_value(alias)})"


def _budget_token_value(alias: str) -> str:
    """The PER-ROW billable-token VALUE (no aggregate) for `alias`.

    2026-10-30: split out of _budget_token_expr because a caller that needs
    its own aggregate — team_cap_spend's SUM(...) FILTER per scope — cannot
    nest the SUM the expr already carries (Postgres: "aggregate function
    calls cannot be nested"; a live 500 the fake DB could never catch).
    This is the single source of the cache-discount arithmetic: the SUM form,
    the per-scope FILTER form and the console rollups all build on it.
    """
    frac = min(1.0, max(0.0, float(settings.budget_cached_exempt_fraction)))
    t = f"{alias}.total_tokens"
    f = f"{alias}.billing_fraction"
    if frac <= 0.0:
        return f"{t} * {f}"
    c = f"{alias}.cached_tokens"
    return f"GREATEST({t} - LEAST({c}, {t}) * {frac}, 0) * {f}"


async def settle(
    key_id: uuid.UUID,
    model: str,
    endpoint: str,
    prompt_tokens: int,
    completion_tokens: int,
    status: str,
    latency_ms: int,
    request_id: str,
    cached_tokens: int = 0,
    billing_fraction: float = 1.0,
    guardrail_events: list[dict[str, Any]] | None = None,
    *,
    routing_policy: str = "",
    routing_ceiling: str = "",
    routing_served: str = "",
    routing_reason: str = "",
    routing_probabilities: str = "",
    billable_units: float | None = None,
    unit_kind: str | None = None,
) -> None:
    """Write the immutable usage row. Called for every request outcome.

    cached_tokens: tokens served from cache — upstream prompt-cache
    (vLLM cached_tokens) or full embedding-cache hits (status='cache_hit';
    those rows cost ZERO budget — the budget SUM excludes them — but stay
    visible for the cache-hits reporting). Cost: computed from model_costs
    AT REQUEST TIME and stored on the row (repricing never rewrites
    history); NULL only when the row was never real upstream spend — an
    unpriced model, a status outside _COSTED_STATUSES (a request that never
    reached an upstream), or a model_costs READ FAULT (the row is written
    unpriced rather than lost — audit P2-16). A post-call
    'guardrail_blocked' row IS priced: the GPU time was spent (audit r5 #9).

    billing_fraction: the off-peak evaluator's CURRENT fraction at the
    settle moment (core.offpeak.fraction(); 1.0 when the feature is off or
    the evaluator failed — read fresh per settle so a mid-relay flip is
    honored per iteration). Stored on the row for the FRACTION-WEIGHTED
    budget SUMs; prompt/completion/total/cost stay TRUE for audit.

    guardrail_events: content-guardrail verdicts for this request
    ({guardrail,mode,engine,action,detail} dicts, cap 50) — stored as JSONB
    next to the cost facts so the "what did the policy do" story lives on
    the same immutable row. NULL = no guardrails ran.

    routing_* (KEYWORD-ONLY, all default '' = routing not in effect): the
    complexity-router decision audit (core.model_routing). The `model`
    column keeps the ACTUALLY-served model in every case — ledger truth is
    never displaced; 'shadow' rows stamp routing_served = would-serve (the
    ceiling model still billed), 'active' rows stamp the routed model that
    billed. Keyword-only so the dozen-plus existing call sites (routes_mcp,
    toolrelay, the proxy paths) stay byte-compatible: every column after
    billing_fraction/guardrail_events must be passed BY NAME, and the new
    INSERT parameters append AFTER the existing positional order (tests and
    tools index the INSERT args tuple positionally).

    billable_units / unit_kind (KEYWORD-ONLY, P10 unit-aware billing; both
    default None = the row prices exactly as always and both columns ledger
    NULL): unit_kind names the row's unit — a CHECK-vocabulary unit
    ('second', ...) prices by costs.compute_units over billable_units, and an
    '*_estimate' suffix ('char_estimate'/'second_estimate'/'token_estimate')
    is the PROXY provenance tag; its EFFECTIVE unit (the base unit without
    the suffix) decides the arithmetic. The gate reads the STORED
    model_costs.unit, never the tag (cross-validation wave 2): stored
    'token' keeps the legacy token estimate arithmetic (every pre-P10 row
    and plain chat is untouched), any other stored unit prices over the
    effective unit and LEDGERS cost NULL + a WARNING on a mismatch — a
    per-1M-SECOND rate is never applied per-1M-TOKEN, and chars are never
    converted to seconds. A quantity out of NUMERIC(18,6) range ledgers the
    whole row unpriced (quantity and cost NULL) rather than aborting the
    INSERT — the upstream has already been paid, so the row itself is never
    lost. 'routing' rows are priced like _COSTED_STATUSES (real GPU spend)
    but deliberately stay OUT of that tuple: the budget SUMs must not
    consume the caller's token/completion envelope for the routing turn.
    """
    cost = None
    # Unit-aware rows (P10, hardened by cross-validation wave 2): the EFFECTIVE
    # unit comes from the row's unit_kind tag — a real CHECK-vocabulary unit
    # ('second', ...) prices over billable_units via costs.compute_units, and an
    # '*_estimate' tag maps to its base unit ('char_estimate'->'char',
    # 'second_estimate'->'second', 'token_estimate'->'token'). The LEGACY token
    # arithmetic is gated on the STORED model_costs unit — never on the tag: a
    # model stored per 'token' (every pre-P10 row, plain chat) keeps billing
    # through compute() exactly as always, while any other stored unit prices
    # over the effective unit and LEDGERS NULL + a WARNING when it does not
    # match (a per-1M-SECOND rate must never ride the per-1M-TOKEN arithmetic —
    # that was a silent 3x over-bill on ASR estimates; nor may chars be billed
    # as seconds — a conversion we cannot make is never fabricated).
    # status='routing' prices like a COSTED status — the decision model burned
    # real GPU time — while remaining OUTSIDE the _COSTED_STATUSES budget-SUM
    # tuple (see its comment above).
    from . import costs as _costs

    effective_unit: str | None = None
    if unit_kind is not None:
        if unit_kind in _costs.UNITS:
            # A REAL unit ('second', ...) ALWAYS prices through compute_units —
            # its mismatch guard (NULL + WARNING, never a converted number) is
            # the standing contract, whatever the stored unit is.
            effective_unit = unit_kind
        else:
            candidate = _EFFECTIVE_UNIT_BY_TAG.get(unit_kind)
            if candidate is None:
                # An unrecognised tag stays on the legacy path (the pre-P10
                # shape): an unknown tag must never invent a unit — and thus a
                # rate — of its own.
                log.warning(
                    "settle: unknown unit_kind %r on %r — priced through the legacy token arithmetic",
                    unit_kind,
                    model,
                )
            else:
                # ESTIMATE tags are gated on the STORED unit, never on the tag:
                # stored 'token' (every pre-P10 row, plain chat, and any voice
                # model not yet repriced) keeps the legacy token arithmetic
                # exactly — the rate IS per-1M-token, so billing the row's token
                # proxy through it is dimensionally honest and pre-P10 rows never
                # change. Any OTHER stored unit prices over the effective unit
                # via compute_units (a per-1M-SECOND rate must never ride the
                # token arithmetic — the silent 3x ASR over-bill the wave-2
                # review measured), and its mismatch guard ledgers NULL + a
                # WARNING rather than converting (a char estimate on a
                # second-priced model ledgers NULL — chars are never billed as
                # seconds, no conversion is fabricated). An UNPRICED model
                # (stored None) also takes the unit path: honest NULL, never the
                # per-1M-TOKEN fallback applied to a non-token quantity.
                stored = await _costs.stored_unit(model)
                if stored != "token":
                    effective_unit = candidate
    # The quantity the row will actually STORE, validated against the column
    # BEFORE anything prices from it (NUMERIC(18,6) ceiling; see the comment
    # below). None = ledger NULL — either the row never carried a quantity, or
    # the one it carried could not be stored and must not be priced from either.
    units_to_store: float | None = None
    if billable_units is not None:
        try:
            bu = float(billable_units)
        except (TypeError, ValueError):
            bu = float("nan")
        if not math.isfinite(bu) or abs(bu) > _BILLABLE_UNITS_MAX:
            # An oversized (or non-finite) quantity must never abort the INSERT:
            # asyncpg would raise NumericValueOutOfRangeError and the immutable
            # billing row for upstream spend already served would be LOST — the
            # exact outcome the P2-16 guard exists to prevent. Quantity and cost
            # (which would overflow NUMERIC(12,6) on the same request) both
            # ledger NULL with a WARNING, row intact.
            log.warning(
                "settle: billable_units %r out of NUMERIC(18,6) range for %r — row ledgers NULL (unpriced), never lost",
                billable_units,
                model,
            )
        else:
            units_to_store = bu
    if status == "cache_hit":
        # served from OUR cache — no upstream spend, even if the model is priced
        from decimal import Decimal

        cost = Decimal("0.000000")
    elif status == "routing" or status in _COSTED_STATUSES or effective_unit is not None:
        # Real upstream spend is priced whatever the OUTCOME was: a post-call
        # guardrail block ('guardrail_blocked') burned the same prefill, and
        # leaving it unpriced stored the tokens with cost_usd NULL (audit r5
        # #9 — the ledger then claimed "real upstream spend" for tokens it
        # never priced).
        # NEVER let the price lookup abort the LEDGER WRITE (audit P2-16): the
        # upstream has already been paid, so losing this immutable billing row
        # to a model_costs read fault is the one outcome worse than an unpriced
        # row — and the cost_usd column already models "unpriced" as NULL.
        # costs.price_hints guards the same call for the same reason.
        try:
            if effective_unit is not None:
                # P10 unit row (measured seconds, or a tagged estimate whose unit
                # matches the stored one): priced over the QUANTITY, never through
                # the token arithmetic. compute_units returns None (and the guard
                # reports it) when the stored unit mismatches — the row ledgers
                # unpriced rather than silently converting through the wrong rate.
                # A unit-tagged row with NO quantity is unpriceable by definition:
                # 0.0 units would forge a 0.000000 bill, so it ledgers NULL.
                cost = (
                    await _costs.compute_units(model, units_to_store, effective_unit)
                    if units_to_store is not None
                    else None
                )
                if units_to_store is None and billable_units is None:
                    log.warning(
                        "settle: unit_kind %r row for %r carries no billable_units — ledgered unpriced",
                        unit_kind,
                        model,
                    )
            else:
                cost = await _costs.compute(model, prompt_tokens, completion_tokens, cached_tokens)
            # Same cliff, cost side (cross-validation wave 2): cost_usd is
            # NUMERIC(12,6) — a price past 999999.999999 (a huge but IN-RANGE
            # quantity times any real rate) would raise
            # NumericValueOutOfRangeError from the same INSERT and lose the row
            # the same way. Honest NULL beats both a lost row and a clamped
            # (fabricated) number.
            if cost is not None and abs(cost) > _COST_USD_MAX:
                log.warning(
                    "settle: computed cost %s exceeds NUMERIC(12,6) for %r — cost ledgers NULL, row intact",
                    cost,
                    model,
                )
                cost = None
        except Exception:
            log.warning("settle: cost lookup failed for %r - ledger row unpriced (non-fatal)", model, exc_info=True)
            cost = None
    events_json = json.dumps(guardrail_events[:50], default=str) if guardrail_events else None
    # Prometheus counter/histogram ride THE SAME row write (metrics rule 4):
    # bumped before the INSERT so a metrics outage can never desync — and
    # with the shims this is a no-op when prometheus_client is absent.
    _metrics.observe_settle(model, endpoint, status, latency_ms)
    await db.execute(
        """
        INSERT INTO usage_ledger
            (key_id, model, endpoint, prompt_tokens, completion_tokens,
             total_tokens, cached_tokens, cost_usd, status, latency_ms,
             request_id, billing_fraction, guardrail_events,
             routing_policy, routing_ceiling, routing_served, routing_reason,
             routing_probabilities, billable_units, unit_kind)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17,
                $18, $19, $20)
        """,
        key_id,
        model,
        endpoint,
        prompt_tokens,
        completion_tokens,
        prompt_tokens + completion_tokens,
        cached_tokens,
        cost,
        status,
        latency_ms,
        request_id,
        billing_fraction,
        events_json,
        routing_policy,
        routing_ceiling,
        routing_served,
        routing_reason,
        routing_probabilities,
        # P10: None/None on every legacy row (unchanged shape); a tagged row
        # carries the provenance quantity and its unit name.
        units_to_store,
        unit_kind,
    )
    # Cache-aware TPM (rate_limit_exclude_cached_tokens): give the key's
    # minute window back the tokens the upstream prefix cache served.
    # status='cache_hit' is EXCLUDED deliberately: the embeddings cache path
    # settles a hit WITHOUT ever running the admission gate, so there is no
    # admission charge to refund - crediting one would mint window tokens
    # from nothing (a rate-limit bypass). Error rows refund too: the window
    # was charged at admission and an upstream failure consumed no cache.
    # TWO mechanisms can adjust this window for cache, and they MUST NOT both
    # run for one request - that double-counts the cache. Which one applies is
    # decided by whether THIS request's admission was already cache-discounted:
    #
    #   prediction OFF -> the charge was the full estimate, so the post-hoc
    #     REFUND below is the only correction — and it is clamped by that
    #     charge (audit P1-4), CUMULATIVELY across every settle of the request
    #     (the relay settles once per iteration), so a request can never give
    #     back more than it took.
    #   prediction ON  -> the charge was ALREADY discounted by the measured
    #     ratio, so a refund would discount a second time. RECONCILIATION
    #     alone settles it: charged + (true_billable - charged) == true
    #     billable exactly, whatever the predicted ratio was.
    #
    # status='cache_hit' never ran a gate, so neither applies.
    #
    # MULTIPLICITY (cross-validation 2026-11-10): the relay settles once PER
    # ITERATION with the same request_id. The record is therefore NOT consumed
    # here — it survives to bound every iteration cumulatively (see
    # _claim_refund) — and the mechanism choice is read from it, because
    # _predict_charges is popped by the first reconcile: without the surviving
    # flag, iterations 2..N looked unpredicted and took the REFUND branch too,
    # running both mechanisms for one request.
    record = _admission_charges.get(request_id) if request_id else None
    predicted = record.predicted if record is not None else (request_id in _predict_charges)
    if status != "cache_hit":
        if predicted:
            try:
                await _reconcile_predicted_charge(key_id, request_id, prompt_tokens, cached_tokens)
            except Exception:
                log.warning("settle: predicted-charge reconciliation failed (non-fatal)", exc_info=True)
        elif settings.rate_limit_exclude_cached_tokens:
            # The charge this request actually made at admission BOUNDS the
            # refund (audit P1-4): refunding more than was charged drains the
            # window below what the key really spent, and TPM then stops
            # enforcing for the rest of the minute. A settle whose admission
            # was never recorded (no request_id, or a path that never ran
            # check_rate_limit) keeps the legacy unclamped refund — there is
            # nothing to bound it by.
            refund = _claim_refund(request_id, int(cached_tokens))
            try:
                await _refund_window_tokens(key_id, refund)
            except Exception:
                log.warning("settle: TPM cache refund failed (non-fatal)", exc_info=True)


async def budget_status(key_id: uuid.UUID) -> list[dict]:
    """Current spend vs limit for every budget on a key (admin/UI view).

    Emits BOTH budget kinds (gaps P1): token budgets keep their original
    shape (now with kind="tokens" so consumers can branch) and USD cost
    budgets — enforced by check_budgets since 0.2 but previously INVISIBLE
    here — appear as kind="usd" rows with limit_usd/spent_usd. Same
    two-aggregate-queries shape as the gate (no per-budget N+1).
    """
    out = []
    for b in await _budget_sums("budgets", "limit_tokens", "total_tokens", key_id, None):
        out.append(
            {
                "kind": "tokens",
                "model": b["model"],
                "period": b["period"],
                "limit": b["limit_tokens"],
                "spent": int(b["spent"]),
                "window_start": _window_start(datetime.now(UTC), b["period"]).isoformat(),
            }
        )
    for cb in await _budget_sums("cost_budgets", "limit_usd", "cost_usd", key_id, None):
        out.append(
            {
                "kind": "usd",
                "model": cb["model"],
                "period": cb["period"],
                "limit_usd": float(cb["limit_usd"]),
                "spent_usd": float(cb["spent"] or 0),
                "window_start": _window_start(datetime.now(UTC), cb["period"]).isoformat(),
            }
        )
    return out


async def model_capacity(key_id: uuid.UUID, models: list[str], est_tokens: int = 0) -> dict[str, dict]:
    """Per-model budget capacity for ONE key, right now — TEAM ENVELOPE INCLUDED.

    Returns {model: {"ok": bool, "remaining_tokens": int | None, "blocked_by": str,
                     "warn": int, "team": {...}?}}.
    remaining_tokens is None when the key carries no token budget for that model
    (unlimited). blocked_by is "" when ok, else a short human reason. warn is
    the highest crossed warning band (80/95) — the SAME number check_budgets
    puts in X-Budget-Warning. `team` appears only when the key's enrollment
    carries an envelope, and reports that envelope's own verdict.
    ON A REFUSED MODEL the band is what was accumulated BEFORE the refusing
    row (the gate's own GateResult carries 0 there) — the gate's
    X-Budget-Warning only rides ADMITTED calls, so there is no header to
    disagree with, and the extra information is what a picker wants: "this
    rung is spent, the next one is at 85%".

    READ "ok" AS "THE ADMISSION GATE WOULD ADMIT THIS", not merely "spent <
    limit". The per-key verdict is the SAME pure evaluator the gate runs
    (_eval_token_rows / _eval_usd_rows), including the admission estimate, and
    the team overlay mirrors _apply_team_envelope exactly: per-model caps, the
    pooled outer bound, and the overflow swallow with its UNCOMMITTED-headroom
    test. That parity is the whole point — the router offers only models
    admission would serve, so it cannot pick one the gate then 429s, and a
    listing cannot say "Unavailable to you" about a model overflow admits.

    `est_tokens` is the request's estimated size. The default 0 keeps the
    listing's historical "has headroom" reading; the routing path passes the
    real estimate so a model with 5k left is not offered for a 100k request.
    The team envelope stays POST-PAID (never charged an estimate), exactly like
    the gate.

    FAIL-OPEN is deliberate and is the routing contract: a budget lookup fault
    must never make models disappear from model_list (an empty or all-blocked
    answer would reroute/refuse traffic on a DB blip). The gate itself still
    enforces on admission, so the worst case is a model shown as available
    that the gate then refuses — never the reverse.
    """
    if not models:
        return {}
    unlimited = {m: {"ok": True, "remaining_tokens": None, "blocked_by": "", "warn": 0} for m in models}
    # TEAM ENVELOPE (2026-11-03): the gate's verdict is the per-key verdict
    # OVERLAID by the enrollment's envelope (check_budgets -> _apply_team_envelope).
    # A capacity that stopped at the per-key rows advertised models the TEAM
    # pool then refused — the "routed to a model I have no budget for"
    # complaint this projection exists to prevent. The lookup is TTL-cached in
    # core.teams (30 s, negative answers included), so it is ~free per call.
    try:
        team = await _team_for_key(key_id)
    except Exception:  # _team_for_key is tolerant already; belt and braces
        log.warning("model_capacity: team lookup failed; per-key verdict only", exc_info=True)
        team = None
    team_budget = team is not None and team.has_budget

    # FAST PATH (perf): the overwhelming majority of keys carry NO budget rows
    # at all, and the aggregates below would pay the lineage fetchrow plus
    # BOTH ledger SUMs to discover that. Probe the tiny indexed catalog first —
    # the same short-circuit check_budgets uses (see its REMEDIATION P4 note) —
    # so the common case costs one cheap probe instead of four round-trips on
    # the pool the data plane shares. The probe covers the OWN rows only; a
    # team-capped key still takes the aggregate path even with no own rows.
    try:
        any_rows = await _budget_rows_exist(key_id, None)
    except Exception:
        # An unknown catalog must never skip the real check below.
        any_rows = None
        log.warning("model_capacity: budget-row probe failed; falling back to the aggregates", exc_info=True)
    if any_rows is False and not team_budget:
        return unlimited

    token_rows: list[Any] = []
    usd_rows: list[Any] = []
    if any_rows is not False:
        # WHY the SAME _budget_sums the gate runs (not budget_status): this is
        # ONE implementation of the spend math, and visibility that disagrees
        # with the gate is worse than no visibility. Two aggregates for the
        # whole model list — the naive shape (check_budgets per model) would be
        # 2*len(models) ledger SUMs on the pool the data plane shares.
        try:
            # ONE shared lineage memo (audit P2-31, cross-validation): without
            # it each family re-reads the same api_keys row — the double PK
            # lookup the gate stopped paying. This runs per /v1/models listing
            # AND per routing decision, so it is a hot path too.
            with _shared_lineage(key_id):
                token_rows = list(await _budget_sums("budgets", "limit_tokens", "total_tokens", key_id, None))
                usd_rows = list(await _budget_sums("cost_budgets", "limit_usd", "cost_usd", key_id, None))
        except Exception:
            log.warning("model_capacity: budget lookup failed; reporting all models as unlimited", exc_info=True)
            return unlimited

    # TEAM SPEND — ONE aggregate for the whole listing. A derived envelope is
    # per-model, so every cap's scope is fetched together (team_cap_spend
    # dedupes and runs one statement, with the pooled '*' scope folded in); an
    # explicit envelope is one pooled number (team_spend).
    caps: tuple[teams.TeamModelCap, ...] = tuple(team.model_caps) if team_budget and team is not None else ()
    spends: dict[tuple[str, str], tuple[int, float]] = {}
    pooled_scope = ("*", team.budget_period) if team_budget and team is not None else None
    if team_budget and team is not None and pooled_scope is not None:
        try:
            if caps:
                scopes = [(c.model, c.period) for c in caps]
                if team.budget_tokens is not None or team.budget_usd is not None:
                    scopes.append(pooled_scope)
                spends = await teams.team_cap_spend(team.id, scopes)
            else:
                tokens_spent, usd_spent = await teams.team_spend(team.id, team.budget_period)
                spends = {pooled_scope: (tokens_spent, usd_spent)}
        except Exception:
            # FAIL-OPEN, same contract as the per-key lookup above: a team
            # aggregate fault degrades to the per-key verdict, never to an
            # empty listing. The gate still enforces on admission.
            log.warning("model_capacity: team spend lookup failed; team overlay skipped", exc_info=True)
            team_budget = False
        # A PARTIAL aggregate is not an envelope: _envelope_eval indexes the
        # scopes it needs, and a missing one would raise out of the listing.
        # Degrade to the per-key verdict instead (fail-open, same contract as
        # the faults above; the gate still enforces on admission).
        if team_budget and not {(c.model, c.period) for c in caps}.issubset(spends):
            log.warning("model_capacity: team spend aggregate is missing scopes; team overlay skipped")
            team_budget = False

    out: dict[str, dict] = {}
    # Per-SCOPE caches for the overflow swallow: a listing evaluates many
    # models, but the number of DISTINCT scopes is the number of configured
    # caps (or one pooled scope), NOT the number of models — so the whole
    # listing still costs at most one round trip per scope.
    scope_cache: dict[tuple[str, str, int | None, float | None], tuple[int | None, float | None]] = {}
    pooled_cache: dict[str, tuple[int | None, float | None]] = {}
    for m in models:
        # Matching semantics MIRROR the gate byte-for-byte: a row applies when
        # row["model"] == m or row["model"] == "*" (the same predicate the
        # ledger JOIN uses), and NO matching row means UNCAPPED, never blocked.
        matched_t = [r for r in token_rows if r["model"] == m or r["model"] == "*"]
        matched_u = [r for r in usd_rows if r["model"] == m or r["model"] == "*"]
        token_refusal, token_warn = _eval_token_rows(matched_t, est_tokens)
        usd_refusal, usd_warn = _eval_usd_rows(matched_u)
        own = token_refusal or usd_refusal or GateResult(True)
        entry: dict[str, Any] = {
            "ok": own.ok,
            "remaining_tokens": _token_headroom(matched_t),
            "blocked_by": "" if own.ok else own.reason,
            "warn": max(token_warn, usd_warn),
        }
        if team_budget and team is not None and pooled_scope is not None:
            caps_for_model = [c for c in caps if c.model == m or c.model == "*"]
            team_refusal, team_warn = _envelope_eval(team, caps_for_model, spends, pooled_scope)
            entry["team"] = {
                "period": team.budget_period,
                "ok": team_refusal is None,
                "warn": team_warn,
            }
            entry["warn"] = max(entry["warn"], team_warn)
            if team_refusal is not None:
                entry["ok"] = False
                entry["team"]["blocked_by"] = team_refusal.reason
                # REASON PARITY: when a per-key refusal cannot be swallowed
                # (overflow off) the gate SHORT-CIRCUITS the envelope and
                # reports the per-key reason; report the same one, or the
                # listing would name a pool the caller never hit first.
                if own.ok or team.overflow_enabled:
                    entry["blocked_by"] = team_refusal.reason
            elif not own.ok:
                # The per-key refusal may be SWALLOWED into the team pool
                # (overflow_enabled) — mirror the gate or the listing would
                # hide a model admission would have served.
                if caps_for_model:
                    standing = await _cap_overflow_verdict(team, own, caps_for_model, scope_cache)
                else:
                    standing = await _pooled_overflow_verdict(team, own, pooled_cache)
                entry["ok"] = standing.ok
                entry["blocked_by"] = "" if standing.ok else standing.reason
        out[m] = entry
    return out


async def budget_status_many(key_ids: list[uuid.UUID]) -> dict[uuid.UUID, list[dict]]:
    """Budget status for MANY keys in TWO total queries (audit finding 6 —
    GET /admin/keys ran the per-key budget_status twice-per-key: ~401
    sequential ledger aggregates at 200 keys, against the pool the data
    plane shares).

    Same shape as budget_status per key: {key_id: [status rows]}. The
    windowed SUM joins the same (key_id, ts) indexes; `IN ($1,...)` bounds
    the scan to the listed keys (the console pages, so the list is small).
    Spend is LINEAGE-SCOPED like the gate (spend_ancestors folded into the
    join key) so the console's budget view can never disagree with what
    the gate will enforce."""
    out: dict[uuid.UUID, list[dict]] = {kid: [] for kid in key_ids}
    if not key_ids:
        return out
    now = datetime.now(UTC)
    starts = {p: _window_start(now, p) for p in _WINDOW_PERIODS}
    # Lineage note (audit P2-5 + review round 3): spend_ancestors arrays are
    # VARIABLE-LENGTH per key, so they cannot bind as a flat uuid[] alongside
    # their keys (asyncpg rejects non-homogeneous nested arrays), and unnest
    # flattens uuid[][] to scalar uuid pairs (no uuid || uuid operator). The
    # correlated subselect below is a PK lookup per budget row — cheap at the
    # console page size this function serves — and is plain valid SQL.
    # (The single-key _budget_sums path binds its ONE lineage as a flat
    # uuid[] parameter, which is safe and done there.)
    placeholders = ", ".join(f"${i + 1}" for i in range(len(key_ids)))
    n = len(key_ids)

    for table, limit_col, sum_col in (
        ("budgets", "limit_tokens", "total_tokens"),
        ("cost_budgets", "limit_usd", "cost_usd"),
    ):
        _expr = _budget_token_expr("l") if sum_col == "total_tokens" else "SUM(l." + sum_col + " * l.billing_fraction)"
        rows = await db.fetch(
            f"""
            SELECT b.key_id, b.model, b.period, b.{limit_col},
                   -- cache-discounted token SUM (same expression as the gate)
                   COALESCE({_expr}, 0)::float AS spent
            FROM {table} b
            LEFT JOIN usage_ledger l
              ON l.key_id = ANY(
                   (SELECT a.spend_ancestors FROM api_keys a WHERE a.id = b.key_id) || b.key_id)
             AND l.status IN ({_COSTED_SQL})
             AND (l.model = b.model OR b.model = '*')
             AND l.ts >= CASE b.period
                         -- _WINDOW_PERIODS order, ELSE = the last one (year)
                         WHEN 'day' THEN ${n + 1}::timestamptz
                         WHEN 'week' THEN ${n + 2}::timestamptz
                         WHEN 'month' THEN ${n + 3}::timestamptz
                         WHEN 'quarter' THEN ${n + 4}::timestamptz
                         ELSE ${n + 5}::timestamptz END
            WHERE b.key_id IN ({placeholders})
            GROUP BY b.key_id, b.model, b.period, b.{limit_col}
            """,
            *key_ids,
            *(starts[p] for p in _WINDOW_PERIODS),
        )
        for r in rows:
            if table == "budgets":
                out[r["key_id"]].append(
                    {
                        "kind": "tokens",
                        "model": r["model"],
                        "period": r["period"],
                        "limit": r[limit_col],
                        "spent": int(r["spent"]),
                        "window_start": starts[r["period"]].isoformat(),
                    }
                )
            else:
                out[r["key_id"]].append(
                    {
                        "kind": "usd",
                        "model": r["model"],
                        "period": r["period"],
                        "limit_usd": float(r[limit_col]),
                        "spent_usd": float(r["spent"] or 0),
                        "window_start": starts[r["period"]].isoformat(),
                    }
                )
    return out
