#!/bin/bash
# Export the union of upstream source coordinates recorded in every mounted
# casket's git/INDEX.tsv into state/upstream-sources.tsv — the manifest that
# scripts/upstream-link-check.sh (upstream-link-check.timer) probes weekly.
#
# Columns: repo | ref | version | kind | seen_in
#   ref     commit sha or branch as recorded (may be empty)
#   version bare version as recorded (may be empty) — the tag-fallback input
#   kind    head  (dir was fetched from the repo default branch: *-head)
#           pin   (ref present)  /  tag  (version only)
# The checker mirrors the fetch pipelines' fallback chain (sha -> v<ver> ->
# <ver> -> HEAD), so "bad" means "not re-fetchable by the pipeline today".
#
# state/ is host-local and not committed to the public repo, so the check runs
# here, regenerating this file each time. Content already fetched lives safely
# inside caskets — this is early-warning monitoring of RE-fetchability
# (docs/collection-model.md §8).
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT="$SCRIPT_DIR/../state/upstream-sources.tsv"

{
    echo -e "# repo\tref\tversion\tkind\tseen_in"
    for idx in /srv/sources-*/git/INDEX.tsv /srv/sources-*/*/git/INDEX.tsv; do
        [[ -f "$idx" ]] || continue
        label=${idx#/srv/}; label=${label%/git/INDEX.tsv}
        awk -F'\t' -v L="$label" 'NR>1 && $2 ~ /^https:\/\/github\.com\// {
            repo=$2; sub(/\/+$/, "", repo)              # trailing-slash noise
            ref=$3; ver=$4
            kind = ($1 ~ /-head$/) ? "head" : (ref != "" ? "pin" : "tag")
            if (ref=="" && ver=="" && kind!="head") next
            print repo"\t"ref"\t"ver"\t"kind"\t"L
        }' "$idx"
    done | sort -u | awk -F'\t' '!seen[$1"\t"$2"\t"$3"\t"$4]++'
} > "$OUT.tmp"

# No caskets mounted (or every INDEX.tsv unreadable) yields a header-only file.
# Replacing a good manifest with that would make the checker report "0 rows,
# all ok" -- a silent pass -- so refuse and keep the previous manifest.
if ! grep -qv '^#' "$OUT.tmp"; then
    rm -f "$OUT.tmp"
    echo "no GitHub rows found under /srv/sources-*/ (caskets not mounted?); keeping $OUT" >&2
    exit 1
fi
mv "$OUT.tmp" "$OUT"

n=$(grep -vc '^#' "$OUT"); r=$(grep -v '^#' "$OUT" | cut -f1 | sort -u | wc -l)
echo "wrote $OUT: $n rows across $r repos"
