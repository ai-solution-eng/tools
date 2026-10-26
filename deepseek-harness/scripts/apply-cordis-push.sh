for ns in $(kubectl get deploy -A -l dsh-user-managed=true \
              -o jsonpath='{range .items[*]}{.metadata.namespace}{"\n"}{end}' | sort -u); do
  # keep only namespaces holding at least one REAL user unit
  # (brokers have no dsh-user-slug label; user units always do)
  kubectl get deploy -n "$ns" -l dsh-user-managed=true \
    -o jsonpath='{range .items[*]}{.metadata.labels.dsh-user-slug}{"\n"}{end}' \
    | grep -q . || continue
  echo "=== $ns ==="
  sh ./push_dsh_cordis_patch.sh --namespace "$ns" --from-cluster --apply
done