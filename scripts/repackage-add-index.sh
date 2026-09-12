#!/usr/bin/env bash
# Backfill the source-index layer (git/INDEX.tsv + by-component/ + by-repo/) into
# an already-built casket WITHOUT re-fetching, by overlaying the generated index
# files on the read-only squashfs mount via overlayfs and re-running mksquashfs on
# the merged view. The bulk source bytes are reused from the mount; only the tiny
# index/symlink layer is added.
#
# Handles both layouts:
#   - single  (Phase A / B): <mount>/meta/MANIFEST.json + <mount>/git/
#   - layered (Phase B-operand): <mount>/<product>/meta/MANIFEST.json + .../git/
#
# Usage: repackage-add-index.sh -m <mount_dir> -o <out.sqfs.xz> [-f A|B]
#   -f A   -> Phase A mksquashfs flags (-Xdict-size 100%)
#   -f B   -> Phase B/B-operand flags (-Xbcj x86 -no-xattrs)  [default]
# Requires: sudo (overlay + loop mount), mksquashfs, python3.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

MOUNT=""; OUT=""; FLAVOR="B"
while [[ $# -gt 0 ]]; do
    case "$1" in
        -m) MOUNT="$2"; shift 2 ;;
        -o) OUT="$2";   shift 2 ;;
        -f) FLAVOR="$2"; shift 2 ;;
        *) echo "unknown arg: $1" >&2; exit 1 ;;
    esac
done
[[ -d "$MOUNT" && -n "$OUT" ]] || { echo "usage: $0 -m <mount> -o <out> [-f A|B]" >&2; exit 1; }

UPPER=$(mktemp -d); WORKD=$(mktemp -d); MERGED=$(mktemp -d)
cleanup() {
    mountpoint -q "$MERGED" && sudo umount "$MERGED" || true
    rm -rf "$UPPER" "$WORKD"; rmdir "$MERGED" 2>/dev/null || true
}
trap cleanup EXIT

# Build index artifacts for one unit (dir with meta/MANIFEST.json + git/) into $2.
build_unit() {
    local unit="$1" up="$2"
    [[ -f "$unit/meta/MANIFEST.json" ]] || return 0
    local tmp; tmp=$(mktemp -d); mkdir -p "$tmp/meta" "$tmp/git"
    cp "$unit/meta/MANIFEST.json" "$tmp/meta/"
    local d
    for d in "$unit"/git/*/; do [[ -d "$d" ]] && mkdir -p "$tmp/git/$(basename "$d")"; done
    python3 "$SCRIPT_DIR/build-source-index.py" "$tmp" >/dev/null
    mkdir -p "$up/git"
    cp "$tmp/git/INDEX.tsv" "$up/git/" 2>/dev/null || true
    cp -a "$tmp/by-component" "$tmp/by-repo" "$up/" 2>/dev/null || true
    rm -rf "$tmp"
}

if [[ -f "$MOUNT/meta/MANIFEST.json" ]]; then       # single (A/C)
    build_unit "$MOUNT" "$UPPER"
else                                                # layered (D)
    for p in "$MOUNT"/*/; do
        build_unit "$p" "$UPPER/$(basename "${p%/}")"
    done
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
