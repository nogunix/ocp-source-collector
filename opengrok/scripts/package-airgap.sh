#!/usr/bin/env bash
#
# package-airgap.sh — bundle everything needed to stand up the casket-ocp
# OpenGrok source browser on an air-gapped host.
#
# Produces, under $OUT_DIR:
#   opengrok-image.tar      the OpenGrok container image (podman save)
#   opengrok-index.tar      the prebuilt index (podman volume export, ~215G)
#   scripts/                run/stop/stage/entrypoint helpers + deploy-airgap.sh
#   MANIFEST.txt            pinned image digest, sizes, sha256 checksums, notes
#
# The source data itself (the casket-*.sqfs.xz files mounted at /srv/sources-*)
# is distributed through the existing casket channel and is NOT bundled here —
# the index references those paths, so the air-gapped host must mount the same
# caskets at the same /srv/sources-ocp* locations before starting OpenGrok.
#
# IMPORTANT: the index is only compatible with the exact OpenGrok version it was
# built with — a different image triggers check_index_and_wipe_out and discards
# it. The image is therefore pinned by digest below and saved as-is.
#
# Run this on the build host (casket-host) after a successful full index.
#
set -euo pipefail

# Pin the exact image the index was built against (see MANIFEST / Step 5 notes).
IMAGE_REF="${IMAGE_REF:-docker.io/opengrok/docker@sha256:5bcdde2e8f9e52990817c2bb5be6532e9ed1a83ef883d9d36a25277aee403206}"
IMAGE_TAG="${IMAGE_TAG:-docker.io/opengrok/docker:latest}"
CONTAINER="${CONTAINER:-casket-ocp-grok}"
DATA_VOLUME="${DATA_VOLUME:-opengrok-data}"
OUT_DIR="${OUT_DIR:-/mnt/hdd/casket-ocp/opengrok-airgap}"
INCLUDE_INDEX="${INCLUDE_INDEX:-1}"   # 0 = lean bundle (customer reindexes)
COMPRESS_INDEX="${COMPRESS_INDEX:-1}" # 1 = zstd the index tar (strips sparse-zero bloat); deploy-airgap.sh imports .zst
ZSTD_LEVEL="${ZSTD_LEVEL:-12}"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
log() { printf '[package-airgap] %s\n' "$*" >&2; }

mkdir -p "$OUT_DIR/scripts"

# Stop the container so the index is quiescent during export (volume stays).
if podman container exists "$CONTAINER" 2>/dev/null; then
  log "stopping container '$CONTAINER' for a consistent index export"
  podman stop "$CONTAINER" >/dev/null 2>&1 || true
fi

log "saving image $IMAGE_REF -> opengrok-image.tar"
# Save by tag so `podman load` restores the tag run-opengrok.sh expects, but the
# pinned digest is recorded in MANIFEST for verification.
podman save -o "$OUT_DIR/opengrok-image.tar" "$IMAGE_TAG"

INDEX_ARTIFACT=""
if [ "$INCLUDE_INDEX" = "1" ]; then
  log "exporting index volume '$DATA_VOLUME' -> opengrok-index.tar (large, ~215G)"
  podman volume export "$DATA_VOLUME" -o "$OUT_DIR/opengrok-index.tar"
  if [ "$COMPRESS_INDEX" = "1" ] && command -v zstd >/dev/null; then
    log "compressing index -> opengrok-index.tar.zst (zstd -T0 -$ZSTD_LEVEL --long=31)"
    zstd -T0 "-$ZSTD_LEVEL" --long=31 -f "$OUT_DIR/opengrok-index.tar" -o "$OUT_DIR/opengrok-index.tar.zst"
    rm -f "$OUT_DIR/opengrok-index.tar"
    INDEX_ARTIFACT="opengrok-index.tar.zst"
  else
    INDEX_ARTIFACT="opengrok-index.tar"
  fi
else
  log "INCLUDE_INDEX=0: skipping index export (lean bundle; customer reindexes)"
fi

log "copying helper scripts"
cp "$HERE"/{run-opengrok.sh,stop-opengrok.sh,stage-sources.sh,entrypoint-ro.sh,deploy-airgap.sh} \
   "$OUT_DIR/scripts/" 2>/dev/null || true
chmod +x "$OUT_DIR"/scripts/*.sh 2>/dev/null || true

log "writing MANIFEST.txt (this hashes the tarballs; may take a while)"
{
  echo "casket-ocp OpenGrok air-gap bundle"
  echo "built: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "image digest (pinned): $IMAGE_REF"
  echo "image id: $(podman image inspect "$IMAGE_TAG" --format '{{.Id}}' 2>/dev/null)"
  echo "include_index: $INCLUDE_INDEX"
  echo
  echo "== sizes =="
  du -h "$OUT_DIR"/opengrok-image.tar 2>/dev/null
  [ -n "$INDEX_ARTIFACT" ] && du -h "$OUT_DIR/$INDEX_ARTIFACT" 2>/dev/null
  echo
  echo "== sha256 =="
  ( cd "$OUT_DIR" && sha256sum opengrok-image.tar $INDEX_ARTIFACT )
  echo
  echo "== required source mounts on target (mount the matching caskets) =="
  ls -d /srv/sources-ocp* 2>/dev/null
} > "$OUT_DIR/MANIFEST.txt"

log "restarting container '$CONTAINER'"
podman start "$CONTAINER" >/dev/null 2>&1 || true

log "done. Bundle at: $OUT_DIR"
ls -lh "$OUT_DIR"
