"""Tests for the permalink backend function.

Network-free: builds a minimal casket filesystem in tmp_path with INDEX.tsv
and SUBMODULES.tsv, then exercises the resolution logic.
Run: pytest tests/test_permalink.py
"""
import importlib.util
import os
import pathlib
import textwrap

import pytest

# Import backends — add mcp/ to sys.path so the module name is resolvable
# (Python 3.14's dataclass processor needs sys.modules[cls.__module__]).
import sys
_MCP_DIR = str(pathlib.Path(__file__).resolve().parent.parent / "mcp")
if _MCP_DIR not in sys.path:
    sys.path.insert(0, _MCP_DIR)
import backends as be


def _make_casket(tmp_path, mount_name="sources-ocp4.18.46",
                 index_rows=None, submodule_rows=None, files=None):
    """Build a minimal casket directory under tmp_path/srv/<mount_name>.

    Returns the mount path. Patches be.SRV and be.ROOT_PREFIX for the test.
    """
    srv = tmp_path / "srv"
    srv.mkdir(exist_ok=True)
    mount = srv / mount_name
    git = mount / "git"
    git.mkdir(parents=True)
    meta = mount / "meta"
    meta.mkdir(parents=True)

    # INDEX.tsv
    if index_rows is None:
        index_rows = [
            ("cluster-kube-apiserver-operator-v4.18.0-202506120905.p0.gb09c698.assembly.stream.el9",
             "https://github.com/openshift/cluster-kube-apiserver-operator",
             "b09c698abc123def456789abcdef0123456789ab",
             "v4.18.0-202506120905",
             "cluster-kube-apiserver-operator"),
        ]
    idx = git / "INDEX.tsv"
    with open(idx, "w") as f:
        f.write("dir\trepo\tref\tversion\tcomponents\n")
        for row in index_rows:
            f.write("\t".join(row) + "\n")

    # Create the source dirs and files
    if files is None:
        files = {}
        for row in index_rows:
            d = row[0]
            fpath = git / d / "pkg" / "operator" / "starter.go"
            fpath.parent.mkdir(parents=True, exist_ok=True)
            fpath.write_text("package operator\n")
            files[f"git/{d}/pkg/operator/starter.go"] = fpath

    # SUBMODULES.tsv
    if submodule_rows is not None:
        sub_tsv = meta / "SUBMODULES.tsv"
        with open(sub_tsv, "w") as f:
            f.write("# component\tpath\trepo\tref\texact\tstatus\n")
            for row in submodule_rows:
                f.write("\t".join(str(c) for c in row) + "\n")

    return mount


@pytest.fixture
def casket(tmp_path, monkeypatch):
    """Create a basic casket and patch globals."""
    mount = _make_casket(tmp_path)
    monkeypatch.setattr(be, "SRV", str(tmp_path / "srv"))
    monkeypatch.setattr(be, "ROOT_PREFIX", str(tmp_path / "srv" / "sources-"))
    return mount


# -------------------------------------------------------- basic INDEX.tsv
def test_permalink_basic(casket):
    src_dir = "cluster-kube-apiserver-operator-v4.18.0-202506120905.p0.gb09c698.assembly.stream.el9"
    path = str(casket / "git" / src_dir / "pkg" / "operator" / "starter.go")
    result = be.permalink(path)
    assert result["url"] == (
        "https://github.com/openshift/cluster-kube-apiserver-operator"
        "/blob/b09c698abc123def456789abcdef0123456789ab"
        "/pkg/operator/starter.go")
    assert result["exact"] is True
    assert result["source"] == "INDEX"
    assert result["ref"] == "b09c698abc123def456789abcdef0123456789ab"
    assert result["file"] == "pkg/operator/starter.go"


def test_permalink_with_line(casket):
    src_dir = "cluster-kube-apiserver-operator-v4.18.0-202506120905.p0.gb09c698.assembly.stream.el9"
    path = str(casket / "git" / src_dir / "pkg" / "operator" / "starter.go")
    result = be.permalink(path, line=42)
    assert result["url"].endswith("#L42")
    assert result["line"] == 42


