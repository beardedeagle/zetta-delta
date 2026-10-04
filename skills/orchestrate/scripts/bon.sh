#!/bin/sh
# Best-of-N plumbing for the orchestrate and isolated skills. Delta runs every
# command in a fresh shell, so each subcommand takes what it needs as arguments.
#
#   bon.sh snapshot               parent: pin this checkout; prints RUN and TREE
#   bon.sh begin  RUN SLUG TREE   candidate: confirm this copy matches the snapshot
#   bon.sh export RUN SLUG TREE   candidate: save the patch, restore this copy
#   bon.sh apply  RUN SLUG        parent: apply one candidate's patch here
#   bon.sh clean  RUN             parent: drop the snapshot ref and saved patches
#
# Delta hides exit codes, so the last line is always "RESULT: ok: ..." or
# "RESULT: failed: ..."; read that line.
set -eu

CACHE="${XDG_CACHE_HOME:-$HOME/Library/Caches}/delta-orch"
finished=0
on_exit() { # a command that failed under set -e still ends with a RESULT line
  rc=$?
  if [ "$finished" = 0 ] && [ "$rc" != 0 ]; then
    printf 'RESULT: failed: a command failed (exit %s); see the error above\n' "$rc"
  fi
}
trap on_exit EXIT

ok() { finished=1; printf 'RESULT: ok: %s\n' "$*"; }
die() { finished=1; printf 'RESULT: failed: %s\n' "$*"; exit 1; }

