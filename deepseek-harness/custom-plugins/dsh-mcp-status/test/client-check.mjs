/**
 * Development check for the MCP status plugin's browser half.
 *
 * Runs the shipped `client.js` against a minimal React shim and a prescribed
 * hook state, so the render path (collapsed chip and expanded panel) is
 * exercised without a browser. Not part of the installed bundle.
 */
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { createContext, runInContext } from 'node:vm'

const results = []
/** Cross-realm safe deep comparison (VM arrays/objects carry another realm's prototypes). */
function plain(value) {
  return JSON.parse(JSON.stringify(value))
}
async function check(name, fn) {
  try {
    await fn()
    results.push(`ok   ${name}`)
  } catch (error) {
    results.push(`FAIL ${name}: ${error.message}`)
    process.exitCode = 1
  }
}

/** Minimal element tree. */
const Fragment = Symbol('Fragment')

/** Prescribed hook state, consumed in call order. */
let stateQueue = []
let effectCount = 0

const ReactShim = {
  Fragment,
  createElement(type, props, ...children) {
    return { type, props: { ...(props ?? {}), children } }
  },
  useState(initial) {
    const next = stateQueue.length ? stateQueue.shift() : initial
    return [next, () => {}]
  },
  useEffect() {
    effectCount += 1
  },
  useCallback(fn) {
    return fn
  },
  useMemo(fn) {
    return fn()
  },
  useRef(value) {
    return { current: value }
  },
}

/** Load `client.js` in a VM with the module loader and React shim in place. */
function loadClient() {
  let registration = null
  const sandbox = {
    window: {
      __ModuleLoader__: {
        load(definition) {
          registration = definition
        },
      },
    },
    require: (specifier) => {
      if (specifier === 'react') return ReactShim
      throw new Error(`unexpected module: ${specifier}`)
    },
    console,
    setTimeout,
    clearTimeout,
    fetch: () => Promise.reject(new Error('fetch must not run in this check')),
  }
  sandbox.globalThis = sandbox
  createContext(sandbox)
  runInContext(readFileSync(new URL('../client.js', import.meta.url), 'utf8'), sandbox, { filename: 'client.js' })
  assert.ok(registration, 'client module did not register a factory')
  return registration
}

/** Flatten an element tree into text and collect class names. */
function walk(node, texts = [], classes = []) {
  if (node === null || node === undefined || node === false) return { texts, classes }
  if (Array.isArray(node)) {
    for (const child of node) walk(child, texts, classes)
    return { texts, classes }
  }
  if (typeof node === 'string' || typeof node === 'number') {
    texts.push(String(node))
    return { texts, classes }
  }
  if (typeof node === 'function') return { texts, classes }
  if (typeof node.type === 'function') return walk(node.type(node.props ?? {}), texts, classes)
  const props = node.props ?? {}
  if (typeof props.className === 'string') classes.push(props.className)
  for (const child of props.children ?? []) walk(child, texts, classes)
  return { texts, classes }
}

const SNAPSHOT = {
  generatedAt: '2026-10-30T21:00:00.000Z',
  summary: { total: 2, probed: 2, connected: 1, hasUpstream: true, upstream: 10, upstreamReachable: 9 },
  connections: [
    {
      id: 'mcp-pcai-llm-gateway',
      serverName: 'pcai-llm-gateway',
      transport: 'streamable-http',
      state: 'connected',
      pluginState: 'active',
      disabled: false,
      endpoint: 'http://pcai-llm-gateway-service.pcai-llm.svc.cluster.local:9200/mcp',
      latencyMs: 41,
      protocolVersion: '2026-07-28',
      serverInfo: { name: 'pcai-gateway', version: '1.4.0' },
      toolCount: 2,
      tools: ['mcp_catalog', 'mcp_call'],
      upstream: {
        generatedAt: '2026-10-30T21:00:00.000Z',
        tool: 'mcp_catalog',
        servers: [
          { server: 'applygate', url: 'http://applygate:9102/mcp', toolCount: 6, reachable: true },
          { server: 'k8s-ops', url: 'http://k8s:9090/mcp', toolCount: 20, reachable: true },
          { server: 'logsearch', url: 'http://logsearch:9101/mcp', toolCount: 5, reachable: false, error: 'connect ECONNREFUSED' },
        ],
      },
    },
    {
      id: 'mcp-dead',
      serverName: 'retired-server',
      transport: 'streamable-http',
      state: 'disconnected',
      pluginState: 'active',
      disabled: false,
      endpoint: 'http://retired:9000/mcp',
      latencyMs: 12,
      error: 'HTTP 401: unauthorized',
    },
  ],
}

