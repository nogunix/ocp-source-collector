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


# --------------------------------------------------------------- parent_refs
class TestParentRefsDegradation:
    def test_unreadable_manifest_falls_back_to_branch_heads(self, tmp_path,
                                                            capsys):
        """A corrupt MANIFEST.json must not abort — every row just goes APPROX."""
        stage = tmp_path / "stage"
        (stage / "meta").mkdir(parents=True)
        (stage / "meta" / "MANIFEST.json").write_text("{ not json")

        assert cs.parent_refs(str(stage)) == {}
        err = capsys.readouterr().err
        assert "cannot read" in err
        assert "APPROX" in err


# ------------------------------------------------------ link_tree fallback
class TestLinkTreeFallback:
    def test_copies_when_hardlink_refused(self, tmp_path, monkeypatch):
        """Store on a different filesystem than the stage: EXDEV -> copy2."""
        src = tmp_path / "src"
        (src / "nested").mkdir(parents=True)
        (src / "nested" / "a.txt").write_text("payload")
        dst = tmp_path / "dst"

        def no_link(*a, **kw):
            raise OSError(18, "Invalid cross-device link")

        monkeypatch.setattr(cs.os, "link", no_link)
        cs.link_tree(str(src), str(dst))
        assert (dst / "nested" / "a.txt").read_text() == "payload"


# ------------------------------------------------------------------ scan_one
class TestScanOneEdges:
    def test_unreadable_gitmodules_reported_as_bad(self, tmp_path, monkeypatch):
        tree = tmp_path / "tree"
        tree.mkdir()
        (tree / ".gitmodules").write_text('[submodule "x"]\n\tpath = x\n')

        real_open = open

        def fake_open(path, *a, **kw):
            if str(path).endswith(".gitmodules"):
                raise PermissionError("EACCES")
            return real_open(path, *a, **kw)

        monkeypatch.setattr("builtins.open", fake_open)
        todo, bad = cs.scan_one(str(tree), "comp", "", "", "", "", {})
        assert todo == []
        assert bad == [("comp", ".gitmodules", "-", "unreadable:PermissionError")]

    def test_empty_gitmodules_yields_nothing(self, tmp_path):
        tree = tmp_path / "tree"
        tree.mkdir()
        (tree / ".gitmodules").write_text("# only a comment\n")
        assert cs.scan_one(str(tree), "comp", "", "", "", "", {}) == ([], [])

    def test_gitlinks_cached_per_slug_ref(self, tmp_path, monkeypatch):
        """The trees API is rate-limited; one call per (slug, ref), reused."""
        tree = tmp_path / "tree"
        (tree / "child").mkdir(parents=True)
        (tree / ".gitmodules").write_text(
            '[submodule "child"]\n\tpath = child\n'
            '\turl = https://github.com/org/child.git\n')

        calls = []

        def fake_gitlinks(slug, ref, token):
            calls.append((slug, ref))
            return {"child": "c" * 40}

        monkeypatch.setattr(cs, "gitlinks", fake_gitlinks)
        seen = {}
        todo1, _ = cs.scan_one(str(tree), "comp", "", "org/parent", "abc", "t", seen)
        todo2, _ = cs.scan_one(str(tree), "comp", "", "org/parent", "abc", "t", seen)

        assert calls == [("org/parent", "abc")]      # second call served from seen
        assert todo1[0][2].ref == "c" * 40
        assert todo1[0][2].exact is True
        assert todo2[0][2].ref == "c" * 40

    def test_no_slug_skips_the_trees_api(self, tmp_path, monkeypatch):
        """Without a parent slug/ref there is nothing to ask the API about."""
        tree = tmp_path / "tree"
        (tree / "child").mkdir(parents=True)
        (tree / ".gitmodules").write_text(
            '[submodule "child"]\n\tpath = child\n'
            '\turl = https://github.com/org/child.git\n\tbranch = main\n')

        def boom(*a, **kw):
            raise AssertionError("gitlinks must not be called without slug/ref")

        monkeypatch.setattr(cs, "gitlinks", boom)
        todo, _bad = cs.scan_one(str(tree), "comp", "", "", "", "", {})
        assert todo[0][2].exact is False


