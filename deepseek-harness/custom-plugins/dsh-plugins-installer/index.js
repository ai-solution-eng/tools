/**
 * Plugins installer — Host half.
 *
 * Installs every sibling bundle of this plugin's own directory, so one install
 * brings a whole `custom-plugins` folder into the profile. Each sibling is
 * installed as its own bundle, which is the point: the Plugins page then lists
 * them individually and each one can be switched on or off.
 *
 * The pass is idempotent — a sibling already present in the profile is skipped —
 * so it survives being interrupted by the Host restarting, which a profile write
 * itself causes. The next boot simply continues where the last one stopped.
 *
 * @module
 */

import { readdirSync, readFileSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

/** Accepted settings and their defaults. */
const DEFAULT_SETTINGS = {
  /** Install missing siblings once the Host has settled. */
  installOnStart: true,
  /** Wait this long after activation before touching the profile. */
  delayMs: 2000,
}

/** Numeric bounds applied to configured settings. */
const BOUNDS = { delayMs: [0, 60000] }

/** Nothing is required: the manager is looked up when the pass runs. */
export const inject = []

/**
 * Coerce a thrown value into one short line of text.
 * @param {unknown} error - thrown value.
 * @returns {string} readable message.
 */
function messageOf(error) {
  if (error === undefined || error === null) return 'unknown error'
  if (typeof error === 'string') return error
  if (typeof error.message === 'string' && error.message) return error.message
  try {
    return String(error)
  } catch {
    return 'unknown error'
  }
}

/**
 * Normalize a raw row config into accepted settings.
 * @param {unknown} config - row config.
 * @returns {typeof DEFAULT_SETTINGS} resolved settings.
 */
function resolveSettings(config) {
  const raw = config && typeof config === 'object' ? config : {}
  const settings = { ...DEFAULT_SETTINGS }
  if (typeof raw.installOnStart === 'boolean') settings.installOnStart = raw.installOnStart
  if (typeof raw.delayMs === 'number' && Number.isFinite(raw.delayMs)) {
    const [min, max] = BOUNDS.delayMs
    settings.delayMs = Math.min(max, Math.max(min, Math.floor(raw.delayMs)))
  }
  return settings
}

/**
 * Every sibling directory that declares a bundle.
 *
 * A directory without a `dsh.bundle` patch is a plain package, not a plugin, and
 * is skipped — the manager would refuse it anyway.
 * @param {string} root - directory to scan.
 * @param {string} selfDir - this plugin's own directory, never installed.
 * @returns {Array<{ dir: string, name: string }>} candidates, ordered by name.
 */
export function discoverSiblings(root, selfDir) {
  const found = []
  const self = resolve(selfDir)
  let entries
  try {
    entries = readdirSync(root, { withFileTypes: true })
  } catch {
    return found
  }
  for (const entry of entries) {
    if (!entry.isDirectory()) continue
    const dir = resolve(root, entry.name)
    if (dir === self) continue
    let manifest
    try {
      manifest = JSON.parse(readFileSync(join(dir, 'package.json'), 'utf8'))
    } catch {
      continue
    }
    if (!manifest || typeof manifest !== 'object') continue
    if (!manifest.dsh || !manifest.dsh.bundle) continue
    if (typeof manifest.name !== 'string' || manifest.name === '') continue
    found.push({ dir, name: manifest.name })
  }
  return found.sort((a, b) => a.name.localeCompare(b.name))
}

/**
 * Register the installer.
 * @param {any} ctx - plugin context.
 * @param {unknown} config - row config.
 */
export function apply(ctx, config) {
  const settings = resolveSettings(config)
  const here = dirname(fileURLToPath(import.meta.url))
  const root = dirname(here)

  /**
   * Log one line without ever throwing out of the plugin.
   * @param {string} text - message.
   * @param {unknown} [error] - optional cause.
   * @returns {void}
   */
  const log = (text, error) => {
    try {
      ctx.logger?.info?.('plugins-installer: ' + text)
      if (error !== undefined) ctx.logger?.warn?.('plugins-installer: ' + messageOf(error))
    } catch {
      // Diagnostics must never break activation.
    }
  }

  if (!settings.installOnStart) {
    log('installOnStart is false; nothing to do')
    return
  }

  /**
   * Install every sibling the profile does not already declare.
   * @returns {Promise<void>} settlement; never rejects.
   */
  const pass = async (manager) => {
    if (!manager || typeof manager.installBundle !== 'function') {
      log('the plugin manager is not mounted; nothing can be installed from here')
      return
    }
    let bundles = []
    try {
      bundles = await manager.listBundles()
    } catch (error) {
      log('could not read the installed bundles', error)
      return
    }
    const installed = new Set(
      (Array.isArray(bundles) ? bundles : [])
        .filter((bundle) => bundle && bundle.installed === true)
        .map((bundle) => bundle.name),
    )
    const candidates = discoverSiblings(root, here)
    const missing = candidates.filter((candidate) => !installed.has(candidate.name))
    if (missing.length === 0) {
      log(candidates.length + ' sibling plugin(s) already installed')
      return
    }
    log('installing ' + missing.length + ' of ' + candidates.length + ' sibling plugin(s)')
    for (const candidate of missing) {
      try {
        log('installing ' + candidate.name)
        await manager.installBundle('link:' + candidate.dir, { activate: true })
      } catch (error) {
        const message = messageOf(error)
        // A write that already landed reports already-installed; that is success.
        if (/already-installed/.test(message)) continue
        log('could not install ' + candidate.name + ': ' + message)
      }
    }
  }

  // Wait for the manager service, not a guessed delay: this row can activate
  // before plugin-manager does, and a pass that ran then would find nothing.
  try {
    ctx.inject(['pluginManager'], (child) => {
      child.effect(() => {
        const timer = setTimeout(() => {
          void pass(child.pluginManager)
        }, settings.delayMs)
        return () => clearTimeout(timer)
      }, 'plugins-installer: startup pass')
    })
  } catch (error) {
    log('could not schedule the startup pass', error)
  }
}

export { resolveSettings }
