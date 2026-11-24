#!/usr/bin/env python3
"""The judge SWEEP: feed recorded turns to the calibrated behaviour judge, off the request path.

WHY THIS EXISTS (2026-11-23, B1 commit 2). `core/judge.py` (commit 1) is the pure half: it turns ONE
turn's evidence into cited `Verdict`s, given an injected judge_fn. Nothing ever RAN it. This script is
the loop that makes the judge real: it SELECTS turns that have never been judged, reads their evidence,
calls the judge, and writes the verdicts to `judge_verdicts` with kind=shadow semantics (the table has no
kind column; "shadow" is the ROLLOUT PHASE, design section 6: the sweep writes verdicts and GATES
NOTHING — no release, no budget, no request is ever refused because of a judge verdict).

WHY A SWEEP AND NOT A REQUEST-PATH HOOK (design section 5): a judge call is a model call. Putting it in
the relay would double the cost of every turn to answer a question nobody asked at request time. So it
runs here, out of band, on turns that are already finished, sampled, bounded by a per-run turn cap and a
daily USD cap.

THE FOUR RULES THAT MAKE A RUN HONEST:

  1. NEVER JUDGE A TURN TWICE. A turn with any `judge_verdicts` row is done (the selection is a
     NOT EXISTS semi-join on the indexed request_id). Re-judging would write duplicate rows for one turn
     and spend the budget twice on the same evidence.
  2. NEVER JUDGE A RUNNING TURN. A turn whose newest `agent_traces` row is younger than the QUIET period
     (10 minutes) may still be writing steps, and a half-written turn is evidence a judge would MISREAD —
     it would report a trajectory that was still in progress as the whole trajectory.
  3. SAMPLE PER REQUEST (`judge_sample_rate`), the same rule the payload store applies: the bucket is a
     hash of the request_id, so it is stable across replicas and restarts and monotone in the rate. A 10%
     sample over a week beats 100% for two days followed by silence when the budget runs out.
  4. BOUND EVERY RUN. `judge_max_turns_per_run` (--limit may only LOWER it) and `judge_daily_usd`. The
     budget is the REAL switch: at 0.0 this script refuses to run at all (design: "the budget is the real
     switch"), and a real run re-reads the judge key's settled spend from the usage ledger as it goes, so
     one run cannot blow through the day's cap. --dry spends nothing and is exempt (see below).

WHAT IT DOES NOT DO: it writes no other table, changes no setting, gates nothing, and never raises — a
sweep fault is a logged line and a non-zero exit, never a traceback out of a cron job. It does NOT call
a model in --dry, and it does not write anything in --dry.

--dry IS THE OPERATOR'S FIRST MOVE: it prints every turn the run WOULD judge, with its step count, its
payload count and the exact prompt size the model would receive, and writes nothing. Run it after setting
`judge_enabled`/`judge_daily_usd` and before the first real run: the prompt size is the cost, and a turn
whose payloads were never captured (trace_payload_sampling is OFF by default) shows up here as a turn the
judge can only ABSTAIN on.

THE MODEL CALL (design section 5): ONE HTTP call to the GATEWAY'S OWN /v1/chat/completions, using
`judge_model` (empty = the gateway's default route alias, `auto`). Going through the gateway means the
judge's spend is real ledger spend an operator can see and cap, with no second routing path, no second
credential store and no new HTTP dependency: the client comes from `routing.router.upstream_client`, the
ONE factory every upstream caller in this repo uses (trust_env pinned False, L-1 audit 2026-10-29). The
adapter lives in its own class so tests never construct it: they inject a fake judge_fn, so no test can
reach the network.

WHERE TO RUN IT: scripts/ is NOT baked into the gateway image, so this cannot be run with kubectl exec
inside the pod. Run it from a checkout that can reach the database (the gateway's own PCAI_LLM_DATABASE_URL
or PCAI_LLM_DB_* configuration) AND the gateway (a port-forward, an in-cluster hostname, whatever your site
uses). It needs:

  * `judge_enabled=true` and `judge_daily_usd > 0` (a real run refuses otherwise);
  * an API key created on THIS gateway for the judge (`--api-key` / PCAI_LLM_JUDGE_API_KEY) — a DEDICATED
    key is the point: the daily cap is measured as that key's settled ledger spend for the day;
  * the gateway's base URL (`--gateway-url` / PCAI_LLM_JUDGE_GATEWAY_URL).

USAGE (from a checkout, or any host that can reach both):

  python scripts/judge_sweep.py --dry                       # what WOULD be judged, and the prompt size
  python scripts/judge_sweep.py --dry --days 1 --limit 5
  python scripts/judge_sweep.py --gateway-url http://127.0.0.1:8080 --api-key "$JUDGE_KEY"
  python scripts/judge_sweep.py --request 4f2c... --gateway-url ... --api-key ...   # ONE named turn

Exit codes: 0 = the run happened (including "nothing eligible"); 2 = refused / not configured (budget 0,
judge disabled, missing --gateway-url/--api-key, database configuration incomplete, bad arguments);
1 = a fault (the database was unreachable or rejected a statement, or the sweep itself faulted).
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import sys
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, NamedTuple

import asyncpg
import httpx2

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from pcai_llm.config import settings  # noqa: E402
from pcai_llm.core import judge  # noqa: E402
from pcai_llm.core.keys import hash_key  # noqa: E402
from pcai_llm.core.model_routing import DEFAULT_ALIAS  # noqa: E402
from pcai_llm.db import Database  # noqa: E402
from pcai_llm.routing.router import upstream_client  # noqa: E402

log = logging.getLogger("judge_sweep")

# Bound on the one connect: an operator/cron run must fail visibly rather than hang behind a wedged
# endpoint (the same reasoning as tool_pin.CONNECT_TIMEOUT_S and tool_approval_admin.CONNECT_TIMEOUT_S).
CONNECT_TIMEOUT_S = 10.0

# THE QUIET PERIOD (design section 5, rule 2). A turn is only eligible once its NEWEST step is older than
# this: a relay turn writes one agent_traces row per model round and per tool call, so a turn inside the
# window may still be growing, and a judge handed half a trajectory would report a trajectory that never
# happened. 10 minutes is comfortably longer than any relay turn this fleet allows (the step budget caps
# rounds, and the upstream timeout caps each round) while short enough that a sweep every 15 minutes sees
# yesterday's turns immediately.
QUIET_MINUTES = 10

# How many recent turns to LOOK AT to fill one run. Sampling is a Python hash of the request_id (the
# payload store's rule), so a 10% sample needs ~10 candidates per judged turn; 20 is the margin, and
# MAX_CANDIDATES bounds one run's scan so a quiet fleet never turns the selection into a table sweep.
CANDIDATE_MULTIPLIER = 20
MAX_CANDIDATES = 5_000

# Default --days window: a week of turns is enough to find work after a weekend, and old enough that a
# stale turn judged now is still readable evidence (payload retention is 7 days by default).
DEFAULT_DAYS = 7

# The sampling salt. DELIBERATELY NOT the payload store's salt: judge_sample_rate is its own knob, and an
# operator raising one must not silently raise the other. The ALGORITHM is the same (sha256 bucket of the
# request_id, monotone in the rate) so the two decisions have the same stability properties.
SAMPLE_SALT = "pcai-llm/judge-sweep/v1"

# The judge is a JSON-emitting verifier, not a writer: a few hundred tokens of answer. The cap keeps a
# runaway model from turning one judge call into a large bill; it is a constant, not a knob, because
# judge_max_tokens would be a second budget the operator did not ask for.
JUDGE_MAX_TOKENS = 1_024
# One judge call is a short non-streaming completion. Long enough for a slow self-hosted model under load,
# short enough that a wedged endpoint does not hold a sweep run open.
JUDGE_TIMEOUT_S = 120.0

# --- the statements this script can issue ----------------------------------
# The turn SELECTION. Newest first, bounded, and two rules live in SQL because they are cheap there: a
# turn is a candidate only when NOTHING has judged it (semi-join on the indexed judge_verdicts(request_id))
# and only inside the --days window. The quiet period and the sampling are applied in Python (eligible()),
# because they are the rules an operator tunes and a test must be able to pin without a database.
_CANDIDATES_SQL = """
SELECT t.request_id,
       MIN(t.ts) AS first_ts,
       MAX(t.ts) AS last_ts,
       count(*)  AS steps
  FROM agent_traces t
 WHERE t.ts >= now() - make_interval(days => $1)
   AND NOT EXISTS (SELECT 1 FROM judge_verdicts v WHERE v.request_id = t.request_id)
 GROUP BY t.request_id
 ORDER BY MAX(t.ts) DESC
 LIMIT $2
