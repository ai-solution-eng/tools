"""Tests for prefill_infer: SGLang log -> aggregate prefill throughput."""

from __future__ import annotations

import pytest

from model_benchmarker.prefill_infer import (
    MAX_BURST_GAP_SECONDS,
    find_bursts,
    format_like_log,
    infer_prefill,
    parse_prefill_line,
    parse_prefill_lines,
    split_runs,
)


def _line(ts, new_token, cached, pending, tps, tflops=500.0, pod="pod-a"):
    return (
        f"{pod}/kserve-container: 2026-10-22T00:00:00 [2026-10-22 {ts} TP0 EP0] "
        f"Prefill batch, #new-seq: 1, #new-token: {new_token}, #cached-token: {cached}, "
        f"full token usage: 0.01, swa token usage: 0.00, #running-req: 0, #queue-req: 0, "
        f"#pending-token: {pending}, cuda graph: False, "
        f"input throughput (token/s): {tps}, est. prefill TFLOPS/s (per GPU): {tflops}"
    )


def _burst(lines, threshold=1000):
    return find_bursts(split_runs(parse_prefill_lines(lines)), threshold=threshold)[0]


class TestParseLine:
    def test_parses_all_fields(self):
        s = parse_prefill_line(_line("13:49:23", 16384, 0, 321161, 4453.26, 654.1))
        assert s is not None
        assert s.pod == "pod-a"
        assert s.timestamp == "2026-10-22 13:49:23"
        assert s.new_token == 16384
        assert s.cached_token == 0
        assert s.pending_token == 321161
        assert s.input_tps == pytest.approx(4453.26)
        assert s.tflops_per_gpu == pytest.approx(654.1)

    def test_ignores_non_prefill_lines(self):
        assert parse_prefill_line("[2026-10-22 13:49:23] Decode batch, #running-req: 3") is None
        assert parse_prefill_line("INFO: 127.0.0.1 - POST /v1/chat/completions 200 OK") is None

    def test_missing_required_field_returns_none(self):
        assert parse_prefill_line("[2026-10-22 13:49:23] Prefill batch, #new-token: 100") is None

    def test_step_seconds_is_new_token_over_rate(self):
        s = parse_prefill_line(_line("13:49:23", 16384, 0, 0, 4096.0))
        assert s.step_seconds == pytest.approx(4.0)

    def test_attended_tokens_includes_cached(self):
        s = parse_prefill_line(_line("13:49:23", 755, 131328, 0, 225.87))
        assert s.attended_tokens == 755 + 131328


class TestPlausibility:
    def test_impossible_tflops_rejected(self):
        s = parse_prefill_line(_line("13:51:32", 9865, 0, 0, 66294.21, 37927.6))
        assert s.plausible_tflops is False

    def test_sub_timer_step_rejected(self):
        s = parse_prefill_line(_line("12:37:33", 6400, 0, 0, 457705.83, 0.5))
        assert s.step_seconds < 0.1
        assert s.plausible_tflops is False

    def test_realistic_step_accepted(self):
        s = parse_prefill_line(_line("13:49:23", 16384, 0, 0, 4453.26, 654.1))
        assert s.plausible_tflops is True


class TestSplitRuns:
    def test_chunks_of_one_drain_are_merged(self):
        lines = [
            _line("13:49:19", 16384, 64768, 337545, 25.95),
            _line("13:49:23", 16384, 0, 321161, 4453.26),
            _line("13:49:27", 16384, 0, 304777, 4157.97),
        ]
        runs = split_runs(parse_prefill_lines(lines))
        assert len(runs) == 1
        assert len(runs[0]) == 3

    def test_break_in_invariant_starts_new_run(self):
        lines = [
            _line("13:49:19", 16384, 0, 49152, 4000.0),
            _line("13:49:23", 16384, 0, 32768, 4000.0),
            _line("13:49:30", 16384, 0, 65536, 4000.0),
        ]
        runs = split_runs(parse_prefill_lines(lines))
        assert len(runs) == 2
        assert len(runs[0]) == 2

    def test_empty_input(self):
        assert split_runs([]) == []


