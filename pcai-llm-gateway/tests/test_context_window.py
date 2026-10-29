"""Context-window discovery + the admission inequality (2026-10-29).

LIVE DEFECT this pins: G2 clamps `Qwen3.8-27B` to 196608 against the model's
262144 window, so the ceiling silently assumed a prompt <= 65536 tokens. A DSH
agent turn on the same gateway is 81515-92912 prompt tokens (the MCP tool
catalog dominates it), so every Qwen-served turn computed
`196608 + ~85000 > 262144` and SGLang rejected it with a 400 BEFORE sampling —
which the relay then rendered as assistant text "(model error 400)" with HTTP
200, i.e. a completed turn whose answer was an error string.

Contract under test:
  * the window is LEARNED from the upstream's own /v1/models card, and an
    unknown window is "no opinion" — never a guess, never a routing veto;
  * the cap solves `prompt + max_tokens <= window` per REQUEST, and still
    honours the operator ceiling as a cost policy (the tighter one wins);
  * the estimator that feeds it COUNTS THE TOOL CATALOG, unlike the routing
    complexity score which deliberately excludes it — that exclusion is
    exactly how the 65536 assumption died;
  * an image costs a flat charge, never its base64 length;
  * a ladder candidate that cannot hold the prompt is dropped, so the decision
    model is never offered a model that will 400;
  * /v1/models advertises `context_length` / `max_tokens` (the names a client's
    model discovery reads), sized by the SMALLEST candidate for an alias.
"""

from __future__ import annotations

from typing import Any

import pytest

from pcai_llm.api import routes_proxy
from pcai_llm.config import clamp_max_tokens
from pcai_llm.core import context_window
from pcai_llm.routing.router import Endpoint, Pool

QWEN_WINDOW = 262144
QWEN_CEILING = 196608
DSH_PROMPT_CHARS = 340_000  # ~85000 tokens at len//4 — the measured agent turn


@pytest.fixture(autouse=True)
def _clean_windows() -> Any:
    """Every test starts with nothing learned — production learns and keeps."""
    context_window.forget_all()
    yield
    context_window.forget_all()


def _body(text_chars: int = 0, *, tools: int = 0, images: int = 0, max_tokens: int | None = None) -> dict[str, Any]:
    content: Any = "x" * text_chars
    if images:
        content = [{"type": "text", "text": "x" * text_chars}]
        content += [
            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "A" * 400_000}} for _ in range(images)
        ]
    body: dict[str, Any] = {"model": "m", "messages": [{"role": "user", "content": content}]}
    if tools:
        body["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": f"tool_{i}",
                    "description": "does a thing " * 40,
                    "parameters": {"type": "object", "properties": {"a": {"type": "string"}}},
                },
            }
            for i in range(tools)
        ]
    if max_tokens is not None:
        body["max_tokens"] = max_tokens
    return body


# ---------------------------------------------------------------------------
# discovery
# ---------------------------------------------------------------------------


def test_read_card_data_array() -> None:
    assert context_window.read_card({"data": [{"id": "q", "max_model_len": QWEN_WINDOW}]}) == QWEN_WINDOW


def test_read_card_models_map() -> None:
    assert context_window.read_card({"models": {"q": {"max_model_len": 4096}}}) == 4096


def test_read_card_takes_the_largest_sibling() -> None:
    """A pool is interchangeable servers; the biggest published window wins."""
    card = {"data": [{"max_model_len": 8192}, {"max_model_len": QWEN_WINDOW}, {"max_model_len": 1024}]}
    assert context_window.read_card(card) == QWEN_WINDOW


@pytest.mark.parametrize(
    "card",
    [
        None,
        {},
        {"data": []},
        {"data": [{"id": "q"}]},
        {"data": [{"max_model_len": True}]},
        {"data": [{"max_model_len": -1}]},
        {"data": [{"max_model_len": "262144"}]},
        "nonsense",
    ],
)
def test_read_card_rejects_junk_without_raising(card: Any) -> None:
    assert context_window.read_card(card) is None


def test_remember_normalizes_the_url_and_rejects_bool() -> None:
    assert context_window.remember("http://up/v1/", QWEN_WINDOW) == QWEN_WINDOW
    assert context_window.cached("http://up/v1") == QWEN_WINDOW
    assert context_window.remember("http://other/v1", True) is None
    assert context_window.cached("http://other/v1") is None


def test_cached_unknown_is_none() -> None:
    assert context_window.cached(None) is None
    assert context_window.cached("http://never-probed/v1") is None


