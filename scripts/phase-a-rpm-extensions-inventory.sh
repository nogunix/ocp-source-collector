#!/bin/bash
# Node-OS *extensions* inventory: enumerate the RPMs shipped in the release
# payload's rhel-coreos-extensions image, and emit them in the exact 5-column
# format 01-extract-rpmdb.sh produces, so the existing VM fetch (02/03) can
# consume them with no changes.
#
# WHY THIS EXISTS
# ---------------
# a-rpm's stream A reads the *base* rpmdb of rhel-coreos. That is only half of
# the node OS. Optional components ship separately, as plain RPMs inside the
# rhel-coreos-extensions payload image, and land on the node via
#   MachineConfig{Spec:{Extensions: ["sandboxed-containers"]}}
#   -> machine-config-operator -> rpm-ostree
# Nothing in the base rpmdb mentions them, so they were invisible to a-rpm:
# 162 RPMs / 10 extensions in 4.20.32, of which 89 binaries (23 distinct SRPMs)
# had no source anywhere in the casket. kata-containers is the one that
# surfaced this -- OSC's operator resolves this very image at runtime
# (sandboxed-containers-operator controllers/daemonset_reconcile.go:34) -- but
# it also covers kernel-rt, usbguard, libreswan, pacemaker/pcs, crun-wasm.
#
# SCOPE: el9 ONLY, on purpose (2026-08-12).
# 4.21/4.22 additionally ship rhel-coreos-10-extensions (RHCOS on RHEL 10.2 --
# com.coreos.osname=rhcos, a real node OS, not a side artifact) and a
# rhel-coreos-10 base image that 01 does not read either. Both are real gaps
# and both are deferred. They are NEVER silently skipped: every el10 tag seen
# is written to extensions-deferred.txt so the gap stays named and countable.
# See docs/a-rpm-collection.md.
#
# Outputs under $CASKET_WORK/phase-a-rpm/rpmdb-extensions-<date>/:
#   <patch>-extensions.tsv    name|epoch|version|release|arch  (== 01's format)
#   meta/extensions-map.tsv   patch|extension|package  (from extensions.json)
#   meta/extensions-deferred.txt  payload tags deliberately not collected yet
#   meta/extensions-missing.txt   SRPM NEVRs absent from the pool = to fetch
#   cache/<digest>/           extracted RPMs, so re-runs cost nothing
#
# Only the package tsvs sit at the top level, and everything else is under
# meta/ for one specific reason: both 02-fetch-srpms.sh and
# phase-a-rpm-by-ocp.sh read their input as a bare "$DIR"/*.tsv glob. A
# 3-column extensions-map.tsv sitting next to the 5-column package lists would
# be silently parsed as a package list -- by-ocp would grow a bogus
# by-ocp/extensions-map/ and emit garbage rows, with nothing raising an error.
#
# The tsv basename carries the "-extensions" suffix deliberately: 02 derives
# its rhocp repo list by trimming the basename to MAJ.MIN, which still works,
# and phase-a-rpm-by-ocp.sh keys by-ocp/<name> off it, so the extension
# packages land in their own by-ocp/<patch>-extensions/ tree rather than being
# mixed into the default-installed set.

set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
source "$(dirname "${BASH_SOURCE[0]}")/lib-fingerprint.sh"

ARCH="${ARCH:-x86_64}"
OCARCH="linux/amd64"
POOL="${POOL:-/srv/sources-ocp-srpms/srpms}"
STAMP="$(date +%Y%m%d)"
OUT_DIR="${OUT_DIR:-${CASKET_WORK}/phase-a-rpm/rpmdb-extensions-${STAMP}}"
VERSIONS=()
EXT_PATH="/usr/share/rpm-ostree/extensions/"

# el9 only for now; el10 tags are recorded, not collected. Keep these two
# patterns adjacent -- a tag matching neither is an unknown shape and must be
# reported rather than dropped.
TAG_COLLECT='^rhel-coreos-extensions$'
TAG_DEFER='^(rhel-coreos-10|rhel-coreos-10-extensions)$'

usage() {
    cat >&2 <<EOF
usage: $(basename "$0") [-v <patch>]... [-o <outdir>] [--pool <dir>]

  -v <patch>   explicit OCP patch (repeatable). Default: current patch of every
               minor in config/phase-a-rpm-minors.txt, via stable channel.
  -o <outdir>  output dir (default $OUT_DIR)
  --pool <dir> existing SRPM pool to diff against (default $POOL)
EOF
    exit 1
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        -v|--version) VERSIONS+=("$2"); shift 2 ;;
        -o|--out) OUT_DIR="$2"; shift 2 ;;
        --pool) POOL="$2"; shift 2 ;;
        -a|--arch) ARCH="$2"; shift 2 ;;
        -h|--help) usage ;;
        *) die "unknown arg: $1" ;;
    esac
done

require_cmd oc jq rpm

