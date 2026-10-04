#!/bin/sh
# Check of examples/agents-prepare.sh: slot claims, including many checkouts
# reclaiming abandoned slots at once; the cache-first dependency fetch; and the
# index maintainer starting in its own session.
#   sh tests/prepare-test.sh                        checks the bundle's example
#   PREPARE=/path/to/prepare sh tests/prepare-test.sh   checks another copy
set -u
PREPARE=${PREPARE:-$(cd "$(dirname "$0")/.." && pwd -P)/examples/agents-prepare.sh}
T=$(cd "$(mktemp -d "${TMPDIR:-/tmp}/prepare-test.XXXXXX")" && pwd -P)
fail=0
trap 'if [ "$fail" = 0 ]; then rm -rf "$T"; else echo "kept $T for inspection"; fi' EXIT
check() { if [ "$2" = "$3" ]; then echo "PASS $1"; else echo "FAIL $1: got [$2] want [$3]"; fail=1; fi; }

mkdir -p "$T/bin" "$T/home/.local/share/zetta-delta" "$T/slots"
# Fake mix and cargo log each call; with MISS_CACHE set, the offline call fails.
for tool in mix cargo; do
  cat > "$T/bin/$tool" <<EOF
#!/bin/sh
echo "$tool \$* hex_offline=\${HEX_OFFLINE:-}" >> "$T/calls"
case "\${HEX_OFFLINE:-}\$*" in 1*|*--offline*) [ -z "\${MISS_CACHE:-}" ] ;; esac
EOF
  chmod +x "$T/bin/$tool"
done
# Fake maintainer: records whether it leads its own session, and the payload's cwd.
cat > "$T/home/.local/share/zetta-delta/ensure-context-indexes.py" <<EOF
import json, os, sys
cwd = json.loads(sys.stdin.read())["cwd"]
open("$T/maintainer", "w").write("leader=%s cwd=%s\n" % (os.getsid(0) == os.getpid(), cwd))
EOF
PATH_ENV="$T/bin:$(dirname "$(command -v git)"):$(dirname "$(command -v python3)"):/usr/bin:/bin"
# prep DIR [VAR=value...]: run the prepare script in DIR like Delta does.
prep() { d=$1; shift; (cd "$d" && env -i HOME="$T/home" PATH="$PATH_ENV" TMPDIR="$T" DELTA_SLOTS_DIR="$T/slots" "$@" bash "$PREPARE" 2>&1); }
slot_of() { sed -n 's/^export DELTA_SLOT=//p' "$1/.delta-env"; }
newrepo() { git init -q "$1"; }

newrepo "$T/c1" && touch "$T/c1/mix.exs" "$T/c1/Cargo.toml"
check "first checkout gets slot 1" "$(prep "$T/c1" | tail -n 1)" "prepare: $T/c1 -> slot 1"
check "cached dependencies: offline fetch only" "$(cat "$T/calls")" "mix deps.get hex_offline=1
cargo fetch --offline hex_offline="
i=0; while [ ! -s "$T/maintainer" ] && [ "$i" -lt 50 ]; do sleep 0.1; i=$((i + 1)); done
check "maintainer runs in its own session" "$(cat "$T/maintainer" 2>/dev/null)" "leader=True cwd=$T/c1"

rm -f "$T/calls"
prep "$T/c1" MISS_CACHE=1 >/dev/null
check "missing from the cache: offline, then online" "$(cat "$T/calls")" "mix deps.get hex_offline=1
mix deps.get hex_offline=
cargo fetch --offline hex_offline=
cargo fetch hex_offline="
check "a rerun keeps its slot" "$(slot_of "$T/c1")" "1"

# Abandoned slots 2-9 (their checkouts are gone); twelve new checkouts start at once.
for n in 2 3 4 5 6 7 8 9; do mkdir -p "$T/slots/$n" && printf '%s\n' "$T/gone-$n" > "$T/slots/$n/owner"; done
for n in 1 2 3 4 5 6 7 8 9 10 11 12; do newrepo "$T/r$n"; done
for n in 1 2 3 4 5 6 7 8 9 10 11 12; do prep "$T/r$n" DELTA_PREPARE_INDEXES=0 > "$T/r$n.out" & done
wait
slots=$(for n in 1 2 3 4 5 6 7 8 9 10 11 12; do slot_of "$T/r$n"; done | sort -n | tr '\n' ' ')
check "concurrent reclaims: every checkout a different slot" "$slots" "2 3 4 5 6 7 8 9 10 11 12 13 "
owners=$(for n in $slots; do cat "$T/slots/$n/owner"; done | sort -u | wc -l | tr -d ' ')
check "concurrent reclaims: each slot names its checkout" "$owners" "12"
check "no reclaim lock left" "$( [ -e "$T/slots/.reclaim.lock" ] && echo left || echo none)" "none"

[ "$fail" = 0 ] && echo "ALL PASS" || echo "SOME FAILED"
[ "$fail" = 0 ]
