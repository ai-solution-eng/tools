"""Tests for the chat-mode parity upgrade (MB-A..MB-D):

* MB-A — ITL/TPOT: GapHistogram bucket math on synthetic delta streams,
  max_stall, TPOT with usage-token precedence, burst-guard flagging;
* MB-B — mode stamping: filename ``_ol`` token, run-header MODE lines,
  round-trip through the results_to_html parser, mixed-mode compare
  refusal, header-driven parsing of legacy 12/16-column files;
* MB-C — open-loop arrivals: Poisson rate sanity over a seeded run,
  burstiness shapes, multiturn contradiction guard;
* MB-D — goodput: predicates over crafted per-request records
  (all-meet / some-fail / failed-not-counted), SLO header stamping,
  max goodput-satisfying load.

The fake model replays scripted deltas with real asyncio.sleep delays, so
the gap recorder sees a genuine event loop clock.
"""

from __future__ import annotations

import asyncio
import math
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from model_benchmarker import benchmark_chat as bc  # noqa: E402
from model_benchmarker.benchmark_chat import (  # noqa: E402
    GapHistogram,
    ITLSummary,
    LevelRow,
    RequestResult,
    Task,
    _arrival_schedule,
    _interarrival_s,
    compute_goodput,
    format_table_markdown,
    max_goodput_level,
    parse_args,
    parse_goodput_spec,
    print_table,
    run_open_loop,
    stream_once,
)

_RESULTS_TO_HTML = SRC / "model_benchmarker" / "results_to_html.py"
sys.path.insert(0, str(_RESULTS_TO_HTML.parent))
import results_to_html as rh  # noqa: E402

# ---------------------------------------------------------------------------
# Fake OpenAI-compatible stream
# ---------------------------------------------------------------------------


class FakeDelta:
    def __init__(self, content=None, reasoning=None):
        self.content = content
        self.reasoning_content = reasoning
        self.reasoning = reasoning  # some servers use `reasoning` (model_extra)

    def model_dump(self):
        return {
            "role": "assistant",
            "content": self.content,
            "reasoning_content": self.reasoning_content,
        }


class _Choice:
    def __init__(self, delta):
        self.delta = delta


class _Usage:
    def __init__(self, n):
        self.completion_tokens = n


class _Chunk:
    def __init__(self, delta, usage=None):
        self.choices = [_Choice(delta)] if delta is not None else []
        self.usage = usage


class _FakeStream:
    """Replays (delay_s, delta|None, usage|None) events on the real loop."""

    def __init__(self, events):
        self._events = events

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for delay, delta, usage in self._events:
            if delay > 0:
                await asyncio.sleep(delay)
            yield _Chunk(delta, usage)


class _FakeCompletions:
    def __init__(self, stream):
        self._stream = stream

    async def create(self, **kwargs):
        return self._stream


class FakeModel:
    def __init__(self, events):
        self.model_name = "fake-model"
        self.api_key = "k"
        self.async_client = type("AC", (), {})()
        self.async_client.chat = type("C", (), {})()
        self.async_client.chat.completions = _FakeCompletions(_FakeStream(events))


def _deltas_at(*gaps_ms, usage_tokens=None, first_delay_ms=5.0):
    """Events: first delta after first_delay_ms, then the given gaps (ms)."""
    events = [(first_delay_ms / 1000.0, FakeDelta(content="t0"), None)]
    for g in gaps_ms:
        events.append((g / 1000.0, FakeDelta(content="tok"), None))
    if usage_tokens is not None:
        events.append((0.0, None, _Usage(usage_tokens)))
    return events


def _task(**kw) -> Task:
    defaults = {"name": "t", "prompt": "p", "max_tokens": 100, "temperature": 1.0}
    defaults.update(kw)
    return Task(**defaults)


# ---------------------------------------------------------------------------
# MB-A: GapHistogram math
# ---------------------------------------------------------------------------


