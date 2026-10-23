"""Budget + rate-limit engine.

Two independent gates, both evaluated BEFORE an upstream call and settled
AFTER it:

1. Period budgets (day/week/month, per key and optionally per model) —
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
import time
import uuid
from collections.abc import Iterable, Mapping
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


VALID_PERIODS = frozenset({"day", "week", "month"})

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
    byte-identical to the pre-lineage behavior. The lineage read rides the
    same aggregate query (a scalar subselect against the PK-indexed
    api_keys row), so no extra round-trip is added.

    Perf audit (critical): the gate used to run one serial SUM per budget
    row (N+1 round-trips — ~14 sequential round-trips for a pinned request
    with 2 token + 2 cost budgets). The window start per period is computed
    HERE in Python (same _window_start math as before) and passed in, so
    window boundaries stay UTC-exact regardless of the DB session timezone.
    """
    now = datetime.now(UTC)
    starts = {p: _window_start(now, p) for p in ("day", "week", "month")}
    model_cond = "AND (b.model = '*' OR b.model = $2)" if model is not None else ""
    # Perf (audit P2-5): read spend_ancestors ONCE and pass it as a uuid[]
    # parameter. The old join built it via a correlated subselect INSIDE the
    # join predicate; the ANY(array) shape already blocks an equality probe
    # on idx_ledger_key_ts, and per-budget-row PK lookups made that strictly
    # worse. One fetch here keeps the helper self-contained.
    # fetchrow preferred (the row shape carries the column name); fetchval
    # fallback (older seams/fakes expose only scalars).
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
    # Defensive shape check (a fake/other DB seam returning a scalar must
    # not 500 the gate): anything but a list/tuple is treated as no lineage.
    ancestors: list[uuid.UUID] = list(_anc_val) if isinstance(_anc_val, (list, tuple)) else []
    args: list[Any] = [key_id]
    if model is not None:
        args.append(model)
    args += [starts["day"], starts["week"], starts["month"]]
    # Number the windows BEFORE appending the lineage (real-PG-verified fix:
    # the old order made the lineage the LAST param and the month window
    # reused its number — one placeholder cannot be both uuid[] and
    # timestamptz, so prepare() failed with CannotCoerceError and every
    # budgeted chat call 500d). Windows sit at n_win-2 .. n_win.
    n_win = len(args)
    args.append(ancestors + [key_id])
    lineage = "$" + str(len(args)) + "::uuid[]"
    # Placeholder-collision guard (real-PG lesson from this wave): each
    # numbered placeholder must map to exactly ONE argument — a number
    # reused with two casts (uuid[] + timestamptz) fails only at real
    # prepare(), which no fake seam can catch.
    # Unconditional (not `assert` — stripped under python -O; S101 class):
    _lineage_num = int(lineage[1:].split("::")[0])
    _win_nums = (n_win - 2, n_win - 1, n_win)
    if _lineage_num != len(args) or _lineage_num <= n_win or len(set(_win_nums)) != 3:
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
          ON l.key_id = ANY({lineage})
         AND l.status IN ('ok', 'error')   -- cache_hit rows are FREE
         AND (l.model = b.model OR b.model = '*')
         AND l.ts >= CASE b.period
                     -- explicit casts: bare params inside a CASE fall back to
                     -- text in Postgres' type resolution → "timestamptz >= text"
                     WHEN 'day' THEN ${n_win - 2}::timestamptz
                     WHEN 'week' THEN ${n_win - 1}::timestamptz
                     ELSE ${n_win}::timestamptz END
        WHERE b.key_id = $1 {model_cond}
        GROUP BY b.model, b.period, b.{limit_col}
        ORDER BY b.period, b.model
        """,
        *args,
    )


async def check_budgets(key_id: uuid.UUID, model: str, est_tokens: int = 0) -> GateResult:
    """Refuse the call if any matching period budget is exhausted.

    GateResult.warn carries the highest crossed threshold (80/95) when a
    matching budget is near exhaustion — surfaced by the proxy as an
    X-Budget-Warning response header (the log line / gauge claims of an
    earlier docstring were never implemented — doc drift A2). Pure
    visibility: NO behavior change (soft-threshold rerouting was
    deliberately rejected — see the cascade design decision).
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

    warn = 0
    # The admission estimate has no cached split (usage does not exist yet),
    # so it is discounted by the SAME fraction the settle discounts the row
    # with — assume the request may be as cache-served as the conversation
    # it continues. The ledger settle brings the truth either way, so the
    # residual budget overshoot is bounded by the CONCURRENT in-flight
    # requests' undiscounted remainder (the same trade the post-paid USD
    # gate accepts); frac=0 keeps the byte-identical legacy charge.
    _frac = min(1.0, max(0.0, float(settings.budget_cached_exempt_fraction)))
    est_billable = int(est_tokens * (1.0 - _frac))
    for b in await _budget_sums("budgets", "limit_tokens", "total_tokens", key_id, model):
        spent = int(b["spent"])
        if spent + est_billable > b["limit_tokens"]:
            # WHICH budget family refused — the metric label (tokens|usd)
            # lets an operator tell "tenant hit its token cap" from
            # "tenant hit its dollar cap" without joining the ledger.
            _metrics.BUDGET_REJECTIONS.labels(kind="tokens").inc()
            return GateResult(False, f"{b['period']} budget {b['limit_tokens']} exceeded (spent {spent})")
        pct = spent * 100 // b["limit_tokens"]
        if pct >= 95:
            warn = max(warn, 95)
        elif pct >= 80:
            warn = max(warn, 80)

    # --- USD budgets (whichever limit hits first stops the key) ---
    # Post-paid: the in-flight request's true cost is known only after the
    # response, so the gate is spent >= limit — the overshoot is bounded by
    # one response's cost.
    for cb in await _budget_sums("cost_budgets", "limit_usd", "cost_usd", key_id, model):
        spent_usd = float(cb["spent"] or 0)
        limit = float(cb["limit_usd"])
        if spent_usd >= limit:
            _metrics.BUDGET_REJECTIONS.labels(kind="usd").inc()
            return GateResult(False, f"{cb['period']} cost budget ${limit:.2f} exceeded (spent ${spent_usd:.4f})")
        pct = int(spent_usd * 100 / limit) if limit else 0
        if pct >= 95:
            warn = max(warn, 95)
        elif pct >= 80:
            warn = max(warn, 80)
    return GateResult(True, warn=warn)


