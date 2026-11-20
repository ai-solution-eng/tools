/**
 * Offline check of the browser half.
 *
 * Loads the shipped `client.js` the way the Harness module loader does, runs
 * `apply()` against a stub Client context, and renders the registered Settings
 * section with stub slot props. No browser, no network, no Harness.
 *
 * What it covers: the registration contract (`settings.section` id, order,
 * locale, injected props), the dictionaries, the rendered states (empty, rows,
 * unavailable, bridge down, problems, external rows), the editor form for both
 * transports, and the live-state store's polling and retry behaviour.
 */
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

const source = readFileSync(new URL('../../src/servers/client.js', import.meta.url), 'utf8')

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
 * Let queued work settle.
 * @returns {Promise<void>} settlement.
 */
const tick = () => new Promise((resolve) => setTimeout(resolve, 10))

/* ------------------------------------------------------------------ *
 * React shim
 * ------------------------------------------------------------------ */

const Fragment = Symbol('Fragment')
let hookIndex = 0
let stateOverrides = {}
const effectCleanups = []

const React = {
  Fragment,
  createElement(type, props, ...children) {
    return {
      type,
      props: { ...(props ?? {}), children: children.flat(Infinity).filter((child) => child !== null && child !== undefined && child !== false) },
    }
  },
  useState(initial) {
    const index = hookIndex
    hookIndex += 1
    const value = Object.hasOwn(stateOverrides, index) ? stateOverrides[index] : typeof initial === 'function' ? initial() : initial
    return [value, () => {}]
  },
  useEffect(fn) {
    const cleanup = fn()
    if (typeof cleanup === 'function') effectCleanups.push(cleanup)
  },
}

/**
 * Render a component with a fresh hook index.
 * @param {Function} component - component to call.
 * @param {Record<string, any>} props - props.
 * @param {Record<number, any>} [overrides] - hook state overrides by call index.
 * @returns {any} element tree.
 */
function render(component, props, overrides = {}) {
  hookIndex = 0
  stateOverrides = overrides
  return component(props)
}

/**
 * Visit every node of a rendered tree, expanding function components the way a
 * renderer would. The shim has no renderer, so a component element is called
 * here.
 * @param {any} node - tree node.
 * @param {(node: any) => void} visit - visitor.
 * @returns {void}
 */
function walk(node, visit) {
  if (node === null || node === undefined || node === false || node === true) return
  if (typeof node === 'string' || typeof node === 'number') return
  if (Array.isArray(node)) {
    for (const child of node) walk(child, visit)
    return
  }
  if (typeof node === 'object' && 'type' in node) {
    if (typeof node.type === 'function') {
      walk(node.type(node.props), visit)
      return
    }
    visit(node)
    walk(node.props.children, visit)
  }
}

/**
 * Concatenate every string in a tree.
 * @param {any} node - tree node.
 * @returns {string} text.
 */
function textOf(node) {
  const parts = []
  walk(node, (entry) => {
    const children = entry.props.children
    for (const child of Array.isArray(children) ? children : []) {
      if (typeof child === 'string' || typeof child === 'number') parts.push(String(child))
    }
  })
  return parts.join(' ')
}

/**
 * Collect nodes whose className contains a marker.
 * @param {any} node - tree node.
 * @param {string} marker - class marker.
 * @returns {any[]} matches.
 */
function byClass(node, marker) {
  const found = []
  walk(node, (entry) => {
    const className = entry.props.className
    if (typeof className !== 'string') return
    if (className.split(/\s+/).includes(marker)) found.push(entry)
  })
  return found
}

/**
 * Collect the props of every element of one type.
 * @param {any} node - tree node.
 * @param {any} type - element type.
 * @returns {any[]} matching props.
 */
function byType(node, type) {
  const found = []
  walk(node, (entry) => {
    if (entry.type === type) found.push(entry.props)
  })
  return found
}

/**
 * Build a store stub over a fixed snapshot.
 * @param {any} snapshot - snapshot value.
 * @returns {any} store.
 */
function storeOf(snapshot) {
  return {
    getSnapshot: () => snapshot,
    subscribe: () => () => {},
  }
}

/* ------------------------------------------------------------------ *
 * Module loading and registration
 * ------------------------------------------------------------------ */

let loaded = null
const window = { __ModuleLoader__: { load(definition) { loaded = definition } } }
new Function('window', source)(window)

