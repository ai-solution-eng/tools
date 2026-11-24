"""Prompt-version keying for the agent-behaviour release gate (2026-11-23, B4).

WHY: `scripts/check_agent_eval.py` ran a fixed golden set and did not know
WHICH prompt it was guarding, so a prompt edit shipped without the gate
noticing -- the scorer code had not moved. The gate is now keyed on
`PROMPT_VERSION` from core/agent_presets (the sweep prompt) and
core/distiller (the memory prompt) and compares them against
tests/evaluations/agent_eval_baseline.json.

WHAT IS PINNED HERE:
  * matching versions pass (exit 0) and the gate output NAMES the versions;
  * a drifted version fails (exit 1) naming BOTH the baseline and the current
    version, and telling the operator to re-baseline deliberately;
  * `--rebaseline` rewrites the file from the LIVE versions and then passes;
  * a missing or malformed baseline FAILS CLOSED (a gate that silently passes
    without its keying file is not a gate);
  * the case path still exits 1 on a genuine regression -- the version check
    ADDS a reason to fail, it does not replace the golden set.

The gate is loaded by path because scripts/ is not a package (the same
pattern tests/test_agent_presets.py uses for scripts/seed_agents.py).
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

from pcai_llm.core import agent_presets, distiller

ROOT = Path(__file__).resolve().parents[1]
GATE_PATH = ROOT / "scripts" / "check_agent_eval.py"
GOLDEN = ROOT / "tests" / "evaluations" / "agent_eval_golden.jsonl"
BASELINE = ROOT / "tests" / "evaluations" / "agent_eval_baseline.json"


def _load_gate() -> ModuleType:
    """Load the gate script by path (no sys.path edit)."""
    spec = importlib.util.spec_from_file_location("check_agent_eval_under_test", GATE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def gate() -> ModuleType:
    return _load_gate()


def _baseline(tmp_path: Path, versions: dict[str, str]) -> Path:
    path = tmp_path / "agent_eval_baseline.json"
    path.write_text(json.dumps({"schema_version": 1, "prompt_versions": versions}), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# matching versions pass, and the output records them
# ---------------------------------------------------------------------------


def test_matching_versions_pass_and_are_printed(
    gate: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    path = _baseline(tmp_path, gate.prompt_versions())
    assert gate.main(["--baseline", str(path)]) == 0
    out = capsys.readouterr().out
    for key, version in gate.prompt_versions().items():
        assert f"{key}={version}" in out
    assert "baseline matched" in out


def test_the_committed_baseline_matches_the_live_prompt_versions(gate: ModuleType) -> None:
    """The repo ships a baseline that passes: this is what automation.sh runs."""
    assert GOLDEN.is_file() and BASELINE.is_file()
    assert gate.load_baseline(BASELINE) == gate.prompt_versions()
    assert gate.main(["--baseline", str(BASELINE)]) == 0


# ---------------------------------------------------------------------------
# drift fails, naming both versions and the remedy
# ---------------------------------------------------------------------------


def test_drifted_baseline_fails_naming_both_versions(
    gate: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    stale = {"agent_presets": "sweep-2020-01-01", "distiller": distiller.PROMPT_VERSION}
    path = _baseline(tmp_path, stale)
    assert gate.main(["--baseline", str(path)]) == 1
    err = capsys.readouterr().err
    assert "prompt version drift" in err
    assert "sweep-2020-01-01" in err  # the baseline version
    assert agent_presets.PROMPT_VERSION in err  # the live version
    assert "--rebaseline" in err  # the deliberate remedy


def test_a_real_prompt_bump_fails_the_gate(
    gate: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """The exact thing this exists for: someone bumps the sweep prompt."""
    path = _baseline(tmp_path, gate.prompt_versions())
    previous = agent_presets.PROMPT_VERSION
    monkeypatch.setattr(agent_presets, "PROMPT_VERSION", "sweep-2099-12-31")
    assert gate.main(["--baseline", str(path)]) == 1
    err = capsys.readouterr().err
    assert "sweep-2099-12-31" in err and previous in err


def test_a_new_or_renamed_version_key_is_drift_not_a_pass(
    gate: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """A renamed module must not silently drop out of the keying."""
    versions = dict(gate.prompt_versions())
    versions["playbook"] = "dsh-2026-11-10"
    path = _baseline(tmp_path, versions)
    assert gate.main(["--baseline", str(path)]) == 1
    err = capsys.readouterr().err
    assert "playbook" in err and "(absent)" in err


# ---------------------------------------------------------------------------
# --rebaseline is the deliberate path
# ---------------------------------------------------------------------------


def test_rebaseline_rewrites_and_then_passes(gate: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    path = _baseline(tmp_path, {"agent_presets": "sweep-2020-01-01", "distiller": "dsh-2020-01-01"})
    assert gate.main(["--baseline", str(path)]) == 1  # stale fails ...
    capsys.readouterr()
    assert gate.main(["--baseline", str(path), "--rebaseline"]) == 0  # ... rewrite passes ...
    written = json.loads(path.read_text(encoding="utf-8"))
    assert written["prompt_versions"] == gate.prompt_versions()
    capsys.readouterr()
    assert gate.main(["--baseline", str(path)]) == 0  # ... and the next plain run passes


def test_rebaseline_after_a_bump_records_the_new_version(
    gate: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bump becomes a reviewable diff line instead of a silent pass."""
    path = _baseline(tmp_path, gate.prompt_versions())
    monkeypatch.setattr(agent_presets, "PROMPT_VERSION", "sweep-2099-12-31")
    assert gate.main(["--baseline", str(path)]) == 1
    assert gate.main(["--baseline", str(path), "--rebaseline"]) == 0
    assert gate.load_baseline(path)["agent_presets"] == "sweep-2099-12-31"


# ---------------------------------------------------------------------------
# fail closed without a usable baseline
# ---------------------------------------------------------------------------


def test_missing_baseline_fails_closed_and_is_not_created(
    gate: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    path = tmp_path / "never_written.json"
    assert gate.main(["--baseline", str(path)]) == 1
    err = capsys.readouterr().err
    assert "missing" in err
    assert "--rebaseline" in err
    assert not path.exists()  # a plain run must never invent the expectation


@pytest.mark.parametrize("body", ["not json", "{}", '{"prompt_versions": {}}', "[]"])
def test_malformed_baseline_fails_closed(
    gate: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture, body: str
) -> None:
    path = tmp_path / "bad.json"
    path.write_text(body, encoding="utf-8")
    assert gate.main(["--baseline", str(path)]) == 1
    assert "FAIL" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# the golden-case gate is unchanged: exit 1 on a real regression
# ---------------------------------------------------------------------------


def test_a_genuine_case_regression_still_exits_1(
    gate: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    path = _baseline(tmp_path, gate.prompt_versions())
    golden = tmp_path / "golden.jsonl"
    case = {
        "name": "relabelled_case",
        "request_id": "x",
        "expect": ["clean"],
        "rows": [{"iteration": 0, "phase": "tool", "server": "prometheus", "tool": "query", "status": "error"}],
    }
    golden.write_text(json.dumps(case) + "\n", encoding="utf-8")
    assert gate.main(["--baseline", str(path), "--golden", str(golden)]) == 1
    err = capsys.readouterr().err
    assert "regressed" in err and "relabelled_case" in err
