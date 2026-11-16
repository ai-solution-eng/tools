/**
 * Offline check of the configuration contract.
 *
 * This plugin cannot `import` a Harness package, so it reimplements the two
 * pieces of the framework's schema machinery it needs:
 *
 * - a StandardSchemaV1 `Config` whose `toJSON()` emits schemastery's own
 *   `{ uid, refs }` envelope, because the Settings service reconstructs a form
 *   schema with `new Schema(json)`;
 * - volatile references built on cosmokit's registered
 *   `Symbol.for('cosmokit.volatile.write')`, so a Settings write commits into
 *   the running fiber and dispatches `loader/volatile-update`.
 *
 * Both are contracts against packages this plugin may not import, so the check
 * below loads the real schemastery and cosmokit out of the DSH installation
 * and replays the exact algorithms `@deepseek-ai/dsh-settings` and the Loader
 * run: `volatileForm` / `plainSchema` / `plainConfig` / `isVolatilePath` /
 * `applyPathOp` / `equalExceptVolatile` / `_commitVolatile`.
 *
 * Those packages are located through `DSH_INSTALL` (default: the documented
 * installation path). When they are absent the interop half is skipped and
 * only the local invariants are asserted, so the check still runs anywhere.
 */
import assert from 'node:assert/strict'
import { existsSync } from 'node:fs'
import { join } from 'node:path'
import { pathToFileURL } from 'node:url'

import {
  Config,
  SERVER_NAME_PATTERN,
  bridgeConfigOf,
  factsOf,
  isHttpUrl,
  isVolatileRef,
  mountableServers,
  normalizeRows,
  readServersField,
  resolveConfigValue,
  schemaToJSON,
  slugOf,
} from '../schema.js'

const results = []
/**
 * Run one check.
 * @param {string} name - check name.
 * @param {() => void} fn - assertions.
 * @returns {void}
 */
function check(name, fn) {
  try {
    fn()
    results.push(`ok   ${name}`)
  } catch (error) {
    results.push(`FAIL ${name}: ${error.message}`)
    process.exitCode = 1
  }
}

const WRITE = Symbol.for('cosmokit.volatile.write')

/* ------------------------------------------------------------------ *
 * Local invariants
 * ------------------------------------------------------------------ */

check('Config is a StandardSchemaV1 named as schemastery', () => {
  assert.equal(Config['~standard'].version, 1)
  assert.equal(Config['~standard'].vendor, 'schemastery')
  assert.equal(typeof Config['~standard'].validate, 'function')
})

check('Config exposes the schema surface the Settings service reads', () => {
  assert.equal(Config.type, 'object')
  assert.ok(Config.meta && typeof Config.meta === 'object')
  assert.ok(Config.dict && Config.dict.servers, 'dict.servers is required')
  assert.equal(Config.dict.servers.meta.volatile, true)
  assert.deepEqual(Config.dict.servers.meta.default, [])
  assert.equal(typeof Config.toJSON, 'function')
})

check('validate() defaults to an empty live list', () => {
  const value = Config['~standard'].validate(undefined).value
  assert.ok(value, 'validate must not reject an absent config')
  assert.ok(isVolatileRef(value.servers), 'servers must be a volatile reference')
  assert.deepEqual(value.servers.get(), [])
})

check('validate() keeps a volatile reference usable after a protocol write', () => {
  const value = Config['~standard'].validate({ servers: [{ serverName: 'github' }] }).value
  assert.deepEqual(value.servers.get().map((row) => row.serverName), ['github'])
  assert.equal(typeof value.servers[WRITE], 'function')
  value.servers[WRITE]([{ serverName: 'other' }])
  assert.deepEqual(value.servers.get().map((row) => row.serverName), ['other'])
})

check('validate() is idempotent over an already-live value', () => {
  const first = Config['~standard'].validate({ servers: [{ serverName: 'github' }] }).value
  const second = Config['~standard'].validate({ servers: first.servers }).value
  assert.deepEqual(second.servers.get(), first.servers.get())
})

check('validate() never rejects, so a stored mistake cannot fail the row', () => {
  for (const raw of [null, 42, 'nope', [], { servers: 'nope' }, { servers: [null, 7, 'x'] }]) {
    const result = Config['~standard'].validate(raw)
    assert.equal(result.issues, undefined, `validate rejected ${JSON.stringify(raw)}`)
    assert.ok(isVolatileRef(result.value.servers))
  }
})

