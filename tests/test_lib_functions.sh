#!/bin/bash
# Tests for parse_args_version_arch(), version_dir(), require_cmd(), and
# catalog_setup() in scripts/lib.sh.
#
# Run: bash tests/test_lib_functions.sh   (exit 0 = pass)
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# parse_args_version_arch calls print_usage on -h; stub it out
print_usage() { :; }

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

# ---- parse_args_version_arch ------------------------------------------------

# basic: -v sets VERSION, ARCH defaults to x86_64
(
    parse_args_version_arch -v 4.20.22
    [[ "$VERSION" == "4.20.22" && "$ARCH" == "x86_64" ]]
) 2>/dev/null
[[ $? -eq 0 ]] && pass "parse_args: -v sets VERSION, ARCH defaults to x86_64" \
                || bad  "parse_args: -v sets VERSION, ARCH defaults to x86_64"

# -v and -a together
(
    parse_args_version_arch -v 4.20 -a aarch64
    [[ "$VERSION" == "4.20" && "$ARCH" == "aarch64" ]]
) 2>/dev/null
[[ $? -eq 0 ]] && pass "parse_args: -v and -a set both" \
                || bad  "parse_args: -v and -a set both"

# missing -v should fail
(parse_args_version_arch) 2>/dev/null
[[ $? -ne 0 ]] && pass "parse_args: missing -v exits non-zero" \
                || bad  "parse_args: missing -v exits non-zero"

# invalid arch should fail
(parse_args_version_arch -v 4.20 -a sparc) 2>/dev/null
[[ $? -ne 0 ]] && pass "parse_args: invalid arch exits non-zero" \
                || bad  "parse_args: invalid arch exits non-zero"

# unknown arg should fail
(parse_args_version_arch -v 4.20 --bogus) 2>/dev/null
[[ $? -ne 0 ]] && pass "parse_args: unknown arg exits non-zero" \
                || bad  "parse_args: unknown arg exits non-zero"

# all valid arches should succeed
for a in x86_64 aarch64 ppc64le s390x multi; do
    (parse_args_version_arch -v 4.20 -a "$a") 2>/dev/null
    [[ $? -eq 0 ]] && pass "parse_args: arch $a accepted" \
                    || bad  "parse_args: arch $a accepted"
done

# ---- version_dir ------------------------------------------------------------

eq "version_dir 4.20.22" \
    "$CASKET_WORK/ocp4.20.22" \
    "$(version_dir 4.20.22)"

eq "version_dir 4.18" \
    "$CASKET_WORK/ocp4.18" \
    "$(version_dir 4.18)"

# ---- require_cmd ------------------------------------------------------------

# bash should always be available
(require_cmd bash) 2>/dev/null
[[ $? -eq 0 ]] && pass "require_cmd: bash found" \
                || bad  "require_cmd: bash found"

# nonexistent command should fail
(require_cmd nonexistent_cmd_xyz_12345) 2>/dev/null
[[ $? -ne 0 ]] && pass "require_cmd: nonexistent command fails" \
                || bad  "require_cmd: nonexistent command fails"

# multiple commands, all valid
(require_cmd bash cat) 2>/dev/null
[[ $? -eq 0 ]] && pass "require_cmd: multiple valid commands pass" \
                || bad  "require_cmd: multiple valid commands pass"

# multiple commands, one invalid
(require_cmd bash nonexistent_cmd_xyz_12345) 2>/dev/null
[[ $? -ne 0 ]] && pass "require_cmd: one invalid in list fails" \
                || bad  "require_cmd: one invalid in list fails"

# ---- catalog_setup ----------------------------------------------------------

# redhat (default)
(
    CATALOG=redhat
    catalog_setup
    [[ "$CATALOG_SUFFIX" == "" && "$CATALOG_INDEX" == *"redhat-operator-index" ]]
) 2>/dev/null
[[ $? -eq 0 ]] && pass "catalog_setup: redhat -> empty suffix, redhat-operator-index" \
                || bad  "catalog_setup: redhat -> empty suffix, redhat-operator-index"

# certified
(
    CATALOG=certified
    catalog_setup
    [[ "$CATALOG_SUFFIX" == "-certified" && "$CATALOG_INDEX" == *"certified-operator-index" ]]
) 2>/dev/null
[[ $? -eq 0 ]] && pass "catalog_setup: certified -> -certified suffix" \
                || bad  "catalog_setup: certified -> -certified suffix"

# community
(
    CATALOG=community
    catalog_setup
    [[ "$CATALOG_SUFFIX" == "-community" && "$CATALOG_INDEX" == *"community-operator-index" ]]
) 2>/dev/null
[[ $? -eq 0 ]] && pass "catalog_setup: community -> -community suffix" \
                || bad  "catalog_setup: community -> -community suffix"

# invalid catalog should fail
(
    CATALOG=invalid
    catalog_setup
) 2>/dev/null
[[ $? -ne 0 ]] && pass "catalog_setup: invalid CATALOG exits non-zero" \
                || bad  "catalog_setup: invalid CATALOG exits non-zero"

# ---- done -------------------------------------------------------------------

if (( fail )); then
    printf '\n%d test(s) failed\n' "$fail"; exit 1
fi
printf '\nall tests passed\n'
