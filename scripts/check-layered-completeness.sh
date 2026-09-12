#!/usr/bin/env bash
# Gate before swapping a b-operand (layered) casket: does the new artifact
# actually carry the products the old one did?
#
# Why this exists: phase-b-operand-combine.sh validates only
# `[[ ${#included[@]} -gt 0 ]]`, and casket-build.sh's per-product loop skips
# any product whose discover/fetch-source/package step failed. A disk-full or
# a transient registry outage therefore does not fail the build -- it silently
# produces a THINNER casket, registers it as staged, and it looks fine. This
# compares each staged artifact against the live one it would replace and
# reports products that disappeared.
#
# Reads "Products included:" from the casket's own README.txt, so it costs one
# small file per artifact rather than a full listing of a 13G image.
#
# Usage: check-layered-completeness.sh [-v 4.18] [--fail-on-loss]
# Exit: 1 if --fail-on-loss and any minor lost a product.
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"
set +e +o pipefail

ONLY=""; FAIL_ON_LOSS=0
while (( $# )); do
    case "$1" in
        -v|--version) ONLY="$2"; shift 2 ;;
        --fail-on-loss) FAIL_ON_LOSS=1; shift ;;
        -h|--help) echo "check-layered-completeness.sh [-v MINOR] [--fail-on-loss]"; exit 0 ;;
        *) die "unknown arg: $1" ;;
    esac
done
require_cmd unsquashfs python3 jq

products_of() {  # $1 = .sqfs.xz path -> one product per line
    [[ -s "$1" ]] || return 1
    unsquashfs -cat "$1" README.txt 2>/dev/null \
        | sed -ne 's/^Products included: //p' | tr ' ' '\n' | sed '/^$/d' | sort
}

EXPECTED=$(grep -vc '^#' "$CONFIG_DIR/phase-b-operand-products.tsv" 2>/dev/null)
log "catalog lists $EXPECTED products (an upper bound: a product absent from an"
log "  older minor's catalog is a legitimate skip, not a loss)"
printf '\n%-7s %-8s %-8s %-7s %s\n' minor staged live delta note

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
rc=0
while IFS= read -r minor; do
    [[ -z "$minor" || "$minor" == \#* ]] && continue
    [[ -n "$ONLY" && "$minor" != "$ONLY" ]] && continue

    s_path=$(python3 "$REGISTRY_PY" get --phase b-operand --unit "$minor" --status staged 2>/dev/null | jq -r '.artifact_path // empty')
    l_path=$(python3 "$REGISTRY_PY" get --phase b-operand --unit "$minor" --status live   2>/dev/null | jq -r '.artifact_path // empty')
    if [[ -z "$s_path" ]]; then
        printf '%-7s %-8s %-8s %-7s %s\n' "$minor" - - - "no staged entry"; continue
    fi
    products_of "$s_path" > "$TMP/s" || { printf '%-7s %-8s %-8s %-7s %s\n' "$minor" ERR - - "cannot read $s_path"; rc=1; continue; }
    sn=$(wc -l < "$TMP/s")
    if [[ -z "$l_path" || ! -s "$l_path" ]]; then
        printf '%-7s %-8s %-8s %-7s %s\n' "$minor" "$sn" - - "no live artifact to compare"; continue
    fi
    products_of "$l_path" > "$TMP/l"
    ln=$(wc -l < "$TMP/l")
    lost=$(comm -13 "$TMP/s" "$TMP/l" | tr '\n' ' ')
    gained=$(comm -23 "$TMP/s" "$TMP/l" | wc -l)
    note="ok"
    if [[ -n "$lost" ]]; then note="LOST: $lost"; (( FAIL_ON_LOSS )) && rc=1; fi
    (( gained > 0 )) && note="$note (+$gained new)"
    printf '%-7s %-8s %-8s %+-7s %s\n' "$minor" "$sn" "$ln" "$((sn-ln))" "$note"
done < <(grep -v '^#' "$CONFIG_DIR/minors.txt" | sed '/^$/d')
exit $rc