check('toJSON() emits a ref graph with no live objects in it', () => {
  const json = Config.toJSON()
  assert.equal(typeof json.uid, 'number')
  assert.ok(json.refs && typeof json.refs === 'object')
  const root = json.refs[json.uid]
  assert.equal(root.type, 'object')
  const servers = json.refs[root.dict.servers]
  assert.equal(servers.type, 'array')
  assert.equal(servers.meta.volatile, true)
  assert.deepEqual(servers.meta.default, [])
  assert.equal(typeof servers.inner, 'number', 'nested schemas are uid references')
  assert.equal(json.refs[servers.inner].type, 'any')
  assert.equal(typeof Config.dict.servers.toJSON, 'function', 'every node serializes itself')
  assert.doesNotThrow(() => JSON.stringify(json))
})

check('normalization keeps ids stable and unique', () => {
  const rows = normalizeRows([
    { serverName: 'github' },
    { serverName: 'github' },
    { serverName: 'GitHub Tools' },
    { id: 'custom', serverName: 'custom' },
    { serverName: 'bad name!' },
  ])
  assert.deepEqual(rows.map((row) => row.id), ['github', 'github-2', 'github-tools', 'custom', 'bad-name'])
  assert.equal(slugOf(''), 'server')
})

check('normalization fills defaults and drops transport-only fields', () => {
  const [http] = normalizeRows([{ serverName: 'a', transport: 'streamable-http', url: ' https://x/mcp ', command: 'npx' }])
  assert.equal(http.url, 'https://x/mcp')
  assert.equal(http.command, '')
  assert.equal(http.toolCallTimeoutMs, 60000)
  assert.equal(http.enabled, true)
  assert.equal(http.failOnStartupError, false)
  const [stdio] = normalizeRows([{ serverName: 'b', transport: 'stdio', command: ' npx ', args: ['-y', 7], env: { TOKEN: 5 } }])
  assert.equal(stdio.command, 'npx')
  assert.deepEqual(stdio.args, ['-y', '7'])
  assert.deepEqual(stdio.env, { TOKEN: '5' })
  assert.equal(stdio.url, '')
})

check('mountableServers reports every unusable row with a reason', () => {
  const view = mountableServers([
    { id: 'ok', serverName: 'ok', url: 'https://example.com/mcp' },
    { id: 'off', serverName: 'off', enabled: false, url: 'https://example.com/mcp' },
    { id: 'noname', serverName: '' },
    { id: 'badname', serverName: 'not valid' },
    { id: 'badurl', serverName: 'badurl', url: 'ftp://example.com' },
    { id: 'dup', serverName: 'ok', url: 'https://example.com/other' },
    { id: 'stdio', serverName: 'stdio', transport: 'stdio', command: '' },
  ])
  assert.deepEqual(view.servers.map((server) => server.row.serverName), ['ok'])
  assert.deepEqual(view.problems.map((problem) => problem.id), ['noname', 'badname', 'badurl', 'dup', 'stdio'])
  for (const problem of view.problems) assert.ok(problem.message.length > 10, 'every problem carries a message')
})

check('bridgeConfigOf matches the bridge config contract', () => {
  const [http] = normalizeRows([
    { serverName: 'github', url: 'https://example.com/mcp', headers: { Authorization: 'Bearer x' }, toolCallTimeoutMs: 1000, failOnStartupError: true },
  ])
  assert.deepEqual(bridgeConfigOf(http), {
    transport: 'streamable-http',
    serverName: 'github',
    url: 'https://example.com/mcp',
    headers: { Authorization: 'Bearer x' },
    toolCallTimeoutMs: 1000,
    failOnStartupError: true,
  })
  const [stdio] = normalizeRows([{ serverName: 'local', transport: 'stdio', command: 'npx', args: ['-y', 'x'], cwd: '/tmp' }])
  assert.deepEqual(bridgeConfigOf(stdio), {
    transport: 'stdio',
    serverName: 'local',
    command: 'npx',
    args: ['-y', 'x'],
    cwd: '/tmp',
    toolCallTimeoutMs: 60000,
    failOnStartupError: false,
  })
})

check('factsOf ignores fields that do not need a new child', () => {
  const [row] = normalizeRows([{ serverName: 'a', url: 'https://example.com/mcp' }])
  const base = factsOf(row)
  assert.equal(factsOf({ ...row, enabled: false }), base)
  assert.equal(factsOf({ ...row, id: 'other' }), base)
  assert.notEqual(factsOf({ ...row, url: 'https://example.com/other' }), base)
  assert.notEqual(factsOf({ ...row, serverName: 'b' }), base)
})

check('readServersField accepts both live and plain shapes', () => {
  assert.deepEqual(readServersField(undefined), [])
  assert.deepEqual(readServersField([{ a: 1 }]), [{ a: 1 }])
  assert.deepEqual(readServersField(resolveConfigValue({ servers: [{ serverName: 'x' }] }).servers).length, 1)
})

