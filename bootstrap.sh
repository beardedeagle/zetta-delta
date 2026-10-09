#!/usr/bin/env bash
# Reproduce the bundled Delta setup without copying credentials or live settings.
# Requires Delta and Homebrew. Quit Delta first. Override account/tier variables
# when your subscriptions differ from this machine's Kimi/Z.AI/Qwen/MiniMax/GPT/Grok setup.
# Usage: bootstrap.sh [--dry-run] [--force] [--prune-legacy] [--skill-dir DIR]
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if [[ ${1:-} == --help || ${1:-} == -h ]]; then
  sed -n '2,5s/^# //p' "$ROOT/bootstrap.sh"
  exit 0
fi
INSTALL_ARGS=("$@")
DRY_RUN=0
while (($#)); do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --force|--prune-legacy) ;;
    --skill-dir) [[ $# -ge 2 ]] || { echo "--skill-dir needs a value" >&2; exit 1; }; shift ;;
    *) echo "unknown bootstrap argument: $1" >&2; exit 1 ;;
  esac
  shift
done
if ((DRY_RUN)); then
  printf '%s\n' "Plan only; no installations, configuration writes, or authentication:" \
    "1. brew bundle install --file $ROOT/Brewfile (build prerequisites and gh)" \
    "2. $ROOT/stack/install.sh (pinned context tools)" \
    "3. $ROOT/install.sh --configure-delta (providers, built-ins, skills, rules)" \
    "Defaults: ZAI_TIER=max QWEN_TIER=pro THREAD_CAP=15 MINIMAX_BILLING=plan; GPT/Grok enabled." \
    "Authenticate your accounts locally afterwards; existing files need --force."
  exit 0
fi
command -v brew >/dev/null 2>&1 || { echo "Homebrew is required: https://brew.sh" >&2; exit 1; }
if command -v pgrep >/dev/null 2>&1 && { pgrep -x Delta >/dev/null 2>&1 || pgrep -x delta >/dev/null 2>&1; }; then
  echo "Quit Delta before running bootstrap.sh; no application is stopped automatically." >&2
  exit 1
fi
HOMEBREW_BUNDLE_NO_UPGRADE=1 brew bundle install --file "$ROOT/Brewfile"
export PREFIX="${PREFIX:-$HOME/.local}"
BREW_PREFIX="$(brew --prefix)"
PYTHON_PREFIX="$(brew --prefix python@3.13)"
export PATH="$PYTHON_PREFIX/libexec/bin:$BREW_PREFIX/bin:$PREFIX/bin:$HOME/.cargo/bin:$PATH"
bash "$ROOT/stack/install.sh"
export ZAI_TIER="${ZAI_TIER:-max}" QWEN_TIER="${QWEN_TIER:-pro}" THREAD_CAP="${THREAD_CAP:-15}"
export GPT_PROVIDER="${GPT_PROVIDER-openai-subscribed}" GROK_PROVIDER="${GROK_PROVIDER-x_ai-subscribed}"
MINIMAX_PROVIDER="${MINIMAX_PROVIDER-$(python3 "$ROOT/scripts/setup.py" provider-id minimax)}"
export MINIMAX_PROVIDER
export MINIMAX_BILLING="${MINIMAX_BILLING:-plan}"
bash "$ROOT/install.sh" --configure-delta "${INSTALL_ARGS[@]}"
