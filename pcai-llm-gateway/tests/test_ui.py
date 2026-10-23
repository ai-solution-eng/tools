"""Chat UI + end-to-end chat through the assembled app (mocked upstream)."""

from __future__ import annotations

import re
import uuid
from typing import Any

import httpx2
import pytest
from fastapi.testclient import TestClient

from pcai_llm.api import routes_proxy
from pcai_llm.api.auth import KeyRecord
from pcai_llm.core import budgets as budgets_mod
from pcai_llm.core import costs as costs_mod
from pcai_llm.core import embedcache
from pcai_llm.routing.router import Endpoint, Router

MODEL = "ui-test-model"


class FakeDB:
    async def fetch(self, sql: str, *a: Any):
        return []

    async def fetchrow(self, sql: str, *a: Any):
        if "profiles" in sql:
            return None
        return {"requests": 1, "tokens": 10}

    async def fetchval(self, sql: str, *a: Any):
        return None

    async def execute(self, sql: str, *a: Any):
        return "INSERT 0 1"


def _client(monkeypatch: pytest.MonkeyPatch, handler) -> TestClient:
    key = KeyRecord(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        name="k",
        key_prefix="hllm-UI",
        scopes=["proxy"],
        allowed_models=[],
        rate_limit_rpm=1000,
        rate_limit_tpm=1_000_000,
        max_concurrent=0,
        expires_at=None,
    )
    import pcai_llm.api.auth as auth_mod
    import pcai_llm.core.cascade as cascade_mod

    async def fake_auth(p: str) -> KeyRecord:
        return key

    monkeypatch.setattr(auth_mod, "authenticate", fake_auth)
    fake = FakeDB()
    monkeypatch.setattr(budgets_mod, "db", fake)
    monkeypatch.setattr(costs_mod, "db", fake)
    monkeypatch.setattr(cascade_mod, "db", fake)
    monkeypatch.setattr(embedcache, "db", fake)
    monkeypatch.setattr(routes_proxy, "db", fake)
    r = Router.with_mock(handler)
    r.set_endpoints(
        MODEL,
        [
            Endpoint(
                id="ep",
                model_name=MODEL,
                base_url="http://fake/v1",
            )
        ],
    )
    monkeypatch.setattr(routes_proxy, "model_router", r)

    from pcai_llm.app import create_app

    return TestClient(create_app(), raise_server_exceptions=False)


def test_ui_served_at_root_and_slash_ui() -> None:
    from pcai_llm.app import create_app

    tc = TestClient(create_app(), raise_server_exceptions=False)
    for path in ("/", "/ui"):
        r = tc.get(path)
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]
        assert "HPE" in r.text and "chat" in r.text.lower()


def test_ui_page_has_model_selector_and_key_input() -> None:
    from pcai_llm.app import create_app

    tc = TestClient(create_app(), raise_server_exceptions=False)
    html = tc.get("/").text
    assert 'id="model"' in html and 'id="apikey"' in html


def test_ui_tools_checkbox_is_one_label_unit() -> None:
    """User feedback: on the user page the tools/MCPs checkbox floated
    detached from its 'tools · MCPs' text (lone checkbox mid-header). Root
    cause: the header text-entry CSS — `header input { width: 260px }` — is
    a descendant selector that ALSO matched the checkbox, stretching it to
    260px and pushing its label text against the Admin link. The checkbox
    must be one <label> unit with its text, and no width rule may hit it."""
    import re

    from pcai_llm.app import create_app

    tc = TestClient(create_app(), raise_server_exceptions=False)
    html = tc.get("/").text
    # one visual unit: the checkbox lives inside its label, with for= wiring
    assert re.search(r'<label[^>]*for="tools"[^>]*>\s*<input type="checkbox" id="tools">', html), (
        "tools checkbox must sit inside its <label for=tools> unit"
    )
    # the text-entry width rules must exclude the checkbox (desktop + phones)
    # (260px → 170px, header-width wave 2026-10-09: masked key field narrowed
    # on user report — the pin's job is the checkbox EXCLUSION, not the pixel
    # count)
    assert 'header input:not([type="checkbox"]) { width: 170px; font-size: 12.5px; }' in html
    assert "header input:not([type=checkbox])" in html  # phones: 100% rule scoped too
    # header order unchanged: conn status left of the tools unit, Admin + theme right
    assert html.index('id="conn"') < html.index('for="tools"') < html.index('href="/admin"') < html.index('id="theme"')