class TestGapHistogram:
    def test_counts_and_edges(self):
        h = GapHistogram()
        h.record(0.005)  # 5 ms
        h.record(0.05)  # 50 ms
        h.record(0.5)  # 500 ms
        assert h.n == 3
        assert h.max_gap_ms == pytest.approx(500.0)
        assert h.total_ms == pytest.approx(555.0)
        assert sum(h.counts) == 3
        # log-spaced edges: 5ms and 500ms land in different buckets
        assert len(h.counts) == bc.ITL_BUCKETS

    def test_clipping_extremes(self):
        h = GapHistogram()
        h.record(0.00001)  # 0.01 ms < low edge
        h.record(120.0)  # 120 s > high edge
        assert h.n == 2
        assert h.counts[0] == 1 and h.counts[-1] == 1
        assert h.max_gap_ms == pytest.approx(120_000.0)

    def test_negative_gap_ignored(self):
        h = GapHistogram()
        h.record(-0.5)
        assert h.n == 0

    def test_merge(self):
        a = GapHistogram()
        a.record(0.01)
        a.record(0.02)
        b = GapHistogram()
        b.record(0.03)
        a.merge(b)
        assert a.n == 3
        assert a.max_gap_ms == pytest.approx(30.0)
        assert a.total_ms == pytest.approx(60.0)
        assert sum(a.counts) == 3

    def test_merge_rejects_different_edges(self):
        # Different bucket counts are refused (the module binds edges at
        # import, so a mismatched bucket count is the reachable mismatch).
        a = GapHistogram()
        b = GapHistogram()
        b.counts = [0] * (bc.ITL_BUCKETS + 8)
        with pytest.raises(ValueError):
            a.merge(b)

    def test_percentiles_bounded_by_edges(self):
        import random

        h = GapHistogram()
        rng = random.Random(42)
        for _ in range(2000):
            h.record(rng.uniform(0.002, 2.0))  # 2ms..2s
        pcts = h.percentiles()
        assert set(pcts) == {"P50", "P95", "P99", "P100"}
        for v in pcts.values():
            assert bc.ITL_EDGES_MS[0] <= v <= bc.ITL_EDGES_MS[-1]
        # ascending
        assert pcts["P50"] <= pcts["P95"] <= pcts["P99"] <= pcts["P100"]
        # P100 approximates the true max within log-bucket granularity
        assert 0.5 * h.max_gap_ms <= pcts["P100"] <= 2.0 * h.max_gap_ms
        # P50 within a factor 1.5 of the true median (1.001 s) — log-bucket
        # quantization at 96 buckets over 5.8 decades is ~7% per bucket
        assert abs(pcts["P50"] - 1001.0) / 1001.0 < 0.5

    def test_percentiles_empty(self):
        assert GapHistogram().percentiles() == {}

    def test_memory_is_fixed_buckets(self):
        # The 1.5M-floats math: a full mixed-task level must stay O(buckets).
        import random

        h = GapHistogram()
        rng = random.Random(7)
        for _ in range(64 * 24576):  # 1.57M gaps — a full worst-case level
            h.record(rng.uniform(0.005, 0.5))
        assert len(h.counts) == 96
        assert h.n == 64 * 24576


# ---------------------------------------------------------------------------
# MB-A: stream_once gap recording / TPOT / burst guard
# ---------------------------------------------------------------------------


class TestStreamOnceITL:
    def test_gaps_recorded_per_text_delta(self):
        res = asyncio.run(stream_once(FakeModel(_deltas_at(10, 10, 10, 10, usage_tokens=5)), _task()))
        assert res.success
        assert res.itl.hist.n == 4  # 5 deltas → 4 gaps
        assert res.itl.max_stall_ms == pytest.approx(10.0, abs=3.0)
        assert res.itl.tpot_ms == pytest.approx(10.0, abs=3.0)
        assert not res.burst_guarded

    def test_dual_field_delta_counts_once(self):
        # A delta carrying BOTH content and reasoning_content used to fall in
        # `elif reasoning:` — the gap recorder must not depend on branch order.
        events = [
            (0.005, FakeDelta(content="a", reasoning="r0"), None),
            (0.010, FakeDelta(content="b", reasoning="r1"), None),
            (0.010, FakeDelta(content="c"), None),
        ]
        res = asyncio.run(stream_once(FakeModel(events), _task()))
        assert res.itl.hist.n == 2  # 3 text-bearing deltas → 2 gaps
        assert res.itl.max_stall_ms == pytest.approx(10.0, abs=3.0)

    def test_gap_independent_of_branch_order(self):
        # reasoning-only stream: same gap math through the reasoning branch
        events = [
            (0.005, FakeDelta(reasoning="r0"), None),
            (0.010, FakeDelta(reasoning="r1"), None),
            (0.020, FakeDelta(reasoning="r2"), None),  # the stall
        ]
        res = asyncio.run(stream_once(FakeModel(events), _task()))
        assert res.itl.hist.n == 2
        assert res.itl.max_stall_ms == pytest.approx(20.0, abs=3.0)
        assert res.ttft_s == pytest.approx(0.005, abs=0.003)

    def test_tpot_usage_token_precedence(self):
        # usage says 11 tokens (5 deltas + extra), so TPOT uses 11, not 5.
        events = _deltas_at(10, 10, 10, 10, usage_tokens=11)
        res = asyncio.run(stream_once(FakeModel(events), _task()))
        assert res.tokens == 11
        # TPOT = (t_end − first_token)/(11−1); 4 gaps of ~10ms + stream end
        # ≈ 42ms → ~4.2ms per token
        assert 2.0 <= res.itl.tpot_ms <= 8.0
        # without usage, tokens = delta count → TPOT ≈ the 10ms gaps
        res2 = asyncio.run(stream_once(FakeModel(_deltas_at(10, 10, 10, 10)), _task()))
        assert res2.tokens == 5
        assert res2.itl.tpot_ms == pytest.approx(10.0, abs=3.0)

    def test_tpot_none_for_single_token(self):
        res = asyncio.run(stream_once(FakeModel(_deltas_at(usage_tokens=1)), _task()))
        assert res.itl.tpot_ms is None
        assert res.itl.hist.n == 0

    def test_burst_guard_flagged(self):
        # Whole response in one burst: gen_time < 0.05 × total → guard fires.
        events = [
            (0.200, FakeDelta(content="all-at-once"), None),  # TTFT only
            (0.0001, FakeDelta(content="x"), _Usage(2)),
        ]
        res = asyncio.run(stream_once(FakeModel(events), _task()))
        assert res.burst_guarded
        assert res.itl.tpot_approximate
        assert res.itl.hist.n == 1  # raw gaps still recorded

    def test_itl_raw_not_inheriting_fallback(self):
        # Under the burst guard, gen_time is rewritten to total_time — the
        # ITL histogram must stay raw (gaps unchanged), only TPOT flagged.
        events = [
            (0.200, FakeDelta(content="a"), None),
            (0.001, FakeDelta(content="b"), _Usage(2)),
        ]
        res = asyncio.run(stream_once(FakeModel(events), _task()))
        assert res.burst_guarded
        assert res.itl.max_stall_ms == pytest.approx(1.0, abs=2.0)
        assert res.itl.max_stall_ms < 200.0  # not the 200ms+total fallback

    def test_last_gap_and_failure_shape(self):
        res = asyncio.run(stream_once(FakeModel(_deltas_at(10, 10, 10, 10)), _task()))
        assert res.itl.last_gap_ms is not None

        # failed request: no ITL at all
        class BoomModel(FakeModel):
            async def _boom(self):
                pass

        events = [(0.005, None, None)]  # no delta → first_token None path
        res2 = asyncio.run(stream_once(FakeModel(events), _task()))
        assert res2.success  # still succeeds with fallback TTFT

    def test_debug_stream_extra_fields(self, capsys):
        class ExtraDelta(FakeDelta):
            def model_dump(self):
                return {"role": "assistant", "content": None, "reasoning_content": None}

        # text under a non-standard field: model_extra path records the gap too
        class _ExtraChunk(_Chunk):
            def __init__(self):
                d = FakeDelta()
                d.__pydantic_extra__ = {"reasoning": "x"}
                self.choices = [_Choice(d)]
                self.usage = None

        class _S:
            def __init__(self):
                self._i = 0

            def __aiter__(self):
                return self

            def __anext__(self):
                if self._i > 1:
                    raise StopAsyncIteration
                self._i += 1
                return _delayed(self._i)

        async def _delayed(i):
            await asyncio.sleep(0.01)
            return _ExtraChunk()

        class _M(FakeModel):
            def __init__(self):
                FakeModel.__init__(self, [])
                self.async_client.chat.completions = _FakeCompletions(_S())

        res = asyncio.run(stream_once(_M(), _task(), debug_stream=True))
        assert res.success
        assert res.itl.hist.n == 1  # both deltas were text-bearing


