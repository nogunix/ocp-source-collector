"""Tests for mcp/insight.py — the release-aware casket-mcp tools
(source_for_image, find_dependency_users, release_diff, rpm_source,
check_patch_shipped). A fake /srv with every casket layout is built in
tmp_path; no mount, network or GitHub needed (ripgrep is, as in CI)."""
import os
import subprocess

import pytest

import backends as be
import insight as ins

D_A = "sha256:" + "a" * 64          # payload image, 4.20.28 and 4.20.35
D_B = "sha256:" + "b" * 64          # operator image
D_BUNDLE = "sha256:" + "c" * 64     # operator bundle
D_CNV = "sha256:" + "d" * 64        # layered operand
D_OLD = "sha256:" + "e" * 64        # inventoried in image-rpms, no longer shipped
MCO_OLD, MCO_NEW = "1" * 40, "2" * 40
REPO = "https://github.com/openshift/machine-config-operator"

SYNC_OLD = """package operator

const (
\tmccEventsRoleBindingTargetManifestPath = "events-rolebinding-target.yaml"
\tmccClusterRoleBindingManifestPath = "clusterrolebinding.yaml"
)

func syncAll() error {

\tco, err := fetch()
\treturn err
}
"""
SYNC_NEW = """package operator

const (
\tmccEventsRoleBindingTargetManifestPath = "events-rolebinding-target.yaml"
\tmccConfigMapsRoleTargetManifestPath = "configmaps-role-target.yaml"
\tmccClusterRoleBindingManifestPath = "clusterrolebinding.yaml"
)

func syncAll() error {
\tco, err := fetch()
\treturn err
}
"""
# a later commit moved the context: the fix is in, the hunk no longer applies verbatim
SYNC_DRIFTED = SYNC_NEW.replace('\tmccClusterRoleBindingManifestPath', '\tmccSomethingNew = "x"\n'
                                '\tmccClusterRoleBindingManifestPath')

PATCH = """diff --git a/pkg/operator/sync.go b/pkg/operator/sync.go
index 111..222 100644
--- a/pkg/operator/sync.go
+++ b/pkg/operator/sync.go
@@ -3,5 +3,6 @@ const (
 const (
 \tmccEventsRoleBindingTargetManifestPath = "events-rolebinding-target.yaml"
+\tmccConfigMapsRoleTargetManifestPath = "configmaps-role-target.yaml"
 \tmccClusterRoleBindingManifestPath = "clusterrolebinding.yaml"
 )
@@ -8,6 +9,5 @@ const (
 func syncAll() error {
-
 \tco, err := fetch()
 \treturn err
 }
diff --git a/manifests/role.yaml b/manifests/role.yaml
new file mode 100644
--- /dev/null
+++ b/manifests/role.yaml
@@ -0,0 +1,2 @@
+kind: Role
+name: machine-config-controller-configmaps
\\ No newline at end of file
diff --git a/manifests/old.yaml b/manifests/old.yaml
deleted file mode 100644
--- a/manifests/old.yaml
+++ /dev/null
@@ -1 +0,0 @@
-kind: OldThing
"""


def w(path, text=""):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


def tsv(path, rows, header=None):
    w(path, (header + "\n" if header else "") + "".join("\t".join(r) + "\n" for r in rows))


