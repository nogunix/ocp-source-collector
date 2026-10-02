#!/bin/bash
# Overlay repack: add a generated layer to an ALREADY-BUILT casket without
# re-fetching any component source. The layer is written to an overlayfs upper
# dir over the read-only casket mount, and mksquashfs runs on the merged view,
# so the bulk source bytes are reused from the mount. Shared by
# repackage-add-{index,deps,submodules}.sh; source after lib.sh.
#
#   overlay_init                         temp dirs + cleanup trap
#   overlay_each_unit MOUNT MARKER FN    run FN <unit-dir> <upper-dir> per unit
#   overlay_repack MOUNT OUT A|B REASON  mount the overlay and build OUT
#
# Units: a single casket (Phase A / B / certified / community) is one unit at
# <mount>/; a layered one (B-operand) has one unit per <mount>/<product>/.

# The overlay must use the flags the casket was built with:
#   A -> Phase A (package.sh)            B -> everything else
overlay_check_flavor() {
    case "$1" in
        A|B) ;;
        *) die "unknown flavor: $1 (want A or B)" ;;
    esac
}

# Temp dirs live on disk, not /tmp (tmpfs = RAM): the deps and submodules
# layers run to GBs. The collectors also hardlink their store into the upper
# dir, and a cross-device link silently degrades to a full copy, so point
# CASKET_REPACK_TMP at the store's filesystem. upper and work must share one
# filesystem for overlayfs anyway.
overlay_init() {
    local base="${CASKET_REPACK_TMP:-$CASKET_WORK/.repack-tmp}"
    mkdir -p "$base"
    UPPER=$(mktemp -d -p "$base"); WORKD=$(mktemp -d -p "$base"); MERGED=$(mktemp -d -p "$base")
    trap overlay_cleanup EXIT
}

overlay_cleanup() {
    if mountpoint -q "$MERGED"; then sudo umount "$MERGED" || true; fi
    rm -rf "$UPPER" "$WORKD"; rmdir "$MERGED" 2>/dev/null || true
}

# overlay_each_unit <mount> <marker> <fn> — a unit is a dir holding <marker>
# (a path relative to it). The mount itself is the single unit when it holds
# the marker; otherwise every product subdir that does is one.
overlay_each_unit() {
    local mount="$1" marker="$2" fn="$3" p
    if [[ -e "$mount/$marker" ]]; then
        "$fn" "$mount" "$UPPER"
        return
    fi
    for p in "$mount"/*/; do
        p="${p%/}"
        [[ -e "$p/$marker" ]] || continue
        "$fn" "$p" "$UPPER/$(basename "$p")"
    done
}

# overlay_repack <mount> <out> <A|B> <reason> — an empty upper means there is
# nothing to add: say why (<reason>) and exit 0 without building anything.
overlay_repack() {
    local mount="$1" out="$2" flavor="$3" reason="$4"
    if [[ -z "$(ls -A "$UPPER" 2>/dev/null)" ]]; then
        log "nothing to add for $mount ($reason) -- skipping repack"
        exit 0
    fi
    chmod -R a+rX "$UPPER"; chmod 755 "$UPPER"   # root must be world-traversable
    sudo mount -t overlay overlay \
        -o lowerdir="$mount",upperdir="$UPPER",workdir="$WORKD" "$MERGED"
    # The output keeps the .sqfs.xz name of what it replaces, so the format
    # is squashfs whatever CASKET_FORMAT says.
    if [[ "$flavor" == "A" ]]; then
        CASKET_FORMAT=sqfs casket_mkfs "$MERGED" "$out" -Xdict-size 100%
    else
        CASKET_FORMAT=sqfs casket_mkfs "$MERGED" "$out" -Xbcj x86 -no-xattrs
    fi
    log "done: $out ($(du -h "$out" | cut -f1))"
}