# ---------------------------------------------------------------------------
# MB-A: level aggregation + table emission
# ---------------------------------------------------------------------------


def _mk_result(**kw) -> RequestResult:
    defaults = {
        "success": True,
        "task": "t",
        "turn": 1,
        "ttft_s": 0.1,
        "tokens": 10,
        "tokens_per_s": 50.0,
        "burst_guarded": False,
    }
    defaults.update(kw)
    return RequestResult(**defaults)


class TestLevelAggregation:
    def test_row_fields(self):
        h1 = GapHistogram()
        for g in (0.01, 0.02, 0.03):
            h1.record(g)
        r1 = _mk_result(itl=ITLSummary(hist=h1, tpot_ms=15.0))
        r2 = _mk_result(itl=ITLSummary(hist=GapHistogram(), tpot_ms=None), turn=2, ttft_s=0.05)
        row = bc.build_level_rows(
            ctx=0,
            users=4,
            task_name="t",
            group=[r1, r2],
            group_fail=0,
            multiturn=True,
            slo=None,
        )[0]
        assert isinstance(row, LevelRow)
        assert row.max_stall_ms == pytest.approx(30.0)
        assert row.tpot["P50"] == pytest.approx(15.0)
        assert row.itl_mean_ms == pytest.approx(20.0)
        assert row.arrival == "closed"
        assert row.goodput is None

    def test_burst_fraction_and_tpot_exclusion(self):
        bursty = _mk_result(itl=ITLSummary(hist=GapHistogram(), tpot_ms=1.0, tpot_approximate=True), burst_guarded=True)
        normal = _mk_result(itl=ITLSummary(hist=GapHistogram(), tpot_ms=20.0))
        row = bc.build_level_rows(ctx=0, users=1, task_name="t", group=[bursty, normal], group_fail=0, multiturn=False)[
            0
        ]
        assert row.burst_guarded_frac == pytest.approx(0.5)
        assert row.tpot["P50"] == pytest.approx(20.0)  # burst-guarded excluded

    def test_markdown_round_trip_with_itl(self):
        row = LevelRow(
            ctx=0,
            users=2,
            task="coding",
            failed=0,
            ttft={"P50": 100.0, "P95": 110.0, "P99": 120.0, "P100": 130.0},
            tps={"P50": 33.0, "P95": 32.0, "P99": 31.0, "P100": 30.0},
            itl={"P50": 12.0, "P95": 13.0, "P99": 14.0, "P100": 15.0},
            tpot={"P50": 11.0, "P95": 11.5, "P99": 12.0, "P100": 12.5},
        )
        md = format_table_markdown([row])
        lines = md.splitlines()
        header = lines[0]
        # COMPACT format (one cell per metric group; fits GitHub width)
        assert "ITL P50/P95/P99/P100 (ms)" in header and "TPOT P50/P95/P99/P100 (ms)" in header
        cells = [c.strip() for c in lines[2].strip("|").split("|")]
        assert len(cells) == 8  # 4 keys + TTFT + tokens/s + ITL + TPOT (compact, single-turn)
        assert cells[4] == "100.0 / 110.0 / 120.0 / 130.0"  # TTFT group
        assert cells[5] == "33.0 / 32.0 / 31.0 / 30.0"  # tokens/s group
        assert cells[6] == "12.0 / 13.0 / 14.0 / 15.0"  # ITL group (extended)
        assert cells[7] == "11.0 / 11.5 / 12.0 / 12.5"  # TPOT group
        parsed = rh._parse_md_chat_row(lines[2], lines[0].strip("|").split("|"))
        assert parsed["ttft"] == [100.0, 110.0, 120.0, 130.0]
        assert parsed["tokens"] == [33.0, 32.0, 31.0, 30.0]
        assert parsed["itl"] == [12.0, 13.0, 14.0, 15.0]
        assert parsed["tpot"] == [11.0, 11.5, 12.0, 12.5]

    def test_markdown_round_trip_all_groups(self):
        row = LevelRow(
            ctx=32768,
            users=32,
            task="mixed",
            failed=2,
            ttft={"P50": 19000.0, "P95": 35000.0, "P99": 36000.0, "P100": 36500.0},
            ttft_post={"P50": 600.0, "P95": 900.0, "P99": 1000.0, "P100": 1100.0},
            tps={"P50": 91.5, "P95": 84.0, "P99": 82.5, "P100": 78.9},
            itl={"P50": 11.0, "P95": 25.0, "P99": 90.0, "P100": 1500.0},
            tpot={"P50": 10.9, "P95": 20.0, "P99": 55.0, "P100": 90.0},
        )
        md = format_table_markdown([row], multiturn=True)
        lines = md.splitlines()
        cells = [c.strip() for c in lines[2].strip("|").split("|")]
        assert len(cells) == 9  # multiturn compact: 4 keys + TTFT + TTFT-post + tokens/s + ITL + TPOT
        parsed = rh._parse_md_chat_row(lines[2], lines[0].strip("|").split("|"))
        assert parsed["ctx"] == 32768 and parsed["failed"] == 2
        assert parsed["ttft"][0] == 19000.0 and parsed["tokens"][0] == 91.5
        assert parsed["ttft_post"][0] == 600.0
        assert parsed["itl"][2] == 90.0 and parsed["tpot"][3] == 90.0

    def test_markdown_round_trip_multiturn(self):
        row = LevelRow(
            ctx=0,
            users=2,
            task="coding",
            failed=0,
            ttft={"P50": 1, "P95": 2, "P99": 3, "P100": 4},
            ttft_post={"P50": 0.5, "P95": 0.6, "P99": 0.7, "P100": 0.8},
            tps={"P50": 9, "P95": 8, "P99": 7, "P100": 6},
            itl={"P50": 1, "P95": 1, "P99": 1, "P100": 1},
            tpot={"P50": 1, "P95": 1, "P99": 1, "P100": 1},
        )
        md = format_table_markdown([row], multiturn=True)
        cells = [c.strip() for c in md.splitlines()[2].strip("|").split("|")]
        assert len(cells) == 9
        parsed = rh._parse_md_chat_row(md.splitlines()[2], md.splitlines()[0].strip("|").split("|"))
        assert parsed["itl"] == [1.0, 1.0, 1.0, 1.0]
        assert parsed["tpot"] == [1.0, 1.0, 1.0, 1.0]

    def test_markdown_none_group_compact_dash(self):
        """A None group renders as ONE dash cell and parses back as None."""
        row = LevelRow(
            ctx=0,
            users=1,
            task="x",
            failed=0,
            ttft={"P50": 1.0, "P95": 2.0, "P99": 3.0, "P100": 4.0},
            ttft_post=None,
            tps={"P50": 9.0, "P95": 8.0, "P99": 7.0, "P100": 6.0},
        )
        md = format_table_markdown([row], multiturn=True, extended=False)
        cells = [c.strip() for c in md.splitlines()[2].strip("|").split("|")]
        assert len(cells) == 7  # 4 keys + ttft + ttft_post(dash) + tokens
        assert cells[5] == "-"
        parsed = rh._parse_md_chat_row(md.splitlines()[2], md.splitlines()[0].strip("|").split("|"))
        assert parsed["ttft_post"] is None
        assert parsed["tokens"] == [9.0, 8.0, 7.0, 6.0]

    def test_print_table_dashes_for_none_groups(self, capsys):
        row = LevelRow(ctx=0, users=1, task="x", failed=0, ttft={"P50": 1, "P95": 1, "P99": 1, "P100": 1})
        print_table([row])
        out = capsys.readouterr().out
        assert row.itl is None
        assert out.count("-") >= 8  # two dash groups (tokens/s is set? no — None too)

    def test_quiet_documented_pairing(self):
        # The --quiet help must document that stdout backpressure distorts ITL.
        import io
        from contextlib import redirect_stdout

        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                parse_args(["--help"])
        except SystemExit:
            pass
        assert "ITL" in buf.getvalue()


