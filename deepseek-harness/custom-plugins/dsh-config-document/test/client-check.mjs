// Offline check of the browser half: loads client.js the way the Harness module
// loader does, runs apply() against a stub context, renders the action with a
// React stand-in, and exercises every acceptance path (open, copy, download,
// reload, Host-editor failure, Escape, missing Host route, Host error payload).
import {
  createChecks,
  createRuntime,
  fakeEvent,
  findAll,
  findByClass,
  findButton,
  installBrowserGlobals,
  loadClientModule,
  report,
  textOf,
  uninstallBrowserGlobals,
} from './harness.mjs'

const NS = 'config-document'
const DOCUMENT = {
  ok: true,
  path: '/var/dsh/home/.dsh/profiles/web/cordis.patch.yml',
  name: 'cordis.patch.yml',
  content: 'plugins:\n  - id: config-document\n',
  bytes: 42,
  lines: 2,
  mtime: '2026-11-16T20:15:00.000Z',
}

/**
 * Load a fresh copy of the bundle, apply it against a stub Client context, and
 * expose the registered cell plus a mount helper that composes the real injected
 * props with the localized `t` seat.
 * @param {Record<string, any>} [options] - browser global overrides.
 * @returns {Record<string, any>} test fixture.
 */
function setup(options = {}) {
  const record = installBrowserGlobals(options)
  const runtime = createRuntime()
  const loaded = loadClientModule()
  const plugin = loaded.factory((name) => {
    if (name === 'react') return runtime.React
    throw new Error(`unexpected require: ${name}`)
  })
  const dictionaries = []
  const registrations = []
  const effects = []
  const ctx = {
    effect(fn, label) {
      effects.push(label)
      return fn()
    },
    locale: {
      register(namespace, dictionary) {
        dictionaries.push({ namespace, dictionary })
        return () => {}
      },
    },
    slots: {
      inject(slot, callback) {
        callback()
        return () => {}
      },
      register(registrationOptions, component) {
        registrations.push({ options: registrationOptions, component })
        return () => {}
      },
    },
    get() {
      return undefined
    },
  }
  plugin.apply(ctx)
  const dictionary = dictionaries[0]?.dictionary?.en ?? {}
  const t = (key, params) => {
    const template = typeof dictionary[key] === 'string' ? dictionary[key] : key
    if (!params) return template
    return template.replace(/\{(\w+)\}/g, (match, name) => (params[name] === undefined ? match : String(params[name])))
  }
  const registration = registrations[0]
  const injected = registration && typeof registration.options.inject === 'function' ? registration.options.inject() : {}
  const mount = (extra = {}) => runtime.mount(registration.component, { t, ...injected, ...extra })
  return { runtime, plugin, loaded, dictionaries, registrations, effects, ctx, record, t, dictionary, registration, injected, mount }
}

const checks = createChecks()

// --- module + registration ---------------------------------------------------
{
  const fixture = setup()
  const { loaded, plugin, registrations, dictionaries } = fixture
  checks.check('module registers itself with the module loader', loaded !== null)
  checks.check('module id is the package name', loaded?.id === '@local/dsh-config-document')
  checks.check('factory returns an inject list', Array.isArray(plugin.inject))
  checks.check('injects slots and locale', plugin.inject.includes('slots') && plugin.inject.includes('locale'))
  checks.check('apply is a function', typeof plugin.apply === 'function')
  checks.check('registers exactly one slot cell', registrations.length === 1)
  const registration = registrations[0]
  checks.equal('cell slot is settings.action', registration?.options?.name, 'settings.action')
  checks.equal('cell reuses the shipped id open-document', registration?.options?.id, 'open-document')
  checks.equal('cell keeps order 0', registration?.options?.order, 0)
  checks.check(
    'cell shadows the shipped entry with a lower priority',
    typeof registration?.options?.priority === 'number' && registration.options.priority < 0,
  )
  checks.equal('cell carries the locale namespace', registration?.options?.locale, NS)
  checks.check('cell injects the Host-editor opener', typeof fixture.injected.openSystemDocument === 'function')
  checks.check('registers one dictionary', dictionaries.length === 1)
  checks.equal('dictionary namespace', dictionaries[0]?.namespace, NS)
  checks.check('dictionary has en and zh', Boolean(dictionaries[0]?.dictionary?.en) && Boolean(dictionaries[0]?.dictionary?.zh))
  uninstallBrowserGlobals()
}

