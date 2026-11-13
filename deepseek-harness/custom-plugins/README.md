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
| [`dsh-mcp-status`](dsh-mcp-status/README.md) | An `MCP` row below the composer: which MCP servers are connected, probe latency, tool counts, and the upstream servers behind a federating one | discovers the `@deepseek-ai/dsh-mcp-client` rows, probes each endpoint with a real MCP handshake, serves `GET /api/mcp-status` | the composer-dock row and its expandable panel |
| [`dsh-asr-endpoint`](dsh-asr-endpoint/README.md) | Voice input that transcribes through **your own HTTP ASR endpoint** instead of the local SenseVoice model — URL, protocol, credentials and request shape all set in `cordis.patch.yml` | registers one remote `ctx.speechToText` provider (`location: 'cloud'`, no `preparation`); posts the recording as OpenAI-compatible multipart, whisper.cpp multipart, base64 JSON or raw WAV | none — the mic comes from the upstream `client-ui-voice-input` row this bundle inserts |

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
    "@local/dsh-mcp-status": "link:/mnt/shared/califra-dsh/tools/deepseek-harness/custom-plugins/dsh-mcp-status"
  },
  "dsh": {
    "profile": {
      "bundles": ["@deepseek-ai/dsh-base", "@deepseek-ai/dsh-web-app", "@local/dsh-mcp-status"]
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
   `dsh-app-shortcuts` and `dsh-mcp-status`.
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
