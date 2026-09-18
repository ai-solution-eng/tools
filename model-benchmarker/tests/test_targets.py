from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from model_benchmarker.endpoint_benchmarker.targets import (
    McpTarget,
    RestTarget,
    TargetError,
    parse_kv,
    pick_query_arg,
    pick_tool,
    redact_secrets,
)

# ---------------------------------------------------------------------------
# parse_kv / redact
# ---------------------------------------------------------------------------


def test_parse_kv_json_values():
    out = parse_kv(["top_k=10", "use_reranker=true", "note=hello world", 'modalities=["text","image"]'], "--arg")
    assert out == {"top_k": 10, "use_reranker": True, "note": "hello world", "modalities": ["text", "image"]}


def test_parse_kv_errors():
    with pytest.raises(TargetError):
        parse_kv(["novalue"], "--arg")
    with pytest.raises(TargetError):
        parse_kv(["=x"], "--arg")


def test_parse_kv_splits_on_first_equals():
    out = parse_kv(["Authorization=Bearer abc=def"], "--header")
    assert out == {"Authorization": "Bearer abc=def"}


def test_redact_secrets():
    data = {
        "headers": {"Authorization": "Bearer sk", "X-Dataset-Password": "pw", "Accept": "application/json"},
        "args": {"api_key": "sk", "query_arg": "query"},
        "nested": {"user_token": "t"},
        "url": "http://x",
        "empty_password": "",
    }
    out = redact_secrets(data)
    assert out["headers"]["Authorization"] == "REDACTED"
    assert out["headers"]["X-Dataset-Password"] == "REDACTED"
    assert out["headers"]["Accept"] == "application/json"
    assert out["args"]["api_key"] == "REDACTED"
    assert out["args"]["query_arg"] == "query"
    assert out["nested"]["user_token"] == "REDACTED"
    assert out["url"] == "http://x"
    assert out["empty_password"] == ""
    # original untouched
    assert data["headers"]["Authorization"] == "Bearer sk"


# ---------------------------------------------------------------------------
# RestTarget
# ---------------------------------------------------------------------------


def _rest(**kw) -> RestTarget:
    kw.setdefault("base_url", "http://localhost:8000/")
    return RestTarget(**kw)


def test_rest_validate_needs_dataset_placeholder():
    with pytest.raises(TargetError, match="dataset"):
        _rest(path_template="/api/datasets/{dataset}/search").validate()


def test_rest_validate_rejects_get_with_body():
    with pytest.raises(TargetError, match="GET"):
        _rest(dataset="d", body_template='{"q": "{query}"}').validate()


def test_rest_validate_bad_body_template():
    with pytest.raises(TargetError, match="not valid JSON"):
        _rest(method="POST", dataset="d", body_template='{"q": "{query}').validate()


def test_rest_build_request_get():
    t = _rest(dataset="ds-a", params={"top_k": 10, "use_reranker": False}, headers={"Accept": "application/json"})
    t.validate()
    url, params, body = t.build_request("what is RAG?")
    assert url == "http://localhost:8000/api/datasets/ds-a/search"
    assert params["q"] == "what is RAG?"
    assert params["top_k"] == 10
    assert body is None


def test_rest_build_request_post_body_escaping():
    t = _rest(
        method="POST",
        path_template="/api/v1/answer",
        query_param="",
        body_template='{"question": "{query}", "top_k": 5}',
        dataset=None,
    )
    t.validate()
    url, params, body = t.build_request('say "hi" \\ back\nline')
    assert url == "http://localhost:8000/api/v1/answer"
    assert params == {}
    parsed = json.loads(body)
    assert parsed["question"] == 'say "hi" \\ back\nline'  # survived JSON round-trip
    assert parsed["top_k"] == 5


# ---------------------------------------------------------------------------
# MCP tool selection / argument building
# ---------------------------------------------------------------------------


def _tool(name, props, required):
    return SimpleNamespace(name=name, input_schema={"type": "object", "properties": props, "required": required})


def test_pick_tool_requested_case_insensitive():
    tools = [_tool("search_dataset", {}, [])]
    assert pick_tool(tools, "SEARCH_DATASET").name == "search_dataset"


def test_pick_tool_not_found_lists_available():
    tools = [_tool("a", {}, []), _tool("b", {}, [])]
    with pytest.raises(TargetError, match="a, b"):
        pick_tool(tools, "zzz")


def test_pick_tool_auto_single():
    assert pick_tool([_tool("only", {}, [])], None).name == "only"


def test_pick_tool_auto_prefers_search_verbs():
    tools = [_tool("list_datasets", {}, []), _tool("search_dataset", {}, []), _tool("health", {}, [])]
    assert pick_tool(tools, None).name == "search_dataset"


def test_pick_tool_auto_ambiguous_errors():
    tools = [_tool("alpha", {}, []), _tool("beta", {}, [])]
    with pytest.raises(TargetError, match="--tool"):
        pick_tool(tools, None)


def test_pick_query_arg_prefers_named():
    tool = _tool("t", {"dataset_name": {"type": "string"}, "query": {"type": "string"}}, ["dataset_name", "query"])
    assert pick_query_arg(tool) == "query"


def test_pick_query_arg_fallback_first_required_string():
    tool = _tool("t", {"question": {"type": "string"}, "n": {"type": "integer"}}, ["question", "n"])
    assert pick_query_arg(tool) == "question"


def test_pick_query_arg_error_lists_properties():
    tool = _tool("t", {"a": {"type": "integer"}, "b": {"type": "number"}}, ["a"])
    with pytest.raises(TargetError, match="--query-arg"):
        pick_query_arg(tool)


def test_mcp_target_arg_building_covers_dataset():
    from model_benchmarker.endpoint_benchmarker.mcp_driver import _build_tool_args

    tool = _tool(
        "search_dataset",
        {"query": {"type": "string"}, "dataset_name": {"type": "string"}, "top_k": {"type": "integer", "default": 10}},
        ["query", "dataset_name"],
    )
    target = McpTarget(url="http://x", args={"top_k": 5}, dataset="ds-a")
    args = _build_tool_args(tool, target, query_arg="query")
    assert args == {"top_k": 5, "dataset_name": "ds-a"}


def test_mcp_target_arg_building_missing_required():
    from model_benchmarker.endpoint_benchmarker.mcp_driver import _build_tool_args

    tool = _tool("t", {"query": {"type": "string"}, "required_thing": {"type": "string"}}, ["query", "required_thing"])
    target = McpTarget(url="http://x")
    with pytest.raises(TargetError, match="required_thing"):
        _build_tool_args(tool, target, query_arg="query")


def test_mcp_target_arg_building_schema_default_satisfies():
    from model_benchmarker.endpoint_benchmarker.mcp_driver import _build_tool_args

    tool = _tool("t", {"query": {"type": "string"}, "opt": {"type": "string", "default": "x"}}, ["query", "opt"])
    args = _build_tool_args(tool, McpTarget(url="http://x"), query_arg="query")
    assert args == {}


def test_rest_describe_roundtrip():
    t = _rest(dataset="ds-a", headers={"Authorization": "Bearer x"})
    d = t.describe()
    assert d["kind"] == "rest"
    assert d["url"].endswith("/api/datasets/ds-a/search")
    assert d["headers"]["Authorization"] == "Bearer x"  # raw here; redaction happens in report