# --------------------------------------------------------------- main() paths
class TestMainDegradation:
    def _stage(self, tmp_path, monkeypatch, gm, store_env=True):
        stage = tmp_path / "stage"
        git = stage / "git" / "my-comp"
        git.mkdir(parents=True)
        (stage / "meta").mkdir(parents=True)
        (git / ".gitmodules").write_text(gm)
        (git / "child").mkdir()
        store = tmp_path / "store"
        monkeypatch.setattr(sys, "argv", [
            "collect-submodules.py", str(stage), "--store", str(store)])
        if store_env:
            monkeypatch.setenv("CASKET_SUBMODULE_STORE", str(store))
        else:
            monkeypatch.delenv("CASKET_SUBMODULE_STORE", raising=False)
        monkeypatch.setenv("CASKET_SUBMODULE_CACHE", str(tmp_path / "cache"))
        monkeypatch.setenv("GITHUB_TOKEN", "fake")
        return stage, store

    _GM = ('[submodule "child"]\n\tpath = child\n'
           '\turl = https://github.com/org/child.git\n\tbranch = main\n')

    def test_warns_when_submodule_store_unset(self, tmp_path, monkeypatch,
                                              capsys):
        """Unset store lands inside the repo and kills the store->stage links."""
        stage = tmp_path / "stage"
        (stage / "git").mkdir(parents=True)
        monkeypatch.delenv("CASKET_SUBMODULE_STORE", raising=False)
        monkeypatch.setenv("CASKET_SUBMODULE_CACHE", str(tmp_path / "cache"))
        monkeypatch.setattr(sys, "argv", [
            "collect-submodules.py", str(stage), "--store", str(tmp_path / "store")])
        assert cs.main() == 0
        err = capsys.readouterr().err
        assert "CASKET_SUBMODULE_STORE unset" in err
        assert "SAME filesystem" in err

    def test_symlinked_component_dir_skipped(self, tmp_path, monkeypatch):
        """git/ dedup symlinks would otherwise be scanned (and filled) twice."""
        stage, _store = self._stage(tmp_path, monkeypatch, self._GM)
        os.symlink(stage / "git" / "my-comp", stage / "git" / "alias-comp")
        (stage / "git" / "plain-file").write_text("not a tree")

        monkeypatch.setattr(cs, "fetch", lambda url, **kw: _make_targz(
            {"main.go": "package main"}))
        assert cs.main() == 0

        comps = {l.split("\t")[0]
                 for l in (stage / "meta" / "SUBMODULES.tsv").read_text().splitlines()
                 if not l.startswith("#")}
        assert comps == {"my-comp"}

    def test_scan_failure_is_reported_not_fatal(self, tmp_path, monkeypatch,
                                                capsys):
        stage, _store = self._stage(tmp_path, monkeypatch, self._GM)

        def boom(*a, **kw):
            raise RuntimeError("scan exploded")

        monkeypatch.setattr(cs, "scan_one", boom)
        assert cs.main() == 0
        assert "my-comp/: scan failed" in capsys.readouterr().err
        unc = (stage / "meta" / "SUBMODULES-uncovered.txt").read_text()
        assert "scan-failed:RuntimeError: scan exploded" in unc

    def test_link_failure_is_recorded(self, tmp_path, monkeypatch):
        """An archive that acquires fine but cannot be linked into the stage."""
        stage, _store = self._stage(tmp_path, monkeypatch, self._GM)
        monkeypatch.setattr(cs, "fetch", lambda url, **kw: _make_targz(
            {"main.go": "package main"}))

        def boom(src, dst):
            raise OSError("ENOSPC")

        monkeypatch.setattr(cs, "link_tree", boom)
        assert cs.main() == 0
        unc = (stage / "meta" / "SUBMODULES-uncovered.txt").read_text()
        assert "link-failed:OSError" in unc

    def test_stale_uncovered_file_removed(self, tmp_path, monkeypatch):
        stage, _store = self._stage(tmp_path, monkeypatch, self._GM)
        unc = stage / "meta" / "SUBMODULES-uncovered.txt"
        unc.write_text("# stale content from an earlier run\n")
        monkeypatch.setattr(cs, "fetch", lambda url, **kw: _make_targz(
            {"main.go": "package main"}))
        assert cs.main() == 0
        assert not unc.exists()

    def test_progress_logged_every_50_archives(self, tmp_path, monkeypatch,
                                               capsys):
        stage = tmp_path / "stage"
        git = stage / "git" / "my-comp"
        git.mkdir(parents=True)
        (stage / "meta").mkdir(parents=True)
        gm = "".join(
            f'[submodule "c{i}"]\n\tpath = c{i}\n'
            f'\turl = https://github.com/org/c{i}.git\n\tbranch = main\n'
            for i in range(50))
        (git / ".gitmodules").write_text(gm)
        for i in range(50):
            (git / f"c{i}").mkdir()
        store = tmp_path / "store"
        monkeypatch.setattr(sys, "argv", [
            "collect-submodules.py", str(stage), "--store", str(store),
            "--max-depth", "1"])
        monkeypatch.setenv("CASKET_SUBMODULE_STORE", str(store))
        monkeypatch.setenv("CASKET_SUBMODULE_CACHE", str(tmp_path / "cache"))
        monkeypatch.setenv("GITHUB_TOKEN", "fake")
        monkeypatch.setattr(cs, "fetch", lambda url, **kw: _make_targz(
            {"main.go": "package main"}))

        assert cs.main() == 0
        assert "  50/50" in capsys.readouterr().err


