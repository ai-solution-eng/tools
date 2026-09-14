# Runbook — benchmarking playbooks

Command-first playbooks for the three benchmarking use cases. For flag-level
reference see the README (Part 1: universal tool, Part 2: chat tool) and
[`cli-reference.md`](cli-reference.md). Executable source of truth:
`--help` on either entry point.

## Which tool when

| You want to know… | Tool | Section |
|---|---|---|
| TTFT / tokens-per-second of a chat model under concurrency | `benchmark_chat.py` | §1 |
| How a chat endpoint's GPU fleet scales with users (coarse, non-streaming) | universal tool, REST mode | §1.7 |
| How ANY MCP server scales (RAG, SQL, tools) | universal tool, `--mode mcp` | §2 |
| How any REST API scales | universal tool, `--mode rest` | §2/§3.3 |

## Conventions

```bash
cd /home/andrew/Code/HPE/ModelBenchmarker
alias eb='PYTHONPATH=src python3 -m model_benchmarker.endpoint_benchmarker'
alias mbc='PYTHONPATH=src python3 -m model_benchmarker.benchmark_chat'
```

- Credentials: `--api_key` > `--api_key_file` > `$PCAI_API_KEY`. Prefer the file/env so tokens stay out of shell history.
- Artifacts land where you point them; for anything that should show up in the HTML report, use the results tree: `results/<model-or-target-dir>/<SETUP>.md`.
- `PROM_URL` (env) or `--prom-url` for the GPU join. Without it, sweeps still produce latency-only scaling curves.

---

## 1. Model benchmarking (chat TTFT / tokens-per-second)

### 1.1 Smoke test — "does this endpoint work at all?"

```bash
mbc --url https://<endpoint> \
  --api_key_file ~/.config/pcai/pcai.key \
  --number_users 4 --requests_per_user 3 --tasks coding,creative,mixed
```

30 seconds, no sweep, no artifacts. Everything failed → endpoint/auth/TLS problem (`--debug_stream` to inspect the stream shape). Registered in-cluster models instead: `--model_class_name <name> --remote` (see `utils/pcai_models.py`; entries marked `currently_deployed=False` fail every request).

### 1.2 The standard capacity suite (the sweep that populated `results/`)

```bash
mbc --model_class_name qwen38_27B --remote \
  --number_users 1,4,32,64 \
  --requests_per_user 5 \
  --tasks coding,creative,mixed \
  --context_length 0,32768 \
  --multiturn --separate_tasks \
  --output results/qwen_38_27b/<SETUP>.md
```

*Question answered:* what TTFT/tokens-s can each concurrency level sustain, cold (ctx=0) vs 32k prefill, per task, with the prefix cache engaged from turn 2 (`--multiturn`), each task isolated (`--separate_tasks`).

Filename encodes the serving setup — `<GPU>[xN]_[sglang|vllm]_[dflash2|mtp|...]_[nvfp4|fp8|bf16]_[hicache[xN]]_[replicasxN].md` (e.g. `H200_sglang_dflash2_hicachex3_replicasx3.md`). One file per (model × setup).

### 1.3 Context/prefill scaling study

```bash
mbc --model_class_name qwen38_27B --remote \
  --number_users 16 --tasks coding \
  --context_length 0,8192,32768,65536
```

*Question:* how does TTFT grow with prefill length at fixed concurrency?

### 1.4 Reasoning-budget sensitivity

```bash
for lvl in off low high; do
  mbc --model_class_name qwen38_27B --remote \
    --number_users 8 --requests_per_user 5 --tasks coding \
    --thinking_level $lvl \
    --output results/qwen_38_27b/qwen_think_$lvl.md
done
```

*Question:* what does a thinking cap cost/return in latency and throughput? (Budget must stay < task max_tokens, or TTFT reads 0.)

### 1.5 Cache regimes — pricing the prefix cache

