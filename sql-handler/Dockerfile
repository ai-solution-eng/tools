# SQLhandler MCP server — direct SQL access to columnar data (MCP server).
# NOTE: pin the base image by digest for production builds; this tag moves.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_HOME=/app \
    TMPDIR=/tmp

# Runtime-only system deps: pyarrow/numpy wheels need libgomp1 at runtime.
# NO build-essential — every dependency ships manylinux wheels and the
# local package is pure Python (hatchling), so keeping a compiler
# toolchain in a network-facing server image is pure attack surface.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgomp1 ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 1000 --create-home --shell /usr/sbin/nologin app

WORKDIR ${APP_HOME}

# Install the package + deps. The explicit azure-* pair is also listed in
# pyproject.toml (keep the two in sync): deltalake's OneLake service-principal
# flow uses them. The [iceberg] extra backs the iceberg backend the helm
# chart advertises. The build requires network (pip).
COPY pyproject.toml README.md ./
COPY src ./src

RUN uv pip install --system \
        pyarrow duckdb azure-identity azure-storage-file-datalake pandas tabulate uvicorn starlette \
    && uv pip install --system -e ".[iceberg]"

ENV PYTHONPATH="/app/src"

# Run unprivileged — the helm chart's hardened profile (security.hardened)
# also enforces runAsNonRoot at the pod level.
USER app

EXPOSE 9097

# Streamable HTTP (MCP 2.0) by default — the PCAI helm chart serves /mcp.
# Override to stdio with: --transport stdio  (or SQLHANDLER_TRANSPORT=stdio)
CMD ["python", "-m", "sqlhandler.server", "--transport", "streamable-http", "--host", "0.0.0.0", "--port", "9097"]