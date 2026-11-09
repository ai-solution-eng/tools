"""Team leader console: file-level wiring assertions (no browser).

String assertions over the two self-contained HTML pages — the new
team_leader.html and the surgical team-tier edits inside admin.html —
plus the one-line subject fix in index.html. They pin the UI to the
.scratch/team-leader/CONTRACT.md route table, because the backend
routes are built in parallel against that same contract:

* team_leader.html exists, is self-contained (no external assets — the
  CSP allows only 'unsafe-inline'), carries the four tabs, the #tlkey
  input, Bearer auth against /team_leader/*, the 401 clear-key path,
  and the esc() XSS discipline.
* admin.html: a <- Chat link IMMEDIATELY LEFT of the admin key input, an
  Enrollment column in the Keys table (cell + search haystack), the
  enrollment mint form's budget inputs + leader-key checkbox wired into
  the POST body, the shown-once leader key in the mint dialog, and the
  Enrollments table's Budget / Allocated / Spend / Leader key / Keys
  columns with the rotate (== mint) / revoke handlers.
* index.html: the enroll redeem POST carries the verified SSO subject
  when (and only when) the browser holds one.
"""

from __future__ import annotations

import os

ADMIN = os.path.join("src", "pcai_llm", "ui", "admin.html")
TEAM = os.path.join("src", "pcai_llm", "ui", "team_leader.html")
INDEX = os.path.join("src", "pcai_llm", "ui", "index.html")


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


# ---------------------------------------------------------------------------
# team_leader.html — existence + self-containment
# ---------------------------------------------------------------------------


def test_every_dollar_lookup_has_a_matching_element_id() -> None:
    """A $('id') with no element in the markup throws at the first write —
    "can't access property 'textContent', $(...) is null" — and kills the rest
    of the render. That is exactly what a span removed from the Usage tab did
    to this console on 2026-10-30 (u-meta): the tab loaded, the tables never
    rendered, and nothing said why. Static check: every id the script looks
    up must exist in the document."""
    import re

    for name, path in (("team_leader", TEAM), ("admin", ADMIN)):
        s = _read(path)
        # Every inline block, not the greedy first-to-last span: the theme wave's
        # pre-paint <script> in <head> made the old greedy re.search swallow the
        # markup between blocks. Union the lookups across all of them — a
        # non-greedy re.search alone would only ever inspect the head script and
        # the check would pass vacuously.
        blocks = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", s, re.DOTALL)
        assert blocks, f"{name}: no inline script block"
        looked_up = set()
        for block in blocks:
            looked_up |= set(re.findall(r"\$\('([^']+)'\)", block))
        present = set(re.findall(r'id="([^"]+)"', s))
        missing = sorted(looked_up - present)
        assert not missing, f"{name}: $() with no element in the markup: {missing}"


def test_team_leader_page_exists_and_is_self_contained() -> None:
    """The console is ONE file served at /team_leader — no external assets.

    The gateway CSP allows only 'unsafe-inline' (no 'unsafe-evals', no
    external origins), so any http(s) reference, <script src>, or external
    stylesheet would render the page dead or strip it. The favicon is a
    data: URI (copied from admin.html).
    """
    assert os.path.exists(TEAM), "team_leader.html is missing from the UI directory"
    s = _read(TEAM)
    assert "http://" not in s, "external http asset reference — the CSP will block it"
    assert "https://" not in s, "external https asset reference — the CSP will block it"
    assert "<script src" not in s
    assert '<link rel="stylesheet"' not in s
    assert "<script>" in s and "<style>" in s


def test_team_leader_has_the_four_tabs() -> None:
    """Keys / Advertised / Usage / Traces — the console's entire surface.

    Four sticky-nav buttons, four sections, in that order: keys is the
    default (it is what a leader was given the key FOR), and advertised
    is the enrollment-blind full registry view.
    """
    s = _read(TEAM)
    for tab in ("keys", "advertised", "usage", "traces"):
        assert f'data-tab="{tab}"' in s, f"missing nav button for {tab}"
        assert f'id="tab-{tab}"' in s, f"missing section for {tab}"
    # keys is the default-active tab (its loader runs at connect, not on
    # tab activation)
    assert 'data-tab="keys" class="active"' in s


def test_team_leader_key_input_and_storage() -> None:
    """#tlkey in the header, persisted in sessionStorage under the
    team-tier key name — the same iframe-safe try/catch discipline as the
    admin key (pcai-llm-theme for the shared theme).
    """
    s = _read(TEAM)
    assert 'id="tlkey"' in s
    assert 'placeholder="Team leader key"' in s
    assert "pcai_team_leader_key" in s
    assert "pcai-llm-theme" in s, "must share the theme key with the admin console"
    assert "pcai-team-leader-tab" in s, "tab-restore needs its own sessionStorage slot"
    # the <- Chat link: left of the title, back to the chat console
    assert 'href="/"' in s
    assert "Back to the chat console" in s


def test_team_leader_calls_the_contract_endpoints() -> None:
    """Every request goes to /team_leader/* with the Bearer header.

    The api() helper prefixes the route and injects Authorization:
    Bearer <key> — a 401 anywhere clears the key (storage AND input)
    and restores the connect prompt, so a dead key can never masquerade
    as live. The contract's full route table must be reachable from
    this page.
    """
    s = _read(TEAM)
    assert "fetch('/team_leader' + path" in s
    assert "'Authorization': 'Bearer '" in s
    # the 401 path: clear + connect prompt
    assert "r.status === 401" in s and "loseAuth()" in s
    # overview — the always-visible budget card
    assert "'/overview'" in s
    # keys: list, mint, patch, budgets (replace-all PUT), per-key usage, delete
    assert "'/keys'" in s
    assert "'/keys' + showRevoked" in s
    assert "'/keys', { method: 'POST'" in s
    assert "/keys/${id}`, { method: 'PATCH'" in s
    assert "/keys/${id}/budgets`, { method: 'PUT'" in s
    assert "/keys/${id}/usage`" in s
    assert "/keys/${id}`, { method: 'DELETE'" in s
    # usage: the SAME three rollup tables the admin console renders (totals /
    # by model / by key) come from /usage/summary scoped server-side to this
    # enrollment, plus the filtered ledger + CSV (authenticated fetch, never a
    # bare anchor — a navigation carries no Authorization header).
    assert "'/usage/summary?since='" in s
    assert "usage-summary" in s and "usage-by-model" in s and "usage-by-key" in s
    assert "usage-window" in s
    assert "format=csv" in s
    # traces
    assert "'/traces'" in s
    # advertised: the FULL presenter payload
    assert "'/presented-models'" in s