# -------------------------------------------------------- submodule resolution
def test_permalink_submodule_exact(tmp_path, monkeypatch):
    wrapper_dir = "zero-trust-workload-identity-manager-release-1.1.0"
    index_rows = [
        (wrapper_dir,
         "https://github.com/openshift/zero-trust-workload-identity-manager-release",
         "612309a974bd",
         "1.1.0",
         "zero-trust-workload-identity-manager"),
    ]
    submodule_rows = [
        (wrapper_dir,
         "zero-trust-workload-identity-manager",
         "openshift/zero-trust-workload-identity-manager",
         "d4b265f257442ac84ed43ac2be542dcaa6b34fd0",
         "1",
         "ok-filled"),
    ]
    mount = _make_casket(tmp_path, mount_name="sources-ocp4.22",
                         index_rows=index_rows, submodule_rows=submodule_rows)
    # Create a file inside the submodule dir
    sub_file = (mount / "git" / wrapper_dir
                / "zero-trust-workload-identity-manager"
                / "pkg" / "controller" / "reconciler.go")
    sub_file.parent.mkdir(parents=True, exist_ok=True)
    sub_file.write_text("package controller\n")

    monkeypatch.setattr(be, "SRV", str(tmp_path / "srv"))
    monkeypatch.setattr(be, "ROOT_PREFIX", str(tmp_path / "srv" / "sources-"))

    result = be.permalink(str(sub_file))
    assert result["source"] == "SUBMODULES"
    assert result["exact"] is True
    assert result["ref"] == "d4b265f257442ac84ed43ac2be542dcaa6b34fd0"
    assert result["url"] == (
        "https://github.com/openshift/zero-trust-workload-identity-manager"
        "/blob/d4b265f257442ac84ed43ac2be542dcaa6b34fd0"
        "/pkg/controller/reconciler.go")
    assert result["file"] == "pkg/controller/reconciler.go"


def test_permalink_submodule_approx(tmp_path, monkeypatch):
    wrapper_dir = "spire-release-0.5"
    index_rows = [
        (wrapper_dir,
         "https://github.com/openshift/spire-release",
         "aaa111bbb222",
         "0.5",
         "spire"),
    ]
    submodule_rows = [
        (wrapper_dir,
         "spiffe-spire",
         "openshift/spiffe-spire",
         "release/v1.14.7",
         "0",
         "ok-branch-head"),
    ]
    mount = _make_casket(tmp_path, mount_name="sources-ocp4.20",
                         index_rows=index_rows, submodule_rows=submodule_rows)
    sub_file = (mount / "git" / wrapper_dir / "spiffe-spire" / "cmd" / "main.go")
    sub_file.parent.mkdir(parents=True, exist_ok=True)
    sub_file.write_text("package main\n")

    monkeypatch.setattr(be, "SRV", str(tmp_path / "srv"))
    monkeypatch.setattr(be, "ROOT_PREFIX", str(tmp_path / "srv" / "sources-"))

    result = be.permalink(str(sub_file))
    assert result["source"] == "SUBMODULES"
    assert result["exact"] is False
    assert "approximate" in result.get("note", "")
    assert result["url"] == (
        "https://github.com/openshift/spiffe-spire"
        "/blob/release/v1.14.7/cmd/main.go")


# -------------------------------------------------------- non-github repo
def test_permalink_non_github(tmp_path, monkeypatch):
    src_dir = "some-internal-component"
    index_rows = [
        (src_dir,
         "https://internal.example.com/repo/some-component",
         "deadbeef",
         "1.0",
         "some-component"),
    ]
    mount = _make_casket(tmp_path, index_rows=index_rows)
    f = mount / "git" / src_dir / "main.go"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("package main\n")

    monkeypatch.setattr(be, "SRV", str(tmp_path / "srv"))
    monkeypatch.setattr(be, "ROOT_PREFIX", str(tmp_path / "srv" / "sources-"))

    result = be.permalink(str(f))
    assert result["url"] is None
    assert "not on GitHub" in result["note"]
    assert result["ref"] == "deadbeef"


# -------------------------------------------------------- error cases
def test_permalink_nonexistent_path(tmp_path, monkeypatch):
    _make_casket(tmp_path)
    monkeypatch.setattr(be, "SRV", str(tmp_path / "srv"))
    monkeypatch.setattr(be, "ROOT_PREFIX", str(tmp_path / "srv" / "sources-"))
    result = be.permalink(str(tmp_path / "srv" / "sources-ocp4.18.46" / "git" / "nope"))
    assert "error" in result


