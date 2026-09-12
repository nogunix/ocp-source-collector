"""Integration tests for scripts/collect-deps.py — link_tree, extract, fetch,
pypi_sdist_url, acquire, and main() with network-free fixtures.

Run: pytest tests/test_collect_deps.py
"""
import importlib.util
import io
import json
import os
import pathlib
import tarfile
import urllib.error
import zipfile
from unittest.mock import patch, MagicMock

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


# ---------------------------------------------------------------- fetch
class TestFetch:
    @patch("urllib.request.urlopen")
    def test_success(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = b"data"
        mock_urlopen.return_value = mock_resp
        assert cd.fetch("https://example.com/pkg.tar.gz", retries=1) == b"data"

    @patch("urllib.request.urlopen")
    def test_404_no_retry(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.HTTPError(
            "url", 404, "Not Found", {}, None)
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            cd.fetch("https://example.com/pkg.tar.gz", retries=3)
        assert exc_info.value.code == 404

    @patch("urllib.request.urlopen")
    def test_retry_then_success(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = b"ok"
        mock_urlopen.side_effect = [
            urllib.error.URLError("timeout"),
            mock_resp,
        ]
        monkeypatch_time = patch.object(cd.time, "sleep")
        with monkeypatch_time:
            result = cd.fetch("https://example.com/x", retries=2)
        assert result == b"ok"

    @patch("urllib.request.urlopen")
    def test_all_retries_fail(self, mock_urlopen):
        mock_urlopen.side_effect = OSError("connection refused")
        with patch.object(cd.time, "sleep"):
            with pytest.raises(OSError, match="connection refused"):
                cd.fetch("https://example.com/x", retries=2)


# ---------------------------------------------------------- pypi_sdist_url
class TestPypiSdistUrl:
    @patch("urllib.request.urlopen")
    def test_sdist_preferred(self, mock_urlopen):
        api_json = json.dumps({"urls": [
            {"packagetype": "bdist_wheel", "filename": "pkg-1.0-py3-none-any.whl",
             "url": "https://files.pythonhosted.org/wheel.whl"},
            {"packagetype": "sdist", "url": "https://files.pythonhosted.org/sdist.tar.gz"},
        ]}).encode()
        mock_resp = MagicMock()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = api_json
        mock_urlopen.return_value = mock_resp
        url, kind = cd.pypi_sdist_url("pkg", "1.0")
        assert kind == "sdist"
        assert "sdist.tar.gz" in url

    @patch("urllib.request.urlopen")
    def test_wheel_fallback(self, mock_urlopen):
        api_json = json.dumps({"urls": [
            {"packagetype": "bdist_wheel", "filename": "pkg-1.0-py3-none-any.whl",
             "url": "https://files.pythonhosted.org/wheel.whl"},
        ]}).encode()
        mock_resp = MagicMock()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = api_json
        mock_urlopen.return_value = mock_resp
        url, kind = cd.pypi_sdist_url("pkg", "1.0")
        assert kind == "wheel"

    @patch("urllib.request.urlopen")
    def test_no_usable_artifact(self, mock_urlopen):
        api_json = json.dumps({"urls": [
            {"packagetype": "bdist_wheel", "filename": "pkg-1.0-cp311-linux.whl",
             "url": "https://x/platform.whl"},
        ]}).encode()
        mock_resp = MagicMock()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = api_json
        mock_urlopen.return_value = mock_resp
        with pytest.raises(ValueError, match="no sdist"):
            cd.pypi_sdist_url("pkg", "1.0")

    @patch("urllib.request.urlopen")
    def test_empty_urls(self, mock_urlopen):
        api_json = json.dumps({"urls": []}).encode()
        mock_resp = MagicMock()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = api_json
        mock_urlopen.return_value = mock_resp
        with pytest.raises(ValueError):
            cd.pypi_sdist_url("pkg", "1.0")


# ---------------------------------------------------------------- acquire
class TestAcquire:
    def test_already_exists(self, tmp_path):
        store = tmp_path / "store"
        target = store / "go" / "mod"
        target.mkdir(parents=True)
        dep = ("go", "example.com/foo", "v1.0", "https://x", "go/mod", "go")
        dest, st, url = cd.acquire(dep, str(store), str(tmp_path / "cache"))
        assert st == "ok"
        assert url == ""

    def test_unsupported_no_dest(self, tmp_path):
        dep = ("go", "foo", "v1", "https://x", "", "go")
        dest, st, url = cd.acquire(dep, str(tmp_path / "store"), str(tmp_path / "cache"))
        assert st == "unsupported"

    def test_unsupported_no_url(self, tmp_path):
        dep = ("go", "foo", "v1", "", "go/foo", "go")
        dest, st, url = cd.acquire(dep, str(tmp_path / "store"), str(tmp_path / "cache"))
        assert st == "unsupported"

    def test_cached_archive(self, tmp_path):
        store = tmp_path / "store"
        cache = tmp_path / "cache"
        cache.mkdir()

        blob = _make_zip(tmp_path, [
            ("mod@v1.0/main.go", "package main"),
        ])
        cache_key = "go_mod.zip"
        (cache / cache_key).write_bytes(blob)

        dep = ("go", "mod", "v1.0", "https://proxy.golang.org/mod/@v/v1.0.zip",
               "go/mod", "go")
        dest, st, url = cd.acquire(dep, str(store), str(cache))
        assert st == "ok"
        assert (store / "go" / "mod" / "main.go").exists()

    @patch("urllib.request.urlopen")
    def test_download_and_extract(self, mock_urlopen, tmp_path):
        store = tmp_path / "store"
        cache = tmp_path / "cache"
        cache.mkdir()

        blob = _make_tar_gz(tmp_path, [
            ("pkg-1.0/lib.py", "print('hello')"),
        ])
        mock_resp = MagicMock()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = blob
        mock_urlopen.return_value = mock_resp

        dep = ("npm", "pkg", "1.0", "https://registry.npmjs.org/pkg/-/pkg-1.0.tgz",
               "npm/pkg@1.0", "1")
        dest, st, url = cd.acquire(dep, str(store), str(cache))
        assert st == "ok"
        assert (store / "npm" / "pkg@1.0" / "lib.py").exists()
        assert (cache / "npm_pkg@1.0.tar.gz").exists()

    @patch("urllib.request.urlopen")
    def test_pypi_api_sdist(self, mock_urlopen, tmp_path):
        store = tmp_path / "store"
        cache = tmp_path / "cache"
        cache.mkdir()

        tarball = _make_tar_gz(tmp_path, [
            ("requests-2.31/setup.py", "from setuptools import setup"),
        ])
        api_json = json.dumps({"urls": [
            {"packagetype": "sdist", "url": "https://files.pythonhosted.org/sdist.tar.gz"},
        ]}).encode()

        call_count = [0]
        def fake_urlopen(req, timeout=None):
            call_count[0] += 1
            resp = MagicMock()
            resp.__enter__ = MagicMock(return_value=resp)
            resp.__exit__ = MagicMock(return_value=False)
            if call_count[0] == 1:
                resp.read.return_value = api_json
            else:
                resp.read.return_value = tarball
            return resp
        mock_urlopen.side_effect = fake_urlopen

        dep = ("pypi", "requests", "2.31", "pypi-api", "pypi/requests-2.31", "1")
        dest, st, url = cd.acquire(dep, str(store), str(cache))
        assert st == "ok"

    @patch("urllib.request.urlopen")
    def test_pypi_api_wheel(self, mock_urlopen, tmp_path):
        store = tmp_path / "store"
        cache = tmp_path / "cache"
        cache.mkdir()

        tarball = _make_tar_gz(tmp_path, [
            ("pkg-1.0/__init__.py", ""),
        ], name="whl.tar.gz")
        api_json = json.dumps({"urls": [
            {"packagetype": "bdist_wheel", "filename": "pkg-1.0-py3-none-any.whl",
             "url": "https://files.pythonhosted.org/pkg-1.0.tar.gz"},
        ]}).encode()

        call_count = [0]
        def fake_urlopen(req, timeout=None):
            call_count[0] += 1
            resp = MagicMock()
            resp.__enter__ = MagicMock(return_value=resp)
            resp.__exit__ = MagicMock(return_value=False)
            if call_count[0] == 1:
                resp.read.return_value = api_json
            else:
                resp.read.return_value = tarball
            return resp
        mock_urlopen.side_effect = fake_urlopen

        dep = ("pypi", "pkg", "1.0", "pypi-api", "pypi/pkg-1.0", "1")
        dest, st, url = cd.acquire(dep, str(store), str(cache))
        assert st == "ok-wheel"

    @patch("urllib.request.urlopen")
    def test_fetch_failure(self, mock_urlopen, tmp_path):
        mock_urlopen.side_effect = urllib.error.HTTPError(
            "url", 404, "Not Found", {}, None)
        dep = ("crates", "serde", "1.0", "https://x/serde-1.0.crate",
               "crates/serde-1.0", "1")
        with patch.object(cd.time, "sleep"):
            dest, st, url = cd.acquire(dep, str(tmp_path / "store"),
                                       str(tmp_path / "cache"))
        assert "failed" in st


# ---------------------------------------------------- extract edge cases
class TestExtractEdgeCases:
    def test_tar_with_directories(self, tmp_path):
        tpath = tmp_path / "archive.tar.gz"
        with tarfile.open(str(tpath), "w:gz") as t:
            d = tarfile.TarInfo(name="top/subdir")
            d.type = tarfile.DIRTYPE
            t.addfile(d)
            info = tarfile.TarInfo(name="top/subdir/file.txt")
            data = b"content"
            info.size = len(data)
            t.addfile(info, io.BytesIO(data))
        blob = open(str(tpath), "rb").read()
        dest = tmp_path / "out"
        cd.extract(blob, str(dest), "1", "http://example.com/x.tar.gz")
        assert (dest / "subdir" / "file.txt").read_text() == "content"

    def test_extract_error_cleanup(self, tmp_path):
        dest = tmp_path / "out"
        with pytest.raises(Exception):
            cd.extract(b"not a valid archive", str(dest), "1", "http://x.tar.gz")
        assert not (tmp_path / "out.tmp").exists()

    def test_tar_no_strip(self, tmp_path):
        blob = _make_tar_gz(tmp_path, [
            ("a/b/c.txt", "hello"),
        ])
        dest = tmp_path / "out"
        cd.extract(blob, str(dest), "0", "http://x/a.tar.gz")
        assert (dest / "a" / "b" / "c.txt").read_text() == "hello"


# -------------------------------------------------- main extra paths
class TestMainExtra:
    def test_main_with_out_flag(self, tmp_path, monkeypatch):
        stage = tmp_path / "stage"
        tree = stage / "git" / "comp"
        tree.mkdir(parents=True)
        (tree / "go.sum").write_text(
            "golang.org/x/net v0.25.0 h1:abc=\n"
            "golang.org/x/net v0.25.0/go.mod h1:def=\n"
        )
        (tree / "go.mod").write_text("module example.com/foo\ngo 1.21\n")
        out = tmp_path / "overlay"
        store = tmp_path / "store"
        monkeypatch.setattr("sys.argv", [
            "collect-deps.py", str(stage),
            "--out", str(out),
            "--store", str(store), "--jobs", "1", "--eco", "go",
        ])
        monkeypatch.setenv("CASKET_DEP_STORE", str(store))
        cd.main()
        assert (out / "meta" / "DEPS.tsv").exists()

    def test_main_eco_filter(self, tmp_path, monkeypatch):
        stage = tmp_path / "stage"
        tree = stage / "git" / "comp"
        tree.mkdir(parents=True)
        (tree / "go.sum").write_text(
            "golang.org/x/net v0.25.0 h1:abc=\n"
            "golang.org/x/net v0.25.0/go.mod h1:def=\n"
        )
        (tree / "go.mod").write_text("module example.com/foo\ngo 1.21\n")
        store = tmp_path / "store"
        monkeypatch.setattr("sys.argv", [
            "collect-deps.py", str(stage),
            "--store", str(store), "--jobs", "1", "--eco", "npm",
        ])
        monkeypatch.setenv("CASKET_DEP_STORE", str(store))
        result = cd.main()
        assert result == 0

    def test_main_broken_manifest(self, tmp_path, monkeypatch):
        stage = tmp_path / "stage"
        tree = stage / "git" / "bad-comp"
        tree.mkdir(parents=True)
        (tree / "Cargo.lock").write_text("not valid cargo lock content\n[[[")
        store = tmp_path / "store"
        monkeypatch.setattr("sys.argv", [
            "collect-deps.py", str(stage),
            "--store", str(store), "--jobs", "1", "--eco", "crates",
        ])
        monkeypatch.setenv("CASKET_DEP_STORE", str(store))
        result = cd.main()
        assert result == 0

    def test_main_symlink_tree_skipped(self, tmp_path, monkeypatch):
        stage = tmp_path / "stage"
        git = stage / "git"
        git.mkdir(parents=True)
        real = tmp_path / "real"
        real.mkdir()
        os.symlink(str(real), str(git / "symlinked"))
        store = tmp_path / "store"
        monkeypatch.setattr("sys.argv", [
            "collect-deps.py", str(stage),
            "--store", str(store), "--jobs", "1",
        ])
        monkeypatch.setenv("CASKET_DEP_STORE", str(store))
        result = cd.main()
        assert result == 0
