# PCAI Porting Notes — <chart-name>

> Copy this template into the chart directory as `PORTING.md` and fill in every
> section with the actual values used during the port. No placeholders left behind.

## Summary

| | |
|---|---|
| Chart | `<name>` v`<version>` (appVersion `<appVersion>`) |
| Source | Ported from `<original path/URL>` / Scaffolded from scratch |
| Packaged | `<path>/<name>-<version>.tgz` |
| Date | `<YYYY-MM-DD>` |

## Changes made

### 1. `values.yaml` — `ezua` block

| Key | Value | Why |
|---|---|---|
| `ezua.domainName` | `${DOMAIN_NAME}` | Substituted by PCAI at deploy time; kept verbatim |
| `ezua.virtualService.endpoint` | `<app-name>.${DOMAIN_NAME}` | Public URL users hit |
| `ezua.virtualService.istioGateway` | `istio-system/ezaf-gateway` | Platform gateway (fixed value) |
| `ezua.authorizationPolicy.namespace` | `istio-system` | Fixed value |
| `ezua.authorizationPolicy.providerName` | `oauth2-proxy` | Fixed value |
| `ezua.authorizationPolicy.enabled` | `true` / absent | Only if the AuthorizationPolicy was requested |

### 2. `templates/virtualservice.yaml` — new file

- Gateway: `.Values.ezua.virtualService.istioGateway`
- Host: `.Values.ezua.virtualService.endpoint`
- Destination: `<fullname>.{{ .Release.Namespace }}.svc.cluster.local`
- Port: `<port>` — taken from `<values path>` (the user-facing service port)

### 3. AuthorizationPolicy

<Included (user opted in) / Excluded (user declined) / Not offered (reason)>.
Template: `templates/authorizationPolicy.yaml`, gated on `ezua.authorizationPolicy.enabled` — or "none".

### 4. `hpe-ezua` labels

- `hpe-ezua/type: vendor-service`
- `hpe-ezua/app: <chart name>`
- Applied: natively via `<_helpers.tpl labels helper + pod template>` — or — via Kyverno
  ClusterPolicy `templates/kyverno.yaml` because <reason: third-party templates left
  unmodified, labels cannot reach pod templates, ...>.

### 5. Other changes

<One line per additional change: chart scaffolded pieces, renames, port overrides,
probes, securityContext, namespace additions. Write "None" if nothing else.>

## Verification

- `helm lint`: <pass — trim output to anything noteworthy>
- `helm template`: rendered <N> resources — <comma-separated `kind/name` list>;
  VirtualService host/destination confirmed against the Service port

## Deploying on PCAI

```bash
helm install <release-name> <name>-<version>.tgz -n <namespace>
# Reachable at https://<app-name>.${DOMAIN_NAME}
```
