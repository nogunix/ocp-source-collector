#!/bin/bash
# Phase B / operand sources: resolve each operand image's upstream github source (via
# phase-b-operand-resolve-labels.py) and fetch the github archive tarball, deduped by
# (repo, ref-or-tag).
#
# Usage: phase-b-operand-fetch-source.sh -p <package> -v <minor> [-a x86_64] [--jobs N] [--limit N]
#
# Inputs (from phase-b-operand-discover.sh):
#   00-discover/images.tsv          component_name TAB image
# Outputs:
#   00-discover/labels.tsv          component TAB image TAB source_url TAB ref TAB version TAB method
#   00-discover/git.tsv             component TAB source_url TAB ref TAB tarball_name TAB version TAB method
#   20-git/<name>-<short>.tar.gz    github archives, deduped by (repo, ref|tag)
#   20-git/fetch.log
#   20-git/fetched.tsv              tarball | url actually fetched | kind | exact
#
# The fetch walks candidate_source_urls (scripts/lib-resolve.sh), the same
# fallback chain Phase B v2 uses, because the labelled commit is very often an
# internal konflux SHA that 404s on public github: 630 of 1355 operand images
# in 4.18 had no source at all as of 2026-07-29, and 194 of those rows HAD a
# resolved repo whose commit simply is not public. Anything past the exact
# commit is an approximation of the shipped source (a neighbouring tag, or a
# branch head), so it is logged as APPROX and recorded in fetched.tsv with
# exact=0 rather than being silently passed off as the real build.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"
# shellcheck source=lib-resolve.sh
source "$SCRIPT_DIR/lib-resolve.sh"   # candidate_source_urls

PKG=""; VERSION=""; ARCH="x86_64"; JOBS=6; LIMIT=""
while (( $# )); do
    case "$1" in
        -p|--package) PKG="$2";     shift 2 ;;
        -v|--version) VERSION="$2";  shift 2 ;;
        -a|--arch)    ARCH="$2";     shift 2 ;;
        --jobs)       JOBS="$2";     shift 2 ;;
        --limit)      LIMIT="$2";    shift 2 ;;
        -h|--help)    echo "phase-b-operand-fetch-source.sh -p <pkg> -v <minor> [--jobs N] [--limit N]"; exit 0 ;;
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

require_cmd oc python3 curl
MINOR="$VERSION"
WORK="${CASKET_WORK}/phase-b-operand/${PKG}/${MINOR}"
DISC="$WORK/00-discover"
OUT="$WORK/20-git"
LOG="$OUT/fetch.log"
RESOLVER="$SCRIPT_DIR/phase-b-operand-resolve-labels.py"
mkdir -p "$OUT"
[[ -s "$DISC/images.tsv" ]] || die "missing $DISC/images.tsv (run phase-b-operand-discover.sh first)"
[[ -f "$RESOLVER" ]] || die "missing resolver: $RESOLVER"

IMAGES_TSV="$DISC/images.tsv"
[[ -n "$LIMIT" ]] && { head -n "$LIMIT" "$IMAGES_TSV" > "$DISC/.images.limit"; IMAGES_TSV="$DISC/.images.limit"; }

# 1. Resolve labels for every (component,image) in parallel.
LABELS="$DISC/labels.tsv"
log "resolving source labels via oc image info (jobs=$JOBS)"
# The operator package's own CSV version (00-discover/meta.tsv column 3). Only
# used for COMPONENT_MAP "tag-csv" components, whose images carry no version
# label at all (apicurio / Service Registry).
CSV_VER="$(awk -F'\t' 'NR==1{print $3}' "$DISC/meta.tsv" 2>/dev/null || true)"

resolve_one() {
    local comp="$1" image="$2"
    local info res
    info=$(oc image info --registry-config="$AUTHFILE" --filter-by-os="$ARCH_FILTER" \
           --output=json "$image" 2>/dev/null) || info=""
    res=$(printf '%s' "$info" | python3 "$RESOLVER" "$comp" "$CSV_VER")
    # res: src \t ref \t version \t method
    printf '%s\t%s\t%s\n' "$comp" "$image" "$res"
}
export -f resolve_one
export AUTHFILE ARCH_FILTER RESOLVER CSV_VER

