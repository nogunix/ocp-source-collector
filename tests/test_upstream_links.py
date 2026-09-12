"""Tests for scripts/check-upstream-links.py — the candidates() URL
generation logic (no network needed).
"""
import importlib.util
import pathlib

_SPEC = importlib.util.spec_from_file_location(
    "check_upstream_links",
    pathlib.Path(__file__).resolve().parent.parent / "scripts" / "check-upstream-links.py")
cul = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(cul)


def test_pin_row_generates_ref_then_v_then_bare():
    got = cul.candidates("abc123", "1.2.3", "pin")
    assert got == ["abc123", "v1.2.3", "1.2.3"]


def test_pin_row_no_duplicate_when_ref_is_vtag():
    got = cul.candidates("v1.2.3", "1.2.3", "pin")
    assert got == ["v1.2.3", "1.2.3"]


def test_tag_row_generates_v_then_bare():
    got = cul.candidates("", "4.20.0", "tag")
    assert got == ["v4.20.0", "4.20.0"]


def test_tag_row_with_ref_puts_ref_first():
    got = cul.candidates("release-4.20", "4.20.0", "tag")
    assert got == ["release-4.20", "v4.20.0", "4.20.0"]


def test_head_row_returns_HEAD():
    got = cul.candidates("main", "1.0.0", "head")
    assert got == ["HEAD"]


def test_empty_version_pin():
    got = cul.candidates("abc123", "", "pin")
    assert got == ["abc123"]


def test_row_key_formats_correctly():
    row = ("https://github.com/org/repo", "abc123", "1.2.3", "pin", "2026-07-01")
    assert cul.row_key(row) == "https://github.com/org/repo\tabc123"


def test_row_key_head_kind():
    row = ("https://github.com/org/repo", "", "", "head", "2026-07-01")
    assert cul.row_key(row) == "https://github.com/org/repo\thead"
