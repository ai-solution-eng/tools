/**
 * Development check for the ASR endpoint plugin's Host half.
 *
 * Offline: no network and no running Harness. It covers settings resolution
 * and its validation errors, canonical WAV parsing, request building for all
 * four protocols, response parsing (dot paths, plain text, empty transcripts),
 * credential redaction, and the registration shape handed to the speech
 * registry. `test/mock-asr.mjs` exercises the actual HTTP round-trip.
 *
 * Not part of the installed bundle; run with `node test/host-check.mjs`.
 */
import assert from 'node:assert/strict'
import {
  apply,
  buildRequest,
  createProvider,
  extractPath,
  parseWav,
  redactSecrets,
  resolveApiKey,
  resolveSettings,
  transcribe,
  transcriptText,
} from '../index.js'

/* ------------------------------------------------------------------ *
 * Fixtures
 * ------------------------------------------------------------------ */

/**
 * Build a canonical 16 kHz mono PCM16 WAV recording.
 * @param {number} seconds - duration.
 * @param {{ sampleRate?: number, channels?: number, bitsPerSample?: number, format?: number }} [shape] - deviations used by the rejection checks.
 * @returns {Buffer} WAV bytes.
 */
function wavOf(seconds, shape = {}) {
  const sampleRate = shape.sampleRate ?? 16000
  const channels = shape.channels ?? 1
  const bitsPerSample = shape.bitsPerSample ?? 16
  const format = shape.format ?? 1
  const blockAlign = channels * (bitsPerSample / 8)
  const frames = Math.round(sampleRate * seconds)
  const dataBytes = frames * blockAlign
  const buffer = Buffer.alloc(44 + dataBytes)
  buffer.write('RIFF', 0, 'ascii')
  buffer.writeUInt32LE(36 + dataBytes, 4)
  buffer.write('WAVE', 8, 'ascii')
  buffer.write('fmt ', 12, 'ascii')
  buffer.writeUInt32LE(16, 16)
  buffer.writeUInt16LE(format, 20)
  buffer.writeUInt16LE(channels, 22)
  buffer.writeUInt32LE(sampleRate, 24)
  buffer.writeUInt32LE(sampleRate * blockAlign, 28)
  buffer.writeUInt16LE(blockAlign, 32)
  buffer.writeUInt16LE(bitsPerSample, 34)
  buffer.write('data', 36, 'ascii')
  buffer.writeUInt32LE(dataBytes, 40)
  for (let sample = 0; sample < dataBytes / 2; sample += 1) {
    buffer.writeInt16LE((sample % 64) - 32, 44 + sample * 2)
  }
  return buffer
}

const WAV = wavOf(1)
const BASE = { endpoint: 'http://127.0.0.1:9/v1/audio/transcriptions' }
const settingsOf = (overrides = {}) => resolveSettings({ ...BASE, ...overrides })

const results = []
function record(name, fn) {
  return Promise.resolve()
    .then(fn)
    .then(() => results.push(`ok   ${name}`))
    .catch((error) => {
      results.push(`FAIL ${name}: ${error.message}`)
      process.exitCode = 1
    })
}

/**
 * Assert a synchronous or asynchronous failure matches a pattern.
 * @param {() => unknown} fn - work that must reject.
 * @param {RegExp} pattern - expected message.
 * @param {string} [code] - expected `error.code`.
 * @returns {Promise<Error>} the error.
 */
async function rejectsWith(fn, pattern, code) {
  try {
    await fn()
  } catch (error) {
    assert.match(error.message, pattern)
    if (code !== undefined) assert.equal(error.code, code, `code was ${error.code}`)
    return error
  }
  throw new Error(`expected a rejection matching ${pattern}`)
}

/* ------------------------------------------------------------------ *
 * Settings resolution
 * ------------------------------------------------------------------ */

