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
# The chart's canonical config ConfigMap (templates/configmap-project.yaml).
# It ALSO holds openwork-server.json and the skill-*/agent-* keys, so any write
# must be a PER-KEY merge patch — never apply/replace, which would drop them.
RELEASE_NAME="${RELEASE_NAME:-openwork-web-helm}"
CM_NAME="${CONFIGMAP_NAME:-$RELEASE_NAME-project-config}"
CM_KEY="opencode.json"
CM_JSONPATH='{.data.opencode\.json}'
# RELEASE_NAMESPACE may be set explicitly; otherwise it is derived after arg
# parsing (the ConfigMap is NOT in a per-user 'project-user-*' namespace).
RELEASE_NAMESPACE_EXPLICIT="${RELEASE_NAMESPACE:-}"
CONFIG_DIR="/var/openwork/home/.config/opencode"
# The LIVE config: the one opencode actually loads and the supervisor watches.
# A plain push NEVER writes it — the payload lands in the staged sibling instead.
# Only --live (or the deprecated TARGET_NAME=opencode.json) replaces it.
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
# TARGET_NAME=opencode.json still selects the live file (backwards compatible)
# but it is DEPRECATED — use --live.
TARGET_NAME="${TARGET_NAME:-opencode-update.json}"
TARGET="$CONFIG_DIR/$TARGET_NAME"
# No pre-push backup is needed for a staged push: the live file is not
# overwritten, so there is nothing to preserve. --live (and the deprecated
# TARGET_NAME=opencode.json mode) takes a rolling backup first.
BACKUP="$CONFIG_DIR/opencode-backup.json"
PULL_CMD="mkdir -p $CONFIG_DIR && if [ -f $LIVE ]; then cp -p $LIVE $BACKUP; fi && cp $DEFAULTS $LIVE"
PUSH_CMD="mkdir -p $CONFIG_DIR && cat > $TARGET"
# --live: rolling backup, then write a temp file and mv it into place — NEVER
# truncate the live file in place (a half-written opencode.json is a broken
# config that the supervisor will reject).
LIVE_CMD="mkdir -p $CONFIG_DIR && if [ -f $LIVE ]; then cp -p $LIVE $BACKUP; fi && cat > $LIVE.tmp && mv -f $LIVE.tmp $LIVE"
LIVE_FROM_DEFAULTS_CMD="mkdir -p $CONFIG_DIR && if [ -f $LIVE ]; then cp -p $LIVE $BACKUP; fi && cp $DEFAULTS $LIVE.tmp && mv -f $LIVE.tmp $LIVE"

usage() {
  cat <<EOF
usage: push_openwork_json_config.sh [--file config.json | --from-configmap | --from-defaults]
                                  [--to-router [--router-only]] [--live] [--apply] [--namespace NS]

STAGES opencode.json (models, providers, MCP servers, permissions) into every
openwork-web user environment (all deployments labeled
openwork-user-managed=true, warm-pool units included).

Two phases — the canonical copy first, then the fan-out:
  phase 1 (--to-router)  publish --file into the chart's canonical ConfigMap
                         '<release>-project-config' (key: opencode.json) in the
                         RELEASE namespace. That is the copy values.yaml renders
                         and a fresh user seeds from. Per-key merge patch: the
                         sibling keys (openwork-server.json, skill-*.md,
                         agent-*.md) are preserved.
  phase 2 (default)      stage the payload into every user pod.

IMPORTANT — a plain push does NOT overwrite the live config. The payload lands
beside it as a sibling file, so the running configuration is untouched:

  live (never written unless --live)  $LIVE
  staged by this script               $TARGET

The sibling is inert: opencode resolves its config from a fixed candidate list
(opencode.jsonc / opencode.json / config.json), not a glob, and the in-pod
supervisor watches an explicit file list. Nothing reloads and no restart is
triggered, so users see no change until you promote the staged file yourself
(the script prints the exact per-unit command). This makes it a safe staging
step for trying a new config. Use --live only when you mean to change the
running config of every unit right now.

  (default)            dry-run preview only
  --apply              actually run
  --file PATH          the JSON payload: stage it into every pod and, with
                       --to-router, publish it into the canonical ConfigMap
  --from-configmap     (the default when no --file/--from-defaults is given)
                       stage the LATEST config as rendered from values.yaml —
                       read from the '<release>-project-config' ConfigMap
                       (key: opencode.json). Needs no running pod. This is the
                       canonical copy an admin can also read directly:
                         kubectl get cm <release>-project-config -n NS -o jsonpath=...
  --from-defaults      stage the copy mounted inside a user pod ($DEFAULTS)
                       instead — same bytes, but requires a running pod and a
                       helm upgrade first
  --to-router          ALSO publish --file into the canonical ConfigMap
                       '<release>-project-config' (namespace: the RELEASE
                       namespace, not a project-user-* one) as a per-key merge
                       patch of data['opencode.json']. Requires --file; mutually
                       exclusive with --from-configmap / --from-defaults.
                       Without --apply it only describes the write.
  --router-only        with --to-router: stop after the canonical write — no pod
                       is contacted at all (no exec, no inventory).
  --live               write the LIVE config ($LIVE) in each pod instead of the
                       staged sibling. A rolling backup ($BACKUP) is taken first
                       and the file is replaced atomically (temp file + mv), but
                       opencode hot-reloads it, so users are affected at once.
                       Requires --apply to actually run. Prefer the staged
                       default and promote per unit.
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
               TARGET_NAME (default: opencode-update.json; TARGET_NAME=opencode.json
               is DEPRECATED — use --live), RELEASE_NAME (default: $RELEASE_NAME),
               CONFIGMAP_NAME (default: $CM_NAME),
               RELEASE_NAMESPACE (default: NAMESPACE, or $RELEASE_NAME when
               NAMESPACE is a project-user-* namespace)
EOF
}

