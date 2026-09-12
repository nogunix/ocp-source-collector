#!/bin/bash
# Build one unit for one phase, driving the existing phase-specific pipeline
# scripts unchanged. Dry-run by default (prints the commands it would run);
# --apply actually runs them and, on success, registers a `staged` entry in
# the registry with the fingerprint captured via scripts/lib-fingerprint.sh
# (the exact same logic casket-check.sh uses, so a build always matches what
# the next check will compare against).
#
# Usage: casket-build.sh --phase a|a-rpm|b|b-operand --unit UNIT [--arch x86_64] [--jobs N] [--outdir DIR] [--apply] [--keep-stage]
#   UNIT is a minor (a/b/b-operand, e.g. 4.20) or "all" (a-rpm, the only unit it has).
#
# On success it also deletes the unit's 50-out staging trees, which are pure
# derived output -- leaving them behind is what filled the root fs on
# 2026-08-09 (see lib-reclaim.sh). Fetched tarballs are always kept, so a
# rebuild re-downloads nothing. --keep-stage opts out.
#
# a-rpm (Phase A's RPM/SRPM sibling -- see README.md "Operations" section) still
# needs its manual subscribed-VM collection step first -- this only drives
# phase-a-rpm-package.sh over whatever phase-a-rpm/srpms/ already holds. That
# manual step is out of scope for automation on purpose (needs a login +
# subscription).
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"
# shellcheck source=lib-fingerprint.sh
source "$SCRIPT_DIR/lib-fingerprint.sh"
# shellcheck source=lib-reclaim.sh
source "$SCRIPT_DIR/lib-reclaim.sh"

PHASE=""; UNIT=""; ARCH="x86_64"; JOBS=6; OUT_DIR="${CASKET_OUT}"; APPLY=0
while (( $# )); do
    case "$1" in
        --phase)  PHASE="$2"; shift 2 ;;
        --unit)   UNIT="$2";  shift 2 ;;
        --arch)   ARCH="$2";  shift 2 ;;
        --jobs)   JOBS="$2";  shift 2 ;;
        --outdir) OUT_DIR="$2"; shift 2 ;;
        --apply)  APPLY=1;    shift ;;
        --keep-stage) export CASKET_KEEP_STAGE=1; shift ;;
        -h|--help)
            echo "casket-build.sh --phase a|a-rpm|b|b-operand --unit UNIT [--arch x86_64] [--jobs N] [--outdir DIR] [--apply] [--keep-stage]"
            exit 0 ;;
        *) die "unknown arg: $1" ;;
    esac
done
case "$PHASE" in a|a-rpm|b|b-operand) ;; *) die "--phase required: a|a-rpm|b|b-operand" ;; esac
[[ -n "$UNIT" ]] || die "--unit required"

# Where the packaging script reports the path it actually wrote. casket-build
# must NOT re-derive that name: both sides used `date -u`, but evaluated hours
# apart, and a build that straddles a UTC midnight then looks for a file that
# was never created (b-operand 4.19, 2026-08-16: 8h50m, 13G stranded). See
# lib.sh record_artifact / artifact_path_from.
ARTIFACT_FILE="$(mktemp -t casket-artifact.XXXXXX)"
trap 'rm -f "$ARTIFACT_FILE"' EXIT

run() {
    echo "+ $*"
    # `if` (not `(( APPLY )) && ...`) so a dry-run (APPLY=0, i.e. the `((`
    # test itself "fails") doesn't trip `set -e` and abort the whole script.
    if (( APPLY )); then
        "$@"
    fi
}

register() {
    local fingerprint="$1" artifact_path="$2" mount_path="$3"
    [[ -s "$artifact_path" ]] || die "expected artifact not found: $artifact_path"
    local entry_id
    entry_id=$(python3 "$REGISTRY_PY" add --phase "$PHASE" --unit "$UNIT" \
        --fingerprint "$fingerprint" --artifact-path "$artifact_path" --mount-path "$mount_path")
    log "registered entry_id=$entry_id (status=staged) for ${PHASE}-${UNIT}"
}