< "$IMAGES_TSV" xargs -P "$JOBS" -L 1 -d '\n' bash -c '
    IFS=$'"'"'\t'"'"' read -r comp image <<<"$0"
    resolve_one "$comp" "$image"
' > "$LABELS.tmp"
mv "$LABELS.tmp" "$LABELS"
log "labels.tsv rows: $(wc -l < "$LABELS")"

# 1b. Sibling-version fixup: hostpath-provisioner and hostpath-csi-driver are
# both built from kubevirt/hostpath-provisioner, but their image labels carry
# no component version (only the CNV product version) and no upstream-* labels,
# so the resolver leaves them z:none. The deployed version rides with the
# hostpath-provisioner-operator (e.g. 0.23.0 -> branch release-v0.23), whose
# labels DO resolve -- borrow it. The branch head is the best public
# approximation (the exact build commit lives only on the internal konflux
# repo). See JANUS case 2026-07-11-cnv-downstream-gap H1.
awk -F'\t' 'BEGIN{OFS="\t"}
NR==FNR { if ($1=="hostpath-provisioner-operator" && $5!="") opver=$5; next }
{
    if (opver != "" && ($1=="hostpath-provisioner" || $1=="hostpath-csi-driver") && $6=="z:none") {
        nn=split(opver, a, ".");
        $3="https://github.com/kubevirt/hostpath-provisioner";
        $4="release-v" a[1] "." a[2];
        $5=opver; $6="b:sibling-map";
    }
    print
}' "$LABELS" "$LABELS" > "$LABELS.fix" && mv "$LABELS.fix" "$LABELS"

# 2. Build (repo, ref|tag) -> unique tarball name; emit git.tsv.
#    awk (not bash `read`) so empty ref/version fields don't get collapsed by
#    IFS-whitespace handling. git.tsv columns:
#      comp \t src \t ref \t tarball \t version \t method
#    NO_SOURCE rows keep tarball="NO_SOURCE".
GIT_TSV="$DISC/git.tsv"
awk -F'\t' 'BEGIN{OFS="\t"}
{
    comp=$1; src=$3; ref=$4; ver=$5; method=$6;
    if (src=="") { print comp,"","","NO_SOURCE",ver,method; next }
    if (ref!="") { key=src"|"ref; short=substr(ref,1,12) }
    else         { key=src"|v"ver; short="v"ver }
    if (!(key in seen)) seen[key]=comp"-"short".tar.gz";
    print comp,src,ref,seen[key],ver,method;
}' "$LABELS" > "$GIT_TSV"

n_total=$(wc -l < "$GIT_TSV")
n_nosrc=$(awk -F'\t' '$4=="NO_SOURCE"' "$GIT_TSV" | wc -l)
n_uniq=$(awk -F'\t' '$4!="NO_SOURCE"{print $4}' "$GIT_TSV" | sort -u | wc -l)
log "git.tsv: $n_total rows, $n_nosrc no-source, $n_uniq unique tarballs"

# 3. Fetch unique tarballs, walking the candidate chain (exact commit first,
#    then per-repo family strategies, version tags, branches).
#    .dl.tsv uses "-" as the empty-field sentinel so the xargs `read` (which
#    collapses empty tab-separated fields) keeps columns aligned.
: > "$LOG"
FETCHED="$OUT/fetched.tsv"
printf '# tarball\turl\tkind\texact\n' > "$FETCHED"
DL="$OUT/.dl.tsv"
awk -F'\t' 'BEGIN{OFS="\t"} $4!="NO_SOURCE"{
    r=($3==""?"-":$3); v=($5==""?"-":$5); print $2,r,v,$4
}' "$GIT_TSV" | sort -u > "$DL"

