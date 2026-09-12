"""Tests for scripts/check-upstream-links.py — pure logic functions and
mocked network calls.

Network-free: all HTTP calls are monkeypatched.
Run: pytest tests/test_check_upstream_links.py
"""
import importlib.util
import io
import json
import os
import pathlib
import types
import urllib.error
import urllib.request

import pytest

_SCRIPT = str(pathlib.Path(__file__).resolve().parent.parent / "scripts" / "check-upstream-links.py")
_spec = importlib.util.spec_from_file_location("check_upstream_links", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


# ------------------------------------------------------------------ candidates
class TestCandidates:
    def test_head_kind(self):
        assert _mod.candidates("abc", "1.0.0", "head") == ["HEAD"]

    def test_pin_with_ref_and_version(self):
        result = _mod.candidates("abc123", "1.0.0", "pin")
        assert result == ["abc123", "v1.0.0", "1.0.0"]

    def test_tag_no_ref(self):
        result = _mod.candidates("", "1.0.0", "tag")
        assert result == ["v1.0.0", "1.0.0"]

    def test_dedup_v_prefix(self):
        result = _mod.candidates("v1.0.0", "1.0.0", "pin")
        assert result == ["v1.0.0", "1.0.0"]

    def test_dedup_ref_equals_version(self):
        result = _mod.candidates("1.0.0", "1.0.0", "pin")
        assert result == ["1.0.0", "v1.0.0"]

    def test_no_ref_no_version(self):
        assert _mod.candidates("", "", "pin") == []

    def test_ref_only(self):
        result = _mod.candidates("abc", "", "pin")
        assert result == ["abc"]

    def test_version_only_pin(self):
        result = _mod.candidates("", "2.3.4", "pin")
        assert result == ["v2.3.4", "2.3.4"]


# ------------------------------------------------------------------ row_key
class TestRowKey:
    def test_with_ref(self):
        row = ("https://github.com/org/repo", "abc123", "1.0.0", "pin", "2024")
        assert _mod.row_key(row) == "https://github.com/org/repo\tabc123"

    def test_no_ref_with_version(self):
        row = ("https://github.com/org/repo", "", "1.0.0", "tag", "2024")
        assert _mod.row_key(row) == "https://github.com/org/repo\t1.0.0"

    def test_no_ref_no_version(self):
        row = ("https://github.com/org/repo", "", "", "head", "2024")
        assert _mod.row_key(row) == "https://github.com/org/repo\thead"


# ------------------------------------------------------------------ http_status
class TestHttpStatus:
    def test_success(self, monkeypatch):
        resp = types.SimpleNamespace(status=200)
        resp.__enter__ = lambda s: s
        resp.__exit__ = lambda s, *a: None
        monkeypatch.setattr(urllib.request, "urlopen",
                            lambda req, timeout=20: resp)
        assert _mod.http_status("https://example.com") == 200

    def test_http_error(self, monkeypatch):
        def raise_404(req, timeout=20):
            raise urllib.error.HTTPError("url", 404, "Not Found", {}, None)
        monkeypatch.setattr(urllib.request, "urlopen", raise_404)
        assert _mod.http_status("https://example.com") == 404

    def test_connection_error(self, monkeypatch):
        def raise_err(req, timeout=20):
            raise ConnectionError("refused")
        monkeypatch.setattr(urllib.request, "urlopen", raise_err)
        assert _mod.http_status("https://example.com") == 0

    def test_token_header(self, monkeypatch):
        captured = {}
        resp = types.SimpleNamespace(status=200)
        resp.__enter__ = lambda s: s
        resp.__exit__ = lambda s, *a: None
        def capture(req, timeout=20):
            captured["auth"] = req.get_header("Authorization")
            return resp
        monkeypatch.setattr(urllib.request, "urlopen", capture)
        _mod.http_status("https://api.github.com/repos/x/y", token="tok123")
        assert captured["auth"] == "Bearer tok123"

    def test_no_token_for_non_github(self, monkeypatch):
        captured = {}
        resp = types.SimpleNamespace(status=200)
        resp.__enter__ = lambda s: s
        resp.__exit__ = lambda s, *a: None
        def capture(req, timeout=20):
            captured["auth"] = req.get_header("Authorization")
            return resp
        monkeypatch.setattr(urllib.request, "urlopen", capture)
        _mod.http_status("https://example.com/foo", token="tok123")
        assert captured["auth"] is None


# ------------------------------------------------------------------ check_repo
class TestCheckRepo:
    def _mock_urlopen(self, monkeypatch, full_name, status=200):
        body = json.dumps({"full_name": full_name}).encode()
        import io as _io
        real_resp = types.SimpleNamespace()
        real_resp.__enter__ = lambda s: _io.BytesIO(body)
        real_resp.__exit__ = lambda s, *a: None
        monkeypatch.setattr(urllib.request, "urlopen",
                            lambda req, timeout=20: real_resp)

    def test_ok(self, monkeypatch):
        self._mock_urlopen(monkeypatch, "org/repo")
        state, detail = _mod.check_repo("https://github.com/org/repo", None)
        assert state == "ok"

    def test_moved(self, monkeypatch):
        self._mock_urlopen(monkeypatch, "new-org/repo")
        state, detail = _mod.check_repo("https://github.com/org/repo", None)
        assert state == "moved"
        assert detail == "new-org/repo"

    def test_gone_404(self, monkeypatch):
        def raise_404(req, timeout=20):
            raise urllib.error.HTTPError("url", 404, "Not Found", {}, None)
        monkeypatch.setattr(urllib.request, "urlopen", raise_404)
        state, detail = _mod.check_repo("https://github.com/org/repo", None)
        assert state == "gone"
        assert "404" in detail

    def test_gone_451(self, monkeypatch):
        def raise_451(req, timeout=20):
            raise urllib.error.HTTPError("url", 451, "Unavailable", {}, None)
        monkeypatch.setattr(urllib.request, "urlopen", raise_451)
        state, detail = _mod.check_repo("https://github.com/org/repo", None)
        assert state == "gone"

    def test_error_500(self, monkeypatch):
        def raise_500(req, timeout=20):
            raise urllib.error.HTTPError("url", 500, "ISE", {}, None)
        monkeypatch.setattr(urllib.request, "urlopen", raise_500)
        state, detail = _mod.check_repo("https://github.com/org/repo", None)
        assert state == "error"
        assert "500" in detail

    def test_connection_error(self, monkeypatch):
        def raise_err(req, timeout=20):
            raise ConnectionError("timeout")
        monkeypatch.setattr(urllib.request, "urlopen", raise_err)
        state, detail = _mod.check_repo("https://github.com/org/repo", None)
        assert state == "error"


# ------------------------------------------------------------------ check_row
class TestCheckRow:
    def test_ok_first_candidate(self, monkeypatch):
        monkeypatch.setattr(_mod, "http_status", lambda url, method="GET", **kw: 200)
        row = ("https://github.com/org/repo", "abc", "1.0", "pin", "2024")
        state, detail = _mod.check_row(row)
        assert state == "ok"

    def test_bad_all_fail(self, monkeypatch):
        monkeypatch.setattr(_mod, "http_status", lambda url, method="GET", **kw: 404)
        row = ("https://github.com/org/repo", "abc", "1.0", "pin", "2024")
        state, detail = _mod.check_row(row)
        assert state == "bad"
        assert "HTTP 404" in detail

    def test_ok_second_candidate(self, monkeypatch):
        calls = []
        def fake_status(url, method="GET", **kw):
            calls.append(url)
            return 200 if "v1.0" in url else 404
        monkeypatch.setattr(_mod, "http_status", fake_status)
        row = ("https://github.com/org/repo", "abc", "1.0", "pin", "2024")
        state, detail = _mod.check_row(row)
        assert state == "ok"

    def test_head_kind(self, monkeypatch):
        monkeypatch.setattr(_mod, "http_status", lambda url, method="GET", **kw: 200)
        row = ("https://github.com/org/repo", "", "", "head", "2024")
        state, detail = _mod.check_row(row)
        assert state == "ok"
        assert detail == "HEAD"


# ------------------------------------------------------------------ main
class TestMain:
    def test_no_manifest(self, tmp_path, monkeypatch):
        monkeypatch.setattr(_mod, "MANIFEST", str(tmp_path / "nonexistent.tsv"))
        monkeypatch.setattr(_mod, "sys", types.SimpleNamespace(
            argv=["prog"], exit=lambda c: None))
        # main() reads sys.argv via argparse, so patch it
        import sys
        monkeypatch.setattr(sys, "argv", ["prog"])
        monkeypatch.setattr(_mod, "MANIFEST", str(tmp_path / "nonexistent.tsv"))
        result = _mod.main()
        assert result == 2

    def test_with_manifest(self, tmp_path, monkeypatch):
        import sys
        monkeypatch.setattr(sys, "argv", ["prog"])

        manifest = tmp_path / "upstream-sources.tsv"
        manifest.write_text(
            "# header\n"
            "https://github.com/org/repo\tabc\t1.0.0\tpin\t2024-01-01\n"
        )
        baseline = tmp_path / "baseline.txt"
        monkeypatch.setattr(_mod, "MANIFEST", str(manifest))
        monkeypatch.setattr(_mod, "BASELINE", str(baseline))
        monkeypatch.setenv("GITHUB_TOKEN", "")

        monkeypatch.setattr(_mod, "check_repo", lambda r, t: ("ok", ""))
        monkeypatch.setattr(_mod, "check_row", lambda r: ("ok", "abc"))

        result = _mod.main()
        assert result == 0

    def test_new_rot_returns_1(self, tmp_path, monkeypatch):
        import sys
        monkeypatch.setattr(sys, "argv", ["prog"])

        manifest = tmp_path / "upstream-sources.tsv"
        manifest.write_text(
            "https://github.com/org/repo\tabc\t1.0.0\tpin\t2024-01-01\n"
        )
        monkeypatch.setattr(_mod, "MANIFEST", str(manifest))
        monkeypatch.setattr(_mod, "BASELINE", str(tmp_path / "no-baseline.txt"))
        monkeypatch.setenv("GITHUB_TOKEN", "")

        monkeypatch.setattr(_mod, "check_repo", lambda r, t: ("ok", ""))
        monkeypatch.setattr(_mod, "check_row", lambda r: ("bad", "abc:HTTP 404"))

        result = _mod.main()
        assert result == 1

    def test_baseline_suppresses_known_rot(self, tmp_path, monkeypatch):
        import sys
        monkeypatch.setattr(sys, "argv", ["prog"])

        manifest = tmp_path / "upstream-sources.tsv"
        manifest.write_text(
            "https://github.com/org/repo\tabc\t1.0.0\tpin\t2024-01-01\n"
        )
        baseline = tmp_path / "baseline.txt"
        baseline.write_text("https://github.com/org/repo\tabc\n")

        monkeypatch.setattr(_mod, "MANIFEST", str(manifest))
        monkeypatch.setattr(_mod, "BASELINE", str(baseline))
        monkeypatch.setenv("GITHUB_TOKEN", "")

        monkeypatch.setattr(_mod, "check_repo", lambda r, t: ("ok", ""))
        monkeypatch.setattr(_mod, "check_row", lambda r: ("bad", "abc:HTTP 404"))

        result = _mod.main()
        assert result == 0

    def test_gone_repo_in_new_rot(self, tmp_path, monkeypatch):
        import sys
        monkeypatch.setattr(sys, "argv", ["prog"])

        manifest = tmp_path / "upstream-sources.tsv"
        manifest.write_text(
            "https://github.com/org/gone-repo\tabc\t1.0.0\tpin\t2024-01-01\n"
        )
        monkeypatch.setattr(_mod, "MANIFEST", str(manifest))
        monkeypatch.setattr(_mod, "BASELINE", str(tmp_path / "no-baseline.txt"))
        monkeypatch.setenv("GITHUB_TOKEN", "")

        monkeypatch.setattr(_mod, "check_repo", lambda r, t: ("gone", "HTTP 404"))
        monkeypatch.setattr(_mod, "check_row", lambda r: ("bad", "abc:HTTP 404"))

        result = _mod.main()
        assert result == 1

    def test_write_baseline(self, tmp_path, monkeypatch):
        import sys
        monkeypatch.setattr(sys, "argv", ["prog", "--write-baseline"])

        manifest = tmp_path / "upstream-sources.tsv"
        manifest.write_text(
            "https://github.com/org/repo\tabc\t1.0.0\tpin\t2024-01-01\n"
        )
        baseline = tmp_path / "baseline.txt"
        monkeypatch.setattr(_mod, "MANIFEST", str(manifest))
        monkeypatch.setattr(_mod, "BASELINE", str(baseline))
        monkeypatch.setenv("GITHUB_TOKEN", "")

        monkeypatch.setattr(_mod, "check_repo", lambda r, t: ("ok", ""))
        monkeypatch.setattr(_mod, "check_row", lambda r: ("bad", "detail"))

        result = _mod.main()
        assert result == 0
        assert baseline.exists()
        content = baseline.read_text()
        assert "org/repo" in content

    def test_limit_flag(self, tmp_path, monkeypatch):
        import sys
        monkeypatch.setattr(sys, "argv", ["prog", "--limit", "1"])

        manifest = tmp_path / "upstream-sources.tsv"
        manifest.write_text(
            "https://github.com/org/repo1\ta\t1.0\tpin\t2024\n"
            "https://github.com/org/repo2\tb\t2.0\tpin\t2024\n"
        )
        monkeypatch.setattr(_mod, "MANIFEST", str(manifest))
        monkeypatch.setattr(_mod, "BASELINE", str(tmp_path / "no-baseline.txt"))
        monkeypatch.setenv("GITHUB_TOKEN", "")

        checked_repos = []
        def fake_check_repo(r, t):
            checked_repos.append(r)
            return ("ok", "")
        monkeypatch.setattr(_mod, "check_repo", fake_check_repo)
        monkeypatch.setattr(_mod, "check_row", lambda r: ("ok", "a"))

        _mod.main()
        assert len(checked_repos) == 1
