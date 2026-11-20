/**
 * MCP servers — configuration schema and row normalization.
 *
 * The page edits one list, `servers`, and the Host turns each entry into a
 * `@deepseek-ai/dsh-mcp-client` child. Two things in this module are load
 * bearing and both are deliberate:
 *
 * 1. `Config` is a **StandardSchemaV1** object, not a schemastery schema. A
 *    `link:`-ed profile plugin cannot `import` a Harness package (its own
 *    `node_modules` has no DSH tree), so it cannot build a schemastery schema
 *    at module scope. Cordis only requires `~standard.validate`; the Settings
 *    service additionally reads `type` / `meta` / `dict` / `toJSON()`, so this
 *    object provides exactly that surface. `toJSON()` emits the ref-graph
 *    schemastery's own `toJSON` emits, which is what `plainSchema()` in
 *    `@deepseek-ai/dsh-settings` reconstructs with `new Schema(json)`.
 *
 * 2. `validate()` returns the `servers` list as a **volatile reference** using
 *    cosmokit's registered symbol `Symbol.for('cosmokit.volatile.write')`. That
 *    is the shared protocol `isVolatile` / `volatileEntries` / `updateVolatile`
 *    use, so a Settings write commits into the running fiber and dispatches
 *    `loader/volatile-update` instead of remounting the plugin. The symbol is
 *    global, so no cosmokit import is needed.
 *
 * Normalization is permissive on purpose: a malformed stored row is preserved
 * in configuration and reported as a problem, never thrown. A rejected
 * validation would fail the whole row and take the page down with it, which is
 * the one outcome this plugin must not produce.
 *
 * @module
 */

/* ------------------------------------------------------------------ *
 * Public constants
 * ------------------------------------------------------------------ */

/** Valid `serverName`: the bridge's own pattern, `[A-Za-z0-9_-]{1,32}`. */
export const SERVER_NAME_PATTERN = /^[A-Za-z0-9_-]{1,32}$/

/** Transports the MCP bridge speaks. */
export const TRANSPORTS = ['streamable-http', 'stdio']

/** Per-tool-call timeout default, matching the bridge's own default. */
export const DEFAULT_TOOL_CALL_TIMEOUT_MS = 60000

/**
 * Cap on the server-instructions block the bridge accepts.
 *
 * The bridge defaults to 32,768 bytes, which silently fails any server that
 * ships a large instruction block (the HPE networking MCP sends ~121 KB). A
 * server added through this page has no way to know that, so rows managed here
 * start at 262,144 — the value the profile's own MCP rows use — and the field
 * is exposed in the editor for a server that needs less.
 */
export const DEFAULT_MAX_INSTRUCTION_BYTES = 262144

/** Largest value `setTimeout` and `AbortSignal` accept, so the bridge cap. */
const MAX_TOOL_CALL_TIMEOUT_MS = 2147483647

/** Field names this plugin owns on a row; everything else is preserved. */
const ROW_FIELDS = [
  'id',
  'serverName',
  'transport',
  'url',
  'headers',
  'command',
  'args',
  'cwd',
  'env',
  'toolCallTimeoutMs',
  'failOnStartupError',
  'maxInstructionBytes',
  'catalogUrl',
  'enabled',
]

/** Cosmokit's registered symbol for the shared volatile-reference protocol. */
const WRITE = Symbol.for('cosmokit.volatile.write')

/* ------------------------------------------------------------------ *
 * JSON helpers
 * ------------------------------------------------------------------ */

/**
 * Whether a value is a plain data object.
 * @param {unknown} value - candidate.
 * @returns {boolean} true for a non-array object with Object.prototype.
 */
export function isPlainObject(value) {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return false
  const proto = Object.getPrototypeOf(value)
  return proto === Object.prototype || proto === null
}

/**
 * Clone a JSON-shaped value, dropping anything JSON cannot carry.
 * @param {unknown} value - candidate.
 * @returns {unknown} detached clone, or undefined when not representable.
 */
export function deepCloneJson(value) {
  if (value === undefined) return undefined
  try {
    const text = JSON.stringify(value)
    return text === undefined ? undefined : JSON.parse(text)
  } catch {
    return undefined
  }
}

/**
 * Structural equality for JSON-shaped values.
 * @param {unknown} left - first value.
 * @param {unknown} right - second value.
 * @returns {boolean} true when both serialize identically.
 */
export function deepEqualJson(left, right) {
  if (left === right) return true
  if (typeof left !== typeof right) return false
  if (left === null || right === null || typeof left !== 'object') return false
  if (Array.isArray(left) !== Array.isArray(right)) return false
  const leftKeys = Object.keys(left)
  const rightKeys = Object.keys(right)
  if (leftKeys.length !== rightKeys.length) return false
  for (const key of leftKeys) {
    if (!Object.hasOwn(right, key)) return false
    if (!deepEqualJson(left[key], right[key])) return false
  }
  return true
}

