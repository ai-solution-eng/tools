# App shortcuts (`@local/openchamber-app-shortcuts`)

Contributed OpenChamber extension — the collection's [README](../README.md) has the install and
contribution guide.

Adds an **App Shortcuts** panel to the OpenChamber context rail (the vertical icon strip on the
right edge) with the environment's sibling pages one click away:

| Control | Opens | What it is |
|---|---|---|
| Terminal | `/terminal` | ttyd + tmux terminal manager (tabbed, persistent across reloads) |
| Data Manager | `/data-manager` | file browser, uploads, downloads, archives, previews |
| Login / Account | `/` | the router's account page: identity, Log out, Open your environment |
| In-app terminal | right-rail `terminal` surface | OpenChamber's own terminal panel |
| In-app files | right-rail `file` surface | OpenChamber's own file editor |

The first three open in a **new tab**, so the running session and its agent stay put. The last two
switch the in-app context rail, which is the lightweight version of the same tools.

## Why the URLs are derived, not hardcoded

The guest frame is served from `<app base>/api/guests/<id>/panel/index.html`, so everything before
`/api/guests/` is the browser-visible OpenChamber base. The panel resolves its targets against that
base:

| Where OpenChamber is served | Terminal resolves to |
|---|---|
| `https://opencode.example.com/` | `https://opencode.example.com/terminal` |
| `https://opencode.example.com/francesco-caliva/` | `https://opencode.example.com/francesco-caliva/terminal` |

Nothing is configured per deployment. `/` is resolved against the host root on purpose: the router
always keeps the bare host for the login / account page.

Both URL shapes reach the same pod. `opencode-web-helm` proxies `/{username}/terminal` straight to
the unit, and its session-keyed passthrough serves root-relative `/terminal` to the signed-in
user's pod — the same mechanism the in-pod pages and the login page already rely on.

## Behaviour

- The three page rows call `host.openUrl()`, which the host opens with `window.open(..., '_blank',
  'noopener,noreferrer')`. The click is attributed to the guest RPC, so no popup blocker fights it.
- The two in-app rows call `host.openSurface()`. The rail switches to that panel.
- Colours come from `applyHostReady()` (`--oc-*` tokens), so light/dark and theme overrides follow.
- Opened outside OpenChamber (a bare `panel/index.html`), the page explains it needs the app instead
  of hanging on a host handshake.
- No capabilities are declared: `openUrl` and `openSurface` need no approval.

## Files

| File | Role |
|---|---|
| `package.json` | manifest (`openchamber.contributes.panel`) + build devDependencies |
| `src/main.ts` | source of the browser half |
| `panel/index.template.html` | hand-written page; `build.mjs` inlines the bundle into it |
| `panel/index.html` | generated entry, committed — the bundle is inline, no `<script src>` |
| `panel/main.js` | built IIFE, committed — OpenChamber never compiles guest code |
| `icon.svg` | rail mark (drawn as a `currentColor` mask) |
| `test/client-check.mjs` | offline check: jsdom frame, SDK handshake, every row clicked |

### Why the script is inlined

A guest frame is `sandbox="allow-scripts"` (and the host adds a matching
`Content-Security-Policy: sandbox allow-scripts`), so its document has an **opaque origin**. A
classic `<script src="…">` from that document is a **cookieless cross-origin request**. Behind the
platform's cookie-gated proxy the request is redirected to the SSO login page, the browser refuses
the HTML with `X-Content-Type-Options: nosniff`, and the panel renders but does nothing.

The page itself is a navigation, so it does send cookies. Inlining the bundle keeps the panel at
**zero subresource requests** and working behind that proxy. `build.mjs` writes both artifacts;
edit `src/main.ts` / `panel/index.template.html`, never `panel/index.html`.

## Build

```sh
npm install
npm run build     # esbuild -> panel/main.js, then inlines it into panel/index.html
npm run check     # offline jsdom check, no network
```

Commit the regenerated `panel/main.js` **and** `panel/index.html` with any source change — installs
from a folder, a ZIP or Git read those files.

## Install

Settings → Extensions → **Folder, ZIP, or URL** → Add. The value is one of:

- the folder path — `/mnt/shared/califra-opencode/tools/opencode/openchamber-plugins/app-shortcuts`
  (every platform unit mounts the shared PVC at `/mnt/shared`)
- a Git URL to this directory published as its own repository, when you want update checks
- a ZIP of this directory, uploaded from the archive button

Folder installs run from the folder: edit, `npm run build`, reopen the panel. ZIP and Git installs
copy into `{dataDir}/extensions/app-shortcuts`; Git installs update from Settings → Extensions when
`version` is bumped.

## Verify

- Settings → Extensions lists **App Shortcuts v1.0.0**, enabled.
- The context rail shows the 2×2 + arrow icon; opening it renders the five rows.
- Clicking Terminal opens `…/terminal` in a new tab; clicking In-app terminal switches the rail.

Remove it from the same page (the card's menu). Disabling keeps the package on disk.
