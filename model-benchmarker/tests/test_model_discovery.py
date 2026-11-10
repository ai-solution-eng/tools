"""Tests for the endpoint model-list probe shared by auto-discovery (the bench
engines) and the webapp's chat-page model dropdown (POST /api/models)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from model_benchmarker.utils import pcai_model_classes as P  # noqa: E402


@pytest.fixture(autouse=True)
def _clear_discovery_caches():
    P._discovered_model_names.clear()
    P._discovery_failures.clear()
    yield
    P._discovered_model_names.clear()
    P._discovery_failures.clear()


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def _clients(monkeypatch, response=None, error=None):
    """Patch both discovery clients; returns which one each call used."""
    calls = {"local": [], "remote": []}

    def make(kind):
        class Client:
            def get(self, url, headers=None, timeout=None):
                calls[kind].append({"url": url, "headers": headers, "timeout": timeout})
                if error is not None:
                    raise error
                return _Resp(response)

        return Client()

    monkeypatch.setattr(P, "_shared_remote_http_client", lambda: make("remote"))
    monkeypatch.setattr(P, "_SHARED_HTTP_CLIENT", make("local"))
    return calls


def test_fetch_model_ids_lists_every_id_and_sends_the_key(monkeypatch):
    calls = _clients(
        monkeypatch,
        {"data": [{"id": "qwen3-32b"}, {"id": ""}, {"no_id": 1}, "junk", {"id": "Qwen/Qwen3-8B"}]},
    )
    ids, error = P._fetch_model_ids("http://h:8000/v1", "sk-1", remote=True, timeout=3.5)
    assert ids == ["qwen3-32b", "Qwen/Qwen3-8B"]
    assert error == ""
    assert calls["remote"] == [
        {"url": "http://h:8000/v1/models", "headers": {"Authorization": "Bearer sk-1"}, "timeout": 3.5}
    ]
    assert calls["local"] == []


def test_fetch_model_ids_local_uses_the_plain_client(monkeypatch):
    calls = _clients(monkeypatch, {"data": [{"id": "m"}]})
    ids, error = P._fetch_model_ids("http://h:8000/v1", "", remote=False)
    assert (ids, error) == (["m"], "")
    assert calls["local"] and not calls["remote"]
    assert calls["local"][0]["headers"] == {}  # no key -> no Authorization header


def test_fetch_model_ids_fails_soft_on_transport_error(monkeypatch):
    _clients(monkeypatch, error=RuntimeError("connection refused"))
    ids, error = P._fetch_model_ids("http://h:8000/v1")
    assert ids == []
    assert "RuntimeError" in error and "connection refused" in error


def test_fetch_model_ids_reports_an_empty_list_without_an_error(monkeypatch):
    # reachable-but-empty is NOT a transport failure: the dropdown says "no
    # models advertised" rather than "could not list models"
    _clients(monkeypatch, {"data": []})
    assert P._fetch_model_ids("http://h:8000/v1") == ([], "")


def test_discover_model_name_returns_first_id_and_caches(monkeypatch):
    calls = _clients(monkeypatch, {"data": [{"id": "first"}, {"id": "second"}]})
    assert P.discover_model_name("http://h:8000/v1") == "first"
    assert len(calls["remote"]) == 1
    # a second construction against the same endpoint is served from the cache
    _clients(monkeypatch, error=RuntimeError("must not be called"))
    assert P.discover_model_name("http://h:8000/v1") == "first"


def test_discover_model_name_failure_is_negatively_cached(monkeypatch):
    _clients(monkeypatch, error=RuntimeError("down"))
    assert P.discover_model_name("http://h:8000/v1") == ""
    # the negative TTL suppresses a re-probe even after the endpoint recovers
    calls = _clients(monkeypatch, {"data": [{"id": "late"}]})
    assert P.discover_model_name("http://h:8000/v1") == ""
    assert calls["remote"] == []


def test_discover_model_name_empty_list_is_negatively_cached(monkeypatch):
    calls = _clients(monkeypatch, {"data": []})
    assert P.discover_model_name("http://h:8000/v1") == ""
    assert len(calls["remote"]) == 1
    calls2 = _clients(monkeypatch, {"data": [{"id": "late"}]})
    assert P.discover_model_name("http://h:8000/v1") == ""
    assert calls2["remote"] == []  # the empty-list probe is in the negative cache


def test_base_model_local_rewrite_only_touches_serving_urls():
    """BaseModel.url_local is used for every default (model_usage=local) run:
    a public https endpoint must keep https, only .serving. URLs are rewritten."""
    conv = P.BaseModel._convert_remote_url_to_local
    assert conv("https://m.serving.pcai.example.net") == "http://m.svc.cluster.local"
    assert conv("https://api.example.com/v1") == "https://api.example.com/v1"


def test_discover_model_ids_is_uncached_for_the_dropdown(monkeypatch):
    calls = _clients(monkeypatch, {"data": [{"id": "a"}, {"id": "b"}]})
    assert P.discover_model_ids("http://h:8000/v1") == (["a", "b"], "")
    assert P.discover_model_ids("http://h:8000/v1") == (["a", "b"], "")
    assert len(calls["remote"]) == 2  # the dropdown always shows the live list
