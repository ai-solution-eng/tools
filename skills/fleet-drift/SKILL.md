---
name: fleet-drift
description: Use when changing ANY shared fleet file (a shared auth/identity module, lint config, linker, formatter, or a vendored shared package), when a shared-file change must reach every repo, when the link/sync checkers behave unexpectedly, or when git operations may have broken hardlinks. Covers the two propagation channels and the CI drift gate. NOT for releasing an image (release-verify) or for cluster breakage (cluster-triage).
---

# Fleet-drift — two channels, both must move

## THE FACT (read this twice)

Shared fleet code propagates through **TWO independent channels simultaneously**:

1. **The hardlink mesh** — managed files are TRUE hardlinks (same inode) from each consumer repo to the
   shared source repo. Editing the source is instantly live in every repo. Verified by `check_links.sh`;
   repaired only by `link_utils.sh`.
2. **The vendored copy** — a shared package (reference name: `mcp_fleet_common/`) is **COPIED** (NOT meshed)
   into each adopting app with a `MANIFEST.sha256` inside the copy. Verified by `fleet_common_sync.sh --check`;
   updated by `fleet_common_sync.sh <app-root>`.

**Naming caution — "twin" ≠ "mesh".** A *delivery mirror* ("twin") produced by a hardlinker can be **detached**
from its source (different inodes) while the *mesh* above stays genuinely linked. Do not infer live mesh state
from a mirror, or vice versa: **check the inodes of the files you actually care about** (`stat -c '%i'`) rather
than trusting either label. The mesh's own checker is the authority for channel 1.

**Updating one channel and not the other is silent fleet-wide drift.** A change that touches both — e.g. a
shared auth/attribution edit — is exactly when drift bites. The vendored package is deliberately NOT meshed;
do not "fix" that by hardlinking it.

Resolve the actual source path from the repos themselves: each consumer carries a `hardlink_config.json`
whose `source` key names the shared source repo (its `dest` key names the mirror, when one exists).

## The gate (run before merge / commit)

```bash
cd <shared-source-repo> && ./fleet_drift_check.sh
```

Runs **three** checks across every repo carrying a `hardlink_config.json` (args forward to all checkers):

| Channel | Check | Repair |
|---|---|---|
| 0 — content direction | `fleet_reconcile.py` (dry-run) | `fleet_reconcile.py --run` (PROMOTE) or resolve CONFLICT by hand |
| 1 — hardlink mesh | `check_links.sh` | `link_utils.sh <repo>` |
| 2 — vendored copy | `fleet_common_sync.sh --check` | `fleet_common_sync.sh <app-root>` + re-gen MANIFEST |

Exit **0** = in sync · **1** = drift found · **2** = usage/environment error.
Nothing enforced this before the gate existed — run it, don't assume it.

## Channel 1 — hardlink mesh

- **`check_links.sh`** compares `stat -c %i` (inode) of each managed file against its source.
  `OK` = same inode · `DRIFT` = not hardlinked · `MISS` = module absent (run `link_utils.sh`).
  Enumerate targets by finding `hardlink_config.json` under the fleet root — not by a hardcoded list.
- **`link_utils.sh <repo>`** re-engages links: shared `*.py` utils + `preprocessors/` → `src/<pkg>/utils/`;
  linker/version/prune/lint files → repo root; the formatter → `src/<pkg>/` (+ `tests/`); the version bumper
  → root **only when a chart exists** (chart-less repos get none — the checker SKIPs it there by design,
  not as a false drift).
- **Git operations silently break links** (checkout/merge/rebase/stash rewrite files → new inode). The only
  repair is `link_utils.sh`. **Never** run git state ops to "fix" drift — see `guard-uncommitted-work`.
