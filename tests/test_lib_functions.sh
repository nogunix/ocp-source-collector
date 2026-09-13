#!/bin/bash
# Tests for parse_args_version_arch(), version_dir(), require_cmd(), and
# catalog_setup() in scripts/lib.sh.
#
# Run: bash tests/test_lib_functions.sh   (exit 0 = pass)
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# parse_args_version_arch calls print_usage on -h; stub it out
print_usage() { :; }

# shellcheck source=../scripts/lib.sh
source "$HERE/../scripts/lib.sh"
set +e +o pipefail

fail=0
pass() { printf '  ok   %s\n' "$1"; }
bad()  { printf '  FAIL %s\n' "$1"; fail=$((fail+1)); }

eq() {
    if [[ "$2" == "$3" ]]; then pass "$1"; else
        bad "$1"; printf '         expected: %s\n         actual:   %s\n' "$2" "$3"
    fi
}

# ---- parse_args_version_arch ------------------------------------------------

# basic: -v sets VERSION, ARCH defaults to x86_64
(
    parse_args_version_arch -v 4.20.22
    [[ "$VERSION" == "4.20.22" && "$ARCH" == "x86_64" ]]
) 2>/dev/null
[[ $? -eq 0 ]] && pass "parse_args: -v sets VERSION, ARCH defaults to x86_64" \
                || bad  "parse_args: -v sets VERSION, ARCH defaults to x86_64"

# -v and -a together
(
    parse_args_version_arch -v 4.20 -a aarch64
    [[ "$VERSION" == "4.20" && "$ARCH" == "aarch64" ]]
) 2>/dev/null
[[ $? -eq 0 ]] && pass "parse_args: -v and -a set both" \
                || bad  "parse_args: -v and -a set both"

# missing -v should fail
(parse_args_version_arch) 2>/dev/null
[[ $? -ne 0 ]] && pass "parse_args: missing -v exits non-zero" \
                || bad  "parse_args: missing -v exits non-zero"

# invalid arch should fail
(parse_args_version_arch -v 4.20 -a sparc) 2>/dev/null
[[ $? -ne 0 ]] && pass "parse_args: invalid arch exits non-zero" \
                || bad  "parse_args: invalid arch exits non-zero"

# unknown arg should fail
(parse_args_version_arch -v 4.20 --bogus) 2>/dev/null
[[ $? -ne 0 ]] && pass "parse_args: unknown arg exits non-zero" \
                || bad  "parse_args: unknown arg exits non-zero"

# all valid arches should succeed
for a in x86_64 aarch64 ppc64le s390x multi; do
    (parse_args_version_arch -v 4.20 -a "$a") 2>/dev/null
    [[ $? -eq 0 ]] && pass "parse_args: arch $a accepted" \
                    || bad  "parse_args: arch $a accepted"
done

# ---- version_dir ------------------------------------------------------------

eq "version_dir 4.20.22" \
    "$CASKET_WORK/ocp4.20.22" \
    "$(version_dir 4.20.22)"

eq "version_dir 4.18" \
    "$CASKET_WORK/ocp4.18" \
    "$(version_dir 4.18)"

# ---- require_cmd ------------------------------------------------------------

# bash should always be available
(require_cmd bash) 2>/dev/null
[[ $? -eq 0 ]] && pass "require_cmd: bash found" \
                || bad  "require_cmd: bash found"

# nonexistent command should fail
(require_cmd nonexistent_cmd_xyz_12345) 2>/dev/null
[[ $? -ne 0 ]] && pass "require_cmd: nonexistent command fails" \
                || bad  "require_cmd: nonexistent command fails"

# multiple commands, all valid
(require_cmd bash cat) 2>/dev/null
[[ $? -eq 0 ]] && pass "require_cmd: multiple valid commands pass" \
                || bad  "require_cmd: multiple valid commands pass"

# multiple commands, one invalid
(require_cmd bash nonexistent_cmd_xyz_12345) 2>/dev/null
[[ $? -ne 0 ]] && pass "require_cmd: one invalid in list fails" \
                || bad  "require_cmd: one invalid in list fails"

# ---- catalog_setup ----------------------------------------------------------

# redhat (default)
(
    CATALOG=redhat
    catalog_setup
    [[ "$CATALOG_SUFFIX" == "" && "$CATALOG_INDEX" == *"redhat-operator-index" ]]
) 2>/dev/null
[[ $? -eq 0 ]] && pass "catalog_setup: redhat -> empty suffix, redhat-operator-index" \
                || bad  "catalog_setup: redhat -> empty suffix, redhat-operator-index"

# certified
(
    CATALOG=certified
    catalog_setup
    [[ "$CATALOG_SUFFIX" == "-certified" && "$CATALOG_INDEX" == *"certified-operator-index" ]]
) 2>/dev/null
[[ $? -eq 0 ]] && pass "catalog_setup: certified -> -certified suffix" \
                || bad  "catalog_setup: certified -> -certified suffix"

# community
(
    CATALOG=community
    catalog_setup
    [[ "$CATALOG_SUFFIX" == "-community" && "$CATALOG_INDEX" == *"community-operator-index" ]]
) 2>/dev/null
[[ $? -eq 0 ]] && pass "catalog_setup: community -> -community suffix" \
                || bad  "catalog_setup: community -> -community suffix"

# invalid catalog should fail
(
    CATALOG=invalid
    catalog_setup
) 2>/dev/null
[[ $? -ne 0 ]] && pass "catalog_setup: invalid CATALOG exits non-zero" \
                || bad  "catalog_setup: invalid CATALOG exits non-zero"

