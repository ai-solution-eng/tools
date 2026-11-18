/**
 * Config document — Host half.
 *
 * The shipped Settings action "Open configuration file" asks the Host to hand the
 * active profile's `cordis.patch.yml` to a *native* text editor
 * (`settings.openSettingsDocument()` → `openNativeTextFile(path)`). That works on
 * a laptop, where the Host process runs on the same desktop as the browser. In a
 * container deployment (the PCAI unit) the Host has no desktop, so the native open
 * fails and the user sees nothing happen.
 *
 * This half closes the gap with a read-only bridge: one authenticated exact Fetch
 * route that answers the profile document's absolute path and text. The browser
 * half (`./client.js`) renders that text in a modal inside the Web UI, so the file
 * is reachable from any browser that can reach the Host.
 *
 * The route deliberately accepts no path parameter: it can only ever answer the
 * one document the Host's own settings service prepares, never an arbitrary Host
 * file. The document is read, never written — persisting profile configuration is
 * the config editor's job (`ctx.configEditor.edit()`), not this plugin's.
 *
 * @module
 */

import { readFile, stat } from 'node:fs/promises'
import { basename } from 'node:path'

/** Accepted settings and their defaults. */
const DEFAULT_SETTINGS = {
  /** Authenticated route path served to the browser (must live under /api). */
  routePath: '/api/config-document',
  /** Response cap for the document text; larger files are truncated and flagged. */
  maxBytes: 1024 * 1024,
}

/** Numeric bounds applied to configured settings. */
const BOUNDS = {
  maxBytes: [1024, 16 * 1024 * 1024],
}

/** Services required before this plugin can serve the document. */
export const inject = ['connection']

/**
 * Coerce a thrown value into one short line of text.
 * @param {unknown} error - thrown value.
 * @returns {string} readable message.
 */
function messageOf(error) {
  if (error === undefined || error === null) return 'unknown error'
  if (typeof error === 'string') return error
  if (typeof error.message === 'string') return error.message
  return String(error)
}

/**
 * Normalize a raw Loader config into accepted settings.
 * @param {unknown} config - row config.
 * @returns {typeof DEFAULT_SETTINGS} resolved settings.
 */
function resolveSettings(config) {
  const raw = config && typeof config === 'object' ? config : {}
  const settings = { ...DEFAULT_SETTINGS }
  if (typeof raw.routePath === 'string' && raw.routePath.trim()) {
    const trimmed = raw.routePath.trim()
    settings.routePath = trimmed.startsWith('/') ? trimmed : `/${trimmed}`
  }
  if (typeof raw.maxBytes === 'number' && Number.isFinite(raw.maxBytes)) {
    const [min, max] = BOUNDS.maxBytes
    settings.maxBytes = Math.min(max, Math.max(min, Math.floor(raw.maxBytes)))
  }
  return settings
}

/**
 * Project a JSON-serializable value as a JSON response.
 * @param {unknown} value - response body.
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
 * Count lines the way an editor shows them: one for the last unterminated line,
 * none for an empty document, and no phantom line after a trailing newline.
 * @param {string} text - document text.
 * @returns {number} line count.
 */
function countLines(text) {
  if (typeof text !== 'string' || text.length === 0) return 0
  let lines = 0
  for (let index = 0; index < text.length; index += 1) {
    if (text.charCodeAt(index) === 10) lines += 1
  }
  return text.endsWith('\n') ? lines : lines + 1
}

/**
 * Resolve the profile document path from whichever Host service owns it.
 * The settings service is the documented owner; the configuration editor exposes
 * the same path and is the fallback when the settings service is not mounted.
 * @param {import('@deepseek-ai/cordis').Context} ctx - plugin context.
 * @returns {Promise<{ path: string } | { error: string }>} resolved path or reasons.
 */
async function resolveDocumentPath(ctx) {
  const attempts = []
  const settings = ctx.get('settings')
  if (settings && typeof settings.prepareDocument === 'function') {
    try {
      const path = await settings.prepareDocument()
      if (typeof path === 'string' && path.length > 0) return { path }
      attempts.push('the settings service prepared no document path')
    } catch (error) {
      attempts.push(`the settings service could not prepare the document (${messageOf(error)})`)
    }
  } else {
    attempts.push('the settings service is not mounted')
  }
  const editor = ctx.get('configEditor')
  if (editor && typeof editor.documentPath === 'string' && editor.documentPath.length > 0) {
    return { path: editor.documentPath }
  }
  attempts.push('the configuration editor exposes no document path')
  return { error: attempts.join('; ') }
}

