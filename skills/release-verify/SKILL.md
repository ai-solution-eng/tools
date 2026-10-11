---
name: release-verify
description: Use when asked "is my fix actually deployed?", when releasing or deploying a versioned service, when verifying a deployed tag/digest/version, or when a fix "should be live but isn't". Covers the layer-by-layer verification chain and the ways a deployed image silently lies. NOT for generic cluster breakage (cluster-triage) or GPU serving config (gpu-serving-change).
---

# Release-verify — is my fix ACTUALLY live?

A deployed tag is a **claim**, not evidence. Verify layer by layer, top down; stop at the first mismatch.
Each check below has lied before.

Deployment values (registry, image, namespace, label selector, version file) are **not** universal — read
them from the project's own build script, chart values, or release. See **Reference instantiation** at the
end for one concrete set.

## The verification chain (run in order)

| # | Layer | Check | A mismatch means |
|---|---|---|---|
| 1 | **Working tree** | Is the fix in the source tree right now? | Nothing else matters yet. Build scripts typically build from the **working tree**, so uncommitted work DOES ship — and a build fired before your edits land bakes the OLD code. Sequence matters. |
| 2 | **Version bump** | Grep the app's version constant (e.g. `__version__` in the package `__init__.py`). | The bump did not run after the fix, or ran before it. |
| 3 | **Image content** | Compare the pushed image's **build time** vs the last source edit (`ls -l --time-style=full-iso <path>`). | The tag predates your fix — the classic shape: a deployed digest **predated** a later security commit. Mutable tags drift; never trust a tag name alone. |
| 4 | **Registry** | `GET https://<registry>/token?scope=repository:<org>/<image>:pull&service=<registry>` then `GET /v2/<org>/<image>/manifests/<tag>` → read `docker-content-digest`. | Token **200** = anonymously pullable; any non-200 = not public. Expect 401/403 — a **nonexistent** package returns 403 in testing, so treat any non-200 as "not public", not specifically 401. |
| 5 | **Deployed object** | `kubectl get pod -n <namespace> -l <selector> -o jsonpath='{range .items[*]}{.metadata.name}{"  "}{.status.containerStatuses[0].imageID}{"\n"}{end}'` | Pod `imageID` digest ≠ the registry digest for that tag → **the pods are running something else** (see IfNotPresent below). Read `<namespace>`/`<selector>` from the release or chart values. |
| 6 | **In-cluster version** | Read the version constant from inside the running container (`cat /app/<pkg>/__init__.py`). | In-image version ≠ the version you think you shipped. This is the fastest in-cluster truth. |
| 7 | **Overlay pin** | Grep the **gitignored** values overlay actually deployed for its `image.tag`. | The overlay pins a different tag than the one you pushed — the deploy renders the OLD image while your release looks green. |

Only when 1–7 all agree is the fix live.

### Exec-tool caveat
A hardened exec tool may **hard-deny interpreters** (e.g. `python3` — "interpreters make the allowlist
meaningless"). The classic `python3 -c "import <pkg>; print(<pkg>.__version__)"` then **cannot** run through
it — use `cat` on the version file instead, or run the one-liner via real `kubectl exec` outside the
allowlist. Check the tool's allowlist before assuming a command will run.

### Do NOT assume a health endpoint exists, or that it reports a version
Read the server's **actual route table** (grep the app for its health route) rather than assuming
`/health`. Two traps that generalize:
- A health path may exist **only as an entry in an auth allowlist** — an exemption string, not a route.
- A real health route may return `{"status":"ok"}` with **no version at all**, and `/docs`/OpenAPI may be
  disabled once auth is on. The version constant read from the container is the reliable surface.
- An unauthenticated probe of a guarded path may return a **redirect (302)**, not JSON — a 302 is not
  evidence the endpoint exists.

## Failure modes that have actually lied

- **`imagePullPolicy: IfNotPresent`**: re-pushing the **same tag never reaches pods** — the node cache wins.
  Worse, once a tag is pushed with wrong content it is **poisoned**. **Always version-bump; never re-push a
  released tag.**
- **Gitignored overlays** pin `image.tag` and are invisible to `git status`. A bump script may rewrite
  top-level `tag:` lines in tracked and local values files — but nested tags often move **only** when the
  nearest `repository:` names a first-party image. Third-party pins are deliberately never moved. Re-check
  any tag you added by hand.
- **Chart/image tag mismatch**: a chart pinning `0.8.0` against an image tagged `v0.8.0` → ErrImagePull; a
  chart pinning a never-pushed tag. **Check the `v` prefix and that the tag exists.**
- **Restrictive file modes under the source tree** (e.g. 600/711) crash a **non-root container**. Run
  `find <src> -type f ! -perm -0044` before building. A build-time `chmod -R a+rX` helps but is not your
  only guard. **These modes travel via hardlinks** — see the `fleet-drift` skill.
- **Proxy interference**: a lab/VPN SOCKS proxy must NOT route registry or Docker Hub traffic, or the build
  hangs at base-image metadata. Check `docker info | grep -i proxy` and the shell environment.

## The release path (shape)

1. **Bump FIRST** and make the bump idempotent. The image must build from the already-bumped tree for its
   baked-in version constant to match the release.
2. Build and push the image, tagged `v${VERSION}`.
3. Package the chart(s).
4. Prune stale chart archives.

Run those only *after* the verification chain fails; verification is this skill's point, not the release.
Read the real commands from the project's build script.

## Values-paste / managed-platform reality

If deploys go through a platform UI rather than helm/kubectl:
- Deploys are **import-chart → edit values**; end users have no helm/kubectl.
- Only a documented subset of variables is substituted (often just `${DOMAIN_NAME}`).
- **A values re-apply only takes effect on chart re-apply** — a rollout restart re-renders nothing. After
  chart edits, **re-package the archive** for import.
- Hotfix images need a **NEW tag every time**.

## Handoff

Stale image confirmed → the fix is a release, not a cluster repair. Cause genuinely unknown →
`cluster-triage`. A shared file was involved → `fleet-drift` (two channels must both move).

## Reference instantiation

One deployment's concrete values — re-derive locally, do not copy:

- Build script `./automation.sh <version>` (bumps via `bump_version.sh`, then `docker buildx build … --push`,
  then `helm-p`, then `prune_charts.py`; `helm-p` renames each `.tgz` → `.tar.gz`).
- Registry/image: `ghcr.io/ai-solution-eng/multimodal-rag-mcp`; token scope
  `repository:ai-solution-eng/multimodal-rag-mcp:pull`.
- Namespace `mm-rag`, selector `app=rag-mcp-server`; in-image version file
  `/app/src/multimodal_rag/__init__.py`; version constant `__version__`.
- The project's routes: `/healthz` (no version), `/api/admin/health` (no version), `/` — **no `/health`
  route**; `/health` appears only in `utils/mcp_auth.py` `DEFAULT_PUBLIC_PATHS`. `/api-status` does not
  exist (it appears only in `automation.sh` comments).
- Local overlays: `helm*/local/values*.yaml` (gitignored; `.values*.yaml`, `helm*/local/*`).
- Chart/image mismatch precedents: ModelDownloader (`0.8.0` vs `v0.8.0`), SQLhandler (never-pushed `v0.4.0`).
- Auth/TLS: endpoints in `ModelBenchmarker/src/model_benchmarker/utils/pcai_models.py`;
  `REMOTE_CA_BUNDLE=$DSH_HOME/hpe-private-ssl-ca.pem` for self-signed ingress.
