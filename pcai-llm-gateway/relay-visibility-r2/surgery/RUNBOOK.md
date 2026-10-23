# DSH pod surgery — Wave R1 relay-render (pod: dsh-user-andrew-bydlon-54d76d56c4-r4zb4)

`k` = `https_proxy=socks5://127.0.0.1:1080 kubectl --kubeconfig ~/SSH/G2/g2_kubeconfig.yaml`
NS=project-user-andrew-bydlon, POD=dsh-user-andrew-bydlon-54d76d56c4-r4zb4
KIT=~/Code/HPE/PCAI_LLM/relay-visibility-r2

Baseline verified live 2026-10-01: pod runs DSH 0.2.0-rc.1; pi-ai dist is PRISTINE
(64241 bytes, 2× parseStreamingJson) — the full merged patch applies; dsh-llm-pi-ai
lib is byte-identical to the kit's orig (113953 bytes). /var/dsh/data/npm → /opt/dsh/npm
(image layer, ephemeral) — hence the restore hook.

## 0. Preconditions (each must pass before proceeding)
```
k get pod dsh-user-andrew-bydlon-54d76d56c4-r4zb4 -n project-user-andrew-bydlon -o jsonpath='{.spec.containers[*].name}'   # expect: dsh-web dsh-preview-proxy dsh-auth
k exec dsh-user-andrew-bydlon-54d76d56c4-r4zb4 -n project-user-andrew-bydlon -c dsh-web -- cat /opt/dsh/npm/.dsh-version            # expect: 0.2.0-rc.1
k exec dsh-user-andrew-bydlon-54d76d56c4-r4zb4 -n project-user-andrew-bydlon -c dsh-web -- wc -c /opt/dsh/npm/lib/node_modules/@deepseek-ai/dsh/node_modules/@earendil-works/pi-ai/dist/api/openai-completions.js   # expect: 64241
k exec dsh-user-andrew-bydlon-54d76d56c4-r4zb4 -n project-user-andrew-bydlon -c dsh-web -- wc -c /opt/dsh/npm/lib/node_modules/@deepseek-ai/dsh/node_modules/@deepseek-ai/dsh-llm-pi-ai/lib/index.js               # expect: 113953
```
If any baseline differs: STOP (different base → regenerate the kit; do not patch blind).

## 1. Stage the patched files + hook on the PVC (durable across restarts)
```
cd $KIT/surgery/stage
k cp openai-completions.js project-user-andrew-bydlon/dsh-user-andrew-bydlon-54d76d56c4-r4zb4:/var/dsh/data/relay-r2/openai-completions.js -c dsh-web
k cp lib-index.js          project-user-andrew-bydlon/dsh-user-andrew-bydlon-54d76d56c4-r4zb4:/var/dsh/data/relay-r2/lib-index.js          -c dsh-web
k cp relay-restore.sh      project-user-andrew-bydlon/dsh-user-andrew-bydlon-54d76d56c4-r4zb4:/var/dsh/data/relay-r2/relay-restore.sh      -c dsh-web
```
(kubectl cp creates the dir? it does NOT — pre-create via exec mkdir if needed; mkdir IS allowlisted… but exec allowlist ≠ pod. kubectl cp uses tar in-pod; the dir must exist. Use: `k exec … -c dsh-web -- mkdir -p /var/dsh/data/relay-r2` FIRST.)

## 2. Patch the live tree (root exec; in-place, same inode, image-layer copy)
```
k exec dsh-user-andrew-bydlon-54d76d56c4-r4zb4 -n project-user-andrew-bydlon -c dsh-web -- cp /var/dsh/data/relay-r2/openai-completions.js /opt/dsh/npm/lib/node_modules/@deepseek-ai/dsh/node_modules/@earendil-works/pi-ai/dist/api/openai-completions.js
k exec dsh-user-andrew-bydlon-54d76d56c4-r4zb4 -n project-user-andrew-bydlon -c dsh-web -- cp /var/dsh/data/relay-r2/lib-index.js          /opt/dsh/npm/lib/node_modules/@deepseek-ai/dsh/node_modules/@deepseek-ai/dsh-llm-pi-ai/lib/index.js
```

## 3. Verify the patch is live in the install tree
```
k exec … -c dsh-web -- grep -c pcaiRelay /opt/dsh/npm/lib/node_modules/@deepseek-ai/dsh/node_modules/@earendil-works/pi-ai/dist/api/openai-completions.js   # expect: 2
k exec … -c dsh-web -- grep -c pcaiRelay /opt/dsh/npm/lib/node_modules/@deepseek-ai/dsh/node_modules/@deepseek-ai/dsh-llm-pi-ai/lib/index.js               # expect: 2  (r2 lib references pcaiRelay on 2 lines — runbook previously misstated 4)
k exec … -c dsh-web -- wc -c /opt/dsh/npm/lib/node_modules/@deepseek-ai/dsh/node_modules/@earendil-works/pi-ai/dist/api/openai-completions.js               # expect: 65643
k exec … -c dsh-web -- wc -c /opt/dsh/npm/lib/node_modules/@deepseek-ai/dsh/node_modules/@deepseek-ai/dsh-llm-pi-ai/lib/index.js                           # expect: 117419
```

