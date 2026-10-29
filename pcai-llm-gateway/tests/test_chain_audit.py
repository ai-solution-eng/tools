"""Hash-chained audit trail (2026-10-26 hardening plan P2-2).

Pins the properties the trail is worth having for:
  * the chain arithmetic is deterministic and field-order sensitive;
  * the MCP trail stores an arguments DIGEST, never the arguments;
  * a tampered / deleted / forked row is REPORTED, not silently accepted;
  * a broken audit write never fails the operation it describes.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest

from pcai_llm.core import audit as audit_mod
from pcai_llm.core import chain_audit
from pcai_llm.core.chain_audit import (
    ADMIN_AUDIT_CHAIN_FIELDS,
    args_digest,
    canonical,
    chain_hash,
    write_mcp_call,
)


class RowsFake:
    """db seam: records the INSERT, and serves a canned row list for verify."""

    def __init__(self, rows: list[dict] | None = None, fail: bool = False) -> None:
        self.rows = rows or []
        self.fail = fail
        self.executed: list[tuple[str, tuple]] = []

    async def execute(self, sql: str, *args: Any) -> str:
        self.executed.append((sql, args))
        if self.fail:
            raise RuntimeError("db down (simulated)")
        return "INSERT 0 1"

    async def fetch(self, sql: str, *args: Any) -> list[dict]:
        return self.rows


def test_canonical_normalises_none_and_joins():
    assert canonical([None, 1, "x", ""]) == "|1|x|"
    assert canonical([]) == ""


def test_chain_hash_is_deterministic_and_order_sensitive():
    a = chain_hash("", ["k", "s", "t"])
    assert a == chain_hash("", ["k", "s", "t"])
    assert a != chain_hash("", ["t", "s", "k"])
    assert a != chain_hash("prev", ["k", "s", "t"])
    assert len(a) == 64 and int(a, 16) >= 0


def test_chain_links_across_rows():
    h1 = chain_hash("", ["row1"])
    h2 = chain_hash(h1, ["row2"])
    assert h2 == chain_hash(h1, ["row2"])
    assert h2 != chain_hash("", ["row2"])  # the link is load-bearing


def test_args_digest_is_deterministic_and_never_echoes_payload():
    payload = {"url": "http://example.test/?d=SECRETBLOB", "n": 2}
    d1, n1 = args_digest(payload)
    d2, n2 = args_digest({"n": 2, "url": "http://example.test/?d=SECRETBLOB"})
    assert (d1, n1) == (d2, n2), "key order must not change the digest"
    assert "SECRETBLOB" not in d1
    assert n1 > 0
    assert d1 == hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def test_args_digest_survives_unserialisable_payloads():
    d, n = args_digest({"obj": object()})
    assert len(d) == 64 and n > 0


@pytest.mark.asyncio
async def test_write_mcp_call_records_digest_not_arguments(monkeypatch):
    fake = RowsFake()
    monkeypatch.setattr(chain_audit, "db", fake)
    await write_mcp_call(
        key_id="11111111-1111-1111-1111-111111111111",
        server="searxng",
        tool="fetch_content",
        arguments={"url": "http://169.254.169.254/latest/meta-data/"},
        outcome="policy_blocked",
        model="Deepseek-V4.1-Flash-1017",
        request_id="req-1",
        detail="url-destination",
    )
    sql, args = fake.executed[0]
    assert "INSERT INTO mcp_call_audit" in sql
    assert "prev_hash" in sql and "row_hash" in sql
    flat = json.dumps([str(a) for a in args])
    assert "169.254.169.254" not in flat, "the trail must not store the arguments"
    assert args[3] == args_digest({"url": "http://169.254.169.254/latest/meta-data/"})[0]
    assert args[5] == "policy_blocked"
    assert args[6] == "Deepseek-V4.1-Flash-1017"


@pytest.mark.asyncio
async def test_write_mcp_call_is_never_fatal(monkeypatch):
    monkeypatch.setattr(chain_audit, "db", RowsFake(fail=True))
    await write_mcp_call(
        key_id=None, server="s", tool="t", arguments={}, outcome="ok"
    )  # must not raise


@pytest.mark.asyncio
async def test_write_mcp_call_bounds_caller_controlled_text(monkeypatch):
    fake = RowsFake()
    monkeypatch.setattr(chain_audit, "db", fake)
    await write_mcp_call(
        key_id=None,
        server="s" * 5000,
        tool="t" * 5000,
        arguments={},
        outcome="ok",
        model="m" * 5000,
        request_id="r" * 5000,
        detail="d" * 5000,
    )
    _, args = fake.executed[0]
    for idx in (1, 2, 6, 7, 8):
        assert len(str(args[idx])) == 200, f"arg {idx} not bounded"


def _chained_rows(n: int = 3) -> list[dict]:
    rows: list[dict] = []
    prev = ""
    for i in range(n):
        fields = [f"key-{i}", "srv", "tool", f"{i:064x}", 10 + i, "ok", "m", f"r{i}", ""]
        h = chain_hash(prev, fields)
        row = {name: value for name, value in zip(chain_audit.MCP_CALL_CHAIN_FIELDS, fields)}
        row.update({"id": i + 1, "prev_hash": prev, "row_hash": h})
        rows.append(row)
        prev = h
    return rows


@pytest.mark.asyncio
async def test_verify_mcp_call_chain_ok(monkeypatch):
    monkeypatch.setattr(chain_audit, "db", RowsFake(_chained_rows(3)))
    result = await chain_audit.verify_mcp_call_chain()
    assert result["ok"] is True
    assert result["checked"] == 3


@pytest.mark.asyncio
async def test_verify_reports_a_tampered_row(monkeypatch):
    rows = _chained_rows(3)
    rows[1]["outcome"] = "ok"  # unchanged
    rows[1]["row_hash"] = "f" * 64  # the row_hash no longer matches its fields
    monkeypatch.setattr(chain_audit, "db", RowsFake(rows))
    result = await chain_audit.verify_mcp_call_chain()
    assert result["ok"] is False
    assert result["first_bad_id"] == 2
    assert result["kind"] == "break"


@pytest.mark.asyncio
async def test_verify_distinguishes_a_fork_from_a_break(monkeypatch):
    rows = _chained_rows(2)
    # a third row chained from the SAME prev as row 2 (two concurrent writers)
    fork_prev = rows[0]["row_hash"]
    fields = ["key-9", "srv", "tool", f"{9:064x}", 19, "ok", "m", "r9", ""]
    row = {name: value for name, value in zip(chain_audit.MCP_CALL_CHAIN_FIELDS, fields)}
    row.update({"id": 3, "prev_hash": fork_prev, "row_hash": chain_hash(fork_prev, fields)})
    rows.append(row)
    monkeypatch.setattr(chain_audit, "db", RowsFake(rows))
    result = await chain_audit.verify_mcp_call_chain()
    assert result["ok"] is False
    assert result["first_bad_id"] == 3
    assert result["kind"] == "fork-or-legacy"


@pytest.mark.asyncio
async def test_verify_labels_a_degraded_row_as_unchained(monkeypatch):
    """A row written by the never-fatal fallback (no row_hash) must be
    reported as kind="unchained" - not as tamper ("break"), and not as a
    fork. The operator's remedy is different for each."""
    rows = _chained_rows(2)
    fields = ["key-9", "srv", "tool", f"{9:064x}", 19, "ok", "m", "r9", ""]
    row = {name: value for name, value in zip(chain_audit.MCP_CALL_CHAIN_FIELDS, fields)}
    row.update({"id": 3, "prev_hash": "", "row_hash": ""})  # the degraded shape
    rows.append(row)
    monkeypatch.setattr(chain_audit, "db", RowsFake(rows))
    result = await chain_audit.verify_mcp_call_chain()
    assert result["ok"] is False
    assert result["first_bad_id"] == 3
    assert result["kind"] == "unchained"