const dictionaries = []
const registrations = []
const effects = []
const configForms = { get: (id) => ({ namespace: id }) }
const ctx = {
  effect(fn, label) {
    effects.push(label)
    const cleanup = fn()
    return typeof cleanup === 'function' ? cleanup : () => {}
  },
  get(name) {
    if (name === 'configForms') return configForms
    return undefined
  },
  locale: {
    register(ns, dict) {
      dictionaries.push({ ns, dict })
      return () => {}
    },
    bind(ns) {
      return (key, params) => {
        const dict = dictionaries.find((entry) => entry.ns === ns)?.dict.en ?? {}
        const template = dict[key] ?? key
        if (!params) return template
        return template.replace(/\{(\w+)\}/g, (match, name) => (params[name] === undefined ? match : String(params[name])))
      }
    },
  },
  slots: {
    inject(slot, fn) {
      fn()
      return () => {}
    },
    register(options, component) {
      registrations.push({ options, component })
      return () => {}
    },
  },
}

const plugin = loaded ? loaded.factory((name) => {
  if (name === 'react') return React
  throw new Error(`unexpected require: ${name}`)
}) : null

await check('the module registers under the package name', () => {
  assert.ok(loaded, 'client.js did not register a module')
  assert.equal(loaded.id, '@local/dsh-mcp-servers')
})

await check('the plugin injects slots and locale and exposes apply()', () => {
  assert.ok(plugin)
  assert.deepEqual(plugin.inject, ['slots', 'locale'])
  assert.equal(typeof plugin.apply, 'function')
})

plugin.apply(ctx)

await check('dictionaries carry English and Chinese for every key', () => {
  assert.equal(dictionaries.length, 1)
  assert.equal(dictionaries[0].ns, 'mcp-servers')
  const en = Object.keys(dictionaries[0].dict.en)
  const zh = Object.keys(dictionaries[0].dict.zh)
  assert.ok(en.length > 50, 'expected the full copy deck')
  assert.deepEqual([...en].sort(), [...zh].sort(), 'every key must exist in both languages')
})

