/**
 * Upstream discovery for a federating MCP server.
 *
 * One MCP connection can stand for many MCP servers: a gateway publishes its
 * upstream list as a catalog tool, or keeps it at a REST endpoint beside the
 * MCP one (LiteLLM's `/v1/mcp/server`), or — when the catalog exists but denies
 * access — names it in the namespaced tool names it proxies.
 *
 * This module answers that question for **both halves of the bundle**: the
 * status half renders the upstream servers under a connected server, and the
 * Settings page renders them under the row that configures it. One
 * implementation is what makes the two surfaces agree about the same gateway;
 * a second copy would drift the moment either side learned a new catalog shape.
 *
 * Deliberately dependency-free beyond `./probe.js`, so the package stays
 * installable without a `node_modules` tree of its own.
 *
 * @module
 */

import { catalogCandidatesFor, fetchCatalog, probeMcpHttp } from './probe.js'

/**
 * Upstream discovery settings and their defaults.
 *
 * Shared by both halves: the status half folds these into its own row
 * settings, and the Settings page reads them from the same row config.
 */
export const UPSTREAM_DEFAULTS = {
  /** Budget for one endpoint probe (handshake plus tool listing). */
  probeTimeoutMs: 4000,
  /** How long a federating server's upstream catalog is reused. */
  upstreamTtlMs: 60000,
  /** Preferred tool that lists a federating server's upstream MCP servers. */
  catalogTool: 'mcp_catalog',
  /**
   * Catalog tool names tried when the preferred one does not exist. Federating
   * gateways disagree about the name, so one panel can unroll several of them.
   */
  catalogTools: ['mcp_catalog', 'list_mcp_servers', 'list_servers', 'list_mcp_tools'],
  /** Upstream server rows included per connection. */
  maxUpstreamServers: 64,
}

/** Numeric bounds applied to the upstream settings. */
export const UPSTREAM_BOUNDS = {
  probeTimeoutMs: [200, 30000],
  upstreamTtlMs: [0, 3600000],
  maxUpstreamServers: [1, 500],
}

/**
 * Resolve the upstream settings from a raw row config.
 *
 * Only the keys this module owns are read; every other key in the row belongs
 * to the half that owns it. Unknown or malformed values fall back to the
 * default rather than failing the row.
 * @param {unknown} config - raw row config.
 * @returns {typeof UPSTREAM_DEFAULTS} resolved settings.
 */
export function resolveUpstreamSettings(config) {
  const raw = config && typeof config === 'object' ? config : {}
  const settings = { ...UPSTREAM_DEFAULTS }
  for (const [key, fallback] of Object.entries(UPSTREAM_DEFAULTS)) {
    const value = raw[key]
    if (typeof fallback === 'number') {
      if (typeof value !== 'number' || !Number.isFinite(value)) continue
      const [min, max] = UPSTREAM_BOUNDS[key] ?? [Number.MIN_SAFE_INTEGER, Number.MAX_SAFE_INTEGER]
      settings[key] = Math.min(max, Math.max(min, Math.floor(value)))
    } else if (Array.isArray(fallback)) {
      if (!Array.isArray(value)) continue
      const names = value
        .filter((entry) => typeof entry === 'string' && entry.trim())
        .map((entry) => entry.trim())
      if (names.length > 0) settings[key] = names
    } else if (typeof value === 'string' && value.trim()) {
      settings[key] = value.trim()
    }
  }
  return settings
}

/**
 * Group a gateway's tool names into upstream servers.
 *
 * A gateway that refuses its catalog still namespaces every proxied tool as
 * `<server>-<tool>` (LiteLLM does), so the server names and their tool counts
 * are already in the tool list. This is the last resort, used only when the
 * catalog endpoint exists but denies access — a 404 means "not a gateway".
 * @param {string[]} toolNames - tools the server listed.
 * @returns {Array<{ server: string, toolCount: number, reachable: boolean }>} derived rows.
 */
export function deriveServersFromTools(toolNames) {
  const counts = new Map()
  for (const name of Array.isArray(toolNames) ? toolNames : []) {
    const at = name.indexOf('-')
    if (at <= 0 || at === name.length - 1) continue
    const server = name.slice(0, at)
    counts.set(server, (counts.get(server) ?? 0) + 1)
  }
  return [...counts.entries()].map(([server, toolCount]) => ({ server, toolCount, reachable: true }))
}

/**
 * Guess a gateway's REST catalog endpoint from its MCP endpoint.
 *
 * LiteLLM proxies the upstream tools and keeps the server list at
 * `/v1/mcp/server`, so a gateway that publishes no catalog tool still has an
 * answer — it is just not an MCP one.
 * @param {string} url - MCP endpoint.
 * @returns {string | undefined} catalog URL, or undefined when the shape is unknown.
 */
export function deriveCatalogUrl(url) {
  try {
    const parsed = new URL(url)
    if (!/\/mcp\/?$/.test(parsed.pathname)) return undefined
    return parsed.origin + '/v1/mcp/server'
  } catch {
    return undefined
  }
}

