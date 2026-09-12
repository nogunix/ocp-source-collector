"""Extra tests for deplib.py — yarn v1 classic, scan_tree edge cases,
go_escape, go_version_key, npm_tarball_url, and resolver boundary conditions.

Network-free: all resolvers are pure functions.
Run: pytest tests/test_deplib_extra.py
"""
import importlib.util
import os
import pathlib

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "deplib", pathlib.Path(__file__).resolve().parent.parent / "scripts" / "deplib.py")
deplib = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(deplib)


# --------------------------------------------------------------- go_escape
class TestGoEscape:
    def test_lowercase_unchanged(self):
        assert deplib.go_escape("golang.org/x/net") == "golang.org/x/net"

    def test_uppercase_escaped(self):
        assert deplib.go_escape("github.com/Azure/go-autorest") == \
            "github.com/!azure/go-autorest"

    def test_multiple_uppercase(self):
        assert deplib.go_escape("github.com/Azure/Azure-SDK") == \
            "github.com/!azure/!azure-!s!d!k"

    def test_empty(self):
        assert deplib.go_escape("") == ""


# ----------------------------------------------------------- go_version_key
class TestGoVersionKey:
    def test_release_outranks_prerelease(self):
        assert deplib.go_version_key("v1.2.3") > deplib.go_version_key("v1.2.3-beta.1")

    def test_higher_version_wins(self):
        assert deplib.go_version_key("v0.38.0") > deplib.go_version_key("v0.37.0")

    def test_pseudo_version_uses_timestamp(self):
        k1 = deplib.go_version_key("v0.0.0-20240101000000-aaa")
        k2 = deplib.go_version_key("v0.0.0-20240201000000-bbb")
        assert k2 > k1

    def test_incompatible_stripped(self):
        k = deplib.go_version_key("v14.2.0+incompatible")
        assert k == (14, 2, 0, 1, "")

    def test_short_version(self):
        k = deplib.go_version_key("v1.2")
        assert k[0:3] == (1, 2, 0)


# --------------------------------------------------------- npm_tarball_url
class TestNpmTarballUrl:
    def test_unscoped(self):
        assert deplib.npm_tarball_url("lodash", "4.17.21") == \
            "https://registry.npmjs.org/lodash/-/lodash-4.17.21.tgz"

    def test_scoped(self):
        assert deplib.npm_tarball_url("@types/react", "18.0.1") == \
            "https://registry.npmjs.org/@types/react/-/react-18.0.1.tgz"


# ----------------------------------------------------------- yarn v1 classic
YARN_V1 = '''\
"@babel/code-frame@^7.0.0":
  version "7.24.7"
  resolved "https://registry.yarnpkg.com/@babel/code-frame/-/code-frame-7.24.7.tgz#882fd9e09e8ee324fb176f5f7c26df37e4baf7cb"
  integrity sha512-abc

"lodash@^4.17.20":
  version "4.17.21"
  resolved "https://registry.yarnpkg.com/lodash/-/lodash-4.17.21.tgz#679591c564c3bffff9ef5880a44cecd4e71c1ec2"
  integrity sha512-xyz
'''


class TestYarnV1:
    def test_parses_classic_format(self):
        deps = {d[1]: d for d in deplib.resolve_yarn_lock(YARN_V1)}
        assert "@babel/code-frame" in deps
        assert "lodash" in deps
        assert deps["lodash"][2] == "4.17.21"
        assert "yarnpkg.com" in deps["lodash"][3]

    def test_dedupes_entries(self):
        doubled = YARN_V1 + '\n"lodash@^4.0.0":\n  version "4.17.21"\n  resolved "https://registry.yarnpkg.com/lodash/-/lodash-4.17.21.tgz"\n'
        deps = deplib.resolve_yarn_lock(doubled)
        names = [d[1] for d in deps if d[1] == "lodash"]
        assert len(names) == 1


# -------------------------------------------------------- yarn berry (v2+)
YARN_BERRY = '''\
__metadata:
  version: 6

"@types/node@npm:20.10.0":
  version: 20.10.0
  resolution: "@types/node@npm:20.10.0"

"typescript@npm:5.3.2":
  version: 5.3.2
  resolution: "typescript@npm:5.3.2"
'''


class TestYarnBerry:
    def test_parses_berry_format(self):
        deps = {d[1]: d for d in deplib.resolve_yarn_lock(YARN_BERRY)}
        assert "@types/node" in deps
        assert deps["@types/node"][2] == "20.10.0"
        assert deps["typescript"][2] == "5.3.2"

    def test_berry_builds_url_from_registry(self):
        deps = {d[1]: d for d in deplib.resolve_yarn_lock(YARN_BERRY)}
        assert deps["typescript"][3] == \
            "https://registry.npmjs.org/typescript/-/typescript-5.3.2.tgz"


# ------------------------------------------------------- cargo git dep edge
class TestCargoGitEdge:
    def test_git_dep_without_sha_marked_unfetchable(self):
        lock = '''
[[package]]
name = "strange"
version = "1.0.0"
source = "git+https://github.com/org/repo?branch=main"
'''
        deps = deplib.resolve_cargo_lock(lock)
        assert len(deps) == 1
        assert deps[0][4] == ""

    def test_git_dep_with_dotgit_suffix(self):
        lock = '''
[[package]]
name = "foo"
version = "1.0.0"
source = "git+https://github.com/org/repo.git?rev=abc#abc123456789def"
'''
        deps = deplib.resolve_cargo_lock(lock)
        assert "repo.git" not in deps[0][3]
        assert "/repo/archive/" in deps[0][3]

    def test_unknown_source_recorded(self):
        lock = '''
[[package]]
name = "custom"
version = "1.0.0"
source = "sparse+https://custom-registry.example.com"
'''
        deps = deplib.resolve_cargo_lock(lock)
        assert deps[0][3] == "sparse+https://custom-registry.example.com"


