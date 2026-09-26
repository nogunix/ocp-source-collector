#!/usr/bin/env python3
"""Phase B / operand sources: label resolver.

Reads `oc image info --output=json` on stdin and prints a single line:

    source_url <TAB> vcs_ref <TAB> version <TAB> method

source_url is always a github.com URL (or empty). vcs_ref is a commit sha
(may be empty when only a tag is available). version is the upstream version
string used for the tag fallback. method records which rule fired (for the
fetch log / git.tsv).

Why a dedicated resolver (vs Phase B's jq one-liner):

  * Layered-product (konflux) images put the *internal* gitlab build repo in
    `org.opencontainers.image.source`, and the real upstream OSS repo in the
    `upstream-vcs-url` / `upstream-vcs-ref` / `upstream-version` labels. So we
    must prefer the upstream-* labels, the opposite of Phase B.
  * Some core components (CNV's virt-*, passt, pr-helper, …) carry a gitlab
    upstream-vcs-url too; they map to a single known upstream repo by name,
    with the tag taken from upstream-version. COMPONENT_MAP encodes that.
  * Some image labels contain raw control characters, which break `jq`.
    json.loads(strict=False) tolerates them.

The component name passed as argv[1] is the image-repo basename with a
trailing -rhelN stripped (e.g. virt-operator, virt-cdi-importer).

argv[2], when given, is the operator package's CSV version (from
00-discover/meta.tsv). It is the last-resort version for a COMPONENT_MAP "tag"
component whose image carries no version label at all -- the apicurio /
Service Registry images are labelled with nothing usable whatsoever, so the
only thing tying them to an upstream tag is the CSV version of the operator
that ships them.
"""
import json
import re
import sys

