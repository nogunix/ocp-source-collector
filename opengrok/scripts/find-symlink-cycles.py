#!/usr/bin/env python3
"""find-symlink-cycles.py — report symlink cycles in a tree OpenGrok will index.

Why this exists
---------------
OpenGrok's post-index cleanup (`PendingFileCompleter.tryDeleteParents` ->
`findFilelessChildren`) walks the xref tree with plain `java.io.File`:

    for path in DirectoryStream(dir):
        if path.isFile():   # stat(2) — FOLLOWS symlinks
            ...
        else:
            findFilelessChildren(path)   # recurses into symlinked dirs

There is no cycle check and no depth limit. A source tree containing a symlink
cycle therefore makes that walk explore an infinite, exponentially branching
path space. It does not crash and it does not log — it just burns one core
forever, at 94% CPU, producing no output, which reads exactly like "the index
is still building".

Measured on 2026-08-08 with srpms-4.22.4: fwupd-2.0.19 ships a mock sysfs tree
under src/tests/sys/ whose `subsystem` links point back at ancestor
directories, e.g.

    sys/class/block/sde                      -> ../../devices/.../block/sde
    sys/devices/.../block/sde/sde5/subsystem -> ../../../../class/block

Visited directories per depth grew 396 -> 657 -> 1112 -> 1879 -> 3242 -> 5738
-> 10151, a clean 1.72x per level. The indexer had reached depth 52 after 2h50m
of CPU; finishing that level alone would have taken ~4.7e11 directory visits.
It never completes. The job has to be killed by hand.

So: catch the cycle before the indexer meets it.

What it does
------------
Walks `root` following symlinks the way OpenGrok does, keeping the chain of
(st_dev, st_ino) for the directories on the current path. A directory already
on that chain is a cycle: the symlink that led there is reported and not
descended into. A global visited set keeps the scan linear -- without it this
tool would hang exactly like the indexer it is meant to protect.

Exit status: 0 = no cycles, 1 = cycles found (so callers can branch on it),
2 = usage error. The cycle list goes to stdout, one `<symlink>\t-> <target>`
per line; progress and totals go to stderr.
"""
import argparse
import os
import sys


def find_cycles(root, on_progress=None):
    """Yield (link_path, resolved_target) for every symlink closing a cycle.

    `root` is walked with symlinks followed, mirroring OpenGrok's traversal.
    Detection is by ancestor (st_dev, st_ino) match, which is what makes a
    cycle a cycle regardless of how the path is spelled.
    """
    seen_global = set()
    n_dirs = 0

    def walk(path, chain):
        nonlocal n_dirs
        try:
            st = os.stat(path)
        except OSError:
            return
        key = (st.st_dev, st.st_ino)
        if key in chain:
            return
        if key in seen_global:
            return
        seen_global.add(key)
        n_dirs += 1
        if on_progress and n_dirs % 100000 == 0:
            on_progress(n_dirs)

        try:
            entries = list(os.scandir(path))
        except OSError:
            return

        chain = chain | {key}
        for e in entries:
            try:
                # java File.isFile() -> stat, follows symlinks. Anything that
                # is not a plain file is recursed into, including symlinks
                # that point at directories.
                if e.is_file(follow_symlinks=True):
                    continue
            except OSError:
                continue
            try:
                est = os.stat(e.path)
            except OSError:
                continue          # broken symlink: DirectoryStream throws, no recursion
            if (est.st_dev, est.st_ino) in chain:
                # This entry points back at a directory on the current path.
                yield e.path, os.path.realpath(e.path)
                continue
            yield from walk(e.path, chain)

    yield from walk(root, frozenset())


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("root", help="directory to scan (e.g. /srv/opengrok-src)")
    ap.add_argument("-q", "--quiet", action="store_true",
                    help="suppress progress/summary on stderr")
    args = ap.parse_args(argv)

    if not os.path.isdir(args.root):
        print(f"not a directory: {args.root}", file=sys.stderr)
        return 2

    def progress(n):
        if not args.quiet:
            print(f"  ... {n} directories scanned", file=sys.stderr)

    sys.setrecursionlimit(20000)
    n = 0
    for link, target in find_cycles(args.root, on_progress=progress):
        print(f"{link}\t-> {target}")
        n += 1

    if not args.quiet:
        if n:
            print(f"{n} symlink cycle(s) found under {args.root}", file=sys.stderr)
        else:
            print(f"no symlink cycles under {args.root}", file=sys.stderr)
    return 1 if n else 0


if __name__ == "__main__":
    sys.exit(main())