check_run() {
  case $1 in bon-[0-9]*-[0-9]*) ;; *) die "RUN must look like bon-YYYYMMDD-HHMMSS, got '$1'" ;; esac
  case $1 in *[![:lower:][:digit:]-]*) die "RUN must look like bon-YYYYMMDD-HHMMSS, got '$1'" ;; esac
}
check_slug() {
  case $1 in ''|*[![:lower:][:digit:].-]*) die "SLUG must use lowercase letters, digits, dots, and hyphens, got '$1'" ;; esac
}
check_tree() {
  case $1 in ''|*[!0123456789abcdef]*) die "TREE must be the hex id the snapshot printed, got '$1'" ;; esac
  [ ${#1} -eq 40 ] || [ ${#1} -eq 64 ] || die "TREE must be the hex id the snapshot printed, got '$1'"
}

# Enter the checkout root; physical path, so parent and copy compare reliably.
enter_root() {
  top=$(git rev-parse --show-toplevel 2>/dev/null) || die "not inside a git checkout: $(pwd)"
  cd "$top"
  root=$(pwd -P)
}

# Tree of every tracked and untracked, non-ignored file, without touching the
# real index or HEAD. This is the content Delta copies and merges. It runs in
# its own subshell, whose trap removes the temporary index however it exits.
tree_now() (
  set -e
  tmp_index=$(mktemp "${TMPDIR:-/tmp}/bon-index.XXXXXX")
  trap 'rm -f "$tmp_index"' EXIT
  idx=$(git rev-parse --git-path index)
  if [ -f "$idx" ]; then cp "$idx" "$tmp_index"; else rm -f "$tmp_index"; fi
  GIT_INDEX_FILE=$tmp_index git add -A
  GIT_INDEX_FILE=$tmp_index git write-tree
)

run_dir() {
  dir="$CACHE/$1"
  [ -f "$dir/parent" ] || die "no snapshot $1 on this machine (expected $dir)"
  parent=$(cat "$dir/parent")
}

in_copy() { # refuse to run candidate steps in the parent's own checkout
  [ "$root" != "$parent" ] \
    || die "this is the parent's checkout, not an isolated copy; the candidate profile needs worktree = \"isolated\""
}

in_parent() {
  [ "$root" = "$parent" ] || die "run this in the checkout that took the snapshot: $parent"
}

cmd=${1:-}
[ $# -gt 0 ] && shift
case $cmd in
  snapshot)
    [ $# -eq 0 ] || die "usage: bon.sh snapshot"
    enter_root
    run="bon-$(date +%Y%m%d-%H%M%S)"
    dir="$CACHE/$run"
    [ ! -e "$dir" ] || die "$dir already exists; retry in a second"
    tree=$(tree_now)
    if git rev-parse -q --verify HEAD >/dev/null; then
      base=$(git commit-tree "$tree" -p HEAD -m "orchestrate snapshot $run")
    else
      base=$(git commit-tree "$tree" -m "orchestrate snapshot $run")
    fi
    # The ref keeps the snapshot's blobs alive for a later three-way apply.
    git update-ref "refs/orchestrate/$run" "$base"
    mkdir -p "$dir"
    printf '%s\n' "$root" > "$dir/parent"
    printf 'RUN=%s\nTREE=%s\n' "$run" "$tree"
    ok "snapshot $run of $root"
    ;;
  begin)
    [ $# -eq 3 ] || die "usage: bon.sh begin RUN SLUG TREE"
    check_run "$1"; check_slug "$2"; check_tree "$3"
    enter_root; run_dir "$1"; in_copy
    now=$(tree_now)
    [ "$now" = "$3" ] || die "this copy differs from snapshot $1 (tree $now, expected $3); report STATUS: blocked"
    ok "copy matches snapshot $1"
    ;;
  export)
    [ $# -eq 3 ] || die "usage: bon.sh export RUN SLUG TREE"
    check_run "$1"; check_slug "$2"; check_tree "$3"
    enter_root; run_dir "$1"; in_copy
    git cat-file -e "$3^{tree}" 2>/dev/null || die "tree $3 is not in this copy; run begin first"
    patch="$dir/$2.patch"
    end=$(tree_now)
    git diff --binary "$3" "$end" > "$patch"
    # Undo the attempt in the working tree only, so Delta's merge-back is empty.
    [ ! -s "$patch" ] || git apply -R "$patch"
    now=$(tree_now)
    [ "$now" = "$3" ] \
      || die "this copy did not return to the snapshot (tree $now) and would merge into the parent; report STATUS: failed"
    [ ! -s "$patch" ] || git apply --numstat "$patch"
    ok "patch saved to $patch; copy restored to the snapshot"
    ;;
  apply)
    [ $# -eq 2 ] || die "usage: bon.sh apply RUN SLUG"
    check_run "$1"; check_slug "$2"
    enter_root; run_dir "$1"; in_parent
    patch="$dir/$2.patch"
    [ -f "$patch" ] || die "no patch at $patch"
    if [ ! -s "$patch" ]; then ok "$2 made no changes; nothing to apply"; exit 0; fi
    if git apply --check "$patch" 2>/dev/null; then
      git apply "$patch"
      ok "applied $2 cleanly"
      exit 0
    fi
    # This checkout changed the patched files after the snapshot. A three-way
    # apply needs those paths to match the index, so stage them first.
    git apply --numstat "$patch" | cut -f3- | while IFS= read -r f; do
      if [ -e "$f" ]; then git add -- "$f"; fi
    done
    if git apply --3way "$patch"; then
      ok "applied $2 with a three-way merge; the patched paths are staged, tell the user"
    else
      die "conflicts in: $(git diff --name-only --diff-filter=U | tr '\n' ' ')- resolve them or ask the user; the patched paths are staged"
    fi
    ;;
  clean)
    [ $# -eq 1 ] || die "usage: bon.sh clean RUN"
    check_run "$1"
    enter_root
    git update-ref -d "refs/orchestrate/$1" 2>/dev/null || true
    rm -rf -- "${CACHE:?}/$1"
    ok "removed snapshot $1 and its patches"
    ;;
  *)
    die "usage: bon.sh snapshot | begin RUN SLUG TREE | export RUN SLUG TREE | apply RUN SLUG | clean RUN"
    ;;
esac
