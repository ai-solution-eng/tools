/**
 * Development check for the MCP status plugin's Host half.
 * Runs the shipped probe + snapshot code against a local mock MCP server and a
 * fake Loader, then asserts the wire projection leaks no request headers.
 *
 * Not part of the installed bundle; run with `node test/host-check.mjs`.
 */
import { createServer } from 'node:http'
import assert from 'node:assert/strict'
import { probeMcpHttp, normalizeCatalog, readToolPayload } from '../probe.js'
import { buildSnapshot, describeConnection, inject, resolveSettings, summarize, toWire } from '../index.js'

const SESSION = 'session-abc'
const SECRET = 'Bearer super-secret-token'

/** One mock MCP server whose behaviour is selected by `mode`. */
function startMock(mode) {
  const server = createServer((req, res) => {
    let body = ''
    req.on('data', (chunk) => { body += chunk })
    req.on('end', () => {
      const send = (payload) => {
        res.writeHead(200, { 'content-type': 'application/json', 'mcp-session-id': SESSION })
        res.end(JSON.stringify(payload))
      }
      if (req.method === 'DELETE') { res.writeHead(200).end(); return }
      if (mode === 'unauthorized') {
        res.writeHead(401, { 'content-type': 'application/json' })
        res.end(JSON.stringify({ error: 'unauthorized: missing or invalid API key' }))
        return
      }
      const message = JSON.parse(body || '{}')
      if (message.method === 'initialize') {
        if (req.headers.authorization !== SECRET) {
          res.writeHead(401, { 'content-type': 'application/json' })
          res.end(JSON.stringify({ error: 'bad token' }))
          return
        }
        const result = {
          protocolVersion: '2026-07-28',
          capabilities: { tools: { listChanged: true } },
          serverInfo: { name: 'mock-gateway', version: '9.9.9' },
        }
        if (mode === 'sse') {
          res.writeHead(200, { 'content-type': 'text/event-stream', 'mcp-session-id': SESSION })
          res.end(`event: message\ndata: ${JSON.stringify({ jsonrpc: '2.0', id: message.id, result })}\n\n`)
          return
        }
        send({ jsonrpc: '2.0', id: message.id, result })
        return
      }
      if (message.method === 'notifications/initialized') { res.writeHead(202).end(); return }
      if (message.method === 'tools/list') {
        send({
          jsonrpc: '2.0',
          id: message.id,
          result: {
            tools: mode === 'litellm'
              ? [
                  { name: 'list_mcp_servers', description: 'list upstream servers' },
                  { name: 'search', description: 'search' },
                ]
              : [
                  { name: 'mcp_catalog', description: 'list servers' },
                  { name: 'mcp_call', description: 'call a tool' },
                ],
          },
        })
        return
      }
      if (message.method === 'tools/call') {
        if (mode === 'litellm') {
          // LiteLLM-style: a differently named tool, a data envelope, and
          // server_url/tool_count/status keys instead of server/tools/error.
          const payload = {
            data: [
              { name: 'github', server_url: 'http://github:8080/mcp', tool_count: 7, status: 'connected' },
              { name: 'slack', server_url: 'http://slack:8080/mcp', tool_count: 3, status: 'error' },
            ],
          }
          send({
            jsonrpc: '2.0',
            id: message.id,
            result: { content: [{ type: 'text', text: JSON.stringify(payload) }] },
          })
          return
        }
        if (mode === 'catalog-error') {
          send({ jsonrpc: '2.0', id: message.id, result: { isError: true, content: [{ type: 'text', text: 'catalog exploded' }] } })
          return
        }
        const catalog = {
          servers: [
            { server: 'applygate', url: 'http://applygate:9102/mcp', tools: [{ tool: 'plan_apply' }, { tool: 'apply_manifest' }] },
            { server: 'k8s-ops', url: 'http://k8s:9090/mcp', tools: [{ tool: 'list_pods' }] },
            { server: 'logsearch', error: { message: 'connect ECONNREFUSED' } },
          ],
        }
        if (mode === 'fastmcp') {
          // FastMCP: typed `str` return -> text block AND { result: <json string> }.
          const text = JSON.stringify(catalog, null, 2)
          send({
            jsonrpc: '2.0',
            id: message.id,
            result: { content: [{ type: 'text', text }], structuredContent: { result: text } },
          })
          return
        }
        if (mode === 'structured-only') {
          send({
            jsonrpc: '2.0',
            id: message.id,
            result: { content: [], structuredContent: { result: JSON.stringify(catalog) } },
          })
          return
        }
        if (mode === 'structured-object') {
          send({
            jsonrpc: '2.0',
            id: message.id,
            result: { content: [], structuredContent: catalog },
          })
          return
        }
        send({
          jsonrpc: '2.0',
          id: message.id,
          result: { content: [{ type: 'text', text: JSON.stringify(catalog) }] },
        })
        return
      }
      send({ jsonrpc: '2.0', id: message.id, error: { code: -32601, message: 'method not found' } })
    })
  })
  return new Promise((resolve) => {
    server.listen(0, '127.0.0.1', () => resolve({ server, port: server.address().port }))
  })
}

