#!/usr/bin/env bash
# This script uses bash arrays and `set -o pipefail`. Running it as
# `sh push_dsh_cordis_patch.sh` on a system whose /bin/sh is dash (Debian,
# Ubuntu) fails with a syntax error, so re-exec under bash when needed.
if [ -z "${BASH_VERSION:-}" ]; then
  if command -v bash >/dev/null 2>&1; then
    exec bash "$0" "$@"
  fi
  echo "error: this script requires bash (arrays + pipefail); bash was not found" >&2
  exit 1
fi
set -euo pipefail

KUBECTL="${KUBECTL:-kubectl}"
NAMESPACE="${NAMESPACE:-dsh-web-helm}"
CONTAINER="${CONTAINER:-dsh-web}"
# Namespace that holds the release's '<release>-project-config' ConfigMap. This
# is NOT necessarily the target namespace: platform (SSO) users get their unit
# in 'project-user-<slug>', while the ConfigMap is rendered into
# .Release.Namespace. Left empty it auto-detects: the target namespace if the
# ConfigMap exists there, else the chart's release namespace.
RELEASE_NAMESPACE="${RELEASE_NAMESPACE:-}"
PROFILE_DIR="/var/dsh/home/.dsh/profiles/web"
# The LIVE patch: the one dsh actually loads and the supervisor watches.
# Nothing in this script writes it unless --live is passed explicitly.
LIVE="$PROFILE_DIR/cordis.patch.yml"
LIVE_NAME="cordis.patch.yml"
BACKUP_NAME="cordis.patch-backup.yml"
# Where a staged push lands: a SIBLING file. This is safe because dsh
# resolves the patch by FIXED filename, not by glob
# (dsh-app-boot: `join(dir, PROFILE_PATCH_FILENAME)`), and the in-pod
# supervisor watches an explicit file list (DSH_WATCH_CONFIG_FILES), with
# DSH_WATCH_CONFIG_DIRS empty on the shipped units. So a sibling .yml is
# completely inert: it is neither loaded nor watched, and cannot trigger a
# restart. (The pre-existing cordis.patch-backup.yml already relies on this.)
TARGET_NAME="${TARGET_NAME:-cordis.patch-update.yml}"
TARGET="$PROFILE_DIR/$TARGET_NAME"
# --live is the supported spelling for a live overwrite. A TARGET_NAME naming the
# live file is still honoured (backwards compatibility) but warned about: it is
# the one setting that rewrites a user's patch, and --live is the mode that
# takes a backup first.
TARGET_NAME_FROM_ENV=0
if [ "$TARGET_NAME" = "$LIVE_NAME" ]; then
  TARGET_NAME_FROM_ENV=1
fi
# The cluster-side CANONICAL copy: the release's '<release>-project-config'
# ConfigMap, key cordis.patch.yml. --to-router writes exactly this one key and
# nothing else. It is what --from-configmap reads, what show-current-config.sh
# prints, and what a NEW unit seeds from (the chart mounts the ConfigMap at
# /opt/dsh-patch and the init container copies it only when the live file does
# not exist yet).
CM_NAME="${CONFIGMAP_NAME:-dsh-web-helm-project-config}"
CM_KEY="cordis.patch.yml"
# What lands in the pod.
#   staged (default): a plain write of the inert sibling.
#   live (--live):    backup FIRST, then write IN PLACE (`cat > file`, same
#                     inode). Deliberately not temp-file+mv: the in-pod
#                     supervisor watches the live file by path, so a
#                     rename-over can orphan its watch.
push_cmd() {
  if [ "$TARGET_NAME" = "$LIVE_NAME" ]; then
    printf '%s' "mkdir -p $PROFILE_DIR && if [ -f $LIVE ]; then cp -p $LIVE $PROFILE_DIR/$BACKUP_NAME; fi && cat > $LIVE"
  else
    printf '%s' "mkdir -p $PROFILE_DIR && cat > $TARGET"
  fi
}

usage() {
  cat <<EOF
usage: push-dsh-cordis-patch.sh (--file cordis.patch.yml | --from-configmap | --from-cluster)
                                [--to-router [--router-only]] [--live]
                                [--user ID]... [--apply] [--namespace NS]
                                [--configmap-namespace NS]

Pushes the dsh web profile's patch layer (MCP integrations, session content
search, title-service caps). Two independent steps, usable together:

  CANONICAL  --to-router   writes the cluster's single canonical copy:
                           ConfigMap <release>-project-config, key
                           cordis.patch.yml. No pod is exec'd and no user file
                           is read or written. This is what --from-configmap
                           reads, what show-current-config.sh prints, and what a
                           NEW unit seeds from (the chart mounts that ConfigMap
                           at /opt/dsh-patch; the init container copies it only
                           when the unit has no patch yet).
  USERS      (default)     stages the patch into every dsh-web unit as
                           profiles/web/cordis.patch-update.yml.

IMPORTANT — a user's LIVE patch is NOT overwritten by default:

  live (only --live)    $LIVE
  staged by this script $TARGET

