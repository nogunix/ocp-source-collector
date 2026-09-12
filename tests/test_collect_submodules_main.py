"""Tests for scripts/collect-submodules.py — store_dir, parent_refs,
link_tree, main early-exit, and scan_one.

Network-free: uses tmp_path fixtures and no GitHub API calls.
Run: pytest tests/test_collect_submodules_main.py
"""
import importlib.util
import json
import os
import pathlib
import sys

# submodulelib must be importable before collect-submodules.py loads
_SCRIPTS = str(pathlib.Path(__file__).resolve().parent.parent / "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import submodulelib as sml  # noqa: E402

_SPEC = importlib.util.spec_from_file_location(
    "collect_submodules",
    pathlib.Path(__file__).resolve().parent.parent / "scripts" / "collect-submodules.py")
cs = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(cs)


# ------------------------------------------------------------------ store_dir
def test_store_dir_simple():
    pin = sml.Pin("sub", "org/repo", "abc123", True, "")
    assert cs.store_dir(pin) == "org_repo@abc123"


def test_store_dir_slash_in_ref():
    pin = sml.Pin("sub", "org/repo", "release/v1.14", False, "")
    assert cs.store_dir(pin) == "org_repo@release_v1.14"


def test_store_dir_deep_slug():
    pin = sml.Pin("sub", "openshift/zero-trust-workload-identity-manager",
                  "d4b265f", True, "")
    assert cs.store_dir(pin) == "openshift_zero-trust-workload-identity-manager@d4b265f"


# --------------------------------------------------------------- parent_refs
def test_parent_refs_phase_a(tmp_path):
    stage = tmp_path / "stage"
    meta = stage / "meta"
    meta.mkdir(parents=True)
    manifest = {"images": [
        {"name": "cluster-version-operator",
         "source": {"tarball": "cvo-abc123.tar.gz",
                    "repo": "https://github.com/openshift/cvo",
                    "commit": "abc123"}},
        {"name": "kubevirt",
         "source": {"tarball": "kubevirt-def456.tar.gz",
                    "repo": "https://github.com/kubevirt/kubevirt",
                    "commit": "def456"}},
    ]}
    (meta / "MANIFEST.json").write_text(json.dumps(manifest))
    refs = cs.parent_refs(str(stage))
    assert refs["cvo-abc123"] == ("https://github.com/openshift/cvo", "abc123")
    assert refs["kubevirt-def456"] == ("https://github.com/kubevirt/kubevirt", "def456")


def test_parent_refs_no_manifest(tmp_path):
    refs = cs.parent_refs(str(tmp_path))
    assert refs == {}


# ------------------------------------------------------------------ link_tree
def test_link_tree_creates_hardlinks(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_text("hello")
    sub = src / "sub"
    sub.mkdir()
    (sub / "b.txt").write_text("world")

    dst = tmp_path / "dst"
    cs.link_tree(str(src), str(dst))

    assert (dst / "a.txt").read_text() == "hello"
    assert (dst / "sub" / "b.txt").read_text() == "world"
    # on the same filesystem, hardlinks share inodes
    assert os.stat(src / "a.txt").st_ino == os.stat(dst / "a.txt").st_ino


# ---------------------------------------------------------- main early-exit
def test_main_no_git_dir(tmp_path, monkeypatch):
    stage = tmp_path / "stage"
    stage.mkdir()
    monkeypatch.setattr(sys, "argv", ["collect-submodules.py", str(stage)])
    assert cs.main() == 0


# ------------------------------------------------------------------ scan_one
def test_scan_one_finds_empty_submodule(tmp_path):
    tree = tmp_path / "comp"
    tree.mkdir()
    (tree / ".gitmodules").write_text(
        '[submodule "child"]\n'
        '\tpath = child\n'
        '\turl = https://github.com/org/child.git\n'
        '\tbranch = main\n'
    )
    # create the empty submodule dir (as git archive would leave it)
    (tree / "child").mkdir()

    seen = {}
    todo, bad = cs.scan_one(str(tree), "comp", "", "", "", "", seen)
    assert len(todo) == 1
    comp, rel, pin = todo[0]
    assert comp == "comp"
    assert pin.slug == "org/child"
    assert pin.ref == "main"
    assert pin.exact is False
    assert bad == []


def test_scan_one_skips_populated_submodule(tmp_path):
    tree = tmp_path / "comp"
    tree.mkdir()
    (tree / ".gitmodules").write_text(
        '[submodule "child"]\n'
        '\tpath = child\n'
        '\turl = https://github.com/org/child.git\n'
        '\tbranch = main\n'
    )
    child = tree / "child"
    child.mkdir()
    (child / "README.md").write_text("already populated")

    seen = {}
    todo, bad = cs.scan_one(str(tree), "comp", "", "", "", "", seen)
    assert todo == []


def test_scan_one_no_gitmodules(tmp_path):
    tree = tmp_path / "comp"
    tree.mkdir()
    todo, bad = cs.scan_one(str(tree), "comp", "", "", "", "", {})
    assert todo == []
    assert bad == []


def test_scan_one_non_github_reported_as_bad(tmp_path):
    tree = tmp_path / "comp"
    tree.mkdir()
    (tree / ".gitmodules").write_text(
        '[submodule "gl"]\n'
        '\tpath = gl\n'
        '\turl = https://gitlab.com/nvidia/something.git\n'
    )
    (tree / "gl").mkdir()

    seen = {}
    todo, bad = cs.scan_one(str(tree), "comp", "", "", "", "", seen)
    assert todo == []
    assert len(bad) == 1
    assert "not-github" in bad[0][3]
