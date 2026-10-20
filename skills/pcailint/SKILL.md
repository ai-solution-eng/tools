---
name: pcailint
description: Use when running ruff or mypy on PCAI code, formatting Python in this fleet's repos, or when asked to identify lint/type issues or make code format-clean. Also covers adding lint or typecheck suppressions correctly. Grounded in the shared pcai_utils formatter.sh + ruff.toml + mypy.ini. NOT for shared-file propagation mechanics (fleet-drift) or release verification (release-verify).
---

# pcailint — ruff + mypy, the PCAI fleet way

## THE FACT (read twice)

Every PCAI repo lints through ONE shared script, `formatter.sh`, hardlinked fleet-wide (canonical copy:
`~/Code/HPE/pcai_utils/formatter.sh`). Per target it runs, in order:

1. `ruff format <target>`
2. `ruff check --fix <target>`
3. `mypy --config-file <repo-root>/mypy.ini <target>`

Any failed step fails the whole run; success prints `FORMATTER: all clean`. SKIP lines are skips, NOT
failures ("no ruff.toml found" / "mypy.ini not found next to ruff.toml" / "no Python files").

Config is canonical in `~/Code/HPE/pcai_utils/` (`ruff.toml`, `mypy.ini`) and hardlinked into every repo
root — one inode, every repo. The script locates the repo root by walking up from its own location to the
first directory containing `ruff.toml`.

## Run it

| You want | Do this |
|---|---|
| Full repo pass (default) | `~/Code/HPE/pcai_utils/formatter.sh <repo-root>` — any explicit arg = repo-wide run (all `src/*` packages + `tests/`), regardless of which copy you invoke |
| One package + its tests | run the copy at `<repo>/src/<pkg>/formatter.sh` (no arg) |
| tests/ only | run `<repo>/tests/formatter.sh` (no arg) |
| pcai_utils itself | `cd ~/Code/HPE/pcai_utils && ./formatter.sh` (no `src/` tree → lints the flat root files + `tests/`) |
| Identify issues WITHOUT touching files | `ruff check --no-cache <target>` · `ruff format --check --no-cache <target>` · `mypy --config-file <repo-root>/mypy.ini --cache-dir=<writable-dir> <target>` |

- **`formatter.sh` MUTATES** (format + autofix). "Identify issues" = the read-only row; "make it clean" = the run rows.
- **Sandboxed/agent runs MUST bypass the tool caches** (verified 2026-10-06): under the DSH file sandbox
  `~/Code/...` is read-only, and both tools crash writing their caches there — ruff dies with
  `Failed to create temporary file ... .ruff_cache` (exit 2), mypy 2.3.0 with an INTERNAL ERROR (exit 2).
  Add `--no-cache` to every ruff call and `--cache-dir=/home/andrew/.dsh/<tmp>` to every mypy call. Both
  workarounds verified clean on pcai_utils; a bare invocation that exits 2 is usually this, not a code finding.
- `ruff check --fix` applies only safe autofixes; whatever remains is reported and needs hand-editing. mypy is
  always report-only — it never rewrites code.
- ruff auto-discovers the root `ruff.toml` by walking up from the target. **mypy does not walk up** — it reads
  `mypy.ini` from the CWD only. Outside formatter.sh, always pass `--config-file <repo-root>/mypy.ini`.
- There is no lint pre-commit hook (the hook does secret scanning only) — run formatter.sh yourself before commit.

## The suppression rules (single-homed config)

- ruff: line-length 120, target py312, `output-format = "full"`, `extend-select = ["E402"]`; ignores
  `BLE001 B008 ASYNC230 SIM115 B023 DTZ005 DTZ006 S110 S112 PLW1510` — each is a deliberate code-wide
  pattern with its reason commented in the file. `extend-exclude`: `build`, `**/src/sqlhandler/mcp_fleet_common`.
  Per-file: `src/*/helper_scripts/*` ignores E402 (scripts that set sys.path before project imports).
- mypy: checks under `python_version = 3.14` while ruff formats for py312 — the interpreter typechecks ahead
  of the format floor; do not "fix" the mismatch. `ignore_missing_imports = True`; recursive discovery excludes
  build/.venv/node_modules/mcp_fleet_common trees, but explicitly listed files are still checked.
- Silence ONE spot in code with `# noqa: <CODE>` — the established pattern for intentional late imports is
  `# noqa: E402` (E402 is not on by default; it was added to keep the coverage flake8 provided).
- Silence a pattern code-wide ONLY by editing the canonical config in pcai_utils. Adding per-repo ignores is
  fleet drift by definition.
- **Never edit `ruff.toml`/`mypy.ini` in a consumer repo** — same inode everywhere; the edit lands in every
  repo instantly. Edit in place in pcai_utils (truncate-and-write; a temp-file-and-rename edit breaks every
  hardlink), then run the drift gate: `cd ~/Code/HPE/pcai_utils && ./fleet_drift_check.sh`. A config change
  is a fleet-drift event (see fleet-drift).

## Gotchas

- formatter.sh must live inside the repo it lints (it walks up for `ruff.toml`); a stray copy elsewhere
  SKIPs harmlessly instead of linting with defaults.
- `formatter.sh` missing from `<repo>/src/<pkg>/` or `<repo>/tests/` after git operations = broken hardlinks.
  Repair with `link_utils.sh <repo>` — never git state operations to fix it (guard-uncommitted-work, fleet-drift).
- `.mypy_cache` / `.ruff_cache` at repo roots are normal tool caches; they must never enter a delivery tree.
- Output is interleaved per target (`== <target> ==` sections): a mypy failure after two clean ruff steps
  still fails the run — read every section before declaring victory.
- After fixing reported issues by hand, re-run formatter.sh on the same target: hand edits can introduce
  formatting drift the first pass did not cover.
