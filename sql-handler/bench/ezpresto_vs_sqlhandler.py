#!/usr/bin/env python3
"""Benchmark SQLhandler vs EzPresto (PrestoDB) against the same dataset.

Both engines are driven over their native client interfaces:

  * SQLhandler — MCP streamable-http (`tools/call` -> `run_sql`) on /mcp,
    or the REST API on /api/query.
  * EzPresto  — native Presto HTTP API: POST /v1/statement, poll `nextUri`
    until the query finishes. Auth via `Authorization: Bearer <jwt>`.

Suites:
  latency      run a fixed query mix N times per engine; report p50/p95/min/max
  concurrency  fixed query pool at levels 1,2,4,...; report wall, qps, p50/p95
  throughput   full-scan aggregates; report rows/s and query wall time

Results are printed as markdown tables and dumped as JSON for comparison.

Examples:
  export EZPRESTO_BEARER=<keycloak UA access token>
  python bench/ezpresto_vs_sqlhandler.py probe
  python bench/ezpresto_vs_sqlhandler.py run --suite all \
      --workload bench/workload_g2.json \
      --sqlhandler-url https://sqlhandler.<domain>/mcp \
      --presto-url https://ezpresto.<domain> \
      --reps 5 --levels 1,4,8,16

Stdlib only.
"""

import argparse
import concurrent.futures
import json
import os
import ssl
import statistics
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE  # internal PCAI CA; HTTP API only, read-only queries

DEFAULT_WORKLOAD = os.path.join(os.path.dirname(__file__), "workload_g2.json")


# --------------------------------------------------------------------------
# Engines
# --------------------------------------------------------------------------

def _parse_mcp_body(body):
    """Parse an MCP HTTP response: plain JSON or SSE (event: message / data:)."""
    body = body.strip()
    if body.startswith("{"):
        return json.loads(body)
    for line in body.splitlines():  # SSE: take the last data: line with JSON
        line = line.strip()
        if line.startswith("data:"):
            payload = line[5:].strip()
            if payload and payload != "{}":
                try:
                    return json.loads(payload)
                except ValueError:
                    continue
    raise ValueError("unparseable MCP response: %r" % body[:200])


def _count_markdown_rows(text):
    """Count data rows in a markdown table (header + separator excluded)."""
    lines = [l for l in text.splitlines() if l.strip().startswith("|")]
    return max(0, len(lines) - 2)


class McpSqlhandler:
    """SQLhandler via MCP streamable-http tools/call -> run_sql."""

    name = "sqlhandler-mcp"

    def __init__(self, url, bearer="", timeout=300):
        url = url.rstrip("/")
        self.url = url + ("/mcp" if not url.endswith("/mcp") else "")
        self.bearer = bearer
        self.timeout = timeout
        self.session_id = None
        self._init_lock = threading.Lock()
        self._init()

    def _post(self, payload, timeout=None):
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.bearer:
            headers["Authorization"] = "Bearer " + self.bearer
        if self.session_id:
            headers["mcp-session-id"] = self.session_id
        req = urllib.request.Request(
            self.url, data=json.dumps(payload).encode(), headers=headers
        )
        with urllib.request.urlopen(req, timeout=timeout or self.timeout, context=CTX) as r:
            sid = r.headers.get("mcp-session-id")
            if sid:
                self.session_id = sid
            body = r.read().decode()
            return _parse_mcp_body(body) if body.strip() else {}

    def _init(self):
        with self._init_lock:
            self._post({
                "jsonrpc": "2.0", "id": 0, "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26", "capabilities": {},
                    "clientInfo": {"name": "sqlhandler-bench", "version": "1.0"},
                },
            })
            self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def query(self, sql):
        """Run one query; returns (wall_seconds, rows_returned)."""
        t0 = time.monotonic()
        resp = self._post({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "run_sql", "arguments": {"sql": sql}},
        })
        if resp.get("error"):
            raise RuntimeError("mcp error: %s" % json.dumps(resp["error"])[:200])
        content = (resp.get("result") or {}).get("content") or []
        text = content[0].get("text", "") if content else ""
        if (resp.get("result") or {}).get("isError"):
            raise RuntimeError("sql error: %s" % text[:200])
        rows = _count_markdown_rows(text)
        return time.monotonic() - t0, rows