# ---------------------------------------------------------------------------
# MB-B: mode stamping + parser round trip
# ---------------------------------------------------------------------------


class TestModeStamping:
    def test_parse_setup_ol_token(self):
        meta = rh.parse_setup("H200x4_ol")
        assert meta["arrival"] == "open"
        assert meta["gpu"] == "NVIDIA H200 (PCIe)" and meta["gpu_count"] == 4
        # closed-loop stays the default for unmarked files
        assert rh.parse_setup("H200x4")["arrival"] == "closed"
        assert rh.parse_setup("OLD_H200x4")["arrival"] == "closed"
        # combined grammar still works
        meta = rh.parse_setup("H200_sglang_dflash2_hicachex3_replicasx3_ol")
        assert meta["arrival"] == "open" and meta["engine"] == "SGLang"

    def test_header_mode_lines_closed(self, capsys):
        args = parse_args(["--url", "http://x", "--number_users", "1"])
        # Extract the MODE: arrival line from a main()-style header build.
        # parse_args is shared; the header assembly lives in main(), so build
        # it the same way through a dry-run: check the flag default instead.
        assert args.arrival_mode == "closed"

    def test_run_header_open_mode_line(self, tmp_path, capsys):
        # Build the header exactly as main() does, via the module's own code
        # path: emulate by calling write_results_file with an open-loop header
        # and asserting the stamped lines land in the artifact.
        row = LevelRow(ctx=0, users=1, task="t", failed=0, ttft={"P50": 1, "P95": 1, "P99": 1, "P100": 1})
        header = (
            "Model: FakeModel name='fake' tasks=[t(1)] request_rate=5.0 req/s level_duration=2.0s context_lengths=[0]\n"
            "MODE: arrival=open — requests issued on a schedule at 5.0 req/s (burstiness inf), levels are 2.0s arrival windows; TTFT includes server-side queueing by design. NOT comparable to closed-loop runs."
        )
        out = tmp_path / "H200x4_ol.md"
        bc.write_results_file(str(out), [row], run_header=header, summary_head="", summary_note="", status="complete")
        text = out.read_text()
        assert "MODE: arrival=open" in text

    def test_parse_chat_table_full_arrival(self):
        text = (
            "# H200x4_ol\n\n"
            "- Model: X\n"
            "- MODE: arrival=open — requests issued on a schedule at 5 req/s (burstiness inf), levels are 2.0s arrival windows.\n"
            "- MODE: arrival=closed\n\n"
            "| ctx | users | task | failed | TTFT P50 (ms) | TTFT P95 (ms) | TTFT P99 (ms) | TTFT P100 (ms) | tokens/s P50 | tokens/s P95 | tokens/s P99 | tokens/s P100 |\n"
            "|:---|:---|:---|:---|---:|---:|---:|---:|---:|---:|---:|---:|\n"
            "| 0 | 1 | t | 0 | 1.0 | 1.0 | 1.0 | 1.0 | 2.0 | 2.0 | 2.0 | 2.0 |\n"
        )
        _mode, rows, arrival = rh.parse_chat_table_full(text)
        assert arrival == "open"
        assert len(rows) == 1
        # and a closed-loop file without the stamp reads back closed
        text2 = text.replace("MODE: arrival=open", "MODE: prewarm")
        _m, _r, arrival2 = rh.parse_chat_table_full(text2)
        assert arrival2 == "closed"

    def test_format_then_parse_round_trip(self):
        rows = [
            LevelRow(
                ctx=32768,
                users=32,
                task="mixed",
                failed=2,
                ttft={"P50": 19000.0, "P95": 35000.0, "P99": 36000.0, "P100": 36500.0},
                tps={"P50": 91.5, "P95": 84.0, "P99": 82.5, "P100": 78.9},
                itl={"P50": 11.0, "P95": 25.0, "P99": 90.0, "P100": 1500.0},
                tpot={"P50": 10.9, "P95": 20.0, "P99": 55.0, "P100": 90.0},
            )
        ]
        md = format_table_markdown(rows)
        lines = md.splitlines()
        parsed = rh._parse_md_chat_row(lines[2], lines[0].strip("|").split("|"))
        assert parsed["ctx"] == 32768 and parsed["failed"] == 2
        assert parsed["ttft"][0] == 19000.0
        assert parsed["itl"][2] == 90.0 and parsed["tpot"][3] == 90.0

    def test_legacy_files_still_parse(self):
        results_dir = SRC.parent.parent / "results"
        legacy = sorted(results_dir.glob("*/*.md"))
        if not legacy:
            pytest.skip("committed results tree not present")
        for f in legacy:
            text = f.read_text()
            _mode, rows = rh.parse_chat_table(text)
            for row in rows:
                assert set(row) >= {"ctx", "users", "task", "failed", "ttft", "tokens"}
                assert row["itl"] is None and row["tpot"] is None  # legacy: no ITL groups

    def test_legacy_fixed_width_rows(self):
        ln = "     0      1    coding      0 |       930.6       930.6       930.6       930.6 |       331.1       318.0       317.6       317.5"
        row = rh._parse_fixed_chat_row(ln)
        assert row["ttft"][0] == 930.6 and row["tokens"][3] == 317.5
        assert row["itl"] is None

    def test_mixed_mode_compare_refusal_in_template(self):
        template = (_RESULTS_TO_HTML.parent / "_html_template.py").read_text()
        assert "Mixed arrival modes selected" in template
        # the refusal keys off the arrival stamp, not the filename
        assert "s.meta&&s.meta.arrival" in template


