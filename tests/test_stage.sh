#!/bin/bash
# Regression tests for opengrok/scripts/lib-stage.sh — the per-minor
# keep-patches cap that bounds the OpenGrok project list. Network-free.
#
# Run: tests/test_stage.sh   (exit 0 = pass)
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../opengrok/scripts/lib-stage.sh
source "$HERE/../opengrok/scripts/lib-stage.sh"

fail=0
pass() { printf '  ok   %s\n' "$1"; }
bad()  { printf '  FAIL %s\n' "$1"; fail=$((fail+1)); }

# caps <desc> <keep> <space-separated input> -- <space-separated expected>
caps() {
    local desc="$1" keep="$2" input="$3" want="$4" got
    got="$(printf '%s\n' $input | cap_patches "$keep" | tr '\n' ' ')"
    got="${got% }"
    [[ "$got" == "$want" ]] && pass "$desc" || {
        bad "$desc"; printf '       got=[%s]\n       want=[%s]\n' "$got" "$want"
    }
}

LIVE="4.18.46 4.18.47 4.18.48 4.20.27 4.20.28 4.20.29 4.22.3 4.22.4 4.22.5"

echo "# cap_patches — newest N z-streams per minor"
caps "keep=1 (the production default): newest of each minor only" 1 "$LIVE" \
     "4.18.48 4.20.29 4.22.5"
caps "keep=2 keeps the newest two" 2 "$LIVE" \
     "4.18.47 4.18.48 4.20.28 4.20.29 4.22.4 4.22.5"
caps "keep=0 means unlimited (old behaviour)" 0 "$LIVE" "$LIVE"
caps "keep larger than the input is not an error" 9 "$LIVE" "$LIVE"

echo "# ordering"
# The bug this guards: a lexical sort ranks 4.22.5 above 4.22.10, so the newest
# z-stream would be the one dropped.
caps "double-digit patch outranks single-digit (sort -V, not lexical)" 2 \
     "4.22.3 4.22.5 4.22.9 4.22.10" "4.22.9 4.22.10"
caps "double-digit minor is not confused with a patch" 1 \
     "4.9.60 4.10.5" "4.9.60 4.10.5"
caps "input order does not matter" 2 \
     "4.20.29 4.20.27 4.20.28" "4.20.28 4.20.29"
caps "duplicate versions collapse" 2 \
     "4.20.28 4.20.28 4.20.29" "4.20.28 4.20.29"

echo "# variant tracks (a-rpm's by-ocp/<patch>-extensions)"
# The bug this guards: with a bare-minor cap key, "4.20.32-extensions" and
# "4.20.32" compete as two z-streams of 4.20. sort -V ranks the suffixed one
# higher, so keep=1 kept the 162-package extensions tree and silently dropped
# the 568-package base tree (kata et al. would have been indexed at the cost of
# every default-installed package).
EXT="4.18.50 4.18.50-extensions 4.20.32 4.20.32-extensions 4.22.8 4.22.8-extensions"
caps "keep=1 keeps base AND extensions of each minor" 1 "$EXT" "$EXT"
caps "a variant does not evict the base it sorts above" 1 \
     "4.20.32 4.20.32-extensions" "4.20.32 4.20.32-extensions"
caps "the cap still applies within a variant track" 1 \
     "4.20.28-extensions 4.20.32-extensions" "4.20.32-extensions"
caps "base and variant are capped independently" 1 \
     "4.20.28 4.20.32 4.20.28-extensions 4.20.32-extensions" \
     "4.20.32 4.20.32-extensions"
caps "two different variants are two tracks" 1 \
     "4.22.8 4.22.8-extensions 4.22.8-el10" "4.22.8 4.22.8-el10 4.22.8-extensions"

echo "# track_of"
[[ "$(track_of 4.20.32)" == "4.20" ]] \
    && pass "plain patch -> minor" || bad "plain patch -> minor"
[[ "$(track_of 4.20.32-extensions)" == "4.20-extensions" ]] \
    && pass "variant keeps its suffix" || bad "variant keeps its suffix"

echo "# edge cases"
caps "empty input" 2 "" ""
caps "single version" 2 "4.22.5" "4.22.5"

echo "# is_kept"
kept="$(printf '%s\n' $LIVE | cap_patches 2)"
is_kept 4.20.29 "$kept" && pass "kept version is reported kept" || bad "kept version is reported kept"
is_kept 4.20.27 "$kept" && bad "capped-out version must not be kept" || pass "capped-out version is not kept"
# Guards a substring match: 4.20.2 is not 4.20.28/29.
is_kept 4.20.2 "$kept" && bad "prefix must not match (4.20.2 vs 4.20.28)" || pass "prefix does not match"

echo "# strip_sha"
[[ "$(printf 'kubevirt-a1b2c3d4e5f6' | strip_sha)" == "kubevirt" ]] \
    && pass "trailing 12-hex sha stripped" || bad "trailing 12-hex sha stripped"
