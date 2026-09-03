# pcai-helm-port

An [opencode](https://opencode.ai) skill that ports Helm charts (or scaffolds new ones) so applications are deployable on **HPE Private Cloud AI (PCAI)**.

## What it does

Given an app (or an existing chart), the skill produces a Helm chart that works on PCAI's ezua platform gateway:

1. **Existing chart or scaffold** — if the input contains a `Chart.yaml`, the chart is ported in place; otherwise a simple, best-practice chart is scaffolded at `charts/<app-name>/`.
2. **`ezua` values block** — appended to `values.yaml` with the `${DOMAIN_NAME}` substitution kept verbatim (PCAI substitutes it at deploy time) and the fixed platform values:
   - `ezua.virtualService.istioGateway: istio-system/ezaf-gateway`
   - `ezua.authorizationPolicy.providerName: oauth2-proxy`
3. **`templates/virtualservice.yaml`** — a new Istio VirtualService routing the public host (`<app-name>.${DOMAIN_NAME}`) to the app's actual user-facing Service and port (read from the app's `service.yaml`, never guessed).
4. **AuthorizationPolicy (optional)** — the skill asks the user before enabling; the template is gated on `ezua.authorizationPolicy.enabled`.
5. **`hpe-ezua` labels** — every Pod/Deployment/Service gets `hpe-ezua/type: vendor-service` and `hpe-ezua/app: <chart-name>`, natively via the labels helper, or via a Kyverno ClusterPolicy when native labels can't reach pod templates (e.g. porting third-party charts unmodified).
6. **Verify** — `helm lint` plus `helm template` with required values set (e.g. `--set-string ezua.domainName=example.com`).
7. **Package** — `helm package`, resulting `.tgz` stays in `charts/`.
8. **PORTING.md** — a filled-in porting report (copied from `templates/PORTING.md`) is written into the chart directory documenting every change, the verification results, and the deploy command/URL.

## Installation

The skill lives in this repo at:

```
pcai-helm-port/
```

opencode picks up project skills from `~/.config/opencode/skills/` automatically. To use it in a specific project only, copy the `pcai-helm-port/` directory into that project's `.opencode/skills/`.

## Usage

opencode consults the skill when the request targets the PCAI platform, e.g.:

- "Port this chart to PCAI" (with a path to an existing chart)
- "Make this app deployable on HPE Private Cloud AI" (no chart yet — scaffolds one)
- "Add the ezua virtual service"

Deliberately **not** for generic Kubernetes/Helm/Istio work with no PCAI context (redis deployments, plain mesh VirtualServices, etc.).

A typical run states which branch it took (ported vs scaffolded, and where the chart lives), asks about the AuthorizationPolicy, and finishes with `helm lint` results, the packaged `.tgz` path, and the resulting URL (`https://<app-name>.${DOMAIN_NAME}`).

## Files

```
.opencode/skills/pcai-helm-port/
├── SKILL.md            # skill definition: workflow and rules opencode follows
├── templates/
│   └── PORTING.md      # template copied into each ported chart directory
└── evals/
    └── evals.json      # eval cases for the opencode skill eval tooling
```

## Evals

Three cases in `evals/evals.json`:

| ID | Case | Expects |
|----|------|---------|
| 0 | Port an existing kuard chart | In-place port: `ezua` block, VirtualService on the real Service port, labels or Kyverno policy, PORTING.md, packaged `.tgz` |
| 1 | Scaffold a Streamlit app from scratch | New chart with standard structure, `ezua` block, labels, PORTING.md, packaged `.tgz` |
| 2 | Scaffold nginx and decline the auth policy | No AuthorizationPolicy (user declined), everything else as above |

Run them with the opencode skill eval tooling (opencode consults the skill, and the output is checked against `expected_output`).
