#!/bin/bash
# Install/refresh the boot-time mount service with THIS checkout's path baked
# into ExecStart (system units can't reference a user's checkout portably).
# Usage: sudo ./scripts/install-mounts-service.sh
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(dirname "$SCRIPT_DIR")"
[[ $EUID -eq 0 ]] || { echo "run with sudo (installs a system unit)" >&2; exit 1; }
sed "s|^ExecStart=.*|ExecStart=${REPO}/scripts/casket-mounts.sh --apply|" \
    "$REPO/systemd/casket-mounts.service" > /etc/systemd/system/casket-mounts.service
systemctl daemon-reload
systemctl enable casket-mounts.service
echo "installed: ExecStart=${REPO}/scripts/casket-mounts.sh --apply"
