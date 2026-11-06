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

On G2, stuck Job pods hit ALL THREE (real incident 2026-10-14: nine pods, six hours;
recurrence 2026-10-29: four pods).

**Layer 2's cure is a LABEL, not a policy edit.** The webhook is gated by a label selector, so
you can switch it off for one object by giving that object the exclusion label — and you can
do that in the same request that clears the finalizer. See "The atomic patch" below.

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
   - "patched (no change)" on a CLUSTER POLICY means the value you tried to set was ALREADY
     there. Do not re-apply it; read the object and work out why it is not matching.
2. **Which finalizer?** batch.kubernetes.io/job-tracking = the Job controller owns it. If the
   parent Job is GONE, that finalizer is orphaned and NOTHING will ever remove it (upstream
   kubernetes#116758). This is the G2 signature.
3. **Does the pod actually match the webhook?** Read the labels, do not assume:
   kubectl get pod <name> -o jsonpath='{.metadata.labels}'
   The policy match is hpe-ezua/type In (app-service-user, vendor-service). The platform
   AUTO-LABELS pods in vendor/operator namespaces, so a diagnostic pod you never labeled can
   still match. The exclusion is pcai-llm/diag Exists — check whether YOUR pod has it.
4. **Is a webhook blocking updates?** Attempt the patch and READ THE ERROR. A forbidden-field
   diff you did not write (schedulerName changing) = a mutating webhook rewrote the request.
   Confirm the culprit:
   kubectl get mutatingwebhookconfigurations -o json | jq -r '.items[] | .metadata.name as $n | .webhooks[]? | select([.rules[]?.resources[]?] | index("pods")) | "\($n) \(.name) \(.failurePolicy)"'
   On G2: kyverno-resource-mutating-webhook-cfg (failurePolicy=Fail) points at ClusterPolicy
   assign-custom-scheduler-for-ezua-user-vendor-pods.
5. **Subresource check (before trying it):** pods/finalizers is NOT served on G2 (404) - do
   not burn a cycle on kubectl replace --raw .../finalizers.

## The atomic patch (the fix that actually works on G2)

Kyverno evaluates the **incoming** object, not the stored one. So send ONE merge patch that
both adds the exclusion label and clears the finalizers. On the way in the exclude matches,
the webhook does not rewrite schedulerName, the update is pure metadata, and it is allowed:

```sh
export https_proxy=socks5://127.0.0.1:1080
K="kubectl --kubeconfig <your G2 kubeconfig>"

for p in <pod1> <pod2>; do
  $K -n <ns> patch pod "$p" --type=merge \
    -p '{"metadata":{"labels":{"pcai-llm/diag":"cleanup"},"finalizers":null}}'
done
unset https_proxy
```

Two patches (label first, then finalizers) is NOT equivalent and is the mistake to avoid: the
first one fails for the same immutable-schedulerName reason, so the second never sees the label.

**Verify — kubectl "deleted" is not evidence.** A DELETE is accepted whenever the object is
already marked for deletion, and kubectl prints `pod "x" deleted` regardless. Always re-list:

```sh
$K -n <ns> get pods -o custom-columns='NAME:.metadata.name,FIN:.metadata.finalizers,DEL:.metadata.deletionTimestamp'
```

`--force --grace-period=0` does NOT bypass finalizers either — it only skips the grace period.
If the object survives both, it is layer 1 + layer 2 and you need the atomic patch.

## Fix order (older route — only if the atomic patch is unavailable)

1. **Check the ClusterPolicy BEFORE patching it.** The pcai-llm/diag exclude has been present
   on G2 since 2026-10-14; re-adding it returns `patched (no change)` and fixes nothing:

   kubectl get clusterpolicy assign-custom-scheduler-for-ezua-user-vendor-pods -o yaml

   Only if the exclude is genuinely absent, add it (use `.../exclude/any/-` to APPEND — a
   plain `add` on `/exclude` replaces the existing exclusion):

   kubectl patch clusterpolicy assign-custom-scheduler-for-ezua-user-vendor-pods --type=json \
     -p='[{"op":"add","path":"/spec/rules/0/exclude/any/-","value":{"resources":{"selector":{"matchExpressions":[{"key":"pcai-llm/diag","operator":"Exists"}]}}}}]'

   A namespace-scoped exclude (`{"resources":{"namespaces":["<ns>"]}}`) also works. Caveat: if
   the ClusterPolicy is GitOps-managed, a sync reverts it.
2. **Remove the orphaned finalizers** with the atomic patch above.
3. **Then delete the not-yet-terminating orphans** (finalizers gone but no deletionTimestamp):
   plain kubectl delete pod - no --force needed once finalizers are clear.

   Gotcha: the env-prefix form (https_proxy=... kubectl ...) applies ONLY to the first command
   in a pipeline - xargs children run WITHOUT the proxy and time out. export first.
   Also: pipes must END a line, not start one (bash cannot parse a leading pipe character).

## Prevention (the part that stops recurrence)

- **One-shot Jobs on G2 become zombies by design**: ttlSecondsAfterFinished deletes the Job,
  which orphans the pod job-tracking finalizer (whose removal needs the webhook-blocked pod
  update). There is NO fully safe way to run Jobs in a namespace where the pod-update webhook
  is active and the finalizers subresource is missing. Prefer: pod exec, Prometheus queries,
  or asking the user to run SQL/kubectl.
- If a Job is unavoidable: put `pcai-llm/diag: <anything>` in the Job's POD TEMPLATE labels
  (not just the Job's) so the exclusion covers the pod from creation, and NEVER delete the
  parent Job manually - leave ttlSecondsAfterFinished to do it AFTER the pod has fully reaped.
- The Istio sidecar trap is a DIFFERENT Job failure mode (the Job never completes because the
  sidecar never exits, so ttlSecondsAfterFinished never fires): set annotation
  sidecar.istio.io/inject: "false" on every one-shot Job. Both traps coexist on G2.

## G2 facts worth remembering

- kubeconfig: your G2 kubeconfig; host kubectl needs https_proxy=socks5://127.0.0.1:1080
- MCP tools (applygate/k8s-ops) run under a DIFFERENT identity: they CAN delete Jobs, CANNOT
  delete Pods (applygate kind allowlist), CANNOT read ClusterPolicies or webhook configs (RBAC).
- Operator namespaces (e.g. applygate-mcp) get `hpe-ezua/type: vendor-service` auto-stamped on
  every pod, so ANY diagnostic Job run there matches the scheduler-stamping policy.
- The zombies are harmless while they last (Succeeded/Error, zero resources) - if policy
  access is unavailable, "leave them and report to the platform owner" is a legitimate outcome.
