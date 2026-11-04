---
name: rag-media-view
description: Use when asked to show, view, render, or display an image or video that lives in a MultimodalRAG dataset (a PCAI deployment), when a rag-mcp-server /api/datasets/.../files/... URL appears in markdown and fails to render, or when a tool result must become a viewable local file. Covers the SSO-gate root cause, the fetch-to-disk-and-render path, and the MCP_FILE_BYTES_MAX ceiling. Do NOT use for uploading media into a dataset or for dataset search itself.
---

# Viewing RAG dataset media

## The one thing to understand first

**A RAG media URL will almost never render as markdown in a browser on a
different origin.** Not a harness bug, not a config mistake — a browser rule.

Two independent gates stack up:

1. **The app host sits behind oauth2-proxy.** A `CUSTOM` AuthorizationPolicy on
   the istio-ingressgateway 302s *every* request on that host to Keycloak before
   FastAPI runs. That includes the HMAC-signed `?token=` URLs the MCP server
   mints precisely so `<img>` tags can fetch protected media. The app-level token
   is never evaluated.
2. **The gateway cookie is `SameSite=Lax`.** Lax cookies are sent on top-level
   navigation but NOT on cross-site subresource loads. So clicking the link works
   (you have a session); embedding it in a page on another origin does not (the
   `<img>` fires cookieless and is 302d again).
3. **The agent sandbox cannot reach that host AT ALL** (2026-10-29, measured).
   Outbound HTTP from `bash` goes through a stub proxy at `127.0.0.1:3128` that
   answers every request with a 723-byte HTML 302, and DNS for the media host
   does not resolve. `curl` and `web_fetch` are dead ends — one turn burned ~4
   steps rediscovering this. Do NOT probe URL reachability, and do NOT
   hand-assemble a signed URL from a guessed token (that produced an invented
   token AND an expired one): go straight to the MCP tool
   `get_dataset_file_bytes`, which authenticates server-side.

Measured, on a controlled host pair:

| Context | Cookie sent? | Renders? |
|---|---|---|
| Same-site `<img>` (app page -> app host) | yes | yes |
| Cross-site `<img>` (foreign page -> app host) | **no** | **no** |
| Top-level navigation (clicking the link) | yes | yes |

A same-site front end (e.g. Open WebUI on the same registrable domain) works for
exactly this reason — the cookie rides along. A local harness on `127.0.0.1` is a
foreign site, so it never does.

**Do not try to fix this client-side.** No CSP tweak, config flag, or harness
setting changes it. The browser will not attach a Lax cookie to a cross-site
subresource. Route around it.

## The working path: fetch bytes, write to disk, render the local file

**HOW TO CALL THE TOOLS (2026-10-29 — this changed, and the old instructions
here were wrong).** MCP tools arrive through the gateway's server-side RELAY,
not through `tools[...]` bindings inside `run_code`. Concretely:

1. `pcai_tools` is a DIRECT tool call (a normal function call, NOT inside
   `run_code`). Call it with `{action:"describe", server:"mm-rag"}`.
2. That PROMOTES mm-rag: its tools (`mm-rag_list_datasets`,
   `mm-rag_search_dataset`, `mm-rag_get_dataset_file_bytes`, …) become
   directly callable for the rest of THIS turn.
3. Call them directly. Do NOT look for `tools["<gateway>__mm-rag__…"]` inside
   `run_code` — that binding does not exist and hunting for it wastes turns.

**CONTEXT COST — read before fetching bytes.** With the relay, a tool result
enters the conversation verbatim, so `get_dataset_file_bytes` on a multi-hundred-
KB photo lands as base64 in context. Prefer a `_preprocessed` variant, cap the
size, and only fetch what you will actually render. The old "fetch inside the
program so the payload never reaches the model" trick required a programmatic
tool binding and no longer applies.

**Writing to disk.** Fetching bytes to a local file still needs a program that
receives them — call `get_dataset_file_bytes` directly and, if the payload is
small enough to carry, decode and write it with `run_code`.

Then render it with an **absolute local path** in markdown. The harness rewrites
absolute paths into its own authenticated file route (`api/file?path=...`) on its
own origin, so there is no cross-site fetch and no cookie problem:

```markdown
![description](/abs/path/to/photo.jpg)
```

Alternatively call the `read_image` tool on the written path — it attaches the
image inline and downscales automatically. Verified with a 16 MB source.

## Filenames: two forms, only one is stable

`get_dataset_files` returns the **stored** name with a 32-hex prefix:
`2f2e7aee2826424fa3feb7ad2560bdef_DSC01378_preprocessed.JPG`.

- `get_dataset_file_bytes` requires the **full stored name**.
- The REST file route also accepts the short name (uuid prefix stripped).
- The HMAC media token is bound to whichever form it was minted for — a token
  minted for the short name will NOT validate against the full path.

## The size ceiling

`get_dataset_file_bytes` refuses files over `MCP_FILE_BYTES_MAX` (default
**256 KiB**). There is **no offset/range parameter** — you cannot chunk around
it. The check is server-side and env-only.

Measured on a real 195-image dataset: 256 KiB covers 79%, 1 MiB 90%, 4 MiB 93%,
8 MiB 95%. The tail is brutal (largest was 29 MB), so pick a sane cap rather than
chasing 100%.

To raise it, set it via chart `extraEnv` (it reaches both containers):

```yaml
extraEnv:
  MCP_FILE_BYTES_MAX: "8388608"   # 8 MiB
```

Cost is **transient server RAM (~2x file size per call)**, not tokens — because
the bytes never enter context in PTC mode. Watch `MCP_POOL_SIZE`: worst case is
pool-size x 2 x cap of peak RAM.

## What NOT to do

- **Do not propose `--skip-auth-route` on the shared oauth2-proxy** to make URLs
  render. It is a platform-wide process: the flag weakens the pattern for every
  app behind that proxy, it strips the identity headers the app ACL check needs
  (`if identity is not None` — so the per-user dataset ACL silently stops being
  enforced), and it still does not make a tokenless URL work: the app then 403s
  because `registry_configured()` demands a token or password. If a gateway change
  is genuinely needed, ask for a host-scoped `notPaths` on that app only.
- **Do not set the cap to the largest file you can find.** Size it to the
  distribution you actually need and keep the pool-size multiplication in mind.
- **Do not build a local proxy** unless a *non-agent* consumer needs a stable URL
  (a browser tab, an external doc). For rendering inside an agent session,
  fetch-to-disk is simpler, has no listener to keep alive, and has no network
  dependency at all.

## Fast triage

| Symptom | Cause | Do |
|---|---|---|
| Image renders as bare alt text | cross-site Lax cookie, 302 to SSO | fetch bytes -> disk -> local path |
| `413` / "over the tool cap" | file > `MCP_FILE_BYTES_MAX` | raise via `extraEnv`, or use a `_preprocessed` variant |
| "File not found" for a name you can see | used the short name | use the full 32-hex-prefixed stored name |
| Token rejected (403) | token minted for the other filename form | re-mint against the exact path you request |
| Nothing renders, no error | gateway 302 returned HTML to an `<img>` | same as row 1 |
