"""The ModelBenchmarker PCAI app: FastAPI routes + fleet API-key gate.

Surfaces:

* GET /                    the LLM memory estimator (the DEFAULT page, public --
                           deterministic math, no cluster access, no secrets)
* GET /benchmark/chat      chat-bench launcher (API-key gated)
* GET /benchmark/endpoint  universal-bench launcher (API-key gated)
* POST /api/estimate       memory-estimate JSON API backing the default page
* GET /api/endpoints       the operator-configured endpoint allowlist (gated)
* POST /api/runs           launch a benchmark (gated) -- one per endpoint
* GET /api/runs            run history (gated)
* GET /api/runs/{id}       status + log tail + artifacts (gated)
* POST /api/runs/{id}/cancel  (gated)
* GET /api/runs/{id}/artifacts/{name}  download one artifact (gated)
* GET /healthz /readyz     probes (public)

Auth follows the fleet K8S-MCP pattern (utils/mcp_auth.py): keys come from
comma-separated MCP_API_KEYS / BENCH_API_KEYS (a chart Secret), are matched
constant-time, and are RE-READ PER REQUEST so a Secret rotation reaches a
running pod without a restart. No keys configured -> benchmark routes stay
gated-OPEN with a loud startup warning (local development mode) -- the chart
wires the Secret unconditionally so shared deployments never start open.

The benchmark HTML pages are part of the protected surface: a browser without
the key gets the fleet's 401 on the page fetch itself; the page then asks for
the key once and keeps it in sessionStorage for its API calls.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .. import __version__
from ..memory_model import ModelConfig, config_from_dict
from ..memory_model.configs import load_config_json
from ..memory_model.dtypes import dtype_label
from ..memory_model.estimate import (
    DEFAULT_OVERHEAD_GIB,
    EstimateRequest,
    EstimateResult,
    HicacheSpec,
    SpeculativeSpec,
    resolve_dtype_weight,
    run_estimate,
    summary_line,
)
from ..memory_model.gpus import DEFAULT_GPU, GPU_DB, GpuSpec, resolve_gpu
from ..utils import mcp_auth as _mcp_auth
from . import runner as R
from .compare import MAX_COMPARE_RUNS, finalize_comparison  # results-page compare feature
from .library import (
    LIB_PREFIX,
    library_entry,
    scan_library,
    seed_results_into_workdir,
)  # committed results/ tree
from .library import (
    RESULTS_DIR_ENV as R__RESULTS_ENV,
)
from .runner import BenchError, BusyEndpointError, MaxRunsError, RunManager, manager_from_env

_UI_DIR = Path(__file__).parent / "ui"
DEFAULT_CONTEXTS = (4096, 16384, 65536, 262144, 1048576)
AUTH_ENV_NAMES = ("MCP_API_KEYS", "BENCH_API_KEYS")

MAX_RUN_CONCURRENT = int(os.environ.get(R.MAX_CONCURRENT_ENV) or R.DEFAULT_MAX_CONCURRENT)
MAX_RUN_SECONDS = float(os.environ.get(R.MAX_RUN_SECONDS_ENV) or R.DEFAULT_RUN_TIMEOUT_S)


def auth_enabled() -> bool:
    return bool(_mcp_auth.configured_keys(AUTH_ENV_NAMES))


def _page(name: str) -> str:
    path = _UI_DIR / name
    if not path.is_file():
        raise HTTPException(status_code=404, detail="page missing")
    return path.read_text(encoding="utf-8")


def _status_payload() -> dict:
    return {
        "app": "model-benchmarker",
        "version": __version__,
        "auth_required": auth_enabled(),
        "telemetry": bool((os.environ.get(R.PROM_URL_ENV) or "").strip()),
        "max_concurrent_runs": MAX_RUN_CONCURRENT,
        "max_run_seconds": MAX_RUN_SECONDS,
    }


def create_app(work_dir: str | None = None) -> Any:
    app = FastAPI(title="ModelBenchmarker", version=__version__, docs_url=None, redoc_url=None, openapi_url=None)
    manager: RunManager = manager_from_env(work_dir)

    # Library seeding: images that ship the committed results/ tree point
    # BENCH_RESULTS_DIR at it; deployments that instead keep the library on
    # the PVC get a one-time seed into <work_dir>/results (skipped when the
    # PVC copy already exists — publishing is kubectl cp). Also covers the
    # broken-in-practice v0.4.3 shape: tree copied but the ENV lost in the
    # build — the seed derivation below re-points the env when it applies.
    if not os.environ.get(R__RESULTS_ENV):
        seeded = seed_results_into_workdir(manager.work_dir)
        if seeded is not None:
            os.environ[R__RESULTS_ENV] = str(seeded)

    if (_UI_DIR / "app.css").is_file():
        app.mount("/static", StaticFiles(directory=_UI_DIR), name="static")

    # ------------------------------------------------------------------ public

    @app.get("/healthz")
    @app.get("/readyz")
    async def healthz() -> JSONResponse:
        return JSONResponse({"status": "ok", "app": "model-benchmarker", "version": __version__})

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        """The DEFAULT page: the LLM memory estimator."""
        return HTMLResponse(_page("memory.html"))

    @app.get("/results", response_class=HTMLResponse)
    async def results_page() -> HTMLResponse:
        """Past benchmark runs from the PVC (public: results are shareable)."""
        return HTMLResponse(_page("results.html"))

    @app.get("/api/results")
    async def results_index() -> JSONResponse:
        """Public-safe index of every run on the PVC (no params, no logs)."""
        return JSONResponse({"runs": manager.public_index()})

    @app.get("/api/results/compare")
    async def results_compare(request: Request) -> JSONResponse:
        """Side-by-side comparison of 2..4 runs (public read-only, like the
        rest of the /results surface). ids: comma-separated run ids, read
        from request.query_params so the route behaves identically through
        real HTTP and the ASGI test helper (which routes on the full URI);
        unknown ids are skipped and reported under "missing"."""
        ids = request.query_params.get("ids") or request.url.query
        requested = [rid.strip() for rid in str(ids or "").split(",") if rid.strip()]
        if not requested:
            raise HTTPException(status_code=400, detail="ids: at least one run id is required")
        if len(requested) > MAX_COMPARE_RUNS:
            raise HTTPException(status_code=400, detail=f"compare at most {MAX_COMPARE_RUNS} runs at a time")
        if len(requested) < 2:
            raise HTTPException(status_code=400, detail="compare needs at least 2 run ids")
        runs_data: list[dict] = []
        missing: list[str] = []
        for rid in requested:
            if rid.startswith(LIB_PREFIX):
                # a committed results/ baseline (lib:<model-slug>/<stem>)
                entry = library_entry(rid)
                if entry is None:
                    missing.append(rid)
                else:
                    runs_data.append(entry)
                continue
            try:
                runs_data.append(manager.compare_data(rid))
            except KeyError:
                missing.append(rid)
        if not runs_data:
            raise HTTPException(
                status_code=400,
                detail="no such run" if not missing else f"no such run(s): {', '.join(missing)}",
            )
        if len(runs_data) == 1:
            # a single surviving run cannot be compared against anything
            raise HTTPException(
                status_code=400,
                detail="need at least 2 comparable runs" if not missing else f"no such run(s): {', '.join(missing)}",
            )
        return JSONResponse(finalize_comparison(runs_data, requested))

    @app.get("/api/library")
    async def results_library() -> JSONResponse:
        """The committed results/ tree (curated serving-setup baselines),
        public read-only like the rest of the results surface. Deployed
        images without a results/ tree return an empty list (the UI hides
        the section); dev trees and images that ship results/ get the full
        library with lib:<slug>/<stem> ids the compare endpoint resolves."""
        return JSONResponse(scan_library())

    @app.get("/api/gpus")
    async def gpu_choices() -> JSONResponse:
        """GPU catalog for the memory page's select (static DB, no auth: pure
        reference data, no cluster access)."""
        return JSONResponse({"gpus": [{"name": s.name, "vram_gib": s.vram_gib} for _k, s in GPU_DB]})

    @app.get("/unlock", response_class=HTMLResponse)
    async def unlock_page() -> HTMLResponse:
        """Branded sign-in page for gated browser navigation (public by
        design: it only collects the key the caller already has)."""
        return HTMLResponse(_page("unlock.html"))

    @app.get("/api/status")
    async def status() -> JSONResponse:
        payload = _status_payload()
        payload["busy_endpoints"] = manager.busy_endpoints()
        return JSONResponse(payload)

    @app.post("/api/estimate")
    async def estimate(request: Request) -> JSONResponse:
        try:
            payload = await request.json()
        except ValueError:
            raise HTTPException(status_code=400, detail="body must be JSON") from None
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="body must be a JSON object")
        return JSONResponse(_run_estimate_api(payload))

    # ------------------------------------------------- gated: benchmark surface

    @app.get("/benchmark/chat", response_class=HTMLResponse)
    async def bench_chat_page() -> HTMLResponse:
        return HTMLResponse(_page("bench_chat.html"))

    @app.get("/benchmark/endpoint", response_class=HTMLResponse)
    async def bench_endpoint_page() -> HTMLResponse:
        return HTMLResponse(_page("bench_endpoint.html"))

    @app.get("/api/catalog")
    async def catalog_models() -> JSONResponse:
        """Seed-catalog models for the memory page's searchable dropdown:
        one entry per catalog deployment (model ref + its serving preset:
        gpu tier/count, TP, kv dtype, mem-fraction). Free-text model refs /
        pasted config.json stay fully supported (advice only, like /api/targets)."""
        return JSONResponse(_catalog_payload())

    @app.get("/api/targets")
    async def targets_list() -> JSONResponse:
        """Operator-curated targets for the combobox (advice only; free text
        stays allowed). Gated with the rest of the benchmark surface."""
        return JSONResponse(
            {"targets": [{"name": t.name, "url": t.url, "kind": t.kind} for t in R.load_target_suggestions()]}
        )

    @app.post("/api/runs")
    async def launch_run(request: Request) -> JSONResponse:
        try:
            payload = await request.json()
        except ValueError:
            raise HTTPException(status_code=400, detail="body must be JSON") from None
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="body must be a JSON object")
        kind = str(payload.get("kind") or "").strip().lower()
        target_url = str(payload.get("target") or payload.get("url") or "").strip()
        params = payload.get("params") or {}
        if not isinstance(params, dict):
            raise HTTPException(status_code=400, detail="params must be an object")

        if kind not in R.KINDS:
            raise HTTPException(status_code=400, detail=f"kind must be one of: {', '.join(R.KINDS)}")
        try:
            R.validate_target(target_url)
        except R.BenchError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        try:
            rec = manager.launch(
                kind, target_url, params, max_concurrent=MAX_RUN_CONCURRENT, max_run_seconds=MAX_RUN_SECONDS
            )
        except BusyEndpointError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        except MaxRunsError as exc:
            raise HTTPException(status_code=429, detail=str(exc)) from None
        except BenchError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        return JSONResponse(
            {"run_id": rec.run_id, "status": rec.status, "endpoint": rec.endpoint, "kind": rec.kind},
            status_code=202,
        )

    @app.get("/api/runs")
    async def runs_list() -> JSONResponse:
        return JSONResponse({"runs": manager.list_runs()})

    @app.get("/api/runs/{run_id}")
    async def run_detail(run_id: str) -> JSONResponse:
        try:
            return JSONResponse(manager.get(run_id))
        except KeyError:
            raise HTTPException(status_code=404, detail="no such run") from None

    @app.post("/api/runs/{run_id}/cancel")
    async def run_cancel(run_id: str) -> JSONResponse:
        try:
            rec = manager.cancel(run_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="no such run") from None
        except BenchError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        return JSONResponse({"run_id": rec.run_id, "status": "cancelling"})

    @app.get("/api/runs/{run_id}/artifacts/{name}")
    async def run_artifact(run_id: str, name: str) -> FileResponse:
        try:
            path = manager.artifact_path(run_id, name)
        except KeyError:
            raise HTTPException(status_code=404, detail="no such run") from None
        except BenchError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        media = {
            ".html": "text/html",
            ".md": "text/markdown",
            ".json": "application/json",
            ".csv": "text/csv",
            ".log": "text/plain",
            ".txt": "text/plain",
        }
        return FileResponse(path, filename=name, media_type=media.get(path.suffix, "application/octet-stream"))

    # ------------------------------------------------ fleet API-key gate (LAST)
    # Wrap the ASGI app AFTER the routes are defined; the middleware re-reads
    # the key set per request (Secret rotation without restart). Only the
    # benchmark surface is gated: /, /api/estimate and /api/status are public.

    def _protected(path: str) -> bool:
        # GATED: the launch surface (start a run, see the target/model advice
        # catalogs) and the launcher pages. PUBLIC by design: reading run
        # STATUS and downloading ARTIFACTS (reports are the point of the app
        # — share links without handing out the launch key).
        if path.startswith("/benchmark/"):
            return True  # launcher pages
        if path == "/api/targets":
            return True  # launch-advice catalog
        # the launch surface is POST /api/runs and POST .../cancel; everything
        # else under /api/runs (status, log tail, artifact list/download) is a
        # PUBLIC read-only result
        return path == "/api/runs" or path.endswith("/cancel")

    if not _mcp_auth.configured_keys(AUTH_ENV_NAMES):
        _mcp_auth.warn_if_open("model-benchmarker-web", AUTH_ENV_NAMES)
    return _GatedApp(_mcp_auth.ApiKeyAuthMiddleware(app, env_names=AUTH_ENV_NAMES, protected=_protected), app.routes)


class _GatedApp:
    """ASGI callable wrapping the auth middleware, keeping .routes reachable
    (uvicorn calls the object itself; introspection/tests read .routes).

    Cookie bridge for BROWSER NAVIGATION: a top-level navigation to a gated
    page cannot carry an X-API-Key header, so the unlock page stores the key
    in an mb-key cookie; this wrapper lifts that cookie into the X-API-Key
    header before the fleet middleware validates it (API clients are
    unaffected -- they keep sending the header). An unauthorized BROWSER
    navigation is redirected to /unlock (the branded sign-in page) instead of
    receiving the raw 401 JSON; fetch/XHR calls still get the JSON 401 the
    page JS handles."""

    _COOKIE_NAME = "mb-key"

    def __init__(self, middleware: Any, routes: Any) -> None:
        self._middleware = middleware
        self.routes = routes

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") == "http" and self._is_browser_navigation(scope):
            # lift the mb-key cookie into X-API-Key
            headers = [(k, v) for k, v in scope.get("headers", []) if k.lower() != b"x-api-key"]
            for k, v in scope.get("headers", []):
                if k.lower() == b"cookie":
                    for part in v.decode("latin-1").split(";"):
                        if part.strip().startswith(self._COOKIE_NAME + "="):
                            headers.append((b"x-api-key", part.split("=", 1)[1].strip().encode("latin-1")))
            nav_scope = dict(scope, headers=headers)
            # intercept the response: a 401 on a navigation -> redirect to /unlock
            state = {"status": None}

            async def send_intercept(message: Any) -> None:
                if message["type"] == "http.response.start":
                    state["status"] = message["status"]
                    if message["status"] != 401:
                        await send(message)
                    return
                if message["type"] == "http.response.body" and state.get("status") == 401:
                    return  # swallow the JSON body
                await send(message)

            await self._middleware(nav_scope, receive, send_intercept)
            if state.get("status") == 401:
                from urllib.parse import quote as _quote

                target = scope.get("path", "/benchmark/chat")
                await send(
                    {
                        "type": "http.response.start",
                        "status": 302,
                        "headers": [(b"location", ("/unlock?next=" + _quote(target, safe="/")).encode())],
                        # consume the receive chain cleanly for the server
                    }
                )
                await send({"type": "http.response.body", "body": b""})
            return
        await self._middleware(scope, receive, send)

    @staticmethod
    def _is_browser_navigation(scope: Any) -> bool:
        """True for top-level browser navigations (sec-fetch-mode: navigate or
        an accept header containing text/html)."""
        for k, v in scope.get("headers", []):
            lk = k.lower()
            if lk == b"sec-fetch-mode" and v.strip().lower() == b"navigate":
                return True
            if lk == b"accept" and b"text/html" in v.lower():
                return True
        return False


# ---------------------------------------------------------------------------
# memory-estimate API (backing the default page) -- the same deterministic
# math as the memory-estimate CLI, over HTTP
# ---------------------------------------------------------------------------


def _catalog_payload() -> dict:
    """Seed-catalog entries shaped for the memory page combobox. Resolution
    order: BENCH_CATALOG_PATH (operator override) -> the Model-Downloader
    catalog discovered next to the repo (dev trees) -> the snapshot bundled
    in the image (the in-cluster default). Serving args come from the
    entry's launch arguments, never guessed. Fails soft: nothing found ->
    empty list."""
    catalog_path: Path | None = None
    env_path = (os.environ.get(R.CATALOG_PATH_ENV) or "").strip()
    if env_path:
        catalog_path = Path(env_path)
    if catalog_path is None:
        try:
            from ..results_to_html import discover_catalog

            catalog_path = discover_catalog()
        except Exception:
            catalog_path = None
    bundled = Path(__file__).parent / "seed_catalog.json"
    if (catalog_path is None or not catalog_path.exists()) and bundled.is_file():
        catalog_path = bundled
    if catalog_path is None or not Path(catalog_path).exists():
        return {"models": [], "source": None}
    try:
        entries = json.loads(Path(catalog_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"models": [], "source": str(catalog_path)}
    if not isinstance(entries, list):
        return {"models": [], "source": str(catalog_path)}

    models: list[dict] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        a = entry.get("arguments") or []
        repo = _catalog_repo_ref(entry)
        served_name = _catalog_served_name(entry)
        if not repo and not served_name:
            continue
        gpu = str(entry.get("tier") or "?")
        gpu_count = entry.get("resource_request_gpu") or 1
        tp = _arg_value(a, "--tp-size") or gpu_count
        pp = _arg_value(a, "--pp-size") or 1
        kv = _arg_value(a, "--kv-cache-dtype")
        mem = _arg_value(a, "--mem-fraction-static")
        # engine variants of the SAME model on the SAME tier differ by their
        # runner/speculative launch flags (sglang DFLASH vs vllm MTP) — keep
        # both, they benchmark differently
        key = f"{served_name or repo}|{gpu}|{gpu_count}|{tp}|{pp}|{_arg_value(a, '--moe-runner-backend') or ''}|{(_arg_value(a, '--speculative-algorithm') or _json_arg_field(_arg_value(a, '--speculative-config'), 'method') or '')}"
        if key in seen:
            continue
        seen.add(key)
        models.append(
            {
                "name": served_name or repo,
                "repo": repo,
                "catalog_id": entry.get("catalog_id"),
                "label": f"{entry.get('name') or repo}",
                "gpu": gpu,
                "gpus": gpu_count,
                "tp": tp,
                "pp": int(pp) if str(pp).isdigit() else 1,
                "kv_dtype": kv or "",
                "mem_fraction": mem or "",
                "moe_runner": _arg_value(a, "--moe-runner-backend") or "",
                "speculative": {
                    "algorithm": (
                        _arg_value(a, "--speculative-algorithm")
                        or (_json_arg_field(_arg_value(a, "--speculative-config"), "method") or "")
                    )
                    .__str__()
                    .upper(),
                    "draft_tokens": int(
                        _arg_value(a, "--speculative-num-draft-tokens")
                        or _json_arg_field(_arg_value(a, "--speculative-config"), "num_speculative_tokens")
                        or 0
                    ),
                    "draft_model": _arg_value(a, "--speculative-draft-model-path") or "",
                    "layers": 0,
                },
                "description": str(entry.get("description") or "")[:140],
            }
        )
    return {"models": models, "source": str(catalog_path)}


def _json_arg_field(argv_val: str | None, field: str) -> str | int | None:
    """A field out of a vLLM-style JSON launch flag (--speculative-config)."""
    if not argv_val:
        return None
    try:
        data = json.loads(argv_val)
    except ValueError:
        return None
    return data.get(field) if isinstance(data, dict) else None


def _arg_value(argv: list, flag: str) -> str | None:
    for i, x in enumerate(argv):
        if x == flag and i + 1 < len(argv):
            return str(argv[i + 1])
    return None


def _catalog_repo_ref(entry: dict) -> str | None:
    """HF repo id / local path a catalog entry serves (CLI parity: prefer the
    serve/run argument, then --model-path, then the PVC URI's tail)."""
    a = entry.get("arguments") or []
    for i, x in enumerate(a):
        if x in ("serve", "run") and i + 1 < len(a) and "/" in str(a[i + 1]) and not str(a[i + 1]).startswith("-"):
            return str(a[i + 1])
    mp = _arg_value(a, "--model-path")
    if mp:
        return str(mp)
    uri = str(entry.get("uri") or "")
    if "pvc://" in uri:
        tail = uri.split("pvc://", 1)[1].split("?")[0]
        parts = [p for p in tail.split("/") if p]
        if len(parts) >= 2:
            return "/".join(parts[-2:])
    return None


def _catalog_served_name(entry: dict) -> str | None:
    """The --served-model-name the entry launches with (the id clients call)."""
    return _arg_value(entry.get("arguments") or [], "--served-model-name")


_GPU_CHOICES = [{"name": spec.name, "vram_gib": spec.vram_gib} for _keys, spec in GPU_DB]


def _resolve_gpu(payload: dict) -> GpuSpec:
    name = str(payload.get("gpu") or "").strip()
    vram = payload.get("gpu_vram")
    if vram is not None and vram != "":
        try:
            v = float(vram)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="gpu_vram must be a number (GiB)") from None
        if not 1 <= v <= 8192:
            raise HTTPException(status_code=400, detail="gpu_vram must be 1..8192 GiB")
        return GpuSpec(name=name or "custom", vram_gib=v)
    gpu = resolve_gpu(name) if name else None
    return gpu or DEFAULT_GPU


