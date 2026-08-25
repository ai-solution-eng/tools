"""Unit tests for the Iceberg provider (catalog + file planning logic).

Uses a fake pyiceberg catalog so nothing touches the network; the local
warehouse read path (open_dataset over real Parquet files in a temp dir) is
exercised for real.
"""

import pyarrow as pa
import pyarrow.dataset as pad
import pyarrow.parquet as pq
import pytest

from sqlhandler.config import IcebergConfig, S3Config
from sqlhandler.iceberg import IcebergProvider
from sqlhandler.provider import LakehouseError, TableInfo


class _File:
    def __init__(self, file_path):
        self.file_path = file_path


class _Task:
    """Mimics a pyiceberg FileScanTask: .file.file_path."""

    def __init__(self, path):
        self.file = _File(path)


class _Scan:
    def __init__(self, paths):
        self._paths = paths

    def plan_files(self):
        return [_Task(p) for p in self._paths]


class _Table:
    def __init__(self, location="", paths=None, schema=None):
        self._location = location
        self._paths = paths or []
        self._schema = schema or pa.schema([pa.field("id", pa.int64())])

    def location(self):
        return self._location

    def scan(self):
        return _Scan(self._paths)

    def schema(self):
        outer = self

        class _S:
            def as_arrow(self):
                return outer._schema

        return _S()


class _FakeCatalog:
    def __init__(self, tables):
        self._tables = tables  # {(db, name): _Table}
        self.loaded = []

    def list_namespaces(self):
        return sorted({(db,) for (db, _) in self._tables})

    def list_tables(self, ns):
        return [(db, name) for (db, name) in self._tables if db == ns]

    def load_table(self, ident):
        self.loaded.append(tuple(ident))
        return self._tables[tuple(ident)]


def _provider(catalog, storage_kwargs=None, **over):
    cfg = IcebergConfig(
        catalog_type="sql",
        catalog_uri="sqlite:///tmp/x.db",
        storage=S3Config(**(storage_kwargs or {})),
        **over,
    )
    p = IcebergProvider(cfg)
    p._cat = catalog
    return p


def test_unconfigured_raises():
    with pytest.raises(LakehouseError):
        IcebergProvider(IcebergConfig())


def test_list_tables():
    cat = _FakeCatalog({("db", "sales"): _Table(), ("db", "items"): _Table()})
    infos = _provider(cat).list_tables()
    assert [(i.schema, i.name) for i in infos] == [("db", "items"), ("db", "sales")]
    assert all(i.format == "parquet" for i in infos)


def test_list_tables_namespace_filter():
    cat = _FakeCatalog({("db", "sales"): _Table(), ("analytics", "events"): _Table()})
    infos = _provider(cat, namespace="analytics").list_tables()
    assert [i.path for i in infos] == ["analytics/events"]


def test_table_uri_is_location():
    cat = _FakeCatalog({("db", "sales"): _Table(location="s3://wh/db/sales")})
    p = _provider(cat)
    assert p.table_uri(TableInfo(name="sales", schema="db")) == "s3://wh/db/sales"


def test_normalize_path_variants():
    p = _provider(None, warehouse="s3://wh")
    assert p._normalize_path("s3://wh/db/t1/1.parquet") == "s3://wh/db/t1/1.parquet"
    assert p._normalize_path("file:///tmp/a.parquet") == "/tmp/a.parquet"
    assert p._normalize_path("/abs/p.parquet") == "/abs/p.parquet"
    assert p._normalize_path("db/t1/1.parquet") == "s3://wh/db/t1/1.parquet"


def test_open_dataset_local_paths(tmp_path):
    data_dir = tmp_path / "t"
    data_dir.mkdir(parents=True)
    pq.write_table(pa.table({"id": [1, 2], "name": ["a", "b"]}), data_dir / "part.parquet")
    table = _Table(
        location=str(data_dir),
        paths=[str(data_dir / "part.parquet")],
        schema=pa.schema([pa.field("id", pa.int64()), pa.field("name", pa.string())]),
    )
    p = _provider(_FakeCatalog({("db", "t"): table}))
    ds = p.open_dataset(TableInfo(name="t", schema="db"))
    assert ds.to_table().num_rows == 2


def test_open_dataset_empty_table_returns_empty_dataset():
    table = _Table(paths=[], schema=pa.schema([pa.field("id", pa.int64())]))
    p = _provider(_FakeCatalog({("db", "empty"): table}))
    ds = p.open_dataset(TableInfo(name="empty", schema="db"))
    assert ds.to_table().num_rows == 0
    assert ds.to_table().schema.field("id").type == pa.int64()


def test_open_dataset_s3_branch(monkeypatch):
    table = _Table(location="s3://wh/db/t", paths=["s3://wh/db/t/data/1.parquet"])
    p = _provider(
        _FakeCatalog({("db", "t"): table}),
        storage_kwargs={
            "endpoint_url": "http://127.0.0.1:9000",
            "access_key": "k",
            "secret_key": "s",
        },
    )
    real_ds = pad.dataset(pa.table({"id": [1]}))
    captured = {}

    def fake_dataset(paths, filesystem=None, format=None):
        captured["paths"] = paths
        captured["fs"] = filesystem
        return real_ds

    monkeypatch.setattr("sqlhandler.iceberg.pad.dataset", fake_dataset)
    ds = p.open_dataset(TableInfo(name="t", schema="db"))
    assert captured["paths"] == ["wh/db/t/data/1.parquet"]  # s3:// stripped
    assert captured["fs"] is not None
    assert ds.to_table().num_rows == 1
