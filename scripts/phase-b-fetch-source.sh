#!/bin/bash
# Phase B: for each operator container, read the OCI/Red Hat build labels
# and fetch the github tarball, deduped by (repo, full_sha).
#
# Label preference (per image):
#   source_url: org.opencontainers.image.source
#               || io.openshift.build.source-location
#               || url        (only if it points at github)
#   vcs_ref:    org.opencontainers.image.revision
#               || vcs-ref
#               || io.openshift.build.commit.id
#
# Why prefer OCI labels first: for Ansible-based operators (e.g. mtv),
# io.openshift.build.source-location points at the ansible-operator-plugins
# *base image* repo while the operator's vcs-ref is a commit in the operator's
# *own* code repo (recorded in org.opencontainers.image.source). The legacy
# preference order paired the base-image URL with the operator commit and 404'd.
#
# On a 404 against <src>/archive/<sha>.tar.gz we fall back to
# <src>/archive/refs/tags/v<version>.tar.gz so internal Red Hat commits that
# never make it to github still resolve to a nearby tag.
#
# Usage: phase-b-fetch-source.sh -v 4.20 [-a x86_64] [--jobs N] [--limit N]
#
# Inputs:
#   00-discover/containers.tsv      operator TAB head_bundle TAB containerImage TAB version
# Outputs:
#   00-discover/labels.tsv          containerImage TAB source_url TAB vcs_ref TAB version
#   00-discover/git.tsv             operator TAB source_url TAB vcs_ref TAB tarball_name (or "NO_SOURCE")
#   20-git/<name>-<short_sha>.tar.gz  github archives, deduped by (repo, sha)
#   20-git/fetch.log

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"

JOBS=6; LIMIT=""
VERSION=""; ARCH="x86_64"
while (( $# )); do
    case "$1" in
        -v|--version) VERSION="$2"; shift 2 ;;
        -a|--arch)    ARCH="$2";    shift 2 ;;
        --jobs)       JOBS="$2";    shift 2 ;;
        --limit)      LIMIT="$2";   shift 2 ;;
        -h|--help)    echo "phase-b-fetch-source.sh -v <minor> [--jobs N] [--limit N]"; exit 0 ;;
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

require_cmd oc jq curl
MINOR="$VERSION"
catalog_setup
WORK="${CASKET_WORK}/phase-b${CATALOG_SUFFIX}/${MINOR}"
DISC="$WORK/00-discover"
OUT="$WORK/20-git"
LOG="$OUT/fetch.log"
mkdir -p "$OUT"

[[ -s "$DISC/containers.tsv" ]] || die "missing $DISC/containers.tsv (run phase-b-fetch-bundles.sh first)"

# 1. Resolve labels for every unique containerImage.
LABELS="$DISC/labels.tsv"
log "querying labels for each containerImage (oc image info)"
: > "$LABELS.tmp"

UNIQUE_IMAGES=$(awk -F'\t' '{print $3}' "$DISC/containers.tsv" | sort -u)
[[ -n "$LIMIT" ]] && UNIQUE_IMAGES=$(echo "$UNIQUE_IMAGES" | head -n "$LIMIT")

