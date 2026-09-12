"""Tests for scripts/report-submodule-gaps.py — unit_minor, iter_units,
find_gitmodules, scan_unit, build_repo_index, pick_elsewhere, and main().

Network-free: uses tmp_path fixtures and monkeypatched paths.
Run: pytest tests/test_report_submodule_gaps.py
"""
import importlib.util
import os
import pathlib
import sys

import pytest

# submodulelib must be importable (report-submodule-gaps imports it at load time)
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))

_spec = importlib.util.spec_from_file_location(
    "report_submodule_gaps",
    str(pathlib.Path(__file__).resolve().parent.parent / "scripts" / "report-submodule-gaps.py"))
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

unit_minor = _mod.unit_minor
iter_units = _mod.iter_units
find_gitmodules = _mod.find_gitmodules
scan_unit = _mod.scan_unit
build_repo_index = _mod.build_repo_index
pick_elsewhere = _mod.pick_elsewhere
main = _mod.main


# ---------------------------------------------------------------- unit_minor
class TestUnitMinor:
    def test_patch_version(self):
        assert unit_minor("sources-ocp4.22.9") == "4.22"

    def test_community_operators(self):
        assert unit_minor("sources-ocp4.22-community-operators") == "4.22"

    def test_layered(self):
        assert unit_minor("sources-layered-ocp4.22") == "4.22"

    def test_no_match(self):
        assert unit_minor("sources-something") == ""

    def test_certified(self):
        assert unit_minor("sources-ocp4.18-certified-operators") == "4.18"


# --------------------------------------------------------------- iter_units
class TestIterUnits:
    def test_single_layout(self, tmp_path):
        mount = tmp_path / "sources-ocp4.20.22"
        (mount / "git").mkdir(parents=True)
        result = list(iter_units(str(tmp_path)))
        assert len(result) == 1
        name, label, unit_dir = result[0]
        assert name == "sources-ocp4.20.22"
        assert label == ""
        assert unit_dir == str(mount)

    def test_b_operand_layout(self, tmp_path):
        mount = tmp_path / "sources-layered-ocp4.20"
        (mount / "cnv" / "git").mkdir(parents=True)
        (mount / "acs" / "git").mkdir(parents=True)
        result = list(iter_units(str(tmp_path)))
        assert len(result) == 2
        labels = {r[1] for r in result}
        assert labels == {"acs", "cnv"}

    def test_skips_non_sources(self, tmp_path):
        (tmp_path / "not-a-mount" / "git").mkdir(parents=True)
        result = list(iter_units(str(tmp_path)))
        assert result == []

    def test_nonexistent_root(self, tmp_path):
        result = list(iter_units(str(tmp_path / "nope")))
        assert result == []

    def test_mixed_layout(self, tmp_path):
        """A mount with git/ at root + a mount with product subdirs."""
        m1 = tmp_path / "sources-ocp4.20.22"
        (m1 / "git").mkdir(parents=True)
        m2 = tmp_path / "sources-layered-ocp4.20"
        (m2 / "cnv" / "git").mkdir(parents=True)
        result = list(iter_units(str(tmp_path)))
        assert len(result) == 2


# ---------------------------------------------------------- find_gitmodules
class TestFindGitmodules:
    def test_finds_at_root(self, tmp_path):
        git_dir = tmp_path / "git"
        tree = git_dir / "some-tree"
        tree.mkdir(parents=True)
        (tree / ".gitmodules").write_text("[submodule]")
        found = list(find_gitmodules(str(git_dir)))
        assert len(found) == 1
        assert found[0].endswith(".gitmodules")

    def test_depth_cap(self, tmp_path):
        git_dir = tmp_path / "git"
        deep = git_dir / "a" / "b" / "c" / "d"
        deep.mkdir(parents=True)
        (deep / ".gitmodules").write_text("[submodule]")
        found = list(find_gitmodules(str(git_dir), max_depth=2))
        assert found == []

    def test_finds_nested(self, tmp_path):
        git_dir = tmp_path / "git"
        nested = git_dir / "tree" / "sub"
        nested.mkdir(parents=True)
        (nested / ".gitmodules").write_text("[submodule]")
        found = list(find_gitmodules(str(git_dir), max_depth=3))
        assert len(found) == 1


# -------------------------------------------------------------- scan_unit
GITMODULES_CONTENT = """\
[submodule "vendor/docsy"]
    path = vendor/docsy
    url = https://github.com/google/docsy.git

[submodule "vendor/hugo-book"]
    path = vendor/hugo-book
    url = https://github.com/alex-shpak/hugo-book.git
"""


