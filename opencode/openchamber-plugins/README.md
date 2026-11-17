# OpenChamber plugins

Contributed UI extensions for [OpenChamber](https://openchamber.dev), one directory per plugin.
Each directory is a standalone package: a `package.json` with an `openchamber` manifest block, a
panel page, and a browser script that OpenChamber loads in a sandboxed iframe.

Unlike the sibling [`deepseek-harness/custom-plugins`](../../deepseek-harness/custom-plugins/README.md),
nothing here is baked into a chart or an image. OpenChamber ships an extension system
(Settings → Extensions, API version 1); a plugin is installed **per user, per instance**, into that
user's data directory, so it survives pod restarts but is not shared between users.

| Plugin | Adds |
|---|---|
| [`app-shortcuts`](app-shortcuts/README.md) | Terminal / Data Manager / Login buttons (new tab) and in-app terminal + files rows in the context rail |

## How an extension works

- A **panel** is a page in the right-hand context rail. `panel.id` is kebab-case and unique;
  `panel.icon` is a Remixicon name or a package `.svg`; `panel.entry` is the page to mount.
- The page runs in `<iframe sandbox="allow-scripts">` — an opaque origin. It cannot touch the app
  DOM, cookies or `window.parent.location`. Everything it does goes through the
  `@openchamber/sdk` `connectHost()` postMessage bridge.
- Useful host calls: `openUrl` (new tab), `openSurface` (switch the context rail to `file`, `git`,
  `terminal`, `diff`, `browser`, `pr`, `linear`, `notes`, `plan`, `context`, `chat`), `toast`,
  `compose`, `attach`, `prompt`, `startSession`, `storage`, `request`, `readFile`/`writeFile`.
- Anything that needs the user's approval is declared under `contributes.capabilities` and approved
  once at install. `openUrl` / `openSurface` need no capability.
- `contributes.page`, `attach`, `actions`, `commands`, `tools`, `integration`, `service` and
  `filesystem` are the other extension points. See the SDK docs linked below.

OpenChamber **never compiles guest code**: `panel/main.js` must ship as one classic (IIFE) file.
The bundler in each plugin does that with esbuild.

## Installing a plugin

Settings → Extensions → the **Folder, ZIP, or URL** field → Add. Three sources:

1. **Folder path** — the fastest for a shared deployment. The platform units mount the shared PVC
   at `/mnt/shared`, so every colleague can paste the same path, e.g.

   ```
   /mnt/shared/califra-opencode/tools/opencode/openchamber-plugins/app-shortcuts
   ```

   The install stores the path, so editing and rebuilding the folder shows up on the next panel
   open — no reinstall. Moving or renaming the folder breaks every install that points at it.
2. **HTTPS Git URL** — `https://github.com/<org>/<repo>.git` (the repository root must carry
   `package.json`). This is the only source that gets Settings → Extensions → *Check for updates*:
   bump `version` in `package.json` to ship one. `#tag` / `#branch` pins the channel.
3. **ZIP** — drag it onto the field or use the archive button. Copied into
   `{dataDir}/extensions/<id>`; no update path. Build one with

   ```sh
   cd openchamber-plugins && zip -r app-shortcuts.zip app-shortcuts -x '*/node_modules/*'
   ```

   An https URL to that `.zip` works in the same field, and redirects are followed — a committed
   archive behind a GitHub `raw` link is a one-line install for units that do not mount
   `/mnt/shared`.

URL installs refuse private/loopback hosts (the server fetches them itself), so a Git URL must be
publicly reachable. Folder installs have no such limit.

Installs are per user. There is no global/tenant-wide install and no auto-install from the chart;
each colleague adds the extension once in their own instance.

## Anatomy of a plugin

```
<plugin>/
  package.json          # name, semver version, openchamber.contributes, build devDependencies
  src/main.ts           # browser-half source
  panel/index.html      # the page the iframe loads
  panel/main.js         # built IIFE — committed, never compiled by the host
  icon.svg              # rail mark, drawn as a currentColor mask
  build.mjs             # esbuild: src/main.ts -> panel/main.js
  test/client-check.mjs # offline jsdom check (no network, no running app)
  README.md
```

Rules that bite:

- `apiVersion` is `1`. `version` is required semver and shown on the Settings card.
- `panel.entry` stays inside the package: no `..`, no absolute path, no URL.
- The iframe has **no** `allow-same-origin`, `allow-popups`, or `allow-forms`. Links and new tabs
  go through `host.openUrl`; `window.open` inside the frame is blocked.
- Relative `script`/`link`/`img` URLs in the entry HTML are served from the package; build every
  script into one file rather than relying on runtime-created URLs (relay mode does not intercept
  those).
- **Inline the panel script into the entry HTML.** The guest frame has an opaque origin, so
  `<script src>`/`<link href>` are cookieless cross-origin requests; behind a cookie-gated proxy
  they are redirected to the login page and refused. The HTML is a navigation and does send
  cookies, so a self-contained page is the only shape that works everywhere. `app-shortcuts` does
  this in `build.mjs`.
- Keep the browser half free of Node built-ins. It is a browser IIFE, not a service.
- A `service` (`contributes.service`) runs as a real process with the user's full access and no
  sandbox — declare one only when the page cannot do the job.

## Contributing a new plugin

1. Copy `app-shortcuts/` — it is the minimal template (one panel, two host calls, no capabilities).
2. Name the package `@local/openchamber-<name>`, keep `"private": true`, give `panel.id` a unique
   kebab-case value.
3. Prefer `host.openSurface` / `host.openUrl` over anything that needs a capability; a plugin that
   asks for nothing installs with no approval dialog.
4. Ship an offline check (`npm run check`) and document install + verify in the plugin's README.
5. Add it to the table above.

## References

- Extension author guide — <https://openchamber.dev/docs/sdk/>
- Host API — <https://openchamber.dev/docs/sdk/host/>
- UI kit — <https://openchamber.dev/docs/sdk/ui/>
- Examples — <https://github.com/openchamber/openchamber/tree/main/packages/sdk/examples>

The SDK version matches the app release it shipped with; `@openchamber/sdk@1.24.2` is the contract
of OpenChamber 1.24.2. Installed extensions load in web and desktop; VS Code and mobile do not load
them yet.
