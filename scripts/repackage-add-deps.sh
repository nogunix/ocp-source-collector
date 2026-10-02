#!/usr/bin/env bash
# Backfill the dependency-source layer (deps/ + meta/DEPS.tsv) into an
# ALREADY-BUILT casket without re-fetching any component source: overlay the
# generated deps on the read-only squashfs mount and re-run mksquashfs on the
# merged view. Same trick as repackage-add-index.sh.
#
# This is the practical way to roll deps out across the whole fleet -- the git
# tarballs and work dirs for older minors are long deleted, and re-running
# discover+fetch for 45 caskets would take days and hit GitHub rate limits,
# while the source bytes we need are already sitting on the mount.
#
# Handles both layouts:
#   single  (Phase A / B / certified / community): <mount>/git/
#   layered (B-operand):                           <mount>/<product>/git/
#
# Usage: repackage-add-deps.sh -m <mount_dir> -o <out.sqfs.xz> [-f A|B] [-j N] [--eco LIST]
# Requires: sudo (overlay mount), mksquashfs, python3.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"
# shellcheck source=lib-overlay.sh
source "$SCRIPT_DIR/lib-overlay.sh"

MOUNT=""; OUT=""; FLAVOR="B"; JOBS=12; ECO="go,crates,npm,pypi"
while [[ $# -gt 0 ]]; do
    case "$1" in
        -m) MOUNT="$2"; shift 2 ;;
        -o) OUT="$2";   shift 2 ;;
        -f) FLAVOR="$2"; shift 2 ;;
        -j) JOBS="$2";  shift 2 ;;
        --eco) ECO="$2"; shift 2 ;;
        *) die "unknown arg: $1" ;;
    esac
done
[[ -d "$MOUNT" && -n "$OUT" ]] || die "usage: $0 -m <mount> -o <out> [-f A|B] [-j N]"
overlay_check_flavor "$FLAVOR"
# CASKET_REPACK_TMP must be on the dep store's filesystem (see lib-overlay.sh).
overlay_init

collect() {  # $1 = unit dir on the mount, $2 = upper dir for that unit
    python3 "$SCRIPT_DIR/collect-deps.py" "$1" --out "$2" --jobs "$JOBS" --eco "$ECO"
}

overlay_each_unit "$MOUNT" git collect
overlay_repack "$MOUNT" "$OUT" "$FLAVOR" "no unvendored manifests"