The staged sibling is inert: dsh loads the patch by fixed filename (not a
glob) and the in-pod supervisor watches an explicit file list, so nothing
reloads and no restart is triggered. Promote deliberately, per unit:

  kubectl exec -n <ns> deploy/<unit> -c $CONTAINER -- sh -c 'cd $PROFILE_DIR && cp -p cordis.patch.yml cordis.patch-backup.yml && mv cordis.patch-update.yml cordis.patch.yml'

  --file PATH          the local YAML patch file: source for --to-router and
                       for the user fan-out
  --to-router          publish --file into the canonical ConfigMap as a merge
                       patch of that ONE key. Idempotent (skipped when already
                       equal) and verified by reading the key back.
  --router-only        with --to-router: stop after the canonical write and
                       never touch a pod (zero exec calls).
  --live               write the LIVE file in each pod instead of the staged
                       sibling. A rolling backup ($BACKUP_NAME)
                       is taken first and the supervisor restarts dsh on the
                       change. This is the ONLY mode that modifies a user's
                       patch; it requires --apply.
  --from-configmap     (recommended source for the fan-out) read the LATEST
                       patch as rendered from values.yaml — from the
                       '<release>-project-config' ConfigMap (key: cordis.patch.yml),
                       i.e. exactly what --to-router writes. Requires an
                       applied helm upgrade. Mutually exclusive with --file /
                       --from-cluster.
  --from-cluster       LEGACY fallback: reads the base64 patch baked into the
                       FIRST selected unit's init-dsh-storage init container.
                       That copy only refreshes when the unit re-stamps, so it
                       can be STALE — prefer --from-configmap.
                       Mutually exclusive with the others.
  --user ID            push ONLY to the unit(s) of this user — the safe way to
                       try a patch on yourself first. Repeatable and
                       comma-separated: --user me --user you  /  --user me,you
                       ID is matched, case-insensitively, against the unit's
                       dsh-user-slug and dsh-user-owner labels, against the
                       deployment name, and against the deployment name with
                       its dsh-user- prefix stripped; a username is also
                       normalised to its slug the way the router does
                       ("Francesco.Caliva" -> francesco-caliva).
                       Omit --user to push to EVERY user unit (the default).
                       An ID that matches no unit is an ERROR (exit 1), so a
                       typo can never silently fall back to pushing to all.
  --list-users         list the user units in the namespace together with the
                       ID to pass to --user, then exit (read-only)
  --apply              actually run
  (default)            dry-run preview only
  --namespace NS       target namespace (default: $NAMESPACE)
  --configmap-namespace NS
                       namespace holding '<release>-project-config' for
                       --from-configmap. Needed when the target namespace is a
                       platform user's own (project-user-<slug>): the ConfigMap
                       lives in the RELEASE namespace, not the user's. Left
                       unset, the target namespace is tried first and the
                       release namespace is used as a fallback.
  --help               this help

env overrides: KUBECTL (default: kubectl), NAMESPACE, CONTAINER (default: $CONTAINER),
               CONFIGMAP_NAME (default: dsh-web-helm-project-config),
               TARGET_NAME (LEGACY: sets the in-pod filename directly; --live is
               the supported way to write the live patch and is the only mode
               that backs it up first),
               RELEASE_NAMESPACE (ConfigMap namespace fallback; default: auto)
EOF
}

APPLY=0
FILE=""
FROM_CLUSTER=0
FROM_CONFIGMAP=0
TO_ROUTER=0
ROUTER_ONLY=0
LIST_USERS=0
USERS=()

# Scratch file for --from-cluster (the extracted patch). Created under TMPDIR
# and removed on exit; never written into the repo.
TMP_PATCH=""
TMP_PAYLOAD=""
TMP_READBACK=""
cleanup_tmp() {
  local f
  for f in "$TMP_PATCH" "$TMP_PAYLOAD" "$TMP_READBACK"; do
    if [ -n "$f" ] && [ -f "$f" ]; then
      rm -f "$f"
    fi
  done
}
trap cleanup_tmp EXIT

# The router derives a unit's slug from the username by lowercasing it and
# collapsing every run of non-alphanumerics to a single dash
# (templates/configmap-router.yaml, slugifyUsername). Mirroring that here lets
# an operator pass the SSO/platform username instead of the slug.
normalize_user() {
  printf '%s' "$1" \
    | LC_ALL=C tr '[:upper:]' '[:lower:]' \
    | LC_ALL=C sed -e 's/[^a-z0-9][^a-z0-9]*/-/g' -e 's/^-//' -e 's/-$//'
}

# add_users <comma-separated ids> — split and accumulate into USERS.
add_users() {
  local raw="$1" part
  # Split on commas only. Globbing is disabled around the word-splitting loop
  # so an id containing '*' or '?' is taken literally instead of being expanded
  # against the current directory.
  local IFS=','
  set -f
  for part in $raw; do
    # trim surrounding whitespace so "a, b" works
    part="$(printf '%s' "$part" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
    if [ -n "$part" ]; then
      USERS+=("$part")
    fi
  done
  set +f
}

# --------------------------------------------------------------- helpers
# sha256 of a file, without assuming which tool is installed.
file_sha256() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$1" | awk '{print $1}'
  elif command -v python3 >/dev/null 2>&1; then
    python3 - "$1" <<'PY'
import hashlib, sys
h = hashlib.sha256()
with open(sys.argv[1], 'rb') as f:
    for chunk in iter(lambda: f.read(65536), b''):
        h.update(chunk)
print(h.hexdigest())
PY
  else
    printf 'unknown'
  fi
}