# ---- casket_ext -------------------------------------------------------------

eq "casket_ext: sqfs default" "sqfs.xz" "$(CASKET_FORMAT=sqfs casket_ext)"
eq "casket_ext: erofs" "erofs.zstd" "$(CASKET_FORMAT=erofs casket_ext)"

(CASKET_FORMAT=bogus casket_ext) 2>/dev/null
[[ $? -ne 0 ]] && pass "casket_ext: invalid format exits non-zero" \
                || bad  "casket_ext: invalid format exits non-zero"

# default value (unset -> sqfs)
eq "casket_ext: unset defaults to sqfs.xz" "sqfs.xz" \
    "$(unset CASKET_FORMAT; source "$HERE/../scripts/lib.sh" 2>/dev/null; casket_ext)"

# ---- casket_mkfs ------------------------------------------------------------

# casket_mkfs requires mksquashfs or mkfs.erofs on PATH; test the dispatch logic
# by verifying it calls the right tool (the actual build would need root on Linux).

# sqfs: should fail with a clear error when mksquashfs is not on PATH (in CI it might be)
(
    TMP_MKFS=$(mktemp -d)
    trap "rm -rf '$TMP_MKFS'" EXIT
    mkdir -p "$TMP_MKFS/stage"
    : > "$TMP_MKFS/stage/hello.txt"
    CASKET_FORMAT=sqfs
    # If mksquashfs is not available, casket_mkfs should fail.
    # If it IS available, it'll fail because we're not root, but the dispatch is correct.
    casket_mkfs "$TMP_MKFS/stage" "$TMP_MKFS/out.sqfs.xz" -Xdict-size 100% 2>/dev/null
) 2>/dev/null
# We just check it doesn't produce a "unknown CASKET_FORMAT" error.
# NOTE: casket_mkfs starts with `rm -f "$out"`, so the output argument must be a
# throwaway path — never /dev/null, which root (as in CI containers) can delete,
# breaking every later step that redirects to it.
TMP_OUT=$(mktemp -d)
trap 'rm -rf "$TMP_OUT"' EXIT
(CASKET_FORMAT=sqfs casket_mkfs /nonexistent "$TMP_OUT/a.img" 2>&1) | grep -q "unknown CASKET_FORMAT" \
    && bad "casket_mkfs: sqfs dispatches correctly" \
    || pass "casket_mkfs: sqfs dispatches correctly"

(CASKET_FORMAT=erofs casket_mkfs /nonexistent "$TMP_OUT/b.img" 2>&1) | grep -q "unknown CASKET_FORMAT" \
    && bad "casket_mkfs: erofs dispatches correctly" \
    || pass "casket_mkfs: erofs dispatches correctly"

(CASKET_FORMAT=bogus casket_mkfs /nonexistent "$TMP_OUT/c.img" 2>&1) | grep -q "unknown CASKET_FORMAT" \
    && pass "casket_mkfs: invalid format produces clear error" \
    || bad  "casket_mkfs: invalid format produces clear error"

# ---- casket_mkfs erofs flags ------------------------------------------------
# Verify the erofs branch passes the compression and feature flags by
# intercepting the mkfs.erofs command line with a shim.

TMP_SHIM=$(mktemp -d)
cat > "$TMP_SHIM/mkfs.erofs" <<'SHIM'
#!/bin/sh
echo "$@" > "${EROFS_CAPTURE_FILE}"
exit 0
SHIM
chmod +x "$TMP_SHIM/mkfs.erofs"

EROFS_CAPTURE_FILE="$TMP_OUT/erofs_args.txt"
export EROFS_CAPTURE_FILE

(
    export PATH="$TMP_SHIM:$PATH"
    CASKET_FORMAT=erofs
    mkdir -p "$TMP_OUT/erofs-stage"
    : > "$TMP_OUT/erofs-stage/dummy.txt"
    casket_mkfs "$TMP_OUT/erofs-stage" "$TMP_OUT/erofs-out.erofs.zstd" 2>/dev/null
)

if [[ -f "$EROFS_CAPTURE_FILE" ]]; then
    erofs_args=$(<"$EROFS_CAPTURE_FILE")

    [[ "$erofs_args" == *"--all-root"* ]] \
        && pass "casket_mkfs erofs: --all-root passed" \
        || bad  "casket_mkfs erofs: --all-root passed"

    [[ "$erofs_args" == *"-z zstd,level=12"* ]] \
        && pass "casket_mkfs erofs: -z zstd,level=12 passed" \
        || bad  "casket_mkfs erofs: -z zstd,level=12 passed"

    [[ "$erofs_args" == *"-C 131072"* ]] \
        && pass "casket_mkfs erofs: -C 131072 (128K pcluster) passed" \
        || bad  "casket_mkfs erofs: -C 131072 (128K pcluster) passed"

    [[ "$erofs_args" == *"dedupe,fragments,ztailpacking"* ]] \
        && pass "casket_mkfs erofs: -E dedupe,fragments,ztailpacking passed" \
        || bad  "casket_mkfs erofs: -E dedupe,fragments,ztailpacking passed"
else
    bad "casket_mkfs erofs: shim was not called (capture file missing)"
fi
rm -rf "$TMP_SHIM"

# ---- done -------------------------------------------------------------------

if (( fail )); then
    printf '\n%d test(s) failed\n' "$fail"; exit 1
fi
printf '\nall tests passed\n'
