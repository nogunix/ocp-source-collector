#!/bin/bash
# Regression tests for scripts/lib-freshness.sh — the verdict that decides when
# casket-check.sh cries wolf. Network-free.
#
# The bug being guarded: b/b-operand are fingerprinted by a catalog digest that
# upstream rebuilds roughly daily, so plain equality reported STALE almost
# permanently and the check stopped meaning anything.
#
# Run: tests/test_freshness.sh   (exit 0 = pass)
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../scripts/lib-freshness.sh
source "$HERE/../scripts/lib-freshness.sh"

fail=0
pass() { printf '  ok   %s\n' "$1"; }
bad()  { printf '  FAIL %s\n' "$1"; fail=$((fail+1)); }

NOW=1786000000          # fixed clock so these never drift
d() { echo $(( NOW - $1 * 86400 )); }   # d N -> epoch N days ago

# v <desc> <want> <mode> <current> <recorded> <built_epoch>
v() {
    local desc="$1" want="$2" got rc
    got=$(freshness_verdict "$3" "$4" "$5" "$6" "$NOW" 10); rc=$?
    if [[ "$got" != "$want" ]]; then
        bad "$desc"; printf '       got=%s want=%s\n' "$got" "$want"; return
    fi
    # Exit status must track the verdict, since callers OR it into $STALE.
    case "$want" in
        fresh|drifted) [[ $rc -eq 0 ]] || { bad "$desc (rc=$rc, want 0)"; return; } ;;
        *)             [[ $rc -eq 1 ]] || { bad "$desc (rc=$rc, want 1)"; return; } ;;
    esac
    pass "$desc"
}

echo "# exact mode (a, a-rpm) — any difference is stale, age is irrelevant"
v "matching version is fresh"                fresh   exact 4.22.8 4.22.8 "$(d 400)"
v "new z-stream is STALE immediately"        STALE   exact 4.22.9 4.22.8 "$(d 0)"
v "a quiet minor is not stale when matching" fresh   exact 4.14.58 4.14.58 "$(d 365)"

echo "# drifting mode (b, b-operand) — a moved digest alone is not stale"
v "digest moved today -> drifted"            drifted drifting sha:new sha:old "$(d 0)"
v "digest moved, 9d old -> still drifted"    drifted drifting sha:new sha:old "$(d 9)"
v "digest moved, exactly 10d -> drifted"     drifted drifting sha:new sha:old "$(d 10)"
v "digest moved, 11d old -> STALE"           STALE   drifting sha:new sha:old "$(d 11)"
v "digest moved, 14d old -> STALE"           STALE   drifting sha:new sha:old "$(d 14)"

echo "# drifting mode — matching digest is never stale, however old"
# Nothing to rebuild toward, so age alone must not raise an alarm.
v "matching digest at 400d is fresh"         fresh   drifting sha:same sha:same "$(d 400)"

echo "# unreachable / missing outrank everything"
v "empty current -> UNREACHABLE"             UNREACHABLE drifting "" sha:old "$(d 0)"
v "empty current beats missing recorded"     UNREACHABLE exact    "" ""      "$(d 0)"
v "no live entry -> MISSING"                 MISSING     drifting sha:new "" "$(d 0)"

echo "# a missing built_at must not read as brand new"
v "drifting with empty built_at -> STALE"    STALE   drifting sha:new sha:old ""
v "drifting with zero built_at -> STALE"     STALE   drifting sha:new sha:old 0
v "matching digest with no built_at is fresh" fresh  drifting sha:same sha:same ""

echo "# the window is configurable"
got=$(freshness_verdict drifting sha:new sha:old "$(d 11)" "$NOW" 30)
[[ "$got" == "drifted" ]] && pass "widening to 30d keeps an 11d artifact drifted" \
                           || bad "widening to 30d keeps an 11d artifact drifted (got $got)"
got=$(freshness_verdict drifting sha:new sha:old "$(d 2)" "$NOW" 1)
[[ "$got" == "STALE" ]] && pass "narrowing to 1d makes a 2d artifact STALE" \
                        || bad "narrowing to 1d makes a 2d artifact STALE (got $got)"

echo "# the default window tracks the auto-update cadence (3-weekly since 2026-09-12)"
# 21d cadence + up to ~3d of build time, so the live artifact can legitimately
# reach ~24d; a missed run reaches ~42d and must be caught. A default at or
# below the cadence would make b/b-operand permanently red again.
[[ "$CASKET_STALE_AFTER_DAYS" -gt 24 && "$CASKET_STALE_AFTER_DAYS" -lt 42 ]] \
    && pass "default window sits between one healthy cycle and a missed one" \
    || bad "default window $CASKET_STALE_AFTER_DAYS outside (24,42)"
got=$(freshness_verdict drifting sha:new sha:old "$(d 24)" "$NOW" "$CASKET_STALE_AFTER_DAYS")
[[ "$got" == "drifted" ]] && pass "a healthy 3-week cycle (24d) is not stale" \
                          || bad "a healthy 3-week cycle (24d) is not stale (got $got)"
got=$(freshness_verdict drifting sha:new sha:old "$(d 42)" "$NOW" "$CASKET_STALE_AFTER_DAYS")
[[ "$got" == "STALE" ]] && pass "a missed 3-week run (42d) is STALE" \
                        || bad "a missed 3-week run (42d) is STALE (got $got)"

echo "# interval_elapsed — the cadence guard the systemd unit relies on"
# i <desc> <want run|skip> <last_epoch> <min_days>
i() {
    local desc="$1" want="$2"
    if interval_elapsed "$3" "$NOW" "$4"; then got=run; else got=skip; fi
    [[ "$got" == "$want" ]] && pass "$desc" || bad "$desc (got $got, want $want)"
}
i "0 days in -> skip"                        skip "$(d 0)"  21
i "20 days in -> skip"                       skip "$(d 20)" 21
i "exactly 21 days -> run"                   run  "$(d 21)" 21
i "22 days -> run"                           run  "$(d 22)" 21
i "a missed cycle (45d) -> run"              run  "$(d 45)" 21
# Fail-open: the guard must never park the pipeline over a bad/absent stamp.
i "no stamp (0) -> run"                      run  0         21
i "empty stamp -> run"                       run  ""        21
i "garbage stamp -> run"                     run  "nope"    21
i "stamp in the future (clock skew) -> run"  run  $(( NOW + 86400 )) 21
i "window 0 disables the guard"              run  "$(d 0)"  0
i "empty window disables the guard"          run  "$(d 0)"  ""
i "garbage window disables the guard"        run  "$(d 0)"  "three"

echo "# age_days_of"
[[ "$(age_days_of "" "$NOW")" == "" ]] && pass "empty timestamp -> empty" || bad "empty timestamp -> empty"
[[ "$(age_days_of "not-a-date" "$NOW")" == "" ]] && pass "garbage timestamp -> empty" || bad "garbage timestamp -> empty"
[[ "$(age_days_of "2026-08-07T23:30:06Z" 1786145406)" == "0" ]] && pass "same-moment -> 0d" || bad "same-moment -> 0d"
[[ "$(age_days_of "2026-08-07T23:30:06Z" $((1786145406 + 5*86400)))" == "5" ]] && pass "5 days later -> 5d" || bad "5 days later -> 5d"

echo
if (( fail )); then echo "$fail test(s) FAILED"; exit 1; fi
echo "all tests passed"