@pytest.mark.asyncio
async def test_verify_does_not_flag_an_empty_chain(monkeypatch):
    monkeypatch.setattr(chain_audit, "db", RowsFake([]))
    result = await chain_audit.verify_mcp_call_chain()
    assert result["ok"] is True and result["checked"] == 0



@pytest.mark.asyncio
async def test_c5_edit_after_a_degraded_row_is_reported_as_break(monkeypatch):
    """C5 — a REAL edit must not be labelled "unchained" because a degraded row
    happens to precede it.

    PRE-FIX (_verify returned at the first mismatching row): the trail
    [ok, ok, degraded(row_hash=''), EDITED] returned
    {"kind": "unchained", "first_bad_id": 3} — the operator's only verdict was
    "fix the schema", and the tampered row 4 was never examined.
    """
    rows = _chained_rows(2)
    # row 3: the degraded shape (a never-fatal unchained write)
    fields3 = ["key-9", "srv", "tool", f"{9:064x}", 19, "ok", "m", "r9", ""]
    row3 = {name: value for name, value in zip(chain_audit.MCP_CALL_CHAIN_FIELDS, fields3)}
    row3.update({"id": 3, "prev_hash": "", "row_hash": ""})
    rows.append(row3)
    # row 4: chained correctly from the degraded row's '' hash, then EDITED
    fields4 = ["key-4", "srv", "tool", f"{4:064x}", 14, "ok", "m", "r4", ""]
    row4 = {name: value for name, value in zip(chain_audit.MCP_CALL_CHAIN_FIELDS, fields4)}
    row4.update({"id": 4, "prev_hash": "", "row_hash": chain_hash("", fields4)})
    row4["outcome"] = "policy_blocked"  # the edit: content no longer matches row_hash
    rows.append(row4)
    monkeypatch.setattr(chain_audit, "db", RowsFake(rows))

    result = await chain_audit.verify_mcp_call_chain()
    assert result["ok"] is False
    assert result["kind"] == "break", "a real edit must be reported as tamper, not as a degraded write"
    assert result["first_bad_id"] == 4
    assert result["unchained"] == 1, "the degraded row is still counted (it is not tamper, but it is not clean)"
    assert result["checked"] == 4, "the walk must reach the edited row (it used to stop at the degraded one)"


