#!/bin/bash
# Swap Phase A caskets from .tar.gz-bundled (20260524, 4.20=20260523) to
# source-extracted layout (20260527). Filenames change, so fstab needs an edit.
#
# Usage: sudo ./scripts/swap-phase-a-extracted.sh

set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "must run as root (sudo)" >&2; exit 1; }

CASKET_DIR="${CASKET_OUT:-/mnt/hdd/casket-ocp}"
NEW_DATE=20260527

# version : old_date
declare -A OLD_DATE=(
    [4.14.58]=20260524
    [4.15.59]=20260524
    [4.16.55]=20260524
    [4.17.53]=20260524
    [4.18.41]=20260524
    [4.19.31]=20260524
    [4.20.22]=20260523
)

# Validate new files exist
for V in "${!OLD_DATE[@]}"; do
    NEW="$CASKET_DIR/casket-${NEW_DATE}-ocp${V}.sqfs.xz"
    [[ -f "$NEW" ]] || { echo "missing $NEW" >&2; exit 1; }
done

# Backup fstab
cp -a /etc/fstab "/etc/fstab.bak.$(date +%Y%m%d-%H%M%S)"

# Rewrite fstab entries
for V in "${!OLD_DATE[@]}"; do
    OLD_FILE="casket-${OLD_DATE[$V]}-ocp${V}.sqfs.xz"
    NEW_FILE="casket-${NEW_DATE}-ocp${V}.sqfs.xz"
    sed -i "s|$OLD_FILE|$NEW_FILE|g" /etc/fstab
done

systemctl daemon-reload

# Remount each
for V in "${!OLD_DATE[@]}"; do
    MNT="/srv/sources-ocp${V}"
    UNIT=$(systemd-escape -p --suffix=mount "$MNT")
    echo "=== $V ==="
    if ! systemctl restart "$UNIT"; then
        echo "  restart failed, trying umount -l + start"
        umount -l "$MNT" || true
        systemctl start "$UNIT"
    fi
done

echo
echo "Mounts:"
for V in "${!OLD_DATE[@]}"; do
    mount | grep "/srv/sources-ocp${V} " || echo "  $V NOT MOUNTED"
done

echo
echo "Old (rollback) files still on disk:"
for V in "${!OLD_DATE[@]}"; do
    OLD="$CASKET_DIR/casket-${OLD_DATE[$V]}-ocp${V}.sqfs.xz"
    [[ -f "$OLD" ]] && ls -lh "$OLD" || echo "  (missing) $OLD"
done
