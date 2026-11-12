/**
 * MCP status — Host half.
 *
 * Answers one question for the Web UI: which MCP servers is this Harness
 * actually connected to right now, and what sits behind them?
 *
 * The bridge plugin (`@deepseek-ai/dsh-mcp-client`) exposes no status surface
 * of its own, and its Loader row stays `active` even when the server is down
 * (`failOnStartupError` defaults to false), so activation state cannot answer
 * the question. This plugin therefore reads the *resolved* config of every MCP
 * client row from the Loader and probes each endpoint with a real MCP
 * handshake (see `./probe.js`). A federating MCP server that publishes a
 * catalog tool is asked for its upstream servers too, which is how one
 * connection can stand for many MCP servers.
 *
 * The snapshot is served on an authenticated route under `/api`, registered
 * through `ctx.connection.fetch`, so only the owner browser peer can read it.
 * Request headers (which carry credentials) never leave the Host: the wire
 * projection in `toWire()` whitelists fields.
 *
 * @module
 */

import { commandName, messageOf, probeMcpHttp, redactUrl } from './probe.js'

/** Loader rows this plugin reports on. */
const MCP_CLIENT_SPECIFIER = 'mcp-client'

/** Fiber lifecycle names, indexed by the Cordis `FiberState` value. */
const FIBER_STATES = ['pending', 'loading', 'active', 'failed', 'disposed', 'unloading']

/** Accepted settings and their defaults. */
const DEFAULT_SETTINGS = {
  /** Authenticated route path served to the browser. */
  routePath: '/api/mcp-status',
  /** Budget for one endpoint probe (handshake plus tool listing). */
  probeTimeoutMs: 4000,
  /** How long one probe result is reused for later reads. */
  connectionTtlMs: 5000,
  /** How long a federating server's upstream catalog is reused. */
  upstreamTtlMs: 60000,
  /** Tool that lists a federating server's upstream MCP servers. */
  catalogTool: 'mcp_catalog',
  /** Tool names included per connection in the snapshot. */
  maxToolsListed: 24,
  /** Upstream server rows included per connection. */
  maxUpstreamServers: 64,
}

/** Numeric bounds applied to configured settings. */
const BOUNDS = {
  probeTimeoutMs: [200, 30000],
  connectionTtlMs: [0, 300000],
  upstreamTtlMs: [0, 3600000],
  maxToolsListed: [1, 200],
  maxUpstreamServers: [1, 500],
}

/** Services required before this plugin can answer. */
export const inject = ['connection']

/**
 * Normalize a raw Loader config into accepted settings.
 * @param {unknown} config - row config.
 * @returns {typeof DEFAULT_SETTINGS} resolved settings.
 */
function resolveSettings(config) {
  const raw = config && typeof config === 'object' ? config : {}
  const settings = { ...DEFAULT_SETTINGS }
  for (const [key, fallback] of Object.entries(DEFAULT_SETTINGS)) {
    const value = raw[key]
    if (typeof fallback === 'number') {
      if (typeof value !== 'number' || !Number.isFinite(value)) continue
      const [min, max] = BOUNDS[key] ?? [Number.MIN_SAFE_INTEGER, Number.MAX_SAFE_INTEGER]
      settings[key] = Math.min(max, Math.max(min, Math.floor(value)))
    } else if (typeof value === 'string' && value.trim()) {
      settings[key] = value.trim()
    }
  }
  if (!settings.routePath.startsWith('/')) settings.routePath = `/${settings.routePath}`
  return settings
}

/**
 * Project a snapshot value as a JSON response.
 * @param {unknown} value - JSON-serializable body.
 * @param {number} [status] - HTTP status.
 * @returns {Response} the response.
 */
function jsonResponse(value, status = 200) {
  return new Response(JSON.stringify(value), {
    status,
    headers: {
      'content-type': 'application/json; charset=utf-8',
      'cache-control': 'no-store',
    },
  })
}

/**
 * Read every Loader row, preferring the Loader service and falling back to the
 * configuration editor that exposes the same entries.
 * @param {import('@deepseek-ai/cordis').Context} ctx - plugin context.
 * @returns {any[]} entry list.
 */
function readEntries(ctx) {
  try {
    const loader = ctx.loader
    if (loader && typeof loader.entries === 'function') return Array.from(loader.entries())
  } catch {
    // A profile without the Loader service falls through to the editor.
  }
  try {
    const editor = ctx.configEditor
    if (editor && typeof editor.entries === 'function') {
      const entries = editor.entries()
      return Array.isArray(entries) ? entries : Array.from(entries ?? [])
    }
  } catch {
    // No configuration editor either: report an empty inventory.
  }
  return []
}

