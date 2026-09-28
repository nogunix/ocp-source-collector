"""Tests for opengrok/scripts/promote-submodules.py — lifting a wrapper repo's
submodules (ZTWIM's operator) to the product level of a staged OpenGrok
project, without indexing anything twice."""
import importlib.util
import os
import pathlib

_SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "opengrok" / "scripts" / "promote-submodules.py"
_spec = importlib.util.spec_from_file_location("promote_submodules", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

WRAPPER = "https://github.com/openshift/zero-trust-workload-identity-manager-release"
SUBS = ["zero-trust-workload-identity-manager", "spiffe-spire"]


def _tree(git, name, subs=(), files=("Makefile", "Containerfile.x"), fill=True):
    d = git / name
    d.mkdir(parents=True)
    for f in files:
        (d / f).write_text("x")
    for s in subs:
        (d / s).mkdir()
        if fill:
            (d / s / "go.mod").write_text("module x")
    return d


def _casket(tmp_path, pin_a="release-1.1", pin_b="release-1.1", fill_b=True):
    """4.20's shape: two wrapper builds (spire's and the operator's) + a sidecar."""
    casket = tmp_path / "casket"
    git = casket / "git"
    _tree(git, "spiffe-csi-driver-c73ed720cd1e", SUBS)
    _tree(git, "zero-trust-workload-identity-manager-612309a974bd", SUBS, fill=fill_b)
    _tree(git, "ose-csi-node-driver-registrar-dcd6a563975e", files=("main.go",))
    (git / "INDEX.tsv").write_text(
        "dir\trepo\tref\tversion\tcomponents\n"
        f"spiffe-csi-driver-c73ed720cd1e\t{WRAPPER}\tc73ed\t\tx\n"
        f"zero-trust-workload-identity-manager-612309a974bd\t{WRAPPER}.git\t6123\t\tx\n"
        "ose-csi-node-driver-registrar-dcd6a563975e\thttps://github.com/openshift/csi-node-driver-registrar\tdcd6\t\tx\n")
    (casket / "meta").mkdir()
    lines = ["# component\tpath\trepo\tref\texact\tstatus"]
    for tree, pin in (("spiffe-csi-driver-c73ed720cd1e", pin_a),
                      ("zero-trust-workload-identity-manager-612309a974bd", pin_b)):
        lines.append(f"{tree}\tzero-trust-workload-identity-manager\topenshift/zero-trust-workload-identity-manager\t{pin}\t1\tok")
        lines.append(f"{tree}\tspiffe-spire\topenshift/spiffe-spire\trelease/v1.14.7\t1\tok")
        lines.append(f"{tree}\tspiffe-spire/nested\topenshift/nested\tabc\t1\tok")  # depth 2: ignored
    (casket / "meta" / "SUBMODULES.tsv").write_text("\n".join(lines) + "\n")

    proj = tmp_path / "stage" / "openshift-zero-trust-workload-identity-manager"
    proj.mkdir(parents=True)
    for d in sorted(os.listdir(git)):
        if (git / d).is_dir():
            os.symlink(git / d, proj / d.rsplit("-", 1)[0])
    lst = tmp_path / "list.txt"
    lst.write_text("# comment\nopenshift/zero-trust-workload-identity-manager-release  # ztwim\n\n")
    return casket, proj, lst


def _target(p):
    return os.readlink(p) if os.path.islink(p) else None


def test_same_pins_promote_once_and_strip_wrappers(tmp_path):
    casket, proj, lst = _casket(tmp_path)
    log = _mod.promote(str(casket), str(proj), _mod.read_list(str(lst)))
    names = sorted(os.listdir(proj))
    assert names == ["ose-csi-node-driver-registrar", "spiffe-spire",
                     "zero-trust-workload-identity-manager",
                     "zero-trust-workload-identity-manager-release",
                     "zero-trust-workload-identity-manager-release-612309a974bd"]
    # the operator is promoted once, from the operator's own wrapper build
    assert _target(proj / "zero-trust-workload-identity-manager").endswith(
        "zero-trust-workload-identity-manager-612309a974bd/zero-trust-workload-identity-manager")
    # wrappers are real dirs without their submodule dirs -> nothing indexed twice
    for w in ("zero-trust-workload-identity-manager-release",
              "zero-trust-workload-identity-manager-release-612309a974bd"):
        assert not os.path.islink(proj / w)
        assert sorted(os.listdir(proj / w)) == ["Containerfile.x", "Makefile"]
    # untouched sidecar
    assert _target(proj / "ose-csi-node-driver-registrar").endswith("ose-csi-node-driver-registrar-dcd6a563975e")
    assert len([l for l in log if l.startswith("promoted")]) == 2


def test_different_pins_keep_both_and_operator_build_wins_the_plain_name(tmp_path):
    casket, proj, lst = _casket(tmp_path, pin_a="aaaa111", pin_b="bbbb222")
    _mod.promote(str(casket), str(proj), _mod.read_list(str(lst)))
    assert _target(proj / "zero-trust-workload-identity-manager").endswith(
        "zero-trust-workload-identity-manager-612309a974bd/zero-trust-workload-identity-manager")
    assert _target(proj / "zero-trust-workload-identity-manager-aaaa111").endswith(
        "spiffe-csi-driver-c73ed720cd1e/zero-trust-workload-identity-manager")


def test_empty_submodule_dir_is_not_promoted(tmp_path):
    casket, proj, lst = _casket(tmp_path, pin_a="aaaa111", pin_b="bbbb222", fill_b=False)
    _mod.promote(str(casket), str(proj), _mod.read_list(str(lst)))
    # only spire's wrapper has a filled copy, so it takes the plain name
    assert _target(proj / "zero-trust-workload-identity-manager").endswith(
        "spiffe-csi-driver-c73ed720cd1e/zero-trust-workload-identity-manager")
    assert not os.path.exists(proj / "zero-trust-workload-identity-manager-bbbb222")


def test_unlisted_repo_is_left_alone(tmp_path):
    casket, proj, _ = _casket(tmp_path)
    before = {n: _target(proj / n) for n in os.listdir(proj)}
    assert _mod.promote(str(casket), str(proj), {"openshift/cert-manager-operator-release"}) == []
    assert {n: _target(proj / n) for n in os.listdir(proj)} == before


def test_ref_with_slash_is_made_path_safe(tmp_path):
    assert _mod.safe("release/v1.14.7") == "release-v1.14.7"
    assert _mod.slug("https://github.com/O/R.git/") == "o/r"


def test_main(tmp_path, capsys):
    casket, proj, lst = _casket(tmp_path)
    assert _mod.main(["p", str(casket), str(proj), str(lst)]) == 0
    assert "promoted" in capsys.readouterr().err
    # missing list file or project dir: silently nothing to do
    assert _mod.main(["p", str(casket), str(proj), str(tmp_path / "nope")]) == 0
    assert _mod.main(["p", str(casket), str(tmp_path / "nope"), str(lst)]) == 0
    assert _mod.main(["p"]) == 2


def test_component_list_decides_when_the_tree_is_named_after_another_image(tmp_path):
    """cert-manager 4.20: the operator image's build is jetstack-cert-manager-<sha>
    (the tarball took the first component's name); trust-manager's wrapper pins
    an older operator branch and must not take the plain name."""
    casket = tmp_path / "casket"
    git = casket / "git"
    _tree(git, "cert-manager-trust-manager-c325754f067a", ["cert-manager-operator"])
    _tree(git, "jetstack-cert-manager-fbb480da9759", ["cert-manager-operator"])
    w = "https://github.com/openshift/cert-manager-operator-release"
    (git / "INDEX.tsv").write_text(
        "dir\trepo\tref\tversion\tcomponents\n"
        f"cert-manager-trust-manager-c325754f067a\t{w}\tc325\t\tcert-manager-trust-manager\n"
        f"jetstack-cert-manager-fbb480da9759\t{w}\tfbb4\t\tcert-manager-istio-csr,cert-manager-operator,jetstack-cert-manager\n")
    (casket / "meta").mkdir()
    (casket / "meta" / "SUBMODULES.tsv").write_text(
        "cert-manager-trust-manager-c325754f067a\tcert-manager-operator\topenshift/cert-manager-operator\tcert-manager-1.19\t0\tok\n"
        "jetstack-cert-manager-fbb480da9759\tcert-manager-operator\topenshift/cert-manager-operator\tcert-manager-1.20\t0\tok\n")
    proj = tmp_path / "stage" / "openshift-cert-manager-operator"
    proj.mkdir(parents=True)
    for d in ("cert-manager-trust-manager-c325754f067a", "jetstack-cert-manager-fbb480da9759"):
        os.symlink(git / d, proj / d.rsplit("-", 1)[0])
    _mod.promote(str(casket), str(proj), {"openshift/cert-manager-operator-release"})
    assert _target(proj / "cert-manager-operator").endswith(
        "jetstack-cert-manager-fbb480da9759/cert-manager-operator")
    assert _target(proj / "cert-manager-operator-cert-manager-1.19").endswith(
        "cert-manager-trust-manager-c325754f067a/cert-manager-operator")