class RestSqlhandler:
    """SQLhandler via the REST JSON API on /api/query."""

    name = "sqlhandler-rest"

    def __init__(self, url, bearer="", timeout=300):
        url = url.rstrip("/")
        if url.endswith("/mcp"):
            url = url[:-4]
        self.url = url + "/api/query"
        self.bearer = bearer
        self.timeout = timeout

    def query(self, sql):
        body = json.dumps({"sql": sql}).encode()
        headers = {"Content-Type": "application/json"}
        if self.bearer:
            headers["Authorization"] = "Bearer " + self.bearer
        req = urllib.request.Request(self.url, data=body, headers=headers)
        t0 = time.monotonic()
        with urllib.request.urlopen(req, timeout=self.timeout, context=CTX) as r:
            payload = json.loads(r.read().decode())
        if payload.get("error"):
            raise RuntimeError("sql error: %s" % str(payload["error"])[:200])
        return time.monotonic() - t0, len(payload.get("rows") or [])


class PrestoEngine:
    """EzPresto via the native Presto HTTP statement API.

    Auth: either a static access token (`bearer`), or a Keycloak refresh
    token (`refresh_token`) which is exchanged for fresh access tokens via
    the token endpoint whenever Presto replies 401. Keycloak rotates refresh
    tokens on every exchange, so the newest one is always kept.
    """

    name = "ezpresto"

    def __init__(self, url, bearer="", timeout=300, user="bench",
                 refresh_token="", token_url="", client_id="ua",
                 client_secret=""):
        self.url = url.rstrip("/")
        self.bearer = bearer
        self.timeout = timeout
        self.user = user
        self.refresh_token = refresh_token
        self.token_url = token_url
        self.client_id = client_id
        self.client_secret = client_secret
        self._token_lock = threading.Lock()
        self._refresh_failures = 0

    def _headers(self):
        headers = {"X-Presto-User": self.user, "X-Presto-Source": "bench"}
        if self.bearer:
            headers["Authorization"] = "Bearer " + self.bearer
        return headers

    def _refresh_access_token(self):
        """Exchange the refresh token for a new access token (thread-safe)."""
        if not (self.refresh_token and self.token_url):
            return False
        with self._token_lock:
            if self._refresh_failures >= 3:
                return False
            body = urllib.parse.urlencode({
                "grant_type": "refresh_token",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "refresh_token": self.refresh_token,
            }).encode()
            req = urllib.request.Request(
                self.token_url, data=body,
                headers={"Content-Type": "application/x-www-form-urlencoded"})
            try:
                with urllib.request.urlopen(req, timeout=30, context=CTX) as r:
                    payload = json.loads(r.read().decode())
            except Exception as exc:  # noqa: BLE001
                self._refresh_failures += 1
                sys.stderr.write("token refresh failed (%d): %s\n"
                                 % (self._refresh_failures, str(exc)[:150]))
                return False
            self.bearer = payload.get("access_token") or self.bearer
            self.refresh_token = payload.get("refresh_token") or self.refresh_token
            return True

    def _fetch(self, url, data=None):
        req = urllib.request.Request(url, data=data, headers=self._headers())
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=CTX) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as exc:
            if exc.code == 401 and self._refresh_access_token():
                req = urllib.request.Request(url, data=data, headers=self._headers())
                with urllib.request.urlopen(req, timeout=self.timeout, context=CTX) as r:
                    return json.loads(r.read().decode())
            raise

    def query(self, sql):
        """POST /v1/statement then poll nextUri; returns (wall_seconds, rows)."""
        t0 = time.monotonic()
        resp = self._fetch(self.url + "/v1/statement", data=sql.encode())
        rows = []
        while True:
            if resp.get("error"):
                raise RuntimeError("presto error: %s" % json.dumps(resp["error"])[:300])
            for col_list in resp.get("data") or []:
                rows.append(col_list)
            nxt = resp.get("nextUri")
            if not nxt:
                break
            resp = self._fetch(nxt)
        return time.monotonic() - t0, len(rows)


# --------------------------------------------------------------------------
# Workload
# --------------------------------------------------------------------------

def load_workload(path):
    with open(path) as fh:
        return json.load(fh)


def engine_for(entry, engine_name):
    q = entry["queries"]
    return q.get(engine_name) or q["sqlhandler"]


# --------------------------------------------------------------------------
# Suites
# --------------------------------------------------------------------------

def pct(sorted_ms, q):
    if not sorted_ms:
        return 0
    return sorted_ms[min(len(sorted_ms) - 1, int(len(sorted_ms) * q))]


