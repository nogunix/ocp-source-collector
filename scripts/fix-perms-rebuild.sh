#!/bin/bash
# One-shot fixer for operator caskets built before chmod normalization was
# added to phase-b-package.sh. Unpacks each .sqfs.xz, chmod -R a+rX, repacks
# with the same mksquashfs options, swaps in place, and remounts.
#
# Usage (must run as root):
#   sudo ./scripts/fix-perms-rebuild.sh                # all 7 operator caskets
#   sudo ./scripts/fix-perms-rebuild.sh 4.20           # one version
#   sudo ./scripts/fix-perms-rebuild.sh 4.19 4.20      # subset
#
# Side effects:
#   - Stops the matching systemd .mount unit before swap, restarts after.
#   - Keeps the original sqfs.xz as <name>.orig until you delete it.
#   - Workdir: $CASKET_WORK/scratch/rebuild-perms/<ver>/

set -euo pipefail

[[ $EUID -eq 0 ]] || { echo "must run as root (sudo)" >&2; exit 1; }

# The readability probe has to run as a NORMAL user -- root can read the very
# files this script exists to find. $SUDO_USER is who invoked us; fall back to
# the owner of the checkout when the script is run as root some other way.
PROBE_USER="${CASKET_USER:-${SUDO_USER:-$(stat -c %U "$(dirname "${BASH_SOURCE[0]}")")}}"
[[ -n "$PROBE_USER" && "$PROBE_USER" != "root" ]] || {
    echo "cannot determine an unprivileged user; set CASKET_USER=<name>" >&2; exit 1; }

CASKET_DIR="${CASKET_OUT:-/mnt/hdd/casket-ocp}"
# CASKET_WORK defaults to THIS checkout (parent of scripts/), never $HOME:
# the repo directory is renameable, and under sudo $HOME is /root.
_SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
: "${CASKET_WORK:=$(dirname "$_SELF_DIR")}"
WORK_ROOT="$CASKET_WORK/scratch/rebuild-perms"
ALL_VERS=(4.14 4.15 4.16 4.17 4.18 4.19 4.20)

VERS=( "${@:-${ALL_VERS[@]}}" )

mkdir -p "$WORK_ROOT"

for V in "${VERS[@]}"; do
    SRC=$(ls "$CASKET_DIR"/casket-*-ocp"$V"-operators.sqfs.xz 2>/dev/null | head -1)
    [[ -n "$SRC" && -f "$SRC" ]] || { echo "skip $V: no casket found" >&2; continue; }

    MNT=/srv/sources-ocp${V}-operators
    UNIT="srv-sources\x2docp${V}\x2doperators.mount"
    STAGE="$WORK_ROOT/$V"

    echo "=== $V :: $SRC ==="

    pre=$(sudo -u "$PROBE_USER" find "$MNT" -type f ! -readable 2>/dev/null | wc -l)
    echo "    pre-rebuild unreadable: $pre"
    if [[ "$pre" -eq 0 ]]; then
        echo "    already clean, skipping"
        continue
    fi

    echo "--> stop mount"
    systemctl stop "$UNIT"

    echo "--> unsquashfs"
    rm -rf "$STAGE"
    unsquashfs -d "$STAGE" "$SRC" >/dev/null

    echo "--> chmod -R a+rX"
    chmod -R a+rX "$STAGE"

    echo "--> backup original to ${SRC}.orig"
    mv "$SRC" "${SRC}.orig"

    echo "--> mksquashfs (xz)"
    mksquashfs "$STAGE" "$SRC" \
        -comp xz -Xbcj x86 \
        -no-progress -all-root -no-xattrs \
        -noappend >/dev/null

    chown "$PROBE_USER:$PROBE_USER" "$SRC"

    echo "--> start mount"
    systemctl start "$UNIT"

    post=$(sudo -u "$PROBE_USER" find "$MNT" -type f ! -readable 2>/dev/null | wc -l)
    echo "    post-rebuild unreadable: $post"

    if [[ "$post" -ne 0 ]]; then
        echo "    FAIL: $post files still unreadable — leaving stage at $STAGE" >&2
        exit 1
    fi

    rm -rf "$STAGE"
    echo "    OK"
done

echo
echo "Done. Original sqfs.xz files preserved as *.orig — delete after spot-check:"
ls -lh "$CASKET_DIR"/*.orig 2>/dev/null || true