"""
# --request: ONE named turn, with no days window (an operator may re-examine a turn from last month).
_ONE_TURN_SQL = """
SELECT t.request_id,
       MIN(t.ts) AS first_ts,
       MAX(t.ts) AS last_ts,
       count(*)  AS steps
  FROM agent_traces t
 WHERE t.request_id = $1
 GROUP BY t.request_id
"""
# Has this turn been judged? (The selection already excludes judged turns; --request must ask directly.)
_JUDGED_SQL = "SELECT count(*) AS rows FROM judge_verdicts WHERE request_id = $1"
# The step skeleton the judge reads (no payload text): what the agent DID.
_STEPS_SQL = """
SELECT iteration, phase, server, tool, ms, status
  FROM agent_traces
 WHERE request_id = $1
 ORDER BY ts, id
"""
# The payloads the judge reads: what the agent SAW. MAY BE EMPTY — payload capture is sampled and OFF by
# default, and the judge must still be able to abstain rather than silently score a turn it cannot see.
_PAYLOADS_SQL = """
SELECT request_id, iteration, server, tool, kind, payload, payload_chars, truncated, redacted
  FROM agent_trace_payloads
 WHERE request_id = $1
 ORDER BY iteration, id
"""
# The deterministic half's findings for the same turn: CONTEXT for the judge, so it does not re-derive
# what four model-free rules already proved (core/agent_eval).
_EVAL_SQL = """
SELECT verdict, reason, detail
  FROM agent_eval_verdicts
 WHERE request_id = $1
 ORDER BY ts, id
"""
# The run's clock, read from the DATABASE so the quiet period and the --days window are measured against
# the same clock the trace rows were written with (a host clock skew would otherwise make a running turn
# look finished, or the whole window empty). Tests inject a datetime into eligible() instead.
_NOW_SQL = "SELECT now() AS current_ts"
# The judge key's identity: the daily cap is measured as THIS key's settled spend. Revoked/expired keys
# are refused up front, because every judge call would fail with a 401 and the sweep would spend a run
# learning nothing.
_KEY_SQL = (
    "SELECT id, revoked_at, (expires_at IS NOT NULL AND expires_at <= now()) AS expired"
    " FROM api_keys WHERE key_hash = $1"
)
# Today's settled spend for the judge key (the ledger is where real spend lives: the gateway settles every
# proxied call, including the judge's). "Today" is the DATABASE's day boundary (date_trunc on now()), the
# same boundary the budget gate uses.
_SPEND_SQL = """
SELECT COALESCE(SUM(cost_usd), 0) AS spent
  FROM usage_ledger
 WHERE key_id = $1
   AND ts >= date_trunc('day', now())
