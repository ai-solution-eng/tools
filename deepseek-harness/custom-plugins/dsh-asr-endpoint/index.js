/**
 * Remote ASR endpoint — Host half.
 *
 * Registers one `ctx.speechToText` provider that sends a finished recording to
 * an HTTP speech-recognition endpoint. Everything about that request — URL,
 * wire protocol, credentials, field names, response shape, deadline — comes
 * from the Loader row config, so the same package points at an
 * OpenAI-compatible server, a whisper.cpp server, a JSON gateway, or a raw
 * WAV endpoint without a code change.
 *
 * The provider has no `preparation`: a remote endpoint needs no download or
 * model load, and the registry synthesizes `preparation: { phase: 'ready' }`
 * for providers that omit it, which is exactly what makes the microphone
 * usable immediately.
 *
 * Nothing here imports a `@deepseek-ai/*` package. A `link:`-ed third-party
 * package resolves only its own `node_modules`, where no DSH tree exists, so
 * the module uses Node builtins and globals (`fetch`, `FormData`, `Blob`,
 * `AbortSignal`) and applies its own config defaults — hence no `Config`
 * export either.
 *
 * Credentials never leave the Host: the key is read from the configured
 * environment variable at request time, and every error message and log line
 * is scrubbed of the key, the environment value, and the configured headers.
 *
 * @module
 */

/* ------------------------------------------------------------------ *
 * Accepted settings
 * ------------------------------------------------------------------ */

/** Wire protocols this provider knows how to speak. */
const PROTOCOLS = ['openai', 'whisper-cpp', 'json', 'raw']

/** Longest delay `setTimeout`/`AbortSignal.timeout` accept. */
const MAX_TIMER_DELAY_MS = 2147483647

/** Longest response body excerpt quoted back in an error message. */
const BODY_SNIPPET_LIMIT = 512

/** Replacement used for every credential in an error message. */
const REDACTED = '[redacted]'

/**
 * Response bodies larger than this are rejected instead of buffered. A
 * transcript is tiny; the cap only protects the Host from an endpoint that
 * streams a file, so it is a constant rather than a tuning knob.
 */
const MAX_RESPONSE_BYTES = 1024 * 1024

/** Smallest canonical WAV file: 44-byte header plus at least one 16-bit sample. */
const WAV_MIN_BYTES = 46

/** Language hints accepted when the row configures none. */
const DEFAULT_LANGUAGES = ['auto', 'en', 'zh', 'de', 'fr', 'es', 'it', 'pt', 'ja', 'ko']

/** Accepted settings and their defaults. `endpoint` has no default and is required. */
const DEFAULT_SETTINGS = {
  /** Wire protocol: `openai`, `whisper-cpp`, `json`, or `raw`. */
  protocol: 'openai',
  /** Sent as the model field; empty omits it. */
  model: '',
  /** Literal key; discouraged in favour of `apiKeyEnv`. */
  apiKey: '',
  /** Environment variable read at request time; wins over `apiKey`. */
  apiKeyEnv: '',
  /** Request header carrying the key. */
  authHeader: 'Authorization',
  /** Prefix placed before the key; the trailing space is deliberate. */
  authScheme: 'Bearer ',
  /** Registration id; must equal the service row's `defaultProvider`. */
  providerId: 'asr-endpoint',
  /** Per-request deadline in milliseconds. */
  requestTimeoutMs: 120000,
  /** Recordings larger than this are rejected before any request is made. */
  maxAudioBytes: 16777216,
  /** Dot path into a JSON response; an empty path means the whole body. */
  responsePath: 'text',
  /** Multipart field name carrying the audio. */
  audioField: 'file',
  /** Field name carrying the language hint. */
  languageField: 'language',
  /** Field name carrying the model. */
  modelField: 'model',
  /** Multipart `response_format` value; empty omits it. */
  responseFormat: 'json',
  /** Send `language` even when it is `auto`. */
  sendLanguageWhenAuto: false,
}

/** Numeric bounds applied to configured settings. */
const BOUNDS = {
  requestTimeoutMs: [1, MAX_TIMER_DELAY_MS],
  maxAudioBytes: [WAV_MIN_BYTES, Number.MAX_SAFE_INTEGER],
}

/* ------------------------------------------------------------------ *
 * Errors
 * ------------------------------------------------------------------ */

/**
 * Build an Error carrying a stable machine-readable code.
 * @param {string} code - `ASR_*` code the UI can branch on.
 * @param {string} message - human-readable, credential-free message.
 * @param {unknown} [cause] - underlying failure, kept off the message.
 * @returns {Error & { code: string }} the error.
 */
