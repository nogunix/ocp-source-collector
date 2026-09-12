#!/bin/bash
# Regression tests for scripts/lib-reclaim.sh — the guard that decides which
# 50-out staging trees casket-build.sh may rm -rf. Network-free; operates only
# inside a throwaway tmpdir.
#
# This code deletes hundreds of gigabytes unattended, so the interesting tests
# are the refusals, not the happy path.
#
# Run: tests/test_reclaim.sh   (exit 0 = pass)
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

LOGGED=""
log() { LOGGED="$LOGGED$*"$'\n'; }

# shellcheck source=../scripts/lib-reclaim.sh
source "$HERE/../scripts/lib-reclaim.sh"

fail=0
pass() { printf '  ok   %s\n' "$1"; }
bad()  { printf '  FAIL %s\n' "$1"; fail=$((fail+1)); }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
ROOT="$TMP/work"

# ok <desc> <path>   -- stage_path_ok must accept
ok() {
    stage_path_ok "$ROOT" "$2" && pass "$1" || bad "$1 (should accept: $2)"
}
# no <desc> <path>   -- stage_path_ok must refuse
no() {
    stage_path_ok "$ROOT" "$2" && bad "$1 (should refuse: $2)" || pass "$1"
}

echo "# stage_path_ok — accepts the four real layouts"
ok "phase a:         ocp<patch>/50-out"              "$ROOT/ocp4.18.50/50-out"
ok "phase b:         phase-b/<minor>/50-out"         "$ROOT/phase-b/4.16/50-out"
ok "phase b-operand: phase-b-operand/<pkg>/<minor>/50-out" \
                                                     "$ROOT/phase-b-operand/rhods-operator/4.16/50-out"
ok "phase a-rpm:     phase-a-rpm/50-out"             "$ROOT/phase-a-rpm/50-out"

echo "# stage_path_ok — accepts b-operand's combined stage"
# combine.sh cp -al's this from the per-product stages, so reclaiming only the
# 50-out dirs would hand it sole ownership of the data instead of freeing it.
ok "b-operand combined: phase-b-operand/_layered/<minor>" \
                                                     "$ROOT/phase-b-operand/_layered/4.16"

echo "# stage_path_ok — refuses everything else"
no "_layered itself (would wipe every minor)" "$ROOT/phase-b-operand/_layered"
no "inside a combined stage"                  "$ROOT/phase-b-operand/_layered/4.16/stage"
no "deep inside a combined stage"             "$ROOT/phase-b-operand/_layered/4.16/stage/git/x"
no "_layered lookalike outside b-operand"     "$ROOT/phase-b/_layered/4.16"
no "the work root itself"                 "$ROOT"
no "a path outside the work root"         "/etc"
no "a sibling of the work root"           "$TMP/other/50-out"
no "a dir that merely starts with 50-out" "$ROOT/phase-b/4.16/50-out-keepme"
no "the fetched tarballs"                 "$ROOT/phase-b/4.16/20-git"
no "a stage's parent"                     "$ROOT/phase-b/4.16"
no "empty path"                           ""
no "root directory"                       "/"
# The reason the ".." check exists: in a bash [[ ]] pattern `*` matches slashes,
# so the prefix test alone accepts a traversal that still ends in /50-out.
no "traversal that still ends in /50-out" "$ROOT/../elsewhere/50-out"
no "traversal in the middle"              "$ROOT/phase-b/../../../tmp/50-out"

echo "# stage_path_ok — empty work root never matches"
stage_path_ok "" "$ROOT/ocp4.18.50/50-out" \
    && bad "empty work root is refused" || pass "empty work root is refused"

echo "# reclaim_stage — removes stages, keeps everything else"
mk() { mkdir -p "$ROOT/ocp4.18.50"/{50-out,10-git,00-discover} \
                "$ROOT/phase-b/4.16"/{50-out,20-git} \
                "$ROOT/phase-b-operand"/{prodA,prodB}/4.16/{50-out,20-git} \
                "$ROOT/phase-a-rpm"/{50-out,srpms} \
                "$ROOT/phase-b/4.16/50-out-keepme" "$ROOT/keepme"; }