"""


class Candidate(NamedTuple):
    """One turn the selection found: what the sweep needs to decide and to judge.

    A NamedTuple, not a dataclass, for the reason scripts/agent_eval_report.py documents: the repo's way to
    load a script in a test is importlib.spec_from_file_location, and on Python 3.14 a @dataclass module that
    was never registered in sys.modules breaks at class creation (dataclasses resolves the module namespace).
    A NamedTuple has no such dependency, so this script stays loadable by path as well as runnable.
    """

    request_id: str
    first_ts: datetime | None
    last_ts: datetime | None
    steps: int
    judged: bool = False


class CommandError(Exception):
    """A failure the operator must READ: the message and the exit code it implies."""

    def __init__(self, message: str, code: int = 1) -> None:
        super().__init__(message)
        self.message = message
        self.code = code


# ---------------------------------------------------------------------------
# the pure rules: sampling, selection, and the model adapter's request shape
# ---------------------------------------------------------------------------


def sampled(request_id: str, rate: float) -> bool:
    """Is THIS REQUEST judged? Deterministic in (request_id, rate). PURE.

    THE SAME RULE core/trace_payloads.sampled applies to payload capture, with its
    own salt: the bucket is a hash of the request_id, so the decision is stable
    across replicas and restarts and MONOTONE in the rate (a request sampled at 0.1
    is still sampled at 0.5 — raising the knob only ever ADDS turns). Per REQUEST,
    never per row: a judged turn is judged whole.

    rate <= 0 samples nothing (the fleet convention for "off"), rate >= 1 samples
    everything.
    """
    if rate <= 0.0:
        return False
    if rate >= 1.0:
        return True
    digest = hashlib.sha256(f"{SAMPLE_SALT}:{request_id}".encode()).digest()
    bucket = int.from_bytes(digest[:8], "big") / float(1 << 64)
    return bucket < rate


def _too_new(candidate: Candidate, now: datetime, quiet_minutes: int) -> bool:
    """Is the turn still inside the quiet period (possibly still running)? PURE."""
    if candidate.last_ts is None:
        return True  # no timestamp = no proof it finished; never judge it
    return (now - candidate.last_ts) < timedelta(minutes=quiet_minutes)


def ineligible_reason(candidate: Candidate, *, now: datetime, quiet_minutes: int = QUIET_MINUTES) -> str:
    """WHY one named turn is not eligible (the --request path prints this). PURE."""
    if candidate.judged:
        return "it already has judge_verdicts rows (a turn is judged once)"
    if candidate.last_ts is None:
        return "it carries no timestamp, so the sweep cannot prove it finished"
    if _too_new(candidate, now, quiet_minutes):
        age = (now - candidate.last_ts).total_seconds() / 60.0
        return (
            f"its newest step is {age:.1f} min old and the quiet period is {quiet_minutes} min"
            " (the turn may still be running)"
        )
    return "its request_id is empty"


def eligible(
    candidates: Sequence[Candidate],
    *,
    now: datetime,
    days: int,
    sample_rate: float,
    limit: int,
    quiet_minutes: int = QUIET_MINUTES,
) -> list[Candidate]:
    """The turns this run may judge, NEWEST FIRST, at most `limit`. PURE.

    Four rules, in the order an operator would explain them:
      1. not already judged (defence in depth — the SQL excludes these too);
      2. QUIET — the newest step is older than `quiet_minutes`;
      3. inside the --days window;
      4. SAMPLED — judge_sample_rate, per request.

    `now` is a PARAMETER, not a clock read: the rule that decides "too new" must be
    pinnable in a test without freezing time, and production passes the DATABASE's
    clock (see _NOW_SQL) so the quiet period is measured against the clock the trace
    rows were written with.
    """
    window = timedelta(days=max(0, int(days)))
    chosen: list[Candidate] = []
    for candidate in candidates:
        if len(chosen) >= limit:
            break
        if candidate.judged or not candidate.request_id:
            continue
        if candidate.last_ts is None or _too_new(candidate, now, quiet_minutes):
            continue
        if (now - candidate.last_ts) > window:
            continue
        if not sampled(candidate.request_id, sample_rate):
            continue
        chosen.append(candidate)
    return chosen


def request_eligible(candidate: Candidate, *, now: datetime, quiet_minutes: int = QUIET_MINUTES) -> bool:
    """Is ONE explicitly-named turn eligible? PURE.

    --request bypasses SAMPLING (the operator named this turn by id, and a hash of
    the id is not a reason to refuse them) and the --days window (an old turn is
    still a legitimate re-examination). It does NOT bypass the two rules that would
    corrupt the table: an already-judged turn would get duplicate rows, and a
    running turn's evidence is incomplete.
    """
    if not candidate.request_id or candidate.judged:
        return False
    return not _too_new(candidate, now, quiet_minutes)


def candidate_limit(limit: int) -> int:
    """How many recent turns to LOOK AT to fill `limit` after sampling. PURE.

    Sampling is a Python-side hash, so a 10% sample needs ~10 candidates per judged
    turn; 20 is the margin, MAX_CANDIDATES bounds one run's scan, and the +50 floor
    keeps a small --limit from being starved by one unlucky hash.
    """
    return min(MAX_CANDIDATES, max(limit * CANDIDATE_MULTIPLIER, limit + 50))


def chat_url(gateway_url: str) -> str:
    """The gateway's OpenAI-compatible chat-completions URL. PURE.

    Accepts the gateway's BASE url ("http://host:8080") or one that already ends in
    /v1 — an operator reading a route table has both in their shell history. An
    empty URL is a usage error, never a silent request to localhost.
    """
    base = (gateway_url or "").strip().rstrip("/")
    if not base:
        raise CommandError(
            "--gateway-url is empty: give the gateway's base URL, e.g. --gateway-url"
            " http://pcai-llm-gateway:8080 (or set PCAI_LLM_JUDGE_GATEWAY_URL).",
            code=2,
        )
    if base.endswith("/v1"):
        return base + "/chat/completions"
    return base + "/v1/chat/completions"


def judge_request_body(model: str, prompt: str) -> dict[str, Any]:
    """The chat-completions body for ONE judge call. PURE.

    temperature 0 because a judge must be as REPRODUCIBLE as the model allows: the
    calibration harness (commit 3) compares two runs per dimension, and a sampling
    judge would make its own agreement score partly noise. stream is False because
    the sweep wants one JSON answer, not a token stream.
    """
    return {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "max_tokens": JUDGE_MAX_TOKENS,
        "stream": False,
    }


def judge_response_text(status_code: int, body: str) -> str:
    """The assistant content of one chat-completions response, or raise. PURE.

    A non-2xx status, an unreadable body or empty content RAISES on purpose: the
    caller turns a judge_fn fault into "no verdicts, one warning" (the
    core.traces.record posture), so a failed model call is NEVER stored as "judged,
    nothing found" — that distinction is the difference between a gap in the quality
    view and a lie in it.
    """
    if not 200 <= status_code < 300:
        raise RuntimeError(f"the gateway answered {status_code}: {body[:200]}")
    try:
        payload = json.loads(body)
        text = payload["choices"][0]["message"]["content"]
    except (TypeError, ValueError, KeyError, IndexError) as exc:
        raise RuntimeError(f"the gateway returned an unreadable body: {exc}") from None
    if not isinstance(text, str) or not text.strip():
        raise RuntimeError("the gateway returned no content")
    return text


# ---------------------------------------------------------------------------
# row helpers: the tolerant readers the rest of the quality layer uses
# ---------------------------------------------------------------------------


def _field(row: Any, name: str, default: Any = None) -> Any:
    """Read one column from a dict, an asyncpg Record, or a plain object."""
    getter = getattr(row, "get", None)
    if callable(getter):
        try:
            return getter(name, default)
        except TypeError:  # pragma: no cover - a .get() without a default arg
            pass
    return getattr(row, name, default)


def _text(row: Any, name: str) -> str:
    value = _field(row, name, "")
    return "" if value is None else str(value)


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):  # pragma: no cover - a non-numeric COUNT
        return 0


def _candidate(row: Any, *, judged: bool = False) -> Candidate:
    """One selection row as a Candidate (the shape _CANDIDATES_SQL returns)."""
    return Candidate(
        request_id=_text(row, "request_id"),
        first_ts=_field(row, "first_ts"),
        last_ts=_field(row, "last_ts"),
        steps=_int(_field(row, "steps")),
        judged=judged,
    )


def _fmt_ts(value: Any) -> str:
    """A TIMESTAMPTZ column as a readable ISO-8601 string (empty when NULL)."""
    if isinstance(value, datetime):
        try:
            return value.isoformat(sep=" ", timespec="seconds")
        except (ValueError, OverflowError):  # pragma: no cover - a hostile datetime
            return str(value)
    return "" if value is None else str(value)


# ---------------------------------------------------------------------------
# the database: the gateway's OWN configuration, one connection, always closed
# ---------------------------------------------------------------------------


def _connection_kwargs() -> dict[str, Any]:
    """The gateway's OWN connection config (one validation point, never a copy).

    Database._connect_kwargs() is where URL-mode vs discrete-mode is validated, and it is fail-loud with
    the exact missing fields -- the same reason tool_approval_admin.py and agent_eval_report.py call it
    instead of re-reading the environment.
    """
    try:
        return Database._connect_kwargs()
    except RuntimeError as exc:
        raise CommandError(
            f"the database is not configured: {exc}\n"
            "Set PCAI_LLM_DATABASE_URL (the gateway's own URL) or every PCAI_LLM_DB_* variable, then re-run.\n"
            "NOTE: scripts/ is NOT baked into the gateway image, so this cannot be run with kubectl exec inside\n"
            "the pod -- run it from a checkout that can reach the database (or copy it into the pod yourself).",
            code=2,
        ) from exc


async def _open() -> asyncpg.Connection:
    """Connect with the gateway's own configuration; every fault is a readable error."""
    kwargs = _connection_kwargs()
    try:
        return await asyncpg.connect(timeout=CONNECT_TIMEOUT_S, **kwargs)
    except (OSError, asyncpg.PostgresError) as exc:
        raise CommandError(f"cannot connect to the database: {exc}", code=1) from exc


