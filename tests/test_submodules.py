"""Regression tests for scripts/submodulelib.py (.gitmodules -> archive URL).

Network-free: the resolution is pure, so CI can guard it even though the
collection step itself needs the GitHub trees API and codeload.

The case that motivated all of this is ZTWIM: openshift/zero-trust-workload-
identity-manager-release is a release repo whose entire content is
Containerfiles plus five submodules, so a codeload archive of it carries no
operator code at all -- no CRDs, no controllers.
"""
import importlib.util
import pathlib

_SPEC = importlib.util.spec_from_file_location(
    "submodulelib",
    pathlib.Path(__file__).resolve().parent.parent / "scripts" / "submodulelib.py")
sml = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(sml)


ZTWIM_GITMODULES = '''\
[submodule "spiffe-spire"]
\tpath = spiffe-spire
\turl = https://github.com/openshift/spiffe-spire.git
\tbranch = release/v1.14.7
[submodule "spiffe-spire-controller-manager"]
\tpath = spiffe-spire-controller-manager
\turl = https://github.com/openshift/spiffe-spire-controller-manager.git
\tbranch = release/v0.6.4
[submodule "spiffe-spiffe-csi"]
\tpath = spiffe-spiffe-csi
\turl = https://github.com/openshift/spiffe-spiffe-csi.git
\tbranch = release/v0.2.8
[submodule "zero-trust-workload-identity-manager"]
\tpath = zero-trust-workload-identity-manager
\turl = https://github.com/openshift/zero-trust-workload-identity-manager.git
\tbranch = release-1.1
[submodule "spiffe-spiffe-helper"]
\tpath = spiffe-spiffe-helper
\turl = https://github.com/openshift/spiffe-spiffe-helper.git
\tbranch = release/0.11.x
'''

# real mode-160000 entries for 612309a974bd (the 4.22 ZTWIM release commit)
ZTWIM_TREE = {"tree": [
    {"path": ".gitmodules", "mode": "100644", "type": "blob", "sha": "3822abe"},
    {"path": "spiffe-spiffe-csi", "mode": "160000", "type": "commit",
     "sha": "ef251025bc11a9e2e60090c980a0381ce5526687"},
    {"path": "spiffe-spiffe-helper", "mode": "160000", "type": "commit",
     "sha": "1d0551d63787b528926b3e17fac949a376040bec"},
    {"path": "spiffe-spire", "mode": "160000", "type": "commit",
     "sha": "154f636ea73099005f90e6c29eb47f78e2f0d349"},
    {"path": "spiffe-spire-controller-manager", "mode": "160000", "type": "commit",
     "sha": "0103fd531f9679de9d7d3a827202aa57c30a6088"},
    {"path": "zero-trust-workload-identity-manager", "mode": "160000",
     "type": "commit", "sha": "d4b265f257442ac84ed43ac2be542dcaa6b34fd0"},
    {"path": "hack", "mode": "040000", "type": "tree", "sha": "0ce916a"},
]}


# --------------------------------------------------------- .gitmodules parsing
def test_parse_ztwim():
    mods = sml.parse_gitmodules(ZTWIM_GITMODULES)
    assert [m.path for m in mods] == [
        "spiffe-spire", "spiffe-spire-controller-manager", "spiffe-spiffe-csi",
        "zero-trust-workload-identity-manager", "spiffe-spiffe-helper"]
    assert mods[0].url == "https://github.com/openshift/spiffe-spire.git"
    assert mods[0].branch == "release/v1.14.7"


def test_parse_tolerates_junk():
    """A stanza missing path/url is dropped, not raised: one malformed
    .gitmodules must never cost the casket every other submodule."""
    txt = ('[submodule "a"]\n\turl = https://github.com/o/a.git\n'   # no path
           '[submodule "b"]\n\tpath = b\n'                           # no url
           '[submodule "c"]\n\tpath = c\n\turl = https://github.com/o/c\n')
    assert [m.path for m in sml.parse_gitmodules(txt)] == ["c"]


