/**
 * Config document — browser half.
 *
 * Replaces the shipped Settings header action `open-document` ("Open
 * configuration file"), which asks the Host to open the profile patch in a native
 * desktop editor. In a container deployment that native open has nowhere to go, so
 * the button silently does nothing. This action opens the same file *inside the Web
 * UI*: the Host half (`./index.js`) serves `{ path, content }` on an authenticated
 * route, and this module renders it in a modal with the absolute path, a line and
 * size hint, Copy, Download, Reload, and a fallback button that still asks the Host
 * to open its desktop editor — reporting the Host's refusal instead of swallowing it.
 *
 * The cell is registered with the shipped id `open-document` at a lower priority
 * than the shipped entry, which is how a list-slot cell is shadowed: the shipped
 * button disappears and this one takes its place, with the same label.
 *
 * Styling uses only `--dsw-alias-*` theme tokens (plus the shared radius, elevation
 * and code-font tokens, each with a literal fallback). No Harness Client package is
 * imported, nothing is written to `document.body`, and no Host-served HTML page is
 * framed.
 */
window.__ModuleLoader__.load({
  id: '@local/dsh-config-document',
  factory(require) {
    const React = require('react')
    const h = React.createElement

    /** Locale namespace owned by this plugin. */
    const NS = 'config-document'
    /** Host route registered by `./index.js`. */
    const ENDPOINT = 'api/config-document'
    /** Fallback name when the Host reports a path without a basename. */
    const FALLBACK_NAME = 'cordis.patch.yml'
    /** Row and field listing for the guided editor. */
    const ENTRIES_ENDPOINT = ENDPOINT + '/entries'
    /** Write route for one scalar field. */
    const EDIT_ENDPOINT = ENDPOINT + '/edit'

    const EN = {
      'action.open': 'Open configuration file',
      'action.openHint': 'Show the configuration file inside the app',
      'dialog.title': 'Configuration file',
      'dialog.close': 'Close',
      'meta.lines': '{count} lines',
      'meta.lineOne': '1 line',
      'meta.truncated': 'showing the first {size}',
      'meta.modified': 'modified {time}',
      'action.copy': 'Copy',
      'action.copied': 'Copied to the clipboard.',
      'action.copyFailed': 'Could not copy automatically; select the text and copy it manually.',
      'action.download': 'Download',
      'action.downloaded': 'The file was handed to the browser download.',
      'action.downloadFailed': 'Could not start the download.',
      'action.reload': 'Reload',
      'action.system': 'Open in Host editor',
      'system.hint': 'Opens the file with the Host machine desktop editor. A headless Host cannot do that; use Download instead.',
      'system.busy': 'Asking the Host to open the file…',
      'system.opened': 'The Host accepted the open request. If no editor window appeared, this Host has no desktop editor.',
      'system.failed': 'The Host could not open the file: {message}',
      'system.unavailable': 'This page has no Host settings connection, so the file cannot be opened on the Host.',
      'state.loading': 'Reading the configuration file…',
      'state.empty': 'The configuration file is empty.',
      'error.title': 'Could not read the configuration file',
      'error.invalid': 'The Host answered with something this page cannot read.',
      'error.route': 'The Host route is not available (HTTP {status}). The Host half of this plugin may not be loaded yet; a dsh restart is required after installing it.',
      'error.network': 'Could not reach the Host: {message}',
      'error.unavailable': 'The Host exposes no configuration document.',
      'hint.readonly': 'Read-only view. Use Edit values for top-level settings, or edit the file on the Host.',
      'action.edit': 'Edit values',
      'action.save': 'Save',
      'action.saving': 'Saving…',
      'action.cancel': 'Cancel',
      'editor.title': 'Edit a value',
      'editor.hint': 'Only top-level text, number and boolean values are offered. Everything else stays in the file.',
      'editor.row': 'Row',
      'editor.noRows': 'No row exposes an editable value.',
      'editor.saved': 'Saved. The configuration editor rewrote the file.',
      'editor.failed': 'Not saved: {message}',
      'editor.unchanged': 'Nothing changed.',
      'guide.title': 'Editing by hand',
      'guide.body': 'To change anything else, open the data manager, navigate to the profile volume and edit {file}, then restart the Host.',
      'guide.copyPath': 'Copy path',
      'guide.copied': 'Path copied.',
    }

    const ZH = {
      'action.open': '打开配置文件',
      'action.openHint': '在应用内查看配置文件',
      'dialog.title': '配置文件',
      'dialog.close': '关闭',
      'meta.lines': '{count} 行',
      'meta.lineOne': '1 行',
      'meta.truncated': '仅显示前 {size}',
      'meta.modified': '修改于 {time}',
      'action.copy': '复制',
      'action.copied': '已复制到剪贴板。',
      'action.copyFailed': '无法自动复制，请选中文本手动复制。',
      'action.download': '下载',
      'action.downloaded': '已交给浏览器下载。',
      'action.downloadFailed': '无法开始下载。',
      'action.reload': '重新加载',
      'action.system': '在主机编辑器中打开',
      'system.hint': '使用主机桌面上的编辑器打开该文件。无桌面的主机会失败，请改用下载。',
      'system.busy': '正在请求主机打开文件…',
      'system.opened': '主机已接受打开请求。若没有出现编辑器窗口，说明该主机没有桌面编辑器。',
      'system.failed': '主机无法打开该文件：{message}',
      'system.unavailable': '此页面没有主机设置连接，无法在主机上打开该文件。',
      'state.loading': '正在读取配置文件…',
      'state.empty': '配置文件为空。',
      'error.title': '无法读取配置文件',
      'error.invalid': '主机返回了此页面无法解析的内容。',
      'error.route': '主机路由不可用（HTTP {status}）。该插件的主机部分可能尚未加载；安装后需要重启 dsh。',
      'error.network': '无法连接主机：{message}',
      'error.unavailable': '主机未提供配置文件。',
      'hint.readonly': '只读文本视图。顶层设置请使用“编辑值”，其他内容请在主机上编辑文件。',
      'action.edit': '编辑值',
      'action.save': '保存',
      'action.saving': '保存中…',
      'action.cancel': '取消',
      'editor.title': '编辑值',
      'editor.hint': '仅提供顶层的文本、数字和布尔值。其他内容仍保留在文件中。',
      'editor.row': '条目',
      'editor.noRows': '没有条目提供可编辑的值。',
      'editor.saved': '已保存。配置编辑器已重写文件。',
      'editor.failed': '未保存：{message}',
      'editor.unchanged': '没有变化。',
      'guide.title': '手动编辑',
      'guide.body': '如需修改其他内容，请打开数据管理器，进入配置卷并编辑 {file}，然后重启主机。',
      'guide.copyPath': '复制路径',
      'guide.copied': '路径已复制。',
    }

    const CSS = [
      '.cfgdoc-root { display: inline-flex; align-items: center; min-width: 0; }',
      '.cfgdoc-trigger { display: inline-flex; align-items: center; gap: 6px; height: 28px; padding: 0 10px; border: 1px solid var(--dsw-alias-border-l2); border-radius: var(--dsw-radius-sm, 6px); background: transparent; color: var(--dsw-alias-label-primary); font: inherit; font-size: 13px; line-height: 20px; white-space: nowrap; cursor: pointer; }',
      '.cfgdoc-trigger:hover { background: var(--dsw-alias-interactive-bg-hover); }',
      '.cfgdoc-trigger:focus-visible { outline: 2px solid var(--dsw-alias-brand-primary); outline-offset: 1px; }',
      '.cfgdoc-trigger__glyph { flex: none; display: inline-flex; align-items: center; }',
      '.cfgdoc-overlay { position: fixed; inset: 0; z-index: 1100; display: flex; align-items: center; justify-content: center; padding: 24px; background: var(--dsw-alias-bg-mask-1); backdrop-filter: var(--dsw-mask-blur, blur(2px)); }',
      '.cfgdoc-dialog { display: flex; flex-direction: column; width: min(920px, 100%); max-height: min(760px, calc(100vh - 48px)); overflow: hidden; border: 1px solid var(--dsw-alias-border-l1); border-radius: var(--dsw-radius-panel, 12px); background: var(--dsw-alias-bg-layer-2); box-shadow: var(--dsw-elevation-prominent, 0 16px 48px rgba(0, 0, 0, 0.28)); }',
      '.cfgdoc-dialog:focus { outline: none; }',
      '.cfgdoc-head { display: flex; align-items: flex-start; gap: 12px; padding: 16px 14px 10px 20px; }',
      '.cfgdoc-head__text { display: flex; flex: 1; flex-direction: column; gap: 4px; min-width: 0; }',
      '.cfgdoc-title { color: var(--dsw-alias-label-primary); font-size: 15px; font-weight: 500; line-height: 22px; }',
      '.cfgdoc-path { color: var(--dsw-alias-label-secondary); font-family: var(--ds-font-family-code, ui-monospace, SFMono-Regular, Menlo, Consolas, monospace); font-size: 11.5px; line-height: 16px; overflow-wrap: anywhere; }',
      '.cfgdoc-meta { display: flex; flex-wrap: wrap; gap: 2px 10px; color: var(--dsw-alias-label-tertiary); font-size: 11.5px; line-height: 16px; }',
      '.cfgdoc-iconbtn { display: inline-flex; flex: none; align-items: center; justify-content: center; width: 28px; height: 28px; padding: 0; border: none; border-radius: var(--dsw-radius-sm, 6px); background: transparent; color: var(--dsw-alias-label-primary); cursor: pointer; }',
      '.cfgdoc-iconbtn:hover { background: var(--dsw-alias-interactive-bg-hover); }',
      '.cfgdoc-iconbtn:focus-visible { outline: 2px solid var(--dsw-alias-brand-primary); outline-offset: 1px; }',
      '.cfgdoc-toolbar { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; padding: 0 20px 12px; }',
      '.cfgdoc-btn { display: inline-flex; align-items: center; height: 28px; padding: 0 10px; border: 1px solid var(--dsw-alias-border-l2); border-radius: var(--dsw-radius-sm, 6px); background: transparent; color: var(--dsw-alias-label-primary); font: inherit; font-size: 12px; line-height: 20px; white-space: nowrap; cursor: pointer; }',
      '.cfgdoc-btn:hover { background: var(--dsw-alias-interactive-bg-hover); }',
      '.cfgdoc-btn:focus-visible { outline: 2px solid var(--dsw-alias-brand-primary); outline-offset: 1px; }',
      '.cfgdoc-btn:disabled { opacity: 0.5; cursor: default; }',
      '.cfgdoc-status { padding: 0 20px 10px; font-size: 12px; line-height: 18px; overflow-wrap: anywhere; }',
      '.cfgdoc-status[data-tone="ok"] { color: var(--dsw-alias-label-secondary); }',
      '.cfgdoc-status[data-tone="busy"] { color: var(--dsw-alias-label-secondary); }',
      '.cfgdoc-status[data-tone="error"] { color: var(--dsw-alias-label-error); }',
      '.cfgdoc-body { display: flex; flex: 1; flex-direction: column; min-height: 0; margin: 0 20px; overflow: auto; border: 1px solid var(--dsw-alias-border-l1); border-radius: var(--dsw-radius-md, 8px); background: var(--dsw-alias-bg-layer-1); scrollbar-width: thin; }',
      '.cfgdoc-code { margin: 0; padding: 12px 14px; color: var(--dsw-alias-label-primary); font-family: var(--ds-font-family-code, ui-monospace, SFMono-Regular, Menlo, Consolas, monospace); font-size: 12px; line-height: 1.55; white-space: pre; tab-size: 2; }',
      '.cfgdoc-notice { display: flex; flex-direction: column; gap: 6px; padding: 18px 16px; color: var(--dsw-alias-label-secondary); font-size: 13px; line-height: 20px; overflow-wrap: anywhere; }',
      '.cfgdoc-notice__title { color: var(--dsw-alias-label-primary); font-weight: 500; }',
      '.cfgdoc-notice__detail { color: var(--dsw-alias-label-secondary); font-family: var(--ds-font-family-code, ui-monospace, SFMono-Regular, Menlo, Consolas, monospace); font-size: 11.5px; line-height: 17px; }',
      '.cfgdoc-editor { display: flex; flex-direction: column; gap: 10px; padding: 14px; }',
      '.cfgdoc-editor__title { font-size: 13px; font-weight: 600; color: var(--dsw-alias-label-primary); }',
      '.cfgdoc-editor__hint { font-size: 12px; line-height: 18px; color: var(--dsw-alias-label-tertiary); }',
      '.cfgdoc-editor__form { display: flex; flex-direction: column; gap: 8px; max-height: 46vh; overflow: auto; }',
      '.cfgdoc-editor__field { display: flex; flex-direction: column; gap: 3px; font-size: 12px; color: var(--dsw-alias-label-secondary); }',
      '.cfgdoc-editor__input { box-sizing: border-box; width: 100%; padding: 5px 8px; border: 1px solid var(--dsw-alias-border-l2); border-radius: 6px; background: var(--dsw-alias-bg-base); color: var(--dsw-alias-label-primary); font: inherit; font-size: 13px; }',
      '.cfgdoc-editor__actions { display: flex; gap: 8px; justify-content: flex-end; }',
      '.cfgdoc-guide { display: flex; flex-direction: column; gap: 6px; padding: 10px 20px; border-top: 1px solid var(--dsw-alias-border-l2); background: var(--dsw-alias-bg-layer-2); }',
      '.cfgdoc-guide__title { font-size: 12px; font-weight: 600; color: var(--dsw-alias-label-secondary); }',
      '.cfgdoc-guide__body { font-size: 12px; line-height: 18px; color: var(--dsw-alias-label-tertiary); }',
      '.cfgdoc-guide__actions { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }',
      '.cfgdoc-guide__path { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 11px; padding: 3px 6px; border-radius: 4px; background: var(--dsw-alias-bg-base); color: var(--dsw-alias-label-secondary); overflow-wrap: anywhere; }',
      '.cfgdoc-foot { display: flex; flex-wrap: wrap; align-items: baseline; justify-content: space-between; gap: 4px 12px; padding: 12px 20px 16px; color: var(--dsw-alias-label-tertiary); font-size: 11.5px; line-height: 16px; }',
      '.cfgdoc-foot__hint { flex: 1; min-width: 0; overflow-wrap: anywhere; }',
      '.cfgdoc-sr { position: absolute; left: -9999px; top: 0; width: 1px; height: 1px; opacity: 0; pointer-events: none; }',
    ].join('\n')

    /**
     * Coerce a thrown value into one short line of text.
     * @param {unknown} error - thrown value.
     * @returns {string} readable message.
     */
    function messageOf(error) {
      if (error === undefined || error === null) return 'unknown error'
      if (typeof error === 'string') return error
      if (typeof error.message === 'string') return error.message
      return String(error)
    }

    /**
     * Render a byte count for humans.
     * @param {unknown} bytes - byte count.
     * @returns {string} short size, or an empty string when unknown.
     */
    function formatBytes(bytes) {
      const value = Number(bytes)
      if (!Number.isFinite(value) || value < 0) return ''
      if (value < 1024) return `${String(Math.round(value))} B`
      if (value < 1024 * 1024) return `${(value / 1024).toFixed(value < 10240 ? 1 : 0)} KB`
      return `${(value / (1024 * 1024)).toFixed(1)} MB`
    }

    /**
     * Render an ISO timestamp with the page's locale.
     * @param {unknown} iso - ISO timestamp.
     * @returns {string} localized timestamp, or an empty string when unreadable.
     */
    function formatTime(iso) {
      if (typeof iso !== 'string' || iso.length === 0) return ''
      const date = new Date(iso)
      if (Number.isNaN(date.getTime())) return ''
      try {
        return date.toLocaleString()
      } catch {
        return date.toISOString()
      }
    }

    /**
     * Build a plural-aware line count.
     * @param {number} count - line count.
     * @param {(key: string, params?: Record<string, unknown>) => string} t - translator.
     * @returns {string} text.
     */
    function lineText(count, t) {
      return count === 1 ? t('meta.lineOne') : t('meta.lines', { count })
    }

    /**
     * Ask the Host to open the document in its own desktop editor.
     * The Host service is looked up lazily so this plugin stays active when the
     * settings controller is absent, and every refusal is reported to the caller.
     * @param {any} ctx - the plugin context.
     * @returns {Promise<{ ok: boolean, code?: string, message?: string }>} outcome.
     */
    async function openSystemDocument(ctx) {
      let remote
      try {
        remote = typeof ctx.get === 'function' ? ctx.get('remote') : undefined
      } catch {
        remote = undefined
      }
      const settings = remote && remote.settings
      const open = settings && typeof settings.openSettingsDocument === 'function'
        ? settings.openSettingsDocument
        : undefined
      if (typeof open !== 'function') return { ok: false, code: 'unavailable' }
      try {
        const result = await open.call(settings)
        if (result && result.ok === false) {
          const error = result.error
          return { ok: false, code: 'failed', message: messageOf(error && error.message) }
        }
        return { ok: true }
      } catch (error) {
        return { ok: false, code: 'failed', message: messageOf(error) }
      }
    }

    /**
     * Write text to the clipboard, falling back to a selection copy when the
     * async clipboard API is unavailable (insecure context, older browser).
     * @param {string} text - text to copy.
     * @param {any} fallback - hidden textarea owned by the dialog.
     * @returns {Promise<boolean>} whether a copy path reported success.
     */
    async function writeClipboard(text, fallback) {
      const nav = typeof navigator === 'undefined' ? undefined : navigator
      if (nav && nav.clipboard && typeof nav.clipboard.writeText === 'function') {
        try {
          await nav.clipboard.writeText(text)
          return true
        } catch {
          // Fall through to the selection path.
        }
      }
      const doc = typeof document === 'undefined' ? undefined : document
      if (!fallback || typeof fallback.select !== 'function' || !doc || typeof doc.execCommand !== 'function') return false
      try {
        fallback.value = text
        if (typeof fallback.focus === 'function') fallback.focus()
        fallback.select()
        return doc.execCommand('copy') === true
      } catch {
        return false
      }
    }

    /**
     * Keep Tab inside the dialog, wrapping at both ends.
     * @param {any} event - React keyboard event.
     * @param {any} dialog - dialog element, when the DOM is available.
     */
    function trapTab(event, dialog) {
      if (!dialog || typeof dialog.querySelector !== 'function' || typeof event.preventDefault !== 'function') return
      const selector = 'button:not(:disabled), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
      let focusable = []
      try {
        // The hidden copy textarea matches the selector but is not tabbable.
        focusable = Array.from(dialog.querySelectorAll(selector)).filter((element) => element.tabIndex >= 0)
      } catch {
        return
      }
      if (focusable.length === 0) {
        event.preventDefault()
        return
      }
      const doc = dialog.ownerDocument
      const active = doc && doc.activeElement ? doc.activeElement : null
      const first = focusable[0]
      const last = focusable[focusable.length - 1]
      if (!event.shiftKey && active === last) {
        event.preventDefault()
        if (typeof first.focus === 'function') first.focus()
      } else if (event.shiftKey && (active === first || active === dialog)) {
        event.preventDefault()
        if (typeof last.focus === 'function') last.focus()
      }
    }

    /** Small document glyph used by the trigger button. */
    function Glyph() {
      return h('svg', {
        width: 14, height: 14, viewBox: '0 0 16 16', 'aria-hidden': true, focusable: 'false',
      },
        h('path', {
          d: 'M3.5 2.25h5.19L12.5 6.06v7.19a.75.75 0 0 1-.75.75H3.5a.75.75 0 0 1-.75-.75V3a.75.75 0 0 1 .75-.75Z',
          fill: 'none', stroke: 'currentColor', strokeWidth: 1.2, strokeLinejoin: 'round',
        }),
        h('path', {
          d: 'M8.5 2.5v3.4h3.6M5.2 8.4h5.1M5.2 10.7h5.1M5.2 13h3.2',
          fill: 'none', stroke: 'currentColor', strokeWidth: 1.2, strokeLinecap: 'round', strokeLinejoin: 'round',
        }),
      )
    }

    /**
     * The document modal: reads the Host route, renders the text, and owns the
     * Copy / Download / Reload / Host-editor actions.
     * @param {{ t: Function, onClose: Function, openSystemDocument: Function }} props - dialog inputs.
     * @returns {any} element.
     */
    function DocumentDialog(props) {
      const { t, onClose } = props
      const openOnHost = typeof props.openSystemDocument === 'function' ? props.openSystemDocument : openSystemDocument
      const [state, setState] = React.useState({ phase: 'loading', data: null, error: null })
      const [tick, setTick] = React.useState(0)
      const [status, setStatus] = React.useState(null)
      const [editOpen, setEditOpen] = React.useState(false)
      const [editRows, setEditRows] = React.useState([])
      const [editRowId, setEditRowId] = React.useState('')
      const [editValues, setEditValues] = React.useState({})
      const [editBusy, setEditBusy] = React.useState(false)
      const dialogRef = React.useRef(null)
      const copyRef = React.useRef(null)
      const linkRef = React.useRef(null)
      const restoreRef = React.useRef(null)

      React.useEffect(() => {
        let cancelled = false
        const load = async () => {
          try {
            const response = await fetch(ENDPOINT, {
              credentials: 'same-origin',
              cache: 'no-store',
              headers: { accept: 'application/json' },
            })
            let body = null
            try {
              body = await response.json()
            } catch {
              body = null
            }
            if (cancelled) return
            if (!body || typeof body !== 'object') {
              setState({
                phase: 'error',
                data: null,
                error: response.ok
                  ? t('error.invalid')
                  : t('error.route', { status: response.status }),
              })
              return
            }
            if (body.ok === false) {
              const detail = typeof body.message === 'string' && body.message.length > 0 ? body.message : ''
              setState({
                phase: 'error',
                data: typeof body.path === 'string' ? { path: body.path } : null,
                error: detail || t('error.unavailable'),
              })
              return
            }
            if (typeof body.content !== 'string') {
              setState({ phase: 'error', data: null, error: t('error.invalid') })
              return
            }
            setState({ phase: 'ready', data: body, error: null })
          } catch (error) {
            if (cancelled) return
            setState({ phase: 'error', data: null, error: t('error.network', { message: messageOf(error) }) })
          }
        }
        load()
        return () => {
          cancelled = true
        }
      }, [tick])

      React.useEffect(() => {
        const doc = typeof document === 'undefined' ? undefined : document
        const previous = doc && doc.activeElement ? doc.activeElement : null
        restoreRef.current = previous && typeof previous.focus === 'function' ? previous : null
        const dialog = dialogRef.current
        if (dialog && typeof dialog.focus === 'function') dialog.focus()
        return () => {
          const target = restoreRef.current
          if (target && target.isConnected !== false && typeof target.focus === 'function') target.focus()
        }
      }, [])

      const onKeyDown = (event) => {
        if (!event) return
        if (event.key === 'Escape') {
          if (typeof event.stopPropagation === 'function') event.stopPropagation()
          if (typeof event.preventDefault === 'function') event.preventDefault()
          onClose()
          return
        }
        if (event.key === 'Tab') trapTab(event, dialogRef.current)
      }

      const onMaskClick = (event) => {
        if (!event || event.target !== event.currentTarget) return
        onClose()
      }

      const onReload = () => {
        setStatus(null)
        // Always drop back to the loading state: a previous error may have left a
        // path-only payload that has no text to keep showing.
        setState({ phase: 'loading', data: null, error: null })
        setTick((value) => value + 1)
      }

      const onCopy = async () => {
        const text = state.data && typeof state.data.content === 'string' ? state.data.content : ''
        const copied = await writeClipboard(text, copyRef.current)
        setStatus({ tone: copied ? 'ok' : 'error', text: copied ? t('action.copied') : t('action.copyFailed') })
      }

      const onDownload = () => {
        const data = state.data
        if (!data || typeof data.content !== 'string') return
        try {
          const blob = new Blob([data.content], { type: 'text/yaml;charset=utf-8' })
          const url = URL.createObjectURL(blob)
          const link = linkRef.current
          if (link) {
            link.href = url
            link.download = typeof data.name === 'string' && data.name.length > 0 ? data.name : FALLBACK_NAME
            link.click()
          }
          setTimeout(() => {
            try {
              URL.revokeObjectURL(url)
            } catch {
              // The object URL is already gone; nothing to release.
            }
          }, 1000)
          setStatus({ tone: 'ok', text: t('action.downloaded') })
        } catch {
          setStatus({ tone: 'error', text: t('action.downloadFailed') })
        }
      }

      const onSystemOpen = async () => {
        setStatus({ tone: 'busy', text: t('system.busy') })
        const result = await openOnHost()
        if (result && result.ok) {
          setStatus({ tone: 'ok', text: t('system.opened') })
          return
        }
        if (result && result.code === 'unavailable') {
          setStatus({ tone: 'error', text: t('system.unavailable') })
          return
        }
        setStatus({ tone: 'error', text: t('system.failed', { message: messageOf(result && result.message) }) })
      }

      const data = state.data
      const path = data && typeof data.path === 'string' ? data.path : ''

      // Guided editor: top-level scalar values only, written through the
      // configuration editor so the row is validated by its own schema and the
      // document keeps its comments and !!js expressions.
      const valuesOf = (row) => {
        const values = {}
        for (const field of row && Array.isArray(row.fields) ? row.fields : []) {
          values[field.path] = field.value === undefined || field.value === null ? '' : String(field.value)
        }
        return values
      }

      const selectRow = (id) => {
        setEditRowId(id)
        setEditValues(valuesOf(editRows.find((entry) => entry.id === id)))
      }

      const openEditor = async () => {
        setEditOpen(true)
        setStatus(null)
        try {
          const response = await fetch(ENTRIES_ENDPOINT, {
            credentials: 'same-origin',
            cache: 'no-store',
            headers: { accept: 'application/json' },
          })
          const body = await response.json().catch(() => null)
          const rows = body && Array.isArray(body.entries) ? body.entries : []
          setEditRows(rows)
          const first = rows.length > 0 ? rows[0] : null
          setEditRowId(first ? first.id : '')
          setEditValues(first ? valuesOf(first) : {})
        } catch (error) {
          setEditRows([])
          setStatus({ tone: 'error', text: t('error.network', { message: messageOf(error) }) })
        }
      }

      const saveEditor = async () => {
        const row = editRows.find((entry) => entry.id === editRowId)
        if (!row || !Array.isArray(row.fields)) return
        const changes = row.fields.filter((field) => String(field.value) !== String(editValues[field.path]))
        if (changes.length === 0) {
          setStatus({ tone: 'ok', text: t('editor.unchanged') })
          return
        }
        setEditBusy(true)
        for (const field of changes) {
          try {
            const response = await fetch(EDIT_ENDPOINT, {
              method: 'POST',
              credentials: 'same-origin',
              cache: 'no-store',
              headers: { 'content-type': 'application/json', accept: 'application/json' },
              body: JSON.stringify({ id: row.id, path: field.path, value: editValues[field.path] }),
            })
            const body = await response.json().catch(() => null)
            if (!body || body.ok !== true) {
              const reason = body && typeof body.message === 'string' && body.message ? body.message : 'HTTP ' + response.status
              setStatus({ tone: 'error', text: t('editor.failed', { message: reason }) })
              setEditBusy(false)
              return
            }
          } catch (error) {
            setStatus({ tone: 'error', text: t('editor.failed', { message: messageOf(error) }) })
            setEditBusy(false)
            return
          }
        }
        setEditBusy(false)
        setEditOpen(false)
        setStatus({ tone: 'ok', text: t('editor.saved') })
        onReload()
      }

      const copyPath = async () => {
        const copied = await writeClipboard(path || FALLBACK_NAME, copyRef.current)
        setStatus({ tone: copied ? 'ok' : 'error', text: copied ? t('guide.copied') : t('action.copyFailed') })
      }
      const ready = state.phase === 'ready' && data !== null
      const meta = []
      if (ready) {
        meta.push(lineText(Number(data.lines) || 0, t))
        const size = formatBytes(data.bytes)
        if (size) meta.push(size)
        if (data.truncated) {
          const cap = formatBytes(data.maxBytes)
          if (cap) meta.push(t('meta.truncated', { size: cap }))
        }
        const modified = formatTime(data.mtime)
        if (modified) meta.push(t('meta.modified', { time: modified }))
      }

      const activeRow = editRows.find((entry) => entry.id === editRowId)
      const editorPane = h('div', { className: 'cfgdoc-editor' },
        h('div', { className: 'cfgdoc-editor__title' }, t('editor.title')),
        h('div', { className: 'cfgdoc-editor__hint' }, t('editor.hint')),
        editRows.length === 0
          ? h('div', { className: 'cfgdoc-notice' }, h('div', null, t('editor.noRows')))
          : h('div', { className: 'cfgdoc-editor__form' },
              h('label', { className: 'cfgdoc-editor__field' },
                h('span', null, t('editor.row')),
                h('select', {
                  className: 'cfgdoc-editor__input',
                  value: editRowId,
                  onChange: (event) => selectRow(event.target.value),
                }, editRows.map((row) => h('option', { key: row.id, value: row.id },
                  row.name ? row.id + ' — ' + row.name : row.id)))),
              (activeRow && Array.isArray(activeRow.fields) ? activeRow.fields : []).map((field) =>
                h('label', { key: field.path, className: 'cfgdoc-editor__field' },
                  h('span', null, field.path),
                  h('input', {
                    className: 'cfgdoc-editor__input',
                    type: 'text',
                    value: editValues[field.path] === undefined ? '' : editValues[field.path],
                    onChange: (event) => setEditValues({ ...editValues, [field.path]: event.target.value }),
                  })))),
        h('div', { className: 'cfgdoc-editor__actions' },
          h('button', { type: 'button', className: 'cfgdoc-btn', onClick: () => setEditOpen(false), disabled: editBusy }, t('action.cancel')),
          h('button', { type: 'button', className: 'cfgdoc-btn', onClick: saveEditor, disabled: editBusy || editRows.length === 0 },
            editBusy ? t('action.saving') : t('action.save'))))

      return h('div', { className: 'cfgdoc-overlay', onClick: onMaskClick, key: 'overlay' },
        h('div', {
          ref: dialogRef,
          className: 'cfgdoc-dialog',
          role: 'dialog',
          'aria-modal': 'true',
          'aria-label': t('dialog.title'),
          tabIndex: -1,
          onKeyDown,
        },
          h('div', { className: 'cfgdoc-head' },
            h('div', { className: 'cfgdoc-head__text' },
              h('div', { className: 'cfgdoc-title' }, t('dialog.title')),
              path ? h('div', { className: 'cfgdoc-path' }, path) : null,
              meta.length > 0
                ? h('div', { className: 'cfgdoc-meta' },
                    meta.map((item, index) => h('span', { key: String(index) }, item)))
                : null,
            ),
            h('button', {
              type: 'button',
              className: 'cfgdoc-iconbtn',
              title: t('dialog.close'),
              'aria-label': t('dialog.close'),
              onClick: onClose,
            }, h('svg', { width: 14, height: 14, viewBox: '0 0 14 14', 'aria-hidden': true, focusable: 'false' },
              h('path', {
                d: 'M3.2 3.2l7.6 7.6M10.8 3.2l-7.6 7.6',
                fill: 'none', stroke: 'currentColor', strokeWidth: 1.4, strokeLinecap: 'round',
              }))),
          ),
          h('div', { className: 'cfgdoc-toolbar' },
            h('button', { type: 'button', className: 'cfgdoc-btn', onClick: openEditor, disabled: !ready }, t('action.edit')),
            h('button', { type: 'button', className: 'cfgdoc-btn', onClick: onCopy, disabled: !ready }, t('action.copy')),
            h('button', { type: 'button', className: 'cfgdoc-btn', onClick: onDownload, disabled: !ready }, t('action.download')),
            h('button', { type: 'button', className: 'cfgdoc-btn', onClick: onReload }, t('action.reload')),
            h('button', { type: 'button', className: 'cfgdoc-btn', onClick: onSystemOpen }, t('action.system')),
          ),
          status ? h('div', {
            className: 'cfgdoc-status',
            'data-tone': status.tone,
            role: status.tone === 'error' ? 'alert' : 'status',
          }, status.text) : null,
          h('div', { className: 'cfgdoc-body' },
            editOpen
              ? editorPane
              : ready
              ? (data.content.length === 0
                  ? h('div', { className: 'cfgdoc-notice' }, h('div', null, t('state.empty')))
                  : h('pre', { className: 'cfgdoc-code' }, data.content))
              : (state.phase === 'error'
                  ? h('div', { className: 'cfgdoc-notice' },
                      h('div', { className: 'cfgdoc-notice__title' }, t('error.title')),
                      h('div', { className: 'cfgdoc-notice__detail' }, state.error))
                  : h('div', { className: 'cfgdoc-notice' }, h('div', null, t('state.loading')))),
          ),
          h('div', { className: 'cfgdoc-guide' },
            h('div', { className: 'cfgdoc-guide__title' }, t('guide.title')),
            h('div', { className: 'cfgdoc-guide__body' }, t('guide.body', { file: path || FALLBACK_NAME })),
            h('div', { className: 'cfgdoc-guide__actions' },
              h('code', { className: 'cfgdoc-guide__path' }, path || FALLBACK_NAME),
              h('button', { type: 'button', className: 'cfgdoc-btn', onClick: copyPath }, t('guide.copyPath')))),
          h('div', { className: 'cfgdoc-foot' },
            h('div', { className: 'cfgdoc-foot__hint' }, t('hint.readonly')),
            h('div', null, t('system.hint')),
          ),
          h('textarea', {
            ref: copyRef,
            className: 'cfgdoc-sr',
            'aria-hidden': 'true',
            tabIndex: -1,
            readOnly: true,
            defaultValue: '',
          }),
          h('a', { ref: linkRef, className: 'cfgdoc-sr', 'aria-hidden': 'true', tabIndex: -1 }),
        ),
      )
    }

    /**
     * The Settings header action: one button, and the modal while it is open.
     * @param {{ t?: Function, openSystemDocument?: Function }} props - slot props.
     * @returns {any} element.
     */
    function ConfigDocumentAction(props) {
      const t = typeof props.t === 'function' ? props.t : (key) => key
      const [open, setOpen] = React.useState(false)
      const close = React.useCallback(() => setOpen(false), [])
      return h(React.Fragment, null,
        h('style', { key: 'style' }, CSS),
        h('div', { className: 'cfgdoc-root', key: 'root' },
          h('button', {
            type: 'button',
            className: 'cfgdoc-trigger',
            title: t('action.openHint'),
            'aria-haspopup': 'dialog',
            'aria-expanded': open ? 'true' : 'false',
            onClick: () => setOpen(true),
          },
            h('span', { className: 'cfgdoc-trigger__glyph' }, h(Glyph, null)),
            h('span', null, t('action.open')),
          ),
          open
            ? h(DocumentDialog, {
                key: 'dialog',
                t,
                onClose: close,
                openSystemDocument: props.openSystemDocument,
              })
            : null,
        ),
      )
    }

    /**
     * Report a registration failure without taking the plugin down: the shipped
     * button keeps working, and the console says why this half stayed out.
     * @param {string} stage - what failed.
     * @param {unknown} error - thrown value.
     */
    function reportFailure(stage, error) {
      try {
        console.warn(`config-document: ${stage} failed: ${messageOf(error)}`)
      } catch {
        // A missing console must not turn a degraded plugin into a crash.
      }
    }

    return {
      inject: ['slots', 'locale'],
      apply(ctx) {
        try {
          ctx.effect(() => ctx.locale.register(NS, { en: EN, zh: ZH }), 'config-document: dictionaries')
        } catch (error) {
          reportFailure('dictionary registration', error)
        }
        try {
          ctx.slots.inject('settings.action', () => {
            try {
              return ctx.slots.register({
                name: 'settings.action',
                id: 'open-document',
                order: 0,
                // A lower priority than the shipped cell (0) shadows it: a list-slot
                // cell resolves to its lowest-priority live entry, so this replaces the
                // shipped "Open configuration file" button instead of adding a second one.
                priority: -1,
                locale: NS,
                inject: () => ({ openSystemDocument: () => openSystemDocument(ctx) }),
              }, ConfigDocumentAction)
            } catch (error) {
              reportFailure('action registration', error)
              return () => {}
            }
          })
        } catch (error) {
          reportFailure('slot injection', error)
        }
      },
    }
  },
})