const registration = loadClient()

await check('module: registers the expected package id', async () => {
  assert.equal(registration.id, '@local/dsh-mcp-status')
  assert.equal(typeof registration.factory, 'function')
})

const plugin = registration.factory((specifier) => {
  if (specifier === 'react') return ReactShim
  throw new Error(`unexpected module: ${specifier}`)
})

await check('plugin: declares slots and locale', async () => {
  assert.deepEqual(plain(plugin.inject), ['slots', 'locale'])
  assert.equal(typeof plugin.apply, 'function')
})

await check('plugin: registers the dock entry and dictionaries', async () => {
  let localeRegistration = null
  let slotRegistration = null
  let injectionKey = null
  const ctx = {
    effect: (factory) => factory(),
    locale: {
      register: (ns, dictionaries) => {
        localeRegistration = { ns, dictionaries }
        return () => {}
      },
    },
    slots: {
      inject: (key, callback) => {
        injectionKey = key
        callback()
        return () => {}
      },
      register: (options, component) => {
        slotRegistration = { options, component }
        return () => {}
      },
    },
  }
  plugin.apply(ctx)
  assert.equal(injectionKey, 'conversation.composer.dock')
  assert.deepEqual(plain(slotRegistration.options), {
    name: 'conversation.composer.dock',
    id: 'mcp-status',
    order: 2,
    locale: 'mcp-status',
  })
  assert.equal(typeof slotRegistration.component, 'function')
  assert.equal(localeRegistration.ns, 'mcp-status')
  assert.deepEqual(plain(Object.keys(localeRegistration.dictionaries).sort()), ['en', 'zh'])
  assert.equal(Object.keys(localeRegistration.dictionaries.en).length, Object.keys(localeRegistration.dictionaries.zh).length)
})

const Dock = slotRegistrationComponent()

/** The registered component, reused by the render checks below. */
function slotRegistrationComponent() {
  let component = null
  plugin.apply({
    effect: (factory) => factory(),
    locale: { register: () => () => {} },
    slots: {
      inject: (key, callback) => callback(),
      register: (options, registered) => {
        component = registered
        return () => {}
      },
    },
  })
  return component
}

/** Render the dock with a prescribed hook state. */
function render(open, state) {
  stateQueue = [open, state, 0]
  effectCount = 0
  return walk(Dock({ t: (key, params) => {
    const dictionary = {
      'chip.label': 'MCP',
      'chip.connected': '{connected} of {total} connected',
      'chip.configured': '{total} configured',
      'chip.unavailable': 'status unavailable',
      'chip.upstream': '{count} servers',
      'chip.expand': 'Show MCP server details',
      'chip.collapse': 'Hide MCP server details',
      'state.connected': 'connected',
      'state.disconnected': 'unreachable',
      'state.disabled': 'disabled',
      'state.configured': 'configured',
      'state.unknown': 'unknown',
      'state.pluginFailed': 'bridge plugin failed',
      'meta.tools': '{count} tools',
      'meta.toolOne': '1 tool',
      'meta.latency': '{ms} ms',
      'upstream.title': 'Servers behind {server}',
      'upstream.tools': '{count} tools',
      'upstream.toolOne': '1 tool',
      'upstream.more': '+{count} more',
      'action.refresh': 'Refresh',
      'note.stdio': 'stdio servers are not probed',
    }
    const template = dictionary[key] ?? key
    if (!params) return template
    return template.replace(/\{(\w+)\}/g, (match, name) => (name in params ? String(params[name]) : match))
  } }))
}

