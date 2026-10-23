/**
 * pi-ai assistant event translation into the Harness streaming protocol.
 *
 * pi-ai tool-call arguments are parsed objects while the Harness keeps their
 * raw JSON representation. pi-ai also reports failures as terminal stream
 * events, which this module maps into Harness finish chunks.
 *
 * @module dsh-llm-pi-ai/stream
 */

import { brandString } from '@deepseek-ai/dsh-brand'
import { CONTEXT_WINDOW_EXCEEDED_CODE, EMPTY_RESPONSE_CODE, isContextWindowExceededError, isQuotaExceededError, LlmError, QUOTA_EXCEEDED_CODE } from '@deepseek-ai/dsh-llm'
import type { FinishReason, StreamChunk, TokenUsage, ToolCallId } from '@deepseek-ai/dsh-llm'
import { isContextOverflow } from '@earendil-works/pi-ai/utils/overflow'
import type { AssistantMessage, AssistantMessageEvent, Usage as PiUsage } from '@earendil-works/pi-ai'
import { toPiReplayState } from './replay.ts'

/**
 * One standalone gateway telemetry object (`{"pcai": {…}}` SSE data line,
 * forwarded by the patched pi-ai provider as a `pcai_event`). The tombstone
 * renderer consumes the tool_call/tool_error outcome fields; every other kind
 * is ignored (the dist provider itself consumes `thinking` iterations for the
 * cycle breaks).
 */
interface PcaiEventPayload {
  event?: unknown
  id?: unknown
  server?: unknown
  tool?: unknown
  ok?: unknown
  ms?: unknown
  args?: unknown
  result?: unknown
  detail?: unknown
}

/**
 * Map pi-ai usage (reasoning folded into output by pi-ai).
 * @param usage - cumulative usage from the terminal pi-ai event.
 * @returns harness counts with pi-ai's exact total; cache fields appear only
 *   when non-zero (pi-ai reports zeros, not absence).
 */
export function mapUsage(usage: PiUsage): TokenUsage {
  return {
    inputTokens: usage.input,
    outputTokens: usage.output,
    totalTokens: usage.totalTokens,
    ...usage.cacheRead > 0 ? { cacheReadTokens: usage.cacheRead } : {},
    ...usage.cacheWrite > 0 ? { cacheWriteTokens: usage.cacheWrite } : {},
  }
}

// XXX(pi-ai upstream): pi-ai flattens the caught error to `error.message`
// (api/anthropic-messages.js: `errorMessage = error instanceof Error ?
// error.message : JSON.stringify(error)`), discarding the original Error and its
// `cause` chain before it reaches us. undici carries the actionable transport
// detail on `cause` (e.g. `SocketError: other side closed`) but hands the fetch
// wrapper a bare `terminated`, so we are left pattern-matching terse words here.
// If pi-ai ever forwards the original Error (or a fetch/dispatcher hook that lets
// us capture the cause ourselves), classify on `code`/`cause` instead of text.
function classifyPiAiError(message: string): string {
  if (/\b(?:401|403)\b/.test(message)) return 'AUTH'
  if (isQuotaExceededError(message)) return QUOTA_EXCEEDED_CODE
  if (/\b429\b|rate.?limit/i.test(message)) return 'RATE_LIMIT'
  // A rejected request body (gateway or provider size cap): resending the
  // same request cannot succeed, so it is invalid, not transient.
  if (/\b413\b|failed to buffer the request body:\s*length limit exceeded|payload too large|request body too large/i.test(message)) return 'INVALID_REQUEST'
  if (/\b400\b|invalid.?request/i.test(message)) return 'INVALID_REQUEST'
  if (/\b5\d\d\b/.test(message)) return 'SERVER'
  if (/\btime(?:d)?\s*out\b|timeout/i.test(message)) return 'TIMEOUT'
  // A stream truncated before the provider's terminal event: each pi-ai provider
  // throws its own wording when the wire closes mid-response without a terminal
  // event (`… stream ended before message_stop`, `… before a terminal response
  // event`, `… ended without a terminal event`, `Stream ended without
  // finish_reason`). The connection dropped mid-response, so this is a transport
  // truncation, not a model-level error.
  if (/stream ended (?:before|without)\b/i.test(message)) return 'TRANSPORT'
  if (/\b(?:network|connection|socket|fetch)\b|\bECONN[A-Z]+\b/i.test(message)
    || /\b(?:other side closed|HTTP2 request did not get a response|WebSocket closed unexpectedly)\b/i.test(message)
    // undici renders a mid-stream socket drop as a bare `terminated` (its
    // `cause` — the real SocketError — was flattened away upstream); Node's
    // stream layer says `Premature close`.
    || /\bterminated\b|premature close/i.test(message)) {
    return 'TRANSPORT'
  }
  return 'PI_AI_ERROR'
}

