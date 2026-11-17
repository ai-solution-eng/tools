/**
 * Offline test harness for the browser half.
 *
 * Loads `client.js` the way the Harness module loader does (a `window` stub plus
 * `__ModuleLoader__.load`), and renders slot components with a small React
 * stand-in that implements the hooks this plugin uses (`useState`, `useRef`,
 * `useEffect`, `useMemo`, `useCallback`, `Fragment`, `createElement`) and expands
 * function components recursively.
 *
 * The stand-in is not React: it exists so the offline checks can assert what the
 * component renders and what its handlers do — including the async Host fetch,
 * clipboard, download, and Host-editor failure paths — without a browser.
 */
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

export const here = path.dirname(fileURLToPath(import.meta.url))
export const bundleRoot = path.join(here, '..')

/** Read and evaluate `client.js` against a `window.__ModuleLoader__` stub. */
export function loadClientModule() {
  const source = fs.readFileSync(path.join(bundleRoot, 'client.js'), 'utf8')
  let loaded = null
  const window = {
    __ModuleLoader__: {
      load(definition) {
        loaded = definition
      },
    },
  }
  // eslint-disable-next-line no-new-func -- the loader evaluates the bundle the same way.
  new Function('window', source)(window)
  return loaded
}

/** Flatten nested children the way `React.createElement` does. */
function flatten(children) {
  const out = []
  const push = (value) => {
    if (value === null || value === undefined || value === false || value === true) return
    if (Array.isArray(value)) {
      for (const item of value) push(item)
      return
    }
    out.push(value)
  }
  for (const child of children) push(child)
  return out
}

/** A stand-in DOM node handed to `ref`s (focus, selection, download anchor). */
function makeNode() {
  return {
    focused: 0,
    clicked: 0,
    selected: 0,
    value: '',
    href: '',
    download: '',
    isConnected: true,
    ownerDocument: null,
    focus() {
      this.focused += 1
      if (this.ownerDocument) this.ownerDocument.activeElement = this
    },
    select() {
      this.selected += 1
    },
    click() {
      this.clicked += 1
    },
    querySelectorAll() {
      return []
    },
    querySelector() {
      return null
    },
  }
}

/**
 * Create one render runtime. Each scenario gets its own so state never leaks.
 * @returns {Record<string, any>} runtime with React, mount, flush, settle, unmount.
 */
