"""Backend-agnostic data provider interface for the SQL engine.

The SQL machinery that makes this fast is storage-agnostic: pyarrow exposes
each source as a pyarrow.dataset.Dataset, DuckDB scans that dataset
(pushing predicates/column projection down to the Parquet/Delta scan), and the
engine caches the cheap metadata (table list + schemas + open dataset handles)
in-process.

What differs per backend is a thin strip: where the data lives, how you
authenticate, how you enumerate "tables", and how you turn a table into a
pyarrow Dataset. That strip is exactly DataProvider. To add a new source
(OneLake, S3/MinIO parquet, local directory, HDFS, GCS...) you write a new
provider subclass and register it in make_provider — the MCP layer, the SQL
engine, and the caches stay untouched.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

__all__ = ["DataProvider", "LakehouseError", "TableInfo", "make_provider"]


class LakehouseError(RuntimeError):
    """Raised for connectivity / authentication / schema / listing errors."""


@dataclass(frozen=True)
class TableInfo:
    """A single table discovered in a data source.

    ``schema`` / ``name`` give the logical address used by SQL (e.g.
    ``sales/customers`` -> schema ``sales``, name ``customers``; a flat table
    has schema ``"default"``). ``location`` is the source-relative physical
    location (a folder or a single file) that the provider actually opens.
    """

    name: str
    schema: str = "default"
    format: str = "parquet"  # "parquet" | "delta" | ...
    num_rows: int | None = None
    location: str = ""

    @property
    def path(self) -> str:
        """Logical ``<schema>/<name>`` key (falls back to bare name)."""
        if self.schema and self.schema != "default":
            return f"{self.schema}/{self.name}"
        return self.name

    @property
    def qualified_name(self) -> str:
        """SQL identifier: ``<schema>_<name>`` (collision-free)."""
        return self.path.replace("/", "_")


class DataProvider(ABC):
    """How a backend exposes its tables to the shared SQL engine.

    Implementations must be cheap-to-construct and thread-safe: the engine
    holds one instance per process and may call any method concurrently.
    """

    kind: str = "abstract"

    @abstractmethod
    def list_tables(self) -> list[TableInfo]:
        """Enumerate every table the source exposes, sorted by path."""

    @abstractmethod
    def table_uri(self, info: TableInfo) -> str:
        """Canonical URI for a table (ABFS/ADLS, s3://, ...), for display."""

    @abstractmethod
    def open_dataset(self, info: TableInfo) -> object:
        """Open the table as a pyarrow Dataset (no row data is loaded).

        The engine caches the returned handle, so this should be an
        "open metadata" operation (read the Delta _delta_log / Parquet footer
        once), not a full download.
        """

    def check_connection(self) -> str | None:
        """Return None if the backend is reachable, or an error message.

        Used by the /ready readiness gate; must be cheap (a single metadata
        / listing call) and must not raise. Default: no-op (always healthy).
        """
        return None

    def check_version(self, info: TableInfo) -> object | None:
        """Return a cheap version token for a table (e.g. the Delta snapshot
        version), or None when the source has no cheap change signal.

        The engine compares this token on cached-dataset reuse so Delta tables
        refresh after an ETL commit without waiting out the dataset cache TTL.
        Must be cheap and never raise (return None on any failure).
        """
        return None

    def close(self) -> None:
        """Release any process-level resources (default: no-op)."""


def make_provider(config: object) -> DataProvider:
    """Build the right provider for a loaded backend config object.

    Dispatch is by config type so new backends only add a branch here.
    """
    from .config import FabricConfig, FileConfig, IcebergConfig, S3Config

    if isinstance(config, S3Config):
        from .s3 import S3Provider

        return S3Provider(config)
    if isinstance(config, IcebergConfig):
        from .iceberg import IcebergProvider

        return IcebergProvider(config)
    if isinstance(config, FileConfig):
        from .file import FileProvider

        return FileProvider(config)
    if isinstance(config, FabricConfig):
        from .onelake import OneLakeProvider

        return OneLakeProvider(config)
    raise LakehouseError(
        f"Unsupported backend config type: {type(config).__name__!r}. Use load_backend_config() to build a known one."
    )
