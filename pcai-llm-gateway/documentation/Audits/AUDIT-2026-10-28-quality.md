# AUDIT 2026-10-28 — "GLM-5.3-Flash and Deepseek-V4.1-Flash feel weaker than expected"

**Question under investigation (verbatim):** *"GLM 5.3 Flash and DS v4.1 Flash both seem to be performing kind of less than what I'd expect. I'm wondering if the gateway can play any role in this, or any other ideas."*

**Method:** 5-agent audit wave (routing/cascade · proxy transform · tool relay · live runtime evidence · config+docs+history) with direction-diverse model pins (GLM-5.3-Flash, Deepseek-V4.1-Flash-1017, Qwen3.8-27B), followed by a 2-agent adversarial cross-validation wave. Read-only throughout: no source edits, no cluster mutations, no git state operations. Per-zone reports in `.scratch/audit-2026-10-28/`.

**Target:** `pcai-llm-gateway` v3.2.2, namespace `pcai-llm`, 2/2 replicas.

---

## 0. Verdict

**The gateway is not silently swapping your models, and it is not truncating or rewriting your requests in any way that would make a good model look weak.** That was verified three independent ways (code, unit-test invariants, and live production logs) and it is the strongest result in this audit.

**Sampling is at spec, not a cause.** `temperature=1.0` is the required operating point for these reasoning models — GLM-5.3-Flash's published `generation_config.json` is `{temperature: 1.0, top_p: 0.95}`, so the observed values are correct, not "hot". *(An earlier revision of this report wrongly framed 1.0 as the cause; that was an error and is retracted.)*

**The one sampling anomaly is Deepseek's `top_p`** — see §2. Otherwise the real drivers are GPU capacity pressure and prompt growth, both outside the gateway, plus gateway defects that are latent rather than the cause of today's symptom.

Ranked:

| # | Cause | Layer | Confidence |
|---|---|---|---|
| 1 | **Deepseek's `top_p` is 0.95, but the deployment asks for 1.0** — the only sampling deviation found. GLM at temp 1.0 / top_p 0.95 is its published standard and is **correct** | model server | **high (both values observed/verified)** |
| 2 | **scs04 is 8/8 GPUs packed** (GLM tp4 + Deepseek tp4, 126–134 GiB/GPU, 61–100% util) with measured KV-cache retraction; Deepseek averages **214k prompt tokens/request** | capacity | high |
| 3 | Reasoning re-feed is tail-truncated to 2000 chars on every request, and on a mid-turn model switch the previous model's reasoning is replayed as the new model's own (P1, latent) | gateway | high on code |
| 4 | The `routing` alias always resolves to Deepseek (the most loaded model) while the decision router **never** clears its 0.60 confidence threshold — up to 2.5 s wasted per request, zero benefit | gateway | high |

---

## 1. The central question: can a pinned model be silently substituted? **Not by routing — no.**

Three independent confirmations:

**(a) Code.** `classify_request` returns `"pinned"` for any registered+allowed name, and pinned requests never cascade:
```
src/pcai_llm/core/cascade.py:157-173
    if model in registered:
        if allowed and model not in allowed: return "forbidden"
        return "pinned"
    return "alias"
```
Invariant pinned by `tests/test_cascade.py:82 test_pinned_ignores_ordering`.

**(b) Live production logs** — the gateway emits one line per request naming the decision. Over the sampled window, **zero** pinned requests were served by a different model:
```
  41  GLM-5.3-Flash -> GLM-5.3-Flash            [pinned]
  34  Deepseek-V4.1-Flash-1017 -> Deepseek-V4.1-Flash-1017 [pinned]
  21  Qwen3.8-27B -> Qwen3.8-27B                [pinned]
  11  routing -> Deepseek-V4.1-Flash-1017       [alias]
   2  auto -> Deepseek-V4.1-Flash-1017          [alias]
```

