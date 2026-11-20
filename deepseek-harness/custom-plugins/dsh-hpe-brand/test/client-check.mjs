// Offline check of the browser half: loads client.js the way the Harness
// module loader does, runs apply() against a stub context, renders both
// components with stub slot props, and asserts the produced tree and the
// registered token layer. No network, no running Harness.
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
check('module id is the package name', loaded?.id === '@local/dsh-hpe-brand')

const plugin = loaded.factory((name) => {
  if (name === 'react') return React
  throw new Error(`unexpected require: ${name}`)
})
check('factory returns inject + apply', Array.isArray(plugin.inject) && typeof plugin.apply === 'function')

// The whole point of the split: `slots` must not be gated behind `theme`, or a
// missing theme service leaves the plugin pending and therefore silent.
check('injects slots only (theme must not gate the seats)', plugin.inject.length === 1 && plugin.inject[0] === 'slots')

// --- stub ctx --------------------------------------------------------------
const registrations = []
const slotInjections = []
const serviceInjections = []
const effects = []
const tokenLayers = []
let tokenLayerDisposed = 0

function makeCtx(withTheme) {
  const effect = (fn, label) => {
    const dispose = fn()
    effects.push({ label, dispose })
    return dispose
  }
  const theme = { overrideTokens(sourceName, tokens) {
    tokenLayers.push({ source: sourceName, tokens })
    return () => { tokenLayerDisposed++ }
  } }
  const ctx = {
    effect,
    // The theme service is read off the context, not declared as a hard inject.
    get(name) { return name === 'theme' && withTheme ? theme : undefined },
    inject(deps, callback) {
      serviceInjections.push(deps)
      if (withTheme) callback({ effect, theme })
    },
    slots: {
      inject(slot, fn) {
        slotInjections.push(slot)
        fn()
        return () => {}
      },
      register(options, component) {
        registrations.push({ options, component })
        return () => {}
      },
    },
  }
  return ctx
}

plugin.apply(makeCtx(true))

// --- theme layer -----------------------------------------------------------
check('reads the theme service off the context, not through a hard inject',
  serviceInjections.length === 0)
check('registers exactly one theme layer', tokenLayers.length === 1)
check('theme layer source is the package id', tokenLayers[0]?.source === '@local/dsh-hpe-brand')

const tokens = tokenLayers[0]?.tokens || {}
const expectedTokens = [
  '--dsw-alias-brand-primary',
  '--dsw-alias-brand-primary-new-colorprimary-new-color',
  '--dsw-alias-brand-text',
  '--dsw-alias-button-primary-fill',
  '--dsw-alias-button-primary-hover',
  '--dsw-alias-state-business-primary',
  '--dsw-alias-link',
  '--dsw-alias-state-business-tertiary',
]
check('overrides exactly the eight accent tokens',
  Object.keys(tokens).sort().join() === expectedTokens.slice().sort().join())
check('every token carries a light and a dark value',
  expectedTokens.every((name) => {
    const value = tokens[name]
    return typeof value?.light === 'string' && typeof value?.dark === 'string'
  }))