/**
 * Build the connection view `withUpstream()` needs from what a caller already
 * knows about a server.
 *
 * The status half builds it from a probe; the Settings page builds it from the
 * tool registry, which already says whether a catalog tool exists. Both end up
 * with the same shape, so neither has to re-implement the catalog rule.
 * @param {{ serverName: string, url: string, headers?: unknown, catalogUrl?: string, toolNames?: string[], endpoint?: string, connected?: boolean }} server - known facts.
 * @param {{ catalogTool?: string, catalogTools?: string[] }} settings - resolved settings.
 * @returns {Record<string, any>} connection view.
 */
export function connectionFor(server, settings) {
  const toolNames = Array.isArray(server.toolNames) ? server.toolNames : []
  return {
    serverName: server.serverName,
    state: server.connected === false ? 'unknown' : 'connected',
    endpoint: server.endpoint,
    toolNames,
    catalogAvailable: catalogCandidatesFor(toolNames, settings).length > 0,
    target: { url: server.url, headers: server.headers },
    catalogUrl: typeof server.catalogUrl === 'string' && server.catalogUrl !== '' ? server.catalogUrl : undefined,
  }
}

/**
 * Ask a federating connection for its upstream servers.
 *
 * Answers are cached per server for `upstreamTtlMs`, including the negative
 * answer: a plain MCP server must not be re-probed on every poll. A probe in
 * flight is shared, so a poll that lands mid-discovery waits for it instead of
 * opening a second connection to the same gateway.
 * @param {Record<string, any>} connection - connected MCP server view.
 * @param {typeof UPSTREAM_DEFAULTS} settings - resolved settings.
 * @param {Map<string, { at: number, value: any, pending: Promise<any> | null }>} cache - upstream cache.
 * @returns {Promise<Record<string, any>>} connection view with `upstream`.
 */
export async function withUpstream(connection, settings, cache) {
  if (connection.state !== 'connected' || !connection.target) return connection
  const key = `${connection.serverName}|${connection.endpoint ?? ''}`
  /** Fold one discovery result into the connection view; null means "not a gateway". */
  const settled = (value) => (value === null || value === undefined ? connection : { ...connection, upstream: value })
  const cached = cache.get(key)
  if (cached?.pending) {
    // A poll that lands mid-discovery waits for the probe already in flight
    // instead of opening a second connection to the same gateway.
    let value = null
    try {
      value = await cached.pending
    } catch {
      value = null
    }
    return settled(value)
  }
  if (cached && Date.now() - cached.at < settings.upstreamTtlMs) {
    // A null value records "this server federates nothing": cache the negative
    // answer too, or a plain server is re-probed on every poll.
    return settled(cached.value)
  }

  const pending = discoverUpstream(connection, settings)
  cache.set(key, { at: Date.now(), value: cached?.value ?? null, pending })
  let value = null
  try {
    value = await pending
  } catch {
    value = null
  }
  cache.set(key, { at: Date.now(), value, pending: null })
  return settled(value)
}

/**
 * Read one server's upstream list, without caching.
 *
 * Three paths, in order: the catalog tool the server exposes, the REST catalog
 * derived from (or configured beside) its endpoint, and — only when that
 * catalog denies access — the namespaced tool names it proxies.
 * @param {Record<string, any>} connection - connected MCP server view.
 * @param {typeof UPSTREAM_DEFAULTS} settings - resolved settings.
 * @returns {Promise<Record<string, any> | null>} upstream block, or null when this is not a gateway.
 */
async function discoverUpstream(connection, settings) {
  const headers = connection.target.headers
  const explicit = typeof connection.catalogUrl === 'string' && connection.catalogUrl !== ''
  const derived = explicit ? connection.catalogUrl : deriveCatalogUrl(connection.target.url)
  let servers = []
  let tool = settings.catalogTool
  let error

  if (connection.catalogAvailable) {
    const probe = await probeMcpHttp({
      url: connection.target.url,
      headers,
      timeoutMs: settings.probeTimeoutMs,
      catalogTool: settings.catalogTool,
      catalogTools: settings.catalogTools,
    })
    servers = Array.isArray(probe.catalog) ? probe.catalog : []
    tool = probe.catalogTool ?? settings.catalogTool
    error = probe.catalogError
  } else if (derived !== undefined) {
    const fetched = await fetchCatalog({ url: derived, headers, timeoutMs: settings.probeTimeoutMs })
    servers = Array.isArray(fetched.rows) ? fetched.rows : []
    tool = 'rest'
    error = fetched.error
    // A denied catalog (401/403) still proves this is a gateway, and its tool
    // names carry the upstream server names. A 404 means there is no catalog
    // here at all, so nothing is derived from a plain server's tool names.
    if (servers.length === 0 && (fetched.status === 401 || fetched.status === 403)) {
      const derivedRows = deriveServersFromTools(connection.toolNames)
      if (derivedRows.length > 0) {
        servers = derivedRows
        tool = 'tool-names'
        error = undefined
      }
    }
    // Nothing answered and nothing could be inferred: this is a plain MCP
    // server, not a gateway. Say nothing rather than render "HTTP 404" under
    // every server that happens to end in /mcp. An explicitly configured
    // catalog URL is the exception — there the operator asked for it.
    if (servers.length === 0 && !explicit) return null
  } else {
    return null
  }

  return {
    generatedAt: new Date().toISOString(),
    tool,
    servers: servers.slice(0, settings.maxUpstreamServers),
    error: error ?? (servers.length ? undefined : 'no upstream servers reported'),
  }
}