/**
 * Coerce a thrown value into one short line of text.
 *
 * The bridge reports a failed start as a wrapper error ("initial connection or
 * tool synchronization failed") and keeps the actionable reason — an
 * instruction-cap overflow, a refused connection — in `cause`. Walking the
 * chain is what makes the page show a reason a user can act on.
 * @param {unknown} error - thrown value.
 * @returns {string} readable message, wrapper plus causes.
 */
export function messageOf(error) {
  if (error === undefined || error === null) return 'unknown error'
  const parts = []
  let current = error
  for (let depth = 0; depth < 4 && current !== undefined && current !== null; depth += 1) {
    let text
    if (typeof current === 'string') text = current
    else if (typeof current.message === 'string' && current.message) text = current.message
    else {
      try {
        text = String(current)
      } catch {
        text = ''
      }
    }
    if (text && !parts.includes(text)) parts.push(text)
    current = typeof current === 'object' ? current.cause : undefined
  }
  return parts.length > 0 ? parts.join(': ') : 'unknown error'
}

/* ------------------------------------------------------------------ *
 * Volatile references (cosmokit-compatible, no import needed)
 * ------------------------------------------------------------------ */

/**
 * Deep-freeze a JSON-shaped value so a reference holder cannot mutate it.
 * @param {unknown} value - detached value.
 * @returns {unknown} the same value, frozen.
 */
function freezeDeep(value) {
  if (value === null || typeof value !== 'object') return value
  Object.freeze(value)
  for (const child of Array.isArray(value) ? value : Object.values(value)) freezeDeep(child)
  return value
}

/**
 * Build a volatile reference: an immutable snapshot plus the protocol write
 * hook the framework uses to commit a newer snapshot in place.
 * @param {unknown} value - initial JSON-shaped value.
 * @returns {Readonly<Record<string | symbol, any>>} the reference.
 */
export function createVolatileRef(value) {
  let current = freezeDeep(deepCloneJson(value))
  return Object.freeze({
    get: () => current,
    [WRITE]: (next) => {
      current = freezeDeep(next)
    },
  })
}

/**
 * Whether a value implements the volatile-reference protocol.
 * @param {unknown} value - candidate.
 * @returns {boolean} true for a reference created by cosmokit or by this module.
 */
export function isVolatileRef(value) {
  return typeof value === 'object' && value !== null && WRITE in value
}

/**
 * Read the live list from a resolved config field, whichever shape it has.
 * A volatile field resolves to a reference; a plain config resolves to the
 * value itself.
 * @param {unknown} field - resolved `config.servers`.
 * @returns {unknown[]} the configured rows.
 */
export function readServersField(field) {
  const value = isVolatileRef(field) ? field.get() : field
  return Array.isArray(value) ? value : []
}

/* ------------------------------------------------------------------ *
 * Row normalization
 * ------------------------------------------------------------------ */

/**
 * Turn a server name into a stable row id.
 * @param {string} serverName - configured server name.
 * @returns {string} lowercase slug, never empty.
 */
export function slugOf(serverName) {
  const slug = String(serverName ?? '')
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 32)
  return slug || 'server'
}

/**
 * Allocate a row id that is not already taken.
 * @param {string} serverName - configured server name.
 * @param {Set<string>} taken - ids in use; the new id is added to it.
 * @returns {string} unique id.
 */
export function allocateId(serverName, taken) {
  const base = slugOf(serverName)
  let candidate = base
  let suffix = 2
  while (taken.has(candidate)) {
    candidate = `${base}-${suffix}`
    suffix += 1
  }
  taken.add(candidate)
  return candidate
}

/**
 * Normalize a string dictionary (headers, env) into string values.
 * @param {unknown} value - configured dictionary.
 * @returns {Record<string, string>} normalized dictionary.
 */
function normalizeStringDict(value) {
  if (!isPlainObject(value)) return {}
  const result = {}
  for (const [key, entry] of Object.entries(value)) {
    const name = String(key).trim()
    if (!name) continue
    if (entry === undefined || entry === null) continue
    result[name] = String(entry)
  }
  return result
}

/**
 * Normalize one configured row, preserving unknown fields.
 * @param {unknown} raw - configured row.
 * @param {number} index - position, used for a fallback id.
 * @param {Set<string>} taken - ids already allocated.
 * @returns {Record<string, any>} normalized row.
 */
