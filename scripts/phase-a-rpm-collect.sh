#!/bin/bash
# Collect SRPMs for all tracked OCP versions using a container instead of a VM.
#
# Replaces the manual VM workflow (scp scripts, ssh, run 01/02/03, rsync back).
# One container pass per RHCOS stream, in order:
#   el9   casket-srpm-collect:el9   (UBI9)   rhel-coreos     -> <patch>.tsv
#   el10  casket-srpm-collect:el10  (UBI10)  rhel-coreos-10  -> <patch>-el10.tsv
# Two images because each UBI's product certificate only unlocks its own
# major's repos (containers/srpm-collect/Containerfile). el10 matters from
# OCP 5.0 on, where fresh installs default to it; 4.21/4.22 ship it opt-in and
# get it too. Patches without rhel-coreos-10 are skipped by the el10 pass.
#
# Two rounds: every stream extracts its rpmdb tsvs first (collect.sh -E), then
# every stream fetches. A package is fetched by the stream its dist tag names,
# not the stream whose tsv lists it (RHCOS 10 ships some el9 builds), so the
# el9 fetch needs the el10 tsvs to exist already.
#
# Usage: phase-a-rpm-collect.sh [--build-image] [--versions "4.20.32 4.22.8"]
#                               [--majors "9 10"]
#
# --versions ""   skip version resolution and rpmdb extraction entirely and
#                 fetch for the tsvs already in rpmdb-<stamp>/ (set
#                 CASKET_COLLECT_STAMP to reuse an earlier run's directory).
#                 This is the re-fetch path: e.g. after adding -extensions
#                 tsvs, or retrying a failed stream.
#
# Prerequisites:
#   - Host registered with subscription-manager (for /etc/pki/entitlement)
#   - Pull secret at ~/.docker/config.json (or $AUTHFILE)
#   - podman
#
# Outputs under $CASKET_WORK/phase-a-rpm/:
#   rpmdb/<patch>.tsv     package lists (same as VM's 01 output)
#   srpms/*.src.rpm       source RPMs (same as VM's 02+03 output)
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"

IMAGE_REPO="casket-srpm-collect"
BUILD_IMAGE=0
VERSIONS_OVERRIDE=""
VERSIONS_GIVEN=0
MAJORS="9 10"

while (( $# )); do
    case "$1" in
        --build-image) BUILD_IMAGE=1; shift ;;
        --versions)    VERSIONS_OVERRIDE="$2"; VERSIONS_GIVEN=1; shift 2 ;;
        --majors)      MAJORS="$2"; shift 2 ;;
        -h|--help)     echo "phase-a-rpm-collect.sh [--build-image] [--versions 'ver1 ver2'] [--majors '9 10']"; exit 0 ;;
        *)             die "unknown arg: $1" ;;
    esac
done

require_cmd podman

PHASE_A_RPM="${CASKET_WORK}/phase-a-rpm"
AUTHFILE="${AUTHFILE:-$HOME/.docker/config.json}"

