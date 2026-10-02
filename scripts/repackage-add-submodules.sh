#!/usr/bin/env bash
# Backfill expanded git submodules (git/<comp>/<path>/ + meta/SUBMODULES.tsv)
# into an ALREADY-BUILT casket without re-fetching any component source:
# overlay the fetched submodule trees on the read-only squashfs mount and
# re-run mksquashfs on the merged view. Same trick as repackage-add-deps.sh.
#
# Why a backfill at all: a GitHub codeload archive writes each gitlink as an
# EMPTY DIR, so every casket built so far carries submodule-using trees as
# build glue with the code missing -- 1052 trees / 2543 empty submodule dirs
# across the fleet as of 2026-08-19. Waiting for the next full rebuild would
# leave e.g. ZTWIM (a release repo that is Containerfiles + 5 submodules and
# nothing else) with no operator source at all for another cycle.
#
# Overlay note: the submodule dirs already exist in the lower (squashfs) layer
# as empty dirs, and overlayfs MERGES a plain upper dir with its lower
# counterpart -- so the filled files appear inside them without a whiteout.
#
# Handles both layouts:
#   single  (Phase A / B / certified / community): <mount>/git/
#   layered (B-operand):                           <mount>/<product>/git/
#
# Usage: repackage-add-submodules.sh -m <mount_dir> -o <out.sqfs.xz> [-f A|B] [-j N]
# Requires: sudo (overlay mount), mksquashfs, python3. Set GITHUB_TOKEN, or
# the trees API rate limit (60/h) forces branch-head APPROX refs.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"
# shellcheck source=lib-overlay.sh
source "$SCRIPT_DIR/lib-overlay.sh"

MOUNT=""; OUT=""; FLAVOR="B"; JOBS=8
while [[ $# -gt 0 ]]; do
    case "$1" in
        -m) MOUNT="$2"; shift 2 ;;
        -o) OUT="$2";   shift 2 ;;
        -f) FLAVOR="$2"; shift 2 ;;
        -j) JOBS="$2";  shift 2 ;;
        *) die "unknown arg: $1" ;;
    esac
done
[[ -d "$MOUNT" && -n "$OUT" ]] || die "usage: $0 -m <mount> -o <out> [-f A|B] [-j N]"
overlay_check_flavor "$FLAVOR"
# CASKET_REPACK_TMP must be on the submodule store's filesystem (see lib-overlay.sh).
overlay_init

collect() {  # $1 = unit dir on the mount, $2 = upper dir for that unit
    python3 "$SCRIPT_DIR/collect-submodules.py" "$1" --out "$2" --jobs "$JOBS"
}

overlay_each_unit "$MOUNT" git collect
overlay_repack "$MOUNT" "$OUT" "$FLAVOR" "no unexpanded submodules"