# ---------------------------------------------------------------------------
# the estimator — the half the routing score deliberately omits
# ---------------------------------------------------------------------------


def test_estimate_counts_the_tool_catalog() -> None:
    """THE load-bearing property: the catalog is the bulk of an agent prompt.

    model_routing.estimate_prompt_tokens excludes tools because a constant
    catalog cannot separate easy work from hard. For FITTING a window that
    exclusion is the bug: it would price an 85k-token DSH turn at ~25k.
    """
    bare = context_window.estimate_context_tokens(_body(4_000))
    with_tools = context_window.estimate_context_tokens(_body(4_000, tools=28))
    assert with_tools > bare * 5
    # Roughly the serialized size / 4 — never less than the text alone.
    assert with_tools - bare > 1_000


def test_estimate_charges_images_flat_not_by_base64_length() -> None:
    """A 400KB data URL must cost a flat charge, not ~100k "tokens".

    Same lesson as model_routing._IMAGE_TOKEN_EQUIVALENT: pricing base64 length
    once pinned every vision call to the strongest model.
    """
    one = context_window.estimate_context_tokens(_body(100, images=1))
    none = context_window.estimate_context_tokens(_body(100))
    assert one - none == context_window.IMAGE_TOKEN_EQUIVALENT


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"messages": None},
        {"messages": [None, 1, "x"]},
        {"tools": "nope"},
        {"messages": [{"content": {"weird": True}}]},
    ],
)
def test_estimate_never_raises_on_junk(body: dict[str, Any]) -> None:
    assert context_window.estimate_context_tokens(body) >= 0


# ---------------------------------------------------------------------------
# the cap: an inequality, not a constant
# ---------------------------------------------------------------------------


def test_cap_solves_the_live_defect() -> None:
    """262144 requested, 196608 ceiling, 262144 window, ~85k prompt.

    The ceiling alone gives 196608 + 85000 > 262144 -> the 400 that produced
    "(model error 400)". The window term is what makes it fit.
    """
    estimate = context_window.estimate_context_tokens(_body(DSH_PROMPT_CHARS))
    cap = context_window.completion_cap(
        context_limit=QWEN_WINDOW, est_prompt=estimate, requested=262144, ceiling=QWEN_CEILING
    )
    assert cap is not None
    assert cap < QWEN_CEILING  # the window, not the ceiling, is binding here
    assert cap + estimate <= QWEN_WINDOW  # the invariant the upstream enforces


def test_cap_never_raises_a_smaller_client_value() -> None:
    assert (
        context_window.completion_cap(
            context_limit=QWEN_WINDOW, est_prompt=1_000, requested=4_096, ceiling=QWEN_CEILING
        )
        == 4_096
    )


def test_cap_keeps_the_operator_ceiling_as_a_cost_policy() -> None:
    """A 1M-window model still honours a deliberately small ceiling."""
    assert (
        context_window.completion_cap(context_limit=1_048_576, est_prompt=1_000, requested=262_144, ceiling=8_192)
        == 8_192
    )


def test_cap_is_no_opinion_when_the_prompt_alone_overflows() -> None:
    """No completion cap can rescue a prompt bigger than the window."""
    assert context_window.completion_cap(context_limit=8_192, est_prompt=9_000, requested=4_096, ceiling=None) is None


def test_cap_is_no_opinion_when_the_window_is_unknown() -> None:
    assert (
        context_window.completion_cap(context_limit=None, est_prompt=85_000, requested=262_144, ceiling=QWEN_CEILING)
        == QWEN_CEILING
    )


# ---------------------------------------------------------------------------
# clamp_max_tokens — the single seam every forward site calls
# ---------------------------------------------------------------------------


def test_clamp_applies_the_window_with_no_ceiling_configured() -> None:
    """The operator should not have to configure anything at all."""
    body = _body(DSH_PROMPT_CHARS, max_tokens=262_144)
    out = clamp_max_tokens(body, "Qwen3.8-27B", {}, context_limit=QWEN_WINDOW)
    estimate = context_window.estimate_context_tokens(body)
    assert out["max_tokens"] < 262_144
    assert out["max_tokens"] + estimate <= QWEN_WINDOW
    assert body["max_tokens"] == 262_144  # caller's body never mutated


def test_clamp_takes_the_tighter_of_ceiling_and_window() -> None:
    body = _body(DSH_PROMPT_CHARS, max_tokens=262_144)
    out = clamp_max_tokens(body, "Qwen3.8-27B", {"Qwen3.8-27B": QWEN_CEILING}, context_limit=QWEN_WINDOW)
    estimate = context_window.estimate_context_tokens(body)
    assert out["max_tokens"] == min(QWEN_CEILING, QWEN_WINDOW - estimate - context_window.SAFETY_MARGIN_TOKENS)