def run_one(engine, sql, timeout):
    """Returns (wall_ms, rows, None) or (wall_ms, 0, error_string)."""
    try:
        wall, rows = engine.query(sql)
        return wall * 1000.0, rows, None
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode()[:200]
        except Exception:
            detail = ""
        return exc.ms if hasattr(exc, "ms") else 0.0, 0, "HTTP %s %s" % (exc.code, detail)
    except Exception as exc:  # noqa: BLE001 - report anything as a failed call
        return 0.0, 0, str(exc)[:200]


def suite_latency(engines, workload, reps, timeout):
    """Query mix, sequential, N reps per engine."""
    print("\n## Latency (sequential, %d reps/query, warm cache)\n" % reps)
    print("| query | rows scanned | engine | p50 | p95 | min | max | ok |")
    print("|---|---:|---|---:|---:|---:|---:|---:|")
    results = []
    for entry in workload:
        for engine in engines:
            samples, errors, rows_out = [], 0, 0
            for _ in range(reps):
                ms, rows, err = run_one(engine, engine_for(entry, engine.name), timeout)
                if err:
                    errors += 1
                else:
                    samples.append(ms)
                    rows_out = rows
            if not samples:
                print("| %s | %d | %s | ERR | | | | 0/%d |" %
                      (entry["name"], entry.get("rows_scanned", 0), engine.name, reps))
                results.append({"query": entry["name"], "engine": engine.name, "error": errors})
                continue
            s = sorted(samples)
            results.append({
                "query": entry["name"], "engine": engine.name,
                "rows_scanned": entry.get("rows_scanned", 0),
                "p50_ms": round(pct(s, 0.5), 1), "p95_ms": round(pct(s, 0.95), 1),
                "min_ms": round(s[0], 1), "max_ms": round(s[-1], 1),
                "ok": "%d/%d" % (len(samples), reps),
            })
            print("| %s | %s | %s | %.1f ms | %.1f ms | %.1f ms | %.1f ms | %s |" % (
                entry["name"], entry.get("rows_scanned", 0), engine.name,
                pct(s, 0.5), pct(s, 0.95), s[0], s[-1], "%d/%d" % (len(samples), reps)))
    return results


