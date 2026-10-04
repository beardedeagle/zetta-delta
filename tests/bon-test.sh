#!/bin/sh
# End-to-end check of bon.sh with a simulated Delta isolated copy.
#   sh tests/bon-test.sh                 checks the bundle's bon.sh
#   BON=/path/to/bon.sh sh tests/bon-test.sh   checks another copy
set -u
BON=${BON:-$(cd "$(dirname "$0")/.." && pwd -P)/skills/orchestrate/scripts/bon.sh}
# Physical path: bon.sh compares checkouts by `pwd -P`.
T=$(cd "$(mktemp -d "${TMPDIR:-/tmp}/bon-test.XXXXXX")" && pwd -P)
export XDG_CACHE_HOME="$T/cache"
fail=0
trap 'if [ "$fail" = 0 ]; then rm -rf "$T"; else echo "kept $T for inspection (log: $T/log)"; fi' EXIT
check() { if [ "$2" = "$3" ]; then echo "PASS $1"; else echo "FAIL $1: got [$2] want [$3]"; fail=1; fi; }
prefix() { case $2 in "$3"*) echo "PASS $1" ;; *) echo "FAIL $1: got [$2] want prefix [$3]"; fail=1 ;; esac; }
snap() { (cd "$1" && idx=$(git rev-parse --git-path index) && cp "$idx" "$T/i" && GIT_INDEX_FILE="$T/i" git add -A && GIT_INDEX_FILE="$T/i" git write-tree); rm -f "$T/i"; }
files() { git ls-files -co --exclude-standard | while IFS= read -r f; do [ -e "$f" ] && echo "$f"; done; }
# Run like Delta does: fresh /bin/sh, piped through cat; return the RESULT line.
delta() { (cd "$1" && /bin/sh -c "$2 | cat" 2>&1) | tee -a "$T/log" | grep '^RESULT:' | tail -1; }
bon() { printf 'sh "%s" %s' "$BON" "$*"; }

# Parent checkout: committed files plus uncommitted edits, an untracked file,
# a deletion, and ignored build output.
mkdir -p "$T/parent" && cd "$T/parent" || exit 1
git init -q && git config user.email t@t && git config user.name t
printf 'build/\n' > .gitignore
printf 'one\ntwo\nthree\n' > a.txt; printf 'keep\n' > keep.txt; printf 'gone\n' > del.txt
printf '\000\001\002' > bin.dat; printf '#!/bin/sh\n' > tool.sh
git add -A && git commit -qm init
printf 'one\ntwo-parent\nthree\n' > a.txt; printf 'untracked\n' > u.txt; rm del.txt
mkdir build && printf 'artifact\n' > build/x

out=$(cd "$T/parent" && /bin/sh -c "$(bon snapshot) | cat")
RUN=$(printf '%s\n' "$out" | sed -n 's/^RUN=//p'); TREE=$(printf '%s\n' "$out" | sed -n 's/^TREE=//p')
check "snapshot prints RUN" "$(printf %s "$RUN" | cut -c1-4)" "bon-"
check "snapshot ref exists" "$(git -C "$T/parent" rev-parse "refs/orchestrate/$RUN^{tree}")" "$TREE"
check "snapshot left index alone" "$(git -C "$T/parent" status --porcelain | tr '\n' ' ')" " M a.txt  D del.txt ?? u.txt "

# Isolated copy as Delta makes it: a separate clone carrying every uncommitted,
# non-ignored change.
mkcopy() {
  git clone -q "$T/parent" "$1" && git -C "$1" config user.email t@t && git -C "$1" config user.name t
  (cd "$T/parent" && git ls-files -co --exclude-standard | while IFS= read -r f; do
     if [ -e "$f" ]; then mkdir -p "$1/$(dirname "$f")"; cp -p "$f" "$1/$f"; else rm -f "$1/$f"; fi; done)
  :
}
mkcopy "$T/copy"
check "begin in copy" "$(delta "$T/copy" "$(bon begin "$RUN" kimi "$TREE")")" "RESULT: ok: copy matches snapshot $RUN"

