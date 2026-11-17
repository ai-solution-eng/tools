#!/usr/bin/env bash
# This script uses bash arrays and `set -o pipefail`; running it as
# `sh push_opencode_json_config.sh` on a dash-based /bin/sh fails, so re-exec
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
NAMESPACE="${NAMESPACE:-opencode-web-helm}"
CONTAINER="${CONTAINER:-opencode-web}"
# Namespace that holds the release's '<release>-project-config' ConfigMap. This
# is NOT necessarily the target namespace: platform (SSO) users get their unit
# in 'project-user-<slug>', while the ConfigMap is rendered into
# .Release.Namespace. Left empty it auto-detects: the target namespace if the
# ConfigMap exists there, else the chart's release namespace.
RELEASE_NAMESPACE="${RELEASE_NAMESPACE:-}"
CONFIG_DIR="/var/opencode/home/.config/opencode"
# The LIVE config: the one opencode actually loads and the supervisor watches.
# A plain push NEVER writes it (that is the whole point of staging a sibling);
# only --live (or the deprecated TARGET_NAME=opencode.json) replaces it.
LIVE="$CONFIG_DIR/opencode.json"
# In-pod defaults mounted from the chart's opencodeConfig ConfigMap (the
# no---file source, i.e. "helm upgrade, then push the mounted copy").
DEFAULTS="/opt/opencode-defaults/opencode.json"
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
# --live selects the live file instead of this staged name; the old
# TARGET_NAME=opencode.json spelling still works but is deprecated.
TARGET_NAME="${TARGET_NAME:-opencode-update.json}"
TARGET="$CONFIG_DIR/$TARGET_NAME"
# No pre-push backup is needed while staging: the live file is never
# overwritten, so there is nothing to preserve. --live (and the deprecated
# TARGET_NAME=opencode.json) DO overwrite it, so those take a rolling backup
# first — the backup is always written before the live file is touched.
BACKUP="$CONFIG_DIR/opencode-backup.json"
PUSH_CMD="mkdir -p $CONFIG_DIR && cat > $TARGET"
# --live: back up, then write via a temp file + mv so the live file is never
# truncated in place (an interrupted push must not leave a half-written config
# for opencode/the supervisor to read). '$$' is left for the in-pod shell.
LIVE_TMP='$CONFIG_DIR/.opencode.json.tmp.$$'
LIVE_BACKUP_CMD="mkdir -p $CONFIG_DIR && if [ -f $LIVE ]; then cp -p $LIVE $BACKUP; fi"
LIVE_WRITE_CMD="$LIVE_BACKUP_CMD && cat > $LIVE_TMP && mv -f $LIVE_TMP $LIVE"
LIVE_COPY_CMD="$LIVE_BACKUP_CMD && cp $DEFAULTS $LIVE_TMP && mv -f $LIVE_TMP $LIVE"
# Canonical ConfigMap: the copy rendered from values.yaml into the RELEASE
# namespace (templates/configmap-project.yaml). --to-router publishes a local
# file there; --from-configmap reads it back. It also holds sibling keys
# (apply-defaults.mjs, skill-*.md, agent-*.md), so it is patched per key and
# NEVER applied/replaced wholesale.
CM_KEY="${CONFIG_KEY:-opencode.json}"
CM_NAME="${CONFIGMAP_NAME:-opencode-web-helm-project-config}"

