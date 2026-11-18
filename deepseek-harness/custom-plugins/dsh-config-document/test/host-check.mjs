// Offline check of the Host half: imports index.js, applies it against a stub
// Host context, and drives the registered Fetch route against a real temporary
// file — plus every degradation path (missing services, missing file, throwing
// prepareDocument, a route that cannot register, truncation).
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { pathToFileURL } from 'node:url'
import { bundleRoot, createChecks, report } from './harness.mjs'

const moduleUrl = pathToFileURL(path.join(bundleRoot, 'index.js')).href
const host = await import(moduleUrl)
const checks = createChecks()

const { apply, applyEdit, countLines, editableEntries, readDocument, resolveDocumentPath, resolveSettings } = host

// --- exports -----------------------------------------------------------------
checks.check('index.js exports apply', typeof apply === 'function')
checks.check('index.js declares the connection service', Array.isArray(host.inject) && host.inject.includes('connection'))
checks.check('index.js exposes its helpers for tests', typeof countLines === 'function' && typeof readDocument === 'function')

// --- line counting -----------------------------------------------------------
checks.equal('countLines: empty document', countLines(''), 0)
checks.equal('countLines: one unterminated line', countLines('a'), 1)
checks.equal('countLines: trailing newline adds no phantom line', countLines('a\n'), 1)
checks.equal('countLines: two lines with trailing newline', countLines('a\nb\n'), 2)
checks.equal('countLines: blank line inside counts', countLines('a\n\nb'), 3)
checks.equal('countLines: CRLF document', countLines('a\r\nb\r\n'), 2)
checks.equal('countLines: non-string input', countLines(undefined), 0)

// --- settings normalization --------------------------------------------------
checks.equal('settings: defaults', resolveSettings(undefined), { routePath: '/api/config-document', maxBytes: 1024 * 1024 })
checks.equal('settings: leading slash added', resolveSettings({ routePath: 'api/mine' }).routePath, '/api/mine')
checks.equal('settings: maxBytes bounded', resolveSettings({ maxBytes: 1 }).maxBytes, 1024)
checks.equal('settings: maxBytes floored', resolveSettings({ maxBytes: 1234.7 }).maxBytes, 1234)
checks.equal('settings: junk ignored', resolveSettings({ routePath: 5, maxBytes: 'big' }).routePath, '/api/config-document')

/** Build a stub Host context that records the registered route. */
function createHostContext(options = {}) {
  const routes = []
  const effects = []
  const logs = { debug: [], warn: [] }
  const ctx = {
    effect(fn, label) {
      effects.push(label)
      if (options.effectThrows === true) throw new Error('route already registered')
      return fn()
    },
    get(name) {
      if (name === 'settings' && options.settings !== undefined) return options.settings
      if (name === 'configEditor' && options.configEditor !== undefined) return options.configEditor
      return undefined
    },
    connection: {
      fetch: {
        register(route) {
          routes.push(route)
          return () => {}
        },
      },
    },
    logger: {
      debug(message) {
        logs.debug.push(message)
      },
      warn(message) {
        logs.warn.push(message)
      },
    },
  }
  return { ctx, routes, effects, logs }
}

/** Read one route response as parsed JSON. */
async function readJson(route, url = 'http://localhost/api/config-document') {
  const response = await route.fetch(new Request(url))
  const text = await response.text()
  return { status: response.status, headers: response.headers, text, body: JSON.parse(text) }
}

// --- registration ------------------------------------------------------------
const tempRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'dsh-config-document-'))
const documentPath = path.join(tempRoot, 'cordis.patch.yml')
const documentText = '# profile patch\nplugins:\n  - id: config-document\n'
fs.writeFileSync(documentPath, documentText, 'utf8')

