/**
 * pcai_tool_server.mjs — DSH-side client tool-server plugin (ROADMAP §7
 * phase 2 wave B2, shape (b): long-poll dispatch over the SAME bearer key).
 *
 * WHAT THIS IS
 * ------------
 * The client half of the gateway's client tool-server: it REGISTERs the
 * tools this machine offers (bash/edit), long-polls the gateway's
 * /v1/dispatch/poll, hands each claimed job to a host-provided executor
 * (DSH's own tool runtime — the local approval policy lives THERE, this
 * plugin never executes anything itself), and POSTs the answer back. The
 * relay loop on the gateway side then CONTINUES — no finish_reason:
 * tool_calls handback for an offered tool ever again.
 *
 * Protocol (pcai-llm-gateway routes_dispatch.py):
 *   POST /v1/dispatch/register  { tools: [{name, description, inputSchema, timeout_s?}], ttl_s? }
 *                               -> { client_id, expires_at, tools }
 *   GET  /v1/dispatch/poll?wait_s=<0..60>
 *                               -> { dispatch_id, tool, arguments, dispatched_at, deadline_s } | 204
 *   POST /v1/dispatch/result    { dispatch_id, ok, content|error } -> { settled: true } | 409
 *   GET  /v1/dispatch/offer     -> this key's live offer (introspection)
 *
 * WHY long-poll (not MCP server-initiated requests): the gateway's /mcp is
 * stateless any-replica by fleet decision, and the installed MCP SDK forbids
 * server->client requests in that posture outright (design doc, wave B,
 * shape (a) REJECTED with SDK evidence). The dispatch surface reuses the
 * chat bearer key — one credential, one revocation story.
 *
 * RELIABILITY POSTURE
 * -------------------
 * - dispatch-once: a claimed job is NOT redelivered; if this process dies
 *   after claiming, the job simply times out into a tool error on the relay
 *   side (a duplicate side-effect execution is the worse outcome — never
 *   re-execute a job you are not certain was not already run).
 * - re-register on: (a) poll 404 (offer expired or gateway restarted — the
 *   registry is in-memory by design), (b) TTL expiry proactively. Pending
 *   jobs against a dropped offer are failed server-side, so no orphaned
 *   dispatch can reach a stale registration.
 * - NEVER hot-loop: every failure path sleeps a jittered backoff; success
 *   paths go straight back to polling. One in-flight poll at a time; a stop
 *   flag unwinds the loop promptly (abort controller drops the long-poll).
 *
 * USAGE (from the DSH host process)
 * ---------------------------------
 *   import { PcaiToolServer } from "./dsh/pcai_tool_server.mjs";
 *
 *   const toolServer = new PcaiToolServer({
 *     baseUrl: "https://llm.example.internal",   // gateway origin
 *     apiKey: process.env.PCAI_LLM_ADMIN_KEY,    // SAME bearer key as chat
 *     tools: [                                    // what this machine offers
 *       {
 *         name: "bash",
 *         description: "Run a shell command on the user's machine",
 *         inputSchema: { type: "object", properties: { command: { type: "string" } }, required: ["command"] },
 *         timeout_s: 120,
 *       },
 *       {
 *         name: "edit",
 *         description: "Apply an edit to a file",
 *         inputSchema: {
 *           type: "object",
 *           properties: { path: { type: "string" }, old_text: { type: "string" }, new_text: { type: "string" } },
 *           required: ["path", "old_text", "new_text"],
 *         },
 *       },
 *     ],
 *     ttl_s: 900,      // offer TTL (server clamps to [60, 86400])
 *     pollWaitS: 25,   // long-poll wait (server clamps to [0, 60])
 *   });
 *
 *   // DELEGATION: the host supplies the executor — local approval policy and
 *   // sandboxing stay DSH-side. Return a string (or JSON-serializable value).
 *   // Throwing rejects the dispatch with the error text (ok=false on the wire).
 *   toolServer.registerExecutor(async ({ tool, arguments, deadline_s }) => {
 *     if (tool === "bash") {
 *       if (!(await approvals.ask(`run \`${arguments.command}\`?`))) {
 *         const err = new Error("approval denied by local policy");
 *         err.approvalDenied = true;          // any thrown error -> ok=false
 *         throw err;
 *       }
 *       return runShell(arguments.command);   // host's real implementation
 *     }
 *     if (tool === "edit") return applyEdit(arguments);
 *     throw new Error(`tool ${tool} not served by this machine`);
 *   });
 *
 *   toolServer.start();   // register + poll loop (all failures inside: logged, retried)
 *   // toolServer.stop(); // abort the poll + close the channel (offer then TTL-expires)
 *
 * CONFIG
 * ------
 *   baseUrl    (required) gateway origin, no trailing path
 *   apiKey     (required) bearer key with the "proxy" scope (the chat key)
 *   tools      (default: bash + edit shapes above) — the offered toolset
 *   ttl_s      (default 900)  offer TTL seconds
 *   pollWaitS  (default 25)   long-poll wait seconds (0..60)
 *   registerRetryS (default 10) base sleep between failed registrations
 *   pollDelayS (default 1)     sleep after a claim/error before re-polling
 *   logger     (default console) { log, warn, error }
 *
 * ENV
 * ---
 *   PCAI_LLM_ADMIN_KEY — the bearer key, when apiKey is not passed.
 *   PCAI_LLM_BASE_URL  — the gateway origin, when baseUrl is not passed.
 */