**(c) Engine identity.** Pod args confirm the servers really are the named models: `--model-path zai-org/GLM-5.3-Flash --served-model-name glm-5.3-flash`; `--model-path deepseek-ai/DeepSeek-V4.1-Flash-1017 --served-model-name deepseek-v4.1-flash`.

**Also ruled out** (each verified, not assumed): implicit `max_tokens` (none exists — `clamp_max_tokens` is a zero-copy no-op for GLM/DS; the only ceiling entry is Qwen), sampling-parameter rewriting (the upstream body *is* the client's body), system-prompt injection (no such code), history truncation (`CONVERSATION_CONTEXT_BUDGET_TOKENS=0`), response caching (the only cache is embeddings-only, keyed on model+input, stores a vector), stream corruption (raw byte relay; the TTFT peek replays every byte), and tool-schema degradation (schemas forwarded verbatim).

---

## 2. Sampling — at spec for GLM, one unexplained deviation on Deepseek

**Retraction:** an earlier revision of this section claimed `temperature=1.0` was the cause of the symptom. That was wrong. `temperature=1.0` is the *required* operating point for these reasoning models, and GLM's published config is exactly that. The error is retracted.

**GLM is correct.** Its model repo's `generation_config.json` (fetched live) is:

```
{"_from_model_config": true, "temperature": 1.0, "top_p": 0.95, "eos_token_id": [154820, 154827, 154829]}
```

`temp 1.0 / top_p 0.95` is GLM-5.3-Flash's standard sampling. The engine applies it, the gateway forwards the body verbatim, and the harness sets neither field. Nothing is wrong here.

**Deepseek is the anomaly.** The deployment asks for one thing and the engine does another:

```
deployment arg:      --preferred-sampling-params '{"temperature":1.0,"top_p":1.0}'
engine, live log:    sampling_params={'temperature': 1.0, ..., 'top_p': 0.95, 'top_k': -1, 'min_p': 0.0, ...}
```

The deployment requests `top_p = 1.0`; the engine applies **0.95**. Every source that could supply that value is accounted for, and none of them is 1.0 except the one that should be:

| source in the merge chain | supplies `top_p` |
|---|---|
| the client (harness profile) | nothing — it sets neither `temperature` nor `top_p` |
| `--preferred-sampling-params` | 1.0 (the deployment explicitly asks for this) |
| SGLang OpenAI defaults | 1.0 |
| the model repo's `generation_config.json` | **the only remaining candidate → 0.95** |

`--sampling-defaults` is not set, so SGLang defaults to `model`, which loads `generation_config.json` and evidently wins over the preferred-params flag for this field. I could not confirm this last step: `deepseek-ai/DeepSeek-V4.1-Flash-1017` returns *Entry not found* on Hugging Face, so its `generation_config.json` is not publicly inspectable. **Treat the origin as unconfirmed and the effective value as observed fact.**

**Why it is worth fixing even though it is small.** DeepSeek's standard operating point is `top_p = 1.0` (your reading, and it matches the deployment's own explicit request). `top_p = 0.95` truncates the sampling tail, which on a reasoning model mildly reduces diversity of thought — a small, systematic bias rather than a dramatic one. The larger problem is that **effective sampling is currently decided by a four-layer merge chain that nothing in the stack makes visible**: the harness sets nothing, the gateway provably never touches sampling (`grep` across `src/pcai_llm` returns zero references), the deployment's flag is silently overridden, and the value that actually governs is in a model repo you cannot read.

**Fix:** stop leaving it implicit. Either send explicit per-model sampling from the harness (`temperature: 1.0` and `top_p: 1.0` for Deepseek, `top_p: 0.95` for GLM), or set `--sampling-defaults openai` on the Deepseek deployment and verify the flag now takes effect. Then confirm from the engine log that the applied `top_p` is 1.0.

---

## 3. Cause #2 — capacity pressure on a fully-packed node

All three engines are SGLang on KServe in `project-user-andrew-bydlon`:

