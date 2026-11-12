"""AIOLI packaged-models DB client.

Reads the DB password from a Kubernetes Secret, connects to the AIOLI
PostgreSQL database, and inserts model configurations into the
packaged_models table.

VERSIONS ARE ASSIGNED HERE, NOT AUTHORED.  The catalog stores version 1 for
every configuration; a push lands as v1 when the name is new on this MLIS
instance and as max(version)+1 when the name already exists with a different
configuration.  An identical configuration is skipped rather than versioned
up, so re-pushing is idempotent.  See versioning.py for the rules.

All blocking DB calls are pushed to a thread so the FastAPI event loop is
not blocked.
"""

import asyncio
import logging

import psycopg2
from psycopg2.extras import Json

from .k8s import K8sClient
from .versioning import FINGERPRINT_COLUMNS, plan_push, resolve_project

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
BOOL_COLUMNS = {"caching_enabled"}

# The row shape plan_push() expects: the assigned version plus every column
# that defines the deployment (versioning.FINGERPRINT_COLUMNS).
EXISTING_COLUMNS = ("version", *FINGERPRINT_COLUMNS)


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
        default_project: str = "",
    ):
        self.k8s = k8s
        self.host = host
        self.port = port
        self.dbname = dbname
        self.user = user
        self.secret_name = secret_name
        self.secret_ns = secret_ns
        self.secret_key = secret_key
        self.default_project = default_project

    async def _password(self) -> str:
        data = await self.k8s.read_secret(self.secret_ns, self.secret_name)
        return data[self.secret_key]

    async def push_batch(self, configs: list[dict], project: str = "") -> list[dict]:
        """Insert configs into packaged_models. Returns per-config results.

        "project" is the project picked for this push (the UI's optional
        namespace field); it files a config that names no project of its own.
        """
        password = await self._password()
        return await asyncio.to_thread(self._push_sync, configs, password, project)

    async def fetch_packaged_uris(self) -> list[tuple[str, str]]:
        """Read-only SELECT of packaged_models (name, uri) for the GC cross-check.

        db.py connects as the postgres superuser — this connection is used
        ONLY for this constant SELECT (no parameters, no writes, no DDL):
        the GC's AIOLI access is strictly read-only by construction. Rows
        with a NULL/empty uri still come back (name, '') so callers can log
        them; the gc matching treats '' as non-matching.
        """
        password = await self._password()
        return await asyncio.to_thread(self._fetch_packaged_uris_sync, password)

    def _fetch_packaged_uris_sync(self, password: str) -> list[tuple[str, str]]:
        conn = psycopg2.connect(
            host=self.host,
            port=self.port,
            dbname=self.dbname,
            user=self.user,
            password=password,
            connect_timeout=10,
            options="-c default_transaction_read_only=on",
        )
        try:
            with conn, conn.cursor() as cur:
                cur.execute("SELECT name, uri FROM packaged_models ORDER BY name")
                return [(str(name), uri or "") for name, uri in cur.fetchall()]
        finally:
            conn.close()

    def _push_sync(self, configs: list[dict], password: str, project: str = "") -> list[dict]:
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
                    results.append(self._push_one(cur, self._with_default_project(cfg, project)))
        finally:
            conn.close()
        return results

    def _with_default_project(self, cfg: dict, project: str = "") -> dict:
        """Fill in the project a config names no project of its own.

        Resolution: explicit entry > the project picked for this push (the UI's
        namespace field) > catalog.defaultProject.  The winner is canonicalised
        by resolve_project(): a PCAI project-<name> namespace is accepted and
        its prefix stripped, so the namespace and the MLIS project name are the
        same configuration.  Applied BEFORE the fingerprint, not at INSERT
        time: the stored row and every later push of the same configuration
        must agree, or each re-push would look like a new configuration and
        mint a version every time.  With nothing set the config is returned
        untouched (the historical behaviour: MLIS rows carry project "" and
        only show under "All projects").
        """
        resolved = resolve_project(cfg, project, self.default_project)
        if resolved == str(cfg.get("project") or "").strip():
            return cfg
        return {**cfg, "project": resolved}

    @staticmethod
    def _existing_rows(cur, name: str) -> list[dict]:
        """Every packaged_models row already holding this name.

        One SELECT per config, not one per column: plan_push() needs the
        whole set to tell "identical configuration" (skip) from "same name,
        different configuration" (version up), and the same transaction sees
        earlier inserts of the same batch.
        """
        cur.execute(
            "SELECT " + ", ".join(EXISTING_COLUMNS) + " FROM packaged_models WHERE name = %s ORDER BY version",
            (name,),
        )
        return [dict(zip(EXISTING_COLUMNS, row)) for row in cur.fetchall()]

    @staticmethod
    def _push_one(cur, cfg: dict) -> dict:
        name = str(cfg.get("name", "")).strip()
        result = {"name": name, "version": 0, "status": "", "detail": "", "bumped": False}

        if not name or not cfg.get("image"):
            result["status"] = "error"
            result["detail"] = "missing required fields: name and image are required"
            return result

        plan = plan_push(AioliDB._existing_rows(cur, name), cfg)
        result.update(
            version=plan["version"],
            status=plan["status"],
            detail=plan["detail"],
            bumped=plan["bumped"],
        )
        if plan["status"] == "skipped":
            return result

        values = []
        for col in COLUMNS:
            if col == "version":
                # MLIS owns the sequence; the config's own version is ignored.
                values.append(plan["version"])
                continue
            val = cfg.get(col)
            if col in JSONB_COLUMNS:
                values.append(Json(val if val is not None else {}))
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
        detail = f"id={row[0]}" if row else "inserted"
        if plan["detail"]:
            detail = f"{detail} - {plan['detail']}"
        result["detail"] = detail
        return result
