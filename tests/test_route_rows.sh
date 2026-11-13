#!/bin/bash
# Regression tests for containers/srpm-collect/route-rows.awk, which decides
# which collection stream (el9 / el10 container) fetches each rpmdb row.
#
# Why this exists: rows used to be routed by tsv FILE NAME. RHCOS 10 ships some
# el9 builds -- 4.21.42-el10-extensions.tsv lists
# kata-containers-3.25.0-7.rhaos4.21.el9 -- so the el10 pass got a package its
# UBI10 container cannot see, and it went unfetched (2026-11-10, fetched by
# hand). The dist tag in the release field is what decides now; the file name
# is only the fallback.
#
# Run: tests/test_route_rows.sh   (exit 0 = pass)
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AWK_PROG="$HERE/../containers/srpm-collect/route-rows.awk"

fail=0
pass() { printf '  ok   %s\n' "$1"; }
bad()  { printf '  FAIL %s\n' "$1"; fail=$((fail+1)); }
eq() {  # eq <desc> <expected> <actual>
    if [[ "$2" == "$3" ]]; then pass "$1"; else
        bad "$1"; printf '         expected: %q\n         actual:   %q\n' "$2" "$3"
    fi
}

T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
tab=$'\t'
row() { printf '%s\t0\t%s\t%s\tx86_64\n' "$1" "$2" "$3"; }  # name ver rel

row bash      5.2.26 3.el9           >  "$T/4.21.42.tsv"
row kernel    5.14.0 570.el9_6       >> "$T/4.21.42.tsv"
row bash      5.2.26 5.el10          >  "$T/4.21.42-el10.tsv"
row kata-containers 3.25.0 7.rhaos4.21.el9 >  "$T/4.21.42-el10-extensions.tsv"
row crun      1.20   1.el10_0        >> "$T/4.21.42-el10-extensions.tsv"
row notag     1.0    1               >> "$T/4.21.42-el10-extensions.tsv"
row legacy    1.0    1.el8           >  "$T/4.14.58-extensions.tsv"

route() {  # route <major> -> sorted "minor name-ver-rel" lines
    awk -v major="$1" -f "$AWK_PROG" "$T"/*.tsv 2>/dev/null \
        | awk -F'\t' '{print $1, $2"-"$4"-"$5}' | sort
}

echo "el9 pass"
el9=$(route 9)
eq "el9 gets its own rows, the el9 build from the el10 tsv, and the el8 fallback" \
"4.14 legacy-1.0-1.el8
4.21 bash-5.2.26-3.el9
4.21 kata-containers-3.25.0-7.rhaos4.21.el9
4.21 kernel-5.14.0-570.el9_6" "$el9"

echo "el10 pass"
el10=$(route 10)
eq "el10 keeps its el10 rows and the untagged one, loses kata" \
"4.21 bash-5.2.26-5.el10
4.21 crun-1.20-1.el10_0
4.21 notag-1.0-1" "$el10"

echo "every row lands in exactly one stream"
total=$(cat "$T"/*.tsv | wc -l)
routed=$(( $(route 9 | wc -l) + $(route 10 | wc -l) ))
eq "rows in == rows routed" "$total" "$routed"

echo "routed-in rows are reported"
msg=$(awk -v major=9 -f "$AWK_PROG" "$T"/*.tsv 2>&1 >/dev/null)
eq "stderr names the one moved row" "1 row(s) routed in from other streams' tsvs by dist tag" "$msg"

echo "row content is passed through untouched"
line=$(awk -v major=9 -f "$AWK_PROG" "$T/4.21.42-el10-extensions.tsv" 2>/dev/null)
eq "minor + original 5 columns" "4.21${tab}kata-containers${tab}0${tab}3.25.0${tab}7.rhaos4.21.el9${tab}x86_64" "$line"

(( fail == 0 )) && echo "all passed" || { echo "$fail failed"; exit 1; }