@pytest.fixture
def srv(tmp_path, monkeypatch):
    s = tmp_path / "srv"
    monkeypatch.setattr(be, "SRV", str(s))
    ins._GOMOD_CACHE.clear()

    # ---- Phase A: two patches of 4.20
    for ver, sha, sync, extra in (("4.20.28", MCO_OLD, SYNC_OLD, True),
                                  ("4.20.35", MCO_NEW, SYNC_DRIFTED, False)):
        m = s / f"sources-ocp{ver}"
        tree = f"machine-config-operator-{sha[:12]}"
        tsv(m / "meta/images.tsv", [("machine-config-operator", f"quay.io/ocp-art@{D_A}", D_A),
                                    ("cli", f"quay.io/ocp-art@sha256:{'f' * 64}", "")])
        tsv(m / "meta/commits.tsv",
            [("machine-config-operator", REPO, sha), ("cli", "https://github.com/openshift/oc", "3" * 40)]
            + ([("gone", "https://github.com/openshift/gone", "4" * 40)] if extra else
               [("fresh", "https://github.com/openshift/fresh", "5" * 40)]))
        tsv(m / "git/INDEX.tsv", [(tree, REPO, sha, "", "machine-config-operator")],
            header="dir\trepo\tref\tversion\tcomponents")
        w(m / "git" / tree / "pkg/operator/sync.go", sync)
        if extra:
            w(m / "git" / tree / "manifests/old.yaml", "kind: OldThing\n")
        else:
            w(m / "git" / tree / "manifests/role.yaml",
              "kind: Role\nname: machine-config-controller-configmaps\n")
        w(m / "git" / tree / "go.mod",
          "module x\n\ngo 1.22\n\nrequire (\n\tgolang.org/x/net v0.30.0 // indirect\n)\n"
          "require golang.org/x/text v0.1.0\n"
          "replace golang.org/x/crypto => golang.org/x/crypto v0.9.0\n"
          "replace (\n\tgolang.org/x/sys => golang.org/x/sys v0.2.0\n\tfoo => ../local\n)\n")
        os.makedirs(m / "deps/go/golang.org/x/net@v0.30.0")
        tsv(m / "meta/DEPS.tsv", [
            (tree, "go.sum", "go", "golang.org/x/net", "v0.30.0", "go/golang.org/x/net@v0.30.0", "ok"),
            (tree, "go.sum", "go", "golang.org/x/net", "v0.20.0", "", "failed:HTTPError"),
            (tree, "go.sum", "go", "golang.org/x/net-extra", "v0.1.0", "", "ok"),
            (tree, "tools/go.sum", "go", "golang.org/x/net", "v0.10.0", "", "ok"),   # no tools/go.mod
            (tree, "Cargo.lock", "crates", "golang.org/x/net", "1.0.0", "", "ok"),
            ("short", "row"),
        ], header="# component\tmanifest\tecosystem\tname\tversion\tdir\tstatus")

    # ---- Phase B: operators catalog
    b = s / "sources-ocp4.20-operators"
    tsv(b / "meta/containers.tsv", [("op-a", "op-a.v1", f"registry.redhat.io/x/op-a@{D_B}", "1.0"),
                                    ("op-nosrc", "op-nosrc.v1", "registry.redhat.io/x/nosrc@sha256:" + "9" * 64, "1")])
    tsv(b / "meta/bundles.tsv", [("op-a", "stable", "op-a.v1", f"registry.redhat.io/x/op-a-bundle@{D_BUNDLE}")])
    tsv(b / "meta/git.tsv", [("op-a", "https://github.com/o/op-a", "6" * 40, "op-a-666666666666.tar.gz", "1.0")])
    tsv(b / "meta/git-fetched.tsv", [("op-a-666666666666.tar.gz", "https://github.com/o/op-a/archive/refs/tags/v1.0.tar.gz", "tag", "0")],
        header="# tarball\turl\tkind\texact")
    tsv(b / "git/INDEX.tsv", [("op-a-666666666666", "https://github.com/o/op-a", "6" * 40, "", "op-a")],
        header="dir\trepo\tref\tversion\tcomponents")
    os.makedirs(b / "git/op-a-666666666666")

    # ---- b-operand: layered product with a wrapper submodule
    lay = s / "sources-layered-ocp4.20" / "cnv"
    tsv(lay / "meta/IMAGE_MAP.tsv", [(f"registry.redhat.io/cnv/virt-api@{D_CNV}", "virt-api", "virt-api-777777777777", "cnv")],
        header="# image\tcomponent\tgit_dir\tinfix")
    tsv(lay / "git/INDEX.tsv", [("virt-api-777777777777", "https://gitlab.com/k/kubevirt", "7" * 40, "", "virt-api"),
                                ("wrapper-888888888888", "https://github.com/o/wrapper-release", "8" * 40, "", "wrapper")],
        header="dir\trepo\tref\tversion\tcomponents")
    os.makedirs(lay / "git/virt-api-777777777777")
    w(lay / "git/wrapper-888888888888/mco/pkg/operator/sync.go", SYNC_NEW)
    tsv(lay / "meta/SUBMODULES.tsv", [("wrapper-888888888888", "mco", "openshift/machine-config-operator", MCO_NEW, "1", "ok")],
        header="component\tpath\trepo\tref\texact\tstatus")
    os.makedirs(s / "sources-layered-ocp4.20" / "not-a-product")
    w(s / "sources-layered-ocp4.20" / "stray-file")

    # ---- a-rpm
    r = s / "sources-ocp-srpms"
    for nevr, patched in (("kernel-5.14.0-570.1.el9_6", False), ("cri-o-1.33.1-1.el9", True),
                          ("kernel-5.14.0-600.1.el9_7", False)):
        t = r / "srpms" / nevr
        w(t / "archives" / "fix-a.patch")
        w(t / "archives" / "src.tar.gz")
        w(t / "info" / f"{nevr.rsplit('-', 2)[0]}.spec")
        w(t / "info" / "srpm_query_ARCH")
        if patched:
            os.makedirs(t / "pre-build" / "cri-o-abc")
        else:
            w(t / "_incomplete")
    for ver, links in (("4.20.30", {"kernel": "kernel-5.14.0-570.1.el9_6"}),
                       ("4.20.38", {"kernel": "kernel-5.14.0-600.1.el9_7", "cri-o": "cri-o-1.33.1-1.el9"}),
                       ("4.20.38-extensions", {"ext-only": "cri-o-1.33.1-1.el9"})):
        d = r / "by-ocp" / ver
        os.makedirs(d)
        for name, nevr in links.items():
            os.symlink(f"../../srpms/{nevr}", d / name)
    w(r / "by-ocp/4.20.38/plain-dir/.keep")
    tsv(r / "meta/image-rpms.tsv", [(D_A, "quay.io/ocp-art", "acl-2.3.1-4.el9"),
                                    (D_A, "quay.io/ocp-art", "bash-5.1-1.el9"),
                                    (D_OLD, "registry.redhat.io/x/op-a", "zlib-1.2-1.el9"),
                                    (D_OLD, "registry.redhat.io/x/op-a", "acl-2.3.1-4.el9")],
        header="# image_digest\trepo\tsrpm_nevr")
    tsv(r / "meta/bin-to-src.tsv", [("kernel-core-5.14.0-600.1.el9_7.x86_64", "kernel-5.14.0-600.1.el9_7", "4.20.38"),
                                    ("kernel-core-5.14.0-570.1.el9_6.x86_64", "kernel-5.14.0-570.1.el9_6", "4.20.30"),
                                    ("bad",)],
        header="# binary-NEVRA\tsource-NEVR\tocp-version")

    w(s / "sources-junk")                  # not a dir
    return s


