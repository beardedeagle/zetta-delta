#!/usr/bin/env bash
# Install the Delta orchestration bundle: custom subagent profiles, the
# /orchestrate skill, and a roster generated for this machine's accounts.
# Also generates personal-AGENTS.generated.md (context router plus Ponytail)
# in $XDG_STATE_HOME/zetta-delta (default ~/.local/state/zetta-delta) for
# pasting into Settings > Rules > Personal AGENTS.md; --clean removes it.
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
#   GPT_PROVIDER         ChatGPT subscription -> gpt-sol (worker, candidate), gpt-astra (reviewer)
#   GROK_PROVIDER        Grok subscription    -> grok (worker, reviewer, candidate)
#   COPILOT_PROVIDER     GitHub Copilot       -> scout-gemini
#   LOCAL_PROVIDER       plus LOCAL_MODEL (local inference) -> scout-local
# Delta offers at most 7 custom profiles. The installer installs them in this
# order, after counting every other profile already installed, and names the rest:
#   scout-qwen scout-deepseek qwen-max deepseek-pro grok gpt-sol minimax
#   gpt-astra scout-gemini scout-local
# Optional tuning:
#   PROFILE_PRIORITY="<names>"  profiles to install first; the rest keep the order above
#   KIMI_LIMIT=3  LOCAL_LIMIT=2  THREAD_CAP=6  METERED_MAX_SPAWNS=2
#   METERED_LANES="DeepSeek API, OpenCode Zen"
#   ORCHESTRATOR_MODEL=k3  ORCHESTRATOR_LANE="Kimi Code"  ORCHESTRATOR_FAMILY=Kimi
#   BUILTIN_{SCOUT,WORKER,REVIEWER}_{MODEL,LANE,FAMILY}  (see README)
#   DELTA_CONFIG_DIR     overrides Delta's config directory (same as Delta)
#   PONYTAIL_DIR         ponytail plugin version dir (default: newest under
#                        ~/.codex/plugins/cache/ponytail/ponytail/)
#   ZETTA_DELTA_REF      commit to fetch the bundle at when none is next to this
#                        script (curl | bash); there is no default
#
# Usage: install.sh [--force] [--dry-run] [--prune-legacy] [--skill-dir DIR] [--clean]
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
SKILL_ROOT="${HOME}/.agents/skills"

while (($#)); do
  case "$1" in
    --force)        FORCE=1 ;;
    --dry-run)      DRY_RUN=1 ;;
    --prune-legacy) PRUNE_LEGACY=1 ;;
    --skill-dir)    [[ $# -ge 2 ]] || die "--skill-dir needs a value"; SKILL_ROOT="$2"; shift ;;
    --clean)        CLEAN=1 ;;
    -h|--help)      HELP=1 ;;
    *)              die "unknown argument: $1 (see --help)" ;;
  esac
  shift
done

