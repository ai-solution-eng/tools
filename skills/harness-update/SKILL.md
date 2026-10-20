---
name: harness-update
description: Use when updating or upgrading the DeepSeek Harness deployment (git pull of the harness repo, rebuild, host restart), when an update failed or left the host unhealthy, or before rolling back a bad harness update. Covers the preflight backup, targeted build, preset-drift check, and the out-of-session restart discipline. NOT for changing harness source code (normal dev flow) or for releasing a deployed service image (that is release-verify).
---

# harness-update (DeepSeek Harness update discipline)

## Why this hurts (the failure model)

The host runs FROM the working repo (`apps/cli/lib/bin.js`), not from an installed artifact — so an
"update" is a live code swap under a running process that owns every session, including the agent
doing the update. The pain is structural:

1. **Restart kills the updater.** The host hosts this session; restarting it mid-turn ends the
   conversation. Every restart step runs from the HOST SHELL, never inside a session.
2. **Presets drift.** Plugin schema changes invalidate preset rows silently at mount — a preset that
   booted yesterday can fail or silently drop tools today. (Reference incident 2026-10-04→09: a
   schema change retired an entire preset; a tool row's `search: true` default started failing every
   call after a provider row was disabled.)
3. **lib/ goes stale against src/.** Profile packages are SYMLINKS into the repo; the host loads
   `lib/index.js` per process. Editing src without rebuilding + restarting changes nothing (or worse,
   mixes old and new).
4. **Partial-update corpses.** Reference day 2026-10-04 left FOUR `$DSH_HOME/.dsh_update_bak_*`
   directories — three failed attempts before one landed. Backup-before-every-step is what makes
   attempt #4 the cheap one.

## The procedure (in order, no steps skipped)

### 0. Preflight (inside a session is fine)

- **Repo state**: `git -C $DSH_REPO status --porcelain`. Dirty files are EITHER the user's
  uncommitted work (read `guard-uncommitted-work` — never stash/discard) OR agent edits awaiting
  commit. A pull with both = conflict pain; surface, don't auto-resolve.
- **Snapshot `$DSH_HOME`**: `cp -a ~/.dsh ~/.dsh_update_bak_$(date +%Y%m%d-%H%M%S)` — EXCLUDE
  nothing; sessions and storages are small relative to the cost of losing them. This is the
  rollback artifact. (The bak dirs are the convention; keep the name pattern.)
- **Record the running host**: PID + start time (`ps -eo pid,lstart,cmd | grep 'bin.js web'`).
- **Note the running sessions** worth preserving: durable teammates survive restarts via
  `session-persistence`; anonymous background children do NOT. Collect or note them as lost.

### 1. Pull

`git -C $DSH_REPO pull --ff-only`. A non-fast-forward means local commits — resolve deliberately
(rebase user commits onto origin), never with merge-blind defaults. If the lockfile changed, run
`pnpm install` in the repo BEFORE building. Check for repo migration notes (`docs/`, upgrade
guides, session-format bumps) — a session-format version change means sessions need the migration
script BEFORE the new host reads them.

### 2. Build (targeted, web host)

The full `pnpm run build:lib:host` = tsc project build + tsdown host pass + desktop bundle. The
desktop leg is NOT needed for a web-host update. Targeted sequence from the repo root:

    node --max-old-space-size=4096 ./node_modules/typescript/bin/tsc -b tsconfig.host.json
    ./node_modules/.bin/tsdown --config-loader native --env.DSH_BUILD_FACE host

Verify freshness per package you care about: compare `src/*.ts` mtime vs `lib/index.js` mtime —
stale lib means the tsdown pass missed the package (or was killed; exit code -2 = sandbox signal,
re-run the tsdown leg alone).

### 3. Update-side-effect checks (before restart, while rollback is still free)