def test_clamp_unknown_window_keeps_the_old_behaviour_byte_for_byte() -> None:
    body = _body(1_000, max_tokens=262_144)
    out = clamp_max_tokens(body, "Qwen3.8-27B", {"Qwen3.8-27B": QWEN_CEILING}, context_limit=None)
    assert out["max_tokens"] == QWEN_CEILING
    assert out is not body


def test_clamp_is_zero_copy_when_nothing_changes() -> None:
    body = _body(1_000, max_tokens=1_024)
    assert clamp_max_tokens(body, "m", {"m": QWEN_CEILING}, context_limit=QWEN_WINDOW) is body
    assert clamp_max_tokens(body, "m", {}, context_limit=None) is body


def test_clamp_handles_the_modern_cap_spelling() -> None:
    body = _body(DSH_PROMPT_CHARS)
    body["max_completion_tokens"] = 262_144
    out = clamp_max_tokens(body, "Qwen3.8-27B", {}, context_limit=QWEN_WINDOW)
    estimate = context_window.estimate_context_tokens(body)
    assert out["max_completion_tokens"] + estimate <= QWEN_WINDOW


# ---------------------------------------------------------------------------
# fit + the ladder gate
# ---------------------------------------------------------------------------


def test_fits_is_fail_open_on_an_unknown_window() -> None:
    assert context_window.fits(context_limit=None, est_prompt=10_000_000) is True


def test_fits_boundary() -> None:
    room = QWEN_WINDOW - 85_000 - context_window.SAFETY_MARGIN_TOKENS
    assert context_window.fits(context_limit=QWEN_WINDOW, est_prompt=85_000, min_completion=room) is True
    assert context_window.fits(context_limit=QWEN_WINDOW, est_prompt=85_000, min_completion=room + 1) is False


def _install_pool(monkeypatch: pytest.MonkeyPatch, name: str, base_url: str) -> None:
    pool = Pool(endpoints=[Endpoint(id=name, model_name=name, base_url=base_url)])
    real = routes_proxy.model_router.pool
    monkeypatch.setattr(
        routes_proxy.model_router, "pool", lambda m, _real=real, _n=name, _p=pool: _p if m == _n else _real(m)
    )


def test_gate_drops_a_model_whose_window_cannot_hold_the_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    """A prompt that does not fit the window AT ALL must leave the ladder.

    This is the case no completion cap can rescue: a session that grows past
    Qwen's 262144 on a ladder that also carries 1M-context models. Offering
    Qwen there is offering a guaranteed 400.
    """
    context_window.remember("http://qwen/v1", QWEN_WINDOW)
    _install_pool(monkeypatch, "Qwen3.8-27B", "http://qwen/v1")
    gated = routes_proxy._context_gated(lambda _m: True, _body(1_200_000))  # ~300k tokens
    assert gated("Qwen3.8-27B") is False
    assert gated("some-unknown-model") is True