APPLY=0
FILE=""
FROM_DEFAULTS=0
FROM_CONFIGMAP=0
LIST_USERS=0
TO_ROUTER=0
ROUTER_ONLY=0
LIVE_FLAG=0

# Scratch dir: the --from-configmap extract, the merge patch, and the current /
# read-back copies of the canonical ConfigMap key. Removed on exit.
TMPD="$(mktemp -d "${TMPDIR:-/tmp}/openwork-push.XXXXXX")"
TMP_PATCH=""
TMP_MERGE="$TMPD/merge-patch.json"
TMP_CUR="$TMPD/current.json"
TMP_VERIFY="$TMPD/readback.json"
TMP_ERR="$TMPD/kubectl.err"
cleanup_tmp() {
  if [ -n "${TMPD:-}" ] && [ -d "$TMPD" ]; then
    rm -rf "$TMPD"
  fi
  return 0
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
    --to-router)
      TO_ROUTER=1
      shift
      ;;
    --router-only)
      ROUTER_ONLY=1
      shift
      ;;
    --live)
      LIVE_FLAG=1
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

if [ "$ROUTER_ONLY" -eq 1 ] && [ "$TO_ROUTER" -eq 0 ]; then
  echo "error: --router-only requires --to-router (it means 'stop after the canonical write')" >&2
  exit 1
fi

# The canonical ConfigMap lives in the RELEASE namespace, which is not the same
# as the per-user 'project-user-*' namespace a fan-out may target.
if [ -n "$RELEASE_NAMESPACE_EXPLICIT" ]; then
  RELEASE_NAMESPACE="$RELEASE_NAMESPACE_EXPLICIT"
else
  case "$NAMESPACE" in
    project-user-*) RELEASE_NAMESPACE="$RELEASE_NAME" ;;
    *) RELEASE_NAMESPACE="$NAMESPACE" ;;
  esac
fi

# TARGET_NAME=opencode.json predates --live: keep it working, warn once.
LIVE_MODE=0
if [ "$LIVE_FLAG" -eq 1 ]; then
  LIVE_MODE=1
elif [ "$TARGET_NAME" = "opencode.json" ]; then
  LIVE_MODE=1
  echo "warning: TARGET_NAME=opencode.json is DEPRECATED — use --live (both write the LIVE config)" >&2
fi
if [ "$LIVE_MODE" -eq 1 ]; then
  if [ "$LIVE_FLAG" -eq 1 ] && [ "$TARGET_NAME" != "opencode.json" ] && [ "$TARGET_NAME" != "opencode-update.json" ]; then
    echo "note: --live writes $LIVE; TARGET_NAME=$TARGET_NAME is ignored" >&2
  fi
  TARGET="$LIVE"