class TestScanUnit:
    def test_empty_submodule(self, tmp_path):
        unit = tmp_path / "unit"
        tree = unit / "git" / "my-tree"
        tree.mkdir(parents=True)
        (tree / ".gitmodules").write_text(GITMODULES_CONTENT)
        (tree / "vendor" / "docsy").mkdir(parents=True)
        (tree / "vendor" / "hugo-book").mkdir(parents=True)
        rows = scan_unit(str(unit))
        assert len(rows) == 2
        states = {r[1]: r[5] for r in rows}
        assert states["vendor/docsy"] == "empty"
        assert states["vendor/hugo-book"] == "empty"

    def test_filled_submodule(self, tmp_path):
        unit = tmp_path / "unit"
        tree = unit / "git" / "my-tree"
        tree.mkdir(parents=True)
        (tree / ".gitmodules").write_text(GITMODULES_CONTENT)
        docsy = tree / "vendor" / "docsy"
        docsy.mkdir(parents=True)
        (docsy / "README.md").write_text("content")
        (tree / "vendor" / "hugo-book").mkdir(parents=True)
        rows = scan_unit(str(unit))
        states = {r[1]: r[5] for r in rows}
        assert states["vendor/docsy"] == "filled"
        assert states["vendor/hugo-book"] == "empty"

    def test_missing_dir(self, tmp_path):
        unit = tmp_path / "unit"
        tree = unit / "git" / "my-tree"
        tree.mkdir(parents=True)
        (tree / ".gitmodules").write_text(GITMODULES_CONTENT)
        # don't create vendor dirs at all
        rows = scan_unit(str(unit))
        states = {r[1]: r[5] for r in rows}
        assert states["vendor/docsy"] == "missing-dir"

    def test_no_gitmodules(self, tmp_path):
        unit = tmp_path / "unit"
        (unit / "git" / "tree").mkdir(parents=True)
        rows = scan_unit(str(unit))
        assert rows == []


# -------------------------------------------------------- build_repo_index
class TestBuildRepoIndex:
    def test_basic_index(self, tmp_path):
        mount = tmp_path / "sources-ocp4.20.22"
        byrepo = mount / "by-repo"
        byrepo.mkdir(parents=True)
        (byrepo / "kubevirt").mkdir()
        (byrepo / "cvo").mkdir()

        units = [("sources-ocp4.20.22", "", str(mount))]
        index = build_repo_index(units)
        assert "kubevirt" in index
        assert "cvo" in index
        assert index["kubevirt"][0][0] == "4.20"  # minor
        assert index["kubevirt"][0][1] == "sources-ocp4.20.22"  # mount

    def test_no_byrepo(self, tmp_path):
        mount = tmp_path / "sources-ocp4.20.22"
        mount.mkdir(parents=True)
        units = [("sources-ocp4.20.22", "", str(mount))]
        index = build_repo_index(units)
        assert index == {}


# -------------------------------------------------------- pick_elsewhere
class TestPickElsewhere:
    def test_same_minor_payload_preferred(self):
        index = {
            "kubevirt": [
                ("4.20", "sources-ocp4.20.22", "kubevirt"),
                ("4.18", "sources-ocp4.18.41", "kubevirt"),
            ]
        }
        result = pick_elsewhere("kubevirt/kubevirt", "4.20", index)
        assert "sources-ocp4.20.22" in result

    def test_different_minor_fallback(self):
        index = {
            "kubevirt": [
                ("4.18", "sources-ocp4.18.41", "kubevirt"),
            ]
        }
        result = pick_elsewhere("kubevirt/kubevirt", "4.20", index)
        assert "sources-ocp4.18.41" in result

    def test_no_hits(self):
        assert pick_elsewhere("nonexistent/repo", "4.20", {}) == ""

    def test_empty_slug(self):
        assert pick_elsewhere("", "4.20", {"": [("4.20", "m", "a")]}) == ""

    def test_payload_mount_preferred_over_catalog(self):
        index = {
            "kubevirt": [
                ("4.20", "sources-ocp4.20-certified-operators", "kubevirt"),
                ("4.20", "sources-ocp4.20.22", "kubevirt"),
            ]
        }
        result = pick_elsewhere("kubevirt/kubevirt", "4.20", index)
        assert "sources-ocp4.20.22" in result


