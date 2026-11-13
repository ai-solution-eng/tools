# Remote ASR endpoint

Contributed plugin — the collection's [README](../README.md) has the install/contribution guide.

A **Host-only** Harness bundle that replaces the local SenseVoice recognizer with a remote HTTP
speech-recognition endpoint. The microphone in the composer works exactly as before; the finished
recording is posted to the URL configured in `cordis.patch.yml` instead of being decoded on the
Host.

The endpoint and its wire protocol are the point of the plugin: the same package points at an
OpenAI-compatible server, a `whisper.cpp` server, a JSON gateway, or a raw-WAV endpoint without a
code change.

```
browser mic ──▶ ctx.speechToText ──▶ this provider ──HTTP──▶ your ASR endpoint
```

## What it registers

One `ctx.speechToText` provider:

| Field | Value |
|---|---|
| `info.id` | `providerId` (default `asr-endpoint`) |
| `info.name` | `displayName` (default `ASR endpoint (<host>)`) |
| `info.location` | `cloud` |
| `info.languages` | `languages` (default `auto, en, zh, de, fr, es, it, pt, ja, ko`) |
| `preparation` | **absent** — a remote endpoint needs no download or model load |

A provider without `preparation` is fully supported: the registry reports `{ phase: 'ready' }` for
it, so the mic is usable immediately and no setup dialog can fire. There is no browser half and no
`dsh.client` declaration — the microphone itself comes from the upstream voice-input package.

## Install

Per profile, like any other bundle (see the collection README). Installing this bundle inserts four
Loader rows:

| Row id | Package | Why |
|---|---|---|
| `asr-speech-to-text` | `@deepseek-ai/dsh-experimental-speech-to-text` | The provider registry. `defaultProvider` must equal this plugin's `providerId` |
| `asr-endpoint` | `@local/dsh-asr-endpoint` | This plugin: registers the remote provider |
| `asr-api-speech-to-text` | `@deepseek-ai/dsh-experimental-api-speech-to-text` | The `speech/*` Remote routes the browser calls |
| `asr-ui-voice-input` | `@deepseek-ai/dsh-experimental-client-ui-voice-input` | The mic in `conversation.input.activity` |

The three upstream packages resolve from the dsh installation; only this package is linked into the
profile. The `asr-*` row ids are deliberate: a profile patch targets rows **by id**, so reusing the
upstream ids (`speech-to-text`, `api-speech-to-text`, `ui-voice-input`) would silently retarget a
later install of `@deepseek-ai/dsh-experimental-voice-input-bundle`.

The shipped row carries a placeholder endpoint. Point it at your server by overriding the row in the
profile's `cordis.patch.yml`:

```yaml
- id: asr-endpoint
  config:
    endpoint: https://asr.example.com/v1/audio/transcriptions
    apiKeyEnv: ASR_API_KEY
```

`config` is **replaced wholesale**, never deep-merged: every key you omit falls back to the plugin
default below, not to the placeholder shipped in the bundle patch. The same edit can be made from
the Plugins page, which writes the override into the profile patch for you.

Export the credential in the environment of the **dsh process** (not just your shell) — a container
or systemd unit needs it in its own environment:

```bash
export ASR_API_KEY=…        # read at request time, so rotation needs no restart
```

Verify: the Plugins page shows the `asr-endpoint` row active, and the voice settings list a provider
named `ASR endpoint (<host>)` that is ready.

## Configuration

Every value except `endpoint` is optional; the defaults are what the plugin uses.

| Field | Default | Meaning |
|---|---|---|
| `endpoint` | — (**required**) | Absolute `http`/`https` URL of the transcription endpoint |
| `protocol` | `openai` | `openai` \| `whisper-cpp` \| `json` \| `raw` |
| `model` | `''` | Sent as the model field; empty omits it |
| `apiKey` | `''` | Literal key (discouraged — it lands in the profile) |
| `apiKeyEnv` | `''` | Environment variable read at request time; wins over `apiKey` |
| `authHeader` | `Authorization` | Header carrying the key |
| `authScheme` | `Bearer ` | Prefix before the key (the trailing space is kept) |
| `headers` | `{}` | Extra request headers |
| `providerId` | `asr-endpoint` | Registration id; must equal the service row's `defaultProvider` |
| `displayName` | `ASR endpoint (<host>)` | Shown in the voice UI provider list |
| `languages` | `auto, en, zh, de, fr, es, it, pt, ja, ko` | Accepted language hints; `auto` must be present |
| `requestTimeoutMs` | `120000` | Per-request deadline |
| `maxAudioBytes` | `16777216` | Reject larger recordings before sending |
| `responsePath` | `text` | Dot path into a JSON response (`choices.0.text` works) |
| `audioField` | `file` | Multipart field name for the audio |
| `languageField` | `language` | Field name for the language hint |
| `modelField` | `model` | Field name for the model |
| `responseFormat` | `json` | Multipart `response_format` value; empty omits it |
| `sendLanguageWhenAuto` | `false` | Send `language` even when it is `auto` |
| `extraFields` | `{}` | Extra multipart / JSON fields |

