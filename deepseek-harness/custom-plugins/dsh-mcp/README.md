# MCP (`@local/dsh-mcp`) — one bundle for both jobs

Installs **one** thing instead of two: the server list and the live status view.

| Job | Where it shows up | Code |
| --- | --- | --- |
| Add / edit / disable / remove MCP servers, and test an endpoint before saving | Settings → **MCP servers** | `../dsh-mcp-servers` |
| Which MCP servers are connected, their tool counts, and the upstream servers behind a federating gateway | The **MCP** chip above the composer | `../dsh-mcp-status` |

## How it composes

`index.js` imports both halves by **relative path** and applies them to the same
context; `client.js` is the two browser halves concatenated, each of which is a
self-contained `__ModuleLoader__.load()` call.

Relative imports are deliberate. A `link:`-installed package resolves its own
dependencies through the profile, and a sibling package in the same directory
tree is not guaranteed to be there. A relative specifier is a filesystem path,
so it always resolves.

Both halves receive the **same row config**:

```yaml
- id: mcp
  name: '@local/dsh-mcp'
  config:
    servers: []            # the server list (Settings page)
    routePath: /api/mcp-status   # optional, status half
    catalogTools: [mcp_catalog, list_mcp_servers, list_servers, list_mcp_tools]
```

The server half reads `servers`; the status half reads its own keys. They do
not collide.

## Requirements

Both source packages must stay installed next to this one:

- `../dsh-mcp-servers`
- `../dsh-mcp-status`

This bundle carries no copy of their code, so an edit there is picked up here
without republishing.

## Switching from the two separate bundles

1. Install this bundle (`link:` to this directory, or the package name).
2. Move the server list: copy `config.servers` from the old `mcp-servers` row
   into the new `mcp` row in `cordis.patch.yml`.
3. Remove the old `mcp-servers` row from the profile patch, and disable the
   `@local/dsh-mcp-servers` and `@local/dsh-mcp-status` bundles.
4. Restart the Host.

## Verify

```bash
node test/compose-check.mjs
```

Checks that one `apply()` registers all three routes, that the client bundle
carries both halves, and that the patch inserts exactly one row. The two halves
keep their own suites (`../dsh-mcp-servers/test`, `../dsh-mcp-status/test`).
