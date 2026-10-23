/**
 * Wave R1 (r2): apply the relay-render mapping to packages/llm/llm-pi-ai/lib/index.js —
 * the compiled form of the src/stream.ts changes (the runtime loads lib/index.js; the
 * repo build would regenerate this, but the repo is read-only here).
 * Anchored inserts; fails loudly if the file changed underneath.
 * Usage: node apply-lib-edits.mjs <pristine-lib-index.js> <out.js>
 */
import { readFileSync, writeFileSync } from 'node:fs'

const [, , srcPath, outPath] = process.argv
if (srcPath === undefined || outPath === undefined) {
  console.error('usage: node apply-lib-edits.mjs <pristine-lib-index.js> <out.js>')
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

const T = '\t'

// Edit L1 — preamble after the toolIds map.
const LIB_PREAMBLE = [
  `${T}// PCAI gateway relay (r3): the gateway executes MCP tools in-cluster inside`,
  `${T}// this same SSE stream. Its tool calls arrive as stamped inert deltas`,
  `${T}// (\`pcai.relay: true\`, display-only) that the patched provider emits as`,
  `${T}// ordinary toolcall_start/delta/end events — but they must NEVER reach the`,
  `${T}// harness as tool-call blocks: the agent loop executes every tool-call`,
  `${T}// block in the assembled message, and a relay call has no client-side`,
  `${T}// result (the gateway already ran it). r3 renders each relay call as a`,
  `${T}// SYNTHETIC REASONING block — one per call, opened and closed at the`,
  `${T}// call's telemetry event stream position (the provider now emits relay`,
  `${T}// toolcall_end there) — carrying the call name, full streamed args, and`,
  `${T}// the gateway's result preview (≤4000 chars).`,
  `${T}const relayCalls = /* @__PURE__ */ new Map();`,
  `${T}const relayById = /* @__PURE__ */ new Map();`,
  `${T}const relayByName = /* @__PURE__ */ new Map();`,
  `${T}let lastRelay;`,
  `${T}let sawRelayRender = false;`,
  `${T}let nextRelayIndex = 1e6;`,
  `${T}const collapseWhitespace = (text, cap) => {`,
  `${T}\tconst flat = text.replace(/\\s+/gu, " ").trim();`,
  `${T}\treturn flat.length > cap ? \`\${flat.slice(0, cap)}…\` : flat;`,
  `${T}};`,
  `${T}function* renderTombstone(call) {`,
  `${T}\tconst index = nextRelayIndex;`,
  `${T}\tnextRelayIndex += 1;`,
  `${T}\tsawRelayRender = true;`,
  `${T}\tyield { type: "block-start", index, blockType: "reasoning" };`,
  `${T}\tlet text = "";`,
  `${T}\tconst push = (chunk) => {`,
  `${T}\t\ttext += chunk;`,
  `${T}\t\treturn { type: "reasoning-delta", index, text: chunk };`,
  `${T}\t};`,
  `${T}\tconst argsPart = call.args.length > 0 ? collapseWhitespace(call.args, 2000) : "";`,
  `${T}\tyield push(\`\\n[tool] \${call.name}(\${argsPart}) \`);`,
  `${T}\tif (call.pcai === undefined) {`,
  `${T}\t\tyield push("\\n→ executed server-side\\n");`,
  `${T}\t} else {`,
  `${T}\t\tconst ms = typeof call.pcai.ms === "number" ? \` (\${call.pcai.ms}ms)\` : "";`,
  `${T}\t\tyield push(\`\\n→ \${call.pcai.ok ? "ok" : "error"}\${ms}\\n\`);`,
  `${T}\t\tif (typeof call.pcai.result === "string" && call.pcai.result.length > 0) {`,
  `${T}\t\t\tyield push(collapseWhitespace(call.pcai.result, 4000) + "\\n");`,
  `${T}\t\t}`,
  `${T}\t}`,
  `${T}\tyield { type: "block-end", index, block: { type: "reasoning", text } };`,
  `${T}}`,
  `${T}function relayCallFor(pcai) {`,
  `${T}\tconst byId = typeof pcai.id === "string" ? relayById.get(pcai.id) : undefined;`,
  `${T}\tif (byId !== undefined) return byId;`,
  `${T}\tconst byName = typeof pcai.tool === "string" ? relayByName.get(pcai.tool) : undefined;`,
  `${T}\tif (byName !== undefined) return byName;`,
  `${T}\treturn lastRelay;`,
  `${T}}`,
].join('\n') + '\n'
insertBefore(
  `${T}for await (const event of events) switch (event.type) {\n${T}\tcase "start": break;\n`,
  LIB_PREAMBLE,
  'lib-preamble',
)


// Edit L2 — toolcall_start: capture relay calls for the tombstone instead of
// emitting a harness block-start.
replaceOnce(
  `${T}	case "toolcall_start": {
${T}		const partial = event.partial.content[event.contentIndex];
${T}		const id = partial?.type === "toolCall" ? partial.id : "";\n`,
  `${T}	case "toolcall_start": {
`
    + `${T}		const partial = event.partial.content[event.contentIndex];
`
    + `${T}		if (partial?.pcaiRelay === true) {
`
    + `${T}			const call = {
`
    + `${T}				id: partial?.type === "toolCall" ? partial.id : "",
`
    + `${T}				name: partial?.type === "toolCall" ? partial.name : "",
`
    + `${T}\t\t\t\targs: ""\n`
    + `${T}			};
`
    + `${T}			relayCalls.set(event.contentIndex, call);
`
    + `${T}			if (call.id.length > 0) relayById.set(call.id, call);
`
    + `${T}			if (call.name.length > 0) relayByName.set(call.name, call);
`
    + `${T}			lastRelay = call;
`
    + `${T}			break;
`
    + `${T}		}
`
    + `${T}		const id = partial?.type === "toolCall" ? partial.id : "";\n`,
  'lib-toolcall_start',
)

// Edit L3 — toolcall_delta: no harness deltas for relay blocks.
replaceOnce(
  `${T}	case "toolcall_delta": {
${T}		const known = toolIds.get(event.contentIndex);`,
  `${T}	case "toolcall_delta": {\n`
    + `${T}		const relayCall = relayCalls.get(event.contentIndex);\n`
    + `${T}		if (relayCall !== undefined) {\n`
    + `${T}			relayCall.args += event.delta;\n`
    + `${T}			break;\n`
    + `${T}		}\n`

    + `${T}		const known = toolIds.get(event.contentIndex);`,
  'lib-toolcall_delta',
)

// Edit L4 — toolcall_end: no harness block-end for relay blocks.
replaceOnce(
  `${T}	case "toolcall_end":
${T}		yield {
${T}			type: "block-end",`,
  `${T}	case "toolcall_end":
${T}		if (relayCalls.has(event.contentIndex)) break; // display-only: no harness block-end
${T}		yield {
${T}			type: "block-end",`,
  'lib-toolcall_end',
)

// Edit L5 — done: flush tombstones, EMPTY_RESPONSE guard. Replacement ends
// where the pristine `replayState:` tail continues (kept as-is), so the case's
// existing finish/return survive; the brace close happens in L5b.
replaceOnce(
  "\t\tcase \"done\":\n\t\t\tyield {\n\t\t\t\ttype: \"usage\",\n\t\t\t\tusage: mapUsage(event.message.usage)\n\t\t\t};\n\t\t\tyield {\n\t\t\t\ttype: \"finish\",\n\t\t\t\treason: mapStopReason(event.message, contextWindow),",
  "\t\tcase \"done\": {\n"
    + "\t\t\tfor (const call of relayCalls.values()) {\n"
    + "\t\t\t\tif (call.pcai === undefined) yield* renderTombstone(call);\n"
    + "\t\t\t}\n"
    + "\t\t\tyield {\n\t\t\t\ttype: \"usage\",\n\t\t\t\tusage: mapUsage(event.message.usage)\n\t\t\t};\n"
    + "\t\t\tlet reason = mapStopReason(event.message, contextWindow);\n"
    + "\t\t\tif (sawRelayRender && reason.kind === \"error\" && reason.failure.code === \"EMPTY_RESPONSE\") {\n"
    + "\t\t\t\treason = { kind: \"stop\" };\n"
    + "\t\t\t}\n"
    + "\t\t\tyield {\n\t\t\t\ttype: \"finish\",\n\t\t\t\treason,",
  'lib-done',
)

// Edit L5b — close the done-case brace after its existing `return;`.
replaceOnce(
  "\t\t\treplayState: toPiReplayState(event.message, requestedModel)\n\t\t\t};\n\t\t\treturn;\n\t\tcase \"error\":",
  "\t\t\treplayState: toPiReplayState(event.message, requestedModel)\n\t\t\t};\n\t\t\treturn;\n\t\t}\n\t\tcase \"error\":",
  'lib-done-brace',
)

// Edit L6 — default arm before the error case: join pcai_events.
replaceOnce(
  `${T}	case "error":
${T}		yield {
${T}			type: "usage",
${T}			usage: mapUsage(event.error.usage)`,
  `${T}	default: {
`
    + `${T}		const pcai = event.pcai;
`
    + `${T}		if (event.type !== "pcai_event" || pcai === undefined) break;
`
    + `${T}		if (pcai.event === "tool_call" || pcai.event === "tool_error") {
`
    + `${T}			const call = relayCallFor(pcai);
`
    + `${T}			if (call !== undefined) {
`
    + `${T}				call.pcai = {
`
    + `${T}					ok: pcai.event === "tool_call" && pcai.ok === true,
`
    + `${T}					...typeof pcai.ms === "number" ? { ms: pcai.ms } : {},
`
    + `${T}					...typeof pcai.result === "string" ? { result: pcai.result } : {}
`
    + `${T}				};
`
    + `${T}				yield* renderTombstone(call);
`
    + `${T}			}
`
    + `${T}		}
`
    + `${T}		break;
`
    + `${T}	}
`
    + `${T}	case "error":
${T}		yield {
${T}			type: "usage",
${T}			usage: mapUsage(event.error.usage)`,
  'lib-default-arm',
)

writeFileSync(outPath, text)
console.log('lib edits applied →', outPath)
