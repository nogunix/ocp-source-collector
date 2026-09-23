#!/usr/bin/env python3
"""Export the union of upstream source coordinates recorded in every mounted
casket's git/INDEX.tsv into state/upstream-sources.tsv — the manifest that
scripts/upstream-link-check.sh (upstream-link-check.timer) probes weekly.

Columns: repo | ref | version | kind | seen_in | candidates
  ref        commit sha or branch as recorded (may be empty)
  version    bare version as recorded (may be empty)
  kind       head (dir was fetched from the repo default branch: *-head)
             pin  (ref present)  /  tag  (version only)
  candidates space-separated codeload refs to try, in order; empty means
             "use check-upstream-links.py's legacy sha -> v<ver> -> <ver> chain"

Why candidates: the B / B-operand pipelines (all three operator catalogs and
the layered caskets) do not stop at the sha. The image's vcs-ref is often a
Konflux-internal commit that never reaches public GitHub, so they walk
candidate_source_urls() (scripts/lib-resolve.sh) through tags and branches.
The checker must walk the same list or it reports every such row as rot.
The list comes from calling that bash function directly, so there is one
implementation of the chain, not a Python copy that drifts.

B-operand caskets also record which candidate actually won
(meta/git-fetched.tsv), and the list is cut after it: if release-4.20 is gone
and only main would answer, the pipeline would now fetch different content,
which is exactly what this check should flag. Phase B caskets carry no such
record, so their rows get the full list.

state/ is host-local and not committed to the public repo, so this runs on
casket-host and regenerates the file each time. Content already fetched lives
safely inside caskets — this is early-warning monitoring of RE-fetchability
(docs/collection-model.md §8).
"""
from __future__ import annotations

import glob
import os
import re
import subprocess
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(SCRIPT_DIR, "..", "state", "upstream-sources.tsv")
SRV = "/srv"

# sources-ocp4.18-operators, sources-ocp4.18-certified-operators, ...
_PHASE_B = re.compile(r"^sources-ocp(\d+\.\d+)-(?:[a-z]+-)?operators$")
# sources-layered-ocp4.16/acm
_LAYERED = re.compile(r"^sources-layered-ocp(\d+\.\d+)/")


def pipeline_minor(label: str) -> str | None:
    """OCP minor for a casket fetched through candidate_source_urls, else None
    (Phase A and anything else keep the checker's legacy chain)."""
    m = _PHASE_B.match(label) or _LAYERED.match(label)
    return m.group(1) if m else None


def url_to_ref(url: str, repo: str) -> str | None:
    """https://github.com/o/r/archive/<X>.tar.gz -> X (codeload accepts X
    as-is: a sha, refs/tags/<t> or refs/heads/<b>)."""
    prefix = repo.rstrip("/") + "/archive/"
    if not (url.startswith(prefix) and url.endswith(".tar.gz")):
        return None
    return url[len(prefix):-len(".tar.gz")]


def archive_suffix(ref: str) -> str:
    """The part after "<repo>-" in a GitHub archive's top-level directory:
    branch slashes become dashes, and a tag's leading v is dropped when a
    digit follows (v1.2.3 -> 1.2.3)."""
    for p in ("refs/tags/", "refs/heads/"):
        if ref.startswith(p):
            name = ref[len(p):]
            if p == "refs/tags/" and re.match(r"^v\d", name):
                name = name[1:]
            return name.replace("/", "-")
    return ref


def cut_at_winner(cands: list[str], winner: str, repo: str) -> list[str]:
    """Truncate cands after the candidate the pipeline actually fetched.
    winner is a git-fetched.tsv url column: a real archive URL, or
    "pre-existing:<top dir>" for a tarball left by an earlier run. Unknown or
    unmatched winners keep the full list rather than guess."""
    if winner.startswith("pre-existing:"):
        top = winner[len("pre-existing:"):]
        name = repo.rstrip("/").rsplit("/", 1)[-1].lower()
        suffix = None
        for c in cands:
            if top.lower() == f"{name}-{archive_suffix(c)}".lower():
                suffix = archive_suffix(c)
                break
        else:
            # renamed repo: the top dir carries the new name; match the
            # suffix, longest first so "release-4.20" beats a bare "4.20"
            for c in sorted(cands, key=lambda c: -len(archive_suffix(c))):
                if top.endswith("-" + archive_suffix(c)):
                    suffix = archive_suffix(c)
                    break
        if suffix is None:
            return cands
        # Tags v1.2.3 and 1.2.3 both unpack to "<repo>-1.2.3", so the top dir
        # cannot say which one won: keep every candidate with that suffix.
        # (osc 4.16: kata-containers has only the bare 1.10.3 tag; cutting at
        # the first match kept v1.10.3 alone, a 404.)
        last = max(i for i, c in enumerate(cands) if archive_suffix(c) == suffix)
        return cands[: last + 1]
    ref = url_to_ref(winner, repo)
    if ref in cands:
        return cands[: cands.index(ref) + 1]
    return cands


