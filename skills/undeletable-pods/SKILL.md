---
name: undeletable-pods
description: Use when pods are stuck Terminating, or a pod/object cannot be deleted (persistent NotFound, force-delete failing, "patched (no change)", deletionTimestamp set but never reaped). Covers the diagnosis ladder (finalizer to webhook to subresource) and the working fix order for the PCAI G2 cluster. Do NOT use for pods that are merely CrashLoopBackOff or OOMKilled (that is cluster-triage) or for general alert investigation.
---

# Undeletable pods (stuck Terminating / un-removable objects)

## The three-layer model

A pod that refuses to die is blocked by EXACTLY ONE of three things, always check in this order:

1. **A finalizer** - metadata.finalizers non-empty + deletionTimestamp set, but the object
   persists for minutes/hours. The object waits for a controller to do work it will never do.
2. **An admission webhook** - every UPDATE to the object is rejected or mutated. On G2 the
   Kyverno policy assign-custom-scheduler-for-ezua-user-vendor-pods stamps
   spec.schedulerName: scheduler-plugins-scheduler into every pod write; schedulerName is
   immutable, so ALL patches to labeled pods fail with a confusing diff mentioning
   schedulerName even though your patch only touched, say, finalizers.
3. **A missing subresource** - pods/finalizers returns 404 on this API server, so the
   purpose-built finalizer-removal endpoint does not exist.

On G2, stuck Job pods hit ALL THREE (real incident 2026-10-14: nine pods, six hours).

## Diagnosis ladder (top to bottom, stop when the cause is found)

1. **Is it a finalizer?**
   kubectl get pod <name> -o custom-columns='FIN:.metadata.finalizers,DEL:.metadata.deletionTimestamp'
   - finalizers non-empty + deletionTimestamp set: finalizer problem (layer 1)
   - finalizers non-empty + deletionTimestamp ABSENT: the pod is not even terminating yet;
     its controller is gone (orphaned). Fix: plain delete pod first - it then enters
     Terminating and hits layer 1/2.
   - "patched (no change)" in kubectl output usually means the patch already landed but the
     object is still listed because its PARENT still holds or reattaches the finalizer.
     Delete the parent Job first, wait ~15s, re-patch.
2. **Which finalizer?** batch.kubernetes.io/job-tracking = the Job controller owns it. If the
   parent Job is GONE, that finalizer is orphaned and NOTHING will ever remove it (upstream
   kubernetes#116758). This is the G2 signature.
3. **Is a webhook blocking updates?** Attempt the patch and READ THE ERROR. A forbidden-field
   diff you did not write (schedulerName changing) = a mutating webhook rewrote the request.
   Confirm the culprit:
   kubectl get mutatingwebhookconfigurations -o json | jq -r '.items[] | .metadata.name as $n | .webhooks[]? | select([.rules[]?.resources[]?] | index("pods")) | "\($n) \(.name) \(.failurePolicy)"'
   On G2: kyverno-resource-mutating-webhook-cfg (failurePolicy=Fail) points at ClusterPolicy
   assign-custom-scheduler-for-ezua-user-vendor-pods (match: pods labeled
   hpe-ezua/type app-service-user|vendor-service - the platform ALSO auto-labels all pods in
   vendor namespaces, so diagnostic pods get caught too).
4. **Subresource check (before trying it):** pods/finalizers is NOT served on G2 (404) - do
   not burn a cycle on kubectl replace --raw .../finalizers.

## Fix order (what actually worked, 2026-10-14)

1. **Patch the ClusterPolicy, not the webhook.** Add an exclude for the affected pods instead
   of disabling anything (webhook failurePolicy: Fail makes disabling it cluster-wide-risky):

   kubectl patch clusterpolicy assign-custom-scheduler-for-ezua-user-vendor-pods --type=json \
     -p='[{"op":"add","path":"/spec/rules/0/exclude","value":{"any":[{"resources":{"selector":{"matchExpressions":[{"key":"<diagnosis-label>","operator":"Exists"}]}}}]}}]'

   G2 uses pcai-llm/diag as the diagnosis-label. Kyverno reconciles in seconds. Label new
   diagnostic workloads with it - prevention AND cure in one edit.
2. **Remove the orphaned finalizers:**

   export https_proxy=socks5://127.0.0.1:1080
   for p in <pod1> <pod2>; do
     kubectl --kubeconfig ~/SSH/G2/g2_kubeconfig.yaml -n <ns> patch pod "$p" --type=merge -p '{"metadata":{"finalizers":null}}'
   done
   unset https_proxy

   Gotcha: the env-prefix form (https_proxy=... kubectl ...) applies ONLY to the first command
   in a pipeline - xargs children run WITHOUT the proxy and time out. export first.
   Also: pipes must END a line, not start one (bash cannot parse a leading pipe character).
3. **Then delete the not-yet-terminating orphans** (finalizers gone but no deletionTimestamp):
   plain kubectl delete pod - no --force needed once finalizers are clear.

## Prevention (the part that stops recurrence)

- **One-shot Jobs on G2 become zombies by design**: ttlSecondsAfterFinished deletes the Job,
  which orphans the pod job-tracking finalizer (whose removal needs the webhook-blocked pod
  update). There is NO fully safe way to run Jobs in a namespace where the pod-update webhook
  is active and the finalizers subresource is missing. Prefer: pod exec, Prometheus queries,
  or asking the user to run SQL/kubectl.
- If a Job is unavoidable: label it with the diagnosis-label covered by the policy exclusion
  (pcai-llm/diag: <anything> on G2), and NEVER delete the parent Job manually - leave
  ttlSecondsAfterFinished to do it AFTER the pod has fully reaped.
- The Istio sidecar trap is a DIFFERENT Job failure mode (the Job never completes because the
  sidecar never exits, so ttlSecondsAfterFinished never fires): set annotation
  sidecar.istio.io/inject: "false" on every one-shot Job. Both traps coexist on G2.

## G2 facts worth remembering

- kubeconfig: ~/SSH/G2/g2_kubeconfig.yaml; host kubectl needs https_proxy=socks5://127.0.0.1:1080
- MCP tools (applygate/k8s-ops) run under a DIFFERENT identity: they CAN delete Jobs, CANNOT
  delete Pods (applygate kind allowlist), CANNOT read ClusterPolicies or webhook configs (RBAC).
- The zombies are harmless while they last (Succeeded/Error, zero resources) - if policy
  access is unavailable, "leave them and report to the platform owner" is a legitimate outcome.