const DEFAULT_TOOLS = [
  {
    name: "bash",
    description: "Run a shell command on the user's machine",
    inputSchema: {
      type: "object",
      properties: { command: { type: "string" } },
      required: ["command"],
    },
    timeout_s: 120,
  },
  {
    name: "edit",
    description: "Apply a text edit to a file on the user's machine",
    inputSchema: {
      type: "object",
      properties: {
        path: { type: "string" },
        old_text: { type: "string" },
        new_text: { type: "string" },
      },
      required: ["path", "old_text", "new_text"],
    },
    timeout_s: 60,
  },
];

const MAX_ANSWERED_CACHE = 512;

/** Jittered exponential backoff: base * 2^attempt capped at 60s, +-25%. */
function backoff(baseS, attempt) {
  const cap = 60;
  const raw = Math.min(baseS * 2 ** Math.min(attempt, 6), cap);
  const jitter = raw * (0.75 + Math.random() * 0.5);
  return Math.max(0.05, jitter) * 1000;
}

function sleep(ms, signal) {
  return new Promise((resolve) => {
    if (signal?.aborted) return resolve();
    const t = setTimeout(resolve, ms);
    signal?.addEventListener("abort", () => {
      clearTimeout(t);
      resolve();
    }, { once: true });
  });
}

export class PcaiToolServer {
  /**
   * @param {object} cfg
   * @param {string} cfg.baseUrl      gateway origin (e.g. https://llm.example.internal)
   * @param {string} cfg.apiKey       bearer key (proxy scope) — defaults to env PCAI_LLM_ADMIN_KEY
   * @param {Array<{name: string, description?: string, inputSchema?: object, timeout_s?: number}>} [cfg.tools]
   * @param {number} [cfg.ttl_s]          offer TTL seconds (default 900)
   * @param {number} [cfg.pollWaitS]      long-poll wait seconds 0..60 (default 25)
   * @param {number} [cfg.registerRetryS] base retry sleep for registration (default 10)
   * @param {number} [cfg.pollDelayS]     sleep after a handled claim (default 1)
   * @param {{log?: Function, warn?: Function, error?: Function}} [cfg.logger]
   */
  constructor(cfg = {}) {
    this.baseUrl = String(cfg.baseUrl || process.env.PCAI_LLM_BASE_URL || "").replace(/\/+$/, "");
    this.apiKey = cfg.apiKey || process.env.PCAI_LLM_ADMIN_KEY || "";
    this.tools = cfg.tools ?? DEFAULT_TOOLS;
    this.ttlS = cfg.ttl_s ?? 900;
    this.pollWaitS = cfg.poll_wait_s ?? cfg.pollWaitS ?? 25;
    this.registerRetryS = cfg.registerRetryS ?? 10;
    this.pollDelayS = cfg.pollDelayS ?? 1;
    this.logger = cfg.logger ?? console;
    /** Host executor: async ({tool, arguments, deadline_s}) => content. */
    this.executor = null;
    this.clientId = null;
    this.expiresAtMs = 0; // client-side clock is a HEURISTIC (server is the authority)
    this.stopped = true;
    this.controller = null;
    this._loop = null;
    // dispatch_ids already answered (bounded FIFO): defense-in-depth against
    // re-EXECUTING a job whose id repeats. The server's dispatch-once pop is
    // the primary guarantee; a replayed id (buggy LB retry, our own bug)
    // still must not trigger a second side-effectful execution.
    this._answeredIds = new Set();
    this._answeredOrder = [];
  }

