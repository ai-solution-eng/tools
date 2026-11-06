"""AUDIT-r5 remediation pins for the guardrails engine — findings #3 and #7.

Engine semantics broadly live in test_guardrails_core.py; this file pins
the two AUDIT-r5 P1 findings against the exact regressions they describe,
the round-2 cross-validation defects on the #3 fix (keyword-side U+0130
under-masking; the decline-path latency cliff), the round-3 defect (the
fold must cover every re._casefix equivalence pair and the deny side
must fold the same way), the round-4 defect (the fold must close over
UPPERCASE forms too), the missing-table degradation net, and the
boundary semantics the #7 fix introduced. Pure functions only — no live
Postgres, no async loop.
"""

from __future__ import annotations

import re
import time

import pytest

from pcai_llm.core.guardrails import (
    MASK,
    _first_keyword_hit,
    _fold,
    _mask_by_spans,
    _mask_keywords,
    _mask_pii,
)

IZ = "\u0130"  # Turkish dotted capital I — the one .lower() lengthener
ID = "\u0131"  # dotless i — re.IGNORECASE-equivalent to 'i', 1:1 in lower()
LS = "\u017f"  # long s — re.IGNORECASE-equivalent to 's', 1:1 in lower()
SG = "\u03c3"  # sigma
SF = "\u03c2"  # final sigma — re.IGNORECASE-equivalent to sigma
SU = "\u03a3"  # capital sigma — upper form; the fold must close over it


def _reference_mask(text: str, keywords: list[str]) -> tuple[str, int]:
    """The sequential loop, kept verbatim as the oracle. Do not 'fix' this."""
    count = 0
    for kw in keywords:
        text, n = re.subn(re.escape(kw), MASK, text, flags=re.IGNORECASE)
        count += n
    return text, count


# --- AUDIT-r5 #3: the span pass must search what re.IGNORECASE matches ----


def test_mask_spans_fold_turkish_dotted_i_in_text() -> None:
    """20 U+0130 before a secret used to shift every span right by 20 and
    ship the secret verbatim while redacting an unrelated tail. The span
    pass folds 'İ' to 'i' (length-preserving, matching re.IGNORECASE), so
    it now masks correctly instead of declining to the loop."""
    text = IZ * 20 + " ACME-SECRET-1 and more"
    fast = _mask_by_spans(text, ("acme-secret-1",))
    assert fast == (IZ * 20 + f" {MASK} and more", 1)
    assert _mask_keywords(text, ["acme-secret-1"]) == _reference_mask(text, ["acme-secret-1"])
    assert "ACME-SECRET-1" not in fast[0]


def test_mask_keywords_keyword_side_turkish_dotted_i_matches_loop() -> None:
    """Round 2, defect A (P2 SEC, under-masking): a keyword that
    re.IGNORECASE matches but str.lower() cannot collapse used to sail
    through the span pass unmasked while the ledger recorded a mask —
    ["İstanbul", "secret"] over "istanbul secret" masked 1 instead of 2.
    Every re-equivalent spelling must mask exactly like the oracle."""
    for kws, text in [
        ([IZ + "stanbul", "secret"], "istanbul secret"),  # the executed defect
        ([ID + "stanbul"], "Istanbul"),
        ([LS + "ecret", "other"], "The \u017fecret other thing"),
    ]:
        assert _mask_keywords(text, kws) == _reference_mask(text, kws), (text, kws)
    out, count = _mask_keywords("istanbul secret", [IZ + "stanbul", "secret"])
    assert (out, count) == (f"{MASK} {MASK}", 2)


def test_fold_is_length_preserving_and_ignorecase_exact() -> None:
    """The fold's two contracts: it never changes a length (offsets stay
    valid against the original text) and it collapses exactly the pairs
    re.IGNORECASE matches (CPython's re case-fix table). Pinned against
    the re module so a future interpreter change surfaces here."""
    for s in ("", "İstanbul", IZ * 3 + "ABC ß 你好 😀 " + ID + LS):
        assert len(_fold(s)) == len(s)
    assert _fold(IZ) == "i"
    family = ["i", "I", IZ, ID]
    for a in family:
        for b in family:
            assert re.fullmatch(re.escape(a), b, re.IGNORECASE), (a, b)
            assert _fold(a) == _fold(b), (a, b)


