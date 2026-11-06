"""Live endpoint metrics: the throughput/load panel's collector.

WHY these tests exist: the panel reads the inference server's OWN Prometheus
metrics, so the two things that can silently lie are (1) the payload parser --
a mis-parse shows a busy fleet as idle -- and (2) the pod -> pool join, which
decides whether a row is attributed to a model or shown as unmapped. Both are
pure functions, so they are pinned directly; the collector's merge is pinned
with a fake tool result.

The window and the query strings are pinned too: the queries are the contract
with vLLM's metric names, and a typo there returns an empty vector rather than
an error, which would read as "the fleet is idle".
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from pcai_llm.core import endpoint_metrics as em


def _tool_result(payload: dict[str, Any]) -> Any:
    """The shape mcp returns: content blocks carrying JSON text."""

    class _Text:
        def __init__(self, text: str) -> None:
            self.text = text

    class _Result:
        def __init__(self, text: str) -> None:
            self.content = [_Text(text)]
            self.isError = False

    return _Result(json.dumps(payload))


def _vector(*rows: tuple[dict[str, Any], float]) -> dict[str, Any]:
    return {
        "status": "success",
        "data": {
            "resultType": "vector",
            "result": [{"metric": m, "value": [1730000000, str(v)]} for m, v in rows],
        },
    }


# --- prom_vector ----------------------------------------------------------


def test_prom_vector_reads_a_vector() -> None:
    got = em.prom_vector(_tool_result(_vector(({"pod": "a"}, 1.5), ({"pod": "b"}, 2.0))))
    assert got == [({"pod": "a"}, 1.5), ({"pod": "b"}, 2.0)]


def test_prom_vector_empty_result_is_empty_not_zero() -> None:
    """An OK answer with no series is NO DATA -- never a zero.

    This is the same trap the off-peak evaluator hit: the envelope carries
    numbers (elapsed_ms), and reading one of them as a sample made a busy
    fleet look idle.
    """
    assert em.prom_vector(_tool_result(_vector())) == []


def test_prom_vector_ignores_errors_and_junk() -> None:
    class _Text:
        def __init__(self, text: str) -> None:
            self.text = text

    class _Result:
        def __init__(self, *texts: str) -> None:
            self.content = [_Text(t) for t in texts]
            self.isError = False

    assert em.prom_vector(_Result("Prometheus returned HTTP 400")) == []
    assert em.prom_vector(_Result('{"status":"error","error":"bad query"}')) == []
    assert em.prom_vector(_Result('{"status":"success","data":{"result":"nope"}}')) == []
    assert em.prom_vector(None) == []


# --- the pod -> pool join -------------------------------------------------


def test_model_key_collapses_paths_and_pool_names() -> None:
    assert em._model_key("/mnt/models/models--Qwen--Qwen3-VL-Reranker-8B/snapshots/abc") == "qwen3vlreranker8b"
    assert em._model_key("Qwen3-VL-Reranker-8B") == "qwen3vlreranker8b"
    assert em._model_key("Qwen/Qwen3.8-27B") == "qwen3827b"


def test_attribution_prefers_the_endpoint_address() -> None:
    """An operator who registered by IP gets an exact join, org prefix or not."""
    row = {"instance": "10.0.3.41:8000", "model_name": ""}
    pools = {"deepseek": ["http://10.0.3.41:8000/v1"]}
    assert em._attribute(row, pools) == "deepseek"


def test_attribution_falls_back_to_the_model_key() -> None:
    row = {"instance": "10.0.9.9:8000", "model_name": "/mnt/models/models--Qwen--Qwen3-VL-Reranker-8B/snapshots/x"}
    pools = {"Qwen3-VL-Reranker-8B": ["http://svc.ns.svc.cluster.local/v1"]}
    assert em._attribute(row, pools) == "Qwen3-VL-Reranker-8B"


def test_attribution_is_empty_when_nothing_matches() -> None:
    """Unmatched pods must SURVIVE, not vanish: a silently dropped row is how
    you conclude a busy pool is idle."""
    assert em._attribute({"instance": "10.0.0.1:1", "model_name": "/x/models--Foo--Bar/x"}, {"other": []}) == ""
    assert em._attribute({"instance": "", "model_name": ""}, None) == ""


# --- snapshot merge -------------------------------------------------------


def test_snapshot_merges_queries_into_one_row_per_pod(monkeypatch: pytest.MonkeyPatch) -> None:
    pod = {"namespace": "ns", "pod": "p1", "model_name": "/mnt/models/models--Qwen--Qwen3-VL-Reranker-8B/s"}
    per_query = {
        "prefill_tps": _vector((pod, 120.0)),
        "decode_tps": _vector((pod, 480.0)),
        "running": _vector((pod, 3.0)),
        "waiting": _vector((pod, 1.0)),
        "kv_cache_pct": _vector(({k: v for k, v in pod.items() if k != "model_name"}, 42.0)),
        "prefill_s": _vector(({k: v for k, v in pod.items() if k != "model_name"}, 30.0)),
        "decode_s": _vector(({k: v for k, v in pod.items() if k != "model_name"}, 10.0)),
    }

    async def _target() -> tuple[str, dict[str, str]]:
        return "http://prom.test/mcp", {}

    async def _call(url: str, headers: Any, tool: str, args: dict[str, Any], timeout: float) -> Any:
        name = next(n for n, q in em.QUERIES.items() if q.replace("{window}", "300s") == args["query"])
        return _tool_result(per_query[name])

    import pcai_llm.core.offpeak as op

    monkeypatch.setattr(op, "prometheus_target", _target)
    monkeypatch.setattr(op, "_call_tool", _call)

    import asyncio

    snap = asyncio.run(em.snapshot(pools={"Qwen3-VL-Reranker-8B": ["http://svc/v1"]}))
    assert snap["available"] is True
    assert len(snap["endpoints"]) == 1
    row = snap["endpoints"][0]
    assert row["pod"] == "p1"
    assert row["prefill_tps"] == 120.0 and row["decode_tps"] == 480.0
    assert row["running"] == 3.0 and row["waiting"] == 1.0
    assert row["kv_cache_pct"] == 42.0
    # prefill share = prefill time / (prefill + decode) = 30/40
    assert row["prefill_share"] == 0.75
    assert row["pool"] == "Qwen3-VL-Reranker-8B"
    assert row["idle"] is False


def test_snapshot_marks_an_untouched_endpoint_idle(monkeypatch: pytest.MonkeyPatch) -> None:
    """0 t/s on a flat counter is IDLE, not broken -- and the panel says so."""
    pod = {"namespace": "ns", "pod": "p1", "model_name": ""}
    per_query = {name: _vector((pod, 0.0)) for name in em.QUERIES}

    async def _target() -> tuple[str, dict[str, str]]:
        return "http://prom.test/mcp", {}

    async def _call(url: str, headers: Any, tool: str, args: dict[str, Any], timeout: float) -> Any:
        name = next(n for n, q in em.QUERIES.items() if q.replace("{window}", "300s") == args["query"])
        return _tool_result(per_query[name])

    import pcai_llm.core.offpeak as op

    monkeypatch.setattr(op, "prometheus_target", _target)
    monkeypatch.setattr(op, "_call_tool", _call)

    import asyncio

    snap = asyncio.run(em.snapshot())
    assert snap["endpoints"][0]["idle"] is True
    # no request time in the window -> no share claimed (None, not 0.0)
    assert snap["endpoints"][0]["prefill_share"] is None


def test_snapshot_never_raises_when_prometheus_is_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    import pcai_llm.core.offpeak as op

    async def _target() -> tuple[str, dict[str, str]]:
        raise LookupError("MCP server 'prometheus' is not in the enabled registry")

    monkeypatch.setattr(op, "prometheus_target", _target)

    import asyncio

    snap = asyncio.run(em.snapshot())
    assert snap["available"] is False
    assert snap["endpoints"] == []
    assert "not in the enabled registry" in snap["error"]


def test_prom_vector_reads_the_RENDERED_TABLE() -> None:
    """The shape this fleet's prometheus MCP tool ACTUALLY returns.

    Verified against the live server on 2026-10-30: it renders one series per
    line, it does not hand back the Prometheus JSON envelope. A JSON-only
    parser returns [] for every query, and the panel then says "no endpoints
    reporting" -- a silent lie that reads exactly like an idle fleet.
    """
    text = (
        "  {model_name=/mnt/models/models--Qwen--Qwen3-VL-Reranker-8B/snapshots/abc,"
        "namespace=project-user, pod=reranker-predictor-00001} = 4.4983 (at 1793369254.388)\n"
        "  {namespace=project-user, pod=other-pod} = 0 (at 1793369254.388)\n"
    )

    class _Text:
        def __init__(self, t: str) -> None:
            self.text = t

    class _Result:
        def __init__(self) -> None:
            self.content = [_Text(text)]
            self.isError = False

    got = em.prom_vector(_Result())
    assert len(got) == 2
    labels, value = got[0]
    assert value == pytest.approx(4.4983)
    assert labels["pod"] == "reranker-predictor-00001"
    assert labels["namespace"] == "project-user"
    # the model path survives intact -- it is the join key
    assert labels["model_name"].endswith("snapshots/abc")
    assert got[1] == ({"namespace": "project-user", "pod": "other-pod"}, 0.0)


def test_prom_vector_skips_non_finite_and_error_rows() -> None:
    class _Text:
        def __init__(self, t: str) -> None:
            self.text = t

    class _Result:
        def __init__(self) -> None:
            self.content = [_Text("  {pod=a} = NaN (at 1)\n  {pod=b} = +Inf (at 1)\n  {pod=c} = 2.5 (at 1)\n")]
            self.isError = False

    got = em.prom_vector(_Result())
    assert [labels["pod"] for labels, _ in got] == ["c"]


def test_snapshot_end_to_end_over_the_rendered_table(monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole merge, fed the shape the real tool returns."""
    per_query = {
        "prefill_tps": "  {namespace=ns, pod=p1, model_name=/mnt/models/models--Qwen--Qwen3-VL-Reranker-8B/s} = 120 (at 1)\n",
        "decode_tps": "  {namespace=ns, pod=p1, model_name=/mnt/models/models--Qwen--Qwen3-VL-Reranker-8B/s} = 480 (at 1)\n",
        "running": "  {namespace=ns, pod=p1, model_name=/mnt/models/models--Qwen--Qwen3-VL-Reranker-8B/s} = 3 (at 1)\n",
        "waiting": "  {namespace=ns, pod=p1, model_name=/mnt/models/models--Qwen--Qwen3-VL-Reranker-8B/s} = 1 (at 1)\n",
        "kv_cache_pct": "  {namespace=ns, pod=p1} = 42 (at 1)\n",
        "prefill_s": "  {namespace=ns, pod=p1} = 30 (at 1)\n",
        "decode_s": "  {namespace=ns, pod=p1} = 10 (at 1)\n",
    }

    class _Text:
        def __init__(self, t: str) -> None:
            self.text = t

    class _Result:
        def __init__(self, t: str) -> None:
            self.content = [_Text(t)]
            self.isError = False

    import pcai_llm.core.offpeak as op

    async def _target() -> tuple[str, dict[str, str]]:
        return "http://prom.test/mcp", {}

    async def _call(url: str, headers: Any, tool: str, args: dict[str, Any], timeout: float) -> Any:
        name = next(n for n, q in em.QUERIES.items() if q.replace("{window}", "300s") == args["query"])
        return _Result(per_query[name])

    monkeypatch.setattr(op, "prometheus_target", _target)
    monkeypatch.setattr(op, "_call_tool", _call)

    import asyncio

    snap = asyncio.run(em.snapshot(pools={"Qwen3-VL-Reranker-8B": ["http://svc/v1"]}))
    row = snap["endpoints"][0]
    assert row["pool"] == "Qwen3-VL-Reranker-8B"
    assert row["decode_tps"] == 480.0 and row["kv_cache_pct"] == 42.0
    assert row["prefill_share"] == 0.75


