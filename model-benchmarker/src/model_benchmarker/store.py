"""The measurement store: atoms -> queryable rows (MB-DB).

Every benchmark measurement — decomposed from PAST artifacts (webapp runs,
committed results tree) or recorded from LIVE runs — lands as one row in a
single SQLite database: model, GPU/setup configuration, concurrency, task,
every distinguishing flag, and the metrics. The store is the modular layer:
comparisons, scaling curves, the results page and future analyses all query
it instead of re-parsing Markdown.

Storage: SQLite (stdlib, zero new dependencies) at <work_dir>/atoms.db.
Writes are idempotent via a content hash (source + row identity), so
re-scanning the same tree or re-recording a level never duplicates rows —
"avoid rerunning redundant info" applies to ingestion too. The schema is
additive-only in spirit: new columns are added with ALTER TABLE by
_ensure_schema, so an older database file keeps working after an upgrade.

Row grain: one measurement = one (kind, model, setup, test) observation.
Percentiles live as p50/p95/p99/p100 columns per metric family (a row is
already the aggregate of its requests; storing request-level data is a
different tool).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS measurements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    -- idempotency: content hash of (source, test identity, metrics)
    content_hash TEXT NOT NULL UNIQUE,
    -- provenance
    kind TEXT NOT NULL,               -- chat | endpoint
    run_id TEXT,                      -- webapp run id (NULL for committed tree)
    source TEXT NOT NULL,             -- artifact path the row was parsed from
    run_status TEXT,                  -- success|timeout|failed|cancelled|interrupted|complete|partial
    started_utc TEXT,
    target_url TEXT,
    -- WHAT was measured
    model TEXT,                       -- model id / directory slug (NULL = unknown)
    -- serving-setup configuration (from filename grammar / run params)
    gpu TEXT, gpu_count INTEGER, engine TEXT, mtp TEXT,
    weights TEXT, hicache INTEGER, replicas INTEGER,
    -- test identity
    ctx INTEGER,                      -- context length (0 = no padding)
    users INTEGER,                    -- concurrency level
    task TEXT,                        -- coding | creative | mixed | custom | N=<c> (endpoint)
    arrival TEXT,                     -- closed | open (endpoint rows: NULL)
    mode TEXT,                        -- multiturn | single | rest | mcp
    requests_per_user INTEGER,        -- closed-loop load (chat)
    request_rate REAL,                -- open-loop configured rate
    level_duration_s REAL,            -- open-loop window
    -- metrics (percentile columns; NULL = group absent)
    failed INTEGER,
    ttft_p50_ms REAL, ttft_p95_ms REAL, ttft_p99_ms REAL, ttft_p100_ms REAL,
    ttft_post_p50_ms REAL, ttft_post_p95_ms REAL, ttft_post_p99_ms REAL, ttft_post_p100_ms REAL,
    tps_p50 REAL, tps_p95 REAL, tps_p99 REAL, tps_p100 REAL,
    itl_p50_ms REAL, itl_p95_ms REAL, itl_p99_ms REAL, itl_p100_ms REAL,
    tpot_p50_ms REAL, tpot_p95_ms REAL, tpot_p99_ms REAL, tpot_p100_ms REAL,
    -- endpoint-only metric families (chat rows: NULL)
    rps REAL,
    lat_p50_ms REAL, lat_p95_ms REAL, lat_p99_ms REAL, lat_max_ms REAL,
    success_rate_pct REAL, gpu_util_mean REAL
);
CREATE INDEX IF NOT EXISTS idx_meas_model ON measurements(model);
CREATE INDEX IF NOT EXISTS idx_meas_test ON measurements(kind, ctx, users, task);
CREATE INDEX IF NOT EXISTS idx_meas_run ON measurements(run_id);
CREATE INDEX IF NOT EXISTS idx_meas_setup ON measurements(gpu, engine);
"""

# percentiles -> column names per metric family
_PCT_COLS = {
    "ttft_ms": "ttft_{}_ms",
    "ttft_post_ms": "ttft_post_{}_ms",
    "tokens_per_s": "tps_{}",
    "itl_ms": "itl_{}_ms",
    "tpot_ms": "tpot_{}_ms",
}