def _missing_quality_layer() -> CommandError:
    """The one schema fault an operator will actually hit on a fresh cluster."""
    return CommandError(
        "the quality-layer tables do not exist (agent_traces / judge_verdicts / agent_trace_payloads):"
        " this database has never booted a gateway that ships them, so nothing has ever been traced."
        " Boot the gateway once (its startup DDL creates them), then re-run.",
        code=1,
    )


async def _candidates(conn: asyncpg.Connection, *, days: int, limit: int) -> list[Candidate]:
    """The recent turns NOTHING has judged, newest first, at most `limit`."""
    try:
        rows = await conn.fetch(_CANDIDATES_SQL, int(days), int(limit))
    except asyncpg.UndefinedTableError as exc:
        raise _missing_quality_layer() from exc
    except asyncpg.PostgresError as exc:
        raise CommandError(f"the database rejected the turn selection: {exc}", code=1) from exc
    return [_candidate(row) for row in rows]


async def _one_turn(conn: asyncpg.Connection, request_id: str) -> Candidate | None:
    """ONE named turn (with its judged state), or None when nothing was traced for it."""
    try:
        rows = await conn.fetch(_ONE_TURN_SQL, request_id)
        judged_row = await conn.fetchrow(_JUDGED_SQL, request_id)
    except asyncpg.UndefinedTableError as exc:
        raise _missing_quality_layer() from exc
    except asyncpg.PostgresError as exc:
        raise CommandError(f"the database rejected the turn lookup: {exc}", code=1) from exc
    if not rows:
        return None
    return _candidate(rows[0], judged=_int(_field(judged_row, "rows")) > 0)