{
  const stub = createHostContext({ settings: { prepareDocument: async () => documentPath } })
  apply(stub.ctx, {})
  checks.equal('registration: document, entries and edit routes', stub.routes.length, 3)
  const route = stub.routes[0]
  checks.equal('registration: default route path', route.path, '/api/config-document')
  checks.equal('registration: GET only', route.methods, ['GET'])
  checks.equal('registration: buffered request body', route.requestBody, 'buffered')
  checks.equal('registration: entries route', stub.routes[1].path, '/api/config-document/entries')
  checks.equal('registration: edit route', stub.routes[2].path, '/api/config-document/edit')
  checks.equal('registration: edit is POST', stub.routes[2].methods, ['POST'])
  checks.equal('registration: one effect per route', stub.effects.length, 3)
  checks.check('registration: logs the served path', stub.logs.debug.some((line) => line.includes('/api/config-document')))

  const { status, headers, body } = await readJson(route)
  checks.equal('route: HTTP 200', status, 200)
  checks.equal('route: JSON content type', headers.get('content-type'), 'application/json; charset=utf-8')
  checks.equal('route: never cached', headers.get('cache-control'), 'no-store')
  checks.equal('route: ok', body.ok, true)
  checks.equal('route: absolute path', body.path, documentPath)
  checks.equal('route: basename', body.name, 'cordis.patch.yml')
  checks.equal('route: content is byte-identical', body.content, documentText)
  checks.equal('route: byte size', body.bytes, Buffer.byteLength(documentText, 'utf8'))
  checks.equal('route: line count', body.lines, 3)
  checks.check('route: mtime is an ISO timestamp', typeof body.mtime === 'string' && !Number.isNaN(Date.parse(body.mtime)))
  checks.check('route: no truncation flag for a small file', body.truncated === undefined)
  checks.check('route: payload carries no host internals', Object.keys(body).sort().join(',') === 'bytes,content,lines,mtime,name,ok,path')
  checks.check('route: no write API is touched', !/writeFile|appendFile|rename|mkdir|unlink/.test(fs.readFileSync(path.join(bundleRoot, 'index.js'), 'utf8')))
}

// --- configured route path + truncation -------------------------------------
{
  const stub = createHostContext({ settings: { prepareDocument: async () => documentPath } })
  apply(stub.ctx, { routePath: '/api/custom-document', maxBytes: 1024 })
  checks.equal('registration: configured route path', stub.routes[0].path, '/api/custom-document')
  const bigPath = path.join(tempRoot, 'big.yml')
  fs.writeFileSync(bigPath, 'x'.repeat(2000), 'utf8')
  const bigStub = createHostContext({ settings: { prepareDocument: async () => bigPath } })
  apply(bigStub.ctx, { maxBytes: 1024 })
  const { body } = await readJson(bigStub.routes[0])
  checks.equal('truncation: content capped', body.content.length, 1024)
  checks.equal('truncation: flagged', body.truncated, true)
  checks.equal('truncation: reports the cap', body.maxBytes, 1024)
  checks.equal('truncation: still reports the real size', body.bytes, 2000)

  // The cap is a byte budget, not a character count.
  const widePath = path.join(tempRoot, 'wide.yml')
  fs.writeFileSync(widePath, 'é'.repeat(2000), 'utf8')
  const wideStub = createHostContext({ settings: { prepareDocument: async () => widePath } })
  apply(wideStub.ctx, { maxBytes: 1024 })
  const wide = (await readJson(wideStub.routes[0])).body
  checks.equal('truncation: multibyte document flagged', wide.truncated, true)
  checks.check('truncation: response respects the byte budget', Buffer.byteLength(wide.content, 'utf8') <= 1024)
}

// --- config editor fallback --------------------------------------------------
{
  const stub = createHostContext({ configEditor: { documentPath } })
  apply(stub.ctx, {})
  const { body } = await readJson(stub.routes[0])
  checks.equal('fallback: configuration editor path is used', body.ok, true)
  checks.equal('fallback: same document', body.path, documentPath)
}

// --- prepareDocument throws, editor answers ---------------------------------
{
  const stub = createHostContext({
    settings: {
      prepareDocument: async () => {
        throw new Error('profile is not writable')
      },
    },
    configEditor: { documentPath },
  })
  apply(stub.ctx, {})
  const { body } = await readJson(stub.routes[0])
  checks.equal('throwing settings: falls back to the editor', body.ok, true)
}

// --- no services -------------------------------------------------------------
{
  const stub = createHostContext()
  apply(stub.ctx, {})
  const { status, body } = await readJson(stub.routes[0])
  checks.equal('no services: HTTP 200 with a data-level error', status, 200)
  checks.equal('no services: ok false', body.ok, false)
  checks.equal('no services: code unavailable', body.code, 'unavailable')
  checks.check('no services: message names both services', body.message.includes('settings service') && body.message.includes('configuration editor'))
}

// --- missing file ------------------------------------------------------------
{
  const missing = path.join(tempRoot, 'nope.yml')
  const stub = createHostContext({ settings: { prepareDocument: async () => missing } })
  apply(stub.ctx, {})
  const { body } = await readJson(stub.routes[0])
  checks.equal('missing file: ok false', body.ok, false)
  checks.equal('missing file: code missing', body.code, 'missing')
  checks.equal('missing file: path reported', body.path, missing)
  checks.includes('missing file: message carries the errno', body.message, 'ENOENT')
}

// --- directory instead of file ----------------------------------------------
{
  const stub = createHostContext({ settings: { prepareDocument: async () => tempRoot } })
  apply(stub.ctx, {})
  const { body } = await readJson(stub.routes[0])
  checks.equal('directory: rejected', body.code, 'not-a-file')
}

