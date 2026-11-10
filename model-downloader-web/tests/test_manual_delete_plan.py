"""Unit tests for the admin manual-deletion verdict (gc.evaluate_manual_targets).

The contract this pins down:

* an admin selecting a model overrides AGE — the TTL line must not appear here;
* a missing provenance manifest is a WARNING, because models fetched outside
  this app have no record and must stay deletable;
* an AIOLI packaged_models hit is a SOFT block (overridable per request),
  because MLIS rows go stale and a hard block would strand those weights;
* a live job, a protectedModels glob, and an UNAVAILABLE cross-check are HARD
  blocks — the last two are the fail-closed half, and the unavailable case is
  the one the report path gets wrong (main.py fails open, the CronJob fails
  closed; this must follow the CronJob).
"""

from model_downloader.app.gc import (
    VERDICT_BLOCK,
    VERDICT_DELETABLE,
    VERDICT_SOFT_BLOCK,
    drop_delete_jobs,
    evaluate_manual_targets,
)

NOW = 1_800_000_000.0
ANCIENT = NOW - 400 * 86400


def entry(name="acme/M", cachepath=None, mtime=ANCIENT, nbytes=100, manifest=True, scanned="clean"):
    return {
        "model_name": name,
        "cachepath": cachepath or f"/mnt/large-models/models--{name.replace('/', '--')}",
        "mtime": mtime,
        "bytes": nbytes,
        "manifest": manifest,
        "scanned": scanned,
    }


def verdict_of(entries, **kw):
    defaults = {"aioli_uris": [], "live_jobs": []}
    defaults.update(kw)
    return evaluate_manual_targets(entries, **defaults)


# ---------------------------------------------------------------------------
# the deletable baseline
# ---------------------------------------------------------------------------


def test_clean_target_is_deletable():
    (row,) = verdict_of([entry()])
    assert row["verdict"] == VERDICT_DELETABLE
    assert row["blocks"] == []
    assert row["warnings"] == []


def test_row_shape():
    (row,) = verdict_of([entry()])
    assert set(row) == {
        "cachepath",
        "model_name",
        "bytes",
        "age_days",
        "manifest",
        "scanned",
        "verdict",
        "blocks",
        "warnings",
    }


def test_order_is_preserved():
    entries = [entry("a/A"), entry("b/B"), entry("c/C")]
    rows = verdict_of(entries)
    assert [r["model_name"] for r in rows] == ["a/A", "b/B", "c/C"]


# ---------------------------------------------------------------------------
# the TTL line must NOT apply — this is the point of a manual action
# ---------------------------------------------------------------------------


def test_age_never_blocks():
    # 400 days old and still deletable: a manual delete is not a TTL decision.
    (row,) = verdict_of([entry(mtime=ANCIENT)])
    assert row["verdict"] == VERDICT_DELETABLE
    assert row["age_days"] is not None and row["age_days"] > 300


def test_unknown_mtime_is_tolerated():
    (row,) = verdict_of([entry(mtime=0)])
    assert row["verdict"] == VERDICT_DELETABLE
    assert row["age_days"] is None


# ---------------------------------------------------------------------------
# hard blocks
# ---------------------------------------------------------------------------


def test_live_job_blocks():
    jobs = [{"namespace": "ns", "job_name": "md-x", "cache_root": "/mnt/large-models", "model_name": ""}]
    (row,) = verdict_of([entry()], live_jobs=jobs)
    assert row["verdict"] == VERDICT_BLOCK
    assert any("live job ns/md-x" in b for b in row["blocks"])


def test_protected_glob_blocks_on_model_name():
    (row,) = verdict_of([entry("deepseek-ai/DeepSeek-V4.1-Flash")], protected_models=["deepseek-ai/*"])
    assert row["verdict"] == VERDICT_BLOCK
    assert any("protectedModels" in b for b in row["blocks"])


def test_protected_glob_blocks_on_cache_dir():
    e = entry("acme/M", cachepath="/mnt/large-models/models--acme--M")
    (row,) = verdict_of([e], protected_models=["models--acme--*"])
    assert row["verdict"] == VERDICT_BLOCK


def test_aioli_unavailable_blocks_everything():
    rows = verdict_of([entry("a/A"), entry("b/B")], aioli_available=False)
    assert [r["verdict"] for r in rows] == [VERDICT_BLOCK, VERDICT_BLOCK]
    assert all(any("AIOLI" in b for b in r["blocks"]) for r in rows)