function asrError(code, message, cause) {
  const error = new Error(message, cause === undefined ? undefined : { cause })
  error.code = code
  return error
}

/* ------------------------------------------------------------------ *
 * Settings resolution
 * ------------------------------------------------------------------ */

/**
 * Coerce a configured value to a trimmed non-empty string.
 * @param {unknown} value - raw config value.
 * @param {string} fallback - value used when absent or blank.
 * @returns {string} the string.
 */
function stringOf(value, fallback) {
  return typeof value === 'string' && value.trim() ? value.trim() : fallback
}

/**
 * Clamp a configured number into its accepted range.
 * @param {unknown} value - raw config value.
 * @param {number} fallback - value used when absent or not finite.
 * @param {[number, number]} bounds - inclusive range.
 * @returns {number} the integer setting.
 */
function integerOf(value, fallback, bounds) {
  if (typeof value !== 'number' || !Number.isFinite(value)) return fallback
  return Math.min(bounds[1], Math.max(bounds[0], Math.floor(value)))
}

/** Header names must be HTTP tokens; anything else would inject a header line. */
const HEADER_NAME = /^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$/

/**
 * Normalize the configured extra headers, rejecting values that cannot be sent.
 * @param {unknown} value - raw `headers` config value.
 * @returns {Record<string, string>} header map.
 * @throws when a name is not an HTTP token or a value is not a single-line string.
 */
function headersOf(value) {
  if (value === undefined || value === null) return {}
  if (typeof value !== 'object' || Array.isArray(value)) {
    throw asrError('ASR_INVALID_HEADERS', 'asr-endpoint: `headers` must be a mapping of header names to values')
  }
  const headers = {}
  for (const [name, raw] of Object.entries(value)) {
    if (!HEADER_NAME.test(name)) {
      throw asrError('ASR_INVALID_HEADERS', `asr-endpoint: ${JSON.stringify(name)} is not a valid HTTP header name`)
    }
    if (typeof raw !== 'string' && typeof raw !== 'number' && typeof raw !== 'boolean') {
      throw asrError('ASR_INVALID_HEADERS', `asr-endpoint: header ${JSON.stringify(name)} must be a string`)
    }
    const text = String(raw)
    if (/[\r\n]/.test(text)) {
      throw asrError('ASR_INVALID_HEADERS', `asr-endpoint: header ${JSON.stringify(name)} must not contain a line break`)
    }
    headers[name] = text
  }
  return headers
}

/**
 * Normalize the extra multipart / JSON fields.
 * @param {unknown} value - raw `extraFields` config value.
 * @returns {Record<string, unknown>} field map.
 * @throws when the value is not a mapping.
 */
function fieldsOf(value) {
  if (value === undefined || value === null) return {}
  if (typeof value !== 'object' || Array.isArray(value)) {
    throw asrError('ASR_INVALID_FIELDS', 'asr-endpoint: `extraFields` must be a mapping of field names to values')
  }
  return { ...value }
}

/**
 * Normalize the accepted language hints.
 * @param {unknown} value - raw `languages` config value.
 * @returns {string[]} language hints; `auto` is included by default.
 * @throws when the list is present but empty or not a list.
 */
function languagesOf(value) {
  if (value === undefined || value === null) return [...DEFAULT_LANGUAGES]
  if (!Array.isArray(value)) {
    throw asrError('ASR_INVALID_LANGUAGES', 'asr-endpoint: `languages` must be a list of language hints')
  }
  const languages = value
    .filter((item) => typeof item === 'string')
    .map((item) => item.trim())
    .filter(Boolean)
  if (languages.length === 0) {
    throw asrError('ASR_INVALID_LANGUAGES', 'asr-endpoint: `languages` must list at least one accepted language hint')
  }
  return languages
}

/**
 * Normalize a raw Loader config into accepted settings, validating the parts
 * that cannot be repaired. Called from `apply()`, so a bad row fails at
 * activation with a message naming the offending key.
 * @param {unknown} config - row config.
 * @returns {typeof DEFAULT_SETTINGS & { endpoint: string, host: string, headers: Record<string, string>, extraFields: Record<string, unknown>, languages: string[], displayName: string }} resolved settings.
 * @throws when `endpoint` is missing, relative, non-http(s), or carries credentials;
 *   when `protocol` is unknown; when `headers`/`extraFields`/`languages` are malformed.
 */
