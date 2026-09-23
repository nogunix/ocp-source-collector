"""Tests for scripts/export-upstream-sources.py — candidate lists for the
B / B-operand rows, and cutting them at the candidate the pipeline won with."""
import importlib.util
import pathlib

_SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "export-upstream-sources.py"
_spec = importlib.util.spec_from_file_location("export_upstream_sources", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

REPO = "https://github.com/openshift/kube-rbac-proxy"
SHA = "095fa67c257b0380184266438efbc2218c4e1761"


class TestPipelineMinor:
    def test_phase_b_catalogs(self):
        assert _mod.pipeline_minor("sources-ocp4.18-operators") == "4.18"
        assert _mod.pipeline_minor("sources-ocp4.18-certified-operators") == "4.18"
        assert _mod.pipeline_minor("sources-ocp4.18-community-operators") == "4.18"

    def test_layered(self):
        assert _mod.pipeline_minor("sources-layered-ocp4.16/acm") == "4.16"

    def test_phase_a_keeps_legacy_chain(self):
        assert _mod.pipeline_minor("sources-ocp4.20.22") is None


class TestUrlToRef:
    def test_sha_and_refs(self):
        assert _mod.url_to_ref(f"{REPO}/archive/{SHA}.tar.gz", REPO) == SHA
        assert _mod.url_to_ref(f"{REPO}/archive/refs/heads/release-4.20.tar.gz", REPO) \
            == "refs/heads/release-4.20"

    def test_other_repo_is_dropped(self):
        assert _mod.url_to_ref("https://github.com/x/y/archive/main.tar.gz", REPO) is None


class TestArchiveSuffix:
    def test_tag_drops_v_before_digit(self):
        assert _mod.archive_suffix("refs/tags/v1.2.3") == "1.2.3"
        assert _mod.archive_suffix("refs/tags/version-1") == "version-1"

    def test_branch_slashes(self):
        assert _mod.archive_suffix("refs/heads/release/4.20") == "release-4.20"


class TestCutAtWinner:
    CANDS = (SHA, "refs/tags/v4.20.0", "refs/tags/4.20.0",
             "refs/heads/release-4.20", "refs/heads/main", "refs/heads/master")

    def test_preexisting_branch(self):
        got = _mod.cut_at_winner(list(self.CANDS), "pre-existing:kube-rbac-proxy-release-4.20", REPO)
        assert got[-1] == "refs/heads/release-4.20"

    def test_preexisting_sha(self):
        got = _mod.cut_at_winner(list(self.CANDS), f"pre-existing:kube-rbac-proxy-{SHA}", REPO)
        assert got == [SHA]

    def test_preexisting_tag_keeps_both_v_and_bare(self):
        # v4.20.0 and 4.20.0 unpack to the same top dir; either may have won
        got = _mod.cut_at_winner(list(self.CANDS), "pre-existing:kube-rbac-proxy-4.20.0", REPO)
        assert got[-2:] == ["refs/tags/v4.20.0", "refs/tags/4.20.0"]

    def test_renamed_repo_matches_suffix(self):
        got = _mod.cut_at_winner(list(self.CANDS), "pre-existing:new-name-release-4.20", REPO)
        assert got[-1] == "refs/heads/release-4.20"

    def test_real_url(self):
        got = _mod.cut_at_winner(list(self.CANDS), f"{REPO}/archive/refs/heads/main.tar.gz", REPO)
        assert got[-1] == "refs/heads/main"

    def test_unknown_keeps_full_list(self):
        assert _mod.cut_at_winner(list(self.CANDS), "pre-existing:unknown", REPO) == list(self.CANDS)


class TestPipelineCandidates:
    def test_calls_lib_resolve_and_keeps_empty_fields_aligned(self):
        got = _mod.pipeline_candidates([
            (REPO, SHA, "4.20.0", "4.16"),
            (REPO, "", "", "4.18"),          # empty ref AND version
            (REPO, SHA, "", "4.19"),
        ])
        assert len(got) == 3
        assert got[0][0] == f"{REPO}/archive/{SHA}.tar.gz"
        assert f"{REPO}/archive/refs/heads/release-4.16.tar.gz" in got[0]
        # no ref / no version: only the minor branch and default branches
        assert got[1] == [f"{REPO}/archive/refs/heads/release-4.18.tar.gz",
                          f"{REPO}/archive/refs/heads/main.tar.gz",
                          f"{REPO}/archive/refs/heads/master.tar.gz"]
        assert got[2][0] == f"{REPO}/archive/{SHA}.tar.gz"
        assert f"{REPO}/archive/refs/heads/release-4.19.tar.gz" in got[2]


class TestCollect:
    def _casket(self, root, label, index_rows, fetched=None):
        base = root / label
        (base / "git").mkdir(parents=True)
        (base / "git" / "INDEX.tsv").write_text(
            "dir\trepo\tref\tversion\tcomponents\n"
            + "".join("\t".join(r) + "\n" for r in index_rows))
        if fetched is not None:
            (base / "meta").mkdir()
            (base / "meta" / "git-fetched.tsv").write_text(
                "# tarball\turl\tkind\texact\n"
                + "".join("\t".join(r) + "\n" for r in fetched))
        return str(base / "git" / "INDEX.tsv")

    def test_rows_per_phase(self, tmp_path, monkeypatch):
        monkeypatch.setattr(_mod, "SRV", str(tmp_path))
        a = self._casket(tmp_path, "sources-ocp4.20.22",
                         [("krp-abc", REPO, SHA, "4.20.0", "x")])
        lay = self._casket(tmp_path, "sources-layered-ocp4.16/acm",
                           [("krp-095f", REPO, SHA, "4.20.0", "x")],
                           fetched=[("krp-095f.tar.gz",
                                     "pre-existing:kube-rbac-proxy-release-4.20",
                                     "preexisting", "0")])
        rows = _mod.collect([a, lay])
        by_label = {r[4]: r for r in rows}
        assert by_label["sources-ocp4.20.22"][5] == ""          # legacy chain
        assert by_label["sources-layered-ocp4.16/acm"][5].split()[-1] \
            == "refs/heads/release-4.20"                         # cut at winner