# The generated rules wait here until pasted into Delta; --clean removes them.
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/zetta-delta"
[[ $STATE_DIR == /* ]] || die "XDG_STATE_HOME must be an absolute path"
readonly STATE_DIR
if ((CLEAN && !HELP)); then
  if [[ ! -e $STATE_DIR ]]; then info "nothing to remove: $STATE_DIR does not exist"
  elif ((DRY_RUN)); then info "would remove $STATE_DIR"
  else rm -rf -- "$STATE_DIR"; info "removed $STATE_DIR"
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
BUILTIN_WORKER_MODEL="${BUILTIN_WORKER_MODEL:-kimi-for-coding}"
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
CONFIG_DIR="$(delta_config_dir)"
PROFILES_DIR="$CONFIG_DIR/profiles"
SKILL_DIR="$SKILL_ROOT/orchestrate"
[[ -f $CONFIG_DIR/settings.json ]] \
  || warn "no settings.json in $CONFIG_DIR; is that Delta's config directory?"

# --------------------------------------------------------------- writing ---

# install_file SRC DEST [render [SED_EXPR...]]: copy, optionally substituting placeholders.
# Extra sed expressions fill placeholders specific to one rendering.
install_file() {
  local src="$1" dest="$2" mode="${3:-copy}" tmp
  shift "$(( $# > 2 ? 3 : 2 ))"
  if [[ -e $dest && $FORCE -ne 1 ]]; then die "$dest exists; rerun with --force to overwrite"; fi
  if ((DRY_RUN)); then info "would write $dest"; return; fi
  mkdir -p -- "$(dirname -- "$dest")"
  tmp="$(mktemp "${dest}.XXXXXX")"
  if [[ $mode == render ]]; then
    sed -e "s|{{KIMI}}|$KIMI_PROVIDER|g"       -e "s|{{ZAI}}|$ZAI_PROVIDER|g" \
        -e "s|{{QWEN}}|$QWEN_PROVIDER|g"       -e "s|{{MINIMAX}}|$MINIMAX_PROVIDER|g" \
        -e "s|{{GPT}}|$GPT_PROVIDER|g"         -e "s|{{GROK}}|$GROK_PROVIDER|g" \
        -e "s|{{COPILOT}}|$COPILOT_PROVIDER|g" -e "s|{{LOCAL}}|$LOCAL_PROVIDER|g" \
        -e "s|{{LOCAL_MODEL}}|$LOCAL_MODEL|g"  -e "s|{{SKILL_DIR}}|$SKILL_DIR|g" \
        "$@" -- "$src" > "$tmp"
    if grep -q '{{[A-Z_]*}}' "$tmp"; then rm -f -- "$tmp"; die "unrendered placeholder in $src"; fi
  else
    cp -- "$src" "$tmp"
  fi
  chmod 0644 "$tmp"
  mv -f -- "$tmp" "$dest"
  info "wrote $dest"
}

profile() { install_file "$SCRIPT_DIR/profiles/$1.toml.tmpl" "$PROFILES_DIR/$1.toml" render; }

# --------------------------------------------------------------- profiles --

# Delta's spawn tool offers the three built-ins, then at most seven custom
# profiles in alphabetical order, and silently drops the rest. Every custom
# profile file in the folder takes a slot, ours or not.
readonly CUSTOM_SLOTS=7
readonly BUILTIN_PROFILES=" worker scout reviewer "
readonly PRIORITY="scout-qwen scout-deepseek qwen-max deepseek-pro grok gpt-sol minimax gpt-astra scout-gemini scout-local"
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
# A metered MiniMax lane gets no candidate role: candidates run on flat lanes only.
if [[ -n $MINIMAX_PROVIDER ]]; then
  roles="worker"
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

# Retire superseded profiles first, so they free their slots.
if ((PRUNE_LEGACY)); then
  for old in $LEGACY_PROFILES; do
    f="$PROFILES_DIR/$old.toml"
    [[ -e $f ]] || continue
    if ((DRY_RUN)); then info "would retire $f"; else mv -f -- "$f" "$f.retired"; info "retired $f -> $f.retired"; fi
  done
fi

# Profiles this install does not manage take their slots first.
others="" n_others=0 legacy=""
for f in "$PROFILES_DIR"/*.toml; do
  [[ -e $f ]] || continue
  n="$(basename -- "$f" .toml)"
  if [[ $BUILTIN_PROFILES == *" $n "* ]] || configured "$n"; then continue; fi
  if [[ $LEGACY_PROFILES == *" $n "* ]]; then
    if ((PRUNE_LEGACY)); then continue; fi  # retired above (or would be, in a dry run)
    legacy+=" $n"
  fi
  others+=" $n"
  n_others=$((n_others + 1))
done

INSTALLED=" " n_installed=0 skipped=""
for p in $order; do
  if ! configured "$p"; then
    if [[ -e $PROFILES_DIR/$p.toml ]]; then
      warn "$p.toml is installed but $(lane_var "$p") is not set; the roster omits it, so the orchestrator will not use it"
    fi
    continue
  fi
  if ((n_others + n_installed < CUSTOM_SLOTS)); then
    install_profile "$p"
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
  for p in $skipped; do
    if [[ -e $PROFILES_DIR/$p.toml ]]; then
      warn "$p.toml from an earlier install is still there; delete it, or Delta drops another profile in its place"
    fi
  done
fi

# ------------------------------------------------------------------ skill --

install_file "$SCRIPT_DIR/skills/orchestrate/SKILL.md" "$SKILL_DIR/SKILL.md"
install_file "$SCRIPT_DIR/skills/orchestrate/references/best-of-n.md" \
             "$SKILL_DIR/references/best-of-n.md" render
install_file "$SCRIPT_DIR/skills/orchestrate/scripts/bon.sh" "$SKILL_DIR/scripts/bon.sh"

# ----------------------------------------------------------------- roster --

metered_list="$METERED_LANES"
[[ -n $MINIMAX_PROVIDER && $MINIMAX_TIER == metered ]] && metered_list="$metered_list, MiniMax"

installed() { [[ $INSTALLED == *" $1 "* ]]; }

generate_roster() {
  local o m name model lane family billing roles use
  printf '# Roster\n\nGenerated by install.sh on %s. Authoritative for this machine.\n' "$(date -u +%Y-%m-%dT%H:%MZ)"
  printf 'Regenerate with install.sh --force after changing plans or built-in models.\n\n'

  printf '## This thread\n\n'
  printf 'Orchestrator model: %s via %s. Family: %s. Never switch it mid-thread.\n' \
    "$ORCHESTRATOR_MODEL" "$ORCHESTRATOR_LANE" "$ORCHESTRATOR_FAMILY"
  printf 'This thread draws on its own lane too; leave headroom there.\n\n'

  printf '## Profiles\n\n'
  printf 'A profile takes only the roles listed here; the task block you send sets the role.\n\n'
  printf '| Spawn as | Model | Lane | Family | Billing | Worktree | Roles | Use for |\n'
  printf '|---|---|---|---|---|---|---|---|\n'
  printf '| Scout (built-in) | %s | %s | %s | flat | shared | scout | Default read-only recon |\n' \
    "$BUILTIN_SCOUT_MODEL" "$BUILTIN_SCOUT_LANE" "$BUILTIN_SCOUT_FAMILY"
  installed scout-qwen     && printf '| scout-qwen | qwen3.8-flash | Qwen Token Plan | Qwen | flat | shared | scout | Quick recon off the Z.AI lane |\n'
  installed scout-deepseek && printf '| scout-deepseek | deepseek-v4.1-flash | Qwen Token Plan | DeepSeek | flat | shared | scout | Recon needing more reasoning, off the Z.AI lane |\n'
  installed scout-gemini   && printf '| scout-gemini | gemini-3.8-flash | GitHub Copilot | Gemini | flat | shared | scout | Recon outside the coding-plan quotas |\n'
  installed scout-local    && printf '| scout-local | %s | Local inference | %s | flat (no quota) | shared | scout | Bulk or background recon; slower |\n' "$LOCAL_MODEL" "$LOCAL_FAMILY"
  printf '| Worker (built-in) | %s | %s | %s | flat | isolated | worker, candidate | Default implementer |\n' \
    "$BUILTIN_WORKER_MODEL" "$BUILTIN_WORKER_LANE" "$BUILTIN_WORKER_FAMILY"
  printf '| Reviewer (built-in) | %s | %s | %s | flat | isolated | reviewer, candidate | Default reviewer |\n' \
    "$BUILTIN_REVIEWER_MODEL" "$BUILTIN_REVIEWER_LANE" "$BUILTIN_REVIEWER_FAMILY"
  for m in "${MODELS[@]}"; do
    IFS='|' read -r name _ model lane family billing _ roles use <<<"$m"
    if installed "$name"; then
      printf '| %s | %s | %s | %s | %s | isolated | %s | %s |\n' "$name" "$model" "$lane" "$family" "$billing" "$roles" "$use"
    fi
  done
  printf '\n'

  printf '## Budgets (subagents in flight at once, this thread)\n\n'
  printf '| Lane | Limit | Basis |\n|---|---|---|\n'
  printf '| Kimi Code | %s | Request window, not concurrency, is the constraint |\n' "$KIMI_LIMIT"
  printf '| Z.AI Coding Plan | %s | Tier %s; concurrency is dynamic and higher off-peak |\n' "$ZAI_LIMIT" "$ZAI_TIER"
  printf '| Qwen Token Plan | %s | Tier %s concurrent-agent allowance |\n' "$QWEN_LIMIT" "$QWEN_TIER"
  [[ -n $MINIMAX_PROVIDER ]] && printf '| MiniMax | %s | Billing: %s |\n' "$MINIMAX_LIMIT" "$MINIMAX_BILLING"
  [[ -n $GPT_PROVIDER ]]     && printf '| ChatGPT subscription | 2 | Unpublished limits |\n'
  [[ -n $GROK_PROVIDER ]]    && printf '| Grok subscription | 1 | Unpublished limits; keep low |\n'
  [[ -n $COPILOT_PROVIDER ]] && printf '| GitHub Copilot | 1 | Premium-request allowance; keep low |\n'
  [[ -n $LOCAL_PROVIDER ]]   && printf '| Local inference | %s | Throughput of the local server |\n' "$LOCAL_LIMIT"
  printf '| **All lanes** | **%s** | Delta Max Agents Per Thread |\n\n' "$THREAD_CAP"
  printf 'Budgets count spawns by the lane they run on, whatever the profile.\n'
  printf 'These budgets are per orchestrator thread. Orchestrator threads do not see each\n'
  printf 'other, so two running at once can together exceed a plan'"'"'s limits. When another\n'
  printf 'orchestrator thread is active, use half of each limit (rounded down, minimum 1)\n'
  printf 'unless a shared admission proxy enforces the limits across threads.\n\n'

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
  printf 'Spawn each with no model override and the BEST-OF-N CANDIDATE block from references/best-of-n.md.\n\n'
  printf '| Spawn as | Model | Lane | Family |\n|---|---|---|---|\n'
  printf '| Worker (built-in) | %s | %s | %s |\n' "$BUILTIN_WORKER_MODEL" "$BUILTIN_WORKER_LANE" "$BUILTIN_WORKER_FAMILY"
  printf '| Reviewer (built-in) | %s | %s | %s |\n' "$BUILTIN_REVIEWER_MODEL" "$BUILTIN_REVIEWER_LANE" "$BUILTIN_REVIEWER_FAMILY"
  for m in "${MODELS[@]}"; do
    IFS='|' read -r name _ model lane family _ _ roles _ <<<"$m"
    if installed "$name" && [[ $roles == *candidate* ]]; then
      printf '| %s | %s | %s | %s |\n' "$name" "$model" "$lane" "$family"
    fi
  done
  return 0
}

roster_dest="$SKILL_DIR/references/roster.md"
if [[ -e $roster_dest && $FORCE -ne 1 ]]; then die "$roster_dest exists; rerun with --force to overwrite"; fi
if ((DRY_RUN)); then
  info "would write $roster_dest:"
  generate_roster | sed 's/^/  | /'
else
  mkdir -p -- "$(dirname -- "$roster_dest")"
  tmp="$(mktemp "${roster_dest}.XXXXXX")"
  generate_roster > "$tmp"
  chmod 0644 "$tmp"
  mv -f -- "$tmp" "$roster_dest"
  info "wrote $roster_dest"
fi

# ------------------------------------------------------------------ rules --
# Personal rules = context router + Ponytail's always-on text, generated from
# the installed plugin so a reinstall picks up Ponytail updates.

PONYTAIL_DIR="${PONYTAIL_DIR:-}"
if [[ -z $PONYTAIL_DIR ]]; then
  pt_base="$HOME/.codex/plugins/cache/ponytail/ponytail"
  if [[ -d $pt_base ]]; then
    PONYTAIL_DIR="$(find "$pt_base" -mindepth 1 -maxdepth 1 -type d | sort -V | tail -n 1)"
  fi
fi

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

generate_rules() {
  cat -- "$SCRIPT_DIR/rules/personal-AGENTS.md"
  if [[ -n $PONYTAIL_DIR && -f $PONYTAIL_DIR/AGENTS.md ]]; then
    printf '\n<!-- PONYTAIL_START %s -->\n' "$(basename -- "$PONYTAIL_DIR")"
    ponytail_block "$PONYTAIL_DIR/AGENTS.md"
    printf '<!-- PONYTAIL_END -->\n'
  fi
}

rules_out="$STATE_DIR/personal-AGENTS.generated.md"
if [[ -z $PONYTAIL_DIR || ! -f $PONYTAIL_DIR/AGENTS.md ]]; then
  warn "ponytail not found; generated rules contain the context router only (set PONYTAIL_DIR)"
fi
if ((DRY_RUN)); then
  info "would write $rules_out"
else
  mkdir -p -- "$STATE_DIR"
  tmp="$(mktemp "${rules_out}.XXXXXX")"
  generate_rules > "$tmp"
  chmod 0644 "$tmp"
  mv -f -- "$tmp" "$rules_out"
  rules_bytes=$(wc -c < "$rules_out" | tr -d ' ')
  info "wrote $rules_out ($rules_bytes bytes)"
  # Delta re-sends these rules every turn and shows no size cap; the budget is ours.
  ((rules_bytes <= 10240)) || warn "generated rules are $rules_bytes bytes, over the 10240-byte budget; trim before adding more"
fi

info ""
info "Next:"
info "  1. Set the built-in Scout, Worker, and Reviewer models in Settings > Subagents"
info "     to match the roster, then confirm every custom profile loaded without errors."
info "  2. Settings > Rules > Personal AGENTS.md: add the contents of"
info "     $rules_out"
info "     (replace any earlier DELTA_CONTEXT_ROUTER and PONYTAIL blocks),"
info "     then remove the file with install.sh --clean."
}
