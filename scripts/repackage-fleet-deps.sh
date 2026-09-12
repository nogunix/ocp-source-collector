#!/usr/bin/env bash
# Roll the dependency-source layer (deps/ + meta/DEPS.tsv) across every mounted
# casket, one at a time, via scripts/repackage-add-deps.sh (overlay + repack --
# no component source is re-fetched).
#
# Usage: repackage-fleet-deps.sh [--date YYYYMMDD] [--out DIR] [--only GLOB] [--dry-run]
#
# Order is deliberate: operators first (smallest, validates the path), then
# layered, then Phase A. Each casket is independent -- a failure logs and moves
# on, so one bad mount never stops the fleet.
#
# The dep store/cache live on the HDD (CASKET_DEP_STORE / the sibling dep-cache)
# because the union across the fleet is >100G and the SSD root fs has ~400G
# total headroom. The overlay upper dir must be on the SAME filesystem as the
# store or collect-deps' hardlinks silently become full copies.
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

DATE=$(date -u +%Y%m%d); OUT_DIR="/mnt/hdd/casket-ocp"; ONLY=""; DRY=0; PAR=1; ONE=""
while (( $# )); do
    case "$1" in
        --parallel) PAR="$2"; shift 2 ;;
        --one)  ONE="$2"; shift 2 ;;   # internal: process a single tab-separated row
        --date) DATE="$2"; shift 2 ;;
        --out)  OUT_DIR="$2"; shift 2 ;;
        --only) ONLY="$2"; shift 2 ;;
        --dry-run) DRY=1; shift ;;
        *) echo "unknown arg: $1" >&2; exit 1 ;;
    esac
done

export CASKET_DEP_STORE="${CASKET_DEP_STORE:-/mnt/hdd/casket-dep-store}"
export CASKET_DEP_CACHE="${CASKET_DEP_CACHE:-/mnt/hdd/casket-dep-cache}"
export CASKET_REPACK_TMP="${CASKET_REPACK_TMP:-/mnt/hdd/.repack-tmp}"
# CASKET_WORK defaults to THIS checkout (parent of scripts/), never $HOME:
# the repo directory is renameable, and under sudo $HOME is /root.
_SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
: "${CASKET_WORK:=$(dirname "$_SELF_DIR")}"
LOG_DIR="$CASKET_WORK/scratch/fleet-deps-$DATE"
mkdir -p "$LOG_DIR"

# mount -> flavor -> output basename
# One pass over the mounts with an exact-suffix case: globbing "ocp*-operators"
# also matches "ocp4.14-certified-operators", and "ocp[0-9]*.[0-9]*" matches
# "ocp4.22-operators" -- both produced duplicate/misflavoured rows.
targets() {
    local m b
    for m in /srv/sources-*; do
        [[ -d "$m" ]] || continue
        b="${m#/srv/sources-}"
        case "$b" in
            layered-ocp*)                     echo -e "$m\tB\tcasket-${DATE}-${b}.sqfs.xz" ;;
            ocp*-operators|ocp*-certified-operators|ocp*-community-operators)
                [[ -d "$m/git" ]] && echo -e "$m\tB\tcasket-${DATE}-${b}.sqfs.xz" ;;
            ocp[0-9]*.[0-9]*)                 # Phase A is the only patch-level mount
                [[ -d "$m/git" ]] && echo -e "$m\tA\tcasket-${DATE}-${b}.sqfs.xz" ;;
            *) ;;                             # srpms / rhel / opengrok: not ours
        esac
    done
}

# --one: process a single row (this is what the parallel workers re-enter as).
if [[ -n "$ONE" ]]; then
    IFS=$'\t' read -r mount flavor outname <<<"$ONE"
    out="${OUT_DIR%/}/$outname"
    [[ -s "$out" ]] && { echo "= $outname already built — skipping"; exit 0; }
    echo "=== $(date -Is) $mount -> $outname"
    if "$SCRIPT_DIR/repackage-add-deps.sh" -m "$mount" -o "$out" -f "$flavor" -j 16 \
         > "$LOG_DIR/$(basename "$mount").log" 2>&1; then
        echo "    ok $(date -Is) $outname $(du -h "$out" 2>/dev/null | cut -f1)"
    else
        echo "    FAILED $outname (see $LOG_DIR/$(basename "$mount").log)"
        exit 1
    fi
    exit 0
fi

LIST="$LOG_DIR/targets.tsv"
targets > "$LIST"
[[ -n "$ONLY" ]] && { grep -E "^$ONLY\s" "$LIST" > "$LIST.f" && mv "$LIST.f" "$LIST"; }
total=$(wc -l < "$LIST")
if (( DRY )); then sed 's/^/+ /' "$LIST"; echo "(dry-run) $total casket(s)"; exit 0; fi

# Parallel workers: one casket is CPU-bound while compressing and IO-bound while
# reading millions of small dep files off the HDD, so a second worker overlaps
# the two phases. More than 2 just thrashes the disk.
export SCRIPT_DIR OUT_DIR LOG_DIR CASKET_DEP_STORE CASKET_DEP_CACHE CASKET_REPACK_TMP
xargs -a "$LIST" -d '\n' -P "$PAR" -I{} "$SCRIPT_DIR/repackage-fleet-deps.sh" \
    --one {} --date "$DATE" --out "$OUT_DIR"
ok=$(ls "$OUT_DIR"/casket-"$DATE"-* 2>/dev/null | wc -l)
skipped=0; failed=$(( total - ok ))

echo "fleet deps: $ok built, $skipped skipped, $failed failed, $total considered"