def test_chat_stream_end_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
    """The exact flow the UI performs: bearer key → streaming chat → SSE."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        body = (
            b'data: {"model":"ui-test-model","choices":[{"delta":{"content":"Hel"}}]}\n\n'
            b'data: {"model":"ui-test-model","choices":[{"delta":{"content":"lo"}}]}\n\n'
            b'data: {"model":"ui-test-model","choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":3,"completion_tokens":2,"total_tokens":5}}\n\n'
            b"data: [DONE]\n\n"
        )
        return httpx2.Response(200, content=body, headers={"content-type": "text/event-stream"})

    tc = _client(monkeypatch, handler)
    with tc.stream(
        "POST",
        "/v1/chat/completions",
        json={"model": MODEL, "messages": [{"role": "user", "content": "hi"}], "stream": True},
        headers={"Authorization": "Bearer hllm-x"},
    ) as r:
        assert r.status_code == 200
        assert "text/event-stream" in r.headers["content-type"]
        body = b"".join(r.iter_bytes()).decode()
        assert "Hel" in body and "lo" in body and "[DONE]" in body


def test_chat_streaming_carries_budget_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    """warn level 95 → the SSE response headers carry X-Budget-Warning."""

    class WarnDB(FakeDB):
        async def fetch(self, sql: str, *a: Any):
            if "FROM budgets" in sql:
                # single aggregate query: budget row joined to window spend
                return [
                    {"model": "*", "period": "month", "limit_tokens": 100000, "spent": 97000}
                ]  # 97% — above 95, below 100
            if "FROM cost_budgets" in sql:
                return []
            return []

        async def fetchval(self, sql: str, *a: Any):
            if "SUM" in str(sql):
                return 97000  # 97% of 100k — above 95, below 100
            return None

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, content=b"data: [DONE]\n\n", headers={"content-type": "text/event-stream"})

    key = KeyRecord(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        name="k",
        key_prefix="hllm-UI",
        scopes=["proxy"],
        allowed_models=[],
        rate_limit_rpm=1000,
        rate_limit_tpm=1_000_000,
        max_concurrent=0,
        expires_at=None,
    )
    import pcai_llm.api.auth as auth_mod
    import pcai_llm.core.cascade as cascade_mod

    async def fake_auth(p: str) -> KeyRecord:
        return key

    monkeypatch.setattr(auth_mod, "authenticate", fake_auth)
    fake = WarnDB()
    monkeypatch.setattr(budgets_mod, "db", fake)
    monkeypatch.setattr(costs_mod, "db", fake)
    monkeypatch.setattr(cascade_mod, "db", fake)
    monkeypatch.setattr(embedcache, "db", fake)
    monkeypatch.setattr(routes_proxy, "db", fake)
    r = Router.with_mock(handler)
    r.set_endpoints(
        MODEL,
        [
            Endpoint(
                id="ep",
                model_name=MODEL,
                base_url="http://fake/v1",
            )
        ],
    )
    monkeypatch.setattr(routes_proxy, "model_router", r)

    from pcai_llm.app import create_app

    tc = TestClient(create_app(), raise_server_exceptions=False)
    with tc.stream(
        "POST",
        "/v1/chat/completions",
        json={"model": MODEL, "messages": [{"role": "user", "content": "hi"}]},
        headers={"Authorization": "Bearer hllm-x"},
    ) as resp:
        assert resp.headers.get("x-budget-warning", "").startswith("95%")


# ---------------------------------------------------------------------------
# Z4 — SSO self-mint homepage card (served-HTML contract)
#
# The card is a convenience, not a surface: the SPA probes GET /v1/identity
# on boot (no credentials — the identity IS the proxy-injected headers) and
# only shows the card when flags.self_mint is true. These tests pin the
# served page's contract for that flow — markup presence, the auth()
# X-API-Key envelope, and the exact wire shapes/copy the card consumes.
# The runtime branch logic itself is exercised by the backend zones' API
# tests (test_identity_probe.py / test_sso_selfmint.py); here the risk is a
# UI regression that silently breaks the contract the JS is written against.
# ---------------------------------------------------------------------------


def _served_html() -> str:
    from pcai_llm.app import create_app

    return TestClient(create_app(), raise_server_exceptions=False).get("/").text


def test_ui_sso_card_present_and_hidden_by_default() -> None:
    """The card ships in the connection area next to the key input but must
    be inert until the boot probe proves self_mint (flag-off/older-backend
    byte-identity: the page renders today exactly as before, card hidden)."""
    html = _served_html()
    btn = re.search(r'<button id="sso-open"[^>]*>', html)
    assert btn, "SSO card button missing from the connection area"
    assert "hidden" in btn.group(0), "SSO card must ship hidden (boot probe reveals it)"
    assert "Sign in with SSO" in html[btn.end() : html.index("</button>", btn.start())]
    # the identity chip row exists (RAG-look): chip + Use-an-API-key + Sign out,
    # all shipped hidden/dismitted until renderIdentityChip() shows them
    assert 'id="sso-chip"' in html and 'id="use-key-link"' in html and 'id="signout-btn"' in html
    assert "display:none" in html[html.index('id="sso-chip"') - 60 : html.index('id="sso-chip"') + 120]
    # card sits in the header button row, between Connect and the invite card
    assert html.index('id="keyconnect"') < html.index('id="sso-open"') < html.index('id="enroll-open"')
    # chip row renders next to the connection status, before the tools unit
    assert html.index('id="conn"') < html.index('id="sso-chip"') < html.index('for="tools"')
    # no label collision with the existing header buttons
    assert html.count(">Connect<") == 1


def test_ui_sso_dialog_reveal_pattern_matches_enroll() -> None:
    """The reveal-once presentation must reuse the enroll dialog's UX: a
    shown-once key block, the same message copy, and a copy button."""
    html = _served_html()
    assert 'id="sso-dlg"' in html
    assert 'id="sso-key"' in html and 'id="sso-copy"' in html
    assert "This key is shown <b>once</b>" in html
    # same fallback copy the enroll dialog carries (server message wins)
    assert "copy this key now — it is shown once" in html.lower()


def test_ui_auth_headers_carry_x_api_key() -> None:
    """Z1 envelope: behind the SSO edge chain the proxy overwrites
    Authorization with its own Bearer id_token, so the SPA key must ALSO go
    as X-API-Key. Authorization stays for direct (non-proxied) deployments —
    BOTH headers, one line, unchanged otherwise."""
    html = _served_html()
    m = re.search(r"function auth\(\) \{.*?\}", html, re.DOTALL)
    assert m, "auth() function missing"
    body = m.group(0)
    assert "'X-API-Key': keyIn.value.trim()" in body
    assert "'Authorization': 'Bearer ' + keyIn.value.trim()" in body
    assert "'Content-Type': 'application/json'" in body


def test_ui_sso_flow_wire_shapes() -> None:
    """The exact wire contract the card consumes (brief §Z4):
    - boot probe: GET /v1/identity with NO auth headers; self_mint gate;
    - mint: POST /v1/keys/for-me, no auth headers, no body (JSON {});
    - 409 → rotate confirm with the ratified copy → re-POST {"rotate": true};
    - 201 → keyIn/store/connect ending byte-identical to enroll-redeem."""
    html = _served_html()
    m = re.search(r"async function ssoProbeIdentity\(\) \{.*?\n  \}", html, re.DOTALL)
    assert m, "ssoProbeIdentity() missing"
    probe = m.group(0)
    assert "fetch('/v1/identity')" in probe, "probe must be a bare GET (no headers object)"
    # chip gate = subject resolution; mint/hint gate = flags.self_mint only.
    # `authenticated` must NOT gate the chip: behind the edge chain a
    # credential-less probe resolves the subject with authenticated=false
    # until a key exists — gating on it hides SSO from its own user.
    assert "self_mint" in probe and "j.subject" in probe
    assert "ssoSeen = true" in probe and "authenticated" not in probe, (
        "the chip gate is j.subject alone — no authenticated/self_mint conjunct"
    )

    m = re.search(r"async function ssoMint\(rotate\) \{.*?\n\n  function ssoHideAndClose", html, re.DOTALL)
    assert m, "ssoMint() missing"
    mint = m.group(0)
    assert "fetch('/v1/keys/for-me'" in mint
    assert "'Content-Type': 'application/json'" in mint
    assert "Authorization" not in mint and "X-API-Key" not in mint, (
        "the mint call must send NO credentials — identity comes from proxy headers"
    )
    assert "rotate: true" in mint
    assert "Replace your API key? The previous key stops authenticating immediately." in mint
    assert "r.status === 201" in mint and "r.status === 409" in mint

    m = re.search(r"function ssoRevealKey\(j\) \{.*?\n  \}", html, re.DOTALL)
    assert m, "ssoRevealKey() missing"
    reveal = m.group(0)
    # byte-identical ending to the enroll-redeem flow (input → persisted → connected)
    assert "keyIn.value = j.key;" in reveal
    assert "store.set('pcai-llm-key', j.key);" in reveal
    assert "connectKey();" in reveal
    # silent-failure posture: never an error banner on 401/403/503
    m = re.search(r"function ssoHideAndClose\(msg\) \{.*?\n  \}", html, re.DOTALL)
    assert m and "$('sso-open').hidden = true" in m.group(0)


def test_ui_sso_boot_probe_runs_and_fails_silent() -> None:
    """The probe is wired at boot (next to the savedKey prefill) and wrapped
    best-effort — an unreachable gateway must never block the chat page."""
    html = _served_html()
    boot = html[html.index("const savedKey = store.get('pcai-llm-key');") :]
    assert "ssoProbeIdentity().then(() => { if (!ssoSeen) renderIdentityChip(); }).catch(() => {});" in boot
    m = re.search(r"async function ssoProbeIdentity\(\) \{.*?\n  \}", html, re.DOTALL)
    assert m
    probe = m.group(0)
    # every failure path returns silently: 404/503 via !r.ok, network via catch
    assert probe.count("return;") >= 3 and "catch { return; }" in probe


def test_ui_identity_chip_renders_by_session_flavor() -> None:
    """MM RAG's renderIdentityChip look, mirrored: an SSO-probe session gets
    the GREEN dot + 'Signed in as <subject>' + 'oidc' pill; a pasted-key
    session gets the AMBER dot + 'API key' pill. SSO outranks paste, and the
    subject renders as-is (no identity source hardcoded)."""
    html = _served_html()
    m = re.search(r"function renderIdentityChip\(\) \{.*?\n  \}", html, re.DOTALL)
    assert m, "renderIdentityChip() missing"
    chip = m.group(0)
    # green (--ok) branch: SSO session, subject as-is, 'oidc' pill
    assert "var(--ok)" in chip and "Signed in as " in chip and "oidc" in chip
    # amber (--warn) branch: pasted key, 'API key' pill with RAG's title copy
    assert "var(--warn)" in chip and "API key" in chip
    assert "stored in this browser only" in chip
    # precedence: SSO branch checked first (ssoSeen gates the green branch)
    assert chip.index("if (ssoSeen)") < chip.index("var(--warn)")
    # SSO discovery sets the flavor from the probe's subject (preferred_username-derived)
    m = re.search(r"async function ssoProbeIdentity\(\) \{.*?\n  \}", html, re.DOTALL)
    assert m and "ssoSubject = String(j.subject)" in m.group(0)
    assert (
        "ssoSubject" in html[html.index("function renderIdentityChip") : html.index("async function ssoProbeIdentity")]
    )


def test_ui_sign_out_clears_key_and_hits_edge_on_sso() -> None:
    """D24-complete sign-out: ALWAYS clears the local key (store.del + input);
    on an SSO-flavored session it ALSO navigates to the same-origin
    oauth2-proxy sign-out (behind the chain the edge re-authenticates
    instantly otherwise); on a pasted-key session it stays on the page and
    drops the per-key transcript state."""
    html = _served_html()
    m = re.search(r"function signOut\(\) \{.*?\n  \}", html, re.DOTALL)
    assert m, "signOut() missing"
    so = m.group(0)
    assert so.index("store.del('pcai-llm-key')") < so.index("location.href = '/oauth2/sign_out'"), (
        "the key must be cleared BEFORE the edge navigation (RAG's sequence)"
    )
    assert "keyIn.value = '';" in so
    assert "if (ssoSeen) location.href = '/oauth2/sign_out';" in so
    # non-SSO path: local-only teardown, no navigation
    assert "else {" in so and "renderIdentityChip();" in so
    # wired to the button, which shares the chip's visibility rule
    assert "$('signout-btn').addEventListener('click', signOut);" in html
    chip_match = re.search(r"function renderIdentityChip\(\) \{.*?\n  \}", html, re.DOTALL)
    assert chip_match is not None, "renderIdentityChip not found in served HTML"
    chip_fn = chip_match.group(0)
    assert chip_fn.count("signout.hidden") >= 2, "Sign out visibility must track the chip"


def test_ui_use_an_api_key_link_present_and_wired() -> None:
    """RAG's chip-row link, header-declutter wave (2026-10): 'switch to API
    key' renders INSIDE the identity chip on SSO sessions (one less standalone
    header element) and focuses the key input — delegated from the chip so it
    survives the chip's re-renders."""
    html = _served_html()
    assert "switch to API key" in html
    assert 'id="use-key-link"' in html
    # the bare header anchor is gone — the link lives only in the chip HTML
    assert 'id="use-key-link" style' not in html
    assert "e.target.closest('#use-key-link')" in html and "keyIn.focus();" in html and "e.preventDefault();" in html
    chip_match = re.search(r"function renderIdentityChip\(\) \{.*?\n  \}", html, re.DOTALL)
    assert chip_match is not None, "renderIdentityChip not found in served HTML"
    chip_fn = chip_match.group(0)
    assert "use-key-link" in chip_fn, "the switch link must be part of the chip's SSO rendering"


