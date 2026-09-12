#!/bin/bash
# Read-only freshness check across casket-ocp phases: for each tracked unit,
# fetch the current upstream fingerprint and compare it to the fingerprint
# recorded for the live registry entry. Never touches anything -- safe to run
# from cron/a systemd timer. Exits non-zero if anything is stale or missing
# (usable directly as an alert condition).
#
# Fingerprints per phase (see scripts/lib-fingerprint.sh for the fetchers).
# "a-rpm" and "b-operand" are sub-resolutions of a/b (same upstream artifact,
# one level deeper), not independent phases -- see README.md "運用" section:
#   a:         the current patch string for the minor (from the stable-<minor> channel)
#   b:         manifest digest of registry.redhat.io/redhat/redhat-operator-index:v<minor>
#   b-operand: same catalog digest as b -- phase-b-operand-discover.sh pulls the
#              identical index, just filtered to a package; reusing it avoids a
#              second query and only costs the rare false-positive rebuild of a
#              minor whose OWN 7 products didn't change but some unrelated
#              catalog entry did.
#   a-rpm:     sha256 of "<minor>:<patch>" pairs across config/phase-a-rpm-minors.txt
#              (a proxy for "the rhel-coreos image probably moved too" -- avoids
#              one `oc image info` per tracked minor just to confirm what a
#              patch bump already strongly implies).
#
# b/b-operand's catalog digest is rebuilt upstream roughly daily, so a plain
# equality test reported STALE almost permanently and the check stopped
# carrying information (2026-08-10: seven of the nine flagged minors had been
# built that same day). Those two phases are therefore judged "drifting": a
# moved digest alone prints `drifted` and exits 0, and only becomes STALE once
# the live artifact is also older than CASKET_STALE_AFTER_DAYS (default 28,
# sized so a healthy auto-update cadence -- every 3 weeks since 2026-09-12 --
# never trips it but one missed run does). a/a-rpm keep exact comparison -- their fingerprints only move when
# there is genuinely new content. Logic + tests: scripts/lib-freshness.sh,
# tests/test_freshness.sh.
#
# Usage: casket-check.sh [--phase a|a-rpm|b|b-operand|all] [--stale-after DAYS]
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"
# shellcheck source=lib-fingerprint.sh
source "$SCRIPT_DIR/lib-fingerprint.sh"
# shellcheck source=lib-freshness.sh
source "$SCRIPT_DIR/lib-freshness.sh"
# lib.sh turns on -e/pipefail; this script's whole point is to keep going
# past a failed network call for one unit and report it as UNREACHABLE.
set +e +o pipefail

PHASE="all"
while (( $# )); do
    case "$1" in
        --phase) PHASE="$2"; shift 2 ;;
        --stale-after) CASKET_STALE_AFTER_DAYS="$2"; shift 2 ;;
        -h|--help) echo "casket-check.sh [--phase a|a-rpm|b|b-operand|all] [--stale-after DAYS]"; exit 0 ;;
        *) die "unknown arg: $1" ;;
    esac
done
case "$PHASE" in a|a-rpm|b|b-operand|all) ;; *) die "bad --phase: $PHASE (want a|a-rpm|b|b-operand|all)" ;; esac
require_cmd curl awk jq oc sha256sum python3

STALE=0

report() {
    # report <phase> <unit> <current> <recorded> <mode> [built_at_iso]
    local phase="$1" unit="$2" current="$3" recorded="$4" mode="${5:-exact}" built_iso="${6:-}"
    local status now built age age_note=""
    now=$(date -u +%s)
    built=$(date -u -d "$built_iso" +%s 2>/dev/null) || built=""
    status=$(freshness_verdict "$mode" "$current" "$recorded" "${built:-0}" "$now" "$CASKET_STALE_AFTER_DAYS") || STALE=1
    age=$(age_days_of "$built_iso" "$now")
    [[ -n "$age" ]] && age_note=" age=${age}d"
    printf '%-10s %-8s %-10s current=%-70s registry=%s%s\n' \
        "$phase" "$unit" "$status" "$current" "${recorded:-<none>}" "$age_note"
}

registry_live() {
    # Echoes "<fingerprint>\t<built_at>" for the live entry, both possibly empty.
    local phase="$1" unit="$2"
    python3 "$REGISTRY_PY" get --phase "$phase" --unit "$unit" --status live 2>/dev/null \
        | jq -r '[(.fingerprint // ""), (.built_at // "")] | @tsv'
}

check_a() {
    local minor fp built
    while IFS= read -r minor; do
        IFS=$'\t' read -r fp built < <(registry_live a "$minor")
        report a "$minor" "$(fetch_patch "$minor")" "$fp" exact "$built"
    done < <(read_units "$CONFIG_DIR/minors.txt")
}

check_b() {
    local minor fp built
    while IFS= read -r minor; do
        IFS=$'\t' read -r fp built < <(registry_live b "$minor")
        report b "$minor" "$(fetch_catalog_digest "$minor")" "$fp" drifting "$built"
    done < <(read_units "$CONFIG_DIR/minors.txt")
}

check_b_operand() {
    local minor fp built
    while IFS= read -r minor; do
        IFS=$'\t' read -r fp built < <(registry_live b-operand "$minor")
        report b-operand "$minor" "$(fetch_catalog_digest "$minor")" "$fp" drifting "$built"
    done < <(read_units "$CONFIG_DIR/minors.txt")
}

check_a_rpm() {
    local fp built
    IFS=$'\t' read -r fp built < <(registry_live a-rpm all)
    report a-rpm all "$(phase_a_rpm_fingerprint)" "$fp" exact "$built"
}

case "$PHASE" in
    a) check_a ;;
    a-rpm) check_a_rpm ;;
    b) check_b ;;
    b-operand) check_b_operand ;;
    all) check_a; check_b; check_b_operand; check_a_rpm ;;
esac

exit "$STALE"