await record('settings: defaults for a minimal row', async () => {
  const settings = settingsOf()
  assert.equal(settings.endpoint, BASE.endpoint)
  assert.equal(settings.host, '127.0.0.1:9')
  assert.equal(settings.protocol, 'openai')
  assert.equal(settings.model, '')
  assert.equal(settings.apiKey, '')
  assert.equal(settings.apiKeyEnv, '')
  assert.equal(settings.authHeader, 'Authorization')
  assert.equal(settings.authScheme, 'Bearer ')
  assert.deepEqual(settings.headers, {})
  assert.equal(settings.providerId, 'asr-endpoint')
  assert.equal(settings.displayName, 'ASR endpoint (127.0.0.1:9)')
  assert.deepEqual(settings.languages, ['auto', 'en', 'zh', 'de', 'fr', 'es', 'it', 'pt', 'ja', 'ko'])
  assert.equal(settings.requestTimeoutMs, 120000)
  assert.equal(settings.maxAudioBytes, 16777216)
  assert.equal(settings.responsePath, 'text')
  assert.equal(settings.audioField, 'file')
  assert.equal(settings.languageField, 'language')
  assert.equal(settings.modelField, 'model')
  assert.equal(settings.responseFormat, 'json')
  assert.equal(settings.sendLanguageWhenAuto, false)
  assert.deepEqual(settings.extraFields, {})
})

await record('settings: every override is honoured', async () => {
  const settings = settingsOf({
    endpoint: 'https://asr.example.com:8443/custom/transcribe',
    protocol: 'whisper-cpp',
    model: 'large-v3',
    apiKey: 'literal-key',
    apiKeyEnv: 'ASR_KEY',
    authHeader: 'X-Api-Key',
    authScheme: 'Token ',
    headers: { 'X-Tenant': 'acme' },
    providerId: 'asr-remote',
    displayName: 'Internal ASR',
    languages: ['auto', 'en'],
    requestTimeoutMs: 5000,
    maxAudioBytes: 100000,
    responsePath: 'choices.0.text',
    audioField: 'audio',
    languageField: 'lang',
    modelField: 'model_name',
    responseFormat: 'verbose_json',
    sendLanguageWhenAuto: true,
    extraFields: { temperature: 0, beam_size: 3 },
  })
  assert.equal(settings.protocol, 'whisper-cpp')
  assert.equal(settings.host, 'asr.example.com:8443')
  assert.equal(settings.model, 'large-v3')
  assert.equal(settings.authHeader, 'X-Api-Key')
  assert.equal(settings.authScheme, 'Token ')
  assert.deepEqual(settings.headers, { 'X-Tenant': 'acme' })
  assert.equal(settings.providerId, 'asr-remote')
  assert.equal(settings.displayName, 'Internal ASR')
  assert.deepEqual(settings.languages, ['auto', 'en'])
  assert.equal(settings.requestTimeoutMs, 5000)
  assert.equal(settings.maxAudioBytes, 100000)
  assert.equal(settings.responsePath, 'choices.0.text')
  assert.equal(settings.audioField, 'audio')
  assert.equal(settings.languageField, 'lang')
  assert.equal(settings.modelField, 'model_name')
  assert.equal(settings.responseFormat, 'verbose_json')
  assert.equal(settings.sendLanguageWhenAuto, true)
  assert.deepEqual(settings.extraFields, { temperature: 0, beam_size: 3 })
})

await record('settings: protocol is case-insensitive and trimmed', async () => {
  assert.equal(settingsOf({ protocol: '  OpenAI ' }).protocol, 'openai')
  assert.equal(settingsOf({ protocol: 'RAW' }).protocol, 'raw')
})

