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

# ---- ensure_github_token ----------------------------------------------------
# A hand-run casket-build.sh used to build without a token and ship branch-head
# submodules (2026-09-26). gh is stubbed on PATH; nothing touches the network.

GH_SHIM="$(mktemp -d)"
printf '#!/bin/sh\n[ "$1 $2" = "auth token" ] && echo gho_from_gh\n' > "$GH_SHIM/gh"
mkdir -p "$GH_SHIM/nogh"
printf '#!/bin/sh\nexit 1\n' > "$GH_SHIM/nogh/gh"
chmod +x "$GH_SHIM/gh" "$GH_SHIM/nogh/gh"

(
    unset GH_TOKEN; export GITHUB_TOKEN=exported
    PATH="$GH_SHIM:$PATH"
    ensure_github_token && [[ "$GITHUB_TOKEN" == exported ]]
) && pass "ensure_github_token: an exported GITHUB_TOKEN wins over gh" \
  || bad  "ensure_github_token: an exported GITHUB_TOKEN wins over gh"

(
    unset GITHUB_TOKEN; export GH_TOKEN=ghtok
    PATH="$GH_SHIM:$PATH"
    ensure_github_token && [[ -z "${GITHUB_TOKEN:-}" ]]
) && pass "ensure_github_token: GH_TOKEN counts and gh is not consulted" \
  || bad  "ensure_github_token: GH_TOKEN counts and gh is not consulted"

(
    unset GITHUB_TOKEN GH_TOKEN
    PATH="$GH_SHIM:$PATH"
    ensure_github_token && [[ "$GITHUB_TOKEN" == gho_from_gh ]]
) && pass "ensure_github_token: borrows gh auth token and exports it" \
  || bad  "ensure_github_token: borrows gh auth token and exports it"

(
    unset GITHUB_TOKEN GH_TOKEN
    PATH="$GH_SHIM/nogh:$PATH"
    ! ensure_github_token && [[ -z "${GITHUB_TOKEN:-}" ]]
) && pass "ensure_github_token: fails when gh has no credential" \
  || bad  "ensure_github_token: fails when gh has no credential"

# casket-build.sh --apply must refuse before doing anything when no token.
STORES="$GH_SHIM/build.env"
printf 'CASKET_DEP_STORE=/x/dep\nCASKET_SUBMODULE_STORE=/x/sub\n' > "$STORES"
out="$(env -u GITHUB_TOKEN -u GH_TOKEN CASKET_BUILD_ENV="$STORES" PATH="$GH_SHIM/nogh:$PATH" \
    bash "$HERE/../scripts/casket-build.sh" --phase b-operand --unit 4.20 --apply 2>&1)"
rc=$?
(( rc != 0 )) && [[ "$out" == *"no GitHub token"* ]] \
  && pass "casket-build.sh --apply refuses without a token" \
  || { bad "casket-build.sh --apply refuses without a token"; printf '         rc=%s out=%s\n' "$rc" "$out"; }

# ---- load_build_env / store guard -------------------------------------------
# The 2026-09-26 hand run had no CASKET_DEP_STORE and wrote 181G into the repo.
ENVF="$GH_SHIM/env"
cat > "$ENVF" <<'ENVEOF'
# comment
CASKET_DEP_STORE=/mnt/hdd/casket-dep-store
CASKET_SUBMODULE_STORE="/home/u/sub store"
  not a line
EXTRA_ONE='quoted'
ENVEOF
(
    unset CASKET_DEP_STORE CASKET_SUBMODULE_STORE EXTRA_ONE
    CASKET_BUILD_ENV="$ENVF" load_build_env
    [[ "$CASKET_DEP_STORE" == /mnt/hdd/casket-dep-store && "$CASKET_SUBMODULE_STORE" == "/home/u/sub store" \
       && "$EXTRA_ONE" == quoted ]]
) && pass "load_build_env: reads KEY=VALUE, strips quotes, skips junk" \
  || bad  "load_build_env: reads KEY=VALUE, strips quotes, skips junk"
(
    export CASKET_DEP_STORE=/explicit
    CASKET_BUILD_ENV="$ENVF" load_build_env
    [[ "$CASKET_DEP_STORE" == /explicit ]]
) && pass "load_build_env: an exported value wins over the file" \
  || bad  "load_build_env: an exported value wins over the file"
(
    CASKET_BUILD_ENV="$GH_SHIM/missing" load_build_env
) && pass "load_build_env: a missing file is not an error" \
  || bad  "load_build_env: a missing file is not an error"