Notes:

- `endpoint` is validated at activation: a relative URL, a non-`http(s)` scheme, or embedded
  credentials (`https://user:pass@…`) fails the row with a message naming the problem.
- `languages` must include `auto` unless the service row sets a different `language`, because the
  service's default is `auto` and `resolve()` rejects a language the provider does not advertise.
  A list without `auto` logs a warning at activation.
- If `apiKeyEnv` names a variable the dsh process does not have, activation logs a warning naming
  the variable; requests are then sent without a credential.
- A recording is only sent after it passes canonical WAV validation (16 kHz, mono, 16-bit PCM) and
  the `maxAudioBytes` check.
- Responses larger than 1 MiB are rejected instead of buffered — a transcript is tiny, and this
  catches an `endpoint` that points at a file server.

## Protocols

`audioSeconds` is always the recording length (WAV data-chunk frames ÷ sample rate), and
`inferenceSeconds` is the monotonic wall-clock time of the HTTP round-trip. An empty or missing
transcript returns `text: ''`, which the service documents as "no speech".

### `openai`

`POST` the endpoint as `multipart/form-data`: `file` = `audio.wav` (`audio/wav`), `model` (if set),
`language` (unless `auto` and `!sendLanguageWhenAuto`), `response_format`, plus `extraFields`.
Response JSON, read `responsePath`.

```bash
curl -sS "$ENDPOINT" \
  -H "Authorization: Bearer $ASR_API_KEY" \
  -F file=@sample.wav \
  -F model=whisper-1 \
  -F language=en \
  -F response_format=json
# → {"text":"hello world"}
```

Covers OpenAI itself, faster-whisper-server, vLLM, FunASR-OpenAI, speaches, LocalAI, LiteLLM and
most gateways.

### `whisper-cpp`

`POST` `multipart/form-data`: `file`, `response_format`, `language` (same `auto` rule),
`temperature=0`, plus `extraFields`. Response JSON `{ "text": … }`.

```bash
curl -sS "$ENDPOINT" \
  -F file=@sample.wav \
  -F response_format=json \
  -F language=en \
  -F temperature=0
# → {"text":"hello world"}
```

Point `endpoint` at the server's `/inference` route. A build that ignores `response_format` and
answers with plain text still works — a non-JSON body is accepted as the transcript.

### `json`

`POST` the endpoint as `application/json` with the recording base64-encoded:

```bash
curl -sS "$ENDPOINT" \
  -H 'Content-Type: application/json' \
  -d "{\"audio\":\"$(base64 -w0 sample.wav)\",\"language\":\"en\",\"model\":\"whisper-1\"}"
# → {"results":[{"transcript":"hello world"}]}   with responsePath: results.0.transcript
```

`audio` is always the literal key; `language` and `model` use `languageField` / `modelField`.
`extraFields` are merged into the object as-is (numbers stay numbers).

### `raw`

`POST` the raw WAV bytes with `Content-Type: audio/wav`:

```bash
curl -sS "$ENDPOINT" \
  -H 'Content-Type: audio/wav' \
  --data-binary @sample.wav
# → hello world        (plain text; a JSON body is read through responsePath)
```

`extraFields` do not apply to this protocol.

## Pointing it at common servers

| Server | Row config |
|---|---|
| OpenAI / Azure OpenAI | `protocol: openai`, `endpoint: https://api.openai.com/v1/audio/transcriptions`, `model: whisper-1` (or `gpt-4o-transcribe`), `apiKeyEnv: OPENAI_API_KEY` |
| faster-whisper-server | `protocol: openai`, `endpoint: http://faster-whisper:8000/v1/audio/transcriptions`, `model: Systran/faster-whisper-large-v3` |
| whisper.cpp server | `protocol: whisper-cpp`, `endpoint: http://whisper:8080/inference` |
| vLLM / LiteLLM / LocalAI | `protocol: openai`, `endpoint: <base>/v1/audio/transcriptions`, `model: <served model>` |
| Custom JSON service | `protocol: json`, `responsePath: <dot path to the text>` |
| Raw WAV service | `protocol: raw` |

Servers that return `{ "text": … }` need no `responsePath`; anything else needs the dot path to its
text field. A server that wants a different form field name (e.g. `audio` instead of `file`) is
covered by `audioField` / `languageField` / `modelField`.

### PCAI LLM gateway (`cohere-transcribe`)

The shipped default row points at the in-cluster gateway, which serves `cohere-transcribe` on an
OpenAI-compatible passthrough at `/v1/audio/transcriptions`:

```yaml
endpoint: http://pcai-llm-gateway-service.pcai-llm.svc.cluster.local:9200/v1/audio/transcriptions
protocol: openai
model: ''                                     # IMPORTANT: keep this empty
headers:
  X-PCAI-Model: cohere-transcribe             # the gateway's model-resolution channel
apiKeyEnv: PCAI_LLM_KEY
```

