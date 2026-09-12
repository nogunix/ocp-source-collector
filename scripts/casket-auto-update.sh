#!/bin/bash
# Unattended freshness-driven rebuild: for every enabled phase/unit, compare
# the current upstream fingerprint against the registry and, when it moved,
# run casket-build.sh --apply to stage a fresh casket. The production swap
# stays human-gated (casket-swap.sh --apply) per the boundary documented in
# docs/operations.md — this script never touches fstab or mounts.
#
# Designed to run from the casket-auto-update.timer systemd user unit (see
# systemd/ in the repo). Also fine to run by hand.
#
# Cadence (--min-interval-days N): the timer wakes weekly, but since 2026-09-12
# production only wants a run every 3 weeks, and systemd OnCalendar cannot say
# "every 3rd week". So the unit passes --min-interval-days 21 and the run skips
# itself (exit 0, one journal line) unless that long has passed since the last
# COMPLETED apply run, recorded in state/auto-update.last-run. Stamping at the
# end, not the start, is deliberate: a run killed by TimeoutStartSec leaves no
# stamp and is retried at the next weekly wake instead of waiting 3 more weeks.
# --force bypasses the guard for a manual `systemctl --user start`.
#
# Skip logic (why we don't rebuild in a loop while waiting for the swap):
#   current == live fingerprint    -> fresh, nothing to do
#   current == staged fingerprint  -> already built, awaiting human swap
#   otherwise                      -> build + register staged
#
# Enabled phases come from config/auto-update-phases.txt (one per line).
# a-rpm is intentionally not automatable: the SRPM collection needs a
# subscribed RHEL 9 VM and an interactive login (docs/pipeline.md).
#
# Output: journald via stdout (systemd captures it) + a one-line-per-unit
# summary in state/auto-update.status for quick "what's pending" checks.
# Exit: non-zero if any build failed or any fingerprint was unreachable
# (so the systemd unit shows as failed and is visible in systemctl).
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"
# shellcheck source=lib-fingerprint.sh
source "$SCRIPT_DIR/lib-fingerprint.sh"
# shellcheck source=lib-freshness.sh
source "$SCRIPT_DIR/lib-freshness.sh"   # interval_elapsed (cadence guard)
set +e +o pipefail

require_cmd curl awk jq oc sha256sum python3 flock

# collect-submodules.py reads the pinned submodule commit from the GitHub
# trees API (mode 160000); nothing else exposes it. Unauthenticated that API
# allows 60 requests/hour while a single operators casket needs ~70, so
# without a token the run does not FAIL -- it quietly falls back to
# .gitmodules branch heads and records every row exact=0 (APPROX), which is
# worse than failing because it looks like success.
#
# Prefer an explicitly exported token (CI, or the unit's EnvironmentFile);
# otherwise borrow the gh CLI's credential. Taking it live rather than
# copying it into a file means there is no second copy on disk to go stale
# behind a `gh auth login`. A user unit runs as the invoking user, so
# ~/.config/gh is readable and gh is on the unit's PATH.
if [[ -z "${GITHUB_TOKEN:-}" && -z "${GH_TOKEN:-}" ]] && command -v gh >/dev/null 2>&1; then
    GITHUB_TOKEN="$(gh auth token 2>/dev/null)"
    [[ -n "$GITHUB_TOKEN" ]] && export GITHUB_TOKEN
fi
if [[ -z "${GITHUB_TOKEN:-}" && -z "${GH_TOKEN:-}" ]]; then
    log "WARNING: no GITHUB_TOKEN/GH_TOKEN and no usable gh credential —"
    log "WARNING:   submodule pins will fall back to branch heads (exact=0)"
fi

DRY=1
MIN_INTERVAL_DAYS=0
FORCE=0
while (( $# )); do
    case "$1" in
        --apply) DRY=0; shift ;;
        --min-interval-days) MIN_INTERVAL_DAYS="${2:-}"; shift 2 ;;
        --force) FORCE=1; shift ;;
        -h|--help)
            echo "casket-auto-update.sh [--apply] [--min-interval-days N] [--force]"
            echo "  default: dry-run, report only"
            echo "  --min-interval-days N  skip unless N days since the last completed apply run"
            echo "  --force                ignore --min-interval-days"
            exit 0 ;;
        *) die "unknown argument: $1" ;;
    esac
done