/**
 * Read the profile document for the browser.
 * Never throws: every failure is reported as `{ ok: false, code, message }` so the
 * browser half can render it instead of showing an empty modal.
 * @param {import('@deepseek-ai/cordis').Context} ctx - plugin context.
 * @param {typeof DEFAULT_SETTINGS} settings - resolved settings.
 * @returns {Promise<Record<string, any>>} document payload.
 */
async function readDocument(ctx, settings) {
  const resolved = await resolveDocumentPath(ctx)
  if (resolved.error !== undefined) return { ok: false, code: 'unavailable', message: resolved.error }
  const path = resolved.path

  let info
  try {
    info = await stat(path)
  } catch (error) {
    return {
      ok: false,
      code: error && error.code === 'ENOENT' ? 'missing' : 'unreadable',
      path,
      message: `cannot stat ${path}: ${messageOf(error)}`,
    }
  }
  if (!info.isFile()) {
    return { ok: false, code: 'not-a-file', path, message: `${path} is not a regular file` }
  }

  let text
  try {
    text = await readFile(path, 'utf8')
  } catch (error) {
    return { ok: false, code: 'unreadable', path, message: `cannot read ${path}: ${messageOf(error)}` }
  }

  // The cap is a byte budget, so measure bytes; a cut multibyte character at the
  // boundary decodes as U+FFFD, which the truncation flag explains in the modal.
  const buffer = Buffer.from(text, 'utf8')
  const truncated = buffer.length > settings.maxBytes
  const content = truncated ? buffer.subarray(0, settings.maxBytes).toString('utf8') : text
  const payload = {
    ok: true,
    path,
    name: basename(path),
    content,
    bytes: info.size,
    lines: countLines(content),
    mtime: new Date(info.mtimeMs).toISOString(),
  }
  if (truncated) {
    payload.truncated = true
    payload.maxBytes = settings.maxBytes
  }
  return payload
}

/** Largest number of scalar fields offered per row. */
const MAX_FIELDS_PER_ENTRY = 24

/**
 * Top-level scalar leaves of a row config: the fields this editor offers.
 * Nested objects and arrays are left to the file, because a partial rewrite of
 * them would drop the parts the form does not show.
 * @param {unknown} config - row config.
 * @returns {Array<{ path: string, value: string | number | boolean, type: string }>} fields.
 */
function scalarFields(config) {
  if (!config || typeof config !== 'object' || Array.isArray(config)) return []
  const fields = []
  for (const [path, value] of Object.entries(config)) {
    const type = typeof value
    if (type !== 'string' && type !== 'number' && type !== 'boolean') continue
    fields.push({ path, value, type })
    if (fields.length >= MAX_FIELDS_PER_ENTRY) break
  }
  return fields
}

/**
 * Rows the editor may change: Loader entries with an id and a plain config.
 * @param {import('@deepseek-ai/cordis').Context} ctx - plugin context.
 * @returns {Array<{ id: string, name: string, fields: Array<object> }>} editable rows.
 */
function editableEntries(ctx) {
  const editor = ctx.get('configEditor')
  if (!editor || typeof editor.entries !== 'function') return []
  const rows = []
  for (const entry of editor.entries()) {
    const id = entry && entry.options ? entry.options.id : undefined
    if (typeof id !== 'string' || id === '') continue
    const name = entry.options && typeof entry.options.name === 'string' ? entry.options.name : ''
    const fields = scalarFields(entry.options ? entry.options.config : undefined)
    if (fields.length === 0) continue
    rows.push({ id, name, fields })
  }
  return rows
}

/**
 * Coerce a submitted value to the type the current one has.
 * @param {unknown} current - current value.
 * @param {unknown} next - submitted value.
 * @returns {{ value?: unknown, error?: string }} coerced value or a reason.
 */
