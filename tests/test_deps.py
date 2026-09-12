"""Regression tests for scripts/deplib.py (lockfile -> download URL).

Network-free: the resolvers are pure, so CI can guard them even though the
collection step itself needs crates.io / proxy.golang.org / npm / PyPI.
"""
import importlib.util
import pathlib

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "deplib", pathlib.Path(__file__).resolve().parent.parent / "scripts" / "deplib.py")
deplib = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(deplib)


# ------------------------------------------------------------------- rust
CARGO_LOCK = '''\
[[package]]
name = "rustls"
version = "0.23.40"
source = "registry+https://github.com/rust-lang/crates.io-index"

[[package]]
name = "kbs"
version = "0.1.0"
dependencies = [
 "rustls",
]

[[package]]
name = "attester"
version = "0.1.0"
source = "git+https://github.com/confidential-containers/guest-components.git?rev=1fcebcb#1fcebcb66a3c0d1b2f3e4d5c6b7a89900112233"

[[package]]
name = "kbs_protocol"
version = "0.1.0"
source = "git+https://github.com/confidential-containers/guest-components.git?rev=1fcebcb#1fcebcb66a3c0d1b2f3e4d5c6b7a89900112233"

[[package]]
name = "weird"
version = "2.0.0"
source = "git+https://gitlab.example.com/x/y#abc123"
'''


@pytest.fixture(scope="module")
def crates():
    return {d[1]: d for d in deplib.resolve_cargo_lock(CARGO_LOCK)}


def test_registry_crate_maps_to_static_crates_io(crates):
    _eco, _n, _v, url, dest, strip = crates["rustls"]
    assert url == "https://static.crates.io/crates/rustls/rustls-0.23.40.crate"
    assert dest == "crates/rustls-0.23.40"
    assert strip == "1"


def test_path_dep_is_skipped(crates):
    assert "kbs" not in crates, "no `source` line = the code is already in-tree"


def test_git_crates_from_one_repo_share_a_dest(crates):
    assert crates["attester"][3].endswith(
        "/confidential-containers/guest-components/archive/"
        "1fcebcb66a3c0d1b2f3e4d5c6b7a89900112233.tar.gz"), "strip .git and ?rev="
    assert crates["attester"][4] == crates["kbs_protocol"][4] == \
        "crates/guest-components-g1fcebcb66a3c"


def test_non_github_git_dep_is_flagged_not_dropped(crates):
    assert crates["weird"][4] == ""     # nothing fetchable -> lands in uncovered


# --------------------------------------------------------------------- go
GO_SUM = '''\
github.com/Azure/go-autorest v14.2.0+incompatible h1:sha1
github.com/Azure/go-autorest v14.2.0+incompatible/go.mod h1:sha2
golang.org/x/net v0.38.0 h1:sha3
golang.org/x/net v0.38.0/go.mod h1:sha4
'''


def test_go_sum_keeps_only_the_selected_version_per_module():
    """Minimal version selection compiles ONE version per module path."""
    multi = """
github.com/Azure/azure-sdk-for-go v46.0.0+incompatible h1:a
github.com/Azure/azure-sdk-for-go v67.2.0+incompatible h1:b
github.com/Azure/azure-sdk-for-go v68.0.0+incompatible h1:c
golang.org/x/net v0.0.0-20230101000000-aaaaaaaaaaaa h1:d
golang.org/x/net v0.38.0 h1:e
"""
    deps = {d[1]: d[2] for d in deplib.resolve_go_sum(multi)}
    assert deps == {"github.com/Azure/azure-sdk-for-go": "v68.0.0+incompatible",
                    "golang.org/x/net": "v0.38.0"}, \
        "keeping every candidate cost 1.9G of unused azure-sdk-for-go per casket"


def test_go_sum_skips_gomod_rows_and_escapes_uppercase():
    deps = deplib.resolve_go_sum(GO_SUM)
    assert len(deps) == 2, "the /go.mod hash row is not a separate archive"
    by_name = {d[1]: d for d in deps}
    az = by_name["github.com/Azure/go-autorest"]
    assert "/github.com/!azure/go-autorest/@v/" in az[3], \
        "proxy.golang.org 404s without the !lowercase escaping"
    assert az[4] == "github.com/Azure/go-autorest@v14.2.0+incompatible"[:0] + \
        "go/github.com/Azure/go-autorest@v14.2.0+incompatible"
    assert az[5] == "go"


