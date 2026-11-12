# App shortcuts (`@local/dsh-app-shortcuts`)

Contributed plugin — the collection's [README](../README.md) has the install/contribution guide.

Three controls at the sidebar foot of the Harness Web UI that open the environment's
sibling pages in a new tab:

| Control | Opens | What it is |
|---|---|---|
| Terminal | `/terminal` | ttyd + tmux terminal manager (tabbed, persistent) |
| Data Manager | `/data-manager` | file browser, uploads, archives, previews |
| Login | `/` | the router's main login / account page |

All are same-origin and served by the pod's nginx. The router's session
passthrough routes the root-relative paths to the signed-in user's own pod, so
the plugin needs no Host route, no fetch and no cookie handling — a plain
`<a href target="_blank">` is the whole integration.

`/` is the one path the router always keeps for itself: with a live session it
renders the signed-in account page (identity, Log out, Open your environment)
above the login form, and without a session it is the plain login page. Opening
it in a new tab therefore lets a user switch accounts or log out without losing
the running Session.

## This is additive, not a replacement

The seat is `sidebar.footer.action`, a **list** slot declared by
`@deepseek-ai/dsh-client-ui-sidebar`. Registering there appends a cell beside
the existing ones (the Cordis panel) and leaves everything else alone:

- `ui-sidebar-terminal` — the in-app right-sidebar shell (`terminal.new`) — untouched.
- `ui-sidebar-files` — the workspace file tree — untouched.
- The right sidebar's guide, tabs and layout — untouched.

The first two tools above are the *heavyweight* versions of those in-app
features: the terminal is tmux-backed and survives a full page reload; the Data
Manager handles uploads/downloads and archive extraction, which the in-app file
tree does not.

## Behaviour

- Sidebar expanded: one row per destination, icon + label.
- Sidebar collapsed to the 56px rail: icon-only buttons stacked vertically.
- Click opens a new tab, so the running Session and its agent stay put.
- Colours come from `--dsw-alias-*` tokens, so light/dark and theme overrides follow.

## Files

| File | Role |
|---|---|
| `index.js` | Host half: `apply()` with no behaviour |
| `client.js` | Browser half: the slot registration and the three controls |
| `cordis.patch.yml` | One `insert` row: `id: app-shortcuts` |
| `locale/en.json`, `locale/zh.json` | Display metadata + UI strings |
| `test/client-check.mjs` | Offline check of the browser half (`node test/client-check.mjs`) |

## Install / remove

Installed with `plugin_manager install_bundle` (target: this directory). It is
a profile bundle, so it applies to every Session in the profile.

To undo, either disable or remove it:

```
plugin_manager  set_bundle   target=@local/dsh-app-shortcuts enabled=false
plugin_manager  remove_bundle target=@local/dsh-app-shortcuts
```

Disabling keeps the files on disk; removing drops the dependency and the
bundle row from the profile's `package.json`. Either way the sidebar returns to
its previous state on the next load.

## Verify

`cordis_inspect_query(platform="client", provider="Slots", method="listSubTree",
input={"root": "sidebar.footer.action"})` should list an occupant
`app-shortcuts` at `order: 20` next to the shipped `cordis-panel`.