check('URL and name contracts', () => {
  assert.equal(isHttpUrl('https://example.com/mcp'), true)
  assert.equal(isHttpUrl('http://127.0.0.1:8080/mcp'), true)
  assert.equal(isHttpUrl('example.com/mcp'), false)
  assert.equal(isHttpUrl('ftp://example.com'), false)
  assert.equal(SERVER_NAME_PATTERN.test('pcai-llm_gateway-2'), true)
  assert.equal(SERVER_NAME_PATTERN.test('a'.repeat(32)), true)
  assert.equal(SERVER_NAME_PATTERN.test('a'.repeat(33)), false)
  assert.equal(SERVER_NAME_PATTERN.test('has space'), false)
})

check('schemaToJSON is stable across calls', () => {
  assert.deepEqual(schemaToJSON(Config.dict.servers), schemaToJSON(Config.dict.servers))
})

/* ------------------------------------------------------------------ *
 * Interop with the real packages (skipped when the install is absent)
 * ------------------------------------------------------------------ */

const installRoot = process.env.DSH_INSTALL ?? '/opt/dsh/npm/lib/node_modules/@deepseek-ai/dsh'
const schemasteryPath = join(installRoot, 'node_modules', '@deepseek-ai', 'schemastery', 'lib', 'index.mjs')
const cosmokitPath = join(installRoot, 'node_modules', '@deepseek-ai', 'cosmokit', 'lib', 'index.js')