# ------------------------------------------------------------------ helpers
def test_slug_and_github_args():
    assert ins._slug("https://github.com/O/R.git/") == "o/r"
    assert ins._slug("git@github.com:o/r.git") == "o/r"
    assert ins._github_args("https://gitlab.com/a/b") is None
    assert ins._github_args("o/r") == {"owner": "o", "repo": "r"}
    assert ins._github_args("o/r", head="abc") == {"owner": "o", "repo": "r", "sha": "abc"}
    g = ins._github_args("https://github.com/o/r", base="a", head="b")
    assert g["compare_url"] == "https://github.com/o/r/compare/a...b"


def test_rg_fixed_edges(tmp_path, monkeypatch):
    assert ins._rg_fixed("x", [str(tmp_path / "nope")]) == []
    f = tmp_path / "f.tsv"
    f.write_text("x\n")
    assert ins._rg_fixed("", [str(f)]) == []

    def boom(*a, **k):
        raise subprocess.TimeoutExpired("rg", 1)
    monkeypatch.setattr(ins.subprocess, "run", boom)
    assert ins._rg_fixed("x", [str(f)]) == []


def test_units_skips_unreadable_mount(tmp_path):
    m = be.Mount(path=str(tmp_path / "missing"), name="x", suffix="x", phase="b-operand", version="4.20")
    assert list(ins._units([m])) == []