export function normalizeRow(raw, index, taken) {
  const source = isPlainObject(raw) ? raw : {}
  const serverName = typeof source.serverName === 'string' ? source.serverName.trim() : ''
  const rawId = typeof source.id === 'string' ? source.id.trim() : ''
  let id = rawId
  if (!id) {
    id = serverName ? allocateId(serverName, taken) : allocateId(`row-${index + 1}`, taken)
  } else if (taken.has(id)) {
    id = allocateId(id, taken)
  } else {
    taken.add(id)
  }
  const transport = TRANSPORTS.includes(source.transport) ? source.transport : 'streamable-http'
  const timeout = Number(source.toolCallTimeoutMs)
  const instructionCap = Number(source.maxInstructionBytes)
  const row = {
    ...source,
    id,
    serverName,
    transport,
    enabled: source.enabled !== false,
    failOnStartupError: source.failOnStartupError === true,
    maxInstructionBytes:
      Number.isFinite(instructionCap) && instructionCap >= 1
        ? Math.floor(instructionCap)
        : DEFAULT_MAX_INSTRUCTION_BYTES,
    toolCallTimeoutMs:
      Number.isFinite(timeout) && timeout > 0
        ? Math.min(MAX_TOOL_CALL_TIMEOUT_MS, Math.floor(timeout))
        : DEFAULT_TOOL_CALL_TIMEOUT_MS,
  }
  if (transport === 'streamable-http') {
    row.url = typeof source.url === 'string' ? source.url.trim() : ''
    // Optional REST catalog endpoint, for a gateway that publishes its upstream
    // server list over HTTP instead of as an MCP tool (LiteLLM does).
    row.catalogUrl = typeof source.catalogUrl === 'string' ? source.catalogUrl.trim() : ''
    row.headers = normalizeStringDict(source.headers)
    row.command = ''
    row.args = []
    row.cwd = ''
    row.env = {}
  } else {
    row.command = typeof source.command === 'string' ? source.command.trim() : ''
    row.args = Array.isArray(source.args) ? source.args.map((entry) => String(entry)) : []
    row.cwd = typeof source.cwd === 'string' ? source.cwd : ''
    row.env = normalizeStringDict(source.env)
    row.url = ''
    row.headers = {}
  }
  return row
}

/**
 * Normalize the whole configured list.
 * @param {unknown} rows - configured rows.
 * @returns {Array<Record<string, any>>} normalized rows with unique ids.
 */
export function normalizeRows(rows) {
  const list = Array.isArray(rows) ? rows : []
  const taken = new Set()
  return list.map((row, index) => normalizeRow(row, index, taken))
}

/* ------------------------------------------------------------------ *
 * Mountable view
 * ------------------------------------------------------------------ */

/**
 * Whether a value is an absolute http(s) URL.
 * @param {unknown} value - candidate.
 * @returns {boolean} true for a usable endpoint.
 */
export function isHttpUrl(value) {
  if (typeof value !== 'string' || !value) return false
  let parsed
  try {
    parsed = new URL(value)
  } catch {
    return false
  }
  return parsed.protocol === 'http:' || parsed.protocol === 'https:'
}

/**
 * The facts that decide whether a mounted child still matches its row. Any
 * change here needs a fresh child; a change to `enabled`, `id`, or an
 * unrelated field does not.
 * @param {Record<string, any>} row - normalized row.
 * @returns {string} stable serialization.
 */
export function factsOf(row) {
  return JSON.stringify([
    row.serverName,
    row.transport,
    row.url ?? '',
    row.command ?? '',
    row.args ?? [],
    row.cwd ?? '',
    row.headers ?? {},
    row.env ?? {},
    row.toolCallTimeoutMs,
    row.failOnStartupError === true,
    row.maxInstructionBytes,
  ])
}

/**
 * Build the bridge config for one row.
 * @param {Record<string, any>} row - normalized row.
 * @returns {Record<string, any>} `@deepseek-ai/dsh-mcp-client` config.
 */
export function bridgeConfigOf(row) {
  const base = {
    serverName: row.serverName,
    toolCallTimeoutMs: row.toolCallTimeoutMs,
    failOnStartupError: row.failOnStartupError === true,
    maxInstructionBytes: row.maxInstructionBytes,
  }
  if (row.transport === 'stdio') {
    const config = { ...base, transport: 'stdio', command: row.command, args: row.args ?? [] }
    if (row.cwd) config.cwd = row.cwd
    if (row.env && Object.keys(row.env).length > 0) config.env = row.env
    return config
  }
  const config = { ...base, transport: 'streamable-http', url: row.url }
  if (row.headers && Object.keys(row.headers).length > 0) config.headers = row.headers
  return config
}

/**
 * Split configured rows into the set to mount and the rows that cannot be
 * mounted, each with the reason a user can act on.
 * @param {unknown} rows - normalized rows (or raw config).
 * @returns {{ servers: Array<{ id: string, row: Record<string, any>, facts: string, config: Record<string, any> }>, problems: Array<{ id: string, serverName: string, message: string }> }} mountable view.
 */
