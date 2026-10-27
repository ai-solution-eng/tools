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
# This script NEVER writes it — a push must leave the running patch untouched.
LIVE="$PROFILE_DIR/cordis.patch.yml"
# Where a push lands instead: a SIBLING file. This is safe because dsh
# resolves the patch by FIXED filename, not by glob
# (dsh-app-boot: `join(dir, PROFILE_PATCH_FILENAME)`), and the in-pod
# supervisor watches an explicit file list (DSH_WATCH_CONFIG_FILES), with
# DSH_WATCH_CONFIG_DIRS empty on the shipped units. So a sibling .yml is
# completely inert: it is neither loaded nor watched, and cannot trigger a
# restart. (The pre-existing cordis.patch-backup.yml already relies on this.)
# Override with TARGET_NAME=cordis.patch.yml to overwrite the live patch
# directly (the old behaviour).
TARGET_NAME="${TARGET_NAME:-cordis.patch-update.yml}"
TARGET="$PROFILE_DIR/$TARGET_NAME"
# No pre-push backup is needed: the live file is never overwritten, so there
# is nothing to preserve. The write is still atomic-ish (`cat > file`) and the
# in-pod supervisor re-validates YAML before any restart, so a broken staged
# patch cannot crash-loop anything.
PUSH_CMD="mkdir -p $PROFILE_DIR && cat > $TARGET"

usage() {
  cat <<EOF
usage: push-dsh-cordis-patch.sh (--file cordis.patch.yml | --from-configmap | --from-cluster)
                                [--user ID]... [--apply] [--namespace NS]
                                [--configmap-namespace NS]

STAGES the dsh web profile's patch layer (MCP integrations, session content
search, title-service caps) into every dsh-web user environment (all
deployments labeled dsh-user-managed=true, warm-pool units included).

IMPORTANT — the live patch is NOT overwritten. The push lands beside it as
a sibling file, so the running configuration is untouched:

  live (never written)  $LIVE
  staged by this script $TARGET

The sibling is inert: dsh loads the patch by fixed filename (not a glob) and
the in-pod supervisor watches an explicit file list, so nothing reloads and
no restart is triggered. Nothing changes for the user until you promote the
staged file yourself (e.g. move it over the live name and let the supervisor
pick it up), which makes this a safe staging step for testing a new patch.
Set TARGET_NAME=cordis.patch.yml to overwrite the live patch directly
instead (the old behaviour; a rolling backup was made in that mode).

  --file PATH          push this local YAML patch file into every pod
  --from-configmap     (recommended) push the LATEST patch as rendered from
                       values.yaml — read from the '<release>-project-config'
                       ConfigMap (key: cordis.patch.yml). This is the canonical
                       copy an admin can also read directly:
                         kubectl get cm <release>-project-config -n NS -o jsonpath=...
                       (the data key is cordis.patch.yml)
                       Requires an applied helm upgrade. Mutually exclusive with
                       --file / --from-cluster.
  --from-cluster       fallback for charts that predate that ConfigMap: reads
                       the dshCordisPatch value baked as base64 into the FIRST
                       selected unit's init-dsh-storage init container. Prefer
                       --from-configmap. Mutually exclusive with the others.
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
               TARGET_NAME (default: cordis.patch-update.yml),
               RELEASE_NAMESPACE (ConfigMap namespace fallback; default: auto)
EOF
}

APPLY=0
FILE=""
FROM_CLUSTER=0
FROM_CONFIGMAP=0
LIST_USERS=0
USERS=()