def test_version_key_and_constraints():
    assert ins._vkey("v1.2.3") < ins._vkey("v1.10.0")
    assert ins._vkey("v0.0.0-20200101000000-abc") < ins._vkey("v0.0.0")
    assert ins._vkey("1.2.3-rc.1") < ins._vkey("1.2.3-rc.2")
    assert ins._vkey("1.2.3+incompatible") == ins._vkey("1.2.3")
    assert ins._satisfies("0.30.0", "")
    assert ins._satisfies("v0.30.0", "<0.33.0,>=0.30")
    assert not ins._satisfies("0.34.0", "<0.33.0")
    for c, v, ok in (("<=1.0", "1.0", True), (">1.0", "1.0", False), ("==1.0", "1.0", True),
                     ("=1.0", "1.1", False), ("!=1.0", "1.1", True), ("1.0", "1.0", True)):
        assert ins._satisfies(v, c) is ok, (c, v)


# ------------------------------------------------------- source_for_image
def test_image_digest_phase_a_with_rpms(srv):
    r = ins.source_for_image(D_A)
    assert r["digest"] == D_A and r["count"] == 2
    m = r["matches"][0]
    assert (m["phase"], m["version"], m["component"]) == ("a", "4.20.28", "machine-config-operator")
    assert m["repo"] == REPO and m["ref"] == MCO_OLD and m["exact"] is True
    assert m["path"].endswith("machine-config-operator-111111111111")
    assert m["github"] == {"owner": "openshift", "repo": "machine-config-operator", "sha": MCO_OLD}
    assert r["rpms"]["match"] == "exact digest"
    assert r["rpms"]["srpm_nevrs"] == ["acl-2.3.1-4.el9", "bash-5.1-1.el9"]


def test_image_bare_hex_and_version_filter(srv):
    r = ins.source_for_image("a" * 64, "4.20.35")
    assert [m["version"] for m in r["matches"]] == ["4.20.35"]


def test_image_phase_b_tag_standin_and_bundle(srv):
    r = ins.source_for_image(f"registry.redhat.io/x/op-a@{D_B}")
    m = r["matches"][0]
    assert (m["phase"], m["component"], m["exact"]) == ("b", "op-a", False)
    assert m["path"].endswith("op-a-666666666666")
    rb = ins.source_for_image(D_BUNDLE)
    assert "bundle" in rb["matches"][0]["listed_in"]
    # the inventory holds only an older build of this repository
    assert r["rpms"]["match"] == "none for this digest"
    assert r["rpms"]["same_repository_inventoried"] == [{"digest": D_OLD, "srpm_count": 2}]


def test_image_layered_non_github_and_no_rpms(srv):
    r = ins.source_for_image(D_CNV)
    m = r["matches"][0]
    assert (m["phase"], m["product"], m["component"]) == ("b-operand", "cnv", "virt-api")
    assert m["github"] is None and m["exact"] is True
    assert r["rpms"]["match"] == "none"


