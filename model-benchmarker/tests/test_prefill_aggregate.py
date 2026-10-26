"""Tests for MB-PREFILL: aggregate prefill throughput from TTFT (D-prefill).

Covers the two additions to the chat benchmark:
  * prompt_tokens capture from the stream usage block (exact prompt length,
    replacing the ~25%-low 4-chars/token padding assumption), and
  * aggregate_prefill_tps = n * S / max(TTFT) per concurrency level.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from model_benchmarker.benchmark_chat import (  # noqa: E402
    LevelRow,
    RequestResult,
    aggregate_prefill_tps,
    format_table_markdown,
    prefill_is_stable,
)


def _res(ttft_s, prompt_tokens=None, *, turn=1, ok=True):
    return RequestResult(
        success=ok,
        task="coding",
        turn=turn,
        ttft_s=ttft_s,
        tokens=100,
        tokens_per_s=50.0,
        prompt_tokens=prompt_tokens,
    )


class TestAggregatePrefill:
    def test_scales_with_concurrency(self):
        """n users each prefilling S tokens: n*S / max(TTFT)."""
        group = [_res(2.0, 1000), _res(4.0, 1000), _res(6.0, 1000), _res(8.0, 1000)]
        assert aggregate_prefill_tps(group) == pytest.approx(4 * 1000 / 8.0)

    def test_uses_server_prompt_tokens_over_nominal(self):
        """The padding target runs ~25% high; the usage block is authoritative."""
        group = [_res(2.846, 24616)]
        assert aggregate_prefill_tps(group, nominal_ctx=32768) == pytest.approx(24616 / 2.846)

    def test_falls_back_to_nominal_ctx_without_usage(self):
        group = [_res(2.846, None)]
        assert aggregate_prefill_tps(group, nominal_ctx=32768) == pytest.approx(32768 / 2.846)

    def test_averages_prompt_sizes_across_users(self):
        group = [_res(2.0, 1000), _res(4.0, 3000)]
        assert aggregate_prefill_tps(group) == pytest.approx(2 * 2000 / 4.0)

    def test_uses_max_ttft_not_mean(self):
        """The LAST first-token marks when all n prefills finished."""
        group = [_res(1.0, 1000), _res(9.0, 1000)]
        assert aggregate_prefill_tps(group) == pytest.approx(2 * 1000 / 9.0)

    def test_ignores_failed_requests(self):
        group = [_res(2.0, 1000), _res(99.0, 1000, ok=False)]
        assert aggregate_prefill_tps(group) == pytest.approx(1000 / 2.0)

    def test_excludes_prefix_reuse_turns(self):
        """Turn 2+ reuses the cached prefix, so they are not prefill measurements."""
        group = [_res(2.0, 1000, turn=1), _res(0.1, 1000, turn=2)]
        assert aggregate_prefill_tps(group) == pytest.approx(1000 / 2.0)

    def test_returns_none_without_successes(self):
        assert aggregate_prefill_tps([_res(1.0, 100, ok=False)]) is None
        assert aggregate_prefill_tps([]) is None

    def test_returns_none_when_no_prompt_length_known(self):
        assert aggregate_prefill_tps([_res(2.0, None)], nominal_ctx=0) is None

    def test_returns_none_for_nonpositive_ttft(self):
        assert aggregate_prefill_tps([_res(0.0, 1000)]) is None

    def test_aggregate_does_not_fall_with_concurrency(self):
        """The point of the metric: more users is more total work, same window shape."""
        one = aggregate_prefill_tps([_res(2.0, 1000)])
        four = aggregate_prefill_tps([_res(2.0, 1000)] * 4)
        assert four > one


class TestPromptTokensCapture:
    def test_request_result_carries_prompt_tokens(self):
        r = _res(1.0, 12345)
        assert r.prompt_tokens == 12345

    def test_prompt_tokens_defaults_to_none(self):
        assert RequestResult(success=True).prompt_tokens is None


class TestPrefillColumn:
    def _row(self, prefill=None):
        return LevelRow(
            ctx=32768,
            users=1,
            task="coding",
            failed=0,
            ttft={"P50": 2845.5, "P95": 2845.5, "P99": 2845.5, "P100": 2845.5},
            tps={"P50": 325.7, "P95": 298.0, "P99": 296.3, "P100": 295.9},
            itl={"P50": 13.7, "P95": 20.8, "P99": 47.7, "P100": 144.5},
            tpot={"P50": 3.1, "P95": 3.4, "P99": 3.4, "P100": 3.4},
            prefill_tps=prefill,
        )

    def test_column_present_when_extended(self):
        md = format_table_markdown([self._row(11516.0)], multiturn=False)
        assert "prefill tok/s (agg)" in md
        assert "11,516" in md

    def test_missing_value_renders_dash(self):
        md = format_table_markdown([self._row(None)], multiturn=False)
        assert md.rstrip().endswith("| - |")

    def test_column_absent_when_not_extended(self):
        md = format_table_markdown([self._row(11516.0)], multiturn=False, extended=False)
        assert "prefill" not in md

    def test_markdown_round_trips_through_the_parser(self):
        """results_to_html must still read a table carrying the new column."""
        rth = pytest.importorskip("model_benchmarker.results_to_html")
        md = format_table_markdown([self._row(11516.0)], multiturn=False)
        _mode, rows, _arrival = rth.parse_chat_table_full(md)
        assert len(rows) == 1
        assert rows[0]["ctx"] == 32768
        assert rows[0]["ttft"] == pytest.approx([2845.5] * 4)


class TestPrefillStability:
    def test_stable_when_spread_is_tight(self):
        group = [_res(2.0, 1000), _res(3.8, 1000)]
        assert prefill_is_stable(group) is True

    def test_unstable_when_one_straggler_dominates(self):
        # A real n=64 level read P100/P50 = 10.4 while its neighbours read 2.0.
        group = [_res(1.0, 1000), _res(1.1, 1000), _res(10.4, 1000)]
        assert prefill_is_stable(group) is False

    def test_single_request_is_stable(self):
        assert prefill_is_stable([_res(5.0, 1000)]) is True

    def test_failures_ignored_in_spread(self):
        group = [_res(1.0, 1000), _res(1.2, 1000), _res(99.0, 1000, ok=False)]
        assert prefill_is_stable(group) is True

    def test_unstable_renders_approximate(self):
        row = LevelRow(
            ctx=32768,
            users=64,
            task="coding",
            failed=0,
            ttft={"P50": 1, "P95": 2, "P99": 3, "P100": 4},
            tps={"P50": 9, "P95": 8, "P99": 7, "P100": 6},
            itl={"P50": 1, "P95": 1, "P99": 1, "P100": 1},
            tpot={"P50": 1, "P95": 1, "P99": 1, "P100": 1},
            prefill_tps=2379.0,
            prefill_stable=False,
        )
        md = format_table_markdown([row], multiturn=False)
        assert "~2,379" in md