class TestParentRefsRowFiltering:
    def test_rows_missing_repo_or_ref_are_dropped(self, tmp_path):
        """MANIFEST rows without both repo and commit cannot pin anything."""
        stage = tmp_path / "stage"
        (stage / "meta").mkdir(parents=True)
        (stage / "meta" / "MANIFEST.json").write_text(json.dumps({"images": [
            {"name": "no-repo", "source": {"tarball": "no-repo-1.tar.gz",
                                           "repo": "", "commit": "abc"}},
            {"name": "no-ref", "source": {"tarball": "no-ref-1.tar.gz",
                                          "repo": "https://github.com/o/r",
                                          "commit": ""}},
            {"name": "good", "source": {"tarball": "good-1.tar.gz",
                                        "repo": "https://github.com/o/g",
                                        "commit": "abc"}},
        ]}))
        assert cs.parent_refs(str(stage)) == {
            "good-1": ("https://github.com/o/g", "abc")}

    def test_first_row_for_a_dir_wins(self, tmp_path):
        stage = tmp_path / "stage"
        (stage / "meta").mkdir(parents=True)
        (stage / "meta" / "MANIFEST.json").write_text(json.dumps({"images": [
            {"name": "a", "source": {"tarball": "shared.tar.gz",
                                     "repo": "https://github.com/o/first",
                                     "commit": "111"}},
            {"name": "b", "source": {"tarball": "shared.tar.gz",
                                     "repo": "https://github.com/o/second",
                                     "commit": "222"}},
        ]}))
        assert cs.parent_refs(str(stage))["shared"] == (
            "https://github.com/o/first", "111")


class TestLinkTreeExistingFiles:
    def test_existing_destination_file_is_left_alone(self, tmp_path):
        """Re-running must not clobber what a previous round already linked."""
        src = tmp_path / "src"
        src.mkdir()
        (src / "a.txt").write_text("from store")
        dst = tmp_path / "dst"
        dst.mkdir()
        (dst / "a.txt").write_text("already here")

        cs.link_tree(str(src), str(dst))
        assert (dst / "a.txt").read_text() == "already here"


class TestMainRoundReuse:
    def test_second_round_reuses_an_already_acquired_archive(self, tmp_path,
                                                             monkeypatch):
        """A nested submodule pointing at a (slug, ref) the first round already
        fetched must not be re-acquired."""
        stage = tmp_path / "stage"
        git = stage / "git" / "my-comp"
        git.mkdir(parents=True)
        (stage / "meta").mkdir(parents=True)
        (git / ".gitmodules").write_text(
            '[submodule "child"]\n\tpath = child\n'
            '\turl = https://github.com/org/child.git\n\tbranch = main\n')
        (git / "child").mkdir()

        # the fetched archive itself declares the same submodule again
        nested = _make_targz({
            ".gitmodules": '[submodule "child"]\n\tpath = child\n'
                           '\turl = https://github.com/org/child.git\n'
                           '\tbranch = main\n'})
        fetches = []

        def fake_fetch(url, **kw):
            fetches.append(url)
            return nested

        store = tmp_path / "store"
        monkeypatch.setattr(sys, "argv", [
            "collect-submodules.py", str(stage), "--store", str(store),
            "--max-depth", "2"])
        monkeypatch.setenv("CASKET_SUBMODULE_STORE", str(store))
        monkeypatch.setenv("CASKET_SUBMODULE_CACHE", str(tmp_path / "cache"))
        monkeypatch.setenv("GITHUB_TOKEN", "fake")
        monkeypatch.setattr(cs, "fetch", fake_fetch)

        assert cs.main() == 0
        archives = [u for u in fetches if "/archive/" in u]
        assert len(archives) == 1         # round 2 served from `status`
        rows = [l for l in (stage / "meta" / "SUBMODULES.tsv").read_text()
                .splitlines() if not l.startswith("#")]
        assert len(rows) == 2             # both referencing paths recorded
