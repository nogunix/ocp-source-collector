"""Extra tests for mcp/backends.py — discovery, resolve, ripgrep mocking,
permalink construction, and coverage_report branches.

Complements test_backends_fs.py (which covers read/diff/list/lockfiles/deps).
Network-free: uses monkeypatched paths and tmp_path fixtures throughout.
"""
import json
import os
import subprocess
import sys
import types

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "mcp"))
import backends as be


def _setup_srv(tmp_path, monkeypatch):
    srv = tmp_path / "srv"
    srv.mkdir()
    monkeypatch.setattr(be, "SRV", str(srv))
    monkeypatch.setattr(be, "ROOT_PREFIX", str(srv) + "/sources-")
    return srv


# ================================================================ _classify
class TestClassify:
    def test_phase_a_patch(self):
        assert be._classify("ocp4.20.22") == ("a", "4.20.22")

    def test_phase_b_operators(self):
        assert be._classify("ocp4.20-operators") == ("b", "4.20")

    def test_phase_b_certified(self):
        assert be._classify("ocp4.20-certified-operators") == ("b-certified", "4.20")

    def test_phase_b_community(self):
        assert be._classify("ocp4.18-community-operators") == ("b-community", "4.18")

    def test_phase_b_operand(self):
        assert be._classify("layered-ocp4.20") == ("b-operand", "4.20")

    def test_phase_a_rpm(self):
        assert be._classify("ocp-srpms") == ("a-rpm", "")

    def test_other(self):
        assert be._classify("something-else") == ("other", "")


# ============================================================== list_mounts
class TestListMounts:
    def test_lists_sources_dirs_only(self, tmp_path, monkeypatch):
        srv = _setup_srv(tmp_path, monkeypatch)
        (srv / "sources-ocp4.20.22").mkdir()
        (srv / "sources-ocp4.18-operators").mkdir()
        (srv / "not-a-source").mkdir()
        (srv / "sources-file").write_text("not a dir")
        mounts = be.list_mounts()
        names = {m.name for m in mounts}
        assert "sources-ocp4.20.22" in names
        assert "sources-ocp4.18-operators" in names
        assert "not-a-source" not in names
        assert "sources-file" not in names

    def test_empty_srv(self, tmp_path, monkeypatch):
        srv = _setup_srv(tmp_path, monkeypatch)
        assert be.list_mounts() == []

    def test_nonexistent_srv(self, monkeypatch):
        monkeypatch.setattr(be, "SRV", "/nonexistent-path-12345")
        monkeypatch.setattr(be, "ROOT_PREFIX", "/nonexistent-path-12345/sources-")
        assert be.list_mounts() == []

    def test_mount_fields(self, tmp_path, monkeypatch):
        srv = _setup_srv(tmp_path, monkeypatch)
        (srv / "sources-ocp4.20.22").mkdir()
        mounts = be.list_mounts()
        assert len(mounts) == 1
        m = mounts[0]
        assert m.name == "sources-ocp4.20.22"
        assert m.suffix == "ocp4.20.22"
        assert m.phase == "a"
        assert m.version == "4.20.22"


# ======================================================= _mounts_for_version
class TestMountsForVersion:
    def test_minor_matches_patch(self, tmp_path, monkeypatch):
        srv = _setup_srv(tmp_path, monkeypatch)
        (srv / "sources-ocp4.20.22").mkdir()
        (srv / "sources-ocp4.18.41").mkdir()
        mounts = be._mounts_for_version("4.20")
        assert len(mounts) == 1
        assert mounts[0].version == "4.20.22"

    def test_exact_match(self, tmp_path, monkeypatch):
        srv = _setup_srv(tmp_path, monkeypatch)
        (srv / "sources-ocp4.20-operators").mkdir()
        mounts = be._mounts_for_version("4.20")
        assert len(mounts) == 1
        assert mounts[0].phase == "b"

    def test_phase_filter(self, tmp_path, monkeypatch):
        srv = _setup_srv(tmp_path, monkeypatch)
        (srv / "sources-ocp4.20.22").mkdir()
        (srv / "sources-ocp4.20-operators").mkdir()
        mounts = be._mounts_for_version("4.20", phase="a")
        assert len(mounts) == 1
        assert mounts[0].phase == "a"

    def test_no_match(self, tmp_path, monkeypatch):
        srv = _setup_srv(tmp_path, monkeypatch)
        (srv / "sources-ocp4.20.22").mkdir()
        mounts = be._mounts_for_version("4.99")
        assert mounts == []


