#!/usr/bin/env bash
# This script uses bash arrays and `set -o pipefail`; running it as
# `sh push_openwork_json_config.sh` on a dash-based /bin/sh fails, so re-exec
# under bash when needed.
if [ -z "${BASH_VERSION:-}" ]; then
  if command -v bash >/dev/null 2>&1; then
    exec bash "$0" "$@"
  fi
  echo "error: this script requires bash (arrays + pipefail); bash was not found" >&2
  exit 1
fi
set -euo pipefail

KUBECTL="${KUBECTL:-kubectl}"
NAMESPACE="${NAMESPACE:-openwork-web-helm}"
CONTAINER="${CONTAINER:-openwork-web}"
CONFIG_DIR="/var/openwork/home/.config/opencode"
# The LIVE config: the one opencode actually loads and the supervisor watches.
# This script NEVER writes it — a push must leave the running config untouched.
LIVE="$CONFIG_DIR/opencode.json"
# In-pod defaults mounted from the chart's openworkConfig ConfigMap (the
# no---file source, i.e. "helm upgrade, then push the mounted copy").
DEFAULTS="/opt/openwork-defaults/opencode.json"
# Where a push lands instead: a SIBLING file. This is safe because opencode
# resolves its config from a FIXED candidate list, not a glob — the binary
# does `["opencode.jsonc","opencode.json","config.json"].map(n => join(config,n))`
# and takes the first that exists — and the in-pod supervisor
# (opencode_supervisor.mjs) watches an explicit file list
# (OPENCODE_WATCH_CONFIG_FILES, default 'opencode.json,config.json,opencode.jsonc')
# plus the skills/agents DIRECTORIES only. So a sibling file is inert: it is
# neither loaded nor watched, and cannot trigger a restart.
# NOTE: the name must NOT be one of those three candidates, and must not end
# in .jsonc (which would win the resolution order over opencode.json).
# Override with TARGET_NAME=opencode.json to overwrite the live config.
TARGET_NAME="${TARGET_NAME:-opencode-update.json}"
TARGET="$CONFIG_DIR/$TARGET_NAME"
# No pre-push backup is needed: the live file is never overwritten, so there
# is nothing to preserve. In the TARGET_NAME=opencode.json (overwrite) mode a
# rolling backup is still taken, exactly as before.
BACKUP="$CONFIG_DIR/opencode-backup.json"
PULL_CMD="mkdir -p $CONFIG_DIR && if [ -f $LIVE ]; then cp -p $LIVE $BACKUP; fi && cp $DEFAULTS $LIVE"
PUSH_CMD="mkdir -p $CONFIG_DIR && cat > $TARGET"
OVERWRITE_CMD="mkdir -p $CONFIG_DIR && if [ -f $LIVE ]; then cp -p $LIVE $BACKUP; fi && cat > $LIVE"

usage() {
  cat <<EOF
usage: push_openwork_json_config.sh [--file config.json | --from-configmap | --from-defaults] [--apply] [--namespace NS]

STAGES opencode.json (models, providers, MCP servers, permissions) into every
openwork-web user environment (all deployments labeled
openwork-user-managed=true, warm-pool units included).

IMPORTANT — the live config is NOT overwritten. The push lands beside it as a
sibling file, so the running configuration is untouched:

  live (never written)  $LIVE
  staged by this script $TARGET

The sibling is inert: opencode resolves its config from a fixed candidate list
(opencode.jsonc / opencode.json / config.json), not a glob, and the in-pod
supervisor watches an explicit file list. Nothing reloads and no restart is
triggered, so users see no change until you promote the staged file yourself
(the script prints the exact per-unit command). This makes it a safe staging
step for trying a new config.
Set TARGET_NAME=opencode.json to overwrite the live config instead (the old
behaviour; a rolling backup to opencode-backup.json is taken in that mode).

  (default)            dry-run preview only
  --apply              actually run
  --file PATH          stage this local JSON file into every pod
  --from-configmap     (recommended, and the default) stage the LATEST config as
                       rendered from values.yaml — read from the
                       '<release>-project-config' ConfigMap (key: opencode.json).
                       Needs no running pod. This is the canonical copy an admin
                       can also read directly:
                         kubectl get cm <release>-project-config -n NS -o jsonpath=...
                       (the data key is opencode.json)
  --from-defaults      stage the copy mounted inside a user pod ($DEFAULTS)
                       instead — same bytes, but requires a running pod and a
                       helm upgrade first
  --list-users         list the user units and the --user IDs, then exit
  --user ID            stage ONLY to the unit(s) of this user — repeatable and
                       comma-separated (--user me --user you / --user me,you).
                       Matched case-insensitively against the unit's
                       openwork-user-slug / openwork-user-owner labels and the
                       deployment name. Omit to target EVERY user unit.
                       An ID matching no unit is an ERROR (exit 1), so a typo
                       can never silently fall back to pushing to all.
  --namespace NS       target namespace (default: $NAMESPACE)
  --help               this help

env overrides: KUBECTL (default: kubectl), NAMESPACE, CONTAINER (default: $CONTAINER),
               TARGET_NAME (default: opencode-update.json)
EOF
}

