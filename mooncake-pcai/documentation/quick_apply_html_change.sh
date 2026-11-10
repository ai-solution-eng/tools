kubectl -n mooncake create configmap mooncake-ui \
  --from-file=app.py=helm/files/app.py --dry-run=client -o yaml \
  | kubectl apply -f -
kubectl -n mooncake rollout restart deploy/mooncake-ui
