"""Apache Iceberg backend (read-only) via the pyiceberg catalog.

Iceberg tables live in an object store as Parquet/ORC data files plus a
metadata layer (snapshots, manifests with per-file stats). pyiceberg talks
to a catalog (REST by default, or a local SQL catalog) to list/describe
tables and resolve the current snapshot to its data files; this provider
exposes those files as a pyarrow Dataset, so the shared engine (DuckDB,
caches, scans) works unchanged:

  * list_tables / describe_table  -> catalog namespaces + table schema
  * open_dataset                  -> manifest-listed Parquet files as a
                                     pyarrow Dataset

Requires the optional dependency ``pyiceberg``:
    pip install 'sqlhandler[iceberg]'
"""

from __future__ import annotations

import logging
import os

import pyarrow as pa
import pyarrow.dataset as pad

from .config import IcebergConfig
from .provider import DataProvider, LakehouseError, TableInfo, _validate_snapshot_version
from .s3 import build_s3fs

logger = logging.getLogger("sqlhandler.iceberg")

_MISSING_MSG = (
    "Iceberg support requires the optional 'pyiceberg' package. Install with:  pip install 'sqlhandler[iceberg]'"
)

_SCHEMES = ("s3://", "file://", "gs://", "abfs://", "abfss://")


class IcebergProvider(DataProvider):
    """Apache Iceberg catalog backend (REST or local SQL), read-only."""

    kind = "iceberg"

    def __init__(self, config: IcebergConfig):
        """Wrap a pyiceberg catalog + its storage credentials."""
        if not config.is_configured:
            raise LakehouseError(
                "Iceberg connection is not configured. Set ICEBERG_CATALOG_URI "
                "(and ICEBERG_CATALOG_TYPE=rest|sql)."
            )
        self.config = config
        self._cat = None

    # ------------------------------------------------------------ catalog
    def _catalog(self):
        """Lazily build the pyiceberg catalog from the config."""
        if self._cat is None:
            try:
                from pyiceberg.catalog import load_catalog
                from pyiceberg.catalog.sql import SqlCatalog
            except ImportError as exc:
                raise LakehouseError(_MISSING_MSG) from exc
            try:
                if self.config.catalog_type == "sql":
                    self._cat = SqlCatalog(
                        self.config.catalog_name,
                        uri=self.config.catalog_uri,
                        warehouse=self.config.warehouse or None,
                    )
                else:
                    self._cat = load_catalog(
                        "default",
                        type="rest",
                        uri=self.config.catalog_uri,
                        token=self.config.catalog_token or None,
                        warehouse=self.config.warehouse or None,
                    )
            except Exception as exc:
                raise LakehouseError(f"Iceberg catalog init failed: {exc}") from exc
        return self._cat

    def _namespaces(self) -> list[str]:
        """Namespaces (schemas) to list, after the optional namespace filter."""
        catalog = self._catalog()
        try:
            namespaces = [ns[0] for ns in catalog.list_namespaces()]
        except Exception as exc:
            raise LakehouseError(f"Iceberg namespace listing failed: {exc}") from exc
        if self.config.namespace:
            namespaces = [ns for ns in namespaces if ns == self.config.namespace]
        return sorted(namespaces)

    # -------------------------------------------------------------- listing
    def list_tables(self) -> list[TableInfo]:
        """Enumerate Iceberg tables across the catalog's namespaces."""
        catalog = self._catalog()
        infos: list[TableInfo] = []
        for ns in self._namespaces():
            try:
                tables = catalog.list_tables(ns)
            except Exception as exc:
                logger.warning("Iceberg list_tables(%s) failed: %s", ns, exc)
                continue
            for _, name in tables:
                infos.append(TableInfo(name=name, schema=ns, format="parquet"))
        return sorted(infos, key=lambda ti: ti.path)

    def table_uri(self, info: TableInfo) -> str:
        """The table's storage location (its warehouse path)."""
        return str(self._load_table(info).location())

    def check_connection(self) -> str | None:
        """Cheap readiness check: resolve the catalog namespaces."""
        try:
            self._namespaces()
            return None
        except Exception as exc:
            return str(exc)

    # ------------------------------------------------------------- dataset
    def _load_table(self, info: TableInfo):
        """Load a pyiceberg table for a logical schema/name."""
        try:
            return self._catalog().load_table((info.schema, info.name))
        except Exception as exc:
            raise LakehouseError(f"Iceberg load_table {info.path} failed: {exc}") from exc

    def _normalize_path(self, path: str) -> str:
        """Make a data-file path usable by pyarrow on the right filesystem."""
        if path.startswith("file://"):
            return path[len("file://") :]
        if any(path.startswith(s) for s in _SCHEMES):
            return path
        if os.path.isabs(path):
            return path
        # Relative to the warehouse (no scheme in the path).
        wh = (self.config.warehouse or "").rstrip("/")
        return f"{wh}/{path}" if wh else path

    def open_dataset(self, info: TableInfo, version: int | None = None):
        """Open an Iceberg table as a pyarrow Dataset (cached by the engine).

        The dataset is built from the Parquet files listed in the table's
        snapshot manifest, so only the metadata (not the rows) is fetched
        here; predicates/projections still push down into the Parquet scan
        at query time. ``version`` selects a historical snapshot id (time
        travel); ``None`` scans the current snapshot.
        """
        table = self._load_table(info)
        scan = table.scan() if version is None else table.scan(snapshot_id=_validate_snapshot_version(version, "Iceberg"))
        try:
            files = [f.file.file_path for f in scan.plan_files()]
        except Exception as exc:
            raise LakehouseError(f"Iceberg scan planning {info.path} failed: {exc}") from exc
        files = [self._normalize_path(p) for p in files]
        if not files:
            # Empty table: return an empty dataset with the table's schema.
            schema = table.schema().as_arrow()
            empty = pa.table([pa.array([], type=f.type) for f in schema], schema=schema)
            return pad.dataset(empty)
        if any(p.startswith("s3://") for p in files):
            fs = build_s3fs(self.config.storage)
            paths = [p[len("s3://") :] for p in files]
            try:
                return pad.dataset(paths, filesystem=fs, format="parquet")
            except Exception as exc:
                raise LakehouseError(f"Could not open Iceberg dataset {info.path}: {exc}") from exc
        try:
            return pad.dataset(files, format="parquet")
        except Exception as exc:
            raise LakehouseError(f"Could not open Iceberg dataset {info.path}: {exc}") from exc