/**
 * The config a row is actually running with: the Fiber's validated config
 * (defaults applied, `!!js` expressions evaluated) when active, else the raw
 * row config for a disabled or failed row.
 *
 * `fiber.config` is NOT that object on Cordis 0.2.2+: it is the raw row input
 * retained for the next activation, so a `!!js` scalar (such as the MCP
 * Authorization header built from an environment variable) stays the inert
 * `{ __jsExpr }` marker the YAML dialect produces. `fiber.effectiveConfig` is
 * the object the plugin was activated with. Reading `fiber.config` here made
 * every expression-derived credential invisible to the probe, so the probe
 * sent no Authorization header and reported a healthy server as disconnected
 * with HTTP 401.
 * @param {any} entry - Loader entry.
 * @returns {Record<string, any> | undefined} resolved config.
 */
function resolvedConfigOf(entry) {
  try {
    const effective = entry?.fiber?.effectiveConfig
    if (effective && typeof effective === 'object') return effective
    const running = entry?.fiber?.config
    if (running && typeof running === 'object') return running
    const raw = entry?.options?.config
    return raw && typeof raw === 'object' ? raw : undefined
  } catch {
    return undefined
  }
}

/**
 * Whether one row configures an MCP server.
 * @param {any} entry - Loader entry.
 * @returns {boolean} true for an MCP client row.
 */
function isMcpClientEntry(entry) {
  const specifier = entry?.options?.name
  if (typeof specifier === 'string' && specifier.includes(MCP_CLIENT_SPECIFIER)) return true
  const config = resolvedConfigOf(entry)
  return Boolean(config && typeof config.serverName === 'string' && typeof config.transport === 'string')
}

/**
 * Whether the row or one of its owners disables it.
 * @param {any} entry - Loader entry.
 * @returns {boolean} true when disabled.
 */
function isDisabled(entry) {
  try {
    return entry?.disabled === true
  } catch {
    return false
  }
}

/**
 * Name of the row's Cordis fiber state.
 * @param {any} entry - Loader entry.
 * @returns {string} lifecycle name.
 */
function pluginStateOf(entry) {
  const state = entry?.fiber?.state
  return typeof state === 'number' ? FIBER_STATES[state] ?? 'unknown' : 'inactive'
}

/**
 * Count the tools the bridge currently publishes for one server name.
 * This is the only available signal for stdio servers, which a status read
 * must not spawn.
 * @param {import('@deepseek-ai/cordis').Context} ctx - plugin context.
 * @param {string} serverName - MCP server namespace.
 * @returns {number | undefined} registered tool count, or undefined when unknown.
 */
function registeredToolCount(ctx, serverName) {
  try {
    const tools = ctx.tools
    if (!tools || typeof tools.schemas !== 'function') return undefined
    const schemas = tools.schemas()
    if (!Array.isArray(schemas)) return undefined
    const prefix = `mcp__${serverName}__`
    return schemas.filter((schema) => typeof schema?.name === 'string' && schema.name.startsWith(prefix)).length
  } catch {
    return undefined
  }
}

/**
 * Build the internal view of one configured MCP server.
 * The returned object may carry request headers under `target`; `toWire()`
 * strips them before anything leaves the Host.
 * @param {import('@deepseek-ai/cordis').Context} ctx - plugin context.
 * @param {any} entry - Loader entry.
 * @param {typeof DEFAULT_SETTINGS} settings - resolved settings.
 * @returns {Promise<Record<string, any>>} internal connection view.
 */
async function describeConnection(ctx, entry, settings) {
  const config = resolvedConfigOf(entry) ?? {}
  const serverName = typeof config.serverName === 'string' && config.serverName.trim()
    ? config.serverName.trim()
    : String(entry?.options?.id ?? 'mcp')
  const transport = typeof config.transport === 'string' ? config.transport : 'unknown'
  const connection = {
    id: String(entry?.options?.id ?? serverName),
    serverName,
    transport,
    pluginState: pluginStateOf(entry),
    disabled: isDisabled(entry),
  }
  if (typeof config.url === 'string' && config.url.trim()) connection.endpoint = redactUrl(config.url.trim())
  if (transport === 'stdio' && typeof config.command === 'string') connection.command = commandName(config.command)

  if (connection.disabled) return { ...connection, state: 'disabled' }

  if (transport === 'streamable-http') {
    if (typeof config.url !== 'string' || !config.url.trim()) {
      return { ...connection, state: 'unknown', error: 'no endpoint configured' }
    }
    // The cheap probe only establishes liveness and the tool list; the catalog
    // call belongs to `withUpstream()`, which is cached on its own slower clock.
    const probe = await probeMcpHttp({
      url: config.url,
      headers: config.headers,
      timeoutMs: settings.probeTimeoutMs,
    })
    connection.target = { url: config.url, headers: config.headers }
    return applyProbe(connection, probe, settings)
  }

  if (transport === 'stdio') {
    const toolCount = registeredToolCount(ctx, serverName)
    return {
      ...connection,
      state: toolCount ? 'configured' : 'unknown',
      toolCount,
      note: 'stdio servers are not spawned for a status read',
    }
  }

  return { ...connection, state: 'unknown', error: `unsupported transport "${transport}"` }
}