APPLY=0
FILE=""
FROM_DEFAULTS=0
FROM_CONFIGMAP=0
LIST_USERS=0

# Scratch file for --from-configmap (the extracted config). Removed on exit.
TMP_PATCH=""
cleanup_tmp() {
  if [ -n "$TMP_PATCH" ] && [ -f "$TMP_PATCH" ]; then
    rm -f "$TMP_PATCH"
  fi
}
trap cleanup_tmp EXIT
USERS=()

normalize_user() {
  printf '%s' "$1" \
    | LC_ALL=C tr '[:upper:]' '[:lower:]' \
    | LC_ALL=C sed -e 's/[^a-z0-9][^a-z0-9]*/-/g' -e 's/^-//' -e 's/-$//'
}

add_users() {
  local raw="$1" part
  local IFS=','
  set -f
  for part in $raw; do
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
    --from-defaults)
      FROM_DEFAULTS=1
      shift
      ;;
    --from-configmap)
      FROM_CONFIGMAP=1
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
    --list-users)
      LIST_USERS=1
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

if [ "$LIST_USERS" -eq 0 ]; then
  _sources=0
  [ -n "$FILE" ] && _sources=$((_sources + 1))
  [ "$FROM_DEFAULTS" -eq 1 ] && _sources=$((_sources + 1))
  [ "$FROM_CONFIGMAP" -eq 1 ] && _sources=$((_sources + 1))
  if [ "$_sources" -gt 1 ]; then
    echo "error: --file, --from-defaults and --from-configmap are mutually exclusive" >&2
    exit 1
  fi
  # --from-configmap is the default when no --file/--from-defaults is given:
  # read the latest config straight from the release ConfigMap (no pod needed).
  if [ "$_sources" -eq 0 ]; then
    FROM_CONFIGMAP=1
  fi

  if [ -n "$FILE" ]; then
    if [ ! -f "$FILE" ]; then
      echo "error: file not found: $FILE" >&2
      exit 1
    fi
    if [ ! -s "$FILE" ]; then
      echo "error: file is empty: $FILE" >&2
      exit 1
    fi
    # Tri-state JSON check: 0 = parses, 1 = invalid, 3 = no python3. A missing
    # interpreter must not masquerade as a broken file.
    JSON_RC=3
    if command -v python3 >/dev/null 2>&1; then
      if python3 -c "import json,sys; json.load(open(sys.argv[1]))" "$FILE" >/dev/null 2>&1; then
        JSON_RC=0
      else
        JSON_RC=$?
      fi
    fi
    case "$JSON_RC" in
      0) : ;;
      1) echo "warning: $FILE is not valid JSON; pushing anyway — opencode validates it before restarting" >&2 ;;
      *) echo "note: JSON pre-check skipped (python3 unavailable) — opencode still validates before restarting" >&2 ;;
    esac
  fi
fi