def test_image_without_source_and_repo_query(srv):
    r = ins.source_for_image("registry.redhat.io/x/nosrc:v1")
    assert r["digest"] is None and "rpms" not in r
    assert r["matches"][0]["repo"] is None and "note" in r["matches"][0]
    # repository-only query collects every release, deduplicated
    r2 = ins.source_for_image("quay.io/ocp-art")
    assert {m["version"] for m in r2["matches"]} == {"4.20.28", "4.20.35"}
    assert len({(m["version"], m["component"]) for m in r2["matches"]}) == r2["count"]


def test_image_phase_a_commit_without_index_row(srv):
    # cli has a commit but no INDEX tree
    r = ins.source_for_image(f"quay.io/ocp-art@sha256:{'f' * 64}")
    m = r["matches"][0]
    assert m["component"] == "cli" and m["path"] is None and m["exact"] is True
    assert m["github"]["sha"] == "3" * 40


def test_image_unknown_and_empty(srv):
    assert ins.source_for_image("sha256:" + "0" * 64)["count"] == 0
    assert "note" in ins.source_for_image("sha256:" + "0" * 64)
    assert "error" in ins.source_for_image("  ")


def test_image_row_filtered_when_digest_only_in_other_column(srv):
    # the digest appears only as images.tsv col 3 of a row whose image differs
    tsv(srv / "sources-ocp4.20.28/meta/images.tsv", [("x", "quay.io/other@sha256:" + "1" * 64, D_BUNDLE)])
    assert all(m["mount"] != "sources-ocp4.20.28" for m in ins.source_for_image(D_BUNDLE)["matches"])


def test_image_git_tsv_missing_falls_back_to_index(srv):
    os.remove(srv / "sources-ocp4.20-operators/meta/git.tsv")
    m = ins.source_for_image(D_B)["matches"][0]
    assert m["path"].endswith("op-a-666666666666")
    tsv(srv / "sources-ocp4.20-operators/meta/git.tsv",
        [("op-a", "https://github.com/o/op-a", "6" * 40, "op-a-other.tar.gz")])
    m = ins.source_for_image(D_B)["matches"][0]
    assert m["path"] is None and m["ref"] == "6" * 40


# -------------------------------------------------- find_dependency_users
def test_dependency_users(srv):
    r = ins.find_dependency_users("golang.org/x/net", "<0.33.0", "4.20", ecosystem="go")
    assert r["count"] == 6            # 3 go rows x 2 patches; crates row filtered
    assert set(r["versions_found"]) == {"v0.10.0", "v0.20.0", "v0.30.0"}
    sel = {(h["manifest"], h["dep_version"]): h["selected"] for h in r["hits"]}
    assert sel[("go.sum", "v0.30.0")] is True
    assert sel[("go.sum", "v0.20.0")] is False
    assert sel[("tools/go.sum", "v0.10.0")] is None
    ok = [h for h in r["hits"] if h["dep_version"] == "v0.30.0"][0]
    assert ok["dep_source"].endswith("golang.org/x/net@v0.30.0")
    assert r["components_per_release"] == {"a 4.20.28": 1, "a 4.20.35": 1}


def test_dependency_users_selected_only_and_limits(srv):
    r = ins.find_dependency_users("golang.org/x/net", selected_only=True, max_results=1)
    assert {h["dep_version"] for h in r["hits"]} <= {"v0.30.0", "v0.10.0", "1.0.0"}
    assert r["truncated"] is True and len(r["hits"]) == 1
    assert "error" in ins.find_dependency_users(" ")


def test_go_mod_parsing(tmp_path):
    ins._GOMOD_CACHE.clear()
    p = tmp_path / "go.mod"
    p.write_text("module m\nrequire (\n\ta v1.0.0\n\ta v2.0.0\n)\n"
                 "replace a => a v1.5.0\nreplace (\n\tb => b v0.1.0\n)\n")
    assert ins._go_mod_requires(str(p)) == {"a": "v1.5.0", "b": "v0.1.0"}
    assert ins._go_mod_requires(str(p)) is ins._go_mod_requires(str(p))   # cached
    assert ins._go_mod_requires(str(tmp_path / "none")) == {}