fi

if [ "$LIST_USERS" -eq 0 ]; then
  _sources=0
  [ -n "$FILE" ] && _sources=$((_sources + 1))
  [ "$FROM_DEFAULTS" -eq 1 ] && _sources=$((_sources + 1))
  [ "$FROM_CONFIGMAP" -eq 1 ] && _sources=$((_sources + 1))
  if [ "$_sources" -gt 1 ]; then
    echo "error: --file, --from-defaults and --from-configmap are mutually exclusive" >&2
    exit 1
  fi
  if [ "$TO_ROUTER" -eq 1 ]; then
    if [ -z "$FILE" ]; then
      echo "error: --to-router requires --file PATH (the payload to publish into the canonical ConfigMap)" >&2
      echo "       --to-router publishes a LOCAL file; it cannot read the ConfigMap it writes" >&2
      exit 1
    fi
    if [ "$FROM_CONFIGMAP" -eq 1 ] || [ "$FROM_DEFAULTS" -eq 1 ]; then
      echo "error: --to-router is mutually exclusive with --from-configmap / --from-defaults" >&2
      exit 1
    fi
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
      1)
        echo "warning: $FILE is not valid JSON; pushing anyway — opencode validates it before restarting" >&2
        if [ "$TO_ROUTER" -eq 1 ]; then
          echo "error: refusing to publish invalid JSON into the canonical ConfigMap '$CM_NAME'" >&2
          echo "       fix $FILE first — every future push and every new user seeds from that key" >&2
          exit 1
        fi
        ;;
      *) echo "note: JSON pre-check skipped (python3 unavailable) — opencode still validates before restarting" >&2 ;;
    esac
  fi
fi

# ----------------------------------------------- source: --from-configmap
# The canonical copy of the config as rendered from values.yaml lives in the
# release's '<release>-project-config' ConfigMap (templates/configmap-project.yaml).
# Reading it needs no running pod, so this is the natural source for a push.
if [ "$FROM_CONFIGMAP" -eq 1 ]; then
  TMP_PATCH="$TMPD/config.json"
  if ! CM_CFG="$("$KUBECTL" get configmap -n "$RELEASE_NAMESPACE" "$CM_NAME" \
        -o jsonpath="$CM_JSONPATH" 2>&1)"; then
    echo "error: kubectl failed reading ConfigMap '$CM_NAME' in '$RELEASE_NAMESPACE': $CM_CFG" >&2
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

# ------------------------------------------------- canonical write: --to-router
# Publish the payload into the chart's canonical ConfigMap with a PER-KEY merge
# patch. That ConfigMap also holds openwork-server.json and the skill-*/agent-*
# keys, so it must NEVER be applied/replaced wholesale — a merge patch of the
# single 'opencode.json' key leaves the siblings untouched.
sha256_file() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$1" | awk '{print $1}'
  elif command -v python3 >/dev/null 2>&1; then
    python3 -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' "$1"
  else
    printf 'unavailable'
  fi
}

# {"data": {"opencode.json": "<file bytes as a JSON string>"}} — ONE key only.
build_merge_patch() {
  if command -v python3 >/dev/null 2>&1; then
    python3 - "$1" "$2" "$CM_KEY" <<'PY'
import json, sys
src, out, key = sys.argv[1], sys.argv[2], sys.argv[3]
with open(src, "rb") as fh:
    text = fh.read().decode("utf-8")
with open(out, "w", encoding="utf-8") as fh:
    json.dump({"data": {key: text}}, fh)
PY
  elif command -v jq >/dev/null 2>&1; then
    jq -Rs --arg k "$CM_KEY" '{data: {($k): .}}' "$1" > "$2"
  else
    echo "error: --to-router needs python3 or jq to build the merge patch" >&2
    return 1
  fi
}

