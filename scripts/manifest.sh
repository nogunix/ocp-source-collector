#!/bin/bash
# Build a JSON manifest cross-referencing images, git commits, and downloaded tarballs.
#
# Usage: manifest.sh -v 4.20.22
#
# Outputs:
#   $CASKET_WORK/ocp<ver>/40-manifest/MANIFEST.json

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"

print_usage() { echo "manifest.sh -v <version>"; }

VERSION=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        -v|--version) VERSION="$2"; shift 2 ;;
        -h|--help) print_usage; exit 0 ;;
        *) die "unknown arg: $1" ;;
    esac
done
[[ -n "$VERSION" ]] || die "version required (-v X.Y.Z)"
require_cmd jq sha256sum

VDIR="$(version_dir "$VERSION")"
DISCDIR="$VDIR/00-discover"
GITDIR="$VDIR/10-git"
OUTDIR="$VDIR/40-manifest"
mkdir -p "$OUTDIR"

[[ -s "$DISCDIR/images.tsv"  ]] || die "missing $DISCDIR/images.tsv"
[[ -s "$DISCDIR/commits.tsv" ]] || die "missing $DISCDIR/commits.tsv"
[[ -s "$GITDIR/fetch.log"    ]] || die "missing $GITDIR/fetch.log — run fetch-git.sh"

log "computing sha256 and size for tarballs"
SHA_TSV="$OUTDIR/.sha.tsv"
: > "$SHA_TSV"
for f in "$GITDIR"/*.tar.gz; do
    [[ -e "$f" ]] || continue
    sum=$(sha256sum "$f" | awk '{print $1}')
    sz=$(stat -c%s "$f")
    printf '%s\t%s\t%s\n' "$(basename "$f")" "$sum" "$sz" >> "$SHA_TSV"
done

log "assembling MANIFEST.json"

jq -n \
    --arg version "$VERSION" \
    --arg generated "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    --slurpfile rel "$DISCDIR/release.json" \
    --rawfile images_tsv "$DISCDIR/images.tsv" \
    --rawfile commits_tsv "$DISCDIR/commits.tsv" \
    --rawfile fetch_log "$GITDIR/fetch.log" \
    --rawfile sha_tsv "$SHA_TSV" \
'
def tsv2obj($keys): split("\n") | map(select(length>0)) | map(
    split("\t") as $f | reduce range(0; $keys|length) as $i ({}; .[$keys[$i]] = ($f[$i] // null))
);

($images_tsv  | tsv2obj(["name","pullspec","digest"]))                       as $imgs |
($commits_tsv | tsv2obj(["name","repo","commit"]))                           as $cmts |
($fetch_log   | tsv2obj(["status","name","repo","commit","size"]))           as $fet  |
($sha_tsv     | tsv2obj(["file","sha256","size"]))                           as $shas |

def expected_file($name; $commit): "\($name)-\($commit[0:12]).tar.gz";

($shas | map({key: .file, value: {sha256: .sha256, size: (.size|tonumber)}})
       | from_entries)                                                       as $by_file |

# Map (repo, commit) -> tarball filename, using the deduped name recorded in fetch.log.
($fet | map(select(.status == "OK" or .status == "SKIP"))
      | map({key: (.repo + "|" + .commit),
             value: expected_file(.name; .commit)})
      | from_entries)                                                        as $file_by_rc |

{
  schema: "casket-ocp/1",
  release: {
    version: $version,
    pullspec: $rel[0].image,
    digest: $rel[0].digest,
    created: $rel[0].config.created,
    generated_at: $generated
  },
  images: [
    $imgs[] as $i |
    ($cmts[] | select(.name == $i.name)) as $c |
    {
      name: $i.name,
      image: { pullspec: $i.pullspec, digest: $i.digest },
      source: (
        if $c then
          ($file_by_rc[$c.repo + "|" + $c.commit]) as $tf |
          ($by_file[$tf // ""]) as $meta |
          {
            repo: $c.repo,
            commit: $c.commit,
            tarball: $tf,
            sha256:  ($meta.sha256 // null),
            size:    ($meta.size   // null),
            status:  (if $meta then "OK" else "MISSING" end)
          }
        else null end
      )
    }
  ],
  totals: {
    images: ($imgs | length),
    unique_repo_commits: ($cmts | map(.repo + "|" + .commit) | unique | length),
    tarballs_present: ($by_file | length),
    tarballs_fail: ($fet | map(select(.status == "FAIL")) | length),
    total_bytes: ([$shas[].size | tonumber] | add // 0)
  }
}
' > "$OUTDIR/MANIFEST.json"

rm -f "$SHA_TSV"
log "wrote $OUTDIR/MANIFEST.json ($(stat -c%s "$OUTDIR/MANIFEST.json") bytes)"
log "summary:"
jq '.totals' "$OUTDIR/MANIFEST.json" | sed 's/^/    /'
