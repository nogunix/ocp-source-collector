#!/bin/bash
# Common helpers for casket-ocp pipeline.

set -euo pipefail

# Default CASKET_WORK to this checkout (parent of scripts/), not $HOME: the
# documented `sudo ./scripts/casket-swap.sh --apply` otherwise resolves to
# /root/<repo> and can't see the registry.
_CASKET_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
: "${CASKET_WORK:=$(dirname "$_CASKET_LIB_DIR")}"
export CASKET_WORK
: "${AUTHFILE:=${HOME:-/root}/.docker/config.json}"
# Final .sqfs.xz artifacts land here (big, read-mostly storage).
: "${CASKET_OUT:=/mnt/hdd/casket-ocp}"
export CASKET_OUT
: "${RELEASE_REGISTRY:=quay.io/openshift-release-dev/ocp-release}"
: "${CONFIG_DIR:=${CASKET_WORK}/config}"
: "${REGISTRY_PY:=${CASKET_WORK}/scripts/registry.py}"
# Casket image format: sqfs (squashfs + xz, legacy default) or erofs (erofs + zstd,
# better random-read performance for source browsing). Inspired by srpmix7's mkcasket
# which supports both. RHEL 9+ kernels mount erofs natively.
: "${CASKET_FORMAT:=sqfs}"

log()  { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*" >&2; }
die()  { log "ERROR: $*"; exit 1; }

require_cmd() {
    for c in "$@"; do
        command -v "$c" >/dev/null 2>&1 || die "missing command: $c"
    done
}

# --- artifact path handoff -------------------------------------------------
# A packaging script names its output once, at the top, from `date -u`.
# casket-build.sh used to rebuild that name itself, from a SECOND `date -u`
# evaluated after the build returned. Both are UTC, so this is not the
# local-vs-UTC bug package.sh already carries a comment about -- it is the
# same expression evaluated hours apart. A build long enough to straddle a UTC
# midnight then makes the two disagree and the register step dies with
# "expected artifact not found" on a casket that built perfectly:
# b-operand 4.19 on 2026-08-16, an 8h50m build, 13G stranded unregistered.
# b-operand runs 3-9h per minor, so this is reachable on any given week.
#
# The producer now records the path it actually wrote and the caller reads it
# back, so the name is decided exactly once.

# record_artifact <path> [file] — called by a packaging script after its
# mksquashfs succeeded. No file requested (the usual manual invocation) is not
# an error, it just does nothing.
record_artifact() {
    local path="$1" out="${2:-}"
    [[ -n "$out" ]] || return 0
    printf '%s\n' "$path" > "$out" \
        || log "WARNING: could not record artifact path to $out"
}

# artifact_path_from <file> <fallback> — called by casket-build.sh. Falls back
# to the caller's guess when the file is missing or empty, so a half-updated
# checkout (or a packaging script invoked without the flag) behaves exactly as
# it did before rather than failing outright.
artifact_path_from() {
    local f="${1:-}" fallback="${2:-}" p=""
    if [[ -n "$f" && -s "$f" ]]; then
        # `read` returns non-zero at EOF without a trailing newline but HAS
        # already filled p, so the failure must not clear it -- a truncated
        # write would otherwise silently fall back to the caller's guess,
        # which is the exact bug this helper exists to prevent.
        IFS= read -r p < "$f" || :
    fi
    if [[ -n "$p" ]]; then
        printf '%s\n' "$p"
    else
        printf '%s\n' "$fallback"
    fi
}

# File extension for the current CASKET_FORMAT.
casket_ext() {
    case "$CASKET_FORMAT" in
        sqfs)  echo "sqfs.xz" ;;
        erofs) echo "erofs.zstd" ;;
        *) die "unknown CASKET_FORMAT: $CASKET_FORMAT (want sqfs or erofs)" ;;
    esac
}

