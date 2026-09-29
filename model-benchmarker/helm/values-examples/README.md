# values-examples -- paste-ready values documents (secret-free)

Sanitized, complete example values documents for this chart, mirrored by the
hardlinker into the public delivery repo -- intentionally secret-free.
Credentials are always role-named fillers (`<BENCH_API_KEY>` style).

| File | Target |
|---|---|
| `values.g2.yaml` | SE G2 (HPE internal cluster, HPE SSO at the gateway) |
| `values.hosted-trial.yaml` | customer PCAI (oauth2-proxy + kyverno pre-install hook) |

Both files are FULL copies of this chart's values.yaml (not override
snippets) with site-specific lines marked `# SITE:`. See the MultimodalRAG
chart's values-examples/README.md for the shared placeholder convention.

# The one required decision: the benchmark pages' API key

Either manage your own Secret:

    kubectl -n <ns> create secret generic bench-platform-keys \
      --from-literal=BENCH_API_KEYS='<key1>,<key2>'

and set `security.existingSecret: bench-platform-keys` (the G2 example), or
leave `security.apiKey`/`existingSecret` empty and let the chart auto-generate
a key into `<deployment.name>-keys` on first install (the hosted-trial
example's default when the filler is cleared). Retrieve an auto-generated key:

    kubectl -n <ns> get secret model-benchmarker-keys \
      -o jsonpath='{.data.BENCH_API_KEYS}' | base64 -d; echo

Target-endpoint keys (JWTs for the endpoints being benchmarked) go into
`bench.endpointApiKeys` or, preferably, a Secret referenced by
`bench.endpointApiKeysExistingSecret`.

Where the real values live: `helm/local/` (gitignored, hardlink-ignored, excluded
from packaged charts by `.helmignore`). Copy the matching example there, fill the
fillers with real credentials, keep it out of every tracked tree.
