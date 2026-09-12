#!/bin/bash
# List (and, with --apply, delete) retired casket artifacts older than a
# retention window. Retired = casket-swap.sh already moved production off
# them; they're rollback insurance only. Dry-run by default.
#
# Usage: casket-cleanup.sh [--older-than-days N] [--apply]
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"
set +e +o pipefail

DAYS=14; APPLY=0
while (( $# )); do
    case "$1" in
        --older-than-days) DAYS="$2"; shift 2 ;;
        --apply) APPLY=1; shift ;;
        -h|--help) echo "casket-cleanup.sh [--older-than-days N] [--apply]"; exit 0 ;;
        *) die "unknown arg: $1" ;;
    esac
done
require_cmd python3 jq date du

CUTOFF=$(date -u -d "-${DAYS} days" +%Y-%m-%dT%H:%M:%SZ)
log "retention: $DAYS days (cutoff $CUTOFF)"

FOUND=0
while IFS= read -r entry; do
    [[ -z "$entry" ]] && continue
    retired_at=$(echo "$entry" | jq -r '.retired_at // empty')
    [[ -n "$retired_at" ]] || continue
    [[ "$retired_at" < "$CUTOFF" ]] || continue
    FOUND=1
    entry_id=$(echo "$entry" | jq -r '.entry_id')
    id=$(echo "$entry" | jq -r '.id')
    path=$(echo "$entry" | jq -r '.artifact_path')
    size="(missing)"
    [[ -e "$path" ]] && size=$(du -h "$path" 2>/dev/null | awk '{print $1}')
    echo "entry_id=$entry_id id=$id retired_at=$retired_at size=$size path=$path"
    if (( APPLY )); then
        rm -f "$path" && log "  deleted $path"
        python3 "$REGISTRY_PY" remove --entry-id "$entry_id" && log "  removed entry_id=$entry_id from registry"
    fi
done < <(python3 "$REGISTRY_PY" list --status retired --json | jq -c '.[]')

(( FOUND )) || { echo "nothing older than $DAYS days"; exit 0; }
(( APPLY )) || echo "(dry-run; re-run with --apply to delete files + remove registry entries)"
