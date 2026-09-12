"""Tests for mcp/backends.py — read_file, diff_file, list_dir,
_find_lockfiles, _cargo_lock_matches, _go_mod_matches, resolve_dependency,
_index_stats, coverage_report, and OpenGrok fallback paths.

Network-free: uses monkeypatched paths and tmp_path fixtures.
Run: pytest tests/test_backends_fs.py
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "mcp"))
import backends as be  # noqa: E402


def _setup_srv(tmp_path, monkeypatch):
    srv = tmp_path / "srv"
    srv.mkdir()
    monkeypatch.setattr(be, "SRV", str(srv))
    monkeypatch.setattr(be, "ROOT_PREFIX", str(srv) + "/sources-")
    return srv


# ------------------------------------------------------------------ read_file
def test_read_file_whole(tmp_path, monkeypatch):
    srv = _setup_srv(tmp_path, monkeypatch)
    mount = srv / "sources-ocp4.20.22"
    mount.mkdir()
    f = mount / "hello.go"
    f.write_text("line1\nline2\nline3\n")
    result = be.read_file(str(f))
    assert result["total_lines"] == 3
    assert result["start"] == 1
    assert result["end"] == 3
    assert result["truncated"] is False
    assert "line1" in result["content"]


def test_read_file_line_range(tmp_path, monkeypatch):
    srv = _setup_srv(tmp_path, monkeypatch)
    mount = srv / "sources-ocp4.20.22"
    mount.mkdir()
    f = mount / "hello.go"
    f.write_text("a\nb\nc\nd\ne\n")
    result = be.read_file(str(f), start=2, end=4)
    assert result["start"] == 2
    assert result["end"] == 4
    assert result["content"] == "b\nc\nd\n"


def test_read_file_truncated(tmp_path, monkeypatch):
    srv = _setup_srv(tmp_path, monkeypatch)
    mount = srv / "sources-ocp4.20.22"
    mount.mkdir()
    f = mount / "big.txt"
    f.write_text("x" * 300)
    result = be.read_file(str(f), max_bytes=100)
    assert result["truncated"] is True
    assert len(result["content"]) == 100


def test_read_file_not_a_file(tmp_path, monkeypatch):
    srv = _setup_srv(tmp_path, monkeypatch)
    mount = srv / "sources-ocp4.20.22"
    mount.mkdir()
    with pytest.raises(ValueError, match="not a file"):
        be.read_file(str(mount))


def test_read_file_outside_roots(monkeypatch):
    monkeypatch.setattr(be, "SRV", "/srv")
    monkeypatch.setattr(be, "ROOT_PREFIX", "/srv/sources-")
    with pytest.raises(ValueError, match="outside casket roots"):
        be.read_file("/etc/passwd")


# ------------------------------------------------------------------ diff_file
def test_diff_identical(tmp_path, monkeypatch):
    srv = _setup_srv(tmp_path, monkeypatch)
    mount = srv / "sources-ocp4.20.22"
    mount.mkdir()
    a = mount / "a.go"
    a.write_text("same\n")
    b = mount / "b.go"
    b.write_text("same\n")
    result = be.diff_file(str(a), str(b))
    assert result["identical"] is True
    assert result["added"] == 0
    assert result["removed"] == 0


def test_diff_changes(tmp_path, monkeypatch):
    srv = _setup_srv(tmp_path, monkeypatch)
    mount = srv / "sources-ocp4.20.22"
    mount.mkdir()
    a = mount / "a.go"
    a.write_text("old line\n")
    b = mount / "b.go"
    b.write_text("new line\n")
    result = be.diff_file(str(a), str(b))
    assert result["identical"] is False
    assert result["added"] >= 1
    assert result["removed"] >= 1
    assert "old line" in result["diff"]
    assert "new line" in result["diff"]


def test_diff_rejects_non_file(tmp_path, monkeypatch):
    srv = _setup_srv(tmp_path, monkeypatch)
    mount = srv / "sources-ocp4.20.22"
    mount.mkdir()
    f = mount / "a.go"
    f.write_text("x\n")
    with pytest.raises(ValueError, match="not a file"):
        be.diff_file(str(f), str(mount))


# ------------------------------------------------------------------ list_dir
def test_list_dir_contents(tmp_path, monkeypatch):
    srv = _setup_srv(tmp_path, monkeypatch)
    mount = srv / "sources-ocp4.20.22"
    mount.mkdir()
    (mount / "subdir").mkdir()
    (mount / "file.txt").write_text("hello")
    result = be.list_dir(str(mount))
    names = {e["name"] for e in result["entries"]}
    assert "subdir" in names
    assert "file.txt" in names
    types = {e["name"]: e["type"] for e in result["entries"]}
    assert types["subdir"] == "dir"
    assert types["file.txt"] == "file"


def test_list_dir_not_a_directory(tmp_path, monkeypatch):
    srv = _setup_srv(tmp_path, monkeypatch)
    mount = srv / "sources-ocp4.20.22"
    mount.mkdir()
    f = mount / "a.go"
    f.write_text("x")
    with pytest.raises(ValueError, match="not a directory"):
        be.list_dir(str(f))


# -------------------------------------------------------------- _find_lockfiles
def test_find_lockfiles_basic(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "Cargo.lock").write_text("[package]")
    (root / "sub").mkdir()
    (root / "sub" / "Cargo.lock").write_text("[package]")
    found = be._find_lockfiles(str(root), {"Cargo.lock"})
    assert len(found) == 2


def test_find_lockfiles_skips_vendor(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "vendor").mkdir()
    (root / "vendor" / "Cargo.lock").write_text("[package]")
    found = be._find_lockfiles(str(root), {"Cargo.lock"})
    assert found == []


def test_find_lockfiles_depth_cap(tmp_path):
    root = tmp_path / "repo"
    deep = root / "a" / "b" / "c" / "d" / "e"
    deep.mkdir(parents=True)
    (deep / "Cargo.lock").write_text("[package]")
    found = be._find_lockfiles(str(root), {"Cargo.lock"}, max_depth=4)
    assert found == []


def test_find_lockfiles_limit(tmp_path):
    root = tmp_path / "repo"
    for i in range(5):
        d = root / f"sub{i}"
        d.mkdir(parents=True)
        (d / "go.mod").write_text(f"module m{i}")
    found = be._find_lockfiles(str(root), {"go.mod"}, limit=2)
    assert len(found) == 2


# -------------------------------------------------------------- _cargo_lock_matches
def test_cargo_lock_exact_match(tmp_path):
    lock = tmp_path / "Cargo.lock"
    lock.write_text("""\