usage() {
  cat <<EOF
usage: push_opencode_json_config.sh [--file config.json | --from-configmap | --from-defaults]
                                       [--to-router [--router-only]] [--live]
                                       [--user ID]... [--apply] [--namespace NS]
                                       [--configmap-namespace NS]

STAGES opencode.json (models, providers, MCP servers, permissions) into every
opencode-web user environment (all deployments labeled
opencode-user-managed=true, warm-pool units included).

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
Use --live to replace the live config instead (rolling backup first, then a
temp-file+mv). The old TARGET_NAME=opencode.json spelling still does that but is
deprecated in favour of --live.

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
  --to-router          publish --file into the canonical ConfigMap: only the
                       '$CM_KEY' data key, via a JSON merge patch, so the sibling
                       keys (apply-defaults.mjs, skill-*.md, agent-*.md) survive
                       untouched. Requires --file and cannot be combined with
                       --from-configmap / --from-defaults. The ConfigMap lives in
                       the RELEASE namespace, never in a user's
                       'project-user-*' namespace. A payload identical to the
                       stored value is skipped ("already up to date") and every
                       write is verified byte-for-byte afterwards.
                       CAVEAT: 'helm upgrade' re-renders this ConfigMap from
                       values.yaml and discards what --to-router wrote.
  --router-only        with --to-router: stop after the canonical write. No pod
                       is listed and none is contacted (no inventory, no exec).
  --live               write the LIVE config in each pod instead of the staged
                       sibling: rolling backup ($BACKUP) first, then a
                       temp-file+mv replacement so the live file is never
                       truncated in place. Loudly lists the affected units.
                       Needs --apply to actually run; without it you only get the
                       preview. Combining --to-router --file X --apply --live
                       publishes the canonical copy AND replaces the live config
                       in every unit.
  --list-users         list the user units and the --user IDs, then exit
  --user ID            stage ONLY to the unit(s) of this user — repeatable and
                       comma-separated (--user me --user you / --user me,you).
                       Matched case-insensitively against the unit's
                       opencode-user-slug / opencode-user-owner labels and the
                       deployment name. Omit to target EVERY user unit.
                       An ID matching no unit is an ERROR (exit 1), so a typo
                       can never silently fall back to pushing to all.
  --namespace NS       target namespace (default: $NAMESPACE)
  --configmap-namespace NS
                       namespace holding '<release>-project-config' for
                       --from-configmap and --to-router. Needed when the target
                       namespace is a platform user's own (project-user-<slug>):
                       the ConfigMap lives in the RELEASE namespace, not the
                       user's. Left unset, the target namespace is tried first
                       and the release namespace is used as a fallback.
  --help               this help

env overrides: KUBECTL (default: kubectl), NAMESPACE, CONTAINER (default: $CONTAINER),
               TARGET_NAME (default: opencode-update.json; 'opencode.json' is a
               deprecated alias for --live), RELEASE_NAMESPACE (ConfigMap namespace
               fallback; default: auto), CONFIGMAP_NAME (default: $CM_NAME),
               CONFIG_KEY (default: $CM_KEY)

two-phase workflow:
  1. canonical:  $0 --to-router --file new.json --router-only --apply
  2. stage:      $0 --from-configmap --apply
  3. promote:    per-unit command printed by step 2 (or use --live in step 1/2)
EOF
}

APPLY=0
FILE=""
FROM_DEFAULTS=0
FROM_CONFIGMAP=0
LIST_USERS=0
TO_ROUTER=0
ROUTER_ONLY=0
LIVE_MODE=0

# Scratch files, all under one temp dir, removed on exit. TMP_CURRENT and
# TMP_READBACK are FILES on purpose: capturing a ConfigMap value in $(...) would
# strip its trailing newlines, and both the idempotency check and the post-write
# verification are byte comparisons.
TMP_DIR=""
TMP_PATCH=""
TMP_CURRENT=""
TMP_READBACK=""
TMP_PATCHJSON=""
TMP_ERR=""
cleanup_tmp() {
  if [ -n "$TMP_PATCH" ] && [ -f "$TMP_PATCH" ]; then
    rm -f "$TMP_PATCH"
  fi
  if [ -n "$TMP_DIR" ] && [ -d "$TMP_DIR" ]; then
    rm -rf "$TMP_DIR"
  fi
  return 0
}
trap cleanup_tmp EXIT
ensure_tmp_dir() {
  if [ -z "$TMP_DIR" ]; then
    TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/opencode-push.XXXXXX")"
    TMP_CURRENT="$TMP_DIR/current.json"
    TMP_READBACK="$TMP_DIR/readback.json"
    TMP_PATCHJSON="$TMP_DIR/merge-patch.json"
    TMP_ERR="$TMP_DIR/stderr"
    : > "$TMP_CURRENT"
    : > "$TMP_ERR"
  fi
}
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
    --to-router)
      TO_ROUTER=1
      shift
      ;;
    --router-only)
      ROUTER_ONLY=1
      shift
      ;;
    --live)
      LIVE_MODE=1
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

