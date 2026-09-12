"""Tests for scripts/phase-a-rpm-image-inventory.py — api, find_image_id,
rpm_manifest, and main().

Network-free: all HTTP calls use unittest.mock.patch("urllib.request.urlopen").
Run: pytest tests/test_image_inventory_extra.py
"""
import importlib.util
import io
import json
import os
import pathlib
import sys
from unittest.mock import patch, MagicMock

import pytest

_SCRIPT = str(pathlib.Path(__file__).resolve().parent.parent
              / "scripts" / "phase-a-rpm-image-inventory.py")
_spec = importlib.util.spec_from_file_location("inventory_extra", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


# ------------------------------------------------------------------ api()
class TestApi:
    @patch("urllib.request.urlopen")
    def test_returns_json(self, mock_urlopen):
        body = json.dumps({"data": [{"_id": "abc"}]}).encode()
        resp = MagicMock()
        resp.__enter__ = MagicMock(return_value=io.BytesIO(body))
        resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = resp
        result = _mod.api("/images?page_size=1")
        assert result == {"data": [{"_id": "abc"}]}

    @patch("urllib.request.urlopen")
    def test_sets_accept_header(self, mock_urlopen):
        body = json.dumps({}).encode()
        resp = MagicMock()
        resp.__enter__ = MagicMock(return_value=io.BytesIO(body))
        resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = resp
        _mod.api("/images")
        req = mock_urlopen.call_args[0][0]
        assert req.get_header("Accept") == "application/json"

    @patch("urllib.request.urlopen")
    def test_uses_pyxis_base(self, mock_urlopen):
        body = json.dumps({}).encode()
        resp = MagicMock()
        resp.__enter__ = MagicMock(return_value=io.BytesIO(body))
        resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = resp
        _mod.api("/images?filter=x")
        req = mock_urlopen.call_args[0][0]
        assert req.full_url.startswith(_mod.PYXIS)

    @patch("urllib.request.urlopen")
    def test_propagates_exception(self, mock_urlopen):
        mock_urlopen.side_effect = ConnectionError("refused")
        with pytest.raises(ConnectionError):
            _mod.api("/images")


# ---------------------------------------------------------- find_image_id()
class TestFindImageId:
    def test_found_first_filter(self, monkeypatch):
        monkeypatch.setattr(_mod, "DELAY", 0)
        call_count = []

        def fake_api(path):
            call_count.append(path)
            return {"data": [{"_id": "img123"}]}

        monkeypatch.setattr(_mod, "api", fake_api)
        result = _mod.find_image_id("sha256:abc")
        assert result == "img123"
        assert len(call_count) == 1

    def test_found_second_filter(self, monkeypatch):
        monkeypatch.setattr(_mod, "DELAY", 0)
        calls = []

        def fake_api(path):
            calls.append(path)
            if len(calls) == 1:
                return {"data": []}
            return {"data": [{"_id": "img456"}]}

        monkeypatch.setattr(_mod, "api", fake_api)
        result = _mod.find_image_id("sha256:def")
        assert result == "img456"
        assert len(calls) == 2

    def test_not_found(self, monkeypatch):
        monkeypatch.setattr(_mod, "DELAY", 0)
        monkeypatch.setattr(_mod, "api", lambda path: {"data": []})
        result = _mod.find_image_id("sha256:nope")
        assert result is None

    def test_exception_continues(self, monkeypatch):
        monkeypatch.setattr(_mod, "DELAY", 0)
        calls = []

        def fake_api(path):
            calls.append(path)
            if len(calls) <= 4:
                raise ConnectionError("timeout")
            return {"data": [{"_id": "recovered"}]}

        monkeypatch.setattr(_mod, "api", fake_api)
        monkeypatch.setattr(_mod.time, "sleep", lambda s: None)
        result = _mod.find_image_id("sha256:flaky")
        assert result == "recovered"

    def test_all_exceptions_returns_none(self, monkeypatch):
        monkeypatch.setattr(_mod, "DELAY", 0)
        monkeypatch.setattr(_mod, "api", lambda path: (_ for _ in ()).throw(ConnectionError("down")))
        monkeypatch.setattr(_mod.time, "sleep", lambda s: None)
        result = _mod.find_image_id("sha256:down")
        assert result is None


# ---------------------------------------------------------- rpm_manifest()
class TestRpmManifest:
    def test_cache_hit(self, tmp_path, monkeypatch):
        monkeypatch.setattr(_mod, "CACHE", str(tmp_path))
        cached = {"rpms": [{"srpm_name": "foo-1.0.src.rpm"}]}
        cache_file = tmp_path / "sha256_abc.json"
        cache_file.write_text(json.dumps(cached))
        result = _mod.rpm_manifest("sha256:abc")
        assert result == cached

    def test_cache_miss_fetches(self, tmp_path, monkeypatch):
        monkeypatch.setattr(_mod, "CACHE", str(tmp_path))
        monkeypatch.setattr(_mod, "DELAY", 0)
        manifest = {"rpms": [{"srpm_name": "bar-2.0.src.rpm"}]}
        monkeypatch.setattr(_mod, "find_image_id", lambda d: "img789")
        monkeypatch.setattr(_mod, "api", lambda path: manifest)
        result = _mod.rpm_manifest("sha256:xyz")
        assert result == manifest
        assert (tmp_path / "sha256_xyz.json").exists()

    def test_cache_miss_no_image_id(self, tmp_path, monkeypatch):
        monkeypatch.setattr(_mod, "CACHE", str(tmp_path))
        monkeypatch.setattr(_mod, "find_image_id", lambda d: None)
        result = _mod.rpm_manifest("sha256:unknown")
        assert result is None

    def test_cache_miss_api_error(self, tmp_path, monkeypatch):
        monkeypatch.setattr(_mod, "CACHE", str(tmp_path))
        monkeypatch.setattr(_mod, "find_image_id", lambda d: "imgX")
        monkeypatch.setattr(_mod, "api", lambda path: (_ for _ in ()).throw(ConnectionError()))
        result = _mod.rpm_manifest("sha256:err")
        assert result is None

    def test_corrupt_cache_refetches(self, tmp_path, monkeypatch):
        monkeypatch.setattr(_mod, "CACHE", str(tmp_path))
        monkeypatch.setattr(_mod, "DELAY", 0)
        (tmp_path / "sha256_bad.json").write_text("not json{{{")
        manifest = {"rpms": []}
        monkeypatch.setattr(_mod, "find_image_id", lambda d: "imgOK")
        monkeypatch.setattr(_mod, "api", lambda path: manifest)
        result = _mod.rpm_manifest("sha256:bad")
        assert result == manifest


# ------------------------------------------------------------------ main()
class TestMain:
    def _setup_main(self, tmp_path, monkeypatch, images=None, manifests=None,
                    pool_files=None, argv_extra=None):
        out_dir = tmp_path / "phase-a-rpm"
        cache_dir = out_dir / "pyxis-cache"
        pool_dir = tmp_path / "pool"
        pool_dir.mkdir(parents=True)
        if pool_files:
            for f in pool_files:
                (pool_dir / f).write_text("")

        monkeypatch.setattr(_mod, "OUT_DIR", str(out_dir))
        monkeypatch.setattr(_mod, "CACHE", str(cache_dir))
        monkeypatch.setattr(_mod, "POOL", str(pool_dir))
        monkeypatch.setattr(_mod, "DELAY", 0)

        if images is None:
            images = {}
        monkeypatch.setattr(_mod, "gather_images", lambda: images)

        if manifests is None:
            manifests = {}
        monkeypatch.setattr(_mod, "rpm_manifest", lambda d: manifests.get(d))

        argv = ["prog"] + (argv_extra or [])
        monkeypatch.setattr(sys, "argv", argv)
        return out_dir, pool_dir

    def test_no_images(self, tmp_path, monkeypatch, capsys):
        out_dir, _ = self._setup_main(tmp_path, monkeypatch)
        rc = _mod.main()
        assert rc == 0
        assert (out_dir / "image-rpms.tsv").exists()
        assert (out_dir / "image-rpms-missing.txt").exists()
        assert (out_dir / "image-rpms-unresolved.tsv").exists()

    def test_resolved_images(self, tmp_path, monkeypatch, capsys):
        images = {
            "sha256:aaa": "registry/img-a",
            "sha256:bbb": "registry/img-b",
        }
        manifests = {
            "sha256:aaa": {"rpms": [
                {"srpm_name": "kernel-6.1.0-1.src.rpm"},
                {"srpm_name": "glibc-2.38-1.src.rpm"},
            ]},
            "sha256:bbb": {"rpms": [
                {"srpm_name": "kernel-6.1.0-1.src.rpm"},
            ]},
        }
        out_dir, _ = self._setup_main(tmp_path, monkeypatch, images, manifests)
        rc = _mod.main()
        assert rc == 0
        tsv = (out_dir / "image-rpms.tsv").read_text()
        assert "kernel-6.1.0-1" in tsv
        assert "glibc-2.38-1" in tsv
        missing = (out_dir / "image-rpms-missing.txt").read_text()
        assert "kernel-6.1.0-1" in missing

    def test_pool_match_not_missing(self, tmp_path, monkeypatch, capsys):
        images = {"sha256:c": "registry/c"}
        manifests = {"sha256:c": {"rpms": [
            {"srpm_name": "openssl-3.0-1.src.rpm"},
        ]}}
        out_dir, _ = self._setup_main(
            tmp_path, monkeypatch, images, manifests,
            pool_files=["openssl-3.0-1"])
        rc = _mod.main()
        assert rc == 0
        missing = (out_dir / "image-rpms-missing.txt").read_text()
        assert "openssl-3.0-1" not in missing

    def test_unresolved_images(self, tmp_path, monkeypatch, capsys):
        images = {"sha256:nope": "registry/nope"}
        out_dir, _ = self._setup_main(tmp_path, monkeypatch, images)
        rc = _mod.main()
        assert rc == 0
        unresolved = (out_dir / "image-rpms-unresolved.tsv").read_text()
        assert "sha256:nope" in unresolved
        assert "registry/nope" in unresolved

    def test_limit_flag(self, tmp_path, monkeypatch, capsys):
        images = {
            "sha256:a1": "r/a",
            "sha256:b2": "r/b",
            "sha256:c3": "r/c",
        }
        queried = []
        def tracking_manifest(d):
            queried.append(d)
            return None
        out_dir, _ = self._setup_main(
            tmp_path, monkeypatch, images, argv_extra=["--limit", "2"])
        monkeypatch.setattr(_mod, "rpm_manifest", tracking_manifest)
        rc = _mod.main()
        assert rc == 0
        assert len(queried) == 2

    def test_pool_flag(self, tmp_path, monkeypatch, capsys):
        custom_pool = tmp_path / "custom-pool"
        custom_pool.mkdir()
        (custom_pool / "zlib-1.2-1").write_text("")
        images = {"sha256:z": "r/z"}
        manifests = {"sha256:z": {"rpms": [
            {"srpm_name": "zlib-1.2-1.src.rpm"},
        ]}}
        monkeypatch.setattr(_mod, "gather_images", lambda: images)
        monkeypatch.setattr(_mod, "rpm_manifest", lambda d: manifests.get(d))
        out_dir = tmp_path / "phase-a-rpm"
        cache_dir = out_dir / "pyxis-cache"
        monkeypatch.setattr(_mod, "OUT_DIR", str(out_dir))
        monkeypatch.setattr(_mod, "CACHE", str(cache_dir))
        monkeypatch.setattr(_mod, "DELAY", 0)
        monkeypatch.setattr(sys, "argv",
                            ["prog", "--pool", str(custom_pool)])
        rc = _mod.main()
        assert rc == 0
        missing = (out_dir / "image-rpms-missing.txt").read_text()
        assert "zlib-1.2-1" not in missing

    def test_nonexistent_pool(self, tmp_path, monkeypatch, capsys):
        images = {"sha256:p": "r/p"}
        manifests = {"sha256:p": {"rpms": [
            {"srpm_name": "pkg-1.0-1.src.rpm"},
        ]}}
        out_dir, _ = self._setup_main(tmp_path, monkeypatch, images, manifests)
        monkeypatch.setattr(_mod, "POOL", str(tmp_path / "no-such-pool"))
        rc = _mod.main()
        assert rc == 0
        missing = (out_dir / "image-rpms-missing.txt").read_text()
        assert "pkg-1.0-1" in missing

    def test_rpms_without_src_suffix_ignored(self, tmp_path, monkeypatch, capsys):
        images = {"sha256:s": "r/s"}
        manifests = {"sha256:s": {"rpms": [
            {"srpm_name": "good-1.0.src.rpm"},
            {"srpm_name": "bad-1.0.x86_64.rpm"},
            {"srpm_name": ""},
            {},
        ]}}
        out_dir, _ = self._setup_main(tmp_path, monkeypatch, images, manifests)
        rc = _mod.main()
        assert rc == 0
        tsv = (out_dir / "image-rpms.tsv").read_text()
        assert "good-1.0" in tsv
        assert "bad-1.0" not in tsv

    def test_progress_print_at_100(self, tmp_path, monkeypatch, capsys):
        images = {f"sha256:{i:04d}": f"r/{i}" for i in range(100)}
        monkeypatch.setattr(_mod, "rpm_manifest", lambda d: None)
        out_dir, _ = self._setup_main(tmp_path, monkeypatch, images)
        rc = _mod.main()
        assert rc == 0
        out = capsys.readouterr().out
        assert "100/100" in out

    def test_output_header_and_sorted(self, tmp_path, monkeypatch, capsys):
        images = {
            "sha256:zzz": "r/z",
            "sha256:aaa": "r/a",
        }
        manifests = {
            "sha256:zzz": {"rpms": [{"srpm_name": "zlib-1.0.src.rpm"}]},
            "sha256:aaa": {"rpms": [{"srpm_name": "aaa-1.0.src.rpm"}]},
        }
        out_dir, _ = self._setup_main(tmp_path, monkeypatch, images, manifests)
        rc = _mod.main()
        assert rc == 0
        tsv = (out_dir / "image-rpms.tsv").read_text()
        lines = tsv.strip().split("\n")
        assert lines[0].startswith("# ")
        data_lines = [l for l in lines if not l.startswith("#")]
        assert data_lines == sorted(data_lines)
