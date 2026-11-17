/**
 * Offline check of the Host half.
 *
 * Runs the shipped `index.js` against a stub Cordis context: no Harness, no
 * network, no child processes. It exercises what the plugin actually has to get
 * right — mounting one bridge child per configured server, reconciling on
 * `loader/volatile-update` without bouncing unchanged servers, reporting a
 * duplicate or unusable row instead of throwing, surviving an unavailable
 * bridge, and serving the snapshot the page reads.
 */
import assert from 'node:assert/strict'

import { Config } from '../schema.js'
import { apply } from '../index.js'

const WRITE = Symbol.for('cosmokit.volatile.write')

const results = []
/**
 * Run one check.
 * @param {string} name - check name.
 * @param {() => Promise<void> | void} fn - assertions.
 * @returns {Promise<void>} settlement.
 */
async function check(name, fn) {
  try {
    await fn()
    results.push(`ok   ${name}`)
  } catch (error) {
    results.push(`FAIL ${name}: ${error.message}`)
    process.exitCode = 1
  }
}

/**
 * Let the plugin's asynchronous reconcile settle. The first bridge resolution
 * is a real module-resolution attempt, which costs a few milliseconds.
 * @returns {Promise<void>} settlement.
 */
const tick = () => new Promise((resolve) => setTimeout(resolve, 60))

/** A bridge stand-in: enough for `ctx.plugin()` to mount and for the route. */
const fakeBridge = {
  name: 'mcp-client',
  inject: ['tools'],
  Config: { '~standard': { version: 1, vendor: 'schemastery', validate: (value) => ({ value }) } },
  apply() {},
}

/**
 * Build a stub context that records what the plugin does.
 * @param {{ servers?: any[], bridge?: any, bridgeImport?: () => Promise<any>, external?: any[], tools?: any[] }} options - fixture.
 * @returns {any} stub context plus inspection handles.
 */
function createContext(options = {}) {
  const servers = options.servers ?? []
  const config = Config['~standard'].validate({ servers }).value
  const state = {
    config,
    mounts: [],
    disposed: [],
    reserved: new Set(),
    routes: [],
    handlers: new Map(),
    settingsCalls: [],
    entries: options.external ?? [],
    tools: options.tools ?? [],
  }
  const ctx = {
    logger: { warn() {}, debug() {}, error() {} },
    fiber: { state: 2 },
    get() {
      return undefined
    },
    on(name, handler) {
      state.handlers.set(name, handler)
      return () => {}
    },
    effect(fn) {
      const disposer = fn()
      return typeof disposer === 'function' ? disposer : () => {}
    },
    inject(_deps, callback) {
      const child = {
        effect: ctx.effect,
        settings: {
          configure(options, fiber) {
            state.settingsCalls.push({ options, fiber })
          },
        },
        connection: {
          fetch: {
            register(route) {
              state.routes.push(route)
              return () => {}
            },
          },
        },
      }
      callback(child)
      return { then() {} }
    },
    loader: {
      import: options.bridgeImport ?? (async () => options.bridge ?? fakeBridge),
      entries: () => state.entries,
    },
    tools: {
      schemas: () => state.tools,
    },
    plugin(plugin, pluginConfig) {
      const name = pluginConfig.serverName
      // The real bridge reserves `serverName` for the life of the child fiber
      // and releases it during disposal, so a premature remount throws.
      if (state.reserved.has(name)) {
        throw new Error(`mcp-client: serverName "${name}" is already in use by another mcp-client instance`)
      }
      state.reserved.add(name)
      if (name === 'boom') {
        const failing = {
          state: 3,
          dispose: async () => {
            state.disposed.push(name)
            state.reserved.delete(name)
          },
        }
        failing.then = (resolve, reject) => Promise.resolve().then(() => reject(new Error('connection refused')))
        state.mounts.push({ name, config: pluginConfig, plugin })
        return failing
      }
      const fiber = {
        state: 2,
        dispose: async () => {
          state.disposed.push(name)
          state.reserved.delete(name)
        },
      }
      // Resolve with a plain value: a thenable resolving to itself would spin.
      fiber.then = (resolve) => Promise.resolve().then(() => resolve(undefined))
      state.mounts.push({ name, config: pluginConfig, plugin })
      return fiber
    },
  }
  /**
   * Replace the live server list the way the framework's volatile commit does.
   * @param {any[]} rows - next list.
   * @returns {void}
   */
  const setServers = (rows) => {
    state.config.servers[WRITE](rows)
    const handler = state.handlers.get('loader/volatile-update')
    if (handler) handler([['servers']])
  }
  /**
   * Read the snapshot through the registered route.
   * @param {string} [query] - query string.
   * @returns {Promise<any>} parsed body.
   */
  const snapshot = async (query = '') => {
    const route = state.routes[0]
    assert.ok(route, 'the status route was not registered')
    const response = await route.fetch(new Request(`http://127.0.0.1${route.path}${query}`))
    return response.json()
  }
  /**
   * Start the plugin the way the Loader does: with its validated config.
   * @returns {void}
   */
  const start = () => apply(ctx, state.config)
  return { ctx, state, config, start, setServers, snapshot }
}

