"""Unit tests for the S3/MinIO provider (table discovery + URI logic).

The tricky parts - deriving a TableInfo from a Parquet object key and
normalizing the MinIO endpoint URL - are pure string logic, so they get real
unit tests here. Listing is tested against a fake filesystem; no network.
"""

import pyarrow.fs as pafs
import pytest

from sqlhandler.config import S3Config
from sqlhandler.provider import LakehouseError, TableInfo
from sqlhandler.s3 import S3Provider


def _provider(**over):
    base = {"access_key": "minioadmin", "secret_key": "minioadmin", "bucket": "lakehouse"}
    base.update(over)
    return S3Provider(S3Config(**base))


# -------------------------------------------------------------- endpoint


def test_endpoint_override_keeps_scheme():
    p = _provider(endpoint_url="http://127.0.0.1:9000")
    assert p._endpoint_override() == "http://127.0.0.1:9000"


def test_endpoint_override_adds_scheme_when_missing():
    p = _provider(endpoint_url="127.0.0.1:9000")
    assert p._endpoint_override() == "http://127.0.0.1:9000"


def test_endpoint_override_https_when_use_ssl():
    p = _provider(endpoint_url="minio.example.com", use_ssl=True)
    assert p._endpoint_override() == "https://minio.example.com"


def test_endpoint_override_none_when_empty():
    assert _provider()._endpoint_override() is None


# ----------------------------------------------------------------- derive


def test_derive_flat_single_file():
    info = _provider()._derive("orders.parquet")
    assert info == TableInfo(name="orders", schema="default", format="parquet", location="orders.parquet")
    assert info.path == "orders"


def test_derive_folder_table():
    info = _provider()._derive("customers/part-0.parquet")
    assert info == TableInfo(name="customers", schema="default", format="parquet", location="customers")


def test_derive_schema_folder_table():
    info = _provider()._derive("sales/customers/part-0.parquet")
    assert info == TableInfo(name="customers", schema="sales", format="parquet", location="sales/customers")
    assert info.path == "sales/customers"


def test_derive_skips_hive_partition():
    info = _provider()._derive("hr/employees/year=2024/part.parquet")
    assert info == TableInfo(name="employees", schema="hr", format="parquet", location="hr/employees")


def test_derive_ignores_non_parquet_and_hidden():
    assert _provider()._derive("orders.csv") is None
    assert _provider()._derive(".hidden/part.parquet") is None


def test_derive_parquet_uppercase_suffix():
    assert _provider()._derive("orders.PARQUET") is not None


# -------------------------------------------------------------- table_uri


def test_table_uri_flat_file():
    p = _provider(prefix="datasets")
    assert (
        p.table_uri(TableInfo(name="orders", schema="default", location="orders.parquet"))
        == "s3://lakehouse/datasets/orders.parquet"
    )


def test_table_uri_schema_folder():
    p = _provider(prefix="datasets")
    assert (
        p.table_uri(TableInfo(name="customers", schema="sales", location="sales/customers"))
        == "s3://lakehouse/datasets/sales/customers"
    )


# --------------------------------------------------------------- listing


class _FakeFS:
    """pyarrow S3 filesystem stand-in returning canned FileInfos."""

    def __init__(self, entries):
        self.entries = entries
        self.selector = None

    def get_file_info(self, selector):
        self.selector = selector
        return self.entries


def test_list_tables_discovers_all_layouts(monkeypatch):
    p = _provider(prefix="datasets")
    fake = _FakeFS(
        [
            pafs.FileInfo("lakehouse/datasets/orders.parquet", pafs.FileType.File),
            pafs.FileInfo("lakehouse/datasets/sales/customers/a.parquet", pafs.FileType.File),
            pafs.FileInfo("lakehouse/datasets/sales/customers/b.parquet", pafs.FileType.File),
            pafs.FileInfo("lakehouse/datasets/hr/employees/year=2024/part.parquet", pafs.FileType.File),
            pafs.FileInfo("lakehouse/datasets/readme.txt", pafs.FileType.File),
            pafs.FileInfo("lakehouse/datasets", pafs.FileType.Directory),
        ]
    )
    monkeypatch.setattr(p, "_s3fs", lambda: fake)
    tables = p.list_tables()
    assert [t.path for t in tables] == ["hr/employees", "orders", "sales/customers"]
    assert fake.selector.recursive


def test_list_tables_error_raises(monkeypatch):
    p = _provider()

    class _Boom:
        def get_file_info(self, selector):
            raise RuntimeError("boom")

    monkeypatch.setattr(p, "_s3fs", lambda: _Boom())
    with pytest.raises(LakehouseError):
        p.list_tables()


def test_unconfigured_raises():
    with pytest.raises(LakehouseError):
        S3Provider(S3Config(bucket="", access_key="a", secret_key="s"))
