"""The tool-span attribute contract (core/tool_span.py).

WHY: the relay executor and the /mcp tools/call span are two different code
paths that must answer the same questions in Langfuse — who, which server/tool,
which turn, which ledger row. They used to assemble that by hand and had
already drifted, so the contract is now one function and these tests pin it:
the canonical keys are present, unknown facts are OMITTED (an absent user.id
means "not attributable", never "unknown user"), and arguments are reduced to a
digest so no tool payload can reach the tracing backend.
"""

from pcai_llm.core import tool_span

CANONICAL = {"langfuse.observation.type", "langfuse.trace.name", "mcp.tool", "tool.status"}


def test_relay_style_span_carries_the_full_contract():
    attrs = tool_span.tool_span_attrs(
        server="k8s-ops",
        tool="list_namespaces",
        status="ok",
        ms=112,
        user_id="61fcf05f-285d-4903-9bd0-3812f71bead3",
        key_name="bryan-admin",
        session_id="1766aa7f16e859e2",
        request_id="chat-abc",
        iteration=3,
        args={"namespace": "default"},
    )
    assert CANONICAL <= attrs.keys()
    assert attrs["mcp.server"] == "k8s-ops"
    assert attrs["mcp.tool"] == "list_namespaces"
    assert attrs["duration_ms"] == 112
    assert attrs["user.id"] == "61fcf05f-285d-4903-9bd0-3812f71bead3"
    assert attrs["pcai.key"] == "bryan-admin"
    assert attrs["langfuse.session.id"] == "1766aa7f16e859e2"
    assert attrs["pcai.request_id"] == "chat-abc"
    assert attrs["pcai.iteration"] == 3
    # the requested name equals the resolved name here, so it is NOT repeated
    assert "mcp.tool.requested" not in attrs
    # no handback on an executed call
    assert "pcai.handback.reason" not in attrs


def test_mcp_style_span_records_requested_vs_resolved():
    attrs = tool_span.tool_span_attrs(
        server="ezapp-deploy",
        tool="get_ezappconfig",
        requested="mcp_call",
        surface="mcp",
        status="ok",
        ms=537,
        user_id="61fcf05f-285d-4903-9bd0-3812f71bead3",
        key_name="bryan-admin",
        request_id="mcp-74965187",
    )
    assert attrs["mcp.tool"] == "get_ezappconfig"
    assert attrs["mcp.tool.requested"] == "mcp_call"
    assert attrs["mcp.surface"] == "mcp"
    assert attrs["pcai.request_id"] == "mcp-74965187"
    # /mcp has no conversation context: these must be absent, not empty strings
    assert "langfuse.session.id" not in attrs
    assert "pcai.iteration" not in attrs


def test_unknown_facts_are_omitted_never_faked():
    attrs = tool_span.tool_span_attrs(tool="run_code", status="handed_back")
    assert CANONICAL <= attrs.keys()
    for absent in ("mcp.server", "duration_ms", "user.id", "pcai.key",
                   "langfuse.session.id", "pcai.request_id", "pcai.iteration",
                   "pcai.args.sha256", "mcp.surface", "mcp.tool.requested"):
        assert absent not in attrs, absent


def test_arguments_are_digested_never_exported():
    secret = "token=sk-live-DO-NOT-LEAK"
    attrs = tool_span.tool_span_attrs(tool="run_kubectl", status="ok", args={"command": secret})
    assert attrs["pcai.args.bytes"] == len('{"command":"token=sk-live-DO-NOT-LEAK"}')
    assert len(attrs["pcai.args.sha256"]) == 16
    assert secret not in repr(attrs)


def test_digest_is_order_insensitive_so_identical_calls_match():
    a = tool_span.tool_span_attrs(tool="t", status="ok", args={"a": 1, "b": 2})
    b = tool_span.tool_span_attrs(tool="t", status="ok", args={"b": 2, "a": 1})
    assert a["pcai.args.sha256"] == b["pcai.args.sha256"]


def test_unserialisable_arguments_do_not_break_the_span():
    attrs = tool_span.tool_span_attrs(tool="t", status="ok", args={"bad": {1, 2}})
    assert "pcai.args.sha256" not in attrs
    assert attrs["mcp.tool"] == "t"