def test_team_leader_escapes_dynamic_strings() -> None:
    """XSS discipline: the page renders server strings (names, subjects,
    enrollment labels) — every one of them through esc(), which must
    escape the quote characters for attribute safety (audit-#3).
    """
    s = _read(TEAM)
    assert "function esc(s)" in s
    assert "&amp;" in s and "&lt;" in s
    assert "&quot;" in s and "&#39;" in s


# ---------------------------------------------------------------------------
# admin.html — surgical team-tier edits
# ---------------------------------------------------------------------------


def test_admin_chat_link_before_admin_key_input() -> None:
    """The <- Chat anchor sits IMMEDIATELY LEFT of the admin key input —
    the operator's first move is usually back to the chat console.

    Anchor on the unique title attribute (other href="/" links exist in
    the page) and assert document order before id="adminkey".
    """
    s = _read(ADMIN)
    assert 'title="Back to the chat console"' in s
    assert s.index('title="Back to the chat console"') < s.index('id="adminkey"')


def test_admin_keys_table_has_enrollment_column() -> None:
    """Keys table: an Enrollment cell after the User column.

    The projection (core.keys.list_keys' LEFT JOIN on enrollments) is
    NULL for keys minted outside the enroll flow — the cell renders an
    em dash, and the label joins the search haystack so operators can
    find "all keys of team X" by typing the enrollment label.
    """
    s = _read(ADMIN)
    # header, in the Keys thead (Name | User | Enrollment | Key | Subject ...)
    assert "Enrollment</th><th>Key</th>" in s
    # the cell: pill when the label is present, honest dash when NULL
    assert "k.enrollment_label ?" in s
    assert 'title="Minted from this enrollment code"' in s
    # the haystack + the placeholder both learn the word
    assert "k.user_profile_name, k.enrollment_label" in s
    assert "subject, or enrollment…" in s
    # the empty-state colspan grew by exactly one column
    assert '<tr><td colspan="12" class="muted">no keys yet</td></tr>' in s


def test_admin_enroll_mint_form_sends_team_fields() -> None:
    """The mint form carries the enrollment envelope inputs and the
    'also mint a team leader key' checkbox — all of them in the POST
    body (explicit booleans: these set the enrollment's own state).
    """
    s = _read(ADMIN)
    for elem in ("e-budget-tokens", "e-budget-usd", "e-budget-period", "e-overflow", "e-with-leader"):
        assert f'id="{elem}"' in s, f"missing mint-form element {elem}"
    # period is a select: day / week / month
    assert '<option value="month">' in s and '<option value="week">' in s
    # the body construction
    for field in (
        "body.budget_tokens",
        "body.budget_usd",
        "body.budget_period",
        "body.overflow_enabled",
        "body.with_team_leader",
    ):
        assert field in s, f"mint POST body missing {field}"
    # overflow defaults to ON (the checkbox ships checked)
    assert 'id="e-overflow" checked' in s
    # the leader key comes back shown-ONCE next to the code, with its own
    # copy button — the same convention as the code itself
    assert "j.team_leader_key ?" in s
    assert 'id="e-leader-key"' in s and 'id="e-leader-copy"' in s


def test_admin_enrollments_table_team_columns_and_leader_controls() -> None:
    """Enrollments table: Budget / Allocated / Spend / Leader key / Keys
    columns, and the leader-key cells hitting the contract endpoints.

    There is no separate mint endpoint — POST .../team-leader/rotate
    MINTS when no active key exists, so the cell shows "mint" when no
    prefix is present and "rotate" + "revoke" when it is (rotate/revoke
    behind confirm, both via withBtn so a double-click cannot double-fire).
    """
    s = _read(ADMIN)
    assert (
        'Budget</th><th title="Sum of the per-key allocations of the keys minted so far (the budgets rows)">Allocated</th>'
        in s
    )
    assert 'Spend</th><th title="Team leader key' in s
    assert '>Leader key</th><th title="Keys minted from this enrollment">Keys</th>' in s
    # the projections render with honest dashes, never fake zeros
    assert "r.allocated_tokens != null" in s
    assert "r.spend_tokens != null" in s
    assert "r.key_count != null" in s
    assert "r.budget_tokens || r.budget_usd" in s
    # the leader-key handler: one function, three actions, both endpoints
    assert "window.leaderKey =" in s
    assert "/team-leader/rotate`" in s
    assert "/team-leader/revoke`" in s
    assert "r.team_leader_key_prefix" in s
    # the shown-once reveal dialog for mint/rotate
    assert 'id="lk-once"' in s and 'id="lk-copy"' in s
    # the empty-state colspan follows the TENANT toggle (2026-10-30): the
    # column hides itself while every row is the default tenant
    assert 'colspan="${colspan}"' in s and "const colspan = showTenant ? 13 : 12;" in s


