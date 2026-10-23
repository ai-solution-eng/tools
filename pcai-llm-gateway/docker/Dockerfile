# pcai-llm-gateway — the multi-tenant LLM + MCP gateway for HPE PCAI:
# OpenAI-compatible /v1 proxy over aggregated model endpoint pools, federated
# MCP with per-tool ACLs, and virtual API keys with ledger-based budgets.
# Base pinned by digest (the python3.14-bookworm-slim tag moves). To refresh
# the pin, resolve the tag's current digest:
#   TOKEN=$(curl -s "https://ghcr.io/token?scope=repository:astral-sh/uv:pull&service=ghcr.io" | sed -n 's/.*"token":"\([^"]*\)".*/\1/p')
#   curl -sI -H "Authorization: Bearer $TOKEN" \
#     -H 'Accept: application/vnd.oci.image.index.v1+json' \
#     https://ghcr.io/v2/astral-sh/uv/manifests/python3.14-bookworm-slim \
#     | grep -i docker-content-digest
# docker/requirements.lock records the known-good Python dependency set this
# image was built and tested with.
FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim@sha256:7cf77f594be8042dab6daa9fe326f90962252268b4f120a7f5dccce4d947e6c1

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_HOME=/app

WORKDIR ${APP_HOME}

# OS CA certificates: the uv base omits them. Upstream model endpoints and
# MCP servers are frequently https://, so TLS verification needs a trust
# store (also what lets corporate-proxy sites verify at all).
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# README.md is COPYed because pyproject.toml declares it as the package
# readme — hatchling validates its existence at build time and fails
# without it (learned the hard way: "OSError: Readme file does not exist").
#
# Deps install FROM docker/requirements.lock (audit finding 19: the lock was
# previously documentation-only while the image re-resolved >= floors — two
# builds of one commit could get different trees). The lock is regenerated
# at release time from the venv that passed the test suite (its header
# records the refresh contract); pyproject.toml ranges remain the SOURCE
# constraints. Deps and the local package install as SEPARATE layers
# (SQLhandler pattern, layer-ordering fix): the dependency wheels live in a
# layer keyed on the manifests (pyproject.toml + README.md + requirements.lock)
# ONLY — no source in this layer — so a code-only release rebuilds just the
# cheap COPY layers below. UV_NO_CACHE=1 keeps the wheel cache out of the
# layer (audit: the uv cache was being baked into the image). The local
# package then installs --no-deps (its dependencies are all present by
# construction). The build requires network (uv pip).
COPY pyproject.toml README.md ./
COPY docker/requirements.lock ./docker/requirements.lock

ENV UV_NO_CACHE=1

RUN uv pip install --system -r docker/requirements.lock

COPY src/ ./src/
RUN uv pip install --system --no-deps .

# Sanity gate (build-time, cheap): the local package must import with zero
# dependency resolution — catches "dep added to pyproject but not to the
# RUN above" drift immediately, in the build log, instead of as a
# first-request ImportError in the pod.
RUN python -c "import pcai_llm; print('pcai_llm', pcai_llm.__version__, 'imports clean')"

# Default working directory (logs/scratch only — the usage ledger lives in
# Postgres, so this image needs NO persistent volume by design).
RUN mkdir -p /tmp/pcai-llm && chown -R 10001:10001 /tmp/pcai-llm

EXPOSE 9200

RUN chmod -R a+rX /app

USER 10001

CMD ["pcai-llm-gateway"]
