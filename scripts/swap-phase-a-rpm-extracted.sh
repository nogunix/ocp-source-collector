#!/bin/bash
# Swap Phase A/RPM (formerly "Phase B") SRPM casket from bundled (.src.rpm
# files) to extracted layout. Filename changes (date roll), so fstab needs
# an edit.
#
# Usage: sudo ./scripts/swap-phase-a-rpm-extracted.sh
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "must run as root (sudo)" >&2; exit 1; }

CASKET_DIR="${CASKET_OUT:-/mnt/hdd/casket-ocp}"
OLD_DATE=20260524
NEW_DATE=20260528
MNT=/srv/sources-ocp-srpms

OLD_FILE="casket-${OLD_DATE}-ocp-srpms.sqfs.xz"
NEW_FILE="casket-${NEW_DATE}-ocp-srpms.sqfs.xz"

[[ -f "$CASKET_DIR/$NEW_FILE" ]] || { echo "missing $CASKET_DIR/$NEW_FILE" >&2; exit 1; }

cp -a /etc/fstab "/etc/fstab.bak.$(date +%Y%m%d-%H%M%S)"
sed -i "s|$OLD_FILE|$NEW_FILE|g" /etc/fstab

systemctl daemon-reload

UNIT=$(systemd-escape -p --suffix=mount "$MNT")
if ! systemctl restart "$UNIT"; then
    echo "restart failed, trying umount -l + start"
    umount -l "$MNT" || true
    systemctl start "$UNIT"
fi

echo
echo "Mount:"
mount | grep "$MNT " || echo "  NOT MOUNTED"

echo
echo "Old casket retained for rollback:"
[[ -f "$CASKET_DIR/$OLD_FILE" ]] && ls -lh "$CASKET_DIR/$OLD_FILE" || echo "  (missing)"