/**
 * Fold one probe result into the connection view.
 * @param {Record<string, any>} connection - connection under construction.
 * @param {import('./probe.js').ProbeResult} probe - probe result.
 * @param {typeof DEFAULT_SETTINGS} settings - resolved settings.
 * @returns {Record<string, any>} completed view.
 */
function applyProbe(connection, probe, settings) {
  if (!probe.ok) {
    return { ...connection, state: 'disconnected', latencyMs: probe.latencyMs, error: probe.error }
  }
  const toolNames = Array.isArray(probe.toolNames) ? probe.toolNames : []
  const hidden = Math.max(0, toolNames.length - settings.maxToolsListed)
  return {
    ...connection,
    state: 'connected',
    latencyMs: probe.latencyMs,
    protocolVersion: probe.protocolVersion,
    serverInfo: probe.serverInfo,
    capabilities: probe.capabilities,
    toolCount: toolNames.length,
    tools: toolNames.slice(0, settings.maxToolsListed),
    toolsHidden: hidden > 0 ? hidden : undefined,
    catalogAvailable: toolNames.includes(settings.catalogTool),
  }
}

/**
 * Probe every configured MCP server.
 * @param {import('@deepseek-ai/cordis').Context} ctx - plugin context.
 * @param {typeof DEFAULT_SETTINGS} settings - resolved settings.
 * @returns {Promise<Array<Record<string, any>>>} connection views.
 */
async function collectConnections(ctx, settings) {
  const entries = readEntries(ctx).filter((entry) => entry && !entry.options?.group && isMcpClientEntry(entry))
  return Promise.all(entries.map((entry) => describeConnection(ctx, entry, settings)))
}

/**
 * Read the connection list, reusing the previous probe result while it is fresh
 * and sharing one in-flight probe run between concurrent readers.
 * @param {import('@deepseek-ai/cordis').Context} ctx - plugin context.
 * @param {typeof DEFAULT_SETTINGS} settings - resolved settings.
 * @param {{ connections: { at: number, value: any[] | null, pending: Promise<any[]> | null } }} caches - caches.
 * @returns {Promise<Array<Record<string, any>>>} connection views.
 */
function readConnections(ctx, settings, caches) {
  const cache = caches.connections
  const fresh = cache.value !== null && Date.now() - cache.at < settings.connectionTtlMs
  if (fresh) return Promise.resolve(cache.value)
  if (cache.pending) return cache.pending
  const pending = collectConnections(ctx, settings).then(
    (value) => {
      cache.value = value
      cache.at = Date.now()
      cache.pending = null
      return value
    },
    (error) => {
      cache.pending = null
      throw error
    },
  )
  cache.pending = pending
  return pending
}

/**
 * Ask a federating connection for its upstream servers, cached per endpoint.
 * @param {Record<string, any>} connection - connected MCP server view.
 * @param {typeof DEFAULT_SETTINGS} settings - resolved settings.
 * @param {Map<string, { at: number, value: any }>} cache - upstream cache.
 * @returns {Promise<Record<string, any>>} connection view with `upstream`.
 */
async function withUpstream(connection, settings, cache) {
  if (connection.state !== 'connected' || !connection.catalogAvailable || !connection.target) return connection
  const key = `${connection.serverName}|${connection.endpoint ?? ''}`
  const cached = cache.get(key)
  if (cached && Date.now() - cached.at < settings.upstreamTtlMs) {
    return { ...connection, upstream: cached.value }
  }
  const probe = await probeMcpHttp({
    url: connection.target.url,
    headers: connection.target.headers,
    timeoutMs: settings.probeTimeoutMs,
    catalogTool: settings.catalogTool,
  })
  const servers = Array.isArray(probe.catalog)
    ? probe.catalog.slice(0, settings.maxUpstreamServers)
    : []
  const upstream = {
    generatedAt: new Date().toISOString(),
    tool: settings.catalogTool,
    servers,
    error: probe.catalogError ?? (servers.length ? undefined : 'no upstream servers reported'),
  }
  cache.set(key, { at: Date.now(), value: upstream })
  return { ...connection, upstream }
}

