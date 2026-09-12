"""Tests for mcp/backends.py — _classify(), safe_path(), and the
INDEX.tsv-driven resolve/list operations.

Network-free: uses monkeypatched paths and tmp_path fixtures.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "mcp"))
import backends as be  # noqa: E402


# --------------------------------------------------------------- _classify
def test_classify_phase_a():
    assert be._classify("ocp4.18.46") == ("a", "4.18.46")
    assert be._classify("ocp4.20.22") == ("a", "4.20.22")


def test_classify_phase_b():
    assert be._classify("ocp4.18-operators") == ("b", "4.18")
    assert be._classify("ocp4.20-operators") == ("b", "4.20")


def test_classify_phase_b_certified():
    assert be._classify("ocp4.18-certified-operators") == ("b-certified", "4.18")


def test_classify_phase_b_community():
    assert be._classify("ocp4.20-community-operators") == ("b-community", "4.20")


def test_classify_b_operand():
    assert be._classify("layered-ocp4.18") == ("b-operand", "4.18")
    assert be._classify("layered-ocp4.22") == ("b-operand", "4.22")


def test_classify_a_rpm():
    assert be._classify("ocp-srpms") == ("a-rpm", "")


def test_classify_other():
    assert be._classify("unknown-thing") == ("other", "")
    assert be._classify("") == ("other", "")


# --------------------------------------------------------------- safe_path
def test_safe_path_accepts_casket_mount(tmp_path, monkeypatch):
    monkeypatch.setattr(be, "SRV", str(tmp_path))
    monkeypatch.setattr(be, "ROOT_PREFIX", str(tmp_path) + "/sources-")
    mount = tmp_path / "sources-ocp4.20.22"
    mount.mkdir()
    f = mount / "git" / "somefile.go"
    f.parent.mkdir(parents=True)
    f.write_text("package main")
    assert be.safe_path(str(f)) == str(f.resolve())


def test_safe_path_rejects_outside_path(monkeypatch):
    monkeypatch.setattr(be, "SRV", "/srv")
    monkeypatch.setattr(be, "ROOT_PREFIX", "/srv/sources-")
    import pytest
    with pytest.raises(ValueError, match="outside casket roots"):
        be.safe_path("/etc/passwd")


def test_safe_path_accepts_srv_itself(tmp_path, monkeypatch):
    monkeypatch.setattr(be, "SRV", str(tmp_path))
    monkeypatch.setattr(be, "ROOT_PREFIX", str(tmp_path) + "/sources-")
    assert be.safe_path(str(tmp_path)) == str(tmp_path.resolve())


def test_safe_path_rejects_traversal(tmp_path, monkeypatch):
    monkeypatch.setattr(be, "SRV", str(tmp_path))
    monkeypatch.setattr(be, "ROOT_PREFIX", str(tmp_path) + "/sources-")
    mount = tmp_path / "sources-ocp4.20"
    mount.mkdir()
    import pytest
    with pytest.raises(ValueError):
        be.safe_path(str(mount / ".." / ".." / "etc" / "passwd"))


# ------------------------------------------------- resolve_repo / resolve_component / list_components
def _make_mount(tmp_path, monkeypatch, suffix="ocp4.20.22"):
    srv = tmp_path / "srv"
    srv.mkdir()
    mount = srv / f"sources-{suffix}"
    gitdir = mount / "git"
    gitdir.mkdir(parents=True)

    idx_content = "dir\trepo\tref\tversion\tcomponents\n"
    idx_content += "cvo-abc123\thttps://github.com/openshift/cvo\tabc123\t4.20.22\tcluster-version-operator\n"
    idx_content += "kubevirt-def\thttps://github.com/kubevirt/kubevirt\tdef456\t4.20.22\tkubevirt\n"
    (gitdir / "INDEX.tsv").write_text(idx_content)

    bycomp = mount / "by-component"
    bycomp.mkdir()
    os.symlink("../git/cvo-abc123", str(bycomp / "cluster-version-operator"))
    os.symlink("../git/kubevirt-def", str(bycomp / "kubevirt"))

    byrepo = mount / "by-repo"
    byrepo.mkdir()
    os.symlink("../git/cvo-abc123", str(byrepo / "cvo"))
    os.symlink("../git/kubevirt-def", str(byrepo / "kubevirt"))

    (gitdir / "cvo-abc123").mkdir()
    (gitdir / "kubevirt-def").mkdir()

    monkeypatch.setattr(be, "SRV", str(srv))
    monkeypatch.setattr(be, "ROOT_PREFIX", str(srv) + "/sources-")
    return mount


def test_resolve_repo_finds_by_basename(tmp_path, monkeypatch):
    _make_mount(tmp_path, monkeypatch)
    hits = be.resolve_repo("kubevirt/kubevirt", "4.20")
    assert any(h["repo_alias"] == "kubevirt" for h in hits)


def test_resolve_repo_full_url(tmp_path, monkeypatch):
    _make_mount(tmp_path, monkeypatch)
    hits = be.resolve_repo("https://github.com/kubevirt/kubevirt.git", "4.20")
    assert len(hits) >= 1


def test_resolve_repo_no_match(tmp_path, monkeypatch):
    _make_mount(tmp_path, monkeypatch)
    hits = be.resolve_repo("nonexistent/repo", "4.20")
    assert hits == []


def test_resolve_component_substring_match(tmp_path, monkeypatch):
    _make_mount(tmp_path, monkeypatch)
    hits = be.resolve_component("cluster-version", "4.20")
    assert any(h["component"] == "cluster-version-operator" for h in hits)


def test_resolve_component_no_match(tmp_path, monkeypatch):
    _make_mount(tmp_path, monkeypatch)
    hits = be.resolve_component("zzz-nonexistent", "4.20")
    assert hits == []


def test_list_components_returns_all(tmp_path, monkeypatch):
    _make_mount(tmp_path, monkeypatch)
    items = be.list_components("4.20")
    assert len(items) == 2
    dirs = {it["dir"] for it in items}
    assert dirs == {"cvo-abc123", "kubevirt-def"}


def test_list_components_phase_filter(tmp_path, monkeypatch):
    _make_mount(tmp_path, monkeypatch)
    items = be.list_components("4.20", phase="a")
    assert len(items) == 2
    items_b = be.list_components("4.20", phase="b")
    assert items_b == []


# ------------------------------------------------- b-operand multi-unit layout
def test_b_operand_multi_unit(tmp_path, monkeypatch):
    srv = tmp_path / "srv"
    srv.mkdir()
    mount = srv / "sources-layered-ocp4.20"
    for product in ("cnv", "acs"):
        gitdir = mount / product / "git"
        gitdir.mkdir(parents=True)
        idx = "dir\trepo\tref\tversion\tcomponents\n"
        idx += f"op-{product}\thttps://github.com/org/{product}\tabc\t1.0\t{product}-operator\n"
        (gitdir / "INDEX.tsv").write_text(idx)

    monkeypatch.setattr(be, "SRV", str(srv))
    monkeypatch.setattr(be, "ROOT_PREFIX", str(srv) + "/sources-")

    items = be.list_components("4.20", phase="b-operand")
    assert len(items) == 2
    products = {it["product"] for it in items}
    assert products == {"cnv", "acs"}