async def _evidence(conn: asyncpg.Connection, request_id: str) -> tuple[list[Any], list[Any], list[Any]]:
    """The three evidence reads for ONE turn: steps, payloads, deterministic verdicts.

    A turn with NO payload rows is NORMAL (payload capture is sampled and off by default): the judge is
    then handed the step skeleton alone and can only ABSTAIN -- the honest answer, and the reason the
    sweep does not skip such a turn (design section 6: the abstention RATE is a calibration metric).
    """
    rows = await conn.fetch(_STEPS_SQL, request_id)
    payloads = await conn.fetch(_PAYLOADS_SQL, request_id)
    eval_verdicts = await conn.fetch(_EVAL_SQL, request_id)
    return list(rows), list(payloads), list(eval_verdicts)


async def _db_now(conn: asyncpg.Connection) -> datetime:
    """The DATABASE's clock (see _NOW_SQL)."""
    try:
        row = await conn.fetchrow(_NOW_SQL)
    except asyncpg.PostgresError as exc:
        raise CommandError(f"the database rejected the clock read: {exc}", code=1) from exc
    value = _field(row, "current_ts")
    if not isinstance(value, datetime):  # pragma: no cover - asyncpg always returns one
        raise CommandError("the database returned no clock value", code=1)
    return value


async def _judge_key_id(conn: asyncpg.Connection, api_key: str) -> Any:
    """The key_id behind the judge's API key, or a CommandError.

    Refusing up front is the point: a revoked/expired/unknown key would make every judge call fail with a
    401 that judge_turn swallows as "no verdicts", so the sweep would burn a run learning nothing.
    """
    try:
        row = await conn.fetchrow(_KEY_SQL, hash_key(api_key))
    except asyncpg.UndefinedTableError as exc:
        raise _missing_quality_layer() from exc
    except asyncpg.PostgresError as exc:
        raise CommandError(f"the database rejected the judge-key lookup: {exc}", code=1) from exc
    if row is None:
        raise CommandError(
            "the --api-key is not a key in this database: the judge must spend under a key THIS gateway knows"
            " (that is also how the daily cap is measured). Create a key for the judge and pass it.",
            code=2,
        )
    if _field(row, "revoked_at") is not None:
        raise CommandError(
            "the judge's API key is REVOKED: every judge call would be refused. Issue a new key.", code=2
        )
    if bool(_field(row, "expired")):
        raise CommandError(
            "the judge's API key has EXPIRED: every judge call would be refused. Issue a new key.", code=2
        )
    return _field(row, "id")


async def _today_spend_usd(conn: asyncpg.Connection, key_id: Any) -> float | None:
    """The judge key's settled USD spend since the database's day boundary. None on a read fault.

    FAIL-OPEN, deliberately and narrowly: this is the sweep's ADVISORY cap. The HARD switches are the
    judge_daily_usd <= 0 refusal (fail-closed, before any call) and the per-run turn bound. A ledger read
    fault must not silently stop the quality sweep for a week -- that is the failure this whole feature
    exists to remove -- so the fault is a WARNING and the run continues.
    """
    try:
        row = await conn.fetchrow(_SPEND_SQL, key_id)
    except asyncpg.PostgresError as exc:
        log.warning("judge sweep: cannot read the ledger for the judge key (%s) — the daily cap is unenforced", exc)
        return None
    try:
        return float(_field(row, "spent") or 0.0)
    except (TypeError, ValueError):  # pragma: no cover - a non-numeric SUM
        return None


# ---------------------------------------------------------------------------
# the production judge_fn: ONE call to the gateway's own chat completions
# ---------------------------------------------------------------------------


