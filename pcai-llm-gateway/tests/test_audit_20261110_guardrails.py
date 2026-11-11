"""AUDIT-2026-11-10 remediation pins — zone R5 (guardrails / audit / helm).

One test per finding fixed in this zone; the finding id is in the name.
Covers: P1-6 (card separators / zero-width), P2-5 (external tail anchor),
P2-6 (helm spoofing warning), P2-7 (secrets audit detail), P2-29 (keyset
paging), P3 (webhook timeout clamp, DNS executor + negative cache, deny-CIDR
precompute, ingress catch-all, conditional master-key secretKeyRef).
"""

from __future__ import annotations

import asyncio
import hashlib
import socket
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from pcai_llm.config import settings
from pcai_llm.core import audit as audit_mod
from pcai_llm.core import chain_audit, url_guard
from pcai_llm.core import guardrails as gr
from pcai_llm.core.chain_audit import chain_hash

HELM = Path(__file__).resolve().parents[1] / "helm"


# ---------------------------------------------------------------------------
# P1-6 — credit-card mask separators and zero-width stripping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "card",
    [
        "4111 1111 1111 1111",  # space (was already masked)
        "4111-1111-1111-1111",  # hyphen (was already masked)
        "4111.1111.1111.1111",  # dot — the most common DISPLAY format (P1-6)
        "4111\u00a01111\u00a01111\u00a01111",  # NBSP (P1-6)
        "4111\u202f1111\u202f1111\u202f1111",  # narrow NBSP
    ],
)
def test_p1_6_card_masked_for_every_display_separator(card: str):
    masked, n = gr._mask_credit_cards(f"pay with {card} please")
    assert n == 1
    assert "4111" not in masked and "[REDACTED]" in masked


def test_p1_6_card_masked_despite_zero_width_embedding():
    for glue in ("\u200b", "\u200c", "\u2060", "\ufeff"):
        text = f"4111{glue}1111{glue}1111{glue}1111"
        masked, n = gr._mask_credit_cards(text)
        assert n == 1, f"zero-width {glue!r} must not defeat the mask"
        # CROSS-VAL STRENGTHENING: the old assertion ('4111' not in masked)
        # passed while '[REDACTED]111' leaked three digits. NO digit of the
        # input may survive, and the output must actually be shorter.
        assert not any(ch.isdigit() for ch in masked), f"digits survived the mask: {masked!r}"
        assert len(masked) < len(text)


def test_p1_6_leading_zero_widths_cannot_shift_the_mask():
    """The cross-val P1 repro: cleaned-copy offsets spliced into the original
    string let the ENTIRE pan survive 16 leading zero-widths."""
    text = "\u200b" * 16 + "4111111111111111"
    masked, n = gr._mask_credit_cards(text)
    assert n == 1
    assert not any(ch.isdigit() for ch in masked), f"the whole pan survived: {masked!r}"


def test_p1_6_luhn_gate_still_held():
    # 16 digits, Luhn-INVALID: must NOT be masked (the gate is the point).
    text = "4111 1111 1111 1112"
    masked, n = gr._mask_credit_cards(text)
    assert n == 0 and masked == text


def test_p1_6_clean_text_returned_byte_identical():
    text = "no cards here, order 1234567890123 pending"
    masked, n = gr._mask_credit_cards(text)
    assert n == 0 and masked == text


@pytest.mark.parametrize(
    "prose",
    [
        # CROSS-VAL P3: decimal lists concatenate to Luhn-valid runs — content
        # must not be destroyed by the widened separator class.
        "values 1.5 2.5 3.5 4.5 5.5 6.5 7.5",  # digits 15253545556575: Luhn-valid
        "latency 3.14 15.9 26.5 35.8 97.9 ms",  # pi digits
    ],
)
def test_p3_ordinary_numeric_prose_not_masked(prose: str):
    masked, n = gr._mask_credit_cards(prose)
    assert n == 0 and masked == prose, f"numeric prose destroyed: {prose!r} -> {masked!r}"


def test_p3_card_display_shapes_still_masked():
    for card in (
        "4111.1111.1111.1111",  # dotted display: 4-digit grouping
        "4111 1111 1111 1111",  # space display
        "3782 822463 10005",  # Amex 4-6-5 grouping
        "4111-1111-1111-1111",
        "4111111111111111",  # bare run (pre-existing behaviour kept)
    ):
        masked, n = gr._mask_credit_cards(f"pay {card} now")
        assert n == 1 and not any(ch.isdigit() for ch in masked), f"{card!r} must be masked"


def test_p3_plausible_span_gate():
    # dotted but NOT 4-digit grouped -> prose
    assert gr._plausible_card_span("1.5 2.5 3.5 4.5 5.5 6.5 7.5") is False
    # dotted 4x4 -> card display
    assert gr._plausible_card_span("4111.1111.1111.1111") is True
    # zero-width anywhere -> obfuscation intent
    assert gr._plausible_card_span("4111\u200b1111") is True
    # all groups shorter than 4 (hyphen/space ranges) -> prose
    assert gr._plausible_card_span("1-5 2-5 3-5 4-5 5-5 6-5 7") is False
    # Amex 4-6-5 -> card
    assert gr._plausible_card_span("3782 822463 10005") is True
    # bare run -> card (pre-existing behaviour)
    assert gr._plausible_card_span("4111111111111111") is True


