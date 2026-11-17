# @local/dsh-mcp-servers

A **Settings → MCP servers** page for the DeepSeek Harness Web UI: the same idea
as Settings → Models, but for Model Context Protocol servers. It lists the MCP
servers configured for the profile and lets you add one (Streamable HTTP or
stdio), edit it, disable it, or remove it — and an added server's tools become
callable by the agent as `mcp__<serverName>__<tool>` without editing YAML. (A
deployment whose supervisor watches the profile patch restarts once on save —
see [Known limits](#known-limits); the configuration, not a restart, is what
keeps the server.)

```
Settings
├── General
├── Models
├── MCP servers      ← this bundle (order 12, between Models and Plugins)
└── Plugins
```

## What it adds

| Surface | Kind | Where |
|---|---|---|
| `settings.section` | page | Settings → MCP servers (`id: mcp-servers`, `order: 12`) |
| `settings.section.icon` | nav glyph | the same page's sidebar row |
| `GET /api/mcp-servers` | Host route | live state the page polls while open |

The page shows, per server: name, transport, endpoint (or command), a state dot
with `Ready` / `Connecting` / `Waiting` / `Failed` / `Disabled`, the number of
`mcp__<name>__*` tools currently registered, and the reason when the server
cannot start. A read-only **Configured elsewhere** section lists MCP rows that
other layers of the profile contribute (bundle patch, profile patch, agent
preset) so the page never pretends to be the whole picture.

## How it works

Three mechanisms, all of them existing Harness contracts:

1. **The list lives in this plugin's own configuration.** The row
   `mcp-servers` in the profile patch holds `config.servers`, declared as a
   *volatile* field. The Settings service therefore exposes it in the shared
   settings mirror, and the page writes it through `ctx.configForms` →
   `settings.mutate` → `ctx.configEditor.edit`, which rewrites only that value
   and keeps every comment and `!!js` expression in the user's
   `cordis.patch.yml` intact. The plugin never reads or rewrites that file
   itself.

2. **Each enabled server is a mounted `@deepseek-ai/dsh-mcp-client` child.**
   `index.js` calls `ctx.plugin(bridge, config)` from its own context, so the
   child fiber is owned by this row and is unloaded with it. The mounted set is
   diffed against the configured set on every change, so an edit to one server
   does not bounce the others, and a removed or disabled server's tools
   disappear immediately.

3. **Live state is reported, not inferred.** The route reports each child's
   fiber state and its registered tool count, which is the only honest signal
   that an added server is actually usable.

`Config` is a hand-written StandardSchemaV1 rather than a schemastery schema:
a `link:`-ed profile plugin resolves only its own `node_modules`, which has no
DSH tree, so it cannot import `@deepseek-ai/schemastery` at module scope. The
schema still emits schemastery's `{ uid, refs }` JSON and still returns
volatile references built on cosmokit's registered
`Symbol.for('cosmokit.volatile.write')`, so the Settings form, the
comment-preserving write, and the in-place `loader/volatile-update` commit all
work exactly as they do for the Models page. `test/schema-check.mjs` proves
this against the real schemastery and cosmokit from the installation.

The bridge module itself is resolved lazily, in order: a plain `import()`, the
Loader's own resolution (`ctx.loader.import`), then adoption of an
already-loaded bridge runtime from any existing MCP row. If all three fail the
page says so and still lets you edit the list.

## Install

The bundle is installed per profile; nothing is global.

```js
// plugin_manager, from the Harness session that owns the profile
{ action: 'install_bundle', target: '/mnt/shared/califra-dsh/tools/deepseek-harness/custom-plugins/dsh-mcp-servers' }
```

That links the package into the profile and adds `@local/dsh-mcp-servers` to
`dsh.profile.bundles`. Then reload the page and open **Settings → MCP servers**.
To remove it again, `{ action: 'remove_bundle', target: '@local/dsh-mcp-servers' }`.

The row it inserts is:

```yaml
- insert:
    - id: mcp-servers
      name: '@local/dsh-mcp-servers'
      config:
        servers: []
```

The first write from the page adds a matching `config` to the profile patch;
later writes update that row in place. **Do not rename the row id**: the page
addresses the settings namespace `mcp-servers`.

## Usage

- **Add** — *Add server*, pick a transport:
  - *Streamable HTTP*: `URL`, optional request `Headers` (name/value rows).
  - *stdio*: `Command`, `Arguments` (one per line, no shell interpolation),
    optional `Working directory` and `Environment`.
  - `Server name` must match `[A-Za-z0-9_-]{1,32}` and be unique; it becomes
    the `mcp__<name>__` tool prefix.
  - `Tool call timeout` defaults to 60000 ms. *Fail loudly when the server
    cannot start* maps to the bridge's `failOnStartupError`.
  - `Instruction cap` bounds the server's instructions block that the bridge
    injects into the system prompt, in bytes. It defaults to 262144, so a
    server with a large instruction payload (the HPE networking MCP sends
    ~121 KB) connects out of the box — the bridge's own default of 32768
    fails such a server with `server instructions exceed
    maxInstructionBytes`. Lower it to keep a verbose server out of the prompt.
- **Edit** — reopens the same form. Changing the endpoint, transport, name or
  any connection field remounts that one server; other servers keep running.
- **Disable / Enable** — keeps the configuration, drops or restores the
  connection and its tools.
- **Remove** — asks for confirmation, then deletes the row and unregisters its
  tools.
- **Retry** — appears when the bridge could not be resolved; asks the Host to
  remount failed servers.

Validation runs twice: in the page before it writes (name charset and
uniqueness, absolute http(s) URL, non-empty command, header/env key shape,
positive timeout and instruction cap) and again in the Host before it mounts
(the same rules, reported per row under *Cannot start*). A row that fails Host validation is
kept in configuration and reported — it never breaks the page or the boot.

## Verify

Offline checks (no network, no live Harness, no browser):

```bash
cd deepseek-harness/custom-plugins/dsh-mcp-servers
node test/schema-check.mjs   # config schema + the framework's volatile contract
node test/host-check.mjs     # mounting, reconciliation, failure isolation, route
node test/client-check.mjs   # registration, dictionaries, rendered states, live store
```

`test/schema-check.mjs` loads the real `schemastery` and `cosmokit` out of the
DSH installation (default `/opt/dsh/npm/lib/node_modules/@deepseek-ai/dsh`,
override with `DSH_INSTALL`) and replays the algorithms
`@deepseek-ai/dsh-settings` and the Loader run over this Config:
`volatileForm`, `plainSchema`, `plainConfig`, `isVolatilePath`,
`applyPathOp`, `equalExceptVolatile`, and the volatile commit. Without that
installation the interop half is skipped and the local invariants still run.

In a running profile:

1. Open **Settings → MCP servers**; the row must appear between Models and
   Plugins with the indigo rack icon.
2. Add a server. Within a few seconds its row shows `Ready` and a tool count,
   and the agent can call `mcp__<name>__<tool>` (ask it to list its tools).
3. Reload the page: the server is still listed, still `Ready`, and re-adding
   the same name is rejected as a duplicate.
4. Disable it: the tools disappear. Remove it: the row and tools are gone.
5. Point a server at an unreachable URL: the row shows `Failed` with the
   bridge's error, the rest of the page keeps working, and the Harness still
   boots.

The route can be inspected directly:

```bash
curl -s --cookie '<your session cookie>' http://127.0.0.1:3080/api/mcp-servers | jq
```

## Known limits

- **A deployment that watches the profile patch restarts on save.** Saving the
  list writes it through the config editor into the profile's
  `cordis.patch.yml`, which is exactly what Settings → Models does. The PCAI
  unit runs `dsh web` under `/opt/preview/dsh_supervisor.mjs`, which polls that
  file every 3 s and restarts `dsh web` ~750 ms after any change, so the page
  reconnects once after a save. Sessions are durable and survive the restart;
  nothing is lost. On a deployment without that watcher the volatile update
  mounts the server live, with no restart at all. Either way the configuration
  is what makes the server persist, so do not be surprised by the reconnect.
- **Only its own row is editable.** MCP servers configured in the profile patch
  or by another bundle are listed read-only under *Configured elsewhere*; edit
  those in `cordis.patch.yml`. Their request headers never leave the Host.
- **No credential storage.** Header and environment values are plain
  configuration, like the bridge's own. For a secret, keep using
  `!!js process.env.X` in the profile patch — the page will not overwrite an
  expression it does not own, but it also cannot create one.
- **`serverName` collisions with rows configured elsewhere** are reported by
  the mounted child (the bridge reserves the namespace), not pre-empted by the
  page.
- **The page needs its own row active.** If `mcp-servers` is disabled or fails
  to load, the page still renders and explains that configuration is
  unavailable instead of showing an empty list.
- **Two copies of the copy deck.** The browser bundle cannot import JSON, so
  the UI strings in `client.js` are an inline copy of `locale/*.json`. Edit
  both; `locale/*.json` is what Plugin Manager and Settings read for
  `meta.title` / `meta.description`, and the test asserts the two languages
  carry identical keys.
- **Browser rendering is unverified here.** The offline checks cover the
  registration, the dictionaries, the rendered element tree and the live store;
  appearance in light/dark and the real `settings.section` host chrome must be
  confirmed in the connected page.

## Files

| File | Role |
|---|---|
| `index.js` | Host half: mount/reconcile `@deepseek-ai/dsh-mcp-client` children, `GET /api/mcp-servers` |
| `schema.js` | `Config` (StandardSchemaV1 + volatile field), row normalization, mountable view |
| `client.js` | Browser half: `settings.section` page, nav icon, dictionaries, live store |
| `cordis.patch.yml` | The bundle's row (`id: mcp-servers`) |
| `locale/en.json`, `locale/zh.json` | Display metadata (`meta.title`/`meta.description`) and the UI copy |
| `icon.svg` | Original artwork: a server rack linked to a live node |
| `test/*.mjs` | Offline checks listed above |

## License

Same terms as the surrounding `custom-plugins` directory.