# build_patch_json SRC OUT — the JSON merge patch for ONE ConfigMap key.
# The key contains dots; it is built as data, never spliced into a jsonpath.
build_patch_json() {
  local src="$1" out="$2"
  if command -v python3 >/dev/null 2>&1; then
    python3 - "$src" "$out" "$CM_KEY" <<'PY'
import json, sys
src, out, key = sys.argv[1], sys.argv[2], sys.argv[3]
with open(src, 'rb') as f:
    content = f.read().decode('utf-8')
with open(out, 'w', encoding='utf-8') as f:
    json.dump({'data': {key: content}}, f)
PY
  elif command -v jq >/dev/null 2>&1; then
    jq -Rs --arg k "$CM_KEY" '{data: {($k): .}}' "$src" > "$out"
  else
    echo "error: --to-router needs python3 or jq to build the merge patch" >&2
    return 1
  fi
}

# Pre-flight YAML check. Three outcomes are deliberately distinguished, so a
# missing parser never masquerades as a broken file:
#   0 = parses, 1 = genuinely invalid YAML, 3 = PyYAML not installed.
# cordis patches use the !!js tag, which PyYAML cannot construct: a plain
# safe_load rejects EVERY real patch (false alarm). Register the tag as an
# opaque scalar — the same posture the in-pod supervisor takes (js-yaml parses
# the tag without evaluating it at load time).
validate_patch_yaml() {
  local file="$1" rc=3
  if command -v python3 >/dev/null 2>&1; then
    if python3 - "$file" <<'PY' >/dev/null 2>&1
import sys
try:
    import yaml
except ImportError:
    sys.exit(3)
class Js(str):
    pass
yaml.SafeLoader.add_constructor('tag:yaml.org,2002:js', lambda l, n: Js(l.construct_scalar(n)))
try:
    yaml.safe_load(open(sys.argv[1]))
except Exception:
    sys.exit(1)
sys.exit(0)
PY
    then
      rc=0
    else
      rc=$?
    fi
  fi
  case "$rc" in
    0) : ;;
    1) echo "warning: $file is not valid YAML; pushing anyway — the in-pod supervisor validates and would reject a broken patch" >&2 ;;
    *) echo "note: YAML pre-check skipped (python3 or PyYAML unavailable) — the in-pod supervisor still validates before restarting dsh" >&2 ;;
  esac
}

# The namespace holding '<release>-project-config' — .Release.Namespace, which is
# NOT necessarily the target namespace: platform (SSO) users get their unit in
# 'project-user-<slug>'.
derive_release_namespace() {
  # Every step is guarded: under 'set -euo pipefail' a failing or empty
  # cluster-wide query (RBAC/policy-blocked -A, no matches) would otherwise
  # abort the whole script with a silent exit 1. Failing to DERIVE a namespace
  # is not an error — the caller still has $NAMESPACE to try.
  "$KUBECTL" get deploy -A -l "hpe-ezua/app=dsh-web-helm" \
    -o jsonpath='{range .items[*]}{.metadata.namespace}{"\n"}{end}' 2>/dev/null \
    | head -1 || true
}

# Prints the namespace that holds the ConfigMap, or returns 1 with the reason on
# stderr. Resolution order (first hit wins): --configmap-namespace /
# RELEASE_NAMESPACE when set (explicit, no probing), then $NAMESPACE, then the
# release namespace derived from the router Deployment.
resolve_cm_namespace() {
  local cands ns derived err=""
  if [ -n "$RELEASE_NAMESPACE" ]; then
    cands="$RELEASE_NAMESPACE"
  else
    cands="$NAMESPACE"
    derived="$(derive_release_namespace)"
    if [ -n "$derived" ] && [ "$derived" != "$NAMESPACE" ]; then
      cands="$cands $derived"
    fi
  fi
  for ns in $cands; do
    if "$KUBECTL" get configmap -n "$ns" "$CM_NAME" -o jsonpath='{.metadata.name}' >/dev/null 2>&1; then
      printf '%s' "$ns"
      return 0
    fi
    err="$("$KUBECTL" get configmap -n "$ns" "$CM_NAME" 2>&1 || true)"
  done
  echo "error: ConfigMap '$CM_NAME' not found in: $cands" >&2
  if [ -n "$err" ]; then
    echo "       last kubectl error: $err" >&2
  fi
  echo "       (upgrade the chart so templates/configmap-project.yaml is applied," >&2
  echo "        pass --configmap-namespace <release-ns>, or use --file / --from-cluster.)" >&2
  return 1
}

