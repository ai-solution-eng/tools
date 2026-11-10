"""Team leader tier: enrollment-scoped console + the team budget envelope.

WHAT: an enrollment code (POST /v1/enroll) is also a TEAM INVITE. Every key
redeemed from it carries api_keys.enrollment_id, which gives that enrollment

  * a HARD budget ENVELOPE over the whole team (enrollments.budget_tokens /
    budget_usd, per budget_period), enforced by core.budgets.check_budgets —
    the ONE enforcement point, so the per-key gate and the team cap can never
    disagree about what "spent" means (both SUM the same ledger rows with the
    same weighting), and
  * a scoped CONTROL PLANE: a team leader key (hash-only, the analog of the
    admin key) whose holder sees exactly the keys minted from that
    enrollment, that enrollment's usage and that enrollment's traces
    (api/routes_team_leader.py; every endpoint is scoped by enrollment id).

BUDGET MODEL: budgets/cost_budgets stay PER KEY — that row is a teammate's
ALLOCATION. The enrollment's caps are a hard cap on the SUM across the
team's keys. With overflow_enabled (DEFAULT TRUE) a key may exceed its own
allocation while the envelope still has headroom: the unallocated remainder
of the team budget is a shared overflow pool. With overflow_enabled FALSE
each key is hard-capped at its own allocation (and the envelope still caps
the team).

CREDENTIAL DISCIPLINE (same as core.keys and the enrollment code itself):
only the SHA-256 hash is stored; the plaintext is returned exactly ONCE at
mint/rotate and never again — never logged, never projected, never returned
by any read path.

FAIL CLOSED (the core.keys.authenticate posture): a revoked leader key, a
REVOKED enrollment (a closed team closes its console) and an EXPIRED
enrollment all resolve to None in authenticate_leader, and the route
dependency maps None to 401.
"""

from __future__ import annotations

import json
import logging
import math
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from ..db import db
from . import keys as _keys

log = logging.getLogger(__name__)

# Valid enrollment budget periods. Validated APP-SIDE (no CHECK constraint —
# matches the repo's tolerance for raw JSON presets written by older builds),
# so every read path normalizes through _normalize_period instead of trusting
# the column. Mirrors core.budgets.VALID_PERIODS by construction; kept here so
# the route layer imports ONE team constant rather than reaching into the
# budget engine.
TEAM_PERIODS = frozenset({"day", "week", "month", "quarter", "year"})
DEFAULT_TEAM_PERIOD = "month"

# enrollment_for_key is the BUDGET GATE's lookup: it runs on every admission
# (and every relay iteration), and the overwhelming majority of keys are not
# in a team at all. A short TTL cache makes the negative answer free after the
# first miss while keeping a budget/overflow edit visible within half a minute
# (invalidate() makes it immediate — every write path calls it).
_CACHE_TTL = 30.0
# Bound: keys are unbounded (one per teammate mint), so the dict must never
# grow with them. Wholesale clear past the bound (the same discipline as the
# other process-local caches in this codebase) — it only costs re-reads.
_CACHE_MAX = 10_000


class _CacheMiss:
    """Sentinel: "not cached" (as opposed to a cached NEGATIVE, which is None)."""


_CACHE_MISS = _CacheMiss()
_CACHE: dict[uuid.UUID, tuple[float, TeamEnrollment | None]] = {}


@dataclass(frozen=True)
class TeamModelCap:
    """One DERIVED per-model team cap: a profile preset x the mint allowance.

    The per-model shape of the derived envelope (2026-10-30): an enrollment
    minted with a profile but no explicit cap does not get ONE pooled number —
    it gets EVERY preset scaled by N, so the team pool for GLM-month is
    N x the GLM-month preset, the day pool is N x the day preset, and so on.
    Overflow draws on the pool of the limit that refused, which is what makes
    "the overflow respects the N x profile limits" literally true.

    model '*' = the any-model preset: a POOLED cap across all models for that
    period (the same convention split_preset_budgets stamps onto keys).
    Exactly one of limit_tokens / limit_usd is set — the preset XOR rule.
    """

    model: str
    period: str
    limit_tokens: int | None
    limit_usd: float | None


@dataclass
class TeamEnrollment:
    """One enrollment's TEAM view: the envelope the gate enforces.

    Deliberately NOT the enrollment code row: the code hash, the uses counter
    and the expiry stay in api/routes_enroll.py — this is the projection the
    budget gate and the team console both need, and nothing more.
    """

    id: uuid.UUID
    label: str
    tenant_id: uuid.UUID
    profile_id: uuid.UUID | None
    budget_tokens: int | None  # None = no team token cap
    budget_usd: float | None  # None = no team USD cap
    budget_period: str  # 'day' | 'week' | 'month'
    overflow_enabled: bool
    # The enrollment's mint allowance (0 = unlimited). The console renders
    # "active / N" from it, and it is the N in the DERIVED envelope.
    max_uses: int = 0
    # True when budget_tokens/budget_usd were NOT set on the row but were
    # derived from the profile presets x max_uses (2026-10-30). The console
    # says so, so an operator can tell "I capped this" from "the profile did".
    budget_derived: bool = False
    # The PER-MODEL caps of a derived envelope (empty for an explicit one).
    # Carrying them on the same object keeps the gate's verdict and the two
    # consoles' display on one row — the single-projection discipline.
    model_caps: tuple[TeamModelCap, ...] = ()
    # Which dimensions the derivation filled ("tokens", "usd"): the gate
    # enforces a derived dimension through its per-model caps and an
    # explicitly-set dimension through the pooled cap, never both.
    derived_dims: frozenset[str] = frozenset()

    @property
    def has_budget(self) -> bool:
        """True when the team has ANY budget to enforce — the gate's "is this a
        team" test.

        A pooled cap of either kind, OR at least one derived per-model cap
        (audit W6 P2-8): a profile that budgets only in a period other than the
        enrollment's derives no pooled number but REAL caps, and the gate must
        consult them instead of skipping the envelope. An enrollment with
        neither keeps today's per-key behavior byte for byte (its keys are
        grouped for reporting only; nothing is enforced).
        """
        return self.budget_tokens is not None or self.budget_usd is not None or bool(self.model_caps)


def _preset_entries(defaults_json: Any) -> list[Any] | None:
    """The profile's budget-preset list, tolerantly parsed (None = no usable
    presets). Shared by the pooled and per-model derivations so the two can
    never disagree about what the profile says."""
    raw = defaults_json
    if isinstance(raw, str):
        try:
            raw = json.loads(raw or "{}")
        except (TypeError, ValueError):
            return None
    if not isinstance(raw, dict):
        return None
    entries = raw.get("budgets")
    if not isinstance(entries, (list, tuple)) or not entries:
        return None
    return list(entries)


