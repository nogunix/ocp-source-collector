#!/bin/bash
# Regression tests for scripts/lib-resolve.sh — guards the three resolution
# bugs fixed in the "rescue uncollected operators" change. Network-free.
#
# Run: tests/test_resolve.sh   (exit 0 = pass)
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../scripts/lib-resolve.sh
source "$HERE/../scripts/lib-resolve.sh"

fail=0
pass() { printf '  ok   %s\n' "$1"; }
bad()  { printf '  FAIL %s\n' "$1"; fail=$((fail+1)); }

# assert that `candidate_source_urls <args>` emits a line exactly equal to $expect
emits() {  # <desc> <expect-url> -- <src> <ref> <ver> <minor>
    local desc="$1" expect="$2"; shift 3
    if candidate_source_urls "$@" | grep -qxF "$expect"; then pass "$desc"; else
        bad "$desc"; printf '       expected line: %s\n' "$expect"
        printf '       got:\n'; candidate_source_urls "$@" | sed 's/^/         /'
    fi
}
# assert a literal string equality
eq() { local desc="$1" got="$2" want="$3"; [[ "$got" == "$want" ]] && pass "$desc" || { bad "$desc"; printf '       got=[%s] want=[%s]\n' "$got" "$want"; }; }

echo "# bug1/2 — per-family ref strategy (product version -> repo tag/branch)"
emits "gitops: v<MAJ.MIN>.0 (patch stripped)" \
  "https://github.com/redhat-developer/gitops-operator/archive/refs/tags/v1.20.0.tar.gz" \
  -- https://github.com/redhat-developer/gitops-operator "" v1.20.4 4.18
emits "istio: maistra-<ver>-dev" \
  "https://github.com/maistra/istio-operator/archive/refs/tags/maistra-2.6.16-dev.tar.gz" \
  -- https://github.com/maistra/istio-operator "" 2.6.16-0 4.18
emits "serverless: release-<product-minor> (NOT ocp minor)" \
  "https://github.com/openshift-knative/serverless-operator/archive/refs/heads/release-1.37.tar.gz" \
  -- https://github.com/openshift-knative/serverless-operator "" 1.37.1 4.18
emits "compliance: MAJOR.MINOR branch" \
  "https://github.com/ComplianceAsCode/compliance-operator/archive/refs/heads/1.9.tar.gz" \
  -- https://github.com/ComplianceAsCode/compliance-operator "" 1.9.0 4.18

echo "# bug3 — empty ref must NOT emit an archive/<empty> URL or shift anything"
got_first="$(candidate_source_urls https://github.com/openshift-knative/serverless-operator "" 1.37.1 4.18 | head -1)"
[[ "$got_first" == *"/archive/.tar.gz" ]] && bad "empty-ref emitted archive/.tar.gz" || pass "empty-ref skips exact-sha URL"
# with a ref present, the exact-sha URL is first
emits "non-empty ref -> exact archive/<sha> first" \
  "https://github.com/openshift/cluster-logging-operator/archive/d699c8.tar.gz" \
  -- https://github.com/openshift/cluster-logging-operator d699c8 6.1.0 4.18

echo "# generic fallbacks always present"
emits "release-<ocp-minor> branch" \
  "https://github.com/openshift/dpu-operator/archive/refs/heads/release-4.18.tar.gz" \
  -- https://github.com/openshift/dpu-operator "" "" 4.18
emits "main fallback" \
  "https://github.com/openshift/dpu-operator/archive/refs/heads/main.tar.gz" \
  -- https://github.com/openshift/dpu-operator "" "" 4.18

echo "# version normalization (no vv, post-release suffix base form)"
emits "v-prefixed version not doubled" \
  "https://github.com/x/y/archive/refs/tags/v0.13.3.tar.gz" \
  -- https://github.com/x/y "" v0.13.3 4.18
emits "post-release suffix -> base tag tried" \
  "https://github.com/x/y/archive/refs/tags/v0.13.3.tar.gz" \
  -- https://github.com/x/y "" 0.13.3-2 4.18

echo "# b-operand rescues (measured on layered-4.18, 2026-07-29)"
# opentelemetry-product ships 0.152.1; upstream only ever tagged v0.152.0.
emits "downstream z-bump -> the .0 release of that MAJ.MIN" \
  "https://github.com/open-telemetry/opentelemetry-operator/archive/refs/tags/v0.152.0.tar.gz" \
  -- https://github.com/open-telemetry/opentelemetry-operator "" 0.152.1 4.18
# KMM versions itself "2.6"; kubernetes-sigs tags v2.6.0.
emits "MAJ.MIN product version -> v<MAJ.MIN>.0 tag" \
  "https://github.com/kubernetes-sigs/kernel-module-management/archive/refs/tags/v2.6.0.tar.gz" \
  -- https://github.com/kubernetes-sigs/kernel-module-management "" 2.6 4.18
# A bare MAJ.MIN must not ask for release-2 (ver_minor of "2.6" is "2").
got="$(candidate_source_urls https://github.com/x/y "" 2.6 4.18)"
grep -q '/heads/release-2\.tar\.gz' <<<"$got" \
  && bad "MAJ.MIN must not emit release-<MAJOR>" || pass "MAJ.MIN does not emit release-<MAJOR>"
grep -q '/heads/release-2\.6\.tar\.gz' <<<"$got" \
  && pass "MAJ.MIN emits release-<MAJ.MIN>" || bad "MAJ.MIN emits release-<MAJ.MIN>"
# MTC 1.8.15: migtools branches on the product minor, not the OCP minor.
emits "product-minor release branch (MTC 1.8.15 -> release-1.8)" \
  "https://github.com/migtools/mig-operator/archive/refs/heads/release-1.8.tar.gz" \
  -- https://github.com/migtools/mig-operator "" 1.8.15 4.18
# Apicurio tags "<ver>.Final" -- nothing generic would ever find it.
emits "apicurio: <ver>.Final tag" \
  "https://github.com/Apicurio/apicurio-registry/archive/refs/tags/2.6.13.Final.tar.gz" \
  -- https://github.com/Apicurio/apicurio-registry "" 2.6.13-r4 4.18

echo "# the exact commit stays first — approximations must never outrank it"
eq "exact sha is candidate #1" \
  "$(candidate_source_urls https://github.com/migtools/mig-operator 1d63ee93 1.8.15 4.18 | head -1)" \
  "https://github.com/migtools/mig-operator/archive/1d63ee93.tar.gz"

echo "# normalize_github"
eq "scp-style"      "$(normalize_github 'git@github.com:openshift/foo.git')" "https://github.com/openshift/foo"
eq "bare host"      "$(normalize_github 'github.com/a/b')"                   "https://github.com/a/b"
eq "/tree/ stripped" "$(normalize_github 'https://github.com/a/b/tree/main')" "https://github.com/a/b"
eq "non-github ->''" "$(normalize_github 'https://www.redhat.com/x')"        ""
eq "empty ->''"     "$(normalize_github '')"                                 ""

echo
if [[ $fail -eq 0 ]]; then echo "ALL PASS"; exit 0; else echo "$fail FAILED"; exit 1; fi
