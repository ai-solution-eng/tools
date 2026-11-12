/**
 * Minimal Model Context Protocol (MCP) probe over Streamable HTTP.
 *
 * The Harness MCP client bridge (`@deepseek-ai/dsh-mcp-client`) owns the real
 * connections but publishes no status surface, so this module speaks just
 * enough of the protocol to answer one question per configured server: is it
 * answering MCP right now, and what does it expose?
 *
 * It performs `initialize` -> `notifications/initialized` -> `tools/list` and,
 * when asked, one `tools/call` for a catalog tool. Both plain JSON and
 * `text/event-stream` responses are accepted, and the server-assigned
 * `Mcp-Session-Id` is carried across the requests of one probe. The session is
 * terminated with a best-effort `DELETE`.
 *
 * Deliberately dependency-free: it imports nothing but globals (`fetch`,
 * `AbortController`, `URL`), so the plugin package stays installable without a
 * node_modules tree of its own.
 *
 * @module
 */

/** Protocol revision the probe asks for first. */
const PRIMARY_PROTOCOL_VERSION = '2026-07-28'
/** Older revisions retried when a server rejects the primary one. */
const FALLBACK_PROTOCOL_VERSIONS = ['2025-11-25', '2025-06-18', '2025-03-26']
/** Upper bound on one response body, so a broken server cannot exhaust memory. */
const MAX_BODY_BYTES = 4 * 1024 * 1024
/** Upper bound on an error excerpt shown in the panel. */
const MAX_ERROR_CHARS = 240

/**
 * Coerce anything thrown into one short line of text.
 * @param {unknown} error - thrown value.
 * @returns {string} readable message.
 */
export function messageOf(error) {
  if (error === undefined || error === null) return 'unknown error'
  if (typeof error === 'string') return truncate(error)
  if (error instanceof Error && typeof error.message === 'string') return truncate(error.message)
  if (typeof error === 'object') {
    const message = error.message ?? error.error ?? error.detail
    if (typeof message === 'string') return truncate(message)
    try {
      return truncate(JSON.stringify(error))
    } catch {
      return truncate(String(error))
    }
  }
  return truncate(String(error))
}

/**
 * Clip one diagnostic to a panel-sized excerpt.
 * @param {string} value - raw text.
 * @returns {string} clipped text.
 */
function truncate(value) {
  const collapsed = value.trim().replace(/\s+/g, ' ')
  return collapsed.length > MAX_ERROR_CHARS ? `${collapsed.slice(0, MAX_ERROR_CHARS)}…` : collapsed
}

/**
 * Build a case-insensitive header table from a configured header object.
 * Non-string values are ignored: the transport only sends strings.
 * @param {unknown} source - configured headers.
 * @returns {Map<string, { name: string, value: string }>} keyed by lowercase name.
 */
function headerTable(source) {
  const table = new Map()
  if (!source || typeof source !== 'object') return table
  for (const [name, value] of Object.entries(source)) {
    if (typeof value === 'string') table.set(name.toLowerCase(), { name, value })
    else if (typeof value === 'number' || typeof value === 'boolean') {
      table.set(name.toLowerCase(), { name, value: String(value) })
    }
  }
  return table
}

/**
 * Merge required request headers over the configured ones, case-insensitively,
 * so a configured `Content-Type` can never duplicate the JSON body type.
 * @param {unknown} configured - configured headers.
 * @param {Record<string, string>} required - headers this probe owns.
 * @returns {Record<string, string>} final request headers.
 */
function requestHeaders(configured, required) {
  const table = headerTable(configured)
  for (const [name, value] of Object.entries(required)) table.set(name.toLowerCase(), { name, value })
  const headers = {}
  for (const { name, value } of table.values()) headers[name] = value
  return headers
}

/**
 * Read a response body with a byte cap.
 * @param {Response} response - fetch response.
 * @returns {Promise<string>} decoded text, possibly truncated at the cap.
 */
async function readBody(response) {
  const stream = response.body
  if (!stream || typeof stream.getReader !== 'function') {
    const text = await response.text()
    return text.length > MAX_BODY_BYTES ? text.slice(0, MAX_BODY_BYTES) : text
  }
  const reader = stream.getReader()
  const decoder = new TextDecoder()
  let total = 0
  let text = ''
  try {
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      total += value.byteLength
      if (total > MAX_BODY_BYTES) {
        text += decoder.decode(value, { stream: true })
        await reader.cancel().catch(() => {})
        break
      }
      text += decoder.decode(value, { stream: true })
    }
    text += decoder.decode()
  } catch (error) {
    // A truncated body still carries whatever frames arrived; only rethrow when
    // nothing was read at all.
    if (!text) throw error
  }
  return text.length > MAX_BODY_BYTES ? text.slice(0, MAX_BODY_BYTES) : text
}

