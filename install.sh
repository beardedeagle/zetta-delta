#!/usr/bin/env bash
# Install the Delta orchestration bundle: custom subagent profiles, the
# /orchestrate, /adversarial, /isolated, and /pr-review skills, and a roster generated for
# this machine's accounts (one copy in each skill).
# Also writes the context router and Ponytail into Delta's Personal AGENTS.md
# (~/.config/delta/AGENTS.md), replacing only those two blocks and keeping the
# rest of the file. --clean removes the personal-AGENTS.generated.md that
# earlier versions left in $XDG_STATE_HOME/zetta-delta for pasting.
# If any file it would write exists, it changes nothing without --force; with
# --force it first saves each file it changes under
# $XDG_STATE_HOME/zetta-delta/backups/<UTC time>/, which --clean keeps.
# Run it from a clone, or piped from curl with ZETTA_DELTA_REF set (README).
#
# Required (a custom provider's id is "custom:" + sha256 of its base URL; see README):
#   KIMI_PROVIDER        Delta provider id for Kimi Code
#   ZAI_PROVIDER         Delta provider id for the Z.AI GLM Coding Plan
#   QWEN_PROVIDER        Delta provider id for the Qwen Cloud Token Plan
# Plan tiers (set these to your real tiers):
#   ZAI_TIER             lite | pro | max                     (default lite)
#   QWEN_TIER            lite | essential | standard | pro    (default lite)
# Optional lanes (each installs its profiles when set):
#   MINIMAX_PROVIDER     plus MINIMAX_BILLING=plan|metered    (default metered)
#                        -> minimax (worker; also best-of-N candidate with plan billing)
#   GPT_PROVIDER         ChatGPT subscription (openai-subscribed) -> gpt-sol (worker, candidate), gpt-astra (reviewer)
#   GROK_PROVIDER        Grok subscription (x_ai-subscribed) -> grok (worker, reviewer, candidate)
#   COPILOT_PROVIDER     GitHub Copilot       -> scout-gemini
#   LOCAL_PROVIDER       plus LOCAL_MODEL (local inference) -> scout-local
# Delta offers at most 7 custom profiles. The installer installs them in this
# order, after counting every other profile already installed, and names the rest:
#   qwen-max deepseek-pro grok gpt-sol minimax scout-gemini scout-local
#   gpt-astra scout-qwen scout-deepseek
# Optional tuning:
#   PROFILE_PRIORITY="<names>"  profiles to install first; the rest keep the order above
#   KIMI_LIMIT=3  LOCAL_LIMIT=2  THREAD_CAP=6  METERED_MAX_SPAWNS=2
#   METERED_LANES="DeepSeek API, OpenCode Zen"
#   ORCHESTRATOR_MODEL=k3  ORCHESTRATOR_LANE="Kimi Code"  ORCHESTRATOR_FAMILY=Kimi
#   BUILTIN_{SCOUT,WORKER,REVIEWER}_{MODEL,LANE,FAMILY}  (see README)
#   BUILTIN_{SCOUT,WORKER,REVIEWER}_{PROVIDER,BILLING,LIMIT}
#   BUILTIN_{SCOUT,WORKER,REVIEWER}_EFFORT   high if catalog-supported; otherwise unpinned
#     Empty omits the pin; explicit known-model efforts must be supported.
#     Known lanes infer their provider, billing, and shared limit. API/custom
#     lanes need an explicit provider id and limit; unknown lanes also need
#     billing=flat|metered. Roles sharing a lane must agree on billing/limit.
#   DELTA_CONFIG_DIR     overrides Delta's config directory (same as Delta)
#   PONYTAIL_DIR         ponytail plugin version dir (default: newest under
#                        ~/.codex/plugins/cache/ponytail/ponytail/)
#   ZETTA_DELTA_REF      commit to fetch the bundle at when none is next to this
#                        script (curl | bash); there is no default
#
# Usage: install.sh [--force] [--dry-run] [--configure-delta] [--prune-legacy] [--skill-dir DIR] [--clean]
# --configure-delta merges bundled credential-free providers and subagent settings.
# Quit Delta before installation. Existing files need --force and are backed up.
set -euo pipefail
# One brace group, closed on the last line: bash parses all of it before
# running any of it, so a download cut short (curl | bash) runs nothing.
{

die()  { printf 'install.sh: error: %s\n' "$*" >&2; exit 1; }
info() { printf '%s\n' "$*"; }
warn() { printf 'install.sh: warning: %s\n' "$*" >&2; }

usage() {
  awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "$SELF"
}

FORCE=0
DRY_RUN=0
PRUNE_LEGACY=0
CLEAN=0
HELP=0
CONFIGURE_DELTA=0
SKILL_ROOT="${HOME}/.agents/skills"

while (($#)); do
  case "$1" in
    --force)        FORCE=1 ;;
    --dry-run)      DRY_RUN=1 ;;
    --prune-legacy) PRUNE_LEGACY=1 ;;
    --skill-dir)    [[ $# -ge 2 ]] || die "--skill-dir needs a value"; SKILL_ROOT="$2"; shift ;;
    --clean)        CLEAN=1 ;;
    --configure-delta) CONFIGURE_DELTA=1 ;;
    -h|--help)      HELP=1 ;;
    *)              die "unknown argument: $1 (see --help)" ;;
  esac
  shift
done

# Rendered helper commands need one absolute shell argument. Line breaks cannot
# be represented by the line-oriented templates; reject them before any writes.
if ((!CLEAN && !HELP)); then
  [[ $SKILL_ROOT != *$'\n'* && $SKILL_ROOT != *$'\r'* ]] || die "--skill-dir must not contain line breaks"
  [[ ! $SKILL_ROOT =~ \{\{[A-Z_]*\}\} ]] || die "--skill-dir must not contain template placeholder syntax"
fi