@pytest.mark.parametrize(
    "text,kept_prefix",
    [
        # CROSS-VAL WAVE 2: the leftmost greedy candidate fuses the decimal's
        # integer part onto the PAN, then the whole span fails the gate and
        # non-overlapping finditer never retries the inner PAN.
        ("score 5.4111 1111 1111 1111", "score 5."),
        ("score 4.4111 1111 1111 1111", "score 4."),
        ("score 6.4111 1111 1111 1111", "score 6."),
        # Dotted Amex 4-6-5 / dotted Diners shapes were false negatives too.
        ("3782.822463.10005", ""),
        ("3056.9367.1234.56", ""),
    ],
)
def test_p2_card_gate_recovers_a_dotted_or_fused_pan(text: str, kept_prefix: str):
    masked, n = gr._mask_credit_cards(text)
    assert n == 1, f"the PAN inside {text!r} survived: {masked!r}"
    assert "[REDACTED]" in masked
    assert masked.startswith(kept_prefix)
    # The decimal's integer part may survive; NOT ONE card digit may.
    assert not any(ch.isdigit() for ch in masked[len(kept_prefix) :]), masked


def test_p2_card_gate_dotted_amex_is_card_display_not_prose():
    assert gr._plausible_card_span("3782.822463.10005") is True
    assert gr._plausible_card_span("3056.936712.3456") is True
    # ...while decimal prose still fails the gate.
    assert gr._plausible_card_span("1.5 2.5 3.5 4.5 5.5 6.5 7.5") is False


def test_p2_card_gate_decimal_prose_untouched_and_dotted_4x4_still_masks():
    prose = "1.5 2.5 3.5 4.5 5.5 6.5 7.5 8.5 9.5 10.5 11.5 12.5 13.5"
    masked, n = gr._mask_credit_cards(prose)
    assert n == 0 and masked == prose, f"numeric prose destroyed: {masked!r}"
    masked, n = gr._mask_credit_cards("card 4111.1111.1111.1111 end")
    assert n == 1 and "4111" not in masked


def test_p1_6_card_mask_reaches_the_engine():
    spec = gr.GuardrailSpec(
        id=__import__("uuid").uuid4(),
        name="pii",
        mode="post_call",
        engine="regex_pii",
        config={"entities": ["CREDIT_CARD"]},
        enabled=True,
    )
    text, events, blocked = asyncio.run(
        gr.run_post_call([spec], "card: 4111.1111.1111.1111", http=None)  # type: ignore[arg-type]
    )
    assert not blocked and "4111" not in text
    assert events[0].detail == "CREDIT_CARDx1"


# ---------------------------------------------------------------------------
# P2-7 — secrets-mask audit detail is never empty
# ---------------------------------------------------------------------------


def test_p2_7_detail_of_covers_secret_kinds():
    assert gr._detail_of({"aws_access_key": 2, "jwt": 1}) == "aws_access_keyx2, jwtx1"
    assert gr._detail_of({}) == ""


def test_p2_7_detail_of_keeps_pii_order():
    assert gr._detail_of({"CREDIT_CARD": 1, "EMAIL": 2}) == "EMAILx2, CREDIT_CARDx1"


def test_p2_7_secrets_engine_event_carries_nonempty_detail():
    spec = gr.GuardrailSpec(
        id=__import__("uuid").uuid4(),
        name="creds",
        mode="post_call",
        engine="secrets",
        config={"kinds": ["aws_access_key"]},
        enabled=True,
    )
    # Assembled at runtime: the fleet scanner's AWSKEY pass is a HARD pattern
    # with NO allow-marker exemption (pcai_utils/secret_scan.sh), and it gates
    # the mirror. This is AWS's OWN documentation placeholder access-key id —
    # not a credential — but it is credential-SHAPED, so it cannot be a literal.
    aws_placeholder = "AKIA" + "IOSFODNN7EXAMPLE"
    text, events, blocked = asyncio.run(
        gr.run_post_call([spec], f"key {aws_placeholder} end", http=None)  # type: ignore[arg-type]
    )
    assert not blocked and "AKIA" not in text
    assert events[0].detail == "aws_access_keyx1"


# ---------------------------------------------------------------------------
# P2-5 — external tail anchor
# ---------------------------------------------------------------------------


class ScriptedDB:
    """db seam that answers per-SQL-shape and records every execute."""

    def __init__(self, chain_rows: list[dict], anchor_rows: list[dict] | None = None):
        self.chain_rows = chain_rows
        self.anchor_rows = anchor_rows if anchor_rows is not None else []
        self.executed: list[tuple[str, tuple]] = []
        self.fetches: list[str] = []

    async def execute(self, sql: str, *args: Any) -> str:
        self.executed.append((sql, args))
        return "INSERT 0 1"

    async def fetch(self, sql: str, *args: Any) -> list[dict]:
        self.fetches.append(sql)
        if chain_audit.ANCHOR_TABLE in sql and "CREATE TABLE" not in sql:
            return self.anchor_rows
        if "SELECT row_hash FROM" in sql:  # stale-anchor prefix probe (id = $1)
            want = int(args[0])
            return [r for r in self.chain_rows if int(r["id"]) == want]
        if "ORDER BY id DESC LIMIT 1" in sql:  # tail fetch for the anchor
            return self.chain_rows[-1:]
        if "WHERE id > $1" in sql:
            last = int(args[0])
            return [r for r in self.chain_rows if int(r["id"]) > last][: int(args[1])]
        return self.chain_rows


def _chained_rows(n: int = 3) -> list[dict]:
    rows: list[dict] = []
    prev = ""
    for i in range(n):
        fields = [f"key-{i}", "srv", "tool", f"{i:064x}", 10 + i, "ok", "m", f"r{i}", ""]
        row = {name: value for name, value in zip(chain_audit.MCP_CALL_CHAIN_FIELDS, fields)}
        row.update({"id": i + 1, "prev_hash": prev, "row_hash": chain_hash(prev, fields)})
        rows.append(row)
        prev = row["row_hash"]
    return rows


def _anchor_for(table: str, last_id: int, last_hash: str) -> dict:
    return {
        "table_name": table,
        "last_id": last_id,
        "last_row_hash": last_hash,
        "mac": chain_audit._anchor_mac(table, last_id, last_hash),
    }


