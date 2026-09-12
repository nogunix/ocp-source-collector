#!/bin/bash
# Add fstab entries + mount points for a new OCP minor (Phase A + Phase B).
# Idempotent: skips lines already present in fstab.
#
# Usage: sudo ./scripts/mount-new-minor.sh <patch_version> <minor>
#   e.g. sudo ./scripts/mount-new-minor.sh 4.21.17 4.21

set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "must run as root (sudo)" >&2; exit 1; }
[[ $# -eq 2 ]] || { echo "usage: $0 <patch> <minor>  (e.g. 4.21.17 4.21)" >&2; exit 1; }

PATCH="$1"; MINOR="$2"
FSTAB=/etc/fstab
CASKET_DIR="${CASKET_OUT:-/mnt/hdd/casket-ocp}"
DATE=20260527   # adjust if rebuilding on a different day

PHASE_A="${CASKET_DIR}/casket-${DATE}-ocp${PATCH}.sqfs.xz"
PHASE_C="${CASKET_DIR}/casket-${DATE}-ocp${MINOR}-operators.sqfs.xz"
MNT_A="/srv/sources-ocp${PATCH}"
MNT_C="/srv/sources-ocp${MINOR}-operators"

[[ -f "$PHASE_A" ]] || { echo "missing: $PHASE_A" >&2; exit 1; }
[[ -f "$PHASE_C" ]] || { echo "missing: $PHASE_C" >&2; exit 1; }

cp -a "$FSTAB" "${FSTAB}.bak.$(date +%s)"
mkdir -p "$MNT_A" "$MNT_C"

OPTS="squashfs loop,ro,nofail,x-systemd.requires-mounts-for=/mnt/hdd 0 0"
for pair in "$PHASE_A|$MNT_A" "$PHASE_C|$MNT_C"; do
    SRC="${pair%|*}"; MNT="${pair#*|}"
    LINE="$SRC $MNT $OPTS"
    if grep -qE "[[:space:]]${MNT}[[:space:]]" "$FSTAB"; then
        echo "fstab already has $MNT — skipping"
    else
        echo "$LINE" >> "$FSTAB"
        echo "added: $LINE"
    fi
done

systemctl daemon-reload
for MNT in "$MNT_A" "$MNT_C"; do
    UNIT=$(systemd-escape -p --suffix=mount "$MNT")
    echo "--> start $UNIT"
    systemctl start "$UNIT"
done

echo
echo "Mounts:"
mount | grep "/srv/sources-ocp${MINOR}" || echo "  NOT MOUNTED"
