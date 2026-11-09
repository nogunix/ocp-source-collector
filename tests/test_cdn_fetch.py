"""containers/srpm-collect/cdn-fetch.py: releasever / repo-major selection.

Network-free: index_repo and download are replaced, so what is under test is
which (repo, releasever) pairs get indexed and where the results land -- the
part that has to be right for el10 (RHCOS 10, the OCP 5.0 default node OS).
"""
import importlib.util
import os
import sys

import pytest

_PATH = os.path.join(os.path.dirname(__file__), "..", "containers", "srpm-collect", "cdn-fetch.py")
_spec = importlib.util.spec_from_file_location("cdn_fetch", _PATH)
cf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cf)


@pytest.mark.parametrize("release,want", [
    ("4.el9_2", "9.2"),
    ("4.el9_2.1", "9.2"),
    ("4.el9", "9"),            # plain: the bare major, not "" + a hardcoded 9
    ("1.el10", "10"),
    ("211.73.1.el10_2", "10.2"),
    ("202610292257.p2.g9186725.assembly.stream.el10", "10"),
    ("1.fc44", None),
])
def test_releasever_for(release, want):
    assert cf.releasever_for(release) == want


def test_repo_major_and_major_of():
    assert cf.repo_major("rhel-10-for-x86_64-baseos-source-rpms") == "10"
    assert cf.repo_major("rhocp-5.0-for-rhel-10-x86_64-source-rpms") == "10"
    assert cf.repo_major("codeready-builder-for-rhel-9-x86_64-source-rpms") == "9"
    assert cf.repo_major("some-product-source-rpms") is None
    assert cf.major_of("9.6") == "9" and cf.major_of("10") == "10"


def test_repo_patterns_cover_el10_rhocp():
    pats = cf.repo_patterns({"10"})
    ok = ["rhel-10-for-x86_64-baseos-eus-source-rpms",
          "rhocp-5.0-for-rhel-10-x86_64-source-rpms",
          "fast-datapath-for-rhel-10-x86_64-source-rpms"]
    assert all(any(p.match(r) for p in pats) for r in ok)
    assert not any(p.match("rhel-9-for-x86_64-baseos-source-rpms") for p in pats)


REPO_FILE = """\
[rhel-9-for-x86_64-baseos-source-rpms]
baseurl = https://cdn/content/dist/rhel9/$releasever/x86_64/baseos/source/SRPMS
[rhel-9-for-x86_64-baseos-eus-source-rpms]
baseurl = https://cdn/content/eus/rhel9/$releasever/x86_64/baseos/source/SRPMS
[rhel-10-for-x86_64-baseos-source-rpms]
baseurl = https://cdn/content/dist/rhel10/$releasever/x86_64/baseos/source/SRPMS
[rhocp-5.0-for-rhel-10-x86_64-source-rpms]
baseurl = https://cdn/content/dist/layered/rhel10/x86_64/rhocp/5.0/source/SRPMS
[rhel-10-for-x86_64-baseos-rpms]
baseurl = https://cdn/binary/not/a/source/repo
"""


def _run(tmp_path, monkeypatch, missing, extra=()):
    (tmp_path / "redhat.repo").write_text(REPO_FILE)
    (tmp_path / "missing.txt").write_text("\n".join(missing) + "\n")
    calls = []
    catalog = {  # what each repo "has", keyed by (repo-id, releasever)
        ("rhel-10-for-x86_64-baseos-source-rpms", "10"): [("ostree", "2025.7", "1.el10")],
        ("rhel-10-for-x86_64-baseos-source-rpms", "10.2"): [("kernel", "6.12.0", "211.73.1.el10_2")],
        ("rhocp-5.0-for-rhel-10-x86_64-source-rpms", "10"): [
            ("cri-o", "5.0.0", "202610292257.p2.g9186725.assembly.stream.el10")],
        ("rhel-9-for-x86_64-baseos-eus-source-rpms", "9.6"): [("glibc", "2.34", "100.el9_6")],
    }

    def fake_index(repo, rv):
        rid = next(k for k, v in repos_by_url.items() if v is repo)
        calls.append((rid, rv))
        return {nvr: f"https://cdn/{rid}/{rv}/{'-'.join(nvr)}.src.rpm"
                for nvr in catalog.get((rid, rv), [])}

    real_read = cf.read_repos
    repos_by_url = {}

    def read_and_remember(path, pats=None):
        r = real_read(path, pats)
        repos_by_url.update(r)
        return r

    monkeypatch.setattr(cf, "read_repos", read_and_remember)
    monkeypatch.setattr(cf, "index_repo", fake_index)
    monkeypatch.setattr(cf, "download", lambda a: (a[1], None))
    dest = tmp_path / "srpms"
    monkeypatch.setattr(sys, "argv", ["cdn-fetch.py", "--missing", str(tmp_path / "missing.txt"),
                                      "--destdir", str(dest), "--repo-file",
                                      str(tmp_path / "redhat.repo"), *extra])
    rc = cf.main()
    return rc, calls, dest


def test_el10_packages_only_index_rhel10_repos(tmp_path, monkeypatch):
    rc, calls, _ = _run(tmp_path, monkeypatch, [
        "ostree-2025.7-1.el10", "kernel-6.12.0-211.73.1.el10_2",
        "cri-o-5.0.0-202610292257.p2.g9186725.assembly.stream.el10"],
        extra=["--unresolved-out", str(tmp_path / "cdn-unresolved-el10.txt")])
    assert rc == 0
    assert {rid for rid, _ in calls} <= {"rhel-10-for-x86_64-baseos-source-rpms",
                                          "rhocp-5.0-for-rhel-10-x86_64-source-rpms"}
    # plain el10 resolves $releasever to "10", never to a hardcoded "9"
    assert ("rhel-10-for-x86_64-baseos-source-rpms", "10") in calls
    assert ("rhel-10-for-x86_64-baseos-source-rpms", "10.2") in calls
    assert not (tmp_path / "cdn-unresolved-el10.txt").exists()   # everything found


def test_mixed_majors_and_unresolved_out(tmp_path, monkeypatch):
    out = tmp_path / "custom-unresolved.txt"
    rc, calls, _ = _run(tmp_path, monkeypatch, [
        "glibc-2.34-100.el9_6", "ostree-2025.7-1.el10", "gone-1.0-1.el10_0"],
        extra=["--unresolved-out", str(out), "--dry-run"])
    assert rc == 0
    for rid, rv in calls:                      # no cross-major indexing
        assert cf.repo_major(rid) == cf.major_of(rv)
    assert ("rhel-9-for-x86_64-baseos-eus-source-rpms", "9.6") in calls
    # --dry-run returns before writing; run again for real to get the file
    rc, _, _ = _run(tmp_path, monkeypatch, ["gone-1.0-1.el10_0"],
                    extra=["--unresolved-out", str(out)])
    assert rc == 0 and "gone-1.0-1.el10_0" in out.read_text()


def test_default_unresolved_path_is_next_to_destdir(tmp_path, monkeypatch):
    rc, _, dest = _run(tmp_path, monkeypatch, ["gone-1.0-1.el9"])
    assert rc == 0
    assert "gone-1.0-1.el9" in (dest.parent / "cdn-unresolved.txt").read_text()
