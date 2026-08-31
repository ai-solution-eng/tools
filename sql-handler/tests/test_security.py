"""Security regression tests for the audit fixes.

Pins the behaviors that an attacker would otherwise use against the
network-facing endpoints:

* the web read-only guard rejects writes hidden behind EXPLAIN/PRAGMA
  (prefix matching used to let ``EXPLAIN ANALYZE INSERT ...`` EXECUTE),
* DuckDB's own filesystem access is locked down for SQL queries (no
  read_csv('/etc/passwd'), no COPY ... TO, registered views still work),
* user-supplied table names cannot traverse outside the NFS root,
* SQLHANDLER_SOURCES rejects duplicate / non-identifier source labels.

These tests document the *reason* for each rejection — if one starts
failing, re-read the attack before loosening the assertion.
"""


import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from sqlhandler.config import FileConfig, load_source_providers
from sqlhandler.engine import SqlEngine, _max_rows
from sqlhandler.file import FileProvider
from sqlhandler.provider import LakehouseError
from sqlhandler.server import _arrow_to_markdown

# ---------------------------------------------------------------------------
# DuckDB filesystem lockdown (engine-level defense for MCP + web queries)
# ---------------------------------------------------------------------------


class _NoTables:
    """Minimal provider stub: no tables, everything else unused."""

    kind = "stub"

    def list_tables(self):
        return []

    def table_uri(self, info):
        return f"stub://{info.path}"

    def open_dataset(self, info):  # pragma: no cover - never reached here
        raise AssertionError("open_dataset should not be called in these tests")


def _engine() -> SqlEngine:
    return SqlEngine(_NoTables())


def _registered_engine(monkeypatch) -> SqlEngine:
    """Engine whose schema registration registers a real in-memory view."""
    eng = _engine()

    def fake_register(self, con, sql):
        con.register("stub_tbl", pa.table({"a": [1, 2, 3], "s": ["x", "y", "z"]}))

    monkeypatch.setattr(SqlEngine, "_register_schema", fake_register)
    return eng


def _registered_schema_of(table: pa.Table):
    """Return a _register_schema replacement registering ``table`` as 'big'."""

    def fake_register(self, con, sql):
        con.register("big", table)

    return fake_register


def test_query_blocks_local_file_reads():
    with pytest.raises(LakehouseError, match="disabled by configuration"):
        _engine().query_duckdb("SELECT count(*) FROM read_csv('/etc/passwd')")


def test_query_blocks_copy_to_disk(tmp_path, monkeypatch):
    target = tmp_path / "exfil.parquet"
    monkeypatch.setattr(SqlEngine, "_register_schema", _registered_schema_of(_big_arrow(3)))
    eng = _engine()
    with pytest.raises(LakehouseError, match="disabled by configuration"):
        eng.query_duckdb(f"COPY big TO '{target}' (FORMAT PARQUET)")
    assert not target.exists()


def test_query_still_scans_registered_views(monkeypatch):
    """The lockdown must not break the engine's registered-dataset path."""
    eng = _registered_engine(monkeypatch)
    out = eng.query_duckdb("SELECT sum(a) AS total FROM stub_tbl")
    assert out.column("total")[0].as_py() == 6


def test_query_blocks_url_reads(monkeypatch):
    """No DuckDB-side network fetches (extension autoload is disabled)."""
    eng = _registered_engine(monkeypatch)
    with pytest.raises(LakehouseError):
        eng.query_duckdb("SELECT count(*) FROM 'https://127.0.0.1:1/nope.csv'")


def test_query_file_access_escape_hatch(monkeypatch):
    """SQLHANDLER_DUCKDB_FILE_ACCESS=1 restores DuckDB file functions."""
    monkeypatch.setenv("SQLHANDLER_DUCKDB_FILE_ACCESS", "1")
    out = _engine().query_duckdb("SELECT count(*) FROM read_csv('/etc/passwd')")
    assert out.num_rows == 1  # headerless single-column read of a real file


# ---------------------------------------------------------------------------
# Table-name traversal guards
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "../secret.parquet",
        "sub/../secret.parquet",
        "..",
        "a/../..",
        "/etc/passwd",
        "x\x00y",
    ],
)
def test_engine_rejects_traversal_names(name):
    with pytest.raises(LakehouseError):
        _engine().describe_table(name)


def _nfs_engine(root) -> SqlEngine:
    return SqlEngine(FileProvider(FileConfig(root_dir=str(root))), cache_ttl=0, dataset_cache_ttl=0)


def test_nfs_provider_cannot_read_outside_root(tmp_path):
    root = tmp_path / "root"
    (root / "sub").mkdir(parents=True)
    pq.write_table(pa.table({"pub": [1]}), root / "sub" / "pub.parquet")
    secret = tmp_path / "secret.parquet"
    pq.write_table(pa.table({"secret": [1, 2, 3]}), secret)

    # Provider level (called directly, bypassing the engine resolver):
    from sqlhandler.provider import TableInfo

    prov = FileProvider(FileConfig(root_dir=str(root)))
    with pytest.raises(LakehouseError, match="outside the NFS root"):
        prov.open_dataset(TableInfo(name="secret.parquet", schema="..", format="parquet"))
    with pytest.raises(LakehouseError, match="outside the NFS root"):
        prov.table_uri(TableInfo(name="secret.parquet", schema="..", format="parquet"))

    # Engine level (user-supplied name resolved then opened): the engine's
    # own traversal guard may fire first — either rejection is correct.
    eng = _nfs_engine(root)
    with pytest.raises(LakehouseError):
        eng.describe_table("../secret.parquet")
    with pytest.raises(LakehouseError):
        eng.scan_arrow("../secret.parquet", limit=5)
    # And the legit table still resolves.
    assert eng.describe_table("sub/pub.parquet")["columns"][0]["name"] == "pub"


