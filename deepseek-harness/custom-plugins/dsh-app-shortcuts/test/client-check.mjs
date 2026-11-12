// Offline check of the browser half: loads client.js the way the Harness
// module loader does, runs apply() against a stub context, renders the
// component with stub props, and asserts the produced tree.
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const here = path.dirname(fileURLToPath(import.meta.url))
const source = fs.readFileSync(path.join(here, '..', 'client.js'), 'utf8')

// --- React stub: createElement returns plain descriptors we can walk. -------
const Fragment = Symbol('Fragment')
const React = {
  Fragment,
  createElement(type, props, ...children) {
    return { type, props: props || {}, children: children.flat(Infinity).filter((c) => c !== null && c !== undefined && c !== false) }
  },
}

// --- window.__ModuleLoader__ stub -----------------------------------------
let loaded = null
const window = { __ModuleLoader__: { load(def) { loaded = def } } }
new Function('window', source)(window)

const checks = []
const check = (name, ok) => { checks.push({ name, ok }) }

check('module registered', !!loaded)
check('module id is the package name', loaded?.id === '@local/dsh-app-shortcuts')

const plugin = loaded.factory((name) => {
  if (name === 'react') return React
  throw new Error(`unexpected require: ${name}`)
})
check('factory returns inject + apply', Array.isArray(plugin.inject) && typeof plugin.apply === 'function')
check('injects slots and locale', plugin.inject.includes('slots') && plugin.inject.includes('locale'))

// --- stub ctx --------------------------------------------------------------
const registrations = []
const dictionaries = []
const effects = []
const ctx = {
  effect(fn, label) { effects.push(label); return fn() },
  locale: { register(ns, dict) { dictionaries.push({ ns, dict }); return () => {} } },
  slots: {
    inject(slot, fn) { fn(); return () => {} },
    register(options, component) { registrations.push({ options, component }); return () => {} },
  },
}

plugin.apply(ctx)

check('registers one dictionary', dictionaries.length === 1)
check('dictionary namespace is app-shortcuts', dictionaries[0]?.ns === 'app-shortcuts')
check('dictionary has en and zh', !!dictionaries[0]?.dict?.en && !!dictionaries[0]?.dict?.zh)
check('dictionary has login strings', !!dictionaries[0]?.dict?.en?.['login.label'] && !!dictionaries[0]?.dict?.zh?.['login.label'])
check('registers exactly one slot cell', registrations.length === 1)

const reg = registrations[0]
check('registers into sidebar.footer.action', reg?.options?.name === 'sidebar.footer.action')
check('cell id is app-shortcuts', reg?.options?.id === 'app-shortcuts')
check('cell order is 20', reg?.options?.order === 20)
check('cell carries the locale namespace', reg?.options?.locale === 'app-shortcuts')

// --- render, wide ----------------------------------------------------------
const t = (key) => dictionaries[0].dict.en[key] ?? key
const wide = reg.component({ wide: true, t })
const wideDiv = wide.children.find((c) => c?.type === 'div')
const wideLinks = wideDiv.children

check('wide: three anchors', wideLinks.length === 3)
check('wide: terminal href', wideLinks[0].props.href === '/terminal')
check('wide: data-manager href', wideLinks[1].props.href === '/data-manager')
check('wide: login href is the router login page', wideLinks[2].props.href === '/')
check('wide: all open a new tab', wideLinks.every((a) => a.props.target === '_blank' && a.props.rel === 'noopener'))
check('wide: all have accessible labels', wideLinks.every((a) => typeof a.props['aria-label'] === 'string' && a.props['aria-label'].length > 0))
check('wide: all show a text label', wideLinks.every((a) => a.children.some((c) => c?.props?.className === 'dsh-app-shortcuts__label')))
check('wide: all render a glyph', wideLinks.every((a) => a.children.some((c) => c?.props?.className === 'dsh-app-shortcuts__glyph')))

// --- render, rail ----------------------------------------------------------
const rail = reg.component({ wide: false, t })
const railDiv = rail.children.find((c) => c?.type === 'div')
check('rail: three anchors', railDiv.children.length === 3)
check('rail: no text labels', railDiv.children.every((a) => !a.children.some((c) => c?.props?.className === 'dsh-app-shortcuts__label')))
check('rail: class switches to rail', String(railDiv.props.className).includes('dsh-app-shortcuts--rail'))

// --- missing t (defensive) -------------------------------------------------
const noT = reg.component({ wide: true })
check('renders without a t function', noT.children.find((c) => c?.type === 'div').children.length === 3)

// --- report ----------------------------------------------------------------
let failed = 0
for (const { name, ok } of checks) {
  if (!ok) failed++
  console.log(`  ${ok ? 'PASS' : 'FAIL'}  ${name}`)
}
console.log(failed === 0 ? `\nall ${checks.length} checks passed` : `\n${failed} of ${checks.length} FAILED`)
process.exit(failed === 0 ? 0 : 1)