# ----------------------------------------------- source: --from-configmap
# The canonical copy of the config as rendered from values.yaml lives in the
# release's '<release>-project-config' ConfigMap (templates/configmap-project.yaml).
# Reading it needs no running pod, so this is the natural source for a push.
if [ "$FROM_CONFIGMAP" -eq 1 ]; then
  TMP_PATCH="${TMPDIR:-/tmp}/opencode-config.$$.json"
  CM_NAME="${CONFIGMAP_NAME:-openwork-web-helm-project-config}"
  if ! CM_CFG="$("$KUBECTL" get configmap -n "$NAMESPACE" "$CM_NAME" \
        -o jsonpath='{.data.opencode\.json}' 2>&1)"; then
    echo "error: kubectl failed reading ConfigMap '$CM_NAME' in '$NAMESPACE': $CM_CFG" >&2
    echo "       (upgrade the chart so templates/configmap-project.yaml is applied," >&2
    echo "        or use --file / --from-defaults.)" >&2
    exit 1
  fi
  if [ -z "$CM_CFG" ]; then
    echo "error: ConfigMap '$CM_NAME' has no 'opencode.json' key (or it is empty)" >&2
    echo "       Is values.opencodeConfig set? Use --file / --from-defaults instead." >&2
    exit 1
  fi
  printf '%s' "$CM_CFG" > "$TMP_PATCH"
  if [ ! -s "$TMP_PATCH" ]; then
    echo "error: the ConfigMap payload was empty" >&2
    exit 1
  fi
  FILE="$TMP_PATCH"
  echo "source: --from-configmap ($CM_NAME -> opencode.json)"
fi

# ------------------------------------------------------------------ inventory
# One line per openwork-user-managed deployment: name|slug|owner|dind-broker.
if ! INVENTORY="$("$KUBECTL" get deploy -n "$NAMESPACE" -l openwork-user-managed=true \
      -o jsonpath='{range .items[*]}{.metadata.name}{"|"}{.metadata.labels.openwork-user-slug}{"|"}{.metadata.labels.openwork-user-owner}{"|"}{.metadata.labels.openwork-dind-broker}{"\n"}{end}' 2>&1)"; then
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
  echo "no user deployments found in namespace '$NAMESPACE' (label selector: openwork-user-managed=true)"
  exit 0
fi

if [ "$LIST_USERS" -eq 1 ]; then
  echo "user units in namespace '$NAMESPACE' (label selector: openwork-user-managed=true)"
  printf '  %-38s %-26s %s\n' "DEPLOYMENT" "--user ID" "NOTE"
  for _i in "${!ALL_NAMES[@]}"; do
    _id="${ALL_SLUGS[$_i]:-${ALL_OWNERS[$_i]:-}}"
    if [ "${ALL_BROKERS[$_i]}" = "true" ]; then
      printf '  %-38s %-26s %s\n' "${ALL_NAMES[$_i]}" "-" "docker-in-docker broker, not an opencode unit"
    elif [ -z "$_id" ]; then
      printf '  %-38s %-26s %s\n' "${ALL_NAMES[$_i]}" "-" "no openwork-user-slug / -owner label"
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
    echo "note: skipping ${#SKIPPED_BROKERS[@]} docker-in-docker broker deployment(s) (no openwork-web container): ${SKIPPED_BROKERS[*]}" >&2
  fi
  if [ "${#SEL_NAMES[@]}" -eq 0 ]; then
    echo "no opencode user units found in namespace '$NAMESPACE' (only docker-in-docker brokers are managed)"
    exit 0
  fi
else
  MISSING=()
  for _u in "${USERS[@]}"; do
    _lower="$(printf '%s' "$_u" | LC_ALL=C tr '[:upper:]' '[:lower:]')"
    _norm="$(normalize_user "$_u")"
    _found=0
    for _i in "${!ALL_NAMES[@]}"; do
      if [ "${ALL_BROKERS[$_i]}" = "true" ]; then
        continue
      fi
      _n="$(printf '%s' "${ALL_NAMES[$_i]}" | LC_ALL=C tr '[:upper:]' '[:lower:]')"
      _s="$(printf '%s' "${ALL_SLUGS[$_i]}" | LC_ALL=C tr '[:upper:]' '[:lower:]')"
      _o="$(printf '%s' "${ALL_OWNERS[$_i]}" | LC_ALL=C tr '[:upper:]' '[:lower:]')"
      _np="${_n#openwork-user-}"
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
        for _seen in ${SEL_NAMES[@]+"${SEL_NAMES[@]}"}; do
          if [ "$_seen" = "${ALL_NAMES[$_i]}" ]; then
            _already=1
          fi
        done
        if [ "$_already" -eq 0 ]; then
          SEL_NAMES+=("${ALL_NAMES[$_i]}")
          SEL_USERS+=("${ALL_SLUGS[$_i]:-${ALL_OWNERS[$_i]:-}}")
        fi
      fi
    done
    if [ "$_found" -eq 0 ]; then
      MISSING+=("$_u")
    fi
  done

  if [ "${#MISSING[@]}" -gt 0 ]; then
    echo "error: no opencode user unit in namespace '$NAMESPACE' matches: ${MISSING[*]}" >&2
    echo "       (label selector: openwork-user-managed=true) — nothing was pushed" >&2
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
        if [ "${ALL_BROKERS[$_i]}" = "true" ] && [ "${ALL_NAMES[$_i]}" = "openwork-dind-$_norm" ]; then
          echo "       note: ${ALL_NAMES[$_i]} is a Docker-in-Docker broker, not an opencode unit" >&2
        fi
      done
      # Platform (Option B) users get their unit in a per-user namespace.
      if [ -n "$_norm" ] && "$KUBECTL" get namespace "project-user-$_norm" >/dev/null 2>&1; then
        echo "       note: namespace 'project-user-$_norm' exists — this user's unit may live there:" >&2
        echo "         $0 --namespace project-user-$_norm --user '$_u' ..." >&2
      fi
    done
    exit 1
  fi
