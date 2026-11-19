/**
 * MCP servers — Host half.
 *
 * Gives the Web UI a Settings page that manages MCP servers the way
 * Settings → Models manages model providers: the list lives in this plugin's
 * own configuration, and every entry in it becomes a live
 * `@deepseek-ai/dsh-mcp-client` child, so an added server's tools are callable
 * as `mcp__<serverName>__<tool>` without editing YAML or restarting anything.
 *
 * Three mechanisms carry the design:
 *
 * 1. **Persistence.** The list is a volatile field of this row's Config (see
 *    `./schema.js`), so the Settings service exposes it in the shared settings
 *    mirror and `ctx.configEditor` writes it back into the profile patch with
 *    the user's comments and `!!js` expressions intact. This plugin never
 *    reads or rewrites that file itself.
 *
 * 2. **Mounting.** One `ctx.plugin()` call per enabled server, made from this
 *    plugin's context, so the child fiber is owned by this row: unloading the
 *    row unloads every child. Children are reconciled by diffing the desired
 *    set against the mounted set, so an unrelated edit never bounces a server
 *    that did not change.
 *
 * 3. **Live state.** A `GET /api/mcp-servers` route reports each server's
 *    fiber state and its `mcp__<serverName>__` tool count, plus the MCP rows
 *    that other layers of the profile configure (read-only), so the page can
 *    show what is actually live instead of echoing configuration back.
 *
 * Nothing here imports a Harness package at module scope. A `link:`-ed
 * profile plugin resolves only its own `node_modules`, which has no DSH tree,
 * so the bridge module is resolved lazily at first use, and its absence is a
 * reported condition rather than a load failure.
 *
 * @module
 */

import { accessSync, constants as fsConstants, realpathSync, statSync } from 'node:fs'
import { createRequire } from 'node:module'
import { delimiter, isAbsolute, join } from 'node:path'
import { pathToFileURL } from 'node:url'

import { probeMcpHttp } from './probe.js'
import {
  Config as McpServersConfig,
  isHttpUrl,
  isPlainObject,
  messageOf,
  mountableServers,
  normalizeRows,
  readServersField,
} from './schema.js'

/** Bridge package this plugin mounts one child of per configured server. */
const BRIDGE_PACKAGE = '@deepseek-ai/dsh-mcp-client'

/** Authenticated route the browser half reads. */
const ROUTE_PATH = '/api/mcp-servers'

/** Authenticated route that probes one unsaved draft server. */
const TEST_ROUTE_PATH = '/api/mcp-servers/test'
/** Route that persists the list directly through the configuration editor. */
const SAVE_ROUTE_PATH = '/api/mcp-servers/save'
/** Loader row this plugin owns; the settings namespace and the row id match. */
const ROW_ID = 'mcp-servers'

/** Budget for one draft probe: a browser request, so it stays short. */
const PROBE_TIMEOUT_MS = 5000

/** Tool names echoed back by a successful probe. */
const MAX_TOOLS_REPORTED = 8

/** Fiber lifecycle names, indexed by the Cordis `FiberState` value. */
const FIBER_STATES = ['pending', 'loading', 'active', 'failed', 'disposed', 'unloading']

/** Cordis `FiberState.FAILED`, the only state a retry can improve. */
const FIBER_FAILED = 3

/** How long a failed bridge resolution is remembered before it is retried. */
const BRIDGE_RETRY_MS = 30000

/** Row config schema: one live `servers` list. */
export const Config = McpServersConfig

/**
 * Project a value as a JSON response.
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
 * Name of a Cordis fiber state.
 * @param {unknown} state - numeric state.
 * @returns {string} lifecycle name.
 */
function fiberStateName(state) {
  return typeof state === 'number' ? FIBER_STATES[state] ?? 'unknown' : 'unmounted'
}

/**
 * Whether a resolved module looks like the MCP bridge plugin.
 * @param {unknown} module - resolved module namespace.
 * @returns {boolean} true when it can be mounted as a plugin.
 */
