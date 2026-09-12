#!/usr/bin/env bash
#
# deploy-airgap.sh — stand up the casket-ocp OpenGrok browser from an air-gap
# bundle produced by package-airgap.sh. Run this on the target (air-gapped) host
# from inside the unpacked bundle's scripts/ directory.
#
# Prerequisites on the target host:
#   - podman installed
#   - the matching casket-*.sqfs.xz sources mounted at /srv/sources-ocp*
#     (same paths as the build host — the index references them). Mount them the
#     usual casket way (fstab loop-mount of the squashfs) BEFORE running this.
#
# Steps performed:
#   1. podman load the OpenGrok image
#   2. recreate + import the prebuilt index volume (if opengrok-index.tar present)
#   3. (re)build the staging symlink tree and launch the container
#
# On first start the container runs one incremental sync (reads the sources to
# confirm nothing changed — no ctags reprocessing). Expect ~20-40 min of I/O for
# the full ~215G, then the browser serves instantly thereafter.
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUNDLE="$(cd "$HERE/.." && pwd)"   # bundle root (scripts/ lives under it)

IMAGE_TAR="${IMAGE_TAR:-$BUNDLE/opengrok-image.tar}"
# Index ships zstd-compressed (opengrok-index.tar.zst) to strip the sparse-zero
# bloat of `podman volume export`; fall back to a plain .tar if that's what's
# present. INDEX_TAR can override either.
INDEX_ZST="${INDEX_ZST:-$BUNDLE/opengrok-index.tar.zst}"
INDEX_TAR="${INDEX_TAR:-$BUNDLE/opengrok-index.tar}"
DATA_VOLUME="${DATA_VOLUME:-opengrok-data}"

log() { printf '[deploy-airgap] %s\n' "$*" >&2; }

# 1. load image
[ -f "$IMAGE_TAR" ] || { echo "missing $IMAGE_TAR" >&2; exit 1; }
log "loading image from $IMAGE_TAR"
podman load -i "$IMAGE_TAR"

# 2. restore index volume (skip for a lean bundle -> first run reindexes ~3h)
if [ -f "$INDEX_ZST" ] || [ -f "$INDEX_TAR" ]; then
  if podman volume exists "$DATA_VOLUME" 2>/dev/null; then
    log "volume '$DATA_VOLUME' already exists — leaving it as-is (delete it first to re-import)"
  else
    podman volume create "$DATA_VOLUME" >/dev/null
    if [ -f "$INDEX_ZST" ]; then
      command -v zstd >/dev/null || { echo "zstd required to import $INDEX_ZST" >&2; exit 1; }
      log "importing index volume '$DATA_VOLUME' from $INDEX_ZST (zstd stream, large)"
      zstd -dc --long=31 "$INDEX_ZST" | podman volume import "$DATA_VOLUME" -
    else
      log "importing index volume '$DATA_VOLUME' from $INDEX_TAR (large)"
      podman volume import "$DATA_VOLUME" "$INDEX_TAR"
    fi
  fi
else
  log "no index tar found — lean bundle; first start will build the index (~3h)"
fi

# 3. sanity-check the sources are mounted
if ! ls -d /srv/sources-ocp* >/dev/null 2>&1; then
  echo "ERROR: no /srv/sources-ocp* mounts found. Mount the casket sources first." >&2
  exit 1
fi

# 4. stage + run (run-opengrok.sh rebuilds staging and starts the container)
log "staging sources and launching OpenGrok"
DATA_VOLUME="$DATA_VOLUME" "$HERE/run-opengrok.sh"

log "deploy complete."