# --------------------------------------------------------------- canonical write
# Publish the local patch into the cluster's single canonical copy. Deliberately
# narrow: ONE key of ONE ConfigMap, no pod access, idempotent, verified by
# read-back. This is the step that makes a local file the source of truth for
# what --from-configmap pushes and what a NEW unit seeds from.
write_canonical() {
  local ns payload readback current_sha local_sha bytes err
  ns="$(resolve_cm_namespace)" || return 1

  if [ ! -f "$FILE" ]; then
    echo "error: file not found: $FILE" >&2
    return 1
  fi
  if [ ! -s "$FILE" ]; then
    echo "error: refusing to publish an empty patch: $FILE" >&2
    return 1
  fi

  local_sha="$(file_sha256 "$FILE")"
  bytes="$(wc -c < "$FILE" | tr -d ' ')"
  TMP_PAYLOAD="$(mktemp "${TMPDIR:-/tmp}/cordis.patch.json.XXXXXX")"
  TMP_READBACK="$(mktemp "${TMPDIR:-/tmp}/cordis.patch.rb.XXXXXX")"
  payload="$TMP_PAYLOAD"
  readback="$TMP_READBACK"
  build_patch_json "$FILE" "$payload" || return 1

  # Idempotency: compare with what the cluster already has (byte-exact).
  if "$KUBECTL" get configmap -n "$ns" "$CM_NAME" \
        -o jsonpath='{.data.cordis\.patch\.yml}' > "$readback" 2>/dev/null; then
    if cmp -s "$FILE" "$readback"; then
      echo "canonical: already up to date ($CM_NAME in '$ns', key $CM_KEY, sha256 $local_sha)"
      return 0
    fi
  fi

  if [ "$APPLY" -eq 0 ]; then
    echo "canonical: would patch configmap/$CM_NAME in '$ns' (key $CM_KEY, $bytes bytes, sha256 $local_sha)"
    return 0
  fi

  # JSON merge patch of data[<key>] ONLY: never apply/replace, which would drop
  # labels, annotations and any sibling key.
  if err="$("$KUBECTL" patch configmap -n "$ns" "$CM_NAME" --type merge \
        --patch-file "$payload" 2>&1)"; then
    :
  else
    case "$err" in
      *"unknown flag"*|*"unknown shorthand"*)
        # kubectl < 1.21 has no --patch-file.
        if ! err="$("$KUBECTL" patch configmap -n "$ns" "$CM_NAME" --type merge \
              -p "$(cat "$payload")" 2>&1)"; then
          echo "error: kubectl patch failed: $err" >&2
          return 1
        fi
        ;;
      *)
        echo "error: kubectl patch failed: $err" >&2
        return 1
        ;;
    esac
  fi

  # Verify by reading the key back to a FILE (never $(...), which strips
  # trailing newlines and would hide a byte-level difference).
  if ! "$KUBECTL" get configmap -n "$ns" "$CM_NAME" \
        -o jsonpath='{.data.cordis\.patch\.yml}' > "$readback" 2>/dev/null; then
    echo "error: the canonical write reported success but the read-back failed" >&2
    return 1
  fi
  if ! cmp -s "$FILE" "$readback"; then
    echo "error: canonical write verification FAILED — the ConfigMap content differs from $FILE" >&2
    echo "       (ConfigMap: $CM_NAME in '$ns', key $CM_KEY)" >&2
    return 1
  fi
  echo "canonical: wrote $CM_NAME in '$ns' (key $CM_KEY, $bytes bytes, sha256 $local_sha) — verified by read-back"
}

while [ $# -gt 0 ]; do
  case "$1" in
    --apply)
      APPLY=1
      shift
      ;;
    --dry-run)
      APPLY=0
      shift
      ;;
    --file)
      shift
      FILE="${1:-}"
      if [ -z "$FILE" ]; then
        echo "error: --file requires a path" >&2
        exit 1
      fi
      shift
      ;;
    --user)
      shift
      if [ -z "${1:-}" ]; then
        echo "error: --user requires an id (e.g. --user francesco-caliva)" >&2
        exit 1
      fi
      add_users "$1"
      shift
      ;;
    --user=*)
      _user_arg="${1#--user=}"
      if [ -z "$_user_arg" ]; then
        echo "error: --user requires an id (e.g. --user=francesco-caliva)" >&2
        exit 1
      fi
      add_users "$_user_arg"
      shift
      ;;
    --from-cluster)
      FROM_CLUSTER=1
      shift
      ;;
    --from-configmap)
      FROM_CONFIGMAP=1
      shift
      ;;
    --to-router)
      TO_ROUTER=1
      shift
      ;;
    --router-only)
      TO_ROUTER=1
      ROUTER_ONLY=1
      shift
      ;;
    --live)
      TARGET_NAME="$LIVE_NAME"
      TARGET="$PROFILE_DIR/$TARGET_NAME"
      TARGET_NAME_FROM_ENV=0
      shift
      ;;
    --list-users)
      LIST_USERS=1
      shift
      ;;
    --configmap-namespace)
      shift
      RELEASE_NAMESPACE="${1:-}"
      if [ -z "$RELEASE_NAMESPACE" ]; then
        echo "error: --configmap-namespace requires a value" >&2
        exit 1
      fi
      shift
      ;;
    --namespace|-n)
      shift
      NAMESPACE="${1:-}"
      if [ -z "$NAMESPACE" ]; then
        echo "error: --namespace requires a value" >&2
        exit 1
      fi
      shift
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      echo "error: unknown argument: $1" >&2
      usage
      exit 1
      ;;
  esac
done

