"""Unit tests for MD-C TTL/GC eviction logic (gc.py).

The heart of MD-C is the THREE-KEY AND deletion rule — delete ONLY when
manifest present AND no AIOLI row AND no live job — plus the uri-matching
contract (cache-dir name / repo id appearing in the uri, NEVER exact path
equality) and the dry-run report shape the UI renders. Each test in the
matrix flips exactly one key to prove every missing key blocks deletion.
"""


from model_downloader.app.gc import (
    aioli_protected_dirs,
    build_gc_plan,
    cache_dir_name,
    is_expired,
    live_job_protects,
    parse_aioli_rows,
    repo_id_from_cache_dir,
    uri_matches_cache_dir,
)

NOW = 1_800_000_000.0
OLD = NOW - 90 * 86400  # 90 days old — beyond every TTL used below


def entry(name="acme/M", cachepath=None, mtime=OLD, nbytes=100, manifest=True, scanned="clean"):
    return {
        "model_name": name,
        "cachepath": cachepath or f"/mnt/large-models/models--{name.replace('/', '--')}",
        "mtime": mtime,
        "bytes": nbytes,
        "manifest": manifest,
        "scanned": scanned,
    }


def plan_of(entries, **kw):
    defaults = {"ttl_days": 30, "aioli_uris": [], "live_jobs": [], "now": NOW}
    defaults.update(kw)
    return build_gc_plan(entries, **defaults)


# ---------------------------------------------------------------------------
# uri matching (the A1/AIOLI contract)
# ---------------------------------------------------------------------------


class TestUriMatching:
    def test_seed_catalog_uri_shape_matches(self):
        uri = "pvc://models-pvc/large-models/deepseek-ai/DeepSeek-V4-Flash-0731?containerPath=/mnt/models"
        assert uri_matches_cache_dir(uri, "models--deepseek-ai--DeepSeek-V4-Flash-0731")

    def test_literal_cache_dir_name_in_uri_matches(self):
        assert uri_matches_cache_dir("pvc://models-pvc/x/models--acme--M", "models--acme--M")

    def test_no_path_equality_required(self):
        # custom cache root on disk, default root in the uri — still matches
        uri = "pvc://models-pvc/large-models/acme/M?containerPath=/mnt/models"
        assert uri_matches_cache_dir(uri, "models--acme--M")

    def test_prefix_models_do_not_match(self):
        uri = "pvc://models-pvc/large-models/deepseek-ai/DeepSeek-V4?containerPath=/mnt/models"
        assert not uri_matches_cache_dir(uri, "models--deepseek-ai--DeepSeek")

    def test_empty_inputs_never_match(self):
        assert not uri_matches_cache_dir("", "models--a--b")
        assert not uri_matches_cache_dir("pvc://x/y", "")
        assert not uri_matches_cache_dir("pvc://x/y", "not-a-cache-dir")

    def test_protected_dirs_set(self):
        dirs = ["models--acme--Serving", "models--acme--Free"]
        uris = ["pvc://models-pvc/large-models/acme/Serving?containerPath=/mnt/models"]
        assert aioli_protected_dirs(uris, dirs) == {"models--acme--Serving"}

    def test_naming_helpers(self):
        assert cache_dir_name("a/B") == "models--a--B"
        assert repo_id_from_cache_dir("models--a--B") == "a/B"


# ---------------------------------------------------------------------------
# the three-key AND deletion matrix
# ---------------------------------------------------------------------------


