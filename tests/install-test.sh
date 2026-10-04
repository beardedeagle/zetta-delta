#!/bin/sh
# Offline check of install.sh's curl mode (curl | bash) and --clean: a fake
# curl serves a tarball of this checkout, and each run gets a scratch HOME.
#   sh tests/install-test.sh                      runs install.sh with the bash on PATH
#   BASH_BIN=/bin/bash sh tests/install-test.sh   with another bash (macOS ships 3.2)
set -u
ROOT=$(cd "$(dirname "$0")/.." && pwd -P)
BASH_BIN=${BASH_BIN:-$(command -v bash)}
T=$(cd "$(mktemp -d "${TMPDIR:-/tmp}/install-test.XXXXXX")" && pwd -P)
fail=0
trap 'if [ "$fail" = 0 ]; then rm -rf "$T"; else echo "kept $T for inspection"; fi' EXIT
check() { if [ "$2" = "$3" ]; then echo "PASS $1"; else echo "FAIL $1: got [$2] want [$3]"; fail=1; fi; }
has() { case $2 in *"$3"*) echo "PASS $1" ;; *) echo "FAIL $1: got [$2] want it to contain [$3]"; fail=1 ;; esac; }
fetched() { if [ -e "$T/url" ]; then cat "$T/url"; else echo none; fi; }

mkdir -p "$T/bin" "$T/home" "$T/tmp" "$T/delta" && touch "$T/delta/settings.json"
tar -czf "$T/bundle.tar.gz" -C "$(dirname "$ROOT")" --exclude .git "$(basename "$ROOT")"
cat > "$T/bin/curl" <<EOF
#!/bin/sh
# Fake curl: records the URL (its last argument) and serves the bundle.
for a; do url=\$a; done
printf '%s\n' "\$url" > "$T/url"
[ -z "\${FAKE_CURL_FAIL:-}" ] || exit 22
cat "$T/bundle.tar.gz"
EOF
chmod +x "$T/bin/curl"

# Paths under $T hold no spaces, so these assignments word-split safely.
base="PATH=$T/bin:/usr/bin:/bin HOME=$T/home TMPDIR=$T/tmp DELTA_CONFIG_DIR=$T/delta"
prov="KIMI_PROVIDER=custom:k ZAI_PROVIDER=custom:z QWEN_PROVIDER=custom:q"
# run SCRIPT ENV ARGS...: feed SCRIPT to bash on stdin, as curl | bash does,
# with only $base and ENV set; print the output and the exit status.
run() {
  src=$1 extra=$2; shift 2
  rm -f "$T/url"
  # shellcheck disable=SC2086  # split on purpose, see above
  out=$(env -i $base $extra "$BASH_BIN" -s -- "$@" < "$src" 2>&1)
  printf '%s\nexit=%s\n' "$out" "$?"
}

r=$(run "$ROOT/install.sh" "$prov" --force)
has "no ref: refused" "$r" "set ZETTA_DELTA_REF to the commit to install"
has "no ref: exit 1" "$r" "exit=1"
check "no ref: nothing fetched" "$(fetched)" "none"

r=$(run "$ROOT/install.sh" "$prov ZETTA_DELTA_REF=a/../b" --force)
has "bad ref: refused" "$r" "ZETTA_DELTA_REF='a/../b' is invalid"
check "bad ref: nothing fetched" "$(fetched)" "none"

r=$(run "$ROOT/install.sh" "$prov ZETTA_DELTA_REF=abc123 FAKE_CURL_FAIL=1" --force)
has "failed download: reported" "$r" "could not fetch https://github.com/beardedeagle/zetta-delta at abc123"
check "failed download: temporary copy removed" "$(ls -A "$T/tmp")" ""

head -c "$(($(wc -c < "$ROOT/install.sh") / 2))" "$ROOT/install.sh" > "$T/half.sh"
r=$(run "$T/half.sh" "$prov ZETTA_DELTA_REF=abc123" --force)
has "cut-short download: bash refuses it (exit 2)" "$r" "exit=2"
check "cut-short download: nothing ran" "$(fetched) $(ls -A "$T/delta")" "none settings.json"

r=$(run "$ROOT/install.sh" "$prov ZETTA_DELTA_REF=abc123" --force)
has "piped install: exit 0" "$r" "exit=0"
check "piped install: fetched the pinned commit" "$(fetched)" "https://github.com/beardedeagle/zetta-delta/archive/abc123.tar.gz"
check "piped install: profiles" "$(cd "$T/delta/profiles" && printf '%s ' *)" "deepseek-pro.toml qwen-max.toml scout-deepseek.toml scout-qwen.toml "
check "piped install: skill" "$(cd "$T/home/.agents/skills/orchestrate" && find . -type f | LC_ALL=C sort | tr '\n' ' ')" \
  "./SKILL.md ./references/best-of-n.md ./references/roster.md ./scripts/bon.sh "
check "piped install: rules in the state folder" \
  "$(head -1 "$T/home/.local/state/zetta-delta/personal-AGENTS.generated.md")" "<!-- DELTA_CONTEXT_ROUTER_START v1 -->"
check "piped install: temporary copy removed" "$(ls -A "$T/tmp")" ""

r=$(run "$ROOT/install.sh" "ZETTA_DELTA_REF=abc123" --help)
has "piped --help: usage from the fetched copy" "$r" "Usage: install.sh [--force]"

rm -f "$T/url"
# shellcheck disable=SC2086
r=$(env -i $base $prov "$BASH_BIN" "$ROOT/install.sh" --dry-run --force 2>&1)
has "clone run: installs from the clone" "$r" "would write $T/delta/profiles/qwen-max.toml"
check "clone run: nothing fetched" "$(fetched)" "none"

r=$(run "$ROOT/install.sh" "" --clean)
has "clean: removes the state folder" "$r" "removed $T/home/.local/state/zetta-delta"
check "clean: folder gone, nothing fetched" "$(ls -A "$T/home/.local/state") $(fetched)" " none"
r=$(run "$ROOT/install.sh" "" --clean)
has "clean again: nothing to remove" "$r" "nothing to remove"

[ "$fail" = 0 ] && echo "ALL PASS" || echo "SOME FAILED"
[ "$fail" = 0 ]