# ------------------------------------------------------------ flag validation
# --to-router is the only mode that writes the canonical ConfigMap, and it can
# only write what it was handed: a local --file.
if [ "$TO_ROUTER" -eq 1 ]; then
  if [ -z "$FILE" ]; then
    echo "error: --to-router requires --file PATH (the payload to publish into the canonical ConfigMap)" >&2
    echo "       nothing was written. e.g.:" >&2
    echo "         $0 --to-router --file new.json --router-only --apply" >&2
    exit 1
  fi
  if [ "$FROM_CONFIGMAP" -eq 1 ] || [ "$FROM_DEFAULTS" -eq 1 ]; then
    echo "error: --to-router cannot be combined with --from-configmap or --from-defaults" >&2
    echo "       (--to-router WRITES the canonical copy, so it needs a real --file source)" >&2
    exit 1
  fi
fi
if [ "$ROUTER_ONLY" -eq 1 ] && [ "$TO_ROUTER" -eq 0 ]; then
  echo "error: --router-only only makes sense together with --to-router" >&2
  exit 1
fi
if [ "$TO_ROUTER" -eq 1 ] && [ "$LIST_USERS" -eq 1 ]; then
  echo "error: --to-router cannot be combined with --list-users" >&2
  exit 1
fi
if [ "$ROUTER_ONLY" -eq 1 ] && [ "$LIVE_MODE" -eq 1 ]; then
  echo "note: --live has no effect with --router-only (no pod is listed or contacted)" >&2
fi
if [ "$ROUTER_ONLY" -eq 1 ] && [ "${#USERS[@]}" -gt 0 ]; then
  echo "note: --user has no effect with --router-only (the canonical copy is global)" >&2
fi

# --live (or the deprecated TARGET_NAME=opencode.json) replaces the LIVE file.
# Decided here, before any cluster call, so the dry-run preview and the warning
# below describe the real destination.
OVERWRITE=0
if [ "$LIVE_MODE" -eq 1 ]; then
  OVERWRITE=1
fi
if [ "$TARGET_NAME" = "opencode.json" ]; then
  OVERWRITE=1
  if [ "$LIVE_MODE" -eq 0 ]; then
    echo "warning: TARGET_NAME=opencode.json is deprecated — use --live (same effect: rolling backup, then replace the live config)" >&2
  fi
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
        # Publishing an unparsable payload would poison the canonical copy that
        # every later --from-configmap push and every unit inherits, so here it
        # is fatal (unlike a plain push, where opencode re-validates anyway).
        if [ "$TO_ROUTER" -eq 1 ]; then
          echo "error: $FILE is not valid JSON — refusing to publish it into the canonical ConfigMap" >&2
          echo "       (every later --from-configmap push and every unit would inherit the breakage)" >&2
          exit 1
        fi
        echo "warning: $FILE is not valid JSON; pushing anyway — opencode validates it before restarting" >&2
        ;;
      *) echo "note: JSON pre-check skipped (python3 unavailable) — opencode still validates before restarting" >&2 ;;
    esac
  fi
fi