# Earlier versions left the rules here for pasting into Delta; --clean removes
# that file and keeps the backups.
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/zetta-delta"
[[ $STATE_DIR == /* ]] || die "XDG_STATE_HOME must be an absolute path"
RULES_OUT="$STATE_DIR/personal-AGENTS.generated.md"
BACKUP_DIR="$STATE_DIR/backups/$(date -u +%Y%m%dT%H%M%SZ)-$$"
readonly STATE_DIR RULES_OUT BACKUP_DIR
if ((CLEAN && !HELP)); then
  if [[ ! -e $RULES_OUT ]]; then info "nothing to remove: $RULES_OUT does not exist"
  elif ((DRY_RUN)); then info "would remove $RULES_OUT"
  else rm -f -- "$RULES_OUT"; info "removed $RULES_OUT"
  fi
  if [[ -d $STATE_DIR/backups ]]; then info "kept the backups in $STATE_DIR/backups"
  elif ((!DRY_RUN)) && [[ -d $STATE_DIR ]]; then rmdir -- "$STATE_DIR" 2>/dev/null || true
  fi
  exit 0
fi

# ---------------------------------------------------------------- bundle ---
# From a clone, the bundle sits next to this script. Piped from curl (or saved
# alone), fetch the repository at ZETTA_DELTA_REF into a temporary folder,
# removed on exit, and install from there.

readonly REPO_URL="https://github.com/beardedeagle/zetta-delta"
SELF="${BASH_SOURCE[0]:-}"
if [[ -f $SELF && -f $(dirname -- "$SELF")/profiles/model.toml.tmpl ]]; then
  SCRIPT_DIR="$(cd -- "$(dirname -- "$SELF")" && pwd)"
else
  ref="${ZETTA_DELTA_REF:-}"
  [[ -n $ref ]] || die "no bundle next to this script; set ZETTA_DELTA_REF to the commit to install (see README)"
  [[ $ref =~ ^[A-Za-z0-9._-]+$ ]] || die "ZETTA_DELTA_REF='$ref' is invalid (a commit, tag, or branch name)"
  command -v curl >/dev/null 2>&1 || die "curl not found"
  SCRIPT_DIR="$(mktemp -d "${TMPDIR:-/tmp}/zetta-delta.XXXXXX")"
  trap 'rm -rf -- "$SCRIPT_DIR"' EXIT
  info "fetching $REPO_URL at $ref"
  curl -fsSL --proto '=https' "$REPO_URL/archive/$ref.tar.gz" \
    | tar -xzf - -C "$SCRIPT_DIR" --strip-components=1 \
    || die "could not fetch $REPO_URL at $ref"
  [[ -f $SCRIPT_DIR/profiles/model.toml.tmpl ]] || die "$REPO_URL at $ref has no bundle"
  SELF="$SCRIPT_DIR/install.sh"
fi
readonly SCRIPT_DIR SELF
if ((HELP)); then usage; exit 0; fi

# ---------------------------------------------------------------- inputs ---

# The same PATH prefix travels in the generated rules to Delta's POSIX shell.
TOOL_BIN="${PREFIX:-$HOME/.local}/bin"
export PATH="$TOOL_BIN:$PATH"
REQUIRED_TOOLS=(python3 git gh rtk tgrep semble codegraph zg ctx7 githits caveman node)
for tool in "${REQUIRED_TOOLS[@]}"; do
  command -v "$tool" >/dev/null 2>&1 || die "$tool not found; run bootstrap.sh from a clone first"
done
python3 -c 'import tomllib' 2>/dev/null || die "Python 3.11 or later is required; run bootstrap.sh first"
if command -v pgrep >/dev/null 2>&1 && { pgrep -x Delta >/dev/null 2>&1 || pgrep -x delta >/dev/null 2>&1; }; then
  die "quit Delta before changing its configuration; then rerun the installer"
fi
if ((CONFIGURE_DELTA)); then
  KIMI_PROVIDER="${KIMI_PROVIDER:-$(python3 "$SCRIPT_DIR/scripts/setup.py" provider-id kimi)}"
  ZAI_PROVIDER="${ZAI_PROVIDER:-$(python3 "$SCRIPT_DIR/scripts/setup.py" provider-id zai)}"
  QWEN_PROVIDER="${QWEN_PROVIDER:-$(python3 "$SCRIPT_DIR/scripts/setup.py" provider-id qwen)}"
fi
: "${KIMI_PROVIDER:?set KIMI_PROVIDER to the Delta provider id for Kimi Code}"
: "${ZAI_PROVIDER:?set ZAI_PROVIDER to the Delta provider id for the Z.AI Coding Plan}"
: "${QWEN_PROVIDER:?set QWEN_PROVIDER to the Delta provider id for the Qwen Token Plan}"

ZAI_TIER="${ZAI_TIER:-lite}"
QWEN_TIER="${QWEN_TIER:-lite}"
MINIMAX_PROVIDER="${MINIMAX_PROVIDER:-}"
MINIMAX_BILLING="${MINIMAX_BILLING:-metered}"
GPT_PROVIDER="${GPT_PROVIDER:-}"
GROK_PROVIDER="${GROK_PROVIDER:-}"
COPILOT_PROVIDER="${COPILOT_PROVIDER:-}"
LOCAL_PROVIDER="${LOCAL_PROVIDER:-}"; LOCAL_MODEL="${LOCAL_MODEL:-}"
LOCAL_FAMILY="${LOCAL_FAMILY:-Local}"
PROFILE_PRIORITY="${PROFILE_PRIORITY:-}"
KIMI_LIMIT="${KIMI_LIMIT:-3}"
LOCAL_LIMIT="${LOCAL_LIMIT:-2}"
THREAD_CAP="${THREAD_CAP:-6}"
METERED_MAX_SPAWNS="${METERED_MAX_SPAWNS:-2}"
METERED_LANES="${METERED_LANES:-DeepSeek API, OpenCode Zen}"
ORCHESTRATOR_MODEL="${ORCHESTRATOR_MODEL:-k3}"
ORCHESTRATOR_LANE="${ORCHESTRATOR_LANE:-Kimi Code}"
ORCHESTRATOR_FAMILY="${ORCHESTRATOR_FAMILY:-Kimi}"
BUILTIN_SCOUT_MODEL="${BUILTIN_SCOUT_MODEL:-glm-5.3-flash}"
BUILTIN_SCOUT_LANE="${BUILTIN_SCOUT_LANE:-Z.AI Coding Plan}"
BUILTIN_SCOUT_FAMILY="${BUILTIN_SCOUT_FAMILY:-GLM}"
BUILTIN_WORKER_MODEL="${BUILTIN_WORKER_MODEL:-k3}"
BUILTIN_WORKER_LANE="${BUILTIN_WORKER_LANE:-Kimi Code}"
BUILTIN_WORKER_FAMILY="${BUILTIN_WORKER_FAMILY:-Kimi}"
BUILTIN_REVIEWER_MODEL="${BUILTIN_REVIEWER_MODEL:-glm-5.3}"
BUILTIN_REVIEWER_LANE="${BUILTIN_REVIEWER_LANE:-Z.AI Coding Plan}"
BUILTIN_REVIEWER_FAMILY="${BUILTIN_REVIEWER_FAMILY:-GLM}"

# Values are substituted with sed and written into Markdown, so restrict them.
check() { # $1=name $2=value $3=regex $4=hint
  [[ $2 =~ $3 ]] || die "$1='$2' is invalid ($4)"
}
id_re='^[A-Za-z0-9._:-]+$'
model_re='^[A-Za-z0-9._:/@+-]+$'
text_re='^[A-Za-z0-9 .,_()/:+-]+$'
int_re='^[1-9][0-9]*$'

check KIMI_PROVIDER "$KIMI_PROVIDER" "$id_re" "letters, digits, . _ : -"
check ZAI_PROVIDER  "$ZAI_PROVIDER"  "$id_re" "letters, digits, . _ : -"
check QWEN_PROVIDER "$QWEN_PROVIDER" "$id_re" "letters, digits, . _ : -"
for v in KIMI_LIMIT LOCAL_LIMIT THREAD_CAP METERED_MAX_SPAWNS; do
  check "$v" "${!v}" "$int_re" "positive integer"
done
for v in METERED_LANES ORCHESTRATOR_LANE ORCHESTRATOR_FAMILY LOCAL_FAMILY \
         BUILTIN_SCOUT_LANE BUILTIN_SCOUT_FAMILY BUILTIN_WORKER_LANE \
         BUILTIN_WORKER_FAMILY BUILTIN_REVIEWER_LANE BUILTIN_REVIEWER_FAMILY; do
  check "$v" "${!v}" "$text_re" "letters, digits, spaces, . , _ ( ) / : + -"
done
for v in ORCHESTRATOR_MODEL BUILTIN_SCOUT_MODEL BUILTIN_WORKER_MODEL BUILTIN_REVIEWER_MODEL; do
  check "$v" "${!v}" "$model_re" "model id characters"
done

case "$ZAI_TIER" in
  lite) ZAI_LIMIT=1 ;; pro) ZAI_LIMIT=2 ;; max) ZAI_LIMIT=3 ;;
  *) die "ZAI_TIER must be lite, pro, or max" ;;
esac
# Lower bound of each tier's published concurrent-agent range.
case "$QWEN_TIER" in
  lite) QWEN_LIMIT=1 ;; essential) QWEN_LIMIT=2 ;; standard) QWEN_LIMIT=3 ;; pro) QWEN_LIMIT=6 ;;
  *) die "QWEN_TIER must be lite, essential, standard, or pro" ;;
esac
case "$MINIMAX_BILLING" in plan|metered) ;; *) die "MINIMAX_BILLING must be plan or metered" ;; esac
if [[ $MINIMAX_BILLING == plan ]]; then MINIMAX_TIER=flat; MINIMAX_LIMIT=2; else MINIMAX_TIER=metered; MINIMAX_LIMIT=1; fi
check PROFILE_PRIORITY "$PROFILE_PRIORITY" '^[a-z -]*$' "profile names separated by spaces"

pair() { # both or neither
  if [[ -n $2 && -z $4 ]] || [[ -z $2 && -n $4 ]]; then die "set both $1 and $3, or neither"; fi
}
pair LOCAL_PROVIDER "$LOCAL_PROVIDER" LOCAL_MODEL "$LOCAL_MODEL"
# The GPT and Grok profiles pin their models; refuse a leftover setting rather than ignore it.
for v in GPT_MODEL GROK_MODEL; do
  [[ -z ${!v:-} ]] || die "$v is no longer used: the GPT and Grok profiles pin their models; unset it"
done
[[ -z $MINIMAX_PROVIDER ]] || check MINIMAX_PROVIDER "$MINIMAX_PROVIDER" "$id_re" "letters, digits, . _ : -"
for v in GPT_PROVIDER GROK_PROVIDER COPILOT_PROVIDER; do
  [[ -z ${!v} ]] || check "$v" "${!v}" "$id_re" "id"
done
if [[ -n $LOCAL_PROVIDER ]]; then check LOCAL_PROVIDER "$LOCAL_PROVIDER" "$id_re" "id"; check LOCAL_MODEL "$LOCAL_MODEL" "$model_re" "model id"; fi


# All profiles sharing a provider or lane use one billing classification and
# one concurrency budget. Built-in overrides must supply missing metadata.
LANES=()             # lane|billing|limit|basis
PROVIDER_LANES=()    # provider id|lane
metered_contains() {
  local part
  local -a parts
  IFS=',' read -r -a parts <<<"$METERED_LANES"
  for part in "${parts[@]}"; do
    part="${part#"${part%%[![:space:]]*}"}"
    part="${part%"${part##*[![:space:]]}"}"
    [[ $part != "$1" ]] || return 0
  done
  return 1
}
known_lane() { # lane -> provider|billing|limit|basis (empty fields need explicit input)
  case $1 in
    "Kimi Code") printf '%s|flat|%s|Request window, not concurrency, is the constraint\n' "$KIMI_PROVIDER" "$KIMI_LIMIT" ;;
    "Z.AI Coding Plan") printf '%s|flat|%s|Tier %s; concurrency is dynamic and higher off-peak\n' "$ZAI_PROVIDER" "$ZAI_LIMIT" "$ZAI_TIER" ;;
    "Qwen Token Plan") printf '%s|flat|%s|Tier %s concurrent-agent allowance\n' "$QWEN_PROVIDER" "$QWEN_LIMIT" "$QWEN_TIER" ;;
    "MiniMax") printf '%s|%s|%s|Billing: %s\n' "$MINIMAX_PROVIDER" "$MINIMAX_TIER" "$MINIMAX_LIMIT" "$MINIMAX_BILLING" ;;
    "ChatGPT subscription") printf '%s|flat|2|Unpublished limits\n' "${GPT_PROVIDER:-openai-subscribed}" ;;
    "Grok subscription") printf '%s|flat|1|Unpublished limits; keep low\n' "${GROK_PROVIDER:-x_ai-subscribed}" ;;
    "GitHub Copilot") printf '%s|flat|1|Premium-request allowance; keep low\n' "$COPILOT_PROVIDER" ;;
    "Local inference") printf '%s|flat|%s|Throughput of the local server\n' "$LOCAL_PROVIDER" "$LOCAL_LIMIT" ;;
    "DeepSeek API"|"OpenCode Zen") printf '|metered||Explicit API concurrency limit\n' ;;
    *) return 1 ;;
  esac
}
register_lane() { # lane billing limit basis
  local entry name billing limit
  if [[ $2 == flat ]] && metered_contains "$1"; then die "lane '$1' is flat but METERED_LANES declares it metered"; fi
  for entry in ${LANES[@]+"${LANES[@]}"}; do
    IFS='|' read -r name billing limit _ <<<"$entry"
    if [[ $name == "$1" ]]; then
      [[ $billing == "$2" && $limit == "$3" ]] || die "lane '$1' has conflicting billing or limits; all profiles on a lane must agree"
      return
    fi
  done
  LANES+=("$1|$2|$3|$4")
}
register_provider() { # provider id lane
  local entry provider lane
  check provider "$1" "$id_re" "Delta provider id"
  for entry in ${PROVIDER_LANES[@]+"${PROVIDER_LANES[@]}"}; do
    IFS='|' read -r provider lane <<<"$entry"
    if [[ $provider == "$1" ]]; then
      [[ $lane == "$2" ]] || die "provider '$1' is assigned to both '$lane' and '$2'; use one shared lane"
      return
    fi
  done
  PROVIDER_LANES+=("$1|$2")
}
add_known_lane() {
  local metadata provider billing limit basis
  metadata="$(known_lane "$1")"
  IFS='|' read -r provider billing limit basis <<<"$metadata"
  register_lane "$1" "$billing" "$limit" "$basis"
  register_provider "$provider" "$1"
}
for lane in "Kimi Code" "Z.AI Coding Plan" "Qwen Token Plan"; do add_known_lane "$lane"; done
[[ -z $MINIMAX_PROVIDER ]] || add_known_lane "MiniMax"
[[ -z $GPT_PROVIDER ]] || add_known_lane "ChatGPT subscription"
[[ -z $GROK_PROVIDER ]] || add_known_lane "Grok subscription"
[[ -z $COPILOT_PROVIDER ]] || add_known_lane "GitHub Copilot"
[[ -z $LOCAL_PROVIDER ]] || add_known_lane "Local inference"

resolve_builtin() {
  local prefix="BUILTIN_$1" lane_key provider_key billing_key limit_key
  local lane provider billing limit metadata known_provider known_billing known_limit basis
  lane_key="${prefix}_LANE"; provider_key="${prefix}_PROVIDER"
  billing_key="${prefix}_BILLING"; limit_key="${prefix}_LIMIT"
  lane="${!lane_key}"; provider="${!provider_key:-}"
  billing="${!billing_key:-}"; limit="${!limit_key:-}"
  if metadata="$(known_lane "$lane")"; then
    IFS='|' read -r known_provider known_billing known_limit basis <<<"$metadata"
    [[ -z $billing || $billing == "$known_billing" ]] || die "$billing_key conflicts with known billing for '$lane' ($known_billing)"
    billing="$known_billing"
    if [[ -n $known_limit ]]; then
      [[ -z $limit || $limit == "$known_limit" ]] || die "$limit_key conflicts with the shared limit for '$lane' ($known_limit); update the lane's setting instead"
      limit="$known_limit"
    fi
    provider="${provider:-$known_provider}"
  else
    basis="Explicit built-in override budget"
    [[ -n $billing ]] || die "unknown $lane_key='$lane'; set $billing_key=flat|metered, $provider_key and $limit_key"
  fi
  [[ -n $provider ]] || die "$lane_key='$lane' needs $provider_key set to its actual Delta provider id"
  [[ -n $limit ]] || die "$lane_key='$lane' needs an explicit positive $limit_key"
  case "$billing" in flat|metered) ;; *) die "$billing_key must be flat or metered" ;; esac
  check "$limit_key" "$limit" "$int_re" "positive integer"
  register_lane "$lane" "$billing" "$limit" "$basis"
  register_provider "$provider" "$lane"
  printf -v "$provider_key" '%s' "$provider"
  printf -v "$billing_key" '%s' "$billing"
  printf -v "$limit_key" '%s' "$limit"
}
for role in SCOUT WORKER REVIEWER; do resolve_builtin "$role"; done

# ---------------------------------------------------------------- paths ----

delta_config_dir() {
  if [[ -n ${DELTA_CONFIG_DIR:-} ]]; then printf '%s\n' "$DELTA_CONFIG_DIR"; return; fi
  case "$(uname -s)" in
    Darwin)               printf '%s\n' "$HOME/Library/Application Support/delta" ;;
    Linux)                printf '%s\n' "${XDG_CONFIG_HOME:-$HOME/.config}/delta" ;;
    MINGW*|MSYS*|CYGWIN*) [[ -n ${APPDATA:-} ]] || die "APPDATA is not set"
                          printf '%s\n' "$APPDATA/delta" ;;
    *)                    die "unsupported OS; set DELTA_CONFIG_DIR" ;;
  esac
}
# Resolve installation roots component by component before .. so a symlinked
# parent keeps its filesystem meaning. Destinations below them are never resolved:
# dangling profile/rule links remain entries for preflight and link backups.
canonical_dir() {
  local path="$1" label="$2" resolved="" rest part candidate
  if [[ $path =~ ^[A-Za-z]:[\\/] || $path == \\\\* ]]; then
    command -v cygpath >/dev/null 2>&1 || die "Windows $label paths need cygpath; use an absolute POSIX path"
    path="$(cygpath -u -- "$path")" || die "could not convert $label directory"
  fi
  [[ $path == /* ]] || path="$PWD/$path"
  [[ $path != //* ]] || resolved=/
  rest="$path"
  while [[ -n $rest ]]; do
    rest="${rest#/}"; part="${rest%%/*}"
    if [[ $rest == */* ]]; then rest="${rest#*/}"; else rest=""; fi
    case $part in
      ""|.) ;;
      ..) resolved="${resolved%/*}" ;;
      *)
        candidate="$resolved/$part"
        if [[ -d $candidate ]]; then resolved="$(cd -- "$candidate" && pwd -P)"
        elif [[ -e $candidate || -L $candidate ]]; then die "$label path component is not a directory: $candidate"
        else resolved="$candidate"
        fi ;;
    esac
  done
  printf '%s\n' "${resolved:-/}"
}
CONFIG_DIR="$(canonical_dir "$(delta_config_dir)" "Delta config")"
SKILL_ROOT="$(canonical_dir "$SKILL_ROOT" "skill")"
# Preserve the tools preflight actually accepted, plus their runtime/build tools.
# Do not import unrelated ambient PATH entries into desktop rules.
TOOL_PATH=""
add_tool_dir() {
  local directory="$1"
  [[ $directory != *$'\n'* && $directory != *$'\r'* && $directory != *:* ]] \
    || die "tool directory must not contain line breaks or colons"
  directory="$(canonical_dir "$directory" "tool")"
  [[ $directory != *$'\n'* && $directory != *$'\r'* && $directory != *:* ]] \
    || die "resolved tool directory must not contain line breaks or colons"
  [[ ":$TOOL_PATH:" != *":$directory:"* ]] || return 0
  TOOL_PATH="${TOOL_PATH:+$TOOL_PATH:}$directory"
}
add_tool_dir "$TOOL_BIN"
for tool in "${REQUIRED_TOOLS[@]}" npm uv cargo brew; do
  if tool_command="$(command -v "$tool")"; then add_tool_dir "$(dirname -- "$tool_command")"; fi
