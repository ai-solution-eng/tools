// Offline check: the sibling scan picks bundles, skips plain packages and never
// installs itself. No Harness, no network.
import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

import { discoverSiblings, resolveSettings } from '../index.js'

const here = path.dirname(fileURLToPath(import.meta.url))
const self = path.dirname(here)
const pluginsRoot = path.dirname(self)

const results = []
let failed = 0
const check = (name, fn) => {
  try {
    fn()
    results.push('ok   ' + name)
  } catch (error) {
    failed += 1
    results.push('FAIL ' + name + ': ' + error.message)
  }
}

check('a bundle sibling is picked up', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'installer-'))
  fs.mkdirSync(path.join(root, 'alpha'))
  fs.writeFileSync(path.join(root, 'alpha', 'package.json'), JSON.stringify({ name: '@local/alpha', dsh: { bundle: { patch: './cordis.patch.yml' } } }))
  const found = discoverSiblings(root, path.join(root, 'installer'))
  assert.deepEqual(found.map((entry) => entry.name), ['@local/alpha'])
  fs.rmSync(root, { recursive: true, force: true })
})

check('a plain package is skipped', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'installer-'))
  fs.mkdirSync(path.join(root, 'plain'))
  fs.writeFileSync(path.join(root, 'plain', 'package.json'), JSON.stringify({ name: '@local/plain' }))
  assert.deepEqual(discoverSiblings(root, path.join(root, 'installer')), [])
  fs.rmSync(root, { recursive: true, force: true })
})

check('a directory without a manifest is skipped', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'installer-'))
  fs.mkdirSync(path.join(root, 'empty'))
  assert.deepEqual(discoverSiblings(root, path.join(root, 'installer')), [])
  fs.rmSync(root, { recursive: true, force: true })
})

check('the installer never installs itself', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'installer-'))
  const selfDir = path.join(root, 'installer')
  fs.mkdirSync(selfDir)
  fs.writeFileSync(path.join(selfDir, 'package.json'), JSON.stringify({ name: '@local/installer', dsh: { bundle: {} } }))
  assert.deepEqual(discoverSiblings(root, selfDir), [])
  fs.rmSync(root, { recursive: true, force: true })
})

check('the real folder yields siblings but not this plugin', () => {
  const found = discoverSiblings(pluginsRoot, self)
  assert.ok(found.length > 0, 'expected at least one sibling bundle')
  assert.equal(found.some((entry) => entry.dir === path.resolve(self)), false, 'the installer listed itself')
  for (const entry of found) assert.ok(fs.existsSync(path.join(entry.dir, 'package.json')))
})

check('settings default and clamp', () => {
  const defaults = resolveSettings(undefined)
  assert.equal(defaults.installOnStart, true)
  assert.equal(defaults.delayMs, 2000)
  assert.equal(resolveSettings({ installOnStart: false }).installOnStart, false)
  assert.equal(resolveSettings({ delayMs: -5 }).delayMs, 0)
  assert.equal(resolveSettings({ delayMs: 999999 }).delayMs, 60000)
  assert.equal(resolveSettings({ delayMs: 'soon' }).delayMs, 2000)
})

for (const line of results) console.log(line)
console.log('')
console.log('installer-check: ' + (results.length - failed) + '/' + results.length + ' checks passed')
process.exitCode = failed === 0 ? 0 : 1
