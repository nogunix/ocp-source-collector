#!/bin/bash
# Phase B: pull each default-channel-head bundle image, extract /manifests and
# /metadata into bundles/<operator>/<head_bundle>/, and record each bundle's
# operator-container annotation (containerImage) into containers.tsv.
#
# Usage: phase-b-fetch-bundles.sh -v 4.20 [-a x86_64] [--jobs N] [--limit N]
#
# Inputs (from phase-b-discover.sh):
#   00-discover/operators.tsv
# Outputs:
#   10-bundles/<operator>/<head_bundle>/manifests/*.yaml
#   10-bundles/<operator>/<head_bundle>/metadata/*.yaml
#   00-discover/containers.tsv : operator TAB head_bundle TAB containerImage TAB version
#   10-bundles/fetch.log

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"

print_usage() {
    cat <<'EOF'
phase-b-fetch-bundles.sh -v <minor> [-a x86_64] [--jobs N] [--limit N]
EOF
}

JOBS=6; LIMIT=""
VERSION=""; ARCH="x86_64"
while (( $# )); do
    case "$1" in
        -v|--version) VERSION="$2"; shift 2 ;;
        -a|--arch)    ARCH="$2";    shift 2 ;;
        --jobs)       JOBS="$2";    shift 2 ;;
        --limit)      LIMIT="$2";   shift 2 ;;
        -h|--help)    print_usage; exit 0 ;;
        *) die "unknown arg: $1" ;;
    esac
done
[[ -n "$VERSION" ]] || die "version required (-v X.Y)"
case "$ARCH" in
    x86_64)  ARCH_FILTER="linux/amd64"   ;;
    aarch64) ARCH_FILTER="linux/arm64"   ;;
    ppc64le) ARCH_FILTER="linux/ppc64le" ;;
    s390x)   ARCH_FILTER="linux/s390x"   ;;
    *)       die "unsupported arch: $ARCH" ;;
esac

require_cmd oc python3
MINOR="$VERSION"
catalog_setup
WORK="${CASKET_WORK}/phase-b${CATALOG_SUFFIX}/${MINOR}"
DISC="$WORK/00-discover"
OUT="$WORK/10-bundles"
LOG="$OUT/fetch.log"
mkdir -p "$OUT"

[[ -s "$DISC/operators.tsv" ]] || die "missing $DISC/operators.tsv (run phase-b-discover.sh first)"

# Build work list: operator TAB head_bundle TAB head_image, skipping those
# whose manifests directory already exists.
WORK_TSV="$OUT/.work.tsv"
awk -F'\t' -v out="$OUT" '
    {
        op=$1; bundle=$3; image=$4;
        dir = out "/" op "/" bundle "/manifests";
        # bash will check existence; print all and filter
        print op "\t" bundle "\t" image
    }' "$DISC/operators.tsv" > "$WORK_TSV.all"

> "$WORK_TSV"
while IFS=$'\t' read -r OP BUNDLE IMAGE; do
    [[ -d "$OUT/$OP/$BUNDLE/manifests" ]] && continue
    printf '%s\t%s\t%s\n' "$OP" "$BUNDLE" "$IMAGE" >> "$WORK_TSV"
done < "$WORK_TSV.all"
rm -f "$WORK_TSV.all"

if [[ -n "$LIMIT" ]]; then
    head -n "$LIMIT" "$WORK_TSV" > "$WORK_TSV.tmp" && mv "$WORK_TSV.tmp" "$WORK_TSV"
fi
n_work=$(wc -l < "$WORK_TSV")
log "bundles to fetch: $n_work (parallel jobs=$JOBS)"

: > "$LOG"