def test_gate_does_not_prune_a_normal_agent_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 100k-token DSH turn FITS a 262144 window once the cap is solved.

    The gate must not "fix" the 400 by silently removing the cheap model from
    every agent request — that would be a routing decision made from a
    capability the model actually has.
    """
    context_window.remember("http://qwen/v1", QWEN_WINDOW)
    _install_pool(monkeypatch, "Qwen3.8-27B", "http://qwen/v1")
    gated = routes_proxy._context_gated(lambda _m: True, _body(400_000))
    assert gated("Qwen3.8-27B") is True


def test_gate_keeps_a_model_that_fits(monkeypatch: pytest.MonkeyPatch) -> None:
    context_window.remember("http://qwen/v1", QWEN_WINDOW)
    _install_pool(monkeypatch, "Qwen3.8-27B", "http://qwen/v1")
    gated = routes_proxy._context_gated(lambda _m: True, _body(4_000))
    assert gated("Qwen3.8-27B") is True


def test_gate_never_overrides_the_wrapped_predicate(monkeypatch: pytest.MonkeyPatch) -> None:
    context_window.remember("http://qwen/v1", QWEN_WINDOW)
    _install_pool(monkeypatch, "Qwen3.8-27B", "http://qwen/v1")
    gated = routes_proxy._context_gated(lambda _m: False, _body(4_000))
    assert gated("Qwen3.8-27B") is False


def test_pool_context_limit_takes_the_smallest_known_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    pool = Pool(
        endpoints=[
            Endpoint(id="a", model_name="m", base_url="http://big/v1"),
            Endpoint(id="b", model_name="m", base_url="http://small/v1"),
            Endpoint(id="c", model_name="m", base_url="http://unknown/v1"),
        ]
    )
    context_window.remember("http://big/v1", 1_048_576)
    context_window.remember("http://small/v1", QWEN_WINDOW)
    monkeypatch.setattr(routes_proxy.model_router, "pool", lambda _m, _p=pool: _p)
    assert routes_proxy._pool_context_limit("m") == QWEN_WINDOW


# ---------------------------------------------------------------------------
# advertisement — so a client need not hardcode what the server publishes
# ---------------------------------------------------------------------------


def test_advertised_caps_are_omitted_when_nothing_is_known() -> None:
    assert routes_proxy._advertised_caps(["nothing-registered"]) == {}


def test_advertised_caps_use_the_smallest_window(monkeypatch: pytest.MonkeyPatch) -> None:
    pools = {
        "big": Pool(endpoints=[Endpoint(id="1", model_name="big", base_url="http://big/v1")]),
        "small": Pool(endpoints=[Endpoint(id="2", model_name="small", base_url="http://small/v1")]),
    }
    context_window.remember("http://big/v1", 1_048_576)
    context_window.remember("http://small/v1", QWEN_WINDOW)
    monkeypatch.setattr(routes_proxy.model_router, "pool", lambda m: pools.get(m))
    # An alias may pick ANY of them, so it is sized by the smallest.
    assert routes_proxy._advertised_caps(["big", "small"]) == {
        "context_length": QWEN_WINDOW,
        "max_tokens": QWEN_WINDOW,
    }


# ---------------------------------------------------------------------------
# discovery pass — registry.refresh() owns it, not the request path
# ---------------------------------------------------------------------------


class _FakeProbeClient:
    """Minimal stand-in for the upstream client a probe pass builds."""

    def __init__(self, payload: dict[str, Any] | None = None, status: int = 200) -> None:
        self.payload = payload or {"data": [{"max_model_len": QWEN_WINDOW}]}
        self.status = status
        self.urls: list[str] = []
        self.closed = False

    async def get(self, url: str, headers: dict[str, str] | None = None, timeout: float | None = None) -> Any:
        self.urls.append(url)
        return _FakeResponse(self.status, self.payload)

    async def aclose(self) -> None:
        self.closed = True


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict[str, Any]) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict[str, Any]:
        return self._payload


async def test_refresh_windows_learns_unknown_and_skips_known(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeProbeClient()
    monkeypatch.setattr(context_window, "upstream_client", lambda **_kw: fake)
    context_window.remember("http://known/v1", 4096)
    considered = await context_window.refresh_windows(
        [("http://known/v1", {}), ("http://new/v1", {"Authorization": "Bearer x"})]
    )
    assert considered == 1  # the known one is not re-probed
    assert fake.urls == ["http://new/v1/models"]
    assert context_window.cached("http://new/v1") == QWEN_WINDOW
    assert fake.closed is True


async def test_refresh_windows_is_a_noop_in_the_steady_state(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeProbeClient()
    monkeypatch.setattr(context_window, "upstream_client", lambda **_kw: fake)
    await context_window.refresh_windows([("http://new/v1", {})])
    assert await context_window.refresh_windows([("http://new/v1", {})]) == 0
    assert fake.urls == ["http://new/v1/models"]  # exactly one probe


async def test_refresh_windows_backs_off_a_failing_upstream(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 401 or an unreachable host must not become a polling loop."""
    fake = _FakeProbeClient(status=401)
    monkeypatch.setattr(context_window, "upstream_client", lambda **_kw: fake)
    await context_window.refresh_windows([("http://broken/v1", {})])
    assert context_window.cached("http://broken/v1") is None
    await context_window.refresh_windows([("http://broken/v1", {})])
    assert fake.urls == ["http://broken/v1/models"]  # the retry window holds it off


