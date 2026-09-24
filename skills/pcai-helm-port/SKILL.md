---
name: pcai-helm-port
description: "Use this skill when the user wants an application made deployable on HPE Private Cloud AI (PCAI). The platform context is essential: the ask must target HPE PCAI, Ezmeral, ezaf, the ezua gateway block, ${DOMAIN_NAME} substitution, or hpe-ezua labels (e.g. \"port this chart to PCAI\", \"make this run on HPE Private Cloud AI\", \"package my chart for ezmeral\", \"add the ezua virtual service\"). The skill ports an existing Helm chart or scaffolds a new one, adds the ezua block, VirtualService, optional AuthorizationPolicy, and required labels, then verifies with helm lint/template and packages. Do NOT use for generic Kubernetes, Helm, or Istio work without a PCAI/Ezmeral/ezaf target — deploying ordinary services (redis, oauth2-proxy, etc.) to a cluster, adding a plain Istio VirtualService for mesh routing, or authoring charts with no HPE PCAI context are all out of scope."
---

# Port Helm Charts to HPE Private Cloud AI (PCAI)

## Goal

Produce a Helm chart for an application that works on HPE Private Cloud AI (PCAI). The chart must expose the app through the platform's ezua gateway so PCAI can route traffic to it after it substitutes `${DOMAIN_NAME}`.

## Decision first: existing chart or scaffold?

Check whether a chart already exists for the app:

- If the user gives a path or repo that contains a `Chart.yaml`, treat it as the existing chart and port it **in place** (edit the files in that chart directory).
- If the user doesn't specify anything other than to port the chart, search the current workspace for a chart directory, or for a chart .tgz. If a tgz is found, use it by unpacking it.
- If the input is an app name without a chart in the workspace, a source repo without a chart, or nothing at all, **scaffold a new chart** at `charts/<app-name>/` (relative to the current working directory). If `charts/` doesn't exist, create it.
- If a `charts/<app-name>/` directory exists with a `Chart.yaml`, port that one rather than scaffolding.

State clearly at the start which branch you took and where the chart lives.

## Workflow

### 1. Resolve the app name and chart name

`Chart.Name` (and the `fullname` helper) is the anchor for every template. Use the app name the user gave, kebab-cased. Where a "kuard-helm"-style suffix was used in examples, that's just the chart name — substitute the real app name.

### 2. Scaffold a chart if none exists

Follow Helm/Kubernetes best practices but keep it **simple** — no over-engineering. A service-style chart normally contains:

```
charts/<app-name>/
├── Chart.yaml          # apiVersion, name, version, description
├── values.yaml         # image, service, resources, replicas
└── templates/
    ├── _helpers.tpl    # fullname, labels, selectorLabels helpers
    ├── deployment.yaml
    ├── service.yaml
    └── serviceaccount.yaml   (only if the app needs one — skip if not)
```