await check('render: nothing before the first snapshot', async () => {
  const { texts } = render(false, { phase: 'loading', data: null, error: null })
  assert.deepEqual(texts, [])
})

await check('render: nothing when no MCP server is configured', async () => {
  const empty = { generatedAt: '', summary: { total: 0, probed: 0, connected: 0, hasUpstream: false, upstream: 0, upstreamReachable: 0 }, connections: [] }
  const { texts } = render(false, { phase: 'ready', data: empty, error: null })
  assert.deepEqual(texts, [])
})

await check('render: collapsed chip summarizes connections', async () => {
  const { texts } = render(false, { phase: 'ready', data: SNAPSHOT, error: null })
  const joined = texts.join(' | ')
  assert.match(joined, /MCP/)
  assert.match(joined, /1 of 2 connected/)
  assert.match(joined, /10 servers/)
  assert.ok(!joined.includes('pcai-llm-gateway'), 'collapsed chip must not list connections')
})

await check('render: expanded panel lists connections and upstream servers', async () => {
  const { texts, classes } = render(true, { phase: 'ready', data: SNAPSHOT, error: null })
  const joined = texts.join(' | ')
  assert.match(joined, /pcai-llm-gateway/)
  assert.match(joined, /streamable-http · connected · 2 tools · 41 ms/)
  assert.match(joined, /Servers behind pcai-llm-gateway/)
  assert.match(joined, /applygate/)
  assert.match(joined, /k8s-ops/)
  assert.match(joined, /logsearch/)
  assert.match(joined, /connect ECONNREFUSED/)
  assert.match(joined, /retired-server/)
  assert.match(joined, /HTTP 401: unauthorized/)
  assert.match(joined, /Refresh/)
  assert.ok(classes.includes('mcp-status-dock__panel'))
  assert.ok(classes.includes('mcp-status-dock__bar'))
})

await check('render: unavailable status still renders a chip', async () => {
  const { texts } = render(false, { phase: 'error', data: null, error: 'HTTP 404' })
  const joined = texts.join(' | ')
  assert.match(joined, /MCP/)
  assert.match(joined, /status unavailable/)
})

await check('render: missing translator falls back to keys without throwing', async () => {
  stateQueue = [false, { phase: 'ready', data: SNAPSHOT, error: null }, 0]
  const tree = Dock({})
  assert.ok(tree, 'component returned nothing without a translator')
})

await check('style: only theme tokens are referenced', async () => {
  const css = readFileSync(new URL('../client.js', import.meta.url), 'utf8')
  const colors = [...css.matchAll(/:\s*(#[0-9a-fA-F]{3,8}|rgba?\([^)]*\)|hsla?\([^)]*\))\b/g)].map((match) => match[1])
  assert.deepEqual(colors, [], `literal colors found: ${colors.join(', ')}`)
  const variables = [...css.matchAll(/var\((--[a-z0-9-]+)\)/g)].map((match) => match[1])
  assert.ok(variables.length > 0)
  for (const name of variables) assert.ok(name.startsWith('--dsw-alias-'), `non-alias token ${name}`)
})

await check('bundle: imports no Harness Client package', async () => {
  const source = readFileSync(new URL('../client.js', import.meta.url), 'utf8')
  const requires = [...source.matchAll(/require\('([^']+)'\)/g)].map((match) => match[1])
  assert.deepEqual(requires, ['react'])
})

for (const line of results) console.log(line)
const failures = results.filter((line) => line.startsWith('FAIL')).length
console.log(`\n${results.length - failures}/${results.length} checks passed`)
