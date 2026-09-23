#!/usr/bin/env python3
"""Record which ref each source tarball was actually fetched from.

Usage: archive-provenance.py <tarball-dir> <git.tsv> > meta/git-fetched.tsv

Phase B's fetchers (phase-b-fetch-source.sh, phase-b-resolve-v2.sh) walk a
fallback chain -- the labelled sha, then tags, release branches, main/master,
HEAD -- but only log "OK <name> <kind>" and nothing at all for a tarball left
by an earlier run, so a casket could not say whether git/<name>/ is the commit
the image was built from or a branch head standing in for it. The answer is
recoverable offline: a GitHub archive holds one top-level directory named
"<repo>-<the ref that was asked for>", so a full sha there means the exact
commit and anything else a tag or branch.

Output matches phase-b-operand-fetch-source.sh's git-fetched.tsv
(tarball | url | kind | exact, url = "pre-existing:<top dir>"), so
export-upstream-sources.py and casket-mcp's permalink read both the same way.
git.tsv cols: operator | source_url | vcs_ref | tarball | ... (git-v2.tsv too).
"""
from __future__ import annotations

import glob
import os
import sys
import tarfile


def top_dir(path: str) -> str:
    """First member's top-level directory name, or "" if unreadable. Reads
    one header, not the whole archive."""
    try:
        with tarfile.open(path) as t:
            m = t.next()
            return m.name.split("/", 1)[0] if m else ""
    except (OSError, tarfile.TarError):
        return ""


def refs_by_tarball(git_tsv: str) -> dict[str, str]:
    refs: dict[str, str] = {}
    with open(git_tsv) as f:
        for line in f:
            c = line.rstrip("\n").split("\t")
            if len(c) >= 4 and c[2]:
                refs[c[3]] = c[2]
    return refs


def provenance(tar_dir: str, git_tsv: str) -> list[str]:
    refs = refs_by_tarball(git_tsv)
    out = ["# tarball\turl\tkind\texact"]
    for path in sorted(glob.glob(os.path.join(tar_dir, "*.tar.gz"))):
        name = os.path.basename(path)
        top = top_dir(path)
        ref = refs.get(name, "")
        # Same rule as record_existing in phase-b-operand-fetch-source.sh.
        exact = bool(ref and top and top.endswith(ref))
        out.append(f"{name}\tpre-existing:{top or 'unknown'}\t"
                   f"{'sha' if exact else 'preexisting'}\t{int(exact)}")
    return out


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__.strip().splitlines()[2], file=sys.stderr)
        return 2
    print("\n".join(provenance(argv[1], argv[2])))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