# ============================================================== _index_units
class TestIndexUnits:
    def test_single_layout(self, tmp_path, monkeypatch):
        srv = _setup_srv(tmp_path, monkeypatch)
        mount_dir = srv / "sources-ocp4.20.22"
        mount_dir.mkdir()
        git_dir = mount_dir / "git"
        git_dir.mkdir()
        (git_dir / "INDEX.tsv").write_text("header\n")
        m = be.Mount(path=str(mount_dir), name="sources-ocp4.20.22",
                     suffix="ocp4.20.22", phase="a", version="4.20.22")
        units = list(be._index_units(m))
        assert len(units) == 1
        assert units[0][0] == ""
        assert units[0][1] == str(mount_dir)

    def test_b_operand_layout(self, tmp_path, monkeypatch):
        srv = _setup_srv(tmp_path, monkeypatch)
        mount_dir = srv / "sources-layered-ocp4.20"
        mount_dir.mkdir()
        for prod in ("cnv", "acs"):
            pd = mount_dir / prod / "git"
            pd.mkdir(parents=True)
            (pd / "INDEX.tsv").write_text("header\n")
        m = be.Mount(path=str(mount_dir), name="sources-layered-ocp4.20",
                     suffix="layered-ocp4.20", phase="b-operand", version="4.20")
        units = list(be._index_units(m))
        assert len(units) == 2
        labels = {u[0] for u in units}
        assert "cnv" in labels
        assert "acs" in labels

    def test_no_index_tsv(self, tmp_path, monkeypatch):
        srv = _setup_srv(tmp_path, monkeypatch)
        mount_dir = srv / "sources-ocp4.20.22"
        mount_dir.mkdir()
        m = be.Mount(path=str(mount_dir), name="sources-ocp4.20.22",
                     suffix="ocp4.20.22", phase="a", version="4.20.22")
        units = list(be._index_units(m))
        assert units == []


# ============================================================== resolve_repo
def _make_mount_with_index(srv, name, index_lines, by_repo=None, by_comp=None):
    """Helper: create a mount dir with git/INDEX.tsv and optional by-repo/, by-component/."""
    mount = srv / name
    git = mount / "git"
    git.mkdir(parents=True)
    idx = "dir\trepo\tref\tversion\tcomponents\n" + index_lines
    (git / "INDEX.tsv").write_text(idx)
    if by_repo:
        br = mount / "by-repo"
        br.mkdir()
        for alias, target in by_repo.items():
            target_dir = mount / "git" / target
            target_dir.mkdir(parents=True, exist_ok=True)
            os.symlink(str(target_dir), str(br / alias))
    if by_comp:
        bc = mount / "by-component"
        bc.mkdir()
        for comp, target in by_comp.items():
            target_dir = mount / "git" / target
            target_dir.mkdir(parents=True, exist_ok=True)
            os.symlink(str(target_dir), str(bc / comp))
    return mount


class TestResolveRepo:
    def test_exact_match(self, tmp_path, monkeypatch):
        srv = _setup_srv(tmp_path, monkeypatch)
        _make_mount_with_index(
            srv, "sources-ocp4.20.22",
            "cvo-abc\thttps://github.com/openshift/cvo\tabc\t4.20\tcluster-version-operator\n",
            by_repo={"cvo": "cvo-abc"})
        hits = be.resolve_repo("cvo", "4.20")
        assert len(hits) >= 1
        assert hits[0]["repo_alias"] == "cvo"
        assert hits[0]["exact"] is True

    def test_substring_match(self, tmp_path, monkeypatch):
        srv = _setup_srv(tmp_path, monkeypatch)
        _make_mount_with_index(
            srv, "sources-ocp4.20.22",
            "kv-def\thttps://github.com/kubevirt/kubevirt\tdef\t4.20\tkubevirt\n",
            by_repo={"kubevirt": "kv-def"})
        hits = be.resolve_repo("kubevirt/kubevirt", "4.20")
        assert len(hits) >= 1
        assert hits[0]["repo_alias"] == "kubevirt"

    def test_no_by_repo_dir(self, tmp_path, monkeypatch):
        srv = _setup_srv(tmp_path, monkeypatch)
        _make_mount_with_index(
            srv, "sources-ocp4.20.22",
            "cvo-abc\thttps://github.com/openshift/cvo\tabc\t4.20\tcluster-version-operator\n")
        hits = be.resolve_repo("cvo", "4.20")
        assert hits == []


# ======================================================= resolve_component
class TestResolveComponent:
    def test_component_match(self, tmp_path, monkeypatch):
        srv = _setup_srv(tmp_path, monkeypatch)
        _make_mount_with_index(
            srv, "sources-ocp4.20.22",
            "cvo-abc\thttps://github.com/openshift/cvo\tabc\t4.20\tcluster-version-operator\n",
            by_comp={"cluster-version-operator": "cvo-abc"})
        hits = be.resolve_component("cluster-version-operator", "4.20")
        assert len(hits) >= 1
        assert hits[0]["component"] == "cluster-version-operator"

    def test_no_by_component_dir(self, tmp_path, monkeypatch):
        srv = _setup_srv(tmp_path, monkeypatch)
        _make_mount_with_index(
            srv, "sources-ocp4.20.22",
            "cvo-abc\thttps://github.com/openshift/cvo\tabc\t4.20\tcluster-version-operator\n")
        hits = be.resolve_component("cluster-version-operator", "4.20")
        assert hits == []