def suite_concurrency(engines, workload, levels, reps, timeout):
    """Fixed query pool driven at increasing concurrency levels."""
    print("\n## Concurrency (pool of %d queries, %d batch(es)/level)\n" % (len(workload), reps))
    print("| engine | level | calls | ok | wall/batch | qps | p50 | p95 | max |")
    print("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    results = []
    for engine in engines:
        jobs = [(engine_for(e, engine.name), e.get("rows_scanned", 0)) for e in workload]
        for level in levels:
            all_ms, ok, walls = [], 0, []
            for _ in range(reps):
                batch = [jobs[i % len(jobs)] for i in range(level)]
                t0 = time.monotonic()
                with concurrent.futures.ThreadPoolExecutor(max_workers=level) as ex:
                    futs = [ex.submit(run_one, engine, sql, timeout) for sql, _ in batch]
                    outcomes = [f.result() for f in futs]
                walls.append(time.monotonic() - t0)
                for ms, _rows, err in outcomes:
                    if not err:
                        all_ms.append(ms)
                        ok += 1
            s = sorted(all_ms)
            total_calls = level * reps
            wall = sum(walls) / len(walls) if walls else 0
            results.append({
                "engine": engine.name, "level": level, "calls": total_calls, "ok": ok,
                "wall_ms": round(wall * 1000), "qps": round(total_calls / wall, 2) if wall else 0,
                "p50_ms": round(pct(s, 0.5), 1), "p95_ms": round(pct(s, 0.95), 1),
                "max_ms": round(s[-1], 1) if s else 0,
            })
            print("| %s | %d | %d | %d | %.0f ms | %.2f | %.1f ms | %.1f ms | %.1f ms |" % (
                engine.name, level, total_calls, ok, wall * 1000,
                total_calls / wall if wall else 0, pct(s, 0.5), pct(s, 0.95),
                s[-1] if s else 0))
    return results


def suite_throughput(engines, workload, reps, timeout):
    """Full-scan aggregates: rows scanned per second."""
    print("\n## Throughput (full-scan aggregates, %d reps)\n" % reps)
    print("| query | rows/rep | engine | wall (mean) | rows/s | ok |")
    print("|---|---:|---|---:|---:|---:|")
    results = []
    for entry in workload:
        if not entry.get("rows_scanned"):
            continue
        for engine in engines:
            walls, errors = [], 0
            for _ in range(reps):
                ms, _rows, err = run_one(engine, engine_for(entry, engine.name), timeout)
                if err:
                    errors += 1
                else:
                    walls.append(ms / 1000.0)
            mean = statistics.mean(walls) if walls else 0
            rps = entry["rows_scanned"] / mean if mean else 0
            results.append({
                "query": entry["name"], "engine": engine.name,
                "rows_scanned": entry["rows_scanned"],
                "wall_ms": round(mean * 1000, 1), "rows_per_s": round(rps),
                "ok": "%d/%d" % (len(walls), reps),
            })
            print("| %s | %s | %s | %.1f ms | %s | %s |" % (
                entry["name"], entry["rows_scanned"], engine.name, mean * 1000,
                "{:,}".format(int(rps)) if rps else "-", "%d/%d" % (len(walls), reps)))
    return results


# --------------------------------------------------------------------------
# Probe: discover what each engine can see (catalogs / tables)
# --------------------------------------------------------------------------

PROBE_SQLS = ["SHOW CATALOGS"]


def _make_presto(args):
    return PrestoEngine(
        args.presto_url, args.bearer, args.timeout, args.presto_user,
        refresh_token=args.refresh_token, token_url=args.keycloak_token_url,
        client_id=args.keycloak_client_id, client_secret=args.keycloak_client_secret)


def cmd_probe(args):
    print("== SQLhandler (%s) ==" % args.sqlhandler_url)
    try:
        engine = _make_sqlhandler(args)
        wall, rows = engine.query("SHOW TABLES")
        print("SHOW TABLES ok in %.0f ms:" % (wall * 1000))
        print(json.dumps(rows, indent=1)[:2000])
    except Exception as exc:  # noqa: BLE001
        print("sqlhandler probe FAILED: %s" % str(exc)[:300])

    print("\n== EzPresto (%s) ==" % args.presto_url)
    try:
        engine = _make_presto(args)
        wall, rows = engine.query("SHOW CATALOGS")
        print("SHOW CATALOGS ok in %.0f ms:" % (wall * 1000))
        print(json.dumps(rows, indent=1))
        catalogs = [r[0] for r in rows]
        for cat in catalogs:
            if cat.lower() in ("system", "jmx", "network", "cache"):
                continue
            try:
                _, schemas = engine.query("SHOW SCHEMAS FROM %s" % cat)
                print("catalog %s schemas: %s" % (cat, [s[0] for s in schemas]))
                for sch in [s[0] for s in schemas]:
                    if sch in ("information_schema",):
                        continue
                    _, tables = engine.query("SHOW TABLES FROM %s.%s" % (cat, sch))
                    if tables:
                        print("  %s.%s tables: %s" % (cat, sch, [t[0] for t in tables]))
            except Exception as exc:  # noqa: BLE001
                print("  catalog %s: %s" % (cat, str(exc)[:150]))
    except Exception as exc:  # noqa: BLE001
        print("ezpresto probe FAILED: %s" % str(exc)[:300])
    return 0


def _jwt_claims(token):
    """Decode a JWT payload without verifying (typ/exp inspection only)."""
    try:
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        return json.loads(__import__("base64").urlsafe_b64decode(payload_b64))
    except Exception:  # noqa: BLE001
        return {}


def cmd_token(args):
    """Classify a pasted token and/or mint a fresh access token."""
    tok = args.token or args.bearer or args.refresh_token
    if tok:
        claims = _jwt_claims(tok)
        typ = claims.get("typ", "?")
        exp = claims.get("exp")
        remains = ""
        if exp:
            remains = ", expires in %d min" % max(0, int(exp - time.time()) // 60)
        print("token type: %s%s  (iss: %s)" % (typ, remains, claims.get("iss", "?")))
        if typ == "Refresh":
            if not args.refresh_token:
                args.refresh_token = tok
        elif typ not in ("Bearer", "Offline"):
            print("NOTE: unknown typ — treating as refresh token if the mint below fails")

    if not args.refresh_token:
        print("no refresh token available; nothing to mint")
        return 1
    engine = _make_presto(args)
    engine.bearer = ""
    ok = engine._refresh_access_token()
    if not ok:
        print("refresh FAILED — token may be revoked/expired; re-grab from the UI session")
        return 2
    claims = _jwt_claims(engine.bearer)
    exp = claims.get("exp")
    print("minted access token for user %r%s" % (
        claims.get("preferred_username"), 
        (", expires in %d min" % max(0, int(exp - time.time()) // 60)) if exp else ""))
    print("\nEZPRESTO_BEARER=%s" % engine.bearer)
    print("\n(new refresh token for future runs)\nEZPRESTO_REFRESH_TOKEN=%s" % engine.refresh_token)
    return 0


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def _make_sqlhandler(args):
    if args.sqlhandler_mode == "rest":
        return RestSqlhandler(args.sqlhandler_url, args.sqlhandler_bearer, args.timeout)
    return McpSqlhandler(args.sqlhandler_url, args.sqlhandler_bearer, args.timeout)


def cmd_run(args):
    workload = load_workload(args.workload)["queries"]
    engines = []
    if args.only in ("sqlhandler", "all"):
        engines.append(_make_sqlhandler(args))
    if args.only in ("presto", "all"):
        engines.append(_make_presto(args))

    levels = [int(x) for x in args.levels.split(",") if x.strip()]
    print("# SQLhandler vs EzPresto — %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    print("\nengines: %s" % ", ".join(e.name for e in engines))
    print("sqlhandler: %s   presto: %s" % (args.sqlhandler_url, args.presto_url))

    # Warm both engines once per query so caches are comparable.
    print("\nwarming: 1 run/query/engine ...", file=sys.stderr)
    for entry in workload:
        for engine in engines:
            run_one(engine, engine_for(entry, engine.name), args.timeout)

    out = {"workload": os.path.basename(args.workload), "suites": {}}
    if args.suite in ("latency", "all"):
        out["suites"]["latency"] = suite_latency(engines, workload, args.reps, args.timeout)
    if args.suite in ("concurrency", "all"):
        out["suites"]["concurrency"] = suite_concurrency(
            engines, workload, levels, args.reps, args.timeout)
    if args.suite in ("throughput", "all"):
        out["suites"]["throughput"] = suite_throughput(
            engines, workload, args.reps, args.timeout)

    if args.json_out:
        with open(args.json_out, "w") as fh:
            json.dump(out, fh, indent=1)
        print("\nJSON results -> %s" % args.json_out)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--sqlhandler-url",
                        default=os.environ.get(
                            "SQLHANDLER_URL",
                            "https://sqlhandler.pcai-se-ai-application.hst.rdlabs.hpecorp.net"))
    common.add_argument("--sqlhandler-mode", default="rest", choices=["mcp", "rest"])
    common.add_argument("--sqlhandler-bearer", default=os.environ.get("SQLHANDLER_BEARER", ""))
    common.add_argument("--presto-url",
                        default=os.environ.get(
                            "PRESTO_URL",
                            "https://ezpresto.pcai-se-ai-application.hst.rdlabs.hpecorp.net"))
    common.add_argument("--bearer", default=os.environ.get("EZPRESTO_BEARER", ""),
                        help="Bearer JWT access token for ezpresto (Keycloak UA realm)")
    common.add_argument("--refresh-token", default=os.environ.get("EZPRESTO_REFRESH_TOKEN", ""),
                        help="Keycloak refresh token; auto-mints access tokens on 401")
    common.add_argument("--keycloak-token-url",
                        default=os.environ.get(
                            "KEYCLOAK_TOKEN_URL",
                            "https://keycloak.pcai-se-ai-application.hst.rdlabs.hpecorp.net"
                            "/realms/UA/protocol/openid-connect/token"))
    common.add_argument("--keycloak-client-id", default=os.environ.get("KEYCLOAK_CLIENT_ID", "ua"))
    common.add_argument("--keycloak-client-secret",
                        default=os.environ.get("KEYCLOAK_CLIENT_SECRET", ""))
    common.add_argument("--presto-user", default=os.environ.get("PRESTO_USER", "bench"))
    common.add_argument("--timeout", type=int, default=300)

    p_probe = sub.add_parser("probe", parents=[common],
                             help="list catalogs/schemas/tables on both engines")
    p_probe.set_defaults(func=cmd_probe)

    p_tok = sub.add_parser("token", parents=[common],
                           help="classify a pasted token / mint a fresh access token")
    p_tok.add_argument("token", nargs="?", default="",
                       help="access or refresh token (either works)")
    p_tok.set_defaults(func=cmd_token)

    p_run = sub.add_parser("run", parents=[common], help="run the benchmark")
    p_run.add_argument("--suite", default="all",
                       choices=["latency", "concurrency", "throughput", "all"])
    p_run.add_argument("--only", default="all", choices=["all", "sqlhandler", "presto"])
    p_run.add_argument("--workload", default=DEFAULT_WORKLOAD)
    p_run.add_argument("--reps", type=int, default=5)
    p_run.add_argument("--levels", default="1,4,8,16")
    p_run.add_argument("--json-out", default=None)
    p_run.set_defaults(func=cmd_run)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
