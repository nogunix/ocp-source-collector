"""Regression tests for casket-mcp's submodule-aware resolution.

A codeload archive writes a gitlink as an empty dir, so a wrapper repo
(`<name>-release`) is what by-component/ and INDEX.tsv name while the
component's own code sits one level down, in a tree recorded only in
meta/SUBMODULES.tsv. These cover the parsing/matching that makes those trees
resolvable — no /srv mount required.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "mcp"))
import backends as be  # noqa: E402

HEADER = "component\tpath\trepo\tref\texact\tstatus\n"


def _unit(tmp_path, rows, trees):
    unit = tmp_path / "unit"
    (unit / "meta").mkdir(parents=True)
    (unit / "meta" / "SUBMODULES.tsv").write_text(
        HEADER + "".join("\t".join(r) + "\n" for r in rows))
    for parent, sub in trees:
        (unit / "git" / parent / sub).mkdir(parents=True)
    return str(unit)


def test_parses_rows_and_builds_tree_paths(tmp_path):
    unit = _unit(
        tmp_path,
        [("ztwim-release-612309a", "zero-trust-workload-identity-manager",
          "openshift/zero-trust-workload-identity-manager", "d4b265f", "1", "ok-cached")],
        [("ztwim-release-612309a", "zero-trust-workload-identity-manager")])
    rows = be._submodule_rows(unit)
    assert len(rows) == 1
    r = rows[0]
    assert r["parent"] == "ztwim-release-612309a"
    assert r["repo"] == "https://github.com/openshift/zero-trust-workload-identity-manager"
    assert r["ref_exact"] is True
    assert r["path"] == os.path.join(
        unit, "git", "ztwim-release-612309a", "zero-trust-workload-identity-manager")


def test_row_without_a_tree_on_disk_is_dropped(tmp_path):
    """Older caskets carry no filled dirs; a row must not become a dead path."""
    unit = _unit(tmp_path, [("wrapper-abc", "spiffe-spire", "openshift/spiffe-spire",
                             "154f636", "1", "ok-cached")], [])
    assert be._submodule_rows(unit) == []


def test_missing_tsv_is_not_an_error(tmp_path):
    unit = tmp_path / "bare"
    (unit / "git").mkdir(parents=True)
    assert be._submodule_rows(str(unit)) == []


def test_same_submodule_under_sibling_wrappers_reported_once(tmp_path):
    """b-operand stages one wrapper per operand image, all with the same
    submodules — the identical (repo, ref) tree must collapse to one hit."""
    rows = [("spire-server-c73ed72", "ztwim", "openshift/ztwim", "d4b265f", "1", "ok-cached"),
            ("spire-oidc-c73ed72", "ztwim", "openshift/ztwim", "d4b265f", "1", "ok-cached")]
    unit = _unit(tmp_path, rows, [(p, s) for p, s, *_ in rows])
    got = be._submodule_rows(unit)
    assert len(got) == 1
    assert got[0]["parent"] == "spire-oidc-c73ed72"  # deterministic: sorted


def test_exact_ref_outranks_branch_head(tmp_path):
    """exact=0 is a branch head fetched today, not what was built."""
    rows = [("aaa-approx", "ztwim", "openshift/ztwim", "release-1.1", "0", "ok-cached"),
            ("zzz-exact", "ztwim", "openshift/ztwim", "d4b265f", "1", "ok-cached")]
    unit = _unit(tmp_path, rows, [(p, s) for p, s, *_ in rows])
    got = be._submodule_rows(unit)
    assert [r["ref_exact"] for r in got] == [True, False]  # despite the dir names


def test_non_github_ref_is_kept_verbatim():
    assert be._sub_repo_url("https://gitlab.com/o/r.git") == "https://gitlab.com/o/r.git"
    assert be._sub_repo_url("o/r") == "https://github.com/o/r"


def test_matching_is_two_way_and_covers_slug_base_and_path():
    row = {"slug": "openshift/spiffe-spire", "sub": "spiffe-spire"}
    assert be._sub_matches(row, "spire")                      # substring of base
    assert be._sub_matches(row, "openshift/spiffe-spire")     # full slug
    assert be._sub_matches(row, "spiffe-spire.git")           # .git stripped
    assert not be._sub_matches(row, "kubevirt")
    assert not be._sub_matches(row, "")                       # empty never matches


def test_upstream_fork_is_not_confused_with_upstream_name():
    """The RH fork is openshift/spiffe-spire; asking for upstream spiffe/spire
    still matches it (same basename), which is what a tracer wants — but the
    hit carries the real slug so the report can say which one it is."""
    row = {"slug": "openshift/spiffe-spire", "sub": "spiffe-spire"}
    assert be._sub_matches(row, "spiffe/spire") is False
    assert be._sub_matches(row, "spire") is True
