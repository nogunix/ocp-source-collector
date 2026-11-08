#!/bin/bash
# Runs INSIDE the casket-srpm-collect container.
# Replaces the three VM scripts (01-extract-rpmdb.sh, 02-fetch-srpms.sh,
# 03-fetch-eus.sh) in a single pass.
#
# Usage: collect.sh -o <output-dir> [-p <pull-secret>] [-V "4.20.32 4.22.8 ..."]
#                   [-M <rhel-major>]
#
# -M picks the node-OS stream, and must match the image it runs in (UBI9 can
# only see RHEL 9 repos, UBI10 only RHEL 10 -- see the Containerfile):
#   -M 9  (default)  payload tag rhel-coreos     -> rpmdb/<patch>.tsv
#   -M 10            payload tag rhel-coreos-10  -> rpmdb/<patch>-el10.tsv
# Each run fetches only for its own stream's tsvs (<patch>[-extensions].tsv for
# 9, <patch>-el10[-extensions].tsv for 10), so both passes can share one
# rpmdb-<date>/ and srpms-<date>/. A patch whose payload has no rhel-coreos-10
# (anything before 4.21) is skipped by the el10 pass, not failed.
#
# Entitlement: this container REGISTERS ITSELF, it does not borrow the host's.
#
# `subscription-manager repos --enable` needs repo definitions, and those are
# generated from a RHEL product certificate. The casket host is Fedora and has
# no /etc/pki/product, so a registered *host* still yields an empty
# redhat.repo and every --enable below fails. UBI9 ships
# /etc/pki/product-default/479.pem (RHEL 9 x86_64), so registration inside the
# container is what makes those repos resolvable.
#
# Credentials come from /run/rhsm.env (mounted read-only, never baked into the
# image, never passed through the container environment where `podman inspect`
# would show them):
#   RHSM_ORG=...            RHSM_ACTIVATION_KEY=...     (preferred)
#   RHSM_USERNAME=...       RHSM_PASSWORD=...           (fallback)
# The registration is removed again on exit, so repeated runs do not
# accumulate systems in the customer portal.
#
# A host that IS RHEL can still mount its entitlements instead
# (-v /etc/pki/entitlement:...:ro); registration is skipped when the container
# already has a usable identity.
#
# Outputs under <output-dir>/:
#   rpmdb/<patch>.tsv     per-OCP-version package list (name|epoch|ver|rel|arch)
#   srpms/*.src.rpm       source RPMs
#   wishlist.txt          deduped binary RPM specs
#   fetch.log             dnf output
#   missing.txt           unresolved after all passes
#   cdn-unresolved.txt    not on the CDN at all (from cdn-fetch.py)
# The last four carry the stream suffix for -M 10 (wishlist-el10.txt, ...), so
# the el9 pass's files survive the el10 pass.
set -euo pipefail

OUT_DIR=""
PULL_SECRET="/run/pull-secret.json"
VERSIONS_STR=""
# Subdirectory names under $OUT_DIR. The caller passes DATED names
# (srpms-<date>/rpmdb-<date>) on purpose -- see the note above the SRPMS_DIR
# assignment below.
SRPM_SUBDIR="srpms"
RPMDB_SUBDIR="rpmdb"
RELEASE_REGISTRY="${RELEASE_REGISTRY:-quay.io/openshift-release-dev/ocp-release}"
MAJOR=9

while [[ $# -gt 0 ]]; do
    case "$1" in
        -o|--output)  OUT_DIR="$2"; shift 2 ;;
        -p|--pull-secret) PULL_SECRET="$2"; shift 2 ;;
        -V|--versions) VERSIONS_STR="$2"; shift 2 ;;
        -S|--srpm-subdir)  SRPM_SUBDIR="$2";  shift 2 ;;
        -R|--rpmdb-subdir) RPMDB_SUBDIR="$2"; shift 2 ;;
        -M|--major)   MAJOR="$2"; shift 2 ;;
        -h|--help)
            echo "collect.sh -o <output-dir> [-p <pull-secret>] [-V 'ver1 ver2 ...']"
            echo "           [-S srpms-<date>] [-R rpmdb-<date>] [-M 9|10]"
            exit 0 ;;
        *) echo "unknown arg: $1" >&2; exit 1 ;;
    esac
