#!/bin/bash
# Phase B v2: improve source resolution rate by combining label data with the
# CSV's own metadata.annotations.repository / spec.links[].url, plus smarter
# tag/branch fallbacks. Re-fetches anything still missing under 20-git/ and
# refreshes git.tsv to record the new tarball mappings.
#
# Usage: phase-b-resolve-v2.sh -v 4.20 [-a x86_64] [--jobs N] [--limit N] [--dry-run]
#
# Inputs (produced by previous Phase B stages):
#   00-discover/operators.tsv
#   00-discover/containers.tsv
#   00-discover/labels.tsv
#   10-bundles/<op>/<bundle>/manifests/*.clusterserviceversion.yaml
# Outputs:
#   00-discover/containers-v2.tsv   op TAB bundle TAB containerImage TAB version TAB csv_repo
#   00-discover/git-v2.tsv          op TAB src_url TAB ref TAB tarball TAB version TAB strategy
#   20-git/<name>-<short>.tar.gz    new tarballs added in place
#   20-git/fetch-v2.log

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"
# shellcheck source=lib-resolve.sh
source "$SCRIPT_DIR/lib-resolve.sh"   # normalize_github, candidate_source_urls

JOBS=6; LIMIT=""; DRY=0
VERSION=""; ARCH="x86_64"
while (( $# )); do
    case "$1" in
        -v|--version) VERSION="$2"; shift 2 ;;
        -a|--arch)    ARCH="$2";    shift 2 ;;
        --jobs)       JOBS="$2";    shift 2 ;;
        --limit)      LIMIT="$2";   shift 2 ;;
        --dry-run)    DRY=1;        shift   ;;
        -h|--help)
            echo "phase-b-resolve-v2.sh -v <minor> [--jobs N] [--limit N] [--dry-run]"
            exit 0 ;;
        *) die "unknown arg: $1" ;;
    esac
done
[[ -n "$VERSION" ]] || die "version required (-v X.Y)"

require_cmd python3 curl jq awk
MINOR="$VERSION"
catalog_setup
WORK="${CASKET_WORK}/phase-b${CATALOG_SUFFIX}/${MINOR}"
DISC="$WORK/00-discover"
BUND="$WORK/10-bundles"
OUT="$WORK/20-git"
LOG="$OUT/fetch-v2.log"
mkdir -p "$OUT"

for f in operators.tsv containers.tsv labels.tsv; do
    [[ -s "$DISC/$f" ]] || die "missing $DISC/$f"
done

# Step 1: build containers-v2.tsv by enriching containers.tsv with CSV-derived
# repository URL.
log "extracting CSV repository annotations"
python3 - "$DISC" "$BUND" <<'PY'
import os, sys, yaml
disc, bund = sys.argv[1], sys.argv[2]

ops = {}
for line in open(f"{disc}/operators.tsv"):
    p = line.rstrip("\n").split("\t")
    if len(p) >= 3:
        ops[p[0]] = p[2]  # op -> head_bundle

def csv_for(op, bundle):
    d = f"{bund}/{op}/{bundle}/manifests"
    if not os.path.isdir(d): return None
    for f in os.listdir(d):
        if f.lower().endswith(".yaml") and "clusterserviceversion" in f.lower():
            return f"{d}/{f}"
    return None

def normalize_github(url):
    url = (url or "").strip().rstrip("/")
    if not url: return ""
    if url.startswith("git@github.com:"):
        url = "https://github.com/" + url[len("git@github.com:"):]
    if url.startswith("github.com/"):
        url = "https://" + url
    if "github.com" not in url:
        return ""
    if url.endswith(".git"):
        url = url[:-4]
    # strip /tree/... or /blob/... suffix
    for marker in ("/tree/", "/blob/"):
        i = url.find(marker)
        if i != -1:
            url = url[:i]
    return url

def extract_repo(csv_path):
    try:
        d = yaml.safe_load(open(csv_path))
    except Exception:
        return ""
    ann = d.get("metadata", {}).get("annotations") or {}
    cand = ann.get("repository", "")
    n = normalize_github(cand)
    if n: return n
    for link in (d.get("spec", {}).get("links") or []):
        url = normalize_github(link.get("url", ""))
        if url:
            return url
    return ""

out = []
for line in open(f"{disc}/containers.tsv"):
    p = line.rstrip("\n").split("\t")
    while len(p) < 4: p.append("")
    op, bundle, image, ver = p[0], p[1], p[2], p[3]
    csv = csv_for(op, bundle) if bundle else None
    repo = extract_repo(csv) if csv else ""
    out.append((op, bundle, image, ver, repo))

with open(f"{disc}/containers-v2.tsv", "w") as f:
    for row in out:
        f.write("\t".join(row) + "\n")

n_with_repo = sum(1 for r in out if r[4])
print(f"containers-v2.tsv: {len(out)} rows, {n_with_repo} with csv_repo")
PY

# Step 2: resolve (src_url, ref) for every operator using priority:
#   1. label org.opencontainers.image.source (github)   from labels.tsv
#   2. label io.openshift.build.source-location (github)  (already merged in labels.tsv)
#   3. label url (github)                                  (already merged in labels.tsv)
#   4. CSV csv_repo (NEW)
# vcs_ref priority kept as: label org.opencontainers.image.revision || vcs-ref ||
#                            io.openshift.build.commit.id
log "resolving src_url + vcs_ref for each operator"
python3 - "$DISC" <<'PY'
import sys, os
disc = sys.argv[1]
labels = {}
for line in open(f"{disc}/labels.tsv"):
    p = line.rstrip("\n").split("\t")
    while len(p) < 4: p.append("")
    img, src, ref, lver = p[0], p[1], p[2], p[3]
    labels[img] = (src, ref, lver)

