# Admin console navigation — design proposal

**Status: IMPLEMENTED 2026-11-18** in `src/pcai_llm/ui/admin.html` (same `data-tab`
contract, no backend change). This note is kept as the design record — the IA rationale,
the interaction spec and the open questions below are what the implementation follows.
Approved mockup: [`admin-nav-redesign.html`](admin-nav-redesign.html) — open it in a browser
(self-contained, no server, no admin key); its "Compare: today's top bar" button still
flips between the old shell and the design.

Scope: `src/pcai_llm/ui/admin.html` (the `/admin` console) only. No backend, API or panel change.

---

## 1. What is actually wrong today

| Symptom | Detail |
|---|---|
| 16 flat tabs | Every panel is one click away but nothing tells you *where* you are in a hierarchy. |
| Invisible grouping | Four `.navgroup` divs separated by a 1px green rule — no labels. A new operator cannot tell why "Guardrails" sits with "Audit", or what the last group is for. |
| The bar costs vertical space | It wraps on narrower windows and eats ~40–90px above every panel; it is sticky, so it also pins that cost. |
| No lookup | There is no way to find a tab by name. "Where is the pricing table?" is a 16-item scan. |
| Header crowding | Brand + back-link + key input + Connect + status + theme in one row; on a phone it wraps to 2–3 rows. |
| No deep link | The active tab lives in `sessionStorage` only, so a link to a panel cannot be shared and Back does not work. |

## 2. Proposal in one line

Move navigation into a **collapsible left rail** with **labelled, collapsible sections** and a
**search box**, slim the header to identity + connection controls, and add a breadcrumb so
orientation survives collapsing.

## 3. Information architecture

Five labelled sections, 16 items — same `data-tab` values, so nothing downstream moves.

| Section | Items | Notes |
|---|---|---|
| **Access & safety** | API keys · Profiles · Enrollment · Guardrails | Guardrails **moved here** from observability: it is a policy surface evaluated around model calls, not telemetry. |
| **Models & routing** | Model endpoints · Presented models · Model groups · Routing | Routing **moved here** from the old "background features" bar — it is how a model is picked, i.e. a model concern. |
| **Agents & tools** | Agents · MCP servers | MCP **moved here** from inventory: an MCP server exists to give agents/keys tools. |
| **Observability** | Usage & cost · Traces · Live metrics · Admin audit | Metrics moves in from the inventory bar (it is a telemetry surface). |
| **Operations** | Off-peak billing · Memory distiller | The two background/GPU-economy features, unchanged in membership. |

Old → new in one line: the four unlabelled bars become five labelled sections; three items move
(Guardrails, Routing, MCP) to the section that owns their concept; nothing is added or removed.

Labels are also sharpened where the old name was ambiguous, keeping the id:
`Keys→API keys`, `Models→Model endpoints`, `Advertised→Presented models`,
`MCP→MCP servers`, `Metrics→Live metrics`, `Usage→Usage & cost`, `Audit→Admin audit`,
`Enroll→Enrollment`, `Memory Distiller→Memory distiller`.

## 4. Component spec

**Rail** — 252px expanded, 62px collapsed; full height, own scroll, sticky; never scrolls with the
panel. Footer holds the two things that are not panels: **Chat console** (the old header
back-link) and **Theme**.

**Section header** — 10.5px uppercase, dim, chevron on the right; click collapses that section;
the set of collapsed sections is remembered. In the collapsed rail the headers degrade to hairline
dividers, so the icon column keeps its rhythm.

**Item** — 18px stroke icon + label. Active state = accent text, 2px accent bar on the left edge,
tinted background. Hover = subtle surface lift.

**Collapsed rail** — icons only with native tooltips, and the search box is replaced by a
magnifier button that expands the rail and focuses the field (so search is never lost).

**Header (slimmed)** — hamburger (mobile only) · admin-key input · Connect · connection status ·
spacer · theme. The back-link moves to the rail footer, which frees the header's right side.

**Breadcrumb** — `Section / Item` above the panel. Cheap, and it is what keeps you oriented once
the rail is collapsed.

**Search** — filters items live across the label, a keyword list and the section name, so
"pricing" finds Usage & cost and "federation" finds MCP servers. Sections with no hits
disappear; a collapsed section auto-expands while a query is active; `Enter` opens the first
hit; `Esc` clears.

## 5. Interaction spec

| Input | Effect |
|---|---|
| `/` | focus the search box (ignored while typing in a field) |
| `Esc` | clear the search, or close the mobile drawer |
| `[` | toggle the rail |
| `Enter` in search | open the first match |
| click section header | collapse/expand that section |

**Persistence** (all `localStorage`, all failure-tolerant — storage can be blocked):
`pcai-admin-rail` = open|collapsed · `pcai-admin-navsecs` = collapsed section ids ·
`pcai-admin-tab` = the existing key, unchanged so an operator's current panel survives the upgrade.

**Deep link** — the active panel is mirrored to `#tab-<name>`; Back/Forward work and a panel can be
linked. Boot order: hash → saved tab → `keys`.

**Mobile (≤900px)** — the rail becomes an off-canvas drawer over a scrim; the hamburger opens it;
picking an item closes it; `Esc` closes it. The header stops wrapping.

## 6. Implementation plan (admin.html only)

1. Replace the `<header>` + `<nav>` block with the rail + slim header + breadcrumb markup.
   **Every `data-tab` value is preserved**, which is the whole compatibility story: `activateTab()`,
   the per-panel loaders and the boot restore keep working against the same contract.
2. CSS: the `nav` / `.navgroup` rules become rail rules; design tokens are untouched, so the two
   themes and the light ramp are inherited, not re-derived.
3. JS: extend the existing `activateTab(name)` (active state, breadcrumb, hash), add
   `filterNav()`, section-collapse persistence, rail collapse, drawer, and the keyboard map.
   ~120 lines, no new dependency, no build step (the console is one static file the app serves).
4. Tests: two assertions in `tests/test_admin_ui.py` pin the old markup —
   the "exactly FOUR `.navgroup` divs" test (line ~950) and a literal
   `<button data-tab="groups">Model groups</button>` check (line ~2192). Both get replaced by a
   structural assertion over the new tree (5 sections, 16 items, every `data-tab` exactly once,
   every item's panel present). Nothing else in the suite touches nav structure — checked.
5. No route, API, config or data-flow change.

## 7. Risks and non-goals

- **Width**: 252px is real estate on a 1280px screen. Mitigations: collapse (remembered) and, if
  you want it, auto-collapse below ~1100px. *Decision needed — see questions.*
- **Not in scope**: the team-leader console (`team_leader.html`, 4 tabs) keeps its top bar for now.
- **Not in scope**: search over panel *content*, and permission-aware nav (hiding sections an
  admin key cannot use — `/admin` is all-or-nothing today).

## 8. Questions for you

1. Section names: **Access & safety** vs *Access & policy*; **Agents & tools** vs *Tools & MCP*.
2. Guardrails: keep it under **Access & safety** (proposed) or move it to **Agents & tools**, since
   it also gates tool calls?
3. Admin-key input: keep it in the header (proposed) or move it into the rail footer and give the
   header entirely to the page title?
4. Auto-collapse the rail below ~1100px, or leave it open and let the operator decide?
5. Same shell for the team-leader console in the same pass, or leave it?
