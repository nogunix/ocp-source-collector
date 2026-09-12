#!/bin/bash
# Collect SRPMs for all tracked OCP versions using a container instead of a VM.
#
# Replaces the manual VM workflow (scp scripts, ssh, run 01/02/03, rsync back).
# Runs the casket-srpm-collect:el9 container with host entitlements mounted.
#
# Usage: phase-a-rpm-collect.sh [--build-image] [--versions "4.20.32 4.22.8"]
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

IMAGE="casket-srpm-collect:el9"
BUILD_IMAGE=0
VERSIONS_OVERRIDE=""

while (( $# )); do
    case "$1" in
        --build-image) BUILD_IMAGE=1; shift ;;
        --versions)    VERSIONS_OVERRIDE="$2"; shift 2 ;;
        -h|--help)     echo "phase-a-rpm-collect.sh [--build-image] [--versions 'ver1 ver2']"; exit 0 ;;
        *)             die "unknown arg: $1" ;;
    esac
done

require_cmd podman

PHASE_A_RPM="${CASKET_WORK}/phase-a-rpm"
AUTHFILE="${AUTHFILE:-$HOME/.docker/config.json}"

# Build the container image if requested or if it doesn't exist
if (( BUILD_IMAGE )) || ! podman image exists "$IMAGE" 2>/dev/null; then
    log "building $IMAGE"
    podman build -f "$SCRIPT_DIR/../containers/srpm-collect/Containerfile" \
        -t "$IMAGE" "$SCRIPT_DIR/../containers/srpm-collect"
fi

# Resolve versions from config if not overridden
if [[ -z "$VERSIONS_OVERRIDE" ]]; then
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

[[ -n "$VERSIONS_OVERRIDE" ]] || die "no versions to collect"

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

log "running collect in container"
# --log-driver=none: podman defaults to journald here, which records the
# container output BOTH under the container name and again through
# podman's own stdout that systemd captures -- every line appeared twice
# in `journalctl --user -u ...`. Verified with a one-line echo container.
podman run --rm --log-driver=none \
    "${ENTITLEMENT_MOUNTS[@]}" \
    -v "$AUTHFILE:/run/pull-secret.json:ro,z" \
    -v "$OUT_DIR:/out:z" \
    "$IMAGE" \
    /work/collect.sh -o /out -V "$VERSIONS_OVERRIDE" \
        -S "$SRPM_SUBDIR" -R "$RPMDB_SUBDIR"

n_srpms=$(ls "$OUT_DIR/$SRPM_SUBDIR/"*.src.rpm 2>/dev/null | wc -l)
n_tsvs=$(ls "$OUT_DIR/$RPMDB_SUBDIR/"*.tsv 2>/dev/null | wc -l)
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