class TestThreeKeyMatrix:
    def test_all_three_keys_present_deletes(self):
        plan = plan_of([entry()], ttl_days=30)
        assert plan["rows"][0]["action"] == "delete"
        assert plan["summary"]["delete"] == 1

    def test_missing_manifest_blocks_deletion(self):
        plan = plan_of([entry(manifest=False)], ttl_days=30)
        assert plan["rows"][0]["action"] == "keep"
        assert "no manifest" in plan["rows"][0]["reason"]
        assert plan["summary"]["no_manifest"] == 1

    def test_manifest_unknown_blocks_deletion(self):
        # None = old scanner, can't tell — must block (fail-safe)
        plan = plan_of([entry(manifest=None)], ttl_days=30)
        assert plan["rows"][0]["action"] == "keep"
        assert "no manifest" in plan["rows"][0]["reason"]

    def test_aioli_row_blocks_deletion(self):
        plan = plan_of([entry(name="acme/Serving")], ttl_days=30,
                       aioli_uris=["pvc://models-pvc/large-models/acme/Serving?containerPath=/mnt/models"])
        assert plan["rows"][0]["action"] == "keep"
        assert "AIOLI" in plan["rows"][0]["reason"]

    def test_live_job_blocks_deletion(self):
        plan = plan_of(
            [entry(name="acme/X", cachepath="/mnt/custom/models--acme--X")],
            ttl_days=30,
            live_jobs=[{"namespace": "ns", "job_name": "md-x-1", "model_name": "acme/X", "cache_root": "/mnt/custom"}],
        )
        assert plan["rows"][0]["action"] == "keep"
        assert "live job ns/md-x-1" in plan["rows"][0]["reason"]

    def test_each_key_alone_is_insufficient_but_required(self):
        """The one-key-at-a-time proof: with the other two keys PRESENT, removing
        exactly one key flips delete -> keep, for every key."""
        base = {"ttl_days": 30, "aioli_uris": [], "live_jobs": []}
        # key 1 removed (no manifest)
        assert plan_of([entry(manifest=False)], **base)["rows"][0]["action"] == "keep"
        # key 2 removed (AIOLI row present)
        assert plan_of([entry(name="acme/S")], aioli_uris=["pvc://models-pvc/l/large-models/acme/S"], live_jobs=[])[
            "rows"
        ][0]["action"] == "keep"
        # key 3 removed (live job pins the root)
        assert plan_of(
            [entry(cachepath="/mnt/r/models--acme--L")],
            live_jobs=[{"namespace": "n", "job_name": "j", "model_name": "acme/L", "cache_root": "/mnt/r"}],
        )["rows"][0]["action"] == "keep"

    def test_below_ttl_keeps_even_when_all_keys_would_allow(self):
        plan = plan_of([entry(mtime=NOW - 5 * 86400)], ttl_days=30)
        assert plan["rows"][0]["action"] == "keep"
        assert "below TTL" in plan["rows"][0]["reason"]

    def test_no_ttl_line_configured_keeps(self):
        plan = plan_of([entry()], ttl_days=0)
        assert plan["rows"][0]["action"] == "keep"
        assert "no TTL" in plan["rows"][0]["reason"]

    def test_protected_glob_blocks_deletion(self):
        plan = plan_of([entry(name="deepseek-ai/DeepSeek-V4")], ttl_days=30, protected_models=["deepseek-ai/*"])
        assert plan["rows"][0]["action"] == "keep"
        assert "protectedModels" in plan["rows"][0]["reason"]

    def test_protected_glob_by_cache_dir_name(self):
        plan = plan_of([entry(name="Qwen/Qwen3", cachepath="/mnt/l/models--Qwen--Qwen3")], ttl_days=30,
                       protected_models=["models--Qwen--*"])
        assert plan["rows"][0]["action"] == "keep"


# ---------------------------------------------------------------------------
# live-job protection semantics
# ---------------------------------------------------------------------------


class TestLiveJobProtection:
    def test_job_root_parent_match(self):
        e = entry(cachepath="/mnt/l/models--acme--A")
        job = {"namespace": "ns", "job_name": "j", "model_name": "acme/A", "cache_root": "/mnt/l"}
        assert live_job_protects(e, [job]) is job

    def test_finished_job_still_protects_while_it_exists(self):
        """A Completed downloader job is TTL-cleaned within the hour — while it
        exists its pod may still be writing; status is deliberately ignored."""
        e = entry(cachepath="/mnt/l/models--acme--A")
        job = {"namespace": "ns", "job_name": "j", "model_name": "acme/A", "cache_root": "/mnt/l", "status": "succeeded"}
        assert live_job_protects(e, [job]) is job

    def test_name_match_only_when_no_root_annotation(self):
        e = entry(name="acme/A", cachepath="/mnt/l/models--acme--A")
        with_name_only = {"namespace": "ns", "job_name": "j", "model_name": "acme/A", "cache_root": ""}
        assert live_job_protects(e, [with_name_only]) is with_name_only
        # a DIFFERENT root with the same model name must NOT protect this dir
        other_root = {"namespace": "ns2", "job_name": "j2", "model_name": "acme/A", "cache_root": "/mnt/other"}
        assert live_job_protects(e, [other_root]) is None

    def test_debug_and_scan_jobs_protect_too(self):
        e = entry(cachepath="/mnt/l/models--acme--A")
        debug = {"namespace": "ns", "job_name": "md-debug-1", "model_name": "", "cache_root": "/mnt/l"}
        assert live_job_protects(e, [debug]) is debug


