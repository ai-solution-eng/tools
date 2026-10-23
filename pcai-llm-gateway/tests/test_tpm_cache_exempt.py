"""Cache-aware TPM (rate_limit_exclude_cached_tokens).

The admission gate charges the key's minute window with the len//4
ESTIMATE; at settle time the real usage is known, including the upstream's
prompt_tokens_details.cached_tokens. Those cache-served tokens are refunded
from the window (conditional decrement, clamped at 0), so a high-cache-hit
deployment (a ~500k-token context re-sent every turn) stops 429ing on
tokens its upstream never actually prefilled.

Pinned here:
* an ok settle with cached_tokens subtracts exactly that many from the
  CURRENT minute window (same _window_start math as check_rate_limit);
* the decrement is CLAMPED to the window balance: a window holding fewer
  tokens than the reported cache hit is drained to zero rather than having
  the whole refund discarded (contract change 2026-10-21 - the old
  all-or-nothing form lost 56.6% of refunded tokens in production);
* a refused request releases its own token charge, so a 429 cannot
  compound the next one (the RPM counter is deliberately NOT released);
* a window that rolled over between admission and settle matches no row -
  the refund is dropped (discarded metric), never credited elsewhere;
* status='cache_hit' rows NEVER refund: the embeddings cache path settles a
  hit without ever running the admission gate, so there is no charge to
  refund - crediting one would mint window tokens from nothing;
* the feature flag off restores the legacy behavior (zero refund queries);
* a refund seam fault is non-fatal (settle still writes its ledger row).
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from pcai_llm.core import budgets as budgets_mod
from pcai_llm.core import costs as costs_mod
from pcai_llm.core import metrics as metrics_mod


class RefundCaptureDB:
    """Fake db recording the refund UPDATE and the settle INSERT by shape.
    The rate_windows state is a single (key_id, window_start) -> tokens
    entry; the UPDATE mirrors the production statement (an EXISTING row is
    decremented by the clamped amount, a missing row matches nothing) so
    rollover and clamp behavior are exercised against the same semantics as
    real Postgres."""

    def __init__(self, window_tokens: int | None = 5000) -> None:
        self.window_tokens = window_tokens  # None = no window row (rollover)
        self.refund_attempts: list[int] = []
        self.refund_results: list[bool] = []  # True = row matched/updated
        self.ledger_rows: list[tuple] = []

    async def fetchrow(self, sql: str, *a: Any):
        if "rate_windows" in sql and "UPDATE" in sql:
            tokens = a[2]
            self.refund_attempts.append(tokens)
            if self.window_tokens is None:
                # No live window row: UPDATE ... FROM cur matches nothing and
                # the refund is dropped (rollover). Never resurrected.
                self.refund_results.append(False)
                return None
            # CLAMPED partial refund: give back min(tokens, balance), never
            # negative, and never more than the window actually holds.
            prior = self.window_tokens
            self.window_tokens = max(0, prior - tokens)
            self.refund_results.append(True)
            return {"prior": prior, "applied": self.window_tokens}
        return None

    async def execute(self, sql: str, *a: Any):
        if "usage_ledger" in sql:
            self.ledger_rows.append(a)
        return "INSERT 0 1"

    async def fetch(self, sql: str, *a: Any):
        return []

    async def fetchval(self, sql: str, *a: Any):
        return None


class _NoDB:
    """Costs-seam stub: model_costs lookup returns None (row settles
    unpriced — cost NULL), matching the repo's unpriced-model test posture."""

    async def fetch(self, sql: str, *a: Any):
        return []

    async def fetchrow(self, sql: str, *a: Any):
        return None

    async def execute(self, sql: str, *a: Any):
        return "INSERT 0 1"

    async def fetchval(self, sql: str, *a: Any):
        return None