/**
 * Summarize the connection list for the collapsed chip.
 * @param {Array<Record<string, any>>} connections - connection views.
 * @returns {Record<string, number | boolean>} summary counters.
 */
function summarize(connections) {
  const probed = connections.filter((item) => item.state === 'connected' || item.state === 'disconnected')
  const connected = connections.filter((item) => item.state === 'connected')
  let upstream = 0
  let upstreamReachable = 0
  let hasUpstream = false
  for (const connection of connections) {
    const servers = connection.upstream?.servers
    if (!Array.isArray(servers) || servers.length === 0) continue
    hasUpstream = true
    upstream += servers.length
    upstreamReachable += servers.filter((server) => server.reachable).length
  }
  return {
    total: connections.length,
    probed: probed.length,
    connected: connected.length,
    hasUpstream,
    upstream,
    upstreamReachable,
  }
}

/**
 * Whitelist the fields that may reach the browser. Request headers are
 * deliberately absent: they carry the MCP credential.
 * @param {Record<string, any>} connection - internal connection view.
 * @returns {Record<string, any>} wire connection.
 */
function toWire(connection) {
  const wire = {
    id: connection.id,
    serverName: connection.serverName,
    transport: connection.transport,
    state: connection.state,
    pluginState: connection.pluginState,
    disabled: connection.disabled,
  }
  if (connection.endpoint !== undefined) wire.endpoint = connection.endpoint
  if (connection.command !== undefined) wire.command = connection.command
  if (connection.latencyMs !== undefined) wire.latencyMs = connection.latencyMs
  if (connection.protocolVersion !== undefined) wire.protocolVersion = connection.protocolVersion
  if (connection.serverInfo !== undefined) wire.serverInfo = connection.serverInfo
  if (connection.capabilities !== undefined) wire.capabilities = connection.capabilities
  if (connection.toolCount !== undefined) wire.toolCount = connection.toolCount
  if (connection.tools !== undefined) wire.tools = connection.tools
  if (connection.toolsHidden !== undefined) wire.toolsHidden = connection.toolsHidden
  if (connection.error !== undefined) wire.error = connection.error
  if (connection.note !== undefined) wire.note = connection.note
  if (connection.upstream !== undefined) {
    wire.upstream = {
      generatedAt: connection.upstream.generatedAt,
      tool: connection.upstream.tool,
      servers: connection.upstream.servers,
      error: connection.upstream.error,
    }
  }
  return wire
}

/**
 * Build the snapshot served to the browser.
 * @param {import('@deepseek-ai/cordis').Context} ctx - plugin context.
 * @param {typeof DEFAULT_SETTINGS} settings - resolved settings.
 * @param {{ connections: { at: number, value: any[] | null, pending: Promise<any[]> | null }, upstream: Map<string, { at: number, value: any }> }} caches - caches.
 * @param {boolean} wantUpstream - whether to include upstream server lists.
 * @returns {Promise<Record<string, any>>} snapshot.
 */
async function buildSnapshot(ctx, settings, caches, wantUpstream) {
  const connections = await readConnections(ctx, settings, caches)
  const expanded = wantUpstream
    ? await Promise.all(connections.map((connection) => withUpstream(connection, settings, caches.upstream)))
    : connections
  return {
    generatedAt: new Date().toISOString(),
    summary: summarize(expanded),
    connections: expanded.map(toWire),
  }
}

/**
 * Register the status route.
 * @param {import('@deepseek-ai/cordis').Context} ctx - plugin context.
 * @param {unknown} config - row config.
 */
export function apply(ctx, config) {
  const settings = resolveSettings(config)
  const caches = {
    connections: { at: 0, value: null, pending: null },
    upstream: new Map(),
  }

  const route = {
    path: settings.routePath,
    methods: ['GET'],
    requestBody: 'buffered',
    fetch: async (request) => {
      try {
        const url = new URL(request.url)
        const snapshot = await buildSnapshot(ctx, settings, caches, url.searchParams.get('upstream') === '1')
        return jsonResponse(snapshot)
      } catch (error) {
        return jsonResponse({
          generatedAt: new Date().toISOString(),
          error: messageOf(error),
          summary: { total: 0, probed: 0, connected: 0, hasUpstream: false, upstream: 0, upstreamReachable: 0 },
          connections: [],
        }, 500)
      }
    },
  }

  ctx.effect(() => ctx.connection.fetch.register(route), 'mcp-status: status route')
  try {
    ctx.logger?.debug?.(`mcp-status: serving ${settings.routePath}`)
  } catch {
    // Diagnostics must never prevent the route from working.
  }
}

export { buildSnapshot, describeConnection, resolveSettings, summarize, toWire }