function isBridgeModule(module) {
  return Boolean(module) && typeof module.apply === 'function'
}

/**
 * Adopt an already-loaded bridge runtime from the Loader.
 *
 * A profile that already configures MCP servers has loaded the bridge module,
 * so its `apply` function and Config schema are reachable through any of those
 * rows' fiber runtimes. Using them mounts children into the same module
 * instance, which keeps the bridge's `serverName` reservation registry shared
 * with the YAML-configured rows.
 * @param {any} ctx - plugin context.
 * @returns {any} a plugin-shaped object, or undefined.
 */
function adoptLoadedBridge(ctx) {
  let entries = []
  try {
    const loader = ctx.loader
    if (loader && typeof loader.entries === 'function') entries = [...loader.entries()]
  } catch {
    return undefined
  }
  for (const entry of entries) {
    const name = entry?.options?.name
    if (typeof name !== 'string' || !name.includes('mcp-client')) continue
    const runtime = entry?.fiber?.runtime
    if (!runtime || typeof runtime.callback !== 'function' || !runtime.Config) continue
    return {
      name: typeof runtime.name === 'string' ? runtime.name : 'mcp-client',
      inject: ['tools'],
      Config: runtime.Config,
      apply: runtime.callback,
    }
  }
  return undefined
}

/**
 * Resolve the bridge module, trying the mechanisms a linked profile plugin has.
 * @param {any} ctx - plugin context.
 * @returns {Promise<any>} the bridge module (or a plugin-shaped stand-in).
 */
async function resolveBridge(ctx) {
  const failures = []
  const attempts = [
    ['module resolution', () => import(BRIDGE_PACKAGE)],
    [
      'loader resolution',
      () =>
        ctx.loader && typeof ctx.loader.import === 'function'
          ? ctx.loader.import(BRIDGE_PACKAGE)
          : Promise.reject(new Error('no Loader service')),
    ],
    [
      // Resolve from the running Host installation. A linked profile plugin has
      // no node_modules of its own, and a Loader that exposes no import() has
      // no other way to reach the bridge — but the process that is running this
      // plugin is the installation that owns it.
      'host resolution',
      () => {
        const entry = typeof process.argv[1] === 'string' && process.argv[1] ? process.argv[1] : join(process.cwd(), 'package.json')
        // The launcher is usually a symlink (/usr/local/bin/dsh): resolution
        // must start from the real file, or it looks beside the symlink.
        let anchor = entry
        try {
          anchor = realpathSync(entry)
        } catch {
          // Keep the entry path; the attempt fails and is reported like any other.
        }
        const hostRequire = createRequire(anchor)
        return import(pathToFileURL(hostRequire.resolve(BRIDGE_PACKAGE)).href)
      },
    ],
  ]
  for (const [label, load] of attempts) {
    try {
      const module = await load()
      if (isBridgeModule(module)) return module
      failures.push(`${label}: module does not export apply()`)
    } catch (error) {
      failures.push(`${label}: ${messageOf(error)}`)
    }
  }
  const adopted = adoptLoadedBridge(ctx)
  if (adopted) return adopted
  throw new Error(`${BRIDGE_PACKAGE} is not available (${failures.join('; ')})`)
}

/**
 * Reduce an endpoint to a displayable form without credentials.
 * @param {unknown} value - configured URL.
 * @returns {string} origin and path, or the original text when unparseable.
 */
function displayEndpoint(value) {
  if (typeof value !== 'string' || !value) return ''
  try {
    const url = new URL(value)
    return `${url.origin}${url.pathname}`
  } catch {
    return value
  }
}

/**
 * Count the tools the bridge currently publishes for one server name.
 * @param {any} ctx - plugin context.
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
 * Tool names the bridge currently publishes for one server name, prefix stripped.
 * @param {import('@deepseek-ai/cordis').Context} ctx - plugin context.
 * @param {string} serverName - MCP server namespace.
 * @returns {string[] | undefined} registered tool names, or undefined when the
 * registry is unavailable.
 */
