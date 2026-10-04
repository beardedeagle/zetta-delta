#!/bin/sh
# Content-preserving patch handoff. Commands run in fresh POSIX shells.
# snapshot; begin RUN SLUG TREE; export RUN SLUG TREE; check RUN TREE;
# apply RUN SLUG; clean RUN. Read the final RESULT line, not a piped exit code.
set -eu
umask 077
CACHE="${XDG_CACHE_HOME:-$HOME/Library/Caches}/delta-orch"
finished=0
scratch=
lock=
snapshot_pending=
snapshot_ref=
restore_lock=
on_exit() {
  rc=$?
  [ -z "$restore_lock" ] || rm -f -- "$restore_lock"
  [ -z "$lock" ] || rmdir "$lock" 2>/dev/null || true
  [ -z "$scratch" ] || rm -rf -- "$scratch"
  if [ -n "$snapshot_pending" ]; then
    if [ -n "$snapshot_ref" ]; then
      git update-ref -d "refs/orchestrate/${snapshot_pending##*/}" "$snapshot_ref" 2>/dev/null || true
    fi
    rm -rf -- "$snapshot_pending"
  fi
  if [ "$finished" = 0 ] && [ "$rc" != 0 ]; then
    printf 'RESULT: failed: a command failed (exit %s); see the error above\n' "$rc"
  fi
}
trap on_exit EXIT
ok() { finished=1; printf 'RESULT: ok: %s\n' "$*"; }
die() { finished=1; printf 'RESULT: failed: %s\n' "$*"; exit 1; }
check_run() {
  case $1 in bon-[0-9]*-[0-9]*) ;; *) die "RUN must look like bon-YYYYMMDD-HHMMSS, got '$1'" ;; esac
  case $1 in *[![:alnum:]-]*) die "RUN must look like bon-YYYYMMDD-HHMMSS, got '$1'" ;; esac
}
check_slug() {
  case $1 in ''|*[![:lower:][:digit:].-]*) die "SLUG must use lowercase letters, digits, dots, and hyphens, got '$1'" ;; esac
}
check_tree() {
  case $1 in ''|*[!0123456789abcdef]*) die "TREE must be the hex id the snapshot printed, got '$1'" ;; esac
  [ ${#1} -eq 40 ] || [ ${#1} -eq 64 ] || die "TREE must be the hex id the snapshot printed, got '$1'"
}
enter_root() {
  top=$(git rev-parse --show-toplevel 2>/dev/null) || die "not inside a git checkout: $(pwd)"
  cd "$top"
  root=$(pwd -P)
  scratch=$(mktemp -d "${TMPDIR:-/tmp}/bon-work.XXXXXX")
}
# Rebuild the content index rather than inherit assume-unchanged/skip-worktree.
# Seed with the initial inventory so later ignore rules cannot hide old files.
# Overlay current tracked entries to include newly force-added ignored files.
tree_now() (
  seed=${1:-}
  tmp_index="$scratch/content-index"
  rm -f "$tmp_index"
  GIT_INDEX_FILE=$tmp_index git read-tree --empty
  if [ -n "$seed" ]; then
    # A normal isolated clone need not possess the uncommitted snapshot tree.
    # Its NUL inventory seeds tracked paths; add materializes local file blobs.
    git -C "$parent" ls-tree -r -z "$seed" > "$scratch/snapshot-entries"
    GIT_INDEX_FILE=$tmp_index git update-index -z --index-info < "$scratch/snapshot-entries"
  fi
  git ls-files --stage -z > "$scratch/entries"
  GIT_INDEX_FILE=$tmp_index git update-index -z --index-info < "$scratch/entries"
  GIT_INDEX_FILE=$tmp_index git add -A
  GIT_INDEX_FILE=$tmp_index git write-tree
)
index_id() {
  git ls-files --stage -z > "$scratch/index-entries"
  git hash-object "$scratch/index-entries"
}
run_owner() {
  dir="$CACHE/$1"
  [ -f "$dir/parent" ] || die "no snapshot $1 on this machine (expected $dir)"
  parent=$(cat "$dir/parent")
}
run_dir() {
  run_owner "$1"
  bound_tree=$(git -C "$parent" rev-parse --verify "refs/orchestrate/$1^{tree}" 2>/dev/null) \
    || die "snapshot ref $1 is missing from its owning checkout"
}
bind_tree() { [ "$1" = "$bound_tree" ] || die "TREE does not belong to this RUN; expected $bound_tree"; }
in_copy() {
  [ "$root" != "$parent" ] \
    || die "this is the parent's checkout, not an isolated copy; the candidate profile needs worktree = \"isolated\""
}
in_parent() { [ "$root" = "$parent" ] || die "run this in the checkout that took the snapshot: $parent"; }
candidate_lock() {
  lock="$dir/$1.lock"
  if ! mkdir "$lock" 2>/dev/null; then lock=; die "another helper owns candidate $1; do not run it concurrently"; fi
}
candidate_index() {
  [ -f "$dir/$1.index" ] && [ -f "$dir/$1.copy" ] || die "run begin for this candidate before export"
  [ "$(cat "$dir/$1.copy")" = "$root" ] || die "this SLUG belongs to another isolated copy"
  current_index=$(index_id)
}
verify_candidate_index() {
  candidate_index "$1"
  [ "$current_index" = "$(cat "$dir/$1.index")" ] \
    || die "candidate index entries differ from begin; copy is not verified restored"
}
save_candidate_index() {
  idx=$(git rev-parse --git-path index)
  if [ -f "$idx" ]; then
    cp "$idx" "$dir/$1.raw-index"
    cp "$idx" "$scratch/index-tree"
    printf 'present\n' > "$dir/$1.index-state"
  else
    GIT_INDEX_FILE="$scratch/index-tree" git read-tree --empty
    printf 'absent\n' > "$dir/$1.index-state"
  fi
  # Existing unmerged stages remain valid to preserve, but cannot be compared
  # as a single tree if the candidate later changes only the index.
  GIT_INDEX_FILE="$scratch/index-tree" git write-tree > "$dir/$1.index-tree" 2>/dev/null || :
}
validate_export_index() {
  candidate_index "$1"
  export_index=$current_index
  [ "$current_index" != "$(cat "$dir/$1.index")" ] || return 0
  old_index_tree=$(cat "$dir/$1.index-tree")
  [ -n "$old_index_tree" ] || die "initial index has unmerged stages; changed index-only semantics require user review; no restoration attempted"
  idx=$(git rev-parse --git-path index)
  cp "$idx" "$scratch/index-tree"
  new_index_tree=$(GIT_INDEX_FILE="$scratch/index-tree" git write-tree) \
    || die "candidate index has unmerged stages; no restoration attempted"
  git diff-tree --name-only -r -z --no-renames "$old_index_tree" "$new_index_tree" > "$scratch/index-paths"
  git diff-tree --name-only -r -z --no-renames --diff-filter=D "$old_index_tree" "$new_index_tree" > "$scratch/index-deletions"
  # Index paths with no working-content delta, and untracking a retained file,
  # cannot be expressed by this helper's working-tree patch contract.
  # shellcheck disable=SC2016
  if ! GIT_LITERAL_PATHSPECS=1 xargs -0 sh -c '
    base=$1; end=$2; shift 2
    for f do
      if git diff-tree --quiet "$base" "$end" -- "$f"; then exit 1; fi
    done
  ' bon-index-delta "$bound_tree" "$now" < "$scratch/index-paths"; then
    die "candidate has index-only changes without working-content changes; no restoration attempted"
  fi
  # shellcheck disable=SC2016
  if ! xargs -0 sh -c '
    end=$1; shift
    for f do
      if git cat-file -e "$end:$f" 2>/dev/null; then exit 1; fi
    done
  ' bon-index-deletion "$now" < "$scratch/index-deletions"; then
    die "candidate index untracks retained files; index-only tracking changes require user review; no restoration attempted"
  fi
}
restore_candidate_index() {
  [ "$(index_id)" = "$export_index" ] \
    || die "candidate index changed during export; recovery patch preserved; no index restoration attempted"
  idx=$(git rev-parse --git-path index)
  if (set -C; : > "$idx.lock") 2>/dev/null; then
    restore_lock="$idx.lock"
  else die "Git index lock is held; recovery patch preserved; index restoration not completed"; fi
  if [ "$(cat "$dir/$slug.index-state")" = present ]; then
    cp "$dir/$slug.raw-index" "$restore_lock"
    mv "$restore_lock" "$idx"
  else
    rm -f -- "$idx" "$restore_lock"
  fi
  restore_lock=
}
export_result() {
  [ ! -s "$patch" ] || git apply --numstat "$patch"
  printf 'RESTORED_TREE=%s\n' "$bound_tree"
  ok "patch saved to $patch; copy restored to the snapshot; working content and index entries verified"
}
publish_export() {
  mv "$pending" "$artifact"
  publish_patch_link
  touch "$dir"
}
publish_patch_link() {
  ln -s "$slug.export/patch" "$scratch/patch-link"
  mv -f "$scratch/patch-link" "$patch"
}
cmd=${1:-}
[ $# -gt 0 ] && shift
case $cmd in
  snapshot)
    [ $# -eq 0 ] || die 'usage: bon.sh snapshot'
    enter_root
    mkdir -p "$CACHE"
    # mktemp reserves atomically; the random suffix distinguishes same-second rooms.
    dir=$(mktemp -d "$CACHE/bon-$(date +%Y%m%d-%H%M%S)-XXXXXX")
    run=${dir##*/}
    snapshot_pending=$dir
    index=$(index_id)
    tree=$(tree_now)
    if git rev-parse -q --verify HEAD >/dev/null; then
      base=$(git commit-tree "$tree" -p HEAD -m "orchestrate snapshot $run")
    else
      base=$(git commit-tree "$tree" -m "orchestrate snapshot $run")
    fi
    git update-ref "refs/orchestrate/$run" "$base" ''
    snapshot_ref=$base
    printf '%s\n' "$root" > "$dir/parent"
    printf '%s\n' "$index" > "$dir/index"
    snapshot_pending=
    printf 'RUN=%s\nTREE=%s\n' "$run" "$tree"
    ok "snapshot $run of $root"
    ;;
  begin)
    [ $# -eq 3 ] || die 'usage: bon.sh begin RUN SLUG TREE'
    check_run "$1"; check_slug "$2"; check_tree "$3"
    enter_root; run_dir "$1"; bind_tree "$3"; in_copy
    candidate_lock "$2"
    now=$(tree_now "$bound_tree")
    [ "$now" = "$bound_tree" ] || die "this copy differs from snapshot $1 (tree $now, expected $bound_tree); report STATUS: blocked"
    if [ -f "$dir/$2.index" ]; then
      verify_candidate_index "$2"
    else
      index_id > "$dir/$2.index"
      save_candidate_index "$2"
      printf '%s\n' "$root" > "$dir/$2.copy"
    fi
    touch "$dir"
    ok "copy matches snapshot $1"
    ;;
  check)
    [ $# -eq 2 ] || die 'usage: bon.sh check RUN TREE'
    check_run "$1"; check_tree "$2"
    enter_root; run_dir "$1"; bind_tree "$2"; in_parent
    now=$(tree_now "$bound_tree")
    [ "$now" = "$bound_tree" ] || die "parent content differs from snapshot $1 (tree $now, expected $bound_tree)"
    [ ! -f "$dir/index" ] || [ "$(index_id)" = "$(cat "$dir/index")" ] \
      || die "parent index entries differ from snapshot $1"
    ok "parent content and index entries match snapshot $1"
    ;;
  export)
    [ $# -eq 3 ] || die 'usage: bon.sh export RUN SLUG TREE'
    check_run "$1"; check_slug "$2"; check_tree "$3"
    enter_root; run_dir "$1"; bind_tree "$3"; in_copy
    slug=$2; candidate_lock "$slug"; candidate_index "$slug"
    patch="$dir/$slug.patch"; pending="$dir/$slug.pending"; artifact="$dir/$slug.export"
    now=$(tree_now "$bound_tree")
    if [ -d "$artifact" ]; then
      [ "$now" = "$bound_tree" ] || die "completed export exists and this copy has newer work; original patch preserved; no restoration attempted"
      verify_candidate_index "$slug"
      if [ ! -e "$patch" ]; then
        publish_patch_link
      fi
      export_result; exit 0
    fi
    [ ! -e "$patch" ] || die "a saved legacy patch already exists; preserve it or use a new SLUG; no restoration attempted"
    if [ -d "$pending" ]; then
      [ -f "$pending/ready" ] || die "incomplete export at $pending; copy unchanged by this invocation; inspect it before retrying"
      end=$(cat "$pending/end")
      [ "$now" = "$end" ] || [ "$now" = "$bound_tree" ] \
        || die "export recovery patch is at $pending/patch but copy has different work; no restoration attempted"
      expected_index=$(cat "$pending/index")
      [ "$current_index" = "$expected_index" ] || [ "$current_index" = "$(cat "$dir/$slug.index")" ] \
        || die "candidate index changed after the interrupted export; recovery patch preserved; no restoration attempted"
      export_index=$current_index
    else
      validate_export_index "$slug"
      mkdir "$pending"
      end=$now
      git diff-tree -p --binary --no-renames "$bound_tree" "$end" > "$pending/patch"
      git diff-tree --name-only -r -z --no-renames "$bound_tree" "$end" > "$pending/paths"
      printf '%s\n' "$end" > "$pending/end"
      printf '%s\n' "$export_index" > "$pending/index"
      : > "$pending/ready"
    fi
    if [ "$now" != "$bound_tree" ] && [ -s "$pending/patch" ]; then
      if ! git apply --whitespace=nowarn -R "$pending/patch"; then
        die "copy restoration failed; recovery patch preserved at $pending/patch; copy is not verified restored"
      fi
    fi
    now=$(tree_now "$bound_tree")
    [ "$now" = "$bound_tree" ] \
      || die "copy did not return to snapshot (tree $now); recovery patch at $pending/patch; copy is not verified restored"
    restore_candidate_index
    verify_candidate_index "$slug"
    publish_export
    export_result
    ;;
  apply)
    [ $# -eq 2 ] || die 'usage: bon.sh apply RUN SLUG'
    check_run "$1"; check_slug "$2"
    enter_root; run_dir "$1"; in_parent
    patch="$dir/$2.patch"
    [ -f "$patch" ] || die "no patch at $patch"
    if [ ! -s "$patch" ]; then ok "$2 made no changes; nothing to apply"; exit 0; fi
    if git apply --whitespace=nowarn --check "$patch" 2>/dev/null; then
      git apply --whitespace=nowarn "$patch"
      ok "applied $2 cleanly"; exit 0
    fi
    paths="$dir/$2.export/paths"
    if [ ! -f "$paths" ]; then
      # Legacy artifacts retain clean apply and old-path three-way support.
      # Git parses the patch itself; never interpret numstat quoting/pathspecs.
      git apply --build-fake-ancestor="$scratch/ancestor" "$patch"
      GIT_INDEX_FILE="$scratch/ancestor" git ls-files -z > "$scratch/legacy-paths"
      paths="$scratch/legacy-paths"
    fi
    # Keep NUL paths as literal argv. New absent destinations belong to apply,
    # while tracked deletions and dangling links must reach the parent's index.
    # xargs bounds argv length; every child fails immediately on a Git error.
    # shellcheck disable=SC2016
    GIT_LITERAL_PATHSPECS=1 xargs -0 sh -c '
      set -e
      for f do
        tracked=0
        if git ls-files --error-unmatch -- "$f" >/dev/null 2>&1; then tracked=1; fi
        if [ -e "$f" ] || [ -L "$f" ] || [ "$tracked" = 1 ]; then
          if [ "$tracked" = 1 ]; then
            git update-index --no-assume-unchanged --no-skip-worktree -- "$f"
          fi
          git add -A -f -- "$f"
        fi
      done
    ' bon-stage < "$paths"
    if git apply --whitespace=nowarn --3way "$patch"; then
      ok "applied $2 with a three-way merge; the patched paths are staged, tell the user"; exit 0
    fi
    conflicts=$(git diff --name-only --diff-filter=U | tr '\n' ' ')
    [ -n "$conflicts" ] \
      || die "the three-way apply of $2 failed without conflicts; inspect the patched paths and error above; selected existing/deleted paths were staged"
    die "conflicts in: $conflicts- resolve them or ask the user; the patched paths are staged"
    ;;
  clean)
    [ $# -eq 1 ] || die 'usage: bon.sh clean RUN'
    check_run "$1"
    enter_root; run_owner "$1"; in_parent
    git update-ref -d "refs/orchestrate/$1"
    rm -rf -- "${CACHE:?}/$1"
    swept=0
    for old in "$CACHE"/bon-*; do
      [ -d "$old" ] || continue
      owner=$(cat "$old/parent" 2>/dev/null || true)
      if [ -n "$owner" ] && [ -d "$owner" ]; then
        [ "$owner" = "$root" ] || continue
        [ -n "$(find "$old" -prune -mtime +30)" ] || continue
      fi
      git update-ref -d "refs/orchestrate/${old##*/}" 2>/dev/null || true
      rm -rf -- "$old"
      swept=$((swept + 1))
    done
    if [ "$swept" -gt 0 ]; then
      ok "removed snapshot $1 and its patches, and $swept abandoned run(s) older than 30 days or without a checkout"
    else ok "removed snapshot $1 and its patches"; fi
    ;;
  *) die 'usage: bon.sh snapshot | begin RUN SLUG TREE | export RUN SLUG TREE | check RUN TREE | apply RUN SLUG | clean RUN' ;;
esac