// --- closed state ------------------------------------------------------------
{
  const fixture = setup()
  const tree = fixture.mount()
  const button = findButton(tree, 'Open configuration file')
  checks.check('closed: renders the shipped label', button !== undefined)
  checks.equal('closed: no dialog yet', findByClass(tree, 'cfgdoc-overlay'), undefined)
  checks.check('closed: trigger is a labelled dialog opener', button?.props['aria-haspopup'] === 'dialog' && button?.props['aria-expanded'] === 'false')
  checks.check('closed: styles are rendered with the component', findAll(tree, (node) => node.type === 'style').length === 1)
  fixture.runtime.unmount()
  uninstallBrowserGlobals()
}

// --- open + read -------------------------------------------------------------
{
  const fixture = setup({ fetch: async () => ({ ok: true, status: 200, json: async () => DOCUMENT }) })
  let tree = fixture.mount()
  findButton(tree, 'Open configuration file').props.onClick(fakeEvent())
  await fixture.runtime.settle()
  tree = fixture.runtime.tree
  const overlay = findByClass(tree, 'cfgdoc-overlay')
  const dialog = findAll(tree, (node) => node.props?.role === 'dialog')[0]
  checks.check('open: modal appears', overlay !== undefined)
  checks.check('open: dialog is a modal with an accessible name', dialog?.props['aria-modal'] === 'true' && dialog?.props['aria-label'] === 'Configuration file')
  checks.equal('open: requests the Host route', fixture.record.fetched[0]?.url, 'api/config-document')
  checks.equal('open: same-origin credentials', fixture.record.fetched[0]?.init?.credentials, 'same-origin')
  checks.includes('open: absolute path is visible', textOf(findByClass(tree, 'cfgdoc-path')), DOCUMENT.path)
  checks.includes('open: line hint is visible', textOf(findByClass(tree, 'cfgdoc-meta')), '2 lines')
  checks.includes('open: size hint is visible', textOf(findByClass(tree, 'cfgdoc-meta')), '42 B')
  checks.includes('open: modified hint is visible', textOf(findByClass(tree, 'cfgdoc-meta')), 'modified')
  checks.equal('open: file text is rendered verbatim', textOf(findByClass(tree, 'cfgdoc-code')), DOCUMENT.content)
  checks.check('open: toolbar exposes the four actions', ['Copy', 'Download', 'Reload', 'Open in Host editor'].every((label) => findButton(tree, label) !== undefined))
  checks.includes('open: read-only hint is visible', textOf(tree), 'Read-only view')
  checks.check('open: dialog takes focus on open', findAll(tree, (node) => node.props?.ref?.__node?.focused > 0).length > 0)

  // Copy
  findButton(tree, 'Copy').props.onClick(fakeEvent())
  await fixture.runtime.settle()
  checks.equal('copy: writes the exact document text', fixture.record.copied, [DOCUMENT.content])
  checks.includes('copy: confirms in the status line', textOf(fixture.runtime.tree), 'Copied to the clipboard.')

  // Download
  tree = fixture.runtime.tree
  findButton(tree, 'Download').props.onClick(fakeEvent())
  await fixture.runtime.settle()
  checks.equal('download: builds one object URL', fixture.record.createdUrls.length, 1)
  checks.equal('download: blob carries the document text', fixture.record.lastBlobParts, [DOCUMENT.content])
  const link = findAll(fixture.runtime.tree, (node) => node.type === 'a')[0]
  checks.equal('download: anchor points at the object URL', link?.props?.ref?.__node?.href, 'blob:config-document/1')
  checks.equal('download: anchor downloads the real basename', link?.props?.ref?.__node?.download, 'cordis.patch.yml')
  checks.equal('download: anchor was clicked', link?.props?.ref?.__node?.clicked, 1)
  checks.includes('download: confirms in the status line', textOf(fixture.runtime.tree), 'handed to the browser download')

  // Reload
  findButton(fixture.runtime.tree, 'Reload').props.onClick(fakeEvent())
  await fixture.runtime.settle()
  checks.equal('reload: re-reads the Host route', fixture.record.fetched.length, 2)
  uninstallBrowserGlobals()
}