# ---------------------------------------------------------------------------
# Wave-1 review fixes (F2/F3/F4)
# ---------------------------------------------------------------------------


def test_admin_api_sends_x_api_key_envelope() -> None:
    """F2: the admin console's api() rides BOTH envelopes. Behind an SSO
    chain classifying /admin, the proxy overwrites Authorization with the
    id_token Bearer — not an admin key — so X-API-Key is the header that
    survives with the console's key; on skip-auth paths it is inert.
    require_admin still reads Authorization (verified: routes_admin's
    dependency is a bearer hash-compare, no X-API-Key path), so BOTH go."""
    s = open("src/pcai_llm/ui/admin.html", encoding="utf-8").read()
    m = re.search(r"async function api\(path, opts = \{\}\) \{.*?headers: \{(.*?)\}", s, re.DOTALL)
    assert m, "admin api() missing"
    hdrs = m.group(1)
    assert "'Authorization': 'Bearer ' + key()" in hdrs, "console keeps its Bearer (skip-auth paths)"
    assert "'X-API-Key': key()" in hdrs, "console sends the X-API-Key envelope (chained /admin)"


def test_stale_chip_renders_after_stored_key_401() -> None:
    """F3: loadModels' 401 branch deletes the dead stored key — the amber
    'API key' chip + Sign out must not outlive it (re-render there)."""
    html = _served_html()
    m = re.search(r"async function loadModels\(\) \{.*?\n  \}", html, re.DOTALL)
    assert m, "loadModels() missing"
    branch = re.search(r"if \(r\.status === 401\) \{.*?\n      \}", m.group(0), re.DOTALL)
    assert branch, "loadModels 401 branch missing"
    b = branch.group(0)
    assert "store.del('pcai-llm-key')" in b
    assert b.index("store.del('pcai-llm-key')") < b.index("renderIdentityChip()"), (
        "the chip must re-render AFTER the dead key is dropped"
    )