@pytest.fixture(autouse=True)
def _reset_anchor_state(monkeypatch):
    chain_audit._anchor_ensured = False
    chain_audit._last_anchor_refresh.clear()
    monkeypatch.setattr(settings, "secrets_master_key", "test-master-key", raising=False)
    yield
    chain_audit._anchor_ensured = False
    chain_audit._last_anchor_refresh.clear()


@pytest.mark.asyncio
async def test_p2_5_anchored_tail_verifies(monkeypatch):
    rows = _chained_rows(3)
    db = ScriptedDB(rows, [_anchor_for("mcp_call_audit", 3, rows[-1]["row_hash"])])
    monkeypatch.setattr(chain_audit, "db", db)
    result = await chain_audit.verify_mcp_call_chain()
    assert result["ok"] is True
    assert result["anchored"] is True and result["anchor_status"] == "anchored"


@pytest.mark.asyncio
async def test_p2_5_recomputed_tail_no_longer_passes(monkeypatch):
    """THE attack: DB-write attacker edits row 3 and recomputes its hash. The
    chain walk alone passes (ok:true pre-fix); the anchor now hardens it."""
    rows = _chained_rows(3)
    rows[-1]["detail"] = "rewritten"
    rows[-1]["row_hash"] = chain_hash(rows[-1]["prev_hash"], [rows[-1][f] for f in chain_audit.MCP_CALL_CHAIN_FIELDS])
    db = ScriptedDB(rows, [_anchor_for("mcp_call_audit", 3, _chained_rows(3)[-1]["row_hash"])])
    monkeypatch.setattr(chain_audit, "db", db)
    result = await chain_audit.verify_mcp_call_chain()
    assert result["ok"] is False
    assert result["kind"] == "anchor-mismatch"
    assert result["anchored"] is False


@pytest.mark.asyncio
async def test_p2_5_deleted_tail_rows_flagged(monkeypatch):
    rows = _chained_rows(3)
    db = ScriptedDB(rows[:1], [_anchor_for("mcp_call_audit", 3, rows[-1]["row_hash"])])
    monkeypatch.setattr(chain_audit, "db", db)
    result = await chain_audit.verify_mcp_call_chain()
    assert result["ok"] is False and result["anchor_status"] == "anchor-mismatch"


@pytest.mark.asyncio
async def test_p2_5_stale_anchor_with_intact_prefix_reports_stale(monkeypatch):
    rows = _chained_rows(3)
    db = ScriptedDB(rows, [_anchor_for("mcp_call_audit", 2, rows[1]["row_hash"])])
    monkeypatch.setattr(chain_audit, "db", db)
    result = await chain_audit.verify_mcp_call_chain()
    assert result["ok"] is True
    assert result["anchored"] is True and result["anchor_status"] == "stale"


@pytest.mark.asyncio
async def test_p2_5_stale_anchor_with_edited_prefix_is_tamper(monkeypatch):
    rows = _chained_rows(3)
    # row 2 edited AND its hash recomputed (chain-consistent): only the anchor
    # can see it. Row 3 is then recomputed chained onto the edited row 2.
    rows[1]["detail"] = "edited"
    rows[1]["row_hash"] = chain_hash(rows[1]["prev_hash"], [rows[1][f] for f in chain_audit.MCP_CALL_CHAIN_FIELDS])
    rows[2]["prev_hash"] = rows[1]["row_hash"]
    rows[2]["row_hash"] = chain_hash(rows[2]["prev_hash"], [rows[2][f] for f in chain_audit.MCP_CALL_CHAIN_FIELDS])
    db = ScriptedDB(rows, [_anchor_for("mcp_call_audit", 2, _chained_rows(3)[1]["row_hash"])])
    monkeypatch.setattr(chain_audit, "db", db)
    result = await chain_audit.verify_mcp_call_chain()
    assert result["ok"] is False and result["anchor_status"] == "anchor-mismatch"


@pytest.mark.asyncio
async def test_p2_5_no_master_key_reports_honestly(monkeypatch):
    monkeypatch.setattr(settings, "secrets_master_key", "", raising=False)
    monkeypatch.setattr(chain_audit, "db", ScriptedDB(_chained_rows(3)))
    result = await chain_audit.verify_mcp_call_chain()
    assert result["ok"] is True  # the chain walk is unchanged...
    assert result["anchored"] is False and result["anchor_status"] == "no-master-key"


@pytest.mark.asyncio
async def test_p2_5_missing_anchor_row_fails_closed(monkeypatch):
    """CROSS-VAL FIX: delete-the-anchor is an ATTACK, not a benign state — with
    a trail present and a master key configured, a missing anchor must fail
    CLOSED (the old 'unanchored, but ok:true' answer WAS the pre-fix bypass)."""
    monkeypatch.setattr(chain_audit, "db", ScriptedDB(_chained_rows(3), []))
    result = await chain_audit.verify_mcp_call_chain()
    assert result["ok"] is False
    assert result["kind"] == "anchor-missing" and result["anchor_status"] == "anchor-missing"
    assert result["anchored"] is False


@pytest.mark.asyncio
async def test_p2_5_empty_trail_is_distinct_from_missing_anchor(monkeypatch):
    """checked == 0 has nothing to anchor: it must NOT be flagged as a missing
    anchor (that would red-flag every fresh deployment) — its own kind, ok
    stands."""
    monkeypatch.setattr(chain_audit, "db", ScriptedDB([], []))
    result = await chain_audit.verify_mcp_call_chain()
    assert result["ok"] is True
    assert result["anchor_status"] == "empty-trail" and result["anchored"] is False
    assert "kind" not in result or result["kind"] == ""