async def test_refresh_windows_never_raises_on_a_broken_client(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Boom:
        async def get(self, *_a: Any, **_k: Any) -> Any:
            raise RuntimeError("upstream gone")

        async def aclose(self) -> None:
            raise RuntimeError("close failed too")

    monkeypatch.setattr(context_window, "upstream_client", lambda **_kw: _Boom())
    assert await context_window.refresh_windows([("http://x/v1", {})]) == 1
    assert context_window.cached("http://x/v1") is None


# ---------------------------------------------------------------------------
# retry sweep — a boot-time probe failure must not blind a pod forever
# ---------------------------------------------------------------------------


async def test_housekeeping_retries_only_unknown_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    """Live 3.4.5 defect: one replica learned all windows, the other only GLM's.

    Discovery ran once inside registry.refresh(), so a probe that missed at
    boot never retried and 3 of 8 forced-Qwen requests kept returning
    "(model error 400)" on that pod.
    """
    from pcai_llm.core import housekeeping

    known = Pool(endpoints=[Endpoint(id="k", model_name="known", base_url="http://known/v1")])
    blind = Pool(
        endpoints=[
            Endpoint(id="u1", model_name="blind", base_url="http://blind-a/v1"),
            Endpoint(id="u2", model_name="blind2", base_url="http://blind-b/v1"),
        ]
    )
    pools = {"known": known, "blind": blind}
    context_window.remember("http://known/v1", QWEN_WINDOW)
    monkeypatch.setattr(housekeeping.model_router, "models", lambda: ["known", "blind"])
    monkeypatch.setattr(housekeeping.model_router, "pool", lambda m: pools.get(m))
    monkeypatch.setattr(housekeeping.upstream_auth, "upstream_headers", lambda ep, _r="": {"X-Test": ep.id})

    seen: list[tuple[str, dict[str, str]]] = []

    async def fake_refresh(entries: list[tuple[str, dict[str, str]]]) -> int:
        seen.extend(entries)
        return len(entries)

    monkeypatch.setattr(housekeeping.context_window, "refresh_windows", fake_refresh)
    await housekeeping._context_window_sweep()

    # The known upstream is never re-probed; both blind ones are retried.
    assert [url for url, _ in seen] == ["http://blind-a/v1", "http://blind-b/v1"]
    assert seen[0][1] == {"X-Test": "u1"}


async def test_housekeeping_sweep_is_a_noop_when_everything_is_known(monkeypatch: pytest.MonkeyPatch) -> None:
    from pcai_llm.core import housekeeping

    pool = Pool(endpoints=[Endpoint(id="k", model_name="m", base_url="http://known/v1")])
    context_window.remember("http://known/v1", QWEN_WINDOW)
    monkeypatch.setattr(housekeeping.model_router, "models", lambda: ["m"])
    monkeypatch.setattr(housekeeping.model_router, "pool", lambda _m: pool)

    called = False

    async def fake_refresh(_entries: Any) -> int:
        nonlocal called
        called = True
        return 0

    monkeypatch.setattr(housekeeping.context_window, "refresh_windows", fake_refresh)
    await housekeeping._context_window_sweep()
    assert called is False  # zero I/O in the steady state


async def test_a_card_without_a_window_is_not_re_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    """The 3.4.6 sweep re-probed the decision runtime every minute, forever.

    vllm-sr publishes family/surfaces, never max_model_len, so "no window" is a
    FACT. Retrying it every cadence is a wasted upstream call and a log line per
    minute per pod.
    """
    fake = _FakeProbeClient(payload={"data": [{"id": "d3", "family": "vllm-sr"}]})
    monkeypatch.setattr(context_window, "upstream_client", lambda **_kw: fake)

    await context_window.refresh_windows([("http://decision/v1", {})])
    assert context_window.cached("http://decision/v1") is None
    assert fake.urls == ["http://decision/v1/models"]

    # A transient retry window does NOT re-ask it, unlike a real failure.
    assert await context_window.refresh_windows([("http://decision/v1", {})]) == 0
    assert fake.urls == ["http://decision/v1/models"]


async def test_a_failure_is_still_retried_after_the_short_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 401/timeout is 'we do not know', not 'it publishes nothing'."""
    fake = _FakeProbeClient(status=503)
    monkeypatch.setattr(context_window, "upstream_client", lambda **_kw: fake)
    await context_window.refresh_windows([("http://flaky/v1", {})])
    assert fake.urls == ["http://flaky/v1/models"]
    # Inside PROBE_RETRY_SECONDS: held off.
    await context_window.refresh_windows([("http://flaky/v1", {})])
    assert fake.urls == ["http://flaky/v1/models"]
    # Once the short backoff has elapsed, it is retried (unlike the no-window case).
    context_window._LAST_ATTEMPT["http://flaky/v1"] = 0.0
    await context_window.refresh_windows([("http://flaky/v1", {})])
    assert fake.urls == ["http://flaky/v1/models", "http://flaky/v1/models"]