# casket_mkfs <stage_dir> <output_path> [extra squashfs flags...]
# Builds a casket image from <stage_dir>. Extra flags are passed to mksquashfs
# only (erofs ignores them). Both tools get -all-root (or --all-root).
casket_mkfs() {
    local stage="$1" out="$2"
    shift 2
    rm -f "$out"
    mkdir -p "$(dirname "$out")"
    case "$CASKET_FORMAT" in
        sqfs)
            require_cmd mksquashfs
            mksquashfs "$stage" "$out" \
                -comp xz "$@" \
                -no-progress -all-root -noappend
            ;;
        erofs)
            require_cmd mkfs.erofs
            # Every flag here is load-bearing; measured on ocp4.14.58
            # (20.25 GB, 1.87M files) against the 1.47 GB xz squashfs:
            #   bare `-z zstd`                       8.74 GB, 331s metadata walk
            #   + dedupe,fragments,ztailpacking,L12  1.86 GB,   9.4s
            #   + -C 131072                          1.61 GB,   7.0s
            # 64% of the tree is under 4 KB, so without fragments/ztailpacking
            # each of those files burns a whole 4 KB block (4.57 GB of floor on
            # this tree alone); -C matches squashfs's 128 KB compression window,
            # which the default 4 KB pcluster is 32x smaller than.
            mkfs.erofs --all-root \
                -z zstd,level=12 -C 131072 \
                -E dedupe,fragments,ztailpacking \
                "$out" "$stage"
            ;;
        *) die "unknown CASKET_FORMAT: $CASKET_FORMAT (want sqfs or erofs)" ;;
    esac
}

version_dir() {
    local ver="$1"
    printf '%s/ocp%s' "$CASKET_WORK" "$ver"
}

# mount_path_for <phase> <unit> [<fingerprint>]
# Production mount naming per phase, as used by the existing swap-*.sh scripts.
# Phase identifiers reflect what each phase actually is (see README.md
# "Operations" section): "a-rpm" and "b-operand" are sub-resolutions of A and B
# (same source artifact, one level deeper), not independent siblings.
# NB: these are just the case-label names -- the mount *paths* below are
# unchanged production paths, not renamed in this pass.
#   a:         /srv/sources-ocp<PATCH>          (mounts by full patch; unit is
#                                                 the minor, so the current
#                                                 patch -- the fingerprint --
#                                                 is required)
#   a-rpm:     /srv/sources-ocp-srpms           (single mount, unit is always "all")
#   b:         /srv/sources-ocp<MINOR>-operators
#   b-operand: /srv/sources-layered-ocp<MINOR>
mount_path_for() {
    local phase="$1" unit="$2" fingerprint="${3:-}"
    case "$phase" in
        a) [[ -n "$fingerprint" ]] || die "mount_path_for: phase a needs a fingerprint (current patch)"
           printf '/srv/sources-ocp%s' "$fingerprint" ;;
        a-rpm) printf '/srv/sources-ocp-srpms' ;;
        b) printf '/srv/sources-ocp%s-operators' "$unit" ;;
        b-operand) printf '/srv/sources-layered-ocp%s' "$unit" ;;
        *) die "mount_path_for: unknown phase: $phase" ;;
    esac
}

# Operator-catalog selection for the phase-b pipeline. CATALOG env var picks
# the index (default: redhat = redhat-operator-index, the original Phase B
# target); certified/community reuse the identical pipeline against
# certified-operator-index / community-operator-index. Sets:
#   CATALOG          redhat|certified|community
#   CATALOG_SUFFIX   "" for redhat (backward-compatible paths/names),
#                    "-certified"/"-community" otherwise -- appended to the
#                    work dir (phase-b<suffix>/<minor>) and the casket name
#                    (casket-<date>-ocp<minor><suffix>-operators.sqfs.xz)
#   CATALOG_INDEX    registry.redhat.io/redhat/<catalog>-operator-index
catalog_setup() {
    : "${CATALOG:=redhat}"
    case "$CATALOG" in
        redhat) CATALOG_SUFFIX="" ;;
        certified|community) CATALOG_SUFFIX="-${CATALOG}" ;;
        *) die "bad CATALOG=$CATALOG (want redhat|certified|community)" ;;
    esac
    CATALOG_INDEX="registry.redhat.io/redhat/${CATALOG}-operator-index"
}

parse_args_version_arch() {
    VERSION=""
    ARCH="x86_64"
    while [[ $# -gt 0 ]]; do
        case "$1" in
            -v|--version) VERSION="$2"; shift 2 ;;
            -a|--arch)    ARCH="$2";    shift 2 ;;
            -h|--help)    print_usage; exit 0 ;;
            *) die "unknown arg: $1" ;;
        esac
    done
    [[ -n "$VERSION" ]] || die "version required (-v X.Y.Z)"
    case "$ARCH" in
        x86_64|aarch64|ppc64le|s390x|multi) ;;
        *) die "unsupported arch: $ARCH" ;;
    esac
}