if (!existsSync(schemasteryPath) || !existsSync(cosmokitPath)) {
  console.log(`skip interop checks: no DSH installation at ${installRoot} (set DSH_INSTALL to point at one)`)
} else {
  const z = (await import(pathToFileURL(schemasteryPath).href)).default
  const cosmokit = await import(pathToFileURL(cosmokitPath).href)

  /* --- the algorithms, copied from the packages they belong to --- */
  const walkSchema = (node) => {
    delete node.meta.volatile
    for (const child of Object.values(node.dict ?? {})) walkSchema(child)
    if (node.inner) walkSchema(node.inner)
    for (const child of node.list ?? []) walkSchema(child)
  }
  const plainSchema = (schema) => {
    const result = new z(schema.toJSON())
    walkSchema(result)
    return result
  }
  const volatileForm = (schema) => {
    if (schema.meta.volatile) return plainSchema(schema)
    if (schema.type === 'object') {
      const dict = Object.fromEntries(
        Object.entries(schema.dict ?? {}).flatMap(([key, child]) => {
          const field = volatileForm(child)
          return field === undefined ? [] : [[key, field]]
        }),
      )
      return Object.keys(dict).length === 0 ? undefined : z.object(dict)
    }
    return undefined
  }
  const isVolatilePath = (schema, path) => {
    if (schema.meta.volatile) return true
    const [key, ...rest] = path
    const child = key === undefined ? undefined : schema.dict?.[key]
    return child !== undefined && isVolatilePath(child, rest)
  }
  const plainConfig = (value) => {
    if (cosmokit.isVolatile(value)) return plainConfig(value.get())
    if (Array.isArray(value)) return value.map(plainConfig)
    if (value !== null && typeof value === 'object') {
      return Object.fromEntries(Object.entries(value).map(([key, child]) => [key, plainConfig(child)]))
    }
    return value
  }
  const projectForm = (schema, value) => {
    if (schema.type === 'object' && value !== null && typeof value === 'object') {
      return Object.fromEntries(
        Object.entries(schema.dict ?? {}).flatMap(([key, child]) => {
          const field = Reflect.get(value, key)
          return field === undefined ? [] : [[key, projectForm(child, field)]]
        }),
      )
    }
    return value
  }
  const applyPathOp = (section, op, schema) => {
    const edit = (input, path, node) => {
      const [head, ...rest] = path
      if (head === undefined) return op.op === 'set' ? op.value : undefined
      const value = input === undefined ? node?.meta.default : input
      if (Array.isArray(value)) {
        const index = Number(head)
        const result = [...value]
        if (rest.length === 0 && op.op === 'unset') result.splice(index, 1)
        else result[index] = edit(value[index], rest, node?.inner)
        return result
      }
      const result = value && typeof value === 'object' && !Array.isArray(value) ? { ...value } : {}
      const child = edit(Object.hasOwn(result, head) ? result[head] : undefined, rest, node?.dict?.[head] ?? node?.inner)
      if (child === undefined) delete result[head]
      else result[head] = child
      return result
    }
    return edit(section, op.path, schema)
  }
  const isSchemastery = (schema) => schema?.['~standard']?.vendor === 'schemastery'
  const equalIgnoringVolatile = (a, b, schema, ancestors = new Set()) => {
    if (schema?.meta?.volatile) return true
    if (schema?.type !== 'object' || !schema.dict || ancestors.has(schema)) return cosmokit.deepEqual(a, b, true)
    const left = a ?? schema.meta?.default
    const right = b ?? schema.meta?.default
    if (!left || !right || typeof left !== 'object' || typeof right !== 'object') return cosmokit.deepEqual(left, right, true)
    ancestors.add(schema)
    try {
      return Object.keys({ ...left, ...right }).every((key) =>
        equalIgnoringVolatile(left[key], right[key], Object.hasOwn(schema.dict, key) ? schema.dict[key] : undefined, ancestors),
      )
    } finally {
      ancestors.delete(schema)
    }
  }
  const equalExceptVolatile = (previous, next, schema) =>
    isSchemastery(schema) ? equalIgnoringVolatile(previous, next, schema) : cosmokit.deepEqual(previous, next, true)

  check('interop: volatileForm() accepts the Config and yields a live field', () => {
    const form = volatileForm(Config)
    assert.ok(form, 'the Settings service would skip this row without a volatile field')
    assert.deepEqual(Object.keys(form.dict), ['servers'])
    assert.equal(form.dict.servers.meta.volatile, undefined, 'the form schema is the plain projection')
  })

  check('interop: plainSchema(new Schema(toJSON())) reconstructs the field', () => {
    const form = volatileForm(Config)
    const json = form.toJSON()
    const rehydrated = new z(json)
    const value = rehydrated({ servers: [{ serverName: 'github' }, { anything: [1, 2] }] })
    assert.equal(Array.isArray(value.servers), true)
    assert.equal(value.servers.length, 2)
  })

  check('interop: the client decode path validates a live value', () => {
    const form = volatileForm(Config)
    const rehydrated = new z(form.toJSON())
    const live = Config['~standard'].validate({ servers: [{ serverName: 'github', enabled: false }] }).value
    const projected = projectForm(form, plainConfig(live))
    assert.doesNotThrow(() => rehydrated(projected))
    assert.deepEqual(projected, { servers: [{ serverName: 'github', enabled: false, id: 'github', transport: 'streamable-http', url: '', headers: {}, command: '', args: [], cwd: '', env: {}, toolCallTimeoutMs: 60000, failOnStartupError: false }] })
  })

  check('interop: isVolatilePath() allows the servers field and nothing else', () => {
    assert.equal(isVolatilePath(Config, ['servers']), true)
    assert.equal(isVolatilePath(Config, []), false)
    assert.equal(isVolatilePath(Config, ['other']), false)
  })

  check('interop: applyPathOp() sets the whole list and unsets one index', () => {
    const value = { servers: [{ id: 'a' }, { id: 'b' }] }
    const set = applyPathOp(value, { op: 'set', path: ['servers'], value: [{ id: 'c' }] }, Config)
    assert.deepEqual(set, { servers: [{ id: 'c' }] })
    const unset = applyPathOp(value, { op: 'unset', path: ['servers', '0'] }, Config)
    assert.deepEqual(unset, { servers: [{ id: 'b' }] })
  })

  check('interop: a list-only edit compares equal, so the write is volatile-only', () => {
    const previous = { servers: [{ id: 'a', serverName: 'a' }] }
    const next = { servers: [{ id: 'a', serverName: 'a' }, { id: 'b', serverName: 'b' }] }
    assert.equal(equalExceptVolatile(previous, next, Config), true)
    assert.equal(equalExceptVolatile(previous, { servers: next.servers, extra: 1 }, Config), false)
  })

  check('interop: a volatile commit updates the running reference', () => {
    const config = Config['~standard'].validate({ servers: [{ id: 'a', serverName: 'a' }] }).value
    const refs = cosmokit.volatileEntries(config)
    assert.equal(refs.length, 1)
    assert.deepEqual(refs[0].path, ['servers'])
    const candidate = Config['~standard'].validate({ servers: [{ id: 'b', serverName: 'b' }] }).value
    assert.equal(cosmokit.deepEqual(config, candidate, true), true, 'ordinary values must not differ')
    const source = candidate.servers
    assert.notEqual(cosmokit.deepEqual(refs[0].ref.get(), source.get(), true), true)
    cosmokit.updateVolatile(refs[0].ref, source)
    assert.deepEqual(config.servers.get().map((row) => row.id), ['b'])
  })

  check('interop: the reference is frozen and detached from the input', () => {
    const input = [{ id: 'a', serverName: 'a' }]
    const config = Config['~standard'].validate({ servers: input }).value
    input.push({ id: 'b', serverName: 'b' })
    assert.equal(config.servers.get().length, 1)
    assert.throws(() => {
      'use strict'
      config.servers.get().push({ id: 'c' })
    })
  })
}

/* ------------------------------------------------------------------ *
 * Report
 * ------------------------------------------------------------------ */

let failed = 0
for (const line of results) {
  if (line.startsWith('FAIL')) failed += 1
  console.log(`  ${line}`)
}
console.log(failed === 0 ? `\nall ${results.length} checks passed` : `\n${failed} of ${results.length} FAILED`)
process.exit(failed === 0 ? 0 : 1)
