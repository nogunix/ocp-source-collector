#!/bin/bash
# Regression tests for the artifact-path handoff in scripts/lib.sh
# (record_artifact / artifact_path_from).
#
# Why this exists: casket-build.sh used to re-derive the packaging script's
# date-stamped output name with a SECOND `date -u`. Both sides were UTC, so
# this was not the local-vs-UTC bug package.sh already documents -- it was the
# same expression evaluated hours apart. b-operand 4.19 on 2026-08-16 built
# for 8h50m, straddled a UTC midnight, produced casket-20260815-... and was
# looked for as casket-20260816-..., so a perfectly good 13G casket was
# reported BUILD-FAILED and left unregistered.
#
# The fallback is the part that must not regress: a packaging script invoked
# without --artifact-out (every manual run) still has to work.
#
# Run: tests/test_artifact_path.sh   (exit 0 = pass)
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# lib.sh sets -euo pipefail and resolves CASKET_WORK; neither matters here.
# shellcheck source=../scripts/lib.sh
source "$HERE/../scripts/lib.sh"
set +e +o pipefail

fail=0
pass() { printf '  ok   %s\n' "$1"; }
bad()  { printf '  FAIL %s\n' "$1"; fail=$((fail+1)); }

eq() {  # eq <desc> <expected> <actual>
    if [[ "$2" == "$3" ]]; then pass "$1"; else
        bad "$1"; printf '         expected: %s\n         actual:   %s\n' "$2" "$3"
    fi
}

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

REAL="/mnt/hdd/casket-ocp/casket-20260815-layered-ocp4.19.sqfs.xz"
GUESS="/mnt/hdd/casket-ocp/casket-20260816-layered-ocp4.19.sqfs.xz"

# --- record_artifact ------------------------------------------------------
f="$TMP/a"
record_artifact "$REAL" "$f"
eq "record_artifact writes the path" "$REAL" "$(cat "$f")"

record_artifact "$REAL" ""
eq "record_artifact with no file is a no-op, not an error" "0" "$?"

# a manual run passes no second argument at all
record_artifact "$REAL"
eq "record_artifact with the arg omitted is a no-op" "0" "$?"

# rewriting must replace, not append (a re-run in the same work dir)
record_artifact "$GUESS" "$f"
eq "record_artifact truncates on rewrite" "$GUESS" "$(cat "$f")"
eq "  ... leaving exactly one line" "1" "$(wc -l < "$f")"

# --- artifact_path_from ---------------------------------------------------
# THE bug: the producer's name wins over the caller's re-derived guess.
printf '%s\n' "$REAL" > "$f"
eq "reported path beats the caller's guess" "$REAL" "$(artifact_path_from "$f" "$GUESS")"

eq "missing file falls back" "$GUESS" "$(artifact_path_from "$TMP/nope" "$GUESS")"

: > "$TMP/empty"
eq "empty file falls back" "$GUESS" "$(artifact_path_from "$TMP/empty" "$GUESS")"

printf '\n' > "$TMP/blank"
eq "blank line falls back" "$GUESS" "$(artifact_path_from "$TMP/blank" "$GUESS")"

eq "empty filename argument falls back" "$GUESS" "$(artifact_path_from "" "$GUESS")"

# a packaging script that logged extra lines: take the first, never concatenate
printf '%s\n%s\n' "$REAL" "trailing junk" > "$TMP/multi"
eq "only the first line is used" "$REAL" "$(artifact_path_from "$TMP/multi" "$GUESS")"

# no trailing newline (printf '%s' or a truncated write)
printf '%s' "$REAL" > "$TMP/nonl"
eq "path without a trailing newline still reads" "$REAL" "$(artifact_path_from "$TMP/nonl" "$GUESS")"

# paths with spaces must survive the round trip unquoted-split-free
SPACED="$TMP/dir with spaces/casket-20260815-ocp4.19.sqfs.xz"
record_artifact "$SPACED" "$f"
eq "a path containing spaces round-trips" "$SPACED" "$(artifact_path_from "$f" "$GUESS")"

if (( fail )); then
    printf '\n%d test(s) failed\n' "$fail"; exit 1
fi
printf '\nall tests passed\n'
