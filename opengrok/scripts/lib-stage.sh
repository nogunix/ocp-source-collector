#!/bin/bash
# lib-stage.sh — pure selection logic for stage-sources.sh, split out so it can
# be unit-tested without touching /srv (stage-sources.sh rm -rf's its staging
# tree on load, so it is not sourceable). Same pattern as scripts/lib-resolve.sh.
#
# Tests: tests/test_stage.sh

# stage-sources.sh defines its own log() right after sourcing this file; the
# fallback only exists so the lib is sourceable standalone from the tests.
command -v log >/dev/null 2>&1 || log() { printf '[lib-stage] %s\n' "$*" >&2; }

# cap_patches <keep>
#
# Read patch versions (one per line) on stdin, print the ones that survive the
# per-minor cap, version-sorted ascending. <keep> = how many z-streams of each
# minor to keep (newest first); 0 = keep everything.
#
# Sorting is `sort -V`, not lexical: 4.22.10 must outrank 4.22.5.
#
# A version may carry a "-<variant>" suffix (a-rpm publishes the node-OS
# extensions layer as by-ocp/<patch>-extensions/, deliberately a separate tree
# from the default-install set). Each variant is its own track: "4.20.32" and
# "4.20.32-extensions" describe the same release from two different images, not
# two z-streams of one thing. Capping them together is what the bare-minor key
# used to do, and with the production keep=1 it dropped the base project
# entirely -- sort -V ranks "4.20.32-extensions" above "4.20.32", so the newest
# "z-stream" of 4.20 became the 162-package extensions tree and the 568-package
# base tree fell off the end.
cap_patches() {
    local keep="${1:-0}"
    # Walk the version-sorted list newest-first, so "newest N of each track"
    # falls out of a single counter per track with no pre-grouping.
    sort -V -u | awk -v keep="$keep" '
        {
            rows[++n] = $0
            core = $0; tag = ""
            if (match(core, /-/)) {
                tag  = substr(core, RSTART)
                core = substr(core, 1, RSTART - 1)
            }
            sub(/\.[^.]*$/, "", core)   # patch -> minor
            tracks[n] = core tag
        }
        END {
            for (i = n; i >= 1; i--)
                if (keep == 0 || ++seen[tracks[i]] <= keep) kept[i] = 1
            for (i = 1; i <= n; i++)
                if (i in kept) print rows[i]
        }'
}

# is_kept <version> <newline-separated-kept-list>
is_kept() { printf '%s\n' "$2" | grep -qxF -- "$1"; }

# track_of <version> — the cap key cap_patches groups by, for log messages.
# "4.20.32" -> "4.20", "4.20.32-extensions" -> "4.20-extensions".
track_of() {
    local core="${1%%-*}" tag=""
    [ "$core" != "$1" ] && tag="-${1#*-}"
    printf '%s%s' "${core%.*}" "$tag"
}

# Strip a trailing "-<hex>" commit suffix (7-40 hex chars) from a dir name.
strip_sha() { sed -E 's/-[0-9a-f]{7,40}$//'; }

# link_git_phase <gitdir> <projdir>
#
# Link every <name>-<sha> subdir of <gitdir> into <projdir> under a clean name.
# On a clean-name collision, fall back to the full (hashed) name so nothing is
# lost.
#
# Returns 1 without creating <projdir> when <gitdir> holds no source tree at
# all. That is not a staging failure -- it means the collection resolved no
# public upstream for that component, which is routine for Phase B-operand:
# 97/146 layered-4.18 products were empty as of 2026-07-29 (39 with no source
# label at all, 49 whose labelled commit is an internal konflux SHA that 404s
# on public github but whose source is in the ocp-<patch> project anyway).
# Creating the dir anyway used to publish an empty OpenGrok project, whose
# /xref/<proj>/<name>/ URL 404s and reads as an indexing bug.
link_git_phase() {
    local gitdir="$1" proj="$2"
    local d name clean target n=0
    for d in "$gitdir"/*/; do
        [ -d "$d" ] || continue
        name="$(basename "$d")"
        clean="$(printf '%s' "$name" | strip_sha)"
        # Create the project dir only once there is something to put in it.
        [ "$n" -eq 0 ] && mkdir -p "$proj"
        target="$proj/$clean"
        [ -e "$target" ] && target="$proj/$name"   # collision -> keep hash
        ln -sfn "$gitdir/$name" "$target"
        n=$((n + 1))
    done
    if [ "$n" -eq 0 ]; then
        log "  $(basename "$proj"): no source collected — not staged"
        return 1
    fi
    log "  $(basename "$proj"): $n components"
    return 0
}