// --- Host editor failure is surfaced, never swallowed ------------------------
{
  const fixture = setup({ fetch: async () => ({ ok: true, status: 200, json: async () => DOCUMENT }) })
  fixture.mount({
    openSystemDocument: async () => ({ ok: false, code: 'failed', message: 'path open failed: xdg-open: not found' }),
  })
  findButton(fixture.runtime.tree, 'Open configuration file').props.onClick(fakeEvent())
  await fixture.runtime.settle()
  findButton(fixture.runtime.tree, 'Open in Host editor').props.onClick(fakeEvent())
  await fixture.runtime.settle()
  const alert = findAll(fixture.runtime.tree, (node) => node.props?.role === 'alert')[0]
  checks.check('host editor: failure is shown as an alert', alert !== undefined)
  checks.includes('host editor: failure carries the Host message', textOf(alert), 'path open failed: xdg-open: not found')
  checks.includes('host editor: failure names the Host', textOf(alert), 'The Host could not open the file')
  uninstallBrowserGlobals()
}

// --- Host editor: no remote connection --------------------------------------
{
  const fixture = setup({ fetch: async () => ({ ok: true, status: 200, json: async () => DOCUMENT }) })
  fixture.mount()
  findButton(fixture.runtime.tree, 'Open configuration file').props.onClick(fakeEvent())
  await fixture.runtime.settle()
  findButton(fixture.runtime.tree, 'Open in Host editor').props.onClick(fakeEvent())
  await fixture.runtime.settle()
  checks.includes('host editor: reports a missing Host connection', textOf(fixture.runtime.tree), 'no Host settings connection')
  uninstallBrowserGlobals()
}

// --- Host editor: remote returns a refusal ----------------------------------
{
  const fixture = setup({ fetch: async () => ({ ok: true, status: 200, json: async () => DOCUMENT }) })
  fixture.ctx.get = (name) => (name === 'remote'
    ? { settings: { async openSettingsDocument() { return { ok: false, error: { message: 'path open failed: no desktop' } } } } }
    : undefined)
  fixture.mount()
  findButton(fixture.runtime.tree, 'Open configuration file').props.onClick(fakeEvent())
  await fixture.runtime.settle()
  findButton(fixture.runtime.tree, 'Open in Host editor').props.onClick(fakeEvent())
  await fixture.runtime.settle()
  checks.includes('host editor: remote refusal reaches the status line', textOf(fixture.runtime.tree), 'no desktop')
  uninstallBrowserGlobals()
}

// --- Host editor: remote accepts --------------------------------------------
{
  const fixture = setup({ fetch: async () => ({ ok: true, status: 200, json: async () => DOCUMENT }) })
  fixture.ctx.get = (name) => (name === 'remote'
    ? { settings: { async openSettingsDocument() { return { opened: true } } } }
    : undefined)
  fixture.mount()
  findButton(fixture.runtime.tree, 'Open configuration file').props.onClick(fakeEvent())
  await fixture.runtime.settle()
  findButton(fixture.runtime.tree, 'Open in Host editor').props.onClick(fakeEvent())
  await fixture.runtime.settle()
  checks.includes('host editor: success is reported', textOf(fixture.runtime.tree), 'The Host accepted the open request')
  uninstallBrowserGlobals()
}

// --- Host editor: remote throws ---------------------------------------------
{
  const fixture = setup({ fetch: async () => ({ ok: true, status: 200, json: async () => DOCUMENT }) })
  fixture.ctx.get = (name) => (name === 'remote'
    ? { settings: { async openSettingsDocument() { throw new Error('gateway/internal: path open failed') } } }
    : undefined)
  fixture.mount()
  findButton(fixture.runtime.tree, 'Open configuration file').props.onClick(fakeEvent())
  await fixture.runtime.settle()
  findButton(fixture.runtime.tree, 'Open in Host editor').props.onClick(fakeEvent())
  await fixture.runtime.settle()
  checks.includes('host editor: a thrown remote error is reported', textOf(fixture.runtime.tree), 'gateway/internal: path open failed')
  uninstallBrowserGlobals()
}

