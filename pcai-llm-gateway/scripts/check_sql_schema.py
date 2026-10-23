#!/usr/bin/env python3
"""Static SQL/schema agreement check -- the gate that would have caught the
2026-10-14 memory-feed outage BEFORE it shipped.

WHY THIS EXISTS
---------------
v2.5.0 shipped code that SELECTed and wrote
`canonical_distills.distilled_content_hash`, a column no migration ever
created. The due query raised on EVERY sweep, the handler swallowed it at
`log.debug`, and the automatic memory feed stopped cluster-wide for hours.
The unit suite was 1462-green throughout: every distiller test stubs the db
layer and asserts BEHAVIOUR, so no test ever compared the SQL text against
the schema. Nothing in the release path ran the suite anyway.

WHAT IT CHECKS (statement text vs SCHEMA_SQL -- no database required)
  1. alias.column references: every `x.col` whose alias resolves to a known
     table must name a column that SCHEMA_SQL actually creates (inline in the
     CREATE TABLE or via ALTER TABLE ... ADD COLUMN IF NOT EXISTS).
  2. INSERT arity: an INSERT naming N columns must supply N expressions.
  3. Known-invalid Postgres literals used as functions (e.g. `infinity()`).

USAGE
  python3 scripts/check_sql_schema.py [--verbose]
Exit 0 = every statement agrees with the schema. Exit 1 = drift (with the
offending file, line and column named).

Run it in the release gate (automation.sh) and/or a pre-commit hook.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "pcai_llm"
SCHEMA_FILE = SRC / "db" / "schema.py"

# SQL keywords that must never be mistaken for a table alias.
_NON_ALIAS = {
    "on", "where", "and", "or", "set", "values", "select", "from", "join",
    "left", "right", "inner", "outer", "full", "cross", "group", "order",
    "limit", "offset", "returning", "conflict", "do", "update", "as", "using",
    "union", "having", "into", "delete", "insert", "with", "case", "when",
    "then", "else", "end", "not", "null", "is", "in", "exists", "for", "by",
}


def schema_columns() -> dict[str, set[str]]:
    """table -> the set of columns SCHEMA_SQL can create."""
    text = SCHEMA_FILE.read_text(encoding="utf-8")
    tables: dict[str, set[str]] = {}

    for m in re.finditer(r"CREATE TABLE IF NOT EXISTS\s+(\w+)\s*\((.*?)\n\);", text, re.DOTALL):
        name, body = m.group(1), m.group(2)
        cols = set(re.findall(r"^\s{4}([a-z_][a-z0-9_]*)\s+[A-Z]", body, re.MULTILINE))
        tables.setdefault(name, set()).update(cols)

    for m in re.finditer(r"ALTER TABLE\s+(\w+)\s+ADD COLUMN IF NOT EXISTS\s+([a-z_][a-z0-9_]*)", text):
        tables.setdefault(m.group(1), set()).add(m.group(2))

    return tables


def sql_literals(path: Path) -> list[tuple[int, str]]:
    """(line, text) for every SQL-ish string constant, implicitly-concatenated
    literals joined. AST-based, so comments/docstrings never produce noise."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:
        return []

    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            s = node.value
        elif isinstance(node, ast.JoinedStr):  # f-string: keep the literal parts
            s = "".join(
                v.value for v in node.values if isinstance(v, ast.Constant) and isinstance(v.value, str)
            )
        else:
            continue
        if re.search(r"\b(SELECT|INSERT INTO|UPDATE|DELETE FROM)\b", s, re.IGNORECASE):
            out.append((getattr(node, "lineno", 0), s))
    # Docstrings are DOCUMENTATION, not statements: prose like "CONTRACT
    # (unchanged): the ledger is never pruned" contains the word "unchanged"
    # and a colon, which the column-position heuristic happily mistook for a
    # column. Drop every docstring so only real statement text is scanned.
    docstrings = {
        id(d)
        for n in ast.walk(tree)
        if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        for d in [ast.get_docstring(n, clean=False)]
        if d is not None
    }
    if docstrings:
        out = [(ln, s) for ln, s in out if id(s) not in docstrings]
    return out