def test_fold_covers_the_interpreters_casefix_table() -> None:
    """Round 3: the fold must cover EVERY literal-equivalence pair
    re.IGNORECASE adds over .lower() — iterate the interpreter's own
    table and assert pairwise fold equality (this is the pin that
    catches a future interpreter shipping new pairs)."""
    import re as _re

    _casefix = getattr(_re, "_casefix", None)  # private + untyped: getattr, not import
    extra = getattr(_casefix, "_EXTRA_CASES", None) or {}
    assert extra, "interpreter lost re._casefix._EXTRA_CASES — fold degraded to plain .lower()"
    for k, v in extra.items():
        members = [int(x) for x in (v if isinstance(v, (tuple, list)) else (v,))]
        for m in members:
            assert _fold(chr(int(k))) == _fold(chr(m)), (hex(int(k)), hex(m))
            # round 4: the class must also contain each member's single-
            # char upper — the check whose absence hid the Σ/σ/ς split.
            up = chr(m).upper()
            if len(up) == 1:
                assert _fold(chr(m)) == _fold(up), (hex(m), hex(ord(up)))
    # U+0130 is not in the table (2-char lower); sre equates it to 'i'.
    assert _fold(IZ) == _fold("i")


def test_mask_keywords_greek_final_sigma_matches_loop() -> None:
    """Round 3, executed defect: 'ς' and 'σ' are re.IGNORECASE-equivalent
    but neither .lower() nor the round-2 fold collapsed them, so
    ["ς", "secret"] over "σ secret" masked 1 instead of 2. The
    table-driven fold must mask both, exactly like the oracle."""
    kws = [SF, "secret"]
    out, count = _mask_keywords(SG + " secret", kws)
    assert (out, count) == (f"{MASK} {MASK}", 2)
    assert _mask_keywords(SG + " secret", kws) == _reference_mask(SG + " secret", kws)


def test_mask_keywords_uppercase_sigma_matches_loop() -> None:
    """Round 4, executed defect: the fold was not a closure over
    UPPERCASE forms — fold('Σ') took plain-lower 'σ' while fold('σ')
    took the table root 'ς', so ["Σ", "secret"] over "σ secret" masked 1
    instead of 2 and the deny side passed. Upper forms now join the
    class; mask and deny must agree with the oracle."""
    kws = [SU, "secret"]
    out, count = _mask_keywords(SG + " secret", kws)
    assert (out, count) == (f"{MASK} {MASK}", 2)
    assert _mask_keywords(SG + " secret", kws) == _reference_mask(SG + " secret", kws)


def test_mask_keywords_final_sigma_context_matches_loop() -> None:
    """Round 5, executed defect: str.lower()'s CONTEXTUAL Greek
    final-sigma rule lowered a word-final 'Σ' to 'ς' (not 'σ'), so the
    fold image of 'Σ' depended on the NEXT character and a keyword 'σ'
    missed it — while re.IGNORECASE matches case per character. The fold
    must be context-free and agree with the oracle in both contexts."""
    text = SG + SF + SU + "+\u0130c"  # sigma forms before cased and uncased chars
    kws = [SG]
    out, count = _mask_keywords(text, kws)
    assert (out, count) == _reference_mask(text, kws)
    assert count == 3


def test_first_keyword_hit_uses_the_same_fold() -> None:
    """Round 3, second site: the deny decision folded with .lower() only,
    so a single-keyword 'ς' policy never hit 'σ' text and PASSED what
    the mask engine would redact. Deny and mask must agree."""
    assert _first_keyword_hit([SF], ["plain " + SG + " text"]) == SF
    # round 4: keyword 'Σ' folds via plain lower to 'σ', which is not the
    # class root — it must still hit.
    assert _first_keyword_hit([SU], ["plain " + SG + " text"]) == SU
    assert _first_keyword_hit(["secret"], ["plain text"]) is None


