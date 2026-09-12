#!/bin/bash
# Swap operator caskets from tarball-bundled → source-extracted layout.
# Both old and new use the same filename (casket-20260527-ocp<ver>-operators.sqfs.xz),
# so this script renames the old to *.pre-extract, moves the new in, and
# restarts the mount unit. fstab is not touched.
#
# Usage: sudo ./scripts/swap-operators-extracted.sh

set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "must run as root (sudo)" >&2; exit 1; }

CASKET_DIR="${CASKET_OUT:-/mnt/hdd/casket-ocp}"
TEST_DIR=/mnt/hdd/casket-ocp-test
DATE=20260527
VERS=(4.14 4.15 4.16 4.17 4.18 4.19 4.20)

for V in "${VERS[@]}"; do
    FILE="casket-${DATE}-ocp${V}-operators.sqfs.xz"
    OLD="$CASKET_DIR/$FILE"
    NEW="$TEST_DIR/$FILE"
    MNT="/srv/sources-ocp${V}-operators"
    UNIT=$(systemd-escape -p --suffix=mount "$MNT")

    [[ -f "$NEW" ]] || { echo "skip $V: new file missing ($NEW)" >&2; continue; }
    [[ -f "$OLD" ]] || { echo "skip $V: old file missing ($OLD)" >&2; continue; }

    echo "=== $V ==="
    # Stash the bundled version next to the new one (for rollback).
    mv -f "$OLD" "${OLD}.pre-extract"
    mv -f "$NEW" "$OLD"

    # Try a clean restart first; on "Job failed" the loop device is busy —
    # fall back to lazy umount + start (same workaround as the v2 swap).
    if ! systemctl restart "$UNIT"; then
        echo "  restart failed, trying umount -l + start"
        umount -l "$MNT" || true
        systemctl start "$UNIT"
    fi
done

echo
echo "Mounts:"
for V in "${VERS[@]}"; do
    mount | grep "/srv/sources-ocp${V}-operators" || echo "  $V NOT MOUNTED"
done

echo
echo "Rollback files (delete once verified):"
ls -lh "$CASKET_DIR"/casket-${DATE}-ocp*-operators.sqfs.xz.pre-extract 2>/dev/null || true