await record('settings: endpoint must be present, absolute and http(s)', async () => {
  await rejectsWith(() => resolveSettings({}), /endpoint.*required/, 'ASR_INVALID_ENDPOINT')
  await rejectsWith(() => resolveSettings({ endpoint: 42 }), /endpoint.*required/, 'ASR_INVALID_ENDPOINT')
  await rejectsWith(() => resolveSettings({ endpoint: '   ' }), /endpoint.*required/, 'ASR_INVALID_ENDPOINT')
  await rejectsWith(() => resolveSettings({ endpoint: 'asr.internal:8000/asr' }), /must use http or https/, 'ASR_INVALID_ENDPOINT')
  await rejectsWith(() => resolveSettings({ endpoint: '/v1/transcriptions' }), /not a valid absolute URL/, 'ASR_INVALID_ENDPOINT')
  await rejectsWith(() => resolveSettings({ endpoint: 'ftp://asr.internal/x' }), /must use http or https/, 'ASR_INVALID_ENDPOINT')
  await rejectsWith(
    () => resolveSettings({ endpoint: 'http://user:pass@asr.internal/x' }),
    /must not embed credentials/,
    'ASR_INVALID_ENDPOINT',
  )
})

await record('settings: unknown protocol is rejected by name', async () => {
  await rejectsWith(() => resolveSettings({ ...BASE, protocol: 'grpc' }), /unknown protocol "grpc"/, 'ASR_INVALID_PROTOCOL')
  await rejectsWith(() => resolveSettings({ ...BASE, protocol: true }), /unknown protocol/, 'ASR_INVALID_PROTOCOL')
})

await record('settings: numeric settings are clamped into range', async () => {
  assert.equal(settingsOf({ requestTimeoutMs: 0 }).requestTimeoutMs, 1)
  assert.equal(settingsOf({ requestTimeoutMs: -5 }).requestTimeoutMs, 1)
  assert.equal(settingsOf({ requestTimeoutMs: 1e12 }).requestTimeoutMs, 2147483647)
  assert.equal(settingsOf({ requestTimeoutMs: Number.NaN }).requestTimeoutMs, 120000)
  assert.equal(settingsOf({ requestTimeoutMs: '9000' }).requestTimeoutMs, 120000)
  assert.equal(settingsOf({ requestTimeoutMs: 1500.9 }).requestTimeoutMs, 1500)
  assert.equal(settingsOf({ maxAudioBytes: 1 }).maxAudioBytes, 46)
  assert.equal(settingsOf({ maxAudioBytes: 4000.7 }).maxAudioBytes, 4000)
})

await record('settings: headers reject injection and non-strings', async () => {
  await rejectsWith(() => settingsOf({ headers: ['x'] }), /`headers` must be a mapping/, 'ASR_INVALID_HEADERS')
  await rejectsWith(() => settingsOf({ headers: { 'bad header': 'x' } }), /not a valid HTTP header name/, 'ASR_INVALID_HEADERS')
  await rejectsWith(() => settingsOf({ headers: { 'X-Key': { nested: true } } }), /must be a string/, 'ASR_INVALID_HEADERS')
  await rejectsWith(() => settingsOf({ headers: { 'X-Key': 'a\r\nInjected: 1' } }), /must not contain a line break/, 'ASR_INVALID_HEADERS')
  assert.deepEqual(settingsOf({ headers: { 'X-Retries': 3 } }).headers, { 'X-Retries': '3' })
})

await record('settings: extraFields and languages reject malformed values', async () => {
  await rejectsWith(() => settingsOf({ extraFields: ['x'] }), /`extraFields` must be a mapping/, 'ASR_INVALID_FIELDS')
  await rejectsWith(() => settingsOf({ languages: 'en' }), /`languages` must be a list/, 'ASR_INVALID_LANGUAGES')
  await rejectsWith(() => settingsOf({ languages: [] }), /at least one accepted language/, 'ASR_INVALID_LANGUAGES')
  assert.deepEqual(settingsOf({ languages: [' en ', '', 'auto'] }).languages, ['en', 'auto'])
})

/* ------------------------------------------------------------------ *
 * WAV parsing
 * ------------------------------------------------------------------ */

await record('wav: canonical recording yields frames and duration', async () => {
  const parsed = parseWav(WAV)
  assert.equal(parsed.sampleRate, 16000)
  assert.equal(parsed.channels, 1)
  assert.equal(parsed.bitsPerSample, 16)
  assert.equal(parsed.dataBytes, 32000)
  assert.equal(parsed.frames, 16000)
  assert.equal(parsed.seconds, 1)
  assert.equal(parseWav(wavOf(0.5)).seconds, 0.5)
  assert.equal(parseWav(wavOf(2.25)).seconds, 2.25)
})

