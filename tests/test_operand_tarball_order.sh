#!/bin/bash
# Regression tests for deterministic B-operand tarball naming in
# scripts/phase-b-operand-fetch-source.sh.
#
# Why this exists: step 1 writes 00-discover/labels.tsv via parallel xargs, so
# rows arrive in non-deterministic order. Step 2 names each deduped (repo, ref)
# tarball after the first component that references it; without sorting first,
# shared tarballs flip names between runs (e.g. acm-prometheus-... vs
# acm-prometheus-config-reloader-...).
#
# Run: bash tests/test_operand_tarball_order.sh   (exit 0 = pass)
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$HERE")"
SCRIPT="$ROOT/scripts/phase-b-operand-fetch-source.sh"

fail=0
pass() { printf '  ok   %s\n' "$1"; }
bad()  { printf '  FAIL %s\n' "$1"; fail=$((fail+1)); }

cleanup() { rm -rf "$T"; }
T=$(mktemp -d)
trap cleanup EXIT

# 1. Verify the script sorts $LABELS.tmp deterministically before step 2.
sort_line=$(grep -n 'LC_ALL=C sort -t .*\$LABELS\.tmp.*> "\$LABELS"' "$SCRIPT" | head -n 1 | cut -d: -f1)
step2_line=$(grep -n '^GIT_TSV=' "$SCRIPT" | head -n 1 | cut -d: -f1)
if [[ -n "$sort_line" && -n "$step2_line" ]] && (( sort_line < step2_line )); then
    pass "phase-b-operand-fetch-source.sh sorts labels before step 2 (line $sort_line < $step2_line)"
else
    bad "phase-b-operand-fetch-source.sh missing pre-step-2 LC_ALL=C sort (sort=${sort_line:-none}, step2=${step2_line:-none})"
fi

# 2. Run the same sort + step-2 awk dedup over two permutations of labels.tsv.
run_step2() {
    local in_tsv="$1" sorted="$2" out_tsv="$3"
    LC_ALL=C sort -t $'\t' -k1,1 -k2,2 "$in_tsv" > "$sorted"
    awk -F'\t' 'BEGIN{OFS="\t"}
    {
        comp=$1; src=$3; ref=$4; ver=$5; method=$6;
        if (src=="") { print comp,"","","NO_SOURCE",ver,method; next }
        if (ref!="") { key=src"|"ref; short=substr(ref,1,12) }
        else         { key=src"|v"ver; short="v"ver }
        if (!(key in seen)) seen[key]=comp"-"short".tar.gz";
        print comp,src,ref,seen[key],ver,method;
    }' "$sorted" > "$out_tsv"
}

cat > "$T/order-a.tsv" <<'EOF'
acm-prometheus-config-reloader	quay.io/img/reloader	https://github.com/prometheus-operator/prometheus-operator	abcdef1234567890	0.70.0	a:vcs-ref
acm-prometheus	quay.io/img/prom	https://github.com/prometheus-operator/prometheus-operator	abcdef1234567890	0.70.0	a:vcs-ref
orphan-helper	quay.io/img/orphan			1.0.0	z:none
tag-comp-b	quay.io/img/tagb	https://github.com/example/shared		2.1.0	b:version-tag
tag-comp-a	quay.io/img/taga	https://github.com/example/shared		2.1.0	b:version-tag
EOF

cat > "$T/order-b.tsv" <<'EOF'
tag-comp-a	quay.io/img/taga	https://github.com/example/shared		2.1.0	b:version-tag
acm-prometheus	quay.io/img/prom	https://github.com/prometheus-operator/prometheus-operator	abcdef1234567890	0.70.0	a:vcs-ref
orphan-helper	quay.io/img/orphan			1.0.0	z:none
tag-comp-b	quay.io/img/tagb	https://github.com/example/shared		2.1.0	b:version-tag
acm-prometheus-config-reloader	quay.io/img/reloader	https://github.com/prometheus-operator/prometheus-operator	abcdef1234567890	0.70.0	a:vcs-ref
EOF

run_step2 "$T/order-a.tsv" "$T/sorted-a.tsv" "$T/git-a.tsv"
run_step2 "$T/order-b.tsv" "$T/sorted-b.tsv" "$T/git-b.tsv"

if diff -u "$T/git-a.tsv" "$T/git-b.tsv"; then
    pass "two permutations produce identical git.tsv after LC_ALL=C sort"
else
    bad "permuted inputs produced different git.tsv"
fi

prom_tar=$(awk -F'\t' '$1=="acm-prometheus-config-reloader"{print $4}' "$T/git-a.tsv")
if [[ "$prom_tar" == "acm-prometheus-abcdef123456.tar.gz" ]]; then
    pass "shared (repo, ref) picks lexicographically first component name ($prom_tar)"
else
    bad "unexpected shared (repo, ref) tarball name: $prom_tar"
fi

tag_tar=$(awk -F'\t' '$1=="tag-comp-b"{print $4}' "$T/git-a.tsv")
if [[ "$tag_tar" == "tag-comp-a-v2.1.0.tar.gz" ]]; then
    pass "shared (repo, version) picks lexicographically first component name ($tag_tar)"
else
    bad "unexpected shared (repo, version) tarball name: $tag_tar"
fi

nosrc=$(awk -F'\t' '$1=="orphan-helper"{print $4}' "$T/git-a.tsv")
if [[ "$nosrc" == "NO_SOURCE" ]]; then
    pass "NO_SOURCE row preserved"
else
    bad "unexpected NO_SOURCE value: $nosrc"
fi

echo
if (( fail )); then echo "$fail test(s) FAILED"; exit 1; fi
echo "all tests passed"
