# OpenWork Web — Multi-User on HPE Private Cloud AI (P1 scaffold)

Multi-user [OpenWork](https://github.com/different-ai/openwork) deployment on **HPE Private Cloud AI**,
ported from the sibling `opencode-web-helm` chart. Each user gets their own PVC-backed workspace;
a shared workspace enables team collaboration.

> **Status: P1 + P2 + P3 template strip done.** Chart lints/renders clean; unit image bakes
> openwork-server + the pinned OpenCode engine (D1/D4/D5); router and preview supervisor
> fully stripped of OpenChamber machinery and rewired to `openwork-server web` on 127.0.0.1:8787
> (rendered router/preview JS passes `node --check`). Remaining before deploy: S1/S2 spikes
> (loopback/Origin behavior + headless UI flows) and config-surface wiring (P5).

## Requirements / build spec

The full DS×GLM cross-validated build spec lives in [`REQUIREMENTS.md`](REQUIREMENTS.md):
- D1: `openwork-server web` single-origin runtime on 127.0.0.1:8787 behind the nginx sidecar (:8082)
- D2: loopback-only server bind; nginx is the sole non-loopback listener
- D3: router cookie gate is the auth boundary; OpenWork bearer tokens as defence-in-depth
- D4: zero `ee/` (OpenWork Den) code — apps/ + packages/ are MIT
- S1/S2 spikes are hard gates before template work continues

## Layout

```
templates/        16 templates, mechanically renamed (openwork-*)
docker/user/      unit image (P2: rewrite to bake openwork-server + pinned OpenCode engine)
scripts/          build/test/push/cleanup scripts (renamed; P2: adapt for openwork)
values.yaml       reference-derived + openwork.* block (serverVersion/opencodeVersion pins)
values.yaml.opencode-reference   pristine copy of the opencode chart values, port provenance
```

## Pushing config to user envs

`scripts/push_openwork_json_config.sh` **stages** an `opencode.json` into user envs
(`~/.config/opencode/` under the openwork home — the filename is unchanged from the
opencode chart). The push lands as `opencode-update.json` beside the live
`opencode.json`, which is never written: opencode resolves its config from a fixed
candidate list (`opencode.jsonc`/`opencode.json`/`config.json`, not a glob) and the
supervisor watches an explicit file list, so the sibling is inert — nothing reloads
until you promote it (the script prints the exact per-unit command). `--user` scopes a
push to specific users; `TARGET_NAME=opencode.json` restores the old overwrite behaviour.

## Machine gates passed (P1)

- `helm lint` — clean
- `helm template` — 19 docs render; zero stale `opencode-*` identifiers
- VirtualService host: `openwork.${DOMAIN_NAME}`
