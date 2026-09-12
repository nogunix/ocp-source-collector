#!/bin/bash
# Phase B / operand sources: combine all layered-product stages for one OCP minor into a single
# casket, one subdir per product. Reduces mount count (one mount per minor
# instead of one per product).
#
# Usage: phase-b-operand-combine.sh -v <minor> [-o /mnt/hdd/casket-ocp] [--date YYYYMMDD]
#
# Expects each product's stage at phase-b-operand/<pkg>/<minor>/50-out/stage (build it
# with phase-b-operand-package.sh --stage-only). Products with no stage are skipped.
#
# Produces: <out>/casket-<DATE>-layered-ocp<minor>.sqfs.xz
#   layout: <infix>/{bundle,git,meta}/...   (+ README.txt at root)
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"

# product list: package:infix, read from config/phase-b-operand-products.tsv (single
# source of truth for the rollout, shared with casket-check.sh/casket-build.sh)
PRODUCTS=()
while IFS=$'\t' read -r pkg infix; do
    [[ -z "$pkg" || "$pkg" == \#* ]] && continue
    PRODUCTS+=("${pkg}:${infix}")
done < "$SCRIPT_DIR/../config/phase-b-operand-products.tsv"

VERSION=""; OUT_DIR="${CASKET_OUT}"; DATE=""; ARTIFACT_OUT=""
while (( $# )); do
    case "$1" in
        -v|--version) VERSION="$2"; shift 2 ;;
        -o|--outdir)  OUT_DIR="$2"; shift 2 ;;
        --artifact-out) ARTIFACT_OUT="$2"; shift 2 ;;
        --date)       DATE="$2";    shift 2 ;;
        -h|--help)    echo "phase-b-operand-combine.sh -v <minor> [-o <dir>] [--date YYYYMMDD] [--artifact-out FILE]"; exit 0 ;;
        *) die "unknown arg: $1" ;;
    esac
done
[[ -n "$VERSION" ]] || die "version required (-v X.Y)"
[[ -n "$DATE" ]] || DATE=$(date -u +%Y%m%d)
require_cmd mksquashfs

MINOR="$VERSION"
COMB="${CASKET_WORK}/phase-b-operand/_layered/${MINOR}/stage"
OUT_PATH="${OUT_DIR%/}/casket-${DATE}-layered-ocp${MINOR}.sqfs.xz"

log "assembling combined stage for $MINOR"
rm -rf "${CASKET_WORK}/phase-b-operand/_layered/${MINOR}"
mkdir -p "$COMB"

included=()
for pair in "${PRODUCTS[@]}"; do
    pkg="${pair%%:*}"; infix="${pair##*:}"
    stage="${CASKET_WORK}/phase-b-operand/${pkg}/${MINOR}/50-out/stage"
    if [[ -d "$stage/git" ]]; then
        cp -al "$stage" "$COMB/$infix"
        ngit=$(find "$COMB/$infix/git" -maxdepth 1 -mindepth 1 -type d | wc -l)
        log "  + $infix ($ngit git dirs)"
        included+=("$infix")
    else
        log "  - $infix skipped (no stage at $stage)"
    fi
done
[[ ${#included[@]} -gt 0 ]] || die "no product stages found for $MINOR"

cat > "$COMB/README.txt" <<EOF
casket-ocp Phase B / operand sources — layered products, OCP v${MINOR}
Generated: $(date -u +%Y-%m-%dT%H:%M:%SZ)
Products included: ${included[*]}

One mount carries all layered-product operand sources, one subdir per product:
  cnv/    OpenShift Virtualization (kubevirt-hyperconverged)
  acs/    Advanced Cluster Security (rhacs-operator / stackrox)
  mce/    multicluster-engine
  acm/    Advanced Cluster Management (advanced-cluster-management)
  rhoai/  Red Hat OpenShift AI (rhods-operator)
  odf/    OpenShift Data Foundation (odf-operator umbrella: operator + console)
  quay/   Red Hat Quay (quay-operator; only the main quay app resolves upstream)
  osc/    OpenShift Sandboxed Containers (sandboxed-containers-operator: CAA +
          kata-containers + podvm recipes; guest-components/kata-agent sources
          pinned via the CAA versions.yaml — phase-b-operand-osc-extras.sh)
  trustee/ Trustee KBS server (trustee-operator; the operator itself is in Phase B)

Each <product>/ holds: bundle/ (head bundle CSV+CRDs), git/<name>-<ref>/ (extracted
upstream source), meta/ (images.tsv, labels.tsv, git.tsv, IMAGE_MAP.tsv,
MANIFEST.json, README.txt).
See repo docs/phase-b-operand-plan.md for resolution method and known structural limits.
EOF

chmod -R a+rX "$COMB"
log "mksquashfs → $OUT_PATH"
rm -f "$OUT_PATH"
mkdir -p "$OUT_DIR"
mksquashfs "$COMB" "$OUT_PATH" \
    -comp xz -Xbcj x86 \
    -no-progress -all-root -no-xattrs -noappend

record_artifact "$OUT_PATH" "$ARTIFACT_OUT"
log "done: ${included[*]}"
ls -lh "$OUT_PATH"
file "$OUT_PATH"
