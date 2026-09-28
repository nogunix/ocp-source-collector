#!/usr/bin/env python3
"""Lift a wrapper repo's submodules to the product level of an OpenGrok project.

Usage: promote-submodules.py <casket-product-dir> <staged-product-dir> <wrapper-list>

Runs from stage-sources.sh right after link_git_phase has linked every tree of
one B-operand product into <staged-product-dir>. For each staged link that
points at a tree whose repo is in <wrapper-list> (see
config/opengrok-promote-submodules.txt):

  - every filled submodule of that tree (meta/SUBMODULES.tsv, depth 1) is
    linked at the product level under its repo name, once per (repo, ref);
  - the wrapper link is replaced by a real directory holding links to the
    wrapper's own entries minus its submodule dirs, named after the wrapper
    repo, so nothing is indexed twice.

Promoted names are taken first: the submodule is the code people look for, the
wrapper is build glue. A name already in use gets "-<ref>" (then "-<tree>")
appended. Only symlinks are created; the casket is never touched.
"""
from __future__ import annotations

import csv
import os
import re
import sys


def read_list(path: str) -> set[str]:
    out = set()
    with open(path) as f:
        for line in f:
            line = line.split("#", 1)[0].strip()
            if line:
                out.add(line.lower())
    return out


def slug(repo: str) -> str:
    """https://github.com/o/r(.git) or o/r -> o/r (lowercase)."""
    r = repo.strip().rstrip("/")
    r = re.sub(r"^https?://github\.com/", "", r)
    r = re.sub(r"\.git$", "", r)
    return r.lower()


def rows(path: str) -> list[list[str]]:
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return [r for r in csv.reader(f, delimiter="\t") if r and not r[0].startswith("#")]


def safe(ref: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", ref).strip("-")


def filled(path: str) -> bool:
    return os.path.isdir(path) and any(True for _ in os.scandir(path))


def promote(casket: str, proj: str, wrappers: set[str]) -> list[str]:
    gitdir = os.path.join(casket, "git")
    index = [r for r in rows(os.path.join(gitdir, "INDEX.tsv")) if len(r) > 1 and r[0] != "dir"]
    repo_of = {r[0]: slug(r[1]) for r in index}
    # every component built from a tree (a tarball is named after only one)
    builds = {r[0]: set(r[4].split(",")) if len(r) > 4 else set() for r in index}
    subs: dict[str, list[tuple[str, str, str]]] = {}
    for r in rows(os.path.join(casket, "meta", "SUBMODULES.tsv")):
        if len(r) >= 4 and "/" not in r[1]:
            subs.setdefault(r[0], []).append((r[1], r[2], r[3]))

    # staged link name -> wrapper tree dir
    wrapper_links = {}
    for name in sorted(os.listdir(proj)):
        link = os.path.join(proj, name)
        if not os.path.islink(link):
            continue
        tree = os.path.basename(os.readlink(link).rstrip("/"))
        if repo_of.get(tree) in wrappers and subs.get(tree):
            wrapper_links[name] = tree
    if not wrapper_links:
        return []

    for name in wrapper_links:
        os.unlink(os.path.join(proj, name))
    taken = set(os.listdir(proj))
    log = []

    def claim(*cands: str) -> str:
        for c in cands:
            if c and c not in taken:
                taken.add(c)
                return c
        raise RuntimeError(f"no free name among {cands}")  # pragma: no cover

    # When two wrapper builds pin different commits of one submodule, the plain
    # name goes to the build of the image named after it: the operator tree
    # from the wrapper that built the operator image, not from the one that
    # built spire. The tree's own name is not enough -- a tarball is named
    # after whichever component claimed it first (cert-manager-operator's
    # build sits in jetstack-cert-manager-<sha>) -- so INDEX.tsv's component
    # list decides.
    cands = []
    for tree in set(wrapper_links.values()):
        built = builds.get(tree, set()) | {re.sub(r"-[0-9a-f]{7,40}$", "", tree)}
        for path, repo, ref in subs[tree]:
            base = slug(repo).rsplit("/", 1)[-1]
            cands.append((base, base not in built, tree, path, repo, ref))
    seen: set[tuple[str, str]] = set()
    for base, _, tree, path, repo, ref in sorted(cands):
        src = os.path.join(gitdir, tree, path)
        if (slug(repo), ref) in seen or not filled(src):
            continue
        seen.add((slug(repo), ref))
        name = claim(base, f"{base}-{safe(ref)}", f"{base}-{safe(ref)}-{tree}")
        os.symlink(src, os.path.join(proj, name))
        log.append(f"promoted {name} -> {tree}/{path} ({repo} {ref})")

    for tree in sorted(set(wrapper_links.values())):
        base = repo_of[tree].rsplit("/", 1)[-1]
        name = claim(base, f"{base}-{tree.rsplit('-', 1)[-1]}", tree)
        skip = {p for p, _, _ in subs[tree]}
        mirror = os.path.join(proj, name)
        os.mkdir(mirror)
        src = os.path.join(gitdir, tree)
        for entry in sorted(os.listdir(src)):
            if entry not in skip:
                os.symlink(os.path.join(src, entry), os.path.join(mirror, entry))
        log.append(f"wrapper  {name}/ -> {tree} (without {len(skip)} submodule dirs)")
    return log


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        print(__doc__.strip().splitlines()[2], file=sys.stderr)
        return 2
    casket, proj, listfile = argv[1:]
    if not os.path.exists(listfile) or not os.path.isdir(proj):
        return 0
    for line in promote(casket, proj, read_list(listfile)):
        print(f"[promote-submodules]   {os.path.basename(proj)}: {line}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
