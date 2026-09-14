# CLI reference — `benchmark_chat.py`

Complete flag, environment and failure-behavior reference for
[`src/model_benchmarker/benchmark_chat.py`](../src/model_benchmarker/benchmark_chat.py).
For the tool's purpose and worked scenarios see the repo
[`README.md`](../README.md); for result files and the HTML report see
[`results-and-report.md`](results-and-report.md).

Run by path from anywhere in the repo (the script bootstraps `sys.path`):

```bash
python src/model_benchmarker/benchmark_chat.py --help
```

Flag style is mixed: most flags use underscores exactly as written
(`--model_class_name`, `--number_users`); `--no-nonce` is the only
dash-style flag.

## Model / endpoint selection

| Flag | Description |
|---|---|
| `--model_class_name NAME` | Use a registered model from `src/model_benchmarker/utils/pcai_models.py` (chat entries: `deepseek_v4_flash_280B`, `qwen38_27B`, `qwen36_27B`, `gemma4_31B`, `glm_52_753B`, `glm_53_flash_331B`). Imported lazily, only when this flag is used. **Caveat:** entries with `currently_deployed=False` (currently `qwen36_27B`, `glm_52_753B`) fail every request — pick a deployed entry or use `--url`. The module embeds cluster-internal URLs and bearer keys and is excluded from the hardlinked deployment, where this flag is unavailable. |
| `--url URL` | OpenAI-compatible endpoint root, **no `/v1`** (the client appends it). Required when `--model_class_name` is omitted. If the served model id is not set, it is auto-discovered via `GET {base}/v1/models` (first listed id; cache bounded by `DISCOVERED_MODEL_CACHE_MAX`). |
| `--api_key KEY` | Bearer token. Optional — omit for endpoints that don't require auth and no `Authorization` header is sent. Prefer `--api_key_file` / `$PCAI_API_KEY` so the secret stays out of shell history and `ps`. |
| `--api_key_file FILE` | Read the bearer token from a file (first line, stripped). Priority: `--api_key` > `--api_key_file` > `$PCAI_API_KEY`. |
| `--remote` | Use the model's **public serving URL**. Without it, a registered model targets its in-cluster URL (`*.svc.cluster.local`-style service DNS). Omit `--remote` when running inside the same cluster. `--url` endpoints are always used as given. |

Endpoints here are model endpoints deployed through MLIS on PCAI — this tool
only sends OpenAI-compatible chat-completion requests to them; it performs no
deployment itself.

## Load shape

| Flag | Default | Description |
|---|---|---|
| `--number_users N,M,…` | `1` | Concurrency levels to sweep. All conversations start simultaneously — no ramp. |
| `--requests_per_user N` | `1` | Sequential requests per user, cycling through `--tasks` (each round of `len(tasks)` requests is a freshly shuffled permutation per user, so the mix stays even). |
| `--tasks t1,t2,…` | `coding,creative` | Task types: `coding` (max_tokens 8192), `creative` (16384), `mixed` (24576 — one request combining coding + creative), `custom`. |
| `--context_length 0,…` | `0` | Prefill-length sweep; each level pads every prompt to ≈ that many input tokens (≈4 chars/token filler text). `0` disables padding. With `--multiturn` only **turn 1** is padded — later turns reuse the prefix. |
| `--multiturn` | off | Each user runs `--requests_per_user` **turns of one growing conversation**: full prior user+assistant history replayed each turn, only the newest user prompt gets a fresh nonce. Assistant turns replay `content` only — reasoning tokens are not resent (matches real chat clients; runs made before this behavior are not directly comparable). Gives the `turn1` vs `TTFT-post` split. |
| `--separate_tasks` | off | Run each task in its own (ctx, users) pass — every task gets a clean turn-1 vs turns-post comparison without cross-task interference. Status counts one level per (ctx, users, task). |
| `--prompt TEXT` | built-in essay | Prompt for the `custom` task. |
| `--max_tokens N` | per task | Override output cap for every task. |
| `--temperature F` | per task | Override sampling temperature (built-ins use 1.0). |
| `--top_p F` | per task | Override top_p (built-ins: coding 0.95, else 1.0). |

## Cache behavior