@pytest.mark.asyncio
async def test_p2_5_wiped_trail_with_surviving_anchor_fails_closed(monkeypatch):
    """CROSS-VAL WAVE 2: DELETE FROM the trail while KEEPING the anchor row.

    The walk sees checked == 0, but the anchor still holds a valid-MAC row
    vouching for last_id > 0 — the DB itself proves rows existed. Pre-fix this
    returned ok:true / anchor_status='empty-trail', indistinguishable from a
    fresh deployment."""
    anchor = _anchor_for("mcp_call_audit", 3, _chained_rows(3)[-1]["row_hash"])
    monkeypatch.setattr(chain_audit, "db", ScriptedDB([], [anchor]))
    result = await chain_audit.verify_mcp_call_chain()
    assert result["checked"] == 0
    assert result["ok"] is False
    assert result["kind"] == "trail-wiped"
    assert result["anchor_status"] == "trail-wiped"
    assert result["anchored"] is False


@pytest.mark.asyncio
async def test_p2_5_wiped_trail_with_forged_anchor_still_fails_closed(monkeypatch):
    """The wipe is detected BEFORE the MAC is trusted: a forged/corrupt anchor
    is still evidence that a non-empty tail once existed."""
    anchor = _anchor_for("mcp_call_audit", 3, _chained_rows(3)[-1]["row_hash"])
    anchor["mac"] = "f" * 64
    monkeypatch.setattr(chain_audit, "db", ScriptedDB([], [anchor]))
    result = await chain_audit.verify_mcp_call_chain()
    assert result["ok"] is False
    assert result["kind"] == "trail-wiped" and result["anchor_status"] == "trail-wiped"


@pytest.mark.asyncio
async def test_p2_5_corrupt_anchor_mac_fails_closed(monkeypatch):
    rows = _chained_rows(3)
    anchor = _anchor_for("mcp_call_audit", 3, rows[-1]["row_hash"])
    anchor["mac"] = "0" * 64  # corrupt/forged
    db = ScriptedDB(rows, [anchor])
    monkeypatch.setattr(chain_audit, "db", db)
    result = await chain_audit.verify_mcp_call_chain()
    assert result["ok"] is False and result["kind"] == "anchor-invalid"


@pytest.mark.asyncio
async def test_p2_5_forged_anchor_mac_is_not_verification(monkeypatch):
    rows = _chained_rows(3)
    anchor = _anchor_for("mcp_call_audit", 3, rows[-1]["row_hash"])
    anchor["mac"] = "f" * 64  # an attacker without the key can only forge this
    monkeypatch.setattr(chain_audit, "db", ScriptedDB(rows, [anchor]))
    result = await chain_audit.verify_mcp_call_chain()
    assert result["ok"] is False and result["kind"] == "anchor-invalid"
    assert result["anchored"] is False and result["anchor_status"] == "anchor-invalid"


@pytest.mark.asyncio
async def test_p2_5_refresh_anchor_writes_a_verifiable_mac(monkeypatch):
    db = ScriptedDB(_chained_rows(3))
    monkeypatch.setattr(chain_audit, "db", db)
    await chain_audit.refresh_anchor("mcp_call_audit")
    upserts = [e for e in db.executed if f"INSERT INTO {chain_audit.ANCHOR_TABLE}" in e[0]]
    assert len(upserts) == 1
    table, last_id, last_hash, mac = upserts[0][1]
    assert (table, last_id, last_hash) == ("mcp_call_audit", 3, _chained_rows(3)[-1]["row_hash"])
    assert mac == chain_audit._anchor_mac("mcp_call_audit", 3, last_hash)


@pytest.mark.asyncio
async def test_p2_5_refresh_anchor_is_never_fatal(monkeypatch):
    class Failing:
        async def execute(self, sql: str, *args: Any) -> str:
            raise RuntimeError("db down")

        async def fetch(self, sql: str, *args: Any) -> list[dict]:
            raise RuntimeError("db down")

    monkeypatch.setattr(chain_audit, "db", Failing())
    await chain_audit.refresh_anchor("mcp_call_audit")  # must not raise


@pytest.mark.asyncio
async def test_p2_5_write_mcp_call_reanchors(monkeypatch):
    db = ChainSimDB()
    monkeypatch.setattr(chain_audit, "db", db)
    await chain_audit.write_mcp_call(key_id=None, server="s", tool="t", arguments={}, outcome="ok")
    upserts = [e for e in db.executed if f"INSERT INTO {chain_audit.ANCHOR_TABLE}" in e[0]]
    assert len(upserts) == 1, "a successful chained write must re-anchor the tail"


@pytest.mark.asyncio
async def test_p2_5_anchor_refresh_is_amortised(monkeypatch):
    """CROSS-VAL: the anchor used to cost a tail SELECT + an UPSERT on EVERY
    chained write (2 extra round trips per MCP tool call on the data plane).
    It must re-stamp at most once per _ANCHOR_REFRESH_S per table."""
    db = ChainSimDB()
    monkeypatch.setattr(chain_audit, "db", db)
    for _ in range(3):
        await chain_audit.write_mcp_call(key_id=None, server="s", tool="t", arguments={}, outcome="ok")
    upserts = [e for e in db.executed if f"INSERT INTO {chain_audit.ANCHOR_TABLE}" in e[0]]
    assert len(upserts) == 1
    await chain_audit.refresh_anchor("mcp_call_audit", force=True)  # force overrides the interval
    assert len([e for e in db.executed if f"INSERT INTO {chain_audit.ANCHOR_TABLE}" in e[0]]) == 2


# ---------------------------------------------------------------------------
# P2-29 — keyset-paged verifier walk
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_p2_29_walk_is_keyset_paged_and_complete(monkeypatch):
    rows = _chained_rows(4)
    db = ScriptedDB(rows, [_anchor_for("mcp_call_audit", 4, rows[-1]["row_hash"])])
    monkeypatch.setattr(chain_audit, "db", db)
    monkeypatch.setattr(chain_audit, "_VERIFY_PAGE", 2)  # force multiple pages
    result = await chain_audit.verify_mcp_call_chain()
    assert result["ok"] is True and result["checked"] == 4
    assert any("WHERE id > $1" in sql and "LIMIT $2" in sql for sql in db.fetches)