# --list-users is a read-only inventory query, so it needs no patch source.
if [ "$LIST_USERS" -eq 0 ]; then
  _sources=0
  [ -n "$FILE" ] && _sources=$((_sources + 1))
  [ "$FROM_CLUSTER" -eq 1 ] && _sources=$((_sources + 1))
  [ "$FROM_CONFIGMAP" -eq 1 ] && _sources=$((_sources + 1))
  if [ "$_sources" -gt 1 ]; then
    echo "error: --file, --from-cluster and --from-configmap are mutually exclusive" >&2
    exit 1
  fi
  # --to-router PUBLISHES --file; it is not a source. Combining it with a read
  # mode makes no sense (publishing the ConfigMap back into itself is a no-op
  # nobody asked for), so reject the mix instead of guessing.
  if [ "$TO_ROUTER" -eq 1 ]; then
    if [ "$FROM_CLUSTER" -eq 1 ] || [ "$FROM_CONFIGMAP" -eq 1 ]; then
      echo "error: --to-router takes --file (the patch to publish); it cannot be combined with --from-configmap / --from-cluster" >&2
      exit 1
    fi
    if [ -z "$FILE" ]; then
      echo "error: --to-router requires --file PATH (the patch to publish)" >&2
      usage
      exit 1
    fi
  fi
  # Exactly one source is required (--to-router always brings --file with it).
  if [ "$_sources" -eq 0 ]; then
    echo "error: a patch source is required: --file cordis.patch.yml, --from-configmap or --from-cluster" >&2
    usage
    exit 1
  fi

  if [ "$FROM_CLUSTER" -eq 0 ] && [ "$FROM_CONFIGMAP" -eq 0 ]; then
    if [ ! -f "$FILE" ]; then
      echo "error: file not found: $FILE" >&2
      echo "       (the patch is not a repo file by default — it lives in values.yaml as" >&2
      echo "        dshCordisPatch. Extract it, or use --from-cluster to push the patch" >&2
      echo "        the chart already rendered into the units.)" >&2
      exit 1
    fi
    if [ ! -s "$FILE" ]; then
      echo "error: file is empty: $FILE" >&2
      exit 1
    fi
    validate_patch_yaml "$FILE"
  fi
fi

# A TARGET_NAME naming the live file is the one legacy spelling that rewrites a
# user's patch, and it does not back it up; --live is the supported form.
if [ "$TARGET_NAME_FROM_ENV" -eq 1 ]; then
  echo "warning: TARGET_NAME=$LIVE_NAME overwrites the LIVE patch. Use --live, which backs it up first." >&2
fi

if [ "$APPLY" -eq 0 ]; then
  echo "DRY RUN - nothing executed"
fi

# --------------------------------------------------------------- canonical step
# Runs BEFORE the user fan-out, so the cluster's canonical copy is already
# current when a new unit seeds from it. --router-only stops here: no inventory,
# no exec, no pod touched.
if [ "$TO_ROUTER" -eq 1 ]; then
  write_canonical || exit 1
  if [ "$ROUTER_ONLY" -eq 1 ]; then
    if [ "$APPLY" -eq 0 ]; then
      echo "canonical only (--router-only): no pod was touched"
    else
      echo "done: canonical copy updated; no pod was touched"
      echo "      push it to users later with: $0 --from-configmap --apply"
    fi
    exit 0
  fi
fi

# ------------------------------------------------------------------ inventory
# One line per dsh-user-managed deployment: name|slug|owner|dind-broker.
# "|" cannot appear in a Kubernetes name/label value, so it is a safe field
# separator. The dsh-user-slug / dsh-user-owner labels are the router's own
# definition of "this deployment is that user's unit"; the dsh-dind-broker
# label marks the separate Docker-in-Docker broker deployments, which carry
# the managed label but have no dsh-web container and no ~/.dsh.
if ! INVENTORY="$("$KUBECTL" get deploy -n "$NAMESPACE" -l dsh-user-managed=true \
      -o jsonpath='{range .items[*]}{.metadata.name}{"|"}{.metadata.labels.dsh-user-slug}{"|"}{.metadata.labels.dsh-user-owner}{"|"}{.metadata.labels.dsh-dind-broker}{"\n"}{end}' 2>&1)"; then
  echo "error: kubectl failed: $INVENTORY" >&2
  exit 1
fi

ALL_NAMES=()
ALL_SLUGS=()
ALL_OWNERS=()
ALL_BROKERS=()
while IFS='|' read -r _n _s _o _b; do
  if [ -z "$_n" ]; then
    continue
  fi
  ALL_NAMES+=("$_n")
  ALL_SLUGS+=("$_s")
  ALL_OWNERS+=("$_o")
  ALL_BROKERS+=("$_b")
done <<< "$INVENTORY"

if [ "${#ALL_NAMES[@]}" -eq 0 ]; then
  echo "no user deployments found in namespace '$NAMESPACE' (label selector: dsh-user-managed=true)"
  exit 0
fi

# --list-users: print the ids this namespace accepts and stop (read-only).
if [ "$LIST_USERS" -eq 1 ]; then
  echo "user units in namespace '$NAMESPACE' (label selector: dsh-user-managed=true)"
  printf '  %-38s %-26s %s\n' "DEPLOYMENT" "--user ID" "NOTE"
  for _i in "${!ALL_NAMES[@]}"; do
    _id="${ALL_SLUGS[$_i]:-${ALL_OWNERS[$_i]:-}}"
    if [ "${ALL_BROKERS[$_i]}" = "true" ]; then
      printf '  %-38s %-26s %s\n' "${ALL_NAMES[$_i]}" "-" "docker-in-docker broker, not a dsh unit"
    elif [ -z "$_id" ]; then
      printf '  %-38s %-26s %s\n' "${ALL_NAMES[$_i]}" "-" "no dsh-user-slug / dsh-user-owner label"
    else
      printf '  %-38s %-26s\n' "${ALL_NAMES[$_i]}" "$_id"
    fi
  done
  exit 0