export function resolveSettings(config) {
  const raw = config && typeof config === 'object' ? config : {}

  if (typeof raw.endpoint !== 'string' || !raw.endpoint.trim()) {
    throw asrError('ASR_INVALID_ENDPOINT', 'asr-endpoint: `endpoint` is required and must be an absolute http(s) URL')
  }
  const endpoint = raw.endpoint.trim()
  let url
  try {
    url = new URL(endpoint)
  } catch {
    throw asrError('ASR_INVALID_ENDPOINT', `asr-endpoint: \`endpoint\` is not a valid absolute URL: ${JSON.stringify(endpoint)}`)
  }
  if (url.protocol !== 'http:' && url.protocol !== 'https:') {
    throw asrError('ASR_INVALID_ENDPOINT', `asr-endpoint: \`endpoint\` must use http or https, not ${url.protocol}`)
  }
  if (url.username || url.password) {
    throw asrError(
      'ASR_INVALID_ENDPOINT',
      'asr-endpoint: `endpoint` must not embed credentials (Node fetch rejects them anyway); set `apiKeyEnv` instead',
    )
  }

  const protocol = raw.protocol === undefined || raw.protocol === null
    ? DEFAULT_SETTINGS.protocol
    : typeof raw.protocol === 'string'
      ? raw.protocol.trim().toLowerCase()
      : ''
  if (!PROTOCOLS.includes(protocol)) {
    throw asrError(
      'ASR_INVALID_PROTOCOL',
      `asr-endpoint: unknown protocol ${JSON.stringify(raw.protocol)}; expected one of ${PROTOCOLS.join(', ')}`,
    )
  }

  const headers = headersOf(raw.headers)
  const extraFields = fieldsOf(raw.extraFields)
  const languages = languagesOf(raw.languages)
  const host = url.host

  return {
    endpoint,
    host,
    protocol,
    model: stringOf(raw.model, DEFAULT_SETTINGS.model),
    apiKey: stringOf(raw.apiKey, DEFAULT_SETTINGS.apiKey),
    apiKeyEnv: stringOf(raw.apiKeyEnv, DEFAULT_SETTINGS.apiKeyEnv),
    authHeader: stringOf(raw.authHeader, DEFAULT_SETTINGS.authHeader),
    // Not trimmed: the trailing space in `Bearer ` is part of the header value.
    authScheme: typeof raw.authScheme === 'string' ? raw.authScheme : DEFAULT_SETTINGS.authScheme,
    headers,
    providerId: stringOf(raw.providerId, DEFAULT_SETTINGS.providerId),
    displayName: stringOf(raw.displayName, `ASR endpoint (${host})`),
    languages,
    requestTimeoutMs: integerOf(raw.requestTimeoutMs, DEFAULT_SETTINGS.requestTimeoutMs, BOUNDS.requestTimeoutMs),
    maxAudioBytes: integerOf(raw.maxAudioBytes, DEFAULT_SETTINGS.maxAudioBytes, BOUNDS.maxAudioBytes),
    responsePath: typeof raw.responsePath === 'string' ? raw.responsePath.trim() : DEFAULT_SETTINGS.responsePath,
    audioField: stringOf(raw.audioField, DEFAULT_SETTINGS.audioField),
    languageField: stringOf(raw.languageField, DEFAULT_SETTINGS.languageField),
    modelField: stringOf(raw.modelField, DEFAULT_SETTINGS.modelField),
    responseFormat: typeof raw.responseFormat === 'string' ? raw.responseFormat.trim() : DEFAULT_SETTINGS.responseFormat,
    sendLanguageWhenAuto: raw.sendLanguageWhenAuto === true,
    extraFields,
  }
}

/* ------------------------------------------------------------------ *
 * WAV validation
 * ------------------------------------------------------------------ */

/**
 * Read a four-character chunk id as ASCII.
 * @param {Uint8Array} bytes - whole file.
 * @param {number} offset - chunk id offset.
 * @returns {string} the id.
 */
function chunkId(bytes, offset) {
  return String.fromCharCode(bytes[offset], bytes[offset + 1], bytes[offset + 2], bytes[offset + 3])
}

/**
 * Validate one canonical 16 kHz mono PCM16 WAV recording and measure it.
 *
 * The layout is the one the rest of the speech stack produces and requires:
 * `dsh-experimental-api-speech-to-text` already runs `validateWave` over the
 * browser's bytes before they reach a provider, and the local SenseVoice
 * provider re-validates them. Accepting anything looser here would admit audio
 * the rest of the stack rejects — a remote provider and a local provider must
 * agree on what a recording is. The check is one pass over 44 bytes, and it
 * yields `audioSeconds` from the data-chunk length rather than from elapsed
 * time.
 * @param {Uint8Array} audio - complete recording.
 * @returns {{ sampleRate: number, channels: number, bitsPerSample: number, frames: number, seconds: number, dataBytes: number }} recording facts.
 * @throws when the bytes are not a canonical 16 kHz mono PCM16 WAV file, are truncated, or carry no frames.
 */