| engine | node | GPUs | notable config | measured pressure |
|---|---|---|---|---|
| `glm-53-flash` (6d) | scs04 | 4 (tp4/ep4) | `--kv-cache-dtype bfloat16`, EAGLE spec-decode, `--hicache-ratio 6.0`, `--mem-fraction-static 0.85`, max-running 96 | 44 + 24 retraction events |
| `deepseek-v41-flash-offload` (2d, 1 restart) | scs04 | 4 (tp4/ep4) | DSPARK spec-decode, `--hicache-ratio 3`, `--mem-fraction-static 0.775`, max-running 128, **temperature 1.0** | **214k avg prompt tokens/req** |
| `qwen-38-27b` (56d) | scs05 | 1 | kv `fp8_e4m3`, ctx 262k, reasoning_effort medium | **89 retracts / 5.22M tokens retracted**, 1,158 aborts/7d, HiCache host tier 99.94% FULL, `MlisPredictorMemoryWarning` firing since Oct 21 |

**scs04 has zero headroom**: GLM(4) + Deepseek(4) = 8/8 GPUs, 126–134 GiB used per GPU, DCGM 61–100%. Two flagships share one node with no spare capacity. Measured KV preemption means requests are *retracted and recomputed* — which shows up to the user as long pauses and inconsistent answers, not as an error.

**Two config observations worth a look:**
- Both scs04 flagships use **bfloat16** KV cache (GLM explicitly; DS by auto-default) while Qwen on scs05 uses `fp8_e4m3`. Halving KV precision would roughly double the concurrent long contexts these two could hold — a direct lever against the retraction measured on scs04.
- **Deepseek averages 214k prompt tokens per request.** That is enormous and it is the dominant driver of DS's KV pressure. It comes from agentic loop growth, not from the gateway (the gateway performs no trimming — `CONVERSATION_CONTEXT_BUDGET_TOKENS=0`).

---

## 4. Gateway defects found (real, but not the cause of today's symptom)

### P1 — Cross-model reasoning leak in the relay loop *(the one live quality mechanism)*
`src/pcai_llm/core/toolrelay.py:1923-1935` appends the assistant row with `reasoning_content` and `tool_calls` but **no model attribution**, then replays that list to whatever model serves the next iteration:
```python
_assistant_row: dict[str, Any] = {"role": "assistant", "content": ..., "tool_calls": tool_calls}
if settings.reasoning_history_enabled:
    _assistant_row["reasoning_content"] = _reasoning_block
relay_msgs.append(_assistant_row)
```
On a mid-turn model switch (budget cascade, `auto`, or TTFT failover) the new model reads the previous model's chain of thought **as its own**. Replaying another model's half-formed reasoning is a well-known degradation pattern. Latent: no mid-turn failover occurred in the sampled window, and **no fallback chains are seeded** — if the `model_fallbacks` table is empty this path is currently inert. Verify with `GET /admin/models/<name>/fallbacks`.

### P2 — Pinned request silently promoted on budget exhaustion, with no fallback header
`cascade.py:491-538` and the mid-relay path (`routes_proxy.py:1313-1320`) promote a pinned request into the profile ordering when budgets drain. Deliberate and documented, but the admission path emits **no `X-PCAI-Fallback` header** — only a log line and the response `model` field reveal the swap. A multi-turn session can drift GLM→DS→Qwen as budgets drain, which reads as flakiness.

### P2 — Client-caused 4xx (and mere slowness) feed the endpoint circuit breaker → a pinned request can be answered by another model
`routing/router.py:99-106` trips at `ENDPOINT_FAILURE_THRESHOLD` consecutive failures with **no time decay** and **no status-class discrimination** (reset only on success, `router.py:88`). `report(False)` is called **unconditionally for any non-2xx, including 400s**, and it sits *before* the 5xx-only fallback walk:

```python
routes_proxy.py:783-789   (non-streaming)     routes_proxy.py:2094-2103  (streaming)
  if not _is_2xx(resp.status_code):             if not _is_2xx(...):
      # "4xx is the caller's/request's fault — never re-rolled"   <-- covers re-rolls, not the breaker
      await cur_pool.report(cur_ep, False, error=f"upstream status {resp.status_code}")
```