fi

COUNT="${#SEL_NAMES[@]}"
OVERWRITE=0
if [ "$TARGET_NAME" = "opencode.json" ]; then
  OVERWRITE=1
fi

# ---------------------------------------------------------------------- push
if [ "$APPLY" -eq 0 ]; then
  echo "DRY RUN - nothing executed"
  if [ "$FROM_CONFIGMAP" -eq 1 ]; then
    echo "mode: stage the latest config from values (--from-configmap) -> opencode-update.json"
  elif [ -n "$FILE" ]; then
    echo "mode: stage local file '$FILE'"
  else
    echo "mode: stage the chart-mounted defaults ($DEFAULTS)"
    echo "note: run a helm upgrade of openworkConfig first so the mounted ConfigMap is current"
  fi
  if [ "$OVERWRITE" -eq 1 ]; then
    echo "target: $LIVE  ** OVERWRITES THE LIVE CONFIG **"
    echo "backup: current file -> $BACKUP"
  else
    echo "target: $TARGET  (staged beside the live config; live file untouched)"
    echo "live:   $LIVE  (NOT modified — nothing reloads until you promote the staged file)"
  fi
  if [ "${#USERS[@]}" -eq 0 ]; then
    echo "scope: ALL user units in namespace '$NAMESPACE' (no --user given)"
  else
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
  if [ "$OVERWRITE" -eq 1 ]; then
    _cmd="$OVERWRITE_CMD"
  elif [ -n "$FILE" ]; then
    _cmd="$PUSH_CMD"
  else
    # --from-defaults stages the mounted copy: read it in-pod and write the
    # staged sibling (no stdin needed).
    _cmd="mkdir -p $CONFIG_DIR && cp $DEFAULTS $TARGET"
  fi
  if [ -n "$FILE" ]; then
    if "$KUBECTL" exec -i -n "$NAMESPACE" "deploy/$_name" -c "$CONTAINER" -- sh -c "$_cmd" < "$FILE" >/dev/null 2>&1; then
      _ok=1
    else
      _ok=0
    fi
  else
    if "$KUBECTL" exec -n "$NAMESPACE" "deploy/$_name" -c "$CONTAINER" -- sh -c "$_cmd" >/dev/null 2>&1; then
      _ok=1
    else
      _ok=0
    fi
  fi
  if [ "$_ok" -eq 1 ]; then
    if [ "$OVERWRITE" -eq 1 ]; then
      echo "[$i/$COUNT] $_label: live config overwritten"
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
if [ "$OVERWRITE" -eq 1 ]; then
  echo "done: $COUNT deployment(s) updated — the LIVE config changed (opencode hot-reloads it; users may need to refresh their browser)"
else
  echo "done: staged $TARGET_NAME in $COUNT deployment(s) — the live config is UNCHANGED"
  echo "      promote when ready, e.g. per unit:"
  echo "        kubectl exec -n <ns> deploy/<unit> -c $CONTAINER -- sh -c 'cd $CONFIG_DIR && cp -p opencode.json opencode-backup.json && mv $TARGET_NAME opencode.json'"
  echo "      (the supervisor validates the JSON and restarts opencode on the change)"
fi