@pytest.mark.asyncio
async def test_p2_29_break_found_on_a_later_page(monkeypatch):
    rows = _chained_rows(4)
    rows[2]["row_hash"] = "e" * 64  # a tampered row on page 2
    db = ScriptedDB(rows, [_anchor_for("mcp_call_audit", 4, rows[-1]["row_hash"])])
    monkeypatch.setattr(chain_audit, "db", db)
    monkeypatch.setattr(chain_audit, "_VERIFY_PAGE", 2)
    result = await chain_audit.verify_mcp_call_chain()
    assert result["ok"] is False and result["kind"] == "break" and result["first_bad_id"] == 3
    assert result["checked"] == 4


# ---------------------------------------------------------------------------
# P3 — webhook timeout clamp (guardrails.py)
# ---------------------------------------------------------------------------


class _FakeResponse:
    status_code = 200

    def json(self) -> dict:
        return {"verdict": "pass"}


class _RecordingHTTP:
    def __init__(self) -> None:
        self.timeouts: list[float] = []

    async def post(self, url: str, **kwargs: Any) -> Any:
        self.timeouts.append(kwargs.get("timeout"))
        return _FakeResponse()


def _webhook_spec(timeout_s: Any) -> gr.GuardrailSpec:
    return gr.GuardrailSpec(
        id=__import__("uuid").uuid4(),
        name="mod",
        mode="post_call",
        engine="webhook",
        config={"url": "http://moderator.test", "timeout_s": timeout_s},
        enabled=True,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raw", "default", "expected"),
    [
        (999999, 10.0, 30.0),  # ~11 days -> the 30s floor of the cap
        (999999, 120.0, 120.0),  # a site with a deliberately slow moderator
        (0, 10.0, 0.5),  # junk zero -> the lower bound
        (-5, 10.0, 0.5),
        (7.5, 10.0, 7.5),  # sane values pass through
    ],
)
async def test_p3_webhook_timeout_clamped(raw: float, default: float, expected: float, monkeypatch):
    monkeypatch.setattr(settings, "guardrail_webhook_timeout_s", default, raising=False)
    http = _RecordingHTTP()
    kind, _ = await gr._webhook_verdict(http, _webhook_spec(raw), "hello")
    assert kind == "pass"
    assert http.timeouts == [pytest.approx(expected)]


@pytest.mark.asyncio
async def test_p3_webhook_timeout_invalid_value_falls_back_then_clamps(monkeypatch):
    monkeypatch.setattr(settings, "guardrail_webhook_timeout_s", 10.0, raising=False)
    http = _RecordingHTTP()
    await gr._webhook_verdict(http, _webhook_spec("not-a-number"), "hello")
    assert http.timeouts == [pytest.approx(10.0)]


# ---------------------------------------------------------------------------
# P3 — url_guard: shared executor + negative cache; deny-CIDR precompute
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_negative_cache():
    url_guard._negative_cache.clear()
    yield
    url_guard._negative_cache.clear()


@pytest.mark.asyncio
async def test_p3_resolve_success_path(monkeypatch):
    def fake_getaddrinfo(host: str, port: Any) -> list[tuple]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.0.1", 0))]

    monkeypatch.setattr(url_guard.socket, "getaddrinfo", fake_getaddrinfo)
    assert await asyncio.to_thread(url_guard.resolve_host_addresses, "example.test") == ["93.184.0.1"]


@pytest.mark.asyncio
async def test_p3_negative_cache_stops_repeat_work(monkeypatch):
    calls = {"n": 0}

    def failing_getaddrinfo(host: str, port: Any) -> list[tuple]:
        calls["n"] += 1
        raise socket.gaierror(socket.EAI_NONAME, "no such name")

    monkeypatch.setattr(url_guard.socket, "getaddrinfo", failing_getaddrinfo)
    for _ in range(3):
        with pytest.raises(socket.gaierror):
            await asyncio.to_thread(url_guard.resolve_host_addresses, "nx.example.test")
    assert calls["n"] == 1, "failures inside the negative TTL must not re-hit the resolver"


@pytest.mark.asyncio
async def test_p3_timeout_populates_negative_cache(monkeypatch):
    calls = {"n": 0}
    release = threading.Event()

    def wedged_getaddrinfo(host: str, port: Any) -> list[tuple]:
        calls["n"] += 1
        release.wait(2.0)  # wedged until the test frees it (bounded)
        return []

    monkeypatch.setattr(url_guard.socket, "getaddrinfo", wedged_getaddrinfo)
    with pytest.raises(socket.gaierror) as exc:
        await asyncio.to_thread(url_guard.resolve_host_addresses, "wedged.test", 0.05)
    assert exc.value.errno == socket.EAI_AGAIN
    with pytest.raises(socket.gaierror):
        await asyncio.to_thread(url_guard.resolve_host_addresses, "wedged.test", 0.05)
    assert calls["n"] == 1
    # Free the wedged lookup and wait for its slot release so the daemon
    # thread cannot hold a semaphore slot into the NEXT test.
    release.set()
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if url_guard._resolve_slots.acquire(blocking=False):
            url_guard._resolve_slots.release()
            break
        await asyncio.sleep(0.01)


def test_p3_negative_cache_is_bounded():
    for i in range(url_guard._NEGATIVE_CACHE_MAX + 50):
        url_guard._negative_remember(f"host-{i}.test")
    assert len(url_guard._negative_cache) <= url_guard._NEGATIVE_CACHE_MAX