out = []
seen_key = {}      # (src,ref) -> filename
for line in open(f"{disc}/containers-v2.tsv"):
    p = line.rstrip("\n").split("\t")
    while len(p) < 5: p.append("")
    op, bundle, image, ver, csv_repo = p
    src, ref, lver = labels.get(image, ("", "", ""))
    final_ver = lver or ver
    final_src = src if src else csv_repo
    if not final_src:
        out.append((op, final_src, ref, "NO_SOURCE", final_ver, "none", csv_repo, src))
        continue
    # ref may be empty — we'll still try branch/tag fallbacks. Use "head"
    # placeholder so the filename is stable.
    effective_ref = ref if ref else "head"
    key = (final_src, effective_ref)
    if key not in seen_key:
        short = effective_ref[:12]
        seen_key[key] = f"{op}-{short}.tar.gz"
    out.append((op, final_src, ref, seen_key[key], final_ver, "pending", csv_repo, src))

with open(f"{disc}/git-v2.tsv", "w") as f:
    for row in out:
        f.write("\t".join(row) + "\n")

n_total = len(out)
n_nosrc = sum(1 for r in out if r[3] == "NO_SOURCE")
n_unique = len({(r[1], r[2]) for r in out if r[3] != "NO_SOURCE"})
n_csv_rescued = sum(1 for r in out if r[3] != "NO_SOURCE" and not r[7] and r[6])
print(f"git-v2.tsv: total={n_total} no_source={n_nosrc} unique_targets={n_unique} csv_rescued={n_csv_rescued}")
PY

# Step 3: fetch all unique (src, ref) targets we don't already have on disk.
# Tries: archive/<sha>, archive/refs/tags/v<ver>, archive/refs/tags/<ver>,
#        archive/refs/heads/release-<minor>, main, master.
log "fetching missing tarballs"
DL_TSV="$OUT/.dl-v2.tsv"
# ref ($3) may be empty for csv_repo-only operators. Emit a sentinel so the
# empty field survives `read` below: IFS=$'\t' collapses consecutive tabs
# (tab is whitespace), which would otherwise shift columns and blank fname.
awk -F'\t' 'BEGIN{OFS="\t"} $4!="NO_SOURCE" {print $2, ($3==""?"_NONE_":$3), $5, $4}' "$DISC/git-v2.tsv" | sort -u > "$DL_TSV"
if [[ -n "$LIMIT" ]]; then
    head -n "$LIMIT" "$DL_TSV" > "$DL_TSV.tmp" && mv "$DL_TSV.tmp" "$DL_TSV"
fi
n_targets=$(wc -l < "$DL_TSV")
log "unique download targets: $n_targets"

: > "$LOG"

dl_one() {
    local src="$1" ref="$2" ver="$3" fname="$4" minor="$5"
    # undo the empty-ref sentinel (see DL_TSV construction above)
    [[ "$ref" == "_NONE_" ]] && ref=""
    local dst="$OUT/$fname"
    if [[ -s "$dst" ]]; then
        echo "SKIP $fname (exists)" >> "$LOG"
        return
    fi
    [[ "$src" == *github.com/* ]] || { echo "FAIL $fname (non-github: $src)" >> "$LOG"; return; }

    # Candidate URLs (exact-sha -> per-family strategy -> version tags -> branches)
    # come from the shared, unit-tested helper in lib-resolve.sh.
    local -a urls=()
    mapfile -t urls < <(candidate_source_urls "$src" "$ref" "$ver" "$minor")

    local u
    for u in "${urls[@]}"; do
        if curl -sSL --fail -o "$dst.tmp" "$u" 2>/dev/null; then
            mv "$dst.tmp" "$dst"
            local size; size=$(stat -c %s "$dst")
            local kind="sha"
            case "$u" in
                */archive/refs/tags/*)  kind="tag:${u##*/refs/tags/}" ;;
                */archive/refs/heads/*) kind="branch:${u##*/refs/heads/}" ;;
            esac
            echo "OK   $fname $kind ($size bytes)" >> "$LOG"
            return
        fi
        rm -f "$dst.tmp"
    done
    echo "FAIL $fname (src=$src ref=$ref ver=$ver tried ${#urls[@]} urls)" >> "$LOG"
}
export -f dl_one candidate_source_urls
export OUT LOG

if [[ "$DRY" -eq 1 ]]; then
    log "dry-run: would download $n_targets targets"
    head -20 "$DL_TSV" >&2
    exit 0
fi

if [[ "$n_targets" -gt 0 ]]; then
    < "$DL_TSV" xargs -P "$JOBS" -L 1 -d '\n' bash -c '
        IFS=$'"'"'\t'"'"' read -r src ref ver fname <<<"$0"
        dl_one "$src" "$ref" "$ver" "$fname" "'"$MINOR"'"
    '
fi
rm -f "$DL_TSV"

ok=$(grep -c "^OK"   "$LOG" 2>/dev/null || true)
skip=$(grep -c "^SKIP" "$LOG" 2>/dev/null || true)
fail=$(grep -c "^FAIL" "$LOG" 2>/dev/null || true)
log "v2 downloads: $ok ok, $skip skip, $fail fail"
log "summary in $LOG"
