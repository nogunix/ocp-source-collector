#!/bin/bash
# Phase B / operand sources: for a single layered-product operator package,
# resolve the default-channel head bundle, pull it, and enumerate its CSV
# spec.relatedImages into images.tsv (the operand set whose sources this
# collects -- same redhat-operator-index catalog Phase B reads, one hop deeper).
#
# Usage: phase-b-operand-discover.sh -p <package> -v <minor> [-a x86_64]
#   -p   operator package name in redhat-operator-index (e.g. kubevirt-hyperconverged)
#   -v   OCP minor (e.g. 4.20). For RHOAI use the catalog minor that carries it.
#
# Outputs under $CASKET_WORK/phase-b-operand/<package>/<minor>/00-discover/:
#   config/catalog.json                 # extracted FBC NDJSON for this package
#   bundle/manifests/*                  # head bundle manifests (incl. CSV)
#   meta.tsv                            # package head_bundle version csv_path
#   images.tsv                          # component_name TAB image (operand set)
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"

print_usage() {
    cat <<'EOF'
phase-b-operand-discover.sh -p <package> -v <minor> [-a x86_64]
  -p, --package   operator package (e.g. kubevirt-hyperconverged)
  -v, --version   OCP minor, e.g. 4.20
  -a, --arch      x86_64 (default) | aarch64 | ppc64le | s390x
EOF
}

PKG=""; VERSION=""; ARCH="x86_64"
while (( $# )); do
    case "$1" in
        -p|--package) PKG="$2";     shift 2 ;;
        -v|--version) VERSION="$2";  shift 2 ;;
        -a|--arch)    ARCH="$2";     shift 2 ;;
        -h|--help)    print_usage; exit 0 ;;
        *) die "unknown arg: $1" ;;
    esac
done
[[ -n "$PKG" ]]     || die "package required (-p <name>)"
[[ -n "$VERSION" ]] || die "version required (-v X.Y)"
case "$ARCH" in
    x86_64)  ARCH_FILTER="linux/amd64"   ;;
    aarch64) ARCH_FILTER="linux/arm64"   ;;
    ppc64le) ARCH_FILTER="linux/ppc64le" ;;
    s390x)   ARCH_FILTER="linux/s390x"   ;;
    *)       die "unsupported arch: $ARCH" ;;
esac

require_cmd oc jq python3
MINOR="$VERSION"
WORK="${CASKET_WORK}/phase-b-operand/${PKG}/${MINOR}"
OUT="$WORK/00-discover"
CONFIG="$OUT/config"
BUNDLE="$OUT/bundle"
mkdir -p "$CONFIG" "$BUNDLE/manifests"

INDEX="registry.redhat.io/redhat/redhat-operator-index:v${MINOR}"

# 1. Extract just this package's FBC config. Idempotent.
#    FBC layout varies per package: a single catalog.json, OR split into
#    package.json + channels(.json|/) + bundles(.json|/), OR several files —
#    and newer konflux-built entries ship catalog.yaml (multi-doc YAML)
#    instead of JSON (50/153 packages in the 4.20 index as of 2026-07).
#    Combine everything into one NDJSON stream for jq.
if [[ -z "$(find "$CONFIG" \( -name '*.json' -o -name '*.yaml' -o -name '*.yml' \) -type f 2>/dev/null)" ]]; then
    # Prefer the whole-catalog extraction Phase B already made for this minor
    # (one oc-extract per minor) over re-pulling index layers per package —
    # with the full-catalog products.tsv that's ~191 extracts/minor otherwise.
    PHASE_B_CFG="${CASKET_WORK}/phase-b/${MINOR}/00-discover/configs/${PKG}"
    if [[ -n "$(find "$PHASE_B_CFG" \( -name '*.json' -o -name '*.yaml' -o -name '*.yml' \) -type f 2>/dev/null)" ]]; then
        log "reusing phase-b configs extraction for $PKG"
        cp -a "$PHASE_B_CFG/." "$CONFIG/"
    else
        log "extracting /configs/$PKG from $INDEX"
        oc image extract --registry-config="$AUTHFILE" --filter-by-os="$ARCH_FILTER" \
            --path "/configs/$PKG/:$CONFIG/" "$INDEX"
    fi
else
    log "config/ present, skipping index extract"
fi