function registeredToolNames(ctx, serverName) {
  try {
    const tools = ctx.tools
    if (!tools || typeof tools.schemas !== 'function') return undefined
    const schemas = tools.schemas()
    if (!Array.isArray(schemas)) return undefined
    const prefix = `mcp__${serverName}__`
    return schemas
      .map((schema) => schema?.name)
      .filter((name) => typeof name === 'string' && name.startsWith(prefix))
      .map((name) => name.slice(prefix.length))
      .sort()
  } catch {
    return undefined
  }
}

/**
 * Read the MCP rows other layers of the profile configure. These are not
 * managed by this page: they belong to the profile patch, a bundle, or an
 * agent preset, and their configuration is not this plugin's to rewrite.
 * Request headers never leave the Host.
 * @param {any} ctx - plugin context.
 * @returns {Array<Record<string, any>>} external rows.
 */
function externalRows(ctx) {
  let entries = []
  try {
    const loader = ctx.loader
    if (loader && typeof loader.entries === 'function') entries = [...loader.entries()]
  } catch {
    return []
  }
  const rows = []
  for (const entry of entries) {
    if (!entry || entry.options?.group) continue
    let config
    try {
      const effective = entry.fiber?.effectiveConfig
      if (effective && typeof effective === 'object') config = effective
      else if (entry.options?.config && typeof entry.options.config === 'object') config = entry.options.config
    } catch {
      config = undefined
    }
    if (!config || typeof config.serverName !== 'string' || typeof config.transport !== 'string') continue
    const transport = config.transport
    const row = {
      id: String(entry.options?.id ?? config.serverName),
      serverName: config.serverName,
      transport,
      disabled: entry.disabled === true,
      fiberState: fiberStateName(entry.fiber?.state),
      toolCount: registeredToolCount(ctx, config.serverName),
      // A server configured in the profile patch lists its tools here too: the
      // page is where an operator looks, whichever layer declared the server.
      tools: registeredToolNames(ctx, config.serverName),
    }
    if (transport === 'streamable-http') row.endpoint = displayEndpoint(config.url)
    else if (typeof config.command === 'string') row.command = config.command.split(/\s+/)[0]
    rows.push(row)
  }
  return rows
}

/**
 * Whether a stdio command names an executable this Host can start.
 *
 * A stdio server cannot be verified without spawning it, which a browser
 * request must not do unbounded, so the cheap and common failure — a command
 * that is not installed or not on `PATH` — is answered here instead.
 * @param {unknown} command - configured command.
 * @returns {'found' | 'missing'} whether the program is reachable.
 */
function commandState(command) {
  const text = typeof command === 'string' ? command.trim() : ''
  if (!text) return 'missing'
  const candidates = isAbsolute(text)
    ? [text]
    : String(process.env.PATH ?? '')
        .split(delimiter)
        .filter(Boolean)
        .map((directory) => join(directory, text))
  for (const candidate of candidates) {
    try {
      accessSync(candidate, fsConstants.X_OK)
      return 'found'
    } catch {
      // Keep looking: the next PATH entry may hold the program.
    }
  }
  return 'missing'
}

/**
 * Whether a configured working directory is usable.
 * @param {unknown} cwd - configured working directory.
 * @returns {'unset' | 'ok' | 'missing' | 'not-a-directory'} state.
 */
function cwdState(cwd) {
  const text = typeof cwd === 'string' ? cwd.trim() : ''
  if (!text) return 'unset'
  try {
    return statSync(text).isDirectory() ? 'ok' : 'not-a-directory'
  } catch {
    return 'missing'
  }
}

/**
 * Probe one draft row without mounting it.
 *
 * This is the page's *Test connection* action: it answers "is this endpoint
 * right?" before a row is saved, so a typo is caught in the form instead of
 * showing up later as a row stuck in `Failed`.
 * @param {Record<string, any>} draft - draft row from the editor.
 * @returns {Promise<Record<string, any>>} probe result for the browser.
 */
