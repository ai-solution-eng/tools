---
name: pcai-app-import
description: "Use this skill when the user wants to create a deployment artifact for HPE Private Cloud AI (PCAI): packaging an already-ported PCAI Helm chart into an EzAppConfig custom resource (logo, values, install options) that imports into PCAI AI Essentials / Ezmeral and deploys the app on a PCAI unit. Trigger whenever the user mentions a PCAI deployment artifact or package, an EzAppConfig, importing an app or framework into PCAI / AI Essentials, or deploying an app to a PCAI unit, even without naming the artifact. If the chart is not yet ported (no ezua block or VirtualService), this skill hands off to the pcai-helm-port skill first. Do NOT use for generic Helm packaging, generic Kubernetes manifests, or chart porting with no PCAI import intent (that is pcai-helm-port's job)."
---

# Create a PCAI Deployment Artifact (EzAppConfig)

## Goal

Turn a Helm chart that is already ported to HPE Private Cloud AI (PCAI) into a running deployment on a PCAI unit: package the chart into an `EzAppConfig` custom resource (app logo, user-approved values, install options), then deploy it — chart uploaded to ChartMuseum and CR applied — through the `ezapp-deploy` MCP server.

The companion skill `pcai-helm-port` ports charts to PCAI; this skill packages a ported chart and ships it. The workflow has exactly one interactive stop — when the user reviews the values — so the deployed artifact always reflects what the user actually approved.

## Paths

This skill bundles helper scripts and assets next to this file. `<skill-dir>` in the commands below means **the directory containing this SKILL.md** — OpenCode reports it as the skill's location when the skill is loaded (e.g. `<project>/.opencode/skills/pcai-app-import/` for a project install, or `~/.config/opencode/skills/pcai-app-import/` for a global one). Resolve bundled files from that location; never reconstruct paths from the project root, since the two install layouts differ.

## Workflow

### 1. Confirm the chart is ported

A chart is ported when it carries the PCAI integration: an `ezua:` block in `values.yaml` and/or a `kind: VirtualService` template. Locate the chart in the current workspace:

- If the user pointed at a chart directory or a `.tgz`, use it (unpack a `.tgz` first).
- Otherwise search the workspace (e.g. `charts/*/Chart.yaml`, or a root-level `Chart.yaml`). If several charts match, ask the user which one.
- Read the chart's `values.yaml` and `templates/`. If there is no `ezua` block or no VirtualService, the chart is **not** ported: execute the `pcai-helm-port` skill first, then continue this workflow with the ported chart.

Also read `Chart.yaml` now — its `name` and `version` anchor everything that follows.

### 2. Source the icon

The CR's `logoImage` needs a small PNG. Try these sources in order, keep the chosen file in the workspace, and remember its path (you will base64-encode it later):

1. **Existing PNG in the workspace.** Look for `*.png` files in the workspace root, the chart directory, and any `assets/` or `images/` directories. A suitable logo is square-ish, small (≈256px or less), small file size, and actually looks like the project's logo — prefer files named `logo` or `icon`. Skip screenshots and photos. If the chosen PNG is oversized, downscale it (e.g. `sips -Z 256` on macOS, or ImageMagick).
2. **Web search, if a web search tool is available in this session.** Find the app's official logo in PNG format, download it into the workspace, and use it.
3. **Generate one.** Write a simple SVG that resembles what the chart deploys (a database cylinder for a data app, a chat bubble for an LLM UI, a stylized first letter of the app name, etc.), then convert it with the bundled converter: `python3 <skill-dir>/scripts/svg_to_png.py icon.svg logo.png 256`. The script tries resvg-py, cairosvg, rsvg-convert, and ImageMagick in turn; if none is installed it copies the skill's bundled `assets/fallback-icon.png` instead, so it always produces a PNG on any OS with nothing installed. **Do not improvise other renderers — macOS qlmanage in particular**: QuickLook mis-transforms SVG geometry (shifted, cropped renders) and flattens transparency onto white, which produces clipped-looking logos. When the converter reports the fallback was used, **offer to install a renderer** in a single `question` tool call with these options: `pip install resvg-py` (Recommended — self-contained wheel, no system libraries), `brew install librsvg`, or keep the generic fallback. If an install is accepted, run it, re-run the converter, and confirm the output line names a renderer rather than the fallback before moving on — note cairosvg can still fail at import on a bare macOS (it needs the cairo system library), in which case use resvg-py. If declined, keep the fallback icon and say so. Fetching the official logo via web search (source 2) is the other path to an app-faithful icon. Tell the user which route produced the icon. The bundled `assets/fallback-icon.svg` doubles as a style example for drawing the app-specific SVG.