[[package]]
name = "rustls"
version = "0.21.10"
source = "registry+https://github.com/rust-lang/crates.io-index"
checksum = "abc123"
dependencies = ["ring 0.17.0"]

[[package]]
name = "ring"
version = "0.17.0"
source = "registry+https://github.com/rust-lang/crates.io-index"
dependencies = []

[[package]]
name = "rustls-webpki"
version = "0.101.0"
dependencies = ["rustls"]
""")
    matches = be._cargo_lock_matches(str(lock), "rustls")
    assert len(matches) == 1
    assert matches[0]["name"] == "rustls"
    assert matches[0]["version"] == "0.21.10"
    assert matches[0]["kind"] == "cargo"


def test_cargo_lock_substring_match(tmp_path):
    lock = tmp_path / "Cargo.lock"
    lock.write_text("""\
[[package]]
name = "rustls-webpki"
version = "0.101.0"
""")
    matches = be._cargo_lock_matches(str(lock), "webpki")
    assert len(matches) == 1
    assert matches[0]["name"] == "rustls-webpki"


def test_cargo_lock_no_match(tmp_path):
    lock = tmp_path / "Cargo.lock"
    lock.write_text("""\
[[package]]
name = "serde"
version = "1.0.0"
""")
    matches = be._cargo_lock_matches(str(lock), "tokio")
    assert matches == []


def test_cargo_lock_malformed(tmp_path):
    lock = tmp_path / "Cargo.lock"
    lock.write_text("not valid toml {{{{")
    matches = be._cargo_lock_matches(str(lock), "anything")
    assert len(matches) == 1
    assert "error" in matches[0]


# ---------------------------------------------------------------- _go_mod_matches
def test_go_mod_exact_match(tmp_path):
    mod = tmp_path / "go.mod"
    mod.write_text("""\
module github.com/example/project

go 1.21

require (
\tgithub.com/stretchr/testify v1.9.0
\tgolang.org/x/crypto v0.24.0 // indirect
)
""")
    matches = be._go_mod_matches(str(mod), "crypto")
    assert len(matches) == 1
    assert matches[0]["name"] == "golang.org/x/crypto"
    assert matches[0]["version"] == "v0.24.0"
    assert matches[0]["indirect"] is True
    assert matches[0]["kind"] == "go"


def test_go_mod_replace_directive(tmp_path):
    mod = tmp_path / "go.mod"
    mod.write_text("""\
module github.com/example/project

go 1.21

require (
\tgithub.com/old/pkg v1.0.0
)

replace (
\tgithub.com/old/pkg => github.com/new/pkg v2.0.0
)
""")
    matches = be._go_mod_matches(str(mod), "old/pkg")
    assert len(matches) == 1
    assert matches[0]["replaced_by"] == "github.com/new/pkg v2.0.0"


def test_go_mod_no_match(tmp_path):
    mod = tmp_path / "go.mod"
    mod.write_text("module example.com/foo\ngo 1.21\n")
    matches = be._go_mod_matches(str(mod), "nonexistent")
    assert matches == []


# ---------------------------------------------------------- resolve_dependency
def test_resolve_dependency_cargo(tmp_path, monkeypatch):
    srv = _setup_srv(tmp_path, monkeypatch)
    mount = srv / "sources-ocp4.20.22"
    mount.mkdir()
    repo = mount / "myrepo"
    repo.mkdir()
    lock = repo / "Cargo.lock"
    lock.write_text("""\
