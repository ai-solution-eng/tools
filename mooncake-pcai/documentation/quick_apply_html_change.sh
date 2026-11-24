kubectl -n mooncake create configmap mooncake-ui \
  --from-file=app.py=helm/files/app.py --dry-run=client -o yaml \
  | kubectl apply -f -
kubectl -n mooncake rollout restart deploy/mooncake-ui

# kmoon -n mooncake patch role mooncake-ui-reader --type=json \
#  -p '[{"op":"add","path":"/rules/-","value":{"apiGroups":[""],"resources":["persistentvolumeclaims"],"verbs":["get","list"]}}]'