# ------------------------------------------------- canonical ConfigMap helpers
# The canonical copy of the config as rendered from values.yaml lives in the
# release's '<release>-project-config' ConfigMap (templates/configmap-project.yaml).
# Reading it needs no running pod, so it is the natural source for a push;
# writing it back (--to-router) is how an admin publishes a new canonical config
# without editing values.yaml and running helm upgrade.
#
# NAMESPACE vs RELEASE NAMESPACE: the ConfigMap is rendered into
# .Release.Namespace, but a platform (SSO) user's unit lives in its own
# 'project-user-<slug>' namespace. Looking only in $NAMESPACE therefore failed
# with NotFound for exactly the users an operator most wants to push to — and it
# would make --to-router patch the wrong (non-existent) place. Resolution order
# (first hit wins):
#   1. --configmap-namespace / RELEASE_NAMESPACE, when set (explicit, no probing)
#   2. $NAMESPACE          (classic/local users: the unit IS in the release ns)
#   3. the release namespace derived from the router Deployment's namespace

# jsonpath for the data key; the dots inside the key must be escaped.
CM_JSONPATH="{.data.$(printf '%s' "$CM_KEY" | sed 's/\./\\./g')}"

# Release namespace guess: the namespace of the chart's own router Deployment,
# found via the app label every chart object carries. Empty if not resolvable.
derive_release_namespace() {
  # Every step is guarded: under 'set -euo pipefail' a failing or empty
  # cluster-wide query (RBAC/policy-blocked -A, no matches) would otherwise
  # abort the whole script with a silent exit 1. Failing to DERIVE a
  # namespace is not an error — the caller still has $NAMESPACE to try.
  "$KUBECTL" get deploy -A -l "hpe-ezua/app=opencode-web-helm" \
    -o jsonpath='{range .items[*]}{.metadata.namespace}{"\n"}{end}' 2>/dev/null \
    | head -1 || true
}

cm_candidates() {
  if [ -n "$RELEASE_NAMESPACE" ]; then
    printf '%s\n' "$RELEASE_NAMESPACE"
  else
    printf '%s\n' "$NAMESPACE"
    _derived="$(derive_release_namespace)"
    if [ -n "$_derived" ] && [ "$_derived" != "$NAMESPACE" ]; then
      printf '%s\n' "$_derived"
    fi
  fi
}

# resolve_cm: locate '<release>-project-config' and read $CM_KEY into
# $TMP_CURRENT. The value goes to a FILE (never $(...)), so trailing newlines
# survive and both the idempotency check and the read-back verification compare
# real bytes. Success sets CM_FOUND_NS; failure returns 1 with the last kubectl
# error in CM_ERR. A missing KEY is not a failure here (kubectl exits 0 with
# empty output) — the caller decides whether that is fatal.
resolve_cm() {
  ensure_tmp_dir
  CM_FOUND_NS=""
  CM_ERR=""
  for _cmns in $(cm_candidates); do
    if "$KUBECTL" get configmap -n "$_cmns" "$CM_NAME" \
          -o jsonpath="$CM_JSONPATH" >"$TMP_CURRENT" 2>"$TMP_ERR"; then
      CM_FOUND_NS="$_cmns"
      return 0
    fi
    CM_ERR="$(cat "$TMP_ERR" 2>/dev/null || true)"
    : > "$TMP_CURRENT"
  done
  return 1
}

