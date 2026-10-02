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
# Staleness guard (2026-09-25): the staged entry picked is simply the one with
# the highest entry_id, and nothing used to compare it with live. A staged
# build left behind by an earlier run is therefore swapped in as a silent
# DOWNGRADE -- 8 b-operand entries from 08-15..08-21 sat staged behind a live
# 08-29..09-01 build until they were retired by hand on 2026-09-23. When the
# swap REPLACES a mount (same mount_path as live), a staged build older than
# live is refused, dry-run included, unless --allow-older is given for a
# deliberate rollback. Phase a is additive (a new patch is a new mountpoint),
# so an older patch there replaces nothing and is not refused.
#
# Usage: casket-swap.sh --phase a|a-rpm|b|b-operand --unit UNIT [--apply] [--allow-older]
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"
set +e +o pipefail   # we do our own error handling below; don't abort mid-loop

PHASE=""; UNIT=""; APPLY=0; ALLOW_OLDER=0
while (( $# )); do
    case "$1" in
        --phase) PHASE="$2"; shift 2 ;;
        --unit)  UNIT="$2";  shift 2 ;;
        --apply) APPLY=1;    shift ;;
        --allow-older) ALLOW_OLDER=1; shift ;;
        -h|--help) echo "casket-swap.sh --phase a|a-rpm|b|b-operand --unit UNIT [--apply] [--allow-older]"; exit 0 ;;
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
STAGED_BUILT=$(echo "$STAGED_JSON" | jq -r '.built_at // empty')
[[ -s "$STAGED_ARTIFACT" ]] || die "staged artifact missing on disk: $STAGED_ARTIFACT"

LIVE_JSON=$(python3 "$REGISTRY_PY" get --phase "$PHASE" --unit "$UNIT" --status live 2>/dev/null)
LIVE_ARTIFACT=""; LIVE_MOUNT=""; LIVE_BUILT=""
if [[ -n "$LIVE_JSON" ]]; then
    LIVE_ARTIFACT=$(echo "$LIVE_JSON" | jq -r '.artifact_path')
    LIVE_MOUNT=$(echo "$LIVE_JSON" | jq -r '.mount_path')
    LIVE_BUILT=$(echo "$LIVE_JSON" | jq -r '.built_at // empty')
fi

# Other staged entries for this unit are never swapped (the highest entry_id
# wins), so they only ever accumulate. Name them so they get retired.
OTHER_STAGED=$(python3 "$REGISTRY_PY" list --phase "$PHASE" --unit "$UNIT" --status staged 2>/dev/null \
    | awk -F'\t' -v keep="$STAGED_ENTRY_ID" 'NR>1 && $1!=keep {print $1}' | paste -sd' ')

run() {
    echo "+ $*"
    if (( APPLY )); then
        "$@" || return $?
    fi
}

echo "unit:     ${PHASE}-${UNIT}"
echo "mount:    $STAGED_MOUNT"
echo "current:  ${LIVE_ARTIFACT:-<none live yet>}${LIVE_BUILT:+  (built $LIVE_BUILT)}"
echo "new:      $STAGED_ARTIFACT${STAGED_BUILT:+  (built $STAGED_BUILT)}"
[[ -n "$OTHER_STAGED" ]] && echo "note:     other staged entries for this unit, never swapped: $OTHER_STAGED -- retire them (registry.py transition --entry-id N --to retired)"

# ISO-8601 UTC strings compare correctly as strings.
if [[ -n "$LIVE_BUILT" && -n "$STAGED_BUILT" && "$LIVE_MOUNT" == "$STAGED_MOUNT" \
      && "$STAGED_BUILT" < "$LIVE_BUILT" ]]; then
    if (( ALLOW_OLDER )); then
        log "WARNING: staged build is OLDER than live -- proceeding because of --allow-older"
    else
        die "staged entry_id=$STAGED_ENTRY_ID (built $STAGED_BUILT) is OLDER than live (built $LIVE_BUILT); swapping it would downgrade $STAGED_MOUNT. Retire it (registry.py transition --entry-id $STAGED_ENTRY_ID --to retired), or pass --allow-older for a deliberate rollback"
    fi
fi

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