def derive_envelope(
    defaults_json: Any, max_uses: Any, period: str
) -> tuple[int | None, float | None, tuple[TeamModelCap, ...]]:
    """The TEAM envelope implied by the enrollment's profile x its mint
    allowance: the pooled (tokens, usd) cap for ONE period PLUS the per-model
    caps of EVERY preset the profile carries.

    WHY (2026-10-30): an enrollment minted WITH a profile but WITHOUT an
    explicit cap left the team uncapped — "no cap" on the console, and
    overflow_enabled had no pool to overflow INTO (the gate skips the envelope
    entirely when has_budget is False), so a key's own allocation was the only
    bound and the auto-overflow switch did nothing. The profile already states
    the per-key limits and the enrollment already states how many keys it may
    mint, so N x per-key IS the team ceiling; derive it instead of making the
    operator retype it.

    ONE cap per (model, kind, PERIOD): a preset keeps its OWN period, so a daily
    preset is a DAILY team pool (10,000 x 4 = 40,000) and a monthly one a
    MONTHLY pool (1,000,000 x 4 = 4,000,000). Both bind — the gate checks each
    cap against ITS OWN period's spend — and the pooled number below is the
    monthly OUTER bound, never a substitute for the day pools.

    The pooled number is the SUM of the presets whose period MATCHES `period`,
    x N (a daily and a monthly limit are different envelopes in time, never
    additive). The per-model caps cover EVERY valid preset, so a profile that
    budgets only in another period still yields REAL caps:
    TeamEnrollment.has_budget counts a caps-only team as a team (audit W6 P2-8),
    which is what stops the gate from skipping the envelope entirely.
    THE OVERFLOW RULE: a per-key refusal is swallowed only into the pool of the
    SAME model+period limit — each N x preset is its own overflow pool,
    enforced by the gate (core.budgets._apply_team_model_caps).

    Rules (each one exists so the number can never be invented):

    * max_uses == 0 means UNLIMITED mints, so there is no N and no derived cap
      (None, None, ()): an honest "no cap", never a guess;
    * a preset whose period is not a team period is skipped entirely — the
      profile-save path refuses those anyway, and a hand-written junk period
      must not invent a window;
    * the preset XOR rule (exactly one of limit_tokens / limit_usd) is applied
      the same way the key-stamping path applies it: tokens win if a
      hand-written entry carries both;
    * DEDUPE NEVER MULTIPLIES A CAP: two presets for ONE (model, kind, period)
      collapse to ONE cap carrying the LARGER limit — never their sum (which
      would invent a ceiling nobody wrote), and never the smaller (which would
      shrink the operator's own number for that model);
    * unreadable JSON or an invalid preset degrades to (None, None, ()) — the
      same fail-safe direction as team_enrollment_from_row.
    """
    try:
        n = int(max_uses or 0)
    except (TypeError, ValueError):
        return (None, None, ())
    if n <= 0:
        return (None, None, ())
    entries = _preset_entries(defaults_json)
    if entries is None:
        return (None, None, ())
    # The caps are already SCALED by n. ONE cap per (model, kind, PERIOD): the
    # preset keeps its OWN period, so a daily preset stays a DAILY team pool and
    # a monthly one a MONTHLY pool — the two are different envelopes in time and
    # both bind (the gate checks each cap against its own period's spend).
    # Keyed by (model, kind, period) so a duplicated preset collapses instead of
    # double-counting; ordered so the cap list stays deterministic.
    best: dict[tuple[str, str, str], int | float] = {}
    order: list[tuple[str, str, str]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        cap_period = entry.get("period")
        if cap_period not in TEAM_PERIODS:
            continue
        model = str(entry.get("model") or "*")
        lt = entry.get("limit_tokens")
        lu = entry.get("limit_usd")
        key: tuple[str, str, str]
        value: int | float
        if isinstance(lt, int) and not isinstance(lt, bool) and lt > 0:
            key, value = (model, "tokens", str(cap_period)), lt * n
        elif isinstance(lu, (int, float)) and not isinstance(lu, bool) and lu > 0:
            key, value = (model, "usd", str(cap_period)), float(lu) * n
        else:
            continue
        if key not in best:
            order.append(key)
            best[key] = value
        elif value > best[key]:
            # two presets for ONE (model, kind, period): the LARGER limit wins.
            # Never their sum (that would invent a ceiling nobody wrote, and
            # would multiply a cap by the number of presets).
            best[key] = value
    tokens = 0
    usd = 0.0
    caps: list[TeamModelCap] = []
    for model, kind, cap_period in order:
        cap_value: int | float = best[(model, kind, cap_period)]
        if kind == "tokens":
            caps.append(TeamModelCap(model=model, period=cap_period, limit_tokens=int(cap_value), limit_usd=None))
            if cap_period == period:
                tokens += int(cap_value)
        else:
            caps.append(TeamModelCap(model=model, period=cap_period, limit_tokens=None, limit_usd=float(cap_value)))
            if cap_period == period:
                usd += float(cap_value)
    return (tokens or None, usd or None, tuple(caps))


def apply_derived_envelope(
    defaults_json: Any,
    max_uses: Any,
    period: str,
    budget_tokens: int | None,
    budget_usd: float | None,
) -> tuple[int | None, float | None, frozenset[str], tuple[TeamModelCap, ...]]:
    """Fill the absent envelope dimensions from the profile and return the caps IN FORCE.

    The ONE implementation shared by the gate's row mapper and the admin listing
    (2026-10-30 review: the listing re-derived on its own and could display caps of a
    dimension the row had capped explicitly — display contradicting enforcement).
    An explicit row value wins PER DIMENSION; a derived dimension's caps are the
    N x preset pools the gate enforces; caps of an explicitly-capped dimension are
    dropped.
    """
    derived_dims: frozenset[str] = frozenset()
    model_caps: tuple[TeamModelCap, ...] = ()
    if budget_tokens is None or budget_usd is None:
        d_tokens, d_usd, raw_caps = derive_envelope(defaults_json, max_uses, period)
        dims: set[str] = set()
        if budget_tokens is None and d_tokens is not None:
            budget_tokens = d_tokens
            dims.add("tokens")
        if budget_usd is None and d_usd is not None:
            budget_usd = d_usd
            dims.add("usd")
        derived_dims = frozenset(dims)
        if derived_dims:
            kept: list[TeamModelCap] = []
            for c in raw_caps:
                c2 = TeamModelCap(
                    c.model,
                    c.period,
                    c.limit_tokens if "tokens" in derived_dims else None,
                    c.limit_usd if "usd" in derived_dims else None,
                )
                if c2.limit_tokens is not None or c2.limit_usd is not None:
                    kept.append(c2)
            model_caps = tuple(kept)
        else:
            # NOTHING was derived into a POOLED dimension, but the per-model
            # pools are still REAL (a profile that budgets only in a period
            # other than the enrollment's). Keeping them is what lets
            # has_budget be true for a caps-only team; dropping them made the
            # gate skip the envelope entirely (audit W6 P2-8).
            model_caps = tuple(raw_caps)
    return budget_tokens, budget_usd, derived_dims, model_caps


def _derived_envelope(defaults_json: Any, max_uses: Any, period: str) -> tuple[int | None, float | None]:
    """The POOLED half of derive_envelope, under its pinned name: the console
    listing and the row mapper share it, and the tests quote the 2-tuple."""
    tokens, usd, _caps = derive_envelope(defaults_json, max_uses, period)
    return (tokens, usd)


def _normalize_period(period: str | None) -> str:
    """Map a stored budget_period onto TEAM_PERIODS.

    budget_period has no CHECK constraint (see the schema comment), so a row
    written by hand or by an older build can hold anything. An unknown value
    must never mean "no window" — it falls back to the schema default, which
    is also the window core.budgets._window_start would pick for it. NULL is
    silent (a row/fake predating the column); a non-NULL junk value is logged
    so an operator can see the bad row.
    """
    if period is None:
        return DEFAULT_TEAM_PERIOD
    if period in TEAM_PERIODS:
        return str(period)
    log.warning(
        "enrollment budget_period %r is not one of %s — treating it as %r",
        period,
        sorted(TEAM_PERIODS),
        DEFAULT_TEAM_PERIOD,
    )
    return DEFAULT_TEAM_PERIOD


def team_enrollment_from_row(row: Any) -> TeamEnrollment | None:
    """Row -> TeamEnrollment, tolerant of partial/fake row shapes.

    Returns None (never an exception) for a row that does not look like an
    enrollment: this mapper is on the admission path (check_budgets) and is
    also fed by the fake db seams the unit suite installs, so an unexpected
    shape must degrade to "no team" — the per-key budgets still apply, which
    is the fail-safe direction. Same .get()-based discipline as
    core.keys.key_record_from_row.

    'label' is the discriminator: it is TEXT NOT NULL in the schema, so a row
    carrying id/tenant_id but no label column came from some OTHER query the
    seam answered for us, and guessing would be worse than answering "none".
    """
    if row is None:
        return None
    getter = getattr(row, "get", None)
    if getter is None:
        return None  # not a mapping/Record (e.g. a bare tuple): never guess
    enrollment_id = getter("id")
    tenant_id = getter("tenant_id")
    label = getter("label")
    if enrollment_id is None or tenant_id is None or label is None:
        return None
    raw_tokens = getter("budget_tokens")
    raw_usd = getter("budget_usd")
    try:
        budget_tokens = int(raw_tokens) if raw_tokens is not None else None
        # asyncpg hands NUMERIC back as Decimal; the gate wants a float it can
        # isfinite() (a NaN/Infinity row must reach that guard, not raise here).
        budget_usd = float(raw_usd) if raw_usd is not None else None
    except (TypeError, ValueError):
        log.warning("enrollment %s has an unreadable budget envelope — ignoring it", enrollment_id)
        return None
    overflow = getter("overflow_enabled")
    period = _normalize_period(getter("budget_period"))
    try:
        max_uses = int(getter("max_uses") or 0)
    except (TypeError, ValueError):
        max_uses = 0
    # DERIVED ENVELOPE (2026-10-30): an enrollment minted with a profile but no
    # explicit cap gets N x the profile's per-key limits, so the gate actually
    # has a pool to enforce and overflow has something to overflow into. An
    # explicit row value always wins PER DIMENSION; a seam that does not carry
    # defaults_json/max_uses simply derives nothing (the old behaviour).
    # 2026-10-30, per-model: the derivation also yields EVERY preset scaled by
    # N as its own team pool (TeamModelCap), and the caps keep only the
    # dimensions the derivation actually filled — an explicitly-capped
    # dimension stays pooled, never both shapes on one dimension.
    budget_tokens, budget_usd, derived_dims, model_caps = apply_derived_envelope(
        getter("defaults_json"), max_uses, period, budget_tokens, budget_usd
    )
    return TeamEnrollment(
        id=enrollment_id,
        label=str(label),
        tenant_id=tenant_id,
        profile_id=getter("profile_id"),
        budget_tokens=budget_tokens,
        budget_usd=budget_usd,
        budget_period=period,
        # Schema default is TRUE; a seam that predates the column reads as the
        # default rather than as a hard cap (the envelope still applies).
        overflow_enabled=True if overflow is None else bool(overflow),
        max_uses=max_uses,
        budget_derived=bool(derived_dims),
        model_caps=model_caps,
        derived_dims=derived_dims,
    )


# The ONE projection every read path shares: the gate's lookup, the leader
# authentication and (via team_enrollment_from_row) the console. Keeping a
# single column list is what makes "what the console shows" and "what the gate
# enforces" the same row.
_ENROLLMENT_COLS = """
       e.id, e.label, e.tenant_id, e.profile_id,
       e.budget_tokens, e.budget_usd, e.budget_period, e.overflow_enabled,
       e.max_uses, p.defaults_json
"""


async def authenticate_leader(plaintext: str) -> TeamEnrollment | None:
    """Resolve a team leader key to its enrollment, enforcing every kill switch.

    Returns None for an unknown key, a revoked leader key, a revoked
    enrollment (closing the team closes its console) and an expired
    enrollment — callers MUST treat that as 401 (fail closed). The predicates
    live in SQL, so there is no window in which a stale Python copy of the row
    could authorize; a miss is None, and only a DB fault propagates.

    The plaintext is hashed with the SAME function as every other credential
    in the gateway (core.keys.hash_key): one hash discipline, one place to
    change it.
    """
    if not plaintext:
        return None  # a missing bearer must not become a hash of ''
    row = await db.fetchrow(
        f"""
        SELECT {_ENROLLMENT_COLS}
        FROM enrollments e
        LEFT JOIN profiles p ON p.id = e.profile_id
        WHERE e.team_leader_key_hash = $1
          AND e.team_leader_key_revoked_at IS NULL
          AND e.revoked_at IS NULL
          AND (e.expires_at IS NULL OR e.expires_at > now())
        """,
        _keys.hash_key(plaintext),
    )
    return team_enrollment_from_row(row)


async def mint_leader_key(enrollment_id: uuid.UUID, *, conn: Any | None = None) -> tuple[str, str]:
    """Mint (or ROTATE) an enrollment's team leader key: (plaintext, prefix).

    One statement, so a rotate can never leave a half-written credential:
    hash + prefix + created_at land together, revoked_at is cleared (which is
    exactly "re-mint un-revokes") and the LAST-USED stamp is reset (audit W6
    F3) — otherwise the new credential inherits the dead key's timestamp, and
    stamp_leader_used's one-hour throttle would keep reporting it as the live
    key's use. The plaintext is returned to the caller and is NOT stored
    anywhere (hash-only, same as api_keys and the code).

    conn: pass an ACQUIRED connection to join a caller's transaction (the
    admin mint-with-enrollment path writes the code and the leader key as one
    unit). None = the module pool.

    Raises LookupError when the enrollment row does not exist — the caller
    owns the HTTP mapping (404), and a silent success here would hand back a
    credential for a row that was deleted concurrently.
    """
    plaintext, key_hash = _keys.generate_key()
    prefix = plaintext[:10]  # same prefix convention as api_keys.key_prefix
    row = await (conn if conn is not None else db).fetchrow(
        """
        UPDATE enrollments
           SET team_leader_key_hash          = $2,
               team_leader_key_prefix        = $3,
               team_leader_key_created_at    = now(),
               team_leader_key_revoked_at    = NULL,
               -- a rotation starts a FRESH usage record (audit W6 F3): the old
               -- stamp belongs to the dead credential, and the 1 h throttle in
               -- stamp_leader_used would otherwise keep showing it
               team_leader_key_last_used_at  = NULL
         WHERE id = $1
        RETURNING id
        """,
        enrollment_id,
        key_hash,
        prefix,
    )
    if row is None:
        raise LookupError(f"enrollment {enrollment_id} not found")
    return plaintext, prefix


async def revoke_leader_key(enrollment_id: uuid.UUID, *, conn: Any | None = None) -> bool:
    """Revoke the enrollment's leader key. True only for a LIVE key.

    Idempotent by predicate: the second call matches no row (revoked_at is
    already set) and answers False, so a console retry can never look like a
    failure and can never re-stamp the timestamp. An enrollment that never had
    a leader key also answers False (hash IS NULL) — nothing was revoked.

    The hash is deliberately NOT cleared: the prefix + created_at stay as the
    audit trail of the credential that existed, and authenticate_leader's
    revoked_at predicate is what denies it.
    """
    tag = await (conn if conn is not None else db).execute(
        """
        UPDATE enrollments SET team_leader_key_revoked_at = now()
         WHERE id = $1
           AND team_leader_key_hash IS NOT NULL
           AND team_leader_key_revoked_at IS NULL
        """,
        enrollment_id,
    )
    return tag == "UPDATE 1"


async def enrollment_for_key(key_id: uuid.UUID) -> TeamEnrollment | None:
    """The BUDGET GATE's lookup: which team does this key belong to?

    One round-trip (api_keys -> enrollments on the FK, both PK-indexed) and
    cached for _CACHE_TTL. NEGATIVE RESULTS ARE CACHED TOO — most keys belong
    to no team, and paying a query per admission for the majority would be a
    regression on the data plane. A DB error is NOT cached: it propagates, so
    the caller's tolerance wrapper (core.budgets._team_for_key) decides what a
    broken lookup means and the next call re-reads immediately.
    """
    cached = _cache_get(key_id)
    if not isinstance(cached, _CacheMiss):
        return cached  # a cached None IS the answer: this key has no team
    row = await db.fetchrow(
        f"""
        SELECT {_ENROLLMENT_COLS}
        FROM api_keys k
        JOIN enrollments e ON e.id = k.enrollment_id
        LEFT JOIN profiles p ON p.id = e.profile_id
        WHERE k.id = $1
        """,
        key_id,
    )
    team = team_enrollment_from_row(row)
    _cache_put(key_id, team)
    return team


async def key_ids_in_enrollment(enrollment_id: uuid.UUID) -> list[uuid.UUID]:
    """Every key minted from this enrollment (revoked ones included).

    No cache: this backs the console's key list and the rotate/usage paths,
    where a just-minted key must be visible on the next read.
    """
    rows = await db.fetch("SELECT id FROM api_keys WHERE enrollment_id = $1", enrollment_id)
    return [r["id"] for r in rows]


async def team_spend(enrollment_id: uuid.UUID, period: str) -> tuple[int, float]:
    """(tokens, usd) spent by the WHOLE team in the current window.

    MUST count exactly what the per-key gate counts, or the console and the
    gate would tell two different stories about one budget:

      * status IN core.budgets._COSTED_SQL — real upstream spend only:
        cache_hit rows are free (no upstream spend), and a post-call
        'guardrail_blocked' row IS counted because the GPU time was spent
        (audit r5 #9). The SAME set every ledger SUM in core.budgets uses,
        imported rather than re-spelled so the two can never drift;
      * the token SUM is core.budgets._budget_token_expr('l') — the SAME
        cache-discounted expression the per-key gate sums, so a team cap can
        never be consumed by tokens the per-key caps exempt;
      * the USD SUM is the plain fraction-weighted cost (costs.compute already
        prices cached tokens at cached_per_m — discounting again here would
        double-count the exemption, the same reason the per-key gate keeps it
        exact);
      * window start from core.budgets._window_start(datetime.now(UTC), ...)
        so the boundaries are UTC-exact regardless of the DB session timezone.

    Keys are matched by api_keys.enrollment_id (not by the lineage array): a
    rotated key keeps its enrollment, and each ledger row is counted once
    because it carries exactly one key_id.
    """
    # Lazy import: core.budgets imports THIS module at module scope (the
    # envelope's only enforcement point), so these two helpers can only be
    # imported at call time. Both are pure.
    from .budgets import _COSTED_SQL, _budget_token_expr, _window_start

    start = _window_start(datetime.now(UTC), _normalize_period(period))
    token_expr = _budget_token_expr("l")
    row = await db.fetchrow(
        f"""
        SELECT COALESCE({token_expr}, 0)::float AS tokens,
               COALESCE(SUM(l.cost_usd * l.billing_fraction), 0)::float AS usd
        FROM usage_ledger l
        JOIN api_keys k ON k.id = l.key_id
        WHERE k.enrollment_id = $1
          AND l.status IN ({_COSTED_SQL})   -- cache_hit rows are FREE
          AND l.ts >= $2::timestamptz
        """,
        enrollment_id,
        start,
    )
    if row is None:
        return 0, 0.0  # a seam with no aggregate to report = nothing spent
    return int(row["tokens"] or 0), float(row["usd"] or 0.0)


async def team_cap_spend(
    enrollment_id: uuid.UUID, scopes: list[tuple[str, str]]
) -> dict[tuple[str, str], tuple[int, float]]:
    """Ledger spend per (model, period) scope — ONE round trip for every cap
    the gate (or the consoles) need.

    A scope's model is either a concrete model name (the SUMs are filtered to
    that model: the pool is that model's alone) or '*' (the SUMs are POOLED
    across models: the any-model preset caps the team's total). The token
    expression, the costed-status set and the fraction weighting are the SAME
    ones team_spend uses — a team cap must never count tokens the per-key gate
    exempts, nor skip ones it charges.

    One query with a FILTER clause per scope, each windowed at its own period
    start, the whole scan bounded by the EARLIEST of those windows. The gate
    pays this INSTEAD of the pooled team_spend on derived-envelope teams, so
    the admission path still spends one aggregate round trip, not several.
    """
    uniq: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for model, period in scopes:
        key = (model, _normalize_period(period))
        if key not in seen:
            seen.add(key)
            uniq.append(key)
    if not uniq:
        return {}
    from .budgets import _COSTED_SQL, _budget_token_value, _window_start

    now = datetime.now(UTC)
    starts = {period: _window_start(now, period) for _m, period in uniq}
    earliest = min(starts.values())
    # The VALUE expr, never the SUM form: this statement brings its own
    # SUM(...) FILTER per scope, and SUM(SUM(...)) is a Postgres error
    # (2026-10-30 live 500 on the Teams + enrollments pages — the fake DB
    # never parses SQL, so only the real database caught it).
    tok = _budget_token_value("l")
    selects: list[str] = []
    args: list[Any] = [enrollment_id]
    for i, (model, period) in enumerate(uniq):
        args.append(starts[period])
        cond = f"l.ts >= ${len(args)}::timestamptz"
        if model != "*":
            args.append(model)
            cond += f" AND l.model = ${len(args)}"
        selects.append(f"COALESCE(SUM({tok}) FILTER (WHERE {cond}), 0)::float AS s{i}_tokens")
        selects.append(f"COALESCE(SUM(l.cost_usd * l.billing_fraction) FILTER (WHERE {cond}), 0)::float AS s{i}_usd")
    args.append(earliest)
    row = await db.fetchrow(
        f"""
        SELECT {", ".join(selects)}
        FROM usage_ledger l
        JOIN api_keys k ON k.id = l.key_id
        WHERE k.enrollment_id = $1
          AND l.status IN ({_COSTED_SQL})
          AND l.ts >= ${len(args)}::timestamptz
        """,
        *args,
    )
    if row is None:
        return {(model, period): (0, 0.0) for model, period in uniq}
    return {
        (model, period): (int(row[f"s{i}_tokens"] or 0), float(row[f"s{i}_usd"] or 0.0))
        for i, (model, period) in enumerate(uniq)
    }


async def team_cap_spend_bulk(
    scopes_by_enrollment: dict[uuid.UUID, list[tuple[str, str]]],
) -> dict[uuid.UUID, dict[tuple[str, str], tuple[int, float]]]:
    """Per-cap ledger spend for MANY enrollments — ONE statement.

    The console LISTING's batch twin of team_cap_spend (audit 2026-11-10
    P2-34): the admin table renders one line per cap for every
    derived-envelope row, and the per-row helper cost one aggregate round trip
    PER ROW. This answers the whole page at once.

    The statement groups the ledger by (enrollment, model) with one FILTER pair
    per DISTINCT period, so:
      * a model scope reads its own (enrollment, model) row's period column;
      * a '*' scope (the any-model pool) sums every model's row for that
        enrollment — exactly what the per-row helper's model-less FILTER sums.

    The token expression, the costed-status set and the fraction weighting are
    the SAME ones team_cap_spend uses (imported, never re-spelled), so the two
    can never tell different stories about one cap. Every requested scope is
    present in the result with (0, 0.0) when the ledger has nothing for it.
    """
    from .budgets import _COSTED_SQL, _budget_token_value, _window_start

    # Normalize/dedupe the period vocabulary FIRST: one FILTER per distinct
    # period, in a stable order so the column names line up with the args.
    periods: list[str] = []
    wanted: dict[uuid.UUID, list[tuple[str, str]]] = {}
    for enrollment_id, scopes in scopes_by_enrollment.items():
        if not scopes:
            continue
        normalized: list[tuple[str, str]] = []
        for model, period in scopes:
            norm = _normalize_period(period)
            if norm not in periods:
                periods.append(norm)
            normalized.append((model, norm))
        wanted[enrollment_id] = normalized
    if not wanted:
        return {}

    now = datetime.now(UTC)
    starts = {period: _window_start(now, period) for period in periods}
    earliest = min(starts.values())
    tok = _budget_token_value("l")
    selects: list[str] = []
    args: list[Any] = [list(wanted)]
    for i, period in enumerate(periods):
        args.append(starts[period])
        idx = len(args)
        selects.append(f"SUM({tok}) FILTER (WHERE l.ts >= ${idx}::timestamptz) AS s{i}_tokens")
        selects.append(f"SUM(l.cost_usd * l.billing_fraction) FILTER (WHERE l.ts >= ${idx}::timestamptz) AS s{i}_usd")
    args.append(earliest)
    rows = await db.fetch(
        f"""
        SELECT k.enrollment_id, l.model, {", ".join(selects)}
        FROM usage_ledger l
        JOIN api_keys k ON k.id = l.key_id
        WHERE k.enrollment_id = ANY($1::uuid[])
          AND l.status IN ({_COSTED_SQL})
          AND l.ts >= ${len(args)}::timestamptz
        GROUP BY k.enrollment_id, l.model
        """,
        *args,
    )
    out: dict[uuid.UUID, dict[tuple[str, str], tuple[int, float]]] = {
        enrollment_id: {scope: (0, 0.0) for scope in scopes} for enrollment_id, scopes in wanted.items()
    }
    for r in rows:
        row_scopes = wanted.get(r["enrollment_id"])
        if row_scopes is None:
            continue
        for i, period in enumerate(periods):
            tokens = int(r[f"s{i}_tokens"] or 0)
            usd = float(r[f"s{i}_usd"] or 0.0)
            for scope in row_scopes:
                model, scope_period = scope
                if scope_period != period:
                    continue
                if model != "*" and str(r["model"]) != model:
                    continue
                prev_t, prev_u = out[r["enrollment_id"]][scope]
                out[r["enrollment_id"]][scope] = (prev_t + tokens, prev_u + usd)
    return out


async def team_scope_available(
    enrollment_id: uuid.UUID,
    model: str,
    period: str,
    cap_tokens: int | None,
    cap_usd: float | None,
) -> tuple[int | None, float | None]:
    """How much of ONE per-model pool is still UNCOMMITTED right now.

    The per-model twin of team_available (same formula, same clamp):

        available = cap - scope_spend - SUM over keys of max(0, alloc_k - spent_k)

    where alloc_k / spent_k are the key's allocation and spend FOR THIS
    (model, period) scope. The committed subtraction is the point: an
    allocation is a promise the pool must keep, so a key that has out-spent
    its own allocation may only overflow into the UNPROMISED remainder —
    never into what is still owed to a teammate who is under budget.

    Model matching mirrors the per-key gate: a key's model='*' budget row
    counts toward every model's allocation (it applies to any model), and
    model='*' scope means the pooled any-model pool (its allocation rows are
    the '*' rows, its spend is every model). Committed is therefore
    deliberately conservative for the '*' pool.

    None for a kind the cap does not bound. Clamps at 0 (over-committed is
    reported as 0, never negative) — the same lossy-by-design contract as
    team_available.

    THE PROMISE SET IS THE LIVE SEATS (audit 2026-11-10 P2-18/P2-22): a
    revoked or expired seat promises nothing — its unspent allocation is
    released at revocation/expiry, not at period rollover — while its SPEND
    still counts toward the pool. That is exactly the split its twin
    team_available makes between team_allocation_state (live keys only) and
    team_spend (every key), so the two pools cannot disagree about a departed
    or lapsed teammate.
    """
    from .budgets import _COSTED_SQL, _budget_token_value, _window_start

    now = datetime.now(UTC)
    window = _normalize_period(period)
    start = _window_start(now, window)
    tok = _budget_token_value("l")
    rows = await db.fetch(
        f"""
        SELECT k.id AS key_id,
               k.revoked_at AS revoked_at,
               k.expires_at AS expires_at,
               COALESCE(b.alloc_tokens, 0)::float AS alloc_tokens,
               COALESCE(c.alloc_usd, 0)::float AS alloc_usd,
               COALESCE(s.spent_tokens, 0)::float AS spent_tokens,
               COALESCE(s.spent_usd, 0)::float AS spent_usd
        FROM api_keys k
        LEFT JOIN (SELECT key_id, SUM(limit_tokens) AS alloc_tokens
                     FROM budgets
                    WHERE period = $2 AND (model = $3 OR model = '*')
                    GROUP BY key_id) b ON b.key_id = k.id
        LEFT JOIN (SELECT key_id, SUM(limit_usd) AS alloc_usd
                     FROM cost_budgets
                    WHERE period = $2 AND (model = $3 OR model = '*')
                    GROUP BY key_id) c ON c.key_id = k.id
        LEFT JOIN (SELECT l.key_id,
                          SUM({tok}) AS spent_tokens,
                          SUM(l.cost_usd * l.billing_fraction) AS spent_usd
                     FROM usage_ledger l
                    WHERE l.status IN ({_COSTED_SQL}) AND l.ts >= $4::timestamptz
                      AND (l.model = $3 OR $3 = '*')
                      -- ENROLLMENT SCOPE (audit W6 F1): the outer WHERE only
                      -- prunes the JOIN, so without this the aggregate GROUPS
                      -- every tenant's ledger rows before the join can discard
                      -- them — on the per-key refusal path, which is hot. The
                      -- subselect idiom is traces.recent's ($1 is already the
                      -- enrollment id; the numbering does not move).
                      AND l.key_id IN (SELECT id FROM api_keys WHERE enrollment_id = $1)
                    GROUP BY l.key_id) s ON s.key_id = k.id
        WHERE k.enrollment_id = $1
          -- NO key-state predicate HERE (audit 2026-11-10 P2-22). This row set
          -- feeds BOTH the promise sum and the scope SPEND, so the old
          -- "AND k.revoked_at IS NULL" also dropped a revoked seat's ledger
          -- rows out of the spend — while its twin team_available counts spend
          -- from EVERY key (team_spend carries no key-state predicate). Every
          -- rotation revokes the old key, so a rotated team systematically
          -- OVERSTATED its per-model headroom. The promise set is applied
          -- below, on these same rows: a REVOKED or EXPIRED seat promises
          -- nothing (owner decision audit W6 P2-7; expiry audit P2-18 — an
          -- expired key is refused at admission, so its unspent allocation can
          -- never be spent yet still counted as committed until rollover).
        """,
        enrollment_id,
        window,
        model,
        start,
    )
    committed_tokens = 0.0
    committed_usd = 0.0
    scope_tokens = 0.0
    scope_usd = 0.0
    for r in rows:
        alloc_t = float(r["alloc_tokens"] or 0.0)
        alloc_u = float(r["alloc_usd"] or 0.0)
        spent_t = float(r["spent_tokens"] or 0.0)
        spent_u = float(r["spent_usd"] or 0.0)
        # SPEND FROM EVERY KEY (audit 2026-11-10 P2-22): a revoked or expired
        # seat's ledger rows are real team spend — team_spend counts them, and
        # dropping them here understated the pool by exactly what every
        # rotation left behind.
        scope_tokens += spent_t
        scope_usd += spent_u
        # THE PROMISE SET (audit P2-18/P2-22): only a LIVE seat holds a promise.
        # .get, not [], so a row/fake predating these columns reads as live
        # (the control default), never a KeyError.
        expires = r.get("expires_at")
        if r.get("revoked_at") is not None or (expires is not None and expires <= now):
            continue
        committed_tokens += max(0.0, alloc_t - spent_t)
        committed_usd += max(0.0, alloc_u - spent_u)
    available_tokens: int | None = None
    if cap_tokens is not None:
        available_tokens = max(0, int(cap_tokens) - int(scope_tokens) - int(committed_tokens))
    available_usd: float | None = None
    if cap_usd is not None:
        available_usd = max(0.0, float(cap_usd) - scope_usd - committed_usd)
    return (available_tokens, available_usd)


async def team_allocated(enrollment_id: uuid.UUID, period: str) -> tuple[int, float]:
    """(tokens, usd) ALLOCATED across the team's keys for one period.

    The sum of the per-key budget rows (budgets.limit_tokens /
    cost_budgets.limit_usd) — what the console reports next to the envelope so
    an operator can see the overflow pool: total - allocated. Rows of OTHER
    periods are ignored (a key may carry a day row and a month row; they are
    different envelopes in time, not additive).

    Advisory only: allocations are what the gate applies per key, and a
    teammate key may legitimately carry no allocation at all.

    The promise set is the LIVE seats (audit 2026-11-10 P2-18): a revoked seat
    is released at revocation, and an EXPIRED seat is released at expiry —
    admission refuses an expired key (core.keys.key_record_from_row), so its
    unspent allocation can never be spent yet still counted as promised until
    the period rolled over. Same predicate team_allocation_state applies.
    """
    row = await db.fetchrow(
        """
        SELECT COALESCE((SELECT SUM(b.limit_tokens)
                           FROM budgets b
                           JOIN api_keys k ON k.id = b.key_id
                          WHERE k.enrollment_id = $1 AND k.revoked_at IS NULL
                            AND (k.expires_at IS NULL OR k.expires_at > now())
                            AND b.period = $2), 0)::float AS tokens,
               COALESCE((SELECT SUM(c.limit_usd)
                           FROM cost_budgets c
                           JOIN api_keys k ON k.id = c.key_id
                          WHERE k.enrollment_id = $1 AND k.revoked_at IS NULL
                            AND (k.expires_at IS NULL OR k.expires_at > now())
                            AND c.period = $3), 0)::float AS usd
        """,
        enrollment_id,
        _normalize_period(period),
        _normalize_period(period),
    )
    if row is None:
        return 0, 0.0
    return int(row["tokens"] or 0), float(row["usd"] or 0.0)


async def team_totals_bulk(
    by_period: dict[str, list[uuid.UUID]],
) -> dict[uuid.UUID, tuple[int, float, int, float]]:
    """Spend AND allocation for MANY enrollments — ONE statement per period.

    The console LISTING's batch twin of team_spend + team_allocated (audit
    2026-11-10 P2-34). The admin enrollment table used to pay BOTH helpers for
    EVERY row — two aggregate round trips, serially, on the console's hottest
    read — plus one more per derived-envelope row for its caps. This answers
    the whole page with one statement per DISTINCT period (normally one: an
    enrollment's window is a deployment-wide choice).

    by_period maps a stored (raw) enrollments.budget_period to the ids to price
    in that window. Periods are normalized through _normalize_period and
    merged, so two spellings of one window share a statement instead of paying
    twice.

    Returns (spent_tokens, spent_usd, allocated_tokens, allocated_usd) per
    enrollment, with the SAME predicates the per-row helpers apply:
      * spend = every key of the enrollment (team_spend carries no key-state
        predicate), costed statuses only, the cache-discounted token value
        expression and the fraction-weighted USD;
      * allocation = the LIVE seats' budget rows for that period (a revoked or
        expired seat promises nothing — audit 2026-11-10 P2-18), exactly
        team_allocated's predicate.
    An enrollment the statement does not answer is absent from the result;
    callers degrade it to zeros (the listing's existing contract).
    """
    from .budgets import _COSTED_SQL, _budget_token_value, _window_start

    groups: dict[str, list[uuid.UUID]] = {}
    for raw_period, ids in by_period.items():
        if ids:
            groups.setdefault(_normalize_period(raw_period), []).extend(ids)
    if not groups:
        return {}
    now = datetime.now(UTC)
    tok = _budget_token_value("l")
    out: dict[uuid.UUID, tuple[int, float, int, float]] = {}
    for period, ids in groups.items():
        rows = await db.fetch(
            f"""
            SELECT e.id AS enrollment_id,
                   COALESCE(sp.spent_tokens, 0)::float AS spent_tokens,
                   COALESCE(sp.spent_usd, 0)::float    AS spent_usd,
                   COALESCE(al.alloc_tokens, 0)::float AS alloc_tokens,
                   COALESCE(au.alloc_usd, 0)::float    AS alloc_usd
            FROM enrollments e
            LEFT JOIN (
                SELECT k.enrollment_id,
                       SUM({tok}) AS spent_tokens,
                       SUM(l.cost_usd * l.billing_fraction) AS spent_usd
                  FROM usage_ledger l
                  JOIN api_keys k ON k.id = l.key_id
                 WHERE k.enrollment_id = ANY($1::uuid[])
                   AND l.status IN ({_COSTED_SQL})
                   AND l.ts >= $2::timestamptz
                 GROUP BY k.enrollment_id
            ) sp ON sp.enrollment_id = e.id
            LEFT JOIN (
                SELECT k.enrollment_id, SUM(b.limit_tokens) AS alloc_tokens
                  FROM budgets b
                  JOIN api_keys k ON k.id = b.key_id
                 WHERE k.enrollment_id = ANY($1::uuid[])
                   AND b.period = $3
                   AND k.revoked_at IS NULL
                   AND (k.expires_at IS NULL OR k.expires_at > now())
                 GROUP BY k.enrollment_id
            ) al ON al.enrollment_id = e.id
            LEFT JOIN (
                SELECT k.enrollment_id, SUM(c.limit_usd) AS alloc_usd
                  FROM cost_budgets c
                  JOIN api_keys k ON k.id = c.key_id
                 WHERE k.enrollment_id = ANY($1::uuid[])
                   AND c.period = $3
                   AND k.revoked_at IS NULL
                   AND (k.expires_at IS NULL OR k.expires_at > now())
                 GROUP BY k.enrollment_id
            ) au ON au.enrollment_id = e.id
            WHERE e.id = ANY($1::uuid[])
            """,
            list(ids),
            _window_start(now, period),
            period,
        )
        for r in rows:
            out[r["enrollment_id"]] = (
                int(r["spent_tokens"] or 0),
                float(r["spent_usd"] or 0.0),
                int(r["alloc_tokens"] or 0),
                float(r["alloc_usd"] or 0.0),
            )
    return out


@dataclass
class KeyAllocation:
    """One key's allocation AND its actual spend for a period.

    The pair is what the team pool needs: an allocation is a PROMISE the pool
    must keep available for that key, but only the part it has not consumed
    yet (committed_*). A key that has already spent past its allocation
    commits nothing further (its over-spend is already inside the team's total
    spend S, and the overflow pool covers it); a key with no allocation
    commits nothing at all.
    """

    key_id: uuid.UUID
    alloc_tokens: int
    alloc_usd: float
    spent_tokens: int
    spent_usd: float

    @property
    def committed_tokens(self) -> int:
        """max(0, alloc - spent) — what this key can still consume under its
        OWN cap, i.e. what the team pool must keep reserved for it."""
        return max(0, self.alloc_tokens - self.spent_tokens)

    @property
    def committed_usd(self) -> float:
        """USD twin of committed_tokens."""
        return max(0.0, self.alloc_usd - self.spent_usd)


async def team_allocation_state(enrollment_id: uuid.UUID, period: str) -> dict[uuid.UUID, KeyAllocation]:
    """Every key of the enrollment with its allocation and spend for a period.

    ONE statement (the console renders a whole team per page, and the envelope
    check reads the same rows). Shape:

      * the key list is api_keys BY enrollment_id, so a key with no budget row
        and no ledger row still appears (allocation 0, spend 0) — an
        unallocated teammate is a row, not an absence;
      * allocations are the SUM of that key's budgets.limit_tokens /
        cost_budgets.limit_usd FOR THIS PERIOD — a key may carry several
        model-specific rows, and they are sub-promises of one allocation, so
        they add up (exactly what the envelope check counts);
      * spend is the SAME aggregate the per-key gate and team_spend use:
        status IN core.budgets._COSTED_SQL (cache_hit rows are free,
        'guardrail_blocked' rows are real spend), ts inside the
        window, tokens via core.budgets._budget_token_expr('l') so the cache
        discount is identical, usd via SUM(cost_usd * billing_fraction);
      * the three aggregates are LEFT JOINed as pre-aggregated derived tables —
        joining the raw tables would multiply rows (budgets x cost_budgets x
        ledger) and inflate every sum.

    Returns {} for a team with no keys. DB errors propagate, the same posture
    as team_spend: a pool computed from a failed read would be a lie, and this
    number guards a WRITE.
    """
    # Lazy import (import cycle — see team_spend).
    from .budgets import _COSTED_SQL, _budget_token_expr, _window_start

    window = _normalize_period(period)
    start = _window_start(datetime.now(UTC), window)
    token_expr = _budget_token_expr("l")
    rows = await db.fetch(
        f"""
        SELECT k.id AS key_id,
               COALESCE(alloc_t.alloc_tokens, 0)::float AS alloc_tokens,
               COALESCE(alloc_u.alloc_usd, 0)::float    AS alloc_usd,
               COALESCE(sp.spent_tokens, 0)::float      AS spent_tokens,
               COALESCE(sp.spent_usd, 0)::float         AS spent_usd
        FROM api_keys k
        LEFT JOIN (
            SELECT b.key_id, SUM(b.limit_tokens) AS alloc_tokens
              FROM budgets b
              JOIN api_keys kb ON kb.id = b.key_id
             WHERE kb.enrollment_id = $1 AND b.period = $2
             GROUP BY b.key_id
        ) alloc_t ON alloc_t.key_id = k.id
        LEFT JOIN (
            SELECT c.key_id, SUM(c.limit_usd) AS alloc_usd
              FROM cost_budgets c
              JOIN api_keys kc ON kc.id = c.key_id
             WHERE kc.enrollment_id = $1 AND c.period = $2
             GROUP BY c.key_id
        ) alloc_u ON alloc_u.key_id = k.id
        LEFT JOIN (
            SELECT l.key_id,
                   -- cache-discounted token SUM, byte-identical to the gate's
                   {token_expr} AS spent_tokens,
                   SUM(l.cost_usd * l.billing_fraction) AS spent_usd
              FROM usage_ledger l
             WHERE l.status IN ({_COSTED_SQL})   -- cache_hit rows are FREE
               AND l.ts >= $3::timestamptz
               AND l.key_id IN (SELECT id FROM api_keys WHERE enrollment_id = $1)
             GROUP BY l.key_id
        ) sp ON sp.key_id = k.id
        WHERE k.enrollment_id = $1
          -- live keys only (owner decision, audit W6 P2-7; expiry audit
          -- 2026-11-10 P2-18): a revoked seat is released at revocation and an
          -- EXPIRED seat at expiry — admission refuses an expired key, so its
          -- unspent allocation could never be spent yet stayed committed until
          -- rollover. See team_scope_available for the same promise set.
          AND k.revoked_at IS NULL
          AND (k.expires_at IS NULL OR k.expires_at > now())
        ORDER BY k.created_at, k.id
        """,
        enrollment_id,
        window,
        start,
    )
    out: dict[uuid.UUID, KeyAllocation] = {}
    for r in rows:
        out[r["key_id"]] = KeyAllocation(
            key_id=r["key_id"],
            alloc_tokens=int(r["alloc_tokens"] or 0),
            alloc_usd=float(r["alloc_usd"] or 0.0),
            spent_tokens=int(r["spent_tokens"] or 0),
            spent_usd=float(r["spent_usd"] or 0.0),
        )
    return out


def _available_usd(value: float) -> float:
    """Clamp a USD pool at 0, and a non-finite one at 0 too.

    A NaN/Infinity envelope (a pre-validation row, or a hand-written UPDATE)
    must never look like headroom: the gate refuses such a row anyway, so the
    console check has to agree and report ZERO — "max(0.0, nan)" would be
    whatever the comparison order happens to produce.
    """
    if not math.isfinite(value):
        return 0.0
    return max(0.0, value)


async def team_available(
    enrollment_id: uuid.UUID,
    period: str,
    *,
    exclude_key_id: uuid.UUID | None = None,
) -> tuple[int | None, float | None]:
    """How much of the team envelope is still GRANTABLE right now.

        available = T - S - SUM over the OTHER keys of max(0, alloc_i - spent_i)

    T is the enrollment cap for the period, S the whole team's spend in the
    window, alloc_i / spent_i key i's allocation and spend. The subtraction of
    the other keys' COMMITTED allocation is the whole point: an allocation is
    a promise the pool must keep, so a seat allocated later gets less. Actual
    spend consumes the pool as well — it is already inside S — and a key that
    has out-spent its allocation reserves nothing more.

    WORKED EXAMPLE (the user's 12 seats x 1M): T = 12M, six seats allocated
    1M each, 9M spent between them, every one of them past its own allocation,
    so SUM(committed) = 0 and available = 12M - 9M - 0 = 3M. Granting three
    more seats 1M each commits 3M, so available = 0 — the remaining pool is
    exactly consumed, and a FOURTH 1M grant is refused (the result clamps at
    0, it never reports a negative pool).

    THE CLAMP IS LOSSY BY DESIGN — never invert this number to recover the
    committed total. When the pool is over-committed, T - S - available is
    SMALLER than the true SUM(committed): at T=12M, S=9M, committed=5M the
    result clamps to 0, and "T - S - available" would report 3M of commitments
    that are really 5M. Read team_allocation_state for the honest per-key sum;
    the second statement is the price of not lying about the pool. (Same for
    the USD side, which clamps independently.)

    None for a kind with no cap: an enrollment that caps only tokens has an
    unlimited USD pool, and the route skips that check (allocations advisory).

    exclude_key_id is the REPLACE-ALL case: the key being re-budgeted has its
    current rows deleted by the write, so its own commitment must not count
    against its new request (the route passes that key's id).

    period is the caller's window, but the pool is always computed for the
    enrollment's OWN budget_period — the cap exists for exactly one window, and
    comparing a day's spend against a monthly cap would understate the pool.
    A mismatch is logged because it means the caller and the cap disagree.

    Implemented on top of team_allocation_state + team_spend so the console and
    the write-time envelope check can never disagree about the numbers.
    """
    row = await db.fetchrow(
        f"SELECT {_ENROLLMENT_COLS} FROM enrollments e LEFT JOIN profiles p ON p.id = e.profile_id WHERE e.id = $1",
        enrollment_id,
    )
    team = team_enrollment_from_row(row)
    if team is None or (team.budget_tokens is None and team.budget_usd is None):
        # No POOLED cap -> there is no envelope to grant from. Deliberately NOT
        # has_budget: a caps-only team (derived per-model pools, no pooled
        # number — audit W6 P2-8) has nothing to grant against either, and
        # answering that here keeps the write path off three aggregates.
        return (None, None)
    window = team.budget_period
    if _normalize_period(period) != window:
        log.warning(
            "team_available: caller asked for %r but enrollment %s budgets by %r — using %r",
            period,
            enrollment_id,
            window,
            window,
        )
    states = await team_allocation_state(enrollment_id, window)
    spent_tokens, spent_usd = await team_spend(enrollment_id, window)
    committed_tokens = sum(st.committed_tokens for kid, st in states.items() if kid != exclude_key_id)
    committed_usd = sum(st.committed_usd for kid, st in states.items() if kid != exclude_key_id)
    # Per kind: None = that kind has no cap (no pool to report). The token
    # pool clamps at 0 for the same reason the USD one does — a grant that
    # would overshoot must be refused, never shown as a negative balance.
    available_tokens: int | None = None
    if team.budget_tokens is not None:
        available_tokens = max(0, int(team.budget_tokens) - int(spent_tokens) - committed_tokens)
    available_usd: float | None = None
    if team.budget_usd is not None:
        available_usd = _available_usd(float(team.budget_usd) - float(spent_usd) - committed_usd)
    return (available_tokens, available_usd)


async def stamp_leader_used(enrollment_id: uuid.UUID, *, min_interval_s: float = 3600.0) -> None:
    """Record that the leader credential was just used — BEST EFFORT.

    WHY: team_leader_key_created_at answers "when was this credential minted",
    never "is it still in use". A stale-credential review (rotate it? revoke
    it?) needs the last SUCCESSFUL auth, and the admin console renders it.

    THROTTLED by min_interval_s (one hour by default): the console polls, and
    an unthrottled UPDATE would write a row per request for a value nobody
    reads at that resolution. The throttle is the UPDATE's own predicate — the
    write lands only when the stored stamp is NULL or older than the interval
    — so concurrent requests cannot produce a write storm either.

    NEVER RAISES (the core.traces.record posture): a telemetry stamp must not
    fail an authentication that already succeeded. Every failure is logged at
    DEBUG and swallowed; the next successful use retries.

    Deliberately NOT called from authenticate_leader: the auth path stays a
    pure read (and its tests free of write side effects). The route calls this
    after a successful auth.
    """
    try:
        await db.execute(
            """
            UPDATE enrollments SET team_leader_key_last_used_at = now()
             WHERE id = $1
               AND (team_leader_key_last_used_at IS NULL
                    OR team_leader_key_last_used_at < now() - $2::interval)
            """,
            enrollment_id,
            timedelta(seconds=float(min_interval_s)),
        )
    except Exception:
        log.debug(
            "leader last-used stamp failed for enrollment %s — ignored",
            enrollment_id,
            exc_info=True,
        )


def _cache_get(key_id: uuid.UUID) -> TeamEnrollment | None | _CacheMiss:
    """Cache read; returns _CACHE_MISS when absent OR expired (never None)."""
    entry = _CACHE.get(key_id)
    if entry is None:
        return _CACHE_MISS
    expires_at, team = entry
    if expires_at <= time.monotonic():
        _CACHE.pop(key_id, None)  # lazy expiry: no sweeper, no background task
        return _CACHE_MISS
    return team


def _cache_put(key_id: uuid.UUID, team: TeamEnrollment | None) -> None:
    if len(_CACHE) >= _CACHE_MAX:
        # Wholesale clear: the bound is about unbounded growth (one entry per
        # key ever seen), not about hit rate — a burst of re-reads is cheaper
        # than an eviction policy nobody would tune.
        _CACHE.clear()
    _CACHE[key_id] = (time.monotonic() + _CACHE_TTL, team)


def invalidate(enrollment_id: uuid.UUID | None = None) -> None:
    """Drop cached enrollment_for_key entries. Call after ANY write that
    changes an enrollment's envelope, its overflow flag, or a key's
    enrollment_id.

    enrollment_id=None clears everything (the blunt instrument: correct for a
    deploy-wide change, and for any write whose affected keys are not known).

    A per-enrollment invalidation drops that team's entries AND every cached
    NEGATIVE (team is None). Negatives carry no enrollment id, so keeping them
    would leave a just-enrolled key reading "no team" for up to the TTL — the
    exact window in which the gate must already enforce the envelope. Dropping
    them costs one extra lookup per affected key; budget writes are a console
    action, not a hot path.
    """
    if enrollment_id is None:
        _CACHE.clear()
        return
    for key_id, (_, team) in list(_CACHE.items()):
        if team is None or team.id == enrollment_id:
            _CACHE.pop(key_id, None)
