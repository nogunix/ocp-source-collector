#!/bin/bash
# phase-a-rpm-extract-one.sh <src.rpm> <stage_root>
#
# Extracts one SRPM into <stage_root>/<NEVR>/ using the srpmix7 layout:
#
#   <NEVR>/pre-build/          rpmbuild -bp output (patched source tree)
#   <NEVR>/archives/           SOURCES contents (tarballs, patches, aux files)
#   <NEVR>/info/<name>.spec    spec file
#   <NEVR>/info/srpm_query_*   per-tag rpm metadata (srpmix7 path only)
#   <NEVR>/log/                rpmbuild logs (srpmix7 path only)
#   <NEVR>/SUCCESSFUL          marker: expansion completed
#   <NEVR>/_incomplete         marker: expansion failed (partial output kept)
#
# Dispatch order:
#   1. Container (casket-srpm-expand:elN) — rpmbuild -bp runs under the
#      distro's own rpm, so %patchN and other version-specific macros work.
#   2. srpmix7 expand on the host (if SRPMIX7= set and zsh available).
#   3. Built-in fallback on the host — same layout, but rpmbuild -bp will
#      fail on el8/el9 specs under Fedora rpm 6.x (%patchN obsolete).
#
# Idempotent: if <stage_root>/<NEVR>/ already exists, exits 0 without re-extracting.
set -euo pipefail
shopt -s nullglob

src="$1"
stage_root="$2"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
: "${SRPMIX7:=${SCRIPT_DIR}/../srpmix7/srpmix7}"
: "${CONTAINER_IMAGE_PREFIX:=casket-srpm-expand}"

nevr=$(rpm -qp --qf '%{NAME}-%{VERSION}-%{RELEASE}' "$src" 2>/dev/null)
[[ -n "$nevr" ]] || { echo "FAIL (no NEVR): $src" >&2; exit 1; }

dest="$stage_root/$nevr"
[[ -d "$dest" ]] && exit 0

tmp=$(mktemp -d)
trap "rm -rf '$tmp'" EXIT

# Detect dist tag from the release string: .el9_4.6 → el9, .el10 → el10
detect_dist() {
    local rel
    rel=$(rpm -qp --qf '%{RELEASE}' "$1" 2>/dev/null)
    if [[ "$rel" =~ \.el([0-9]+) ]]; then
        echo "el${BASH_REMATCH[1]}"
    fi
}