def _int_field(payload: dict, key: str, default: int, lo: int, hi: int) -> int:
    v = payload.get(key)
    if v is None or v == "":
        return default
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail=f"{key} must be an integer") from None
    if not lo <= n <= hi:
        raise HTTPException(status_code=400, detail=f"{key} must be between {lo} and {hi}")
    return n


def _mirror_config(model_ref: str) -> Path | None:
    """The webapp's bundled config mirror: config.json files for the seed-catalog
    models ship inside the image (webapp/hf_configs/). In-cluster this answers
    immediately with ZERO egress -- huggingface.co may be unreachable from a
    customer PCAI namespace. The runtime PVC mirror (managed by the estimate
    path below) is checked first so user-fetched configs survive restarts."""
    candidates = [
        _work_dir() / "hf-configs" / (model_ref.replace("/", "__") + ".json"),
        Path(__file__).parent / "hf_configs" / (model_ref.replace("/", "__") + ".json"),
    ]
    for c in candidates:
        if c.is_file():
            return c
    return None


def _ensure_hf_cache_writable() -> None:
    """Hub downloads need a writable HF cache; pod images often ship a read-only
    or ephemeral ~/.cache (the Qwen3.8-Flash-Next fetch failure). Point HF_HOME
    at the work dir (PVC-backed) when the environment has not chosen one --
    must run before any hub call."""
    if not os.environ.get("HF_HOME"):
        cache = _work_dir() / "hf-cache"
        try:
            cache.mkdir(parents=True, exist_ok=True)
            os.environ["HF_HOME"] = str(cache)
        except OSError:
            pass  # keep the default; the hub error now surfaces verbatim


