"""Unit tests for MD-B preflight + per-namespace quota (preflight.py + queue gate).

Covers the preflight math (fit / refuse with bytes-needed vs bytes-free /
warn-unknown), the quota decision (boundary, per-namespace override, unknown
usage), per-namespace usage attribution from scanner entries + job
annotations/manifests, the cached PreflightService behavior (TTL reuse,
usage deltas folded between scans), and the queue.submit gate ordering
(refusal happens BEFORE any Job creation / semaphore slot).
"""

import asyncio
import time

import pytest

from model_downloader.app.preflight import (
    PreflightConfig,
    PreflightError,
    PreflightService,
    QuotaExceededError,
    cache_dir_name,
    decide_disk_preflight,
    decide_quota,
    human_bytes,
    parse_k8s_quantity,
    repo_id_from_cache_dir,
    usage_by_namespace,
)
from model_downloader.app.queue import JobQueue

# ---------------------------------------------------------------------------
# pure math: disk preflight
# ---------------------------------------------------------------------------


class TestDiskPreflight:
    def test_fits(self):
        decision, reason = decide_disk_preflight(estimate_bytes=100, capacity_bytes=1000, used_bytes=500)
        assert decision == "ok"
        assert "100 bytes needed" in reason and "500 bytes free" in reason

    def test_refuse_names_bytes_needed_vs_free(self):
        decision, reason = decide_disk_preflight(estimate_bytes=900, capacity_bytes=1000, used_bytes=500)
        assert decision == "refuse"
        assert "900 bytes" in reason and "500 bytes free" in reason
        assert "capacity 1000 - used 500" in reason

    def test_refuse_accounts_pending_and_margin(self):
        decision, reason = decide_disk_preflight(
            estimate_bytes=900, capacity_bytes=1000, used_bytes=500, pending_bytes=50, safety_margin_bytes=100
        )
        assert decision == "refuse"
        assert "pending 50" in reason and "margin 100" in reason

    def test_exact_fit_is_ok(self):
        decision, _ = decide_disk_preflight(estimate_bytes=500, capacity_bytes=1000, used_bytes=500)
        assert decision == "ok"

    def test_unknown_estimate_is_ok_not_refusal(self):
        decision, reason = decide_disk_preflight(estimate_bytes=None, capacity_bytes=1000, used_bytes=500)
        assert decision == "ok"
        assert "unknown" in reason

    def test_unknown_capacity_or_usage_warns(self):
        decision, _reason = decide_disk_preflight(estimate_bytes=100, capacity_bytes=1000, used_bytes=None)
        assert decision == "warn"
        decision, _reason2 = decide_disk_preflight(estimate_bytes=100, capacity_bytes=None, used_bytes=None)
        assert decision == "warn"


# ---------------------------------------------------------------------------
# pure math: quota
# ---------------------------------------------------------------------------


class TestQuotaDecision:
    def test_no_quota_is_off(self):
        decision, _ = decide_quota(namespace="ns", estimate_bytes=10**12, usage_bytes=0, quota_bytes=None)
        assert decision == "off"

    def test_within_quota_ok(self):
        decision, reason = decide_quota(namespace="ns", estimate_bytes=100, usage_bytes=500, quota_bytes=1000)
        assert decision == "ok"
        assert "600 of 1000" in reason

    def test_boundary_exactly_at_quota_refuses(self):
        decision, reason = decide_quota(namespace="ns", estimate_bytes=1, usage_bytes=1000, quota_bytes=1000)
        assert decision == "refuse"
        assert "1000 bytes used + 1 requested > 1000 quota" in reason
        assert "'ns'" in reason

    def test_unknown_usage_warns(self):
        decision, _reason = decide_quota(namespace="ns", estimate_bytes=100, usage_bytes=None, quota_bytes=1000)
        assert decision == "warn"

    def test_already_over_quota_with_unknown_estimate_refuses(self):
        decision, _ = decide_quota(namespace="ns", estimate_bytes=None, usage_bytes=1500, quota_bytes=1000)
        assert decision == "refuse"