await record('wav: a Buffer subarray with a non-zero offset parses', async () => {
  // The real runtime hands the provider a Buffer from the shared pool, so
  // byteOffset is not zero. The parser must honour byteOffset/byteLength.
  const pooled = Buffer.alloc(WAV.length + 256)
  WAV.copy(pooled, 128)
  const view = pooled.subarray(128, 128 + WAV.length)
  assert.notEqual(view.byteOffset, 0)
  assert.equal(parseWav(view).seconds, 1)
})

await record('wav: a non-Uint8Array is rejected', async () => {
  await rejectsWith(() => parseWav(WAV.buffer), /Uint8Array of WAV bytes/, 'ASR_INVALID_AUDIO')
  await rejectsWith(() => parseWav(undefined), /Uint8Array of WAV bytes/, 'ASR_INVALID_AUDIO')
})

await record('wav: truncated and inconsistent files are rejected', async () => {
  await rejectsWith(() => parseWav(Buffer.alloc(20)), /too short to be a canonical/, 'ASR_INVALID_AUDIO')
  await rejectsWith(() => parseWav(WAV.subarray(0, 44)), /too short to be a canonical/, 'ASR_INVALID_AUDIO')
  // Header intact, body cut short: the RIFF length no longer matches.
  await rejectsWith(() => parseWav(WAV.subarray(0, 1000)), /does not match the file length/, 'ASR_INVALID_AUDIO')
  // Header claims more data than the file holds.
  const lying = Buffer.from(WAV)
  lying.writeUInt32LE(40000, 40)
  await rejectsWith(() => parseWav(lying), /declares 40000 bytes but the file holds 32000/, 'ASR_INVALID_AUDIO')
  // Odd data length cannot be a whole number of 16-bit samples: the lengths
  // agree, so only the odd-size check can catch it.
  const odd = Buffer.concat([WAV, Buffer.from([0])])
  odd.writeUInt32LE(32001, 40)
  odd.writeUInt32LE(odd.length - 8, 4)
  await rejectsWith(() => parseWav(odd), /odd number of bytes/, 'ASR_INVALID_AUDIO')
})

await record('wav: non-PCM, non-mono and non-16-bit files are rejected', async () => {
  await rejectsWith(() => parseWav(wavOf(1, { format: 3 })), /not uncompressed PCM/, 'ASR_INVALID_AUDIO')
  await rejectsWith(() => parseWav(wavOf(1, { channels: 2 })), /2 channels, not mono/, 'ASR_INVALID_AUDIO')
  await rejectsWith(() => parseWav(wavOf(1, { bitsPerSample: 8 })), /8-bit samples/, 'ASR_INVALID_AUDIO')
  await rejectsWith(() => parseWav(wavOf(1, { sampleRate: 48000 })), /48000 Hz sample rate/, 'ASR_INVALID_AUDIO')
  await rejectsWith(() => parseWav(Buffer.concat([Buffer.from('NOPE'), WAV.subarray(4)])), /missing RIFF header/, 'ASR_INVALID_AUDIO')
})

/* ------------------------------------------------------------------ *
 * Request building
 * ------------------------------------------------------------------ */

await record('request: openai multipart shape', async () => {
  const settings = settingsOf({
    apiKey: 'test-key',
    model: 'whisper-1',
    headers: { 'X-Tenant': 'acme' },
    extraFields: { temperature: '0' },
  })
  const request = buildRequest(settings, { audio: WAV, language: 'en' }, { env: {} })
  assert.equal(request.url, BASE.endpoint)
  assert.equal(request.init.method, 'POST')
  assert.equal(request.init.headers.Authorization, 'Bearer test-key')
  assert.equal(request.init.headers['X-Tenant'], 'acme')
  assert.ok(!('content-type' in request.init.headers), 'multipart must let fetch set the boundary')
  const form = request.init.body
  assert.ok(form instanceof FormData)
  const file = form.get('file')
  assert.equal(file.name, 'audio.wav')
  assert.equal(file.type, 'audio/wav')
  assert.equal(file.size, WAV.length)
  assert.deepEqual(Buffer.from(await file.arrayBuffer()), WAV)
  assert.equal(form.get('model'), 'whisper-1')
  assert.equal(form.get('language'), 'en')
  assert.equal(form.get('response_format'), 'json')
  assert.equal(form.get('temperature'), '0')
})