# =========================================================== list_components
class TestListComponents:
    def test_basic(self, tmp_path, monkeypatch):
        srv = _setup_srv(tmp_path, monkeypatch)
        _make_mount_with_index(
            srv, "sources-ocp4.20.22",
            "cvo-abc\thttps://github.com/openshift/cvo\tabc\t4.20\tcluster-version-operator\n"
            "kv-def\thttps://github.com/kubevirt/kubevirt\tdef\t4.20\tvirt-api,virt-controller\n")
        comps = be.list_components("4.20")
        assert len(comps) == 2
        assert comps[0]["dir"] == "cvo-abc"
        assert comps[1]["components"] == ["virt-api", "virt-controller"]

    def test_with_phase_filter(self, tmp_path, monkeypatch):
        srv = _setup_srv(tmp_path, monkeypatch)
        _make_mount_with_index(
            srv, "sources-ocp4.20.22",
            "cvo-abc\thttps://github.com/openshift/cvo\tabc\t4.20\tcluster-version-operator\n")
        _make_mount_with_index(
            srv, "sources-ocp4.20-operators",
            "op-xyz\thttps://github.com/openshift/op\txyz\t4.20\tmy-operator\n")
        comps_a = be.list_components("4.20", phase="a")
        comps_b = be.list_components("4.20", phase="b")
        assert len(comps_a) == 1
        assert comps_a[0]["phase"] == "a"
        assert len(comps_b) == 1
        assert comps_b[0]["phase"] == "b"


# ================================================================= _rg mock
class TestRgMock:
    def test_rg_parses_json_matches(self, monkeypatch):
        rg_output = "\n".join([
            json.dumps({"type": "match", "data": {
                "path": {"text": "/srv/sources-ocp4.20.22/git/cvo/main.go"},
                "line_number": 42,
                "lines": {"text": "func main() {}\n"}}}),
            json.dumps({"type": "begin", "data": {}}),
            json.dumps({"type": "match", "data": {
                "path": {"text": "/srv/sources-ocp4.20.22/git/cvo/util.go"},
                "line_number": 10,
                "lines": {"text": "func helper() {}\n"}}}),
        ])

        def fake_run(cmd, **kw):
            return types.SimpleNamespace(stdout=rg_output, stderr="", returncode=0)

        monkeypatch.setattr(subprocess, "run", fake_run)
        results = be._rg(["-e", "func", "/tmp"])
        assert len(results) == 2
        assert results[0]["line"] == 42
        assert results[0]["text"] == "func main() {}"
        assert results[1]["path"].endswith("util.go")

    def test_rg_timeout(self, monkeypatch):
        def fake_run(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, 60)

        monkeypatch.setattr(subprocess, "run", fake_run)
        results = be._rg(["-e", "x", "/tmp"])
        assert len(results) == 1
        assert "error" in results[0]

    def test_rg_invalid_json_skipped(self, monkeypatch):
        rg_output = "not json\n{bad json too\n"

        def fake_run(cmd, **kw):
            return types.SimpleNamespace(stdout=rg_output, stderr="", returncode=1)

        monkeypatch.setattr(subprocess, "run", fake_run)
        results = be._rg(["-e", "x", "/tmp"])
        assert results == []


# ================================================================= grep
class TestGrep:
    def test_grep_basic(self, tmp_path, monkeypatch):
        srv = _setup_srv(tmp_path, monkeypatch)
        mount = srv / "sources-ocp4.20.22"
        mount.mkdir()
        monkeypatch.setattr(be, "_rg", lambda args, **kw: [
            {"path": "/fake/path", "line": 1, "text": "match"}
        ])
        result = be.grep("pattern", str(mount))
        assert result["backend"] == "ripgrep"
        assert result["count"] == 1

    def test_grep_with_glob_and_ignore_case(self, tmp_path, monkeypatch):
        srv = _setup_srv(tmp_path, monkeypatch)
        mount = srv / "sources-ocp4.20.22"
        mount.mkdir()
        captured_args = {}

        def fake_rg(args, **kw):
            captured_args["args"] = args
            return []

        monkeypatch.setattr(be, "_rg", fake_rg)
        be.grep("pat", str(mount), glob="*.go", ignore_case=True)
        assert "-i" in captured_args["args"]
        assert "-g" in captured_args["args"]
        assert "*.go" in captured_args["args"]


# ============================================================= _parse_index
class TestParseIndex:
    def test_basic(self, tmp_path):
        idx = tmp_path / "INDEX.tsv"
        idx.write_text(
            "dir\trepo\tref\tversion\tcomponents\n"
            "cvo-abc\thttps://github.com/openshift/cvo\tabc123\t4.20\tcluster-version-operator\n"
            "kv-def\thttps://github.com/kubevirt/kubevirt\tdef456\t4.20\tkubevirt\n"
        )
        rows = be._parse_index(str(idx))
        assert len(rows) == 2
        assert rows[0] == ("cvo-abc", "https://github.com/openshift/cvo", "abc123")
        assert rows[1][0] == "kv-def"

    def test_missing_file(self, tmp_path):
        rows = be._parse_index(str(tmp_path / "nonexistent.tsv"))
        assert rows == []

    def test_short_lines_skipped(self, tmp_path):
        idx = tmp_path / "INDEX.tsv"
        idx.write_text("header\n" "onlytwocols\tval\n" "a\tb\tc\n")
        rows = be._parse_index(str(idx))
        assert len(rows) == 1