class GatewayJudge:
    """The production judge_fn: ONE HTTP call to the gateway's own /v1/chat/completions.

    WHY THE GATEWAY'S OWN ENDPOINT (design section 5): every model call in this fleet goes through the
    gateway -- routing, auth, budgets and the usage ledger all live there. Posting to the gateway means the
    judge's spend is REAL spend an operator can see and cap, with no second routing path, no second
    credential store and no duplicated pricing. `judge_model` empty = the gateway's default route alias
    (core.model_routing.DEFAULT_ALIAS, "auto"), which the gateway resolves for the judge key.

    NO NEW HTTP DEPENDENCY: the client comes from routing.router.upstream_client, the ONE factory every
    upstream caller uses (trust_env pinned False -- L-1 audit 2026-10-29), so ambient proxy env cannot
    reroute a judge call.

    INJECTED SEAM: --dry never constructs this, and tests never construct it at all -- they inject a fake
    judge_fn, so no test can reach the network. The blocking __call__ matches commit-1's
    `judge_fn(prompt) -> raw` contract; the sweep invokes it on a worker thread (asyncio.to_thread), which
    is why a private event loop here is safe.
    """

    def __init__(self, *, gateway_url: str, api_key: str, model: str, timeout_s: float = JUDGE_TIMEOUT_S) -> None:
        self.url = chat_url(gateway_url)
        self._api_key = api_key
        self._model = model
        self._timeout_s = timeout_s

    def __call__(self, prompt: str) -> str:
        """One judge call, synchronously (commit-1's judge_fn contract).

        A blocking call needs its OWN event loop, so it must run on a worker thread
        (asyncio.to_thread). Called from inside a running loop it would raise a bare
        RuntimeError from asyncio.run -- which judge_turn swallows as "no verdicts",
        turning a programming mistake into a sweep that silently judges NOTHING. The
        guard below makes that mistake loud and names the fix.
        """
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass  # no loop in this thread: the documented path
        else:
            raise RuntimeError("GatewayJudge blocks: invoke it through asyncio.to_thread, never from the event loop")
        return asyncio.run(self._apost(prompt))

    async def _apost(self, prompt: str) -> str:
        body = judge_request_body(self._model, prompt)
        headers = {"Authorization": f"Bearer {self._api_key}", "content-type": "application/json"}
        async with upstream_client(timeout=httpx2.Timeout(self._timeout_s)) as client:
            resp = await client.post(self.url, json=body, headers=headers)
        return judge_response_text(resp.status_code, resp.text)


def judge_one_turn(
    rows: Sequence[Any],
    payloads: Sequence[Any],
    eval_verdicts: Sequence[Any],
    judge_fn: Callable[[str], Any],
    *,
    model: str,
    prompt_version: str,
    request_id: str,
) -> list[judge.Verdict]:
    """Judge ONE turn with the deterministic findings as context. NEVER raises.

    WHY THIS MIRRORS judge_turn INSTEAD OF CALLING IT: commit-1's judge_turn signature is PINNED by
    tests/test_judge.py (its parameter list is asserted, so it cannot grow a parameter without editing a
    file outside this commit's write zone), and the brief requires agent_eval_verdicts as context. So the
    sweep drives the documented seam itself -- screen, prompt, injected judge_fn, validate -- through the
    SAME functions judge_turn calls, so the citation rule, the abstention handling and the contamination
    flag are identical; only the prompt gains the findings block. If judge_turn ever gains the parameter,
    this helper collapses to a one-line call.

    A judge_fn fault (an exception, a non-JSON body, an empty content) returns [] and logs ONE line: the
    core.traces.record posture. The turn is then NOT judged (no rows), so the next run retries it -- "the
    model call failed" and "the judge found nothing" must not be the same outcome.
    """
    try:
        screen = judge.screen_payloads(payloads)
        prompt = judge.build_judge_prompt(rows, payloads, request_id=request_id, eval_verdicts=eval_verdicts)
        raw = judge_fn(prompt)
        kept, _dropped = judge.validate_verdicts(
            raw,
            request_id=request_id,
            model=model,
            prompt_version=prompt_version,
            contaminated=screen.contaminated,
            known_refs=judge.known_evidence(rows, payloads, request_id=request_id),
        )
        return kept
    except Exception:
        log.warning("judge turn failed for request_id=%s — no verdicts stored", request_id, exc_info=True)
        return []


# ---------------------------------------------------------------------------
# the run: refusals first (they cost nothing), then select, judge, store
# ---------------------------------------------------------------------------


def _max_turns() -> int:
    """The configured per-run turn ceiling (judge_max_turns_per_run)."""
    try:
        ceiling = int(settings.judge_max_turns_per_run)
    except (TypeError, ValueError):  # pragma: no cover - a non-numeric knob
        ceiling = 0
    if ceiling < 1:
        raise CommandError(
            f"judge_max_turns_per_run={settings.judge_max_turns_per_run!r}: it is the per-run turn bound and a"
            " non-positive value would make every run score nothing. Set it to the number of turns one run may"
            " score (judge_enabled / judge_daily_usd are the knobs that switch the sweep OFF).",
            code=2,
        )
    return ceiling


def effective_limit(limit: int | None) -> int:
    """The per-run turn bound: --limit, CLAMPED to judge_max_turns_per_run.

    The config knob is the ceiling ("turns scored per sweep run") and --limit may only LOWER it: a
    command-line flag must not be able to out-spend the operator's configured bound in one run.
    """
    ceiling = _max_turns()
    if limit is None:
        return ceiling
    return max(1, min(int(limit), ceiling))