CAT="$OUT/.catalog.ndjson"
find "$CONFIG" -name '*.json' -type f -exec cat {} + > "$CAT"
# YAML FBC -> NDJSON (append; a package ships one format or the other)
find "$CONFIG" \( -name '*.yaml' -o -name '*.yml' \) -type f -print0 2>/dev/null \
  | xargs -0 -r python3 -c '
import sys, json, yaml
for p in sys.argv[1:]:
    try:
        for doc in yaml.safe_load_all(open(p)):
            if doc is not None:
                print(json.dumps(doc, default=str))
    except Exception as e:
        print(f"WARN: yaml parse failed {p}: {e}", file=sys.stderr)
' >> "$CAT"
[[ -s "$CAT" ]] || die "no FBC json/yaml for package $PKG in $INDEX"
defchan=$(jq -r 'select(.schema=="olm.package") | .defaultChannel' "$CAT" | head -1)
[[ -n "$defchan" && "$defchan" != "null" ]] || die "no defaultChannel for $PKG"
log "defaultChannel: $defchan"

# 2. Compute the true channel head (entries minus replaces∪skips). Same logic
#    as phase-b-discover.sh.
head_bundle=$(jq -r --arg c "$defchan" '
    select(.schema=="olm.channel" and .name==$c)
    | (.entries // []) as $e
    | ($e | map(.name)) as $names
    | (($e | map(.replaces // empty)) + ($e | map(.skips // []) | add // [])) as $superseded
    | ($names - $superseded) as $heads
    | if ($heads | length) == 0 then ($names | last)
      else ($e | map(select(.name as $n | $heads | index($n))) | last | .name)
      end' "$CAT" | head -1)
[[ -n "$head_bundle" && "$head_bundle" != "null" ]] || die "could not compute head bundle"
head_image=$(jq -r --arg n "$head_bundle" \
    'select(.schema=="olm.bundle" and .name==$n) | .image' "$CAT" | head -1)
[[ -n "$head_image" && "$head_image" != "null" ]] || die "no image for head bundle $head_bundle"
log "head bundle: $head_bundle"
log "head image:  $head_image"

# 3. Pull the head bundle and extract /manifests (holds the CSV). Idempotent.
if [[ -z "$(ls -A "$BUNDLE/manifests" 2>/dev/null)" ]]; then
    log "extracting bundle manifests"
    oc image extract --registry-config="$AUTHFILE" --filter-by-os="$ARCH_FILTER" \
        --path "/manifests/:$BUNDLE/manifests/" "$head_image"
else
    log "bundle/manifests present, skipping"
fi

csv=$(find "$BUNDLE/manifests" -maxdepth 1 -name "*.clusterserviceversion.yaml" | head -1)
if [[ -z "$csv" ]]; then
    csv=$(find "$BUNDLE/manifests" -maxdepth 1 -type f \( -name "*.yaml" -o -name "*.yml" \) \
          | xargs -r grep -l "ClusterServiceVersion" 2>/dev/null | head -1)
fi
[[ -n "$csv" && -f "$csv" ]] || die "no CSV found in bundle manifests"
log "CSV: $csv"

# 4. Enumerate spec.relatedImages -> images.tsv (component_name TAB image).
#    component_name = image-repo basename with trailing -rhelN stripped.
python3 - "$csv" > "$OUT/images.tsv" <<'PY'
import re, sys, yaml
d = yaml.safe_load(open(sys.argv[1]))
spec = d.get("spec", {}) or {}
ri = spec.get("relatedImages", []) or []
# also include the operator's own containerImage if not already present
cont = (d.get("metadata", {}).get("annotations") or {}).get("containerImage", "")
images = []
seen = set()
for x in ri:
    im = (x.get("image") or "").strip()
    if im and im not in seen:
        seen.add(im); images.append(im)
if cont and cont not in seen:
    seen.add(cont); images.append(cont)
for im in images:
    base = im.split("/")[-1].split("@")[0].split(":")[0]
    name = re.sub(r"-rhel\d+$", "", base)
    print(f"{name}\t{im}")
PY

version=$(python3 -c "
import yaml,sys
d=yaml.safe_load(open('$csv'))
print((d.get('spec',{}) or {}).get('version',''))
")
printf '%s\t%s\t%s\t%s\n' "$PKG" "$head_bundle" "$version" "$csv" > "$OUT/meta.tsv"

n_img=$(wc -l < "$OUT/images.tsv")
log "wrote $OUT/images.tsv ($n_img operand images), version $version"
log "next: scripts/phase-b-operand-fetch-source.sh -p $PKG -v $MINOR"