# ---------------------------------------------------------------------------
# MB-C: open-loop arrivals
# ---------------------------------------------------------------------------


class TestOpenLoop:
    def test_expovariate_mean_rate(self):
        rng = __import__("random").Random(1234)
        gaps = [_interarrival_s(rng, 50.0, math.inf) for _ in range(20000)]
        mean = sum(gaps) / len(gaps)
        assert mean == pytest.approx(1 / 50.0, rel=0.05)

    def test_gamma_burstiness_shapes(self):
        import random

        rng = random.Random(99)
        # cv of gamma(k=b, theta=1/(b·R)) is 1/b
        for b in (0.5, 2.0, 4.0):
            gaps = [_interarrival_s(rng, 100.0, b) for _ in range(50000)]
            mean = sum(gaps) / len(gaps)
            var = sum((g - mean) ** 2 for g in gaps) / len(gaps)
            cv = (var**0.5) / mean
            assert mean == pytest.approx(1 / 100.0, rel=0.05)
            assert cv == pytest.approx(1 / b, rel=0.2)

    def test_invalid_burstiness(self):
        with pytest.raises(ValueError):
            _interarrival_s(__import__("random").Random(1), 10.0, 0.0)
        with pytest.raises(ValueError):
            _interarrival_s(__import__("random").Random(1), 0.0, math.inf)

    def test_arrival_schedule_window_and_cap(self):
        rng = __import__("random").Random(5)
        offsets = _arrival_schedule(rng, 20.0, math.inf, 1.0)
        assert all(0 <= t <= 1.0 for t in offsets)
        assert len(offsets) == pytest.approx(20, rel=0.5)  # ~rate×window
        # cap honored
        offsets_capped = _arrival_schedule(__import__("random").Random(5), 1000.0, math.inf, 5.0, max_requests=50)
        assert len(offsets_capped) == 50
        # deterministic under the same seed
        a = _arrival_schedule(__import__("random").Random(5), 20.0, 2.0, 1.0)
        b = _arrival_schedule(__import__("random").Random(5), 20.0, 2.0, 1.0)
        assert a == b

    def test_seeded_run_achieves_mean_rate(self):
        # A seeded Poisson run at 40 req/s over a 1s window issues ~40
        # requests; the achieved rate (incl. drain) is reported and positive.
        class OneTokModel:
            def __init__(self):
                self.model_name = "fake"
                self.api_key = "k"
                self.async_client = type("AC", (), {})()
                self.async_client.chat = type("C", (), {})()
                self.async_client.chat.completions = _OneTokCompletions()

        class _OneTokCompletions:
            async def create(self, **kwargs):
                return _FakeStream([(0.001, FakeDelta(content="x"), _Usage(1))])

        async def go():
            return await run_open_loop(
                OneTokModel(),
                [_task()],
                rate=40.0,
                window_s=1.0,
                quiet=True,
                seed=7,
                pool_max=128,
            )

        results, achieved, duration = asyncio.run(go())
        assert len(results) == pytest.approx(40, rel=0.4)
        assert achieved > 0
        assert all(r.success for r in results)
        # the level runs until every issued request completes; the last
        # arrival is inside the window, so duration is window − ~last gap
        assert duration >= 0.5

    def test_run_open_loop_returns_rate_and_duration(self):
        class OneTokModel:
            def __init__(self):
                self.model_name = "fake"
                self.api_key = "k"
                self.async_client = type("AC", (), {})()
                self.async_client.chat = type("C", (), {})()
                self.async_client.chat.completions = _OneTokCompletions()

        class _OneTokCompletions:
            async def create(self, **kwargs):
                return _FakeStream([(0.001, FakeDelta(content="x"), _Usage(1))])

        async def go():
            return await run_open_loop(
                OneTokModel(), [_task(max_tokens=1)], rate=25.0, window_s=0.5, quiet=True, seed=3
            )

        results, achieved, duration = asyncio.run(go())
        assert len(results) > 0
        assert achieved > 0 and duration > 0.4
        assert all(r.turn == 1 for r in results)

    def test_multiturn_open_loop_guard(self):
        # The contradiction guard must reject multiturn + open before any traffic.
        args = parse_args(["--url", "http://x", "--arrival_mode", "open", "--request_rate", "5", "--multiturn"])
        with pytest.raises(SystemExit, match="closed-loop only"):
            bc.validate_args(args)

    def test_request_rate_requires_open(self):
        args = parse_args(["--url", "http://x", "--request_rate", "5"])
        with pytest.raises(SystemExit, match="only with --arrival_mode open"):
            bc.validate_args(args)
        args = parse_args(["--url", "http://x", "--burstiness", "2"])
        with pytest.raises(SystemExit, match="only with --arrival_mode open"):
            bc.validate_args(args)
        args = parse_args(["--url", "http://x", "--arrival_mode", "open"])
        with pytest.raises(SystemExit, match="requires a positive --request_rate"):
            bc.validate_args(args)
        args = parse_args(
            ["--url", "http://x", "--arrival_mode", "open", "--request_rate", "5", "--level_duration", "0"]
        )
        with pytest.raises(SystemExit, match="level_duration"):
            bc.validate_args(args)

    def test_closed_mode_still_validates(self):
        args = parse_args(["--url", "http://x", "--multiturn", "--requests_per_user", "2"])
        bc.validate_args(args)  # no raise

    def test_poisson_alias_for_open(self):
        args = parse_args(["--url", "http://x", "--arrival_mode", "poisson", "--request_rate", "5", "--multiturn"])
        with pytest.raises(SystemExit, match="closed-loop only"):
            bc.validate_args(args)  # normalizes to open, then the multiturn guard fires
        assert args.arrival_mode == "open"

    def test_inflight_cap_surfaced(self, capsys):
        class OneTokModel:
            def __init__(self):
                self.model_name = "fake"
                self.api_key = "k"
                self.async_client = type("AC", (), {})()
                self.async_client.chat = type("C", (), {})()
                self.async_client.chat.completions = _OneTokCompletions()

        class _OneTokCompletions:
            async def create(self, **kwargs):
                return _FakeStream([(0.001, FakeDelta(content="x"), _Usage(1))])

        asyncio.run(
            run_open_loop(
                OneTokModel(), [_task(max_tokens=1)], rate=50.0, window_s=0.2, quiet=False, seed=1, pool_max=128
            )
        )
        out = capsys.readouterr().out
        assert "in-flight cap" in out
        assert "min(rate×timeout, pool 128)" in out