/**
 * Map a terminal pi-ai event to the harness finish reason.
 * @param message - the assistant message carried by the `done` or `error` event.
 * @param contextWindow - resolved catalog capacity for usage-based overflow detection.
 * @returns the mapped harness reason. Recognized error text, `stop` usage above
 *   `contextWindow`, and zero-output `length` usage that fills the window map
 *   to `CONTEXT_WINDOW_EXCEEDED`; a `stop` with no content blocks maps to an
 *   `EMPTY_RESPONSE` error, while terminal `pending` and `deferred` states map
 *   to non-retryable `PI_AI_ERROR` failures.
 */
export function mapStopReason(message: AssistantMessage, contextWindow?: number): FinishReason {
  const piAiOverflow = isContextOverflow(message, contextWindow)
  const harnessOverflow = message.stopReason === 'error'
    && message.errorMessage !== undefined
    && isContextWindowExceededError(message.errorMessage)
  if (piAiOverflow || harnessOverflow) {
    return {
      kind: 'error',
      failure: {
        message: message.errorMessage ?? `pi-ai detected context overflow for model "${message.model}"`,
        code: CONTEXT_WINDOW_EXCEEDED_CODE,
      },
    }
  }

  switch (message.stopReason) {
    case 'stop':
      // A terminal stop that produced no content blocks is a degenerate
      // provider completion, not a successful (empty) assistant message.
      if (message.content.length === 0) {
        return {
          kind: 'error',
          failure: {
            message: `model "${message.model}" returned a completed response with no content`,
            code: EMPTY_RESPONSE_CODE,
          },
        }
      }
      return { kind: 'stop' }
    case 'length': return { kind: 'max-tokens' }
    case 'toolUse': return { kind: 'tool-calls' }
    case 'pending': return {
      kind: 'error',
      failure: { message: `pi-ai stream for model "${message.model}" ended pending`, code: 'PI_AI_ERROR' },
    }
    case 'deferred': return {
      kind: 'error',
      failure: { message: `pi-ai deferred response for model "${message.model}" is not supported`, code: 'PI_AI_ERROR' },
    }
    case 'aborted': return {
      kind: 'aborted',
      failure: { message: message.errorMessage ?? 'pi-ai stream aborted', code: 'ABORTED' },
    }
    case 'error': {
      const text = message.errorMessage ?? 'pi-ai stream error'
      return { kind: 'error', failure: { message: text, code: classifyPiAiError(text) } }
    }
  }
}

/**
 * Translate the pi-ai event stream into StreamChunks. pi-ai never throws
 * mid-stream — failures arrive as `error` events, which become error/aborted
 * `finish` chunks (the harness protocol's other error-delivery style).
 * @param events - one assistant turn's pi-ai event stream.
 * @param contextWindow - resolved catalog capacity for usage-based overflow detection.
 * @param callerSignal - caller cancellation state; an aborted caller makes any
 *   in-band terminal error an aborted finish.
 * @param requestedModel - request model identity recorded for durable replay.
 * @returns the harness chunks, ending with `usage` then `finish`; throws
 *   `LlmError` (`STREAM_CLOSED`) if the source ends without a terminal event.
 */
