# Example values files (shareable · secret-free)

This folder holds **sanitized, paste-ready example values files** for the
`model-downloader` chart. It is hardlinked into the public delivery tree
(`pcai-solutions/tools/model-downloader-web/`), so everything here must be
safe to publish:

- **No secrets.** Credentials are role-named placeholders (`<MINIO_ACCESS_KEY>`,
  `<MINIO_PASSWORD>`); usernames/namespaces are `<USERNAME>`,
  `project-user-<USERNAME>`. Site endpoints and
  domains (e.g. `hpeproxy.its.hpecorp.net`, the SE G2 cluster domain) are kept
  because they are not secrets.
- **Real per-site values live in `helm/local/`** (repo-only: gitignored via
  `helm*/local/*`, hardlink-ignored via `helm*/local`, never packaged via
  `helm/.helmignore`). See `helm/local/README.md` in the source repo for that
  convention. Never copy credentials from `helm/local/` into this folder.

## Placeholder convention

Every value **you** must replace is wrapped in angle brackets and named for
its role — `<MINIO_PASSWORD>`, `<EMBEDDER_API_KEY>`, `<USERNAME>`. The one
exception is `${DOMAIN_NAME}`: PCAI substitutes it before rendering, so leave
it as-is. Lowercase `<tokens>` inside comments are illustrative patterns, not
values.

## Files

| File | Target | Highlights |
|---|---|---|
| [`values.g2.yaml`](values.g2.yaml) | HPE internal SE G2 PCAI cluster (`pcai-se-ai-application.hst.rdlabs.hpecorp.net`) | `hpe_proxies: true` (corporate proxy + Zscaler TLS bypass), PVC + MinIO (`mlis-models` bucket), `project-user-*` namespaces. |
| [`values.hosted-trial.yaml`](values.hosted-trial.yaml) | Customer-hosted PCAI (hosted trial) | `${DOMAIN_NAME}` placeholders for EZUA ingress, `hpe_proxies: false`, storage backend choice, Kyverno gate called out in the header. |

## Using a file on PCAI

1. **Import the chart once** into PCAI (the packaged chart, e.g.
   `model-downloader-1.4.4.tar.gz`). PCAI users never run `helm install` /
   `kubectl apply` — deployment is values-only after the import.
2. **Open the chart's Helm Values editor** in PCAI and paste the whole file
   (both files are *full values* documents — they stand alone and do not
   depend on the chart defaults being merged first, though merging works too).
3. **Adjust the `# SITE:` lines** — namespace, usernames, S3 endpoint/bucket
   and keys, MLIS/AIOLI endpoint if it differs.
4. **Apply.** PCAI resolves `${DOMAIN_NAME}` before rendering; leave those
   placeholders untouched.

On SE G2, apply `values.g2.yaml` as-is after filling the `# SITE:` lines. On a
hosted trial, read the top-level README's **"Kyverno on hosted trial systems"**
section first — the chart's downloader Jobs must be admitted by the
platform-wide `protect-models-pvc` Kyverno policy, and `kyverno.enabled` must
stay `true`.

## Sanity-checking a render (operators)

Operators with `helm` available can eyeball the render before touching PCAI:

```bash
helm lint helm
helm template helm -f helm/values-examples/values.g2.yaml
helm template helm -f helm/values-examples/values.hosted-trial.yaml
```

`${DOMAIN_NAME}` stays a literal string in the render (PCAI substitutes it at
apply time); on a non-PCAI cluster, pass `--set ezua.domainName=<real-domain>`
for the check.