done
PROFILES_DIR="$CONFIG_DIR/profiles"
[[ -f $CONFIG_DIR/settings.json ]] \
  || ((CONFIGURE_DELTA)) || die "no settings.json in $CONFIG_DIR; initialize Delta or use --configure-delta"
SETTINGS_ARGS=("$CONFIG_DIR/settings.json" --thread-cap "$THREAD_CAP")
if ((CONFIGURE_DELTA)); then
  SETTINGS_ARGS+=(--provider "kimi=$KIMI_PROVIDER" --provider "zai=$ZAI_PROVIDER" --provider "qwen=$QWEN_PROVIDER")
  [[ -z $MINIMAX_PROVIDER ]] || SETTINGS_ARGS+=(--provider "minimax=$MINIMAX_PROVIDER")
fi
# Render into /dev/null before planning writes, so malformed inputs cannot leave
# partially updated profiles. Live settings never appear in installer output.
python3 "$SCRIPT_DIR/scripts/setup.py" settings "${SETTINGS_ARGS[@]}" >/dev/null \
  || die "Delta settings preflight failed"
render_builtin() {
  local role="$1" provider="BUILTIN_${1}_PROVIDER" model="BUILTIN_${1}_MODEL" effort="BUILTIN_${1}_EFFORT"
  local -a effort_args=()
  [[ -z ${!effort+x} ]] || effort_args=(--effort "${!effort}")
  python3 "$SCRIPT_DIR/scripts/setup.py" builtin "$PROFILES_DIR/$(printf '%s' "$role" | tr '[:upper:]' '[:lower:]').toml" \
    --model "${!provider}/${!model}" ${effort_args[@]+"${effort_args[@]}"}
}
for role in SCOUT WORKER REVIEWER; do render_builtin "$role" >/dev/null || die "built-in $role preflight failed"; done