def _work_dir() -> Path:
    return Path(os.environ.get("BENCH_WORK_DIR") or "/data")


def _hicache_from_payload(payload: dict) -> HicacheSpec:
    """HicacheSpec from the memory page's form fields.

    hicache: "" (off) | "ratio" | "size"; hicache_l3 GiB is the optional L3
    backing-tier budget. hicache_tp_layout: "" (auto) | "replicated" |
    "sharded" — the explicit override for the rare engine that differs from
    the model-structure default. Invalid combos fail soft (off + warning is
    wrong for a math tool: a bad input is a 400).
    """
    mode = str(payload.get("hicache") or "").strip().lower()
    if mode not in ("ratio", "size"):
        return HicacheSpec.off()

    def _num(key: str) -> float:
        try:
            v = float(payload.get(key) or 0)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail=f"{key} must be a number") from None
        return v

    ratio = _num("hicache_ratio") if mode == "ratio" else 0.0
    size = _num("hicache_size") if mode == "size" else 0.0
    l3 = _num("hicache_l3")
    if mode == "ratio" and ratio <= 0:
        raise HTTPException(status_code=400, detail="hicache_ratio must be > 0 when hicache=ratio")
    if mode == "size" and size <= 0:
        raise HTTPException(status_code=400, detail="hicache_size must be > 0 when hicache=size")
    if l3 < 0:
        raise HTTPException(status_code=400, detail="hicache_l3 must be >= 0")
    layout = str(payload.get("hicache_tp_layout") or "").strip().lower()
    rep: bool | None = {"replicated": True, "sharded": False}.get(layout)
    if layout and rep is None:
        raise HTTPException(status_code=400, detail="hicache_tp_layout must be '', 'replicated' or 'sharded'")
    return HicacheSpec(l2_mode=mode, l2_ratio=ratio, l2_gib=size, l3_gib=l3, l2_tp_replicated=rep)


