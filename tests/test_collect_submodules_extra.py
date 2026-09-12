"""Extra tests for scripts/collect-submodules.py — fetch, gitlinks, extract,
acquire, and the full main() loop.

Network-free: all HTTP calls are mocked via unittest.mock.patch.
Run: pytest tests/test_collect_submodules_extra.py
"""
import gzip
import importlib.util
import io
import json
import os
import pathlib
import sys
import tarfile
import urllib.error

from unittest.mock import MagicMock, patch

import pytest

_SCRIPTS = str(pathlib.Path(__file__).resolve().parent.parent / "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import submodulelib as sml  # noqa: E402

_SPEC = importlib.util.spec_from_file_location(
    "collect_submodules",
    pathlib.Path(__file__).resolve().parent.parent / "scripts" / "collect-submodules.py")
cs = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(cs)


def _make_targz(files):
    """Build an in-memory .tar.gz with a top-level directory prefix."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, content in files.items():
            info = tarfile.TarInfo(name=f"repo-abc123/{name}")
            data = content.encode() if isinstance(content, str) else content
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
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
        assert cs.fetch("https://example.com/file.tar.gz") == b"data"

    @patch("urllib.request.urlopen")
    def test_retry_on_500(self, mock_urlopen):
        err = urllib.error.HTTPError("url", 500, "ISE", {}, None)
        resp = MagicMock()
        resp.__enter__ = MagicMock(return_value=resp)
        resp.__exit__ = MagicMock(return_value=False)
        resp.read.return_value = b"ok"
        mock_urlopen.side_effect = [err, resp]
        result = cs.fetch("https://example.com", retries=2)
        assert result == b"ok"
        assert mock_urlopen.call_count == 2

    @patch("urllib.request.urlopen")
    def test_403_not_retried(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.HTTPError(
            "url", 403, "Forbidden", {}, None)
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            cs.fetch("https://example.com", retries=3)
        assert exc_info.value.code == 403
        assert mock_urlopen.call_count == 1

    @patch("urllib.request.urlopen")
    def test_404_not_retried(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.HTTPError(
            "url", 404, "Not Found", {}, None)
        with pytest.raises(urllib.error.HTTPError):
            cs.fetch("https://example.com", retries=3)
        assert mock_urlopen.call_count == 1

    @patch("urllib.request.urlopen")
    def test_451_not_retried(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.HTTPError(
            "url", 451, "Unavailable", {}, None)
        with pytest.raises(urllib.error.HTTPError):
            cs.fetch("https://example.com", retries=3)
        assert mock_urlopen.call_count == 1

    @patch("urllib.request.urlopen")
    def test_all_retries_exhausted(self, mock_urlopen):
        mock_urlopen.side_effect = OSError("network down")
        with pytest.raises(OSError, match="network down"):
            cs.fetch("https://example.com", retries=2)
        assert mock_urlopen.call_count == 2

    @patch("urllib.request.urlopen")
    def test_custom_headers(self, mock_urlopen):
        resp = MagicMock()
        resp.__enter__ = MagicMock(return_value=resp)
        resp.__exit__ = MagicMock(return_value=False)
        resp.read.return_value = b"ok"
        mock_urlopen.return_value = resp
        cs.fetch("https://example.com", headers={"Authorization": "Bearer tok"})
        req = mock_urlopen.call_args[0][0]
        assert req.get_header("Authorization") == "Bearer tok"
        assert req.get_header("User-agent") == cs.UA


# ---------------------------------------------------------------- gitlinks
class TestGitlinks:
    @patch("urllib.request.urlopen")
    def test_success(self, mock_urlopen):
        tree_json = {"tree": [
            {"mode": "160000", "path": "vendor/docsy", "sha": "abc123"},
            {"mode": "100644", "path": "main.go", "sha": "def456"},
        ]}
        body = json.dumps(tree_json).encode()
        resp = MagicMock()
        resp.__enter__ = MagicMock(return_value=resp)
        resp.__exit__ = MagicMock(return_value=False)
        resp.read.return_value = body
        mock_urlopen.return_value = resp
        result = cs.gitlinks("org/repo", "abc123", "tok")
        assert result == {"vendor/docsy": "abc123"}

    @patch("urllib.request.urlopen")
    def test_error_returns_empty(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.HTTPError(
            "url", 404, "Not Found", {}, None)
        result = cs.gitlinks("org/repo", "abc123", "tok")
        assert result == {}

    @patch("urllib.request.urlopen")
    def test_no_token(self, mock_urlopen):
        tree_json = {"tree": []}
        body = json.dumps(tree_json).encode()
        resp = MagicMock()
        resp.__enter__ = MagicMock(return_value=resp)
        resp.__exit__ = MagicMock(return_value=False)
        resp.read.return_value = body
        mock_urlopen.return_value = resp
        cs.gitlinks("org/repo", "abc123", "")
        req = mock_urlopen.call_args[0][0]
        assert req.get_header("Authorization") is None


# ---------------------------------------------------------------- extract
class TestExtract:
    def test_strips_top_level_dir(self, tmp_path):
        blob = _make_targz({"src/main.py": "print('hi')", "README.md": "hello"})
        target = str(tmp_path / "out")
        cs.extract(blob, target)
        assert (tmp_path / "out" / "src" / "main.py").read_text() == "print('hi')"
        assert (tmp_path / "out" / "README.md").read_text() == "hello"

    def test_skips_top_level_only_entry(self, tmp_path):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            info = tarfile.TarInfo(name="repo-abc123/")
            info.type = tarfile.DIRTYPE
            tf.addfile(info)
            info2 = tarfile.TarInfo(name="repo-abc123/file.txt")
            data = b"content"
            info2.size = len(data)
            tf.addfile(info2, io.BytesIO(data))
        target = str(tmp_path / "out")
        cs.extract(buf.getvalue(), target)
        assert (tmp_path / "out" / "file.txt").read_text() == "content"

    def test_rejects_path_traversal(self, tmp_path):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            info = tarfile.TarInfo(name="repo-abc123/../../etc/passwd")
            data = b"evil"
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
        target = str(tmp_path / "out")
        cs.extract(buf.getvalue(), target)
        assert not (tmp_path / "etc").exists()

    def test_replaces_existing_target(self, tmp_path):
        target = tmp_path / "out"
        target.mkdir()
        (target / "old.txt").write_text("stale")
        blob = _make_targz({"new.txt": "fresh"})
        cs.extract(blob, str(target))
        assert not (target / "old.txt").exists()
        assert (target / "new.txt").read_text() == "fresh"


# ---------------------------------------------------------------- acquire
class TestAcquire:
    def test_cached_in_store(self, tmp_path):
        store = tmp_path / "store"
        cache = tmp_path / "cache"
        store.mkdir()
        cache.mkdir()
        target = store / "org_repo@abc123"
        target.mkdir()
        (target / "file.txt").write_text("cached")
        pin = sml.Pin("sub", "org/repo", "abc123", True,
                      "https://github.com/org/repo/archive/abc123.tar.gz")
        dest, status = cs.acquire(pin, str(store), str(cache))
        assert status == "ok-cached"
        assert dest == "org_repo@abc123"

    def test_cached_in_cache_file(self, tmp_path):
        store = tmp_path / "store"
        cache = tmp_path / "cache"
        store.mkdir()
        cache.mkdir()
        blob = _make_targz({"main.go": "package main"})
        (cache / "org_repo@abc123.tar.gz").write_bytes(blob)
        pin = sml.Pin("sub", "org/repo", "abc123", True,
                      "https://github.com/org/repo/archive/abc123.tar.gz")
        dest, status = cs.acquire(pin, str(store), str(cache))
        assert status == "ok"
        assert (store / dest / "main.go").exists()

    @patch("urllib.request.urlopen")
    def test_fetch_and_cache(self, mock_urlopen, tmp_path):
        store = tmp_path / "store"
        cache = tmp_path / "cache"
        store.mkdir()
        cache.mkdir()
        blob = _make_targz({"main.go": "package main"})
        resp = MagicMock()
        resp.__enter__ = MagicMock(return_value=resp)
        resp.__exit__ = MagicMock(return_value=False)
        resp.read.return_value = blob
        mock_urlopen.return_value = resp
        pin = sml.Pin("sub", "org/repo", "abc123", True,
                      "https://github.com/org/repo/archive/abc123.tar.gz")
        dest, status = cs.acquire(pin, str(store), str(cache))
        assert status == "ok"
        assert (store / dest / "main.go").exists()
        assert (cache / "org_repo@abc123.tar.gz").exists()

    @patch("urllib.request.urlopen")
    def test_fetch_failure(self, mock_urlopen, tmp_path):
        store = tmp_path / "store"
        cache = tmp_path / "cache"
        store.mkdir()
        cache.mkdir()
        mock_urlopen.side_effect = urllib.error.HTTPError(
            "url", 404, "Not Found", {}, None)
        pin = sml.Pin("sub", "org/repo", "abc123", True,
                      "https://github.com/org/repo/archive/abc123.tar.gz")
        dest, status = cs.acquire(pin, str(store), str(cache))
        assert "failed:" in status
        assert "HTTPError" in status


# --------------------------------------------------------------- main flow
class TestMainFull:
    def _setup_stage(self, tmp_path, gitmodules_content, files=None):
        stage = tmp_path / "stage"
        git = stage / "git" / "my-comp"
        git.mkdir(parents=True)
        (git / ".gitmodules").write_text(gitmodules_content)
        meta = stage / "meta"
        meta.mkdir(parents=True)
        if files:
            for name, content in files.items():
                path = git / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)
        return stage

    def test_no_submodules_found(self, tmp_path, monkeypatch):
        stage = tmp_path / "stage"
        git = stage / "git" / "my-comp"
        git.mkdir(parents=True)
        store = tmp_path / "store"
        monkeypatch.setattr(sys, "argv", [
            "collect-submodules.py", str(stage), "--store", str(store)])
        monkeypatch.setenv("CASKET_SUBMODULE_STORE", str(store))
        assert cs.main() == 0

    def test_with_empty_submodule(self, tmp_path, monkeypatch):
        gm = (
            '[submodule "child"]\n'
            '\tpath = child\n'
            '\turl = https://github.com/org/child.git\n'
            '\tbranch = main\n'
        )
        stage = self._setup_stage(tmp_path, gm)
        (stage / "git" / "my-comp" / "child").mkdir()
        store = tmp_path / "store"
        blob = _make_targz({"main.go": "package main"})

        monkeypatch.setattr(sys, "argv", [
            "collect-submodules.py", str(stage), "--store", str(store)])
        monkeypatch.setenv("CASKET_SUBMODULE_STORE", str(store))
        monkeypatch.setenv("GITHUB_TOKEN", "fake")
        monkeypatch.setattr(cs, "fetch", lambda url, **kw: blob)

        cs.main()
        tsv = (stage / "meta" / "SUBMODULES.tsv").read_text()
        assert "my-comp" in tsv
        assert "child" in tsv

    def test_writes_uncovered(self, tmp_path, monkeypatch):
        gm = (
            '[submodule "gl"]\n'
            '\tpath = gl\n'
            '\turl = https://gitlab.com/nvidia/something.git\n'
        )
        stage = self._setup_stage(tmp_path, gm)
        (stage / "git" / "my-comp" / "gl").mkdir()
        store = tmp_path / "store"

        monkeypatch.setattr(sys, "argv", [
            "collect-submodules.py", str(stage), "--store", str(store)])
        monkeypatch.setenv("CASKET_SUBMODULE_STORE", str(store))
        monkeypatch.setenv("GITHUB_TOKEN", "fake")

        cs.main()
        unc = (stage / "meta" / "SUBMODULES-uncovered.txt").read_text()
        assert "not-github" in unc

    def test_out_flag_writes_elsewhere(self, tmp_path, monkeypatch):
        gm = (
            '[submodule "child"]\n'
            '\tpath = child\n'
            '\turl = https://github.com/org/child.git\n'
            '\tbranch = main\n'
        )
        stage = self._setup_stage(tmp_path, gm)
        (stage / "git" / "my-comp" / "child").mkdir()
        out = tmp_path / "out"
        store = tmp_path / "store"
        blob = _make_targz({"main.go": "package main"})

        monkeypatch.setattr(sys, "argv", [
            "collect-submodules.py", str(stage),
            "--out", str(out), "--store", str(store)])
        monkeypatch.setenv("CASKET_SUBMODULE_STORE", str(store))
        monkeypatch.setenv("GITHUB_TOKEN", "fake")
        monkeypatch.setattr(cs, "fetch", lambda url, **kw: blob)

        cs.main()
        assert (out / "meta" / "SUBMODULES.tsv").exists()

    def test_no_github_token_warning(self, tmp_path, monkeypatch, capsys):
        stage = tmp_path / "stage"
        git = stage / "git" / "my-comp"
        git.mkdir(parents=True)
        store = tmp_path / "store"

        monkeypatch.setattr(sys, "argv", [
            "collect-submodules.py", str(stage), "--store", str(store)])
        monkeypatch.setenv("CASKET_SUBMODULE_STORE", str(store))
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("GH_TOKEN", raising=False)

        cs.main()
        err = capsys.readouterr().err
        assert "GITHUB_TOKEN" in err or "GH_TOKEN" in err

    def test_failed_acquire_goes_to_uncovered(self, tmp_path, monkeypatch):
        gm = (
            '[submodule "child"]\n'
            '\tpath = child\n'
            '\turl = https://github.com/org/child.git\n'
            '\tbranch = main\n'
        )
        stage = self._setup_stage(tmp_path, gm)
        (stage / "git" / "my-comp" / "child").mkdir()
        store = tmp_path / "store"

        monkeypatch.setattr(sys, "argv", [
            "collect-submodules.py", str(stage), "--store", str(store)])
        monkeypatch.setenv("CASKET_SUBMODULE_STORE", str(store))
        monkeypatch.setenv("GITHUB_TOKEN", "fake")

        def fail_fetch(url, **kw):
            raise urllib.error.HTTPError("url", 404, "Not Found", {}, None)
        monkeypatch.setattr(cs, "fetch", fail_fetch)

        cs.main()
        unc = (stage / "meta" / "SUBMODULES-uncovered.txt").read_text()
        assert "child" in unc