fi

# --------------------------------------------------------------- user filter
SEL_NAMES=()
SEL_USERS=()

if [ "${#USERS[@]}" -eq 0 ]; then
  # No --user given: every USER unit in the namespace (the historical
  # behaviour). Docker-in-Docker brokers carry the same managed label but run
  # only the docker-dind container and have no ~/.dsh, so pushing to them
  # could only ever fail — they are skipped here and reported below.
  SKIPPED_BROKERS=()
  for _i in "${!ALL_NAMES[@]}"; do
    if [ "${ALL_BROKERS[$_i]}" = "true" ]; then
      SKIPPED_BROKERS+=("${ALL_NAMES[$_i]}")
      continue
    fi
    SEL_NAMES+=("${ALL_NAMES[$_i]}")
    SEL_USERS+=("${ALL_SLUGS[$_i]:-${ALL_OWNERS[$_i]:-}}")
  done
  if [ "${#SKIPPED_BROKERS[@]}" -gt 0 ]; then
    echo "note: skipping ${#SKIPPED_BROKERS[@]} docker-in-docker broker deployment(s) (no dsh-web container): ${SKIPPED_BROKERS[*]}" >&2
  fi
  if [ "${#SEL_NAMES[@]}" -eq 0 ]; then
    echo "no dsh user units found in namespace '$NAMESPACE' (only docker-in-docker brokers are managed)"
    exit 0
  fi
else
  MISSING=()
  for _u in "${USERS[@]}"; do
    _lower="$(printf '%s' "$_u" | LC_ALL=C tr '[:upper:]' '[:lower:]')"
    _norm="$(normalize_user "$_u")"
    _found=0
    for _i in "${!ALL_NAMES[@]}"; do
      # A dind broker is never a dsh unit: skip it so "--user me" cannot
      # select the broker instead of (or as well as) the real unit.
      if [ "${ALL_BROKERS[$_i]}" = "true" ]; then
        continue
      fi
      _n="$(printf '%s' "${ALL_NAMES[$_i]}" | LC_ALL=C tr '[:upper:]' '[:lower:]')"
      _s="$(printf '%s' "${ALL_SLUGS[$_i]}" | LC_ALL=C tr '[:upper:]' '[:lower:]')"
      _o="$(printf '%s' "${ALL_OWNERS[$_i]}" | LC_ALL=C tr '[:upper:]' '[:lower:]')"
      _np="${_n#dsh-user-}"
      _match=0
      for _cand in "$_s" "$_o" "$_n" "$_np"; do
        if [ -z "$_cand" ]; then
          continue
        fi
        if [ "$_cand" = "$_lower" ]; then
          _match=1
        elif [ -n "$_norm" ] && [ "$_cand" = "$_norm" ]; then
          _match=1
        fi
      done
      if [ "$_match" -eq 1 ]; then
        _found=1
        _already=0
        # ${SEL_NAMES[@]+...} keeps this safe on bash < 4.4 under set -u,
        # where expanding an empty array is an unbound-variable error.
        for _seen in ${SEL_NAMES[@]+"${SEL_NAMES[@]}"}; do
          if [ "$_seen" = "${ALL_NAMES[$_i]}" ]; then
            _already=1
          fi
        done
        if [ "$_already" -eq 0 ]; then
          SEL_NAMES+=("${ALL_NAMES[$_i]}")
          # Record the unit's OWN identity (its slug/owner label) rather than
          # the string the operator typed, so the preview always names the
          # real user even when --user was a deployment name or a username.
          SEL_USERS+=("${ALL_SLUGS[$_i]:-${ALL_OWNERS[$_i]:-}}")
        fi
      fi
    done
    if [ "$_found" -eq 0 ]; then
      MISSING+=("$_u")
    fi
  done

  if [ "${#MISSING[@]}" -gt 0 ]; then
    echo "error: no dsh user unit in namespace '$NAMESPACE' matches: ${MISSING[*]}" >&2
    echo "       (label selector: dsh-user-managed=true) — nothing was pushed" >&2
    AVAILABLE=""
    for _i in "${!ALL_NAMES[@]}"; do
      if [ "${ALL_BROKERS[$_i]}" != "true" ]; then
        _id="${ALL_SLUGS[$_i]:-${ALL_OWNERS[$_i]:-}}"
        if [ -n "$_id" ]; then
          AVAILABLE="$AVAILABLE $_id"
        fi
      fi
    done
    if [ -n "$AVAILABLE" ]; then
      echo "       available --user IDs:$AVAILABLE" >&2
    else
      echo "       this namespace has no user units; try --list-users" >&2
    fi
    for _u in "${MISSING[@]}"; do
      _norm="$(normalize_user "$_u")"
      for _i in "${!ALL_NAMES[@]}"; do
        if [ "${ALL_BROKERS[$_i]}" = "true" ] && [ "${ALL_NAMES[$_i]}" = "dsh-dind-$_norm" ]; then
          echo "       note: ${ALL_NAMES[$_i]} is a Docker-in-Docker broker, not a dsh unit" >&2
        fi
      done
      # Platform (Option B) users get their unit in a per-user namespace, so a
      # missing id here is often just the wrong namespace. Only suggest it when
      # that namespace actually exists — never send the operator on a wild goose
      # chase after a plain typo.
      if [ -n "$_norm" ] && "$KUBECTL" get namespace "project-user-$_norm" >/dev/null 2>&1; then
        echo "       note: namespace 'project-user-$_norm' exists — this user's unit may live there:" >&2
        echo "         $0 --namespace project-user-$_norm --user '$_u' ..." >&2
      fi
    done
    exit 1
  fi
