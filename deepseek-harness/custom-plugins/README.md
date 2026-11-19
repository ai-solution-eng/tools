# Custom plugins

Contributed UI plugins for the Harness Web UI, one directory per plugin. Each directory is a
standalone npm package — a Cordis **bundle** that inserts a Host row and, optionally, ships a
browser half that registers into a client slot.

The chart does **not** ship them: `.helmignore` excludes this directory, no `values.yaml` key
references it, and nothing installs a plugin into a user pod automatically. Installation is per
**profile** (`web`) and lands on the user's state PVC, so it survives pod restarts but is not
shared between users.

| Plugin | Adds | Host half | Browser half |
|---|---|---|---|
| [`dsh-app-shortcuts`](dsh-app-shortcuts/README.md) | Terminal / Data Manager / Login buttons at the sidebar foot (`sidebar.footer.action`) | none — `apply()` has no behaviour | three same-origin links, opened in a new tab |

| [`dsh-asr-endpoint`](dsh-asr-endpoint/README.md) | Voice input that transcribes through **your own HTTP ASR endpoint** instead of the local SenseVoice model — URL, protocol, credentials and request shape all set in `cordis.patch.yml` | registers one remote `ctx.speechToText` provider (`location: 'cloud'`, no `preparation`); posts the recording as OpenAI-compatible multipart, whisper.cpp multipart, base64 JSON or raw WAV | none — the mic comes from the upstream `client-ui-voice-input` row this bundle inserts |
| [`dsh-mcp`](dsh-mcp/README.md) | MCP in one bundle: a **Settings → MCP servers** page (list, add, edit, disable, remove, test an endpoint) **and** an `MCP` row below the composer showing what is connected, its tool counts, and the upstream servers behind a federating gateway | mounts one `@deepseek-ai/dsh-mcp-client` child per enabled server, serves `GET /api/mcp-servers` (fiber state, live tool counts), `POST /api/mcp-servers/test` (probe a draft), `POST /api/mcp-servers/save`, and `GET /api/mcp-status` (handshake probe plus upstream discovery) | the page and the composer-dock panel; `client.js` is generated from `src/servers/client.js` + `src/status/client.js` by `scripts/compose-client.mjs` |
| [`dsh-config-document`](dsh-config-document/README.md) | Makes the Settings header button **Open configuration file** work in a headless deployment: the profile's `cordis.patch.yml` opens *inside* the Web UI instead of in an OS editor on the Host (`settings.action`, replaces the shipped cell) | serves the prepared document's path and text on `GET /api/config-document`; read-only, accepts no path parameter | the modal: monospace content, path, line/size hints, Copy, Download, Reload, and an "Open in Host editor" fallback that reports the Host's failure |

## Anatomy of a plugin

```
<plugin>/
  package.json         # name @local/dsh-<name>, private, dsh.bundle.patch + dsh.client
  cordis.patch.yml     # one `insert` row: { id, name, config? }
  index.js             # Host half: apply(ctx) — may be empty
  client.js            # Browser half: slot registration + React components
  locale/{en,zh}.json  # display metadata and UI strings
  icon.svg
  test/*.mjs           # offline checks (no network, no live Harness)
  README.md
```

- `dsh.bundle.patch` names the patch file the profile loader applies. The `id` inside it must be
  unique across the whole profile — a duplicate id fails the boot.
- `dsh.client` declares the browser half (`platform: web`), whether it loads immediately, and the
  client packages it injects. Keep that list minimal: the browser half may require only `react`
  and the `@deepseek-ai/dsh-client-*` packages it declares.
- Style from the `--dsw-alias-*` design tokens, so light/dark and theme overrides follow.
- Keep the halves separate: the Host half runs in the dsh process, the browser half ships to the
  Web client. A Host-side change is only re-imported when the dsh process restarts; the browser
  half hot-reloads.
- Ship an offline test (`node test/host-check.mjs`, `node test/client-check.mjs`) — both plugins
  here do, and neither needs a network or a running Harness.