# --- container path: rpmbuild -bp inside a per-distro image ---
container_extract() {
    local dist="$1"
    local image="${CONTAINER_IMAGE_PREFIX}:${dist}"

    podman image exists "$image" 2>/dev/null || return 1

    local src_abs
    src_abs=$(cd "$(dirname "$src")" && pwd)/$(basename "$src")

    mkdir -p "$dest/info" "$dest/archives" "$dest/pre-build"

    local top="$tmp/rpmbuild"
    mkdir -p "$top"/{SOURCES,SPECS,BUILD,BUILDROOT,RPMS,SRPMS}

    # Install SRPM on the host (just unpacks spec+sources, no %prep)
    if ! rpm --define "_topdir $top" -i "$src" 2>/dev/null; then
        return 1
    fi

    cp "$top"/SPECS/*.spec "$dest/info/" 2>/dev/null || true
    for f in "$top"/SOURCES/*; do
        [[ -e "$f" ]] || continue
        mv "$f" "$dest/archives/"
    done

    # Run rpmbuild -bp inside the container with the distro's rpm
    local c_top="/work/rpmbuild"
    if podman run --rm \
        --volume "$top/SPECS:$c_top/SPECS:ro,z" \
        --volume "$dest/archives:$c_top/SOURCES:ro,z" \
        --volume "$top/BUILD:$c_top/BUILD:z" \
        "$image" \
        bash -c "rpmbuild --define '_topdir $c_top' --nodeps -bp $c_top/SPECS/*.spec" \
        >"$tmp/prep.log" 2>&1; then

        local build_out="$top/BUILD"
        local wrapped=( "$build_out"/*-build )
        if (( ${#wrapped[@]} == 1 )) && [[ -d "${wrapped[0]}" ]]; then
            build_out="${wrapped[0]}"
        fi
        for d in "$build_out"/*; do
            [[ "$(basename "$d")" == SPECPARTS ]] && continue
            mv "$d" "$dest/pre-build/" 2>/dev/null || true
        done
        find "$dest/pre-build" -type d -name .git -prune -exec rm -rf {} + 2>/dev/null || true
        : > "$dest/SUCCESSFUL"
    else
        echo "incomplete (container rpmbuild -bp failed)" > "$dest/_incomplete"
    fi

    return 0
}

# --- srpmix7 path ---
srpmix7_extract() {
    mkdir -p "$dest"
    "$SRPMIX7" expand \
        --stype=file --sloc="$src" \
        --dtype=dir  --dloc="$dest" \
        srpm --nodeps 2>"$tmp/srpmix7.log"
}

# --- built-in path: produce srpmix7-compatible layout ---
builtin_extract() {
    mkdir -p "$dest/info" "$dest/archives" "$dest/pre-build"

    local top="$tmp/rpmbuild"
    mkdir -p "$top"/{SOURCES,SPECS,BUILD,BUILDROOT,RPMS,SRPMS}

    if ! rpm --define "_topdir $top" -i "$src" 2>/dev/null; then
        local rc=0
        ( cd "$tmp/raw" 2>/dev/null || { mkdir "$tmp/raw" && cd "$tmp/raw"; } && rpm2cpio "$src" | cpio -idm --quiet ) || rc=$?
        if (( rc != 0 && rc != 141 )); then
            echo "FAIL (cpio rc=$rc): $src" >&2; return 1
        fi
        mv "$tmp/raw"/*.spec "$dest/info/" 2>/dev/null || true
        for f in "$tmp/raw"/*; do
            [[ -e "$f" ]] || continue
            mv "$f" "$dest/archives/"
        done
        echo "incomplete (rpm -i failed, raw fallback)" > "$dest/_incomplete"
        return 0
    fi

    cp "$top"/SPECS/*.spec "$dest/info/" 2>/dev/null || true

    for f in "$top"/SOURCES/*; do
        [[ -e "$f" ]] || continue
        mv "$f" "$dest/archives/"
    done

    local spec=( "$top"/SPECS/*.spec )
    if (( ${#spec[@]} )) && rpmbuild --define "_topdir $top" --nodeps -bp "${spec[0]}" \
           >"$tmp/prep.log" 2>&1; then
        local build_out="$top/BUILD"
        local wrapped=( "$build_out"/*-build )
        if (( ${#wrapped[@]} == 1 )) && [[ -d "${wrapped[0]}" ]]; then
            build_out="${wrapped[0]}"
        fi
        for d in "$build_out"/*; do
            [[ "$(basename "$d")" == SPECPARTS ]] && continue
            mv "$d" "$dest/pre-build/" 2>/dev/null || true
        done
        find "$dest/pre-build" -type d -name .git -prune -exec rm -rf {} + 2>/dev/null || true
        : > "$dest/SUCCESSFUL"
    else
        echo "incomplete (rpmbuild -bp failed)" > "$dest/_incomplete"
    fi

    return 0
}

# --- dispatch ---
dist=$(detect_dist "$src")

# 1. Container: best path — distro-native rpm handles %patchN etc.
if [[ -n "$dist" ]] && container_extract "$dist"; then
    exit 0
fi
[[ -n "$dist" ]] && rm -rf "$dest"

# 2. srpmix7 on host
if [[ -x "$SRPMIX7" ]] && command -v zsh >/dev/null 2>&1; then
    if srpmix7_extract; then
        exit 0
    fi
    echo "WARN (srpmix7 failed, built-in fallback): $nevr" >&2
    rm -rf "$dest"
fi

# 3. Built-in fallback (rpmbuild -bp likely fails on el8/el9 under Fedora rpm 6.x)
builtin_extract || exit 1