# The attempt: edit, add, delete, binary change, mode change, nested new file.
cd "$T/copy" || exit 1
printf 'one\ntwo-parent\nthree\nfour\n' > a.txt; printf 'new\n' > n.txt; rm keep.txt
printf '\000\003' > bin.dat; chmod +x tool.sh; mkdir -p lib/deep && printf 'deep\n' > lib/deep/m.txt
mkdir -p build && printf 'ignored change\n' > build/y
mkdir -p "$T/expect" && files | while IFS= read -r f; do mkdir -p "$T/expect/$(dirname "$f")"; cp -p "$f" "$T/expect/$f"; done

r=$(delta "$T/copy" "$(bon export "$RUN" kimi "$TREE")")
check "export" "$(printf %s "$r" | sed 's/saved to [^;]*;/saved;/')" "RESULT: ok: patch saved; copy restored to the snapshot; working content and index entries verified"
check "copy back at snapshot" "$(snap "$T/copy")" "$TREE"
check "copy has no new files" "$(cd "$T/copy" && printf '%s ' *)" "a.txt bin.dat build keep.txt tool.sh u.txt "

check "apply refused in copy" "$(delta "$T/copy" "$(bon apply "$RUN" kimi)")" "RESULT: failed: run this in the checkout that took the snapshot: $T/parent"
prefix "begin refused in parent" "$(delta "$T/parent" "$(bon begin "$RUN" kimi "$TREE")")" "RESULT: failed: this is the parent's checkout, not an isolated copy"
check "apply clean" "$(delta "$T/parent" "$(bon apply "$RUN" kimi)")" "RESULT: ok: applied kimi cleanly"
diffs=$(cd "$T/parent" && { files; (cd "$T/expect" && find . -type f | sed 's|^\./||'); } | sort | uniq -u; cd "$T/parent" && files | while IFS= read -r f; do cmp -s "$f" "$T/expect/$f" || echo "$f"; done)
check "parent now equals the attempt" "$diffs" ""
check "mode change carried" "$( [ -x "$T/parent/tool.sh" ] && echo x)" "x"
check "deletion carried" "$( [ -e "$T/parent/keep.txt" ] && echo present || echo gone)" "gone"

# Conflict path: a second candidate edits the line the parent has since changed.
# First put the parent back to its snapshot state.
cd "$T/parent" && git read-tree "$TREE" && git checkout-index -a -f && git clean -qfd && git reset -q
mkcopy "$T/copy2"
check "begin copy2" "$(delta "$T/copy2" "$(bon begin "$RUN" glm "$TREE")")" "RESULT: ok: copy matches snapshot $RUN"
printf 'one\ntwo-glm\nthree\n' > "$T/copy2/a.txt"
check "export copy2" "$(delta "$T/copy2" "$(bon export "$RUN" glm "$TREE")" | cut -c1-23)" "RESULT: ok: patch saved"
printf 'one\ntwo-later\nthree\n' > "$T/parent/a.txt"
r=$(delta "$T/parent" "$(bon apply "$RUN" glm)")
check "apply conflict reported" "$(printf %s "$r" | cut -c1-34)" "RESULT: failed: conflicts in: a.tx"
if grep -q '<<<<<<<' "$T/parent/a.txt"; then echo "PASS conflict markers present"; else echo "FAIL no conflict markers"; fail=1; fi

# Three-way path with a rename: the candidate renames u.txt, which is untracked
# in the parent, and edits a.txt, which the parent has since edited elsewhere.
cd "$T/parent" && git read-tree "$TREE" && git checkout-index -a -f && git clean -qfd && git reset -q
mkcopy "$T/copy3"
check "begin copy3" "$(delta "$T/copy3" "$(bon begin "$RUN" deepseek "$TREE")")" "RESULT: ok: copy matches snapshot $RUN"
mv "$T/copy3/u.txt" "$T/copy3/v.txt"; printf 'one\ntwo-parent\nthree-deepseek\n' > "$T/copy3/a.txt"
check "export copy3" "$(delta "$T/copy3" "$(bon export "$RUN" deepseek "$TREE")" | cut -c1-23)" "RESULT: ok: patch saved"
printf 'one-later\ntwo-parent\nthree\n' > "$T/parent/a.txt"
check "apply rename three-way" "$(delta "$T/parent" "$(bon apply "$RUN" deepseek)" | cut -c1-58)" "RESULT: ok: applied deepseek with a three-way merge; the p"
check "rename three-way: both edits kept" "$(tr '\n' ' ' < "$T/parent/a.txt")" "one-later two-parent three-deepseek "
check "rename three-way: file renamed" "$( [ -e "$T/parent/u.txt" ] && echo u; [ -e "$T/parent/v.txt" ] && echo v)" "v"