# ---------------------------------------------------------------------------
# quantities + naming helpers
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_parse_k8s_quantity(self):
        assert parse_k8s_quantity("500Gi") == 500 * 1024**3
        assert parse_k8s_quantity("1Ti") == 1024**4
        assert parse_k8s_quantity("262144") == 262144
        assert parse_k8s_quantity(None) is None
        assert parse_k8s_quantity("") is None
        assert parse_k8s_quantity("garbage") is None

    def test_cache_dir_roundtrip(self):
        assert cache_dir_name("acme/Test-Model") == "models--acme--Test-Model"
        assert repo_id_from_cache_dir("models--acme--Test-Model") == "acme/Test-Model"
        assert repo_id_from_cache_dir("not-a-cache-dir") == ""
        assert repo_id_from_cache_dir("") == ""

    def test_human_bytes(self):
        assert human_bytes(None) == "unknown"
        assert human_bytes(512) == "512 B"
        assert human_bytes(1536) == "1.5 KiB"
        assert human_bytes(5 * 1024**3) == "5.0 GiB"


# ---------------------------------------------------------------------------
# per-namespace usage attribution
# ---------------------------------------------------------------------------


class TestUsageAttribution:
    def test_exact_cache_root_annotation_attributes(self):
        entries = [{"cachepath": "/mnt/custom/models--acme--A", "bytes": 100, "model_name": "acme/A"}]
        jobs = [{"namespace": "ns1", "cache_root": "/mnt/custom", "model_name": "acme/A"}]
        usage = usage_by_namespace(entries, live_jobs=jobs)
        assert usage == {"ns1": 100}

    def test_parent_dir_of_cache_dir_matches_root(self):
        entries = [{"cachepath": "/mnt/large-models/models--acme--A", "bytes": 100, "model_name": "acme/A"}]
        jobs = [{"namespace": "ns1", "cache_root": "/mnt/large-models/", "model_name": "acme/A"}]
        assert usage_by_namespace(entries, live_jobs=jobs) == {"ns1": 100}

    def test_model_name_fallback_when_no_root_match(self):
        entries = [{"cachepath": "/mnt/other/models--acme--A", "bytes": 100, "model_name": "acme/A"}]
        jobs = [{"namespace": "ns2", "cache_root": "/mnt/large-models", "model_name": "acme/A"}]
        assert usage_by_namespace(entries, live_jobs=jobs) == {"ns2": 100}

    def test_unattributable_sums_under_marker_key(self):
        entries = [{"cachepath": "/mnt/nowhere/models--x--Y", "bytes": 42, "model_name": "x/Y"}]
        usage = usage_by_namespace(entries, live_jobs=[])
        assert usage == {"_unattributed": 42}

    def test_same_model_two_roots_stay_distinct(self):
        entries = [
            {"cachepath": "/mnt/alice/models--acme--A", "bytes": 100, "model_name": "acme/A"},
            {"cachepath": "/mnt/bob/models--acme--A", "bytes": 200, "model_name": "acme/A"},
        ]
        jobs = [
            {"namespace": "alice-ns", "cache_root": "/mnt/alice", "model_name": "acme/A"},
            {"namespace": "bob-ns", "cache_root": "/mnt/bob", "model_name": "acme/A"},
        ]
        usage = usage_by_namespace(entries, live_jobs=jobs)
        assert usage == {"alice-ns": 100, "bob-ns": 200}

    def test_bad_bytes_and_missing_paths_are_skipped(self):
        entries = [
            {"cachepath": "/mnt/x/models--a--B", "bytes": "not-a-number", "model_name": "a/B"},
            {"cachepath": "", "bytes": 10, "model_name": "a/B"},
            {"cachepath": "/mnt/x/models--a--C", "bytes": None, "model_name": "a/C"},
        ]
        # the parseable entry (10 bytes, empty cachepath) still counts; the
        # unparseable/missing ones are skipped without crashing
        usage = usage_by_namespace(entries, live_jobs=[])
        assert usage.get("_unattributed") == 10
        assert sum(usage.values()) == 10

    def test_manifest_style_attribution_matches_job_history(self):
        """Finished jobs (job history) attribute usage exactly like live jobs —
        this is the manifest.json-ownership channel A1 opened for B."""
        entries = [{"cachepath": "/mnt/large-models/models--acme--A", "bytes": 7, "model_name": "acme/A"}]
        history = [{"namespace": "ns9", "cache_root": "/mnt/large-models", "model_name": "acme/A"}]
        assert usage_by_namespace(entries, live_jobs=history) == {"ns9": 7}


