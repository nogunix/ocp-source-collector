#!/bin/bash
# Bundle phase-a-rpm/srpms/ + per-OCP rpmdb tsvs into a single .sqfs.xz, in the
# same internal-xz squashfs format as Phase A/C (and existing /mnt/hdd/casket/*).
# This is Phase A's RPM-level sibling: same release payload, but rhel-coreos
# needs RPM/SRPM extraction instead of git-commit resolution (see README.md).
#
# Each SRPM is extracted into srpms/<NEVR>/ using the srpmix7 layout:
#   pre-build/   rpmbuild -bp output (patched source tree)
#   archives/    SOURCES contents (tarballs, patches, aux files)
#   info/        spec file + rpm metadata
# Users can cd straight into source on the mounted casket.
#
# Usage: phase-a-rpm-package.sh [-o OUT_DIR]
#   OUT_DIR defaults to /mnt/hdd/casket-ocp
#   EXTRACT_JOBS env var controls parallelism (default 8).

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"

OUT_DIR="${CASKET_OUT}"
ARTIFACT_OUT=""
while (( $# )); do
  case "$1" in
    -o|--out) OUT_DIR="$2"; shift 2 ;;
    --artifact-out) ARTIFACT_OUT="$2"; shift 2 ;;
    -h|--help) echo "phase-a-rpm-package.sh [-o OUT_DIR] [--artifact-out FILE]"; exit 0 ;;
    *) die "unknown arg: $1" ;;
  esac
done

require_cmd mksquashfs jq sha256sum rpm rpm2cpio cpio tar xargs

PHASE_A_RPM="${CASKET_WORK}/phase-a-rpm"
SRPM_DIR="${PHASE_A_RPM}/srpms"
RPMDB_DIR="${PHASE_A_RPM}/rpmdb"
LOG_DIR="${PHASE_A_RPM}/logs"
STAGE="${PHASE_A_RPM}/50-out/stage"
EXTRACT_JOBS="${EXTRACT_JOBS:-8}"

[[ -d "$SRPM_DIR"  ]] || die "no SRPMs at $SRPM_DIR (run rsync from VM first)"
[[ -d "$RPMDB_DIR" ]] || die "no rpmdb tsvs at $RPMDB_DIR"

DATE=$(date -u +%Y%m%d)
OUT_NAME="casket-${DATE}-ocp-srpms.sqfs.xz"
OUT_PATH="${OUT_DIR%/}/${OUT_NAME}"

log "staging into $STAGE"
if [[ "${KEEP_STAGE:-0}" != "1" ]]; then
    rm -rf "$STAGE"
fi
mkdir -p "$STAGE/srpms" "$STAGE/meta" "$STAGE/meta/rpmdb"

# 1. Extract each SRPM into srpms/<NEVR>/ using srpmix7 layout (pre-build/,
#    archives/, info/). KEEP_STAGE=1 reuses an already-populated $STAGE/srpms
#    (extraction is also idempotent per-NEVR, so re-running only fills gaps).
log "extracting $(ls "$SRPM_DIR" | wc -l) SRPMs in parallel (-P ${EXTRACT_JOBS})"
( cd "$SRPM_DIR" && ls *.src.rpm ) | \
    xargs -P "$EXTRACT_JOBS" -I{} \
        "$SCRIPT_DIR/phase-a-rpm-extract-one.sh" "$SRPM_DIR/{}" "$STAGE/srpms"

