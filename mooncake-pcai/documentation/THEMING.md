# Theming: HPE palette with Auto / Light / Dark

A ~40-line pattern you can drop into any app. **Auto follows the OS** via
`prefers-color-scheme`; Light and Dark override it and are remembered per browser.

Three properties make it behave:

1. `data-theme` lives on `<html>` **in the markup**, so the right scheme applies on the first
   paint — no flash of the wrong theme before JavaScript runs.
2. The dark rules are scoped to `[data-theme="auto"]` inside the media query, so an explicit
   Light choice wins even when the OS is dark.
3. `color-scheme` is set alongside the variables, so scrollbars, form controls and the
   browser chrome follow too — without it a dark page keeps white scrollbars.

## 1. Markup

```html
<html lang="en" data-theme="auto">
```

```html
<div class="seg" role="group" aria-label="Theme">
  <button data-theme-btn="auto">Auto</button>
  <button data-theme-btn="light">Light</button>
  <button data-theme-btn="dark">Dark</button>
</div>
```

## 2. CSS

```css
:root{
  color-scheme: light;
  --green:#01a982; --green-strong:#007a5e; --green-soft:#e6f6f1;
  --bg:#f5f5f7; --panel:#ffffff; --ink:#1a1a1a; --muted:#6b7280; --line:#e4e4e8;
  --bar-bg:#ececf0; --danger:#c0392b; --danger-soft:#fdecea;
}

@media (prefers-color-scheme: dark){
  :root[data-theme="auto"]{
    color-scheme: dark;
    --green:#2ed3a8; --green-strong:#4be3b8; --green-soft:#102b25;
    --bg:#0e0e10; --panel:#18181b; --ink:#f2f2f2; --muted:#9aa0a6; --line:#2b2b30;
    --bar-bg:#2a2a30; --danger:#ff9a90; --danger-soft:#3a1d1a;
  }
}

:root[data-theme="dark"]{
  color-scheme: dark;
  --green:#2ed3a8; --green-strong:#4be3b8; --green-soft:#102b25;
  --bg:#0e0e10; --panel:#18181b; --ink:#f2f2f2; --muted:#9aa0a6; --line:#2b2b30;
  --bar-bg:#2a2a30; --danger:#ff9a90; --danger-soft:#3a1d1a;
}

.seg{display:flex;border:1px solid var(--line);border-radius:8px;overflow:hidden}
.seg button{background:transparent;color:var(--muted);border:0;padding:6px 11px;
            font-size:12.5px;cursor:pointer}
.seg button.on{background:var(--green-soft);color:var(--green-strong);font-weight:600}
```

## 3. JavaScript

```js
var THEME_KEY = "app-theme";   // one key per app
function applyTheme(t){
  document.documentElement.setAttribute("data-theme", t);
  try { localStorage.setItem(THEME_KEY, t); } catch (e) {}
  var b = document.querySelectorAll("[data-theme-btn]");
  for (var i = 0; i < b.length; i++) {
    if (b[i].getAttribute("data-theme-btn") === t) { b[i].classList.add("on"); }
    else { b[i].classList.remove("on"); }
  }
}
var saved = "auto";
try { saved = localStorage.getItem(THEME_KEY) || "auto"; } catch (e) {}
applyTheme(saved);
var seg = document.querySelectorAll("[data-theme-btn]");
for (var j = 0; j < seg.length; j++) {
  seg[j].onclick = function(){ applyTheme(this.getAttribute("data-theme-btn")); };
}
```

## Why not just `@media (prefers-color-scheme)` alone

Because users want to override it. A media-only theme cannot be pinned to light on a dark-OS
machine, which is exactly the case that gets complained about. Keeping `auto` as an explicit
state — rather than "no choice" — also means the toggle shows which mode is active.

## Palette

`#01a982` is HPE green, the same value MM RAG uses; the light surfaces (`#f5f5f7` / `#ffffff` /
`#1a1a1a` ink) match its neutral ramp. Dark mode swaps to a near-black page (`#0e0e10`) with
`#18181b` panels and a lightened accent (`#2ed3a8`) that holds contrast on dark surfaces.

Two things to check when porting:

- **Accent contrast.** `#01a982` on `#0e0e10` is roughly 3.4:1 — fine for large text and UI
  borders, thin for body copy. That is why the dark theme lightens it to `#2ed3a8`.
- **Storage blocked.** The `try/catch` around `localStorage` matters in iframes and private
  modes; without it a blocked store throws on load and the theme never applies.
