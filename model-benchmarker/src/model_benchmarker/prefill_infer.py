"""Infer prefill throughput from SGLang server log lines (MB-PREFILL).

The chat benchmark measures TTFT client-side, which conflates prefill compute
with queue wait and HTTP overhead.  SGLang's own per-batch log line reports
the engine's view directly::

    [2026-10-22 13:49:23 TP0 EP0] Prefill batch, #new-seq: 1, #new-token: 16384,
    #cached-token: 0, full token usage: 0.01, swa token usage: 0.00,
    #running-req: 0, #queue-req: 0, #pending-token: 321161, cuda graph: False,
    input throughput (token/s): 4453.26, est. prefill TFLOPS/s (per GPU): 654.1

Two fields carry the measurement:

* "input throughput (token/s)" - the rate at which THIS batch step computed its
  newly-attended tokens.  It counts #new-token only: tokens served from the
  prefix cache (#cached-token) are attended over but not recomputed, so they do
  not appear in the rate.  Verified against wall clock: for consecutive
  chunked-prefill steps, #new-token / input throughput equals the timestamp
  delta to the next line (to within timer resolution).
* "#pending-token" - tokens still queued.  It decreases by exactly #new-token on
  each consecutive chunk, so a run of steps holding that invariant is one
  continuous drain of the queue.

Aggregate prefill throughput for a burst is sum(#new-token) over the burst
divided by its wall-clock span - the engine's own token count over the
engine's own clock, with no client-side timing in the path.

WHAT THIS CANNOT RECOVER.  #pending-token is the GLOBAL queued backlog, not a
per-request counter, and #new-seq reports the number of sequences in the batch
(observed as 1 throughout chunked prefill).  So the log does NOT reveal how many
requests were in flight or how big each one was: a burst of 353,929 tokens may
be eleven 32k requests, one 353k request, or any mix.  This module therefore
reports tokens and chunk counts, plus an "equivalent 32k requests" figure for
scale, and deliberately never claims a request count.

Caveats this module encodes rather than hides:

* "est. prefill TFLOPS/s (per GPU)" is unusable on the first chunk of a request
  (it divides whole-request FLOPs by one chunk's time, and reports values far
  above any real GPU peak) and on sub-100 ms steps (timer floor).  Those
  samples are dropped by the plausible_tflops predicate.
* A log window is bounded by container log retention.  infer_prefill reports
  the covered span so a caller never presents a 3-hour slice as if it were a
  full benchmark run.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from itertools import pairwise

# H200/H100-class BF16 dense peak is ~989 TFLOPS/GPU. Anything above this is a
# unit/timer artifact, not a measurement (SGLang reports >40000 on some steps).
TFLOPS_CEILING = 989.0
# Steps shorter than this are dominated by timer resolution: 16384 tokens at
# "457705 tok/s" is a 36 ms step, which no real prefill achieves.
MIN_STEP_SECONDS = 0.1
# A burst is recognised by a queued backlog this large; the benchmark's 32k
# levels sit far above it, idle single-shot traffic far below.
BURST_PENDING_THRESHOLD = 100_000
# Two waves separated by more than this are separate bursts, not one.
MAX_BURST_GAP_SECONDS = 60.0
# Reference size for the scale figure (the benchmark's high-context level).
REFERENCE_CONTEXT = 32768

_TS_RE = re.compile(r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
_POD_RE = re.compile(r"^(?P<pod>[^/]+)/")
_FIELDS = {
    "new_seq": re.compile(r"#new-seq: (\d+)"),
    "new_token": re.compile(r"#new-token: (\d+)"),
    "cached_token": re.compile(r"#cached-token: (\d+)"),
    "pending_token": re.compile(r"#pending-token: (\d+)"),
    "running_req": re.compile(r"#running-req: (\d+)"),
    "queue_req": re.compile(r"#queue-req: (\d+)"),
    "input_tps": re.compile(r"input throughput \(token/s\): ([\d.]+)"),
    "tflops_per_gpu": re.compile(r"est\. prefill TFLOPS/s \(per GPU\): ([\d.]+)"),
}


def _parse_ts(value: str) -> dt.datetime:
    """Parse a log timestamp.

    SGLang prints local time with no UTC offset, so the result is deliberately
    naive: the only arithmetic done with it is a difference between two stamps
    from the same log stream, where the missing offset cancels exactly.
    """
    return dt.datetime.strptime(value, "%Y-%m-%d %H:%M:%S")  # noqa: DTZ007


@dataclass
class PrefillStep:
    """One Prefill batch log line."""

    pod: str
    timestamp: str
    new_seq: int
    new_token: int
    cached_token: int
    pending_token: int
    running_req: int
    queue_req: int
    input_tps: float
    tflops_per_gpu: float

    @property
    def step_seconds(self) -> float:
        """Engine-reported duration of this batch step."""
        return self.new_token / self.input_tps if self.input_tps else 0.0

    @property
    def attended_tokens(self) -> int:
        """Every token this step attended over (recomputed + prefix-cached)."""
        return self.new_token + self.cached_token

    @property
    def plausible_tflops(self) -> bool:
        """False when the step is too short or reports an impossible FLOP rate."""
        return self.tflops_per_gpu <= TFLOPS_CEILING and self.step_seconds >= MIN_STEP_SECONDS


@dataclass
class PrefillBurst:
    """One continuous drain of the prefill queue - the throughput measurement."""

    pod: str
    start: str
    end: str
    steps: list[PrefillStep] = field(default_factory=list)

    @property
    def tokens(self) -> int:
        """Tokens this burst actually computed (engine's own counter)."""
        return sum(s.new_token for s in self.steps)

    @property
    def wall_seconds(self) -> float:
        """Span from first to last log line.

        The final step's own duration is not included: measured at 0.15 s of a
        133 s burst, it is below the log's one-second timestamp resolution.
        """
        return (_parse_ts(self.end) - _parse_ts(self.start)).total_seconds()

    @property
    def aggregate_tps(self) -> float:
        """Aggregate prefill tok/s over the burst's wall-clock span."""
        w = self.wall_seconds
        return self.tokens / w if w > 0 else 0.0

    @property
    def steady_tps(self) -> float:
        """Aggregate excluding the first step (HiCache load, not prefill)."""
        rest = self.steps[1:] or self.steps
        secs = sum(s.step_seconds for s in rest)
        return sum(s.new_token for s in rest) / secs if secs else 0.0

    @property
    def equivalent_32k_requests(self) -> float:
        """Scale figure only - the log does not reveal the real request count."""
        return self.tokens / REFERENCE_CONTEXT

    @property
    def chunk_size(self) -> int:
        """Modal #new-token, i.e. the server's --chunked-prefill-size."""
        if not self.steps:
            return 0
        counts: dict[int, int] = {}
        for s in self.steps:
            counts[s.new_token] = counts.get(s.new_token, 0) + 1
        return max(counts, key=lambda k: counts[k])


def _num(line: str, pattern: re.Pattern[str]) -> float | None:
    m = pattern.search(line)
    return float(m.group(1)) if m else None


def parse_prefill_line(line: str) -> PrefillStep | None:
    """Parse one Prefill batch line; None when it is not one or is malformed."""
    if "Prefill batch" not in line:
        return None
    ts = _TS_RE.search(line)
    pod = _POD_RE.match(line)
    vals = {k: _num(line, p) for k, p in _FIELDS.items()}
    if ts is None or vals["new_token"] is None or vals["input_tps"] is None:
        return None
    return PrefillStep(
        pod=pod.group("pod") if pod else "",
        timestamp=ts.group(1),
        new_seq=int(vals["new_seq"] or 0),
        new_token=int(vals["new_token"]),
        cached_token=int(vals["cached_token"] or 0),
        pending_token=int(vals["pending_token"] or 0),
        running_req=int(vals["running_req"] or 0),
        queue_req=int(vals["queue_req"] or 0),
        input_tps=vals["input_tps"],
        tflops_per_gpu=vals["tflops_per_gpu"] or 0.0,
    )


def parse_prefill_lines(lines: list[str]) -> list[PrefillStep]:
    """Parse every prefill line, preserving order; unparseable lines are skipped."""
    out = [s for s in (parse_prefill_line(ln) for ln in lines) if s is not None]
    out.sort(key=lambda s: s.timestamp)
    return out


def split_runs(steps: list[PrefillStep]) -> list[list[PrefillStep]]:
    """Split one pod's steps into continuous queue drains.

    Consecutive steps belong to the same drain while
    prev.pending - cur.pending == cur.new_token - the invariant a chunked
    prefill maintains while it is working through one backlog, and breaks when
    the queue empties or a new wave arrives.
    """
    if not steps:
        return []
    runs: list[list[PrefillStep]] = [[steps[0]]]
    for prev, cur in pairwise(steps):
        if prev.pending_token - cur.pending_token == cur.new_token:
            runs[-1].append(cur)
        else:
            runs.append([cur])
    return runs


def find_bursts(
    runs: list[list[PrefillStep]],
    threshold: int = BURST_PENDING_THRESHOLD,
    max_gap_seconds: float = MAX_BURST_GAP_SECONDS,
) -> list[PrefillBurst]:
    """Keep the drains that represent real batched load.

    A run qualifies when its opening step saw a queued backlog of at least
    ``threshold`` tokens - idle single-shot traffic never does, a benchmark
    level does.  A gap longer than ``max_gap_seconds`` also splits bursts: two
    waves separated by an idle minute are two measurements, and merging them
    would dilute the aggregate with dead time.
    """
    bursts: list[PrefillBurst] = []
    cur: list[PrefillBurst] = []
    for run in runs:
        if not run or run[0].pending_token < threshold:
            if cur:
                bursts.append(_merge(cur))
                cur = []
            continue
        b = PrefillBurst(pod=run[0].pod, start=run[0].timestamp, end=run[-1].timestamp, steps=run)
        if cur and (_parse_ts(b.start) - _parse_ts(cur[-1].end)).total_seconds() > max_gap_seconds:
            bursts.append(_merge(cur))
            cur = []
        cur.append(b)
    if cur:
        bursts.append(_merge(cur))
    return bursts


def _merge(bursts: list[PrefillBurst]) -> PrefillBurst:
    """Fold adjacent qualifying drains into one burst."""
    return PrefillBurst(
        pod=bursts[0].pod,
        start=bursts[0].start,
        end=bursts[-1].end,
        steps=[s for b in bursts for s in b.steps],
    )


def infer_prefill(lines: list[str]) -> dict:
    """Full inference over a log slice: steps -> drains -> bursts.

    Returns a JSON-ready dict with per-pod bursts plus the log's covered span,
    so a caller can tell a full run from a retention-limited slice.
    """
    steps = parse_prefill_lines(lines)
    by_pod: dict[str, list[PrefillStep]] = {}
    for s in steps:
        by_pod.setdefault(s.pod, []).append(s)

    pods: list[dict] = []
    for pod, ps in sorted(by_pod.items()):
        bursts = find_bursts(split_runs(ps))
        pods.append(
            {
                "pod": pod,
                "steps": len(ps),
                "first": ps[0].timestamp if ps else None,
                "last": ps[-1].timestamp if ps else None,
                "bursts": [
                    {
                        "start": b.start,
                        "end": b.end,
                        "tokens": b.tokens,
                        "chunks": len(b.steps),
                        "chunk_size": b.chunk_size,
                        "wall_seconds": round(b.wall_seconds, 1),
                        "aggregate_tps": round(b.aggregate_tps, 1),
                        "steady_tps": round(b.steady_tps, 1),
                        "equivalent_32k_requests": round(b.equivalent_32k_requests, 1),
                    }
                    for b in bursts
                ],
            }
        )
    span = (steps[0].timestamp, steps[-1].timestamp) if steps else (None, None)
    return {"covered": {"first": span[0], "last": span[1]}, "pods": pods}


def format_like_log(burst: PrefillBurst) -> list[str]:
    """Render a burst as SGLang-shaped Prefill batch lines.

    One line per batch step, preserving the engine's own field values, so the
    output can be read by the same tooling (and the same eyeballs) as a raw
    server log.  #pending-token is re-derived from the tail of the burst so the
    last line correctly reaches 0.
    """
    out: list[str] = []
    remaining = sum(s.new_token for s in burst.steps)
    for s in burst.steps:
        remaining -= s.new_token
        out.append(
            f"{s.pod}/kserve-container: 2026-10-22T00:00:00 [{s.timestamp} TP0 EP0] "
            f"Prefill batch, #new-seq: {s.new_seq}, "
            f"#new-token: {s.new_token}, #cached-token: {s.cached_token}, "
            f"full token usage: 0.00, swa token usage: 0.00, "
            f"#running-req: {s.running_req}, #queue-req: {s.queue_req}, "
            f"#pending-token: {max(remaining, 0)}, cuda graph: False, "
            f"input throughput (token/s): {s.input_tps:.2f}, "
            f"est. prefill TFLOPS/s (per GPU): {s.tflops_per_gpu:.2f}"
        )
    return out
