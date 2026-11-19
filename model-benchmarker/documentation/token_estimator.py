"""Throughput model for a replica that prefills and decodes on the same wall clock.

The one assumption: prefill and decode share the box's time budget, so a second spent
prefilling is a second not spent decoding.

Cached input is cheap but not free in time. A cache hit costs a KV read -- about 1.5 us
for 7 kB at 4.8 TB/s -- against roughly 87 us of prefill per token, so a cached token is
charged 2% of what an uncached one costs (cache_read_ratio, default 0.02). At an 85%
hit rate that takes 2.3% off the total rate.

Notation
--------
T
    Total tokens per second, input and output together.
D
    Decode rate: output tokens per second the replica sustains.
P
    Prefill rate: *uncached* input tokens per second the replica sustains.
i
    Input fraction of total tokens, cached input included (default 0.85).
c
    Cache hit rate on input (default 0.85).
r
    Time a cached token costs relative to an uncached one (default 0.02).
t_o
    Fraction of wall clock spent decoding.
t_prefill, t_cache, t_i
    Fractions spent on uncached prefill, on cached reads, and on input in total
    (t_i = t_prefill + t_cache, and t_i + t_o = 1).

Derivation
----------
Over one second, with t_o spent decoding and t_i = 1 - t_o prefilling::

    O = D * t_o                 output tokens
    I = P * t_prefill           uncached input tokens actually prefilled
    C = I * c / (1 - c)         input tokens served from cache, read at P / r
    T = O + I + C = O + I / (1 - c)

The traffic mix pins the split, because the input fraction is fixed::

    i = (I + C) / T  =>  I = i * (1 - c) * T  and  T = O / (1 - i)

Solving that with the three shares adding to 1 gives::

    t_o      = (1 - i) / D / seconds
    t_prefill = i * (1 - c) / P / seconds
    t_cache   = i * c * r / P / seconds

and the closed form this module uses::

    T = 1 / ((1 - i) / D + i * ((1 - c) + c * r) / P)

The terms of that denominator are the seconds per total token spent decoding, prefilling
and reading cached prefixes. Setting r = 0 makes cache hits free, which is the older
model; the difference is a couple of percent at a realistic hit rate.

Worked example -- DeepSeek V4.1 Flash per GPU at 32K context, 64 users
(D = 913.6, P = 2879, i = c = 0.85, r = 0.02): T = 4684 total tokens/s, 76.9% of the
second spent decoding, 20.7% prefilling and 2.4% reading cached prefixes, i.e. 703
output, 597 uncached input and 3,384 cached input tokens per second.
"""

__all__ = ["TokenEstimate", "estimate_tokens", "estimate_tokens_per_second", "estimate_tokens_str"]