rm -rf "$ROOT"; mk

CASKET_KEEP_STAGE=0
reclaim_stage "$ROOT" "$ROOT/ocp4.18.50/50-out"
[[ $RECLAIMED_COUNT == 1 && ! -d "$ROOT/ocp4.18.50/50-out" ]] \
    && pass "phase a stage removed, count=1" || bad "phase a stage removed, count=1"

reclaim_stage "$ROOT" "$ROOT"/phase-b-operand/*/4.16/50-out
[[ $RECLAIMED_COUNT == 2 ]] && pass "b-operand glob removes both products" \
                            || bad "b-operand glob removes both products (got $RECLAIMED_COUNT)"

# The real call site passes the combined stage alongside the glob; the hardlink
# farm must go in the same breath or the space is not actually returned.
mkdir -p "$ROOT/phase-b-operand/_layered/4.16/stage" "$ROOT/phase-b-operand/_layered/4.17/stage"
reclaim_stage "$ROOT" "$ROOT"/phase-b-operand/*/4.16/50-out \
                      "$ROOT/phase-b-operand/_layered/4.16"
[[ $RECLAIMED_COUNT == 1 && ! -d "$ROOT/phase-b-operand/_layered/4.16" ]] \
    && pass "combined stage removed alongside the per-product glob" \
    || bad "combined stage removed alongside the per-product glob"
[[ -d "$ROOT/phase-b-operand/_layered/4.17" ]] \
    && pass "another minor's combined stage is untouched" \
    || bad "another minor's combined stage is untouched"

# An unmatched glob arrives as the literal pattern; it must be a silent no-op.
reclaim_stage "$ROOT" "$ROOT"/phase-b-operand/*/9.99/50-out
[[ $RECLAIMED_COUNT == 0 ]] && pass "unmatched glob is a no-op" \
                            || bad "unmatched glob is a no-op (got $RECLAIMED_COUNT)"

reclaim_stage "$ROOT" "/etc" "$ROOT" "$ROOT/keepme" "$ROOT/phase-b/4.16/50-out-keepme"
[[ $RECLAIMED_COUNT == 0 && -d /etc && -d "$ROOT/keepme" && -d "$ROOT/phase-b/4.16/50-out-keepme" ]] \
    && pass "hostile paths all refused, nothing removed" \
    || bad "hostile paths all refused, nothing removed"

echo "# reclaim_stage — never touches fetched sources"
survived=1
for d in "$ROOT/ocp4.18.50/10-git" "$ROOT/ocp4.18.50/00-discover" \
         "$ROOT/phase-b/4.16/20-git" "$ROOT/phase-b-operand/prodA/4.16/20-git" \
         "$ROOT/phase-a-rpm/srpms"; do
    [[ -d "$d" ]] || { survived=0; echo "       missing: $d"; }
done
(( survived )) && pass "tarballs / discover / srpms all survive" \
               || bad "tarballs / discover / srpms all survive"

echo "# reclaim_stage — CASKET_KEEP_STAGE=1 is a full no-op"
rm -rf "$ROOT"; mk
CASKET_KEEP_STAGE=1
reclaim_stage "$ROOT" "$ROOT/ocp4.18.50/50-out"
[[ $RECLAIMED_COUNT == 0 && -d "$ROOT/ocp4.18.50/50-out" ]] \
    && pass "CASKET_KEEP_STAGE=1 keeps the stage" || bad "CASKET_KEEP_STAGE=1 keeps the stage"
CASKET_KEEP_STAGE=0

echo "# reclaim_stage — a refusal is reported, not silent"
LOGGED=""
reclaim_stage "$ROOT" "/etc"
[[ "$LOGGED" == *"refusing to remove unexpected stage path: /etc"* ]] \
    && pass "refusal is logged" || bad "refusal is logged"

echo
if (( fail )); then echo "$fail test(s) FAILED"; exit 1; fi
echo "all tests passed"