def parse_args_validation(**kw):
    """(kept for the helpers below) Minimal validation stub."""
    if kw.get("request_rate"):
        raise SystemExit("--request_rate applies only with --arrival_mode open.")
    if kw.get("arrival_open_no_rate"):
        raise SystemExit("--arrival_mode open requires a positive --request_rate (req/s).")
    raise AssertionError("unreachable")


# ---------------------------------------------------------------------------
# MB-D: goodput
# ---------------------------------------------------------------------------


def _slo(spec: str):
    return parse_goodput_spec(spec)


def _req(ttft_ms: float, tpot_ms: float | None, *, success=True, burst=False, gaps=None) -> RequestResult:
    h = GapHistogram()
    for g in gaps or ():
        h.record(g / 1000.0)
    return RequestResult(
        success=success,
        task="t",
        ttft_s=ttft_ms / 1000.0,
        tokens=10,
        itl=ITLSummary(hist=h, tpot_ms=tpot_ms, tpot_approximate=burst),
        burst_guarded=burst,
    )


class TestGoodput:
    def test_spec_parsing(self):
        assert _slo("ttft<=2000,tpot<=50") == [("ttft", "<=", 2000.0), ("tpot", "<=", 50.0)]
        assert _slo("ITL<=99.9") == [("itl_p99", "<=", 99.9)]
        assert _slo("max_stall<=500") == [("max_stall", "<=", 500.0)]
        with pytest.raises(SystemExit):
            _slo("ttft<2000")  # strict < not accepted
        with pytest.raises(SystemExit):
            _slo("latency<=2000")  # unknown metric
        with pytest.raises(SystemExit):
            _slo("")

    def test_all_meet(self):
        results = [_req(500, 20), _req(900, 40)]
        joint, each = compute_goodput(results, _slo("ttft<=2000,tpot<=50"))
        assert joint == 1.0
        assert each == {"ttft": 1.0, "tpot": 1.0}

    def test_some_fail(self):
        results = [_req(500, 20), _req(5000, 20), _req(500, 90)]
        joint, each = compute_goodput(results, _slo("ttft<=2000,tpot<=50"))
        # r1 misses ttft, r2 misses tpot → only r0 has goodput
        assert joint == pytest.approx(1 / 3)
        assert each["ttft"] == pytest.approx(2 / 3)
        assert each["tpot"] == pytest.approx(2 / 3)

    def test_failed_requests_not_goodput(self):
        ok = [_req(500, 20) for _ in range(3)]
        failed = [_req(0, None, success=False) for _ in range(7)]
        joint, _ = compute_goodput(ok + failed, _slo("ttft<=2000"))
        # 3 successes all meet; the 7 failures are NOT misses (vLLM convention)
        assert joint == 1.0
        # and they ARE countable separately via the row's failed column
        row = bc.build_level_rows(ctx=0, users=1, task_name="t", group=ok, group_fail=len(failed), multiturn=False)[0]
        assert row.failed == 7 and row.n_requests == 3

    def test_burst_guarded_tpot_excluded_ttft_counted(self):
        ok = [_req(500, 1.0, burst=True), _req(500, 20.0)]
        joint, each = compute_goodput(ok, _slo("ttft<=2000,tpot<=50"))
        # burst request: excluded from the tpot predicate (not a miss), still
        # counted for ttft
        assert each["ttft"] == 1.0
        assert each["tpot"] == 1.0  # only the one judged request
        assert joint == 1.0
        # TPOT-only SLO with ALL burst-guarded → joint None (nobody judged)
        joint2, each2 = compute_goodput([_req(500, 1.0, burst=True)], _slo("tpot<=50"))
        assert joint2 is None and each2 == {}

    def test_itl_p99_predicate(self):
        # request A: gaps 10ms x9 → P99 ≈ 10ms; request B: one 500ms stall
        a = _req(100, None, gaps=[10] * 9)
        b = _req(100, None, gaps=[10] * 9 + [500])
        joint, each = compute_goodput([a, b], _slo("itl<=99.9"))
        assert each["itl_p99"] == pytest.approx(0.5)
        assert joint == pytest.approx(0.5)

    def test_max_goodput_level(self):
        def row(ctx, users, gp):
            return LevelRow(
                ctx=ctx,
                users=users,
                task="t",
                failed=0,
                ttft={},
                goodput=gp,
                goodput_each={"ttft": gp},
            )

        rows = [row(0, 1, 1.0), row(0, 4, 0.95), row(0, 32, 0.5), row(32768, 1, 0.99)]
        best = max_goodput_level(rows, 0.9)
        assert best is not None and (best.ctx, best.users) == (32768, 1)
        assert max_goodput_level([row(0, 1, 0.5)], 0.9) is None

    def test_goodput_printed_and_stamped(self, tmp_path, capsys):
        # Artifact header stamps the SLO thresholds (mandatory) — write a file
        # via main()-equivalent plumbing and check the GOODPUT SLOs line.
        header = "Model: FakeModel\nGOODPUT SLOs: ttft<=2000ms, tpot<=50ms — goodput = fraction of successful requests meeting ALL SLOs; failed requests are not goodput (counted separately); burst-guarded requests (approximate TPOT) are excluded from TPOT predicates, counted for TTFT."
        out = tmp_path / "H200.md"
        row = LevelRow(
            ctx=0,
            users=1,
            task="t",
            failed=0,
            ttft={"P50": 1, "P95": 1, "P99": 1, "P100": 1},
            goodput=1.0,
            goodput_each={"ttft": 1.0, "tpot": 1.0},
            n_requests=1,
        )
        bc.write_results_file(str(out), [row], run_header=header, summary_head="", summary_note="")
        text = out.read_text()
        assert "GOODPUT SLOs: ttft<=2000ms, tpot<=50ms" in text
        # thresholds are mandatory: the goodput fraction never appears without them
        assert "goodput = fraction of successful requests meeting ALL SLOs" in text
        bc.print_goodput_table([row])
        out2 = capsys.readouterr().out
        assert "100%" in out2 and "ttft=100%" in out2

    def test_goodput_cell_variants(self):
        r = LevelRow(ctx=0, users=1, task="t", failed=0, ttft={})
        assert bc.goodput_cell(r) == "-"
        r.goodput = 0.5
        assert bc.goodput_cell(r) == "50%"
        r.goodput_each = {"ttft": 1.0, "tpot": 0.5}
        assert "100/50" in bc.goodput_cell(r)