fi

COUNT="${#SEL_NAMES[@]}"

# ----------------------------------------------- source: --from-configmap
# The canonical copy of the patch as rendered from values.yaml dshCordisPatch
# lives in the release's '<release>-project-config' ConfigMap (see
# templates/configmap-project.yaml). This is the "latest from values" an
# administrator reads without touching a pod, and the natural source for a
# push. Falls back with a clear message if the chart predates that ConfigMap.
#
# NAMESPACE vs RELEASE NAMESPACE: the ConfigMap is rendered into
# .Release.Namespace, but a platform (SSO) user's unit lives in its own
# 'project-user-<slug>' namespace. Looking only in $NAMESPACE therefore failed
# with NotFound for exactly the users an operator most wants to push to.
# Resolution order (first hit wins):
#   1. --configmap-namespace / RELEASE_NAMESPACE, when set (explicit, no probing)
#   2. $NAMESPACE          (classic/local users: the unit IS in the release ns)
#   3. the release namespace derived from the router Deployment's namespace
if [ "$FROM_CONFIGMAP" -eq 1 ]; then
  TMP_PATCH="${TMPDIR:-/tmp}/cordis.patch.$$.yml"
  # The ConfigMap is rendered into .Release.Namespace, which is NOT necessarily
  # the target namespace: a platform (SSO) user's unit lives in its own
  # 'project-user-<slug>' namespace. resolve_cm_namespace() tries
  # --configmap-namespace / RELEASE_NAMESPACE, then $NAMESPACE, then the release
  # namespace derived from the router Deployment — and explains the failure.
  CM_FOUND_NS="$(resolve_cm_namespace)" || exit 1
  if ! CM_PATCH="$("$KUBECTL" get configmap -n "$CM_FOUND_NS" "$CM_NAME" \
        -o jsonpath='{.data.cordis\.patch\.yml}' 2>&1)"; then
    echo "error: kubectl failed reading '$CM_NAME' in '$CM_FOUND_NS': $CM_PATCH" >&2
    exit 1
  fi
  if [ -z "$CM_PATCH" ]; then
    echo "error: ConfigMap '$CM_NAME' in '$CM_FOUND_NS' has no 'cordis.patch.yml' key (or it is empty)" >&2
    echo "       Is values.dshCordisPatch set, or has --to-router published a patch?" >&2
    echo "       Use --file / --from-cluster instead." >&2
    exit 1
  fi
  printf '%s' "$CM_PATCH" > "$TMP_PATCH"
  if [ ! -s "$TMP_PATCH" ]; then
    echo "error: the ConfigMap payload decoded to an empty file" >&2
    exit 1
  fi
  FILE="$TMP_PATCH"
  echo "source: --from-configmap ($CM_NAME in '$CM_FOUND_NS' -> cordis.patch.yml)"
fi

# --------------------------------------------------- source: --from-cluster
# Legacy path: older charts do not ship the project-config ConfigMap, so the
# patch is only visible as the base64 blob BAKED into the init-dsh-storage
# init container (values.yaml dshCordisPatch -> templates/configmap-router.yaml).
# Prefer --from-configmap; this remains for pre-upgrade clusters.
if [ "$FROM_CLUSTER" -eq 1 ]; then
  TMP_PATCH="${TMPDIR:-/tmp}/cordis.patch.$$.yml"
  SOURCE_UNIT="${SEL_NAMES[0]}"
  if ! INIT_SPEC="$("$KUBECTL" get deploy -n "$NAMESPACE" "$SOURCE_UNIT" \
        -o jsonpath='{range .spec.template.spec.initContainers[*]}{.name}{"\t"}{.command[*]}{"\n"}{end}' 2>&1)"; then
    echo "error: kubectl failed reading '$SOURCE_UNIT': $INIT_SPEC" >&2
    exit 1
  fi
  # Pull the base64 payload that the chart emits immediately before the
  # '| base64 -d > .../cordis.patch.yml' redirect. Matching on the redirect
  # (not on the init container's name) keeps this working if the container is
  # ever renamed; the .env seed uses the same shape and is excluded by the
  # target path in the pattern.
  PATCH_B64="$(printf '%s' "$INIT_SPEC" | sed -n \
    's/.*printf %s \\*"\([A-Za-z0-9+\/=]*\)\\*" | base64 -d > [^ ]*\/cordis\.patch\.yml.*/\1/p' \
    | head -1)"
  if [ -z "$PATCH_B64" ]; then
    echo "error: could not find the seeded cordis.patch.yml in '$SOURCE_UNIT'" >&2
    echo "       (no init container carries a 'base64 -d > .../cordis.patch.yml' seed)." >&2
    echo "       Has the chart been upgraded with a non-empty dshCordisPatch? Fall back to --file." >&2
    exit 1
  fi
  if ! printf '%s' "$PATCH_B64" | base64 -d > "$TMP_PATCH" 2>/dev/null; then
    echo "error: the seeded payload in '$SOURCE_UNIT' is not valid base64" >&2
    exit 1
  fi
  if [ ! -s "$TMP_PATCH" ]; then
    echo "error: the seeded payload in '$SOURCE_UNIT' decoded to an empty file" >&2
    exit 1
  fi
  FILE="$TMP_PATCH"
  echo "source: --from-cluster (patch baked into '$SOURCE_UNIT' init container)"