Best practices to follow:
- Use the standard `fullname` / `labels` / `selectorLabels` helper pattern (`{{ include "<chart>.fullname" . }}`).
- Deployment: `matchLabels` with `selectorLabels`, `image: "{{ .Values.image.repository }}:{{ .Values.image.tag | default .Chart.AppVersion }}"`, `pullPolicy`, `resources`, `replicas`.
- Service: template `port` and `targetPort`; default to port 80 (or the app's real port) and make it overridable via `values.yaml`.
- `values.yaml`: sensible defaults, generous comments, `image.repository`, `image.tag`, `image.pullPolicy`, `replicaCount`, `service.type/port/targetPort`, `resources`, `autoscaling` optional, `nodeSelector`/`tolerations`/`affinity` optional.
- Use `{{ .Chart.Name }}` and `{{ .Release.Name }}` rather than hardcoding names.
- Don't add CRDs, OPA policies, horizontal pod autoscaling, or other heavyweight machinery unless the app genuinely needs it.
- Don't create a chart that duplicates an existing upstream chart (e.g. kuard) — if there's an official chart, prefer porting it.

### 3. Add the `ezua` block to values.yaml

Append this block to the beginning of `values.yaml`. Keep the `${DOMAIN_NAME}` substitution verbatim — PCAI substitutes it after packaging. Replace `app-name` in the endpoint with the application name.

```yaml
ezua:
  domainName: "${DOMAIN_NAME}"
  # configure the application endpoint
  virtualService:
    endpoint: "app-name.${DOMAIN_NAME}"
    istioGateway: "istio-system/ezaf-gateway"
  # configure the authorization policy
  authorizationPolicy:
    namespace: "istio-system"
    providerName: "oauth2-proxy"
```

The `istioGateway` and `authorizationPolicy` blocks are always exactly as shown — do not invent different values.

### 4. Add `templates/virtualservice.yaml`

Create `templates/virtualservice.yaml` in the chart. Substitute the chart name into the helper references, and template the destination `host` and `port.number` to the **actual Service and port a user would access** (the web UI, API, etc.):

```yaml
#More information https://docs.ezmeral.hpe.com/unified-analytics/15/ManageClusters/importing-applications.html?hl=import
apiVersion: networking.istio.io/v1alpha3
kind: VirtualService
metadata:
  name: {{ include "<chart>.fullname" . }}
  labels:
    {{- include "<chart>.labels" . | nindent 4 }}
spec:
  gateways:
    - {{ .Values.ezua.virtualService.istioGateway }}
  hosts:
    - {{ .Values.ezua.virtualService.endpoint | required "\nValid .Values.ezua.virtualService.endpoint is required !" }}
  http:
  - match:
    - uri:
        prefix: ""
    route:
    - destination:
        host: {{ include "<chart>.fullname" . }}.{{ .Release.Namespace }}.svc.cluster.local
        port:
          number: {{ .Values.<chart>.service.port }}
```

Find the correct value for the destination port by reading the app's actual `service.yaml` (the `.Values...service.port` that the Service listens on) — don't guess.

### 5. AuthorizationPolicy — ask the user

**Prompt the user with the question tool** asking whether they want the AuthorizationPolicy enabled. Options: "Yes, add it" / "No, skip it" (add an "Other" is auto-provided). If they say yes:
- Add `enabled: true` conditionally in `values.yaml` under `ezua.authorizationPolicy.enabled: true`
- Create `templates/authorizationPolicy.yaml`:

```yaml
apiVersion: security.istio.io/v1beta1
kind: AuthorizationPolicy
metadata:
  name: {{ .Release.Name }}-auth-policy
  namespace: {{ .Values.ezua.authorizationPolicy.namespace }}
spec:
  action: CUSTOM
  provider:
    name: {{ .Values.ezua.authorizationPolicy.providerName }}
  rules:
    - to:
        - operation:
            hosts:
            - {{ .Values.ezua.virtualService.endpoint }}
  selector:
    matchLabels:
      istio: "ingressgateway"
```

Wrap the manifest in `{{- if .Values.ezua.authorizationPolicy.enabled }}` ... `{{- end }}` so it can be toggled off. If they say no, create the policy template but ensure it is set to enabled: false in values.yaml.

### 6. Ensure the required labels

Every Pod, Deployment, and Service in the chart must carry these metadata labels:

```yaml
"hpe-ezua/type": vendor-service
"hpe-ezua/app": {{ .Chart.Name }}
```

This is usually achieved by adding `hpe-ezua/type: vendor-service` and `hpe-ezua/app: {{ .Chart.Name }}` to the template's `labels` helper (and for pods, to `spec.template.metadata.labels` via selectorLabels, so the Deployment's pod selector and pod template both carry it). Prior to manually modifying all templates labels, check to see if there is a higher-level way built into the chart, like the _helpers.tpl or a way to add extra labels already.

