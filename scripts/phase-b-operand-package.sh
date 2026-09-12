#!/bin/bash
# Phase B / operand sources: bundle a layered product's operand sources into
# one casket .sqfs.xz.
#
# Usage: phase-b-operand-package.sh -p <package> -v <minor> [-i <infix>] [-o /mnt/hdd/casket-ocp]
#   -i  filename infix (default: package name). e.g. -i cnv  -> casket-<date>-cnv-ocp<minor>.sqfs.xz
#
# Produces:
#   <out>/casket-<YYYYMMDD>-<infix>-ocp<minor>.sqfs.xz
# with internal layout:
#   bundle/manifests/...               head bundle CSV + CRDs (the OLM payload)
#   git/<name>-<short>/...             extracted github source, deduped by (repo, ref|tag)
#   meta/{MANIFEST.json,README.txt,images.tsv,labels.tsv,git.tsv,git-fetched.tsv,fetch.log}
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"

PKG=""; VERSION=""; INFIX=""; OUT_DIR="${CASKET_OUT}"; STAGE_ONLY=0
while (( $# )); do
    case "$1" in
        -p|--package) PKG="$2";     shift 2 ;;
        -v|--version) VERSION="$2";  shift 2 ;;
        -i|--infix)   INFIX="$2";    shift 2 ;;
        -o|--outdir)  OUT_DIR="$2";  shift 2 ;;
        --stage-only) STAGE_ONLY=1;  shift ;;
        -h|--help)    echo "phase-b-operand-package.sh -p <pkg> -v <minor> [-i <infix>] [-o <dir>] [--stage-only]"; exit 0 ;;
        *) die "unknown arg: $1" ;;
    esac
done
[[ -n "$PKG" ]]     || die "package required (-p <name>)"
[[ -n "$VERSION" ]] || die "version required (-v X.Y)"
[[ -n "$INFIX" ]]   || INFIX="$PKG"

require_cmd jq sha256sum python3
MINOR="$VERSION"
WORK="${CASKET_WORK}/phase-b-operand/${PKG}/${MINOR}"
DISC="$WORK/00-discover"
GIT="$WORK/20-git"
STAGE="$WORK/50-out/stage"

[[ -d "$DISC/bundle" ]] || die "missing $DISC/bundle (run phase-b-operand-discover.sh)"
[[ -d "$GIT"         ]] || die "missing $GIT (run phase-b-operand-fetch-source.sh)"

DATE=$(date -u +%Y%m%d)
OUT_NAME="casket-${DATE}-${INFIX}-ocp${MINOR}.$(casket_ext)"
OUT_PATH="${OUT_DIR%/}/${OUT_NAME}"

log "staging into $STAGE"
rm -rf "$STAGE"
mkdir -p "$STAGE/meta" "$STAGE/git"

# 1. bundle/ — head bundle manifests verbatim (CSV + CRDs)
log "staging bundle/"
cp -al "$DISC/bundle/." "$STAGE/bundle/"

