// Offline check of the composed bundle: one apply() must register the server
// management routes and the status route, and the client bundle must carry both
// browser halves.
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { apply, inject } from '../index.js'

const here = path.dirname(fileURLToPath(import.meta.url))
const root = path.dirname(here)
const results = []
let failed = 0
const check = (name, fn) => {
  try {
    const problem = fn()
    if (problem) {
      failed += 1
      results.push('FAIL ' + name + ': ' + problem)
      return
    }
    results.push('ok   ' + name)
  } catch (error) {
    failed += 1
    results.push('FAIL ' + name + ': ' + error.message)
  }
}

check('host declares the services both halves need', () => {
  if (!inject.includes('connection')) return 'connection missing'
  if (!inject.includes('tools')) return 'tools missing'
})

const routes = []
const ctx = {
  inject(names, callback) {
    callback(ctx)
    return { then() {} }
  },
  effect(fn) {
    return fn()
  },
  get() {
    return undefined
  },
  logger: { debug() {}, info() {}, warn() {}, error() {} },
  connection: { fetch: { register(route) { routes.push(route); return () => {} } } },
  plugin() {
    return { then(resolve) { resolve() }, dispose: async () => {} }
  },
  tools: { schemas: () => [] },
  on() {},
}

check('one apply() registers the server and status routes', () => {
  apply(ctx, { servers: [] })
  const paths = routes.map((route) => route.path)
  if (paths.length !== 3) return 'expected 3 routes, got ' + JSON.stringify(paths)
  if (!paths.includes('/api/mcp-servers')) return 'server status route missing'
  if (!paths.includes('/api/mcp-servers/test')) return 'server test route missing'
  if (!paths.includes('/api/mcp-status')) return 'status route missing'
})

check('both methods are wired on the server routes', () => {
  const test = routes.find((route) => route.path === '/api/mcp-servers/test')
  if (!test || !Array.isArray(test.methods) || test.methods[0] !== 'POST') return 'test route is not POST'
  const status = routes.find((route) => route.path === '/api/mcp-status')
  if (!status || !Array.isArray(status.methods) || status.methods[0] !== 'GET') return 'status route is not GET'
})

check('the client bundle registers once, under the bundle name', () => {
  const client = fs.readFileSync(path.join(root, 'client.js'), 'utf8')
  const loads = client.match(/__ModuleLoader__\.load\(/g) ?? []
  // The harness builds one bundle per package and matches a single registration
  // against the package name, so two registrations would activate neither.
  if (loads.length !== 1) return 'expected 1 module registration, got ' + loads.length
  if (!client.includes("id: '@local/dsh-mcp'")) return 'the registration id is not the bundle name'
})

check('the client bundle still carries both halves', () => {
  const client = fs.readFileSync(path.join(root, 'client.js'), 'utf8')
  if (!client.includes('serversHalf.factory(require)')) return 'servers half not composed'
  if (!client.includes('statusHalf.factory(require)')) return 'status half not composed'
  if (!client.includes("'mcp-servers'")) return 'servers locale namespace missing'
  if (!client.includes("'mcp-status'")) return 'status locale namespace missing'
})

check('the bundle patch inserts exactly one row', () => {
  const patch = fs.readFileSync(path.join(root, 'cordis.patch.yml'), 'utf8')
  const ids = patch.match(/^\s+- id:/gm) ?? []
  if (ids.length !== 1) return 'expected one inserted row, got ' + ids.length
  if (!patch.includes("name: '@local/dsh-mcp'")) return 'row does not name the bundle'
})

for (const line of results) console.log(line)
console.log('')
console.log('compose-check: ' + (results.length - failed) + '/' + results.length + ' checks passed')
process.exitCode = failed === 0 ? 0 : 1