/**
 * Parse the JSON-RPC messages carried by one response body.
 * `text/event-stream` bodies contribute one message per `data:` frame; every
 * other content type is a plain JSON body, possibly a batch array.
 * @param {string} text - response body.
 * @param {string} contentType - lowercased response content type.
 * @returns {unknown[]} decoded messages, ignoring keep-alive and non-JSON frames.
 */
export function parseJsonRpcMessages(text, contentType) {
  if (!text || !text.trim()) return []
  if (contentType.includes('text/event-stream')) {
    const messages = []
    for (const frame of text.split(/\r?\n\r?\n/)) {
      const data = []
      for (const line of frame.split(/\r?\n/)) {
        if (line.startsWith('data:')) data.push(line.slice(5).replace(/^ /, ''))
      }
      if (!data.length) continue
      const raw = data.join('\n').trim()
      if (!raw) continue
      try {
        messages.push(JSON.parse(raw))
      } catch {
        // Keep-alive comments and partial frames are not protocol messages.
      }
    }
    return messages
  }
  try {
    const parsed = JSON.parse(text)
    return Array.isArray(parsed) ? parsed : [parsed]
  } catch {
    return []
  }
}

/**
 * Pick the JSON-RPC response matching one request id.
 * @param {unknown[]} messages - decoded messages.
 * @param {number} id - request id.
 * @returns {{ result?: unknown, error?: unknown } | undefined} matching response.
 */
function responseFor(messages, id) {
  for (const message of messages) {
    if (!message || typeof message !== 'object') continue
    if (message.id !== id) continue
    if ('result' in message || 'error' in message) return message
  }
  return undefined
}

/**
 * Concatenate the text content blocks of a tool result.
 * @param {unknown} result - `tools/call` result.
 * @returns {string} joined text, empty when the result carries none.
 */
function toolText(result) {
  if (!result || typeof result !== 'object') return ''
  const content = Array.isArray(result.content) ? result.content : []
  const texts = []
  for (const part of content) {
    if (part && part.type === 'text' && typeof part.text === 'string') texts.push(part.text)
  }
  return texts.join('\n')
}

/**
 * Parse text as JSON without throwing.
 * @param {unknown} text - candidate text.
 * @returns {unknown} parsed value, or undefined when it is not JSON.
 */
function parseJsonText(text) {
  if (typeof text !== 'string' || !text.trim()) return undefined
  try {
    return JSON.parse(text)
  } catch {
    return undefined
  }
}

/**
 * Unwrap the shapes a server uses for structured tool output.
 *
 * FastMCP — the Python SDK, which the PCAI gateway is built on — emits
 * `structuredContent` for a typed return and wraps a non-object return (such as
 * a JSON string) as `{ result: <value> }`. Servers that predate structured
 * output send the payload only as text.
 * @param {unknown} value - `structuredContent` or a payload.
 * @returns {unknown} the payload, or undefined when nothing usable is there.
 */
function unwrapStructured(value) {
  if (typeof value === 'string') return parseJsonText(value)
  if (!value || typeof value !== 'object') return undefined
  if (!Array.isArray(value)) {
    const keys = Object.keys(value)
    if (keys.length === 1 && keys[0] === 'result') {
      const inner = value.result
      if (typeof inner === 'string') return parseJsonText(inner)
      if (inner && typeof inner === 'object') return inner
      return undefined
    }
  }
  return value
}

/**
 * Read a tool result payload as structured data.
 *
 * The text blocks are tried first because they carry exactly what the tool
 * returned; `structuredContent` is the fallback, unwrapped from its wrapper.
 * @param {unknown} result - `tools/call` result.
 * @returns {unknown} parsed payload, or undefined when it is not JSON.
 */
export function readToolPayload(result) {
  if (!result || typeof result !== 'object') return undefined
  const fromText = parseJsonText(toolText(result))
  if (fromText !== undefined) return fromText
  if (result.structuredContent !== undefined && result.structuredContent !== null) {
    return unwrapStructured(result.structuredContent)
  }
  return undefined
}

