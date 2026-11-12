/**
 * MCP status — browser half.
 *
 * Renders one ambient row below the composer: a status dot, the connected
 * count, and — when opened — every configured MCP server with its transport,
 * endpoint, live probe result, and the upstream MCP servers a federating
 * server reports.
 *
 * The data comes from the Host route the package's `index.js` registers
 * (`GET /api/mcp-status`), polled on a timer while the row is mounted. The
 * request headers that carry the MCP credential never leave the Host.
 *
 * Styling uses only `--dsw-alias-*` theme tokens; the markup is the plugin's
 * own, so nothing here depends on a Harness Client package beyond React.
 */
window.__ModuleLoader__.load({
  id: '@local/dsh-mcp-status',
  factory(require) {
    const React = require('react')
    const h = React.createElement

    /** Locale namespace owned by this plugin. */
    const NS = 'mcp-status'
    /** Host route that answers the snapshot. */
    const ENDPOINT = 'api/mcp-status'
    /** How often the row re-reads the snapshot. */
    const POLL_MS = 20000
    /** Upstream rows rendered per connection before summarizing the rest. */
    const MAX_UPSTREAM_ROWS = 32

    const EN = {
      'chip.label': 'MCP',
      'chip.connected': '{connected} of {total} connected',
      'chip.configured': '{total} configured',
      'chip.unavailable': 'status unavailable',
      'chip.upstream': '{count} servers',
      'chip.expand': 'Show MCP server details',
      'chip.collapse': 'Hide MCP server details',
      'state.connected': 'connected',
      'state.disconnected': 'unreachable',
      'state.disabled': 'disabled',
      'state.configured': 'configured',
      'state.unknown': 'unknown',
      'state.pluginFailed': 'bridge plugin failed',
      'meta.tools': '{count} tools',
      'meta.toolOne': '1 tool',
      'meta.latency': '{ms} ms',
      'upstream.title': 'Servers behind {server}',
      'upstream.tools': '{count} tools',
      'upstream.toolOne': '1 tool',
      'upstream.more': '+{count} more',
      'action.refresh': 'Refresh',
      'note.stdio': 'stdio servers are not probed',
    }

    /** Chinese dictionary; every other language falls back to English. */
    const ZH = {
      'chip.label': 'MCP',
      'chip.connected': '{connected}/{total} 已连接',
      'chip.configured': '已配置 {total} 个',
      'chip.unavailable': '状态不可用',
      'chip.upstream': '{count} 个服务器',
      'chip.expand': '展开 MCP 服务器详情',
      'chip.collapse': '收起 MCP 服务器详情',
      'state.connected': '已连接',
      'state.disconnected': '无法连接',
      'state.disabled': '已禁用',
      'state.configured': '已配置',
      'state.unknown': '未知',
      'state.pluginFailed': '桥接插件失败',
      'meta.tools': '{count} 个工具',
      'meta.toolOne': '1 个工具',
      'meta.latency': '{ms} 毫秒',
      'upstream.title': '{server} 背后的服务器',
      'upstream.tools': '{count} 个工具',
      'upstream.toolOne': '1 个工具',
      'upstream.more': '另有 {count} 个',
      'action.refresh': '刷新',
      'note.stdio': 'stdio 服务器不进行探测',
    }

    const CSS = [
      '.mcp-status-dock { display: flex; flex-direction: column; gap: 6px; font-size: 12px; line-height: 1.45; color: var(--dsw-alias-label-secondary); }',
      '.mcp-status-dock__bar { display: inline-flex; align-items: center; gap: 6px; align-self: flex-start; padding: 2px 8px 2px 6px; margin: 0; border: 1px solid transparent; border-radius: 999px; background: transparent; color: inherit; font: inherit; cursor: pointer; }',
      '.mcp-status-dock__bar:hover { background: var(--dsw-alias-bg-layer-2); }',
      '.mcp-status-dock__bar:focus-visible { outline: 2px solid var(--dsw-alias-brand-primary); outline-offset: 1px; }',
      '.mcp-status-dock__label { font-weight: 600; color: var(--dsw-alias-label-primary); letter-spacing: .02em; }',
      '.mcp-status-dock__chevron { flex: none; opacity: .65; transition: transform .15s ease; }',
      '.mcp-status-dock__bar[aria-expanded="true"] .mcp-status-dock__chevron { transform: rotate(90deg); }',
      '.mcp-status-dock__dot { flex: none; width: 7px; height: 7px; border-radius: 50%; background: var(--dsw-alias-state-idle-primary); }',
      '.mcp-status-dock__dot[data-tone="ok"] { background: var(--dsw-alias-state-success-primary); }',
      '.mcp-status-dock__dot[data-tone="warn"] { background: var(--dsw-alias-state-warn-primary); }',
      '.mcp-status-dock__dot[data-tone="error"] { background: var(--dsw-alias-state-error-primary); }',
      '.mcp-status-dock__dot[data-tone="idle"] { background: var(--dsw-alias-state-idle-primary); }',
      '.mcp-status-dock__dot--small { width: 5px; height: 5px; }',
      '.mcp-status-dock__panel { display: flex; flex-direction: column; gap: 10px; padding: 10px 12px; border: 1px solid var(--dsw-alias-border-l1); border-radius: 10px; background: var(--dsw-alias-bg-layer-1); }',
      '.mcp-status-dock__row { display: flex; flex-direction: column; gap: 3px; min-width: 0; }',
      '.mcp-status-dock__row-head { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; min-width: 0; }',
      '.mcp-status-dock__name { color: var(--dsw-alias-label-primary); font-weight: 600; overflow-wrap: anywhere; }',
      '.mcp-status-dock__muted { color: var(--dsw-alias-label-secondary); }',
      '.mcp-status-dock__endpoint { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 11px; color: var(--dsw-alias-label-secondary); overflow-wrap: anywhere; }',
      '.mcp-status-dock__error { color: var(--dsw-alias-state-error-primary); overflow-wrap: anywhere; }',
      '.mcp-status-dock__upstream { display: flex; flex-direction: column; gap: 3px; margin: 4px 0 0 3px; padding-left: 10px; border-left: 1px solid var(--dsw-alias-border-l1); }',
      '.mcp-status-dock__upstream-title { font-weight: 600; color: var(--dsw-alias-label-secondary); }',
      '.mcp-status-dock__upstream-item { display: flex; align-items: baseline; gap: 6px; min-width: 0; flex-wrap: wrap; }',
      '.mcp-status-dock__upstream-name { color: var(--dsw-alias-label-primary); overflow-wrap: anywhere; }',
      '.mcp-status-dock__footer { display: flex; justify-content: flex-end; }',
      '.mcp-status-dock__refresh { padding: 2px 8px; border: 1px solid var(--dsw-alias-border-l1); border-radius: 6px; background: transparent; color: var(--dsw-alias-label-secondary); font: inherit; cursor: pointer; }',
      '.mcp-status-dock__refresh:hover { background: var(--dsw-alias-bg-layer-2); color: var(--dsw-alias-label-primary); }',
      '.mcp-status-dock__refresh:focus-visible { outline: 2px solid var(--dsw-alias-brand-primary); outline-offset: 1px; }',
    ].join('\n')

    /** Locale keys for the connection states the Host reports. */
    const STATE_KEYS = {
      connected: 'state.connected',
      disconnected: 'state.disconnected',
      disabled: 'state.disabled',
      configured: 'state.configured',
      unknown: 'state.unknown',
    }

    /**
     * Coerce a thrown value into one short line of text.
     * @param {unknown} error - thrown value.
     * @returns {string} readable message.
     */
    function messageOf(error) {
      if (!error) return 'unknown error'
      if (typeof error === 'string') return error
      if (typeof error.message === 'string') return error.message
      return String(error)
    }

    /**
     * Read the Host snapshot, then keep it fresh while the row is mounted.
     * @param {boolean} open - whether the panel is expanded (asks for upstream data).
     * @returns {{ phase: string, data: any, error: string | null, refresh: () => void }} view state.
     */
    function useStatus(open) {
      const [state, setState] = React.useState({ phase: 'loading', data: null, error: null })
      const [tick, setTick] = React.useState(0)
      const refresh = React.useCallback(() => setTick((value) => value + 1), [])
      React.useEffect(() => {
        let cancelled = false
        let timer = null
        const url = open ? `${ENDPOINT}?upstream=1` : ENDPOINT
        const load = async () => {
          try {
            const response = await fetch(url, {
              credentials: 'same-origin',
              cache: 'no-store',
              headers: { accept: 'application/json' },
            })
            if (!response.ok) throw new Error(`HTTP ${response.status}`)
            const data = await response.json()
            if (!data || !Array.isArray(data.connections)) throw new Error(messageOf(data && data.error))
            if (cancelled) return
            setState({ phase: 'ready', data, error: null })
          } catch (error) {
            if (cancelled) return
            setState((previous) => ({
              phase: previous.data ? 'ready' : 'error',
              data: previous.data,
              error: messageOf(error),
            }))
          }
          if (!cancelled) timer = setTimeout(load, POLL_MS)
        }
        load()
        return () => {
          cancelled = true
          if (timer) clearTimeout(timer)
        }
      }, [open, tick])
      return { ...state, refresh }
    }

    /**
     * Pick the tone of one state.
     * @param {string} state - connection state.
     * @returns {'ok' | 'warn' | 'error' | 'idle'} tone.
     */
    function toneOfState(state) {
      if (state === 'connected') return 'ok'
      if (state === 'disconnected') return 'error'
      if (state === 'disabled') return 'idle'
      return 'warn'
    }

    /**
     * Tone of the collapsed chip, from the Host summary.
     * @param {any} summary - snapshot summary.
     * @returns {'ok' | 'warn' | 'error' | 'idle'} tone.
     */
    function toneOfSummary(summary) {
      const probed = Number(summary?.probed ?? 0)
      const connected = Number(summary?.connected ?? 0)
      if (probed === 0) return 'idle'
      if (connected === probed) return 'ok'
      return connected === 0 ? 'error' : 'warn'
    }

    /**
     * Build the localized count text for one of a singular/plural key pair.
     * @param {string} oneKey - key used for exactly one.
     * @param {string} manyKey - key used otherwise.
     * @param {number} count - number to describe.
     * @param {(key: string, params?: Record<string, unknown>) => string} t - translator.
     * @returns {string} text.
     */
    function countText(oneKey, manyKey, count, t) {
      return count === 1 ? t(oneKey) : t(manyKey, { count })
    }

    /**
     * One upstream server row.
     * @param {{ server: any, t: Function }} props - row data.
     * @returns {any} element.
     */
    function UpstreamRow({ server, t }) {
      const reachable = server.reachable !== false
      const detail = reachable
        ? countText('upstream.toolOne', 'upstream.tools', Number(server.toolCount ?? 0), t)
        : server.error || t('state.disconnected')
      return h('div', { className: 'mcp-status-dock__upstream-item' },
        h('span', { className: 'mcp-status-dock__dot mcp-status-dock__dot--small', 'data-tone': reachable ? 'ok' : 'error', 'aria-hidden': true }),
        h('span', { className: 'mcp-status-dock__upstream-name' }, server.server),
        h('span', { className: 'mcp-status-dock__muted' }, detail),
      )
    }

    /**
     * The upstream block of one federating connection.
     * @param {{ connection: any, t: Function }} props - connection view.
     * @returns {any} element.
     */
    function UpstreamList({ connection, t }) {
      const upstream = connection.upstream
      const servers = Array.isArray(upstream?.servers) ? upstream.servers : []
      if (!servers.length && !upstream?.error) return null
      const shown = servers.slice(0, MAX_UPSTREAM_ROWS)
      const hidden = servers.length - shown.length
      return h('div', { className: 'mcp-status-dock__upstream' },
        h('div', { className: 'mcp-status-dock__upstream-title' }, t('upstream.title', { server: connection.serverName })),
        upstream?.error && !servers.length
          ? h('div', { className: 'mcp-status-dock__muted' }, upstream.error)
          : null,
        shown.map((server) => h(UpstreamRow, { key: server.server, server, t })),
        hidden > 0 ? h('div', { className: 'mcp-status-dock__muted' }, t('upstream.more', { count: hidden })) : null,
      )
    }

    /**
     * One configured MCP server.
     * @param {{ connection: any, t: Function }} props - connection view.
     * @returns {any} element.
     */
    function ConnectionRow({ connection, t }) {
      const stateLabel = t(STATE_KEYS[connection.state] ?? STATE_KEYS.unknown)
      const meta = [connection.transport, stateLabel]
      if (typeof connection.toolCount === 'number' && connection.state !== 'disabled') {
        meta.push(countText('meta.toolOne', 'meta.tools', connection.toolCount, t))
      }
      if (typeof connection.latencyMs === 'number' && connection.state === 'connected') {
        meta.push(t('meta.latency', { ms: connection.latencyMs }))
      }
      if (connection.command) meta.splice(1, 0, connection.command)
      return h('div', { className: 'mcp-status-dock__row' },
        h('div', { className: 'mcp-status-dock__row-head' },
          h('span', { className: 'mcp-status-dock__dot', 'data-tone': toneOfState(connection.state), 'aria-hidden': true }),
          h('span', { className: 'mcp-status-dock__name' }, connection.serverName),
          h('span', { className: 'mcp-status-dock__muted' }, meta.join(' · ')),
          connection.pluginState === 'failed'
            ? h('span', { className: 'mcp-status-dock__error' }, t('state.pluginFailed'))
            : null,
        ),
        connection.endpoint ? h('div', { className: 'mcp-status-dock__endpoint' }, connection.endpoint) : null,
        connection.note ? h('div', { className: 'mcp-status-dock__muted' }, t('note.stdio')) : null,
        connection.error ? h('div', { className: 'mcp-status-dock__error' }, connection.error) : null,
        h(UpstreamList, { connection, t }),
      )
    }

    /**
     * The composer-dock row.
     * @param {{ t?: Function }} props - slot props (`t` comes from the locale seat).
     * @returns {any} element.
     */
    function McpStatusDock(props) {
      const t = typeof props.t === 'function' ? props.t : (key) => key
      const [open, setOpen] = React.useState(false)
      const { phase, data, error, refresh } = useStatus(open)
      const connections = Array.isArray(data?.connections) ? data.connections : []
      const summary = data?.summary

      if (!data && phase !== 'error') return null
      if (data && connections.length === 0) return null

      const probed = Number(summary?.probed ?? 0)
      const connected = Number(summary?.connected ?? 0)
      const total = Number(summary?.total ?? connections.length)
      const tone = phase === 'error' && !data ? 'error' : toneOfSummary(summary)
      const headline = !data
        ? t('chip.unavailable')
        : probed === 0
          ? t('chip.configured', { total })
          : t('chip.connected', { connected, total: probed })
      const upstreamCount = Number(summary?.upstream ?? 0)

      return h(React.Fragment, null,
        h('style', { key: 'style' }, CSS),
        h('div', { className: 'mcp-status-dock', key: 'dock' },
          h('button', {
            type: 'button',
            className: 'mcp-status-dock__bar',
            'aria-expanded': open ? 'true' : 'false',
            'aria-label': open ? t('chip.collapse') : t('chip.expand'),
            title: error && !data ? error : undefined,
            onClick: () => setOpen((value) => !value),
          },
            h('span', { className: 'mcp-status-dock__dot', 'data-tone': tone, 'aria-hidden': true }),
            h('span', { className: 'mcp-status-dock__label' }, t('chip.label')),
            h('span', null, headline),
            upstreamCount > 0 ? h('span', null, `· ${t('chip.upstream', { count: upstreamCount })}`) : null,
            h('svg', {
              className: 'mcp-status-dock__chevron',
              width: 10, height: 10, viewBox: '0 0 10 10',
              'aria-hidden': true,
            }, h('path', {
              d: 'M3 1.5 L6.5 5 L3 8.5',
              fill: 'none',
              stroke: 'currentColor',
              strokeWidth: 1.5,
              strokeLinecap: 'round',
              strokeLinejoin: 'round',
            })),
          ),
          open
            ? h('div', { className: 'mcp-status-dock__panel' },
                connections.map((connection) => h(ConnectionRow, { key: connection.id, connection, t })),
                error ? h('div', { className: 'mcp-status-dock__error' }, error) : null,
                h('div', { className: 'mcp-status-dock__footer' },
                  h('button', { type: 'button', className: 'mcp-status-dock__refresh', onClick: refresh }, t('action.refresh')),
                ),
              )
            : null,
        ),
      )
    }

    return {
      inject: ['slots', 'locale'],
      apply(ctx) {
        ctx.effect(() => ctx.locale.register(NS, { en: EN, zh: ZH }), 'mcp-status: dictionaries')
        ctx.slots.inject('conversation.composer.dock', () => ctx.slots.register({
          name: 'conversation.composer.dock',
          id: 'mcp-status',
          order: 2,
          locale: NS,
        }, McpStatusDock))
      },
    }
  },
})
