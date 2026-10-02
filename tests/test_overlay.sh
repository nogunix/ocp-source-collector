#!/bin/bash
# Tests for scripts/lib-overlay.sh — the overlay repack shared by
# repackage-add-{index,deps,submodules}.sh. Network- and sudo-free: the parts
# that mount are not exercised here.
#
# Run: tests/test_overlay.sh   (exit 0 = pass)
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../scripts/lib.sh
source "$HERE/../scripts/lib.sh"
# shellcheck source=../scripts/lib-overlay.sh
source "$HERE/../scripts/lib-overlay.sh"
set +e +o pipefail

fail=0
pass() { printf '  ok   %s\n' "$1"; }
bad()  { printf '  FAIL %s\n' "$1"; fail=$((fail+1)); }
eq() {
    if [[ "$2" == "$3" ]]; then pass "$1"; else
        bad "$1"; printf '         expected: %s\n         actual:   %s\n' "$2" "$3"
    fi
}

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
seen() { printf '%s -> %s\n' "${1#"$TMP"/}" "${2#"$TMP"/}" >> "$TMP/seen"; }

echo "# overlay_each_unit"
mkdir -p "$TMP/single/git" "$TMP/layered/a/git" "$TMP/layered/b" "$TMP/layered/c/git"
UPPER="$TMP/up"
overlay_each_unit "$TMP/single" git seen
eq "a mount holding the marker is the one unit" "single -> up" "$(cat "$TMP/seen")"

rm -f "$TMP/seen"
overlay_each_unit "$TMP/layered" git seen
eq "layered: each product holding the marker, others skipped" \
    "layered/a -> up/a layered/c -> up/c" "$(paste -sd' ' "$TMP/seen")"

rm -f "$TMP/seen"
mkdir -p "$TMP/idx/p/meta"; : > "$TMP/idx/p/meta/MANIFEST.json"; mkdir -p "$TMP/idx/q/git"
overlay_each_unit "$TMP/idx" meta/MANIFEST.json seen
eq "the marker can be a file path" "idx/p -> up/p" "$(cat "$TMP/seen")"

echo "# overlay_check_flavor"
( overlay_check_flavor A && overlay_check_flavor B ) 2>/dev/null
eq "A and B are accepted" "0" "$?"
out=$( (overlay_check_flavor C) 2>&1 ); rc=$?
{ (( rc != 0 )) && [[ "$out" == *"unknown flavor: C"* ]]; } && pass "anything else dies (it used to fall through to B)" \
    || bad "anything else dies (it used to fall through to B)"

echo "# overlay_init / overlay_repack / overlay_cleanup"
out=$(
    export CASKET_REPACK_TMP="$TMP/rt"
    overlay_init
    printf '%s\n' "$UPPER" "$WORKD" "$MERGED" > "$TMP/dirs"
    overlay_repack "$TMP/single" "$TMP/out.sqfs.xz" B "nothing here" 2>&1
); rc=$?
{ (( rc == 0 )) && [[ "$out" == *"(nothing here) -- skipping repack"* && ! -e "$TMP/out.sqfs.xz" ]]; } \
    && pass "an empty upper exits 0 with the reason and builds nothing" \
    || { bad "an empty upper exits 0 with the reason and builds nothing"; printf '         rc=%s out=%s\n' "$rc" "$out"; }
[[ "$(head -1 "$TMP/dirs")" == "$TMP/rt/"* ]] && pass "temp dirs go under CASKET_REPACK_TMP" \
    || bad "temp dirs go under CASKET_REPACK_TMP"
left=0; while read -r d; do [[ -e "$d" ]] && left=1; done < "$TMP/dirs"
eq "the exit trap removes every temp dir" "0" "$left"

if (( fail )); then
    printf '\n%d test(s) failed\n' "$fail"; exit 1
fi
printf '\nall tests passed\n'