const results = []
function record(name, fn) {
  return Promise.resolve()
    .then(fn)
    .then(() => results.push(`ok   ${name}`))
    .catch((error) => {
      results.push(`FAIL ${name}: ${error.message}`)
      process.exitCode = 1
    })
}

const json = await startMock('json')
const sse = await startMock('sse')
const unauthorized = await startMock('unauthorized')
const catalogError = await startMock('catalog-error')
const fastmcp = await startMock('fastmcp')
const structuredOnly = await startMock('structured-only')
const structuredObject = await startMock('structured-object')
const litellm = await startMock('litellm')
const deadPort = await new Promise((resolve) => {
  const server = createServer(() => {})
  server.listen(0, '127.0.0.1', () => {
    const port = server.address().port
    server.close(() => resolve(port))
  })
})

const headers = { Authorization: SECRET, 'Content-Type': 'application/json' }

await record('probe: plain JSON handshake + tool list', async () => {
  const probe = await probeMcpHttp({ url: `http://127.0.0.1:${json.port}/mcp`, headers, timeoutMs: 3000 })
  assert.equal(probe.ok, true, probe.error)
  assert.equal(probe.protocolVersion, '2026-07-28')
  assert.deepEqual(probe.serverInfo, { name: 'mock-gateway', version: '9.9.9' })
  assert.deepEqual(probe.toolNames, ['mcp_catalog', 'mcp_call'])
  assert.deepEqual(probe.capabilities, ['tools'])
  assert.ok(probe.latencyMs >= 0)
})

await record('probe: text/event-stream responses', async () => {
  const probe = await probeMcpHttp({ url: `http://127.0.0.1:${sse.port}/mcp`, headers, timeoutMs: 3000 })
  assert.equal(probe.ok, true, probe.error)
  assert.equal(probe.serverInfo.name, 'mock-gateway')
  assert.equal(probe.toolNames.length, 2)
})

await record('probe: unauthorized is a readable failure', async () => {
  const probe = await probeMcpHttp({ url: `http://127.0.0.1:${unauthorized.port}/mcp`, headers, timeoutMs: 3000 })
  assert.equal(probe.ok, false)
  assert.match(probe.error, /HTTP 401/)
  assert.match(probe.error, /unauthorized/)
})

await record('probe: wrong credential fails the handshake', async () => {
  const probe = await probeMcpHttp({ url: `http://127.0.0.1:${json.port}/mcp`, headers: { Authorization: 'Bearer nope' }, timeoutMs: 3000 })
  assert.equal(probe.ok, false)
  assert.match(probe.error, /HTTP 401/)
})

await record('probe: refused connection is a readable failure', async () => {
  const probe = await probeMcpHttp({ url: `http://127.0.0.1:${deadPort}/mcp`, headers, timeoutMs: 3000 })
  assert.equal(probe.ok, false)
  assert.ok(probe.error.length > 0)
})

await record('probe: catalog call is normalized', async () => {
  const probe = await probeMcpHttp({
    url: `http://127.0.0.1:${json.port}/mcp`,
    headers,
    timeoutMs: 3000,
    catalogTool: 'mcp_catalog',
  })
  assert.equal(probe.ok, true, probe.error)
  assert.equal(probe.catalog.length, 3)
  assert.deepEqual(probe.catalog[0], { server: 'applygate', url: 'http://applygate:9102/mcp', toolCount: 2, reachable: true, error: undefined })
  assert.equal(probe.catalog[2].reachable, false)
  assert.match(probe.catalog[2].error, /ECONNREFUSED/)
})