/* ------------------------------------------------------------------ *
 * Mounting and reconciliation
 * ------------------------------------------------------------------ */

await check('apply() registers the settings opt-out and the status route', async () => {
  const { ctx, state, start } = createContext()
  start()
  assert.equal(state.settingsCalls.length, 1, 'the generated settings page must be disabled')
  assert.deepEqual(state.settingsCalls[0].options, { auto: false })
  assert.equal(state.routes.length, 2, 'status + test routes')
  assert.equal(state.routes[0].path, '/api/mcp-servers')
  assert.deepEqual(state.routes[0].methods, ['GET'])
  assert.equal(state.routes[1].path, '/api/mcp-servers/test')
  assert.deepEqual(state.routes[1].methods, ['POST'])
})

await check('apply() mounts one bridge child per enabled server', async () => {
  const { ctx, state, start } = createContext({
    servers: [
      { id: 'one', serverName: 'one', url: 'https://one.example.com/mcp' },
      { id: 'two', serverName: 'two', url: 'https://two.example.com/mcp' },
      { id: 'off', serverName: 'off', url: 'https://off.example.com/mcp', enabled: false },
    ],
  })
  start()
  await tick()
  assert.deepEqual(state.mounts.map((mount) => mount.name), ['one', 'two'])
  assert.equal(state.mounts[0].plugin, fakeBridge, 'the resolved bridge module is what gets mounted')
  assert.deepEqual(state.mounts[0].config, {
    transport: 'streamable-http',
    serverName: 'one',
    url: 'https://one.example.com/mcp',
    toolCallTimeoutMs: 60000,
    failOnStartupError: false,
    maxInstructionBytes: 262144,
  })
})

await check('apply() does not throw when nothing is configured', async () => {
  const { state, start } = createContext()
  assert.doesNotThrow(() => start())
  await tick()
  assert.equal(state.mounts.length, 0)
})

await check('a volatile update mounts additions and unmounts removals only', async () => {
  const { ctx, state, start, setServers } = createContext({
    servers: [
      { id: 'one', serverName: 'one', url: 'https://one.example.com/mcp' },
      { id: 'two', serverName: 'two', url: 'https://two.example.com/mcp' },
    ],
  })
  start()
  await tick()
  assert.deepEqual(state.mounts.map((mount) => mount.name), ['one', 'two'])

  setServers([
    { id: 'one', serverName: 'one', url: 'https://one.example.com/mcp' },
    { id: 'three', serverName: 'three', url: 'https://three.example.com/mcp' },
  ])
  await tick()
  assert.deepEqual(state.disposed, ['two'], 'only the removed server is unmounted')
  assert.deepEqual(state.mounts.map((mount) => mount.name), ['one', 'two', 'three'], 'the unchanged server is not remounted')
})

await check('an edit that changes the endpoint remounts that server after disposal settles', async () => {
  const { ctx, state, start, setServers, snapshot } = createContext({
    servers: [{ id: 'one', serverName: 'one', url: 'https://one.example.com/mcp' }],
  })
  start()
  await tick()
  setServers([{ id: 'one', serverName: 'one', url: 'https://changed.example.com/mcp' }])
  await tick()
  assert.deepEqual(state.disposed, ['one'], 'the outgoing child is disposed first')
  assert.equal(state.mounts.length, 2, 'the replacement mounts once the namespace is free')
  assert.equal(state.mounts[1].config.url, 'https://changed.example.com/mcp')
  const body = await snapshot()
  assert.equal(body.servers[0].error, undefined, 'the replacement did not collide with the outgoing child')
})