done

[[ -n "$OUT_DIR" ]] || { echo "usage: collect.sh -o <output-dir>" >&2; exit 1; }
[[ -f "$PULL_SECRET" ]] || { echo "pull secret not found: $PULL_SECRET" >&2; exit 1; }
[[ "$MAJOR" =~ ^[0-9]+$ ]] || { echo "-M wants a RHEL major (9, 10), got: $MAJOR" >&2; exit 1; }

# Stream naming. el9 keeps the historical unsuffixed names so every existing
# rpmdb-<date>/ and the by-ocp/<patch>/ tree mean what they always meant.
if [[ "$MAJOR" == 9 ]]; then
    RPMDB_TAG="rhel-coreos"
    SUF=""
else
    RPMDB_TAG="rhel-coreos-${MAJOR}"
    SUF="-el${MAJOR}"
fi

log() { printf '\033[1;36m[collect]\033[0m %s\n' "$*"; }

TSV_DIR="$OUT_DIR/$RPMDB_SUBDIR"
SRPMS_DIR="$OUT_DIR/$SRPM_SUBDIR"
mkdir -p "$TSV_DIR" "$SRPMS_DIR"

# The tsvs this stream fetches for. A basename (minus -extensions) ending in
# -el<N> belongs to stream N; an unsuffixed one belongs to el9.
stream_tsvs() {
    local f b
    for f in "$TSV_DIR"/*.tsv; do
        [[ -e "$f" ]] || continue
        b=$(basename "$f" .tsv); b=${b%-extensions}
        if [[ "$b" =~ -el([0-9]+)$ ]]; then
            [[ "${BASH_REMATCH[1]}" == "$MAJOR" ]] && printf '%s\n' "$f"
        else
            [[ "$MAJOR" == 9 ]] && printf '%s\n' "$f"
        fi
    done
    return 0
}

# ── Phase 1: extract rpmdb ──────────────────────────────────────────────
read -ra VERSIONS <<< "$VERSIONS_STR"
if (( ${#VERSIONS[@]} == 0 )); then
    echo "no versions given (-V); skipping rpmdb extraction" >&2
    if [[ -n "$(stream_tsvs)" ]]; then
        log "reusing existing rpmdb tsvs"
    else
        echo "no rpmdb tsvs and no versions — nothing to do" >&2; exit 1
    fi
fi

for V in "${VERSIONS[@]}"; do
    out_tsv="$TSV_DIR/$V$SUF.tsv"
    if [[ -s "$out_tsv" ]]; then
        log "$V: tsv exists ($(wc -l < "$out_tsv") rows), skipping"
        continue
    fi
    # rhel-coreos-<N> only exists from 4.21 on. Its absence is the normal case
    # for older patches, not an extraction failure, so ask first. (el9 does not
    # ask: every tracked payload carries rhel-coreos, and a missing one IS a
    # failure worth the warning below.)
    if [[ "$MAJOR" != 9 ]] && ! oc adm release info --registry-config="$PULL_SECRET" \
            --image-for="$RPMDB_TAG" "${RELEASE_REGISTRY}:${V}-x86_64" >/dev/null 2>&1; then
        log "$V: payload has no $RPMDB_TAG, skipping"
        continue
    fi
    log "$V: querying $RPMDB_TAG rpmdb"
    cache_dir=$(mktemp -d)
    # Keep stderr: sending it to /dev/null turned a single "unknown flag:
    # --rpmdb" (the image had oc 4.17, which predates the flag) into nine
    # identical "extraction failed" warnings that named no cause.
    if ! oc adm release info --registry-config="$PULL_SECRET" \
        --rpmdb --rpmdb-cache="$cache_dir" \
        --rpmdb-image="$RPMDB_TAG" \
        "${RELEASE_REGISTRY}:${V}-x86_64" >/dev/null 2>"$cache_dir/.stderr"; then
        log "WARN: $V rpmdb extraction failed:"
        sed 's/^/    /' "$cache_dir/.stderr" >&2
        rm -rf "$cache_dir"
        continue
    fi

    cache_json=$(ls "$cache_dir"/sha256_* 2>/dev/null | head -1)
    if [[ -n "$cache_json" ]]; then
        jq -r '.["rpmdb.pkglist"][] | [.[0], .[1], .[2], .[3], .[4]] | @tsv' "$cache_json" > "$out_tsv"
        log "$V: $(wc -l < "$out_tsv") packages"
    else
        log "WARN: $V: no rpmdb cache produced"
    fi
    rm -rf "$cache_dir"
done

# ── Phase 2: enable repos ───────────────────────────────────────────────
# NOT $OUT_DIR/srpms. That directory is the MERGE POINT of three independent
# collection streams -- node-OS base (this script), node-OS extensions, and the
# in-container RPM layer -- and assembling it is a human step
# (docs/a-rpm-collection.md). Writing this stream's output straight into it
# leaves a partial corpus that phase-a-rpm-package.sh will happily package:
# that is the "3270 -> 795" regression the document was written to prevent.
#
# Dated directories are also what the recommended incremental path already
# expects: repackage-srpms-refresh.sh -s srpms-<date> -r rpmdb-<date> overlays
# just the new packages onto the mounted casket, keeping the existing corpus.
WISH="$OUT_DIR/wishlist$SUF.txt"
FETCH_LOG="$OUT_DIR/fetch$SUF.log"
MISSING="$OUT_DIR/missing$SUF.txt"

mapfile -t STREAM_TSVS < <(stream_tsvs)
if (( ${#STREAM_TSVS[@]} == 0 )); then
    # el10: no tracked payload ships rhel-coreos-10 (all older than 4.21).
    # Nothing to fetch is a result, not an error.
    log "no el$MAJOR tsvs in $TSV_DIR -- nothing to fetch for this stream"
    exit 0
fi
log "stream el$MAJOR: ${#STREAM_TSVS[@]} tsvs"

mapfile -t OCP_MINORS < <(
    {
        printf '%s\n' "${STREAM_TSVS[@]##*/}" \
            | sed -e 's/\.tsv$//' -e 's/-extensions$//' -e 's/-el[0-9]\+$//' \
                  -e 's/^\([0-9]\+\.[0-9]\+\)\..*/\1/'
        awk -F'\t' '{print $4}' "${STREAM_TSVS[@]}" 2>/dev/null \
            | grep -oE 'rhaos[0-9]+\.[0-9]+' | sed 's/^rhaos//'
    } | sort -u -V
)
log "rhocp minors: ${OCP_MINORS[*]}"

