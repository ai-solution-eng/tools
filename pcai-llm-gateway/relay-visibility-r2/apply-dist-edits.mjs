/**
 * Wave R1 (r3): apply the PCAI relay-render edits to pi-ai's dist/api/openai-completions.js.
 * Each insert is anchored on its exact surrounding text; fails loudly if the file
 * changed underneath or the edit is already present.
 * Usage: node apply-dist-edits.mjs <pristine.js> <out.js>
 *
 * r3 additions over r2 (cycle breaks — DSH 2026-10-02 stale-panel report): the
 * relay numbers its in-stream model cycles on every `pcai.thinking` event.
 * pi-ai accumulates ALL thinking into ONE block and ALL content into ONE text
 * block for the whole stream (single `thinkingBlock`/`textBlock` accumulators,
 * never reset), so a relayed multi-cycle turn renders one ever-growing Think
 * panel at the top of the message while the visible text below is already-final
 * narration. On a NEW cycle number we finishBlock() the open thinking/text
 * blocks mid-stream (thinking_end/text_end at the CURRENT indexes) and clear
 * the accumulators, so the next cycle's deltas open FRESH blocks at their true
 * stream position. Relay tool telemetry also finalizes the display-only
 * toolCall block at its stream position (r2 only closed cards in the
 * end-of-stream finishBlock loop, so a card could not complete before the next
 * cycle's thinking began).
 */
import { readFileSync, writeFileSync } from 'node:fs'

const [, , srcPath, outPath] = process.argv
if (srcPath === undefined || outPath === undefined) {
  console.error('usage: node apply-dist-edits.mjs <pristine.js> <out.js>')
  process.exit(1)
}
let text = readFileSync(srcPath, 'utf8')

function insertBefore(anchor, addition, label) {
  if (text.includes(addition.trimEnd())) throw new Error(`${label}: already applied`)
  const at = text.indexOf(anchor)
  if (at === -1) throw new Error(`${label}: anchor not found`)
  text = text.slice(0, at) + addition + text.slice(at)
}

function replaceOnce(oldText, newText, label) {
  if (text.includes(newText.trimEnd())) throw new Error(`${label}: already applied`)
  const at = text.indexOf(oldText)
  if (at === -1) throw new Error(`${label}: anchor not found`)
  text = text.slice(0, at) + newText + text.slice(at + oldText.length)
}

// Edit D0 — stream-scoped cycle/bookkeeping state next to the block maps.
insertBefore(
  '            const toolCallBlocksByIndex = new Map();\n',
  '            let pcaiLastIteration;\n'
    + '            let lastRelayBlock;\n',
  'pcai-cycle-state',
)

// Edit 1 — standalone {"pcai":…} SSE objects (no choices): forward as a dedicated
// pi-ai event instead of dropping them. r3: the same hook carries the cycle
// break (new thinking iteration) and the mid-stream relay toolCall finish.
insertBefore(
  '                const choice = Array.isArray(chunk.choices) ? chunk.choices[0] : undefined;\n',
  '                if (chunk.pcai && !Array.isArray(chunk.choices)) {\n'
    + '                    // PCAI relay cycle-break (r3): the relay numbers its in-stream\n'
    + '                    // model cycles on every thinking telemetry event. A NEW cycle\n'
    + '                    // number closes pi-ai\'s single accumulated thinking/text blocks\n'
    + '                    // (thinking_end/text_end at the CURRENT indexes) and clears the\n'
    + '                    // block pointers, so the following cycle\'s reasoning/content\n'
    + '                    // deltas open FRESH blocks at their true stream position —\n'
    + '                    // per-cycle Think panels instead of one ever-growing panel\n'
    + '                    // that fills while the answer text below it is already final.\n'
    + '                    if (chunk.pcai.event === "thinking" && typeof chunk.pcai.iteration === "number") {\n'
    + '                        const it = chunk.pcai.iteration;\n'
    + '                        if (typeof pcaiLastIteration === "number" && it > pcaiLastIteration) {\n'
    + '                            if (thinkingBlock !== null) {\n'
    + '                                finishBlock(thinkingBlock);\n'
    + '                                thinkingBlock = null;\n'
    + '                            }\n'
    + '                            if (textBlock !== null) {\n'
    + '                                finishBlock(textBlock);\n'
    + '                                textBlock = null;\n'
    + '                            }\n'
    + '                        }\n'
    + '                        pcaiLastIteration = it;\n'
    + '                    }\n'
    + '                    if (chunk.pcai.event === "tool_call" || chunk.pcai.event === "tool_error") {\n'
    + '                        // The call\'s argument deltas are complete by the time its\n'
    + '                        // telemetry event arrives — finalize the display-only block\n'
    + '                        // AT ITS STREAM POSITION (toolcall_end, original index), so\n'
    + '                        // the rendering client sees the completed card between the\n'
    + '                        // cycles instead of every card closing at stream end.\n'
    + '                        const relayBlock =\n'
    + '                            (typeof chunk.pcai.id === "string" ? toolCallBlocksById.get(chunk.pcai.id) : undefined) || lastRelayBlock;\n'
    + '                        if (relayBlock !== undefined && relayBlock.pcaiRelay === true) {\n'
    + '                            finishBlock(relayBlock);\n'
    + '                        }\n'
    + '                    }\n'
    + '                    // PCAI gateway relay (Wave R1 contract): standalone {"pcai": …}\n'
    + '                    // SSE data objects carry no choices — surface them as a dedicated\n'
    + '                    // event instead of dropping them, so the harness adapter can join\n'
    + '                    // relay-executed tool calls with their result previews.\n'
    + '                    stream.push({ type: "pcai_event", pcai: chunk.pcai, partial: output });\n'
    + '                    continue;\n'
    + '                }\n',
  'pcai-event-forward',
)