check('fill tokens are hex colours, not bare strings',
  expectedTokens.every((name) => /^#[0-9a-f]{6}$/i.test(tokens[name]?.light) && /^#[0-9a-f]{6}$/i.test(tokens[name]?.dark)))
check('brand-primary light is HPE green', tokens['--dsw-alias-brand-primary']?.light === '#01a982')
check('brand-primary dark is the brighter HPE green', tokens['--dsw-alias-brand-primary']?.dark === '#02c99a')
check('brand-text uses the text-grade green on light', tokens['--dsw-alias-brand-text']?.light === '#017a5e')
check('the primary button fill is the text-grade green, so its white label clears AA',
  tokens['--dsw-alias-button-primary-fill']?.light === '#017a5e')
check('the mark keeps the bright brand green',
  tokens['--dsw-alias-brand-primary']?.light !== tokens['--dsw-alias-button-primary-fill']?.light)
check('focus ring and links follow the accent',
  tokens['--dsw-alias-state-business-primary']?.light === '#017a5e'
  && tokens['--dsw-alias-link']?.light === '#017a5e')
check('the selection wash is a green tint',
  tokens['--dsw-alias-state-business-tertiary']?.light === '#e6f7f1'
  && tokens['--dsw-alias-state-business-tertiary']?.dark === '#123a30')
check('primary-button hover moves with the fill',
  tokens['--dsw-alias-button-primary-hover']?.light !== '#43454a'
  && tokens['--dsw-alias-button-primary-hover']?.dark !== '#ebeef2')

// --- slot registrations ----------------------------------------------------
check('registers exactly the three seats', registrations.length === 3)
const byName = (name) => registrations.find((r) => r.options?.name === name)
const markReg = byName('sidebar.brand.mark')
const nameReg = byName('sidebar.brand.name')
const heroReg = byName('conversation.hero.brand.mark')
check('registers into sidebar.brand.mark', !!markReg)
check('registers into sidebar.brand.name', !!nameReg)
check('registers into conversation.hero.brand.mark', !!heroReg)
check('single slots take no id/order/locale options',
  [markReg, nameReg, heroReg].every((r) => Object.keys(r.options).join() === 'name'))
check('each seat gets its own injection (one failure cannot cascade)',
  slotInjections.length === 3
  && slotInjections.includes('sidebar.brand.mark')
  && slotInjections.includes('sidebar.brand.name')
  && slotInjections.includes('conversation.hero.brand.mark'))

// --- render: sidebar mark (size supplied) ----------------------------------
const svg = markReg.component({ size: 24 })
check('mark is an svg', svg.type === 'svg')
check('mark is sized by the owner prop', svg.props.width === 24 && svg.props.height === 24)
check('mark viewBox is 24 units', svg.props.viewBox === '0 0 24 24')
check('mark is decorative', svg.props['aria-hidden'] === true && svg.props.focusable === false)
check('mark has a single path', svg.children.length === 1 && svg.children[0].type === 'path')
check('mark path is the HPE element geometry, inset one unit',
  svg.children[0].props.d === 'M6.9 1 H22.1 L17.1 23 H1.9 Z')

// A presentation attribute does not parse var(); the fill has to be a style
// property or the parallelogram paints black.
check('mark fill is a style property, not a presentation attribute',
  svg.children[0].props.fill === undefined)
check('mark fill resolves the accent token through CSS',
  svg.children[0].props.style?.fill === 'var(--dsw-alias-brand-primary)')

// --- render: hero mark (size + host className) -----------------------------
const hero = heroReg.component({ size: 34, className: 'host-fish-class' })
check('hero mark honours the requested size', hero.props.width === 34 && hero.props.height === 34)
check('hero mark keeps the host geometry class', hero.props.className === 'host-fish-class')

// --- render: mark without props (defensive) --------------------------------
const bare = markReg.component({})
check('mark falls back to 24 when no size is given', bare.props.width === 24)

// --- render: wordmark ------------------------------------------------------
const wordmark = nameReg.component({})
const styleTag = wordmark.children.find((c) => c?.type === 'style')
const wordSpan = wordmark.children.find((c) => c?.props?.className === 'dsh-hpe-brand')
check('wordmark ships its own style element', typeof styleTag?.children?.[0] === 'string')
check('wordmark style is scoped to the plugin prefix', styleTag.children[0].includes('.dsh-hpe-brand{'))
const cssText = styleTag.children[0]
const colourValues = [...cssText.matchAll(/color:([^;}]+)/g)].map((match) => match[1].trim())
check('every colour in the wordmark style comes from a theme token',
  colourValues.length === 2 && colourValues.every((value) => value.startsWith('var(--dsw-alias-')))
check('the accent word ellipsises instead of clipping mid-glyph',
  /\.dsh-hpe-brand__accent\{[^}]*text-overflow:ellipsis/.test(cssText))
check('wordmark renders one inline group', !!wordSpan)
check('wordmark has two words', wordSpan.children.length === 2)
check('wordmark first word is the neutral product word',
  wordSpan.children[0].props.className === 'dsh-hpe-brand__product' && wordSpan.children[0].children[0] === 'DeepSeek')
check('wordmark second word carries the accent',
  wordSpan.children[1].props.className === 'dsh-hpe-brand__accent' && wordSpan.children[1].children[0] === 'Harness')
check('wordmark adds no interactive element', !wordmark.children.some((c) => c?.type === 'button' || c?.type === 'a'))

// --- effects are labelled and disposable -----------------------------------
check('one labelled effect, installed for the accent layer',
  effects.length === 1 && effects[0].label === 'hpe-brand: accent tokens')
check('token effect returns a disposer', typeof effects[0].dispose === 'function')
effects[0].dispose()
check('disposing the token effect removes the layer', tokenLayerDisposed === 1)

// --- no theme service at all (defensive) -----------------------------------
registrations.length = 0
let threw = null
try { plugin.apply(makeCtx(false)) } catch (error) { threw = error }
check('apply survives a context without the theme service', threw === null)
check('the seats still register without a theme service', registrations.length === 3)
check('no token layer is registered without a theme service', tokenLayers.length === 1)

// --- report ----------------------------------------------------------------
let failed = 0
for (const { name, ok } of checks) {
  if (!ok) failed++
  console.log(`  ${ok ? 'PASS' : 'FAIL'}  ${name}`)
}
console.log(failed === 0 ? `\nall ${checks.length} checks passed` : `\n${failed} of ${checks.length} FAILED`)
process.exit(failed === 0 ? 0 : 1)