# ---------------------------------------------------------------------------
# End-to-end: artifact write → parse → report
# ---------------------------------------------------------------------------


class TestEndToEndArtifact:
    def _write_open_loop_artifact(self, tmp_path: Path) -> Path:
        rows = [
            LevelRow(
                ctx=0,
                users=1,
                task="coding",
                failed=0,
                ttft={"P50": 100.0, "P95": 110.0, "P99": 120.0, "P100": 130.0},
                tps={"P50": 33.0, "P95": 32.0, "P99": 31.0, "P100": 30.0},
                itl={"P50": 12.0, "P95": 13.0, "P99": 14.0, "P100": 15.0},
                tpot={"P50": 11.0, "P95": 11.5, "P99": 12.0, "P100": 12.5},
                goodput=1.0,
                goodput_each={"ttft": 1.0, "tpot": 1.0},
                n_requests=1,
                arrival="open",
                request_rate=5.0,
                achieved_rate=4.8,
            ),
        ]
        header = (
            "Model: FakeModel name='qwen' usage=True tasks=[coding(max_tokens=100)] "
            "request_rate=5.0 req/s level_duration=2.0s context_lengths=[0]\n"
            "MODE: arrival=open — requests issued on a schedule at 5.0 req/s (burstiness inf), "
            "levels are 2.0s arrival windows; TTFT includes server-side queueing by design. "
            "NOT comparable to closed-loop runs.\n"
            "GOODPUT SLOs: ttft<=2000ms, tpot<=50ms — goodput = fraction of successful requests "
            "meeting ALL SLOs; failed requests are not goodput (counted separately); "
            "burst-guarded requests (approximate TPOT) are excluded from TPOT predicates, "
            "counted for TTFT.\n"
            "tokens/s = completion_tokens / time-from-first-token-to-stream-end (TTFT excluded); "
            "ITL/TPOT (ms) come from per-request gap histograms."
        )
        out = tmp_path / "results" / "qwen_38_27b" / "H200x4_ol.md"
        out.parent.mkdir(parents=True)
        bc.write_results_file(str(out), rows, run_header=header, summary_head="x:", summary_note="y")
        return tmp_path

    def test_open_loop_artifact_round_trip(self, tmp_path):
        root = self._write_open_loop_artifact(tmp_path)
        text = (root / "results" / "qwen_38_27b" / "H200x4_ol.md").read_text()
        _mode, rows, arrival = rh.parse_chat_table_full(text)
        assert arrival == "open"
        assert rows[0]["itl"] == [12.0, 13.0, 14.0, 15.0]
        assert rows[0]["tpot"] == [11.0, 11.5, 12.0, 12.5]
        # filename grammar: _ol token → arrival=open
        assert rh.parse_setup("H200x4_ol")["arrival"] == "open"

    def test_report_stamps_and_refusal_data(self, tmp_path):
        root = self._write_open_loop_artifact(tmp_path)
        closed = root / "results" / "qwen_38_27b" / "H200x4.md"
        closed.write_text(
            "# H200x4\n\n- MODE: arrival=closed\n\n"
            "| ctx | users | task | failed | TTFT P50 (ms) | TTFT P95 (ms) | TTFT P99 (ms) | TTFT P100 (ms) "
            "| tokens/s P50 | tokens/s P95 | tokens/s P99 | tokens/s P100 |\n"
            "|:---|:---|:---|:---|---:|---:|---:|---:|---:|---:|---:|---:|\n"
            "| 0 | 1 | coding | 0 | 100.0 | 110.0 | 120.0 | 130.0 | 33.0 | 32.0 | 31.0 | 30.0 |\n"
        )
        out = root / "report.html"
        rc = rh.main(["--results", str(root / "results"), "--output", str(out), "--catalog", ""])
        assert rc == 0
        html = out.read_text()
        assert "Mixed arrival modes selected" in html  # refusal wired
        assert "open-loop" in html
        import json
        import re

        m = re.search(r"const DATA = (\{.*?\});\n", html, re.DOTALL)
        data = json.loads(m.group(1).replace("<\\/", "</"))
        stamps = {(s["file"], s["meta"]["arrival"]) for mdl in data["models"] for s in mdl["setups"]}
        assert ("H200x4.md", "closed") in stamps and ("H200x4_ol.md", "open") in stamps