// --- missing Host route ------------------------------------------------------
{
  const fixture = setup({
    fetch: async () => ({
      ok: false,
      status: 404,
      json: async () => {
        throw new Error('not json')
      },
    }),
  })
  fixture.mount()
  findButton(fixture.runtime.tree, 'Open configuration file').props.onClick(fakeEvent())
  await fixture.runtime.settle()
  const notice = findByClass(fixture.runtime.tree, 'cfgdoc-notice')
  checks.includes('missing route: names the failure', textOf(notice), 'Could not read the configuration file')
  checks.includes('missing route: reports the HTTP status', textOf(notice), 'HTTP 404')
  checks.includes('missing route: explains the restart requirement', textOf(notice), 'dsh restart')
  uninstallBrowserGlobals()
}

// --- Host error payload ------------------------------------------------------
{
  const fixture = setup({
    fetch: async () => ({
      ok: true,
      status: 200,
      json: async () => ({ ok: false, code: 'missing', path: '/nope/cordis.patch.yml', message: 'cannot stat /nope/cordis.patch.yml: ENOENT' }),
    }),
  })
  fixture.mount()
  findButton(fixture.runtime.tree, 'Open configuration file').props.onClick(fakeEvent())
  await fixture.runtime.settle()
  const tree = fixture.runtime.tree
  checks.includes('host error: shows the Host message', textOf(findByClass(tree, 'cfgdoc-notice')), 'ENOENT')
  checks.includes('host error: still shows the document path', textOf(findByClass(tree, 'cfgdoc-path')), '/nope/cordis.patch.yml')
  checks.check('host error: copy and download are disabled', findButton(tree, 'Copy')?.props.disabled === true && findButton(tree, 'Download')?.props.disabled === true)
  uninstallBrowserGlobals()
}

// --- empty document ----------------------------------------------------------
{
  const fixture = setup({
    fetch: async () => ({ ok: true, status: 200, json: async () => ({ ...DOCUMENT, content: '', lines: 0, bytes: 0 }) }),
  })
  fixture.mount()
  findButton(fixture.runtime.tree, 'Open configuration file').props.onClick(fakeEvent())
  await fixture.runtime.settle()
  checks.includes('empty document: says so instead of showing a blank box', textOf(fixture.runtime.tree), 'The configuration file is empty.')
  uninstallBrowserGlobals()
}

// --- clipboard fallback ------------------------------------------------------
{
  const fixture = setup({
    clipboardFails: true,
    execCommandResult: true,
    fetch: async () => ({ ok: true, status: 200, json: async () => DOCUMENT }),
  })
  fixture.mount()
  findButton(fixture.runtime.tree, 'Open configuration file').props.onClick(fakeEvent())
  await fixture.runtime.settle()
  findButton(fixture.runtime.tree, 'Copy').props.onClick(fakeEvent())
  await fixture.runtime.settle()
  checks.equal('copy fallback: uses the selection copy path', fixture.record.execCommand, 'copy')
  checks.includes('copy fallback: still confirms', textOf(fixture.runtime.tree), 'Copied to the clipboard.')
  uninstallBrowserGlobals()
}

