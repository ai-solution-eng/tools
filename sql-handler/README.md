# SQLhandler

Fast, direct SQL access to columnar data, exposed as an **MCP server** — a
drop-in **EzPresto / PrestoDB replacement**. It reads tabular data **directly**
from the source with pyarrow and queries it with **DuckDB** over the exposed
pyarrow datasets, pushing predicates / column projection into the scan.

Four backends are supported (selected by `SQLHANDLER_BACKEND`):

- **onelake** (default) — Microsoft Fabric OneLake (Delta Lake over ABFS)
- **s3 / minio** — S3-compatible object storage (Parquet files)
- **iceberg** — Apache Iceberg tables through a catalog (REST or SQL), Parquet data files
- **nfs** — a mounted directory (NFS / PVC / hostPath): Delta Lake **and** Parquet

They all share the same SQL engine, caches, MCP tools, and backend-aware
readiness probe; only the `DataProvider` behind them differs. See
[Backend comparison](#backend-comparison-onelake-fabric-vs-s3-minio).

## Backend comparison: OneLake (Fabric) vs S3 (MinIO)

Both backends speak the same MCP tools and SQL engine. The differences are
only in *where* the data lives, how you authenticate, and how tables are
discovered:

| Aspect | OneLake (`onelake`) | S3 / MinIO (`s3`) |
|---|---|---|
| Format | Delta Lake (Parquet + `_delta_log`) | Parquet (plain or Hive-partitioned) |
| Credentials | Entra **service principal** (tenant/client/secret) | S3 **access/secret key**, or anonymous for public buckets |
| Auth flow | AAD `client_credentials` → storage-scoped bearer token | AWS SigV4 (pyarrow's bundled SDK) |
| Table root | `Tables/` under the lakehouse | `S3_BUCKET` + optional `S3_PREFIX` |
| Table layout | `Tables/<schema>/<table>` | `<prefix>/<schema>/<table>`, `<prefix>/<table>`, or a single file |
| Discovery API | DFS REST (`?resource=filesystem`) — blob listing unsupported | S3 `ListObjectsV2` |
| Reader | `deltalake` → pyarrow Dataset | `pyarrow` S3 filesystem → pyarrow Dataset |
| URI shown by `describe_table` | `abfss://<ws>@.../<lh>/Tables/...` | `s3://<bucket>/<prefix>/...` |
| Auth settings | no proxy/endpoint — AAD only | `S3_ENDPOINT_URL`, `S3_REGION`, `S3_ANONYMOUS`, `S3_USE_SSL` |

### Minimal config, side by side

**OneLake (Fabric)** — `SQLHANDLER_BACKEND=onelake` (default):

```bash
export SQLHANDLER_BACKEND=onelake
export FABRIC_TENANT_ID=<tenant-id>
export FABRIC_CLIENT_ID=<client-id>
export FABRIC_CLIENT_SECRET=<secret>            # keep in a Secret / .env
export FABRIC_LAKEHOUSE_ABFSS_URL=abfss://<ws>@onelake.dfs.fabric.microsoft.com/<lh>
# …or FABRIC_WORKSPACE_ID + FABRIC_LAKEHOUSE_ID instead of the full URL
sqlhandler --transport streamable-http --port 9097
```

**S3 / MinIO** — `SQLHANDLER_BACKEND=s3`:

```bash
export SQLHANDLER_BACKEND=s3
export S3_ENDPOINT_URL=http://127.0.0.1:9000   # MinIO; omit for AWS
export S3_REGION=us-east-1
export S3_ACCESS_KEY=<access-key>
export S3_SECRET_KEY=<secret-key>               # never in source control
export S3_BUCKET=lakehouse                      # required
export S3_PREFIX=datasets                       # optional sub-tree
sqlhandler --transport streamable-http --port 9097
```

**Which one?**

- Use **onelake** when the data already lives in Microsoft Fabric (as the
  Toromont lakehouse does) — zero copying, Delta's transactional metadata.
- Use **s3** when your data is Parquet in object storage (MinIO on-prem, AWS
  S3, GCS-interop) or you want a plain-file lake that any tool can dump into.
- Use **iceberg** when you have a real lakehouse catalog — schema/time-travel
  management, concurrent writers, cross-engine reads. It is the catalog-only
  option in the [roadmap](#other-data-sources--roadmap).
- Both onelake and s3 work through the same Helm chart (`backend:` value in
  `values.yaml`); see the deployment sections below.
## Why it's faster

| Approach | What happens on a 40M-row table |
|---|---|
| EzPresto/PrestoDB | Rows pulled over JDBC, re-serialized, full scans |
| **SQLhandler** | Delta scan reads only needed columns + filters from Parquet on OneLake |

## Architecture

```
LLM agent ──MCP──> sqlhandler (MCP server) ──DuckDB + pyarrow──> DataProvider
                                             │
                                             ├── OneLakeProvider  (ABFS, Delta Lake)
                                             ├── S3Provider       (MinIO/AWS, Parquet)
                                             ├── IcebergProvider  (catalog + Parquet)
                                             └── FileProvider     (NFS/local, Delta + Parquet)
```

Components:

- `src/sqlhandler/config.py`    — env-driven connection config + backend selection
- `src/sqlhandler/provider.py`  — `DataProvider` interface + `TableInfo`
- `src/sqlhandler/engine.py`    — shared SQL engine: DuckDB queries, scans, caches
- `src/sqlhandler/onelake.py`   — OneLake (ABFS/Delta) provider
- `src/sqlhandler/s3.py`        — S3/MinIO (Parquet) provider
- `src/sqlhandler/iceberg.py`    — Iceberg (catalog + Parquet) provider
- `src/sqlhandler/file.py`        — NFS/local filesystem (Delta + Parquet) provider
- `src/sqlhandler/server.py`    — the MCP server (`list_tables`, `describe_table`,
                                  `run_sql`, `scan_table`)

## OneLake access notes

- Table discovery uses the **DFS REST API** (`?resource=filesystem`) — OneLake
  does not implement the ADLS Gen2 *blob* list API that `pyarrow.AzureFileSystem`
  uses, so `fs.get_file_info`-style listing fails with `501 Not Implemented`.
- Tables live under schema folders: `Tables/<schema>/<table>` (e.g.
  `Tables/workorder/work_order_header`). `list_tables` returns a flat list keyed
  by `<schema>/<table>`; queries may reference the bare name or
  `<schema>_<name>`.
- The ABFS account is the workspace GUID; `deltalake` is configured with
  `account_name=onelake` + the service principal via `azure_tenant_id` /
  `azure_client_id` / `azure_client_secret`, plus the `onelake.dfs...` endpoints.

## S3 / MinIO (Parquet) backend

Set `SQLHANDLER_BACKEND=s3` (or `minio`) and point it at an S3-compatible
store. Everything else — SQL engine, caches, MCP tools — is unchanged.

```bash
cd SQLhandler
SQLHANDLER_BACKEND=s3 \
  S3_ENDPOINT_URL=http://127.0.0.1:9000 \
  S3_ACCESS_KEY=minioadmin S3_SECRET_KEY=minioadmin \
  S3_BUCKET=lakehouse S3_PREFIX=datasets \
  sqlhandler --transport streamable-http --host 0.0.0.0 --port 9097
```

### How tables are discovered

Under `S3_BUCKET` (+ optional `S3_PREFIX`), every Parquet file or folder of
Parquet files becomes a table:

| Layout | Table | SQL name |
|---|---|---|
| `<prefix>/orders.parquet` | `orders` | `orders` |
| `<prefix>/customers/*.parquet` | `customers` | `customers` |
| `<prefix>/sales/customers/*.parquet` | schema `sales`, table `customers` | `customers` or `sales_customers` |
| `<prefix>/hr/employees/year=2024/*.parquet` | schema `hr`, table `employees` | `employees` or `hr_employees` |

Hive partition folders (`key=value`) inside a table folder are folded into
the table; hidden folders (starting with `.`) are ignored. Filtering and
projection push down to the Parquet scan exactly as with the OneLake backend.

### MinIO specifics

- pyarrow (already a dependency) talks to S3 directly — **no extra pip
  dependency** (no boto3 / s3fs).
- `S3_ENDPOINT_URL` may omit the scheme; `http` is assumed unless
  `S3_USE_SSL=true`.
- Path-style addressing is the default (`S3_PATH_STYLE=true`), which is what
  MinIO uses.
- `S3_ANONYMOUS=true` serves a public bucket without keys.

### Quick local test

```bash
# Start MinIO (or any S3) once:
minio server /tmp/minio-data --address 127.0.0.1:9000

# Run the gated integration test against the live endpoint:
SQLHANDLER_TEST_MINIO=1 SQLHANDLER_TEST_S3_ENDPOINT=http://127.0.0.1:9000 \
  SQLHANDLER_TEST_S3_ACCESS_KEY=minioadmin SQLHANDLER_TEST_S3_SECRET_KEY=minioadmin \
  pytest tests/test_s3_integration.py -q
```

## Iceberg (catalog) backend

Set `SQLHANDLER_BACKEND=iceberg` to query **Apache Iceberg** tables through a
catalog. Iceberg keeps its table metadata (snapshots, manifests with per-file
stats) next to the Parquet data files, so a table is a first-class entity:
real schemas, time travel, concurrent writers, cross-engine reads. This
server reads the manifest-listed Parquet files with the same pyarrow engine.

```bash
export SQLHANDLER_BACKEND=iceberg
export ICEBERG_CATALOG_TYPE=rest          # or sql for a local SQL catalog
export ICEBERG_CATALOG_URI=http://rest-catalog:8181
# export ICEBERG_CATALOG_TOKEN=<token>
# export ICEBERG_NAMESPACE=analytics      # optional namespace filter
# S3 storage for the data files (same S3_* vars as the s3 backend):
export S3_ENDPOINT_URL=http://127.0.0.1:9000
export S3_ACCESS_KEY=minioadmin S3_SECRET_KEY=minioadmin
sqlhandler --transport streamable-http --port 9097
```

Two catalog types are supported:

- **`rest`** (default) — an Iceberg REST catalog (Dremio, Nessie, Amazon S3
  Tables). `ICEBERG_CATALOG_URI` is the REST endpoint.
- **`sql`** — a SQL catalog (SQLite/Postgres) for local dev/tests:
  `ICEBERG_CATALOG_TYPE=sql` and `ICEBERG_CATALOG_URI=sqlite:///path/catalog.db`.
  Writers and readers must use the same `ICEBERG_CATALOG_NAME`.

Tables are addressed as `<namespace>/<name>` and read as Parquet; `run_sql` /
`scan_table` push predicates/projections into the Parquet scan as with the
other backends. Install the optional dependency first:

```bash
pip install 'sqlhandler[iceberg]'
```

### Quick local test

```bash
# create a local SQL catalog + a table with pyiceberg, then:
SQLHANDLER_TEST_ICEBERG=1 pytest tests/test_iceberg_integration.py -q
```

## NFS / local filesystem backend

Set `SQLHANDLER_BACKEND=nfs` (or `file`/`local`) to read tables from a
directory mounted into the machine — NFS via a PV/PVC, a hostPath, or any
volume. Backed by pyarrow's `LocalFileSystem`, it needs no credentials or
endpoint, and it auto-detects both formats:

- **Delta Lake** tables (any folder containing a `_delta_log/`) — read with
  `deltalake`, with snapshot-version-aware cache invalidation (see caching).
- **Parquet** files/folders — same conventions as the S3 backend.

Parquet data files *inside* a Delta folder are folded into that Delta table,
never exposed as separate tables; hidden (`.`) paths are skipped.

```bash
export SQLHANDLER_BACKEND=nfs
export NFS_ROOT=/data                        # mounted directory with tables
sqlhandler --transport streamable-http --port 9097
```

### How tables are discovered

| Layout | Format | Table | SQL name |
|---|---|---|---|
| `<root>/orders.parquet` | parquet | `orders` | `orders` |
| `<root>/sales/customers.parquet` | parquet | schema `sales`, `customers` | `customers` / `sales_customers` |
| `<root>/work_orders/` (+ `_delta_log/`) | **delta** | `work_orders` | `work_orders` |
| `<root>/sales/work_orders/` (+ `_delta_log/`) | **delta** | schema `sales`, `work_orders` | `work_orders` / `sales_work_orders` |

### Kubernetes / Helm (NFS)

Mount a PVC (or hostPath) with the tables and point the chart at it:

```bash
helm install sqlhandler ./helm \
  --namespace <ns> \
  --set-string ezua.domainName=<your-domain> \
  --set backend=nfs \
  --set nfs.rootDir=/data \
  --set nfs.mount.enabled=true \
  --set nfs.mount.pvcName=my-data-pvc
```

The chart sets `NFS_ROOT`, mounts the PVC read-only at `/data`, and the
readiness probe verifies the mount is present.

### Caching & freshness (NFS Delta)

Like every backend, the NFS backend reuses open Dataset handles for
`SQLHANDLER_DATASET_CACHE_TTL` and pre-warms schemas via
`cache.prewarmTables`. On top of that, Delta Lake tables on NFS get
**snapshot-version-aware invalidation**: the engine re-checks the table's
`_delta_log` version and reopens the dataset when an ETL commit bumps it, so
new rows appear without a restart. The re-check is throttled by
`cache.versionCheckInterval` (values.yaml) / `SQLHANDLER_VERSION_CHECK_INTERVAL`
(default `10` seconds; `0` = re-check on every query). ETL jobs writing to the
mount therefore show up within a few seconds on a cached replica.
## Setup

```bash
cd SQLhandler
cp config/.env.example config/.env   # fill in FABRIC_* (do NOT commit secret)
uv sync                               # or: pip install -e .
sqlhandler --transport stdio          # run as MCP server (stdio)
```

To run as a streamable HTTP server (**MCP 2.0 SDK**, `mcp>=2.0.0`,
stateless streamable-http) on a port:

```bash
sqlhandler --transport streamable-http --host 0.0.0.0 --port 9097
# MCP endpoint:  http://host:9097/mcp
# health/ready:  http://host:9097/health  http://host:9097/ready
```

## Configuration (environment)

| Variable | Purpose |
|---|---|
| `FABRIC_TENANT_ID` | Entra tenant id |
| `FABRIC_CLIENT_ID` | Service principal client id |
| `FABRIC_CLIENT_SECRET` | Service principal secret (**keep in .env/secret, never commit**) |
| `FABRIC_LAKEHOUSE_ABFSS_URL` | Full ABFS URL to the lakehouse (preferred) |
| `FABRIC_WORKSPACE_ID` | Workspace GUID (fallback) |
| `FABRIC_LAKEHOUSE_ID` | Lakehouse GUID (fallback) |
| `FABRIC_AUTHORITY` | OneLake authority (default `onelake.dfs.fabric.microsoft.com`) |
| `SQLHANDLER_BACKEND` | Data source: `onelake` (default), `s3`/`minio`, `iceberg`, or `nfs`/`file`/`local` |
| `S3_ENDPOINT_URL` | S3 endpoint, e.g. `http://127.0.0.1:9000` for MinIO |
| `S3_REGION` | S3 region (default `us-east-1`) |
| `S3_ACCESS_KEY` / `S3_SECRET_KEY` | S3 credentials |
| `S3_BUCKET` | S3 bucket that holds the tables |
| `S3_PREFIX` | Optional sub-tree within the bucket (default empty) |
| `S3_ANONYMOUS` | `true` to read a public bucket without keys |
| `S3_USE_SSL` | Force `https` when `S3_ENDPOINT_URL` has no scheme |
| `ICEBERG_CATALOG_TYPE` | `rest` (default) or `sql` |
| `ICEBERG_CATALOG_URI` | REST endpoint or `sqlite:///path` (sql) |
| `ICEBERG_CATALOG_TOKEN` | Optional REST bearer token |
| `ICEBERG_CATALOG_NAME` | Catalog name (SQL catalogs partition by it; default `sqlhandler`) |
| `ICEBERG_WAREHOUSE` | Optional table location root |
| `ICEBERG_NAMESPACE` | Optional namespace filter for `list_tables` |
| `NFS_ROOT` | Mounted directory with tables (nfs backend; required) |
| `SQLHANDLER_CACHE_TTL` | Seconds to keep `list_tables`/`describe_table` results in memory (default `3600`, `0` disables) |
| `SQLHANDLER_DATASET_CACHE_TTL` | Seconds to reuse an open Dataset handle (default `3600`, `0` disables) |
| `SQLHANDLER_DATASET_CACHE_TABLES` | Max tables held in the dataset LRU (default `8`) |
| `SQLHANDLER_VERSION_CHECK_INTERVAL` | How often a Delta table's snapshot version is re-checked (default `10`s; `0` = every query) |
| `SQLHANDLER_MAX_ROWS` | Server-side row cap for `run_sql` results (default `1000`; `0` = unlimited) |
| `SQLHANDLER_MAX_OUTPUT_ROWS` | Max rows rendered as markdown to a client (default `1000`) |
| `SQLHANDLER_READINESS_CHECK` | `0` to disable the backend-aware `/ready` connectivity probe |
| `SQLHANDLER_PREWARM_TABLES` | Comma-separated tables whose schemas are warmed at startup, e.g. `work_order_header,work_order_note_recent` |

## Performance & caching

`list_tables` (DFS REST metadata) and `describe_table` (Delta `_delta_log`)
are the calls agents/OWUI repeat on every session, and both hit the
lakehouse every time. The server now keeps both in an in-process cache:

- A single process-wide handler is reused (previously a new handler was
  built per tool call, silently discarding the table-list cache).
- `describe_table` results are cached per table for `SQLHANDLER_CACHE_TTL`
  seconds, so the frequently-described tables return from memory (~ms)
  instead of re-reading the Delta log (~8s).
- `SQLHANDLER_PREWARM_TABLES` warms those schemas in a background thread at
  first request, so even the first describe is a hit.
- Open Delta datasets are reused per table for `SQLHANDLER_DATASET_CACHE_TTL`
  (default `3600`) seconds, LRU-bounded by `SQLHANDLER_DATASET_CACHE_TABLES`
  (default `8`). This removes the per-query Delta `_delta_log` re-open (the
  serialized part of every call) so concurrent queries stop piling up on it —
  the rows themselves are still fetched from the source on every scan, so
  query results are never cached.
- **Delta snapshot-version-aware invalidation**: for Delta Lake tables (nfs
  and onelake) the engine stores the table's snapshot version with the cached
  handle and re-checks it on reuse (`SQLHANDLER_VERSION_CHECK_INTERVAL`,
  default `10`s). When an ETL commit bumps the version, the cached dataset is
  reopened so new rows appear without waiting out the TTL and without a restart.

The caches are **in-process**: a new replica starts fresh (and pre-warms).
They only cache metadata — query results are never cached, so you always see
the latest rows. Set the TTL envs to `0` to disable.

## PCAI / MCP 2.0 deployment (Helm)

SQLhandler ships as a **PCAI (HPE Ezmeral Unified Analytics) MCP 2.0** server —
it is built on the **`mcp>=2.0.0` SDK's low-level `Server`** and serves the
**standard MCP protocol** (initialize handshake) over stateless
streamable-http at `/mcp`, so any standard MCP client can connect (DSH,
official Python/TS SDKs, MCP Inspector, OWUI). The `helm/` chart wires it
into the PCAI platform (Istio gateway, oauth2-proxy auth, vendor-service
discovery labels) exactly like the MultimodalRAG and AgentBuilder charts.

### Endpoint

| Access method | URL |
|---|---|
| Via PCAI gateway (production) | `https://sqlhandler.<your-domain>/mcp` |
| `kubectl port-forward` (local) | `http://localhost:9097/mcp` |

### Deploy

```bash
cd SQLhandler

# 1. Create the Fabric credential Secret out-of-band (never in values.yaml):
kubectl -n <ns> create secret generic fabric-credentials \
  --from-literal=tenant-id="$FABRIC_TENANT_ID" \
  --from-literal=client-id="$FABRIC_CLIENT_ID" \
  --from-literal=client-secret="$FABRIC_CLIENT_SECRET"

# 2. Install with the PCAI domain + lakehouse coordinates:
helm install sqlhandler ./helm \
  --namespace <ns> \
  --set-string ezua.domainName=<your-domain> \
  --set-string ezua.virtualService.endpoint=sqlhandler.<your-domain> \
  --set fabric.lakehouseAbfssUrl=<abfss://ws@onelake.../lh>

# For the Toromont deployment, a ready-made values file with the real
# service principal + OneLake coordinates is provided (DO NOT commit it):
#   helm install sqlhandler ./helm -n <ns> -f helm/deploy-toromont-values.yaml \
#     --set-string ezua.domainName=<your-domain>
# With it, the chart creates the Secret itself (fabric.credentialsSecret.create=true)
# and wires the credential env vars (FABRIC_TENANT_ID / FABRIC_CLIENT_ID /
# FABRIC_CLIENT_SECRET) explicitly into the Deployment.
```

### Deploy the S3 / MinIO backend

```bash
kubectl -n <ns> create secret generic s3-credentials \
  --from-literal=access-key="$S3_ACCESS_KEY" \
  --from-literal=secret-key="$S3_SECRET_KEY"

helm install sqlhandler ./helm \
  --namespace <ns> \
  --set-string ezua.domainName=<your-domain> \
  --set backend=s3 \
  --set s3.endpointUrl=http://minio:9000 \
  --set s3.bucket=lakehouse \
  --set s3.prefix=datasets
```

The chart wires `SQLHANDLER_BACKEND=s3` and the S3 env vars, and injects
`S3_ACCESS_KEY` / `S3_SECRET_KEY` from the `s3-credentials` Secret. For a
public bucket set `--set s3.anonymous=true` (no credential Secret needed).
### Deploy the Iceberg backend

```bash
kubectl -n <ns> create secret generic iceberg-credentials \
  --from-literal=token="$ICEBERG_CATALOG_TOKEN" \
  --from-literal=access-key="$S3_ACCESS_KEY" \
  --from-literal=secret-key="$S3_SECRET_KEY"

helm install sqlhandler ./helm \
  --namespace <ns> \
  --set-string ezua.domainName=<your-domain> \
  --set backend=iceberg \
  --set iceberg.catalogType=rest \
  --set iceberg.catalogUri=http://rest-catalog:8181 \
  --set iceberg.storage.endpointUrl=http://minio:9000
```

The chart wires `SQLHANDLER_BACKEND=iceberg` and the `ICEBERG_*` env vars,
injecting the REST token and S3 storage keys from the `iceberg-credentials`
Secret. `ICEBERG_CATALOG_TYPE=sql` (a local SQL catalog) needs no Secret at
all when the warehouse is local.
The OneLake/Fabric Secret wiring is only rendered when `backend: onelake`,
so an S3-only deployment needs no Fabric credential at all.
The chart renders, when `ezua.enabled=true` (default):

- `Deployment` + `Service` (port `9097`, MCP at `/mcp`)
- Istio `VirtualService` routing `/mcp` to the service with a **long timeout**
  (3600s default) for agent loops / streaming, and a short timeout elsewhere
- Istio `AuthorizationPolicy` (oauth2-proxy) so PCAI clients authenticate
- a Kyverno `ClusterPolicy` that tags the workload `hpe-ezua/type:
  vendor-service` + `hpe-ezua/app: sqlhandler` for PCAI discovery/monitoring
- liveness probe against `/health`, and a **backend-aware readiness probe**
  against `/ready` — it performs a cheap connectivity check on the selected
  backend (OneLake DFS token+list, S3 list, Iceberg catalog, NFS root) and
  returns 503 when the data source is unreachable, so broken-credential pods
  are drained from the Service instead of serving errors

Set `ezua.enabled=false` to disable the PCAI integration (VirtualService,
AuthorizationPolicy, Kyverno) and deploy as a plain MCP server.

### Connect an MCP client

```json
{
  "mcpServers": {
    "sqlhandler": {
      "url": "https://sqlhandler.<your-domain>/mcp",
      "headers": { "Authorization": "Bearer <token>" }
    }
  }
}
```

## MCP tools

- `list_tables`       — enumerate tables in the configured source (any backend)
- `describe_table`    — columns/types/URI for a table
- `run_sql`           — run SQL via DuckDB (aggregations/joins), results as markdown
- `scan_table`        — pull columns/rows via pyarrow with a row limit

## Other data sources / roadmap

The DataProvider interface is the only thing a new source touches - new
backends are a subclass plus one make_provider branch. In rough order of
value for large-scale data access:

| Source | What it gives you | Cost |
|---|---|---|
| Delta Lake on S3 | Your Parquet already has a Delta _delta_log (transactions, time travel) but sits in an S3 bucket. Reuses the same deltalake reader as OneLake, just against s3:// storage. | Low - a format flag on S3Provider |
| DuckDB httpfs native | Let DuckDB read S3 Parquet directly via its bundled httpfs extension instead of through a pyarrow dataset - fewer moving parts, sometimes better pushdown. | Low - a query path variant |
| **Local / NFS directory** | **Implemented** as the `nfs` backend (Delta + Parquet) - see above. | Done |
| Azure Blob / ADLS Gen2 | ADLS/blob storage that has not been loaded into Fabric, using pyarrow's AzureFileSystem. | Low |
| GCS | Google Cloud Storage Parquet via pyarrow's GCS filesystem. | Low |
| **Iceberg tables** | **Implemented** as an `iceberg` backend (REST or SQL catalog) — see above. | Done |
| Hive / HMS catalog | If a Hive Metastore already owns the metadata, list/describe through it and read the underlying Parquet. | Medium |
| Trino / BigQuery / Snowflake | A distributed engine as the source - only needed when object-store reads alone cannot carry the concurrency or the compute already lives there. | Medium-high - new SQL bridge |

Rule of thumb: if the data is (or can be) columnar Parquet/Delta in an object
store, pointing a DataProvider at it beats round-tripping rows through a
JDBC/engine bridge - which is exactly why SQLhandler replaced EzPresto.
## Security note

The temporary service principal credential from Toromont was delivered in
plaintext email. Keep it only in `config/.env` (git-ignored) or the deployment
secret store, and **rotate it** once the integration is confirmed.

## Dev

```bash
uv pip install -e .[dev]
ruff check src
pytest
```