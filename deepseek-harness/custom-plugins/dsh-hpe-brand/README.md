# HPE brand (`@local/dsh-hpe-brand`)

Contributed plugin — the collection's [README](../README.md) has the install/contribution guide.

Restyles the Harness Web UI in HPE terms, taking the HPE operations UI (the
Prometheus MCP *Ops Timeline* page) as the design reference: its green accent,
its brand element, and its two-tone product wordmark.

| Where | Before | With this plugin |
|---|---|---|
| Primary button | near-black / near-white | HPE green, text-grade step `#017a5e` (light), `#02c99a` (dark) |
| Switch, checkbox, spinner, slot caret, dock drop hint | neutral / DeepSeek blue | HPE green `#01a982` (light), `#02c99a` (dark) |
| Focus rings, links, selection wash | DeepSeek blue | HPE green, and a pale green wash |
| Sidebar brand mark | the fish | the HPE element |
| Sidebar wordmark | DeepSeek Harness wordmark (vector) | **DeepSeek** + **Harness** in HPE green |
| Blank-session mark | the fish | the HPE element |

## How it works

Three sanctioned extension points, no DOM patching, no root replacement:

1. **Accent — one token layer.** `ctx.theme.overrideTokens()` stacks a
   `--dsw-alias-*` override over the active theme. Eight tokens change:

   | Token | Paints |
   |---|---|
   | `brand-primary` | the brand mark, switches, checkbox accent, spinner |
   | `brand-primary-new-colorprimary-new-color` | the slot caret, the dock drop hint |
   | `brand-text` | the wordmark's accent word |
   | `button-primary-fill` | the primary button |
   | `button-primary-hover` | its hover state |
   | `state-business-primary` | ~90 focus outlines, plus selected-control border and text |
   | `link` | every link |
   | `state-business-tertiary` | the pale selection wash |

   Every token carries a light *and* a dark value, so no scheme sniffing. The
   base palettes are untouched, the layer composes with other layers (later
   wins per token), and unloading the plugin removes it.

   **Two greens, on purpose.** `#01a982` is 3:1 on white — fine for a fill, a
   ring or a graphic, thin for lettering and for a white label sitting on it.
   So the bright brand green paints the mark and the small controls, while the
   text-grade step `#017a5e` (5.3:1) paints the wordmark, links, focus rings
   and the primary button's fill. The button's fill is overridden *separately*
   from `brand-primary` because the shared `label-primary-foreground` token
   cannot be flipped to dark ink without also inverting the contrast button.

2. **Brand mark — three single slots.** `sidebar.brand.mark`,
   `sidebar.brand.name` and `conversation.hero.brand.mark` are *single* slots
   declared by the sidebar and conversation packages for exactly this purpose.
   The mark is drawn from the reference UI's clip-path geometry
   (`polygon(25% 0%, 100% 0%, 75% 100%, 0% 100%)`) on a 24-unit viewBox, inset
   one unit so its ink matches the fish it replaces, and one drawing serves
   both the 24px sidebar seat and the 34px hero seat.

   A single slot keeps only its **first** registration
   (`entriesOfSlot(slot)[0]`, deduped), and the shipped `ui-brand-official` row
   — the whale, the DeepSeek wordmark and the home whale layer — activates
   before any user bundle. So the bundle patch **disables that row**: without
   it, the two sidebar seats stay the whale and these marks never render. That
   also removes the DeepSeek home animation. Remove the
   `- id: ui-brand-official` override from `cordis.patch.yml` (or re-enable the
   row in **Plugins**) to get the shipped brand back.

3. **Styles — component-local.** The wordmark ships its `<style>` as an element
   inside the rendered component, so unmounting the occupant removes it, and
   every colour comes from a `--dsw-alias-*` token, so light/dark and any
   theme change follow. The mark's fill reads `--dsw-alias-brand-primary`, the
   very token this plugin sets, so the artwork is HPE green in both schemes
   without pinning a literal colour.

The two wordmark words are proper nouns and stay untranslated in every locale,
so the browser half registers no locale dictionary; the Plugin Manager
metadata lives in `locale/*.json`.

## Behaviour and limits

- Both themes are covered. Light uses `#01a982` for fills and `#017a5e` for
  text and text-bearing fills; dark uses `#02c99a`.
- This bundle disables the shipped `ui-brand-official` row (see above). The
  Plugin Manager shows it as an entry this bundle switched off; re-enable it to
  restore the whale, at the cost of these marks.
- Turning the plugin off restores the shipped look — the token layer is
  disposed, the three seats fall back to their declared fallbacks (the fish),
  and the whale row comes back if you re-enable it.
- Not covered, on purpose: `state-success-primary` (brand green and success
  green must stay distinguishable), `button-primary-dimmed` (a disabled button
  has to stay visibly disabled), `--dsw-specific-sidebar-fill` (it feeds a
  colour-mix for the sidebar tint, so tinting it risks muddiness), and the
  chart/trajectory palettes (categorical data colours are not brand chrome).
  Two low-traffic links still read `--dsw-alias-brand-primary` at 3:1.
- The browser half loads `immediately`, so the accent is in place before the
  first conversation renders rather than after the first slot mount.

## Files

| File | Role |
|---|---|
| `index.js` | Host half: `apply()` with no behaviour |
| `client.js` | Browser half: token layer, brand mark, wordmark |
| `cordis.patch.yml` | Inserts `id: hpe-brand`, and disables `ui-brand-official` |
| `locale/en.json`, `locale/it.json` | Plugin Manager title + description |
| `icon.svg` | The HPE element |
| `test/client-check.mjs` | Offline check of the browser half (`node test/client-check.mjs`) |

## Install / remove

Installed with `plugin_manager install_bundle` (target: this directory). It is
a profile bundle, so it applies to every Session in the profile. A newly
installed bundle's browser half is fetched on the next page load, so reload the
Web UI once after installing.

To undo, either disable or remove it:

```
plugin_manager  set_bundle   target=@local/dsh-hpe-brand enabled=false
plugin_manager  remove_bundle target=@local/dsh-hpe-brand
```

Disabling keeps the files on disk and restores the shipped look; removing drops
the dependency and the bundle row from the profile's `package.json`.

## Verify

```
cordis_inspect_query(platform="client", provider="Slots", method="listSubTree",
  input={"root": "sidebar.brand.mark"})        # and sidebar.brand.name,
                                               # conversation.hero.brand.mark
```

Each should list an occupant from this bundle (`conversation.hero.brand.mark`
has none without it). The rendered colour is not verifiable from the Host:
confirm the green primary button, the mark and the two-tone wordmark in the
connected page, in both light and dark.
