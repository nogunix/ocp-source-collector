#!/bin/bash
# Regression tests for casket-swap.sh's staleness guard. Dry-run only, against
# a throwaway registry, so nothing is mounted.
#
# The bug being guarded: the staged entry swapped in is simply the highest
# entry_id, with no comparison against live. 8 b-operand builds from
# 2026-08-15..21 sat staged behind a live 08-29..09-01 build; any
# `casket-swap.sh --apply` on those units would have silently downgraded
# production.
#
# Run: tests/test_swap.sh   (exit 0 = pass)
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SWAP="$HERE/../scripts/casket-swap.sh"
REGISTRY_PY="$HERE/../scripts/registry.py"

fail=0
pass() { printf '  ok   %s\n' "$1"; }
bad()  { printf '  FAIL %s\n' "$1"; fail=$((fail+1)); }

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/state" "$TMP/hdd"

# entry <entry_id> <phase> <unit> <status> <built_at> <mount>
entry() {
    local art="$TMP/hdd/$1.sqfs.xz"
    echo x > "$art"
    printf '{"entry_id":%s,"id":"%s-%s","phase":"%s","unit":"%s","status":"%s","built_at":"%s","artifact_path":"%s","mount_path":"%s","fingerprint":"f%s","retired_at":null}' \
        "$1" "$2" "$3" "$2" "$3" "$4" "$5" "$art" "$6" "$1"
}
registry() {   # registry <entry-json>...
    local IFS=,
    printf '{"artifacts":[%s]}\n' "$*" > "$TMP/state/registry.json"
}
# swap <args...> -> sets OUT and RC
swap() {
    OUT=$(CASKET_WORK="$TMP" REGISTRY_PY="$REGISTRY_PY" bash "$SWAP" "$@" 2>&1); RC=$?
}
has()  { grep -qF -- "$1" <<<"$OUT"; }

echo "# replacement phases refuse a staged build older than live"
registry "$(entry 1 b 4.20 live   2026-09-01T00:00:00Z "$TMP/srv/b420")" \
         "$(entry 2 b 4.20 staged 2026-08-15T00:00:00Z "$TMP/srv/b420")"
swap --phase b --unit 4.20
{ (( RC != 0 )) && has "is OLDER than live"; } && pass "older staged is refused" \
    || { bad "older staged is refused"; echo "$OUT" | sed 's/^/       /'; }
has "mount -o loop" && bad "refusal happens before any mount step" || pass "refusal happens before any mount step"

swap --phase b --unit 4.20 --allow-older
{ (( RC == 0 )) && has "proceeding because of --allow-older"; } && pass "--allow-older overrides for a rollback" \
    || { bad "--allow-older overrides for a rollback"; echo "$OUT" | sed 's/^/       /'; }

echo "# newer staged, or nothing live yet, goes through"
registry "$(entry 1 b-operand 4.22 live   2026-09-01T00:00:00Z "$TMP/srv/l422")" \
         "$(entry 2 b-operand 4.22 staged 2026-09-07T00:00:00Z "$TMP/srv/l422")"
swap --phase b-operand --unit 4.22
{ (( RC == 0 )) && has "mount -o loop"; } && pass "newer staged is allowed" \
    || { bad "newer staged is allowed"; echo "$OUT" | sed 's/^/       /'; }

registry "$(entry 5 a-rpm all staged 2026-09-24T00:00:00Z "$TMP/srv/srpms")"
swap --phase a-rpm --unit all
(( RC == 0 )) && pass "no live entry: allowed" || { bad "no live entry: allowed"; echo "$OUT" | sed 's/^/       /'; }

echo "# phase a is additive: an older patch on a different mount replaces nothing"
registry "$(entry 1 a 4.20 live   2026-09-04T00:00:00Z "$TMP/srv/ocp4.20.35")" \
         "$(entry 2 a 4.20 staged 2026-08-01T00:00:00Z "$TMP/srv/ocp4.20.30")"
swap --phase a --unit 4.20
(( RC == 0 )) && pass "older patch on its own mountpoint is allowed" \
    || { bad "older patch on its own mountpoint is allowed"; echo "$OUT" | sed 's/^/       /'; }

echo "# leftover staged entries are named"
registry "$(entry 1 b-operand 4.18 live   2026-09-01T00:00:00Z "$TMP/srv/l418")" \
         "$(entry 2 b-operand 4.18 staged 2026-08-15T00:00:00Z "$TMP/srv/l418")" \
         "$(entry 3 b-operand 4.18 staged 2026-09-06T00:00:00Z "$TMP/srv/l418")"
swap --phase b-operand --unit 4.18
{ (( RC == 0 )) && has "never swapped: 2"; } && pass "older leftover staged entry_id is reported" \
    || { bad "older leftover staged entry_id is reported"; echo "$OUT" | sed 's/^/       /'; }

if (( fail )); then
    printf '\n%d test(s) failed\n' "$fail"; exit 1
fi
printf '\nall tests passed\n'