export async function* toStreamChunks(
  events: AsyncIterable<AssistantMessageEvent>,
  contextWindow?: number,
  callerSignal?: AbortSignal,
  requestedModel?: string,
): AsyncGenerator<StreamChunk> {
  // pi-ai contentIndex ↔ our block index map 1:1 (both count blocks from 0
  // in stream order), but we track ids per index for tool calls.
  const toolIds = new Map<number, { id: string; name: string }>()

  // PCAI gateway relay (r3): the gateway executes MCP tools in-cluster inside
  // this same SSE stream. Its tool calls arrive as stamped inert deltas
  // (`pcai.relay: true`, display-only) that the patched provider emits as
  // ordinary toolcall_start/delta/end events — but they must NEVER reach the
  // harness as tool-call blocks: the agent loop executes every tool-call block
  // in the assembled message, and a relay call has no client-side result (the
  // gateway already ran it). r3 renders each relay call as a SYNTHETIC
  // REASONING block — one per call, opened and closed at the call's telemetry
  // event stream position (the provider now emits relay toolcall_end there) —
  // carrying the call name, full streamed args, and the gateway's result
  // preview (≤4000 chars). The r2 single ever-growing tombstone block (and
  // its 200/300-char caps) is gone; per-call blocks keep the DSH Think panels
  // separating per agent cycle instead of one panel swallowing the turn.
  type RelayCall = {
    id: string
    name: string
    args: string
    pcai?: { ok: boolean; ms?: number; result?: string }
  }
  /** Relay calls by pi-ai contentIndex (suppression state per block). */
  const relayCalls = new Map<number, RelayCall>()
  /** Result-join index: tool_call id (primary) then tool name (fallback). */
  const relayById = new Map<string, RelayCall>()
  const relayByName = new Map<string, RelayCall>()
  /** Most recent relay call — join fallback when the event names neither. */
  let lastRelay: RelayCall | undefined
  /** True once any tombstone was emitted; guards the relay-only EMPTY_RESPONSE case. */
  let sawRelayRender = false
  /** Stream position of the next synthetic reasoning block hosting a tombstone. */
  let nextRelayIndex = 1_000_000

  const collapseWhitespace = (text: string, cap: number): string => {
    const flat = text.replace(/\s+/gu, ' ').trim()
    return flat.length > cap ? `${flat.slice(0, cap)}…` : flat
  }

  /** Open one synthetic reasoning block for one relay call and emit its full tombstone. */
  function* renderTombstone(call: RelayCall): Generator<StreamChunk> {
    const index = nextRelayIndex
    nextRelayIndex += 1
    sawRelayRender = true
    yield { type: 'block-start', index, blockType: 'reasoning' }
    let text = ''
    const push = (chunk: string): StreamChunk[] => {
      text += chunk
      return [{ type: 'reasoning-delta', index, text: chunk }]
    }
    const chunks: StreamChunk[] = []
    const argsPart = call.args.length > 0 ? collapseWhitespace(call.args, 2000) : ''
    chunks.push(...push(`\n[tool] ${call.name}(${argsPart}) `))
    // The matching pcai_event carries the outcome; a call whose event never
    // arrived was still executed server-side (the stamped delta proves it).
    if (call.pcai === undefined) {
      chunks.push(...push('\n→ executed server-side\n'))
    } else {
      const ms = typeof call.pcai.ms === 'number' ? ` (${call.pcai.ms}ms)` : ''
      chunks.push(...push(`\n→ ${call.pcai.ok ? 'ok' : 'error'}${ms}\n`))
      if (typeof call.pcai.result === 'string' && call.pcai.result.length > 0) {
        chunks.push(...push(collapseWhitespace(call.pcai.result, 4000) + '\n'))
      }
    }
    for (const chunk of chunks) yield chunk
    yield { type: 'block-end', index, block: { type: 'reasoning', text } }
  }

  /** Join a gateway telemetry payload to its relay call; undefined when nothing matches. */
  function relayCallFor(pcai: PcaiEventPayload): RelayCall | undefined {
    const byId = typeof pcai.id === 'string' ? relayById.get(pcai.id) : undefined
    if (byId !== undefined) return byId
    const byName = typeof pcai.tool === 'string' ? relayByName.get(pcai.tool) : undefined
    if (byName !== undefined) return byName
    return lastRelay
  }

  for await (const event of events) {
    switch (event.type) {
      case 'start':
        break
      case 'text_start':
        yield { type: 'block-start', index: event.contentIndex, blockType: 'text' }
        break
      case 'text_delta':
        yield { type: 'text-delta', index: event.contentIndex, text: event.delta }
        break
      case 'text_end':
        yield { type: 'block-end', index: event.contentIndex, block: { type: 'text', text: event.content } }
        break
      case 'thinking_start':
        yield { type: 'block-start', index: event.contentIndex, blockType: 'reasoning' }
        break
      case 'thinking_delta':
        yield { type: 'reasoning-delta', index: event.contentIndex, text: event.delta }
        break
      case 'thinking_end':
        yield { type: 'block-end', index: event.contentIndex, block: { type: 'reasoning', text: event.content } }
        break
      case 'toolcall_start': {
        // The id/name live on the partial's content at this index.
        const partial = event.partial.content[event.contentIndex]
        // Relay-rendered calls never reach the harness as tool-call blocks:
        // record the call (id/name) for the synthetic reasoning tombstone.
        if ((partial as { pcaiRelay?: boolean } | undefined)?.pcaiRelay === true) {
          const call: RelayCall = {
            id: partial?.type === 'toolCall' ? partial.id : '',
            name: partial?.type === 'toolCall' ? partial.name : '',
            args: '',
          }
          relayCalls.set(event.contentIndex, call)
          if (call.id.length > 0) relayById.set(call.id, call)
          if (call.name.length > 0) relayByName.set(call.name, call)
          lastRelay = call
          break
        }
        const id = partial?.type === 'toolCall' ? partial.id : ''
        const name = partial?.type === 'toolCall' ? partial.name : ''
        toolIds.set(event.contentIndex, { id, name })
        yield { type: 'block-start', index: event.contentIndex, blockType: 'tool-call' }
        break
      }
      case 'toolcall_delta': {
        // Relay args stream in after toolcall_start; accumulate the raw
        // fragments so the tombstone carries the complete argument JSON.
        const relayCall = relayCalls.get(event.contentIndex)
        if (relayCall !== undefined) {
          relayCall.args += event.delta
          break
        }
        const known = toolIds.get(event.contentIndex)
        yield {
          type: 'tool-call-delta',
          index: event.contentIndex,
          id: brandString<ToolCallId>(known?.id ?? ''),
          ...known?.name !== undefined && known.name.length > 0 ? { name: known.name } : {},
          argumentsDelta: event.delta,
        }
        break
      }
      case 'toolcall_end':
        // r3: the provider emits relay toolcall_end at the call's telemetry
        // event (mid-stream position) — suppress the harness block (display-
        // only). The tombstone renders from the pcai_event that immediately
        // follows (it carries the outcome); done flushes calls whose event
        // never arrived.
        if (relayCalls.has(event.contentIndex)) break
        yield {
          type: 'block-end',
          index: event.contentIndex,
          block: {
            type: 'tool-call',
            id: brandString<ToolCallId>(event.toolCall.id),
            name: event.toolCall.name,
            // pi-ai hands back the PARSED arguments; the harness vocabulary
            // keeps the raw string.
            arguments: JSON.stringify(event.toolCall.arguments),
          },
        }
        break
      case 'done':
        // Relay calls whose telemetry event never arrived still executed
        // server-side; close their tombstones with the honest outcome.
        for (const call of relayCalls.values()) {
          if (call.pcai === undefined) yield* renderTombstone(call)
        }
        yield { type: 'usage', usage: mapUsage(event.message.usage) }
        {
          let reason = mapStopReason(event.message, contextWindow)
          // Relay-only turn guard (r2, kept): pi-ai strips stamped blocks from
          // the message content, so a stream that carried ONLY relay-rendered
          // calls has no content blocks at 'done' and would mis-map to
          // EMPTY_RESPONSE. The tombstones above are the turn's real output;
          // report a plain stop.
          if (sawRelayRender && reason.kind === 'error' && reason.failure.code === EMPTY_RESPONSE_CODE) {
            reason = { kind: 'stop' }
          }
          yield {
            type: 'finish',
            reason,
            replayState: toPiReplayState(event.message, requestedModel),
          }
        }
        return
      case 'error':
        // In-stream error delivery (pi-ai's style) → error finish chunk
        // (the harness's other sanctioned error path besides throwing).
        yield { type: 'usage', usage: mapUsage(event.error.usage) }
        yield {
          type: 'finish',
          reason: mapStopReason(
            callerSignal?.aborted ? { ...event.error, stopReason: 'aborted' } : event.error,
            contextWindow,
          ),
        }
        return
      default: {
        // The patched pi-ai provider forwards the gateway's standalone
        // `{"pcai": …}` SSE objects as `pcai_event`s — outside pi-ai's declared
        // AssistantMessageEvent union, so this arm is reached via a cast. The
        // tool_call/tool_error outcome completes the relay call's tombstone;
        // every other kind (`thinking`, `usage`, guards, handbacks) is
        // observed and ignored — the dist provider itself consumes the
        // `thinking` iteration for the cycle breaks.
        const pcai = (event as { pcai?: PcaiEventPayload }).pcai
        if ((event as { type?: string }).type !== 'pcai_event' || pcai === undefined) break
        if (pcai.event === 'tool_call' || pcai.event === 'tool_error') {
          const call = relayCallFor(pcai)
          if (call !== undefined) {
            call.pcai = {
              ok: pcai.event === 'tool_call' && pcai.ok === true,
              ...typeof pcai.ms === 'number' ? { ms: pcai.ms } : {},
              ...typeof pcai.result === 'string' ? { result: pcai.result } : {},
            }
            yield* renderTombstone(call)
          }
        }
        break
      }
    }
  }
  throw new LlmError('pi-ai event stream ended without done/error', 'STREAM_CLOSED')
}
