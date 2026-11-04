"""Admin console: page serving + new API surface (profiles-as-defaults,
MCP grants, tenant/user reads, discovery move)."""

from __future__ import annotations

import json
import uuid
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from pcai_llm.api import routes_admin
from pcai_llm.config import settings
from pcai_llm.db import Database


class FakeConn:
    def __init__(self, db: FakeDB) -> None:
        self._db = db

    def transaction(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc: object):
        return False

    async def fetchrow(self, sql: str, *a: Any):
        return {"id": uuid.uuid4()}

    async def fetchval(self, sql, *a):
        # MIN(ts) for the retained-history label; None = empty ledger
        return None

    async def execute(self, sql: str, *a: Any):
        self._db.executed.append((sql, a))
        return "OK"

    async def executemany(self, sql: str, args: Any):
        self._db.executed.append((sql, list(args)))


class FakeDB:
    def __init__(self) -> None:
        self.executed: list[tuple] = []
        self.fetchrows: list[tuple[str, tuple]] = []
        self.users: dict[str, Any] = {}
        # The profile defaults the create-key path resolves. Settable per test
        # so the budget-preset cases can hand the route a dollar preset, a
        # token preset, or a deliberately invalid one (a row an older build
        # could have written).
        self.profile_defaults: dict[str, Any] = {
            "budgets": [{"model": "*", "period": "month", "limit_tokens": 100000}],
            "mcp_grants": [["logsearch", "search_logs"]],
        }

    def acquire(self):
        return FakeConn(self)

    def acquire_bounded(self):
        # The settlement path takes this; without it the real pool raises
        # 'database not connected' and /admin/usage/summary 500s.
        return FakeConn(self)

    async def fetchrow(self, sql: str, *a: Any):
        self.fetchrows.append((sql, a))
        if "FROM profiles" in sql:
            return {
                "id": uuid.uuid4(),
                "name": "power",
                "model_order": ["m1"],
                "defaults_json": json.dumps(self.profile_defaults),
            }
        if "FROM users" in sql:
            return {"id": a[0]}
        return {"id": uuid.uuid4()}

    async def fetch(self, sql: str, *a: Any):
        if "FROM tenants" in sql and "ORDER" in sql:
            return [{"id": uuid.uuid4(), "name": "t", "created_at": "now"}]
        if "FROM mcp_tool_acls" in sql:
            return [{"server": "logsearch", "tool": "search_logs", "allowed": True}]
        return []

    async def fetchval(self, sql: str, *a: Any):
        # MIN(ts) drives the retained-history label; None = empty ledger.
        if "min(ts)" in sql.lower():
            return None
        if "count(*)" in sql.lower():
            return 0
        return 1

    async def execute(self, sql: str, *a: Any):
        # delete-affected-rows emulation: the price-removal route keys its
        # 200-vs-404 off the DELETE tag; "qwen*" counts as priced, anything
        # else as absent (same sql-sniffing style as fetch above).
        if "DELETE FROM model_costs" in sql:
            return "DELETE 1" if a and "qwen" in str(a[0]).lower() else "DELETE 0"
        return "OK"


@pytest.fixture
def tc(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "admin_api_keys", "admin-secret")
    fake = FakeDB()
    monkeypatch.setattr(routes_admin, "db", fake)
    import pcai_llm.core.costs as costs_mod
    import pcai_llm.core.keys as keys_mod

    monkeypatch.setattr(keys_mod, "db", fake)
    monkeypatch.setattr(costs_mod, "db", fake)
    app_fastapi = __import__("fastapi").FastAPI()
    app_fastapi.include_router(routes_admin.router)
    return TestClient(app_fastapi, raise_server_exceptions=False), fake


def _h():
    return {"Authorization": "Bearer admin-secret"}


def test_console_and_help_split(tc) -> None:
    t, _ = tc
    # security fix: /admin/help is ADMIN-GATED now (it enumerated the whole
    # admin surface + the Secret name to anyone) — 401 without the key,
    # index with it.
    assert t.get("/admin/help").status_code == 401
    r = t.get("/admin/help", headers=_h())
    assert r.status_code == 200 and "endpoints" in r.json()
    # console itself is served by app.py — tested there


def test_create_key_with_profile_seeds_budgets_and_grants(tc) -> None:
    t, fake = tc
    r = t.post(
        "/admin/keys",
        json={"user_id": str(uuid.uuid4()), "name": "k", "scopes": ["proxy", "mcp"], "profile_id": str(uuid.uuid4())},
        headers=_h(),
    )
    assert r.status_code == 201, r.text
    assert r.json()["key"].startswith("hllm-")
    sqls = " || ".join(sql for sql, _ in fake.executed)
    assert "UPDATE users SET profile_id" in sqls
    assert "INSERT INTO budgets" in sqls
    assert "INSERT INTO mcp_tool_acls" in sqls


def test_mcp_grants_get_and_put(tc) -> None:
    t, _ = tc
    kid = str(uuid.uuid4())
    r = t.get(f"/admin/keys/{kid}/mcp-grants", headers=_h())
    # D-F1: GET returns both row kinds with an allowed flag.
    assert r.status_code == 200
    assert r.json() == [{"server": "logsearch", "tool": "search_logs", "allowed": True}]
    r2 = t.put(
        f"/admin/keys/{kid}/mcp-grants", json={"grants": [{"server": "applygate", "tool": "plan_apply"}]}, headers=_h()
    )
    assert r2.status_code == 200 and r2.json()["grants"] == 1
    # invalid period on profile must 400
    r3 = t.post(
        "/admin/profiles",
        json={"name": "bad", "budgets": [{"model": "*", "period": "fortnight", "limit_tokens": 5}]},
        headers=_h(),
    )
    assert r3.status_code == 400


def test_tenants_and_users_reads(tc) -> None:
    t, _ = tc
    assert t.get("/admin/tenants", headers=_h()).status_code == 200
    assert t.get(f"/admin/tenants/{uuid.uuid4()}/users", headers=_h()).status_code == 200


def test_profile_create_roundtrips_defaults(tc) -> None:
    t, _ = tc
    r = t.post(
        "/admin/profiles",
        json={
            "name": "power",
            "model_order": ["m1", "m2"],
            "budgets": [{"model": "*", "period": "month", "limit_tokens": 1000000}],
            "mcp_grants": [{"server": "logsearch", "tool": "search_logs"}],
        },
        headers=_h(),
    )
    assert r.status_code == 201, r.text  # upsert-by-name is a creation here
    j = r.json()
    assert j["budgets"][0]["limit_tokens"] == 1000000
    assert j["mcp_grants"] == [["logsearch", "search_logs"]]


def test_delete_profile_guard_and_force(tc) -> None:
    t, _ = tc
    pid = str(uuid.uuid4())
    # no users attached -> deletes straight away
    r = t.delete(f"/admin/profiles/{pid}", headers=_h())
    assert r.status_code == 200 and r.json()["detached_users"] == 0

    # users attached -> 409 unless force
    class AttachedDB(FakeDB):
        async def fetchval(self, sql: str, *a: Any):
            if "FROM users" in sql and "count" in sql.lower():
                return 3
            return 1

    import pcai_llm.api.routes_admin as ra

    orig = ra.db
    # swap in the attached-db fixture temporarily
    fake_attached = AttachedDB()
    ra.db = cast(Database, fake_attached)  # test double implementing the Database surface
    try:
        r409 = t.delete(f"/admin/profiles/{pid}", headers=_h())
        assert r409.status_code == 409 and "3 attached" in r409.json()["detail"]
        rforced = t.delete(f"/admin/profiles/{pid}?force=true", headers=_h())
        assert rforced.status_code == 200 and rforced.json()["detached_users"] == 3
    finally:
        ra.db = orig


def test_costs_endpoints_and_summary(tc) -> None:
    t, _fake = tc
    r = t.post(
        "/admin/costs",
        json={"model_name": "qwen38", "input_per_m": 0.5, "output_per_m": 1.5, "cached_per_m": 0.1},
        headers=_h(),
    )
    assert r.status_code == 200 and r.json()["priced"]
    assert t.get("/admin/costs", headers=_h()).status_code == 200
    r_bad = t.post("/admin/costs", json={"model_name": "q", "input_per_m": -1}, headers=_h())
    # ge=0 moved into the pydantic Field (audit P1-1, NaN-proof): the contract is now 422
    assert r_bad.status_code == 422
    assert t.get("/admin/usage/summary", headers=_h()).status_code == 200
    # price removal (the "renamed model leaves a ghost price forever" fix):
    # the {model_name:path} converter must carry slash-containing names — a
    # plain path param 404s them before the handler ever runs (the same trap
    # DELETE /models documents). Unpriced names 404; the route is
    # admin-gated like every other admin surface.
    assert t.delete("/admin/costs/Qwen/Qwen3.8-27B-FP8", headers=_h()).json()["deleted"]
    assert t.delete("/admin/costs/never-priced", headers=_h()).status_code == 404
    assert t.delete("/admin/costs/qwen38").status_code == 401