await record('request: openai omits language for auto unless configured', async () => {
  const omitted = buildRequest(settingsOf(), { audio: WAV, language: 'auto' }, { env: {} })
  assert.equal(omitted.init.body.get('language'), null)
  const sent = buildRequest(settingsOf({ sendLanguageWhenAuto: true }), { audio: WAV, language: 'auto' }, { env: {} })
  assert.equal(sent.init.body.get('language'), 'auto')
  const empty = buildRequest(settingsOf(), { audio: WAV, language: '' }, { env: {} })
  assert.equal(empty.init.body.get('language'), null)
  const noModel = buildRequest(settingsOf(), { audio: WAV, language: 'en' }, { env: {} })
  assert.equal(noModel.init.body.get('model'), null)
})

await record('request: openai honours custom field names and array extraFields', async () => {
  const settings = settingsOf({
    audioField: 'audio',
    languageField: 'lang',
    modelField: 'model_name',
    model: 'large-v3',
    extraFields: { hotwords: ['dsh', 'cordis'] },
  })
  const form = buildRequest(settings, { audio: WAV, language: 'zh' }, { env: {} }).init.body
  assert.equal(form.get('audio').name, 'audio.wav')
  assert.equal(form.get('file'), null)
  assert.equal(form.get('lang'), 'zh')
  assert.equal(form.get('model_name'), 'large-v3')
  assert.deepEqual(form.getAll('hotwords'), ['dsh', 'cordis'])
})

await record('request: whisper-cpp sends temperature=0 and no model', async () => {
  const form = buildRequest(
    settingsOf({ protocol: 'whisper-cpp', model: 'ignored-model' }),
    { audio: WAV, language: 'de' },
    { env: {} },
  ).init.body
  assert.equal(form.get('temperature'), '0')
  assert.equal(form.get('model'), null)
  assert.equal(form.get('language'), 'de')
  assert.equal(form.get('response_format'), 'json')
  assert.equal(form.get('file').name, 'audio.wav')
})

await record('request: json protocol base64-encodes the recording', async () => {
  const settings = settingsOf({
    protocol: 'json',
    apiKey: 'json-key',
    model: 'whisper-1',
    extraFields: { temperature: 0, beam_size: 3, response_format: 'json' },
  })
  const request = buildRequest(settings, { audio: WAV, language: 'en' }, { env: {} })
  assert.equal(request.init.headers['content-type'], 'application/json')
  assert.equal(request.init.headers.Authorization, 'Bearer json-key')
  const payload = JSON.parse(request.init.body)
  assert.equal(payload.audio, WAV.toString('base64'))
  assert.equal(payload.language, 'en')
  assert.equal(payload.model, 'whisper-1')
  assert.equal(payload.temperature, 0)
  assert.equal(payload.beam_size, 3)
})

await record('request: json protocol uses configured field names and omits auto', async () => {
  const settings = settingsOf({ protocol: 'json', languageField: 'lang', modelField: 'model_name', model: 'm' })
  const payload = JSON.parse(buildRequest(settings, { audio: WAV, language: 'auto' }, { env: {} }).init.body)
  assert.equal(payload.lang, undefined)
  assert.equal(payload.language, undefined)
  assert.equal(payload.model_name, 'm')
  assert.equal(payload.audio, WAV.toString('base64'))
})

await record('request: raw protocol posts the WAV bytes untouched', async () => {
  const request = buildRequest(settingsOf({ protocol: 'raw', model: 'm' }), { audio: WAV, language: 'en' }, { env: {} })
  assert.equal(request.init.headers['content-type'], 'audio/wav')
  assert.equal(request.init.body, WAV)
  assert.equal(request.init.headers.Authorization, undefined)
})