def test_mint_success_shows_oidc_chip_immediately() -> None:
    """F4: a minted key IS oidc-provenance (bound_via sso-create) —
    ssoRevealKey flips the session flavor to SSO so the green/oidc chip
    renders immediately instead of after a reload."""
    html = _served_html()
    m = re.search(r"function ssoRevealKey\(j\) \{.*?\n  \}", html, re.DOTALL)
    assert m, "ssoRevealKey() missing"
    reveal = m.group(0)
    assert "ssoSeen = true" in reveal and "renderIdentityChip();" in reveal


# ---------------------------------------------------------------------------
# 2026-10-07 follow-ups (user screenshot review: ghost pill + banner parity)
# ---------------------------------------------------------------------------


def test_budget_strip_ships_hidden_without_inline_display() -> None:
    """The empty-pill bug: #budget-strip carried an INLINE display:flex,
    which overrides the hidden attribute's UA [hidden]{display:none} (any
    author display rule beats it) — the strip rendered as an EMPTY pill
    between "usage" and the connection status until its first content.
    The flex layout must live in the #budget-strip CSS rule, guarded by an
    explicit [hidden] override."""
    html = _served_html()
    m = re.search(r'<button id="budget-strip"[^>]*>', html)
    assert m, "budget-strip button missing"
    assert "hidden" in m.group(0), "budget-strip must ship hidden (content appears on connect)"
    assert "display" not in m.group(0), (
        "NO inline display on #budget-strip — it defeats the hidden attribute (the empty-pill bug)"
    )
    # the CSS rule carries the layout AND the [hidden] guard
    assert re.search(r"#budget-strip \{[^}]*display:\s*flex", html, re.DOTALL), (
        "budget-strip flex layout must come from the CSS rule"
    )
    assert re.search(r"#budget-strip\[hidden\] \{[^}]*display:\s*none", html, re.DOTALL), (
        "the [hidden] override must beat the author display:flex rule"
    )