def pipeline_candidates(queries: list[tuple[str, str, str, str]]) -> list[list[str]]:
    """Run candidate_source_urls for every (src, ref, ver, minor) in one bash
    process; each answer is terminated by a lone "--" line."""
    if not queries:
        return []
    # Tab is IFS whitespace, so `read` would collapse an empty field and shift
    # the rest; empty values travel as "-" (the pipelines' own convention).
    script = (
        f'source "{SCRIPT_DIR}/lib-resolve.sh"\n'
        "while IFS=$'\\t' read -r src ref ver minor; do\n"
        '  [[ "$ref" == - ]] && ref=""; [[ "$ver" == - ]] && ver=""\n'
        '  candidate_source_urls "$src" "$ref" "$ver" "$minor"; echo --\n'
        "done\n")
    stdin = "".join("\t".join(v or "-" for v in q) + "\n" for q in queries)
    out = subprocess.run(["bash", "-c", script], input=stdin, text=True,
                         capture_output=True, check=True).stdout
    answers: list[list[str]] = [[]]
    for line in out.splitlines():
        if line == "--":
            answers.append([])
        elif line:
            answers[-1].append(line)
    answers.pop()  # after the final "--"
    if len(answers) != len(queries):
        raise RuntimeError(f"candidate_source_urls: {len(answers)} answers for {len(queries)} rows")
    return answers


def load_winners(label: str) -> dict[str, str]:
    """git dir name -> git-fetched.tsv url column (B-operand caskets only)."""
    path = os.path.join(SRV, label, "meta", "git-fetched.tsv")
    wins: dict[str, str] = {}
    if os.path.isfile(path):
        with open(path) as f:
            for line in f:
                c = line.rstrip("\n").split("\t")
                if line.startswith("#") or len(c) < 2:
                    continue
                wins[c[0].removesuffix(".tar.gz")] = c[1]
    return wins


def collect(index_paths: list[str]) -> list[tuple[str, ...]]:
    raw = []  # (repo, ref, ver, kind, label, dir, minor)
    for idx in index_paths:
        label = idx[len(SRV) + 1:].removesuffix("/git/INDEX.tsv")
        minor = pipeline_minor(label)
        with open(idx) as f:
            lines = f.read().splitlines()[1:]
        for line in lines:
            c = line.split("\t")
            if len(c) < 4 or not c[1].startswith("https://github.com/"):
                continue
            d, repo, ref, ver = c[0], c[1].rstrip("/"), c[2], c[3]
            kind = "head" if d.endswith("-head") else ("pin" if ref else "tag")
            if not ref and not ver and kind != "head":
                continue
            raw.append((repo, ref, ver, kind, label, d, minor))

    todo = [i for i, r in enumerate(raw) if r[6] and r[3] != "head"]
    answers = pipeline_candidates([(raw[i][0], raw[i][1], raw[i][2], raw[i][6]) for i in todo])
    cands: dict[int, str] = {}
    winners: dict[str, dict[str, str]] = {}
    for i, urls in zip(todo, answers):
        repo, _, _, _, label, d, _ = raw[i]
        refs = [r for r in (url_to_ref(u, repo) for u in urls) if r]
        if label not in winners:
            winners[label] = load_winners(label)
        if d in winners[label]:
            refs = cut_at_winner(refs, winners[label][d], repo)
        cands[i] = " ".join(refs)

    # One row per distinct (coordinates, candidate list): the same commit
    # fetched under two minors can have different chains, and each must hold.
    seen: set[tuple[str, ...]] = set()
    rows = []
    for i, (repo, ref, ver, kind, label, _d, _m) in sorted(
            enumerate(raw), key=lambda t: (t[1][0], t[1][1], t[1][2], t[1][3], t[1][4])):
        key = (repo, ref, ver, kind, cands.get(i, ""))
        if key in seen:
            continue
        seen.add(key)
        rows.append((repo, ref, ver, kind, label, cands.get(i, "")))
    return rows


def main() -> int:
    idx = sorted(glob.glob(f"{SRV}/sources-*/git/INDEX.tsv")
                 + glob.glob(f"{SRV}/sources-*/*/git/INDEX.tsv"))
    rows = collect(idx)
    # No caskets mounted yields no rows. Replacing a good manifest with an
    # empty one would make the checker report "0 rows, all ok" -- a silent
    # pass -- so refuse and keep the previous manifest.
    if not rows:
        print(f"no GitHub rows found under {SRV}/sources-*/ (caskets not mounted?); "
              f"keeping {OUT}", file=sys.stderr)
        return 1
    tmp = OUT + ".tmp"
    with open(tmp, "w") as f:
        f.write("# repo\tref\tversion\tkind\tseen_in\tcandidates\n")
        f.writelines("\t".join(r) + "\n" for r in rows)
    os.replace(tmp, OUT)
    n_c = sum(1 for r in rows if r[5])
    print(f"wrote {OUT}: {len(rows)} rows across {len({r[0] for r in rows})} repos "
          f"({n_c} with pipeline candidates)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
