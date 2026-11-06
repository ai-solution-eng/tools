---
name: values-blueprint
description: Use when creating or editing Helm chart values files, adding/restructuring values keys, writing values.yaml headers or comments, deciding where secrets/keys live (existingSecret vs values), packaging a site values file, or porting the ratified values blueprint to another repo or chart. Encodes the user-readable-and-configurable-first ideology ratified 2026-10 in MultimodalRAG (helm-scale-large/local/values.g2.yaml). NOT for chart template logic (that follows the chart's own conventions) or release verification (release-verify).
---

# values-blueprint — Helm values ideology, the ratified way

## THE PRIME DIRECTIVE (read twice)

**Values files are the product's user interface.** The user reading the file matters
as much as the chart consuming it. Every rule below serves two masters, in order:

1. **User-readable first** — a new operator can scan the file top-to-bottom and know
   what the deployment does and what they must supply, without opening a template.
2. **Configurable second** — every key that exists is one a user might actually set;
   every key that doesn't exist is one they'd have to set everywhere anyway (chart
   default covers it).

## THE BLUEPRINT (ratified 2026-10, canonical example: MultimodalRAG
## `helm-scale-large/local/values.g2.yaml`)

### Layout and spacing

- **Section headers** group the file:

  ```
  # ============================================================================
  # SECTION NAME
  # (a CRITICAL note ONLY if needed — one or two lines, never an essay)
  # ============================================================================

  key:
    ...
  ```

- **Exactly one blank line** between every top-level block and between sibling
  subsections the user added. No runs of 2+ blank lines, no trailing whitespace.
- Top-level keys read like a deployment tour: Image/Scaling → Resources →
  App Parallelism → Secrets → App Defaults → Security → S3 Related → Networking.

### Flattening (trivial dicts and lists)

- Flatten a dict to flow style when it is small and boring — **up to ~120 chars on
  the fixed line**:

  ```yaml
  resources:
    app:
      requests: {cpu: 2, memory: 4Gi}
      limits: {cpu: 4, memory: 8Gi}
  ```

  **CPU first, then memory.** Preserve quoting exactly (`cpu: '4'` stays quoted —
  a quote-losing rewrite changes the rendered value).
- Single-item lists go flow-style: `prefixes: [s3://bucket/prefix/]`.
- Anything with real structure (models.*, oidc.*, sources:) stays block-style.

### Comments

- **One line per knob, or none.** The name + section header usually suffice.
- **Never** date-stamped history essays ("2026-09: …", "fixed 2026-10: …").
  Changelog material belongs in CHANGELOG.md / documentation, not values.
- **Banned marker species** (ratified 2026-10 after the example-file cleanup):
  `# SITE:` fill-in markers (state the fact as a plain one-liner instead),
  decision/audit IDs (`D15`, `D25`, `P0-7` — fleet shorthand meaningless to a
  fresh reader; docs carry that context), and dashed sub-headers
  (`# -- Foo ---` — they fight the banner sections; use a banner or nothing).
- Keep pointers to docs (`— DEPLOYMENT.md §2`), and CRITICAL warnings in the
  section-header block only when misconfiguration is loud or irreversible.
- Do not rewrite the user's own comments — remove TODOs once resolved, don't
  convert them to commentary.
- Explain non-obvious SEMANTICS inline (e.g. "url is read from the Secret first"),
  not mechanics the chart already guarantees.

### REQUIRED header (the secrets-first ideology)

Every chart `values.yaml` and every example opens with this contract:

```
# ### REQUIRED ### — key material lives in Secrets you own, NOT in values
# (build from helm/local/secrets.template.yaml; missing Secret = CreateContainerError;
# every key the Secret carries overrides the values seeds below it).
#   1. modelSecrets.existingSecret — the broad secret: MODEL_*_API_KEY per enabled
#      role, optionally MODEL_*_URL / S3_* / REDIS_PASSWORD (one object, everything)
#   2. models.embedder.url — reachable endpoint (startup ABORTS if unreachable)
#      unless the Secret carries MODEL_EMBEDDER_URL
#   3. security.existingSecret — RAG_API_KEY + MEDIA_TOKEN_SECRET …
#   4. ezua.virtualService.endpoint — RENDER FAILS without it …
```

**Exactly ONE startup section per file** — title (+ sanitized/derivation note for
examples) + REQUIRED + OPTIONAL, closed by one banner. Never stack a second
header block when merging or relocating styles; after any header touch, count:
title ×1, `### REQUIRED` ×1 (overlay fragments exempt), banner pairs balanced.

### Secret ideology (the existingSecret ladder)

- **Key material never transits values.** Values keys for secrets are SEEDS or
  fallbacks only, rendered blank in real site files.
- **Precedence ladder** (per key): user Secret > values seed > sticky stored value.
  Implement via wiring order — user Secret LAST in `envFrom` and LAST in the
  `CONFIG_DIR` chain; never copy Secret material into rendered manifests.
- **Hard-wire** `existingSecret` references (missing Secret = CreateContainerError,
  loud) rather than soft-fallback. A values fallback is not a substitute for a
  missing Secret.
- **The broadest-secret pattern**: one Secret can carry every deployment credential
  — model API keys + endpoint overrides + S3 + Redis (+ optionally the REST key /
  media token / MCP keys). Document the build-from-template + `kubectl apply` +
  blank-the-seeds flow in DEPLOYMENT.md, with the **`kubectl apply` does NOT
  envsubst** caveat (bake real domains into URL literals; keep `${DOMAIN_NAME}`
  forms in values as the documented fallback).
- Chart-managed sticky Secrets (`<deployment.name>-model-keys`) are load-bearing
  plumbing even when fully shadowed — never delete the object; blank shadowed
  values instead.
- Site secret manifests: `helm/local/secrets.template.yaml` (placeholders) →
  `secrets.<site>.yaml` (real material, gitignored, mode 0600, generated
  programmatically with a byte-identical round-trip — NEVER hand-copy long tokens).

### Site-file conventions

- `values.example.yaml` — tracked, sanitized, `<FILL:...>` placeholders, full key
  parity with the chart.
- `values.<site>.yaml` — gitignored (`helm*/local/*`), mode 0600, one per site per
  chart tree (trees differ; never cross-copy).
- User annotations on a site file are theirs: keep them, fix only what's broken
  (invalid kubectl, dead config, drift from the chart).
- **An example documents the knobs; the live site file demonstrates the blueprint
  applied.** Do NOT mirror live values into examples — keep their own tuned
  numbers, filler seeds (`<FILL:>`, `existingSecret: ""`). The only values an
  example inherits from live are STALENESS fixes (a posture live disproved).

## THE EDITING WORKFLOW (always — no exceptions)

1. **Capture the baseline**: `helm template` render per chart + a flattened
   key→value JSON of every values file you will touch.
2. Edit (anchored replacements; assert `count == 1` on every anchor).
3. **Verify zero unintended change**: re-run the key diff — removed/added/changed
   sets must match intent EXACTLY (watch for anchor-merge artifacts like stray
   `""`). Re-render and `diff` against baseline for comment-only changes.
4. Run the chart test suite + `helm lint` for touched charts.
5. Remove the baselines.
6. **Report per-file scope honestly**: which files got the full restructure, which
   a mechanical pass, which were skipped — "applied to all files" without depth is
   an overstatement this blueprint has already punished once.

## PORTING TO ANOTHER REPO

Order: (1) pick the chart's `values.yaml`, apply layout/spacing/flattening and the
REQUIRED header; (2) map its secret keys onto the ladder (add `existingSecret`
knobs to the chart if missing — wire LAST in envFrom/CONFIG_DIR, hard-wired);
(3) create `helm/local/secrets.template.yaml` + update the tracked README section;
(4) port examples + docs; (5) run the same baseline→verify workflow. Do NOT port
site-file material — each site regenerates its own.

## GOTCHAS (each bit a real incident)

- `kubectl apply` does NOT envsubst — `${DOMAIN_NAME}` in a Secret literal deploys
  literally; PCAI's values-editor pipeline is the only envsubst path.
- CronJob bucket names go RAW to boto3 (`upload_file(bucket=…)`): bare name only,
  and the bucket must pre-exist (MinIO does not auto-create). Never point a backup
  bucket at a watched-sources prefix — weekly tarballs re-ingest as documents.
- `ssoGate`-style dead config: a values key indented one level too deep silently
  configures nothing. After restructuring, diff the RENDER, not just the YAML.
- Old `sh.helm.release.v1.*` Secrets embed the user-supplied values OF THAT DEPLOY —
  they keep pre-migration key material alive at rest. Prune history
  (`helm upgrade --history-max 3` or delete old revisions) after secret migrations.
- A shared ConfigMap feeding multiple containers means some values groups cannot be
  split by consumer without template churn — annotate the consumer in a comment
  instead of restructuring values.
- **Stale posture comments**: a comment asserting a deployment posture ("gateway
  applies SSO centrally, oidc off") can be disproved by the live cluster while the
  YAML stays untouched. Before keeping/porting such a comment — especially into an
  example claiming to be "derived from" a site file — check the live objects; if
  stale, replace with the active config + a one-liner, not a shorter essay.
- **Restructuring comments is where verifiers go blind** (2026-10 incident): key-value
  parity and byte-identical renders BOTH stay green while comments are duplicated or
  misfiled. When moving blocks: (a) a block's span must end at the next block's
  ATTACHED-COMMENT start, never the next key line — otherwise attached comment groups
  land in two spans and get duplicated; (b) the first key's comment walk-up will
  swallow the whole file header if they are contiguous — treat everything before the
  first key as the header; (c) after any structural pass, diff the COMMENT structure
  (per-key attached groups vs `git show HEAD:<file>`, duplicate-group scan, banner
  placement) — not just the values.
