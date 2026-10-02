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

# load_build_env — read host-local build settings (the store locations) from
# ${CASKET_BUILD_ENV:-~/.config/casket/build.env}, the same file
# casket-auto-update.service loads with EnvironmentFile=. KEY=VALUE lines
# only; a variable already set in the environment is left alone. See
# systemd/casket-build.env.example.
load_build_env() {
    local f="${CASKET_BUILD_ENV:-$HOME/.config/casket/build.env}" line key val
    [[ -r "$f" ]] || return 0
    while IFS= read -r line || [[ -n "$line" ]]; do
        [[ "$line" =~ ^[[:space:]]*([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]] || continue
        key="${BASH_REMATCH[1]}" val="${BASH_REMATCH[2]}"
        [[ "$val" =~ ^\"(.*)\"$ || "$val" =~ ^\'(.*)\'$ ]] && val="${BASH_REMATCH[1]}"
        [[ -n "${!key+x}" ]] || export "$key=$val"
    done < "$f"
}

# ensure_github_token — make a GitHub token available to child processes.
# An exported GITHUB_TOKEN/GH_TOKEN wins; otherwise borrow the gh CLI's
# credential live (no second copy on disk to go stale behind `gh auth login`).
# Returns 0 when a token is available.
#
# collect-submodules.py reads pinned submodule commits from the git trees API,
# which allows 60 requests/hour unauthenticated. Without a token a build does
# not fail: it falls back to .gitmodules branch heads and records them exact=0.
# casket-auto-update.sh always borrowed gh's token, but a hand-run
# casket-build.sh did not, and the 2026-09-26 manual b-operand rebuild shipped
# 32-51 branch-head submodules per minor (ZTWIM's operator among them).
ensure_github_token() {
    if [[ -z "${GITHUB_TOKEN:-}" && -z "${GH_TOKEN:-}" ]] && command -v gh >/dev/null 2>&1; then
        local t
        t="$(gh auth token 2>/dev/null)" || t=""
        [[ -n "$t" ]] && export GITHUB_TOKEN="$t"
    fi
    [[ -n "${GITHUB_TOKEN:-}" || -n "${GH_TOKEN:-}" ]]
}

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

# stage_enrich <stage_dir> — the layers every source casket (a / b / b-operand)
# gets on top of its git/ trees, in dependency order. Neither collector ever
# fails the build; the index always runs.
stage_enrich() {
    local stage="$1"
    # A GitHub codeload archive writes every gitlink as an EMPTY DIR, so a tree
    # that uses submodules arrives as build glue with the code missing (the
    # 2026-08-19 scan: 1052 trees, all 2543 submodule dirs empty; ZTWIM's
    # release repo is nothing but Containerfiles + 5 empty submodules). Fill
    # them before collect-deps runs, so the filled-in trees get their own deps
    # collected too. Set CASKET_COLLECT_SUBMODULES=0 to skip.
    if [[ "${CASKET_COLLECT_SUBMODULES:-1}" == "1" ]]; then
        log "expanding git submodules"
        python3 "$SCRIPT_DIR/collect-submodules.py" "$stage" \
            --jobs "${CASKET_SUBMODULE_JOBS:-8}" \
            || log "collect-submodules failed (continuing with empty submodule dirs)"
    fi
    # Language-level dependency sources: a collected tree carries its own code,
    # but its deps only if that language vendors in-tree (Go usually, Rust/
    # Node/Python never). Fetch the rest from the lockfiles so a dependency CVE
    # is traceable offline. Set CASKET_COLLECT_DEPS=0 to skip.
    if [[ "${CASKET_COLLECT_DEPS:-1}" == "1" ]]; then
        log "collecting language-level dependency sources"
        python3 "$SCRIPT_DIR/collect-deps.py" "$stage" --jobs "${CASKET_DEP_JOBS:-12}" \
            || log "collect-deps failed (continuing without deps/)"
    fi
    log "building source index (INDEX.tsv + by-component/ + by-repo/)"
    python3 "$SCRIPT_DIR/build-source-index.py" "$stage"
}

# stage_normalize <stage_dir> — `oc image extract` produces 0640 files, which
# mksquashfs preserves verbatim (`-all-root` normalizes owner, not mode).
# Without this the mounted casket is unreadable to non-root users.
stage_normalize() {
    log "chmod -R a+rX $1"
    chmod -R a+rX "$1"
}

# casket_finalize <stage_dir> <output_path> <artifact_out> [squashfs flags...]
# Normalize, build the image, refuse an empty result, and record its path for
# casket-build.sh (an empty <artifact_out> records nothing).
casket_finalize() {
    local stage="$1" out="$2" artifact_out="$3"
    shift 3
    stage_normalize "$stage"
    log "building casket image (${CASKET_FORMAT}) -> $out"
    casket_mkfs "$stage" "$out" "$@" || die "casket_mkfs failed: $out"
    [[ -s "$out" ]] || die "casket_mkfs produced no output: $out"
    record_artifact "$out" "$artifact_out"
    log "final: $out ($(numfmt --to=iec --suffix=B "$(stat -c%s "$out")"))"
    file "$out"
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