def test_enroll_row_is_a_team_edit_revoke_delete() -> None:
    """2026-10-30 feedback wave: the enrollment row IS a team now.

    * ACTIONS: edit sits on the LEFT and revoke on the RIGHT of an active
      row (the destructive action is no longer the only one on offer); a
      REVOKED row keeps only delete, wired to DELETE /admin/enrollments/{id}
      with a confirm that says what happens to minted keys;
    * USES: the value is plain nowrap text — as a squeezed button it wrapped
      onto two lines;
    * BUDGET: a derived cap (mint allowance x the profile preset) is MARKED,
      not shown as an unexplained number;
    * TENANT: the column hides itself while every row is the default tenant.
    """
    s = _read(ADMIN)
    # the editors exist; the buried uses-button editor is gone
    assert "window.editEnrollment" in s and "window.deleteEnrollment" in s
    assert "enrollMaxUses" not in s
    # the editor dialog PATCHes the envelope endpoint with the editKey
    # conventions (blank = unchanged, explicit-null expiry clear)
    assert "api(`/enrollments/${id}`, { method: 'PATCH', body })" in s
    assert 'id="ee-clear"' in s and 'id="ee-overflow"' in s
    # ACTIONS cell: revoked ? delete : edit + revoke, revoke on the right
    act = s[s.index("const actions = r.revoked_at") :]
    act = act[: act.index("return `<tr>")]
    assert "deleteEnrollment(this" in act, "revoked rows offer delete"
    assert "editEnrollment(" in act and "revokeEnrollment(this" in act
    assert act.index("editEnrollment(") < act.index("revokeEnrollment(this"), "revoke on the RIGHT of edit"
    # delete wiring + the confirm that states the key consequence
    assert "method: 'DELETE'" in s
    assert "lose the team grouping" in s
    # uses: plain nowrap text, never a wrapped button
    assert '<td class="nw">' in s
    # derived caps are marked, never bare numbers
    assert "r.budget_derived" in s and "(derived)" in s
    # tenant columns (both tables) hide while everything is fleet
    assert "col-tenant" in s and "col-ttenant" in s
    assert "!== 'fleet'" in s
    # the delete endpoint's audit action is filterable in the Audit tab
    assert "'enroll.delete'" in s


# ---------------------------------------------------------------------------
# index.html — the redeem POST carries the SSO subject
# ---------------------------------------------------------------------------


def test_index_redeem_sends_sso_subject_when_known() -> None:
    """Redeeming an invite code from an SSO-authenticated browser binds the
    key to the VERIFIED subject — provenance, not a guess. When no SSO
    identity is present (plain key pasted) the body is unchanged: subject
    stays optional, exactly as before.
    """
    s = _read(INDEX)
    assert "const body = { code, name };" in s
    # the redeem dialog no longer sells the legacy private-dataset opt-in
    assert "$('enroll-memory')" not in s
    assert "if (ssoSeen && ssoSubject) body.subject = ssoSubject;" in s
    assert "body: JSON.stringify(body)" in s


def test_index_sign_out_navigates_to_the_gateway_owned_route() -> None:
    """Closeout (wave 3): Sign out clears the local key and then NAVIGATES to
    /oauth2/sign_out, which the GATEWAY owns and always answers with a 302 —
    to the platform oauth2-proxy's sign-out when PCAI_LLM_SSO_SIGN_OUT_URL is
    configured, else back to /?signout=local. The wave-2 fetch probe is gone
    because it existed only while that path could 404 (a blind navigation to
    a may-404 URL stranded users on the gateway's JSON error page with the
    SSO cookie alive). The honest local-only note is now rendered at BOOT
    from the signout=local query param, not from inside signOut().
    """
    s = _read(INDEX)
    assert "function signOut()" in s
    assert "location.href = '/oauth2/sign_out'" in s, (
        "the gateway route always redirects, so navigating is the correct move"
    )
    assert "edgeSignOut" not in s, "the probe is dead — the route can no longer 404"
    assert "redirect: 'manual'" not in s
    assert "signout=local" in s
    assert "history.replaceState" in s
    so = s[s.index("function signOut()") : s.index("async function ssoProbeIdentity()")]
    assert so.index("clearKey()") < so.index("location.href = '/oauth2/sign_out'"), (
        "the local key is always cleared FIRST — before the sign-out navigation (w6 F1: clearKey drops both stores + the remember flag)"
    )
    assert "signoutNote(" in s
    assert 'id="signout-note"' in s, "the header needs its transient note surface"


def test_index_chip_identity_and_provenance_lifecycle() -> None:
    """2026-11-03 owner report: "it should show signed in as andrew-bydlon"
    and "switching to SSO does not make any sense".

    The chip now always names the probe's identity when one resolved, and the
    PILL reports provenance ('oidc' vs 'API key'). The cosmetic mode links are
    gone: only ONE credential is stored, so a "switch" could only relabel the
    chip. keyMode still records which credential is active (an SSO-minted key
    keeps the SSO identity), and Sign out is offered whenever a key is
    connected — the old rule hid it for every session on an SSO deployment,
    including a pasted admin key.
    """
    s = _read(INDEX)
    assert "let keyMode = false" in s
    # Bug B's rule survives, with the 2026-11-03 SSO exception: an SSO-MINTED
    # key keeps the SSO identity (the identity that authorizes it IS the edge
    # identity), so the mode is connectKey(opts.sso) rather than a bare
    # keyMode=true. A pasted/connected key still flips the provenance pill.
    assert "keyMode = !(opts && opts.sso);" in s
    assert "an SSO-MINTED key keeps the SSO identity" in s
    assert "a dead key must not hold the chip in API-key mode" in s
    # identity + provenance, and no fake switches
    # SSO mode is the ACTIVE credential's binding (server-verified by
    # probeKeyIdentity), not the connect path — so a pasted key bound to the
    # identity gets the same treatment as a minted one.
    assert "const ssoBound = keyed && keySsoBound;" in s
    assert "probeKeyIdentity()" in s
    assert "signout.hidden = !(keyed && !ssoBound);" in s
    assert "use-key-link" not in s and "use-sso-link" not in s