// --- keyboard + mask --------------------------------------------------------
{
  const fixture = setup({ fetch: async () => ({ ok: true, status: 200, json: async () => DOCUMENT }) })
  fixture.mount()
  findButton(fixture.runtime.tree, 'Open configuration file').props.onClick(fakeEvent())
  await fixture.runtime.settle()
  let tree = fixture.runtime.tree
  const dialog = findAll(tree, (node) => node.props?.role === 'dialog')[0]
  const escape = fakeEvent({ key: 'Escape' })
  dialog.props.onKeyDown(escape)
  fixture.runtime.flush()
  checks.check('escape: closes the dialog', findByClass(fixture.runtime.tree, 'cfgdoc-overlay') === undefined)
  checks.check('escape: prevents the event reaching the Settings panel', escape.defaultPrevented === true && escape.propagationStopped === true)

  const tab = fakeEvent({ key: 'Tab' })
  dialog.props.onKeyDown(tab)
  checks.check('tab: trapped inside the dialog (no focusable nodes -> no focus escape)', tab.defaultPrevented === true)

  findButton(fixture.runtime.tree, 'Open configuration file').props.onClick(fakeEvent())
  await fixture.runtime.settle()
  tree = fixture.runtime.tree
  const overlay = findByClass(tree, 'cfgdoc-overlay')
  const inside = fakeEvent()
  inside.target = {}
  inside.currentTarget = {}
  overlay.props.onClick(inside)
  fixture.runtime.flush()
  checks.check('mask: a click inside the dialog keeps it open', findByClass(fixture.runtime.tree, 'cfgdoc-overlay') !== undefined)
  const maskEvent = fakeEvent()
  maskEvent.target = maskEvent.currentTarget = {}
  overlay.props.onClick(maskEvent)
  fixture.runtime.flush()
  checks.check('mask: a click on the mask closes it', findByClass(fixture.runtime.tree, 'cfgdoc-overlay') === undefined)

  findButton(fixture.runtime.tree, 'Open configuration file').props.onClick(fakeEvent())
  await fixture.runtime.settle()
  const closeButton = findAll(fixture.runtime.tree, (node) => node.props?.['aria-label'] === 'Close')[0]
  closeButton.props.onClick(fakeEvent())
  fixture.runtime.flush()
  checks.check('close button: closes the dialog', findByClass(fixture.runtime.tree, 'cfgdoc-overlay') === undefined)
  uninstallBrowserGlobals()
}

// --- degrade without a locale seat ------------------------------------------
{
  const fixture = setup({ fetch: async () => ({ ok: true, status: 200, json: async () => DOCUMENT }) })
  const tree = fixture.runtime.mount(fixture.registration.component, {})
  checks.check('no t prop: still renders instead of throwing', findAll(tree, (node) => node.type === 'button').length > 0)
  uninstallBrowserGlobals()
}

// --- reload after an error ---------------------------------------------------
{
  let attempt = 0
  const fixture = setup({
    fetch: async () => {
      attempt += 1
      if (attempt === 1) {
        return {
          ok: true,
          status: 200,
          json: async () => ({ ok: false, code: 'missing', path: '/gone/cordis.patch.yml', message: 'cannot stat /gone/cordis.patch.yml: ENOENT' }),
        }
      }
      return { ok: true, status: 200, json: async () => DOCUMENT }
    },
  })
  fixture.mount()
  findButton(fixture.runtime.tree, 'Open configuration file').props.onClick(fakeEvent())
  await fixture.runtime.settle()
  checks.includes('reload: error is shown first', textOf(fixture.runtime.tree), 'ENOENT')
  findButton(fixture.runtime.tree, 'Reload').props.onClick(fakeEvent())
  await fixture.runtime.settle()
  const tree = fixture.runtime.tree
  checks.equal('reload after error: document text appears', textOf(findByClass(tree, 'cfgdoc-code')), DOCUMENT.content)
  checks.equal('reload after error: error notice is gone', findByClass(tree, 'cfgdoc-notice'), undefined)
  uninstallBrowserGlobals()
}

// --- apply survives a rejected registration ---------------------------------
{
  installBrowserGlobals({})
  const runtime = createRuntime()
  const loaded = loadClientModule()
  const plugin = loaded.factory((name) => {
    if (name === 'react') return runtime.React
    throw new Error(`unexpected require: ${name}`)
  })
  const warnings = []
  const originalWarn = console.warn
  console.warn = (message) => warnings.push(String(message))
  const ctx = {
    effect(fn) {
      return fn()
    },
    locale: {
      register() {
        return () => {}
      },
    },
    slots: {
      inject(slot, callback) {
        callback()
        return () => {}
      },
      register() {
        throw new Error('list slot "settings.action" already has an entry with id "open-document"')
      },
    },
    get() {
      return undefined
    },
  }
  let threw = false
  try {
    plugin.apply(ctx)
  } catch {
    threw = true
  } finally {
    console.warn = originalWarn
  }
  checks.check('apply: a rejected cell registration does not throw', threw === false)
  checks.check('apply: the rejection is reported on the console', warnings.some((line) => line.includes('action registration failed')))
  uninstallBrowserGlobals()
}

report('client-check', checks.results)