# ======================================================= _parse_submodules
class TestParseSubmodules:
    def test_basic(self, tmp_path):
        tsv = tmp_path / "SUBMODULES.tsv"
        tsv.write_text(
            "cvo-abc\tvendor/sub\torg/subrepo\taaa111\t1\tok\n"
            "kv-def\tthemes/docsy\tgoogle/docsy\tbbb222\t0\tok\n"
        )
        rows = be._parse_submodules(str(tsv))
        assert len(rows) == 2
        assert rows[0]["component"] == "cvo-abc"
        assert rows[0]["exact"] is True
        assert rows[1]["exact"] is False

    def test_comments_skipped(self, tmp_path):
        tsv = tmp_path / "SUBMODULES.tsv"
        tsv.write_text(
            "# header comment\n"
            "cvo-abc\tvendor/sub\torg/subrepo\taaa111\t1\tok\n"
        )
        rows = be._parse_submodules(str(tsv))
        assert len(rows) == 1

    def test_missing_file(self, tmp_path):
        rows = be._parse_submodules(str(tmp_path / "nope.tsv"))
        assert rows == []


# ============================================================= _github_url
class TestGithubUrl:
    def test_github_url(self):
        assert be._github_url("https://github.com/openshift/cvo") == \
            "https://github.com/openshift/cvo"

    def test_github_url_with_git_suffix(self):
        assert be._github_url("https://github.com/openshift/cvo.git") == \
            "https://github.com/openshift/cvo"

    def test_github_url_with_trailing_slash(self):
        assert be._github_url("https://github.com/openshift/cvo/") == \
            "https://github.com/openshift/cvo"

    def test_non_github(self):
        assert be._github_url("https://gitlab.com/foo/bar") is None

    def test_empty(self):
        assert be._github_url("") is None

    def test_none_input(self):
        assert be._github_url(None) is None


# ============================================================= permalink
class TestPermalink:
    def _make_permalink_fixture(self, srv):
        mount = srv / "sources-ocp4.20.22"
        git = mount / "git"
        src = git / "cvo-abc"
        src.mkdir(parents=True)
        (src / "main.go").write_text("package main\n")
        idx = git / "INDEX.tsv"
        idx.write_text(
            "dir\trepo\tref\tversion\tcomponents\n"
            "cvo-abc\thttps://github.com/openshift/cvo\tabc123\t4.20\tcluster-version-operator\n"
        )
        return mount, src

    def test_basic_permalink(self, tmp_path, monkeypatch):
        srv = _setup_srv(tmp_path, monkeypatch)
        mount, src = self._make_permalink_fixture(srv)
        result = be.permalink(str(src / "main.go"))
        assert result["url"] == "https://github.com/openshift/cvo/blob/abc123/main.go"
        assert result["source"] == "INDEX"
        assert result["exact"] is True

    def test_permalink_with_line(self, tmp_path, monkeypatch):
        srv = _setup_srv(tmp_path, monkeypatch)
        mount, src = self._make_permalink_fixture(srv)
        result = be.permalink(str(src / "main.go"), line=42)
        assert result["url"].endswith("#L42")
        assert result["line"] == 42

    def test_permalink_nonexistent_path(self, tmp_path, monkeypatch):
        srv = _setup_srv(tmp_path, monkeypatch)
        mount, _ = self._make_permalink_fixture(srv)
        result = be.permalink(str(mount / "git" / "cvo-abc" / "nonexistent.go"))
        assert "error" in result

    def test_permalink_dir_not_in_index(self, tmp_path, monkeypatch):
        srv = _setup_srv(tmp_path, monkeypatch)
        mount, _ = self._make_permalink_fixture(srv)
        unknown = mount / "git" / "unknown-dir"
        unknown.mkdir()
        (unknown / "file.go").write_text("x")
        result = be.permalink(str(unknown / "file.go"))
        assert "error" in result
        assert "not found in" in result["error"]

    def test_permalink_non_github_repo(self, tmp_path, monkeypatch):
        srv = _setup_srv(tmp_path, monkeypatch)
        mount = srv / "sources-ocp4.20.22"
        git = mount / "git"
        src = git / "internal-repo"
        src.mkdir(parents=True)
        (src / "main.go").write_text("x")
        (git / "INDEX.tsv").write_text(
            "dir\trepo\tref\tversion\tcomponents\n"
            "internal-repo\thttps://gitlab.internal/foo/bar\tabc\t4.20\tinternal\n"
        )
        result = be.permalink(str(src / "main.go"))
        assert result["url"] is None
        assert "not on GitHub" in result["note"]

    def test_permalink_not_under_git(self, tmp_path, monkeypatch):
        srv = _setup_srv(tmp_path, monkeypatch)
        mount = srv / "sources-ocp4.20.22"
        git = mount / "git"
        git.mkdir(parents=True)
        (git / "INDEX.tsv").write_text("dir\trepo\tref\tv\tc\n")
        meta = mount / "meta"
        meta.mkdir()
        (meta / "file.txt").write_text("x")
        result = be.permalink(str(meta / "file.txt"))
        assert "error" in result

    def test_permalink_no_index(self, tmp_path, monkeypatch):
        srv = _setup_srv(tmp_path, monkeypatch)
        mount = srv / "sources-ocp4.20.22"
        mount.mkdir()
        (mount / "file.txt").write_text("x")
        result = be.permalink(str(mount / "file.txt"))
        assert "error" in result

    def test_permalink_submodule(self, tmp_path, monkeypatch):
        srv = _setup_srv(tmp_path, monkeypatch)
        mount = srv / "sources-ocp4.20.22"
        git = mount / "git"
        src = git / "wrapper-abc"
        sub_dir = src / "vendor" / "sub"
        sub_dir.mkdir(parents=True)
        (sub_dir / "lib.go").write_text("package sub\n")
        (git / "INDEX.tsv").write_text(
            "dir\trepo\tref\tversion\tcomponents\n"
            "wrapper-abc\thttps://github.com/org/wrapper\twrap123\t4.20\twrapper\n"
        )
        meta = mount / "meta"
        meta.mkdir()
        (meta / "SUBMODULES.tsv").write_text(
            "wrapper-abc\tvendor/sub\torg/subrepo\tsub456\t1\tok\n"
        )
        result = be.permalink(str(sub_dir / "lib.go"))
        assert result["source"] == "SUBMODULES"
        assert "org/subrepo" in result["repo"]
        assert result["ref"] == "sub456"
        assert result["exact"] is True
        assert "lib.go" in result["url"]

    def test_permalink_submodule_inexact(self, tmp_path, monkeypatch):
        srv = _setup_srv(tmp_path, monkeypatch)
        mount = srv / "sources-ocp4.20.22"
        git = mount / "git"
        src = git / "wrapper-abc"
        sub_dir = src / "themes" / "docsy"
        sub_dir.mkdir(parents=True)
        (sub_dir / "README.md").write_text("# docsy\n")
        (git / "INDEX.tsv").write_text(
            "dir\trepo\tref\tversion\tcomponents\n"
            "wrapper-abc\thttps://github.com/org/wrapper\twrap123\t4.20\twrapper\n"
        )
        meta = mount / "meta"
        meta.mkdir()
        (meta / "SUBMODULES.tsv").write_text(
            "wrapper-abc\tthemes/docsy\tgoogle/docsy\tmain\t0\tok\n"
        )
        result = be.permalink(str(sub_dir / "README.md"))
        assert result["exact"] is False
        assert "note" in result
        assert "approximate" in result["note"]