| Flag | Description |
|---|---|
| `--no-nonce` | Replace the per-request random nonce with a fixed shared marker (`my-shared-benchmark-prefix`), so every request across all users shares one identical prefix and the server's prefix cache can be reused across users. Isolates cache-sharing benefit from cold-prefill cost. |
| `--prewarm` | Before each (ctx) level, fire one shared-prefix request per task so the prefix graph is populated *ahead of* the users. Pairs with `--no-nonce`; alone it warms a prefix no request will reuse. |

## Reasoning / sampling overrides

| Flag | Description |
|---|---|
| `--enable_thinking` / `--disable_thinking` | Force `chat_template_kwargs.enable_thinking` True/False (Qwen3-style). Default leaves the server default. |
| `--thinking_budget N` | Cap reasoning tokens via `chat_template_kwargs.thinking_token_budget`. Must be smaller than the task's max_tokens, or the model may spend the whole budget reasoning and TTFT reads 0. |
| `--thinking_level LEVEL` | Preset budget: `off`=0, `low`=1024, `medium`=4096, `high`=16384, `x-high`=32768. Overrides `--thinking_budget`. |

## Diagnostics / output

| Flag | Description |
|---|---|
| `--debug_stream` | Print the first delta's field names/values and per-request content vs reasoning chunk counts — diagnose TTFT=0 when a server streams text under a non-`content` delta field. |
| `--quiet` | Suppress per-request progress lines (keeps prewarm notices, level previews, the final table). Recommended when stdout is piped/redirected: per-request `print()` runs on the event loop, and a backpressured stdout can stall all streams and distort TTFT/tokens-s. |
| `--output FILE` | Write the summary table to FILE (in addition to stdout), rewritten after **every completed sweep level** and stamped with a `Status:` line (`complete`, `in progress — k/n levels`, `INTERRUPTED…`, `CRASHED…`). Writes are atomic (`.partial` temp file + `os.replace`), so a killed run never leaves a truncated file. The report generator only picks the file up once at least one level succeeded. Result files must live under a per-model subdirectory (`results/<model-dir>/<SETUP>.md`). |

## Environment variables

| Var | Default | Purpose |
|---|---|---|
| `MODEL_POOL_MAX_CONNECTIONS` | `128` | httpx connection-pool ceiling. With fewer connections than the concurrency level, requests serialize client-side and high-percentile TTFT blows up — the benchmark becomes pool-bound, not server-bound. Raise to match your max `--number_users`; the script prints a warning when a level exceeds it. |
| `MODEL_POOL_MAX_KEEPALIVE_CONNECTIONS` | `32` | Idle keep-alive connections kept by the pool. Below your concurrency level, wave boundaries pay fresh TCP+TLS handshakes. |
| `PCAI_API_KEY` | unset | Fallback bearer token (lowest priority). |
| `REMOTE_CA_BUNDLE` | unset | Path to a CA bundle (`.pem`/`.crt`). When set (and the file exists), TLS verification is **on** for remote endpoints against that bundle; when unset, remote endpoints run with verification **disabled** (self-signed PCAI ingress) and a one-time `[security]` warning is printed to stderr. |
| `DISCOVERED_MODEL_CACHE_MAX` | `512` | Bound on the per-URL `/v1/models` model-name discovery cache. |
| `SYNC_POOL_SIZE` | `12` | Threads in the sync→async bridge pool of the shared platform modules. |

## Timeouts and failure behavior

- Request timeout is `connect=30s, read=600s` (`_MODEL_REQUEST_TIMEOUT` in `utils/pcai_model_classes.py`), applied to both the httpx clients and the OpenAI SDK clients (the SDK otherwise ignores an injected httpx client's timeout and falls back to its own `connect=5s`, turning slow high-concurrency handshakes into spurious failures).
- Very long generations at high concurrency can still log `FAILED: Request timed out.` — that is the server being saturated, not a client bug.
- If **all** requests in a level fail, the level is skipped (rows omitted), the first error is printed, and the sweep continues to the next level.
- Failed requests are excluded from percentile stats but counted in the `failed` column.
- Invalid values (`--number_users abc`, negative `--context_length`, `--requests_per_user < 1`, unknown task or model class names) exit immediately with a message before any traffic is sent.
