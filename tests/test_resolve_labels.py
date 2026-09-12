"""Unit tests for scripts/phase-b-operand-resolve-labels.py pure helpers.

The module file name has dashes, so load it via importlib. Network-free.
Run: pytest tests/test_resolve_labels.py
"""
import importlib.util
import json
import os
import subprocess
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(_HERE, "..", "scripts", "phase-b-operand-resolve-labels.py")
_spec = importlib.util.spec_from_file_location("phase_b_operand_resolve_labels", _SRC)
rl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rl)


@pytest.mark.parametrize("url, expect", [
    ("https://github.com/openshift/cluster-logging-operator", "https://github.com/openshift/cluster-logging-operator"),
    ("https://github.com/a/b.git", "https://github.com/a/b"),
    ("https://github.com/a/b/", "https://github.com/a/b"),
    ("https://github.com/a/b/tree/release-4.18", "https://github.com/a/b"),
    ("https://github.com/a/b/blob/main/go.mod", "https://github.com/a/b"),
    ("quay.io/openshift/foo", ""),
    ("https://www.redhat.com/x", ""),
    ("", ""),
    ("github.com/a", ""),  # too few path parts
])
def test_gh(url, expect):
    assert rl.gh(url) == expect


@pytest.mark.parametrize("v, expect", [
    ("v1.16.0", "1.16.0"),
    ("1.16.0", "1.16.0"),
    ("1.6.5-66-gcd86febb88", "1.6.5"),  # git-describe suffix dropped
    ("v0.13.0", "0.13.0"),
    ("", ""),
])
def test_parse_version_tag(v, expect):
    assert rl.parse_version_tag(v) == expect


# COMPONENT_MAP entries added for JANUS case 2026-07-11-cnv-downstream-gap:
# real 4.20 label shapes observed on registry.redhat.io.
def test_component_map_cnv_gap_entries():
    assert rl.COMPONENT_MAP["kubevirt-ipam-controller"] == ("kubevirt/ipam-extensions", "upstream-commit")
    assert rl.COMPONENT_MAP["virt-artifacts-server"] == ("kubevirt/kubevirt", "tag")
    # deliberately unmapped: no public linkage / handled by the sibling fixup
    assert "virtio-win" not in rl.COMPONENT_MAP
    assert "hostpath-provisioner" not in rl.COMPONENT_MAP
    assert "hostpath-csi-driver" not in rl.COMPONENT_MAP


# --- resolver end-to-end (stdin JSON -> the single output line) -------------

def resolve(labels, component="", csv_ver=None):
    """Run the resolver on a synthetic `oc image info --output=json` payload."""
    payload = json.dumps({"config": {"config": {"Labels": labels}}})
    argv = [sys.executable, _SRC, component] + ([csv_ver] if csv_ver is not None else [])
    out = subprocess.run(argv, input=payload, capture_output=True, text=True, check=True)
    return out.stdout.rstrip("\n").split("\t")


def test_source_label_still_wins_normally():
    src, ref, ver, method = resolve(
        {"org.opencontainers.image.source": "https://github.com/migtools/mig-controller",
         "org.opencontainers.image.revision": "fd13940869e0",
         "version": "1.8.15"},
        component="openshift-migration-controller")
    assert (src, ref, ver, method) == (
        "https://github.com/migtools/mig-controller", "fd13940869e0", "1.8.15", "c:source-label")


def test_infra_repo_label_is_rejected_and_named():
    """All four KMM images label konflux-ci/mintmaker as their source.

    Accepting it would stage konflux's dependency-bump bot under the operator's
    name. COMPONENT_MAP must take over, and the version comes from the image's
    own "2.6" label.
    """
    src, ref, ver, method = resolve(
        {"org.opencontainers.image.source": "https://github.com/konflux-ci/mintmaker",
         "org.opencontainers.image.revision": "b4f8e0176901",
         "version": "2.6"},
        component="kernel-module-management-worker")
    assert src == "https://github.com/kubernetes-sigs/kernel-module-management"
    assert (ref, ver, method) == ("", "2.6", "b:component-map")


def test_infra_repo_with_no_mapping_reports_z_infra_label():
    # Not z:none: the distinction is what tells you a label existed but was junk.
    assert resolve(
        {"org.opencontainers.image.source": "https://github.com/konflux-ci/mintmaker",
         "org.opencontainers.image.revision": "deadbeef"},
        component="some-unmapped-component")[3] == "z:infra-label"


def test_infra_repo_does_not_shadow_a_real_repo_in_a_later_label():
    src, _, _, method = resolve(
        {"org.opencontainers.image.source": "https://github.com/konflux-ci/mintmaker",
         "source-location": "https://github.com/stackrox/stackrox",
         "org.opencontainers.image.revision": "abc1234"},
        component="x")
    assert (src, method) == ("https://github.com/stackrox/stackrox", "c:source-label")


def test_tag_csv_uses_the_operator_csv_version():
    """apicurio images carry no usable label at all -- only the CSV version ties
    them to an upstream tag (2.6.13-r4 -> tag 2.6.13.Final, built by
    candidate_source_urls)."""
    src, ref, ver, method = resolve({}, component="apicurio-registry-sql", csv_ver="2.6.13-r4")
    assert src == "https://github.com/Apicurio/apicurio-registry"
    assert (ref, ver, method) == ("", "2.6.13", "b:component-map")


def test_tag_csv_without_a_csv_version_is_still_z_none():
    assert resolve({}, component="apicurio-registry-sql")[3] == "z:none"


def test_csv_version_is_not_applied_to_plain_tag_components():
    """The CSV version is the Red Hat product version; for "tag" components it
    has nothing to do with the upstream tag, so it must NOT leak in."""
    assert resolve({}, component="quay", csv_ver="3.15.4")[3] == "z:none"


# --- z:none products mapped 2026-07-30 (probed against github) --------------

@pytest.mark.parametrize("component, repo", [
    ("kiali", "kiali/kiali"),
    ("keycloak-rhel9-operator", "keycloak/keycloak"),          # operator in the monorepo
    ("openshift-update-service", "openshift/cincinnati"),      # OSUS is Cincinnati
    ("descheduler", "openshift/descheduler"),
    ("web-terminal-tooling", "redhat-developer/web-terminal-tooling"),
])
def test_z_none_products_now_map_via_csv_version(component, repo):
    src, ref, ver, method = resolve({}, component=component, csv_ver="9.9.9")
    assert src == f"https://github.com/{repo}"
    assert (ref, ver, method) == ("", "9.9.9", "b:component-map")


@pytest.mark.parametrize("component", [
    # Product version and upstream version are different universes here, so a
    # mapping would only ever reach a branch head. Keep them NO_SOURCE.
    "amq-broker", "amq-broker-rhel8-operator", "strimzi-rhel9-operator",
    "apicast-gateway", "mcg-core", "quay-bridge-operator",
    "quay-container-security-operator", "authorino", "limitador", "wasm-shim",
    # base images with no upstream repo at all
    "ubi-minimal", "postgresql-15",
])
def test_deliberately_unmapped_products_stay_unmapped(component):
    assert component not in rl.COMPONENT_MAP