### 3. Ask the user the one-shot questions

This step is a required interactive stop — never answer these questions yourself or silently use defaults. Call the `question` tool exactly once, with a `questions` array containing all four of these entries (this gives the user one form to confirm in a single keystroke):

1. **category** — options: `dataScience` (Recommended), `dataEngineering`, `analytics`
2. **namespace** — suggested option: the chart name (Recommended); the user can type a different one
3. **label** — suggested option: the humanized chart name, e.g. `Arize Phoenix` for `arize-phoenix` (Recommended); user can type another
4. **description** — suggested option: same text as the label (Recommended); user can type another

If your session has no `question` tool, ask the same four questions in a plain chat message and end your turn, waiting for the user's reply before continuing. Either way, do not write `values-override.yaml` or start step 4 until the user has answered.

### 4. Write values-override.yaml

Copy the chart's `values.yaml` into `values-override.yaml` at the workspace root **byte-for-byte — no trimming, no reformatting, no "helpful" cleanups**. This file is the user's review surface: they approve exactly what will ship, so anything you silently drop is a value they never saw. This holds even when the chart's values are huge. If a very large values file threatens the tool-call cap later, that is handled at apply time (step 8, HTTP staging) — never by trimming here.

These values end up **only** in the EzAppConfig's `spec.values` — PCAI applies them as overrides on top of the chart's defaults at install time. They never touch the chart package, so editing this file never requires repackaging or re-uploading the chart.

Two things matter here:

- Keep `${DOMAIN_NAME}` **verbatim**. PCAI substitutes it at import time; replacing it breaks the endpoint.
- Keep the file valid YAML — the user will edit it by hand.

### 5. Pause for the user

This is the second required interactive stop. End your turn: tell the user that `values-override.yaml` is ready and contains the chart's values, that they should edit anything they want changed, and to say "done" when finished. Never package the CR in the same turn that you wrote the file — the artifact must reflect values the user actually approved. When the user says they are done, resume at step 6. (If `values-override.yaml` has gone missing when you resume, recreate it from the chart and pause again.)

### 6. Package the EzAppConfig

Read the edited `values-override.yaml` and build the CR in the workspace as `ezappconfig-<chart-name>.yaml`:

```yaml
apiVersion: ezconfig.hpe.ezaf.com/v1alpha1
kind: EzAppConfig
metadata:
  labels:
    hpe-ezua/imported-app: "true"
  name: <chart-name>-<chart-version>-<epoch-ms>
spec:
  backoffLimit: 3
  category: <from step 3>
  chartVersion: <from Chart.yaml>
  description: <from step 3>
  install: true
  label: <from step 3>
  logoImage: <base64 of the icon PNG>
  name: <exact chart name from Chart.yaml>
  options:
    create-namespace: "true"
    namespace: <from step 3>
    timeout: 45m
    wait: "true"
  values: |-
    <contents of the edited values-override.yaml, indented to sit inside this block scalar>
  version: ""
```

Field rules:

- `metadata.name` — `<chart name>-<chart version>-<epoch milliseconds>`, e.g. `arize-phoenix-4.0.17-1769614071004`. Milliseconds: `date +%s000` or `python3 -c 'import time; print(int(time.time()*1000))'`.
- `logoImage` — `base64 -i logo.png | tr -d '\n'` (macOS) or `base64 -w0 logo.png` (GNU). Strip every newline; embedded line breaks corrupt the logo.
- `name` — the exact chart name from `Chart.yaml`, not the label and not the directory name when those differ.
- Fixed fields, do not change: `backoffLimit: 3`, `install: true`, `options.create-namespace`, `options.timeout`, `options.wait`, `version: ""`.
- `category` — exactly one of `dataScience`, `dataEngineering`, `analytics`.
- **No cluster-populated fields.** If you are looking at a captured live CR (one with `creationTimestamp`, `uid`, `resourceVersion`, `generation`, or `finalizers`), leave those out — the cluster adds them on import.
- The `values` block scalar is the classic indentation trap: every line of `values-override.yaml` must be indented consistently under `values: |-`, with the file's own relative indentation preserved. Build the file and then verify (next step) rather than trusting your eyes.

### 7. Verify

Run the bundled verifier (dependency-free, needs only python3):

```
python3 <skill-dir>/scripts/verify_ezappconfig.py ezappconfig-<chart-name>.yaml values-override.yaml
```