PHASES_FILE="$CONFIG_DIR/auto-update-phases.txt"
STATUS_FILE="$CASKET_WORK/state/auto-update.status"
STAMP_FILE="$CASKET_WORK/state/auto-update.last-run"
LOCK_FILE="$CASKET_WORK/state/auto-update.lock"
[[ -f "$PHASES_FILE" ]] || die "missing $PHASES_FILE"

# Serialize whole runs: a second timer firing (or a manual run) while builds
# are still going must wait, not interleave with a half-written registry.
exec 9>"$LOCK_FILE"
flock -n 9 || { log "another auto-update run holds $LOCK_FILE; exiting"; exit 0; }

# Cadence guard. Only a real (--apply) run is rate-limited: a dry-run costs
# nothing but fingerprint fetches and is the documented way to ask "what would
# it build?" at any time.
if (( ! DRY && ! FORCE )); then
    last=$(stat -c %Y "$STAMP_FILE" 2>/dev/null) || last=0
    last=${last:-0}
    now=$(date -u +%s)
    if ! interval_elapsed "$last" "$now" "$MIN_INTERVAL_DAYS"; then
        log "skipping: last completed run was $(( (now - last) / 86400 ))d ago," \
            "below --min-interval-days $MIN_INTERVAL_DAYS (--force to override)"
        exit 0
    fi
fi

FAIL=0
SUMMARY=""

registry_fp() {
    # registry_fp <phase> <unit> <status>
    python3 "$REGISTRY_PY" get --phase "$1" --unit "$2" --status "$3" 2>/dev/null \
        | jq -r '.fingerprint // empty'
}

note() {
    # note <phase> <unit> <verdict> <detail>
    log "$1/$2: $3 ($4)"
    SUMMARY+="$(printf '%-10s %-8s %-14s %s' "$1" "$2" "$3" "$4")"$'\n'
}

process_unit() {
    local phase="$1" unit="$2" current="$3"
    if [[ -z "$current" ]]; then
        note "$phase" "$unit" UNREACHABLE "fingerprint fetch failed"; FAIL=1; return
    fi
    local live staged
    live=$(registry_fp "$phase" "$unit" live)
    staged=$(registry_fp "$phase" "$unit" staged)
    if [[ "$current" == "$live" ]]; then
        note "$phase" "$unit" fresh "live=$live"; return
    fi
    if [[ "$current" == "$staged" ]]; then
        note "$phase" "$unit" awaiting-swap "staged=$staged; run casket-swap.sh --phase $phase --unit $unit --apply"; return
    fi
    if (( DRY )); then
        note "$phase" "$unit" would-build "current=$current live=${live:-<none>} (dry-run)"; return
    fi
    log "$phase/$unit: stale (current=$current live=${live:-<none>}) -> building"
    if "$SCRIPT_DIR/casket-build.sh" --phase "$phase" --unit "$unit" --apply; then
        note "$phase" "$unit" built "staged $current; awaiting swap"
    else
        note "$phase" "$unit" BUILD-FAILED "see journal"; FAIL=1
    fi
}

while IFS= read -r phase; do
    case "$phase" in
        a)
            while IFS= read -r minor; do
                process_unit a "$minor" "$(fetch_patch "$minor")"
            done < <(read_units "$CONFIG_DIR/minors.txt") ;;
        b|b-operand)
            while IFS= read -r minor; do
                process_unit "$phase" "$minor" "$(fetch_catalog_digest "$minor")"
            done < <(read_units "$CONFIG_DIR/minors.txt") ;;
        a-rpm)
            # Check-only: never auto-build (needs the subscribed VM).
            current=$(phase_a_rpm_fingerprint)
            live=$(registry_fp a-rpm all live)
            if [[ -n "$current" && "$current" != "$live" ]]; then
                note a-rpm all manual-needed "run the A-rpm pipeline by hand (docs/pipeline.md)"
            else
                note a-rpm all fresh "live=${live:-<none>}"
            fi ;;
        *) die "unknown phase in $PHASES_FILE: $phase" ;;
    esac
done < <(read_units "$PHASES_FILE")

{
    echo "# casket-auto-update run $(date -Is)"
    printf '%s' "$SUMMARY"
} > "$STATUS_FILE"
log "summary written to $STATUS_FILE"

# Stamp a completed apply run, failures included: one unit that cannot build
# must not silently demote the whole schedule back to weekly. A run that never
# reaches this line (killed, timed out) leaves the old stamp and is retried at
# the next weekly wake.
if (( ! DRY )); then
    touch "$STAMP_FILE"
    log "cadence stamp updated: $STAMP_FILE"
fi

exit "$FAIL"