@pytest.fixture()
def flag_on(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(budgets_mod.settings, "rate_limit_exclude_cached_tokens", True)
    # settle() prices 'ok' rows through costs.compute — costs has its OWN db
    # seam (the same seam-splitting the repo's fakes already honor).
    monkeypatch.setattr(costs_mod, "db", _NoDB())
    return monkeypatch


@pytest.fixture()
def flag_off(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(budgets_mod.settings, "rate_limit_exclude_cached_tokens", False)
    return monkeypatch


async def test_ok_settle_refunds_cached_tokens(flag_on, monkeypatch) -> None:
    """An ok settle with cached_tokens subtracts exactly that many from the
    CURRENT minute window."""
    fake = RefundCaptureDB(window_tokens=10_000)
    monkeypatch.setattr(budgets_mod, "db", fake)
    kid = uuid.uuid4()
    await budgets_mod.settle(kid, "m", "chat", 9_000, 100, "ok", 5, "r1", cached_tokens=4_471)
    assert fake.refund_attempts == [4_471]
    assert fake.refund_results == [True]
    assert fake.window_tokens == 10_000 - 4_471
    assert len(fake.ledger_rows) == 1  # ledger write unaffected


async def test_refund_clamps_to_window_balance_not_dropped(flag_on, monkeypatch) -> None:
    """A window holding FEWER tokens than the reported cache hit is drained to
    zero, not left untouched.

    CONTRACT CHANGE (2026-10-21): this test previously pinned the
    all-or-nothing behaviour ("left untouched"), which discarded the WHOLE
    refund whenever the balance fell short. On a live deployment that threw
    away 56.6% of refunded tokens, because cached_tokens (the upstream real
    prompt size) routinely exceeds the admission charge (a len//4 estimate).
    The refund is now CLAMPED: give back what the window holds, no more.
    The safety property is unchanged - it can still never go negative and
    can still never mint tokens the window did not have."""
    fake = RefundCaptureDB(window_tokens=1_000)
    monkeypatch.setattr(budgets_mod, "db", fake)
    await budgets_mod.settle(uuid.uuid4(), "m", "chat", 900, 100, "ok", 5, "r", cached_tokens=4_471)
    assert fake.refund_attempts == [4_471]
    assert fake.refund_results == [True]
    assert fake.window_tokens == 0  # clamped: drained, never negative


async def test_window_rollover_drops_refund(flag_on, monkeypatch) -> None:
    """No live window row (rolled over, pruned) matches no row - the refund
    is discarded, never credited to a future window."""
    fake = RefundCaptureDB(window_tokens=None)
    monkeypatch.setattr(budgets_mod, "db", fake)
    await budgets_mod.settle(uuid.uuid4(), "m", "chat", 900, 100, "ok", 5, "r", cached_tokens=4_471)
    assert fake.refund_attempts == [4_471]
    assert fake.refund_results == [False]
    assert fake.ledger_rows, "ledger row still written"


async def test_cache_hit_status_never_refunds(flag_on, monkeypatch) -> None:
    """The embeddings cache path settles status='cache_hit' WITHOUT ever
    running the admission gate - refunding one would mint window tokens
    that were never charged (a rate-limit bypass)."""
    fake = RefundCaptureDB(window_tokens=10_000)
    monkeypatch.setattr(budgets_mod, "db", fake)
    await budgets_mod.settle(uuid.uuid4(), "m", "embeddings", 500, 0, "cache_hit", 5, "r", cached_tokens=500)
    assert fake.refund_attempts == []


async def test_zero_cached_tokens_no_refund_query(flag_on, monkeypatch) -> None:
    """No cache hits -> no refund round-trip (the common cold path pays
    nothing for the feature)."""
    fake = RefundCaptureDB(window_tokens=10_000)
    monkeypatch.setattr(budgets_mod, "db", fake)
    await budgets_mod.settle(uuid.uuid4(), "m", "chat", 900, 100, "ok", 5, "r", cached_tokens=0)
    assert fake.refund_attempts == []


async def test_error_status_refunds_too(flag_on, monkeypatch) -> None:
    """An upstream failure was charged at admission and consumed no cache -
    its cached_tokens (typically 0, but an upstream may report some) refund
    like any other settle."""
    fake = RefundCaptureDB(window_tokens=2_000)
    monkeypatch.setattr(budgets_mod, "db", fake)
    await budgets_mod.settle(uuid.uuid4(), "m", "chat", 0, 0, "error", 5, "r", cached_tokens=250)
    assert fake.refund_attempts == [250]
    assert fake.window_tokens == 1_750


async def test_flag_off_is_legacy_behavior(flag_off, monkeypatch) -> None:
    """Kill switch off: zero refund queries fire (byte-identical legacy
    settle)."""
    fake = RefundCaptureDB(window_tokens=10_000)
    monkeypatch.setattr(budgets_mod, "db", fake)
    await budgets_mod.settle(uuid.uuid4(), "m", "chat", 900, 100, "ok", 5, "r", cached_tokens=4_471)
    assert fake.refund_attempts == []
    assert len(fake.ledger_rows) == 1


async def test_refund_fault_is_nonfatal(flag_on, monkeypatch) -> None:
    """A refund seam fault must never eat the settle - the ledger row is the
    source of truth; the window correction is best-effort."""
    fake = RefundCaptureDB(window_tokens=10_000)

    async def boom(sql: str, *a: Any):
        if "rate_windows" in sql and "UPDATE" in sql:
            raise RuntimeError("db blip")

    async def execute_ok(sql: str, *a: Any):
        if "usage_ledger" in sql:
            fake.ledger_rows.append(a)
        return "INSERT 0 1"

    monkeypatch.setattr(
        budgets_mod,
        "db",
        type(
            "B",
            (),
            {
                "fetchrow": staticmethod(boom),
                "execute": staticmethod(execute_ok),
                "fetch": staticmethod(lambda *a: []),
                "fetchval": staticmethod(lambda *a: None),
            },
        )(),
    )
    await budgets_mod.settle(uuid.uuid4(), "m", "chat", 900, 100, "ok", 5, "r", cached_tokens=100)
    assert len(fake.ledger_rows) == 1


async def test_refund_metric_outcomes(flag_on, monkeypatch) -> None:
    """The observability counter records refunded vs discarded so refunds +
    discards reconcile against the ledger cached_tokens column."""
    calls: list[tuple[int, bool]] = []
    monkeypatch.setattr(
        metrics_mod, "observe_tpm_cache_refund", lambda t, discarded: calls.append((t, discarded)), raising=False
    )
    # budgets imports the module, so patch the name ON the budgets module too
    monkeypatch.setattr(
        budgets_mod._metrics, "observe_tpm_cache_refund", lambda t, discarded: calls.append((t, discarded))
    )
    fake = RefundCaptureDB(window_tokens=10_000)
    monkeypatch.setattr(budgets_mod, "db", fake)
    await budgets_mod.settle(uuid.uuid4(), "m", "chat", 900, 100, "ok", 5, "r1", cached_tokens=4_471)
    fake2 = RefundCaptureDB(window_tokens=None)
    monkeypatch.setattr(budgets_mod, "db", fake2)
    await budgets_mod.settle(uuid.uuid4(), "m", "chat", 900, 100, "ok", 5, "r2", cached_tokens=100)
    assert calls == [(4_471, False), (100, True)]


async def test_refund_targets_current_minute_window(flag_on, monkeypatch) -> None:
    """The refund UPDATE keys on the SAME window math as check_rate_limit
    (the current minute, day-anchored) - asserting the WHERE parameters so a
    drift between the two functions cannot ship silently."""
    seen: dict[str, Any] = {}

    async def fetchrow(sql: str, *a: Any):
        if "rate_windows" in sql and "UPDATE" in sql:
            seen["key_id"], seen["start"], seen["tokens"] = a
            return {"tokens": 1}
        return None

    async def execute(sql: str, *a: Any):
        return "INSERT 0 1"

    monkeypatch.setattr(
        budgets_mod,
        "db",
        type(
            "B",
            (),
            {
                "fetchrow": staticmethod(fetchrow),
                "execute": staticmethod(execute),
                "fetch": staticmethod(lambda *a: []),
                "fetchval": staticmethod(lambda *a: None),
            },
        )(),
    )
    kid = uuid.uuid4()
    await budgets_mod.settle(kid, "m", "chat", 10, 0, "ok", 1, "r", cached_tokens=7)
    assert seen["key_id"] == kid
    assert seen["tokens"] == 7
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    expected = budgets_mod._window_start(now, "day").replace(hour=now.hour, minute=now.minute)
    assert seen["start"] == expected


class RejectedChargeDB:
    """Fake db replaying the rate_windows upsert + rejected-charge release.

    Mirrors production: admission adds est_tokens unconditionally (the
    upsert must be atomic before the compare), and the release statement
    subtracts exactly what was added, clamped at zero."""

    def __init__(self) -> None:
        self.tokens = 0
        self.requests = 0
        self.released: list[int] = []

    async def fetchrow(self, sql: str, *a: Any):
        if "INSERT INTO rate_windows" in sql:
            est = int(a[2])
            self.requests += 1
            self.tokens += est
            return {"requests": self.requests, "tokens": self.tokens}
        return None

    async def execute(self, sql: str, *a: Any):
        if "UPDATE rate_windows" in sql:
            est = int(a[2])
            self.released.append(est)
            self.tokens = max(0, self.tokens - est)
        return "UPDATE 1"

    async def fetch(self, sql: str, *a: Any):
        return []

    async def fetchval(self, sql: str, *a: Any):
        return None


async def test_rejected_request_releases_its_token_charge(monkeypatch) -> None:
    """A REFUSED request must not leave its estimate in the window.

    The upsert charges before it can compare (it has to - a read-then-write
    would race), so without a release every 429 raises the odds of the next
    one for the rest of the minute. The RPM counter is NOT released: that is
    the anti-hammer axis."""
    fake = RejectedChargeDB()
    monkeypatch.setattr(budgets_mod, "db", fake)
    kid = uuid.uuid4()
    # 60k estimate against a 100k TPM: the first fits, the second does not.
    first = await budgets_mod.check_rate_limit(kid, rpm=60, tpm=100_000, est_tokens=60_000)
    assert first.ok
    assert fake.tokens == 60_000
    second = await budgets_mod.check_rate_limit(kid, rpm=60, tpm=100_000, est_tokens=60_000)
    assert not second.ok
    assert "TPM limit" in second.reason
    # The refusal gave back its own charge: the window is unchanged at 60k,
    # NOT 120k. Repeated refusals cannot compound.
    assert fake.tokens == 60_000, f"refusal must not compound the window, got {fake.tokens}"
    assert fake.released == [60_000]
    # The REQUEST counter still advanced twice - a 429 loop still burns RPM.
    assert fake.requests == 2


async def test_repeated_rejections_do_not_compound(monkeypatch) -> None:
    """A client looping on 429s stays at the same window balance instead of
    ratcheting it upward every attempt (the pre-fix behaviour)."""
    fake = RejectedChargeDB()
    monkeypatch.setattr(budgets_mod, "db", fake)
    kid = uuid.uuid4()
    await budgets_mod.check_rate_limit(kid, rpm=60, tpm=100_000, est_tokens=60_000)
    for _ in range(5):
        gate = await budgets_mod.check_rate_limit(kid, rpm=60, tpm=100_000, est_tokens=60_000)
        assert not gate.ok
        assert fake.tokens == 60_000, f"window ratcheted to {fake.tokens}"


async def test_release_failure_is_nonfatal(monkeypatch) -> None:
    """A release seam fault must not turn a 429 into a 500 - the refusal
    stands, and the window simply keeps the pre-fix charge."""

    class FlakyDB(RejectedChargeDB):
        async def execute(self, sql: str, *a: Any):
            if "UPDATE rate_windows" in sql:
                raise RuntimeError("db blip")
            return "INSERT 0 1"

    fake = FlakyDB()
    monkeypatch.setattr(budgets_mod, "db", fake)
    kid = uuid.uuid4()
    await budgets_mod.check_rate_limit(kid, rpm=60, tpm=100_000, est_tokens=60_000)
    gate = await budgets_mod.check_rate_limit(kid, rpm=60, tpm=100_000, est_tokens=60_000)
    assert not gate.ok, "the refusal must still stand"
    assert "TPM limit" in gate.reason


# ---------------------------------------------------------------------------
# Admission cache prediction (rate_limit_predict_cached)
# ---------------------------------------------------------------------------


class PredictionDB:
    """Fake db for the prediction path: serves a ledger history row for the
    measured cache ratio and replays the rate_windows arithmetic."""

    def __init__(self, cached: float = 780_000.0, prompt: float = 800_000.0) -> None:
        self.tokens = 0
        self.requests = 0
        self.peak = 0
        self.hist = {"cached": cached, "prompt": prompt}

    async def fetchrow(self, sql: str, *a: Any):
        if "INSERT INTO rate_windows" in sql:
            self.requests += 1
            self.tokens += int(a[2])
            self.peak = max(self.peak, self.tokens)
            return {"requests": self.requests, "tokens": self.tokens}
        if "WITH cur AS" in sql:
            prior = self.tokens
            self.tokens = max(0, prior - int(a[2]))
            return {"prior": prior, "applied": self.tokens}
        if "usage_ledger" in sql and "cached_tokens" in sql:
            return self.hist
        return None

    async def execute(self, sql: str, *a: Any):
        if "SET tokens = tokens + " in sql:
            self.tokens += int(a[2])
        elif "GREATEST" in sql:
            self.tokens = max(0, self.tokens - int(a[2]))
        return "ok"

    async def fetch(self, sql: str, *a: Any):
        return []

    async def fetchval(self, sql: str, *a: Any):
        return None


@pytest.fixture()
def predict_on(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(budgets_mod.settings, "rate_limit_predict_cached", True)
    monkeypatch.setattr(budgets_mod.settings, "rate_limit_predict_reconcile", True)
    budgets_mod._predict_cache.clear()
    budgets_mod._predict_charges.clear()
    yield monkeypatch
    budgets_mod._predict_cache.clear()
    budgets_mod._predict_charges.clear()


@pytest.fixture()
def predict_off(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(budgets_mod.settings, "rate_limit_predict_cached", False)
    budgets_mod._predict_cache.clear()
    budgets_mod._predict_charges.clear()
    yield monkeypatch
    budgets_mod._predict_cache.clear()
    budgets_mod._predict_charges.clear()


async def test_flag_off_charges_the_full_estimate(predict_off, monkeypatch) -> None:
    """The kill switch must be byte-identical to the legacy charge: a caller
    with a 97.5% cache ratio is still charged the FULL estimate."""
    fake = PredictionDB()
    monkeypatch.setattr(budgets_mod, "db", fake)
    await budgets_mod.check_rate_limit(uuid.uuid4(), rpm=60, tpm=100_000, est_tokens=808_192, request_id="r")
    assert fake.peak == 808_192, "flag off must charge the undiscounted estimate"
    assert budgets_mod._predict_charges == {}, "no charge recorded when prediction is off"


async def test_prediction_discounts_by_measured_ratio(predict_on, monkeypatch) -> None:
    """A key whose own ledger shows 97.5% cached is charged ~2.5% of the
    estimate at admission - the 800k-context case that used to 429 instantly."""
    fake = PredictionDB()
    monkeypatch.setattr(budgets_mod, "db", fake)
    gate = await budgets_mod.check_rate_limit(uuid.uuid4(), rpm=60, tpm=100_000, est_tokens=808_192, request_id="r")
    assert gate.ok, "a cache-served 800k context must be admitted at 100k TPM"
    assert fake.peak == 20_204, f"expected the measured-ratio charge, got {fake.peak}"


async def test_no_history_charges_full_estimate(predict_on, monkeypatch) -> None:
    """A brand-new key (no ledger rows) has no measured ratio: it pays the
    FULL estimate. Prediction must never hand an unknown key a discount."""
    fake = PredictionDB(cached=0.0, prompt=0.0)
    monkeypatch.setattr(budgets_mod, "db", fake)
    await budgets_mod.check_rate_limit(uuid.uuid4(), rpm=60, tpm=100_000, est_tokens=808_192, request_id="r")
    assert fake.peak == 808_192, "unknown key must not receive a cache discount"


async def test_reconciliation_charges_the_true_billable_total(predict_on, monkeypatch) -> None:
    """Settle brings the charge to the TRUE billable total, whatever the
    predicted ratio was: charged + (true_billable - charged) == true_billable."""
    fake = PredictionDB()
    monkeypatch.setattr(budgets_mod, "db", fake)
    kid = uuid.uuid4()
    await budgets_mod.check_rate_limit(kid, rpm=60, tpm=100_000, est_tokens=808_192, request_id="r1")
    await budgets_mod._reconcile_predicted_charge(kid, "r1", prompt_tokens=800_000, cached_tokens=780_000)
    assert fake.tokens == 20_000, f"expected the true billable 20k, got {fake.tokens}"


async def test_cache_busting_caller_pays_full_cost(predict_on, monkeypatch) -> None:
    """THE BYPASS GUARD: a caller with a high measured ratio who sends a FRESH
    prompt (a nonce defeats the prefix cache) is admitted cheaply but the
    true cost lands at settle, so the next call in the window is refused."""
    fake = PredictionDB()
    monkeypatch.setattr(budgets_mod, "db", fake)
    kid = uuid.uuid4()
    gate = await budgets_mod.check_rate_limit(kid, rpm=60, tpm=100_000, est_tokens=808_192, request_id="r1")
    assert gate.ok, "admitted cheaply on the predicted ratio"
    await budgets_mod._reconcile_predicted_charge(kid, "r1", prompt_tokens=800_000, cached_tokens=0)
    assert fake.tokens == 800_000, f"the real cost must land, got {fake.tokens}"
    budgets_mod._predict_cache.clear()
    again = await budgets_mod.check_rate_limit(kid, rpm=60, tpm=100_000, est_tokens=808_192, request_id="r2")
    assert not again.ok, "the bypass must be bounded to ONE cheap admission per window"


async def test_predicted_request_does_not_also_take_the_cache_refund(predict_on, monkeypatch) -> None:
    """The refund and the reconciliation must never BOTH adjust one request -
    that double-counts the cache. With prediction on, settle reconciles only."""
    fake = PredictionDB()
    monkeypatch.setattr(budgets_mod, "db", fake)
    monkeypatch.setattr(budgets_mod.settings, "rate_limit_exclude_cached_tokens", True)
    monkeypatch.setattr(costs_mod, "db", _NoDB())
    kid = uuid.uuid4()
    await budgets_mod.check_rate_limit(kid, rpm=60, tpm=100_000, est_tokens=808_192, request_id="r1")
    await budgets_mod.settle(kid, "m", "chat", 800_000, 100, "ok", 5, "r1", cached_tokens=780_000)
    assert fake.tokens == 20_000, f"double-counting the cache would drift this; got {fake.tokens}"


async def test_reconcile_without_recorded_charge_is_a_noop(predict_on, monkeypatch) -> None:
    """A settle whose admission charge was never recorded (prediction off at
    admission, or the entry expired) must not guess a delta."""
    fake = PredictionDB()
    fake.tokens = 5_000
    monkeypatch.setattr(budgets_mod, "db", fake)
    await budgets_mod._reconcile_predicted_charge(uuid.uuid4(), "unknown", 800_000, 780_000)
    assert fake.tokens == 5_000, "no recorded charge -> no adjustment"
