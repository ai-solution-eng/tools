/**
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

/**
 * MCP servers — browser half.
 *
 * Registers Settings → MCP servers: a management page that lists the MCP
 * servers configured for this profile, adds new ones (Streamable HTTP or
 * stdio), edits them, disables them, and removes them.
 *
 * Two data sources meet here:
 *
 * - **Configuration** comes from the shared settings mirror,
 *   `ctx.configForms.get('mcp-servers')`, which is the same machinery the
 *   Models page uses. Every write is one atomic `servers` replacement through
 *   the Host's comment-preserving config editor, so the profile patch keeps
 *   its comments and `!!js` expressions.
 * - **Live state** comes from the Host route this package's `index.js`
 *   registers (`GET /api/mcp-servers`), polled only while the page is
 *   mounted. It reports each server's bridge fiber state and how many
 *   `mcp__<serverName>__*` tools are actually registered.
 *
 * Styling uses only `--dsw-alias-*` theme tokens, and the markup is the
 * plugin's own, so nothing here depends on a Harness Client package beyond
 * React. All visible text comes from the dictionaries below.
 */
const serversHalf = {
  factory(require) {
    const React = require('react')
    const h = React.createElement

    /** Locale namespace owned by this plugin. */
    const NS = 'mcp-servers'
    /** Host Loader entry id, which is also the settings namespace. */
    const NAMESPACE = 'mcp-servers'
    /** Host route that answers the live snapshot. */
    const ENDPOINT = 'api/mcp-servers'

/** Route that probes one unsaved draft. */
const TEST_ENDPOINT = 'api/mcp-servers/test'
    /** How often live state is re-read while the page is open. */
    const POLL_MS = 5000
    /** The bridge's `serverName` contract. */
    const SERVER_NAME_PATTERN = /^[A-Za-z0-9_-]{1,32}$/
    /** RFC 7230 field-name token, for request-header keys. */
    const HEADER_NAME_PATTERN = /^[A-Za-z0-9!#$%&'*+.^_`|~-]+$/
    /** Environment variable name. */
    const ENV_NAME_PATTERN = /^[A-Za-z_][A-Za-z0-9_]*$/
    /** Default per-call timeout, matching the bridge. */
    const DEFAULT_TIMEOUT_MS = 60000

/** Cap on server instructions; generous so large-instruction servers work. */
const DEFAULT_MAX_INSTRUCTION_BYTES = 262144

    const EN = {
      'nav': 'MCP servers',
      'title': 'MCP servers',
      'description':
        'Connect Model Context Protocol servers. Each server’s tools become available to the agent as mcp__<name>__<tool>.',
      'add': 'Add server',
      'empty.title': 'No MCP servers yet',
      'empty.body': 'Add a Streamable HTTP endpoint or a stdio command to get started.',
      'loading': 'Loading configuration…',
      'unavailable.title': 'Configuration is not available',
      'unavailable.body':
        'The MCP servers row is not active in this profile, so this page cannot read or write the server list.',
      'unavailable.retry': 'Try again',
      'bridge.title': 'The MCP bridge is unavailable',
      'bridge.body': 'Servers can still be configured, but they cannot connect until the bridge loads.',
      'state.ready': 'Ready',
      'state.starting': 'Connecting',
      'state.pending': 'Waiting',
      'state.failed': 'Failed',
      'state.disabled': 'Disabled',
      'tools.none': 'no tools',
      'tools.one': '1 tool',
      'tools.many': '{count} tools',
      'action.edit': 'Edit',
      'action.enable': 'Enable',
      'action.disable': 'Disable',
      'action.remove': 'Remove',
      'action.retry': 'Retry',
      'action.save': 'Save server',
      'action.saving': 'Saving…',
      'action.cancel': 'Cancel',
      'action.test': 'Test connection',
      'action.testing': 'Testing…',
      'test.connected': 'Connected in {ms} ms — {count} tools',
      'test.connectedNoTools': 'Connected in {ms} ms — no tools listed',
      'test.failed': 'Connection failed',
      'test.server': 'Server: {name} {version}',
      'test.stdio': 'stdio servers are not started for a connection test',
      'test.commandFound': 'command found on PATH',
      'test.commandMissing': 'command not found on PATH',
      'test.cwdMissing': 'working directory not found',
      'action.addHeader': 'Add header',
      'action.addVariable': 'Add variable',
      'action.clear': 'Clear',
      'transport.streamable-http': 'Streamable HTTP',
      'transport.stdio': 'stdio',
      'field.serverName': 'Server name',
      'field.serverName.hint': 'Letters, digits, underscore and dash. This becomes the mcp__<name>__ tool prefix.',
      'field.transport': 'Transport',
      'field.url': 'URL',
      'field.url.hint': 'Absolute http(s) address of the MCP endpoint.',
      'field.headers': 'Request headers',
      'field.headers.hint': 'Optional. Use these for API keys or tenant routing.',
      'field.command': 'Command',
      'field.command.hint': 'Executable to start, for example npx or uvx.',
      'field.args': 'Arguments',
      'field.args.hint': 'One argument per line, passed without shell interpolation.',
      'field.cwd': 'Working directory',
      'field.cwd.hint': 'Optional. Defaults to the harness working directory.',
      'field.env': 'Environment',
      'field.env.hint': 'Optional. Variables merged over the scrubbed environment.',
      'field.timeout': 'Tool call timeout',
      'field.timeout.hint': 'Milliseconds. Default {ms}.',
      'field.instructions': 'Instruction cap',
      'field.instructions.hint':
        'Bytes of server instructions allowed into the system prompt. Default {bytes}. Raise it when a server reports that its instructions exceed the cap.',
      'field.failOnStartup': 'Fail loudly when the server cannot start',
      'field.enabled': 'Enabled',
      'field.enabled.hint': 'Disabled servers keep their configuration and are not connected.',
      'editor.addTitle': 'Add MCP server',
      'editor.editTitle': 'Edit {name}',
      'placeholder.name': 'github',
      'placeholder.url': 'https://mcp.example.com/mcp',
      'placeholder.command': 'npx',
      'placeholder.args': '-y\n@modelcontextprotocol/server-github',
      'placeholder.cwd': '/srv/workspace',
      'placeholder.key': 'Name',
      'placeholder.value': 'Value',
      'error.nameRequired': 'Enter a server name.',
      'error.namePattern': 'Use 1-32 characters from A-Z, a-z, 0-9, underscore or dash.',
      'error.nameTaken': 'Another server already uses this name.',
      'error.urlRequired': 'Enter the endpoint URL.',
      'error.urlInvalid': 'Enter an absolute http or https URL.',
      'error.commandRequired': 'Enter the command to start.',
      'error.timeout': 'Enter a positive whole number of milliseconds.',
      'error.instructions': 'Enter a positive whole number of bytes.',
      'error.headerName': 'Header names may only contain letters, digits and token characters.',
      'error.envName': 'Variable names may only contain letters, digits and underscore.',
      'error.duplicateKey': 'This name is already used above.',
      'error.saveFailed': 'The server was not saved. Check the values and try again.',
      'error.removeFailed': 'The server was not removed. Try again.',
      'error.unavailableWrite': 'Configuration is not available, so this change cannot be saved.',
      'confirm.remove': 'Remove {name}?',
      'confirm.removeBody': 'Its configuration is deleted and its tools stop being available.',
      'confirm.keep': 'Keep',
      'problem.title': 'Cannot start',
      'external.title': 'Configured elsewhere',
      'external.body':
        'These MCP servers come from the profile configuration. Edit them in the configuration file; this page only reports them.',
      'external.disabled': 'Disabled',
      'note.footer': 'Changes are saved to this profile’s configuration file.',
    }

    const ZH = {
      'nav': 'MCP 服务器',
      'title': 'MCP 服务器',
      'description': '连接模型上下文协议（MCP）服务器。每个服务器的工具会以 mcp__<名称>__<工具> 的形式提供给智能体。',
      'add': '添加服务器',
      'empty.title': '还没有 MCP 服务器',
      'empty.body': '添加一个 Streamable HTTP 端点或一条 stdio 命令即可开始。',
      'loading': '正在读取配置…',
      'unavailable.title': '配置不可用',
      'unavailable.body': '此配置档中 MCP 服务器条目未激活，因此本页面无法读取或写入服务器列表。',
      'unavailable.retry': '重试',
      'bridge.title': 'MCP 桥接插件不可用',
      'bridge.body': '仍可配置服务器，但在桥接插件加载之前无法建立连接。',
      'state.ready': '就绪',
      'state.starting': '连接中',
      'state.pending': '等待中',
      'state.failed': '失败',
      'state.disabled': '已禁用',
      'tools.none': '无工具',
      'tools.one': '1 个工具',
      'tools.many': '{count} 个工具',
      'action.edit': '编辑',
      'action.enable': '启用',
      'action.disable': '禁用',
      'action.remove': '删除',
      'action.retry': '重试',
      'action.save': '保存服务器',
      'action.saving': '保存中…',
      'action.cancel': '取消',
      'action.test': '测试连接',
      'action.testing': '测试中…',
      'test.connected': '已连接，耗时 {ms} 毫秒，{count} 个工具',
      'test.connectedNoTools': '已连接，耗时 {ms} 毫秒，未列出工具',
      'test.failed': '连接失败',
      'test.server': '服务器：{name} {version}',
      'test.stdio': 'stdio 服务器不会为连接测试而启动',
      'test.commandFound': '在 PATH 中找到命令',
      'test.commandMissing': '在 PATH 中未找到命令',
      'test.cwdMissing': '工作目录不存在',
      'action.addHeader': '添加请求头',
      'action.addVariable': '添加变量',
      'action.clear': '清除',
      'transport.streamable-http': 'Streamable HTTP',
      'transport.stdio': 'stdio',
      'field.serverName': '服务器名称',
      'field.serverName.hint': '字母、数字、下划线和连字符。它将作为 mcp__<名称>__ 工具前缀。',
      'field.transport': '传输方式',
      'field.url': 'URL',
      'field.url.hint': 'MCP 端点的绝对 http(s) 地址。',
      'field.headers': '请求头',
      'field.headers.hint': '可选。用于 API 密钥或租户路由。',
      'field.command': '命令',
      'field.command.hint': '要启动的可执行文件，例如 npx 或 uvx。',
      'field.args': '参数',
      'field.args.hint': '每行一个参数，不经过 shell 解析。',
      'field.cwd': '工作目录',
      'field.cwd.hint': '可选。默认为 harness 工作目录。',
      'field.env': '环境变量',
      'field.env.hint': '可选。合并到已清理的环境之上。',
      'field.timeout': '工具调用超时',
      'field.timeout.hint': '毫秒。默认 {ms}。',
      'field.instructions': '指令上限',
      'field.instructions.hint': '允许进入系统提示的服务器指令字节数。默认 {bytes}。当服务器报告指令超过上限时调高此值。',
      'field.failOnStartup': '服务器无法启动时报告失败',
      'field.enabled': '启用',
      'field.enabled.hint': '禁用的服务器保留配置，但不建立连接。',
      'editor.addTitle': '添加 MCP 服务器',
      'editor.editTitle': '编辑 {name}',
      'placeholder.name': 'github',
      'placeholder.url': 'https://mcp.example.com/mcp',
      'placeholder.command': 'npx',
      'placeholder.args': '-y\n@modelcontextprotocol/server-github',
      'placeholder.cwd': '/srv/workspace',
      'placeholder.key': '名称',
      'placeholder.value': '值',
      'error.nameRequired': '请输入服务器名称。',
      'error.namePattern': '请使用 1-32 个字符：A-Z、a-z、0-9、下划线或连字符。',
      'error.nameTaken': '另一个服务器已使用此名称。',
      'error.urlRequired': '请输入端点 URL。',
      'error.urlInvalid': '请输入绝对的 http 或 https URL。',
      'error.commandRequired': '请输入要启动的命令。',
      'error.timeout': '请输入正整数毫秒值。',
      'error.instructions': '请输入正整数字节数。',
      'error.headerName': '请求头名称只能包含字母、数字和标记字符。',
      'error.envName': '变量名只能包含字母、数字和下划线。',
      'error.duplicateKey': '上面的列表中已存在该名称。',
      'error.saveFailed': '服务器未保存。请检查取值后重试。',
      'error.removeFailed': '服务器未删除。请重试。',
      'error.unavailableWrite': '配置不可用，无法保存此次更改。',
      'confirm.remove': '删除 {name}？',
      'confirm.removeBody': '其配置将被删除，工具也将不再可用。',
      'confirm.keep': '保留',
      'problem.title': '无法启动',
      'external.title': '在其他位置配置',
      'external.body': '这些 MCP 服务器来自配置档配置。请在配置文件中修改，本页面仅作展示。',
      'external.disabled': '已禁用',
      'note.footer': '更改会保存到此配置档的配置文件中。',
    }

    const CSS = [
      '.dsh-mcp-servers{display:flex;flex-direction:column;gap:16px;color:var(--dsw-alias-label-primary);font-size:14px;line-height:22px}',
      '.dsh-mcp-servers__head{display:flex;align-items:flex-start;justify-content:space-between;gap:16px}',
      '.dsh-mcp-servers__heading{display:flex;flex-direction:column;gap:4px;min-width:0}',
      '.dsh-mcp-servers__title{margin:0;font-size:16px;font-weight:500;line-height:24px;color:var(--dsw-alias-label-primary)}',
      '.dsh-mcp-servers__desc{margin:0;color:var(--dsw-alias-label-secondary);font-size:14px;line-height:22px}',
      '.dsh-mcp-servers__primary{flex:none;height:32px;padding:0 14px;border:1px solid transparent;border-radius:8px;background:var(--dsw-alias-button-primary-fill);color:var(--dsw-alias-label-primary-foreground);font:inherit;font-size:14px;cursor:pointer}',
      '.dsh-mcp-servers__primary:hover{background:var(--dsw-alias-button-primary-hover)}',
      '.dsh-mcp-servers__primary:disabled{cursor:default;opacity:.55}',
      '.dsh-mcp-servers__primary:focus-visible{outline:2px solid var(--dsw-alias-brand-primary);outline-offset:1px}',
      '.dsh-mcp-servers__ghost{height:28px;padding:0 10px;border:1px solid var(--dsw-alias-border-l2);border-radius:6px;background:transparent;color:var(--dsw-alias-label-secondary);font:inherit;font-size:13px;cursor:pointer}',
      '.dsh-mcp-servers__ghost:hover{background:var(--dsw-alias-interactive-bg-hover);color:var(--dsw-alias-label-primary)}',
      '.dsh-mcp-servers__ghost:disabled{cursor:default;opacity:.5}',
      '.dsh-mcp-servers__ghost:focus-visible{outline:2px solid var(--dsw-alias-brand-primary);outline-offset:1px}',
      '.dsh-mcp-servers__ghost--danger:hover{background:var(--dsw-alias-interactive-bg-hover-danger);color:var(--dsw-alias-state-error-primary)}',
      '.dsh-mcp-servers__notice{display:flex;flex-direction:column;gap:2px;padding:10px 12px;border:1px solid var(--dsw-alias-border-l2);border-radius:8px;background:var(--dsw-alias-bg-layer-1);color:var(--dsw-alias-label-secondary);font-size:13px;line-height:20px}',
      '.dsh-mcp-servers__notice--warn{border-color:var(--dsw-alias-state-warn-primary);color:var(--dsw-alias-label-primary)}',
      '.dsh-mcp-servers__notice--error{border-color:var(--dsw-alias-state-error-primary);color:var(--dsw-alias-label-primary)}',
      '.dsh-mcp-servers__noticeTitle{font-weight:500;color:var(--dsw-alias-label-primary)}',
      '.dsh-mcp-servers__list{display:flex;flex-direction:column;gap:8px;margin:0;padding:0;list-style:none}',
      '.dsh-mcp-servers__row{display:flex;align-items:flex-start;justify-content:space-between;gap:12px;padding:12px;border:1px solid var(--dsw-alias-border-l2);border-radius:10px;background:var(--dsw-alias-bg-layer-1)}',
      '.dsh-mcp-servers__rowMain{display:flex;flex-direction:column;gap:3px;min-width:0;flex:1}',
      '.dsh-mcp-servers__rowTitle{display:flex;align-items:center;gap:8px;flex-wrap:wrap;min-width:0}',
      '.dsh-mcp-servers__name{font-weight:500;color:var(--dsw-alias-label-primary);overflow-wrap:anywhere}',
      '.dsh-mcp-servers__chip{flex:none;padding:0 6px;border:1px solid var(--dsw-alias-border-l2);border-radius:5px;color:var(--dsw-alias-label-secondary);font-size:11px;line-height:18px}',
      '.dsh-mcp-servers__dot{flex:none;width:7px;height:7px;border-radius:50%;background:var(--dsw-alias-state-idle-primary)}',
      '.dsh-mcp-servers__dot[data-tone="ok"]{background:var(--dsw-alias-state-success-primary)}',
      '.dsh-mcp-servers__dot[data-tone="warn"]{background:var(--dsw-alias-state-warn-primary)}',
      '.dsh-mcp-servers__dot[data-tone="error"]{background:var(--dsw-alias-state-error-primary)}',
      '.dsh-mcp-servers__endpoint{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12px;line-height:18px;color:var(--dsw-alias-label-secondary);overflow-wrap:anywhere}',
      '.dsh-mcp-servers__meta{color:var(--dsw-alias-label-tertiary);font-size:12px;line-height:18px}',
      '.dsh-mcp-servers__error{color:var(--dsw-alias-state-error-primary);font-size:12px;line-height:18px;overflow-wrap:anywhere}',
      '.dsh-mcp-servers__rowActions{display:flex;align-items:center;gap:6px;flex:none;flex-wrap:wrap;justify-content:flex-end}',
      '.dsh-mcp-servers__empty{display:flex;flex-direction:column;gap:4px;padding:20px 12px;border:1px dashed var(--dsw-alias-border-l2);border-radius:10px;text-align:center;color:var(--dsw-alias-label-secondary)}',
      '.dsh-mcp-servers__emptyTitle{color:var(--dsw-alias-label-primary);font-weight:500}',
      '.dsh-mcp-servers__card{display:flex;flex-direction:column;gap:14px;padding:16px;border:1px solid var(--dsw-alias-border-l2);border-radius:10px;background:var(--dsw-alias-bg-layer-2)}',
      '.dsh-mcp-servers__cardTitle{margin:0;font-size:14px;font-weight:500;line-height:22px;color:var(--dsw-alias-label-primary)}',
      '.dsh-mcp-servers__fields{display:flex;flex-direction:column;gap:12px}',
      '.dsh-mcp-servers__field{display:flex;flex-direction:column;gap:4px;min-width:0}',
      '.dsh-mcp-servers__label{color:var(--dsw-alias-label-primary);font-size:13px;line-height:20px}',
      '.dsh-mcp-servers__hint{color:var(--dsw-alias-label-tertiary);font-size:12px;line-height:18px}',
      '.dsh-mcp-servers__input,.dsh-mcp-servers__select,.dsh-mcp-servers__textarea{box-sizing:border-box;width:100%;min-width:0;padding:6px 10px;border:1px solid var(--dsw-alias-border-l2);border-radius:8px;background:var(--dsw-alias-bg-base);color:var(--dsw-alias-label-primary);font:inherit;font-size:14px;line-height:22px}',
      '.dsh-mcp-servers__input:focus-visible,.dsh-mcp-servers__select:focus-visible,.dsh-mcp-servers__textarea:focus-visible{outline:2px solid var(--dsw-alias-brand-primary);outline-offset:1px}',
      '.dsh-mcp-servers__input[aria-invalid="true"]{border-color:var(--dsw-alias-state-error-primary)}',
      '.dsh-mcp-servers__textarea{resize:vertical;min-height:56px;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12px;line-height:18px}',
      '.dsh-mcp-servers__row2{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:12px}',
      '.dsh-mcp-servers__kv{display:flex;flex-direction:column;gap:6px}',
      '.dsh-mcp-servers__kvRow{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1.4fr) auto;gap:6px;align-items:center}',
      '.dsh-mcp-servers__check{display:flex;align-items:flex-start;gap:8px}',
      '.dsh-mcp-servers__check input{margin:3px 0 0}',
      '.dsh-mcp-servers__fieldError{color:var(--dsw-alias-state-error-primary);font-size:12px;line-height:18px}',
      '.dsh-mcp-servers__actions{display:flex;align-items:center;gap:8px;justify-content:flex-end}',
      '.dsh-mcp-servers__testButton{margin-right:auto}',
      '.dsh-mcp-servers__testResult{display:flex;flex-direction:column;gap:2px;padding:8px 10px;border-radius:8px;border:1px solid var(--dsw-alias-border-l2);background:var(--dsw-alias-bg-base)}',
      '.dsh-mcp-servers__testResult[data-tone="ok"]{border-color:var(--dsw-alias-state-success-primary)}',
      '.dsh-mcp-servers__testResult[data-tone="error"]{border-color:var(--dsw-alias-state-error-primary)}',
      '.dsh-mcp-servers__testText{font-size:12px;line-height:18px;color:var(--dsw-alias-label-secondary);overflow-wrap:anywhere}',
      '.dsh-mcp-servers__testResult[data-tone="ok"] .dsh-mcp-servers__testText{color:var(--dsw-alias-state-success-primary)}',
      '.dsh-mcp-servers__testResult[data-tone="error"] .dsh-mcp-servers__testText{color:var(--dsw-alias-state-error-primary)}',
      '.dsh-mcp-servers__sectionTitle{margin:0;font-size:13px;font-weight:500;line-height:20px;color:var(--dsw-alias-label-secondary)}',
      '.dsh-mcp-servers__footer{color:var(--dsw-alias-label-tertiary);font-size:12px;line-height:18px}',
      '.dsh-mcp-servers__external{display:flex;flex-direction:column;gap:8px}',
      '@media (max-width:640px){.dsh-mcp-servers__head{flex-direction:column}.dsh-mcp-servers__row{flex-direction:column}.dsh-mcp-servers__rowActions{justify-content:flex-start}.dsh-mcp-servers__row2{grid-template-columns:minmax(0,1fr)}}',
    ].join('')

    /* ---------------------------------------------------------------- *
     * Helpers
     * ---------------------------------------------------------------- */

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
     * Whether a value is an absolute http(s) URL.
     * @param {string} value - candidate.
     * @returns {boolean} true for a usable endpoint.
     */
    function isHttpUrl(value) {
      if (typeof value !== 'string' || !value) return false
      try {
        const url = new URL(value)
        return url.protocol === 'http:' || url.protocol === 'https:'
      } catch {
        return false
      }
    }

    /**
     * Turn a server name into a stable row id.
     * @param {string} serverName - configured server name.
     * @returns {string} slug.
     */
    function slugOf(serverName) {
      const slug = String(serverName ?? '')
        .toLowerCase()
        .replace(/[^a-z0-9]+/g, '-')
        .replace(/^-+|-+$/g, '')
        .slice(0, 32)
      return slug || 'server'
    }

    /**
     * Allocate a row id that is not already taken.
     * @param {string} serverName - configured server name.
     * @param {string[]} taken - ids in use.
     * @returns {string} unique id.
     */
    function allocateId(serverName, taken) {
      const base = slugOf(serverName)
      let candidate = base
      let suffix = 2
      while (taken.includes(candidate)) {
        candidate = `${base}-${suffix}`
        suffix += 1
      }
      return candidate
    }

    /**
     * Convert a dictionary into editable key/value rows.
     * @param {unknown} value - configured dictionary.
     * @returns {Array<{ key: string, value: string }>} rows.
     */
    function pairsOf(value) {
      if (!value || typeof value !== 'object' || Array.isArray(value)) return []
      return Object.entries(value).map(([key, entry]) => ({ key, value: String(entry ?? '') }))
    }

    /**
     * Convert editable key/value rows back into a dictionary, skipping blank keys.
     * @param {Array<{ key: string, value: string }>} pairs - rows.
     * @returns {Record<string, string>} dictionary.
     */
    function dictOf(pairs) {
      const result = {}
      for (const pair of pairs ?? []) {
        const key = String(pair.key ?? '').trim()
        if (!key) continue
        result[key] = String(pair.value ?? '')
      }
      return result
    }

    /**
     * An empty editor draft.
     * @returns {Record<string, any>} draft.
     */
    function emptyDraft() {
      return {
        id: '',
        serverName: '',
        transport: 'streamable-http',
        url: '',
        headers: [],
        command: '',
        argsText: '',
        cwd: '',
        env: [],
        toolCallTimeoutMs: String(DEFAULT_TIMEOUT_MS),
        maxInstructionBytes: String(DEFAULT_MAX_INSTRUCTION_BYTES),
        failOnStartupError: false,
        enabled: true,
      }
    }

    /**
     * Build an editor draft from a configured row.
     * @param {Record<string, any>} row - configured row.
     * @returns {Record<string, any>} draft.
     */
    function draftOf(row) {
      const transport = row.transport === 'stdio' ? 'stdio' : 'streamable-http'
      return {
        id: typeof row.id === 'string' ? row.id : '',
        serverName: typeof row.serverName === 'string' ? row.serverName : '',
        transport,
        url: typeof row.url === 'string' ? row.url : '',
        headers: pairsOf(row.headers),
        command: typeof row.command === 'string' ? row.command : '',
        argsText: Array.isArray(row.args) ? row.args.join('\n') : '',
        cwd: typeof row.cwd === 'string' ? row.cwd : '',
        env: pairsOf(row.env),
        toolCallTimeoutMs: String(row.toolCallTimeoutMs ?? DEFAULT_TIMEOUT_MS),
        maxInstructionBytes: String(row.maxInstructionBytes ?? DEFAULT_MAX_INSTRUCTION_BYTES),
        failOnStartupError: row.failOnStartupError === true,
        enabled: row.enabled !== false,
      }
    }

    /**
     * Validate one draft against the rest of the list.
     * @param {Record<string, any>} draft - editor draft.
     * @param {Array<Record<string, any>>} rows - configured rows.
     * @param {(key: string, params?: Record<string, unknown>) => string} t - translator.
     * @returns {{ fields: Record<string, string>, keys: { headers?: string, env?: string } }} errors, empty when valid.
     */
    function validateDraft(draft, rows, t) {
      const fields = {}
      const keys = {}
      const name = String(draft.serverName ?? '').trim()
      if (!name) fields.serverName = t('error.nameRequired')
      else if (!SERVER_NAME_PATTERN.test(name)) fields.serverName = t('error.namePattern')
      else {
        const clash = rows.some(
          (row) => row.id !== draft.id && String(row.serverName ?? '').trim() === name,
        )
        if (clash) fields.serverName = t('error.nameTaken')
      }
      if (draft.transport === 'stdio') {
        if (!String(draft.command ?? '').trim()) fields.command = t('error.commandRequired')
      } else {
        const url = String(draft.url ?? '').trim()
        if (!url) fields.url = t('error.urlRequired')
        else if (!isHttpUrl(url)) fields.url = t('error.urlInvalid')
      }
      const timeout = String(draft.toolCallTimeoutMs ?? '').trim()
      if (timeout) {
        const parsed = Number(timeout)
        if (!Number.isInteger(parsed) || parsed <= 0) fields.toolCallTimeoutMs = t('error.timeout')
      }
      const cap = String(draft.maxInstructionBytes ?? '').trim()
      if (cap) {
        const parsedCap = Number(cap)
        if (!Number.isInteger(parsedCap) || parsedCap <= 0) fields.maxInstructionBytes = t('error.instructions')
      }
      const checkPairs = (pairs, pattern, messageKey) => {
        const seen = new Set()
        for (const pair of pairs ?? []) {
          const key = String(pair.key ?? '').trim()
          if (!key) continue
          if (!pattern.test(key)) return messageKey
          const folded = key.toLowerCase()
          if (seen.has(folded)) return 'error.duplicateKey'
          seen.add(folded)
        }
        return undefined
      }
      const headerError = checkPairs(draft.headers, HEADER_NAME_PATTERN, 'error.headerName')
      if (headerError) keys.headers = t(headerError)
      const envError = checkPairs(draft.env, ENV_NAME_PATTERN, 'error.envName')
      if (envError) keys.env = t(envError)
      return { fields, keys }
    }

    /**
     * Build a configured row from a validated draft.
     * @param {Record<string, any>} draft - editor draft.
     * @param {string[]} takenIds - ids in use by other rows.
     * @returns {Record<string, any>} configured row.
     */
    function rowOfDraft(draft, takenIds) {
      const serverName = String(draft.serverName ?? '').trim()
      const id = draft.id || allocateId(serverName, takenIds)
      const timeout = Number(String(draft.toolCallTimeoutMs ?? '').trim() || DEFAULT_TIMEOUT_MS)
      const cap = Number(String(draft.maxInstructionBytes ?? '').trim() || DEFAULT_MAX_INSTRUCTION_BYTES)
      const row = {
        id,
        serverName,
        transport: draft.transport === 'stdio' ? 'stdio' : 'streamable-http',
        toolCallTimeoutMs: Number.isFinite(timeout) && timeout > 0 ? Math.floor(timeout) : DEFAULT_TIMEOUT_MS,
        maxInstructionBytes: Number.isFinite(cap) && cap > 0 ? Math.floor(cap) : DEFAULT_MAX_INSTRUCTION_BYTES,
        failOnStartupError: draft.failOnStartupError === true,
        enabled: draft.enabled !== false,
      }
      if (row.transport === 'stdio') {
        row.command = String(draft.command ?? '').trim()
        row.args = String(draft.argsText ?? '')
          .split('\n')
          .map((line) => line.trim())
          .filter((line) => line.length > 0)
        if (String(draft.cwd ?? '').trim()) row.cwd = String(draft.cwd).trim()
        const env = dictOf(draft.env)
        if (Object.keys(env).length > 0) row.env = env
      } else {
        row.url = String(draft.url ?? '').trim()
        const headers = dictOf(draft.headers)
        if (Object.keys(headers).length > 0) row.headers = headers
      }
      return row
    }

    /**
     * Map a live bridge state onto a locale key.
     * @param {string | undefined} state - reported state.
     * @returns {string} locale key.
     */
    function stateKey(state) {
      if (state === 'ready') return 'state.ready'
      if (state === 'starting') return 'state.starting'
      if (state === 'failed') return 'state.failed'
      return 'state.pending'
    }

    /**
     * Tone of one live state.
     * @param {string | undefined} state - reported state.
     * @returns {'ok' | 'warn' | 'error' | 'idle'} tone.
     */
    function stateTone(state) {
      if (state === 'ready') return 'ok'
      if (state === 'failed') return 'error'
      if (state === 'starting' || state === 'pending') return 'warn'
      return 'idle'
    }

    /* ---------------------------------------------------------------- *
     * Stores
     * ---------------------------------------------------------------- */

    /**
     * Subscribe to a snapshot store (a settings form or the live store).
     * @param {{ getSnapshot: () => any, subscribe: (listener: () => void) => () => void }} store - store.
     * @returns {any} the current snapshot.
     */
    function useStore(store) {
      const [snapshot, setSnapshot] = React.useState(() => (store ? store.getSnapshot() : undefined))
      React.useEffect(() => {
        if (!store) return undefined
        let active = true
        const sync = () => {
          if (active) setSnapshot(store.getSnapshot())
        }
        sync()
        const unsubscribe = store.subscribe(sync)
        return () => {
          active = false
          if (typeof unsubscribe === 'function') unsubscribe()
        }
      }, [store])
      return snapshot
    }

    /**
     * Poll the Host snapshot while at least one component is subscribed.
     * @returns {{ getSnapshot: () => any, subscribe: (listener: () => void) => () => void, refresh: (retry?: boolean) => Promise<void> }} store.
     */
    function createLiveStore() {
      let snapshot = { status: 'idle', data: null, error: null }
      const listeners = new Set()
      let timer = null
      let inFlight = null

      const emit = () => {
        for (const listener of [...listeners]) {
          try {
            listener()
          } catch {
            // A failing subscriber must not stop the others.
          }
        }
      }

      const load = async (retry) => {
        const url = retry ? `${ENDPOINT}?retry=1` : ENDPOINT
        try {
          const response = await fetch(url, {
            credentials: 'same-origin',
            cache: 'no-store',
            headers: { accept: 'application/json' },
          })
          if (!response.ok) throw new Error(`HTTP ${response.status}`)
          const data = await response.json()
          if (!data || typeof data !== 'object') throw new Error('unexpected response')
          snapshot = { status: 'ready', data, error: null }
        } catch (error) {
          snapshot = { status: snapshot.data ? 'ready' : 'error', data: snapshot.data, error: messageOf(error) }
        }
        emit()
      }

      const schedule = () => {
        if (listeners.size === 0) return
        if (timer !== null) return
        timer = setTimeout(() => {
          timer = null
          void tick()
        }, POLL_MS)
      }

      const tick = async () => {
        if (inFlight) return inFlight
        inFlight = load(false).finally(() => {
          inFlight = null
          schedule()
        })
        return inFlight
      }

      return {
        getSnapshot: () => snapshot,
        subscribe(listener) {
          listeners.add(listener)
          if (listeners.size === 1) void tick()
          return () => {
            listeners.delete(listener)
            if (listeners.size === 0 && timer !== null) {
              clearTimeout(timer)
              timer = null
            }
          }
        },
        refresh(retry) {
          if (inFlight) return inFlight
          inFlight = load(retry === true).finally(() => {
            inFlight = null
            schedule()
          })
          return inFlight
        },
      }
    }

    /* ---------------------------------------------------------------- *
     * Controls
     * ---------------------------------------------------------------- */

    /**
     * A labelled text input.
     * @param {{ id: string, label: string, value: string, hint?: string, error?: string, placeholder?: string, mono?: boolean, onChange: (value: string) => void }} props - field props.
     * @returns {any} element.
     */
    function TextField(props) {
      const inputId = `dsh-mcp-servers-${props.id}`
      return h('div', { className: 'dsh-mcp-servers__field' },
        h('label', { className: 'dsh-mcp-servers__label', htmlFor: inputId }, props.label),
        h('input', {
          id: inputId,
          className: 'dsh-mcp-servers__input',
          type: 'text',
          value: props.value ?? '',
          placeholder: props.placeholder,
          spellCheck: false,
          autoComplete: 'off',
          'aria-invalid': props.error ? 'true' : undefined,
          onChange: (event) => props.onChange(event.target.value),
        }),
        props.error
          ? h('span', { className: 'dsh-mcp-servers__fieldError' }, props.error)
          : props.hint
            ? h('span', { className: 'dsh-mcp-servers__hint' }, props.hint)
            : null,
      )
    }

    /**
     * A labelled textarea.
     * @param {{ id: string, label: string, value: string, hint?: string, placeholder?: string, onChange: (value: string) => void }} props - field props.
     * @returns {any} element.
     */
    function TextAreaField(props) {
      const inputId = `dsh-mcp-servers-${props.id}`
      return h('div', { className: 'dsh-mcp-servers__field' },
        h('label', { className: 'dsh-mcp-servers__label', htmlFor: inputId }, props.label),
        h('textarea', {
          id: inputId,
          className: 'dsh-mcp-servers__textarea',
          rows: 3,
          value: props.value ?? '',
          placeholder: props.placeholder,
          spellCheck: false,
          onChange: (event) => props.onChange(event.target.value),
        }),
        props.hint ? h('span', { className: 'dsh-mcp-servers__hint' }, props.hint) : null,
      )
    }

    /**
     * A labelled checkbox row.
     * @param {{ id: string, label: string, hint?: string, checked: boolean, onChange: (checked: boolean) => void }} props - field props.
     * @returns {any} element.
     */
    function CheckField(props) {
      const inputId = `dsh-mcp-servers-${props.id}`
      return h('div', { className: 'dsh-mcp-servers__field' },
        h('div', { className: 'dsh-mcp-servers__check' },
          h('input', {
            id: inputId,
            type: 'checkbox',
            checked: props.checked === true,
            onChange: (event) => props.onChange(event.target.checked),
          }),
          h('label', { className: 'dsh-mcp-servers__label', htmlFor: inputId }, props.label),
        ),
        props.hint ? h('span', { className: 'dsh-mcp-servers__hint' }, props.hint) : null,
      )
    }

    /**
     * Editable key/value pairs.
     * @param {{ id: string, label: string, hint?: string, error?: string, addLabel: string, pairs: Array<{ key: string, value: string }>, keyPlaceholder: string, valuePlaceholder: string, onChange: (pairs: Array<{ key: string, value: string }>) => void }} props - editor props.
     * @returns {any} element.
     */
    function KeyValueEditor(props) {
      const pairs = Array.isArray(props.pairs) ? props.pairs : []
      const update = (index, patch) => {
        props.onChange(pairs.map((pair, position) => (position === index ? { ...pair, ...patch } : pair)))
      }
      const remove = (index) => {
        props.onChange(pairs.filter((_, position) => position !== index))
      }
      return h('div', { className: 'dsh-mcp-servers__field' },
        h('span', { className: 'dsh-mcp-servers__label' }, props.label),
        h('div', { className: 'dsh-mcp-servers__kv' },
          pairs.map((pair, index) =>
            h('div', { className: 'dsh-mcp-servers__kvRow', key: `${props.id}-${index}` },
              h('input', {
                className: 'dsh-mcp-servers__input',
                type: 'text',
                value: pair.key ?? '',
                placeholder: props.keyPlaceholder,
                spellCheck: false,
                autoComplete: 'off',
                'aria-label': props.keyPlaceholder,
                onChange: (event) => update(index, { key: event.target.value }),
              }),
              h('input', {
                className: 'dsh-mcp-servers__input',
                type: 'text',
                value: pair.value ?? '',
                placeholder: props.valuePlaceholder,
                spellCheck: false,
                autoComplete: 'off',
                'aria-label': props.valuePlaceholder,
                onChange: (event) => update(index, { value: event.target.value }),
              }),
              h('button', {
                type: 'button',
                className: 'dsh-mcp-servers__ghost',
                onClick: () => remove(index),
              }, props.removeLabel),
            ),
          ),
          h('button', {
            type: 'button',
            className: 'dsh-mcp-servers__ghost',
            onClick: () => props.onChange([...pairs, { key: '', value: '' }]),
          }, props.addLabel),
        ),
        props.error
          ? h('span', { className: 'dsh-mcp-servers__fieldError' }, props.error)
          : props.hint
            ? h('span', { className: 'dsh-mcp-servers__hint' }, props.hint)
            : null,
      )
    }

    /* ---------------------------------------------------------------- *
     * Rows
     * ---------------------------------------------------------------- */

    /**
     * One configured server.
     * @param {{ row: any, live: any, problem: any, t: Function, onEdit: Function, onToggle: Function, onRemove: Function }} props - row props.
     * @returns {any} element.
     */
    function ServerRow(props) {
      const { row, live, problem, t } = props
      const enabled = row.enabled !== false
      const endpoint = row.transport === 'stdio' ? row.command : row.url
      const state = enabled ? live?.state : 'disabled'
      const toolCount = typeof live?.toolCount === 'number' ? live.toolCount : undefined
      const metaText = enabled && typeof toolCount === 'number'
        ? `${t(stateKey(state))} · ${toolCount === 1 ? t('tools.one') : t('tools.many', { count: toolCount })}`
        : t(enabled ? stateKey(state) : 'state.disabled')
      const error = problem?.message ?? live?.error
      return h('li', { className: 'dsh-mcp-servers__row' },
        h('div', { className: 'dsh-mcp-servers__rowMain' },
          h('div', { className: 'dsh-mcp-servers__rowTitle' },
            h('span', {
              className: 'dsh-mcp-servers__dot',
              'data-tone': problem ? 'error' : enabled ? stateTone(state) : 'idle',
              'aria-hidden': true,
            }),
            h('span', { className: 'dsh-mcp-servers__name' }, row.serverName || t('field.serverName')),
            h('span', { className: 'dsh-mcp-servers__chip' }, t(`transport.${row.transport === 'stdio' ? 'stdio' : 'streamable-http'}`)),
            enabled ? null : h('span', { className: 'dsh-mcp-servers__chip' }, t('state.disabled')),
          ),
          endpoint ? h('div', { className: 'dsh-mcp-servers__endpoint' }, endpoint) : null,
          h('div', { className: 'dsh-mcp-servers__meta' }, metaText),
          error
            ? h('div', { className: 'dsh-mcp-servers__error' }, problem ? `${t('problem.title')}: ${error}` : error)
            : null,
        ),
        h('div', { className: 'dsh-mcp-servers__rowActions' },
          h('button', { type: 'button', className: 'dsh-mcp-servers__ghost', onClick: () => props.onEdit(row) }, t('action.edit')),
          h('button', {
            type: 'button',
            className: 'dsh-mcp-servers__ghost',
            onClick: () => props.onToggle(row),
          }, enabled ? t('action.disable') : t('action.enable')),
          h('button', {
            type: 'button',
            className: 'dsh-mcp-servers__ghost dsh-mcp-servers__ghost--danger',
            onClick: () => props.onRemove(row),
          }, t('action.remove')),
        ),
      )
    }

    /**
     * The add/edit form.
     * @param {{ draft: any, rows: any[], t: Function, busy: boolean, error: string | null, onChange: Function, onSave: Function, onCancel: Function }} props - editor props.
     * @returns {any} element.
     */
    function ServerEditor(props) {
      const { draft, rows, t } = props
      const [errors, setErrors] = React.useState({ fields: {}, keys: {} })
      const [probe, setProbe] = React.useState(null)
      const patch = (next) => props.onChange({ ...draft, ...next })
      /**
       * Probe the unsaved draft against its endpoint. The Host answers with the
       * handshake result, the tool count and the server identity, so a wrong URL
       * or a missing header is caught here instead of surfacing later as a row
       * stuck in Failed.
       */
      const runTest = async () => {
        setProbe({ status: 'running' })
        try {
          const payload = rowOfDraft(
            { ...draft, id: draft.id || 'test', serverName: draft.serverName || 'test' },
            [],
          )
          const response = await fetch(TEST_ENDPOINT, {
            method: 'POST',
            credentials: 'same-origin',
            cache: 'no-store',
            headers: { 'content-type': 'application/json', accept: 'application/json' },
            body: JSON.stringify(payload),
          })
          const data = await response.json().catch(() => null)
          if (!data || typeof data !== 'object') throw new Error(`HTTP ${response.status}`)
          setProbe({ status: 'done', result: data })
        } catch (error) {
          setProbe({ status: 'done', result: { ok: false, error: messageOf(error) } })
        }
      }
      const probeResult = probe && probe.status === 'done' ? probe.result : null
      const probeTone = probeResult ? (probeResult.stdio ? 'info' : probeResult.ok ? 'ok' : 'error') : 'info'
      let probeText = ''
      if (probeResult) {
        if (probeResult.stdio) {
          probeText = `${t('test.stdio')} — ${probeResult.commandFound ? t('test.commandFound') : t('test.commandMissing')}`
          if (probeResult.cwd === 'missing' || probeResult.cwd === 'not-a-directory') probeText += `, ${t('test.cwdMissing')}`
        } else if (probeResult.ok) {
          probeText = probeResult.toolCount > 0
            ? t('test.connected', { ms: probeResult.latencyMs, count: probeResult.toolCount })
            : t('test.connectedNoTools', { ms: probeResult.latencyMs })
        } else {
          probeText = `${t('test.failed')}: ${probeResult.error || 'unknown error'}`
        }
      }
      const probeServer = probeResult && probeResult.serverInfo && probeResult.serverInfo.name
        ? t('test.server', { name: probeResult.serverInfo.name, version: probeResult.serverInfo.version || '' })
        : ''
      const submit = (event) => {
        if (event && typeof event.preventDefault === 'function') event.preventDefault()
        const found = validateDraft(draft, rows, t)
        setErrors(found)
        if (Object.keys(found.fields).length > 0 || Object.keys(found.keys).length > 0) return
        props.onSave(draft)
      }
      const stdio = draft.transport === 'stdio'
      return h('form', { className: 'dsh-mcp-servers__card', onSubmit: submit },
        h('h3', { className: 'dsh-mcp-servers__cardTitle' },
          draft.id ? t('editor.editTitle', { name: draft.serverName || draft.id }) : t('editor.addTitle')),
        h('div', { className: 'dsh-mcp-servers__fields' },
          h('div', { className: 'dsh-mcp-servers__row2' },
            h(TextField, {
              id: 'serverName',
              label: t('field.serverName'),
              value: draft.serverName,
              hint: t('field.serverName.hint'),
              error: errors.fields.serverName,
              placeholder: t('placeholder.name'),
              onChange: (value) => patch({ serverName: value }),
            }),
            h('div', { className: 'dsh-mcp-servers__field' },
              h('label', { className: 'dsh-mcp-servers__label', htmlFor: 'dsh-mcp-servers-transport' }, t('field.transport')),
              h('select', {
                id: 'dsh-mcp-servers-transport',
                className: 'dsh-mcp-servers__select',
                value: draft.transport,
                onChange: (event) => patch({ transport: event.target.value }),
              },
                h('option', { value: 'streamable-http' }, t('transport.streamable-http')),
                h('option', { value: 'stdio' }, t('transport.stdio')),
              ),
            ),
          ),
          stdio
            ? h('div', { className: 'dsh-mcp-servers__fields' },
                h(TextField, {
                  id: 'command',
                  label: t('field.command'),
                  value: draft.command,
                  hint: t('field.command.hint'),
                  error: errors.fields.command,
                  placeholder: t('placeholder.command'),
                  onChange: (value) => patch({ command: value }),
                }),
                h(TextAreaField, {
                  id: 'args',
                  label: t('field.args'),
                  value: draft.argsText,
                  hint: t('field.args.hint'),
                  placeholder: t('placeholder.args'),
                  onChange: (value) => patch({ argsText: value }),
                }),
                h(TextField, {
                  id: 'cwd',
                  label: t('field.cwd'),
                  value: draft.cwd,
                  hint: t('field.cwd.hint'),
                  placeholder: t('placeholder.cwd'),
                  onChange: (value) => patch({ cwd: value }),
                }),
                h(KeyValueEditor, {
                  id: 'env',
                  label: t('field.env'),
                  hint: t('field.env.hint'),
                  error: errors.keys.env,
                  addLabel: t('action.addVariable'),
                  removeLabel: t('action.clear'),
                  pairs: draft.env,
                  keyPlaceholder: t('placeholder.key'),
                  valuePlaceholder: t('placeholder.value'),
                  onChange: (pairs) => patch({ env: pairs }),
                }),
              )
            : h('div', { className: 'dsh-mcp-servers__fields' },
                h(TextField, {
                  id: 'url',
                  label: t('field.url'),
                  value: draft.url,
                  hint: t('field.url.hint'),
                  error: errors.fields.url,
                  placeholder: t('placeholder.url'),
                  onChange: (value) => patch({ url: value }),
                }),
                h(KeyValueEditor, {
                  id: 'headers',
                  label: t('field.headers'),
                  hint: t('field.headers.hint'),
                  error: errors.keys.headers,
                  addLabel: t('action.addHeader'),
                  removeLabel: t('action.clear'),
                  pairs: draft.headers,
                  keyPlaceholder: t('placeholder.key'),
                  valuePlaceholder: t('placeholder.value'),
                  onChange: (pairs) => patch({ headers: pairs }),
                }),
              ),
          h(TextField, {
            id: 'timeout',
            label: t('field.timeout'),
            value: draft.toolCallTimeoutMs,
            hint: t('field.timeout.hint', { ms: DEFAULT_TIMEOUT_MS }),
            error: errors.fields.toolCallTimeoutMs,
            onChange: (value) => patch({ toolCallTimeoutMs: value }),
          }),
          h(TextField, {
            id: 'maxInstructionBytes',
            label: t('field.instructions'),
            value: draft.maxInstructionBytes,
            hint: t('field.instructions.hint', { bytes: DEFAULT_MAX_INSTRUCTION_BYTES }),
            error: errors.fields.maxInstructionBytes,
            onChange: (value) => patch({ maxInstructionBytes: value }),
          }),
          h(CheckField, {
            id: 'failOnStartupError',
            label: t('field.failOnStartup'),
            checked: draft.failOnStartupError,
            onChange: (checked) => patch({ failOnStartupError: checked }),
          }),
          h(CheckField, {
            id: 'enabled',
            label: t('field.enabled'),
            hint: t('field.enabled.hint'),
            checked: draft.enabled,
            onChange: (checked) => patch({ enabled: checked }),
          }),
        ),
        props.error ? h('div', { className: 'dsh-mcp-servers__fieldError' }, props.error) : null,
        h('div', { className: 'dsh-mcp-servers__actions' },
          h('button', {
            type: 'button',
            className: 'dsh-mcp-servers__ghost dsh-mcp-servers__testButton',
            onClick: runTest,
            disabled: probe !== null && probe.status === 'running',
          }, probe !== null && probe.status === 'running' ? t('action.testing') : t('action.test')),
          h('button', {
            type: 'button',
            className: 'dsh-mcp-servers__ghost',
            onClick: () => props.onCancel(),
          }, t('action.cancel')),
          h('button', { type: 'submit', className: 'dsh-mcp-servers__primary', disabled: props.busy === true },
            props.busy ? t('action.saving') : t('action.save')),
        ),
        probe === null
          ? null
          : h('div', { className: 'dsh-mcp-servers__testResult', 'data-tone': probe.status === 'running' ? 'info' : probeTone },
              h('span', { className: 'dsh-mcp-servers__testText' },
                probe.status === 'running' ? t('action.testing') : probeText),
              probeServer ? h('span', { className: 'dsh-mcp-servers__hint' }, probeServer) : null,
              probeResult && Array.isArray(probeResult.tools) && probeResult.tools.length > 0
                ? h('span', { className: 'dsh-mcp-servers__hint' }, probeResult.tools.join(', '))
                : null,
            ),
      )
    }

    /**
     * The Settings page.
     * @param {{ t?: Function, form?: any, live?: any }} props - slot props.
     * @returns {any} element.
     */
    function McpServersSection(props) {
      const t = typeof props.t === 'function' ? props.t : (key, params) => (params ? `${key} ${JSON.stringify(params)}` : key)
      // The settings transport may activate after this plugin, so the form is
      // resolved on demand instead of being captured at apply() time.
      const form = props.form ?? (typeof props.resolveForm === 'function' ? props.resolveForm() : undefined)
      const live = props.live
      const formState = useStore(form)
      const liveState = useStore(live)
      const [draft, setDraft] = React.useState(null)
      const [busy, setBusy] = React.useState(false)
      const [writeError, setWriteError] = React.useState(null)
      const [confirming, setConfirming] = React.useState(null)
      const [attempt, setAttempt] = React.useState(0)

      // No form means the settings transport never answered: that is the same
      // condition as an inactive row, not a pending read.
      const status = form ? formState?.status ?? 'loading' : 'unavailable'
      const rows = status === 'ready' && formState?.value && Array.isArray(formState.value.servers) ? formState.value.servers : []
      const data = liveState?.data
      const liveById = {}
      const liveByName = {}
      for (const entry of Array.isArray(data?.servers) ? data.servers : []) {
        if (entry && typeof entry.id === 'string') liveById[entry.id] = entry
        if (entry && typeof entry.serverName === 'string') liveByName[entry.serverName] = entry
      }
      const problemById = {}
      const problemByName = {}
      for (const problem of Array.isArray(data?.problems) ? data.problems : []) {
        if (problem && typeof problem.id === 'string') problemById[problem.id] = problem
        if (problem && typeof problem.serverName === 'string' && problem.serverName) problemByName[problem.serverName] = problem
      }
      const external = Array.isArray(data?.external) ? data.external : []

      /**
       * Persist the whole list in one atomic write.
       * @param {any[]} nextRows - next configured list.
       * @returns {Promise<boolean>} whether the Host accepted the write.
       */
      const write = async (nextRows) => {
        if (!form) {
          setWriteError(t('error.unavailableWrite'))
          return false
        }
        setBusy(true)
        setWriteError(null)
        try {
          const accepted = await form.mutate([{ op: 'set', path: ['servers'], value: nextRows }])
          if (!accepted) {
            setWriteError(t('error.saveFailed'))
            return false
          }
          if (live && typeof live.refresh === 'function') void live.refresh(false)
          return true
        } catch (error) {
          setWriteError(messageOf(error))
          return false
        } finally {
          setBusy(false)
        }
      }

      const saveDraft = async (value) => {
        const takenIds = rows.filter((row) => row.id !== value.id).map((row) => row.id).filter((id) => typeof id === 'string')
        const row = rowOfDraft(value, takenIds)
        const next = value.id
          ? rows.map((entry) => (entry.id === value.id ? row : entry))
          : [...rows, row]
        if (await write(next)) setDraft(null)
      }

      const toggleRow = async (row) => {
        const next = rows.map((entry) => (entry.id === row.id ? { ...entry, enabled: entry.enabled === false } : entry))
        await write(next)
      }

      const removeRow = async (row) => {
        setConfirming(null)
        const next = rows.filter((entry) => entry.id !== row.id)
        if (!(await write(next))) setWriteError(t('error.removeFailed'))
      }

      const canWrite = Boolean(form) && status === 'ready' && formState?.writable !== false

      return h('div', { className: 'dsh-mcp-servers' },
        h('style', { key: 'css' }, CSS),
        h('div', { className: 'dsh-mcp-servers__head', key: 'head' },
          h('div', { className: 'dsh-mcp-servers__heading' },
            h('h2', { className: 'dsh-mcp-servers__title' }, t('title')),
            h('p', { className: 'dsh-mcp-servers__desc' }, t('description')),
          ),
          h('button', {
            type: 'button',
            className: 'dsh-mcp-servers__primary',
            disabled: !canWrite || draft !== null,
            onClick: () => {
              setWriteError(null)
              setDraft(emptyDraft())
            },
          }, t('add')),
        ),
        status === 'loading'
          ? h('div', { className: 'dsh-mcp-servers__notice', key: 'loading' }, t('loading'))
          : null,
        status === 'unavailable'
          ? h('div', { className: 'dsh-mcp-servers__notice dsh-mcp-servers__notice--warn', key: `unavailable-${attempt}` },
              h('span', { className: 'dsh-mcp-servers__noticeTitle' }, t('unavailable.title')),
              h('span', null, t('unavailable.body')),
              h('span', null,
                h('button', {
                  type: 'button',
                  className: 'dsh-mcp-servers__ghost',
                  onClick: () => setAttempt(attempt + 1),
                }, t('unavailable.retry')),
              ),
            )
          : null,
        data?.bridge && data.bridge.available === false
          ? h('div', { className: 'dsh-mcp-servers__notice dsh-mcp-servers__notice--warn', key: 'bridge' },
              h('span', { className: 'dsh-mcp-servers__noticeTitle' }, t('bridge.title')),
              h('span', null, t('bridge.body')),
              data.bridge.error ? h('span', { className: 'dsh-mcp-servers__error' }, data.bridge.error) : null,
              h('span', null,
                h('button', {
                  type: 'button',
                  className: 'dsh-mcp-servers__ghost',
                  onClick: () => {
                    if (live && typeof live.refresh === 'function') void live.refresh(true)
                  },
                }, t('action.retry')),
              ),
            )
          : null,
        liveState?.error
          ? h('div', { className: 'dsh-mcp-servers__notice dsh-mcp-servers__notice--error', key: 'liveError' }, liveState.error)
          : null,
        writeError
          ? h('div', { className: 'dsh-mcp-servers__notice dsh-mcp-servers__notice--error', key: 'writeError' }, writeError)
          : null,
        rows.length === 0 && status === 'ready' && draft === null
          ? h('div', { className: 'dsh-mcp-servers__empty', key: 'empty' },
              h('span', { className: 'dsh-mcp-servers__emptyTitle' }, t('empty.title')),
              h('span', null, t('empty.body')),
            )
          : null,
        rows.length > 0
          ? h('ul', { className: 'dsh-mcp-servers__list', key: 'list' },
              rows.map((row, index) =>
                h(ServerRow, {
                  key: typeof row.id === 'string' ? row.id : `row-${index}`,
                  row,
                  live: liveById[row.id] ?? liveByName[row.serverName],
                  problem: problemById[row.id] ?? problemByName[row.serverName],
                  t,
                  onEdit: (target) => {
                    setWriteError(null)
                    setDraft(draftOf(target))
                  },
                  onToggle: (target) => {
                    void toggleRow(target)
                  },
                  onRemove: (target) => setConfirming(target),
                }),
              ),
            )
          : null,
        confirming
          ? h('div', { className: 'dsh-mcp-servers__notice dsh-mcp-servers__notice--warn', key: 'confirm' },
              h('span', { className: 'dsh-mcp-servers__noticeTitle' }, t('confirm.remove', { name: confirming.serverName })),
              h('span', null, t('confirm.removeBody')),
              h('span', { className: 'dsh-mcp-servers__actions' },
                h('button', {
                  type: 'button',
                  className: 'dsh-mcp-servers__ghost',
                  onClick: () => setConfirming(null),
                }, t('confirm.keep')),
                h('button', {
                  type: 'button',
                  className: 'dsh-mcp-servers__ghost dsh-mcp-servers__ghost--danger',
                  onClick: () => {
                    void removeRow(confirming)
                  },
                }, t('action.remove')),
              ),
            )
          : null,
        draft !== null
          ? h(ServerEditor, {
              key: 'editor',
              draft,
              rows,
              t,
              busy,
              error: writeError,
              onChange: setDraft,
              onSave: (value) => {
                void saveDraft(value)
              },
              onCancel: () => {
                setDraft(null)
                setWriteError(null)
              },
            })
          : null,
        external.length > 0
          ? h('div', { className: 'dsh-mcp-servers__external', key: 'external' },
              h('h3', { className: 'dsh-mcp-servers__sectionTitle' }, t('external.title')),
              h('p', { className: 'dsh-mcp-servers__hint' }, t('external.body')),
              h('ul', { className: 'dsh-mcp-servers__list' },
                external.map((entry, index) =>
                  h('li', { className: 'dsh-mcp-servers__row', key: `${entry.id ?? index}` },
                    h('div', { className: 'dsh-mcp-servers__rowMain' },
                      h('div', { className: 'dsh-mcp-servers__rowTitle' },
                        h('span', {
                          className: 'dsh-mcp-servers__dot',
                          'data-tone': entry.disabled ? 'idle' : entry.fiberState === 'active' ? 'ok' : 'warn',
                          'aria-hidden': true,
                        }),
                        h('span', { className: 'dsh-mcp-servers__name' }, entry.serverName),
                        h('span', { className: 'dsh-mcp-servers__chip' },
                          t(`transport.${entry.transport === 'stdio' ? 'stdio' : 'streamable-http'}`)),
                        entry.disabled ? h('span', { className: 'dsh-mcp-servers__chip' }, t('external.disabled')) : null,
                      ),
                      entry.endpoint || entry.command
                        ? h('div', { className: 'dsh-mcp-servers__endpoint' }, entry.endpoint || entry.command)
                        : null,
                      typeof entry.toolCount === 'number'
                        ? h('div', { className: 'dsh-mcp-servers__meta' },
                            entry.toolCount === 1 ? t('tools.one') : t('tools.many', { count: entry.toolCount }))
                        : null,
                    ),
                  ),
                ),
              ),
            )
          : null,
        h('p', { className: 'dsh-mcp-servers__footer', key: 'footer' }, t('note.footer')),
      )
    }

    /**
     * Navigation glyph for the Settings sidebar.
     * @param {{ size?: number, active?: boolean }} props - icon props.
     * @returns {any} element.
     */
    function McpServersIcon(props) {
      const size = typeof props.size === 'number' ? props.size : 16
      return h('svg', { viewBox: '0 0 16 16', width: size, height: size, 'aria-hidden': true, focusable: false },
        h('rect', {
          x: 1.6, y: 2.4, width: 9.2, height: 4.2, rx: 1.2,
          fill: 'none', stroke: 'currentColor', strokeWidth: 1.2,
        }),
        h('rect', {
          x: 1.6, y: 9.4, width: 9.2, height: 4.2, rx: 1.2,
          fill: 'none', stroke: 'currentColor', strokeWidth: 1.2,
        }),
        h('circle', { cx: 3.9, cy: 4.5, r: 0.85, fill: 'currentColor' }),
        h('circle', { cx: 3.9, cy: 11.5, r: 0.85, fill: 'currentColor' }),
        h('path', {
          d: 'M12.2 4.5 H13.4 M12.2 11.5 H13.4 M13.4 4.5 V11.5',
          fill: 'none', stroke: 'currentColor', strokeWidth: 1.2, strokeLinecap: 'round',
        }),
      )
    }

    return {
      inject: ['slots', 'locale'],
      apply(ctx) {
        ctx.effect(() => ctx.locale.register(NS, { en: EN, zh: ZH }), 'mcp-servers: dictionaries')
        const bound = typeof ctx.locale?.bind === 'function' ? ctx.locale.bind(NS) : (key) => key
        let form
        /**
         * Resolve the settings form for this plugin's namespace. `configForms`
         * is provided by the settings UI, which may activate after this module,
         * so this stays lazy and cached once it answers.
         * @returns {any} the form controller, or undefined when unavailable.
         */
        const resolveForm = () => {
          if (form) return form
          const configForms = typeof ctx.get === 'function' ? ctx.get('configForms') : undefined
          if (configForms && typeof configForms.get === 'function') form = configForms.get(NAMESPACE)
          return form
        }
        const live = createLiveStore()
        ctx.slots.inject('settings.section', () =>
          ctx.slots.register(
            {
              name: 'settings.section',
              id: NAMESPACE,
              order: 12,
              label: () => bound('nav'),
              locale: NS,
              inject: () => ({ form: resolveForm(), live, resolveForm }),
            },
            McpServersSection,
          ),
        )
        ctx.slots.inject('settings.section.icon', () =>
          ctx.slots.register(
            {
              name: 'settings.section.icon',
              id: NAMESPACE,
              locale: NS,
            },
            McpServersIcon,
          ),
        )
      },
    }
  },
}

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
const statusHalf = {
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
        connection.note ? h('div', { className: 'mcp-status-dock__muted' }, connection.note) : null,
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
}

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
