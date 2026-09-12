"""Tests for the submodule-gap report's join logic.

The report's value is the `elsewhere` column -- telling a reader which casket
does carry the code behind an empty submodule dir. Picking the wrong one sends
them to a copy at the wrong version, which is worse than an empty answer.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "report_submodule_gaps",
    os.path.join(os.path.dirname(__file__), "..", "scripts", "report-submodule-gaps.py"))
rsg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rsg)


def test_unit_minor_reads_every_mount_shape():
    assert rsg.unit_minor("sources-ocp4.22-community-operators") == "4.22"
    assert rsg.unit_minor("sources-layered-ocp4.18") == "4.18"
    assert rsg.unit_minor("sources-ocp4.20.33") == "4.20"      # payload patch mount
    assert rsg.unit_minor("sources-rhel9") == ""


def _idx(*hits):
    index = {}
    for minor, mount, alias in hits:
        index.setdefault(alias.lower(), []).append((minor, mount, alias))
    return index


def test_same_minor_wins_over_another_minor():
    index = _idx(("4.18", "sources-ocp4.18.52", "kubernetes"),
                 ("4.22", "sources-ocp4.22.9", "kubernetes"))
    assert rsg.pick_elsewhere("openshift/kubernetes", "4.22", index) == \
        "sources-ocp4.22.9:by-repo/kubernetes"


def test_payload_mount_wins_over_a_catalog_at_the_same_minor():
    """The payload copy is pinned to the release's own commit; a catalog copy
    is whatever that operator happened to vendor."""
    index = _idx(("4.22", "sources-ocp4.22-community-operators", "kubernetes"),
                 ("4.22", "sources-ocp4.22.9", "kubernetes"))
    assert rsg.pick_elsewhere("openshift/kubernetes", "4.22", index) == \
        "sources-ocp4.22.9:by-repo/kubernetes"


def test_repo_collected_nowhere_returns_empty():
    assert rsg.pick_elsewhere("asmacdo/docsy", "4.22", _idx()) == ""


def test_non_github_submodule_has_no_slug_and_no_answer():
    index = _idx(("4.22", "sources-ocp4.22.9", "aws-kube-ci"))
    assert rsg.pick_elsewhere("", "4.22", index) == ""


def test_match_is_on_the_repo_basename_not_the_owner():
    """by-repo/ entries are basenames, so a fork under another owner still
    resolves -- openshift/spiffe-spire is found under `spiffe-spire`."""
    index = _idx(("4.22", "sources-ocp4.22-operators", "spiffe-spire"))
    assert rsg.pick_elsewhere("spiffe/spire", "4.22", index) == ""
    assert rsg.pick_elsewhere("openshift/spiffe-spire", "4.22", index) == \
        "sources-ocp4.22-operators:by-repo/spiffe-spire"