await record('request: content-type is never taken from extra headers', async () => {
  const openai = buildRequest(settingsOf({ headers: { 'Content-Type': 'text/plain' } }), { audio: WAV, language: 'en' }, { env: {} })
  assert.ok(!('content-type' in openai.init.headers))
  const json = buildRequest(
    settingsOf({ protocol: 'json', headers: { 'content-type': 'text/plain' } }),
    { audio: WAV, language: 'en' },
    { env: {} },
  )
  assert.equal(json.init.headers['content-type'], 'application/json')
  assert.equal(json.init.headers['Content-Type'], undefined)
  const raw = buildRequest(
    settingsOf({ protocol: 'raw', headers: { 'Content-Type': 'text/plain' } }),
    { audio: WAV, language: 'en' },
    { env: {} },
  )
  assert.equal(raw.init.headers['content-type'], 'audio/wav')
})

await record('credentials: apiKeyEnv is read at request time and wins', async () => {
  const settings = settingsOf({ apiKey: 'literal-key', apiKeyEnv: 'ASR_API_KEY' })
  assert.equal(resolveApiKey(settings, {}), 'literal-key')
  assert.equal(resolveApiKey(settings, { ASR_API_KEY: 'env-key' }), 'env-key')
  assert.equal(resolveApiKey(settings, { ASR_API_KEY: '  ' }), 'literal-key')
  assert.equal(resolveApiKey(settingsOf(), {}), '')
  const header = buildRequest(settings, { audio: WAV, language: 'en' }, { env: { ASR_API_KEY: 'env-key' } }).init.headers
  assert.equal(header.Authorization, 'Bearer env-key')
})

await record('credentials: a case-variant auth header is replaced, not duplicated', async () => {
  const settings = settingsOf({ apiKey: 'k', headers: { authorization: 'stale-value' } })
  const headers = buildRequest(settings, { audio: WAV, language: 'en' }, { env: {} }).init.headers
  assert.equal(headers.Authorization, 'Bearer k')
  assert.equal(headers.authorization, undefined)
})

await record('credentials: custom header and scheme compose the value', async () => {
  const settings = settingsOf({ apiKey: 'k', authHeader: 'X-Api-Key', authScheme: '' })
  const headers = buildRequest(settings, { audio: WAV, language: 'en' }, { env: {} }).init.headers
  assert.equal(headers['X-Api-Key'], 'k')
  assert.equal(headers.Authorization, undefined)
})

/* ------------------------------------------------------------------ *
 * Response parsing
 * ------------------------------------------------------------------ */

await record('path: dot paths index arrays and stop at missing values', async () => {
  const value = { text: 'hi', choices: [{ text: 'first' }, { text: 'second' }], nested: { deep: { value: 7 } } }
  assert.equal(extractPath(value, 'text'), 'hi')
  assert.equal(extractPath(value, 'choices.0.text'), 'first')
  assert.equal(extractPath(value, 'choices.1.text'), 'second')
  assert.equal(extractPath(value, 'nested.deep.value'), 7)
  assert.equal(extractPath(value, ''), value)
  assert.equal(extractPath(value, undefined), value)
  assert.equal(extractPath(value, 'missing'), undefined)
  assert.equal(extractPath(value, 'choices.9.text'), undefined)
  assert.equal(extractPath(value, 'choices.text'), undefined)
  assert.equal(extractPath(value, 'text.deeper'), undefined)
  assert.equal(extractPath(value, 'nested.deep.value.deeper'), undefined)
})

