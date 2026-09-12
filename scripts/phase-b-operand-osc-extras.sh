#!/bin/bash
# Phase B / operand sources, OSC extras: pull the guest-components and
# kata-containers sources that live INSIDE the osc-podvm-payload image but are
# not derivable from any image label. The podvm image is built from
# confidential-containers/cloud-api-adaptor (its vcs-ref -> COMPONENT_MAP), and
# that repo's versions.yaml pins the exact guest-components / kata-containers
# refs baked into the payload. So: read the ALREADY-FETCHED CAA tarball's
# versions.yaml (no extra network for the resolve step) and fetch those two
# archives into 20-git/, appending matching rows to git.tsv + labels.tsv so
# phase-b-operand-package.sh stages and indexes them like any other component.
#
# Run AFTER phase-b-operand-fetch-source.sh, BEFORE phase-b-operand-package.sh:
#   phase-b-operand-osc-extras.sh -v <minor>
#
# Idempotent: rows/tarballs already present are left alone. If the CAA source
# is missing or versions.yaml has no pins (old layouts), logs and exits 0 —
# the rest of the OSC product is still packaged.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"

PKG="sandboxed-containers-operator"
VERSION=""
while (( $# )); do
    case "$1" in
        -v|--version) VERSION="$2"; shift 2 ;;
        -p|--package) PKG="$2";     shift 2 ;;
        -h|--help) echo "phase-b-operand-osc-extras.sh -v <minor> [-p <pkg>]"; exit 0 ;;
        *) die "unknown arg: $1" ;;
    esac
done
[[ -n "$VERSION" ]] || die "version required (-v X.Y)"
require_cmd python3 curl tar

WORK="${CASKET_WORK}/phase-b-operand/${PKG}/${VERSION}"
DISC="$WORK/00-discover"
GIT="$WORK/20-git"
GIT_TSV="$DISC/git.tsv"
[[ -s "$GIT_TSV" ]] || { log "no git.tsv at $GIT_TSV — skipping extras"; exit 0; }

# 1. Locate the fetched CAA tarball (any component resolved to the CAA repo).
caa_tarball=$(awk -F'\t' '$2=="https://github.com/confidential-containers/cloud-api-adaptor" && $4!="NO_SOURCE" {print $4; exit}' "$GIT_TSV")
[[ -n "$caa_tarball" && -s "$GIT/$caa_tarball" ]] || { log "no fetched cloud-api-adaptor tarball — skipping extras"; exit 0; }

# 2. Extract versions.yaml from the tarball (new layout src/cloud-api-adaptor/,
#    old layout repo root) and parse the guest-components / kata pins.
pins=$(tar -xzOf "$GIT/$caa_tarball" --wildcards '*/src/cloud-api-adaptor/versions.yaml' 2>/dev/null \
    || tar -xzOf "$GIT/$caa_tarball" --wildcards '*/versions.yaml' 2>/dev/null \
    || true)
[[ -n "$pins" ]] || { log "versions.yaml not found in $caa_tarball — skipping extras"; exit 0; }

mapfile -t refs < <(printf '%s' "$pins" | python3 -c '
import sys, yaml
try:
    d = yaml.safe_load(sys.stdin) or {}
except Exception:
    sys.exit(0)
oci = d.get("oci") or {}
for key, repo in (("guest-components", "confidential-containers/guest-components"),
                  ("kata-containers",  "kata-containers/kata-containers")):
    e = oci.get(key) or {}
    ref = e.get("reference") or ""
    tag = e.get("tag") or ""
    if ref or tag:
        print(f"{key}\t{repo}\t{ref}\t{tag}")
')
[[ ${#refs[@]} -gt 0 ]] || { log "no oci pins in versions.yaml — skipping extras"; exit 0; }

# 3. Append rows + download, deduped against existing git.tsv entries.
added=0
for row in "${refs[@]}"; do
    IFS=$'\t' read -r comp repo ref tag <<<"$row"
    src="https://github.com/$repo"
    if [[ -n "$ref" ]]; then short="${ref:0:12}"; else short="$tag"; fi
    fname="${comp}-${short}.tar.gz"
    if awk -F'\t' -v s="$src" -v r="$ref" '$2==s && $3==r {found=1} END{exit !found}' "$GIT_TSV"; then
        log "  = $comp ($src @ ${ref:-$tag}) already in git.tsv"
        continue
    fi
    url=""
    [[ -n "$ref" ]] && url="$src/archive/${ref}.tar.gz"
    [[ -z "$url" && -n "$tag" ]] && url="$src/archive/refs/tags/${tag}.tar.gz"
    if [[ ! -s "$GIT/$fname" ]]; then
        curl -sSL --fail -o "$GIT/$fname.tmp" "$url" || { log "  ! $comp download failed ($url)"; rm -f "$GIT/$fname.tmp"; continue; }
        mv "$GIT/$fname.tmp" "$GIT/$fname"
    fi
    printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$comp" "$src" "$ref" "$fname" "${tag#v}" "f:caa-versions-pin" >> "$GIT_TSV"
    printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$comp" "(derived:cloud-api-adaptor versions.yaml)" "$src" "$ref" "${tag#v}" "f:caa-versions-pin" >> "$DISC/labels.tsv"
    log "  + $comp -> $src @ ${ref:-$tag} ($fname)"
    added=$((added+1))
done
log "osc extras: $added source(s) added for $PKG/$VERSION"
