#!/usr/bin/env python3
"""Report git submodule directories that are still EMPTY on the mounted caskets.

A GitHub codeload archive writes a gitlink as an empty directory, so a tree
that uses submodules reaches a casket as build glue with the code missing.
collect-submodules.py fills those at build time and records the result in the
casket's own meta/SUBMODULES.tsv, but caskets built before it existed -- the
certified and community catalogs -- carry the empty dirs with no record at all,
and for most of them a backfill would not help:

  certified: the parent commits are konflux-internal (trees API 422) and the
             .gitmodules carry no `branch`, so nothing can be pinned; the
             submodules are mostly Hugo doc themes (docsy, hugo-book, doks).
  community: dominated by WMCO's openshift/* forks, which already sit in the
             same minor's Phase A casket at the exact payload commit -- a
             branch-head copy would be a larger, less accurate duplicate.

So the gap is recorded rather than filled: this writes docs/submodule-gaps.tsv
(one row per empty dir, with where the code actually is, when it is anywhere)
and docs/submodule-gaps.md. Read-only; touches no casket and no mount.

Usage: report-submodule-gaps.py [--root /srv] [--out-dir docs]
"""
from __future__ import annotations

import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from submodulelib import github_slug, parse_gitmodules  # noqa: E402

MINOR_RE = re.compile(r"ocp(\d+\.\d+)")
MAX_DEPTH = 3  # git/<tree>/.gitmodules, plus one nested level


def unit_minor(mount_name: str) -> str:
    """4.22 out of sources-ocp4.22-community-operators / sources-layered-ocp4.22
    / sources-ocp4.22.9 — the key both sides of the "look here instead" join."""
    m = MINOR_RE.search(mount_name)
    return m.group(1) if m else ""


def iter_units(root: str):
    """(mount_name, unit_label, unit_dir) for every casket unit holding a git/.
    b-operand mounts hold one unit per product; everything else is one unit."""
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return
    for name in names:
        if not name.startswith("sources-"):
            continue
        mount = os.path.join(root, name)
        if os.path.isdir(os.path.join(mount, "git")):
            yield name, "", mount
            continue
        try:
            subs = sorted(os.listdir(mount))
        except OSError:
            continue
        for sub in subs:
            d = os.path.join(mount, sub)
            if os.path.isdir(os.path.join(d, "git")):
                yield name, sub, d


def find_gitmodules(git_dir: str, max_depth: int = MAX_DEPTH):
    """.gitmodules paths under git/, bounded — a submodule of a submodule is
    one level deeper, and an unbounded walk over a whole casket is minutes."""
    base = git_dir.rstrip("/").count("/")
    for dirpath, dirnames, filenames in os.walk(git_dir):
        if dirpath.count("/") - base >= max_depth:
            dirnames[:] = []
        if ".gitmodules" in filenames:
            yield os.path.join(dirpath, ".gitmodules")


def scan_unit(unit_dir: str):
    """(tree, path, url, slug, branch, state) rows; state = empty | filled."""
    git_dir = os.path.join(unit_dir, "git")
    rows = []
    for gm in find_gitmodules(git_dir):
        tree_dir = os.path.dirname(gm)
        tree = os.path.relpath(tree_dir, git_dir)
        try:
            with open(gm, errors="replace") as f:
                entries = parse_gitmodules(f.read())
        except OSError:
            continue
        parent_slug = ""  # relative URLs are rare here and resolve to ""
        for sm in entries:
            target = os.path.join(tree_dir, sm.path)
            try:
                state = "filled" if os.listdir(target) else "empty"
            except OSError:
                state = "missing-dir"
            rows.append((tree, sm.path, sm.url,
                         github_slug(sm.url, parent_slug), sm.branch, state))
    return rows


