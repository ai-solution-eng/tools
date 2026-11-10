"""Hash-chained audit helpers (2026-10-26 hardening plan P2-2).

WHY: applygate's audit file is hash-chained and verifies ok live (check it
any time with the applygate verify_audit_chain tool - the count moves, the
mechanism does not). The GATEWAY's own record - which key/model asked for which tool with
which arguments - was plain rows in Postgres: an operator (or anyone who
gets the DB) could edit or delete them and nothing would notice. For the
exfiltration attack class this is the record that matters, so it is the one
that gets tamper evidence.

TWO TRAILS, ONE SHAPE (db/schema.py holds the DDL and the full rationale):
  * admin_audit    - control plane; chained inside core.audit.audit_action.
  * mcp_call_audit - the MCP tool calls themselves; written by write_mcp_call
                     from the gate chain and the relay executor.

CHAIN SHAPE: row_hash = sha256(prev_hash || "|" || canonical_row_text).
The first row chains from "" (the empty prev). canonical_row_text is the
pipe-joined field list with None normalised to "" - so a field REORDER or a
type change is a chain break, which is the point.

RESIDUAL (stated, not hidden): the prev_hash is read in the same statement
that inserts, so two CONCURRENT writers can both chain from the same prev -
a FORK, not a tamper. verify_*() reports a fork distinctly from a break, and
both admin mutations and MCP tool calls are low-volume relative to chat, so
this is an accepted residual rather than a locking hot path.

EXTERNAL TAIL ANCHOR (P2-5, audit 2026-11-10): the chain alone is
tamper-evident only against an attacker who cannot rewrite the trail - one
WITH DB write access edits row N and recomputes (prev_hash, row_hash) for
every later row, and verify passes as ok:true. The anchor closes that class:
after each successful chained write the tail's (last id, last row_hash) is
HMAC'd with a key derived from the secrets master key (the same
PCAI_LLM_SECRETS_MASTER_KEY core.secretbox consumes - it lives in the pod
env, never in the DB the attacker holds) and stored in audit_chain_anchor,
a table OUTSIDE both chained trails. verify() compares the walked tail
against the anchor and reports anchor-mismatch (ok:false) when a valid
anchor no longer vouches for the trail. Without a master key the check
degrades to today's chain-only behaviour and reports anchor_status
"no-master-key" - honestly, not as verification. Residual: rows appended
AFTER the last anchor are covered only once the next write re-anchors
(reported as anchor_status "stale", with the anchored prefix still checked).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
import uuid
from typing import Any

from ..config import settings
from ..db import db

log = logging.getLogger(__name__)


def is_unique_violation(exc: BaseException) -> bool:
    """True when exc is a UNIQUE-constraint violation (the fork-guard race).

    Matched by class name AND message so the module does not have to import
    asyncpg (chain_audit is used by tests that fake the db seam entirely).
    """
    if type(exc).__name__ in ("UniqueViolationError", "IntegrityError"):
        return True
    text = str(exc).lower()
    return "duplicate key value" in text or "unique constraint" in text


def canonical(parts: list[Any]) -> str:
    """Pipe-joined, None-normalised, str()-ed field list."""
    return "|".join("" if p is None else str(p) for p in parts)


def chain_hash(prev_hash: str, parts: list[Any]) -> str:
    """row_hash for one row: sha256 over prev_hash + the canonical row text."""
    material = (prev_hash or "") + "|" + canonical(parts)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def unchained_tombstone() -> str:
    """A UNIQUE "unchained-<uuid4>" tombstone for the degraded write path.

    P3 (audit 2026-11-10): the degraded path used to leave BOTH chain columns
    NULL/'', which (a) exempted the row from the uq_*_audit_prev partial
    unique index (WHERE prev_hash <> '') — so two concurrent chained writes
    after a degraded row both chained from prev='' with NO guard — and (b) was
    the only reason the index could not simply cover '' (every legacy
    pre-chain row carries prev_hash='').

    Each degraded write draws TWO independent tombstones (prev and row_hash):
    a single shared value would self-collide — the very next chained writer
    coalesces prev from THIS row's row_hash, and a prev_hash already present
    in the table is exactly what the unique index refuses. Distinct draws
    restore the fork guard with no schema change; the "unchained-" prefix
    keeps the row classifiable as UNCHAINED by the verifier (a bare unique
    hash would be misread as kind="break" = tamper)."""
    return "unchained-" + uuid.uuid4().hex


def args_digest(arguments: Any) -> tuple[str, int]:
    """(sha256 hex, byte length) of the arguments payload.

    The trail stores the DIGEST, never the arguments: it must answer "what was
    asked" without becoming a second copy of the payload - and without storing
    whatever the model may have been trying to exfiltrate. Deterministic JSON
    (sort_keys, no spaces) so the same call always digests the same.
    """
    try:
        blob = json.dumps(arguments, sort_keys=True, separators=(",", ":"), default=str)
    except Exception:  # broad-by-design: a digest must never fail a tool call
        try:
            blob = repr(arguments)
        except Exception:  # a hostile __repr__ must not escape either
            blob = "<undigestable>"
    raw = blob.encode("utf-8", "replace")
    return hashlib.sha256(raw).hexdigest(), len(raw)


# NOTE (Python 3.14.6 parser gotcha, MEASURED twice on 2026-10-26 - once in a
# minimal repro, once against this module): a BARE "*" with NO trailing comma,
# alone on its own line, followed by the parameter list, silently DROPS the
# first keyword-only parameter from the signature. Exact repro (fails):
#     async def f(\n    *\n    key_id,\n    server: str,\n) -> None: ...
#   -> ast kwonlyargs == ['server']          (key_id becomes a LOCAL)
# Both of these work (ast kwonlyargs == ['key_id', 'server']):
#     async def f(\n    *,\n    key_id,\n    server: str,\n) -> None: ...
#     async def f(*, key_id, server: str) -> None: ...
# So the "*," form is fine and the bare "*" form is not. Keep this signature
# on one line - do not reformat it.
async def write_mcp_call(
    *,
    key_id: Any,
    server: str,
    tool: str,
    arguments: Any,
    outcome: str,
    model: str = "",
    request_id: str = "",
    detail: str = "",
) -> None:
    """Append ONE chained mcp_call_audit row. Best-effort BY CONTRACT: an
    audit failure logs loudly and never fails the tool call it describes
    (the same tradeoff core.audit documents at length - a missing trail line
    must not turn a successful tool call into a reported failure)."""
    try:
        digest, size = args_digest(arguments)
    except Exception:  # never-fatal: a digest fault must not fail the call
        log.exception("mcp_call_audit: could not digest arguments (row will record an empty digest)")
        digest, size = "", 0
    chained_args = (
        key_id,
        server[:200],
        tool[:200],
        digest,
        size,
        outcome,
        (model or "")[:200],
        (request_id or "")[:200],
        (detail or "")[:200],
        "|"
        + canonical(
            [
                key_id,
                server[:200],
                tool[:200],
                digest,
                size,
                outcome,
                (model or "")[:200],
                (request_id or "")[:200],
                (detail or "")[:200],
            ]
        ),
    )
    # FORK GUARD (wave-2 review P2-3): prev_hash is read INSIDE the inserting
    # statement, so two concurrent writers would both chain from the same prev
    # and the trail would FORK - which the verifier correctly reports as
    # not-ok, making the tamper detector red from birth on a busy gateway.
    # The schema now carries a partial UNIQUE index on prev_hash, so the loser
    # of that race gets a unique violation instead; re-running the statement
    # re-reads prev and chains onto the winner. Three attempts is generous: a
    # retry only loses when three writers land in the same instant.
    last_exc: BaseException | None = None
    for _attempt in range(3):
        try:
            await db.execute(
                """
                INSERT INTO mcp_call_audit
                    (key_id, server, tool, args_sha256, args_bytes, outcome, model,
                     request_id, detail, prev_hash, row_hash)
                SELECT $1, $2, $3, $4, $5, $6, $7, $8, $9, p.prev_hash,
                       encode(sha256(convert_to(p.prev_hash || $10, 'UTF8')), 'hex')
                  FROM (SELECT coalesce((SELECT row_hash FROM mcp_call_audit
                                          ORDER BY id DESC LIMIT 1), '') AS prev_hash) p
                """,
                *chained_args,
            )
            # P2-5: re-anchor the tail (never fatal — refresh_anchor swallows
            # its own failures). Skipped on the degraded path below: an
            # unchained tail row is already reported as kind="unchained".
            await refresh_anchor("mcp_call_audit")
            return
        except Exception as exc:
            last_exc = exc
            if not is_unique_violation(exc):
                break
    try:
        if last_exc is None:  # pragma: no cover - defensive
            return
        raise last_exc
    except Exception:
        # DEGRADED PATH, deliberate: the chain is an ENHANCEMENT to a trail
        # that must exist. If the chained INSERT cannot run (a database
        # without the built-in sha256(bytea), a pre-migration schema), losing
        # the audit row would be strictly worse than losing its tamper
        # evidence - so retry WITHOUT the chain columns and say so loudly.
        # The verifier reports these rows as kind="unchained", which is the
        # honest label for them.
        log.warning(
            "mcp_call_audit: chained insert failed - retrying UNCHAINED (rows verify as kind=unchained)",
            exc_info=True,
        )
        try:
            # Unique tombstones in BOTH chain columns (see unchained_tombstone):
            # prev is never '' again, so the fork-guard partial unique index
            # guards concurrent writers AFTER a degraded row too.
            tombstone_prev = unchained_tombstone()
            tombstone_row = unchained_tombstone()
            await db.execute(
                """
                INSERT INTO mcp_call_audit
                    (key_id, server, tool, args_sha256, args_bytes, outcome, model,
                     request_id, detail, prev_hash, row_hash)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                """,
                *chained_args[:9],
                tombstone_prev,
                tombstone_row,
            )
        except Exception:
            log.exception(
                "mcp_call_audit WRITE FAILED (call %s/%s outcome=%s succeeded; audit row lost) - check database health",
                server,
                tool,
                outcome,
            )


# --- external tail anchor (P2-5, audit 2026-11-10) -------------------------
ANCHOR_TABLE = "audit_chain_anchor"
# Domain separation: the anchor MAC key is a derivative of the master key, not
# the Fernet key itself, so the two uses cannot weaken each other.
_ANCHOR_KEY_LABEL = b"pcai-llm:audit-chain-anchor:v1"

# db/schema.py owns production DDL and is outside this remediation zone's
# write scope, so the anchor table is ensured lazily per process. Adding the
# same DDL to SCHEMA_SQL (the proper home) is reported to the Lead; the lazy
# CREATE TABLE IF NOT EXISTS is idempotent and keeps working either way.
ANCHOR_DDL = """CREATE TABLE IF NOT EXISTS audit_chain_anchor (
    table_name TEXT PRIMARY KEY,
    last_id BIGINT NOT NULL,
    last_row_hash TEXT NOT NULL,
    mac TEXT NOT NULL,
    anchored_at TIMESTAMPTZ NOT NULL DEFAULT now()
)"""

_anchor_ensured = False

# Keyset page size for the verifier walk (P2-29): memory per trail is one
# page, not one unbounded result set.
_VERIFY_PAGE = 2000


async def _ensure_anchor_table() -> bool:
    global _anchor_ensured
    if _anchor_ensured:
        return True
    try:
        await db.execute(ANCHOR_DDL)
    except Exception:
        log.warning(
            "audit_chain_anchor: could not ensure the anchor table — tail anchoring disabled",
            exc_info=True,
        )
        return False
    _anchor_ensured = True
    return True


def _anchor_mac(table: str, last_id: int, last_row_hash: str) -> str | None:
    """HMAC-SHA256 over the chain tail, keyed by a domain-separated derivative
    of the secrets master key; None when no master key is configured (the
    caller degrades to the unanchored behaviour and says so)."""
    master = settings.secrets_master_key.strip()
    if not master:
        return None
    key = hashlib.sha256(_ANCHOR_KEY_LABEL + b"|" + master.encode("utf-8")).digest()
    material = f"{table}|{int(last_id)}|{last_row_hash}".encode()
    return hmac.new(key, material, hashlib.sha256).hexdigest()


# Amortisation (cross-val fix, audit 2026-11-10): re-anchoring on EVERY
# chained write cost a tail SELECT + an UPSERT per MCP tool call on the data
# plane. The anchor is re-stamped at most once per this interval per table;
# rows written in between are covered by verify()'s "stale" prefix check, so
# the tamper window a lazy anchor opens is bounded by the interval, not by
# write volume.
_ANCHOR_REFRESH_S = 60.0
_last_anchor_refresh: dict[str, float] = {}


async def refresh_anchor(table: str, force: bool = False) -> None:
    """Re-anchor the chain tail after a successful chained INSERT.

    Best-effort BY CONTRACT (the audit write's own tradeoff): an anchor
    failure logs loudly and never fails the write it follows — a stale anchor
    is reported by verify() as "stale", never as a false tamper verdict.
    Amortised: at most one refresh per _ANCHOR_REFRESH_S per table (force=True
    overrides — used by operators/tests to re-anchor on demand).

    Call ONLY after the writer's own-pool commit (audit_action's conn-is-None
    shape): an anchor written inside a caller transaction that later rolls
    back would point past the real tail and flip every later verify() into a
    false anchor-mismatch.
    """
    try:
        if not settings.secrets_master_key.strip():
            return  # anchoring not configured — the unanchored residual stands
        if not force:
            last = _last_anchor_refresh.get(table, 0.0)
            if time.monotonic() - last < _ANCHOR_REFRESH_S:
                return
        if not await _ensure_anchor_table():
            return
        rows = await db.fetch(f"SELECT id, row_hash FROM {table} ORDER BY id DESC LIMIT 1")
        if not rows:
            return
        last_id = int(rows[0]["id"])
        last_hash = rows[0]["row_hash"] or ""
        mac = _anchor_mac(table, last_id, last_hash)
        if mac is None:  # pragma: no cover - key removed between the two checks
            return
        await db.execute(
            f"""
            INSERT INTO {ANCHOR_TABLE} (table_name, last_id, last_row_hash, mac)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (table_name)
            DO UPDATE SET last_id = EXCLUDED.last_id,
                          last_row_hash = EXCLUDED.last_row_hash,
                          mac = EXCLUDED.mac,
                          anchored_at = now()
            """,
            table,
            last_id,
            last_hash,
            mac,
        )
        _last_anchor_refresh[table] = time.monotonic()
    except Exception:
        log.warning(
            "audit_chain_anchor: tail anchor refresh failed for %s — verify() will report it as stale",
            table,
            exc_info=True,
        )


async def _anchor_verdict(table: str, last_id: int, last_row_hash: str) -> dict[str, Any]:
    """Compare the walked tail against the externally anchored tail.

    FAIL-CLOSED ON ABSENCE (cross-val fix, audit 2026-11-10): with a master
    key configured and a NON-EMPTY trail, a missing or undecodable anchor row
    is itself the attack — recompute the tail, delete the anchor, and the old
    "unanchored, but ok:true" answer was the pre-fix outcome. It now yields
    ok:false with a DISTINCT kind, so an operator can tell the states apart:

      anchored        the stored MAC is valid and covers exactly this tail.
      stale           the MAC is valid but covers an EARLIER tail, and the row
                      it covers still carries its anchored hash: rows were
                      appended after the last anchor; the anchored PREFIX holds.
      anchor-mismatch the MAC is valid but no longer vouches for the trail
                      (an edited tail row, or deleted rows): TAMPER — ok:false.
      anchor-missing  a trail EXISTS but no anchor row vouches for it (never
                      anchored since the feature, or the anchor row was
                      DELETED by the same hand that edited the trail): ok:false.
      anchor-invalid  an anchor row exists but its MAC does not verify (a
                      forged/corrupt anchor, or a master-key rotation):
                      ok:false — verification cannot be claimed.
      empty-trail     nothing to anchor (checked = 0) AND no anchor row
                      exists: a fresh deployment. The chain walk's ok stands,
                      distinctly reported.
      trail-wiped     checked = 0 but an anchor row still vouches for a
                      NON-EMPTY tail: the DB itself proves rows were deleted.
                      Fail-closed — deliberately checked BEFORE the MAC, since
                      even a forged/corrupt anchor is evidence a tail existed.
      no-master-key   anchoring not configured: chain-only behaviour, reported
                      honestly rather than claimed as verification.
      unavailable     the anchor table could not be ensured (DB fault).
    """
    if not settings.secrets_master_key.strip():
        return {"anchored": False, "anchor_status": "no-master-key"}
    if not await _ensure_anchor_table():
        return {"anchored": False, "anchor_status": "unavailable", "hardened": {"ok": False, "kind": "anchor-missing"}}
    stored = await db.fetch(
        f"SELECT last_id, last_row_hash, mac FROM {ANCHOR_TABLE} WHERE table_name = $1",
        table,
    )
    if last_id == 0:
        # P2 (cross-val wave 2): an EMPTY trail is benign ONLY when the anchor
        # agrees there was never anything to anchor (fresh deployment). A
        # surviving anchor row that vouches for a NON-EMPTY tail means the
        # trail was deleted underneath it — the DB itself proves rows existed,
        # so this fails CLOSED even though the walk found nothing. The MAC is
        # deliberately NOT consulted: a forged/corrupt anchor is still evidence
        # that a tail once existed (and is a tamper signal of its own).
        if not stored:
            return {"anchored": False, "anchor_status": "empty-trail"}
        try:
            a_id = int(stored[0]["last_id"])
        except Exception:
            a_id = -1  # an anchor we cannot even parse: fail closed below
        if a_id <= 0:
            return {"anchored": False, "anchor_status": "empty-trail"}
        return {
            "anchored": False,
            "anchor_status": "trail-wiped",
            "hardened": {"ok": False, "kind": "trail-wiped"},
        }
    missing = {
        "anchored": False,
        "anchor_status": "anchor-missing",
        "hardened": {"ok": False, "kind": "anchor-missing"},
    }
    if not stored:
        return missing
    try:
        a_id = int(stored[0]["last_id"])
        a_hash = stored[0]["last_row_hash"] or ""
        a_mac = stored[0]["mac"] or ""
    except Exception:
        return missing
    expect = _anchor_mac(table, a_id, a_hash)
    if expect is None or not hmac.compare_digest(expect, a_mac):
        # A MAC we cannot vouch for (forged anchor row, or a rotated master
        # key until the next write re-anchors): fail CLOSED, never claim.
        return {
            "anchored": False,
            "anchor_status": "anchor-invalid",
            "hardened": {"ok": False, "kind": "anchor-invalid"},
        }
    mismatch = {
        "anchored": False,
        "anchor_status": "anchor-mismatch",
        "hardened": {"ok": False, "kind": "anchor-mismatch"},
    }
    if a_id == last_id:
        if a_hash == last_row_hash:
            return {"anchored": True, "anchor_status": "anchored"}
        return mismatch
    if a_id > last_id:
        # The anchor covers rows the trail no longer has: the tail was DELETED.
        return mismatch
    # Rows were appended after the last anchor. The anchor still vouches for
    # the PREFIX iff the row it covers still carries its anchored hash — one
    # single-row lookup, only in this case.
    covered = await db.fetch(f"SELECT row_hash FROM {table} WHERE id = $1", a_id)
    covered_hash = ""
    if covered:
        try:
            covered_hash = covered[0]["row_hash"] or ""
        except Exception:
            covered_hash = ""
    if covered_hash == a_hash:
        return {"anchored": True, "anchor_status": "stale"}
    return mismatch


async def _verify(table: str, fields: list[str]) -> dict[str, Any]:
    """Walk one chained table by id ASC and report the worst real break.

    SEVERITY-ORDERED VERDICT (audit C5, 2026-10-29). The walk used to RETURN at
    the first row whose hash did not match, which made one degraded row (a
    never-fatal unchained write, or a pre-migration row with row_hash = '')
    the operator's ONLY verdict — every later row was never examined, so a
    real edit further down the trail was reported as "unchained", i.e. as a
    schema problem rather than as tamper. Three classes, in the order they must
    be surfaced:

      * break           - the row's hash does not match its OWN content
                          (edited fields, or an edited row_hash): TAMPER.
      * fork-or-legacy  - the row is internally consistent but does not follow
                          THIS row: two concurrent writers chained from the
                          same prev (the accepted residual), a row written
                          before chaining existed, or a spliced-in row.
      * unchained       - row_hash was never written (the degraded path). Not
                          tamper; the remedy is the DB/schema.

    A degraded row is COUNTED and stepped over (`prev` advances to its own
    hash — '' is the true predecessor state the next row chained from), so the
    verdict is the most severe class present anywhere in the trail, and
    `unchained` reports how many degraded rows were seen. `first_bad_id`
    names the first row OF THE REPORTED CLASS and `checked` is the number of
    rows the walk examined (the whole trail).

    The walk is KEYSET-PAGED (P2-29, audit 2026-11-10 — the fetch used to be
    one unbounded `ORDER BY id ASC` per trail, materialising a never-pruned
    chain in one result set on the pool the data plane shares): pages of
    _VERIFY_PAGE rows keyed on `id > $last`, with `prev`, the counters and
    the worst-finding carried across pages, so memory stays one page per trail.

    The result carries the external tail anchor's verdict (P2-5): see
    _anchor_verdict for the anchor_status vocabulary. A valid anchor that no
    longer vouches for the walked tail is TAMPER (ok:false,
    kind="anchor-mismatch") — this is what stops an attacker with DB write
    access, who can recompute the whole chain tail, from passing as ok:true.
    """
    prev = ""
    checked = 0
    unchained = 0
    last_id = 0
    first_unchained: dict[str, Any] | None = None
    first_fork: dict[str, Any] | None = None
    first_break: dict[str, Any] | None = None
    while True:
        batch = await db.fetch(
            f"SELECT id, prev_hash, row_hash, {', '.join(fields)} FROM {table} WHERE id > $1 ORDER BY id ASC LIMIT $2",
            last_id,
            _VERIFY_PAGE,
        )
        if not batch:
            break
        progressed = False
        for row in batch:
            want = chain_hash(prev, [row[f] for f in fields])
            got = row["row_hash"] or ""
            # P3 (audit 2026-11-10): a degraded row carries either '' (the
            # legacy shape) or a unique "unchained-" tombstone (the new shape
            # that keeps prev non-empty for the fork-guard index). Both are
            # the UNCHAINED class — a tombstone must never be misread as
            # tamper ("break") just because it is a non-empty hash.
            is_unchained = not got or got.startswith("unchained-")
            if got != want:
                # A degraded row's hash can never be "internally consistent",
                # so the self-consistency probe is skipped for it.
                self_consistent = (
                    not is_unchained
                    and bool(got)
                    and chain_hash(row["prev_hash"] or "", [row[f] for f in fields]) == got
                )
                finding = {
                    "ok": False,
                    "table": table,
                    "first_bad_id": row["id"],
                    "kind": "unchained" if is_unchained else ("fork-or-legacy" if self_consistent else "break"),
                    "expected": want,
                    "found": got,
                }
                if is_unchained:
                    unchained += 1
                    if first_unchained is None:
                        first_unchained = finding
                elif not self_consistent:
                    if first_break is None:
                        first_break = finding
                elif first_fork is None:
                    first_fork = finding
            # ADVANCE UNCONDITIONALLY: a degraded row's '' IS the predecessor state
            # the following row chained from, so stepping over it keeps every later
            # row's expectation correct (this is what lets the walk reach a real
            # edit that sits after a degraded row).
            prev = got
            checked += 1
            row_id = int(row["id"])
            if row_id > last_id:
                last_id = row_id
                progressed = True
        # Defensive no-progress guard: a seam that ignores the keyset predicate
        # would otherwise loop forever; a real database always advances.
        if not progressed:
            break
        if len(batch) < _VERIFY_PAGE:
            break  # final page
    # `checked` is the number of rows this verification EXAMINED (the whole
    # trail now, always) — in both the ok and the not-ok shape.
    worst = first_break or first_fork or first_unchained
    result = (
        {
            "ok": True,
            "table": table,
            "checked": checked,
            "first_bad_id": None,
            "kind": "",
            "unchained": 0,
        }
        if worst is None
        else {**worst, "checked": checked, "unchained": unchained}
    )
    # External tail anchor (P2-5): a valid anchor that no longer vouches for
    # the walked tail hardens the verdict — it never softens a chain break.
    # Fail-closed verdicts travel in anchor["hardened"] so result.update()
    # cannot clobber the walk's own kind before the severity rule below runs.
    anchor = await _anchor_verdict(table, last_id, prev)
    hardened = anchor.pop("hardened", None)
    result.update(anchor)
    # The anchor's fail-closed kinds (anchor-mismatch / anchor-missing /
    # anchor-invalid) are TAMPER-or-worse: they outrank the benign classes.
    # They override a clean walk or a merely-degraded ("unchained") verdict;
    # a break/fork-or-legacy walk verdict already names the tampered row, so
    # it stays the reported kind.
    if hardened is not None and result.get("kind") in ("", "unchained"):
        result.update(hardened)
        result["first_bad_id"] = None
    return result


async def verify_admin_audit_chain() -> dict[str, Any]:
    """Verify admin_audit. Fields MUST match the order core.audit.audit_action
    chains over - a change in one place without the other is a chain break."""
    return await _verify("admin_audit", ADMIN_AUDIT_CHAIN_FIELDS)


async def verify_mcp_call_chain() -> dict[str, Any]:
    return await _verify("mcp_call_audit", MCP_CALL_CHAIN_FIELDS)


# The chained field lists are MODULE CONSTANTS so the writer and the verifier
# cannot drift: audit_action imports ADMIN_AUDIT_CHAIN_FIELDS and builds its
# hash from exactly these, in exactly this order.
ADMIN_AUDIT_CHAIN_FIELDS: list[str] = [
    "actor_key_id",
    "actor_subject",
    "actor_source",
    "action",
    "resource_type",
    "resource_id",
    "details_sha256",
    "request_id",
    "source_ip",
]

MCP_CALL_CHAIN_FIELDS: list[str] = [
    "key_id",
    "server",
    "tool",
    "args_sha256",
    "args_bytes",
    "outcome",
    "model",
    "request_id",
    "detail",
]