# ------------------------------------------------------------------- node
NPM_LOCK = '''{
  "lockfileVersion": 3,
  "packages": {
    "": {"name": "root", "version": "1.0.0"},
    "node_modules/lodash": {
      "version": "4.17.21",
      "resolved": "https://registry.npmjs.org/lodash/-/lodash-4.17.21.tgz"
    },
    "node_modules/@types/react": {
      "version": "18.0.1",
      "resolved": "https://registry.npmjs.org/@types/react/-/react-18.0.1.tgz"
    },
    "node_modules/local-thing": {"version": "1.0.0", "link": true}
  }
}'''


def test_npm_lock_uses_resolved_urls_and_keeps_scopes():
    deps = {d[1]: d for d in deplib.resolve_npm_lock(NPM_LOCK)}
    assert deps["lodash"][3].endswith("/lodash-4.17.21.tgz")
    assert deps["@types/react"][4] == "npm/@types/react@18.0.1"
    assert "local-thing" not in deps, "link:/workspace deps have no tarball"
    assert "root" not in deps


def test_npm_lock_v1_bool_resolved_does_not_crash():
    """`"resolved": false` is legal in lockfile v1 -- it crashed every
    community-operators casket in the 2026-07-26 fleet run."""
    v1 = '''{
      "lockfileVersion": 1,
      "dependencies": {
        "bundled-thing": {"version": "1.0.0", "resolved": false, "bundled": true},
        "real-thing": {
          "version": "2.0.0",
          "resolved": "https://registry.npmjs.org/real-thing/-/real-thing-2.0.0.tgz",
          "dependencies": {
            "nested": {"version": "3.0.0",
                       "resolved": "https://registry.npmjs.org/nested/-/nested-3.0.0.tgz"}
          }
        }
      }
    }'''
    deps = {d[1] for d in deplib.resolve_npm_lock(v1)}
    assert deps == {"real-thing", "nested"}


# ----------------------------------------------------------------- python
def test_only_pinned_requirements_are_resolved():
    reqs = """
# comment
urllib3==2.2.1
requests[security]==2.31.0
flask>=2.0          # unpinned: no single right answer offline
-r other.txt
"""
    deps = {d[1] for d in deplib.resolve_python_requirements(reqs)}
    assert deps == {"urllib3", "requests"}


def test_poetry_lock_resolves_every_entry():
    lock = '''
[[package]]
name = "urllib3"
version = "2.2.1"

[[package]]
name = "certifi"
version = "2024.2.2"
'''
    deps = {d[1]: d[2] for d in deplib.resolve_poetry_lock(lock)}
    assert deps == {"urllib3": "2.2.1", "certifi": "2024.2.2"}


# -------------------------------------------------------------- tree scan
def test_vendored_tree_is_skipped(tmp_path):
    (tmp_path / "go.sum").write_text(GO_SUM)
    (tmp_path / "vendor").mkdir()
    assert list(deplib.scan_tree(str(tmp_path))) == [], \
        "deps already in-tree: nothing to fetch"


def test_resolver_crash_is_yielded_not_raised(tmp_path, monkeypatch):
    """One malformed lockfile must not abort the whole casket.

    A `"resolved": false` in a single community operator's package-lock took
    down seven caskets in the 2026-07-26 fleet run before this.
    """
    def boom(_text):
        raise ValueError("malformed lock")

    monkeypatch.setattr(deplib, "MANIFESTS", [("go.sum", boom),
                                              ("Cargo.lock", deplib.resolve_cargo_lock)])
    (tmp_path / "go.sum").write_text(GO_SUM)
    (tmp_path / "Cargo.lock").write_text(CARGO_LOCK)
    rows = list(deplib.scan_tree(str(tmp_path)))
    errs = [(m, d) for m, d in rows if isinstance(d, BaseException)]
    deps = [d for _m, d in rows if isinstance(d, tuple)]
    assert len(errs) == 1 and str(errs[0][1]) == "malformed lock"
    assert any(d[1] == "rustls" for d in deps), \
        "the healthy manifest in the same tree must still be collected"


def test_unvendored_tree_yields_its_manifest(tmp_path):
    (tmp_path / "go.sum").write_text(GO_SUM)
    rows = list(deplib.scan_tree(str(tmp_path)))
    assert {r[0] for r in rows} == {"go.sum"}
    assert len(rows) == 2