```bash
# Cold (default nonce — every request a fresh prefix, worst case):
mbc --model_class_name deepseek_v4_flash_280B --remote \
  --number_users 1,4,32,128 --requests_per_user 5 \
  --tasks coding,creative,mixed --context_length 0,32768 \
  --output results/deepseek_v4_flash_0731/cold.md

# Shared prefix (all users reuse ONE prefix — agentic fleet shape):
mbc --model_class_name deepseek_v4_flash_280B --remote \
  --number_users 1,4,32,128 --requests_per_user 5 \
  --tasks coding,creative,mixed --context_length 0,32768 \
  --no-nonce \
  --output results/deepseek_v4_flash_0731/agentic_shared_prefix.md

# Best case: prefix resident BEFORE users arrive:
#   same + --prewarm
```

Compare cold vs shared-prefix at the same (ctx, users): the gap is what the prefix/KV cache earns. `--multiturn` splits this per row (`turn1` vs `TTFT-post`).

### 1.6 After any suite — regenerate the report

```bash
python3 src/model_benchmarker/results_to_html.py --open
```

### 1.7 Crossover: GPU scaling curve for a chat endpoint (non-streaming)

The chat tool reports per-request streaming metrics but has no telemetry join. For the *capacity* story (concurrency → GPU_UTIL/VRAM/power), drive the same endpoint through the universal tool — see §2.2, with `--method POST --path /v1/chat/completions --body '{"model": "<model>", "messages": [{"role": "user", "content": "{query}"}], "max_tokens": 256}'`.

### Gotchas (chat)

- **Pool-bound ≠ server-bound.** `MODEL_POOL_MAX_CONNECTIONS` (default 128) below your max users → high-percentile TTFT is your client queueing, not the model. The tool warns; raise the env var.
- **`--quiet` when piping.** Per-request prints run on the event loop; a backpressured stdout distorts TTFT.
- **DeepSeek caveat.** Premier models carry teammates' live traffic — numbers include their load.

---

## 2. MCP benchmarking (general — any MCP server)

Works against **any HTTP MCP endpoint** (streamable-http default; `--transport sse` for older servers). Stdio-only servers need an HTTP shim first — the benchmarker speaks HTTP, not stdio.

### 2.0 Step zero — discovery (always do this first)

```bash
eb --mode mcp --url https://rag.example.com/mcp --list-tools
```

Prints each tool: name, required args, and the inferred query argument:

```
  search_dataset  (required: query, dataset_name)  query -> 'query'
```

If the inference is wrong, force it with `--query-arg <name>`; fill required args with `--arg K=V` (values JSON-parsed: `top_k=10` → int, `use_reranker=true` → bool). A required argument that can't be filled aborts loudly — nothing is silently sent empty.

### 2.1 Single-level load — "what does N users look like?"

```bash
eb --mode mcp --url https://rag.example.com/mcp \
  --dataset their-dataset \
  -N 32 --duration 120 \
  --queries-file their_queries.txt \
  --output run.json --md results/their-system/N32.md
```

Each simulated user holds its own persistent MCP session for the whole level (same connection model as a real LLM client). Warm-up round (default 1) is unmeasured.

### 2.2 The scaling sweep + GPU telemetry (the hosted-trial run)

```bash
eb --mode mcp --url https://rag.example.com/mcp \
  --dataset their-dataset \
  --sweep 1,4,32,64 \
  --duration 180 --settle 30 \
  --prom-url http://prometheus:9090 \
  --prom-selector 'exported_namespace="their-ns"' \
  --baseline-duration 120 \
  --output results/their-system/scaling.json \
  --csv results/their-system/scaling.csv \
  --md results/their-system/H200x4_scaling.md \
  --html results/their-system/report.html \
  --note 'warm cache' --note '4x H200, HiCache x3' \
  --log-file results/their-system/scaling.log
```

Rules of thumb:

| Knob | Rule |
|---|---|
| `--duration` | ≥ 3× the Prometheus scrape interval (~15s DCGM) → 120–180s. Shorter windows produce 1–2-sample GPU averages; the report flags them (`note:` lines + Samples column). |
| `--settle` | 30s default; GPU utilization decays asynchronously after load stops. The next level's telemetry fetch overlaps the sleep, so settle is nearly free. |
| `--prom-selector` | `exported_namespace="their-ns"` (DCGM k8s pod-mapping) attributes utilization to the customer's pods; fallback `Hostname="..."` per node. |
| `--baseline-duration` | 60–120s on shared clusters — the idle baseline that makes `mean−idle` the honest number. |
| `--knee-factor` | 2.0 default (p99 doubles = knee). Re-tune later without re-running: `eb --from run.json --knee-factor 1.5 --md new.md`. |

Read the curve: **knee point first**, then `rps/user` (flat until the knee = healthy linear scaling), `GPU_UTIL −idle`, `FB_USED` (VRAM breaks first at high concurrency), `POWER_USAGE` (watts ≈ real work).

### 2.3 How tool/argument resolution works

- **Tool pick:** exact `--tool`, else a single tool as-is, else the first search-ish name (`search`, `query`, `ask`, `retrieve`, `find`, `lookup`).
- **Query arg:** first match of (`query`, `q`, `prompt`, `text`, `search`, `question`, `input`, `message`) that is required — else the first required string property. Override: `--query-arg`.
- **Dataset arg:** `--dataset` fills (`dataset_name`, `dataset`, `dataset_id`, `collection`, `collection_name`, `index`) when required.
- **Everything else:** `--arg K=V`, repeatable.

### 2.4 Re-render / compare after the fact

```bash
eb --from results/their-system/scaling.json --knee-factor 1.5 \
   --md results/their-system/scaling_knee15.md --csv results/their-system/scaling_knee15.csv
```

---

## 3. Worked examples

### 3.1 RAG MCP (`search_dataset`) — the MM-RAG shape

```bash
# Discovery first:
eb --mode mcp --url http://rag-mcp-server-mcp.mm-rag.svc.cluster.local:9090/mcp --list-tools

# No-reranker baseline vs reranker A/B (the cross-encoder dominates latency — measure both):
eb --mode mcp --url http://rag-mcp-server-mcp.mm-rag.svc.cluster.local:9090/mcp \
  --dataset andrew-test-dataset -N 100 --duration 120 \
  --arg top_k=10 \
  --output rag_noreranker.json

eb --mode mcp --url http://rag-mcp-server-mcp.mm-rag.svc.cluster.local:9090/mcp \
  --dataset andrew-test-dataset -N 100 --duration 120 \
  --arg top_k=10 --arg use_reranker=true --arg reranker_top_k=3 \
  --output rag_reranker.json
```

Historical expectations (SE G2, `helm-scale-medium`, N=100): ~44–49 r/s without the reranker; **~3.4 r/s with it** (~20× mean latency) — the A/B pair is the deliverable.

Variants:
- **REST parity** (the API path, no MCP): default flags already match — `eb --url http://rag-mcp-server-api.mm-rag.svc.cluster.local --dataset <ds> -N 100` (GET `/api/datasets/{dataset}/search?q=...`, auto dataset discovery).
- **GPU scaling with the sweep** (§2.2) using `--prom-selector 'exported_namespace="mm-rag"'`.
- **VLM-query pool**: `--query-set vlm` (or `mixed`) trips the server's VLM-on-query path — a different (much slower) curve than `generic`. Say which pool the report represents: `--note 'query-set: vlm'`.

### 3.2 SQL MCP (`run_sql` / `execute_query`)

SQL tools take **SQL statements** as the query pool, not prose. One statement per line in the pool file (`#` comments allowed; multi-line SQL does not fit the one-query-per-line format).

```bash
# queries.sql — one SELECT per line, deliberately cost-varied:
# SELECT COUNT(*) FROM work_order_header
# SELECT status, COUNT(*) FROM work_order_header GROUP BY status
# SELECT * FROM work_order_header ORDER BY created_at DESC LIMIT 100
```

