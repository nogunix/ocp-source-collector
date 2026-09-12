#!/bin/bash
# Reclaiming derived 50-out staging trees, split out of casket-build.sh so the
# path guard can be tested without running a build (same pattern as
# lib-stage.sh / lib-resolve.sh). Network-free, no side effects on source.
#
# Why this exists: casket-build.sh used to leave every 50-out/ behind. A stage
# is pure derived output -- extracted source plus a full copy of the collected
# deps -- but nothing ever deleted it, so by 2026-08-09 286 stages had
# accumulated to ~666G and filled the root fs. Seven b-operand builds died with
# ENOSPC in seconds each, and the trailing run-opengrok.sh died mid-restage and
# left OpenGrok with 17 of 18 projects.
#
# It only grew teeth when deps collection landed (2026-07-26), taking a stage
# from a few GB to ~33G: $CASKET_DEP_STORE lives on the HDD while the work tree
# is on the root fs, so collect-deps' store->stage hardlinks cannot span the two
# filesystems and silently degrade to full copies. Every stage therefore
# carries its own complete copy of that minor's deps.

# stage_path_ok <work_root> <path>
#
# True when <path> is safe to rm -rf. Two shapes qualify, both pure derived
# output under <work_root>, and neither may contain "..":
#
#   <root>/**/50-out                        a unit's staging tree
#   <root>/phase-b-operand/_layered/<minor> b-operand's combined stage
#
# This predicate is the entire safety argument for the removal, which is why it
# is a separate function with its own tests rather than an inline condition.
#
# The ".." check is not redundant. In a bash [[ ]] pattern, `*` matches slashes
# too, so "$root"/*/50-out happily accepts "$root/../../elsewhere/50-out".
#
# The _layered case is not optional and was missed on the first pass (2026-08-09).
# phase-b-operand-combine.sh assembles it with `cp -al`, i.e. hardlinks into the
# per-product 50-out/stage trees. While those still exist the combined stage
# costs almost nothing, so it looks harmless -- but the moment the 50-out dirs
# are reclaimed the _layered links become the sole owners of the data and the
# space is never returned. Measured: 409G held by three minors' _layered after
# their 50-out dirs had been deleted. combine.sh only rm -rf's the minor it is
# about to rebuild, so every other minor's copy persists indefinitely.
stage_path_ok() {
    local root="$1" d="$2"
    [[ -n "$root" && -n "$d" ]] || return 1
    [[ "$d" != *..* ]]          || return 1
    # A unit's staging tree.
    [[ "$d" == "$root"/*/50-out ]] && return 0
    # b-operand's combined stage. Match the parent exactly so this cannot creep
    # down into _layered/<minor>/stage/git/... or up to _layered itself.
    [[ "${d%/*}" == "$root/phase-b-operand/_layered" && "$d" != "${d%/*}" ]] && return 0
    return 1
}

# reclaim_stage <work_root> <dir>...
#
# Removes every <dir> accepted by stage_path_ok. Non-existent paths are skipped
# silently, so an unmatched glob from the caller costs nothing. Sets
# RECLAIMED_COUNT to the number actually removed.
#
# CASKET_KEEP_STAGE=1 turns the whole thing into a no-op, for inspecting a build.
#
# Only the stage is touched: 00-discover/, 10-git/, 20-git/ and 40-manifest/
# stay, so a rebuild re-stages from tarballs already on disk and re-downloads
# nothing.
reclaim_stage() {
    local root="$1"; shift
    RECLAIMED_COUNT=0
    if [[ "${CASKET_KEEP_STAGE:-0}" == 1 ]]; then
        log "keeping staging trees (CASKET_KEEP_STAGE=1)"
        return 0
    fi
    local d
    for d in "$@"; do
        [[ -d "$d" ]] || continue          # also absorbs an unmatched glob
        if ! stage_path_ok "$root" "$d"; then
            log "refusing to remove unexpected stage path: $d"
            continue
        fi
        if rm -rf -- "$d"; then
            RECLAIMED_COUNT=$(( RECLAIMED_COUNT + 1 ))
        else
            log "failed to remove stage: $d"
        fi
    done
    return 0
}

# Fallback so the lib is usable (and testable) standalone; casket-build.sh
# sources lib.sh first and keeps its own richer log().
if ! declare -F log >/dev/null 2>&1; then
    log() { printf '[reclaim] %s\n' "$*" >&2; }
fi