**Bonus site:** `routes_proxy.py:2077` also calls `report(False, error="ttft budget exceeded")` — so *slowness alone* feeds the breaker, on a node that is deliberately packed 8/8.

**Why this is the gateway's only real "wrong model answered" vector.** Five consecutive client-caused 400s (e.g. oversized-context 400s from one busy agent key) eject a **healthy** endpoint for 60 s. Pinned requests are explicitly documented to keep a failover chain for exactly this case — `model_fallbacks.py:19-22`: *"PINNED requests keep their failover chain, too — a pin says 'serve THIS model', and this module only engages when THAT model demonstrably cannot."* If the chain names another model, the pinned GLM request is then answered by it, with `X-PCAI-Fallback` set. Live: `breaker_trips = 9` over 7 days.

**This is why §1's headline needs one qualifier:** a pinned model is never substituted *by routing*, but it **can** be substituted by the breaker → fallback chain, and by mid-relay budget exhaustion (§9.1). Both are latent (neither fired in the sampled window). **The first thing to check is the contents of `model_fallbacks`** — if the chains are empty, both paths are inert; if GLM's chain names Deepseek or Qwen, they are live and they are a designed quality downgrade on every upstream hiccup.

Fix direction: skip `report(False)` for 4xx (all but 429), and stop counting `ttft budget exceeded` against a node you intentionally packed.

### P2 — Any unregistered model name is silently treated as an alias
`cascade.py:157-173` returns `"alias"` for *any* name not in `registered`; the documented `'unknown'` → 404 branch is **dead code** (it is never returned). So a stale or typo'd model id does not fail — it is resolved through the profile ordering to whatever model sits at the head. This is exactly the trap recorded in the repo's own `fix-stale-model-ids.sh` ("either a hard spawn failure or a silent alias to GLM"). Recommendation: 404 an unrecognised name unless it is on an explicit alias allowlist (`auto`, `auto-demand-aware`, `routing`).

### P1 — The `routing` profile is **inert**: policy is `shadow`, so nothing it decides is ever served
*(Rewritten after a dedicated verification pass — the original bullet was wrong on the mechanism. See §11.)*

The profile runs in `shadow`, which computes and records routing decisions but never applies them. Verified directly from code, not inference: decision-model calls **do** happen (so policy ≠ `off`, which returns early at `model_routing.py:682`), yet the request log still shows `requested=routing` (so policy ≠ `active`, which alone rewrites `requested_model` at `routes_proxy.py:2842-2843`). Therefore **policy = shadow**.

Consequence: every `routing` request is served exactly like the plain `auto` alias — a cascade through the profile ordering, landing on `order[0]` = Deepseek — plus a decision-model call (261 ms p50 / 463 ms p95, measured) whose result is discarded. With `ROUTE_DEFAULT_MODEL=false`, `routing` and `auto` are **behaviourally identical** today.

### P2 — `demand_aware_excluded_models` is honoured by only one of two selectors
`["Qwen3.8-27B"]` is read by `cascade.py:310` (head pick) but **never** by `core/model_routing.py`. Today this is safe only because no profile sets `routing_min_model` (empty floor ⇒ no downgrade). Set a floor to enable the documented cost saving and every sub-1500-token request is served by the cheapest rung — i.e. the Qwen you explicitly excluded — with no warning.

### P2 — The deployed artifact cannot be rebuilt from git
HEAD is `6f29366` "release: bump 2.3.1" (2026-10-09). The tree is 3.2.2 with **78 files, +11,902 / −934 lines uncommitted**, including untracked `core/decision_routing.py` (617 lines) and `core/chain_audit.py`. Consequences: no commit can be blamed for a regression in the last 19 days, the running image is not reproducible or bisectable, and there is no way to prove the image matches the tree. **This is the single biggest obstacle to answering "what changed?"** — and it means untested code is live in production.

