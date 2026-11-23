"""Deterministic, model-free agent scoring: what the trace already PROVES.

WHAT: one verdict set per relay turn (request_id), computed from the
`agent_traces` rows the relay already wrote. No model call, no judge, no
payloads, no network. This is the first half of the quality layer — a
calibrated judge is the second — and it is the half that can run on every
turn without spending a token.

THE FIVE VERDICTS (a turn can carry several; `clean` is exclusive):
  not_granted  a tool call was DENIED by the grant/pin gate (status denied)
  tool_error   a tool call did not succeed (status not ok/handed_back/denied)
  loop         >= agent_eval_loop_threshold calls to the same (server, tool)
  step_budget  more than agent_eval_max_steps model rounds in the turn
  clean        nothing above fired — exactly one row, never alongside another

WHY (server, tool) AND NOT ARGUMENTS FOR THE LOOP RULE: agent_traces does not
carry arguments, and the payload table is SAMPLED (trace_payload_sampling) —
a scorer that needed the arguments would answer differently depending on the
sample rate, i.e. it would not be deterministic. (server, tool) is the
strongest signal every trace row always carries.

WHY `handed_back` IS NOT A TOOL ERROR AND NOT LOOP EVIDENCE: a handback is a
CLIENT-executed tool call — the DSH harness loop, the fleet's primary client.
The gateway did not run it, so it cannot call it a failure; and a harness turn
that reads five files is five (client, read) rows, which is normal work, not a
loop. Counting them would mark essentially every DSH turn unclean.

WHY ROUNDS ARE DISTINCT ITERATIONS: a relay turn can start at
iterations_prior > 0 (a continued turn). max(iteration)+1 would charge the
turn for rounds it never ran; the distinct iteration values traced for THIS
request measure the work this turn actually did.

WHAT THIS IS NOT: not a judge, not an LLM call, not a CI gate. `score()` is a
pure function over rows so a test and a script run the same code; `record()`
is the only writer and it is gated on agent_eval_enabled. Wiring a gate that
can fail a build is a deliberate, separate decision.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from ..config import settings
from ..db import db

log = logging.getLogger(__name__)

# The verdict vocabulary, in REPORT ORDER: a turn that both looped and blew its
# budget writes its rows in this order, and `clean` is exclusive and last. The
# DDL pins the same vocabulary (ck_eval_verdicts_vocab).
VERDICTS = ("not_granted", "tool_error", "loop", "step_budget", "clean")
CLEAN = "clean"

# Tool statuses that are NOT a tool fault:
#   ok          - the tool ran and returned a result;
#   handed_back - a CLIENT-executed call (the DSH harness loop); the gateway
#                 handed it back and the client ran it — normal traffic;
#   denied      - its own verdict (not_granted), never double-reported as a
#                 tool fault.
# Everything else (error, rate_limited, budget_exceeded, and any status a
# future build adds) is a failure: an unknown status must be loud, not silent.
_NON_FAILURE_STATUSES = frozenset({"ok", "handed_back", "denied"})

_INSERT_SQL = """
INSERT INTO agent_eval_verdicts (request_id, verdict, reason, detail)
VALUES ($1, $2, $3, $4)
"""

_SELECT_STEPS_SQL = """
SELECT iteration, phase, server, tool, status
FROM agent_traces
WHERE request_id = $1
ORDER BY ts, id
"""


@dataclass(frozen=True)
class Verdict:
    """One scored finding for one turn: the row agent_eval_verdicts stores.

    `detail` is canonical JSON text (sorted keys) so two runs over the same
    rows produce byte-identical evidence — a diff on the verdict table is a
    diff in behaviour, never in dict ordering.
    """

    verdict: str
    reason: str
    detail: str


def _field(row: Any, name: str, default: Any = None) -> Any:
    """Read one column from a trace row.

    Accepts a plain dict, an asyncpg Record (which has .get) or any object
    with the attribute, so the same scorer serves the unit tests, a script and
    the DB path without a conversion step.
    """
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


def _iteration(row: Any) -> int:
    try:
        return int(_field(row, "iteration", 0) or 0)
    except (TypeError, ValueError):  # pragma: no cover - a non-numeric column
        return 0


def _call(row: Any) -> str:
    """The (server, tool) pair as one sortable, groupable label."""
    return f"{_text(row, 'server')}__{_text(row, 'tool')}"


def _detail(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, default=str)


def score(
    rows: Sequence[Any],
    *,
    max_steps: int | None = None,
    loop_threshold: int | None = None,
) -> list[Verdict]:
    """Score ONE relay turn's trace rows. PURE — no DB, no clock, no config write.

    `rows` is any sequence of the per-step rows agent_traces.record() writes
    (dicts or Records; only iteration/phase/server/tool/status are read).
    Thresholds default to settings.agent_eval_max_steps /
    agent_eval_loop_threshold and can be passed explicitly for a dry run.

    Both thresholds follow the fleet-wide knob convention: 0 (or negative)
    DISABLES that rule rather than flagging every turn — a gate must not fail
    everything because an operator zeroed a knob.

    Returns the verdicts in REPORT ORDER (VERDICTS). `clean` is returned only
    when nothing else fired, so "no findings" is a positive statement and not
    an empty list a caller can mistake for "not scored".
    """
    if max_steps is None:
        max_steps = settings.agent_eval_max_steps
    if loop_threshold is None:
        loop_threshold = settings.agent_eval_loop_threshold
    tool_steps = [r for r in rows if _text(r, "phase") == "tool"]
    verdicts: list[Verdict] = []

    # 1) not_granted: the ACL/pin gate refused a tool the model asked for.
    denied = [r for r in tool_steps if _text(r, "status") == "denied"]
    if denied:
        verdicts.append(
            Verdict(
                "not_granted",
                f"{len(denied)} tool call(s) denied by the grant gate",
                _detail(
                    {
                        "count": len(denied),
                        "calls": dict(sorted(Counter(_call(r) for r in denied).items())),
                    }
                ),
            )
        )

    # 2) tool_error: anything that is not a success, a handback or a denial.
    errors = [r for r in tool_steps if _text(r, "status") not in _NON_FAILURE_STATUSES]
    if errors:
        verdicts.append(
            Verdict(
                "tool_error",
                f"{len(errors)} tool call(s) did not succeed",
                _detail(
                    {
                        "count": len(errors),
                        "by_status": dict(sorted(Counter(_text(r, "status") for r in errors).items())),
                        "calls": dict(sorted(Counter(_call(r) for r in errors).items())),
                    }
                ),
            )
        )

    # 3) loop: the model asked for the same tool over and over. Handbacks are
    #    excluded (see the module docstring): a harness turn repeats
    #    (client, read) by construction and the gateway did not execute them.
    observed = [r for r in tool_steps if _text(r, "status") != "handed_back"]
    repeats = Counter(_call(r) for r in observed)
    if loop_threshold > 0 and repeats and max(repeats.values()) >= loop_threshold:
        offenders = {call: n for call, n in sorted(repeats.items()) if n >= loop_threshold}
        verdicts.append(
            Verdict(
                "loop",
                f"{len(offenders)} tool call(s) repeated >= {loop_threshold} times",
                _detail({"threshold": loop_threshold, "calls": offenders}),
            )
        )

    # 4) step_budget: DISTINCT iterations (see the module docstring).
    rounds = len({_iteration(r) for r in rows})
    if max_steps > 0 and rounds > max_steps:
        verdicts.append(
            Verdict(
                "step_budget",
                f"{rounds} model rounds > {max_steps} budget",
                _detail({"rounds": rounds, "max_steps": max_steps}),
            )
        )

    # 5) clean: exclusive — "no findings" must be a positive statement, so a
    #    caller can never confuse it with "this turn was not scored".
    if not verdicts:
        verdicts.append(
            Verdict(
                CLEAN,
                f"{len(rows)} traced step(s), no findings",
                _detail({"steps": len(rows), "tool_calls": len(tool_steps)}),
            )
        )
    return verdicts


async def record(
    request_id: str,
    rows: Sequence[Any],
    *,
    max_steps: int | None = None,
    loop_threshold: int | None = None,
) -> list[Verdict]:
    """Score ONE turn and persist its verdicts when agent_eval_enabled.

    Returns the verdicts either way, so a dry run (flag off) and a script
    still see the scoring — the flag gates the WRITE, not the analysis.
    Writes are BEST-EFFORT (the core.traces.record posture): a verdict-table
    fault is a WARNING and a gap in the quality view, never a failed request.
    """
    verdicts = score(rows, max_steps=max_steps, loop_threshold=loop_threshold)
    if not settings.agent_eval_enabled:
        return verdicts
    for verdict in verdicts:
        try:
            await db.execute(_INSERT_SQL, request_id, verdict.verdict, verdict.reason, verdict.detail)
        except Exception:
            log.warning(
                "agent eval verdict write failed (request_id=%s verdict=%s) — continuing",
                request_id,
                verdict.verdict,
                exc_info=True,
            )
    return verdicts


async def evaluate_turn(request_id: str) -> list[Verdict]:
    """Read one request's trace rows, score them, persist (when enabled).

    The entry point a script or the Lead's gate calls: it owns the read so a
    caller cannot score a different row set than the one stored. The READ is
    allowed to raise on purpose — a gate that reported "clean" because the
    query failed would be worse than a gate that failed loudly. Only the
    verdict WRITES are best-effort (see record()).
    """
    rows = await db.fetch(_SELECT_STEPS_SQL, request_id)
    return await record(request_id, rows)