def test_queries_cover_both_engines() -> None:
    """This fleet runs vLLM (vision/audio/embedding) AND SGLang (the CHAT
    models). A vLLM-only selector produced a panel with every chat model
    missing -- the models the panel exists for -- while still looking healthy.
    """
    for key in ("prefill_tps", "decode_tps", "running", "waiting", "kv_cache_pct"):
        q = em.QUERIES[key]
        assert "vllm:" in q and "sglang:" in q, f"{key} must cover both engines"
    # the two engines name the same concept differently; both must be present
    assert "vllm:prompt_tokens_total|sglang:prompt_tokens_total" in em.QUERIES["prefill_tps"]
    assert "vllm:generation_tokens_total|sglang:generation_tokens_total" in em.QUERIES["decode_tps"]
    assert "vllm:num_requests_running|sglang:num_running_reqs" in em.QUERIES["running"]
    assert "vllm:num_requests_waiting|sglang:num_queue_reqs" in em.QUERIES["waiting"]
    assert "vllm:kv_cache_usage_perc|sglang:token_usage" in em.QUERIES["kv_cache_pct"]
    # the SGLang counter is split by is_streaming; summing by pod collapses it
    assert "sum by (namespace, pod, instance, model_name)" in em.QUERIES["prefill_tps"]


