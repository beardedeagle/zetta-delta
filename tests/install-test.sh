#!/bin/sh
# Offline check of install.sh's curl mode (curl | bash), refusals, backups, the
# Personal AGENTS.md update, and --clean: a fake curl serves a tarball of this
# checkout, and each run gets a scratch HOME.
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
# System tools (including macOS Python) may legitimately leave TMPDIR scratch.
# Only directories owned by the installer's bundle fetch count as residue.
download_residue() { find "$T/tmp" -mindepth 1 -maxdepth 1 -name 'zetta-delta.*' -print; }

mkdir -p "$T/bin" "$T/home" "$T/tmp" "$T/delta" && touch "$T/delta/settings.json"
printf 'system scratch\n' > "$T/tmp/system-scratch"
# A Ponytail plugin whose cache folder name is not its version, as Codex stores it.
pt=$T/home/.codex/plugins/cache/ponytail/ponytail/1.0.0
mkdir -p "$pt" && printf '# Ponytail\n\nGrep every caller of the function you touch.\n' > "$pt/AGENTS.md"
printf '{\n  "name": "ponytail",\n  "version": "4.10.3"\n}\n' > "$pt/package.json"
rules=$T/home/.config/delta/AGENTS.md
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
check "failed download: temporary copy removed" "$(download_residue)" ""

head -c "$(($(wc -c < "$ROOT/install.sh") / 2))" "$ROOT/install.sh" > "$T/half.sh"
r=$(run "$T/half.sh" "$prov ZETTA_DELTA_REF=abc123" --force)
has "cut-short download: bash refuses it (exit 2)" "$r" "exit=2"
check "cut-short download: nothing ran" "$(fetched) $(ls -A "$T/delta")" "none settings.json"

r=$(run "$ROOT/install.sh" "$prov ZETTA_DELTA_REF=abc123" --force)
has "piped install: exit 0" "$r" "exit=0"
check "piped install: fetched the pinned commit" "$(fetched)" "https://github.com/beardedeagle/zetta-delta/archive/abc123.tar.gz"
check "piped install: profiles" "$(cd "$T/delta/profiles" && printf '%s ' *)" "deepseek-pro.toml qwen-max.toml scout-deepseek.toml scout-qwen.toml "
check "piped install: skill" "$(cd "$T/home/.agents/skills/orchestrate" && find . -type f | LC_ALL=C sort | tr '\n' ' ')" \
  "./SKILL.md ./references/best-of-n.md ./references/effective-identity.md ./references/identity-registry.json ./references/roster.md ./scripts/bon.sh ./scripts/identity.py "
check "piped install: adversarial skill" "$(cd "$T/home/.agents/skills/adversarial" && find . -type f | LC_ALL=C sort | tr '\n' ' ')" \
  "./SKILL.md ./references/effective-identity.md ./references/identity-registry.json ./references/roster.md ./scripts/identity.py "
check "piped install: isolated skill" "$(cd "$T/home/.agents/skills/isolated" && find . -type f | LC_ALL=C sort | tr '\n' ' ')" \
  "./SKILL.md ./references/effective-identity.md ./references/identity-registry.json ./references/roster.md ./scripts/bon.sh ./scripts/identity.py "
check "piped install: pr-review skill" "$(cd "$T/home/.agents/skills/pr-review" && find . -type f | LC_ALL=C sort | tr '\n' ' ')" \
  "./SKILL.md ./references/effective-identity.md ./references/identity-registry.json ./references/roster.md ./scripts/identity.py "
check "piped install: one roster and one bon.sh for every skill" "$(cd "$T/home/.agents/skills" \
  && cmp -s orchestrate/references/roster.md adversarial/references/roster.md \
  && cmp -s orchestrate/references/roster.md isolated/references/roster.md \
  && cmp -s orchestrate/references/roster.md pr-review/references/roster.md \
  && cmp -s orchestrate/scripts/bon.sh isolated/scripts/bon.sh && echo same)" "same"
has "piped install: isolated runs its own bon.sh" "$(cat "$T/home/.agents/skills/isolated/SKILL.md")" \
  "sh '$T/home/.agents/skills/isolated/scripts/bon.sh' snapshot"
check "piped install: rules in Delta's Personal AGENTS.md" "$(head -1 "$rules")" "<!-- DELTA_CONTEXT_ROUTER_START v1 -->"
check "piped install: Ponytail labelled with its own version" "$(grep '^<!-- PONYTAIL_START' "$rules")" "<!-- PONYTAIL_START 4.10.3 -->"
check "piped install: nothing left to paste" "$(ls -A "$T/home/.local/state" 2>/dev/null)" ""
check "piped install: temporary copy removed" "$(download_residue)" ""
check "piped install: system scratch preserved" "$(cat "$T/tmp/system-scratch")" "system scratch"