def test_settle_computes_cost_and_excludes_cache_hit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cost math: uncached input + output + cached rates; cache_hit rows
    ledger with cost but the BUDGET sums exclude them."""
    import asyncio
    import uuid as u

    from pcai_llm.core import budgets as bm
    from pcai_llm.core import costs as cm

    class CostDB:
        def __init__(self):
            self.rows = []

        async def fetch(self, sql, *a):
            if "FROM model_costs" in sql:
                return [{"model_name": "m", "input_per_m": 0.5, "output_per_m": 1.5, "cached_per_m": 0.1}]
            return []

        async def execute(self, sql, *a):
            self.rows.append(a)
            return "OK"

        async def fetchrow(self, sql, *a):
            return None

        async def fetchval(self, sql, *a):
            if "min(ts)" in sql.lower():
                return None
            return 0

    fake = CostDB()
    monkeypatch.setattr(bm, "db", fake)
    monkeypatch.setattr(cm, "db", fake)
    kid = u.uuid4()
    asyncio.run(bm.settle(kid, "m", "chat", 1000000, 500000, "ok", 5, "r", cached_tokens=400000))
    # per-1M: (1M-0.4M)/1M*0.5 + 0.5M/1M*1.5 + 0.4M/1M*0.1 = 0.3 + 0.75 + 0.04
    row = fake.rows[-1]
    assert abs(float(row[7]) - 1.09) < 0.0001
    # a cache_hit row: visible, zero-budget
    asyncio.run(bm.settle(kid, "m", "embeddings", 800000, 0, "cache_hit", 2, "r2", cached_tokens=800000))
    hit = fake.rows[-1]
    assert hit[8] == "cache_hit" and float(hit[7]) == 0.0


def test_console_full_key_ux_and_rotate_wiring() -> None:
    """Key UX (the "prefix is useless" feedback): the console must surface
    the FULL key at create + rotate — selectable monospace field, one-click
    copy with a non-secure-context fallback, and the shown-once /
    cannot-recover warning — while the keys list stays prefix-only WITH a
    muted shown-once hint. Rotate must hit POST /admin/keys/{id}/rotate
    (server revokes the old key; the old client-side re-issue flow, which
    kept the old key alive and renamed the key '<name>-rotated', is gone)."""
    s = open("src/pcai_llm/ui/admin.html", encoding="utf-8").read()
    # shown-once reveal: explicit warning + copy affordance + fallback path
    assert "Shown only once — copy it now; it cannot be recovered." in s
    assert "navigator.clipboard.writeText" in s
    assert "execCommand('copy')" in s
    assert 'id="copy-new-key"' in s
    # rotate wired to the server endpoint; old client-side flow fully removed
    assert "await api(`/keys/${id}/rotate`, { method: 'POST' })" in s
    assert "reissueKey" not in s and "doReissue" not in s
    # confirm dialog: the old key stops working immediately
    assert "OLD KEY STOPS WORKING IMMEDIATELY" in s
    # keys list: prefix-only display + the muted shown-once hint where it renders
    assert (
        "key_prefix" in s and 'title="The full key was shown exactly once' in s and "key-prefix-hint" not in s
    )  # hint demoted to a hover title (was per-row visual noise)
    # the rotate handler must be exported for the inline onclick registry
    assert "rotateKey" in s


def test_console_profile_routing_knobs() -> None:
    """Profile routing rollout surfaces in the console: a policy select
    (off/shadow/active) + a min-model input with the ladder hint, both in the
    save body, loaded back on edit (legacy rows predate the columns), and
    rendered in the profiles table — plus the ~60s cache hint, because
    core.model_routing TTL-caches profile_routing_config per user (a save is
    not instantly live on the proxy path)."""
    s = open("src/pcai_llm/ui/admin.html", encoding="utf-8").read()
    # editor controls exist with the exact operator-facing hint
    assert 'id="p-routing-policy"' in s
    for v in ("off", "shadow", "active"):
        assert f'<option value="{v}"' in s
    assert 'id="p-routing-min"' in s
    # the operator-facing hint carries the same two facts it always did
    # (2026-10-30 copy pass moved it under the input)
    assert "One of the profile's models. Empty = no downgrade." in s
    assert "~60s" in s  # the proxy-cache staleness hint
    # save body carries both fields
    assert "routing_policy: $('p-routing-policy').value" in s
    assert "routing_min_model: $('p-routing-min').value.trim()" in s
    # edit path loads them back with the API-equivalent defaults
    assert "$('p-routing-policy').value = p.routing_policy || 'off'" in s
    assert "$('p-routing-min').value = p.routing_min_model || ''" in s
    # profiles table renders the rollout state (escaped, like every cell)
    assert "esc(p.routing_policy)" in s and "esc(p.routing_min_model)" in s


def test_console_tool_descriptions_render_rich_and_safe() -> None:
    """The Tools dialog renders MCP tool DESCRIPTIONS through the escape-first
    markdown helper (they are upstream-MCP semi-trusted content carrying
    markdown shapes), while tool NAMES stay plain esc() text. The helper must
    escape BEFORE any transform, restrict link URLs to https? with no
    quotes/parens/whitespace, and pass through no raw HTML."""
    s = open("src/pcai_llm/ui/admin.html", encoding="utf-8").read()
    # description goes through the rich renderer; the plain-esc path is gone
    # (match the ${…} interpolation so the renderToolDesc call itself, which
    # contains the same substring, doesn't false-positive)
    assert "renderToolDesc(t.description)" in s
    assert "${esc(t.description)}" not in s
    # tool names stay escaped plain text in the same row template
    assert '<td class="mono">${esc(t.tool)}</td>' in s
    # helper exists with the trust comment + escape-first structure
    assert "function renderToolDesc" in s
    assert "UPSTREAM-MCP SEMI-TRUSTED" in s
    assert "esc(raw)" in s  # per-line escape inside the helper
    assert r"(https?:\/\/[^\s)]+)" in s  # charset-restricted URL pattern

    # BEHAVIORAL: extract esc + renderToolDesc from the page and run them
    # under node against the security + rendering contract. Escape-first is
    # the invariant that makes the transforms safe, so it is asserted on real
    # output, not just on source shape.
    import json
    import re
    import subprocess
    import tempfile

    esc_fn = re.search(r"function esc\(s\) \{.*?\n\}", s, re.DOTALL)
    rich_fn = re.search(r"function renderToolDesc\(text\) \{.*?\n\}", s, re.DOTALL)
    assert esc_fn and rich_fn, "esc/renderToolDesc not extractable from admin.html"
    cases = [
        # hostile markup renders as inert TEXT, never as markup
        ("script", "<script>x</script>", "&lt;script&gt;x&lt;/script&gt;"),
        ("img-onerror", "<img src=x onerror=alert(1)>", "&lt;img src=x onerror=alert(1)&gt;"),
        # attribute breakout impossible: the upstream quote is already &quot;
        # (parsed as attribute DATA, not a delimiter) before the URL regex runs
        (
            "quote-in-url",
            '[x](https://a"b)',
            '<a href="https://a&quot;b" target="_blank" rel="noopener noreferrer">x</a>',
        ),
        # non-http(s) schemes never linkify
        ("js-scheme", "[x](javascript:alert(1))", "[x](javascript:alert(1))"),
        # the shapes tool descriptions actually use
        (
            "shapes",
            "Use `s` for **logs** — [docs](https://e.com/d)",
            (
                "Use <code>s</code> for <b>logs</b> — "
                '<a href="https://e.com/d" target="_blank" rel="noopener noreferrer">docs</a>'
            ),
        ),
        # consecutive `- ` lines group into one list; a plain line closes it
        ("list-grouping", "- a\n- b\nplain\n- c", "<ul><li>a</li><li>b</li></ul><br>plain<ul><li>c</li></ul>"),
        (
            "list-link",
            "- item with [link](https://e.com)",
            '<ul><li>item with <a href="https://e.com" target="_blank" rel="noopener noreferrer">link</a></li></ul>',
        ),
        ("empty", "", ""),
        ("null", None, ""),
    ]
    harness = "const t = (n, got, want) => { if (got !== want) { console.error(`FAIL ${n}: got ${got} want ${want}`); process.exit(1); } };\n"
    calls = "".join(
        f"t({json.dumps(n)}, renderToolDesc({json.dumps(inp)}), {json.dumps(want)});\n" for n, inp, want in cases
    )
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
        f.write(esc_fn.group(0) + "\n" + rich_fn.group(0) + "\n" + harness + calls)
        path = f.name
    r = subprocess.run(["node", path], capture_output=True, text=True)
    assert r.returncode == 0, f"renderToolDesc behavior mismatch:\n{r.stdout}\n{r.stderr}"


def test_console_tab_restore_and_usage_refresh() -> None:
    """Browser-reload UX: a page refresh must keep the operator on their tab
    (the console hardcodes Keys active in the markup, so a refresh always
    stranded them there — refreshing 'usage' took tab-click + re-filter). Tab
    switching now runs through ONE shared activateTab() (nav clicks and boot
    restore, so a restored tab lazy-loads exactly like a click), the last tab
    is remembered in the SAME iframe-safe sessionStore as the admin key (fresh
    sessions still open on Keys; invalid/renamed values repair to Keys), and
    restore runs inside the auto-connect path after the key input is
    populated (restored-tab loaders fire real fetches). The Usage tab gains a
    ↻ refresh button and its ledger user-filter also persists across reloads
    (pendingUser seed honored by the first fetch, which races ahead of
    connect()'s keys load). Client-only — no API/schema changes."""
    s = open("src/pcai_llm/ui/admin.html", encoding="utf-8").read()

    # shared activation path: nav clicks and boot restore both go through it
    assert "function activateTab(name)" in s
    assert "activateTab(b.dataset.tab)" in s
    assert "restoreSavedTab()" in s
    # the old inline-only handler body is gone (no duplicate loader wiring)
    assert "if (b.dataset.tab === 'usage')" not in s

    # persistence through the guarded session store, not localStorage
    assert "sessionStore.set('pcai-admin-tab', b.dataset.tab)" in s
    assert "sessionStore.get('pcai-admin-tab')" in s
    # invalid/renamed tabs fall back to the default view, never a dead page
    assert "sessionStore.set('pcai-admin-tab', 'keys')" in s
    # restore runs only in the auto-connect path (key input populated first)
    assert "restoreSavedTab();\n  connect();" in s

    # usage refresh button: exists, wired, styled like the other tab reloads
    assert 'id="u-reload"' in s
    assert "$('u-reload').addEventListener('click'" in s
    # page-wide refresh: the button sits at the TOP of the tab (by the Totals
    # heading, above every table it reloads) and re-runs the SAME loaders tab
    # activation does — pricing editor included, not just the ledger.
    assert s.index('id="u-reload"') < s.index('id="usage-summary"')
    assert "loadCostsEditor().then(() => loadUsage())" in s
    # ledger user filter survives a reload (restored after repopulation)
    assert "sessionStore.set('pcai-admin-usage-user'" in s
    assert "pcai-admin-usage-user" in s and "dataset.pendingUser" in s


def test_usage_summary_wire_contract_json_numbers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Chargeback totals must cross the wire as JSON NUMBERS, not strings.

    SUM(bigint) in Postgres yields numeric; asyncpg decodes that as Decimal
    and the JSON layer stringifies it — the console received
    "4427047307" (quoted) and its String.toLocaleString() rendered the
    Totals row comma-less while the ::float billed_tokens grouped fine.
    The endpoint now casts the token sums ::bigint so they serialize as
    plain ints; the browser formats whatever arrives through (+v) anyway.
    """
    import json as _json

    class RollupDB:
        """Decodes like asyncpg WITH the casts in place: ::bigint -> int,
        ::float -> float, count(*) -> int. (Pre-fix, SUM(bigint) decoded as
        Decimal and Pydantic v2 stringified it in JSON mode.)"""

        async def fetchrow(self, sql: str, *a: Any):
            assert "::bigint" in sql, "token sums must be cast ::bigint in the totals query"
            return {
                "requests": 35233,
                "total_tokens": 4591938230,
                "input_tokens": 4427047307,
                "output_tokens": 16150923,
                "cached_tokens": 4389349248,
                "cache_hit_requests": 0,
                "cost_usd": 145.5972,
                "billed_tokens": 3860581391.0,
                "billed_cost_usd": 126.5955,
            }

        async def fetchval(self, sql, *a):
            # MIN(ts) for the retained-history label; None = empty ledger
            return None

        async def fetch(self, sql: str, *a: Any):
            if "GROUP BY model" in sql:
                assert "::bigint" in sql, "by-model token sums must be ::bigint too"
            if "GROUP BY k.name" in sql:
                assert "::bigint" in sql, "by-key token sums must be ::bigint too"
            return []

    fake = RollupDB()
    monkeypatch.setattr(settings, "admin_api_keys", "admin-secret")
    orig = routes_admin.db
    routes_admin.db = cast(Database, fake)  # type: ignore[assignment]
    try:
        app_fastapi = __import__("fastapi").FastAPI()
        app_fastapi.include_router(routes_admin.router)
        t = TestClient(app_fastapi, raise_server_exceptions=False)
        r = t.get("/admin/usage/summary", headers=_h())
        assert r.status_code == 200
        totals = r.json()["totals"]
        # ints (requests/cache hits/::bigint sums) are JSON numbers, never quoted
        for field in (
            "requests",
            "cache_hit_requests",
            "total_tokens",
            "input_tokens",
            "output_tokens",
            "cached_tokens",
        ):
            assert isinstance(totals[field], int), (
                f"{field} crossed the wire as {type(totals[field])}: {_json.dumps(totals[field])}"
            )
        # USD rollups stay floats via the existing ::float casts
        assert isinstance(totals["cost_usd"], float) and isinstance(totals["billed_cost_usd"], float)
    finally:
        routes_admin.db = orig


def test_console_usage_integer_formatting_node() -> None:
    """Behavioral: the console's integer cells group through (+v).toLocaleString()
    regardless of wire shape — the comma-less Totals row came from a string's
    no-op .toLocaleString(); every numeric cell now coerces first."""
    import json
    import re
    import subprocess
    import tempfile

    s = open("src/pcai_llm/ui/admin.html", encoding="utf-8").read()
    fmt_fn = re.search(r"const fmtInt = v => .*?;", s, re.DOTALL)
    assert fmt_fn, "fmtInt helper not extractable from admin.html"
    harness = "const t = (n, got, want) => { if (got !== want) { console.error(`FAIL ${n}: got ${got} want ${want}`); process.exit(1); } };\n"
    cases = [
        # the historical bug shape: SUM(bigint) stringified by the JSON layer
        ("string-from-decimal", "4427047307", "4,427,047,307"),
        # numeric shapes (the ::bigint cast / legacy ::float paths)
        ("number", 3860581391, "3,860,581,391"),
        ("float-integer", 3860581391.0, "3,860,581,391"),
        # small + boundary values stay exact
        ("small", 35233, "35,233"),
        ("zero", 0, "0"),
        ("cache-hits-zero", "0", "0"),
        # null/undefined/empty render as plain 0, never 'NaN'/'undefined'
        ("null", None, "0"),
        ("undefined", None, "0"),
        ("empty", "", "0"),
    ]
    calls = "".join(f"t({json.dumps(n)}, fmtInt({json.dumps(inp)}), {json.dumps(want)});\n" for n, inp, want in cases)
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
        f.write(fmt_fn.group(0) + "\n" + harness + calls)
        path = f.name
    r = subprocess.run(["node", path], capture_output=True, text=True)
    assert r.returncode == 0, f"fmtInt behavior mismatch:\n{r.stdout}\n{r.stderr}"


# ---------------------------------------------------------------------------
# Regression (review round: usage_summary blocker): the three rollup queries
# must be f-strings — a literal {_cond} in the SQL text is a Postgres syntax
# error on EVERY summary call, and the fake-DB seam cannot see it.
# ---------------------------------------------------------------------------


def test_usage_summary_queries_are_interpolated_and_windowed(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    from pcai_llm.api import routes_admin as ra

    captured: list[tuple[str, str, tuple]] = []

    class CapDB:
        async def fetchrow(self, sql, *args):
            captured.append(("fetchrow", sql, args))
            return {}

        async def fetchval(self, sql, *a):
            # MIN(ts) for the retained-history label; None = empty ledger
            return None

        async def fetch(self, sql, *args):
            captured.append(("fetch", sql, args))
            return []

    monkeypatch.setattr(ra, "db", CapDB())
    asyncio.run(ra.usage_summary(None, since=None))
    # FOUR queries since 2026-10-26: totals, by_model, by_key, and the
    # per-key-per-model breakdown that powers the expandable rows.
    assert len(captured) == 4
    for kind, sql, args in captured:
        assert "{" not in sql, f"{kind}: literal brace in SQL (not an f-string): {sql[:120]}"
        assert len(args) == 2, f"{kind}: expected the 2 window params, got {len(args)}"
        assert "WHERE ts >=" in sql or "WHERE l.ts >=" in sql, f"{kind}: window condition missing"
    # the breakdown must GROUP BY model or it cannot split a key's traffic
    assert any("GROUP BY k.name, u.name, l.model" in sql for _k, sql, _a in captured)


# ---------------------------------------------------------------------------
# Regression (review note: the two _bounded_window/_parse_since 400 branches
# were live-verified but unpinned). Both belong to the P1-4/P2-6 fix set.
# ---------------------------------------------------------------------------


def test_admin_summary_rejects_oversized_window(tc) -> None:
    # 366-day cap: an old explicit since is a 400 (a typo, not an intent)
    t, _f = tc
    r = t.get("/admin/usage/summary", headers=_h(), params={"since": "2020-01-01T00:00:00Z"})
    assert r.status_code == 400
    assert "window too large" in r.text


def test_admin_summary_rejects_naive_since(tc) -> None:
    # audit P2-6: a tz-naive datetime would be container-local when asyncpg encodes it
    t, _f = tc
    r = t.get("/admin/usage/summary", headers=_h(), params={"since": "2025-01-01T00:00:00"})
    assert r.status_code == 400
    assert "UTC offset" in r.text


def test_console_distiller_model_dropdown() -> None:
    """The distiller panel's summarizer model is a DROPDOWN of the registered
    router pools, not freeform text: a typo used to ship a model name the
    distill engine only refused at distill time (DistillUnavailable). Pinned:
    * the control is a <select id="di-model"> (the freeform input is gone);
    * the empty option stays \"(calling key's own routing)\" — null clears the
      override back to the env default, byte-identical to the old save body;
    * options carry esc()d values (model names are admin-controlled but the
      escape-first convention is not waived);
    * a configured model that is NOT currently registered still renders,
      marked — saving the panel must never silently rewrite an override the
      operator set;
    * loadModels() re-paints the select, so pools registered after the panel
      first painted still appear (the DOMContentLoaded paint runs before
      connect() populates state.models);
    * the save handler still reads .value and maps '' -> null."""
    s = open("src/pcai_llm/ui/admin.html", encoding="utf-8").read()
    assert '<select id="di-model"' in s
    assert '<input id="di-model"' not in s  # the freeform path is gone
    assert "(calling key\\'s own routing)" in s  # the JS string escapes the apostrophe
    assert "paintDiModelSelect" in s
    # esc()d option values + the not-registered preservation branch
    assert "+ esc(m) +" in s and "+ esc(cur) +" in s  # both option branches escape
    assert "(not registered)</option>" in s
    # the painter keys off the SAME registry snapshot the Models tab renders
    assert "Array.isArray(state.models)" in s
    # loadModels keeps the dropdown in sync (best-effort — never fails the tab)
    assert "try { paintDiModelSelect(); } catch (e) {}" in s
    # save body unchanged: '' clears the override, non-empty writes the name
    assert "model: modelRaw === '' ? null : modelRaw" in s


# ---------------------------------------------------------------------------
# Dollar-amount budget presets (2026-10-20)
# ---------------------------------------------------------------------------
# The KEY-level budget API has accepted limit_usd since 0.2, but the PROFILE
# preset path was token-only: a key minted from a profile (admin create,
# enrollment redeem, SSO self-mint) could never RECEIVE a dollar budget, so
# "give this tenant $20/month" meant hand-editing every key afterwards. These
# tests pin the whole chain: the preset validates, round-trips, and lands in
# cost_budgets (not budgets) on every seeding path.


def test_profile_accepts_usd_preset_and_roundtrips(tc) -> None:
    """POST /admin/profiles accepts a limit_usd preset, stores ONLY that key
    (exclude_none — a reader branches on presence), and returns it."""
    t, fake = tc
    r = t.post(
        "/admin/profiles",
        json={
            "name": "dollar-team",
            "model_order": ["m1"],
            "budgets": [
                {"model": "*", "period": "month", "limit_usd": 20},
                {"model": "m1", "period": "day", "limit_tokens": 5000},
            ],
        },
        headers=_h(),
    )
    assert r.status_code == 201, r.text
    budgets = r.json()["budgets"]
    usd = [b for b in budgets if "limit_usd" in b]
    tok = [b for b in budgets if "limit_tokens" in b]
    assert len(usd) == 1 and len(tok) == 1
    assert usd[0]["limit_usd"] == 20.0 and usd[0]["period"] == "month"
    # exclude_none: the dollar preset must NOT carry a limit_tokens key (and
    # vice versa) — otherwise every reader needs a null test instead of a
    # presence test.
    assert "limit_tokens" not in usd[0] and "limit_usd" not in tok[0]
    # the stored defaults_json carries both kinds, each reduced to its ONE
    # limit key (the upsert is a fetchrow: the row IS the RETURNING value).
    upserts = [a for sql, a in fake.fetchrows if "INSERT INTO profiles" in sql]
    assert upserts, "profile upsert never executed"
    stored = json.loads(upserts[0][2])["budgets"]
    assert any("limit_usd" in b and "limit_tokens" not in b for b in stored)
    assert any("limit_tokens" in b and "limit_usd" not in b for b in stored)


def test_profile_budget_preset_needs_exactly_one_limit(tc) -> None:
    """Both limits or neither is a 400 — the same contract the live-key
    route enforces, so a profile can never hold a preset the key route would
    refuse."""
    t, _ = tc
    for bad in (
        # both limits / neither: the ROUTE refuses (400, via the shared
        # splitter — the same validator the redeem runs on stored JSON).
        ({"model": "*", "period": "month", "limit_usd": 20, "limit_tokens": 1000}, 400),
        ({"model": "*", "period": "month"}, 400),
        ({"model": "*", "period": "fortnight", "limit_usd": 5}, 400),
        # a non-positive value is refused EARLIER, by the pydantic Field
        # (gt=0) — 422, like every other constrained field on this model.
        # Either status is a refusal; what must never happen is a 201.
        ({"model": "*", "period": "month", "limit_usd": 0}, 422),
        ({"model": "*", "period": "month", "limit_usd": -5}, 422),
        ({"model": "*", "period": "month", "limit_tokens": 0}, 422),
    ):
        body, want = bad
        r = t.post("/admin/profiles", json={"name": "bad", "budgets": [body]}, headers=_h())
        assert r.status_code == want, f"{body} -> {r.status_code} {r.text}"


def test_create_key_from_usd_profile_seeds_cost_budgets(tc) -> None:
    """A key created with a dollar-preset profile gets a cost_budgets row,
    not a budgets row — and a token preset in the same profile still lands
    in budgets."""
    t, fake = tc
    fake.profile_defaults = {
        "budgets": [
            {"model": "*", "period": "month", "limit_usd": 20},
            {"model": "m1", "period": "day", "limit_tokens": 5000},
        ],
        "mcp_grants": [],
    }
    r = t.post(
        "/admin/keys",
        json={"user_id": str(uuid.uuid4()), "name": "k", "scopes": ["proxy"], "profile_id": str(uuid.uuid4())},
        headers=_h(),
    )
    assert r.status_code == 201, r.text
    inserts = [
        (sql, a) for sql, a in fake.executed if sql.startswith(("INSERT INTO budgets", "INSERT INTO cost_budgets"))
    ]
    by_table: dict[str, list] = {}
    for sql, args in inserts:
        table = "cost_budgets" if "cost_budgets" in sql else "budgets"
        by_table.setdefault(table, []).extend(args)
    assert len(by_table.get("cost_budgets", [])) == 1, inserts
    assert len(by_table.get("budgets", [])) == 1, inserts
    usd_row = by_table["cost_budgets"][0]
    assert usd_row[1] == "*" and usd_row[2] == "month" and usd_row[3] == 20.0
    tok_row = by_table["budgets"][0]
    assert tok_row[1] == "m1" and tok_row[3] == 5000


def test_create_key_refuses_invalid_stored_preset_as_400(tc) -> None:
    """A profile row written by an older build (token-only JSON, or a shape
    this build refuses) must be a 400 naming the profile — NOT a 500 raised
    inside the create transaction."""
    t, fake = tc
    fake.profile_defaults = {"budgets": [{"model": "*", "period": "month"}], "mcp_grants": []}
    r = t.post(
        "/admin/keys",
        json={"user_id": str(uuid.uuid4()), "name": "k", "profile_id": str(uuid.uuid4())},
        headers=_h(),
    )
    assert r.status_code == 400, r.text
    assert "invalid budget preset" in r.json()["detail"]
    # nothing was created: the validation runs BEFORE the transaction opens
    assert not [sql for sql, _ in fake.executed if sql.startswith("INSERT INTO api_keys")]


def test_split_preset_budgets_contract() -> None:
    """The shared validator: kinds split by table, and every refusal shape is
    a ValueError (the admin route, the enrollment redeem and the SSO stamping
    all translate it — none may raise a raw DB error at a key-creation site)."""
    import pytest as _pytest

    from pcai_llm.core.budgets import split_preset_budgets

    tok, usd = split_preset_budgets(
        [
            {"model": "*", "period": "month", "limit_tokens": 10},
            {"model": "m", "period": "day", "limit_usd": 2.5},
        ]
    )
    assert tok == [("*", "month", 10)]
    assert usd == [("m", "day", 2.5)]
    # a missing model defaults to the whole-key "*" budget
    tok, _ = split_preset_budgets([{"period": "week", "limit_tokens": 1}])
    assert tok == [("*", "week", 1)]
    for bad in (
        [{"period": "month"}],
        [{"period": "month", "limit_tokens": 1, "limit_usd": 1}],
        [{"period": "month", "limit_tokens": 0}],
        [{"period": "month", "limit_tokens": True}],  # bool is an int subclass
        [{"period": "month", "limit_usd": 0}],
        [{"period": "month", "limit_usd": "5"}],
        # 2026-11-02: "year" (and "quarter") are VALID periods now — the still-
        # invalid shape is "hour", which belongs to the rate limiter, not budgets.
        [{"period": "hour", "limit_usd": 5}],
    ):
        with _pytest.raises(ValueError):
            split_preset_budgets(bad)


def test_console_profile_budget_kind_selector() -> None:
    """The Profiles tab budget editor offers a tokens|USD kind, exactly like
    the per-key budget dialog (b-type): one limit kind per entry, the label
    names the unit, and the limit VALUE switches to a dollar-sized default so
    a leftover token count can never be pasted into a USD field.

    Both render sites (the editor table and the profiles table pill) must
    branch on the row shape instead of printing limit_tokens unconditionally
    — a USD preset has no limit_tokens, so the old expression rendered "0".
    """
    s = open("src/pcai_llm/ui/admin.html", encoding="utf-8").read()
    # kind selector + unit label + the flip handler (mirrors b-type)
    assert 'id="p-b-type"' in s
    assert '<option value="usd">USD</option>' in s
    assert 'id="p-b-limit-label"' in s
    assert "USD limit" in s and "Token limit" in s
    # the entry is built with exactly ONE limit key (XOR contract)
    assert "entry.limit_usd = +$('p-b-limit').value" in s
    assert "entry.limit_tokens = +$('p-b-limit').value" in s
    # both render sites branch on limit_usd first
    assert s.count("b.limit_usd !== undefined") >= 2
    # the dollar starter exists and is a dollar-only preset
    assert "limit_usd: 20 }" in s
    # honest caveat about unpriced models is shown next to the key budget dialog
    assert "only counts models that have a price row" in s


def test_split_preset_budgets_rejects_over_column_maximum() -> None:
    """A limit above its COLUMN bound must be a ValueError here, not a
    NumericValueOutOfRangeError at the INSERT.

    The regression this pins: the splitter validated only positivity while
    budgets.limit_tokens is BIGINT and cost_budgets.limit_usd is
    NUMERIC(12,4). A profile saved with limit_usd = 5e8 passed the save gate
    and then 500d EVERY later create-key / redeem / SSO mint that stamped it.
    """
    import pytest as _pytest

    from pcai_llm.core.budgets import MAX_LIMIT_TOKENS, MAX_LIMIT_USD, split_preset_budgets

    # exactly at the bound is accepted (inclusive)
    tok, usd = split_preset_budgets(
        [
            {"period": "month", "limit_tokens": MAX_LIMIT_TOKENS},
            {"period": "month", "limit_usd": MAX_LIMIT_USD},
        ]
    )
    assert tok == [("*", "month", MAX_LIMIT_TOKENS)]
    assert usd == [("*", "month", MAX_LIMIT_USD)]
    # one over is refused with a message naming the bound
    with _pytest.raises(ValueError, match="column maximum"):
        split_preset_budgets([{"period": "month", "limit_tokens": MAX_LIMIT_TOKENS + 1}])
    with _pytest.raises(ValueError, match="column maximum"):
        split_preset_budgets([{"period": "month", "limit_usd": 1e9}])
    # the values that used to slip through
    for bad in (1e12, 5e8, 99_999_999.99999):
        with _pytest.raises(ValueError, match="column maximum"):
            split_preset_budgets([{"period": "month", "limit_usd": bad}])
    with _pytest.raises(ValueError, match="column maximum"):
        split_preset_budgets([{"period": "month", "limit_tokens": 2**70}])


def test_profile_usd_preset_over_column_max_is_a_422_not_a_500(tc) -> None:
    """The pydantic Field mirrors the column, so an absurd dollar preset is
    refused at SAVE (422) and can never be persisted to 500 later."""
    t, fake = tc
    r = t.post(
        "/admin/profiles",
        json={"name": "too-big", "budgets": [{"model": "*", "period": "month", "limit_usd": 5e8}]},
        headers=_h(),
    )
    assert r.status_code == 422, r.text
    # nothing was persisted
    assert not [a for sql, a in fake.fetchrows if "INSERT INTO profiles" in sql]


def test_sso_default_profile_wiring_validates_presets(tc) -> None:
    """Wiring a profile with an invalid preset as the tenant SSO default is
    refused AT WIRE TIME (400 naming the profile).

    Without this gate the operator leaves the screen satisfied and every
    later self-mint/rotate for that tenant fails — previously as an opaque
    500 raised from inside the mint transaction.
    """
    t, fake = tc
    fake.profile_defaults = {"budgets": [{"model": "*", "period": "month"}], "mcp_grants": []}
    r = t.put(
        f"/admin/tenants/{uuid.uuid4()}/sso-default-profile",
        json={"profile_id": str(uuid.uuid4())},
        headers=_h(),
    )
    assert r.status_code == 400, r.text
    assert "cannot be the SSO default" in r.json()["detail"]
    assert "invalid budget preset" in r.json()["detail"]


def test_sso_default_profile_wiring_accepts_usd_preset(tc) -> None:
    """A valid dollar preset wires cleanly — the gate must not refuse the
    feature it was added to protect."""
    t, fake = tc
    fake.profile_defaults = {"budgets": [{"model": "*", "period": "month", "limit_usd": 20}], "mcp_grants": []}
    r = t.put(
        f"/admin/tenants/{uuid.uuid4()}/sso-default-profile",
        json={"profile_id": str(uuid.uuid4())},
        headers=_h(),
    )
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# Routing wave (2026-10-23): nav tab groups (contract 5a), the model-routing
# panel (5b), the presented-endpoints block (5c) and the index.html
# hidden-models hint (5d). Source-shape pins in this file's seam style — the
# new routes' API behavior is pinned backend-side (tests/test_routing_config.py,
# impl-backend's file). The routes may 404 while this UI lands: the panels are
# written degrade-first, and these pins hold that line.
# ---------------------------------------------------------------------------

NAV_GROUP_TABS = [
    "keys|profiles|enroll",
    # agents sit with the registry tabs: an agent IS a model entry in the data
    # plane (GET /v1/models, pcai.type="agent"), so it belongs next to Models.
    # 2026-10-30: "Advertised" (the registry view split out of Models) and
    # "Metrics" (live endpoint throughput/load) join the inventory group —
    # both are about the objects the Models tab registers.
    "models|advertised|agents|mcp|metrics",
    "usage|traces|audit|guardrails",
    "offpeak|distiller|routing",
]


def test_console_nav_groups_with_green_separators() -> None:
    """5a: the tabs are grouped into exactly FOUR .navgroup divs —
    identity, inventory, observability (Usage folded in with
    traces/audit/guardrails — the Lead's ruling), background GPU-economy
    (off-peak, distiller, routing) — separated by a thin green rule. The
    grouping is COSMETIC ONLY: buttons stay <button data-tab=...> inside
    <nav>, so activateTab(), the nav-click wiring and the boot restore work
    unchanged, and nav keeps flex-wrap so a narrow window wraps whole groups
    WITH their separators intact."""
    import re

    s = open("src/pcai_llm/ui/admin.html", encoding="utf-8").read()
    nav = re.search(r"<nav>(.*?)</nav>", s, re.DOTALL)
    assert nav, "nav block missing"
    groups = [re.findall(r'data-tab="([^"]+)"', c) for c in nav.group(1).split('class="navgroup"')[1:]]
    assert len(groups) == 4, f"expected 4 navgroups, got {groups}"
    assert ["|".join(g) for g in groups] == NAV_GROUP_TABS
    # separator + wrap rules on the documented CSS contract
    assert ".navgroup + .navgroup" in s
    assert "border-left: 1px solid var(--ok)" in s
    assert re.search(r"nav \{[^}]*flex-wrap:\s*wrap", s), "nav must wrap on narrow screens"
    # the activation contract is untouched by the regrouping
    assert "activateTab(b.dataset.tab)" in s
    assert "sessionStore.set('pcai-admin-tab', b.dataset.tab)" in s


def test_console_routing_panel() -> None:
    """5b: the Model routing panel is CONFIG-ONLY (decision-model prompt +
    router model set + GPU-load injection) and DEFENSIVE — the routes are
    built concurrently by impl-backend and may 404 while this UI lands, so
    loadRouting() degrades to an 'unavailable' pill exactly like
    loadDistiller(), never a dead page. The panel must expose the EFFECTIVE
    state (config AND env, distinguishable), the server placeholder legend,
    and an honest footer that the decision model itself is not called yet."""
    import re

    s = open("src/pcai_llm/ui/admin.html", encoding="utf-8").read()
    assert 'data-tab="routing"' in s and 'id="tab-routing"' in s
    assert "if (name === 'routing') loadRouting().catch(" in s  # lazy-load on activation
    # the three contract endpoints
    assert "api('/routing/config')" in s
    assert "api('/routing/config', { method: 'PUT', body })" in s
    assert "api('/routing/prompt-preview'" in s
    fn = re.search(r"async function loadRouting\(\) \{.*?\n\}", s, re.DOTALL)
    assert fn, "loadRouting not extractable"
    body = fn.group(0)
    assert "unavailable" in body, "panel must degrade to the unavailable pill on a failed fetch"
    assert "env-off" in body and "effective_enabled" in body, "pill must distinguish config-paused from env-off"
    # save body: the three card-derived shapes + GPU injection checkbox ride along
    assert ".rt-card-routable:checked" in s
    assert "inject_gpu_load: $('rt-inject').checked" in s
    assert "model_cards: cardForm.model_cards" in s
    assert "model_modalities: cardForm.model_modalities" in s
    # GPU-load availability line mirrors the Off-peak panel's honesty gate
    gpu = re.search(r"function paintRoutingGpu\(gpu\) \{.*?\n\}", s, re.DOTALL)
    assert gpu and "available" in gpu.group(0)
    # the footer states WHEN routing engages (the operator's whole question
    # when they open this panel), and the mode help says what shadow vs active
    # actually DO.
    assert "Routing engages only when a request names" in s
    assert "the model is asked and its pick recorded, the heuristics still serve" in s


def test_console_routing_model_cards() -> None:
    """One card per routable model, and the CARD TEXT is the criteria.

    2026-10-30: the per-model role text moved out of the prompt (which named
    three models) into per-model cards, because the candidate set is
    per-request. The panel is the only place an operator can say what a model
    is for, so the contract it must keep is: render from the available chat
    models, save all three shapes from ONE read of the form, and surface the
    models that have no description (a decision model given bare labels has no
    basis to discriminate -- measured 0/15 commits when the roles were removed
    and not relocated).
    """
    import re

    s = open("src/pcai_llm/ui/admin.html", encoding="utf-8").read()
    # the panel exists, and it is painted from the chat pools
    assert 'id="rt-cards"' in s
    fn = re.search(r"function paintRoutingCards\(models, allowed, modalities, cards, chat\) \{.*?\n\}", s, re.DOTALL)
    assert fn, "paintRoutingCards not extractable"
    body = fn.group(0)
    assert "rt-card-routable" in body and "rt-card-desc" in body and "rt-card-image" in body
    # empty allowed_models means "every chat model", so every card renders ticked
    assert "allowAll" in body
    # descriptions are set through .value, never parsed as markup
    assert "ta.value = " in body
    # one reader for all three shapes
    rd = re.search(r"function readRoutingCards\(models\) \{.*?\n\}", s, re.DOTALL)
    assert rd, "readRoutingCards not extractable"
    rbody = rd.group(0)
    assert "allowed_models" in rbody and "model_modalities" in rbody and "model_cards" in rbody
    # coverage line names the undescribed models
    cov = re.search(r"function paintRoutingCoverage\(\) \{.*?\n\}", s, re.DOTALL)
    assert cov and "described" in cov.group(0)
    # the card placeholder legend is served, not invented in the browser
    assert "criteria_placeholders" in s
    assert "CARD_PLACEHOLDER_HELP" in s
    # live statistics are their OWN panel: a counter read-out must not compete
    # with the config form for the same scroll
    assert s.index("Live statistics") > s.index("Model cards")
    assert s.index("Live statistics") > s.index("Decision prompt")


def test_console_mcp_server_description_tooltip() -> None:
    """The MCP server name carries its description on hover/focus.

    title= was rejected on purpose: it cannot be reached by keyboard and it
    renders outside the page's control, so the cell builds a focusable bubble
    and escapes the description like every other server string.
    """
    import re

    s = open("src/pcai_llm/ui/admin.html", encoding="utf-8").read()
    fn = re.search(r"function mcpNameCell\(sv\) \{.*?\n\}", s, re.DOTALL)
    assert fn, "mcpNameCell not extractable"
    body = fn.group(0)
    # tabindex="0", not merely "tabindex": -1 matches the substring and is NOT
    # keyboard reachable, which is the whole point of not using title=
    assert "tip-bubble" in body and 'tabindex="0"' in body
    assert "esc(desc)" in body, "the description must be escaped like any server string"
    assert "mcpNameCell(sv)" in s, "the MCP table must use the cell"
    assert ".tip > .tip-bubble" in s, "the bubble needs its CSS"
    assert ".tip:focus > .tip-bubble" in s, "keyboard focus must open it"
    assert "max-height" in s and "overflow: auto" in s, "a long description must scroll inside the bubble"
    # fixed, not absolute: the card is a clipping context and would cut the bubble
    assert "position: fixed" in s


# ---------------------------------------------------------------------------
# Agent profiles (2026-10-29): the Agents tab, the profile picker, and the
# presented-models section. Source-shape pins in this file's seam style — the
# API behavior is pinned backend-side (tests/test_agents.py).
# ---------------------------------------------------------------------------


def test_console_agents_tab() -> None:
    """The Agents tab is a full CRUD surface: it must be reachable from the nav,
    lazy-load on activation, and drive the three /admin/agents verbs. The server
    picker is the ONLY place an operator says what an agent may see, so its
    checkboxes are pinned by class (the save reads exactly that class).

    2026-11-04: the picker grew per-server TOOL narrowing — a server row carries
    its include checkbox, an expander that loads the live catalog, and the tool
    checkboxes; the save derives both lists in agSelection()."""
    import re

    s = open("src/pcai_llm/ui/admin.html", encoding="utf-8").read()
    assert 'data-tab="agents"' in s and 'id="tab-agents"' in s
    assert "if (name === 'agents') loadAgents().catch(" in s
    assert "api('/agents')" in s
    assert 'api("/agents", { method: "POST", body: {' in s
    assert 'encodeURIComponent(name), { method: "DELETE" }' in s
    # the picker + save contract
    assert 'id="ag-servers"' in s
    assert 'class="ag-srv-box"' in s and "ag-tools-btn" in s
    assert "function agSelection()" in s and "tools: sel.tools" in s
    assert '"/mcp-servers/" + encodeURIComponent(server) + "/tools"' in s
    # live report 2026-11-04: the expander opened once and never closed, and a
    # deselected server still showed every tool ticked. Both are pinned here:
    # the [hidden] selector is what makes retraction possible at all, and
    # agSyncToolPanel is what keeps the checkboxes honest about inclusion.
    assert ".ag-srvrow .ag-tools[hidden] { display: none; }" in s
    assert "function agSyncToolPanel(" in s and "server not included" in s
    assert 'id="ag-prompt"' in s and 'id="ag-model"' in s
    # the honest legend: an agent HIDES tools, it never grants them
    assert "can only ever HIDE tools, never add one" in s
    # the empty state must not read as "broken" on a fresh install
    fn = re.search(r"function agRenderTable\(\) \{.*?\n\}", s, re.DOTALL)
    assert fn and "no agents yet" in fn.group(0)
    # handlers are DATA-ATTRIBUTE bound, never inline: JSON.stringify emits
    # literal double quotes, which terminate a double-quoted attribute and made
    # every edit/delete button dead (cross-validation 2026-10-29).
    assert 'data-agent-edit="' in s and 'data-agent-del="' in s
    assert "JSON.stringify(a.name)" not in s
    assert "tb.querySelectorAll('[data-agent-edit]')" in s
    # a failed agent-list load must NOT clear a profile's selection
    assert "state.agentsLoaded ? pAgentSelection() : undefined" in s
    # starting from a preset/profile must reset the picker, not carry ticks over
    assert s.count("pRenderAgents([])") >= 1


def test_console_profile_agents_picker() -> None:
    """A profile decides which agents its keys may use, so the editor must carry
    the selection AND always send it: POST /admin/profiles is a full upsert, and
    an omitted `agents` means "leave untouched" — unticking every box would
    otherwise silently keep the old selection."""
    import re

    s = open("src/pcai_llm/ui/admin.html", encoding="utf-8").read()
    assert 'id="p-agents"' in s
    assert 'class="p-agent"' in s
    fn = re.search(r"function pAgentSelection\(\) \{.*?\n\}", s, re.DOTALL)
    assert fn, "pAgentSelection not extractable"
    # sent only when the list actually loaded — an empty picker must not clear
    # a selection this console never saw (cross-validation 2026-10-29)
    assert "agents: state.agentsLoaded ? pAgentSelection() : undefined," in s
    # editProfile must paint the saved selection, not the previous one
    assert "pRenderAgents(p.agents || []);" in s
    # the profiles table reports it per row
    assert "<th>Agents</th>" in s
    # the profiles table's action cell is the SAME blocky .rowacts grid the Keys
    # table uses (operator request 2026-10-29) — and it carries the new
    # apply-grants action
    assert 'class="rowacts"' in s
    assert "propagateGrants(this,'" in s
    assert "propagate-grants" in s
    # .rowacts is a fixed 168px two-column grid with ellipsis, so the labels
    # must stay terse nouns — "apply limits" truncated to "apply lim…"
    # (operator report 2026-10-29). The verb lives in the tooltip.
    assert 'title="APPLY this profile' in s and ">limits</button>" in s
    assert ">grants</button>" in s
    assert ">apply limits</button>" not in s and ">apply grants</button>" not in s
    # off-peak is visible per profile: it is the one profile knob that changes
    # how fast a budget drains, so it must not be a hidden editor field
    assert "<th>Off-peak</th>" in s
    # stamped vs live grants is visible, so an operator knows whether the
    # propagate button is even needed
    assert "stamped" in s and "LIVE grants" in s


def test_console_presented_endpoints_lists_agents() -> None:
    """An agent IS a model entry in the data plane, so the panel that claims to
    show what /v1/models advertises must list them — with the servers, the
    resolved model, and the profiles that offer each one."""
    s = open("src/pcai_llm/ui/admin.html", encoding="utf-8").read()
    assert "const agentRows = (d.agents || []).map(" in s
    assert 'Agents <span class="muted">— tool surfaces selectable as a model</span>' in s
    assert "no agents defined" in s
    for col in ("Servers", "Offered by", "Presented"):
        assert col in s


def test_console_presented_endpoints_block() -> None:
    """5c: the Models tab gains a REFRESHABLE 'Presented endpoints' card
    rendering GET /admin/presented-models — the admin-plane view of what
    /v1/models advertises (aliases, routing pseudo-models, real pools) with
    a muted counts line — loaded on tab activation AND from the refresh
    button, degrading to a muted unavailable line on older backends."""
    s = open("src/pcai_llm/ui/admin.html", encoding="utf-8").read()
    # the heading lost its subtitle in the 2026-10-30 copy pass; the fact it
    # carried (this is what /v1/models advertises) moved to the hint below it
    assert "Presented endpoints</h2>" in s
    assert 'What <span class="mono">/v1/models</span> advertises' in s
    # refresh button wired like the distiller's ↻ (intermediate-var binding)
    assert 'id="pe-reload"' in s and "const peReloadBtn = $('pe-reload')" in s
    assert "peReloadBtn.addEventListener('click', loadPresentedModels)" in s
    assert "api('/presented-models')" in s
    # split out of Models on 2026-10-30: its own tab owns the lazy load
    assert "if (name === 'advertised') loadPresentedModels().catch(" in s
    assert 'id="tab-advertised"' in s
    assert "if (name === 'models') loadPresentedModels" not in s
    # the three row families + the counts line (N presented · M registered)
    for marker in ("Aliases", "Routing pseudo-models", "Registered pools"):
        assert marker in s
    assert " presented · " in s


def test_index_hidden_models_hint() -> None:
    """5d: the chat console's picker silently omits models the key's
    allowlist excludes (the "why is Qwen missing" report). The proxy reports
    the excluded count in the X-PCAI-Models-Denied header; the page reads it
    DEFENSIVELY (absent header = older backend = no hint, never a throw) and
    shows one muted line under the picker, cleared at 0 and on a dead key."""
    import re

    s = open("src/pcai_llm/ui/index.html", encoding="utf-8").read()
    assert "X-PCAI-Models-Denied" in s
    assert 'id="model-deny-hint"' in s
    fn = re.search(r"function paintDeniedHint\(n\) \{.*?\n  \}", s, re.DOTALL)
    assert fn, "paintDeniedHint not extractable"
    body = fn.group(0)
    # the JS string escapes the apostrophe in "key's" (file convention)
    assert "hidden by this key\\'s allowlist" in body
    assert "ask your admin" in body
    assert "el.hidden = false" in body and "el.hidden = true" in body
    # failure-proof parse: absent header -> null -> '0' -> 0, never a throw
    assert "r.headers.get('X-PCAI-Models-Denied') || '0'" in s
    # the dead-key path clears the hint (no stale advice after a 401)
    assert "paintDeniedHint(0);" in s


def test_routing_dropdown_offers_decision_pools_not_just_chat() -> None:
    """REPORTED 2026-10-23: d3-turbo was registered, healthy and correctly
    typed, yet INVISIBLE in the routing dropdown — the one place it is meant to
    be chosen. Cause: loadRouting() built the dropdown from `d.chat_models`,
    which by definition excludes a decision pool, so a correctly-configured
    decision model could never be selected. The GET has always returned
    `decision_models`; the console simply never read it."""
    import re

    s = open("src/pcai_llm/ui/admin.html", encoding="utf-8").read()
    fn = re.search(r"async function loadRouting\(\) \{.*?\n\}", s, re.DOTALL)
    assert fn, "loadRouting not extractable"
    body = fn.group(0)
    assert "d.decision_models" in body, (
        "the dropdown must read the decision-capable set — reading chat_models "
        "hides every decision pool, which is the bug this pins"
    )
    # A configured name that is no longer decision-capable must still render
    # (marked), never be silently dropped from the select.
    assert "not decision-capable" in body


def test_routing_panel_exposes_decision_mode_controls() -> None:
    """The decision model is only USEFUL if the operator can switch it on.
    decision_mode / confidence / timeout were API-only, so the panel could
    configure a model it had no way to activate."""
    s = open("src/pcai_llm/ui/admin.html", encoding="utf-8").read()
    for ctl in ('id="rt-mode"', 'id="rt-minconf"', 'id="rt-timeout"'):
        assert ctl in s, f"{ctl} missing — the decision model cannot be enabled from the console"
    # all three modes must be selectable, with off as the shipped default
    for mode in ('value="off"', 'value="shadow"', 'value="active"'):
        assert mode in s
    # and they must actually be SENT on save, or the control is decorative
    assert "decision_mode: $('rt-mode').value" in s
    assert "decision_min_confidence:" in s and "decision_timeout_ms:" in s


def test_routing_panel_no_longer_claims_the_model_is_unwired() -> None:
    """The panel said the decision model "is not wired" and that the stored
    config had no data-plane reader. That became FALSE when routes_proxy
    started calling decision_settings() — stale copy that tells an operator
    their setting does nothing is worse than no copy at all."""
    s = open("src/pcai_llm/ui/admin.html", encoding="utf-8").read()
    assert "not yet consulted by the data plane" not in s
    assert "does not consult it yet" not in s
    assert "not wired" not in s


def test_live_pools_show_model_type() -> None:
    """The pools table showed model + endpoints only, so the single most
    load-bearing fact about a pool -- what it SERVES -- was invisible. A
    decision model cannot serve chat; an embedding pool 400s a chat
    completion; a chat pool typed `decision` silently vanishes from
    /v1/models and the ladder."""
    s = open("src/pcai_llm/ui/admin.html", encoding="utf-8").read()
    assert "<th>Type</th>" in s
    assert "model_type" in s
    # a mixed pool must be called out rather than silently showing one type
    assert "mixed" in s


def test_nav_is_sticky_below_the_header() -> None:
    """The tab strip scrolled away on a long page, forcing a scroll back to
    the top to change section. It must stick, and it must stick BELOW the
    header (not under it) with a z-index under the error strip."""
    import re

    s = open("src/pcai_llm/ui/admin.html", encoding="utf-8").read()
    m = re.search(r"nav \{[^}]*\}", s)
    assert m, "nav rule not found"
    rule = m.group(0)
    assert "position: sticky" in rule
    assert "top: var(--header-h" in rule, "nav must offset by the header height"
    assert "z-index: 4" in rule, "nav must sit under the header (5) and errstrip (9)"
    assert "--header-h:" in s, "the header height variable must be defined"


def test_key_actions_are_a_compact_grid_not_a_button_pile() -> None:
    """Eight stacked ghost buttons made every row ~150px tall and read as a
    scattered pile. Now a fixed 2-column grid, and `copy prefix` is gone
    (the prefix is already displayed in the Key column) with budgets folded
    into the edit dialog so the grid is a tidy 2x3."""
    s = open("src/pcai_llm/ui/admin.html", encoding="utf-8").read()
    assert ".rowacts" in s and "grid-template-columns: 1fr 1fr" in s
    assert '<div class="rowacts">' in s
    # copy prefix removed entirely (button AND handler)
    assert "copy prefix" not in s
    assert "copyKeyPrefix" not in s
    # budgets no longer a row button; it renders inside the edit dialog
    assert "showBudgets('${k.id}')" not in s
    assert "budgetsMarkup(bud)" in s, "budgets must render inside editKey"
    # and the 2x3 shape: revoke/rotate, edit/MCP, creds/usage
    for label in ("revoke", "rotate", "edit", "MCP", "creds", "usage"):
        assert ">" + label + "</button>" in s, f"{label} button missing from the grid"


def test_keys_page_has_a_search_filter() -> None:
    """Hunting a key meant scanning the whole table. The filter is
    CLIENT-SIDE over the already-loaded list (a server round-trip per
    keystroke would be worse than useless) and matches the handles an
    operator actually has: name, user, prefix, subject."""
    s = open("src/pcai_llm/ui/admin.html", encoding="utf-8").read()
    assert 'id="k-search"' in s
    assert "function filterKeys" in s or "window.filterKeys" in s
    # rows carry a purpose-built haystack, not their whole textContent
    assert "data-search=" in s
    # the filter must survive a reload, or editing a key silently resets it
    assert "filterKeys();" in s


def test_console_endpoint_metrics_panel() -> None:
    """The Metrics tab is the engine's own view: token rates split by phase,
    queue depth, KV-cache occupancy, per POD.

    Three contracts matter and each has bitten before:
      * it polls only while the tab is VISIBLE (an admin console left open all
        day must not hammer Prometheus);
      * it degrades to a line of text, never a dead tab, when the collector
        cannot reach Prometheus;
      * an unattributed pod SURVIVES as an "unmapped" row -- dropping it is how
        a busy pool reads as idle.
    """
    import re

    s = open("src/pcai_llm/ui/admin.html", encoding="utf-8").read()
    assert 'data-tab="metrics"' in s and 'id="tab-metrics"' in s
    assert "api('/endpoint-metrics')" in s
    # lazy-load + poll lifecycle, mirroring the off-peak tab
    assert "if (name === 'metrics') {" in s
    assert "startEndpointMetricsAuto()" in s and "stopEndpointMetricsAuto()" in s
    assert "document.visibilityState === 'hidden'" in s
    assert "clearInterval(emTimer)" in s
    # the columns the question is about
    # "Prompt t/s", not "Prefill t/s": the counter includes prefix-cache hits,
    # so calling it prefill claimed GPU work the engine never did
    for col in ("Prompt t/s", "Decode t/s", "Running", "Queue", "KV%", "Cache hit", "Prefill share"):
        assert col in s, col
    assert "Prefill t/s" not in s
    # unmapped pods are rendered, not dropped
    assert "unmapped" in s
    # idle is a WORD, not a zero that reads as a fault
    assert "· idle" in s
    # a failed fetch degrades the pill instead of throwing
    fn = re.search(r"async function loadEndpointMetrics\(\) \{.*?\n\}", s, re.DOTALL)
    assert fn, "loadEndpointMetrics not extractable"
    body = fn.group(0)
    assert "unavailable" in body and "no endpoints reporting" in body
    # chunked prefill is stated, not papered over with a binary state
    assert "chunked prefill" in s
    # a real-but-small KV reading must not round to "0.0" (it reads as "empty
    # cache" when the engine is holding a little) -- percentages get their own
    # formatter, and the exact value rides in the title
    assert "function emPct(v, digits)" in s
    assert "emPct(r.kv_cache_pct, 2)" in s, "KV% needs two decimals: the pool is huge"
    assert "emPct(r.cache_hit_pct)" in s
    # a value below the printed precision must say so, not print a zero
    assert "return '<' + floor.toFixed(d);" in s
    # the fraction is unreadable without the absolute
    assert "function emKvTitle(r)" in s and "tokens held" in s
    assert "% of the KV cache in use" in s
    # the endpoint cell must show ONE identity, not pod twice
    assert "function emEndpoint(r)" in s and "function emEndpointSub(r)" in s


def test_console_offpeak_per_model_panel_is_its_own_card() -> None:
    """Per-model utilisation left the global evaluator's card (2026-10-30).

    It is a different object (one row per model, its own PromQL, its own
    hysteresis state) and it made the billing card scroll; it now has its own
    heading and its own hint.
    """
    s = open("src/pcai_llm/ui/admin.html", encoding="utf-8").read()
    i = s.index('<section id="tab-offpeak"')
    j = s.index("</section>", i)
    panel = s[i:j]
    assert panel.count('<div class="card">') == 2, "off-peak must be two cards"
    assert "Per-model utilization" in panel
    # the util block is inside the SECOND card, after the global table
    assert panel.index("Per-model utilization") > panel.index('id="op-table"')
    assert 'id="op-models"' in panel


def test_banner_escapes_server_data_by_default() -> None:
    """w6 F1 (REPRODUCED): a key name set by a TEAM LEADER crossed into the
    admin console unescaped — the key-edit save banner interpolated it raw
    into innerHTML, and the fleet admin key sits one sessionStorage read
    away from the resulting stored XSS. banner() must therefore escape by
    DEFAULT; a call site composes markup itself only via the explicit
    html=true opt-in, in which case it esc()s every dynamic fragment.

    Behavioral pin: extract esc() + banner() verbatim from the page and run
    them under node against a fake DOM — a hostile key name renders inert
    on the default path, live markup survives on the opt-in, and no call
    site pre-escapes (a double-escape would render &#39; literally in
    error banners carrying server details with quotes)."""
    import re
    import subprocess
    import tempfile

    s = open("src/pcai_llm/ui/admin.html", encoding="utf-8").read()
    esc_fn = re.search(r"function esc\(s\) \{.*?\n\}", s, re.DOTALL)
    banner_fn = re.search(r"function banner\(.*?\n\}", s, re.DOTALL)
    assert esc_fn and banner_fn, "esc/banner not extractable from admin.html"
    # the contract is in the signature: markup is opt-in, not the default
    assert re.search(r"function banner\(msg, cls = '', timeout, html = false\)", banner_fn.group(0))
    # no call site may pre-escape: under the default-esc contract that is a
    # redundant call that turns a quoted server detail into a literal &#39;
    assert "banner(esc(" not in s, "banner call site pre-escapes (double-escape)"

    harness = (
        "const el = { _h: '', set innerHTML(v) { this._h = v; }, get innerHTML() { return this._h; } };\n"
        "const $ = id => (id === 'banner' ? el : {});\n"
        "const t = (n, cond) => { if (!cond) { console.error('FAIL ' + n); process.exit(1); } };\n"
    )
    calls = (
        # hostile key name (the exact F1 vector) must render INERT on the
        # default path: no live element, and the escape entities are present
        "banner('key updated - limits live immediately (x\"><img src=x onerror=alert(1)>)', '', 0);\n"
        't("no-live-element", !/<img/i.test(el.innerHTML));\n'
        't("escaped", el.innerHTML.includes("&lt;img src=x onerror=alert(1)&gt;"));\n'
        # a server detail with quotes gets ONE escape, never a double
        'el.innerHTML = ""; banner("400: agent \\"a\\" missing", "", 0);\n'
        't("single-escape", el.innerHTML.includes("agent &quot;a&quot; missing") && !el.innerHTML.includes("&amp;"));\n'
        # the opt-in still renders real markup (1784 & friends keep their <b>)
        'el.innerHTML = ""; banner("self-mint <b>allowed</b> for this user", "", 0, true);\n'
        't("opt-in-live", el.innerHTML.includes("<b>allowed</b>"));\n'
        # ...but the opt-in never protects a RAW hostile payload: the opt-in
        # contract is that the caller esc()s its fragments itself
        'el.innerHTML = ""; banner("x" + "&lt;img src=x onerror=alert(1)&gt;", "", 0, true);\n'
        't("opt-in-fragment-inert", !/<img/i.test(el.innerHTML));\n'
    )
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
        f.write(esc_fn.group(0) + "\n" + banner_fn.group(0) + "\n" + harness + calls)
        path = f.name
    r = subprocess.run(["node", path], capture_output=True, text=True)
    assert r.returncode == 0, f"banner default-escape contract broken:\n{r.stdout}\n{r.stderr}"


def test_budget_cell_is_one_worst_state_control_with_popup() -> None:
    """w7 (owner 2026-11-02): the ALLOCATION cell used to stack ONE PILL PER
    BUDGET — a key with 6 per-model budgets made its row ~400px tall. It is
    now ONE compact control (count label, worst-state colour) whose click
    opens EVERY budget in the existing #dlg, one line each. Colour = the
    worst state across the row's budgets using the F9 burn-down thresholds:
    >=95% is the state that REFUSES so it renders red (never hidden behind
    yellow), >=80% warns, all <80% ok, no budgets → neutral dash. Same
    single-line treatment for the enrollments caps cell (viewCaps).

    Behavioral pin: extract the real esc + the budgetPct/budgetState/
    viewBudgets/viewCaps block + the budgetCell builder verbatim from the
    served page and run them under node with fake dlg/fmt helpers — the
    pre-fix page has no viewBudgets/viewCaps and budgetCell is a stacked-
    pills mapper, so extraction fails pre-fix."""
    import re
    import subprocess
    import tempfile

    html = open("src/pcai_llm/ui/admin.html", encoding="utf-8").read()
    esc_fn = re.search(r"function esc\(s\) \{.*?\n\}", html, re.DOTALL)
    assert esc_fn, "esc() not extractable"
    w7 = re.search(r"const budgetPct = b => \{.*?window\.viewCaps = btn => \{.*?\n\};", html, re.DOTALL)
    assert w7, "the single-control budget lifecycle (budgetPct/viewBudgets/viewCaps) not extractable"
    cell_fn = re.search(r"const budgetCell = k => \{.*?\n  \};", html, re.DOTALL)
    assert cell_fn, "budgetCell is not a single-control builder (still the stacked-pills mapper?)"
    assert 'onclick="viewCaps(this)"' in html, "the enrollments caps cell must use the same single control"
    assert '<div class="capline" title="' not in html, "stacked capline divs must be gone from the caps cell"

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
        "const cell6 = budgetCell({ name: 'K1', budget_status: B6 });\n"
        "t('a-one-button', (cell6.match(/<button/g) || []).length === 1);\n"
        "t('a-one-pill', (cell6.match(/class=\"pill/g) || []).length === 1);\n"
        "t('a-no-span-pills', cell6.indexOf('<span class=\"pill') === -1);\n"
        "t('a-count-label', cell6.indexOf('6 budgets') !== -1);\n"
        "t('a-fixed-height', cell6.indexOf('<div') === -1 && cell6.indexOf('<br') === -1);\n"
        # (b) colour = worst state: <80 green, >=80 yellow, >=95 red, none neutral
        "t('b-green', /class=\"pill on\"/.test(budgetCell({ name: 'g', budget_status: [{ model: 'm', period: 'd', spent: 1, limit: 100 }] })));\n"
        "t('b-warn80', /class=\"pill warn\"/.test(budgetCell({ name: 'g', budget_status: [{ model: 'm', period: 'd', spent: 80, limit: 100 }] })));\n"
        "t('b-warn85', /class=\"pill warn\"/.test(budgetCell({ name: 'g', budget_status: [{ model: 'm', period: 'd', spent: 85, limit: 100 }] })));\n"
        "t('b-red96', /class=\"pill off\"/.test(budgetCell({ name: 'g', budget_status: [{ model: 'm', period: 'd', spent: 96, limit: 100 }] })));\n"
        "t('b-worst-red', /class=\"pill off\"/.test(cell6));\n"
        "const none = budgetCell({ name: 'n', budget_status: [] });\n"
        "t('b-neutral', none.indexOf('<button') === -1 && none.indexOf('muted') !== -1);\n"
        # (c) the popup: one line per budget, model + period + spent/limit, key name in the title
        "window.viewBudgets({ dataset: { name: 'K1', b: JSON.stringify(B6) } });\n"
        "const d = dlgCalls[dlgCalls.length - 1];\n"
        "t('c-title', d.indexOf('<h2>Budgets — K1</h2>') !== -1);\n"
        "t('c-rows', (d.match(/<span class=\"pill (on|warn|off)\"/g) || []).length === 6);\n"
        "t('c-model-period', d.indexOf('gpt-5.6 day') !== -1 && d.indexOf('claude-sonnet-4-6 month') !== -1);\n"
        "t('c-spent-limit', d.indexOf('800 / 1000 tok') !== -1 && d.indexOf('$2.0000 / $10.0000') !== -1);\n"
        "t('c-detail', d.indexOf('800 tok spent of 1000 tok allocated') !== -1);\n"
        # (d) hostile model/period stays inert in the cell AND in the popup
        "const Q = String.fromCharCode(34);\n"
        "const hostile = { name: 'H', budget_status: [{ model: Q + '>><img src=x onerror=alert(1)>' + Q, period: Q + '><script>window.pwned=1</script>' + Q, spent: 1, limit: 10 }] };\n"
        "const ch = budgetCell(hostile);\n"
        "t('d-cell-inert', ch.indexOf('<img') === -1 && ch.indexOf('&lt;img') !== -1 && ch.indexOf('<script>') === -1);\n"
        "window.viewBudgets({ dataset: { name: 'H', b: JSON.stringify(hostile.budget_status) } });\n"
        "const dh = dlgCalls[dlgCalls.length - 1];\n"
        "t('d-dlg-inert', dh.indexOf('<img') === -1 && dh.indexOf('&lt;img') !== -1 && dh.indexOf('<script>') === -1 && dh.indexOf('pwned') !== -1);\n"
        # (e) caps cell popup: worst-state over caps + one line per cap
        "const C3 = [\n"
        "  { model: 'gpt-5.6', period: 'month', limit_tokens: 1000, spent_tokens: 50 },\n"
        "  { model: '*', period: 'day', limit_tokens: 100, spent_tokens: 96 },\n"
        "  { model: 'claude-opus-4-7', period: 'month', limit_usd: 20, spent_usd: 3 }\n"
        "];\n"
        "t('caps-worst-red', capsWorst(C3) === 'off');\n"
        "t('caps-worst-warn', capsWorst([{ model: '*', period: 'day', limit_tokens: 100, spent_tokens: 85 }]) === 'warn');\n"
        "t('caps-no-spend-green', capsWorst([{ model: '*', period: 'month', limit_tokens: 100 }]) === 'on');\n"
        "window.viewCaps({ dataset: { name: 'E1', b: JSON.stringify(C3) } });\n"
        "const dc = dlgCalls[dlgCalls.length - 1];\n"
        "t('caps-dlg-rows', (dc.match(/<tr>/g) || []).length === 4);\n"
        "t('caps-dlg-pool', dc.indexOf('team pool = preset × mint allowance') !== -1);\n"
        "t('caps-dlg-limit', dc.indexOf('1000 tok / month') !== -1 && dc.indexOf('$20.0000 / month') !== -1);\n"
    )
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
        f.write(harness + esc_fn.group(0) + "\n" + w7.group(0) + "\n" + cell_fn.group(0) + "\n" + calls)
        path = f.name
    r = subprocess.run(["node", path], capture_output=True, text=True)
    assert r.returncode == 0, f"single-control budget cell contract broken:\n{r.stdout}\n{r.stderr}"


def test_subject_cell_drops_the_default_provenance_chip() -> None:
    """w7 + 2026-11-02 (owner request): the DEFAULT provenance chips are GONE
    from the subject cell, but the provenance DISTINCTION is not.

    Two defaults are silent now: "self-asserted" (typed at redemption, the
    normal case for enrolled keys) and "oauth2-proxy" (the fleet's SSO door —
    every SSO subject already reads sso-<name>, so the chip repeated the same
    fact on every row). The cell states the provenance in the TITLE on every
    row and reserves the visible chip for a NON-default provider or an
    authoritative binding — the case an operator actually needs to spot. This
    pin guards both halves: the noise is gone AND the signal survives
    (removing both would silently make a claimed identity look like a verified
    one).

    Behavioural: subjectCell + esc are extracted verbatim from the page and
    run under node."""
    import re
    import subprocess
    import tempfile

    s = open("src/pcai_llm/ui/admin.html", encoding="utf-8").read()
    esc_fn = re.search(r"function esc\(s\) \{.*?\n\}", s, re.DOTALL)
    cell_fn = re.search(r"const subjectCell = k => \{.*?\n  \};", s, re.DOTALL)
    assert esc_fn and cell_fn, "esc/subjectCell not extractable from src/pcai_llm/ui/admin.html"
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
        # oauth2-proxy is the fleet's DEFAULT SSO door: no chip (the sso-
        # subject already says it), provenance still in the title
        "const v = subjectCell({subject:'sso-bob', subject_idp:'oauth2-proxy', bound_via:'sso'});\n"
        't("no-chip-on-default-sso", !/<span class="pill"/.test(v));\n'
        't("sso-title-states-via", v.includes("bound via oauth2-proxy"));\n'
        # NON-DEFAULT provider: the chip is back, and it names the provider
        "const k = subjectCell({subject:'sso-dana', subject_idp:'keycloak', bound_via:'sso'});\n"
        't("chip-on-other-idp", /<span class="pill"/.test(k));\n'
        't("chip-names-other-idp", k.includes(">keycloak<"));\n'
        't("chip-title-states-other-idp", k.includes("bound via keycloak"));\n'
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

    html = open("src/pcai_llm/ui/admin.html", encoding="utf-8").read()
    # CLASS GUARD: an inline handler may not touch the script-scoped `$`.
    offenders = re.findall(r'on[a-z]+="[^"]*\$\s*\(', html)
    assert not offenders, f"inline handler references the script-scoped $: {offenders}"

    dollar = re.search(r"const \$ = id => document\.getElementById\(id\);", html)
    clear = re.search(r"window\.clearKeySearch = \(\) => \{.*?\n\};", html, re.DOTALL)
    assert dollar and clear, "$/clearKeySearch not extractable from src/pcai_llm/ui/admin.html"
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
    """Operator report: "move the show to the titled header instead of lifting
    the box up. I want a horizontal plane of input in these cases, no bumps up
    and down."

    Pins the three moving parts of the fix, so a later edit cannot silently
    reintroduce a bump:
      1. .row aligns on the TOP of the control slot (align-items:start), so
         anything hanging BELOW a control (a hint, a reveal toggle, a "+ new"
         button) cannot lift that field's input;
      2. each line's heights are MEASURED and published as CSS variables --
         --fhead-h (title line), --hint-h (band under the control),
         --ctl-top (non-field items), --chk-top (checkbox labels) -- by
         layoutFormRowsNow(), and the CSS consumes every one of them;
      3. a password reveal toggle rides the field's TITLE line (.fhead); the
         "toggle after the input" shape survives only as the fallback for bare
         controls such as the header admin key.
    The old opt-in .row-hints hack (absolute hint + a hard-coded 26px) is
    superseded by the measured band and must not come back.
    """
    s = open("src/pcai_llm/ui/admin.html", encoding="utf-8").read()

    # 1. alignment contract
    assert ".row { display: flex; gap: 10px; flex-wrap: wrap; align-items: start; }" in s
    assert "row-hints" not in s, "the per-row opt-in hack is superseded by the measured band"

    # 2. the CSS that consumes the measured variables
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
    assert "position: absolute" in hint_rule, "the hint must leave the flow"
    assert "var(--hint-h, 0px)" in hint_rule, "the hint must be painted in the measured band"

    # 3. structure + measurement
    assert "function structureFormRows(root) {" in s
    assert "function armPasswordToggles(root) {" in s
    assert "function layoutFormRowsNow() {" in s
    assert "function layoutFormRows() {" in s
    assert "head.className = 'fhead';" in s
    # the toggle goes to the title line FIRST; the bare-input shape is the fallback
    assert "if (head) head.appendChild(btn);\n    else input.after(btn);" in s
    # grouping per rendered line, captured before any variable is written
    assert "const lines = new Map();" in s
    assert "el.style.removeProperty('--ctl-top');" in s
    for var in ("--hint-h", "--fhead-h", "--ctl-top", "--chk-top"):
        assert f"el.style.setProperty('{var}'" in s, f"{var} never published"
    # checkbox-bearing labels are row-shaped: they get the centring nudge
    assert "if (el.querySelector('input[type=\"checkbox\"]')) {" in s

    # 4. every render path re-measures (a hidden section measures as 0)
    assert "armPasswordToggles($('dlg-body')); layoutFormRowsNow(); }" in s
    assert "armPasswordToggles(div);\n  layoutFormRowsNow();" in s
    assert "layoutFormRowsNow();\n  return true;" in s  # activateTab
    assert "layoutFormRowsNow();\nwindow.addEventListener('resize', layoutFormRows);" in s
    assert "new MutationObserver(layoutFormRows).observe(document.body, { childList: true, subtree: true });" in s


def test_form_row_labels_carry_a_title_and_a_direct_hint() -> None:
    """The structure/measurement contract is structural, so assert it on the
    markup: (a) every password field inside a row label has TITLE text before
    it (that text is what structureFormRows wraps into .fhead -- a password
    field with no title would fall back to a toggle under the input and bump
    the row again), and (b) every div.hint that lives inside a row hangs off a
    label that is a DIRECT child of that row (the band is measured with
    ':scope > .hint'; a hint nested one level deeper would silently escape the
    plane)."""
    from html.parser import HTMLParser

    class Node:
        def __init__(self, tag, attrs, parent=None):
            self.tag, self.attrs, self.parent = tag, dict(attrs), parent
            self.children = []
            self.text = ""

        def classes(self):
            return self.attrs.get("class", "").split()

    class P(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.void = {"input", "br", "img", "meta", "link", "hr", "source", "col"}
            self.root = Node("root", [])
            self.stack = [self.root]

        def handle_starttag(self, tag, attrs):
            n = Node(tag, attrs, self.stack[-1])
            self.stack[-1].children.append(n)
            if tag not in self.void:
                self.stack.append(n)

        def handle_endtag(self, tag):
            if tag in self.void:
                return
            for i in range(len(self.stack) - 1, 0, -1):
                if self.stack[i].tag == tag:
                    del self.stack[i:]
                    break

        def handle_data(self, data):
            self.stack[-1].text += data
            if data.strip():
                # keep the text as a child node: the title of a field is a text
                # node BEFORE its input, and that is exactly what gets wrapped
                n = Node("#text", [])
                n.text = data
                self.stack[-1].children.append(n)

    def walk(n):
        yield n
        for c in n.children:
            yield from walk(c)

    for path in ("src/pcai_llm/ui/admin.html", "src/pcai_llm/ui/team_leader.html"):
        tree = P()
        tree.feed(open(path, encoding="utf-8").read())
        nodes = list(walk(tree.root))
        rows = [n for n in nodes if n.tag in ("div", "form") and ("row" in n.classes() or "ep-row" in n.classes())]
        pw_labels = 0
        for row in rows:
            for label in [c for c in row.children if c.tag == "label"]:
                if not any(c.tag == "input" and c.attrs.get("type") == "password" for c in label.children):
                    continue
                pw_labels += 1
                first_ctl = next(i for i, c in enumerate(label.children) if c.tag == "input")
                before = "".join(c.text for c in label.children[:first_ctl])
                assert before.strip(), f"{path}: a row password field has no title to wrap"
        if path.endswith("admin.html"):
            # the MCP API key and the model-pool endpoint API key are the two
            # row password fields this contract exists for
            assert pw_labels >= 2, f"{path}: expected the API-key password fields to be row labels"

    # (b) hints inside rows are direct label children
    tree = P()
    tree.feed(open("src/pcai_llm/ui/admin.html", encoding="utf-8").read())
    for n in walk(tree.root):
        if n.tag != "div" or "hint" not in n.classes():
            continue
        a = n.parent
        while a is not None and not (a.tag in ("div", "form") and ("row" in a.classes() or "ep-row" in a.classes())):
            a = a.parent
        if a is None:
            continue
        assert n.parent is not None and n.parent.tag == "label", "a row hint must sit on a label"
        assert n.parent.parent is a, "a row hint must be a DIRECT child of a row label"