# --------------------------------------------------------------- writing ---
# Every file goes through install_file or install_output. While PLANNING is 1
# they only record the destination in PLANNED, so the install can refuse
# before it changes anything.

PLANNING=0
PLANNED=()

backup() { # DEST: save DEST under BACKUP_DIR before it changes
  local saved="$BACKUP_DIR/${1#/}"
  [[ -d $BACKUP_DIR ]] || info "saving the files this install changes under $BACKUP_DIR"
  mkdir -p -- "$(dirname -- "$saved")"
  if [[ $1 == "$CONFIG_DIR/settings.json" ]]; then
    # Keep the link for restoration and snapshot its content privately without
    # changing the original target's permissions. A dangling link has no content.
    [[ ! -L $1 ]] || cp -Pp -- "$1" "$saved.link"
    if [[ -e $1 ]]; then (umask 077; cat -- "$1" > "$saved"); fi
  else
    cp -Pp -- "$1" "$saved"
  fi
}

commit_file() { # TMP DEST: move a finished TMP into place, saving the DEST it changes
  if [[ $2 == "$CONFIG_DIR/settings.json" ]]; then chmod 0600 "$1"; else chmod 0644 "$1"; fi
  if [[ -L $2 ]] || { [[ -e $2 ]] && ! cmp -s -- "$1" "$2"; }; then backup "$2"; fi
  mv -f -- "$1" "$2"
  info "wrote $2"
}

# Escape the two separate languages: literal replacement text for sed, and
# complete POSIX shell arguments for commands embedded in the skill templates.
sed_literal() { printf '%s' "$1" | sed 's/[\\&|]/\\&/g'; }
shell_argument() {
  local rest="$1" head
  printf "'"
  while [[ $rest == *"'"* ]]; do
    head="${rest%%\'*}"
    printf '%s' "$head" "'\\''"
    rest="${rest#*\'}"
  done
  printf "%s'" "$rest"
}

# install_file SRC DEST [render [SED_EXPR...]]: copy, optionally substituting placeholders.
# Extra sed expressions fill placeholders specific to one rendering.
install_file() {
  local src="$1" dest="$2" mode="${3:-copy}" tmp
  shift "$(( $# > 2 ? 3 : 2 ))"
  if ((PLANNING)); then PLANNED+=("$dest"); return; fi
  if ((DRY_RUN)); then info "would write $dest"; return; fi
  mkdir -p -- "$(dirname -- "$dest")"
  tmp="$(mktemp "${dest}.XXXXXX")"
  if [[ $mode == render ]]; then
    sed -e "s|{{KIMI}}|$KIMI_PROVIDER|g"       -e "s|{{ZAI}}|$ZAI_PROVIDER|g" \
        -e "s|{{QWEN}}|$QWEN_PROVIDER|g"       -e "s|{{MINIMAX}}|$MINIMAX_PROVIDER|g" \
        -e "s|{{GPT}}|$GPT_PROVIDER|g"         -e "s|{{GROK}}|$GROK_PROVIDER|g" \
        -e "s|{{COPILOT}}|$COPILOT_PROVIDER|g" -e "s|{{LOCAL}}|$LOCAL_PROVIDER|g" \
        -e "s|{{LOCAL_MODEL}}|$LOCAL_MODEL|g" \
        -e "s|{{SKILL_DIR}}|$(sed_literal "$SKILL_DIR")|g" \
        -e "s|{{BON_SH}}|$(sed_literal "$(shell_argument "$SKILL_DIR/scripts/bon.sh")")|g" \
        -e "s|{{IDENTITY_SH}}|$(sed_literal "$(shell_argument "$SKILL_DIR/scripts/identity.py")")|g" \
        -e "s|{{IDENTITY_REGISTRY}}|$(sed_literal "$(shell_argument "$SKILL_DIR/references/identity-registry.json")")|g" \
        "$@" -- "$src" > "$tmp"
    if grep -q '{{[A-Z_]*}}' "$tmp"; then rm -f -- "$tmp"; die "unrendered placeholder in $src"; fi
  else
    cp -- "$src" "$tmp"
  fi
  commit_file "$tmp" "$dest"
}

