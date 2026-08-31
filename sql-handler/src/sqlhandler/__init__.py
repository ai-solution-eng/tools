"""SQLhandler: fast, direct SQL access to columnar data.

The package provides an MCP server (``sqlhandler.server``) plus a reusable
Python layer for reading tabular data as pyarrow Datasets and running SQL
with DuckDB. Backends are pluggable DataProviders:

  * ``sqlhandler.onelake`` - Microsoft Fabric OneLake (Delta Lake over ABFS)
  * ``sqlhandler.s3``      - S3-compatible object storage (MinIO) Parquet

Build an engine with::

    from sqlhandler.config import load_backend_config
    from sqlhandler.provider import make_provider
    from sqlhandler.engine import SqlEngine

    _, config = load_backend_config()
    engine = SqlEngine(make_provider(config))

or keep the old single-class import (``OneLakeHandler``) from
``sqlhandler.lakehouse``.
"""

from .config import (
    CacheConfig,
    FabricConfig,
    FileConfig,
    IcebergConfig,
    S3Config,
    load_backend_config,
    load_cache_config,
    load_config,
    load_dotenv,
    load_file_config,
    load_iceberg_config,
    load_s3_config,
)
from .engine import SqlEngine
from .file import FileProvider
from .iceberg import IcebergProvider
from .lakehouse import OneLakeHandler
from .onelake import OneLakeProvider
from .provider import DataProvider, LakehouseError, TableInfo, make_provider
from .s3 import S3Provider

__all__ = [
    "CacheConfig",
    "DataProvider",
    "FabricConfig",
    "FileConfig",
    "FileProvider",
    "IcebergConfig",
    "IcebergProvider",
    "LakehouseError",
    "OneLakeHandler",
    "OneLakeProvider",
    "S3Config",
    "S3Provider",
    "SqlEngine",
    "TableInfo",
    "load_backend_config",
    "load_cache_config",
    "load_config",
    "load_dotenv",
    "load_file_config",
    "load_iceberg_config",
    "load_s3_config",
    "make_provider",
]

__version__ = "0.8.0"
