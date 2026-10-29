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
    assert "must be one of the profile's models; empty = no downgrade" in s
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
        [{"period": "year", "limit_usd": 5}],
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
    "models|agents|mcp",
    "usage|traces|audit|guardrails",
    "offpeak|distiller|routing",
]


def test_console_nav_groups_with_green_separators() -> None:
    """5a: the twelve tabs are grouped into exactly FOUR .navgroup divs —
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
    # save body: the allowlist array + GPU injection checkbox ride along
    assert ".rt-allowed-model:checked" in s
    assert "inject_gpu_load: $('rt-inject').checked" in s
    # GPU-load availability line mirrors the Off-peak panel's honesty gate
    gpu = re.search(r"function paintRoutingGpu\(gpu\) \{.*?\n\}", s, re.DOTALL)
    assert gpu and "available" in gpu.group(0)
    # muted footer: the decision model IS called on routed requests (it is no
    # longer config-storage-only -- core.decision_routing landed). The copy must
    # say what shadow vs active actually DO, since that is the operator's whole
    # question when they open this panel.
    assert "IS called on routed requests" in s
    assert "records its pick without serving it" in s
    # and the shadow help text must not repeat the old "no latency risk" claim:
    # shadow DOES pay one bounded decision call per routed request.
    assert "pays one extra decision call" in s


# ---------------------------------------------------------------------------
# Agent profiles (2026-10-29): the Agents tab, the profile picker, and the
# presented-models section. Source-shape pins in this file's seam style — the
# API behavior is pinned backend-side (tests/test_agents.py).
# ---------------------------------------------------------------------------


def test_console_agents_tab() -> None:
    """The Agents tab is a full CRUD surface: it must be reachable from the nav,
    lazy-load on activation, and drive the three /admin/agents verbs. The server
    picker is the ONLY place an operator says what an agent may see, so its
    checkboxes are pinned by class (the save reads exactly that class)."""
    import re

    s = open("src/pcai_llm/ui/admin.html", encoding="utf-8").read()
    assert 'data-tab="agents"' in s and 'id="tab-agents"' in s
    assert "if (name === 'agents') loadAgents().catch(" in s
    assert "api('/agents')" in s
    assert 'api("/agents", { method: "POST", body: {' in s
    assert 'encodeURIComponent(name), { method: "DELETE" }' in s
    # the picker + save contract
    assert 'id="ag-servers"' in s
    assert 'class="ag-srv"' in s
    assert 'document.querySelectorAll("#ag-servers input.ag-srv")' in s
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
    assert "Agents <span class=\"muted\">— tool surfaces selectable as a model</span>" in s
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
    assert "Presented endpoints — what /v1/models advertises" in s
    # refresh button wired like the distiller's ↻ (intermediate-var binding)
    assert 'id="pe-reload"' in s and "const peReloadBtn = $('pe-reload')" in s
    assert "peReloadBtn.addEventListener('click', loadPresentedModels)" in s
    assert "api('/presented-models')" in s
    assert "if (name === 'models') loadPresentedModels().catch(" in s
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