export function createRuntime() {
  const Fragment = Symbol('Fragment')
  const scopes = new Map()
  const documentStub = { activeElement: null, execCommand: () => false }
  let currentCells = []
  let cursor = 0
  let pendingEffects = []
  let dirty = false
  let Component = null
  let currentProps = null
  let tree = null

  const scopeAt = (key) => {
    let scope = scopes.get(key)
    if (scope === undefined) {
      scope = { cells: [] }
      scopes.set(key, scope)
    }
    return scope
  }

  const hookCell = (index) => {
    if (currentCells[index] === undefined) currentCells[index] = {}
    return currentCells[index]
  }

  const depsChanged = (previous, deps) => {
    if (previous === undefined || deps === undefined || deps === null) return true
    if (previous.length !== deps.length) return true
    return deps.some((value, index) => !Object.is(value, previous[index]))
  }

  const React = {
    Fragment,
    createElement(type, props, ...children) {
      return { type, props: props || {}, children: flatten(children) }
    },
    useState(initial) {
      const cell = hookCell(cursor)
      cursor += 1
      if (!('value' in cell)) cell.value = typeof initial === 'function' ? initial() : initial
      const set = (next) => {
        const value = typeof next === 'function' ? next(cell.value) : next
        if (Object.is(value, cell.value)) return
        cell.value = value
        dirty = true
      }
      return [cell.value, set]
    },
    useRef(initial) {
      const cell = hookCell(cursor)
      cursor += 1
      if (!('current' in cell)) cell.current = initial
      return cell
    },
    useEffect(fn, deps) {
      const cell = hookCell(cursor)
      cursor += 1
      if (depsChanged(cell.deps, deps)) pendingEffects.push({ cell, fn })
      cell.deps = deps
    },
    useMemo(fn, deps) {
      const cell = hookCell(cursor)
      cursor += 1
      if (depsChanged(cell.deps, deps)) {
        cell.value = fn()
        cell.deps = deps
      }
      return cell.value
    },
    useCallback(fn, deps) {
      return React.useMemo(() => fn, deps)
    },
  }

  /** Expand one element: call function components, keep host elements as descriptors. */
  const renderNode = (node, key) => {
    if (node === null || node === undefined || typeof node === 'boolean') return null
    if (typeof node === 'string' || typeof node === 'number') return node
    if (Array.isArray(node)) return node.map((child, index) => renderNode(child, `${key}.${index}`))
    const { type, props } = node
    if (type === Fragment) {
      return node.children.map((child, index) => renderNode(child, `${key}.${index}`))
    }
    if (typeof type === 'function') {
      const scope = scopeAt(key)
      const previousCells = currentCells
      const previousCursor = cursor
      currentCells = scope.cells
      cursor = 0
      let output
      try {
        output = type(props)
      } finally {
        currentCells = previousCells
        cursor = previousCursor
      }
      return renderNode(output, key)
    }
    return {
      ...node,
      children: node.children.map((child, index) => renderNode(child, `${key}.${index}`)),
    }
  }

  /** Attach a stand-in DOM node to every `ref` in the tree (React does this for real). */
  const bindRefs = (node) => {
    if (node === null || node === undefined || typeof node !== 'object') return
    if (Array.isArray(node)) {
      for (const item of node) bindRefs(item)
      return
    }
    const ref = node.props ? node.props.ref : undefined
    if (ref && typeof ref === 'object' && 'current' in ref) {
      if (ref.__node === undefined) {
        ref.__node = makeNode()
        ref.__node.ownerDocument = documentStub
      }
      ref.current = ref.__node
    }
    for (const child of node.children || []) bindRefs(child)
  }

  const renderOnce = () => {
    pendingEffects = []
    dirty = false
    tree = renderNode({ type: Component, props: currentProps, children: [] }, 'root')
    bindRefs(tree)
    const effects = pendingEffects
    pendingEffects = []
    for (const { cell, fn } of effects) {
      if (typeof cell.cleanup === 'function') cell.cleanup()
      const cleanup = fn()
      cell.cleanup = typeof cleanup === 'function' ? cleanup : undefined
    }
    return tree
  }

  const render = () => {
    let guard = 0
    do {
      renderOnce()
      guard += 1
    } while (dirty && guard < 50)
    return tree
  }

  return {
    React,
    document: documentStub,
    get tree() {
      return tree
    },
    mount(component, props) {
      Component = component
      currentProps = props
      return render()
    },
    rerender(props) {
      currentProps = props === undefined ? currentProps : props
      return render()
    },
    flush() {
      if (dirty) return render()
      return tree
    },
    async settle(turns = 8) {
      for (let index = 0; index < turns; index += 1) {
        await new Promise((resolve) => setTimeout(resolve, 0))
        if (dirty) render()
      }
      return tree
    },
    unmount() {
      for (const scope of scopes.values()) {
        for (const cell of scope.cells) {
          if (cell && typeof cell.cleanup === 'function') {
            cell.cleanup()
            cell.cleanup = undefined
          }
        }
      }
    },
  }
}

/** Depth-first walk over a rendered tree. */
export function walk(node, visit) {
  if (node === null || node === undefined) return
  if (Array.isArray(node)) {
    for (const item of node) walk(item, visit)
    return
  }
  if (typeof node === 'string' || typeof node === 'number') return
  visit(node)
  for (const child of node.children || []) walk(child, visit)
}

/** All nodes matching a predicate. */
export function findAll(tree, predicate) {
  const found = []
  walk(tree, (node) => {
    if (predicate(node)) found.push(node)
  })
  return found
}

/** Visible text of a node (string children joined, nested elements included). */
export function textOf(node) {
  if (node === null || node === undefined) return ''
  if (typeof node === 'string') return node
  if (typeof node === 'number') return String(node)
  if (Array.isArray(node)) return node.map((item) => textOf(item)).join('')
  return (node.children || []).map((child) => textOf(child)).join('')
}