# 2. git/ — extract github tarballs so source browses directly on the mount
log "staging git/ (extracted)"
shopt -s nullglob
for t in "$GIT"/*.tar.gz; do
    base=$(basename "$t" .tar.gz)
    dst="$STAGE/git/$base"
    [[ -d "$dst" ]] && continue
    mkdir -p "$dst"
    tar -xzf "$t" -C "$dst" --strip-components=1
done
shopt -u nullglob

# 3. meta/
cp "$DISC"/{images.tsv,labels.tsv,git.tsv,meta.tsv} "$STAGE/meta/" 2>/dev/null || true
cp "$GIT/fetch.log" "$STAGE/meta/git-fetch.log" 2>/dev/null || true
cp "$GIT/fetched.tsv" "$STAGE/meta/git-fetched.tsv" 2>/dev/null || true

log "building MANIFEST.json"
PKG="$PKG" MINOR="$MINOR" INFIX="$INFIX" DATE="$DATE" \
GEN="$(date -u +%Y-%m-%dT%H:%M:%SZ)" DISC="$DISC" STAGE="$STAGE" GIT="$GIT" python3 <<'PY'
import csv, json, os
disc=os.environ['DISC']; stage=os.environ['STAGE']; gitdir=os.environ['GIT']
def tsv(path, cols):
    if not os.path.exists(path): return []
    with open(path) as f:
        return [dict(zip(cols, r + [""]*(len(cols)-len(r))))
                for r in csv.reader(f, delimiter='\t') if r]
images = tsv(f"{disc}/images.tsv", ["component","image"])
git    = tsv(f"{disc}/git.tsv",    ["component","source_url","vcs_ref","tarball","version","method"])
resolved = [g for g in git if g["tarball"] not in ("NO_SOURCE","")]
by_method={}
for g in git:
    by_method[g["method"]]=by_method.get(g["method"],0)+1

# 20-git/fetched.tsv records which candidate URL actually answered. Only the
# exact-commit archive is the source the image was built from; a tag or branch
# head is an approximation, and a consumer doing CVE work needs to know which
# it is looking at. Absent for caskets built before 2026-07-30.
fetched = {}
for row in tsv(f"{gitdir}/fetched.tsv", ["tarball","url","kind","exact"]):
    if row["tarball"].startswith("#"): continue
    fetched[row["tarball"]] = row
for g in git:
    f = fetched.get(g["tarball"])
    g["fetched_url"]  = f["url"]  if f else ""
    g["fetch_kind"]   = f["kind"] if f else ""
    g["source_exact"] = (f["exact"] == "1") if f else None
manifest = {
    "generator": "casket-ocp/phase-b-operand-package.sh",
    "generated_at": os.environ['GEN'],
    "package": os.environ['PKG'],
    "infix": os.environ['INFIX'],
    "ocp_minor": os.environ['MINOR'],
    "date": os.environ['DATE'],
    "totals": {
        "operand_images": len(images),
        "source_resolved": len(resolved),
        "source_missing":  len([g for g in git if g["tarball"]=="NO_SOURCE"]),
        "unique_tarballs": len({g["tarball"] for g in resolved}),
        # exact + approx + fetch_failed == source_resolved. Keeping the three
        # apart is the point: "resolved" only means a repo URL was found, and
        # the labelled commit 404s on public github often enough that a bare
        # resolved count reads as far better coverage than there is.
        "source_exact":        len([g for g in resolved if g["source_exact"] is True]),
        "source_approx":       len([g for g in resolved if g["source_exact"] is False]),
        "source_fetch_failed": len([g for g in resolved if g["source_exact"] is None]),
        "by_method": by_method,
    },
    "images": images,
    "git": git,
}

json.dump(manifest, open(f"{stage}/meta/MANIFEST.json","w"), indent=2)

# IMAGE_MAP.tsv: image pullspec -> component -> staged git dir. Lets a
# must-gather / cluster image reference be resolved straight to source
# (JANUS feedback 2026-07-15, proposal 3). Derived rows (f:caa-versions-pin)
# have no image of their own and are keyed by the pseudo-component name.
img_by_comp = {i["component"]: i["image"] for i in images}
with open(f"{stage}/meta/IMAGE_MAP.tsv", "w") as f:
    f.write("# image\tcomponent\tgit_dir\tinfix\n")
    for g in git:
        gd = g["tarball"][:-len(".tar.gz")] if g["tarball"].endswith(".tar.gz") else g["tarball"]
        f.write(f'{img_by_comp.get(g["component"], "-")}\t{g["component"]}\t{gd}\t{os.environ["INFIX"]}\n')
PY

cat > "$STAGE/meta/README.txt" <<EOF
casket-ocp Phase B / operand sources — ${PKG}, OCP v${MINOR}
Generated: $(date -u +%Y-%m-%dT%H:%M:%SZ)

bundle/manifests/...   Head bundle CSV + CRDs from
                       registry.redhat.io/redhat/redhat-operator-index:v${MINOR}.
git/<name>-<ref>/...   Operand source from github at the upstream commit/tag
                       resolved from container labels (upstream-vcs-* preferred
                       over the internal konflux org.opencontainers.image.source).
                       Deduped by (source_url, ref|tag); GitHub archive top-level
                       dir stripped so the tree reads directly.
meta/images.tsv        component | operand image (from CSV spec.relatedImages)
meta/labels.tsv        component | image | source_url | ref | version | method
meta/git.tsv           component | source_url | ref | tarball | version | method
                       ("NO_SOURCE" when no public github upstream is resolvable —
                       typically OCP base images (ose-*, already in Phase A),
                       virtio-win, or internal-only components.)
                       NOTE: method "b:component-map" rows have an EMPTY ref and are
                       fetched by tag (v<version>); this is NOT NO_SOURCE. Several
                       such components dedup to one repo tarball, so the tarball/dir
                       name is just the first component seen (see git/INDEX.tsv).
meta/git-fetched.tsv   tarball | url actually fetched | kind (sha/tag/branch) | exact
                       READ THIS BEFORE TRUSTING A TREE FOR CVE WORK. The commit in
                       the image labels is frequently an internal konflux SHA that
                       404s on public github; when that happens the fetch falls back
                       to a version tag or a branch head, which is an APPROXIMATION
                       of the shipped source, not the build itself. exact=1 means the
                       tree is the labelled commit; exact=0 means it is not, and the
                       url column says what was taken instead. A resolved row absent
                       from this file was not fetched at all (no candidate answered).
                       MANIFEST.json totals split the same three ways
                       (source_exact / source_approx / source_fetch_failed).
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
meta/MANIFEST.json     machine-readable index + per-method resolution totals
git/INDEX.tsv          dir | repo | ref | version | components — reverse index.
                       git/ dirs are named after the FIRST component seen for a
                       (repo, ref|tag) key, so multiple components (and the real
                       repo) can hide under one name (e.g. kubevirt/kubevirt under
                       "passt-network-binding-plugin-cni-v1.4.1"). Use INDEX.tsv
                       and the symlink trees below to find anything by name.
by-component/<comp>    -> ../git/<dir>  (every component, incl. deduped ones)
by-repo/<repo-name>    -> ../git/<dir>  (e.g. by-repo/kubevirt, by-repo/containerized-data-importer)
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

log "chmod -R a+rX $STAGE"
chmod -R a+rX "$STAGE"

if [[ "$STAGE_ONLY" == "1" ]]; then
    log "stage-only: $STAGE ready (skipping mksquashfs)"
    exit 0
fi

log "building casket image (${CASKET_FORMAT}) → $OUT_PATH"

casket_mkfs "$STAGE" "$OUT_PATH" -Xbcj x86 -no-xattrs

log "done"
ls -lh "$OUT_PATH"
file "$OUT_PATH"
