"""Shared fixtures: a real local REST server (aiohttp, background thread) and
a real local MCP server (MCPServer.streamable_http_app under uvicorn, background thread).

Both bind port 0 (OS-assigned) so tests never collide.
"""

from __future__ import annotations

import asyncio
import json
import queue
import sys
import threading
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


# ---------------------------------------------------------------------------
# REST fixture server
# ---------------------------------------------------------------------------


def _make_rest_app(state: dict):
    from aiohttp import web

    async def healthz(request):
        return web.Response(text="ok")

    async def datasets(request):
        return web.json_response({"datasets": [{"name": "ds-a"}, {"name": "ds-b"}]})

    async def search(request):
        name = request.match_info["name"]
        q = request.query.get("q", "")
        state["searches"].append(
            {"path": request.path, "dataset": name, "q": q, "params": dict(request.query), "headers": dict(request.headers)}
        )
        await asyncio.sleep(0.01)
        top_k = int(request.query.get("top_k", "3"))
        results = [{"q": q, "rank": i} for i in range(min(top_k, 3))]
        return web.json_response({"results": results, "dataset": name})

    async def echo(request):
        body = await request.text()
        state["echoes"].append({"body": body, "headers": dict(request.headers)})
        await asyncio.sleep(0.01)
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            parsed = None
        return web.json_response({"echo": parsed, "results": [1, 2, 3]})

    async def fail(request):
        state["fails"] += 1
        return web.json_response({"error": "boom"}, status=500)

    app = web.Application()
    app.router.add_get("/healthz", healthz)
    app.router.add_get("/api/datasets", datasets)
    app.router.add_get("/api/datasets/{name}/search", search)
    app.router.add_post("/echo", echo)
    app.router.add_get("/fail", fail)
    return app


def _run_aiohttp(app, port_q, stop_event):
    from aiohttp import web

    async def main():
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = runner.addresses[0][1]
        port_q.put(port)
        while not stop_event.is_set():
            await asyncio.sleep(0.05)
        await runner.cleanup()

    asyncio.run(main())


@pytest.fixture()
def rest_server():
    state = {"searches": [], "echoes": [], "fails": 0}
    app = _make_rest_app(state)
    port_q: queue.Queue[int] = queue.Queue()
    stop_event = threading.Event()
    thread = threading.Thread(target=_run_aiohttp, args=(app, port_q, stop_event), daemon=True)
    thread.start()
    port = port_q.get(timeout=10)
    yield {"base_url": f"http://127.0.0.1:{port}", "state": state}
    stop_event.set()
    thread.join(timeout=5)


# ---------------------------------------------------------------------------
# MCP fixture server
# ---------------------------------------------------------------------------


def _make_mcp_server():
    from mcp.server import MCPServer

    server = MCPServer(name="test-rag-mcp", version="1.0.0")

    async def search_dataset(
        query: str,
        dataset_name: str,
        top_k: int = 10,
        use_reranker: bool = False,
        reranker_top_k: int = 3,
    ) -> str:
        await asyncio.sleep(0.02)
        return f"{min(top_k, 3)} results for {query!r} in {dataset_name}"

    async def list_datasets() -> str:
        return "ds-a, ds-b"

    async def boom(query: str) -> str:
        raise RuntimeError("kapow")

    server.add_tool(search_dataset, name="search_dataset", description="Search a dataset")
    server.add_tool(list_datasets, name="list_datasets", description="List datasets")
    server.add_tool(boom, name="boom", description="Always raises")
    return server


def _run_uvicorn(app, port_q, ready, stopper):
    import uvicorn

    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error")
    server = uvicorn.Server(config)

    original = server.handle_exit

    def handle_exit(*a, **kw):
        stopper["stop"] = True
        original(*a, **kw)

    server.handle_exit = handle_exit

    async def wait_port():
        while not server.started:
            await asyncio.sleep(0.05)
        port = server.servers[0].sockets[0].getsockname()[1]
        port_q.put(port)

    async def main():
        serve_task = asyncio.create_task(server.serve())
        try:
            await asyncio.wait_for(wait_port(), timeout=15)
            ready.set()
            while not stopper.get("stop"):
                await asyncio.sleep(0.05)
            server.should_exit = True
        finally:
            await serve_task

    try:
        asyncio.run(main())
    except Exception:
        import traceback

        traceback.print_exc()


@pytest.fixture()
def mcp_server_url():
    app = _make_mcp_server().streamable_http_app()
    port_q: queue.Queue[int] = queue.Queue()
    ready = threading.Event()
    stopper = {"stop": False}
    thread = threading.Thread(target=_run_uvicorn, args=(app, port_q, ready, stopper), daemon=True)
    thread.start()
    if not ready.wait(timeout=20):
        raise RuntimeError("MCP fixture server failed to start")
    port = port_q.get(timeout=5)
    yield f"http://127.0.0.1:{port}/mcp"
    stopper["stop"] = True
    thread.join(timeout=5)