## 4. Install the self-heal hook into dsh-startup.sh (PVC-backed)
Append BEFORE the first `start_services` invocation (the `start_services()` call
path) — simplest correct placement: immediately after the `ln -sf /var/dsh/data/npm/bin/dsh …` line:
```
k exec … -c dsh-web -- bash -lc 'grep -q relay-restore /var/dsh/bin/dsh-startup.sh || sed -i "/ln -sf \/var\/dsh\/data\/npm\/bin\/dsh/a\\
# Wave R1 relay-render: re-apply patched files after every restart (idempotent, version-guarded)\\
/var/dsh/data/relay-r2/relay-restore.sh" /var/dsh/bin/dsh-startup.sh'
k exec … -c dsh-web -- grep -n relay-restore /var/dsh/bin/dsh-startup.sh   # expect the 2 new lines
```
(If sed is unavailable in the exec path, do it with a single `bash -lc` python3 one-liner — python3 IS in the pod.)

## 5. Restart dsh web (supervisor auto-restarts in ~1s; sessions unaffected — they persist on the PVC)
```
k exec … -c dsh-web -- pkill -f "dsh web"
```

## 6. Opt in — the provider header (config change triggers a supervisor restart too)
Edit /var/dsh/home/.dsh/profiles/web/cordis.patch.yml → the pcai-model-endpoint-flow
provider `headers:` block (next to X-PCAI-Execute-Tools) add:
```
          X-PCAI-Relay-Deltas: "1"
```
(`kubectl exec` + python3 one-liner, or edit via the DSH UI config editor / your usual
route. The dsh_supervisor watches cordis.patch.yml and restarts dsh automatically.)

## 7. End-to-end verify
- Open DSH, ask something that triggers a gateway-relayed MCP call.
- Expect: a tool-call card for the relayed call, plus (Think) the enriched
  `[tool] server__tool(...) → ok (Nms)` + `↳ preview` line.
- Gateway console: the call shows phase=tool rows as usual.

## Rollback (clean)
```
# restore hook line removal:
k exec … -c dsh-web -- sed -i '/relay-restore/d' /var/dsh/bin/dsh-startup.sh
# pristine restore is automatic on the NEXT pod restart (image layer wins);
# for immediate revert without restart, re-extract from the image:
#   (reinstall branch) k exec … -c dsh-web -- bash -lc 'cd /tmp && npm pack @deepseek-ai/dsh@0.2.0-rc.1 …'  — impractical; simpler: delete the pod (PVC persists; init reinstalls pristine).
k delete pod dsh-user-andrew-bydlon-54d76d56c4-r4zb4 -n project-user-andrew-bydlon
# then remove the hook line + /var/dsh/data/relay-r2 when the new pod is up.
```

## Durability notes
- /opt/dsh is the image layer: any pod restart wipes the patch — the hook re-applies
  it before dsh starts (that's why stage lives on the PVC).
- On a future DSH image/version bump, the hook's version guard makes it a no-op
  (and logs the skip) instead of corrupting a new install tree. Regenerate the kit
  then (r2 repo flow: apply-dsh.sh + publish).

## 4b. HOOK-LINE CORRECTION (2026-10-01 10:30 — REQUIRED)
The step-4 sed collapsed the comment and invocation onto ONE line (the
invocation became part of the `#` comment → the hook never runs). Fix with the
base64 payload (fish/quote-safe; executes python3 in-pod to re-split the line):
```
k exec $P -n $NS -c dsh-web -- bash -c 'echo <B64> | base64 -d | python3'
```
Payload file: stage/hookfix.b64 (988 chars). Expected output:
`FIXED: hook split into comment + guarded invocation`
Verify after:
```
k exec $P -n $NS -c dsh-web -- tail -n +32 /var/dsh/bin/dsh-startup.sh | head -4
# expect:  comment line, then  [ -x /var/dsh/data/relay-r2/relay-restore.sh ] && /var/dsh/data/relay-r2/relay-restore.sh
```
And run the hook ONCE now to confirm it exits cleanly (tree already patched):
```
k exec $P -n $NS -c dsh-web -- /var/dsh/data/relay-r2/relay-restore.sh; echo "exit=$?"
# expect: exit=0, no output (already patched branch)
```
