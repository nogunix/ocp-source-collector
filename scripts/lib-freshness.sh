#!/bin/bash
# The freshness verdict for casket-check.sh, split out so it can be tested
# without any network (same pattern as lib-reclaim.sh / lib-stage.sh).
#
# Why this is not just "current != recorded":
#
# b and b-operand are fingerprinted by the redhat-operator-index manifest
# digest, and that digest is rebuilt upstream roughly daily. A plain equality
# test therefore reports STALE almost permanently -- on 2026-08-10, seven of
# the nine minors it flagged had been built that same day, and 4.14-4.17 went
# from "fresh" to "STALE" within one hour with no build in between. A check
# that is always red tells you nothing on the day something is actually wrong,
# so the digest alone must not drive the verdict.
#
# Two modes:
#
#   exact     a, a-rpm. The fingerprint is a release version (or a hash of
#             version pairs). It only moves when there really is new content,
#             so any difference is immediately STALE and age is irrelevant --
#             a minor that has not shipped a z-stream in two months is not
#             stale, it is just quiet.
#
#   drifting  b, b-operand. A difference means "the catalog moved", which is
#             true most days and is not by itself a reason to spend ~6h
#             rebuilding. It becomes STALE only once the live artifact is also
#             older than the age window. If the digest matches there is nothing
#             to rebuild at all, so age cannot make it stale on its own.
#
# The window default is set against the auto-update cadence, which moved from
# weekly to every 3 weeks on 2026-09-12 (systemd/casket-auto-update.timer):
# the live artifact is at most 21 days + the build itself (b-operand runs can
# take ~3 days) old when the next cycle starts, so 28 days leaves a few days of
# slack and a healthy cadence still never trips it. A single missed or killed
# run puts the artifact at ~42 days and does -- which is the whole point, and
# is exactly what happened on 2026-08-07 and again on 08-08 when the disk
# filled. Re-size this whenever the timer changes; the two numbers are one
# decision, and a window below the cadence makes the check permanently red
# again (the failure mode this file exists to fix).

: "${CASKET_STALE_AFTER_DAYS:=28}"

# freshness_verdict <mode> <current> <recorded> <built_at_epoch> <now_epoch> <max_age_days>
#
# Echoes exactly one of: fresh | drifted | STALE | MISSING | UNREACHABLE
# Returns 0 when the verdict is an OK one (fresh/drifted), 1 otherwise, so
# callers can use it directly to accumulate an exit status.
freshness_verdict() {
    local mode="$1" current="$2" recorded="$3" built="$4" now="$5" maxdays="$6"

    if [[ -z "$current" ]]; then echo "UNREACHABLE"; return 1; fi
    if [[ -z "$recorded" ]]; then echo "MISSING";     return 1; fi

    if [[ "$current" == "$recorded" ]]; then echo "fresh"; return 0; fi

    # Fingerprints differ from here on.
    if [[ "$mode" != "drifting" ]]; then echo "STALE"; return 1; fi

    # A missing or unparseable built_at must not silently read as "brand new".
    if [[ -z "$built" || -z "$now" || "$built" -le 0 ]] 2>/dev/null; then
        echo "STALE"; return 1
    fi

    local age_days=$(( (now - built) / 86400 ))
    if (( age_days > maxdays )); then echo "STALE"; return 1; fi
    echo "drifted"; return 0
}

# age_days_of <iso8601> [now_epoch]  -- "" when the timestamp is unusable.
age_days_of() {
    local iso="$1" now="${2:-$(date -u +%s)}" built
    [[ -n "$iso" ]] || { echo ""; return 1; }
    built=$(date -u -d "$iso" +%s 2>/dev/null) || { echo ""; return 1; }
    [[ -n "$built" ]] || { echo ""; return 1; }
    echo $(( (now - built) / 86400 ))
}

# interval_elapsed <last_run_epoch> <now_epoch> <min_days>
#
# The cadence guard behind casket-auto-update.sh's --min-interval-days. systemd
# OnCalendar has no "every 3rd week" syntax (it can express weekly, or day-of-
# month ranges that only approximate a month), so the timer keeps firing weekly
# and the run itself decides whether this week is its week.
#
# Returns 0 = run, 1 = skip. Fail-open everywhere: no stamp, an unreadable one,
# a non-numeric window or a stamp in the future (clock skew, a restored backup)
# all mean "run". A guard that parks the whole pipeline for weeks because a
# gitignored file went missing would be far worse than one extra build.
interval_elapsed() {
    local last="$1" now="$2" mindays="$3"
    [[ "$mindays" =~ ^[0-9]+$ ]] || return 0
    (( mindays > 0 )) || return 0
    [[ "$last" =~ ^[0-9]+$ && "$now" =~ ^[0-9]+$ ]] || return 0
    (( last > 0 )) || return 0
    (( last > now )) && return 0
    (( (now - last) / 86400 >= mindays ))
}