[[ "$(printf 'passt-network-binding-plugin-cni-v1.4.1' | strip_sha)" == "passt-network-binding-plugin-cni-v1.4.1" ]] \
    && pass "a version suffix is not a sha" || bad "a version suffix is not a sha"
# "cadc" is 4 hex chars -- under the 7-char floor, so it must survive.
[[ "$(printf 'foo-cadc' | strip_sha)" == "foo-cadc" ]] \
    && pass "short hex suffix is not a sha" || bad "short hex suffix is not a sha"

echo "# link_git_phase — empty projects must not be staged"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
log() { :; }   # silence the lib's progress output for the rest of the run

# A casket whose git/ holds real source trees.
mkdir -p "$TMP/full/git/mig-controller-fd13940869e0/pkg" \
         "$TMP/full/git/mig-ui-ff46aee10866/src"
link_git_phase "$TMP/full/git" "$TMP/stage/layered-4.18/mtc-operator"
rc=$?
(( rc == 0 )) && pass "non-empty git/ returns 0" || bad "non-empty git/ returns 0"
[[ -L "$TMP/stage/layered-4.18/mtc-operator/mig-controller" ]] \
    && pass "clean-named symlink created" || bad "clean-named symlink created"
[[ -d "$TMP/stage/layered-4.18/mtc-operator/mig-controller/pkg" ]] \
    && pass "symlink resolves to the source tree" || bad "symlink resolves to the source tree"

# The service-registry-operator case: git/ exists but carries only INDEX.tsv,
# because every operand image resolved to NO_SOURCE.
mkdir -p "$TMP/empty/git"
: > "$TMP/empty/git/INDEX.tsv"
link_git_phase "$TMP/empty/git" "$TMP/stage/layered-4.18/service-registry-operator"
rc=$?
(( rc == 1 )) && pass "source-less git/ returns 1" || bad "source-less git/ returns 1"
[[ -e "$TMP/stage/layered-4.18/service-registry-operator" ]] \
    && bad "source-less project dir must not be created" \
    || pass "source-less project dir is not created"

# All products of a minor empty => the parent project dir must not appear either,
# or OpenGrok publishes a project with zero files.
link_git_phase "$TMP/empty/git" "$TMP/stage/layered-4.14/apicast-operator" 2>/dev/null
[[ -e "$TMP/stage/layered-4.14" ]] \
    && bad "all-empty minor must not create the parent project" \
    || pass "all-empty minor creates no parent project"

# Collision path: two trees whose clean names match must both survive.
mkdir -p "$TMP/dup/git/csi-operator-aaaaaaaaaaaa" "$TMP/dup/git/csi-operator-bbbbbbbbbbbb"
link_git_phase "$TMP/dup/git" "$TMP/stage/dup"
n=$(ls -1 "$TMP/stage/dup" | wc -l)
(( n == 2 )) && pass "clean-name collision keeps both trees" \
    || { bad "clean-name collision keeps both trees"; printf '       got=%d links\n' "$n"; }

echo "# payload_only — sidecar-only layered products are grouped, not mixed in"
MAP="$TMP/phase-a-map.tsv"
printf '4.20\thttps://github.com/openshift/kube-rbac-proxy\tocp-4.20.35/kube-rbac-proxy\n4.20\thttps://github.com/openshift/oc\tocp-4.20.35/cli\n4.22\thttps://github.com/openshift/router\tocp-4.22.11/router\n' > "$MAP"
idx() { printf 'dir\trepo\tref\tversion\tcomponents\n'; printf '%s\n' "$@"; }

# cephcsi-operator's shape: its own operator is z:none, only the rbac proxy came back.
idx "ose-kube-rbac-proxy-6dd7486b8c21	https://github.com/openshift/kube-rbac-proxy	6dd7486b8c21	4.20.0	ose-kube-rbac-proxy" > "$TMP/side.tsv"
payload_only "$TMP/side.tsv" "$MAP" 4.20 \
    && pass "all trees are payload repos -> sidecar-only" || bad "all trees are payload repos -> sidecar-only"

# Repo match, not commit: the layered build is never the payload commit.
idx "cli-aaaaaaaaaaaa	https://github.com/openshift/oc	aaaaaaaaaaaa		cli" \
    "ose-kube-rbac-proxy-bbbbbbbbbbbb	https://github.com/openshift/kube-rbac-proxy	bbbbbbbbbbbb		x" > "$TMP/side2.tsv"
payload_only "$TMP/side2.tsv" "$MAP" 4.20 \
    && pass "several payload-repo trees, any commit -> sidecar-only" || bad "several payload-repo trees, any commit -> sidecar-only"

# The product's own tree next to a sidecar: it has real source, stays at the top.
idx "ose-kube-rbac-proxy-6dd7486b8c21	https://github.com/openshift/kube-rbac-proxy	6dd7486b8c21		x" \
    "mig-controller-fd13940869e0	https://github.com/migtools/mig-controller	fd13940869e0		mig-controller" > "$TMP/mixed.tsv"
