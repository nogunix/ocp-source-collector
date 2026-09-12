#!/bin/bash
# Phase B: pull the redhat-operator-index for a given OCP minor and parse the
# File-Based Catalog (FBC) into per-operator TSVs.
#
# Usage: phase-b-discover.sh -v 4.20 [-a x86_64]
#   -v is the OCP minor (e.g. 4.20). Patch is not relevant for the operator
#   catalog (the index is tagged vX.Y).
#
# Outputs under $CASKET_WORK/phase-b/<minor>/00-discover/:
#   configs/<operator>/catalog.json        # extracted FBC NDJSON, verbatim
#   operators.tsv                          # name TAB default_channel TAB head_bundle TAB head_bundle_image
#   bundles.tsv                            # operator TAB bundle TAB channel TAB bundle_image
#   containers.tsv                         # operator TAB head_bundle TAB containerImage (from CSV annotation)

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"

print_usage() {
    cat <<'EOF'
phase-b-discover.sh -v <minor> [-a x86_64]
  -v, --version   OCP minor, e.g. 4.20
  -a, --arch      x86_64 (default) | aarch64 | ppc64le | s390x
EOF
}

parse_args_version_arch "$@"
case "$ARCH" in
    x86_64)  ARCH_FILTER="linux/amd64"   ;;
    aarch64) ARCH_FILTER="linux/arm64"   ;;
    ppc64le) ARCH_FILTER="linux/ppc64le" ;;
    s390x)   ARCH_FILTER="linux/s390x"   ;;
    *)       die "multi not supported for catalog; pick one arch" ;;
esac

require_cmd oc jq python3
MINOR="$VERSION"  # phase-b uses minor only

catalog_setup
WORK="${CASKET_WORK}/phase-b${CATALOG_SUFFIX}/${MINOR}"
OUT="$WORK/00-discover"
CONFIGS="$OUT/configs"
mkdir -p "$CONFIGS"

INDEX="${CATALOG_INDEX}:v${MINOR}"

# 1. Extract /configs from the index (FBC). Idempotent.
if [[ -z "$(ls -A "$CONFIGS" 2>/dev/null)" ]]; then
    log "extracting catalog /configs from $INDEX"
    oc image extract --registry-config="$AUTHFILE" --filter-by-os="$ARCH_FILTER" \
        --path "/configs/:$CONFIGS/" "$INDEX"
else
    log "configs/ already populated, skipping extract"
fi

n_operators=$(find "$CONFIGS" -maxdepth 1 -mindepth 1 -type d | wc -l)
log "operators in catalog: $n_operators"

# FBC layouts vary per package and have drifted over time: a single
# catalog.json; split package.json + channels.json + bundles.json (ACM);
# arbitrary names (netobserv: index.json); nested bundles/*.json (cephcsi);
# and konflux-era catalog.yaml (multi-doc YAML). Each new shape used to be
# silently skipped (50/153 packages in the 4.20 index until 2026-07-17).
# Normalize: concatenate EVERY *.json (recursive) + convert EVERY *.yaml
# into one NDJSON per package under $OUT/.ndjson/, and run jq on that —
# the same approach phase-b-operand-discover.sh has always used.
NDJSON_DIR="$OUT/.ndjson"
mkdir -p "$NDJSON_DIR"
pkg_ndjson() {  # $1 = op_dir ; echoes the ndjson path, or returns 1
    local dir="${1%/}" out
    out="$NDJSON_DIR/$(basename "$dir").ndjson"
    {
        find "$dir" -name '*.json' -type f -exec cat {} + 2>/dev/null
        find "$dir" \( -name '*.yaml' -o -name '*.yml' \) -type f -print0 2>/dev/null \
          | xargs -0 -r python3 -c '
import sys, json, yaml
for p in sys.argv[1:]:
    try:
        for doc in yaml.safe_load_all(open(p)):
            if doc is not None:
                print(json.dumps(doc, default=str))
    except Exception as e:
        print(f"WARN: yaml parse failed {p}: {e}", file=sys.stderr)
'
    } > "$out"
    [[ -s "$out" ]] || return 1
    echo "$out"
}

