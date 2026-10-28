# How to ship the relay fixes

**Short answer: you do not merge anything into DSH.** The fixes are in the
gateway (PCAI_LLM). DSH is a *client* of the gateway and needs no code change
for any of the fixes below — it already sends the headers and already
understands the stamp. You release the gateway; DSH picks the fixes up on the
next request.

There is exactly **one** DSH-side item, and it is not a merge — see
"What DSH still needs" at the bottom.

---

## What was fixed, and where it lives

| Fix | Repo | Deploys via |
|---|---|---|
| Reasoning-key blindness (CUT 1) | PCAI_LLM | gateway image |
| Tool-call assembly (F-2, F-3, F-4, E-1, E-2, E-3) | PCAI_LLM | gateway image |
| Render index collision (F-1) | PCAI_LLM | gateway image |
| Usage chunk on handback (F-7) | PCAI_LLM | gateway image |
| Retry gates (F-6, F-8, E-4) | PCAI_LLM | gateway image |
| Model-type detect-on-register | PCAI_LLM | gateway image + chart |
| Type selector in the console | PCAI_LLM | gateway image (admin.html is baked in) |

Nothing in this list is a DSH change. DSH's `X-PCAI-Relay-Deltas: 1` and the
patched pi-ai provider already do the right thing; they were being fed a
gateway that mangled the stream.

---

## The release (gateway)

```bash
cd /home/andrew/Code/HPE/PCAI_LLM
git status                     # confirm your ~9,800 lines of work are intact
./automation.sh 3.1.2          # bump FIRST, then gate, build, push, package
```

`automation.sh` runs a three-part release gate before building — static
SQL↔schema check, ruff, full pytest. The image builds from the **working tree**,
so uncommitted work ships; a build fired before your edits land bakes the old
code. Bump **first** (`automation.sh` does this for you).

It also bumps `helm/local/values.g2.yaml`'s `tag:` — that overlay is
**gitignored**, so `git status` will not show it. That is the pin the deploy
actually renders. Verify it moved.

### Deploy

```bash
helm upgrade pcai-llm-gateway ./helm -n pcai-llm -f helm/local/values.g2.yaml
kubectl rollout status deploy/pcai-llm-gateway -n pcai-llm
```

### Verify (do not skip — a deployed tag is a claim, not evidence)

```bash
# 1. the image actually built after your edits
docker manifest inspect ghcr.io/ai-solution-eng/pcai-llm-gateway:v3.1.2 | grep -i digest

# 2. the pods run that digest, not a cached older one
kubectl get pods -n pcai-llm -l app=pcai-llm-gateway \
  -o jsonpath='{range .items[*]}{.metadata.name}{"  "}{.status.containerStatuses[0].imageID}{"\n"}{end}'

# 3. the version constant inside the running container
kubectl exec -n pcai-llm deploy/pcai-llm-gateway -- cat /app/pcai_llm/__init__.py | head -3
```

`imagePullPolicy: IfNotPresent` is the trap here: **re-pushing the same tag
never reaches the pods.** Bump the version; never re-push a released tag.

### Re-type the existing pools (one-time)

Your six registered pools still carry `model_type='chat'` in the database —
detect-on-register only affects **new** registrations. Re-save each one from the
console with Type = *auto-detect* (Models → register → same name + URL), or
re-save with an explicit type. The embedder and reranker will then report
correctly and stop being offered to `/v1/chat`.

I deliberately did **not** auto-migrate the registry: rewriting six rows in your
database is your call, not mine.

---

## What DSH still needs

**One item, and it is already committed — not a merge.**

`packages/llm/llm-pi-ai/src/stream.ts` and the pi-ai patch are both tracked and
committed (`bbe8e5e112`, `92f60d953b`, `75ff9f522c`). Nothing to merge there.

But **CUT 3 from the audit is still open on the DSH side**, and I did not fix it
because you asked me to fix the gateway's relay:

> A relay card is kept in the harness message but stripped from the pi-ai
> message, so the replay envelope's block count mismatches and
> `assembler.ts:148` discards the whole envelope. The next request then
> serializes that turn's assistant message via `foreignAssistant`, which emits
> a thinking block with **no signature** — so pi-ai omits the reasoning field.

Effect: even with the gateway fixed, a relayed turn's reasoning is still not
replayed back to the model on the following turn. That is a DSH fix
(`assembler.ts` / `replay.ts`), and it is the one thing that genuinely lives in
the other repo.

Two more audit items also remain **deliberately unfixed** on the gateway side,
because each changes behaviour you should decide on, not inherit from me:

- **CUT 2** — the inbound reasoning strip (`routes_proxy.py:2825`). It exists to
  stop the 2026-09-18 amplification storm. Removing it re-feeds reasoning to the
  model (which would help the "no thinking" symptom) but risks re-triggering
  that storm for clients that re-append reasoning blocks.
- **CUT 4 / the teaching loop** — `_reasoning_trace` returning `b""` under
  `render_deltas`, and the in-loop assistant append omitting reasoning. Both are
  the same trade: what the model and the user see between tool calls.

Say the word on those and I will do them as a second, separate change with its
own review wave.

---

## What I did not do

- **No commit.** Your working tree has ~9,800 uncommitted lines that predate
  this session; committing would entangle my changes with yours. Backups of both
  edited files are in `.scratch/relay-fix-backup/` if you want to diff or revert.
- **No deploy.** Nothing reached the cluster.
- **No hardlink sync.** The twin at
  `pcai-solutions/tools/pcai-llm-gateway/` is the maintainer's channel; run
  `link_utils.sh` / the fleet drift gate as you normally would before merging.