def estimate_tokens_per_second(
    prefill_tps: float,
    decode_tps: float,
    input_rate: float = 0.85,
    cache_hit_rate: float = 0.85,
    cache_read_ratio: float = 0.02,
) -> dict[str, float]:
    """Split a replica's throughput into output, uncached input and cached tokens.

    Everything returned is per second of wall clock, so the values read as rates:
    `total_tps` is what the box serves, and `t_i` / `t_o` say how the second was
    spent to serve it.

    Args:
        prefill_tps: Prefill rate in uncached input tokens per second.
        decode_tps: Decode rate in output tokens per second.
        input_rate: Input share of total tokens, cached input included.
        cache_hit_rate: Share of input tokens served from cache.
        cache_read_ratio: Time a cached token costs relative to an uncached one --- a
            KV read against a prefill. Default 0.02; pass 0.0 to make hits free.

    Returns:
        A dict with eight keys:

        * `t_i` -- fraction of the second spent on input, prefill plus cached reads.
        * `t_o` -- fraction of the second spent decoding (`1 - t_i`).
        * `t_prefill` / `t_cache` -- the two parts of `t_i`.
        * `input_tps` -- uncached input tokens prefilled per second.
        * `output_tps` -- output tokens decoded per second.
        * `cached_tps` -- input tokens served from cache per second.
        * `total_tps` -- input plus output tokens per second
          (`input_tps + output_tps + cached_tps`).

    Raises:
        ValueError: If a rate is not positive, a fraction is outside `[0, 1]`, or
            the mix is degenerate (every token a cache hit, so throughput is
            unbounded).

    Example:
        >>> r = estimate_tokens_per_second(prefill_tps=2879.0, decode_tps=913.6)
        >>> round(r["total_tps"])
        4684
        >>> round(r["t_i"], 3)
        0.231
    """
    if prefill_tps <= 0 or decode_tps <= 0:
        raise ValueError("prefill_tps and decode_tps must be positive")
    if not 0.0 <= input_rate <= 1.0:
        raise ValueError("input_rate must be in [0, 1]")
    if not 0.0 <= cache_hit_rate <= 1.0:
        raise ValueError("cache_hit_rate must be in [0, 1]")
    if not 0.0 <= cache_read_ratio <= 1.0:
        raise ValueError("cache_read_ratio must be in [0, 1]")

    output_fraction = 1.0 - input_rate
    uncached_input_fraction = input_rate * (1.0 - cache_hit_rate)
    cached_input_fraction = input_rate * cache_hit_rate

    # Seconds per total token: decode work, uncached-prefill work, and the read a
    # cached token still costs at cache_read_ratio of a prefill.
    prefill_seconds = uncached_input_fraction / prefill_tps
    cache_seconds = cached_input_fraction * cache_read_ratio / prefill_tps
    seconds = output_fraction / decode_tps + prefill_seconds + cache_seconds
    if seconds <= 0.0:
        raise ValueError(
            "degenerate mix: with input_rate at 1 and every token cached for free, throughput is unbounded"
        )

    total_tps = 1.0 / seconds
    t_o = (output_fraction / decode_tps) / seconds
    t_prefill = prefill_seconds / seconds
    t_cache = cache_seconds / seconds
    t_i = t_prefill + t_cache

    output_tps = decode_tps * t_o
    input_tps = prefill_tps * t_prefill
    # By subtraction rather than I * c / (1 - c), so cache_hit_rate = 1 (where that
    # quotient is 0/0) stays finite.
    cached_tps = total_tps - output_tps - input_tps

    return {
        "t_i": t_i,
        "t_o": t_o,
        "t_prefill": t_prefill,
        "t_cache": t_cache,
        "input_tps": input_tps,
        "output_tps": output_tps,
        "cached_tps": cached_tps,
        "total_tps": total_tps,
    }


_SECONDS_PER = {
    "seconds": 1.0,
    "minutes": 60.0,
    "hours": 3_600.0,
    "days": 86_400.0,
    "weeks": 604_800.0,
    "months": 2_592_000.0,  # 30 days
    "years": 31_536_000.0,  # 365 days
}


def _human_period(elapsed_seconds: float) -> str:
    """Render a duration the way a reader would say it, with the seconds alongside."""
    for singular, plural, size in (
        ("yr", "yr", 31_536_000.0),
        ("day", "days", 86_400.0),
        ("h", "h", 3_600.0),
        ("min", "min", 60.0),
    ):
        if elapsed_seconds >= size:
            n = elapsed_seconds / size
            shown = f"{n:,.0f}" if abs(n - round(n)) < 1e-9 else f"{n:,.1f}"
            return f"{shown} {singular if shown in {'1', '1.0'} else plural} ({elapsed_seconds:,.0f} s)"
    shown = (
        f"{elapsed_seconds:,.0f}" if abs(elapsed_seconds - round(elapsed_seconds)) < 1e-9 else f"{elapsed_seconds:,.1f}"
    )
    return f"{shown} s"


class TokenEstimate(dict[str, float]):
    """Token counts over a period, keyed like the per-second estimate.

    A plain dict -- `est["total_tokens"]` works -- that prints as a table, because
    the numbers are large enough that the commas are what make them readable.

    Keys: `t_i` and `t_o` (fractions of the period spent prefilling and decoding),
    `prefill_seconds` / `decode_seconds`, and `input_tokens` / `output_tokens` /
    `cached_tokens` / `total_tokens` (counts). The per-second estimate it came
    from is kept in `rate`.
    """

    elapsed_seconds: float
    rate: dict[str, float]

    def __init__(self, counts: dict[str, float], elapsed_seconds: float, rate: dict[str, float]) -> None:
        super().__init__(counts)
        self.elapsed_seconds = elapsed_seconds
        self.rate = rate

    def to_string(self) -> str:
        """The printable table -- what printing the estimate shows."""
        counts = ("input_tokens", "output_tokens", "cached_tokens", "total_tokens")
        width = max(len(f"{self[key]:,.0f}") for key in counts)

        def row(label: str, key: str, rate_key: str, note: str = "") -> str:
            return f"  {label:<7}{self[key]:>{width},.0f} tok   ({self.rate[rate_key]:,.0f} tok/s{note})"

        cache_share = f"  -  cache {100 * self.get('t_cache', 0.0):.1f}%" if self.get("t_cache") else ""
        lines = [
            f"TokenEstimate over {_human_period(self.elapsed_seconds)}",
            f"  time    prefill {100 * self.get('t_prefill', self['t_i']):.1f}%{cache_share}  -  decode {100 * self['t_o']:.1f}%",
            row("input", "input_tokens", "input_tps", ", uncached"),
            row("output", "output_tokens", "output_tps"),
            row("cached", "cached_tokens", "cached_tps"),
            row("total", "total_tokens", "total_tps"),
        ]
        return "\n".join(lines)

    def __repr__(self) -> str:
        return self.to_string()

    __str__ = __repr__