export function parseWav(audio) {
  if (!(audio instanceof Uint8Array)) {
    throw asrError('ASR_INVALID_AUDIO', 'Speech input must carry the recording as a Uint8Array of WAV bytes')
  }
  if (audio.byteLength < WAV_MIN_BYTES) {
    throw asrError(
      'ASR_INVALID_AUDIO',
      `Audio is too short to be a canonical 16 kHz mono PCM16 WAV recording (${audio.byteLength} bytes)`,
    )
  }
  const view = new DataView(audio.buffer, audio.byteOffset, audio.byteLength)
  const expected = 'Audio must be a canonical 16 kHz mono PCM16 WAV recording'
  if (chunkId(audio, 0) !== 'RIFF') throw asrError('ASR_INVALID_AUDIO', `${expected}: missing RIFF header`)
  if (chunkId(audio, 8) !== 'WAVE') throw asrError('ASR_INVALID_AUDIO', `${expected}: form type is not WAVE`)
  if (chunkId(audio, 12) !== 'fmt ' || view.getUint32(16, true) !== 16) {
    throw asrError('ASR_INVALID_AUDIO', `${expected}: the first chunk is not a 16-byte "fmt " chunk`)
  }
  const audioFormat = view.getUint16(20, true)
  if (audioFormat !== 1) {
    throw asrError('ASR_INVALID_AUDIO', `${expected}: WAV format ${audioFormat} is not uncompressed PCM (1)`)
  }
  const channels = view.getUint16(22, true)
  if (channels !== 1) throw asrError('ASR_INVALID_AUDIO', `${expected}: it declares ${channels} channels, not mono`)
  const sampleRate = view.getUint32(24, true)
  if (sampleRate !== 16000) {
    throw asrError('ASR_INVALID_AUDIO', `${expected}: it declares a ${sampleRate} Hz sample rate, not 16000 Hz`)
  }
  const byteRate = view.getUint32(28, true)
  const blockAlign = view.getUint16(32, true)
  const bitsPerSample = view.getUint16(34, true)
  if (bitsPerSample !== 16) {
    throw asrError('ASR_INVALID_AUDIO', `${expected}: it declares ${bitsPerSample}-bit samples, not 16-bit PCM`)
  }
  if (blockAlign !== 2) {
    throw asrError('ASR_INVALID_AUDIO', `${expected}: it declares a block alignment of ${blockAlign}, not 2`)
  }
  if (byteRate !== 32000) {
    throw asrError('ASR_INVALID_AUDIO', `${expected}: it declares a ${byteRate} B/s byte rate, not 32000 B/s`)
  }
  if (chunkId(audio, 36) !== 'data') throw asrError('ASR_INVALID_AUDIO', `${expected}: the "data" chunk is missing`)
  if (view.getUint32(4, true) !== audio.byteLength - 8) {
    throw asrError(
      'ASR_INVALID_AUDIO',
      `${expected}: the RIFF size (${view.getUint32(4, true)}) does not match the file length (${audio.byteLength})`,
    )
  }
  const dataBytes = view.getUint32(40, true)
  if (dataBytes !== audio.byteLength - 44) {
    throw asrError(
      'ASR_INVALID_AUDIO',
      `${expected}: the "data" chunk declares ${dataBytes} bytes but the file holds ${audio.byteLength - 44}`,
    )
  }
  if (dataBytes % 2 !== 0) {
    throw asrError('ASR_INVALID_AUDIO', `${expected}: the "data" chunk holds an odd number of bytes`)
  }
  const frames = dataBytes / 2
  if (frames === 0) throw asrError('ASR_INVALID_AUDIO', 'Audio contains no frames')

  return { sampleRate, channels, bitsPerSample, frames, seconds: frames / sampleRate, dataBytes }
}

/* ------------------------------------------------------------------ *
 * Credentials
 * ------------------------------------------------------------------ */

/**
 * Resolve the request credential. The environment is read at request time, so
 * rotating the variable does not need a profile restart; a configured
 * `apiKeyEnv` wins over the literal `apiKey` when it resolves to a value.
 * @param {ReturnType<typeof resolveSettings>} settings - resolved settings.
 * @param {Record<string, string | undefined>} env - environment to read.
 * @returns {string} the credential, or an empty string when none is configured.
 */
export function resolveApiKey(settings, env) {
  if (settings.apiKeyEnv) {
    const value = env?.[settings.apiKeyEnv]
    if (typeof value === 'string' && value.trim()) return value.trim()
  }
  return settings.apiKey
}

