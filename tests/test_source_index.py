"""Tests for scripts/build-source-index.py — INDEX.tsv generation and
symlink tree construction (by-component/, by-repo/).

Network-free: the module is pure FS logic tested with tmp_path fixtures.
"""
import importlib.util
import json
import os
import pathlib

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "build_source_index",
    pathlib.Path(__file__).resolve().parent.parent / "scripts" / "build-source-index.py")
bsi = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(bsi)


# ------------------------------------------------------------------- _safe
def test_safe_replaces_slash_and_space():
    assert bsi._safe("openshift/kubevirt") == "openshift_kubevirt"
    assert bsi._safe("some name") == "some_name"


def test_safe_noop_on_clean_name():
    assert bsi._safe("cluster-version-operator") == "cluster-version-operator"


# --------------------------------------------------------------- _repo_base
def test_repo_base_extracts_github_basename():
    assert bsi._repo_base("https://github.com/openshift/kubevirt.git") == "kubevirt"
    assert bsi._repo_base("https://github.com/openshift/kubevirt") == "kubevirt"


def test_repo_base_ignores_non_github():
    assert bsi._repo_base("https://gitlab.com/org/repo") == ""


def test_repo_base_empty_input():
    assert bsi._repo_base("") == ""
    assert bsi._repo_base(None) == ""


# ----------------------------------------------------------------- records
def test_records_phase_a_format():
    manifest = {"images": [
        {"name": "cluster-version-operator",
         "source": {"status": "OK", "tarball": "cvo-abc123.tar.gz",
                    "repo": "https://github.com/openshift/cvo", "commit": "abc123"}},
        {"name": "bad-image",
         "source": {"status": "FAILED", "tarball": ""}},
    ]}
    rows = list(bsi.records(manifest))
    assert len(rows) == 1
    comp, repo, ref, ver, tb = rows[0]
    assert comp == "cluster-version-operator"
    assert repo == "https://github.com/openshift/cvo"
    assert tb == "cvo-abc123.tar.gz"


def test_records_phase_b_format():
    manifest = {"git": [
        {"component": "my-operator", "source_url": "https://github.com/org/repo",
         "vcs_ref": "deadbeef", "version": "1.2.3", "tarball": "my-op-deadbeef.tar.gz"},
        {"operator": "fallback-op", "source_url": "https://github.com/org/fb",
         "vcs_ref": "cafe", "version": "1.0.0", "tarball": "fb-cafe.tar.gz"},
        {"component": "no-source", "tarball": "NO_SOURCE"},
    ]}
    rows = list(bsi.records(manifest))
    assert len(rows) == 2
    assert rows[0][0] == "my-operator"
    assert rows[1][0] == "fallback-op"


def test_records_mixed_a_and_b():
    manifest = {
        "images": [{"name": "img1", "source": {"tarball": "img1.tar.gz",
                    "repo": "r1", "commit": "c1"}}],
        "git": [{"component": "op1", "source_url": "r2", "vcs_ref": "c2",
                 "version": "1.0", "tarball": "op1.tar.gz"}],
    }
    rows = list(bsi.records(manifest))
    assert len(rows) == 2


# ----------------------------------------------------------- main (end-to-end)
def test_main_creates_index_and_symlinks(tmp_path, monkeypatch):
    stage = tmp_path / "stage"
    meta = stage / "meta"
    gitdir = stage / "git"
    meta.mkdir(parents=True)
    gitdir.mkdir()
    (gitdir / "cvo-abc123").mkdir()
    (gitdir / "kubevirt-def456").mkdir()

    manifest = {"images": [
        {"name": "cluster-version-operator",
         "source": {"tarball": "cvo-abc123.tar.gz",
                    "repo": "https://github.com/openshift/cluster-version-operator",
                    "commit": "abc123"}},
        {"name": "kubevirt",
         "source": {"tarball": "kubevirt-def456.tar.gz",
                    "repo": "https://github.com/kubevirt/kubevirt",
                    "commit": "def456"}},
    ]}
    (meta / "MANIFEST.json").write_text(json.dumps(manifest))

    monkeypatch.setattr("sys.argv", ["build-source-index.py", str(stage)])
    assert bsi.main() == 0

    idx = gitdir / "INDEX.tsv"
    assert idx.exists()
    lines = idx.read_text().splitlines()
    assert lines[0] == "dir\trepo\tref\tversion\tcomponents"
    assert len(lines) == 3  # header + 2 entries

    assert (stage / "by-component" / "cluster-version-operator").is_symlink()
    assert (stage / "by-component" / "kubevirt").is_symlink()
    assert (stage / "by-repo" / "cluster-version-operator").is_symlink()
    assert (stage / "by-repo" / "kubevirt").is_symlink()


def test_main_dedup_multiple_components_same_dir(tmp_path, monkeypatch):
    """Multiple components sharing the same (repo, commit) get one git/ dir."""
    stage = tmp_path / "stage"
    (stage / "meta").mkdir(parents=True)
    (stage / "git" / "shared-abc").mkdir(parents=True)

    manifest = {"images": [
        {"name": "comp-a", "source": {"tarball": "shared-abc.tar.gz",
                                      "repo": "https://github.com/org/shared",
                                      "commit": "abc"}},
        {"name": "comp-b", "source": {"tarball": "shared-abc.tar.gz",
                                      "repo": "https://github.com/org/shared",
                                      "commit": "abc"}},
    ]}
    (stage / "meta" / "MANIFEST.json").write_text(json.dumps(manifest))
    monkeypatch.setattr("sys.argv", ["build-source-index.py", str(stage)])
    bsi.main()

    idx = (stage / "git" / "INDEX.tsv").read_text().splitlines()
    data_lines = [l for l in idx[1:] if l.strip()]
    assert len(data_lines) == 1
    assert "comp-a,comp-b" in data_lines[0]

    assert (stage / "by-component" / "comp-a").is_symlink()
    assert (stage / "by-component" / "comp-b").is_symlink()


def test_main_skips_unlisted_dirs(tmp_path, monkeypatch):
    """Dirs not in MANIFEST are ignored (leftover from partial runs)."""
    stage = tmp_path / "stage"
    (stage / "meta").mkdir(parents=True)
    (stage / "git" / "orphan-dir").mkdir(parents=True)

    manifest = {"images": [
        {"name": "gone", "source": {"tarball": "not-staged.tar.gz",
                                    "repo": "r", "commit": "c"}},
    ]}
    (stage / "meta" / "MANIFEST.json").write_text(json.dumps(manifest))
    monkeypatch.setattr("sys.argv", ["build-source-index.py", str(stage)])
    bsi.main()

    idx = (stage / "git" / "INDEX.tsv").read_text().splitlines()
    assert len(idx) == 1  # header only