await check('disabling a server unmounts it without losing the row', async () => {
  const { ctx, state, start, setServers, snapshot } = createContext({
    servers: [{ id: 'one', serverName: 'one', url: 'https://one.example.com/mcp' }],
  })
  start()
  await tick()
  setServers([{ id: 'one', serverName: 'one', url: 'https://one.example.com/mcp', enabled: false }])
  await tick()
  assert.deepEqual(state.disposed, ['one'])
  const body = await snapshot()
  assert.equal(body.servers.length, 0, 'a disabled server is not reported as mountable')
})

/* ------------------------------------------------------------------ *
 * Failure isolation
 * ------------------------------------------------------------------ */

await check('a duplicate serverName is reported, not thrown', async () => {
  const { ctx, state, start, snapshot } = createContext({
    servers: [
      { id: 'one', serverName: 'one', url: 'https://one.example.com/mcp' },
      { id: 'two', serverName: 'one', url: 'https://two.example.com/mcp' },
    ],
  })
  start()
  await tick()
  assert.equal(state.mounts.length, 1, 'only one of the duplicates is mounted')
  const body = await snapshot()
  assert.equal(body.problems.length, 1)
  assert.match(body.problems[0].message, /already uses this serverName/)
})

await check('a malformed row is reported, not mounted, and does not stop the rest', async () => {
  const { ctx, state, start, snapshot } = createContext({
    servers: [
      { id: 'bad', serverName: 'not valid', url: 'https://x.example.com/mcp' },
      { id: 'good', serverName: 'good', url: 'https://good.example.com/mcp' },
    ],
  })
  start()
  await tick()
  assert.deepEqual(state.mounts.map((mount) => mount.name), ['good'])
  const body = await snapshot()
  assert.deepEqual(body.problems.map((problem) => problem.id), ['bad'])
})

await check('a child that fails to start is reported and does not break apply()', async () => {
  const { ctx, start, snapshot } = createContext({
    servers: [{ id: 'boom', serverName: 'boom', url: 'https://boom.example.com/mcp' }],
  })
  assert.doesNotThrow(() => start())
  await tick()
  const body = await snapshot()
  assert.equal(body.servers[0].state, 'failed')
  assert.match(body.servers[0].error, /connection refused/)
})

await check('an unavailable bridge is reported instead of thrown', async () => {
  const { ctx, start, snapshot } = createContext({
    servers: [{ id: 'one', serverName: 'one', url: 'https://one.example.com/mcp' }],
    bridgeImport: async () => {
      throw new Error('MODULE_NOT_FOUND')
    },
  })
  assert.doesNotThrow(() => start())
  await tick()
  const body = await snapshot()
  assert.equal(body.bridge.available, false)
  assert.match(body.bridge.error, /MODULE_NOT_FOUND/)
  assert.equal(body.servers.length, 1, 'the configured server is still reported')
})

await check('the bridge is adopted from an already-loaded MCP row when resolution fails', async () => {
  const runtime = { name: 'mcp-client', callback() {}, Config: fakeBridge.Config }
  const { ctx, state, start } = createContext({
    servers: [{ id: 'one', serverName: 'one', url: 'https://one.example.com/mcp' }],
    bridgeImport: async () => {
      throw new Error('MODULE_NOT_FOUND')
    },
    external: [{ options: { id: 'mcp-legacy', name: '@deepseek-ai/dsh-mcp-client' }, fiber: { state: 2, runtime } }],
  })
  start()
  await tick()
  assert.equal(state.mounts.length, 1)
  assert.equal(state.mounts[0].plugin.apply, runtime.callback)
  assert.equal(state.mounts[0].plugin.Config, runtime.Config)
})

/* ------------------------------------------------------------------ *
 * Snapshot
 * ------------------------------------------------------------------ */