def test_identity_chip_shows_whenever_probe_resolves_subject() -> None:
    """Banner parity with MM RAG / SQL (user, 2026-10-07): 'Signed in as
    <subject> · oidc' must render whenever the boot probe resolves an
    identity — even when self-mint is disabled (the chip is identity
    disclosure, not a mint affordance). Only the CARD and the boot hint's
    SSO clause track flags.self_mint."""
    html = _served_html()
    m = re.search(r"async function ssoProbeIdentity\(\) \{.*?\n  \}", html, re.DOTALL)
    assert m, "ssoProbeIdentity() missing"
    probe = m.group(0)
    # the subject gate comes FIRST and alone; the mint gate is a separate flag read
    assert probe.index("if (j.subject)") < probe.index("self_mint"), (
        "the chip must be gated on subject resolution BEFORE/independent of the mint flag"
    )
    # probe failure paths leave both affordances hidden (early return, no chip)
    assert probe.count("return;") >= 3
    # the hint clause ships hidden and is toggled WITH the mint button
    assert re.search(r'<span id="hint-sso" style="display:none">', html), (
        "the boot hint's SSO clause must ship hidden (never names an absent affordance)"
    )
    assert "$('sso-open').hidden = !mintable" in probe
    assert "hint-sso" in probe and "mintable ? 'inline' : 'none'" in probe
    # the chip itself renders from the probe state (renderIdentityChip is called after)
    assert probe.index("ssoSeen = true") < probe.index("renderIdentityChip();")
    # the invariant has TWO enforcement sites — probe (above) and the mint-
    # failure path: a 403/401 mid-mint hides the card and must clear the hint
    # clause with it (DS execution review, 2026-10-07: the clear was unpinned)
    m = re.search(r"function ssoHideAndClose\(msg\) \{.*?\n  \}", html, re.DOTALL)
    assert m, "ssoHideAndClose() missing"
    assert "hint-sso" in m.group(0) and "display = 'none'" in m.group(0), (
        "a failed mint must clear the boot hint's SSO clause along with the card"
    )
