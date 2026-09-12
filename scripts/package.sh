#!/bin/bash
# Package fetched sources + manifest into a casket-style squashfs.xz archive.
#
# Usage: package.sh -v 4.20.22 [-o /mnt/hdd/casket-ocp]
#
# Produces:
#   <outdir>/casket-<YYYYMMDD>-ocp<ver>.sqfs.xz

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"

print_usage() {
    cat <<'EOF'
package.sh -v <version> [-o <outdir>]
  -v, --version   OCP version
  -o, --outdir    where to write the .sqfs.xz (default: $CASKET_OUT, see lib.sh)
  --artifact-out  write the final artifact path to this file (for callers that
                  must not re-derive the date-stamped name -- see lib.sh)
EOF
}

VERSION=""
OUTROOT="${CASKET_OUT}"
ARTIFACT_OUT=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        -v|--version) VERSION="$2"; shift 2 ;;
        -o|--outdir)  OUTROOT="$2"; shift 2 ;;
        --artifact-out) ARTIFACT_OUT="$2"; shift 2 ;;
        -h|--help)    print_usage; exit 0 ;;
        *) die "unknown arg: $1" ;;
    esac
done
[[ -n "$VERSION" ]] || die "version required (-v X.Y.Z)"

VDIR="$(version_dir "$VERSION")"
STAGE="$VDIR/50-out/stage"
MANIFEST="$VDIR/40-manifest/MANIFEST.json"
GITDIR="$VDIR/10-git"
DISCDIR="$VDIR/00-discover"

[[ -s "$MANIFEST" ]] || die "missing $MANIFEST — run manifest.sh first"

rm -rf "$STAGE"
mkdir -p "$STAGE/git" "$STAGE/meta"

log "staging files (extracting git tarballs)"
# Extract every tarball from 10-git/ into $STAGE/git/<base>/ so the source tree
# is browseable directly on the mounted casket. squashfs xz dedupes the
# uncompressed content across overlapping repos, so the final .sqfs.xz is
# typically smaller than the .tar.gz-bundled version.
for f in "$GITDIR"/*.tar.gz; do
    [[ -e "$f" ]] || continue
    base=$(basename "$f" .tar.gz)
    dst="$STAGE/git/$base"
    [[ -d "$dst" ]] && continue
    mkdir -p "$dst"
    # GitHub archives have a single top-level dir like "<name>-<sha>/"; strip it.
    tar -xzf "$f" -C "$dst" --strip-components=1
done

cp "$MANIFEST"                  "$STAGE/meta/MANIFEST.json"
cp "$DISCDIR/release.json"      "$STAGE/meta/release.json"
cp "$DISCDIR/release-commits.txt" "$STAGE/meta/release-commits.txt"
cp "$DISCDIR/images.tsv"        "$STAGE/meta/images.tsv"
cp "$DISCDIR/commits.tsv"       "$STAGE/meta/commits.tsv"
cp "$GITDIR/fetch.log"          "$STAGE/meta/fetch.log"

cat > "$STAGE/meta/README.txt" <<EOF
casket-ocp ${VERSION}
Generated: $(date -u +%Y-%m-%dT%H:%M:%SZ)
Phase: A (upstream git tarballs)

Layout:
  git/<name>-<sha>/ — extracted GitHub source tree, one per unique (repo, commit).
                     The archive's top-level dir is stripped, so files sit directly
                     under <name>-<sha>/.
  meta/             — manifest, release info, fetch log

See meta/MANIFEST.json for image -> tarball mapping. The "tarball" field still
holds the original archive filename — strip ".tar.gz" to get the directory name.

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
git/INDEX.tsv          dir | repo | ref | version | components — reverse index.
                       git/ dirs are named after the FIRST component seen for a
                       (repo, commit) key, so deduped components share one dir.
by-component/<name>    -> ../git/<dir>  (every image component)
by-repo/<repo-name>    -> ../git/<dir>  (find a tree by upstream repo name)
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

mkdir -p "$OUTROOT"
# UTC date, matching casket-build.sh's expected-artifact check (a local-time date
# here made the two disagree between 00:00 JST and 09:00 JST and the staged
# registration failed with "expected artifact not found").
OUT="$OUTROOT/casket-$(date -u +%Y%m%d)-ocp${VERSION}.$(casket_ext)"

# Normalize mode bits — see phase-b-package.sh for rationale.
log "chmod -R a+rX $STAGE"
chmod -R a+rX "$STAGE"

log "building casket image (${CASKET_FORMAT}) -> $OUT"

casket_mkfs "$STAGE" "$OUT" -Xdict-size 100%

if [[ -s "$OUT" ]]; then
    record_artifact "$OUT" "$ARTIFACT_OUT"
    log "final: $OUT ($(numfmt --to=iec --suffix=B "$(stat -c%s "$OUT")"))"
    log "verify: file '$OUT'"
    file "$OUT"
else
    die "casket_mkfs produced no output"
fi