@pytest.mark.asyncio
async def test_c5_degraded_row_still_walks_past_itself(monkeypatch):
    """A degraded row followed by CONSISTENT rows keeps the trail's later rows
    verifiable: the verdict is still "unchained" (unchanged contract) and the
    count is reported, so the operator sees the whole picture."""
    rows = _chained_rows(2)
    fields3 = ["key-9", "srv", "tool", f"{9:064x}", 19, "ok", "m", "r9", ""]
    row3 = {name: value for name, value in zip(chain_audit.MCP_CALL_CHAIN_FIELDS, fields3)}
    row3.update({"id": 3, "prev_hash": "", "row_hash": ""})
    rows.append(row3)
    fields4 = ["key-4", "srv", "tool", f"{4:064x}", 14, "ok", "m", "r4", ""]
    row4 = {name: value for name, value in zip(chain_audit.MCP_CALL_CHAIN_FIELDS, fields4)}
    row4.update({"id": 4, "prev_hash": "", "row_hash": chain_hash("", fields4)})
    rows.append(row4)
    monkeypatch.setattr(chain_audit, "db", RowsFake(rows))

    result = await chain_audit.verify_mcp_call_chain()
    assert result["ok"] is False
    assert result["kind"] == "unchained"
    assert result["first_bad_id"] == 3
    assert result["unchained"] == 1
    assert result["checked"] == 4, "the consistent row after the degraded one must have been checked"


@pytest.mark.asyncio
async def test_c5_clean_trail_reports_zero_unchained(monkeypatch):
    """The ok shape gains the count (additive — consumers read .get("ok"))."""
    monkeypatch.setattr(chain_audit, "db", RowsFake(_chained_rows(3)))
    result = await chain_audit.verify_mcp_call_chain()
    assert result["ok"] is True and result["unchained"] == 0 and result["checked"] == 3


@pytest.mark.asyncio
async def test_c5_break_outranks_an_earlier_fork(monkeypatch):
    """Severity order: a tampered row is reported even when a benign fork
    (the accepted concurrency residual) sits earlier in the trail."""
    rows = _chained_rows(2)
    # row 3: a FORK (chained from row 1's hash, internally consistent)
    fork_prev = rows[0]["row_hash"]
    fields3 = ["key-9", "srv", "tool", f"{9:064x}", 19, "ok", "m", "r9", ""]
    row3 = {name: value for name, value in zip(chain_audit.MCP_CALL_CHAIN_FIELDS, fields3)}
    row3.update({"id": 3, "prev_hash": fork_prev, "row_hash": chain_hash(fork_prev, fields3)})
    rows.append(row3)
    # row 4: a real edit
    fields4 = ["key-4", "srv", "tool", f"{4:064x}", 14, "ok", "m", "r4", ""]
    row4 = {name: value for name, value in zip(chain_audit.MCP_CALL_CHAIN_FIELDS, fields4)}
    row4.update({"id": 4, "prev_hash": row3["row_hash"], "row_hash": chain_hash(row3["row_hash"], fields4)})
    row4["detail"] = "rewritten"
    rows.append(row4)
    monkeypatch.setattr(chain_audit, "db", RowsFake(rows))

    result = await chain_audit.verify_mcp_call_chain()
    assert result["ok"] is False
    assert result["kind"] == "break" and result["first_bad_id"] == 4


@pytest.mark.asyncio
async def test_admin_audit_insert_carries_the_chain(monkeypatch):
    fake = RowsFake()
    monkeypatch.setattr(audit_mod, "db", fake)
    await audit_mod.audit_action(
        actor_key_id=7,
        actor_subject="svc",
        actor_source="admin-key",
        action="key.create",
        resource_type="key",
        resource_id="abc",
        details={"name": "k", "secret": "should-be-redacted"},
        request_id="req-9",
        source_ip="10.0.0.1",
    )
    sql, args = fake.executed[0]
    assert "details_sha256" in sql and "prev_hash" in sql and "row_hash" in sql
    assert len(args) == 11
    # args[6] is the redacted JSON; args[9] its digest; args[10] the chain tail
    assert "should-be-redacted" not in str(args[6])
    assert args[9] == hashlib.sha256(str(args[6]).encode()).hexdigest()
    assert str(args[10]).startswith("|")
    expected_fields = canonical(
        [7, "svc", "admin-key", "key.create", "key", "abc", args[9], "req-9", "10.0.0.1"]
    )
    assert args[10] == "|" + expected_fields
    assert ADMIN_AUDIT_CHAIN_FIELDS == [
        "actor_key_id", "actor_subject", "actor_source", "action",
        "resource_type", "resource_id", "details_sha256", "request_id", "source_ip",
    ]


@pytest.mark.asyncio
async def test_admin_audit_write_failure_is_never_fatal(monkeypatch):
    monkeypatch.setattr(audit_mod, "db", RowsFake(fail=True))
    await audit_mod.audit_action(
        actor_source="admin-key",
        action="key.create",
        resource_type="key",
        resource_id="abc",
    )  # must not raise