class TestFindBursts:
    def test_backlog_above_threshold_is_a_burst(self):
        lines = [_line("13:49:19", 16384, 0, 300000, 4000.0)]
        assert len(find_bursts(split_runs(parse_prefill_lines(lines)))) == 1

    def test_small_backlog_is_not_a_burst(self):
        lines = [_line("13:49:19", 16384, 0, 500, 4000.0)]
        assert find_bursts(split_runs(parse_prefill_lines(lines))) == []

    def test_idle_gap_splits_bursts(self):
        lines = [
            _line("13:49:19", 16384, 0, 300000, 4000.0),
            _line("13:57:59", 16384, 0, 300000, 4000.0),
        ]
        assert len(find_bursts(split_runs(parse_prefill_lines(lines)))) == 2


class TestBurstMetrics:
    def test_tokens_sum_new_token(self):
        lines = [
            _line("13:49:19", 16384, 0, 32768, 4096.0),
            _line("13:49:23", 16384, 0, 16384, 4096.0),
            _line("13:49:27", 16384, 0, 0, 4096.0),
        ]
        assert _burst(lines).tokens == 3 * 16384

    def test_aggregate_uses_wall_span(self):
        lines = [
            _line("13:49:19", 16384, 0, 32768, 4096.0),
            _line("13:49:23", 16384, 0, 16384, 4096.0),
            _line("13:49:27", 16384, 0, 0, 4096.0),
        ]
        b = _burst(lines)
        assert b.wall_seconds == pytest.approx(8.0)
        assert b.aggregate_tps == pytest.approx(3 * 16384 / 8.0)

    def test_steady_excludes_first_chunk(self):
        lines = [
            _line("13:49:19", 16384, 0, 32768, 1024.0),
            _line("13:49:35", 16384, 0, 16384, 4096.0),
            _line("13:49:39", 16384, 0, 0, 4096.0),
        ]
        b = _burst(lines)
        assert b.steady_tps > b.aggregate_tps

    def test_chunk_size_is_modal_new_token(self):
        lines = [
            _line("13:49:19", 16384, 0, 65536, 4096.0),
            _line("13:49:23", 16384, 0, 49152, 4096.0),
            _line("13:49:27", 9865, 0, 0, 4096.0),
        ]
        assert _burst(lines).chunk_size == 16384

    def test_equivalent_requests_is_scale_not_a_count(self):
        lines = [
            _line("13:49:19", 16384, 0, 32768, 4096.0),
            _line("13:49:23", 16384, 0, 16384, 4096.0),
            _line("13:49:27", 16384, 0, 0, 4096.0),
        ]
        assert _burst(lines).equivalent_32k_requests == pytest.approx(49152 / 32768)


class TestFormatLikeLog:
    def test_round_trips_through_the_parser(self):
        lines = [
            _line("13:49:19", 16384, 0, 16384, 4096.0),
            _line("13:49:23", 16384, 0, 0, 4096.0),
        ]
        b = _burst(lines)
        out = format_like_log(b)
        assert len(out) == len(b.steps)
        reparsed = parse_prefill_line("pod-a/kserve-container: " + out[0])
        assert reparsed is not None
        assert reparsed.new_token == 16384
        assert reparsed.input_tps == pytest.approx(4096.0)

    def test_last_line_reaches_zero_pending(self):
        lines = [
            _line("13:49:19", 16384, 0, 16384, 4096.0),
            _line("13:49:23", 16384, 0, 0, 4096.0),
        ]
        out = format_like_log(_burst(lines))
        assert "#pending-token: 0" in out[-1]


class TestInferPrefill:
    def test_reports_covered_span(self):
        lines = [
            _line("13:49:19", 16384, 0, 300000, 4096.0),
            _line("13:49:23", 16384, 0, 283616, 4096.0),
            _line("13:49:27", 16384, 0, 0, 4096.0),
        ]
        out = infer_prefill(lines)
        assert out["covered"]["first"] == "2026-10-22 13:49:19"
        assert out["covered"]["last"] == "2026-10-22 13:49:27"
        assert out["pods"][0]["bursts"][0]["tokens"] > 0

    def test_never_claims_a_request_count(self):
        lines = [
            _line("13:49:19", 16384, 0, 300000, 4096.0),
            _line("13:49:23", 16384, 0, 0, 4096.0),
        ]
        burst = infer_prefill(lines)["pods"][0]["bursts"][0]
        assert "n_requests" not in burst
        assert "equivalent_32k_requests" in burst

    def test_empty_input_is_safe(self):
        out = infer_prefill([])
        assert out["covered"] == {"first": None, "last": None}
        assert out["pods"] == []

    def test_constant_is_exported(self):
        assert MAX_BURST_GAP_SECONDS > 0