async def check_rate_limit(key_id: uuid.UUID, rpm: int, tpm: int, est_tokens: int, request_id: str = "") -> GateResult:
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
    # the key's OWN measured cache ratio, so a cache-heavy conversation is
    # not refused for tokens the upstream will serve from prefix cache.
    # Without the flag this is exactly est_tokens (byte-identical legacy).
    charge = await predicted_charge(key_id, est_tokens)
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
    remaining_requests = max(0, rpm - row["requests"])
    remaining_tokens = max(0, tpm - row["tokens"])
    if row["requests"] > rpm:
        _metrics.RATE_LIMITED.labels(scope="rpm").inc()
        await _release_rejected_charge(key_id, start, charge)
        return GateResult(
            False,
            f"RPM limit {rpm} exceeded ({row['requests']} this minute)",
            remaining_requests=remaining_requests,
            remaining_tokens=remaining_tokens,
        )
    if row["tokens"] > tpm:
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
_predict_cache: dict[uuid.UUID, tuple[float, float]] = {}  # key -> (ratio, expires_at)


def _predict_ttl() -> float:
    return float(getattr(settings, "rate_limit_predict_ttl_s", 60) or 60)


def _predict_enabled() -> bool:
    return bool(getattr(settings, "rate_limit_predict_cached", False))


