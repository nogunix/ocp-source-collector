#!/bin/bash
# Tests for index_extract() in scripts/lib.sh: an operator-index extraction is
# reused only while the index digest is unchanged. Before it existed, a
# populated configs/ was reused forever and b/b-operand rebuilt a July catalog
# for three months. Network-free: `oc` is a shell function stub.
#
# Run: bash tests/test_index_extract.sh   (exit 0 = pass)
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# shellcheck source=../scripts/lib.sh
source "$HERE/../scripts/lib.sh"
set +e +o pipefail
log() { :; }

fail=0
pass() { printf '  ok   %s\n' "$1"; }
bad()  { printf '  FAIL %s\n' "$1"; fail=$((fail+1)); }
eq() {
    if [[ "$2" == "$3" ]]; then pass "$1"; else
        bad "$1"; printf '         expected: %s\n         actual:   %s\n' "$2" "$3"
    fi
}

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
AUTHFILE=/dev/null

# Stub: `oc image info` answers $FAKE_DIGEST (empty = unreachable);
# `oc image extract --path SRC:DST IMG` writes IMG into DST/catalog.json.
FAKE_DIGEST=""
EXTRACTS=0
oc() {
    case "$1 $2" in
        "image info")
            [[ -n "$FAKE_DIGEST" ]] && printf '{"digest":"%s"}\n' "$FAKE_DIGEST"
            ;;
        "image extract")
            local a dst img
            for a in "$@"; do
                case "$a" in *:/*) dst="${a#*:}" ;; esac
                img="$a"
            done
            EXTRACTS=$((EXTRACTS+1))
            printf '%s\n' "$img" > "$dst/catalog.json"
            ;;
    esac
}

D="$TMP/configs"
IDX=example.io/idx:v4.20

# 1. first run extracts from the pinned digest and stamps it
FAKE_DIGEST=sha256:aaa
index_extract "$IDX" /configs/ "$D" linux/amd64
eq "first run extracts" 1 "$EXTRACTS"
eq "extracts by digest" "$IDX@sha256:aaa" "$(cat "$D/catalog.json")"
eq "stamp written" sha256:aaa "$(cat "$D.digest")"
eq "INDEX_DIGEST set" sha256:aaa "$INDEX_DIGEST"

# 2. same digest: reused
index_extract "$IDX" /configs/ "$D" linux/amd64
eq "same digest reuses" 1 "$EXTRACTS"

# 3. digest moved: wiped and re-extracted (stale files must not survive)
touch "$D/removed-upstream.json"
FAKE_DIGEST=sha256:bbb
index_extract "$IDX" /configs/ "$D" linux/amd64
eq "moved digest re-extracts" 2 "$EXTRACTS"
eq "re-extract uses new digest" "$IDX@sha256:bbb" "$(cat "$D/catalog.json")"
[[ ! -e "$D/removed-upstream.json" ]] && pass "old files wiped" || bad "old files wiped"

# 4. populated dir without a stamp (pre-fix state) is treated as stale
rm -f "$D.digest"
index_extract "$IDX" /configs/ "$D" linux/amd64
eq "unstamped dir re-extracts" 3 "$EXTRACTS"

# 5. registry unreachable: existing extraction reused, empty one is fatal
FAKE_DIGEST=""
index_extract "$IDX" /configs/ "$D" linux/amd64
eq "unreachable reuses existing" 3 "$EXTRACTS"
( index_extract "$IDX" /configs/ "$TMP/empty" linux/amd64 ) 2>/dev/null
[[ $? -ne 0 ]] && pass "unreachable + empty dies" || bad "unreachable + empty dies"

# 6. seed with matching stamp is copied, not pulled
SEED="$TMP/phase-b/configs"
mkdir -p "$SEED/pkg"
echo seeded > "$SEED/pkg/catalog.json"
echo sha256:ccc > "$SEED.digest"
FAKE_DIGEST=sha256:ccc
P="$TMP/pkg-config"
index_extract "$IDX" /configs/pkg/ "$P" linux/amd64 "$SEED/pkg" "$SEED.digest"
eq "matching seed copied" seeded "$(cat "$P/catalog.json")"
eq "matching seed: no pull" 3 "$EXTRACTS"
eq "seeded dir stamped" sha256:ccc "$(cat "$P.digest")"

# 7. seed with a stale stamp is ignored
FAKE_DIGEST=sha256:ddd
index_extract "$IDX" /configs/pkg/ "$P" linux/amd64 "$SEED/pkg" "$SEED.digest"
eq "stale seed: pulled instead" 4 "$EXTRACTS"
eq "stale seed: not copied" "$IDX@sha256:ddd" "$(cat "$P/catalog.json")"

(( fail == 0 )) && echo "all index_extract tests passed" || echo "$fail failure(s)"
exit $(( fail > 0 ))