def build_repo_index(units) -> dict[str, list[tuple[str, str, str]]]:
    """repo basename -> [(minor, mount, tree dir)] from every unit's by-repo/.
    This is what lets an empty dir point at the casket that does have the code
    (usually Phase A, at the exact payload commit)."""
    index: dict[str, list[tuple[str, str, str]]] = {}
    for mount_name, _label, unit_dir in units:
        byrepo = os.path.join(unit_dir, "by-repo")
        try:
            aliases = os.listdir(byrepo)
        except OSError:
            continue
        for alias in aliases:
            index.setdefault(alias.lower(), []).append(
                (unit_minor(mount_name), mount_name, alias))
    return index


def pick_elsewhere(slug: str, minor: str, index) -> str:
    """Where the same repo is already collected. Same minor wins; a payload
    mount (sources-ocp<x.y.z>) wins over another catalog, because that copy is
    pinned to the release's own commit."""
    if not slug:
        return ""
    hits = index.get(slug.rsplit("/", 1)[-1].lower(), [])
    if not hits:
        return ""

    def rank(h):
        h_minor, mount, _alias = h
        payload = bool(re.fullmatch(r"sources-ocp\d+\.\d+\.\d+", mount))
        return (h_minor != minor, not payload, mount)

    _minor, mount, alias = sorted(hits, key=rank)[0]
    return f"{mount}:by-repo/{alias}"   # directly listable: ls /srv/<mount>/by-repo/<alias>


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="/srv")
    ap.add_argument("--out-dir", default=os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs"))
    args = ap.parse_args()

    units = list(iter_units(args.root))
    if not units:
        print(f"no casket units under {args.root}", file=sys.stderr)
        return 1
    index = build_repo_index(units)

    rows = []
    per_mount: dict[str, dict[str, int]] = {}
    for mount_name, label, unit_dir in units:
        minor = unit_minor(mount_name)
        counts = per_mount.setdefault(mount_name, {"empty": 0, "filled": 0})
        for tree, path, url, slug, branch, state in scan_unit(unit_dir):
            counts[state] = counts.get(state, 0) + 1
            if state == "filled":
                continue
            rows.append({
                "mount": mount_name, "product": label, "tree": tree,
                "path": path, "url": url, "slug": slug, "branch": branch,
                "elsewhere": pick_elsewhere(slug, minor, index),
            })

    os.makedirs(args.out_dir, exist_ok=True)
    tsv = os.path.join(args.out_dir, "submodule-gaps.tsv")
    with open(tsv, "w") as f:
        f.write("mount\tproduct\ttree\tpath\turl\tslug\tbranch\telsewhere\n")
        for r in sorted(rows, key=lambda r: (r["mount"], r["product"], r["tree"], r["path"])):
            f.write("\t".join(r[k] for k in
                              ("mount", "product", "tree", "path", "url",
                               "slug", "branch", "elsewhere")) + "\n")

    md = os.path.join(args.out_dir, "submodule-gaps.md")
    affected = {r["mount"] for r in rows}
    covered = sum(1 for r in rows if r["elsewhere"])
    with open(md, "w") as f:
        f.write("# Empty submodule directories on the mounted caskets\n\n")
        f.write(f"Generated by `scripts/report-submodule-gaps.py` from `{args.root}`.\n\n")
        f.write(f"- {len(rows)} empty submodule dirs across {len(affected)} mounts\n")
        f.write(f"- {covered} of them name a repo that another casket already carries "
                "(see the `elsewhere` column -- usually the payload mount, at the "
                "exact release commit)\n")
        f.write(f"- {len(rows) - covered} are collected nowhere in the fleet\n\n")
        f.write("A tree whose submodule dir is empty holds the build glue only: its\n"
                "Containerfiles and Makefile are there, the submodule's code is not.\n"
                "Search the `elsewhere` mount instead of concluding the code was\n"
                "never collected.\n\n")
        f.write("| mount | empty | filled |\n|---|---|---|\n")
        for mount in sorted(per_mount):
            c = per_mount[mount]
            if c["empty"]:
                f.write(f"| {mount} | {c['empty']} | {c['filled']} |\n")
        f.write("\nPer-row detail, including where each repo can be read instead: "
                "`docs/submodule-gaps.tsv`.\n")
    print(f"{len(rows)} empty dirs across {len(affected)} mounts -> {tsv}, {md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