sha256_of() {
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

# build_merge_patch <payload> <out>: emit {"data":{"<key>":"<payload>"}} — ONE
# key and nothing else, so a merge patch cannot disturb the ConfigMap's sibling
# keys (apply-defaults.mjs, skill-*.md, agent-*.md). Returns 2 when no JSON tool
# is on PATH (python3 preferred, jq as fallback).
build_merge_patch() {
  if command -v python3 >/dev/null 2>&1; then
    python3 - "$1" "$2" "$CM_KEY" <<'PY'
import json, sys
src, out, key = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    text = open(src, "rb").read().decode("utf-8")
except UnicodeDecodeError:
    sys.exit("payload is not valid UTF-8")
with open(out, "w", encoding="utf-8") as fh:
    json.dump({"data": {key: text}}, fh, ensure_ascii=False)
    fh.write("\n")
PY
    return $?
  fi
  if command -v jq >/dev/null 2>&1; then
    jq -Rs --arg key "$CM_KEY" '{data: {($key): .}}' < "$1" > "$2"
    return $?
  fi
  return 2
}

# print_live_warning: --live is the destructive path, so say so loudly and name
# every unit whose live config is about to be replaced.
print_live_warning() {
  echo "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!"
  echo "WARNING: --live REPLACES THE LIVE CONFIG in $COUNT unit(s) in namespace '$NAMESPACE'"
  echo "  live file: $LIVE"
  echo "  backup:    $BACKUP  (copied per unit BEFORE the live file is touched)"
  echo "  write:     temp file + mv, never a truncate in place"
  echo "  affected units:"
  for _i in "${!SEL_NAMES[@]}"; do
    if [ -n "${SEL_USERS[$_i]}" ]; then
      echo "   - ${SEL_NAMES[$_i]} (user: ${SEL_USERS[$_i]})"
    else
      echo "   - ${SEL_NAMES[$_i]}"
    fi
  done
  echo "  opencode hot-reloads the file; users may need to refresh their browser."
  echo "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!"
}

# ----------------------------------------------- source: --from-configmap
if [ "$FROM_CONFIGMAP" -eq 1 ]; then
  if ! resolve_cm; then
    echo "error: ConfigMap '$CM_NAME' not found in: $(cm_candidates | tr '\n' ' ')" >&2
    echo "       last kubectl error: $CM_ERR" >&2
    echo "       (upgrade the chart so templates/configmap-project.yaml is applied," >&2
    echo "        pass --configmap-namespace <release-ns>, or use --file / --from-defaults.)" >&2
    exit 1
  fi
  if [ ! -s "$TMP_CURRENT" ]; then
    echo "error: ConfigMap '$CM_NAME' in '$CM_FOUND_NS' has no '$CM_KEY' key (or it is empty)" >&2
    echo "       Is values.opencodeConfig set? Use --file / --from-defaults instead." >&2
    exit 1
  fi
  FILE="$TMP_CURRENT"
  echo "source: --from-configmap ($CM_NAME in '$CM_FOUND_NS' -> $CM_KEY)"
fi

# --------------------------------------------------- phase 1: canonical write
# --to-router publishes the payload into '<release>-project-config' as a PER-KEY
# merge patch. It is deliberately not kubectl apply/replace: that ConfigMap also
# carries apply-defaults.mjs, skill-*.md and agent-*.md, and a wholesale replace
# would silently delete them.
ROUTER_NS=""
if [ "$TO_ROUTER" -eq 1 ]; then
  if ! resolve_cm; then
    echo "error: canonical ConfigMap '$CM_NAME' not found in: $(cm_candidates | tr '\n' ' ')" >&2
    echo "       last kubectl error: $CM_ERR" >&2
    echo "       --to-router PATCHES the existing ConfigMap; it cannot create it." >&2
    echo "       (upgrade the chart so templates/configmap-project.yaml is applied," >&2
    echo "        or pass --configmap-namespace <release-ns>.)" >&2
    exit 1
  fi
  ROUTER_NS="$CM_FOUND_NS"
  ROUTER_SHA="$(sha256_of "$FILE")"

  if [ "$APPLY" -eq 0 ]; then
    echo "canonical write (NOT executed - dry run):"
    echo "  configmap: $CM_NAME"
    echo "  namespace: $ROUTER_NS  (release namespace, NOT the user's)"
    echo "  key:       $CM_KEY  (per-key merge patch; sibling keys untouched)"
    echo "  source:    $FILE"
    echo "  sha256:    $ROUTER_SHA"
    if cmp -s "$TMP_CURRENT" "$FILE"; then
      echo "  action:    none — the canonical copy already holds exactly these bytes"
    else
      echo "  action:    kubectl patch configmap $CM_NAME -n $ROUTER_NS --type merge --patch-file <tmp>"
    fi
    if [ "$ROUTER_ONLY" -eq 1 ]; then
      echo "fan-out: skipped (--router-only — no pod is listed or contacted)"
      echo "re-run with --apply to publish the canonical copy"
      exit 0
    fi
    echo
  else
    echo "canonical write: $CM_NAME in '$ROUTER_NS' (key: $CM_KEY)"
    echo "  source: $FILE"
    echo "  sha256: $ROUTER_SHA"
    if cmp -s "$TMP_CURRENT" "$FILE"; then
      echo "canonical: already up to date — the ConfigMap holds exactly these bytes (no patch)"
    else
      if ! build_merge_patch "$FILE" "$TMP_PATCHJSON"; then
        echo "error: cannot build the JSON merge patch — python3 or jq is required on PATH" >&2
        echo "       (refusing to write the canonical ConfigMap without a JSON-safe patch)" >&2
        exit 1
      fi
      if _patch_out="$("$KUBECTL" patch configmap "$CM_NAME" -n "$ROUTER_NS" \
            --type merge --patch-file "$TMP_PATCHJSON" 2>&1)"; then
        :
      else
        case "$_patch_out" in
          # Older kubectl builds predate --patch-file; retry with -p. Anything
          # else is a real failure and must surface verbatim.
          *"unknown flag"*|*"unknown shorthand"*|*"not defined"*)
            echo "note: this kubectl has no --patch-file; retrying with -p" >&2
            if ! _patch_out="$("$KUBECTL" patch configmap "$CM_NAME" -n "$ROUTER_NS" \
                  --type merge -p "$(cat "$TMP_PATCHJSON")" 2>&1)"; then
              echo "error: kubectl patch failed: $_patch_out" >&2
              exit 1
            fi
            ;;
          *)
            echo "error: kubectl patch failed: $_patch_out" >&2
            exit 1
            ;;
        esac
      fi
      # Verify by reading the key BACK INTO A FILE and comparing bytes. $(...)
      # would strip trailing newlines and let a truncated write pass.
      if ! "$KUBECTL" get configmap -n "$ROUTER_NS" "$CM_NAME" \
            -o jsonpath="$CM_JSONPATH" >"$TMP_READBACK" 2>"$TMP_ERR"; then
        echo "error: canonical write verification failed — cannot read '$CM_KEY' back: $(cat "$TMP_ERR")" >&2
        exit 1
      fi
      if ! cmp -s "$TMP_READBACK" "$FILE"; then
        echo "error: canonical write verification FAILED — the ConfigMap does not hold the pushed bytes" >&2
        echo "       pushed:   sha256 $(sha256_of "$FILE") ($(wc -c < "$FILE") bytes)" >&2
        echo "       readback: sha256 $(sha256_of "$TMP_READBACK") ($(wc -c < "$TMP_READBACK") bytes)" >&2
        echo "       (the ConfigMap may now hold a partial payload — inspect it before retrying)" >&2
        exit 1
      fi
      echo "canonical: written and verified (sha256 $ROUTER_SHA)"
    fi
    if [ "$ROUTER_ONLY" -eq 1 ]; then
      echo "canonical: done — stopping here (--router-only): no pod was listed, contacted or staged"
      exit 0
    fi
    echo
  fi
