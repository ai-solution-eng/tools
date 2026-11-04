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
    # BOTH consoles reachable from the homepage, stacked in ONE right-aligned
    # column (w6 follow-up, owner request): /team_leader is the
    # enrollment-scoped sibling of /admin, so it sits directly beneath it
    # instead of widening the header row.
    assert 'href="/team_leader"' in html, "the homepage must link the team-leader console"
    assert html.index('href="/admin"') < html.index('href="/team_leader"') < html.index('id="theme"')
    _stack = html[html.index('href="/admin"') - 400 : html.index('href="/team_leader"')]
    assert "flex-direction:column" in _stack, "Admin + Team leader must share one stacked container"


def test_chat_tools_box_sends_explicit_relay_three_state() -> None:
    """w6 F2: the tools box must be able to TURN OFF the relay. The header
    is a three-state wire value (routes_proxy._relay_wants: '1' forces on,
    '0' forces off, ABSENT falls back to the key's relay_default). The box
    used to send '1' when checked and OMIT the header when unchecked, so
    for a relay_default=true key unchecking was a no-op and the user was
    billed for in-cluster tool calls the UI had told them were off. The box
    now sends '0' when unchecked — a hard off the backend honors even for
    default-on keys. (The '0' relay-400 cannot fire from this page: it only
    applies to body-declared {type: mcp} tools, and the chat body carries
    none.)"""
    html = _served_html()
    assert "hdrs['X-PCAI-Execute-Tools'] = useTools ? '1' : '0'" in html, (
        "unchecked tools box must send the explicit '0' (hard off), not omit the header"
    )
    assert "if (useTools) hdrs['X-PCAI-Execute-Tools'] = '1'" not in html, "the omit-when-unchecked form must be gone"