def aliases_in(sql: str) -> dict[str, str]:
    """alias -> table, from FROM/JOIN clauses."""
    found: dict[str, str] = {}
    for m in re.finditer(r"\b(?:FROM|JOIN)\s+(\w+)(?:\s+(?:AS\s+)?(\w+))?", sql, re.IGNORECASE):
        table, alias = m.group(1), m.group(2)
        if not alias:
            continue
        if alias.lower() in _NON_ALIAS:
            continue
        found[alias] = table
    return found


def _sql_keywords_in(expr: str) -> bool:
    """True when an expression looks like a SQL keyword/function rather than a
    column name (used to avoid false positives on the unaliased-column scan)."""
    first = expr.strip().split("(")[0].strip().split()[0] if expr.strip() else ""
    return first.lower() in _SQL_FUNCS or not re.fullmatch(r"[a-z_][a-z0-9_]*", first or "", re.IGNORECASE)


def check_inserts(sql: str, tables: dict[str, set[str]]) -> list[str]:
    """INSERT columns must EXIST and match the value count.

    Two checks, because both failed in production:
      * arity -- the None branch named 5 columns and supplied 4 expressions;
      * existence -- a renamed/typo'd column list is just as fatal, and the
        first version of this checker missed it entirely (adversarial review
        2026-10-14 proved it by inserting a bogus column and watching the
        checker still exit 0).
    Balanced-paren aware: values contain nested calls like now() and
    make_interval(...).
    """
    problems: list[str] = []
    for m in re.finditer(r"INSERT INTO\s+(\w+)\s*\(", sql, re.IGNORECASE):
        start = m.end() - 1
        depth, i = 0, start
        while i < len(sql):
            if sql[i] == "(":
                depth += 1
            elif sql[i] == ")":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        cols = sql[start + 1 : i]
        rest = sql[i + 1 :]
        vm = re.match(r"\s*VALUES\s*\(", rest)
        if not vm:
            continue  # INSERT ... SELECT: arity is the query's business
        vs = vm.end() - 1
        depth, j = 0, vs
        while j < len(rest):
            if rest[j] == "(":
                depth += 1
            elif rest[j] == ")":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        vals = rest[vs + 1 : j]
        table = m.group(1)
        col_list = [c.strip() for c in cols.split(",") if c.strip()]
        n_cols, n_vals = len(col_list), len([v for v in vals.split(",") if v.strip()])
        if n_cols != n_vals:
            problems.append(
                f"INSERT INTO {table} names {n_cols} column(s) but supplies {n_vals} expression(s)"
            )
        # EXISTENCE: a column the schema never creates is fatal at runtime even
        # when the arity happens to match.
        if table in tables:
            for col in col_list:
                name = col.split(".")[-1].strip().strip('"')
                if not re.fullmatch(r"[a-z_][a-z0-9_]*", name, re.IGNORECASE):
                    continue
                if name not in tables[table]:
                    problems.append(
                        f"INSERT INTO {table} names column '{name}' which SCHEMA_SQL never creates"
                    )
    return problems


def check_unaliased_columns(sql: str, tables: dict[str, set[str]]) -> list[str]:
    """Single-table statements that name a bare column the table lacks.

    Only fires when the statement touches EXACTLY ONE known table (so the
    column's owner is unambiguous) and skips SQL keywords/functions. This is
    the second blind spot adversarial review proved: an unaliased
    `bogus_unaliased_col` sailed through the first version.
    """
    problems: list[str] = []
    mentioned = [t for t in re.findall(r"\b(?:FROM|JOIN|INTO|UPDATE)\s+(\w+)", sql, re.IGNORECASE) if t in tables]
    if len(set(mentioned)) != 1:
        return problems
    table = mentioned[0]
    known = tables[table]
    # Strip SQL comments and quoted literals first: a trailing `-- comment`
    # or a value like 'user' must never be read as a column name (the first
    # cut of this check flagged comment prose -- a validator that cries wolf
    # gets switched off, which is worse than no validator).
    scrubbed = re.sub(r"--[^\n]*", " ", sql)
    scrubbed = re.sub(r"'[^']*'", "''", scrubbed)
    scrubbed = re.sub(r"\$\d+", "?", scrubbed)  # bind placeholders
    scrubbed = re.sub(r"::\s*[a-z_]+", " ", scrubbed, flags=re.IGNORECASE)  # casts
    # Named SQL arguments (`make_interval(secs => $1)`): the name is a
    # parameter, not a column reference.
    scrubbed = re.sub(r"\b[a-z_][a-z0-9_]*\s*=>", " ", scrubbed, flags=re.IGNORECASE)
    for tok in set(re.findall(r"\b([a-z_][a-z0-9_]*)\b", scrubbed, re.IGNORECASE)):
        low = tok.lower()
        if low in known or low in _SQL_KEYWORDS or low in _SQL_FUNCS or low == table.lower():
            continue
        # only flag tokens that sit in a column-ish position (after SELECT/WHERE/
        # AND/OR/comma or before = / < / >), so aliases and CTE names are safe
        if re.search(r"(?:SELECT|WHERE|AND|OR|SET|BY|,|\()\s*" + re.escape(tok) + r"\s*(?:,|=|<|>|\bIS\b|\bAS\b|\)|$)", scrubbed, re.IGNORECASE):
            problems.append(
                f"statement on '{table}' references column '{tok}' which SCHEMA_SQL never creates"
            )
    return problems


