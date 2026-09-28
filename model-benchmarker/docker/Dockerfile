# ModelBenchmarker PCAI app: memory estimator + API-key-gated benchmark launchers.
# The bench engines (benchmark_chat / endpoint_benchmarker) run as subprocesses of
# the webapp; the image carries the whole package. NOTE: pin the base image by
# digest for production builds; this tag moves.
FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Literal /app, never a WORKDIR variable: an unexpanded variable name becomes a
# literal-string workdir, silently redirecting the uv build to a directory of
# that name; the build then dies inside setuptools.
WORKDIR /app

# CA certificates: needed for HF config.json fetches and https benchmark targets.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# .dockerignore (context root) excludes src/**/pcai_models.py — platform JWTs
# ship in no delivery artifact; the webapp and bench CLIs here address targets
# by --url + Secret-sourced keys and never import it. README.md is re-included
# there (negation) and copied: pyproject declares it as the package
# long_description, and a missing readme breaks metadata completeness.
#
# Deps and the local package install as SEPARATE layers (SQLhandler pattern,
# layer-ordering fix): the dependency wheels live in a layer keyed on the
# manifests (README.md + pyproject.toml + requirements.txt) ONLY — no source
# in this layer — so a code-only release rebuilds just the cheap COPY layers
# below instead of re-resolving and re-downloading every wheel. requirements.txt
# drives the install (pyproject's [project].dependencies repeat the same pins,
# so the manifests stay in sync): requirements.txt is the superset manifest and
# already ships in the context, making it the authoritative dep source here.
COPY README.md pyproject.toml requirements.txt ./

RUN uv pip install --system -r requirements.txt

COPY src/ ./src/

# THE credential boundary, enforced by the build itself: if pcai_models.py (or
# any archive) ever reaches the context again — a .dockerignore edit, a new
# subtree, an ignore-pattern regression — the build FAILS here, loudly, instead
# of shipping JWTs. Runs after the source COPY so it scans exactly what the
# image will carry (and what the install below resolves against).
RUN if find /app -name 'pcai_models.py' -o -name '*.zip' -o -name '*.tgz' -o -name '*.tar.gz' | grep -q .; then \
        echo 'CREDENTIAL LEAK in build context — see .dockerignore' >&2; \
        find /app -name 'pcai_models.py' -o -name '*.zip' -o -name '*.tgz' >&2; \
        exit 1; \
    fi
RUN chmod -R a+rX /app || true
RUN uv pip install --system --no-deps .

# Sanity gate (build-time, cheap): the local package must import with zero
# dependency resolution — catches "dep added to pyproject/requirements.txt but
# missing from the dep layer" drift immediately, in the build log, instead of
# as a first-request ImportError in the pod.
RUN python -c "import model_benchmarker; print('model_benchmarker', model_benchmarker.__version__, 'imports clean')"

# Runs land under /data (the chart's PVC); the app writes only here.
RUN mkdir -p /data && chown 1000:1000 /data

EXPOSE 8000

CMD ["python", "-m", "model_benchmarker.webapp", "--host", "0.0.0.0", "--port", "8000"]