# Scratch file for --from-cluster (the extracted patch). Created under TMPDIR
# and removed on exit; never written into the repo.
TMP_PATCH=""
cleanup_tmp() {
  if [ -n "$TMP_PATCH" ] && [ -f "$TMP_PATCH" ]; then
    rm -f "$TMP_PATCH"
  fi
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
  # Exactly one source is required.
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
    # Pre-flight YAML check. Three outcomes are deliberately distinguished, so
    # a missing parser never masquerades as a broken file:
    #   0 = parses, 1 = genuinely invalid YAML, 3 = PyYAML not installed.
    # cordis patches use the !!js tag, which PyYAML cannot construct: a plain
    # safe_load rejects EVERY real patch (false alarm). Register the tag as an
    # opaque scalar — the same posture the in-pod supervisor takes (js-yaml
    # parses the tag without evaluating it at load time).
    YAML_RC=3
    if command -v python3 >/dev/null 2>&1; then
      if python3 - "$FILE" <<'PY' >/dev/null 2>&1
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
        YAML_RC=0
      else
        YAML_RC=$?
      fi
    fi
    case "$YAML_RC" in
      0) : ;;
      1) echo "warning: $FILE is not valid YAML; pushing anyway — the in-pod supervisor validates and would reject a broken patch" >&2 ;;
      *) echo "note: YAML pre-check skipped (python3 or PyYAML unavailable) — the in-pod supervisor still validates before restarting dsh" >&2 ;;
    esac
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
  CM_NAME="${CONFIGMAP_NAME:-dsh-web-helm-project-config}"

  # Release namespace guess: the namespace of the chart's own router Deployment,
  # found via the app label every chart object carries. Empty if not resolvable.
  derive_release_namespace() {
    # Every step is guarded: under 'set -euo pipefail' a failing or empty
    # cluster-wide query (RBAC/policy-blocked -A, no matches) would otherwise
    # abort the whole script with a silent exit 1. Failing to DERIVE a
    # namespace is not an error — the caller still has $NAMESPACE to try.
    "$KUBECTL" get deploy -A -l "hpe-ezua/app=dsh-web-helm" \
      -o jsonpath='{range .items[*]}{.metadata.namespace}{"\n"}{end}' 2>/dev/null \
      | head -1 || true
  }

  if [ -n "$RELEASE_NAMESPACE" ]; then
    CM_CANDIDATES="$RELEASE_NAMESPACE"
  else
    CM_CANDIDATES="$NAMESPACE"
    _derived="$(derive_release_namespace)"
    if [ -n "$_derived" ] && [ "$_derived" != "$NAMESPACE" ]; then
      CM_CANDIDATES="$CM_CANDIDATES $_derived"
    fi
  fi

  CM_FOUND_NS=""
  CM_PATCH=""
  CM_ERR=""
  for _cmns in $CM_CANDIDATES; do
    if CM_PATCH="$("$KUBECTL" get configmap -n "$_cmns" "$CM_NAME" \
          -o jsonpath='{.data.cordis\.patch\.yml}' 2>&1)"; then
      CM_FOUND_NS="$_cmns"
      break
    fi
    CM_ERR="$CM_PATCH"
    CM_PATCH=""
  done

  if [ -z "$CM_FOUND_NS" ]; then
    echo "error: ConfigMap '$CM_NAME' not found in: $CM_CANDIDATES" >&2
    echo "       last kubectl error: $CM_ERR" >&2
    echo "       (upgrade the chart so templates/configmap-project.yaml is applied," >&2
    echo "        pass --configmap-namespace <release-ns>, or use --file / --from-cluster.)" >&2
    exit 1
  fi
  if [ -z "$CM_PATCH" ]; then
    echo "error: ConfigMap '$CM_NAME' in '$_cmns' has no 'cordis.patch.yml' key (or it is empty)" >&2
    echo "       Is values.dshCordisPatch set? Use --file / --from-cluster instead." >&2
    exit 1
  fi
  printf '%s' "$CM_PATCH" > "$TMP_PATCH"
  if [ ! -s "$TMP_PATCH" ]; then
    echo "error: the ConfigMap payload decoded to an empty file" >&2
    exit 1
  fi
  FILE="$TMP_PATCH"
  echo "source: --from-configmap ($CM_NAME in '$_cmns' -> cordis.patch.yml)"
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
if [ "$APPLY" -eq 0 ]; then
  echo "DRY RUN - nothing executed"
  if [ "$FROM_CONFIGMAP" -eq 1 ]; then
    echo "mode: push the latest patch from values (--from-configmap) -> $TARGET"
  elif [ "$FROM_CLUSTER" -eq 1 ]; then
    echo "mode: push the chart-rendered patch (--from-cluster) -> $TARGET"
  else
    echo "mode: push local file '$FILE' -> $TARGET"
  fi
  if [ "$TARGET_NAME" = "cordis.patch.yml" ]; then
    echo "target: $TARGET  ** OVERWRITES THE LIVE PATCH **"
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
  if "$KUBECTL" exec -i -n "$NAMESPACE" "deploy/$_name" -c "$CONTAINER" -- sh -c "$PUSH_CMD" < "$FILE" >/dev/null 2>&1; then
    if [ "$TARGET_NAME" = "cordis.patch.yml" ]; then
      echo "[$i/$COUNT] $_label: live patch overwritten"
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
if [ "$TARGET_NAME" = "cordis.patch.yml" ]; then
  echo "done: $COUNT deployment(s) updated — the LIVE patch changed (dsh hot-reloads it; users may need to refresh their browser)"
else
  echo "done: staged $TARGET_NAME in $COUNT deployment(s) — the live patch is UNCHANGED"
  echo "      promote when ready, e.g. per unit:"
  echo "        kubectl exec -n <ns> deploy/<unit> -c $CONTAINER -- sh -c 'cd $PROFILE_DIR && cp -p cordis.patch.yml cordis.patch-backup.yml && mv $TARGET_NAME cordis.patch.yml'"
  echo "      (the supervisor YAML-validates the file and restarts dsh on the change)"
fi