EMPTYENV="$GH_SHIM/empty.env"; : > "$EMPTYENV"
out="$(env -u CASKET_DEP_STORE -u CASKET_SUBMODULE_STORE GITHUB_TOKEN=t CASKET_BUILD_ENV="$EMPTYENV" \
    bash "$HERE/../scripts/casket-build.sh" --phase b-operand --unit 4.20 --apply 2>&1)"
rc=$?
(( rc != 0 )) && [[ "$out" == *"unset: CASKET_DEP_STORE CASKET_SUBMODULE_STORE"* ]] \
  && pass "casket-build.sh --apply refuses with the stores unset" \
  || { bad "casket-build.sh --apply refuses with the stores unset"; printf '         rc=%s out=%s\n' "$rc" "$out"; }
rm -rf "$GH_SHIM"

# ---- stage_enrich -----------------------------------------------------------

# Stub collectors record the order they ran in. The order is the contract:
# submodules before deps (filled trees get their deps collected), index last.
ENR=$(mktemp -d)
mkdir -p "$ENR/bin" "$ENR/stage"
for s in collect-submodules collect-deps build-source-index; do
    printf 'open("%s", "a").write("%s\\n")\n' "$ENR/order" "$s" > "$ENR/bin/$s.py"
done
(SCRIPT_DIR="$ENR/bin"; stage_enrich "$ENR/stage") 2>/dev/null
eq "stage_enrich: submodules, then deps, then index" \
    "collect-submodules collect-deps build-source-index" "$(paste -sd' ' "$ENR/order")"

rm -f "$ENR/order"
(SCRIPT_DIR="$ENR/bin" CASKET_COLLECT_SUBMODULES=0 CASKET_COLLECT_DEPS=0 stage_enrich "$ENR/stage") 2>/dev/null
eq "stage_enrich: both collectors can be skipped, the index cannot" \
    "build-source-index" "$(paste -sd' ' "$ENR/order")"

rm -f "$ENR/order"
printf 'import sys; sys.exit(1)\n' > "$ENR/bin/collect-deps.py"
(SCRIPT_DIR="$ENR/bin"; set -e; stage_enrich "$ENR/stage") 2>/dev/null
eq "stage_enrich: a failing collector does not fail the build" "0" "$?"
grep -qx build-source-index "$ENR/order" && pass "stage_enrich: the index still runs after a collector fails" \
    || bad "stage_enrich: the index still runs after a collector fails"
rm -rf "$ENR"

# ---- casket_finalize --------------------------------------------------------

FIN=$(mktemp -d)
mkdir -p "$FIN/stage"
: > "$FIN/stage/f"; chmod 600 "$FIN/stage/f"
(
    casket_mkfs() { printf '%s\n' "$*" > "$FIN/args"; echo img > "$2"; }
    casket_finalize "$FIN/stage" "$FIN/out.img" "$FIN/artifact" -Xbcj x86
) >/dev/null 2>&1
eq "casket_finalize: normalizes modes first" "644" "$(stat -c %a "$FIN/stage/f")"
eq "casket_finalize: passes flags through to casket_mkfs" \
    "$FIN/stage $FIN/out.img -Xbcj x86" "$(cat "$FIN/args")"
eq "casket_finalize: records the artifact path" "$FIN/out.img" "$(cat "$FIN/artifact")"

rm -f "$FIN/artifact"
out="$(
    casket_mkfs() { : > "$2"; }
    casket_finalize "$FIN/stage" "$FIN/empty.img" "$FIN/artifact" 2>&1
)"; rc=$?
{ (( rc != 0 )) && [[ "$out" == *"produced no output"* && ! -e "$FIN/artifact" ]]; } \
    && pass "casket_finalize: an empty image dies before it is recorded" \
    || { bad "casket_finalize: an empty image dies before it is recorded"; printf '         rc=%s out=%s\n' "$rc" "$out"; }

out="$(
    casket_mkfs() { return 1; }
    casket_finalize "$FIN/stage" "$FIN/fail.img" "$FIN/artifact" 2>&1
)"; rc=$?
{ (( rc != 0 )) && [[ "$out" == *"casket_mkfs failed"* && ! -e "$FIN/artifact" ]]; } \
    && pass "casket_finalize: a failed mkfs dies before it is recorded" \
    || { bad "casket_finalize: a failed mkfs dies before it is recorded"; printf '         rc=%s out=%s\n' "$rc" "$out"; }
rm -rf "$FIN"

# ---- done -------------------------------------------------------------------

if (( fail )); then
    printf '\n%d test(s) failed\n' "$fail"; exit 1
fi
printf '\nall tests passed\n'