fi

# ---------------------------------------------------------------------- push
# (the DRY RUN banner was printed once, before the canonical step)
if [ "$APPLY" -eq 0 ]; then
  if [ "$FROM_CONFIGMAP" -eq 1 ]; then
    echo "mode: push the latest patch from values (--from-configmap) -> $TARGET"
  elif [ "$FROM_CLUSTER" -eq 1 ]; then
    echo "mode: push the chart-rendered patch (--from-cluster) -> $TARGET"
  else
    echo "mode: push local file '$FILE' -> $TARGET"
  fi
  if [ "$TARGET_NAME" = "$LIVE_NAME" ]; then
    echo "target: $TARGET  ** OVERWRITES THE LIVE PATCH **"
    echo "backup: $PROFILE_DIR/$BACKUP_NAME (copied first, when a live patch exists)"
    echo "note:   the supervisor validates the new YAML, then restarts dsh"
  else
    echo "target: $TARGET  (staged beside the live patch; live file untouched)"
    echo "live:   $LIVE  (NOT modified — nothing reloads until you promote the staged file)"
  fi
  if [ "${#USERS[@]}" -eq 0 ]; then
    echo "scope: ALL user units in namespace '$NAMESPACE' (no --user given)"
  else
    # Echo the DISTINCT units that matched, so "--user me --user me" or a
    # username/deployment-name spelling still reads back as the real users.
    _scope=""
    for _i in "${!SEL_NAMES[@]}"; do
      _who="${SEL_USERS[$_i]:-${SEL_NAMES[$_i]}}"
      case " $_scope " in
        *" $_who "*) ;;
        *) _scope="${_scope:+$_scope }$_who" ;;
      esac
    done
    echo "scope: --user ${USERS[*]} -> $_scope (namespace '$NAMESPACE')"
  fi
  echo "namespace: $NAMESPACE | container: $CONTAINER | deployments: $COUNT"
  for _i in "${!SEL_NAMES[@]}"; do
    if [ -n "${SEL_USERS[$_i]}" ]; then
      echo " - ${SEL_NAMES[$_i]} (user: ${SEL_USERS[$_i]})"
    else
      echo " - ${SEL_NAMES[$_i]}"
    fi
  done
  echo "re-run with --apply to execute"
  exit 0
fi

if [ "$TARGET_NAME" = "$LIVE_NAME" ]; then
  echo "WARNING: --live rewrites the LIVE patch in $COUNT unit(s); each unit's current patch is copied to $BACKUP_NAME first" >&2
fi
echo "updating $COUNT deployment(s) in namespace '$NAMESPACE'"
FAILED_COUNT=0
FAILED_LIST=""
i=0
for _i in "${!SEL_NAMES[@]}"; do
  i=$((i + 1))
  _name="${SEL_NAMES[$_i]}"
  _who="${SEL_USERS[$_i]}"
  if [ -n "$_who" ]; then
    _label="$_name (user: $_who)"
  else
    _label="$_name"
  fi
  if "$KUBECTL" exec -i -n "$NAMESPACE" "deploy/$_name" -c "$CONTAINER" -- sh -c "$(push_cmd)" < "$FILE" >/dev/null 2>&1; then
    if [ "$TARGET_NAME" = "$LIVE_NAME" ]; then
      echo "[$i/$COUNT] $_label: live patch written (backup: $BACKUP_NAME)"
    else
      echo "[$i/$COUNT] $_label: staged ($TARGET_NAME)"
    fi
  else
    echo "[$i/$COUNT] $_label: FAILED" >&2
    FAILED_COUNT=$((FAILED_COUNT + 1))
    FAILED_LIST="$FAILED_LIST $_name"
  fi
done

if [ "$FAILED_COUNT" -gt 0 ]; then
  echo "done with $FAILED_COUNT failure(s):$FAILED_LIST" >&2
  exit 1
fi
if [ "$TARGET_NAME" = "$LIVE_NAME" ]; then
  echo "done: $COUNT deployment(s) updated — the LIVE patch changed (dsh hot-reloads it; users may need to refresh their browser)"
  echo "      previous content kept as $BACKUP_NAME in each unit's $PROFILE_DIR"
else
  echo "done: staged $TARGET_NAME in $COUNT deployment(s) — the live patch is UNCHANGED"
  echo "      promote when ready, e.g. per unit:"
  echo "        kubectl exec -n <ns> deploy/<unit> -c $CONTAINER -- sh -c 'cd $PROFILE_DIR && cp -p cordis.patch.yml cordis.patch-backup.yml && mv $TARGET_NAME cordis.patch.yml'"
  echo "      (the supervisor YAML-validates the file and restarts dsh on the change)"
fi