/**
 * Every string that must never appear in a message: the literal key, the
 * environment value, the composed auth header, and the configured headers.
 * @param {ReturnType<typeof resolveSettings>} settings - resolved settings.
 * @param {Record<string, string | undefined>} env - environment to read.
 * @returns {string[]} secrets.
 */
function secretsOf(settings, env) {
  const secrets = []
  const key = resolveApiKey(settings, env)
  if (key) {
    secrets.push(key)
    secrets.push(`${settings.authScheme}${key}`)
  }
  if (settings.apiKeyEnv) {
    const value = env?.[settings.apiKeyEnv]
    if (typeof value === 'string' && value) secrets.push(value)
  }
  for (const value of Object.values(settings.headers)) secrets.push(value)
  return secrets
}

/**
 * Remove every known secret from a message and mask labelled credentials that
 * were not configured here (for example a server echoing its own token).
 * @param {string} text - message or response snippet.
 * @param {Iterable<string>} secrets - exact values to replace.
 * @returns {string} the scrubbed text.
 */
export function redactSecrets(text, secrets) {
  let result = typeof text === 'string' ? text : ''
  for (const secret of secrets) {
    if (typeof secret !== 'string' || secret.length < 4) continue
    result = result.split(secret).join(REDACTED)
  }
  // JSON bodies: {"api_key":"…"} / {"authorization":"…"}.
  result = result.replace(
    /("(?:api[_-]?key|authorization|token|secret|password)"\s*:\s*")([^"]{4,})"/gi,
    (_match, prefix) => `${prefix}${REDACTED}"`,
  )
  // Header-ish lines: `Authorization: Bearer …` / `api-key=…`.
  result = result.replace(
    /(^|\r?\n)(\s*(?:api[_-]?key|authorization|token|secret|password)\s*[:=]\s*)([^\r\n]{4,})/gi,
    (_match, lineStart, prefix) => `${lineStart}${prefix}${REDACTED}`,
  )
  return result
}

/* ------------------------------------------------------------------ *
 * Request building
 * ------------------------------------------------------------------ */

/**
 * Whether the language hint belongs in the request.
 * @param {ReturnType<typeof resolveSettings>} settings - resolved settings.
 * @param {string} language - language hint from the recording.
 * @returns {boolean} true when it should be sent.
 */
function sendsLanguage(settings, language) {
  if (!language) return false
  if (language === 'auto' && !settings.sendLanguageWhenAuto) return false
  return true
}

/**
 * Copy a header map without one header, compared case-insensitively.
 * @param {Record<string, string>} headers - source headers.
 * @param {string} name - header to drop.
 * @returns {Record<string, string>} filtered headers.
 */
function withoutHeader(headers, name) {
  const lower = name.toLowerCase()
  const copy = {}
  for (const [key, value] of Object.entries(headers)) {
    if (key.toLowerCase() !== lower) copy[key] = value
  }
  return copy
}

/**
 * Compose the request headers: configured headers, then the credential, then
 * the protocol's own `content-type`.
 * @param {ReturnType<typeof resolveSettings>} settings - resolved settings.
 * @param {Record<string, string | undefined>} env - environment to read.
 * @param {string | undefined} contentType - content type the body requires.
 * @returns {Record<string, string>} headers to send.
 */
function requestHeaders(settings, env, contentType) {
  let headers = withoutHeader(settings.headers, 'content-type')
  const key = resolveApiKey(settings, env)
  if (key) {
    // Drop any case-variant of the auth header first: sending both would put
    // the configured header and the credential in the same request.
    headers = withoutHeader(headers, settings.authHeader)
    headers[settings.authHeader] = `${settings.authScheme}${key}`
  }
  if (contentType !== undefined) headers = { ...headers, 'content-type': contentType }
  return headers
}

/**
 * Flatten one configured extra field into multipart entries.
 * @param {string} name - field name.
 * @param {unknown} value - configured value.
 * @returns {Array<[string, string]>} entries to append; arrays append once per item.
 */
function multipartEntries(name, value) {
  if (value === undefined || value === null) return []
  if (Array.isArray(value)) return value.flatMap((item) => multipartEntries(name, item))
  if (typeof value === 'object') return [[name, JSON.stringify(value)]]
  return [[name, String(value)]]
}

/**
 * Build the HTTP request for one recording under the configured protocol.
 * @param {ReturnType<typeof resolveSettings>} settings - resolved settings.
 * @param {{ audio: Uint8Array, language: string }} input - recording and language hint.
 * @param {{ env?: Record<string, string | undefined> }} [runtime] - environment override for tests.
 * @returns {{ url: string, init: { method: string, headers: Record<string, string>, body: FormData | string | Uint8Array } }} request.
 */