# ---------------------------------------------------------------------------
# wave 2 (2026-10-30): the corrected, spend-aware budget model
# ---------------------------------------------------------------------------


def test_team_leader_overflow_settings_patch() -> None:
    """Wave 2 (2): the TEAM LEADER controls auto-overflow.

    PATCH /team_leader/settings with an explicit boolean, then re-read the
    overview — optimistic-free: a failed PATCH must surface the error and
    revert the checkbox, never leave the toggle lying. The checkbox mirrors
    the overview flag (overflow_enabled !== false: absent = on, the
    backend default).
    """
    s = _read(TEAM)
    assert 'id="ovr-overflow-tog"' in s
    assert "'/settings', { method: 'PATCH', body: { overflow_enabled: desired }" in s
    # optimistic-free: the PATCH precedes the overview re-read, and the
    # failure path reverts the checkbox and banners the error
    so = s[s.index("$('ovr-overflow-tog').onchange") : s.index("$('ovr-overflow-tog').onchange") + 900]
    assert so.index("api('/settings'" + "") < so.index("loadOverview()"), "PATCH before re-read"
    assert "tog.checked = !desired" in so, "failed PATCH must revert the toggle"
    assert "banner(esc(e.message), 'error', 9000)" in so


def test_team_leader_overview_is_spend_aware() -> None:
    """Wave 2 (1): the overview card renders the corrected model — per
    dimension, in order: Total (or "no cap") · Spent · Committed ·
    Available, with Available dominant; the bar fills with spent +
    committed vs total; available 0 says "pool exhausted" plainly; the
    null-cap "no cap" stays honest.
    """
    s = _read(TEAM)
    # the JS reads the new overview fields
    assert "o.committed || {}" in s
    assert "o.available || {}" in s
    assert "budgetBlock('tokens', b.tokens, sp.tokens, cm.tokens, av.tokens, fmtInt)" in s
    assert "budgetBlock('USD', b.usd, sp.usd, cm.usd, av.usd, fmtUSD)" in s
    # the per-kind line order: Total · Spent · Committed · Available
    block = s[s.index("function budgetBlock") : s.index("async function loadOverview")]
    assert block.index("Total <b") < block.index("Spent <b") < block.index("Committed <b") < block.index("Available ")
    # spent + committed fill the bar, not spend alone
    assert "const used = s + c;" in block
    assert "Math.min(1, used / Number(total))" in block
    # honest edges: exhausted pool says so, null cap says "no cap"
    assert "pool exhausted" in block
    assert "free budget or raise the cap with your admin" in block
    assert "no cap" in block


def test_team_leader_allocation_cell_and_409_verbatim() -> None:
    """Wave 2 (3): the keys table shows SPENT / ALLOCATED with a warn tint
    when spent > allocated (who is riding the pool), and the budgets
    dialog shows the CURRENT available pool before any number is typed —
    and the server's 409 (which now names cap, spent, committed,
    available, requested) must reach the banner VERBATIM: api() lifts
    j.detail into the error message and the save handler banners it.
    """
    s = _read(TEAM)
    # SPENT first, then the allocation (2026-10-30 order flip — preserved in
    # the w7 popup): "10,000 / 6,720" was read as "10,000 spent of a 6,720
    # cap" — an overrun — when it meant the opposite. Since w7 (owner
    # 2026-11-02) the cell itself carries no numbers — count + worst-state
    # colour — so these pins moved from the cell window to viewBudgets,
    # which still renders the per-budget spent/limit lines spent-first.
    vb = s[s.index("window.viewBudgets") : s.index("window.viewBudgets") + 1700]
    assert "tok spent of " in vb and "spending from the overflow pool" in vb
    assert "${fmtInt(b.spent)} / ${capTxt}" in vb, "spent must render FIRST"
    assert "${fmtInt(b.limit)} / " not in vb, "the ambiguous allocated-first form must not come back"
    # the cell's colour is the WORST state across the row's budgets — the
    # old per-row "over -> warn" tint is subsumed (an overrun is >=100% -> off)
    keys_fn = s[s.index("async function loadKeys") : s.index("async function loadKeys") + 2600]
    assert "budgetWorst(list)" in keys_fn
    # the budgets dialog shows the available pool BEFORE typing
    headroom = s[s.index("function envelopeHeadroomHtml") : s.index("window.tlBudgets")]
    assert "o.available || {}" in headroom
    assert "still available" in headroom
    assert "committed" in headroom
    # the 409 path: api() carries the server detail into e.message…
    assert "typeof j.detail === 'string' ? j.detail" in s
    # …and the budgets save handler banners that message verbatim
    save = s[s.index("$('tb-save').onclick") : s.index("$('tb-save').onclick") + 420]
    assert "banner(esc(e.message), 'error', 9000)" in save


def test_budget_envelope_card_lives_on_the_usage_tab() -> None:
    """2026-10-30: the envelope card was pinned above EVERY tab, so the
    Advertised and Traces pages carried a budget table with nothing to do.
    It belongs to the tab whose numbers it explains — and the tab switch
    must re-read it, because its numbers are spend-aware."""
    s = _read(TEAM)
    keys = s.index('<section id="tab-keys"')
    usage = s.index('<section id="tab-usage">')
    traces = s.index('<section id="tab-traces">')
    ovr = s.index('<div id="ovr"')
    assert ovr > keys, "the envelope card must no longer sit above the tab set"
    assert usage < ovr < traces, "the envelope card must sit inside the Usage tab"
    assert "loadOverview().catch" in s[s.index("if (name === 'usage')") : s.index("if (name === 'traces')")]