# Print what the canonical write is, and (with --apply) do it + verify it.
canonical_write() {
  local _sha _out _rc
  _sha="$(sha256_file "$FILE")"
  echo "phase 1/2 - canonical ConfigMap (--to-router)"
  echo "  configmap: $CM_NAME"
  echo "  namespace: $RELEASE_NAMESPACE"
  echo "  data key:  $CM_KEY  (per-key merge patch; sibling keys preserved)"
  echo "  source:    $FILE"
  echo "  sha256:    $_sha"
  if [ "$APPLY" -eq 0 ]; then
    echo "  action:    would merge-patch $CM_KEY (dry run - nothing executed)"
    return 0
  fi
  if ! "$KUBECTL" get configmap -n "$RELEASE_NAMESPACE" "$CM_NAME" \
        -o jsonpath="$CM_JSONPATH" > "$TMP_CUR" 2> "$TMP_ERR"; then
    echo "error: kubectl failed reading ConfigMap '$CM_NAME' in '$RELEASE_NAMESPACE': $(cat "$TMP_ERR")" >&2
    echo "       (upgrade the chart so templates/configmap-project.yaml is applied.)" >&2
    return 1
  fi
  if cmp -s "$TMP_CUR" "$FILE"; then
    echo "  action:    already up to date — $CM_KEY is byte-identical to $FILE; nothing patched"
    return 0
  fi
  if ! build_merge_patch "$FILE" "$TMP_MERGE"; then
    return 1
  fi
  _rc=0
  _out="$("$KUBECTL" patch cm "$CM_NAME" -n "$RELEASE_NAMESPACE" --type merge \
          --patch-file "$TMP_MERGE" 2>&1)" || _rc=$?
  if [ "$_rc" -ne 0 ]; then
    case "$_out" in
      *"unknown flag"*|*"unknown shorthand flag"*|*"flag provided but not defined"*)
        echo "  note:      kubectl has no --patch-file; falling back to -p" >&2
        _rc=0
        _out="$("$KUBECTL" patch cm "$CM_NAME" -n "$RELEASE_NAMESPACE" --type merge \
                -p "$(cat "$TMP_MERGE")" 2>&1)" || _rc=$?
        ;;
    esac
  fi
  if [ "$_rc" -ne 0 ]; then
    echo "error: kubectl patch failed on ConfigMap '$CM_NAME' key $CM_KEY: $_out" >&2
    return 1
  fi
  # Verify by reading the key back into a FILE — $(...) would strip trailing
  # newlines and turn a real mismatch into a false pass.
  if ! "$KUBECTL" get configmap -n "$RELEASE_NAMESPACE" "$CM_NAME" \
        -o jsonpath="$CM_JSONPATH" > "$TMP_VERIFY" 2> "$TMP_ERR"; then
    echo "error: kubectl failed reading back ConfigMap '$CM_NAME': $(cat "$TMP_ERR")" >&2
    return 1
  fi
  if ! cmp -s "$TMP_VERIFY" "$FILE"; then
    echo "error: READBACK MISMATCH — '$CM_NAME' key $CM_KEY in '$RELEASE_NAMESPACE' does not hold what was sent" >&2
    echo "       sent:     $FILE (sha256 $(sha256_file "$FILE"))" >&2
    echo "       readback: $TMP_VERIFY (sha256 $(sha256_file "$TMP_VERIFY"))" >&2
    echo "       refusing to continue (a fan-out from an unverified canonical copy is unsafe)" >&2
    return 1
  fi
  echo "  action:    merge-patched; readback verified byte-identical (sha256 $(sha256_file "$TMP_VERIFY"))"
  return 0
}

# Loud, unit-by-unit banner for --live (it changes the running config).
live_warning() {
  local _i
  echo "!!! WARNING: --live TARGETS THE LIVE CONFIG ($LIVE) IN EVERY UNIT BELOW !!!" >&2
  echo "!!!   opencode hot-reloads it — users are affected immediately" >&2
  echo "!!!   rolling backup first: $LIVE -> $BACKUP (previous backup overwritten)" >&2
  echo "!!!   live file replaced atomically (temp file + mv), never truncated in place" >&2
  echo "!!!   units ($COUNT):" >&2
  for _i in "${!SEL_NAMES[@]}"; do
    if [ -n "${SEL_USERS[$_i]:-}" ]; then
      echo "!!!     - ${SEL_NAMES[$_i]} (user: ${SEL_USERS[$_i]})" >&2
    else
      echo "!!!     - ${SEL_NAMES[$_i]}" >&2
    fi
  done
}