/**
 * Project a server catalog payload into the panel's upstream rows.
 * Accepts `{ servers: [...] }` and a bare array; a row is reachable unless it
 * carries an `error`/`failure`, which is how a federating gateway reports a
 * server it could not reach.
 * @param {unknown} payload - catalog payload.
 * @returns {Array<{ server: string, url?: string, toolCount: number, reachable: boolean, error?: string }> | undefined} rows.
 */
export function normalizeCatalog(payload) {
  const list = Array.isArray(payload?.servers)
    ? payload.servers
    : Array.isArray(payload)
      ? payload
      : Array.isArray(payload?.result?.servers)
        ? payload.result.servers
        : undefined
  if (!list) return undefined
  const rows = []
  for (const item of list) {
    if (!item || typeof item !== 'object') continue
    const server = typeof item.server === 'string' ? item.server : typeof item.name === 'string' ? item.name : undefined
    if (!server) continue
    const tools = Array.isArray(item.tools) ? item.tools : []
    const failure = item.error ?? item.failure
    rows.push({
      server,
      url: typeof item.url === 'string' ? item.url : undefined,
      toolCount: tools.length,
      reachable: failure === undefined || failure === null,
      error: failure === undefined || failure === null ? undefined : messageOf(failure),
    })
  }
  return rows
}

/**
 * Normalize `initialize` server identity.
 * @param {unknown} info - `serverInfo` member.
 * @returns {{ name?: string, version?: string } | undefined} identity.
 */
function normalizeServerInfo(info) {
  if (!info || typeof info !== 'object') return undefined
  const name = typeof info.name === 'string' ? info.name : undefined
  const version = typeof info.version === 'string' ? info.version : undefined
  if (!name && !version) return undefined
  return { name, version }
}

/**
 * One probe result.
 * @typedef {object} ProbeResult
 * @property {boolean} ok - whether the endpoint completed an MCP handshake.
 * @property {number} latencyMs - wall time of the probe.
 * @property {string} [error] - failure reason when `ok` is false.
 * @property {string} [protocolVersion] - negotiated revision.
 * @property {{ name?: string, version?: string }} [serverInfo] - server identity.
 * @property {string[]} [toolNames] - tools the server currently exposes.
 * @property {string[]} [capabilities] - capability names from `initialize`.
 * @property {Array<object>} [catalog] - normalized catalog rows when requested.
 * @property {string} [catalogError] - catalog failure reason.
 */

/**
 * Probe one Streamable-HTTP MCP endpoint.
 * @param {object} options - probe inputs.
 * @param {string} options.url - MCP endpoint URL.
 * @param {Record<string, string>} [options.headers] - configured request headers (never returned).
 * @param {number} [options.timeoutMs] - overall budget for the probe.
 * @param {string} [options.catalogTool] - tool to call for an upstream catalog.
 * @returns {Promise<ProbeResult>} probe result.
 */
