"""Release-aware questions only the caskets can answer, offline.

GitHub knows repos, commits and PRs; it does not know which commit a given
OpenShift release shipped, which source an image digest was built from, which
RHEL patches RHCOS carries, or what every shipped component vendors. The
caskets record exactly that (meta/*.tsv beside each source tree). Every tool
here answers from the mounts alone and, where GitHub can take the question
further, returns the owner/repo/commit arguments for the GitHub MCP tools
(get_commit, list_commits, pull_request_read) instead of calling GitHub itself
-- so the same answers work in an air-gapped deployment.

Tools: source_for_image, find_dependency_users, release_diff, rpm_source,
check_patch_shipped.
"""
from __future__ import annotations

import os
import re
import subprocess

import backends as be

_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")


# ------------------------------------------------------------------ helpers
def _read_tsv(path: str) -> list[list[str]]:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return [ln.rstrip("\n").split("\t") for ln in f
                    if ln.strip() and not ln.startswith("#")]
    except OSError:
        return []


def _rg_fixed(needle: str, files: list[str], timeout: int = 120) -> list[tuple[str, str]]:
    """Lines containing `needle` (fixed string) in `files` -> [(path, line)].
    A prefilter only: callers re-check the column they care about."""
    files = [f for f in files if os.path.isfile(f)]
    if not files or not needle:
        return []
    try:
        proc = subprocess.run(
            ["rg", "-F", "--no-heading", "--with-filename", "--null",
             "--no-line-number", "--", needle, *files],
            capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return []
    out = []
    for raw in proc.stdout.splitlines():
        path, _, line = raw.partition("\0")
        out.append((path, line))
    return out


def _slug(repo: str) -> str:
    r = repo.strip().rstrip("/")
    r = re.sub(r"^(https?://)?github\.com/", "", r)
    r = re.sub(r"^git@github\.com:", "", r)
    return r.removesuffix(".git").lower()


def _github_args(repo: str, base: str = "", head: str = "") -> dict | None:
    """owner/repo (+ refs) shaped for the GitHub MCP tools; None off GitHub."""
    if "github.com" not in repo and not re.match(r"^[\w.-]+/[\w.-]+$", repo):
        return None
    owner, _, name = _slug(repo).partition("/")
    d = {"owner": owner, "repo": name}
    if base and head:
        d.update(base=base, head=head,
                 compare_url=f"https://github.com/{owner}/{name}/compare/{base}...{head}")
    elif head:
        d["sha"] = head
    return d


def _mounts(version: str) -> list[be.Mount]:
    """Mounts for a version filter, a-rpm excluded. "" = all; a minor ("4.20")
    = every mount of that minor; a patch ("4.20.35") = that Phase A patch plus
    the minor-scoped B/b-operand/catalog mounts (they have no patches).
    backends._mounts_for_version matches a patch by its minor, which would
    answer "4.20.35" with every 4.20.x payload."""
    out = []
    for m in be.list_mounts():
        if m.phase == "a-rpm":
            continue
        if not version:
            out.append(m)
            continue
        minor = ".".join(version.split(".")[:2])
        if m.phase == "a":
            if m.version == version or (version == minor and m.version.startswith(minor + ".")):
                out.append(m)
        elif m.version == minor:
            out.append(m)
    return out


def _units(mounts: list[be.Mount]):
    """(mount, product, unit_dir) for every unit that carries meta/."""
    for m in mounts:
        if os.path.isdir(os.path.join(m.path, "meta")):
            yield m, None, m.path
            continue
        try:
            subs = sorted(os.listdir(m.path))
        except OSError:
            continue
        for sub in subs:
            d = os.path.join(m.path, sub)
            if os.path.isdir(os.path.join(d, "meta")):
                yield m, sub, d


def _index(unit: str) -> list[dict]:
    """git/INDEX.tsv rows: dir | repo | ref | version | components."""
    rows = []
    for c in _read_tsv(os.path.join(unit, "git", "INDEX.tsv")):
        if len(c) >= 3 and c[0] != "dir":
            rows.append({"dir": c[0], "repo": c[1], "ref": c[2],
                         "components": c[4].split(",") if len(c) > 4 and c[4] else []})
    return rows


def _where(m: be.Mount, product: str | None) -> dict:
    return {"mount": m.name, "phase": m.phase, "version": m.version, "product": product}


# -------------------------------------------------------- A: source_for_image
def source_for_image(image: str, version: str = "") -> dict:
    """Image digest / reference -> the releases that ship it and its source."""
    q = image.strip()
    m = _DIGEST_RE.search(q)
    digest = m.group(0) if m else (f"sha256:{q}" if _HEX64_RE.match(q) else "")
    if digest:
        needle = digest
    else:
        # repository without tag: "reg:5000/ns/img:tag" -> "reg:5000/ns/img"
        ref = q.split("@", 1)[0]
        head, _, last = ref.rpartition("/")
        needle = (head + "/" if head else "") + last.split(":", 1)[0]
    if not needle:
        return {"query": image, "error": "give an image reference or a sha256 digest"}

    mounts = _mounts(version)
    files, owner = [], {}
    for mt, product, unit in _units(mounts):
        for name in ("images.tsv", "containers.tsv", "bundles.tsv", "IMAGE_MAP.tsv"):
            f = os.path.join(unit, "meta", name)
            files.append(f)
            owner[f] = (mt, product, unit, name)

    matches, seen = [], set()
    for path, line in _rg_fixed(needle, files):
        mt, product, unit, kind = owner[path]
        c = line.split("\t")
        if kind == "images.tsv":            # component | image [| digest]
            comp, img = c[0], c[1] if len(c) > 1 else ""
        elif kind == "containers.tsv":      # operator | csv | image | version
            comp, img = c[0], c[2] if len(c) > 2 else ""
        elif kind == "bundles.tsv":         # operator | channel | csv | bundle image
            comp, img = c[0], c[3] if len(c) > 3 else ""
        else:                               # IMAGE_MAP: image | component | git_dir | infix
            comp, img = (c[1] if len(c) > 1 else ""), c[0]
        if needle not in img:
            continue
        key = (mt.name, product, comp, img)
        if key in seen:
            continue
        seen.add(key)
        src = _image_source(mt, unit, kind, comp, c)
        matches.append({**_where(mt, product), "component": comp, "image": img,
                        "listed_in": f"meta/{kind}"
                        + (" (operator bundle image)" if kind == "bundles.tsv" else ""),
                        **src})
    matches.sort(key=lambda r: (r["phase"], r["version"], r["product"] or "", r["component"]))

    out = {"query": image, "digest": digest or None, "count": len(matches), "matches": matches}
    if digest:
        repos = sorted({mm["image"].split("@", 1)[0] for mm in matches})
        out["rpms"] = _image_rpms(digest, repos)
    if not matches:
        out["note"] = ("not listed in any mounted casket's image tables; check the "
                       "version filter, or the image may be from a catalog/minor that is not mounted")
    return out


def _image_source(mt: be.Mount, unit: str, kind: str, comp: str, cols: list[str]) -> dict:
    """repo / ref / tree for one image row. Phase A commits are the payload's
    own record (exact); B/b-operand trees may be a tag/branch stand-in."""
    idx = _index(unit)
    row = None
    if kind == "IMAGE_MAP.tsv" and len(cols) > 2:
        row = next((r for r in idx if r["dir"] == cols[2]), None)
    elif kind == "images.tsv" and mt.phase == "a":
        commits = {c[0]: (c[1], c[2]) for c in _read_tsv(os.path.join(unit, "meta", "commits.tsv"))
                   if len(c) >= 3}
        repo, ref = commits.get(comp, ("", ""))
        row = next((r for r in idx if comp in r["components"]), None) or \
            next((r for r in idx if ref and r["ref"] == ref), None)
        if row is None and repo:
            return {"repo": repo, "ref": ref, "path": None, "exact": True,
                    "github": _github_args(repo, head=ref)}
    else:
        git = {c[0]: c for c in _read_tsv(os.path.join(unit, "meta", "git.tsv")) if len(c) >= 4}
        g = git.get(comp)
        if g:
            d = g[3].removesuffix(".tar.gz")
            row = next((r for r in idx if r["dir"] == d), None) or \
                {"dir": d, "repo": g[1], "ref": g[2], "components": [comp]}
        else:
            row = next((r for r in idx if comp in r["components"]), None)
    if row is None:
        return {"repo": None, "ref": None, "path": None, "exact": None,
                "note": "no source tree recorded for this component (see SOURCE-NOT-COLLECTED / meta/git.tsv)"}
    path = os.path.join(unit, "git", row["dir"])
    if mt.phase == "a":
        exact = True
    else:
        approx, _refs = be._fetched_approx(unit, row["dir"], row["repo"])
        exact = not approx
    return {"repo": row["repo"], "ref": row["ref"],
            "path": path if os.path.isdir(path) else None, "exact": exact,
            "github": _github_args(row["repo"], head=row["ref"]) if row["repo"] else None}


def _image_rpms(digest: str, repos: list[str], cap: int = 400) -> dict:
    """SRPMs inside the image, from a-rpm's container inventory (stream C).
    The inventory is keyed by the digest it was taken at; a casket rebuilt
    since carries newer digests, so fall back to the same repository's
    inventoried digests and say so."""
    f = os.path.join(be.SRV, "sources-ocp-srpms", "meta", "image-rpms.tsv")
    src = "sources-ocp-srpms/meta/image-rpms.tsv (read with rpm_source)"
    nevrs = sorted({c[2] for _p, ln in _rg_fixed(digest, [f])
                    if len(c := ln.split("\t")) > 2 and c[0] == digest})
    if nevrs:
        return {"match": "exact digest", "count": len(nevrs), "srpm_nevrs": nevrs[:cap],
                "truncated": len(nevrs) > cap, "source": src}
    other: dict[str, int] = {}
    for repo in repos:
        for _p, ln in _rg_fixed(repo, [f]):
            c = ln.split("\t")
            if len(c) > 2 and c[1] == repo:
                other[c[0]] = other.get(c[0], 0) + 1
    if other:
        return {"match": "none for this digest", "count": 0, "srpm_nevrs": [],
                "same_repository_inventoried": [{"digest": d, "srpm_count": n}
                                                for d, n in sorted(other.items())][:20],
                "note": "the inventory predates this digest; the listed digests of the same "
                        "repository are other builds (source_for_image them for their release)",
                "source": src}
    return {"match": "none", "count": 0, "srpm_nevrs": [], "source": src}


# ---------------------------------------------------- B: find_dependency_users
def _vkey(v: str) -> tuple:
    """Sortable key for semver-ish versions (Go incl. pseudo-versions, npm,
    crates, PEP 440 approximated). Pre-release sorts before its release."""
    v = v.strip().lstrip("vV").split("+", 1)[0]
    main, _, pre = v.partition("-")
    nums = [int(x) for x in re.findall(r"\d+", main)][:4]
    nums += [0] * (4 - len(nums))
    pre_key = tuple((0, int(p)) if p.isdigit() else (1, p) for p in re.split(r"[.\-]", pre)) if pre else ()
    return (*nums, 0 if pre else 1, pre_key)


def _satisfies(version: str, constraint: str) -> bool:
    """constraint: '', '1.2.3' (exact), or comma-joined ops: '<1.2.3,>=1.0'."""
    if not constraint.strip():
        return True
    vk = _vkey(version)
    for part in constraint.split(","):
        part = part.strip()
        mm = re.match(r"^(<=|>=|==|!=|<|>|=)?\s*(.+)$", part)
        op, target = (mm.group(1) or "=="), _vkey(mm.group(2))
        ok = {"<": vk < target, "<=": vk <= target, ">": vk > target, ">=": vk >= target,
              "==": vk == target, "=": vk == target, "!=": vk != target}[op]
        if not ok:
            return False
    return True


_MAX_VERSIONS = 30
_GOMOD_CACHE: dict[str, dict[str, str]] = {}
_REQ_LINE = re.compile(r"^\s*(?:require\s+)?([^\s()]+)\s+(v[^\s]+)")


def _go_mod_requires(path: str) -> dict[str, str]:
    """module -> version required by go.mod (replace directives honoured when
    they pin a version). {} when there is no go.mod."""
    if path in _GOMOD_CACHE:
        return _GOMOD_CACHE[path]
    req: dict[str, str] = {}
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        _GOMOD_CACHE[path] = req
        return req
    block = None
    for line in text.splitlines():
        line = line.split("//", 1)[0].rstrip()
        s = line.strip()
        if s.startswith(("require (", "replace (")):
            block = s.split()[0]
            continue
        if s == ")":
            block = None
            continue
        if s.startswith("replace ") or block == "replace":
            body = s[len("replace "):] if s.startswith("replace ") else s
            lhs, _, rhs = body.partition("=>")
            rp = rhs.split()
            if len(rp) == 2 and lhs.split():
                req[lhs.split()[0]] = rp[1]
            continue
        if s.startswith("require ") or block == "require":
            mm = _REQ_LINE.match(s)
            if mm and mm.group(1) not in req:
                req[mm.group(1)] = mm.group(2)
    _GOMOD_CACHE[path] = req
    return req


def find_dependency_users(name: str, version_constraint: str = "", version: str = "",
                          ecosystem: str = "", selected_only: bool = False,
                          max_results: int = 500) -> dict:
    """Which shipped components vendor/lock dependency `name`, at which version."""
    if not name.strip():
        return {"error": "name required (e.g. golang.org/x/net)"}
    mounts = _mounts(version)
    files, owner = [], {}
    for mt, product, unit in _units(mounts):
        f = os.path.join(unit, "meta", "DEPS.tsv")
        files.append(f)
        owner[f] = (mt, product, unit)

    hits, versions = [], {}
    for path, line in _rg_fixed(name, files):
        c = line.split("\t")     # component | manifest | ecosystem | name | version | dir | status
        if len(c) < 5 or c[3] != name:
            continue
        if ecosystem and c[2] != ecosystem:
            continue
        if not _satisfies(c[4], version_constraint):
            continue
        mt, product, unit = owner[path]
        selected = None
        if c[2] == "go" and c[1].endswith("go.sum"):
            req = _go_mod_requires(os.path.join(unit, "git", c[0], c[1][:-len("go.sum")] + "go.mod"))
            selected = (req.get(name) == c[4]) if req else None
            if selected is False and selected_only:
                continue
        versions[c[4]] = versions.get(c[4], 0) + 1
        dep_dir = os.path.join(unit, "deps", c[5]) if len(c) > 5 and c[5] else None
        hits.append({**_where(mt, product), "component": c[0], "manifest": c[1],
                     "ecosystem": c[2], "dep_version": c[4], "selected": selected,
                     "dep_source": dep_dir if dep_dir and os.path.isdir(dep_dir) else None,
                     "status": c[6] if len(c) > 6 else ""})
    hits.sort(key=lambda h: (h["phase"], _vkey(h["version"] or "0"), h["product"] or "",
                             h["component"], _vkey(h["dep_version"])))
    by_release: dict[str, set] = {}
    for h in hits:
        by_release.setdefault(f'{h["phase"]} {h["version"]}', set()).add(
            (h["product"] or "") + "/" + h["component"])
    # A widely vendored library shows hundreds of versions (x/net: 41 in one
    # minor, most of them go.sum pseudo-versions). Keep the newest _MAX_VERSIONS,
    # which are the ones nearest a fix; the total says how many were dropped.
    by_version = sorted(versions.items(), key=lambda kv: _vkey(kv[0]))
    shown = by_version[-_MAX_VERSIONS:]
    return {
        "dependency": name, "constraint": version_constraint or None,
        "count": len(hits), "truncated": len(hits) > max_results,
        "versions_found": dict(shown),
        "versions_total": len(by_version),
        "versions_truncated": len(by_version) > len(shown),
        "components_per_release": {k: len(v) for k, v in sorted(by_release.items())},
        "hits": hits[:max_results],
        "note": ("rows come from meta/DEPS.tsv: the lockfiles of the SHIPPED source. Go rows "
                 "are go.sum, which lists every version in the module graph -- 'selected' "
                 "says whether go.mod actually requires that version (None: no go.mod / not Go); "
                 "pass selected_only=True for CVE impact. A dependency fetched at build time "
                 "outside a lockfile is not seen"),
    }


# -------------------------------------------------------- C: release_diff
def _phase_a_mount(ver: str) -> be.Mount | None:
    return next((m for m in be.list_mounts() if m.phase == "a" and m.version == ver), None)


def _rpm_set(ver: str) -> dict[str, str]:
    d = os.path.join(be.SRV, "sources-ocp-srpms", "by-ocp", ver)
    out = {}
    try:
        for name in os.listdir(d):
            out[name] = os.path.basename(os.readlink(os.path.join(d, name)).rstrip("/")) \
                if os.path.islink(os.path.join(d, name)) else name
    except OSError:
        return {}
    return out


def release_diff(from_version: str, to_version: str, include_rpms: bool = True) -> dict:
    """What changed between two OCP payload releases (Phase A patches)."""
    a, b = _phase_a_mount(from_version), _phase_a_mount(to_version)
    missing = [v for v, m in ((from_version, a), (to_version, b)) if m is None]
    if missing:
        mounted = sorted((m.version for m in be.list_mounts() if m.phase == "a"), key=_vkey)
        return {"error": f"not mounted as Phase A: {', '.join(missing)}", "mounted": mounted}

    def commits(m):
        return {c[0]: (c[1], c[2]) for c in _read_tsv(os.path.join(m.path, "meta", "commits.tsv"))
                if len(c) >= 3}
    old, new = commits(a), commits(b)
    groups: dict[tuple, list[str]] = {}
    for comp in sorted(set(old) & set(new)):
        if old[comp] != new[comp]:
            groups.setdefault((new[comp][0], old[comp][1], new[comp][1]), []).append(comp)
    changed = []
    for (repo, o, n), comps in sorted(groups.items()):
        g = _github_args(repo, base=o, head=n)
        changed.append({"repo": repo, "from_commit": o, "to_commit": n, "components": comps,
                        "github": g})
    out = {
        "from": from_version, "to": to_version,
        "changed_repos": len(changed), "changed": changed,
        "added_components": sorted((c, *new[c]) for c in set(new) - set(old)),
        "removed_components": sorted((c, *old[c]) for c in set(old) - set(new)),
        "unchanged_components": sum(1 for c in set(old) & set(new) if old[c] == new[c]),
        "next_step": ("for PRs / Jira keys per changed repo, call the GitHub MCP "
                      "list_commits(owner, repo, sha=to_commit) and read until from_commit; "
                      "merge commits name the PR ('Merge pull request #N'), then "
                      "pull_request_read for its title/body"),
    }
    if include_rpms:
        ro, rn = _rpm_set(from_version), _rpm_set(to_version)
        if ro or rn:
            out["rpms"] = {
                "changed": sorted((p, ro[p], rn[p]) for p in set(ro) & set(rn) if ro[p] != rn[p]),
                "added": sorted((p, rn[p]) for p in set(rn) - set(ro)),
                "removed": sorted((p, ro[p]) for p in set(ro) - set(rn)),
            }
        else:
            byocp = os.path.join(be.SRV, "sources-ocp-srpms", "by-ocp")
            have = sorted(os.listdir(byocp), key=_vkey) if os.path.isdir(byocp) else []
            out["rpms"] = {"note": ("a-rpm keeps one patch per minor, and neither release is "
                                    "one of them; compare RHCOS packages with rpm_source "
                                    "across these instead"),
                           "a_rpm_versions": have}
    return out


# -------------------------------------------------------------- D: rpm_source
def _srpm_root() -> str:
    return os.path.join(be.SRV, "sources-ocp-srpms")


def _resolve_ocp(ver: str) -> str | None:
    """'4.20.38' as is; a minor ('4.20') -> the newest base patch in by-ocp."""
    root = os.path.join(_srpm_root(), "by-ocp")
    try:
        have = os.listdir(root)
    except OSError:
        return None
    if ver in have:
        return ver
    cands = [v for v in have if "-" not in v and v.startswith(ver + ".")]
    return max(cands, key=_vkey) if cands else None


def _bin_to_src(binary: str, ver: str) -> str | None:
    for c in _read_tsv(os.path.join(_srpm_root(), "meta", "bin-to-src.tsv")):
        if len(c) >= 3 and c[2].split("-")[0] == ver.split("-")[0] \
                and c[0].rsplit("-", 2)[0] == binary:
            return c[1].rsplit("-", 2)[0]
    return None


def rpm_source(package: str, version: str) -> dict:
    """RHCOS package (source or binary name) -> NEVR and the Red Hat SRPM tree."""
    ver = _resolve_ocp(version)
    if ver is None:
        return {"error": f"no a-rpm view for {version}",
                "mounted": sorted(os.listdir(os.path.join(_srpm_root(), "by-ocp")), key=_vkey)
                if os.path.isdir(os.path.join(_srpm_root(), "by-ocp")) else []}
    by = os.path.join(_srpm_root(), "by-ocp")
    src, via = package, None
    variants = [ver, f"{ver}-extensions"]
    view = next((v for v in variants if os.path.exists(os.path.join(by, v, package))), None)
    if view is None:
        src = _bin_to_src(package, ver) or ""
        via = "binary package (meta/bin-to-src.tsv)" if src else None
        view = next((v for v in variants if src and os.path.exists(os.path.join(by, v, src))), None)
    if view is None:
        return {"package": package, "ocp_version": ver,
                "error": "not in this release's RHCOS SRPM set (base or -extensions)"}
    tree = os.path.realpath(os.path.join(by, view, src))
    ls = lambda d: sorted(os.listdir(os.path.join(tree, d))) if os.path.isdir(os.path.join(tree, d)) else []
    archives = ls("archives")
    others = {}
    for v in sorted(os.listdir(by), key=_vkey):
        p = os.path.join(by, v, src)
        if os.path.exists(p):
            others[v] = os.path.basename(os.path.realpath(p))
    return {
        "package": package, "source_package": src, "resolved_via": via,
        "ocp_version": ver, "view": view,
        "nevr": os.path.basename(tree), "path": tree,
        "spec": [os.path.join(tree, "info", f) for f in ls("info") if f.endswith(".spec")],
        "patches": [f for f in archives if re.search(r"\.(patch|diff)$", f)],
        "sources": [f for f in archives if not re.search(r"\.(patch|diff)$", f)],
        "patched_tree": [os.path.join(tree, "pre-build", d) for d in ls("pre-build")],
        "incomplete": os.path.exists(os.path.join(tree, "_incomplete")),
        "nevr_by_release": others,
        "note": ("RHEL SRPMs are not on GitHub: patches/ and the spec are the Red Hat "
                 "delta; patched_tree is %prep output (empty when incomplete=true)"),
    }


# ----------------------------------------------- F: check_patch_shipped
def _norm(line: str) -> str:
    return " ".join(line.split())


def parse_patch(patch: str) -> list[dict]:
    """Unified diff -> [{path, new_file, deleted, hunks:[{old, new}]}].
    old/new are a hunk's line sequences (context + removed / context + added).

    Accepts `git show` / GitHub `.diff` text, and also GitHub MCP get_commit
    file patches, which carry no ---/+++ headers: join them as
    "diff --git a/<filename> b/<filename>" followed by that file's patch."""
    files, cur, hunk = [], None, None
    for raw in patch.splitlines():
        if raw.startswith("diff --git "):
            m = re.match(r"^diff --git a/(.+?) b/(.+)$", raw)
            cur = {"old_path": "a/" + m.group(1), "path": "b/" + m.group(2),
                   "hunks": [], "headers": False} if m else None
            if cur:
                files.append(cur)
            hunk = None
            continue
        if raw.startswith("--- ") and hunk is None:
            if cur is None or cur["headers"]:
                cur = {"old_path": "", "path": "", "hunks": [], "headers": False}
                files.append(cur)
            cur["old_path"], cur["headers"] = raw[4:].strip(), True
            continue
        if raw.startswith("+++ ") and hunk is None and cur is not None:
            cur["path"] = raw[4:].strip()
            continue
        if raw.startswith("@@") and cur is not None:
            hunk = {"old": [], "new": [], "add": [], "rem": [], "header": raw}
            cur["hunks"].append(hunk)
            continue
        if hunk is None or raw.startswith("\\"):
            continue
        tag, body = (raw[:1], raw[1:]) if raw else (" ", "")
        if tag in (" ", "-"):
            hunk["old"].append(body)
        if tag in (" ", "+"):
            hunk["new"].append(body)
        if tag == "+":
            hunk["add"].append(body)
        elif tag == "-":
            hunk["rem"].append(body)
    out = []
    strip = lambda p: re.sub(r"^[ab]/", "", p.split("\t", 1)[0])
    for f in files:
        new_file = f["old_path"].startswith("/dev/null") or (
            bool(f["hunks"]) and all(h["header"].startswith("@@ -0,0 ") for h in f["hunks"]))
        deleted = f["path"].startswith("/dev/null")
        path = strip(f["old_path"] if deleted else f["path"])
        if path and f["hunks"] or deleted:
            out.append({"path": path, "new_file": new_file, "deleted": deleted,
                        "hunks": [{k: h[k] for k in ("old", "new", "add", "rem")}
                                  for h in f["hunks"]]})
    return out


def _contains_block(hay: list[str], block: list[str]) -> bool:
    blk = [b for b in (_norm(x) for x in block) if b]
    if not blk:
        return False
    n = len(blk)
    for i in range(len(hay) - n + 1):
        if hay[i] == blk[0] and hay[i:i + n] == blk:
            return True
    return False


_MEANINGFUL = re.compile(r"[A-Za-z0-9_]{3,}")


def _meaningful(lines: list[str]) -> list[str]:
    """Lines that identify a change: not blank, not bare punctuation like '},'."""
    return [n for n in (_norm(x) for x in lines) if _MEANINGFUL.search(n)]


def _in_order(hay: list[str], needles: list[str]) -> bool:
    i = 0
    for n in needles:
        try:
            i = hay.index(n, i) + 1
        except ValueError:
            return False
    return True


def _hunk_state(hay: list[str], h: dict) -> str | None:
    """applied / not_applied; None for a whitespace-only hunk.
    The exact post-image first. Then, because later commits shift context, the
    hunk's own added lines in order. The pre-image is never evidence for an
    addition: it is just context, which the patched file contains as well. A
    removal-only hunk is applied when its removed lines are gone."""
    add, rem = _meaningful(h["add"]), _meaningful(h["rem"])
    if not add and not rem:
        return None
    if _contains_block(hay, h["new"]):
        return "applied"
    if add:
        return "applied" if _in_order(hay, add) else "not_applied"
    if _contains_block(hay, h["old"]) or _in_order(hay, rem):
        return "not_applied"
    return "applied"


def _check_file(tree: str, f: dict) -> str:
    p = os.path.join(tree, f["path"])
    if f["deleted"]:
        return "applied" if not os.path.exists(p) else "not_applied"
    try:
        with open(p, encoding="utf-8", errors="replace") as fh:
            hay = [x for x in (_norm(ln) for ln in fh) if x]
    except OSError:
        return "not_applied" if f["new_file"] else "file_missing"
    res = {st for h in f["hunks"] if (st := _hunk_state(hay, h)) is not None}
    if not res or res == {"applied"}:
        return "applied"
    if res == {"not_applied"}:
        return "not_applied"
    return "partial"


def _shipped_trees(repo: str, version: str):
    """Every mounted tree built from `repo`: INDEX.tsv rows and filled submodules."""
    want = _slug(repo)
    mounts = _mounts(version)
    for mt, product, unit in _units(mounts):
        for r in _index(unit):
            if _slug(r["repo"]) == want:
                yield mt, product, os.path.join(unit, "git", r["dir"]), r["ref"], None
        for s in be._submodule_rows(unit):
            if s["slug"].lower() == want:
                yield mt, product, s["path"], s["ref"], s["parent"]


def check_patch_shipped(patch: str, repo: str, version: str = "") -> dict:
    """Is this fix in the source each release SHIPPED? Content-based, so a
    cherry-pick (different SHA) and a non-public build commit both count."""
    files = parse_patch(patch)
    if not files:
        return {"error": "no file hunks found; pass a unified diff (git show / GitHub .diff)"}
    results, seen = [], set()
    for mt, product, tree, ref, parent in _shipped_trees(repo, version):
        key = (mt.name, product, os.path.realpath(tree))
        if key in seen or not os.path.isdir(tree):
            continue
        seen.add(key)
        per_file = {f["path"]: _check_file(tree, f) for f in files}
        vals = set(per_file.values())
        verdict = ("applied" if vals == {"applied"} else
                   "not_applied" if vals <= {"not_applied", "file_missing"} else "partial")
        results.append({**_where(mt, product), "tree": tree, "ref": ref,
                        "submodule_of": parent, "result": verdict, "files": per_file})
    results.sort(key=lambda r: (r["phase"], _vkey(r["version"] or "0"), r["product"] or ""))

    first: dict[str, str] = {}
    for r in results:
        if r["phase"] == "a" and r["result"] == "applied":
            minor = ".".join(r["version"].split(".")[:2])
            first.setdefault(minor, r["version"])
    out = {"repo": repo, "files_in_patch": [f["path"] for f in files],
           "trees_checked": len(results), "first_applied_phase_a_patch": first,
           "results": results}
    if not results:
        out["note"] = (f"no mounted tree is built from {repo}; check resolve_repo "
                       "(the repo may ship under a fork or wrapper name)")
    else:
        out["note"] = ("applied = every hunk's post-image is in the shipped file; partial = "
                       "some hunks only (or context drifted); a downstream carry patch can "
                       "change context, so read the tree for partial results")
    return out
