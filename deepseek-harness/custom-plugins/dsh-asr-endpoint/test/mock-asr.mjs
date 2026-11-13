/**
 * End-to-end check for the ASR endpoint plugin against a local mock server.
 *
 * Starts a throwaway `node:http` server on 127.0.0.1:0 and runs the real
 * `transcribe()` path for every protocol: openai, whisper-cpp, json and raw.
 * Each case asserts both sides — the request shape the server actually
 * received and the transcript handed back — plus the failure paths a remote
 * endpoint produces: a 500 that echoes credentials, a timeout, caller
 * cancellation, an HTML answer and an empty transcript.
 *
 * Offline (loopback only), no running Harness. Run with `node test/mock-asr.mjs`.
 */
import assert from 'node:assert/strict'
import { createServer } from 'node:http'
import { apply, createProvider, resolveSettings } from '../index.js'

/* ------------------------------------------------------------------ *
 * Fixtures
 * ------------------------------------------------------------------ */

/**
 * Build a canonical 16 kHz mono PCM16 WAV recording.
 * @param {number} seconds - duration.
 * @returns {Buffer} WAV bytes.
 */
function wavOf(seconds) {
  const frames = Math.round(16000 * seconds)
  const dataBytes = frames * 2
  const buffer = Buffer.alloc(44 + dataBytes)
  buffer.write('RIFF', 0, 'ascii')
  buffer.writeUInt32LE(36 + dataBytes, 4)
  buffer.write('WAVE', 8, 'ascii')
  buffer.write('fmt ', 12, 'ascii')
  buffer.writeUInt32LE(16, 16)
  buffer.writeUInt16LE(1, 20)
  buffer.writeUInt16LE(1, 22)
  buffer.writeUInt32LE(16000, 24)
  buffer.writeUInt32LE(32000, 28)
  buffer.writeUInt16LE(2, 32)
  buffer.writeUInt16LE(16, 34)
  buffer.write('data', 36, 'ascii')
  buffer.writeUInt32LE(dataBytes, 40)
  for (let sample = 0; sample < frames; sample += 1) {
    buffer.writeInt16LE((sample % 64) - 32, 44 + sample * 2)
  }
  return buffer
}

const WAV = wavOf(0.5)

/**
 * Start one mock ASR server. Every request is recorded before the handler runs.
 * @param {(request: { method: string, url: string, headers: Record<string, string>, body: Buffer }, response: import('node:http').ServerResponse) => unknown} handler - request handler.
 * @returns {Promise<{ server: import('node:http').Server, requests: any[], origin: string, stop: () => Promise<void> }>} handle.
 */
function startServer(handler) {
  const requests = []
  const server = createServer((request, response) => {
    const chunks = []
    request.on('data', (chunk) => chunks.push(chunk))
    request.on('end', () => {
      const recorded = {
        method: request.method,
        url: request.url,
        headers: request.headers,
        body: Buffer.concat(chunks),
      }
      requests.push(recorded)
      Promise.resolve(handler(recorded, response)).catch((error) => {
        try {
          response.writeHead(500, { 'content-type': 'text/plain' }).end(String(error))
        } catch {
          // The client may already have gone away.
        }
      })
    })
  })
  return new Promise((resolve) => {
    server.listen(0, '127.0.0.1', () => {
      const origin = `http://127.0.0.1:${server.address().port}`
      resolve({
        server,
        requests,
        origin,
        stop: async () => {
          server.closeAllConnections?.()
          await new Promise((done) => server.close(done))
        },
      })
    })
  })
}

/**
 * A fake Host context that captures the registered provider.
 * @returns {{ ctx: any, captured: { provider?: any, dispose?: () => Promise<void>, logs: string[], warnings: string[] } }} fixture.
 */
function fakeCtx() {
  const captured = { logs: [], warnings: [] }
  const ctx = {
    effect: (factory) => {
      captured.dispose = factory()
    },
    speechToText: {
      register: (provider) => {
        captured.provider = provider
        return async () => {}
      },
    },
    logger: {
      debug: (message) => captured.logs.push(message),
      warn: (message) => captured.warnings.push(message),
    },
  }
  return { ctx, captured }
}

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
 * Assert a rejection and return the error.
 * @param {() => Promise<unknown>} fn - work that must reject.
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

/** The abort signal a healthy caller provides. */
const liveSignal = () => new AbortController().signal

const servers = []
async function serve(handler) {
  const handle = await startServer(handler)
  servers.push(handle)
  return handle
}

/* ------------------------------------------------------------------ *
 * Protocol round-trips
 * ------------------------------------------------------------------ */

