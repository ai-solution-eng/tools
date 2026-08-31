"""Unit tests for the container-derived resource budgets (no cgroup files needed).

The cgroup reads are mocked at the file-read seam, so the limit detection and
proportional budget math are exercised on any platform (including local dev,
where /sys/fs/cgroup does not exist and the budgets must come back empty).
"""

import pytest

from sqlhandler import resources
from sqlhandler.resources import (
    _memory_budget_string,
    container_cpu_count,
    container_memory_bytes,
    duckdb_budget,
    process_rss_bytes,
)


@pytest.fixture(autouse=True)
def _reset_warning_flag():
    """Keep the once-only warning flag from leaking between tests."""
    resources._warned_no_cgroup = False
    yield
    resources._warned_no_cgroup = False


def _mock_reads(monkeypatch, files: dict[str, str]):
    """Point _read_cgroup_value at an in-memory 'filesystem'."""
    monkeypatch.setattr(resources, "_read_cgroup_value", lambda *paths: next(
        (files[p] for p in paths if p in files), None
    ))


# ------------------------------------------------------------------ memory
def test_memory_limit_cgroup_v2_numeric(monkeypatch):
    _mock_reads(monkeypatch, {"/sys/fs/cgroup/memory.max": "2147483648"})
    assert container_memory_bytes() == 2147483648


def test_memory_limit_cgroup_v2_max_is_unlimited(monkeypatch):
    _mock_reads(monkeypatch, {"/sys/fs/cgroup/memory.max": "max"})
    assert container_memory_bytes() is None


def test_memory_limit_cgroup_v1_sentinel_is_unlimited(monkeypatch):
    sentinel = str(9223372036854771712)  # v1's page-aligned "no limit"
    _mock_reads(monkeypatch, {"/sys/fs/cgroup/memory/memory.limit_in_bytes": sentinel})
    assert container_memory_bytes() is None


def test_memory_limit_missing_files(monkeypatch):
    _mock_reads(monkeypatch, {})
    assert container_memory_bytes() is None


def test_memory_limit_invalid_value(monkeypatch):
    _mock_reads(monkeypatch, {"/sys/fs/cgroup/memory.max": "not-a-number"})
    assert container_memory_bytes() is None


# --------------------------------------------------------------------- cpu
@pytest.mark.parametrize(
    ("cpu_max", "expected"),
    [
        ("100000 100000", 1.0),   # 1 CPU (the pod's limit)
        ("200000 100000", 2.0),   # 2 CPUs
        ("50000 100000", 0.5),    # half a CPU -> threads floor to 1
        ("max 100000", None),     # unlimited
        ("bogus", None),          # malformed
    ],
)
def test_cpu_limit_cgroup_v2(monkeypatch, cpu_max, expected):
    _mock_reads(monkeypatch, {"/sys/fs/cgroup/cpu.max": cpu_max})
    assert container_cpu_count() == expected


def test_cpu_limit_cgroup_v1_fallback(monkeypatch):
    _mock_reads(
        monkeypatch,
        {
            "/sys/fs/cgroup/cpu/cpu.cfs_quota_us": "100000",
            "/sys/fs/cgroup/cpu/cpu.cfs_period_us": "100000",
        },
    )
    assert container_cpu_count() == 1.0


def test_cpu_limit_cgroup_v1_unlimited_quota(monkeypatch):
    _mock_reads(
        monkeypatch,
        {
            "/sys/fs/cgroup/cpu/cpu.cfs_quota_us": "-1",
            "/sys/fs/cgroup/cpu/cpu.cfs_period_us": "100000",
        },
    )
    assert container_cpu_count() is None


# ----------------------------------------------------------------- budget
def test_budget_is_proportional_to_pod_limit(monkeypatch):
    _mock_reads(
        monkeypatch,
        {
            "/sys/fs/cgroup/memory.max": "2147483648",  # 2Gi pod limit
            "/sys/fs/cgroup/cpu.max": "100000 100000",  # 1 CPU
        },
    )
    budget = duckdb_budget()
    assert budget["memory_limit"] == "1.2GiB"  # 0.6 x 2Gi, rounded to 1 decimal
    assert budget["threads"] == 1              # floor of the 1-CPU limit
    assert budget["container_memory_bytes"] == 2147483648
    assert budget["container_cpu_count"] == 1.0
    assert budget["temp_directory"] == "/tmp/sqlhandler-duckdb-spill"


def test_budget_fraction_overridable(monkeypatch):
    monkeypatch.setenv("SQLHANDLER_DUCKDB_MEMORY_FRACTION", "0.25")
    _mock_reads(monkeypatch, {"/sys/fs/cgroup/memory.max": "2147483648"})
    assert duckdb_budget()["memory_limit"] == "512MiB"


def test_budget_fraction_zero_disables_cap(monkeypatch):
    monkeypatch.setenv("SQLHANDLER_DUCKDB_MEMORY_FRACTION", "0")
    _mock_reads(
        monkeypatch,
        {"/sys/fs/cgroup/memory.max": "2147483648", "/sys/fs/cgroup/cpu.max": "100000 100000"},
    )
    budget = duckdb_budget()
    assert "memory_limit" not in budget
    assert budget["threads"] == 1  # cpu budget still applies


def test_budget_fraction_invalid_falls_back(monkeypatch):
    monkeypatch.setenv("SQLHANDLER_DUCKDB_MEMORY_FRACTION", "banana")
    _mock_reads(monkeypatch, {"/sys/fs/cgroup/memory.max": "2147483648"})
    assert duckdb_budget()["memory_limit"] == "1.2GiB"


def test_budget_empty_when_no_cgroups(monkeypatch):
    _mock_reads(monkeypatch, {})
    assert duckdb_budget() == {}


def test_budget_temp_dir_overridable(monkeypatch):
    monkeypatch.setenv("SQLHANDLER_DUCKDB_TEMP_DIR", "/data/spill")
    _mock_reads(monkeypatch, {"/sys/fs/cgroup/cpu.max": "100000 100000"})
    assert duckdb_budget()["temp_directory"] == "/data/spill"


# ---------------------------------------------------------------- helpers
def test_memory_budget_string_formats():
    assert _memory_budget_string(2147483648) == "2.0GiB"
    assert _memory_budget_string(int(2.4 * 1024**3)) == "2.4GiB"
    assert _memory_budget_string(768 * 1024 * 1024) == "768MiB"


def test_process_rss_parses_vm_rss(monkeypatch):
    status = (
        "Name:\tpython\n"
        "VmPeak:\t 1234567 kB\n"
        "VmRSS:\t     456 kB\n"
    )
    monkeypatch.setattr(resources, "_read_cgroup_value", lambda *p: status)
    assert process_rss_bytes() == 456 * 1024


def test_process_rss_none_without_proc(monkeypatch):
    monkeypatch.setattr(resources, "_read_cgroup_value", lambda *p: None)
    assert process_rss_bytes() is None