def require_operator_switch() -> None:
    """Refuse a REAL run unless the operator has switched the judge on.

    Two knobs, in the order the design states them. THE BUDGET IS THE REAL SWITCH (design section 5):
    judge_enabled alone authorizes no spend, and judge_daily_usd=0.0 means no model call may be made --
    so a run refuses on the budget FIRST. judge_enabled second: the design ships the judge off until it
    has been CALIBRATED (section 6), so a sweep must not quietly start writing shadow verdicts while the
    operator still has the judge switched off.
    """
    if float(settings.judge_daily_usd) <= 0.0:
        raise CommandError(
            "no judge budget: judge_daily_usd=0.0. The budget is the REAL switch -- a judge call is a model"
            " call, and 0.0 means no spend is permitted even when judge_enabled is true. Set judge_daily_usd"
            " to the daily ceiling you accept, then re-run. (--dry spends nothing and writes nothing, and is"
            " exempt.)",
            code=2,
        )
    if not settings.judge_enabled:
        raise CommandError(
            "the judge is OFF: judge_enabled=false. The design ships it off until it has been CALIBRATED"
            " (documentation/design/agent-judge.md section 6): enable it deliberately, then re-run."
            " (--dry spends nothing and writes nothing, and is exempt.)",
            code=2,
        )


def require_transport(args: argparse.Namespace) -> None:
    """Refuse a real run without the two things a model call needs."""
    chat_url(args.gateway_url)  # validates the URL shape (raises CommandError when empty)
    if not args.api_key:
        raise CommandError(
            "--api-key (or PCAI_LLM_JUDGE_API_KEY) is required for a real run: the judge spends real money"
            " under its own key on this gateway, and that key is also how the daily cap is measured."
            " (--dry needs neither.)",
            code=2,
        )


def empty_state_line(*, candidates: int, sample_rate: float, days: int) -> str:
    """The empty-run line, naming the rule that produced it. PURE."""
    if candidates == 0:
        return (
            f"judge_sweep: nothing to judge — every turn in the last {days} day(s) has already been judged"
            " (or nothing was traced in the window)."
        )
    return (
        f"judge_sweep: nothing to judge — all {candidates} recent turn(s) are already judged, still inside the"
        f" {QUIET_MINUTES}-minute quiet period, or outside the {sample_rate:g} sample."
        " Raise judge_sample_rate (or wait for turns to age) and re-run."
    )


def print_knobs() -> None:
    """The knob values that explain a run (read-only). PURE apart from reading settings."""
    print(
        "  knobs: "
        f"judge_enabled={bool(settings.judge_enabled)} "
        f"judge_daily_usd={settings.judge_daily_usd:g} "
        f"judge_max_turns_per_run={settings.judge_max_turns_per_run} "
        f"judge_sample_rate={settings.judge_sample_rate:g} "
        f"judge_model={settings.judge_model or DEFAULT_ALIAS}"
    )


async def _dry_report(conn: asyncpg.Connection, selected: Sequence[Candidate], model: str) -> int:
    """Print what a real run WOULD send: no model call, no write."""
    print(f"judge_sweep: dry run — {len(selected)} turn(s) WOULD be judged (no model call, no writes)")
    total_chars = 0
    for candidate in selected:
        try:
            rows, payloads, eval_verdicts = await _evidence(conn, candidate.request_id)
            prompt = judge.build_judge_prompt(
                rows, payloads, request_id=candidate.request_id, eval_verdicts=eval_verdicts
            )
        except Exception:
            log.warning(
                "judge sweep: cannot read the evidence for request_id=%s — skipped in this dry run",
                candidate.request_id,
                exc_info=True,
            )
            continue
        total_chars += len(prompt)
        print(
            f"  {candidate.request_id}  steps={len(rows)} payloads={len(payloads)}"
            f" findings={len(eval_verdicts)}  prompt={len(prompt):,} chars (~{len(prompt) // 4:,} tokens)"
            f"  last={_fmt_ts(candidate.last_ts) or '(no ts)'}"
        )
    print(f"  model={model} prompt_version={judge.PROMPT_VERSION} total prompt={total_chars:,} chars")
    print_knobs()
    if float(settings.judge_daily_usd) <= 0.0:
        print("  NOTE: judge_daily_usd is 0.0, so a REAL run would refuse: the budget is the real switch.")
    return 0


async def _judge_selected(
    conn: asyncpg.Connection,
    selected: Sequence[Candidate],
    judge_fn: Callable[[str], Any],
    model: str,
    *,
    key_id: Any,
) -> int:
    """Judge every selected turn, store its verdicts, stop when the day's cap is reached.

    The cap is re-read after EVERY turn rather than only at the start: a sweep that judged 25 turns after
    the budget was already spent would be the exact failure the cap exists to prevent. The read is one
    indexed SUM per turn (the ledger's (key_id, ts) index), which is nothing next to a model call.
    """
    judged = written = 0
    budget = float(settings.judge_daily_usd)
    for candidate in selected:
        request_id = candidate.request_id
        try:
            rows, payloads, eval_verdicts = await _evidence(conn, request_id)
        except Exception:
            log.warning(
                "judge sweep: cannot read the evidence for request_id=%s — skipping this turn",
                request_id,
                exc_info=True,
            )
            continue
        verdicts = await asyncio.to_thread(
            judge_one_turn,
            rows,
            payloads,
            eval_verdicts,
            judge_fn,
            model=model,
            prompt_version=judge.PROMPT_VERSION,
            request_id=request_id,
        )
        judged += 1
        written += await judge.store_verdicts(
            verdicts,
            execute=conn.execute,
            fallback_refs=judge.known_evidence(rows, payloads, request_id=request_id),
        )
        spent = await _today_spend_usd(conn, key_id)
        if spent is not None and spent >= budget:
            print(f"judge_sweep: the daily judge budget is spent (${spent:.2f} of ${budget:.2f}) — stopping this run")
            break
    print(
        f"judge_sweep: judged {judged} turn(s), wrote {written} verdict row(s) into judge_verdicts"
        f" (model={model}, prompt_version={judge.PROMPT_VERSION})"
    )
    return 0