# Guards.
mkdir -p "$T/stale" && cp -R "$T/copy2/." "$T/stale/" && printf 'drift\n' > "$T/stale/a.txt"
prefix "begin detects drift" "$(delta "$T/stale" "$(bon begin "$RUN" qwen "$TREE")")" "RESULT: failed: this copy differs from snapshot $RUN"
check "bad RUN" "$(delta "$T/copy2" "$(bon begin 'run-1' qwen "$TREE")")" "RESULT: failed: RUN must look like bon-YYYYMMDD-HHMMSS, got 'run-1'"
check "bad SLUG" "$(delta "$T/copy2" "$(bon begin "$RUN" Qwen "$TREE")")" "RESULT: failed: SLUG must use lowercase letters, digits, dots, and hyphens, got 'Qwen'"
check "placeholder TREE" "$(delta "$T/copy2" "$(bon begin "$RUN" qwen '\<TREE\>')")" "RESULT: failed: TREE must be the hex id the snapshot printed, got '<TREE>'"
check "missing args" "$(delta "$T/copy2" "$(bon export "$RUN")")" "RESULT: failed: usage: bon.sh export RUN SLUG TREE"

# Failures still end with a RESULT line; the temp-file check below covers them too.
mkdir -p "$T/nogit"
check "snapshot outside git" "$(delta "$T/nogit" "$(bon snapshot)")" "RESULT: failed: not inside a git checkout: $T/nogit"
if [ "$(id -u)" != 0 ]; then  # root reads unreadable files
  mkdir -p "$T/locked" && cp -R "$T/copy2/." "$T/locked/" && printf 'x\n' > "$T/locked/locked.txt" && chmod 000 "$T/locked/locked.txt"
  prefix "git failure inside begin" "$(delta "$T/locked" "$(bon begin "$RUN" qwen "$TREE")")" "RESULT: failed: a command failed (exit 128)"
  chmod 644 "$T/locked/locked.txt"
fi

# Abandoned runs: one of this checkout's, 31 days old; one whose checkout is
# gone; a recent one of this checkout and an old one of another, both kept.
C=$T/cache/delta-orch
mkrun() { mkdir -p "$C/$1" && printf '%s\n' "$2" > "$C/$1/parent" && git -C "$T/parent" update-ref "refs/orchestrate/$1" HEAD; }
mkrun bon-20200101-000000 "$T/parent"; touch -t "$(date -v-31d +%Y%m%d%H%M 2>/dev/null || date -d '31 days ago' +%Y%m%d%H%M)" "$C/bon-20200101-000000"
mkrun bon-20200102-000000 "$T/no-such-checkout"
mkrun bon-20200103-000000 "$T/parent"
mkrun bon-20200104-000000 "$T/copy2"; touch -t 202001010000 "$C/bon-20200104-000000"
check "clean" "$(delta "$T/parent" "$(bon clean "$RUN")")" "RESULT: ok: removed snapshot $RUN and its patches, and 2 abandoned run(s) older than 30 days or without a checkout"
check "clean removed the run's ref and the abandoned ones'" "$(git -C "$T/parent" for-each-ref --format='%(refname:short)' refs/orchestrate | tr '\n' ' ')" "orchestrate/bon-20200103-000000 orchestrate/bon-20200104-000000 "
check "clean removed the run's cache and the abandoned ones'" "$(cd "$C" && printf '%s ' *)" "bon-20200103-000000 bon-20200104-000000 "
left=0
for f in "${TMPDIR:-/tmp}"/bon-index.*; do if [ -e "$f" ]; then left=$((left + 1)); fi; done
check "no temp index files left" "$left" "0"

[ "$fail" = 0 ] && echo "ALL PASS" || echo "SOME FAILED"
exit "$fail"