# Literals that are NOT functions. Calling them raises at runtime.
_INVALID_FN = {"infinity": "'-infinity'::timestamptz (infinity is a literal, not a function)"}

# SQL keywords that must never be mistaken for a column name.
_SQL_KEYWORDS = {
    "select", "from", "where", "and", "or", "not", "null", "is", "in", "as",
    "join", "left", "right", "inner", "outer", "full", "cross", "on", "using",
    "group", "order", "by", "limit", "offset", "having", "union", "all",
    "insert", "into", "values", "update", "set", "delete", "returning",
    "conflict", "do", "nothing", "case", "when", "then", "else", "end", "asc",
    "desc", "distinct", "exists", "between", "like", "ilike", "interval",
    "cast", "true", "false", "with", "unnest", "array", "any", "some", "each",
    "count", "sum", "max", "min", "avg", "coalesce", "greatest", "least",
}

# Built-in SQL functions/constructs (never columns).
_SQL_FUNCS = {
    "now", "make_interval", "interval", "coalesce", "greatest", "least",
    "count", "sum", "max", "min", "avg", "lower", "upper", "length", "trim",
    "date_trunc", "extract", "to_char", "lpad", "rpad", "concat", "nullif",
    "abs", "round", "floor", "ceil", "jsonb_build_object", "to_jsonb",
    "jsonb_array_elements", "generate_series", "unnest", "array_agg",
}


def main() -> int:
    verbose = "--verbose" in sys.argv
    tables = schema_columns()
    if not tables:
        print("check_sql_schema: could not parse SCHEMA_SQL -- refusing to pass", file=sys.stderr)
        return 1

    problems: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        for line, sql in sql_literals(path):
            rel = path.relative_to(ROOT)

            for alias, table in aliases_in(sql).items():
                if table not in tables:
                    continue
                for col in sorted(set(re.findall(r"\b" + re.escape(alias) + r"\.([a-z_][a-z0-9_]*)", sql))):
                    if col not in tables[table]:
                        problems.append(
                            f"{rel}:{line}: '{alias}.{col}' -- table '{table}' has no column '{col}' "
                            f"in SCHEMA_SQL (the sweep/query will raise at runtime)"
                        )

            for msg in check_inserts(sql, tables):
                problems.append(f"{rel}:{line}: {msg}")

            for msg in check_unaliased_columns(sql, tables):
                problems.append(f"{rel}:{line}: {msg}")

            for fn, why in _INVALID_FN.items():
                if re.search(r"\b" + fn + r"\s*\(", sql):
                    problems.append(f"{rel}:{line}: '{fn}()' called as a function -- use {why}")

    if problems:
        print(f"check_sql_schema: {len(problems)} problem(s) -- SQL and SCHEMA_SQL disagree\n")
        for p in problems:
            print("  " + p)
        print("\nFix the schema or the statement so they agree, then re-run.")
        return 1

    if verbose:
        total_cols = sum(len(c) for c in tables.values())
        print(f"check_sql_schema: OK -- {len(tables)} tables, {total_cols} columns, all statements agree")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