# ---------------------------------------------------------------------------
# PreflightService (cached, injected k8s)
# ---------------------------------------------------------------------------


class FakeK8s:
    def __init__(self, capacity=10 * 1024**3):
        self.capacity = capacity
        self.capacity_reads = 0
        self.scan_runs = 0
        self.scan_result: list = []
        self.scan_error = ""

    async def read_pvc_capacity(self, namespace, pvc_name):
        self.capacity_reads += 1
        return self.capacity

    async def run_usage_scan(self):  # not used directly; scan injected below
        raise NotImplementedError


class TestPreflightService:
    def test_capacity_cached_across_calls(self):
        async def go():
            k8s = FakeK8s(capacity=1234)
            svc = PreflightService(PreflightConfig(), k8s=k8s, usage_ttl=60)
            a = await svc.get_capacity("ns", "pvc")
            b = await svc.get_capacity("ns", "pvc")
            assert a == b == 1234
            assert k8s.capacity_reads == 1  # TTL cache: one read

        asyncio.run(go())

    def test_usage_scan_runs_once_within_ttl_and_folds_deltas(self):
        async def go():
            k8s = FakeK8s()
            svc = PreflightService(PreflightConfig(), k8s=k8s, usage_ttl=60)

            async def fake_scan(**kwargs):
                svc.scan_runs = getattr(svc, "scan_runs", 0) + 1
                return [{"model_name": "acme/A", "cachepath": "/mnt/l/models--acme--A", "bytes": 100}]

            svc._run_usage_scan = fake_scan  # type: ignore[method-assign]
            jobs = [{"namespace": "ns1", "cache_root": "/mnt/l", "model_name": "acme/A"}]
            used, usage = await svc.get_usage(namespace="ns1", pvc_name="p", scan_root="/mnt/l", scan_image="img", jobs=jobs)
            assert used == 100 and usage["ns1"] == 100
            # second call inside TTL: cached, no re-scan
            used2, _usage2 = await svc.get_usage(namespace="ns1", pvc_name="p", scan_root="/mnt/l", scan_image="img", jobs=jobs)
            assert used2 == 100
            assert svc.scan_runs == 1
            # a submission's delta is folded in immediately
            svc.record_usage_delta("ns1", 50)
            _used3, usage3 = await svc.get_usage(namespace="ns1", pvc_name="p", scan_root="/mnt/l", scan_image="img", jobs=jobs)
            assert usage3["ns1"] == 150

        asyncio.run(go())

    def test_failed_scan_keeps_last_known_usage(self):
        async def go():
            k8s = FakeK8s()
            svc = PreflightService(PreflightConfig(), k8s=k8s, usage_ttl=0.01)

            async def good_scan(**kwargs):
                return [{"model_name": "acme/A", "cachepath": "/mnt/l/models--acme--A", "bytes": 100}]

            async def bad_scan(**kwargs):
                return None

            svc._run_usage_scan = good_scan  # type: ignore[method-assign]
            jobs = [{"namespace": "ns1", "cache_root": "/mnt/l", "model_name": "acme/A"}]
            await svc.get_usage(namespace="ns1", pvc_name="p", scan_root="/mnt/l", scan_image="img", jobs=jobs)
            svc._run_usage_scan = bad_scan  # type: ignore[method-assign]
            await asyncio.sleep(0.02)
            used, usage = await svc.get_usage(namespace="ns1", pvc_name="p", scan_root="/mnt/l", scan_image="img", jobs=jobs)
            # stale-but-known beats zeroing a namespace's usage
            assert used == 100 and usage["ns1"] == 100

        asyncio.run(go())

    def test_size_estimate_cached_and_unknown_on_failure(self):
        async def go():
            svc = PreflightService(PreflightConfig())
            svc._sizes["cached/repo"] = (12345, time.time())
            assert await svc.get_model_size("cached/repo") == 12345
            # unreachable hub -> None (warn-only path)
            assert await svc.get_model_size("no/such-repo-xyz-404") is None

        asyncio.run(go())


# ---------------------------------------------------------------------------
# the submit-time gate end-to-end (config + service, no cluster)
# ---------------------------------------------------------------------------