fetch_one() {
    local op="$1" bundle="$2" image="$3"
    local dst="$OUT/$op/$bundle"
    # `oc image extract --path A:B` requires B to exist already and be empty.
    # Pre-create both target dirs. Also: oc returns 0 even when paths don't
    # exist, so check stderr content as the fail signal.
    mkdir -p "$dst/manifests" "$dst/metadata"
    local errf
    errf=$(mktemp)
    oc image extract --registry-config="$AUTHFILE" --filter-by-os="$ARCH_FILTER" \
        --path "/manifests/:$dst/manifests/" \
        --path "/metadata/:$dst/metadata/" \
        "$image" 2>"$errf" >/dev/null
    if grep -qiE "error|denied|not found" "$errf"; then
        cat "$errf" >> "$LOG"
        echo "FAIL $op $bundle ($image)" >> "$LOG"
        rm -f "$errf"
        return 1
    fi
    rm -f "$errf"
    if [[ -z "$(ls -A "$dst/manifests" 2>/dev/null)" ]]; then
        echo "FAIL $op $bundle (manifests/ empty after extract)" >> "$LOG"
        return 1
    fi
    echo "OK   $op $bundle" >> "$LOG"
}
export -f fetch_one
export AUTHFILE ARCH_FILTER OUT LOG

if [[ "$n_work" -gt 0 ]]; then
    # Don't let a single transient bundle-pull failure (xargs returns 123 when
    # any child exits non-zero) abort the whole minor under `set -e`. Individual
    # failures are logged; we gate on the aggregate count below instead.
    < "$WORK_TSV" xargs -P "$JOBS" -L 1 -d '\n' bash -c '
        IFS=$'"'"'\t'"'"' read -r op bundle image <<<"$0"
        fetch_one "$op" "$bundle" "$image"
    ' || true
fi

# Gate: tolerate a few transient failures, but a wholesale failure (e.g. broken
# auth / registry outage) should stop the pipeline rather than ship an empty
# casket.
pulled_ok=$(grep -c "^OK"   "$LOG" 2>/dev/null || true)
pulled_fail=$(grep -c "^FAIL" "$LOG" 2>/dev/null || true)
if [[ "$pulled_ok" -eq 0 && "$n_work" -gt 0 ]]; then
    die "all $n_work bundle pulls failed — check registry auth/connectivity ($LOG)"
fi
if [[ "$pulled_fail" -gt 0 ]]; then
    log "WARN: $pulled_fail/$n_work bundle pulls failed (continuing; see $LOG)"
fi

# Update containers.tsv: walk every (operator, head_bundle) dir and read CSV
log "extracting containerImage annotation from each CSV"
: > "$DISC/containers.tsv"
while IFS=$'\t' read -r OP _CHAN BUNDLE _; do
    dst="$OUT/$OP/$BUNDLE"
    csv=$(find "$dst/manifests" -maxdepth 1 -name "*.clusterserviceversion.yaml" 2>/dev/null | head -1)
    if [[ -z "$csv" ]]; then
        csv=$(find "$dst/manifests" -maxdepth 1 -type f \( -name "*.yaml" -o -name "*.yml" \) 2>/dev/null \
              | xargs -r grep -l "ClusterServiceVersion" 2>/dev/null | head -1)
    fi
    [[ -n "$csv" && -f "$csv" ]] || continue

    container=$(python3 -c "
import yaml, sys
try:
    d = yaml.safe_load(open('$csv'))
except Exception as e:
    sys.exit(0)
ann = (d.get('metadata',{}).get('annotations') or {})
print(ann.get('containerImage',''))
" 2>/dev/null)
    version=$(python3 -c "
import yaml, sys
try:
    d = yaml.safe_load(open('$csv'))
except Exception:
    sys.exit(0)
print((d.get('spec',{}) or {}).get('version',''))
" 2>/dev/null)
    # Keep the row even when the CSV carries no containerImage annotation:
    # phase-b-resolve-v2.sh can still recover the source from the CSV's
    # metadata.annotations.repository (csv_repo). Dropping empty-container rows
    # here silently excluded operators like serverless/pipelines/compliance.
    printf '%s\t%s\t%s\t%s\n' "$OP" "$BUNDLE" "$container" "$version" >> "$DISC/containers.tsv"
done < "$DISC/operators.tsv"

ok=$(grep -c "^OK"   "$LOG" || true)
fail=$(grep -c "^FAIL" "$LOG" || true)
n_containers=$(wc -l < "$DISC/containers.tsv")
log "fetched: $ok ok, $fail failed; containers.tsv rows: $n_containers"
log "next: scripts/phase-b-fetch-source.sh -v $MINOR"