function coerceValue(current, next) {
  if (typeof current === 'number') {
    const parsed = Number(next)
    if (!Number.isFinite(parsed)) return { error: 'enter a number' }
    return { value: parsed }
  }
  if (typeof current === 'boolean') {
    if (typeof next === 'boolean') return { value: next }
    if (next === 'true') return { value: true }
    if (next === 'false') return { value: false }
    return { error: 'enter true or false' }
  }
  if (typeof next !== 'string') return { error: 'enter a text value' }
  return { value: next }
}

/**
 * Apply one scalar edit through the configuration editor, which validates the
 * row against its own schema, patches the document in place (comments and
 * !!js expressions preserved), writes atomically and rolls back on failure.
 * @param {import('@deepseek-ai/cordis').Context} ctx - plugin context.
 * @param {unknown} body - request body.
 * @returns {Promise<Record<string, any>>} result.
 */
async function applyEdit(ctx, body) {
  const editor = ctx.get('configEditor')
  if (!editor || typeof editor.edit !== 'function') {
    return { ok: false, code: 'unavailable', message: 'the configuration editor is not mounted' }
  }
  const id = body && typeof body.id === 'string' ? body.id : ''
  const path = body && typeof body.path === 'string' ? body.path : ''
  if (!id || !path) return { ok: false, code: 'invalid', message: 'id and path are required' }
  let entry
  for (const candidate of editor.entries()) {
    if (candidate && candidate.options && candidate.options.id === id) {
      entry = candidate
      break
    }
  }
  if (entry === undefined) return { ok: false, code: 'missing', message: 'no row with id ' + id }
  const current = entry.options && entry.options.config ? entry.options.config[path] : undefined
  if (current === undefined) return { ok: false, code: 'missing', message: 'row has no field ' + path }
  const coerced = coerceValue(current, body.value)
  if (coerced.error !== undefined) return { ok: false, code: 'invalid', message: coerced.error }
  try {
    await editor.edit(entry, (config) => ({ ...config, [path]: coerced.value }))
    return { ok: true }
  } catch (error) {
    return { ok: false, code: 'refused', message: messageOf(error) }
  }
}

/**
 * Register the document routes.
 * @param {import('@deepseek-ai/cordis').Context} ctx - plugin context.
 * @param {unknown} config - row config.
 */
export function apply(ctx, config) {
  const settings = resolveSettings(config)

  const route = {
    path: settings.routePath,
    methods: ['GET'],
    requestBody: 'buffered',
    fetch: async () => {
      try {
        return jsonResponse(await readDocument(ctx, settings))
      } catch (error) {
        return jsonResponse({ ok: false, code: 'internal', message: messageOf(error) }, 500)
      }
    },
  }

  // A missing or conflicting route must never take the plugin row down: the
  // browser half degrades to a clear "route unavailable" message instead.
  const entriesRoute = {
    path: settings.routePath + '/entries',
    methods: ['GET'],
    requestBody: 'buffered',
    fetch: async () => {
      try {
        return jsonResponse({ ok: true, entries: editableEntries(ctx) })
      } catch (error) {
        return jsonResponse({ ok: false, code: 'internal', message: messageOf(error) }, 500)
      }
    },
  }

  const editRoute = {
    path: settings.routePath + '/edit',
    methods: ['POST'],
    requestBody: 'buffered',
    fetch: async (request) => {
      let body
      try {
        body = await request.json()
      } catch {
        return jsonResponse({ ok: false, code: 'invalid', message: 'expected a JSON body' }, 400)
      }
      try {
        const result = await applyEdit(ctx, body)
        return jsonResponse(result, result.ok === true ? 200 : 400)
      } catch (error) {
        return jsonResponse({ ok: false, code: 'internal', message: messageOf(error) }, 500)
      }
    },
  }

  try {
    ctx.effect(() => ctx.connection.fetch.register(route), 'config-document: document route')
    ctx.effect(() => ctx.connection.fetch.register(entriesRoute), 'config-document: entries route')
    ctx.effect(() => ctx.connection.fetch.register(editRoute), 'config-document: edit route')
    ctx.logger?.debug?.(`config-document: serving ${settings.routePath}`)
  } catch (error) {
    try {
      ctx.logger?.warn?.(`config-document: route registration failed: ${messageOf(error)}`)
    } catch {
      // Diagnostics must never prevent the plugin from settling.
    }
  }
}

export { applyEdit, countLines, editableEntries, readDocument, resolveDocumentPath, resolveSettings, scalarFields }
