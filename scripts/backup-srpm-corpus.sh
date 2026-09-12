#!/usr/bin/env bash
# Back up the collected .src.rpm corpus to the HDD.
#
# Why this exists: the casket stores only EXTRACTED trees and deliberately
# drops the .src.rpm (docs/a-rpm-collection.md). The corpus under
# phase-a-rpm/srpms*/ is therefore the ONLY original -- it cannot be
# reconstructed from a mounted casket. Worse, part of it is no longer
# obtainable: EUS micro-releases (el9_2, el9_4 ...) age out of the CDN, which
# is exactly why 475 packages could not be re-fetched on 2026-08-25 after
# 15 hours of trying. Losing this directory means losing source that no longer
# exists upstream.
#
# It lives on the root fs (with ~600G free) while /mnt/hdd has terabytes, so
# there is no reason not to hold a second copy.
#
# Safe to run while a collection is in flight: rsync takes whatever is there,
# and re-running picks up the rest.
#
# Usage: backup-srpm-corpus.sh [-d DEST] [--dry-run]
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"
set +e +o pipefail

DEST="${CASKET_SRPM_BACKUP:-/mnt/hdd/casket-srpm-corpus}"
DRY=()
while (( $# )); do
    case "$1" in
        -d|--dest) DEST="$2"; shift 2 ;;
        --dry-run) DRY=(--dry-run); shift ;;
        -h|--help) echo "backup-srpm-corpus.sh [-d DEST] [--dry-run]"; exit 0 ;;
        *) die "unknown arg: $1" ;;
    esac
done
require_cmd rsync

SRC="$CASKET_WORK/phase-a-rpm"
[[ -d "$SRC" ]] || die "no $SRC"
mkdir -p "$DEST" || die "cannot create $DEST"

# Only the .src.rpm corpus. Not rpmdb caches, not logs, not extracted stages --
# those are all regenerable, and including them would multiply the size.
mapfile -t DIRS < <(cd "$SRC" && ls -d srpms srpms-[0-9]* srpms-containers srpms-ext-* srpms-fill* 2>/dev/null)
(( ${#DIRS[@]} )) || die "no srpm directories under $SRC"

log "source: $SRC"
log "dest:   $DEST"
total=0
for d in "${DIRS[@]}"; do
    n=$(ls "$SRC/$d"/*.src.rpm 2>/dev/null | wc -l)
    total=$((total + n))
    printf '  %-24s %5d .src.rpm\n' "$d" "$n"
done
log "$total .src.rpm across ${#DIRS[@]} directories"

rc=0
for d in "${DIRS[@]}"; do
    log "syncing $d"
    rsync -a --info=stats2 "${DRY[@]}" \
        --include='*/' --include='*.src.rpm' --exclude='*' \
        "$SRC/$d/" "$DEST/$d/" 2>&1 | grep -E 'Number of|Total transferred' || rc=1
done

if (( ${#DRY[@]} == 0 )); then
    have=$(find "$DEST" -name '*.src.rpm' 2>/dev/null | wc -l)
    log "backup now holds $have .src.rpm ($(du -sh "$DEST" 2>/dev/null | cut -f1))"
    (( have >= total )) || { log "WARNING: backup has fewer files than source ($have < $total)"; rc=1; }
fi
exit $rc