# Reclaim this unit's derived staging trees now that the casket is on disk and
# registered. The guard and the removal live in lib-reclaim.sh (with tests in
# tests/test_reclaim.sh); this wrapper only adds the dry-run check and the
# free-space line. See lib-reclaim.sh for why this is not optional.
reclaim() {
    (( APPLY )) || return 0
    reclaim_stage "$CASKET_WORK" "$@"
    log "reclaimed ${RECLAIMED_COUNT} staging dir(s); $(df -h --output=avail "$CASKET_WORK" | tail -1 | tr -d ' ') free"
}

build_a() {
    local patch mount_path
    patch="$(fetch_patch "$UNIT")"
    [[ -n "$patch" ]] || die "could not resolve current patch for minor $UNIT (stable channel unreachable?)"
    log "phase a: minor=$UNIT -> current patch=$patch"
    run "$SCRIPT_DIR/discover.sh"  -v "$patch" -a "$ARCH"
    run "$SCRIPT_DIR/fetch-git.sh" -v "$patch" --jobs "$JOBS"
    run "$SCRIPT_DIR/manifest.sh"  -v "$patch"
    run "$SCRIPT_DIR/package.sh"   -v "$patch" -o "$OUT_DIR" --artifact-out "$ARTIFACT_FILE"
    (( APPLY )) || return 0
    mount_path="$(mount_path_for a "$UNIT" "$patch")"
    register "$patch" "$(artifact_path_from "$ARTIFACT_FILE" \
        "$OUT_DIR/casket-$(date -u +%Y%m%d)-ocp${patch}.$(casket_ext)")" "$mount_path"
    # Phase A never retires an old patch, so each z-stream adds a whole new
    # ocp<patch>/ work dir -- the stages accumulate by count as well as by size.
    reclaim "$(version_dir "$patch")/50-out"
}

build_b() {
    local digest mount_path
    digest="$(fetch_catalog_digest "$UNIT")"
    [[ -n "$digest" ]] || die "could not resolve catalog digest for minor $UNIT (registry unreachable?)"
    log "phase b: minor=$UNIT -> catalog digest=$digest"
    run "$SCRIPT_DIR/phase-b-discover.sh"      -v "$UNIT" -a "$ARCH"
    run "$SCRIPT_DIR/phase-b-fetch-bundles.sh" -v "$UNIT" --jobs "$JOBS"
    run "$SCRIPT_DIR/phase-b-fetch-source.sh"  -v "$UNIT" --jobs "$JOBS"
    run "$SCRIPT_DIR/phase-b-resolve-v2.sh"    -v "$UNIT" --jobs "$JOBS"
    run "$SCRIPT_DIR/phase-b-package.sh"       -v "$UNIT" -o "$OUT_DIR" --artifact-out "$ARTIFACT_FILE"
    (( APPLY )) || return 0
    mount_path="$(mount_path_for b "$UNIT")"
    register "$digest" "$(artifact_path_from "$ARTIFACT_FILE" \
        "$OUT_DIR/casket-$(date -u +%Y%m%d)-ocp${UNIT}-operators.$(casket_ext)")" "$mount_path"
    # No catalog flag is passed above, so CATALOG_SUFFIX is empty and the stage
    # is plain phase-b/<minor>. certified/community are built by another path.
    reclaim "$CASKET_WORK/phase-b/$UNIT/50-out"
}

