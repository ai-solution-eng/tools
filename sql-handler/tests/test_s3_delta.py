"""Tests for Delta-on-S3: S3_FORMAT config, discovery tagging, delta open path.

No live S3 needed: listing is driven by a fake pyarrow filesystem and the
deltalake DeltaTable constructor is monkeypatched to capture how the provider
dials S3.
"""

import pyarrow.fs as pafs
import pytest

from sqlhandler.config import S3Config, load_s3_config
from sqlhandler.provider import LakehouseError, TableInfo
from sqlhandler.s3 import S3Provider, normalize_s3_format


class FakeFileInfo:
    def __init__(self, path, is_file=True):
        self.path = path
        self.type = pafs.FileType.File if is_file else pafs.FileType.Directory
        self.size = 10
        self.size_bytes = 10


class FakeS3FS:
    """Minimal S3FileSystem stand-in returning a canned listing."""

    def __init__(self, paths):
        self.paths = paths

    def get_file_info(self, selector):
        return [FakeFileInfo(p) for p in self.paths]


def _provider(paths, fmt="auto", bucket="lake", prefix=""):
    cfg = S3Config(bucket=bucket, prefix=prefix, access_key="a", secret_key="b", format=fmt)
    prov = S3Provider(cfg)
    prov._fs = FakeS3FS(paths)
    return prov


# ------------------------------------------------------------------- config


def test_s3_format_env_parsing():
    cfg = load_s3_config({"S3_BUCKET": "b", "S3_FORMAT": "delta"})
    assert cfg.format == "delta"
    cfg = load_s3_config({"S3_BUCKET": "b"})
    assert cfg.format == "auto"


def test_normalize_s3_format():
    assert normalize_s3_format("auto") == "auto"
    assert normalize_s3_format("DELTA") == "delta"
    assert normalize_s3_format("") == "auto"
    with pytest.raises(LakehouseError, match="Invalid S3_FORMAT"):
        normalize_s3_format("iceberg")


def test_provider_rejects_bad_format():
    with pytest.raises(LakehouseError, match="Invalid S3_FORMAT"):
        S3Provider(S3Config(bucket="b", access_key="a", secret_key="b", format="excel"))


# ---------------------------------------------------------------- discovery


def test_auto_detects_delta_and_skips_its_parquet_files():
    prov = _provider(
        [
            "lake/delta_tbl/_delta_log/00000000000000000000.json",
            "lake/delta_tbl/_delta_log/00000000000000000007.checkpoint.parquet",
            "lake/delta_tbl/part-0001.parquet",
            "lake/plain/part-0001.parquet",
            "lake/single.parquet",
        ],
        fmt="auto",
    )
    tables = {t.path: t for t in prov.list_tables()}  # bare name under default schema
    assert tables["delta_tbl"].format == "delta"
    assert tables["plain"].format == "parquet"
    assert tables["single"].format == "parquet"
    assert "delta_tbl/_delta_log" not in tables  # checkpoint not a table


def test_delta_format_tags_everything():
    prov = _provider(["lake/customers/part-0001.parquet"], fmt="delta")
    tables = prov.list_tables()
    assert tables[0].format == "delta"
    assert tables[0].name == "customers" and tables[0].schema == "default"


def test_parquet_format_ignores_delta_logs():
    prov = _provider(
        [
            "lake/delta_tbl/_delta_log/00000000000000000000.json",
            "lake/delta_tbl/part-0001.parquet",
        ],
        fmt="parquet",
    )
    tables = prov.list_tables()
    assert len(tables) == 1
    assert tables[0].format == "parquet"


def test_nested_delta_table_gets_schema_folder():
    prov = _provider(
        [
            "lake/sales/orders/_delta_log/00000000000000000000.json",
            "lake/sales/orders/part.parquet",
        ],
        fmt="auto",
    )
    tables = prov.list_tables()
    assert len(tables) == 1
    assert tables[0].schema == "sales"
    assert tables[0].name == "orders"


# ------------------------------------------------------------ delta opening