export function mountableServers(rows) {
  const servers = []
  const problems = []
  const names = new Set()
  for (const row of normalizeRows(rows)) {
    if (row.enabled === false) continue
    if (!SERVER_NAME_PATTERN.test(row.serverName)) {
      problems.push({
        id: row.id,
        serverName: row.serverName,
        message: row.serverName
          ? 'serverName must be 1-32 characters from A-Z, a-z, 0-9, "_" or "-"'
          : 'serverName is required',
      })
      continue
    }
    if (names.has(row.serverName)) {
      problems.push({
        id: row.id,
        serverName: row.serverName,
        message: 'another enabled server already uses this serverName',
      })
      continue
    }
    if (row.transport === 'streamable-http' && !isHttpUrl(row.url)) {
      problems.push({ id: row.id, serverName: row.serverName, message: 'url must be an absolute http(s) URL' })
      continue
    }
    if (row.transport === 'stdio' && !row.command) {
      problems.push({ id: row.id, serverName: row.serverName, message: 'command is required for a stdio server' })
      continue
    }
    names.add(row.serverName)
    servers.push({ id: row.id, row, facts: factsOf(row), config: bridgeConfigOf(row) })
  }
  return { servers, problems }
}

/* ------------------------------------------------------------------ *
 * Config schema
 * ------------------------------------------------------------------ */

/**
 * Serialize one schema node into schemastery's `{ uid, refs }` envelope.
 * `plainSchema()` in the Settings service reconstructs it with
 * `new Schema(json)`, so the shape must match schemastery's own `toJSON()`.
 * @param {Record<string, any>} node - schema node.
 * @returns {{ uid: number, refs: Record<string, any> }} serialized schema.
 */
export function schemaToJSON(node) {
  const refs = {}
  let counter = 0
  const visit = (current) => {
    if (!current || typeof current !== 'object') return undefined
    const uid = (counter += 1)
    const descriptor = { type: current.type }
    if (current.meta !== undefined) descriptor.meta = deepCloneJson(current.meta) ?? {}
    refs[uid] = descriptor
    if (current.inner) descriptor.inner = visit(current.inner)
    if (current.dict) {
      descriptor.dict = {}
      for (const [key, child] of Object.entries(current.dict)) descriptor.dict[key] = visit(child)
    }
    if (Array.isArray(current.list)) descriptor.list = current.list.map((child) => visit(child))
    if (current.sKey) descriptor.sKey = visit(current.sKey)
    return uid
  }
  const uid = visit(node)
  return { uid, refs }
}

/**
 * Build one schema node. Every node carries its own `toJSON()`, because
 * `plainSchema()` in the Settings service calls it on whichever node it finds
 * volatile — not only on the root.
 * @param {{ type: string, meta?: Record<string, any>, inner?: any, dict?: Record<string, any>, list?: any[] }} spec - node shape.
 * @returns {Record<string, any>} frozen schema node.
 */
function schemaNode(spec) {
  const node = { type: spec.type }
  if (spec.meta !== undefined) node.meta = Object.freeze({ ...spec.meta })
  if (spec.inner) node.inner = spec.inner
  if (spec.dict) node.dict = Object.freeze({ ...spec.dict })
  if (spec.list) node.list = Object.freeze([...spec.list])
  node.toJSON = () => schemaToJSON(node)
  return Object.freeze(node)
}

/**
 * The `servers` field schema: one volatile array at a fixed object path, which
 * is exactly what a live-editable field has to be. Elements are `any`, so a
 * future row field never invalidates a stored document.
 */
export const SERVERS_SCHEMA = schemaNode({
  type: 'array',
  meta: { default: [], volatile: true },
  inner: schemaNode({ type: 'any', meta: {} }),
})

/**
 * Validate and normalize one raw config into the running value.
 * @param {unknown} raw - raw row config.
 * @returns {Record<string, any>} `{ servers: <volatile reference> }`.
 */
export function resolveConfigValue(raw) {
  const source = isPlainObject(raw) ? raw : {}
  return { servers: createVolatileRef(normalizeRows(readServersField(source.servers))) }
}

/**
 * The plugin's Config: a StandardSchemaV1 whose one live field is `servers`.
 * Cordis validates through `~standard`; the Settings service additionally
 * reads `type`, `meta`, `dict`, and `toJSON()`.
 */
export const Config = Object.freeze({
  type: 'object',
  meta: Object.freeze({}),
  dict: Object.freeze({ servers: SERVERS_SCHEMA }),
  toJSON: () => schemaToJSON(Config),
  '~standard': Object.freeze({
    version: 1,
    vendor: 'schemastery',
    validate(value) {
      try {
        return { value: resolveConfigValue(value) }
      } catch (error) {
        return { issues: [{ message: messageOf(error), path: [] }] }
      }
    },
  }),
})

/** Field names this plugin owns; exported for documentation and tests. */
export const SERVER_ROW_FIELDS = Object.freeze([...ROW_FIELDS])
