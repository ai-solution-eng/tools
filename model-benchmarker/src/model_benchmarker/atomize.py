"""Atomize past benchmark runs into "this model ran this test" records (MB-ATOM).

A sweep's report.md is one file per RUN and its tables are the clear,
human-facing artifact -- this tool deliberately does NOT touch them. It reads
past runs' artifacts and DECOMPOSES each one into atomic test records: one
record per (model, ctx, users, task) measurement, each self-describing enough
to answer "this model ran this test, here is what it scored" without the run
context. The atoms land somewhere else entirely: a JSON Lines file (machine)
plus a sibling flat Markdown table (human), both written atomically.

Inputs (read-only; both shapes, mixed freely):
  * webapp runs: <work_dir>/runs/<run-id>/report.md (+ run_meta.json sibling,
    which supplies run_id/target/status/params provenance)
  * committed results tree: standalone results/**/*.md files (provenance then
    comes from the file's own "- Model: ... name='...'" header bullet)

What one atom carries:
  * identity: kind, model, test_id ("ctx=0|users=32|coding|closed"), label
    (compare.py-compatible, so atoms join the results page's compare view)
  * provenance: run_id, source path, target_url, status, started_utc, arrival
    (open/closed), mode (multiturn/single), load (requests_per_user or
    request_rate/level_duration)
  * metrics: full P50/P95/P99/P100 arrays for TTFT, TTFT-post, tokens/s,
    ITL, TPOT (groups absent from the artifact stay null) + failed count

Semantics kept from compare.py: within one file, a repeated (ctx, users,
task) tuple is a later level of the same test -- the LAST occurrence wins in
the Markdown view; the JSONL keeps every occurrence (row_index orders them)
so nothing is silently dropped.

Usage:
  python -m model_benchmarker.atomize <runs-or-results-dir> [more dirs/files...]
      [--out atoms.jsonl]

Defaults: scans every *.md under the given directories (skipping .partial and
report.html noise), writes <cwd>/atoms.jsonl + <cwd>/atoms.md.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

PCTS = ("p50", "p95", "p99", "p100")

# "- Model: PcaitChat name='deepseek-...'" -- the run header bullet
# write_results_file embeds in every artifact (benchmark_chat.py header_lines).
_MODEL_RE = re.compile(r"- Model: \S+ name='([^']*)'")


def _pcts(values: list[float] | None) -> dict[str, float] | None:
    if not values:
        return None
    return dict(zip(PCTS, (float(v) for v in values)))


def _load_meta(path: Path) -> dict:
    try:
        meta = json.loads(path.read_text(encoding="utf-8"))
        return meta if isinstance(meta, dict) else {}
    except (OSError, ValueError):
        return {}


def _meta_params(meta: dict) -> dict:
    """meta['params'] narrowed to a dict (JSON round-trips can degrade it)."""
    p = meta.get("params")
    return p if isinstance(p, dict) else {}


def _extract_model(md_text: str, meta: dict, source: Path | None = None) -> str | None:
    """Model identity, best-effort, in priority order: run_meta params.model
    (webapp runs), the report's own "- Model: ... name='...'" header bullet
    (CLI --output files), else the results-tree DIRECTORY slug (the committed
    results/<model-slug>/<setup>.md convention, the same identity /api/library
    keys artifacts by). None only when none apply (rendered "unknown" — never
    guessed from a hostname)."""
    model = str(_meta_params(meta).get("model") or "").strip()
    if model:
        return model
    m = _MODEL_RE.search(md_text)
    if m:
        return m.group(1)
    if source is not None:
        # results/<model-slug>/<setup>.md: the slug directory names the model.
        # Guarded THREE ways: the path must have at least a parent-of-parent
        # (a bare name like "x" resolves against the CWD — its "slug" would be
        # the CWD's parent, pure fiction); the parent must not look like a
        # runs dir (run ids are not models); and the source must actually
        # EXIST under that parent (a synthetic/relative string that resolves
        # elsewhere is not a results-tree artifact).
        try:
            resolved = source.resolve()
            # the file must EXIST at this path (a synthetic relative string
            # like "x.md" resolves into the CWD — its "slug" would be the
            # CWD's parent, pure fiction)
            if (
                resolved.is_file()
                and len(resolved.parent.parts) >= 2
                and not _looks_like_runs_dir(resolved.parent.parent)
                and source.parent.parent.is_dir()
            ):
                return resolved.parent.name
        except OSError:
            pass
    return None


def _looks_like_runs_dir(d: Path) -> bool:
    """True when a directory holds per-run subdirectories (run_meta.json
    pattern) rather than model-slug directories — their names are run ids,
    not models."""
    try:
        return any((q / "run_meta.json").is_file() for q in d.iterdir() if q.is_dir())
    except OSError:
        return False


def _chat_load(params: dict) -> dict:
    """The load shape of the run that produced an atom (closed vs open)."""
    load: dict[str, Any] = {}
    if params.get("requests_per_user") is not None:
        load["requests_per_user"] = params.get("requests_per_user")
    if params.get("request_rate") is not None:
        load["request_rate"] = params.get("request_rate")
    if params.get("level_duration") is not None:
        load["level_duration_s"] = params.get("level_duration")
    return load


def atomize_chat_report(md_text: str, source: str, meta: dict) -> list[dict]:
    """One chat report.md -> one atom per parsed table row."""
    from model_benchmarker.results_to_html import parse_chat_table_full

    _mode, rows, arrival = parse_chat_table_full(md_text)
    params = _meta_params(meta)
    model = _extract_model(md_text, meta, Path(source))
    mode = "multiturn" if _mode == "multiturn" else "single"
    atoms: list[dict] = []
    occurrence: dict[tuple, int] = {}
    for idx, r in enumerate(rows):
        ttft, tokens = r.get("ttft"), r.get("tokens")
        if not ttft or not tokens:
            continue  # unparseable row; the JSONL records only real measurements
        key = (r.get("ctx"), r.get("users"), str(r.get("task")))
        occurrence[key] = occurrence.get(key, -1) + 1
        test_id = f"ctx={key[0]}|users={key[1]}|{key[2]}|{arrival}"
        atoms.append(
            {
                "kind": "chat",
                "atom_id": f"{meta.get('run_id') or Path(source).stem}#{idx}",
                "test_id": test_id,
                "label": f"ctx={key[0]} users={key[1]} {key[2]}",
                "model": model,
                "run_id": meta.get("run_id"),
                "source": source,
                "status": meta.get("status"),
                "started_utc": meta.get("started_utc"),
                "target_url": meta.get("target_url"),
                "arrival": arrival,
                "mode": mode,
                "load": _chat_load(params),
                "row_index": idx,
                "occurrence": occurrence[key],
                "test": {"ctx": key[0], "users": key[1], "task": key[2]},
                "metrics": {
                    "failed": _num_or_none(r.get("failed")),
                    "ttft_ms": _pcts(ttft),
                    "ttft_post_ms": _pcts(r.get("ttft_post")),
                    "tokens_per_s": _pcts(tokens),
                    "itl_ms": _pcts(r.get("itl")),
                    "tpot_ms": _pcts(r.get("tpot")),
                },
            }
        )
    return atoms


_NUM_RE = re.compile(r"^-?\d+(?:\.\d+)?$")


def _num_or_none(value: Any) -> float | None:
    """Ints/floats pass through; numeric strings (the tables' \"2\" failed
    cells) coerce; everything else (\"-\", None, prose) is None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and _NUM_RE.match(value.strip()):
        return float(value.strip())
    return None


def atomize_endpoint_run(run_json: dict, source: str, meta: dict) -> list[dict]:
    """One endpoint run.json -> one atom per summary row (label = N=<conc>)."""
    from model_benchmarker.webapp.compare import normalize_endpoint_rows

    params = meta.get("params")
    params = params if isinstance(params, dict) else {}
    mode = str(params.get("mode") or "rest")
    atoms: list[dict] = []
    for idx, row in enumerate(normalize_endpoint_rows(run_json)):
        atoms.append(
            {
                "kind": "endpoint",
                "atom_id": f"{meta.get('run_id') or Path(source).stem}#{idx}",
                "test_id": f"{mode}|{row['label']}",
                "label": row["label"],
                "model": None,
                "run_id": meta.get("run_id"),
                "source": source,
                "status": meta.get("status"),
                "started_utc": meta.get("started_utc"),
                "target_url": meta.get("target_url"),
                "arrival": None,
                "mode": mode,
                "load": {},
                "row_index": idx,
                "occurrence": 0,
                "test": {"label": row["label"]},
                "metrics": row["metrics"],
            }
        )
    return atoms


def latest_occurrences(atoms: list[dict]) -> list[dict]:
    """The table view's rows: for repeated test_ids inside one source the
    LAST occurrence is the current measurement (compare.py semantics — a
    repeated (ctx, users, task) is a later level of the same test); other
    sources keep their own rows. First-appearance order is preserved."""
    latest: dict[tuple, dict] = {}
    order: list[tuple] = []
    for a in atoms:
        key = (a.get("source"), a.get("test_id"))
        if key not in latest:
            order.append(key)
        latest[key] = a
    return [latest[k] for k in order]


def atom_to_compare_metrics(atom: dict) -> dict:
    """Atom metrics -> compare.py's flat row-metric shape (the results
    page's column keys). Missing percentile groups map to None (dash)."""
    m = atom.get("metrics") or {}
    ttft = m.get("ttft_ms") or {}
    tpost = m.get("ttft_post_ms") or {}
    tps = m.get("tokens_per_s") or {}

    def n(d: dict, k: str) -> float | None:
        v = d.get(k)
        return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None

    failed = m.get("failed")
    return {
        "ttft_turn1_p50_ms": n(ttft, "p50"),
        "ttft_turn1_p95_ms": n(ttft, "p95"),
        "ttft_post_p50_ms": n(tpost, "p50"),
        "ttft_post_p95_ms": n(tpost, "p95"),
        "tokens_p50": n(tps, "p50"),
        "tokens_p95": n(tps, "p95"),
        "failed": float(failed) if isinstance(failed, (int, float)) and not isinstance(failed, bool) else None,
    }


_MD_ESC = re.compile(r"\|")


def _md_cell(value: Any) -> str:
    text = "—" if value is None else str(value)
    return _MD_ESC.sub("\\|", text)


def atoms_markdown(atoms: list[dict], title: str) -> str:
    """The human table: one row per atom (latest occurrence per test), the
    columns that answer "this model ran this test" at a glance. Full
    percentile arrays stay in the JSONL; this shows P50/P95 for the two
    headline metrics so the table stays clear and narrow."""
    cols = [
        "model",
        "run",
        "arrival",
        "ctx",
        "users",
        "task",
        "failed",
        "TTFT p50 (ms)",
        "TTFT p95 (ms)",
        "tokens/s p50",
        "tokens/s p95",
        "status",
    ]
    note = (
        "Atomized from past runs — one row per (model, ctx, users, task) measurement; "
        "the last occurrence of a repeated test supersedes earlier ones. "
        "Full P50/P95/P99/P100 arrays per metric: see the sibling .jsonl."
    )
    out = [
        f"# {title}",
        "",
        note,
        "",
        "| " + " | ".join(cols) + " |",
        "|" + "|".join([":---"] * 7 + ["---:"] * 5) + "|",
    ]
    for a in atoms:
        m = a.get("metrics") or {}
        ttft = m.get("ttft_ms") or {}
        tps = m.get("tokens_per_s") or {}
        test = a.get("test") or {}
        run = a.get("run_id") or Path(str(a.get("source") or "")).stem
        out.append(
            "| "
            + " | ".join(
                [
                    _md_cell(a.get("model") or "unknown"),
                    _md_cell(str(run)[:24]),
                    _md_cell(a.get("arrival") or a.get("mode")),
                    _md_cell(test.get("ctx", test.get("label"))),
                    _md_cell(test.get("users", "")),
                    _md_cell(test.get("task", "")),
                    _md_cell(m.get("failed")),
                    _md_cell(ttft.get("p50")),
                    _md_cell(ttft.get("p95")),
                    _md_cell(tps.get("p50")),
                    _md_cell(tps.get("p95")),
                    _md_cell(a.get("status")),
                ]
            )
            + " |"
        )
    out.append("")
    return "\n".join(out)


def _atomic_write(path: Path, text: str) -> None:
    """Repo convention: .partial temp file + os.replace, so a kill mid-write
    can never leave a truncated atom file behind."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".partial")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def collect_sources(paths: list[str]) -> list[Path]:
    """Expand dirs/files into report candidates: every *.md, plus run.json
    siblings (endpoint runs), skipping .partial and non-report markdown."""
    sources: list[Path] = []
    seen: set[Path] = set()
    for raw in paths:
        p = Path(raw).expanduser()
        candidates = sorted(q for q in p.rglob("*.md") if not q.name.endswith(".partial")) if p.is_dir() else [p]
        for c in candidates:
            if c not in seen:
                seen.add(c)
                sources.append(c)
    return sources


def atomize_paths(paths: list[str]) -> tuple[list[dict], list[str]]:
    """Run the decomposition over dirs/files. Returns (atoms, notes) where
    notes are per-source skip reasons (shown on stderr, never fatal)."""
    atoms: list[dict] = []
    notes: list[str] = []
    from model_benchmarker.results_to_html import parse_chat_table_full

    for src in collect_sources(paths):
        try:
            text = src.read_text(encoding="utf-8")
        except OSError as exc:
            notes.append(f"skip {src}: {exc}")
            continue
        meta = _load_meta(src.parent / "run_meta.json")
        run_id = meta.get("run_id") or src.parent.name
        if src.name == "run.json":
            try:
                parsed = json.loads(text)
            except ValueError:
                notes.append(f"skip {src}: invalid JSON")
                continue
            got = atomize_endpoint_run(parsed if isinstance(parsed, dict) else {}, str(src), {**meta, "run_id": run_id})
            atoms.extend(got)
            notes.append(f"{src}: {len(got)} endpoint atoms")
            continue
        _mode, rows, _arrival = parse_chat_table_full(text)
        if not rows:
            notes.append(f"skip {src}: no chat table rows")
            continue
        got = atomize_chat_report(text, str(src), {**meta, "run_id": run_id})
        atoms.extend(got)
        notes.append(f"{src}: {len(got)} chat atoms")
    return atoms, notes


def backfill_db(db_path: str, paths: list[str]) -> tuple[int, int, list[str]]:
    """Atomize dirs/files AND record them into the measurement store.

    Grouped per source so the results-tree filename grammar can stamp the
    serving-setup columns (gpu/engine/mtp/... from <SETUP>.md); run sources
    (with run_meta.json) record without setup columns — a run's setup lives
    in its params, not its filename. Idempotent end to end: re-running the
    same backfill skips every already-recorded row.
    """
    from model_benchmarker import store
    from model_benchmarker.results_to_html import parse_chat_table_full, parse_setup

    inserted = skipped = 0
    notes: list[str] = []
    for src in collect_sources(paths):
        try:
            text = src.read_text(encoding="utf-8")
        except OSError as exc:
            notes.append(f"skip {src}: {exc}")
            continue
        meta = _load_meta(src.parent / "run_meta.json")
        run_id = meta.get("run_id") or src.parent.name
        is_run = (src.parent / "run_meta.json").is_file()
        if src.name == "run.json":
            try:
                parsed = json.loads(text)
            except ValueError:
                notes.append(f"skip {src}: invalid JSON")
                continue
            atoms = atomize_endpoint_run(
                parsed if isinstance(parsed, dict) else {}, str(src), {**meta, "run_id": run_id}
            )
            kind_note = "endpoint"
        else:
            _mode, rows, _arrival = parse_chat_table_full(text)
            if not rows:
                notes.append(f"skip {src}: no chat table rows")
                continue
            atoms = atomize_chat_report(text, str(src), {**meta, "run_id": run_id})
            kind_note = "chat"
        # setup columns: filename grammar for committed-tree artifacts only
        setup = None
        if not is_run and src.parent.parent.is_dir() and not _looks_like_runs_dir(src.parent.parent):
            setup = parse_setup(src.stem)
            # the grammar's arrival is about the FILENAME token; the table's
            # own arrival stamp is authoritative for the rows
            setup.pop("arrival", None)
        i, s = store.record_atoms(db_path, atoms, source=str(src), setup=setup)
        inserted += i
        skipped += s
        notes.append(f"{src}: {len(atoms)} {kind_note} atoms -> {i} recorded, {s} already stored")
    return inserted, skipped, notes


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="atomize-tests",
        description=(
            "Decompose past benchmark runs into atomic 'this model ran this test' records "
            "(read-only on the reports; writes atoms.jsonl + atoms.md, and optionally "
            "records every measurement into the SQLite store via --db)."
        ),
    )
    parser.add_argument("paths", nargs="+", help="run dirs, results dirs, or report .md / run.json files")
    parser.add_argument(
        "--out", default="atoms.jsonl", help="output JSONL path (default ./atoms.jsonl; .md written beside it)"
    )
    parser.add_argument(
        "--db",
        default="",
        help="record every decomposed measurement into this SQLite store "
        "(schema created on first use; idempotent — already-stored rows are skipped)",
    )
    parser.add_argument(
        "--db-only",
        action="store_true",
        help="with --db: skip the JSONL/MD artifacts, the store is the output",
    )
    args = parser.parse_args(argv)

    if args.db:
        inserted, skipped, notes = backfill_db(args.db, args.paths)
        for n in notes:
            print(n, file=sys.stderr)
        summary = store_summary(args.db)
        print(
            f"store {args.db}: {summary['measurements']} measurement(s), "
            f"models: {', '.join(summary['models']) or '(none)'}"
        )
        print(f"this pass: {inserted} recorded, {skipped} already stored (idempotent)")
        if args.db_only:
            return

    atoms, notes = atomize_paths(args.paths)
    for n in notes:
        print(n, file=sys.stderr)

    out_jsonl = Path(args.out).expanduser()
    out_md = out_jsonl.with_suffix(".md")
    payload = "".join(json.dumps(a, ensure_ascii=False) + "\n" for a in atoms)
    _atomic_write(out_jsonl, payload)
    title = f"Atomized benchmark tests ({len(atoms)})"
    _atomic_write(out_md, atoms_markdown(latest_occurrences(atoms), title))

    digest = hashlib.sha256(payload.encode()).hexdigest()[:12]
    models = sorted({str(a.get("model") or "unknown") for a in atoms})
    print(f"{len(atoms)} atom(s) from {len(notes)} source(s) -> {out_jsonl} (+ {out_md})")
    print(f"models: {', '.join(models) if models else '(none)'}  sha256:{digest}")


def store_summary(db_path: str) -> dict:
    """Local import shim: store.stats without importing at module top."""
    from model_benchmarker import store

    return store.stats(db_path)


if __name__ == "__main__":
    main()
