"""Usage rollups: the DEFAULT order and the click-to-sort headers (2026-11-23).

REPORTED: "By model seems to sort by billed cost while by key seems to sort
cost. Default for both should be billed. Additionally, can we allow user to
click these to choose a sort by increasing/decreasing mode?"

Both rollups answer the SAME chargeback question, but they arrived in two
different orders (the server folded both by gross `cost_usd`, and each console
rendered whatever order arrived), so an operator comparing the tables was
comparing two rankings — the "top spender" row could differ between them. Both
now DEFAULT to billed cost, descending (what budgets actually consumed), and
every header sorts on click with a second click flipping the direction.

Pinned here:
  * the shared sort block is BYTE-IDENTICAL in admin.html and
    team_leader.html — two self-contained pages, one place the order may be
    decided, so it must not drift;
  * it BEHAVES (extracted from the real page and run under node): billed
    descending by default, direction flips, text columns read A→Z, NULLs sort
    last in BOTH directions, the cached payload is never mutated, and a saved
    choice is restored;
  * both tables are wired for it (data-sort on every column, aria-sort painted
    from the spec, click + keyboard bound, the CSS arrow that reads aria-sort);
  * the SERVER's order agrees with the console default (the rollup fold and
    both /team_leader/usage ORDER BY clauses lead with billed cost).
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile

import pytest

ADMIN = "src/pcai_llm/ui/admin.html"
TEAM = "src/pcai_llm/ui/team_leader.html"
BLOCK_START = "// ---- usage rollups: default order"
BLOCK_END = "// ---- end usage rollup sorting"

MODEL_COLS = [
    "model",
    "requests",
    "input_tokens",
    "output_tokens",
    "cached_tokens",
    "cache_hit_rate_pct",
    "cost_usd",
    "billable_tokens",
    "billed_cost_usd",
]
KEY_COLS = [
    "key_name",
    "user_name",
    "requests",
    "input_tokens",
    "output_tokens",
    "cached_tokens",
    "cache_hit_rate_pct",
    "cost_usd",
    "billable_tokens",
    "billed_cost_usd",
]


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def _sort_block(path: str) -> str:
    """The shared block: the sort spec, the row comparator, the painter and the
    wiring. Everything that decides the order lives between these markers."""
    s = _read(path)
    assert BLOCK_START in s, f"{path}: the sort block start marker is gone"
    assert BLOCK_END in s, f"{path}: the sort block end marker is gone"
    return s[s.index(BLOCK_START) : s.index(BLOCK_END)]


def _thead(html: str, table_id: str) -> str:
    i = html.index(f'id="{table_id}"')
    return html[i : html.index("</thead>", i)]


def _run_block(block: str, assertions: str, seed: str | None = None) -> None:
    """Run the page's own sort block under node against a real payload shape.

    `seed` is the JSON the session store returns for the saved-sort key — the
    restore path reads it at load, exactly as the browser would."""
    stub = (
        "const sessionStore = { _d: " + json.dumps({} if seed is None else {"pcai-usage-sort": seed}) + ",\n"
        "  get(k) { return Object.prototype.hasOwnProperty.call(this._d, k) ? this._d[k] : null; },\n"
        "  set(k, v) { this._d[k] = v; } };\n"
    )
    payload = (
        "const rows = [\n"
        "  { model: 'a', cost_usd: 10, billed_cost_usd: 1, cache_hit_rate_pct: null, requests: 5 },\n"
        "  { model: 'b', cost_usd: 1, billed_cost_usd: 9, cache_hit_rate_pct: 12.5, requests: 50 },\n"
        "  { model: 'c', cost_usd: 5, billed_cost_usd: 5, cache_hit_rate_pct: 99, requests: 1 },\n"
        "];\n"
        "const order = spec => sortUsageRows(rows, spec).map(r => r.model).join('');\n"
        "const fail = (n, got, want) => { if (got !== want) {\n"
        "  console.error('FAIL ' + n + ': got ' + JSON.stringify(got) + ' want ' + JSON.stringify(want));\n"
        "  process.exit(1); } };\n"
    )
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
        f.write(stub + block + "\n" + payload + assertions)
        path = f.name
    r = subprocess.run(["node", path], capture_output=True, text=True)
    assert r.returncode == 0, f"the sort block misbehaved under node:\n{r.stdout}\n{r.stderr}"


# ---------------------------------------------------------------------------
# the two consoles share ONE sort implementation
# ---------------------------------------------------------------------------


def test_the_sort_block_is_identical_in_both_consoles() -> None:
    """admin.html and team_leader.html are separate self-contained pages (no
    shared asset to import), so the block is duplicated BY DESIGN — and pinned
    byte-for-byte, because a fix in one console that is not mirrored in the
    other is exactly the drift that produced the original complaint."""
    admin, team = _sort_block(ADMIN), _sort_block(TEAM)
    assert admin == team, "the usage-sort block drifted between the two consoles"
    assert "function sortUsageRows(" in admin
    assert "function paintUsageSort(" in admin
    assert "function wireUsageSort(" in admin


# ---------------------------------------------------------------------------
# behavior: default billed-descending, click flips, NULLs last
# ---------------------------------------------------------------------------


def test_usage_sort_defaults_to_billed_cost_descending() -> None:
    block = _sort_block(ADMIN)
    _run_block(
        block,
        "fail('model default col', usageSort.model.col, 'billed_cost_usd');\n"
        "fail('model default dir', usageSort.model.dir, -1);\n"
        "fail('key default col', usageSort.key.col, 'billed_cost_usd');\n"
        "fail('key default dir', usageSort.key.dir, -1);\n"
        "fail('default spec', JSON.stringify(USAGE_SORT_DEFAULT), JSON.stringify({ col: 'billed_cost_usd', dir: -1 }));\n"
        # billed order (b=9, c=5, a=1); the GROSS order would be a(10), c(5), b(1)
        "fail('billed desc', order(USAGE_SORT_DEFAULT), 'bca');\n"
        "fail('gross desc is one click away', order({ col: 'cost_usd', dir: -1 }), 'acb');\n"
        "console.log('OK');\n",
    )


def test_usage_sort_flips_direction_and_reads_text_naturally() -> None:
    block = _sort_block(ADMIN)
    _run_block(
        block,
        "fail('billed asc', order({ col: 'billed_cost_usd', dir: 1 }), 'acb');\n"
        "fail('text asc', order({ col: 'model', dir: 1 }), 'abc');\n"
        "fail('text desc', order({ col: 'model', dir: -1 }), 'cba');\n"
        "fail('numeric column', order({ col: 'requests', dir: -1 }), 'bac');\n"
        # a NULL is "no cached input", not zero: it must sort LAST either way, or
        # a dash floating to the top of an ascending sort reads as the best row
        "fail('nulls last desc', order({ col: 'cache_hit_rate_pct', dir: -1 }), 'cba');\n"
        "fail('nulls last asc', order({ col: 'cache_hit_rate_pct', dir: 1 }), 'bca');\n"
        # the cached payload is the source of truth: sorting copies, never mutates
        "fail('payload untouched', rows.map(r => r.model).join(''), 'abc');\n"
        "console.log('OK');\n",
    )


def test_usage_sort_restores_the_saved_choice() -> None:
    block = _sort_block(ADMIN)
    _run_block(
        block,
        "fail('restored key col', usageSort.key.col, 'cost_usd');\n"
        "fail('restored key dir', usageSort.key.dir, 1);\n"
        "fail('model stays default', usageSort.model.col, 'billed_cost_usd');\n"
        "console.log('OK');\n",
        seed=json.dumps({"key": {"col": "cost_usd", "dir": 1}}),
    )


def test_usage_sort_ignores_junk_in_the_store() -> None:
    """A corrupt/partial saved choice must fall back to the billed-cost default
    rather than leaving a half-applied sort (an unknown column would otherwise
    sort every row as 0 and silently freeze the table's order)."""
    block = _sort_block(ADMIN)
    _run_block(
        block,
        "fail('junk model col', usageSort.model.col, 'billed_cost_usd');\n"
        "fail('junk key col', usageSort.key.col, 'billed_cost_usd');\n"
        "fail('junk key dir', usageSort.key.dir, -1);\n"
        "console.log('OK');\n",
        seed=json.dumps({"model": {"col": "not_a_column", "dir": 7}, "key": "garbage"}),
    )


# ---------------------------------------------------------------------------
# wiring: the headers the operator clicks
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", [ADMIN, TEAM])
def test_usage_rollup_headers_are_sortable(path: str) -> None:
    html = _read(path)
    for table_id, cols in (("usage-by-model", MODEL_COLS), ("usage-by-key", KEY_COLS)):
        thead = _thead(html, table_id)
        got = re.findall(r'data-sort="([^"]+)"', thead)
        assert got == cols, f"{path} {table_id}: sortable columns are {got}, expected {cols}"
        # the caret column (by key) is not a value — it must stay unsortable
        assert 'data-sort="' not in thead.split("<th", 2)[0]
    # both renderers sort from the cached payload and paint/arm the headers
    assert "sortUsageRows((usageSummary && usageSummary.by_model) || [], usageSort.model)" in html
    assert "sortUsageRows((usageSummary && usageSummary.by_key) || [], usageSort.key)" in html
    assert "paintUsageSort('usage-by-model', usageSort.model)" in html
    assert "paintUsageSort('usage-by-key', usageSort.key)" in html
    assert "wireUsageSort('usage-by-model', 'model')" in html
    assert "wireUsageSort('usage-by-key', 'key')" in html
    # the payload is cached for the re-render (a sort click must not refetch)
    assert "let usageSummary = null;" in html
    assert "usageSummary = sum;" in html


@pytest.mark.parametrize("path", [ADMIN, TEAM])
def test_sortable_header_css_draws_the_arrow_from_aria_sort(path: str) -> None:
    """One source of truth for the arrow: aria-sort. A JS-painted glyph would be
    a second copy of the state and could disagree with the rows."""
    html = _read(path)
    assert "th.sortable { cursor: pointer;" in html
    assert 'th.sortable[aria-sort="ascending"]::after { content: "▲"; }' in html
    assert 'th.sortable[aria-sort="descending"]::after { content: "▼"; }' in html
    # the arrow's width is reserved on EVERY sortable header, so the row does
    # not reflow when it moves between columns
    assert 'th.sortable::after { content: "";' in html
    # the painter writes aria-sort; it must never write the glyph itself
    block = _sort_block(path)
    assert "th.setAttribute('aria-sort'," in block
    assert "textContent" not in block.split("function paintUsageSort(")[1].split("function wireUsageSort(")[0]


def test_sortable_header_is_keyboard_reachable() -> None:
    """A click-only control is unreachable by keyboard; the header keeps its
    columnheader role (aria-sort belongs to it) and gains tabindex + Enter/Space."""
    for path in (ADMIN, TEAM):
        block = _sort_block(path)
        assert "th.setAttribute('tabindex', '0')" in block
        assert "ev.key !== 'Enter'" in block
        assert "ev.preventDefault();" in block  # Space must not scroll the page


# ---------------------------------------------------------------------------
# the server's own order must agree with the console default
# ---------------------------------------------------------------------------


def test_rollup_fold_orders_by_billed_cost_then_gross() -> None:
    """GET /admin/usage/summary returns by_model/by_key already ordered; a
    consumer that trusts the wire order must get the billed-cost ranking the
    console shows. cost_usd is the tie-break, so an undiscounted deployment
    (where the columns are equal) keeps its previous order."""
    from pcai_llm.api.routes_admin import _fold_unit_rows

    rows = [
        {"model": "gross-top", "cost_usd": 10.0, "billed_cost_usd": 1.0, "unit_kind": None, "unit_units": 0.0},
        {"model": "billed-top", "cost_usd": 1.0, "billed_cost_usd": 9.0, "unit_kind": None, "unit_units": 0.0},
        {"model": "middle", "cost_usd": 5.0, "billed_cost_usd": 5.0, "unit_kind": None, "unit_units": 0.0},
    ]
    assert [r["model"] for r in _fold_unit_rows(rows, ("model",))] == [
        "billed-top",
        "middle",
        "gross-top",
    ]
    tied = [
        {"model": "cheap", "cost_usd": 2.0, "billed_cost_usd": 1.0, "unit_kind": None, "unit_units": 0.0},
        {"model": "pricey", "cost_usd": 8.0, "billed_cost_usd": 1.0, "unit_kind": None, "unit_units": 0.0},
    ]
    assert [r["model"] for r in _fold_unit_rows(tied, ("model",))] == ["pricey", "cheap"]


def test_every_rollup_query_orders_by_billed_cost() -> None:
    """The leader console renders the same chargeback table, so its SQL must
    lead with the same column — otherwise the two consoles disagree about the
    top spender on any discounted (off-peak) traffic."""
    admin = _read("src/pcai_llm/api/routes_admin.py")
    leader = _read("src/pcai_llm/api/routes_team_leader.py")
    # the fold's own sort (by_model + by_key both ride it)
    assert '(d.get("billed_cost_usd") or 0.0, d.get("cost_usd") or 0.0)' in admin
    # the per-key-per-model drill-down opens under a billed-ordered parent
    assert admin.count("ORDER BY billed_cost_usd DESC, cost_usd DESC") == 1
    assert leader.count("ORDER BY billed_cost_usd DESC, cost_usd DESC") == 2
    assert "ORDER BY cost_usd DESC" not in leader, "a leader rollup still ranks by gross cost"