### P3 — Assorted
- Tool results are capped at 32,000 chars with an explicit marker (`toolrelay.py:129-151`) — self-correctable, but if a tool returns >32K the model loses whatever sits past the cut.
- The relay catalog can serve a stale tool list for up to 120 s after a blip, with no notification to the model.
- `CHANGELOG.md` has **no 3.x section at all** (`grep '^## 3'` → 0 hits); highest documented release is 2.9.2, placed *above* "Unreleased".
- `FEATURES.md:684` still documents a routing-score term for tool definitions that the code deliberately removed (2026-10-26); `DEPLOYMENT.md:318` contradicts it and is correct.
- `helm/values-examples/values.g2.yaml` (committed) diverges from `helm/local/values.g2.yaml` (deployed) on `toolRelayMaxIterations` (16 vs 32), `clientToolDispatch` (false vs **true**), `toolRelayRenderDeltas` (absent vs true).
- `distiller_model=""` + `DISTILLER_ENABLED=true` puts background distillation LLM calls on the **same** cascade and GPUs as user traffic.
- 879M "TPM refund" tokens/7d — accounting smell worth a look.

---

## 5. What's solid (verified — do not "fix" these)

- Pinned requests never cascade; `tests/test_cascade.py:82` pins it.
- No implicit `max_tokens`, no ceiling for GLM/DS; `tests/test_max_tokens_clamp.py` pins the zero-copy contract.
- Sampling parameters, `stop`, `seed`, `tools` are forwarded verbatim.
- No system-prompt injection anywhere in `src/`.
- Streaming is a raw byte relay with a byte-identical TTFT peek; no retry can interleave deltas.
- `canonical.reconcile` returns the client's incoming list in every mode — a stale DB row can never replace the conversation.
- The reasoning cap keeps the **tail** and marks the cut — the correct half to keep.
- Guardrails ship with empty tables and fail open; the embed cache is embeddings-only.
- Gateway TTFT overhead is sub-second (p95 ≈ 0.75–0.98 s); total latency is engine generation time.
- Error rates are low: GLM 0.33%, Deepseek 0.42% (Qwen is the outlier at 3.6%).

---

## 6. Recommended actions, cheapest first

1. **Pin sampling explicitly per model from the harness** — `temperature: 1.0` for both, `top_p: 1.0` for Deepseek and `top_p: 0.95` for GLM — so the four-layer merge chain stops deciding it invisibly. Then confirm from the Deepseek engine log that the applied `top_p` is 1.0 (today it is 0.95 while the deployment asks for 1.0). Small effect, but it is the one concrete sampling deviation found.
2. **Check the `model_fallbacks` table contents.** That single query tells you whether the two latent substitution paths (breaker → chain, mid-relay budget swap) are live or inert. Cheapest high-information action in this report.
3. **Re-check capacity.** scs04 has no headroom for two 4-GPU flagships. Either move one model to another node, or cut Deepseek's prompt growth (below). Both flagships use bf16 KV cache; `fp8_e4m3` would roughly double the concurrent long contexts they can hold.
4. **Cut the 214k-token Deepseek prompts.** The dominant KV pressure driver. Turning on tool-result compaction or setting a sane `CONVERSATION_CONTEXT_BUDGET_TOKENS` (after making `trim_to_budget` tool-pair-aware — it currently pops messages without regard to `tool_calls`/`role=tool` pairing) would pay back immediately.
5. **A/B the reasoning re-feed:** one session with `PCAI_LLM_REASONING_HISTORY_ENABLED=false`. It is the newest request-path change (2026-10-26) and the only common-path prompt rewrite.
6. **Commit the tree.** 3.2.2 is 11.9k uncommitted lines in production. Until it is committed, no regression can be attributed and no image can be rebuilt.
7. **Small fixes:** exclude 4xx-except-429 from the breaker; stop feeding the breaker on `ttft budget exceeded` for a deliberately packed node; 404 unrecognised model names; lower `DEFAULT_MIN_CONFIDENCE` or skip the decision call when it cannot clear it; make `demand_aware_excluded_models` binding on both selectors; emit `X-PCAI-Fallback` on the admission path.