# Run image-info lookups in parallel
resolve_one() {
    local image="$1"
    local info
    # timeout: community-catalog images live on arbitrary third-party
    # registries (ghcr.io, personal quay repos, ...) where a dead endpoint
    # can hang oc for many minutes and stall the whole minor.
    info=$(timeout 60 oc image info --registry-config="$AUTHFILE" --filter-by-os="$ARCH_FILTER" \
           --output=json "$image" 2>/dev/null) || { printf '%s\t\t\t\n' "$image"; return; }
    # Labels live at .config.config.Labels
    local src ref ver
    src=$(echo "$info" | jq -r '
        .config.config.Labels["org.opencontainers.image.source"]
        // .config.config.Labels["io.openshift.build.source-location"]
        // .config.config.Labels["url"]
        // ""')
    # Only github URLs are downloadable as archive tarballs.
    [[ "$src" == *github.com/* ]] || src=""
    ref=$(echo "$info" | jq -r '
        .config.config.Labels["org.opencontainers.image.revision"]
        // .config.config.Labels["vcs-ref"]
        // .config.config.Labels["io.openshift.build.commit.id"]
        // ""')
    ver=$(echo "$info" | jq -r '.config.config.Labels.version // ""')
    printf '%s\t%s\t%s\t%s\n' "$image" "$src" "$ref" "$ver"
}
export -f resolve_one
export AUTHFILE ARCH_FILTER

echo "$UNIQUE_IMAGES" | xargs -P "$JOBS" -I {} bash -c 'resolve_one "$@"' _ {} > "$LABELS.tmp"
mv "$LABELS.tmp" "$LABELS"
n_lbl=$(wc -l < "$LABELS")
log "labels.tsv rows: $n_lbl"

# 2. Build (operator, source_url, vcs_ref) → unique tarball filename mapping.
#    Filename uses operator-name + short_sha. Same (repo, sha) shared across
#    operators -> one tarball (first operator's name wins for filename).
GIT_TSV="$DISC/git.tsv"
: > "$GIT_TSV"
declare -A TARBALL_FOR_KEY      # "repo|sha" -> filename
declare -A TARBALL_BY_NAME      # filename -> "repo|sha" (sanity)

while IFS=$'\t' read -r OP BUNDLE IMAGE VER; do
    line=$(awk -F'\t' -v i="$IMAGE" '$1==i {print; exit}' "$LABELS")
    src=$(echo "$line" | awk -F'\t' '{print $2}')
    ref=$(echo "$line" | awk -F'\t' '{print $3}')
    lver=$(echo "$line" | awk -F'\t' '{print $4}')
    [[ -z "$lver" ]] && lver="$VER"
    if [[ -z "$src" || -z "$ref" ]]; then
        printf '%s\t%s\t%s\t%s\t%s\n' "$OP" "" "" "NO_SOURCE" "$lver" >> "$GIT_TSV"
        continue
    fi
    key="$src|$ref"
    if [[ -z "${TARBALL_FOR_KEY[$key]+x}" ]]; then
        short=${ref:0:12}
        fname="${OP}-${short}.tar.gz"
        TARBALL_FOR_KEY[$key]="$fname"
        TARBALL_BY_NAME[$fname]="$key"
    fi
    printf '%s\t%s\t%s\t%s\t%s\n' "$OP" "$src" "$ref" "${TARBALL_FOR_KEY[$key]}" "$lver" >> "$GIT_TSV"
done < "$DISC/containers.tsv"

n_total=$(wc -l < "$GIT_TSV")
n_nosrc=$(awk -F'\t' '$4=="NO_SOURCE"' "$GIT_TSV" | wc -l)
n_unique=$(awk -F'\t' '$4!="NO_SOURCE" {print $2"|"$3}' "$GIT_TSV" | sort -u | wc -l)
log "git.tsv rows: $n_total  (no-source: $n_nosrc, unique tarballs to fetch: $n_unique)"

# 3. Fetch unique tarballs in parallel from github (no auth needed).
: > "$LOG"
DL_TSV="$OUT/.dl.tsv"
# Columns from git.tsv: 1=op 2=src 3=ref 4=tarball 5=version
# DL_TSV expected by dl_one: src \t ref \t version \t tarball
awk -F'\t' 'BEGIN{OFS="\t"} $4!="NO_SOURCE" {print $2, $3, $5, $4}' "$GIT_TSV" | sort -u > "$DL_TSV"

dl_one() {
    local src="$1" ref="$2" ver="$3" fname="$4"
    local dst="$OUT/$fname"
    [[ -s "$dst" ]] && { echo "SKIP $fname (exists)" >> "$LOG"; return; }
    [[ "$src" == *github.com/* ]] || { echo "SKIP $fname (non-github source: $src)" >> "$LOG"; return; }

    local url_sha="${src%/}/archive/${ref}.tar.gz"
    local url_tag=""
    [[ -n "$ver" ]] && url_tag="${src%/}/archive/refs/tags/v${ver}.tar.gz"

    if curl -sSL --fail -o "$dst.tmp" "$url_sha" 2>/dev/null; then
        mv "$dst.tmp" "$dst"
        echo "OK   $fname sha ($(stat -c %s "$dst") bytes)" >> "$LOG"
    elif [[ -n "$url_tag" ]] && curl -sSL --fail -o "$dst.tmp" "$url_tag" 2>/dev/null; then
        mv "$dst.tmp" "$dst"
        echo "OK   $fname tag v${ver} ($(stat -c %s "$dst") bytes)" >> "$LOG"
    else
        rm -f "$dst.tmp"
        if [[ -n "$url_tag" ]]; then
            echo "FAIL $fname (tried $url_sha and $url_tag)" >> "$LOG"
        else
            echo "FAIL $fname ($url_sha)" >> "$LOG"
        fi
    fi
}
export -f dl_one
export OUT LOG

if [[ -s "$DL_TSV" ]]; then
    < "$DL_TSV" xargs -P "$JOBS" -L 1 -d '\n' bash -c '
        IFS=$'"'"'\t'"'"' read -r src ref ver fname <<<"$0"
        dl_one "$src" "$ref" "$ver" "$fname"
    '
fi
rm -f "$DL_TSV"

ok=$(grep -c "^OK"   "$LOG" || true)
skip=$(grep -c "^SKIP" "$LOG" || true)
fail=$(grep -c "^FAIL" "$LOG" || true)
log "downloads: $ok ok, $skip skip, $fail fail"
log "next: scripts/phase-b-package.sh -v $MINOR"