[[package]]
name = "serde"
version = "1.0.200"
source = "registry+https://github.com/rust-lang/crates.io-index"
""")
    result = be.resolve_dependency(str(repo), "serde", kind="cargo")
    assert result["count"] == 1
    assert result["matches"][0]["name"] == "serde"


def test_resolve_dependency_go(tmp_path, monkeypatch):
    srv = _setup_srv(tmp_path, monkeypatch)
    mount = srv / "sources-ocp4.20.22"
    mount.mkdir()
    repo = mount / "myrepo"
    repo.mkdir()
    gomod = repo / "go.mod"
    gomod.write_text("""\
module github.com/example/proj

go 1.21

require (
\tgithub.com/spf13/cobra v1.8.0
)
""")
    result = be.resolve_dependency(str(repo), "cobra", kind="go")
    assert result["count"] == 1
    assert result["matches"][0]["version"] == "v1.8.0"


def test_resolve_dependency_no_lockfiles(tmp_path, monkeypatch):
    srv = _setup_srv(tmp_path, monkeypatch)
    mount = srv / "sources-ocp4.20.22"
    mount.mkdir()
    repo = mount / "empty-repo"
    repo.mkdir()
    result = be.resolve_dependency(str(repo), "anything")
    assert result["count"] == 0
    assert "note" in result


def test_resolve_dependency_not_a_dir(tmp_path, monkeypatch):
    srv = _setup_srv(tmp_path, monkeypatch)
    mount = srv / "sources-ocp4.20.22"
    mount.mkdir()
    f = mount / "file.txt"
    f.write_text("x")
    result = be.resolve_dependency(str(f), "x")
    assert "error" in result


# ---------------------------------------------------------------- _index_stats
def test_index_stats_counts(tmp_path):
    unit = tmp_path / "unit"
    gitdir = unit / "git"
    gitdir.mkdir(parents=True)
    idx = "dir\trepo\tref\tversion\tcomponents\n"
    idx += "cvo-abc\thttps://github.com/openshift/cvo\tabc\t4.20\tcluster-version-operator\n"
    idx += "kv-def\thttps://github.com/kubevirt/kubevirt\tdef\t4.20\tkubevirt\n"
    idx += "shared\thttps://github.com/openshift/cvo\txyz\t4.20\tcomp-a,comp-b\n"
    (gitdir / "INDEX.tsv").write_text(idx)
    stats = be._index_stats(str(unit))
    assert stats["source_dirs"] == 3
    assert stats["repos"] == 2
    assert stats["components"] == 4


def test_index_stats_missing(tmp_path):
    assert be._index_stats(str(tmp_path)) is None


# ----------------------------------------------------------- coverage_report
def test_coverage_report_basic(tmp_path, monkeypatch):
    srv = _setup_srv(tmp_path, monkeypatch)
    mount = srv / "sources-ocp4.20.22"
    gitdir = mount / "git"
    gitdir.mkdir(parents=True)
    idx = "dir\trepo\tref\tversion\tcomponents\n"
    idx += "cvo\thttps://github.com/openshift/cvo\tabc\t4.20.22\tcluster-version-operator\n"
    (gitdir / "INDEX.tsv").write_text(idx)
    monkeypatch.setattr(be, "CASKET_WORK", str(tmp_path / "nonexistent"))
    result = be.coverage_report("4.20")
    assert "a" in result["phases"]
    assert result["phases"]["a"]["source_dirs"] == 1
    assert result["known_gaps"]


def test_coverage_report_no_mounts(tmp_path, monkeypatch):
    srv = _setup_srv(tmp_path, monkeypatch)
    monkeypatch.setattr(be, "CASKET_WORK", str(tmp_path / "nonexistent"))
    result = be.coverage_report("9.99")
    assert "note" in result["phases"]


# ----------------------------------------------------------- opengrok fallbacks
def test_search_text_no_path_no_opengrok(monkeypatch):
    monkeypatch.setattr(be, "opengrok_up", lambda timeout=3.0: False)
    result = be.search_text("foo")
    assert result["backend"] == "none"
    assert "error" in result


def test_search_symbol_no_opengrok_no_path(monkeypatch):
    monkeypatch.setattr(be, "opengrok_up", lambda: False)
    result = be.search_symbol("MyFunc")
    assert result["backend"] == "none"
    assert result["count"] == 0


def test_search_refs_no_opengrok(monkeypatch):
    monkeypatch.setattr(be, "opengrok_up", lambda: False)
    result = be.search_refs("MyFunc")
    assert "error" in result
    assert result["count"] == 0


import shutil

@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep not installed")
def test_search_text_with_path_uses_ripgrep(tmp_path, monkeypatch):
    srv = _setup_srv(tmp_path, monkeypatch)
    mount = srv / "sources-ocp4.20.22"
    mount.mkdir()
    f = mount / "main.go"
    f.write_text("package main\nfunc hello() {}\n")
    monkeypatch.setattr(be, "opengrok_up", lambda timeout=3.0: False)
    result = be.search_text("hello", path=str(mount))
    assert result["backend"] == "ripgrep"


def test_opengrok_search_bad_mode():
    result = be.opengrok_search("badmode", "query")
    assert "error" in result
    assert "bad mode" in result["error"]
