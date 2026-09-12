#!/bin/bash
# Point production at the latest staged build for one phase+unit.
#
# 2026-07-11 rework: caskets are no longer listed in /etc/fstab (that had
# grown past 50 lines). The mount source of truth is the registry (live
# entries) plus config/static-mounts.tsv, materialized by
# scripts/casket-mounts.sh (run at boot by systemd/casket-mounts.service).
# A swap is therefore just: replace the mount in place, then transition the
# registry entry to live.
#
# Mount naming (scripts/lib.sh mount_path_for) differs by phase:
#   a mounts by full PATCH (/srv/sources-ocp<patch>) -- a patch bump is a NEW
#     mountpoint, so older patches for the same minor stay mounted and their
#     registry entries stay live (older exact-patch browsing is a feature).
#   a-rpm/b/b-operand mount by a stable path -- a rebuild replaces the old
#     content in place, so the previous live entry is retired (file kept on
#     disk for rollback until casket-cleanup.sh).
#
# Usage: casket-swap.sh --phase a|a-rpm|b|b-operand --unit UNIT [--apply]
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"
set +e +o pipefail   # we do our own error handling below; don't abort mid-loop

PHASE=""; UNIT=""; APPLY=0
while (( $# )); do
    case "$1" in
        --phase) PHASE="$2"; shift 2 ;;
        --unit)  UNIT="$2";  shift 2 ;;
        --apply) APPLY=1;    shift ;;
        -h|--help) echo "casket-swap.sh --phase a|a-rpm|b|b-operand --unit UNIT [--apply]"; exit 0 ;;
        *) die "unknown arg: $1" ;;
    esac
done
case "$PHASE" in a|a-rpm|b|b-operand) ;; *) die "--phase required: a|a-rpm|b|b-operand" ;; esac
[[ -n "$UNIT" ]] || die "--unit required"
require_cmd python3 jq mountpoint

if (( APPLY )) && [[ $EUID -ne 0 ]]; then
    die "--apply mounts/unmounts; re-run with sudo"
fi

STAGED_JSON=$(python3 "$REGISTRY_PY" get --phase "$PHASE" --unit "$UNIT" --status staged 2>/dev/null)
[[ -n "$STAGED_JSON" ]] || die "no staged build for ${PHASE}-${UNIT}; run casket-build.sh --phase $PHASE --unit $UNIT --apply first"
STAGED_ENTRY_ID=$(echo "$STAGED_JSON" | jq -r '.entry_id')
STAGED_ARTIFACT=$(echo "$STAGED_JSON" | jq -r '.artifact_path')
STAGED_MOUNT=$(echo "$STAGED_JSON" | jq -r '.mount_path')
[[ -s "$STAGED_ARTIFACT" ]] || die "staged artifact missing on disk: $STAGED_ARTIFACT"

LIVE_JSON=$(python3 "$REGISTRY_PY" get --phase "$PHASE" --unit "$UNIT" --status live 2>/dev/null)
LIVE_ARTIFACT=""
[[ -n "$LIVE_JSON" ]] && LIVE_ARTIFACT=$(echo "$LIVE_JSON" | jq -r '.artifact_path')

run() {
    echo "+ $*"
    if (( APPLY )); then
        "$@" || return $?
    fi
}

echo "unit:     ${PHASE}-${UNIT}"
echo "mount:    $STAGED_MOUNT"
echo "current:  ${LIVE_ARTIFACT:-<none live yet>}"
echo "new:      $STAGED_ARTIFACT"

# 1. Replace the mount in place (no fstab involved).
if mountpoint -q "$STAGED_MOUNT" 2>/dev/null; then
    echo "SWAP: remount $STAGED_MOUNT"
    run umount "$STAGED_MOUNT" || { echo "  busy -> umount -l"; run umount -l "$STAGED_MOUNT"; }
else
    echo "ADD: new mountpoint $STAGED_MOUNT"
    run mkdir -p "$STAGED_MOUNT"
fi
run mount -o loop,ro "$STAGED_ARTIFACT" "$STAGED_MOUNT"

# 2. Verify, then make it official in the registry.
if (( APPLY )); then
    if ! mountpoint -q "$STAGED_MOUNT"; then
        die "$STAGED_MOUNT not mounted after swap -- registry left untouched, investigate before re-running"
    fi
    RETIRE_FLAG=()
    [[ "$PHASE" != "a" ]] && RETIRE_FLAG=(--retire-previous)
    python3 "$REGISTRY_PY" transition --entry-id "$STAGED_ENTRY_ID" --to live --mount-path "$STAGED_MOUNT" "${RETIRE_FLAG[@]}"
    log "registry: entry_id=$STAGED_ENTRY_ID -> live${RETIRE_FLAG:+ (previous live retired)}"
    log "reboot persistence comes from systemd/casket-mounts.service (casket-mounts.sh --apply)"
else
    echo "(dry-run; re-run with sudo ... --apply to remount + update registry)"
fi