# Components whose source URL is not in any usable label (the source/url labels
# point at docs.redhat.com / catalog.redhat.com), so we hard-map name -> repo.
# Value is (github "org/name", mode):
#   "tag"             -> fetch by version tag only (downstream commit not on the
#                        public repo); e.g. CNV core, Red Hat Quay.
#   "upstream-commit" -> fetch by the upstream-vcs-ref commit (it IS on github),
#                        tag as fallback; e.g. ODF (red-hat-storage).
#   "commit"          -> fetch by the vcs-ref/revision commit, tag as fallback.
#   "tag-csv"         -> like "tag", but the image carries NO version label at
#                        all, so fall back to the operator's CSV version
#                        (argv[2]). Opt-in per component: for most mapped
#                        components the CSV version is the Red Hat product
#                        version and has nothing to do with the upstream tag.
COMPONENT_MAP = {
    # CNV core: all built from kubevirt/kubevirt; only the upstream tag resolves.
    "virt-api": ("kubevirt/kubevirt", "tag"),
    "virt-controller": ("kubevirt/kubevirt", "tag"),
    "virt-handler": ("kubevirt/kubevirt", "tag"),
    "virt-launcher": ("kubevirt/kubevirt", "tag"),
    "virt-operator": ("kubevirt/kubevirt", "tag"),
    "virt-exportproxy": ("kubevirt/kubevirt", "tag"),
    "virt-exportserver": ("kubevirt/kubevirt", "tag"),
    "virt-synchronization-controller": ("kubevirt/kubevirt", "tag"),
    "pr-helper": ("kubevirt/kubevirt", "tag"),
    "sidecar-shim": ("kubevirt/kubevirt", "tag"),
    "libguestfs-tools": ("kubevirt/kubevirt", "tag"),
    "passt-network-binding-plugin-cni": ("kubevirt/kubevirt", "tag"),
    "passt-network-binding-plugin-sidecar": ("kubevirt/kubevirt", "tag"),
    # virt-artifacts-server is a kubevirt-monorepo build too: its labels carry
    # upstream-vcs-url=gitlab.cee .../downstream/kubevirt +
    # upstream-version=1.6.5-66-g… (same as the virt-* core), so the public tag
    # is the best reachable source (JANUS case 2026-07-11-cnv-downstream-gap).
    "virt-artifacts-server": ("kubevirt/kubevirt", "tag"),
    # kubevirt-ipam-controller: upstream-vcs-ref/-version ARE labeled (and the
    # commit is public -- it equals tag v<upstream-version> on ipam-extensions);
    # only upstream-vcs-url is missing, so rule (a) can't fire. Map the repo.
    "kubevirt-ipam-controller": ("kubevirt/ipam-extensions", "upstream-commit"),
    # NOTE deliberately unmapped: virtio-win. Its only source label is the
    # internal konflux repo (cnv-virtio-win-container-disk) with no upstream-*
    # labels; the public virtio-win org has no verifiable commit linkage.
    # NOTE hostpath-provisioner / hostpath-csi-driver are handled by the
    # sibling-version fixup in phase-b-operand-fetch-source.sh (their labels
    # carry no component version at all -- it must come from the
    # hostpath-provisioner-operator row).
    # ODF umbrella: upstream-vcs-ref commit is present on github.
    "odf-rhel9-operator": ("red-hat-storage/odf-operator", "upstream-commit"),
    "odf-console": ("red-hat-storage/odf-console", "upstream-commit"),
    # Red Hat Quay: only the main quay app maps cleanly to an upstream tag.
    # clair / quay-builder / quay-operator ship RH-internal builds whose
    # versions+commits don't match the public upstream repos -> left NO_SOURCE.
    "quay": ("quay/quay", "tag"),
    # OSC (openshift-sandboxed-containers) & Trustee: every source-ish label
    # points at catalog.redhat.com, but vcs-ref IS a public upstream commit
    # (verified per-image against github for OSC 1.13 / trustee 1.2, 2026-07-16).
    # osc-podvm-payload's vcs-ref lands on cloud-api-adaptor because the podvm
    # image is built from that repo's podvm/ recipes; the guest-components /
    # kata-agent sources inside the payload are pulled separately by
    # phase-b-operand-osc-extras.sh from the fetched CAA tree's versions.yaml.
    # NOTE deliberately unmapped: osc-dm-verity-image (no vcs-ref at all),
    # osc-storage-helper + trustee-rhel9-operator (vcs-ref not public; the
    # trustee *operator* source is collected by Phase B anyway).
    "osc-monitor": ("kata-containers/kata-containers", "commit"),
    "osc-cloud-api-adaptor": ("confidential-containers/cloud-api-adaptor", "commit"),
    "osc-cloud-api-adaptor-webhook": ("confidential-containers/cloud-api-adaptor", "commit"),
    "osc-podvm-builder": ("openshift/sandboxed-containers-operator", "commit"),
    "osc-podvm-payload": ("confidential-containers/cloud-api-adaptor", "commit"),
    "osc-must-gather": ("openshift/sandboxed-containers-operator", "commit"),
    "trustee": ("confidential-containers/trustee", "commit"),
    # NetObserv: same OSC-style situation — vcs-ref IS the public upstream
    # commit (verified against the netobserv org for 4.20/1.12.1), but no
    # usable source-URL label. console-plugin pf4/pf5 variants build from the
    # same console-plugin repo.
    "network-observability-ebpf-agent": ("netobserv/netobserv-ebpf-agent", "commit"),
    "network-observability-flowlogs-pipeline": ("netobserv/flowlogs-pipeline", "commit"),
    "network-observability-console-plugin": ("netobserv/network-observability-console-plugin", "commit"),
    "network-observability-console-plugin-pf4": ("netobserv/network-observability-console-plugin", "commit"),
    "network-observability-console-plugin-pf5": ("netobserv/network-observability-console-plugin", "commit"),
    "network-observability-rhel9-operator": ("netobserv/network-observability-operator", "commit"),
    # KMM: the only source label is konflux-ci/mintmaker (see INFRA_REPOS), and
    # the images version themselves MAJ.MIN ("2.6") while upstream tags MAJ.MIN.0
    # -- candidate_source_urls' ".0 release" rule covers that. rh-ecosystem-edge
    # is the OLD home (tags stop at v2.0.2); kubernetes-sigs carries v2.6.0.
    "kernel-module-management-rhel9-operator": ("kubernetes-sigs/kernel-module-management", "tag"),
    "kernel-module-management-worker": ("kubernetes-sigs/kernel-module-management", "tag"),
    "kernel-module-management-signing": ("kubernetes-sigs/kernel-module-management", "tag"),
    "kernel-module-management-must-gather": ("kubernetes-sigs/kernel-module-management", "tag"),
    # Service Registry (3scale/apicurio): every apicurio image carries no
    # source-ish label at all (z:none for all 3 in 4.18/4.20/4.22). Upstream
    # tags are "<version>.Final", not v<version> -- hence the apicurio branch in
    # candidate_source_urls. CSV version 2.6.13-r4 -> tag 2.6.13.Final (verified).
    "apicurio-registry-rhel8-operator": ("Apicurio/apicurio-registry-operator", "tag-csv"),
    "apicurio-registry-sql": ("Apicurio/apicurio-registry", "tag-csv"),
    "apicurio-registry-kafkasql": ("Apicurio/apicurio-registry", "tag-csv"),
    # --- z:none products mapped 2026-07-30 -------------------------------------
    # 39 of the 146 layered-4.18 products resolved NOTHING (no source-ish label on
    # any of their images). Every entry below was verified by probing the real
    # candidate_source_urls chain with the operator's CSV version: the ones here
    # land on a matching upstream tag, or on a release branch of the matching
    # minor. Products whose ONLY hit was main/master are deliberately left
    # unmapped -- see the exclusion note after this block.
    "kiali": ("kiali/kiali", "tag-csv"),                                  # v2.27.1
    "kiali-rhel9-operator": ("kiali/kiali-operator", "tag-csv"),          # v2.27.1
    "kiali-ossmc": ("kiali/openshift-servicemesh-plugin", "tag-csv"),     # v2.27.1
    # RHBK is Keycloak; the operator lives in the same monorepo. Tags carry no v.
    "keycloak": ("keycloak/keycloak", "tag-csv"),                         # 26.6.4
    "keycloak-rhel9-operator": ("keycloak/keycloak", "tag-csv"),          # 26.6.4
    "devworkspace-rhel9-operator": ("devfile/devworkspace-operator", "tag-csv"),   # v0.42.0
    "devworkspace-project-clone": ("devfile/devworkspace-operator", "tag-csv"),    # same repo
    "web-terminal-rhel9-operator": ("redhat-developer/web-terminal-operator", "tag-csv"),  # v1.13.0
    "web-terminal-exec": ("redhat-developer/web-terminal-exec", "tag-csv"),        # v1.13.0
    "web-terminal-tooling": ("redhat-developer/web-terminal-tooling", "tag-csv"),  # v1.13.0
    "machine-deletion-remediation-rhel9-operator": ("medik8s/machine-deletion-remediation", "tag-csv"),  # v0.5.0
    "deployment-validation-rhel8-operator": ("app-sre/deployment-validation-operator", "tag-csv"),       # 0.7.15
    "costmanagement-metrics-rhel9-operator": ("project-koku/koku-metrics-operator", "tag-csv"),          # v4.4.1
    # OpenShift Update Service is upstream Cincinnati.
    "openshift-update-service": ("openshift/cincinnati", "tag-csv"),                      # v5.0.3
    "openshift-update-service-rhel8-operator": ("openshift/cincinnati-operator", "tag-csv"),  # v5.0.3
    # Repo identity certain, ref approximate: CSV 4.18.4 has no tag, v4.18.0 does.
    "lvms-rhel9-operator": ("openshift/lvm-operator", "tag-csv"),
    # RHCL (Red Hat Connectivity Link) is Kuadrant. CSV 1.3.5 -> v1.3.0.
    # NOTE the sub-components (authorino, limitador, wasm-shim, the console
    # plugin) version independently of the product and only ever hit main --
    # left unmapped on purpose.
    "rhcl-rhel9-operator": ("Kuadrant/kuadrant-operator", "tag-csv"),
    # These four have no matching tag but DO have a release branch of the right
    # minor (release-5.1 for descheduler 5.1.4, release-4.18 for the rest), which
    # candidate_source_urls reaches on its own.
    "descheduler": ("openshift/descheduler", "tag-csv"),
    "kube-descheduler-rhel9-operator": ("openshift/cluster-kube-descheduler-operator", "tag-csv"),
    "run-once-duration-override": ("openshift/run-once-duration-override-operator", "tag-csv"),
    "run-once-duration-override-rhel9-operator": ("openshift/run-once-duration-override-operator", "tag-csv"),
    "secondary-scheduler-rhel9-operator": ("openshift/secondary-scheduler-operator", "tag-csv"),
    "odf-multicluster-orchestrator": ("red-hat-storage/odf-multicluster-orchestrator", "tag-csv"),
    # DELIBERATELY UNMAPPED (probed 2026-07-30, only main/master answered, i.e.
    # the product version and the upstream version are different universes and a
    # branch head would assert a link the evidence does not support):
    #   amq-broker* (7.12.7 vs apache/activemq-artemis), amq-streams/bridge
    #   (3.2.0 vs strimzi 0.4x), apicast* (3scale), mcg-core / mcg-rhel9-operator
    #   (noobaa), quay-bridge-operator + quay-container-security-operator (3.17.3
    #   is the Quay product version), node-maintenance (medik8s 5.5.0),
    #   authorino / limitador / wasm-shim / rhcl-console-plugin (Kuadrant
    #   sub-components). Also unmapped: ubi-minimal / postgresql-* (base images,
    #   no upstream github) and mtr-* / rhsso-* / eap / fuse-* / rhdh (no
    #   verified repo+version pair yet).
}


