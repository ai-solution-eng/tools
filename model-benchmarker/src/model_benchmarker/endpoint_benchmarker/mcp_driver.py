"""MCP driver: sessions, tool discovery, and the per-user tool-call loop.

Key fix vs the original Multimodal RAG benchmark script: an MCP target needs
**only the MCP endpoint URL**.  Connectivity is verified by opening the MCP
session itself (initialize handshake + ``list_tools``) and discovery happens
over MCP — there is no companion REST URL to specify or wrongly derive.

Tool arguments are resolved once (probe connection) from the tool's
``inputSchema`` + ``--arg`` overrides; every simulated user then opens its
own persistent session and calls the resolved tool in a loop, mirroring how
a real LLM client keeps a connection open.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from .stats import BenchmarkStats, err_key
from .targets import DATASET_ARG_NAMES, McpTarget, TargetError, _schema_properties, pick_query_arg, pick_tool

log = logging.getLogger("endpoint_benchmarker")


@dataclass
class ResolvedTool:
    """Everything the per-user loop needs, resolved once via a probe session."""

    tool_name: str
    query_arg: str
    args: dict  # full argument dict; "{query}" placeholder handled per call
    description: str | None = None


def _build_tool_args(tool, target: McpTarget, query_arg: str) -> dict:
    """Assemble the full argument dict for the chosen tool.

    ``query_arg`` is deliberately *not* inserted here (the sampled query is
    merged in per call) but it does count as covered for the required-args
    check.
    """
    props, required = _schema_properties(tool)
    args = dict(target.args)

    if target.dataset is not None:
        for cand in DATASET_ARG_NAMES:
            match = next((n for n in props if n.lower() == cand), None)
            if match is not None:
                args.setdefault(match, target.dataset)
                break

    missing = [r for r in required if r not in args and r != query_arg and "default" not in (props.get(r) or {})]
    if missing:
        raise TargetError(
            f"tool '{tool.name}' requires argument(s) {missing} with no default and none were provided; "
            f"pass them with --arg name=value (the sampled query goes to '{query_arg}')"
        )
    return args


@asynccontextmanager
async def open_mcp_session(target: McpTarget) -> AsyncIterator:
    """Open one MCP client session against the target (streamable-http or sse).

    Custom headers and/or ``verify=False`` require building the HTTP client
    ourselves (the mcp 2.x SDK uses the ``httpx2`` package for HTTP
    transports); otherwise we pass the plain transport.
    """
    from mcp import Client  # imported lazily: REST-only users need no mcp SDK

    if target.transport == "streamable-http":
        from mcp.client.streamable_http import streamable_http_client

        if target.headers or target.insecure:
            import httpx2

            http_client = httpx2.AsyncClient(
                headers=target.headers or None,
                verify=not target.insecure,
                follow_redirects=True,
                timeout=httpx2.Timeout(30.0, read=300.0),
            )
            transport = streamable_http_client(target.url, http_client=http_client, terminate_on_close=False)
        else:
            transport = streamable_http_client(target.url, terminate_on_close=False)
    elif target.transport == "sse":
        from mcp.client.sse import sse_client

        if target.insecure:
            import httpx2

            def factory(  # signature mirrors mcp's McpHttpClientFactory protocol
                headers: dict[str, str] | None = None,
                timeout: httpx2.Timeout | None = None,
                auth: httpx2.Auth | None = None,
            ) -> httpx2.AsyncClient:
                return httpx2.AsyncClient(headers=headers, timeout=timeout, auth=auth, verify=False)

            transport = sse_client(target.url, headers=target.headers or None, httpx_client_factory=factory)
        else:
            transport = sse_client(target.url, headers=target.headers or None)
    else:
        raise TargetError(f"unsupported MCP transport: {target.transport!r}")

    async with Client(transport) as client:
        yield client


async def resolve_mcp_target(target: McpTarget, list_only: bool = False) -> list[ResolvedTool] | None:
    """Connect once, verify the server, and resolve the tool + arguments.

    With ``list_only=True`` prints the tool table instead and returns None
    (the ``--list-tools`` action).

    Tool selection/validation happens *after* the session closes on purpose:
    the session runs in an anyio TaskGroup that wraps exceptions raised
    inside it into an ``ExceptionGroup``, which would mangle user-facing
    errors like "tool not found".
    """
    async with open_mcp_session(target) as client:
        info = client.server_info
        log.info(
            "Connected to MCP server: %s (protocol %s)",
            getattr(info, "name", "?") if info else "?",
            getattr(client, "protocol_version", "?"),
        )
        tools_result = await asyncio.wait_for(client.list_tools(), timeout=30.0)
        tools = list(tools_result.tools)

        if list_only:
            print(f"\n{len(tools)} tool(s) on {target.url}:\n")
            for t in tools:
                _props, required = _schema_properties(t)
                try:
                    qarg = pick_query_arg(t)
                    qnote = f"  query -> '{qarg}'"
                except TargetError:
                    qnote = ""
                req = ", ".join(required) if required else "-"
                desc = (t.description or "").strip().splitlines()[0][:100] if t.description else ""
                print(f"  {t.name}  (required: {req}){qnote}")
                if desc:
                    print(f"      {desc}")
            print()
            return None

    tool = pick_tool(tools, target.tool_name)
    query_arg = target.query_arg or pick_query_arg(tool)
    args = _build_tool_args(tool, target, query_arg)
    log.info(
        "Resolved tool '%s' (query -> '%s', %d fixed arg(s))%s",
        tool.name,
        query_arg,
        len(args),
        f": {sorted(args)}" if args else "",
    )
    return [
        ResolvedTool(
            tool_name=tool.name,
            query_arg=query_arg,
            args=args,
            description=getattr(tool, "description", None),
        )
    ]


async def run_user_mcp(
    user_id: int,
    target: McpTarget,
    resolved: ResolvedTool,
    queries: list[str],
    duration: float,
    ramp_delay: float,
    stats: BenchmarkStats,
    call_timeout: float = 120.0,
    seed: int | None = None,
) -> None:
    """Simulate one user holding one MCP session and calling the tool in a loop.

    Timed-out calls are recorded as failures so a queued-up level still
    terminates near *duration* (the streamable-http read timeout is 300s —
    without the cap, ``asyncio.gather`` waits for stragglers far past the
    level window and the Prometheus join would smear).
    """
    rng = random.Random(seed if seed is not None else user_id)

    if ramp_delay > 0:
        await asyncio.sleep(ramp_delay)

    end_time = time.monotonic() + duration
    try:
        async with open_mcp_session(target) as client:
            while time.monotonic() < end_time:
                query = rng.choice(queries)
                call_args = {**resolved.args, resolved.query_arg: query}
                t0 = time.monotonic()
                try:
                    result = await asyncio.wait_for(
                        client.call_tool(resolved.tool_name, call_args),
                        timeout=call_timeout,
                    )
                    elapsed = time.monotonic() - t0
                    if result.is_error:
                        err_text = next(
                            (b.text[:120] for b in result.content if hasattr(b, "text")),
                            "",
                        )
                        stats.record_failure(
                            elapsed,
                            f"MCP error: {err_text}" if err_text else "MCP error",
                        )
                        log.debug("[user=%s] MCP error in %.0fms: %s", user_id, elapsed * 1000, err_text)
                    else:
                        nbytes = sum(len(getattr(b, "text", "") or "") for b in result.content)
                        n_results = max(1, len(result.content))
                        stats.record_success(elapsed, n_results=n_results, n_bytes=nbytes)
                        log.debug("[user=%s] ok in %.0fms (%d blocks)", user_id, elapsed * 1000, len(result.content))
                except TimeoutError:
                    elapsed = time.monotonic() - t0
                    stats.record_failure(elapsed, f"Timeout (>{call_timeout:g}s)")
                except Exception as exc:
                    elapsed = time.monotonic() - t0
                    stats.record_failure(elapsed, err_key(exc))
                    log.debug("[user=%s] call failed: %s", user_id, exc)
                    await asyncio.sleep(0.5)  # avoid a tight error loop hammering the server
    except Exception as exc:  # session creation / initialization failed
        stats.record_failure(0.0, f"MCP connect: {err_key(exc)}")
        log.debug("[user=%s] session setup failed: %s", user_id, exc)


async def mcp_call_once(target: McpTarget, resolved: ResolvedTool, query: str, call_timeout: float) -> None:
    """One throwaway-session call — used for warm-up rounds."""
    try:
        async with open_mcp_session(target) as client:
            await asyncio.wait_for(
                client.call_tool(resolved.tool_name, {**resolved.args, resolved.query_arg: query}),
                timeout=call_timeout,
            )
    except Exception as exc:
        log.debug("warm-up call failed (ignored): %s", err_key(exc))