- **Preset drift**: run the deployment's preset drift check (reference:
  `$DSH_HOME/.dsh_update_preset_check.mjs` — diffs user agent-presets against shipped bases and
  validates rows against CURRENT plugin schemas). Fix flagged rows BEFORE restart; a broken row can
  fail the whole preset at mount.
- **Heal probe**: run the deployment's heal/post-update probe (reference:
  `$DSH_HOME/.dsh_update_heal.mjs`).
- **Renamed/moved packages**: preset rows referencing a moved plugin id fail at mount; grep the
  user presets for each id the release notes renamed.

### 4. Restart (HOST SHELL ONLY — never from inside a session)

From a terminal OUTSIDE the harness (this kills every session, including the one you were in):

    ~/.local/bin/dsh_sb web        # bubblewrap-sandboxed launcher (the normal one)
    # or: ~/.dsh/bin/dsh_nos web   # no sandbox, same env

The launchers own: CA bundle env (`NODE_EXTRA_CA_CERTS`), conda env, `DSH_HOME`, and the task-board
stale-lock check. If the start refuses with "task-board lock held by running pid", the old host is
still alive — kill it first rather than forcing with `DSH_NO_TASKBOARD_LOCK_CHECK=1` (two writers on
one ledger = corruption).

### 5. Post-restart verification (new session)

In order, each step a real probe, not an assumption:

1. New host PID and start time (must be post-restart, not the recorded preflight PID).
2. The profile loaded: no preset-mount errors in the host output; the session's tool catalog
   contains the expected tools (probe the ONE tool you most recently changed, e.g.
   `list_agents({})` for team work).
3. Session continuity: prior sessions listed, newest messages readable (proves persistence format
   compatibility).
4. Version/build sanity: the repo HEAD you pulled is what runs (`build marker` / git log match).

### 6. Skills and drift cleanup

- If the update changed tool surfaces, update the affected skills the SAME DAY while the diff is
  fresh (reference: the 2026-10-10 teammate-routing fix required same-session skill edits, and the
  skills deliberately stayed one restart behind the code to avoid documenting unavailable
  capability).
- Delete the update backup dir ONLY after the post-restart verification passes AND a day of real
  use. Until then it is the rollback.

## Rollback

`git -C $DSH_REPO reset --hard <previous-HEAD>` (record the previous HEAD in the preflight step) +
rebuild (step 2) + restore `$DSH_HOME` from the backup dir + restart. If sessions fail to load after
rollback, it is usually a session-format version skew — restore the sessions dir from the SAME
backup as the rest of `$DSH_HOME`, not selectively.

## Reference instantiation

One deployment's values — re-derive locally:

- Repo: `/home/andrew/Projects/deepseek-harness` (origin: github.com/deepseek-ai/deepseek-harness).
  Host runs `apps/cli/lib/bin.js web` from the repo working tree.
- `$DSH_HOME` = `/home/andrew/.dsh`; profiles at `$DSH_HOME/profiles/web` — its
  `node_modules/@deepseek-ai/*` entries are symlinks INTO the repo, so repo rebuilds flow through
  with no install step.
- Launchers: `~/.local/bin/dsh_sb` (bwrap sandbox) and `~/.dsh/bin/dsh_nos` (no sandbox). Both set
  `NODE_EXTRA_CA_CERTS=$DSH_HOME/certs/dsh-trust-bundle.pem` and the conda env (ML14).
- Update probes: `$DSH_HOME/.dsh_update_preset_check.mjs` (preset drift, read-only, exits 0 with
  `preset-drift:` lines), `$DSH_HOME/.dsh_update_heal.mjs` (profile module fallback probe).
- Build marker: `$DSH_HOME/.dsh_update_build_marker` (hash + built-at + HEAD).
- Session-format migration: repo script `pnpm run migrate:sessions-to-v4` (needed only when the
  release bumps the format; check release notes).
- Fossil record motivating the backup rule: `$DSH_HOME/.dsh_update_bak_20261004-*` (4 dirs, 1 day).