def test_half_capped_envelope_names_the_unbounded_dimension() -> None:
    """2026-10-30 (found live): the token envelope was derived and the bar
    sat at 50% of 48,000,000 while USD had no cap — and the note said "No
    team envelope is set", contradicting the bar above it, then quoted SPEND
    as an allocation sum. Half-capped is the COMMON case: profiles preset
    tokens, not USD."""
    s = _read(TEAM)
    assert "if (noTok && noUsd)" in s, "the both-uncapped case must be explicit"
    assert "else if (noTok || noUsd)" in s, "a half-capped envelope gets its own sentence"
    assert "dimension is unbounded; the " in s
    # a dimension with no allocations must never be quoted as a "sum"
    assert "if (allocTok > 0) parts.push" in s and "if (allocUsd > 0) parts.push" in s


def test_uncapped_envelope_names_the_sum_of_key_allocations() -> None:
    """2026-10-30: a bare "no cap" on the team envelope read as "the team is
    unlimited", but the gate returns the per-key verdict untouched when the
    enrollment has no cap of that kind — so every key is capped on its own
    and the real team ceiling is the SUM of their allocations. A leader with
    N keys on one profile expects "N x the key limits"; the card must say
    where the ceiling actually comes from."""
    s = _read(TEAM)
    assert 'id="ovr-nocap"' in s, "the no-cap note element must exist"
    assert "the team ceiling is the SUM of" in s
    # alloc = committed + spent (committed is clamped at 0, so this is the
    # honest per-period total the console can compute from the overview)
    assert "(Number(cm.tokens) || 0) + (Number(sp.tokens) || 0)" in s
    assert "(Number(cm.usd) || 0) + (Number(sp.usd) || 0)" in s


def test_admin_leader_key_health_pill() -> None:
    """Wave 2 (4): the Enrollments "Leader key" cell renders a health pill
    from team_leader_key_last_used_at / created_at, with the thresholds in
    NAMED constants at the top of the script (30d unused-warn, 90d
    rotate-nudge), and NO pill at all when the timestamps are absent
    (older backend / no leader key => plain dash).
    """
    s = _read(ADMIN)
    # named constants, near the top of the script block (well before the
    # enrollments loader that consumes them)
    assert "const LEADER_KEY_UNUSED_WARN_DAYS = 30;" in s
    assert "const LEADER_KEY_ROTATE_NUDGE_DAYS = 90;" in s
    assert s.index("const LEADER_KEY_UNUSED_WARN_DAYS = 30;") < s.index("function loadEnrollTab")
    # the projection field + the three states
    assert "team_leader_key_last_used_at" in s
    assert "never used" in s and "rotate?" in s and "used " in s
    # absent timestamps render nothing (no fake health on older backends)
    fn = s[s.index("function leaderKeyHealthHtml") : s.index("function leaderKeyHealthHtml") + 1400]
    assert "return '';" in fn
    # the cell wires the function into the ACTIVE leader branch
    assert "leaderKeyHealthHtml(r)" in s[s.index("const leaderCell") : s.index("const leaderCell") + 1200]


def test_admin_topup_queue_card_and_handlers() -> None:
    """The APPROVAL half of the top-up loop (wave 3). Approving is the only
    path by which a leader-initiated amount reaches an enrollment cap, so the
    card must show the enrollment's CURRENT cap next to the ask before the
    admin commits — the resulting total has to be visible, not inferred.

    The dialog's amount inputs are prefilled from the request and stay
    editable: that IS the override (grant less, or switch kinds).
    """
    s = _read(ADMIN)
    assert 'id="topup-table"' in s, "the pending-ask table"
    assert "/topup-requests?status=pending" in s
    assert "window.approveTopup" in s and "window.denyTopup" in s
    assert "/approve" in s and "/deny" in s
    # the current cap rides the approve dialog, so the new total is visible
    assert "Current cap:" in s
    # prefilled, editable amounts (the override)
    assert "req.requested_tokens ?? ''" in s and "req.requested_usd ?? ''" in s
    # wired into the Enroll tab refresh (caps change -> both refresh)
    assert "loadTopups()" in s
    # honest empty state, never a fake row
    assert "no pending top-up requests" in s


def test_team_console_topup_request_surface() -> None:
    """The REQUEST half: the leader files one open ask and sees its state.

    One open ask at a time is the server rule (409), so the button must be
    hidden while a request is pending — otherwise the console offers an action
    that can only fail.
    """
    s = _read(TEAM)
    assert 'id="ovr-topup"' in s
    assert "/topup-requests" in s
    assert "method: 'POST'" in s
    assert "top-up pending" in s
    assert "state.topups" in s


def test_team_console_sends_the_x_api_key_envelope() -> None:
    """Review P1 (2026-10-30): the team console MUST send X-API-Key, not only
    Authorization.

    Behind the platform SSO chain oauth2-proxy injects its OWN
    `Authorization: Bearer <id_token>` into every forwarded request,
    overwriting whatever the page set — so the leader key never arrives and
    every call 401s. routes_team_leader._leader_credential prefers X-API-Key
    for exactly this reason, and both sibling consoles already send it. A
    console that omits it cannot connect on a chained deployment at all.
    """
    s = _read(TEAM)
    assert "'X-API-Key': key()" in s, "the api() envelope must carry the leader key"
    # both fetch sites (api() and the CSV download) carry it
    assert s.count("'X-API-Key'") >= 2


def test_per_model_caps_surface_in_both_consoles() -> None:
    """2026-10-30, the operator's reading made real: a derived envelope is
    PER-MODEL — one cap per preset x N, rendered as its own line (admin)
    and its own filled bar (team leader), and the overflow language says
    each pool is the only one for its kind. The approve dialog warns that
    an explicit pooled cap REPLACES the per-model pools."""
    s = _read(ADMIN)
    assert "budget_caps" in s and "capline" in s
    assert "team pool = preset × mint allowance" in s
    assert "REPLACES those per-model pools" in s, "approve dialog warns about the shape switch"
    t = _read(TEAM)
    assert "function capBlock" in t
    assert "o.caps" in t and "Per-limit pools" in t
    assert "the ONLY overflow pool for its kind" in t


