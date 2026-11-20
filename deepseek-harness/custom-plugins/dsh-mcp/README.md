# MCP (`@local/dsh-mcp`) — one bundle for both jobs

Installs **one** thing: the server list and the live status view.

| Job | Where it shows up | Code |
| --- | --- | --- |
| Add / edit / disable / remove MCP servers, and test an endpoint before saving | Settings → **MCP servers** | `src/servers/` |
| Which MCP servers are connected, their tool counts, and the upstream servers behind a federating gateway | The **MCP** chip above the composer | `src/status/` |

Both halves are vendored in `src/`; nothing outside this package is required.

## Layout

```
index.js                  composes both halves: one apply, one row, one bundle
client.js                 GENERATED — see scripts/compose-client.mjs
src/servers/              the management half (page, routes, bridge mounting)
src/status/               the status half (probe, upstream discovery)
scripts/compose-client.mjs   regenerates client.js from both halves
test/                     compose-check plus each half's own suites
```

`client.js` is generated because the harness builds **one client bundle per
package** and matches a single registration against the package name: the two
halves cannot register themselves. After editing either half:

```bash
node scripts/compose-client.mjs
```

`test/compose-check.mjs` fails when the checked-in bundle is stale.

## The row

```yaml
- id: mcp-servers                 # must stay "mcp-servers"
  name: '@local/dsh-mcp'
  config:
    servers: []                  # the server list (Settings page)
    routePath: /api/mcp-status   # optional, status half
    catalogTools: [mcp_catalog, list_mcp_servers, list_servers, list_mcp_tools]
```

**The row id must stay `mcp-servers`.** The browser half addresses its settings
namespace by that literal name; a different id makes the page write into
whatever row it can reach instead. The bundle patch pins it for that reason.

The server half reads `servers`; the status half reads its own keys. They do
not collide.

## Upstream discovery

Three paths, in order:

1. **MCP catalog tool** — `mcp_catalog`, `list_mcp_servers`, `list_servers`,
   `list_mcp_tools`, or a strict `list_*/get_*/show_*` name guess.
2. **REST catalog** — `{origin}/v1/mcp/server` derived from an `/mcp` URL, or an
   explicit **Upstream catalog URL** on the row.
3. **Tool-name grouping** — when the catalog exists but denies access (401/403),
   the upstreams are read from namespaced tool names (`server-tool`). A 404
   means "not a gateway", so a plain server's tools are never misread.

## Requirements

- A Host with the `connection` and `tools` services (every Web profile).
- `pnpm` on `PATH` for the install itself.

Saving the server list rewrites `cordis.patch.yml`, and a deployment whose
supervisor watches that file restarts the Host to apply it. The page treats a
dropped save response as success for that reason.

## Verify

```bash
node test/compose-check.mjs        # composition, routes, generated bundle
node test/servers/client-check.mjs
node test/servers/host-check.mjs
node test/servers/schema-check.mjs
node test/status/client-check.mjs
node test/status/host-check.mjs
```
