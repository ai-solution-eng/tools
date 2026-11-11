#!/usr/bin/env python3
"""Make a model InferenceService a Mooncake client.

WHAT GOES IN THE ISVC, AND WHY IT IS NOT EVERYTHING
---------------------------------------------------
KServe Serverless wraps the predictor in a Knative Revision, and Knative
validates that revision template far more strictly than it validates a Pod:

  hostPath volumes               never allowed
  env valueFrom.fieldRef         gated by kubernetes.podspec-fieldref
  securityContext.capabilities   gated by kubernetes.podspec-securitycontext

On the G2 unit those gates are closed, so anything a catalog cannot express has
to be injected at the POD level instead - by the Kyverno policy in deploy/.
This script therefore writes only the catalog-expressible part into the ISVC:

  * the two serve flags
  * the five static env vars
  * MOONCAKE_RDMA=true          (a literal - the policy keys on it)

and, in Knative-safe mode, REMOVES anything a previous run may have written
that Knative will reject, so a half-applied patch cannot wedge the controller.

--raw-deployment switches to the full pod-spec patch (fieldRef env vars, device
limit, IPC_LOCK, /dev/infiniband) for a KServe RawDeployment cluster with no
Knative in front of it.

ROLLOUT
-------
Patching an ISVC rolls a new Knative revision. With minReplicas >= 1 the
outgoing revision keeps its pod: min-scale is fixed when a revision is created,
revisions are immutable, and no annotation will scale an old one to zero. (An
earlier version of this script patched autoscaling.knative.dev/minScale on the
revision to try. That is the wrong key - Knative reads min-scale - so it lowered
nothing, and because Knative copies revision annotations into the pod template
it instead triggered a rolling restart of the live revision.)

So: apply the patch, wait for the new revision to report Ready, then DELETE the
previous revision, which is the only thing that actually removes its pod. Both
revisions hold a pod until then - that is the price of not taking the endpoint
down. --keep-old-revision leaves the old one in place.

Usage:
  export KUBECONFIG=~/SSH/G2/g2_kubeconfig.yaml
  export https_proxy=socks5://127.0.0.1:1080
  python3 scripts/patch-model-mooncake.py --isvc deepseek-v41-flash-offload --protocol rdma --dry-run
"""
import argparse
import json
import subprocess
import sys
import time

CONTAINER = "/spec/predictor/containers/0"
PREDICTOR = "/spec/predictor"

FLAGS = ["--hicache-storage-backend", "mooncake",
         "--hicache-storage-prefetch-policy", "timeout"]

FIELDREF_NAMES = ("MOONCAKE_LOCAL_HOSTNAME", "MOONCAKE_HOST_ID")
RDMA_LIMIT = "rdma/rdma_shared_device_a"
INFINIBAND = "infiniband"

KUBE = ["kubectl"]


def run(cmd, check=True):
    try:
        return subprocess.check_output(cmd, stderr=subprocess.PIPE)
    except subprocess.CalledProcessError as exc:
        if not check:
            return b""
        detail = (exc.stderr or b"").decode("utf-8", "replace").strip()
        sys.stderr.write(detail + "\n\n")
        sys.stderr.write(
            "kubectl failed. This script uses whatever context your shell provides -\n"
            "export the cluster settings first, do not pass them here:\n"
            "  export KUBECONFIG=~/SSH/G2/g2_kubeconfig.yaml\n"
            "  export https_proxy=socks5://127.0.0.1:1080\n"
            "  python3 %s --isvc <name> --protocol rdma\n" % sys.argv[0])
        sys.exit(2)


def get_isvc(ns, isvc):
    return json.loads(run(KUBE + ["-n", ns, "get", "isvc", isvc, "-o", "json"]))


def current_revision(ns, isvc):
    """The revision KServe last rolled out.

    KServe keeps it under status.components.predictor; a bare Knative Service
    keeps it at the top level. Read both. The first version of this script only
    read the latter, which is always empty on a KServe ISVC, so current_revision
    returned None, the scale-down was skipped with a warning, and every patch
    left the outgoing revision holding its pod."""
    status = get_isvc(ns, isvc).get("status") or {}
    comp = ((status.get("components") or {}).get("predictor") or {})
    return (comp.get("latestReadyRevision")
            or comp.get("latestCreatedRevision")
            or status.get("latestReadyRevisionName")
            or status.get("latestCreatedRevisionName"))