def test_chat_key_defaults_to_session_storage_with_explicit_remember() -> None:
    """w6 F1 (audit F3): the bearer key used to persist in localStorage —
    the one console that renders MODEL OUTPUT kept a long-lived plaintext
    key on disk, while admin.html / team_leader.html deliberately use
    sessionStorage for the same class of credential. The fix aligns with
    that product ruling: the key defaults to sessionStorage; the
    "remember key" box (UNCHECKED by default) is the only path to
    localStorage; a legacy plaintext localStorage key is migrated to
    sessionStorage once at boot and not kept; sign-out clears BOTH stores.

    Behavioral pin: extract the iframe-safe store wrapper and the key
    lifecycle (saveKey/loadKey/clearKey) verbatim from the served page and
    run them under node with Map-backed sessionStorage/localStorage fakes —
    asserting the actual storage calls, no browser needed."""
    import re
    import subprocess
    import tempfile

    html = _served_html()
    # the control exists, is UNCHECKED by default
    m = re.search(r'<input type="checkbox" id="remember-key"[^>]*>', html)
    assert m, "remember-key control missing"
    assert "checked" not in m.group(0), "remember box must be UNCHECKED by default"
    store_fn = re.search(r"const store = \{.*?\n  \};", html, re.DOTALL)
    key_fn = re.search(r"const KEY = 'pcai-llm-key'.*?function clearKey\(\) \{.*?\n  \}", html, re.DOTALL)
    assert store_fn, "iframe/privacy store wrapper not extractable"
    assert key_fn, "key storage lifecycle (saveKey/loadKey/clearKey) not extractable"
    # the old raw localStorage key paths must be gone from the key handlers
    assert "store.set('pcai-llm-key'" not in html, "raw localStorage key WRITE remains"
    assert "store.del('pcai-llm-key')" not in html, "raw localStorage key DELETE remains"

    harness = (
        "const mk = () => { const m = new Map(); return { getItem: k => (m.has(k) ? m.get(k) : null), setItem: (k, v) => m.set(k, String(v)), removeItem: k => m.delete(k) }; };\n"
        "const sessionStorage = mk();\n"
        "const localStorage = mk();\n"
        "const rememberEl = { checked: false };\n"
        "const $ = id => (id === 'remember-key' ? rememberEl : {});\n"
        "const t = (n, cond) => { if (!cond) { console.error('FAIL ' + n); process.exit(1); } };\n"
    )
    calls = (
        # unchecked: key lands in sessionStorage only, localStorage stays clean
        "saveKey('k-session');\n"
        "t('sess-has', sessionStorage.getItem('pcai-llm-key') === 'k-session');\n"
        "t('ls-absent', localStorage.getItem('pcai-llm-key') === null);\n"
        "t('flag-off', localStorage.getItem('pcai-key-remember') !== '1');\n"
        # checked: the key goes to localStorage (the opt-in) and the flag is set
        "rememberEl.checked = true; saveKey('k-remember');\n"
        "t('ls-has', localStorage.getItem('pcai-llm-key') === 'k-remember');\n"
        "t('flag-on', localStorage.getItem('pcai-key-remember') === '1');\n"
        # boot read order: sessionStorage wins when both hold a key
        "sessionStorage.setItem('pcai-llm-key', 'sess-wins');\n"
        "t('sess-first', loadKey() === 'sess-wins');\n"
        # legacy plaintext localStorage key + opt-in off: migrated once, not kept
        "sessionStorage.removeItem('pcai-llm-key'); localStorage.setItem('pcai-llm-key', 'legacy'); localStorage.removeItem('pcai-key-remember');\n"
        "const lk = loadKey();\n"
        "t('legacy-migrated', lk === 'legacy' && sessionStorage.getItem('pcai-llm-key') === 'legacy' && localStorage.getItem('pcai-llm-key') === null);\n"
        # opt-in on + session empty: the localStorage copy is read (kept — it is opted in)
        "sessionStorage.removeItem('pcai-llm-key'); localStorage.setItem('pcai-llm-key', 'ls-val'); localStorage.setItem('pcai-key-remember', '1');\n"
        "t('ls-read-when-opted-in', loadKey() === 'ls-val');\n"
        # sign-out: clears BOTH stores and the flag
        "rememberEl.checked = true; saveKey('k-final');\n"
        "clearKey();\n"
        "t('signout-cleared', sessionStorage.getItem('pcai-llm-key') === null && localStorage.getItem('pcai-llm-key') === null && localStorage.getItem('pcai-key-remember') === null);\n"
    )
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
        f.write(store_fn.group(0) + "\n" + key_fn.group(0) + "\n" + harness + calls)
        path = f.name
    r = subprocess.run(["node", path], capture_output=True, text=True)
    assert r.returncode == 0, f"key storage contract broken:\n{r.stdout}\n{r.stderr}"


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
    assert 'id="sso-chip"' in html and 'id="signout-btn"' in html
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
    # 2026-11-03: the destructive choice NAMES the existing key (owner view:
    # prefix + created_at) instead of a blind "Replace your API key?", and it
    # promises what is now true — conversations follow the key group.
    assert "This account already has an API key." in mint
    assert "Your saved conversations are kept" in mint
    assert "fetch('/v1/keys/for-me')" in mint, "the 409 dialog reads the owner view first"
    assert "r.status === 201" in mint and "r.status === 409" in mint

    m = re.search(r"function ssoRevealKey\(j\) \{.*?\n  \}", html, re.DOTALL)
    assert m, "ssoRevealKey() missing"
    reveal = m.group(0)
    # the enroll-redeem ending, PLUS provenance (2026-11-03): the key is
    # persisted as an SSO credential and connected in the SSO chip flavor, so
    # the identity text survives and boot can auto-associate it.
    assert "keyIn.value = j.key;" in reveal
    assert "saveKey(j.key, true);" in reveal
    assert "connectKey({ sso: true });" in reveal
    assert "$('remember-key').checked = true;" in reveal, (
        "an SSO key is a device credential: it must survive a tab close"
    )
    # silent-failure posture: never an error banner on 401/403/503
    m = re.search(r"function ssoHideAndClose\(msg\) \{.*?\n  \}", html, re.DOTALL)
    assert m and "ssoMintable = false" in m.group(0) and "renderIdentityChip();" in m.group(0)


def test_ui_sso_boot_probe_runs_and_fails_silent() -> None:
    """The probe is wired at boot (next to the savedKey prefill) and wrapped
    best-effort — an unreachable gateway must never block the chat page."""
    html = _served_html()
    boot = html[html.index("const savedKey = loadKey();") :]
    assert "ssoProbeIdentity().then(() => { if (!ssoSeen) renderIdentityChip(); }).catch(() => {});" in boot
    m = re.search(r"async function ssoProbeIdentity\(\) \{.*?\n  \}", html, re.DOTALL)
    assert m
    probe = m.group(0)
    # every failure path returns silently: 404/503 via !r.ok, network via catch
    assert probe.count("return;") >= 3 and "catch { return; }" in probe


