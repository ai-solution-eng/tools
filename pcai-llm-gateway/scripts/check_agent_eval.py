#!/usr/bin/env python3
"""Release gate: agent-behaviour scoring must not regress.

WHY THIS EXISTS (2026-11-23). This fleet gated code quality hard and agent quality not at
all: `agent_traces` recorded what an agent did and what it cost, and nothing ever asked
whether the behaviour was right. `core/agent_eval` scores the deterministic half of that
question from the trace rows already written; this gate is what makes the score binding.

WHAT IT ASSERTS. Every case in tests/evaluations/agent_eval_golden.jsonl is a turn whose
verdict set is KNOWN. The file is the baseline: a case may not be relabelled to launder a
bad result into a pass (the same rule the RAG faithfulness gate uses). If the scorer stops
catching a class of failure -- a refactor that narrows `loop`, a status vocabulary drift that
turns `denied` into `ok` -- the matching case fails here, at release time, instead of
silently in production.

KEYED ON PROMPT VERSION (2026-11-23, B4). A gate that does not know WHICH prompt it is
guarding cannot see the change it most needs to see: edit the sweep prompt, ship it, and the
golden cases still pass because the SCORER did not move. So the gate records `PROMPT_VERSION`
from core/agent_presets (the sweep prompt) and core/distiller (the memory prompt) and compares
them against tests/evaluations/agent_eval_baseline.json. On a mismatch it fails with BOTH
versions named. A prompt bump is then a DECISION: re-baseline deliberately with
`--rebaseline`, which rewrites the file, so the bump shows up in the diff and in review.

Exit 0 = prompt versions match AND every case matched. Exit 1 = version drift, a missing
baseline, or at least one case mismatch (printed with the actual verdicts and their reasons,
so the failure is diagnosable without re-running anything).

Usage:  python scripts/check_agent_eval.py [--golden PATH] [--baseline PATH] [--rebaseline] [-v]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from pcai_llm.core import agent_eval, agent_presets, distiller  # noqa: E402

DEFAULT_GOLDEN = ROOT / "tests" / "evaluations" / "agent_eval_golden.jsonl"
DEFAULT_BASELINE = ROOT / "tests" / "evaluations" / "agent_eval_baseline.json"
BASELINE_SCHEMA_VERSION = 1


class BaselineError(Exception):
    """The prompt-version baseline is missing or unreadable."""


def prompt_versions() -> dict[str, str]:
    """The prompt versions this gate guards, keyed by the baseline JSON's names.

    Read from the modules at CALL time (not import time) so a test can move a
    version and watch the gate fail -- the same path a real bump takes.
    """
    return {
        "agent_presets": str(agent_presets.PROMPT_VERSION),
        "distiller": str(distiller.PROMPT_VERSION),
    }


def load_baseline(path: Path) -> dict[str, str]:
    """Read the committed expectation. Missing or malformed is a FAIL, never a pass."""
    if not path.is_file():
        raise BaselineError(f"{path}: prompt-version baseline is missing")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise BaselineError(f"{path}: not valid JSON ({exc})") from exc
    versions = data.get("prompt_versions") if isinstance(data, dict) else None
    if not isinstance(versions, dict) or not versions:
        raise BaselineError(f"{path}: missing a non-empty 'prompt_versions' object")
    return {str(k): str(v) for k, v in versions.items()}


def write_baseline(path: Path) -> dict[str, str]:
    """Rewrite the expectation from the CURRENT prompt versions. A deliberate act."""
    versions = prompt_versions()
    payload = {
        "schema_version": BASELINE_SCHEMA_VERSION,
        "_comment": (
            "Prompt versions this agent-behaviour gate is keyed on. Written by "
            "scripts/check_agent_eval.py --rebaseline; changing it is a decision to "
            "accept a prompt bump, not a formality."
        ),
        "prompt_versions": versions,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return versions


def version_drift(expected: dict[str, str], actual: dict[str, str]) -> list[str]:
    """One line per version that moved, appeared or disappeared. PURE."""
    drift: list[str] = []
    for key in sorted(set(expected) | set(actual)):
        want, got = expected.get(key), actual.get(key)
        if want != got:
            drift.append(f"  {key}: baseline {want or '(absent)'} -> now {got or '(absent)'}")
    return drift


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
        raise SystemExit(f"{path}: no cases found -- the gate would pass vacuously")
    return cases


def _print_drift(drift: list[str]) -> None:
    print("FAIL: prompt version drift -- this gate is keyed on the prompt versions:", file=sys.stderr)
    for line in drift:
        print(line, file=sys.stderr)
    print(
        "\nA prompt bump must be a decision, not a silent pass. If the prompt change is\n"
        "intended and the golden cases still describe the behaviour you want, re-baseline\n"
        "deliberately (the bump then shows up in review):\n"
        "  python scripts/check_agent_eval.py --rebaseline\n"
        "If it is NOT intended, revert the PROMPT_VERSION change instead.",
        file=sys.stderr,
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Agent-behaviour eval gate")
    ap.add_argument("--golden", type=Path, default=DEFAULT_GOLDEN)
    ap.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    ap.add_argument(
        "--rebaseline",
        action="store_true",
        help="rewrite --baseline from the current PROMPT_VERSIONs (the cases still run)",
    )
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    actual = prompt_versions()
    if args.rebaseline:
        write_baseline(args.baseline)
        print(f"re-baselined {args.baseline}: " + ", ".join(f"{k}={v}" for k, v in sorted(actual.items())))

    try:
        expected = load_baseline(args.baseline)
    except BaselineError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        print(
            "\nThe gate cannot prove which prompt it is guarding without it. Create it deliberately:",
            file=sys.stderr,
        )
        print("  python scripts/check_agent_eval.py --rebaseline", file=sys.stderr)
        return 1

    drift = version_drift(expected, actual)
    if drift:
        _print_drift(drift)
        return 1

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
            "(fix the case AND justify it in the commit -- relabelling to go green is the\n"
            "failure mode this gate exists to prevent).",
            file=sys.stderr,
        )
        return 1

    versions = ", ".join(f"{k}={v}" for k, v in sorted(actual.items()))
    print(f"agent-behaviour eval gate: {len(cases)} golden cases matched | prompts {versions} (baseline matched)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