# Classify a winning candidate URL: the exact-commit archive is the only one
# that is the source the image was actually built from.
url_kind() {
    case "$1" in
        */archive/refs/tags/*)  printf 'tag' ;;
        */archive/refs/heads/*) printf 'branch' ;;
        */archive/HEAD.tar.gz)  printf 'branch' ;;   # default branch, not a sha
        *)                      printf 'sha' ;;
    esac
}

# A tarball left by an earlier run is not re-fetched, so it would contribute no
# fetched.tsv row and get counted as a fetch failure. Its provenance is
# recoverable offline: a GitHub archive holds exactly one top-level directory,
# named "<repo>-<the ref that was asked for>", so a full sha there means the
# exact commit was fetched and anything else means a tag/branch was. Read via
# python tarfile (one header, one process) rather than `tar -tzf | head`, which
# both decompresses the whole archive and trips the pipefail/SIGPIPE trap.
record_existing() {
    local dst="$1" ref="$2" fname="$3" top
    top=$(python3 -c 'import sys,tarfile
t=tarfile.open(sys.argv[1]); m=t.next()
print(m.name.split("/")[0] if m else "")' "$dst" 2>/dev/null) || top=""
    if [[ -n "$ref" && -n "$top" && "$top" == *"$ref" ]]; then
        printf '%s\tpre-existing:%s\t%s\t%s\n' "$fname" "$top" sha 1 >> "$FETCHED"
    else
        printf '%s\tpre-existing:%s\t%s\t%s\n' "$fname" "${top:-unknown}" preexisting 0 >> "$FETCHED"
    fi
}

dl_one() {
    local src="$1" ref="$2" ver="$3" fname="$4"
    [[ "$ref" == "-" ]] && ref=""
    [[ "$ver" == "-" ]] && ver=""
    local dst="$OUT/$fname"
    if [[ -s "$dst" ]]; then
        record_existing "$dst" "$ref" "$fname"
        echo "SKIP $fname (exists)" >> "$LOG"
        return
    fi
    local url kind exact n=0 last=""
    # Not a subshell (process substitution), so `last` survives the loop.
    while IFS= read -r url; do
        [[ -n "$url" ]] || continue
        n=$((n + 1)); last="$url"
        curl -sSL --fail -o "$dst.tmp" "$url" 2>/dev/null || { rm -f "$dst.tmp"; continue; }
        mv "$dst.tmp" "$dst"
        kind="$(url_kind "$url")"
        [[ "$kind" == sha ]] && exact=1 || exact=0
        printf '%s\t%s\t%s\t%s\n' "$fname" "$url" "$kind" "$exact" >> "$FETCHED"
        if [[ "$exact" == 1 ]]; then
            echo "OK     $fname sha ($(stat -c %s "$dst") b) $url" >> "$LOG"
        else
            echo "APPROX $fname $kind ($(stat -c %s "$dst") b) $url" >> "$LOG"
        fi
        return
    done < <(candidate_source_urls "$src" "$ref" "$ver" "$MINOR")
    echo "FAIL $fname ($n candidates tried, last=$last)" >> "$LOG"
}
# candidate_source_urls comes from lib-resolve.sh and must be exported too --
# dl_one runs in a fresh `bash -c` under xargs, which inherits only exported
# functions, not the sourcing shell's definitions.
export -f dl_one url_kind record_existing candidate_source_urls
export OUT LOG FETCHED MINOR

if [[ -s "$DL" ]]; then
    < "$DL" xargs -P "$JOBS" -L 1 -d '\n' bash -c '
        IFS=$'"'"'\t'"'"' read -r src ref ver fname <<<"$0"
        dl_one "$src" "$ref" "$ver" "$fname"
    '
fi
rm -f "$DL" "$DISC/.images.limit" 2>/dev/null || true

ok=$(grep -c "^OK"     "$LOG" || true)
approx=$(grep -c "^APPROX" "$LOG" || true)
skip=$(grep -c "^SKIP"   "$LOG" || true)
fail=$(grep -c "^FAIL"   "$LOG" || true)
log "downloads: $ok exact, $approx approximate (tag/branch, see fetched.tsv), $skip skip, $fail fail"
log "next: scripts/phase-b-operand-package.sh -p $PKG -v $MINOR"