def test_permalink_outside_casket(tmp_path, monkeypatch):
    monkeypatch.setattr(be, "SRV", str(tmp_path / "srv"))
    monkeypatch.setattr(be, "ROOT_PREFIX", str(tmp_path / "srv" / "sources-"))
    with pytest.raises(ValueError, match="outside casket"):
        be.permalink("/tmp/evil")


# ------------------------------------------------ b-operand (product subdir)
def test_permalink_b_operand_product(tmp_path, monkeypatch):
    """b-operand mounts have per-product subdirs, each with its own INDEX.tsv."""
    srv = tmp_path / "srv"
    srv.mkdir()
    mount = srv / "sources-layered-ocp4.20"
    mount.mkdir()
    # cnv product subdir
    cnv = mount / "cnv"
    git = cnv / "git"
    git.mkdir(parents=True)
    meta = cnv / "meta"
    meta.mkdir()

    src_dir = "kubevirt-v1.5.0"
    idx = git / "INDEX.tsv"
    idx.write_text(
        "dir\trepo\tref\tversion\tcomponents\n"
        f"{src_dir}\thttps://github.com/kubevirt/kubevirt\t"
        "cafe1234deadbeef\tv1.5.0\tvirt-operator,virt-api\n")
    f = git / src_dir / "pkg" / "virt-api" / "api.go"
    f.parent.mkdir(parents=True)
    f.write_text("package api\n")

    monkeypatch.setattr(be, "SRV", str(srv))
    monkeypatch.setattr(be, "ROOT_PREFIX", str(srv / "sources-"))

    result = be.permalink(str(f))
    assert result["url"] == (
        "https://github.com/kubevirt/kubevirt"
        "/blob/cafe1234deadbeef/pkg/virt-api/api.go")
    assert result["source"] == "INDEX"
    assert result["mount"] == "sources-layered-ocp4.20"


# ------------------------------------------------ file at tree root (no subpath)
def test_permalink_file_at_tree_root(casket):
    src_dir = "cluster-kube-apiserver-operator-v4.18.0-202506120905.p0.gb09c698.assembly.stream.el9"
    f = casket / "git" / src_dir / "Dockerfile"
    f.write_text("FROM ubi9\n")
    result = be.permalink(str(f))
    assert result["file"] == "Dockerfile"
    assert "/blob/" in result["url"]
    assert result["url"].endswith("/Dockerfile")


# -------------------------------------------------------- unmounted / stray paths
def test_permalink_path_under_sources_but_not_a_mount(tmp_path, monkeypatch):
    """A path that passes safe_path but belongs to no listed mount.

    list_mounts() only yields directories, so a regular file named
    sources-* satisfies the confinement check yet matches no mount.
    """
    srv = tmp_path / "srv"
    srv.mkdir()
    stray = srv / "sources-stray"
    stray.write_text("not a mount")
    monkeypatch.setattr(be, "SRV", str(srv))
    monkeypatch.setattr(be, "ROOT_PREFIX", str(srv / "sources-"))

    result = be.permalink(str(stray))
    assert "not under any mounted casket" in result["error"]


def test_permalink_ignores_submodule_row_of_other_component(tmp_path, monkeypatch):
    """SUBMODULES.tsv rows for a different component must not hijack the hit."""
    src_dir = "cvo-abc123"
    index_rows = [(src_dir, "https://github.com/openshift/cluster-version-operator",
                   "abc123def456", "4.20.0", "cluster-version-operator")]
    # A submodule row whose path would match, but for another component
    submodule_rows = [
        ("some-other-tree", "pkg", "openshift/other", "f" * 40, "1", "ok-filled"),
        (src_dir, "vendor-sub", "openshift/sub", "e" * 40, "1", "ok-filled"),
    ]
    mount = _make_casket(tmp_path, mount_name="sources-ocp4.20.22",
                         index_rows=index_rows, submodule_rows=submodule_rows)
    f = mount / "git" / src_dir / "pkg" / "cvo.go"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("package cvo\n")
    monkeypatch.setattr(be, "SRV", str(tmp_path / "srv"))
    monkeypatch.setattr(be, "ROOT_PREFIX", str(tmp_path / "srv" / "sources-"))

    result = be.permalink(str(f))
    assert result["source"] == "INDEX"
    assert result["repo"] == "https://github.com/openshift/cluster-version-operator"
    assert result["file"] == "pkg/cvo.go"


