#!/usr/bin/env bash
# Build and install the context tools the Delta rules route to
# (rules/personal-AGENTS.md), each at a pinned upstream release with this
# repository's patches applied (stack/patches/<tool>/, in order), plus the
# index maintainer the rules run. Run it from a clone of this repository.
#
# Tools (default: all; name some to install only those):
#   tgrep             microsoft/tgrep v1.0.11                    cargo install
#   semble            MinishLab/semble v0.6.1                    uv tool, with the mcp extra
#   codegraph         colbymchenry/codegraph v1.6.2 + patches    bundle under ~/.codegraph
#   zvec-grep         zvec-ai/zvec-grep v0.2.2 + patches         npm -g, packed from source
#   rtk               rtk-ai/rtk v0.51.0 + patch                 cargo install
#   ctx7 githits caveman                                         npm -g, pinned versions
#   index-maintainer  context-indexes/ -> ~/.local/share/zetta-delta/
#
# zvec-grep's version is stamped <release>+zetta-delta.<hash of its patches>,
# and the index maintainer's zg guard (zvec-grep/verify-install.py, copied to
# ~/.local/share/zvec-grep/) is pinned to the installed files, so the maintainer
# refuses zg if anything else replaces it. An earlier guard and pin are saved
# under ~/.local/share/zvec-grep/restores/<UTC time>/ first.
#
# Needs git, curl, and tar, plus per tool: cargo (tgrep, rtk, codegraph's
# native kernel), uv (semble), node >= 22 and npm (codegraph, zvec-grep, ctx7,
# githits, caveman), python3 (zvec-grep, index-maintainer).
#
# Settings:
#   PREFIX                  every command lands in $PREFIX/bin (default ~/.local):
#                           cargo and npm install under it, uv links its tools there
#   UV_TOOL_BIN_DIR         uv's own setting; overrides $PREFIX/bin for semble
#   CODEGRAPH_INSTALL_DIR   codegraph bundles (default ~/.codegraph, as upstream)
#   CODEGRAPH_BIN_DIR       codegraph launcher link (default $PREFIX/bin)
# A codegraph install keeps earlier versions under versions/ for rollback.
#
# Usage: stack/install.sh [--dry-run] [tool...]
set -euo pipefail

STACK_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly STACK_DIR

die()  { printf 'stack/install.sh: error: %s\n' "$*" >&2; exit 1; }
info() { printf '%s\n' "$*"; }
warn() { printf 'stack/install.sh: warning: %s\n' "$*" >&2; }
usage() { awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "${BASH_SOURCE[0]}"; }

# Pinned sources: name|repository|tag|commit. The clone must be at the commit.
SOURCES=(
  "tgrep|https://github.com/microsoft/tgrep|v1.0.11|7b706715ad3b620c350c73209523ba5034cd66a8"
  "semble|https://github.com/MinishLab/semble|v0.6.1|24497845460960db1839c8485319df189a889225"
  "codegraph|https://github.com/colbymchenry/codegraph|v1.6.2|6560052a6f856855d3f71eee838fd66ccfa4285d"
  "zvec-grep|https://github.com/zvec-ai/zvec-grep|v0.2.2|b1a9148e26a7bc9bd4a52229ffb7532d3063793d"
  "rtk|https://github.com/rtk-ai/rtk|v0.51.0|e001f773f80b22b7dc4c7a79521b30e35aaef026"
)
# Unpatched npm tools, pinned: tool|package spec|command.
NPM_TOOLS=(
  "ctx7|ctx7@0.5.13|ctx7"
  "githits|githits@0.26.0|githits"
  "caveman|@caveman-ai/cli@2.0.1|caveman"
)
readonly ALL_TOOLS="tgrep semble codegraph zvec-grep rtk ctx7 githits caveman index-maintainer"

DRY_RUN=0
tools=""
while (($#)); do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    -h|--help) usage; exit 0 ;;
    -*)        die "unknown argument: $1 (see --help)" ;;
    *)         [[ " $ALL_TOOLS " == *" $1 "* ]] || die "unknown tool: $1 (known: $ALL_TOOLS)"
               [[ " $tools " == *" $1 "* ]] || tools+=" $1" ;;
  esac
  shift
done
[[ -n $tools ]] || tools=" $ALL_TOOLS"
selected() { [[ " $tools " == *" $1 "* ]]; }

