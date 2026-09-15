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

# --- system units must exec a system binary, not a script under /home -------
# SELinux is Enforcing here and a SYSTEM unit runs as init_t, which may not
# *execute* a file labelled user_home_t -- it dies at status=203/EXEC with
# `avc: denied { execute }` before the script's first line runs. init_t may
# still *read* user_home_t, so the working form is to exec an interpreter
# (bin_t) and pass the script as an argument:
#     ExecStart=/usr/bin/bash /home/<user>/<repo>/scripts/foo.sh --apply
# USER units are exempt: they run in the invoking user's domain, not init_t,
# so casket-auto-update.service may name its %h script directly.
# Regression guard for the 2026-09-15 outage, where casket-mounts.service
# named its script directly, failed 203/EXEC at every boot, left 0 of 87
# squashfs mounted, and took OpenGrok's :8080 /xref/ down with it for 2 days.
for f in "$ROOT"/systemd/*.service; do
    [[ -f "$f" ]] || continue
    name="$(basename "$f")"
    # A unit is a USER unit if it uses %h (system units cannot) or installs
    # into default.target rather than multi-user.target. Comments are stripped
    # first -- casket-mounts.service *mentions* %h in a comment explaining that
    # a system unit cannot use it, and matching that would skip the very unit
    # this check exists for.
    directives="$(grep -vE '^[[:space:]]*#' "$f")"
    if grep -qE '%h' <<<"$directives" || grep -qE '^WantedBy=default\.target' <<<"$directives"; then
        pass "$name is a user unit (init_t exec rule does not apply)"
        continue
    fi
    unit_ok=1
    while IFS= read -r line; do
        val="${line#*=}"
        val="${val#"${val%%[![:space:]]*}"}"
        # strip systemd's special ExecStart prefixes (- @ + ! :)
        while [[ "$val" == [-@+!:]* ]]; do val="${val:1}"; done
        first="${val%% *}"
        [[ -z "$first" ]] && continue
        case "$first" in
            /usr/*|/bin/*|/sbin/*) ;;
            *)
                bad "$name: system unit execs '$first' -- must exec a system binary (e.g. /usr/bin/bash) and pass the script as an argument, or init_t dies at 203/EXEC under SELinux"
                unit_ok=0
                ;;
        esac
    done < <(grep -E '^Exec(Start|StartPre|StartPost|Stop|StopPost|Reload)=' <<<"$directives")
    (( unit_ok )) && pass "$name execs a system binary"
done

# --- the installer must not undo that ---------------------------------------
# install-mounts-service.sh rewrites ExecStart with the checkout's absolute
# path, so a correct template alone is not enough -- the sed replacement has
# to keep the interpreter too, or a reinstall silently reintroduces the bug.
installer="$ROOT/scripts/install-mounts-service.sh"
if [[ -f "$installer" ]]; then
    if grep -qE 's\|\^ExecStart=\.\*\|ExecStart=(/usr/bin/|/bin/)' "$installer"; then
        pass "install-mounts-service.sh keeps an interpreter in ExecStart"
    else
        bad "install-mounts-service.sh rewrites ExecStart without an interpreter -- a reinstall would reintroduce the 203/EXEC failure"
    fi
fi

if (( fail )); then
    printf '\n%d test(s) failed\n' "$fail"; exit 1
fi
printf '\nall tests passed\n'
