"""Tests for mcp/layout.py — the one reader per casket TSV file.

The round-trip tests feed the readers what the writers produce
(scripts/build-source-index.py, scripts/collect-submodules.py), so a format
change on one side fails here instead of silently dropping rows in the tools.
"""
import importlib.util
import json
import os
import pathlib

_ROOT = pathlib.Path(__file__).resolve().parent.parent

import layout

_SPEC = importlib.util.spec_from_file_location(
    "build_source_index", _ROOT / "scripts" / "build-source-index.py")
bsi = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(bsi)


# ------------------------------------------------------------------ read_tsv
def test_read_tsv_skips_blank_and_comment_lines(tmp_path):
    f = tmp_path / "x.tsv"
    f.write_text("# header\n\na\tb\n  \nc\n")
    assert layout.read_tsv(str(f)) == [["a", "b"], ["c"]]


def test_read_tsv_missing_file(tmp_path):
    assert layout.read_tsv(str(tmp_path / "nope.tsv")) == []


# ---------------------------------------------------------------- read_index
def test_read_index_basic(tmp_path):
    idx = tmp_path / "INDEX.tsv"
    idx.write_text(
        "dir\trepo\tref\tversion\tcomponents\n"
        "cvo-abc\thttps://github.com/openshift/cvo\tabc123\t4.20\tcvo,cvo-extra\n"
        "kv-def\thttps://github.com/kubevirt/kubevirt\tdef456\t\t\n"
    )
    rows = layout.read_index(str(idx))
    assert rows[0] == {"dir": "cvo-abc", "repo": "https://github.com/openshift/cvo",
                       "ref": "abc123", "version": "4.20",
                       "components": ["cvo", "cvo-extra"]}
    assert rows[1]["version"] == "" and rows[1]["components"] == []


def test_read_index_short_rows_skipped(tmp_path):
    """The writer emits 5 columns; anything shorter is a damaged row."""
    idx = tmp_path / "INDEX.tsv"
    idx.write_text("header\n" "onlytwocols\tval\n" "a\tb\tc\n" "d\te\tf\t\t\n")
    assert layout.read_index(str(idx)) == [
        {"dir": "d", "repo": "e", "ref": "f", "version": "", "components": []}]


def test_read_index_drops_empty_component_names(tmp_path):
    idx = tmp_path / "INDEX.tsv"
    idx.write_text("header\n" "d\tr\tf\t4.20\tone,,two\n")
    assert layout.read_index(str(idx))[0]["components"] == ["one", "two"]


def test_read_index_missing_file(tmp_path):
    assert layout.read_index(str(tmp_path / "nonexistent.tsv")) == []


# ----------------------------------------------------------- read_submodules
def test_read_submodules_basic(tmp_path):
    tsv = tmp_path / "SUBMODULES.tsv"
    tsv.write_text(
        "# component\tpath\trepo\tref\texact\tstatus\n"
        "cvo-abc\tvendor/sub\torg/subrepo\taaa111\t1\tok\n"
        "kv-def\tthemes/docsy\tgoogle/docsy\tbbb222\t0\tok\n"
    )
    rows = layout.read_submodules(str(tsv))
    assert rows[0] == {"component": "cvo-abc", "path": "vendor/sub",
                       "repo": "org/subrepo", "ref": "aaa111", "exact": True}
    assert rows[1]["exact"] is False


def test_read_submodules_truncated_row_skipped(tmp_path):
    tsv = tmp_path / "SUBMODULES.tsv"
    tsv.write_text(
        "# component\tpath\trepo\tref\texact\tstatus\n"
        "short\trow\n"
        "wrapper\tspire\topenshift/spire\t" + "a" * 40 + "\t1\tok-filled\n")
    assert [r["path"] for r in layout.read_submodules(str(tsv))] == ["spire"]


def test_read_submodules_missing_file(tmp_path):
    assert layout.read_submodules(str(tmp_path / "nope.tsv")) == []


def test_paths_are_relative_to_the_unit():
    assert layout.index_path("/u") == os.path.join("/u", "git", "INDEX.tsv")
    assert layout.submodules_path("/u") == os.path.join("/u", "meta", "SUBMODULES.tsv")


# ---------------------------------------------------- writer -> reader trips
def test_index_round_trip_from_build_source_index(tmp_path, monkeypatch):
    stage = tmp_path / "stage"
    (stage / "meta").mkdir(parents=True)
    (stage / "git" / "cvo-abc123").mkdir(parents=True)
    (stage / "meta" / "MANIFEST.json").write_text(json.dumps({"images": [
        {"name": "cluster-version-operator",
         "source": {"tarball": "cvo-abc123.tar.gz",
                    "repo": "https://github.com/openshift/cluster-version-operator",
                    "commit": "abc123"}},
    ]}))
    monkeypatch.setattr("sys.argv", ["build-source-index.py", str(stage)])
    assert bsi.main() == 0
    rows = layout.read_index(layout.index_path(str(stage)))
    assert [(r["dir"], r["repo"], r["ref"], r["components"]) for r in rows] == [
        ("cvo-abc123", "https://github.com/openshift/cluster-version-operator",
         "abc123", ["cluster-version-operator"])]