**The gotcha, and why `model` is empty.** The gateway resolves the model itself — `?model=` first,
then the `X-PCAI-Model` header, then the multipart `model` field — but it forwards the multipart body
**byte for byte** to the voice upstream. A `model` field in the body therefore reaches the upstream
verbatim, which rejects the gateway's alias:

```
model: cohere-transcribe        → 404 {"error":{"message":"The model `cohere-transcribe` does not exist."}}
X-PCAI-Model: cohere-transcribe → 200 {"text":" Thank you.","usage":{"type":"duration","seconds":1}}
```

So name the model out-of-band (header above, or `?model=cohere-transcribe` on the endpoint URL) and
send **no** multipart `model` field. Verified against the deployed gateway: the provider returned
`{"text":"Thank you.","audioSeconds":1,"inferenceSeconds":0.33}`.

`X-PCAI-Model` is gateway-specific. For a plain OpenAI-compatible server, drop the `headers` block
and put the served model name in `model` instead.

## Behaviour and failure modes

| Situation | What happens |
|---|---|
| Caller cancels the recording | The caller's own abort reason is re-thrown unchanged |
| Deadline exceeded | `ASR_TIMEOUT`: "ASR endpoint `<host>` did not answer within `<n>` ms" |
| Non-2xx answer | `ASR_HTTP_ERROR` with the status and a ≤512-character body snippet |
| Endpoint unreachable | `ASR_UNREACHABLE` with the transport detail |
| Body is not JSON where JSON is required | `ASR_BAD_RESPONSE` naming the problem |
| HTML answer | `ASR_BAD_RESPONSE` suggesting the endpoint URL is the web UI, not the API |
| Empty / missing transcript | `{ text: '', audioSeconds, inferenceSeconds }` |
| Malformed or oversized recording | `ASR_INVALID_AUDIO` / `ASR_AUDIO_TOO_LARGE`, before any request |

The credential is never included in an error message or a log line: the plugin scrubs the literal
key, the environment value, the composed auth header and every configured header value, and masks
labelled credentials (`api_key`, `authorization`, `token`, …) that a server echoes back. The
activation log names only the provider id, the endpoint host and the protocol.

Use `https` when the endpoint is not on a trusted network: with `http`, the key and the audio travel
in clear text. Embedded URL credentials are rejected — Node's `fetch` cannot send them anyway.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `Speech provider is unavailable: asr-endpoint` | The service row's `defaultProvider` does not match this row's `providerId` |
| `Speech provider asr-endpoint does not support language: auto` | `languages` was overridden without `auto` |
| `Speech provider already registered: asr-endpoint` | Two provider rows share a `providerId`, or the upstream bundle is also installed |
| `package … resolves from multiple active Loader sources` | The upstream voice-input bundle is installed alongside this one (see below) |
| HTTP 401 / 403 | The environment variable named by `apiKeyEnv` is missing from the dsh process environment, or the scheme/header is wrong |
| `HTML page instead of a transcript` | `endpoint` points at the server's web UI; use the API route |
| `did not answer within 120000 ms` | The server is slow or unreachable; raise `requestTimeoutMs` or check the network path from the dsh process |
| `ASR request … failed: fetch failed (ECONNREFUSED)` | Wrong host/port, or the endpoint is not reachable from the dsh process's network namespace |
| Mic is missing entirely | The `asr-ui-voice-input` row is not active; check the Plugins page |
| `Audio must be a canonical 16 kHz mono PCM16 WAV recording` | Only possible when calling the provider directly: the browser recorder and the API layer both produce canonical WAV |

## Do not install alongside the upstream voice bundle

`@deepseek-ai/dsh-experimental-voice-input-bundle` mounts the same registry, API and UI packages.
Installing it **and** this bundle mounts each package twice and fails at activation:

- `Speech provider already registered: asr-endpoint` (duplicate provider id), or
- `package @deepseek-ai/dsh-experimental-… resolves from multiple active Loader sources: …; remove one entry`.

Remove one of them. This bundle is a replacement for that bundle's local recognizer row, not an
addition to it.

## Files

| File | Role |
|---|---|
| `index.js` | Host half: settings resolution, WAV validation, request building, response parsing, provider registration |
| `cordis.patch.yml` | The four Loader rows and every tunable, commented with its default |
| `locale/{en,zh}.json` | Plugin-manager metadata only (`meta.title` / `meta.description`) |
| `icon.svg` | Plugin-manager icon (`currentColor`, so it follows the theme) |
| `test/host-check.mjs` | Offline checks: settings, WAV parsing, request building, response parsing, redaction, registration |
| `test/mock-asr.mjs` | Loopback mock server: full `transcribe()` round-trip for all four protocols plus the failure paths |

No `@deepseek-ai/*` package is imported: a `link:`-ed third-party package resolves only its own
`node_modules`, so the plugin uses Node builtins and globals (`fetch`, `FormData`, `Blob`,
`AbortSignal`) and applies its own defaults instead of exporting a `Config` schema.

## Tests

Both run with plain `node` — no network beyond loopback, no running Harness:

```bash
node --check index.js
node test/host-check.mjs
node test/mock-asr.mjs
```
