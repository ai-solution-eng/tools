/**
 * App shortcuts — browser half.
 *
 * Adds three controls at the sidebar foot that open the environment's sibling
 * pages in a new tab:
 *   Terminal      /terminal      (ttyd + tmux, tabbed/persistent)
 *   Data Manager  /data-manager  (file browser, uploads, archives)
 *   Login         /              (main sign-in / account page)
 *
 * All are same-origin and reachable root-relative through the router's
 * session passthrough, so a plain anchor is the whole integration — no Host
 * route, no fetch, no cookie handling. `/` is the one path the router always
 * keeps for itself: with a live session it renders the signed-in account
 * page (identity, Log out, Open your environment) above the login form, and
 * without one it is the plain login page.
 *
 * The seat is `sidebar.footer.action`: a LIST slot declared by
 * @deepseek-ai/dsh-client-ui-sidebar. Registering here is additive, so the
 * Harness' own right-sidebar terminal (ui-sidebar-terminal) and workspace file
 * tree (ui-sidebar-files) keep working exactly as before.
 *
 * Styling uses only --dsw-alias-* theme tokens and mirrors the sidebar's own
 * row metrics, so the controls follow light/dark automatically.
 */
window.__ModuleLoader__.load({
  id: '@local/dsh-app-shortcuts',
  factory(require) {
    const React = require('react')
    const h = React.createElement

    /** Locale namespace owned by this plugin. */
    const NS = 'app-shortcuts'

    const EN = {
      'terminal.label': 'Terminal',
      'terminal.hint': 'Open the Terminal in a new tab',
      'dataManager.label': 'Data Manager',
      'dataManager.hint': 'Open the Data Manager in a new tab',
      'login.label': 'Login',
      'login.hint': 'Open the login page in a new tab',
    }

    const ZH = {
      'terminal.label': '终端',
      'terminal.hint': '在新标签页中打开终端',
      'dataManager.label': '数据管理器',
      'dataManager.hint': '在新标签页中打开数据管理器',
      'login.label': '登录',
      'login.hint': '在新标签页中打开登录页面',
    }

    /** Terminal glyph: a window with a prompt and a cursor. */
    function TerminalGlyph() {
      return h('svg', {
        viewBox: '0 0 16 16', width: 16, height: 16, 'aria-hidden': true, focusable: false,
      },
        h('rect', {
          x: 1.6, y: 2.6, width: 12.8, height: 10.8, rx: 2,
          fill: 'none', stroke: 'currentColor', strokeWidth: 1.3,
        }),
        h('path', {
          d: 'M4.6 6.1 L6.7 8.1 L4.6 10.1',
          fill: 'none', stroke: 'currentColor', strokeWidth: 1.3,
          strokeLinecap: 'round', strokeLinejoin: 'round',
        }),
        h('path', {
          d: 'M8.6 10.3 H11.3',
          fill: 'none', stroke: 'currentColor', strokeWidth: 1.3, strokeLinecap: 'round',
        }),
      )
    }

    /** Data Manager glyph: a folder. */
    function FolderGlyph() {
      return h('svg', {
        viewBox: '0 0 16 16', width: 16, height: 16, 'aria-hidden': true, focusable: false,
      },
        h('path', {
          d: 'M1.8 4.3 A1.3 1.3 0 0 1 3.1 3 H6.2 L7.7 4.7 H12.9 A1.3 1.3 0 0 1 14.2 6 V11.7 A1.3 1.3 0 0 1 12.9 13 H3.1 A1.3 1.3 0 0 1 1.8 11.7 Z',
          fill: 'none', stroke: 'currentColor', strokeWidth: 1.3, strokeLinejoin: 'round',
        }),
      )
    }

    /** Login glyph: an arrow entering an open door (sign-in). */
    function LoginGlyph() {
      return h('svg', {
        viewBox: '0 0 16 16', width: 16, height: 16, 'aria-hidden': true, focusable: false,
      },
        h('path', {
          d: 'M9.7 2.7 H12 A1.3 1.3 0 0 1 13.3 4 V12 A1.3 1.3 0 0 1 12 13.3 H9.7',
          fill: 'none', stroke: 'currentColor', strokeWidth: 1.3,
          strokeLinecap: 'round', strokeLinejoin: 'round',
        }),
        h('path', {
          d: 'M2.7 8 H8.3',
          fill: 'none', stroke: 'currentColor', strokeWidth: 1.3, strokeLinecap: 'round',
        }),
        h('path', {
          d: 'M6 5.7 L8.3 8 L6 10.3',
          fill: 'none', stroke: 'currentColor', strokeWidth: 1.3,
          strokeLinecap: 'round', strokeLinejoin: 'round',
        }),
      )
    }

    /** The three destinations, in display order. */
    const APPS = [
      { key: 'terminal', path: '/terminal', labelKey: 'terminal.label', hintKey: 'terminal.hint', Glyph: TerminalGlyph },
      { key: 'dataManager', path: '/data-manager', labelKey: 'dataManager.label', hintKey: 'dataManager.hint', Glyph: FolderGlyph },
      { key: 'login', path: '/', labelKey: 'login.label', hintKey: 'login.hint', Glyph: LoginGlyph },
    ]

    const CSS = [
      '.dsh-app-shortcuts{display:flex;width:100%;min-width:0}',
      '.dsh-app-shortcuts--wide{flex-direction:column;gap:2px}',
      '.dsh-app-shortcuts--rail{flex-direction:column;align-items:center;gap:2px;width:auto}',
      '.dsh-app-shortcuts__link{display:flex;align-items:center;gap:8px;box-sizing:border-box;',
      'border-radius:var(--dsw-radius-md,8px);color:var(--dsw-alias-label-primary);',
      'text-decoration:none;font:inherit;font-size:14px;line-height:22px;cursor:pointer;',
      'transition:background .15s ease}',
      '.dsh-app-shortcuts__link:hover{background:var(--dsw-alias-bg-layer-2)}',
      '.dsh-app-shortcuts__link:focus-visible{outline:2px solid var(--dsw-alias-brand-primary);outline-offset:-2px}',
      '.dsh-app-shortcuts--wide .dsh-app-shortcuts__link{min-height:34px;padding:6px 8px;margin:0 2px}',
      '.dsh-app-shortcuts--rail .dsh-app-shortcuts__link{width:36px;height:36px;justify-content:center;padding:0}',
      '.dsh-app-shortcuts__glyph{display:inline-flex;align-items:center;justify-content:center;flex:none}',
      '.dsh-app-shortcuts__label{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}',
    ].join('')

    /**
     * One row per app. The owner passes `wide`: true when the sidebar renders
     * its full column (label + icon), false for the 56px rail (icon only).
     *
     * @param {{ wide?: boolean, t?: Function }} props - slot props.
     */
    function AppShortcuts(props) {
      const t = typeof props.t === 'function' ? props.t : (key) => key
      const wide = props.wide !== false
      return h(React.Fragment, null,
        h('style', { key: 'css' }, CSS),
        h('div', { key: 'rows', className: 'dsh-app-shortcuts ' + (wide ? 'dsh-app-shortcuts--wide' : 'dsh-app-shortcuts--rail') },
          APPS.map((app) => {
            const label = t(app.labelKey)
            return h('a', {
              key: app.key,
              className: 'dsh-app-shortcuts__link',
              href: app.path,
              target: '_blank',
              rel: 'noopener',
              title: t(app.hintKey),
              'aria-label': t(app.hintKey),
            },
              h('span', { className: 'dsh-app-shortcuts__glyph' }, h(app.Glyph)),
              wide ? h('span', { className: 'dsh-app-shortcuts__label' }, label) : null,
            )
          }),
        ),
      )
    }

    return {
      inject: ['slots', 'locale'],
      apply(ctx) {
        ctx.effect(() => ctx.locale.register(NS, { en: EN, zh: ZH }), 'app-shortcuts: dictionaries')
        ctx.slots.inject('sidebar.footer.action', () => ctx.slots.register({
          name: 'sidebar.footer.action',
          id: 'app-shortcuts',
          order: 20,
          locale: NS,
        }, AppShortcuts))
      },
    }
  },
})