def test_ui_identity_chip_renders_identity_and_provenance() -> None:
    """2026-11-03 owner report: the chip must keep "Signed in as
    andrew-bydlon" while the PILL says which credential authenticates.

    This replaces the bug-B mode-switch design. There is only ONE stored
    credential, so the two in-chip "switch" links could only RELABEL the chip
    — the operator rightly called that a no-op. Identity (dot + name) is
    disclosure; the pill ('oidc' vs 'API key') is provenance.
    """
    html = _served_html()
    m = re.search(r"function renderIdentityChip\(\) \{.*?\n  \}", html, re.DOTALL)
    assert m, "renderIdentityChip() missing"
    chip = m.group(0)
    # both dot colors survive; the NAME comes from the probe whenever it resolved
    assert "var(--ok)" in chip and "var(--warn)" in chip
    assert "Signed in as " in chip and "escHtml(ssoSubject" in chip
    # provenance: the pill follows the ACTIVE credential, not a separate mode branch
    # SSO mode is the ACTIVE credential's binding (server-verified), not the
    # connect path: a pasted key bound to the identity is SSO too.
    assert "const ssoBound = keyed && keySsoBound;" in chip
    assert "oidc" in chip and "API key" in chip
    assert "stored in this browser only" in chip
    # no fake switches
    assert "use-key-link" not in chip and "use-sso-link" not in chip
    # keyless SSO still says so (the old "knows me but cannot chat" confusion)
    assert "no key on this browser yet" in chip
    # Sign out is offered for a NON-SSO key only: an SSO-bound credential ends
    # at the platform sign-out, and a keyless browser has nothing to clear.
    assert "signout.hidden = !(keyed && !ssoBound);" in chip
    # SSO discovery sets the name from the probe's subject
    m = re.search(r"async function ssoProbeIdentity\(\) \{.*?\n  \}", html, re.DOTALL)
    assert m and "ssoSubject = String(j.subject)" in m.group(0)


def test_ui_sign_out_clears_key_then_navigates_to_the_gateway_route() -> None:
    """D24-complete sign-out, closeout edition (2026-10-30, wave 3): ALWAYS
    clears the local key FIRST (clearKey + input + keyMode), then on a
    pre-sign-out SSO session NAVIGATES to /oauth2/sign_out — a path the
    GATEWAY now owns and always answers with a 302 (to the platform
    oauth2-proxy when PCAI_LLM_SSO_SIGN_OUT_URL is set, else back to
    /?signout=local). The navigation is safe precisely BECAUSE the route can
    no longer 404: the wave-2 fetch probe existed only while it could, and
    it is gone. The honest "platform session survived" note now lives at
    BOOT (the signout=local branch), not inside signOut()."""
    html = _served_html()
    m = re.search(r"function signOut\(\) \{.*?\n  \}", html, re.DOTALL)
    assert m, "signOut() missing"
    so = m.group(0)
    assert so.index("clearKey()") < so.index("location.href = '/oauth2/sign_out'"), (
        "the key must be cleared BEFORE the navigation (RAG's sequence); w6 F1: clearKey drops BOTH stores + the remember flag"
    )
    assert "keyIn.value = '';" in so
    assert "keyMode = false;" in so
    # 2026-11-03 owner rule: the EXIT follows the ACTIVE credential's BINDING —
    # an SSO-bound key ends the platform session (defensive; the button is
    # hidden for it), a NON-SSO pasted key only clears this device.
    assert "const ssoBound = keySsoBound;" in so
    # the dead probe must not come back: it is what stranded users on the
    # gateway's JSON 404 page in the first place.
    assert "edgeSignOut" not in html
    assert "redirect: 'manual'" not in html
    # wired to the button, which shares the chip's visibility rule
    assert "$('signout-btn').addEventListener('click', signOut);" in html
    chip_match = re.search(r"function renderIdentityChip\(\) \{.*?\n  \}", html, re.DOTALL)
    assert chip_match is not None, "renderIdentityChip not found in served HTML"
    chip_fn = chip_match.group(0)
    assert chip_fn.count("signout.hidden") >= 2, "Sign out visibility must track the chip"
    # VISIBILITY (2026-11-03 owner rule: "If SSO … no sign-out; if not, allow
    # sign-out"). The button tracks the ACTIVE CREDENTIAL's SSO BINDING
    # (server-verified by probeKeyIdentity), not the mere visibility of an SSO
    # session — so an admin key on an SSO deployment keeps its Sign out, and a
    # pasted key bound to the identity is treated as SSO.
    assert "signout.hidden = !(keyed && !ssoBound);" in chip_fn, (
        "Sign out must be offered for a NON-SSO key only (SSO ends at the platform sign-out)"
    )
    # the honest local-only outcome is reported at BOOT from the query param
    # the gateway's 302 carries — and the param is stripped so a refresh
    # does not re-show it.
    assert "signout=local" in html
    assert "history.replaceState" in html
    assert "signoutNote(" in html