async def _run(args: argparse.Namespace) -> int:
    """Select, judge and store. Every refusal happens BEFORE the first model call."""
    limit = effective_limit(args.limit)
    model = str(settings.judge_model or DEFAULT_ALIAS)
    if not args.dry:
        require_operator_switch()
        require_transport(args)
    conn = await _open()
    try:
        key_id: Any = None
        if not args.dry:
            key_id = await _judge_key_id(conn, args.api_key)
            budget = float(settings.judge_daily_usd)
            spent = await _today_spend_usd(conn, key_id)
            if spent is None:
                log.warning("judge sweep: the ledger read failed — the daily cap is UNENFORCED for this run")
            elif spent >= budget:
                raise CommandError(
                    f"the judge's daily budget is spent: ${spent:.2f} of judge_daily_usd=${budget:.2f} already"
                    " settled today for the judge key. The budget is the real switch — raise it deliberately, or"
                    " wait for the next day boundary.",
                    code=2,
                )
            else:
                print(
                    f"judge_sweep: judge budget ${budget:.2f}/day, ${spent:.2f} settled today —"
                    f" ${budget - spent:.2f} left"
                )
        now = await _db_now(conn)
        if args.request:
            candidate = await _one_turn(conn, args.request)
            if candidate is None:
                print(f"judge_sweep: no traced turn {args.request} — nothing to judge.")
                return 0
            if not request_eligible(candidate, now=now):
                print(f"judge_sweep: {args.request} is not eligible: {ineligible_reason(candidate, now=now)}")
                return 0
            selected = [candidate]
        else:
            sample_rate = float(settings.judge_sample_rate)
            candidates = await _candidates(conn, days=args.days, limit=candidate_limit(limit))
            selected = eligible(candidates, now=now, days=args.days, sample_rate=sample_rate, limit=limit)
            if not selected:
                print(empty_state_line(candidates=len(candidates), sample_rate=sample_rate, days=args.days))
                return 0
        if args.dry:
            return await _dry_report(conn, selected, model)
        judge_fn = GatewayJudge(gateway_url=args.gateway_url, api_key=args.api_key, model=model)
        return await _judge_selected(conn, selected, judge_fn, model, key_id=key_id)
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# the CLI
# ---------------------------------------------------------------------------


def _positive_int(value: str) -> int:
    """argparse type: an integer >= 1 (a zero-turn or zero-day ask is a mistake)."""
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected an integer, got {value!r}") from exc
    if number < 1:
        raise argparse.ArgumentTypeError(f"must be >= 1, got {number}")
    return number


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    """The CLI surface: the dry run, the bounds, one named turn, and the transport."""
    ap = argparse.ArgumentParser(
        prog="judge_sweep.py",
        description=(
            "Judge recorded agent turns off the request path: sampled, bounded, budget-gated."
            " Writes judge_verdicts only; gates nothing."
        ),
        epilog=(
            "scripts/ is not in the gateway image: run this from a checkout that can reach both the database"
            " and the gateway. Start with --dry (no model call, no writes). A real run needs"
            " judge_enabled=true, judge_daily_usd>0, --gateway-url and --api-key."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "--dry",
        action="store_true",
        help="print the turns that WOULD be judged and each prompt size; no model call, no writes",
    )
    ap.add_argument(
        "--limit",
        type=_positive_int,
        default=None,
        metavar="N",
        help="turns to judge this run (default and CEILING: judge_max_turns_per_run)",
    )
    ap.add_argument(
        "--days",
        type=_positive_int,
        default=DEFAULT_DAYS,
        metavar="N",
        help=f"only consider turns from the last N days (default {DEFAULT_DAYS})",
    )
    ap.add_argument(
        "--request",
        default="",
        metavar="ID",
        help="judge ONE named request id (bypasses sampling and --days; keeps the quiet period)",
    )
    ap.add_argument(
        "--gateway-url",
        default=os.environ.get("PCAI_LLM_JUDGE_GATEWAY_URL", ""),
        metavar="URL",
        help="the gateway base URL the judge calls (env PCAI_LLM_JUDGE_GATEWAY_URL)",
    )
    ap.add_argument(
        "--api-key",
        default=os.environ.get("PCAI_LLM_JUDGE_API_KEY", ""),
        metavar="KEY",
        help="the judge key on THIS gateway, also the daily-cap unit (env PCAI_LLM_JUDGE_API_KEY)",
    )
    ap.add_argument("-v", "--verbose", action="store_true", help="log at INFO (warnings are always shown)")
    return ap.parse_args(argv)


def _setup_logging(verbose: bool) -> None:
    """Warnings are always visible; -v adds the INFO trail."""
    logging.basicConfig(
        level=logging.INFO if verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )


def main(argv: list[str] | None = None) -> int:
    """Entry point: parse, run, report a fault as ONE line and an exit code. NEVER raises."""
    args = _parse_args(argv)
    _setup_logging(args.verbose)
    try:
        return asyncio.run(_run(args))
    except CommandError as exc:
        print(f"judge_sweep: {exc.message}", file=sys.stderr)
        return exc.code
    except Exception as exc:
        # The sweep's contract: a fault is a logged line, never a traceback out of a
        # cron job. The exception TYPE is printed because that is what an operator
        # greps for; the traceback goes to the log for whoever needs it.
        log.warning("judge sweep faulted", exc_info=True)
        print(f"judge_sweep: the sweep faulted ({type(exc).__name__}: {exc}) — nothing was raised", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