export function buildRequest(settings, input, runtime = {}) {
  const env = runtime.env ?? process.env
  const { audio, language } = input
  const extra = Object.entries(settings.extraFields)

  if (settings.protocol === 'openai' || settings.protocol === 'whisper-cpp') {
    // Multipart: the browser-provided boundary is authoritative, so never set
    // `content-type` here.
    const form = new FormData()
    form.append(settings.audioField, new Blob([audio], { type: 'audio/wav' }), 'audio.wav')
    if (settings.protocol === 'openai' && settings.model) form.append(settings.modelField, settings.model)
    if (settings.protocol === 'whisper-cpp') form.append('temperature', '0')
    if (settings.responseFormat) form.append('response_format', settings.responseFormat)
    if (sendsLanguage(settings, language)) form.append(settings.languageField, language)
    for (const [name, value] of extra) {
      for (const [key, text] of multipartEntries(name, value)) form.append(key, text)
    }
    return { url: settings.endpoint, init: { method: 'POST', headers: requestHeaders(settings, env, undefined), body: form } }
  }

  if (settings.protocol === 'json') {
    const payload = { audio: Buffer.from(audio.buffer, audio.byteOffset, audio.byteLength).toString('base64') }
    if (sendsLanguage(settings, language)) payload[settings.languageField] = language
    if (settings.model) payload[settings.modelField] = settings.model
    for (const [name, value] of extra) payload[name] = value
    return {
      url: settings.endpoint,
      init: { method: 'POST', headers: requestHeaders(settings, env, 'application/json'), body: JSON.stringify(payload) },
    }
  }

  return {
    url: settings.endpoint,
    init: { method: 'POST', headers: requestHeaders(settings, env, 'audio/wav'), body: audio },
  }
}

/* ------------------------------------------------------------------ *
 * Response parsing
 * ------------------------------------------------------------------ */

/**
 * Read a dot path out of a decoded response. Numeric segments index arrays,
 * so `choices.0.text` and `results.0.transcript` both work.
 * @param {unknown} value - decoded JSON value.
 * @param {string} path - dot path; empty selects the value itself.
 * @returns {unknown} the selected value, or undefined when the path is absent.
 */
export function extractPath(value, path) {
  if (typeof path !== 'string' || path === '') return value
  let current = value
  for (const segment of path.split('.')) {
    if (current === null || current === undefined) return undefined
    if (Array.isArray(current)) {
      if (!/^\d+$/.test(segment)) return undefined
      current = current[Number(segment)]
    } else if (typeof current === 'object') {
      if (!Object.hasOwn(current, segment)) return undefined
      current = current[segment]
    } else {
      return undefined
    }
  }
  return current
}

/**
 * Turn the selected response value into transcript text. A missing value and
 * an empty string both mean "no speech" to the service.
 * @param {ReturnType<typeof resolveSettings>} settings - resolved settings.
 * @param {unknown} value - decoded JSON value.
 * @returns {string} transcript text.
 * @throws when the configured path selects a structured value.
 */
function transcriptOf(settings, value) {
  const picked = extractPath(value, settings.responsePath)
  if (picked === undefined || picked === null) return ''
  if (typeof picked === 'string') return picked.trim()
  if (typeof picked === 'number' || typeof picked === 'boolean') return String(picked)
  throw asrError(
    'ASR_BAD_RESPONSE',
    `ASR endpoint ${settings.host}: responsePath ${JSON.stringify(settings.responsePath)} selected a ${
      Array.isArray(picked) ? 'array' : typeof picked
    }, not text`,
  )
}

/**
 * Parse a response body into transcript text. JSON is preferred; a plain-text
 * body is accepted for `raw` endpoints and lenient gateways, while an HTML
 * page or a body declared as JSON that does not parse is a clear failure.
 * @param {ReturnType<typeof resolveSettings>} settings - resolved settings.
 * @param {string} body - response body.
 * @param {string | null} contentType - response content type.
 * @returns {string} transcript text.
 * @throws when the body is unparseable JSON or an HTML page.
 */
export function transcriptText(settings, body, contentType) {
  const text = typeof body === 'string' ? body : ''
  const trimmed = text.trim()
  // An empty body is "no speech", not a malformed answer.
  if (trimmed === '') return ''
  if (trimmed.startsWith('{') || trimmed.startsWith('[')) {
    let parsed
    try {
      parsed = JSON.parse(trimmed)
    } catch {
      throw asrError(
        'ASR_BAD_RESPONSE',
        `ASR endpoint ${settings.host} returned a body that starts like JSON but does not parse`,
      )
    }
    return transcriptOf(settings, parsed)
  }
  const type = (contentType ?? '').toLowerCase()
  if (type.includes('json')) {
    throw asrError('ASR_BAD_RESPONSE', `ASR endpoint ${settings.host} declared a JSON response that does not parse`)
  }
  if (type.includes('html') || trimmed.startsWith('<')) {
    throw asrError(
      'ASR_BAD_RESPONSE',
      `ASR endpoint ${settings.host} returned an HTML page instead of a transcript; check that the endpoint URL is the transcription API`,
    )
  }
  return trimmed
}

