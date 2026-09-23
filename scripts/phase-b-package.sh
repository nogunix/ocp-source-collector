#!/bin/bash
# Phase B: bundle catalog/bundles/git into one casket .sqfs.xz per OCP minor.
#
# Usage: phase-b-package.sh -v 4.20 [-o /mnt/hdd/casket-ocp]
#
# Produces:
#   <out>/casket-<YYYYMMDD>-ocp<minor>-operators.sqfs.xz
# with internal layout:
#   catalog/<operator>/catalog.json    (FBC, verbatim)
#   bundles/<operator>/<head>/{manifests,metadata}/
#   git/<name>-<short_sha>.tar.gz      (deduped)
#   meta/{MANIFEST.json,README.txt,operators.tsv,containers.tsv,git.tsv,git-fetched.tsv,labels.tsv,fetch*.log}

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"

VERSION=""; OUT_DIR="${CASKET_OUT}"
ARTIFACT_OUT=""
while (( $# )); do
    case "$1" in
        -v|--version) VERSION="$2"; shift 2 ;;
        -o|--outdir)  OUT_DIR="$2"; shift 2 ;;
        --artifact-out) ARTIFACT_OUT="$2"; shift 2 ;;
        -h|--help)    echo "phase-b-package.sh -v <minor> [-o <dir>]"; exit 0 ;;
        *) die "unknown arg: $1" ;;
    esac
done
[[ -n "$VERSION" ]] || die "version required (-v X.Y)"

require_cmd jq sha256sum
MINOR="$VERSION"
catalog_setup
WORK="${CASKET_WORK}/phase-b${CATALOG_SUFFIX}/${MINOR}"
DISC="$WORK/00-discover"
BUNDLES="$WORK/10-bundles"
GIT="$WORK/20-git"
STAGE="$WORK/50-out/stage"

[[ -d "$DISC/configs" ]] || die "missing $DISC/configs (run phase-b-discover.sh)"
[[ -d "$BUNDLES"      ]] || die "missing $BUNDLES (run phase-b-fetch-bundles.sh)"
[[ -d "$GIT"          ]] || die "missing $GIT (run phase-b-fetch-source.sh)"

DATE=$(date -u +%Y%m%d)
OUT_NAME="casket-${DATE}-ocp${MINOR}${CATALOG_SUFFIX}-operators.$(casket_ext)"
OUT_PATH="${OUT_DIR%/}/${OUT_NAME}"

log "staging into $STAGE"
rm -rf "$STAGE"
mkdir -p "$STAGE/meta"

# 1. catalog/ — copy the FBC verbatim
log "staging catalog/ (FBC)"
mkdir -p "$STAGE/catalog"
cp -al "$DISC/configs/." "$STAGE/catalog/"

# 2. bundles/ — copy manifests + metadata for each head bundle
log "staging bundles/"
mkdir -p "$STAGE/bundles"
for op_dir in "$BUNDLES"/*/; do
    [[ -d "$op_dir" ]] || continue
    op=$(basename "${op_dir%/}")
    [[ "$op" == "fetch.log" || "$op" == .* ]] && continue
    for b_dir in "$op_dir"*/; do
        [[ -d "$b_dir" ]] || continue
        bundle=$(basename "${b_dir%/}")
        mkdir -p "$STAGE/bundles/$op/$bundle"
        for sub in manifests metadata; do
            [[ -d "$b_dir$sub" ]] && cp -al "$b_dir$sub" "$STAGE/bundles/$op/$bundle/"
        done
    done
done