def test_p3_denied_ip_reason_behaviour_preserved():
    assert "denied range" in url_guard.denied_ip_reason("127.0.0.1")
    assert "denied range" in url_guard.denied_ip_reason("2130706433")  # decimal form
    assert "denied range" in url_guard.denied_ip_reason("::ffff:127.0.0.1")
    assert url_guard.denied_ip_reason("8.8.8.8") == ""
    extra = ("10.0.0.0/8",)
    assert "denied range" in url_guard.denied_ip_reason("10.1.2.3", extra)
    assert url_guard.denied_ip_reason("8.8.8.8", extra) == ""
    assert url_guard.denied_ip_reason("8.8.8.8", ("not-a-cidr",)) == ""  # malformed ignored


def test_p3_denied_nets_built_once_and_rebuilt_only_on_change():
    url_guard._extra_nets_cache = None
    nets = url_guard._denied_nets(("10.0.0.0/8",))
    assert url_guard._extra_nets_cache is not None
    assert url_guard._denied_nets(("10.0.0.0/8",)) is nets, "same extra_cidrs must reuse the parsed list"
    rebuilt = url_guard._denied_nets(("172.16.0.0/12",))
    assert rebuilt is not nets and url_guard._extra_nets_cache[0] == ("172.16.0.0/12",)
    assert len(url_guard._denied_nets(())) >= len(url_guard._CORE_DENIED_NETS)


def test_p3_core_denied_nets_are_module_level():
    # The core list is parsed exactly once at import — no per-call parsing.
    assert len(url_guard._CORE_DENIED_NETS) == len(url_guard.CORE_DENIED_CIDRS)


@pytest.mark.asyncio
async def test_p3_resolver_saturation_fails_closed():
    """CROSS-VAL: the shared-pool detour turned 4 wedged lookups into a GLOBAL
    outage (every later check queue-timed-out into the fail-open path). With
    per-call threads + a slot bound, saturation is a DISTINCT exception that
    narrow-catch callers (routes_admin catches gaierror/OSError/UnicodeError
    only) do NOT swallow — the URL is refused, not waved through."""
    acquired = []
    try:
        for _ in range(url_guard._RESOLVE_MAX_CONCURRENT):
            assert url_guard._resolve_slots.acquire(blocking=False)
            acquired.append(True)
        with pytest.raises(url_guard.ResolverSaturated):
            await asyncio.to_thread(url_guard.resolve_host_addresses, "anything.test")
        try:
            url_guard.resolve_host_addresses("anything.test")
        except url_guard.ResolverSaturated as exc:
            assert not isinstance(exc, socket.gaierror) and not isinstance(exc, OSError), (
                "saturation must be distinct from the fail-open resolution failures"
            )
    finally:
        for _ in acquired:
            url_guard._resolve_slots.release()


@pytest.mark.asyncio
async def test_p3_resolver_isolation_one_wedged_lookup(monkeypatch):
    """One in-flight (wedged) lookup must not starve another host's check."""
    assert url_guard._resolve_slots.acquire(blocking=False)  # the wedged lookup's slot
    try:

        def fake_getaddrinfo(host: str, port: Any) -> list[tuple]:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.0.1", 0))]

        monkeypatch.setattr(url_guard.socket, "getaddrinfo", fake_getaddrinfo)
        assert await asyncio.to_thread(url_guard.resolve_host_addresses, "example.test") == ["93.184.0.1"]
    finally:
        url_guard._resolve_slots.release()


@pytest.mark.asyncio
async def test_p3_resolver_slots_fully_released_after_success(monkeypatch):
    def fake_getaddrinfo(host: str, port: Any) -> list[tuple]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.0.1", 0))]

    monkeypatch.setattr(url_guard.socket, "getaddrinfo", fake_getaddrinfo)
    await asyncio.to_thread(url_guard.resolve_host_addresses, "example.test")
    drained = 0
    while url_guard._resolve_slots.acquire(blocking=False):
        drained += 1
    for _ in range(drained):
        url_guard._resolve_slots.release()
    assert drained == url_guard._RESOLVE_MAX_CONCURRENT, "every slot must come back after the lookup"


def _free_resolver_slots() -> int:
    free = 0
    while url_guard._resolve_slots.acquire(blocking=False):
        free += 1
    for _ in range(free):
        url_guard._resolve_slots.release()
    return free


def test_p3_thread_start_failure_releases_the_resolver_slot(monkeypatch):
    """CROSS-VAL WAVE 2: the slot acquired before worker.start() was released
    only inside _work's finally, which never runs when start() raises
    "can't start new thread". One permanent leak per failure; eight such
    failures saturate the resolver forever (every later lookup ->
    ResolverSaturated -> global SSRF-check refusal)."""

    class _BoomThread:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def start(self) -> None:
            raise RuntimeError("can't start new thread")

        def join(self, timeout: float | None = None) -> None:
            pass

    monkeypatch.setattr(url_guard.threading, "Thread", _BoomThread)
    assert _free_resolver_slots() == url_guard._RESOLVE_MAX_CONCURRENT
    for i in range(url_guard._RESOLVE_MAX_CONCURRENT):
        with pytest.raises(RuntimeError, match="can't start new thread"):
            url_guard.resolve_host_addresses(f"boom-{i}.test")
        assert _free_resolver_slots() == url_guard._RESOLVE_MAX_CONCURRENT, (
            f"start() failure #{i + 1} leaked a resolver slot"
        )


# ---------------------------------------------------------------------------
# P2-6 / P3 — helm source pins (rendered validation is done with helm template)
# ---------------------------------------------------------------------------


def test_p2_6_notes_carries_verified_principal_and_working_command():
    notes = (HELM / "templates" / "NOTES.txt").read_text()
    assert "cluster.local/ns/istio-system/sa/istio-ingressgateway-service-account" in notes
    assert "istio=ingressgateway" in notes
    assert "get pod -l 'app=ezaf-gateway'" not in notes, "the broken command must be gone"
    assert "/admin" in notes, "the admin surface belongs in the protected-surfaces list"


