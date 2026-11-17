// Offline check that the locale files and the runtime dictionaries cannot drift:
// the JSON files shipped for Plugin Manager/Settings display are the same strings
// the running component renders, and every key the code asks for exists in both
// languages.
import fs from 'node:fs'
import path from 'node:path'
import { bundleRoot, createChecks, createRuntime, loadClientModule, report } from './harness.mjs'

const checks = createChecks()
const NS = 'config-document'

/** Run the browser half against a stub context and return its dictionaries. */
function dictionaries() {
  const runtime = createRuntime()
  const loaded = loadClientModule()
  const plugin = loaded.factory((name) => {
    if (name === 'react') return runtime.React
    throw new Error(`unexpected require: ${name}`)
  })
  const captured = []
  const ctx = {
    effect(fn) {
      return fn()
    },
    locale: {
      register(namespace, dictionary) {
        captured.push({ namespace, dictionary })
        return () => {}
      },
    },
    slots: {
      inject(slot, callback) {
        callback()
        return () => {}
      },
      register() {
        return () => {}
      },
    },
    get() {
      return undefined
    },
  }
  plugin.apply(ctx)
  return captured[0]
}

const captured = dictionaries()
const clientSource = fs.readFileSync(path.join(bundleRoot, 'client.js'), 'utf8')
const enFile = JSON.parse(fs.readFileSync(path.join(bundleRoot, 'locale/en.json'), 'utf8'))
const zhFile = JSON.parse(fs.readFileSync(path.join(bundleRoot, 'locale/zh.json'), 'utf8'))

checks.equal('dictionary namespace matches the registration', captured?.namespace, NS)
const EN = captured?.dictionary?.en ?? {}
const ZH = captured?.dictionary?.zh ?? {}

const withoutMeta = (file) => {
  const copy = { ...file }
  delete copy.meta
  return copy
}

checks.equal('locale/en.json UI strings equal the runtime EN dictionary', withoutMeta(enFile), EN)
checks.equal('locale/zh.json UI strings equal the runtime ZH dictionary', withoutMeta(zhFile), ZH)

const enKeys = Object.keys(EN).sort()
const zhKeys = Object.keys(ZH).sort()
const fileEnKeys = Object.keys(withoutMeta(enFile)).sort()
const fileZhKeys = Object.keys(withoutMeta(zhFile)).sort()

checks.equal('both languages define the same keys', enKeys, zhKeys)
checks.equal('locale/en.json defines exactly the runtime keys', fileEnKeys, enKeys)
checks.equal('locale/zh.json defines exactly the runtime keys', fileZhKeys, zhKeys)

// Every key the code asks for exists in both dictionaries.
const asked = new Set()
for (const match of clientSource.matchAll(/\bt\(\s*'([^']+)'/g)) asked.add(match[1])
for (const match of clientSource.matchAll(/\bt\(\s*"([^"]+)"/g)) asked.add(match[1])
const missing = [...asked].filter((key) => !(key in EN) || !(key in ZH))
checks.equal('every t() key exists in both languages', missing, [])
checks.check('the code asks for more than a handful of keys', asked.size > 20)

// No dictionary key is dead weight.
const unused = enKeys.filter((key) => !asked.has(key))
checks.equal('no unused dictionary keys', unused, [])

// Placeholders agree between languages for the same key.
const placeholders = (value) => [...String(value).matchAll(/\{(\w+)\}/g)].map((match) => match[1]).sort()
const mismatched = enKeys.filter((key) => JSON.stringify(placeholders(EN[key])) !== JSON.stringify(placeholders(ZH[key])))
checks.equal('both languages use the same placeholders', mismatched, [])

// The shipped label must be preserved so the button reads as the same one.
checks.equal('button label matches the shipped English label', EN['action.open'], 'Open configuration file')
checks.equal('button label matches the shipped Chinese label', ZH['action.open'], '打开配置文件')

report('locale-parity', checks.results)