async function probeDraft(draft) {
  const source = isPlainObject(draft) ? draft : {}
  const transport = source.transport === 'stdio' ? 'stdio' : 'streamable-http'
  if (transport === 'stdio') {
    // A status read must not spawn a process, so answer the cheap question —
    // is the command on PATH? — and say plainly that the server was not started.
    return {
      ok: false,
      transport,
      stdio: true,
      commandFound: commandState(source.command) === 'found',
      cwd: cwdState(source.cwd),
      note: 'stdio servers are not started for a connection test',
    }
  }
  const url = typeof source.url === 'string' ? source.url.trim() : ''
  if (!isHttpUrl(url)) return { ok: false, transport, error: 'enter an absolute http or https URL' }
  const result = await probeMcpHttp({
    url,
    headers: isPlainObject(source.headers) ? source.headers : undefined,
    timeoutMs: PROBE_TIMEOUT_MS,
  })
  const toolNames = Array.isArray(result.toolNames) ? result.toolNames : []
  return {
    ok: result.ok === true,
    transport,
    latencyMs: result.latencyMs,
    protocolVersion: result.protocolVersion,
    serverInfo: result.serverInfo,
    capabilities: result.capabilities,
    toolCount: result.ok ? toolNames.length : undefined,
    tools: result.ok ? toolNames.slice(0, MAX_TOOLS_REPORTED) : undefined,
    error: result.ok ? undefined : result.error,
    timedOut: result.timedOut === true ? true : undefined,
  }
}

/**
 * Register the MCP settings page's Host half.
 * @param {any} ctx - plugin context.
 * @param {unknown} config - row config.
 */