def estimate_tokens(
    prefill_tps: float,
    decode_tps: float,
    input_rate: float = 0.85,
    cache_hit_rate: float = 0.85,
    cache_read_ratio: float = 0.02,
    seconds: float = 0,
    minutes: float = 0,
    hours: float = 0,
    days: float = 0,
    weeks: float = 0,
    months: float = 0,
    years: float = 0,
) -> TokenEstimate:
    """Token counts a replica serves over a period, from the per-second estimate.

    The duration arguments are additive, so `days=7, hours=12` works. Months are 30
    days and years 365, matching how capacity plans are usually written. With no
    duration given the period is one second.

    Args:
        prefill_tps: Prefill rate in uncached input tokens per second.
        decode_tps: Decode rate in output tokens per second.
        input_rate: Input share of total tokens, cached input included.
        cache_hit_rate: Share of input tokens served from cache.
        cache_read_ratio: Time a cached token costs relative to an uncached one.
        seconds: Duration component, in seconds.
        minutes: Duration component, in minutes.
        hours: Duration component, in hours.
        days: Duration component, in days.
        weeks: Duration component, in weeks.
        months: Duration component, in 30-day months.
        years: Duration component, in 365-day years.

    Returns:
        A TokenEstimate (a dict) with `t_i` / `t_o` as fractions of the period,
        `prefill_seconds` / `decode_seconds`, and the four token counts.

    Example:
        >>> est = estimate_tokens(11000, 900, years=1)
        >>> f"{est['total_tokens']:,}"
        '175,618,350,110'
        >>> round(est["t_i"], 3)
        0.072
    """
    rate = estimate_tokens_per_second(prefill_tps, decode_tps, input_rate, cache_hit_rate, cache_read_ratio)

    elapsed_seconds = sum(
        value * _SECONDS_PER[unit]
        for unit, value in {
            "seconds": seconds,
            "minutes": minutes,
            "hours": hours,
            "days": days,
            "weeks": weeks,
            "months": months,
            "years": years,
        }.items()
    )
    if elapsed_seconds <= 0:
        elapsed_seconds = 1.0

    counts = {
        "t_i": rate["t_i"],
        "t_o": rate["t_o"],
        "t_prefill": rate["t_prefill"],
        "t_cache": rate["t_cache"],
        "prefill_seconds": rate["t_prefill"] * elapsed_seconds,
        "cache_seconds": rate["t_cache"] * elapsed_seconds,
        "decode_seconds": rate["t_o"] * elapsed_seconds,
        # Counts are whole tokens; the fractions and durations above stay exact.
        "input_tokens": round(rate["input_tps"] * elapsed_seconds),
        "output_tokens": round(rate["output_tps"] * elapsed_seconds),
        "cached_tokens": round(rate["cached_tps"] * elapsed_seconds),
        "total_tokens": round(rate["total_tps"] * elapsed_seconds),
    }
    return TokenEstimate(counts, elapsed_seconds, rate)


def estimate_tokens_str(
    prefill_tps: float,
    decode_tps: float,
    input_rate: float = 0.85,
    cache_hit_rate: float = 0.85,
    cache_read_ratio: float = 0.02,
    seconds: float = 0,
    minutes: float = 0,
    hours: float = 0,
    days: float = 0,
    weeks: float = 0,
    months: float = 0,
    years: float = 0,
) -> str:
    """The printable table from estimate_tokens, as a string.

    Same arguments as estimate_tokens; useful when the output is going into a log, a
    report or a docstring rather than into a variable.

    Example:
        >>> print(estimate_tokens_str(11000, 900, days=1).splitlines()[0])
        TokenEstimate over 1 day (86,400 s)
    """
    return estimate_tokens(
        prefill_tps,
        decode_tps,
        input_rate,
        cache_hit_rate,
        cache_read_ratio,
        seconds=seconds,
        minutes=minutes,
        hours=hours,
        days=days,
        weeks=weeks,
        months=months,
        years=years,
    ).to_string()