# ------------------------------------------------------------------ main
class TestMain:
    def test_empty_root_returns_1(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sys, "argv",
                            ["report-submodule-gaps.py",
                             "--root", str(tmp_path / "empty"),
                             "--out-dir", str(tmp_path / "out")])
        assert main() == 1

    def test_writes_output_files(self, tmp_path, monkeypatch):
        root = tmp_path / "srv"
        mount = root / "sources-ocp4.20.22"
        tree = mount / "git" / "my-tree"
        tree.mkdir(parents=True)
        (tree / ".gitmodules").write_text(GITMODULES_CONTENT)
        (tree / "vendor" / "docsy").mkdir(parents=True)
        (tree / "vendor" / "hugo-book").mkdir(parents=True)

        out_dir = tmp_path / "out"
        monkeypatch.setattr(sys, "argv",
                            ["report-submodule-gaps.py",
                             "--root", str(root),
                             "--out-dir", str(out_dir)])
        rc = main()
        assert rc == 0
        assert (out_dir / "submodule-gaps.tsv").exists()
        assert (out_dir / "submodule-gaps.md").exists()
        tsv = (out_dir / "submodule-gaps.tsv").read_text()
        assert "sources-ocp4.20.22" in tsv
        assert "vendor/docsy" in tsv

    def test_filled_submodules_not_in_tsv(self, tmp_path, monkeypatch):
        root = tmp_path / "srv"
        mount = root / "sources-ocp4.20.22"
        tree = mount / "git" / "my-tree"
        tree.mkdir(parents=True)
        gm = '[submodule "sub"]\n    path = sub\n    url = https://github.com/org/repo.git\n'
        (tree / ".gitmodules").write_text(gm)
        sub = tree / "sub"
        sub.mkdir()
        (sub / "main.go").write_text("package main")

        out_dir = tmp_path / "out"
        monkeypatch.setattr(sys, "argv",
                            ["report-submodule-gaps.py",
                             "--root", str(root),
                             "--out-dir", str(out_dir)])
        rc = main()
        assert rc == 0
        tsv = (out_dir / "submodule-gaps.tsv").read_text()
        lines = [l for l in tsv.strip().split("\n") if not l.startswith("mount")]
        assert len(lines) == 0


class TestIterUnitsUnreadable:
    def test_mount_that_is_a_file_is_skipped(self, tmp_path):
        """sources-* need not be a directory — a stray file must not abort."""
        (tmp_path / "sources-stray").write_text("not a mount")
        good = tmp_path / "sources-ocp4.20.22" / "git"
        good.mkdir(parents=True)
        assert [u[0] for u in iter_units(str(tmp_path))] == ["sources-ocp4.20.22"]

    def test_unlistable_mount_is_skipped(self, tmp_path, monkeypatch):
        mount = tmp_path / "sources-layered-ocp4.20"
        mount.mkdir()
        good = tmp_path / "sources-ocp4.20.22" / "git"
        good.mkdir(parents=True)

        real_listdir = os.listdir

        def fake_listdir(p):
            if str(p) == str(mount):
                raise PermissionError("EACCES")
            return real_listdir(p)

        monkeypatch.setattr(_mod.os, "listdir", fake_listdir)
        assert [u[0] for u in iter_units(str(tmp_path))] == ["sources-ocp4.20.22"]


class TestScanUnitUnreadableGitmodules:
    def test_unreadable_gitmodules_skipped(self, tmp_path, monkeypatch):
        unit = tmp_path / "unit"
        git = unit / "git"
        (git / "bad-tree").mkdir(parents=True)
        (git / "bad-tree" / ".gitmodules").write_text(
            '[submodule "x"]\n\tpath = x\n\turl = https://github.com/o/x.git\n')

        real_open = open

        def fake_open(path, *a, **kw):
            if str(path).endswith(".gitmodules"):
                raise PermissionError("EACCES")
            return real_open(path, *a, **kw)

        monkeypatch.setattr("builtins.open", fake_open)
        assert scan_unit(str(unit)) == []

    def test_mount_subdir_without_git_is_not_a_unit(self, tmp_path):
        """b-operand mounts hold product subdirs; meta/ and stray dirs are not."""
        mount = tmp_path / "sources-layered-ocp4.20"
        (mount / "cnv" / "git").mkdir(parents=True)
        (mount / "meta").mkdir()
        (mount / "README").write_text("")
        assert [u[1] for u in iter_units(str(tmp_path))] == ["cnv"]
