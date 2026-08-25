"""Backward-compatible entry point for the SQLhandler data layer.

The original OneLakeHandler bundled OneLake-specific access and the shared
SQL engine in one class. That is now split into:

  * sqlhandler.provider - DataProvider interface + TableInfo
  * sqlhandler.onelake  - OneLake/ABFS/Delta specifics
  * sqlhandler.s3       - S3/MinIO/Parquet specifics
  * sqlhandler.engine   - shared engine (DuckDB + caches)

This module keeps the old import paths working (OneLakeHandler, TableInfo,
LakehouseError) for code written against the 0.2.x API.
"""

from __future__ import annotations

from .config import FabricConfig
from .engine import SqlEngine
from .onelake import OneLakeProvider
from .provider import LakehouseError, TableInfo

__all__ = ["LakehouseError", "OneLakeHandler", "TableInfo"]


class OneLakeHandler(SqlEngine):
    """Backward-compatible class: the OneLake engine (provider + engine).

    Equivalent to SqlEngine(OneLakeProvider(config), ...). New code should
    build a provider via make_provider and pass it to SqlEngine directly.
    """

    def __init__(
        self,
        config: FabricConfig,
        cache_ttl: int = 3600,
        dataset_cache_ttl: int = 3600,
        dataset_cache_tables: int = 8,
    ):
        super().__init__(
            OneLakeProvider(config),
            cache_ttl=cache_ttl,
            dataset_cache_ttl=dataset_cache_ttl,
            dataset_cache_tables=dataset_cache_tables,
        )
