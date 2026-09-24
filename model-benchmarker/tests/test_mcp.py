"""End-to-end MCP tests against a REAL MCP server (MCPServer under uvicorn).

The headline: every test here uses ONLY the MCP endpoint URL — no companion
REST URL — which is exactly the fix over the original benchmark script.
"""

from __future__ import annotations

import asyncio

import pytest

from model_benchmarker.endpoint_benchmarker.cli import main
from model_benchmarker.endpoint_benchmarker.mcp_driver import resolve_mcp_target, run_user_mcp
from model_benchmarker.endpoint_benchmarker.queries import load_queries
from model_benchmarker.endpoint_benchmarker.stats import BenchmarkStats
from model_benchmarker.endpoint_benchmarker.sweep import run_sweep
from model_benchmarker.endpoint_benchmarker.targets import McpTarget, TargetError


@pytest.fixture()
def queries():
    return load_queries(None, "generic")[0]


def test_resolve_auto_picks_search_tool_and_query_arg(mcp_server_url):
    target = McpTarget(url=mcp_server_url, dataset="ds-a", args={"top_k": 5})
    resolved = asyncio.run(resolve_mcp_target(target))
    assert resolved is not None
    r = resolved[0]
    assert r.tool_name == "search_dataset"  # preferred over list_datasets/boom
    assert r.query_arg == "query"
    assert r.args["dataset_name"] == "ds-a"  # --dataset filled the dataset-ish arg
    assert r.args["top_k"] == 5


def test_list_tools_action(mcp_server_url, capsys):
    target = McpTarget(url=mcp_server_url)
    out = asyncio.run(resolve_mcp_target(target, list_only=True))
    assert out is None
    captured = capsys.readouterr().out
    assert "search_dataset" in captured
    assert "query -> 'query'" in captured
    assert "required: query, dataset_name" in captured


def test_resolve_unknown_tool_errors(mcp_server_url):
    target = McpTarget(url=mcp_server_url, tool_name="nope")
    with pytest.raises(TargetError, match="nope"):
        asyncio.run(resolve_mcp_target(target))


def test_mcp_run_users_single_url(mcp_server_url, queries):
    """The full load loop against a real MCP server with just the MCP URL."""
    target = McpTarget(url=mcp_server_url, dataset="ds-a", args={"top_k": 2})
    resolved = asyncio.run(resolve_mcp_target(target))[0]

    async def _run():
        return await run_sweep(
            [2],
            duration=1.0,
            ramp_up=0.0,
            warmup_rounds=1,  # exercises the MCP warm-up path (N throwaway sessions)
            settle=0.0,
            call_timeout=10.0,
            seed=3,
            progress_interval=0.5,
            queries=queries,
            mcp_target=target,
            resolved=resolved,
        )

    levels = asyncio.run(_run())
    level_stats = levels[0].stats
    s = level_stats.summary()
    assert s["successful"] > 0
    assert s["failed"] == 0
    assert s["success_rate_pct"] == 100.0
    assert s["avg_results_per_query"] >= 1


def test_mcp_error_tool_counted_as_failure(mcp_server_url, queries):
    target = McpTarget(url=mcp_server_url, tool_name="boom")
    resolved = asyncio.run(resolve_mcp_target(target))[0]
    assert resolved.tool_name == "boom"

    async def _run():
        stats = BenchmarkStats()
        await run_user_mcp(
            user_id=0,
            target=target,
            resolved=resolved,
            queries=queries,
            duration=0.6,
            ramp_delay=0.0,
            stats=stats,
            call_timeout=10.0,
        )
        return stats

    stats = asyncio.run(_run())
    s = stats.summary()
    assert s["failed"] == s["total_requests"] > 0
    assert any("MCP error" in k or "kapow" in k for k in s["errors"])


def test_mcp_cli_full_run_no_second_url(mcp_server_url, tmp_path):
    """THE fix under test: MCP mode driven end-to-end with a single URL."""
    out = tmp_path / "mcp.json"
    rc = main(
        [
            "--mode",
            "mcp",
            "--url",
            mcp_server_url,
            "--dataset",
            "ds-a",
            "-N",
            "2",
            "--duration",
            "1.0",
            "--warmup-rounds",
            "0",
            "--ramp-up",
            "0",
            "--progress-interval",
            "1",
            "--output",
            str(out),
            "--quiet",
        ]
    )
    assert rc == 0
    payload = __import__("json").loads(out.read_text())
    assert payload["config"]["target"]["kind"] == "mcp"
    assert payload["config"]["target"]["tool"] == "search_dataset"
    assert payload["levels"][0]["client_stats"]["successful"] > 0
    assert "api_url" not in payload["config"]["target"]


def test_mcp_cli_list_tools_flag(mcp_server_url, capsys):
    rc = main(["--mode", "mcp", "--url", mcp_server_url, "--list-tools", "--quiet"])
    assert rc == 0
    assert "list_datasets" in capsys.readouterr().out


def test_mcp_unreachable_server_fails_cleanly(tmp_path, capsys):
    # port 1 on localhost is closed in practice
    rc = main(
        [
            "--mode",
            "mcp",
            "--url",
            "http://127.0.0.1:1/mcp",
            "-N",
            "1",
            "--duration",
            "0.5",
            "--quiet",
        ]
    )
    assert rc in (1, 2)
    err = capsys.readouterr().err
    assert "Error" in err or rc == 1