---

## 7. Test gaps (why these survived)

The obvious suspects are covered (`clamp_max_tokens`, `pick_serving_model`/`complexity_score`, TTFT gate, zero-completion retry, pinned-never-cascades). Genuine gaps:
1. Nothing pins the reasoning cap's **tail-keep** semantics — `grep 'earlier reasoning trimmed' tests/*.py` → 0 hits. A head-keeping regression would still pass a `len() <= cap` assertion.
2. No test asserts a mid-turn model switch does not replay the previous model's `reasoning_content` (the P1 above).
3. No test covers a routing floor that is an excluded model.
4. No test for unrecognised-model-name handling (the dead `'unknown'` branch).

---

## 8. Residual uncertainty

- **No database read path.** Per-request `requested`→`served` pairs and `finish_reason=length` prevalence live in the gateway's postgres, which is not reachable read-only from this session. The log-derived counts in §1 cover a sample window on pods that are minutes old, not 7 days. Closing this needs an admin API key (`/admin/models/<name>/fallbacks`, request ledger).
- **GLM's live applied sampling is not observable** (level-0 request logs), but its model config is `temp 1.0 / top_p 0.95` and nothing else could override it, so the values are known even if unobserved.
- **The origin of Deepseek's `top_p=0.95` is unconfirmed** — the model repo is not publicly fetchable, so the `generation_config.json` step in §2 is inference. The *effective value* is observed fact.
- **`model_fallbacks` contents are unknown** — if chains are non-empty, several latent paths above are live.
- Gateway pods were ~3 minutes old at audit start, so no historical gateway logs exist for the degradation window; all history is Prometheus-derived.
---

## 9. Cross-validation (2-agent adversarial wave)

A second wave tried to break the headline. **It survived.** Reviewer `xval-falsify` (Qwen3.8-27B) examined ten mechanisms the first wave did not cover — HTTP proxy inheritance, connection pooling, keep-alive vs long streams, mid-relay failover, client-tool dispatch, load-aware cascade, offpeak billing, reasoning injection, dirty-tree deployment — and found **no gateway mechanism that degrades a pinned request's output** under current conditions.

### 9.1 Correction to §1: a pinned model CAN be swapped mid-relay

The first wave's "pinned never cascades" is true **at admission** but **not mid-relay**. On budget exhaustion during a relay turn, the request continues on the next model in the profile ordering. This is documented in the code, not accidental:

```
api/routes_proxy.py:3425-3428
  "The tail comes from the profile ordering for BOTH alias and pinned requests
   (a pinned GLM that dies of budget mid-stream flows into the ordering's next model ...)"
core/cascade.py:95
  "the relay's mid-loop budget failover walks this tail: a PINNED request ..."
```
Combined with the P1 cross-model reasoning leak (§4), this is the one path where a GLM conversation can silently continue as Deepseek *and* inherit GLM's chain of thought as its own. **It is not firing today** — zero budget-exhaustion events in the sampled logs — so it is latent, not the cause of the current complaint. It becomes the top risk the moment budgets are tightened.

### 9.2 New finding — nobody enforces the context window (P2)

The harness profile declares a **1,048,576-token** context window for both Flash models:

```
~/.dsh/profiles/web/cordis.patch.yml:107-126
  - id: GLM-5.3-Flash
    maxTokens: 262144
    contextWindow: 1048576
  - id: Deepseek-V4.1-Flash-1017
    maxTokens: 262144
    contextWindow: 1048576
```

and the gateway has **no context-window awareness at all** (`grep -rn 'context_window|contextWindow|context_length' src/pcai_llm/` → **zero hits**). Nobody validates that `prompt_tokens + max_tokens` fits the model's real window:

- the harness will not compact until 1,048,576 tokens, so prompts grow unbounded;
- the gateway forwards whatever it receives;
- the engine is the only thing that knows the real limit.

This is the mechanism behind Deepseek's **214k-token average prompt** (§3), and it explains the shape of the failure: not errors, but prefill pressure, KV retraction, and recompute. It also feeds the 4xx→breaker defect (§4): an over-long prompt returns 400, and five of those eject a healthy endpoint. **Action:** set `contextWindow` to the engines' true limits and have the gateway reject (with a clear 400) any request exceeding them. *Caveat: I could not read the engines' true limits (startup logs rotated, no in-pod exec), so the correct value must be confirmed from the model configs.*

### 9.3 Latent trap — HTTP proxy inheritance

The gateway honours `HTTP_PROXY=http://hpeproxy.its.hpecorp.net:8080` because its httpx client does not set `trust_env=False`. `NO_PROXY` currently covers `.svc.cluster.local`, `10.0.0.0/8` and `172.16.0.0/12`, and the observed sub-second TTFT plus 0.4% error rate prove upstream calls are **not** being proxied today. But if an endpoint URL ever moves to an uncovered host or IP range, every upstream call would silently traverse the corporate proxy — adding latency and risking truncated SSE streams. `trust_env=False` would make this explicit and immune to env changes.

### 9.4 Where the reviewers disagree, and how to settle it

`xval-falsify`'s single best guess was "model capability ceiling — the Flash variants are simply weaker than your bar." That is a real possibility and it is **not** ruled out. But it does not explain why the *same* models are sampled at `temperature=1.0` (§2), and it does not explain the Oct 21–26 degradation timeline (§3). The two hypotheses are distinguishable by one experiment:

> **The settling measurement:** take ~50 real prompts and run them twice — once through the gateway, once directly against the sglang endpoints — with `temperature` pinned to the same value in both runs. If the outputs are equivalent, the gateway is exonerated and the remaining levers are sampling and capacity. If they differ, diff the two captured prompts (`PCAI_LLM_EFFECTIVE_PROMPT_CAPTURE=true` already stores them for 7 days) and the gateway transform is proven.

---

## 10. Zone reports

| Report | Zone | Verdict |
|---|---|---|
| `.scratch/audit-2026-10-28/routing-cascade.md` | routing / cascade / failover / offpeak | No substitution on pinned; 3 latent findings |
| `.scratch/audit-2026-10-28/proxy-transform.md` | request/response transform | Byte-faithful proxy; 1 common-path prompt rewrite |
| `.scratch/audit-2026-10-28/tool-relay.md` | tool relay / MCP surface | Clean; 2 self-healing P3 |
| `.scratch/audit-2026-10-28/runtime-evidence.md` | live cluster evidence | Gateway innocent; serving stack is the pressure point |
| `.scratch/audit-2026-10-28/config-docs-drift.md` | config / docs / history | 1 live P1, 3 doc drifts, git blackout |
| `.scratch/audit-2026-10-28/xval-falsify.md` | adversarial cross-validation | Headline survives; mid-relay swap is real |
---

## 11. The `routing` profile — corrected verification (2026-10-28, third pass)

This user drives **all** traffic through the `routing` (decision-model) pseudo-model and does not pin.

> **Correction history — read this first.** An earlier revision of this section claimed the decision model "never fires / abstains 100%". **That was wrong**, and the error was methodological: `decision_routing.py` logs the abstain line **only when a decision fails to land** (`if not result.ok: log.info(...)`). Searching the log for `decision routing:` therefore returns *only abstentions* and silently omits every success. I reported a sampling artefact as a measurement. A second revision compounded it by inferring a confidence-scale bug from those same abstain-only confidences. Both are retracted below.

### 11.1 The decision model is healthy — measured

