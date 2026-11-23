#!/usr/bin/env python3
"""Release gate: agent-behaviour scoring must not regress.

WHY THIS EXISTS (2026-11-23). This fleet gated code quality hard and agent quality not at
all: `agent_traces` recorded what an agent did and what it cost, and nothing ever asked
whether the behaviour was right. `core/agent_eval` scores the deterministic half of that
question from the trace rows already written; this gate is what makes the score binding.

WHAT IT ASSERTS. Every case in tests/evaluations/agent_eval_golden.jsonl is a turn whose
verdict set is KNOWN. The file is the baseline: a case may not be relabelled to launder a
bad result into a pass (the same rule the RAG faithfulness gate uses). If the scorer stops
catching a class of failure — a refactor that narrows `loop`, a status vocabulary drift that
turns `denied` into `ok` — the matching case fails here, at release time, instead of
silently in production.

Exit 0 = every case matched. Exit 1 = at least one mismatch (printed with the actual
verdicts and their reasons, so the failure is diagnosable without re-running anything).

Usage:  python scripts/check_agent_eval.py [--golden PATH] [-v]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from pcai_llm.core import agent_eval  # noqa: E402

DEFAULT_GOLDEN = ROOT / "tests" / "evaluations" / "agent_eval_golden.jsonl"


def load_cases(path: Path) -> list[dict]:
    """Read the golden set. `#` comment lines and blank lines are skipped."""
    cases: list[dict] = []
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            case = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"{path}:{lineno}: not valid JSON ({exc})") from exc
        for key in ("name", "rows", "expect"):
            if key not in case:
                raise SystemExit(f"{path}:{lineno}: missing required key {key!r}")
        cases.append(case)
    if not cases:
        raise SystemExit(f"{path}: no cases found — the gate would pass vacuously")
    return cases


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Agent-behaviour eval gate")
    ap.add_argument("--golden", type=Path, default=DEFAULT_GOLDEN)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    cases = load_cases(args.golden)
    failures: list[str] = []

    for case in cases:
        verdicts = agent_eval.score(case["rows"])
        got = sorted({v.verdict for v in verdicts})
        want = sorted(set(case["expect"]))

        unknown = [v for v in got if v not in agent_eval.VERDICTS]
        if unknown:
            failures.append(f"{case['name']}: scorer returned unknown verdict(s) {unknown}")

        if got != want:
            detail = "; ".join(f"{v.verdict}: {v.reason}" for v in verdicts) or "(no verdicts)"
            failures.append(
                f"{case['name']}: expected {want}, got {got}\n    rows={len(case['rows'])}  evidence: {detail}"
            )
        elif args.verbose:
            print(f"  ok   {case['name']:38s} -> {', '.join(got)}")

    if failures:
        print(f"FAIL: {len(failures)} of {len(cases)} golden case(s) regressed:", file=sys.stderr)
        for f in failures:
            print("  - " + f, file=sys.stderr)
        print(
            "\nEither the scorer lost a failure class (fix the scorer) or the case is wrong\n"
            "(fix the case AND justify it in the commit — relabelling to go green is the\n"
            "failure mode this gate exists to prevent).",
            file=sys.stderr,
        )
        return 1

    print(f"agent-behaviour eval gate: {len(cases)} golden cases matched")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
