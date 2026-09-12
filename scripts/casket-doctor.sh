#!/bin/bash
# Preflight check for a new host (docs/setup.md). Verifies what the selected
# usage mode needs and prints OK/WARN/FAIL per item — read-only, changes
# nothing. Exit non-zero if any FAIL.
#
# Usage: casket-doctor.sh [--view|--build]
#   --view  (default) viewing already-built caskets: mounts + optional search
#   --build building caskets too: adds tools, registry auth, output dir
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"
set +e +o pipefail

MODE="view"
[[ "${1:-}" == "--build" ]] && MODE="build"
[[ "${1:-}" == "-h" || "${1:-}" == "--help" ]] && { echo "casket-doctor.sh [--view|--build]"; exit 0; }

FAIL=0
ok()   { printf 'OK    %s\n' "$*"; }
warn() { printf 'WARN  %s\n' "$*"; }
bad()  { printf 'FAIL  %s\n' "$*"; FAIL=1; }

echo "# casket-doctor ($MODE mode)  CASKET_WORK=$CASKET_WORK"

# --- common: viewing ---------------------------------------------------
for c in jq awk python3; do
    command -v "$c" >/dev/null && ok "tool: $c" || bad "tool missing: $c"
done
command -v rg >/dev/null && ok "tool: rg (casket-mcp search)" || warn "rg missing (casket-mcp text search degraded)"

if [[ -f "$CONFIG_DIR/static-mounts.tsv" ]]; then
    n=$(grep -cvE '^\s*(#|$)' "$CONFIG_DIR/static-mounts.tsv")
    ok "config/static-mounts.tsv: $n entries"
    while IFS=$'\t' read -r artifact mnt; do
        [[ -z "$artifact" || "$artifact" == \#* ]] && continue
        [[ -s "$artifact" ]] || bad "static-mounts artifact missing: $artifact"
    done < "$CONFIG_DIR/static-mounts.tsv"
else
    warn "config/static-mounts.tsv not found (fine if everything is registry-managed)"
fi

if [[ -r "$CASKET_WORK/state/registry.json" ]]; then
    live=$(python3 "$REGISTRY_PY" list --status live --json 2>/dev/null | jq 'length')
    ok "registry: ${live:-0} live entries"
else
    warn "state/registry.json unreadable (fine on a view-only host using static-mounts only)"
fi

mounted=$(awk '$3=="squashfs" && $2 ~ /^\/srv\/sources-/' /proc/mounts | wc -l)
if (( mounted > 0 )); then
    ok "mounts: $mounted squashfs under /srv/sources-*"
else
    warn "no casket mounts yet — run: sudo ./scripts/casket-mounts.sh --apply"
fi

systemctl is-enabled casket-mounts.service >/dev/null 2>&1 \
    && ok "boot service: casket-mounts.service enabled" \
    || warn "boot service not enabled — mounts won't survive reboot (sudo ./scripts/install-mounts-service.sh)"

# --- optional search backends (informational) ---------------------------
curl -s -o /dev/null -m 3 -w '%{http_code}' "${OPENGROK_URL:-http://localhost:8080}/" 2>/dev/null | grep -q 200 \
    && ok "opengrok: webapp up" || warn "opengrok: not running (optional; casket-mcp/ripgrep still work)"

# --- build mode extras ---------------------------------------------------
if [[ "$MODE" == "build" ]]; then
    for c in oc curl sha256sum mksquashfs xz rpm2cpio cpio tar xargs flock; do
        command -v "$c" >/dev/null && ok "tool: $c" || bad "tool missing: $c"
    done
    if [[ -r "$AUTHFILE" ]]; then
        ok "authfile: $AUTHFILE"
        for reg in quay.io registry.redhat.io; do
            jq -e --arg r "$reg" '.auths[$r] // .auths[("https://" + $r)] // empty' "$AUTHFILE" >/dev/null 2>&1 \
                && ok "auth: $reg present" || warn "auth: $reg not in authfile (needed for pulls from it)"
        done
    else
        bad "authfile unreadable: $AUTHFILE (set AUTHFILE or install a pull secret)"
    fi
    if [[ -d "$CASKET_OUT" && -w "$CASKET_OUT" ]]; then
        ok "output dir writable: $CASKET_OUT ($(df -h --output=avail "$CASKET_OUT" | tail -1 | tr -d ' ') free)"
    else
        bad "output dir missing/unwritable: $CASKET_OUT (set CASKET_OUT or mkdir)"
    fi
    avail_gb=$(df -BG --output=avail "$CASKET_WORK" | tail -1 | tr -dc 0-9)
    (( avail_gb >= 200 )) && ok "work space: ${avail_gb}G free at CASKET_WORK" \
        || warn "work space: only ${avail_gb}G free at CASKET_WORK (a full B rebuild wants ~150G transient)"
fi

(( FAIL )) && echo "# result: FAIL (fix the FAIL lines above)" || echo "# result: OK"
exit "$FAIL"
