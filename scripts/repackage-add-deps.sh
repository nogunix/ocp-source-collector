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

MOUNT=""; OUT=""; FLAVOR="B"; JOBS=12; ECO="go,crates,npm,pypi"
while [[ $# -gt 0 ]]; do
    case "$1" in
        -m) MOUNT="$2"; shift 2 ;;
        -o) OUT="$2";   shift 2 ;;
        -f) FLAVOR="$2"; shift 2 ;;
        -j) JOBS="$2";  shift 2 ;;
        --eco) ECO="$2"; shift 2 ;;
        *) echo "unknown arg: $1" >&2; exit 1 ;;
    esac
done
[[ -d "$MOUNT" && -n "$OUT" ]] || { echo "usage: $0 -m <mount> -o <out> [-f A|B] [-j N]" >&2; exit 1; }

# upper/work MUST sit on the same filesystem as the dep store: collect-deps
# hardlinks the store into the upper dir, and a cross-device link silently
# degrades to a full copy (GBs of deps into /tmp, which is tmpfs = RAM).
# CASKET_WORK defaults to THIS checkout (parent of scripts/), never $HOME:
# the repo directory is renameable, and under sudo $HOME is /root.
_SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
: "${CASKET_WORK:=$(dirname "$_SELF_DIR")}"
TMPBASE="${CASKET_REPACK_TMP:-$CASKET_WORK/.repack-tmp}"
mkdir -p "$TMPBASE"
UPPER=$(mktemp -d -p "$TMPBASE"); WORKD=$(mktemp -d -p "$TMPBASE"); MERGED=$(mktemp -d -p "$TMPBASE")
cleanup() {
    mountpoint -q "$MERGED" && sudo umount "$MERGED" || true
    rm -rf "$UPPER" "$WORKD"; rmdir "$MERGED" 2>/dev/null || true
}
trap cleanup EXIT

collect() {  # $1 = unit dir on the mount, $2 = upper dir for that unit
    [[ -d "$1/git" ]] || return 0
    python3 "$SCRIPT_DIR/collect-deps.py" "$1" --out "$2" --jobs "$JOBS" --eco "$ECO"
}

if [[ -d "$MOUNT/git" ]]; then                      # single (A / B / certified / community)
    collect "$MOUNT" "$UPPER"
else                                                # layered (B-operand)
    for p in "$MOUNT"/*/; do
        [[ -d "$p/git" ]] || continue
        collect "${p%/}" "$UPPER/$(basename "${p%/}")"
    done
fi

if [[ -z "$(ls -A "$UPPER" 2>/dev/null)" ]]; then
    echo "nothing to add for $MOUNT (no unvendored manifests) — skipping repack" >&2
    exit 0
fi
chmod -R a+rX "$UPPER"; chmod 755 "$UPPER"   # root must be world-traversable

sudo mount -t overlay overlay \
    -o lowerdir="$MOUNT",upperdir="$UPPER",workdir="$WORKD" "$MERGED"

rm -f "$OUT"; mkdir -p "$(dirname "$OUT")"
if [[ "$FLAVOR" == "A" ]]; then
    mksquashfs "$MERGED" "$OUT" -comp xz -Xdict-size 100% \
        -no-progress -noappend -all-root
else
    mksquashfs "$MERGED" "$OUT" -comp xz -Xbcj x86 \
        -no-progress -all-root -no-xattrs -noappend
fi
echo "done: $OUT ($(du -h "$OUT" | cut -f1))"