def test_jobs_unavailable_blocks_everything():
    # The report path fails OPEN here (main.py swallows the listing error).
    # The manual path must fail CLOSED, like the GC CronJob.
    (row,) = verdict_of([entry()], jobs_available=False)
    assert row["verdict"] == VERDICT_BLOCK
    assert any("live jobs" in b for b in row["blocks"])


def test_hard_block_wins_over_soft_block():
    jobs = [{"namespace": "ns", "job_name": "md-x", "cache_root": "/mnt/large-models", "model_name": ""}]
    (row,) = verdict_of(
        [entry()],
        live_jobs=jobs,
        aioli_uris=["pvc://models-pvc/large-models/acme/M?containerPath=/mnt/models"],
    )
    assert row["verdict"] == VERDICT_BLOCK
    # both reasons survive, so the operator sees the full picture
    assert any("live job" in b for b in row["blocks"])
    assert any("AIOLI" in b for b in row["blocks"])


# ---------------------------------------------------------------------------
# soft block — the one overridable refusal
# ---------------------------------------------------------------------------


def test_aioli_row_is_soft_block_and_quotes_the_uri():
    uri = "pvc://models-pvc/large-models/acme/M?containerPath=/mnt/models"
    (row,) = verdict_of([entry()], aioli_uris=[uri])
    assert row["verdict"] == VERDICT_SOFT_BLOCK
    assert any(uri in b for b in row["blocks"])


def test_aioli_match_is_by_name_not_path():
    # custom cache root: the on-disk path differs from the uri, the cache-dir
    # name is what matches (the gc.py contract).
    e = entry("acme/M", cachepath="/mnt/elsewhere/models--acme--M")
    (row,) = verdict_of([e], aioli_uris=["pvc://models-pvc/large-models/acme/M"])
    assert row["verdict"] == VERDICT_SOFT_BLOCK


def test_similar_name_does_not_match():
    # models--acme--M must not match a uri for acme/M-Extra
    e = entry("acme/M", cachepath="/mnt/large-models/models--acme--M")
    (row,) = verdict_of([e], aioli_uris=["pvc://models-pvc/large-models/acme/M-Extra"])
    assert row["verdict"] == VERDICT_DELETABLE


# ---------------------------------------------------------------------------
# manifest is a warning, never a block
# ---------------------------------------------------------------------------


def test_missing_manifest_warns_but_allows():
    (row,) = verdict_of([entry(manifest=False)])
    assert row["verdict"] == VERDICT_DELETABLE
    assert any("no provenance manifest" in w for w in row["warnings"])


def test_unknown_manifest_warns_but_allows():
    (row,) = verdict_of([entry(manifest=None)])
    assert row["verdict"] == VERDICT_DELETABLE
    assert any("unknown" in w for w in row["warnings"])


def test_delete_jobs_are_dropped_from_the_live_job_list():
    # A legacy delete Job carrying the scan root as its cache-root blocked the
    # whole page; delete Jobs must never count as use.
    jobs = [
        {"job_name": "md-delete-abc", "managed_by": "model-downloader-delete", "cache_root": "/mnt/large-models"},
        {"job_name": "md-x", "managed_by": "model-downloader", "cache_root": "/mnt/large-models"},
    ]
    kept = drop_delete_jobs(jobs)
    assert [j["job_name"] for j in kept] == ["md-x"]


def test_a_legacy_delete_job_no_longer_blocks_anything():
    # The exact stuck state: a finished delete Job annotated with the scan root.
    jobs = [{"job_name": "md-delete-old", "managed_by": "model-downloader-delete", "cache_root": "/mnt/large-models"}]
    (row,) = verdict_of([entry()], live_jobs=drop_delete_jobs(jobs))
    assert row["verdict"] == VERDICT_DELETABLE


def test_manifest_warning_survives_a_block():
    jobs = [{"namespace": "ns", "job_name": "md-x", "cache_root": "/mnt/large-models", "model_name": ""}]
    (row,) = verdict_of([entry(manifest=False)], live_jobs=jobs)
    assert row["verdict"] == VERDICT_BLOCK
    assert any("no provenance manifest" in w for w in row["warnings"])