# Repos that are never a component's upstream source, however they ended up in
# a label. konflux-ci/* is Red Hat's build platform, and mintmaker is its
# dependency-bump bot: all four kernel-module-management images (in 4.18, 4.20
# and 4.22 alike) carry org.opencontainers.image.source=konflux-ci/mintmaker,
# because the build pipeline overwrote the label with its own repo. Rule (c)
# would happily accept that and stage the bot's source tree under the
# operator's name. Rejecting it here lets COMPONENT_MAP (or z:infra-label)
# take over instead.
INFRA_REPOS = {
    "konflux-ci/mintmaker",
    "konflux-ci/konflux-ci",
    "konflux-ci/build-definitions",
    "konflux-ci/release-service-catalog",
}


def is_infra(url: str) -> bool:
    """True for a normalized github URL that is build tooling, not product source."""
    return url.removeprefix("https://github.com/") in INFRA_REPOS if url else False


def gh(url: str) -> str:
    """Normalize a github URL to https://github.com/<org>/<repo> or ''."""
    if not url or "github.com/" not in url:
        return ""
    url = url.strip()
    # strip protocol noise and trailing .git / slash
    tail = url.split("github.com/", 1)[1]
    tail = tail.rstrip("/")
    if tail.endswith(".git"):
        tail = tail[:-4]
    parts = tail.split("/")
    if len(parts) < 2:
        return ""
    return "https://github.com/" + parts[0] + "/" + parts[1]