# ── Phase 1.5: entitle this container ───────────────────────────────────
RHSM_ENV="${RHSM_ENV:-/run/rhsm.env}"
REGISTERED_HERE=0

unregister_maybe() {
    (( REGISTERED_HERE )) || return 0
    log "unregistering this container"
    subscription-manager unregister >/dev/null 2>&1 || true
}
trap unregister_maybe EXIT

if subscription-manager identity >/dev/null 2>&1; then
    log "already entitled (host entitlements mounted, or pre-registered)"
elif [[ -f "$RHSM_ENV" ]]; then
    # shellcheck disable=SC1090
    set -a; . "$RHSM_ENV"; set +a
    if [[ -n "${RHSM_ORG:-}" && -n "${RHSM_ACTIVATION_KEY:-}" ]]; then
        log "registering with org=${RHSM_ORG} (activation key)"
        subscription-manager register --force \
            --org "$RHSM_ORG" --activationkey "$RHSM_ACTIVATION_KEY" >/dev/null \
            || { echo "registration failed" >&2; exit 1; }
    elif [[ -n "${RHSM_USERNAME:-}" && -n "${RHSM_PASSWORD:-}" ]]; then
        log "registering as ${RHSM_USERNAME}"
        subscription-manager register --force \
            --username "$RHSM_USERNAME" --password "$RHSM_PASSWORD" >/dev/null \
            || { echo "registration failed" >&2; exit 1; }
    else
        echo "$RHSM_ENV has neither RHSM_ORG+RHSM_ACTIVATION_KEY nor RHSM_USERNAME+RHSM_PASSWORD" >&2
        exit 1
    fi
    REGISTERED_HERE=1
