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
from collections.abc import Sequence
from dataclasses import dataclass, replace

__all__ = [
    "DataProvider",
    "LakehouseError",
    "MultiProvider",
    "TableInfo",
    "make_provider",
]


def _validate_snapshot_version(version: object, backend: str) -> int:
    """Validate a time-travel version argument (int snapshot id/version).

    Returns the int value; raises LakehouseError for anything else (bools
    are ints in Python but are never a legitimate snapshot id).
    """
    if isinstance(version, bool) or not isinstance(version, int) or version < 0:
        raise LakehouseError(
            f"{backend} time travel needs a non-negative integer snapshot version, "
            f"got {version!r}."
        )
    return version


class LakehouseError(RuntimeError):
    """Raised for connectivity / authentication / schema / listing errors."""


@dataclass(frozen=True)
class TableInfo:
    """A single table discovered in a data source.

    ``schema`` / ``name`` give the logical address used by SQL (e.g.
    ``sales/customers`` -> schema ``sales``, name ``customers``; a flat table
    has schema ``"default"``). ``location`` is the source-relative physical
    location (a folder or a single file) that the provider actually opens.
    ``source`` names the federated source the table came from (see
    :class:`MultiProvider`); a single-source engine keeps ``"default"``.
    """

    name: str
    schema: str = "default"
    format: str = "parquet"  # "parquet" | "delta" | ...
    num_rows: int | None = None
    location: str = ""
    source: str = "default"

    @property
    def path(self) -> str:
        """Logical ``<schema>/<name>`` key (falls back to bare name)."""
        if self.schema and self.schema != "default":
            return f"{self.schema}/{self.name}"
        return self.name

    @property
    def qualified_name(self) -> str:
        """SQL identifier: ``[source_]<schema>_<name>`` (collision-free)."""
        qualified = self.path.replace("/", "_")
        if self.source and self.source != "default":
            return f"{self.source}_{qualified}"
        return qualified


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
    def open_dataset(self, info: TableInfo, version: int | None = None) -> object:
        """Open the table as a pyarrow Dataset (no row data is loaded).

        The engine caches the returned handle, so this should be an
        "open metadata" operation (read the Delta _delta_log / Parquet footer
        once), not a full download.

        ``version`` selects a HISTORICAL snapshot for time travel (``None``
        = current): a Delta snapshot version (nfs/onelake) or an Iceberg
        snapshot id. Backends without version history (plain Parquet) must
        raise :class:`LakehouseError` when a version is requested.
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


class MultiProvider(DataProvider):
    """Federate one or more DataProviders behind a single engine.

    Each wrapped provider gets a distinct ``source`` label. Tables it exposes
    are tagged with that source, and ``table_uri`` / ``open_dataset`` /
    ``check_version`` are routed back to the owning provider. Because
    :attr:`TableInfo.qualified_name` is prefixed with the source, same-named
    tables in different sources stay distinct SQL identifiers (e.g. source
    ``warehouse`` + table ``orders`` -> ``warehouse_orders``), enabling
    cross-source queries through one engine.

    Thread-safety: the wrapped providers must already be thread-safe; this
    class only tags tables and dispatches, so it inherits their guarantees.
    """

    kind = "multi"

    def __init__(
        self,
        providers: Sequence[DataProvider],
        sources: Sequence[str],
    ):
        if not providers:
            raise ValueError("MultiProvider needs at least one provider")
        if len(providers) != len(sources):
            raise ValueError("providers and sources must be the same length")
        self.providers = list(providers)
        self.sources = [str(s) for s in sources]
        self._by_source = dict(zip(self.sources, self.providers))

    @property
    def source_count(self) -> int:
        """Number of federated sources (1+)."""
        return len(self.providers)

    def _owner(self, info: TableInfo) -> DataProvider:
        return self._by_source.get(info.source, self.providers[0])

    def list_tables(self) -> list[TableInfo]:
        out: list[TableInfo] = []
        for source, prov in zip(self.sources, self.providers):
            for ti in prov.list_tables():
                out.append(replace(ti, source=source))
        return sorted(out, key=lambda t: (t.source, t.path))

    def table_uri(self, info: TableInfo) -> str:
        return self._owner(info).table_uri(info)

    def open_dataset(self, info: TableInfo, version: int | None = None) -> object:
        return self._owner(info).open_dataset(info, version)

    def check_version(self, info: TableInfo) -> object | None:
        return self._owner(info).check_version(info)

    def check_connection(self) -> str | None:
        errors: list[str] = []
        for source, prov in zip(self.sources, self.providers):
            err = prov.check_connection()
            if err:
                errors.append(f"{source}: {err}")
        return "; ".join(errors) or None

    def close(self) -> None:
        for prov in self.providers:
            try:
                prov.close()
            except Exception:  # best effort on shutdown
                pass


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