## Installing a plugin (per profile)

A profile keeps an **absolute** `link:` dependency *and* the package name in
`dsh.profile.bundles`, both in `$DSH_HOME/.dsh/profiles/web/package.json`:

```json
{
  "dependencies": {
    "@local/dsh-mcp": "link:/mnt/shared/califra-dsh/tools/deepseek-harness/custom-plugins/dsh-mcp"
  },
  "dsh": {
    "profile": {
      "bundles": ["@deepseek-ai/dsh-base", "@deepseek-ai/dsh-web-app", "@local/dsh-mcp"]
    }
  }
}
```

Two ways to get there:

1. **Harness UI → Plugins → install bundle**, target = the plugin directory. This is
   `plugin_manager install_bundle`; it maintains both the dependency and the bundle list.
2. **CLI**: `dsh plugin --profile web add <absolute-dir>` runs `pnpm add` in the profile and
   creates the `link:` dependency — but it does **not** touch `dsh.profile.bundles`, so add the
   package name there yourself or the bundle stays inert.

### `ERR_PNPM_ADDING_TO_ROOT` when installing

`dsh plugin …` forwards its arguments **verbatim** to pnpm, and the profile directory is
deliberately a pnpm **workspace root** — dsh writes `profiles/web/pnpm-workspace.yaml`
(`packages: [.]`) when it initializes the profile. So either install path can stop with:

```
ERR_PNPM_ADDING_TO_ROOT  Running this command will add the dependency to the workspace root,
which might not be what you want - if you really meant it, make it explicit by running this
command again with the -w flag (or --workspace-root).
```

Fix it once per profile — pnpm reads `.npmrc` from the project root, and dsh never writes that
file (so nothing will overwrite it):

```sh
echo 'ignore-workspace-root-check=true' >> ~/.dsh/profiles/web/.npmrc
```

That also fixes **Harness UI → Plugins → install bundle**, which goes through the same
plugin-manager path. For a single CLI call the `-w` flag is enough:
`dsh plugin --profile web add <absolute-dir> -w`. **Chart ≥0.4.47 seeds that file on first boot**
(seed-once, next to `cordis.patch.yml`), so a fresh unit needs nothing; units created before that
need the one line above. The file lives on the user's state PVC, so it survives pod restarts and
upgrades.

Because the stored path is absolute, **moving a plugin directory breaks every profile that links
it** — re-link after a move (`dsh plugin --profile web add <new-dir>`). Remove with
`plugin_manager remove_bundle`, or `dsh plugin --profile web remove @local/<name>` plus dropping
the bundle-list entry.

### Paths are part of the contract

The path is stored in the *user's* profile, on their PVC, so a rename/move in this repo is a
breaking change for everyone who installed from the old location. When a move has to happen:

1. Leave a **relative symlink at the old path** pointing at the new directory
   (`ln -s deepseek-harness/custom-plugins/<name> <old-path>`). The shared PVC is mounted at the
   same path in every user pod, so this keeps existing profiles resolving with no action from
   their owners. The 2026-11-12 move into this folder did exactly that for
   `dsh-app-shortcuts` and `dsh-mcp`.
2. Re-link each profile to the new path when convenient, then delete the symlink.

A profile that still points at a removed path only fails **on the next dsh restart** (the running
process keeps the already-imported modules), which is why a move is easy to miss.

## Contributing a new plugin

1. Copy `dsh-app-shortcuts/` — it is the minimal template (one slot, no Host behaviour).
2. Name the package `@local/dsh-<name>` and keep `"private": true`.
3. Give the Cordis row a unique `id`, namespaced after the plugin.
4. Prefer an **additive** slot. `single` slots that already ship UI (e.g. `sidebar.workspaces`,
   `main.conversation`) shadow or fight the shipped UI; `list` slots append and are safe.
5. Document install + verify in the plugin's own README, and add it to the table above.

Anything added here stays out of the packaged chart — `helm package` honours the chart's
`.helmignore`, which excludes `custom-plugins/`.