# ---------------------------------------------- resolve_python_requirements
class TestPythonRequirements:
    def test_extras_brackets(self):
        deps = deplib.resolve_python_requirements("requests[security]==2.31.0\n")
        assert len(deps) == 1
        assert deps[0][1] == "requests"

    def test_inline_comment_ignored(self):
        deps = deplib.resolve_python_requirements("flask==2.0.0  # pinned\n")
        assert len(deps) == 1

    def test_dash_r_skipped(self):
        deps = deplib.resolve_python_requirements("-r other.txt\n")
        assert len(deps) == 0

    def test_dedup_case_insensitive(self):
        text = "Flask==2.0.0\nflask==2.0.0\n"
        deps = deplib.resolve_python_requirements(text)
        assert len(deps) == 1

    def test_empty_text(self):
        assert deplib.resolve_python_requirements("") == []

    def test_unpinned_not_resolved(self):
        assert deplib.resolve_python_requirements("flask>=2.0") == []


# --------------------------------------------------------- resolve_npm_lock
class TestNpmLockEdge:
    def test_empty_json_object(self):
        assert deplib.resolve_npm_lock("{}") == []

    def test_invalid_json(self):
        assert deplib.resolve_npm_lock("not json") == []

    def test_v1_nested_dependencies(self):
        v1 = '''{
          "lockfileVersion": 1,
          "dependencies": {
            "a": {
              "version": "1.0.0",
              "resolved": "https://registry.npmjs.org/a/-/a-1.0.0.tgz",
              "dependencies": {
                "b": {
                  "version": "2.0.0",
                  "resolved": "https://registry.npmjs.org/b/-/b-2.0.0.tgz"
                }
              }
            }
          }
        }'''
        deps = {d[1] for d in deplib.resolve_npm_lock(v1)}
        assert deps == {"a", "b"}

    def test_v2_scoped_name_from_meta(self):
        lock = '''{
          "packages": {
            "": {},
            "node_modules/@scope/pkg": {
              "name": "@scope/pkg",
              "version": "1.0.0",
              "resolved": "https://registry.npmjs.org/@scope/pkg/-/pkg-1.0.0.tgz"
            }
          }
        }'''
        deps = deplib.resolve_npm_lock(lock)
        assert deps[0][1] == "@scope/pkg"


# ----------------------------------------------------------- poetry lock
class TestPoetryLockEdge:
    def test_dedup(self):
        lock = '''
[[package]]
name = "urllib3"
version = "2.2.1"

[[package]]
name = "urllib3"
version = "2.2.1"
'''
        deps = deplib.resolve_poetry_lock(lock)
        assert len(deps) == 1

    def test_empty_text(self):
        assert deplib.resolve_poetry_lock("") == []


# ------------------------------------------------------- scan_tree edges
class TestScanTreeEdges:
    def test_depth_limit(self, tmp_path):
        deep = tmp_path / "a" / "b" / "c"
        deep.mkdir(parents=True)
        (deep / "go.sum").write_text("golang.org/x/net v0.38.0 h1:sha\n")
        rows = list(deplib.scan_tree(str(tmp_path), max_depth=2))
        assert rows == []

    def test_symlink_skipped(self, tmp_path):
        (tmp_path / "real.lock").write_text("[[package]]\nname = \"x\"\nversion = \"1\"\n")
        os.symlink(str(tmp_path / "real.lock"), str(tmp_path / "Cargo.lock"))
        rows = list(deplib.scan_tree(str(tmp_path)))
        cargo_rows = [r for r in rows if "Cargo.lock" in r[0]]
        assert len(cargo_rows) == 0

    def test_testdata_dir_skipped(self, tmp_path):
        td = tmp_path / "testdata"
        td.mkdir()
        (td / "go.sum").write_text("golang.org/x/net v0.38.0 h1:sha\n")
        rows = list(deplib.scan_tree(str(tmp_path)))
        assert rows == []

    def test_node_modules_skipped(self, tmp_path):
        nm = tmp_path / "node_modules"
        nm.mkdir()
        (nm / "package-lock.json").write_text('{"packages":{}}')
        rows = list(deplib.scan_tree(str(tmp_path)))
        assert rows == []

    def test_barren_manifest_yields_none(self, tmp_path):
        (tmp_path / "requirements.txt").write_text("flask>=2.0\n")
        rows = list(deplib.scan_tree(str(tmp_path)))
        assert len(rows) == 1
        assert rows[0][1] is None

    def test_vendored_cargo_skipped(self, tmp_path):
        (tmp_path / "Cargo.lock").write_text(
            '[[package]]\nname = "x"\nversion = "1.0"\nsource = "registry+crates"\n')
        (tmp_path / "vendor").mkdir()
        rows = list(deplib.scan_tree(str(tmp_path)))
        assert rows == []

    def test_git_dir_skipped(self, tmp_path):
        gitdir = tmp_path / ".git"
        gitdir.mkdir()
        (gitdir / "go.sum").write_text("golang.org/x/net v0.38.0 h1:sha\n")
        rows = list(deplib.scan_tree(str(tmp_path)))
        assert rows == []