build_b_operand() {
    local digest mount_path pkg infix
    digest="$(fetch_catalog_digest "$UNIT")"
    [[ -n "$digest" ]] || die "could not resolve catalog digest for minor $UNIT (registry unreachable?)"
    log "phase b-operand: minor=$UNIT -> catalog digest=$digest (shared with phase b)"
    while IFS=$'\t' read -r pkg infix; do
        [[ -z "$pkg" || "$pkg" == \#* ]] && continue
        # With the full catalog in products.tsv, any per-product failure must
        # skip just that product, never the minor: a package can be absent
        # from an older minor's catalog (trustee-operator: v4.15+ only), its
        # CSV can lack relatedImages (images.tsv empty -> fetch-source dies),
        # or none of its operand images resolve to a public source.
        if ! run "$SCRIPT_DIR/phase-b-operand-discover.sh" -p "$pkg" -v "$UNIT" -a "$ARCH"; then
            log "skipping $pkg for $UNIT (discover failed — not in this catalog?)"
            continue
        fi
        if ! run "$SCRIPT_DIR/phase-b-operand-fetch-source.sh" -p "$pkg" -v "$UNIT" --jobs "$JOBS"; then
            log "skipping $pkg for $UNIT (fetch-source failed — no operand images?)"
            continue
        fi
        # OSC: the podvm payload's guest-components/kata sources are pinned in
        # the CAA repo's versions.yaml, not in any image label — pull them too.
        if [[ "$pkg" == "sandboxed-containers-operator" ]]; then
            run "$SCRIPT_DIR/phase-b-operand-osc-extras.sh" -p "$pkg" -v "$UNIT"
        fi
        if ! run "$SCRIPT_DIR/phase-b-operand-package.sh"  -p "$pkg" -v "$UNIT" -i "$infix" --stage-only; then
            log "skipping $pkg for $UNIT (package failed)"
            continue
        fi
    done < "$CONFIG_DIR/phase-b-operand-products.tsv"
    run "$SCRIPT_DIR/phase-b-operand-combine.sh" -v "$UNIT" -o "$OUT_DIR" --artifact-out "$ARTIFACT_FILE"
    (( APPLY )) || return 0
    mount_path="$(mount_path_for b-operand "$UNIT")"
    register "$digest" "$(artifact_path_from "$ARTIFACT_FILE" \
        "$OUT_DIR/casket-$(date -u +%Y%m%d)-layered-ocp${UNIT}.$(casket_ext)")" "$mount_path"
    # One stage per product per minor -- ~190 of them, the largest single
    # contributor to the 2026-08-09 fill. combine.sh has already consumed them.
    #
    # _layered/<minor> must go in the SAME call, and must not be forgotten:
    # combine.sh hardlinks it (cp -al) to those per-product stages, so deleting
    # only the 50-out dirs transfers sole ownership of the data to _layered
    # instead of freeing it. 154G for 4.16 alone.
    reclaim "$CASKET_WORK"/phase-b-operand/*/"$UNIT"/50-out \
            "$CASKET_WORK/phase-b-operand/_layered/$UNIT"
}

build_a_rpm() {
    [[ "$UNIT" == "all" ]] || die "phase a-rpm has a single unit: --unit all"
    local fingerprint mount_path
    # Collection is deliberately NOT run from here.
    #
    # phase-a-rpm/srpms/ is the merge point of three independent streams
    # (node-OS base, node-OS extensions, in-container RPM layer) and only the
    # first is automated. Running the collector from the build would populate
    # that directory with one stream and then package it -- the "3270 -> 795"
    # regression docs/a-rpm-collection.md was written to prevent. The
    # collector therefore writes to srpms-<date>/ and the merge stays a
    # human step, same as it was with the VM.
    #
    #   scripts/phase-a-rpm-collect.sh            # -> phase-a-rpm/srpms-<date>/
    #   scripts/repackage-srpms-refresh.sh ...    # incremental overlay
    #
    log "phase a-rpm: packaging whatever is already merged into phase-a-rpm/srpms/"
    log "phase a-rpm:   (collect + merge are separate; see docs/a-rpm-collection.md)"
    run "$SCRIPT_DIR/phase-a-rpm-package.sh" -o "$OUT_DIR" --artifact-out "$ARTIFACT_FILE"
    (( APPLY )) || return 0
    fingerprint="$(phase_a_rpm_fingerprint)"
    mount_path="$(mount_path_for a-rpm all)"
    register "$fingerprint" "$(artifact_path_from "$ARTIFACT_FILE" \
        "$OUT_DIR/casket-$(date -u +%Y%m%d)-ocp-srpms.$(casket_ext)")" "$mount_path"
    # phase-a-rpm/srpms/ is the collected input (the manual VM step) and stays;
    # only the packaging stage is derived.
    reclaim "$CASKET_WORK/phase-a-rpm/50-out"
}

require_cmd curl awk jq oc python3
case "$PHASE" in
    a) build_a ;;
    a-rpm) build_a_rpm ;;
    b) build_b ;;
    b-operand) build_b_operand ;;
esac
(( APPLY )) || echo "(dry-run; re-run with --apply to actually build + register)"