def test_ui_chip_has_no_fake_mode_switches() -> None:
    """2026-11-03 owner report: "when I click switch to api key and switch to
    sso, it seems like a no-op … switching to SSO does not make any sense".

    Both links were cosmetic: only ONE credential is stored, so a "switch"
    could only relabel the chip. They are removed. Switching credentials is
    the explicit path — paste a key + Connect, or Sign out and use the SSO
    card — and the chip reports identity + provenance instead.
    """
    html = _served_html()
    assert "use-key-link" not in html
    assert "use-sso-link" not in html
    # the RENDERED links are gone; prose comments may still name the removed
    # affordance (they document why it went), so match the anchor text.
    assert ">switch to API key<" not in html
    assert ">switch to SSO session<" not in html
    assert "e.target.closest('#use-key-link, #use-sso-link')" not in html
    chip_match = re.search(r"function renderIdentityChip\(\) \{.*?\n  \}", html, re.DOTALL)
    assert chip_match is not None, "renderIdentityChip not found in served HTML"
    assert "ssoBound" in chip_match.group(0)


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
    assert "clearKey()" in b  # w6 F1: dead key is dropped from BOTH stores
    assert b.index("clearKey()") < b.index("renderIdentityChip()"), (
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
    # and the flavor STICKS through connect (2026-11-03): connectKey({sso:true})
    # is what keeps "Signed in as <name> · oidc" instead of flipping to the
    # API-key chip the instant the key lands.
    assert "connectKey({ sso: true });" in reveal


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
    # 2026-11-03: the probe only RECORDS the flag. The card/hint visibility
    # rule moved to renderIdentityChip — ONE site, and the rule is now
    # "flag AND no key on this browser": with a working key connected the
    # card would only offer a destructive rotation ("Clicking the SSO button
    # ALWAYS gets me a new rotated key", operator report).
    assert "ssoMintable = !!(j.flags && j.flags.self_mint === true)" in probe
    m = re.search(r"function renderIdentityChip\(\) \{.*?\n  \}", html, re.DOTALL)
    assert m, "renderIdentityChip() missing"
    chip = m.group(0)
    assert "const showMint = ssoMintable && !ready();" in chip
    assert "if (open) open.hidden = !showMint;" in chip
    assert "hintSso.style.display = showMint ? 'inline' : 'none';" in chip
    # the chip itself renders from the probe state (renderIdentityChip is called after)
    assert probe.index("ssoSeen = true") < probe.index("renderIdentityChip();")
    # the invariant has TWO enforcement sites — probe (above) and the mint-
    # failure path: a 403/401 mid-mint hides the card and must clear the hint
    # clause with it (DS execution review, 2026-10-07: the clear was unpinned)
    m = re.search(r"function ssoHideAndClose\(msg\) \{.*?\n  \}", html, re.DOTALL)
    assert m, "ssoHideAndClose() missing"
    assert "ssoMintable = false" in m.group(0) and "renderIdentityChip();" in m.group(0), (
        "a failed mint must clear the boot hint's SSO clause along with the card"
    )


def test_sso_sign_out_route_always_redirects(monkeypatch: pytest.MonkeyPatch) -> None:
    """The gateway OWNS /oauth2/sign_out, so the console's "Sign out" can never
    land on a JSON 404 with the SSO cookie still alive (the reported bug: the
    path fell through the VirtualService catch-all to FastAPI).

    Two shapes, both a redirect:
      * PCAI_LLM_SSO_SIGN_OUT_URL set -> 302 to the platform proxy's sign-out
        with rd appended (the proxy clears the domain-wide cookie and bounces
        back);
      * unset -> 302 to /?signout=local, which the console reads to say the
        platform session survived. Honest beats silent.

    rd is a RETURN path on THIS origin, so anything absolute or
    protocol-relative must be dropped — an open redirect here would be a
    phishing primitive on a page that keeps bearer keys in sessionStorage.
    """
    from pcai_llm.app import create_app
    from pcai_llm.config import settings

    tc = TestClient(create_app(), raise_server_exceptions=False)

    monkeypatch.setattr(settings, "sso_sign_out_url", "", raising=False)
    r = tc.get("/oauth2/sign_out", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/?signout=local"

    for hostile in ("//evil.example/x", "https://evil.example/x", "/\\evil.example"):
        r = tc.get("/oauth2/sign_out", params={"rd": hostile}, follow_redirects=False)
        assert r.status_code == 302
        assert "evil.example" not in r.headers["location"]

    monkeypatch.setattr(
        settings, "sso_sign_out_url", "https://oauth2-proxy.example.test/oauth2/sign_out", raising=False
    )
    r = tc.get("/oauth2/sign_out", params={"rd": "/team_leader"}, follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "https://oauth2-proxy.example.test/oauth2/sign_out?rd=%2Fteam_leader"


def test_no_inline_handler_calls_a_script_scoped_helper() -> None:
    """w7 bug CLASS guard, across all three consoles.

    An inline attribute handler (onclick="...") is evaluated against WINDOW,
    not the script's lexical scope. So every helper it names must be a window
    property — or a top-level `function` declaration, which a classic script
    makes global. It may NOT name a script-scoped `const`/`let`.

    That is exactly how the key-filter "clear" button broke: it called the
    script-scoped `const $ = id => document.getElementById(id)` and threw
    `ReferenceError: $ is not defined` on click, clearing nothing. `filterKeys`
    in the same attribute worked only because it is explicitly
    `window.filterKeys`. This pin would have caught it, and catches the next
    one of the same shape (a helper that is later moved out of window scope,
    or a new button wired to a module-local function).
    """
    import re

    # JS KEYWORDS are not identifiers-with-a-call — "if (" / "for (" match the
    # call pattern too. Listing them keeps the guard about FUNCTIONS.
    keywords = {
        "if",
        "for",
        "while",
        "switch",
        "catch",
        "typeof",
        "return",
        "new",
        "function",
        "delete",
        "in",
        "of",
        "instanceof",
        "do",
        "else",
        "void",
    }
    builtins = keywords | {
        "this",
        "event",
        "document",
        "window",
        "console",
        "JSON",
        "Number",
        "String",
        "Boolean",
        "Array",
        "Object",
        "Math",
        "Date",
        "parseInt",
        "parseFloat",
        "encodeURIComponent",
        "decodeURIComponent",
        "alert",
        "confirm",
        "prompt",
        "setTimeout",
        "clearTimeout",
        "isNaN",
        "fetch",
        "RegExp",
        "Error",
    }
    paths = (
        "src/pcai_llm/ui/admin.html",
        "src/pcai_llm/ui/team_leader.html",
        "src/pcai_llm/ui/index.html",
    )
    offenders = []
    for path in paths:
        html = open(path, encoding="utf-8").read()
        on_window = set(re.findall(r"window\.([A-Za-z_$][\w$]*)\s*=", html))
        # a top-level declaration in a classic script IS a window property
        declared = set(re.findall(r"^(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(", html, re.MULTILINE))
        ok = on_window | declared | builtins
        for body in re.findall(r'on[a-z]+="([^"]*)"', html):
            # a CALL is an identifier followed by "(", NOT preceded by "." —
            # obj.method(...) is a method call, not a global lookup
            for m in re.finditer(r"(^|[^.\w$])([A-Za-z_$][\w$]*)\s*\(", body):
                if m.group(2) not in ok:
                    offenders.append((path, m.group(2), body[:60]))
    assert not offenders, f"inline handler calls a non-window helper: {offenders}"


def test_chat_discloses_the_model_that_actually_served() -> None:
    """w7 presentation fix (owner report): the answer can come from a model
    that is NOT the one in the picker.

    Two ways that happens, both legitimate: a cascade alias resolves per
    request, and a rate/budget refusal on the chosen model fails over down
    the ordering (routes_proxy._admit_with_failover). The gateway names both
    facts on the wire — X-PCAI-Served-Model and X-PCAI-Fallback — and the
    console read NEITHER, so a failover was invisible and read as a bug
    ("it appears to be routing to deepseek").

    Source pin (the disclosure is inline in the send path, not a standalone
    function): both headers must be consulted, the notice must be conditional
    on the served model DIFFERING from the requested one, and the meta line
    must stay textContent-painted so a model name can never become markup.
    """
    html = open("src/pcai_llm/ui/index.html", encoding="utf-8").read()
    assert "X-PCAI-Served-Model" in html, "the chat console must read the served-model header"
    assert "X-PCAI-Fallback" in html, "the chat console must read the fallback header"
    assert "servedModel !== model" in html, "the notice must be conditional on a DIFFERENT model"
    assert "' · served by '" in html, "the meta line must name the served model"
    # the requested model is the picker value; a fallback notice is only
    # meaningful against it
    assert "const model = modelSel.value;" in html
    # meta is painted inert — a model name from a header must never be markup
    assert "m.textContent = meta;" in html


def test_mcp_dialog_is_de_specialized_no_memory_dataset_card() -> None:
    """2026-11-04 operator decision: stop specializing.

    Memory identity is ONE per-key header override (mm-rag: X-API-Key, minted
    in the RAG app) merged over the server-level auth; the SSO-managed ★
    dataset is resolved by the RAG backend from that key. So the dialog must
    NOT model a dataset/password pair, must NOT supersede the memory server's
    generic credential row, and the redeem page must not sell the legacy
    dataset opt-in. Stored header NAMES stay visible so a legacy pair is
    replaceable/clearable.
    """
    html = open("src/pcai_llm/ui/index.html", encoding="utf-8").read()
    assert "Private memory dataset" not in html, "the dataset card is removed"
    assert "mem.headers.dataset" not in html and "mem.headers.password" not in html
    assert "#mem-save" not in html and "#mem-clear" not in html, "the pair editor is gone"
    assert "el.dataset.server !== mem.server" not in html, (
        "the memory server's generic credential row must never be superseded"
    )
    # the generic row IS the attachment surface and names the current credential
    assert 'placeholder="Header name (e.g. X-API-Key)"' in html
    assert "cred_header_names.join(', ')" in html, "stored header names stay visible"
    # the redeem page no longer offers (or sends) the legacy dataset opt-in
    assert "Give me a private memory dataset" not in html
    assert "memory_opt_in: $('enroll-memory').checked" not in html
    assert "MCP tools → Private memory dataset" not in html


def test_markdown_renderer_handles_blocks_and_escapes_model_markup() -> None:
    """The assistant bubble renders markdown instead of showing raw syntax
    (2026-11-04 operator report: the console printed ```bash fences,
    `backticks` and ## headings literally).

    The safety half is load-bearing: model output is UNTRUSTED, so the renderer
    escapes every text chunk BEFORE applying inline rules and emits only tags
    its own literals produce. Behavioral pin: extract the marked renderer block
    verbatim and run it under node with adversarial fixtures.
    """
    import subprocess
    import tempfile

    html = _served_html()
    start = "  // ---- markdown renderer (pure; tests extract verbatim) ----"
    end = "  // ---- end markdown renderer ----"
    assert start in html and end in html, "the renderer block must stay extractable"
    block = html[html.index(start) : html.index(end)]

    BT = "`"
    fixture = "\n".join(
        [
            "Critical aspects of the **DeepSeek-V4.1-Flash-1017** deployment:",
            "",
            "## The verified serve recipe",
            "",
            BT * 3 + "bash",
            "sgllang serve --model-path deepseek-ai/DeepSeek-V4.1-Flash-1017 \\",
            "--mem-fraction-static 0.8",
            BT * 3,
            "",
            "- **--ep-size 4 is a hard requirement** on 4xH200/TP4 - dies with " + BT + "Mxfp4Cutlass" + BT,
            "- **mem-fraction-static 0.8** passed on a cold-cache first boot",
            "  - nested: the 0.80 OOM'd at CUDA-graph capture",
            "",
            "> Poisoned-boot tell: a *negative* weight-load line means void that boot.",
            "",
            "| Stage | Time |",
            "|-------|------|",
            "| Cold GEMM | 32,768 shapes |",
            "",
            "---",
            "",
            "See https://example.com/docs for more.",
        ]
    )
    unsafe = "\n".join(
        [
            "<script>alert(1)</script>",
            "<img src=x onerror=alert(2)>",
            "[click](javascript:alert(3))",
            "![x](javascript:alert(4))",
            BT * 3 + "html",
            "<b>not bold</b> & <script>x</script>",
            BT * 3,
        ]
    )
    unclosed = BT * 3 + "bash\nls -la /tmp"

    harness = (
        """
const t = (n, cond) => { if (!cond) { console.error('FAIL ' + n); process.exit(1); } };
const out = renderMarkdown(__IN__);
t('code-block', out.includes('class="md-code"') && out.includes('md-lang">bash'));
t('pre', out.includes('<pre><code>sgllang serve'));
t('copy', out.includes('class="md-copy"'));
t('heading', out.includes('<h3 class="md-h">The verified serve recipe'));
t('bold', out.includes('<b>DeepSeek-V4.1-Flash-1017</b>'));
t('inline-code', out.includes('<code>Mxfp4Cutlass</code>'));
t('list', out.includes('<ul><li><b>--ep-size 4 is a hard requirement</b>'));
t('nested', out.includes('<li>nested: the 0.80 OOM'));
t('quote', out.includes('<blockquote>Poisoned-boot tell'));
t('italic', out.includes('<i>negative</i>'));
t('table', out.includes('<table><thead><tr><th>Stage</th>'));
t('hr', out.includes('<hr>'));
t('autolink', out.includes('<a href="https://example.com/docs"'));
const bad = renderMarkdown(__UNSAFE__);
t('no-script', !bad.includes('<script'));
t('no-img-inject', !bad.includes('<img src=x'));
t('no-js-link', !bad.includes('href="javascript'));
t('no-js-img', !bad.includes('src="javascript'));
t('code-escaped', bad.includes('&lt;b&gt;not bold&lt;/b&gt; &amp; &lt;script&gt;x&lt;/script&gt;'));
const open = renderMarkdown(__UNCLOSED__);
t('streaming-fence', open.includes('class="md-code"') && open.includes('ls -la /tmp'));
console.log('MARKDOWN_OK');
""".replace("__IN__", repr(fixture))
        .replace("__UNSAFE__", repr(unsafe))
        .replace("__UNCLOSED__", repr(unclosed))
    )
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
        f.write(block + "\n" + harness)
        path = f.name
    r = subprocess.run(["node", path], capture_output=True, text=True)
    assert r.returncode == 0, f"markdown renderer contract broken:\n{r.stdout}\n{r.stderr}"


def test_chat_stream_paints_markdown_live_and_renders_the_final_frame() -> None:
    """Live rendering is throttled (re-parsing a long answer every frame is not
    free) but the turn's FINAL paint must render unconditionally — otherwise the
    last streamed frame could be a stale mid-throttle render."""
    html = _served_html()
    assert "paint(() => { renderRichSoon(bubble_, acc); });" in html, "stream must use the throttled renderer"
    assert "else renderRich(bubble_, acc);" in html, "the final frame must render markdown"
    assert "mdPaintedAt = 0;" in html, "each turn must start with a fresh throttle window"
    assert ".msg.md { white-space: normal; }" in html, "markdown bubbles must not inherit pre-wrap"
    assert ".msg.md .md-copy" in html and 'class="md-copy"' in html, "code blocks need the copy affordance"


def test_sso_session_associates_automatically_and_never_offers_rotation() -> None:
    """2026-11-03 operator report, the three UX defects pinned together.

    (1) The chip knew the identity while the chat was dead; (2) clicking
    "Sign in with SSO" ALWAYS minted a rotated key (the card was shown even
    with a working key connected, and the 409 path rotated); (3) the chip
    flipped to "API-key user" the moment the mint landed.

    The fixes this test locks:
      * PROVENANCE rides the key store — saveKey(v, true) / isSsoKey() /
        clearKey() — so a returning browser re-associates with no click and
        keeps the SSO flavor;
      * boot connects with that flavor: connectKey({sso: isSsoKey()});
      * the mint card is a FIRST-mint affordance only (flag AND no key);
      * the keyless SSO chip says so instead of implying a session.
    """
    html = _served_html()
    m = re.search(r"function saveKey\(v, sso\) \{.*?\n  \}", html, re.DOTALL)
    assert m, "saveKey(v, sso) missing"
    save = m.group(0)
    assert "sess.set(SSO, '1')" in save and "store.set(SSO, '1')" in save
    assert "store.del(SSO)" in save, "a non-SSO key must clear the provenance"
    m = re.search(r"function isSsoKey\(\) \{.*?\n  \}", html, re.DOTALL)
    assert m, "isSsoKey() missing"
    assert "sess.get(SSO) === '1'" in m.group(0)
    m = re.search(r"function clearKey\(\) \{.*?\n  \}", html, re.DOTALL)
    assert m, "clearKey() missing"
    assert "sess.del(SSO)" in m.group(0) and "store.del(SSO)" in m.group(0), (
        "signing out must clear the provenance with the key"
    )
    # boot auto-associates the stored credential in the SSO flavor
    assert "connectKey({ sso: isSsoKey() });" in html
    # the mint path persists + remembers and keeps the flavor
    assert "connectKey({ sso: true });" in html
    assert "$('remember-key').checked = true;" in html
    # the keyless SSO chip admits the state instead of implying a session
    m = re.search(r"function renderIdentityChip\(\) \{.*?\n  \}", html, re.DOTALL)
    assert m
    assert "no key on this browser yet" in m.group(0)


def test_ui_header_status_placement_and_unlimited_budget_strip() -> None:
    """2026-11-03 owner report (screenshot review): the connection dot read as
    an orphan in the chip row, an amber dot sat beside a CONNECTED admin key,
    and an uncapped key's strip said "no budgets on this key" — nothing
    visible at all.

    Pins the three fixes: the dot sits WITH the model picker; the chip dot is
    green whenever a KEY is connected (amber only for "identity resolved, no
    key on this browser"); the strip falls back to the live minute window when
    the key carries no period budget.
    """
    html = _served_html()
    # the connection dot moved into the model picker's slot
    assert html.index('id="modelwrap"') < html.index('id="conn"') < html.index('id="apikey"')
    # the chip dot tracks a WORKING KEY, not the credential flavor
    chip = re.search(r"function renderIdentityChip\(\) \{.*?\n  \}", html, re.DOTALL)
    assert chip is not None
    assert "dot(keyed ? 'var(--ok)' : 'var(--warn)')" in chip.group(0)
    # an uncapped key still shows live usage instead of a dead-end label
    strip = re.search(r"async function refreshBudgetStrip\(\) \{.*?\n  \}", html, re.DOTALL)
    assert strip is not None
    # match the CODE, not the comment that documents why it went
    assert "parts.push('no budgets on this key')" not in strip.group(0)
    assert "tok this min" in strip.group(0)


def test_ui_sso_mode_is_the_active_credentials_binding() -> None:
    """2026-11-03 owner rule: "If SSO, use SSO key, no sign-out, green. This
    should also be realized with the API key connect option for the SSO. If
    not, allow signout."

    SSO mode is NOT the connect path (mint vs paste) — it is whether the
    ACTIVE key is bound to the browser's SSO subject, decided SERVER-SIDE by
    probeKeyIdentity (GET /v1/identity WITH the key returns the key's own
    binding). So a pasted SSO key behaves exactly like the minted one (green,
    'oidc', no local Sign out), while an admin/CLI key keeps Sign out.
    """
    html = _served_html()
    m = re.search(r"async function probeKeyIdentity\(\) \{.*?\n  \}", html, re.DOTALL)
    assert m, "probeKeyIdentity() missing"
    probe = m.group(0)
    assert "fetch('/v1/identity', { headers: auth() })" in probe
    assert "String(j.subject) === ssoSubject" in probe, (
        "SSO mode must be the key's OWN binding vs the browser identity, not a connect-path flag"
    )
    # connectKey probes AFTER the key is live (loadModels settles readiness)
    ck = re.search(r"async function connectKey\(opts\) \{.*?\n  \}", html, re.DOTALL)
    assert ck is not None
    assert ck.group(0).index("await loadModels()") < ck.group(0).index("await probeKeyIdentity()")
    # boot re-probes once the subject is known (connectKey may run first)
    bp = re.search(r"async function ssoProbeIdentity\(\) \{.*?\n  \}", html, re.DOTALL)
    assert bp is not None and "await probeKeyIdentity()" in bp.group(0)
    # the visibility rule: non-SSO keys only
    chip = re.search(r"function renderIdentityChip\(\) \{.*?\n  \}", html, re.DOTALL)
    assert chip is not None
    assert "signout.hidden = !(keyed && !ssoBound);" in chip.group(0)