def test_missing_fold_table_degrades_to_the_exact_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """The safety net for a table-less interpreter had no pin: with the
    translate table emptied, plain .lower() LENGTHENS 'İsecret', so the
    span pass must DECLINE (None) and _mask_keywords must fall back to
    the sequential loop and equal the oracle. Pin the DECLINE only — a
    table-less interpreter still under-masks exotic pairs (see _fold)."""
    monkeypatch.setattr("pcai_llm.core.guardrails._FOLD_TRANS", {})
    assert _mask_by_spans(IZ + "secret", ("secret",)) is None
    assert _mask_keywords(IZ + "secret", ["secret"]) == _reference_mask(IZ + "secret", ["secret"])


def test_mask_fast_path_survives_fold_on_a_large_prompt() -> None:
    """Round 2, defect B (P2 PERF/DoS): declining the span pass on a
    length anomaly turned the O(keywords x text) loop into an
    attacker-triggerable cliff — one 'İ' on a 1.5 MB prompt with 50
    keywords cost 573 ms of event-loop stall (338 ms on the re-measured
    tree). Folding keeps the fast path on; the 250 ms bound is generous
    (measured ~14 ms)."""
    keywords = [f"keyword{i:02d}" for i in range(50)]
    for label, text in [
        ("one 'İ' prepended", IZ + "x" * 1_499_999),
        ("one 'ς' prepended (round 3)", SF + "x" * 1_499_999),
    ]:
        t0 = time.perf_counter()
        out, count = _mask_keywords(text, keywords)
        elapsed = time.perf_counter() - t0
        assert count == 0 and out == text
        assert elapsed < 0.25, f"fast path lost to the fold ({label}): {elapsed:.2f}s"


# --- AUDIT-r5 #7: bounded email scan (unchanged from round 1) -------------


def test_email_scan_stays_linear_on_a_long_unmatched_run() -> None:
    """AUDIT-r5 #7 (P1 PERF): an unbounded local part made the email scan
    quadratic on texts with no match — a 100k-char 'a' prompt (just inside
    the pre_call length cap) stalled the event loop for ~8.4 s. Bounded
    local/domain runs keep the sweep linear. The 250 ms bound is generous
    so slow CI cannot flake; the pre-fix number was ~8.4 s."""
    text = "a" * 100_000
    t0 = time.perf_counter()
    out, counts = _mask_pii(text, {"EMAIL"})
    elapsed = time.perf_counter() - t0
    assert counts == {} and out == text  # nothing matched, nothing changed
    assert elapsed < 0.25, f"email scan went superlinear again: {elapsed:.2f}s"


def test_email_bounds_keep_real_address_semantics() -> None:
    """The {1,64} local / {1,255} domain bounds must not move matching for
    real addresses: dotted and plus-tagged local parts, multi-label
    domains and case-insensitivity still mask; a dotless domain still
    passes through; exactly-64-char local parts (the RFC 5321 maximum)
    still mask whole."""
    out, counts = _mask_pii("mail a.b+c@sub.example.co.uk and Bob.Smith@Example.COM", {"EMAIL"})
    assert counts == {"EMAIL": 2}
    assert "a.b+c@sub.example.co.uk" not in out
    assert "Bob.Smith@Example.COM" not in out

    out2, counts2 = _mask_pii("reach user@localhost or nobody", {"EMAIL"})
    assert counts2 == {} and "user@localhost" in out2

    out3, counts3 = _mask_pii("x" * 64 + "@ex.com", {"EMAIL"})
    assert counts3 == {"EMAIL": 1} and MASK in out3 and "x" * 64 not in out3


def test_email_overlong_local_part_degrades_without_stalling() -> None:
    """A local part longer than RFC 5321's 64 octets is not a real
    address; the bounded pattern still masks a (tail-window) match so
    junk input degrades gracefully instead of matching nothing or
    stalling — pin the count so a future rewrite keeps that property."""
    out, counts = _mask_pii("x" * 65 + "@ex.com", {"EMAIL"})
    assert counts == {"EMAIL": 1} and MASK in out
