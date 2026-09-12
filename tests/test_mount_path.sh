#!/bin/bash
# Tests for mount_path_for() in scripts/lib.sh.
#
# Run: bash tests/test_mount_path.sh   (exit 0 = pass)
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# shellcheck source=../scripts/lib.sh
source "$HERE/../scripts/lib.sh"
set +e +o pipefail

fail=0
pass() { printf '  ok   %s\n' "$1"; }
bad()  { printf '  FAIL %s\n' "$1"; fail=$((fail+1)); }

eq() {
    if [[ "$2" == "$3" ]]; then pass "$1"; else
        bad "$1"; printf '         expected: %s\n         actual:   %s\n' "$2" "$3"
    fi
}

# --- phase a (requires fingerprint = current patch) --------------------------
eq "phase a with fingerprint" \
    "/srv/sources-ocp4.20.22" \
    "$(mount_path_for a 4.20 4.20.22)"

# phase a without fingerprint should die — capture the exit code
out=$(mount_path_for a 4.20 2>&1)
rc=$?
[[ $rc -ne 0 ]] && pass "phase a without fingerprint exits non-zero" \
                 || bad  "phase a without fingerprint exits non-zero"

# --- phase a-rpm (single mount, ignores unit) --------------------------------
eq "phase a-rpm" \
    "/srv/sources-ocp-srpms" \
    "$(mount_path_for a-rpm all)"

eq "phase a-rpm with any unit value" \
    "/srv/sources-ocp-srpms" \
    "$(mount_path_for a-rpm 4.20)"

# --- phase b (minor -> operators suffix) -------------------------------------
eq "phase b 4.18" \
    "/srv/sources-ocp4.18-operators" \
    "$(mount_path_for b 4.18)"

eq "phase b 4.22" \
    "/srv/sources-ocp4.22-operators" \
    "$(mount_path_for b 4.22)"

# --- phase b-operand (minor -> layered prefix) -------------------------------
eq "phase b-operand 4.18" \
    "/srv/sources-layered-ocp4.18" \
    "$(mount_path_for b-operand 4.18)"

eq "phase b-operand 4.22" \
    "/srv/sources-layered-ocp4.22" \
    "$(mount_path_for b-operand 4.22)"

# --- unknown phase should die ------------------------------------------------
out=$(mount_path_for x 4.20 2>&1)
rc=$?
[[ $rc -ne 0 ]] && pass "unknown phase exits non-zero" \
                 || bad  "unknown phase exits non-zero"

if (( fail )); then
    printf '\n%d test(s) failed\n' "$fail"; exit 1
fi
printf '\nall tests passed\n'