def test_attribution_matches_sglang_model_names() -> None:
    """SGLang reports 'Qwen/Qwen3.8-27B-FP8', 'glm-5.3-flash',
    'deepseek-v4.1-flash' -- org prefix and quantisation suffix included."""
    pools = {
        "Qwen3.8-27B": ["http://svc-a/v1"],
        "GLM-5.3-Flash": ["http://svc-b/v1"],
        "Deepseek-V4.1-Flash-1017": ["http://svc-c/v1"],
    }
    for model_name, expected in (
        ("Qwen/Qwen3.8-27B-FP8", "Qwen3.8-27B"),
        ("glm-5.3-flash", "GLM-5.3-Flash"),
        ("deepseek-v4.1-flash", "Deepseek-V4.1-Flash-1017"),
    ):
        row = {"instance": "", "model_name": model_name}
        assert em._attribute(row, pools) == expected, model_name


def test_sglang_row_has_no_prefill_share(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prefill/decode TIME histograms are vLLM-only; SGLang rows must say
    nothing rather than invent a ratio from token counts."""
    pod = {"namespace": "ns", "pod": "glm-53-flash-predictor-00001", "model_name": "glm-5.3-flash"}
    per_query = {
        "prefill_tps": _vector((pod, 90.0)),
        "decode_tps": _vector((pod, 310.0)),
        "running": _vector((pod, 2.0)),
        "waiting": _vector((pod, 0.0)),
        "kv_cache_pct": _vector(({k: v for k, v in pod.items() if k != "model_name"}, 12.5)),
        "prefill_s": _vector(),
        "decode_s": _vector(),
    }

    class _Text:
        def __init__(self, t: str) -> None:
            self.text = t

    class _Result:
        def __init__(self, t: str) -> None:
            self.content = [_Text(t)]
            self.isError = False

    import json as _json

    import pcai_llm.core.offpeak as op

    async def _target() -> tuple[str, dict[str, str]]:
        return "http://prom.test/mcp", {}

    async def _call(url: str, headers: Any, tool: str, args: dict[str, Any], timeout: float) -> Any:
        name = next(n for n, q in em.QUERIES.items() if q.replace("{window}", "300s") == args["query"])
        return _Result(_json.dumps(per_query[name]))

    monkeypatch.setattr(op, "prometheus_target", _target)
    monkeypatch.setattr(op, "_call_tool", _call)

    import asyncio

    row = asyncio.run(em.snapshot(pools={"GLM-5.3-Flash": ["http://svc-b/v1"]}))["endpoints"][0]
    assert row["pool"] == "GLM-5.3-Flash"
    assert row["decode_tps"] == 310.0 and row["kv_cache_pct"] == 12.5
    assert row["prefill_share"] is None


def test_identity_merges_when_a_family_lacks_the_pod_label() -> None:
    """SGLang's label set differs from vLLM's. If one family carries pod and
    another only instance, they must still merge into ONE row -- otherwise the
    panel shows the same endpoint twice, half of it blank."""
    assert em._series_key({"namespace": "ns", "pod": "p1", "instance": "10.0.0.1:8000"}) == "ns/p1"
    assert em._series_key({"namespace": "ns", "instance": "10.0.0.1:8000"}) == "ns/10.0.0.1:8000"
    assert em._series_key({"namespace": "ns", "pod": "", "instance": "10.0.0.1:8000"}) == "ns/10.0.0.1:8000"
    # and every query groups by BOTH, so the merge key is consistent by shape
    for key in ("prefill_tps", "decode_tps", "running", "waiting"):
        assert "namespace, pod, instance, model_name" in em.QUERIES[key]
    for key in ("kv_cache_pct", "prefill_s", "decode_s"):
        assert "namespace, pod, instance" in em.QUERIES[key]


def test_cache_hit_query_exists_for_sglang() -> None:
    """prompt_tokens_total counts CACHED prompt tokens too.

    Measured on this fleet (2026-10-30), one prefill batch was
    "#new-token: 1682, #cached-token: 35840" -- 96% cached -- while the
    counter reported the full 37 522. Without a cache-hit reading the panel
    shows an impossible amount of prefill work.
    """
    q = em.QUERIES["cache_hit_pct"]
    assert "sglang:cache_hit_rate" in q
    assert "* 100" in q


def test_used_tokens_is_collected_for_the_kv_tooltip() -> None:
    """A percentage of a ~12M-token pool reads 0.98% whether that is three
    conversations or three hundred, so the absolute rides alongside."""
    q = em.QUERIES["used_tokens"]
    assert "sglang:num_used_tokens" in q