  /** Provide the host-side executor (see header). Call BEFORE start(). */
  registerExecutor(fn) {
    this.executor = fn;
  }

  _headers(extra = {}) {
    return {
      Authorization: `Bearer ${this.apiKey}`,
      ...extra,
    };
  }

  _url(path) {
    return `${this.baseUrl}${path}`;
  }

  /**
   * One registration attempt. Returns {ok, clientId, expiresAtMs, retryable}.
   * 401/403/400 are NOT retried by the loop's caller until the next TTL
   * check (a wrong key or bad offer does not improve by hammering) — 5xx and
   * network errors are.
   */
  async _registerOnce(signal) {
    try {
      const resp = await fetch(this._url("/v1/dispatch/register"), {
        method: "POST",
        headers: this._headers({ "Content-Type": "application/json" }),
        body: JSON.stringify({ tools: this.tools, ttl_s: this.ttlS }),
        signal,
      });
      if (resp.ok) {
        const body = await resp.json().catch(() => ({}));
        this.clientId = body.client_id || this.clientId;
        // expires_at is a server-monotonic value — opaque; keep our own TTL
        // heuristic for proactive re-registration.
        this.expiresAtMs = Date.now() + this.ttlS * 1000;
        this.logger.log(
          `[pcai_tool_server] registered ${this.tools.length} tool(s) as ${this.clientId}`,
        );
        return { ok: true };
      }
      if (resp.status === 404) {
        // Flag off, or the route unmounted — this build of the gateway does
        // not serve the dispatch surface; back off politely and keep trying
        // (the flag can be flipped live).
        return { ok: false, retryable: true, status: 404 };
      }
      if (resp.status === 401 || resp.status === 403 || resp.status === 400 || resp.status === 413) {
        const text = await resp.text().catch(() => "");
        this.logger.error(
          `[pcai_tool_server] registration refused (${resp.status}): ${text.slice(0, 200)}`,
        );
        return { ok: false, retryable: resp.status === 401 || resp.status === 403, status: resp.status };
      }
      return { ok: false, retryable: true, status: resp.status };
    } catch (err) {
      if (signal?.aborted) return { ok: false, retryable: false, aborted: true };
      this.logger.warn(`[pcai_tool_server] register failed: ${err?.message || err}`);
      return { ok: false, retryable: true };
    }
  }

  /**
   * Ensure a live offer: register on first start, on poll-404 (expiry or
   * gateway restart), or proactively as our TTL heuristic runs out.
   */
  async _ensureRegistered(signal) {
    const expired = Date.now() >= this.expiresAtMs;
    if (this.clientId && !expired) return true;
    let attempt = 0;
    while (!signal.aborted) {
      const out = await this._registerOnce(signal);
      if (out.ok) return true;
      if (out.aborted) return false;
      if (!out.retryable) {
        // Auth/validation refused: do NOT hot-loop; wait a full base period
        // (the operator may fix the key / the offer mid-flight).
        attempt = 0;
      }
      await sleep(backoff(this.registerRetryS, attempt++), signal);
    }
    return false;
  }

  /**
   * Claim one dispatch job, or null after the long-poll wait elapses (204)
   * / on a recoverable error (the loop retries with backoff).
   */
  async _pollOnce(signal) {
    let resp;
    try {
      resp = await fetch(
        this._url(`/v1/dispatch/poll?wait_s=${encodeURIComponent(this.pollWaitS)}`),
        { method: "GET", headers: this._headers(), signal },
      );
    } catch (err) {
      if (signal?.aborted) return { done: true };
      this.logger.warn(`[pcai_tool_server] poll failed: ${err?.message || err}`);
      return { backoff: true };
    }
    if (resp.status === 204) return { empty: true };
    if (resp.status === 404) {
      // Offer expired or the gateway restarted (in-memory registry) —
      // re-register on the next loop iteration.
      this.clientId = null;
      this.expiresAtMs = 0;
      return { reregister: true };
    }
    if (resp.status === 401 || resp.status === 403) {
      this.logger.error("[pcai_tool_server] poll refused (auth) — backing off");
      return { backoff: true };
    }
    if (!resp.ok) return { backoff: true };
    const job = await resp.json().catch(() => null);
    if (!job || !job.dispatch_id) return { empty: true };
    return { job };
  }

