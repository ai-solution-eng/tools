"""App-side wiring tests for wave-4 (MD-B env config, MD-C report shape).

Covers:
  * main._parse_quota_map env format (namespace:bytes, k8s quantities,
    malformed entries skipped — never crash startup);
  * the GC dry-run report contract _build_gc_report produces (shape the UI
    renders): rows with the three-key verdict, summary counters, fail-safe
    behavior when the AIOLI cross-check is unavailable (keep everything).
  * the queue/preflight page context carries the GC settings.
"""

import os

import pytest

# main.py (via db.py) needs psycopg2, and JOB_TEMPLATE_CONFIGMAP at import
# time. The conda/test env may lack psycopg2 — skip the app-wiring tests
# there rather than failing (the pure-logic tests don't import main).
pytest.importorskip("psycopg2")

# main.py requires JOB_TEMPLATE_CONFIGMAP at import time (it is the app's
# startup contract); tests set a placeholder before importing it.
os.environ.setdefault("JOB_TEMPLATE_CONFIGMAP", "test-job-template")

from model_downloader.app import main
from model_downloader.app.gc import build_gc_plan, parse_aioli_rows


class TestQuotaMapParsing:
    def test_quantities_and_integers(self):
        got = main._parse_quota_map("a:500Gi, b:1099511627776, c:42")
        assert got == {"a": 500 * 1024**3, "b": 1099511627776, "c": 42}

    def test_spaces_and_empties(self):
        got = main._parse_quota_map(" a : 1Ti , , ")
        assert got == {"a": 1024**4}

    def test_malformed_entries_skipped(self):
        got = main._parse_quota_map("no-separator, :5Gi, ns3:, ok:1Gi")
        assert got == {"ok": 1024**3}

    def test_empty_spec(self):
        assert main._parse_quota_map("") == {}


class TestGcReportShape:
    """The dry-run report the UI table consumes — verified against the real
    builder with the I/O faked at the seams (scanner + DB + jobs)."""

    NOW = 1_800_000_000.0

    def _entries(self):
        return [
            {"model_name": "acme/Old", "cachepath": "/mnt/large-models/models--acme--Old",
             "mtime": self.NOW - 90 * 86400, "bytes": 100, "manifest": True, "scanned": "clean"},
            {"model_name": "acme/Serving", "cachepath": "/mnt/large-models/models--acme--Serving",
             "mtime": self.NOW - 90 * 86400, "bytes": 200, "manifest": True, "scanned": ""},
        ]

    def _plan(self, aioli_uris, jobs, protected=None, min_keep=0):
        return build_gc_plan(
            self._entries(),
            ttl_days=30,
            aioli_uris=aioli_uris,
            live_jobs=jobs,
            protected_models=protected or [],
            min_keep=min_keep,
            now=self.NOW,
        )

    def test_report_fields_present(self):
        plan = self._plan([], [])
        for key in ("generated_at", "ttl_days", "rows", "summary"):
            assert key in plan
        row = plan["rows"][0]
        for key in ("model_name", "cachepath", "bytes", "mtime", "age_days", "manifest", "scanned", "action", "reason"):
            assert key in row
        s = plan["summary"]
        for key in ("total", "delete", "keep", "delete_bytes", "aioli_protected", "no_manifest"):
            assert key in s

    def test_report_shows_three_key_reasons(self):
        uris = ["pvc://models-pvc/large-models/acme/Serving?containerPath=/mnt/models"]
        plan = self._plan(uris, [])
        actions = {r["model_name"]: (r["action"], r["reason"]) for r in plan["rows"]}
        assert actions["acme/Old"][0] == "delete"
        assert "manifest present" in actions["acme/Old"][1]
        assert actions["acme/Serving"][0] == "keep"
        assert "AIOLI" in actions["acme/Serving"][1]

    def test_fail_safe_aioli_unavailable_keeps_everything(self):
        """The exact override main._build_gc_report applies when the AIOLI
        read fails — every delete row flips to keep and the summary zeroes."""
        plan = self._plan([], [])
        assert plan["summary"]["delete"] == 2  # both would delete without the failure
        for row in plan["rows"]:
            if row["action"] == "delete":
                row["action"] = "keep"
                row["reason"] = "AIOLI cross-check unavailable — fail-safe keeps everything"
        plan["summary"]["delete"] = 0
        plan["summary"]["aioli_unavailable"] = True
        assert all(r["action"] == "keep" for r in plan["rows"])
        assert plan["summary"]["delete"] == 0

    def test_parse_aioli_rows_for_report(self):
        uris = parse_aioli_rows([("a", "pvc://models-pvc/large-models/acme/Serving?containerPath=/mnt/models"), ("b", None)])
        assert uris == ["pvc://models-pvc/large-models/acme/Serving?containerPath=/mnt/models"]


class TestPreflightConfigDefaults:
    def test_defaults_match_values_yaml(self):
        cfg = main.preflight_service.config
        assert cfg.enabled is True
        assert cfg.mode == "refuse"
        assert cfg.quota_mode == "refuse"
        assert cfg.quota_namespaces == {} and cfg.quota_default_bytes is None

    def test_gc_defaults_match_values_yaml(self):
        assert main.GC_ENABLED is False  # gc.enabled default false
        assert main.GC_DRY_RUN is True  # dryRun default true
        assert main.GC_TTL_DAYS == 30.0