install_output() { # DEST CMD [ARG...]: write CMD's output to DEST
  local dest="$1" tmp
  shift
  if ((PLANNING)); then PLANNED+=("$dest"); return; fi
  if ((DRY_RUN)); then info "would write $dest"; return; fi
  mkdir -p -- "$(dirname -- "$dest")"
  tmp="$(mktemp "${dest}.XXXXXX")"
  "$@" > "$tmp"
  commit_file "$tmp" "$dest"
}

profile() { install_file "$SCRIPT_DIR/profiles/$1.toml.tmpl" "$PROFILES_DIR/$1.toml" render; }

# --------------------------------------------------------------- profiles --

# Delta's spawn tool offers the three built-ins, then at most seven custom
# profiles in alphabetical order, and silently drops the rest. Every custom
# profile file in the folder takes a slot, ours or not.
readonly CUSTOM_SLOTS=7
readonly BUILTIN_PROFILES=" worker scout reviewer "
# Primary families before duplicate models/scouts; still at most seven slots.
readonly PRIORITY="qwen-max deepseek-pro grok gpt-sol minimax scout-gemini scout-local gpt-astra scout-qwen scout-deepseek"
# Superseded: v1's profiles (replaced by the built-ins) and v4's role profiles
# (replaced by one profile per model). --prune-legacy retires them.
LEGACY_PROFILES=" scout-fast scout-deep worker-kimi reviewer-glm"
LEGACY_PROFILES+=" worker-qwen worker-minimax worker-gpt worker-grok reviewer-deepseek reviewer-gpt reviewer-grok"
LEGACY_PROFILES+=" candidate candidate-kimi candidate-glm candidate-qwen candidate-deepseek candidate-gpt candidate-grok candidate-minimax "
readonly LEGACY_PROFILES

# Install order: PROFILE_PRIORITY's names first, then the rest of PRIORITY.
order=""
for p in $PROFILE_PRIORITY; do
  [[ " $PRIORITY " == *" $p "* ]] || die "PROFILE_PRIORITY: unknown profile '$p' (known: $PRIORITY)"
  [[ " $order " != *" $p "* ]] || die "PROFILE_PRIORITY: duplicate profile '$p'"
  order+=" $p"
done
for p in $PRIORITY; do
  if [[ " $order " != *" $p "* ]]; then order+=" $p"; fi
done

SCOUTS=" scout-qwen scout-deepseek "
[[ -n $COPILOT_PROVIDER ]] && SCOUTS+="scout-gemini "
[[ -n $LOCAL_PROVIDER ]] && SCOUTS+="scout-local "

# One profile per model, rendered from profiles/model.toml.tmpl; the task block
# sets the role. One source for the profiles and the roster. Fields:
# name|provider|model|lane|family|billing|thinking|roles|use (empty thinking: no pin).
MODELS=(
  "qwen-max|$QWEN_PROVIDER|qwen3.8-max|Qwen Token Plan|Qwen|flat|xhigh|worker, candidate|Hardest or most correctness-critical unit (algorithms, concurrency, parsing, numeric code); second implementer family"
  "deepseek-pro|$QWEN_PROVIDER|deepseek-v4-pro|Qwen Token Plan|DeepSeek|flat|max|reviewer, candidate|Reviews GLM work; second reviewer for security-sensitive, concurrency-heavy, or correctness-critical units"
)
if [[ -n $GPT_PROVIDER ]]; then
  MODELS+=("gpt-sol|$GPT_PROVIDER|gpt-6.1-sol|ChatGPT subscription|OpenAI|flat|high|worker, candidate|Hard or correctness-critical units; extra implementer family")
  MODELS+=("gpt-astra|$GPT_PROVIDER|gpt-6-astra|ChatGPT subscription|OpenAI|flat|xhigh|reviewer|Extra reviewer family, or an extra reviewer on critical units")
fi
if [[ -n $GROK_PROVIDER ]]; then
  MODELS+=("grok|$GROK_PROVIDER|grok-4.7|Grok subscription|xAI|flat|xhigh|worker, reviewer, candidate|Extra implementer and reviewer family")
fi
# Metered candidates require explicit user naming and the skill's spending gate.
if [[ -n $MINIMAX_PROVIDER ]]; then
  roles="worker, candidate (explicit metered opt-in only)"
  if [[ $MINIMAX_TIER == flat ]]; then roles="worker, candidate"; fi
  MODELS+=("minimax|$MINIMAX_PROVIDER|MiniMax-M3|MiniMax|MiniMax|$MINIMAX_TIER||$roles|Well-specified, self-contained units; UI work that benefits from screenshots; check the roster's billing first")
fi

model_entry() { # print the MODELS entry named $1
  local m
  for m in "${MODELS[@]}"; do
    if [[ ${m%%|*} == "$1" ]]; then printf '%s\n' "$m"; return 0; fi
  done
  return 1
}

configured() { [[ $SCOUTS == *" $1 "* ]] || model_entry "$1" >/dev/null; }

lane_var() { # the setting that enables an optional profile
  case $1 in
    scout-gemini) echo COPILOT_PROVIDER ;;
    scout-local)  echo LOCAL_PROVIDER ;;
    minimax)      echo MINIMAX_PROVIDER ;;
    gpt-*)        echo GPT_PROVIDER ;;
    grok)         echo GROK_PROVIDER ;;
  esac
}

install_profile() { # $1=profile name: a scout template, or a MODELS entry
  local entry name provider model lane thinking roles use
  if ! entry="$(model_entry "$1")"; then profile "$1"; return; fi
  IFS='|' read -r name provider model lane _ _ thinking roles use <<<"$entry"
  install_file "$SCRIPT_DIR/profiles/model.toml.tmpl" "$PROFILES_DIR/$name.toml" render \
    -e "s|{{PROVIDER}}|$provider|g" -e "s|{{MODEL_ID}}|$model|g" -e "s|{{LANE}}|$lane|g" \
    -e "s|{{ROLES}}|$roles|g"       -e "s|{{USE}}|$use|g" \
    -e "s|{{THINKING}}|$thinking|g" -e '/^thinking_effort = ""$/d'
}

