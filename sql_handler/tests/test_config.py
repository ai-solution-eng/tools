"""Unit tests for SQLhandler config + backend selection (no network)."""

import os

import pytest

from sqlhandler.config import (
    FabricConfig,
    IcebergConfig,
    S3Config,
    load_backend_config,
    load_cache_config,
    load_config,
    load_iceberg_config,
    load_s3_config,
)
from sqlhandler.provider import LakehouseError, make_provider


def test_config_from_env():
    env = {
        "FABRIC_TENANT_ID": "t",
        "FABRIC_CLIENT_ID": "c",
        "FABRIC_CLIENT_SECRET": "s",
        "FABRIC_WORKSPACE_ID": "w",
        "FABRIC_LAKEHOUSE_ID": "l",
    }
    cfg = load_config(env)
    assert cfg.is_configured
    assert cfg.abfss_tables_url == ("abfss://w@onelake.dfs.fabric.microsoft.com/l/Tables")


def test_config_full_url_wins():
    env = {
        "FABRIC_TENANT_ID": "t",
        "FABRIC_CLIENT_ID": "c",
        "FABRIC_CLIENT_SECRET": "s",
        "FABRIC_LAKEHOUSE_ABFSS_URL": "abfss://ws@onelake.dfs.fabric.microsoft.com/lh",
        "FABRIC_WORKSPACE_ID": "ignored",
        "FABRIC_LAKEHOUSE_ID": "ignored",
    }
    cfg = load_config(env)
    assert cfg.abfss_tables_url.endswith("/Tables")


def test_unconfigured_onelake_raises():
    with pytest.raises(LakehouseError):
        make_provider(FabricConfig())


def test_env_not_polluted_by_dotenv(tmp_path):
    # load_dotenv should not clobber existing variables.
    os.environ["SQLHANDLER_ENV_FILE"] = str(tmp_path / "missing.env")
    from sqlhandler.config import load_dotenv

    load_dotenv()  # no crash when file absent


def test_cache_config_defaults():
    cc = load_cache_config({})
    assert cc.ttl_seconds == 3600
    assert cc.prewarm_tables == ()
    assert cc.enabled


def test_cache_config_from_env():
    cc = load_cache_config(
        {
            "SQLHANDLER_CACHE_TTL": "120",
            "SQLHANDLER_PREWARM_TABLES": " work_order_header, work_order_note_recent ,",
        }
    )
    assert cc.ttl_seconds == 120
    assert cc.prewarm_tables == ("work_order_header", "work_order_note_recent")


def test_cache_config_disabled():
    assert not load_cache_config({"SQLHANDLER_CACHE_TTL": "0"}).enabled


def test_cache_config_invalid_ttl_falls_back():
    assert load_cache_config({"SQLHANDLER_CACHE_TTL": "abc"}).ttl_seconds == 3600


# ---------------------------------------------------------------------------
# S3 / backend selection
# ---------------------------------------------------------------------------


def _s3env(**over):
    base = {
        "S3_ENDPOINT_URL": "http://127.0.0.1:9000",
        "S3_ACCESS_KEY": "minioadmin",
        "S3_SECRET_KEY": "minioadmin",
        "S3_BUCKET": "lakehouse",
        "S3_PREFIX": "datasets",
    }
    base.update(over)
    return base


def test_s3_config_from_env():
    cfg = load_s3_config(_s3env())
    assert cfg.is_configured
    assert cfg.endpoint_url == "http://127.0.0.1:9000"
    assert cfg.bucket == "lakehouse"
    assert cfg.prefix == "datasets"
    assert cfg.region == "us-east-1"


def test_s3_config_anonymous_public_bucket():
    cfg = load_s3_config({"S3_ANONYMOUS": "1", "S3_BUCKET": "public"})
    assert cfg.is_configured


def test_s3_config_missing_bucket():
    assert not load_s3_config({"S3_ACCESS_KEY": "a", "S3_SECRET_KEY": "s"}).is_configured


def test_s3_config_missing_creds():
    assert not load_s3_config({"S3_BUCKET": "b"}).is_configured


def test_s3_provider_from_make_provider():
    p = make_provider(load_s3_config(_s3env()))
    assert p.kind == "s3"


def test_backend_defaults_to_onelake():
    backend, cfg = load_backend_config({})
    assert backend == "onelake"
    assert isinstance(cfg, FabricConfig)


def test_backend_s3_selected():
    env = dict(_s3env())
    env["SQLHANDLER_BACKEND"] = "s3"
    backend, cfg = load_backend_config(env)
    assert backend == "s3"
    assert isinstance(cfg, S3Config)


def test_backend_minio_alias():
    env = dict(_s3env())
    env["SQLHANDLER_BACKEND"] = "minio"
    assert load_backend_config(env)[0] == "s3"


def test_backend_parquet_alias():
    env = dict(_s3env())
    env["SQLHANDLER_BACKEND"] = "parquet"
    assert load_backend_config(env)[0] == "s3"


# ---------------------------------------------------------------------------
# Iceberg backend
# ---------------------------------------------------------------------------


def _iceenv(**over):
    base = {
        "SQLHANDLER_BACKEND": "iceberg",
        "ICEBERG_CATALOG_TYPE": "rest",
        "ICEBERG_CATALOG_URI": "http://rest-catalog:8181",
        "ICEBERG_CATALOG_TOKEN": "tok",
        "ICEBERG_NAMESPACE": "analytics",
        "S3_ENDPOINT_URL": "http://127.0.0.1:9000",
        "S3_ACCESS_KEY": "minioadmin",
        "S3_SECRET_KEY": "minioadmin",
    }
    base.update(over)
    return base


def test_iceberg_config_from_env():
    cfg = load_iceberg_config(_iceenv())
    assert cfg.catalog_type == "rest"
    assert cfg.catalog_uri == "http://rest-catalog:8181"
    assert cfg.catalog_token == "tok"
    assert cfg.namespace == "analytics"
    assert cfg.is_configured


def test_iceberg_sql_catalog():
    cfg = load_iceberg_config({"ICEBERG_CATALOG_TYPE": "sql", "ICEBERG_CATALOG_URI": "sqlite:///tmp/x.db"})
    assert cfg.catalog_type == "sql"
    assert cfg.is_configured


def test_iceberg_unconfigured():
    assert not load_iceberg_config({}).is_configured


def test_backend_iceberg_selected():
    env = _iceenv()
    backend, cfg = load_backend_config(env)
    assert backend == "iceberg"
    assert isinstance(cfg, IcebergConfig)