/** Find a `<button>` whose visible text equals `label`. */
export function findButton(tree, label) {
  const buttons = findAll(tree, (node) => node.type === 'button')
  return buttons.find((node) => textOf(node) === label)
}

/** Find the first element with the given className. */
export function findByClass(tree, className) {
  return findAll(tree, (node) => typeof node.props?.className === 'string' && node.props.className.split(' ').includes(className))[0]
}

/** A React-like synthetic event for handler dispatch. */
export function fakeEvent(overrides = {}) {
  const event = {
    key: undefined,
    shiftKey: false,
    defaultPrevented: false,
    propagationStopped: false,
    preventDefault() {
      event.defaultPrevented = true
    },
    stopPropagation() {
      event.propagationStopped = true
    },
    target: undefined,
    currentTarget: undefined,
    ...overrides,
  }
  return event
}

/** Install the browser globals the client half uses; returns the recorded calls. */
export function installBrowserGlobals(options = {}) {
  const record = {
    fetched: [],
    copied: [],
    createdUrls: [],
    revokedUrls: [],
    clipboardFails: options.clipboardFails === true,
  }

  globalThis.fetch = async (url, init) => {
    record.fetched.push({ url, init })
    if (typeof options.fetch === 'function') return options.fetch(url, init)
    return {
      ok: true,
      status: 200,
      json: async () => ({ ok: false, code: 'unavailable', message: 'no fetch stub configured' }),
    }
  }

  Object.defineProperty(globalThis, 'navigator', {
    configurable: true,
    value: {
      clipboard: {
        async writeText(text) {
          if (record.clipboardFails) throw new Error('clipboard blocked')
          record.copied.push(text)
        },
      },
    },
  })

  const OriginalBlob = globalThis.Blob
  globalThis.Blob = class RecordingBlob extends OriginalBlob {
    constructor(parts, blobOptions) {
      super(parts, blobOptions)
      record.lastBlobParts = parts
      record.lastBlobOptions = blobOptions
    }
  }

  globalThis.URL.createObjectURL = (blob) => {
    record.createdUrls.push(blob)
    return 'blob:config-document/1'
  }
  globalThis.URL.revokeObjectURL = (url) => {
    record.revokedUrls.push(url)
  }

  globalThis.document = {
    activeElement: null,
    execCommand: (command) => {
      record.execCommand = command
      return options.execCommandResult === true
    },
  }

  return record
}

/** Remove the globals installed above. */
export function uninstallBrowserGlobals() {
  delete globalThis.fetch
  delete globalThis.document
  if (globalThis.URL) {
    delete globalThis.URL.createObjectURL
    delete globalThis.URL.revokeObjectURL
  }
}

/** Simple assertion collector shared by the checks. */
export function createChecks() {
  const results = []
  const check = (name, ok, detail) => {
    results.push({ name, ok: ok === true, detail })
  }
  const equal = (name, actual, expected) => {
    const ok = JSON.stringify(actual) === JSON.stringify(expected)
    results.push({ name, ok, detail: ok ? undefined : `expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}` })
  }
  const includes = (name, haystack, needle) => {
    const ok = typeof haystack === 'string' && haystack.includes(needle)
    results.push({ name, ok, detail: ok ? undefined : `expected ${JSON.stringify(haystack)} to include ${JSON.stringify(needle)}` })
  }
  return { results, check, equal, includes }
}

/** Print the results and exit non-zero on the first failure. */
export function report(label, results) {
  let failed = 0
  for (const result of results) {
    if (result.ok) {
      console.log(`  ok   ${result.name}`)
    } else {
      failed += 1
      console.log(`  FAIL ${result.name}${result.detail ? ` — ${result.detail}` : ''}`)
    }
  }
  console.log(`\n${label}: ${results.length - failed}/${results.length} checks passed`)
  if (failed > 0) process.exitCode = 1
  return failed === 0
}
