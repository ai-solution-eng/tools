"""Regression tests for the 2026-11-10 audit remediation wave, zone R9
(config / app / DB / observability).

Covers the ops findings that are testable without a live Postgres:
P2-27 (per-boot schema full-scan DML gated), P2-28 (embedding_cache TTL +
prune), P2-13 (gated one-live-sso-key index), and the P3s (flat-span
docstring honesty, OTLP URL credential leak, CSP frame-ancestors
allow-list, MCP server-label contract comment). The fork-guard empty-prev
gap (schema.py uq_*_audit_prev) is reported as ACCEPTED-RESIDUAL: both of
the audit's suggested fixes are defective as stated (see the zone report).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest

from pcai_llm.config import settings
from pcai_llm.db.schema import SCHEMA_SQL

_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# P2-27 — per-boot schema transaction must not carry full-scan DML
# ---------------------------------------------------------------------------


def test_budget_period_check_readds_are_not_valid() -> None:
    """The budget period CHECK re-adds run on EVERY boot: without NOT VALID
    each ADD re-validates every existing row (a full scan under ACCESS
    EXCLUSIVE) per boot. Same discipline as ck_ledger_status."""
    fragment = "CHECK (period IN ('day','week','month','quarter','year')) NOT VALID"
    assert fragment in SCHEMA_SQL
    assert SCHEMA_SQL.count(fragment) == 2, "both budget tables must be NOT VALID"


def test_guardrails_engine_check_readd_is_not_valid() -> None:
    """Cross-validation P3 (verified on a real PG): guardrails_engine_check
    was DROP+ADDed without NOT VALID — a VALID re-add scans every row under
    ACCESS EXCLUSIVE on every boot inside the same implicit transaction as
    the rest of SCHEMA_SQL. Also the honest posture: the unconditional
    re-add would reset an operator's VALIDATE on the next boot anyway."""
    assert "CHECK (engine IN ('blocklist','regex_pii','length','webhook','secrets')) NOT VALID" in SCHEMA_SQL


def test_url_guard_resolver_saturated_counter_is_wired() -> None:
    """The resolver-saturation refusal must have a dashboard surface: a
    label-free counter in core.metrics (global condition) incremented at
    core.url_guard's saturation raise site — not from inside a busy loop,
    and not with a caller-controlled label."""
    from pcai_llm.core import metrics, url_guard

    assert url_guard.__name__  # import sanity: no cycle between the two
    with patch.object(metrics, "URL_GUARD_RESOLVER_SATURATED") as counter:
        metrics.observe_url_guard_resolver_saturated()
        counter.inc.assert_called_once()
    text = (_ROOT / "src" / "pcai_llm" / "core" / "url_guard.py").read_text(encoding="utf-8")
    assert "observe_url_guard_resolver_saturated()" in text
    # the increment sits at the saturation raise site, before the raise
    inc_at = text.index("_metrics.observe_url_guard_resolver_saturated()")
    raise_at = text.index("raise ResolverSaturated(")
    assert inc_at < raise_at


def test_api_keys_key_group_backfill_is_gated() -> None:
    """The api_keys key_group backfill UPDATE must sit inside a DO block
    gated on rows actually needing it, and the NOT NULL flip gated on the
    catalog nullability — bare, both re-plan on every boot."""
    start = SCHEMA_SQL.index("ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS key_group UUID;")
    block = SCHEMA_SQL[start : SCHEMA_SQL.index("END $$;", start)]
    assert "IF EXISTS (SELECT 1 FROM api_keys WHERE key_group IS NULL) THEN" in block
    assert "UPDATE api_keys SET key_group = COALESCE(spend_ancestors[1], id) WHERE key_group IS NULL" in block
    assert "is_nullable = 'YES'" in block
    assert "ALTER TABLE api_keys ALTER COLUMN key_group SET NOT NULL" in block
    # the gate comes FIRST: a steady-state boot runs the probes, not the DML
    gate = block.index("IF EXISTS (SELECT 1 FROM api_keys WHERE key_group IS NULL)")
    assert gate < block.index("UPDATE api_keys SET key_group")


def test_conversations_key_group_backfill_is_gated() -> None:
    """The transcript-table backfill is the big one: on the first
    post-upgrade boot it rewrites every row (legitimate, once); every later
    boot must be an index probe, not a full scan inside the schema
    transaction."""
    start = SCHEMA_SQL.index("ALTER TABLE conversations ADD COLUMN IF NOT EXISTS key_group UUID;")
    block = SCHEMA_SQL[start : SCHEMA_SQL.index("END $$;", start)]
    assert "IF EXISTS (SELECT 1 FROM conversations WHERE key_group IS NULL) THEN" in block
    assert "UPDATE conversations c SET key_group = k.key_group" in block
    assert "is_nullable = 'YES'" in block
    assert "ALTER TABLE conversations ALTER COLUMN key_group SET NOT NULL" in block
    gate = block.index("IF EXISTS (SELECT 1 FROM conversations WHERE key_group IS NULL)")
    assert gate < block.index("UPDATE conversations c SET key_group")