# ------------------------------------ fallback-fetched trees (git-fetched.tsv)
_KRP = ("ose-kube-rbac-proxy-095fa67c257b",
        "https://github.com/openshift/kube-rbac-proxy",
        "095fa67c257b0380184266438efbc2218c4e1761", "4.20.0", "ose-kube-rbac-proxy")


def _fetched_casket(tmp_path, monkeypatch, fetched_rows, index_row=_KRP):
    mount = _make_casket(tmp_path, mount_name="sources-ocp4.18-operators",
                         index_rows=[index_row])
    with open(mount / "meta" / "git-fetched.tsv", "w") as f:
        f.write("# tarball\turl\tkind\texact\n")
        f.writelines("\t".join(row) + "\n" for row in fetched_rows)
    monkeypatch.setattr(be, "SRV", str(tmp_path / "srv"))
    monkeypatch.setattr(be, "ROOT_PREFIX", str(tmp_path / "srv" / "sources-"))
    return str(mount / "git" / index_row[0] / "pkg" / "operator" / "starter.go")


def test_permalink_fallback_branch(tmp_path, monkeypatch):
    path = _fetched_casket(tmp_path, monkeypatch, [
        ("other.tar.gz", "pre-existing:other-main", "preexisting", "0"),
        (f"{_KRP[0]}.tar.gz", "pre-existing:kube-rbac-proxy-release-4.20", "preexisting", "0"),
    ])
    r = be.permalink(path)
    assert r["url"] == ("https://github.com/openshift/kube-rbac-proxy"
                        "/blob/release-4.20/pkg/operator/starter.go")
    assert r["exact"] is False and r["ref"] == "release-4.20"
    assert r["built_from"] == _KRP[2]
    assert "alt_url" not in r


def test_permalink_fallback_tag_is_ambiguous(tmp_path, monkeypatch):
    path = _fetched_casket(tmp_path, monkeypatch, [
        (f"{_KRP[0]}.tar.gz", "pre-existing:kube-rbac-proxy-4.20.0", "preexisting", "0")])
    r = be.permalink(path)
    assert r["ref"] == "v4.20.0"
    assert r["alt_url"].endswith("/blob/4.20.0/pkg/operator/starter.go")
    assert "alt_url" in r["note"]


def test_permalink_fallback_real_url(tmp_path, monkeypatch):
    path = _fetched_casket(tmp_path, monkeypatch, [
        (f"{_KRP[0]}.tar.gz",
         "https://github.com/openshift/kube-rbac-proxy/archive/refs/tags/v4.20.0.tar.gz",
         "tag", "0")])
    r = be.permalink(path)
    assert r["ref"] == "v4.20.0" and r["exact"] is False
    assert "alt_url" not in r


def test_permalink_fallback_unrecoverable_ref(tmp_path, monkeypatch):
    path = _fetched_casket(tmp_path, monkeypatch, [
        (f"{_KRP[0]}.tar.gz", "pre-existing:new-name-main", "preexisting", "0")])
    r = be.permalink(path)
    assert r["url"] is None and r["ref"] is None and r["exact"] is False
    assert "could not be recovered" in r["note"]


def test_permalink_exact_fetch_keeps_index_ref(tmp_path, monkeypatch):
    path = _fetched_casket(tmp_path, monkeypatch, [
        (f"{_KRP[0]}.tar.gz", f"pre-existing:kube-rbac-proxy-{_KRP[2]}", "sha", "1")])
    r = be.permalink(path)
    assert r["exact"] is True and r["ref"] == _KRP[2]
    assert "built_from" not in r


def test_permalink_unlisted_dir_keeps_index_ref(tmp_path, monkeypatch):
    path = _fetched_casket(tmp_path, monkeypatch, [
        ("short-row.tar.gz",),
        ("unrelated.tar.gz", "pre-existing:x-main", "preexisting", "0")])
    r = be.permalink(path)
    assert r["exact"] is True and r["source"] == "INDEX"