# --router-only: the canonical write IS the whole job — never touch a pod.
if [ "$TO_ROUTER" -eq 1 ] && [ "$ROUTER_ONLY" -eq 1 ]; then
  if [ "$LIST_USERS" -eq 1 ]; then
    echo "error: --list-users cannot be combined with --router-only" >&2
    exit 1
  fi
  if [ "$APPLY" -eq 0 ]; then
    echo "DRY RUN - nothing executed"
    canonical_write
    echo "phase 2/2 - fan-out: SKIPPED (--router-only) — no pod is contacted"
    echo "re-run with --apply to execute"
    exit 0
  fi
  canonical_write || exit 1
  echo "done: canonical ConfigMap '$CM_NAME' key $CM_KEY updated in '$RELEASE_NAMESPACE'; no pod contacted (--router-only)"
  exit 0
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
  # With --to-router the canonical write is still meaningful on its own.
  if [ "$TO_ROUTER" -eq 1 ]; then
    if [ "$APPLY" -eq 0 ]; then
      echo "DRY RUN - nothing executed"
      canonical_write
      echo "phase 2/2 - fan-out: no user deployments in namespace '$NAMESPACE' (label selector: openwork-user-managed=true)"
      echo "re-run with --apply to execute"
      exit 0
    fi
    canonical_write || exit 1
    echo "no user deployments found in namespace '$NAMESPACE' (label selector: openwork-user-managed=true) — canonical write only"
    exit 0
  fi
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
if [ "$LIVE_MODE" -eq 1 ]; then
  OVERWRITE=1
fi

# ---------------------------------------------------------------------- push
if [ "$APPLY" -eq 0 ]; then
  echo "DRY RUN - nothing executed"
  if [ "$TO_ROUTER" -eq 1 ]; then
    canonical_write
    echo "phase 2/2 - fan-out to user units"
  fi
  if [ "$FROM_CONFIGMAP" -eq 1 ]; then
    if [ "$LIVE_MODE" -eq 1 ]; then
      echo "mode: the latest config from values (--from-configmap) -> LIVE $LIVE"
    else
      echo "mode: stage the latest config from values (--from-configmap) -> opencode-update.json"
    fi
  elif [ -n "$FILE" ]; then
    if [ "$LIVE_MODE" -eq 1 ]; then
      echo "mode: write local file '$FILE' to the LIVE config"
    else
      echo "mode: stage local file '$FILE'"
    fi
  else
    echo "mode: stage the chart-mounted defaults ($DEFAULTS)"
    echo "note: run a helm upgrade of openworkConfig first so the mounted ConfigMap is current"
  fi
  if [ "$OVERWRITE" -eq 1 ]; then
    echo "target: $LIVE  ** OVERWRITES THE LIVE CONFIG **"
    echo "backup: current file -> $BACKUP, then temp file + mv (never truncated in place)"
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
  if [ "$OVERWRITE" -eq 1 ]; then
    live_warning
  fi
  echo "re-run with --apply to execute"
  exit 0
fi

if [ "$TO_ROUTER" -eq 1 ]; then
  canonical_write || exit 1
  echo "phase 2/2 - fan-out to user units"
fi
if [ "$OVERWRITE" -eq 1 ]; then
  live_warning
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
  if [ -n "$FILE" ] && [ "$OVERWRITE" -eq 1 ]; then
    _cmd="$LIVE_CMD"
  elif [ -n "$FILE" ]; then
    _cmd="$PUSH_CMD"
  elif [ "$OVERWRITE" -eq 1 ]; then
    _cmd="$LIVE_FROM_DEFAULTS_CMD"
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
      echo "[$i/$COUNT] $_label: LIVE config replaced (backup: opencode-backup.json)"
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
  echo "      previous config per unit: $BACKUP"
else
  echo "done: staged $TARGET_NAME in $COUNT deployment(s) — the live config is UNCHANGED"
  echo "      promote when ready, e.g. per unit:"
  echo "        kubectl exec -n <ns> deploy/<unit> -c $CONTAINER -- sh -c 'cd $CONFIG_DIR && cp -p opencode.json opencode-backup.json && mv $TARGET_NAME opencode.json'"
  echo "      (the supervisor validates the JSON and restarts opencode on the change)"
  echo "      or do it for every unit at once: $0 --file <config.json> --live --apply"
fi
