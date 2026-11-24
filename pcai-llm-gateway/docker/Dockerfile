# pcai-llm-gateway — the multi-tenant LLM + MCP gateway for HPE PCAI:
# OpenAI-compatible /v1 proxy over aggregated model endpoint pools, federated
# MCP with per-tool ACLs, and virtual API keys with ledger-based budgets.
# BASE IMAGE — trixie-slim, pinned by digest.
#
# WHY NOT bookworm (changed 2026-11-23): uv's python3.14-BOOKWORM variants were last
# pushed 2026-02-04 and are absent from uv's current image matrix — the line is frozen,
# so refreshing its digest returns the same ~3.14.3 build. That left the image eight
# patch releases and THREE expedited security releases behind the host interpreter:
# 3.14.8 (SSLContext use-after-free via sni_callback, zipfile memory exhaustion, tarfile
# filter bypass), 3.14.9 (compromised-CA certifi), 3.14.10 (ssl distrusts the compromised
# eMudhra intermediate CA keys). That last one matters specifically HERE: this gateway is
# a TLS client to every upstream model endpoint and every MCP server, so trusting a
# compromised intermediate is an MITM exposure on the fleet's own outbound path.
# 3.14.5 also reverted the incremental GC to generational after production memory-pressure
# reports — the old base still carried the reverted GC in a long-running server process.
#
# The tag moves; the DIGEST is the pin. To refresh it:
#   TOKEN=$(curl -s "https://ghcr.io/token?scope=repository:astral-sh/uv:pull&service=ghcr.io" | sed -n 's/.*"token":"\([^"]*\)".*/\1/p')
#   curl -sI -H "Authorization: Bearer $TOKEN" \
#     -H 'Accept: application/vnd.oci.image.index.v1+json' \
#     https://ghcr.io/v2/astral-sh/uv/manifests/python3.14-trixie-slim \
#     | grep -i docker-content-digest
#
# NOTE ON THE INVALID FORM: a reference has exactly one tag — "repo:tag:tag" is not a
# reference, it is a parse error (a ':' after the first '/' can only be host:port).
# docker/requirements.lock records the known-good Python dependency set this
# image was built and tested with.
# Pinned by digest. Resolved 2026-11-23 from the OCI image INDEX
# (Accept: application/vnd.oci.image.index.v1+json), and verified two ways: HEAD and GET
# return the same Docker-Content-Digest, and sha256(response body) equals it — so this is
# the multi-arch index digest, which is what a tag pin must name.
#
# The image reports PYTHON_VERSION=3.14.10 (label: 0.13.9-python3.14-trixie-slim), i.e. the
# newest 3.14 patch including the three expedited security releases above.
#
# Per-platform children, if a per-arch pin is ever wanted:
#   linux/amd64  sha256:a5a28632d29063a2b111e4bb172a0bd217ae31137ffc22007510b88e88ebe78c
#   linux/arm64  sha256:d164572cd5fa91379aa9c296414f558ac4314f25b9e14ce3cd94950846623b5a
FROM ghcr.io/astral-sh/uv:python3.14-trixie-slim@sha256:da0f1f04404173d7d2fa158112e09624d5a80262efa86c89882b68389dfd44eb

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
