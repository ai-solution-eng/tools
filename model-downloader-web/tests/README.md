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
- `test_manual_delete_plan.py` — the admin deletion verdict
  (`gc.evaluate_manual_targets`): the TTL line is NOT applied, a missing
  manifest warns rather than blocks, an AIOLI row is a SOFT block, and a live
  job / protected glob / unavailable cross-check are HARD blocks. Also that
  delete Jobs are dropped from the live-job list (a legacy annotated one
  otherwise blocks the whole page).
- `test_manual_delete_wiring.py` — the real FastAPI app: the auth gate (SSO
  admin by username or group, shared key, and the verified-OIDC-token path
  including forged-header refusal), the isolation rule (a target must come from
  the current scan), list vs pin preview modes, single-use `verdict_id`,
  override rules, the form's SSO-derived default namespace, and that the Job is
  created in the namespace that OWNS the PVC.
- `test_delete_targets.py` — the in-pod module: payload decoding, every
  structural refusal (relative, symlink, non-directory, bad basename, outside
  root, traversal), rename-before-delete, vanished-target accounting, tombstone
  contents, and exit codes. Uses the real filesystem, not mocks.
- `test_job_templates.py` — regression guards for two shipped bugs: every
  optional template key must be mapped in `K8sClient.start()` (a missing one
  made every commit die with "template is missing"), and the delete Job must
  carry no `cache-root` annotation (it pinned the whole PVC, blocking its own
  targets and every later preview). Also that a client with no template
  ConfigMap can still start — the in-pod delete Job does exactly that.
- `test_oidc_vendored.py` — the vendored `pcai_utils` OIDC verifier is
  unmodified (SHA-256 guard: edit it upstream and re-copy, never patch here),
  and the admin gate still calls the shared verifier rather than trusting a
  header.

Requires `helm` on PATH and pyyaml/pytest (`pip install pytest pyyaml`).
`test_wave4_wiring.py` additionally imports the app (needs `psycopg2-binary`;
it skips itself when that is not installed).