def test_billable_tokens_column_matches_the_budget_expression() -> None:
    """2026-10-30: the per-user usage tables must show the SAME number the
    team envelope counts — the budget expression (cached at the configured
    exemption x off-peak fraction). Without it, a team bar saying
    "Spent 37,581" sat beside raw per-user tokens and read as nonsense."""
    s = _read(ADMIN)
    t = _read(TEAM)
    for doc in (s, t):
        assert "billable_tokens" in doc
        assert "tokens charged against period budgets" in doc
        assert "the SAME number the team envelope" in doc
    # the admin per-pool lines carry their own spend
    assert "team pool = preset × mint allowance · spent" in s


def test_alloc_cell_is_one_worst_state_control_with_popup() -> None:
    """w7 (owner 2026-11-02): the ALLOCATION cell used to stack ONE PILL PER
    BUDGET — a key with 6 per-model budgets made its row ~400px tall. It is
    now ONE compact control (count label, worst-state colour) whose click
    opens EVERY budget in the existing #dlg, one line each. Colour = the
    worst state across the row's budgets using the same thresholds as
    admin's F9 burn-down column: >=95% is the state that REFUSES so it
    renders red (an over-allocation is >=100% → red, and the per-row
    "spending from the overflow pool" note is preserved in the dialog),
    >=80% warns, all <80% ok, no budgets → neutral dash.

    Behavioral pin: extract the real esc + the budgetPct/budgetState/
    viewBudgets block + the allocCell builder verbatim from the page and
    run them under node with fake dlg/fmt helpers — the pre-fix page has
    no viewBudgets and allocCell is a stacked-pills mapper, so extraction
    fails pre-fix."""
    import re
    import subprocess
    import tempfile

    s = _read(TEAM)
    esc_fn = re.search(r"function esc\(s\) \{.*?\n\}", s, re.DOTALL)
    assert esc_fn, "esc() not extractable"
    w7 = re.search(r"const budgetPct = b => \{.*?window\.viewBudgets = btn => \{.*?\n\};", s, re.DOTALL)
    assert w7, "the single-control budget lifecycle (budgetPct/budgetState/viewBudgets) not extractable"
    cell_fn = re.search(r"const allocCell = k => \{.*?\n  \};", s, re.DOTALL)
    assert cell_fn, "allocCell is not a single-control builder (still the stacked-pills mapper?)"

    harness = (
        "const t = (n, c) => { if (!c) { console.error('FAIL ' + n); process.exit(1); } };\n"
        "const window = {};\n"
        "const dlgCalls = [];\n"
        "const dlg = html => dlgCalls.push(html);\n"
        "const fmtInt = v => (v == null || v === '' ? '0' : String(Math.round(+v || 0)));\n"
        "const fmtUSD = v => (v == null ? '—' : '$' + Number(v).toFixed(4));\n"
    )
    calls = (
        "const B6 = [\n"
        "  { model: 'gpt-5.6', period: 'day', spent: 800, limit: 1000 },\n"
        "  { model: 'gpt-5.6', period: 'month', spent: 10, limit: 10000 },\n"
        "  { model: 'claude-sonnet-4-6', period: 'month', spent: 5, limit: 100 },\n"
        "  { model: 'gemini-3.5', period: 'day', kind: 'usd', spent_usd: 2, limit_usd: 10 },\n"
        "  { model: 'gpt-5.6', period: 'week', spent: 96, limit: 100 },\n"
        "  { model: 'o4-mini', period: 'month', spent: 1, limit: 10 }\n"
        "];\n"
        # (a) exactly ONE control for a 6-budget key — no stacked pills
        "const cell6 = allocCell({ name: 'K1', budget_status: B6 });\n"
        "t('a-one-button', (cell6.match(/<button/g) || []).length === 1);\n"
        "t('a-one-pill', (cell6.match(/class=\"pill/g) || []).length === 1);\n"
        "t('a-no-span-pills', cell6.indexOf('<span class=\"pill') === -1);\n"
        "t('a-count-label', cell6.indexOf('6 budgets') !== -1);\n"
        # (b) colour = worst state: <80 green, >=80 yellow, >=95 red, none neutral
        "t('b-green', /class=\"pill on\"/.test(allocCell({ name: 'g', budget_status: [{ model: 'm', period: 'd', spent: 1, limit: 100 }] })));\n"
        "t('b-warn80', /class=\"pill warn\"/.test(allocCell({ name: 'g', budget_status: [{ model: 'm', period: 'd', spent: 80, limit: 100 }] })));\n"
        "t('b-red96', /class=\"pill off\"/.test(allocCell({ name: 'g', budget_status: [{ model: 'm', period: 'd', spent: 96, limit: 100 }] })));\n"
        "t('b-worst-red', /class=\"pill off\"/.test(cell6));\n"
        "const none = allocCell({ name: 'n', budget_status: [] });\n"
        "t('b-neutral', none.indexOf('<button') === -1 && none.indexOf('muted') !== -1);\n"
        # (c) the popup: one line per budget, model + period + spent/limit, key name in the title
        "window.viewBudgets({ dataset: { name: 'K1', b: JSON.stringify(B6) } });\n"
        "const d = dlgCalls[dlgCalls.length - 1];\n"
        "t('c-title', d.indexOf('<h2>Budgets — K1</h2>') !== -1);\n"
        "t('c-rows', (d.match(/<span class=\"pill (on|warn|off)\"/g) || []).length === 6);\n"
        "t('c-model-period', d.indexOf('gpt-5.6 day') !== -1 && d.indexOf('claude-sonnet-4-6 month') !== -1);\n"
        "t('c-spent-limit', d.indexOf('800 / 1000 tok') !== -1 && d.indexOf('$2.0000 / $10.0000') !== -1);\n"
        # the team console's overflow note is preserved in the dialog (overrun → red + note)
        "window.viewBudgets({ dataset: { name: 'K2', b: JSON.stringify([{ model: 'gpt-5.6', period: 'month', spent: 120, limit: 100 }]) } });\n"
        "const ov = dlgCalls[dlgCalls.length - 1];\n"
        "t('overflow-note', ov.indexOf('spending from the overflow pool') !== -1 && ov.indexOf('120 tok spent of 100 tok allocated') !== -1);\n"
        "t('over-is-red', /class=\"pill off\"/.test(allocCell({ name: 'K2', budget_status: [{ model: 'm', period: 'd', spent: 120, limit: 100 }] })));\n"
        # (d) hostile model/period stays inert in the cell AND in the popup
        "const Q = String.fromCharCode(34);\n"
        "const hostile = { name: 'H', budget_status: [{ model: Q + '><img src=x onerror=alert(1)>' + Q, period: Q + '><script>window.pwned=1</script>' + Q, spent: 1, limit: 10 }] };\n"
        "const ch = allocCell(hostile);\n"
        "t('d-cell-inert', ch.indexOf('<img') === -1 && ch.indexOf('&lt;img') !== -1 && ch.indexOf('<script>') === -1);\n"
        "window.viewBudgets({ dataset: { name: 'H', b: JSON.stringify(hostile.budget_status) } });\n"
        "const dh = dlgCalls[dlgCalls.length - 1];\n"
        "t('d-dlg-inert', dh.indexOf('<img') === -1 && dh.indexOf('&lt;img') !== -1 && dh.indexOf('<script>') === -1 && dh.indexOf('pwned') !== -1);\n"
    )
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
        f.write(harness + esc_fn.group(0) + "\n" + w7.group(0) + "\n" + cell_fn.group(0) + "\n" + calls)
        path = f.name
    r = subprocess.run(["node", path], capture_output=True, text=True)
    assert r.returncode == 0, f"single-control alloc cell contract broken:\n{r.stdout}\n{r.stderr}"