await record('probe: failing catalog tool is reported, not thrown', async () => {
  const probe = await probeMcpHttp({
    url: `http://127.0.0.1:${catalogError.port}/mcp`,
    headers,
    timeoutMs: 3000,
    catalogTool: 'mcp_catalog',
  })
  assert.equal(probe.ok, true)
  assert.equal(probe.catalog, undefined)
  assert.match(probe.catalogError, /catalog exploded/)
})

await record('probe: FastMCP { result: <json string> } wrapper is unwrapped', async () => {
  const probe = await probeMcpHttp({
    url: `http://127.0.0.1:${fastmcp.port}/mcp`,
    headers,
    timeoutMs: 3000,
    catalogTool: 'mcp_catalog',
  })
  assert.equal(probe.ok, true, probe.error)
  assert.equal(probe.catalogError, undefined)
  assert.equal(probe.catalog.length, 3)
  assert.equal(probe.catalog[0].server, 'applygate')
})

await record('probe: structuredContent-only responses are unwrapped', async () => {
  for (const port of [structuredOnly.port, structuredObject.port]) {
    const probe = await probeMcpHttp({ url: `http://127.0.0.1:${port}/mcp`, headers, timeoutMs: 3000, catalogTool: 'mcp_catalog' })
    assert.equal(probe.ok, true, probe.error)
    assert.equal(probe.catalog.length, 3, `port ${port}`)
  }
})

await record('payload: every tool-result shape', async () => {
  const body = { servers: [{ server: 'x' }] }
  const json = JSON.stringify(body)
  assert.deepEqual(readToolPayload({ content: [{ type: 'text', text: json }] }), body)
  assert.deepEqual(readToolPayload({ content: [], structuredContent: { result: json } }), body)
  assert.deepEqual(readToolPayload({ content: [], structuredContent: body }), body)
  assert.deepEqual(readToolPayload({ content: [{ type: 'text', text: 'not json' }], structuredContent: { result: json } }), body)
  assert.equal(readToolPayload({ content: [{ type: 'text', text: 'not json' }] }), undefined)
  assert.equal(readToolPayload({ content: [] }), undefined)
  assert.deepEqual(normalizeCatalog({ result: { servers: [{ server: 'y' }] } })[0].server, 'y')
})

await record('catalog: bare array and structuredContent shapes', async () => {
  assert.equal(normalizeCatalog({ servers: [{ server: 'a', tools: [] }] })[0].reachable, true)
  assert.equal(normalizeCatalog([{ name: 'b' }])[0].server, 'b')
  assert.equal(normalizeCatalog({ nope: true }), undefined)
})

/* ------------------------------------------------------------------ *
 * Snapshot layer against a fake Loader
 * ------------------------------------------------------------------ */

const fakeEntry = (id, config, options = {}) => ({
  id,
  options: { id, name: '@deepseek-ai/dsh-mcp-client', config },
  // Cordis 0.2.2+: `config` is the raw row input, `effectiveConfig` the
  // validated object the plugin runs with. A healthy row has both.
  fiber: options.fiber ?? { config, effectiveConfig: config, state: 2 },
  disabled: options.disabled ?? false,
})

const liveConfig = { transport: 'streamable-http', serverName: 'mock', url: `http://127.0.0.1:${json.port}/mcp`, headers }
const deadConfig = { transport: 'streamable-http', serverName: 'dead', url: `http://127.0.0.1:${deadPort}/mcp`, headers }
const stdioConfig = { transport: 'stdio', serverName: 'local', command: '/usr/local/bin/mcp-local', args: ['--flag'] }

const fakeEntries = [
  fakeEntry('mcp-mock', liveConfig),
  fakeEntry('mcp-dead', deadConfig),
  fakeEntry('mcp-local', stdioConfig),
  fakeEntry('mcp-off', { transport: 'streamable-http', serverName: 'off', url: 'http://127.0.0.1:1/mcp' }, { disabled: true, fiber: undefined }),
  { id: 'other', options: { id: 'other', name: '@deepseek-ai/dsh-other', config: {} }, disabled: false },
]