_ensure_hf_cache_writable()


def _run_estimate_api(payload: dict) -> dict:
    model = str(payload.get("model") or "").strip()
    config_json = payload.get("config_json")
    source = ""
    cfg: ModelConfig | None = None
    if config_json:
        try:
            raw = json.loads(config_json) if isinstance(config_json, str) else config_json
        except ValueError:
            raise HTTPException(status_code=400, detail="config_json is not valid JSON") from None
        if not isinstance(raw, dict):
            raise HTTPException(status_code=400, detail="config_json must be a JSON object")
        cfg = config_from_dict(raw)
        source = "pasted config.json"
    elif model:
        mirrored = _mirror_config(model)
        if mirrored is not None:
            try:
                cfg = config_from_dict(json.loads(mirrored.read_text(encoding="utf-8")))
                source = str(mirrored)
            except (OSError, ValueError):
                cfg = None
        else:
            cfg = None
        if cfg is None:
            try:
                cfg, source = load_config_json(model)
            except (FileNotFoundError, ValueError) as exc:
                raise HTTPException(
                    status_code=400,
                    detail=f"{exc} -- paste the model's config.json instead when the repo is unreachable",
                ) from None
            except Exception as exc:  # hub fetch failures etc.
                raise HTTPException(
                    status_code=400,
                    detail=f"could not load config for {model!r}: {type(exc).__name__} -- paste config.json instead",
                ) from None
            else:
                # successful hub/cache fetch: mirror it onto the work dir so the
                # config survives pod restarts and egress loss (best-effort)
                try:
                    mirror_dir = _work_dir() / "hf-configs"
                    mirror_dir.mkdir(parents=True, exist_ok=True)
                    (mirror_dir / (model.replace("/", "__") + ".json")).write_text(
                        json.dumps(cfg.raw), encoding="utf-8"
                    )
                except OSError:
                    pass
    else:
        raise HTTPException(status_code=400, detail="model or config_json is required")

    assert cfg is not None
    if not cfg.num_hidden_layers or not cfg.hidden_size:
        raise HTTPException(
            status_code=400,
            detail="config has no hidden_size/num_hidden_layers -- is this a config.json?",
        )

    gpu = _resolve_gpu(payload)
    gpu_count = _int_field(payload, "gpus", 1, 1, 64)
    tp = _int_field(payload, "tp", gpu_count, 1, gpu_count)
    pp = _int_field(payload, "pp", 1, 1, gpu_count)
    ep = _int_field(payload, "ep", 1, 1, gpu_count)
    mem_fraction_raw = payload.get("mem_fraction")
    overhead_raw = payload.get("overhead")
    try:
        mem_fraction = float(mem_fraction_raw) if mem_fraction_raw not in (None, "") else 0.9
        overhead = float(overhead_raw) if overhead_raw is not None else DEFAULT_OVERHEAD_GIB
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="mem_fraction/overhead must be numbers") from None
    if not 0.1 <= mem_fraction <= 1.0:
        raise HTTPException(status_code=400, detail="mem_fraction must be 0.1..1.0")
    if not 0 <= overhead <= 64:
        raise HTTPException(status_code=400, detail="overhead must be 0..64 GiB")

    # MoE runner + speculative shape (the deployment's actual launch flags;
    # the catalog dropdown pre-fills them from the entry's launch arguments)
    moe_runner = str(payload.get("moe_runner") or "").strip() or None
    legacy_speculative = payload.get("speculative")
    spec: SpeculativeSpec | None = None
    spec_payload = payload.get("speculative")
    if isinstance(spec_payload, dict):
        spec = SpeculativeSpec(
            algorithm=str(spec_payload.get("algorithm") or "").__str__().upper(),
            draft_tokens=int(spec_payload.get("draft_tokens") or 0),
            draft_model=str(spec_payload.get("draft_model") or "").strip(),
            layers=int(spec_payload.get("layers") or 0),
        )
    elif legacy_speculative not in (None, ""):
        # legacy integer form: MTP-family draft layers
        spec = SpeculativeSpec(algorithm="MTP", layers=int(legacy_speculative))
    hicache = _hicache_from_payload(payload)
    kv_dtype = payload.get("kv_dtype") or None
    weight_dtype = resolve_dtype_weight(cfg, payload.get("weight_dtype") or None)
    contexts_raw = payload.get("contexts")
    try:
        contexts = sorted({int(c) for c in contexts_raw}) if contexts_raw else list(DEFAULT_CONTEXTS)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="contexts must be integers") from None
    if not contexts or any(c < 256 or c > 4_000_000 for c in contexts) or len(contexts) > 12:
        raise HTTPException(status_code=400, detail="contexts: 1..12 values between 256 and 4000000")
    grid = bool(payload.get("grid"))

    # Speculative flip (built-in MTP module): derive the layer count from the
    # model's own config when the caller didn't specify one. DSPARK is
    # DeepSeek's name for the checkpoint-resident MTP module; MTP/NEXTN are
    # the same mechanism in other families. DFLASH (separate draft model) and
    # explicit layers pass through untouched. EAGLE's draft KV is NOT
    # auto-derived (draft-head shape is engine-specific) -- noted, not guessed.
    spec_notes: list[str] = []
    if (
        spec is not None
        and spec.algorithm in ("", "DSPARK", "MTP", "NEXTN")
        and spec.layers == 0
        and not spec.draft_model
    ):
        builtin = cfg.num_nextn_predict_layers or 0
        if builtin:
            spec.layers = builtin
            if not spec.algorithm:
                spec.algorithm = "DSPARK" if (cfg.model_type or "").startswith("deepseek") else "MTP"
            spec_notes.append(
                f"speculative {spec.algorithm}: using the model's built-in MTP module "
                f"({builtin} layer(s) from config.json)"
            )
        else:
            spec_notes.append(
                "speculative flip is ON but the model has no built-in MTP module "
                "(config.num_nextn_predict_layers absent) and no draft model was given -- "
                "no speculative KV counted"
            )
    elif spec is not None and spec.algorithm == "EAGLE" and spec.layers == 0 and not spec.draft_model:
        spec_notes.append(
            "EAGLE draft KV not auto-derived (draft-head shape is engine-specific) -- "
            "set the layer count explicitly for an exact number"
        )

    def _est(g: int, t: int) -> EstimateResult:
        return run_estimate(
            EstimateRequest(
                cfg=cfg,  # type: ignore[arg-type]
                gpu=gpu,
                gpu_count=g,
                tp_size=t,
                pp_size=pp,
                weight_dtype=weight_dtype,
                kv_dtype=kv_dtype,
                mem_fraction=mem_fraction,
                overhead_gib=overhead,
                context=contexts[-1],
                speculative=spec,
                hicache=hicache,
                moe_runner=moe_runner,
                ep_size=ep,
            )
        )

    res = _est(gpu_count, tp)

    scenarios: list[dict] = []
    if grid:
        tps = sorted({t for t in (1, 2, 4, 8, 16) if t <= gpu_count} | {tp})
        counts = sorted({gpu_count} | {int(g) for g in (payload.get("grid_gpus") or []) if int(g) >= 1})
        for g in counts:
            for t in [x for x in tps if x <= g]:
                r = _est(g, t)
                scenarios.append({"label": f"{gpu.name} x{g} TP{t}", "verdict": summary_line(r), "fits": r.fits})

    kvd = res.details["kv"]
    wd = res.details["weights"]
    params_b = (wd.get("params") or 0) / 1e9
    kv_label = dtype_label(kv_dtype) if kv_dtype else "bf16 (auto)"
    structure = [
        ["model_type", cfg.model_type or "?"],
        ["parameters", f"{params_b:.1f}B" if params_b else "unknown (config incomplete)"],
        [
            "weights",
            (
                f"{res.weights_bytes / 1024**3:.1f} GiB @ {dtype_label(weight_dtype) or '?'}"
                if res.weights_bytes
                else "unknown (dtype?)"
            ),
        ],
        ["attention", str(kvd.get("arch", "?"))],
        ["kv formula", str(kvd.get("formula", "?"))],
        ["kv per token", f"{res.kv_bytes_per_token / 1024:.1f} KiB @ {kv_label}"],
        ["layers", str(kvd.get("layer_mix").label()) if kvd.get("layer_mix") else "?"],
        ["config source", source],
    ]

    def _m_tokens(v: float | None) -> str:
        return f"{v / 1e6:.2f}M" if v is not None else "-"

    tiers = res.tiers
    cache_tiers: dict = {}
    if hicache.active:
        layout_map = {
            None: "auto (per model structure)",
            True: "replicated per TP rank",
            False: "sharded across TP ranks",
        }
        cache_tiers = {
            "device": {
                "tokens": res.kv_tokens_total if res.fits else None,
                "tokens_label": _m_tokens(res.kv_tokens_total if res.fits else None),
                "pool_gib_per_replica": (
                    round((res.kv_pool_per_gpu or 0) * min(gpu_count, tp) / 1024**3, 1) if res.kv_pool_per_gpu else 0
                ),
            },
            "l2": {
                "mode": hicache.l2_mode,
                "gib_per_replica": round(tiers.l2_bytes_per_replica / 1024**3, 1) if tiers.l2_bytes_per_replica else 0,
                "tokens": tiers.l2_tokens,
                "tokens_label": _m_tokens(tiers.l2_tokens),
                "layout": layout_map.get(hicache.l2_tp_replicated, layout_map[None]),
            },
            "l3": {
                "requested_gib": hicache.l3_gib,
                "gib_per_replica": round(tiers.l3_bytes_per_replica / 1024**3, 1) if tiers.l3_bytes_per_replica else 0,
                "tokens": tiers.l3_tokens,
                "tokens_label": _m_tokens(tiers.l3_tokens),
            },
            "total_tokens": tiers.total_tokens,
            "total_tokens_label": _m_tokens(tiers.total_tokens),
            "notes": tiers.notes,
        }

    primary_label = f"{gpu.name} x{gpu_count} TP{tp}" + (f" PP{pp}" if pp > 1 else "")
    grid_rows = [
        {
            "label": primary_label,
            "kv": (f"{res.kv_tokens_total / 1e6:.2f}M" if (res.fits and res.kv_tokens_total) else "-"),
            "cells": [
                (int(res.kv_tokens_total // c) if (res.fits and res.kv_tokens_total) else None) for c in contexts
            ],
        }
    ]

    status = "ok" if res.fits else ("does not fit" if res.fits is False else "unknown weights size")
    if res.fits and res.kv_tokens_total is not None:
        status += (
            f" | KV pool {res.kv_tokens_total / 1e6:.2f}M tokens | {res.concurrency or 0} x {contexts[-1] // 1024}k"
        )

    return {
        "model": model.rsplit("/", 1)[-1] if model else "(pasted config)",
        "status": status,
        "config": [
            ["model", model or "(pasted config)"],
            ["gpu", f"{gpu.name} x{gpu_count} (TP{tp}" + (f", PP{pp}" if pp > 1 else "") + ")"],
            ["weight dtype", dtype_label(weight_dtype) or "?"],
            ["kv dtype", kv_label],
            ["mem fraction", f"{mem_fraction:g}"],
            *(["expert parallel", str(ep)] if ep > 1 else []),
            ["overhead", f"{overhead:g} GiB/GPU"],
            *(
                [
                    [
                        "hicache L2",
                        f"ratio {hicache.l2_ratio:g}x pool"
                        if hicache.l2_mode == "ratio"
                        else f"{hicache.l2_gib:g} GiB RAM",
                    ],
                    *([["hicache L3", f"{hicache.l3_gib:g} GiB backing tier"]] if hicache.l3_gib else []),
                ]
                if hicache.active
                else []
            ),
            ["max context", f"{contexts[-1]:,}"],
        ],
        "structure": structure,
        "cache_tiers": cache_tiers,
        "scenarios": scenarios,
        "grid": {"contexts": contexts, "rows": grid_rows},
        "warnings": [*spec_notes, *res.warnings],
        "fits": res.fits,
        "kv_tokens_total": res.kv_tokens_total,
        "concurrency": res.concurrency,
        "gpus": _GPU_CHOICES,
        # estimator-form introspection: does THIS model ship a built-in MTP module?
        "builtin_mtp_layers": cfg.num_nextn_predict_layers or 0,
        # compressed-attention bounds (when the family carries an indexer):
        # the headline prices the indexer conservatively; this floor is the
        # engine-dependent optimistic bound
        "kv_per_token_optimistic": (
            res.details["kv"].get("per_layer_optimistic", 0) * res.details["kv"]["layer_mix"].kv_layers
            if res.details["kv"].get("per_layer_optimistic")
            else None
        ),
        "speculative_applied": spec if isinstance(spec, dict) or spec is None else None,
    }