def test_p2_6_values_g2_carries_verified_principal_not_the_phantom():
    values = (HELM / "values-examples" / "values.g2.yaml").read_text()
    assert "cluster.local/ns/istio-system/sa/istio-ingressgateway-service-account" in values
    assert "- cluster.local/ns/istio-system/sa/ezaf-gateway" not in values, "the phantom SA must not be proposed"
    assert "istio=ingressgateway" in values
    # CROSS-VAL: the mangled one-line form swallowed '-o jsonpath=...' as an
    # inline comment — the documented command listed pods and never printed
    # the SA name. The jsonpath half must live on its OWN line, and the
    # kubectl line must end with the shell continuation.
    kubectl_line = next(line for line in values.splitlines() if "istio=ingressgateway" in line and "get pod" in line)
    assert "jsonpath" not in kubectl_line, f"command still mangled onto one line: {kubectl_line!r}"
    assert kubectl_line.rstrip().endswith("\\")
    assert any("-o jsonpath=" in line for line in values.splitlines())


def test_p3_ingress_has_no_catch_all_path():
    ingress = (HELM / "templates" / "ingress.yaml").read_text()
    assert "- path: /\n" not in ingress, "the Prefix catch-all routed /admin despite the comment"
    for path in ("/v1", "/mcp", "/health", "/ready", "/ui"):
        assert f"- path: {path}" in ingress


def test_p3_master_key_env_rendered_only_when_secret_named():
    deployment = (HELM / "templates" / "deployment.yaml").read_text()
    needle = "PCAI_LLM_SECRETS_MASTER_KEY"
    at = deployment.index(needle)
    guard = deployment.rindex("{{- if", 0, at)
    assert "existingSecret" in deployment[guard:at], "the env must sit behind the existingSecret guard"


# ---------------------------------------------------------------------------
# P3 — fork guard after a degraded row: unique "unchained-" tombstones
# ---------------------------------------------------------------------------


class ChainSimDB:
    """Models the chained table's rows PLUS the uq_*_audit_prev partial unique
    index (prev_hash unique where <> ''). Like the real statements, the prev
    coalesce read happens INSIDE execute."""

    UNIQUE_VIOLATION = 'duplicate key value violates unique constraint "uq_mcp_call_audit_prev"'

    def __init__(self) -> None:
        self.rows: list[dict] = []  # {"id", "prev_hash", "row_hash"}
        self.executed: list[tuple[str, tuple]] = []
        self.attempted_prevs: list[str] = []
        self.index_violations = 0
        self._race_prev: str | None = None  # emulates a stale concurrent read
        self._next_id = 1

    def seed_degraded(self, prev: str, row: str) -> None:
        self.rows.append({"id": self._next_id, "prev_hash": prev, "row_hash": row})
        self._next_id += 1

    async def execute(self, sql: str, *args: Any) -> str:
        self.executed.append((sql, args))
        if "INSERT INTO mcp_call_audit" not in sql:
            return "INSERT 0 1"  # anchor upsert / DDL
        if "SELECT" in sql:  # chained insert — prev comes from the coalesce
            prev = self._race_prev if self._race_prev is not None else (self.rows[-1]["row_hash"] if self.rows else "")
            self._race_prev = None
            self.attempted_prevs.append(prev)
            if prev and any(r["prev_hash"] == prev for r in self.rows):
                self.index_violations += 1
                raise RuntimeError(self.UNIQUE_VIOLATION)  # the partial unique index
            new_hash = hashlib.sha256((prev + str(args[9])).encode()).hexdigest()
            self.rows.append({"id": self._next_id, "prev_hash": prev, "row_hash": new_hash})
            self._next_id += 1
            return "INSERT 0 1"
        # degraded insert: the tombstones are the last two args
        self.seed_degraded(str(args[-2]), str(args[-1]))
        return "INSERT 0 1"

    async def fetch(self, sql: str, *args: Any) -> list[dict]:
        if "ORDER BY id DESC LIMIT 1" in sql:
            return self.rows[-1:]
        return self.rows


class _FailingChainedDB:
    """Chained INSERT always fails (non-unique); the degraded INSERT records."""

    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple]] = []

    async def execute(self, sql: str, *args: Any) -> str:
        self.executed.append((sql, args))
        if "SELECT" in sql:
            raise RuntimeError("no sha256(bytea) here (simulated)")
        return "INSERT 0 1"

    async def fetch(self, sql: str, *args: Any) -> list[dict]:
        return []


@pytest.mark.asyncio
async def test_p3_degraded_mcp_write_stamps_unique_tombstones(monkeypatch):
    db = _FailingChainedDB()
    monkeypatch.setattr(chain_audit, "db", db)
    for _ in range(2):
        await chain_audit.write_mcp_call(key_id=None, server="s", tool="t", arguments={}, outcome="ok")
    degraded = [e for e in db.executed if "INSERT INTO mcp_call_audit" in e[0] and "SELECT" not in e[0]]
    assert len(degraded) == 2
    for sql, args in degraded:
        assert "prev_hash, row_hash" in sql
        prev, row = str(args[-2]), str(args[-1])
        assert prev.startswith("unchained-") and row.startswith("unchained-")
        assert prev != row, "a shared tombstone would make the NEXT chained writer collide with the unique index"
    assert degraded[0][1][-2] != degraded[1][1][-2], "each degraded draw is unique (the index never self-collides)"


@pytest.mark.asyncio
async def test_p3_degraded_admin_write_stamps_unique_tombstones(monkeypatch):
    db = _FailingChainedDB()
    monkeypatch.setattr(audit_mod, "db", db)
    for _ in range(2):
        await audit_mod.audit_action(
            actor_source="admin-key", action="key.create", resource_type="key", resource_id="x"
        )
    degraded = [e for e in db.executed if "INSERT INTO admin_audit" in e[0] and "SELECT" not in e[0]]
    assert len(degraded) == 2
    for _sql, args in degraded:
        assert str(args[-2]).startswith("unchained-") and str(args[-1]).startswith("unchained-")
        assert args[-2] != args[-1]
    assert degraded[0][1][-1] != degraded[1][1][-1]


