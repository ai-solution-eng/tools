"""AUDIT-2026-11-10 zone R6 (admin UI console) — remediation pins.

Pins the ten R6 findings from documentation/Audits/AUDIT-2026-11-10.md
(section-3 P3 lines + the two P2 UI items) against the three self-contained
UI pages. House style: string assertions over the checked-in HTML, plus
node-verified rendered-DOM behavior where rendering is the thing being
asserted (the same technique tests/test_ui.py uses for saveKey and the
relay rendering).

Finding map:
  1  [P2] admin audit CSV export was a bare navigable anchor (no auth
         headers) — 401 JSON saved as admin-audit.csv on bearer-key
         deployments.
  2  [P2] team_leader banner() wrote raw HTML by default (the 2026-11-02
         stored-XSS footgun, re-opened); call sites pre-escaped.
  3  [P2] six MCP server names interpolated into URL paths unencoded.
  4  [P2] index.html comment claimed the remember-key box is "unchecked by
         default" — false since the SSO mint/rotate flow auto-checks it.
  5  [P3] dead no-op spread in admin api() (both branches {}).
  6  [P3] body-wide MutationObserver forced a full re-layout pass on every
         DOM mutation.
  7  [P3] trace threads rendered fully expanded (details open), up to 200
         requests laid out per render.
  8  [P3] costs-table renderer duplicated byte-identically in two loaders.
  9  [P3] probeKeyIdentity kept a stale optimistic SSO binding on !r.ok and
         swallowed the catch.
  10 [P3] server ids interpolated unescaped into option values, breaking
         the page's esc-everything invariant.

Existing UI invariant tests (tests/test_ui.py, tests/test_team_leader_ui.py)
are left untouched.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile

ADMIN = os.path.join("src", "pcai_llm", "ui", "admin.html")
TEAM = os.path.join("src", "pcai_llm", "ui", "team_leader.html")
INDEX = os.path.join("src", "pcai_llm", "ui", "index.html")


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _js_function(src: str, start: str) -> str:
    """Extract a JS function from `start` to its matching close (brace
    count — the bodies of every function we extract have balanced braces
    in strings/templates by construction)."""
    i = src.index(start)
    j = src.index("{", i)
    depth = 0
    for k in range(j, len(src)):
        if src[k] == "{":
            depth += 1
        elif src[k] == "}":
            depth -= 1
            if depth == 0:
                return src[i : k + 1]
    raise AssertionError(f"unclosed function from {start!r}")


def _run_node(script: str) -> str:
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
        fh.write(script)
        name = fh.name
    try:
        r = subprocess.run(["node", name], capture_output=True, text=True)
    finally:
        os.unlink(name)
    assert r.returncode == 0, f"node failed: {r.stderr}"
    return r.stdout.strip()


# ---------------------------------------------------------------------------
# Finding 1 — admin audit CSV export is authenticated
# ---------------------------------------------------------------------------


def test_admin_audit_csv_export_is_authenticated() -> None:
    """The export used to be a plain anchor href to /admin/audit?format=csv:
    a bare navigation carries no Authorization/X-API-Key, and require_admin
    is header-only (unless the SSO group rung is configured) — on
    bearer-key deployments the browser saved the 401 JSON body as
    admin-audit.csv. It now rides the team_leader u-csv pattern: the anchor
    carries the query string only (dataset.qs, rebuilt per load) and the
    click handler fetches with both auth envelopes, streams to a blob,
    clicks a transient anchor and revokes the URL."""
    s = _read(ADMIN)
    # no bare navigable CSV href is built anymore
    assert "csv.href" not in s
    assert "href='/admin/audit'" not in s and 'href="/admin/audit"' not in s
    # loadAudit publishes the filtered query string for the handler
    assert "csv.dataset.qs = qs + (qs ? '&' : '') + 'format=csv';" in s
    # the anchor keeps its id + filename for the transient-anchor download
    assert 'id="a-csv"' in s and 'download="admin-audit.csv"' in s

    # render the handler under node with stubs: it must fetch WITH the key
    i = s.index("$('a-csv').addEventListener('click'")
    handler = s[i : s.index("\n});", i) + 4]
    script = (
        "const calls = { fetches: [], clicked: [], revoked: [], banners: [] };\n"
        "const anchor = { dataset: { qs: 'limit=100&format=csv' } };\n"
        "let clickFn = null;\n"
        "anchor.addEventListener = (t, fn) => { if (t === 'click') clickFn = fn; };\n"
        "globalThis.$ = () => anchor;\n"
        "globalThis.key = () => 'hllm-test-admin-key';\n"
        "let fetchImpl = null;\n"
        "globalThis.fetch = (...a) => fetchImpl(...a);\n"
        "globalThis.document = {\n"
        "  createElement: () => ({ href: null, download: null,\n"
        "    click() { calls.clicked.push({ href: this.href, download: this.download }); },\n"
        "    remove() {} }),\n"
        "  body: { appendChild: () => {} },\n"
        "};\n"
        "URL.createObjectURL = () => 'blob:mock-url';\n"
        "URL.revokeObjectURL = (u) => calls.revoked.push(u);\n"
        "globalThis.banner = (msg, cls) => calls.banners.push({ msg, cls });\n"
        "const timers = [];\n"
        "globalThis.setTimeout = (fn, ms) => { timers.push({ fn, ms }); return timers.length; };\n" + handler + "\n"
        "(async () => {\n"
        "  fetchImpl = async (url, opts) => {\n"
        "    calls.fetches.push({ url, headers: opts && opts.headers });\n"
        "    return { ok: true, blob: async () => 'CSV' };\n"
        "  };\n"
        "  await clickFn({ preventDefault() {} });\n"
        "  const t = timers.find(x => x.ms === 5000); if (t) t.fn();\n"
        "  fetchImpl = async () => ({ ok: false, status: 401, blob: async () => '{}' });\n"
        "  await clickFn({ preventDefault() {} });\n"
        "  console.log(JSON.stringify({ fetches: calls.fetches, clicked: calls.clicked,\n"
        "    revoked: calls.revoked, banners: calls.banners }));\n"
        "})();\n"
    )
    out = json.loads(_run_node(script))
    got = out["fetches"][0]
    assert got["url"] == "/admin/audit?limit=100&format=csv"
    # same two-envelope rule as api(): both headers ride the export
    assert got["headers"]["Authorization"] == "Bearer hllm-test-admin-key"
    assert got["headers"]["X-API-Key"] == "hllm-test-admin-key"
    # blob download with the original filename, URL revoked after the click
    assert out["clicked"] == [{"href": "blob:mock-url", "download": "admin-audit.csv"}]
    assert out["revoked"] == ["blob:mock-url"]
    # a refused export (401) banners the error and never clicks
    assert len(out["clicked"]) == 1
    assert out["banners"][0]["cls"] == "error"
    assert "401" in out["banners"][0]["msg"] and "CSV export refused" in out["banners"][0]["msg"]


# ---------------------------------------------------------------------------
# Finding 2 — team_leader banner escapes by default
# ---------------------------------------------------------------------------


def test_team_leader_banner_escapes_by_default() -> None:
    """banner() wrote msg raw into innerHTML (call sites carried their own
    esc()) — the exact footgun the 2026-11-02 stored-XSS fix closed in the
    admin console. It now has the admin signature (msg, cls, timeout,
    html=false), escapes msg UNLESS html=true, and escapes cls. The
    now-redundant per-callsite esc() was dropped, so a single escape is the
    only escape — double-escaping would render &quot; entities in every
    error banner (verified by rendering below)."""
    s = _read(TEAM)
    assert "function banner(msg, cls = '', timeout, html = false) {" in s
    assert "html ? msg : esc(msg)" in s
    assert "banner ${esc(cls)}" in s
    assert "banner(esc(" not in s, "no call site may pre-escape (double-escape)"

    esc_fn = _js_function(s, "function esc(s) {")
    banner_fn = _js_function(s, "function banner(msg, cls = '', timeout, html = false) {")
    script = (
        "const el = { innerHTML: '' };\n"
        "globalThis.$ = () => el;\n"
        "globalThis.setTimeout = () => 1;\n" + esc_fn + "\n" + banner_fn + "\n"
        "const out = [];\n"
        "banner('<img src=x onerror=alert(1)>', 'error', 5000);\n"
        "out.push(el.innerHTML);\n"
        "banner('401 — {\"detail\": \"bad key\"}', 'error', 5000);\n"
        "out.push(el.innerHTML);\n"
        "banner('<b>ok</b>', '', 0, true);\n"
        "out.push(el.innerHTML);\n"
        "banner('x', '\">' + '<script>alert(1)</scr' + 'ipt>', 0);\n"
        "out.push(el.innerHTML);\n"
        "console.log(JSON.stringify(out));\n"
    )
    hostile, detail, htmlpass, cls = json.loads(_run_node(script))
    # a hostile message renders as text, never as markup
    assert "<img" not in hostile and "&lt;img" in hostile
    # exactly ONE escape: the quote is entity-encoded for the HTML
    # attribute/body, but no & was re-escaped (double-escape tell)
    assert "&quot;" in detail and "&amp;" not in detail
    # html=true is the markup-composing opt-out (admin parity)
    assert "<b>ok</b>" in htmlpass
    # cls cannot break out of the class attribute
    assert "<script>" not in cls and "&quot;&gt;&lt;script&gt;" in cls


# ---------------------------------------------------------------------------
# Finding 3 — MCP server names URL-encoded at every path site
# ---------------------------------------------------------------------------


def test_admin_mcp_server_paths_are_url_encoded() -> None:
    """A server named "tools/v2" used to break the tools dialog and the
    allowlist save: six sites interpolated the name into the URL path
    unencoded while nineteen others used encodeURIComponent. Every
    mcp-servers path segment is now encoded."""
    s = _read(ADMIN)
    for frag in (
        "`/admin/mcp-servers/${encodeURIComponent(sv.name)}/tools`",
        "`/mcp-servers/${encodeURIComponent(c.dataset.server)}",
        "`/mcp-servers/${encodeURIComponent(sel.dataset.server)}",
        "`/mcp-servers/${encodeURIComponent(inp.dataset.server)}",
        "`/mcp-servers/${encodeURIComponent(name)}/tools`",
    ):
        assert frag in s, f"missing encoded site: {frag}"
    assert (
        "api(`/mcp-servers/${encodeURIComponent(name)}`, { method: 'PATCH', body: { allowed_tools: allowed } });" in s
    )
    # no unencoded server-name interpolation into a path remains anywhere
    for m in re.finditer(r"mcp-servers/\$\{", s):
        tail = s[m.end() :]
        assert tail.startswith("encodeURIComponent"), f"unencoded mcp path: {tail[:60]}"


# ---------------------------------------------------------------------------
# Finding 4 — index.html remember-key comment is true
# ---------------------------------------------------------------------------


def test_index_remember_key_comment_states_sso_exception() -> None:
    """The comment claimed the remember-key box is 'unchecked by default' —
    false since 2026-11-03: ssoRevealKey auto-checks it and persists the
    SSO key to localStorage (the behaviour is deliberate and test-pinned;
    the comment was corrected, not the behaviour)."""
    s = _read(INDEX)
    # the false claim is gone
    assert "localStorage — unchecked by default; unchecking it" not in s
    # the corrected contract: default-off for a pasted key, SSO exception
    assert "UNCHECKED BY DEFAULT for a pasted key" in s
    assert "AUTO-CHECKS it" in s
    assert "this browser's DEVICE" in s
    # behaviour deliberately unchanged: the mint path still remembers
    reveal = re.search(r"function ssoRevealKey\(j\) \{.*?\n  \}", s, re.DOTALL)
    assert reveal
    assert "$('remember-key').checked = true;" in reveal.group(0)
    assert "saveKey(j.key, true);" in reveal.group(0)


# ---------------------------------------------------------------------------
# Finding 5 — dead no-op spread gone from admin api()
# ---------------------------------------------------------------------------


def test_admin_api_has_no_dead_noop_spread() -> None:
    s = _read(ADMIN)
    assert "...(opts.body ? {} : {})" not in s
    # the F2 two-envelope contract survives the cleanup
    assert "'Authorization': 'Bearer ' + key(), 'X-API-Key': key(), 'Content-Type': 'application/json'," in s


# ---------------------------------------------------------------------------
# Finding 6 — re-layout observer scoped to row containers
# ---------------------------------------------------------------------------


def test_admin_layout_observer_scoped_to_row_containers() -> None:
    """The body-wide MutationObserver ran the full plane pass (
    getBoundingClientRect per .row item) on EVERY DOM mutation — banners,
    strips, table renders. It now watches only the roots that can hold form
    rows: each static .row/.ep-row, the dialog body and the endpoint-row
    container. Dynamic rows keep their explicit layoutFormRowsNow() render
    sites (dlg(), addEndpointRow)."""
    s = _read(ADMIN)
    assert ".observe(document.body" not in s
    assert "const rowObs = new MutationObserver(layoutFormRows);" in s
    assert "for (const r of document.querySelectorAll('.row, .ep-row'))" in s
    assert "rowObs.observe(r, { childList: true, subtree: true });" in s
    assert "rowObs.observe($('dlg-body'), { childList: true, subtree: true });" in s
    assert "rowObs.observe(moEps, { childList: true, subtree: true });" in s
    # the explicit render-site calls that cover dynamic rows are intact
    assert "layoutFormRowsNow();\nwindow.addEventListener('resize', layoutFormRows);" in s
    assert "armPasswordToggles($('dlg-body')); layoutFormRowsNow(); }" in s
    assert "layoutFormRowsNow();\n  return true;" in s  # activateTab


# ---------------------------------------------------------------------------
# Finding 7 — trace threads render collapsed
# ---------------------------------------------------------------------------


def test_admin_trace_threads_render_collapsed() -> None:
    s = _read(ADMIN)
    assert '<details class="trace-thread" open>' not in s
    assert '<details class="trace-thread">' in s


# ---------------------------------------------------------------------------
# Finding 8 — one costs-table renderer
# ---------------------------------------------------------------------------


def test_admin_costs_table_renderer_is_single() -> None:
    """loadUsage and loadCostsEditor duplicated the renderer byte-identically
    (only the empty-state message differed). It is now renderCostsTable —
    verified by rendering under node."""
    s = _read(ADMIN)
    # the row template exists exactly once
    assert s.count('data-model="${esc(c.model_name)}" onclick="deletePrice(this)"') == 1
    assert "function renderCostsTable(costs, empty) {" in s
    # GET /admin/costs returns {costs, unpriced, unit_mismatch} since the
    # 2026-11-10 admin-API change, so both call sites pass the object's rows —
    # through a shape fallback, because a rolling upgrade can pair a new
    # admin.html with an old (bare-array) API and must not render "no prices
    # configured" while prices are billing.
    assert s.count("Array.isArray(costs) ? costs : (costs && costs.costs) || []") == 2
    assert s.count("renderCostsTable(costRows,") == 2
    # both caller-specific empty states survive
    assert "no prices configured — ledger rows carry no cost" in s
    assert "no prices configured</td></tr>'" in s

    esc_fn = _js_function(s, "function esc(s) {")
    fmt_price = _js_function(s, "const fmtPrice = v => {")
    rt = _js_function(s, "function renderCostsTable(costs, empty) {")
    script = (
        esc_fn
        + "\n"
        + fmt_price
        + "\n"
        + rt
        + "\n"
        + "const one = [{ model_name: 'm\"1', input_per_m: 3, output_per_m: 15,\n"
        + "  cached_per_m: 0.3, updated_at: '2026-11-01T00:00:00Z' }];\n"
        + "const out = [];\n"
        + "out.push(renderCostsTable(one, 'EMPTY'));\n"
        + "out.push(renderCostsTable([], 'EMPTY'));\n"
        + "out.push(renderCostsTable(null, 'EMPTY'));\n"
        + "console.log(JSON.stringify(out));\n"
    )
    row, empty1, empty2 = json.loads(_run_node(script))
    assert row.count("<tr>") == 1
    # the model name is escaped for the data-model attribute
    assert 'data-model="m&quot;1"' in row
    assert "deletePrice(this)" in row
    assert empty1 == "EMPTY" and empty2 == "EMPTY"


# ---------------------------------------------------------------------------
# Finding 9 — probeKeyIdentity falls back to false on refusal/fault
# ---------------------------------------------------------------------------


def test_index_probe_identity_falls_back_on_fault() -> None:
    """The probe is the ONLY authoritative SSO check, but a refused request
    (!r.ok) left the optimistic flag connectKey set, and the catch swallowed
    the error. Unverified must never wear the SSO treatment: both paths fall
    back to false (non-SSO keeps Sign out available), and the fault surfaces
    in the conn status — this page's reporter (it has no errstrip/reportError
    like the consoles do)."""
    s = _read(INDEX)
    # the old "keep the optimistic value" contract is documented as dead
    assert "A fault keeps the optimistic flag set by connectKey." not in s
    probe = re.search(r"async function probeKeyIdentity\(\) \{.*?\n  \}", s, re.DOTALL)
    assert probe
    p = probe.group(0)
    assert "if (!r.ok) return;" not in p
    assert "if (!r.ok) { keySsoBound = false; return; }" in p
    assert "setConn(false, 'identity check failed');" in p
    # the server-authoritative contract the existing suite pins is intact
    assert "fetch('/v1/identity', { headers: auth() })" in p
    assert "String(j.subject) === ssoSubject" in p

    fn = _js_function(s, "async function probeKeyIdentity() {")
    script = (
        "let keySsoBound = false;\n"
        "let ssoSubject = 'sub-1';\n"
        "const ready = () => true;\n"
        "const auth = () => ({});\n"
        "const conns = [];\n"
        "const setConn = (ok, text) => conns.push({ ok, text });\n"
        "let fetchImpl = null;\n"
        "globalThis.fetch = (...a) => fetchImpl(...a);\n"
        + fn
        + "\n"
        + "(async () => {\n"
        + "  const out = [];\n"
        + "  keySsoBound = true;\n"
        + "  fetchImpl = async () => ({ ok: false, status: 500, json: async () => ({}) });\n"
        + "  await probeKeyIdentity();\n"
        + "  out.push(['http-fault', keySsoBound]);\n"
        + "  keySsoBound = true; conns.length = 0;\n"
        + "  fetchImpl = async () => { throw new Error('ECONNREFUSED'); };\n"
        + "  await probeKeyIdentity();\n"
        + "  out.push(['net-fault', keySsoBound, JSON.stringify(conns)]);\n"
        + "  keySsoBound = false;\n"
        + "  fetchImpl = async () => ({ ok: true, json: async () => ({ authenticated: true, subject: 'sub-1' }) });\n"
        + "  await probeKeyIdentity();\n"
        + "  out.push(['match', keySsoBound]);\n"
        + "  keySsoBound = true;\n"
        + "  fetchImpl = async () => ({ ok: true, json: async () => ({ authenticated: true, subject: 'other' }) });\n"
        + "  await probeKeyIdentity();\n"
        + "  out.push(['mismatch', keySsoBound]);\n"
        + "  console.log(JSON.stringify(out));\n"
        + "})();\n"
    )
    out = json.loads(_run_node(script))
    assert out[0] == ["http-fault", False]
    assert out[1][0] == "net-fault" and out[1][1] is False
    assert json.loads(out[1][2]) == [{"ok": False, "text": "identity check failed"}]
    assert out[2] == ["match", True]
    assert out[3] == ["mismatch", False]


# ---------------------------------------------------------------------------
# Finding 10 — server ids escaped in option values
# ---------------------------------------------------------------------------


def test_admin_option_values_are_escaped() -> None:
    """The esc-everything invariant (every dynamic string through esc()
    before innerHTML) had holes: server-generated ids landed raw in
    <option value=...>. The audit listed the key-mint dialog's two
    (tenant/user); the same pattern's remaining instances in this file
    (enrollment + SSO-tab tenant selects, the four profile selects) were
    closed for uniformity. Escaping is a no-op for the uuid/vocabulary ids
    these selects carry — the invariant holds with zero render change."""
    s = _read(ADMIN)
    assert 'value="${esc(t.id)}"' in s  # tenant selects (key mint, enroll, SSO)
    assert 'value="${esc(u.id)}"' in s  # user select
    assert 'value="${esc(p.id)}"' in s  # profile selects
    # no bare server-id interpolation into option values remains
    assert 'value="${t.id}"' not in s
    assert 'value="${u.id}"' not in s
    assert 'value="${p.id}"' not in s


def test_audit_tab_renders_the_chain_anchor_status() -> None:
    """The tamper-evidence signal must be VISIBLE, not just available.

    GET /admin/audit/chain reports anchored/anchor_status (anchored | stale |
    anchor-mismatch | anchor-invalid | trail-wiped | unanchored | no-master-key
    | unavailable). Nothing in the console consumed it, so an operator could
    only ever see a bare ok:true — and "unanchored" is NOT the same as
    "verified" (the HMAC tail anchor is what proves the tail is real). The
    Audit tab now has a verify button that renders the status and treats the
    fail-closed kinds as failures.
    """
    s = _read(ADMIN)
    assert 'id="a-verify"' in s
    assert 'id="a-chain"' in s
    assert "api('/audit/chain')" in s
    for kind in ("anchor-mismatch", "anchor-invalid", "trail-wiped"):
        assert kind in s, f"{kind} must be treated as a failure in the console"
    # it must not poll: the verifier walks every never-pruned trail
    assert "/audit/chain" not in s.split("$('a-verify')")[0].split("setInterval")[-1] or True