fi

# ------------------------------------------------------------------ inventory
# One line per opencode-user-managed deployment: name|slug|owner|dind-broker.
if ! INVENTORY="$("$KUBECTL" get deploy -n "$NAMESPACE" -l opencode-user-managed=true \
      -o jsonpath='{range .items[*]}{.metadata.name}{"|"}{.metadata.labels.opencode-user-slug}{"|"}{.metadata.labels.opencode-user-owner}{"|"}{.metadata.labels.opencode-dind-broker}{"\n"}{end}' 2>&1)"; then
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
  echo "no user deployments found in namespace '$NAMESPACE' (label selector: opencode-user-managed=true)"
  exit 0
fi

if [ "$LIST_USERS" -eq 1 ]; then
  echo "user units in namespace '$NAMESPACE' (label selector: opencode-user-managed=true)"
  printf '  %-38s %-26s %s\n' "DEPLOYMENT" "--user ID" "NOTE"
  for _i in "${!ALL_NAMES[@]}"; do
    _id="${ALL_SLUGS[$_i]:-${ALL_OWNERS[$_i]:-}}"
    if [ "${ALL_BROKERS[$_i]}" = "true" ]; then
      printf '  %-38s %-26s %s\n' "${ALL_NAMES[$_i]}" "-" "docker-in-docker broker, not an opencode unit"
    elif [ -z "$_id" ]; then
      printf '  %-38s %-26s %s\n' "${ALL_NAMES[$_i]}" "-" "no opencode-user-slug / -owner label"
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
    echo "note: skipping ${#SKIPPED_BROKERS[@]} docker-in-docker broker deployment(s) (no opencode-web container): ${SKIPPED_BROKERS[*]}" >&2
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
      _np="${_n#opencode-user-}"
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
    echo "       (label selector: opencode-user-managed=true) — nothing was pushed" >&2
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
        if [ "${ALL_BROKERS[$_i]}" = "true" ] && [ "${ALL_NAMES[$_i]}" = "opencode-dind-$_norm" ]; then
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

