/**
 * MCP — Host half, one package for both jobs.
 *
 * Composes the two existing halves instead of copying them:
 *
 * - `../dsh-mcp-servers/index.js` owns the server list (mounts one
 *   `@deepseek-ai/dsh-mcp-client` child per configured server, serves the
 *   settings page routes).
 * - `../dsh-mcp-status/index.js` owns the status snapshot (which MCP servers
 *   are connected, and the upstream servers behind a federating one).
 *
 * Both receive the same row config: the server half reads `servers`, the
 * status half reads its own keys (`routePath`, TTLs, catalog tool names), so
 * one row configures both without collision.
 *
 * The relative imports are deliberate. A `link:`-installed package resolves
 * its own dependencies through the profile, which a sibling package in the
 * same directory tree is not guaranteed to be; a relative specifier is a
 * filesystem path and always resolves.
 *
 * @module
 */

import { apply as applyServers } from './src/servers/index.js'
import { apply as applyStatus } from './src/status/index.js'

/**
 * The row's config schema, re-exported from the server half.
 *
 * The Loader needs it to validate and normalise this row's config, and the
 * Settings service needs it to expose the row as an editable form. Without this
 * export the row still mounts (the server half reads a plain `servers` array),
 * but the page reports "Configuration is not available" because no schema is
 * registered for the row.
 */
export { Config } from './src/servers/index.js'

/**
 * Services required before either half can answer.
 *
 * `tools` comes from the status half: without it, reading the tool registry
 * throws and every server is reported as unregistered.
 */
export const inject = ['connection', 'tools']

/**
 * Apply both halves to the same context.
 * @param {import('@deepseek-ai/cordis').Context} ctx - plugin context.
 * @param {unknown} config - row config shared by both halves.
 * @returns {void}
 */
export function apply(ctx, config) {
  applyServers(ctx, config)
  applyStatus(ctx, config)
}
