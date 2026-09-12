#!/bin/bash
# Fetch upstream git source tarballs for an OCP release.
#
# Reads commits.tsv (produced by discover.sh) and downloads each unique
# (repo, commit) pair as a tarball from GitHub's archive endpoint.
#
# Usage: fetch-git.sh -v 4.20.22 [--limit N] [--jobs N]
#
# Outputs (under $CASKET_WORK/ocp<ver>/10-git/):
#   <basename>-<short_sha>.tar.gz   — one per unique repo+commit
#   fetch.log                       — per-target status (OK/FAIL/SKIP)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"

print_usage() {
    cat <<'EOF'
fetch-git.sh -v <version> [--limit N] [--jobs N]
  -v, --version   OCP version (must match a prior discover.sh run)
  --limit         download at most N targets (for smoke testing)
  --jobs          parallel download workers (default 6)
EOF
}

VERSION=""
LIMIT=0
JOBS=6
while [[ $# -gt 0 ]]; do
    case "$1" in
        -v|--version) VERSION="$2"; shift 2 ;;
        --limit)      LIMIT="$2";   shift 2 ;;
        --jobs)       JOBS="$2";    shift 2 ;;
        -h|--help)    print_usage; exit 0 ;;
        *) die "unknown arg: $1" ;;
    esac
done
[[ -n "$VERSION" ]] || die "version required (-v X.Y.Z)"
require_cmd curl awk sort

INDIR="$(version_dir "$VERSION")/00-discover"
OUTDIR="$(version_dir "$VERSION")/10-git"
[[ -s "$INDIR/commits.tsv" ]] || die "missing $INDIR/commits.tsv — run discover.sh first"
mkdir -p "$OUTDIR"

# Build unique (repo, commit) work list. Preserve a representative component name
# for filenames by taking the first occurrence per (repo, commit).
WORK="$OUTDIR/.work.tsv"
awk -F'\t' '
    { key = $2 "\t" $3 }
    !seen[key]++ { print $1 "\t" $2 "\t" $3 }
' "$INDIR/commits.tsv" > "$WORK"

if [[ "$LIMIT" -gt 0 ]]; then
    head -n "$LIMIT" "$WORK" > "$WORK.lim" && mv "$WORK.lim" "$WORK"
fi

total=$(wc -l < "$WORK")
log "targets: $total unique repo+commit pairs (jobs=$JOBS)"

fetch_one() {
    local name="$1" repo="$2" commit="$3" outdir="$4"
    local short="${commit:0:12}"
    # Derive owner/repo from URL like https://github.com/<owner>/<repo>(.git)?
    local path="${repo#https://github.com/}"
    path="${path%.git}"
    local fname="${name}-${short}.tar.gz"
    local out="${outdir}/${fname}"
    if [[ -s "$out" ]]; then
        printf 'SKIP\t%s\t%s\t%s\texists\n' "$name" "$repo" "$commit"
        return 0
    fi
    local url="https://github.com/${path}/archive/${commit}.tar.gz"
    local tmp="${out}.partial"
    if curl -fsSL --retry 3 --retry-delay 2 --connect-timeout 15 \
              -o "$tmp" "$url" 2>/dev/null; then
        mv "$tmp" "$out"
        local sz; sz=$(stat -c%s "$out")
        printf 'OK\t%s\t%s\t%s\t%s\n' "$name" "$repo" "$commit" "$sz"
    else
        rm -f "$tmp"
        printf 'FAIL\t%s\t%s\t%s\t-\n' "$name" "$repo" "$commit"
    fi
}
export -f fetch_one

LOG="$OUTDIR/fetch.log"
: > "$LOG"

# Drive xargs with NUL-separated fields (3 per record) to avoid quoting hazards.
awk -F'\t' '{printf "%s\0%s\0%s\0", $1, $2, $3}' "$WORK" \
| xargs -0 -P "$JOBS" -n 3 \
    bash -c 'fetch_one "$1" "$2" "$3" "'"$OUTDIR"'"' _ >> "$LOG"

ok=$(grep -c '^OK'   "$LOG" || true)
sk=$(grep -c '^SKIP' "$LOG" || true)
fl=$(grep -c '^FAIL' "$LOG" || true)
log "done: OK=$ok SKIP=$sk FAIL=$fl"
total_bytes=$(awk -F'\t' '$1=="OK"{s+=$5} END{print s+0}' "$LOG")
log "downloaded size: $(numfmt --to=iec --suffix=B "$total_bytes")"
rm -f "$WORK"

if [[ "$fl" -gt 0 ]]; then
    log "failures listed in $LOG (filter with: awk -F'\\t' '\$1==\"FAIL\"' $LOG)"
fi
