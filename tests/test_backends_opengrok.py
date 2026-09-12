"""Tests for mcp/backends.py — OpenGrok functions, search_text/search_symbol/
search_refs branches, _index_units OSError, submodule-aware resolve/list,
and coverage_report a-rpm/expected-products paths.

Network-free: all HTTP calls are mocked via unittest.mock.patch.
Run: pytest tests/test_backends_opengrok.py
"""
import json
import os
import sys
import urllib.error
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "mcp"))
import backends as be


def _setup_srv(tmp_path, monkeypatch):
    srv = tmp_path / "srv"
    srv.mkdir()
    monkeypatch.setattr(be, "SRV", str(srv))
    monkeypatch.setattr(be, "ROOT_PREFIX", str(srv) + "/sources-")
    return srv


# ================================================================ opengrok_up
class TestOpengrokUp:
    @patch("urllib.request.urlopen")
    def test_success(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp
        assert be.opengrok_up() is True

    @patch("urllib.request.urlopen")
    def test_http_error_still_up(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.HTTPError(
            "url", 401, "Unauthorized", {}, None)
        assert be.opengrok_up() is True

    @patch("urllib.request.urlopen")
    def test_connection_error_down(self, mock_urlopen):
        mock_urlopen.side_effect = ConnectionError("refused")
        assert be.opengrok_up() is False

    @patch("urllib.request.urlopen")
    def test_timeout_error_down(self, mock_urlopen):
        mock_urlopen.side_effect = TimeoutError("timed out")
        assert be.opengrok_up() is False


# ============================================================ opengrok_projects
class TestOpengrokProjects:
    @patch("urllib.request.urlopen")
    def test_list_of_strings(self, mock_urlopen):
        body = json.dumps(["proj-a", "proj-b"]).encode()
        mock_resp = MagicMock()
        mock_resp.__enter__ = MagicMock(return_value=MagicMock(
            read=MagicMock(return_value=body)))
        mock_resp.__exit__ = MagicMock(return_value=False)
        ctx = MagicMock()
        ctx.read.return_value = body
        mock_resp.__enter__.return_value = ctx

        import io
        mock_urlopen.return_value = MagicMock(
            __enter__=MagicMock(return_value=io.BytesIO(body)),
            __exit__=MagicMock(return_value=False))
        result = be.opengrok_projects()
        assert result == ["proj-a", "proj-b"]

    @patch("urllib.request.urlopen")
    def test_list_of_objects(self, mock_urlopen):
        import io
        body = json.dumps([{"name": "proj-x"}, {"name": "proj-y"}]).encode()
        mock_urlopen.return_value = MagicMock(
            __enter__=MagicMock(return_value=io.BytesIO(body)),
            __exit__=MagicMock(return_value=False))
        result = be.opengrok_projects()
        assert result == ["proj-x", "proj-y"]

    @patch("urllib.request.urlopen")
    def test_exception_returns_empty(self, mock_urlopen):
        mock_urlopen.side_effect = ConnectionError("down")
        assert be.opengrok_projects() == []

    @patch("urllib.request.urlopen")
    def test_empty_names_filtered(self, mock_urlopen):
        import io
        body = json.dumps(["", "proj-a", {"name": ""}, {"name": "proj-b"}]).encode()
        mock_urlopen.return_value = MagicMock(
            __enter__=MagicMock(return_value=io.BytesIO(body)),
            __exit__=MagicMock(return_value=False))
        result = be.opengrok_projects()
        assert result == ["proj-a", "proj-b"]


# ============================================================ opengrok_search
class TestOpengrokSearch:
    @patch("urllib.request.urlopen")
    def test_full_mode(self, mock_urlopen, tmp_path, monkeypatch):
        monkeypatch.setattr(be, "OPENGROK_SRC", str(tmp_path))
        real_file = tmp_path / "proj" / "src" / "main.go"
        real_file.parent.mkdir(parents=True)
        real_file.write_text("package main")

        import io
        body = json.dumps({
            "results": {
                "/proj/src/main.go": [
                    {"lineNumber": 1, "line": "<b>package</b> main"}
                ]
            }
        }).encode()
        mock_urlopen.return_value = MagicMock(
            __enter__=MagicMock(return_value=io.BytesIO(body)),
            __exit__=MagicMock(return_value=False))
        result = be.opengrok_search("full", "package main")
        assert result["backend"] == "opengrok"
        assert result["count"] == 1
        assert result["results"][0]["text"] == "package main"
        assert result["results"][0]["line"] == 1

    @patch("urllib.request.urlopen")
    def test_def_mode(self, mock_urlopen, tmp_path, monkeypatch):
        monkeypatch.setattr(be, "OPENGROK_SRC", str(tmp_path))
        import io
        body = json.dumps({"results": {}}).encode()
        mock_urlopen.return_value = MagicMock(
            __enter__=MagicMock(return_value=io.BytesIO(body)),
            __exit__=MagicMock(return_value=False))
        result = be.opengrok_search("def", "myFunc")
        assert result["backend"] == "opengrok"
        assert result["mode"] == "def"
        assert result["count"] == 0

    def test_bad_mode(self):
        result = be.opengrok_search("invalid", "query")
        assert "error" in result

    @patch("urllib.request.urlopen")
    def test_exception_returns_error(self, mock_urlopen):
        mock_urlopen.side_effect = ConnectionError("down")
        result = be.opengrok_search("full", "query")
        assert "error" in result
        assert result["backend"] == "opengrok"

    @patch("urllib.request.urlopen")
    def test_max_results_cap(self, mock_urlopen, tmp_path, monkeypatch):
        monkeypatch.setattr(be, "OPENGROK_SRC", str(tmp_path))
        f = tmp_path / "x.go"
        f.write_text("x")
        import io
        hits = [{"lineNumber": i, "line": f"line {i}"} for i in range(10)]
        body = json.dumps({"results": {"/x.go": hits}}).encode()
        mock_urlopen.return_value = MagicMock(
            __enter__=MagicMock(return_value=io.BytesIO(body)),
            __exit__=MagicMock(return_value=False))
        result = be.opengrok_search("full", "line", max_results=3)
        assert result["count"] == 3

    @patch("urllib.request.urlopen")
    def test_with_projects_param(self, mock_urlopen, tmp_path, monkeypatch):
        monkeypatch.setattr(be, "OPENGROK_SRC", str(tmp_path))
        import io
        body = json.dumps({"results": {}}).encode()
        mock_urlopen.return_value = MagicMock(
            __enter__=MagicMock(return_value=io.BytesIO(body)),
            __exit__=MagicMock(return_value=False))
        result = be.opengrok_search("symbol", "foo", projects="proj-a")
        assert result["backend"] == "opengrok"
        call_url = mock_urlopen.call_args[0][0]
        assert "projects=proj-a" in call_url

    @patch("urllib.request.urlopen")
    def test_line_number_variants(self, mock_urlopen, tmp_path, monkeypatch):
        monkeypatch.setattr(be, "OPENGROK_SRC", str(tmp_path))
        f = tmp_path / "a.go"
        f.write_text("x")
        import io
        body = json.dumps({
            "results": {
                "/a.go": [
                    {"line_number": 42, "line": "hit"},
                    {"line": "no-line-num"},
                ]
            }
        }).encode()
        mock_urlopen.return_value = MagicMock(
            __enter__=MagicMock(return_value=io.BytesIO(body)),
            __exit__=MagicMock(return_value=False))
        result = be.opengrok_search("full", "hit")
        assert result["results"][0]["line"] == 42
        assert result["results"][1]["line"] == 0


# ============================================================ search_text
class TestSearchTextOpengrok:
    def test_path_uses_ripgrep(self, tmp_path, monkeypatch):
        srv = _setup_srv(tmp_path, monkeypatch)
        mount = srv / "sources-ocp4.20.22"
        mount.mkdir()
        f = mount / "test.txt"
        f.write_text("hello world\n")
        monkeypatch.setattr(be, "opengrok_up", lambda: False)
        monkeypatch.setattr(be, "_rg", lambda args, timeout=30: [
            {"path": str(f), "line": 1, "text": "hello world"}])
        result = be.search_text("hello", path=str(f.parent))
        assert result["backend"] == "ripgrep"

    def test_no_path_opengrok_up_success(self, monkeypatch):
        monkeypatch.setattr(be, "opengrok_up", lambda: True)
        monkeypatch.setattr(be, "opengrok_search", lambda mode, q, proj=None, mx=50: {
            "backend": "opengrok", "count": 1, "results": [{"path": "/x", "line": 1}]
        })
        result = be.search_text("query", version="4.20")
        assert result["backend"] == "opengrok"

    def test_no_path_opengrok_up_error_explicit_engine(self, monkeypatch):
        monkeypatch.setattr(be, "opengrok_up", lambda: True)
        monkeypatch.setattr(be, "opengrok_search", lambda mode, q, proj=None, mx=50: {
            "error": "bad query", "backend": "opengrok"
        })
        result = be.search_text("query", engine="opengrok")
        assert "error" in result
        assert result["backend"] == "opengrok"

    def test_no_path_opengrok_down_returns_slow_note(self, monkeypatch):
        monkeypatch.setattr(be, "opengrok_up", lambda: False)
        result = be.search_text("query", version="4.20")
        assert result["backend"] == "none"
        assert "error" in result

    def test_no_path_opengrok_up_error_auto_falls_to_none(self, monkeypatch):
        monkeypatch.setattr(be, "opengrok_up", lambda: True)
        monkeypatch.setattr(be, "opengrok_search", lambda mode, q, proj=None, mx=50: {
            "error": "internal", "backend": "opengrok"
        })
        result = be.search_text("query", engine="auto")
        assert result["backend"] == "none"


# ============================================================ search_symbol
class TestSearchSymbolOpengrok:
    def test_opengrok_up_success(self, monkeypatch):
        monkeypatch.setattr(be, "opengrok_up", lambda: True)
        monkeypatch.setattr(be, "opengrok_search", lambda mode, name, proj=None: {
            "backend": "opengrok", "mode": "def", "count": 1, "results": []
        })
        result = be.search_symbol("MyFunc")
        assert result["backend"] == "opengrok"

    def test_opengrok_up_error_with_path_fallback(self, tmp_path, monkeypatch):
        srv = _setup_srv(tmp_path, monkeypatch)
        mount = srv / "sources-ocp4.20.22"
        mount.mkdir()
        f = mount / "main.go"
        f.write_text("func MyFunc() {}\n")
        monkeypatch.setattr(be, "opengrok_up", lambda: True)
        monkeypatch.setattr(be, "opengrok_search", lambda mode, name, proj=None: {
            "error": "internal"
        })
        monkeypatch.setattr(be, "_rg", lambda args, timeout=30: [
            {"path": str(f), "line": 1, "text": "func MyFunc() {}"}])
        result = be.search_symbol("MyFunc", path=str(mount))
        assert "ripgrep" in result["backend"]

    def test_opengrok_down_with_path_fallback(self, tmp_path, monkeypatch):
        srv = _setup_srv(tmp_path, monkeypatch)
        mount = srv / "sources-ocp4.20.22"
        mount.mkdir()
        f = mount / "main.go"
        f.write_text("type MyType struct {}\n")
        monkeypatch.setattr(be, "opengrok_up", lambda: False)
        monkeypatch.setattr(be, "_rg", lambda args, timeout=30: [
            {"path": str(f), "line": 1, "text": "type MyType struct {}"}])
        result = be.search_symbol("MyType", path=str(mount))
        assert "ripgrep" in result["backend"]

    def test_opengrok_down_no_path_error(self, monkeypatch):
        monkeypatch.setattr(be, "opengrok_up", lambda: False)
        result = be.search_symbol("MyFunc")
        assert result["backend"] == "none"
        assert "error" in result


# ============================================================ search_refs
class TestSearchRefs:
    def test_opengrok_up(self, monkeypatch):
        monkeypatch.setattr(be, "opengrok_up", lambda: True)
        monkeypatch.setattr(be, "opengrok_search", lambda mode, name, proj=None: {
            "backend": "opengrok", "mode": "symbol", "count": 5, "results": []
        })
        result = be.search_refs("MyFunc")
        assert result["backend"] == "opengrok"
        assert result["mode"] == "symbol"

    def test_opengrok_down(self, monkeypatch):
        monkeypatch.setattr(be, "opengrok_up", lambda: False)
        result = be.search_refs("MyFunc")
        assert "error" in result
        assert result["count"] == 0


# ============================================================ _index_units
class TestIndexUnitsOSError:
    def test_oserror_path(self, tmp_path, monkeypatch):
        srv = _setup_srv(tmp_path, monkeypatch)
        mount_path = srv / "sources-ocp4.20.22"
        mount_path.mkdir()
        mount = be.Mount(
            path=str(mount_path), name="sources-ocp4.20.22",
            suffix="ocp4.20.22", phase="a", version="4.20.22")
        # chmod 0o000 would not deny root, and CI runs as root -- raise from
        # the call itself so the guard is exercised for any user.
        def deny(p):
            raise PermissionError("EACCES")

        monkeypatch.setattr(be.os, "listdir", deny)
        assert list(be._index_units(mount)) == []


# ============================================== submodule-aware resolve/list
def _setup_submodule_mount(tmp_path, monkeypatch):
    """Set up a mount with SUBMODULES.tsv for testing submodule paths."""
    srv = _setup_srv(tmp_path, monkeypatch)
    mount = srv / "sources-ocp4.20.22"
    git = mount / "git"
    git.mkdir(parents=True)
    idx = "dir\trepo\tref\tversion\tcomponents\n"
    idx += "my-release\thttps://github.com/org/my-release\tabc123\t4.20\tmy-release\n"
    (git / "INDEX.tsv").write_text(idx)
    byrepo = mount / "by-repo"
    byrepo.mkdir()
    os.symlink(str(git / "my-release"), str(byrepo / "my-release"))
    bycomp = mount / "by-component"
    bycomp.mkdir()
    os.symlink(str(git / "my-release"), str(bycomp / "my-release"))
    tree = git / "my-release"
    tree.mkdir(exist_ok=True)
    sub = tree / "vendor" / "sub-lib"
    sub.mkdir(parents=True)
    (sub / "main.go").write_text("package main")
    meta = mount / "meta"
    meta.mkdir()
    (meta / "SUBMODULES.tsv").write_text(
        "component\tpath\trepo\tref\texact\tstatus\n"
        "my-release\tvendor/sub-lib\torg/sub-lib\tdef456\t1\tfilled\n"
    )
    return srv


class TestResolveRepoSubmodule:
    def test_submodule_match(self, tmp_path, monkeypatch):
        _setup_submodule_mount(tmp_path, monkeypatch)
        hits = be.resolve_repo("sub-lib", "4.20")
        sub_hits = [h for h in hits if h.get("submodule_of")]
        assert len(sub_hits) >= 1
        assert sub_hits[0]["repo_alias"] == "org/sub-lib"
        assert sub_hits[0]["ref"] == "def456"

    def test_parent_repo_also_found(self, tmp_path, monkeypatch):
        _setup_submodule_mount(tmp_path, monkeypatch)
        hits = be.resolve_repo("my-release", "4.20")
        parent_hits = [h for h in hits if not h.get("submodule_of")]
        assert len(parent_hits) >= 1


class TestResolveComponentSubmodule:
    def test_submodule_match(self, tmp_path, monkeypatch):
        _setup_submodule_mount(tmp_path, monkeypatch)
        hits = be.resolve_component("sub-lib", "4.20")
        sub_hits = [h for h in hits if h.get("submodule_of")]
        assert len(sub_hits) >= 1
        assert sub_hits[0]["repo"] == "https://github.com/org/sub-lib"
        assert sub_hits[0]["ref"] == "def456"


class TestListComponentsSubmodule:
    def test_submodule_rows_included(self, tmp_path, monkeypatch):
        _setup_submodule_mount(tmp_path, monkeypatch)
        result = be.list_components("4.20")
        sub_rows = [r for r in result if r.get("submodule_of")]
        assert len(sub_rows) >= 1
        assert sub_rows[0]["dir"] == "my-release/vendor/sub-lib"
        assert sub_rows[0]["repo"] == "https://github.com/org/sub-lib"


# ============================================= _submodule_rows short-line skip
class TestSubmoduleRowsShortLine:
    def test_short_lines_skipped(self, tmp_path):
        meta = tmp_path / "meta"
        meta.mkdir()
        (meta / "SUBMODULES.tsv").write_text(
            "component\tpath\trepo\tref\texact\tstatus\n"
            "short\n"
            "also\tshort\n"
        )
        result = be._submodule_rows(str(tmp_path))
        assert result == []


# ============================= coverage_report a-rpm + expected products paths
class TestCoverageReportARpm:
    def test_a_rpm_srpms_found(self, tmp_path, monkeypatch):
        srv = _setup_srv(tmp_path, monkeypatch)
        pv_dir = srv / "sources-ocp-srpms" / "by-ocp" / "4.20.22"
        pv_dir.mkdir(parents=True)
        for i in range(3):
            (pv_dir / f"pkg-{i}.src.rpm").write_text("")
        monkeypatch.setattr(be, "CASKET_WORK", str(tmp_path / "nonexistent"))

        result = be.coverage_report("4.20")
        assert result["phases"]["a-rpm"]["srpms"] == 3
        assert result["phases"]["a-rpm"]["ocp_version"] == "4.20.22"

    def test_expected_products_config(self, tmp_path, monkeypatch):
        srv = _setup_srv(tmp_path, monkeypatch)
        mount = srv / "sources-layered-ocp4.20"
        for prod in ("cnv",):
            git = mount / prod / "git"
            git.mkdir(parents=True)
            (git / "INDEX.tsv").write_text(
                "dir\trepo\tref\tversion\tcomponents\n"
                "comp\thttps://github.com/x/y\tabc\t4.20\tx\n"
            )
        work = tmp_path / "work"
        cfg_dir = work / "config"
        cfg_dir.mkdir(parents=True)
        (cfg_dir / "phase-b-operand-products.tsv").write_text(
            "# comment line\n"
            "idx\tcnv\n"
            "idx\tacs\n"
        )
        monkeypatch.setattr(be, "CASKET_WORK", str(work))
        result = be.coverage_report("4.20")
        assert "cnv" not in result["missing_b_operand_products"]
        assert "acs" in result["missing_b_operand_products"]


# ============================================ _rg_search_text no-mounts branch
class TestRgSearchTextNoMounts:
    def test_version_no_match(self, tmp_path, monkeypatch):
        _setup_srv(tmp_path, monkeypatch)
        result = be._rg_search_text("query", "99.99", None, 100, True)
        assert result["count"] == 0
        assert result["note"] == "no matching mounts"

    def test_no_version_no_path_uses_all_mounts(self, tmp_path, monkeypatch):
        srv = _setup_srv(tmp_path, monkeypatch)
        mount = srv / "sources-ocp4.20.22"
        mount.mkdir()
        (mount / "test.txt").write_text("hello world\n")
        monkeypatch.setattr(be, "_rg", lambda args, timeout=30: [
            {"path": str(mount / "test.txt"), "line": 1, "text": "hello world"}])
        result = be._rg_search_text("hello", "", None, 100, True)
        assert result["backend"] == "ripgrep"


# ============================================== _go_mod_matches OSError branch
class TestGoModMatchesOSError:
    def test_oserror_returns_error_entry(self):
        result = be._go_mod_matches("/nonexistent/go.mod", "foo")
        assert len(result) == 1
        assert "error" in result[0]


# =============================================== permalink not under any mount
class TestPermalinkNoMount:
    def test_path_not_under_mount(self, tmp_path, monkeypatch):
        srv = _setup_srv(tmp_path, monkeypatch)
        f = srv / "sources-ocp4.20.22" / "test.txt"
        f.parent.mkdir(parents=True)
        f.write_text("content")
        result = be.permalink(str(f))
        assert "error" in result


# ========================================= opengrok_search non-int line number
class TestOpengrokSearchLineEdge:
    @patch("urllib.request.urlopen")
    def test_non_int_line_number(self, mock_urlopen, tmp_path, monkeypatch):
        monkeypatch.setattr(be, "OPENGROK_SRC", str(tmp_path))
        f = tmp_path / "z.go"
        f.write_text("x")
        import io
        body = json.dumps({
            "results": {
                "/z.go": [{"lineNumber": "not-a-number", "line": "hit"}]
            }
        }).encode()
        mock_urlopen.return_value = MagicMock(
            __enter__=MagicMock(return_value=io.BytesIO(body)),
            __exit__=MagicMock(return_value=False))
        result = be.opengrok_search("full", "hit")
        assert result["results"][0]["line"] == 0


# ===================================== list_components OSError on INDEX.tsv
class TestListComponentsOSError:
    def test_index_oserror(self, tmp_path, monkeypatch):
        srv = _setup_srv(tmp_path, monkeypatch)
        mount = srv / "sources-ocp4.20.22"
        git = mount / "git"
        git.mkdir(parents=True)
        idx = git / "INDEX.tsv"
        idx.write_text("dir\trepo\tref\tversion\tcomponents\n")

        real_open = open

        def deny(path, *a, **kw):
            if str(path).endswith("INDEX.tsv"):
                raise PermissionError("EACCES")
            return real_open(path, *a, **kw)

        monkeypatch.setattr("builtins.open", deny)
        result = be.list_components("4.20")
        assert [r for r in result if not r.get("submodule_of")] == []
