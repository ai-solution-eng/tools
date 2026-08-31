"""S3-compatible (MinIO / AWS / any S3) backend for Parquet data.

This is the S3-flavoured DataProvider: it discovers "tables" as Parquet
files under a bucket+prefix and exposes each as a pyarrow Dataset. It is
built entirely on pyarrow (no boto3/s3fs dependency): pyarrow's bundled AWS
SDK handles authentication, listing and object reads, and DuckDB pushes
predicates/projections down through the pyarrow dataset the same way it
does for OneLake/Delta.

Table layout conventions (how "tables" are discovered under the prefix):

  * prefix/orders.parquet            -> table orders          (single file)
  * prefix/customers/*.parquet       -> table customers       (folder)
  * prefix/sales/customers/*.parquet -> schema sales, table customers
  * prefix/sales/customers/dt=2024/* -> schema sales, table customers
    (partition folders inside a table folder are folded into the table)

Hidden folders (starting with '.') are skipped. Works with MinIO out of the
box: set the endpoint URL (e.g. http://127.0.0.1:9000), access/secret keys
and bucket; path-style access is on by default (what MinIO uses).
"""

from __future__ import annotations

import logging

import pyarrow.dataset as pad
import pyarrow.fs as pafs

from .config import S3Config
from .provider import DataProvider, LakehouseError, TableInfo

logger = logging.getLogger("sqlhandler.s3")

_PARQUET_SUFFIX = ".parquet"


def _endpoint_override(endpoint_url: str, use_ssl: bool) -> str | None:
    """Normalize an S3 endpoint URL so pyarrow can dial a local MinIO."""
    url = (endpoint_url or "").strip()
    if not url:
        return None
    if "://" not in url:
        scheme = "https" if use_ssl else "http"
        url = f"{scheme}://{url}"
    return url


def build_s3fs(config: S3Config) -> pafs.S3FileSystem:
    """Create a pyarrow S3 filesystem from an S3Config (shared by the S3 and
    Iceberg backends for reading data files from object storage).
    """
    kwargs: dict = {
        "access_key": config.access_key or None,
        "secret_key": config.secret_key or None,
        "session_token": config.session_token or None,
        "region": config.region,
        "endpoint_override": _endpoint_override(config.endpoint_url, config.use_ssl),
        "anonymous": config.anonymous,
    }
    try:
        return pafs.S3FileSystem(**{k: v for k, v in kwargs.items() if v is not None})
    except Exception as exc:
        raise LakehouseError(f"Could not create S3 filesystem: {exc}") from exc


class S3Provider(DataProvider):
    """S3-compatible (MinIO / AWS S3 / GCS-interop) Parquet backend."""

    kind = "s3"

    def __init__(self, config: S3Config):
        """Wrap credentials + listing/read access for one S3 bucket/prefix."""
        if not config.is_configured:
            raise LakehouseError(
                "S3/MinIO connection is not configured. Set S3_BUCKET and "
                "S3_ACCESS_KEY / S3_SECRET_KEY (or S3_ANONYMOUS=1 for a public "
                "bucket), plus S3_ENDPOINT_URL when targeting MinIO."
            )
        self.config = config
        self._fs: pafs.S3FileSystem | None = None

    # ------------------------------------------------------------- client
    def _endpoint_override(self) -> str | None:
        """Normalize the configured endpoint URL (kept for tests/back-compat)."""
        return _endpoint_override(self.config.endpoint_url, self.config.use_ssl)

    def _s3fs(self) -> pafs.S3FileSystem:
        """Lazily build (and keep) the pyarrow S3 filesystem handle."""
        if self._fs is None:
            self._fs = build_s3fs(self.config)
        return self._fs

    # -------------------------------------------------------- addressing
    def _base(self) -> str:
        """Bucket/prefix root (no leading or trailing slash), e.g. 'mybucket'."""
        parts = [self.config.bucket.strip("/")]
        if self.config.prefix:
            parts.append(self.config.prefix.strip("/"))
        return "/".join(p for p in parts if p)

    def table_uri(self, info: TableInfo) -> str:
        """s3:// URI of the table folder (or single Parquet file)."""
        location = info.location or info.path
        return f"s3://{self._base()}/{location}"

    @staticmethod
    def _is_parquet(name: str) -> bool:
        return name.lower().endswith(_PARQUET_SUFFIX)

    def _derive(self, rel: str) -> TableInfo | None:
        """Map a source-relative Parquet path to a TableInfo.

        Walks up from the file past Hive partition folders (``key=value``) to
        find the table folder; the folder directly above it (if any) is the
        schema. A single file at the prefix root becomes a table named by its
        file stem under the "default" schema.
        """
        parts = [p for p in rel.split("/") if p]
        if not parts or not self._is_parquet(parts[-1]):
            return None
        if any(p.startswith(".") for p in parts[:-1]):
            return None
        dirs = list(parts[:-1])
        # Skip Hive partition folders (e.g. dt=2024) - they are not tables.
        while dirs and "=" in dirs[-1]:
            dirs.pop()
        if not dirs:
            # Single file at the prefix root: orders.parquet -> table orders
            name = parts[-1][: -len(_PARQUET_SUFFIX)]
            return TableInfo(name=name, schema="default", format="parquet", location=parts[-1])
        name = dirs[-1]
        schema = dirs[-2] if len(dirs) >= 2 else "default"
        location = "/".join(dirs)
        return TableInfo(name=name, schema=schema, format="parquet", location=location)

    # ----------------------------------------------------------- listing
    def list_tables(self) -> list[TableInfo]:
        """Enumerate the Parquet tables under bucket/prefix (recursive)."""
        fs = self._s3fs()
        root = self._base()
        try:
            selector = pafs.FileSelector(root, recursive=True)
            infos = fs.get_file_info(selector)
        except Exception as exc:
            raise LakehouseError(f"Could not list S3 path '{root}': {exc}") from exc

        seen: dict[str, TableInfo] = {}
        for fi in infos:
            if fi.type != pafs.FileType.File:
                continue
            rel = fi.path[len(root) :].strip("/")
            if not rel or not self._is_parquet(rel):
                continue
            info = self._derive(rel)
            if info is not None:
                seen[info.path] = info
        return sorted(seen.values(), key=lambda ti: ti.path)

    # ----------------------------------------------------------- dataset
    def check_connection(self) -> str | None:
        """Cheap readiness check: list the bucket/prefix root."""
        try:
            root = self._base()
            self._s3fs().get_file_info(pafs.FileSelector(root, recursive=False))
            return None
        except Exception as exc:
            return str(exc)

    def open_dataset(self, info: TableInfo):
        """Open a table folder/file as a pyarrow Dataset (cached by engine)."""
        location = info.location or info.path
        # S3 keys are flat so ".." cannot escape the bucket, but a traversal
        # segment is always a caller bug — fail fast with a clear error
        # instead of a confusing NoSuchKey from a literal "../" prefix.
        if any(part == ".." for part in location.split("/")):
            raise LakehouseError(f"Invalid S3 table location: {location!r}")
        fs = self._s3fs()
        root = f"{self._base()}/{location}"
        try:
            return pad.dataset(root, filesystem=fs, format="parquet")
        except Exception as exc:
            raise LakehouseError(f"Could not open S3 dataset '{info.path}': {exc}") from exc