# 2. operators.tsv : name, defaultChannel, head_bundle_name, head_bundle_image
{
    for op_dir in "$CONFIGS"/*/; do
        cat=$(pkg_ndjson "$op_dir") || continue
        name=$(basename "${op_dir%/}")
        defchan=$(jq -r 'select(.schema=="olm.package") | .defaultChannel' "$cat" 2>/dev/null | head -1)
        [[ -z "$defchan" || "$defchan" == "null" ]] && continue
        # Compute true channel head: entries minus (replaces ∪ skips). When
        # multiple heads exist (orphaned old chains), pick the last in array
        # order (catalogs typically append newer bundles).
        head_bundle=$(jq -r --arg c "$defchan" '
            select(.schema=="olm.channel" and .name==$c)
            | (.entries // []) as $e
            | ($e | map(.name)) as $names
            | (($e | map(.replaces // empty)) + ($e | map(.skips // []) | add // [])) as $superseded
            | ($names - $superseded) as $heads
            | if ($heads | length) == 0 then ($names | last)
              else ($e | map(select(.name as $n | $heads | index($n))) | last | .name)
              end
        ' "$cat" 2>/dev/null | head -1)
        [[ -z "$head_bundle" || "$head_bundle" == "null" ]] && continue
        head_image=$(jq -r --arg n "$head_bundle" \
            'select(.schema=="olm.bundle" and .name==$n) | .image' \
            "$cat" 2>/dev/null | head -1)
        printf '%s\t%s\t%s\t%s\n' "$name" "$defchan" "$head_bundle" "$head_image"
    done
} > "$OUT/operators.tsv"
n_with_head=$(wc -l < "$OUT/operators.tsv")
log "operators with default-channel head: $n_with_head"

# Denominator check — the lesson of the 2026-06-09 containerImage drop and
# the 2026-07-17 YAML-FBC drop, both of which silently skipped packages while
# every log line stayed green: ALWAYS print numerator/denominator side by
# side and name what fell through. (`_`-prefixed dirs are FBC metadata, e.g.
# _operator-deprecations-content, not packages.)
comm -23 \
  <(find "$CONFIGS" -maxdepth 1 -mindepth 1 -type d -printf '%f\n' | grep -v '^_' | sort) \
  <(cut -f1 "$OUT/operators.tsv" | sort) > "$OUT/uncovered.txt" || true
n_uncovered=$(wc -l < "$OUT/uncovered.txt")
if (( n_uncovered > 0 )); then
    log "WARNING: $n_uncovered/$((n_with_head + n_uncovered)) catalog packages have NO operators.tsv row (no parsable FBC / defaultChannel / head):"
    sed 's/^/    /' "$OUT/uncovered.txt" >&2
else
    log "coverage: all $n_with_head catalog packages captured"
fi

# 3. bundles.tsv : all bundles across all channels (for reference)
{
    for op_dir in "$CONFIGS"/*/; do
        cat="$NDJSON_DIR/$(basename "${op_dir%/}").ndjson"
        [[ -s "$cat" ]] || continue
        name=$(basename "${op_dir%/}")
        # First: build channel→entries map
        jq -r --arg op "$name" '
            select(.schema=="olm.channel") as $chan
            | $chan.entries[]
            | [$op, .name, $chan.name] | @tsv
        ' "$cat" 2>/dev/null
    done | sort -u > "$OUT/.bundle-channel.tmp"

    # Then merge with bundle→image map
    {
        for op_dir in "$CONFIGS"/*/; do
            cat="$NDJSON_DIR/$(basename "${op_dir%/}").ndjson"
            [[ -s "$cat" ]] || continue
            name=$(basename "${op_dir%/}")
            jq -r --arg op "$name" '
                select(.schema=="olm.bundle") | [$op, .name, .image] | @tsv
            ' "$cat" 2>/dev/null
        done
    } > "$OUT/.bundle-image.tmp"

    LC_ALL=C join -t $'\t' -1 1 -2 1 -o 1.1,1.2,2.3,1.3 \
        <(awk -F'\t' '{print $1"|"$2"\t"$3}' "$OUT/.bundle-channel.tmp" | LC_ALL=C sort -u) \
        <(awk -F'\t' '{print $1"|"$2"\t"$3}' "$OUT/.bundle-image.tmp"   | LC_ALL=C sort -u) \
        | awk -F'\t' 'BEGIN{OFS="\t"} {split($1, a, "|"); print a[1], a[2], $2, $3}'
} > "$OUT/bundles.tsv"
n_bundles=$(wc -l < "$OUT/bundles.tsv")
log "bundle-channel rows: $n_bundles"
rm -f "$OUT/.bundle-channel.tmp" "$OUT/.bundle-image.tmp"

# 4. containers.tsv : for each (operator, head_bundle), the containerImage
#    annotation captured from the bundle's CSV (filled by fetch-bundles).
#    Stub here; fetch-bundles will populate.
[[ -f "$OUT/containers.tsv" ]] || : > "$OUT/containers.tsv"

log "wrote $OUT/operators.tsv  ($n_with_head operators)"
log "wrote $OUT/bundles.tsv    ($n_bundles bundle-channel rows)"
log "next: scripts/phase-b-fetch-bundles.sh -v $MINOR"
