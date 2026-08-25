"""Integration test against a real S3/MinIO endpoint (optional).

Skip it by default; run with a live MinIO (or any S3) server:

    export SQLHANDLER_TEST_MINIO=1
    export SQLHANDLER_TEST_S3_ENDPOINT=http://127.0.0.1:9000
    export SQLHANDLER_TEST_S3_ACCESS_KEY=minioadmin
    export SQLHANDLER_TEST_S3_SECRET_KEY=minioadmin
    pytest tests/test_s3_integration.py -q
"""

import os
import uuid

import pyarrow as pa
import pyarrow.fs as pafs
import pyarrow.parquet as pq
import pytest

from sqlhandler.config import S3Config
from sqlhandler.engine import SqlEngine
from sqlhandler.s3 import S3Provider


def _minio_available() -> bool:
    if not os.environ.get("SQLHANDLER_TEST_MINIO"):
        return False
    ep = os.environ.get("SQLHANDLER_TEST_S3_ENDPOINT", "http://127.0.0.1:9000")
    ak = os.environ.get("SQLHANDLER_TEST_S3_ACCESS_KEY", "minioadmin")
    sk = os.environ.get("SQLHANDLER_TEST_S3_SECRET_KEY", "minioadmin")
    try:
        fs = pafs.S3FileSystem(
            access_key=ak,
            secret_key=sk,
            region="us-east-1",
            endpoint_override=ep,
        )
        fs.get_file_info("x" + str(uuid.uuid4()))
        return True
    except Exception:
        return False


def _cfg(prefix=None):
    return S3Config(
        endpoint_url=os.environ.get("SQLHANDLER_TEST_S3_ENDPOINT", "http://127.0.0.1:9000"),
        access_key=os.environ.get("SQLHANDLER_TEST_S3_ACCESS_KEY", "minioadmin"),
        secret_key=os.environ.get("SQLHANDLER_TEST_S3_SECRET_KEY", "minioadmin"),
        bucket=os.environ.get("SQLHANDLER_TEST_S3_BUCKET", "sqlhandler-test-int"),
        prefix=prefix,
    )


def _upload(fs, bucket, rel, table):
    buf = pa.BufferOutputStream()
    pq.write_table(table, buf)
    out = fs.open_output_stream(f"{bucket}/{rel}")
    out.write(buf.getvalue().to_pybytes())
    out.close()


def test_minio_end_to_end():
    if not _minio_available():
        pytest.skip("Set SQLHANDLER_TEST_MINIO=1 + endpoint to run")

    bucket = os.environ.get("SQLHANDLER_TEST_S3_BUCKET", "sqlhandler-test-int")
    fs = pafs.S3FileSystem(
        access_key=os.environ.get("SQLHANDLER_TEST_S3_ACCESS_KEY", "minioadmin"),
        secret_key=os.environ.get("SQLHANDLER_TEST_S3_SECRET_KEY", "minioadmin"),
        region="us-east-1",
        endpoint_override=os.environ.get("SQLHANDLER_TEST_S3_ENDPOINT", "http://127.0.0.1:9000"),
        allow_bucket_creation=True,
    )
    try:
        fs.create_dir(bucket)
    except Exception:
        pass

    unique = f"t{uuid.uuid4().hex[:8]}"
    _upload(
        fs,
        bucket,
        f"{unique}/orders.parquet",
        pa.table({"order_id": [1, 2], "amount": [10.0, 20.0]}),
    )
    _upload(
        fs,
        bucket,
        f"{unique}/sales/customers/part-0.parquet",
        pa.table({"cust_id": [1, 2], "name": ["a", "b"]}),
    )
    _upload(fs, bucket, f"{unique}/hr/employees/year=2024/part.parquet", pa.table({"emp_id": [7, 8]}))

    eng = SqlEngine(S3Provider(_cfg(prefix=unique)))
    names = sorted(t.path for t in eng.list_tables())
    assert names == ["hr/employees", "orders", "sales/customers"]

    d = eng.describe_table("customers")
    assert {c["name"] for c in d["columns"]} == {"cust_id", "name"}

    arrow = eng.query_duckdb("SELECT count(*) AS c FROM sales_customers")
    assert arrow.to_pydict() == {"c": [2]}

    scan = eng.scan_arrow("orders", columns=["order_id"], limit=1)
    assert scan.num_rows == 1

    # cleanup: drop the unique prefix objects
    for fi in fs.get_file_info(pafs.FileSelector(f"{bucket}/{unique}", recursive=True)):
        if fi.type == pafs.FileType.File:
            fs.delete_file(fi.path)
