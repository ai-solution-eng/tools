# MCP status

Contributed plugin — the collection's [README](../README.md) has the install/contribution guide.

A Harness Web plugin that answers one question without asking the model: **which MCP servers is this
Harness connected to right now, and what sits behind them?**

A small `MCP` row sits below the message composer with a status dot and a count. Click it and it
expands into every configured MCP server — transport, endpoint, live probe result, latency and tool
count — plus, for a server that federates others, the upstream MCP servers it reports.

```
● MCP   1 of 1 connected   · 10 servers
  ├─ ● pcai-llm-gateway   streamable-http · connected · 2 tools · 85 ms
  │    http://pcai-llm-gateway-service.pcai-llm.svc.cluster.local:9200/mcp
  │    Servers behind pcai-llm-gateway
  │      ● applygate      6 tools
  │      ● k8s-ops        20 tools
  │      ● logsearch      5 tools
  └─ …
```

## Why a probe instead of a flag

The bridge plugin (`@deepseek-ai/dsh-mcp-client`) publishes no status surface, and its Loader row
stays `active` even when the server is down — `failOnStartupError` defaults to `false`. Activation
state therefore cannot answer the question, so this plugin reads the **resolved** config of every MCP
client row from the Loader and probes each endpoint with a real MCP handshake
(`initialize` → `notifications/initialized` → `tools/list`). A federating server that exposes a
catalog tool is asked for its upstream servers in the same way, which is how one connection can
stand for many MCP servers.

On Cordis 0.2.2+ the resolved object is `fiber.effectiveConfig`. `fiber.config` is the **raw** row
input retained for the next activation, where a `!!js` scalar is still the inert `{ __jsExpr }`
marker the YAML dialect produces — reading it silently drops expression-derived credentials (for
example an `Authorization` header built from an environment variable) and reports a healthy server
as `disconnected` with `HTTP 401`. `test/host-check.mjs` covers that regression.

## Host / client split

| Half | File | Role |
|---|---|---|
| Host | `index.js` | Discovers MCP rows, probes them, serves the snapshot |
| Host | `probe.js` | Dependency-free Streamable-HTTP MCP probe (JSON and `text/event-stream`) |
| Client | `client.js` | The composer-dock row and its expandable panel |

The snapshot is served on `GET /api/mcp-status` (and `?upstream=1` for the catalog), registered
through `ctx.connection.fetch`, so only the owner browser peer can read it. Request headers — which
carry the MCP credential — never leave the Host: the wire projection in `toWire()` whitelists fields,
and `test/host-check.mjs` asserts the serialized snapshot contains neither the token nor a `headers`
key.

## Configuration

Every value is optional; the defaults are what the plugin uses.

| Field | Default | Meaning |
|---|---|---|
| `routePath` | `/api/mcp-status` | Authenticated route the browser half reads |
| `probeTimeoutMs` | `4000` | Budget for one endpoint probe |
| `connectionTtlMs` | `5000` | How long one probe result is reused |
| `upstreamTtlMs` | `60000` | How long a federating server's catalog is reused |
| `catalogTool` | `mcp_catalog` | Tool that lists a federating server's upstream servers |
| `maxToolsListed` | `24` | Tool names included per connection |
| `maxUpstreamServers` | `64` | Upstream rows included per connection |

```yaml
- id: mcp-status
  name: '@local/dsh-mcp-status'
  config:
    probeTimeoutMs: 8000
```

## States

| State | Meaning |
|---|---|
| `connected` | The endpoint completed an MCP handshake |
| `disconnected` | The probe failed; the row carries the reason |
| `disabled` | The Loader row is disabled |
| `configured` | A stdio server; reported from the bridge's registered tools, never spawned |
| `unknown` | No endpoint, an unsupported transport, or no bridge tools for a stdio row |

## Tests

```
node test/host-check.mjs     # probe + snapshot against a local mock MCP server (21 checks)
node test/client-check.mjs   # render path against a React shim (11 checks)
```

The host check covers plain-JSON and `text/event-stream` responses, `401`/refused-connection
failures, catalog parsing (including the FastMCP `{ result: "<json>" }` wrapper), row
classification, the wire whitelist, and the route handler. The client check renders the collapsed
and expanded views, asserts the style sheet references only `--dsw-alias-*` tokens, and asserts the
bundle requires nothing but `react`.

## Notes

- A Host-side code change is only re-imported when the Harness process restarts; the client half
  reloads through the Web client HMR transport.
- stdio MCP servers are not spawned for a status read; their state comes from the tools the bridge
  has registered.
- The upstream list reflects what the federating server reports. A server it cannot reach appears as
  an unreachable row rather than disappearing.