| metric | value | source |
|---|---|---|
| decision calls (this process) | 51 — abstained 19, **served 32 (62.7%)** | admin console, pod `mzw6n` |
| decision calls (fleet, 24h) | 593.7 abstained / 103.4 served = **14.8% land** | `gateway_decision_calls_total` |
| latency | **308 ms mean**, p50 341 ms, p95 484 ms | `gateway_decision_latency_ms` |
| mode / model / floor | `active` / `vllm-sr/d3-turbo` / `0.6` | `routing_config` |
| prompt | operator-supplied, 2,499 chars | `routing_config` |

The router answers, quickly, and clears its floor most of the time. The per-request cost is ~0.3 s, not the 2.5 s cap I previously quoted.

### 11.2 Two independent gates — and the console shows only one

This is the actual defect, and it explains the console screenshot:

| knob | where it lives | what it controls | observed value |
|---|---|---|---|
| `decision_mode` | `routing_config` (DB) | whether the decision model is **consulted** (`routes_proxy.py:2730-2743`) | **`active`** — the panel's "DECISION MODE" |
| `profiles.routing_policy` | `profiles` table | whether the answer is **served** (`model_routing.py:894`) | **`shadow`** — *not shown anywhere in the panel* |

`resolve_routed_model` fetches the second one itself (`profile_routing_config`, `model_routing.py:681`); the admin payload at `routes_admin.py:4493-4499` never includes it. So the console can read "DECISION MODE: active" while routing is not applied.

**`routing_policy = shadow` is established by elimination, not by direct read:**

| candidate | ruled out by |
|---|---|
| `off` | returns at `model_routing.py:682` **before** the decision seam — but decision calls demonstrably happen |
| `active` | returns `choice.model` (`:927`) and the caller rewrites `requested_model` (`routes_proxy.py:2842-2843`), so the log would read `requested=<served model> kind=pinned`. Live: **15/15 `requested=routing ... kind=alias`** |
| exception fallback | would log `routing: engagement failed` or `routing: decision failed (legacy behavior)` — **0 matches** in 2,000 log lines |
| ⇒ | **`shadow`** — the only branch that returns `requested` unchanged (`:894-905`) |

Corollary: the panel's `served` counter means *"the decision landed"*, **not** *"the decision was applied"* — despite the in-code comment at `model_routing.py:845-848` promising a re-label that does not exist in the code.

### 11.3 Why routing would still mostly serve Deepseek even when armed

For bare `routing`, `ceiling = order[0]` (`model_routing.py:706`) and `order = [Deepseek, GLM, Qwen]`, so the alias fallback lands on Deepseek. The deterministic heuristic is also saturated — measured scores **124,312–138,993** vs `routing_tier2_tokens = 6000` (21–23×) — so `pick_serving_model` returns the ceiling on every request. Only the decision seam can downgrade, and under `shadow` its pick is discarded.

### 11.4 What to do

1. **Set `profiles.routing_policy = 'active'`** for the profile. This is the only change that makes the router's pick serve. Confirm with `SELECT DISTINCT routing_policy, routing_min_model FROM profiles;` — and note that until it is `active`, `decision_mode` is irrelevant to what you are served.
2. **Surface `routing_policy` in the admin panel next to `decision_mode`**, or rename them so one is not mistaken for the other. Two knobs that both look like "routing is on" is the root cause of this confusion.
3. **Re-label the `served` counter** as `landed` (or implement the re-label the comment promises), so the panel cannot report 62.7% "served" while 0% is applied.
4. **Log the abstain distribution** (`probabilities=%s`, `decision_routing.py:610-616`) — still worth doing, for tuning rather than for the now-retracted scale hypothesis.
5. **Rescale tiers for agent traffic** (`tier1 ≈ 60k`, `tier2 ≈ 150k`) so the deterministic path can also route; today it is pinned to the ceiling by construction.

### 11.5 Still open

`profiles.routing_policy`, `profiles.routing_min_model` and the raw System One payload are not readable from this session (no admin key, no DB path, no pod exec). Everything above is code + log + metric inference, with the elimination table in §11.2 as the strongest link. **One `SELECT` closes it.**