const fakeCtx = {
  loader: { entries: () => fakeEntries[Symbol.iterator]() },
  tools: { schemas: () => [{ name: 'mcp__local__ping' }, { name: 'mcp__local__run' }, { name: 'bash' }] },
}

const settings = resolveSettings({})
const caches = () => ({ connections: { at: 0, value: null, pending: null }, upstream: new Map() })

await record('snapshot: classifies every row', async () => {
  const snapshot = await buildSnapshot(fakeCtx, settings, caches(), true)
  const byId = Object.fromEntries(snapshot.connections.map((item) => [item.id, item]))
  assert.deepEqual(Object.keys(byId).sort(), ['mcp-dead', 'mcp-local', 'mcp-mock', 'mcp-off'])
  assert.equal(byId['mcp-mock'].state, 'connected')
  assert.equal(byId['mcp-mock'].toolCount, 2)
  assert.equal(byId['mcp-dead'].state, 'disconnected')
  assert.equal(byId['mcp-local'].state, 'configured')
  assert.equal(byId['mcp-local'].toolCount, 2)
  assert.equal(byId['mcp-local'].command, 'mcp-local')
  assert.equal(byId['mcp-off'].state, 'disabled')
  assert.equal(snapshot.summary.total, 4)
  assert.equal(snapshot.summary.probed, 2)
  assert.equal(snapshot.summary.connected, 1)
})

await record('services: the tools registry is injected', () => {
  // Without 'tools' in inject, `ctx.tools` throws ("cannot get property
  // \"tools\" without inject"), every tool lookup silently returns nothing, and
  // every server is reported as unregistered — which is exactly how the
  // page-managed servers came back as "configured" instead of "connected".
  assert.ok(inject.includes('connection'), 'connection is required for the route')
  assert.ok(inject.includes('tools'), 'tools is required to read registered tool names')
})

await record('snapshot: servers added through the MCP servers page are counted', async () => {
  // The page mounts its servers as children of its own row, so they are not
  // Loader entries. Only the row's `servers` list reveals them.
  const managed = [
    { id: 'litellm', serverName: 'aruba', transport: 'streamable-http', url: 'http://127.0.0.1:1/mcp', enabled: true },
    { id: 'off', serverName: 'offpage', transport: 'streamable-http', url: 'http://127.0.0.1:1/mcp', enabled: false },
  ]
  // The page declares `servers` volatile, so a resolved config holds a
  // reference object — NOT the array. Reading it with Array.isArray() silently
  // returned zero rows and the panel under-counted every page-managed server.
  const volatileRef = Object.freeze({
    get: () => managed,
    [Symbol.for('cosmokit.volatile.write')]: () => {},
  })
  const hostEntry = {
    id: 'mcp-servers',
    options: { id: 'mcp-servers', name: '@local/dsh-mcp-servers', config: { servers: volatileRef } },
    fiber: { effectiveConfig: { servers: volatileRef }, state: 2 },
    disabled: false,
  }
  const ctx = {
    loader: { entries: () => [hostEntry][Symbol.iterator]() },
    tools: { schemas: () => [{ name: 'mcp__aruba__health' }, { name: 'mcp__aruba__skills_list' }, { name: 'bash' }] },
  }
  const snapshot = await buildSnapshot(ctx, settings, caches(), false)
  const byName = Object.fromEntries(snapshot.connections.map((item) => [item.serverName, item]))
  assert.deepEqual(Object.keys(byName).sort(), ['aruba', 'offpage'])
  assert.equal(byName.aruba.state, 'connected')
  assert.equal(byName.aruba.toolCount, 2)
  assert.equal(byName.aruba.managedBy, 'mcp-servers')
  assert.deepEqual(byName.aruba.tools, ['health', 'skills_list'])
  assert.equal(byName.offpage.state, 'disabled')
  assert.equal(snapshot.summary.total, 2)
  assert.equal(snapshot.summary.connected, 1)

  // A plain array (an unresolved or hand-written config) must work too.
  const plainHost = {
    id: 'mcp-servers',
    options: { id: 'mcp-servers', name: '@local/dsh-mcp-servers', config: { servers: managed } },
    fiber: { effectiveConfig: { servers: managed }, state: 2 },
    disabled: false,
  }
  const plainCtx = {
    loader: { entries: () => [plainHost][Symbol.iterator]() },
    tools: { schemas: () => [{ name: 'mcp__aruba__health' }, { name: 'mcp__aruba__skills_list' }] },
  }
  const plain = await buildSnapshot(plainCtx, settings, caches(), false)
  assert.equal(plain.summary.total, 2)
  assert.equal(plain.connections.find((item) => item.serverName === 'aruba').toolCount, 2)

  // The host row was renamed once already (dsh-mcp-servers -> the composed
  // dsh-mcp bundle). Discovery must not depend on the package name, or every
  // page-managed server disappears from the panel.
  const renamed = {
    id: 'mcp',
    options: { id: 'mcp', name: '@local/dsh-mcp', config: { servers: managed } },
    fiber: { effectiveConfig: { servers: volatileRef }, state: 2 },
    disabled: false,
  }
  const renamedCtx = {
    loader: { entries: () => [renamed][Symbol.iterator]() },
    tools: { schemas: () => [{ name: 'mcp__aruba__health' }, { name: 'mcp__aruba__skills_list' }] },
  }
  const afterRename = await buildSnapshot(renamedCtx, settings, caches(), false)
  assert.equal(afterRename.summary.total, 2, 'a renamed host row is still discovered')
  assert.equal(afterRename.connections.find((item) => item.serverName === 'aruba').state, 'connected')
})

