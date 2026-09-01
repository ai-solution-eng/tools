"""Direct access to a Microsoft Fabric OneLake lakehouse (Delta Lake).

This is the OneLake-flavoured DataProvider: it knows how to find Delta tables
over OneLake's ABFS endpoint and expose each as a pyarrow Dataset. All the
generic machinery (DuckDB SQL, caches, markdown rendering) lives in the shared
engine / server layer, so this module only contains OneLake-specific code.

OneLake specifics:
  * The ABFS account is the workspace GUID; the *container* is always
    onelake and the authority is onelake.dfs.fabric.microsoft.com.
    deltalake's Azure storage options handle the client-credentials
    exchange (service principal).
  * OneLake does not implement the ADLS Gen2 *blob* list API that
    pyarrow's AzureFileSystem uses, so table discovery goes over the
    **DFS REST API** (?resource=filesystem&recursive=false) instead.
  * Tables live one level under Tables in schema folders
    (Tables/workorder/work_order_header); list_tables returns a flat list
    keyed by <schema>/<table>.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from .config import FabricConfig
from .provider import DataProvider, LakehouseError, TableInfo, _validate_snapshot_version

logger = logging.getLogger("sqlhandler.onelake")

_DELTA_LOG = "_delta_log"

# Schema directories are listed concurrently in list_tables (one DFS call
# each); cap the fan-out so a lake with hundreds of schemas doesn't open
# hundreds of simultaneous connections.
_SCHEMA_LIST_WORKERS = 8

# Refresh the cached DFS bearer token this many seconds before it expires
# (Entra client-credentials tokens for storage.azure.com live 1h by default).
_TOKEN_REFRESH_MARGIN = 300

# Token acquisition retries for transient failures (network blips, AAD 429/5xx).
_TOKEN_ATTEMPTS = 3
_TOKEN_RETRY_BASE_SLEEP = 0.5

# Upper bound on DFS list continuation pages per directory (safety guard).
_DFS_LIST_MAX_PAGES = 5


class OneLakeProvider(DataProvider):
    """Fabric OneLake (ABFS, Delta Lake) backend."""

    kind = "onelake"

    def __init__(self, config: FabricConfig):
        """Wrap auth + dataset access for a single OneLake lake."""
        if not config.is_configured:
            raise LakehouseError(
                "Fabric/OneLake connection is not configured. Set "
                "FABRIC_TENANT_ID, FABRIC_CLIENT_ID, FABRIC_CLIENT_SECRET and "
                "either FABRIC_LAKEHOUSE_ABFSS_URL or "
                "FABRIC_WORKSPACE_ID + FABRIC_LAKEHOUSE_ID."
            )
        self.config = config
        self._token: str | None = None
        self._token_expires_at: float = 0.0
        self._token_lock = threading.Lock()

    # ------------------------------------------------------------- auth
    def _dfs_access_token(self) -> str:
        """Return a non-expired storage-scoped bearer token for the DFS REST API.

        OneLake/Entra access tokens are short-lived (default 1h), so the cached
        token is refreshed before it lapses instead of being cached forever
        (a stale cached token is what makes every DFS call start failing with
        HTTP 401 after the first hour of pod uptime).
        """
        now = time.time()
        if self._token and self._token_expires_at > now + _TOKEN_REFRESH_MARGIN:
            return self._token
        # Serialize refreshes so a token that is about to lapse does not trigger
        # a stampede of AAD token requests from concurrent tool calls.
        with self._token_lock:
            now = time.time()
            if self._token and self._token_expires_at > now + _TOKEN_REFRESH_MARGIN:
                return self._token
            return self._acquire_token()

    def _acquire_token(self) -> str:
        """POST a fresh client-credentials token and cache it.

        Retries transient failures (network blips, AAD 429/5xx) with a short
        backoff. As a last resort, reuses a cached token that is still within
        its lifetime so a refresh hiccup does not take the lakehouse offline.
        """
        body = urllib.parse.urlencode(
            {
                "client_id": self.config.client_id,
                "client_secret": self.config.client_secret,
                "scope": "https://storage.azure.com/.default",
                "grant_type": "client_credentials",
            }
        ).encode()
        req = urllib.request.Request(
            f"https://login.microsoftonline.com/{self.config.tenant_id}/oauth2/v2.0/token",
            data=body,
            method="POST",
        )
        last_err: Exception | None = None
        for attempt in range(_TOKEN_ATTEMPTS):
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    payload = json.loads(resp.read())
                token = payload.get("access_token")
                if not token:
                    raise LakehouseError(f"No access_token in token response: {payload}")
                self._token = token
                expires_in = int(payload.get("expires_in", 3600) or 3600)
                self._token_expires_at = time.time() + expires_in
                return token
            except urllib.error.HTTPError as exc:
                # Deterministic auth errors (4xx, except 429) are not retried;
                # throttling (429) and server errors (>=500) are transient.
                if exc.code < 500 and exc.code != 429:
                    raise LakehouseError(f"OneLake token acquisition failed: HTTP {exc.code}") from exc
                last_err = exc
            except Exception as exc:
                last_err = exc
            if attempt < _TOKEN_ATTEMPTS - 1:
                time.sleep(_TOKEN_RETRY_BASE_SLEEP * (attempt + 1))
        # A refresh failed but the previous token is still unexpired: keep
        # serving with it rather than failing every request.
        if self._token and self._token_expires_at > time.time():
            return self._token
        raise LakehouseError(f"OneLake token acquisition failed: {last_err}") from last_err

    # ------------------------------------------------------------ addressing
    @property
    def _workspace(self) -> str:
        """The ABFS account = workspace GUID."""
        rest = self.config.abfss_tables_url.split("://", 1)[1]
        container_at, _, _ = rest.partition("/")
        account, _, _ = container_at.partition("@")
        return account

    @property
    def _lh_root(self) -> str:
        """In-account path of the lakehouse root (no leading slash)."""
        rest = self.config.abfss_tables_url.split("://", 1)[1]
        _, _, path = rest.partition("/")
        path = path.lstrip("/")
        if path.endswith("/Tables"):
            return path[: -len("/Tables")]
        return path

    @property
    def base_path(self) -> str:
        """In-account path of the lake Tables directory."""
        return f"{self._lh_root}/Tables"

    def table_uri(self, info: TableInfo) -> str:
        """Full ABFS URL to a Delta table folder."""
        return f"abfss://{self._workspace}@{self.config.fabric_authority}/{self._lh_root}/Tables/{info.path}"

    def check_version(self, info: TableInfo) -> int | None:
        """Latest Delta version from the table's _delta_log (one DFS list).

        Cheap enough to run on every cached-dataset reuse, so a Delta table
        picks up a new ETL commit without waiting out the dataset cache TTL.
        Returns None when the listing fails (engine falls back to TTL).
        """
        if info.format != "delta":
            return None
        try:
            entries = self._dfs_list(f"{self._lh_root}/Tables/{info.path}/_delta_log")
        except Exception:
            return None
        versions = [0]
        for entry in entries:
            name = entry.get("name", "").rstrip("/").split("/")[-1]
            stem = name.split(".", 1)[0]
            if stem.isdigit():
                versions.append(int(stem))
        return max(versions)

    def check_connection(self) -> str | None:
        """Cheap readiness check: acquire token and list the Tables root."""
        try:
            self._dfs_list(self.base_path)
            return None
        except Exception as exc:
            return str(exc)

    # -------------------------------------------------------------- metadata
    def _dfs_list(self, path: str) -> list[dict]:
        """List a directory via the DFS REST API (OneLake-compatible).

        Follows OneLake's pagination tokens so a large directory is never
        truncated, and refreshes a rejected bearer token (HTTP 401) once
        before surfacing an error.
        """
        url = f"https://{self.config.fabric_authority}/{self._workspace}/{path}?recursive=false&resource=filesystem"
        paths: list[dict] = []
        continuation: str | None = None
        refreshed_auth = False
        for _ in range(_DFS_LIST_MAX_PAGES):
            token = self._dfs_access_token()
            full_url = url
            if continuation:
                full_url += f"&continuation={urllib.parse.quote(continuation)}"
            req = urllib.request.Request(full_url, headers={"Authorization": f"Bearer {token}"})
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    payload = json.loads(resp.read())
            except urllib.error.HTTPError as exc:
                if exc.code == 401 and not refreshed_auth:
                    refreshed_auth = True
                    self._token = None
                    self._token_expires_at = 0.0
                    continue
                raise LakehouseError(f"DFS list of {path} failed: HTTP {exc.code}") from exc
            except Exception as exc:
                raise LakehouseError(f"DFS list of {path} failed: {exc}") from exc
            paths.extend(payload.get("paths", []))
            continuation = payload.get("continuation")
            if not continuation:
                return paths
        raise LakehouseError(f"DFS list of {path} failed: too many continuation pages")

    def list_tables(self) -> list[TableInfo]:
        """List the Delta tables under the lake Tables dir.

        OneLake doesn't support blob-style listing, so we enumerate via the
        DFS REST API: Tables -> schema dirs -> table dirs. Schema listings run
        concurrently (one HTTPS round-trip per schema is the bulk of the
        latency on lakes with many schemas).
        """
        tables_root = self.base_path
        try:
            schema_entries = self._dfs_list(tables_root)
        except LakehouseError as exc:
            raise LakehouseError(f"Failed to list tables at {tables_root}: {exc}") from exc

        schemas = [
            entry.get("name", "").rstrip("/").split("/")[-1]
            for entry in schema_entries
            if entry.get("isDirectory") == "true"
            and entry.get("name", "").rstrip("/").split("/")[-1]
            and not entry.get("name", "").rstrip("/").split("/")[-1].startswith(".")
        ]

        def _list_schema(schema: str) -> list[TableInfo]:
            try:
                table_entries = self._dfs_list(f"{tables_root}/{schema}")
            except LakehouseError:
                return []
            return [
                TableInfo(name=name, schema=schema, format="delta")
                for te in table_entries
                if (name := te.get("name", "").rstrip("/").split("/")[-1])
                and te.get("isDirectory") == "true"
                and name != _DELTA_LOG
                and not name.startswith(".")
            ]

        infos: list[TableInfo] = []
        with ThreadPoolExecutor(max_workers=min(_SCHEMA_LIST_WORKERS, max(len(schemas), 1))) as pool:
            for batch in pool.map(_list_schema, schemas):
                infos.extend(batch)

        infos.sort(key=lambda ti: ti.path)
        return infos

    # ----------------------------------------------------------- data access
    def _open_delta(self, info: TableInfo, version: int | None = None):
        """Open a deltalake DeltaTable handle (optionally a past version)."""
        from deltalake import DeltaTable as DeltaTableCls

        uri = self.table_uri(info)
        try:
            if version is None:
                return DeltaTableCls(uri, storage_options=self._storage_options())
            return DeltaTableCls(uri, version=version, storage_options=self._storage_options())
        except Exception as exc:
            raise LakehouseError(f"Could not open Delta table {info.path!r}: {exc}") from exc

    def _storage_options(self) -> dict:
        """Storage options for deltalake (OneLake account + SP creds)."""
        return {
            "account_name": "onelake",
            "azure_tenant_id": self.config.tenant_id,
            "azure_client_id": self.config.client_id,
            "azure_client_secret": self.config.client_secret,
            "dfs_endpoint": self.config.fabric_authority,
            "blob_endpoint": self.config.fabric_authority.replace("dfs", "blob"),
        }

    def open_dataset(self, info: TableInfo, version: int | None = None):
        """Open the Delta table as a pyarrow Dataset (cached by the engine).

        ``version`` pins a historical Delta snapshot for time travel.
        """
        if version is not None:
            _validate_snapshot_version(version, "Delta")
            dt = self._open_delta(info, version=int(version))
        else:
            dt = self._open_delta(info)
        try:
            return dt.to_pyarrow_dataset()
        except Exception as exc:
            raise LakehouseError(f"Could not open Delta dataset {info.path!r}: {exc}") from exc
