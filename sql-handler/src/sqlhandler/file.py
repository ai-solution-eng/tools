"""Local / NFS mounted directory of Parquet and Delta Lake tables.

The "nfs" backend reads tables from a directory mounted into the container
(NFS via a PV/PVC, hostPath, or any volume). It supports both:
  * Apache Delta Lake tables - folders containing a _delta_log/
  * plain Parquet files/folders
Discovery mirrors the S3 backend for Parquet and adds Delta-log detection;
reading is backed by pyarrow's LocalFileSystem and deltalake, so no
credentials or endpoint are needed.
"""

from __future__ import annotations

import logging
import os

import pyarrow.dataset as pad
import pyarrow.fs as pafs

from .config import FileConfig
from .provider import DataProvider, LakehouseError, TableInfo, _validate_snapshot_version

logger = logging.getLogger("sqlhandler.file")

_PARQUET_SUFFIX = ".parquet"
_DELTA_LOG = "_delta_log"


class FileProvider(DataProvider):
    """NFS / local-filesystem backend: Delta Lake tables and Parquet files."""

    kind = "nfs"

    def __init__(self, config: FileConfig):
        """Wrap a mounted directory of Delta/Parquet tables."""
        if not config.is_configured:
            raise LakehouseError(
                "NFS/file backend is not configured. Set NFS_ROOT to the mounted directory containing the tables."
            )
        self.config = config
        self._lfs: pafs.LocalFileSystem | None = None

    def _fs(self) -> pafs.LocalFileSystem:
        if self._lfs is None:
            self._lfs = pafs.LocalFileSystem()
        return self._lfs

    def _root(self) -> str:
        return self.config.root_dir.rstrip("/")

    def _contained_path(self, info: TableInfo) -> str:
        """Absolute path for a table, verified to stay under the NFS root.

        The engine resolves user-supplied table names into
        ``<schema>/<name>`` locations; without this check a name like
        ``../secret.parquet`` would read files outside the mounted root.
        Symlinks are resolved on both sides (realpath) so a link planted
        inside the root cannot point out either.
        """
        location = info.location or info.path
        root = os.path.realpath(self._root())
        target = os.path.realpath(os.path.join(root, location))
        if target != root and not target.startswith(root + os.sep):
            raise LakehouseError(f"Table location {location!r} is outside the NFS root {self._root()!r}")
        return target

    def table_uri(self, info: TableInfo) -> str:
        """Absolute path of the table folder (or single Parquet file)."""
        return self._contained_path(info)

    @staticmethod
    def _is_parquet(name: str) -> bool:
        return name.lower().endswith(_PARQUET_SUFFIX)

    @staticmethod
    def _hidden(path_parts) -> bool:
        return any(p.startswith(".") for p in path_parts)

    def _derive(self, rel: str) -> TableInfo | None:
        """Map a relative Parquet path to a TableInfo (mirrors S3)."""
        parts = [p for p in rel.split("/") if p]
        if not parts or not self._is_parquet(parts[-1]):
            return None
        if self._hidden(parts[:-1]):
            return None
        dirs = list(parts[:-1])
        while dirs and "=" in dirs[-1]:
            dirs.pop()
        if not dirs:
            name = parts[-1][: -len(_PARQUET_SUFFIX)]
            return TableInfo(name=name, schema="default", format="parquet", location=parts[-1])
        name = dirs[-1]
        schema = dirs[-2] if len(dirs) >= 2 else "default"
        location = "/".join(dirs)
        return TableInfo(name=name, schema=schema, format="parquet", location=location)

    def list_tables(self) -> list[TableInfo]:
        """Enumerate Delta Lake tables and Parquet tables under the root."""
        fs = self._fs()
        root = self._root()
        try:
            selector = pafs.FileSelector(root, recursive=True)
            infos = fs.get_file_info(selector)
        except Exception as exc:
            raise LakehouseError(f"Could not list NFS path '{root}': {exc}") from exc

        dirs = [fi for fi in infos if fi.type == pafs.FileType.Directory]
        files = [fi for fi in infos if fi.type == pafs.FileType.File]

        # Delta tables: directories directly containing a _delta_log dir.
        delta_roots: set[str] = set()
        for d in dirs:
            if d.path.rstrip("/").endswith("/" + _DELTA_LOG):
                delta_roots.add(d.path.rstrip("/")[: -len("/" + _DELTA_LOG)])

        seen: dict[str, TableInfo] = {}
        for dr in sorted(delta_roots, key=len):
            rel = dr[len(root) :].strip("/")
            if not rel or self._hidden(rel.split("/")):
                continue
            parts = rel.split("/")
            name = parts[-1]
            schema = parts[-2] if len(parts) >= 2 else "default"
            location = "/".join(parts)
            ti = TableInfo(name=name, schema=schema, format="delta", location=location)
            seen.setdefault(ti.path, ti)

        for fi in files:
            rel = fi.path[len(root) :].strip("/")
            if not rel or not self._is_parquet(rel):
                continue
            if any(fi.path.startswith(dr + "/") for dr in delta_roots):
                continue  # data files inside a Delta table, not their own table
            info = self._derive(rel)
            if info is not None:
                seen.setdefault(info.path, info)
        return sorted(seen.values(), key=lambda ti: ti.path)

    def open_dataset(self, info: TableInfo, version: int | None = None):
        """Open a Delta table or Parquet folder/file as a pyarrow Dataset.

        ``version`` (Delta only) pins a historical snapshot for time travel.
        """
        root = self._contained_path(info)
        try:
            if info.format == "delta":
                from deltalake import DeltaTable

                if version is None:
                    return DeltaTable(root).to_pyarrow_dataset()
                _validate_snapshot_version(version, "Delta")
                return DeltaTable(root, version=int(version)).to_pyarrow_dataset()
            if version is not None:
                raise LakehouseError(
                    f"Time travel is not supported for plain Parquet table '{info.path}' "
                    "(only Delta and Iceberg tables have version history)."
                )
            return pad.dataset(root, filesystem=self._fs(), format="parquet")
        except LakehouseError:
            raise
        except Exception as exc:
            raise LakehouseError(f"Could not open NFS table '{info.path}': {exc}") from exc

    def check_version(self, info: TableInfo) -> int | None:
        """Latest Delta snapshot version from the local _delta_log listing.

        Cheap (a single directory listing) so the engine can refresh a cached
        Delta dataset right after an ETL commit. Non-Delta tables: None.
        """
        if info.format != "delta":
            return None
        try:
            delta_log = os.path.join(self._contained_path(info), _DELTA_LOG)
            names = os.listdir(delta_log)
        except (OSError, LakehouseError):
            return None
        versions = [0]
        for name in names:
            stem = name.split(".", 1)[0]
            if stem.isdigit():
                versions.append(int(stem))
        return max(versions)

    def check_connection(self) -> str | None:
        """Verify the mounted directory is present and readable."""
        try:
            root = self._root()
            fi = self._fs().get_file_info(root)
            if fi.type != pafs.FileType.Directory:
                return f"NFS root '{root}' is not a directory"
            return None
        except Exception as exc:
            return str(exc)