# ---------------------------------------------------------------------------
# TTL / LRU line
# ---------------------------------------------------------------------------


class TestTtlAndLru:
    def test_is_expired(self):
        e = {"mtime": NOW - 31 * 86400}
        assert is_expired(e, ttl_days=30, now=NOW)
        assert not is_expired(e, ttl_days=30, now=NOW - 2 * 86400)
        assert not is_expired(e, ttl_days=None, now=NOW)
        assert not is_expired({"mtime": 0}, ttl_days=30, now=NOW)  # unknown mtime never expires

    def test_min_keep_protects_newest_candidates(self):
        entries = [
            entry(name="acme/A", mtime=NOW - 90 * 86400, cachepath="/mnt/l/models--acme--A"),
            entry(name="acme/B", mtime=NOW - 80 * 86400, cachepath="/mnt/l/models--acme--B"),
            entry(name="acme/C", mtime=NOW - 70 * 86400, cachepath="/mnt/l/models--acme--C"),
        ]
        plan = plan_of(entries, ttl_days=30, min_keep=2)
        actions = {r["model_name"]: r["action"] for r in plan["rows"]}
        assert actions == {"acme/A": "delete", "acme/B": "keep", "acme/C": "keep"}
        assert "LRU min_keep=2" in next(r["reason"] for r in plan["rows"] if r["model_name"] == "acme/B")
        assert plan["summary"]["delete_bytes"] == 100  # only A's bytes

    def test_min_keep_zero_is_no_line(self):
        entries = [entry(name="acme/A", cachepath="/mnt/l/models--acme--A")]
        plan = plan_of(entries, ttl_days=30, min_keep=0)
        assert plan["rows"][0]["action"] == "delete"


# ---------------------------------------------------------------------------
# report shape (the dry-run UI contract)
# ---------------------------------------------------------------------------


class TestReportShape:
    def test_row_fields_and_summary(self):
        plan = plan_of(
            [entry(name="acme/Serving", nbytes=500), entry(name="acme/Free", nbytes=700)],
            ttl_days=30,
            aioli_uris=["pvc://models-pvc/large-models/acme/Serving"],
        )
        for row in plan["rows"]:
            assert set(row) >= {"model_name", "cachepath", "bytes", "mtime", "age_days", "manifest", "scanned", "action", "reason"}
        s = plan["summary"]
        assert s["total"] == 2 and s["delete"] == 1 and s["keep"] == 1
        assert s["delete_bytes"] == 700
        assert s["aioli_protected"] == 1
        assert plan["ttl_days"] == 30
        assert isinstance(plan["generated_at"], float)
        assert isinstance(plan["rows"], list)

    def test_empty_enumeration_is_a_valid_report(self):
        plan = plan_of([], ttl_days=30)
        assert plan["rows"] == []
        assert plan["summary"]["total"] == 0 and plan["summary"]["delete"] == 0

    def test_scanned_verdict_is_reported_not_enforced(self):
        """GC never deletes because of a scan verdict — that is the A2 gate's
        job at download time. A suspicious verdict shows in the row."""
        plan = plan_of([entry(scanned="suspicious")], ttl_days=30)
        assert plan["rows"][0]["action"] == "delete"  # TTL is the only deletion line
        assert plan["rows"][0]["scanned"] == "suspicious"


# ---------------------------------------------------------------------------
# AIOLI rows flattening
# ---------------------------------------------------------------------------


class TestParseAioliRows:
    def test_tuples_and_dicts_and_nulls(self):
        rows = [("a", "pvc://x/y"), {"name": "b", "uri": "s3://b/k"}, ("c", None), ("d", ""), "garbage"]
        uris = parse_aioli_rows(rows)
        assert uris == ["pvc://x/y", "s3://b/k"]

    def test_empty(self):
        assert parse_aioli_rows([]) == []
        assert parse_aioli_rows(None) == []