# Profiles this install does not manage take their slots first; with
# --prune-legacy, superseded ones are retired before anything is written.
others="" n_others=0 legacy=""
for f in "$PROFILES_DIR"/*.toml; do
  [[ -e $f || -L $f ]] || continue
  n="$(basename -- "$f" .toml)"
  if [[ $BUILTIN_PROFILES == *" $n "* || " $PRIORITY " == *" $n "* ]]; then continue; fi
  if [[ $LEGACY_PROFILES == *" $n "* ]]; then
    if ((PRUNE_LEGACY)); then continue; fi  # retired below (or would be, in a dry run)
    legacy+=" $n"
  fi
  others+=" $n"
  n_others=$((n_others + 1))
done

((n_others <= CUSTOM_SLOTS)) || die "$n_others unmanaged custom profiles exceed Delta's $CUSTOM_SLOTS slots; retire some before installing"

INSTALLED=" " n_installed=0 skipped=""
for p in $order; do
  if ! configured "$p"; then
    if [[ -e $PROFILES_DIR/$p.toml ]]; then
      warn "$p.toml is installed but $(lane_var "$p") is not set; the roster omits it, so the orchestrator will not use it"
    fi
    continue
  fi
  if ((n_others + n_installed < CUSTOM_SLOTS)); then
    INSTALLED+="$p "
    n_installed=$((n_installed + 1))
  else
    skipped+=" $p"
  fi
done

if [[ -n $legacy ]]; then
  warn "superseded profiles still installed:$legacy; each takes one of Delta's $CUSTOM_SLOTS custom slots, so retire them with --prune-legacy"
fi
if [[ -n $skipped ]]; then
  warn "Delta offers only $CUSTOM_SLOTS custom profiles and $n_others other profiles take slots${others:+ (${others# })}; not installed:$skipped. Free slots, or reorder with PROFILE_PRIORITY"
fi

# The final active directory determines Delta's offered set. Retire earlier
# managed selections that no longer belong to this install, preserving contents
# under .retired and backing up entries before a forced update.
RETIRE=()
for p in $PRIORITY; do
  f="$PROFILES_DIR/$p.toml"
  if [[ $INSTALLED != *" $p "* && ( -e $f || -L $f ) ]]; then RETIRE+=("$f"); fi
done
if ((PRUNE_LEGACY)); then
  for p in $LEGACY_PROFILES; do
    f="$PROFILES_DIR/$p.toml"
    [[ ! -e $f && ! -L $f ]] || RETIRE+=("$f")
  done
fi

# ----------------------------------------------------------------- roster --

metered_list="$METERED_LANES"
for entry in ${LANES[@]+"${LANES[@]}"}; do
  IFS='|' read -r lane billing _ _ <<<"$entry"
  if [[ $billing == metered ]] && ! metered_contains "$lane"; then metered_list="$metered_list, $lane"; fi
done

BUILTIN_WORKER_ROLES="worker, candidate"
BUILTIN_REVIEWER_ROLES="reviewer, candidate"
[[ $BUILTIN_WORKER_BILLING != metered ]] || BUILTIN_WORKER_ROLES="worker, candidate (explicit metered opt-in only)"
[[ $BUILTIN_REVIEWER_BILLING != metered ]] || BUILTIN_REVIEWER_ROLES="reviewer, candidate (explicit metered opt-in only)"

MODEL_FAMILIES=() # exact provider id|model id|family bindings
register_model() { # provider id model id family
  local entry provider model family
  for entry in ${MODEL_FAMILIES[@]+"${MODEL_FAMILIES[@]}"}; do
    IFS='|' read -r provider model family <<<"$entry"
    if [[ $provider == "$1" && $model == "$2" ]]; then
      [[ $family == "$3" ]] || die "provider/model '$1/$2' has conflicting families '$family' and '$3'; set consistent metadata"
      return
    fi
  done
  MODEL_FAMILIES+=("$1|$2|$3")
}
register_model "$BUILTIN_SCOUT_PROVIDER" "$BUILTIN_SCOUT_MODEL" "$BUILTIN_SCOUT_FAMILY"
register_model "$BUILTIN_WORKER_PROVIDER" "$BUILTIN_WORKER_MODEL" "$BUILTIN_WORKER_FAMILY"
register_model "$BUILTIN_REVIEWER_PROVIDER" "$BUILTIN_REVIEWER_MODEL" "$BUILTIN_REVIEWER_FAMILY"
register_model "$QWEN_PROVIDER" qwen3.8-flash Qwen
register_model "$QWEN_PROVIDER" deepseek-v4.1-flash DeepSeek
[[ -z $COPILOT_PROVIDER ]] || register_model "$COPILOT_PROVIDER" gemini-3.8-flash Gemini
[[ -z $LOCAL_PROVIDER ]] || register_model "$LOCAL_PROVIDER" "$LOCAL_MODEL" "$LOCAL_FAMILY"
for entry in "${MODELS[@]}"; do
  IFS='|' read -r _ provider model _ family _ _ _ _ <<<"$entry"
  register_model "$provider" "$model" "$family"
done

lane_metadata() {
  local entry name
  for entry in ${LANES[@]+"${LANES[@]}"}; do
    name="${entry%%|*}"
    if [[ $name == "$1" ]]; then printf '%s\n' "$entry"; return; fi
  done
  return 1
}
generate_identity_registry() {
  local entry provider lane billing limit model family metadata comma=""
  printf '{"version":1,"thread_limit":%s,"metered_max_spawns":%s,"providers":[' "$THREAD_CAP" "$METERED_MAX_SPAWNS"
  for entry in ${PROVIDER_LANES[@]+"${PROVIDER_LANES[@]}"}; do
    IFS='|' read -r provider lane <<<"$entry"
    metadata="$(lane_metadata "$lane")"
    IFS='|' read -r _ billing limit _ <<<"$metadata"
    printf '%s{"provider_id":"%s","lane":"%s","billing":"%s","limit":%s}' "$comma" "$provider" "$lane" "$billing" "$limit"
    comma=,
  done
  printf '],"models":['; comma=""
  for entry in ${MODEL_FAMILIES[@]+"${MODEL_FAMILIES[@]}"}; do
    IFS='|' read -r provider model family <<<"$entry"
    printf '%s{"provider_id":"%s","model_id":"%s","family":"%s"}' "$comma" "$provider" "$model" "$family"
    comma=,
  done
  printf ']}\n'
}
identity_registry="$(generate_identity_registry)"

installed() { [[ $INSTALLED == *" $1 "* ]]; }

generate_roster() {
  local o m name provider model lane family billing roles use entry limit basis metadata
  printf '# Roster\n\nGenerated by install.sh on %s. Authoritative for this machine.\n' "$(date -u +%Y-%m-%dT%H:%MZ)"
  printf 'Regenerate with install.sh --force after changing plans or built-in models.\n\n'

  printf '## This thread\n\n'
  printf 'Orchestrator model: %s via %s. Family: %s. Never switch it mid-thread.\n' \
    "$ORCHESTRATOR_MODEL" "$ORCHESTRATOR_LANE" "$ORCHESTRATOR_FAMILY"
  printf 'This thread draws on its own lane too; leave headroom there.\n\n'

  printf '## Profiles\n\n'
  printf 'A profile takes only the roles listed here; the block you send sets the role, and each skill says which of its blocks each role may take.\n\n'
  printf '| Spawn as | Provider ID | Model | Lane | Family | Billing | Worktree | Roles | Use for |\n'
  printf '|---|---|---|---|---|---|---|---|---|\n'
  printf '| Scout (built-in) | %s | %s | %s | %s | %s | shared | scout | Default read-only recon |\n' \
    "$BUILTIN_SCOUT_PROVIDER" "$BUILTIN_SCOUT_MODEL" "$BUILTIN_SCOUT_LANE" "$BUILTIN_SCOUT_FAMILY" "$BUILTIN_SCOUT_BILLING"
  installed scout-qwen     && printf '| scout-qwen | %s | qwen3.8-flash | Qwen Token Plan | Qwen | flat | shared | scout | Quick recon off the Z.AI lane |\n' "$QWEN_PROVIDER"
  installed scout-deepseek && printf '| scout-deepseek | %s | deepseek-v4.1-flash | Qwen Token Plan | DeepSeek | flat | shared | scout | Recon needing more reasoning, off the Z.AI lane |\n' "$QWEN_PROVIDER"
  installed scout-gemini   && printf '| scout-gemini | %s | gemini-3.8-flash | GitHub Copilot | Gemini | flat | shared | scout | Recon outside the coding-plan quotas |\n' "$COPILOT_PROVIDER"
  installed scout-local    && printf '| scout-local | %s | %s | Local inference | %s | flat (no quota) | shared | scout | Bulk or background recon; slower |\n' "$LOCAL_PROVIDER" "$LOCAL_MODEL" "$LOCAL_FAMILY"
  printf '| Worker (built-in) | %s | %s | %s | %s | %s | isolated | %s | Default implementer |\n' \
    "$BUILTIN_WORKER_PROVIDER" "$BUILTIN_WORKER_MODEL" "$BUILTIN_WORKER_LANE" "$BUILTIN_WORKER_FAMILY" "$BUILTIN_WORKER_BILLING" "$BUILTIN_WORKER_ROLES"
  printf '| Reviewer (built-in) | %s | %s | %s | %s | %s | isolated | %s | Default reviewer |\n' \
    "$BUILTIN_REVIEWER_PROVIDER" "$BUILTIN_REVIEWER_MODEL" "$BUILTIN_REVIEWER_LANE" "$BUILTIN_REVIEWER_FAMILY" "$BUILTIN_REVIEWER_BILLING" "$BUILTIN_REVIEWER_ROLES"
  for m in "${MODELS[@]}"; do
    IFS='|' read -r name provider model lane family billing _ roles use <<<"$m"
    if installed "$name"; then
      printf '| %s | %s | %s | %s | %s | %s | isolated | %s | %s |\n' "$name" "$provider" "$model" "$lane" "$family" "$billing" "$roles" "$use"
    fi
  done
  printf '\n'

  printf '## Budgets (subagents in flight at once, this thread)\n\n'
  printf '| Lane | Limit | Basis |\n|---|---|---|\n'
  for entry in ${LANES[@]+"${LANES[@]}"}; do
    IFS='|' read -r lane _ limit basis <<<"$entry"
    printf '| %s | %s | %s |\n' "$lane" "$limit" "$basis"
  done
  printf '| **All lanes** | **%s** | Delta Max Agents Per Thread |\n\n' "$THREAD_CAP"
  printf 'Budgets count spawns by the lane they run on, whatever the profile.\n'
  printf 'These budgets are per orchestrator thread. Orchestrator threads do not see each\n'
  printf 'other, so two running at once can together exceed a plan'"'"'s limits. When another\n'
  printf 'orchestrator thread is active, use half of each limit (rounded down, minimum 1)\n'
  printf 'unless a shared admission proxy enforces the limits across threads.\n\n'

  printf '## Provider lanes\n\n'
  printf '| Provider ID | Lane | Billing | Limit |\n|---|---|---|---|\n'
  for entry in ${PROVIDER_LANES[@]+"${PROVIDER_LANES[@]}"}; do
    IFS='|' read -r provider lane <<<"$entry"
    metadata="$(lane_metadata "$lane")"
    IFS='|' read -r _ billing limit _ <<<"$metadata"
    printf '| %s | %s | %s | %s |\n' "$provider" "$lane" "$billing" "$limit"
  done
  printf '\nExact configured model-family bindings and provider metadata are also in identity-registry.json; use effective-identity.md for an actual model override.\n\n'

  printf '## Metered lanes\n\n'
  printf 'Metered: %s. Allowance: %s metered spawns per run, under SKILL.md invariant 7.\n' \
    "$metered_list" "$METERED_MAX_SPAWNS"
  printf 'Route around metered duplicates: GLM via Z.AI, DeepSeek via the Qwen plan, Kimi via Kimi Code.\n\n'

  printf '## Reviewer preference order\n\n'
  printf 'Pick the first reviewer whose family differs from the author'"'"'s family:\n\n'
  o=1
  printf '%s. Reviewer (built-in): %s\n' "$o" "$BUILTIN_REVIEWER_FAMILY"; o=$((o+1))
  for m in "${MODELS[@]}"; do
    IFS='|' read -r name _ _ _ family _ _ roles _ <<<"$m"
    if installed "$name" && [[ $roles == *reviewer* ]]; then printf '%s. %s: %s\n' "$o" "$name" "$family"; o=$((o+1)); fi
  done
  printf '\n## Best-of-N candidates (flat lanes)\n\n'
  printf '%s\n\n' "Pinned candidates use no model override; user-named unprofiled overrides follow effective-identity.md and the skill's candidate block."
  printf '| Spawn as | Model | Lane | Family |\n|---|---|---|---|\n'
  [[ $BUILTIN_WORKER_BILLING != flat ]] || printf '| Worker (built-in) | %s | %s | %s |\n' "$BUILTIN_WORKER_MODEL" "$BUILTIN_WORKER_LANE" "$BUILTIN_WORKER_FAMILY"
  [[ $BUILTIN_REVIEWER_BILLING != flat ]] || printf '| Reviewer (built-in) | %s | %s | %s |\n' "$BUILTIN_REVIEWER_MODEL" "$BUILTIN_REVIEWER_LANE" "$BUILTIN_REVIEWER_FAMILY"
  for m in "${MODELS[@]}"; do
    IFS='|' read -r name _ model lane family billing _ roles _ <<<"$m"
    if installed "$name" && [[ $billing == flat && $roles == *candidate* ]]; then
      printf '| %s | %s | %s | %s |\n' "$name" "$model" "$lane" "$family"
    fi
  done
  return 0
}

roster="$(generate_roster)" # generated once, so every skill's copy is identical

# ------------------------------------------------------------------ rules --
# Personal rules = context router + Ponytail's always-on text, generated from
# the installed plugin so a reinstall picks up Ponytail updates. They go into
# Delta's Personal AGENTS.md, which Delta re-reads at the start of every turn.

readonly RULES_FILE="$HOME/.config/delta/AGENTS.md"

PONYTAIL_DIR="${PONYTAIL_DIR:-}"
if [[ -z $PONYTAIL_DIR ]]; then
  pt_base="$HOME/.codex/plugins/cache/ponytail/ponytail"
  if [[ -d $pt_base ]]; then
    PONYTAIL_DIR="$(find "$pt_base" -mindepth 1 -maxdepth 1 -type d | sort -V | tail -n 1)"
  fi
fi
# Fresh Delta installations need no Codex plugin cache for these rules.
if [[ ! -f $PONYTAIL_DIR/AGENTS.md ]]; then PONYTAIL_DIR="$SCRIPT_DIR/rules/ponytail"; fi

ponytail_block() { # adapt ponytail's AGENTS.md to Delta and the context router
  local src="$1"
  if ! command -v python3 >/dev/null 2>&1; then
    warn "python3 not found; including ponytail text unadapted"
    cat -- "$src"; return
  fi
  python3 - "$src" <<'PY'
import sys
text = open(sys.argv[1], encoding="utf-8").read()
out = []
for line in text.splitlines():
    if line.startswith("# Ponytail"):
        out.append("### Simplicity (Ponytail, full level)")
        continue
    if line.startswith("(Yes, this file also applies"):
        continue
    out.append(line)
body = "\n".join(out).rstrip() + "\n"
old = "Grep every caller of the function you touch"
new = ("Find every caller of the function you touch "
       "(codegraph where the language is covered, otherwise tgrep)")
if old in body:
    body = body.replace(old, new)
else:
    print("install.sh: warning: ponytail caller sentence changed upstream; kept verbatim",
          file=sys.stderr)
sys.stdout.write(body)
PY
}

ponytail_version() { # the plugin's own version; its cache folder's name can differ
  local v=""
  if [[ -f $PONYTAIL_DIR/package.json ]]; then
    v="$(awk -F'"' '/^[[:space:]]*"version"[[:space:]]*:/ { print $4; exit }' "$PONYTAIL_DIR/package.json")"
  fi
  printf '%s\n' "${v:-$(basename -- "$PONYTAIL_DIR")}"
}

generate_rules() {
  sed "s|{{TOOL_PATH_SH}}|$(sed_literal "$(shell_argument "$TOOL_PATH")")|g" "$SCRIPT_DIR/rules/personal-AGENTS.md"
  if [[ -n $PONYTAIL_DIR && -f $PONYTAIL_DIR/AGENTS.md ]]; then
    printf '\n<!-- PONYTAIL_START %s -->\n' "$(ponytail_version)"
    ponytail_block "$PONYTAIL_DIR/AGENTS.md"
    printf '<!-- PONYTAIL_END -->\n'
  fi
}

# The blocks this install owns in RULES_FILE: the router, and Ponytail when found
# (an earlier Ponytail block stays when it is not).
rules_blocks="DELTA_CONTEXT_ROUTER"
if [[ -n $PONYTAIL_DIR && -f $PONYTAIL_DIR/AGENTS.md ]]; then
  rules_blocks+="|PONYTAIL"
else
  warn "ponytail not found; the rules contain the context router only (set PONYTAIL_DIR)"
fi
readonly rules_blocks
# A block missing its START or END line would make the rest of the file look
# like part of it; refuse rather than drop the user's own rules.
if [[ -f $RULES_FILE ]]; then
  awk '
    /^<!-- (DELTA_CONTEXT_ROUTER|PONYTAIL)_START/ {
      name = $0; sub(/^<!-- /, "", name); sub(/_START.*/, "", name)
      if (active != "") { bad = 1; print "nested or overlapping " name "_START marker" > "/dev/stderr"; exit 1 }
      active = name; next
    }
    /^<!-- (DELTA_CONTEXT_ROUTER|PONYTAIL)_END -->/ {
      name = $0; sub(/^<!-- /, "", name); sub(/_END.*/, "", name)
      if (active != name) { bad = 1; print name "_END line without its matching " name "_START line" > "/dev/stderr"; exit 1 }
      active = ""
    }
    END {
      if (!bad && active != "") { print active "_START line without its " active "_END line" > "/dev/stderr"; exit 1 }
    }
  ' "$RULES_FILE" || die "$RULES_FILE has malformed block markers; fix that by hand first"
