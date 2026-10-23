/**
 * PCAI gateway relay rendering (r3: per-call tombstone blocks + provider-side
 * cycle breaks).
 *
 * The gateway executes MCP tools in-cluster inside one chat-completions SSE
 * stream: its tool calls arrive as stamped inert tool-call deltas (`pcai.relay`,
 * display-only, indexes offset from 1000 so they never merge with handback
 * deltas) plus standalone `{"pcai": …}` telemetry objects that pi-ai's patched
 * provider forwards as `pcai_event`s. The provider now also emits relay
 * `toolcall_end` AT the telemetry event (stream position) and re-opens fresh
 * thinking/text blocks on every new `pcai.thinking` iteration — the cycle
 * breaks. These tests pin the harness-facing half: relay calls are suppressed
 * as tool-call blocks (the agent loop executes every tool-call block in the
 * assembled message, so none may ever reach it), each renders as its OWN
 * synthetic reasoning tombstone — opened and closed at the call's stream
 * position, carrying full streamed args (≤2000 chars) and the gateway's result
 * preview (≤4000 chars) — unstamped calls are untouched, and a relay-only turn
 * finishes `stop` rather than `EMPTY_RESPONSE`.
 *
 * @module dsh-llm-pi-ai/stream-relay
 */

import { describe, expect, it } from 'vitest'
import { EMPTY_RESPONSE_CODE } from '@deepseek-ai/dsh-llm'
import type { ContentBlock, StreamChunk } from '@deepseek-ai/dsh-llm'
import type { AssistantMessage, AssistantMessageEvent, Usage } from '@earendil-works/pi-ai'
import { toStreamChunks } from '../src/stream.ts'

function usage(input = 0, output = 0): Usage {
  return {
    input,
    output,
    cacheRead: 0,
    cacheWrite: 0,
    totalTokens: input + output,
    cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
  }
}

function assistant(overrides: Partial<AssistantMessage> = {}): AssistantMessage {
  return {
    role: 'assistant',
    content: [],
    api: 'openai-completions',
    provider: 'pcai',
    model: 'pcai-model',
    usage: usage(),
    stopReason: 'stop',
    timestamp: 0,
    ...overrides,
  }
}

async function* feed(...events: AssistantMessageEvent[]): AsyncGenerator<AssistantMessageEvent> {
  for (const event of events) yield event
}

async function collect(stream: AsyncIterable<StreamChunk>): Promise<StreamChunk[]> {
  const out: StreamChunk[] = []
  for await (const chunk of stream) out.push(chunk)
  return out
}

/** The pi-ai `pcai_event` the patched provider pushes for one telemetry object. */
function pcaiEvent(pcai: Record<string, unknown>, partial: AssistantMessage): AssistantMessageEvent {
  return { type: 'pcai_event', pcai, partial } as unknown as AssistantMessageEvent
}

/** A pi-ai toolcall partial whose block carries the relay stamp from the dist patch. */
function relayPartial(index: number, id: string, name: string, args: Record<string, unknown>): AssistantMessage {
  return assistant({
    content: [{ type: 'toolCall', id, name, arguments: args, pcaiRelay: true } as AssistantMessage['content'][number] & { pcaiRelay: boolean }],
  })
}

const RELAY_BLOCK: StreamChunk = { type: 'block-start', index: 1_000_000, blockType: 'reasoning' }