- **Modes travel with hardlinks.** A file mode set on the source is the SAME inode in every consumer, so a
  restrictive mode (e.g. `711` on a shared script) appears inside a consumer's `src/` tree — which is a
  container-crash shape for a non-root container (see `release-verify`'s mode check). **Fix the mode on the
  source**, not in each consumer.

## Channel 2 — the vendored package

- Source of truth: `<shared-source-repo>/<pkg>/` (reference: `audit.py`, `health.py`, `metrics.py`,
  `namespace_policy.py`, `__init__.py`, `README.md`).
- Consumers vendor a copy at `<app>/<pkg>/` + `MANIFEST.sha256`. The gate discovers them by **scan**, not a
  hardcoded list — but a scan glob can be narrower than reality. **A green gate does not prove every
  vendored copy is in sync**: enumerate the copies yourself (`find <fleet-root> -name MANIFEST.sha256`) and
  compare against what the gate actually reported. If the gate prints a count, sanity-check it.
- **Nested vendoring is a legitimate exception, not drift.** A consumer that vendors the package *nested*
  (e.g. `src/<pkg>/<shared>/`) may carry a deliberate import shim in its `__init__.py`
  (`try: from <shared> import ... except ModuleNotFoundError: from . import ...`). The remaining files stay
  byte-identical to the source, and the shim is re-applied after every sync. **Do not "repair" that
  `__init__.py` to match the source** — you would break nested imports.
- **Copy semantics:** excludes `__pycache__`/`*.pyc`/`MANIFEST.sha256`; **never deletes** a dest file the
  source no longer has (a removed module shows as a manifest mismatch on `--check` — the signal to re-sync,
  deliberately not an auto-delete).

## Editing protocol

1. Edit the file **in the shared source repo** — the mesh carries it to every linked repo. Promotion from a
   consumer back into the source is via **truncate-write onto the source inode** (never temp-rename), so
   every existing link updates atomically and nlink only grows.
2. If the change touches the vendored package, ALSO `fleet_common_sync.sh <app-root>` for **every** consumer
   and re-generate each `MANIFEST.sha256`.
3. Run `./fleet_drift_check.sh`. Non-zero = do not commit.
4. Git ops may re-detach links afterwards → re-run the gate before committing.

### Direction decisions (channel 0)

`fleet_reconcile.py` makes drift direction-decidable against its state file:

| Situation | Action |
|---|---|
| one repo copy advanced | **PROMOTE** into the source inode — fleet-wide instantly |
| source advanced, copies at baseline | **REPAIR** — `ln -f` copies back on |
| both moved to the same content | **NORMALIZE** |
| both moved apart | **CONFLICT** — reported, exit 1, nothing written |

Dry-run by default; `--run` applies; `--repos <r>` scopes; a `--build` mode refreshes generated delivery
mirrors. A watcher can run the same loop on an interval. Bootstrap without a baseline uses **git as
arbiter**: an uncommitted modified copy wins (promote); committed divergence needs a manual merge.

## Hygiene gotchas

- **Audit inode-level BEFORE relinking.** Blind `link_utils.sh` overwrites consumer work with stale source
  content — that asymmetry is exactly what `fleet_reconcile.py` exists to prevent.
- **Syncthing silently re-inodes** mirror files (the tell: `.sync-conflict` litter; the link checker
  excludes `*sync-conflict*` so stale conflict copies never read as a managed MISS).
- **Frozen vintages are deliberate — don't force-link them.** An exceptions file lists non-mesh copies
  (frozen demo vintages, a consumer's vendored fork); reconcile skips them. Removing an entry means the copy
  rejoins the mesh — add entries only with a reason.
- Generated delivery mirrors are produced by a linker (dry-run unless `--run`), honoring layered ignores.
  **A mirror is not a build source** — a mirror's ignore list may exclude the build scripts the image needs.

## Deployment values to resolve locally

One deployment's concrete values — re-derive locally:

- Shared source repo: `<your pcai_utils checkout>/` (checkers: `fleet_drift_check.sh`,
  `check_links.sh`, `link_utils.sh`, `fleet_common_sync.sh`, `fleet_reconcile.py`, `fleet_watch.sh`).
- Vendored package: `mcp_fleet_common/`; gate glob `"$HPE_ROOT"/mcp_servers/*/mcp_fleet_common/MANIFEST.sha256`
  — it sees only `mcp_servers/workbench_mcp` (prints "1 copy"), while the box holds **four** `MANIFEST.sha256`
  files (others under `SQLhandler/src/sqlhandler/` and `pcai-solutions/mcp-servers/`, outside the glob).
- Mesh repos (13, all verified 2026-10-04): `Speech2SpeechOnboardingProject`, `MultimodalRAG`,
  `ModelDownloader`, `ModelBenchmarker`, `SQLhandler`, `PCAI_LLM`, and
  `mcp_servers/{ddgs_lite,searxng_mcp,K8S-MCP,prometheus_mcp,workbench_mcp,applygate_mcp,logsearch_mcp}`.
- The nested-vendoring exception is SQLhandler (`src/sqlhandler/mcp_fleet_common/`).
- Mode precedent: `pcai_utils/fleet_reconcile.py` is `711` and propagated into
  `MultimodalRAG/src/multimodal_rag/utils/`.
- Mirror example: MultimodalRAG's twin excludes `automation.sh`/`bump_version.sh`/`pcai_models.py`, so it can
  never build the image.
