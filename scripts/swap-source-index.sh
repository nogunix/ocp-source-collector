#!/usr/bin/env bash
# Swap production casket mounts to the source-index rebuild (casket-<DATE>-*).
# For each in-scope /srv mount, repoint its fstab line to the new dated file,
# daemon-reload, and restart the systemd mount unit. Old files are left on disk
# for rollback. Idempotent. Run with --apply to actually change things; without
# it, prints the plan (dry-run).
#
# Usage: swap-source-index.sh [--date YYYYMMDD] [--apply]
set -uo pipefail
DATE=20260608; APPLY=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --date) DATE="$2"; shift 2 ;;
        --apply) APPLY=1; shift ;;
        *) echo "unknown: $1" >&2; exit 1 ;;
    esac
done
OUTDIR="${CASKET_OUT:-/mnt/hdd/casket-ocp}"
run() { echo "+ $*"; [[ "$APPLY" == 1 ]] && sudo "$@"; }

for m in /srv/sources-ocp4.*/ /srv/sources-layered-ocp4.*/; do
    mp=${m%/}
    suffix=$(basename "$mp"); suffix=${suffix#sources-}
    newf="$OUTDIR/casket-$DATE-$suffix.sqfs.xz"
    [[ -s "$newf" ]] || { echo "SKIP $suffix: new file missing ($newf)"; continue; }
    cur=$(awk -v mp="$mp" '$2==mp{print $1}' /etc/fstab)
    [[ -n "$cur" ]] || { echo "SKIP $suffix: no fstab line for $mp"; continue; }
    [[ "$cur" == "$newf" ]] && { echo "OK   $suffix: already $newf"; continue; }
    echo "SWAP $suffix: $cur -> $newf"
    unit=$(systemd-escape -p --suffix=mount "$mp")
    # awk exact-field rewrite, not sed+regex: `${cur//#/\\#}` (meant to
    # escape a literal '#' for sed's '#'-delimited pattern) doesn't actually
    # work in bash 5.x -- a bare '#' in `${var//#/...}` is parsed as the
    # start-anchor operator, not a literal character, so it silently no-ops
    # instead of escaping. That leaves an unescaped '#' as the sed pattern's
    # delimiter-terminator, so the substitution never matches a real fstab
    # line and this swap has been a no-op. Exact string field comparison
    # sidesteps regex-escaping entirely (fstab paths can also contain other
    # regex metacharacters like '.' that a sed pattern would need to escape).
    run bash -c "awk -v mp='$mp' -v newsrc='$newf' '\$2==mp{\$1=newsrc} {print}' /etc/fstab > /etc/fstab.tmp.\$\$ && mv /etc/fstab.tmp.\$\$ /etc/fstab"
    run systemctl daemon-reload
    if [[ "$APPLY" == 1 ]]; then
        if ! sudo systemctl restart "$unit"; then
            echo "  restart failed (loop busy?) -> umount -l + start"
            sudo umount -l "$mp" 2>/dev/null || true
            sudo systemctl start "$unit"
        fi
        sleep 1
        if mountpoint -q "$mp" && [[ -e "$mp" ]]; then
            echo "  mounted OK: $(awk -v mp="$mp" '$2==mp{print $1}' /etc/fstab)"
        else
            echo "  WARN: $mp not mounted after swap"
        fi
    fi
done
[[ "$APPLY" == 1 ]] || echo "(dry-run; re-run with --apply to change fstab + remount)"
