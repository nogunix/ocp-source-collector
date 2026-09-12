#!/bin/bash
# Reconcile the casket squashfs mounts under /srv against the desired state:
#   desired = state/registry.json entries with status=live
#           + config/static-mounts.tsv (caskets outside the registry: the two
#             RHEL caskets, catalog caskets, anything hand-managed)
#
# This replaces the old one-fstab-line-per-casket operation (which had grown
# past 50 lines): fstab no longer lists caskets at all; boot-time mounting is
# systemd/casket-mounts.service running this script with --apply.
#
# Actions per mountpoint (scope: /srv/sources-* only — never touches anything
# else):
#   missing            -> mount -o loop,ro <artifact> <mountpoint>
#   wrong backing file -> umount (lazy on busy) + mount the desired artifact
#   not desired        -> umount (a retired casket whose registry entry moved)
#
# Dry-run by default; --apply needs root. Idempotent.
#
# Usage: casket-mounts.sh [--apply]
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"
set +e +o pipefail

APPLY=0
[[ "${1:-}" == "--apply" ]] && APPLY=1
[[ "${1:-}" == "-h" || "${1:-}" == "--help" ]] && { echo "casket-mounts.sh [--apply]"; exit 0; }
require_cmd python3 jq awk losetup mountpoint
(( APPLY )) && [[ $EUID -ne 0 ]] && die "--apply mounts/unmounts; re-run with sudo"

STATIC_TSV="$CONFIG_DIR/static-mounts.tsv"

# --- desired state: mount_path -> artifact_path --------------------------
declare -A WANT
while IFS=$'\t' read -r artifact mnt; do
    [[ -z "$artifact" || "$artifact" == \#* || -z "$mnt" ]] && continue
    WANT["$mnt"]="$artifact"
done < <(
    python3 "$REGISTRY_PY" list --status live --json 2>/dev/null \
        | jq -r '.[] | select(.mount_path and .artifact_path) | "\(.artifact_path)\t\(.mount_path)"'
    [[ -f "$STATIC_TSV" ]] && grep -vE '^[[:space:]]*(#|$)' "$STATIC_TSV"
)
[[ ${#WANT[@]} -gt 0 ]] || die "desired state is empty (registry unreadable and no $STATIC_TSV?) — refusing to unmount everything"

# --- current state: squashfs mounts under /srv/sources-* -----------------
declare -A HAVE
while read -r dev mnt fstype _; do
    [[ "$fstype" == "squashfs" && "$mnt" == /srv/sources-* ]] || continue
    backing=$(losetup -nO BACK-FILE "$dev" 2>/dev/null | head -1 | awk '{$1=$1};1')
    HAVE["$mnt"]="${backing:-unknown}"
done < /proc/mounts

run() { echo "+ $*"; (( APPLY )) && "$@"; }

n_ok=0 n_mount=0 n_remount=0 n_umount=0 fail=0
for mnt in "${!WANT[@]}"; do
    artifact="${WANT[$mnt]}"
    if [[ ! -s "$artifact" ]]; then
        echo "SKIP  $mnt (artifact missing on disk: $artifact)"; fail=1; continue
    fi
    if [[ -z "${HAVE[$mnt]:-}" ]]; then
        echo "MOUNT $mnt <- $artifact"
        run mkdir -p "$mnt"
        run mount -o loop,ro "$artifact" "$mnt" || fail=1
        n_mount=$((n_mount+1))
    elif [[ "${HAVE[$mnt]}" != "$artifact" ]]; then
        echo "SWAP  $mnt: ${HAVE[$mnt]} -> $artifact"
        run umount "$mnt" || run umount -l "$mnt"
        run mount -o loop,ro "$artifact" "$mnt" || fail=1
        n_remount=$((n_remount+1))
    else
        n_ok=$((n_ok+1))
    fi
done
for mnt in "${!HAVE[@]}"; do
    if [[ -z "${WANT[$mnt]:-}" ]]; then
        echo "UMOUNT $mnt (was ${HAVE[$mnt]}; no longer desired)"
        run umount "$mnt" || run umount -l "$mnt"
        n_umount=$((n_umount+1))
    fi
done

log "reconcile: ok=$n_ok mount=$n_mount swap=$n_remount umount=$n_umount$( ((APPLY)) || echo ' (dry-run)')"
exit "$fail"
