# Local deployment values (gitignored)

This directory holds **per-deployment values files that must never be
committed** — client credentials, environment-specific endpoints, cluster
names. Everything in here except this README and `values.example.yaml` is
ignored by `.gitignore` (`helm/local/*`).

## Convention

| File | Purpose |
|---|---|
| `values.example.yaml` | Tracked, sanitized starting point — copy it. |
| `deploy-<site>-values.yaml` | One per deployment site (e.g. `deploy-toromont-values.yaml` at the repo root, kept for continuity). |
| `values-minio-dev.yaml` | Local/dev MinIO credentials for the default `backend: s3` chart. |

## Creating the credential Secrets out-of-band (preferred)

The chart defaults to `credentialsSecret.create: false` — you create the
Secret yourself so credentials never pass through values files or the Helm
release secret:

```bash
# S3 / MinIO (keys must match s3.credentialsSecret.accessKeyKey/secretKeyKey)
kubectl -n <namespace> create secret generic s3-credentials \
  --from-literal=access-key=<S3_ACCESS_KEY> \
  --from-literal=secret-key=<S3_SECRET_KEY>

# OneLake / Fabric service principal
kubectl -n <namespace> create secret generic fabric-credentials \
  --from-literal=tenant-id=<tenant-id> \
  --from-literal=client-id=<client-id> \
  --from-literal=client-secret=<client-secret>
```

## Reading back / rotating an existing Secret

```bash
kubectl -n <namespace> get secret s3-credentials -o jsonpath='{.data.access-key}' | base64 -d; echo
kubectl -n <namespace> get secret s3-credentials -o jsonpath='{.data.secret-key}' | base64 -d; echo
```

Note: the Deployment template carries `checksum/secret` + `checksum/config`
annotations, so after rotating a Secret with `kubectl apply` the pod rolls
automatically on the next `helm upgrade` (or immediately if you re-run the
upgrade that re-renders the annotation).

## Using a values file from here

```bash
helm upgrade --install sqlhandler ./helm -n <namespace> \
  -f helm/local/values-minio-dev.yaml
```

(For PCAI, paste the same values into the PCAI *Helm Values* editor instead.)
