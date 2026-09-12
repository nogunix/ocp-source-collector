#!/bin/bash
# Discover all images in an OCP release payload and their upstream git coordinates.
#
# Usage: discover.sh -v 4.20.0 [-a x86_64]
#
# Outputs (under $CASKET_WORK/ocp<ver>/00-discover/):
#   release.json         — oc adm release info --output=json
#   release-commits.txt  — oc adm release info --commits (raw)
#   images.tsv           — TAB: name<TAB>pullspec<TAB>digest
#   commits.tsv          — TAB: name<TAB>repo_url<TAB>commit_sha

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"

print_usage() {
    cat <<'EOF'
discover.sh -v <version> [-a <arch>]
  -v, --version   OCP version, e.g. 4.20.0
  -a, --arch      x86_64 (default) | aarch64 | ppc64le | s390x | multi
EOF
}

parse_args_version_arch "$@"
require_cmd oc jq

RELEASE_PULLSPEC="${RELEASE_REGISTRY}:${VERSION}-${ARCH}"
OUTDIR="$(version_dir "$VERSION")/00-discover"
mkdir -p "$OUTDIR"

log "release: $RELEASE_PULLSPEC"
log "outdir:  $OUTDIR"

# 1. Pull release manifest as JSON.
if [[ ! -s "$OUTDIR/release.json" ]]; then
    log "fetching release info (this can take ~1 min)"
    oc adm release info --registry-config="$AUTHFILE" \
        --output=json "$RELEASE_PULLSPEC" > "$OUTDIR/release.json.tmp"
    mv "$OUTDIR/release.json.tmp" "$OUTDIR/release.json"
else
    log "release.json exists, skipping fetch"
fi

# 2. Flatten image list.
jq -r '.references.spec.tags[] | [.name, .from.name] | @tsv' \
    "$OUTDIR/release.json" \
| awk -F'\t' 'BEGIN{OFS="\t"} {
    name=$1; ref=$2;
    digest=ref; sub(/^.*@/, "", digest);
    print name, ref, digest
}' > "$OUTDIR/images.tsv"

n_images=$(wc -l < "$OUTDIR/images.tsv")
log "discovered $n_images payload images"

# 3. Capture upstream git coordinates (name, repo, commit) via `oc adm release info --commits`.
if [[ ! -s "$OUTDIR/release-commits.txt" ]]; then
    log "fetching --commits info"
    oc adm release info --registry-config="$AUTHFILE" \
        --commits "$RELEASE_PULLSPEC" > "$OUTDIR/release-commits.txt.tmp"
    mv "$OUTDIR/release-commits.txt.tmp" "$OUTDIR/release-commits.txt"
else
    log "release-commits.txt exists, skipping"
fi

# Parse the "Images:" table. Format:
#   NAME                REPO                                       COMMIT
#   <name>              https://github.com/<owner>/<repo>          <40hex>
awk '
    /^Images:[[:space:]]*$/ { in_table=1; next }
    in_table && /^[[:space:]]+NAME[[:space:]]+REPO[[:space:]]+COMMIT/ { next }
    in_table && /^[[:space:]]*$/ { in_table=0; next }
    in_table {
        # split on runs of whitespace, output TSV
        n=split($0, a, /[[:space:]]+/);
        # a[1] is leading empty due to indent; first non-empty starts at a[2]
        name=a[2]; repo=a[3]; commit=a[4];
        if (name != "" && repo != "" && commit != "") {
            printf "%s\t%s\t%s\n", name, repo, commit
        }
    }
' "$OUTDIR/release-commits.txt" > "$OUTDIR/commits.tsv"

n_commits=$(wc -l < "$OUTDIR/commits.tsv")
n_unique=$(awk -F'\t' '{print $2"@"$3}' "$OUTDIR/commits.tsv" | sort -u | wc -l)
log "extracted $n_commits commit entries (unique repo+commit: $n_unique)"
log "wrote $OUTDIR/images.tsv and $OUTDIR/commits.tsv"
log "next: scripts/fetch-git.sh -v $VERSION [--limit N]"