fi

personal_rules() { # our blocks, then whatever else RULES_FILE already holds
  local rest=""
  generate_rules
  if [[ -f $RULES_FILE ]]; then
    rest="$(awk -v s="^<!-- ($rules_blocks)_START" -v e="^<!-- ($rules_blocks)_END -->" \
      '$0 ~ s { skip = 1 } !skip && (NF || kept) { kept = 1; print } $0 ~ e { skip = 0 }' "$RULES_FILE")"
  fi
  if [[ -n $rest ]]; then printf '\n%s\n' "$rest"; fi
}

# ---------------------------------------------------------------- install --
# Every file the install writes, in order. Each skill is self-contained under
# $SKILL_ROOT with its own copy of the roster; install_file renders
# {{SKILL_DIR}} from SKILL_DIR.

readonly SKILLS="orchestrate adversarial isolated pr-review"

write_bundle() {
  local p skill SKILL_DIR="$SKILL_ROOT/orchestrate"
  if ((CONFIGURE_DELTA)); then
    install_output "$CONFIG_DIR/settings.json" python3 "$SCRIPT_DIR/scripts/setup.py" settings "${SETTINGS_ARGS[@]}"
  fi
  for p in SCOUT WORKER REVIEWER; do
    install_output "$PROFILES_DIR/$(printf '%s' "$p" | tr '[:upper:]' '[:lower:]').toml" render_builtin "$p"
  done
  for p in $INSTALLED; do install_profile "$p"; done
  install_file "$SCRIPT_DIR/skills/orchestrate/SKILL.md" "$SKILL_DIR/SKILL.md" render
  install_file "$SCRIPT_DIR/skills/orchestrate/references/best-of-n.md" \
               "$SKILL_DIR/references/best-of-n.md" render
  install_file "$SCRIPT_DIR/skills/orchestrate/scripts/bon.sh" "$SKILL_DIR/scripts/bon.sh"
  SKILL_DIR="$SKILL_ROOT/adversarial"
  install_file "$SCRIPT_DIR/skills/adversarial/SKILL.md" "$SKILL_DIR/SKILL.md" render
  SKILL_DIR="$SKILL_ROOT/isolated"
  install_file "$SCRIPT_DIR/skills/isolated/SKILL.md" "$SKILL_DIR/SKILL.md" render
  install_file "$SCRIPT_DIR/skills/orchestrate/scripts/bon.sh" "$SKILL_DIR/scripts/bon.sh"
  SKILL_DIR="$SKILL_ROOT/pr-review"
  install_file "$SCRIPT_DIR/skills/pr-review/SKILL.md" "$SKILL_DIR/SKILL.md" render
  for skill in $SKILLS; do
    SKILL_DIR="$SKILL_ROOT/$skill"
    install_output "$SKILL_DIR/references/roster.md" printf '%s\n' "$roster"
    install_output "$SKILL_DIR/references/identity-registry.json" printf '%s\n' "$identity_registry"
    install_file "$SCRIPT_DIR/skills/orchestrate/scripts/identity.py" "$SKILL_DIR/scripts/identity.py"
    install_file "$SCRIPT_DIR/skills/orchestrate/references/effective-identity.md" "$SKILL_DIR/references/effective-identity.md" render
  done
  install_output "$RULES_FILE" personal_rules
}