await record('openai: multipart request and transcript through apply()', async () => {
  const mock = await serve((_request, response) => {
    response.writeHead(200, { 'content-type': 'application/json' })
    response.end(JSON.stringify({ text: '  hello from openai  ' }))
  })
  const { ctx, captured } = fakeCtx()
  apply(ctx, {
    endpoint: `${mock.origin}/v1/audio/transcriptions`,
    protocol: 'openai',
    model: 'whisper-1',
    apiKey: 'test-key',
    headers: { 'X-Tenant': 'acme' },
    extraFields: { temperature: '0' },
  })
  const result = await captured.provider.transcribe({ audio: WAV, language: 'en' }, liveSignal())
  assert.equal(result.text, 'hello from openai')
  assert.equal(result.audioSeconds, 0.5)
  assert.ok(result.inferenceSeconds >= 0 && Number.isFinite(result.inferenceSeconds))

  const request = mock.requests[0]
  assert.equal(request.method, 'POST')
  assert.equal(request.url, '/v1/audio/transcriptions')
  assert.equal(request.headers.authorization, 'Bearer test-key')
  assert.equal(request.headers['x-tenant'], 'acme')
  assert.match(request.headers['content-type'], /^multipart\/form-data; boundary=/)
  const body = request.body.toString('latin1')
  assert.match(body, /name="file"; filename="audio\.wav"/)
  assert.match(body, /Content-Type: audio\/wav/)
  assert.match(body, /name="model"\r\n\r\nwhisper-1/)
  assert.match(body, /name="language"\r\n\r\nen/)
  assert.match(body, /name="response_format"\r\n\r\njson/)
  assert.match(body, /name="temperature"\r\n\r\n0/)
  assert.ok(request.body.includes(WAV), 'the recorded WAV bytes must be sent verbatim')
  await captured.dispose()
})

await record('whisper-cpp: multipart request and transcript', async () => {
  const mock = await serve((_request, response) => {
    response.writeHead(200, { 'content-type': 'application/json' })
    response.end(JSON.stringify({ text: 'hello from whisper.cpp' }))
  })
  const provider = createProvider(resolveSettings({ endpoint: `${mock.origin}/inference`, protocol: 'whisper-cpp' }))
  const result = await provider.transcribe({ audio: WAV, language: 'de' }, liveSignal())
  assert.equal(result.text, 'hello from whisper.cpp')
  assert.equal(result.audioSeconds, 0.5)
  const request = mock.requests[0]
  assert.equal(request.url, '/inference')
  const body = request.body.toString('latin1')
  assert.match(body, /name="temperature"\r\n\r\n0/)
  assert.doesNotMatch(body, /name="model"/)
  assert.match(body, /name="language"\r\n\r\nde/)
  assert.match(body, /name="response_format"\r\n\r\njson/)
  assert.ok(request.body.includes(WAV))
})

await record('json: base64 body, dot-path response', async () => {
  const mock = await serve((_request, response) => {
    response.writeHead(200, { 'content-type': 'application/json' })
    response.end(JSON.stringify({ results: [{ transcript: 'hello from json' }] }))
  })
  const provider = createProvider(
    resolveSettings({
      endpoint: `${mock.origin}/asr`,
      protocol: 'json',
      model: 'whisper-1',
      responsePath: 'results.0.transcript',
      extraFields: { temperature: 0 },
    }),
  )
  const result = await provider.transcribe({ audio: WAV, language: 'en' }, liveSignal())
  assert.equal(result.text, 'hello from json')
  const request = mock.requests[0]
  assert.equal(request.headers['content-type'], 'application/json')
  const payload = JSON.parse(request.body.toString('utf8'))
  assert.equal(payload.audio, WAV.toString('base64'))
  assert.equal(payload.language, 'en')
  assert.equal(payload.model, 'whisper-1')
  assert.equal(payload.temperature, 0)
})

await record('raw: WAV body, plain-text response', async () => {
  const mock = await serve((_request, response) => {
    response.writeHead(200, { 'content-type': 'text/plain; charset=utf-8' })
    response.end('  hello from raw  ')
  })
  const provider = createProvider(resolveSettings({ endpoint: `${mock.origin}/transcribe`, protocol: 'raw' }))
  const result = await provider.transcribe({ audio: WAV, language: 'en' }, liveSignal())
  assert.equal(result.text, 'hello from raw')
  const request = mock.requests[0]
  assert.equal(request.headers['content-type'], 'audio/wav')
  assert.deepEqual(request.body, WAV)
})

