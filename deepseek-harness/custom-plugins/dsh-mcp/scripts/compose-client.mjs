/**
 * Compose the browser half of @local/dsh-mcp from the two halves it bundles.
 *
 * The harness builds ONE client bundle per package and matches a single
 * registration against the package name, so the halves cannot register
 * themselves. Each half is captured as a factory and the composed module
 * applies both.
 *
 * Run after editing either half:
 *   node scripts/compose-client.mjs
 *
 * `test/compose-check.mjs` fails when the checked-in bundle is stale.
 */
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const here = path.dirname(fileURLToPath(import.meta.url))
const root = path.dirname(here)
const plugins = path.dirname(root)

const HEADER = `/**
 * MCP — browser half.
 *
 * One bundle, two surfaces: the Settings page that manages MCP servers and the
 * composer-dock chip that reports which ones are connected.
 *
 * The harness builds ONE client bundle per package and matches its single
 * registration against the package name, so the two halves cannot register
 * themselves: each is captured as a factory here and the composed module
 * applies both to the same client context.
 */

`

const FOOTER = `

window.__ModuleLoader__.load({
  id: '@local/dsh-mcp',
  factory(require) {
    const servers = serversHalf.factory(require)
    const status = statusHalf.factory(require)
    return {
      inject: [...new Set([...(servers.inject ?? []), ...(status.inject ?? [])])],
      apply(ctx) {
        servers.apply(ctx)
        status.apply(ctx)
      },
    }
  },
})
`

/**
 * Read one half and rewrite its self-registration into a named factory.
 * @param {string} file - path to the half's client.js.
 * @param {string} id - module id the half registers itself with.
 * @param {string} name - binding name for the captured object.
 * @returns {string} the half as a `const <name> = { ... }` statement.
 */
function capture(file, id, name) {
  const text = fs.readFileSync(file, 'utf8')
  const header = "window.__ModuleLoader__.load({\n  id: '" + id + "',\n"
  const at = text.indexOf(header)
  if (at < 0) throw new Error('registration header not found in ' + file)
  let body = text.slice(0, at) + 'const ' + name + ' = {\n' + text.slice(at + header.length)
  body = body.trimEnd()
  if (!body.endsWith('})')) throw new Error('unexpected tail in ' + file)
  return body.slice(0, -2) + '}'
}

/**
 * Build the composed client source.
 * @returns {string} the bundle source.
 */
export function composeClient() {
  const servers = capture(path.join(plugins, 'dsh-mcp-servers', 'client.js'), '@local/dsh-mcp-servers', 'serversHalf')
  const status = capture(path.join(plugins, 'dsh-mcp-status', 'client.js'), '@local/dsh-mcp-status', 'statusHalf')
  return HEADER + servers + '\n\n' + status + FOOTER
}

/**
 * Path of the generated bundle.
 * @returns {string} absolute path.
 */
export function clientPath() {
  return path.join(root, 'client.js')
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  fs.writeFileSync(clientPath(), composeClient())
  console.log('composed client.js from both halves')
}