# 2. Copy rpmdb tsvs and fetch logs verbatim.
cp "$RPMDB_DIR"/*.tsv "$STAGE/meta/rpmdb/"
[[ -d "$LOG_DIR" ]] && cp "$LOG_DIR"/*.txt "$LOG_DIR"/*.log "$STAGE/meta/" 2>/dev/null || true

# 3. Build MANIFEST.json: per OCP version a list of NEVRA, plus flat SRPM index.
log "building MANIFEST.json (rpm -qp on each SRPM for canonical NEVR)"
TMP_SRPM_JSON=$(mktemp)
trap "rm -f $TMP_SRPM_JSON" EXIT

# For each .src.rpm, capture filename + sha256 + size + canonical NEVR.
for f in "$SRPM_DIR"/*.src.rpm; do
    base=$(basename "$f")
    sha=$(sha256sum "$f" | awk '{print $1}')
    size=$(stat -c %s "$f")
    nevr=$(rpm -qp --qf '%{NAME}-%{VERSION}-%{RELEASE}' "$f" 2>/dev/null)
    printf '{"file":"%s","sha256":"%s","size":%d,"nevr":"%s"}\n' "$base" "$sha" "$size" "$nevr" >> "$TMP_SRPM_JSON"
done
n_srpms=$(wc -l < "$TMP_SRPM_JSON")
log "indexed $n_srpms SRPMs"

# Per-version NEVRA list (just emit the tsv content as JSON arrays).
PER_VERSION_JSON=$(mktemp)
trap "rm -f $TMP_SRPM_JSON $PER_VERSION_JSON" EXIT
for tsv in "$RPMDB_DIR"/*.tsv; do
    v=$(basename "$tsv" .tsv)
    jq -nR --arg v "$v" --slurpfile lines <(jq -R 'split("\t") | {name:.[0], epoch:.[1], version:.[2], release:.[3], arch:.[4]}' "$tsv") \
        '{version:$v, pkgs:$lines}'
done | jq -s '.' > "$PER_VERSION_JSON"

jq -n \
  --slurpfile srpms <(jq -s '.' "$TMP_SRPM_JSON") \
  --slurpfile versions "$PER_VERSION_JSON" \
  --arg date "$DATE" \
  --arg gen "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  '{
     generator: "casket-ocp/phase-a-rpm-package.sh",
     generated_at: $gen,
     date: $date,
     totals: {
       ocp_versions: ($versions[0] | length),
       srpms: ($srpms[0] | length),
       unique_binary_rpms: ([$versions[0][].pkgs[] | "\(.name)-\(.version)-\(.release)"] | unique | length)
     },
     ocp_versions: ($versions[0] | map({version, pkg_count: (.pkgs|length)})),
     srpms: $srpms[0]
   }' > "$STAGE/meta/MANIFEST.json"

# 3b. Build by-ocp/<version>/<src-name> -> srpms/<NEVR> symlink tree so the OCP
#     minor each SRPM belongs to is visible on the mounted casket (srpms/ stays
#     deduped). Also writes meta/bin-to-src.tsv.
log "building by-ocp symlink tree"
"$SCRIPT_DIR/phase-a-rpm-by-ocp.sh" -s "$STAGE" -r "$RPMDB_DIR" -j "$EXTRACT_JOBS"

# 4. Tiny README inside the casket.
n_srpm_dirs=$(find "$STAGE/srpms" -mindepth 1 -maxdepth 1 -type d | wc -l)
n_successful=$(find "$STAGE/srpms" -maxdepth 2 -name SUCCESSFUL | wc -l)
n_incomplete=$(find "$STAGE/srpms" -maxdepth 2 -name _incomplete | wc -l)
cat > "$STAGE/meta/README.txt" <<EOF
casket-ocp Phase A / RPM sources — RHCOS (rhel-coreos) SRPMs
Generated: $(date -u +%Y-%m-%dT%H:%M:%SZ)
Layout: srpmix7 (https://github.com/masatake/srpmix7)

srpms/<NEVR>/                       One directory per SRPM ($n_srpm_dirs total, deduped
                                    across all OCP versions). NEVR = name-version-release.
srpms/<NEVR>/pre-build/             PATCHED source — rpmbuild -bp output (%setup + %patch).
                                    RHEL/downstream-only changes ARE present in the tree.
                                    Markers: SUCCESSFUL ($n_successful) | _incomplete
                                    ($n_incomplete, %prep failed — partial output kept).
srpms/<NEVR>/archives/              SOURCES contents — original tarballs, .patch files,
                                    and auxiliary sources.
srpms/<NEVR>/info/<name>.spec       RPM spec file.
srpms/<NEVR>/info/srpm_query_*      Per-tag rpm metadata (srpmix7 path only).
srpms/<NEVR>/log/                   rpmbuild logs (srpmix7 path only).

by-ocp/<OCP-VER>/<src-name>         Symlink into srpms/<NEVR> for every source an
                                    OCP minor uses. cd by-ocp/4.20.22/ to see /
                                    browse exactly that release's SRPMs (srpms/
                                    itself is deduped across all versions).

meta/rpmdb/<V>.tsv      Per-OCP-version package list, columns:
                          name TAB epoch TAB version TAB release TAB arch
meta/bin-to-src.tsv     binary-NEVRA TAB source-NEVR TAB ocp-version
meta/MANIFEST.json      Index: SRPM files (sha256/size/nevr) + per-version pkg lists

Source files are extracted; cd /srv/sources-ocp-srpms/srpms/<NEVR>/pre-build/
to browse directly. The original .src.rpm is NOT bundled — re-fetch from rhel-coreos
rpmdb if you need to rpmbuild from scratch.
EOF

# Normalize mode bits — see phase-b-package.sh for rationale.
log "chmod -R a+rX $STAGE"
chmod -R a+rX "$STAGE"

# 5. mksquashfs — same options as Phase A package.sh.
log "mksquashfs → $OUT_PATH"
rm -f "$OUT_PATH"
mksquashfs "$STAGE" "$OUT_PATH" \
    -comp xz -Xbcj x86 \
    -no-progress -all-root -no-xattrs \
    -noappend

record_artifact "$OUT_PATH" "$ARTIFACT_OUT"
log "done"
ls -lh "$OUT_PATH"
file "$OUT_PATH"