def test_parse_stops_at_other_section():
    txt = ('[submodule "a"]\n\tpath = a\n\turl = https://github.com/o/a\n'
           '[core]\n\tpath = nope\n\turl = https://github.com/o/nope\n')
    assert [m.path for m in sml.parse_gitmodules(txt)] == ["a"]


def test_parse_ignores_comments_and_quotes():
    txt = ('# comment\n[submodule "a"]\n\tpath = "a/b"\n'
           '\tURL = https://github.com/o/a\n')
    mods = sml.parse_gitmodules(txt)
    assert (mods[0].path, mods[0].url) == ("a/b", "https://github.com/o/a")


# ------------------------------------------------------------------ URL -> slug
def test_slug_forms():
    # the three forms that actually occur across the fleet
    assert sml.github_slug("https://github.com/openshift/spiffe-spire.git") \
        == "openshift/spiffe-spire"
    assert sml.github_slug("https://github.com/openshift/spiffe-spire") \
        == "openshift/spiffe-spire"
    assert sml.github_slug("git@github.com:openshift/spiffe-spire.git") \
        == "openshift/spiffe-spire"


def test_slug_rejects_non_github():
    # 9 gitlab refs in the fleet; they must be reported, never fetched blindly
    assert sml.github_slug(
        "https://gitlab.com/nvidia/container-infrastructure/aws-kube-ci.git") == ""
    assert sml.github_slug("") == ""
    assert sml.github_slug("https://github.com/owneronly") == ""


def test_slug_relative_needs_parent():
    assert sml.github_slug("../peer.git", "openshift/parent") == "openshift/peer"
    assert sml.github_slug("../peer.git") == ""
    # git resolves relative URLs against the superproject's remote, so two
    # levels up legitimately crosses to another owner
    assert sml.github_slug("../../other/peer.git", "openshift/parent") \
        == "other/peer"
    # but it must never climb past the host into a bare or negative path
    assert sml.github_slug("../../../../escape.git", "openshift/parent") == ""


def test_slug_ssh_scheme_and_subgroup_trim():
    assert sml.github_slug("ssh://git@github.com/openshift/spiffe-spire.git") \
        == "openshift/spiffe-spire"


# --------------------------------------------------------------- gitlink pinning
def test_gitlinks_only_takes_mode_160000():
    links = sml.gitlinks_from_tree(ZTWIM_TREE)
    assert len(links) == 5
    assert ".gitmodules" not in links and "hack" not in links
    assert links["spiffe-spire"] == "154f636ea73099005f90e6c29eb47f78e2f0d349"


def test_gitlinks_empty_input():
    assert sml.gitlinks_from_tree({}) == {}
    assert sml.gitlinks_from_tree({"tree": None}) == {}


# ------------------------------------------------------------------- planning
def test_plan_exact_when_tree_available():
    mods = sml.parse_gitmodules(ZTWIM_GITMODULES)
    pins, skipped = sml.plan_submodules(
        mods, sml.gitlinks_from_tree(ZTWIM_TREE),
        "openshift/zero-trust-workload-identity-manager-release")
    assert skipped == []
    assert len(pins) == 5
    assert all(p.exact for p in pins)
    ztwim = [p for p in pins if p.path == "zero-trust-workload-identity-manager"][0]
    assert ztwim.url == ("https://github.com/openshift/"
                         "zero-trust-workload-identity-manager/archive/"
                         "d4b265f257442ac84ed43ac2be542dcaa6b34fd0.tar.gz")


def test_plan_falls_back_to_branch_and_marks_approx():
    """No trees API (offline / rate-limited) is the normal degraded case: fetch
    the branch head, but never claim it is the commit that was built."""
    mods = sml.parse_gitmodules(ZTWIM_GITMODULES)
    pins, skipped = sml.plan_submodules(mods, {})
    assert skipped == []
    assert not any(p.exact for p in pins)
    spire = [p for p in pins if p.path == "spiffe-spire"][0]
    assert spire.ref == "release/v1.14.7"


