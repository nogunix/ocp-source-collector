#!/usr/bin/env python3
"""Build a reverse source index + symlink trees on a staged casket.

Reads a staged meta/MANIFEST.json (Phase A / C / D formats are auto-detected)
and writes, under the stage dir:

  git/INDEX.tsv          dir <TAB> repo <TAB> ref <TAB> version <TAB> components
  by-component/<comp>    -> ../git/<dir>   (one per component; many may share a dir)
  by-repo/<basename>     -> ../git/<dir>   (alias by upstream repo name)

This defeats the dedup dir-naming gotcha: dirs are named after the first-seen
component for a (repo, ref|tag) key, so e.g. kubevirt/kubevirt core hides under
"passt-network-binding-plugin-cni-v1.4.1". The index/symlinks make every
component and every repo discoverable regardless of the physical dir name.

Usage: build-source-index.py <stage_dir>
       (expects <stage_dir>/meta/MANIFEST.json and <stage_dir>/git/)
"""
import json, os, sys


def _safe(name: str) -> str:
    return name.replace("/", "_").replace(" ", "_").strip()


def _repo_base(url: str) -> str:
    if not url:
        return ""
    u = url.rstrip("/")
    # only treat github-style URLs as having a meaningful repo basename
    if "github.com" not in u:
        return ""
    return _safe(u.split("/")[-1].removesuffix(".git"))


def records(manifest: dict):
    """Yield (component, repo, ref, version, tarball) across A/C/D formats."""
    if "images" in manifest:  # Phase A
        for it in manifest["images"]:
            src = it.get("source") or {}
            if src.get("status") not in (None, "OK"):
                continue
            tb = src.get("tarball") or ""
            if tb and tb != "NO_SOURCE":
                yield (it.get("name", ""), src.get("repo", ""),
                       src.get("commit", ""), "", tb)
    for g in manifest.get("git", []):  # Phase B / B-operand
        tb = g.get("tarball") or ""
        if not tb or tb == "NO_SOURCE":
            continue
        comp = g.get("component") or g.get("operator") or ""
        yield (comp, g.get("source_url", ""), g.get("vcs_ref", ""),
               g.get("version", ""), tb)


def main() -> int:
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    stage = sys.argv[1]
    mf = os.path.join(stage, "meta", "MANIFEST.json")
    gitdir = os.path.join(stage, "git")
    if not os.path.isfile(mf):
        sys.exit(f"no MANIFEST.json at {mf}")
    manifest = json.load(open(mf))

    # group by physical dir (tarball minus .tar.gz)
    groups: dict[str, dict] = {}
    for comp, repo, ref, ver, tb in records(manifest):
        d = tb[:-7] if tb.endswith(".tar.gz") else tb
        if not os.path.isdir(os.path.join(gitdir, d)):
            continue  # tarball listed but not staged (skip silently)
        e = groups.setdefault(d, {"repo": repo, "ref": ref, "ver": ver,
                                  "comps": []})
        if comp and comp not in e["comps"]:
            e["comps"].append(comp)
        # prefer a non-empty repo/ref/ver if the first row lacked it
        for k, v in (("repo", repo), ("ref", ref), ("ver", ver)):
            if not e[k] and v:
                e[k] = v

    # INDEX.tsv
    with open(os.path.join(gitdir, "INDEX.tsv"), "w") as f:
        f.write("dir\trepo\tref\tversion\tcomponents\n")
        for d in sorted(groups):
            e = groups[d]
            f.write(f"{d}\t{e['repo']}\t{e['ref']}\t{e['ver']}\t"
                    f"{','.join(sorted(e['comps']))}\n")

    # symlink trees (relative links so they resolve on the mounted casket)
    bycomp = os.path.join(stage, "by-component")
    byrepo = os.path.join(stage, "by-repo")
    for p in (bycomp, byrepo):
        os.makedirs(p, exist_ok=True)
    n_comp = n_repo = 0
    for d, e in groups.items():
        target = os.path.join("..", "git", d)
        for comp in e["comps"]:
            link = os.path.join(bycomp, _safe(comp))
            if not os.path.lexists(link):
                os.symlink(target, link); n_comp += 1
        base = _repo_base(e["repo"])
        if base:
            link = os.path.join(byrepo, base)
            if os.path.lexists(link):  # same repo, different ref -> disambiguate
                link = os.path.join(byrepo, f"{base}-{(e['ref'] or e['ver'])[:12]}")
            if not os.path.lexists(link):
                os.symlink(target, link); n_repo += 1

    print(f"source-index: {len(groups)} dirs, {n_comp} by-component, "
          f"{n_repo} by-repo links, INDEX.tsv written")
    return 0


if __name__ == "__main__":
    sys.exit(main())