else
    # Failing here is the point. Without entitlement every dnf download below
    # returns nothing and the run would "succeed" with an empty srpms/.
    echo "no entitlement: not registered and $RHSM_ENV is absent" >&2
    echo "  put RHSM_ORG= and RHSM_ACTIVATION_KEY= in the host file that is" >&2
    echo "  mounted there (default ~/.config/casket/rhsm.env, chmod 600)" >&2
    exit 1
fi

log "enabling repos"
R="rhel-${MAJOR}-for-x86_64"
subscription-manager repos \
    --enable="$R-baseos-rpms" \
    --enable="$R-appstream-rpms" \
    --enable="$R-baseos-source-rpms" \
    --enable="$R-appstream-source-rpms" >/dev/null 2>&1 || true
for m in "${OCP_MINORS[@]}"; do
    subscription-manager repos \
        --enable="rhocp-${m}-for-rhel-${MAJOR}-x86_64-rpms" \
        --enable="rhocp-${m}-for-rhel-${MAJOR}-x86_64-source-rpms" >/dev/null 2>&1 \
        || log "warn: rhocp-${m} (rhel-${MAJOR}) repos not entitled"
done

for r in \
    "$R-highavailability-rpms" \
    "$R-highavailability-source-rpms" \
    "$R-resilientstorage-rpms" \
    "$R-resilientstorage-source-rpms" \
    "$R-nfv-rpms" \
    "$R-nfv-source-rpms" \
    "$R-rt-rpms" \
    "$R-rt-source-rpms" \
    "codeready-builder-for-rhel-${MAJOR}-x86_64-rpms" \
    "codeready-builder-for-rhel-${MAJOR}-x86_64-source-rpms" \
    "fast-datapath-for-rhel-${MAJOR}-x86_64-rpms" \
    "fast-datapath-for-rhel-${MAJOR}-x86_64-source-rpms"
do
    subscription-manager repos --enable="$r" >/dev/null 2>&1 \
        || log "  warn: $r not available"
done

# ── Phase 3: build wishlist and fetch SRPMs ─────────────────────────────
log "building wishlist"
awk -F'\t' '{printf "%s-%s-%s\n", $1, $3, $4}' "${STREAM_TSVS[@]}" | sort -u > "$WISH"
n=$(wc -l < "$WISH")
log "wishlist: $n unique bin-RPM specs"

: > "$FETCH_LOG"
: > "$MISSING"

# skip_if_unavailable: the entitlement lists repos that have no metadata at
# all -- rhocp-4.22-for-rhel-10-x86_64-source-rpms 404s on repomd.xml
# (2026-11-09). dnf's default (False) then fails EVERY command while that
# repo is enabled, which cost the first el10 run all 699 packages; el9 lost
# two batches the same way without anyone noticing. A repo that cannot be
# read has nothing to give, so skip it and keep the others.
DNF=(dnf --setopt=skip_if_unavailable=True)

log "refreshing dnf cache"
# Plain makecache: the source repos are already enabled by subscription-manager.
# (`makecache --source` is not a dnf option -- it printed usage into fetch.log
# on every run, el9 included, and refreshed nothing.)
"${DNF[@]}" -q makecache 2>>"$FETCH_LOG" || true