# ------------------------------------------------------------ release_diff
def test_release_diff(srv):
    r = ins.release_diff("4.20.28", "4.20.35")
    assert r["changed_repos"] == 1
    c = r["changed"][0]
    assert (c["from_commit"], c["to_commit"], c["components"]) == (MCO_OLD, MCO_NEW, ["machine-config-operator"])
    assert c["github"]["base"] == MCO_OLD and c["github"]["head"] == MCO_NEW
    assert r["added_components"][0][0] == "fresh" and r["removed_components"][0][0] == "gone"
    assert r["unchanged_components"] == 1
    assert "a_rpm_versions" in r["rpms"]
    assert "rpms" not in ins.release_diff("4.20.28", "4.20.35", include_rpms=False)


def test_release_diff_rpms_and_missing(srv):
    # give both patches an a-rpm view
    by = srv / "sources-ocp-srpms/by-ocp"
    os.rename(by / "4.20.30", by / "4.20.28")
    os.rename(by / "4.20.38", by / "4.20.35")
    r = ins.release_diff("4.20.28", "4.20.35")
    assert r["rpms"]["changed"] == [("kernel", "kernel-5.14.0-570.1.el9_6", "kernel-5.14.0-600.1.el9_7")]
    assert ("cri-o", "cri-o-1.33.1-1.el9") in r["rpms"]["added"]
    assert r["rpms"]["removed"] == []
    bad = ins.release_diff("4.20.28", "4.20.99")
    assert "4.20.99" in bad["error"] and bad["mounted"] == ["4.20.28", "4.20.35"]


def test_release_diff_without_srpm_mount(srv):
    import shutil
    shutil.rmtree(srv / "sources-ocp-srpms")
    assert ins.release_diff("4.20.28", "4.20.35")["rpms"]["a_rpm_versions"] == []


# -------------------------------------------------------------- rpm_source
def test_rpm_source_minor_and_patch(srv):
    r = ins.rpm_source("cri-o", "4.20")
    assert r["ocp_version"] == "4.20.38" and r["nevr"] == "cri-o-1.33.1-1.el9"
    assert r["patches"] == ["fix-a.patch"] and r["sources"] == ["src.tar.gz"]
    assert r["spec"][0].endswith("info/cri-o.spec")
    assert r["patched_tree"][0].endswith("pre-build/cri-o-abc") and r["incomplete"] is False
    k = ins.rpm_source("kernel", "4.20.30")
    assert k["incomplete"] is True and k["patched_tree"] == []
    assert k["nevr_by_release"] == {"4.20.30": "kernel-5.14.0-570.1.el9_6",
                                    "4.20.38": "kernel-5.14.0-600.1.el9_7"}


def test_rpm_source_binary_extensions_and_errors(srv):
    r = ins.rpm_source("kernel-core", "4.20.38")
    assert r["source_package"] == "kernel" and "bin-to-src" in r["resolved_via"]
    assert r["nevr"] == "kernel-5.14.0-600.1.el9_7"
    assert ins.rpm_source("ext-only", "4.20.38")["view"] == "4.20.38-extensions"
    assert "error" in ins.rpm_source("nope", "4.20.38")
    miss = ins.rpm_source("kernel", "4.99")
    assert "error" in miss and "4.20.38" in miss["mounted"]


def test_rpm_source_without_srpm_mount(srv):
    import shutil
    shutil.rmtree(srv / "sources-ocp-srpms")
    assert ins.rpm_source("kernel", "4.20")["mounted"] == []


