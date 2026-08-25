"""Regression tests for the OneLake token lifecycle (refresh, retry, 401)."""

import json
import time
import urllib.error
from unittest import mock

from sqlhandler.config import FabricConfig
from sqlhandler.onelake import OneLakeProvider

_CFG = FabricConfig(
    tenant_id="t",
    client_id="c",
    client_secret="s",
    lakehouse_abfss_url="abfss://ws@onelake.dfs.fabric.microsoft.com/lh",
)


class _FakeResp:
    """A urlopen() response that is also a context manager."""

    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _provider():
    return OneLakeProvider(_CFG)


def test_retries_transient_error_then_succeeds():
    p = _provider()
    calls = {"n": 0}

    def flaky(req, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise urllib.error.URLError("network blip")
        return _FakeResp({"access_token": "tok-1", "expires_in": 3600})

    with mock.patch("urllib.request.urlopen", side_effect=flaky):
        assert p._dfs_access_token() == "tok-1"
    assert calls["n"] == 2


def test_cached_token_reused_until_expiry():
    p = _provider()
    with mock.patch(
        "urllib.request.urlopen",
        side_effect=lambda req, timeout=None: _FakeResp({"access_token": "tok-1", "expires_in": 3600}),
    ):
        assert p._dfs_access_token() == "tok-1"
        assert p._dfs_access_token() == "tok-1"
    p._token_expires_at = 0
    with mock.patch(
        "urllib.request.urlopen",
        side_effect=lambda req, timeout=None: _FakeResp({"access_token": "tok-2", "expires_in": 3600}),
    ):
        assert p._dfs_access_token() == "tok-2"


def test_deterministic_401_not_retried():
    p = _provider()
    p._token = None
    p._token_expires_at = 0

    def bad401(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 401, "unauthorized", {}, None)

    with mock.patch("urllib.request.urlopen", side_effect=bad401):
        try:
            p._dfs_access_token()
            raise AssertionError("expected a LakehouseError")
        except Exception as exc:
            assert "OneLake token acquisition failed" in str(exc)


def test_429_retries_then_falls_back_to_cached_token():
    p = _provider()
    p._token = "old-token"
    p._token_expires_at = time.time() + 600

    def throttled(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 429, "throttled", {}, None)

    with mock.patch("urllib.request.urlopen", side_effect=throttled):
        assert p._dfs_access_token() == "old-token"


def test_dfs_list_refreshes_once_on_401():
    p = _provider()
    p._token = "stale"
    p._token_expires_at = time.time() + 3600

    def dfs(req, timeout=None):
        if req.full_url.startswith("https://login.microsoftonline.com"):
            return _FakeResp({"access_token": "fresh", "expires_in": 3600})
        raise urllib.error.HTTPError(req.full_url, 401, "unauthorized", {}, None)

    with mock.patch("urllib.request.urlopen", side_effect=dfs):
        try:
            p._dfs_list("lh/Tables")
            raise AssertionError("expected a LakehouseError after refresh")
        except Exception as exc:
            assert "HTTP 401" in str(exc)
    assert p._token == "fresh"


def test_onelake_check_version_parses_delta_log():
    from sqlhandler.provider import TableInfo

    p = _provider()
    delta = TableInfo(name="t", schema="s", format="delta")
    entries = [
        {"name": "00000000000000000000.json"},
        {"name": "00000000000000000003.json"},
        {"name": "_last_checkpoint"},
    ]
    with mock.patch.object(p, "_dfs_list", return_value=entries):
        assert p.check_version(delta) == 3
    assert p.check_version(TableInfo(name="t", schema="s", format="parquet")) is None
    with mock.patch.object(p, "_dfs_list", side_effect=Exception("boom")):
        assert p.check_version(delta) is None
