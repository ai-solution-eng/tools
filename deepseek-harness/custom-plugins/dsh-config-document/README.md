# Configuration file viewer (`@local/dsh-config-document`)

Makes the Settings header button **“Open configuration file”** actually open the
profile configuration file — *inside* the Harness Web UI, in the browser you are
already using.

![icon](icon.svg)

- **Before:** the button called the Host's native file opener. On a laptop the Host
  *is* the desktop, so a text editor window appeared. On the PCAI deployment the Host
  is a headless container, so the open failed (or opened nothing anyone can see) and
  the button appeared to do nothing at all.
- **After:** the same button (same label, same place) opens a modal in the app showing
  the file's absolute path, its line and size hints, and its exact text in a monospace
  scroll area, with **Copy**, **Download**, **Reload**, and a secondary
  **Open in Host editor** button that still asks the Host to open its own desktop
  editor and *reports* the Host's refusal instead of swallowing it.

## Root cause

`@deepseek-ai/dsh-client-ui-settings-general` registers the `settings.action` cell
`open-document`. Its component calls
`remote.settings.openSettingsDocument()`, which on the Host
(`@deepseek-ai/dsh-api-settings-controller`) does:

```text
settings.prepareDocument()  ->  ctx.configEditor.documentPath
openNativeTextFile(path)    ->  xdg-open / open -t / Explorer
```

That is an **OS-level open on the Host machine**. `describe()` in the same controller
hardcodes `hasDocument: true`, so the action renders on every deployment — including
ones where there is no desktop to open anything on. Nothing in the chain can tell the
browser that the open failed, and the shipped component only shows a small inline
error when the Host *rejects* the call; when the native opener "succeeds" by doing
nothing visible, the user sees silence.

## What this bundle does

| Half | File | Responsibility |
| --- | --- | --- |
| Host | `index.js` | Registers one authenticated, read-only Fetch route (`GET /api/config-document` by default) that answers `{ ok, path, name, content, bytes, lines, mtime }` for `ctx.settings.prepareDocument()`, falling back to `ctx.configEditor.documentPath`. |
| Browser | `client.js` | Registers the `settings.action` cell with the shipped id `open-document` at a **lower priority**, which shadows the shipped cell, and renders the modal. |

Two details make it read as the same button rather than a second one:

1. **The id.** A `settings.action` list cell resolves to the first live entry with that
   id in priority order (`entriesOfSlot` in the slot core). Registering the shipped id
   `open-document` at priority `-1` shadows the shipped entry at priority `0`, so
   exactly one button remains.
2. **The label.** `action.open` is “Open configuration file” in English and
   “打开配置文件” in Chinese — the shipped strings.

The browser half also degrades instead of crashing:

- Host route missing (Host half not loaded yet, or a restart is still pending) →
  the modal explains the HTTP status and that a `dsh` restart is required.
- `settings` service missing → the Host route answers
  `{ ok: false, code: 'unavailable', message: … }` naming what it looked for.
- Document missing/unreadable → the modal shows the Host's message and the path.
- No `t` seat, no `remote` service, no clipboard API → every path still renders and
  reports, nothing throws out of `apply()`.

### Save is deliberately **not** implemented

The task allowed a save path only if it were provably safe. It is not, so this bundle
is read-only and says so in the UI (“Read-only view. Download the file to edit it, then
restore it on the Host.”):

- The profile patch is written in the **Loader's YAML dialect** (`!!js` expressions,
  custom tags). No YAML parser is reachable from a plain-JS browser/Host bundle that
  understands that dialect — a plain `yaml.load` would reject valid documents, so a
  “validate then write” gate would either be a lie or refuse the user's real file.
- The profile patch is live configuration for a running Host. A raw rewrite bypasses
  `ctx.configEditor.edit()`, which is the sanctioned persistence path: it validates the
  candidate, preserves comments and `!!js` expressions outside the replaced values, and
  serializes with other profile writes. The plugin-development reference is explicit
  that plugin code must not read-filter-rewrite the active profile patch.
- **Download** gives the same practical result with no risk: edit locally, then restore
  the file through whatever channel the operator already uses for the profile.

If a future Host exposes a document-level validate-and-write API, a save button can be
added to `client.js` + a `POST` branch in `index.js` without touching the rest.

## Files

```text
package.json          @local/dsh-config-document, dsh.bundle.patch + dsh.client{web, immediately, inject}
cordis.patch.yml      one Host row: config-document -> '@local/dsh-config-document'
index.js              Host half: authenticated read-only Fetch route
client.js             browser half: settings.action cell + modal (Copy/Download/Reload/Host editor)
locale/en.json        meta.title / meta.description + every UI string
locale/zh.json        the same keys in Chinese
icon.svg              original artwork (document sheet + inspection lens)
README.md             this file
test/harness.mjs      module-loader stub, React stand-in, browser globals, assertions
test/client-check.mjs behaviour of the action and modal (68 checks)
test/host-check.mjs   route registration and every read/degradation path (60 checks)
test/manifest-check.mjs manifest, patch, icon, locale, static rules (68 checks)
test/locale-parity.mjs  locale files == runtime dictionaries, no missing/dead keys (12 checks)
```