def test_key_group_not_null_is_preceded_by_default_for_both_tables() -> None:
    """Cross-validation P1 (reproduced on PG 18): SET NOT NULL WITHOUT a
    server default aborts the boot — a row inserted between the backfill
    UPDATE's snapshot and the ALTER (an old-version pod mid rolling-upgrade
    writes no key_group) fails "column contains null values", and SCHEMA_SQL
    being one implicit transaction the whole schema apply rolls back; after
    the ALTER lands, every writer that does not name the column 500s. Both
    key_group migrations must therefore SET DEFAULT gen_random_uuid() BEFORE
    SET NOT NULL, inside the same is_nullable gate.

    WEAK PIN, stated honestly: this is a substring-order assertion, not a
    live-Postgres race test — the audit's test gap #9 (SCHEMA_SQL applied
    twice, and against a pre-migration DB with a concurrent writer) is the
    right long-term fix and remains open."""
    for table in ("api_keys", "conversations"):
        prefix = "ALTER TABLE " + table + " ALTER COLUMN key_group SET "
        default_at = SCHEMA_SQL.index(prefix + "DEFAULT gen_random_uuid();")
        notnull_at = SCHEMA_SQL.index(prefix + "NOT NULL;")
        # the default must precede the NOT NULL...
        assert default_at < notnull_at, table + ": SET DEFAULT must precede SET NOT NULL"
        # ...inside the SAME is_nullable gate (between the gate and the END)
        gate_at = SCHEMA_SQL.rindex("is_nullable = 'YES'", 0, notnull_at)
        end_at = SCHEMA_SQL.index("END $$;", notnull_at)
        assert gate_at < default_at < notnull_at < end_at, (
            table + ": SET DEFAULT/SET NOT NULL must sit inside the nullability gate"
        )


# ---------------------------------------------------------------------------
# P2-13 — one live sso-create key per subject, enforced by the database
# ---------------------------------------------------------------------------


def test_uq_api_keys_one_live_sso_gated_creation() -> None:
    """The partial unique index exists with the audit predicate, and its
    creation is gated: duplicates in a pre-fix database must produce a
    WARNING + skip, not a failed boot on every subsequent start."""
    marker = SCHEMA_SQL.index("uq_api_keys_one_live_sso")
    block = SCHEMA_SQL[SCHEMA_SQL.rindex("DO $$", 0, marker) : SCHEMA_SQL.index("END $$;", marker)]
    assert "CREATE UNIQUE INDEX IF NOT EXISTS uq_api_keys_one_live_sso" in block
    assert "ON api_keys(subject_id) WHERE bound_via = 'sso-create' AND revoked_at IS NULL" in block
    assert "GROUP BY subject_id HAVING count(*) > 1" in block
    assert "RAISE WARNING" in block
    # the WARNING path fires only when duplicates exist
    assert block.index("IF dup_subjects > 0 THEN") < block.index("RAISE WARNING")


# ---------------------------------------------------------------------------
# P2-28 — embedding_cache TTL + housekeeping prune
# ---------------------------------------------------------------------------


def test_embedding_cache_ttl_default_is_keep_forever() -> None:
    """0 = keep forever, the retention-knob convention: nothing is deleted
    until the operator opts in."""
    assert settings.embedding_cache_ttl_s == 0


def test_embedcache_prune_gated_on_ttl() -> None:
    from pcai_llm.core import embedcache

    class FakeDB:
        def __init__(self) -> None:
            self.sql: str | None = None

        async def execute(self, sql: str, *a: object) -> str:
            self.sql = sql
            return "DELETE 7"

    fake = FakeDB()
    with patch.object(embedcache, "db", fake):
        assert asyncio.run(embedcache.prune(0)) == 0
        assert fake.sql is None, "ttl 0 must not touch the database"
        assert asyncio.run(embedcache.prune(3600)) == 7
    assert "DELETE FROM embedding_cache" in (fake.sql or "")
    assert "make_interval(secs =>" in (fake.sql or "")


def test_housekeeping_sweep_prunes_embedding_cache() -> None:
    from pcai_llm.core import housekeeping

    class FakeDB:
        async def execute(self, sql: str, *a: object) -> str:
            return "DELETE 0"

    called: list[int] = []

    async def fake_prune(ttl: int) -> int:
        called.append(ttl)
        return 0

    async def noop() -> None:
        return None

    monkey = pytest.MonkeyPatch()
    monkey.setattr(settings, "embedding_cache_ttl_s", 3600)
    try:
        with (
            patch.object(housekeeping, "db", FakeDB()),
            patch.object(housekeeping.embedcache, "prune", fake_prune),
            patch.object(housekeeping.response_cache, "purge_expired", noop),
            patch.object(housekeeping, "_prune_canonical_store", noop),
        ):
            asyncio.run(housekeeping._sweep_once())
            assert called == [3600]
            monkey.setattr(settings, "embedding_cache_ttl_s", 0)
            asyncio.run(housekeeping._sweep_once())
            assert called == [3600], "ttl 0 (keep forever) must not prune"
    finally:
        monkey.undo()