def test_plan_reports_unfetchable_instead_of_dropping():
    txt = ('[submodule "gl"]\n\tpath = gl\n'
           '\turl = https://gitlab.com/nvidia/container-infrastructure/aws-kube-ci.git\n'
           '[submodule "nopin"]\n\tpath = nopin\n\turl = https://github.com/o/r\n')
    pins, skipped = sml.plan_submodules(sml.parse_gitmodules(txt), {})
    assert pins == []
    assert sorted(s[2] for s in skipped) == ["no-pinned-commit", "not-github"]


def test_plan_prefers_gitlink_over_branch():
    txt = ('[submodule "a"]\n\tpath = a\n\turl = https://github.com/o/a\n'
           '\tbranch = main\n')
    pins, _ = sml.plan_submodules(sml.parse_gitmodules(txt), {"a": "deadbeef" * 5})
    assert pins[0].exact and pins[0].ref == "deadbeef" * 5


# ------------------------------------------------------- value-object equality
def test_submodule_eq_same_fields():
    a = sml.Submodule("n", "p", "https://github.com/o/r.git", "main")
    b = sml.Submodule("n", "p", "https://github.com/o/r.git", "main")
    assert a == b


def test_submodule_eq_differs_on_branch():
    a = sml.Submodule("n", "p", "u", "main")
    b = sml.Submodule("n", "p", "u", "release-4.20")
    assert a != b


def test_submodule_eq_foreign_type():
    """__eq__ must return NotEqual, not raise, against a non-Submodule."""
    assert sml.Submodule("n", "p", "u") != ("n", "p", "u", "")
    assert sml.Submodule("n", "p", "u") != object()


def test_pin_eq_same_fields():
    a = sml.Pin("p", "o/r", "a" * 40, True, "https://github.com/o/r")
    b = sml.Pin("p", "o/r", "a" * 40, True, "https://github.com/o/r")
    assert a == b


def test_pin_eq_differs_on_exact():
    a = sml.Pin("p", "o/r", "main", False, "u")
    b = sml.Pin("p", "o/r", "main", True, "u")
    assert a != b


def test_pin_eq_foreign_type():
    assert sml.Pin("p", "o/r", "ref", True, "u") != "p"


# ------------------------------------------------------- github_slug rejects
def test_github_slug_rejects_unparseable_url():
    """Neither scp-style nor scheme://host/path — no slug to be had."""
    assert sml.github_slug("just-a-string") == ""
    assert sml.github_slug("github.com/openshift/cvo") == ""
    assert sml.github_slug("https://github.com") == ""


def test_github_slug_rejects_non_github_host():
    assert sml.github_slug("https://gitlab.com/org/repo.git") == ""


def test_github_slug_scp_style():
    assert sml.github_slug("git@github.com:openshift/cvo.git") == "openshift/cvo"


# ------------------------------------------------- parse_gitmodules key lines
def test_parse_gitmodules_ignores_unknown_keys():
    text = ('[submodule "x"]\n'
            '\tpath = x\n'
            '\turl = https://github.com/o/x.git\n'
            '\tshallow = true\n'
            '\tupdate = none\n')
    subs = sml.parse_gitmodules(text)
    assert subs == [sml.Submodule("x", "x", "https://github.com/o/x.git", "")]


def test_parse_gitmodules_ignores_non_key_lines():
    """Continuation junk inside a stanza is neither a section nor a key."""
    text = ('[submodule "x"]\n'
            '\tpath = x\n'
            '\t= orphaned value\n'
            '\turl = https://github.com/o/x.git\n')
    subs = sml.parse_gitmodules(text)
    assert [s.path for s in subs] == ["x"]