export async function probeMcpHttp(options) {
  const startedAt = Date.now()
  const timeoutMs = Number.isFinite(options.timeoutMs) && options.timeoutMs > 0 ? options.timeoutMs : 4000
  const deadline = startedAt + timeoutMs
  let sessionId

  /**
   * Send one JSON-RPC message and resolve its response, if it expects one.
   * @param {Record<string, unknown>} payload - JSON-RPC message.
   * @returns {Promise<any>} the `result` member, or undefined for notifications.
   */
  const send = async (payload) => {
    const remaining = deadline - Date.now()
    if (remaining <= 0) throw new Error('probe timed out')
    const controller = new AbortController()
    const timer = setTimeout(() => controller.abort(), remaining)
    try {
      const headers = requestHeaders(options.headers, {
        'content-type': 'application/json',
        accept: 'application/json, text/event-stream',
        ...(sessionId ? { 'mcp-session-id': sessionId } : {}),
      })
      const response = await fetch(options.url, {
        method: 'POST',
        headers,
        body: JSON.stringify(payload),
        signal: controller.signal,
        redirect: 'follow',
      })
      const assignedSession = response.headers.get('mcp-session-id')
      if (assignedSession) sessionId = assignedSession
      if (!response.ok) {
        const text = await readBody(response).catch(() => '')
        throw new Error(`HTTP ${response.status}${text.trim() ? `: ${truncate(text)}` : ''}`)
      }
      if (response.status === 202 || response.status === 204) return undefined
      if (payload.id === undefined) return undefined
      const text = await readBody(response)
      const messages = parseJsonRpcMessages(text, (response.headers.get('content-type') || '').toLowerCase())
      const message = responseFor(messages, payload.id)
      if (!message) throw new Error(messages.length ? 'unexpected MCP response' : 'empty MCP response')
      if (message.error !== undefined && message.error !== null) throw new Error(messageOf(message.error))
      return message.result
    } finally {
      clearTimeout(timer)
    }
  }

  /** Release the session without letting a failure change the probe result. */
  const terminate = async () => {
    if (!sessionId) return
    const controller = new AbortController()
    const timer = setTimeout(() => controller.abort(), Math.min(1500, timeoutMs))
    try {
      await fetch(options.url, {
        method: 'DELETE',
        headers: requestHeaders(options.headers, { 'mcp-session-id': sessionId }),
        signal: controller.signal,
      })
    } catch {
      // Termination is advisory; servers may answer 405 or nothing at all.
    } finally {
      clearTimeout(timer)
      sessionId = undefined
    }
  }

  try {
    let initialized
    let lastError
    for (const protocolVersion of [PRIMARY_PROTOCOL_VERSION, ...FALLBACK_PROTOCOL_VERSIONS]) {
      try {
        initialized = await send({
          jsonrpc: '2.0',
          id: 1,
          method: 'initialize',
          params: {
            protocolVersion,
            capabilities: {},
            clientInfo: { name: 'dsh-mcp-status', version: '1.0.0' },
          },
        })
        lastError = undefined
        break
      } catch (error) {
        lastError = error
        // Only a revision mismatch is worth retrying; anything else is the answer.
        if (!/protocol|version/i.test(messageOf(error))) throw error
      }
    }
    if (lastError) throw lastError

    await send({ jsonrpc: '2.0', method: 'notifications/initialized' }).catch(() => {})

    let toolNames = []
    try {
      const listed = await send({ jsonrpc: '2.0', id: 2, method: 'tools/list', params: {} })
      const tools = Array.isArray(listed?.tools) ? listed.tools : []
      toolNames = tools.map((tool) => tool?.name).filter((name) => typeof name === 'string')
    } catch {
      // A server may refuse tools/list; the handshake alone still proves liveness.
    }

    /** @type {ProbeResult} */
    const result = {
      ok: true,
      latencyMs: Date.now() - startedAt,
      protocolVersion: typeof initialized?.protocolVersion === 'string' ? initialized.protocolVersion : undefined,
      serverInfo: normalizeServerInfo(initialized?.serverInfo),
      toolNames,
      capabilities: initialized?.capabilities && typeof initialized.capabilities === 'object'
        ? Object.keys(initialized.capabilities)
        : [],
    }

    const catalogTool = typeof options.catalogTool === 'string' ? options.catalogTool : ''
    if (catalogTool && toolNames.includes(catalogTool)) {
      try {
        const called = await send({
          jsonrpc: '2.0',
          id: 3,
          method: 'tools/call',
          params: { name: catalogTool, arguments: {} },
        })
        if (called?.isError === true) {
          result.catalogError = messageOf(toolText(called) || 'catalog tool failed')
        } else {
          const payload = readToolPayload(called)
          const rows = normalizeCatalog(payload)
          if (rows) result.catalog = rows
          else result.catalogError = 'catalog tool returned no server list'
        }
      } catch (error) {
        result.catalogError = messageOf(error)
      }
    }

    return result
  } catch (error) {
    return { ok: false, latencyMs: Date.now() - startedAt, error: messageOf(error) }
  } finally {
    await terminate()
  }
}

/**
 * Hide userinfo from an endpoint before it reaches the browser.
 * @param {string} value - configured URL.
 * @returns {string} URL without credentials.
 */
export function redactUrl(value) {
  try {
    const url = new URL(value)
    url.username = ''
    url.password = ''
    return url.toString()
  } catch {
    return String(value)
  }
}

/**
 * Last path segment of a command, so a stdio row names the program without
 * echoing the user's filesystem layout.
 * @param {string} value - configured command.
 * @returns {string} basename.
 */
export function commandName(value) {
  const text = String(value ?? '')
  const parts = text.split(/[\\/]/).filter(Boolean)
  return parts.length ? parts[parts.length - 1] : text
}
