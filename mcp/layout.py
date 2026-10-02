"""Readers for the TSV files a casket carries beside its source trees.

One reader per file, so every tool sees the same rows. The writers are
scripts/build-source-index.py (git/INDEX.tsv) and scripts/collect-submodules.py
(meta/SUBMODULES.tsv); tests/test_mcp_layout.py checks the readers against
their output. A missing or unreadable file reads as no rows: older caskets
lack some of these files, and the tools report that instead of failing.
"""
from __future__ import annotations

import os


def read_tsv(path: str) -> list[list[str]]:
    """Every non-blank line not starting with '#', split on tabs."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return [ln.rstrip("\n").split("\t") for ln in f
                    if ln.strip() and not ln.startswith("#")]
    except OSError:
        return []


def index_path(unit: str) -> str:
    return os.path.join(unit, "git", "INDEX.tsv")


def read_index(path: str) -> list[dict]:
    """git/INDEX.tsv -> [{dir, repo, ref, version, components}, ...].

    The first line is the header (`dir repo ref version components`). The
    writer always emits all 5 columns, so a shorter row is damaged and is
    skipped. Empty names in the comma-separated component list are dropped."""
    rows: list[dict] = []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            next(f, None)
            for line in f:
                c = line.rstrip("\n").split("\t")
                if len(c) < 5:
                    continue
                rows.append({
                    "dir": c[0], "repo": c[1], "ref": c[2], "version": c[3],
                    "components": [x for x in c[4].split(",") if x],
                })
    except OSError:
        pass
    return rows


def submodules_path(unit: str) -> str:
    return os.path.join(unit, "meta", "SUBMODULES.tsv")


def read_submodules(path: str) -> list[dict]:
    """meta/SUBMODULES.tsv -> [{component, path, repo, ref, exact}, ...].

    `repo` is as recorded: `owner/name` for GitHub, otherwise the raw URL.
    The header is a '#' line. Rows with fewer than 5 columns are skipped."""
    return [{"component": c[0], "path": c[1], "repo": c[2], "ref": c[3],
             "exact": c[4] == "1"}
            for c in read_tsv(path) if len(c) >= 5]