// Edit 2 — stamped relay deltas: mark the tool-call block assembled from a chunk
// carrying the top-level pcai.relay stamp. Runs AFTER ensureToolCallBlock has
// resolved/created the block (the delta handler may see only argument fragments
// for a call whose header chunk carried no pcai stamp, so the flag is additive).
// r3: remember the block so a telemetry event whose id lookup misses (rare
// pre-stamped argument-only chunks) can still finalize it mid-stream.
insertBefore(
  '                            let delta = "";\n'
    + '                            if (toolCall.function?.arguments) {\n',
  '                            if (chunk.pcai && chunk.pcai.relay === true) {\n'
    + '                                // PCAI relay render delta (Wave R1): the gateway\n'
    + '                                // already executed this call in-cluster. Mark the\n'
    + '                                // block display-only; the assembled message must\n'
    + '                                // never carry it as an executable tool_call (no\n'
    + '                                // replay pairing exists client-side).\n'
    + '                                block.pcaiRelay = true;\n'
    + '                                lastRelayBlock = block;\n'
    + '                            }\n',
  'pcaiRelay-mark',
)

// Edit D6 — finishBlock must finalize each relay toolCall block EXACTLY ONCE:
// the pcai tool_call/tool_error hook (Edit 1) finalizes it at its stream
// position; the end-of-stream finishBlock loop then skips it (a second
// toolcall_end after the cycle break would duplicate the card). A relay call
// whose telemetry never arrived is finalized by the end-of-stream loop — its
// tombstone is the adapter's done-flush "executed server-side" line.
replaceOnce(
  '            const finishBlock = (block) => {\n'
    + '                const contentIndex = getContentIndex(block);\n'
    + '                if (contentIndex === -1) {\n'
    + '                    return;\n'
    + '                }\n',
  '            const finishBlock = (block) => {\n'
    + '                // PCAI relay render block (r3): finalize once — the pcai\n'
    + '                // telemetry hook (mid-stream) or the end-of-stream loop below,\n'
    + '                // never both.\n'
    + '                if (block.type === "toolCall" && block.pcaiRelay === true) {\n'
    + '                    if (block.pcaiFinished === true) {\n'
    + '                        return;\n'
    + '                    }\n'
    + '                    block.pcaiFinished = true;\n'
    + '                }\n'
    + '                const contentIndex = getContentIndex(block);\n'
    + '                if (contentIndex === -1) {\n'
    + '                    return;\n'
    + '                }\n',
  'finishBlock-relay-guard',
)

// Edit 3 — final message assembly: strip pcaiRelay blocks AFTER the finishBlock
// loop ran (post toolcall_end emission), keeping pi-ai's contentIndex space
// stable across the whole event stream.
insertBefore(
  '            if (options?.signal?.aborted) {\n'
    + '                throw new Error("Request was aborted");\n'
    + '            }\n',
  '            // PCAI relay render blocks (Wave R1): display-only — remove them\n'
    + '            // from the message only after finishBlock has emitted every\n'
    + '            // toolcall_end against the original content indexes, so no\n'
    + "            // following block's index shifts.\n"
    + '            output.content = output.content.filter((block) => !(block.type === "toolCall" && block.pcaiRelay));\n',
  'relay-strip',
)

writeFileSync(outPath, text)
console.log('applied 5 edits →', outPath)
