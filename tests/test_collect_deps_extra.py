"""Extra tests for scripts/collect-deps.py — fetch, pypi_sdist_url,
extract, acquire, link_tree, and main().

Network-free: all HTTP calls use unittest.mock.patch("urllib.request.urlopen").
Run: pytest tests/test_collect_deps_extra.py
"""
import importlib.util
import io
import json
import os
import pathlib
import shutil
import sys
import tarfile
import urllib.error
import zipfile
from unittest.mock import patch, MagicMock

import pytest

_SCRIPTS = str(pathlib.Path(__file__).resolve().parent.parent / "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import deplib  # noqa: E402

_SPEC = importlib.util.spec_from_file_location(
    "collect_deps",
    pathlib.Path(__file__).resolve().parent.parent / "scripts" / "collect-deps.py")
cd = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(cd)


def _make_targz(files):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, content in files.items():
            info = tarfile.TarInfo(name=f"top-dir/{name}")
            data = content.encode() if isinstance(content, str) else content
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _make_zip(files, prefix="mod@v1.0.0/"):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in files.items():
            zf.writestr(prefix + name, content)
    return buf.getvalue()


# ------------------------------------------------------------------ fetch
class TestFetch:
    @patch("urllib.request.urlopen")
    def test_success(self, mock_urlopen):
        resp = MagicMock()
        resp.__enter__ = MagicMock(return_value=resp)
        resp.__exit__ = MagicMock(return_value=False)
        resp.read.return_value = b"data"
        mock_urlopen.return_value = resp
        assert cd.fetch("https://example.com/file.tar.gz") == b"data"

    @patch("urllib.request.urlopen")
    def test_retry_on_network_error(self, mock_urlopen):
        resp = MagicMock()
        resp.__enter__ = MagicMock(return_value=resp)
        resp.__exit__ = MagicMock(return_value=False)
        resp.read.return_value = b"ok"
        mock_urlopen.side_effect = [OSError("fail"), resp]
        result = cd.fetch("https://example.com", retries=2)
        assert result == b"ok"

    @patch("urllib.request.urlopen")
    def test_404_not_retried(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.HTTPError(
            "url", 404, "Not Found", {}, None)
        with pytest.raises(urllib.error.HTTPError):
            cd.fetch("https://example.com", retries=3)
        assert mock_urlopen.call_count == 1

    @patch("urllib.request.urlopen")
    def test_all_retries_fail(self, mock_urlopen):
        mock_urlopen.side_effect = OSError("down")
        with pytest.raises(OSError, match="down"):
            cd.fetch("https://example.com", retries=2)
        assert mock_urlopen.call_count == 2


# -------------------------------------------------------- pypi_sdist_url
class TestPypiSdistUrl:
    @patch("urllib.request.urlopen")
    def test_sdist_preferred(self, mock_urlopen):
        api_resp = {"urls": [
            {"packagetype": "bdist_wheel", "url": "https://pypi.org/wheel.whl",
             "filename": "pkg-1.0-py3-none-any.whl"},
            {"packagetype": "sdist", "url": "https://pypi.org/sdist.tar.gz"},
        ]}
        body = json.dumps(api_resp).encode()
        resp = MagicMock()
        resp.__enter__ = MagicMock(return_value=resp)
        resp.__exit__ = MagicMock(return_value=False)
        resp.read.return_value = body
        mock_urlopen.return_value = resp
        url, kind = cd.pypi_sdist_url("pkg", "1.0")
        assert url == "https://pypi.org/sdist.tar.gz"
        assert kind == "sdist"

    @patch("urllib.request.urlopen")
    def test_pure_wheel_fallback(self, mock_urlopen):
        api_resp = {"urls": [
            {"packagetype": "bdist_wheel", "url": "https://pypi.org/wheel.whl",
             "filename": "pkg-1.0-py3-none-any.whl"},
        ]}
        body = json.dumps(api_resp).encode()
        resp = MagicMock()
        resp.__enter__ = MagicMock(return_value=resp)
        resp.__exit__ = MagicMock(return_value=False)
        resp.read.return_value = body
        mock_urlopen.return_value = resp
        url, kind = cd.pypi_sdist_url("pkg", "1.0")
        assert kind == "wheel"

    @patch("urllib.request.urlopen")
    def test_no_sdist_no_pure_wheel_raises(self, mock_urlopen):
        api_resp = {"urls": [
            {"packagetype": "bdist_wheel", "url": "https://pypi.org/wheel.whl",
             "filename": "pkg-1.0-cp311-linux.whl"},
        ]}
        body = json.dumps(api_resp).encode()
        resp = MagicMock()
        resp.__enter__ = MagicMock(return_value=resp)
        resp.__exit__ = MagicMock(return_value=False)
        resp.read.return_value = body
        mock_urlopen.return_value = resp
        with pytest.raises(ValueError, match="no sdist"):
            cd.pypi_sdist_url("pkg", "1.0")


# -------------------------------------------------------------- extract
class TestExtract:
    def test_tar_strip_1(self, tmp_path):
        blob = _make_targz({"src/main.py": "hello"})
        dest = str(tmp_path / "out")
        cd.extract(blob, dest, "1", "https://example.com/a.tar.gz")
        assert (tmp_path / "out" / "src" / "main.py").read_text() == "hello"

    def test_zip_strip_go(self, tmp_path):
        blob = _make_zip({"main.go": "package main"},
                         prefix="github.com/org/mod@v1.0.0/")
        dest = str(tmp_path / "out")
        cd.extract(blob, dest, "go", "https://proxy.golang.org/mod.zip")
        assert (tmp_path / "out" / "main.go").read_text() == "package main"

    def test_zip_strip_1(self, tmp_path):
        blob = _make_zip({"file.txt": "content"}, prefix="pkg-1.0/")
        dest = str(tmp_path / "out")
        cd.extract(blob, dest, "1", "https://example.com/a.zip")
        assert (tmp_path / "out" / "file.txt").read_text() == "content"

    def test_path_traversal_rejected(self, tmp_path):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            info = tarfile.TarInfo(name="top/../../etc/passwd")
            data = b"evil"
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
        dest = str(tmp_path / "out")
        cd.extract(buf.getvalue(), dest, "1", "https://example.com/a.tar.gz")
        assert not (tmp_path / "etc").exists()

    def test_tmp_cleaned_on_failure(self, tmp_path):
        dest = str(tmp_path / "out")
        with pytest.raises(Exception):
            cd.extract(b"not an archive", dest, "1", "https://example.com/a.tar.gz")
        assert not os.path.exists(dest + ".tmp")


# -------------------------------------------------------------- acquire
class TestAcquire:
    def test_cached_in_store(self, tmp_path):
        store = tmp_path / "store"
        cache = tmp_path / "cache"
        target = store / "crates" / "tokio-1.0"
        target.mkdir(parents=True)
        (target / "lib.rs").write_text("fn main()")
        cache.mkdir()
        dep = ("crates", "tokio", "1.0", "https://crates.io/dl", "crates/tokio-1.0", "1")
        dest, status, url = cd.acquire(dep, str(store), str(cache))
        assert status == "ok"
        assert dest == "crates/tokio-1.0"

    def test_unsupported_no_url(self, tmp_path):
        store = tmp_path / "store"
        cache = tmp_path / "cache"
        store.mkdir()
        cache.mkdir()
        dep = ("crates", "weird", "1.0", "", "crates/weird-1.0", "0")
        dest, status, url = cd.acquire(dep, str(store), str(cache))
        assert status == "unsupported"

    @patch("urllib.request.urlopen")
    def test_fetch_and_extract(self, mock_urlopen, tmp_path):
        store = tmp_path / "store"
        cache = tmp_path / "cache"
        store.mkdir()
        cache.mkdir()
        blob = _make_targz({"lib.rs": "fn main()"})
        resp = MagicMock()
        resp.__enter__ = MagicMock(return_value=resp)
        resp.__exit__ = MagicMock(return_value=False)
        resp.read.return_value = blob
        mock_urlopen.return_value = resp
        dep = ("crates", "tokio", "1.0",
               "https://static.crates.io/crates/tokio/tokio-1.0.crate",
               "crates/tokio-1.0", "1")
        dest, status, url = cd.acquire(dep, str(store), str(cache))
        assert status == "ok"
        assert (store / "crates" / "tokio-1.0" / "lib.rs").exists()
        assert (cache / "crates_tokio-1.0.tar.gz").exists()

    @patch("urllib.request.urlopen")
    def test_fetch_failure(self, mock_urlopen, tmp_path):
        store = tmp_path / "store"
        cache = tmp_path / "cache"
        store.mkdir()
        cache.mkdir()
        mock_urlopen.side_effect = urllib.error.HTTPError(
            "url", 404, "Not Found", {}, None)
        dep = ("crates", "gone", "1.0",
               "https://static.crates.io/crates/gone/gone-1.0.crate",
               "crates/gone-1.0", "1")
        dest, status, url = cd.acquire(dep, str(store), str(cache))
        assert "failed:" in status

    def test_cached_in_cache_file(self, tmp_path):
        store = tmp_path / "store"
        cache = tmp_path / "cache"
        store.mkdir()
        cache.mkdir()
        blob = _make_targz({"main.go": "package main"})
        (cache / "crates_pkg-1.0.tar.gz").write_bytes(blob)
        dep = ("crates", "pkg", "1.0", "https://example.com/dl", "crates/pkg-1.0", "1")
        dest, status, url = cd.acquire(dep, str(store), str(cache))
        assert status == "ok"
        assert (store / "crates" / "pkg-1.0" / "main.go").exists()


# ------------------------------------------------------------ link_tree
class TestLinkTree:
    def test_hardlinks_files(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        (src / "a.txt").write_text("hello")
        sub = src / "sub"
        sub.mkdir()
        (sub / "b.txt").write_text("world")
        dst = tmp_path / "dst"
        cd.link_tree(str(src), str(dst))
        assert (dst / "a.txt").read_text() == "hello"
        assert (dst / "sub" / "b.txt").read_text() == "world"

    def test_existing_files_not_overwritten(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        (src / "a.txt").write_text("new")
        dst = tmp_path / "dst"
        dst.mkdir()
        (dst / "a.txt").write_text("old")
        cd.link_tree(str(src), str(dst))
        assert (dst / "a.txt").read_text() == "old"


# ---------------------------------------------------------------- main()
class TestMain:
    def _setup(self, tmp_path, monkeypatch, trees=None):
        stage = tmp_path / "stage"
        gitroot = stage / "git"
        gitroot.mkdir(parents=True)
        (stage / "meta").mkdir(parents=True)
        store = tmp_path / "store"
        cache = tmp_path / "cache"

        if trees:
            for comp, files in trees.items():
                comp_dir = gitroot / comp
                comp_dir.mkdir()
                for name, content in files.items():
                    path = comp_dir / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(content)

        monkeypatch.setenv("CASKET_DEP_STORE", str(store))
        monkeypatch.setenv("CASKET_DEP_CACHE", str(cache))
        monkeypatch.setattr(sys, "argv", [
            "collect-deps.py", str(stage),
            "--store", str(store), "--jobs", "1"])
        return stage, store, cache

    def test_no_git_dir(self, tmp_path, monkeypatch):
        stage = tmp_path / "stage"
        stage.mkdir()
        monkeypatch.setenv("CASKET_DEP_STORE", str(tmp_path / "store"))
        monkeypatch.setattr(sys, "argv", [
            "collect-deps.py", str(stage),
            "--store", str(tmp_path / "store")])
        assert cd.main() == 0

    def test_no_manifests(self, tmp_path, monkeypatch):
        stage, store, cache = self._setup(tmp_path, monkeypatch, {
            "my-comp": {"main.go": "package main"}
        })
        assert cd.main() == 0

    def test_vendored_go_skipped(self, tmp_path, monkeypatch):
        stage, store, cache = self._setup(tmp_path, monkeypatch, {
            "my-comp": {"go.sum": "golang.org/x/net v0.38.0 h1:sha\n"}
        })
        (stage / "git" / "my-comp" / "vendor").mkdir()
        assert cd.main() == 0
        assert not (stage / "deps").exists()

    def test_resolves_and_writes_meta(self, tmp_path, monkeypatch):
        reqs = "urllib3==2.2.1\n"
        stage, store, cache = self._setup(tmp_path, monkeypatch, {
            "my-comp": {"requirements.txt": reqs}
        })
        monkeypatch.setattr(cd, "acquire",
                            lambda dep, s, c: (dep[4], "ok", dep[3]))
        rc = cd.main()
        assert rc == 0
        tsv = (stage / "meta" / "DEPS.tsv").read_text()
        assert "urllib3" in tsv
        assert "my-comp" in tsv

    def test_failed_acquire_in_uncovered(self, tmp_path, monkeypatch):
        reqs = "urllib3==2.2.1\n"
        stage, store, cache = self._setup(tmp_path, monkeypatch, {
            "my-comp": {"requirements.txt": reqs}
        })
        monkeypatch.setattr(cd, "acquire",
                            lambda dep, s, c: (dep[4], "failed:HTTPError", dep[3]))
        rc = cd.main()
        assert rc == 0
        unc = (stage / "meta" / "DEPS-uncovered.txt").read_text()
        assert "urllib3" in unc

    def test_eco_filter(self, tmp_path, monkeypatch):
        stage, store, cache = self._setup(tmp_path, monkeypatch, {
            "my-comp": {"requirements.txt": "flask==2.0.0\n"}
        })
        monkeypatch.setattr(sys, "argv", [
            "collect-deps.py", str(stage),
            "--store", str(store), "--eco", "go,crates", "--jobs", "1"])
        rc = cd.main()
        assert rc == 0
        assert not (stage / "meta" / "DEPS.tsv").exists() or \
            "flask" not in (stage / "meta" / "DEPS.tsv").read_text()

    def test_broken_manifest_in_uncovered(self, tmp_path, monkeypatch):
        stage, store, cache = self._setup(tmp_path, monkeypatch, {
            "my-comp": {"package-lock.json": "not valid json {{{"}
        })
        rc = cd.main()
        assert rc == 0

    def test_out_flag(self, tmp_path, monkeypatch):
        reqs = "urllib3==2.2.1\n"
        stage, store, cache = self._setup(tmp_path, monkeypatch, {
            "my-comp": {"requirements.txt": reqs}
        })
        out = tmp_path / "out"
        monkeypatch.setattr(sys, "argv", [
            "collect-deps.py", str(stage),
            "--out", str(out),
            "--store", str(store), "--jobs", "1"])
        monkeypatch.setattr(cd, "acquire",
                            lambda dep, s, c: (dep[4], "ok", dep[3]))
        rc = cd.main()
        assert rc == 0
        assert (out / "meta" / "DEPS.tsv").exists()


# ------------------------------------------------------- extract: odd members
class TestExtractOddMembers:
    def test_zip_directory_entries_skipped(self, tmp_path):
        """Explicit "dir/" entries carry no bytes and must not become files."""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("top/", "")
            z.writestr("top/sub/", "")
            z.writestr("top/sub/a.txt", "hello")
        dest = tmp_path / "out"
        cd.extract(buf.getvalue(), str(dest), "1", "x.zip")
        assert (dest / "sub" / "a.txt").read_text() == "hello"
        assert not (dest / "sub").is_file()

    def test_tar_symlink_member_skipped(self, tmp_path):
        """A symlink is neither isfile() nor isdir() — dropped, not followed."""
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            data = b"real"
            info = tarfile.TarInfo("top/real.txt")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
            link = tarfile.TarInfo("top/link.txt")
            link.type = tarfile.SYMTYPE
            link.linkname = "/etc/passwd"
            tf.addfile(link)
        dest = tmp_path / "out"
        cd.extract(buf.getvalue(), str(dest), "1", "x.tar.gz")
        assert (dest / "real.txt").read_text() == "real"
        assert not (dest / "link.txt").exists()

    def test_tar_unextractable_file_skipped(self, tmp_path, monkeypatch):
        """extractfile() returning None must not blow up the whole archive."""
        blob = _make_targz({"a.txt": "A", "b.txt": "B"})
        real_extractfile = tarfile.TarFile.extractfile

        def fake_extractfile(self, member):
            if member.name.endswith("a.txt"):
                return None
            return real_extractfile(self, member)

        monkeypatch.setattr(tarfile.TarFile, "extractfile", fake_extractfile)
        dest = tmp_path / "out"
        cd.extract(blob, str(dest), "1", "x.tar.gz")
        assert not (dest / "a.txt").exists()
        assert (dest / "b.txt").read_text() == "B"


# ------------------------------------------------- link_tree hardlink fallback
class TestLinkTreeFallback:
    def test_copies_when_hardlink_refused(self, tmp_path, monkeypatch):
        """Cross-filesystem store/stage: os.link raises EXDEV, copy2 saves it."""
        src = tmp_path / "src"
        src.mkdir()
        (src / "a.txt").write_text("payload")
        dst = tmp_path / "dst"

        def no_link(*a, **kw):
            raise OSError(18, "Invalid cross-device link")

        monkeypatch.setattr(cd.os, "link", no_link)
        cd.link_tree(str(src), str(dst))
        assert (dst / "a.txt").read_text() == "payload"
        assert os.stat(dst / "a.txt").st_nlink == 1


# --------------------------------------------------------- main(): warn + meta
class TestMainStoreWarning:
    def test_warns_when_dep_store_unset(self, tmp_path, monkeypatch, capsys):
        """The 2026-07-31 incident: an unset store silently filled the checkout."""
        stage = tmp_path / "stage"
        (stage / "git").mkdir(parents=True)
        monkeypatch.delenv("CASKET_DEP_STORE", raising=False)
        monkeypatch.setenv("CASKET_DEP_CACHE", str(tmp_path / "cache"))
        monkeypatch.setattr(sys, "argv", [
            "collect-deps.py", str(stage), "--store", str(tmp_path / "store")])
        assert cd.main() == 0
        assert "CASKET_DEP_STORE unset" in capsys.readouterr().err


class TestMainDegradation:
    def _stage(self, tmp_path, monkeypatch, comp, files):
        stage = tmp_path / "stage"
        comp_dir = stage / "git" / comp
        comp_dir.mkdir(parents=True)
        for name, content in files.items():
            (comp_dir / name).write_text(content)
        monkeypatch.setenv("CASKET_DEP_STORE", str(tmp_path / "store"))
        monkeypatch.setenv("CASKET_DEP_CACHE", str(tmp_path / "cache"))
        monkeypatch.setattr(sys, "argv", [
            "collect-deps.py", str(stage),
            "--store", str(tmp_path / "store"), "--jobs", "1"])
        return stage

    def test_tree_walk_failure_is_reported_not_fatal(self, tmp_path, monkeypatch,
                                                     capsys):
        """A blown-up scan_tree costs that tree only; the run continues."""
        stage = self._stage(tmp_path, monkeypatch, "bad-comp",
                            {"requirements.txt": "urllib3==2.2.1\n"})
        (stage / "git" / "good-comp").mkdir()
        (stage / "git" / "good-comp" / "requirements.txt").write_text(
            "certifi==2024.2.2\n")

        real_scan = cd.deplib.scan_tree

        def boom(tree, **kw):
            if tree.endswith("bad-comp"):
                raise RuntimeError("walk exploded")
            return real_scan(tree, **kw)

        monkeypatch.setattr(cd.deplib, "scan_tree", boom)
        monkeypatch.setattr(cd, "acquire", lambda dep, s, c: (dep[4], "ok", dep[3]))

        assert cd.main() == 0
        err = capsys.readouterr().err
        assert "bad-comp: scan failed" in err
        assert "failed to parse" in err

        unc = (stage / "meta" / "DEPS-uncovered.txt").read_text()
        assert "RuntimeError: walk exploded" in unc
        assert "certifi" in (stage / "meta" / "DEPS.tsv").read_text()

    def test_barren_manifest_listed_as_no_pinned_versions(self, tmp_path,
                                                          monkeypatch):
        """`requests>=2` has no single right version to fetch offline."""
        stage = self._stage(tmp_path, monkeypatch, "comp",
                            {"requirements.txt": "requests>=2\n",
                             "go.sum": "golang.org/x/net v0.38.0 h1:sha\n"})
        monkeypatch.setattr(cd, "acquire", lambda dep, s, c: (dep[4], "ok", dep[3]))
        assert cd.main() == 0
        unc = (stage / "meta" / "DEPS-uncovered.txt").read_text()
        assert "no-pinned-versions" in unc
        assert "requirements.txt" in unc

    def test_stale_uncovered_file_removed(self, tmp_path, monkeypatch):
        """A clean run must not leave the previous run's uncovered list behind."""
        stage = self._stage(tmp_path, monkeypatch, "comp",
                            {"requirements.txt": "urllib3==2.2.1\n"})
        meta = stage / "meta"
        meta.mkdir(parents=True, exist_ok=True)
        unc = meta / "DEPS-uncovered.txt"
        unc.write_text("# stale content from an earlier run\n")

        monkeypatch.setattr(cd, "acquire", lambda dep, s, c: (dep[4], "ok", dep[3]))
        assert cd.main() == 0
        assert not unc.exists()

    def test_progress_logged_every_500_archives(self, tmp_path, monkeypatch,
                                                capsys):
        self._stage(tmp_path, monkeypatch, "comp", {
            "requirements.txt": "".join(f"pkg{i}==1.0.0\n" for i in range(501))})
        monkeypatch.setattr(cd, "acquire", lambda dep, s, c: (dep[4], "ok", dep[3]))
        monkeypatch.setattr(cd, "link_tree", lambda src, dst: None)
        assert cd.main() == 0
        assert "  500/501" in capsys.readouterr().err

    def test_resolver_exception_recorded_per_manifest(self, tmp_path, monkeypatch):
        """scan_tree yields the exception itself; it lands in the broken list."""
        stage = self._stage(tmp_path, monkeypatch, "comp",
                            {"go.sum": "golang.org/x/net v0.38.0 h1:sha\n",
                             "requirements.txt": "urllib3==2.2.1\n"})

        def boom(text):
            raise ValueError("resolver exploded")

        monkeypatch.setattr(
            cd.deplib, "MANIFESTS",
            [("go.sum", boom)] + [m for m in cd.deplib.MANIFESTS
                                  if m[0] != "go.sum"])
        monkeypatch.setattr(cd, "acquire", lambda dep, s, c: (dep[4], "ok", dep[3]))

        assert cd.main() == 0
        unc = (stage / "meta" / "DEPS-uncovered.txt").read_text()
        assert "ValueError: resolver exploded" in unc
        assert "go.sum" in unc


class TestExtractGoPrefixDetection:
    def test_go_zip_without_a_path_separator_keeps_full_names(self, tmp_path):
        """A module zip whose only entry is the top-level marker: no prefix."""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("example.com/m@v1.0.0", "marker")
        dest = tmp_path / "out"
        cd.extract(buf.getvalue(), str(dest), "go", "m.zip")
        assert (dest / "example.com" / "m@v1.0.0").read_text() == "marker"

    def test_zip_with_unknown_strip_keeps_full_paths(self, tmp_path):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("top/a.txt", "A")
        dest = tmp_path / "out"
        cd.extract(buf.getvalue(), str(dest), "0", "x.zip")
        assert (dest / "top" / "a.txt").read_text() == "A"
