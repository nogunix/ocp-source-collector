#!/bin/bash
# Freshness-fingerprint helpers shared by casket-check.sh and casket-build.sh.
# Source lib.sh first (needs CONFIG_DIR, die). Pure query functions -- no
# registry access, no side effects beyond a per-process digest cache.
#
# See casket-check.sh's header comment for why each phase uses the signal it
# does.

declare -A CATALOG_DIGEST_CACHE=()   # minor -> digest; shared by phase b and b-operand

: "${UPGRADE_GRAPH_URL:=https://api.openshift.com/api/upgrades_info/v1/graph}"

latest_patch_in_graph() {
    # stdin: update-graph JSON for one channel. Prints the highest GA patch of
    # minor $1, or nothing. A stable-<minor> channel also lists the previous
    # minor's patches (they are upgrade sources), hence the prefix filter;
    # pre-releases (5.0.0-rc.1) never count. Versions compare numerically, so
    # 4.22.22 beats 4.22.9.
    jq -r --arg p "$1." '
        [.nodes[]?.version | strings | select(startswith($p) and (test("^[0-9]+\\.[0-9]+\\.[0-9]+$")))]
        | max_by(split(".") | map(tonumber)) // empty' 2>/dev/null
}

fetch_patch() {
    # current latest patch for a minor's stable channel; prints nothing and
    # returns non-zero when no source answers.
    #
    # The update graph (the channel itself) is the source of truth. The mirror's
    # stable-<minor>/release.txt is only a fallback: it is a pointer derived
    # from the channel and has gone stale before -- on 2026-11-08 it still read
    # 4.22.18 (dated 2026-10-08) while stable-4.22 already carried 4.22.22, and
    # it has no stable-5.0 at all, so a mirror-only check silently stopped
    # seeing new 4.20+ patches and never saw OCP 5.
    #
    # NB: no `exit` in the awk script -- release.txt has exactly one Version
    # line, and awk exiting early would close the pipe while curl is still
    # writing, making curl exit 23 (SIGPIPE) and take the whole pipeline down
    # under `set -o pipefail` even though the value was captured correctly.
    local minor="$1" v
    v=$(curl -fsSL --max-time 10 -H 'Accept: application/json' \
            "${UPGRADE_GRAPH_URL}?channel=stable-${minor}&arch=amd64" 2>/dev/null \
        | latest_patch_in_graph "$minor") || v=""
    if [[ -n "$v" ]]; then
        printf '%s\n' "$v"
        return 0
    fi
    curl -fsSL --max-time 10 "https://mirror.openshift.com/pub/openshift-v4/clients/ocp/stable-${minor}/release.txt" 2>/dev/null \
        | awk '/^ *Version:/ {print $2}'
}

fetch_catalog_digest() {
    # manifest digest of the redhat-operator-index tag for a minor (phase b
    # and b-operand both read this same index), cached per process, empty on failure
    local minor="$1"
    if [[ -z "${CATALOG_DIGEST_CACHE[$minor]:-}" ]]; then
        local d
        d=$(oc image info "registry.redhat.io/redhat/redhat-operator-index:v${minor}" -o json --filter-by-os=linux/amd64 2>/dev/null | jq -r '.digest // empty')
        CATALOG_DIGEST_CACHE[$minor]="${d:-__unreachable__}"
    fi
    [[ "${CATALOG_DIGEST_CACHE[$minor]}" == "__unreachable__" ]] && return 0
    printf '%s' "${CATALOG_DIGEST_CACHE[$minor]}"
}

read_units() {
    # non-comment, non-blank lines of a config file
    grep -vE '^\s*(#|$)' "$1"
}

phase_a_rpm_fingerprint() {
    # sha256 of "<minor>:<patch>" pairs across config/phase-a-rpm-minors.txt --
    # a proxy for "the rhel-coreos image probably moved too" (see header).
    local pairs=()
    while IFS= read -r minor; do
        pairs+=("${minor}:$(fetch_patch "$minor")")
    done < <(read_units "$CONFIG_DIR/phase-a-rpm-minors.txt")
    printf '%s\n' "${pairs[@]}" | sort | sha256sum | awk '{print $1}'
}