await check('every literal translation key used by the module exists in the deck', () => {
  const dict = dictionaries[0].dict.en
  const used = new Set()
  for (const match of source.matchAll(/\bt\('([a-zA-Z][\w.-]*)'/g)) used.add(match[1])
  for (const match of source.matchAll(/'(state\.[a-z]+)'/g)) used.add(match[1])
  for (const match of source.matchAll(/'(error\.[a-zA-Z]+)'/g)) used.add(match[1])
  used.add('transport.streamable-http')
  used.add('transport.stdio')
  const missing = [...used].filter((key) => dict[key] === undefined)
  assert.deepEqual(missing, [], `keys used but not defined: ${missing.join(', ')}`)
})

await check('the page registers as a settings section beside the host pages', () => {
  const section = registrations.find((entry) => entry.options.name === 'settings.section')
  assert.ok(section, 'no settings.section registration')
  assert.equal(section.options.id, 'mcp-servers')
  assert.equal(section.options.order, 12, 'between Models (10) and Plugins (15)')
  assert.equal(section.options.locale, 'mcp-servers')
  assert.equal(section.options.label(), 'MCP servers')
  const injected = section.options.inject()
  assert.ok(injected.form, 'the settings form is injected into the page')
  assert.ok(injected.live, 'the live store is injected into the page')
})

await check('the navigation icon registers for the same section id', () => {
  const icon = registrations.find((entry) => entry.options.name === 'settings.section.icon')
  assert.ok(icon, 'no settings.section.icon registration')
  assert.equal(icon.options.id, 'mcp-servers')
  const tree = icon.component({ size: 16, active: true })
  assert.equal(tree.type, 'svg')
  assert.equal(tree.props.width, 16)
})

/* ------------------------------------------------------------------ *
 * Rendering
 * ------------------------------------------------------------------ */

const t = (key, params) => {
  const template = dictionaries[0].dict.en[key] ?? key
  if (!params) return template
  return template.replace(/\{(\w+)\}/g, (match, name) => (params[name] === undefined ? match : String(params[name])))
}

const section = registrations.find((entry) => entry.options.name === 'settings.section').component

/**
 * Render the page with a form snapshot and a live snapshot.
 * @param {{ form: any, live?: any, overrides?: Record<number, any> }} options - fixtures.
 * @returns {any} tree.
 */
function renderPage(options) {
  return render(
    section,
    { t, form: storeOf(options.form), live: storeOf(options.live ?? { status: 'idle', data: null, error: null }) },
    options.overrides ?? {},
  )
}

const readyForm = (servers) => ({ status: 'ready', value: { servers }, base: { servers: [] }, user: { servers }, revision: 1, writable: true })

await check('an empty list shows the empty state and the add action', () => {
  const tree = renderPage({ form: readyForm([]) })
  const text = textOf(tree)
  assert.match(text, /MCP servers/)
  assert.match(text, /No MCP servers yet/)
  assert.match(text, /Add a Streamable HTTP endpoint or a stdio command/)
  const buttons = byType(tree, 'button').map((props) => textOf({ type: 'button', props }))
  assert.ok(buttons.some((label) => label.includes('Add server')))
})

await check('a configured server shows its name, transport, endpoint and live state', () => {
  const tree = renderPage({
    form: readyForm([
      { id: 'github', serverName: 'github', transport: 'streamable-http', url: 'https://mcp.example.com/mcp', enabled: true },
      { id: 'local', serverName: 'local', transport: 'stdio', command: 'npx', args: ['-y', 'server'], enabled: true },
    ]),
    live: {
      status: 'ready',
      error: null,
      data: {
        bridge: { available: true },
        servers: [
          { id: 'github', serverName: 'github', state: 'ready', fiberState: 'active', toolCount: 12, tools: ['search', 'fetch'] },
          { id: 'local', serverName: 'local', state: 'starting', fiberState: 'loading', toolCount: 0 },
        ],
        problems: [],
        external: [],
      },
    },
  })
  const text = textOf(tree)
  assert.match(text, /github/)
  assert.match(text, /https:\/\/mcp.example.com\/mcp/)
  assert.match(text, /Streamable HTTP/)
  assert.match(text, /Ready/)
  assert.match(text, /12 tools/)
  assert.match(text, /local/)
  assert.match(text, /npx/)
  assert.match(text, /Connecting/)
  const rows = byClass(tree, 'dsh-mcp-servers__row')
  assert.equal(rows.length, 2)
  const actions = byClass(tree, 'dsh-mcp-servers__ghost').map((entry) => textOf(entry))
  assert.ok(actions.some((label) => label.includes('Edit')))
  assert.ok(actions.some((label) => label.includes('Disable')))
  assert.ok(actions.some((label) => label.includes('Remove')))
})

await check('a disabled server shows as disabled and offers Enable', () => {
  const tree = renderPage({
    form: readyForm([{ id: 'one', serverName: 'one', transport: 'streamable-http', url: 'https://one.example.com/mcp', enabled: false }]),
    live: { status: 'ready', error: null, data: { bridge: { available: true }, servers: [], problems: [], external: [] } },
  })
  const text = textOf(tree)
  assert.match(text, /Disabled/)
  const actions = byClass(tree, 'dsh-mcp-servers__ghost').map((entry) => textOf(entry))
  assert.ok(actions.some((label) => label.includes('Enable')))
})

await check('an unusable row is reported under the row', () => {
  const tree = renderPage({
    form: readyForm([{ id: 'bad', serverName: 'bad name', transport: 'streamable-http', url: 'https://x.example.com/mcp', enabled: true }]),
    live: {
      status: 'ready',
      error: null,
      data: {
        bridge: { available: true },
        servers: [],
        problems: [{ id: 'bad', serverName: 'bad name', message: 'serverName must be 1-32 characters' }],
        external: [],
      },
    },
  })
  const text = textOf(tree)
  assert.match(text, /Cannot start/)
  assert.match(text, /serverName must be 1-32 characters/)
})

await check('an unavailable settings namespace explains itself instead of showing an empty list', () => {
  const tree = renderPage({ form: { status: 'unavailable', value: undefined, revision: undefined, writable: false } })
  const text = textOf(tree)
  assert.match(text, /Configuration is not available/)
  assert.match(text, /not active in this profile/)
  assert.match(text, /Try again/)
  const add = byType(tree, 'button').find((props) => textOf({ type: 'button', props }).includes('Add server'))
  assert.equal(add.disabled, true, 'nothing can be written when the row is unavailable')
})

await check('a missing settings transport shows the unavailable state, not a spinner', () => {
  hookIndex = 0
  stateOverrides = {}
  const tree = section({ t, form: undefined, live: storeOf({ status: 'idle', data: null, error: null }) })
  const text = textOf(tree)
  assert.match(text, /Configuration is not available/)
  assert.equal(text.includes('Loading configuration'), false)
})

await check('an unavailable bridge is surfaced with its reason', () => {
  const tree = renderPage({
    form: readyForm([]),
    live: {
      status: 'ready',
      error: null,
      data: { bridge: { available: false, error: 'MODULE_NOT_FOUND' }, servers: [], problems: [], external: [] },
    },
  })
  const text = textOf(tree)
  assert.match(text, /The MCP bridge is unavailable/)
  assert.match(text, /MODULE_NOT_FOUND/)
})

await check('servers configured elsewhere are listed read-only', () => {
  const tree = renderPage({
    form: readyForm([]),
    live: {
      status: 'ready',
      error: null,
      data: {
        bridge: { available: true },
        servers: [],
        problems: [],
        external: [
          { id: 'mcp-hpe-networking', serverName: 'hpe-networking', transport: 'streamable-http', endpoint: 'https://hpe.example.com/mcp', fiberState: 'active', toolCount: 4 },
        ],
      },
    },
  })
  const text = textOf(tree)
  assert.match(text, /Configured elsewhere/)
  assert.match(text, /hpe-networking/)
  assert.match(text, /4 tools/)
  const rows = byClass(tree, 'dsh-mcp-servers__row')
  assert.equal(rows.length, 1)
  assert.equal(byType(tree, 'button').some((props) => textOf({ type: 'button', props }).includes('Remove')), false, 'external rows expose no actions')
})

await check('the add form offers the streamable-http fields', () => {
  const draft = {
    id: '', serverName: '', transport: 'streamable-http', url: '', headers: [], command: '', argsText: '', cwd: '', env: [],
    toolCallTimeoutMs: '60000', maxInstructionBytes: '32768', failOnStartupError: false, enabled: true,
  }
  const tree = renderPage({ form: readyForm([]), overrides: { 2: draft } })
  const text = textOf(tree)
  assert.match(text, /Add MCP server/)
  assert.match(text, /Server name/)
  assert.match(text, /Transport/)
  assert.match(text, /Request headers/)
  assert.match(text, /Save server/)
  assert.match(text, /Cancel/)
  assert.equal(text.includes('Working directory'), false, 'stdio-only fields stay hidden')
  const labels = byClass(tree, 'dsh-mcp-servers__label').map((entry) => textOf(entry))
  assert.ok(labels.includes('Server name'))
  assert.ok(labels.includes('URL'))
})

await check('a connected server can list its tools', () => {
  const tree = renderPage({
    form: readyForm([
      { id: 'github', serverName: 'github', transport: 'streamable-http', url: 'https://mcp.example.com/mcp', enabled: true },
    ]),
    live: {
      status: 'ready',
      error: null,
      data: {
        bridge: { available: true },
        servers: [{ id: 'github', serverName: 'github', state: 'ready', fiberState: 'active', toolCount: 2, tools: ['fetch', 'search'] }],
        problems: [],
        external: [],
      },
    },
  })
  const summary = byType(tree, 'summary').map((props) => textOf({ type: 'summary', props }))
  assert.ok(summary.some((label) => label.includes('Tools (2)')), 'the list is collapsed behind a summary')
  const text = textOf(tree)
  assert.match(text, /fetch/)
  assert.match(text, /search/)
})

await check('the editor masks a credential header value', () => {
  const base = {
    id: '', serverName: 'x', transport: 'streamable-http', url: 'http://x/mcp',
    command: '', argsText: '', cwd: '', env: [],
    toolCallTimeoutMs: '60000', maxInstructionBytes: '262144', catalogUrl: '',
    failOnStartupError: false, enabled: true,
  }
  const secretDraft = { ...base, headers: [{ key: 'Authorization', value: 'Bearer sk-secret' }] }
  const secretTree = renderPage({ form: readyForm([]), overrides: { 2: secretDraft } })
  const secretInput = byType(secretTree, 'input').find((props) => props.value === 'Bearer sk-secret')
  assert.ok(secretInput, 'the header value input is rendered')
  assert.equal(secretInput.type, 'password', 'a bearer token is masked')

  const plainDraft = { ...base, headers: [{ key: 'X-Auth-Request-User', value: 'francesco-caliva' }] }
  const plainTree = renderPage({ form: readyForm([]), overrides: { 2: plainDraft } })
  const plainInput = byType(plainTree, 'input').find((props) => props.value === 'francesco-caliva')
  assert.equal(plainInput.type, 'text', 'a non-credential header stays readable')
})

await check('the add form offers the stdio fields', () => {
  const draft = {
    id: '', serverName: 'local', transport: 'stdio', url: '', headers: [], command: '', argsText: '', cwd: '', env: [],
    toolCallTimeoutMs: '60000', maxInstructionBytes: '32768', failOnStartupError: false, enabled: true,
  }
  const tree = renderPage({ form: readyForm([]), overrides: { 2: draft } })
  const text = textOf(tree)
  assert.match(text, /Command/)
  assert.match(text, /Arguments/)
  assert.match(text, /Working directory/)
  assert.match(text, /Environment/)
  assert.equal(text.includes('Request headers'), false, 'http-only fields stay hidden')
})

await check('a live-state read failure is reported without hiding the list', () => {
  const tree = renderPage({
    form: readyForm([{ id: 'one', serverName: 'one', transport: 'streamable-http', url: 'https://one.example.com/mcp', enabled: true }]),
    live: { status: 'error', data: null, error: 'HTTP 500' },
  })
  const text = textOf(tree)
  assert.match(text, /HTTP 500/)
  assert.match(text, /one/)
})

await check('the settings transport is resolved lazily, not captured at apply time', () => {
  // A second instance whose configForms service only appears after activation,
  // which is the real activation order of the settings UI.
  let configFormsService
  const lateRegistrations = []
  const lateDictionaries = []
  const lateCtx = {
    effect(fn) {
      const cleanup = fn()
      return typeof cleanup === 'function' ? cleanup : () => {}
    },
    get(name) {
      return name === 'configForms' ? configFormsService : undefined
    },
    locale: {
      register(ns, dict) {
        lateDictionaries.push({ ns, dict })
        return () => {}
      },
      bind: () => (key) => key,
    },
    slots: {
      inject(slot, fn) {
        fn()
        return () => {}
      },
      register(options, component) {
        lateRegistrations.push({ options, component })
        return () => {}
      },
    },
  }
  const late = loaded.factory((name) => {
    if (name === 'react') return React
    throw new Error(`unexpected require: ${name}`)
  })
  late.apply(lateCtx)
  const section = lateRegistrations.find((entry) => entry.options.name === 'settings.section')
  assert.ok(section)
  assert.equal(section.options.inject().form, undefined, 'no form before the service exists')
  const controller = { namespace: 'mcp-servers' }
  configFormsService = { get: (id) => ({ ...controller, id }) }
  const resolved = section.options.inject()
  assert.ok(resolved.form, 'the form resolves once the service appears')
  assert.equal(resolved.form.id, 'mcp-servers')
  assert.equal(typeof resolved.resolveForm, 'function')
})

/* ------------------------------------------------------------------ *
 * Live store
 * ------------------------------------------------------------------ */

await check('the live store polls the Host route and can ask for a retry', async () => {
  const calls = []
  const originalFetch = globalThis.fetch
  globalThis.fetch = async (url) => {
    calls.push(String(url))
    return {
      ok: true,
      async json() {
        return { bridge: { available: true }, servers: [{ id: 'one', serverName: 'one', state: 'ready', toolCount: 2 }], problems: [], external: [] }
      },
    }
  }
  try {
    const injected = registrations.find((entry) => entry.options.name === 'settings.section').options.inject()
    const live = injected.live
    assert.equal(live.getSnapshot().status, 'idle')
    const unsubscribe = live.subscribe(() => {})
    await tick()
    assert.equal(calls.length, 1)
    assert.equal(calls[0], 'api/mcp-servers')
    const snapshot = live.getSnapshot()
    assert.equal(snapshot.status, 'ready')
    assert.equal(snapshot.data.servers[0].toolCount, 2)
    await live.refresh(true)
    assert.ok(calls.some((url) => url.includes('retry=1')), 'retry asks the Host to remount failed servers')
    unsubscribe()
  } finally {
    globalThis.fetch = originalFetch
  }
})

/* ------------------------------------------------------------------ *
 * Report
 * ------------------------------------------------------------------ */

for (const cleanup of effectCleanups) {
  try {
    cleanup()
  } catch {
    // Cleanup failures are not the subject of this check.
  }
}

let failed = 0
for (const line of results) {
  if (line.startsWith('FAIL')) failed += 1
  console.log(`  ${line}`)
}
console.log(failed === 0 ? `\nall ${results.length} checks passed` : `\n${failed} of ${results.length} FAILED`)
process.exit(failed === 0 ? 0 : 1)