## Install

The bundle is a normal profile bundle; install it with `plugin_manager`
(`action: install_bundle`, `target:` the **absolute** directory of this package), or
add it to the profile by hand and let the Loader pick it up:

```yaml
# profile cordis.patch.yml (the file this plugin displays)
- insert:
    - id: config-document
      name: '@local/dsh-config-document'
      # config:            # both optional
      #   routePath: /api/config-document
      #   maxBytes: 1048576
```

A **fresh install** normally activates through the running profile (the Loader inserts
the row and imports `index.js` live), and the browser half hot-reloads. If the Host row
does not come up — for example after editing `index.js` of an already-installed bundle,
because a fresh JavaScript module generation needs a restart — the modal reports the
missing route (HTTP 404) with the restart hint instead of failing silently.

## Verify

1. Open the Web UI → **More → Settings**.
2. The header still shows one **“Open configuration file”** button (not two).
3. Click it: a modal appears with the absolute path of the profile's
   `cordis.patch.yml`, a `N lines · X KB · modified …` hint, and the file text.
4. **Copy** copies the text, **Download** saves `cordis.patch.yml`, **Reload** re-reads
   the Host, **Escape** and a mask click close the modal.
5. **Open in Host editor** on the PCAI Host shows a red alert containing the Host's
   failure message (`path open failed: …`) instead of doing nothing.
6. Offline: `node test/client-check.mjs && node test/host-check.mjs && node test/manifest-check.mjs && node test/locale-parity.mjs`
   (all four exit non-zero on failure).

Optional Host-side probe, once installed and restarted:

```bash
curl -s -b "…session cookie…" http://127.0.0.1:3080/api/config-document | head -c 400
```

The route is authenticated, so an unauthenticated `curl` answers `401` — that is the
expected result and the check that the route is not public.

## Security notes

- **Read-only.** `index.js` calls `readFile`/`stat` only; there is no write, rename or
  delete anywhere in the bundle (`test/manifest-check.mjs` asserts it).
- **No path input.** The route accepts no parameter, so it cannot be turned into an
  arbitrary Host file reader. It only ever answers the single document the Host's own
  settings service prepares.
- **Same authenticated peer as the rest of `/api`.** The route is registered through
  `ctx.connection.fetch`, which applies the Host/Origin fence and requires the
  authenticated owner peer; a non-owner peer gets `403`, no session gets `401`.
- **No caching.** Every response carries `cache-control: no-store`, so the document is
  not retained by the browser cache after the modal closes.
- **Content scope.** The response is the user's own profile patch — the same file the
  shipped button was trying to open on the Host desktop. It is not redacted: a patch can
  contain literal secrets if the operator wrote them there. Prefer `!!js` environment
  references in the patch (they are displayed as written, not evaluated).
- **No remote code.** The browser half requires only `react` from the page's module
  table; it imports no Harness Client package, injects no HTML, frames no page, and
  writes nothing to `document.body`.

## Compatibility and design notes

- The modal is rendered inside the Settings panel's own DOM subtree (no portal), styled
  only with `--dsw-alias-*` theme tokens plus the shared radius/elevation/code-font
  tokens, each with a literal fallback. It inherits light/dark switching from the host.
- Escape and Tab are handled on the dialog and stopped from propagating, so closing the
  document modal does not also close the Settings panel. Focus moves into the dialog on
  open and returns to the trigger on close.
- `priority` is the documented shadowing knob for slot cells (“register at a different
  priority to shadow it (lowest renders)”); shipped plugins use it the same way. This is
  the one intentional exception to the “prefer an additive slot” convention in
  `custom-plugins/README.md`: the request was to fix *the* existing button, not to add a
  second one, and shadowing is how a list-slot cell is replaced.
- The route path must stay under `/api` (`connection` rejects other exact Fetch routes).

## Known limitations

- Read-only (see above).
- **Open in Host editor** can only ever work when the Host has a desktop session; on
  the PCAI container it is expected to fail, and the modal shows the Host's message.
- A document larger than `maxBytes` (1 MiB default) is truncated in the response and
  flagged in the modal; use Download for the complete file only after raising the cap.
- Visual verification in a live page was not available in the authoring session: the
  offline checks cover the rendered tree, handlers, route behaviour, manifest, icon and
  locale parity, but not the painted result in the browser.
