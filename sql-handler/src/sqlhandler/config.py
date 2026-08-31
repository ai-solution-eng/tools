"""Configuration for the SQLhandler Microsoft Fabric OneLake access.

All sensitive values are read from the environment, never hardcoded. Populate a
``.env`` file (see ``config/.env.example``) or export the variables in the
deployment environment. The temporary service-principal credential provided by
Toromont must NOT be committed to source control.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field


def _getenv(name: str, default: str = "") -> str:
    """Read an environment variable with a stripped fallback."""
    return os.environ.get(name, default).strip()


def _to_int(name: str, env: Mapping[str, str], default: int) -> int:
    """Read an env var as an int, falling back to ``default`` on garbage."""
    raw = _getenv(name, env.get(name, ""))
    try:
        return int(raw)
    except ValueError:
        return default


def _to_bool(name: str, env: Mapping[str, str], default: bool = False) -> bool:
    """Read an env var as a bool (true/1/yes/on), falling back on garbage."""
    raw = _getenv(name, env.get(name, "")).lower()
    if not raw:
        return default
    if raw in ("1", "true", "yes", "on", "y"):
        return True
    if raw in ("0", "false", "no", "off", "n"):
        return False
    return default


@dataclass(frozen=True)
class FabricConfig:
    """Connection details for a Microsoft Fabric / OneLake workspace.

    Two ways to address a lakehouse are supported:
      * ``lakehouse_abfss_url`` - a full ABFS/OneLake URL to the lakehouse
        "Tables" directory (preferred; works with both DuckDB and pyarrow).
      * explicit ``workspace_id`` + ``lakehouse_id`` - assembled into the URL.
    """

    tenant_id: str = ""
    client_id: str = ""
    client_secret: str = ""

    # Full ABFS URL to the lakehouse, e.g.
    #   abfss://<workspace-id>@onelake.dfs.fabric.microsoft.com/<lakehouse-id>
    lakehouse_abfss_url: str = ""

    # Fallback: assemble the URL from workspace + lakehouse GUIDs.
    workspace_id: str = ""
    lakehouse_id: str = ""

    # Optional: override the fabric DFS authority (defaults to the public one).
    fabric_authority: str = "onelake.dfs.fabric.microsoft.com"

    @property
    def is_configured(self) -> bool:
        """Whether we have enough to build an ABFS URL and authenticate."""
        has_identity = all((self.tenant_id, self.client_id, self.client_secret))
        has_url = bool(self.lakehouse_abfss_url)
        has_ids = bool(self.workspace_id and self.lakehouse_id)
        return has_identity and (has_url or has_ids)

    @property
    def abfss_tables_url(self) -> str:
        """The ABFS URL pointing at the lakehouse ``Tables`` directory."""
        if self.lakehouse_abfss_url:
            base = self.lakehouse_abfss_url.rstrip("/")
            # If the user passed a URL already ending in /Tables, keep it.
            return base if base.endswith("/Tables") else f"{base}/Tables"
        return f"abfss://{self.workspace_id}@{self.fabric_authority}/{self.lakehouse_id}/Tables"


def load_config(env: dict | None = None) -> FabricConfig:
    """Build a :class:`FabricConfig` from the environment (or a provided dict).

    Environment variables (also loadable from ``config/.env``):
      FABRIC_TENANT_ID
      FABRIC_CLIENT_ID
      FABRIC_CLIENT_SECRET
      FABRIC_LAKEHOUSE_ABFSS_URL
      FABRIC_WORKSPACE_ID
      FABRIC_LAKEHOUSE_ID
      FABRIC_AUTHORITY
    """
    e = env if env is not None else os.environ
    return FabricConfig(
        tenant_id=_getenv("FABRIC_TENANT_ID", e.get("FABRIC_TENANT_ID", "")),
        client_id=_getenv("FABRIC_CLIENT_ID", e.get("FABRIC_CLIENT_ID", "")),
        client_secret=_getenv("FABRIC_CLIENT_SECRET", e.get("FABRIC_CLIENT_SECRET", "")),
        lakehouse_abfss_url=_getenv(
            "FABRIC_LAKEHOUSE_ABFSS_URL",
            e.get("FABRIC_LAKEHOUSE_ABFSS_URL", ""),
        ),
        workspace_id=_getenv("FABRIC_WORKSPACE_ID", e.get("FABRIC_WORKSPACE_ID", "")),
        lakehouse_id=_getenv("FABRIC_LAKEHOUSE_ID", e.get("FABRIC_LAKEHOUSE_ID", "")),
        fabric_authority=_getenv("FABRIC_AUTHORITY", e.get("FABRIC_AUTHORITY", "onelake.dfs.fabric.microsoft.com")),
    )


@dataclass(frozen=True)
class S3Config:
    """Connection details for an S3-compatible object store (MinIO, AWS).

    ``endpoint_url`` is the S3 API endpoint; for MinIO that is e.g.
    ``http://127.0.0.1:9000`` (scheme optional - https is inferred from
    ``use_ssl``). ``bucket`` is required and ``prefix`` scopes the search to a
    sub-tree (e.g. ``datasets``). Path-style addressing is on by default,
    which is what MinIO and most S3-compatible stores use.
    """

    endpoint_url: str = ""
    region: str = "us-east-1"
    access_key: str = ""
    secret_key: str = ""
    session_token: str = ""
    bucket: str = ""
    prefix: str = ""
    anonymous: bool = False
    use_ssl: bool = False
    path_style_access: bool = True

    @property
    def is_configured(self) -> bool:
        """Whether we have enough to point at the bucket and authenticate."""
        if not self.bucket:
            return False
        if self.anonymous:
            return True
        return bool(self.access_key and self.secret_key)


def load_s3_config(env: dict | None = None) -> S3Config:
    """Build a :class:`S3Config` from the environment (or a provided dict).

    Environment variables (also loadable from ``config/.env``):
      S3_ENDPOINT_URL
      S3_REGION
      S3_ACCESS_KEY
      S3_SECRET_KEY
      S3_SESSION_TOKEN
      S3_BUCKET
      S3_PREFIX
      S3_ANONYMOUS   (true/1/on enables anonymous read of a public bucket)
      S3_USE_SSL     (force https when endpoint_url has no scheme)
      S3_PATH_STYLE  (default true; MinIO uses path-style addressing)
    """
    e = env if env is not None else os.environ
    return S3Config(
        endpoint_url=_getenv("S3_ENDPOINT_URL", e.get("S3_ENDPOINT_URL", "")),
        region=_getenv("S3_REGION", e.get("S3_REGION", "us-east-1")),
        access_key=_getenv("S3_ACCESS_KEY", e.get("S3_ACCESS_KEY", "")),
        secret_key=_getenv("S3_SECRET_KEY", e.get("S3_SECRET_KEY", "")),
        session_token=_getenv("S3_SESSION_TOKEN", e.get("S3_SESSION_TOKEN", "")),
        bucket=_getenv("S3_BUCKET", e.get("S3_BUCKET", "")),
        prefix=_getenv("S3_PREFIX", e.get("S3_PREFIX", "")),
        anonymous=_to_bool("S3_ANONYMOUS", e, False),
        use_ssl=_to_bool("S3_USE_SSL", e, False),
        path_style_access=_to_bool("S3_PATH_STYLE", e, True),
    )


@dataclass(frozen=True)
class IcebergConfig:
    """Connection details for an Apache Iceberg catalog.

    ``catalog_type`` selects how tables are listed/described:
      * ``rest`` (default) - Iceberg REST catalog (the modern standard; what
        Amazon S3 Tables / Dremio / Nessie expose)
      * ``sql``            - a SQL catalog (SQLite/Postgres), handy for local
        development and tests

    ``catalog_uri`` is the REST endpoint URL (rest) or e.g. ``sqlite:///path``
    (sql). ``storage`` carries the credentials/endpoint of the object store
    the Parquet data files live on (reuses the ``S3_*`` env vars); it is
    ignored when the warehouse is on the local filesystem.
    """

    catalog_type: str = "rest"
    catalog_uri: str = ""
    catalog_token: str = ""
    catalog_name: str = "sqlhandler"  # SQL catalogs partition metadata by this name
    warehouse: str = ""
    namespace: str = ""  # optional filter for list_tables (default: all)
    storage: S3Config = field(default_factory=S3Config)

    @property
    def is_configured(self) -> bool:
        """Whether we have a catalog type + URI to talk to."""
        return self.catalog_type in ("rest", "sql") and bool(self.catalog_uri)


def load_iceberg_config(env: dict | None = None) -> IcebergConfig:
    """Build an :class:`IcebergConfig` from the environment (or a provided dict).

    Environment variables:
      ICEBERG_CATALOG_TYPE   (rest | sql; default rest)
      ICEBERG_CATALOG_URI    (REST endpoint, or sqlite:///path for sql)
      ICEBERG_CATALOG_TOKEN  (optional bearer token for REST)
      ICEBERG_CATALOG_NAME   (catalog name; SQL catalogs partition by it)
      ICEBERG_WAREHOUSE      (optional table location root)
      ICEBERG_NAMESPACE      (optional namespace filter)
      plus the S3_* variables for the storage the data files live on
    """
    e = env if env is not None else os.environ
    ctype = _getenv("ICEBERG_CATALOG_TYPE", e.get("ICEBERG_CATALOG_TYPE", "rest")).strip().lower()
    if ctype not in ("rest", "sql"):
        ctype = "rest"
    return IcebergConfig(
        catalog_type=ctype,
        catalog_uri=_getenv("ICEBERG_CATALOG_URI", e.get("ICEBERG_CATALOG_URI", "")),
        catalog_token=_getenv("ICEBERG_CATALOG_TOKEN", e.get("ICEBERG_CATALOG_TOKEN", "")),
        catalog_name=_getenv("ICEBERG_CATALOG_NAME", e.get("ICEBERG_CATALOG_NAME", "sqlhandler")),
        warehouse=_getenv("ICEBERG_WAREHOUSE", e.get("ICEBERG_WAREHOUSE", "")),
        namespace=_getenv("ICEBERG_NAMESPACE", e.get("ICEBERG_NAMESPACE", "")),
        storage=load_s3_config(env),
    )


@dataclass(frozen=True)
class FileConfig:
    """Readable local / NFS mounted directory of Parquet files.

    Backed by pyarrow's LocalFileSystem, so it works for any directory
    mounted into the container (hostPath, NFS via PV/PVC, or other).
    Table discovery uses the same folder conventions as the S3 backend.
    """

    root_dir: str = ""

    @property
    def is_configured(self) -> bool:
        return bool(self.root_dir)


def load_file_config(env: dict | None = None) -> FileConfig:
    """Build a :class:`FileConfig` from the environment.

    Environment variables:
      NFS_ROOT - absolute path to the mounted root directory (required)
    """
    e = env if env is not None else os.environ
    return FileConfig(root_dir=_getenv("NFS_ROOT", e.get("NFS_ROOT", "")))


def load_backend_config(env: dict | None = None) -> tuple[str, object]:
    """Select and load the backend config from the environment.

    ``SQLHANDLER_BACKEND`` chooses the data source:
      * ``onelake`` (default) - Microsoft Fabric OneLake, Delta Lake (ABFS)
      * ``s3`` / ``minio``     - S3-compatible object store, Parquet files
      * ``nfs`` / ``file``     - local / mounted (NFS) directory, Parquet files
      * ``iceberg``            - Apache Iceberg REST/SQL catalog

    Returns ``(backend_name, config)``; feed the config to
    :func:`sqlhandler.provider.make_provider` to build the provider.
    """
    e = env if env is not None else os.environ
    backend = _getenv("SQLHANDLER_BACKEND", e.get("SQLHANDLER_BACKEND", "onelake")).lower()
    if backend in ("nfs", "file", "local"):
        return "nfs", load_file_config(env)
    if backend in ("s3", "minio", "parquet"):
        return "s3", load_s3_config(env)
    if backend == "iceberg":
        return "iceberg", load_iceberg_config(env)
    return "onelake", load_config(env)


def load_source_providers(env: dict | None = None) -> object | None:
    """Build a federated :class:`MultiProvider` from ``SQLHANDLER_SOURCES``.

    ``SQLHANDLER_SOURCES`` is an optional JSON array of source objects. When
    set, the engine federates every source behind one endpoint (cross-source
    joins included); when unset, single-source mode behaves exactly as before.

    Each entry mirrors the backend env vars, e.g.::

      [
        {"name": "sales",      "backend": "s3",     "bucket": "bucket-a",
         "endpointUrl": "http://minio:9000", "accessKey": "...", "secretKey": "..."},
        {"name": "inventory",  "backend": "s3",     "bucket": "bucket-b",
         "prefix": "raw", "endpointUrl": "http://minio:9000",
         "accessKey": "...", "secretKey": "..."}
      ]

    Supported backends: ``s3``/``minio`` (S3_BUCKET/PREFIX/ENDPOINT_URL/
    REGION/ACCESS_KEY/SECRET_KEY/ANONYMOUS/USE_SSL), ``onelake``
    (abfssUrl or workspaceId+lakehouseId), ``nfs`` (rootDir) and ``iceberg``
    (catalogType/catalogUri/warehouse).

    Source labels (``name``) must be unique and valid identifiers
    (letters/digits/underscores, not starting with a digit) — they become
    the DuckDB name prefix for that source's tables. Violations raise
    ``ValueError`` at startup instead of silently shadowing a source.
    """
    e = env if env is not None else os.environ
    raw = _getenv("SQLHANDLER_SOURCES", e.get("SQLHANDLER_SOURCES", "")).strip()
    if not raw:
        return None
    try:
        sources = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"SQLHANDLER_SOURCES is not valid JSON: {exc}") from exc
    if not isinstance(sources, list) or not sources:
        raise ValueError("SQLHANDLER_SOURCES must be a non-empty JSON array")

    from .provider import MultiProvider, make_provider

    labels: list[str] = []
    providers: list = []
    for idx, src in enumerate(sources):
        if not isinstance(src, dict):
            raise TypeError(f"SQLHANDLER_SOURCES[{idx}] must be an object")
        label = str(src.get("name") or f"source{idx + 1}")
        # Source labels become DuckDB identifier prefixes (source_schema_name)
        # and routing keys. Duplicates would silently make the earlier source
        # unreachable (dict(zip(...)) keeps the last), and non-identifier
        # characters produce ambiguous SQL names — both are user errors worth
        # failing on at startup rather than debugging at query time.
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", label):
            raise ValueError(
                f"SQLHANDLER_SOURCES[{idx}] name {label!r} must be a valid identifier "
                "(letters, digits, underscores; not starting with a digit)"
            )
        if label in labels:
            raise ValueError(f"SQLHANDLER_SOURCES[{idx}] name {label!r} is duplicated; source names must be unique")
        backend = str(src.get("backend") or "s3").lower()
        src_env = {
            "SQLHANDLER_BACKEND": backend,
            "S3_ENDPOINT_URL": src.get("endpointUrl", ""),
            "S3_REGION": src.get("region", "us-east-1"),
            "S3_ACCESS_KEY": src.get("accessKey", ""),
            "S3_SECRET_KEY": src.get("secretKey", ""),
            "S3_SESSION_TOKEN": src.get("sessionToken", ""),
            "S3_BUCKET": src.get("bucket", ""),
            "S3_PREFIX": src.get("prefix", ""),
            "S3_ANONYMOUS": "true" if src.get("anonymous") else "false",
            "S3_USE_SSL": "true" if src.get("useSsl") else "false",
            "FABRIC_LAKEHOUSE_ABFSS_URL": src.get("abfssUrl", ""),
            "FABRIC_WORKSPACE_ID": src.get("workspaceId", ""),
            "FABRIC_LAKEHOUSE_ID": src.get("lakehouseId", ""),
            "NFS_ROOT": src.get("rootDir", ""),
            "ICEBERG_CATALOG_TYPE": src.get("catalogType", "rest"),
            "ICEBERG_CATALOG_URI": src.get("catalogUri", ""),
            "ICEBERG_CATALOG_NAME": src.get("catalogName", "sqlhandler"),
            "ICEBERG_WAREHOUSE": src.get("warehouse", ""),
        }
        _, cfg = load_backend_config(src_env)
        providers.append(make_provider(cfg))
        labels.append(label)
    return MultiProvider(providers, labels)


@dataclass(frozen=True)
class CacheConfig:
    """In-process caching knobs for the SQL MCP server.

    ``list_tables`` and ``describe_table`` hit the lakehouse (DFS metadata and
    the Delta ``_delta_log``) on every call, and agents/OWUI re-run them on
    every session. Both are effectively static between ETL runs, so caching
    them in-process for a few minutes makes repeat calls near-instant without
    any external cache service.
    """

    ttl_seconds: int = 3600
    prewarm_tables: tuple[str, ...] = ()
    # Reuse open pyarrow Delta datasets per table (avoids re-reading the
    # Delta _delta_log on every query; the underlying rows still come from
    # OneLake on each scan). TTL in seconds, LRU cap in tables.
    dataset_cache_ttl: int = 3600
    dataset_cache_tables: int = 8
    # How often (seconds) the Delta snapshot version is re-checked to
    # invalidate a cached Dataset. 0 = check on every reuse.
    version_check_interval: int = 10
    # Serve list_tables from the cache immediately and refresh it in the
    # background (plus an automatic refresh every ttl_seconds), so callers
    # never block on the S3/DFS listing. Ignored when ttl_seconds == 0.
    list_async_refresh: bool = True

    @property
    def enabled(self) -> bool:
        return self.ttl_seconds > 0


def load_cache_config(env: dict | None = None) -> CacheConfig:
    """Build a :class:`CacheConfig` from the environment (or a provided dict).

    Environment variables (also loadable from ``config/.env``):
      SQLHANDLER_CACHE_TTL       - seconds to keep list_tables / describe_table
                                   results in memory (0 disables caching)
      SQLHANDLER_PREWARM_TABLES  - comma-separated Delta table names whose
                                   schemas are warmed into the describe cache
                                   at server startup (the tables your queries
                                   hit most often)
      SQLHANDLER_DATASET_CACHE_TTL - seconds to reuse open Delta datasets
                                   per table (0 disables; default 3600)
      SQLHANDLER_DATASET_CACHE_TABLES - max tables kept in the dataset LRU
                                        (default 8)
      SQLHANDLER_VERSION_CHECK_INTERVAL - how often to re-check a Delta
                                        snapshot version (seconds; 0 = every
                                        reuse; default 10)
      SQLHANDLER_LIST_ASYNC_REFRESH - serve list_tables from the cache
                                        immediately and refresh in the
                                        background every ttl_seconds
                                        (true/1/on; default true)
    """
    e = env if env is not None else os.environ
    raw_ttl = _getenv("SQLHANDLER_CACHE_TTL", e.get("SQLHANDLER_CACHE_TTL", "3600"))
    try:
        ttl = int(raw_ttl)
    except ValueError:
        ttl = 3600
    raw_prewarm = _getenv("SQLHANDLER_PREWARM_TABLES", e.get("SQLHANDLER_PREWARM_TABLES", ""))
    prewarm = tuple(t.strip() for t in raw_prewarm.split(",") if t.strip())
    dttl = _to_int("SQLHANDLER_DATASET_CACHE_TTL", e, 3600)
    dcap = _to_int("SQLHANDLER_DATASET_CACHE_TABLES", e, 8)
    vci = _to_int("SQLHANDLER_VERSION_CHECK_INTERVAL", e, 10)
    async_list = _to_bool("SQLHANDLER_LIST_ASYNC_REFRESH", e, True)
    return CacheConfig(
        ttl_seconds=max(ttl, 0),
        prewarm_tables=prewarm,
        dataset_cache_ttl=max(dttl, 0),
        dataset_cache_tables=max(dcap, 0),
        version_check_interval=max(vci, 0),
        list_async_refresh=async_list,
    )


def load_dotenv(path: str | None = None) -> None:
    """Minimal .env loader (no external dep). Reads ``KEY=VALUE`` lines.

    Set ``SQLHANDLER_ENV_FILE`` to point at your file, or pass ``path``. Values
    are only set into ``os.environ`` if not already present.
    """
    if path is None:
        path = os.environ.get("SQLHANDLER_ENV_FILE", "")
    if not path:
        # Conventional location: <repo>/config/.env next to the checked-out
        # src/ tree (two levels up from this module). Do NOT probe further up
        # the tree — picking up an unrelated sibling project's .env is worse
        # than finding nothing.
        here = os.path.dirname(os.path.abspath(__file__))
        for candidate in (
            os.path.join(here, "..", "..", "config", ".env"),
        ):
            if os.path.exists(candidate):
                path = candidate
                break
    if not path or not os.path.exists(path):
        return

    with open(path, "r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