await record('response: JSON bodies are read through responsePath', async () => {
  assert.equal(transcriptText(settingsOf(), JSON.stringify({ text: '  hello world  ' }), 'application/json'), 'hello world')
  assert.equal(
    transcriptText(settingsOf({ responsePath: 'choices.0.text' }), JSON.stringify({ choices: [{ text: 'picked' }] }), 'application/json'),
    'picked',
  )
  assert.equal(transcriptText(settingsOf({ responsePath: 'results.0.transcript' }), JSON.stringify({ results: [{ transcript: 'r' }] }), 'application/json'), 'r')
  assert.equal(transcriptText(settingsOf(), JSON.stringify({ text: 5 }), 'application/json'), '5')
  assert.equal(transcriptText(settingsOf(), JSON.stringify({ text: '' }), 'application/json'), '')
  assert.equal(transcriptText(settingsOf(), JSON.stringify({ other: 'x' }), 'application/json'), '')
  assert.equal(transcriptText(settingsOf(), JSON.stringify({ text: null }), 'application/json'), '')
  assert.equal(transcriptText(settingsOf(), '', 'application/json'), '')
})

await record('response: plain text is accepted, HTML and broken JSON are not', async () => {
  assert.equal(transcriptText(settingsOf({ protocol: 'raw' }), '  raw transcript  ', 'text/plain'), 'raw transcript')
  assert.equal(transcriptText(settingsOf({ protocol: 'raw' }), 'raw transcript'), 'raw transcript')
  await rejectsWith(
    () => transcriptText(settingsOf(), '<!doctype html><html></html>', 'text/html'),
    /HTML page instead of a transcript/,
    'ASR_BAD_RESPONSE',
  )
  await rejectsWith(() => transcriptText(settingsOf(), 'not json', 'application/json'), /declared a JSON response/, 'ASR_BAD_RESPONSE')
  await rejectsWith(() => transcriptText(settingsOf(), '{broken', 'application/json'), /does not parse/, 'ASR_BAD_RESPONSE')
  await rejectsWith(() => transcriptText(settingsOf(), '{"text": ', 'text/plain'), /starts like JSON but does not parse/, 'ASR_BAD_RESPONSE')
  await rejectsWith(
    () => transcriptText(settingsOf({ responsePath: 'payload' }), JSON.stringify({ payload: { text: 'x' } }), 'application/json'),
    /selected a object, not text/,
    'ASR_BAD_RESPONSE',
  )
})

/* ------------------------------------------------------------------ *
 * Credential redaction
 * ------------------------------------------------------------------ */

await record('redaction: configured secrets never survive in a message', async () => {
  const secrets = ['super-secret-key', 'Bearer super-secret-key', 'tenant-header-value']
  const body = 'Authorization: Bearer super-secret-key; x-tenant=tenant-header-value'
  const redacted = redactSecrets(body, secrets)
  assert.ok(!redacted.includes('super-secret-key'))
  assert.ok(!redacted.includes('tenant-header-value'))
  assert.match(redacted, /\[redacted\]/)
})

await record('redaction: labelled credentials are masked even when unconfigured', async () => {
  assert.match(redactSecrets('{"api_key":"abcdef123456"}', []), /\[redacted\]/)
  assert.ok(!redactSecrets('{"api_key":"abcdef123456"}', []).includes('abcdef123456'))
  assert.match(redactSecrets('api-key=abcdef123456', []), /\[redacted\]/)
  assert.ok(!redactSecrets('token: abcdef123456', []).includes('abcdef123456'))
})

await record('redaction: short values are left alone, non-strings are safe', async () => {
  assert.equal(redactSecrets('the value is ok', ['ok']), 'the value is ok')
  assert.equal(redactSecrets(undefined, ['secret-value']), '')
})

/* ------------------------------------------------------------------ *
 * Transcribe-level validation (no network is reached)
 * ------------------------------------------------------------------ */

await record('transcribe: audio shape, size and WAV validity are enforced', async () => {
  const settings = settingsOf()
  await rejectsWith(() => transcribe(settings, { audio: 'not-bytes', language: 'en' }), /Uint8Array of WAV bytes/, 'ASR_INVALID_AUDIO')
  await rejectsWith(
    () => transcribe(settingsOf({ maxAudioBytes: 100 }), { audio: WAV, language: 'en' }),
    /maxAudioBytes limit is 100/,
    'ASR_AUDIO_TOO_LARGE',
  )
  await rejectsWith(
    () => transcribe(settings, { audio: WAV.subarray(0, 100), language: 'en' }),
    /does not match the file length|too short to be a canonical/,
    'ASR_INVALID_AUDIO',
  )
})

