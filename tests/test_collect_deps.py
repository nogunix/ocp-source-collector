"""Integration tests for scripts/collect-deps.py — link_tree, extract, and
main() with network-free fixtures.

Run: pytest tests/test_collect_deps.py
"""
import importlib.util
import io
import os
import pathlib
import tarfile
import zipfile

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "collect_deps",
    pathlib.Path(__file__).resolve().parent.parent / "scripts" / "collect-deps.py")
cd = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(cd)


# ---------------------------------------------------------------- link_tree
def test_link_tree_creates_hardlinks(tmp_path):
    src = tmp_path / "store" / "go" / "mod"
    src.mkdir(parents=True)
    (src / "go.mod").write_text("module example.com/foo")
    (src / "main.go").write_text("package main")

    dst = tmp_path / "stage" / "deps" / "go" / "mod"
    cd.link_tree(str(src), str(dst))

    assert (dst / "go.mod").exists()
    assert (dst / "main.go").exists()
    assert (dst / "go.mod").read_text() == "module example.com/foo"


def test_link_tree_skips_existing(tmp_path):
    src = tmp_path / "store"
    src.mkdir()
    (src / "a.txt").write_text("from store")

    dst = tmp_path / "dst"
    dst.mkdir()
    (dst / "a.txt").write_text("pre-existing")

    cd.link_tree(str(src), str(dst))
    assert (dst / "a.txt").read_text() == "pre-existing"


# ---------------------------------------------------------------- extract
def _make_zip(tmp_path, entries, name="archive.zip"):
    zpath = tmp_path / name
    with zipfile.ZipFile(str(zpath), "w") as z:
        for arcname, content in entries:
            z.writestr(arcname, content)
    return open(str(zpath), "rb").read()


def _make_tar_gz(tmp_path, entries, name="archive.tar.gz"):
    tpath = tmp_path / name
    with tarfile.open(str(tpath), "w:gz") as t:
        for arcname, content in entries:
            info = tarfile.TarInfo(name=arcname)
            data = content.encode() if isinstance(content, str) else content
            info.size = len(data)
            t.addfile(info, io.BytesIO(data))
    return open(str(tpath), "rb").read()


def test_extract_zip_strip_1(tmp_path):
    blob = _make_zip(tmp_path, [
        ("top/src/main.go", "package main"),
        ("top/README.md", "hello"),
    ])
    dest = tmp_path / "out"
    cd.extract(blob, str(dest), "1", "http://example.com/x.zip")
    assert (dest / "src" / "main.go").read_text() == "package main"
    assert (dest / "README.md").read_text() == "hello"


def test_extract_tar_gz_strip_1(tmp_path):
    blob = _make_tar_gz(tmp_path, [
        ("top/lib.py", "print('hi')"),
        ("top/sub/util.py", "pass"),
    ])
    dest = tmp_path / "out"
    cd.extract(blob, str(dest), "1", "http://example.com/x.tar.gz")
    assert (dest / "lib.py").read_text() == "print('hi')"
    assert (dest / "sub" / "util.py").read_text() == "pass"


def test_extract_rejects_traversal(tmp_path):
    blob = _make_zip(tmp_path, [
        ("top/../../../etc/passwd", "root:x:0:0"),
        ("top/safe.txt", "ok"),
    ])
    dest = tmp_path / "out"
    cd.extract(blob, str(dest), "1", "http://example.com/x.zip")
    assert not (tmp_path / "etc").exists()
    assert (dest / "safe.txt").read_text() == "ok"


def test_extract_zip_go_strip(tmp_path):
    blob = _make_zip(tmp_path, [
        ("golang.org/x/net@v0.25.0/http2/h2c.go", "package h2c"),
        ("golang.org/x/net@v0.25.0/README.md", "net"),
    ])
    dest = tmp_path / "out"
    cd.extract(blob, str(dest), "go", "http://example.com/x.zip")
    assert (dest / "http2" / "h2c.go").read_text() == "package h2c"
    assert (dest / "README.md").read_text() == "net"


# ---------------------------------------------------------------- main
def test_main_no_git_dir(tmp_path, monkeypatch):
    stage = tmp_path / "stage"
    stage.mkdir()
    monkeypatch.setattr("sys.argv", ["collect-deps.py", str(stage),
                                     "--store", str(tmp_path / "store")])
    monkeypatch.setenv("CASKET_DEP_STORE", str(tmp_path / "store"))
    assert cd.main() == 0


def test_main_with_go_tree(tmp_path, monkeypatch):
    """A stage with a go.sum should resolve deps and write DEPS.tsv.
    The actual fetching fails (no network) but the metadata files are created."""
    stage = tmp_path / "stage"
    tree = stage / "git" / "my-component"
    tree.mkdir(parents=True)
    (stage / "meta").mkdir()

    (tree / "go.sum").write_text(
        "golang.org/x/net v0.25.0 h1:abc=\n"
        "golang.org/x/net v0.25.0/go.mod h1:def=\n"
    )
    (tree / "go.mod").write_text("module example.com/foo\ngo 1.21\n")

    store = tmp_path / "store"
    monkeypatch.setattr("sys.argv", [
        "collect-deps.py", str(stage),
        "--store", str(store), "--jobs", "1", "--eco", "go",
    ])
    monkeypatch.setenv("CASKET_DEP_STORE", str(store))
    cd.main()

    deps_tsv = stage / "meta" / "DEPS.tsv"
    assert deps_tsv.exists()
    content = deps_tsv.read_text()
    assert "my-component" in content
    assert "golang.org/x/net" in content


def test_main_with_vendored_go(tmp_path, monkeypatch):
    """A vendored Go tree should be skipped (vendor/ dir present)."""
    stage = tmp_path / "stage"
    tree = stage / "git" / "vendored-comp"
    tree.mkdir(parents=True)
    (stage / "meta").mkdir()

    (tree / "go.sum").write_text(
        "golang.org/x/text v0.15.0 h1:abc=\n"
        "golang.org/x/text v0.15.0/go.mod h1:def=\n"
    )
    (tree / "vendor").mkdir()

    store = tmp_path / "store"
    monkeypatch.setattr("sys.argv", [
        "collect-deps.py", str(stage),
        "--store", str(store), "--jobs", "1", "--eco", "go",
    ])
    monkeypatch.setenv("CASKET_DEP_STORE", str(store))
    result = cd.main()
    assert result == 0
    assert not (stage / "meta" / "DEPS.tsv").exists()