# 3. git/ — extract github tarballs so source is browseable directly on the mount
log "staging git/ (extracted)"
mkdir -p "$STAGE/git"
shopt -s nullglob
for t in "$GIT"/*.tar.gz; do
    base=$(basename "$t" .tar.gz)   # e.g. 3scale-operator-0ced2bbee24d
    dst="$STAGE/git/$base"
    [[ -d "$dst" ]] && continue
    mkdir -p "$dst"
    # GitHub archives have a single top-level dir like "3scale-operator-master/";
    # strip it so the source tree lands directly under $dst.
    tar -xzf "$t" -C "$dst" --strip-components=1
done
shopt -u nullglob

# 4. meta/ — TSVs, logs, MANIFEST
# Prefer the v2-resolved source mapping when present: git-v2.tsv carries the
# csv_repo rescues + ref-fallback resolutions (its first 5 cols match git.tsv:
# operator, source_url, vcs_ref, tarball, version). Staged git/ is file-driven
# so this only affects meta/MANIFEST/INDEX completeness.
GIT_TSV="$DISC/git.tsv"
[[ -s "$DISC/git-v2.tsv" ]] && GIT_TSV="$DISC/git-v2.tsv"
cp "$DISC"/{operators.tsv,bundles.tsv,containers.tsv,labels.tsv} "$STAGE/meta/" 2>/dev/null || true
cp "$GIT_TSV" "$STAGE/meta/git.tsv" 2>/dev/null || true
cp "$BUNDLES/fetch.log" "$STAGE/meta/bundles-fetch.log" 2>/dev/null || true
cp "$GIT/fetch.log"     "$STAGE/meta/git-fetch.log"     2>/dev/null || true
# Which ref each tarball really came from (sha = the image's commit, else a
# tag/branch standing in for it). Same format as b-operand's git-fetched.tsv;
# read by export-upstream-sources.py and casket-mcp's permalink. 4.18 had
# 27 of 139 tarballs at the exact sha, so this is not a corner case.
python3 "$SCRIPT_DIR/archive-provenance.py" "$GIT" "$GIT_TSV" > "$STAGE/meta/git-fetched.tsv"

# Build MANIFEST.json via python — easier TSV handling than jq slurpfile.
log "building MANIFEST.json"
MINOR="$MINOR" DATE="$DATE" GEN="$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
DISC="$DISC" STAGE="$STAGE" GIT_TSV="$GIT_TSV" python3 <<'PY'
import csv, json, os
disc=os.environ['DISC']; stage=os.environ['STAGE']
def tsv(path, cols):
    if not os.path.exists(path): return []
    with open(path) as f:
        return [dict(zip(cols, r + [""]*(len(cols)-len(r))))
                for r in csv.reader(f, delimiter='\t') if r]
ops  = tsv(f"{disc}/operators.tsv",  ["operator","default_channel","head_bundle","head_image"])
cont = tsv(f"{disc}/containers.tsv", ["operator","head_bundle","container_image","version"])
# git-v2.tsv (preferred) or git.tsv; both carry version in col 5 (extra cols ignored).
git  = tsv(os.environ['GIT_TSV'],    ["operator","source_url","vcs_ref","tarball","version"])
resolved = [g for g in git if g["tarball"] not in ("NO_SOURCE","")]
manifest = {
    "generator": "casket-ocp/phase-b-package.sh",
    "generated_at": os.environ['GEN'],
    "ocp_minor": os.environ['MINOR'],
    "date": os.environ['DATE'],
    "totals": {
        "operators_with_head": len(ops),
        "container_images":    len(cont),
        "source_resolved":     len(resolved),
        "source_missing":      len([g for g in git if g["tarball"]=="NO_SOURCE"]),
        "unique_tarballs":     len({g["tarball"] for g in resolved}),
    },
    "operators": ops,
    "containers": cont,
    "git": git,
}
with open(f"{stage}/meta/MANIFEST.json","w") as f:
    json.dump(manifest, f, indent=2)
PY

# README
cat > "$STAGE/meta/README.txt" <<EOF
casket-ocp Phase B — redhat-operators v${MINOR}
Generated: $(date -u +%Y-%m-%dT%H:%M:%SZ)

catalog/<operator>/catalog.json   File-Based Catalog (FBC), NDJSON, verbatim from
                                  ${CATALOG_INDEX}:v${MINOR}.
bundles/<operator>/<head>/...     Per-operator default-channel HEAD bundle:
                                    manifests/ : CSV + CRDs (the OLM payload)
                                    metadata/  : bundle annotations.yaml etc.
git/<name>-<sha>/...              GitHub source for the operator container's
                                  vcs-ref label, deduped by (source_url, full_sha):
                                  that exact commit where meta/git-fetched.tsv
                                  says exact=1, otherwise the tag/branch the fetch
                                  fell back to (the labelled commit is often not
                                  on public GitHub). Extracted from the
                                  GitHub archive tarball (top-level dir stripped),
                                  so the source tree is readable directly without
                                  unpacking. meta/git.tsv still references the
                                  original tarball filename — strip ".tar.gz" to
                                  get the directory name.
meta/operators.tsv                operator | default_channel | head_bundle | head_image
meta/containers.tsv               operator | head_bundle | containerImage | csv_version
meta/labels.tsv                   containerImage | source_url | vcs_ref
meta/git.tsv                      operator | source_url | vcs_ref | tarball ("NO_SOURCE" if unresolved)
meta/git-fetched.tsv              tarball | pre-existing:<archive top dir> | sha|preexisting | exact
                                  exact=0: git/<name>/ is a tag/branch standing in for vcs_ref
meta/MANIFEST.json                machine-readable index (sha256, totals, all of the above)
deps/<eco>/<name>@<version>/
                       Dependency SOURCE for the trees that do not carry it
                       in-tree: go/ (proxy.golang.org module zips), crates/
                       (crates.io), npm/ (registry tarballs), pypi/ (sdists).
                       A Go tree with its own vendor/ is skipped; Rust, Node and
                       Python never vendor, so for those this is the only copy.
                       This is what makes a CVE in rustls / golang.org/x/net /
                       lodash traceable offline.
meta/SUBMODULES.tsv    component | path | repo | ref | exact | status — git submodules,
                       which a GitHub codeload archive ships as EMPTY DIRS, refilled
                       at the pinned (mode-160000) commit. exact=0 means the pin was
                       unavailable and the .gitmodules branch head was used instead.
meta/SUBMODULES-uncovered.txt
                       present only when something is missing — names every submodule
                       that did NOT land (non-GitHub host, no pin, download failure)
meta/DEPS.tsv          component | manifest | ecosystem | name | version | dir | status
meta/DEPS-uncovered.txt
                       present only when something is missing — names every dep
                       ref that did NOT land, plus manifests that carry no pinned
                       versions (an unpinned requirements.txt has no one right
                       answer to fetch)
git/INDEX.tsv                     dir | repo | ref | version | components — reverse index.
                                  git/ dirs are named after the first operator seen
                                  for a (repo, ref) key; deduped operators share a dir.
by-component/<operator>           -> ../git/<dir>  (every operator by name)
by-repo/<repo-name>               -> ../git/<dir>  (find a tree by upstream repo name)
EOF

# A GitHub codeload archive writes every gitlink as an EMPTY DIR, so a tree
# that uses submodules arrives as build glue with the code missing (the
# 2026-08-19 scan: 1052 trees, all 2543 submodule dirs empty; ZTWIM's release
# repo is nothing but Containerfiles + 5 empty submodules). Fill them before
# collect-deps runs, so the filled-in trees get their own deps collected too.
# Set CASKET_COLLECT_SUBMODULES=0 to skip; never fails the build.
if [[ "${CASKET_COLLECT_SUBMODULES:-1}" == "1" ]]; then
    log "expanding git submodules"
    python3 "$SCRIPT_DIR/collect-submodules.py" "$STAGE" \
        --jobs "${CASKET_SUBMODULE_JOBS:-8}" \
        || log "collect-submodules failed (continuing with empty submodule dirs)"
fi

log "building source index (INDEX.tsv + by-component/ + by-repo/)"
# Language-level dependency sources: a collected tree carries its own code,
# but its deps only if that language vendors in-tree (Go usually, Rust/Node/
# Python never). Fetch the rest from the lockfiles so a dependency CVE is
# traceable offline. Set CASKET_COLLECT_DEPS=0 to skip; never fails the build.
if [[ "${CASKET_COLLECT_DEPS:-1}" == "1" ]]; then
    log "collecting language-level dependency sources"
    python3 "$SCRIPT_DIR/collect-deps.py" "$STAGE" --jobs "${CASKET_DEP_JOBS:-12}" \
        || log "collect-deps failed (continuing without deps/)"
fi

python3 "$SCRIPT_DIR/build-source-index.py" "$STAGE"

# 5. Normalize mode bits. `oc image extract` produces 0640 files which
# mksquashfs preserves verbatim; `-all-root` only normalizes owner, not mode.
# Without this step the mounted casket is unreadable to non-root users.
log "chmod -R a+rX $STAGE"
chmod -R a+rX "$STAGE"

# 6. Build casket image.
log "building casket image (${CASKET_FORMAT}) → $OUT_PATH"

casket_mkfs "$STAGE" "$OUT_PATH" -Xbcj x86 -no-xattrs

record_artifact "$OUT_PATH" "$ARTIFACT_OUT"
log "done"
ls -lh "$OUT_PATH"
file "$OUT_PATH"