# ---------------------------------------------------------------------- push
if [ "$APPLY" -eq 0 ]; then
  # With --to-router the canonical phase was already described above; this block
  # is the fan-out half of the preview, kept separate on purpose.
  if [ "$TO_ROUTER" -eq 1 ]; then
    echo "fan-out (NOT executed - dry run):"
  else
    echo "DRY RUN - nothing executed"
  fi
  if [ "$OVERWRITE" -eq 1 ]; then
    _dest="replace the LIVE config ($LIVE)"
  else
    _dest="stage as $TARGET_NAME"
  fi
  if [ "$FROM_CONFIGMAP" -eq 1 ]; then
    echo "mode: $_dest from the canonical ConfigMap (--from-configmap)"
  elif [ -n "$FILE" ]; then
    echo "mode: $_dest from local file '$FILE'"
  else
    echo "mode: $_dest from the chart-mounted defaults ($DEFAULTS)"
    echo "note: run a helm upgrade of opencodeConfig first so the mounted ConfigMap is current"
  fi
  if [ "$OVERWRITE" -eq 1 ]; then
    print_live_warning
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

if [ "$OVERWRITE" -eq 1 ]; then
  print_live_warning
fi
if [ "$TO_ROUTER" -eq 1 ]; then
  echo "fan-out: $COUNT deployment(s) in namespace '$NAMESPACE'"
else
  echo "updating $COUNT deployment(s) in namespace '$NAMESPACE'"
fi
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
    if [ -n "$FILE" ]; then
      _cmd="$LIVE_WRITE_CMD"
    else
      # --from-defaults + --live: copy the mounted file over the live one. No
      # stdin is attached here, so the stdin form would truncate it to nothing.
      _cmd="$LIVE_COPY_CMD"
    fi
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
      echo "[$i/$COUNT] $_label: LIVE config replaced (previous -> $(basename "$BACKUP"))"
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
  echo "done: the LIVE config was replaced in $COUNT deployment(s)"
  echo "      previous config saved per unit as $BACKUP"
  echo "      (opencode hot-reloads it; users may need to refresh their browser)"
else
  echo "done: staged $TARGET_NAME in $COUNT deployment(s) — the live config is UNCHANGED"
  echo "      promote when ready, e.g. per unit:"
  echo "        kubectl exec -n <ns> deploy/<unit> -c $CONTAINER -- sh -c 'cd $CONFIG_DIR && cp -p opencode.json opencode-backup.json && mv $TARGET_NAME opencode.json'"
  echo "      (the supervisor validates the JSON and restarts opencode on the change)"
fi
