# SQLhandler MCP server — direct OneLake (Delta) access.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_HOME=/app

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential libgomp1 ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR ${APP_HOME}

# Install the package + deps. mcp>=2.0.0 (MCP 2.0 SDK) comes from
# pyproject.toml via `uv pip install --system -e .`. The build requires
# network (uv sync / pip).
COPY pyproject.toml README.md ./
COPY src ./src

RUN uv pip install --system \
        pyarrow duckdb azure-identity azure-storage-file-datalake pandas tabulate uvicorn starlette \
    && uv pip install --system -e .

ENV PYTHONPATH="/app/src"

EXPOSE 9097

# Streamable HTTP (MCP 2.0) by default — the PCAI helm chart serves /mcp.
# Override to stdio with: --transport stdio  (or SQLHANDLER_TRANSPORT=stdio)
CMD ["python", "-m", "sqlhandler.server", "--transport", "streamable-http", "--host", "0.0.0.0", "--port", "9097"]