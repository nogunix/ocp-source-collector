#!/bin/bash
# Runs INSIDE the casket-srpm-collect container.
# Replaces the three VM scripts (01-extract-rpmdb.sh, 02-fetch-srpms.sh,
# 03-fetch-eus.sh) in a single pass.
#
# Usage: collect.sh -o <output-dir> [-p <pull-secret>] [-V "4.20.32 4.22.8 ..."]
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

while [[ $# -gt 0 ]]; do
    case "$1" in
        -o|--output)  OUT_DIR="$2"; shift 2 ;;
        -p|--pull-secret) PULL_SECRET="$2"; shift 2 ;;
        -V|--versions) VERSIONS_STR="$2"; shift 2 ;;
        -S|--srpm-subdir)  SRPM_SUBDIR="$2";  shift 2 ;;
        -R|--rpmdb-subdir) RPMDB_SUBDIR="$2"; shift 2 ;;
        -h|--help)
            echo "collect.sh -o <output-dir> [-p <pull-secret>] [-V 'ver1 ver2 ...']"
            echo "           [-S srpms-<date>] [-R rpmdb-<date>]"
            exit 0 ;;
        *) echo "unknown arg: $1" >&2; exit 1 ;;
    esac
done

[[ -n "$OUT_DIR" ]] || { echo "usage: collect.sh -o <output-dir>" >&2; exit 1; }
[[ -f "$PULL_SECRET" ]] || { echo "pull secret not found: $PULL_SECRET" >&2; exit 1; }

log() { printf '\033[1;36m[collect]\033[0m %s\n' "$*"; }

TSV_DIR="$OUT_DIR/$RPMDB_SUBDIR"
SRPMS_DIR="$OUT_DIR/$SRPM_SUBDIR"
mkdir -p "$TSV_DIR" "$SRPMS_DIR"