/* ------------------------------------------------------------------ *
 * Transcription
 * ------------------------------------------------------------------ */

/**
 * Read a response body as UTF-8 under a byte cap. Reading through the stream
 * rather than `response.text()` means an endpoint that answers with a large
 * file cannot balloon the Host's memory before the cap is noticed.
 * @param {Response} response - fetch response.
 * @param {ReturnType<typeof resolveSettings>} settings - resolved settings.
 * @returns {Promise<string>} decoded body.
 * @throws {Error & { code: string }} `ASR_BAD_RESPONSE` when the body exceeds the cap.
 */
async function readCappedBody(response, settings) {
  if (!response.body) return ''
  const reader = response.body.getReader()
  const chunks = []
  let length = 0
  try {
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      length += value.byteLength
      if (length > MAX_RESPONSE_BYTES) {
        throw asrError(
          'ASR_BAD_RESPONSE',
          `ASR endpoint ${settings.host} returned more than ${MAX_RESPONSE_BYTES} bytes`,
        )
      }
      chunks.push(value)
    }
  } finally {
    await reader.cancel().catch(() => {})
    reader.releaseLock()
  }
  return Buffer.concat(chunks).toString('utf8')
}

/**
 * Map a failed request onto the distinct failure the UI should report: caller
 * cancellation first (its own reason is re-thrown), then the deadline, then
 * an unreachable endpoint.
 * @param {ReturnType<typeof resolveSettings>} settings - resolved settings.
 * @param {unknown} error - thrown value.
 * @param {AbortSignal | undefined} callerSignal - caller cancellation.
 * @param {AbortSignal} timeoutSignal - deadline signal.
 * @param {Record<string, string | undefined>} env - environment to read.
 * @returns {Error} the error to throw.
 */
function requestFailure(settings, error, callerSignal, timeoutSignal, env) {
  const secrets = secretsOf(settings, env)
  if (callerSignal?.aborted) {
    const reason = callerSignal.reason
    if (reason instanceof Error) return reason
    return asrError('ASR_CANCELLED', redactSecrets('Transcription cancelled', secrets))
  }
  if (timeoutSignal.aborted) {
    return asrError(
      'ASR_TIMEOUT',
      redactSecrets(`ASR endpoint ${settings.host} did not answer within ${settings.requestTimeoutMs} ms`, secrets),
    )
  }
  const cause = error && typeof error === 'object' ? error.cause : undefined
  const detail = cause && typeof cause === 'object'
    ? [cause.code, cause.message].filter((part) => typeof part === 'string' && part).join(' ')
    : ''
  const message = error instanceof Error ? error.message : String(error)
  return asrError(
    'ASR_UNREACHABLE',
    redactSecrets(`ASR request to ${settings.host} failed: ${message}${detail ? ` (${detail})` : ''}`, secrets),
    error,
  )
}

/**
 * Recognize one complete recording through the configured endpoint.
 * @param {ReturnType<typeof resolveSettings>} settings - resolved settings.
 * @param {{ audio: Uint8Array, language: string }} input - WAV bytes and language hint.
 * @param {AbortSignal} [signal] - caller cancellation.
 * @param {{ env?: Record<string, string | undefined> }} [runtime] - environment override for tests.
 * @returns {Promise<{ text: string, audioSeconds: number, inferenceSeconds: number }>} transcript and timings.
 * @throws {Error & { code: string }} `ASR_INVALID_AUDIO` for a bad recording, `ASR_AUDIO_TOO_LARGE`
 *   over the limit, `ASR_TIMEOUT` past the deadline, `ASR_HTTP_ERROR` for a non-2xx answer,
 *   `ASR_BAD_RESPONSE` for an unusable body, `ASR_UNREACHABLE` for a transport failure; caller
 *   cancellation re-throws the caller's own reason unchanged.
 */