await record('snapshot: upstream servers appear for the gateway', async () => {
  const snapshot = await buildSnapshot(fakeCtx, settings, caches(), true)
  const gateway = snapshot.connections.find((item) => item.id === 'mcp-mock')
  assert.equal(gateway.upstream.servers.length, 3)
  assert.equal(gateway.upstream.servers[2].reachable, false)
  assert.equal(snapshot.summary.upstream, 3)
  assert.equal(snapshot.summary.upstreamReachable, 2)
  assert.equal(snapshot.summary.hasUpstream, true)
})

await record('snapshot: a federator with a differently named catalog tool unrolls too', async () => {
  const entry = fakeEntry('mcp-litellm', {
    transport: 'streamable-http',
    serverName: 'litellm',
    url: 'http://127.0.0.1:' + litellm.port + '/mcp',
    headers,
  })
  const ctx = {
    loader: { entries: () => [entry][Symbol.iterator]() },
    tools: { schemas: () => [] },
  }
  const snapshot = await buildSnapshot(ctx, settings, caches(), true)
  const connection = snapshot.connections.find((item) => item.serverName === 'litellm')
  assert.equal(connection.state, 'connected')
  assert.equal(connection.upstream.tool, 'list_mcp_servers', 'the tool that answered is reported')
  assert.deepEqual(connection.upstream.servers.map((server) => server.server), ['github', 'slack'])
  assert.equal(connection.upstream.servers[0].toolCount, 7, 'tool_count is read')
  assert.equal(connection.upstream.servers[0].url, 'http://github:8080/mcp', 'server_url is read')
  assert.equal(connection.upstream.servers[1].reachable, false, 'a failure status marks it unreachable')
})

await record('snapshot: upstream is omitted unless requested', async () => {
  const snapshot = await buildSnapshot(fakeCtx, settings, caches(), false)
  assert.ok(snapshot.connections.every((item) => item.upstream === undefined))
  assert.equal(snapshot.summary.hasUpstream, false)
})

await record('wire: request headers never reach the browser', async () => {
  const snapshot = await buildSnapshot(fakeCtx, settings, caches(), true)
  const serialized = JSON.stringify(snapshot)
  assert.ok(!serialized.includes('super-secret-token'), 'token leaked into the snapshot')
  assert.ok(!serialized.includes('"headers"'), 'headers leaked into the snapshot')
  for (const connection of snapshot.connections) {
    assert.equal(connection.target, undefined)
    assert.equal(connection.catalogAvailable, undefined)
  }
})

await record('wire: toWire whitelists fields', async () => {
  const wire = toWire({ id: 'x', serverName: 'x', transport: 'streamable-http', state: 'connected', pluginState: 'active', disabled: false, target: { headers }, catalogAvailable: true })
  assert.deepEqual(Object.keys(wire).sort(), ['disabled', 'id', 'pluginState', 'serverName', 'state', 'transport'])
})