r=$(run "$ROOT/install.sh" "ZETTA_DELTA_REF=abc123" --help)
has "piped --help: usage from the fetched copy" "$r" "Usage: install.sh [--force]"

rm -f "$T/url"
# shellcheck disable=SC2086
r=$(env -i $base $prov "$BASH_BIN" "$ROOT/install.sh" --dry-run --force 2>&1)
has "clone run: installs from the clone" "$r" "would write $T/delta/profiles/qwen-max.toml"
check "clone run: nothing fetched" "$(fetched)" "none"

# clone ARGS...: run the clone's install.sh over the install above; print the output and the exit status.
# shellcheck disable=SC2086
clone() { out=$(env -i $base $prov "$BASH_BIN" "$ROOT/install.sh" "$@" 2>&1); printf '%s\nexit=%s\n' "$out" "$?"; }
snapshot() { (cd "$T" && find delta home -type f | LC_ALL=C sort | xargs cksum); }
state=$T/home/.local/state/zetta-delta
printf 'name = "old"\n' > "$T/delta/profiles/candidate.toml" # a superseded profile
# Rules from an earlier install, plus one of the user's own.
printf '<!-- DELTA_CONTEXT_ROUTER_START v0 -->\nold router\n<!-- DELTA_CONTEXT_ROUTER_END -->\n\n# my own rule\n' > "$rules"
before=$(snapshot)
r=$(clone --prune-legacy)
has "no --force: refused" "$r" "rerun with --force"
has "no --force: names every file in the way" "$r" "$rules"
check "no --force: nothing changed, nothing retired" "$(snapshot)" "$before"

printf '# local edit\n' >> "$T/home/.agents/skills/adversarial/SKILL.md"
printf '# local review edit\n' >> "$T/home/.agents/skills/pr-review/SKILL.md"
r=$(clone --force --prune-legacy)
has "--force: exit 0" "$r" "exit=0"
check "--force: the edited file saved first" "$(tail -n 1 "$state"/backups/*/"${T#/}"/home/.agents/skills/adversarial/SKILL.md)" "# local edit"
check "--force: the edit replaced" "$(cmp -s "$ROOT/skills/adversarial/SKILL.md" "$T/home/.agents/skills/adversarial/SKILL.md" && echo same)" "same"
check "--force: the edited review skill saved first" "$(tail -n 1 "$state"/backups/*/"${T#/}"/home/.agents/skills/pr-review/SKILL.md)" "# local review edit"
check "--force: the review edit replaced" "$(cmp -s "$ROOT/skills/pr-review/SKILL.md" "$T/home/.agents/skills/pr-review/SKILL.md" && echo same)" "same"
check "--force: unchanged files not saved" "$(find "$state/backups" -name scout-qwen.toml -o -name scout-deepseek.toml -o -name qwen-max.toml -o -name deepseek-pro.toml -o -name bon.sh | wc -l | tr -d ' ')" "0"
check "--prune-legacy: retired" "$(cd "$T/delta/profiles" && ls candidate.toml*)" "candidate.toml.retired"
check "--force: old router replaced, once" "$(grep -c -e '^old router' -e '^<!-- DELTA_CONTEXT_ROUTER_START v1' "$rules")" "1"
check "--force: the user's own rule kept" "$(tail -n 1 "$rules")" "# my own rule"
check "--force: old rules saved first" "$(sed -n 2p "$state"/backups/*/"${T#/}"/home/.config/delta/AGENTS.md)" "old router"

printf '<!-- PONYTAIL_START 1 -->\n' >> "$rules" # a block with no end
before=$(snapshot)
r=$(clone --force)
has "unterminated block: refused" "$r" "PONYTAIL_START line without its PONYTAIL_END line"
check "unterminated block: nothing changed" "$(snapshot)" "$before"

printf 'old\n' > "$state/personal-AGENTS.generated.md" # left by an earlier version for pasting
r=$(run "$ROOT/install.sh" "" --clean)
has "clean: removes the old pasted-rules file" "$r" "removed $state/personal-AGENTS.generated.md"
check "clean: keeps only the backups, nothing fetched" "$(ls -A "$state") $(fetched)" "backups none"
r=$(run "$ROOT/install.sh" "" --clean)
has "clean again: nothing to remove" "$r" "nothing to remove"

[ "$fail" = 0 ] && echo "ALL PASS" || echo "SOME FAILED"
[ "$fail" = 0 ]