# ---------------------------------------------------------------------------
# P3 — tracing: flat-span docstring honesty + OTLP URL credential leak
# ---------------------------------------------------------------------------


def test_config_no_longer_promises_child_spans() -> None:
    """config.py once promised one parent per request with a child per relay
    hop; at the 2026-11-10 audit start_span had exactly one caller and no
    traceparent propagation, so every trace was one flat span. Child spans
    have since landed (the Langfuse/OTLP work); the old promise must stay
    gone, and the comment must describe the tree that actually exists."""
    text = (_ROOT / "src" / "pcai_llm" / "config.py").read_text(encoding="utf-8")
    assert "child per relay hop" not in text


def test_failed_otlp_export_logs_host_not_credentials(caplog: pytest.LogCaptureFixture) -> None:
    from pcai_llm.core import tracing

    class ExplodingClient:
        async def post(self, *a: object, **kw: object) -> None:
            raise RuntimeError("connection refused")

    with (
        patch.object(tracing, "_get_client", lambda: ExplodingClient()),
        patch.object(settings, "otel_endpoint", "http://user:supersecret@collector.example:4318"),
        caplog.at_level("DEBUG", logger="pcai_llm.core.tracing"),
    ):
        asyncio.run(tracing._export({"resourceSpans": []}))

    assert "supersecret" not in caplog.text, "credentials must never reach a log line"
    assert "user@" not in caplog.text, "userinfo is part of the credential"
    assert "collector.example:4318" in caplog.text, "host:port is what diagnosis needs"


# ---------------------------------------------------------------------------
# P3 — CSP frame-ancestors allow-list
# ---------------------------------------------------------------------------


def test_csp_frame_ancestors_default_is_self_only() -> None:
    from pcai_llm.app import _csp_frame_ancestors

    assert _csp_frame_ancestors() == "'self'"


def test_csp_frame_ancestors_allowlist_and_sanitisation() -> None:
    from pcai_llm.app import _csp_frame_ancestors

    monkey = pytest.MonkeyPatch()
    try:
        monkey.setattr(settings, "csp_frame_ancestors", " https://portal.example.com , https://ops.example.net:8443 ")
        assert _csp_frame_ancestors() == "'self' https://portal.example.com https://ops.example.net:8443"
        # header-shaping junk is dropped, never passed through: semicolons
        # split CSP directives, quotes/keywords would fake source
        # expressions, spaces/commas are the delimiters themselves.
        monkey.setattr(
            settings,
            "csp_frame_ancestors",
            "https://ok.example,;script-src 'unsafe-eval',javascript:alert(1),'self',https://also.ok.example/path",
        )
        assert _csp_frame_ancestors() == "'self' https://ok.example https://also.ok.example/path"
    finally:
        monkey.undo()


def test_csp_header_no_longer_allows_any_https_framer() -> None:
    text = (_ROOT / "src" / "pcai_llm" / "app.py").read_text(encoding="utf-8")
    assert "frame-ancestors 'self' https:" not in text


# ---------------------------------------------------------------------------
# P3 — MCP server metric label contract
# ---------------------------------------------------------------------------


def test_mcp_server_label_comment_states_the_caller_contract() -> None:
    """metrics.py claimed the server label was bounded by the MCP registry
    while call sites passed caller-controlled strings. The comment must
    state the actual contract: the caller passes a registry-resolved name
    (or a fixed bucket)."""
    text = (_ROOT / "src" / "pcai_llm" / "core" / "metrics.py").read_text(encoding="utf-8")
    assert "bounded by the MCP registry" not in text
    assert "registry-resolved server name" in text


# ---------------------------------------------------------------------------
# Follow-up (costs wave) — cost_fallback_per_m knob + startup unpriced WARNING
# ---------------------------------------------------------------------------


def test_cost_fallback_per_m_default_is_off() -> None:
    """0.0 = off — the historical NULL/unpriced behaviour. The knob exists
    so a USD budget gate cannot be silently inert for an unpriced model,
    but nothing is billed until the operator sets it."""
    assert settings.cost_fallback_per_m == 0.0


def test_lifespan_warns_unpriced_best_effort() -> None:
    """The lifespan must call costs.warn_unpriced on the router's model list
    AFTER the registry refresh has populated it, wrapped so a cost-table
    fault cannot block startup (one WARNING per boot, never a raise)."""
    text = (_ROOT / "src" / "pcai_llm" / "app.py").read_text(encoding="utf-8")
    call = "await costs.warn_unpriced(model_router.models())"
    assert call in text, "lifespan must call the unpriced-model WARNING"
    at = text.index(call)
    refresh_at = text.rindex("await registry.refresh()", 0, at)
    assert refresh_at < at, "the check must run AFTER the registry refresh"
    assert text.rindex("try:", 0, at) < at, "the call must be guarded"
    assert ('except Exception:\n        log.debug("startup: unpriced-model check skipped", exc_info=True)') in text, (
        "a cost-table fault must be swallowed, never re-raised"
    )