# ============================================= _submodule_rows + _sub_matches
class TestSubmoduleRows:
    def test_parses_and_dedupes(self, tmp_path):
        unit = tmp_path / "unit"
        meta = unit / "meta"
        meta.mkdir(parents=True)
        git = unit / "git"
        (git / "wrapper-a" / "sub1").mkdir(parents=True)
        (git / "wrapper-b" / "sub1").mkdir(parents=True)
        (meta / "SUBMODULES.tsv").write_text(
            "component\tpath\trepo\tref\texact\tstatus\n"
            "wrapper-a\tsub1\torg/subrepo\taaa\t1\tok\n"
            "wrapper-b\tsub1\torg/subrepo\taaa\t0\tok\n"
        )
        rows = be._submodule_rows(str(unit))
        assert len(rows) == 1
        assert rows[0]["ref_exact"] is True

    def test_skips_missing_dirs(self, tmp_path):
        unit = tmp_path / "unit"
        meta = unit / "meta"
        meta.mkdir(parents=True)
        (unit / "git").mkdir()
        (meta / "SUBMODULES.tsv").write_text(
            "component\tpath\trepo\tref\texact\tstatus\n"
            "wrapper-a\tsub1\torg/subrepo\taaa\t1\tok\n"
        )
        rows = be._submodule_rows(str(unit))
        assert rows == []

    def test_no_submodules_file(self, tmp_path):
        rows = be._submodule_rows(str(tmp_path))
        assert rows == []


class TestSubMatches:
    def test_slug_match(self):
        row = {"slug": "kubevirt/kubevirt", "sub": "kubevirt"}
        assert be._sub_matches(row, "kubevirt") is True

    def test_no_match(self):
        row = {"slug": "kubevirt/kubevirt", "sub": "kubevirt"}
        assert be._sub_matches(row, "unrelated") is False

    def test_empty_query(self):
        row = {"slug": "kubevirt/kubevirt", "sub": "kubevirt"}
        assert be._sub_matches(row, "") is False