async def measured_cache_ratio(key_id: uuid.UUID) -> float:
    """This key's token-weighted cache ratio over its recent ledger rows.

    WHY a MEASURED ratio rather than a blind discount: the admission gate has
    no upstream usage to consult (the call has not run yet), so the only
    honest predictor of "how much of this prompt will the prefix cache serve"
    is how much it served for this key recently. A blind fraction would give
    an uncached caller the same discount as a cached one - a rate-limit
    bypass. A measured ratio is self-limiting: to raise it you must actually
    be cache-served.

    Returns 0.0 when there is no history (a new key pays the FULL estimate)
    or when the feature/ledger is unavailable - fail-safe to the legacy
    charge, never to a discount.
    """
    now = time.monotonic()
    hit = _predict_cache.get(key_id)
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
                WHERE key_id = $1 AND status = 'ok' AND prompt_tokens > 0
                ORDER BY ts DESC
                LIMIT $2
            ) recent
            """,
            key_id,
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
    _predict_cache[key_id] = (ratio, now + _predict_ttl())
    # Bound the cache: a gateway with many keys must not grow this unboundedly.
    if len(_predict_cache) > 4096:
        for k in [k for k, v in _predict_cache.items() if v[1] <= now]:
            _predict_cache.pop(k, None)
    return ratio


async def predicted_charge(key_id: uuid.UUID, est_tokens: int) -> int:
    """The admission charge: the estimate discounted by the MEASURED ratio.

    Returns est_tokens unchanged when prediction is off (byte-identical
    legacy charge), so the flag is a true kill switch.
    """
    if not _predict_enabled() or est_tokens <= 0:
        return est_tokens
    ratio = await measured_cache_ratio(key_id)
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
    if not _predict_enabled() or not bool(getattr(settings, "rate_limit_predict_reconcile", True)):
        return
    if prompt_tokens <= 0 or not request_id:
        return
    # The charge this request ACTUALLY made at admission. Recomputing it from
    # the ratio here would be wrong: the ratio is refreshed from the ledger,
    # which now contains this very row - the prediction would grade itself.
    remembered = _predict_charges.pop(request_id, None)
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
    than the reported cache hit gives back what it has and no more. Never an
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
    frac = min(1.0, max(0.0, float(settings.budget_cached_exempt_fraction)))
    t = f"{alias}.total_tokens"
    f = f"{alias}.billing_fraction"
    if frac <= 0.0:
        return f"SUM({t} * {f})"
    c = f"{alias}.cached_tokens"
    return f"SUM(GREATEST({t} - LEAST({c}, {t}) * {frac}, 0) * {f})"


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
) -> None:
    """Write the immutable usage row. Called for every request outcome.

    cached_tokens: tokens served from cache — upstream prompt-cache
    (vLLM cached_tokens) or full embedding-cache hits (status='cache_hit';
    those rows cost ZERO budget — the budget SUM excludes them — but stay
    visible for the cache-hits reporting). Cost: computed from model_costs
    AT REQUEST TIME and stored on the row (repricing never rewrites
    history); NULL when unpriced.

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
    """
    cost = None
    if status == "cache_hit":
        # served from OUR cache — no upstream spend, even if the model is priced
        from decimal import Decimal

        cost = Decimal("0.000000")
    elif status == "ok":
        from . import costs as _costs

        cost = await _costs.compute(model, prompt_tokens, completion_tokens, cached_tokens)
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
             routing_policy, routing_ceiling, routing_served, routing_reason)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17)
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
    #     REFUND below is the only correction (legacy behaviour, unchanged).
    #   prediction ON  -> the charge was ALREADY discounted by the measured
    #     ratio, so a refund would discount a second time. RECONCILIATION
    #     alone settles it: charged + (true_billable - charged) == true
    #     billable exactly, whatever the predicted ratio was.
    #
    # status='cache_hit' never ran a gate, so neither applies.
    predicted = request_id in _predict_charges
    if status != "cache_hit":
        if predicted:
            try:
                await _reconcile_predicted_charge(key_id, request_id, prompt_tokens, cached_tokens)
            except Exception:
                log.warning("settle: predicted-charge reconciliation failed (non-fatal)", exc_info=True)
        elif settings.rate_limit_exclude_cached_tokens:
            try:
                await _refund_window_tokens(key_id, cached_tokens)
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
    starts = {p: _window_start(now, p) for p in ("day", "week", "month")}
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
             AND l.status IN ('ok', 'error')
             AND (l.model = b.model OR b.model = '*')
             AND l.ts >= CASE b.period
                         WHEN 'day' THEN ${n + 1}::timestamptz
                         WHEN 'week' THEN ${n + 2}::timestamptz
                         ELSE ${n + 3}::timestamptz END
            WHERE b.key_id IN ({placeholders})
            GROUP BY b.key_id, b.model, b.period, b.{limit_col}
            """,
            *key_ids,
            starts["day"],
            starts["week"],
            starts["month"],
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
