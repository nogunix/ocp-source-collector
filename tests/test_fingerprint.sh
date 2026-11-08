#!/bin/bash
# Tests for read_units(), latest_patch_in_graph() and fetch_patch() in
# scripts/lib-fingerprint.sh (network-free: curl is stubbed).
#
# Run: bash tests/test_fingerprint.sh   (exit 0 = pass)
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# shellcheck source=../scripts/lib.sh
source "$HERE/../scripts/lib.sh"
# shellcheck source=../scripts/lib-fingerprint.sh
source "$HERE/../scripts/lib-fingerprint.sh"
set +e +o pipefail

fail=0
pass() { printf '  ok   %s\n' "$1"; }
bad()  { printf '  FAIL %s\n' "$1"; fail=$((fail+1)); }

eq() {
    if [[ "$2" == "$3" ]]; then pass "$1"; else
        bad "$1"; printf '         expected: %s\n         actual:   %s\n' "$2" "$3"
    fi
}

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# --- read_units: basic config file -------------------------------------------
cat > "$TMP/minors.txt" << 'EOF'
# comment line
4.14
4.15

4.16
  # indented comment
4.17
EOF

got=$(read_units "$TMP/minors.txt" | tr '\n' ',')
eq "read_units skips comments and blanks" "4.14,4.15,4.16,4.17," "$got"

# --- read_units: empty file --------------------------------------------------
: > "$TMP/empty.txt"
got=$(read_units "$TMP/empty.txt" 2>/dev/null)
eq "read_units on empty file produces nothing" "" "$got"

# --- read_units: all comments -------------------------------------------------
cat > "$TMP/comments.txt" << 'EOF'
# only comments
# nothing else
EOF
got=$(read_units "$TMP/comments.txt" 2>/dev/null)
eq "read_units on all-comment file produces nothing" "" "$got"

# --- read_units: single line, no trailing newline ----------------------------
printf '4.20' > "$TMP/single.txt"
got=$(read_units "$TMP/single.txt")
eq "read_units on single line without newline" "4.20" "$got"

# --- latest_patch_in_graph ----------------------------------------------------
# stable-5.0 also lists 4.22.x (upgrade sources); candidate channels carry rcs.
cat > "$TMP/graph.json" << 'EOF'
{"nodes":[{"version":"4.22.21"},{"version":"4.22.22"},{"version":"5.0.0-rc.8"},
          {"version":"5.0.0"},{"version":"5.0.1"}],"edges":[[0,3],[1,4],[3,4]]}
EOF
eq "graph: picks the minor's patch, not the previous minor's" \
   "5.0.1" "$(latest_patch_in_graph 5.0 < "$TMP/graph.json")"
eq "graph: previous minor filtered by prefix" \
   "4.22.22" "$(latest_patch_in_graph 4.22 < "$TMP/graph.json")"

echo '{"nodes":[{"version":"4.22.9"},{"version":"4.22.22"},{"version":"4.22.10"}]}' > "$TMP/num.json"
eq "graph: numeric compare (4.22.22 > 4.22.9)" \
   "4.22.22" "$(latest_patch_in_graph 4.22 < "$TMP/num.json")"

echo '{"nodes":[{"version":"4.2.30"},{"version":"4.22.1"}]}' > "$TMP/prefix.json"
eq "graph: 4.2 does not match 4.22.x" \
   "4.2.30" "$(latest_patch_in_graph 4.2 < "$TMP/prefix.json")"

echo '{"nodes":[{"version":"5.0.0-ec.1"},{"version":"5.0.0-rc.0"}]}' > "$TMP/rc.json"
eq "graph: pre-releases only -> nothing" "" "$(latest_patch_in_graph 5.0 < "$TMP/rc.json")"
eq "graph: empty node list -> nothing" "" "$(echo '{"nodes":[]}' | latest_patch_in_graph 5.0)"
eq "graph: non-JSON (error page) -> nothing" "" "$(echo '<html>502</html>' | latest_patch_in_graph 5.0)"
eq "graph: empty input -> nothing" "" "$(latest_patch_in_graph 5.0 < /dev/null)"

# --- fetch_patch: graph first, mirror fallback --------------------------------
# curl is stubbed per case: GRAPH_BODY / MIRROR_BODY are what each endpoint
# returns; "FAIL" makes that endpoint fail like a network error.
curl() {
    local url="${*: -1}" body
    case "$url" in
        *upgrades_info*) body="$GRAPH_BODY" ;;
        *release.txt)    body="$MIRROR_BODY" ;;
        *)               return 99 ;;
    esac
    [[ "$body" == FAIL ]] && return 22
    printf '%s\n' "$body"
}
MIRROR_TXT=$'Name:      4.22.18\n  Version:  4.22.18\n'

GRAPH_BODY=$(cat "$TMP/num.json") MIRROR_BODY="$MIRROR_TXT"
eq "fetch_patch: graph wins over a stale mirror" "4.22.22" "$(fetch_patch 4.22)"

GRAPH_BODY=FAIL MIRROR_BODY="$MIRROR_TXT"
eq "fetch_patch: graph down -> mirror" "4.22.18" "$(fetch_patch 4.22)"

GRAPH_BODY='{"nodes":[]}' MIRROR_BODY="$MIRROR_TXT"
eq "fetch_patch: graph has no GA patch -> mirror" "4.22.18" "$(fetch_patch 4.22)"

GRAPH_BODY=FAIL MIRROR_BODY=FAIL
got=$(set -o pipefail; fetch_patch 5.0); rc=$?
eq "fetch_patch: both down -> empty" "" "$got"
if (( rc != 0 )); then pass "fetch_patch: both down -> non-zero (callers' '|| continue')"
else bad "fetch_patch: both down -> non-zero (callers' '|| continue')"; fi
unset -f curl

if (( fail )); then
    printf '\n%d test(s) failed\n' "$fail"; exit 1
fi
printf '\nall tests passed\n'