def test_subject_cell_drops_the_default_provenance_chip() -> None:
    """w7 (owner request): the "self-asserted" chip is GONE from the subject
    cell, but the provenance DISTINCTION is not.

    "self-asserted" (typed at redemption) is the DEFAULT for enrolled keys, so
    a visible chip on every row marked the normal case as if it were
    noteworthy. The cell now states the provenance in the TITLE on every row
    and reserves the visible chip for a NON-default provenance — a verified or
    authoritative binding, which is the case an operator actually needs to
    spot. This pin guards both halves: the noise is gone AND the signal
    survives (removing both would silently make a claimed identity look like a
    verified one).

    Behavioural: subjectCell + esc are extracted verbatim from the page and
    run under node."""
    import re
    import subprocess
    import tempfile

    s = open("src/pcai_llm/ui/team_leader.html", encoding="utf-8").read()
    esc_fn = re.search(r"function esc\(s\) \{.*?\n\}", s, re.DOTALL)
    cell_fn = re.search(r"const subjectCell = k => \{.*?\n  \};", s, re.DOTALL)
    assert esc_fn and cell_fn, "esc/subjectCell not extractable from src/pcai_llm/ui/team_leader.html"
    # the default must not render a chip at all
    assert "self-asserted</span>" not in s, "the self-asserted chip is still rendered"

    harness = (
        esc_fn.group(0)
        + "\n"
        + cell_fn.group(0)
        + "\n"
        + "const t = (n, c) => { if (!c) { console.error('FAIL ' + n); process.exit(1); } };\n"
    )
    calls = (
        # DEFAULT: no visible chip, provenance in the title
        "const d = subjectCell({subject:'alice', subject_idp:'self-asserted', bound_via:'enrollment'});\n"
        't("no-chip-on-default", !/<span class="pill/.test(d));\n'
        't("no-self-asserted-text", !d.includes(">self-asserted<"));\n'
        't("title-states-claim", d.includes("a claim, not a verified identity"));\n'
        't("subject-rendered", d.includes(">alice<"));\n'
        # NON-DEFAULT: the chip is back, and it names the provenance
        "const v = subjectCell({subject:'bob', subject_idp:'oauth2-proxy', bound_via:'sso'});\n"
        't("chip-on-verified", /<span class="pill"/.test(v));\n'
        't("chip-names-provenance", v.includes(">oauth2-proxy<"));\n'
        't("chip-title-states-via", v.includes("bound via oauth2-proxy"));\n'
        # admin-asserted is also non-default
        "const a = subjectCell({subject:'carol', subject_idp:'admin-asserted'});\n"
        't("chip-on-admin-asserted", /<span class="pill"/.test(a));\n'
        # unbound stays a dash
        't("unbound-dash", subjectCell({subject:null}).includes("unbound"));\n'
        # escaping intact on the hostile subject in BOTH cell and title
        "const h = subjectCell({subject:'x\"><img src=x onerror=alert(1)>', subject_idp:'oauth2-proxy'});\n"
        't("subject-escaped", !/<img/i.test(h) && h.includes("&lt;img"));\n'
    )
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as fh:
        fh.write(harness + calls)
        path = fh.name
    r = subprocess.run(["node", path], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, f"subjectCell contract broken:\n{r.stdout}\n{r.stderr}"


def test_clear_button_uses_a_window_level_handler() -> None:
    """w7 BUG (reported): the key-filter "clear" button threw
    ReferenceError: $ is not defined and cleared nothing.

    Cause: the button was an INLINE onclick calling `$('k-search')`. `$` is a
    top-level `const`, which lives in the SCRIPT's lexical scope; an inline
    handler is evaluated against WINDOW, so it could not see it. `filterKeys`
    in the same attribute worked only because it is explicitly
    `window.filterKeys`. Any helper an inline attribute touches must be on
    window — that is the rule this pins.

    Two halves: no inline attribute handler may reference a bare `$`, and the
    window-level replacement really does clear the field and re-filter.
    Behavioural: the real `$` and `window.clearKeySearch` are extracted from
    the page and run under node against a fake document."""
    import re
    import subprocess
    import tempfile

    html = open("src/pcai_llm/ui/team_leader.html", encoding="utf-8").read()
    # CLASS GUARD: an inline handler may not touch the script-scoped `$`.
    offenders = re.findall(r'on[a-z]+="[^"]*\$\s*\(', html)
    assert not offenders, f"inline handler references the script-scoped $: {offenders}"

    dollar = re.search(r"const \$ = id => document\.getElementById\(id\);", html)
    clear = re.search(r"window\.clearKeySearch = \(\) => \{.*?\n\};", html, re.DOTALL)
    assert dollar and clear, "$/clearKeySearch not extractable from src/pcai_llm/ui/team_leader.html"
    assert 'onclick="clearKeySearch()"' in html, "the clear button must call the window-level handler"

    harness = (
        "globalThis.window = globalThis;\n"
        "let filtered = 0;\n"
        "globalThis.filterKeys = () => { filtered++; };\n"
        "const input = { value: 'needle' };\n"
        "const document = { getElementById: id => (id === 'k-search' ? input : null) };\n"
        "const t = (n, c) => { if (!c) { console.error('FAIL ' + n); process.exit(1); } };\n"
        + dollar.group(0)
        + "\n"
        + clear.group(0)
        + "\n"
        + "window.clearKeySearch();\n"
        't("field-cleared", input.value === "");\n'
        't("refiltered", filtered === 1);\n'
        # a missing input must not throw (the old handler guarded for it)
        "globalThis.document = { getElementById: () => null };\n"
        "window.clearKeySearch();\n"
        't("tolerates-missing-input", true);\n'
    )
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as fh:
        fh.write(harness)
        path = fh.name
    r = subprocess.run(["node", path], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, f"clearKeySearch contract broken:\n{r.stdout}\n{r.stderr}"


# ---------------------------------------------------------------------------
# Form rows: ONE horizontal plane per line (2026-11-03, r2 feedback)
# ---------------------------------------------------------------------------


def test_form_rows_share_one_control_plane() -> None:
    """The leader console's Mint form reported the same bump as admin.html's
    forms: the Subject / Expires-in-days hints pushed their inputs above the
    rest of the line. This page carries the SAME contract (align on the top of
    the control slot + measured --fhead-h/--hint-h/--ctl-top/--chk-top), wired
    on every render path."""
    s = _read(TEAM)
    assert ".row { display: flex; gap: 10px; flex-wrap: wrap; align-items: start; }" in s
    for rule in (
        ".row > label { position: relative; padding-bottom: var(--hint-h, 0px); }",
        ".row > *:not(label) { margin-top: var(--ctl-top, 0px); }",
        '.row > label:has(input[type="checkbox"]) { margin-top: var(--chk-top, 0px); }',
        ".row > label > .fhead, .ep-row > label > .fhead { min-height: var(--fhead-h, 0px); }",
        ".fhead { display: flex; align-items: center; gap: 8px; justify-content: space-between; }",
    ):
        assert rule in s, f"missing plane rule: {rule}"
    hint_rule = s[s.index(".row > label:not(.chk) > .hint {") :]
    hint_rule = hint_rule[: hint_rule.index("}")]
    assert "position: absolute" in hint_rule and "var(--hint-h, 0px)" in hint_rule

    assert "function structureFormRows(root) {" in s
    assert "function layoutFormRowsNow() {" in s
    assert "function layoutFormRows() {" in s
    assert "if (head) head.appendChild(btn);\n    else input.after(btn);" in s
    for var in ("--hint-h", "--fhead-h", "--ctl-top", "--chk-top"):
        assert f"el.style.setProperty('{var}'" in s, f"{var} never published"

    # every render path re-measures (a hidden section measures as 0)
    assert "$('dlg').showModal(); layoutFormRowsNow(); }" in s
    assert "layoutFormRowsNow();\n  return true;" in s  # activateTab
    assert "layoutFormRowsNow();\nwindow.addEventListener('resize', layoutFormRows);" in s
    assert "new MutationObserver(layoutFormRows).observe(document.body, { childList: true, subtree: true });" in s
    # the header key's toggle is the shared helper now, not a bespoke copy
    assert "armPasswordToggles(document);" in s
    assert "armHeaderShowPw" not in s


def test_the_two_consoles_share_the_same_plane_implementation() -> None:
    """admin.html and team_leader.html are separate self-contained pages, but
    the plane contract must not drift between them: the four helpers are
    compared function-by-function (comments/whitespace normalised). A fix in
    one console that is not mirrored in the other fails here."""
    import re

    def js_function(src: str, name: str) -> str:
        i = src.index(f"function {name}(")
        depth = 0
        for j in range(i, len(src)):
            if src[j] == "{":
                depth += 1
            elif src[j] == "}":
                depth -= 1
                if depth == 0:
                    return src[i : j + 1]
        raise AssertionError(f"{name} not closed")

    def norm(body: str) -> list[str]:
        out = []
        for line in body.splitlines():
            line = re.sub(r"//.*$", "", line).strip()
            if line:
                out.append(re.sub(r"\s+", " ", line))
        return out

    admin = _read(ADMIN)
    team = _read(TEAM)
    for name in ("structureFormRows", "armPasswordToggles", "layoutFormRowsNow", "layoutFormRows"):
        assert norm(js_function(admin, name)) == norm(js_function(team, name)), (
            f"{name} drifted between admin.html and team_leader.html"
        )