It checks that the YAML parses, every required field is present, `logoImage` is valid base64 decoding to a real PNG, and the `spec.values` block matches `values-override.yaml` exactly. Fix anything it reports and re-run until it prints `OK`.

### 8. Deploy with the ezapp-deploy MCP server

The point of the artifact is a running app, so finish the job: upload the chart, then apply the CR, using the `ezapp-deploy` MCP server — its tools, plus its authenticated `/upload` and `/manifest` endpoints for large payloads. Chart first, CR second — PCAI's controller pulls the chart from ChartMuseum as soon as it processes the CR, so the chart must already be there.

If the `ezapp-deploy` tools are not available in this session, the server isn't connected: tell the user to connect it and stop. Do not fall back to hand-rolled `kubectl` or direct ChartMuseum API calls — the only sanctioned non-tool path is the server's own `/upload` endpoint (see the size-based upload below), which exists precisely so cluster access is mediated, validated, and ledger-tracked.

**Choosing among multiple ezapp-deploy servers.** The config can host several servers that each expose this same five-tool set (config names are unique keys, so expect environment-style names like `ezapp-deploy-lab` and `ezapp-deploy-prod`; tools are namespaced by server, so the tool list tells you how many candidates exist). If there is more than one, **ask the user which one to use** — one `question` tool call listing the candidate server names as options, since the name typically encodes the target cluster/ChartMuseum and only the user knows where this app belongs. Do not guess. Whichever the user picks, use that **same server for every operation in this run** — upload, apply, read-back, and any later rollback. Mixing servers splits the ledger: a chart uploaded via one is invisible to the other, deletes get refused, and a CR applied by the other server shows up as unmanaged.