  /** Execute one claimed job through the host executor; POST the answer. */
  async _runJob(job, signal) {
    // Replayed dispatch_id: never re-execute (dispatch-once extends to the
    // client side). The POST would 409 anyway; skipping the execution is the
    // part that actually matters.
    if (this._answeredIds.has(job.dispatch_id)) {
      this.logger.warn(`[pcai_tool_server] duplicate dispatch_id ${job.dispatch_id} — not re-executed`);
      return;
    }
    this._answeredIds.add(job.dispatch_id);
    this._answeredOrder.push(job.dispatch_id);
    while (this._answeredOrder.length > MAX_ANSWERED_CACHE) {
      this._answeredIds.delete(this._answeredOrder.shift());
    }
    let ok = true;
    let payload = "";
    try {
      if (typeof this.executor !== "function") {
        throw new Error("no executor registered (registerExecutor was never called)");
      }
      const out = await this.executor({
        tool: job.tool,
        arguments: job.arguments ?? {},
        deadline_s: job.deadline_s,
      });
      payload = typeof out === "string" ? out : JSON.stringify(out ?? "");
    } catch (err) {
      ok = false;
      payload = err?.message || String(err);
    }
    try {
      const resp = await fetch(this._url("/v1/dispatch/result"), {
        method: "POST",
        headers: this._headers({ "Content-Type": "application/json" }),
        body: JSON.stringify(
          ok
            ? { dispatch_id: job.dispatch_id, ok: true, content: payload }
            : { dispatch_id: job.dispatch_id, ok: false, error: payload },
        ),
        signal,
      });
      if (resp.status === 409) {
        // First answer already won (or the relay timed out) — replay denial
        // is working as designed; drop our payload.
        this.logger.warn(`[pcai_tool_server] result for ${job.dispatch_id} settled elsewhere (409)`);
      } else if (!resp.ok) {
        this.logger.error(`[pcai_tool_server] result POST failed (${resp.status}) — job dropped (dispatch-once)`);
      }
    } catch (err) {
      if (!signal?.aborted) {
        this.logger.error(`[pcai_tool_server] result POST failed: ${err?.message || err} — job dropped (dispatch-once)`);
      }
    }
  }

  /**
   * The poll loop. Runs until stop(); every path either claims a job or
   * sleeps — there is no tight spin anywhere in this method.
   */
  async _runLoop() {
    const signal = this.controller.signal;
    let backoffAttempt = 0;
    while (!signal.aborted) {
      if (!(await this._ensureRegistered(signal))) continue;
      const out = await this._pollOnce(signal);
      if (out.done) break;
      if (out.job) {
        backoffAttempt = 0;
        // Await the job fully before polling again: one in-flight dispatch
        // at a time keeps local approval prompts ordered.
        await this._runJob(out.job, signal);
        await sleep(this.pollDelayS * 1000, signal);
        continue;
      }
      if (out.backoff) {
        await sleep(backoff(this.pollDelayS, backoffAttempt++), signal);
        continue;
      }
      backoffAttempt = 0;
      if (!out.empty && !out.reregister) {
        await sleep(this.pollDelayS * 1000, signal);
      }
    }
  }

  /** Start the channel (idempotent). Registers + polls until stop(). */
  start() {
    if (!this.baseUrl) throw new Error("PcaiToolServer: baseUrl (or PCAI_LLM_BASE_URL) is required");
    if (!this.apiKey) throw new Error("PcaiToolServer: apiKey (or PCAI_LLM_ADMIN_KEY) is required");
    if (Array.isArray(this.tools) && this.tools.length === 0) {
      throw new Error("PcaiToolServer: an empty tool offer registers nothing — pass at least one tool");
    }
    if (!this.stopped) return;
    this.stopped = false;
    this.controller = new AbortController();
    this._loop = this._runLoop().catch((err) => {
      this.logger.error(`[pcai_tool_server] loop crashed: ${err?.message || err}`);
      this.stopped = true;
    });
  }

  /** Stop polling and close the in-flight long-poll. The offer TTL-expires. */
  async stop() {
    this.stopped = true;
    this.controller?.abort();
    try {
      await this._loop;
    } catch {
      /* already logged */
    }
    this._loop = null;
    this.controller = null;
  }
}

export default PcaiToolServer;