**If it is not possible to add the labels natively** (e.g. you're porting an existing third-party chart whose templates you must not restructure, or the labels can't reach pod templates), create a Kyverno `ClusterPolicy` instead:

```yaml
apiVersion: kyverno.io/v1
kind: ClusterPolicy
metadata:
  name: {{ printf "add-vendor-app-labels-%s-%s" .Release.Name .Chart.Name }}
  annotations:
    "helm.sh/hook": pre-install
    "helm.sh/hook-weight": "-5"
    "helm.sh/hook-delete-policy": before-hook-creation
spec:
  background: false
  rules:
  - name: add-vendor-app-labels
    match:
      any:
      - resources:
          namespaces:
          - {{ .Release.Namespace }}
          kinds:
          - Pod
          - Deployment
          - Service
    mutate:
      patchStrategicMerge:
        metadata:
            labels:
              "hpe-ezua/type": vendor-service
              "hpe-ezua/app": {{ .Chart.Name }}
```

If the chart defines namespaces, list all of them; otherwise keep only `.Release.Namespace`. Prefer native labels over the Kyverno policy, and explain to the user which route you took and why.

### 7. Verify

Before packaging, verify the chart renders correctly:

1. `helm lint charts/<app-name>/` — must pass cleanly.
2. `helm template <app-name> charts/<app-name>/` — set the required values PCAI substitutes, e.g. `--set ezua.virtualService.endpoint=app-name.example.com` (or `--set-string ezua.domainName=example.com`), so the required-value checks and templating are exercised. Confirm the VirtualService, labels, and (if enabled) AuthorizationPolicy render.

Fix anything that fails before packaging.

### 8. Package

Run:

```
helm package charts/<app-name>/
```

Keep the resulting `.tgz` next to the chart (it stays in `charts/`). Tell the user the path to the packaged chart.

### 9. Write PORTING.md

Create `PORTING.md` inside the chart directory (`charts/<app-name>/PORTING.md`) so the port is self-documenting: anyone reviewing the diff or deploying the chart later can see exactly what was changed for PCAI and why. Copy the bundled template at `templates/PORTING.md` (in this skill's directory) into the chart directory, then fill in **every** section with the actual values used — no placeholders left behind. The structure is fixed:

1. **Summary** — chart name/version, whether it was ported or scaffolded (and from where), packaged `.tgz` path, date
2. **Changes made** — one numbered subsection per change: the `ezua` block (as a key/value/why table), the new VirtualService (gateway, host, destination host:port and where the port came from), the AuthorizationPolicy decision (included/excluded and why), the `hpe-ezua` labels route (native vs Kyverno, with the reason), and any other changes
3. **Verification** — `helm lint` result and the `helm template` resource list
4. **Deploying on PCAI** — instruct the user to open PCAI's AI Essentials homepage, navigate to Tools & Frameworks, select Import Framework, then follow the prompts. Note any pertinent values that may need to be changed. Provide the resulting VirtualService URL

Record decisions, not just edits — the "why" column/notes (e.g. "Kyverno used because the upstream chart's templates must not be restructured") are the most valuable part for future maintainers.

## Order of operations & what to confirm

1. Determine existing-vs-scaffold, state it
2. Scaffold if needed
3. Add `ezua` block to values.yaml
4. Add virtualservice.yaml
5. **Ask the user** about AuthorizationPolicy
6. Add labels (native or Kyverno)
7. helm lint + helm template to verify
8. helm package, report the .tgz
9. Write PORTING.md in the chart directory (from the bundled template)
10. Respond to the user with instructions on deploying the chart from the AI Essentials UI, as described in the Deploying to PCAI section of the PORTING.md file

If the app needs choices the user didn't specify (port, service name), ask the user for clarification using the question tool after verifying that the choice cannot be made on behalf of the user. Make resonable default choices that align with best practices where applicable and low-stakes, but do not automatically make a decision that may be incorrect or have the potential to disrupt the app's deployment.
