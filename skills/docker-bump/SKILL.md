---
name: docker-bump
description: Use when a new chart revision ships and the user asks "just a docker bump?" — i.e. can the latest g2 values be pasted into the PCAI values editor as-is, instead of fixing/re-packaging the helm side. Decides paste-only vs chart-work by diffing g2 values against what the deployed chart actually consumes. NOT for authoring or restructuring values files (values-blueprint), packaging a chart for PCAI import (pcai-app-import), or verifying a tag actually went live (release-verify).
---

# docker-bump — "Just a docker bump?" for PCAI deploys

## The question being answered

A new chart revision exists. The user wants to know:

> Can I paste the latest g2 values into the PCAI values editor, or does the helm
> side need fixing first?

Answer it with a diff + a consumer check. Never answer from vibes — the failure
mode is pasting values that reference keys the deployed chart ignores, then
wondering why the deployment didn't change.

## The grounded PCAI deploy reality (do not re-litigate)

- Deploys are **import-chart → edit values**; end users have no helm/kubectl.
- PCAI substitutes a **fixed, documented placeholder set** in pasted values —
  **not** just \`\${DOMAIN_NAME}\`. A \`\${VAR}\` outside that set is left
  literal and breaks the deployment. See **Placeholder substitution** below for
  the full list and the authoritative source.
- **Values re-apply only takes effect on chart re-apply.** A rollout restart
  re-renders nothing. After pasting new values, trigger the chart re-apply.
- After chart template edits, **re-package the \`.tgz\`** for PCAI import —
  values can never carry template logic.
- **Hotfix images need a NEW tag every time.** Pasting values that pin the same
  tag redeploys the same image.

## Placeholder substitution — the actual rule

> **Source of truth:** *HPE AI Essentials Software 1.12.x Documentation*
> (PCAI user guide), § "Preparing the Framework Helm Chart" → **"Using
> Placeholders in \`values.yaml\`"** (p. 97) and **"Importing Frameworks Using
> UI"** (p. 98). Retrieve it from the \`pcai-documentation\` dataset in mm-rag:
> \`search_dataset(dataset_name="pcai-documentation", query="placeholder
> values.yaml DOMAIN_NAME AIRGAP_REGISTRY substitution")\`. Do not re-derive the
> list from memory — re-query when it matters.

The documented placeholder keys:

| Placeholder | Meaning |
|---|---|
| \`DOMAIN_NAME\` | FQDN of the HPE AI Essentials cluster |
| \`AIRGAP_REGISTRY\` | URL of the private, air-gapped container registry |
| \`OIDC_CLIENT_ID\` | Client ID for embedded OIDC authentication |
| \`OIDC_CLIENT_SECRET\` | Secret associated with the OIDC client ID |
| \`OIDC_DOMAIN\` | Domain name of the OIDC provider |
| \`NAMESPACE\` | Kubernetes namespace where Helm deploys the release |
| \`RELEASE_NAME\` | Name of the current Helm release |
| \`CHART_VERSION\` | Version of the Helm chart used for deployment |

**Mechanics (from the docs):**

- Reference them **bash-style** — \`\${OIDC_CLIENT_SECRET}\` — anywhere within
  \`EzAppConfig.Spec.Values\`.
- Substitution is **dynamic and applies only to placeholders defined in
  \`EzAppConfig.Spec.Values\`**. A placeholder appearing in a chart's own
  \`values.yaml\` but **not** specified in \`EzAppConfig.Spec.Values\` is
  **not replaced**.
- The docs' own example uses several together:

  \`\`\`yaml
  ezua:
    oidc:
      client_id: "\${OIDC_CLIENT_ID}"
      client_secret: "\${OIDC_CLIENT_SECRET}"
      domain: "\${OIDC_DOMAIN}"
      domainName: "\${DOMAIN_NAME}"
    virtualService:
      endpoint: "\${RELEASE_NAME}-\${NAMESPACE}.\${DOMAIN_NAME}"
      istioGateway: "istio-system/ezaf-gateway"
  \`\`\`

**What this means when reviewing a paste:** \`\${OIDC_CLIENT_SECRET}\` and the
other documented placeholders are **expected and correct** in pasted g2 values.
Do **not** flag them as "lands literally and breaks the deployment" — that is
only true for a \`\${…}\` outside the table above (e.g. an app-specific variable
the operator invented, or one meant to be resolved by Helm rather than PCAI).

**Residual risk worth stating, not assuming:** substitution is real, but whether
the *value* behind it is populated depends on the cluster. If OIDC is disabled
on the deployment, \`\${OIDC_CLIENT_SECRET}\` may still substitute to an empty or
placeholder value — that is a cluster-configuration question, not a paste
mechanism failure. Say which one you mean.

## The decision procedure

### Step 1 — Diff the new g2 values against live pasted values

Diff \`values.g2.yaml\` (new) against the values currently in the EzAppConfig
(decode the release secret to see what PCAI actually stored — don't trust the
editor's rendering). Classify every hunk:

- **Image-only**: \`image.repository\` / \`image.tag\` (and nothing else).
- **Value-only**: existing keys, new literals (replicas, resources, feature flags).
- **Key-shape**: keys added, removed, renamed, or re-nested.
- **Non-values**: anything that needs template logic (new env wiring, new
  containers, ports, VirtualService/ezua block changes, new subchart).

### Step 2 — Check the deployed chart consumes the changed keys

For every changed key, confirm the **deployed** chart (the one already imported
into PCAI) references it in its templates. Keys present in g2 values but absent
from the deployed chart's templates are inert — pasting them does nothing.

### Step 3 — Apply the decision table

| Diff classification | Deployed chart consumes keys? | Verdict |
|---|---|---|
| Image-only (new tag) | yes | **Just a docker bump.** Paste. |
| Value-only | yes | **Just a values paste.** Paste + re-apply. |
| Key-shape | new keys yes, deployed chart knows them | Paste, but check chart defaults cover removals. |
| Key-shape | new keys the deployed chart ignores | **Helm side first**: update chart, re-package \`.tgz\`, re-import, then paste. |
| Non-values | — | **Helm side.** Values cannot carry template logic. |

### Step 4 — If paste-only, do it right

1. Paste the full latest g2 values — never a partial merge by hand; the g2 file
   is the source of truth and partial merges drift.
2. Mind the paste mechanics: preserve quoting exactly (\`cpu: '4'\` stays
   quoted); keep \`\${…}\` references **to the documented placeholder set** only.
3. Trigger the chart re-apply — pasted values are inert until then.
4. Verify the new tag actually went live (release-verify skill): decode the
   rendered workload, confirm image digest changed.

## Common failure modes

- **Pasting keys the deployed chart doesn't know.** New g2 keys + old imported
  chart = silent no-op. The chart must be re-packaged and re-imported first.
- **Same-tag hotfix.** The pod redeploys with the cached old image. Hotfix = new tag.
- **Template changes smuggled into values.** A new sidecar, port, or ezua block
  edit is chart work; no values paste can express it.
- **Forgetting re-apply.** Values saved in the editor change nothing until the
  chart re-applies. A rollout restart is not a re-apply.
- **Undocumented \`\${…}\`.** Substitution covers the documented set (see
  **Placeholder substitution**); anything else lands literally. Verify by
  decoding the release secret after paste.
- **Over-correcting the other way.** Do not reject a values file *because* it
  contains \`\${OIDC_CLIENT_SECRET}\` — that is the documented mechanism. Check
  the name against the table, not your memory.

## Relationship to other skills

- \`values-blueprint\` — how g2 values files are authored; this skill consumes them.
- \`pcai-app-import\` — when the verdict is "helm side", this packages the \`.tgz\`.
- \`release-verify\` — after any paste, this confirms the deployed image/tag moved.