describe('pcai relay tombstones (toStreamChunks)', () => {
  it('renders one closed tombstone block per relay call at its stream position', async () => {
    const relayPartialMsg = relayPartial(0, 'c1', 'mm-rag__search_dataset', { dataset_name: 'logs' })
    const chunks = await collect(toStreamChunks(feed(
      { type: 'toolcall_start', contentIndex: 0, partial: relayPartialMsg },
      { type: 'toolcall_delta', contentIndex: 0, delta: '{"dataset_name":"logs"}', partial: relayPartialMsg },
      {
        type: 'toolcall_end',
        contentIndex: 0,
        toolCall: { type: 'toolCall', id: 'c1', name: 'mm-rag__search_dataset', arguments: { dataset_name: 'logs' } },
        partial: relayPartialMsg,
      },
      pcaiEvent({ event: 'tool_call', id: 'c1', tool: 'mm-rag__search_dataset', ok: true, ms: 2450, result: '10 hits · top score 0.91' }, relayPartialMsg),
      { type: 'done', reason: 'stop', message: assistant({ usage: usage(3, 2) }) },
    )))
    // No tool-call block of any kind reaches the harness output.
    expect(chunks.filter(c => c.type === 'block-start' && c.blockType === 'tool-call')).toEqual([])
    expect(chunks.filter(c => c.type === 'tool-call-delta')).toEqual([])
    expect(chunks.filter(c => c.type === 'block-end' && (c.block as ContentBlock).type === 'tool-call')).toEqual([])
    // One synthetic reasoning block hosts the full tombstone, opened and
    // closed at the telemetry event's stream position.
    const header = '\n[tool] mm-rag__search_dataset({"dataset_name":"logs"}) '
    const outcome = '\n→ ok (2450ms)\n'
    const preview = '10 hits · top score 0.91\n'
    expect(chunks.slice(0, 6)).toEqual([
      RELAY_BLOCK,
      { type: 'reasoning-delta', index: 1_000_000, text: header },
      { type: 'reasoning-delta', index: 1_000_000, text: outcome },
      { type: 'reasoning-delta', index: 1_000_000, text: preview },
      { type: 'block-end', index: 1_000_000, block: { type: 'reasoning', text: header + outcome + preview } },
      { type: 'usage', usage: { inputTokens: 3, outputTokens: 2, totalTokens: 5 } },
    ])
    expect(chunks.at(-1)).toMatchObject({ type: 'finish', reason: { kind: 'stop' } })
  })

  it('renders the error variant from a tool_error event', async () => {
    const relayPartialMsg = relayPartial(0, 'c2', 'web_fetch', { url: 'http://x' })
    const chunks = await collect(toStreamChunks(feed(
      { type: 'toolcall_start', contentIndex: 0, partial: relayPartialMsg },
      pcaiEvent({ event: 'tool_error', id: 'c2', tool: 'web_fetch', detail: 'upstream 503', ms: 80 }, relayPartialMsg),
      { type: 'done', reason: 'stop', message: assistant() },
    )))
    expect(chunks.slice(0, 4)).toEqual([
      RELAY_BLOCK,
      // No toolcall_delta was fed, so the streamed-args accumulator is empty.
      { type: 'reasoning-delta', index: 1_000_000, text: '\n[tool] web_fetch() ' },
      { type: 'reasoning-delta', index: 1_000_000, text: '\n→ error (80ms)\n' },
      { type: 'block-end', index: 1_000_000, block: { type: 'reasoning', text: '\n[tool] web_fetch() \n→ error (80ms)\n' } },
    ])
  })

  it('falls back to the tool name when the event carries no call id, and to the last relay call otherwise', async () => {
    const relayPartialMsg = relayPartial(0, '', 'db_query', {})
    const chunks = await collect(toStreamChunks(feed(
      { type: 'toolcall_start', contentIndex: 0, partial: relayPartialMsg },
      pcaiEvent({ event: 'tool_call', tool: 'db_query', ok: true, ms: 12, result: 'rows: 3' }, relayPartialMsg),
      { type: 'done', reason: 'stop', message: assistant() },
    )))
    expect(chunks[1]).toMatchObject({ text: '\n[tool] db_query() ' })
    expect(chunks[2]).toEqual({ type: 'reasoning-delta', index: 1_000_000, text: '\n→ ok (12ms)\n' })
    expect(chunks[3]).toEqual({ type: 'reasoning-delta', index: 1_000_000, text: 'rows: 3\n' })
    expect(chunks[4]).toMatchObject({ type: 'block-end', index: 1_000_000 })
  })

  it('closes a telemetry-less relay call with "executed server-side" at done', async () => {
    const relayPartialMsg = relayPartial(0, 'c3', 'slow_tool', {})
    const chunks = await collect(toStreamChunks(feed(
      { type: 'toolcall_start', contentIndex: 0, partial: relayPartialMsg },
      { type: 'done', reason: 'stop', message: assistant() },
    )))
    expect(chunks.slice(0, 4)).toEqual([
      RELAY_BLOCK,
      { type: 'reasoning-delta', index: 1_000_000, text: '\n[tool] slow_tool() ' },
      { type: 'reasoning-delta', index: 1_000_000, text: '\n→ executed server-side\n' },
      { type: 'block-end', index: 1_000_000, block: { type: 'reasoning', text: '\n[tool] slow_tool() \n→ executed server-side\n' } },
    ])
  })

  it('gives each relay call its own tombstone block in stream order', async () => {
    const both = assistant({
      content: [
        { type: 'toolCall', id: 'r1', name: 'tool_a', arguments: {}, pcaiRelay: true },
        { type: 'toolCall', id: 'r2', name: 'tool_b', arguments: {}, pcaiRelay: true },
      ] as AssistantMessage['content'][number][],
    })
    const chunks = await collect(toStreamChunks(feed(
      { type: 'toolcall_start', contentIndex: 0, partial: both },
      { type: 'toolcall_start', contentIndex: 1, partial: both },
      pcaiEvent({ event: 'tool_call', id: 'r1', ok: true, ms: 5, result: 'A' }, both),
      pcaiEvent({ event: 'tool_call', id: 'r2', ok: false, ms: 9, result: 'B' }, both),
      { type: 'done', reason: 'stop', message: assistant() },
    )))
    const starts = chunks.filter(c => c.type === 'block-start' && typeof c.index === 'number' && c.index >= 1_000_000)
    const ends = chunks.filter(c => c.type === 'block-end' && typeof c.index === 'number' && c.index >= 1_000_000)
    expect(starts).toEqual([
      RELAY_BLOCK,
      { type: 'block-start', index: 1_000_001, blockType: 'reasoning' },
    ])
    expect(ends.map(c => c.index)).toEqual([1_000_000, 1_000_001])
    // Each tombstone joined to its own outcome by call id.
    expect(chunks.filter(c => c.type === 'reasoning-delta' && c.index === 1_000_000)).toEqual([
      { type: 'reasoning-delta', index: 1_000_000, text: '\n[tool] tool_a() ' },
      { type: 'reasoning-delta', index: 1_000_000, text: '\n→ ok (5ms)\n' },
      { type: 'reasoning-delta', index: 1_000_000, text: 'A\n' },
    ])
    expect(chunks.filter(c => c.type === 'reasoning-delta' && c.index === 1_000_001)).toEqual([
      { type: 'reasoning-delta', index: 1_000_001, text: '\n[tool] tool_b() ' },
      { type: 'reasoning-delta', index: 1_000_001, text: '\n→ error (9ms)\n' },
      { type: 'reasoning-delta', index: 1_000_001, text: 'B\n' },
    ])
  })

  it('renders exactly one tombstone even when the stream carries duplicate relay toolcall_ends', async () => {
    const relayPartialMsg = relayPartial(0, 'c7', 'dup_tool', {})
    const chunks = await collect(toStreamChunks(feed(
      { type: 'toolcall_start', contentIndex: 0, partial: relayPartialMsg },
      {
        type: 'toolcall_end',
        contentIndex: 0,
        toolCall: { type: 'toolCall', id: 'c7', name: 'dup_tool', arguments: {} },
        partial: relayPartialMsg,
      },
      {
        type: 'toolcall_end',
        contentIndex: 0,
        toolCall: { type: 'toolCall', id: 'c7', name: 'dup_tool', arguments: {} },
        partial: relayPartialMsg,
      },
      pcaiEvent({ event: 'tool_call', id: 'c7', ok: true, ms: 4 }, relayPartialMsg),
      { type: 'done', reason: 'stop', message: assistant() },
    )))
    // The relay toolcall_end arm is pure suppression; only the telemetry
    // event renders, exactly once.
    expect(chunks.filter(c => c.type === 'block-start' && typeof c.index === 'number' && c.index >= 1_000_000)).toEqual([RELAY_BLOCK])
    expect(chunks.filter(c => c.type === 'block-end' && typeof c.index === 'number' && c.index >= 1_000_000)).toHaveLength(1)
  })

  it('collapses whitespace and caps args at ~2000 and previews at ~4000 chars', async () => {
    const args = JSON.stringify({ q: 'x'.repeat(600) })
    const relayPartialMsg = relayPartial(0, 'c4', 'search', JSON.parse(args))
    const preview = 'r'.repeat(500)
    const chunks = await collect(toStreamChunks(feed(
      { type: 'toolcall_start', contentIndex: 0, partial: relayPartialMsg },
      { type: 'toolcall_delta', contentIndex: 0, delta: args, partial: relayPartialMsg },
      pcaiEvent({ event: 'tool_call', id: 'c4', ok: true, ms: 5, result: `a\n  b\t${preview}` }, relayPartialMsg),
      { type: 'done', reason: 'stop', message: assistant() },
    )))
    const argsLine = chunks[1].type === 'reasoning-delta' ? chunks[1].text : ''
    expect(argsLine.startsWith('\n[tool] search(')).toBe(true)
    // The 600-char arg now fits UNCAP (r3 raised the 200-char tombstone cap
    // to 2000): collapsed JSON has no whitespace to remove.
    expect(argsLine).toBe(`\n[tool] search(${"{\"q\":\"" + 'x'.repeat(600) + "\"}"}) `)
    const resultLine = chunks[3]
    expect(resultLine?.type === 'reasoning-delta' && resultLine.text.startsWith('a b r')).toBe(true)
    expect(resultLine?.type === 'reasoning-delta' && resultLine.text.length).toBeLessThanOrEqual(4002)
  })

  it('keeps unstamped tool calls byte-identical to the unpatched mapping', async () => {
    const handbackPartial = assistant({ content: [{ type: 'toolCall', id: 'hb1', name: 'write_file', arguments: {} }] })
    const chunks = await collect(toStreamChunks(feed(
      { type: 'toolcall_start', contentIndex: 0, partial: handbackPartial },
      { type: 'toolcall_delta', contentIndex: 0, delta: '{"path":"a.md"}', partial: handbackPartial },
      {
        type: 'toolcall_end',
        contentIndex: 0,
        toolCall: { type: 'toolCall', id: 'hb1', name: 'write_file', arguments: { path: 'a.md' } },
        partial: handbackPartial,
      },
      { type: 'done', reason: 'toolUse', message: assistant({ content: handbackPartial.content, stopReason: 'toolUse' }) },
    )))
    expect(chunks.slice(0, 4)).toEqual([
      { type: 'block-start', index: 0, blockType: 'tool-call' },
      { type: 'tool-call-delta', index: 0, id: 'hb1', name: 'write_file', argumentsDelta: '{"path":"a.md"}' },
      { type: 'block-end', index: 0, block: { type: 'tool-call', id: 'hb1', name: 'write_file', arguments: '{"path":"a.md"}' } },
      { type: 'usage', usage: { inputTokens: 0, outputTokens: 0, totalTokens: 0 } },
    ])
    expect(chunks.at(-1)).toMatchObject({ type: 'finish', reason: { kind: 'tool-calls' } })
  })

  it('maps a relay-only stream to a non-empty reasoning stop (never EMPTY_RESPONSE)', async () => {
    const relayPartialMsg = relayPartial(0, 'c5', 'list_datasets', {})
    const chunks = await collect(toStreamChunks(feed(
      { type: 'toolcall_start', contentIndex: 0, partial: relayPartialMsg },
      pcaiEvent({ event: 'tool_call', id: 'c5', ok: true, ms: 900, result: 'datasets: a, b' }, relayPartialMsg),
      // pi-ai strips the relay block, so 'done' carries zero content blocks —
      // the degenerate shape that would otherwise map to EMPTY_RESPONSE.
      { type: 'done', reason: 'stop', message: assistant() },
    )))
    const reasoning = chunks.filter(c => c.type === 'reasoning-delta')
    expect(reasoning.length).toBeGreaterThan(0)
    expect(chunks.at(-1)).toEqual({
      type: 'finish',
      reason: { kind: 'stop' },
      replayState: {
        response: {
          kind: 'pi-ai', version: 2, api: 'openai-completions', provider: 'pcai',
          model: 'pcai-model', stopReason: 'stop',
        },
        blocks: [],
      },
    })
  })

  it('does not mis-map a genuinely empty stream to stop', async () => {
    const chunks = await collect(toStreamChunks(feed(
      { type: 'done', reason: 'stop', message: assistant() },
    )))
    expect(chunks.at(-1)).toMatchObject({
      type: 'finish',
      reason: { kind: 'error', failure: { code: EMPTY_RESPONSE_CODE } },
    })
  })

  it('ignores unknown pcai_event kinds without crashing', async () => {
    const relayPartialMsg = relayPartial(0, 'c6', 'ok_tool', {})
    const chunks = await collect(toStreamChunks(feed(
      { type: 'toolcall_start', contentIndex: 0, partial: relayPartialMsg },
      pcaiEvent({ event: 'tool_progress', id: 'c6', stage: 'streaming' }, relayPartialMsg),
      pcaiEvent({ event: 'tool_call', id: 'c6', ok: true, ms: 3 }, relayPartialMsg),
      { type: 'done', reason: 'stop', message: assistant() },
    )))
    expect(chunks.slice(0, 4)).toEqual([
      RELAY_BLOCK,
      { type: 'reasoning-delta', index: 1_000_000, text: '\n[tool] ok_tool() ' },
      { type: 'reasoning-delta', index: 1_000_000, text: '\n→ ok (3ms)\n' },
      { type: 'block-end', index: 1_000_000, block: { type: 'reasoning', text: '\n[tool] ok_tool() \n→ ok (3ms)\n' } },
    ])
  })

  it('suppresses a relay block that arrives between handback calls without shifting their indexes', async () => {
    const relayFirst = assistant({
      content: [
        { type: 'toolCall', id: 'rc', name: 'relay_tool', arguments: {}, pcaiRelay: true } as AssistantMessage['content'][number] & { pcaiRelay: boolean },
        { type: 'toolCall', id: 'hb', name: 'handback_tool', arguments: {} },
      ],
    })
    const chunks = await collect(toStreamChunks(feed(
      { type: 'toolcall_start', contentIndex: 0, partial: relayFirst },
      { type: 'toolcall_start', contentIndex: 1, partial: relayFirst },
      {
        type: 'toolcall_end',
        contentIndex: 1,
        toolCall: { type: 'toolCall', id: 'hb', name: 'handback_tool', arguments: { k: 1 } },
        partial: relayFirst,
      },
      pcaiEvent({ event: 'tool_call', id: 'rc', ok: true, ms: 7 }, relayFirst),
      { type: 'done', reason: 'stop', message: relayFirst },
    )))
    // The handback block keeps its stream index 1 and its normal shape; the
    // relay tombstone lives in its own synthetic block AFTER the handback's
    // end (telemetry arrives after the batch's native deltas).
    expect(chunks[0]).toEqual({ type: 'block-start', index: 1, blockType: 'tool-call' })
    expect(chunks[1]).toEqual({
      type: 'block-end',
      index: 1,
      block: { type: 'tool-call', id: 'hb', name: 'handback_tool', arguments: '{"k":1}' },
    })
    expect(chunks[2]).toEqual(RELAY_BLOCK)
    expect(chunks[3]).toMatchObject({ type: 'reasoning-delta', index: 1_000_000, text: '\n[tool] relay_tool() ' })
  })
})
