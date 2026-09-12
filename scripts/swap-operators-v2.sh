#!/bin/bash
# Swap operator caskets from 20260524 → 20260527 (Phase B v2 rebuild).
# Updates /etc/fstab, daemon-reload, and restarts each .mount unit.
#
# Usage: sudo ./scripts/swap-operators-v2.sh

set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "must run as root (sudo)" >&2; exit 1; }

FSTAB=/etc/fstab
CASKET_DIR="${CASKET_OUT:-/mnt/hdd/casket-ocp}"
VERS=(4.14 4.15 4.16 4.17 4.18 4.19 4.20)

cp -a "$FSTAB" "${FSTAB}.bak.$(date +%s)"

for V in "${VERS[@]}"; do
    OLD=$(ls "$CASKET_DIR"/casket-2026052[34]-ocp"$V"-operators.sqfs.xz 2>/dev/null | head -1)
    NEW="$CASKET_DIR/casket-20260527-ocp${V}-operators.sqfs.xz"
    [[ -f "$NEW" ]] || { echo "skip $V: new file missing ($NEW)" >&2; continue; }
    [[ -n "$OLD" && -f "$OLD" ]] || { echo "skip $V: old file missing" >&2; continue; }

    OLD_BASE=$(basename "$OLD")
    NEW_BASE=$(basename "$NEW")
    [[ "$OLD_BASE" == "$NEW_BASE" ]] && { echo "$V already on new file"; continue; }

    echo "=== $V ==="
    echo "  old: $OLD_BASE"
    echo "  new: $NEW_BASE"

    sed -i "s|$OLD_BASE|$NEW_BASE|g" "$FSTAB"
done

systemctl daemon-reload

for V in "${VERS[@]}"; do
    UNIT=$(systemd-escape -p --suffix=mount "/srv/sources-ocp${V}-operators")
    echo "--> restart $UNIT"
    systemctl restart "$UNIT" || echo "  WARN: restart failed for $UNIT"
done

echo
echo "Mounts:"
for V in "${VERS[@]}"; do
    mount | grep "/srv/sources-ocp${V}-operators" || echo "  $V NOT MOUNTED"
done