1. **Prepare the package.** The chart package and the override values are independent: the `.tgz` carries the chart's default values, and the user's edits live only in the CR's `spec.values`. So if a PCAI `.tgz` already exists — in the workspace, or already uploaded to ChartMuseum from an earlier run — use it as-is; **do not repackage after the user edits `values-override.yaml`**. Package only when no suitable `.tgz` exists yet (chart is a directory after `pcai-helm-port` ports it in place: `helm package charts/<chart-name>/`) or when the chart content itself changed (then bump the version in `Chart.yaml` first). Whichever package you use, its `Chart.yaml` name/version must match the EzAppConfig's `spec.name` and `spec.chartVersion`.
2. **Upload the chart (pick by size).** Measure the package first — `wc -c <chart>.tgz` — because tool-call arguments are capped (~43 KB) and base64 inflates the file by 4/3×:
   - **Under ~30 KB**: call the `upload_chart` MCP tool with the base64 of the file (`base64 -i <chart>.tgz | tr -d '\n'` on macOS, `base64 -w0 <chart>.tgz` on GNU) and `filename` (the tgz's basename). Leave `force` at `false`.
   - **~30 KB or larger — or any sign the base64 path truncated at the cap**: upload the raw file with the shell. This is the server's own authenticated endpoint, not a bypass:
     ```
     curl -sS -H "Authorization: Bearer $EZAPP_DEPLOY_KEY" --data-binary @<chart>.tgz \
       -w '\n%{http_code}' https://<host>/upload
     ```
   - **From either path**, confirm the response reports the expected chart name+version (parsed from `Chart.yaml`) and that they match the EzAppConfig's `spec.name` and `spec.chartVersion`. A mismatch means the wrong package or a stale CR — fix before applying. HTTP `201` = created; `409` = that version already exists: if the chart content is unchanged (the common re-deploy case — only `values-override.yaml` changed), skip ahead and apply the CR; otherwise do **not** reach for `force` — bump the chart version in `Chart.yaml`, re-package, and upload again. Only retry with `force=true` when this same server uploaded that exact version before (its ledger tracks that); when in doubt, ask the user.
   - **Chunked trio — fallback only** (shell/curl unavailable in a locked-down runtime *and* the chart exceeds the single-shot tool-call cap): use `upload_chart_begin` → `upload_chart_chunk` (seq `0..N`, each exactly the `chunk_bytes` size the server specifies) → `upload_chart_commit`. Before starting, measure the file (`wc -c`) and hash it (`sha256sum`, or `shasum -a 256` on macOS) — the commit verifies the sha256 before anything reaches ChartMuseum. If the server reports these chunked tools don't exist, chunked upload is disabled (`EZAPP_MCP_CHUNKED_UPLOAD_ENABLED=false`): report that to the user and stop.
3. **Apply the CR (choose by size).** Measure it first — `wc -c ezappconfig-<chart-name>.yaml` — because a long `spec.values` plus the base64 `logoImage` can push the CR past the ~43 KB tool-call cap:
   - **Under ~30 KB**: call the MCP tool with the full text: `apply_ezappconfig(manifest_yaml=<contents of the CR file>)`.
   - **~30 KB or larger**: stage it over HTTP, then apply the id server-side:
     ```
     code=$(curl -sS -H "Authorization: Bearer $EZAPP_DEPLOY_KEY" \
          --data-binary @"$CR_YAML" -w '%{http_code}' -o /tmp/manifest.json \
          "$EZAPP_DEPLOY_URL/manifest")
     ```
     - `200` → read `"manifest_id"` from `/tmp/manifest.json` and call `apply_ezappconfig(manifest_id=<id>)`. Staged manifests are **single-use** and **expire after 15 minutes** — apply promptly; on expiry, re-POST.
     - `400` → the CR failed validation (wrong kind, missing fields, forbidden namespace). The generated CR must be a single YAML document with no `metadata.namespace`; the response states the reason — fix the CR, do not retry unchanged.
     - `413` → the CR exceeds the server cap: reduce it (smaller icon, trimmed values).
   - **Either path**: the CR is applied with field-manager `ezapp-deploy-mcp` and recorded in the ledger.
   - **If apply refuses because a CR with that name already exists but is unmanaged — STOP and ask the operator.** Never work around it: no manual `kubectl`, no renaming tricks. `get_ezappconfig` can show what the existing CR holds so the operator can decide (the default summary is usually enough; include values only if the block is small). One recognized exception: if the operator just ran a delete and the old CR is still terminating (see the rollback step below), the refusal is expected — wait for `get_ezappconfig` to return NotFound, then re-apply.
4. **Poll the install with `get_ezappconfig`.** Applying is asynchronous — PCAI's controller needs time. Pause for 1 minute (`sleep 60`), then call `get_ezappconfig(name=<metadata.name>)` and read its **default summary output**. Do **not** pass `include_values=true` — the values block plus the base64 logo will overrun the tool-result cap; only use it when you know the values block is very small, which is rarely needed just to check status. If the status is still transitional (e.g. `initialized`), wait another minute and poll again. On `error` or `warning`, report the failure reason to the user rather than assuming success.
5. **Rollback / uninstall, if the user asks:**
   - **`delete_ezappconfig(name)`** — one call. When the server *accepts* the delete, the ledger entry is cleared immediately, but the operator-side uninstall runs **asynchronously** via the CR's finalizer. Confirm completion by polling `get_ezappconfig(name)` until it returns **NotFound**. If you re-apply the same CR name while the old one is still terminating, the server refuses — wait for NotFound, then re-apply.
   - **A client-side timeout (`-32001`) on delete does NOT mean failure** — the DELETE was accepted. Re-check with `get_ezappconfig` and continue from there.
   - **Then `delete_chart(chart_name, chart_version)`** if the chart version should be freed — skip it when you plan to redeploy the same version (the 409 handling in the upload step makes redeploying over an existing version free). Both operations are ledger-gated — they only work for objects this server created — which is another reason not to bypass it.

### 9. Final message

Report to the user:

- The artifact path (`ezappconfig-<chart-name>.yaml`) and which icon was used
- Deployment result: chart `<name>-<version>` uploaded to ChartMuseum (or already present), CR `<metadata.name>` applied, and the final install status from the polling in step 8
- **The app URL.** Take the endpoint from the CR (`ezua.virtualService.endpoint`, e.g. `openbao.${DOMAIN_NAME}`) and substitute the cluster's real domain. The MCP host shares the cluster domain including subdomains — the app lives at `<app-name>.<domain>` and the MCP server at `<something>.<domain>` (e.g. MCP `https://ezapp-deploy-mcp.pcai-se-ai-application.hst.rdlabs.hpecorp.net`, app `https://openbao.pcai-se-ai-application.hst.rdlabs.hpecorp.net`). So the domain is the MCP hostname minus its first label:
  ```
  python3 - <<'EOF'
  import os, urllib.parse
  host = urllib.parse.urlparse(os.environ["EZAPP_DEPLOY_URL"]).hostname
  domain = host.split(".", 1)[1] if "." in host else host
  endpoint = "openbao.${DOMAIN_NAME}"  # from the CR's ezua.virtualService.endpoint
  print("https://" + endpoint.replace("${DOMAIN_NAME}", domain))
  EOF
  ```
  Give the resulting URL, and mention the app also shows up in the PCAI AI Essentials UI under Tools & Frameworks.
