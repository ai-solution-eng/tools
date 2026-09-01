"""Tests for the local/NFS (file) backend and provider readiness checks."""

import os

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from sqlhandler.config import FabricConfig, FileConfig, IcebergConfig, S3Config
from sqlhandler.file import FileProvider
from sqlhandler.iceberg import IcebergProvider
from sqlhandler.onelake import OneLakeProvider
from sqlhandler.s3 import S3Provider


def _write_parquet(root, rel):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    pq.write_table(pa.table({"id": [1, 2, 3], "name": ["x", "y", "z"]}), path)


def test_file_provider_lists_and_checks(tmp_path):
    _write_parquet(str(tmp_path), "orders.parquet")
    _write_parquet(str(tmp_path), "sales/customers.parquet")
    # Hive partition folder inside a table folds into the table
    _write_parquet(str(tmp_path), "sales/customers/dt=2024/x.parquet")
    _write_parquet(str(tmp_path), ".hidden/x.parquet")

    provider = FileProvider(FileConfig(root_dir=str(tmp_path)))
    tables = provider.list_tables()
    paths = [t.path for t in tables]
    assert "orders" in paths
    assert "sales/customers" in paths
    assert all("dt=" not in t.path for t in tables)
    assert all(".hidden" not in t.path for t in tables)
    assert provider.check_connection() is None

    orders = next(t for t in tables if t.path == "orders")
    assert provider.open_dataset(orders).to_table().num_rows == 3


def test_file_provider_missing_root_reports_connection_error(tmp_path):
    provider = FileProvider(FileConfig(root_dir=str(tmp_path / "missing")))
    assert provider.check_connection() is not None


def test_readiness_checks_report_failure_not_exception():
    # OneLake with invalid creds returns an error message instead of raising
    cfg = FabricConfig(
        tenant_id="t",
        client_id="c",
        client_secret="s",
        lakehouse_abfss_url="abfss://ws@onelake.dfs.fabric.microsoft.com/lh",
    )
    assert isinstance(OneLakeProvider(cfg).check_connection(), str)

    # S3 against an unreachable endpoint returns an error string
    s3 = S3Config(
        bucket="__missing__",
        anonymous=True,
        endpoint_url="http://127.0.0.1:9",
    )
    assert S3Provider(s3).check_connection() is not None

    # Iceberg against an unreachable catalog returns an error string
    ice = IcebergProvider(IcebergConfig(catalog_uri="http://127.0.0.1:9/catalog"))
    assert ice.check_connection() is not None


def test_file_provider_delta_lake(tmp_path):
    """Delta Lake tables on the NFS root are discovered and readable."""
    from deltalake import write_deltalake

    sales = tmp_path / "sales"
    sales.mkdir()
    write_deltalake(
        str(sales / "work_orders"),
        pa.table({"wo": [1, 2, 3], "desc": ["a", "b", "c"]}),
        mode="overwrite",
    )
    # parquet data file left next to the delta table must not become a table
    pq.write_table(pa.table({"id": [9]}), str(tmp_path / "standalone.parquet"))

    provider = FileProvider(FileConfig(root_dir=str(tmp_path)))
    tables = provider.list_tables()
    paths = [t.path for t in tables]
    assert "sales/work_orders" in paths
    assert "standalone" in paths

    delta = next(t for t in tables if t.format == "delta")
    assert delta.format == "delta"
    assert provider.open_dataset(delta).to_table().num_rows == 3

    # parquet file inside a delta folder is folded into the delta table only
    inner = [t for t in tables if t.format == "parquet" and "work_orders" in t.path]
    assert not inner


def test_file_provider_delta_version_invalidation(tmp_path):
    """A Delta commit is visible via the cache before the TTL expires."""
    from deltalake import write_deltalake

    from sqlhandler.engine import SqlEngine

    write_deltalake(str(tmp_path / "delta"), pa.table({"a": [1]}), mode="overwrite")
    provider = FileProvider(FileConfig(root_dir=str(tmp_path)))
    engine = SqlEngine(provider, dataset_cache_ttl=3600, cache_ttl=3600, version_check_interval=0)
    info = next(t for t in provider.list_tables() if t.format == "delta")

    ds1 = engine._open_dataset(info)
    assert ds1.to_table().num_rows == 1
    assert provider.check_version(info) is not None

    write_deltalake(str(tmp_path / "delta"), pa.table({"a": [2]}), mode="append")
    ds2 = engine._open_dataset(info)
    assert ds2 is not ds1  # version changed -> reopened
    assert ds2.to_table().num_rows == 2

    # plain parquet has no version signal
    pq.write_table(pa.table({"x": [1]}), str(tmp_path / "plain.parquet"))
    pinfo = next(t for t in provider.list_tables() if t.format == "parquet")
    assert provider.check_version(pinfo) is None