# ======================================================= coverage_report extra
class TestCoverageReportExtra:
    def test_b_operand_with_products(self, tmp_path, monkeypatch):
        srv = _setup_srv(tmp_path, monkeypatch)
        mount = srv / "sources-layered-ocp4.20"
        for prod in ("cnv", "acs"):
            git = mount / prod / "git"
            git.mkdir(parents=True)
            idx = "dir\trepo\tref\tversion\tcomponents\n"
            idx += f"comp-{prod}\thttps://github.com/x/{prod}\tabc\t4.20\t{prod}\n"
            (git / "INDEX.tsv").write_text(idx)
        monkeypatch.setattr(be, "CASKET_WORK", str(tmp_path / "work"))
        cfg_dir = tmp_path / "work" / "config"
        cfg_dir.mkdir(parents=True)
        (cfg_dir / "phase-b-operand-products.tsv").write_text(
            "# comment\n"
            "idx\tcnv\n"
            "idx\tacs\n"
            "idx\tmissing-product\n"
        )
        result = be.coverage_report("4.20")
        assert "b-operand" in result["phases"]
        prods = result["phases"]["b-operand"]["products"]
        assert "cnv" in prods
        assert "acs" in prods
        assert "missing-product" in result["missing_b_operand_products"]

    def test_a_rpm_srpms_branch(self, tmp_path, monkeypatch):
        """A minor version picks up the by-ocp/<patch>/ SRPM count."""
        srv = _setup_srv(tmp_path, monkeypatch)
        pv_dir = srv / "sources-ocp-srpms" / "by-ocp" / "4.20.22"
        pv_dir.mkdir(parents=True)
        for i in range(5):
            (pv_dir / f"pkg-{i}.src.rpm").write_text("")
        monkeypatch.setattr(be, "CASKET_WORK", str(tmp_path / "nonexistent"))

        result = be.coverage_report("4.20")
        assert result["phases"]["a-rpm"] == {
            "mount": "sources-ocp-srpms", "ocp_version": "4.20.22", "srpms": 5}

    def test_a_rpm_exact_patch_match(self, tmp_path, monkeypatch):
        srv = _setup_srv(tmp_path, monkeypatch)
        pv_dir = srv / "sources-ocp-srpms" / "by-ocp" / "4.20.22"
        pv_dir.mkdir(parents=True)
        (pv_dir / "pkg.src.rpm").write_text("")
        monkeypatch.setattr(be, "CASKET_WORK", str(tmp_path / "nonexistent"))

        result = be.coverage_report("4.20.22")
        assert result["phases"]["a-rpm"]["srpms"] == 1

    def test_a_rpm_other_minor_ignored(self, tmp_path, monkeypatch):
        """4.2 must not swallow 4.20.x — the prefix test appends the dot."""
        srv = _setup_srv(tmp_path, monkeypatch)
        pv_dir = srv / "sources-ocp-srpms" / "by-ocp" / "4.20.22"
        pv_dir.mkdir(parents=True)
        monkeypatch.setattr(be, "CASKET_WORK", str(tmp_path / "nonexistent"))

        assert "a-rpm" not in be.coverage_report("4.2")["phases"]

    def test_a_rpm_unreadable_dir_skipped(self, tmp_path, monkeypatch):
        """A by-ocp entry that is a file, not a dir, is skipped not fatal."""
        srv = _setup_srv(tmp_path, monkeypatch)
        by_ocp = srv / "sources-ocp-srpms" / "by-ocp"
        by_ocp.mkdir(parents=True)
        (by_ocp / "4.20.22").write_text("not a directory")
        monkeypatch.setattr(be, "CASKET_WORK", str(tmp_path / "nonexistent"))

        result = be.coverage_report("4.20")
        assert "a-rpm" not in result["phases"]

    def test_no_srpms_root(self, tmp_path, monkeypatch):
        _setup_srv(tmp_path, monkeypatch)
        monkeypatch.setattr(be, "CASKET_WORK", str(tmp_path / "nonexistent"))
        assert "a-rpm" not in be.coverage_report("4.20")["phases"]

    def test_multiple_phases(self, tmp_path, monkeypatch):
        srv = _setup_srv(tmp_path, monkeypatch)
        # Phase a mount
        mount_a = srv / "sources-ocp4.20.22"
        git_a = mount_a / "git"
        git_a.mkdir(parents=True)
        (git_a / "INDEX.tsv").write_text(
            "dir\trepo\tref\tversion\tcomponents\n"
            "cvo\thttps://github.com/openshift/cvo\tabc\t4.20\tcvo\n"
        )
        # Phase b mount
        mount_b = srv / "sources-ocp4.20-operators"
        git_b = mount_b / "git"
        git_b.mkdir(parents=True)
        (git_b / "INDEX.tsv").write_text(
            "dir\trepo\tref\tversion\tcomponents\n"
            "op\thttps://github.com/openshift/op\tdef\t4.20\top\n"
        )
        monkeypatch.setattr(be, "CASKET_WORK", str(tmp_path / "nonexistent"))
        result = be.coverage_report("4.20")
        assert "a" in result["phases"]
        assert "b" in result["phases"]
        assert result["phases"]["a"]["source_dirs"] == 1
        assert result["phases"]["b"]["source_dirs"] == 1


# =========================================== _sub_repo_url
class TestSubRepoUrl:
    def test_github_slug(self):
        assert be._sub_repo_url("org/repo") == "https://github.com/org/repo"

    def test_non_slug(self):
        assert be._sub_repo_url("https://some-url") == "https://some-url"


# =========================================== search_text extra branches
class TestSearchTextBranches:
    def test_opengrok_engine_returns_error(self, monkeypatch):
        monkeypatch.setattr(be, "opengrok_up", lambda timeout=3.0: True)
        monkeypatch.setattr(be, "opengrok_search",
                            lambda *a, **kw: {"error": "test error", "backend": "opengrok"})
        result = be.search_text("query", engine="opengrok")
        assert result["backend"] == "opengrok"
        assert "error" in result

    def test_auto_with_opengrok_up_success(self, monkeypatch):
        monkeypatch.setattr(be, "opengrok_up", lambda timeout=3.0: True)
        monkeypatch.setattr(be, "opengrok_search",
                            lambda *a, **kw: {"backend": "opengrok", "count": 1,
                                              "results": [{"path": "/x", "line": 1, "text": "y"}]})
        result = be.search_text("query", engine="auto")
        assert result["backend"] == "opengrok"
        assert result["count"] == 1

    def test_auto_opengrok_up_but_error_falls_to_none(self, monkeypatch):
        monkeypatch.setattr(be, "opengrok_up", lambda timeout=3.0: True)
        monkeypatch.setattr(be, "opengrok_search",
                            lambda *a, **kw: {"error": "fail", "backend": "opengrok"})
        result = be.search_text("query", engine="auto")
        assert result["backend"] == "none"