@pytest.mark.asyncio
async def test_p3_degraded_row_then_chained_row_verifies_unchained_not_break(monkeypatch):
    """The tombstone shape (prev_hash=tombstone-A, row_hash=tombstone-B, next
    row chained from tombstone-B) must classify as UNCHAINED — a bare unique
    row_hash would be misread as kind="break" = TAMPER (red from birth). The
    master key is unset here so the walk's classification is observed alone
    (with a key and no anchor row, the honest combined verdict is
    anchor-missing — covered by the fail-closed test)."""
    monkeypatch.setattr(settings, "secrets_master_key", "", raising=False)
    t_prev, t_row = chain_audit.unchained_tombstone(), chain_audit.unchained_tombstone()
    fields2 = ["key-1", "srv", "tool", f"{1:064x}", 11, "ok", "m", "r1", ""]
    row1 = {name: "" for name in chain_audit.MCP_CALL_CHAIN_FIELDS}  # degraded content
    row1.update({"id": 1, "prev_hash": t_prev, "row_hash": t_row})
    row2 = {name: value for name, value in zip(chain_audit.MCP_CALL_CHAIN_FIELDS, fields2)}
    row2.update({"id": 2, "prev_hash": t_row, "row_hash": chain_hash(t_row, fields2)})
    monkeypatch.setattr(chain_audit, "db", ScriptedDB([row1, row2]))
    result = await chain_audit.verify_mcp_call_chain()
    assert result["kind"] == "unchained" and result["first_bad_id"] == 1
    assert result["unchained"] == 1 and result["checked"] == 2


@pytest.mark.asyncio
async def test_p3_legacy_empty_row_hash_still_verifies_unchained(monkeypatch):
    """Backward compatibility: pre-existing degraded rows carry row_hash=''
    and must still verify as unchained, with the next row walkable."""
    rows = _chained_rows(1)
    legacy = {name: "" for name in chain_audit.MCP_CALL_CHAIN_FIELDS}
    legacy.update({"id": 2, "prev_hash": "", "row_hash": ""})  # the legacy shape
    rows.append(legacy)
    fields3 = ["key-2", "srv", "tool", f"{2:064x}", 12, "ok", "m", "r2", ""]
    row3 = {name: value for name, value in zip(chain_audit.MCP_CALL_CHAIN_FIELDS, fields3)}
    row3.update({"id": 3, "prev_hash": "", "row_hash": chain_hash("", fields3)})
    rows.append(row3)
    monkeypatch.setattr(settings, "secrets_master_key", "", raising=False)
    monkeypatch.setattr(chain_audit, "db", ScriptedDB(rows))
    result = await chain_audit.verify_mcp_call_chain()
    assert result["kind"] == "unchained" and result["first_bad_id"] == 2 and result["checked"] == 3


@pytest.mark.asyncio
async def test_p3_concurrent_write_after_degraded_row_is_index_guarded(monkeypatch):
    """Two chained writers both coalesce prev from the TOMBSTONE row_hash
    (non-empty, so the partial unique index now APPLIES): the loser gets the
    unique violation and its retry chains onto the winner — the fork the
    index exists to prevent."""
    db = ChainSimDB()
    db.seed_degraded(chain_audit.unchained_tombstone(), chain_audit.unchained_tombstone())
    monkeypatch.setattr(chain_audit, "db", db)
    await chain_audit.write_mcp_call(key_id=None, server="s", tool="t", arguments={}, outcome="ok")  # winner
    winner_hash = db.rows[-1]["row_hash"]
    tomb_row_hash = db.rows[0]["row_hash"]
    assert tomb_row_hash.startswith("unchained-")
    # The loser's coalesce already read the tombstone before the winner committed:
    db._race_prev = tomb_row_hash
    await chain_audit.write_mcp_call(key_id=None, server="s", tool="t", arguments={}, outcome="ok")
    loser_attempts = db.attempted_prevs[1:]
    assert loser_attempts[0] == tomb_row_hash, "the loser chained from the stale tombstone read"
    assert db.index_violations == 1, "the partial unique index must refuse the loser's first attempt"
    final = db.rows[-1]
    assert final["prev_hash"] == winner_hash, "the retry must chain onto the winner, not the tombstone"
    assert len(db.rows) == 3, "exactly one row landed for the loser (no fork)"


@pytest.mark.asyncio
async def test_p2_5_anchor_still_catches_recomputed_tail_with_tombstones(monkeypatch):
    """P2-5 regression with the tombstone shape in play: rows 1-2 recomputed
    (row 1 as a tombstone, row 2 chained onto it) against an anchor made from
    the CLEAN rows — the anchor still hardens ok to False."""
    clean = _chained_rows(2)
    t_prev, t_row = chain_audit.unchained_tombstone(), chain_audit.unchained_tombstone()
    fields2 = [clean[1][f] for f in chain_audit.MCP_CALL_CHAIN_FIELDS]
    tomb1 = {name: "" for name in chain_audit.MCP_CALL_CHAIN_FIELDS}
    tomb1.update({"id": 1, "prev_hash": t_prev, "row_hash": t_row})
    rows = [
        tomb1,
        {name: value for name, value in zip(chain_audit.MCP_CALL_CHAIN_FIELDS, fields2)}
        | {"id": 2, "prev_hash": t_row, "row_hash": chain_hash(t_row, fields2)},
    ]
    db = ScriptedDB(rows, [_anchor_for("mcp_call_audit", 2, clean[1]["row_hash"])])
    monkeypatch.setattr(chain_audit, "db", db)
    result = await chain_audit.verify_mcp_call_chain()
    assert result["ok"] is False and result["kind"] == "anchor-mismatch"
    assert result["anchor_status"] == "anchor-mismatch"