def test_file_provider_delta_version_check_throttled(tmp_path):
    """A non-zero version_check_interval defers the re-check past the commit."""
    from deltalake import write_deltalake

    from sqlhandler.engine import SqlEngine

    write_deltalake(str(tmp_path / "t"), pa.table({"a": [1]}), mode="overwrite")
    provider = FileProvider(FileConfig(root_dir=str(tmp_path)))
    engine = SqlEngine(provider, dataset_cache_ttl=3600, cache_ttl=3600, version_check_interval=10)
    info = next(t for t in provider.list_tables() if t.format == "delta")

    ds1 = engine._open_dataset(info)
    assert ds1.to_table().num_rows == 1

    write_deltalake(str(tmp_path / "t"), pa.table({"a": [2]}), mode="append")
    # throttled: within the interval the cached handle is reused as-is
    ds2 = engine._open_dataset(info)
    assert ds2 is ds1

    # force an immediate check by shrinking the interval to 0 on a new engine
    engine2 = SqlEngine(provider, dataset_cache_ttl=3600, cache_ttl=3600, version_check_interval=0)
    ds3 = engine2._open_dataset(info)
    assert ds3 is not ds1
    assert ds3.to_table().num_rows == 2


# ---------------------------------------------------------------- time travel


def _delta_engine(tmp_path):
    """A FileProvider engine over a 2-version Delta table + a plain Parquet table."""
    import pyarrow as pa
    import pyarrow.parquet as pq
    from deltalake import write_deltalake

    from sqlhandler.config import FileConfig
    from sqlhandler.engine import SqlEngine

    d = tmp_path / "workorder" / "work_order"
    d.mkdir(parents=True)
    write_deltalake(str(d), pa.table({"id": [1, 2], "amount": [10.0, 20.0]}), mode="overwrite")
    write_deltalake(str(d), pa.table({"id": [3], "amount": [30.0]}), mode="append")
    p = tmp_path / "workorder" / "plain_parquet"
    p.mkdir(parents=True)
    pq.write_table(pa.table({"id": [9]}), p / "part.parquet")

    eng = SqlEngine(FileProvider(FileConfig(root_dir=str(tmp_path))), cache_ttl=0)
    return eng


def test_scan_version_as_of_delta(tmp_path):
    eng = _delta_engine(tmp_path)
    assert eng.scan_arrow("workorder/work_order").num_rows == 3
    v0 = eng.scan_arrow("workorder/work_order", version_as_of=0)
    assert v0.num_rows == 2 and v0.to_pydict()["id"] == [1, 2]
    v1 = eng.scan_arrow("workorder/work_order", version_as_of=1)
    assert v1.num_rows == 3


def test_query_version_as_of_delta(tmp_path):
    eng = _delta_engine(tmp_path)
    r0 = eng.query_duckdb("SELECT sum(amount) AS total FROM work_order", version_as_of=0)
    assert r0.to_pydict() == {"total": [30.0]}
    r1 = eng.query_duckdb("SELECT sum(amount) AS total FROM work_order", version_as_of=1)
    assert r1.to_pydict() == {"total": [60.0]}


def test_time_travel_rejected_for_plain_parquet(tmp_path):
    eng = _delta_engine(tmp_path)
    from sqlhandler.provider import LakehouseError

    with pytest.raises(LakehouseError, match="not supported"):
        eng.scan_arrow("workorder/plain_parquet", version_as_of=0)
    with pytest.raises(LakehouseError, match="not supported"):
        eng.query_duckdb("SELECT * FROM plain_parquet", version_as_of=0)


def test_time_travel_version_validation(tmp_path):
    eng = _delta_engine(tmp_path)
    from sqlhandler.provider import LakehouseError

    with pytest.raises(LakehouseError, match="non-negative integer"):
        eng.scan_arrow("workorder/work_order", version_as_of="zero")
    with pytest.raises(LakehouseError, match="non-negative integer"):
        eng.scan_arrow("workorder/work_order", version_as_of=True)  # bool is not a version
    with pytest.raises(LakehouseError, match="non-negative integer"):
        eng.query_duckdb("SELECT * FROM work_order", version_as_of=-1)


def test_time_travel_dataset_cache_is_version_keyed(tmp_path):
    eng = _delta_engine(tmp_path)
    # Interleave current + historical reads; each read must see its own snapshot.
    assert eng.scan_arrow("workorder/work_order", version_as_of=0).num_rows == 2
    assert eng.scan_arrow("workorder/work_order").num_rows == 3
    assert eng.scan_arrow("workorder/work_order", version_as_of=0).num_rows == 2
    assert eng.scan_arrow("workorder/work_order", version_as_of=1).num_rows == 3