class TestPreflightGate:
    def _service(self, *, capacity, entries, jobs, config, size=1000):
        async def go_impl():
            k8s = FakeK8s(capacity=capacity)
            svc = PreflightService(config, k8s=k8s, usage_ttl=60)
            svc._sizes["acme/M"] = (size, time.time())

            async def fake_scan(**kwargs):
                return entries

            svc._run_usage_scan = fake_scan  # type: ignore[method-assign]
            return svc

        return go_impl()

    def test_refuse_raises_with_bytes_detail(self):
        async def go():
            # 1000-capacity PVC, 900 used, 1000-byte model -> refuse
            svc = await self._service(
                capacity=1000,
                entries=[{"model_name": "a/B", "cachepath": "/mnt/l/models--a--B", "bytes": 900}],
                jobs=[{"namespace": "ns", "cache_root": "/mnt/l", "model_name": "a/B"}],
                config=PreflightConfig(enabled=True, mode="refuse"),
                size=1000,
            )
            with pytest.raises(PreflightError) as exc:
                await svc.check(
                    namespace="ns",
                    repo_id="acme/M",
                    estimate_bytes=1000,
                    pvc_name="p",
                    scan_root="/mnt/l",
                    scan_image="img",
                    jobs=[{"namespace": "ns", "cache_root": "/mnt/l", "model_name": "a/B"}],
                )
            assert "1000 bytes" in exc.value.detail and "bytes free" in exc.value.detail

        asyncio.run(go())

    def test_warn_mode_reports_but_does_not_raise(self):
        async def go():
            svc = await self._service(
                capacity=1000,
                entries=[{"model_name": "a/B", "cachepath": "/mnt/l/models--a--B", "bytes": 900}],
                jobs=[{"namespace": "ns", "cache_root": "/mnt/l", "model_name": "a/B"}],
                config=PreflightConfig(enabled=True, mode="warn"),
                size=1000,
            )
            report = await svc.check(
                namespace="ns",
                repo_id="acme/M",
                estimate_bytes=1000,
                pvc_name="p",
                scan_root="/mnt/l",
                scan_image="img",
                jobs=[{"namespace": "ns", "cache_root": "/mnt/l", "model_name": "a/B"}],
            )
            assert report["disk"]["decision"] == "refuse"  # decision recorded...
            # ...but nothing raised (warn mode)

        asyncio.run(go())

    def test_disabled_returns_off_report(self):
        async def go():
            svc = await self._service(capacity=1000, entries=[], jobs=[], config=PreflightConfig(enabled=False), size=1000)
            report = await svc.check(
                namespace="ns", repo_id="acme/M", estimate_bytes=None, pvc_name="p", scan_root="/mnt/l", scan_image="img"
            )
            assert report["disk"]["decision"] == "off" and report["quota"]["decision"] == "off"

        asyncio.run(go())

    def test_quota_refuse_raises_quota_error(self):
        async def go():
            svc = await self._service(
                capacity=10**9,
                entries=[{"model_name": "a/B", "cachepath": "/mnt/l/models--a--B", "bytes": 900}],
                jobs=[{"namespace": "ns", "cache_root": "/mnt/l", "model_name": "a/B"}],
                config=PreflightConfig(enabled=True, mode="refuse", quota_default_bytes=1000, quota_mode="refuse"),
                size=500,
            )
            with pytest.raises(QuotaExceededError) as exc:
                await svc.check(
                    namespace="ns",
                    repo_id="acme/M",
                    estimate_bytes=500,
                    pvc_name="p",
                    scan_root="/mnt/l",
                    scan_image="img",
                    jobs=[{"namespace": "ns", "cache_root": "/mnt/l", "model_name": "a/B"}],
                )
            assert "quota" in exc.value.detail

        asyncio.run(go())

    def test_per_namespace_quota_override(self):
        cfg = PreflightConfig(quota_default_bytes=100, quota_namespaces={"big": 10**6}, quota_mode="refuse")
        assert cfg.quota_for("small") == 100
        assert cfg.quota_for("big") == 10**6

    def test_quota_warn_mode_does_not_raise(self):
        async def go():
            svc = await self._service(
                capacity=10**9,
                entries=[{"model_name": "a/B", "cachepath": "/mnt/l/models--a--B", "bytes": 900}],
                jobs=[{"namespace": "ns", "cache_root": "/mnt/l", "model_name": "a/B"}],
                config=PreflightConfig(enabled=True, mode="refuse", quota_default_bytes=1000, quota_mode="warn"),
                size=500,
            )
            report = await svc.check(
                namespace="ns",
                repo_id="acme/M",
                estimate_bytes=500,
                pvc_name="p",
                scan_root="/mnt/l",
                scan_image="img",
                jobs=[{"namespace": "ns", "cache_root": "/mnt/l", "model_name": "a/B"}],
            )
            assert report["quota"]["decision"] == "refuse"  # recorded, not raised (warn)

        asyncio.run(go())


