#!/bin/bash
# Tests for read_units() in scripts/lib-fingerprint.sh.
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

if (( fail )); then
    printf '\n%d test(s) failed\n' "$fail"; exit 1
fi
printf '\nall tests passed\n'