# =========================================== search_symbol extra branches
class TestSearchSymbolBranches:
    def test_opengrok_up_success(self, monkeypatch):
        monkeypatch.setattr(be, "opengrok_up", lambda: True)
        monkeypatch.setattr(be, "opengrok_search",
                            lambda *a, **kw: {"backend": "opengrok", "count": 1,
                                              "results": [{"path": "/x", "line": 1}]})
        result = be.search_symbol("MyFunc")
        assert result["backend"] == "opengrok"

    def test_opengrok_down_with_path_fallback(self, tmp_path, monkeypatch):
        srv = _setup_srv(tmp_path, monkeypatch)
        mount = srv / "sources-ocp4.20.22"
        mount.mkdir()
        (mount / "main.go").write_text("func MyFunc() {}\n")
        monkeypatch.setattr(be, "opengrok_up", lambda: False)
        monkeypatch.setattr(be, "_rg", lambda args, **kw: [
            {"path": str(mount / "main.go"), "line": 1, "text": "func MyFunc() {}"}
        ])
        result = be.search_symbol("MyFunc", path=str(mount))
        assert "ripgrep" in result["backend"]


# ===================================================== partial-branch coverage
class TestMountsForVersionMinorFallback:
    def test_patch_mount_matches_a_different_patch_of_the_same_minor(
            self, tmp_path, monkeypatch):
        """4.18.99 is not mounted, but the 4.18 line is — the minor still matches."""
        srv = _setup_srv(tmp_path, monkeypatch)
        (srv / "sources-ocp4.18.41").mkdir()
        (srv / "sources-ocp4.20.22").mkdir()
        names = {m.name for m in be._mounts_for_version("4.18.99")}
        assert names == {"sources-ocp4.18.41"}

    def test_different_minor_is_excluded(self, tmp_path, monkeypatch):
        srv = _setup_srv(tmp_path, monkeypatch)
        (srv / "sources-ocp4.18.41").mkdir()
        assert be._mounts_for_version("4.19.1") == []


class TestResolveComponentSkipsNonMatchingSubmodules:
    def test_unrelated_submodule_row_not_returned(self, tmp_path, monkeypatch):
        srv = _setup_srv(tmp_path, monkeypatch)
        mount = srv / "sources-ocp4.20.22"
        (mount / "git").mkdir(parents=True)
        (mount / "meta").mkdir(parents=True)
        (mount / "git" / "INDEX.tsv").write_text(
            "dir\trepo\tref\tversion\tcomponents\n")
        bycomp = mount / "by-component"
        bycomp.mkdir()
        for sub in ("spire", "csi"):
            (mount / "git" / "wrapper" / sub).mkdir(parents=True)
        (mount / "meta" / "SUBMODULES.tsv").write_text(
            "# component\tpath\trepo\tref\texact\tstatus\n"
            "wrapper\tspire\topenshift/spiffe-spire\t" + "a" * 40 + "\t1\tok-filled\n"
            "wrapper\tcsi\topenshift/spiffe-csi\t" + "b" * 40 + "\t1\tok-filled\n")

        hits = be.resolve_component("spiffe-csi", "4.20")
        assert [h["component"] for h in hits] == ["spiffe-csi"]


class TestListComponentsShortRows:
    def test_truncated_index_row_skipped(self, tmp_path, monkeypatch):
        srv = _setup_srv(tmp_path, monkeypatch)
        mount = srv / "sources-ocp4.20.22"
        (mount / "git").mkdir(parents=True)
        (mount / "git" / "INDEX.tsv").write_text(
            "dir\trepo\tref\tversion\tcomponents\n"
            "truncated\trepo\tref\n"
            "good\thttps://github.com/o/g\tabc\t4.20\tgood\n")
        dirs = [c["dir"] for c in be.list_components("4.20")]
        assert dirs == ["good"]


class TestRipgrepIgnoreCaseFlag:
    def test_case_sensitive_search_omits_dash_i(self, tmp_path, monkeypatch):
        srv = _setup_srv(tmp_path, monkeypatch)
        (srv / "sources-ocp4.20.22").mkdir()
        captured = {}
        monkeypatch.setattr(be, "_rg", lambda args: captured.setdefault("args", args) or [])

        be._rg_search_text("Foo", "4.20", None, 10, False)
        assert "-i" not in captured["args"]

    def test_case_insensitive_search_adds_dash_i(self, tmp_path, monkeypatch):
        srv = _setup_srv(tmp_path, monkeypatch)
        (srv / "sources-ocp4.20.22").mkdir()
        captured = {}
        monkeypatch.setattr(be, "_rg", lambda args: captured.setdefault("args", args) or [])

        be._rg_search_text("Foo", "4.20", None, 10, True)
        assert "-i" in captured["args"]