# ---------------------------------------------------------------------------
# queue.submit gate ordering
# ---------------------------------------------------------------------------


class TestQueueSubmitGate:
    class RefusingService:
        code = "quota_exceeded"

        def __init__(self):
            self.checked = False

        async def check(self, **kwargs):
            self.checked = True
            raise QuotaExceededError("namespace quota exceeded: 1 bytes used + 2 requested > 2 quota for namespace 'ns'")

        def record_usage_delta(self, *a, **k):
            pass

        def record_pending(self, *a, **k):
            pass

    class FakeK8s:
        def __init__(self):
            self.jobs_created = 0

        async def create_job(self, *a, **k):
            self.jobs_created += 1
            return ("secret", "job")

        async def wait_for_job(self, *a, **k):
            return True, "succeeded"

    def _queue(self):
        q = JobQueue(max_concurrency=1, k8s=self.FakeK8s(), pvc_name="models-pvc")
        q._sem = asyncio.Semaphore(1)  # start() without an extra event loop
        return q

    def test_refusal_happens_before_job_creation(self):
        async def go():
            q = self._queue()
            svc = self.RefusingService()
            with pytest.raises(QuotaExceededError):
                await q.submit("ns", "acme/M", "tok", preflight=svc)
            assert svc.checked
            # no record, no job creation
            assert q.jobs == {}
            assert q.k8s.jobs_created == 0  # type: ignore[attr-defined]

        asyncio.run(go())

    def test_s3_submissions_bypass_preflight(self):
        async def go():
            q = self._queue()
            svc = self.RefusingService()
            record = await q.submit("ns", "acme/M", "tok", storage="s3", s3_path="s3://bucket/p/", preflight=svc)
            assert not svc.checked  # never consulted for S3
            assert record.status in ("queued", "running", "succeeded")

        asyncio.run(go())

    def test_no_preflight_keeps_old_behavior(self):
        async def go():
            q = self._queue()
            record = await q.submit("ns", "acme/M", "tok")
            assert record.id in q.jobs

        asyncio.run(go())

    def test_accepted_submission_charges_usage_and_pending(self):
        class AcceptingService:
            def __init__(self):
                self.deltas = []
                self.pending = []

            async def check(self, **kwargs):
                return {"estimate_bytes": 4321}

            def record_usage_delta(self, ns, b):
                self.deltas.append((ns, b))

            def record_pending(self, ns, b):
                self.pending.append((ns, b))

        async def go():
            q = self._queue()
            svc = AcceptingService()
            await q.submit("ns", "acme/M", "tok", preflight=svc)
            assert svc.deltas == [("ns", 4321)]
            assert svc.pending == [("ns", 4321)]

        asyncio.run(go())

    def test_submit_passes_dicts_not_jobrecords_to_preflight(self):
        """Regression: with a tracked job in the map, the next submit handed
        preflight raw JobRecord objects, and usage_by_namespace died on
        job.get("namespace") — 'JobRecord' object has no attribute 'get'."""

        class CapturingService:
            def __init__(self):
                self.captured = None

            async def check(self, **kwargs):
                self.captured = kwargs.get("jobs")
                return {"estimate_bytes": None}

            def record_usage_delta(self, *a, **k):
                pass

            def record_pending(self, *a, **k):
                pass

        async def go():
            q = self._queue()
            svc = CapturingService()
            first = await q.submit("ns", "acme/M", "tok", preflight=svc)
            assert first.id in q.jobs  # a tracked live job is what triggers it
            await q.submit("ns", "acme/M2", "tok", preflight=svc)
            assert svc.captured
            assert all(isinstance(j, dict) for j in svc.captured)
            assert svc.captured[0]["model_name"] == "acme/M"

        asyncio.run(go())