export async function transcribe(settings, input, signal, runtime = {}) {
  const env = runtime.env ?? process.env
  signal?.throwIfAborted?.()
  const audio = input?.audio
  if (!(audio instanceof Uint8Array)) {
    throw asrError('ASR_INVALID_AUDIO', 'Speech input must carry the recording as a Uint8Array of WAV bytes')
  }
  if (audio.byteLength > settings.maxAudioBytes) {
    throw asrError(
      'ASR_AUDIO_TOO_LARGE',
      `Recording is ${audio.byteLength} bytes; the configured maxAudioBytes limit is ${settings.maxAudioBytes}`,
    )
  }
  const wav = parseWav(audio)
  const language = typeof input.language === 'string' ? input.language : ''
  const request = buildRequest(settings, { audio, language }, { env })

  const timeoutSignal = AbortSignal.timeout(settings.requestTimeoutMs)
  const combined = signal === undefined ? timeoutSignal : AbortSignal.any([signal, timeoutSignal])
  // The inference clock is monotonic and starts after validation, so it
  // measures the recognition round-trip only — matching what the local
  // provider reports.
  const startedAt = performance.now()
  let response
  let body
  try {
    response = await fetch(request.url, { ...request.init, signal: combined })
    body = await readCappedBody(response, settings)
  } catch (error) {
    // A body-cap failure is already a precise diagnosis; do not relabel it.
    if (error && typeof error === 'object' && typeof error.code === 'string' && error.code.startsWith('ASR_')) {
      throw error
    }
    throw requestFailure(settings, error, signal, timeoutSignal, env)
  }
  const inferenceSeconds = (performance.now() - startedAt) / 1000

  // Cancellation wins over any result, including a late one.
  signal?.throwIfAborted?.()

  if (!response.ok) {
    const snippet = redactSecrets(String(body ?? '').slice(0, BODY_SNIPPET_LIMIT), secretsOf(settings, env))
    throw asrError(
      'ASR_HTTP_ERROR',
      `ASR endpoint ${settings.host} responded ${response.status} ${response.statusText}${snippet ? `: ${snippet}` : ''}`,
    )
  }

  return {
    text: transcriptText(settings, body, response.headers.get('content-type')),
    audioSeconds: wav.seconds,
    inferenceSeconds,
  }
}

/* ------------------------------------------------------------------ *
 * Registration
 * ------------------------------------------------------------------ */

/** Loader row name, for diagnostics. */
export const name = 'asr-endpoint'

/** The provider registry must exist before the provider can be registered. */
export const inject = ['speechToText']

/**
 * Build the `SpeechProvider` object handed to the registry.
 *
 * `preparation` is deliberately absent: a remote endpoint needs no download or
 * model load, and the registry reports `{ phase: 'ready' }` for a provider that
 * omits it, so the microphone records immediately.
 * @param {ReturnType<typeof resolveSettings>} settings - resolved settings.
 * @param {{ env?: Record<string, string | undefined> }} [runtime] - environment override for tests.
 * @returns {{ info: { id: string, name: string, location: 'cloud', languages: string[] }, transcribe: (input: { audio: Uint8Array, language: string }, signal: AbortSignal) => Promise<{ text: string, audioSeconds: number, inferenceSeconds: number }> }} the provider.
 */
export function createProvider(settings, runtime = {}) {
  const env = runtime.env ?? process.env
  return {
    info: {
      id: settings.providerId,
      name: settings.displayName,
      location: 'cloud',
      languages: settings.languages,
    },
    transcribe: async (input, signal) => await transcribe(settings, input, signal, { env }),
  }
}

/**
 * Register the remote provider for the lifetime of this row.
 * @param {import('@deepseek-ai/cordis').Context} ctx - plugin context.
 * @param {unknown} config - row config.
 * @throws when the row config is unusable; the row then fails at activation
 *   instead of registering a provider that cannot answer.
 */
export function apply(ctx, config) {
  const settings = resolveSettings(config)
  const provider = createProvider(settings)
  ctx.effect(() => {
    const unregister = ctx.speechToText.register(provider)
    return async () => {
      await unregister()
    }
  }, 'asr-endpoint: remote speech provider')
  try {
    ctx.logger?.debug?.(`asr-endpoint: provider ${settings.providerId} → ${settings.host} (${settings.protocol})`)
    if (!settings.languages.includes('auto')) {
      ctx.logger?.warn?.(
        'asr-endpoint: `languages` does not include "auto"; the speech service row defaults to language auto and will fail to resolve unless it selects a supported language',
      )
    }
    if (settings.apiKeyEnv && !process.env[settings.apiKeyEnv]) {
      ctx.logger?.warn?.(
        `asr-endpoint: apiKeyEnv ${JSON.stringify(settings.apiKeyEnv)} is not set in the dsh process environment; requests will be sent without a credential`,
      )
    }
  } catch {
    // Diagnostics must never prevent registration.
  }
}