class TestFindLockfiles:
    def test_ignores_non_lockfile_siblings(self, tmp_path):
        (tmp_path / "Cargo.lock").write_text("")
        (tmp_path / "Cargo.toml").write_text("")
        (tmp_path / "README.md").write_text("")
        assert be._find_lockfiles(str(tmp_path), {"Cargo.lock"}) == [
            str(tmp_path / "Cargo.lock")]

    def test_stops_at_limit(self, tmp_path):
        for i in range(5):
            d = tmp_path / f"crate{i}"
            d.mkdir()
            (d / "Cargo.lock").write_text("")
        assert len(be._find_lockfiles(str(tmp_path), {"Cargo.lock"}, limit=2)) == 2

    def test_skips_vendored_trees(self, tmp_path):
        (tmp_path / "vendor").mkdir()
        (tmp_path / "vendor" / "Cargo.lock").write_text("")
        (tmp_path / "Cargo.lock").write_text("")
        assert be._find_lockfiles(str(tmp_path), {"Cargo.lock"}) == [
            str(tmp_path / "Cargo.lock")]


class TestParseSubmodulesShortRows:
    def test_truncated_row_skipped(self, tmp_path):
        tsv = tmp_path / "SUBMODULES.tsv"
        tsv.write_text(
            "# component\tpath\trepo\tref\texact\tstatus\n"
            "short\trow\n"
            "wrapper\tspire\topenshift/spire\t" + "a" * 40 + "\t1\tok-filled\n")
        rows = be._parse_submodules(str(tsv))
        assert [r["path"] for r in rows] == ["spire"]


class TestPermalinkMountAndUnitSelection:
    def test_picks_the_right_mount_out_of_several(self, tmp_path, monkeypatch):
        srv = _setup_srv(tmp_path, monkeypatch)
        (srv / "sources-ocp4.18.41").mkdir()      # listed first, not the match
        mount = srv / "sources-ocp4.20.22"
        git = mount / "git" / "cvo-abc"
        git.mkdir(parents=True)
        (mount / "git" / "INDEX.tsv").write_text(
            "dir\trepo\tref\tversion\tcomponents\n"
            "cvo-abc\thttps://github.com/openshift/cvo\tabc123\t4.20\tcvo\n")
        f = git / "main.go"
        f.write_text("package main\n")

        result = be.permalink(str(f))
        assert result["mount"] == "sources-ocp4.20.22"
        assert result["file"] == "main.go"

    def test_picks_the_right_b_operand_product_unit(self, tmp_path, monkeypatch):
        srv = _setup_srv(tmp_path, monkeypatch)
        mount = srv / "sources-layered-ocp4.20"
        for prod in ("acs", "cnv"):
            (mount / prod / "git").mkdir(parents=True)
            (mount / prod / "git" / "INDEX.tsv").write_text(
                "dir\trepo\tref\tversion\tcomponents\n"
                f"{prod}-tree\thttps://github.com/o/{prod}\t{prod}sha\t1.0\t{prod}\n")
        f = mount / "cnv" / "git" / "cnv-tree" / "main.go"
        f.parent.mkdir(parents=True)
        f.write_text("package main\n")

        result = be.permalink(str(f))
        assert result["ref"] == "cnvsha"
        assert result["repo"] == "https://github.com/o/cnv"


class TestIndexStatsRows:
    def test_row_without_repo_or_components(self, tmp_path, monkeypatch):
        srv = _setup_srv(tmp_path, monkeypatch)
        mount = srv / "sources-ocp4.20.22"
        (mount / "git").mkdir(parents=True)
        (mount / "git" / "INDEX.tsv").write_text(
            "dir\trepo\tref\tversion\tcomponents\n"
            "orphan\t\t\t\t\n"                       # no repo, no components
            "dup\thttps://github.com/o/r\tabc\t4.20\tone,,two\n"
            "short\trow\n")
        monkeypatch.setattr(be, "CASKET_WORK", str(tmp_path / "nonexistent"))

        stats = be.coverage_report("4.20")["phases"]["a"]
        assert stats["source_dirs"] == 2       # the 2-col row is not counted
        assert stats["repos"] == 1
        assert stats["components"] == 2        # the empty name between commas drops


class TestCoverageReportMountSelection:
    def test_other_phase_mount_ignored(self, tmp_path, monkeypatch):
        """sources-<something unrecognised> classifies as "other" and is skipped."""
        srv = _setup_srv(tmp_path, monkeypatch)
        (srv / "sources-scratch").mkdir()
        monkeypatch.setattr(be, "CASKET_WORK", str(tmp_path / "nonexistent"))
        result = be.coverage_report("")
        assert "other" not in result["phases"]

    def test_mount_without_index_tsv_contributes_no_phase(self, tmp_path,
                                                          monkeypatch):
        srv = _setup_srv(tmp_path, monkeypatch)
        (srv / "sources-ocp4.20.22").mkdir()      # no git/INDEX.tsv at all
        monkeypatch.setattr(be, "CASKET_WORK", str(tmp_path / "nonexistent"))
        result = be.coverage_report("4.20")
        assert "a" not in result["phases"]