await record('transcribe: an already-cancelled caller re-throws its own reason', async () => {
  const reason = new Error('user cancelled the recording')
  const controller = new AbortController()
  controller.abort(reason)
  const error = await rejectsWith(() => transcribe(settingsOf(), { audio: WAV, language: 'en' }, controller.signal), /user cancelled/)
  assert.equal(error, reason)
})

/* ------------------------------------------------------------------ *
 * Registration
 * ------------------------------------------------------------------ */

await record('provider: registration shape has no preparation', async () => {
  const settings = settingsOf({ providerId: 'asr-remote', displayName: 'Internal ASR', languages: ['auto', 'en'] })
  const provider = createProvider(settings)
  assert.deepEqual(provider.info, {
    id: 'asr-remote',
    name: 'Internal ASR',
    location: 'cloud',
    languages: ['auto', 'en'],
  })
  assert.equal(provider.preparation, undefined)
  assert.equal(typeof provider.transcribe, 'function')
})

await record('apply: registers one provider and disposes it with the fiber', async () => {
  const captured = { logs: [], warnings: [], unregistered: false }
  let dispose
  const ctx = {
    effect: (factory, label) => {
      captured.label = label
      dispose = factory()
    },
    speechToText: {
      register: (provider) => {
        captured.provider = provider
        return async () => {
          captured.unregistered = true
        }
      },
    },
    logger: {
      debug: (message) => captured.logs.push(message),
      warn: (message) => captured.warnings.push(message),
    },
  }
  apply(ctx, { ...BASE, apiKey: 'super-secret-key', providerId: 'asr-remote' })
  assert.equal(captured.provider.info.id, 'asr-remote')
  assert.equal(captured.provider.info.location, 'cloud')
  assert.equal(captured.label, 'asr-endpoint: remote speech provider')
  assert.ok(captured.logs.length > 0)
  assert.ok(!captured.logs.join('\n').includes('super-secret-key'), 'the credential leaked into a log line')
  assert.equal(captured.warnings.length, 0)
  assert.equal(typeof dispose, 'function')
  await dispose()
  assert.equal(captured.unregistered, true)
})

await record('apply: a row without an endpoint fails at activation', async () => {
  const ctx = { effect: () => {}, speechToText: { register: () => async () => {} } }
  assert.throws(() => apply(ctx, {}), /endpoint.*required/)
  assert.throws(() => apply(ctx, { endpoint: 'ws://asr/x' }), /must use http or https/)
})

await record('apply: a missing apiKeyEnv variable is warned about by name', async () => {
  const captured = { warnings: [] }
  const ctx = {
    effect: (factory) => factory(),
    speechToText: { register: () => async () => {} },
    logger: { debug: () => {}, warn: (message) => captured.warnings.push(message) },
  }
  const variable = 'DSH_ASR_ENDPOINT_ABSENT_KEY'
  delete process.env[variable]
  apply(ctx, { ...BASE, apiKeyEnv: variable })
  assert.equal(captured.warnings.length, 1)
  assert.match(captured.warnings[0], /is not set in the dsh process environment/)
  assert.ok(captured.warnings[0].includes(variable))
})

await record('apply: a language list without auto warns about the service default', async () => {
  const captured = { warnings: [] }
  const ctx = {
    effect: (factory) => factory(),
    speechToText: { register: () => async () => {} },
    logger: { debug: () => {}, warn: (message) => captured.warnings.push(message) },
  }
  apply(ctx, { ...BASE, languages: ['en', 'de'] })
  assert.equal(captured.warnings.length, 1)
  assert.match(captured.warnings[0], /does not include "auto"/)
})

for (const line of results) console.log(line)
const failures = results.filter((line) => line.startsWith('FAIL')).length
console.log(`\n${results.length - failures}/${results.length} checks passed`)