# ── Phase 1: extract rpmdb ──────────────────────────────────────────────
read -ra VERSIONS <<< "$VERSIONS_STR"
if (( ${#VERSIONS[@]} == 0 )); then
    echo "no versions given (-V); skipping rpmdb extraction" >&2
    if [[ -d "$TSV_DIR" ]] && ls "$TSV_DIR"/*.tsv >/dev/null 2>&1; then
        log "reusing existing rpmdb tsvs"
    else
        echo "no rpmdb tsvs and no versions — nothing to do" >&2; exit 1
    fi
fi

for V in "${VERSIONS[@]}"; do
    out_tsv="$TSV_DIR/$V.tsv"
    if [[ -s "$out_tsv" ]]; then
        log "$V: tsv exists ($(wc -l < "$out_tsv") rows), skipping"
        continue
    fi
    log "$V: querying rhel-coreos rpmdb"
    cache_dir=$(mktemp -d)
    # Keep stderr: sending it to /dev/null turned a single "unknown flag:
    # --rpmdb" (the image had oc 4.17, which predates the flag) into nine
    # identical "extraction failed" warnings that named no cause.
    if ! oc adm release info --registry-config="$PULL_SECRET" \
        --rpmdb --rpmdb-cache="$cache_dir" \
        --rpmdb-image=rhel-coreos \
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
WISH="$OUT_DIR/wishlist.txt"
FETCH_LOG="$OUT_DIR/fetch.log"
MISSING="$OUT_DIR/missing.txt"

mapfile -t OCP_MINORS < <(
    {
        find "$TSV_DIR" -maxdepth 1 -name '*.tsv' -printf '%f\n' 2>/dev/null \
            | sed -e 's/\.tsv$//' -e 's/-extensions$//' \
                  -e 's/^\([0-9]\+\.[0-9]\+\)\..*/\1/'
        awk -F'\t' '{print $4}' "$TSV_DIR"/*.tsv 2>/dev/null \
            | grep -oE 'rhaos[0-9]+\.[0-9]+' | sed 's/^rhaos//'
    } | sort -u -V
)
(( ${#OCP_MINORS[@]} )) || { echo "no *.tsv in $TSV_DIR" >&2; exit 1; }
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
subscription-manager repos \
    --enable=rhel-9-for-x86_64-baseos-rpms \
    --enable=rhel-9-for-x86_64-appstream-rpms \
    --enable=rhel-9-for-x86_64-baseos-source-rpms \
    --enable=rhel-9-for-x86_64-appstream-source-rpms >/dev/null 2>&1 || true
for m in "${OCP_MINORS[@]}"; do
    subscription-manager repos \
        --enable="rhocp-${m}-for-rhel-9-x86_64-rpms" \
        --enable="rhocp-${m}-for-rhel-9-x86_64-source-rpms" >/dev/null 2>&1 \
        || log "warn: rhocp-${m} repos not entitled"
done

for r in \
    rhel-9-for-x86_64-highavailability-rpms \
    rhel-9-for-x86_64-highavailability-source-rpms \
    rhel-9-for-x86_64-resilientstorage-rpms \
    rhel-9-for-x86_64-resilientstorage-source-rpms \
    rhel-9-for-x86_64-nfv-rpms \
    rhel-9-for-x86_64-nfv-source-rpms \
    rhel-9-for-x86_64-rt-rpms \
    rhel-9-for-x86_64-rt-source-rpms \
    codeready-builder-for-rhel-9-x86_64-rpms \
    codeready-builder-for-rhel-9-x86_64-source-rpms \
    fast-datapath-for-rhel-9-x86_64-rpms \
    fast-datapath-for-rhel-9-x86_64-source-rpms
do
    subscription-manager repos --enable="$r" >/dev/null 2>&1 \
        || log "  warn: $r not available"
done

# ── Phase 3: build wishlist and fetch SRPMs ─────────────────────────────
log "building wishlist"
awk -F'\t' '{printf "%s-%s-%s\n", $1, $3, $4}' "$TSV_DIR"/*.tsv | sort -u > "$WISH"
n=$(wc -l < "$WISH")
log "wishlist: $n unique bin-RPM specs"

: > "$FETCH_LOG"
: > "$MISSING"

log "refreshing dnf cache"
dnf -q makecache --source 2>>"$FETCH_LOG" || true

batch=(); i=0
flush() {
    [[ ${#batch[@]} -eq 0 ]] && return
    if ! dnf -q download --source --destdir="$SRPMS_DIR" "${batch[@]}" 2>>"$FETCH_LOG"; then
        for spec in "${batch[@]}"; do
            if ! dnf -q download --source --destdir="$SRPMS_DIR" "$spec" 2>>"$FETCH_LOG"; then
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

n_got=$(ls "$SRPMS_DIR"/*.src.rpm 2>/dev/null | wc -l)
n_miss=$(wc -l < "$MISSING")
log "pass 1: $n_got SRPMs fetched, $n_miss unresolved"

# ── Phase 4: EUS/E4S/FDP retry for micro-releases ──────────────────────
if [[ -s "$MISSING" ]]; then
    log "EUS/E4S retry pass"
    EUS="--enablerepo=rhel-9-for-x86_64-baseos-eus-source-rpms --enablerepo=rhel-9-for-x86_64-appstream-eus-source-rpms"
    E4S="--enablerepo=rhel-9-for-x86_64-baseos-e4s-source-rpms --enablerepo=rhel-9-for-x86_64-appstream-e4s-source-rpms"
    FDP="--enablerepo=fast-datapath-for-rhel-9-x86_64-source-rpms"
    HA="--enablerepo=rhel-9-for-x86_64-highavailability-eus-source-rpms --enablerepo=rhel-9-for-x86_64-highavailability-e4s-source-rpms"
    RS="--enablerepo=rhel-9-for-x86_64-resilientstorage-eus-source-rpms --enablerepo=rhel-9-for-x86_64-resilientstorage-e4s-source-rpms"
    RT="--enablerepo=rhel-9-for-x86_64-rt-eus-source-rpms --enablerepo=rhel-9-for-x86_64-rt-e4s-source-rpms --enablerepo=rhel-9-for-x86_64-nfv-eus-source-rpms --enablerepo=rhel-9-for-x86_64-nfv-e4s-source-rpms"
    CRB="--enablerepo=codeready-builder-for-rhel-9-x86_64-eus-source-rpms"

    # Plain .el9 without releasever pin
    list=$(grep -E "\.el9(\.[0-9]+)*$" "$MISSING" || true)
    if [[ -n "$list" ]]; then
        log "  el9 (no pin): $(echo "$list" | wc -l) pkgs"
        echo "$list" | xargs -r dnf download --source \
            --destdir "$SRPMS_DIR" --skip-broken -q 2>>"$FETCH_LOG" || true
    fi

    # Micro-releases with releasever pin
    for pair in "el9_2 9.2" "el9_4 9.4" "el9_6 9.6" "el9_8 9.8" "el9_0 9.0"; do
        tag=${pair% *}; rv=${pair#* }
        list=$(grep -E "\.${tag}(\.[0-9]+)*$" "$MISSING" || true)
        [[ -z "$list" ]] && continue
        log "  $tag (releasever=$rv): $(echo "$list" | wc -l) pkgs"
        echo "$list" | xargs -r dnf download --source --releasever="$rv" \
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

n_got=$(ls "$SRPMS_DIR"/*.src.rpm 2>/dev/null | wc -l)
n_miss=$(wc -l < "$MISSING")
log "done: $n_got SRPMs, $n_miss still unresolved (see missing.txt)"
