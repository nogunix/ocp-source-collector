"""Tests for deplib.py's MANIFESTS routing and the _dep() helper.

The individual resolvers are well covered by test_deps.py; this file
guards the wiring — which filename pattern triggers which resolver.
"""
import fnmatch
import importlib.util
import pathlib

_SPEC = importlib.util.spec_from_file_location(
    "deplib", pathlib.Path(__file__).resolve().parent.parent / "scripts" / "deplib.py")
deplib = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(deplib)


def _resolver_for(filename):
    """Return the resolver function that MANIFESTS routes a filename to."""
    for pattern, resolver in deplib.MANIFESTS:
        if fnmatch.fnmatch(filename, pattern):
            return resolver
    return None


def test_cargo_lock_routes_to_cargo_resolver():
    assert _resolver_for("Cargo.lock") is deplib.resolve_cargo_lock


def test_go_sum_routes_to_go_resolver():
    assert _resolver_for("go.sum") is deplib.resolve_go_sum


def test_npm_lock_routes_to_npm_resolver():
    assert _resolver_for("package-lock.json") is deplib.resolve_npm_lock


def test_npm_shrinkwrap_routes_to_npm_resolver():
    assert _resolver_for("npm-shrinkwrap.json") is deplib.resolve_npm_lock


def test_yarn_lock_routes_to_yarn_resolver():
    assert _resolver_for("yarn.lock") is deplib.resolve_yarn_lock


def test_poetry_lock_routes_to_poetry_resolver():
    assert _resolver_for("poetry.lock") is deplib.resolve_poetry_lock


def test_requirements_txt_routes_to_python_resolver():
    assert _resolver_for("requirements.txt") is deplib.resolve_python_requirements


def test_custom_requirements_name_routes_to_python_resolver():
    assert _resolver_for("python-requirements.okd") is deplib.resolve_python_requirements


def test_unrecognized_file_has_no_resolver():
    assert _resolver_for("Makefile") is None
    assert _resolver_for("README.md") is None


def test_dep_helper_returns_correct_tuple():
    d = deplib._dep("crates", "tokio", "1.0.0", "https://example.com", "crates/tokio-1.0.0", "1")
    assert d == ("crates", "tokio", "1.0.0", "https://example.com", "crates/tokio-1.0.0", "1")


def test_vendored_markers_exist_for_go_and_cargo():
    assert "go.sum" in deplib.VENDORED_MARKER
    assert "Cargo.lock" in deplib.VENDORED_MARKER
    assert deplib.VENDORED_MARKER["go.sum"] == "vendor"
    assert deplib.VENDORED_MARKER["Cargo.lock"] == "vendor"


def test_scan_tree_first_pattern_wins(tmp_path):
    """If a file matches multiple patterns, the first match in MANIFESTS wins.
    requirements.txt matches both 'requirements*.txt' and 'requirements*'."""
    (tmp_path / "requirements.txt").write_text("flask==2.0.0\n")
    rows = list(deplib.scan_tree(str(tmp_path)))
    manifests_seen = {r[0] for r in rows}
    assert manifests_seen == {"requirements.txt"}
    count = sum(1 for r in rows if r[0] == "requirements.txt" and isinstance(r[1], tuple))
    assert count == 1