# ------------------------------------------------------ check_patch_shipped
def test_parse_patch_git_and_headerless():
    files = ins.parse_patch(PATCH)
    assert [(f["path"], f["new_file"], f["deleted"]) for f in files] == [
        ("pkg/operator/sync.go", False, False),
        ("manifests/role.yaml", True, False),
        ("manifests/old.yaml", False, True)]
    # GitHub MCP get_commit patches: no ---/+++ lines, new file only by @@ -0,0
    bare = "\n".join(ln for ln in PATCH.splitlines()
                     if not ln.startswith(("--- ", "+++ ", "index ", "new file", "deleted file")))
    assert [(f["path"], f["new_file"]) for f in ins.parse_patch(bare)][:2] == [
        ("pkg/operator/sync.go", False), ("manifests/role.yaml", True)]
    # plain `diff -u` of one file, and junk
    plain = "--- a/x.go\t2026\n+++ b/x.go\t2026\n@@ -1 +1 @@\n-old line here\n+new line here\n"
    assert ins.parse_patch(plain)[0]["path"] == "x.go"
    assert ins.parse_patch("hello\nworld\n") == []
    assert ins.parse_patch("diff --git weird\n@@ -1 +1 @@\n+x\n") == []


def test_check_patch_shipped(srv):
    r = ins.check_patch_shipped(PATCH, REPO, "4.20")
    res = {(x["phase"], x["version"], x["product"]): x for x in r["results"]}
    old = res[("a", "4.20.28", None)]
    assert old["result"] == "not_applied"
    assert old["files"] == {"pkg/operator/sync.go": "not_applied",
                            "manifests/role.yaml": "not_applied",
                            "manifests/old.yaml": "not_applied"}
    # context drifted in 4.20.35 but the added line is there -> applied
    new = res[("a", "4.20.35", None)]
    assert new["result"] == "applied", new["files"]
    # found through the wrapper's filled submodule (no role.yaml there)
    sub = res[("b-operand", "4.20", "cnv")]
    assert sub["submodule_of"] == "wrapper-888888888888"
    assert sub["files"]["manifests/role.yaml"] == "not_applied"
    assert sub["result"] == "partial"
    assert r["first_applied_phase_a_patch"] == {"4.20": "4.20.35"}


def test_check_patch_edge_cases(srv):
    assert "error" in ins.check_patch_shipped("not a diff", REPO)
    none = ins.check_patch_shipped(PATCH, "o/unknown")
    assert none["trees_checked"] == 0 and "resolve_repo" in none["note"]
    # modified file missing in the tree; whitespace-only hunk is neutral
    p = ("diff --git a/absent.go b/absent.go\n--- a/absent.go\n+++ b/absent.go\n"
         "@@ -1 +1 @@\n-x = 1\n+y = 2\n"
         "diff --git a/pkg/operator/sync.go b/pkg/operator/sync.go\n"
         "@@ -1,2 +1,2 @@\n-}\n+ }\n")
    r = ins.check_patch_shipped(p, REPO, "4.20.28")
    x = [y for y in r["results"] if y["version"] == "4.20.28"][0]
    assert x["files"] == {"absent.go": "file_missing", "pkg/operator/sync.go": "applied"}
    assert x["result"] == "partial"


def test_hunk_states():
    hay = ["a line one", "b line two"]
    assert ins._hunk_state(hay, {"old": [], "new": [], "add": [" "], "rem": []}) is None
    # removal whose pre-image context is gone but the removed line still present
    h = {"old": ["zzz context", "b line two"], "new": ["zzz context"], "add": [], "rem": ["b line two"]}
    assert ins._hunk_state(hay, h) == "not_applied"
    # removal-only, removed line gone, context drifted -> applied
    h = {"old": ["zzz context", "gone line"], "new": ["zzz context"], "add": [], "rem": ["gone line"]}
    assert ins._hunk_state(hay, h) == "applied"
    # added lines absent, no context match -> not applied
    h = {"old": ["qqq"], "new": ["qqq", "new stuff"], "add": ["new stuff"], "rem": []}
    assert ins._hunk_state(hay, h) == "not_applied"
    assert ins._contains_block(hay, ["", "  "]) is False