export function apply(ctx, config) {
  /** Mounted children, keyed by the bridge's `serverName`. */
  const mounted = new Map()
  /** Resolved bridge module, or undefined while unresolved. */
  let bridge
  /** Last bridge resolution failure and when it happened. */
  let bridgeFailure
  /** Settlement of every child disposal started so far, in order. */
  let disposing = Promise.resolve()
  /** In-flight reconcile, so two readers cannot mount the same server twice. */
  let reconciling = false
  /** A reconcile request that arrived while one was running. */
  let reconcileAgain = false

  /** The live configured list, whatever shape the resolved field has. */
  const configuredRows = () => {
    try {
      return readServersField(config && typeof config === 'object' ? config.servers : undefined)
    } catch {
      return []
    }
  }

  /**
   * Log a warning without ever throwing out of `apply`.
   * @param {string} text - message.
   * @param {unknown} [error] - optional cause.
   */
  const warn = (text, error) => {
    try {
      const logger = ctx.logger
      logger?.warn?.(`mcp-servers: ${text}`)
      if (error !== undefined) logger?.warn?.(error)
    } catch {
      // Diagnostics must never break the plugin.
    }
  }

  /**
   * Resolve the bridge, remembering a recent failure instead of retrying on
   * every read.
   * @returns {Promise<any | undefined>} the bridge, or undefined when unavailable.
   */
  const ensureBridge = async () => {
    if (bridge) return bridge
    if (bridgeFailure && Date.now() - bridgeFailure.at < BRIDGE_RETRY_MS) return undefined
    try {
      bridge = await resolveBridge(ctx)
      bridgeFailure = undefined
      return bridge
    } catch (error) {
      bridgeFailure = { at: Date.now(), error }
      warn('MCP bridge unavailable', error)
      return undefined
    }
  }

  /**
   * Mount one child bridge for a configured server.
   * @param {{ row: Record<string, any>, facts: string, config: Record<string, any> }} server - mountable server.
   * @returns {void}
   */
  const mount = (server) => {
    const serverName = server.row.serverName
    const record = { facts: server.facts, fiber: undefined, error: undefined }
    mounted.set(serverName, record)
    try {
      const fiber = ctx.plugin(bridge, server.config)
      record.fiber = fiber
      Promise.resolve(fiber).then(
        () => {
          if (mounted.get(serverName) === record) record.error = undefined
        },
        (error) => {
          if (mounted.get(serverName) === record) record.error = messageOf(error)
          warn(`server "${serverName}" failed to start`, error)
        },
      )
    } catch (error) {
      record.error = messageOf(error)
      warn(`server "${serverName}" could not be mounted`, error)
    }
  }

  /**
   * Stop one mounted child.
   *
   * Disposal is queued and awaited before the next mount: the bridge releases
   * its `serverName` reservation during disposal, so replacing a server in the
   * same pass would otherwise collide with the outgoing child.
   * @param {string} serverName - bridge namespace.
   * @returns {void}
   */
  const unmount = (serverName) => {
    const record = mounted.get(serverName)
    if (!record) return
    mounted.delete(serverName)
    const fiber = record.fiber
    if (!fiber || typeof fiber.dispose !== 'function') return
    try {
      disposing = disposing
        .then(() => Promise.resolve().then(() => fiber.dispose()))
        .then(
          () => undefined,
          (error) => warn(`server "${serverName}" did not stop cleanly`, error),
        )
    } catch (error) {
      warn(`server "${serverName}" did not stop cleanly`, error)
    }
  }

  /**
   * Bring the mounted children in line with the configured list.
   * @returns {Promise<void>} settlement; never rejects.
   */
  const reconcile = async () => {
    if (reconciling) {
      reconcileAgain = true
      return
    }
    reconciling = true
    try {
      const desired = mountableServers(configuredRows())
      const wanted = new Map(desired.servers.map((server) => [server.row.serverName, server]))
      for (const [serverName, record] of [...mounted]) {
        const next = wanted.get(serverName)
        if (!next || next.facts !== record.facts) unmount(serverName)
      }
      if (desired.servers.length === 0) {
        // Nothing to mount, but the bridge is still resolved so the page can
        // report the truth instead of "not resolved yet" on an empty list.
        void ensureBridge()
        return
      }
      // A replaced server may reuse the same namespace, so wait for the
      // outgoing child to release it before mounting the replacement.
      await disposing
      const module = await ensureBridge()
      if (!module) return
      for (const server of desired.servers) {
        if (mounted.has(server.row.serverName)) continue
        mount(server)
      }
    } catch (error) {
      warn('reconcile failed', error)
    } finally {
      reconciling = false
      if (reconcileAgain) {
        reconcileAgain = false
        void reconcile()
      }
    }
  }

  /**
   * Drop the memoized bridge failure and every child that is not running, so
   * the next reconcile tries them again.
   * @returns {void}
   */
  const resetFailures = () => {
    bridgeFailure = undefined
    for (const [serverName, record] of [...mounted]) {
      const fiber = record.fiber
      if (!fiber || fiber.state === FIBER_FAILED) unmount(serverName)
    }
  }

  /**
   * Build the snapshot served to the browser.
   * @returns {Record<string, any>} snapshot.
   */
  const snapshot = () => {
    const desired = mountableServers(configuredRows())
    const servers = desired.servers.map((server) => {
      const serverName = server.row.serverName
      const record = mounted.get(serverName)
      const state = fiberStateName(record?.fiber?.state)
      const failed = Boolean(record?.error) || state === 'failed'
      return {
        id: server.id,
        serverName,
        transport: server.row.transport,
        endpoint: server.row.transport === 'streamable-http' ? server.row.url : server.row.command,
        state: failed ? 'failed' : state === 'active' ? 'ready' : record?.fiber ? 'starting' : 'pending',
        fiberState: state,
        toolCount: registeredToolCount(ctx, serverName),
        // The names, so the page can list them; the count alone left the
        // operator guessing what a server actually offers.
        tools: registeredToolNames(ctx, serverName),
        error: record?.error,
      }
    })
    return {
      generatedAt: new Date().toISOString(),
      writable: true,
      bridge: bridge
        ? { available: true }
        : bridgeFailure
          ? { available: false, error: messageOf(bridgeFailure.error) }
          : desired.servers.length === 0
            // No servers means nothing needs the bridge; saying it is
            // unavailable made a fresh install look broken.
            ? { available: true }
            : { available: false, error: 'the MCP bridge has not been resolved yet' },
      servers,
      problems: desired.problems,
      external: externalRows(ctx),
    }
  }

  // Keep the mounted set in step with live configuration changes. The event
  // fires on this row's fiber only.
  ctx.on('loader/volatile-update', () => {
    void reconcile()
  })

  // This plugin ships its own page, so the generic generated one would only
  // duplicate it with a raw array editor.
  try {
    ctx.inject(['settings'], (child) => {
      child.effect(() => child.settings.configure({ auto: false }, ctx.fiber))
    })
  } catch (error) {
    warn('could not disable the generated settings page', error)
  }

  // The browser half reads live state from this authenticated route. A profile
  // without the Web connection simply serves no route.
  try {
    ctx.inject(['connection'], (child) => {
      const route = {
        path: ROUTE_PATH,
        methods: ['GET'],
        requestBody: 'buffered',
        fetch: async (request) => {
          try {
            const url = new URL(request.url)
            if (url.searchParams.get('retry') === '1') {
              resetFailures()
              await reconcile()
            } else {
              void reconcile()
            }
            return jsonResponse(snapshot())
          } catch (error) {
            return jsonResponse(
              { generatedAt: new Date().toISOString(), error: messageOf(error), servers: [], problems: [], external: [] },
              500,
            )
          }
        },
      }
      child.effect(() => child.connection.fetch.register(route), 'mcp-servers: status route')

      const testRoute = {
        path: TEST_ROUTE_PATH,
        methods: ['POST'],
        requestBody: 'buffered',
        fetch: async (request) => {
          let draft
          try {
            draft = await request.json()
          } catch {
            return jsonResponse({ ok: false, error: 'expected a JSON body' }, 400)
          }
          try {
            return jsonResponse(await probeDraft(draft))
          } catch (error) {
            return jsonResponse({ ok: false, error: messageOf(error) }, 500)
          }
        },
      }
      child.effect(() => child.connection.fetch.register(testRoute), 'mcp-servers: test route')

      // Direct persistence, used when the settings form's write is refused.
      // The settings service reports a refusal as a bare false, so the page
      // cannot tell the operator why; this route runs the same sanctioned
      // editor call and returns the reason instead.
      const saveRoute = {
        path: SAVE_ROUTE_PATH,
        methods: ['POST'],
        requestBody: 'buffered',
        fetch: async (request) => {
          let body
          try {
            body = await request.json()
          } catch {
            return jsonResponse({ ok: false, error: 'expected a JSON body' }, 400)
          }
          const rows = Array.isArray(body && body.servers) ? body.servers : undefined
          if (rows === undefined) return jsonResponse({ ok: false, error: 'servers must be an array' }, 400)
          const editor = ctx.get('configEditor')
          if (!editor || typeof editor.edit !== 'function') {
            return jsonResponse({ ok: false, error: 'the configuration editor is not mounted' }, 503)
          }
          let entry
          for (const candidate of editor.entries()) {
            if (candidate && candidate.options && candidate.options.id === ROW_ID) {
              entry = candidate
              break
            }
          }
          if (entry === undefined) {
            return jsonResponse({ ok: false, error: 'no configuration row with id ' + ROW_ID }, 404)
          }
          try {
            await editor.edit(entry, (current) => ({ ...current, servers: rows }))
            void reconcile()
            return jsonResponse({ ok: true })
          } catch (error) {
            return jsonResponse({ ok: false, error: messageOf(error) }, 400)
          }
        },
      }
      child.effect(() => child.connection.fetch.register(saveRoute), 'mcp-servers: save route')
    })
  } catch (error) {
    warn('could not register the status route', error)
  }

  // Mount whatever the profile already configured. Deliberately not awaited:
  // a slow or unreachable MCP server must not delay this row's activation.
  void reconcile()
  // Resolve the bridge once at startup, so the page can report a real state
  // before the first server is configured.
  void ensureBridge()
}