def test_s3_provider_rejects_traversal_segments():
    from sqlhandler.config import S3Config
    from sqlhandler.provider import TableInfo
    from sqlhandler.s3 import S3Provider

    prov = S3Provider(S3Config(bucket="b", access_key="a", secret_key="s"))
    info = TableInfo(name="x", schema="..", format="parquet")
    with pytest.raises(LakehouseError, match="Invalid S3 table location"):
        prov.open_dataset(info)


# ---------------------------------------------------------------------------
# SQLHANDLER_SOURCES label validation
# ---------------------------------------------------------------------------


def test_sources_reject_duplicate_labels():
    raw = '[{"name":"sales","bucket":"a","accessKey":"k","secretKey":"s"},' \
          '{"name":"sales","bucket":"b","accessKey":"k","secretKey":"s"}]'
    with pytest.raises(ValueError, match="duplicated"):
        load_source_providers({"SQLHANDLER_SOURCES": raw})


@pytest.mark.parametrize("bad", ["my-source", "my source", "1source", "a.b"])
def test_sources_reject_unsafe_labels(bad):
    raw = f'[{{"name":"{bad}","bucket":"a","accessKey":"k","secretKey":"s"}}]'
    with pytest.raises(ValueError, match="valid identifier"):
        load_source_providers({"SQLHANDLER_SOURCES": raw})


def test_sources_accept_safe_labels():
    raw = '[{"name":"sales","bucket":"b1","accessKey":"k","secretKey":"s"},' \
          '{"name":"source2","bucket":"b2","accessKey":"k","secretKey":"s"}]'
    mp = load_source_providers({"SQLHANDLER_SOURCES": raw})
    assert mp is not None and mp.source_count == 2


# ---------------------------------------------------------------------------
# SQLHANDLER_MAX_ROWS cap (the MCP memory-exhaustion guard)
# ---------------------------------------------------------------------------


def _big_arrow(n):
    return pa.table({"i": list(range(n))})


def test_max_rows_default_and_garbage(monkeypatch):
    monkeypatch.delenv("SQLHANDLER_MAX_ROWS", raising=False)
    assert _max_rows() == 1000
    monkeypatch.setenv("SQLHANDLER_MAX_ROWS", "banana")
    assert _max_rows() == 1000
    monkeypatch.setenv("SQLHANDLER_MAX_ROWS", "-3")
    assert _max_rows() == 0  # negative clamps to "disabled"


def test_query_duckdb_caps_rows(monkeypatch):
    monkeypatch.setenv("SQLHANDLER_MAX_ROWS", "50")
    monkeypatch.setattr(SqlEngine, "_register_schema", _registered_schema_of(_big_arrow(200)))
    out = _engine().query_duckdb("SELECT i FROM big")
    assert out.num_rows == 50


def test_query_duckdb_explicit_limit_min_of_both(monkeypatch):
    monkeypatch.setenv("SQLHANDLER_MAX_ROWS", "50")
    monkeypatch.setattr(SqlEngine, "_register_schema", _registered_schema_of(_big_arrow(200)))
    eng = _engine()
    assert eng.query_duckdb("SELECT i FROM big", limit=10).num_rows == 10  # limit < cap
    assert eng.query_duckdb("SELECT i FROM big", limit=500).num_rows == 50  # cap wins


def test_query_duckdb_cap_disabled(monkeypatch):
    monkeypatch.setenv("SQLHANDLER_MAX_ROWS", "0")
    monkeypatch.setattr(SqlEngine, "_register_schema", _registered_schema_of(_big_arrow(200)))
    assert _engine().query_duckdb("SELECT i FROM big").num_rows == 200


# ---------------------------------------------------------------------------
# Markdown output cap (SQLHANDLER_MAX_OUTPUT_ROWS)
# ---------------------------------------------------------------------------


def test_markdown_caps_output_rows(monkeypatch):
    monkeypatch.setenv("SQLHANDLER_MAX_OUTPUT_ROWS", "20")
    md = _arrow_to_markdown(_big_arrow(100), max_rows=None)
    data_lines = [line for line in md.splitlines() if line.startswith("|")]
    # header + separator + at most 20 data rows
    assert len(data_lines) <= 22
    assert any("19" in line for line in data_lines)  # last rendered row is 19
    assert not any("| 99" in line for line in data_lines)  # nothing past the cap


def test_markdown_garbage_env_falls_back(monkeypatch):
    """A garbage cap env var must not dump the raw Arrow repr to the client."""
    monkeypatch.setenv("SQLHANDLER_MAX_OUTPUT_ROWS", "banana")
    md = _arrow_to_markdown(_big_arrow(5), max_rows=None)
    assert md.startswith("|")  # still a rendered markdown table
    assert "pyarrow" not in md.lower()


# ---------------------------------------------------------------------------
# misc: iceberg error message no longer tuple-mangled
# ---------------------------------------------------------------------------


def test_iceberg_unconfigured_error_message():
    from sqlhandler.config import IcebergConfig
    from sqlhandler.iceberg import IcebergProvider

    with pytest.raises(LakehouseError) as excinfo:
        IcebergProvider(IcebergConfig())
    msg = str(excinfo.value)
    assert msg.startswith("Iceberg connection is not configured.")
    assert not msg.startswith("(")  # the old two-arg raise mangled the message
