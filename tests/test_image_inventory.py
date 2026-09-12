"""Tests for scripts/phase-a-rpm-image-inventory.py — gather_images() TSV
parsing and digest extraction.

Network-free: uses tmp_path fixtures with fake CASKET_WORK layouts.
Run: pytest tests/test_image_inventory.py
"""
import importlib.util
import pathlib

_SPEC = importlib.util.spec_from_file_location(
    "inventory",
    pathlib.Path(__file__).resolve().parent.parent / "scripts" / "phase-a-rpm-image-inventory.py")
inv = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(inv)


def _make_work(tmp_path):
    """Create a fake CASKET_WORK with representative TSV files."""
    # Phase A payload images
    d = tmp_path / "ocp4.20" / "00-discover"
    d.mkdir(parents=True)
    (d / "images.tsv").write_text(
        "cluster-version-operator\tregistry.redhat.io/cvo@sha256:aaa111\n"
        "kubevirt\tregistry.redhat.io/kubevirt@sha256:bbb222\n"
        "no-digest-image\tregistry.redhat.io/nodigest:latest\n"
    )
    # Phase B operator containers
    d2 = tmp_path / "phase-b" / "4.20" / "00-discover"
    d2.mkdir(parents=True)
    (d2 / "containers.tsv").write_text(
        "my-operator\tmy-bundle\tregistry.redhat.io/op@sha256:ccc333\n"
    )
    # Phase B-operand images
    d3 = tmp_path / "phase-b-operand" / "cnv" / "4.20" / "00-discover"
    d3.mkdir(parents=True)
    (d3 / "images.tsv").write_text(
        "virt-launcher\tregistry.redhat.io/virt@sha256:ddd444\n"
    )
    return tmp_path


def test_gather_images_finds_all_phases(tmp_path, monkeypatch):
    work = _make_work(tmp_path)
    monkeypatch.setattr(inv, "CASKET_WORK", str(work))
    images = inv.gather_images()
    assert "sha256:aaa111" in images
    assert "sha256:bbb222" in images
    assert "sha256:ccc333" in images
    assert "sha256:ddd444" in images
    assert len(images) == 4


def test_gather_images_skips_entries_without_digest(tmp_path, monkeypatch):
    work = _make_work(tmp_path)
    monkeypatch.setattr(inv, "CASKET_WORK", str(work))
    images = inv.gather_images()
    for digest in images:
        assert "sha256:" in digest


def test_gather_images_preserves_repo_hint(tmp_path, monkeypatch):
    work = _make_work(tmp_path)
    monkeypatch.setattr(inv, "CASKET_WORK", str(work))
    images = inv.gather_images()
    assert images["sha256:aaa111"] == "registry.redhat.io/cvo"


def test_gather_images_deduplicates(tmp_path, monkeypatch):
    d = tmp_path / "ocp4.20" / "00-discover"
    d.mkdir(parents=True)
    (d / "images.tsv").write_text(
        "img-a\tregistry.redhat.io/a@sha256:same123\n"
        "img-b\tregistry.redhat.io/b@sha256:same123\n"
    )
    monkeypatch.setattr(inv, "CASKET_WORK", str(tmp_path))
    images = inv.gather_images()
    assert len(images) == 1
    assert images["sha256:same123"] == "registry.redhat.io/a"


def test_gather_images_empty_work(tmp_path, monkeypatch):
    monkeypatch.setattr(inv, "CASKET_WORK", str(tmp_path))
    images = inv.gather_images()
    assert images == {}
