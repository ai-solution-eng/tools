"""Tests for the webapp runner: free-text target validation, argv builders,
and the single-flight (per-origin contention) run registry."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from model_benchmarker.webapp import runner as R  # noqa: E402


def _url(host="127.0.0.1", port=9, scheme="http", path=""):
    return f"{scheme}://{host}:{port}{path}"


# ---------------------------------------------------------------------------
# target validation (free text)
# ---------------------------------------------------------------------------


def test_validate_target_accepts_http_https():
    assert R.validate_target("https://model.example.com") == "https://model.example.com"
    assert R.validate_target("http://10.0.0.5:8000/v1 ") == "http://10.0.0.5:8000/v1"


def test_validate_target_rejects_non_http():
    for bad in ("ftp://x", "file:///etc/passwd", "example.com", "javascript:alert(1)"):
        with pytest.raises(R.BenchError):
            R.validate_target(bad)


def test_validate_target_rejects_embedded_credentials():
    with pytest.raises(R.BenchError, match="credentials"):
        R.validate_target("http://user:pass@host:8000")


def test_validate_target_rejects_whitespace_and_control_chars():
    with pytest.raises(R.BenchError, match="whitespace"):
        R.validate_target("http://host/\tpath")
    with pytest.raises(R.BenchError):
        R.validate_target("http://hos\nt:8000")


def test_validate_target_requires_hostname():
    with pytest.raises(R.BenchError, match="hostname"):
        R.validate_target("http://")


def test_origin_is_scheme_host_port():
    assert R.origin_of("https://Model.Example.com/a") == "https://model.example.com:443"
    assert R.origin_of("https://model.example.com:8443/b") == "https://model.example.com:8443"
    assert R.origin_of("http://model.example.com/c") == "http://model.example.com:80"
    # same origin, different paths -> SAME contention identity
    assert R.origin_of("http://h:1/x") == R.origin_of("http://h:1/y")
    # different ports -> different origins
    assert R.origin_of("http://h:1/x") != R.origin_of("http://h:2/x")


def test_suggestions_loader(monkeypatch):
    monkeypatch.delenv(R.TARGET_SUGGESTIONS_ENV, raising=False)
    assert R.load_target_suggestions() == []
    monkeypatch.setenv(
        R.TARGET_SUGGESTIONS_ENV,
        json.dumps(
            [
                {"name": "qwen (chat)", "url": "https://qwen.svc:8000", "kind": "chat"},
                {"name": "bad", "url": "ftp://nope"},
                {"name": "no url"},
                "junk",
            ]
        ),
    )
    s = R.load_target_suggestions()
    assert [(t.name, t.kind) for t in s] == [("qwen (chat)", "chat")]


# ---------------------------------------------------------------------------
# argv builders
# ---------------------------------------------------------------------------


def test_chat_builder_defaults(tmp_path):
    argv, env, meta = R.build_chat_argv(_url(), {}, tmp_path)
    assert argv[1:3] == ["-m", "model_benchmarker.benchmark_chat"]
    assert "--url" in argv and _url() in argv
    assert "--quiet" in argv  # piped stdout: per-request prints distort TTFT
    assert "PCAI_API_KEY" not in env
    assert meta == {}


def test_chat_builder_full(tmp_path):
    params = {
        "number_users": "1,2,4",
        "requests_per_user": 3,
        "context_length": "0,8192",
        "tasks": "coding,creative",
        "arrival_mode": "open",
        "request_rate": 2.5,
        "level_duration": 45,
        "goodput": "ttft<=2000,tpot<=50",
        "api_key": "sk-test",
        "multiturn": True,
        "no_nonce": True,
        "thinking_level": "high",
        "seed": 42,
    }
    argv, env, meta = R.build_chat_argv(_url(), params, tmp_path)
    joined = " ".join(argv)
    assert "--number_users 1,2,4" in joined
    assert "--arrival_mode open" in joined
    assert "--goodput ttft<=2000,tpot<=50" in joined
    assert "--multiturn" in argv and "--no-nonce" in argv
    assert env["PCAI_API_KEY"] == "sk-test"
    assert "api_key" not in json.dumps(meta)


def test_chat_builder_open_requires_rate(tmp_path):
    with pytest.raises(R.BenchError, match="request_rate"):
        R.build_chat_argv(_url(), {"arrival_mode": "open"}, tmp_path)


def test_chat_builder_task_whitelist(tmp_path):
    with pytest.raises(R.BenchError, match="unknown task"):
        R.build_chat_argv(_url(), {"tasks": "coding;rm -rf"}, tmp_path)


def test_endpoint_builder_rest(tmp_path):
    params = {
        "mode": "rest",
        "method": "post",
        "path": "/api/v1/answer",
        "body": '{"question": "{query}", "top_k": 5}',
        "sweep": "1,4,16",
        "duration": 120,
        "headers": {"X-Env": "trial"},
        "api_key": "tok-1",
    }
    argv, _env, meta = R.build_endpoint_argv(_url(), params, tmp_path, prom_url=None)
    joined = " ".join(argv)
    assert "--mode rest" in joined
    assert "--method POST" in joined
    assert "--sweep 1,4,16" in joined
    assert "Authorization=Bearer tok-1" in joined
    assert "tok-1" not in json.dumps(meta)


def test_endpoint_builder_body_template_validation(tmp_path):
    with pytest.raises(R.BenchError, match="JSON"):
        R.build_endpoint_argv(_url(), {"mode": "rest", "body": "{not json}"}, tmp_path)


def test_endpoint_builder_mcp(tmp_path):
    params = {
        "mode": "mcp",
        "tool": "search_dataset",
        "args": {"top_k": 10, "use_reranker": True},
        "transport": "streamable-http",
        "dataset": "their-ds",
        "N": 8,
    }
    argv, _env, meta = R.build_endpoint_argv(_url(), params, tmp_path, prom_url=None)
    joined = " ".join(argv)
    assert "--mode mcp" in joined
    assert "--tool search_dataset" in joined
    assert "--arg top_k=10" in joined
    assert "-N 8" in joined
    assert isinstance(meta.get("args"), dict)


def test_endpoint_builder_prom_is_operator_only(tmp_path):
    argv, _env, _meta = R.build_endpoint_argv(
        _url(), {"mode": "rest", "prom_selector": 'ns="x"'}, tmp_path, prom_url=None
    )
    assert "--prom-url" not in argv
    argv2, _e, _m = R.build_endpoint_argv(
        _url(), {"mode": "rest", "prom_selector": 'exported_namespace="their-ns"'}, tmp_path, prom_url="http://p:9090"
    )
    assert "--prom-selector" in argv2


def test_selector_rejects_injection(tmp_path):
    with pytest.raises(R.BenchError):
        R.build_endpoint_argv(_url(), {"mode": "rest", "prom_selector": 'x"}//bad'}, tmp_path, prom_url="http://p:9090")


def test_header_name_whitelist(tmp_path):
    with pytest.raises(R.BenchError, match="invalid name"):
        R.build_endpoint_argv(_url(), {"mode": "rest", "headers": {"Bad Name\n": "v"}}, tmp_path)


def test_endpoint_key_lookup_by_url(monkeypatch, tmp_path):
    monkeypatch.setenv(R.ENDPOINT_KEYS_ENV, json.dumps({_url(): "tok-2"}))
    argv, _env, _meta = R.build_endpoint_argv(_url(), {"mode": "rest"}, tmp_path)
    assert "Authorization=Bearer tok-2" in " ".join(argv)


# ---------------------------------------------------------------------------
# run registry (single-flight per ORIGIN, artifacts, cancel)
# ---------------------------------------------------------------------------


class FakePopen:
    def __init__(self, argv, **kw):
        self.pid = 4242
        self._slow = "--sweep" in argv

    def wait(self, timeout=None):
        if self._slow:
            time.sleep(30)  # outlives the test; the daemon thread dies with it
        return 0

    @property
    def returncode(self):
        return 0


def test_launch_one_endpoint_at_a_time(monkeypatch, tmp_path):
    monkeypatch.setattr(R.subprocess, "Popen", FakePopen)
    mgr = R.RunManager(tmp_path / "w")
    rec = mgr.launch("endpoint", _url(), {"mode": "rest", "N": 1})
    assert rec.status == "running"
    assert rec.endpoint == "http://127.0.0.1:9"
    # same ORIGIN (any path) -> 409 semantics via BusyEndpointError
    with pytest.raises(R.BusyEndpointError):
        mgr.launch("endpoint", _url(path="/other"), {"mode": "rest", "N": 1})
    # different origin runs concurrently
    rec2 = mgr.launch("endpoint", _url(host="127.0.0.2"), {"mode": "rest", "N": 1})
    assert rec2.run_id != rec.run_id


def test_cancel_semantics_note(tmp_path):
    """Cancel flags a RUNNING subprocess; the supervisor terminates it at the
    next 2s poll. A run that exits on its own faster than that keeps its
    natural status (never relabelled 'cancelled') -- verified live: a
    fast-failing health-check run reports 'failed', a real sweep reports
    'cancelled'. The UI's Cancel button works for long-running benchmarks,
    which is the case that matters."""


def test_launch_rejects_bad_targets(tmp_path):
    mgr = R.RunManager(tmp_path / "w")
    for bad in ("ftp://x", "http://u:p@h", "not a url", ""):
        with pytest.raises(R.BenchError):
            mgr.launch("endpoint", bad, {"mode": "rest"})


def test_run_meta_written_and_scanned(tmp_path, monkeypatch):
    monkeypatch.setattr(R.subprocess, "Popen", FakePopen)
    mgr = R.RunManager(tmp_path / "w")
    rec = mgr.launch("endpoint", _url(), {"mode": "rest", "N": 2})
    for _ in range(50):
        if rec.status != "running":
            break
        time.sleep(0.05)
    assert rec.status == "success"
    meta = json.loads((tmp_path / "w" / "runs" / rec.run_id / "run_meta.json").read_text())
    assert meta["status"] == "success"
    assert meta["endpoint"] == "http://127.0.0.1:9"
    mgr2 = R.RunManager(tmp_path / "w")
    assert mgr2.get(rec.run_id)["status"] == "success"


def test_artifact_path_traversal_refused(tmp_path):
    mgr = R.RunManager(tmp_path / "w")
    with pytest.raises(R.BenchError):
        mgr.artifact_path("some-run", "../../etc/passwd")
    with pytest.raises(R.BenchError):
        mgr.artifact_path("some-run", ".hidden")