if [[ ${#VERSIONS[@]} -eq 0 ]]; then
    log "resolving current patch for each minor in phase-a-rpm-minors.txt"
    while IFS= read -r minor; do
        p="$(fetch_patch "$minor")"
        [[ -n "$p" ]] || { log "warn: could not resolve current patch for $minor -- skipping"; continue; }
        VERSIONS+=("$p")
    done < <(read_units "$CONFIG_DIR/phase-a-rpm-minors.txt")
fi
[[ ${#VERSIONS[@]} -gt 0 ]] || die "no versions to inventory"
log "versions: ${VERSIONS[*]}"

CACHE="$OUT_DIR/cache"
META="$OUT_DIR/meta"
mkdir -p "$CACHE" "$META"
MAP="$META/extensions-map.tsv"
DEFERRED="$META/extensions-deferred.txt"
: > "$MAP"; printf '# patch\textension\tpackage\n' >> "$MAP"
: > "$DEFERRED"

for V in "${VERSIONS[@]}"; do
    out_tsv="$OUT_DIR/${V}-extensions.tsv"
    rel_json="$CACHE/release-${V}.json"

    if [[ ! -s "$rel_json" ]]; then
        log "$V: reading release payload"
        oc adm release info -a "$AUTHFILE" -o json \
            "${RELEASE_REGISTRY}:${V}-${ARCH}" > "$rel_json" 2>/dev/null \
            || { rm -f "$rel_json"; die "$V: oc adm release info failed"; }
    fi

    # Record every coreos-ish tag we are NOT collecting, so the el10 gap (and
    # any future rename) is named in a file rather than lost in a regex.
    jq -r --arg d "$TAG_DEFER" \
        '.references.spec.tags[].name | select(test($d))' "$rel_json" \
        | sed "s/^/${V}\t/" >> "$DEFERRED"

    if [[ -s "$out_tsv" ]]; then
        log "$V: $(wc -l < "$out_tsv") pkgs already inventoried, skipping"
        continue
    fi

    digest="$(jq -r --arg c "$TAG_COLLECT" \
        '.references.spec.tags[] | select(.name|test($c)) | .from.name' "$rel_json")"
    [[ -n "$digest" && "$digest" != "null" ]] \
        || { log "warn: $V has no rhel-coreos-extensions tag -- skipping"; continue; }

    ext_dir="$CACHE/${digest##*:}"
    if [[ ! -d "$ext_dir" ]]; then
        log "$V: extracting $EXT_PATH from ${digest##*@}"
        tmp="${ext_dir}.partial"
        rm -rf "$tmp"; mkdir -p "$tmp"
        oc image extract "$digest" -a "$AUTHFILE" --filter-by-os "$OCARCH" \
            --path "${EXT_PATH}:${tmp}/" --confirm >/dev/null \
            || { rm -rf "$tmp"; die "$V: extract failed"; }
        mv "$tmp" "$ext_dir"
    else
        log "$V: extensions already extracted (cache hit)"
    fi

    shopt -s nullglob
    rpms=("$ext_dir"/*.rpm)
    shopt -u nullglob
    (( ${#rpms[@]} )) || die "$V: no .rpm under $EXT_PATH in the extensions image"

    # Same 5 columns 01-extract-rpmdb.sh emits from the rpmdb JSON. EPOCHNUM
    # gives 0 (not "(none)") when the package has no epoch, which is what the
    # by-ocp reader treats as absent.
    rpm -qp --qf '%{NAME}\t%{EPOCHNUM}\t%{VERSION}\t%{RELEASE}\t%{ARCH}\n' \
        "${rpms[@]}" 2>/dev/null | sort -u > "$out_tsv"
    log "$V: ${#rpms[@]} RPMs -> $(wc -l < "$out_tsv") rows -> $out_tsv"

    if [[ -s "$ext_dir/extensions.json" ]]; then
        jq -r --arg v "$V" \
            '.extensions | to_entries[] | .key as $e | .value.packages[]
             | [$v, $e, .] | @tsv' "$ext_dir/extensions.json" >> "$MAP"
    else
        log "warn: $V: no extensions.json -- extension->package map incomplete"
    fi
done

# --- coverage against the existing pool ------------------------------------
# Map each binary RPM to its SOURCERPM and report which SRPMs the casket does
# not already have. This is the wishlist stream B/C ultimately have to fill.
MISS="$META/extensions-missing.txt"
: > "$MISS"
if [[ -d "$POOL" ]]; then
    log "diffing against pool $POOL"
    shopt -s nullglob
    for ext_dir in "$CACHE"/*/; do
        rpms=("$ext_dir"*.rpm)
        (( ${#rpms[@]} )) || continue
        rpm -qp --qf '%{SOURCERPM}\n' "${rpms[@]}" 2>/dev/null
    done | sed 's/\.src\.rpm$//' | sort -u | while IFS= read -r nevr; do
        [[ -n "$nevr" ]] || continue
        [[ -d "$POOL/$nevr" ]] || echo "$nevr"
    done > "$MISS"
    shopt -u nullglob
    log "SRPMs not in pool: $(wc -l < "$MISS") (see $MISS)"
else
    log "warn: pool $POOL not found -- skipping coverage diff"
fi

log "deferred (el10, not collected): $(grep -c . "$DEFERRED" || true) tags -- see $DEFERRED"
log "done. Copy $OUT_DIR/*-extensions.tsv into the VM's \$TSV_DIR and run 02/03."