def test_version_scoping(srv):
    names = lambda v: sorted(m.name for m in ins._mounts(v))
    assert names("4.20.35") == ["sources-layered-ocp4.20", "sources-ocp4.20-operators",
                                "sources-ocp4.20.35"]
    assert "sources-ocp4.20.28" in names("4.20") and "sources-ocp-srpms" not in names("")
    assert names("4.18") == []


def test_image_duplicate_rows_and_foreign_inventory_repo(srv):
    c = srv / "sources-ocp4.20-operators/meta/containers.tsv"
    c.write_text(c.read_text() + f"op-a\top-a.v1\tregistry.redhat.io/x/op-a@{D_B}\t1.0\n")
    with open(srv / "sources-ocp-srpms/meta/image-rpms.tsv", "a") as f:
        f.write(f"{D_OLD}\tregistry.redhat.io/x/op-a-other\tzz-1-1.el9\n")
    r = ins.source_for_image(D_B)
    assert r["count"] == 1
    assert r["rpms"]["same_repository_inventoried"] == [{"digest": D_OLD, "srpm_count": 2}]


def test_dependency_versions_are_capped_to_the_newest(srv, monkeypatch):
    """x/net shows 41 versions in one minor; only the newest _MAX_VERSIONS are listed."""
    monkeypatch.setattr(ins, "_MAX_VERSIONS", 2)
    r = ins.find_dependency_users("golang.org/x/net", "", "4.20.35", ecosystem="go")
    assert list(r["versions_found"]) == ["v0.20.0", "v0.30.0"]   # v0.10.0 dropped
    assert r["versions_total"] == 3 and r["versions_truncated"] is True
    capped_count = r["count"]
    monkeypatch.setattr(ins, "_MAX_VERSIONS", 30)
    ok = ins.find_dependency_users("golang.org/x/net", "", "4.20.35", ecosystem="go")
    assert ok["versions_total"] == 3 and ok["versions_truncated"] is False
    assert capped_count == ok["count"]       # hits are not affected by the cap


def test_dependency_constraint_excludes(srv):
    r = ins.find_dependency_users("golang.org/x/net", ">=0.25", "4.20.28", ecosystem="go")
    assert r["versions_found"] == {"v0.30.0": 1}


def test_check_patch_mixed_hunks_binary_and_dead_rows(srv):
    # INDEX rows pointing at the same tree twice and at a missing tree
    idx = srv / "sources-ocp4.20.28/git/INDEX.tsv"
    idx.write_text(idx.read_text()
                   + f"machine-config-operator-111111111111\t{REPO}\t{MCO_OLD}\t\tdup\n"
                   + f"missing-dir\t{REPO}\t{MCO_OLD}\t\tx\n")
    p = ("diff --git a/logo.png b/logo.png\nBinary files differ\n"
         "diff --git a/pkg/operator/sync.go b/pkg/operator/sync.go\n"
         "@@ -1,2 +1,3 @@\n package operator\n+import \"fmt\" // added here\n"
         "@@ -9,3 +9,3 @@\n func syncAll() error {\n-\tco, err := fetch()\n+\tco, err := fetchAll()\n")
    r = ins.check_patch_shipped(p, REPO, "4.20.28")
    assert r["files_in_patch"] == ["pkg/operator/sync.go"]
    x = [y for y in r["results"] if y["version"] == "4.20.28"]
    assert len(x) == 1 and x[0]["files"]["pkg/operator/sync.go"] == "not_applied"
    w(srv / "sources-ocp4.20.28/git/machine-config-operator-111111111111/pkg/operator/sync.go",
      SYNC_OLD.replace("package operator", 'package operator\nimport "fmt" // added here'))
    x = [y for y in ins.check_patch_shipped(p, REPO, "4.20.28")["results"] if y["version"] == "4.20.28"]
    assert x[0]["files"]["pkg/operator/sync.go"] == "partial"