await record('settings: bounds and defaults', async () => {
  const resolved = resolveSettings({ probeTimeoutMs: 1, connectionTtlMs: -5, routePath: 'status', catalogTool: '  x  ' })
  assert.equal(resolved.probeTimeoutMs, 200)
  assert.equal(resolved.connectionTtlMs, 0)
  assert.equal(resolved.routePath, '/status')
  assert.equal(resolved.catalogTool, 'x')
  assert.equal(resolveSettings(undefined).routePath, '/api/mcp-status')
})

await record('summarize: empty inventory', async () => {
  assert.deepEqual(summarize([]), { total: 0, probed: 0, connected: 0, hasUpstream: false, upstream: 0, upstreamReachable: 0 })
})

await record('describe: stdio without a bridge falls back to unknown', async () => {
  const view = await describeConnection({ tools: { schemas: () => [] } }, fakeEntry('mcp-local', stdioConfig), settings)
  assert.equal(view.state, 'unknown')
  assert.equal(view.toolCount, 0)
})

await record('describe: failed fiber is reported as pluginState', async () => {
  const view = await describeConnection(fakeCtx, fakeEntry('mcp-dead', deadConfig, { fiber: { config: deadConfig, state: 3 } }), settings)
  assert.equal(view.pluginState, 'failed')
  assert.equal(view.state, 'disconnected')
})

await record('describe: expression-derived headers come from effectiveConfig, not raw config', async () => {
  // Regression: a `!!js` Authorization stays an inert marker in the raw row
  // input. Reading it made the probe send no auth and report HTTP 401.
  const rawConfig = {
    ...liveConfig,
    headers: { Authorization: { __jsExpr: "'Bearer ' + process.env.MCP_TOKEN" } },
  }
  const entry = {
    id: 'mcp-expr',
    options: { id: 'mcp-expr', name: '@deepseek-ai/dsh-mcp-client', config: rawConfig },
    fiber: { config: rawConfig, effectiveConfig: liveConfig, state: 2 },
    disabled: false,
  }
  const view = await describeConnection(fakeCtx, entry, settings)
  assert.equal(view.state, 'connected')
  assert.equal(view.target.headers.Authorization, SECRET)
})

await record('describe: resolved config falls back to raw input for a failed row', async () => {
  const rawConfig = { ...deadConfig, headers: { Authorization: { __jsExpr: "'Bearer ' + process.env.MCP_TOKEN" } } }
  const entry = {
    id: 'mcp-expr-dead',
    options: { id: 'mcp-expr-dead', name: '@deepseek-ai/dsh-mcp-client', config: rawConfig },
    fiber: { config: rawConfig, effectiveConfig: undefined, state: 3 },
    disabled: false,
  }
  const view = await describeConnection(fakeCtx, entry, settings)
  assert.equal(view.pluginState, 'failed')
  assert.equal(view.state, 'disconnected')
})

await record('route: handler answers JSON and never throws', async () => {
  const { apply } = await import('../index.js')
  let registered
  const ctx = {
    ...fakeCtx,
    connection: { fetch: { register: (route) => { registered = route; return () => Promise.resolve() } } },
    effect: (factory) => factory(),
    logger: { debug: () => {} },
  }
  apply(ctx, {})
  assert.equal(registered.path, '/api/mcp-status')
  assert.deepEqual(registered.methods, ['GET'])
  const response = await registered.fetch(new Request('http://localhost/api/mcp-status'))
  assert.equal(response.status, 200)
  assert.match(response.headers.get('content-type'), /application\/json/)
  const body = await response.json()
  assert.equal(body.connections.length, 4)
  const withUpstream = await registered.fetch(new Request('http://localhost/api/mcp-status?upstream=1'))
  const expanded = await withUpstream.json()
  assert.equal(expanded.connections.find((item) => item.id === 'mcp-mock').upstream.servers.length, 3)
})

for (const line of results) console.log(line)
const failures = results.filter((line) => line.startsWith('FAIL')).length
console.log(`\n${results.length - failures}/${results.length} checks passed`)
json.server.close()
litellm.server.close()
sse.server.close()
unauthorized.server.close()
catalogError.server.close()
fastmcp.server.close()
structuredOnly.server.close()
structuredObject.server.close()
