#!/bin/bash
# Remount-only follow-up: fstab is already updated by swap-operators-v2.sh,
# this just restarts the systemd .mount units with the correct escaped names.
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "must run as root (sudo)" >&2; exit 1; }

VERS=(4.14 4.15 4.16 4.17 4.18 4.19 4.20 4.21)

systemctl daemon-reload

for V in "${VERS[@]}"; do
    MP="/srv/sources-ocp${V}-operators"
    UNIT=$(systemd-escape -p --suffix=mount "$MP")
    echo "--> restart $UNIT"
    if ! systemctl restart "$UNIT"; then
        # Loop device busy ("Job failed") — lazy-umount then start fresh.
        echo "  restart failed; trying lazy umount + start"
        umount -l "$MP" 2>/dev/null || true
        systemctl start "$UNIT" || echo "  WARN: start still failed for $UNIT"
    fi
done

echo
echo "Mounts:"
for V in "${VERS[@]}"; do
    mount | grep "/srv/sources-ocp${V}-operators" || echo "  $V NOT MOUNTED"
done
