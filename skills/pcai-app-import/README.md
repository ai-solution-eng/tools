# pcai-app-import

Packages an already-ported HPE Private Cloud AI (PCAI) Helm chart into an `EzAppConfig` custom resource (logo, values, install options), then deploys it — chart uploaded to ChartMuseum, CR applied — via the `ezapp-deploy` MCP server. See `SKILL.md` for the full workflow.

## Prerequisites

### Required

- **A PCAI-ported Helm chart.** The chart must already carry the PCAI integration: an `ezua:` block in `values.yaml` and/or a `kind: VirtualService` template. If it is not ported yet, install and run the companion skill **`pcai-helm-port`** first — this skill chains to it automatically.
- **The `ezapp-deploy` MCP server**, configured in `opencode.json` with these environment variables:
  - `EZAPP_DEPLOY_KEY` — bearer token for the server's authenticated endpoints
  - `EZAPP_DEPLOY_URL` — the server's base URL (also used to derive the cluster domain for the final app URL)

  Without it the skill stops at the deploy step by design — cluster access is mediated, validated, and ledger-tracked by the server, never hand-rolled.
- **python3** — used by the bundled scripts (`scripts/svg_to_png.py`, `scripts/verify_ezappconfig.py`). Both are standard-library only; no pip packages required.
- **helm** — to package the chart (`helm package`) when the chart is a directory rather than an existing `.tgz`.
- **curl** — used for payloads over the tool-call size cap (large chart uploads to `/upload`, large CRs staged to `/manifest`).

### Optional

- **An SVG renderer** — only needed to convert a *generated* SVG icon into a PNG. Recommended: `pip install resvg-py` (self-contained wheel, no system libraries). `cairosvg`, `librsvg` (`rsvg-convert`), or ImageMagick also work; on a bare macOS, `cairosvg` additionally needs the cairo system library (`brew install cairo`). Without any renderer the skill falls back to the bundled generic icon (`assets/fallback-icon.png`) — or fetches the app's official logo from the web when a web search tool is available. macOS `qlmanage` is deliberately not used: QuickLook mis-transforms SVG geometry and flattens transparency.

## Notes

- The skill works whether installed at the project level (`<project>/.opencode/skills/pcai-app-import/`) or globally (`~/.config/opencode/skills/pcai-app-import/`); bundled files are resolved relative to the skill's own directory.
- The workflow has one interactive stop: the user reviews `values-override.yaml` before anything is packaged or deployed.
- Rollbacks (`delete_ezappconfig`, `delete_chart`) only affect objects the `ezapp-deploy` server itself created — its ledger gates both operations.