payload_only "$TMP/mixed.tsv" "$MAP" 4.20 \
    && bad "a non-payload tree keeps the product at the top" || pass "a non-payload tree keeps the product at the top"

# Keyed by minor: 4.22's payload repos say nothing about a 4.20 product.
idx "router-cccccccccccc	https://github.com/openshift/router	cccccccccccc		router" > "$TMP/router.tsv"
payload_only "$TMP/router.tsv" "$MAP" 4.20 \
    && bad "another minor's payload does not count" || pass "another minor's payload does not count"

# Empty git/ (INDEX.tsv header only) is not-collected, not sidecar-only.
idx > "$TMP/none.tsv"
payload_only "$TMP/none.tsv" "$MAP" 4.20 \
    && bad "no trees is not sidecar-only" || pass "no trees is not sidecar-only"
payload_only "$TMP/missing.tsv" "$MAP" 4.20 \
    && bad "missing INDEX.tsv is not sidecar-only" || pass "missing INDEX.tsv is not sidecar-only"

echo "# find-symlink-cycles.py — the thing that hung srpms-4.22.4 for 2h50m"
CYC="$HERE/../opengrok/scripts/find-symlink-cycles.py"
cycles_in() { python3 "$CYC" -q "$1" 2>/dev/null; }

# A tree with no cycles must come back clean and exit 0.
mkdir -p "$TMP/clean/a/b/c"
: > "$TMP/clean/a/b/c/file.txt"
ln -sfn ../../b "$TMP/clean/a/b/c/up"          # points at an ancestor by path...
rm -f "$TMP/clean/a/b/c/up"
ln -sfn "$TMP/clean/a/b/c/file.txt" "$TMP/clean/a/filelink"   # link to a FILE: fine
out="$(cycles_in "$TMP/clean")"; rc=$?
[[ -z "$out" ]] && pass "acyclic tree reports nothing" || {
    bad "acyclic tree reports nothing"; printf '       got=[%s]\n' "$out"; }
(( rc == 0 )) && pass "acyclic tree exits 0" || bad "acyclic tree exits 0"

# Self-referential link, criu's `compel -> .` shape.
mkdir -p "$TMP/selfloop/uapi"
ln -sfn . "$TMP/selfloop/uapi/compel"
out="$(cycles_in "$TMP/selfloop")"
[[ "$out" == *"uapi/compel"* ]] && pass "self-referential link (x -> .) detected" || {
    bad "self-referential link (x -> .) detected"; printf '       got=[%s]\n' "$out"; }

# The real one: fwupd's mock sysfs, a two-hop cycle through sibling subtrees.
#   sys/class/block/sde -> ../../devices/blk/sde
#   sys/devices/blk/sde/subsystem -> ../../../class/block
mkdir -p "$TMP/sysfs/sys/class/block" "$TMP/sysfs/sys/devices/blk/sde"
ln -sfn ../../devices/blk/sde "$TMP/sysfs/sys/class/block/sde"
ln -sfn ../../../class/block "$TMP/sysfs/sys/devices/blk/sde/subsystem"
out="$(cycles_in "$TMP/sysfs")"; rc=$?
[[ -n "$out" ]] && pass "mutual sysfs-style cycle detected" || bad "mutual sysfs-style cycle detected"
(( rc == 1 )) && pass "cycle found exits 1" || bad "cycle found exits 1"

# Termination is the whole point: an undetected cycle here means the scan
# hangs exactly like the indexer it exists to protect. 20s is ~1000x headroom
# for this fixture.
timeout 20 python3 "$CYC" -q "$TMP/sysfs" >/dev/null 2>&1
(( $? != 124 )) && pass "scan terminates on a cyclic tree" || bad "scan terminates on a cyclic tree"

# A dangling symlink must not be mistaken for a cycle (DirectoryStream throws
# on it, so OpenGrok never recurses into one either).
mkdir -p "$TMP/dangling"
ln -sfn /nonexistent-target-xyz "$TMP/dangling/broken"
out="$(cycles_in "$TMP/dangling")"
[[ -z "$out" ]] && pass "dangling symlink is not a cycle" || {
    bad "dangling symlink is not a cycle"; printf '       got=[%s]\n' "$out"; }

# A directory reachable twice (diamond, not a cycle) must stay quiet -- this is
# the common shape in SRPM trees and a false positive here costs real source.
mkdir -p "$TMP/diamond/real/sub"
ln -sfn ../real "$TMP/diamond/alias"
mkdir -p "$TMP/diamond/other"
ln -sfn ../real "$TMP/diamond/other/alias2"
out="$(cycles_in "$TMP/diamond")"
[[ -z "$out" ]] && pass "shared (non-cyclic) subtree is not a cycle" || {
    bad "shared (non-cyclic) subtree is not a cycle"; printf '       got=[%s]\n' "$out"; }

if (( fail )); then printf '\n%d test(s) FAILED\n' "$fail"; exit 1; fi
printf '\nall tests passed\n'