def parse_version_tag(v: str) -> str:
    """Turn an upstream-version into a bare version string for a v<tag>.

    git-describe forms like '1.6.5-66-gcd86febb88' -> '1.6.5';
    'v1.16.0' -> '1.16.0'; '0.13.0' -> '0.13.0'.

    A value with no digit at all is not a version and returns ''. Images
    label `version` with 'release' (machine-deletion-remediation,
    multicluster-globalhub-*, korrel8r) or '.' (web-terminal-tooling since
    4.19). Passed through, those became the tarball names '...-vrelease' and
    '...-v.', and for tag-csv components they masked the CSV version, so the
    fetch fell back to the main branch head although the v0.5.0 tag exists.
    """
    if not v:
        return ""
    v = v.strip()
    if v.startswith("v"):
        v = v[1:]
    # drop git-describe suffix (-<n>-g<sha>)
    v = v.split("-")[0]
    if not any(ch.isdigit() for ch in v):
        return ""
    return v


def main() -> int:
    component = sys.argv[1] if len(sys.argv) > 1 else ""
    csv_ver = parse_version_tag(sys.argv[2]) if len(sys.argv) > 2 else ""
    raw = sys.stdin.read()
    try:
        info = json.loads(raw, strict=False)
    except Exception:
        # strict=False only tolerates raw control chars; some images (e.g.
        # osc-podvm-payload) carry label values with INVALID backslash escapes
        # (\x etc), which still raise. Double such backslashes and retry.
        try:
            info = json.loads(re.sub(r'\\(?![\\/"bfnrtu])', r'\\\\', raw), strict=False)
        except Exception:
            print("\t\t\tz:noinfo")
            return 0
    labels = (((info or {}).get("config") or {}).get("config") or {}).get("Labels") or {}

    # Some images carry a comma-separated duplicate ref (e.g. "<sha>,<sha>");
    # take the first token.
    def first_ref(s: str) -> str:
        return (s or "").strip().split(",")[0].strip()

    # A rejected infra repo is NOT a silent drop: it changes the final method to
    # z:infra-label so git.tsv / MANIFEST by_method name it.
    infra_seen = False

    up_url = gh(labels.get("upstream-vcs-url", ""))
    if is_infra(up_url):
        infra_seen, up_url = True, ""
    up_ref = first_ref(labels.get("upstream-vcs-ref", ""))
    up_ver = parse_version_tag(labels.get("upstream-version", "") or "")

    # The github source URL lives in different label keys per product:
    #   org.opencontainers.image.source  -> ACM/MCE (stolostron)
    #   source-location                  -> ACS (stackrox)
    #   url                              -> RHOAI (red-hat-data-services)
    # vcs-url / io.openshift.build.source-location appear on some others.
    oci_rev = first_ref(labels.get("org.opencontainers.image.revision", "")
               or labels.get("vcs-ref", "")
               or labels.get("io.openshift.build.commit.id", ""))
    oci_ver = parse_version_tag(labels.get("version", "") or "")
    src_keys = [
        "org.opencontainers.image.source",
        "source-location",
        "io.openshift.build.source-location",
        "vcs-url",
        "url",
    ]
    main_src = ""
    for k in src_keys:
        cand = gh(labels.get(k, ""))
        if is_infra(cand):
            infra_seen = True
            continue          # keep looking; another label may hold the real repo
        if cand:
            main_src = cand
            break

    # Priority:
    # a) upstream github url + upstream commit (konflux upstream-* labels)
    if up_url and up_ref:
        print(f"{up_url}\t{up_ref}\t{up_ver}\ta:upstream-vcs")
        return 0
    # b) known component -> mapped upstream repo (source URL is unusable / absent)
    if component in COMPONENT_MAP:
        repo, mode = COMPONENT_MAP[component]
        ver = up_ver or oci_ver
        if mode == "upstream-commit":
            ref = up_ref or oci_rev
        elif mode == "commit":
            ref = oci_rev or up_ref
        else:  # "tag" / "tag-csv"
            ref = ""
        if mode == "tag-csv":
            ver = ver or csv_ver
        if ref or ver:
            print(f"https://github.com/{repo}\t{ref}\t{ver}\tb:component-map")
            return 0
    # c) github source label (source/source-location/url/...) + commit
    if main_src and oci_rev:
        print(f"{main_src}\t{oci_rev}\t{oci_ver or up_ver}\tc:source-label")
        return 0
    # d) upstream github url with only a version (no commit)
    if up_url and up_ver:
        print(f"{up_url}\t\t{up_ver}\td:upstream-tag")
        return 0
    # e) github source label with only a version
    if main_src and (oci_ver or up_ver):
        print(f"{main_src}\t\t{oci_ver or up_ver}\te:source-tag")
        return 0
    print("\t\t\tz:infra-label" if infra_seen else "\t\t\tz:none")
    return 0


if __name__ == "__main__":
    sys.exit(main())
