#!/bin/bash
# Regression test for the binary-name -> source map in
# scripts/phase-a-rpm-by-ocp.sh when rpmspec cannot help.
#
# Why this exists: the live casket listed 91 by-ocp rows as UNRESOLVED
# (2026-11-13) although their SRPMs were in it. Two gaps:
#   - the spec was looked for only at <nevr>/info/*.spec; entries in the older
#     rpm2cpio layout keep it at <nevr>/<name>.spec (dtc -> libfdt);
#   - when rpmspec fails to parse a spec (the host rpm rejects old %patchN
#     syntax), its subpackages were never mapped, and a subpackage with its
#     own version (ding-libs -> libbasicobjects-0.1.1) has no (V,R) match.
# The fix reads Name:/%package off the spec text when rpmspec fails. Here
# rpmspec is a stub that always fails, so only that fallback can resolve.
#
# Run: tests/test_byocp_spec_fallback.sh   (exit 0 = pass)
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

fail=0
pass() { printf '  ok   %s\n' "$1"; }
bad()  { printf '  FAIL %s\n' "$1"; fail=$((fail+1)); }
eq() {  # eq <desc> <expected> <actual>
    if [[ "$2" == "$3" ]]; then pass "$1"; else
        bad "$1"; printf '         expected: %q\n         actual:   %q\n' "$2" "$3"
    fi
}

T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
mkdir -p "$T/bin" "$T/stage/meta" "$T/rpmdb"
printf '#!/bin/sh\nexit 1\n' > "$T/bin/rpmspec"; chmod +x "$T/bin/rpmspec"

S="$T/stage/srpms"
# old layout: spec at the top level; subpackages carry their own versions
mkdir -p "$S/ding-libs-0.6.1-53.el9"
cat > "$S/ding-libs-0.6.1-53.el9/ding-libs.spec" <<'EOF'
Name: ding-libs
Version: 0.6.1
%package -n libbasicobjects
%package -n %{name}-devel
%package -n libpath_utils
EOF
# srpmix layout, and a second source with the same (V,R) as dtc
mkdir -p "$S/dtc-1.6.0-7.el9/info" "$S/other-1.6.0-7.el9/info"
cat > "$S/dtc-1.6.0-7.el9/info/dtc.spec" <<'EOF'
Name:    dtc
%package -n libfdt
%package devel
%package -n %{?scl_prefix}skipped
EOF
printf 'Name: other\n' > "$S/other-1.6.0-7.el9/info/other.spec"

row() { printf '%s\t0\t%s\t%s\tx86_64\n' "$@"; }
{
    row libbasicobjects 0.1.1 53.el9
    row libpath_utils   0.2.1 53.el9
    row libfdt          1.6.0 7.el9
    row dtc-devel       1.6.0 7.el9
    row other           1.6.0 7.el9
    row nosuch          1.0   1.el9
} > "$T/rpmdb/9.9.9.tsv"

PATH="$T/bin:$PATH" CASKET_WORK="$T" \
    "$HERE/../scripts/phase-a-rpm-by-ocp.sh" -s "$T/stage" -r "$T/rpmdb" -j 2 >/dev/null 2>&1 \
    || bad "phase-a-rpm-by-ocp.sh exited non-zero"

got=$(awk -F'\t' '!/^#/{print $1, $2}' "$T/stage/meta/bin-to-src.tsv")
eq "subpackages of an unparseable spec map to their source" \
"libbasicobjects-0.1.1-53.el9.x86_64 ding-libs-0.6.1-53.el9
libpath_utils-0.2.1-53.el9.x86_64 ding-libs-0.6.1-53.el9
libfdt-1.6.0-7.el9.x86_64 dtc-1.6.0-7.el9
dtc-devel-1.6.0-7.el9.x86_64 dtc-1.6.0-7.el9
other-1.6.0-7.el9.x86_64 other-1.6.0-7.el9
nosuch-1.0-1.el9.x86_64 UNRESOLVED" "$got"

eq "by-ocp links the sources" "ding-libs dtc other" \
    "$(ls "$T/stage/by-ocp/9.9.9" | paste -sd' ')"

(( fail == 0 )) && echo "all passed" || { echo "$fail failed"; exit 1; }