PREFIX="${PREFIX:-$HOME/.local}"
CODEGRAPH_INSTALL_DIR="${CODEGRAPH_INSTALL_DIR:-$HOME/.codegraph}"
CODEGRAPH_BIN_DIR="${CODEGRAPH_BIN_DIR:-$PREFIX/bin}"
export UV_TOOL_BIN_DIR="${UV_TOOL_BIN_DIR:-$PREFIX/bin}"
readonly MAINTAINER_DIR="$HOME/.local/share/zetta-delta"  # the rules run it from here
readonly ZG_GUARD_DIR="$HOME/.local/share/zvec-grep"     # the maintainer runs the zg guard from here
for v in PREFIX UV_TOOL_BIN_DIR CODEGRAPH_INSTALL_DIR CODEGRAPH_BIN_DIR; do
  [[ ${!v} == /* ]] || die "$v must be an absolute path"
done

# ---------------------------------------------------------- prerequisites --

need() { command -v "$1" >/dev/null 2>&1 || die "$1 not found; $2 needs it"; }
need git "fetching sources"; need curl "codegraph's bundle"; need tar "codegraph's bundle"
for t in tgrep rtk codegraph; do if selected "$t"; then need cargo "$t"; fi; done
if selected semble; then need uv semble; fi
for t in codegraph zvec-grep ctx7 githits caveman; do
  if selected "$t"; then
    need npm "$t"; need node "$t"
    (( $(node -p 'process.versions.node.split(".")[0]') >= 22 )) || die "$t needs node >= 22"
  fi
done
for t in zvec-grep index-maintainer; do if selected "$t"; then need python3 "$t"; fi; done

# ---------------------------------------------------------------- sources --

source_entry() { # print the SOURCES entry named $1
  local s
  for s in "${SOURCES[@]}"; do
    if [[ ${s%%|*} == "$1" ]]; then printf '%s\n' "$s"; return 0; fi
  done
  return 1
}

patches() { # list tool $1's patches in apply order
  local p
  for p in "$STACK_DIR/patches/$1"/*.patch; do
    if [[ -e $p ]]; then printf '%s\n' "$p"; fi
  done
}

fetch_source() { # $1=tool: clone its pinned commit into $WORK/$1, apply its patches, print the folder
  local name url tag commit dir p
  IFS='|' read -r name url tag commit <<<"$(source_entry "$1")"
  dir="$WORK/$name"
  info "$name: fetching $url $tag" >&2
  git -c init.defaultBranch=main init -q "$dir"
  git -C "$dir" fetch -q --depth 1 "$url" "$commit" || die "$name: could not fetch $commit from $url"
  git -C "$dir" checkout -q FETCH_HEAD
  [[ $(git -C "$dir" rev-parse HEAD) == "$commit" ]] || die "$name: $url did not give commit $commit"
  while IFS= read -r p; do
    git -C "$dir" apply "$p" || die "$name: $(basename -- "$p") does not apply to $tag"
    info "$name: applied $(basename -- "$p")" >&2
  done < <(patches "$name")
  printf '%s\n' "$dir"
}

# --------------------------------------------------------------- installs --

install_cargo() { # $1=tool $2=crate folder inside the source
  local dir
  dir="$(fetch_source "$1")"
  cargo install --locked --quiet --force --root "$PREFIX" --path "$dir/$2"
}

install_semble() { # the mcp extra keeps semble's MCP server working for other agents
  local dir
  dir="$(fetch_source semble)"
  uv tool install --force --quiet "semble[mcp] @ file://$dir"
}

install_zvec_grep() { # pack the patched source under a stamped version, install it, pin the zg guard to it
  local dir tgz stamp pkg saved f
  dir="$(fetch_source zvec-grep)"
  stamp="$(while IFS= read -r f; do cat -- "$f"; done < <(patches zvec-grep) | git hash-object --stdin)"
  (cd "$dir" && npm ci --ignore-scripts --no-audit --no-fund --loglevel=error \
     && npm pkg set "version=$(node -p 'require("./package.json").version')+zetta-delta.${stamp:0:12}" \
     && npm pack --pack-destination "$WORK" --loglevel=error >/dev/null)
  tgz=("$WORK"/zvec-zvec-grep-*.tgz)
  [[ -f ${tgz[0]} ]] || die "zvec-grep: npm pack produced no tarball"
  npm install -g --prefix "$PREFIX" --no-audit --no-fund --loglevel=error "${tgz[0]}"
  pkg="$PREFIX/lib/node_modules/@zvec/zvec-grep"
  [[ -f $pkg/package.json ]] || die "zvec-grep: no package at $pkg after the install"
  if [[ -e $ZG_GUARD_DIR/local-install.json || -e $ZG_GUARD_DIR/verify-install.py ]]; then
    saved="$ZG_GUARD_DIR/restores/$(date -u +%Y%m%dT%H%M%SZ)"
    mkdir -p -- "$saved"
    for f in local-install.json verify-install.py; do
      if [[ -e $ZG_GUARD_DIR/$f ]]; then cp -p -- "$ZG_GUARD_DIR/$f" "$saved/"; fi
    done
    info "zvec-grep: saved the previous zg guard and pin under $saved"
  fi
  mkdir -p -- "$ZG_GUARD_DIR"
  cp -- "$STACK_DIR/zvec-grep/verify-install.py" "$ZG_GUARD_DIR/"
  chmod 0755 "$ZG_GUARD_DIR/verify-install.py"
  if ! { python3 "$ZG_GUARD_DIR/verify-install.py" --pin "$pkg" && python3 "$ZG_GUARD_DIR/verify-install.py" "$pkg"; }; then
    die "zvec-grep: the zg guard does not accept the build just installed"
  fi
}

install_codegraph() { # upstream's self-contained bundle, linked like upstream's install.sh does
  local dir target tag dest stamp
  case "$(uname -s)-$(uname -m)" in
    Darwin-arm64)               target=darwin-arm64 ;;
    Darwin-x86_64)              target=darwin-x64 ;;
    Linux-x86_64)               target=linux-x64 ;;
    Linux-aarch64|Linux-arm64)  target=linux-arm64 ;;
    *) die "codegraph: no bundle target for $(uname -s)-$(uname -m)" ;;
  esac
  IFS='|' read -r _ _ tag _ <<<"$(source_entry codegraph)"
  dir="$(fetch_source codegraph)"
  # build-kernel.sh copies the kernel from the crate's own target/, so leave
  # CARGO_TARGET_DIR unset for it.
  (cd "$dir" && npm ci --no-audit --no-fund --loglevel=error \
     && env -u CARGO_TARGET_DIR bash scripts/build-kernel.sh && bash scripts/build-bundle.sh "$target")
  stamp="$(while IFS= read -r p; do cat -- "$p"; done < <(patches codegraph) | git hash-object --stdin)"
  mkdir -p -- "$CODEGRAPH_INSTALL_DIR/versions" "$CODEGRAPH_BIN_DIR"
  # A fresh build directory preserves the active bundle, including on a
  # same-release patch update or reinstall. Activate only a complete bundle.
  dest="$(mktemp -d "$CODEGRAPH_INSTALL_DIR/versions/$tag-zetta-delta.${stamp:0:12}-$target.XXXXXX")"
  if ! tar -xzf "$dir/release/codegraph-$target.tar.gz" -C "$dest" --strip-components=1; then
    rm -rf -- "$dest"
    die "codegraph: bundle extraction failed; the previous install is unchanged"
  fi
  if [[ ! -x $dest/bin/codegraph ]]; then
    rm -rf -- "$dest"
    die "codegraph: bundle has no executable launcher; the previous install is unchanged"
  fi
  ln -sf -- "$dest/bin/codegraph" "$CODEGRAPH_BIN_DIR/codegraph"
  ln -sfn -- "$dest" "$CODEGRAPH_INSTALL_DIR/current"
}

install_npm() { # $1=package spec
  npm install -g --prefix "$PREFIX" --no-audit --no-fund --loglevel=error "$1"
}

install_maintainer() {
  mkdir -p -- "$MAINTAINER_DIR"
  cp -- "$STACK_DIR/context-indexes/ensure-context-indexes.py" "$MAINTAINER_DIR/"
  chmod 0755 "$MAINTAINER_DIR/ensure-context-indexes.py"
}

# ----------------------------------------------------------------- run -----

plan() { # describe tool $1's install
  local entry name url tag commit spec n e
  if entry="$(source_entry "$1")"; then
    IFS='|' read -r name url tag commit <<<"$entry"
    n=$(patches "$1" | wc -l | tr -d ' ')
    info "$1: $url $tag ($commit) + $n patch(es)"
    [[ $1 != zvec-grep ]] || info "$1: stamps the version with the patches' hash and pins the zg guard in $ZG_GUARD_DIR"
    return
  fi
  for e in "${NPM_TOOLS[@]}"; do
    IFS='|' read -r name spec _ <<<"$e"
    if [[ $name == "$1" ]]; then info "$1: npm $spec"; return; fi
  done
  info "$1: $STACK_DIR/context-indexes/ensure-context-indexes.py -> $MAINTAINER_DIR/"
}

if ((DRY_RUN)); then
  for t in $tools; do plan "$t"; done
  exit 0
fi

WORK="$(mktemp -d "${TMPDIR:-/tmp}/zetta-delta-stack.XXXXXX")"
trap 'rm -rf -- "$WORK"' EXIT
export CARGO_TARGET_DIR="$WORK/cargo-target"

expected=""  # "command|path" pairs to check against PATH afterwards
for t in $tools; do
  plan "$t"
  case "$t" in
    tgrep)            install_cargo tgrep tgrep-cli; expected+=" tgrep|$PREFIX/bin/tgrep" ;;
    rtk)              install_cargo rtk .;           expected+=" rtk|$PREFIX/bin/rtk" ;;
    semble)           install_semble;                expected+=" semble|$UV_TOOL_BIN_DIR/semble" ;;
    zvec-grep)        install_zvec_grep;             expected+=" zg|$PREFIX/bin/zg" ;;
    codegraph)        install_codegraph;             expected+=" codegraph|$CODEGRAPH_BIN_DIR/codegraph" ;;
    index-maintainer) install_maintainer ;;
    *)
      for e in "${NPM_TOOLS[@]}"; do
        IFS='|' read -r name spec cmd <<<"$e"
        if [[ $name == "$t" ]]; then install_npm "$spec"; expected+=" $cmd|$PREFIX/bin/$cmd"; fi
      done ;;
  esac
  info "$t: installed"
done

# A copy earlier on PATH would shadow the one just installed.
for pair in $expected; do
  cmd="${pair%%|*}" want="${pair#*|}"
  have="$(command -v "$cmd" || true)"
  if [[ -z $have ]]; then
    warn "$cmd is installed at $want but not on PATH"
  elif [[ $have != "$want" ]]; then
    warn "$cmd on PATH is $have, not the one just installed ($want)"
  fi
done