def policy_present():
    """True / False / None. None means we could not tell (no RBAC to read a
    cluster-scoped ClusterPolicy)."""
    try:
        out = subprocess.check_output(
            KUBE + ["get", "clusterpolicy", "mlis-mooncake-rdma", "-o", "name"],
            stderr=subprocess.PIPE)
        return bool(out.strip())
    except subprocess.CalledProcessError as exc:
        err = (exc.stderr or b"").decode("utf-8", "replace")
        if "NotFound" in err or "not found" in err.lower():
            return False
        return None


def wait_for_new_revision(ns, isvc, previous, timeout):
    """Block until the ISVC reports a ready revision that is not 'previous'."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = get_isvc(ns, isvc).get("status") or {}
        comp = ((status.get("components") or {}).get("predictor") or {})
        ready = comp.get("latestReadyRevision") or status.get("latestReadyRevisionName")
        if ready and ready != previous:
            return ready
        print("  waiting for the new revision to become ready (latest ready: %s)..."
              % (ready or "-"))
        time.sleep(15)
    return None


def delete_revision(ns, revision):
    run(KUBE + ["-n", ns, "delete", "revision", revision, "--ignore-not-found=true"])


def plan(container, predictor, protocol, master, raw):
    """Decide every change and explain it. Returns (patches, notes).

    Env, volumeMounts, volumes and securityContext are written as whole-list
    replacements rather than index-level adds: the lists are small, the result
    is idempotent, and it cannot mis-index when entries are also being removed.
    Args are append-only because sglang parses argv in order and the existing
    list is the model's tuned command line.
    """
    patches, notes = [], []

    # ---- serve flags
    args = list(container.get("args") or [])
    missing = []
    for i in range(0, len(FLAGS), 2):
        if FLAGS[i] not in args:
            missing.extend([FLAGS[i], FLAGS[i + 1]])
    if missing:
        if container.get("args") is None:
            patches.append({"op": "add", "path": CONTAINER + "/args", "value": missing})
        else:
            for value in missing:
                patches.append({"op": "add", "path": CONTAINER + "/args/-", "value": value})
        notes.append("added serve flags: " + " ".join(missing))

    # ---- env
    had_env = container.get("env") is not None
    env = [dict(e) for e in (container.get("env") or [])]
    want = [
        {"name": "MOONCAKE_MASTER", "value": master + ":50051"},
        {"name": "MOONCAKE_TE_META_DATA_SERVER",
         "value": "http://" + master + ":8080/metadata"},
        {"name": "MOONCAKE_PROTOCOL", "value": protocol},
        {"name": "MOONCAKE_GLOBAL_SEGMENT_SIZE", "value": "0"},
        {"name": "MOONCAKE_CHECK_SERVER", "value": "1"},
    ]
    if protocol == "rdma":
        want.append({"name": "MOONCAKE_RDMA", "value": "true"})
    if raw:
        want.append({"name": "MOONCAKE_LOCAL_HOSTNAME",
                     "valueFrom": {"fieldRef": {"fieldPath": "status.podIP"}}})
        want.append({"name": "MOONCAKE_HOST_ID",
                     "valueFrom": {"fieldRef": {"fieldPath": "spec.nodeName"}}})

    changed = False
    for item in want:
        idx = next((i for i, e in enumerate(env) if e.get("name") == item["name"]), None)
        if idx is None:
            env.append(item)
            changed = True
            notes.append("added env " + item["name"])
        elif env[idx] != item:
            env[idx] = item
            changed = True
            notes.append("updated env " + item["name"])

    if not raw:
        kept = []
        for e in env:
            if e.get("name") in FIELDREF_NAMES and (e.get("valueFrom") or {}).get("fieldRef"):
                notes.append("removed env %s - a Knative revision cannot carry "
                             "valueFrom.fieldRef" % e.get("name"))
                changed = True
                continue
            kept.append(e)
        env = kept

    if protocol != "rdma":
        kept = [e for e in env if e.get("name") != "MOONCAKE_RDMA"]
        if len(kept) != len(env):
            notes.append("removed env MOONCAKE_RDMA (tcp)")
            changed = True
        env = kept

    if changed:
        patches.append({"op": "replace" if had_env else "add",
                        "path": CONTAINER + "/env", "value": env})

    # ---- rdma plumbing
    limits = dict(((container.get("resources") or {}).get("limits") or {}))
    had_sc = container.get("securityContext") is not None
    sc = dict(container.get("securityContext") or {})
    caps = list(((sc.get("capabilities") or {}).get("add")) or [])
    mounts = [dict(m) for m in (container.get("volumeMounts") or [])]
    vols = [dict(v) for v in (predictor.get("volumes") or [])]

    if raw and protocol == "rdma":
        if "IPC_LOCK" not in caps:
            caps.append("IPC_LOCK")
            sc["capabilities"] = {"add": caps}
            patches.append({"op": "replace" if had_sc else "add",
                            "path": CONTAINER + "/securityContext", "value": sc})
            notes.append("added securityContext capabilities: IPC_LOCK")
        if RDMA_LIMIT not in limits:
            patches.append({"op": "add",
                            "path": "%s/resources/limits/%s"
                                    % (CONTAINER, RDMA_LIMIT.replace("/", "~1")),
                            "value": "1"})
            notes.append("added limit " + RDMA_LIMIT)
        if not any(m.get("name") == INFINIBAND for m in mounts):
            patches.append({"op": "add", "path": CONTAINER + "/volumeMounts/-",
                            "value": {"name": INFINIBAND, "mountPath": "/dev/infiniband"}})
            notes.append("mounted /dev/infiniband")
        if not any(v.get("name") == INFINIBAND for v in vols):
            patches.append({"op": "add", "path": PREDICTOR + "/volumes/-",
                            "value": {"name": INFINIBAND,
                                      "hostPath": {"path": "/dev/infiniband"}}})
            notes.append("added hostPath volume /dev/infiniband")
    else:
        if "IPC_LOCK" in caps:
            caps = [c for c in caps if c != "IPC_LOCK"]
            if caps:
                sc["capabilities"] = {"add": caps}
            else:
                sc.pop("capabilities", None)
            if sc:
                patches.append({"op": "replace", "path": CONTAINER + "/securityContext",
                                "value": sc})
            else:
                patches.append({"op": "remove", "path": CONTAINER + "/securityContext"})
            notes.append("removed IPC_LOCK - Knative gates securityContext.capabilities; "
                         "the policy injects it at the pod")
        if RDMA_LIMIT in limits:
            patches.append({"op": "remove",
                            "path": "%s/resources/limits/%s"
                                    % (CONTAINER, RDMA_LIMIT.replace("/", "~1"))})
            notes.append("removed limit %s - the policy injects it at the pod" % RDMA_LIMIT)
        if any(m.get("name") == INFINIBAND for m in mounts):
            mounts = [m for m in mounts if m.get("name") != INFINIBAND]
            patches.append({"op": "replace", "path": CONTAINER + "/volumeMounts",
                            "value": mounts})
            notes.append("removed the /dev/infiniband mount - hostPath is not allowed in "
                         "a Knative revision")
        if any(v.get("name") == INFINIBAND for v in vols):
            vols = [v for v in vols if v.get("name") != INFINIBAND]
            patches.append({"op": "replace", "path": PREDICTOR + "/volumes", "value": vols})
            notes.append("removed the infiniband volume")

    return patches, notes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--isvc", required=True)
    ap.add_argument("--namespace", default="project-user-andrew-bydlon")
    ap.add_argument("--store-namespace", default="mooncake")
    ap.add_argument("--protocol", choices=["tcp", "rdma"], default="tcp")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--raw-deployment", action="store_true",
                    help="KServe RawDeployment (no Knative): write the fieldRef env vars "
                         "and the RDMA plumbing into the ISVC itself")
    ap.add_argument("--allow-missing-policy", action="store_true",
                    help="proceed with --protocol rdma even though the Kyverno policy "
                         "could not be found")
    ap.add_argument("--keep-old-revision", action="store_true",
                    help="leave the previous revision (and its pod) in place instead of "
                         "deleting it once the new one is ready")
    ap.add_argument("--timeout", type=int, default=1800,
                    help="seconds to wait for the new revision to become ready")
    args = ap.parse_args()

    master = "%s-master.%s.svc.cluster.local" % (args.store_namespace, args.store_namespace)
    obj = get_isvc(args.namespace, args.isvc)
    predictor = obj["spec"]["predictor"]
    container = predictor["containers"][0]

    raw = args.raw_deployment

    if args.protocol == "rdma" and not raw:
        present = policy_present()
        if present is False and not args.allow_missing_policy:
            print("ERROR: --protocol rdma on a Knative/KServe ISVC needs the Kyverno policy")
            print("       deploy/kyverno-mlis-mooncake-rdma.yaml. It injects the device limit")
            print("       and IPC_LOCK at pod-create time, where Knative cannot reject them.")
            print("       Install it as a cluster admin, or re-run with")
            print("       --allow-missing-policy to write only the ISVC-expressible part.")
            return 3
        if present is None:
            print("WARNING: could not read ClusterPolicy mlis-mooncake-rdma (no cluster-scoped")
            print("         RBAC for this identity); assuming it is installed.")

    patches, notes = plan(container, predictor, args.protocol, master, raw)

    if not patches:
        print("nothing to do: %s already carries the Mooncake client config" % args.isvc)
        return 0

    print("== %d change(s) ==" % len(patches))
    for note in notes:
        print("  * " + note)
    print("")
    print(json.dumps(patches, indent=2))

    if args.dry_run:
        print("")
        rev = current_revision(args.namespace, args.isvc)
        if args.keep_old_revision:
            print("(--keep-old-revision: %s keeps its pod; two copies until you remove it)"
                  % (rev or "the current revision"))
        elif rev:
            print("(rollout would: apply -> wait for the new revision to report Ready ->")
            print(" delete revision %s, which removes its pod. Two copies during the reload.)" % rev)
        else:
            print("(no current revision found in the status; nothing to retire)")
        return 0

    previous = None if args.keep_old_revision else current_revision(args.namespace, args.isvc)

    run(KUBE + ["-n", args.namespace, "patch", "isvc", args.isvc,
                "--type=json", "-p", json.dumps(patches)])

    if previous:
        print("")
        print("== waiting for the new revision, then retiring %s ==" % previous)
        new_rev = wait_for_new_revision(args.namespace, args.isvc, previous, args.timeout)
        if new_rev:
            delete_revision(args.namespace, previous)
            print("  retired %s - %s holds the only copy of the model now."
                  % (previous, new_rev))
        else:
            print("  the new revision was not ready within %ds, so %s is still running."
                  % (args.timeout, previous))
            print("  Remove the old copy once it is ready with:")
            print("    kubectl -n %s delete revision %s" % (args.namespace, previous))

    print("")
    print("Rolled a new revision. Verify once it is up:")
    print("  kubectl -n %s get pods -l serving.kserve.io/inferenceservice=%s"
          % (args.namespace, args.isvc))
    print("  kubectl -n %s logs -f <new-pod> -c kserve-container | grep -iE 'mooncake|rdma|memcpy'"
          % args.namespace)
    print("")
    print("Success is BOTH lines:")
    print("    Mooncake store setup successfully.")
    print("    Mooncake store warmup successfully.")
    if args.protocol == "rdma":
        print("and 'Found 1 HCAs' / 'Using RDMA transport', never 'TCP-only environment'.")
        if not raw:
            print("")
            print("The RDMA device and IPC_LOCK come from the Kyverno policy at pod create.")
            print("If the pod has no /dev/infiniband, the policy did not fire - check")
            print("  kubectl get clusterpolicy mlis-mooncake-rdma")
    return 0


if __name__ == "__main__":
    sys.exit(main())
