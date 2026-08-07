"""AIOLI packaged-models DB client.

Reads the DB password from a Kubernetes Secret, connects to the AIOLI
PostgreSQL database, and inserts model configurations into the
packaged_models table.  Duplicate (name, version) entries are skipped.
All blocking DB calls are pushed to a thread so the FastAPI event loop
is not blocked.
"""

import asyncio
import logging

import psycopg2
from psycopg2.extras import Json

from .k8s import K8sClient

log = logging.getLogger(__name__)

COLUMNS = [
    "name",
    "version",
    "description",
    "uri",
    "image",
    "registry",
    "arguments",
    "environment",
    "resource_request_cpu",
    "resource_request_memory",
    "resource_request_gpu",
    "resource_limit_cpu",
    "resource_limit_memory",
    "resource_limit_gpu",
    "resource_gpu_type",
    "model_format",
    "caching_enabled",
    "project",
    "metadata",
]
JSONB_COLUMNS = {"environment", "metadata"}
INT_COLUMNS = {"version"}
BOOL_COLUMNS = {"caching_enabled"}


class AioliDB:
    def __init__(
        self,
        k8s: K8sClient,
        host: str,
        port: int,
        dbname: str,
        user: str,
        secret_name: str,
        secret_ns: str,
        secret_key: str,
    ):
        self.k8s = k8s
        self.host = host
        self.port = port
        self.dbname = dbname
        self.user = user
        self.secret_name = secret_name
        self.secret_ns = secret_ns
        self.secret_key = secret_key

    async def _password(self) -> str:
        data = await self.k8s.read_secret(self.secret_ns, self.secret_name)
        return data[self.secret_key]

    async def push_batch(self, configs: list[dict]) -> list[dict]:
        """Insert configs into packaged_models. Returns per-config results."""
        password = await self._password()
        return await asyncio.to_thread(self._push_sync, configs, password)

    def _push_sync(self, configs: list[dict], password: str) -> list[dict]:
        results = []
        conn = psycopg2.connect(
            host=self.host,
            port=self.port,
            dbname=self.dbname,
            user=self.user,
            password=password,
            connect_timeout=10,
        )
        try:
            with conn, conn.cursor() as cur:
                for cfg in configs:
                    results.append(self._push_one(cur, cfg))
        finally:
            conn.close()
        return results

    @staticmethod
    def _push_one(cur, cfg: dict) -> dict:
        name = str(cfg.get("name", "")).strip()
        version = int(cfg.get("version", 1) or 1)
        result = {"name": name, "version": version, "status": "", "detail": ""}

        if not name or not cfg.get("image"):
            result["status"] = "error"
            result["detail"] = "missing required fields: name and image are required"
            return result

        cur.execute(
            "SELECT id FROM packaged_models WHERE name = %s AND version = %s",
            (name, version),
        )
        if cur.fetchone():
            result["status"] = "skipped"
            result["detail"] = "already exists in MLIS"
            return result

        values = []
        for col in COLUMNS:
            val = cfg.get(col)
            if col in JSONB_COLUMNS:
                values.append(Json(val if val is not None else {}))
            elif col in INT_COLUMNS:
                values.append(int(val) if val is not None else 1)
            elif col in BOOL_COLUMNS:
                values.append(bool(val))
            elif col == "project":
                values.append(val if val is not None else "")
            elif val is None:
                values.append(None)
            else:
                values.append(val)

        placeholders = ", ".join(["%s"] * len(COLUMNS))
        cols = ", ".join(COLUMNS)
        cur.execute(
            f"INSERT INTO packaged_models ({cols}) VALUES ({placeholders}) RETURNING id",
            values,
        )
        row = cur.fetchone()
        result["status"] = "pushed"
        result["detail"] = f"id={row[0]}" if row else "inserted"
        return result