def _hash_row(source: str, atom: dict) -> str:
    """Content hash over everything that identifies one measurement: source
    artifact, run identity, test identity, and the metric values. Same row
    content from a re-scan -> same hash -> INSERT OR IGNORE keeps one copy;
    a genuinely re-measured level (new run) differs in run_id/started_utc."""
    identity = {
        "source": source,
        "run_id": atom.get("run_id"),
        "started_utc": atom.get("started_utc"),
        "test_id": atom.get("test_id"),
        "occurrence": atom.get("occurrence"),
        "row_index": atom.get("row_index"),
        "metrics": atom.get("metrics"),
    }
    return hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    if row is None:
        conn.execute("INSERT INTO meta (key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
    conn.commit()


@contextmanager
def connect(db_path: str | Path):
    """One connection with the schema ensured; commits on success, rolls back
    on error, always closes. WAL mode: the webapp can read while a backfill
    writes."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        _ensure_schema(conn)
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def record_atoms(
    db_path: str | Path, atoms: list[dict], *, source: str | None = None, setup: dict | None = None
) -> tuple[int, int]:
    """Insert atoms as measurement rows. Returns (inserted, skipped_duplicate).

    Idempotent: rows are keyed by content hash, so re-recording the same
    artifact's atoms is a no-op (the skip count tells you how much was
    already there — no redundant work on re-scan). ``setup`` stamps the
    serving-config columns (filename-grammar meta from results-tree scans);
    run scans omit it and the columns stay NULL (unknown is honest).
    """
    inserted = skipped = 0
    with connect(db_path) as conn:
        for a in atoms:
            src = source or str(a.get("source") or "")
            row = _row_from_atom(a, src, setup or {})
            h = _hash_row(src, a)
            cols = ", ".join(row)
            marks = ", ".join("?" for _ in row)
            cur = conn.execute(
                f"INSERT OR IGNORE INTO measurements (content_hash, {cols}) VALUES (?, {marks})",
                [h, *row.values()],
            )
            if cur.rowcount > 0:
                inserted += 1
            else:
                skipped += 1
    return inserted, skipped


def _row_from_atom(a: dict, src: str, setup: dict) -> dict[str, Any]:
    """The single atom -> SQL-row mapping shared by both record paths."""
    row: dict[str, Any] = {
        "kind": a.get("kind"),
        "run_id": a.get("run_id"),
        "source": src,
        "run_status": a.get("status"),
        "started_utc": a.get("started_utc"),
        "target_url": a.get("target_url"),
        "model": a.get("model"),
        "gpu": setup.get("gpu"),
        "gpu_count": setup.get("gpu_count"),
        "engine": setup.get("engine"),
        "mtp": setup.get("mtp"),
        "weights": setup.get("weights"),
        "hicache": setup.get("hicache"),
        "replicas": setup.get("replicas"),
        "ctx": None,
        "users": None,
        "task": None,
        "arrival": a.get("arrival"),
        "mode": a.get("mode"),
        "requests_per_user": (a.get("load") or {}).get("requests_per_user"),
        "request_rate": (a.get("load") or {}).get("request_rate"),
        "level_duration_s": (a.get("load") or {}).get("level_duration_s"),
    }
    test = a.get("test") or {}
    if a.get("kind") == "chat":
        row["ctx"] = test.get("ctx")
        row["users"] = test.get("users")
        row["task"] = test.get("task")
    else:
        label = str(test.get("label") or a.get("label") or "")
        conc = label.split("N=")[-1].split()[0] if "N=" in label else None
        try:
            row["users"] = int(conc) if conc is not None else None
        except ValueError:
            row["users"] = None
        row["task"] = label
    m = a.get("metrics") or {}
    row["failed"] = int(m["failed"]) if isinstance(m.get("failed"), (int, float)) else None
    for family, colfmt in _PCT_COLS.items():
        block = m.get(family) or {}
        for p in ("p50", "p95", "p99", "p100"):
            v = block.get(p)
            row[colfmt.format(p)] = float(v) if isinstance(v, (int, float)) else None
    for k in ("rps", "lat_p50_ms", "lat_p95_ms", "lat_p99_ms", "lat_max_ms", "success_rate_pct", "gpu_util_mean"):
        v = m.get(k)
        row[k] = float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None
    return row


def count(db_path: str | Path, where: str | None = None, params: list | tuple = ()) -> int:
    """Row count over an optional WHERE clause (the filtered total the query
    endpoints report alongside their page of rows)."""
    sql = "SELECT COUNT(*) FROM measurements"
    if where:
        sql += " WHERE " + where
    with connect(db_path) as conn:
        return int(conn.execute(sql, tuple(params)).fetchone()[0])


# Whitelisted filter columns for the shared filter builder (the /api/store
# query + facets routes): every entry maps a query param to a SQL column.
# Anything else in a request is ignored — the route never interpolates
# client strings into SQL.
FILTER_COLUMNS: dict[str, str] = {
    "model": "model",
    "kind": "kind",
    "run_id": "run_id",
    "task": "task",
    "arrival": "arrival",
    "mode": "mode",
    "gpu": "gpu",
    "engine": "engine",
    "mtp": "mtp",
    "weights": "weights",
    "ctx": "ctx",
    "users": "users",
    "hicache": "hicache",
    "replicas": "replicas",
}


def build_filters(query_params: dict[str, Any]) -> tuple[str, list[Any]]:
    """Client query params -> (where-SQL, bind params) using FILTER_COLUMNS.
    Values are bound, never interpolated; unknown keys are ignored; empty
    strings mean 'no filter'. Numeric columns are bound as ints when the
    value parses, so ctx=0 works (a bare string '0' would still match in
    SQLite's affinity coercion, but ints keep the intent explicit)."""
    where: list[str] = []
    params: list[Any] = []
    for key, col in FILTER_COLUMNS.items():
        v = str(query_params.get(key) or "").strip()
        if v == "":
            continue
        if col in ("ctx", "users", "hicache", "replicas", "gpu_count"):
            try:
                params.append(int(v))
            except ValueError:
                continue  # non-numeric filter value: ignore, not error
        else:
            params.append(v)
        where.append(f"{col} = ?")
    return (" AND ".join(where) if where else "", params)


def facets(db_path: str | Path) -> dict:
    """The faceted navigation tree for the atom browser: models -> setups ->
    available tests, with flags and counts. One query per level; cheap at
    this table size (hundreds of rows). Setup identity = the tuple (gpu,
    gpu_count, engine, mtp, weights, hicache, replicas) — rows with all
    NULL (un-stamped run records) group under setup=null."""
    with connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        models: list[dict] = []
        for m in conn.execute(
            "SELECT model, COUNT(*) AS n FROM measurements WHERE model IS NOT NULL GROUP BY model ORDER BY model"
        ).fetchall():
            model = m["model"]
            setups: list[dict] = []
            for s in conn.execute(
                """SELECT gpu, gpu_count, engine, mtp, weights, hicache, replicas, COUNT(*) AS n,
                          SUM(CASE WHEN mode='multiturn' THEN 1 ELSE 0 END) AS multiturn_n,
                          SUM(CASE WHEN arrival='open' THEN 1 ELSE 0 END) AS open_n
                   FROM measurements WHERE model = ? GROUP BY gpu, gpu_count, engine, mtp, weights, hicache, replicas
                   ORDER BY gpu, engine, hicache""",
                (model,),
            ).fetchall():
                setup_id = "|".join(
                    "" if s[c] is None else str(s[c])
                    for c in ("gpu", "gpu_count", "engine", "mtp", "weights", "hicache", "replicas")
                )
                tests: list[dict] = []
                for t in conn.execute(
                    """SELECT kind, ctx, users, task, arrival, mode, COUNT(*) AS n
                       FROM measurements WHERE model = ?
                         AND gpu IS ? AND gpu_count IS ? AND engine IS ? AND mtp IS ?
                         AND weights IS ? AND hicache IS ? AND replicas IS ?
                       GROUP BY kind, ctx, users, task, arrival, mode
                       ORDER BY kind, ctx, users, task""",
                    (model, s["gpu"], s["gpu_count"], s["engine"], s["mtp"], s["weights"], s["hicache"], s["replicas"]),
                ).fetchall():
                    tests.append(
                        {
                            "kind": t["kind"],
                            "ctx": t["ctx"],
                            "users": t["users"],
                            "task": t["task"],
                            "arrival": t["arrival"],
                            "mode": t["mode"],
                            "count": t["n"],
                        }
                    )
                setups.append(
                    {
                        "setup_id": setup_id,
                        "gpu": s["gpu"],
                        "gpu_count": s["gpu_count"],
                        "engine": s["engine"],
                        "mtp": s["mtp"],
                        "weights": s["weights"],
                        "hicache": s["hicache"],
                        "replicas": s["replicas"],
                        "count": s["n"],
                        "multiturn_count": s["multiturn_n"],
                        "open_count": s["open_n"],
                        "tests": tests,
                    }
                )
            models.append({"model": model, "count": m["n"], "setups": setups})
        total = conn.execute("SELECT COUNT(*) FROM measurements").fetchone()[0]
    return {"models": models, "total": total}


def query(db_path: str | Path, sql: str, params: list | tuple = ()) -> list[dict]:
    """Read path for every consumer (results page, compare, analyses):
    returns rows as dicts. SELECT-only by convention; the store is written
    through record_* only."""
    with connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def stats(db_path: str | Path) -> dict:
    """Cheap store summary for /api/store/status: row count, models, sources."""
    with connect(db_path) as conn:
        n = conn.execute("SELECT COUNT(*) FROM measurements").fetchone()[0]
        models = [
            r[0] for r in conn.execute("SELECT DISTINCT model FROM measurements WHERE model IS NOT NULL ORDER BY model")
        ]
        runs = conn.execute("SELECT COUNT(DISTINCT run_id) FROM measurements WHERE run_id IS NOT NULL").fetchone()[0]
    return {"measurements": n, "models": models, "runs": runs}