await check('the snapshot reports fiber state and live tool counts', async () => {
  const { ctx, start, snapshot } = createContext({
    servers: [
      { id: 'one', serverName: 'one', url: 'https://one.example.com/mcp' },
      { id: 'two', serverName: 'two', transport: 'stdio', command: 'npx', args: ['-y', 'server'] },
    ],
    tools: [
      { name: 'mcp__one__search' },
      { name: 'mcp__one__fetch' },
      { name: 'mcp__two__run' },
      { name: 'bash' },
    ],
  })
  start()
  await tick()
  const body = await snapshot()
  assert.equal(body.writable, true)
  assert.equal(body.bridge.available, true)
  assert.deepEqual(body.servers.map((server) => server.serverName), ['one', 'two'])
  assert.equal(body.servers[0].state, 'ready')
  assert.equal(body.servers[0].toolCount, 2)
  assert.equal(body.servers[1].toolCount, 1)
  assert.equal(body.servers[1].endpoint, 'npx')
  assert.equal(body.servers[1].transport, 'stdio')
})

await check('the test route probes a draft without mounting it', async () => {
  const { state, start } = createContext({ servers: [] })
  start()
  await tick()
  const route = state.routes.find((entry) => entry.path === '/api/mcp-servers/test')
  assert.ok(route, 'the test route was not registered')

  const bad = await route.fetch({
    json: async () => ({ transport: 'streamable-http', url: 'ftp://example.com' }),
  })
  const badBody = await bad.json()
  assert.equal(badBody.ok, false)
  assert.match(badBody.error, /http/i)

  const stdio = await route.fetch({
    json: async () => ({ transport: 'stdio', command: 'definitely-not-a-real-binary-xyz' }),
  })
  const stdioBody = await stdio.json()
  assert.equal(stdioBody.stdio, true)
  assert.equal(stdioBody.commandFound, false)
  assert.equal(state.mounts.length, 0, 'probing must never mount a child')
})

await check('external MCP rows are reported without their request headers', async () => {
  const { ctx, start, snapshot } = createContext({
    external: [
      {
        options: { id: 'mcp-legacy', name: '@deepseek-ai/dsh-mcp-client' },
        fiber: {
          state: 2,
          effectiveConfig: {
            transport: 'streamable-http',
            serverName: 'legacy',
            url: 'https://user:secret@legacy.example.com/mcp?token=abc',
            headers: { Authorization: 'Bearer secret' },
          },
        },
      },
      { options: { id: 'not-mcp' }, fiber: { state: 2, effectiveConfig: { something: true } } },
    ],
  })
  start()
  await tick()
  const body = await snapshot()
  assert.equal(body.external.length, 1)
  const [row] = body.external
  assert.equal(row.serverName, 'legacy')
  assert.equal(row.endpoint, 'https://legacy.example.com/mcp')
  assert.equal(JSON.stringify(body).includes('secret'), false, 'credentials must not reach the browser')
})

await check('retry leaves a healthy server alone', async () => {
  const { ctx, state, start, snapshot } = createContext({
    servers: [{ id: 'one', serverName: 'one', url: 'https://one.example.com/mcp' }],
  })
  start()
  await tick()
  const before = state.mounts.length
  const body = await snapshot('?retry=1')
  assert.equal(body.bridge.available, true)
  assert.equal(state.mounts.length, before, 'a healthy server is not remounted by retry')
  assert.deepEqual(state.disposed, [])
})

await check('retry disposes a failed child and mounts it again', async () => {
  const { ctx, state, start, snapshot } = createContext({
    servers: [{ id: 'boom', serverName: 'boom', url: 'https://boom.example.com/mcp' }],
  })
  start()
  await tick()
  assert.equal(state.mounts.length, 1)
  const body = await snapshot('?retry=1')
  assert.deepEqual(state.disposed, ['boom'], 'the failed child is disposed before the retry')
  assert.equal(state.mounts.length, 2, 'the retry mounts it again')
  assert.equal(body.servers[0].state, 'failed', 'and reports that it failed again')
})

/* ------------------------------------------------------------------ *
 * Report
 * ------------------------------------------------------------------ */

let failed = 0
for (const line of results) {
  if (line.startsWith('FAIL')) failed += 1
  console.log(`  ${line}`)
}
console.log(failed === 0 ? `\nall ${results.length} checks passed` : `\n${failed} of ${results.length} FAILED`)
process.exit(failed === 0 ? 0 : 1)