await record('openai: an empty transcript is not an error', async () => {
  const mock = await serve((_request, response) => {
    response.writeHead(200, { 'content-type': 'application/json' })
    response.end(JSON.stringify({ text: '' }))
  })
  const provider = createProvider(resolveSettings({ endpoint: `${mock.origin}/asr` }))
  const result = await provider.transcribe({ audio: WAV, language: 'auto' }, liveSignal())
  assert.equal(result.text, '')
  // `auto` is not sent unless sendLanguageWhenAuto is on.
  const body = mock.requests[0].body.toString('latin1')
  assert.doesNotMatch(body, /name="language"/)
})

/* ------------------------------------------------------------------ *
 * Failure paths
 * ------------------------------------------------------------------ */

await record('500: status and snippet surface, credentials do not', async () => {
  const mock = await serve((_request, response) => {
    response.writeHead(500, { 'content-type': 'text/plain' })
    response.end('Authorization: Bearer super-secret-key\nupstream model exploded')
  })
  const provider = createProvider(
    resolveSettings({ endpoint: `${mock.origin}/asr`, apiKey: 'super-secret-key' }),
  )
  const error = await rejectsWith(
    () => provider.transcribe({ audio: WAV, language: 'en' }, liveSignal()),
    /responded 500/,
    'ASR_HTTP_ERROR',
  )
  assert.match(error.message, /upstream model exploded/)
  assert.match(error.message, /\[redacted\]/)
  assert.ok(!error.message.includes('super-secret-key'), 'the API key leaked into the error')
  assert.ok(error.message.length < 900, 'the body snippet must be truncated')
})

await record('timeout: a hanging endpoint fails with the deadline', async () => {
  const mock = await serve(() => {
    // Never answers.
  })
  const provider = createProvider(
    resolveSettings({ endpoint: `${mock.origin}/asr`, requestTimeoutMs: 150 }),
  )
  const startedAt = Date.now()
  const error = await rejectsWith(
    () => provider.transcribe({ audio: WAV, language: 'en' }, liveSignal()),
    /did not answer within 150 ms/,
    'ASR_TIMEOUT',
  )
  assert.ok(Date.now() - startedAt >= 100, 'the deadline must actually be waited for')
  assert.equal(error.code, 'ASR_TIMEOUT')
})

await record('cancellation: the caller reason wins over the deadline', async () => {
  const mock = await serve(() => {
    // Never answers.
  })
  const provider = createProvider(
    resolveSettings({ endpoint: `${mock.origin}/asr`, requestTimeoutMs: 5000 }),
  )
  const controller = new AbortController()
  const reason = new Error('user cancelled the recording')
  setTimeout(() => controller.abort(reason), 50)
  const error = await rejectsWith(
    () => provider.transcribe({ audio: WAV, language: 'en' }, controller.signal),
    /user cancelled/,
  )
  assert.equal(error, reason, 'the caller reason must be re-thrown unchanged')
})

await record('html: a misconfigured endpoint URL is reported clearly', async () => {
  const mock = await serve((_request, response) => {
    response.writeHead(200, { 'content-type': 'text/html' })
    response.end('<!doctype html><html><body>not an ASR API</body></html>')
  })
  const provider = createProvider(resolveSettings({ endpoint: `${mock.origin}/` }))
  await rejectsWith(
    () => provider.transcribe({ audio: WAV, language: 'en' }, liveSignal()),
    /HTML page instead of a transcript/,
    'ASR_BAD_RESPONSE',
  )
})

await record('credentials: apiKeyEnv is read on every request', async () => {
  const mock = await serve((_request, response) => {
    response.writeHead(200, { 'content-type': 'application/json' })
    response.end(JSON.stringify({ text: 'ok' }))
  })
  const variable = 'DSH_ASR_ENDPOINT_TEST_KEY'
  const provider = createProvider(
    resolveSettings({ endpoint: `${mock.origin}/asr`, apiKeyEnv: variable }),
  )
  try {
    process.env[variable] = 'first-env-key'
    await provider.transcribe({ audio: WAV, language: 'en' }, liveSignal())
    process.env[variable] = 'second-env-key'
    await provider.transcribe({ audio: WAV, language: 'en' }, liveSignal())
  } finally {
    delete process.env[variable]
  }
  assert.equal(mock.requests[0].headers.authorization, 'Bearer first-env-key')
  assert.equal(mock.requests[1].headers.authorization, 'Bearer second-env-key')
})

/* ------------------------------------------------------------------ *
 * Report and cleanup
 * ------------------------------------------------------------------ */

for (const line of results) console.log(line)
const failures = results.filter((line) => line.startsWith('FAIL')).length
console.log(`\n${results.length - failures}/${results.length} checks passed`)

for (const handle of servers) await handle.stop()