```bash
# Discovery (SQLhandler-style server):
eb --mode mcp --url http://sql-mcp.<ns>.svc.cluster.local:8080/mcp --list-tools

# ezpresto-style: the query arg is literally 'query' → auto-detected.
# SQLhandler-style: the arg is 'sql' → NOT in the auto-detect list; force it:
eb --mode mcp --url http://sql-mcp.<ns>.svc.cluster.local:8080/mcp \
  --tool run_sql \
  --query-arg sql \
  --queries-file queries.sql \
  -N 16 --duration 60 \
  --output sql_run.json --md results/sql-mcp/run_sql_N16.md
```

The **noop-baseline trick** — separate protocol cost from query cost by benchmarking a near-noop tool alongside the real one:

```bash
# list_tables (cheap, no engine round-trip per row) = the MCP/session overhead floor:
eb --mode mcp --url http://sql-mcp.<ns>.svc.cluster.local:8080/mcp \
  --tool list_tables -N 16 --duration 60 --output sql_noop.json

# run_sql = overhead + engine cost. (run_sql − list_tables) ≈ what the engine pays.
```

SQL-specific cautions:
- **Statement cost variance is the whole story.** Pool statements deliberately: mix trivial (`SELECT 1`-class), medium (aggregations), heavy (large scans with `LIMIT`). One heavy statement in the pool shows up as the p99/knee — that's signal, not noise; use `--percentiles 50,90,99` and read the error/latency spread.
- **Read-only discipline:** SELECT-only pool files; the benchmarker samples the pool verbatim. Never point it at a pool with DDL/DML.
- **Engine-side pools:** each simulated user holds a persistent MCP session — behind the MCP server the engine may still serialize on its own connection pool; the noop baseline tells you which side saturates.
- **Parametrized tools** (`--arg catalog=x --arg schema=y` for attached-database servers): pin them explicitly so every sampled statement runs against the same catalog.

### 3.3 Any REST endpoint

```bash
eb --url https://their-rag.example.com \
  --method POST --path /api/v1/answer \
  --body '{"question": "{query}", "top_k": 5}' \
  --header 'Authorization=Bearer '"$TOKEN" \
  --queries-file their_queries.txt \
  -N 32 --duration 120 \
  --md results/their-api/N32.md
```

`{query}` splices JSON-escaped inside the body template; `{dataset}` in `--path` fills from `--dataset`; `--health-path ''` skips the health probe for APIs without one.

---

## 4. Artifacts — where things land

| Artifact | Flag | Consumers |
|---|---|---|
| JSON run artifact | `--output` | the machine-readable record; `--from` re-renders from it |
| Per-level CSV | `--csv` | spreadsheets |
| **Markdown report** | `--md` | humans + the results tree → `results_to_html.py` (RAG tab) |
| HTML report | `--html` | self-contained, attachable to email |
| Log tee | `--log-file` | the full leveled log |

Everything for the report goes under `results/<dir>/<SETUP>.md` (files directly in `results/` are ignored). Regenerate after adding files:

```bash
python3 src/model_benchmarker/results_to_html.py --open
```

## 5. Troubleshooting quick table

| Symptom | Likely cause | Fix |
|---|---|---|
| `required argument could not be filled` | tool needs an arg outside dataset/query slots | `--arg K=V` (and `--list-tools` to see it) |
| Query went into the wrong tool arg | auto-detection mismatched | `--query-arg <name>` |
| Every request failed in MCP mode | stdio server / wrong URL / transport | confirm HTTP MCP endpoint; `--transport sse` for old servers |
| GPU numbers rest on 1–2 samples | level shorter than ~3 scrape intervals | `--duration 120+`; check Samples column / `note:` lines |
| GPU_UTIL suspiciously high on shared nodes | neighbor contamination | `--baseline-duration 120`, read `mean−idle` |
| p99 blows up but p50 fine, REST mode | client pool ceiling | raise `MODEL_POOL_MAX_CONNECTIONS` (chat tool) — universal tool sizes its pool per level automatically |
| Knee at N=1 with everything "failed" | all requests erroring (auth/4xx) | read the error deep-dive table; fix auth first — knee detection fires on error rate |
