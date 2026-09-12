#!/bin/bash
# Validate config/ and systemd/ file syntax.
#
# - config/*.txt / *.tsv: no trailing whitespace, no Windows line endings,
#   UTF-8 clean, tab-separated where expected
# - systemd/*.service / *.timer: systemd-analyze verify (basic parse check)
#
# Run: bash tests/test_config_syntax.sh   (exit 0 = pass)
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$HERE")"

fail=0
pass() { printf '  ok   %s\n' "$1"; }
bad()  { printf '  FAIL %s\n' "$1"; fail=$((fail+1)); }

# --- no Windows line endings (CR) in config files ---------------------------
cr_found=0
for f in "$ROOT"/config/*; do
    [[ -f "$f" ]] || continue
    if grep -qP '\r' "$f" 2>/dev/null; then
        bad "CR line ending in $(basename "$f")"
        cr_found=1
    fi
done
(( cr_found )) || pass "no Windows line endings in config/"

# --- no trailing whitespace in config files ---------------------------------
trail_found=0
for f in "$ROOT"/config/*; do
    [[ -f "$f" ]] || continue
    if grep -qP '[ \t]+$' "$f" 2>/dev/null; then
        bad "trailing whitespace in $(basename "$f")"
        trail_found=1
    fi
done
(( trail_found )) || pass "no trailing whitespace in config/"

# --- TSV files must be tab-separated (no commas where tabs are expected) ----
for f in "$ROOT"/config/*.tsv; do
    [[ -f "$f" ]] || continue
    name="$(basename "$f")"
    # every non-comment, non-blank line should contain at least one tab
    bad_lines=0
    while IFS= read -r line; do
        [[ -z "$line" || "$line" == \#* ]] && continue
        if [[ "$line" != *$'\t'* ]]; then
            bad_lines=$((bad_lines + 1))
        fi
    done < "$f"
    if (( bad_lines )); then
        bad "$name: $bad_lines line(s) without tabs"
    else
        pass "$name is tab-separated"
    fi
done

# --- config text files are valid UTF-8 --------------------------------------
utf8_ok=1
for f in "$ROOT"/config/*; do
    [[ -f "$f" ]] || continue
    if ! iconv -f UTF-8 -t UTF-8 "$f" >/dev/null 2>&1; then
        bad "$(basename "$f") is not valid UTF-8"
        utf8_ok=0
    fi
done
(( utf8_ok )) && pass "all config files are valid UTF-8"

# --- systemd unit files: basic section structure ----------------------------
for f in "$ROOT"/systemd/*.service "$ROOT"/systemd/*.timer; do
    [[ -f "$f" ]] || continue
    name="$(basename "$f")"
    # Must contain [Unit] section
    if grep -q '^\[Unit\]' "$f"; then
        pass "$name has [Unit] section"
    else
        bad "$name missing [Unit] section"
    fi
    # .service must have [Service], .timer must have [Timer]
    case "$name" in
        *.service)
            if grep -q '^\[Service\]' "$f"; then
                pass "$name has [Service] section"
            else
                bad "$name missing [Service] section"
            fi
            ;;
        *.timer)
            if grep -q '^\[Timer\]' "$f"; then
                pass "$name has [Timer] section"
            else
                bad "$name missing [Timer] section"
            fi
            ;;
    esac
    # No Windows line endings
    if grep -qP '\r' "$f" 2>/dev/null; then
        bad "$name has Windows line endings"
    else
        pass "$name has Unix line endings"
    fi
done

if (( fail )); then
    printf '\n%d test(s) failed\n' "$fail"; exit 1
fi
printf '\nall tests passed\n'