class _FakeDeltaTable:
    calls: list = []  # noqa: RUF012 - test double, list reset per test

    def __init__(self, uri, version=None, storage_options=None):
        _FakeDeltaTable.calls.append(
            {"uri": uri, "version": version, "storage_options": storage_options}
        )
        self._dataset = f"dataset-for-{uri}-v{version}"

    def to_pyarrow_dataset(self):
        return self._dataset


def test_open_delta_routes_with_storage_options(monkeypatch):
    import deltalake

    monkeypatch.setattr(deltalake, "DeltaTable", _FakeDeltaTable)
    _FakeDeltaTable.calls = []
    prov = _provider([], fmt="delta")
    info = TableInfo(name="orders", schema="sales", format="delta", location="sales/orders")
    ds = prov.open_dataset(info)
    call = _FakeDeltaTable.calls[0]
    assert ds == "dataset-for-s3://lake/sales/orders-vNone"
    assert call["uri"] == "s3://lake/sales/orders"
    opts = call["storage_options"]
    assert opts["aws_access_key_id"] == "a"
    assert opts["aws_secret_access_key"] == "b"
    assert opts["aws_region"] == "us-east-1"
    assert "aws_endpoint" not in opts  # AWS default: no endpoint override


def test_open_delta_version_and_minio_options(monkeypatch):
    import deltalake

    monkeypatch.setattr(deltalake, "DeltaTable", _FakeDeltaTable)
    _FakeDeltaTable.calls = []
    cfg = S3Config(
        bucket="lake",
        access_key="a",
        secret_key="b",
        endpoint_url="http://127.0.0.1:9000",
        use_ssl=False,
        format="delta",
    )
    prov = S3Provider(cfg)
    prov._fs = FakeS3FS([])
    info = TableInfo(name="orders", schema="default", format="delta")
    ds = prov.open_dataset(info, version=3)
    call = _FakeDeltaTable.calls[0]
    assert call["version"] == 3
    assert ds == f"dataset-for-{prov.table_uri(info)}-v3"
    assert call["storage_options"]["aws_endpoint"] == "http://127.0.0.1:9000"
    assert call["storage_options"]["aws_allow_http"] == "true"


def test_open_delta_anonymous_skip_signature(monkeypatch):
    import deltalake

    monkeypatch.setattr(deltalake, "DeltaTable", _FakeDeltaTable)
    _FakeDeltaTable.calls = []
    prov = _provider([], fmt="delta")
    prov.config = S3Config(bucket="lake", anonymous=True, format="delta")
    prov._fs = FakeS3FS([])
    prov.open_dataset(TableInfo(name="pub", format="delta"))
    assert _FakeDeltaTable.calls[0]["storage_options"]["aws_skip_signature"] == "true"


def test_open_delta_error_wrapped(monkeypatch):
    import deltalake

    class Boom:
        def __init__(self, *a, **k):
            raise RuntimeError("no such bucket")

    monkeypatch.setattr(deltalake, "DeltaTable", Boom)
    prov = _provider([], fmt="delta")
    with pytest.raises(LakehouseError, match="Could not open S3 Delta table"):
        prov.open_dataset(TableInfo(name="x", format="delta"))


def test_parquet_time_travel_still_rejected(monkeypatch):
    prov = _provider([], fmt="auto")
    with pytest.raises(LakehouseError, match="not supported"):
        prov.open_dataset(TableInfo(name="plain", format="parquet"), version=1)



# ------------------------------------------------------------ version check


def test_delta_version_from_log_keys():
    prov = _provider(
        [
            "lake/orders/_delta_log/00000000000000000000.json",
            "lake/orders/_delta_log/00000000000000000004.json",
            "lake/orders/_delta_log/00000000000000000004.checkpoint.parquet",
        ],
        fmt="auto",
    )
    info = TableInfo(name="orders", format="delta", location="orders")
    assert prov.check_version(info) == 4


def test_check_version_non_delta_is_none():
    prov = _provider([], fmt="auto")
    assert prov.check_version(TableInfo(name="plain", format="parquet")) is None
