# Tests

Run with the repo's app sources importable (no package install needed):

    PYTHONPATH=src python3 -m pytest tests/ -q

- `test_helm_render.py` — renders the chart with `helm template` and asserts
  the provenance manifest step / revision env appear when `provenance.enabled`
  (default) and disappear entirely when it is false; embedded python compiles.
- `test_provenance_manifest.py` — extracts the provenance python block from a
  live render and executes it against a synthetic HF cache (blobs + symlinked
  snapshots + refs): revision pinning variants, per-file sha256/size, LFS
  cross-check match/mismatch, license resolution, idempotent re-run.
- `test_provenance_wiring.py` — app-side wiring: `_render` placeholder
  substitution (`JOB_ID`, `SUBMITTED_BY`) and `parse_scan_line` handling of
  the scanner's manifest-presence column (4-column new, 3-column old).
- `test_preflight_quota.py` — MD-B: preflight math (fit / refuse naming
  bytes-needed vs bytes-free / warn-unknown), quota decisions (boundary,
  per-namespace override, warn-vs-refuse), per-namespace usage attribution
  from scanner rows + job annotations/manifests (custom roots included),
  PreflightService TTL caching + usage deltas between scans, and the
  queue.submit gate ordering (refusal before any Job creation; S3 bypass).
- `test_gc_plan.py` — MD-C: the THREE-KEY AND deletion matrix (each key
  missing blocks deletion; each key alone insufficient), AIOLI cross-check
  matching (cache-dir name / repo id in uri — never exact path equality),
  live-job protection semantics, TTL/LRU line, protectedModels globs, and
  the dry-run report shape.
- `test_scanner_columns.py` — the chart ≥ 1.7 scanner contract: six columns
  (adds du-bytes + ModelScan verdict), backward-compatible 3/4-column rows,
  malformed-manifest tolerance of the rendered verdict reader, merge behavior
  for the new fields.
- `test_wave4_render.py` — chart renders for the wave-4 features: gate step
  present/absent with `gate.enabled`, GC CronJob only with `gc.enabled=true`
  (scanner admission pattern verbatim, dryRun default true, python blocks
  compile), the read-only PVC RBAC rule tied to `preflight.enabled`, quota
  env rendering, and the byte-identical all-off render.
- `test_wave4_wiring.py` — app-side wiring: `_parse_quota_map` env format,
  the GC dry-run report shape + AIOLI-unavailable fail-safe, and defaults
  matching values.yaml (needs `psycopg2` — skipped when absent).

Requires `helm` on PATH and pyyaml/pytest (`pip install pytest pyyaml`).
`test_wave4_wiring.py` additionally imports the app (needs `psycopg2-binary`;
it skips itself when that is not installed).