batch=(); i=0
flush() {
    [[ ${#batch[@]} -eq 0 ]] && return
    if ! "${DNF[@]}" -q download --source --destdir="$SRPMS_DIR" "${batch[@]}" 2>>"$FETCH_LOG"; then
        for spec in "${batch[@]}"; do
            if ! "${DNF[@]}" -q download --source --destdir="$SRPMS_DIR" "$spec" 2>>"$FETCH_LOG"; then
                echo "$spec" >> "$MISSING"
            fi
        done
    fi
    batch=()
}

while IFS= read -r SPEC; do
    i=$((i+1))
    batch+=("$SPEC")
    if (( ${#batch[@]} >= 100 )); then
        log "progress: $i / $n"
        flush
    fi
done < "$WISH"
flush

n_got=$(find "$SRPMS_DIR" -maxdepth 1 -name '*.src.rpm' | wc -l)
n_miss=$(wc -l < "$MISSING")
log "pass 1: $n_got SRPMs fetched, $n_miss unresolved"

# ── Phase 4: EUS/E4S/FDP retry for micro-releases ──────────────────────
if [[ -s "$MISSING" ]]; then
    log "EUS/E4S retry pass"
    EUS="--enablerepo=$R-baseos-eus-source-rpms --enablerepo=$R-appstream-eus-source-rpms"
    E4S="--enablerepo=$R-baseos-e4s-source-rpms --enablerepo=$R-appstream-e4s-source-rpms"
    HA="--enablerepo=$R-highavailability-eus-source-rpms --enablerepo=$R-highavailability-e4s-source-rpms"
    RS="--enablerepo=$R-resilientstorage-eus-source-rpms --enablerepo=$R-resilientstorage-e4s-source-rpms"
    RT="--enablerepo=$R-rt-eus-source-rpms --enablerepo=$R-rt-e4s-source-rpms --enablerepo=$R-nfv-eus-source-rpms --enablerepo=$R-nfv-e4s-source-rpms"
    CRB="--enablerepo=codeready-builder-for-rhel-${MAJOR}-x86_64-eus-source-rpms"

    # Plain .el<N> without releasever pin
    list=$(grep -E "\.el${MAJOR}(\.[0-9]+)*$" "$MISSING" || true)
    if [[ -n "$list" ]]; then
        log "  el$MAJOR (no pin): $(echo "$list" | wc -l) pkgs"
        echo "$list" | xargs -r "${DNF[@]}" download --source \
            --destdir "$SRPMS_DIR" --skip-broken -q 2>>"$FETCH_LOG" || true
    fi

    # Micro-releases with releasever pin, read off the missing packages'
    # own dist tags (el9_6 -> 9.6, el10_2 -> 10.2) instead of a fixed list
    # that has to grow with every RHEL minor and know every major.
    mapfile -t MICRO < <(grep -oE "\.el${MAJOR}_[0-9]+" "$MISSING" | sed 's/^\.//' | sort -u -V)
    for tag in "${MICRO[@]}"; do
        rv="${MAJOR}.${tag#el"${MAJOR}"_}"
        list=$(grep -E "\.${tag}(\.[0-9]+)*$" "$MISSING" || true)
        [[ -z "$list" ]] && continue
        log "  $tag (releasever=$rv): $(echo "$list" | wc -l) pkgs"
        echo "$list" | xargs -r "${DNF[@]}" download --source --releasever="$rv" \
            $EUS $E4S $HA $RS $RT $CRB --destdir "$SRPMS_DIR" --skip-broken -q 2>>"$FETCH_LOG" || true
    done

    # Everything still missing goes to cdn-fetch.py, which resolves against
    # each repo's own metadata instead of guessing releasevers.
    #
    # This replaced a sweep that ran one `dnf download` per package per
    # releasever from a hardcoded 9.0/9.2/9.4/9.6/9.8 list. With no dnf cache
    # in a --rm container that sweep spent 15 hours on 475 packages and
    # downloaded none; cdn-fetch does the same set in 11 minutes and gets 167.
    # It also names the ones that are genuinely gone from the CDN
    # (cdn-unresolved.txt) rather than leaving them indistinguishable from
    # "not tried yet", and it reads the releasever off each package's own
    # dist tag, so el10 and a future 9.10 need no edit.
    if [[ -s "$MISSING" ]] && [[ -x /work/cdn-fetch.py ]]; then
        log "  CDN index fetch for $(wc -l < "$MISSING") remaining"
        /work/cdn-fetch.py --missing "$MISSING" --destdir "$SRPMS_DIR" \
            --unresolved-out "$OUT_DIR/cdn-unresolved$SUF.txt" \
            --jobs "${CDN_JOBS:-8}" || log "  WARN: cdn-fetch failed"
    fi

    # Rebuild missing.txt with what's actually still missing
    still_missing=$(mktemp)
    while IFS= read -r spec; do
        f="${spec}.src.rpm"
        [[ -f "$SRPMS_DIR/$f" ]] || echo "$spec"
    done < "$MISSING" > "$still_missing"
    mv "$still_missing" "$MISSING"
fi

n_got=$(find "$SRPMS_DIR" -maxdepth 1 -name '*.src.rpm' | wc -l)
n_miss=$(wc -l < "$MISSING")
log "done (el$MAJOR): $n_got SRPMs in $SRPM_SUBDIR, $n_miss still unresolved (see ${MISSING##*/})"
