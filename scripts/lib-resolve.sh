#!/bin/bash
# Pure source-resolution helpers shared by Phase B resolve-v2 and the test suite.
# No side effects on source: only function definitions. Network-free.

# normalize_github <url> -> https://github.com/<org>/<repo>  (or empty)
# Mirrors the inline normalizer in phase-b-resolve-v2.sh step 1.
normalize_github() {
    local url="${1:-}"
    url="${url%/}"
    [[ -z "$url" ]] && { printf ''; return; }
    [[ "$url" == git@github.com:* ]] && url="https://github.com/${url#git@github.com:}"
    [[ "$url" == github.com/* ]] && url="https://$url"
    [[ "$url" != *github.com* ]] && { printf ''; return; }
    [[ "$url" == *.git ]] && url="${url%.git}"
    # strip /tree/... or /blob/... suffix
    url="${url%%/tree/*}"
    url="${url%%/blob/*}"
    printf '%s' "$url"
}

# candidate_source_urls <src> <ref> <ver> <minor>
# Emit, one per line in priority order, the github archive URLs to try for an
# operator/operand source. Encapsulates the dl_one fallback chain so it can be
# unit-tested without network:
#   1. exact commit  (archive/<ref>)            when ref is non-empty
#   2. per-repo family strategy                  (gitops/istio/serverless/compliance)
#   3. version tags  (v<ver>, <ver>, base forms, the .0 release of that MAJ.MIN)
#   4. branches      (release-<product-minor>, release-<ocp-minor>, main, master)
# ver is the product version (e.g. 1.37.1 or v1.20.4); minor is the OCP minor.
#
# Only candidate 1 is the source the image was actually built from. Everything
# after it is an approximation and callers must record it as such -- see
# phase-b-operand-fetch-source.sh, which writes fetched.tsv with exact=0 for
# any tag/branch hit.
candidate_source_urls() {
    local src="$1" ref="$2" ver="$3" minor="$4"
    local base="${src%/}"

    # strip leading v's so we never emit vv; ver_minor = MAJOR.MINOR
    local ver_clean="$ver"
    while [[ "${ver_clean:0:1}" == "v" ]]; do ver_clean="${ver_clean:1}"; done
    local ver_base="${ver_clean%%-*}"
    local ver_minor="${ver_clean%.*}"

    [[ -n "$ref" ]] && printf '%s\n' "$base/archive/${ref}.tar.gz"

    case "$src" in
        *github.com/redhat-developer/gitops-operator*)
            [[ -n "$ver_minor" ]] && printf '%s\n' "$base/archive/refs/tags/v${ver_minor}.0.tar.gz" ;;
        *github.com/maistra/istio-operator*)
            # CSV version carries a build suffix (2.6.16-0); maistra tags use the
            # base version: maistra-2.6.16-dev / maistra-2.6.16.
            if [[ -n "$ver_base" ]]; then
                printf '%s\n' "$base/archive/refs/tags/maistra-${ver_base}-dev.tar.gz"
                printf '%s\n' "$base/archive/refs/tags/maistra-${ver_base}.tar.gz"
            fi ;;
        *github.com/openshift-knative/serverless-operator*)
            [[ -n "$ver_minor" ]] && printf '%s\n' "$base/archive/refs/heads/release-${ver_minor}.tar.gz" ;;
        *github.com/Apicurio/apicurio-registry*)
            # Apicurio tags releases "<version>.Final" -- no v prefix, and the
            # generic candidates would never find it. Verified: CSV version
            # 2.6.13-r4 -> tag 2.6.13.Final (HTTP 200, 2026-07-30).
            [[ -n "$ver_base" ]] && printf '%s\n' "$base/archive/refs/tags/${ver_base}.Final.tar.gz" ;;
        *github.com/ComplianceAsCode/compliance-operator*)
            if [[ -n "$ver_clean" ]]; then
                printf '%s\n' "$base/archive/refs/tags/v${ver_clean}.tar.gz"
                [[ -n "$ver_minor" ]] && printf '%s\n' "$base/archive/refs/heads/${ver_minor}.tar.gz"
            fi ;;
    esac

    if [[ -n "$ver_clean" ]]; then
        printf '%s\n' "$base/archive/refs/tags/v${ver_clean}.tar.gz"
        printf '%s\n' "$base/archive/refs/tags/${ver_clean}.tar.gz"
        if [[ "$ver_base" != "$ver_clean" ]]; then
            printf '%s\n' "$base/archive/refs/tags/v${ver_base}.tar.gz"
            printf '%s\n' "$base/archive/refs/tags/${ver_base}.tar.gz"
        fi
        # The ".0 release" of that MAJOR.MINOR. Two real shapes land here:
        # a product versioned MAJ.MIN against a repo that tags MAJ.MIN.0
        # (kernel-module-management 2.6 -> kubernetes-sigs v2.6.0), and a
        # downstream z-bump the public repo never tagged (opentelemetry
        # 0.152.1 -> v0.152.0). Both measured on layered-4.18, 2026-07-29.
        local ver_zero=""
        case "$ver_clean" in
            *.*.*) ver_zero="${ver_minor}.0" ;;
            *.*)   ver_zero="${ver_clean}.0" ;;
        esac
        if [[ -n "$ver_zero" && "$ver_zero" != "$ver_clean" ]]; then
            printf '%s\n' "$base/archive/refs/tags/v${ver_zero}.tar.gz"
            printf '%s\n' "$base/archive/refs/tags/${ver_zero}.tar.gz"
        fi
    fi
    # release-<product minor> before release-<ocp minor>: a layered product's
    # branches track its own version (MTC 1.8.15 -> migtools release-1.8), and
    # only OCP components branch on the OCP minor.
    # (MAJ.MIN.PATCH only -- a bare "2.6" would otherwise ask for release-2.)
    case "$ver_clean" in
        *.*.*) printf '%s\n' "$base/archive/refs/heads/release-${ver_minor}.tar.gz" ;;
        *.*)   printf '%s\n' "$base/archive/refs/heads/release-${ver_clean}.tar.gz" ;;
    esac
    printf '%s\n' "$base/archive/refs/heads/release-${minor}.tar.gz"
    printf '%s\n' "$base/archive/refs/heads/main.tar.gz"
    printf '%s\n' "$base/archive/refs/heads/master.tar.gz"
}