read -ra MAJOR_LIST <<< "$MAJORS"
(( ${#MAJOR_LIST[@]} )) || die "--majors is empty"
for major in "${MAJOR_LIST[@]}"; do
    [[ "$major" =~ ^[0-9]+$ ]] || die "--majors wants RHEL majors like '9 10', got: $major"
done

# Build each stream's image if requested, missing, or stale. Same
# Containerfile, the UBI base is what differs.
#
# "Stale" = built from other sources than the checkout's. The image bakes in
# collect.sh and cdn-fetch.py, so an image built before an edit keeps running
# the old copy: on 2026-11-09 the el10 pass died with "unknown arg: -M"
# because :el10 predated the -M option. Each build is labelled with a hash of
# its build context and rebuilt when the label no longer matches.
CTX="$SCRIPT_DIR/../containers/srpm-collect"
src_hash=$(cat "$CTX/Containerfile" "$CTX/collect.sh" "$CTX/cdn-fetch.py" "$CTX/route-rows.awk" | sha256sum | cut -c1-16)
for major in "${MAJOR_LIST[@]}"; do
    image="$IMAGE_REPO:el$major"
    have=$(podman image inspect --format '{{ index .Labels "casket.src-hash" }}' "$image" 2>/dev/null) || have=""
    if (( BUILD_IMAGE )) || [[ "$have" != "$src_hash" ]]; then
        log "building $image (sources ${have:-<none>} -> $src_hash)"
        podman build -f "$CTX/Containerfile" \
            --build-arg "BASE=registry.access.redhat.com/ubi${major}/ubi:latest" \
            --label "casket.src-hash=$src_hash" \
            -t "$image" "$CTX"
    fi
done

# Resolve versions from config if not given. An explicit --versions "" means
# "no extraction, reuse the tsvs" and must not fall into this branch: it used
# to, so the empty string was indistinguishable from "not passed" and a
# re-fetch always re-resolved (and re-extracted) every patch.
if (( ! VERSIONS_GIVEN )); then
    source "$SCRIPT_DIR/lib-fingerprint.sh"
    VERSIONS_OVERRIDE=""
    while IFS= read -r minor; do
        [[ -z "$minor" || "$minor" == \#* ]] && continue
        patch=$(fetch_patch "$minor") || continue
        VERSIONS_OVERRIDE+="$patch "
    done < "$SCRIPT_DIR/../config/phase-a-rpm-minors.txt"
    VERSIONS_OVERRIDE="${VERSIONS_OVERRIDE% }"
    log "resolved versions: $VERSIONS_OVERRIDE"
fi

if (( ! VERSIONS_GIVEN )); then
    [[ -n "$VERSIONS_OVERRIDE" ]] || die "no versions to collect"
fi

# Output goes to DATED subdirectories, never straight into srpms/.
#
# phase-a-rpm/srpms/ is the merge point of three independent collection
# streams (node-OS base = this script, node-OS extensions, in-container RPM
# layer) and assembling it is a human step -- docs/a-rpm-collection.md exists
# because building from a partially-merged srpms/ once shrank the casket from
# 3270 packages to 795. Writing this stream's output directly into it would
# reintroduce exactly that.
#
# Dated names are also the native input of the recommended incremental path:
#   repackage-srpms-refresh.sh -s phase-a-rpm/srpms-<date> -r .../rpmdb-<date>
# which overlays only the new packages onto the mounted casket.
OUT_DIR="$PHASE_A_RPM"
STAMP="${CASKET_COLLECT_STAMP:-$(date -u +%Y%m%d)}"
SRPM_SUBDIR="srpms-$STAMP"
RPMDB_SUBDIR="rpmdb-$STAMP"
mkdir -p "$OUT_DIR/$RPMDB_SUBDIR" "$OUT_DIR/$SRPM_SUBDIR"

# Entitlement. Two ways in, in this order:
#
#   1. The container registers itself from $RHSM_ENV. This is the path on this
#      host, which is Fedora: `subscription-manager repos --enable` needs repo
#      definitions generated from a RHEL product certificate, and a Fedora host
#      has no /etc/pki/product, so registering the HOST yields an empty
#      redhat.repo and every --enable fails. UBI9 carries
#      /etc/pki/product-default/479.pem, so registering inside the container
#      works. The file is mounted read-only rather than passed as environment,
#      so the key never shows up in `podman inspect`.
#   2. A host that is itself RHEL can lend its entitlements instead.
#
# One of the two must be present: without entitlement `dnf download --source`
# quietly returns nothing and the run would "succeed" with an empty srpms/.
ENTITLEMENT_MOUNTS=()
RHSM_ENV="${CASKET_RHSM_ENV:-$HOME/.config/casket/rhsm.env}"
have_entitlement=0
if [[ -f "$RHSM_ENV" ]]; then
    # ,z is not optional: the file lives under ~/.config and is therefore
    # SELinux user_home_t, which the container cannot read. Without it the
    # bind mount succeeds and the file reads as "absent" (permission denied
    # on stat), so collect.sh reports "no entitlement" for a credentials
    # file that is right there. The pull-secret and output mounts below
    # already carry it.
    ENTITLEMENT_MOUNTS+=(-v "$RHSM_ENV:/run/rhsm.env:ro,z")
    have_entitlement=1
    log "credentials: $RHSM_ENV (container registers itself)"
fi
if [[ -d /etc/pki/entitlement ]]; then
    ENTITLEMENT_MOUNTS+=(-v /etc/pki/entitlement:/etc/pki/entitlement:ro)
    [[ -d /etc/rhsm ]] && ENTITLEMENT_MOUNTS+=(-v /etc/rhsm:/etc/rhsm:ro)
    [[ -f /etc/yum.repos.d/redhat.repo ]] && \
        ENTITLEMENT_MOUNTS+=(-v /etc/yum.repos.d/redhat.repo:/etc/yum.repos.d/redhat.repo:ro)
    have_entitlement=1
    log "entitlement: host /etc/pki/entitlement"
fi
(( have_entitlement )) || die "no entitlement: create $RHSM_ENV (RHSM_ORG=, RHSM_ACTIVATION_KEY=, chmod 600) or register this host"

# A host RHEL's own entitlement (/etc/pki/entitlement + redhat.repo) is for
# its own major only. Lending it to the other major's container would hand
# collect.sh a redhat.repo with none of that major's repos -- an "entitled"
# run that fetches nothing -- so with no rhsm.env, only the host's major runs.
host_major=""
if [[ -r /etc/os-release ]]; then
    # `|| true`: on a non-RHEL host (this one is Fedora) the test fails, and
    # under set -e a failing $(...) assignment ends the script with no message.
    host_major=$(. /etc/os-release; [[ "${ID:-}" == rhel ]] && echo "${VERSION_ID%%.*}") || true
fi

# --log-driver=none: podman defaults to journald here, which records the
# container output BOTH under the container name and again through podman's
# own stdout that systemd captures -- every line appeared twice in
# `journalctl --user -u ...`. Verified with a one-line echo container.
run_collect() {
    local major="$1"; shift
    podman run --rm --log-driver=none \
        "${ENTITLEMENT_MOUNTS[@]}" \
        -v "$AUTHFILE:/run/pull-secret.json:ro,z" \
        -v "$OUT_DIR:/out:z" \
        "$IMAGE_REPO:el$major" \
        /work/collect.sh -o /out -M "$major" \
            -S "$SRPM_SUBDIR" -R "$RPMDB_SUBDIR" "$@"
}

# A failed stream does not stop the next one: the passes write disjoint tsvs
# and log files, and an el10 hiccup should not cost the el9 refresh (or the
# other way round). The failure is reported and exits non-zero.
failed=()

# Round 1: extract every stream's tsvs. No entitlement involved (it reads the
# release payload with the pull secret), so it runs for every major.
if [[ -n "$VERSIONS_OVERRIDE" ]]; then
    for major in "${MAJOR_LIST[@]}"; do
        log "extracting el$major rpmdb in $IMAGE_REPO:el$major"
        run_collect "$major" -V "$VERSIONS_OVERRIDE" -E \
            || { log "el$major: extract FAILED"; failed+=("el$major(extract)"); }
    done
else
    log "--versions \"\": no extraction, reusing the tsvs in $OUT_DIR/$RPMDB_SUBDIR"
fi
find "$OUT_DIR/$RPMDB_SUBDIR" -maxdepth 1 -name '*.tsv' | grep -q . \
    || die "no rpmdb tsvs in $OUT_DIR/$RPMDB_SUBDIR -- nothing to fetch"

# Round 2: fetch. Each pass sees every stream's tsvs and takes the rows whose
# dist tag is its own (containers/srpm-collect/route-rows.awk).
for major in "${MAJOR_LIST[@]}"; do
    if [[ ! -f "$RHSM_ENV" && "$major" != "$host_major" ]]; then
        log "el$major: skipped -- no $RHSM_ENV, and the host's entitlement does not cover RHEL $major"
        failed+=("el$major(no-entitlement)")
        continue
    fi
    log "fetching el$major SRPMs in $IMAGE_REPO:el$major"
    run_collect "$major" -V "" \
        || { log "el$major: collect FAILED"; failed+=("el$major"); }
done

# find, not `ls glob | wc`: with nothing fetched the glob does not match, ls
# fails, and pipefail ended the script here -- before the failure summary.
n_srpms=$(find "$OUT_DIR/$SRPM_SUBDIR" -maxdepth 1 -name '*.src.rpm' | wc -l)
n_tsvs=$(find "$OUT_DIR/$RPMDB_SUBDIR" -maxdepth 1 -name '*.tsv' | wc -l)
log "done: $n_tsvs rpmdb tsvs, $n_srpms SRPMs"
log "  $OUT_DIR/$SRPM_SUBDIR"
log "  $OUT_DIR/$RPMDB_SUBDIR"
log ""
log "This is ONE of three collection streams. Nothing is packaged yet."
log "To fold it into the live casket incrementally (recommended):"
log "  scripts/repackage-srpms-refresh.sh -m /srv/sources-ocp-srpms \\"
log "    -s $OUT_DIR/$SRPM_SUBDIR -r $OUT_DIR/$RPMDB_SUBDIR \\"
log "    -o \$CASKET_OUT/casket-\$(date -u +%Y%m%d)-ocp-srpms.sqfs.xz"
log "See docs/a-rpm-collection.md for the other streams and the merge."
(( ${#failed[@]} == 0 )) || die "streams not collected: ${failed[*]}"
