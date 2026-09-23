"""Tests for scripts/archive-provenance.py — recovering which ref each Phase B
tarball came from out of the GitHub archive's top-level directory name."""
import importlib.util
import io
import pathlib
import tarfile

_SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "archive-provenance.py"
_spec = importlib.util.spec_from_file_location("archive_provenance", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

SHA = "2d8faa383b34bd0c120bad4d23fc85a2b63fea47"


def _tarball(path, top):
    with tarfile.open(path, "w:gz") as t:
        data = b"x"
        info = tarfile.TarInfo(f"{top}/README.md")
        info.size = len(data)
        t.addfile(info, io.BytesIO(data))


def _setup(tmp_path):
    tars = tmp_path / "20-git"
    tars.mkdir()
    _tarball(tars / "rhcl-operator-2d8faa383b34.tar.gz", "kuadrant-operator-1.3.0")
    _tarball(tars / "exact-op-2d8faa383b34.tar.gz", f"exact-op-{SHA}")
    _tarball(tars / "head-op-head.tar.gz", "head-op-main")
    (tars / "broken.tar.gz").write_bytes(b"not a tarball")
    with tarfile.open(tars / "empty.tar.gz", "w:gz"):
        pass
    git = tmp_path / "git-v2.tsv"
    git.write_text(
        f"rhcl-operator\thttps://github.com/Kuadrant/kuadrant-operator\t{SHA}\t"
        "rhcl-operator-2d8faa383b34.tar.gz\t1.3.6\tpending\n"
        f"exact-op\thttps://github.com/o/exact-op\t{SHA}\texact-op-2d8faa383b34.tar.gz\t1.0\n"
        "head-op\thttps://github.com/o/head-op\t\thead-op-head.tar.gz\t1.0\n"
        "short\trow\n")
    return tars, git


def test_provenance_rows(tmp_path):
    tars, git = _setup(tmp_path)
    rows = {r.split("\t")[0]: r.split("\t")[1:] for r in _mod.provenance(str(tars), str(git))[1:]}
    assert rows["rhcl-operator-2d8faa383b34.tar.gz"] == \
        ["pre-existing:kuadrant-operator-1.3.0", "preexisting", "0"]
    assert rows["exact-op-2d8faa383b34.tar.gz"] == [f"pre-existing:exact-op-{SHA}", "sha", "1"]
    assert rows["head-op-head.tar.gz"] == ["pre-existing:head-op-main", "preexisting", "0"]
    assert rows["broken.tar.gz"] == ["pre-existing:unknown", "preexisting", "0"]
    assert rows["empty.tar.gz"] == ["pre-existing:unknown", "preexisting", "0"]


def test_main(tmp_path, capsys):
    tars, git = _setup(tmp_path)
    assert _mod.main(["prog", str(tars), str(git)]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "# tarball\turl\tkind\texact" and len(out) == 6


def test_main_usage(capsys):
    assert _mod.main(["prog"]) == 2
    assert "Usage" in capsys.readouterr().err