// --- prepareDocument returns nothing ----------------------------------------
{
  const stub = createHostContext({ settings: { prepareDocument: async () => '' } })
  apply(stub.ctx, {})
  const { body } = await readJson(stub.routes[0])
  checks.equal('empty path: unavailable', body.code, 'unavailable')
  checks.includes('empty path: explains the settings answer', body.message, 'prepared no document path')
}

// --- route registration failure never throws --------------------------------
{
  const stub = createHostContext({ effectThrows: true })
  let threw = false
  try {
    apply(stub.ctx, {})
  } catch {
    threw = true
  }
  checks.check('apply: never throws when the route cannot register', threw === false)
  checks.check('apply: the failure is logged as a warning', stub.logs.warn.some((line) => line.includes('route registration failed')))
}

// --- apply without a connection service ------------------------------------
{
  const ctx = {
    effect(fn) {
      return fn()
    },
    get() {
      return undefined
    },
    logger: { debug() {}, warn() {} },
  }
  let threw = false
  try {
    apply(ctx, {})
  } catch {
    threw = true
  }
  checks.check('apply: never throws without a connection service', threw === false)
}

// --- direct helper use ------------------------------------------------------
{
  const ctx = { get: () => undefined }
  const resolved = await resolveDocumentPath(ctx)
  checks.check('resolveDocumentPath: reports reasons instead of throwing', typeof resolved.error === 'string')
  const payload = await readDocument(ctx, resolveSettings(undefined))
  checks.equal('readDocument: unavailable without services', payload.code, 'unavailable')
}

// --- guided editing ---------------------------------------------------------
{
  const entries = [
    {
      options: {
        id: 'mcp-pcai-llm-gateway',
        name: '@deepseek-ai/dsh-mcp-client',
        config: {
          transport: 'streamable-http',
          serverName: 'pcai-llm-gateway',
          url: 'http://old:9200/mcp',
          toolCallTimeoutMs: 600000,
          headers: { Authorization: 'secret' },
        },
      },
    },
    { options: { id: 'no-scalars', name: 'x', config: { nested: { a: 1 }, list: [1, 2] } } },
  ]
  const calls = []
  const configEditor = {
    entries: () => entries,
    edit: async (entry, change) => {
      calls.push({ id: entry.options.id, next: change(structuredClone(entry.options.config)) })
    },
  }
  const ctx = { get: (name) => (name === 'configEditor' ? configEditor : undefined) }

  const listed = editableEntries(ctx)
  checks.equal('editableEntries: skips rows without scalar fields', listed.length, 1)
  checks.equal('editableEntries: reports the row id', listed[0].id, 'mcp-pcai-llm-gateway')
  checks.check('editableEntries: nested objects are not offered', !listed[0].fields.some((field) => field.path === 'headers'))

  const ok = await applyEdit(ctx, { id: 'mcp-pcai-llm-gateway', path: 'url', value: 'http://new:9200/mcp' })
  checks.equal('applyEdit: accepts a string change', ok.ok, true)
  checks.equal('applyEdit: the change reaches the editor', calls[0].next.url, 'http://new:9200/mcp')
  checks.equal('applyEdit: sibling scalars survive', calls[0].next.serverName, 'pcai-llm-gateway')
  checks.equal('applyEdit: an untouched object sibling survives', calls[0].next.headers.Authorization, 'secret')

  const missing = await applyEdit(ctx, { id: 'nope', path: 'url', value: 'x' })
  checks.equal('applyEdit: unknown row is refused', missing.code, 'missing')
  const missingField = await applyEdit(ctx, { id: 'mcp-pcai-llm-gateway', path: 'nope', value: 'x' })
  checks.equal('applyEdit: unknown field is refused', missingField.code, 'missing')
  const badNumber = await applyEdit(ctx, { id: 'mcp-pcai-llm-gateway', path: 'toolCallTimeoutMs', value: 'soon' })
  checks.equal('applyEdit: a non-numeric number is refused', badNumber.code, 'invalid')
  const goodNumber = await applyEdit(ctx, { id: 'mcp-pcai-llm-gateway', path: 'toolCallTimeoutMs', value: '120000' })
  checks.equal('applyEdit: a numeric string is coerced', goodNumber.ok, true)
  checks.equal('applyEdit: the coerced value is a number', calls[1].next.toolCallTimeoutMs, 120000)
  const noEditor = await applyEdit({ get: () => undefined }, { id: 'x', path: 'y', value: 'z' })
  checks.equal('applyEdit: reports a missing editor', noEditor.code, 'unavailable')
}

fs.rmSync(tempRoot, { recursive: true, force: true })
report('host-check', checks.results)
