#!/usr/bin/env bash
#
# stop-opengrok.sh — stop and remove the OpenGrok container.
#
# The index lives in the named volume (default opengrok-data) and is left
# intact, so the next `run-opengrok.sh` reuses it instead of reindexing from
# scratch. Pass --wipe-index to also delete the index volume.
#
set -euo pipefail

CONTAINER="${CONTAINER:-casket-ocp-grok}"
DATA_VOLUME="${DATA_VOLUME:-opengrok-data}"

log() { printf '[stop-opengrok] %s\n' "$*" >&2; }

log "stopping and removing container '$CONTAINER'"
podman rm -f "$CONTAINER" >/dev/null 2>&1 || true

if [ "${1:-}" = "--wipe-index" ]; then
  log "wiping index volume '$DATA_VOLUME' (next run will reindex from scratch)"
  podman volume rm "$DATA_VOLUME" >/dev/null 2>&1 || true
else
  log "index volume '$DATA_VOLUME' kept (use --wipe-index to delete it)"
fi
log "done."