# Plan first: without --force, refuse before anything changes.
PLANNING=1
write_bundle
PLANNING=0
existing=""
for f in "${PLANNED[@]}" ${RETIRE[@]+"${RETIRE[@]}"}; do
  if [[ -e $f || -L $f ]]; then existing+=$'\n  '"$f"; fi
done
if [[ -n $existing ]] && ((!FORCE)); then
  die "these files exist; rerun with --force to replace them (it saves each one it changes first):$existing"
fi

for f in ${RETIRE[@]+"${RETIRE[@]}"}; do
  if ((DRY_RUN)); then info "would retire $f"; continue; fi
  backup "$f"
  if [[ -L $f.retired ]] || { [[ -e $f.retired ]] && ! cmp -s -- "$f" "$f.retired"; }; then backup "$f.retired"; fi
  mv -f -- "$f" "$f.retired"
  info "retired $f -> $f.retired"
done

write_bundle
if ((DRY_RUN)); then
  info "roster:"
  printf '%s\n' "$roster" | sed 's/^/  | /'
else
  rules_bytes=$(wc -c < "$RULES_FILE" | tr -d ' ')
  info "$RULES_FILE: $rules_bytes bytes"
  # Delta re-sends these rules every turn and shows no size cap; the budget is ours.
  ((rules_bytes <= 10240)) || warn "Personal AGENTS.md is $rules_bytes bytes, over the 10240-byte budget; trim before adding more"
fi
[[ ! -d $BACKUP_DIR ]] || info "the files this install changed were saved under $BACKUP_DIR"

info ""
info "Next:"
info "  1. Built-in Scout, Worker, and Reviewer models now match the roster."
info "     Start Delta and confirm every selected profile loaded without errors."
if ((CONFIGURE_DELTA)); then
  info "     Provider/model metadata and subagent limits are configured; authenticate"
  info "     coding-plan providers and subscription accounts locally in Delta."
else
  info "     Set Delta's subagent limits to THREAD_CAP=$THREAD_CAP (overall $((2 * THREAD_CAP))),"
  info "     or use --configure-delta to set limits and bundled provider metadata."
fi
info "     Keep provider Model Preferences on Global so profiles retain their models."
info "     PR targets require local GitHub authentication: gh auth login."
info "  2. Nothing to paste: Delta re-reads $RULES_FILE"
info "     (Settings > Rules > Personal AGENTS.md) at the start of each turn."
[[ ! -e $RULES_OUT ]] || info "     $RULES_OUT from an earlier install is no longer used; install.sh --clean removes it."
}
